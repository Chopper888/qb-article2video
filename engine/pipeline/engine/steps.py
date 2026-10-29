# -*- coding: utf-8 -*-
"""各步骤的实现（S0/S1/S2/S3/S4/S5/S6/S7/S8/S9/S10）。

原则（任务书 §A3）：
  - 每一步一个函数，能真跑的步骤**调用现有函数**（splitter/annotate、word_align、
    image_map/backfill、scene_build、render_mux），不在引擎里另写一套算法；
  - 付费步骤（S1 口播、S3 配音、S8 出图）本轮不接真实调用：已有 `imported`/`done`
    产物且校验通过就跳过，否则报错（见 cli 的 `--no-paid` 处理），退出码 2。

S2 的序数连续组判定（R4b）与统计块是从 `R168/r168_group.py` 的 `main()` 逐行搬来的
（那段逻辑没有独立函数可调）。搬移不改写：字典键顺序、缩进、`indent=1` 均保持一致，
产物与 R168 版本逐字节相同（diff 只有 `narration_path` 这个路径字符串，属预期差异）。
"""
import contextlib
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
import wave
from collections import Counter
from pathlib import Path, PureWindowsPath

from pipeline.engine import (errors, imagegen, intake, ledger, netretry, paid, paths, pricing,
                             render_project, spec, stopflag, stubs)
from pipeline import elements
# StepError 定义搬到 errors.py（paid.py 也要抛它，互相 import 会成环）；
# 这里 re-export，保持 `from pipeline.engine.steps import StepError` 的老用法可用。
from pipeline.engine.errors import PaidBlocked, StepError  # noqa: F401

_REPO_ROOT = Path(__file__).resolve().parents[2]


@contextlib.contextmanager
def _child_stdout_to_stderr():
    """把子进程继承的 fd 1 临时改指到 fd 2。

    Remotion CLI 的打包进度（`Bundling 6%`…）是子进程自己往 stdout 写的，不经过
    Python 的 `sys.stdout`，会混进事件流。事件流只允许一行一个 JSON，所以在调用
    渲染子进程期间把 fd 1 让给 stderr（步骤内部不 emit 事件，安全）。
    """
    try:
        sys.stdout.flush()
        saved = os.dup(1)
    except Exception:
        yield
        return
    try:
        os.dup2(2, 1)
        yield
    finally:
        try:
            os.dup2(saved, 1)
        finally:
            os.close(saved)


class Ctx:
    """一次 `run` 的运行上下文。"""

    def __init__(self, root, article, delivery_root, tmp_dir, no_paid, log,
                 runtime_node_modules=None, max_cost_cny=None, stub=True):
        self.root = Path(root)
        self.article = article
        self.delivery_root = Path(delivery_root) if delivery_root else None
        self.tmp_dir = Path(tmp_dir) if tmp_dir else None
        self.no_paid = no_paid
        self.log = log
        # 不传时按默认规则从工作根目录推：`<work-root 的上一级>\_runtime\node_modules`
        # （root = <work-root>\<article>，所以 root.parent 就是 work-root）。
        self.runtime_node_modules = (Path(runtime_node_modules) if runtime_node_modules
                                     else paths.resolve_runtime_node_modules(self.root.parent))
        # S0 判定出的内容类型；S0 失败时给 run_done 带 content_type 用
        self.resolved_content_type = None
        # ---- 付费（ST1-P4a）：统一入口 paid.perform 的上下文 ----
        self.max_cost_cny = max_cost_cny     # 本篇费用上限（元）；None = 不限
        self.stub = stub                     # True = 本轮只走桩，不发真实请求
        self.spent_cny = 0.0                 # 已累计花费（由 cli 从账本 totals 垫上）
        self.ledger_doc = None               # 账本对象引用（由 cli 注入），供即时记账
        # ST3-P26：S8 并行出图时，即时记账和任务号登记都改这一份账本，改和落盘都要先拿这把锁。
        self.ledger_lock = threading.RLock()
        # 步骤内进度（ST3-P23 §3.6）：cli 注入 `progress(step, done, total, unit, message=None)`，
        # 节流在 cli 里做；没注入时（测试、工具脚本）什么也不发。
        self.progress_cb = None
        self.reset_paid()
        # ST3-P28b：对齐结果以磁盘为准——整个进程内 word_align / R148 都从本篇的
        # `04-对齐/` 读 S4 的成果（S4 自己算新旧时会临时 suspend）。
        from pipeline import align_global
        align_global.set_source(self.root)

    def reset_paid(self):
        """每步开始前清零本步的付费统计。"""
        self.paid_stats = {"calls": 0, "paid_calls": 0, "hits": 0, "stub_calls": 0,
                           "estimated_cny": 0.0, "incomplete": False, "real_cny": 0.0}
        self.paid_log = []

    def progress(self, step, done, total, unit, message=None):
        cb = getattr(self, "progress_cb", None)
        if cb is not None:
            cb(step, done, total, unit, message)

    def record_paid(self, cny, step):
        """真实付费成功后的即时记账（ST3-P2b 缺陷a）。

        每次真实付费调用成功后立即：把金额累加到 `spent_cny`（费用上限预检用），
        并把金额写进账本当前步骤的 `cost` 并原子落盘——中断/强杀后账本不丢已成功调用。
        缓存命中与桩调用不走这里（不计费 / 保持现状）。
        """
        with self.ledger_lock:
            self.spent_cny = round(self.spent_cny + float(cny), 4)
            doc = getattr(self, "ledger_doc", None)
            if doc is None:
                return
            rec = ledger.find_step(doc, step)
            if rec is None:
                return
            c = rec.setdefault("cost", {"calls": 0, "paid_calls": 0, "cache_hits": 0,
                                        "stub_calls": 0, "estimated_cny": 0.0,
                                        "incomplete": False})
            c["calls"] = int(c.get("calls", 0)) + 1
            c["paid_calls"] = int(c.get("paid_calls", 0)) + 1
            prev_cny = float(c.get("estimated_cny") or 0.0)
            c["estimated_cny"] = round(prev_cny + float(cny), 4)
            ledger.save_atomic(self.root, ledger.touch(doc))

    def p(self, *parts):
        return self.root.joinpath(*parts)


# --------------------------------------------------------------------------
# 付费步骤（本轮不接真实调用）
# --------------------------------------------------------------------------

# 付费步骤 -> (要调用的东西, 需要的密钥)
PAID_STEP_CALL = {
    "S0": ("方舟识图分类", ["ARK_API_KEY"]),
    "S1": ("DeepSeek 口播生成", ["DEEPSEEK_API_KEY"]),
    "S3": ("豆包 TTS 配音", ["MODEL_SPEECH_API_KEY"]),
    "S6": ("DeepSeek 导演链 + 方舟识图", ["DEEPSEEK_API_KEY", "ARK_API_KEY"]),
    "S7": ("DeepSeek 画面提炼", ["DEEPSEEK_API_KEY"]),
    "S8": ("RunningHub 出图", ["RUNNINGHUB_API_KEY"]),
}

# 各付费点缓存落在哪个目录（A5 第 1 条）。S6 的导演链/图句匹配/标签共用一个缓存目录，
# 停留判断另一个——与历史脚本（P7\cache、P9\cache）一致，故历史缓存继续命中。
_CACHE_DIR = {
    "S0": ("00-输入", "cache"),
    "S1": ("01-口播", "cache"),
    "S3": ("03-配音", "cache"),
    "S6_DIRECTOR": ("06-编排", "cache"),
    "S6_STAY": ("05-截图分析", "cache"),
    "S7": ("06-编排", "cache"),
    "S8": ("07-出图", "cache"),
}

_DEEPSEEK_MODEL = "deepseek-v4-pro"                 # 同 shot_ai.DEEPSEEK_MODEL
_ARK_MODEL = "doubao-seed-2-1-pro-260915"           # 同 shot_ai.ARK_MODEL
_SPEECH_MODEL = "doubao-tts-seed-icl-2.0"           # 豆包长文本 TTS（resource id seed-icl-2.0）


def _progress(ctx, step, done, total, unit, message=None):
    """发步骤内进度（ST3-P23 §3.6）。测试和工具脚本里的简化上下文没有 progress，就什么也不发。"""
    fn = getattr(ctx, "progress", None)
    if callable(fn):
        fn(step, done, total, unit, message)


def _cache_dir(ctx, which):
    return ctx.p(*_CACHE_DIR[which])


def _require_env(name):
    """密钥**只读环境变量**（ST1-P4a2 §一.5）：不查注册表、不读 dotenv。"""
    v = os.environ.get(name)
    if not v:
        raise StepError(f"缺少环境变量密钥: {name}", kind=errors.KIND_KEY_MISSING,
                        service=errors.ENV_SERVICE.get(name))
    return v


_SERVICE_NAME = {"deepseek": "DeepSeek", "ark": "方舟"}


def _raise_if_auth_failed(calls):
    """ST3-P22：shot_ai 的调用返回 None 时看一眼最后一次的 HTTP 状态——密钥错误 / 余额不足立即失败
    （不再让上层当"模型没返回"去重试、兜底）；其他失败照旧返回 None。"""
    last = calls[-1] if calls else {}
    kind = errors.http_kind(last.get("status"), last.get("body"))
    if kind:
        name = _SERVICE_NAME.get(last.get("api"), last.get("api"))
        what = "余额不足" if kind == errors.KIND_BALANCE else "密钥错误或无权限"
        raise StepError(f"{name} {what}：HTTP {last.get('status')} {(last.get('body') or '')[:200]}",
                        kind=kind)


# shot_ai 的真实调用把异常吞掉、只记异常类名（返回 None）；这些类名算临时网络故障（requests 的异常）。
_TRANSIENT_EXC_NAMES = frozenset({"ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "SSLError",
                                  "ProxyError", "ChunkedEncodingError"})


def _shot_ai_transient(calls):
    """ST3-P26：shot_ai 的调用返回 None 时，看最后一次是不是临时故障（返回原因文字；不是返回 None）。"""
    last = calls[-1] if calls else {}
    if last.get("status") in netretry.TRANSIENT_HTTP_IDEMPOTENT:
        return f"HTTP {last.get('status')}"
    if "status" not in last and last.get("error") in _TRANSIENT_EXC_NAMES:
        return last.get("error")
    return None


def _real_text_call(model):
    """引擎侧真实文本调用（DeepSeek）。密钥延迟到真正发请求时才读（ST3-F1 第6项附带：
    纯缓存回放 / --no-paid 时不要求密钥）。返回可被 `perform` 延迟调用的可调用对象。
    ST3-P26：临时网络故障按 `netretry` 重试；重试用完仍返回 None（调用方照旧当"没返回"处理）。"""
    from pipeline import shot_ai

    def call(prompt, max_tokens=4096):
        tc = shot_ai.make_real_text_call(api_key=_require_env("DEEPSEEK_API_KEY"))
        out = netretry.call(lambda: tc(prompt, max_tokens=max_tokens), service="DeepSeek", what="文本",
                            retry_on=lambda o: _shot_ai_transient(tc.calls) if o is None else None)
        _note_usage(tc.calls)
        if out is None:
            _raise_if_auth_failed(tc.calls)
        return out
    return call


def _note_usage(calls):
    """ST3-P23 §3.4：把 shot_ai 记下的 usage 报给统一入口（按实际用量计价）。"""
    for c in calls or []:
        if c.get("prompt") is not None or c.get("completion") is not None:
            paid.note_usage(c.get("prompt"), c.get("completion"))


def _real_vision_call(model):
    """引擎侧真实识图调用（方舟）。密钥延迟到真正发请求时才读、临时故障重试（同 `_real_text_call`）。"""
    from pipeline import shot_ai

    def call(image_bytes, prompt, max_tokens=512, timeout=60):
        vc = shot_ai.make_real_vision_call(api_key=_require_env("ARK_API_KEY"))
        out = netretry.call(lambda: vc(image_bytes, prompt, max_tokens=max_tokens, timeout=timeout),
                            service="方舟", what="识图",
                            retry_on=lambda o: _shot_ai_transient(vc.calls) if o is None else None)
        _note_usage(vc.calls)
        if out is None:
            _raise_if_auth_failed(vc.calls)
        return out
    return call


def _refuse_paid(ctx, sid):
    what, keys = PAID_STEP_CALL[sid]
    if ctx.no_paid:
        raise StepError(f"禁止付费：需要调用 {what}（{sid}，密钥 {'/'.join(keys)}）",
                        kind=errors.KIND_PAID_BLOCKED)
    raise StepError(f"本轮未接入付费调用：需要调用 {what}（{sid}，密钥 {'/'.join(keys)}）")


# --------------------------------------------------------------------------
# S0 识图分类（付费点；缺分类时才走）
# --------------------------------------------------------------------------

_CLS_PROMPT = ("判断这张图属于哪一类：如果它是**软件界面/网页/聊天窗口/报错信息/终端**的"
               "真实截图，输出 截图；否则输出 配图。只输出 JSON："
               '{"分类": "截图"} 或 {"分类": "配图"}')


def _parse_cls(raw):
    """从识图返回里取分类。解析不出按"配图"（保守：宁可不判成截图型）。"""
    try:
        v = json.loads(raw)
        if isinstance(v, dict):
            return v.get("分类") or v.get("label") or "配图"
    except Exception:
        pass
    return "截图" if "截图" in (raw or "") and "非截图" not in (raw or "") else "配图"


def _classify_images(ctx, imgs, items):
    """对缺分类的图逐张走统一付费入口（S0）。命中 `00-输入/cache` 就不发请求。"""
    vision = paid.make_paid_vision_call(
        ctx, step="S0", cache_dir=_cache_dir(ctx, "S0"), model=_ARK_MODEL,
        describe=f"需要识图分类 {len(imgs)} 张", invoke_real=_real_vision_call(_ARK_MODEL),
        invoke_stub=stubs.stub_vision_classify)
    src_dir = ctx.p("00-输入")
    for i, im in enumerate(imgs):
        rel = im["rel"].split("/", 1)[1] if im["rel"].startswith("00-输入/") else im["rel"]
        data = (src_dir / rel).read_bytes()
        raw = vision(data, _CLS_PROMPT)
        items[im["sha256"]] = {"分类": _parse_cls(raw), "来源": "识图"}
        _progress(ctx, "S0", i + 1, len(imgs), "张")
    return items


# --------------------------------------------------------------------------
# S1 口播（付费点）
# --------------------------------------------------------------------------


def _pseudo_narration(md_text):
    """把文章 md 粗略改写成"像口播稿"的形态（桩用，不替代真实模板重写）。

    - 去掉整行的图片语法；- `# 标题` → `【标题】`（与真实 narration.txt 的标题写法一致）。
    """
    out = []
    for ln in md_text.splitlines():
        s = ln.strip()
        if not s:
            out.append("")
            continue
        if re.match(r"^!\[.*\]\(.*\)\s*$", s):
            continue
        m = re.match(r"^#{1,6}\s+(.*)$", s)
        if m:
            out.append(f"【{m.group(1).strip()}】")
            continue
        out.append(s)
    return "\n".join(out)


# S1 口播模板（ST1-P5b 起从 vendor 读取，不再依赖外部「中转站」目录）
_NARRATION_TEMPLATE = paths.VENDOR_DIR / "zhongzhuanzhan" / "Article-to-Narration提示词模板-v2_3.md"

# ST3-P2 B：含代码块/命令/表格的文章，在口播提示词**末尾**追加的补充规则。
# ST3-P3b 第4项：命令和设置项不逐字念。ST3-P3d 第4项：对所有文章追加（不再以是否含要素为前提）。
# ST3-P3c 修复二：命令行明确「不得点名」，并给出反例/正例。
_ELEMENT_RULE = (
    "\n\n【补充规则：本文含代码块 / 命令行 / 表格】\n"
    "- 命令行：说明这一步的作用即可，不要念出命令名、参数、包名、网址或文件路径；\n"
    "- 配置/设置类内容（环境变量、配置文件、参数设置等）：讲清在设置什么，"
    "提示观众按画面中的内容设置，不要逐项念出具体的值；\n"
    "- 代码块：交代代码的用途，不要逐行朗读，也不要念长字符串或密钥类内容；\n"
    "- 表格：概括表格的主题，只讲重点，不要逐格念。"
)

# 开场白由系统生成，正文直接从第一个要点开始。
_OPENING_RULE = (
    "\n\n【补充规则：开场白】\n"
    "开场白由系统生成，正文直接从第一个要点开始；不要再介绍标题，也不要复述文章编号或分类。"
)


def _narration_topic(title):
    """从文章标题提取口播主题：去星号等装饰符号，再去掉"编号 分类-"前缀。

    例：'＊2.3.7 基础篇-如何安装pytorch和cuda，以及Xformers' -> '如何安装pytorch和cuda，以及Xformers'。
    解析不出前缀时返回（去掉装饰符号后的）完整标题。
    """
    if not title:
        return ""
    t = title.strip()
    t = re.sub(r"^[\s＊*★☆#]+", "", t)
    m = re.match(r"^\d+(?:\.\d+)*\s*[^-—–]*?[-—–]\s*", t)
    if m:
        t = t[m.end():].strip()
    return t or title.strip()


def _article_title(ctx):
    """取文章标题：优先 来源.json 的「清洗后标题/原标题」，其次 文章.md 首个标题。"""
    src = ctx.p("00-输入", "来源.json")
    if src.exists():
        try:
            d = json.loads(src.read_text(encoding="utf-8"))
            t = d.get("清洗后标题") or d.get("原标题")
            if t:
                return t.strip()
        except Exception:
            pass
    art = ctx.p("00-输入", "文章.md")
    if art.exists():
        for ln in art.read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s.startswith("#"):
                return re.sub(r"^#+\s*", "", s).strip() or ctx.article
    return ctx.article

# ST3-F1 第6项：口播命令点名检查规则常量表。(名称, 正则)
# 去掉了 `\b`——中文是 Unicode `\w`，`\b` 在"汉字↔ASCII"之间不构成边界，导致"输入cmd"
# 这种紧贴写法漏检；改用 ASCII 字母/数字边界。`cmd`/`powershell`/`python`/`py` 作为
# "单独提到的词"不算命中（"在地址栏输入 cmd"只是在说打开终端，不是在念命令）。
# 命令关键词（pip/conda/git…）后紧跟中文或中文标点时视为"提到"而非"念命令"，
# 如"pip，也就是安装命令"不算命中；"pip3 install …"才算。
_ASCII_EDGE = r"(?<![A-Za-z0-9])"
_COMMAND_RULES = [
    ("命令关键词", re.compile(_ASCII_EDGE + r"(pip3?|conda|git|npm|npx|pnpm|yarn|curl|wget)"
                              r"(?![A-Za-z0-9\u4e00-\u9fff，。！？；：、（）])")),
    ("python -m", re.compile(r"python3?\s+-m", re.I)),
    ("cd/set/export 命令", re.compile(r"(?:^|[\s，。；])（?(cd|set|export)\s+[A-Za-z]", re.I)),
    ("网址", re.compile(r"https?://|www\.", re.I)),
    ("盘符路径", re.compile(r"[A-Za-z]:\\")),
    ("斜杠路径", re.compile(r"(?:^|[\s(（])/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")),
    ("双横线参数", re.compile(r"\s--[A-Za-z]")),
]

_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；\n])")


def _split_sentences(text):
    """按句切分：以 。！？； 和换行为切分点，保留句末标点。"""
    return [p.strip() for p in _SENT_SPLIT_RE.split(text) if p.strip()]


def _find_command_hits(narration):
    """ST3-F1 第6项：按句扫描口播，返回真正念出命令的句子（去重）。"""
    hits = []
    for s in _split_sentences(narration):
        if s.startswith("【"):
            continue
        for name, pat in _COMMAND_RULES:
            if pat.search(s):
                hits.append({"sentence": s, "rule": name})
                break
    return hits


def _build_narration_messages(template, article_text):
    """把模板拆成 system + user，并把文章嵌入。"""
    lines = template.splitlines()
    marker = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("## User Prompt") or ln.strip().lower().startswith("user prompt"):
            marker = i
            break
    if marker is None:
        system = template
        user = ("请按以上要求，把下面这篇文章改写成自然、完整的视频口播稿。\n\n【原始文章】\n\n" + article_text)
    else:
        system = "\n".join(lines[:marker])
        user = "\n".join(lines[marker:])
    if "{{source_article}}" in user:
        user = user.replace("{{source_article}}", article_text)
    else:
        user = user + "\n\n【原始文章】\n\n" + article_text
    return system, user


def _real_narration_call(system, user):
    """真实口播（DeepSeek，R168 配置：reasoning_effort=high + thinking 开启），返回 content 字符串。

    ST3-P26：临时网络故障（连接 / SSL / 读超时、HTTP 429 / 500 / 502 / 503 / 504）按 `netretry` 退避重试，
    用完 → network；密钥错误 / 余额不足、其他 4xx 立即失败。回复被截断或为空（内容类，不是网络问题）
    沿用原来的做法：隔 60 秒再要一次，最多 3 次。
    """
    import requests
    api_key = _require_env("DEEPSEEK_API_KEY")
    body = {
        "model": _DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": 32768, "stream": False,
        "reasoning_effort": "high", "thinking": {"type": "enabled"},
    }

    def _post():
        return requests.post("https://api.deepseek.com/chat/completions",
                             headers={"Authorization": f"Bearer {api_key}",
                                      "Content-Type": "application/json"},
                             json=body, timeout=600)

    def _transient(r):
        return f"HTTP {r.status_code}" if r.status_code in netretry.TRANSIENT_HTTP_IDEMPOTENT else None

    last = None
    for attempt in range(3):
        try:
            r = netretry.call(_post, service="DeepSeek", what="口播", retry_on=_transient)
        except Exception as exc:
            if netretry.is_transient_exc(exc):
                raise StepError(f"DeepSeek 口播失败（网络，重试用完）: {type(exc).__name__}: {exc}",
                                kind=errors.KIND_NETWORK) from exc
            raise StepError(f"DeepSeek 口播失败: {type(exc).__name__}: {exc}",
                            kind=errors.KIND_SERVICE) from exc
        if r.status_code != 200:
            last = RuntimeError(f"HTTP {r.status_code}: {(r.text or '')[:200]}")
            # ST3-P22：密钥错误 / 余额不足重试也没用，立即失败
            kind = errors.http_kind(r.status_code, r.text)
            if kind:
                what = "余额不足" if kind == errors.KIND_BALANCE else "密钥错误或无权限"
                raise StepError(f"DeepSeek 口播失败（{what}）: {last}", kind=kind)
            kind = errors.KIND_NETWORK if _transient(r) else errors.KIND_SERVICE
            raise StepError(f"DeepSeek 口播失败: RuntimeError: {last}", kind=kind)
        try:
            data = r.json()
            u = data.get("usage") or {}
            paid.note_usage(u.get("prompt_tokens"), u.get("completion_tokens"))
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except Exception as exc:
            raise StepError(f"DeepSeek 口播失败（回复无法解析）: {type(exc).__name__}: {exc}",
                            kind=errors.KIND_SERVICE) from exc
        if choice.get("finish_reason") == "length":
            last = RuntimeError("finish_reason=length (截断)")
        elif not content or not content.strip():
            last = RuntimeError("empty content")
        else:
            return content
        if attempt < 2:
            stopflag.sleep(60)
    raise StepError(f"DeepSeek 口播失败: {type(last).__name__}: {last}", kind=errors.KIND_SERVICE)


CMD_CLOSING_COPY = "复制屏幕上这行命令即可"
CMD_CLOSING_TYPE = "输入屏幕上的命令即可"
_COPY_ACTION_RE = re.compile(r"复制(?!.{0,12}官网)|拷贝|粘贴")


def command_closing(sentence):
    """ST3-P12 第 11 步：命令句的统一说法。这一步的动作是「复制」→ 复制屏幕上这行命令即可；
    是「输入」（或没写明）→ 输入屏幕上的命令即可。「从 xx 官网复制」说的是命令的来源，不算动作。"""
    s = re.sub(r"从[^，。；]{0,20}官网复制", "", sentence or "")
    return CMD_CLOSING_COPY if _COPY_ACTION_RE.search(s) else CMD_CLOSING_TYPE


def _rewrite_command_prompt(sentence):
    return (
        "下面这句口播提到了命令、网址或文件路径，请改写：\n"
        f"原句：{sentence}\n"
        "要求：不要念出命令名、参数、包名、网址或文件路径；讲清这一步的作用，"
        f"然后统一说“{command_closing(sentence)}”。原句写明了命令从哪里来时，"
        "后面再加一句“这条命令可以从 xx 官网复制”，xx 照原句的写法。\n"
        "只输出改写后的句子，不要任何其他文字。"
    )


def _rewrite_command_line(ctx, sentence, cache_dir):
    """ST3-P3d 第4项：把命中命令的句子交 DeepSeek 重写（不念命令）。返回 (重写句, 命中次)。"""
    prompt = _rewrite_command_prompt(sentence)
    key = paid.cache_key(_DEEPSEEK_MODEL, prompt, {"rewrite_command": True})
    out = paid.perform(ctx, step="S1", service="deepseek", cache_dir=cache_dir, key=key,
                       describe="口播命令重写", quantity=0.0,
                       prompt_chars=len(prompt) + 12, max_tokens=32768,
                       invoke_real=lambda: _real_narration_call("你是口播稿改写助手。", prompt),
                       invoke_stub=lambda: sentence)
    return (out or sentence).strip()


# R101 E：模板要求每个标题后加过渡语，过渡语有时和标题前一句几乎相同（同一句话念两遍）。
# 相邻两句（中间隔着【标题】行也算相邻）归一化后相似度 ≥ 这个值、且不短于下面的字数 → 删掉后一句。
DUP_SENTENCE_MIN_RATIO = 0.9
DUP_SENTENCE_MIN_CHARS = 4
_DUP_SENT_END = "。！？!?"


def _split_keep(line):
    """一行按句末标点切开，保留标点（拼回去与原行逐字相同）。"""
    out, cur = [], ""
    for ch in line:
        cur += ch
        if ch in _DUP_SENT_END:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def dedupe_adjacent_sentences(text):
    """R101 E：删掉与前一句几乎相同的后一句。返回 (新文本, 删除记录)。【标题】行原样保留、不参与比较。"""
    import difflib
    from pipeline import allocate
    lines = text.split("\n")
    removed = []
    prev = None
    out = []
    for ln in lines:
        if re.match(r"^\s*【.*】\s*$", ln) or not ln.strip():
            out.append(ln)
            continue
        kept = []
        for s in _split_keep(ln):
            n = allocate.norm(s)
            if not n:
                kept.append(s)
                continue
            if (prev is not None and min(len(n), len(prev[1])) >= DUP_SENTENCE_MIN_CHARS
                    and difflib.SequenceMatcher(None, prev[1], n, autojunk=False).ratio()
                    >= DUP_SENTENCE_MIN_RATIO):
                removed.append({"删掉": s.strip(), "前一句": prev[0].strip()})
                continue
            kept.append(s)
            prev = (s, n)
        new = "".join(kept)
        if new == ln:
            out.append(ln)              # 这一行没删东西：逐字不变
        elif new.strip():
            out.append(new.rstrip())
        # 整行都删掉了：这一行不要
    return "\n".join(out), removed


