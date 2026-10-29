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
