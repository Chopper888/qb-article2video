# -*- coding: utf-8 -*-
r"""引擎用到的固定路径（本轮集中在此，便于 P3/P4 固化）。

- **引擎根**：`pipeline` 包所在仓库（`python -m pipeline.engine` 由它提供），
  由 `__file__` 向上推出（ST1-P4a3 起**禁止写死主仓路径**）。
- S1-R4 语义标注原型（`splitter` / `annotate`）等原外部依赖已经全部搬进 `pipeline\vendor\`，
  引擎只从这里导入；**外部只读代码依赖清单为空**。
- 渲染工程源：`src`、`package.json`、`tsconfig.json` 从引擎根下 `R57-render-test` 复制。
- 运行时副本：`_runtime\node_modules`（ST1-P2 修订：渲染工程 node_modules 联接指向它，
  不再指向主仓，使 remotion 打包缓存落在副本内）。见任务书 ST1-P2 修订裁定第 3 条。
  字体同样来自运行时副本（`_runtime\fonts\…`），由 `prepare_sample --with-runtime` 准备。

运行时目录在 ST1-P2 里是**写死的** `<开发测试目录>\_runtime`（`DEFAULT_RUNTIME_NODE_MODULES`）。
ST1-P2b 起改为可配置：`--runtime-dir`，不传时默认 `<work-root 的上一级>\_runtime`
（见 `default_runtime_dir`），所以同一份引擎可以配合任意工作根目录。
"""
import sys
from pathlib import Path

# 引擎根：当前执行的这份代码所在的仓库根（`pipeline\engine\paths.py` 向上两级）。
# 禁止写死主仓绝对路径；引擎对自身文件的一切读取都以这里为基准。
ENGINE_ROOT = Path(__file__).resolve().parents[2]

# 第三方代码存放点（ST1-P5b 起：外部依赖 S1-R4/中转站/wz-sp-ctsc 已搬进主仓，
# 引擎只从这里导入，不再读写仓库外的三个原目录）。
VENDOR_DIR = ENGINE_ROOT / "pipeline" / "vendor"

RENDER_MAIN = ENGINE_ROOT / "R57-render-test"
DEFAULT_RENDER_SRC = RENDER_MAIN / "src"

RUNTIME_DIRNAME = "_runtime"

# 本篇素材箱：按名字定位源文件时依次在这几个目录里找（音频 / 出图插画 / 文章原图）
AUDIO_DIR = "03-配音/audio"
AI_IMAGE_DIR = "07-出图/images"
ORIGINAL_IMAGE_DIR = "00-输入/图片和附件"
ASSET_DIRS = (AUDIO_DIR, AI_IMAGE_DIR, ORIGINAL_IMAGE_DIR)

AUDIO_GLOB = AUDIO_DIR + "/*.wav"
AI_IMAGE_GLOB = AI_IMAGE_DIR + "/*.png"
ORIGINAL_IMAGE_GLOB = ORIGINAL_IMAGE_DIR + "/*"
FONT_REL = "fonts/LXGWWenKai-Regular.ttf"


def default_runtime_dir(work_root):
    """运行时目录默认值：`<work-root 的上一级>\\_runtime`。"""
    return Path(work_root).resolve().parent / RUNTIME_DIRNAME


def resolve_runtime_node_modules(work_root, runtime_dir=None):
    """算渲染工程 node_modules 联接目标。

    `runtime_dir` 给的是**运行时目录**本身（其下有 node_modules），不是 node_modules 路径。
    """
    rd = Path(runtime_dir) if runtime_dir else default_runtime_dir(work_root)
    return rd / "node_modules"


def runtime_node_modules_ok(node_modules):
    """运行时副本是否可用：存在且含 remotion 包。"""
    return (Path(node_modules) / "remotion").is_dir()


def ensure_prototype_on_path():
    """把 vendor 里的 S1-R4 原型目录加入 sys.path（S2 的 splitter / annotate 从那里导入）。"""
    s = str(VENDOR_DIR / "s1-r4")
    if s not in sys.path:
        sys.path.insert(0, s)


