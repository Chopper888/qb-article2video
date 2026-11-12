# -*- coding: utf-8 -*-
"""R120M P1：MOSS 本地出图（用户电脑上的 ComfyUI）；R120M3：MOSS · RH 云端（RunningHub AI 应用）。
R121M：4 种风格（新增 S4 扁平矢量）、工作流 v2（1360×768 出图 → 4x 放大 → 1920×1080）、
MOSS · RH 自动并发（起始 3，遇到 RunningHub 并发 / 队列已满降为 1），不再有"RH 同时出图数"设置。

S8 只在出图方式为 MOSS（`A2V_IMAGE_BACKEND=moss_local` / `moss_rh`）时用到本模块；出图方式为 GPT（默认）时
S8 / plan / estimate / 缓存键一概不碰这里的任何东西（`config()` 只读环境变量，不 import 任何第三方库）。

设置（APP 设置页"出图"一组写进普通设置，经环境变量传给引擎；命令行同样可用）：

  - `A2V_IMAGE_BACKEND`      ：`gpt`（默认，RunningHub）/ `moss_local`（本地 ComfyUI）/ `moss_rh`（RunningHub 上的 MOSS 应用）
  - `A2V_MOSS_STYLE`         ：`S0` 手绘线稿（默认）/ `S3` 科技霓虹 / `S1` 日系水彩 / `S4` 扁平矢量（两种 MOSS 都用）
  - `A2V_COMFYUI_URL`        ：默认 `http://127.0.0.1:8188`（只对 MOSS 本地有效）
  - `A2V_MOSS_FALLBACK_GPT`  ：`1`（默认）MOSS 3 张都不合格时用 GPT 补这一张；`0` 用缺字最少的一张并标红
  - （R121M 起不再读 `A2V_MOSS_RH_CONCURRENCY`：MOSS · RH 同时出图数自动调节，见 `RHSlots`）

单张流程（任务书 §2.3）：

    labels = filter_labels(labels_of(提示词))  # 「」内文字，第一个为主标题；R121M：去掉画不出的编号 / 标点
    labels 为空或多于 8 个                 → 这一张走 GPT（"按规则走 GPT"）
    scene, carrier = default_scene(映射后的插画类型)
    layout = build_layout(labels, 风格, scene, carrier, W=1360, H=768)
    第 1、2、3 个种子依次（最多 MOSS_MAX_CANDIDATES 张）：提交出图 → 取图 → ocr_check（本机判图）
        合格就用这张，后面的种子不再出
    3 张都不合格：补图开关开 → GPT 出这一张；关 → 用缺字最少的一张，检查报告标红

"提交出图"：MOSS 本地 = 本机 ComfyUI（`ComfyClient`，串行）；MOSS · RH = RunningHub AI 应用
（`RHMossClient`，提交 / 查询 / 取图复用 `imagegen` 里 GPT 出图那套 RunningHub 调用；同时最多
`RH_AUTO_CONCURRENCY` 个任务，遇到并发 / 队列已满本次运行降为 1，见 `RHSlots`）。

R120M3 §3：MOSS 方式下要用 GPT 出的图（3 张都不合格且开了补图、或标注文字多于 8 个按规则走 GPT），
提示词改用"仿 MOSS 画风"的写法（`gpt_prompt()`：`moss_gpt_style.json` 的画风描述 + 画面内容 + 画面文字），
缓存键按这段完整提示词算，与普通 GPT 图分开。GPT 方式完全不碰这些。

Hermes 交付的两个模块（`pipeline/vendor/moss/layout_rules.py`、`ocr_gate.py`）与工作流 JSON 原样放在
vendor 里、按文件路径装载（不改它们的任何一行，也不往 sys.path 里加目录）。`ocr_gate` 依赖的
rapidocr-onnxruntime / onnxruntime 只在真正判图时才 import。
"""
import hashlib
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from pipeline.engine import errors, paths
from pipeline.engine.errors import StepError

# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------

BACKEND_ENV = "A2V_IMAGE_BACKEND"
STYLE_ENV = "A2V_MOSS_STYLE"
COMFYUI_URL_ENV = "A2V_COMFYUI_URL"
FALLBACK_ENV = "A2V_MOSS_FALLBACK_GPT"

BACKEND_GPT = "gpt"
BACKEND_MOSS = "moss_local"
BACKEND_MOSS_RH = "moss_rh"
BACKENDS = (BACKEND_GPT, BACKEND_MOSS, BACKEND_MOSS_RH)
MOSS_BACKENDS = (BACKEND_MOSS, BACKEND_MOSS_RH)
BACKEND_NAMES = {BACKEND_GPT: "GPT（RunningHub）", BACKEND_MOSS: "MOSS 本地（ComfyUI）",
                 BACKEND_MOSS_RH: "MOSS · RH 云端"}
# R121M §6：MOSS · RH 自动并发——一次运行开始时同时最多提交 3 个任务（不同插画之间并发；同一张插画的
# 候选仍按顺序）。RunningHub 回"并发 / 队列已满"时本次运行余下部分降为 1，被拒的任务等一会儿重新提交。
RH_AUTO_CONCURRENCY = 3
RH_BUSY_RETRY_SEC = 30.0              # 被拒后等多久重新提交（官方 415 建议 30～120 秒后重试）
RH_BUSY_MAX_WAIT_SEC = 1800.0         # 同一个候选被拒累计等这么久仍提交不上：整步停下（提示稍后重跑）
# MOSS · RH 每张候选图大约消耗的 RH 币（实测约 73 币 / 张；RH 币多为平台赠送，不计入人民币费用）。
RH_COINS_PER_IMAGE = 73
# R121M：正式版 4 种风格（S2 漫画网点合格率偏低，暂不开放）。风格的定义以 vendor 里的 layout_rules.py 为准。
STYLES = ("S0", "S3", "S1", "S4")
STYLE_NAMES = {"S0": "手绘线稿", "S3": "科技霓虹", "S1": "日系水彩", "S4": "扁平矢量"}
DEFAULT_STYLE = "S0"
DEFAULT_COMFYUI_URL = "http://127.0.0.1:8188"

