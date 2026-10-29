# -*- coding: utf-8 -*-
"""总装线引擎的命令行入口（任务书 §A、§B）。

    python -m pipeline.engine run    --work-root … --article … [--from S9]
                                     [--delivery-root …] [--tmp-dir …] [--no-paid]
    python -m pipeline.engine plan   --work-root … --article …
    python -m pipeline.engine status --work-root … --article …
    python -m pipeline.engine import --work-root … --input … [--refetch]
                                     [--content-type auto|theory|screenshot] [--title-chars N]
    python -m pipeline.engine estimate   --work-root … --article …      （ST3-P23：开跑前的费用范围）
    python -m pipeline.engine check-keys [--services deepseek,ark,tts,runninghub,feishu]

约定（外壳约定 v0.1 §2 定稿）：
  - stdout 只出「一行一个 JSON」的事件；步骤内部的 print 一律改道 stderr + `logs\\`；
  - 退出码：0 成功 / 1 检查标红（已出片，成片存在，不是失败）/ 2 执行失败 / 3 中断 / 4 配置错误
    （参数解析错误、参数非法、运行锁冲突、其他配置错误；这些情况 stdout 只输出一条 run_done，
    并带可选字段 reason：argument_error / already_running / config_error，其他退出码不输出 reason）；
  - 中断：当前步骤保持未完成（状态仍为 running），账本原子写盘后退出 3；
  - ST3-P22：失败的 `run_done`（退出码 2/3/4）和 `step_fail` 另带 `error_kind`（取值见 errors.py），
    导入成功后每条 `run_done` 都带 `article`；导入成功后最先发 `input_resolved`；
  - P31：`step_fail` 和失败的 `run_done`（退出码 2/4）另带 `service`（哪家外部服务，见 errors.SERVICES；
    不来自外部服务为 null）；
  - `plan` / `status` / `estimate` 严格只读，不写账本、不写日志、不建目录；`check-keys` 只读环境变量、
    只调不计费的接口；
  - ST3-P23：跳过判定在每一步**轮到它时**按当时磁盘上的输入指纹和产物重新做（`_recheck`），上游本轮
    产出了新内容，下游同一次运行里跟着处理；`plan` 仍按开跑前的状态预览。
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from pipeline.engine import (errors, estimate, events, feishu_read, imagegen, intake, keycheck, ledger,
                             netretry, paths, spec, stopflag, tempdir)
from pipeline.engine import steps as steps_mod
from pipeline.engine.steps import Ctx, StepError

EXIT_OK = 0
EXIT_CHECK = 1
EXIT_EXEC = 2
EXIT_INTERRUPT = 3
EXIT_CONFIG = 4

# run_done.reason（仅退出码 4 输出，其他退出码不输出；见 ST1-v4.1 / 外壳约定 v0.5.1）。
REASON_ARGUMENT_ERROR = "argument_error"
REASON_ALREADY_RUNNING = "already_running"
REASON_CONFIG_ERROR = "config_error"

# 费用上限（元）：`--max-cost-cny` > 环境变量 `A2V_MAX_COST_CNY` > 内置默认。
# 默认上限 50。
DEFAULT_MAX_COST_CNY = 50.0


def resolve_max_cost_cny(arg):
    """按「参数 → 环境变量 → 内置默认」三级取值。参数非法（非数字）返回 None 当配置错误。"""
    if arg is not None:
        try:
            return float(arg)
        except (ValueError, TypeError):
            return None
    env = os.environ.get("A2V_MAX_COST_CNY")
    if env and env.strip():
        try:
            return float(env.strip())
        except ValueError:
            return None  # 调用方按配置错误处理
    return DEFAULT_MAX_COST_CNY


# --------------------------------------------------------------------------
# 运行锁（ST1-P5a E）：同一篇文章文件夹同一时刻只允许一个 run。
# --------------------------------------------------------------------------

_LOCK_FILENAME = ".run.lock"


def _pid_is_python(pid):
    """Windows 下判断 PID 是否仍存活且是 python 进程（tasklist 查映像名）。"""
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10)
        return "python" in r.stdout.lower()
    except Exception:
        return False


def _acquire_run_lock(root):
    """获取文章文件夹运行锁。返回 (acquired, note)。

    acquired=False → 锁里 PID 仍存活且是 python 进程，note 为「该文章正在运行（PID x）」。
    acquired=True  → note 为 None（新锁）或「清除过期锁」（上次被强杀留下的旧锁已清）。
    """
    p = Path(root) / _LOCK_FILENAME
    note = None
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        pid = d.get("pid")
        if pid and _pid_is_python(pid):
            return False, f"该文章正在运行（PID {pid}）"
        try:
            p.unlink()
            note = "清除过期锁"
        except Exception:
            pass
    p.write_text(json.dumps({"pid": os.getpid(), "started": round(time.time())},
                             ensure_ascii=False), encoding="utf-8")
    return True, note


def _release_run_lock(root):
    # P31b：停止标记随运行锁一起收掉（引擎收尾后删除标记，约定 v5.4）。
    stopflag.disarm()
    try:
        p = Path(root) / _LOCK_FILENAME
        if p.exists():
            p.unlink()
    except Exception:
        pass

STATUS_RUNNING = ledger.STATUS_RUNNING
STATUS_DONE = ledger.STATUS_DONE
STATUS_FAILED = ledger.STATUS_FAILED
SKIPPABLE = ledger.SKIPPABLE_STATUS


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


class _Tee:
    """把步骤内部的 print 同时写到 stderr 与日志文件（stdout 留给事件）。"""

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, s):
        for st in self._streams:
            try:
                st.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def _install_interrupt_handlers():
    def _raise(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGINT, _raise)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _raise)


def _git_code_version():
    """引擎相关代码的版本串：git 短哈希；**相关代码有未提交改动时加 `-dirty`**（A8.4）。

    「相关代码」= `pipeline\\` 下的一切（`pipeline\\engine\\` 与各步骤调用到的模块），
    含未跟踪的新文件——一个还没提交的引擎不该被当成干净版本对外声明。
    """
    try:
        r = subprocess.run(["git", "-C", str(paths.ENGINE_ROOT), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        ver = r.stdout.strip()
        if not ver:
            return None
        st = subprocess.run(["git", "-C", str(paths.ENGINE_ROOT), "status", "--porcelain",
                             "--", "pipeline"],
                            capture_output=True, text=True, timeout=10)
        dirty = any(l.strip() for l in st.stdout.splitlines())
        return ver + ("-dirty" if dirty else "")
    except Exception:
        return None


def peek_content_type(root):
    """只读预览内容类型，返回 (类型, 依据)；判不出时类型为 None。

    逻辑与 S0 一致（`00-输入\\来源.json` 人工指定 → `00-输入\\图片分类.json`），
    只是**不写盘**，供 `plan` / `status` 这两个严格只读的子命令使用。
    旧判断逻辑（存在 `05-截图分析\\` 或 `shots_data.ts` → 截图型）已撤掉，不再参与判断。
    """
    src = root / "00-输入" / "来源.json"
    if src.exists():
        try:
            d = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        raw = d.get("内容类型") or d.get("content_type")
        ct = steps_mod.TYPE_ALIASES.get(raw) if raw else None
        if ct:
            return ct, f"来源.json 的 内容类型={raw}"
    cls = root / "00-输入" / "图片分类.json"
    if cls.exists():
        try:
            items = json.loads(cls.read_text(encoding="utf-8")).get("items") or {}
        except Exception:
            items = {}
        if items:
            labels = {v.get("分类") for v in items.values()}
            if labels & set(steps_mod.SCREENSHOT_LABELS):
                return spec.CONTENT_TYPE_SCREENSHOT, "图片分类里有截图"
            return spec.CONTENT_TYPE_THEORY, "图片分类里没有截图"
    return None, "类型未定（来源.json 无内容类型、且无图片分类）"


# --------------------------------------------------------------------------
# 账本与跳过判定
# --------------------------------------------------------------------------


_new_doc = ledger.new_doc
_touch = ledger.touch


def _render_source_fingerprint():
    """S10 渲染源码（引擎根 `R57-render-test`）的合并指纹。

    渲染源码是 S10 的**隐式输入**：改动它（哪怕只改一个注释字节）都应让 S10 重跑，
    而不是被"场景数据没变"错误跳过。由引擎根（`paths.RENDER_MAIN`）推出，随代码走。
    """
    import hashlib
    rs = paths.RENDER_MAIN
    h = hashlib.sha256()
    for rel in ("src", "package.json", "tsconfig.json"):
        p = rs / rel
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    h.update(str(f.relative_to(rs)).replace("\\", "/").encode("utf-8"))
                    h.update(ledger.sha256_file(f).encode("utf-8"))
        elif p.is_file():
            h.update(rel.encode("utf-8"))
            h.update(ledger.sha256_file(p).encode("utf-8"))
    return h.hexdigest()


def _fingerprint(root, sid, steps):
    fp = ledger.fingerprint(root, ledger.expand(root, steps[sid]["in"]))
    if sid == "S10":
        import hashlib
        h = hashlib.sha256()
        h.update(fp.encode("utf-8"))
        h.update(_render_source_fingerprint().encode("utf-8"))
        fp = h.hexdigest()
    return fp


def _paid_fields(ctx, doc, sid):
    """把这一步经统一入口发生的付费调用统计**累计**写进账本 `cost`（ST1-P4a §一.4 / ST1-P4c C3）。

    没有任何付费活动（含命中缓存）时返回 `{}`，`cost` 保持默认值不动。
    续跑同一步多次时，`paid_calls` / `estimated_cny` 等字段与账本里已有的历史值**累加**，
    不再覆盖（否则续跑前已发生的付费会被丢掉）。命中缓存只加 `cache_hits`、不加金额。
    """
    st = ctx.paid_stats
    if not st["calls"] and not st["hits"]:
        return {}
    prev = (ledger.find_step(doc, sid) or {}).get("cost") or {}
    prev_cny = prev.get("estimated_cny")
    prev_cny = float(prev_cny) if isinstance(prev_cny, (int, float)) else 0.0
    # 缺陷a：真实付费（calls/paid_calls/estimated_cny）已由 perform→record_paid 即时写入
    # 本步骤 cost，这里不再重复累加，只补缓存命中（cache_hits）与桩调用（stub_calls）。
    return {"cost": {
        "calls": int(prev.get("calls", 0)) + st.get("stub_calls", 0),
        "paid_calls": int(prev.get("paid_calls", 0)),
        "cache_hits": int(prev.get("cache_hits", 0)) + st["hits"],
        "stub_calls": int(prev.get("stub_calls", 0)) + st.get("stub_calls", 0),
        "estimated_cny": round(prev_cny, 4),
        "incomplete": bool(prev.get("incomplete")) or bool(st["incomplete"]),
    }}


def _verify(root, doc, sid, steps):
    """跳过判定：状态可跳过 + 产物 sha256 全对 + （若已记录）输入指纹不变。"""
    rec = ledger.find_step(doc, sid)
    if rec is None:
        return False, "账本无记录"
    if rec.get("status") not in SKIPPABLE:
        return False, f"状态={rec.get('status')}"
    # ST3-P16：S8 有缺图时要重跑（只为缺图重新出图，已有的图不重出）；没有缺图照常按产物与指纹判定。
    why = steps_mod.rerun_reason(root, sid)
    if why:
        return False, why
    outs = rec.get("outputs") or []
    if not outs:
        return False, "无产物记录"
    for o in outs:
        p = Path(root) / o["path"]
        if not p.exists():
            return False, f"产物缺失 {o['path']}"
        if ledger.sha256_file(p) != o["sha256"]:
            return False, f"产物已改动 {o['path']}"
    fp = rec.get("input_fingerprint")
    if fp is not None and fp != _fingerprint(root, sid, steps):
        return False, "输入指纹变化"
    return True, f"跳过（{rec.get('status')}）"


def _survey_normal(root, ct, steps, order, doc):
    """不带 --from/--to 的常规跳过判定。"""
    res = {}
    need = set()
    for sid in order:
        ok, why = _verify(root, doc, sid, steps)
        res[sid] = ("skip" if ok else "run", why)
        if not ok:
            need.add(sid)
    # 连带重跑：本轮要跑的步骤，其数据下游也重跑。
    # 付费步骤例外——它们是内容寻址的（ST1-P4），只按自身产物+输入指纹判定，
    # 不因上游重算而被连带拉起来（否则首跑 S2 就会把 S3/S8 拖起来）。
    closure = set()
    for sid in need:
        closure |= spec.downstream_of(ct, sid)
    for sid in order:
        if sid in need or sid not in closure:
            continue
        if steps[sid]["cost"]:
            res[sid] = ("skip", "付费步骤：产物与指纹校验通过，不做连带重跑")
        else:
            res[sid] = ("run", "上游步骤本轮要重跑（连带）")
    return res


def _survey(root, args, ct, steps, order, doc):
    """算这一轮哪些步骤要跑，返回 {sid: (action, reason)}。

    --from 指定起点（含下游）；--to 指定终点（其后强制 skip）。两者可叠加。
    """
    if args.cmd == "run" and args.from_step:
        frm = args.from_step
        if frm not in steps:
            return None, f"--from 的步骤 {frm} 不适用于内容类型 {ct}"
        on = {frm} | spec.downstream_of(ct, frm)
        res = {}
        for sid in order:
            if sid in on:
                res[sid] = ("run", f"--from {frm} 指定的步骤或其下游")
            else:
                res[sid] = ("skip", f"不在 --from {frm} 的范围")
    else:
        res = _survey_normal(root, ct, steps, order, doc)
    if args.cmd == "run" and args.to_step:
        to = args.to_step
        if to not in steps:
            return None, f"--to 的步骤 {to} 不适用于内容类型 {ct}"
        idx = order.index(to)
        for sid in order[idx + 1:]:
            res[sid] = ("skip", f"不在 --to {to} 的范围")
    return res, None


def _recheck_eligible(args, sid, order, lock, unlock):
    """这一步的"跳过"是否来自常规跳过判定（而不是 --from / --to / 定稿锁的强制决定）。"""
    if args.from_step:
        return False
    if args.to_step and args.to_step in order and order.index(sid) > order.index(args.to_step):
        return False
    if sid in ("S1", "S2", "S3") and (unlock or args.accept_narration_edit
                                      or (lock is not None and lock != "__corrupt__")):
        return False
    return True


def _recheck(root, doc, sid, steps, ct, ran):
    """ST3-P23 §3.1：轮到这一步时重新判断。返回 None（照旧跳过）或新的跳过原因（改为执行）。

    - 产物、输入指纹（此刻磁盘上的）或状态不再通过校验 → 执行（上游本轮改写了它的输入）；
    - 非付费步骤、上游本轮跑过 → 执行（连带重跑，与开跑前的判定口径一致）；
    - 付费步骤只看自己的产物与指纹：上游重跑但产出没变时照旧跳过（内容寻址，不白花钱）。
    """
    ok, why = _verify(root, doc, sid, steps)
    if not ok:
        return f"轮到时重新判断：{why}（上游本轮已更新）" if ran else why
    ups = [s for s in ran if sid in spec.downstream_of(ct, s)]
    if ups and not steps[sid]["cost"]:
        return f"上游步骤本轮已重跑（{'、'.join(ups)}，连带）"
    return None


# --------------------------------------------------------------------------
# 检查结果过期判定（ST3-P14 第 5 步）
# --------------------------------------------------------------------------

# 账本里的检查结果 -> 产生它的步骤，以及过期时的说法。
_CHECK_RESULTS = (
    ("original_coverage", "S9", "检查结果已过期（{by}），需要跑到 S9 才会重新判定"),
    ("render_checks", "S10", "成片检查已过期（{by}），需要跑到 S10 才会重新判定"),
)


def _mark_stale_checks(doc, ct, order, ran):
    """给账本里已过期的检查结果打上 `stale` 标记，返回要写进 run_done 消息的说明。

    一份检查结果（产生它的步骤记作 P）：
      - 本次跑了 P：本次的结果，有效；
      - 本次没跑 P，但 P 的数据上游（`spec.downstream_of` 反查）本次有步骤跑完了：过期——
        打上 `stale: {by: [本次跑了的上游步骤]}` 写回账本，之后的运行只要还没重跑 P，就一直按过期处理，
        不会再拿它判退出码；
      - 本次没跑 P，上游也没跑（例如只跑 S10 时的 S9 结果）：沿用账本里的结论（过期的仍是过期）。
    P 重跑时整份结果被新结果替换，标记随之消失。
    """
    notes = []
    for key, prod, text in _CHECK_RESULTS:
        res = doc.get(key)
        if not res or prod not in order:
            continue
        if prod in ran:
            res.pop("stale", None)
            continue
        ups = [s for s in ran if prod in spec.downstream_of(ct, s)]
        if ups:
            res["stale"] = {"by": ups, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if res.get("stale"):
            by = res["stale"].get("by") or []
            notes.append(text.format(by=f"之后重跑过 {'、'.join(by)}" if by else "上游已重跑"))
    return notes


# --------------------------------------------------------------------------
# 定稿锁（ST3-F1 第2项）：S1/S2/S3 产物锁定后，除非显式解锁/接受编辑，否则不重跑。
# --------------------------------------------------------------------------


def _read_narration_lock(root):
    """读 `01-口播/定稿锁.json`。不存在返回 None；存在但解析失败返回特殊串 "__corrupt__"。"""
    p = Path(root) / steps_mod._NARRATION_LOCK_REL
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return "__corrupt__"


def _narration_lock_changed(root, lock):
    """返回与锁内 sha256 不一致的文件（相对路径）；空列表 = 一致。"""
    files = (lock or {}).get("files") or {}
    changed = []
    for rel, sha in files.items():
        p = Path(root) / rel
        if not p.is_file():
            changed.append(f"{rel}（缺失）")
        elif ledger.sha256_file(p) != sha:
            changed.append(rel)
    return changed


def _apply_narration_flags(res, order, lock, unlock, accept):
    """定稿锁 / --unlock-narration / --accept-narration-edit 对 S1/S2/S3 的覆盖。"""
    for sid in ("S1", "S2", "S3"):
        if sid not in res:
            continue
        if unlock:
            res[sid] = ("run", "解除定稿锁，重新生成")
        elif accept:
            if sid == "S1":
                res[sid] = ("skip", "保留人工定稿口播（--accept-narration-edit）")
            else:
                res[sid] = ("run", "重新分组/配音（--accept-narration-edit）")
        elif lock is not None and lock != "__corrupt__":
            res[sid] = ("skip", "口播已定稿，跳过")


# --------------------------------------------------------------------------
# plan / status
# --------------------------------------------------------------------------


def _resolve_input_source(args, emitter):
    """`--input` 前置（`import` 子命令与 `run --input` 共用）：导入或复用文章文件夹并做输入检查。

    成功：发 `input_resolved`，回填 `args.article`，返回 None。
    失败：已发 `run_done`，返回退出码——导入失败 4（飞书图片下载失败 2）；
    有阻断问题时先发 `input_problems`，再 4 + config_error + error_kind=input_invalid。
    """
    src = getattr(args, "input_src", None)
    if not src:
        return None
    n = getattr(args, "title_chars", intake.DEFAULT_TITLE_CHARS)
    if not (intake.TITLE_CHARS_MIN <= n <= intake.TITLE_CHARS_MAX):
        emitter.emit(events.EVENT_RUN_DONE, None,
                     f"配置错误：--title-chars 要在 {intake.TITLE_CHARS_MIN}～{intake.TITLE_CHARS_MAX} 之间（{n}）",
                     exit_code=EXIT_CONFIG, reason=REASON_ARGUMENT_ERROR,
                     error_kind=errors.KIND_INTERNAL)
        return EXIT_CONFIG
    try:
        info = intake.run_import(src, args.work_root, refetch=bool(getattr(args, "refetch", False)),
                                 content_type=getattr(args, "content_type", "auto"),
                                 title_chars=n,
                                 list_csv=os.environ.get("A2V_ARTICLE_LIST_CSV") or None)
    except intake.InputProblems as e:
        emitter.emit(events.EVENT_INPUT_PROBLEMS, None, f"输入有 {len(e.problems)} 个问题",
                     problems=e.problems)
        emitter.emit(events.EVENT_RUN_DONE, None, f"配置错误：输入有问题（{e}）",
                     exit_code=EXIT_CONFIG, reason=REASON_CONFIG_ERROR,
                     error_kind=errors.KIND_INPUT_INVALID)
        return EXIT_CONFIG
    except feishu_read.FeishuArgumentError as e:
        emitter.emit(events.EVENT_RUN_DONE, None, f"配置错误：{e}", exit_code=EXIT_CONFIG,
                     reason=REASON_ARGUMENT_ERROR, error_kind=e.kind,
                     service=errors.failure_service(e, e.kind))
        return EXIT_CONFIG
    except feishu_read.FeishuConfigError as e:
        extra = {"missing_keys": e.missing_keys} if e.missing_keys else {}
        emitter.emit(events.EVENT_RUN_DONE, None, f"配置错误：{e}", exit_code=EXIT_CONFIG,
                     reason=REASON_CONFIG_ERROR, error_kind=e.kind,
                     service=errors.failure_service(e, e.kind), **extra)
        return EXIT_CONFIG
    except feishu_read.FeishuImageError as e:
        emitter.emit(events.EVENT_RUN_DONE, None, f"执行失败：{e}", exit_code=EXIT_EXEC,
                     error_kind=e.kind, service=errors.failure_service(e, e.kind))
        return EXIT_EXEC
    args.article = info["article"]
    emitter.run_done_defaults["article"] = info["article"]
    root = Path(args.work_root) / info["article"]
    ct, _ = peek_content_type(root)
    emitter.emit(events.EVENT_INPUT_RESOLVED, None,
                 ("复用已有文件夹" + ("（输入有变化）" if info["changed"] else "（输入没变）")
                  if info["reused"] else "已新建文章文件夹") + f" {info['article']}",
                 article=info["article"], work_dir=info["work_dir"],
                 source_type=info["source_type"], title=info["title"],
                 reused=info["reused"], changed=info["changed"],
                 image_count=info["image_count"], content_type=ct,
                 warnings=info["warnings"])
    if info["problems"]:
        emitter.emit(events.EVENT_INPUT_PROBLEMS, None, f"输入有 {len(info['problems'])} 个问题",
                     problems=info["problems"])
        emitter.emit(events.EVENT_RUN_DONE, None,
                     f"配置错误：输入有 {len(info['problems'])} 个问题，未执行任何步骤",
                     exit_code=EXIT_CONFIG, reason=REASON_CONFIG_ERROR,
                     error_kind=errors.KIND_INPUT_INVALID)
        return EXIT_CONFIG
    return None


def cmd_import(args, emitter):
    """`import`：只导入、做输入检查，不执行任何步骤、不要运行时、不查业务密钥。"""
    code = _resolve_input_source(args, emitter)
    if code is not None:
        return code
    ct, _ = peek_content_type(Path(args.work_root) / args.article)
    emitter.emit(events.EVENT_RUN_DONE, None, f"导入完成 {args.article}",
                 article=args.article, content_type=ct, exit_code=EXIT_OK)
    return EXIT_OK


def _resolve(args, emitter):
    """公共前置：解析工作文件夹。返回 (root, ct, reason) 或 (None, code)。

    这里只是**预览**内容类型（供挑步骤表用）；真正的判定在 S0 步骤里做，S0 的产物
    `00-输入\\类型判断.json` 才是权威。预览判不出（缺来源.json 与图片分类）时 `ct` 为
    **None**（F2 口径：判不出就是 null，不硬凑成理论型），按理论型列步骤——S0 随后就会以
    "需要识图分类"失败，一步也不会往下走。
    """
    root = Path(args.work_root) / args.article
    if not root.is_dir():
        emitter.emit(events.EVENT_RUN_DONE, None,
                     f"配置错误：文章文件夹不存在 {root}", exit_code=EXIT_CONFIG,
                     reason=REASON_CONFIG_ERROR, error_kind=errors.KIND_INPUT_INVALID)
        return None, EXIT_CONFIG
    emitter.run_done_defaults["article"] = args.article
    ct, reason = peek_content_type(root)
    return (root, ct, reason), None


def cmd_plan(args, emitter):
    got = _resolve(args, emitter)
    if got[0] is None:
        return got[1]
    root, ct, reason = got[0]
    steps = spec.steps_for(ct)
    order = spec.order_for(ct)
    doc = ledger.load(root) or _new_doc(args.article, ct)
    res, err = _survey(root, args, ct, steps, order, doc)
    if err:
        emitter.emit(events.EVENT_RUN_DONE, None, f"配置错误：{err}", exit_code=EXIT_CONFIG,
                     reason=REASON_CONFIG_ERROR, error_kind=errors.KIND_INTERNAL)
        return EXIT_CONFIG
    for sid in order:
        rec = ledger.find_step(doc, sid) or {}
        action, why = res[sid]
        emitter.emit(events.EVENT_PLAN, sid, why,
                     name=steps[sid]["name"], status=rec.get("status", ledger.STATUS_PENDING),
                     action=action, paid=steps[sid]["cost"],
                     inputs=steps[sid]["in"], outputs=steps[sid]["out"])
    emitter.emit(events.EVENT_RUN_DONE, None, f"plan 完成（内容类型 {ct}：{reason}）", exit_code=EXIT_OK)
    return EXIT_OK


def cmd_status(args, emitter):
    got = _resolve(args, emitter)
    if got[0] is None:
        return got[1]
    root, ct, reason = got[0]
    steps = spec.steps_for(ct)
    order = spec.order_for(ct)
    doc = ledger.load(root)
    if doc is None:
        doc = _new_doc(args.article, ct)
    for sid in order:
        rec = ledger.find_step(doc, sid)
        if rec is None:
            emitter.emit(events.EVENT_STATUS, sid, "账本无记录", name=steps[sid]["name"],
                         status=ledger.STATUS_PENDING, outputs=[], cost=steps[sid]["cost"])
            continue
        files = ledger.expand(root, steps[sid]["out"])
        emitter.emit(events.EVENT_STATUS, sid, rec.get("message") or "",
                     name=rec.get("name") or steps[sid]["name"], status=rec.get("status"),
                     cost=steps[sid]["cost"], outputs=files,
                     duration_sec=rec.get("duration_sec"), attempts=rec.get("attempts"),
                     source=rec.get("source"))
    emitter.emit(events.EVENT_RUN_DONE, None, "status 完成",
                 article=doc.get("article"), content_type=doc.get("content_type"),
                 delivery_path=doc.get("delivery_path"), totals=doc.get("totals"),
                 exit_code=EXIT_OK)
    return EXIT_OK


def _image_service_error(emitter):
    """ST3-P27：内部环境变量 `A2V_IMAGE_SERVICE` 写错 → 发 run_done（退出码 4、argument_error）并返回退出码。"""
    try:
        imagegen.selected()
    except imagegen.ServiceConfigError as e:
        emitter.emit(events.EVENT_RUN_DONE, None, f"配置错误：{e}", exit_code=EXIT_CONFIG,
                     reason=REASON_ARGUMENT_ERROR, error_kind=errors.KIND_INTERNAL)
        return EXIT_CONFIG
    return None


def cmd_estimate(args, emitter):
    """`estimate`（ST3-P23 §3.5）：开跑前的费用范围。只读，不联网、不要密钥、不花钱。"""
    got = _resolve(args, emitter)
    if got[0] is None:
        return got[1]
    bad = _image_service_error(emitter)
    if bad is not None:
        return bad
    root, ct, reason = got[0]
    est = estimate.estimate(root, args.article, ct)
    emitter.emit(events.EVENT_ESTIMATE, None,
                 f"预计花费 ¥{est['low_cny']:.2f}～¥{est['high_cny']:.2f}"
                 + (f"（已有缓存省下约 ¥{est['saved_by_cache_cny']:.2f}）" if est["saved_by_cache_cny"] else ""),
                 **est)
    emitter.emit(events.EVENT_RUN_DONE, None, f"estimate 完成（内容类型 {ct}：{reason}）",
                 content_type=ct, exit_code=EXIT_OK)
    return EXIT_OK


def cmd_check_keys(args, emitter):
    """`check-keys`（ST3-P23 §3.7）：每个服务发一条 key_check。只调不计费的接口，拿不准的返回 unknown。

    检测本身做完就是退出码 0（某个服务不可用写在它那一条的 status 里）；`--services` 写错是 4。
    """
    try:
        services = keycheck.parse_services(args.services)
    except ValueError as e:
        emitter.emit(events.EVENT_RUN_DONE, None, f"配置错误：{e}", exit_code=EXIT_CONFIG,
                     reason=REASON_ARGUMENT_ERROR, error_kind=errors.KIND_INTERNAL)
        return EXIT_CONFIG
    results = keycheck.check(services)
    for r in results:
        r = dict(r)
        msg = r.pop("message")
        emitter.emit(events.EVENT_KEY_CHECK, None, f"{r['service_name']}：{msg}", **r)
    emitter.emit(events.EVENT_RUN_DONE, None, "check-keys 完成",
                 results={r["service"]: r["status"] for r in results}, exit_code=EXIT_OK)
    return EXIT_OK


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _pull_pending_jobs(root, doc):
    """把磁盘上（步骤刚写入的）`pending_jobs` 并回运行器手里的账本对象。

    步骤（如 S3）自己 load/save 账本登记异步任务号；运行器手里是另一份对象，
    失败落盘时会把它整份覆盖掉，导致"提交后中断"的任务号丢失、重跑只能重复提交
    （§一.4）。失败路径落盘前先并一次。
    """
    disk = ledger.load(root)
    if disk is not None:
        doc["pending_jobs"] = disk.get("pending_jobs") or []


def _respect_to(res, order, args):
    """自动解锁时 S1～S3 强制重跑，但仍以 `--to` 为界（显式 --unlock-narration 的老行为不动）。"""
    to = args.to_step
    if to and to in order:
        for sid in order[order.index(to) + 1:]:
            res[sid] = ("skip", f"不在 --to {to} 的范围")


def _narration_source_changed(root, lock):
    """定稿锁记的原文 sha256 与现在的 `00-输入/文章.md` 不同 → True。旧锁没记原文时视为没变。"""
    old = (lock or {}).get("source_md_sha256")
    if not old:
        return False
    p = Path(root) / "00-输入" / "文章.md"
    return (ledger.sha256_file(p) if p.is_file() else None) != old


def _keys_needed(root, order, res, steps):
    """这一轮（`order` 里）要跑的付费步骤实际会用到的密钥变量。S0 只在真要识图时才要方舟。"""
    need = set()
    for s2 in order:
        if res[s2][0] != "run" or not steps[s2]["cost"]:
            continue
        if s2 == "S0" and not steps_mod.s0_needs_vision(root):
            continue
        need |= set(spec.PAID_KEYS.get(s2, []))
    return need


def _safe_save(root, doc, log=None):
    """失败/中断收尾时写账本；写不了（磁盘满等）就记一句，不让异常漏出去吞掉 run_done。"""
    try:
        ledger.save_atomic(root, _touch(doc))
        return True
    except Exception as e:
        if log:
            try:
                log(f"[引擎] 账本写盘失败：{type(e).__name__}: {e}")
            except Exception:
                pass
        return False


def _fail_step(emitter, root, doc, ctx, sid, msg, kind, t0, ct, close, service=None):
    """步骤失败：账本记 failed（尽力而为），发 step_fail + run_done（退出码 2，带 error_kind、service）。"""
    try:
        _pull_pending_jobs(root, doc)
    except Exception:
        pass
    try:
        ledger.upsert_step(doc, sid, status=STATUS_FAILED, message=msg,
                           duration_sec=round(time.time() - t0, 1),
                           **_paid_fields(ctx, doc, sid))
    except Exception:
        pass
    _safe_save(root, doc, ctx.log)
    emitter.emit(events.EVENT_STEP_FAIL, sid, msg, error_kind=kind, service=service)
    emitter.emit(events.EVENT_RUN_DONE, None, f"执行失败于 {sid}",
                 content_type=ctx.resolved_content_type or ct, exit_code=EXIT_EXEC,
                 error_kind=kind, service=service)
    close()
    return EXIT_EXEC


def cmd_run(args, emitter):
    got = _resolve(args, emitter)
    if got[0] is None:
        return got[1]
    root, ct, reason = got[0]
    steps = spec.steps_for(ct)
    order = spec.order_for(ct)

    # 运行锁（ST1-P5a E）：同文章已有引擎在跑时，第二次 run 直接退出码 4，防两个引擎
    # 同时写同一文件夹（APP 关闭后留孤儿进程或重复启动时会出现）。plan/status 只读不建锁。
    lock_ok, lock_note = _acquire_run_lock(root)
    if not lock_ok:
        emitter.emit(events.EVENT_RUN_DONE, None, lock_note, exit_code=EXIT_CONFIG,
                     reason=REASON_ALREADY_RUNNING, error_kind=errors.KIND_ALREADY_RUNNING)
        return EXIT_CONFIG
    # P31b：拿到运行锁后开始监视停止标记；上一次运行留下的旧标记先删掉。
    stale_stop = stopflag.arm(root)

    # 运行时副本（C1）：`--runtime-dir` 不传时 = `<work-root 的上一级>\_runtime`；
    # 不存在或缺 remotion 包 → 配置错误，退出码 4。
    runtime_nm = paths.resolve_runtime_node_modules(args.work_root, args.runtime_dir)
    if not paths.runtime_node_modules_ok(runtime_nm):
        _release_run_lock(root)
        emitter.emit(events.EVENT_RUN_DONE, None,
                     f"配置错误：运行时目录不可用（缺 remotion 包）{runtime_nm}",
                     exit_code=EXIT_CONFIG, reason=REASON_CONFIG_ERROR,
                     error_kind=errors.KIND_INTERNAL)
        return EXIT_CONFIG

    # 字体可选：本仓库用系统字体，无需字体文件；若运行时带了自定义字体则保持可用。
    runtime_font = Path(runtime_nm).parent / paths.FONT_REL
    if not runtime_font.is_file():
        src_font = paths.RENDER_MAIN / "public" / paths.FONT_REL
        if src_font.is_file():
            runtime_font.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_font, runtime_font)

    # 费用上限：参数 → 环境变量 A2V_MAX_COST_CNY → 内置默认 50。非法（≤0 或非数字）即配置错误（退出码 4）。
    max_cost = resolve_max_cost_cny(args.max_cost_cny)
    if max_cost is None or max_cost <= 0:
        _release_run_lock(root)
        emitter.emit(events.EVENT_RUN_DONE, None,
                     "配置错误：费用上限非法（--max-cost-cny="
                     f"{args.max_cost_cny!r}, A2V_MAX_COST_CNY="
                     f"{os.environ.get('A2V_MAX_COST_CNY')!r}）", exit_code=EXIT_CONFIG,
                     reason=REASON_ARGUMENT_ERROR, error_kind=errors.KIND_INTERNAL)
        return EXIT_CONFIG

    # ST3-P27：出图服务的内部切换写错也按参数错误（退出码 4），开跑前就拦下。
    bad = _image_service_error(emitter)
    if bad is not None:
        _release_run_lock(root)
        return bad

    # 临时目录 + 禁写主仓/子进程字节码（§C4）
    # ST3-P30 / v5.6：本次运行的临时文件写进专用子目录，结束时按结果清理。
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    tmp_run_dir = tempdir.prepare(tempdir.resolve_base(args.work_root, args.tmp_dir))
    tempdir.apply_env(tmp_run_dir)
    run_success = {"ok": False}

    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_f = open(log_path, "w", encoding="utf-8")
    err_stream = sys.stderr
    sys.stdout = _Tee(err_stream, log_f)
    sys.stderr = sys.stdout
    ctx_log = lambda m: print(m, flush=True)  # noqa: E731

    def close():
        netretry.set_notifier(None)
        try:
            sys.stdout.flush()
            log_f.close()
        except Exception:
            pass
        tempdir.finalize(tmp_run_dir, run_success["ok"])
        _release_run_lock(root)   # 正常/失败/中断都释放运行锁（ST1-P5a E）

    if lock_note:
        ctx_log(f"[引擎] {lock_note}")
    if stale_stop:
        ctx_log(f"[引擎] 删除上次运行留下的停止标记 {stopflag.STOP_FILENAME}")
    ctx_log(f"[引擎] 文章 {args.article} | 内容类型（预览）{ct}（{reason}）")
    ctx_log(f"[引擎] 工作文件夹 {root}")
    ctx_log(f"[引擎] 引擎根 {paths.ENGINE_ROOT}")
    ctx_log(f"[引擎] 运行时副本 {runtime_nm}")
    ctx_log(f"[引擎] 代码版本 {_git_code_version()}")

    doc = ledger.load(root)
    if doc is None:
        doc = _new_doc(args.article, ct)
        ctx_log("[引擎] 新建账本")
    elif ct is not None:
        # 只在这个预览判得出时才覆盖：判不出（None）不能把上轮已定的类型抹掉。
        doc["content_type"] = ct
    ledger.save_atomic(root, _touch(doc))

    # ST3-F1 第2项：定稿锁处理（优先于 --from / 跳过判定）。
    unlock = bool(args.unlock_narration)
    auto_unlocked = False
    if unlock:
        lp = Path(root) / steps_mod._NARRATION_LOCK_REL
        if lp.is_file():
            lp.unlink()
            ctx_log("[引擎] --unlock-narration：已删除定稿锁，重新生成口播/分组/配音")
    lock = _read_narration_lock(root)
    if lock == "__corrupt__":
        emitter.emit(events.EVENT_RUN_DONE, None,
                     "配置错误：定稿锁.json 无法解析", exit_code=EXIT_CONFIG,
                     reason=REASON_CONFIG_ERROR, error_kind=errors.KIND_INTERNAL)
        close()
        return EXIT_CONFIG
    if lock is not None and not args.accept_narration_edit and not unlock:
        changed = _narration_lock_changed(root, lock)
        if _narration_source_changed(root, lock):
            # ST3-P22（G8）：原文变了。口播没人改过 → 自动解锁、按新原文重新生成；改过 → 让用户选。
            if changed:
                emitter.emit(events.EVENT_RUN_DONE, None,
                             "配置错误：原文已变化，但口播产物有人工修改："
                             + "、".join(changed)
                             + "（--unlock-narration 放弃人工修改、按新原文重新生成；"
                               "--accept-narration-edit 保留人工修改）",
                             exit_code=EXIT_CONFIG, reason=REASON_CONFIG_ERROR,
                             error_kind=errors.KIND_INPUT_INVALID)
                close()
                return EXIT_CONFIG
            (Path(root) / steps_mod._NARRATION_LOCK_REL).unlink()
            lock, unlock, auto_unlocked = None, True, True
            ctx_log("[引擎] 原文已变化，口播将重新生成（已自动解除定稿锁）")
            emitter.emit(events.EVENT_NARRATION_UNLOCKED, None, "原文已变化，口播将重新生成")
        elif changed:
            emitter.emit(events.EVENT_RUN_DONE, None,
                         "配置错误：口播已定稿，但以下文件已改动："
                         + "、".join(changed)
                         + "（--unlock-narration 重新生成，或 --accept-narration-edit 接受编辑）",
                         exit_code=EXIT_CONFIG, reason=REASON_CONFIG_ERROR,
                         error_kind=errors.KIND_INPUT_INVALID)
            close()
            return EXIT_CONFIG

    res, err = _survey(root, args, ct, steps, order, doc)
    if err:
        emitter.emit(events.EVENT_RUN_DONE, None, f"配置错误：{err}", exit_code=EXIT_CONFIG,
                     reason=REASON_CONFIG_ERROR, error_kind=errors.KIND_INTERNAL)
        close()
        return EXIT_CONFIG
    _apply_narration_flags(res, order, lock, unlock, args.accept_narration_edit)
    if auto_unlocked:
        _respect_to(res, order, args)

    ctx = Ctx(root=root, article=args.article, delivery_root=args.delivery_root,
              tmp_dir=args.tmp_dir, no_paid=args.no_paid, log=ctx_log,
              runtime_node_modules=runtime_nm, max_cost_cny=max_cost, stub=args.stub)
    # 已花费从账本累计值接着算（续跑时上限是整个文件夹的，不是单轮的）。
    ctx.spent_cny = float((doc.get("totals") or {}).get("estimated_cny") or 0.0)
    # 缺陷a：把账本对象引用注入 ctx，供真实付费成功后的即时记账（record_paid）。
    ctx.ledger_doc = doc
    # 预览的内容类型先垫上：`--from S5` 或 S0 被跳过时，S5/S9 要用它挑产物目录
    # （理论型 → 06-编排，截图型 → 05-截图分析）。S0 真跑时会用权威判定覆盖。
    ctx.resolved_content_type = ct
    code_version = _git_code_version()
    interrupted = False
    ran = []      # 本次运行里跑完的步骤（ST3-P14：判断检查结果是否过期）

    keys_checked = False
    ctx.progress_cb = _ProgressThrottle(emitter)
    # ST3-P26：外部请求临时故障重试时发一条 step_progress（done/total 沿用这一步上一条进度），
    # message「网络不稳定，正在重试（第 N 次）」，另带 `retry` 字段（APP 不认识可忽略）。
    cur_step = {"sid": None}

    def _net_retry(service, attempt, wait, reason, what):
        ctx.progress_cb.note(cur_step["sid"], f"网络不稳定，正在重试（第 {attempt} 次）",
                             retry={"service": service, "what": what, "attempt": attempt,
                                    "wait_sec": wait, "reason": reason})
    netretry.set_notifier(_net_retry)
    try:
        for sid in order:
            stopflag.check()       # P31b：每一步开始前看停止标记
            cur_step["sid"] = sid
            action, why = res[sid]
            if action == "skip" and _recheck_eligible(args, sid, order, lock, unlock):
                again = _recheck(root, doc, sid, steps, ctx.resolved_content_type or ct, ran)
                if again:
                    action, why = "run", again
                    res[sid] = (action, why)
                    ctx_log(f"[引擎] {sid} 轮到时重新判断：改为执行（{why}）")
            if action == "skip":
                emitter.emit(events.EVENT_STEP_SKIP, sid, why, name=steps[sid]["name"])
                continue
            # 密钥检查在第一个要跑的付费步骤之前，一次查齐这一轮之后真正会跑的付费步骤；
            # S0 判成截图型、插进 S6 之后再查一次。`--no-paid` 时一个都不查。
            # ST3-P22（G15）：只要求实际会调的服务——S0 不识图就不要方舟（见 _keys_needed）。
            # ST3-P23：轮到时才改为执行的付费步骤（_recheck）不在开跑时那一次的清单里，轮到它时再查它自己的。
            if steps[sid]["cost"] and not args.no_paid and not args.stub:
                need = _keys_needed(root, order[order.index(sid):] if not keys_checked else [sid],
                                    res, steps)
                keys_checked = True
                missing = sorted(k for k in need if not os.environ.get(k))
                if missing:
                    msg = "缺少必需的环境变量密钥: " + ", ".join(missing)
                    emitter.emit(events.EVENT_RUN_DONE, None, f"配置错误：{msg}",
                                 exit_code=EXIT_CONFIG, reason=REASON_CONFIG_ERROR,
                                 error_kind=errors.KIND_KEY_MISSING, missing_keys=missing,
                                 service=errors.keys_service(missing))
                    close()
                    return EXIT_CONFIG
            rec = ledger.find_step(doc, sid) or {}
            attempts = int(rec.get("attempts") or 0) + 1
            ledger.upsert_step(doc, sid, name=steps[sid]["name"], status=STATUS_RUNNING,
                               attempts=attempts, message="", code_version=code_version)
            ledger.save_atomic(root, _touch(doc))
            emitter.emit(events.EVENT_STEP_START, sid, f"{steps[sid]['name']}（第 {attempts} 次）",
                         name=steps[sid]["name"])
            t0 = time.time()
            ctx.reset_paid()
            try:
                result = steps_mod.STEPS[sid](ctx)
            except KeyboardInterrupt:
                raise
            except BaseException as e:
                # 引擎健壮性（ST1-P4b 方案端裁定）：StepError 之外，还捕获 SystemExit 及
                # KeyboardInterrupt 以外的 BaseException 子类，转为该步 failed + 退出码 2，
                # 保证输出 run_done（KeyboardInterrupt 已在上方 re-raise，仍走中断语义）。
                # ST3-P22：失败处理本身写不了盘（磁盘满）也要发出 run_done（_fail_step）。
                msg = str(e) if isinstance(e, StepError) else f"{type(e).__name__}: {e}"
                kind = errors.classify(e, sid)
                return _fail_step(emitter, root, doc, ctx, sid, msg, kind,
                                  t0, ct, close, service=errors.failure_service(e, kind))
            outs = ledger.expand(root, steps[sid]["out"])
            if not outs:
                msg = f"步骤未产出声明的产物: {steps[sid]['out']}"
                return _fail_step(emitter, root, doc, ctx, sid, msg,
                                  errors.KIND_RENDER if sid == "S10" else errors.KIND_INTERNAL,
                                  t0, ct, close)
            # S0 刚跑完 → 用权威判定同步账本 content_type（判不出时为 None，如实记 null）
            if ctx.resolved_content_type is not None:
                doc["content_type"] = ctx.resolved_content_type
            extra = {}
            if isinstance(result, dict) and result.get("source") is not None:
                # 步骤自报来源（S6 导入子项写明「←P7/P2/P4/P12」），逐条记进账本
                extra["source"] = result["source"]
            if ctx.paid_stats.get("stub_calls"):
                # 桩结果在账本 `source` 里标「桩」（ST1-P4a2 §三.A.4）
                prev = extra.get("source")
                extra["source"] = (f"{prev}；含桩调用 {ctx.paid_stats['stub_calls']} 次"
                                   if prev else f"桩调用 {ctx.paid_stats['stub_calls']} 次")
            if isinstance(result, dict) and result.get("jobs") is not None:
                # 步骤自己登记/清掉的异步任务号（豆包 TTS）：ledger 的 pending_jobs
                doc["pending_jobs"] = result["jobs"]
            if isinstance(result, dict) and result.get("image_service"):
                # ST3-P27：S8 本次实际用的出图服务（如 rh_app:2100836516310687745），事后追查用。
                extra["image_service"] = result["image_service"]
            if sid == "S10" and result:
                doc["delivery_path"] = str(result)
            if isinstance(result, dict) and result.get("original_coverage") is not None:
                # 原图覆盖自检（ST1-P4c C4）：S9 的结果写进账本，供收尾判退出码。
                doc["original_coverage"] = result["original_coverage"]
            if isinstance(result, dict) and result.get("command_still_hits") is not None:
                # ST3-F1 第6项：S1 命令重写后仍命中 → 记入账本，供收尾并入退出码 1 标红。
                doc["command_still_hits"] = result["command_still_hits"]
            if isinstance(result, dict) and result.get("alignment_sig_before") is not None:
                # ST3-P28b：S4 重算前的对齐签名，供付费步骤判"对齐是否真的换了"。
                extra["alignment_sig_before"] = result["alignment_sig_before"]
            if (sid in spec.alignment_readers(ctx.resolved_content_type or ct)
                    and (root / "04-对齐" / "alignment.json").is_file()):
                # ST3-P28b：记下这一步用的是哪一份对齐（续跑时判"对齐换了没"用）。
                from pipeline import align_global
                extra["alignment_sig"] = align_global.alignment_signature(root)
            ledger.upsert_step(doc, sid, status=STATUS_DONE,
                               input_fingerprint=_fingerprint(root, sid, steps),
                               outputs=ledger.outputs_record(root, outs),
                               duration_sec=round(time.time() - t0, 1),
                               message=(result.get("message") if isinstance(result, dict) else "") or "",
                               **extra, **_paid_fields(ctx, doc, sid))
            ledger.save_atomic(root, _touch(doc))
            ran.append(sid)
            ctx.progress_cb.finish(sid)
            # R101 G：S0 的 step_done 带上判定出的内容类型（APP 据此把流程条换成 11 步，S6 的事件才不被丢掉）
            s0_type = ({"content_type": ctx.resolved_content_type} if sid == "S0" else {})
            emitter.emit(events.EVENT_STEP_DONE, sid, f"{len(outs)} 个产物",
                         name=steps[sid]["name"], outputs=outs,
                         duration_sec=round(time.time() - t0, 1),
                         paid_calls=ctx.paid_stats["paid_calls"],
                         cache_hits=ctx.paid_stats["hits"],
                         estimated_cny=round(ctx.paid_stats.get("real_cny", 0.0), 4), **s0_type)
            # S0 权威判定后按实际内容类型重排后续步骤（ST1-P4b 方案端裁定：不接受两趟跑）。
            # 预览类型 ct 在启动时按 None 列的是理论线（无 S6）；S0 真跑判为截图型后，
            # 这里把后续步骤重排成截图线，账本 steps 数组补上新增步骤（如 S6，状态 pending），
            # 不删除、不改动任何已完成步骤的记录。`order[:]` 原地替换使外层 for 循环
            # 从下一项起按新顺序推进（S6 位于 S5 与 S7 之间）。
            if sid == "S0" and ctx.resolved_content_type not in (None, ct):
                ct = ctx.resolved_content_type
                steps = spec.steps_for(ct)
                new_order = spec.order_for(ct)
                for s in new_order:
                    if ledger.find_step(doc, s) is None:
                        ledger.upsert_step(doc, s, name=steps[s]["name"],
                                           status=ledger.STATUS_PENDING)
                ledger.save_atomic(root, _touch(doc))
                res, err = _survey(root, args, ct, steps, new_order, doc)
                if err:
                    emitter.emit(events.EVENT_RUN_DONE, None, f"配置错误：{err}",
                                 exit_code=EXIT_CONFIG, reason=REASON_CONFIG_ERROR,
                                 error_kind=errors.KIND_INTERNAL)
                    close()
                    return EXIT_CONFIG
                order[:] = new_order
                _apply_narration_flags(res, order, lock, unlock, args.accept_narration_edit)
                if auto_unlocked:
                    _respect_to(res, order, args)
                keys_checked = False     # 新插进来的步骤（S6）可能要别的密钥
    except KeyboardInterrupt as e:
        interrupted = True
        ctx_log("[引擎] " + ("发现停止标记" if isinstance(e, stopflag.StopRequested) else "收到中断信号")
                + "，当前步骤保持未完成")

    if interrupted:
        _safe_save(root, doc, ctx_log)
        emitter.emit(events.EVENT_RUN_DONE, None, "已中断：当前步骤未完成，下次运行重跑该步",
                     interrupted=True, exit_code=EXIT_INTERRUPT,
                     error_kind=errors.KIND_INTERRUPTED)
        close()
        return EXIT_INTERRUPT

    # 跳过步骤的输入指纹留空（导入步骤的上游当时还没产出）→ 本轮补记
    for sid in order:
        if res[sid][0] != "skip":
            continue
        rec = ledger.find_step(doc, sid)
        if rec and rec.get("status") in SKIPPABLE and rec.get("input_fingerprint") is None:
            rec["input_fingerprint"] = _fingerprint(root, sid, steps)
    # ST3-P14：检查结果过期判定（S9 / S10 的结果，上游在它之后重跑过就不再作数）。
    stale_notes = _mark_stale_checks(doc, ctx.resolved_content_type or ct, order, ran)
    ledger.save_atomic(root, _touch(doc))
    report_json = _sync_check_json_stale(root, doc)
    ctx_log(f"[引擎] 完成：日志 {log_path}")
    for n in stale_notes:
        ctx_log(f"[引擎] {n}")
    # 原图覆盖自检（ST1-P4c C4）：缺图时已出片但检查标红，退出码 1。
    # ST3-F1 第6项：口播命令重写后仍命中，同样并入退出码 1 标红清单。
    # ST3-P14：退出码 1 只来自仍然有效的检查结果（过期的见 stale_notes，只写进消息）。
    cov = doc.get("original_coverage") or {}
    missing = [] if cov.get("stale") else (cov.get("missing") or [])
    rc = doc.get("render_checks") or {}
    frame_missing = [] if rc.get("stale") else (rc.get("frame_missing") or [])
    audio_reds = [] if rc.get("stale") else ((rc.get("audio") or {}).get("reds") or [])
    still_cmds = doc.get("command_still_hits") or []
    ill_missing = [] if cov.get("stale") else (cov.get("illustration_missing") or [])
    problems = []
    if ill_missing:
        problems.append(f"插画缺失 {len(ill_missing)} 张 {', '.join(ill_missing)}")
    if missing:
        problems.append(f"{len(missing)} 张缺失 {', '.join(missing)}")
    if frame_missing:
        problems.append(f"要素未上屏 {len(frame_missing)} 个 {', '.join(frame_missing)}")
    if audio_reds:
        problems.append("成片声音与本次配音不符")
    if still_cmds:
        problems.append(f"口播仍念命令 {len(still_cmds)} 句")
    exit_code = EXIT_CHECK if problems else EXIT_OK
    msg = ("运行结束（检查标红：" + "；".join(problems) + "）" if problems else "运行结束")
    if stale_notes:
        msg += "（" + "；".join(stale_notes) + "）"
    emitter.emit(events.EVENT_RUN_DONE, None, msg,
                 article=args.article, content_type=ctx.resolved_content_type or ct,
                 delivery_path=doc.get("delivery_path"),
                 totals=doc.get("totals"), report_json=report_json, exit_code=exit_code)
    run_success["ok"] = True   # 退出码 0 / 1 都算成功结束（v5.6）
    close()
    return exit_code


def _sync_check_json_stale(root, doc):
    """ST3-P23 §3.8：把账本里的过期标记同步进 `10-报告/检查结果.json`（只改 stale 两栏，
    检查报告.md 不动），返回这个文件的路径；文件不存在（老文章没跑过新版 S9/S10）返回 None。"""
    p = Path(root) / steps_mod.CHECK_JSON_REL
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        stale = {}
        for key, prod, _ in _CHECK_RESULTS:
            if (doc.get(key) or {}).get("stale") and (key != "render_checks" or d.get("rendered")):
                stale[key] = doc[key]["stale"]
        if bool(stale) != bool(d.get("stale")) or (stale or None) != d.get("stale_detail"):
            d["stale"], d["stale_detail"] = bool(stale), (stale or None)
            steps_mod.write_check_json(root, d)
    except Exception:
        pass
    return str(p)


class _ProgressThrottle:
    """步骤内进度（ST3-P23 §3.6）：同一步最多每秒发一次 `step_progress`，和上一条相同的不重发；
    `done >= total` 的那条一定发。
    步骤成功结束时（`finish`）补发节流掉的最后一条；最后发出的仍不是 done == total 时，按它的
    total 补一条 done == total（这一步已经做完）。"""

    def __init__(self, emitter, interval=1.0, clock=time.monotonic):
        self._em, self._interval, self._clock = emitter, interval, clock
        self._last_t = {}      # step -> 上次发出的时间
        self._last = {}        # step -> 上次发出的那条
        self._pending = {}     # step -> 节流掉的最新一条

    def __call__(self, step, done, total, unit, message=None):
        stopflag.check()           # P31b：步骤内进度回调顺带看停止标记（S10 渲染时每 0.2 秒一次）
        rec = {"done": int(done), "total": int(total), "unit": unit, "message": message or ""}
        if rec == self._last.get(step):
            return                 # 和上一条一模一样（例如渲染百分比没动）：不重发
        if rec["done"] >= rec["total"] or self._clock() - self._last_t.get(step, -1e9) >= self._interval:
            self._send(step, rec)
        else:
            self._pending[step] = rec

    def _send(self, step, rec):
        self._pending.pop(step, None)
        self._last_t[step] = self._clock()
        self._last[step] = rec
        self._em.emit(events.EVENT_STEP_PROGRESS, step, rec["message"],
                      done=rec["done"], total=rec["total"], unit=rec["unit"])

    def note(self, step, message, **extra):
        """不经节流直接发一条提示（ST3-P26 网络重试用）：done/total/unit 沿用这一步上一条进度，
        不改节流状态。这一步还没发过进度时按 0/0。"""
        last = self._pending.get(step) or self._last.get(step) or {"done": 0, "total": 0, "unit": ""}
        self._em.emit(events.EVENT_STEP_PROGRESS, step, message,
                      done=last["done"], total=last["total"], unit=last["unit"], **extra)

    def finish(self, step):
        rec = self._pending.pop(step, None)
        if rec is not None:
            self._send(step, rec)
        last = self._last.get(step)
        if last is not None and last["done"] < last["total"]:
            self._send(step, dict(last, done=last["total"]))


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def _add_common(p, with_run):
    p.add_argument("--work-root", required=True, help="工作根目录")
    if with_run:
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--article", default=None, help="文章文件夹名")
        g.add_argument("--input", dest="input_src", default=None,
                       help="飞书链接，或本地的文件夹 / .zip / .md 文件（导入或复用；与 --article 二选一）")
    else:
        p.add_argument("--article", required=True, help="文章文件夹名")
    p.add_argument("--delivery-root", default=None, help="成片交付根目录")
    p.add_argument("--tmp-dir", default=None, help="临时目录（TEMP/TMP 指向它）")
    p.add_argument("--runtime-dir", default=None,
                   help=r"运行时副本目录（其下有 node_modules）；默认 <work-root 的上一级>\_runtime")
    if with_run:
        _add_import_opts(p)
        p.add_argument("--from", dest="from_step", default=None, help="从该步骤开始（含下游）")
        p.add_argument("--to", dest="to_step", default=None, help="跑到该步骤即停（其后步骤跳过）")
        p.add_argument("--no-paid", action="store_true", help="禁止付费调用")
        p.add_argument("--unlock-narration", action="store_true",
                       help="删除定稿锁，按正常流程重新生成口播/分组/配音")
        p.add_argument("--accept-narration-edit", action="store_true",
                       help="保留当前 narration.txt（人工定稿），只重跑分组与配音后重新落锁")
        p.add_argument("--stub", action="store_true",
                       help="【仅开发测试】用桩代替真实付费调用（不代表真实结果，不许出片）")
        p.add_argument("--max-cost-cny", dest="max_cost_cny", type=float, default=None,
                       help="本篇费用上限（元）。不传读环境变量 A2V_MAX_COST_CNY；"
                            f"再不传用内置默认 {DEFAULT_MAX_COST_CNY}。")


def _add_import_opts(p):
    p.add_argument("--refetch", action="store_true",
                   help="配合 --input：飞书=复用已导入文件夹时重新抓取原文；"
                        "本地 md=强制把 md 和图片重新同步进原文件夹（不新建文件夹）")
    p.add_argument("--content-type", dest="content_type", default="auto",
                   choices=intake.CONTENT_TYPE_CHOICES,
                   help="配合 --input：文章类型。auto=自动判断（有图要付费识图）；"
                        "theory/screenshot=指定类型，写进来源.json，S0 不识图")
    p.add_argument("--title-chars", dest="title_chars", type=int,
                   default=intake.DEFAULT_TITLE_CHARS,
                   help=f"配合 --input：新文件夹名里标题取前几个字符"
                        f"（{intake.TITLE_CHARS_MIN}～{intake.TITLE_CHARS_MAX}，"
                        f"默认 {intake.DEFAULT_TITLE_CHARS}）")


class _ArgError(Exception):
    """argparse 参数解析失败（未知参数/缺必填/数值非法），携带错误文本。"""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class _Parser(argparse.ArgumentParser):
    """把 argparse 的 `error` 从「打印 + sys.exit(2)」改成抛 `_ArgError`，
    使 main 能统一按退出码 4 + 单条 run_done 处理（ST1-P5a F）。"""

    def error(self, message):
        raise _ArgError(message)


def build_parser():
    ap = _Parser(prog="article2video",
                 description="自动视频总装线引擎（python -m pipeline.engine）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add_common(sub.add_parser("run", help="执行（可续跑）"), True)
    _add_common(sub.add_parser("plan", help="只读：列出每一步将执行还是跳过"), False)
    _add_common(sub.add_parser("status", help="只读：按账本汇报每一步状态"), False)
    ep = sub.add_parser("estimate", help="只读：开跑前的费用范围（不联网、不要密钥）")
    ep.add_argument("--work-root", required=True, help="工作根目录")
    ep.add_argument("--article", required=True, help="文章文件夹名")
    kp = sub.add_parser("check-keys", help="检测密钥是否可用（只调不计费的接口）")
    kp.add_argument("--services", default=None,
                    help=f"逗号分隔，可选 {','.join(keycheck.SERVICES)}；默认全部")
    ip = sub.add_parser("import", help="只导入并检查输入，不执行任何步骤")
    ip.add_argument("--work-root", required=True, help="工作根目录")
    ip.add_argument("--input", dest="input_src", required=True,
                    help="飞书链接，或本地的文件夹 / .zip / .md 文件（导入或复用）")
    _add_import_opts(ip)
    return ap


def main(argv=None):
    # 事件流固定 UTF-8：stdout 被重定向到文件时 Python 会用系统本地编码（cp936），
    # 那样事件文件就不是合法 UTF-8 了。日志同理。必须在 parse_args 之前——
    # `--help` 是 argparse 自己 print 的，晚了就还是 cp936。
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    try:
        args = build_parser().parse_args(argv)
    except _ArgError as e:
        # ST1-P5a F：参数解析失败一律退出码 4，且 stdout 只有一条 run_done。
        emitter = events.Emitter(stream=sys.stdout)
        emitter.emit(events.EVENT_RUN_DONE, None, f"配置错误：{e.message}",
                     exit_code=EXIT_CONFIG, reason=REASON_ARGUMENT_ERROR)
        return EXIT_CONFIG
    except SystemExit as e:
        # --help / --version：argparse 已打印到 stdout，正常退出（0）。
        code = e.code if isinstance(e.code, int) else 1
        return code if code in (0, None) else EXIT_CONFIG
    real_out = sys.stdout
    emitter = events.Emitter(stream=real_out)
    _install_interrupt_handlers()
    try:
        return _dispatch(args, emitter)
    except KeyboardInterrupt:
        _release_after_crash(args)
        emitter.emit(events.EVENT_RUN_DONE, None, "已中断", interrupted=True,
                     exit_code=EXIT_INTERRUPT, error_kind=errors.KIND_INTERRUPTED)
        return EXIT_INTERRUPT
    except Exception as e:
        # ST3-P22：步骤之外的意外（典型是磁盘写满时写账本/日志失败）也要发出 run_done，
        # 不能以 Python 默认的退出码 1 静默退出。
        _release_after_crash(args)
        kind = errors.classify(e)
        emitter.emit(events.EVENT_RUN_DONE, None, f"执行失败：{type(e).__name__}: {e}",
                     exit_code=EXIT_EXEC, error_kind=kind, service=errors.failure_service(e, kind))
        return EXIT_EXEC


def _release_after_crash(args):
    """只清本进程自己拿的运行锁（别的进程正在跑同一篇时不能动它的锁）。"""
    if args.cmd != "run" or not getattr(args, "article", None):
        return
    root = Path(args.work_root) / args.article
    try:
        d = json.loads((root / _LOCK_FILENAME).read_text(encoding="utf-8"))
    except Exception:
        return
    if d.get("pid") == os.getpid():
        _release_run_lock(root)


def _dispatch(args, emitter):
    if args.cmd == "import":
        return cmd_import(args, emitter)
    if args.cmd == "estimate":
        return cmd_estimate(args, emitter)
    if args.cmd == "check-keys":
        return cmd_check_keys(args, emitter)
    if args.cmd == "run":
        code = _resolve_input_source(args, emitter)
        if code is not None:
            return code
    if args.cmd == "plan":
        return cmd_plan(args, emitter)
    if args.cmd == "status":
        return cmd_status(args, emitter)
    return cmd_run(args, emitter)
