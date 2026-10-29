# -*- coding: utf-8 -*-
"""引擎步骤错误（退出码 2）与结构化失败原因 `error_kind`（ST3-P22 §3.5）。

单独成一个模块，供 `steps.py` 与 `paid.py` 共用——付费封装要在"发请求之前"抛出
拦截错误，而 `steps.py` 又要 import `paid.py`，两者若互相 import 会成环。
`pipeline.engine.steps.StepError` 仍然可用（steps 里 re-export 这个名字）。

`error_kind` 是 `step_fail` / `run_done` 上的新字段，**不改退出码、不改 reason**。
取值固定为 `ERROR_KINDS` 里的这些；抛错处用 `StepError(msg, kind=…)` 标明，
没标明的由 `classify()` 按异常类型兜底判断。

P31：同样两处事件另带 `service`（失败来自哪一家外部服务，取值见 `SERVICES`；不来自外部服务为 null）。
抛错处用 `StepError(msg, kind=…, service=…)` 标明；统一付费入口 `paid.perform` 给真实调用里抛出的
异常补标它的服务；`failure_service()` 沿异常链取，`LOCAL_KINDS` 这几种失败一律为 null。
"""
import errno

KIND_INPUT_INVALID = "input_invalid"
KIND_KEY_MISSING = "key_missing"
KIND_KEY_INVALID = "key_invalid"
KIND_BALANCE = "balance_insufficient"
KIND_NETWORK = "network"
KIND_SERVICE = "service_error"
KIND_COST_LIMIT = "cost_limit"
KIND_PAID_BLOCKED = "paid_blocked"
KIND_RENDER = "render_failed"
KIND_DISK_FULL = "disk_full"
KIND_ALREADY_RUNNING = "already_running"
KIND_INTERRUPTED = "interrupted"
KIND_INTERNAL = "internal"

ERROR_KINDS = (KIND_INPUT_INVALID, KIND_KEY_MISSING, KIND_KEY_INVALID, KIND_BALANCE,
               KIND_NETWORK, KIND_SERVICE, KIND_COST_LIMIT, KIND_PAID_BLOCKED, KIND_RENDER,
               KIND_DISK_FULL, KIND_ALREADY_RUNNING, KIND_INTERRUPTED, KIND_INTERNAL)


# P31：外部服务键，与 `estimate.by_service` 一致；`check-keys` 的 `tts` 即 `doubao_speech`。
SERVICE_DEEPSEEK = "deepseek"
SERVICE_ARK = "ark"
SERVICE_SPEECH = "doubao_speech"
SERVICE_RUNNINGHUB = "runninghub"
SERVICE_FEISHU = "feishu"
SERVICES = (SERVICE_DEEPSEEK, SERVICE_ARK, SERVICE_SPEECH, SERVICE_RUNNINGHUB, SERVICE_FEISHU)
# 计价 / 密钥检测里的别名 → 事件里的服务键
_SERVICE_ALIASES = {"runninghub_app": SERVICE_RUNNINGHUB, "tts": SERVICE_SPEECH}
# 密钥环境变量 → 服务
ENV_SERVICE = {
    "DEEPSEEK_API_KEY": SERVICE_DEEPSEEK,
    "ARK_API_KEY": SERVICE_ARK,
    "MODEL_SPEECH_API_KEY": SERVICE_SPEECH,
    "RUNNINGHUB_API_KEY": SERVICE_RUNNINGHUB,
    "FEISHU_APP_ID": SERVICE_FEISHU,
    "FEISHU_APP_SECRET": SERVICE_FEISHU,
}
# 这几种失败不是哪一家外部服务造成的：service 一律为 null
LOCAL_KINDS = (KIND_PAID_BLOCKED, KIND_COST_LIMIT, KIND_RENDER, KIND_DISK_FULL,
               KIND_ALREADY_RUNNING, KIND_INTERRUPTED, KIND_INTERNAL)


