# -*- coding: utf-8 -*-
r"""引擎运行期临时目录（ST3-P30 / v5.6）。

一次运行的临时文件全部写进 ``<临时根>\_a2v_tmp\<运行编号>``（临时根可用
``--tmp-dir`` 或环境变量 ``A2V_TEMP_DIR`` 改，默认 ``<work-root>\_a2v_tmp``），
并把它自己和子进程（node / Remotion / ffmpeg）的 ``TEMP``、``TMP`` 指到那里。

清理规则：
  - 成功结束（退出码 0 / 1）：删掉本次运行目录；
  - 失败或中断：保留本次运行目录，删掉更早的（只留最近 1 次）；
  - 每次运行开始时：清掉 24 小时以前的残留（断电、崩溃留下的）；
  - 正在运行的那一次（目录名带 PID 且进程仍存活）一律不动。
"""
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

TEMP_ENV = "A2V_TEMP_DIR"
TMP_DIRNAME = "_a2v_tmp"
STALE_SECONDS = 24 * 3600

# 运行编号：<YYYYmmdd-HHMMSS>-<PID>
_RUN_ID_RE = re.compile(r"^\d{8}-\d{6}-(\d+)$")


def resolve_base(work_root, explicit=None):
    r"""临时根：`--tmp-dir` → 环境变量 `A2V_TEMP_DIR` → `<work-root>\_a2v_tmp`。"""
    if explicit:
        return Path(explicit)
    env = (os.environ.get(TEMP_ENV) or "").strip()
    if env:
        return Path(env)
    return Path(work_root) / TMP_DIRNAME


def new_run_id(now=None):
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"{ts}-{os.getpid()}"


def prepare(base, run_id=None, now=None):
    """建好本次运行目录，顺手清掉 24 小时前的残留。返回运行目录。"""
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    run_id = run_id or new_run_id(now)
    now = time.time() if now is None else now
    for p in base.iterdir():
        if p.name == run_id or not p.is_dir() or _pid_of(p) is None:
            continue
        if _is_running(p):
            continue
        try:
            if now - p.stat().st_mtime >= STALE_SECONDS:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            continue
    run_dir = base / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def apply_env(run_dir):
    """把本进程（及其子进程）的 TEMP/TMP 指到运行目录。"""
    p = str(run_dir)
    os.environ["TEMP"] = p
    os.environ["TMP"] = p
    # tempfile 第一次用到时会缓存目录，改完环境变量要重置，引擎自己的临时文件才会跟着走。
    tempfile.tempdir = p


def finalize(run_dir, success):
    """运行结束：成功删本次目录；失败保留本次、只留最近 1 次（不动正在跑的）。"""
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return
    if success:
        shutil.rmtree(run_dir, ignore_errors=True)
        return
    for p in run_dir.parent.iterdir():
        if p == run_dir or not p.is_dir() or _pid_of(p) is None or _is_running(p):
            continue
        shutil.rmtree(p, ignore_errors=True)


def _pid_of(p):
    """目录名是运行编号时返回其中的 PID，否则 None（不是引擎建的目录，不碰）。"""
    m = _RUN_ID_RE.match(p.name)
    return int(m.group(1)) if m else None


def _is_running(p):
    pid = _pid_of(p)
    return pid is not None and _pid_alive(pid)


def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return True  # 查不了时保守当作还在跑，宁可不删
    return f'"{pid}"' in r.stdout