# R121M §5：工作流 v2 = v1 + 官方放大链（节点 60/61/62：4x-UltraSharp 放大 → lanczos 缩到 1920×1080）。
# 工作流标识进缓存键；Hermes 定稿版到了直接覆盖同名文件（输入节点 26 / 52 不变）。
WORKFLOW_VERSION = "moss-pure-v2"
WORKFLOW_FILE = "MOSS-纯出图版-v2-API格式.json"
CANVAS_W, CANVAS_H = 1360, 768        # build_layout 的画布：必须与工作流节点 50 / 51 一致（Hermes 说明 §八）
NODE_CANVAS_W, NODE_CANVAS_H = "50", "51"
MAX_LABELS = 8
# R120M3 §2：每张图最多出几个候选（种子 s、s+1、s+2），任一合格即停；写进内容键、明细和检查报告。
MOSS_MAX_CANDIDATES = 3
SEED_COUNT = MOSS_MAX_CANDIDATES      # 旧名（R120M），同一个值
CANDIDATE_TIMEOUT_SEC = 300           # 单张超时（超时视为该候选不合格）
RH_CANDIDATE_TIMEOUT_SEC = 900        # MOSS · RH 单张超时（实测约 1 分钟；含排队，放宽到 15 分钟）
POLL_SEC = 2.0
NODE_LAYOUT = "26"                    # 对外输入①：Layout JSON（inputs.value）
NODE_SEED = "52"                      # 对外输入②：随机种子（inputs.value）

# 模型文件：节点 → (加载器类型, 字段名, 去哪里下载)。链接见 Hermes《说明.md》§七；
# R121M：放大模型 4x-UltraSharp（说明.md 里没有下载链接，按任务书写"在 ComfyUI 管理器里搜索安装"）。
MODEL_NODES = {
    "29": ("UNETLoader", "unet_name", "https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT"),
    "31": ("CLIPLoader", "clip_name", "https://huggingface.co/Qwen/Qwen3.5-9B"),
    "33": ("VAELoader", "vae_name", "https://huggingface.co/black-forest-labs/FLUX.2-dev"),
    "60": ("UpscaleModelLoader", "model_name", "在 ComfyUI 管理器里搜索 4x-UltraSharp 安装"),
}
MODEL_DIRS = {"UNETLoader": "models/diffusion_models（或 models/unet）",
              "CLIPLoader": "models/text_encoders（或 models/clip）",
              "VAELoader": "models/vae",
              "UpscaleModelLoader": "models/upscale_models"}

# 引擎插画类型（`出图需求.json` 的 `插画类型`，即决策记录的 path）→ `layout_rules.default_scene()` 认识的类型。
# 前 7 个与 Hermes 的 TYPE_SCENE 同名；后 4 个是引擎里有、Hermes 模板里没有的，按画面最接近的模板映射。
# 表里没有的类型交给 default_scene 的兜底（generic兜底）。
TYPE_MAP = {
    "标题→轻量模板": "标题→轻量模板",
    "Type:对比": "Type:对比",
    "Type:流程图": "Type:流程图",
    "Type:框架": "Type:框架",
    "Type:场景": "Type:场景",
    "generic兜底": "generic兜底",
    "空档插画": "空档插画",
    "标注空档插画": "空档插画",        # 旧版（ST3-P12）留下的空档插画需求
    "Type:信息图": "Type:框架",         # 信息卡片 ≈ 一叠卡片 + 简单图形
    "Type:时间轴": "Type:流程图",       # 按时间推进 ≈ 窗口 + 进度条
    "开篇导览模板": "Type:框架",        # 每个话题一张卡片
}


class ConfigError(ValueError):
    """出图设置的值不认识（cli 按参数错误处理，退出码 4）。"""


class Config:
    def __init__(self, backend=BACKEND_GPT, style=DEFAULT_STYLE, url=DEFAULT_COMFYUI_URL, fallback=True):
        self.backend, self.style, self.url, self.fallback = backend, style, url, fallback

    @property
    def moss(self):
        """两种 MOSS（本地 / RH 云端）都算。"""
        return self.backend in MOSS_BACKENDS

    @property
    def rh(self):
        return self.backend == BACKEND_MOSS_RH

    def signature(self):
        """影响出图结果的设置（S8 记在 `07-出图/出图方式.json`，变了就重跑 S8）。GPT 方式为 None。
        ComfyUI 地址不影响结果，不算进来。"""
        if not self.moss:
            return None
        return {"backend": self.backend, "style": self.style, "fallback_gpt": bool(self.fallback),
                "workflow": WORKFLOW_VERSION, "max_candidates": MOSS_MAX_CANDIDATES,
                "gpt_style": gpt_style_digest()}

    def describe(self):
        if not self.moss:
            return BACKEND_NAMES[BACKEND_GPT]
        where = "RunningHub" if self.rh else "ComfyUI"
        return (f"{BACKEND_NAMES[self.backend]}（{where}，{self.style} {style_name(self.style)}，"
                f"不合格{'用 GPT 补图' if self.fallback else '不补图'}）")


def style_name(style):
    return STYLE_NAMES.get(style, style)


def config(environ=None):
    """读出图设置。值写错抛 ConfigError；没设的按默认（GPT）。"""
    env = os.environ if environ is None else environ
    backend = (env.get(BACKEND_ENV) or "").strip().lower() or BACKEND_GPT
    if backend not in BACKENDS:
        raise ConfigError(f"{BACKEND_ENV}={env.get(BACKEND_ENV)!r} 不认识（只能是 gpt、moss_local 或 moss_rh）")
    style = (env.get(STYLE_ENV) or "").strip().upper() or DEFAULT_STYLE
    if style not in STYLES:
        raise ConfigError(f"{STYLE_ENV}={env.get(STYLE_ENV)!r} 不认识（只能是 {'、'.join(STYLES)}）")
    url = (env.get(COMFYUI_URL_ENV) or "").strip().rstrip("/") or DEFAULT_COMFYUI_URL
    if not url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"{COMFYUI_URL_ENV}={env.get(COMFYUI_URL_ENV)!r} 不是 http:// 地址")
    fb = (env.get(FALLBACK_ENV) or "").strip().lower()
    if fb in ("", "1", "true", "on", "yes"):
        fallback = True
    elif fb in ("0", "false", "off", "no"):
        fallback = False
    else:
        raise ConfigError(f"{FALLBACK_ENV}={env.get(FALLBACK_ENV)!r} 不认识（只能是 1 或 0）")
    # R121M §4.3：旧版的 A2V_MOSS_RH_CONCURRENCY 不再读取（APP 旧设置里存的值传进来也不报错）。
    return Config(backend, style, url, fallback)