def compose_narration(ctx):
    """S1 的计算部分（不写文件）：生成 / 回放口播 → 命令句重写 → 加开场白。

    R101 K：费用估算用 `--no-paid` 的上下文调它，看口播能不能从缓存原样回放（命中缓存不写盘、不联网，
    没命中直接抛 PaidBlocked）。`step_s1` 调它再把结果写出来，两者走同一套代码、同一个缓存键。
    返回 {"art", "text", "rewrite_result", "still_hits", "rewrite_log", "dedup"}。
    """
    art = ctx.p("00-输入", "文章.md")
    if not art.exists():
        raise StepError(f"缺少输入文章: {art}")
    text = art.read_text(encoding="utf-8")

    cache_dir = _cache_dir(ctx, "S1")
    tpl_p = Path(_NARRATION_TEMPLATE)
    template = tpl_p.read_text(encoding="utf-8")
    system, user = _build_narration_messages(template, text)
    tpl_variants = newline_variants(tpl_p.read_bytes())       # R101 N：[原样, 全 LF, 全 CRLF]
    src_sha = hashlib.sha256(tpl_variants[0]).hexdigest()
    # ST3-P3d 第4项：命令不点名规则对所有文章追加（不再以是否含要素为前提）。
    # ST3-F1 第3项：开场白规则追加，正文直接从第一个要点开始。
    params = {"max_tokens": 32768, "reasoning_effort": "high", "thinking": "enabled",
              "template_sha256": src_sha,
              "element_rule_sha256": hashlib.sha256(_ELEMENT_RULE.encode("utf-8")).hexdigest(),
              "opening_rule_sha256": hashlib.sha256(_OPENING_RULE.encode("utf-8")).hexdigest()}
    user = user + _ELEMENT_RULE + _OPENING_RULE
    key = paid.cache_key(_DEEPSEEK_MODEL, text, params)
    # R101 N：模板换行符不同（Windows / Mac 检出、不同版本的安装包）时按其他写法算的键也查
    alt_keys = [paid.cache_key(_DEEPSEEK_MODEL, text,
                               dict(params, template_sha256=hashlib.sha256(v).hexdigest()))
                for v in tpl_variants[1:]]
    # 相邻重复句只在新生成时删除（在付费回调里做，缓存保存的已经是删过的稿）。
    # 老文章的旧缓存直接沿用：重跑 S1 时口播不变，配音缓存仍然命中。
    dedup = {"ran": False, "removed": []}

    def _fresh(gen):
        out, removed = dedupe_adjacent_sentences(gen() or "")
        dedup.update(ran=True, removed=removed)
        return out

    out_text = paid.perform(
        ctx, step="S1", service="deepseek", cache_dir=cache_dir, key=key,
        describe="口播生成", quantity=0.0, alt_keys=alt_keys,
        prompt_chars=len(system) + len(user), max_tokens=params["max_tokens"],
        invoke_real=lambda: _fresh(lambda: _real_narration_call(system, user)),
        invoke_stub=lambda: _fresh(lambda: _pseudo_narration(text)))
    out_text = (out_text or "").strip()

    # 口播命令点名检查：按句命中时只重写这一句（最多 2 次），仍命中则标红。
    rewrite_log = []
    for attempt in range(2):
        hits = _find_command_hits(out_text)
        if not hits:
            break
        for h in hits:
            rewritten = _rewrite_command_line(ctx, h["sentence"], cache_dir)
            if rewritten != h["sentence"]:
                out_text = out_text.replace(h["sentence"], rewritten, 1)
            rewrite_log.append({"attempt": attempt + 1, "rule": h["rule"],
                                "before": h["sentence"], "after": rewritten})
    still_hits = _find_command_hits(out_text)
    rewrite_result = {"log": rewrite_log, "still_hits": still_hits}

    # ST3-F1 第3项：系统生成的固定开场白，作为 narration.txt 第一行（命令检查在其后，
    # 开场白不含命令，不会误伤）。
    topic = _narration_topic(_article_title(ctx))
    out_text = f"今天介绍一下，{topic}。\n" + out_text
    return {"art": art, "text": out_text, "rewrite_result": rewrite_result, "still_hits": still_hits,
            "rewrite_log": rewrite_log, "dedup": dedup}


def step_s1(ctx):
    """口播稿：走统一付费入口（DeepSeek，R168 模板）。产物 `01-口播/narration.txt` + `narration_meta.json`。"""
    c = compose_narration(ctx)
    art, out_text, rewrite_result = c["art"], c["text"], c["rewrite_result"]
    still_hits, rewrite_log, dedup = c["still_hits"], c["rewrite_log"], c["dedup"]
    ctx.rewrite_result = rewrite_result

    out_dir = ctx.p("01-口播")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "narration.txt").write_text(out_text + "\n", encoding="utf-8")
    (out_dir / "命令重写记录.json").write_text(
        json.dumps(rewrite_result, ensure_ascii=False, indent=1), encoding="utf-8")
    if dedup["ran"]:
        (out_dir / "重复句删除记录.json").write_text(json.dumps({
            "阈值": DUP_SENTENCE_MIN_RATIO, "最短字数": DUP_SENTENCE_MIN_CHARS,
            "删除": dedup["removed"]}, ensure_ascii=False, indent=1), encoding="utf-8")
        if dedup["removed"]:
            ctx.log(f"[S1] 删掉与前一句几乎相同的句子 {len(dedup['removed'])} 处："
                    + "；".join(x["删掉"] for x in dedup["removed"]))
    meta = {"article_path": str(art), "model": _DEEPSEEK_MODEL,
            "output_chars_nonspace": len(re.sub(r"\s", "", out_text)),
            "output_lines": len(out_text.splitlines()),
            "source": ("桩" if ctx.stub else "真实/缓存")}
    (out_dir / "narration_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    st = ctx.paid_stats
    ctx.log(f"[S1] 口播 {meta['output_chars_nonspace']} 字 / 调用 {st['calls']}"
            f" / 命中 {st['hits']} / 桩 {st['stub_calls']} / 命令重写 {len(rewrite_log)} 次"
            f" / 仍命中 {len(still_hits)} -> {out_dir / 'narration.txt'}")
    return {"source": ("桩（口播原文代答）" if st["stub_calls"] else None),
            "rewrite_result": rewrite_result,
            "command_still_hits": [h["sentence"] for h in still_hits]}


# --------------------------------------------------------------------------
# S3 配音（付费点；豆包异步：先落 pending_jobs 再查）
# --------------------------------------------------------------------------


_CN_DIGITS = "零一二三四五六七八九"


def _cn_int(n):
    """非负整数转中文数字（0→零，12→十二，105→一百零五，2003→二千零三）。"""
    n = int(n)
    if n < 0:
        return "负" + _cn_int(-n)
    if n == 0:
        return "零"
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + ("" if n == 10 else _CN_DIGITS[n - 10])
    if n < 100:
        tens, ones = divmod(n, 10)
        return _CN_DIGITS[tens] + "十" + ("" if ones == 0 else _CN_DIGITS[ones])
    if n < 1000:
        h, rest = divmod(n, 100)
        return _CN_DIGITS[h] + "百" + ("" if rest == 0 else ("零" if rest < 10 else "") + _cn_int(rest))
    if n < 10000:
        t, rest = divmod(n, 1000)
        return _CN_DIGITS[t] + "千" + ("" if rest == 0 else ("零" if rest < 100 else "") + _cn_int(rest))
    if n < 100000000:
        w, rest = divmod(n, 10000)
        return _cn_int(w) + "万" + ("" if rest == 0 else ("零" if rest < 1000 else "") + _cn_int(rest))
    y, rest = divmod(n, 100000000)
    return _cn_int(y) + "亿" + ("" if rest == 0 else ("零" if rest < 10000000 else "") + _cn_int(rest))


def _version_to_cn(m):
    """把 `2.3.7` 这类含两个及以上点号的数字串转成「二点三点七」；
    带字母后缀（如 `.post7`）的，数字部分照转、后缀前加空格保留原样。"""
    num_part = m.group(1)
    suffix = m.group(2)
    cn = "点".join(_cn_int(seg) for seg in num_part.split("."))
    if suffix:
        cn += "".join(" " + seg for seg in suffix.split(".") if seg)
    return cn


_CJK = "\u3400-\u4dbf\u4e00-\u9fff"
_CJK_ASCII_GAP_RE = re.compile(
    rf"(?<=[{_CJK}])(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])(?=[{_CJK}])")


def _tts_text(narration):
    """提交给 TTS 的文本：去掉 `【标题】` 行（豆包会把标题念出来，S1-R45 的结论）。"""
    keep = []
    for ln in narration.splitlines():
        if re.match(r"^\s*【.*】\s*$", ln):
            continue
        keep.append(ln)
    text = "\n".join(keep).strip()
    # ST3-P3c 修复四：英文标识符(两侧 ASCII 字母/数字)里的下划线 → 空格，
    # 豆包才按词念("DISTUTILS USE SDK")，而不是把 _ 念成 "underscore"。
    # 只改 TTS 输入、不改字幕；1:1 由 narration_prep.prep_tts_text 同步维护。
    text = re.sub(r"(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])", " ", text)
    # ST3-P3d 修复一：「第 2.3.7 节」点分序号的「第」删掉（豆包念「第二点三点七」语调奇怪）。
    text = re.sub(r"第(?=\s*\d+(?:\.\d+)+)", "", text)
    # ST3-F1 第5项：含两个及以上点号的数字串（版本号）转中文逐段读法，避免被当小数念。
    # 只含一个点号（如 12.4）不做处理，保持豆包默认读法。
    text = re.sub(r"(\d+(?:\.\d+){2,})((?:\.[A-Za-z][A-Za-z0-9]*)*)", _version_to_cn, text)
    # ST3-P9：汉字与英文字母/数字相邻处插一个空格（`pytorch和cuda` → `pytorch 和 cuda`），
    # 豆包才把英文词当独立的词念。放在最后做，已转成中文读法的版本号不受影响。
    # 只改 TTS 输入：字幕取的是分组原文（scene_build 的 subtitle_lines），不经过这里。
    text = _CJK_ASCII_GAP_RE.sub(" ", text)
    return text


def _stub_tts_submit(text, cache_dir):
    """桩：提交"任务"。返回稳定 task_id，并把提交内容落到 `<cache>/tts_jobs/<id>.json`，
    让**重跑的新进程**也能按任务号查到结果（模拟真实豆包的异步任务）。"""
    tid = "stub-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    d = Path(cache_dir) / "tts_jobs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.json").write_text(json.dumps({"text": text}, ensure_ascii=False),
                                   encoding="utf-8")
    log = d / "_submissions.log"
    with open(log, "a", encoding="utf-8") as f:
        f.write(tid + "\n")
    return tid


def _stub_tts_query(task_id, cache_dir):
    d = Path(cache_dir) / "tts_jobs"
    p = d / f"{task_id}.json"
    if not p.is_file():
        raise StepError(f"桩：查不到任务号 {task_id}（未提交过）")
    payload = json.loads(p.read_text(encoding="utf-8"))
    return stubs.stub_tts(payload["text"])


# 豆包长文本 TTS（submit/query 请求结构）
_TTS_BASE = "https://openspeech.bytedance.com/api/v3/tts"
# 音色：环境变量 A2V_TTS_SPEAKER（留空用默认公共音色）；资源编号按音色自动选择：
# 复刻音色（S_ 开头）→ seed-icl-2.0；公共音色按代际 → _uranus_bigtts=seed-tts-2.0、_mars_bigtts=seed-tts-1.0。
# 环境变量 A2V_TTS_RESOURCE_ID 非空时手动覆盖。
_TTS_RESOURCE_CLONE = "seed-icl-2.0"
_TTS_RESOURCE_DEFAULT_PUBLIC = "seed-tts-2.0"
_PUBLIC_SPEAKER_RESOURCES = (
    ("_uranus_bigtts", "seed-tts-2.0"),
    ("_mars_bigtts", "seed-tts-1.0"),
)
_TTS_RESOURCE_ENV = "A2V_TTS_RESOURCE_ID"
_DEFAULT_PUBLIC_SPEAKER = "zh_female_shuangkuaisisi_uranus_bigtts"
_TTS_SPEAKER_ENV = "A2V_TTS_SPEAKER"
_TTS_SPEECH_RATE = 25          # 1.25×（narration_prep.SPEECH_RATE_1_25X）


def tts_speaker():
    """配音音色：环境变量 A2V_TTS_SPEAKER，留空用默认公共音色。"""
    return (os.environ.get(_TTS_SPEAKER_ENV) or "").strip() or _DEFAULT_PUBLIC_SPEAKER


def tts_resource_id(speaker=None):
    """按音色选 X-Api-Resource-Id；A2V_TTS_RESOURCE_ID 非空时手动覆盖。"""
    override = (os.environ.get(_TTS_RESOURCE_ENV) or "").strip()
    if override:
        return override
    spk = (speaker if speaker is not None else tts_speaker()).strip()
    if spk.startswith("S_"):
        return _TTS_RESOURCE_CLONE
    for suffix, rid in _PUBLIC_SPEAKER_RESOURCES:
        if spk.endswith(suffix):
            return rid
    return _TTS_RESOURCE_DEFAULT_PUBLIC


def _tts_find(d, key, default=None):
    if isinstance(d, dict):
        if key in d:
            return d[key]
        for v in d.values():
            r = _tts_find(v, key, None)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = _tts_find(v, key, None)
            if r is not None:
                return r
    return default


def _tts_post_once(url, body, api_key, resource_id=None):
    import urllib.request
    import urllib.error
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id or tts_resource_id(),
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            raw = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            raw = {"raw": "HTTP " + str(code)}
        return code, raw


def _tts_post(url, body, api_key, submit=False, resource_id=None):
    """发一次豆包请求，返回 (HTTP 状态, 回复)；连不上（重试用完）返回 (-1, {"exception": …})。

    ST3-P26：临时故障按 `netretry` 重试。`submit=True`（提交任务）只重试确定没发出去的失败和 429 / 503；
    请求已发出、没收到回复的抛 `StepError(network)`，不自动重新提交（防重复扣费）。
    """
    if submit:
        try:
            return netretry.call(lambda: _tts_post_once(url, body, api_key, resource_id), service="豆包配音", what="提交",
                                 retry_exc=netretry.is_unsent_exc,
                                 retry_on=netretry.http_status_retry(netretry.TRANSIENT_HTTP_SUBMIT))
        except Exception as e:
            if netretry.is_transient_exc(e) and not netretry.is_unsent_exc(e):
                raise StepError(f"豆包 TTS 提交请求已发出、但没收到回复（{netretry.describe_exc(e)}）；"
                                f"服务端可能已经建了任务，为防重复扣费不自动重新提交，重跑会重新提交",
                                kind=errors.KIND_NETWORK) from e
            return -1, {"exception": repr(e)}
    try:
        return netretry.call(lambda: _tts_post_once(url, body, api_key, resource_id), service="豆包配音", what="查询",
                             retry_on=netretry.http_status_retry(netretry.TRANSIENT_HTTP_IDEMPOTENT))
    except Exception as e:
        return -1, {"exception": repr(e)}


def _real_tts_submit(text, cache_dir):
    api_key = _require_env("MODEL_SPEECH_API_KEY")
    speaker = tts_speaker()
    body = {
        "user": {"uid": "s1-engine"},
        "unique_id": str(uuid.uuid4()),
        "req_params": {
            "text": text,
            "speaker": speaker,
            "audio_params": {
                "format": "wav",
                "sample_rate": 24000,
                "speech_rate": _TTS_SPEECH_RATE,
                "loudness_rate": 0,
                "enable_timestamp": True,
            },
        },
    }
    code, resp = _tts_post(_TTS_BASE + "/submit", body, api_key, submit=True,
                           resource_id=tts_resource_id(speaker))
    if code == -1 or (isinstance(resp, dict) and resp.get("code") not in (0, 20000000)):
        kind = (errors.KIND_NETWORK if code == -1 or code in netretry.TRANSIENT_HTTP
                else errors.http_kind(code, json.dumps(resp, ensure_ascii=False)) or errors.KIND_SERVICE)
        raise StepError(f"豆包 TTS 提交失败: HTTP {code} {json.dumps(resp, ensure_ascii=False)[:300]}",
                        kind=kind)
    task_id = _tts_find(resp, "task_id") or _tts_find(resp, "taskid")
    if not task_id:
        raise StepError(f"豆包 TTS 提交无 task_id: {json.dumps(resp, ensure_ascii=False)[:300]}")
    return task_id


def _real_tts_query(task_id, cache_dir):
    api_key = _require_env("MODEL_SPEECH_API_KEY")
    qresp = None
    status = None
    code = None
    for _ in range(240):
        code, qresp = _tts_post(_TTS_BASE + "/query", {"user": "s1-engine", "task_id": task_id}, api_key,
                                resource_id=tts_resource_id())
        if code == -1:
            break          # 单次查询的网络重试已经用完：不再空转，按 network 失败（任务号留着，续跑接着查）
        status = _tts_find(qresp, "task_status")
        if status in (2, 3):
            break
        stopflag.sleep(5)          # P31b：任务号已登记，停下后续跑接着查
    if status != 2:
        # 轮询耗尽或一直连不上 → network；服务端报失败（status 3 等）→ service_error
        kind = (errors.KIND_SERVICE if status == 3
                else errors.KIND_NETWORK if status is None or code == -1
                else errors.http_kind(code) or errors.KIND_NETWORK)
        raise StepError(f"豆包 TTS 失败: {json.dumps(qresp, ensure_ascii=False)[:300]}", kind=kind)
    sentences = _tts_find(qresp, "sentences") or []
    audio_url = _tts_find(qresp, "audio_url")
    audio_seconds = sentences[-1].get("endTime") if sentences else 0.0
    return {"sentences": sentences, "audio_seconds": audio_seconds, "audio_url": audio_url}


# ST3-F1 第4项：配音开头留白秒数（首音可能被播放器吃掉，故统一垫 0.3s 静音）。
_NARRATION_LEAD_IN = 0.3

# ST3-F1 第2项：定稿锁文件与锁定的产物清单（cli 也读这份清单做校验）。
_NARRATION_LOCK_REL = "01-口播/定稿锁.json"
_NARRATION_LOCK_FILES = [
    "01-口播/narration.txt",
    "02-分组/groups_raw.json",
    "03-配音/narration_tts.txt",
    "03-配音/audio/narration.wav",
    "03-配音/sentences.json",
]


def _prepend_silence(wav_bytes, seconds):
    """在 WAV 开头加 `seconds` 秒静音（PCM 原样拼接，不改采样参数）。"""
    import io
    src = wave.open(io.BytesIO(wav_bytes), "rb")
    params = src.getparams()
    frames = src.readframes(src.getnframes())
    src.close()
    n_sil = int(round(seconds * params.framerate))
    silence = b"\x00" * (n_sil * params.sampwidth * params.nchannels)
    buf = io.BytesIO()
    out = wave.open(buf, "wb")
    out.setparams(params)
    out.writeframes(silence + frames)
    out.close()
    return buf.getvalue()


def _shift_sentences(sentences, delta):
    """所有句子/词的时间戳整体加 `delta` 秒（开头留白用）。"""
    delta = float(delta)
    for s in sentences:
        s["startTime"] = round(float(s.get("startTime", 0)) + delta, 3)
        s["endTime"] = round(float(s.get("endTime", 0)) + delta, 3)
        for w in s.get("words") or []:
            w["startTime"] = round(float(w.get("startTime", 0)) + delta, 3)
            w["endTime"] = round(float(w.get("endTime", 0)) + delta, 3)
    return sentences


