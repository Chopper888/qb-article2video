# -*- coding: utf-8 -*-
r"""付费调用的统一入口（ST1-P4a 任务书 §一.2、§三.B）。

**所有付费调用都必须经过 `perform()`**，它依次负责：

  1. 查缓存（命中 → 直接返回，不发请求、不计费、不查 `--no-paid`、不查费用上限）
  2. 检查 `--no-paid`
  3. 检查费用上限（**在发请求之前**；超了就抛 `PaidBlocked`，退出码 2）
  4. 真实调用（或桩）
  5. 写缓存（失败结果不写，ST3-P23）
  6. 记账（写进本次 `run` 的步骤 `cost`）

缓存键与 A5 定稿完全一致（与已有 `pipeline/shot_ai.py:_cache_key` 同构，
所以历史上按内容落盘的缓存**继续有效**）：

    sha256(model + "|" + prompt + "|" + extra + "|" + json.dumps(params, sort_keys=True))

**键里只有请求内容**：模型名、提示词全文、参数、图片字节哈希。绝不出现
组号、文件路径、时间戳这类与内容无关的东西（A5 第 4 条的错位反例）。

缓存值落在 `cache\<key>.json`；结果是二进制（出图 PNG、配音 WAV）时，
二进制存同目录的 `cache\<key>.bin`，JSON 里只留 `sha256` 与指针。

ST3-P23 §3.2：**失败结果不进缓存**。外部服务返回 None、空串、空二进制，或调用方给的 `valid`
判为不合格时，一律不写缓存（按失败交还调用方）；读缓存时命中的是 None / 空结果（旧版本写进去的
"毒缓存"）当作没命中。空列表 / 空字典不算失败：S7 的决策函数可能合法地返回空（例如没有导览点）。

ST3-P23 §3.4：费用上限覆盖所有服务。发请求前按"已花 + 本次保守预估"检查（文本类按提示词长度和
max_tokens 预估）；调用后按接口返回的 usage 记实际金额（真实调用封装经 `note_usage` 报上来），
拿不到 usage 时按字符数估并标 `usage_estimated`。
"""
import hashlib
import json
import threading
from pathlib import Path

from pipeline.engine import errors, pricing, stopflag
from pipeline.engine.errors import KIND_COST_LIMIT, PaidBlocked

CACHE_DIRNAME = "cache"
BLOB_SUFFIX = ".bin"

# RunningHub 出图的请求内容常量（A5 缓存键口径：模型名 + 提示词 + 参数）。
# 出图缓存落 `07-出图\cache`；准备脚本与 S8 必须用同一套常量，否则键对不上。
RUNNINGHUB_MODEL = "runninghub-gpt-image-2"
RUNNINGHUB_ASPECT = "16:9"
RUNNINGHUB_RESOLUTION = "1k"


def runninghub_key(prompt, aspect=RUNNINGHUB_ASPECT, resolution=RUNNINGHUB_RESOLUTION):
    """出图请求的缓存键。`prompt` 是提示词全文，键里**不含组号、文件路径**。"""
    return cache_key(RUNNINGHUB_MODEL, prompt,
                     {"aspect_ratio": aspect, "resolution": resolution})


# --------------------------------------------------------------------------
# 缓存键 / 读写
# --------------------------------------------------------------------------


def cache_key(model, prompt, params=None, extra=""):
    """按请求内容算缓存键（A5 定稿口径，与 shot_ai._cache_key 同构）。"""
    s = (model + "|" + prompt + "|" + extra + "|"
         + json.dumps(params or {}, sort_keys=True, ensure_ascii=False))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def cache_paths(cache_dir, key):
    d = Path(cache_dir)
    return d / (key + ".json"), d / (key + BLOB_SUFFIX)


def is_failed_result(result):
    """外部服务调用的失败结果：None、空白字符串、空二进制。"""
    if result is None:
        return True
    if isinstance(result, str):
        return not result.strip()
    if isinstance(result, (bytes, bytearray)):
        return len(result) == 0
    return False