def selected_or_gpt():
    """出图设置；写错时按 GPT 处理（只给跳过判定 / 估算用——cli 开跑前已经按参数错误拦下）。"""
    try:
        return config()
    except ConfigError:
        return Config()


# --------------------------------------------------------------------------
# Hermes 模块（原样，按文件路径装载）
# --------------------------------------------------------------------------

VENDOR_MOSS = paths.VENDOR_DIR / "moss"
_mods = {}


def _load(name):
    if name not in _mods:
        p = VENDOR_MOSS / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"a2v_moss_{name}", p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _mods[name] = mod
    return _mods[name]


def layout_rules():
    return _load("layout_rules")


def ocr_gate():
    return _load("ocr_gate")


def workflow():
    """工作流 API 格式（每次返回新的副本）。"""
    return json.loads((VENDOR_MOSS / WORKFLOW_FILE).read_text(encoding="utf-8"))


def node_types(wf=None):
    return sorted({n["class_type"] for n in (wf or workflow()).values()})


def output_size(wf=None):
    """R121M §5.2：工作流最终保存的图片尺寸（以工作流为准）。

    保存节点（SaveImage）的输入来自 ImageScale 时取它的 width / height（v2：1920×1080）；
    否则就是出图画布（节点 50 / 51，v1：1360×768）。认不出时按画布。"""
    wf = wf or workflow()
    try:
        canvas = (int(wf[NODE_CANVAS_W]["inputs"]["value"]), int(wf[NODE_CANVAS_H]["inputs"]["value"]))
    except (KeyError, TypeError, ValueError):
        canvas = (CANVAS_W, CANVAS_H)
    for n in wf.values():
        if n.get("class_type") != "SaveImage":
            continue
        src = (n.get("inputs") or {}).get("images")
        node = wf.get(str(src[0])) if isinstance(src, list) and src else None
        if node and node.get("class_type") == "ImageScale":
            try:
                return int(node["inputs"]["width"]), int(node["inputs"]["height"])
            except (KeyError, TypeError, ValueError):
                break
    return canvas


def model_files(wf=None):
    """[(节点号, 加载器类型, 字段, 文件名, 下载链接)]（工作流里没有的节点跳过：v1 没有放大模型）。"""
    wf = wf or workflow()
    out = []
    for nid, (cls, field, link) in MODEL_NODES.items():
        if nid not in wf:
            continue
        out.append((nid, cls, field, wf[nid]["inputs"][field], link))
    return out


def build_graph(layout, seed):
    g = workflow()
    g[NODE_LAYOUT]["inputs"]["value"] = layout
    g[NODE_SEED]["inputs"]["value"] = int(seed)
    return g


# --------------------------------------------------------------------------
# R120M3 §3：GPT 补图仿 MOSS 画风（`moss_gpt_style.json`，明文、不进加密规则包，R120W 会直接改它）
# --------------------------------------------------------------------------

GPT_STYLE_FILE = paths.ENGINE_ROOT / "pipeline" / "engine" / "moss_gpt_style.json"
_GPT_STYLE_FIELDS = ("版本", "开头", "画风标题", "风格", "内容标题", "画面内容", "文字说明")


def _strip_notes(v):
    """去掉以下划线开头的注释字段（程序不读，改注释不影响缓存键）。"""
    if isinstance(v, dict):
        return {k: _strip_notes(x) for k, x in v.items() if not str(k).startswith("_")}
    return v


def gpt_style():
    """读 `moss_gpt_style.json`（每次现读：人工改了文件马上生效）。缺字段 / 格式坏了抛 ConfigError。"""
    try:
        doc = json.loads(GPT_STYLE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"{GPT_STYLE_FILE.name} 读不出来：{e}") from e
    doc = _strip_notes(doc)
    miss = [k for k in _GPT_STYLE_FIELDS if k not in doc]
    for st in STYLES:
        if not isinstance((doc.get("风格") or {}).get(st), dict) or not doc["风格"][st].get("画风描述"):
            miss.append(f"风格.{st}.画风描述")
    if miss:
        raise ConfigError(f"{GPT_STYLE_FILE.name} 缺字段：{'、'.join(miss)}")
    return doc


def gpt_style_digest():
    """画风文件里程序用到的内容的指纹（进出图设置签名：R120W 改了画风描述，S8 会重跑、补的图按新提示词重出）。"""
    try:
        doc = gpt_style()
    except ConfigError:
        return None
    return hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def gpt_prompt(labels, style, scene_key):
    """MOSS 方式下给 GPT 的提示词：画风描述 + 画面内容（MOSS 模板的中文意译）+ 画面文字（逐条，第一个为主标题）。"""
    doc = gpt_style()
    scenes = doc["画面内容"]
    scene = scenes.get(scene_key) or scenes.get(DEFAULT_SCENE_FALLBACK) or ""
    lines = [doc["开头"],
             doc["画风标题"] + doc["风格"][style]["画风描述"],
             doc["内容标题"] + scene,
             doc["文字说明"]]
    lines += [f"{i}. 「{w}」" for i, w in enumerate(labels, 1)]
    return "\n".join(lines)


DEFAULT_SCENE_FALLBACK = "generic兜底"


# --------------------------------------------------------------------------
# 单张的规则：走不走 MOSS、版式、种子、缓存键
# --------------------------------------------------------------------------


def scene_type(illus_type, known=()):
    """引擎插画类型 → MOSS 模板键。R121M：版式规则自己有这个类型的模板（v3 新增 Type:信息图 /
    Type:时间轴）时直接用它，否则按 TYPE_MAP 映射到最接近的模板。"""
    t = illus_type or ""
    if t in known:
        return t
    return TYPE_MAP.get(t, t)


class Route:
    """一张插画在 MOSS 方式下怎么出。`moss=False` 时 `reason` 说明为什么按规则走 GPT。

    `gpt_prompt` / `gpt_key`：要用 GPT 出这一张时（补图、或按规则走 GPT）用的仿 MOSS 提示词与它的缓存键；
    为 None 时用出图需求里原来的提示词（没有标注文字、版式规则不接受的图）。"""

    def __init__(self, moss, labels, reason="", layout=None, seeds=(), item_key=None, scene_key="",
                 gpt_prompt=None, gpt_key=None):
        self.moss, self.labels, self.reason, self.layout = moss, labels, reason, layout
        self.seeds, self.item_key, self.scene_key = list(seeds), item_key, scene_key
        self.gpt_prompt, self.gpt_key = gpt_prompt, gpt_key

    def gpt_item(self, it):
        """原样出图需求项 → 实际交给 GPT 的那一项（提示词、缓存键换成仿 MOSS 的；没有就原样）。"""
        if not self.gpt_prompt:
            return it
        return dict(it, 提示词=self.gpt_prompt, 请求内容哈希=self.gpt_key)