def _write_narration_lock(ctx):
    """S1/S2/S3 全部成功后落定稿锁：记录口播相关产物的 sha256。任一文件缺失则不落锁。"""
    files = {}
    for rel in _NARRATION_LOCK_FILES:
        p = ctx.p(*rel.split("/"))
        if not p.is_file():
            ctx.log(f"[S3] 定稿锁未落：缺少 {rel}")
            return
        files[rel] = ledger.sha256_file(p)
    lock = {"schema_version": 1, "files": files, "locked_at": ledger.now_iso()}
    # ST3-P22（G8）：同时记下原文的 sha256，原文变了（飞书 --refetch / 本地 md 同步）开跑时能认出来
    art = ctx.p("00-输入", "文章.md")
    if art.is_file():
        lock["source_md_sha256"] = ledger.sha256_file(art)
    (ctx.p("01-口播") / "定稿锁.json").write_text(
        json.dumps(lock, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.log("[S3] 已落定稿锁 -> 01-口播/定稿锁.json")


def step_s3(ctx):
    """配音：统一付费入口 + **异步任务号先落账本**（A5 第 3 条 / §一.4）。"""
    narr_p = ctx.p("01-口播", "narration.txt")
    if not narr_p.exists():
        raise StepError(f"缺少 S1 产物: {narr_p}")
    tts_text = _tts_text(narr_p.read_text(encoding="utf-8"))
    if not tts_text:
        raise StepError("S3 待配音文本为空")

    cache_dir = _cache_dir(ctx, "S3")
    key = paid.cache_key(_SPEECH_MODEL, tts_text,
                         {"service": "doubao-speech", "speaker": tts_speaker(),
                          "speech_rate": _TTS_SPEECH_RATE, "sample_rate": 24000, "format": "wav"})

    doc = _ledger_doc(ctx)
    submitted = {"n": 0}

    def _run():
        # ① 先查已登记的任务号；查到就接着收，不重复提交（省一次费用）。
        job = ledger.find_job(doc, "S3", "doubao_speech")
        if job:
            task_id = job["task_id"]
            ctx.log(f"[S3] 续查已登记任务 {task_id}")
        else:
            submit = _stub_tts_submit if ctx.stub else _real_tts_submit
            task_id = retry_submit(ctx, submit, tts_text, cache_dir)
            submitted["n"] += 1
            _progress(ctx, "S3", 0, 1, "段", "已提交配音任务，等待合成")
            # ② 拿到任务号**立即**写进账本，然后才轮询。
            ledger.add_job(doc, "S3", "doubao_speech", task_id,
                           note=f"文本 {len(tts_text)} 字")
            ledger.save_atomic(ctx.root, ledger.touch(doc))
        query = _stub_tts_query if ctx.stub else _real_tts_query
        return query(task_id, cache_dir)

    # ST3-P23 §3.4：按提交文本的非空白字数计价；没有句子时间戳的结果算失败（不写缓存）。
    n_chars = len(re.sub(r"\s", "", tts_text))
    result = paid.perform(ctx, step="S3", service="doubao_speech", cache_dir=cache_dir,
                          key=key, describe=f"配音（{len(tts_text)} 字）", quantity=float(n_chars),
                          invoke_real=_run, invoke_stub=_run,
                          valid=lambda r: isinstance(r, dict) and bool(r.get("sentences")))
    if not isinstance(result, dict) or not result.get("sentences"):
        raise StepError("豆包 TTS 返回的结果里没有句子时间戳（未写缓存）", kind=errors.KIND_SERVICE,
                        service=errors.SERVICE_SPEECH)

    sentences = result.get("sentences") or []
    audio_seconds = result.get("audio_seconds") or 0.0
    name = "narration.wav"
    out_dir = ctx.p("03-配音")
    (out_dir / "audio").mkdir(parents=True, exist_ok=True)

    # ST3-F1 第1项：音频字节单独进缓存（audio_url 一小时过期，不能只存链接）。
    audio_key = key + "-wav"
    hit, audio_bytes, _ = paid.load_cached(cache_dir, audio_key)
    if not hit:
        if result.get("audio_url"):
            import urllib.request
            try:
                audio_bytes = netretry.call(
                    lambda: urllib.request.urlopen(result["audio_url"], timeout=120).read(),
                    service="豆包配音", what="下载音频")
            except Exception as e:
                errors.tag_service(e, errors.SERVICE_SPEECH)
                raise
        else:
            audio_bytes = stubs.silence_wav_bytes(audio_seconds)
        if not audio_bytes:
            raise StepError("豆包 TTS 音频下载为空（未写缓存）", kind=errors.KIND_SERVICE,
                            service=errors.SERVICE_SPEECH)
        paid.store_cached(cache_dir, audio_key, audio_bytes, step="S3", service="doubao_speech")
    _progress(ctx, "S3", 1, 1, "段")

    # ST3-F1 第4项：开头加 0.3 秒静音，时间戳同步 +0.3（落锁前完成，锁记的是处理后文件）。
    audio_bytes = _prepend_silence(audio_bytes, _NARRATION_LEAD_IN)
    sentences = _shift_sentences(sentences, _NARRATION_LEAD_IN)

    (out_dir / "audio" / name).write_bytes(audio_bytes)
    (out_dir / "sentences.json").write_text(
        json.dumps(sentences, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "narration_tts.txt").write_text(tts_text + "\n", encoding="utf-8")
    (out_dir / "query_response.json").write_text(
        json.dumps({"http": 200, "resp": {"code": 20000000,
                                          "data": {"sentences": sentences,
                                                   "req_text_length": len(tts_text)}}},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    # 收到结果 → 清掉已登记的任务号
    for task_id in [j["task_id"] for j in (doc.get("pending_jobs") or [])
                    if j.get("step") == "S3"]:
        ledger.remove_job(doc, task_id)
    ledger.save_atomic(ctx.root, ledger.touch(doc))

    # ST3-F1 第2项：S1/S2/S3 全部成功后落定稿锁。
    _write_narration_lock(ctx)

    st = ctx.paid_stats
    total_s = sentences[-1].get("endTime") if sentences else (audio_seconds + _NARRATION_LEAD_IN)
    ctx.log(f"[S3] 句 {len(sentences)} / 音频 {total_s:.1f}s / 调用 {st['calls']}"
            f" / 命中 {st['hits']} / 桩 {st['stub_calls']} -> {out_dir / 'audio' / name}")
    return {"jobs": doc.get("pending_jobs") or [],
            "source": ("桩（静音 WAV + 字级时间戳）" if st["stub_calls"] else None)}


def retry_submit(ctx, submit, text, cache_dir):
    """提交（真实/桩）——单独一个函数，便于演练脚本注入"提交后中断"。"""
    return submit(text, cache_dir)


def step_s8(ctx):
    if ctx.resolved_content_type == spec.CONTENT_TYPE_SCREENSHOT:
        raise StepError("禁止付费：截图型 S8 走导入，缺少 07-出图/manifest.json 或 images/")
    _refuse_paid(ctx, "S8")


# --------------------------------------------------------------------------
# S0 内容类型判断（ST1-P2b 新增；排在所有步骤之前）
# --------------------------------------------------------------------------

# ST1-P3：截图型开关打开。截图线按 SHOT_TAIL 的步骤表执行（S8 在 S7 之前）。
SCREENSHOT_SUPPORTED = True

# 来源.json 的「内容类型」写法（中英都认）
TYPE_ALIASES = {"理论型": spec.CONTENT_TYPE_THEORY, "theory": spec.CONTENT_TYPE_THEORY,
                "截图型": spec.CONTENT_TYPE_SCREENSHOT, "screenshot": spec.CONTENT_TYPE_SCREENSHOT}
# 图片分类里表示「真实截图」的取值（B3 定 `截图`/`非截图`，兼容 `真实截图`）
SCREENSHOT_LABELS = ("截图", "真实截图")


def _write_type_judgment(out, ct, basis, **extra):
    doc = {"schema_version": 1, "content_type": ct, "依据": basis,
           "decided": ledger.now_iso()}
    doc.update(extra)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def _article_images(ctx):
    """文章里的图片清单（机械抽取，与 S5 同一套 image_map 逻辑），按 sha256 去重。

    返回 [{name, rel, sha256}]；只收**确实在工作文件夹里**的图。
    """
    from pipeline import image_map
    art = ctx.p("00-输入", "文章.md")
    payload = image_map.extract_images(art.read_text(encoding="utf-8"), str(art))
    out, seen = [], set()
    for im in payload["images"]:
        rel, p = intake.input_image_path(ctx.p("00-输入"), im)
        if not p.is_file():
            continue
        sha = ledger.sha256_file(p)
        if sha in seen:
            continue
        seen.add(sha)
        out.append({"name": Path(rel).name, "rel": "00-输入/" + rel, "sha256": sha})
    return out


def s0_needs_vision(root):
    """S0 这一轮会不会真的调方舟识图（ST3-P22 G15，开跑前查密钥用；只读）。

    要调 = 来源.json 没指定内容类型 + 文章里有图 + 至少一张图的分类既不在 `图片分类.json`、
    也不在 `00-输入/cache`（识图缓存，键与 `_classify_images` 一致）。
    """
    from pipeline import image_map
    inp = Path(root) / "00-输入"
    try:
        d = json.loads((inp / "来源.json").read_text(encoding="utf-8"))
    except Exception:
        d = {}
    if TYPE_ALIASES.get(d.get("内容类型") or d.get("content_type")):
        return False
    art = inp / "文章.md"
    if not art.is_file():
        return False
    try:
        items = json.loads((inp / "图片分类.json").read_text(encoding="utf-8")).get("items") or {}
    except Exception:
        items = {}
    payload = image_map.extract_images(art.read_text(encoding="utf-8"), str(art))
    cache_dir = Path(root).joinpath(*_CACHE_DIR["S0"])
    for im in payload["images"]:
        _, p = intake.input_image_path(inp, im)
        if not p.is_file():
            continue
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() in items:
            continue
        key = paid.cache_key(_ARK_MODEL, _CLS_PROMPT, {"max_tokens": 512},
                             extra=hashlib.sha256(data).hexdigest())
        if not paid.cache_paths(cache_dir, key)[0].is_file():
            return True
    return False


def step_s0(ctx):
    """判断内容类型，产物 `00-输入\类型判断.json`。

    顺序（任务书 B2）：`00-输入\来源.json` 的人工指定 → 图片分类（≥1 张截图 = 截图型）。
    缺分类时本轮不发识图调用，直接失败（`--no-paid` 与不带的 message 不同）。
    截图型在 `SCREENSHOT_SUPPORTED` 打开之前一律拒绝。
    """
    out_dir = ctx.p("00-输入")
    out = out_dir / "类型判断.json"
    art = out_dir / "文章.md"
    if not art.exists():
        raise StepError(f"缺少输入文章: {art}")

    src_p = out_dir / "来源.json"
    cls_p = out_dir / "图片分类.json"

    specified = None
    if src_p.exists():
        try:
            d = json.loads(src_p.read_text(encoding="utf-8"))
        except Exception as e:
            raise StepError(f"来源.json 解析失败: {e}")
        raw = d.get("内容类型") or d.get("content_type")
        if raw:
            specified = TYPE_ALIASES.get(raw)
            if specified is None:
                raise StepError(f"来源.json 的内容类型无法识别: {raw!r}")

    if specified:
        ct, basis, extra = specified, "人工指定", {"来源": "00-输入/来源.json"}
    else:
        imgs = _article_images(ctx)
        items = {}
        if cls_p.exists():
            try:
                items = json.loads(cls_p.read_text(encoding="utf-8")).get("items") or {}
            except Exception as e:
                raise StepError(f"图片分类.json 解析失败: {e}")
        missing = [im for im in imgs if im["sha256"] not in items]
        if missing:
            # 缺分类 → 走统一付费入口逐张识图（命中 `00-输入/cache` 就不发请求）。
            # `--no-paid` / 超出上限 / 桩模式的分支都在 paid.perform 里，message 前缀统一。
            try:
                items = _classify_images(ctx, missing, items)
            except PaidBlocked as e:
                _write_type_judgment(out, None, "缺少分类",
                                     image_total=len(imgs), image_missing=len(missing))
                raise StepError(str(e), kind=e.kind)
            cls_p.write_text(json.dumps({"schema_version": 1, "items": items},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
        shots = [im for im in imgs if items[im["sha256"]].get("分类") in SCREENSHOT_LABELS]
        ct = spec.CONTENT_TYPE_SCREENSHOT if shots else spec.CONTENT_TYPE_THEORY
        basis = "图片分类"
        extra = {"image_total": len(imgs), "image_screenshot": len(shots)}

    ctx.resolved_content_type = ct
    _write_type_judgment(out, ct, basis, **extra)
    ctx.log(f"[S0] 内容类型 {ct}（依据：{basis}） -> {out}")

    if ct == spec.CONTENT_TYPE_SCREENSHOT and not SCREENSHOT_SUPPORTED:
        raise StepError("当前版本暂不支持截图型")


def _asset_subdir(ctx):
    """截图型本篇素材子目录——**由引擎自己定**（= 文章文件夹名）。

    同一子目录同时用于 AI 插画（`<子目录>/<文件名>`）与截图素材
    （`shots/<子目录>/<文件名>`）；`08-场景/shotImageMap.json` 按它生成。
    不读、也不依赖主仓共享 `public\\shots\\p12_239`。
    """
    return ctx.article


# --------------------------------------------------------------------------
# S2 分组（真跑：S1-R4 原型 splitter + annotate；R4b 序数连续组）
# --------------------------------------------------------------------------

_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
       "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cnum(s):
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        p = s.split("十")
        a = _CN[p[0]] if p[0] else 1
        b = _CN[p[1]] if len(p) > 1 and p[1] else 0
        return a * 10 + b
    return sum(_CN.get(ch, 0) for ch in s) if all(ch in _CN for ch in s) else None


def _ordinal_start_val(A, text):
    t = text.strip()
    if not A.RE_ORDINAL_START.match(t):
        return None
    m2 = re.match(r"第([一二三四五六七八九十\d]+)", t)
    if m2:
        return _cnum(m2.group(1))
    m3 = re.match(r"([一二三四五六七八九十]+)是", t)
    if m3:
        return _cnum(m3.group(1))
    return None


def _eff(role):
    return "explanation" if role == "evidence" else role


def step_s2(ctx):
    paths.ensure_prototype_on_path()
    from splitter import split_narration
    import annotate as A

    narration_path = ctx.p("01-口播", "narration.txt")
    if not narration_path.exists():
        raise StepError(f"缺少 S1 产物: {narration_path}")
    out_dir = ctx.p("02-分组")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "groups_raw.json"

    narration = narration_path.read_text(encoding="utf-8").strip()
    segments = split_narration(narration)
    groups = A.group_segments(segments)
    seg_by_id = {s["segment_id"]: s for s in segments}

    groups_out = []
    for g in groups:
        text = A.group_text(segments, g)
        cls = A.classify_group(text)
        segs = [seg_by_id[sid] for sid in g["segment_ids"]]
        grouptext = "".join(s["source_text"] for s in segs)
        groups_out.append({
            "group_id": g["gid"],
            "paragraph_index": g["paragraph_index"],
            "segment_ids": g["segment_ids"],
            "segment_count": len(g["segment_ids"]),
            "semantic_role": cls["semantic_role"],
            "eff_role": _eff(cls["semantic_role"]),
            "rule": cls["rule"],
            "confidence": cls["confidence"],
            "ordinal_count": cls.get("ordinal_count"),
            "starts_with_bracket_title": bool(grouptext.strip().startswith("【")),
            "group_text": text,
        })

    # R4b: 连续序数递增组 -> principle
    run_id = 0
    i = 0
    n = len(groups_out)
    while i < n:
        if groups_out[i]["starts_with_bracket_title"]:
            i += 1
            continue
        v = _ordinal_start_val(A, groups_out[i]["group_text"])
        if v is None:
            i += 1
            continue
        run = [i]
        prev = v
        j = i + 1
        while j < n and not groups_out[j]["starts_with_bracket_title"]:
            vj = _ordinal_start_val(A, groups_out[j]["group_text"])
            if vj is not None and vj > prev:
                run.append(j)
                prev = vj
                j += 1
            else:
                break
        if len(run) >= 2:
            run_id += 1
            for k in run:
                g = groups_out[k]
                g["semantic_role"] = "principle"
                g["eff_role"] = "principle"
                g["rule"] = "R4b_multi_group_ordinal"
                g["confidence"] = 1.0 if len(run) >= 3 else 0.7
                g["ordinal_count"] = len(run)
                g["ordinal_run_id"] = f"run{run_id}"
        i = j

    title_groups = [g["group_id"] for g in groups_out if g["starts_with_bracket_title"]]
    body_count = len(groups_out) - len(title_groups)
    role_dist = Counter(g["eff_role"] for g in groups_out if not g["starts_with_bracket_title"])

    title_follow_check = []
    for idx, g in enumerate(groups_out):
        if not g["starts_with_bracket_title"]:
            continue
        nxt = groups_out[idx + 1] if idx + 1 < len(groups_out) else None
        if nxt is None:
            ok, kind = False, "无后续组"
        elif nxt["starts_with_bracket_title"]:
            ok, kind = False, "下一组仍是标题(缺过渡语)"
        else:
            ok, kind = True, "正文组"
        title_follow_check.append({
            "title_group": g["group_id"],
            "title_text": g["group_text"],
            "next_group": nxt["group_id"] if nxt else None,
            "next_text_head": (nxt["group_text"][:40] if nxt else None),
            "followed_by_body": ok,
            "note": kind,
        })

    stats = {
        "narration_chars": len(narration),
        "total_groups": len(groups_out),
        "body_groups": body_count,
        "title_groups": title_groups,
        "title_count": len(title_groups),
        "eff_role_dist_body": dict(role_dist),
        "ordinal_runs_detected": run_id,
        "title_follow_check": title_follow_check,
        "all_titles_followed_by_body": all(c["followed_by_body"] for c in title_follow_check),
    }
    payload = {"stats": stats, "narration_path": str(narration_path), "groups": groups_out}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    ctx.log(f"[S2] 总段数 {len(groups_out)} / 正文 {body_count} / 标题 {len(title_groups)}"
            f" / 序数 run {run_id} -> {out}")


# --------------------------------------------------------------------------
# S4 对齐（真跑：pipeline.word_align）
# --------------------------------------------------------------------------


def step_s4(ctx):
    """S4 对齐（ST3-P28）：旧方法（word_align + R148）与新方法各算一遍，整篇择一。

    两套结果都落盘：`04-对齐/alignment.json`（组级）+ `04-对齐/char_times.json`（字级）。
    **此后所有步骤一律从磁盘读这两份**（见 `align_global.active_results`），续跑时
    S4 被跳过也不会退回旧方法。判定写 `logs/对齐判定.json`；沿用旧结果时
    alignment.json 与 v5.3 逐字节相同，char_times.json 只是把旧方法的字级结果补出来。
    """
    from pipeline import align_global, scene_build, word_align as wa
    import r148_subtitle_align as salign

    sent_p = ctx.p("03-配音", "sentences.json")
    grp_p = ctx.p("02-分组", "groups_raw.json")
    if not sent_p.exists():
        raise StepError(f"缺少 S3 产物: {sent_p}")
    if not grp_p.exists():
        raise StepError(f"缺少 S2 产物: {grp_p}")
    out_dir = ctx.p("04-对齐")
    out_dir.mkdir(parents=True, exist_ok=True)

    sent = json.loads(sent_p.read_text(encoding="utf-8"))
    data = json.loads(grp_p.read_text(encoding="utf-8"))
    body = [g for g in data["groups"] if not g["starts_with_bracket_title"]]
    groups = [{"group_id": g["group_id"], "text": g["group_text"]} for g in body]

    audios = ledger.expand(ctx.root, [paths.AUDIO_GLOB])
    audio_dur = _read_audio_dur(ctx.root / audios[0]) if audios else None

    # 旧方法（v5.3 原样）。必须先 suspend：否则 word_align/R148 会读到上一轮写在盘上的
    # 对齐结果，把"新方法结果"当成"旧方法"来比。
    align_global.clear_active()
    align_global.suspend_source()
    old_groups = wa.align_groups(sent, groups)
    old_chars = salign.align_groups(sent, groups)
    # 新方法：整篇有序对齐，组级/字级只算一次
    new = align_global.align_all(sent, groups)

    decision = align_global.decide(
        old_groups, old_chars, new["groups"], new["chars"], groups, sent,
        audio_dur, scene_build._split_subtitle)

    if decision["method"] == "old":
        results = old_groups
        final_chars = old_chars
    else:
        results = new["groups"]
        final_chars = new["chars"]

    # 本轮改写前磁盘上的对齐签名。付费的 S6/S7 没有 `alignment_sig` 时靠它判断
    # "这次 S4 有没有换过对齐"：没换就照样跳过，不白花钱。
    sig_before = align_global.alignment_signature(ctx.root)

    (out_dir / "char_times.json").write_text(
        json.dumps(final_chars, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "alignment.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    # 落盘之后恢复来源并清掉进程内覆盖：下游一律读磁盘（内容与刚写的一致）。
    align_global.clear_active()
    align_global.set_source(ctx.root)

    check = align_global.self_check(groups, results, final_chars, sent, audio_dur,
                                    scene_build._split_subtitle)
    decision["final_self_check"] = check
    logs_dir = ctx.root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "对齐判定.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=1), encoding="utf-8")

    ctx.log(f"[S4] 对齐 {len(results)} 段 -> {out_dir / 'alignment.json'}")
    ctx.log(f"[S4] 对齐：{decision['reason']}")
    if not check["passed"]:
        f = check["failures"][0]
        t = f.get("time")
        ttxt = f"{int(t) // 60:02d}:{int(t) % 60:02d}" if isinstance(t, (int, float)) else "?"
        raise StepError(
            f"对齐自查未通过：{f['item']}；第一处 {f.get('group_id') or ''} {ttxt} "
            f"「{(f.get('text') or '')[:40]}」".strip(),
            kind=errors.KIND_INTERNAL)
    return {"alignment_sig_before": sig_before}


# --------------------------------------------------------------------------
# S5 图片定位（真跑：pipeline.image_map + pipeline.backfill）
# --------------------------------------------------------------------------


def step_s5(ctx):
    """图片定位。产物落点按内容类型分：理论型 `06-编排/`，截图型 `05-截图分析/`。"""
    from pipeline import backfill, image_map

    article = ctx.p("00-输入", "文章.md")
    grp_p = ctx.p("02-分组", "groups_raw.json")
    if not article.exists():
        raise StepError(f"缺少输入文章: {article}")
    if not grp_p.exists():
        raise StepError(f"缺少 S2 产物: {grp_p}")
    out = ctx.root / spec.steps_for(ctx.resolved_content_type)["S5"]["out"][0]
    out.parent.mkdir(parents=True, exist_ok=True)

    img_map = image_map.build_image_map(str(article), str(out))
    backfill.build_backfill(str(out), str(grp_p))
    n_ai = _tag_image_kinds(ctx, out)
    still = _fallback_attach_unmapped(ctx, out, grp_p)
    el_still = _attach_elements(ctx, out.parent)
    ctx.log(f"[S5] 图片 {img_map['image_count']} 张（非截图 {n_ai}） -> {out}")
    if el_still:
        ctx.log(f"[S5] 警告：{len(el_still)} 个要素回填+兜底后仍无组号（已列出，不静默丢弃）: "
                f"{', '.join(e['id'] for e in el_still)}")
    if still:
        names = [Path(e.get("source_relative_path") or e["image_path"]).name for e in still]
        ctx.log(f"[S5] 警告：{len(still)} 张图回填+兜底后仍无组号（已列出，不静默丢弃）: "
                f"{', '.join(names)}")
        return {"message": f"图片定位：{img_map['image_count']} 张，非截图 {n_ai}；"
                           f"{len(still)} 张仍无组号（{', '.join(names)}）"}
    return None


def _tag_image_kinds(ctx, out):
    """给 `image_map.json` 每条打 `kind`——A8.7 混合型的**唯一开关**。

    `分类` 落在 `SCREENSHOT_LABELS` 里 → `"screenshot"`，否则 → `"ai"`；**没有分类信息时
    一律 `"screenshot"`**（理论型与旧样本因此与改动前完全一致）。返回 `ai` 的条数。

    注意**从磁盘重读**：`mapped_group_id` 是 `backfill.build_backfill` 写进文件的，
    `build_image_map` 的返回值里还没有它，拿旧字典回写会把回填结果抹掉。
    """
    img_map = json.loads(out.read_text(encoding="utf-8"))
    cls_p = ctx.p("00-输入", "图片分类.json")
    items = {}
    if cls_p.exists():
        try:
            items = json.loads(cls_p.read_text(encoding="utf-8")).get("items") or {}
        except Exception as e:
            raise StepError(f"图片分类.json 解析失败: {e}")
    sha_by_name = {im["name"]: im["sha256"] for im in _article_images(ctx)}
    n_ai = 0
    for e in img_map.get("images", []):
        name = Path(e.get("source_relative_path") or e["image_path"]).name
        label = (items.get(sha_by_name.get(name)) or {}).get("分类")
        e["kind"] = "ai" if (label and label not in SCREENSHOT_LABELS) else "screenshot"
        if e["kind"] == "ai":
            n_ai += 1
    out.write_text(json.dumps(img_map, ensure_ascii=False, indent=1), encoding="utf-8")
    return n_ai


def _fallback_attach_unmapped(ctx, out, grp_p):
    """回填后仍无组号的原图：按位置兜底挂组（ST1-P4b 方案端裁定；ST1-P5a A 增强）。

    兜底顺序（保证"任何一张原图只要文章里有正文组或标题组就能挂组"）：
      1. 其后第一个有口播（正文）的组；
      2. 若没有，其前最近的有口播组；
      3. 若还没有，文章第一个组。
    位置用 `nearest_heading` 定位标题组 `paragraph_index`：先精确匹配（去【】空白），
    再模糊匹配（difflib，容忍口播把「内容」改写「视频」这类措辞差异）。
    兜底后仍无组号的返回其清单，由 step_s5 记进账本 message 与日志，**不静默丢弃**。
    """
    import difflib
    imap = json.loads(out.read_text(encoding="utf-8"))
    groups = json.loads(grp_p.read_text(encoding="utf-8")).get("groups", [])
    unmapped = [e for e in imap.get("images", []) if e.get("mapped_group_id") is None]
    if not unmapped:
        return []
    body = sorted([g for g in groups if not g.get("starts_with_bracket_title")],
                  key=lambda g: g.get("paragraph_index", -1))
    titles = [t for t in groups if t.get("starts_with_bracket_title")]

    def _key(text):
        return re.sub(r"[【】\s]", "", text or "")

    def _locate_title(heading):
        key = _key(heading)
        if not key:
            return None
        for t in titles:
            if _key(t.get("group_text", "")) == key:
                return t.get("paragraph_index")
        best, best_ratio = None, 0.0
        for t in titles:
            ratio = difflib.SequenceMatcher(None, key, _key(t.get("group_text", ""))).ratio()
            if ratio > best_ratio:
                best_ratio, best = ratio, t.get("paragraph_index")
        return best if best_ratio >= 0.5 else None

    still = []
    for e in unmapped:
        pos = _locate_title(e.get("nearest_heading"))
        target = None
        if pos is not None:
            after = [g for g in body if g.get("paragraph_index", -1) > pos]
            before = [g for g in body if g.get("paragraph_index", -1) < pos]
            target = after[0] if after else (before[-1] if before else None)
        if target is None and body:
            target = body[0]   # 兜底：文章第一个正文组
        if target is None and groups:
            target = groups[0]  # 终极兜底：文章第一个组（可能是标题组）
        if target is None:
            still.append(e)
            continue
        e["mapped_group_id"] = target["group_id"]
        e["match_basis"] = "position_fallback"
        e["confidence"] = None
        e["matched_text_head"] = target["group_text"][:40]
    out.write_text(json.dumps(imap, ensure_ascii=False, indent=1), encoding="utf-8")
    return still


def _attach_elements(ctx, out_dir):
    """ST3-P2 A：识别代码块/命令块/表格并挂组（不得丢失），写 `elements.json`。

    挂组规则沿用「原图必须挂组」的兜底顺序（与 `_fallback_attach_unmapped` 一致）：
    其后第一个有口播的组 → 其前最近的组 → 第一个组。要素记录类型/语言标识/原文内容/
    在文中的位置（行号 + 最近标题）。返回兜底后仍无组号的要素清单（不静默丢弃）。
    """
    import difflib
    from pipeline import elements as el_mod

    article = ctx.p("00-输入", "文章.md")
    grp_p = ctx.p("02-分组", "groups_raw.json")
    text = article.read_text(encoding="utf-8")
    els = el_mod.extract_elements(text)
    groups = json.loads(grp_p.read_text(encoding="utf-8")).get("groups", [])
    # ST3-P11 R3：正文里的命令另存一栏（`inline_commands`），不混进 `elements`——
    # 围栏要素的识别、挂组、「含要素的组不出插画」的判断都保持原样。
    inline = _attach_inline_commands(el_mod.extract_inline_commands(text), text, groups,
                                     out_dir / "image_map.json")

    if not els:
        out = out_dir / "elements.json"
        out.write_text(json.dumps({"elements": [], "inline_commands": inline},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        return []

    body = sorted([g for g in groups if not g.get("starts_with_bracket_title")],
                  key=lambda g: g.get("paragraph_index", -1))
    titles = [t for t in groups if t.get("starts_with_bracket_title")]

    def _key(text):
        return re.sub(r"[【】\s]", "", text or "")

    def _locate_title(heading):
        key = _key(heading)
        if not key:
            return None
        for t in titles:
            if _key(t.get("group_text", "")) == key:
                return t.get("paragraph_index")
        best, best_ratio = None, 0.0
        for t in titles:
            ratio = difflib.SequenceMatcher(None, key, _key(t.get("group_text", ""))).ratio()
            if ratio > best_ratio:
                best_ratio, best = ratio, t.get("paragraph_index")
        return best if best_ratio >= 0.5 else None

    still = []
    for e in els:
        pos = _locate_title(e.get("nearest_heading"))
        target = None
        if pos is not None:
            after = [g for g in body if g.get("paragraph_index", -1) > pos]
            before = [g for g in body if g.get("paragraph_index", -1) < pos]
            target = after[0] if after else (before[-1] if before else None)
        if target is None and body:
            target = body[0]   # 兜底：文章第一个正文组
        if target is None and groups:
            target = groups[0]  # 终极兜底：文章第一个组
        if target is None:
            still.append(e)
            continue
        e["mapped_group_id"] = target["group_id"]
        e["match_basis"] = "position_fallback"
    out = out_dir / "elements.json"
    out.write_text(json.dumps({"elements": els, "inline_commands": inline},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    return still


def _attach_inline_commands(cmds, text, groups, image_map_p):
    """ST3-P11 R3：给正文里的命令编号、挂组，并找出它「旁边的截图」。

    旁边有截图 = 原文里这条命令和某张截图之间没有隔着其他图片或小标题。两边都有时取
    原文位置最近的一张，距离相同取后面那张。挂组 = 口播里最像这句原文的那一组；对不上
    就按原文位置，挂到命令所在小节里排在前面的最后一个正文组。
    """
    import difflib
    if not cmds:
        return []
    lines = text.splitlines()
    heading_lines = [i + 1 for i, ln in enumerate(lines) if re.match(r"^#{1,6}\s+\S", ln)]
    images = []
    if Path(image_map_p).exists():
        for e in json.loads(Path(image_map_p).read_text(encoding="utf-8")).get("images", []):
            images.append({"line": e.get("line") or 0,
                           "name": Path(e.get("source_relative_path") or e.get("image_path") or "").name,
                           "screenshot": (e.get("kind") or "screenshot") == "screenshot",
                           "gid": e.get("mapped_group_id")})
    images.sort(key=lambda x: x["line"])
    body = sorted([g for g in groups if not g.get("starts_with_bracket_title")],
                  key=lambda g: g.get("paragraph_index", -1))

    def _n(t):
        return re.sub(r"[^\w]", "", t or "").lower()

    sents = []
    order = {g["group_id"]: i for i, g in enumerate(body)}
    for g in body:
        for part in re.split(r"[。！？!?；;\n]", g.get("group_text", "")):
            if len(_n(part)) >= 4:
                sents.append((g["group_id"], _n(part)))

    def _near(line):
        prev = next((im for im in reversed(images) if im["line"] < line), None)
        nxt = next((im for im in images if im["line"] > line), None)
        if prev and any(prev["line"] < h < line for h in heading_lines):
            prev = None
        if nxt and any(line < h < nxt["line"] for h in heading_lines):
            nxt = None
        cands = [im for im in (prev, nxt) if im and im["screenshot"]]
        if not cands:
            return None
        # 距离相同取后面那张：按（距离, 是否在前面）排
        return min(cands, key=lambda im: (abs(im["line"] - line), im["line"] < line))

    out = []
    last = 0          # 同一句话在口播里出现多次时，按原文顺序往后取（不回头挂到前面那一次）
    for k, c in enumerate(cmds):
        best_gid, best = None, 0.0
        for pool in ([x for x in sents if order[x[0]] >= last], sents):
            for cand in (c["context"], c["sentence"]):
                cn = _n(cand)
                if len(cn) < 4:
                    continue
                for gid, sn in pool:
                    r = difflib.SequenceMatcher(None, cn, sn).ratio()
                    if r > best:
                        best_gid, best = gid, r
                if best >= 0.5:
                    break
            if best >= 0.5:
                break
        if best >= 0.5:
            last = order[best_gid]
        near = _near(c["line"])
        basis = "口播句"
        if best < 0.5:
            best_gid, basis = (near["gid"] if near and near["gid"] else (body[0]["group_id"] if body else None)), \
                              ("旁边的截图" if near and near["gid"] else "position_fallback")
        if best_gid in order:
            last = max(last, order[best_gid])
        out.append(dict(c, id=f"cmd_{k + 1:03d}", mapped_group_id=best_gid, match_basis=basis,
                        near_image=near["name"] if near else None))
    return out


# --------------------------------------------------------------------------
# S6 截图分析（ST1-P3 收编：shot_groups 引擎真跑，其余子项导入）
# --------------------------------------------------------------------------

# (相对路径, 说明)——付费/模型调用的一次性结果，随样本放进工作文件夹，引擎只读不重跑
_SHOT_IMPORT_SUBITEMS = [
    ("05-截图分析/labels.json", "标签←P4"),
    ("05-截图分析/matches_v2.json", "图句匹配←P4"),
    ("05-截图分析/stay_judgments.json", "停留判断明细←P9"),
    ("05-截图分析/stay_endings.json", "停留终点←P12"),
    ("06-编排/ai_out.json", "密度与方向←P2"),
]
_SHOT_IMPORT_CACHE = [
    ("05-截图分析/cache", "停留判断缓存←P9"),
    ("06-编排/cache", "导演链缓存←P7"),
]
_SHOT_SOURCE_NOTE = "子项导入：导演链←P7，密度←P2，标签←P4，停留←P12"


_FPS = 30


def _subtitle_lines(sent):
    lines = []
    for s in sent:
        chars = []
        for w in s.get("words", []):
            fr = round(w.get("startTime", 0) * _FPS)
            for ch in w.get("word", ""):
                chars.append({"c": ch, "f": fr})
        lines.append({"start_frame": round(s.get("startTime", 0) * _FPS),
                      "end_frame": round(s.get("endTime", 0) * _FPS), "chars": chars})
    return lines


def _chapter_of_gid(gr):
    h1 = [(g["paragraph_index"], g["group_text"].strip("【】").split("、")[0])
          for g in gr["groups"] if g.get("starts_with_bracket_title")
          and re.match(r"^【[一二三四]、", g["group_text"])]
    h1.sort()

    def f(gid):
        pidx = next((g["paragraph_index"] for g in gr["groups"] if g["group_id"] == gid), None)
        if pidx is None:
            return None
        chap = None
        for hp, name in h1:
            if hp <= pidx:
                chap = name
        return chap
    return {g["group_id"]: f(g["group_id"]) for g in gr["groups"]}


def _s6_text_call(ctx, cache_dir):
    return paid.make_paid_text_call(
        ctx, step="S6", cache_dir=cache_dir, model=_DEEPSEEK_MODEL,
        describe="导演链/图句匹配/标签", invoke_real=_real_text_call(_DEEPSEEK_MODEL),
        invoke_stub=stubs.stub_text)


def _s6_vision_call(ctx, cache_dir):
    return paid.make_paid_vision_call(
        ctx, step="S6", cache_dir=cache_dir, model=_ARK_MODEL,
        describe="导演链识图", invoke_real=_real_vision_call(_ARK_MODEL),
        invoke_stub=stubs.stub_vision)


def _import_or_raise(ctx, rel, err):
    p = ctx.root / rel
    if not p.is_file():
        raise err
    return json.loads(p.read_text(encoding="utf-8"))


def step_s6(ctx):
    """截图分析：**引擎真跑 + 命中历史缓存**（ST1-P4a2 §一.3），逐项记账。

    四个子项各跑各的，哪个跑不动（缓存没命中且 `--no-paid`）就**退回导入**那一项，
    并把原因逐项写进账本 `source`。**不伪造缓存键**。
    """
    from pipeline import shot_ai, shot_groups as sg, shot_timeline

    art = ctx.p("00-输入", "文章.md")
    im_p = ctx.p("05-截图分析", "image_map.json")
    gr_p = ctx.p("02-分组", "groups_raw.json")
    for p in (art, im_p, gr_p):
        if not p.exists():
            raise StepError(f"缺少上游产物: {p}")

    im = json.loads(im_p.read_text(encoding="utf-8"))
    gr = json.loads(gr_p.read_text(encoding="utf-8"))
    sent = _load_json(ctx, "03-配音/sentences.json")
    # 混合型（A8.7 第 3 条）：截图分析**只覆盖 `kind=screenshot` 的条目**，`kind=ai`
    # 的原图原样透传给 S7（S7 会给这些组按理论型出 AI 插图）。
    im_ss = {**im, "images": [e for e in im["images"]
                              if e.get("kind", "screenshot") == "screenshot"
                              and e.get("mapped_group_id")]}
    groups = sg.gen_shot_groups(str(art), im_ss, gr)
    (ctx.p("05-截图分析") / "shot_groups.json").write_text(
        json.dumps(groups, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.log(f"[S6] 截图分组 {len(groups)} 组（引擎计算；非截图 {len(im['images']) - len(im_ss['images'])} 张已排除）")

    d_cache = _cache_dir(ctx, "S6_DIRECTOR")
    s_cache = _cache_dir(ctx, "S6_STAY")
    text_call = _s6_text_call(ctx, d_cache)
    vision_call = _s6_vision_call(ctx, d_cache)

    notes = {}

    # ---- ① 导演链（含密度、并列）----
    try:
        article_lines = art.read_text(encoding="utf-8").splitlines()
        ai_out = shot_ai.run_director(
            {"groups": groups, "image_map": im_ss, "groups_raw": gr, "sentences": sent,
             "image_dir": str(ctx.p("00-输入", "图片和附件")),
             "article_lines": article_lines},
            text_call, vision_call, cache_dir=str(d_cache))
        notes["ai_out"] = "引擎（缓存回放）"
    except PaidBlocked as e:
        ai_out = _import_or_raise(ctx, "06-编排/ai_out.json", e)
        notes["ai_out"] = f"导入（{e}）"
    _progress(ctx, "S6", 1, 4, "项", "导演链")
    (ctx.p("06-编排") / "ai_out.json").write_text(
        json.dumps(ai_out, ensure_ascii=False, indent=1), encoding="utf-8")

    body = [g for g in gr["groups"] if not g.get("starts_with_bracket_title")]
    g_narr = {g["group_id"]: g["group_text"] for g in body}
    ch_gid = _chapter_of_gid(gr)
    from pipeline.shot_director import fn_of
    img_by_fn = {fn_of(e): e for e in im_ss["images"]}
    # 混合型：导演层 `direction` 覆盖全篇（含 `kind=ai` 原图），但镜头表**只排截图**——
    # 那几张原图在 S7 走理论型 AI 插图。这里把 `direction` 收窄到 `im_ss` 的键上，
    # 免得 `build_shots` 的 `img_by_fn[fn]` 取不到（KeyError）。
    direction_ss = [d for d in ai_out["direction"] if d.get("image_ref") in img_by_fn]
    img_dir = ctx.p("00-输入", "图片和附件")
    image_sizes = {fn: _img_size(img_dir / fn) for fn in img_by_fn}
    subsec_of = {fn: e.get("nearest_heading", "") for fn, e in img_by_fn.items()}

    chap_imgs, chap_narr = {}, {}
    for g in body:
        c = ch_gid.get(g["group_id"])
        if c:
            chap_narr.setdefault(c, []).append(g["group_text"])
    for fn, e in img_by_fn.items():
        c = ch_gid.get(e.get("mapped_group_id"))
        if c:
            chap_imgs.setdefault(c, []).append(fn)
    for c in chap_narr:
        chap_narr[c] = "。".join(chap_narr[c])

    # ---- ② 图句匹配 v2 ----
    try:
        matches_v2 = shot_ai._chain_match_v2(chap_imgs, chap_narr, img_by_fn, subsec_of,
                                             text_call, d_cache)
        notes["matches_v2"] = "引擎（缓存回放）"
    except PaidBlocked as e:
        matches_v2 = _import_or_raise(ctx, "05-截图分析/matches_v2.json", e)
        notes["matches_v2"] = f"导入（{e}）"
    _progress(ctx, "S6", 2, 4, "项", "图句匹配")
    (ctx.p("05-截图分析") / "matches_v2.json").write_text(
        json.dumps(matches_v2, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- ③ 网格标签（先空标签跑一遍 build_shots 拿网格图）----
    from pipeline import shot_director as sd
    subtitle_lines = _subtitle_lines(sent)

    def _build(labels):
        return sd.build_shots({
            "image_map": im_ss, "groups_raw": gr, "direction": direction_ss,
            "sentences": sent, "subtitle_lines": subtitle_lines,
            "bc": ai_out["bc"], "f": ai_out["f"], "parallel_v2": ai_out["parallel_v2"],
            "annotations": ai_out["annotations"], "article_lines": article_lines,
            "image_sizes": image_sizes, "density": ai_out.get("density", {}),
            "matches": {}, "matches_v2": matches_v2, "labels": labels,
            "chapter_of_gid": ch_gid,
        }, lead_in=0, include_placeholders=True)

    try:
        plan0 = _build({})
        grid_fns = sorted({x["image"] for s in plan0["shots"] if s["type"] == "grid"
                           for x in s["grid"]["images"]})
        # ST3-P3c 修复三：单截图且无本组侧栏（items 空）的，也补一个 ≤8 字短标签。
        single_fns = sorted({s["image"] for s in plan0["shots"]
                             if s.get("image") and s["type"] != "grid" and not s.get("items")})
        label_fns = sorted(set(grid_fns) | set(single_fns))
        labels, label_manual = shot_ai._chain_label(label_fns, g_narr, img_by_fn,
                                                    text_call, d_cache)
        notes["labels"] = "引擎（缓存回放）"
    except PaidBlocked as e:
        doc = _import_or_raise(ctx, "05-截图分析/labels.json", e)
        labels, label_manual = doc.get("labels", {}), doc.get("manual", [])
        notes["labels"] = f"导入（{e}）"
    _progress(ctx, "S6", 3, 4, "项", "网格标签")
    (ctx.p("05-截图分析") / "labels.json").write_text(
        json.dumps({"labels": labels, "manual": label_manual}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    # ---- ③b 导演层镜头计划（供 S7 路由钩子读取）----
    # P7 当年 stage_b3 的路由依据是 **stage_b2 落盘的导演层 `build_shots` 输出**
    # （`SHOT-M4-P7-新文章整篇/p7_build.py:87-94` 构 full_inputs，`:104-107` 据此建路由）。
    # 那份输入**不含** density / matches_v2 / labels / chapter_of_gid。引擎此前让 S7 读
    # 自己写的 `06-编排/shots_plan.json`，成了「S7 依赖 S7 产物」的循环——新文章首跑时该
    # 文件还不存在，钩子返回 None，所有非标题组都被判成 ai_default（P4a2 回执 §五.D）。
    # 这里把同一份导演层产出落到与 S7 时间轴产物不同的名字下，路由改读这一份，循环消除。
    director_plan = sd.build_shots({
        "image_map": im_ss, "groups_raw": gr, "direction": direction_ss,
        "sentences": sent, "subtitle_lines": subtitle_lines,
        "bc": ai_out["bc"], "f": ai_out["f"], "parallel_v2": ai_out["parallel_v2"],
        "annotations": ai_out["annotations"], "article_lines": article_lines,
        "image_sizes": image_sizes,
    }, lead_in=0, include_placeholders=True)
    (ctx.p("05-截图分析") / "镜头计划.json").write_text(
        json.dumps(director_plan, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- ④ 停留判断（另一个缓存目录）----
    corrected, _ = shot_timeline.fix_subsection_mapping(im_ss, gr)
    subsec = shot_timeline.subsection_groups(gr)
    g_text = {g["group_id"]: g["group_text"] for g in body}
    stay_call = paid.make_paid_text_call(
        ctx, step="S6", cache_dir=s_cache, model=_DEEPSEEK_MODEL, describe="停留判断",
        invoke_real=_real_text_call(_DEEPSEEK_MODEL), invoke_stub=stubs.stub_text)
    try:
        stay_endings = shot_timeline.judge_stay_endings(im_ss, gr, corrected, subsec,
                                                       g_text, labels, stay_call, s_cache)
        notes["stay_endings"] = "引擎（缓存回放）"
    except PaidBlocked as e:
        stay_endings = _import_or_raise(ctx, "05-截图分析/stay_endings.json", e)
        notes["stay_endings"] = f"导入（{e}）"
    _progress(ctx, "S6", 4, 4, "项", "停留判断")
    (ctx.p("05-截图分析") / "stay_endings.json").write_text(
        json.dumps(stay_endings, ensure_ascii=False, indent=1), encoding="utf-8")
    (ctx.p("05-截图分析") / "stay_judgments.json").write_text(
        json.dumps({"stay_endings": {g: {"stay_end_gid": v.get("stay_end_gid"),
                                         "judgments": v.get("judgments", [])}
                                     for g, v in stay_endings.items()}},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    src = "；".join(f"{k}←{v}" for k, v in notes.items())
    ctx.log(f"[S6] {src}")
    return {"source": src}


# --------------------------------------------------------------------------
# S7 编排（ST1-P4a：两种类型统一"先编排后出图"）
#   理论型：插画决策投影成 `06-编排/出图需求.json`。
#   截图型：出图需求清单 + 最终时间轴 `06-编排/shots_plan.json`（build_timeline 真跑）。
# 插画决策两条路（ST1-P4a2 §一.1）：有 `06-编排/插图决策.json` 就导入（旧付费结果，
# A6「计算 + 子项导入」口径）；没有就**真算**（`orchestrate.decide_records`，4 个依赖
# 经统一付费入口），故 spec 里 S7 记 `cost=True`。截图型的真算还要 `route_hook`——它读
# S6 落盘的导演层产出 `05-截图分析/镜头计划.json`（见 `_s7_route_hook`），与时间轴解耦。
# --------------------------------------------------------------------------


def _load_json(ctx, rel):
    p = ctx.root / rel
    if not p.exists():
        raise StepError(f"缺少上游产物: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _elements_rel(ctx):
    """elements.json 落点（与 image_map.json 同目录：理论型 `06-编排/`，截图型 `05-截图分析/`）。"""
    return ("05-截图分析" if ctx.resolved_content_type == spec.CONTENT_TYPE_SCREENSHOT
            else "06-编排") + "/elements.json"


def _load_elements(ctx):
    """读 S5 产出的要素清单；缺失时返回空列表（老样本没有该文件，要素视为无）。"""
    p = ctx.p(_elements_rel(ctx))
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("elements", [])


def _img_size(p):
    from PIL import Image
    with Image.open(p) as im:
        return im.size


def _load_decisions(ctx):
    """读导入的插画决策（`06-编排/插图决策.json`），返回 `(doc, records)`。"""
    p = ctx.p("06-编排", "插图决策.json")
    if not p.exists():
        raise StepError(f"缺少插画决策（导入件）: {p}")
    doc = json.loads(p.read_text(encoding="utf-8"))
    recs = doc.get("records")
    if not isinstance(recs, list):
        raise StepError(f"插画决策格式不对（缺 records 数组）: {p}")
    return doc, recs


def _req_items(records):
    """把插画决策投影成出图需求项（组号 / 插画类型 / 提示词 / 尺寸参数 / 请求内容哈希）。

    请求内容哈希 = `paid.runninghub_key(提示词)`，只含请求内容（模型名、提示词、参数），
    **不含组号与文件路径**（A5 第 4 条）。
    """
    items = []
    for r in records:
        prompt = r.get("prompt")
        img = r.get("image")
        items.append({
            "组号": r.get("group_id"),
            "插画类型": r.get("path"),
            "提示词": prompt,
            "提示词来源": ("导入（旧付费结果）" if prompt else None),
            "尺寸参数": ({"aspect_ratio": paid.RUNNINGHUB_ASPECT,
                          "resolution": paid.RUNNINGHUB_RESOLUTION} if prompt else None),
            "请求内容哈希": (paid.runninghub_key(prompt) if prompt else None),
            # ST3-P9：决策里的图片路径是 Windows 绝对路径；按 Windows 规则取文件名，
            # 在 Windows 上与 Path(img).name 完全一致，在 Mac 上也能取对。
            "文件名": (PureWindowsPath(img).name if img else None),
            "结果": r.get("result"),
        })
    return items


def _write_requirements(ctx, records, source, decision_file=None):
    """写出 `06-编排/出图需求.json`：S8 只读这份清单。"""
    doc = {"schema_version": 1, "content_type": ctx.resolved_content_type,
           "generated": ledger.now_iso(), "source": source,
           "决策文件": decision_file,
           "records": records, "items": _req_items(records)}
    out = ctx.p("06-编排", "出图需求.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


# --------------------------------------------------------------------------
# S7 决策真算路径（ST1-P4a2 §一.1）：经统一入口调模型，键按"调用内容"算
# --------------------------------------------------------------------------

# S7 决策用的模型：类型判断/主体提炼/标题/导览走 DeepSeek（同 ctsc.match 与 gen.*）
_S7_MODEL = _DEEPSEEK_MODEL


# S7 决策 dep 的源码文件（缓存键纳入其 sha256，提示词/实现变化即失效）。
# ST1-P5b 乙：路径改为 vendor 内部，且 sha256 只按文件内容算（不含路径）。
_S7_DEP_SOURCES = {
    "match": [paths.VENDOR_DIR / "wz-sp-ctsc" / "scripts" / "type_match.py",
              paths.VENDOR_DIR / "wz-sp-ctsc" / "references" / "type-manifest.json"],
    "extract_subject": [paths.VENDOR_DIR / "wz-sp-ctsc" / "scripts" / "generate.py"],
    "extract_title": [paths.ENGINE_ROOT / "pipeline" / "orchestrate.py"],
    "extract_overview": [paths.VENDOR_DIR / "wz-sp-ctsc" / "scripts" / "generate.py"],
}
_s7_dep_src_cache = {}
_S7_DEP_PROMPT_EST = 4000      # 决策函数内部提示词模板的字符数（保守估计，费用预估用）


def newline_variants(data):
    """R101 N：一份源文件的几种换行写法：[原样, 全 LF, 全 CRLF]（去重，原样在前）。

    缓存键里含源文件的 sha256（S1 模板、S7 决策源码）。同一个仓库在 Windows（CRLF）和 Mac（LF）上检出的字节不同，
    1.0.0 的安装包与 CLN1W 重打的包之间这些文件也只差换行符。查缓存时几种写法都试，换行符不再让缓存失效。
    """
    lf = data.replace(b"\r\n", b"\n")
    out = []
    for v in (data, lf, lf.replace(b"\n", b"\r\n")):
        if v not in out:
            out.append(v)
    return out


def _dep_src_hashes(name):
    """S7 决策 dep 的实现源码指纹（各文件内容的 sha256），模块级缓存一次。

    ST1-P5b 乙：只按文件内容算、不含路径——vendor 副本与原文件逐字节相同，
    搬家后缓存键不变、旧缓存继续命中。
    R101 N：返回 [按原样字节算的指纹, 全部 LF 时, 全部 CRLF 时]（去重）；第一个就是 1.0.0 的算法。
    """
    if name not in _s7_dep_src_cache:
        datas = [Path(f).read_bytes() for f in _S7_DEP_SOURCES.get(name, [])]
        out = []
        for mode in range(3):
            h = hashlib.sha256()
            for d in datas:
                v = d if mode == 0 else (d.replace(b"\r\n", b"\n") if mode == 1
                                        else d.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
                h.update(hashlib.sha256(v).hexdigest().encode("utf-8"))
            if h.hexdigest() not in out:
                out.append(h.hexdigest())
        _s7_dep_src_cache[name] = out
    return _s7_dep_src_cache[name]


def _dep_src_hash(name):
    """按原样字节算的指纹（与 1.0.0 相同）。"""
    return _dep_src_hashes(name)[0]


def _paid_dep(ctx, cache_dir, name, real, stub, model=_S7_MODEL, service="deepseek"):
    """把一个决策函数包成"经统一付费入口"的可调用对象。

    缓存键 = 规范化调用参数 + dep 名 + 实现源码文件 sha256（`_dep_src_hash`）——
    提示词写在函数内部、引擎拿不到原文，故用「源码文件指纹」兜底：源码/提示词一变，
    键就变、缓存即失效。
    """
    srcs = _dep_src_hashes(name)

    def call(*args, **kwargs):
        canon = json.dumps({"fn": name, "args": list(args), "kwargs": kwargs},
                           ensure_ascii=False, sort_keys=True)
        keys = [paid.cache_key(model, canon, {"dep": name, "src": s}) for s in srcs]
        key = keys[0]
        # 提示词在决策函数内部、引擎拿不到：按调用参数字符数 + 提示词模板约 4000 字、输出 4096 预估。
        return paid.perform(
            ctx, step="S7", service=service, cache_dir=cache_dir, key=key,
            describe=f"决策 {name}", quantity=0.0,
            prompt_chars=len(canon) + _S7_DEP_PROMPT_EST, max_tokens=4096, alt_keys=keys[1:],
            invoke_real=lambda: _norm(real(*args, **kwargs)),
            invoke_stub=(None if stub is None else lambda: _norm(stub(*args, **kwargs))))
    return call


def _norm(v):
    """元组转列表——缓存只存 JSON，元组来回一趟会退化成首元素。"""
    return list(v) if isinstance(v, tuple) else v


def _s7_deps(ctx):
    """S7 的四个决策函数，全部经统一付费入口（缓存键见 `_paid_dep`）。"""
    from pipeline import orchestrate
    cache_dir = _cache_dir(ctx, "S7")
    real_deps = orchestrate._default_deps()
    return {
        "match": _paid_dep(ctx, cache_dir, "match", real_deps["match"], stubs.stub_match),
        "extract_subject": _paid_dep(ctx, cache_dir, "extract_subject",
                                     real_deps["extract_subject"], stubs.stub_extract_subject),
        "extract_title": _paid_dep(ctx, cache_dir, "extract_title",
                                   real_deps["extract_title"], stubs.stub_extract_title),
        "extract_overview": _paid_dep(ctx, cache_dir, "extract_overview",
                                      real_deps["extract_overview"], stubs.stub_extract_overview),
    }


def _load_inline_commands(ctx):
    """读 S5 识别出的正文命令（ST3-P11 R3）；老样本没有这一栏，视为无。"""
    p = ctx.p(_elements_rel(ctx))
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("inline_commands", [])


def _inline_card_elements(cmds):
    """把要出卡片的正文命令写成要素（命令行样式卡片），定锚用它所在的那句原文。"""
    return [{"id": c["id"], "type": "command", "lang": "", "lines": [c["text"]], "raw": c["raw"],
             "line": c["line"], "nearest_heading": None, "mapped_group_id": c.get("mapped_group_id"),
             "match_basis": c.get("match_basis"), "origin": "inline",
             "anchor_texts": [t for t in (c.get("context"), c.get("sentence")) if t]}
            for c in cmds]


def _sentence_index(ctx, groups, sentences):
    """口播句子索引（每句的开始帧、所属组），S7 的取材范围与 S9 的检查共用。"""
    from pipeline import allocate, scene_build  # noqa: F401（scene_build 负责把 R148 加进搜索路径）
    import r148_subtitle_align as salign
    body = [g for g in groups if not g.get("starts_with_bracket_title")]
    ca = {r["group_id"]: r for r in salign.align_groups(
        sentences, [{"group_id": g["group_id"], "text": g["group_text"]} for g in body])}
    return allocate.sentence_index(
        [(g["group_id"], (ca.get(g["group_id"]) or {}).get("char_times") or []) for g in body], _FPS)


_FILL_PATH = "标注空档插画"       # 同 scene_build.FILL_PATH（ST3-P12b 起不再生成，只用来清掉 P12 留下的旧需求）


class _NoReply(Exception):
    """模型这一次没有返回内容（网络错误、HTTP 非 200）。不写进缓存，按这一次调用失败算。"""


def _runtime_font(ctx):
    """渲染端左栏用的字体文件（若运行时目录提供）。没有时量宽改用系统字体。"""
    fp = Path(ctx.runtime_node_modules).parent / paths.FONT_REL
    return fp if fp.is_file() else None


def _shot_windows(data):
    from pipeline import shot_annotate
    return shot_annotate.shot_windows(data)


def _annotate_shots(ctx, im, groups, sentences, article, windows, boxed, counter, labels=None):
    """ST3-P12 R1：给每张截图生成左侧标注（一行标题 + 逐句条目）。

    时机：S7 把场景试排一遍之后——这时每张图的实际在屏时间已经定了（`windows`）。
    输入：这张图在屏期间的口播句（带序号和开始时间）、图前后的原文段落、所在小节标题、本图命令框的内容。
    模型调用走统一付费入口（DeepSeek 文本）；失败或校验两次不过走规则兜底（见 shot_annotate）。
    返回 ({文件名: 标注}, 生成记录)。
    """
    from pipeline import shot_annotate, shot_timeline
    real = _real_text_call(_S7_MODEL)

    def _real(prompt, max_tokens=4096):
        counter["requests"] = counter.get("requests", 0) + 1
        out = real(prompt, max_tokens=max_tokens)
        if out is None:
            raise _NoReply("DeepSeek 没有返回内容")
        return out

    text_call = paid.make_paid_text_call(
        ctx, step="S7", cache_dir=_cache_dir(ctx, "S7"), model=_S7_MODEL, describe="逐句标注",
        invoke_real=_real, invoke_stub=lambda prompt, max_tokens=4096: "")
    return shot_annotate.annotate_all(im, groups, sentences, article, windows, boxed, text_call,
                                      soft_errors=(_NoReply,), labels=shot_timeline.image_titles(im, labels),
                                      hints=shot_timeline.image_hints(im, labels),
                                      progress=lambda d, t: _progress(ctx, "S7", d, t, "张", "截图标注"))


def _rescope_illustrations(ctx, records, shots, sidx, deps=None, shown=None):
    """ST3-P11 R5：一组口播里有截图锚点时，这组插画只用第一个截图锚点句之前的口播来写出图需求。

    插画只在截图到来之前上屏，按整组取材会把后面才讲到的内容画进去。取材范围变了的记录
    换一个文件名（`-pre`），旧图不会被当成新需求的结果沿用。返回改写的条数。
    `shown`：试排里真正上屏的插画所属组；给了就只改这些组（不上屏的插画不值得重新出图）。
    """
    from pipeline import allocate, orchestrate
    sents = sidx["sentences"]
    first = {}
    for sh in shots:
        if sh.get("type") == "grid" or not sents:
            continue
        si = allocate._sentence_at(sidx, (sh.get("alloc") or {}).get("anchor", sh["timing"]["start_frame"]))
        gid = sents[si]["gid"]
        first[gid] = min(first.get(gid, si), si)
    n = 0
    for r in records:
        gid = r.get("group_id")
        if gid not in first or not r.get("prompt") or not r.get("ill_type") or r.get("subject") is None:
            continue
        if shown is not None and gid not in shown:
            continue
        own = [x["i"] for x in sents if x["gid"] == gid]
        raw = allocate.group_sentences(r.get("text") or "")
        k = sum(1 for i in own if i < first[gid])
        if len(raw) != len(own) or k == 0 or k >= len(raw):
            continue      # 第一句就是截图锚点（插画本来不上屏），或句子对不齐：保持原样
        scope = "".join(raw[:k]).strip()
        name = r["path"].split(":", 1)[1] if str(r.get("path", "")).startswith("Type:") else "兜底"
        deps = deps or _s7_deps(ctx)
        subject, labels = orchestrate.ds_retry(deps["extract_subject"], scope, name)
        r["text_scope"] = scope
        if subject is None:
            r["path"], r["result"] = "不出图(弃权)", None
            r.pop("prompt", None), r.pop("image", None)
        else:
            r["subject"], r["labels"] = subject, labels
            r["prompt"] = orchestrate.gen.build_prompt(r["ill_type"], subject, labels)
            r["image"] = str(ctx.p("07-出图", "images") / f"{gid}-{r['ill_type']}-pre.png")
            r["result"] = "待出图"
        n += 1
        ctx.log(f"[S7] 插画取材范围 {gid}：只用第一个截图锚点句之前的 {k}/{len(raw)} 句")
    return n


def _trial_scenes(ctx, records, shots, plan):
    """S7 里把场景试排一遍（纯计算，同 S9）：用当前的出图需求、镜头计划、句子时间戳。

    缺对齐或配音产物时返回 None。R5 用它判断哪些插画真的上屏，R6 用它估算标题卡后面的空档，
    ST3-P12 R1 用它定每张截图的实际在屏时间。
    """
    from pipeline import orchestrate, scene_build
    align_p, sent_p = ctx.p("04-对齐", "alignment.json"), ctx.p("03-配音", "sentences.json")
    audios = ledger.expand(ctx.root, [paths.AUDIO_GLOB])
    if not (align_p.exists() and sent_p.exists() and audios):
        ctx.log("[S7] 缺对齐或配音产物，无法试排场景：插画取材范围与空档插画跳过")
        return None
    screenshot = ctx.resolved_content_type == spec.CONTENT_TYPE_SCREENSHOT
    groups, _t, _b = orchestrate.load_groups(ctx.p("02-分组"))
    kw = {}
    if screenshot:
        kw = {"shots": shots, "asset_subdir": _asset_subdir(ctx),
              "stay_covered": set(plan.get("stay_covered") or []),
              "partial_recover": plan.get("partial_recover") or {}}
        cards = plan.get("inline_cards") or []
    else:
        cards = _load_inline_commands(ctx)
    im_p = ctx.p("05-截图分析" if screenshot else "06-编排", "image_map.json")
    art_p = ctx.p("00-输入", "文章.md")
    return scene_build.build_scenes(
        [r for r in records if r.get("path") not in ("空档插画", _FILL_PATH)],
        {r["group_id"]: r for r in json.loads(align_p.read_text(encoding="utf-8"))},
        json.loads(sent_p.read_text(encoding="utf-8")), groups,
        audio_dur=_read_audio_dur(ctx.root / audios[0]), audio_name=Path(audios[0]).name,
        elements=_load_elements(ctx) + _inline_card_elements(cards),
        image_map=json.loads(im_p.read_text(encoding="utf-8")) if im_p.exists() else None,
        article_text=art_p.read_text(encoding="utf-8") if art_p.exists() else None,
        strict=False, **kw)


def _gap_requirements(ctx, records, data):
    """ST3-P11 R6：「画面最多提前 2 秒」之后标题卡后面空出来的时间，超过 6 秒的各生成一条空档插画需求。

    空档取自试排（`_trial_scenes`）里分配器登记的「标题卡延长」；插画取材于空档期间的口播。返回新增条数。
    """
    from pipeline import orchestrate
    records[:] = [r for r in records if r.get("path") != "空档插画"]
    if data is None:
        return 0
    deps, n = None, 0
    for note in data.get("allocation_notes", []):
        if note.get("type") != "标题卡延长" or not note.get("long"):
            continue
        tgid = note["id"].split(":", 1)[1]
        lo, hi = note["gap_from"], note["gap_to"]
        text = "".join("".join(c["c"] for c in ln["chars"]) for ln in data["subtitle_lines"]
                       if lo <= ln["start_frame"] < hi).strip()
        if not text:
            continue
        deps = deps or _s7_deps(ctx)
        subject, labels = orchestrate.ds_retry(deps["extract_subject"], text, "兜底")
        if subject is None:
            ctx.log(f"[S7] 空档插画 {tgid}：画面提炼弃权，标题卡延长")
            continue
        records.append({
            "group_id": f"{tgid}-gap", "text": text, "starts_with_bracket_title": False,
            "path": "空档插画", "gap_for": tgid, "gap": {"from_frame": lo, "to_frame": hi},
            "ill_type": "generic", "subject": subject, "labels": labels,
            "transition": "cut", "transition_kind": "inbetween",
            "prompt": orchestrate.gen.build_prompt("generic", subject, labels),
            "result": "待出图", "image": str(ctx.p("07-出图", "images") / f"{tgid}-gap.png")})
        n += 1
        ctx.log(f"[S7] 空档插画 {tgid}-gap：{_fmt_mmss(lo, _FPS)}～{_fmt_mmss(hi, _FPS)}（{(hi - lo) / _FPS:.1f} 秒）")
    return n


def _decide_records(ctx):
    """S7 真算：调 `orchestrate.decide_records`，deps 全部经统一付费入口。"""
    from pipeline import orchestrate
    from pipeline.table_prompt import build_table_prompt

    im_rel = ("05-截图分析" if ctx.resolved_content_type == spec.CONTENT_TYPE_SCREENSHOT
              else "06-编排")
    deps = _s7_deps(ctx)
    els = _load_elements(ctx)
    element_gids = {e["mapped_group_id"] for e in els if e.get("mapped_group_id")}
    return orchestrate.decide_records(
        ctx.root, deps=deps, build_table_prompt=build_table_prompt,
        route_hook=_s7_route_hook(ctx),
        # 混杂分流只截图型开（A8.7 第 4/5 条）；理论型恒关，配图仍按 W1 用原图。
        mixed_kinds=(ctx.resolved_content_type == spec.CONTENT_TYPE_SCREENSHOT),
        # ST1-P4c C1：原图原样展示——`kind=ai` 的原图也走「原图」，不再做 AI 插图。
        original_images=True,
        # ST3-P2：含代码/命令/表格的组不出 AI 插图（画面由要素卡片接管）。
        element_gids=element_gids,
        img_dir=ctx.p("07-出图", "images"),
        groups_json=ctx.p("02-分组", "groups_raw.json"),
        image_map_json=ctx.p(im_rel, "image_map.json"))


_HIGHLIGHT_PROMPT = (
    "你是一个代码/表格讲解对口型助手。给定一张卡片的内容（带行号），以及讲解这张卡片的"
    "几句口播稿，为每句口播稿分配它讲到的行号范围。\n\n"
    "卡片内容（行号: 内容）：\n{card}\n\n"
    "口播稿（按讲解顺序的几句）：\n{narration}\n\n"
    "请只输出 JSON 数组，长度与口播句数一致，每项是该句讲到的行号，形如 "
    '{{"lines": [1, 2]}}。行号从 1 开始。某句是过渡/概括、不指向具体行时，'
    "给它覆盖整卡的范围或空数组。只输出 JSON，不要任何解释。"
)


def _stub_highlight(prompt, max_tokens=4096):
    """高亮对应桩：返回空数组（→ 回退整卡显示，不高亮）。"""
    return "[]"


def _strip_code_fence(raw):
    """把 DeepSeek 常把 JSON 包在 ```json … ``` 围栏里的返回剥出裸 JSON 文本。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def _card_text_with_numbers(el):
    """把要素内容拼成带行号的文本（代码=每行；表格=表头+数据行，行号从 1 数数据行）。"""
    if el.get("type") == "table":
        rows = el.get("rows") or []
        parts = ["表头: " + " | ".join(el.get("header") or [])]
        for i, r in enumerate(rows):
            parts.append(f"{i + 1}: " + " | ".join(r))
        return "\n".join(parts)
    lines = el.get("lines") or []
    return "\n".join(f"{i + 1}: {l}" for i, l in enumerate(lines))


def _num_lines(el):
    if el.get("type") == "table":
        return len(el.get("rows") or [])
    return len(el.get("lines") or [])


def _generate_highlights(ctx, els, groups):
    """ST3-P2 C：为每张代码卡/表格卡生成「口播句 → 高亮行」对应（DeepSeek，统一付费入口）。

    输入 = 卡片内容（带行号）+ 该组口播稿（group_text）；输出 = 每句对应的行号范围（有序）。
    不合法（JSON 解析失败/行号越界/长度与口播句数不符）→ 该卡回退 None（整卡显示，不高亮）。
    命令行（terminal）不需要对应关系，跳过。结果写 `06-编排/高亮对应.json`。
    """
    cache_dir = _cache_dir(ctx, "S7")
    text_call = paid.make_paid_text_call(
        ctx, step="S7", cache_dir=cache_dir, model=_S7_MODEL, describe="高亮对应",
        invoke_real=_real_text_call(_S7_MODEL), invoke_stub=_stub_highlight)

    gid_to_text = {g["group_id"]: g["group_text"] for g in groups}
    mappings = {}
    for el in els:
        if el.get("type") not in ("code", "table"):
            continue
        gid = el.get("mapped_group_id")
        nar = gid_to_text.get(gid) if gid else None
        if not nar:
            mappings[el["id"]] = None
            continue
        num = _num_lines(el)
        if num <= 0:
            mappings[el["id"]] = None
            continue
        prompt = _HIGHLIGHT_PROMPT.format(
            card=_card_text_with_numbers(el), narration=nar)
        raw = text_call(prompt, max_tokens=2048)
        try:
            arr = json.loads(_strip_code_fence(raw or ""))
            if not isinstance(arr, list):
                raise ValueError("非数组")
            out = []
            for it in arr:
                ls = it.get("lines") if isinstance(it, dict) else None
                if not isinstance(ls, list):
                    raise ValueError("缺 lines")
                ints = [int(x) for x in ls]
                if any(x < 1 or x > num for x in ints):
                    raise ValueError(f"行号越界: {ints}")
                out.append({"lines": sorted(set(ints))})
            if not out:
                raise ValueError("空结果")
            mappings[el["id"]] = out
        except Exception as e:
            ctx.log(f"[S7] 高亮对应 {el['id']} 回退整卡：{type(e).__name__}: {e}")
            mappings[el["id"]] = None

    out = ctx.p("06-编排", "高亮对应.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"mappings": mappings}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    n_ok = sum(1 for v in mappings.values() if v)
    ctx.log(f"[S7] 高亮对应 {len(mappings)} 卡 / 有效 {n_ok} -> {out}")
    return mappings


def _generate_highlights_if_needed(ctx):
    """S7 里统一调用的高亮对应入口：无要素时跳过（不写文件）。"""
    els = _load_elements(ctx)
    if not els:
        return None
    groups = json.loads(ctx.p("02-分组", "groups_raw.json").read_text(encoding="utf-8")).get("groups", [])
    return _generate_highlights(ctx, els, groups)


def _s7_route_hook(ctx):
    """截图型新文章的 route_hook：读 **S6 的 `05-截图分析/镜头计划.json`**（导演层产出）。

    依据同 P7 `stage_b3`（`SHOT-M4-P7-新文章整篇/p7_build.py:104-107`）：
    `screenshot_gids = {s["group"] for s in shots_plan["shots"]}` 后建钩子。**不读**
    `06-编排/shots_plan.json`——那是 S7 自己的时间轴产物，读了就成环（新文章首跑时不存在）。
    """
    if ctx.resolved_content_type != spec.CONTENT_TYPE_SCREENSHOT:
        return None
    p = ctx.p("05-截图分析", "镜头计划.json")
    if not p.is_file():
        return None
    from pipeline import shot_route
    plan = json.loads(p.read_text(encoding="utf-8"))
    shot_gids = {s["group"] for s in plan.get("shots", [])}
    return shot_route.make_route_hook(plan, shot_gids)


OVERVIEW_LOG_REL = "logs/导览判定.json"


def _record_overview(ctx, sid):
    """ST3-P25：正文里有 ≥2 个导览候选时（`routing.detect_overview` 只认最靠前的一个），
    把判定写进 `logs/导览判定.json`，检查报告据此提示；不足 2 个时删掉旧记录（没有就什么都不做）。
    这个文件不在任何步骤的输入里，不影响跳过判定。缺分组文件时什么都不做（报错留给步骤本身）。"""
    from pipeline import routing
    gp = ctx.p("02-分组", "groups_raw.json")
    if not gp.is_file():
        return None
    groups = json.loads(gp.read_text(encoding="utf-8"))["groups"]
    amb = routing.overview_ambiguity([g for g in groups if not g["starts_with_bracket_title"]])
    p = ctx.root / OVERVIEW_LOG_REL
    if amb is None:
        if p.exists():
            p.unlink()
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": 1, "candidates": 1 + len(amb["ignored"]), **amb},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.log(f"[{sid}] 导览候选 {1 + len(amb['ignored'])} 处，按最靠前的 {amb['chosen']['group_id']} 处理，"
            f"忽略 {'、'.join(x['group_id'] for x in amb['ignored'])} -> {OVERVIEW_LOG_REL}")
    return amb


def _gnum(gid):
    m = re.fullmatch(r"G0*(\d+)", str(gid))
    return m.group(1) if m else str(gid)


def overview_notice_text(rec):
    """检查报告里「导览候选不止一处」那条提示的原文。"""
    ign = "、".join(_gnum(x["group_id"]) for x in rec["ignored"])
    return (f"正文里有 {rec['candidates']} 处像\"导览\"的段落，已按最靠前的第 {_gnum(rec['chosen']['group_id'])} 组处理；"
            f"第 {ign} 组已忽略，请确认。")


def step_s7(ctx):
    """编排：**有导入件用导入件，没有就真算**（ST1-P4a2 §一.1）。"""
    _record_overview(ctx, "S7")
    if ctx.resolved_content_type == spec.CONTENT_TYPE_SCREENSHOT:
        return _s7_screenshot(ctx)
    return _s7_theory(ctx)


def _s7_theory(ctx):
    if ctx.p("06-编排", "插图决策.json").exists():
        _, records = _load_decisions(ctx)
        doc = _write_requirements(ctx, records, "导入自 R168（插画决策；无模型缓存可重放）",
                                  decision_file="06-编排/插图决策.json")
        src = "插图决策导入自 R168"
    else:
        records = _decide_records(ctx)
        doc = _write_requirements(ctx, records, "引擎真算（统一付费入口）")
        st = ctx.paid_stats
        src = (f"决策真算（调用 {st['calls']} / 命中 {st['hits']} / 桩 {st['stub_calls']}）"
               if (st["calls"] or st["hits"]) else "决策真算")
    # ST3-P11 R6：空档插画需求（理论型同样适用）。
    if _gap_requirements(ctx, records, _trial_scenes(ctx, records, None, {})):
        doc = _write_requirements(ctx, records, doc["source"], decision_file=doc.get("决策文件"))
    need = sum(1 for i in doc["items"] if i["请求内容哈希"])
    ctx.log(f"[S7] 出图需求 {len(doc['items'])} 项 / 需出图 {need} -> 06-编排/出图需求.json")
    # ST3-P2 C：高亮对应（代码卡/表格卡；与出图需求无关，独立生成）
    _generate_highlights_if_needed(ctx)
    return {"source": src}


def _s7_screenshot(ctx):
    """截图型编排：出图需求清单 + 最终时间轴。

    时间轴输入与 `SHOT-M5-P12c\\p12c_build.py` 的 build 一致（image_map / groups_raw /
    sentences / direction / image_sizes / labels / stay_endings）；唯一的差别是
    `img_gids`（"已有插画的集合"）改从**出图需求清单**来，不再读 `07-出图/manifest.json`
    ——否则会与 S8「读 S7 的清单」成环。
    """
    from pipeline import routing, shot_timeline
    from pipeline.shot_director import fn_of

    im = _load_json(ctx, "05-截图分析/image_map.json")
    gr = _load_json(ctx, "02-分组/groups_raw.json")
    sent = _load_json(ctx, "03-配音/sentences.json")
    ai_out = _load_json(ctx, "06-编排/ai_out.json")
    labels = _load_json(ctx, "05-截图分析/labels.json")["labels"]
    stay_endings = _load_json(ctx, "05-截图分析/stay_endings.json")
    if ctx.p("06-编排", "插图决策.json").exists():
        _, records = _load_decisions(ctx)
        dec_src, dec_file = "导入自 P12c（插画决策；无模型缓存可重放）", "06-编排/插图决策.json"
    else:
        records = _decide_records(ctx)
        dec_src, dec_file = "引擎真算（统一付费入口）", None

    img_dir = ctx.p("00-输入", "图片和附件")
    image_sizes = {}
    for e in im["images"]:
        fn = fn_of(e)
        p = img_dir / fn
        if not p.is_file():
            raise StepError(f"缺少截图原图: {p}")
        image_sizes[fn] = _img_size(p)
    g80 = ctx.p("07-出图", "images", "G80-generic.png")
    if g80.exists():
        image_sizes["G80-generic.png"] = _img_size(g80)

    img_gids = {r["group_id"] for r in records if r.get("image")}

    # 时间轴只排截图原图；`kind=ai` 的原图不进截图时间轴（A8.7 第 4/6 条）。
    # ST1-P4c 段丁：无组号的图（回填+兜底仍失败）不进截图时间轴，否则 S6/S7 会 KeyError。
    # ST3-P11 R11：没挂上任何口播组的截图按原文位置就近兜底，照样进时间轴，并登记为提示。
    im_ss, orphans = shot_timeline.attach_orphans(
        {**im, "images": [e for e in im["images"] if e.get("kind", "screenshot") == "screenshot"]},
        [g["group_id"] for g in gr["groups"] if not g.get("starts_with_bracket_title")])
    art_p = ctx.p("00-输入", "文章.md")
    # ST3-F2：图句匹配参与时间安排——每张图的期望出现帧 = 讲到它的那句话的开始帧。
    m2_p = ctx.p("05-截图分析", "matches_v2.json")
    matches_v2 = json.loads(m2_p.read_text(encoding="utf-8")) if m2_p.exists() else None
    article = art_p.read_text(encoding="utf-8") if art_p.exists() else None
    from pipeline import shot_annotate
    shot_annotate.use_font(_runtime_font(ctx))
    tl_inputs = {
        "image_map": im_ss, "groups_raw": gr, "sentences": sent,
        "direction": ai_out["direction"], "image_sizes": image_sizes,
        "labels": labels, "img_gids": img_gids, "matches_v2": matches_v2,
        # ST3-P11：R4 按原文判断「连着贴」；R3 短命令进旁边截图的命令框。
        "article_text": article,
        "inline_commands": _load_inline_commands(ctx),
    }

    body = [g for g in gr["groups"] if not g.get("starts_with_bracket_title")]
    transition_ids, func_short_ids = routing.identify_no_image_groups(gr["groups"])
    abstain_ids = {r["group_id"] for r in records if r.get("path") == "不出图(弃权)"}
    no_img = transition_ids | func_short_ids | abstain_ids

    def _build(annotations):
        tl = shot_timeline.build_timeline(dict(tl_inputs, annotations=annotations), stay_endings=stay_endings)
        shot_gids = {s["group"] for s in tl["shots"]}
        fallback_groups = sorted({g["group_id"] for g in body
                                  if g["group_id"] not in img_gids and g["group_id"] not in no_img
                                  and g["group_id"] not in tl["stay_covered"]
                                  and g["group_id"] not in shot_gids})
        plan = {"shots": tl["shots"], "stay_covered": sorted(tl["stay_covered"]),
                "mapping_fix": tl["mapping_fix"],
                "fallback_groups": fallback_groups, "partial_recover": tl["partial_recover"],
                "recommend_log": tl.get("recommend_log", []),
                "pending_no_img": tl.get("pending_no_img", []),
                "switch_log": tl.get("switch_log", []),
                "meaningless_fns": tl.get("meaningless_fns", []),
                "inline_cards": tl.get("inline_cards", []), "inline_boxed": tl.get("inline_boxed", []),
                "inline_log": tl.get("inline_log", []), "annot_over": tl.get("annot_over", []),
                "orphans": orphans}
        return tl, plan

    # ST3-P12 R1：左侧标注逐图、逐句生成。先不带标注把时间线和场景试排一遍，拿到每张图的实际在屏时间，
    # 再按在屏期间的口播句生成标注，带着标注重排。标注会改变命令的去向时（命令框放不下、版式翻转），
    # 在屏时间跟着变，再来一轮（最多三轮；仍有出入的条目由 S9 按最终时间去掉并登记）。
    tl, plan = _build({})
    trial = _trial_scenes(ctx, records, tl["shots"], plan)
    ann_log, ann_counter, rounds = [], {}, 0
    while trial is not None and rounds < 3:
        rounds += 1
        windows = _shot_windows(trial)
        boxed = {}
        for c in plan["inline_log"]:
            if c["dest"] == "命令框":
                boxed.setdefault(c["near_image"], []).append(c["text"])
        ann, ann_log = _annotate_shots(ctx, im_ss, gr["groups"], sent, article, windows, boxed, ann_counter,
                                       labels=labels)
        tl, plan = _build(ann)
        trial = _trial_scenes(ctx, records, tl["shots"], plan)
        if trial is None or _shot_windows(trial) == windows:
            break
    shots = tl["shots"]
    stay_covered = tl["stay_covered"]
    # ST3-P11 R5 / R6：插画取材范围、空档插画需求（都只改出图需求，不动镜头）。
    sidx = _sentence_index(ctx, gr["groups"], sent)
    shown = None if trial is None else {sc["alloc_id"].split(":", 1)[1] for sc in trial["scenes"]
                                        if str(sc.get("alloc_id", "")).startswith("ill:")}
    n_scope = _rescope_illustrations(ctx, records, shots, sidx, shown=shown)
    n_gap = _gap_requirements(ctx, records, trial)
    # ST3-P12b：去掉了 R1 兜底第二层（标注空档插画）。超过 8 秒没有新内容的空档只在 S9 的检查报告里提示。
    records[:] = [r for r in records if r.get("path") != _FILL_PATH]
    src_count = {}
    for row in ann_log:
        src_count[row["source"]] = src_count.get(row["source"], 0) + 1
    plan["annotation"] = {"rounds": rounds, "by_source": src_count, "requests": ann_counter.get("requests", 0)}
    out = ctx.p("06-编排", "shots_plan.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.p("06-编排", "标注生成记录.json").write_text(
        json.dumps({"说明": "ST3-P12 R1：每张截图的在屏时间、在屏期间的口播句、模型两次输出与校验结果、最终条目",
                    "rounds": rounds, "requests": ann_counter.get("requests", 0), "by_source": src_count,
                    "shots": ann_log, "over": plan["annot_over"]},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    doc = _write_requirements(ctx, records, dec_src, decision_file=dec_file)
    need = sum(1 for i in doc["items"] if i["请求内容哈希"])
    n_fail = sum(1 for s in shots if not s["self_check"]["pass"])
    ctx.log(f"[S7] 镜头 {len(shots)} / 自检不过 {n_fail} / 越界修正 {len(plan['mapping_fix'])}"
            f" / 停留覆盖 {len(stay_covered)} / 出图需求 {need}"
            f" / 命令框 {len(plan['inline_boxed'])} / 命令卡片 {len(plan['inline_cards'])}"
            f" / 取材改写 {n_scope} / 空档插画 {n_gap} / 兜底截图 {len(orphans)}"
            f" / 标注 {src_count}（{rounds} 轮，本次发出请求 {ann_counter.get('requests', 0)} 次）"
            f" -> {out}")
    # ST3-P2 C：高亮对应（代码卡/表格卡；截图型同样支持）
    _generate_highlights_if_needed(ctx)
    return {"source": dec_src}


# --------------------------------------------------------------------------
# S8 出图（ST1-P4a：只读 S7 的 `06-编排/出图需求.json`）
# --------------------------------------------------------------------------


# 出图服务的提交 / 查询 / 下载在 `imagegen.py`（ST3-P26 §4.2 第 7 条；ST3-P27 起默认是 RunningHub AI 应用，
# 官方模型接口为备用），这里只管调度：并行、软等待、缺图、任务号登记。
# ST3-P16：单张图的任务返回失败（含 1501 内容审核偶发误判）时，用同一提示词、同一参数再提交，
# 最多再 3 次（一张图最多 4 次），每次之前等这么多秒。临时网络故障由 `netretry` 退避（不算在这里）。
_RH_RESUBMIT_WAITS = (10, 30, 60)
# ST3-P26 §4.2：S8 调度参数（集中在这里）。
_RH_POLL_SECONDS = 3            # 每张图两次查询之间隔几秒
_S8_SOFT_WAIT_SEC = 8 * 60      # 一张图查了这么久还没出来 → 先登记为"待收取"，空出位置给别的图
_S8_HARD_WAIT_SEC = 60 * 60     # 一张图本次运行里最多等这么久（从本次第一次查它算起）；到了记为缺图
_S8_STOP_GRACE_SEC = 20         # P31b：停止时最多等这么久，让正在提交的几张把任务号登记进账本
_S8_MAX_ACTIVE = 3              # 同时在提交 / 查询中的图最多几张（下表没登记的服务用它）
# ST3-P27 §4.5：按出图服务分别定（键 = imagegen 的 service_key）。AI 应用实测 5 张并发无排队，这里仍取 3。
_S8_MAX_ACTIVE_BY_SERVICE = {"runninghub_app": 3, "runninghub": 3}
_S8_MAX_PARKED = 3              # "待收取"最多几张（满了以后，超过软等待的图就接着等，不再让位）
# 每一次失败都登记在这里（一行一条 JSON），排查不用再翻运行日志。
RH_FAIL_LOG = "出图失败登记.jsonl"
# 4 次都失败的插画记为缺图：S9 不排它、由前后画面补位，检查报告标红；下次正常 run 只为它们重新出图。
MISSING_ILLUSTRATIONS = "缺图.json"


class IllustrationFailed(StepError):
    """一张插画提交了 1 + len(_RH_RESUBMIT_WAITS) 次都失败（S8 记为缺图，不让整篇停下）。"""


def _rh_fail_record(ctx, fname, tid, info, attempt, action):
    """把一次出图失败写进 `07-出图/出图失败登记.jsonl` 和运行日志。"""
    rec = {"时间": ledger.now_iso(), "图片": fname, "任务号": str(tid),
           "失败码": info.get("errorCode", ""), "失败信息": info.get("errorMessage", ""),
           "第几次提交": attempt, "处理": action}
    p = ctx.p("07-出图", RH_FAIL_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _S8_FILE_LOCK, open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    ctx.log(f"[S8] 出图失败 {fname}（task {tid}，第 {attempt} 次提交）："
            f"errorCode {rec['失败码'] or '无'} {rec['失败信息']}；{action}")


def missing_illustrations(root):
    """S8 登记的缺图里、磁盘上仍然没有的那些文件名（S9 不排它们；cli 据此让 S8 重跑补图）。"""
    p = Path(root) / "07-出图" / MISSING_ILLUSTRATIONS
    if not p.is_file():
        return []
    try:
        names = json.loads(p.read_text(encoding="utf-8")).get("files") or []
    except Exception:
        return []
    return [n for n in names if not (Path(root) / "07-出图" / "images" / n).is_file()]


def illustration_missing_text(names):
    """检查报告 / 运行日志里「插画缺失」那一条标红的原文。"""
    return (f"插画缺失 {len(names)} 张：{'、'.join(names)}"
            f"（出图多次失败，已用前后画面补位；直接重跑即可补出）")


def rerun_reason(root, sid):
    """产物与指纹都没变、但这一步仍要重跑的理由（没有返回 None）。

    - S8：缺图补出。
    - S4：v5.3 及以前跑完的文章没有 `04-对齐/char_times.json`，免费重跑补出。
    - S6/S7/S9（读对齐的步骤）：S4 判定改用新方法、而本步的记录不是按这份对齐算的
      （记录里的 `alignment_sig` 与磁盘对齐不一致；老账本没有这个字段）。
      对齐文件只在 `alignment_sig` 上判新鲜度，不进输入指纹——避免"新增依赖声明"
      让已完成的文章在付费步骤上白白重跑。
    """
    root = Path(root)
    if sid == "S8":
        miss = missing_illustrations(root)
        if miss:
            return f"缺图 {len(miss)} 张，只为它们重新出图（{'、'.join(miss)}）"
        return None
    if sid == "S4":
        align_p = root / "04-对齐" / "alignment.json"
        chars_p = root / "04-对齐" / "char_times.json"
        if align_p.is_file() and not chars_p.is_file():
            return "缺 04-对齐/char_times.json（v5.3 及以前），S4 免费重跑补出"
        return None
    if sid in ("S6", "S7", "S9"):
        dec_p = root / "logs" / "对齐判定.json"
        if not dec_p.is_file():
            return None
        try:
            decision = json.loads(dec_p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if decision.get("method") != "new":
            return None
        doc = ledger.load(root) or {}
        rec = ledger.find_step(doc, sid) or {}
        from pipeline import align_global
        cur = align_global.alignment_signature(root)
        if rec.get("alignment_sig") == cur:
            return None
        # 付费步骤（S6/S7）：S4 本轮重算前后对齐签名相同 → 对齐没换，产物仍有效。
        # S9 免费，即使没换也重跑一次（从磁盘读新对齐并把 `alignment_sig` 补进账本）。
        if sid in ("S6", "S7"):
            s4 = ledger.find_step(doc, "S4") or {}
            if s4.get("alignment_sig_before") == cur:
                return None
        return "对齐已改用新方法，需要按磁盘上的新对齐重跑"
    return None


# R101 B：每张产出图的缓存键（= 出图需求项的 `请求内容哈希`）记在这里，S8 据此判定旧图还能不能用。
ILLUSTRATION_RECORD = "图片记录.json"


def _load_illustration_record(root):
    """{文件名: {"请求内容哈希", "sha256", "time"}}；没有或读不了返回 {}。"""
    p = Path(root) / "07-出图" / ILLUSTRATION_RECORD
    try:
        return dict(json.loads(p.read_text(encoding="utf-8")).get("files") or {})
    except (OSError, ValueError, AttributeError):
        return {}


def _save_illustration_record(root, files):
    p = Path(root) / "07-出图" / ILLUSTRATION_RECORD
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({
        "schema_version": 1,
        "说明": "每张插画对应的出图缓存键（请求内容哈希 = 提示词 + 模型 + 尺寸）；S8 只在键一致时复用已有图片",
        "files": files}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


_REDO_TEXT = {"changed": "这个组的内容变了（缓存键与图片记录不一致）",
              "nomatch": "已有图片没有缓存键记录，且与本组缓存里的图不同",
              "norecord": "已有图片没有缓存键记录，本组缓存里也没有"}


def illustration_plan(root, items):
    """R101 B：出图需求的每一项该怎么处理（S8 和费用估算共用）。返回 {文件名: 状态}：

    - `reuse`    —— 图片在，且图片记录里的缓存键与本项一致；或者没有记录、但本项缓存里的图与磁盘上
                    这张逐字节相同（1.0.0 及以前出的图都没有记录，走这一条）：直接用（不付费、不改文件）；
    - `changed`  —— 图片在、记录里的键不一致（这个组号现在的内容变了，例如删了一节、组号前移）；
    - `nomatch`  —— 图片在、没有记录，本项缓存里有图但和磁盘上的不同（磁盘上是别的组的旧图）；
    - `norecord` —— 图片在、没有记录，本项缓存里也没有；
    - `new`      —— 图片不在。
    除 `reuse` 外都走统一付费入口：先查缓存，查不到才付费出图（`--no-paid` 下查不到就停）。
    """
    root = Path(root)
    img_dir = root / "07-出图" / "images"
    cache_dir = root / "07-出图" / "cache"
    rec = _load_illustration_record(root)
    out = {}
    for it in items:
        fname, key = it.get("文件名"), it.get("请求内容哈希")
        if not fname or not key:
            continue
        p = img_dir / fname
        if not p.is_file():
            out[fname] = "new"
            continue
        have = (rec.get(fname) or {}).get("请求内容哈希")
        if have is not None:
            out[fname] = "reuse" if have == key else "changed"
            continue
        hit, val, _ = paid.load_cached(cache_dir, key)
        if not hit:
            out[fname] = "norecord"
        elif isinstance(val, (bytes, bytearray)) and bytes(val) == p.read_bytes():
            out[fname] = "reuse"
        else:
            out[fname] = "nomatch"
    return out


def _rh_stub(prompt, dest, cache_dir):
    """桩：不联网，返回一张**真实尺寸**的占位 PNG（1k / 16:9 → 1024×576）。"""
    return stubs.stub_generate_image(prompt)


def _find_runninghub_job(doc, fname):
    """在账本 pending_jobs 里按文件名找已登记的 RunningHub 出图任务号（防重复扣费）。

    ST3-P27：两个出图服务（AI 应用 `runninghub_app`、官方接口 `runninghub`）登记的都认，
    续查时回到当初提交它的那个服务。"""
    for j in (doc.get("pending_jobs") or []):
        if j.get("step") == "S8" and j.get("service") in imagegen.SERVICE_KEYS and j.get("note") == fname:
            return j
    return None


def _s8_max_active(service_key):
    return _S8_MAX_ACTIVE_BY_SERVICE.get(service_key, _S8_MAX_ACTIVE)


def _ledger_doc(ctx):
    """付费步骤登记任务号用的账本对象。

    ST3-P26：用 cli 注入的那一份（`ctx.ledger_doc`，和真实付费后的即时记账是同一个对象）。
    以前步骤自己从磁盘另读一份：即时记账落盘时拿旧的 `pending_jobs` 把步骤刚清掉的任务号写回去，
    出图全部收完后账本里还留着已收取的任务号（P25W2）。测试 / 工具脚本里没有注入时才从磁盘读。
    """
    doc = getattr(ctx, "ledger_doc", None)
    if doc is not None:
        return doc
    return ledger.load(ctx.root) or ledger.new_doc(ctx.article, ctx.resolved_content_type)


def _ledger_lock(ctx):
    lk = getattr(ctx, "ledger_lock", None)
    return lk if lk is not None else _FALLBACK_LEDGER_LOCK


_FALLBACK_LEDGER_LOCK = threading.RLock()
_S8_FILE_LOCK = threading.Lock()


class _Parked(Exception):
    """这张图超过软等待：任务号已登记，先让出位置，全部提交完以后统一收取。"""


class _Cancelled(Exception):
    """别的图出了让整步停下的错：这张不再提交 / 查询（已登记的任务号留着，续跑会先续查）。"""


class _S8Scheduler:
    """S8 调度：最多 `_S8_MAX_ACTIVE` 张图同时在提交 / 查询；超过软等待的先登记"待收取"、让出位置；
    全部提交完以后统一收取，每张最多等到 `_S8_HARD_WAIT_SEC`。出图服务只经 `imagegen.ImageService` 调用。"""

    def __init__(self, ctx, doc):
        self.ctx, self.doc = ctx, doc
        self.lock = _ledger_lock(ctx)
        self.stop = threading.Event()
        self.parked = []           # 待收取的文件名（按登记先后）
        self._t0 = {}              # 文件名 -> 本次运行里开始查这个任务号的时刻
        self._svc = None
        self._others = {}          # 账本里别的出图服务登记的任务号要回到那个服务续查
        self._by_tid = {}          # 本次运行里提交的任务号 -> 提交它的服务（待收取的图续查时用回同一个）
        self.used = []             # 本次实际发过请求的出图服务（账本 S8 `image_service`）
        self.submitting = 0        # P31b：正在提交、任务号还没登记进账本的张数（停止时要等它们登记完）

    def _use(self, svc):
        with self.lock:
            lab = getattr(svc, "label", "") or getattr(svc, "service_key", "") or svc.name
            if lab not in self.used:
                self.used.append(lab)
                self.ctx.log(f"[S8] 出图服务 {lab}")

    def service(self):
        with self.lock:
            if self._svc is None:
                self._svc = imagegen.service_for(_require_env("RUNNINGHUB_API_KEY"), log=self.ctx.log)
            return self._svc

    def service_for_job(self, job):
        """续查已登记任务号用的服务：本次提交的 → 提交它的那个；登记的就是当前服务（或旧账本没写服务）
        → 当前服务；否则回到当初提交它的那个服务。"""
        with self.lock:
            mine = self._by_tid.get(str(job.get("task_id")))
        if mine is not None:
            return mine
        svc = self.service()
        key = job.get("service")
        cur = getattr(svc, "service_key", None)
        if (not key or key == cur or key not in imagegen.BY_KEY
                or cur not in imagegen.BY_KEY):     # 换成了别的实现（测试替身等）：全部交给它
            return svc
        with self.lock:
            if key not in self._others:
                self._others[key] = imagegen.service_by_key(key, _require_env("RUNNINGHUB_API_KEY"),
                                                            log=self.ctx.log)
            return self._others[key]

    def _save(self):
        ledger.save_atomic(self.ctx.root, ledger.touch(self.doc))

    def _try_park(self, fname):
        with self.lock:
            if len(self.parked) >= _S8_MAX_PARKED:
                return False
            self.parked.append(fname)
            return True

    def stopping(self):
        """整步要停：别的图出了让整步停下的错，或者 APP 写了停止标记（P31b）。"""
        return self.stop.is_set() or stopflag.requested()

    def _wait(self, svc, fname, tid, may_park):
        t0 = self._t0[fname]
        while True:
            if self.stopping():
                raise _Cancelled()
            time.sleep(_RH_POLL_SECONDS)
            if self.stopping():
                raise _Cancelled()      # 任务号留在账本里：续跑先续查，不重新提交
            status, x = svc.check(tid)
            if status in ("success", "fail"):
                return status, x
            el = time.monotonic() - t0
            if el >= _S8_HARD_WAIT_SEC:
                return "timeout", None
            if may_park and el >= _S8_SOFT_WAIT_SEC and self._try_park(fname):
                return "park", None

    def run(self, fname, prompt, may_park):
        """一张图：续查已登记的任务号或提交，等结果，取回。返回图片字节（交给 paid.perform 写缓存、记账）。"""
        ctx, doc = self.ctx, self.doc
        with self.lock:
            job = _find_runninghub_job(doc, fname)
        tries = 1 + len(_RH_RESUBMIT_WAITS)
        for attempt in range(1, tries + 1):
            if job:
                tid = job["task_id"]
                svc = self.service_for_job(job)
                job = None
                self._t0.setdefault(fname, time.monotonic())
                ctx.log(f"[S8] 续查已登记任务 {tid}（{fname}）")
            else:
                if self.stopping():
                    raise _Cancelled()
                svc = self.service()
                with self.lock:
                    self.submitting += 1
                try:
                    tid = svc.submit(prompt)
                    # 提交后立即落账本：超时/中断后续跑能查到，不重复提交（防重复扣费）。
                    with self.lock:
                        self._by_tid[str(tid)] = svc
                        ledger.add_job(doc, "S8", getattr(svc, "service_key", "") or "runninghub", tid,
                                       note=fname)
                        self._save()
                finally:
                    with self.lock:
                        self.submitting -= 1
                self._t0[fname] = time.monotonic()
            self._use(svc)
            status, x = self._wait(svc, fname, tid, may_park)
            if status == "success":
                data = svc.download(x)
                # ST3-P27 §4.4：服务报了实际金额（AI 应用的 usage）就按它记账，没报按单价。
                paid.note_charge(svc.usage_usd(tid), "USD")
                with self.lock:
                    ledger.remove_job(doc, tid)
                    self._save()
                return data
            if status == "fail":
                # 任务失败（失败的任务不收费）：清号、登记，等一会儿用同一提示词、同一参数再提交。
                with self.lock:
                    ledger.remove_job(doc, tid)
                    self._save()
                if attempt < tries:
                    wait = _RH_RESUBMIT_WAITS[attempt - 1]
                    _rh_fail_record(ctx, fname, tid, x, attempt,
                                    f"{wait} 秒后用同一提示词重新提交（第 {attempt + 1} 次）")
                    time.sleep(wait)
                    continue
                _rh_fail_record(ctx, fname, tid, x, attempt, f"{tries} 次都失败，记为缺图")
                raise IllustrationFailed(f"RunningHub 生成失败 {tries} 次（最后 task {tid}，{fname}）")
            if status == "park":
                ctx.log(f"[S8] {fname}（task {tid}）等了 {_S8_SOFT_WAIT_SEC // 60} 分钟还没出来，"
                        f"先登记为待收取，接着出别的图")
                raise _Parked(tid)
            # 等到上限：任务号留在账本里（下次运行先续查它），这一张按出图失败记为缺图。
            mins = _S8_HARD_WAIT_SEC // 60
            _rh_fail_record(ctx, fname, tid, {"errorCode": "", "errorMessage": f"等了 {mins} 分钟还没出图"},
                            attempt, "任务号保留待查，记为缺图（下次运行先续查这个任务号）")
            raise IllustrationFailed(f"RunningHub 出图超时（task {tid}，{fname}，等了 {mins} 分钟，"
                                     f"任务号已登记，下次运行先续查）")


def step_s8(ctx):
    """出图：逐条读 `06-编排/出图需求.json` 出图，已有的图不重出。

    每张图都经统一付费入口 `paid.perform`（service=`runninghub`，键=请求内容哈希）：
    命中 `07-出图/cache` 就不发请求、不计费；未命中才走真实/桩调用并写缓存。

    ST3-P2 防重复扣费（方案端裁定）：真实提交后立即把任务号写入账本 `pending_jobs`
    （同豆包配音 S3 做法）；续跑时先查已登记任务号——已出图则直接取回不重新提交，任务失败才清号重新提交。
    桩模式不发请求、不涉及任务号。`manifest.json` 由清单里的 `records` 原样写出（理论型/截图型都是这一条路径）。

    ST3-P16：任务失败时等一会儿用同一提示词再提交（最多 4 次，见 `_RH_RESUBMIT_WAITS`），每次失败
    登记进 `07-出图/出图失败登记.jsonl`；4 次都失败就记进 `07-出图/缺图.json`、接着出其他图，本步不报错。

    ST3-P26：要真实出图的图最多 `_S8_MAX_ACTIVE` 张同时进行；一张图超过软等待（`_S8_SOFT_WAIT_SEC`）先登记为
    待收取、接着出别的，全部提交完以后统一收取；等到 `_S8_HARD_WAIT_SEC` 还没出来按出图失败记缺图，
    任务号留在账本里。命中缓存、桩、`--no-paid` 的图不发请求，仍在主线程里按清单顺序逐张处理。
    全部完成后清掉账本里已经收取成功的出图任务号。
    """
    req = _load_json(ctx, "06-编排/出图需求.json")
    records = req.get("records") or []
    items = req.get("items") or []
    img_dir = ctx.p("07-出图", "images")
    img_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = ctx.p("07-出图", "cache")

    doc = _ledger_doc(ctx)
    sched = _S8Scheduler(ctx, doc)
    # ST3-P27：计价、并发数按当前选的出图服务（AI 应用 / 官方接口）；缓存键两者相同。
    svc_key = imagegen.service_key()
    max_active = _s8_max_active(svc_key)

    made = skipped = 0
    missing = []
    order = {}
    # R101 B：旧图能不能用只看内容（缓存键），不看文件名在不在（组号前移时旧文件名会指向别的组的图）。
    plan = illustration_plan(ctx.root, items)
    img_rec = _load_illustration_record(ctx.root)
    redo = {"changed": 0, "nomatch": 0, "norecord": 0}

    def _record(it, dest):
        """写出一张图后，把它的缓存键记进图片记录（每张都落盘：中途停下也不丢）。"""
        with _S8_FILE_LOCK:
            img_rec[it["文件名"]] = {"请求内容哈希": it["请求内容哈希"],
                                    "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
                                    "time": ledger.now_iso()}
            _save_illustration_record(ctx.root, img_rec)
    # ST3-P23 §3.6：每完成或失败一张发一次进度（已有的图也算完成）。
    total = sum(1 for it in items if it.get("请求内容哈希"))
    st = {"done": 0, "collected_parked": 0}

    def _parked_note():
        n = len(sched.parked) - st["collected_parked"]
        return f"{n} 张图等待较久，全部提交后统一收取" if n > 0 else None

    def _tick(message=None):
        note = _parked_note()
        msg = "；".join(m for m in (message, note) if m) or None
        _progress(ctx, "S8", st["done"], total, "张", msg)

    def _perform(it, dest, may_park=True, reserved=0.0):
        fname = it["文件名"]
        return paid.perform(
            ctx, step="S8", service=svc_key, cache_service="runninghub",
            cache_dir=cache_dir, key=it["请求内容哈希"],
            describe=f"出图 {it.get('组号')}（{it.get('插画类型')}）", quantity=1.0,
            invoke_real=lambda: sched.run(fname, it["提示词"], may_park),
            invoke_stub=lambda p=it["提示词"], d=dest: _rh_stub(p, d, cache_dir),
            reserved_cny=reserved)

    def _handle(it, dest, ok, val):
        """一张图有了结果（主线程里）。返回 None；让整步停下的错误返回异常。"""
        nonlocal made
        fname = it["文件名"]
        if ok and paid.is_failed_result(val):
            # ST3-P23 §3.2：取回的图片为空（未写缓存），按这一张出图失败处理。
            ok, val = False, IllustrationFailed(f"RunningHub 取回的图片为空（{fname}）")
        if ok:
            dest.write_bytes(val if isinstance(val, (bytes, bytearray)) else str(val).encode("utf-8"))
            _record(it, dest)
            made += 1
            st["done"] += 1
            _tick()
            return None
        if isinstance(val, IllustrationFailed):
            # 多次失败 / 等到上限也不让整篇停下：记为缺图，接着出其他图（S9 补位、检查报告标红）。
            missing.append(fname)
            ctx.log(f"[S8] 缺图 {fname}：{val}；接着出其他图，下次正常运行只为缺图重新出图")
            st["done"] += 1
            _tick(f"{fname} 出图失败，记为缺图")
            return None
        if isinstance(val, _Parked):
            _tick()
            return None
        if isinstance(val, _Cancelled):
            return None
        return val

    results = queue.Queue()
    active = {}
    by_name = {}
    errs = []

    def _worker(it, dest, may_park, reserved):
        try:
            results.put((it, dest, True, _perform(it, dest, may_park, reserved)))
        except BaseException as e:      # noqa: BLE001  交回主线程处理
            results.put((it, dest, False, e))

    def _drain_one():
        while True:
            try:
                it, dest, ok, val = results.get(timeout=1.0)
                break
            except queue.Empty:
                stopflag.check()       # P31b：等出图结果的间隙看停止标记
                continue
        active.pop(it["文件名"], None)
        if it["文件名"] in sched.parked and not isinstance(val, _Parked):
            st["collected_parked"] += 1        # 待收取的图有了结果
        err = _handle(it, dest, ok, val)
        if err is not None:
            errs.append(err)
            sched.stop.set()

    def _wind_down():
        """P31b：停止 / 中断时收尾。不再提交新图；正在提交的（请求已发出、任务号还没登记）最多等
        `_S8_STOP_GRACE_SEC` 秒，让任务号登记进账本；正在查的、等着重新提交的不用等——任务号已在账本里
        （续跑只续查、不重新提交），或者没有在途的任务。这期间已经交回的图照常落盘。"""
        polls = int(_S8_STOP_GRACE_SEC / 0.5)
        while active:
            with sched.lock:
                busy = sched.submitting
            try:
                it, dest, ok, val = results.get(timeout=0.5) if busy and polls > 0 else results.get_nowait()
            except queue.Empty:
                if not busy or polls <= 0:
                    break
                polls -= 1
                continue
            except KeyboardInterrupt:
                break                  # 收尾时又被打断：不再等
            active.pop(it["文件名"], None)
            if ok and not paid.is_failed_result(val):
                dest.write_bytes(val if isinstance(val, (bytes, bytearray)) else str(val).encode("utf-8"))
                _record(it, dest)
                ctx.log(f"[S8] 停止前取回 {it['文件名']}")
        with sched.lock:
            jobs = [f"{j.get('task_id')}（{j.get('note')}）" for j in (doc.get("pending_jobs") or [])
                    if j.get("step") == "S8"]
        ctx.log(f"[S8] 已停止：账本里待续查的出图任务 {len(jobs)} 个"
                + (f"：{'、'.join(jobs)}" if jobs else "")
                + (f"；仍在提交、任务号没登记上的 {sched.submitting} 张（等了 {_S8_STOP_GRACE_SEC} 秒）"
                   if sched.submitting else ""))

    def _dispatch(it, dest, may_park):
        one = pricing.pre_estimate(svc_key, 1.0)
        outstanding = len(active) + len(sched.parked) - st["collected_parked"]
        th = threading.Thread(target=_worker, args=(it, dest, may_park, one * outstanding),
                              name=f"S8-{it['文件名']}", daemon=True)
        active[it["文件名"]] = th
        th.start()

    try:
        for idx, it in enumerate(items):
            key = it.get("请求内容哈希")
            if not key:
                continue
            fname = it.get("文件名")
            if not fname:
                raise StepError(f"出图需求项缺文件名: {it.get('组号')}")
            order[fname] = idx
            dest = img_dir / fname
            state = plan.get(fname, "new")
            if state == "reuse":
                skipped += 1          # A5 第 2 条：已有的图不重出（R101 B：缓存键一致才算"已有"）
                if fname not in img_rec:      # 旧文章：键从旧 manifest 还原，补进图片记录
                    _record(it, dest)
                st["done"] += 1
                _tick()
                continue
            if state in redo:
                redo[state] += 1
                ctx.log(f"[S8] {fname}：" + _REDO_TEXT[state] + "，按缓存查，查不到才出图")
            if sched.stop.is_set():
                break
            stopflag.check()          # P31b：每张图开始前看停止标记（完成 / 失败一张后的进度回调里也看）
            # 命中缓存 / 桩 / --no-paid：不发请求，主线程里直接处理（顺序、事件与以前逐张串行时一样）。
            if ctx.stub or ctx.no_paid or paid.load_cached(cache_dir, key)[0]:
                try:
                    val, ok = _perform(it, dest), True
                except IllustrationFailed as e:
                    val, ok = e, False
                err = _handle(it, dest, ok, val)
                if err is not None:
                    raise err
                continue
            while len(active) >= max_active and not sched.stop.is_set():
                _drain_one()
            if sched.stop.is_set():
                break
            stopflag.check()
            by_name[fname] = (it, dest)
            _dispatch(it, dest, may_park=True)
        while active:
            _drain_one()
        # 全部提交完：统一收取待收取的图（不再让位，各自等到上限）。
        for fname in list(sched.parked):
            if sched.stop.is_set():
                break
            while len(active) >= max_active and not sched.stop.is_set():
                _drain_one()
            if sched.stop.is_set():
                break
            it, dest = by_name[fname]
            ctx.log(f"[S8] 收取待收取的图 {fname}")
            _dispatch(it, dest, may_park=False)
        while active:
            _drain_one()
    except BaseException as e:
        sched.stop.set()
        if isinstance(e, KeyboardInterrupt):
            _wind_down()
        raise
    if errs:
        if isinstance(errs[0], KeyboardInterrupt):
            _wind_down()        # 停止是某张图的线程先发现的（统一付费入口 / 重试等待里）
        raise errs[0]
    if total == 0:
        _progress(ctx, "S8", 0, 0, "张")

    missing.sort(key=lambda f: order.get(f, 0))
    out = ctx.p("07-出图", "manifest.json")
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    miss_p = ctx.p("07-出图", MISSING_ILLUSTRATIONS)
    if missing:
        miss_p.write_text(json.dumps({
            "files": missing, "updated": ledger.now_iso(),
            "说明": "这些插画出图多次失败（失败原因见 出图失败登记.jsonl），成片里由前后画面补位；"
                    "直接重跑（不带 --from）即可只为它们重新出图"}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    elif miss_p.exists():
        miss_p.unlink()
    # ST3-P26 §4.3：清掉已经收取成功的出图任务号（图已在 images/ 里）；没收到的留着，下次先续查。
    with sched.lock:
        stale = [j for j in (doc.get("pending_jobs") or [])
                 if j.get("step") == "S8" and j.get("service") in imagegen.SERVICE_KEYS
                 and j.get("note") and (img_dir / j["note"]).is_file()]
        for j in stale:
            ledger.remove_job(doc, j.get("task_id"))
        if stale:
            ledger.save_atomic(ctx.root, ledger.touch(doc))
            ctx.log(f"[S8] 清掉已收取的出图任务号 {len(stale)} 个："
                    + "、".join(f"{j.get('task_id')}（{j.get('note')}）" for j in stale))
    pst = ctx.paid_stats
    ctx.log(f"[S8] 清单 {len(items)} 项 / 新出 {made} / 已有跳过 {skipped}"
            f" / 内容变了重做 {redo['changed']} / 旧图不符重做 {redo['nomatch']}"
            f" / 无记录重做 {redo['norecord']}"
            f" / 缓存命中 {pst['hits']} / 真实付费 {pst['paid_calls']}"
            f" / 桩调用 {pst['stub_calls']}"
            + (f" / 缺图 {len(missing)}（{'、'.join(missing)}）" if missing else "") + f" -> {out}")
    out = {"jobs": doc.get("pending_jobs") or [],
           "message": (f"缺图 {len(missing)} 张：{'、'.join(missing)}" if missing else "")}
    if sched.used:
        # ST3-P27 §4.2：本次实际发过请求的出图服务（命中缓存 / 桩 / --no-paid 不记，账本里保留上一次的值）。
        out["image_service"] = "+".join(sched.used)
    return out


# --------------------------------------------------------------------------
# S9 场景构建（真跑：pipeline.scene_build）
# --------------------------------------------------------------------------


def _read_audio_dur(audio_path):
    try:
        w = wave.open(str(audio_path), "rb")
        dur = w.getnframes() / w.getframerate()
        w.close()
        return dur
    except Exception:
        return None


def _shot_image_map(ctx, shots):
    """本篇 shotImageMap：`{原图文件名: "shots/<子目录>/<原图文件名>"}`。

    素材来源是 `00-输入/图片和附件` 的**原图**（不是主仓 `public\\shots\\p12_239`）。
    文件名集合由 shots（含网格图）实际引用决定；缺一个就停下。
    """
    fns = set()
    for s in shots:
        if s.get("image"):
            fns.add(s["image"])
        for g in (s.get("grid") or {}).get("images") or []:
            if g.get("image"):
                fns.add(g["image"])
    src_dir = ctx.p("00-输入", "图片和附件")
    missing = sorted(fn for fn in fns if not (src_dir / fn).is_file())
    if missing:
        raise StepError("截图素材缺失（00-输入/图片和附件）: " + ", ".join(missing))
    sub = _asset_subdir(ctx)
    return {fn: f"shots/{sub}/{fn}" for fn in sorted(fns)}


def _shots_ts(mapping, shots):
    return "\n".join([
        "// 由 ST1-P3 引擎 S9 生成（本篇 shotImageMap + shotsData）。",
        "export const shotImageMap = " + json.dumps(mapping, ensure_ascii=False, indent=1) + ";",
        "",
        "export const shotsData = " + json.dumps(shots, ensure_ascii=False, indent=1) + ";",
        "",
    ])


def _fmt_mmss(frame, fps):
    if frame is None:
        return ""
    sec = int(frame // fps)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _fmt_mmss1(frame, fps):
    """分:秒，秒带一位小数（例如 02:39.3）。"""
    tenth = round(frame * 10 / fps)
    return f"{tenth // 600:02d}:{tenth % 600 / 10:04.1f}"


def _names_in_scene(sc):
    """一个场景里出现的原图文件名 / 要素 id（与分配器底线 3 的口径同一个函数）。"""
    from pipeline import allocate
    names = set(allocate.scene_images(sc))
    if sc.get("element_id"):
        names.add(sc["element_id"])
    return names


def _short_names(data):
    """分配器判定为「不足最短时长」的图名 / 要素 id：已登记的「时间不足」+ 底线 4 的违反。"""
    ids = {n["id"] for n in data.get("allocation_notes", []) if n["type"] == "时间不足"}
    ids |= {x["scene"] for x in (data.get("baselines") or {}).get("4") or []}
    out = set()
    for sc in data.get("scenes", []):
        if sc.get("alloc_id") in ids:
            out |= _names_in_scene(sc)
    return out


def _appearance(data):
    """每张原图 / 每个要素在场景数据里的出现明细：{名字: (总帧数, [{场景, 起, 止}])}。"""
    fps = int(data.get("fps") or 30)
    out = {}
    for sc in data.get("scenes", []):
        dur = int(sc.get("end_frame") or 0) - int(sc.get("start_frame") or 0)
        for name in _names_in_scene(sc):
            tot, det = out.get(name, (0, []))
            det.append({"场景": sc.get("type") or sc.get("eff_role") or "content",
                        "起": _fmt_mmss(sc.get("start_frame"), fps),
                        "止": _fmt_mmss(sc.get("end_frame"), fps)})
            out[name] = (tot + dur, det)
    return out


def _original_table(ctx, data, short):
    """原图覆盖核对表（`logs/原图覆盖核对表.json`）：逐张列出现次数、总时长、明细。

    ST3-P9：这里只列表。缺没缺、够不够长由分配器的底线说了算（`short` 即分配器判定的不足名单），
    不再另用一套「出现 ≥1.5 秒」的算法重新判一遍。
    """
    im_rel = ("05-截图分析" if ctx.resolved_content_type == spec.CONTENT_TYPE_SCREENSHOT
              else "06-编排")
    imap = _load_json(ctx, f"{im_rel}/image_map.json")
    fps = int(data.get("fps") or 30)
    seen = _appearance(data)
    table = []
    for e in imap.get("images", []):
        fn = Path(e.get("source_relative_path") or e.get("image_path") or "").name
        if not fn:
            continue
        dur, det = seen.get(fn, (0, []))
        table.append({
            "图名": fn,
            "截图或非截图": ("截图" if (e.get("kind") or "screenshot") == "screenshot" else "非截图"),
            "出现次数": len(det),
            "总时长秒": round(dur / fps, 2),
            "时长不足": fn in short,
            "明细": det,
        })
    return table


def _element_table(els, data, short):
    """要素覆盖核对表（`logs/要素覆盖核对表.json`）：代码/命令/表格逐个列出现次数、总时长、明细。

    ST3-P9：同上，只列表；缺失与时长不足读分配器的结论。
    """
    fps = int(data.get("fps") or 30)
    type_cn = {"code": "代码卡片", "command": "命令行窗口", "table": "表格卡片"}
    seen = _appearance(data)
    table = []
    for e in els:
        eid = e.get("id")
        dur, det = seen.get(eid, (0, []))
        table.append({
            "要素": eid,
            "类型": type_cn.get(e.get("type"), e.get("type")),
            "语言": e.get("lang") or "",
            "出现次数": len(det),
            "总时长秒": round(dur / fps, 2),
            "时长不足": eid in short,
            "时长不足原因": "本章节借完仍不够最短时长" if eid in short else "",
            "明细": det,
        })
    return table


def check_illustration_scope(labels, start_frame, end_frame, sidx):
    """ST3-P11 R5 检查：插画要画的关键词里，有多少是在插画下屏之后才第一次讲到的。

    「第一次讲到」从这张插画上屏的时候算起（它上屏之前别处提过不算数），精确到字。
    返回 (后讲到的词, 关键词总数)。上屏之后再没讲到的词不算「后讲到」。超过一半由调用方出提示。
    """
    from pipeline import allocate
    late = []
    for w in labels or []:
        f = allocate.first_mention(sidx, w, start_frame)
        if f is not None and f >= end_frame:
            late.append(w)
    return late, len(labels or [])


def _p11_tips(ctx, data, manifest_all, groups, sentences, plan):
    """ST3-P11 的提示项（不标红）：插画取材、兜底截图、占位图。"""
    tips = []
    fps = int(data.get("fps") or 30)
    try:
        sidx = _sentence_index(ctx, groups, sentences)
        rec_by_img = {PureWindowsPath(r["image"]).name: r for r in manifest_all
                      if r.get("image") and r.get("labels")}
        for sc in data.get("scenes", []):
            if sc.get("is_original") or sc.get("shot") or not sc.get("image"):
                continue
            rec = rec_by_img.get(Path(sc["image"]).name)
            if not rec:
                continue
            late, total = check_illustration_scope(rec["labels"], sc["start_frame"], sc["end_frame"], sidx)
            if total and len(late) * 2 > total:
                tips.append(f"插画取材 {rec['group_id']}（{_fmt_mmss(sc['start_frame'], fps)}～"
                            f"{_fmt_mmss(sc['end_frame'], fps)}）：要画的 {total} 个关键词里有 {len(late)} 个"
                            f"在它下屏之后才讲到（{'、'.join(late)}）")
    except Exception as e:
        ctx.log(f"[S9] 插画取材检查失败（不影响出片）: {e}")
    for fn in (plan or {}).get("orphans") or []:
        tips.append(f"截图兜底 {fn}：没有挂上任何口播组，按它在原文里的位置出现")
    ph_p = ctx.p("07-出图", "占位图.json")
    if ph_p.exists():
        try:
            used = {Path(sc["image"]).name for sc in data.get("scenes", []) if sc.get("image")}
            names = [x for x in json.loads(ph_p.read_text(encoding="utf-8")).get("files", []) if x in used]
            if names:
                tips.append(f"含占位图 {len(names)} 张（灰底「待出图」，正式出片前需要出图）：{'、'.join(names)}")
        except Exception as e:
            ctx.log(f"[S9] 占位图清单读取失败（不影响出片）: {e}")
    return tips


def _check_annotation_coverage(ctx, data=None):
    """截图标注覆盖率（提示，不标红）。ST3-P12 R1：按来源分开统计，规则兜底不再算成「有标注」。

    来源：逐句链（条目全部来自模型）/ 逐句链+规则（部分条目是规则补的）/ 规则兜底 / 只有标题 / 没有标注。
    coverage = 逐句链（含部分规则补的）的张数 ÷ 截图张数；missing = 其余的图。
    按最终送去渲染的场景数据算（S9 去掉的条目不算）；旧版镜头计划没有来源，按有没有条目算。
    """
    shots = {}
    if data is not None:
        for sc in data.get("scenes", []):
            sh = sc.get("shot")
            if sh and sh.get("type") != "grid":
                cur = shots.setdefault(sh["image"], dict(sh, items=[]))
                cur["items"] = cur["items"] + [it for it in sh.get("items") or [] if it not in cur["items"]]
    else:
        p = ctx.p("06-编排", "shots_plan.json")
        if p.exists():
            shots = {s["image"]: s for s in json.loads(p.read_text(encoding="utf-8")).get("shots", [])
                     if s.get("type") != "grid"}
    by_source, missing, rows = {}, [], []
    for fn, sh in shots.items():
        items = sh.get("items") or []
        if sh.get("annot"):
            n_rule = sum(1 for it in items if it.get("src") == "规则")
            src = ("只有标题" if (not items and sh.get("title")) else "没有标注" if not items
                   else "逐句链" if n_rule == 0 else "规则兜底" if n_rule == len(items) else "逐句链+规则")
        else:
            src = (sh.get("sidebar_source") or "本组") if items else "没有标注"
            src = {"本组": "导演链（按组）", "标签": "标签兜底"}.get(src, src)
        by_source[src] = by_source.get(src, 0) + 1
        rows.append({"image": fn, "source": src, "title": sh.get("title") or "", "items": len(items)})
        if src not in ("逐句链", "逐句链+规则", "导演链（按组）"):
            missing.append(fn)
    total = len(shots)
    return {"total": total, "by_source": by_source, "missing": missing, "shots": rows,
            "coverage": (total - len(missing)) / total if total else 1.0}


_TTS_SENT_MIN = 4          # 归一化后不足 4 个字的短句不单独判（太短，读法差一两个字就过半）
_TTS_SENT_RATIO = 0.5      # 一句话在配音里对上的字不到一半 → 判为整句漏读


def _read_number(m):
    """数字的读法（豆包的读法）：`40%`→百分之四十，`12.4`→十二点四，`100`→一百，年份逐位读。"""
    num, pct, year = m.group(1), m.group(2), m.group(3)
    if "." in num:
        a, b = num.split(".", 1)
        cn = _cn_int(a) + "点" + "".join(_CN_DIGITS[int(d)] for d in b)
    elif year and len(num) == 4:
        cn = "".join(_CN_DIGITS[int(d)] for d in num)
    else:
        cn = _cn_int(num)
    return ("百分之" if pct else "") + cn + (year or "")


def _norm_tts(s):
    """配音完整性用的归一化：数字按读法转成中文、ASCII 字母转小写、只留汉字和字母。"""
    s = re.sub(r"(\d+(?:\.\d+)?)(%?)(年?)", _read_number, s or "")
    out = []
    for ch in s:
        if ch.isascii() and ch.isalpha():
            out.append(ch.lower())
        elif "\u4e00" <= ch <= "\u9fff":
            out.append("二" if ch == "两" else ch)
    return "".join(out)


def check_tts_groups(groups, words):
    """ST3-P11 R10：配音完整性——按组、按顺序把「送去配音的文字」和「豆包字级时间戳」对齐。

    groups: 正文组 [{"group_id", "group_text"}]，按口播顺序；words: 豆包逐字流（按时间顺序的字符串）。
    每组先按 `_tts_text` 变成送去配音的写法，再把数字换成读法；全篇按顺序连起来，和配音词流做一次
    保序对齐（漏掉的句子即使在后面重复出现，也不会被对到那里去）。一句话对上的字不到一半，
    判为整句漏读。返回 [{"group_id", "text", "missing": [漏读的句子], "coverage"}]，非空即标红。
    """
    import difflib
    rows, exp = [], []
    for g in groups:
        sents = []
        for part in _split_sentences(_tts_text(g.get("group_text", ""))):
            n = _norm_tts(part)
            if n:
                sents.append((part, n))
                exp.append(n)
        rows.append((g, sents))
    exp = "".join(exp)
    hit = [False] * len(exp)
    for b in difflib.SequenceMatcher(None, exp, _norm_tts(words), autojunk=False).get_matching_blocks():
        for k in range(b.size):
            hit[b.a + k] = True
    out, at = [], 0
    for g, sents in rows:
        missing, start = [], at
        for part, n in sents:
            got = sum(hit[at:at + len(n)])
            if len(n) >= _TTS_SENT_MIN and got < len(n) * _TTS_SENT_RATIO:
                missing.append(part)
            at += len(n)
        if missing:
            out.append({"group_id": g["group_id"], "text": g.get("group_text", "")[:40],
                        "missing": missing, "coverage": round(sum(hit[start:at]) / max(1, at - start), 3)})
    return out


def _check_tts_completeness(ctx, data):
    """配音完整性：送去配音的每句话都要在豆包字级时间戳里按顺序找到（见 `check_tts_groups`）。

    返回 [{"group_id","text","missing"}]，非空即标红（退出码 1）。
    """
    sent = _load_json(ctx, "03-配音/sentences.json")
    grp = _load_json(ctx, "02-分组/groups_raw.json")
    body = [g for g in grp["groups"] if not g.get("starts_with_bracket_title")]
    words = "".join(w.get("word", "") for s in sent for w in s.get("words", []))
    return check_tts_groups(body, words)


def step_s9(ctx):
    from pipeline import orchestrate, scene_build

    ct = ctx.resolved_content_type
    screenshot = ct == spec.CONTENT_TYPE_SCREENSHOT

    grp_dir = ctx.p("02-分组")
    align_p = ctx.p("04-对齐", "alignment.json")
    sent_p = ctx.p("03-配音", "sentences.json")
    mani_p = ctx.p("07-出图", "manifest.json")
    for p in (align_p, sent_p, mani_p):
        if not p.exists():
            raise StepError(f"缺少上游产物: {p}")
    audios = ledger.expand(ctx.root, [paths.AUDIO_GLOB])
    if not audios:
        raise StepError("缺少 S3 产物: 03-配音/audio/*.wav")
    audio_name = Path(audios[0]).name
    audio_path = ctx.root / audios[0]

    out_dir = ctx.p("08-场景")
    out_dir.mkdir(parents=True, exist_ok=True)

    sentences = json.loads(sent_p.read_text(encoding="utf-8"))
    alignment = {r["group_id"]: r for r in json.loads(align_p.read_text(encoding="utf-8"))}
    manifest_all = json.loads(mani_p.read_text(encoding="utf-8"))
    # ST3-P16：S8 多次出图失败的 AI 插画（缺图）不排，它的时间由前后画面补上（分配器本来就首尾相接铺满）。
    # 只有 AI 插画会缺：原图、截图、要素卡片不经过出图。
    ill_missing = missing_illustrations(ctx.root)
    if ill_missing:
        gone = set(ill_missing)
        manifest_all = [r for r in manifest_all
                        if not (r.get("image") and r.get("result") != "原图"
                                and PureWindowsPath(r["image"]).name in gone)]
        ctx.log(f"[S9] 缺图 {len(ill_missing)} 张不排，由前后画面补位：{'、'.join(ill_missing)}")
    groups, titles, body = orchestrate.load_groups(grp_dir)
    _record_overview(ctx, "S9")

    # ST3-P2：要素清单 + 高亮对应（S5/S7 产物；老样本缺文件视为无要素）。
    els = _load_elements(ctx)
    hl_p = ctx.p("06-编排", "高亮对应.json")
    highlight_map = (json.loads(hl_p.read_text(encoding="utf-8")).get("mappings", {})
                     if hl_p.exists() else {})

    kw, indent = {}, 2
    shots = None
    plan = {}
    if screenshot:
        plan = _load_json(ctx, "06-编排/shots_plan.json")
        shots = plan["shots"]
        kw = {"shots": shots, "asset_subdir": _asset_subdir(ctx),
              "stay_covered": set(plan.get("stay_covered") or []),
              "partial_recover": plan.get("partial_recover") or {}}
        indent = 1
    # ST3-P11 R3：要出卡片的正文命令并进要素清单（截图型由 S7 定去向；理论型没有截图，全部出卡片）。
    # 旧版镜头计划没有这一栏时不出，保持原样。
    els = els + _inline_card_elements(plan.get("inline_cards", []) if screenshot
                                      else _load_inline_commands(ctx))

    # ST3-F2：原图/要素按原文位置定锚要用 image_map 与文章原文；缺了就退回「所属组内等比分布」。
    im_p = ctx.p("05-截图分析" if screenshot else "06-编排", "image_map.json")
    art_p = ctx.p("00-输入", "文章.md")
    data = scene_build.build_scenes(manifest_all, alignment, sentences, groups,
                                    audio_dur=_read_audio_dur(audio_path),
                                    audio_name=audio_name, elements=els,
                                    highlight_map=highlight_map,
                                    image_map=json.loads(im_p.read_text(encoding="utf-8")) if im_p.exists() else None,
                                    article_text=art_p.read_text(encoding="utf-8") if art_p.exists() else None,
                                    strict=False, **kw)

    (out_dir / "video_data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    # ST3-P9：五条底线（不空白/不重叠/原图齐全/最短时长/画面跟着口播走）不过**照常出片**——
    # 违反项写进登记清单和检查报告的标红区，本次运行以退出码 1 结束（接口约定 v0.5.1：
    # 退出码 1 = 检查标红、已出片、不是失败）。回归测试仍然严格，见 tests/test_baselines.py。
    from pipeline import allocate
    fps = int(data.get("fps") or 30)
    base = data["baselines"]
    violations = allocate.describe_violations(base, fps)
    alloc_notes = data.get("allocation_notes", [])
    logs_dir = ctx.root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "时间分配登记.json").write_text(
        json.dumps({"baselines": base, "violations": [v["text"] for v in violations],
                    "notes": alloc_notes}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    for v in violations:
        ctx.log(f"[S9] 标红 {v['text']}")

    if screenshot:
        # 镜头内部时间已由分配器回写进场景，渲染侧数据一律取回写后的这一份。
        shots = [sc["shot"] for sc in data["scenes"] if sc.get("shot")]
        mapping = _shot_image_map(ctx, shots)
        (out_dir / "video_data.ts").write_text(
            "export const videoData = " + json.dumps(data, ensure_ascii=False) + ";\n",
            encoding="utf-8")
        (out_dir / "shots_data.ts").write_text(_shots_ts(mapping, shots), encoding="utf-8")
        (out_dir / "shotImageMap.json").write_text(
            json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")
        n_ss = sum(1 for s in data["scenes"] if s.get("type") == "screenshot")
        ctx.log(f"[S9] 截图镜头 {n_ss} / shotImageMap {len(mapping)} 条"
                f" / 子目录 {_asset_subdir(ctx)}")

    ctx.log(f"[S9] 场景 {len(data['scenes'])} / 字幕行 {len(data['subtitle_lines'])}"
            f" / 内容 {data['content_frames']} 帧 / 总 {data['total_frames']} 帧"
            f" / 音频 {audio_name} -> {out_dir / 'video_data.json'}")

    # ST3-P9 统一口径：时长、覆盖、空白、重叠四类检查一律读分配器的结论（data["baselines"] 与
    # 登记清单），这里不再另算。核对表只是把同一份结论按图 / 按要素摊开，方便人看。
    el_ids = {e.get("id") for e in els}
    short = _short_names(data)
    table = _original_table(ctx, data, short)
    etable = _element_table(els, data, short)
    blanks = [{"start_frame": x["start_frame"], "end_frame": x["end_frame"] - 1,
               "frames": x["end_frame"] - x["start_frame"]} for x in base["1"]]
    overlaps = [({"start_frame": x["start_frame"], "end_frame": x["end_frame"] - 1,
                  "frames": x["end_frame"] - x["start_frame"]} if "start_frame" in x else x)
                for x in base["2"]]
    # ST3-P3d 修复一：配音完整性硬检查——TTS 输入缺字标红（与画面分配无关，仍在这里查）。
    tts_missing = _check_tts_completeness(ctx, data)
    (logs_dir / "原图覆盖核对表.json").write_text(
        json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    (logs_dir / "要素覆盖核对表.json").write_text(
        json.dumps(etable, ensure_ascii=False, indent=1), encoding="utf-8")
    (logs_dir / "空白帧核对表.json").write_text(
        json.dumps(blanks, ensure_ascii=False, indent=1), encoding="utf-8")
    (logs_dir / "重叠核对表.json").write_text(
        json.dumps(overlaps, ensure_ascii=False, indent=1), encoding="utf-8")
    (logs_dir / "配音完整性核对表.json").write_text(
        json.dumps(tts_missing, ensure_ascii=False, indent=1), encoding="utf-8")
    # ST3-P3d 第5项：截图标注覆盖率（提示，不标红）。
    ann_cov = _check_annotation_coverage(ctx, data if screenshot else None)
    (logs_dir / "截图标注覆盖率.json").write_text(
        json.dumps(ann_cov, ensure_ascii=False, indent=1), encoding="utf-8")
    if ann_cov["total"]:
        ctx.log(f"[S9] 截图标注覆盖率（逐句链）{ann_cov['coverage']:.0%}，按来源：{ann_cov.get('by_source')}"
                + (f"；不是逐句链的 {len(ann_cov['missing'])} 张：{', '.join(ann_cov['missing'])}"
                   if ann_cov["missing"] else ""))
    # ST3-P12 R1：标注登记——被去掉的条目（句子不在屏、超出左栏上限）、停留 ≥ 10 秒的截图里
    # 最长多久没有新内容（目标 ≤ 8 秒）。ST3-P12b：超过 8 秒的空档不再插画，只在检查报告里提示。
    gap_tips = []
    if screenshot:
        from pipeline import shot_annotate
        stays = shot_annotate.stay_rows(data["scenes"])
        long_stays = [dict(r, over_8s=r["longest_gap"] > shot_annotate.GAP_MAX_FRAMES)
                      for r in stays if r["dur"] >= shot_annotate.LONG_STAY_FRAMES]
        gaps_over = [{"image": r["image"], "from_frame": a, "to_frame": b, "sec": round((b - a) / fps, 1)}
                     for r in long_stays for a, b in r["gaps"] if b - a > shot_annotate.GAP_MAX_FRAMES]
        gap_tips = [f"标注空档 {g['image']}：{_fmt_mmss1(g['from_frame'], fps)}～{_fmt_mmss1(g['to_frame'], fps)}"
                    f" 有 {g['sec']:.1f} 秒没有新内容（停留 ≥ 10 秒的截图，目标 ≤ 8 秒）" for g in gaps_over]
        (logs_dir / "标注登记.json").write_text(json.dumps({
            "dropped": data.get("annotation_drops", []), "over": plan.get("annot_over", []),
            "gaps_over_8s": gaps_over,
            "long_stays": long_stays, "commands": plan.get("inline_log", [])},
            ensure_ascii=False, indent=1), encoding="utf-8")
        for d in data.get("annotation_drops", []):
            ctx.log(f"[S9] 标注去掉 {d['image']}「{d['text']}」：{d['reason']}")
        bad = [r for r in long_stays if r["over_8s"]]
        if bad:
            ctx.log(f"[S9] 停留 ≥ 10 秒的截图 {len(long_stays)} 张，其中 {len(bad)} 张有超过 8 秒没有新内容："
                    + "、".join(f"{r['image']}（{r['longest_gap'] / fps:.1f} 秒）" for r in bad))
    # 底线违反 + 配音缺字并入 original_coverage.missing：cli 收尾见它非空就把退出码记为 1。
    tts_tags = [f"配音漏读 {m['group_id']}（{len(m['missing'])} 句）" for m in tts_missing]
    cov = {
        "total": len(table),
        "missing": [v["tag"] for v in violations] + tts_tags,
        "table": table,
        "image_missing": [x for x in base["3"] if x not in el_ids],
        "element_coverage": {"total": len(etable),
                             "missing": [x for x in base["3"] if x in el_ids], "table": etable},
        "blank_frames": blanks,
        "overlaps": overlaps,
        "tts_missing": tts_missing,
        "baseline_violations": [v["text"] for v in violations],
        # 借时长仍不够的登记（超时 = 晚于锚点句 2 秒以上；时间不足 = 少于最短时长），进检查报告的提示。
        "alloc_notes": [n for n in alloc_notes if n["type"] in ("超时", "时间不足", "改用网格")
                        or (n["type"] == "标题卡延长" and n.get("long"))],
        # ST3-P11：R6 提前出现核对、R5 插画取材检查、R11 兜底截图、占位图（都只是提示）。
        "early": allocate.early_items(data),
        "late": allocate.late_items(data),          # R101 C：推后出现核对（与提前出现对称）
        "extra_tips": _p11_tips(ctx, data, manifest_all, groups, sentences, plan) + gap_tips,
        # ST3-P16：出图多次失败、成片里由前后画面补位的插画（标红，退出码 1；重跑补出后消失）。
        "illustration_missing": ill_missing,
    }
    (logs_dir / "提前出现核对表.json").write_text(
        json.dumps(cov["early"], ensure_ascii=False, indent=1), encoding="utf-8")
    (logs_dir / "推后出现核对表.json").write_text(
        json.dumps(cov["late"], ensure_ascii=False, indent=1), encoding="utf-8")
    for n in cov["alloc_notes"]:
        ctx.log(f"[S9] 时间分配登记 [{n['type']}] {n['id']}（锚点 {_fmt_mmss(n['anchor'], 30)}）{n.get('reason', '')}")
    ctx.original_coverage = cov
    if ill_missing:
        ctx.log(f"[S9] 检查标红 {illustration_missing_text(ill_missing)} -> 照常出片，退出码将记为 1")
    if cov["missing"]:
        ctx.log(f"[S9] 检查标红 {len(cov['missing'])} 项（{', '.join(cov['missing'])}）"
                f"-> 照常出片，退出码将记为 1")
    else:
        ctx.log(f"[S9] 五条底线全部通过：原图 {cov['total']} 张、要素 {len(etable)} 个、"
                f"空白 0、重叠 0；配音完整")
    # 检查报告在这里就写一版（只含场景数据能定的检查），S10 出片后刷新。
    # 这样渲染失败时留在文件夹里的也是本次的结论，不会是上一轮的旧报告。
    try:
        _make_check_report(ctx, data, rendered=False)
    except Exception as e:
        ctx.log(f"[S9] 检查报告生成失败（不影响出片）: {e}")
    return {"original_coverage": cov}


# --------------------------------------------------------------------------
# S10 成片渲染（真跑：pipeline.render_mux，工程隔离见 render_project）
# --------------------------------------------------------------------------


_RENDER_WRAPPER = (
    "import sys\n"
    "from pipeline import render_mux\n"
    "render_mux.render_mux(sys.argv[1], sys.argv[2], sys.argv[3], cwd=sys.argv[4],\n"
    "                      concurrency=int(sys.argv[5]), keep_temp=False)\n"
)


def _kill_tree(pid):
    if os.name != "nt":
        # ST3-P9：非 Windows 没有 taskkill；渲染子进程与引擎同一进程组，直接结束它即可。
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, text=True)


LOOPBACK_PRELOAD = Path(__file__).with_name("loopback_only.cjs")


def render_env():
    """渲染子进程（Node / Remotion）的环境：去掉六个密钥变量（ST3-P22 G18），渲染用不到它们。

    R101 I：NODE_OPTIONS 加上 `--require loopback_only.cjs`，渲染里的 Node 只在 127.0.0.1 上监听
    （Remotion 默认绑所有网卡，Windows 第一次出片会弹防火墙提示）。原有的 NODE_OPTIONS 保留在前面。
    """
    env = dict(os.environ)
    for k in spec.SECRET_ENV_VARS:
        env.pop(k, None)
    if LOOPBACK_PRELOAD.is_file():
        # NODE_OPTIONS 里双引号内的反斜杠是转义符：Windows 路径的 \ 要写成 \\
        quoted = str(LOOPBACK_PRELOAD.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        req = f'--require "{quoted}"'
        prev = (env.get("NODE_OPTIONS") or "").strip()
        if req not in prev:
            env["NODE_OPTIONS"] = f"{prev} {req}".strip()
    return env


# Remotion 4 的进度行（非终端输出时一行一条或用 \r 覆盖）："Rendering frames ━━ 120/3600" /
# "Rendered 120/3600" / "Encoding video ━━ 120/3600" / "Encoded 120/3600"。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_RENDER_PROG_RE = re.compile(r"Render(?:ing|ed)(?: frames)?\D*?(\d+)\s*/\s*(\d+)")
_ENCODE_PROG_RE = re.compile(r"Encod(?:ing|ed)(?: video| audio)?\D*?(\d+)\s*/\s*(\d+)")
_RENDER_SHARE = 90          # 渲染帧占进度的 90%，编码 9%，最后 1% 留给 ffmpeg 重封装


def render_percent(line):
    """从一行渲染输出里认出进度，返回 0～99 的整数百分比；不是进度行返回 None。"""
    t = _ANSI_RE.sub("", line or "")
    m = _RENDER_PROG_RE.search(t)
    if m and int(m.group(2)):
        return min(_RENDER_SHARE, int(_RENDER_SHARE * int(m.group(1)) / int(m.group(2))))
    m = _ENCODE_PROG_RE.search(t)
    if m and int(m.group(2)):
        return min(99, _RENDER_SHARE + int(9 * int(m.group(1)) / int(m.group(2))))
    if "[render_mux] 步骤2" in t:
        return 99
    return None


def _pump_render_output(stream, state):
    """读渲染子进程的输出（按 \r / \n 切行）：进度行只记最新百分比（日志里每 10 秒留一条），
    其他行原样转到 stderr + 日志。在后台线程里跑。"""
    import codecs
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    buf = ""
    last_logged = 0.0

    def handle(line):
        nonlocal last_logged
        if not line.strip():
            return
        pct = render_percent(line)
        if pct is not None:
            state["pct"] = max(state.get("pct", 0), pct)
            if time.time() - last_logged < 10:
                return
            last_logged = time.time()
        try:
            sys.stdout.write(_ANSI_RE.sub("", line) + "\n")
            sys.stdout.flush()
        except Exception:
            pass

    while True:
        chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
        if not chunk:
            break
        buf += dec.decode(chunk)
        parts = re.split(r"[\r\n]", buf)
        buf = parts.pop()
        for ln in parts:
            handle(ln)
    buf += dec.decode(b"", final=True)
    handle(buf)


def _run_render(rp, out_mp4, concurrency=4, progress=None, argv=None):
    """在子进程里跑 render_mux，主线程轮询等待——让中断（SIGINT/SIGBREAK）真的能被接住。

    不能直接在主线程里调 render_mux：它内部是 `subprocess.run(..., shell=True)` 的不可中断
    等待。引擎装了 SIGBREAK 处理器以后，Python 把 CTRL_BREAK 记下来留给主线程执行，而主线程
    正卡在 C 调用里出不来，事件就被吞掉（实测见 ST1-P2-理论型装线\\_probe_ctrlbreak8.py 的
    U/V 两例：阻塞式等待收不到、轮询式 0.2s 内就退出 7）。轮询每 0.2s 回到一次解释器，信号
    立刻生效；中断时顺手把渲染进程树收掉，不留孤儿。

    ST3-P23 §3.6：子进程的 stdout / stderr 接到管道里读（不再借 fd 1 → fd 2 的重定向，管道本身就
    不会混进事件流），从 Remotion 的进度行算百分比；`progress(pct)` 在主线程的轮询里调用。
    """
    import threading
    argv = argv or [sys.executable, "-c", _RENDER_WRAPPER, "src/index-r125.ts", "R125-字幕调整",
                    str(out_mp4), str(rp), str(concurrency)]
    env = render_env()
    env["PYTHONDONTWRITEBYTECODE"] = "1"   # 子进程别往主仓写 .pyc
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(_REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    state = {"pct": 0}
    sys.stdout.flush()
    p = subprocess.Popen(argv, cwd=str(_REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    reader = threading.Thread(target=_pump_render_output, args=(p.stdout, state), daemon=True)
    reader.start()
    try:
        while p.poll() is None:
            time.sleep(0.2)
            if progress is not None:
                progress(state["pct"])
    except KeyboardInterrupt:
        _kill_tree(p.pid)
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
        raise
    reader.join(timeout=10)
    if p.returncode != 0:
        raise StepError(f"渲染失败（render_mux 退出码 {p.returncode}）", kind=errors.KIND_RENDER)


def _find_asset(ctx, name):
    """按文件名在本篇工作文件夹里定位素材，返回相对路径；找不到返回 None。"""
    for sub in paths.ASSET_DIRS:
        rel = f"{sub}/{name}"
        if (ctx.root / rel).is_file():
            return rel
    return None


def _shot_element_assets(ctx, data):
    """截图型：镜头子树里被**元素级**引用的截图（网格里除首图外的每一张等）。

    `ScreenshotScene` 是按 `shotImageMap[el.image]` 查元素级图片的，而这些图只在
    `shot.gid_images` / `shot.grid` 里出现、**不在** `scene.image` 上——只按 `scene.image`
    收集会漏（ST1-P3 D 首跑 `网络问题.png` 就是这么漏掉、渲染报"图片无法解码"）。
    返回 [(源相对路径, public 下相对路径)]，源优先取 `00-输入/图片和附件`。
    """
    out, seen = [], set()
    img_dir = ctx.p("00-输入", "图片和附件")
    sub = _asset_subdir(ctx)

    def add(raw):
        fn = Path(raw).name
        if not fn or fn in seen:
            return
        seen.add(fn)
        if (img_dir / fn).is_file():
            out.append((f"00-输入/图片和附件/{fn}", f"shots/{sub}/{fn}"))
            return
        if ctx.p("07-出图", "images", fn).is_file():
            out.append((f"07-出图/images/{fn}", f"{sub}/{fn}"))
            return
        raise StepError(f"镜头元素引用的素材找不到: {fn}", kind=errors.KIND_RENDER)

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "gid_images" and isinstance(v, list):
                    for x in v:
                        if isinstance(x, str):
                            add(x)
                    continue
                if k == "image" and isinstance(v, str):
                    add(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for s in data.get("scenes", []):
        if s.get("type") == "screenshot":
            walk(s.get("shot") or {})
    return out


def _scene_assets(ctx, data):
    """从场景数据推本篇素材清单（A2）：音频 + 每个镜头引用的插画/原图。

    **只依据场景数据**，不再按目录 glob 扫（ST1-P2 那样会把没被引用的图也塞进来）。
    缺任何一个源文件就报错并列出，绝不回退到主仓共享 `public\\`。

    落位目标 = 场景数据里的 staticFile 引用**原样**：音频是文件名（public 根），
    插画是 `<子目录>/<文件名>`，截图是 `shots/<子目录>/<文件名>`。源文件按文件名在
    本篇工作文件夹里定位（03-配音/audio、07-出图/images、00-输入/图片和附件）。
    """
    refs, seen = [], set()
    for r in [data.get("audio")] + [s.get("image") for s in data.get("scenes", [])]:
        if r and r not in seen:
            seen.add(r)
            refs.append(r)
    assets, missing = [], []
    for ref in refs:
        rel = _find_asset(ctx, Path(ref).name)
        if rel is None:
            missing.append(ref)
        else:
            assets.append((rel, ref))
    if missing:
        raise StepError("渲染素材缺失（不从主仓 public 兜底）: " + ", ".join(sorted(missing)),
                        kind=errors.KIND_RENDER)
    # 截图型：再补上元素级（网格）引用的截图，它们不在 scene.image 里
    if ctx.resolved_content_type == spec.CONTENT_TYPE_SCREENSHOT:
        have = {dst for _, dst in assets}
        for rel, dst in _shot_element_assets(ctx, data):
            if dst not in have:
                have.add(dst)
                assets.append((rel, dst))
    return assets


def _make_contact_sheet(ctx, mp4):
    """联系表（ST1-P5a G）：成片每 3 秒抽一帧，12 帧一张（3 列 × 4 行），帧左上角标分:秒。

    产物落 `10-报告\\联系表-01.jpg …`。失败抛异常，由调用方捕获记日志（不影响出片，
    也不计入成片交付）。
    """
    import shutil as _sh
    import subprocess as _sp
    import tempfile
    from PIL import Image, ImageDraw, ImageFont

    STEP = 3  # 秒
    COLS, ROWS = 3, 4
    CELL_W, CELL_H = 480, 270

    r = _sp.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(mp4)], capture_output=True, text=True, timeout=60)
    dur = float(r.stdout.strip())

    font = None
    for f in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/msyh.ttc"):
        if os.path.exists(f):
            try:
                font = ImageFont.truetype(f, 20)
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()

    out_dir = ctx.p("10-报告")
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="contact_")
    try:
        secs = list(range(0, int(dur) + 1, STEP))
        frames = []
        for s in secs:
            fp = os.path.join(tmp, f"f{s:05d}.jpg")
            _sp.run(["ffmpeg", "-y", "-v", "error", "-ss", str(s), "-i", str(mp4),
                     "-frames:v", "1", "-vf", f"scale={CELL_W}:{CELL_H}", fp],
                    check=True, capture_output=True)
            frames.append((s, fp))
        sheet_no = 1
        for k in range(0, len(frames), COLS * ROWS):
            chunk = frames[k:k + COLS * ROWS]
            sheet = Image.new("RGB", (COLS * CELL_W, ROWS * CELL_H), (0, 0, 0))
            for idx, (s, fp) in enumerate(chunk):
                with Image.open(fp) as cell_img:
                    cell = cell_img.convert("RGB")
                d = ImageDraw.Draw(cell)
                d.rectangle([0, 0, 210, 30], fill=(0, 0, 0))
                d.text((6, 4), f"{s // 60:02d}:{s % 60:02d}", fill=(255, 255, 255), font=font)
                rr, cc = idx // COLS, idx % COLS
                sheet.paste(cell, (cc * CELL_W, rr * CELL_H))
            out = out_dir / f"联系表-{sheet_no:02d}.jpg"
            sheet.save(out, quality=90)
            sheet_no += 1
        ctx.log(f"[S10] 联系表 {sheet_no - 1} 张 -> {out_dir}")
    finally:
        _sh.rmtree(tmp, ignore_errors=True)


# ST3-P23 §3.8：检查结果的结构化版本（10-报告/检查结果.json）。结论与检查报告.md 第一行一一对应。
CHECK_JSON_REL = "10-报告/检查结果.json"
CHECK_VERDICT = {"通过": "pass", "有提示": "review", "标红": "fail"}
_MMSS_RE = re.compile(r"(?<![\d:])(\d{2,}:\d{2})(?:\.\d)?(?![\d:])")


def _mmss_in(text):
    """文字里第一个"分:秒"时间点（定位到成片的时间）；没有返回 None。"""
    m = _MMSS_RE.search(text or "")
    return m.group(1) if m else None


def _check_item(level, kind, message, time_=None, detail=None):
    return {"level": level, "kind": kind, "message": message, "time": time_, "detail_file": detail}


def write_check_json(root, obj):
    """原子写 `10-报告/检查结果.json`（先写 .tmp 再替换）。"""
    out = Path(root) / CHECK_JSON_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)
    return out


def _timeline_checks(ctx, data):
    """ST3-P28 §3.4：时间线检查（全部标红，kind=timeline_broken）。

    返回 (reds, notes)：reds 是 [(中文消息, 分:秒, 明细文件), ...]；notes 给检查报告
    "时间线检查"一节用。第 6 项直接用 sentences.json 的句子时间，不经过任何对齐产物。
    """
    from pipeline import align_global

    fps = int(data.get("fps") or 30)
    reds, notes = [], []
    scenes = data.get("scenes") or []
    subs = data.get("subtitle_lines") or []
    chapters = data.get("chapters") or []

    def label(sc):
        gid = sc.get("group_id") or "?"
        typ = sc.get("type")
        return f"{gid}（{typ}）" if typ else gid

    # 1. 除第一个场景外，有场景起点在第 0 帧
    bad0 = [sc for i, sc in enumerate(scenes) if i > 0 and int(sc.get("start_frame") or 0) == 0]
    for sc in bad0:
        reds.append((f"时间线：{label(sc)} 在第 0 帧开始（00:00）", "00:00", None))
    notes.append(f"场景起点：{'通过' if not bad0 else f'{len(bad0)} 个场景在第 0 帧'}")

    # 2. 单个场景超过 120 秒
    long_sc = []
    for sc in scenes:
        st, en = int(sc.get("start_frame") or 0), int(sc.get("end_frame") or 0)
        dur = (en - st) / fps
        if dur > 120:
            long_sc.append((sc, st, en, dur))
    for sc, st, en, dur in long_sc:
        reds.append((f"时间线：{label(sc)} 从 {_fmt_mmss(st, fps)} 持续 {dur:.1f} 秒，"
                     f"超过 120 秒", _fmt_mmss(st, fps), None))
    notes.append(f"场景时长：{'通过' if not long_sc else f'{len(long_sc)} 个场景超过 120 秒'}")

    # 3. 字幕某一行显示超过 15 秒
    long_sub = []
    for li, ln in enumerate(subs):
        st, en = int(ln.get("start_frame") or 0), int(ln.get("end_frame") or 0)
        dur = (en - st) / fps
        if dur > 15:
            text = "".join(c.get("c") or "" for c in ln.get("chars") or [])[:20]
            long_sub.append((li, st, dur, text))
    for li, st, dur, text in long_sub[:10]:
        reds.append((f"时间线：字幕第 {li + 1} 行从 {_fmt_mmss(st, fps)} 起显示 {dur:.1f} 秒，"
                     f"超过 15 秒（{text}）", _fmt_mmss(st, fps), None))
    if len(long_sub) > 10:
        reds.append((f"时间线：另有 {len(long_sub) - 10} 行字幕显示超过 15 秒", "", None))
    notes.append(f"字幕时长：{'通过' if not long_sub else f'{len(long_sub)} 行超过 15 秒'}")

    # 4. 有起止都为 0 的字幕行
    zero = [li for li, ln in enumerate(subs)
            if int(ln.get("start_frame") or 0) == 0 and int(ln.get("end_frame") or 0) == 0]
    if zero:
        reds.append((f"时间线：{len(zero)} 行字幕起止都是 00:00（第 {zero[0] + 1}～{zero[-1] + 1} 行）",
                     "00:00", None))
    notes.append(f"字幕 0/0：{'通过' if not zero else f'{len(zero)} 行'}")

    # 5. 进度条章节起点不递增，或章节时长 ≤ 0
    bad_ch = []
    prev_start = None
    for ch in chapters:
        st, en = ch.get("start_time"), ch.get("end_time")
        if st is None or en is None:
            bad_ch.append((ch, "缺少时间"))
            continue
        if prev_start is not None and st < prev_start - 1e-6:
            bad_ch.append((ch, "起点不递增"))
        if en - st <= 0:
            bad_ch.append((ch, "时长不大于 0"))
        prev_start = st
    for ch, why in bad_ch:
        st = ch.get("start_time") or 0
        reds.append((f"时间线：进度条章节「{ch.get('name') or '?'}」{why}"
                     f"（{_fmt_mmss(int(st * fps), fps)}～"
                     f"{_fmt_mmss(int((ch.get('end_time') or 0) * fps), fps)}）",
                     _fmt_mmss(int(st * fps), fps), None))
    notes.append(f"进度条章节：{'通过' if not bad_ch else f'{len(bad_ch)} 处异常'}")

    # 6. 独立对照：sentences.json 的句子时间 vs 该句开头几个字所在字幕行的起点
    ents = []
    try:
        sentences = _load_json(ctx, "03-配音/sentences.json")
    except Exception:
        sentences = []
    sub_norm, line_of, line_start = [], [], []
    for li, ln in enumerate(subs):
        line_start.append(int(ln.get("start_frame") or 0))
        for c in ln.get("chars") or []:
            for nc in align_global.normalize_char(c.get("c") or ""):
                sub_norm.append(nc)
                line_of.append(li)
    joined = "".join(sub_norm)
    checked = 0
    for s in sentences:
        head = align_global.normalize(s.get("text") or "")
        st = s.get("startTime")
        if len(head) < 4 or st is None:
            continue
        # 全局找出现位置，不用单调游标：前一句一旦匹配错，游标会被推过本句真正
        # 位置，后面的句子只能一错到底（P27D 那类错位正是这样漏掉的）。
        # 同一开头可能在文中重复（如"网页端 Claude…"），取字幕行起点离本句
        # 配音时间最近的那一处；开头尽量取长（12→8→6→4 字）以减少撞车。
        hits = []
        for n in (12, 8, 6, 4):
            cut = head[:n]
            if len(cut) < 4:
                continue
            hits = []
            pos = 0
            while True:
                k = joined.find(cut, pos)
                if k < 0:
                    break
                hits.append(k)
                pos = k + 1
                if len(hits) > 200:
                    break
            if hits:
                break
        if not hits:
            continue
        li = min((line_of[k] for k in hits),
                 key=lambda x: abs(line_start[x] / fps - st))
        checked += 1
        line_st = line_start[li] / fps
        diff = abs(line_st - st)
        if diff > 2.0:
            ents.append({"sentence": (s.get("text") or "")[:40], "sentence_start": st,
                         "line_no": li + 1, "line_start": line_st, "diff": diff})
    for e in ents[:10]:
        reds.append((f"时间线：配音句子与字幕行起点偏差 {e['diff']:.1f} 秒"
                     f"（句子「{e['sentence'][:16]}」在 {_fmt_mmss(int(e['sentence_start'] * fps), fps)}，"
                     f"所在字幕第 {e['line_no']} 行从 {_fmt_mmss(int(e['line_start'] * fps), fps)} 开始）",
                     _fmt_mmss(int(e["sentence_start"] * fps), fps), None))
    if len(ents) > 10:
        reds.append((f"时间线：配音句子与字幕行起点偏差超过 2 秒共 {len(ents)} 条（上面列前 10 条）",
                     _fmt_mmss(int(ents[0]["sentence_start"] * fps), fps), None))
    notes.append("配音-字幕独立对照：" + ("通过" if not ents else
                                        f"{len(ents)} 条偏差超过 2 秒（核对 {checked} 句）"))
    return reds, notes


def _make_check_report(ctx, data, rendered=True):
    """检查报告（`10-报告/检查报告.md`）。S9 写一版，S10 出片后刷新。失败不影响出片。

    第一行是总体结论（通过 / 标红 / 有提示），有标红时紧接着列标红项，时间点一律分:秒。
    ST3-P9 统一口径——同一件事只有一种算法：
    - 空白、重叠、原图与要素是否齐全、最短时长、是否跟着口播：读分配器的五条底线
      （`original_coverage.baseline_violations`，标红）和登记清单（`alloc_notes`，提示）；
    - 配音缺字（标红）：S9 的配音完整性检查；
    - 要素未上屏（标红）：S10 的成片抽帧核对；
    - 成片声音与本次配音不符（标红）：S10 出片后的声音核对（ST3-P14）；
    - 口播仍念命令（标红）：S1 的命令检查；
    - 音画差 >1 秒（提示）：音频时长对内容帧数。
    ST3-P14：已过期的检查结果（上游在它之后重跑过，见 cli `_mark_stale_checks`）不算标红，只写明已过期。
    """
    import wave
    reds, tips, checks = [], [], []
    items = []          # ST3-P23 §3.8：同样的结论按条写进 10-报告/检查结果.json

    def red(text, kind, time_=None, detail=None):
        reds.append(text)
        items.append(_check_item("red", kind, text, time_, detail))

    def tip(text, kind, time_=None, detail=None):
        tips.append(text)
        items.append(_check_item("notice", kind, text, time_, detail))

    stale = {}

    # ST3-P25：导览候选不止一处（S7/S9 只认了最靠前的一个），提示。没有记录文件就没有这一条。
    ov_p = Path(ctx.root) / OVERVIEW_LOG_REL
    if ov_p.is_file():
        tip(overview_notice_text(json.loads(ov_p.read_text(encoding="utf-8"))), "overview_ambiguous",
            detail=OVERVIEW_LOG_REL)

    doc = getattr(ctx, "ledger_doc", None) or {}
    cov = getattr(ctx, "original_coverage", None) or doc.get("original_coverage")
    if cov and cov.get("stale"):
        checks.append("画面检查：结果已过期（" + _stale_text(cov["stale"]) + "），需要跑到 S9 才会重新判定")
        stale["original_coverage"] = cov["stale"]
        cov = None
    rc = (doc.get("render_checks") or {}) if rendered else {}
    if rc.get("stale"):
        stale["render_checks"] = rc["stale"]
        rc = {}
    if cov:
        # ST3-P14 以前的账本把成片抽帧的未上屏并在这里；新账本放在 render_checks。
        frame_missing = cov.get("frame_missing") or []
        viol = cov.get("baseline_violations")
        if viol is None:
            # 旧版 S9 留在账本里的结果（只有缺失清单，没有底线明细）：原样列出。
            for m in cov.get("missing") or []:
                if m not in frame_missing:
                    red(f"缺失：{m}", "missing", detail="logs/原图覆盖核对表.json")
        else:
            for v in viol:
                red(v, "baseline_violation", _mmss_in(v), "logs/时间分配登记.json")
            for m in cov.get("tts_missing") or []:
                red(f"配音漏读：{m['group_id']}（{'／'.join(m['missing'])}）", "tts_missing",
                    detail="logs/配音完整性核对表.json")
        if cov.get("illustration_missing"):
            red(illustration_missing_text(cov["illustration_missing"]), "illustration_missing",
                detail="07-出图/缺图.json")
        checks.append("画面底线：五条全部通过" if not cov.get("baseline_violations")
                      else f"画面底线：有 {len(cov['baseline_violations'])} 处未过（见标红）")
        total = cov.get("total", 0)
        img_missing = cov.get("image_missing", [])
        checks.append(f"原图覆盖：{total - len(img_missing)}/{total} 出现")
        ecov = cov.get("element_coverage") or {}
        etotal = ecov.get("total", 0)
        if etotal:
            checks.append(f"要素覆盖：{etotal - len(ecov.get('missing', []))}/{etotal} 出现")
        # 时间分配登记（同章节借时长后仍超时/不足/改用网格），提示。
        for n in cov.get("alloc_notes") or []:
            tip(f"时间分配 [{n['type']}] {n['id']}（锚点 {_fmt_mmss(n['anchor'], 30)}）：{n.get('reason', '')}",
                "time_allocation", _fmt_mmss(n.get("anchor"), 30) or None, "logs/时间分配登记.json")
        if "early" in cov:
            early = cov.get("early") or []
            checks.append(f"提前出现：比锚点句早超过 6 秒的画面 {len(early)} 处")
            for x in early:
                tip(f"提前出现 {x['scene']}：比{x.get('basis', '锚点句')}（{_fmt_mmss(x['anchor'], 30)}）"
                    f"早 {x['early'] / 30:.1f} 秒",
                    "early_appearance", _fmt_mmss(x.get("anchor"), 30) or None, "logs/提前出现核对表.json")
        if "late" in cov:
            late = cov.get("late") or []
            checks.append(f"推后出现：比对应口播句晚超过 6 秒的画面 {len(late)} 处")
            for x in late:
                tip(f"推后出现 {x['scene']}：比{x.get('basis', '锚点句')}（{_fmt_mmss(x['anchor'], 30)}）"
                    f"晚 {x['late'] / 30:.1f} 秒",
                    "late_appearance", _fmt_mmss(x.get("anchor"), 30) or None, "logs/推后出现核对表.json")
        for t in cov.get("extra_tips") or []:
            gap = t.startswith("标注空档")
            tip(t, "annotation_gap" if gap else "layout_tip", _mmss_in(t),
                "logs/标注登记.json" if gap else None)
        # ST3-P2c：画面级要素上屏核对（frame_check），未上屏标红（退出码 1）。
        frame_missing = list(dict.fromkeys(frame_missing + (rc.get("frame_missing") or [])))
        if frame_missing:
            red(f"要素未上屏 {len(frame_missing)} 个：{'、'.join(frame_missing)}", "element_not_on_screen",
                detail="logs/要素覆盖核对表.json")
        for e in ecov.get("table", []):
            fc = e.get("frame_check")
            if fc and not fc.get("上屏"):
                tip(f"要素 {e['要素']}({e['类型']}) 画面级未上屏（{fc.get('命中')}/{fc.get('抽帧')} 帧命中）",
                    "element_frame_check", None, "logs/要素覆盖核对表.json")
    still_cmds = doc.get("command_still_hits") or []
    if still_cmds:
        red(f"口播仍念命令 {len(still_cmds)} 句", "narration_command", detail="01-口播/命令重写记录.json")

    fps = int(data.get("fps") or 30)
    # ST3-P2b 步骤5：音画差用 content_frames（不含收尾黑场 BLACK_BUFFER_SEC），
    # 避免每篇都误报「音画差约 3 秒」。content_frames 由 scene_build 按音频时长+黑场分开记录。
    content_frames = int(data.get("content_frames") or data.get("total_frames") or 0)
    video_dur = content_frames / fps
    audio_dur = None
    try:
        audios = ledger.expand(ctx.root, [paths.AUDIO_GLOB])
        if audios:
            w = wave.open(str(ctx.root / audios[0]), "rb")
            audio_dur = w.getnframes() / w.getframerate()
            w.close()
    except Exception:
        pass
    if audio_dur is not None:
        diff = abs(audio_dur - video_dur)
        checks.append(f"音画差：音频 {audio_dur:.2f}s vs 视频 {video_dur:.2f}s（差 {diff:.2f}s）")
        if diff > 1.0:
            tip(f"音画差 {diff:.2f}s 超 1s 阈值", "av_duration_diff")
    au = rc.get("audio")
    if au:
        for r in au.get("reds") or []:
            red(r, "audio_mismatch", _mmss_in(r))
        checks.append(f"成片声音：音轨 {au['stream_sec']:.2f}s，应为 {au['expected_sec']:.2f}s"
                      f"（本次配音 {au['narration_sec']:.2f}s + 收尾 {au['tail_sec']:.1f}s）；"
                      f"有声部分到 {au['mp4_audible_end']:.2f}s 结束，本次配音到 {au['narration_audible_end']:.2f}s")
    elif rc.get("audio_error"):
        tip(f"成片声音核对没有做成：{rc['audio_error']}", "audio_check_failed")
    checks.append("成片：已渲染" if rendered
                  else "成片：尚未渲染（本报告由场景构建生成，只含画面分配与配音的检查；渲染完成后刷新）")

    # ST3-P28 §3.4：时间线检查（全部标红，直接读 sentences.json 独立对照）
    tl_reds, tl_notes = _timeline_checks(ctx, data)
    for text, t, detail in tl_reds:
        red(text, "timeline_broken", t or _mmss_in(text), detail)

    conclusion = "标红" if reds else ("有提示" if tips else "通过")
    lines = [f"检查报告 · 总体结论：{conclusion}"]
    if reds:
        lines += ["", "标红（退出码 1，不影响出片）："] + [f"  - {r}" for r in reds]
    lines += [""] + [f"- {c}" for c in checks]
    lines += ["", "## 时间线检查"] + [f"- {n}" for n in tl_notes]
    if tips:
        lines += ["", "提示（不影响退出码）："] + [f"  - {t}" for t in tips]
    out = ctx.p("10-报告", "检查报告.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ctx.log(f"[{'S10' if rendered else 'S9'}] 检查报告 -> {out}（结论：{conclusion}）")
    write_check_json(ctx.root, {
        "schema_version": 1,
        "verdict": CHECK_VERDICT[conclusion],
        "conclusion": conclusion,
        "rendered": bool(rendered),
        "items": items,
        "generated_at": ledger.now_iso(),
        "generated_by": "S10" if rendered else "S9",
        "stale": bool(stale),
        "stale_detail": stale or None,
    })
    return conclusion


def _element_frame_check(ctx, data, out_mp4):
    """ST3-P2c：画面级要素上屏核对——从成片抽帧做像素判定，不命中则标红（退出码 1）。

    更新账本（`ctx.ledger_doc`，即 cli 的 doc）：给要素覆盖核对表每项加 `frame_check`，
    未上屏的要素记进 `render_checks.frame_missing`（cli 据此把退出码记为 1），并把核对表写回
    `logs\\要素覆盖核对表.json`。`--from S10`（S9 跳过）时，核对表从文件读，保证 frame_check 仍能落盘。
    """
    from pipeline.engine import frame_check

    results = frame_check.check_element_frames(data, out_mp4)
    by_id = {r["element_id"]: r for r in results}
    frame_failed = [r["element_id"] for r in results if not r["ok"]]

    # 读/建要素覆盖核对表（S9 跳过时从文件读）
    table = []
    ec_p = ctx.p("logs", "要素覆盖核对表.json")
    if ec_p.exists():
        try:
            table = json.loads(ec_p.read_text(encoding="utf-8")) or []
        except Exception:
            table = []
    if not table:
        table = [{"要素": r["element_id"], "类型": r["type"]} for r in results]
    for e in table:
        r = by_id.get(e.get("要素"))
        if r is not None:
            e["frame_check"] = {"命中": r["hits"], "抽帧": r["frames"], "上屏": r["ok"]}

    # ST3-P14：写进账本 render_checks（S10 自己的检查结果），cli 收尾据此把退出码记为 1。
    # 以前并在 S9 的 original_coverage.missing 里，S10 重跑没有未上屏时旧的不会被清掉；这里顺手清掉。
    doc = getattr(ctx, "ledger_doc", None)
    if doc is None:
        doc = {}
    doc.setdefault("render_checks", {})["frame_missing"] = frame_failed
    cov = doc.get("original_coverage")
    if cov and cov.get("frame_missing"):
        old = set(cov.pop("frame_missing"))
        cov["missing"] = [m for m in cov.get("missing") or [] if m not in old]

    logs_dir = ctx.root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "要素覆盖核对表.json").write_text(
        json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    ctx.log(f"[S10] 要素上屏核对：{len(results)} 个要素 / 未上屏 {len(frame_failed)} 个")
    return frame_failed


# ST3-P14 第 3.2 条：成片声音核对。
AUDIO_TOL_SEC = 1 / 30          # 音轨时长误差上限：1 帧
AUDIBLE_RMS = 0.01              # 10 毫秒一窗，均方根超过它算「有声」
AUDIBLE_TOL_SEC = 0.5           # 成片与配音「最后有声的时刻」允许的差


def _audio_stream_sec(mp4):
    """ffprobe 读成片**音频流**的时长（秒）；没有音频流返回 None。"""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=duration", "-of", "csv=p=0", str(mp4)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {r.stderr.strip()[:200]}")
    out = r.stdout.strip()
    return float(out) if out else None


def _last_audible_sec(path, rate=16000, win_sec=0.01):
    """解码成单声道 16 kHz，从尾部往前找最后一个有声的 10 毫秒窗口，返回它的结束时刻（秒）。

    配音和成片用同一种算法量，两边的差只来自内容。ffmpeg 输出 wav 到管道
    （Remotion 自带的精简版 ffmpeg 没有裸 PCM 输出格式），这里自己找 `data` 块。
    """
    import array
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1",
                        "-ar", str(rate), "-c:a", "pcm_s16le", "-f", "wav", "-"],
                       capture_output=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 解码失败: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    raw = r.stdout
    i = raw.find(b"data", 12)
    if i < 0:
        raise RuntimeError("ffmpeg 输出里没有 data 块")
    pcm = raw[i + 8:]
    x = array.array("h")
    x.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder != "little":
        x.byteswap()
    win = int(rate * win_sec)
    thr = (AUDIBLE_RMS * 32768) ** 2 * win
    for k in range(len(x) // win - 1, -1, -1):
        if sum(v * v for v in x[k * win:(k + 1) * win]) > thr:
            return (k + 1) * win / rate
    return 0.0


def _audio_check(ctx, data, out_mp4):
    """成片里装进去的声音是不是本次配音（ST3-P14 第 3.2 条）。

    1. 音轨时长：ffprobe 读成片音频流时长，必须等于「本次配音时长 + 收尾留白」（`scene_build.
       frames_for_audio`，与场景构建同一个公式），误差不超过 1 帧；
    2. 最后有声的时刻：Remotion 会把音轨补静音补到整条时间线那么长，只看时长查不出装错了配音
       （59 篇事故的成片音轨就是 225.47 秒、正好等于新配音 + 3 秒，但 186 秒以后全是静音）。
       所以再比成片和本次配音「最后有声的时刻」，差超过 AUDIBLE_TOL_SEC 也标红。
    「本次配音」= 场景数据引用的音频在本篇工作文件夹里的源文件（与渲染工程素材同一个定位规则）。
    返回结果 dict，`reds` 为标红文字（空 = 一致）。
    """
    from pipeline import scene_build
    rel = _find_asset(ctx, Path(data.get("audio") or "").name)
    if rel is None:
        raise RuntimeError(f"找不到本次配音: {data.get('audio')}")
    with wave.open(str(ctx.root / rel), "rb") as w:
        narr = w.getnframes() / w.getframerate()
    fps = int(data.get("fps") or scene_build.FPS)
    _, total = scene_build.frames_for_audio(narr)
    expected = total / fps
    stream = _audio_stream_sec(out_mp4)
    mp4_end = _last_audible_sec(out_mp4)
    narr_end = _last_audible_sec(ctx.root / rel)
    reds = []
    if stream is None:
        reds.append(f"成片没有声音，本次配音 {narr:.2f} 秒")
    elif abs(stream - expected) > AUDIO_TOL_SEC + 1e-6:
        reds.append(f"成片声音时长 {stream:.2f} 秒，与本次配音 {narr:.2f} 秒 + 收尾 "
                    f"{scene_build.BLACK_BUFFER_SEC:.1f} 秒不符，成片可能用了旧配音")
    if stream is not None and abs(mp4_end - narr_end) > AUDIBLE_TOL_SEC:
        reds.append(f"成片声音到 {mp4_end:.2f} 秒就没有了，本次配音到 {narr_end:.2f} 秒才结束"
                    f"（差 {abs(mp4_end - narr_end):.2f} 秒），成片可能用了旧配音"
                    if mp4_end < narr_end else
                    f"成片声音到 {mp4_end:.2f} 秒才结束，本次配音到 {narr_end:.2f} 秒就结束了"
                    f"（差 {abs(mp4_end - narr_end):.2f} 秒），成片可能用了旧配音")
    return {"narration": rel, "narration_sec": round(narr, 4), "tail_sec": scene_build.BLACK_BUFFER_SEC,
            "expected_sec": round(expected, 4), "stream_sec": stream,
            "mp4_audible_end": round(mp4_end, 3), "narration_audible_end": round(narr_end, 3),
            "reds": reds}


def _stale_text(stale):
    by = stale.get("by") or []
    return f"之后重跑过 {'、'.join(by)}" if by else "上游已重跑"


def step_s10(ctx):
    # 桩模式安全（ST1-P4a2 §一.6）：账本里只要有任何结果来自桩，就不许出片。
    doc = ledger.load(ctx.root)
    n_stub = ((doc or {}).get("totals") or {}).get("stub_calls", 0)
    if n_stub:
        raise StepError(f"桩结果不得出片：账本里有 {n_stub} 条桩产生的付费结果，"
                        f"交付目录不产出任何文件")

    vd_p = ctx.p("08-场景", "video_data.json")
    if not vd_p.exists():
        raise StepError(f"缺少 S9 产物: {vd_p}")
    data = json.loads(vd_p.read_text(encoding="utf-8"))

    out_dir = ctx.p("09-成片")
    out_dir.mkdir(parents=True, exist_ok=True)
    rp = out_dir / "render"

    # ST3-P14：本次 S10 的检查结果从空开始记，不带上一次 S10 的。
    if ctx.ledger_doc is not None:
        ctx.ledger_doc["render_checks"] = {}

    assets = _scene_assets(ctx, data)
    info = render_project.prepare(ctx, rp, assets)
    ctx.log(f"[S10] 渲染工程 {rp} | 联接 {info['junction']} | 素材 {len(info['files'])}（含字体）| "
            f"新建 {info['new']} / 沿用（内容相同）{info['same']} / 替换（内容不同）{info['replace']}"
            f"（新建与替换里硬链接 {info['hardlink']} / 复制 {info['copy']}）")
    if info["replaced"]:
        ctx.log(f"[S10] 替换（内容不同）{len(info['replaced'])} 个：{'、'.join(info['replaced'])}")
    if info["unused"]:
        ctx.log(f"[S10] 渲染工程 public 里本次没用到的旧文件 {len(info['unused'])} 个（不删除），前几个："
                + "、".join(info["unused"][:5]))
    # ST3-P14 第 3.1 条：出片前逐个核对素材内容；不一致说明上面的刷新没生效，属引擎内部错误，不渲染。
    bad = render_project.verify(rp, info["files"])
    if bad:
        raise StepError("出片前核对不通过（渲染工程里的素材与源文件内容不同，引擎内部错误，未开始渲染）："
                        + "；".join(f"{b['name']}（源 {b['source']} sha256={b['src_sha256']}，"
                                   f"渲染工程 sha256={b['dst_sha256']}）" for b in bad))
    ctx.log(f"[S10] 出片前核对：{len(info['files'])} 个素材与源文件内容一致（配音 {data.get('audio')}）")

    # 场景数据写进隔离工程的 src（渲染只读它）
    (rp / "src" / "video_data.ts").write_text(
        "export const videoData = " + json.dumps(data, ensure_ascii=False) + ";\n",
        encoding="utf-8")
    if ctx.resolved_content_type == spec.CONTENT_TYPE_SCREENSHOT:
        # 用本篇自己的 shots_data.ts 覆盖工程里从主仓复制来那份
        # （主仓那份的 shotImageMap 指向 p12_239，与本篇子目录不符）。
        own = ctx.p("08-场景", "shots_data.ts")
        if not own.exists():
            raise StepError(f"缺少 S9 产物: {own}")
        (rp / "src" / "shots" / "shots_data.ts").write_text(
            own.read_text(encoding="utf-8"), encoding="utf-8")

    out_mp4 = out_dir / f"{ctx.article}.mp4"
    _progress(ctx, "S10", 0, 100, "%", "渲染")
    _run_render(rp, out_mp4, concurrency=4,
                progress=lambda pct: _progress(ctx, "S10", min(pct, 99), 100, "%", "渲染"))
    _progress(ctx, "S10", 100, 100, "%", "渲染完成")
    ctx.log(f"[S10] 成片 -> {out_mp4} ({out_mp4.stat().st_size} 字节)")
    stopflag.check()       # P31b：渲染后的几项核对各要十几秒，开始前各看一次停止标记

    # ST1-P5a G：联系表成为引擎正式产物。失败不影响出片，只记日志。
    try:
        _make_contact_sheet(ctx, out_mp4)
    except Exception as e:
        ctx.log(f"[S10] 联系表生成失败（不影响出片）: {e}")
    stopflag.check()

    # ST3-P2c：画面级要素上屏核对（成片抽帧像素判定，写进覆盖核对表 + 检查报告）。失败不影响出片。
    try:
        _element_frame_check(ctx, data, out_mp4)
    except Exception as e:
        ctx.log(f"[S10] 要素上屏核对失败（不影响出片）: {e}")
    stopflag.check()

    # ST3-P14 第 3.2 条：成片里装进去的声音是不是本次配音。不符标红（退出码 1）；核对本身失败不影响出片。
    try:
        au = _audio_check(ctx, data, out_mp4)
        if ctx.ledger_doc is not None:
            ctx.ledger_doc.setdefault("render_checks", {})["audio"] = au
        ctx.log(f"[S10] 成片声音核对：音轨 {au['stream_sec']:.3f}s / 应为 {au['expected_sec']:.3f}s；"
                f"有声到 {au['mp4_audible_end']:.2f}s / 本次配音有声到 {au['narration_audible_end']:.2f}s"
                + (f" -> 标红：{'；'.join(au['reds'])}" if au["reds"] else " -> 一致"))
    except Exception as e:
        if ctx.ledger_doc is not None:
            ctx.ledger_doc.setdefault("render_checks", {})["audio_error"] = str(e)
        ctx.log(f"[S10] 成片声音核对失败（不影响出片）: {e}")

    # ST1-P5b 丁：检查报告（S9 已写过一版，这里补上成片抽帧核对的结果）。失败不影响出片。
    try:
        _make_check_report(ctx, data, rendered=True)
    except Exception as e:
        ctx.log(f"[S10] 检查报告生成失败（不影响出片）: {e}")

    if ctx.delivery_root:
        dst_dir = ctx.delivery_root / ctx.article
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f"{ctx.article}.mp4"
        import shutil
        shutil.copy2(out_mp4, dst)
        ctx.log(f"[S10] 交付 -> {dst}")
        return str(dst)
    return str(out_mp4)


STEPS = {
    "S0": step_s0,
    "S1": step_s1,
    "S2": step_s2,
    "S3": step_s3,
    "S4": step_s4,
    "S5": step_s5,
    "S6": step_s6,
    "S7": step_s7,
    "S8": step_s8,
    "S9": step_s9,
    "S10": step_s10,
}
