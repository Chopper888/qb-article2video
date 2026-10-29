# -*- coding: utf-8 -*-
"""统一的临时故障重试（ST3-P26 §4.1）。

引擎自己发出的外部请求（DeepSeek、方舟、豆包配音、RunningHub、飞书）遇到**临时性网络故障**时，
都经 `call()` 自己等待、自己重试，参数集中在本模块顶部。

- 算临时故障：连接失败 / 被重置、SSL 握手或读取中断、读超时（`is_transient_exc`）；
  HTTP 429、502、503、504（`TRANSIENT_HTTP`）；服务明确返回的"限流 / 繁忙"（调用方用 `retry_on` 认）。
- 不算：401、402、403、余额不足、参数错误、其他 4xx、内容类错误——调用方照旧按原分类立即失败。
- 退避：`BACKOFF_SEC`（5、15、45、90、90……秒），单次请求累计等待不超过 `MAX_WAIT_SEC`；
  超过上限就把最后一次的异常 / 结果原样交还调用方，调用方按原来的方式失败（`error_kind=network`）。
- 每次重试写一行日志并通知 `set_notifier` 注册的回调（cli 据此发 `step_progress`，
  APP 能显示"网络不稳定，正在重试（第 N 次）"）。

**提交类请求**（豆包配音提交、RunningHub 提交）只重试"确定没被服务端收下"的失败
（`is_unsent_exc`：urllib 在发完请求之前出的错一律包成 `URLError`），以及服务端明确拒收的 429 / 503；
请求已经发出、没拿到回复的（读超时、读到一半断开、502 / 504）不自动重新提交，防重复扣费。
"""
import errno
import http.client
import socket
import ssl
import sys
import threading
import time
import urllib.error

from pipeline.engine import stopflag

# 退避序列（秒）：第 N 次重试前等 BACKOFF_SEC[N-1]，超出序列长度的都按最后一个。
BACKOFF_SEC = (5, 15, 45, 90)
# 单次请求的累计等待上限（秒）。5+15+45+90×5 = 515，再等一次就超 600 → 最多重试 8 次。
MAX_WAIT_SEC = 600
# 算临时故障的 HTTP 状态。
TRANSIENT_HTTP = frozenset({429, 502, 503, 504})
# 查询 / 下载 / 文本调用（重发不会多扣一次任务费）额外重试 500：
# DeepSeek、方舟文档都把 500 列为"服务内部错误，稍后重试"，旧代码对 RunningHub / 豆包 / 飞书也一直重试 5xx。
TRANSIENT_HTTP_IDEMPOTENT = TRANSIENT_HTTP | {500}
# 提交类请求：只重试服务端明确拒收的状态（限流、繁忙）。502 / 504 是网关的回复，后端可能已经建了任务。
TRANSIENT_HTTP_SUBMIT = frozenset({429, 503})

# 不是网络问题的 OSError（本地文件、磁盘）。
_LOCAL_ERRNOS = {errno.ENOSPC, errno.ENOENT, errno.EACCES, errno.EPERM, errno.EISDIR,
                 errno.ENOTDIR, errno.EROFS, getattr(errno, "EDQUOT", errno.ENOSPC)}

_notifier = None
_lock = threading.Lock()


def set_notifier(fn):
    """注册重试通知回调 `fn(service, attempt, wait_sec, reason, what)`（cli 用来发事件）；None 取消。"""
    global _notifier
    _notifier = fn


def _requests_exc():
    """(算临时故障的 requests 异常, requests 异常基类)。requests 没装时两者都是空。"""
    try:
        import requests
        return ((requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                 requests.exceptions.ChunkedEncodingError), (requests.exceptions.RequestException,))
    except Exception:
        return (), ()


def is_transient_exc(e):
    """连接失败、连接被重置、SSL 中断、读超时等临时网络故障 → True。HTTPError（服务端的应答）→ False。"""
    if isinstance(e, urllib.error.HTTPError):
        return False
    transient, base = _requests_exc()
    if isinstance(e, transient):
        return True
    if isinstance(e, base):
        return False          # requests 的其他异常（InvalidURL 等）是 OSError 子类，但不是网络故障
    if isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                      ssl.SSLError, http.client.HTTPException)):
        return True
    if isinstance(e, OSError):
        # 其余 OSError（如裸的 "Network is unreachable"）按网络算；本地文件 / 磁盘错误不算。
        if isinstance(e, (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)):
            return False
        return e.errno not in _LOCAL_ERRNOS
    return False


def is_unsent_exc(e):
    """确定**没把请求发完**的失败（提交类请求可以安全重发）。

    urllib 的 `do_open` 把建连接、TLS 握手、发送请求阶段的 OSError 全部包成 `URLError`；
    发完请求之后（`getresponse` 读回复时）出的错原样抛出（socket.timeout、ConnectionResetError、
    RemoteDisconnected、SSL 读取中断……）——那时服务端可能已经收下并建了任务，不能盲目重发。
    """
    return isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError)


def describe_exc(e):
    s = f"{type(e).__name__}: {e}"
    return s if len(s) <= 160 else s[:157] + "..."


def waits(max_wait=None):
    """依次给出每次重试前要等的秒数；累计超过上限就停。"""
    cap = MAX_WAIT_SEC if max_wait is None else max_wait
    total, n = 0, 0
    while True:
        w = BACKOFF_SEC[min(n, len(BACKOFF_SEC) - 1)]
        if total + w > cap:
            return
        total += w
        n += 1
        yield w


def _stderr_log(line):
    print(line, file=sys.stderr, flush=True)


def _notify(service, attempt, wait, reason, what, log):
    line = f"[网络] {service}{('（' + what + '）') if what else ''} 临时故障：{reason}；{wait} 秒后第 {attempt} 次重试"
    try:
        (log or _stderr_log)(line)
    except Exception:
        pass
    fn = _notifier
    if fn is not None:
        try:
            with _lock:
                fn(service, attempt, wait, reason, what)
        except Exception:
            pass


def call(fn, *, service, what="", retry_exc=is_transient_exc, retry_on=None, log=None,
         max_wait=None):
    """调 `fn()`，遇到临时故障按退避重试。

    - `retry_exc(e) -> bool`：这个异常算不算临时故障（默认 `is_transient_exc`；提交类传 `is_unsent_exc`）。
    - `retry_on(result) -> str|None`：拿到结果后判断要不要重试（返回原因文字 = 要重试，如 "HTTP 503"）。
    - 等待用完：异常 → 原样抛出最后一次的异常；结果 → 原样返回最后一次的结果（调用方照原来的方式失败）。
    """
    gen = waits(max_wait)
    attempt = 0
    while True:
        exc = None
        try:
            result = fn()
        except Exception as e:
            if not retry_exc(e):
                raise
            exc, reason = e, describe_exc(e)
        else:
            reason = retry_on(result) if retry_on is not None else None
            if not reason:
                return result
        wait = next(gen, None)
        if wait is None:
            if exc is not None:
                raise exc
            return result
        attempt += 1
        _notify(service, attempt, wait, reason, what, log)
        stopflag.sleep(wait)       # P31b：等重试时发现停止标记就不再重试


def http_status_retry(statuses):
    """给 `(status, body)` 形式的结果用的 `retry_on`：状态在 `statuses` 里就重试。"""
    def check(result):
        st = result[0] if isinstance(result, tuple) else result
        return f"HTTP {st}" if st in statuses else None
    return check
