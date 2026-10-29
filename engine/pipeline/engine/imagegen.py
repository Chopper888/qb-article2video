# -*- coding: utf-8 -*-
"""出图服务接口（ST3-P26 §4.2 第 7 条）：「向哪家服务要图」只在这里。

S8 的调度（并行、软等待、缺图、任务号登记）只通过 `ImageService` 的三个方法调用出图服务：

- `submit(prompt) -> task_id`：提交一张图（付费点）。失败抛 `StepError`（带 error_kind）。
- `check(task_id) -> (status, x)`：查一次任务状态，不等待。status ∈ `running` / `success` / `fail`；
  success 时 x 是输出 URL，fail 时 x 是 `{"errorCode", "errorMessage"}`（服务返回的原文）。
- `download(url) -> bytes`：取回图片。

以后换成别的服务（本地或其他平台的开源模型）只换这一块：写一个同样三个方法的类，
在 `service_for()` 里返回它。**出图缓存键不在这里**：键由 `paid.runninghub_key(提示词)` 按请求内容算
（模型名常量 + 提示词 + 尺寸参数），换实现类不会改变已有的键。

两个实现（ST3-P27）：

- `RunningHubAppService`（**默认**）：作者发布在 RunningHub 上的 AI 应用（`www.runninghub.ai`），
  应用里调的是同一个模型 gpt-image-2.0/text-to-image/economy。应用参数全部在 `RH_APP`。
- `RunningHubService`（备用）：RunningHub 官方模型接口 GPT Image 2（`www.runninghub.ai`，
  薄转接 wz-sp-ctsc/scripts/generate.py 的请求结构）。

用哪个由内部环境变量 `A2V_IMAGE_SERVICE` 决定（`app` 或不设 → AI 应用；`official` → 官方接口；
其他值 → cli 按参数错误退出，退出码 4）。这个变量不进命令行参数、不进界面。
两个服务模型、提示词、参数都相同，出图缓存键不区分服务（见 ST3-P27 回执 §4.3）。

临时网络故障按 `netretry` 重试；提交只重试确定没发出去的失败（防重复扣费，见 `netretry` 模块说明）。
"""
import json
import os
import urllib.error
import urllib.request

from pipeline.engine import errors, netretry
from pipeline.engine.errors import StepError

# RunningHub 账号在 .ai 站（与 .cn 不互通），引擎所有请求统一走这里。
RUNNINGHUB_BASE = "https://www.runninghub.ai"
RUNNINGHUB_HOST = "www.runninghub.ai"
RH_BASE = RUNNINGHUB_BASE
# 与 paid.RUNNINGHUB_ASPECT / RUNNINGHUB_RESOLUTION 同值（缓存键用的是 paid 里那一套，请求体用这一套）。
RH_ASPECT = "16:9"
RH_RESOLUTION = "1k"
# RunningHub 提交返回的余额不足（P25W 实测：{"errorCode":"605","errorMessage":"Your balance is insufficient. ..."}）。
RH_BALANCE_CODES = ("605",)

# AI 应用的全部参数（换应用或改节点时只改这一处）。
# 请求体：{"nodeInfoList": [{nodeId, fieldName, fieldValue} × 3], "instanceType", "usePersonalQueue"}。
RH_APP = {
    "base": RUNNINGHUB_BASE,
    "webapp_id": "2100836516310687745",
    "node_id": "3",
    "field_prompt": "prompt",
    "field_aspect": "aspectRatio",
    "field_resolution": "resolution",
    "aspect": "16:9",
    "resolution": "1k",
    "instance_type": "default",
    "use_personal_queue": "false",
}

# ST3-P27：出图服务的内部切换（不进命令行参数、不进界面）。
IMAGE_SERVICE_ENV = "A2V_IMAGE_SERVICE"
SERVICE_APP = "app"
SERVICE_OFFICIAL = "official"
_BALANCE_WORDS = ("balance is insufficient", "insufficient balance", "余额不足")


def is_balance_reply(obj):
    """RunningHub 的回复（dict 或原文）是不是"余额不足"。check-keys 和 S8 提交共用这一条判定。"""
    if isinstance(obj, (str, bytes)):
        raw = obj.decode("utf-8", "replace") if isinstance(obj, bytes) else obj
        try:
            obj = json.loads(raw)
        except Exception:
            return any(w in raw.lower() for w in _BALANCE_WORDS)
    if not isinstance(obj, dict):
        return False
    for k in ("errorCode", "code"):
        if str(obj.get(k) or "") in RH_BALANCE_CODES:
            return True
    msg = " ".join(str(obj.get(k) or "") for k in ("errorMessage", "msg", "message")).lower()
    return any(w in msg for w in _BALANCE_WORDS)


