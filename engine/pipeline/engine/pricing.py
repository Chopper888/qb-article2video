# -*- coding: utf-8 -*-
"""单价表与费用折算（ST1-P4a 任务书 §一.4；ST3-P23 §3.4 补齐全部服务）。

约定：

- 单价写在本模块里，是**登记性**的（不是从网络查的）。每一项旁写来源网址、查询日期、计价口径；
  查不到准确数字的取一个**偏高**的估计值，`estimated=True`。
- 计价口径：
  - RunningHub 出图 = 张（`unit_price`，美元，按 `USD_TO_CNY` 折算）；
  - DeepSeek / 方舟 = tokens（`input_per_m` / `output_per_m`，元 / 百万 tokens），用量取接口返回的
    `usage`；拿不到 `usage` 时按字符数估算（1 字符记 1 个 token，偏高），并在记账里标 `usage_estimated`；
  - 豆包语音合成 = 提交文本的非空白字符数（`unit_price`，元 / 字）。
- 未登记的服务单价为"未知"，计费按 0 记，账本 `cost.incomplete` 标 True（口径："金额不完整"）。

**本模块不代表真实账单**：估计值取偏高，`estimated_cny` 用来告诉用户"大概花多少"、做费用上限。
"""

USD_TO_CNY = 7.10          # 可配置常量（汇率）
UNKNOWN = "未知"

# 发请求之前给文本类调用的保守预估：输入按提示词每个字符 1 个 token，输出按 max_tokens 全用满。
# 识图的图片按每张 IMAGE_TOKENS_EST 个 token 预估（方舟单图上限附近，偏高）。
CHARS_PER_TOKEN_EST = 1.0
IMAGE_TOKENS_EST = 4000

# service key -> 登记信息。
PRICES = {
    "runninghub": {
        "service": "RunningHub GPT Image 2", "unit": "张", "kind": "unit",
        "unit_price": 0.015, "currency": "USD", "estimated": False,
        "source": "ST1-P4a 任务书给定（$0.015/张，1k 16:9）", "checked": "2026-10-19",
        "basis": "按张：每张 $0.015，按 USD_TO_CNY 折人民币；失败的任务不收费",
    },
    # 默认出图服务（作者的 RunningHub AI 应用，模型同上）。名字与官方接口相同，用户不感知换了通道。
    "runninghub_app": {
        "service": "RunningHub GPT Image 2", "unit": "张", "kind": "unit",
        "unit_price": 0.020, "currency": "USD", "estimated": False,
        "source": "Windows 实测 2026-10-19（RH-AI应用实测回执：6 张，应用运行费 consumeMoney $0.002～0.005 "
                  "+ 模型费 thirdPartyConsumeMoney $0.015，每张 $0.017～0.020，取最高值）",
        "checked": "2026-10-19",
        "basis": "按张：预检和估算每张 $0.020；成功结果带 usage 时按 consumeMoney + thirdPartyConsumeMoney "
                 "实际金额记账，按 USD_TO_CNY 折人民币；失败的任务不收费",
    },
    "deepseek": {
        "service": "DeepSeek 文本（deepseek-v4-pro）", "unit": "tokens", "kind": "tokens",
        "input_per_m": 9.0, "output_per_m": 27.0, "currency": "CNY", "estimated": False,
        "source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing", "checked": "2026-10-19",
        "basis": "按 token：输入（缓存未命中）9 元/百万、输出 27 元/百万；取高峰时段价"
                 "（空闲时段半价，不按时段区分，所以偏高）；思考内容按输出计",
    },
    "ark": {
        "service": "方舟识图（doubao-seed-2-1-pro）", "unit": "tokens", "kind": "tokens",
        "input_per_m": 6.0, "output_per_m": 30.0, "currency": "CNY", "estimated": True,
        "source": "https://tech.ifeng.com/c/8uBqB6LMgIn（豆包 2.1 Pro 发布报道；官方定价页 "
                  "https://www.volcengine.com/docs/82379/1544106 为动态页面，未能读出数字）",
        "checked": "2026-10-19",
        "basis": "按 token：输入 6 元/百万、输出 30 元/百万（图片按方舟换算成输入 token）；"
                 "取报道里的在线推理价，不用批量价和缓存命中价",
    },
    "doubao_speech": {
        "service": "豆包语音合成（声音复刻 2.0，seed-icl-2.0）", "unit": "字（非空白）", "kind": "unit",
        "unit_price": 0.00032, "currency": "CNY", "estimated": False,
        "source": "用户账单实测 2026-10-19（火山引擎账单 2026-10-19 05:00 时段"
                  "「语音合成-豆包声音复刻模型」扣费 ¥2.82，对应 9018 字，约 3.13 元/万字，略往高取 3.2 元/万字）",
        "checked": "2026-10-19",
        "basis": "按字数：每个非空白字符 0.00032 元（3.2 元/万字）；重新提交按再计一次",
    },
}