def load_cached(cache_dir, key):
    """读缓存。返回 `(hit: bool, result, record)`；未命中时 result 为 None。

    命中的是失败结果（None / 空，旧版本写进去的毒缓存）时按没命中返回，`record` 里带
    `poisoned: True`，调用方据此计数（不删文件，留给人看）。
    """
    meta_p, blob_p = cache_paths(cache_dir, key)
    if not meta_p.is_file():
        return False, None, None
    rec = json.loads(meta_p.read_text(encoding="utf-8"))
    if rec.get("blob"):
        if not blob_p.is_file():
            return False, None, None
        data = blob_p.read_bytes()
        if is_failed_result(data):
            return False, None, {"poisoned": True}
        return True, data, rec
    # 引擎写的是 `result`；历史缓存（`shot_ai.make_cached_text_call`/`_vision_call`
    # 与 P9 的停留判断）写的是 `content`——两种都要认，否则 S6 缓存回放会全部落空。
    val = rec.get("result") if "result" in rec else rec.get("content")
    if is_failed_result(val):
        return False, None, {"poisoned": True}
    return True, val, rec


def store_cached(cache_dir, key, result, *, step, service, extra=None):
    """写缓存。`result` 是 bytes → 拆成 .bin + 指针。失败结果（见 `is_failed_result`）不写，返回 None。"""
    if is_failed_result(result):
        return None
    meta_p, blob_p = cache_paths(cache_dir, key)
    meta_p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"key": key, "step": step, "service": service}
    if extra:
        rec.update(extra)
    if isinstance(result, (bytes, bytearray)):
        blob_p.write_bytes(bytes(result))
        rec["blob"] = True
        rec["sha256"] = hashlib.sha256(bytes(result)).hexdigest()
        rec["size"] = len(result)
    else:
        rec["blob"] = False
        rec["result"] = result
        if isinstance(result, str):
            # 同时写 `content`，与 `shot_ai.make_cached_text_call` 的历史格式互通
            # （旧脚本读 `.get("content")`）。两个键同值，读哪边都对。
            rec["content"] = result
    meta_p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta_p


# --------------------------------------------------------------------------
# 用量（usage）：真实调用封装在拿到接口返回后报上来，perform 据此记实际金额
# --------------------------------------------------------------------------

_usage = threading.local()


def note_usage(prompt_tokens=None, completion_tokens=None):
    """真实调用拿到接口返回的 usage 后调用（一次 invoke_real 里多次请求会累加）。"""
    box = getattr(_usage, "box", None)
    if box is None:
        return
    box["prompt_tokens"] = box.get("prompt_tokens", 0) + int(prompt_tokens or 0)
    box["completion_tokens"] = box.get("completion_tokens", 0) + int(completion_tokens or 0)
    box["requests"] = box.get("requests", 0) + 1


def note_charge(amount, currency="USD"):
    """真实调用拿到服务报的实际金额后调用（ST3-P27：AI 应用出图结果里的 usage）。

    `pricing.actual_cny` 见到 `charge` 就按它记账，不再按单价。一次 invoke_real 里多次报会累加。
    """
    box = getattr(_usage, "box", None)
    if box is None or amount is None:
        return
    box["charge"] = round(box.get("charge", 0.0) + float(amount), 6)
    box["charge_currency"] = currency


def _begin_usage():
    _usage.box = {}


def _end_usage():
    box = getattr(_usage, "box", None)
    _usage.box = None
    return box or None


# --------------------------------------------------------------------------
# 记账
# --------------------------------------------------------------------------


_record_lock = threading.Lock()


def _record(ctx, *, step, service, quantity, cache_hit, cny, unit_price, from_stub=False,
            usage=None, usage_estimated=False, failed=False):
    # ST3-P26：S8 并行出图时几个线程会同时记账。
    with _record_lock:
        _record_unlocked(ctx, step=step, service=service, quantity=quantity, cache_hit=cache_hit,
                         cny=cny, unit_price=unit_price, from_stub=from_stub, usage=usage,
                         usage_estimated=usage_estimated, failed=failed)