def seeds_for(gpt_key):
    """种子由这一张的缓存键（GPT 请求内容哈希）确定性导出：第一个 = 前 8 位十六进制，后面依次 +1、+2。"""
    s = int(str(gpt_key)[:8], 16)
    return [s + i for i in range(SEED_COUNT)]


def candidate_key(layout, seed, style, backend=BACKEND_MOSS):
    """一张候选图的缓存键：Layout JSON + 种子 + 工作流版本 + 两个规则版本 + 风格（Hermes 建议的口径）
    + 后端标识（R120M3：`moss_local` / `moss_rh` 两边结果分开存——RH 上是 bf16 权重，与本机 int8 不完全相同）。"""
    lr, og = layout_rules(), ocr_gate()
    s = "|".join([WORKFLOW_VERSION, layout, str(int(seed)), lr.LAYOUT_RULES_VERSION,
                  og.OCR_GATE_VERSION, style, f"backend={backend}"])
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def ocr_key(cand_key, labels):
    og = ocr_gate()
    s = "|".join(["ocr", og.OCR_GATE_VERSION, cand_key, json.dumps(labels, ensure_ascii=False)])
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def item_key(gpt_key, layout, style, fallback, backend=BACKEND_MOSS, fallback_key=None):
    """这一张在 MOSS 方式下的"内容键"（记进 `图片记录.json`，判断旧图还能不能用）。

    含 GPT 请求内容哈希（种子由它导出）、补图用的仿 MOSS 提示词的键、版式、风格、补图开关、后端、
    候选数上限、工作流与规则版本；任何一项变了都按"内容变了"重做（重做时候选图与 GPT 图都先查缓存）。"""
    lr, og = layout_rules(), ocr_gate()
    s = "|".join(["moss-item", WORKFLOW_VERSION, lr.LAYOUT_RULES_VERSION, og.OCR_GATE_VERSION,
                  str(gpt_key), layout, style, "fallback=1" if fallback else "fallback=0",
                  f"backend={backend}", f"max_candidates={MOSS_MAX_CANDIDATES}",
                  f"fallback_key={fallback_key or ''}"])
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def route(item, cfg):
    """按任务书 §2.3 判定这一张走 MOSS 还是按规则走 GPT（R120M3：要用 GPT 时附上仿 MOSS 的提示词）。"""
    from pipeline.engine import paid
    lr = layout_rules()
    prompt = item.get("提示词") or ""
    raw = lr.labels_of(prompt)
    labels = raw
    if hasattr(lr, "filter_labels"):
        # R121M（规则 v3）：纯章节编号（`二、` `2.1`）、纯标点画不出来，版式与判图都只用剩下的
        # （Hermes 样片脚本同样先过滤再 build_layout / ocr_check）。
        labels = lr.filter_labels(raw)
    if not labels:
        return Route(False, labels, "提示词里没有「」标注文字" if not raw
                     else "「」标注文字都是编号或标点（画不出来）")
    sk = scene_type(item.get("插画类型"), lr.TYPE_SCENE)
    sk = sk if sk in lr.TYPE_SCENE else lr.DEFAULT_SCENE_KEY
    gp = gpt_prompt(labels, cfg.style, sk)
    gpk = paid.runninghub_key(gp)
    if len(labels) > MAX_LABELS:
        return Route(False, labels, f"标注文字 {len(labels)} 个，多于 {MAX_LABELS} 个", scene_key=sk,
                     gpt_prompt=gp, gpt_key=gpk)
    scene, carrier = lr.default_scene(sk)
    try:
        layout = lr.build_layout(labels, cfg.style, scene, carrier, W=CANVAS_W, H=CANVAS_H)
    except ValueError as e:
        return Route(False, labels, f"版式规则不接受：{e}")
    gk = item.get("请求内容哈希")
    return Route(True, labels, "", layout, seeds_for(gk),
                 item_key(gk, layout, cfg.style, cfg.fallback, cfg.backend, gpk), sk, gp, gpk)


# --------------------------------------------------------------------------
# ComfyUI 客户端（本地 HTTP；串行）
# --------------------------------------------------------------------------

SERVICE = errors.SERVICE_COMFYUI
DOWN_TEXT = "本地 ComfyUI 没有启动（或地址不对），请先启动 ComfyUI，或在设置里把出图方式切回 GPT"


class ComfyDown(StepError):
    kind = errors.KIND_COMFYUI_DOWN
    service = SERVICE


class SetupIncomplete(StepError):
    kind = errors.KIND_MOSS_SETUP
    service = SERVICE


class CandidateFailed(Exception):
    """这一张候选没出成（ComfyUI 执行报错 / 超时）：按"该候选不合格"处理。"""