class StepError(Exception):
    """步骤执行失败（退出码 2）。`kind` 是 error_kind（None = 由 classify 兜底）；`service` 见 SERVICES。"""

    kind = None
    service = None

    def __init__(self, message="", kind=None, service=None):
        super().__init__(message)
        if kind is not None:
            self.kind = kind
        if service is not None:
            self.service = norm_service(service)


def norm_service(name):
    """服务名（含别名）→ 事件里的服务键；不认识的返回 None。"""
    name = _SERVICE_ALIASES.get(name, name)
    return name if name in SERVICES else None


def tag_service(exc, service):
    """给异常补标服务（已经标了的不改）。返回原异常，便于 `raise tag_service(e, …)`。"""
    if getattr(exc, "service", None) is None:
        try:
            exc.service = norm_service(service)
        except Exception:
            pass
    return exc


def service_of(exc):
    """沿异常链取第一个标明的服务；都没标返回 None。"""
    for e in _chain(exc):
        s = norm_service(getattr(e, "service", None))
        if s:
            return s
    return None


def failure_service(exc, kind):
    """事件里的 `service`：本地原因（LOCAL_KINDS）为 None，否则取异常链上标明的服务。"""
    if kind in LOCAL_KINDS:
        return None
    return service_of(exc)


def keys_service(names):
    """缺的密钥都属于同一家服务时返回它，否则 None（多家一起缺看 missing_keys）。"""
    svcs = {ENV_SERVICE.get(n) for n in names or []}
    return svcs.pop() if len(svcs) == 1 and None not in svcs else None


class PaidBlocked(StepError):
    """付费调用被拦截（`--no-paid` 或超出费用上限）——也是步骤失败的一种。

    `paid.py` 在 `--no-paid` / 超上限时抛它；因为它继承 `StepError`，
    `cli.cmd_run` 的 `except StepError` 直接接得住，不必在每处调用点再包一层。
    """

    kind = KIND_PAID_BLOCKED


def _chain(exc):
    """异常本身 + __cause__/__context__ 链（防环）。"""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def is_disk_full(exc):
    return any(isinstance(e, OSError) and e.errno == errno.ENOSPC for e in _chain(exc))


def _is_network(exc):
    import socket
    import urllib.error
    net = (ConnectionError, TimeoutError, socket.timeout, urllib.error.URLError)
    try:
        import requests
        net = net + (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
    except Exception:
        pass
    for e in _chain(exc):
        if isinstance(e, urllib.error.HTTPError):
            return False          # HTTPError 是 URLError 的子类，但它是服务端的应答
        if isinstance(e, net):
            return True
    return False


def classify(exc, step=None):
    """异常 → error_kind。标了 kind 的照用；磁盘满、网络异常按类型认；S10 的步骤错误算渲染失败。"""
    if is_disk_full(exc):
        return KIND_DISK_FULL
    if isinstance(exc, KeyboardInterrupt):
        return KIND_INTERRUPTED
    for e in _chain(exc):
        k = getattr(e, "kind", None)
        if k in ERROR_KINDS:
            return k
    if _is_network(exc):
        return KIND_NETWORK
    msg = str(exc)
    if msg.startswith("缺少环境变量密钥"):
        return KIND_KEY_MISSING
    if msg.startswith("超出费用上限"):
        return KIND_COST_LIMIT
    if msg.startswith("禁止付费"):
        return KIND_PAID_BLOCKED
    if step == "S10" and isinstance(exc, StepError):
        return KIND_RENDER
    return KIND_INTERNAL


def http_kind(status, body=""):
    """外部服务返回的 HTTP 状态 → error_kind（只认得出的几种；认不出返回 None）。

    依据：DeepSeek 官方错误码（401 鉴权失败、402 余额不足、429 限流）；
    火山方舟公共错误码（401 AuthenticationError、403 AccountOverdueError 欠费 / AccessDenied 无权限）。
    """
    body = body or ""
    if status == 402:
        return KIND_BALANCE
    if status == 403 and ("AccountOverdue" in body or "Overdue" in body or "欠费" in body):
        return KIND_BALANCE
    if status in (401, 403):
        return KIND_KEY_INVALID
    return None