def entry(service_key):
    """登记项；未登记的服务按"未知"处理（不抛错，按 0 计）。"""
    return PRICES.get(service_key) or {
        "service": service_key, "unit": "未知", "kind": "unit", "unit_price": None,
        "currency": "CNY", "estimated": True}


def is_priced(service_key):
    e = entry(service_key)
    if e.get("kind") == "tokens":
        return e.get("input_per_m") is not None and e.get("output_per_m") is not None
    return e.get("unit_price") is not None


def unit_price_cny(service_key):
    """按次 / 按张 / 按字的单价折人民币；按 token 计价或未知时返回 None。"""
    e = entry(service_key)
    if e.get("kind") == "tokens":
        return None
    p = e.get("unit_price")
    if p is None:
        return None
    return p * USD_TO_CNY if e.get("currency") == "USD" else float(p)


def cny_for(service_key, quantity):
    """按计量（张 / 字）算折合人民币。返回 `(cny, unit_price_cny_or_None)`。

    `cny` 在单价未知时为 0（不是 `None`）——调用方据此把 `cost.incomplete` 置位。
    """
    up = unit_price_cny(service_key)
    if up is None:
        return 0.0, None
    return round(up * float(quantity or 0), 4), up


def cny_tokens(service_key, input_tokens, output_tokens):
    """按 token 用量算折合人民币；未登记返回 0。"""
    e = entry(service_key)
    if e.get("kind") != "tokens" or not is_priced(service_key):
        return 0.0
    return round((float(input_tokens or 0) * e["input_per_m"]
                  + float(output_tokens or 0) * e["output_per_m"]) / 1e6, 6)


def pre_estimate(service_key, quantity=0.0, prompt_chars=None, max_tokens=None, images=0):
    """发请求之前的保守预估（费用上限检查用）。

    按 token 计价的服务：输入 = 提示词字符数（每字符 1 token）+ 每张图 IMAGE_TOKENS_EST，
    输出 = max_tokens 全用满；不知道提示词长度时输入记 0、只算输出。
    其他服务：单价 × 计量。
    """
    e = entry(service_key)
    if e.get("kind") == "tokens":
        tin = float(prompt_chars or 0) / CHARS_PER_TOKEN_EST + images * IMAGE_TOKENS_EST
        return cny_tokens(service_key, tin, float(max_tokens or 0))
    return cny_for(service_key, quantity)[0]


def charge_cny(amount, currency):
    """服务返回的实际金额折人民币（USD 按 USD_TO_CNY）。"""
    return round(float(amount) * (USD_TO_CNY if currency == "USD" else 1.0), 6)


def actual_cny(service_key, quantity=0.0, usage=None, prompt_chars=None, result=None, images=0):
    """一次真实调用之后的金额。返回 `(cny, usage_estimated)`。

    按 token 计价：有 `usage`（`{"prompt_tokens", "completion_tokens"}`，多次请求已累加）就按它算；
    没有就按字符数估：输入 = 提示词字符数 + 每张图 IMAGE_TOKENS_EST，输出 = 结果的字符数，
    `usage_estimated=True`。其他服务：服务报了实际金额（`usage["charge"]`，币种 `usage["charge_currency"]`，
    ST3-P27 AI 应用出图）就按它算，没报就单价 × 计量。
    """
    e = entry(service_key)
    if e.get("kind") != "tokens":
        if usage and usage.get("charge") is not None:
            return charge_cny(usage["charge"], usage.get("charge_currency") or e.get("currency")), False
        return cny_for(service_key, quantity)[0], False
    if usage and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
        return cny_tokens(service_key, usage.get("prompt_tokens"), usage.get("completion_tokens")), False
    out_chars = len(result) if isinstance(result, str) else len(str(result or ""))
    tin = float(prompt_chars or 0) / CHARS_PER_TOKEN_EST + images * IMAGE_TOKENS_EST
    return cny_tokens(service_key, tin, out_chars / CHARS_PER_TOKEN_EST), True


def describe(service_key):
    """人类可读的一行登记说明（回执用）。"""
    e = entry(service_key)
    if e.get("kind") == "tokens":
        price = f"输入 {e.get('input_per_m')} / 输出 {e.get('output_per_m')} 元每百万 tokens"
    else:
        up = e.get("unit_price")
        price = UNKNOWN if up is None else f"{up} {e.get('currency')}/{e.get('unit')}"
    est = "（估计）" if e.get("estimated") else ""
    return f"{e.get('service')} | 计量={e.get('unit')} | 单价={price}{est}"


def table():
    """全部登记项（回执的单价表）。"""
    return {k: dict(v) for k, v in PRICES.items()}