class ComfyClient:
    def __init__(self, url, timeout=30):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.client_id = "a2v-engine"

    def _open(self, path, data=None, timeout=None):
        req = urllib.request.Request(self.url + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"Content-Type": "application/json"} if data is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ComfyDown(f"{DOWN_TEXT}（{self.url}：{type(e).__name__}: {getattr(e, 'reason', e)}）") from e

    def _json(self, path, data=None, timeout=None):
        st, raw = self._open(path, data, timeout)
        try:
            body = json.loads(raw.decode("utf-8", "replace") or "null")
        except ValueError:
            body = None
        return st, body, raw

    def object_info(self):
        st, body, raw = self._json("/object_info", timeout=60)
        if st != 200 or not isinstance(body, dict):
            raise ComfyDown(f"{DOWN_TEXT}（{self.url}/object_info 返回 HTTP {st}：{raw[:200]!r}）")
        return body

    def submit(self, graph):
        data = json.dumps({"prompt": graph, "client_id": self.client_id}, ensure_ascii=False).encode("utf-8")
        st, body, raw = self._json("/prompt", data=data, timeout=60)
        pid = body.get("prompt_id") if isinstance(body, dict) else None
        if st != 200 or not pid:
            # 工作流校验不过（节点 / 模型不对）：ComfyUI 回 400 + node_errors
            raise CandidateFailed(f"ComfyUI 拒绝了任务 HTTP {st}：{raw[:300].decode('utf-8', 'replace')}")
        return str(pid)

    def history(self, pid):
        st, body, _ = self._json(f"/history/{urllib.parse.quote(pid)}")
        if st != 200 or not isinstance(body, dict):
            return None
        return body.get(pid) or None

    def view(self, img):
        q = urllib.parse.urlencode({"filename": img.get("filename", ""), "subfolder": img.get("subfolder", ""),
                                    "type": img.get("type", "output")})
        st, raw = self._open(f"/view?{q}", timeout=180)
        if st != 200 or not raw:
            raise CandidateFailed(f"ComfyUI 取图失败 HTTP {st}")
        return raw

    def interrupt(self):
        try:
            self._open("/interrupt", data=b"{}", timeout=10)
        except StepError:
            pass

    def generate(self, graph, timeout=None, should_stop=None, sleep=time.sleep, clock=time.monotonic):
        """提交一个任务并等它出图，返回 PNG 字节。超时 / 执行失败抛 CandidateFailed。"""
        timeout = CANDIDATE_TIMEOUT_SEC if timeout is None else timeout
        pid = self.submit(graph)
        t0 = clock()
        while True:
            if should_stop is not None:
                should_stop()
            rec = self.history(pid)
            if rec:
                status = rec.get("status") or {}
                if status.get("status_str") not in (None, "success") or status.get("completed") is False:
                    msgs = [m for m in status.get("messages") or [] if m and m[0] == "execution_error"]
                    detail = (msgs[0][1].get("exception_message") if msgs and isinstance(msgs[0][1], dict)
                              else status.get("status_str"))
                    raise CandidateFailed(f"ComfyUI 执行失败：{detail}")
                for out in (rec.get("outputs") or {}).values():
                    for img in out.get("images") or []:
                        return self.view(img)
                raise CandidateFailed("ComfyUI 任务完成但没有输出图片")
            if clock() - t0 >= timeout:
                self.interrupt()
                raise CandidateFailed(f"等了 {int(timeout)} 秒还没出图（单张超时）")
            sleep(POLL_SEC)

    def candidate(self, layout, seed, should_stop=None):
        """出一张候选图：返回 (PNG 字节, 附加信息)。"""
        return self.generate(build_graph(layout, seed), should_stop=should_stop), {}


# --------------------------------------------------------------------------
# R120M3：MOSS · RH 云端（RunningHub AI 应用；提交 / 查询 / 取图复用 imagegen 的 RunningHub 调用）
# --------------------------------------------------------------------------

RH_SERVICE = errors.SERVICE_RUNNINGHUB
RH_SWITCH_HINT = "也可以在设置里切到 GPT 或 MOSS 本地"
RH_POLL_SEC = 5.0


class RHBusy(Exception):
    """R121M §6.2：RunningHub 回"并发 / 队列已满 / 超出同时任务数"（任务没建成）。

    不算候选失败、不换种子、不计 RH 币：调用方降并发后重新提交同一个候选。"""


class RHSlots:
    """R121M §6：MOSS · RH 同时在跑的任务数上限（起始 RH_AUTO_CONCURRENCY；被拒一次本次运行降为 1）。

    `hold()` 拿到名额才提交，任务出完（成功 / 失败 / 超时）才还；降级后已经在跑的任务照常跑完，
    新的提交要等在跑的数降到 1 以下。所有状态在锁里改，线程安全。"""

    def __init__(self, limit=RH_AUTO_CONCURRENCY, clock=time.time):
        self.start = int(limit)
        self.limit = int(limit)
        self.active = 0
        self.peak = 0
        self.degraded_at = None       # 降级时间（ISO 字符串）
        self.degraded_reason = None
        self.rejects = 0              # 被拒（并发 / 队列已满）后重新提交的次数
        self._clock = clock
        self._cv = threading.Condition()

    def acquire(self, should_stop=None, poll=0.5):
        with self._cv:
            while self.active >= self.limit:
                if should_stop is not None:
                    self._cv.release()
                    try:
                        should_stop()
                    finally:
                        self._cv.acquire()
                self._cv.wait(timeout=poll)
            self.active += 1
            self.peak = max(self.peak, self.active)

    def release(self):
        with self._cv:
            self.active -= 1
            self._cv.notify_all()

    def degrade(self, reason):
        """降为 1（只记第一次的时间与原因）；返回这次是不是刚降。"""
        with self._cv:
            self.rejects += 1
            first = self.limit > 1
            self.limit = 1
            if first:
                self.degraded_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._clock()))
                self.degraded_reason = reason
            self._cv.notify_all()
            return first

    def report(self):
        with self._cv:
            return {"起始并发": self.start, "最高同时": self.peak, "降级": self.degraded_at is not None,
                    "降级时间": self.degraded_at, "降级原因": self.degraded_reason,
                    "降级后并发": 1 if self.degraded_at else None, "被拒重提次数": self.rejects}


class RHMossClient:
    """MOSS · RH：每个候选图 = 提交一次 AI 应用任务（节点 26 = Layout JSON，节点 52 = 种子）。

    可以多线程同时用（每次调用各自提交、各自轮询，不共享状态）。提交失败（余额不足、密钥不对、
    服务错误……）抛 StepError，整步停下（提示里加一句可以切到 GPT 或 MOSS 本地）；任务失败、超时
    按"这个候选不合格"处理（CandidateFailed）。"""

    def __init__(self, api_key, log=None):
        from pipeline.engine import imagegen
        self.api_key, self.log = api_key, log
        self.url = f"RunningHub AI 应用 {imagegen.RH_MOSS_APP['webapp_id']}"

    def candidate(self, layout, seed, should_stop=None, sleep=time.sleep, clock=time.monotonic, timeout=None):
        from pipeline.engine import imagegen
        timeout = RH_CANDIDATE_TIMEOUT_SEC if timeout is None else timeout
        try:
            tid = imagegen.moss_app_submit(layout, seed, self.api_key, log=self.log, busy_exc=RHBusy)
        except RHBusy:
            raise
        except StepError as e:
            raise StepError(f"MOSS · RH 出图提交失败：{e}；{RH_SWITCH_HINT}",
                            kind=errors.classify(e, "S8"), service=RH_SERVICE) from e
        t0 = clock()
        while True:
            if should_stop is not None:
                should_stop()
            status, x, usage = imagegen.app_check(tid, self.api_key, log=self.log)
            if status == "success":
                return imagegen.download(x, log=self.log), {"task_id": tid, "usage": usage}
            if status == "fail":
                x = x or {}
                raise CandidateFailed(f"RunningHub 任务 {tid} 失败：{x.get('errorCode') or ''} "
                                      f"{x.get('errorMessage') or ''}".strip())
            if clock() - t0 >= timeout:
                raise CandidateFailed(f"RunningHub 任务 {tid} 等了 {int(timeout)} 秒还没出图（单张超时）")
            sleep(RH_POLL_SEC)