def _post_once(url, payload, api_key, timeout):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def rh_request(url, payload, api_key, timeout=120, submit=False, log=None):
    """发一次 RunningHub 请求，返回 (HTTP 状态, 正文)。临时故障按 `netretry` 重试，用完原样交还 / 抛出。

    `submit=True`（提交任务）：只重试确定没发出去的失败和 429 / 503；请求已发出却没收到回复的异常直接抛出。
    """
    if submit:
        return netretry.call(lambda: _post_once(url, payload, api_key, timeout),
                             service="RunningHub", what="提交", log=log,
                             retry_exc=netretry.is_unsent_exc,
                             retry_on=netretry.http_status_retry(netretry.TRANSIENT_HTTP_SUBMIT))
    return netretry.call(lambda: _post_once(url, payload, api_key, timeout),
                         service="RunningHub", what="查询", log=log,
                         retry_on=netretry.http_status_retry(netretry.TRANSIENT_HTTP_IDEMPOTENT))


def _walk(v):
    if isinstance(v, dict):
        for x in v.values():
            yield from _walk(x)
    elif isinstance(v, list):
        for x in v:
            yield from _walk(x)
    else:
        yield v


def rh_submit(prompt, api_key, log=None):
    """提交 RunningHub 文生图任务，返回 taskId。"""
    try:
        st, raw = rh_request(f"{RH_BASE}/openapi/v2/rhart-image-g-2/text-to-image",
                             {"prompt": prompt, "aspectRatio": RH_ASPECT,
                              "resolution": RH_RESOLUTION}, api_key, submit=True, log=log)
    except Exception as e:
        if netretry.is_transient_exc(e) and not netretry.is_unsent_exc(e):
            raise StepError(f"RunningHub 提交请求已发出、但没收到回复（{netretry.describe_exc(e)}）；"
                            f"服务端可能已经建了任务，为防重复扣费不自动重新提交，重跑会重新提交这一张",
                            kind=errors.KIND_NETWORK) from e
        raise
    tid = None
    try:
        j = json.loads(raw)
        for v in _walk(j):
            if isinstance(v, str) and v.isdigit() and len(v) >= 12:
                tid = v
                break
    except Exception:
        pass
    if not tid:
        if is_balance_reply(raw):
            kind = errors.KIND_BALANCE
        elif st in netretry.TRANSIENT_HTTP:
            kind = errors.KIND_NETWORK
        else:
            kind = errors.http_kind(st, raw) or errors.KIND_SERVICE
        raise StepError(f"RunningHub 提交失败 HTTP {st}: {raw[:300]}", kind=kind)
    return tid


def rh_check(tid, api_key, log=None):
    """查一次 RunningHub 任务状态（不等待）。返回 (status, x)，status ∈ running/success/fail。"""
    st2, qraw = rh_request(f"{RH_BASE}/openapi/v2/query", {"taskId": tid}, api_key, timeout=60, log=log)
    try:
        qj = json.loads(qraw)
    except Exception:
        qj = {}
    d = qj.get("data", qj) if isinstance(qj, dict) else qj
    status = ""
    if isinstance(d, dict):
        status = str(d.get("status") or d.get("taskStatus") or "")
    elif isinstance(d, str):
        status = d
    if status.lower() in ("success", "succeeded", "complete", "completed"):
        url = None
        for v in _walk(d):
            if isinstance(v, str) and v.startswith("http"):
                url = v
                break
        if not url:
            raise StepError(f"RunningHub 任务成功但无输出 URL: {qraw[:300]}")
        return "success", url
    if status.lower() in ("fail", "failed", "error", "cancel", "cancelled"):
        info = d if isinstance(d, dict) else {}
        return "fail", {"errorCode": str(info.get("errorCode") or ""),
                        "errorMessage": str(info.get("errorMessage") or info.get("failedReason") or "")}
    return "running", None


