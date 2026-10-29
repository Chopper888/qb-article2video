# -*- coding: utf-8 -*-
r"""Article2Video 引擎（ST1-P2 第一版，本轮只做理论型）。

统一入口：`python -m pipeline.engine <run|plan|status> …`（对外文档称 `article2video`）。

本包只做编排（读写账本、发事件、按步骤调用既有 `pipeline.*` 与外部依赖），
不改既有 `pipeline\` 模块的默认行为。设计依据见 `pipeline\ASSEMBLY_LINE.md` 的 A8 节。

启动即设置 `sys.dont_write_bytecode = True`（见 `__main__`），并把子进程的
`PYTHONDONTWRITEBYTECODE`、`TEMP`、`TMP` 指到 `--tmp-dir`，以免写主仓。
"""