# --------------------------------------------------------------------------
# 出图前检查（不花钱）
# --------------------------------------------------------------------------


def _choices(info, cls, field):
    """object_info 里某个加载器字段的可选值列表；结构认不出返回 None。"""
    try:
        spec = info[cls]["input"]
        f = (spec.get("required") or {}).get(field) or (spec.get("optional") or {}).get(field)
        first = f[0]
        if isinstance(first, list):
            return [str(x) for x in first]
        if isinstance(first, str) and len(f) > 1 and isinstance(f[1], dict):   # 新版 COMBO 写法
            return [str(x) for x in f[1].get("options") or []]
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return None


def _norm_model(name):
    return str(name).replace("\\", "/").lower()


def preflight(client):
    """§2.2：ComfyUI 能连上、14 个节点类型都在、三个模型文件在加载器的可选值里。

    返回检查结果（写进日志与 `logs/出图明细.json`）；不通过时抛 ComfyDown / SetupIncomplete（早停）。"""
    wf = workflow()
    rep = {"url": client.url, "ok": False, "nodes_total": len(node_types(wf)), "models_total": len(model_files(wf)),
           "missing_nodes": [], "missing_models": [], "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        info = client.object_info()
    except ComfyDown as e:
        rep["error"] = str(e)
        raise ComfyDown(str(e), kind=errors.KIND_COMFYUI_DOWN, service=SERVICE) from e
    rep["missing_nodes"] = [c for c in node_types(wf) if c not in info]
    for nid, cls, field, fname, link in model_files(wf):
        if cls not in info:
            continue
        choices = _choices(info, cls, field)
        if choices is None:
            continue          # 认不出可选值结构：不拦（真提交时 ComfyUI 会报）
        if _norm_model(fname) not in {_norm_model(c) for c in choices}:
            rep["missing_models"].append({"node": nid, "loader": cls, "file": fname,
                                          "folder": MODEL_DIRS.get(cls, ""), "download": link})
    rep["ok"] = not rep["missing_nodes"] and not rep["missing_models"]
    if not rep["ok"]:
        e = SetupIncomplete(setup_text(rep), kind=errors.KIND_MOSS_SETUP, service=SERVICE)
        e.report = rep            # R121M：明细里写清缺哪个节点 / 哪个模型文件
        raise e
    return rep


def rh_preflight():
    """R120M3 §1.2 第 4 条：MOSS · RH 的出图前检查只看 RunningHub 密钥填了没有（不做 ComfyUI 检查）。"""
    from pipeline.engine import spec
    names = spec.PAID_KEYS.get("S8") or ["RUNNINGHUB_API_KEY"]
    rep = {"url": "RunningHub", "ok": False, "检查": "RunningHub 密钥已填写", "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    missing = [n for n in names if not (os.environ.get(n) or "").strip()]
    if missing:
        rep["error"] = f"没有填写 RunningHub 密钥（{'、'.join(missing)}）"
        raise StepError(f"MOSS · RH 出图要用 RunningHub 密钥，请先在设置里填写；{RH_SWITCH_HINT}",
                        kind=errors.KIND_KEY_MISSING, service=RH_SERVICE)
    rep["ok"] = True
    return rep


def setup_text(rep):
    parts = []
    if rep.get("missing_nodes"):
        parts.append(f"ComfyUI 缺少节点 {'、'.join(rep['missing_nodes'])}（ComfyUI 版本太旧，请更新 ComfyUI）")
    for m in rep.get("missing_models") or []:
        how = m["download"] if not str(m["download"]).startswith("http") else f"下载：{m['download']}"
        parts.append(f"缺模型文件 {m['file']}（放进 ComfyUI 的 {m['folder']}，{how}）")
    return "MOSS 本地出图还没准备好：" + "；".join(parts) + "。或在设置里把出图方式切回 GPT"


# --------------------------------------------------------------------------
# 判图
# --------------------------------------------------------------------------


def check_text(png_bytes, labels, tmp_dir):
    """ocr_check 只收文件路径：写一个临时文件判完删掉。返回 (ok, {"missing", "extra"})。"""
    og = ocr_gate()
    tmp_dir.mkdir(parents=True, exist_ok=True)
    p = tmp_dir / f"ocr-{hashlib.sha256(png_bytes).hexdigest()[:16]}.png"
    p.write_bytes(png_bytes)
    try:
        ok, d = og.ocr_check(str(p), labels)
    finally:
        try:
            p.unlink()
        except OSError:
            pass
    return bool(ok), {"missing": list(d.get("missing") or []), "extra": list(d.get("extra") or [])}


# --------------------------------------------------------------------------
# S8 用的单张执行器（主线程里串行：同一时间只向 ComfyUI 提交 1 个任务）
# --------------------------------------------------------------------------

CACHE_SUBDIR = "cache-moss"          # `07-出图/cache-moss`：与 GPT 的 `07-出图/cache` 分开
DETAIL_REL = "logs/出图明细.json"
SETTING_REL = "07-出图/出图方式.json"


class Runner:
    """一张插画的 MOSS 流程：候选图先查缓存，查不到才提交出图；判图结果也缓存（重跑不重复判）。

    MOSS 本地：在主线程里逐张出（同一时间只向 ComfyUI 提交 1 个任务）。
    MOSS · RH：`prefetch()` 用 RH_AUTO_CONCURRENCY 个线程同时出不同的图（每张图内部的候选仍按顺序，
    合格即停），同时在跑的 RH 任务数由 `self.slots` 管（被拒降为 1）；判图在本机做，多线程时排队判
    （RapidOCR 不保证线程安全）。
    桩模式（`--stub`）不连 ComfyUI / RunningHub、不判图：候选图用占位 PNG，按合格处理（不写缓存）。
    `--no-paid` 时 MOSS · RH 不提交（消耗 RH 币），缓存里没有的候选按禁止付费停下。"""

    def __init__(self, ctx, cfg, cache_dir, client=None):
        from pipeline.engine import paid, stubs   # noqa: F401（只在 S8 里用到）
        self.ctx, self.cfg, self.cache_dir = ctx, cfg, cache_dir
        if client is None:
            client = (RHMossClient(os.environ.get("RUNNINGHUB_API_KEY", ""), log=self._log) if cfg.rh
                      else ComfyClient(cfg.url))
        self.client = client
        self.slots = RHSlots() if cfg.rh else None
        self.busy_sleep = time.sleep          # 被拒后的等待（测试替换成不真等的）
        self.pre = None
        self.done = {}                 # prefetch 的结果：文件名 → run() 的返回值
        self._ocr_lock = threading.Lock()
        self._abort = threading.Event()

    @property
    def backend(self):
        return self.cfg.backend

    def _log(self, m):
        try:
            self.ctx.log(m)
        except Exception:
            pass

    def needs_service(self, r):
        """这一张按顺序走一遍候选时会不会真的提交出图（候选图与判图结果都在缓存里就不会）。"""
        from pipeline.engine import paid
        if self.ctx.stub or not r.moss:
            return False
        for s in r.seeds:
            ck = candidate_key(r.layout, s, self.cfg.style, self.backend)
            if not paid.load_cached(self.cache_dir, ck)[0]:
                return True
            h, v, _ = paid.load_cached(self.cache_dir, ocr_key(ck, r.labels))
            if not (h and isinstance(v, dict)):
                return False          # 图在缓存里、只差判图：不用出图服务
            if v.get("ok"):
                return False
        return False

    needs_comfy = needs_service        # 旧名（R120M）

    def ensure_ready(self):
        if self.pre is not None or self.ctx.stub:
            return self.pre
        if self.cfg.rh:
            self._log("[S8] MOSS · RH 出图前检查：RunningHub 密钥")
            try:
                self.pre = rh_preflight()
            except StepError as e:
                self.pre = {"url": "RunningHub", "ok": False, "error": str(e)}
                self._log(f"[S8] MOSS · RH 出图前检查不通过：{e}")
                raise
            self._log("[S8] MOSS · RH 出图前检查通过：RunningHub 密钥已填写")
            return self.pre
        self._log(f"[S8] MOSS 出图前检查：{self.client.url}")
        try:
            self.pre = preflight(self.client)
        except StepError as e:
            self.pre = dict(getattr(e, "report", None) or {"url": self.client.url, "ok": False}, error=str(e))
            self._log(f"[S8] MOSS 出图前检查不通过：{e}")
            raise
        self._log(f"[S8] MOSS 出图前检查通过：工作流用到的 {self.pre['nodes_total']} 种节点类型都在，"
                  f"{self.pre.get('models_total', len(model_files()))} 个模型文件都在（含放大模型）")
        return self.pre

    def _stop_check(self):
        from pipeline.engine import stopflag
        if self._abort.is_set():
            raise KeyboardInterrupt("另一张图出错，停止出图")
        stopflag.check()

    def prefetch(self, jobs):
        """MOSS · RH：最多 RH_AUTO_CONCURRENCY 张图同时出（jobs = [(Route, 文件名)]，按顺序提交）。
        结果放进 `self.done`，主线程按清单顺序取用。任何一张抛出整步要停的错误时，其余的在下一次查询时停下。
        R121M：同时在跑的任务数由 `self.slots` 控制（被拒降为 1），这里的线程数只是上限。"""
        jobs = [(r, f) for r, f in jobs if r.moss and f not in self.done]
        if not jobs:
            return
        n = self.slots.start if self.slots is not None else 1
        if n == 1 or len(jobs) == 1:
            for r, f in jobs:
                self.done[f] = self.run(r, f)
            return
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
        from pipeline.engine import stopflag
        self._log(f"[S8] MOSS · RH：{len(jobs)} 张图，自动并发：起始最多 {n} 个任务同时出"
                  f"（RunningHub 回并发 / 队列已满时降为 1）")
        first_err = []
        lock = threading.Lock()

        def one(r, f):
            if self._abort.is_set():
                return
            try:
                res = self.run(r, f)
                with lock:
                    self.done[f] = res
            except BaseException as e:       # noqa: BLE001  交回主线程
                with lock:
                    if not first_err:
                        first_err.append(e)
                self._abort.set()

        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="S8-MOSS-RH") as ex:
            futs = [ex.submit(one, r, f) for r, f in jobs]
            try:
                for fu in futs:
                    while True:
                        try:
                            fu.result(timeout=1.0)      # one() 自己接住异常，这里只会等超时
                            break
                        except FutTimeout:
                            stopflag.check()            # 主线程等结果的间隙看停止标记
            except BaseException:
                self._abort.set()
                raise
        if first_err:
            raise first_err[0]

    def _candidate(self, r, fname, i, seed):
        """出一张候选。MOSS · RH：先拿并发名额；被拒（并发 / 队列已满）→ 降为 1、等一会儿重新提交同一个
        种子（不算候选失败、不计 RH 币），累计等太久整步停下。"""
        if self.slots is None:
            return self.client.candidate(r.layout, seed, should_stop=self._stop_check)
        waited = 0.0
        while True:
            self.slots.acquire(should_stop=self._stop_check)
            try:
                return self.client.candidate(r.layout, seed, should_stop=self._stop_check)
            except RHBusy as e:
                first = self.slots.degrade(str(e))
                self._log(f"[S8] MOSS · RH {fname} 第 {i + 1} 张候选被 RunningHub 拒收（{e}）："
                          + ("本次运行余下部分降为同时 1 个任务；" if first else "")
                          + f"{int(RH_BUSY_RETRY_SEC)} 秒后重新提交（不算候选失败、不换种子）")
            finally:
                self.slots.release()
            if waited >= RH_BUSY_MAX_WAIT_SEC:
                raise StepError(f"MOSS · RH 出图：RunningHub 一直回并发 / 队列已满，{fname} 第 {i + 1} 张候选"
                                f"等了 {int(waited)} 秒仍提交不上；请稍后重跑，{RH_SWITCH_HINT}",
                                kind=errors.KIND_SERVICE, service=RH_SERVICE)
            t = 0.0
            while t < RH_BUSY_RETRY_SEC:
                self._stop_check()
                step = min(1.0, RH_BUSY_RETRY_SEC - t)
                self.busy_sleep(step)
                t += step
            waited += RH_BUSY_RETRY_SEC

    def concurrency_report(self):
        """检查报告 / 明细用：MOSS · RH 的并发情况（本地方式为 None）。"""
        return self.slots.report() if self.slots is not None else None

    def run(self, r, fname):
        """返回 {"ok", "png", "candidates": [...], "chosen": 第几张或 None}。prefetch 过的直接返回结果。"""
        if fname in self.done:
            return self.done[fname]
        from pipeline.engine import paid, stubs
        cands, best = [], None
        for i, seed in enumerate(r.seeds):
            self._stop_check()
            ck = candidate_key(r.layout, seed, self.cfg.style, self.backend)
            c = {"seed": seed, "key": ck}
            hit, png, _ = paid.load_cached(self.cache_dir, ck)
            c["cached"] = bool(hit)
            if not hit:
                if self.ctx.stub:
                    png = stubs.stub_generate_image(f"moss|{r.layout}|{seed}", size=output_size())
                else:
                    if self.cfg.rh and self.ctx.no_paid:
                        raise errors.PaidBlocked(f"禁止付费：MOSS · RH 出图 {fname} 第 {i + 1} 张候选"
                                                 f"（消耗 RH 币）（S8）")
                    self.ensure_ready()
                    t0 = time.monotonic()
                    self._log(f"[S8] MOSS{' · RH' if self.cfg.rh else ''} 出图 {fname}："
                              f"第 {i + 1} 张候选（种子 {seed}）")
                    if self.cfg.rh:
                        c["submitted"] = True          # 提交了就算消耗 RH 币（失败的任务不一定收，按上限估）
                    try:
                        png, info = self._candidate(r, fname, i, seed)
                    except CandidateFailed as e:
                        c.update(ok=False, error=str(e), missing=list(r.labels), extra=[])
                        cands.append(c)
                        self._log(f"[S8] MOSS {fname} 第 {i + 1} 张候选没出成：{e}")
                        continue
                    c["seconds"] = round(time.monotonic() - t0, 1)
                    if info.get("task_id"):
                        c["task_id"] = info["task_id"]
                    if info.get("usage") is not None:
                        c["usage"] = info["usage"]
                    paid.store_cached(self.cache_dir, ck, png, step="S8", service=self.backend,
                                      extra={"describe": f"MOSS 候选 {fname} 种子 {seed}", "seed": seed,
                                             "workflow": WORKFLOW_VERSION, "style": self.cfg.style,
                                             "backend": self.backend})
            if self.ctx.stub:
                ok, d = True, {"missing": [], "extra": []}
            else:
                ok_k = ocr_key(ck, r.labels)
                h2, v2, _ = paid.load_cached(self.cache_dir, ok_k)
                if h2 and isinstance(v2, dict) and "ok" in v2:
                    ok, d = bool(v2["ok"]), v2
                else:
                    with self._ocr_lock:
                        ok, d = check_text(png, r.labels, self.ctx.root / "07-出图" / "tmp-ocr")
                    paid.store_cached(self.cache_dir, ok_k, {"ok": ok, "missing": d["missing"],
                                                             "extra": d["extra"]},
                                      step="S8", service="moss_ocr")
            c.update(ok=ok, missing=list(d.get("missing") or []), extra=list(d.get("extra") or []))
            cands.append(c)
            score = (len(c["missing"]), len(c["extra"]))
            if best is None or score < best[0]:
                best = (score, png, i + 1)
            self._log(f"[S8] MOSS {fname} 第 {i + 1} 张候选{'合格' if ok else '不合格'}"
                      + ("" if ok else f"（缺 {c['missing'] or '无'}，多 {c['extra'] or '无'}）")
                      + ("（缓存）" if hit else ""))
            if ok:
                return {"ok": True, "png": png, "candidates": cands, "chosen": i + 1}
        return {"ok": False, "png": best[1] if best else None, "candidates": cands,
                "chosen": best[2] if best else None}


def public_candidates(cands):
    """明细里写的候选信息（不写缓存键全文）。"""
    out = []
    for c in cands:
        o = {"种子": c["seed"], "合格": bool(c.get("ok")), "缺词": c.get("missing") or [],
             "多字": c.get("extra") or [], "来自缓存": bool(c.get("cached"))}
        if c.get("error"):
            o["没出成"] = c["error"]
        if c.get("seconds") is not None:
            o["耗时秒"] = c["seconds"]
        if c.get("submitted"):
            o["提交RH"] = True
        if c.get("task_id"):
            o["RH任务号"] = c["task_id"]
        if c.get("usage") is not None:
            o["RH返回用量"] = c["usage"]
        out.append(o)
    return out


_NTH = {1: "moss_first", 2: "moss_second", 3: "moss_third"}


def summarize(entries):
    """明细逐张 → 检查报告"出图"一节用的计数。`rh_submits`：本次真的提交到 RunningHub 的候选数（算 RH 币）。"""
    cnt = {"moss_first": 0, "moss_second": 0, "moss_third": 0, "moss_to_gpt": 0, "rule_gpt": 0, "red": 0,
           "missing": 0, "rh_submits": 0}
    for e in entries:
        if e.get("本次") is None:
            cnt["rh_submits"] += sum(1 for c in e.get("候选") or [] if c.get("提交RH"))
        m = e.get("方式")
        if m == "moss":
            cnt[_NTH.get(e.get("采用第几张"), "moss_third")] += 1
        elif m == "gpt_fallback":
            cnt["moss_to_gpt"] += 1
        elif m == "gpt_rule":
            cnt["rule_gpt"] += 1
        elif m == "moss_red":
            cnt["red"] += 1
        elif m == "missing":
            cnt["missing"] += 1
    return cnt


def rh_coins(n_submits):
    """R120M3：RH 币消耗（估）= 提交次数 × RH_COINS_PER_IMAGE（RH 币，不计入人民币费用）。"""
    return int(n_submits) * RH_COINS_PER_IMAGE


def load_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def recorded_signature(root):
    d = load_json(root / SETTING_REL)
    return (d or {}).get("signature") if isinstance(d, dict) else None