def _record_unlocked(ctx, *, step, service, quantity, cache_hit, cny, unit_price, from_stub,
                     usage, usage_estimated, failed):
    st = ctx.paid_stats
    if cache_hit:
        st["hits"] += 1
    else:
        st["calls"] += 1
        if from_stub:
            # 桩调用的次数（ST1-P4a2 §一.6）：S10 只看这个数，>0 就不许出片。
            # `paid_calls` 只算**真正发出去的**付费调用——桩不发请求，不算。
            st["stub_calls"] = st.get("stub_calls", 0) + 1
        else:
            st["paid_calls"] += 1
            # 真实花掉的钱（step_done.estimated_cny 用；桩的预估金额不算花费）
            st["real_cny"] = round(st.get("real_cny", 0.0) + (cny or 0.0), 6)
        if failed:
            st["failed_results"] = st.get("failed_results", 0) + 1
        if usage_estimated:
            st["usage_estimated"] = True
    if cny:
        st["estimated_cny"] = round(st.get("estimated_cny", 0.0) + cny, 4)
    if not pricing.is_priced(service):
        st["incomplete"] = True
    log = getattr(ctx, "paid_log", None)
    if log is not None:
        log.append({
            "step": step, "service": service, "service_name": pricing.entry(service)["service"],
            "unit": pricing.entry(service)["unit"], "quantity": quantity,
            "unit_price_cny": unit_price, "cny": cny, "cache_hit": bool(cache_hit),
            "usage": usage, "usage_estimated": bool(usage_estimated), "failed": bool(failed),
            "source": ("缓存" if cache_hit else ("桩" if from_stub else "真实")),
        })


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------


def perform(ctx, *, step, service, cache_dir, key, describe,
            quantity=1.0, invoke_real, invoke_stub=None, prompt_chars=None, max_tokens=None,
            images=0, valid=None, reserved_cny=0.0, cache_service=None, alt_keys=()):
    """统一付费入口。返回缓存/调用结果（JSON 可序列化值，或 bytes）。

    - `quantity` 是**发请求之前**能给出的计量（出图=1 张；配音=非空白字数；文本调用传 0）。
    - 文本 / 识图（按 token 计价）：`prompt_chars` 提示词字符数、`max_tokens` 输出上限、`images` 图片张数，
      用来做发请求前的保守预估；调用后按 usage 记实际金额。
    - `valid(result) -> bool`：调用方对结果的额外校验（如解析失败）；不合格与 None / 空一样不写缓存。
    - `invoke_real` 真实实现；`invoke_stub` 桩（本轮 `ctx.stub` 为真时用它）。
    - 命中缓存时**不调用任何一个**，也不查 `--no-paid` / 费用上限。命中毒缓存按没命中处理。
    - `reserved_cny`：已经提交、还没记账的其他任务的预估金额（S8 并行出图时），费用上限检查一并算上。
    - `cache_service`：缓存记录里写的服务名（默认同 `service`）。ST3-P27：两个出图服务共用一套出图缓存，
      缓存记录里统一写 `runninghub`，计价、记账仍按 `service`（实际用的服务）。
    - `alt_keys`（R101 N）：同一个请求的其他写法算出的键（例如源文件换行符不同时），`key` 没命中就依次查它们；
      只读不写（命中旧键不另存一份），新结果只写在 `key` 下。
    """
    hit, result, rec = load_cached(cache_dir, key)
    if not hit:
        for alt in alt_keys:
            if not alt or alt == key:
                continue
            a_hit, a_result, a_rec = load_cached(cache_dir, alt)
            if a_hit:
                hit, result, rec = a_hit, a_result, a_rec
                break
    if hit:
        _record(ctx, step=step, service=service, quantity=rec.get("quantity", quantity),
                cache_hit=True, cny=0.0, unit_price=pricing.unit_price_cny(service))
        return result
    if rec and rec.get("poisoned"):
        st = getattr(ctx, "paid_stats", None)
        if st is not None:
            st["poisoned_hits"] = st.get("poisoned_hits", 0) + 1
        _log(ctx, f"[{step}] 缓存里是失败结果（毒缓存），按没命中处理：{describe}")

    if ctx.no_paid:
        raise PaidBlocked(f"禁止付费：{describe}（{step}）")

    est_cny = pricing.pre_estimate(service, quantity, prompt_chars, max_tokens, images)
    unit_price = pricing.unit_price_cny(service)
    spent = ctx.spent_cny + (reserved_cny or 0.0)
    if ctx.max_cost_cny is not None and (spent + est_cny) > ctx.max_cost_cny:
        held = f"（含已提交未记账 ¥{reserved_cny:.4f}）" if reserved_cny else ""
        raise PaidBlocked(
            f"超出费用上限：本次预计花费 ¥{est_cny:.4f}（{describe}），"
            f"累计 ¥{spent:.4f}{held} + ¥{est_cny:.4f} > 上限 ¥{ctx.max_cost_cny:.4f}（{step}）",
            kind=KIND_COST_LIMIT)

    # P31b：已经要求停止就不再发新的付费请求（含桩，行为一致）。
    stopflag.check()
    usage, usage_est = None, False
    if ctx.stub:
        if invoke_stub is None:
            raise PaidBlocked(f"禁止付费：本轮为桩模式，但 {step} 未提供桩（{describe}）")
        result = invoke_stub()
        cny = est_cny
    else:
        _begin_usage()
        try:
            result = invoke_real()
        except Exception as e:
            # P31：真实调用里抛出的异常补标服务（失败事件的 `service` 字段）
            errors.tag_service(e, service)
            raise
        finally:
            usage = _end_usage()
        stored = result if not isinstance(result, tuple) else result[0]
        cny, usage_est = pricing.actual_cny(service, quantity, usage, prompt_chars,
                                            stored if isinstance(stored, str) else None, images)
        # 缺陷a：真实付费成功后立即累加 spent_cny 并写账本原子落盘（中断/强杀不丢账）。
        ctx.record_paid(cny, step)

    stored = result if not isinstance(result, tuple) else result[0]
    failed = is_failed_result(stored) or (valid is not None and not _safe_valid(valid, stored))
    if failed:
        # §3.2：失败结果不写缓存，原样交还调用方（调用方本来就按 None / 空 / 不合格处理）。
        _log(ctx, f"[{step}] 调用结果为空或不合格，不写缓存：{describe}")
    else:
        store_cached(cache_dir, key, stored, step=step, service=cache_service or service,
                     extra={"describe": describe, "quantity": quantity})
    _record(ctx, step=step, service=service, quantity=quantity,
            cache_hit=False, cny=cny, unit_price=unit_price,
            from_stub=bool(ctx.stub), usage=usage, usage_estimated=usage_est, failed=failed)
    return result