# --------------------------------------------------------------------------
# R120M2 Q1：外部程序（node / ffmpeg / ffprobe）一律用绝对路径，不依赖 PATH。
#
# 1.1.0 安装版在没装 Node.js 的电脑上 S10 必败：render_mux 经 shell 调裸命令 `npx`，
# 而 APP 1.0 界面启动引擎时只把 ffmpeg 目录放进了 PATH（运行包自带的 node 没放）。
# 现在引擎自己定位这几个程序，顺序：
#   1. 环境变量（APP 传进来的绝对路径）：A2V_NODE = node 可执行文件；A2V_FFMPEG_DIR = ffmpeg/ffprobe 所在目录；
#   2. 运行包自带：<运行包>/node/node.exe、<运行包>/ffmpeg/bin/ffmpeg.exe（引擎在 <运行包>/engine，
#      所以运行包 = 引擎根的上一级；Mac/Linux 上是 node/bin/node、ffmpeg/bin/ffmpeg）；
#   3. 只在开发模式（源码运行、没有规则归档）下回退到 PATH 里找（shutil.which，得到的也是绝对路径），
#      并在日志里写明用的是系统里的那一个。打包模式找不到自带版本就直接报错，不去碰 PATH。
# --------------------------------------------------------------------------
NODE_ENV = "A2V_NODE"
FFMPEG_DIR_ENV = "A2V_FFMPEG_DIR"
RULES_ARCHIVE_NAME = "_a2v_rules.bin"      # 与 _protect.ARCHIVE_NAME 相同；有它就是打包模式


class ToolNotFound(RuntimeError):
    """自带的外部程序找不到。"""


def is_packaged(engine_root=None):
    """打包模式 = 引擎根下有规则归档（开发模式源码直接跑，没有它）。"""
    return (Path(engine_root or ENGINE_ROOT) / RULES_ARCHIVE_NAME).is_file()


def _exe(name, platform=None):
    return name + ".exe" if (platform or sys.platform) == "win32" else name


def _is_exe(p):
    import os
    return bool(p) and Path(p).is_absolute() and Path(p).is_file() and os.access(str(p), os.X_OK)


def find_node(engine_root=None, environ=None, platform=None):
    """返回 (node 绝对路径, 来源说明)。来源：'A2V_NODE' / '运行包自带' / '系统 PATH（开发模式回退）'。"""
    import os
    import shutil
    env = os.environ if environ is None else environ
    root = Path(engine_root or ENGINE_ROOT)
    v = (env.get(NODE_ENV) or "").strip()
    if v:
        if _is_exe(v):
            return str(Path(v)), NODE_ENV
        raise ToolNotFound(f"{NODE_ENV} 指向的 node 不存在或不可执行: {v}")
    plat = platform or sys.platform
    bundled = root.parent / "node" / ("node.exe" if plat == "win32" else "bin/node")
    if _is_exe(bundled):
        return str(bundled), "运行包自带"
    if not is_packaged(root):
        found = shutil.which("node", path=env.get("PATH"))
        if found:
            return os.path.abspath(found), "系统 PATH（开发模式回退）"
    raise ToolNotFound(f"找不到运行包自带的 node（应在 {bundled}）")


def find_ffmpeg_tool(name, engine_root=None, environ=None, platform=None):
    """name 为 'ffmpeg' 或 'ffprobe'。返回 (绝对路径, 来源说明)。"""
    import os
    import shutil
    env = os.environ if environ is None else environ
    root = Path(engine_root or ENGINE_ROOT)
    plat = platform or sys.platform
    exe = _exe(name, plat)
    d = (env.get(FFMPEG_DIR_ENV) or "").strip()
    if d:
        p = Path(d) / exe
        if _is_exe(p):
            return str(p), FFMPEG_DIR_ENV
        raise ToolNotFound(f"{FFMPEG_DIR_ENV} 目录里没有 {exe}: {d}")
    bundled = root.parent / "ffmpeg" / "bin" / exe
    if _is_exe(bundled):
        return str(bundled), "运行包自带"
    if not is_packaged(root):
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            return os.path.abspath(found), "系统 PATH（开发模式回退）"
    raise ToolNotFound(f"找不到运行包自带的 {exe}（应在 {bundled}）")


def ffmpeg_exe(name="ffmpeg"):
    """只要路径（给 ffmpeg / ffprobe 的各个调用点用）。"""
    return find_ffmpeg_tool(name)[0]


def remotion_cli_js(render_project):
    """渲染工程里 Remotion CLI 的 JS 入口（@remotion/cli 的 bin.remotion，即 remotion-cli.js）。"""
    import json
    pkg_dir = Path(render_project) / "node_modules" / "@remotion" / "cli"
    try:
        rel = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8")).get("bin", {}).get("remotion")
    except (OSError, ValueError):
        rel = None
    js = pkg_dir / (rel or "remotion-cli.js")
    if not js.is_file():
        raise ToolNotFound(f"运行环境里没有 Remotion 命令行入口: {js}")
    return str(js)
