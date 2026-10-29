# -*- coding: utf-8 -*-
"""`python -m pipeline.engine` 入口。

第一件事：禁止写 .pyc（配合子进程的 PYTHONDONTWRITEBYTECODE=1），避免在主仓留下字节码。
"""
import sys

sys.dont_write_bytecode = True

from pipeline.engine.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
