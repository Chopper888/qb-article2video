# -*- coding: utf-8 -*-
"""停止标记文件（P31b，约定 v5.4）：APP 想让引擎停下时，在文章文件夹里写 `.stop-request`。

和 SIGINT 是同一种"被停止"：引擎在检查点发现标记，就抛 `StopRequested`（`KeyboardInterrupt`
的子类），走原来的中断收尾——账本落盘、已出的图和登记的任务号都留着，`run_done` 退出码 3、
`error_kind=interrupted`。Windows 上没有能发给单个进程的 SIGINT，APP 靠这个文件"先打招呼"。

检查点（`check()` / `sleep()`）：
- 每一步开始前（cli）；
- 步骤内进度回调（cli 的 `_ProgressThrottle`，S0/S3/S6/S7/S8/S10 都在发；S10 渲染轮询每 0.2 秒一次）；
- 统一付费入口发请求之前（`paid.perform`，收尾时不再发新的付费请求）；
- 网络重试、豆包配音查询、口播重试、S8 重新提交之前的等待（`sleep()`，每秒看一次）；
- S8 主线程等出图结果的轮询间隙（每秒一次）。

标记只在 `arm()` 之后生效（`run` 拿到运行锁之后）；没有 arm 时 `check()` 什么也不做，
`sleep()` 就是普通的 sleep。收尾时 `disarm()` 删掉标记。
"""
import threading
import time
from pathlib import Path

STOP_FILENAME = ".stop-request"

# 进程起点：比它旧的标记是上一次运行留下的（开跑时删掉）；不比它旧的是 APP 在本次启动之后写的，照常生效。
PROCESS_T0 = time.time()

_path = None
_seen = threading.Event()


class StopRequested(KeyboardInterrupt):
    """停止标记文件触发的中断。是 KeyboardInterrupt 的子类：凡是接住中断的地方（S8 收尾、S10 收掉
    渲染进程树、cli 退出码 3）都原样适用。"""


def stop_path(root):
    return Path(root) / STOP_FILENAME


def arm(root, started_at=None):
    """本次运行开始监视 `<root>/.stop-request`。已有的旧标记（修改时间早于本进程启动）先删掉，返回 True；
    否则返回 False。"""
    global _path
    _seen.clear()
    p = stop_path(root)
    _path = p
    t0 = PROCESS_T0 if started_at is None else started_at
    try:
        if p.exists() and p.stat().st_mtime < t0:
            p.unlink()
            return True
    except OSError:
        pass
    return False


def disarm():
    """收尾：删掉标记，不再监视。"""
    global _path
    p, _path = _path, None
    _seen.clear()
    if p is not None:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def requested():
    """标记是否已经出现（出现过一次就一直算，删掉也不撤回）。"""
    if _seen.is_set():
        return True
    p = _path
    if p is None:
        return False
    try:
        hit = p.exists()
    except OSError:
        hit = False
    if hit:
        _seen.set()
    return hit


def check():
    """发现标记就抛 StopRequested。"""
    if requested():
        raise StopRequested()


def sleep(seconds, step=1.0):
    """可被停止标记打断的 sleep：每 `step` 秒看一次标记，发现就抛 StopRequested。
    没在监视（没有 arm）时就是一次普通的 `time.sleep(seconds)`。"""
    if _path is None:
        time.sleep(seconds)
        return
    end = time.monotonic() + max(0.0, float(seconds))
    while True:
        check()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(step, left))


def armed():
    return _path is not None


def _reset_for_tests():
    global _path
    _path = None
    _seen.clear()

