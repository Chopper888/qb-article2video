# -*- coding: utf-8 -*-
r"""密钥检测（ST3-P23 §3.7，`python -m pipeline.engine check-keys`）。

**铁律：检测不能产生任何费用。** 每个服务只调确认不计费的接口；拿不准的一律不调，返回 `unknown`。
密钥只读环境变量，不打印、不写盘。结果取值：
`ok` / `missing` / `key_invalid` / `balance_insufficient` / `network` / `unknown`。

各服务用的接口（不计费的依据写在回执 §8）：
  - deepseek：`GET https://api.deepseek.com/user/balance`（官方"查询余额"接口；DeepSeek 只按对话接口的
    token 用量计费，这个接口不经模型、没有 token）。`is_available=false` → 余额不足；
  - feishu：`POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal`（换取应用访问凭证，
    飞书开放平台接口本身不收费；引擎导入飞书文章时本来就要先换这个 token）；
  - runninghub：`POST https://www.runninghub.ai/uc/openapi/accountStatus`（官方"获取账户信息"，
    返回剩余 RH 币 `remainCoins`；只查账户、不跑工作流）；
  - ark（方舟）、tts（豆包语音）：用的是 API Key，能免费验证它的接口（查余额要 AK/SK 签名）没有找到
    官方说明，不调，返回 `unknown`（只查变量在不在）。
"""
import json
import os

from pipeline.engine import imagegen

SERVICES = ("deepseek", "ark", "tts", "runninghub", "feishu")
KEYS = {
    "deepseek": ["DEEPSEEK_API_KEY"],
    "ark": ["ARK_API_KEY"],
    "tts": ["MODEL_SPEECH_API_KEY"],
    "runninghub": ["RUNNINGHUB_API_KEY"],
    "feishu": ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
}
NAMES = {"deepseek": "DeepSeek", "ark": "方舟识图", "tts": "豆包语音合成",
         "runninghub": "RunningHub 出图", "feishu": "飞书"}

OK, MISSING, INVALID, BALANCE, NETWORK, UNKNOWN = (
    "ok", "missing", "key_invalid", "balance_insufficient", "network", "unknown")

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
RUNNINGHUB_ACCOUNT_URL = f"{imagegen.RUNNINGHUB_BASE}/uc/openapi/accountStatus"
TIMEOUT = 20


def parse_services(arg):
    """`--services a,b` → 列表；空 = 全部。不认识的服务名抛 ValueError。"""
    if not arg:
        return list(SERVICES)
    out = [s.strip() for s in arg.split(",") if s.strip()]
    bad = [s for s in out if s not in SERVICES]
    if bad:
        raise ValueError(f"不认识的服务：{', '.join(bad)}（可选 {', '.join(SERVICES)}）")
    return out


def _result(service, status, message, **extra):
    d = {"service": service, "service_name": NAMES[service], "status": status, "message": message,
         "env_vars": KEYS[service]}
    d.update(extra)
    return d


def _http(method, url, *, headers=None, body=None):
    """发一次请求，返回 (状态码, 正文 dict 或 None)；连不上返回 (None, None)。"""
    import requests
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
        else:
            r = requests.post(url, headers=headers, json=body, timeout=TIMEOUT)
    except Exception:
        return None, None
    try:
        data = r.json()
    except Exception:
        data = None
    return r.status_code, data


def check_deepseek(env, http=_http):
    code, data = http("GET", DEEPSEEK_BALANCE_URL,
                      headers={"Authorization": f"Bearer {env['DEEPSEEK_API_KEY']}",
                               "Accept": "application/json"})
    if code is None:
        return NETWORK, "连不上 DeepSeek", {}
    if code == 401:
        return INVALID, "密钥错误（HTTP 401）", {}
    if code == 402:
        return BALANCE, "余额不足（HTTP 402）", {}
    if code != 200 or not isinstance(data, dict):
        return (NETWORK if code in (429,) or code >= 500 else UNKNOWN), f"查询余额失败（HTTP {code}）", {}
    bal = [{"currency": b.get("currency"), "total": b.get("total_balance"),
            "granted": b.get("granted_balance"), "topped_up": b.get("topped_up_balance")}
           for b in data.get("balance_infos") or []]
    extra = {"balance": bal}
    if data.get("is_available") is False:
        return BALANCE, "余额不足（is_available=false）", extra
    return OK, "密钥有效", extra


def check_feishu(env, http=_http):
    code, data = http("POST", FEISHU_TOKEN_URL,
                      body={"app_id": env["FEISHU_APP_ID"], "app_secret": env["FEISHU_APP_SECRET"]})
    if code is None:
        return NETWORK, "连不上飞书", {}
    if isinstance(data, dict) and data.get("code") == 0 and data.get("tenant_access_token"):
        return OK, "应用凭证有效", {}
    if isinstance(data, dict) and data.get("code") is not None:
        # 10003 / 10014 等：app_id 或 app_secret 不对
        return INVALID, f"换取凭证失败（code {data.get('code')}：{str(data.get('msg'))[:80]}）", {}
    return (NETWORK if code >= 500 else UNKNOWN), f"换取凭证失败（HTTP {code}）", {}


def check_runninghub(env, http=_http):
    code, data = http("POST", RUNNINGHUB_ACCOUNT_URL,
                      headers={"Content-Type": "application/json", "Host": imagegen.RUNNINGHUB_HOST},
                      body={"apikey": env["RUNNINGHUB_API_KEY"]})
    if code is None:
        return NETWORK, "连不上 RunningHub", {}
    if isinstance(data, dict) and imagegen.is_balance_reply(data):
        # ST3-P26：和 S8 提交一样，errorCode 605（Your balance is insufficient）按余额不足报
        return BALANCE, f"余额不足（{str(data.get('errorMessage') or data.get('msg') or data.get('errorCode'))[:80]}）", {}
    if code != 200 or not isinstance(data, dict):
        return (INVALID if code in (401, 403) else NETWORK if code >= 500 else UNKNOWN), \
            f"查询账户失败（HTTP {code}）", {}
    if data.get("code") != 0:
        # 官方文档没有列出错误码的含义：只要没查成，按密钥不可用报（附原文），不猜是不是余额问题
        return INVALID, f"查询账户失败（code {data.get('code')}：{str(data.get('msg'))[:80]}）", {}
    d = data.get("data") or {}
    coins = d.get("remainCoins")
    extra = {"balance": [{"currency": "RH币", "total": coins}]} if coins is not None else {}
    try:
        if coins is not None and float(coins) <= 0:
            return BALANCE, "RH 币余额为 0", extra
    except (TypeError, ValueError):
        pass
    return OK, "密钥有效", extra


CHECKERS = {"deepseek": check_deepseek, "feishu": check_feishu, "runninghub": check_runninghub}


def check(services, env=None, http=_http):
    """逐个服务检测，返回结果列表。缺变量 → missing，不发请求；ark / tts → unknown，不发请求。"""
    env = dict(os.environ if env is None else env)
    out = []
    for s in services:
        miss = [k for k in KEYS[s] if not env.get(k)]
        if miss:
            out.append(_result(s, MISSING, "缺少环境变量：" + "、".join(miss), missing_keys=miss))
            continue
        fn = CHECKERS.get(s)
        if fn is None:
            out.append(_result(s, UNKNOWN, "变量已设置；这个服务没有确认不计费的检测接口，没有联网验证"))
            continue
        status, msg, extra = fn(env, http=http)
        out.append(_result(s, status, msg, **extra))
    return out


def summary(results):
    return json.dumps({r["service"]: r["status"] for r in results}, ensure_ascii=False)
