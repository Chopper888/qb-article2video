# -*- coding: utf-8 -*-
"""stdout JSON 事件（外壳约定 v0.1 §2.4）。

每行一个 JSON，一条事件一行；字段固定包含
`schema_version`、`seq`、`time`、`event`、`step`、`message`。
`time` 为带时区的 ISO 8601。事件类型见 `EVENT_TYPES`；`plan` / `status` 另有各自的事件类型。
额外字段（如 `status`、`cost`、`decision`）属于「其他字段」，APP 必须忽略不认识的字段。
"""
import datetime
import json
import sys
import threading

SCHEMA_VERSION = 1

# 执行流事件
EVENT_STEP_START = "step_start"
EVENT_STEP_DONE = "step_done"
EVENT_STEP_SKIP = "step_skip"
EVENT_STEP_FAIL = "step_fail"
EVENT_RUN_DONE = "run_done"
# 只读子命令事件
EVENT_PLAN = "plan"
EVENT_STATUS = "status"
# 导入（ST3-P22）：导入成功后最先发 input_resolved；有输入问题时紧跟一条 input_problems
EVENT_INPUT_RESOLVED = "input_resolved"
EVENT_INPUT_PROBLEMS = "input_problems"
# 原文变化、口播自动解锁（ST3-P22 §3.7）
EVENT_NARRATION_UNLOCKED = "narration_unlocked"
# ST3-P23：步骤内进度（step/done/total/unit/message）、开跑前费用范围、密钥检测（一个服务一条）
EVENT_STEP_PROGRESS = "step_progress"
EVENT_ESTIMATE = "estimate"
EVENT_KEY_CHECK = "key_check"


# 带 `service` 字段的失败退出码（P31）：2 执行失败、4 配置错误
FAIL_EXIT_CODES = (2, 4)


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class Emitter:
    """顺序发事件到 stdout。线程安全（本引擎单线程，仅为稳妥）。"""

    def __init__(self, stream=None):
        self._stream = stream or sys.stdout
        self._seq = 0
        self._lock = threading.Lock()
        # run_done 的默认字段（ST3-P22：导入成功后每条 run_done 都带 article）
        self.run_done_defaults = {}

    def emit(self, event, step=None, message="", **extra):
        with self._lock:
            self._seq += 1
            rec = {
                "schema_version": SCHEMA_VERSION,
                "seq": self._seq,
                "time": _now(),
                "event": event,
                "step": step,
                "message": message,
            }
            rec.update(extra)
            if event == EVENT_RUN_DONE:
                for k, v in self.run_done_defaults.items():
                    if rec.get(k) is None:
                        rec[k] = v
            # P31：step_fail 和失败的 run_done（退出码 2/4）一定带 service（不来自外部服务为 null）
            if event == EVENT_STEP_FAIL or (event == EVENT_RUN_DONE
                                            and rec.get("exit_code") in FAIL_EXIT_CODES):
                rec.setdefault("service", None)
            self._stream.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._stream.flush()
        return rec