def _app_payload(prompt):
    a = RH_APP
    node = a["node_id"]
    return {"nodeInfoList": [
        {"nodeId": node, "fieldName": a["field_prompt"], "fieldValue": prompt},
        {"nodeId": node, "fieldName": a["field_aspect"], "fieldValue": a["aspect"]},
        {"nodeId": node, "fieldName": a["field_resolution"], "fieldValue": a["resolution"]},
    ], "instanceType": a["instance_type"], "usePersonalQueue": a["use_personal_queue"]}


def app_submit(prompt, api_key, log=None):
    """提交 AI 应用出图任务，返回 taskId。

    提交接口直接返回完整任务对象（`taskId`、`status`=RUNNING、`errorCode`……）。`taskId` 为空时
    按官方接口同一套规则分类：余额不足 / 临时故障 / 鉴权失败（401、403）/ 其他服务错误。
    """
    url = f"{RH_APP['base']}/openapi/v2/run/ai-app/{RH_APP['webapp_id']}"
    try:
        st, raw = rh_request(url, _app_payload(prompt), api_key, submit=True, log=log)
    except Exception as e:
        if netretry.is_transient_exc(e) and not netretry.is_unsent_exc(e):
            raise StepError(f"RunningHub 提交请求已发出、但没收到回复（{netretry.describe_exc(e)}）；"
                            f"服务端可能已经建了任务，为防重复扣费不自动重新提交，重跑会重新提交这一张",
                            kind=errors.KIND_NETWORK) from e
        raise
    tid = None
    try:
        j = json.loads(raw)
        d = j.get("data") if isinstance(j, dict) and isinstance(j.get("data"), dict) else j
        if isinstance(d, dict):
            v = d.get("taskId")
            tid = str(v) if v not in (None, "") else None
    except Exception:
        pass
    if not tid:
        if is_balance_reply(raw):
            kind = errors.KIND_BALANCE
        elif st in netretry.TRANSIENT_HTTP:
            kind = errors.KIND_NETWORK
        else:
            kind = errors.http_kind(st, raw) or errors.KIND_SERVICE
        raise StepError(f"RunningHub 提交失败 HTTP {st}: {raw[:300]}", kind=kind)
    return tid


def app_usage_usd(usage):
    """AI 应用成功结果里的 `usage` → 美元金额（`consumeMoney` + `thirdPartyConsumeMoney`）。

    两项都没有 / 都读不出数字时返回 None（调用方按单价记账）。服务返回的是字符串（如 "0.002"）。
    """
    if not isinstance(usage, dict):
        return None
    total, seen = 0.0, False
    for k in ("consumeMoney", "thirdPartyConsumeMoney"):
        v = usage.get(k)
        if v in (None, ""):
            continue
        try:
            total += float(v)
            seen = True
        except (TypeError, ValueError):
            continue
    return round(total, 6) if seen else None


def app_check(tid, api_key, log=None):
    """查一次 AI 应用任务状态（不等待）。返回 (status, x, usage)。

    status ∈ running/success/fail；success 时 x 是 `results[0].url`，usage 是结果里的 `usage`（可能为 None）。
    `status` 为空或不认识时当作"仍在运行"（P27 实测：并发时偶有一次查询返回的 status 为空，下一次恢复）。
    """
    st2, qraw = rh_request(f"{RH_APP['base']}/openapi/v2/query", {"taskId": tid}, api_key,
                           timeout=60, log=log)
    try:
        qj = json.loads(qraw)
    except Exception:
        qj = {}
    d = qj.get("data", qj) if isinstance(qj, dict) else qj
    if not isinstance(d, dict):
        d = {}
    status = str(d.get("status") or d.get("taskStatus") or "").lower()
    if status in ("success", "succeeded", "complete", "completed"):
        url = None
        res = d.get("results")
        if isinstance(res, list):
            for r in res:
                if isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"].startswith("http"):
                    url = r["url"]
                    break
        if not url:
            for v in _walk(res):
                if isinstance(v, str) and v.startswith("http"):
                    url = v
                    break
        if not url:
            raise StepError(f"RunningHub 任务成功但无输出 URL: {qraw[:300]}")
        return "success", url, d.get("usage")
    if status in ("fail", "failed", "error", "cancel", "cancelled"):
        reason = d.get("failedReason")
        if isinstance(reason, dict):
            reason = json.dumps(reason, ensure_ascii=False) if reason else ""
        return "fail", {"errorCode": str(d.get("errorCode") or ""),
                        "errorMessage": str(d.get("errorMessage") or reason or "")}, None
    if status not in ("running", "queued", "queue", "pending", "waiting", "created", "submitted", ""):
        _log(log, f"[S8] RunningHub 任务 {tid} 返回了不认识的状态 {status!r}，按仍在运行处理")
    elif not status:
        _log(log, f"[S8] RunningHub 任务 {tid} 这次查询的状态为空（HTTP {st2}），按仍在运行处理")
    return "running", None, None


