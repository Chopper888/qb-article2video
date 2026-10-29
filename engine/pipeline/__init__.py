# -*- coding: utf-8 -*-
"""pipeline 调度层（M4 收成目标）。

R162 冻结方案：把每轮 r1xx_gen.py / r1xx_prep_video.py 里复制粘贴的调度逻辑，
收敛成本包下的参数化模块，让每轮脚本改为"调用"而非"复制一份再改"。

包内文件（按 P1~P4 自然接缝拆分，非 600 行单文件）：
  - routing.py     决策判断（P1 已落）：纯决策函数，导览检测 / N2·N3 无图组识别 /
                   转场选择 / 标题场景。输入确定、输出确定、无副作用。
  - orchestrate.py 编排主循环（P2 待落）：main() 决策树 + 补跑循环 + ds_retry。
  - scene_build.py 场景构建（P3 待落）：title_win / ov_s·ov_e / scenes 挂载 / chapters。

import 约定（R162 §A；2026-10 改为包自定位）：导入本包即自动把仓库根加入 sys.path，
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
之后 `from pipeline import routing` 在任何工作目录下可用。本包内部如需出图原语（generate/type_match），
在对应模块内反向 sys.path.insert wz-sp-ctsc/scripts。

写入权（R162 拍板第 6 项）：主线 Claude Code 独占写，Codex 只读。
"""

import sys
from pathlib import Path

# 仓库根 = 本文件（pipeline/__init__.py）的上一级目录；不再写死绝对路径。
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