def _safe_valid(valid, result):
    try:
        return bool(valid(result))
    except Exception:
        return False


def _log(ctx, msg):
    fn = getattr(ctx, "log", None)
    if callable(fn):
        try:
            fn(msg)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 给"注入式"调用点（shot_ai 的 text_call / vision_call）用的封装
# --------------------------------------------------------------------------


def make_paid_text_call(ctx, *, step, cache_dir, model, describe,
                        invoke_real, invoke_stub=None, service="deepseek"):
    """包一个 `text_call(prompt, max_tokens=…) -> str|None`，走统一入口。

    键与 `shot_ai._cache_key(model, prompt, {"max_tokens": …})` 完全一致，
    所以历史按内容落盘的缓存继续命中。
    """
    def text_call(prompt, max_tokens=4096):
        key = cache_key(model, prompt, {"max_tokens": max_tokens})
        return perform(ctx, step=step, service=service, cache_dir=cache_dir, key=key,
                       describe=f"{describe}(max_tokens={max_tokens})", quantity=0.0,
                       prompt_chars=len(prompt), max_tokens=max_tokens,
                       invoke_real=lambda: invoke_real(prompt, max_tokens=max_tokens),
                       invoke_stub=(None if invoke_stub is None
                                    else lambda: invoke_stub(prompt, max_tokens=max_tokens)))

    return text_call


def make_paid_vision_call(ctx, *, step, cache_dir, model, describe,
                          invoke_real, invoke_stub=None, service="ark"):
    """包一个 `vision_call(image_bytes, prompt, max_tokens=…, timeout=…) -> str|None`。

    键与 `shot_ai._cache_key(..., extra=sha256(图片字节))` 完全一致。
    """
    def vision_call(image_bytes, prompt, max_tokens=512, timeout=60):
        extra = hashlib.sha256(image_bytes).hexdigest()
        key = cache_key(model, prompt, {"max_tokens": max_tokens}, extra=extra)
        return perform(ctx, step=step, service=service, cache_dir=cache_dir, key=key,
                       describe=f"{describe}(max_tokens={max_tokens})", quantity=0.0,
                       prompt_chars=len(prompt), max_tokens=max_tokens, images=1,
                       invoke_real=lambda: invoke_real(image_bytes, prompt,
                                                       max_tokens=max_tokens, timeout=timeout),
                       invoke_stub=(None if invoke_stub is None
                                    else lambda: invoke_stub(image_bytes, prompt,
                                                             max_tokens=max_tokens,
                                                             timeout=timeout)))

    return vision_call