def _log(log, line):
    try:
        (log or (lambda m: None))(line)
    except Exception:
        pass


def download(url, log=None, service="RunningHub"):
    """取回输出文件（GET，可重复）。临时故障按 `netretry` 重试。"""
    return netretry.call(lambda: urllib.request.urlopen(url, timeout=120).read(),
                         service=service, what="下载", log=log)


class ImageService:
    """出图服务接口。S8 只调这三个方法。"""

    name = ""
    service_key = ""       # 计价 / 账本用的服务名（pricing.PRICES 的键）
    label = ""             # 账本 S8 `image_service` 里记的名字（ST3-P27）

    def submit(self, prompt):
        raise NotImplementedError

    def check(self, task_id):
        raise NotImplementedError

    def download(self, url):
        raise NotImplementedError

    def usage_usd(self, task_id):
        """成功任务的实际金额（美元）；服务不给 / 读不出时 None（按单价记账）。"""
        return None


class RunningHubService(ImageService):
    """官方模型接口（备用，`A2V_IMAGE_SERVICE=official`）。请求与 v5.2 逐字节相同。"""
    name = "RunningHub"
    service_key = "runninghub"
    label = "rh_official:rhart-image-g-2"

    def __init__(self, api_key, log=None):
        self.api_key = api_key
        self.log = log

    def submit(self, prompt):
        return rh_submit(prompt, self.api_key, log=self.log)

    def check(self, task_id):
        return rh_check(task_id, self.api_key, log=self.log)

    def download(self, url):
        return download(url, log=self.log)


class RunningHubAppService(ImageService):
    """作者的 RunningHub AI 应用（默认）。参数见 `RH_APP`。"""
    name = "RunningHub"
    service_key = "runninghub_app"
    label = f"rh_app:{RH_APP['webapp_id']}"

    def __init__(self, api_key, log=None):
        self.api_key = api_key
        self.log = log
        self._usage = {}

    def submit(self, prompt):
        return app_submit(prompt, self.api_key, log=self.log)

    def check(self, task_id):
        status, x, usage = app_check(task_id, self.api_key, log=self.log)
        if status == "success":
            self._usage[str(task_id)] = usage
        return status, x

    def download(self, url):
        return download(url, log=self.log)

    def usage_usd(self, task_id):
        return app_usage_usd(self._usage.get(str(task_id)))


_CLASSES = {SERVICE_APP: RunningHubAppService, SERVICE_OFFICIAL: RunningHubService}
# 账本 pending_jobs 里出图任务的服务名 → 实现类（续查已登记的任务号要回到它当初提交的那个服务）。
BY_KEY = {c.service_key: c for c in _CLASSES.values()}
SERVICE_KEYS = tuple(BY_KEY)


class ServiceConfigError(ValueError):
    """`A2V_IMAGE_SERVICE` 的值不认识（cli 按参数错误处理，退出码 4）。"""


def selected():
    """当前选的出图服务：`app`（默认）或 `official`。空串按没设处理；其他值抛 ServiceConfigError。"""
    v = (os.environ.get(IMAGE_SERVICE_ENV) or "").strip().lower()
    if not v:
        return SERVICE_APP
    if v not in _CLASSES:
        raise ServiceConfigError(f"{IMAGE_SERVICE_ENV}={os.environ.get(IMAGE_SERVICE_ENV)!r} 不认识"
                                 f"（只能是 {SERVICE_APP} 或 {SERVICE_OFFICIAL}）")
    return v


def service_class():
    """当前选的出图服务的实现类（不建实例、不要密钥；计价、并发数用它的 `service_key`）。"""
    return _CLASSES[selected()]


def service_key():
    return service_class().service_key


def service_for(api_key, log=None):
    """S8 用的出图服务。以后换服务只改这里。"""
    return service_class()(api_key, log=log)


def service_by_key(key, api_key, log=None):
    """按账本里登记的服务名建实例（续查别的服务提交的任务号）。不认识的名字返回 None。"""
    cls = BY_KEY.get(key)
    return cls(api_key, log=log) if cls else None
