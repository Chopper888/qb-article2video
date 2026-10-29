# -*- coding: utf-8 -*-
r"""开跑前的费用范围（ST3-P23 §3.5，`python -m pipeline.engine estimate`）。

用在 `import` 之后、`run` 之前：**不联网、不要密钥、不花钱、不写盘**。

做法：先按与 `plan` 相同的跳过判定（`cli._survey` + 定稿锁）定出这一轮哪些付费步骤要跑，再对每个
要跑的付费步骤估"调用次数 × 每次的钱"，给出低 / 高两个数。估算只用开跑前就知道的东西：

  - C：`00-输入/文章.md` 的非空白字数；I：md 里能用的图片张数；内容类型（已知 / 推测 / 未知）；
  - 口播字数 N、分组数 G：已有产物就用实际值，没有就按 C 推（系数见 `RATIOS`，由 ST3-P8 四篇校准）；
  - 出图张数：已有 `06-编排/出图需求.json` 就数还没出、也没缓存的；没有就按 G 推；
  - 配音：已有口播就按实际要提交的字数，并查配音缓存。

范围宁可偏宽、不能偏低：高值按 DeepSeek 高峰时段价、方舟的偏高估计价、豆包语音的账单实测价（见 pricing.py），
低值 DeepSeek 按空闲时段的半价。能被已有缓存省掉的（步骤会跳过、图已出或已缓存、配音已缓存）
记进 `saved_by_cache_cny`，不计入 `low_cny` / `high_cny`。
"""
import json
import re
from pathlib import Path

from pipeline.engine import imagegen, intake, ledger, paid, pricing, spec

# 由 ST3-P8 四篇（001-示例篇、002-示例篇-截图、59-ST3F1、72-ST3F3）校准的系数（低, 高）。
# 校准数据和对比表见 ST3-P23 回执 §7。
RATIOS = {
    "narration_per_char": (0.55, 1.10),     # 口播字数 / 原文字数：实测 0.70～0.99
    "chars_per_group": (45, 85),            # 原文字数 / 分组数：实测 51～83
    "illus_per_group_theory": (0.5, 1.0),   # 理论型出图张数 / 分组数：实测 0.81
    "illus_per_group_shot": (0.2, 0.9),     # 截图型出图张数 / 分组数：实测 0.27～0.70
    "s6_text_per_image": (2.5, 9.0),        # S6 DeepSeek 调用 / 截图张数
    "s6_vision_per_image": (0.5, 1.5),      # S6 方舟识图 / 截图张数
    "s7_per_group": (0.3, 1.2),             # S7 决策调用 / 分组数
    "s7_annot_per_image": (0.5, 3.0),       # S7 逐句标注调用 / 截图张数
    "s1_calls": (1, 3),                     # 口播生成 + 命令重写
    "s3_submits": (1, 1.5),                 # 配音提交次数（提交失败重新提交按再计一次）
}

# 平均每次调用的 token（低, 高）：输入、输出。S6/S7 由缓存回放实测（ST3-P23 回执 §7）：高值取实测平均的
# 2 倍上下（单次最长的提示词 6682 字，但一篇文章几十上百次调用平均下来在 1000～1400 字）。
TOKENS = {
    "s0_vision": ((1100, 4300), (20, 100)),     # 提示词约 300 字 + 一张图（800～4000 token）
    "s6_text": ((500, 2500), (60, 400)),        # 实测提示词均 1050～1230 字；结果均 150～180 字
    "s6_vision": ((1100, 4300), (30, 150)),     # 实测提示词 300 字、结果均 64 字
    "s7": ((1000, 4000), (50, 800)),            # 逐句标注实测均 1000～1400 字；决策调用的提示词在函数内部，按偏高估
    "s1_reasoning": (1000, 20000),              # 口播开了深度思考，思考内容按输出计
}
DEEPSEEK_LOW_FACTOR = 0.5                       # 低值按 DeepSeek 空闲时段价（高峰价的一半）

SERVICES = ("deepseek", "ark", "doubao_speech", "runninghub")
# ST3-P27：输出里出图那一栏的键仍是 `runninghub`（APP 已认），单价按当前选的出图服务
# （默认 AI 应用 `runninghub_app`，`A2V_IMAGE_SERVICE=official` 时官方接口）。
IMAGE_BUCKET = "runninghub"


def _price_key(s):
    return imagegen.service_key() if s == IMAGE_BUCKET else s


def _nonspace(s):
    return len(re.sub(r"\s", "", s or ""))


def _tok_cny(service, tin, tout, low):
    c = pricing.cny_tokens(service, tin, tout)
    return c * DEEPSEEK_LOW_FACTOR if (low and service == "deepseek") else c


class _Acc:
    """按服务累加 (低, 高)，另记每一步的分项。"""

    def __init__(self):
        self.by_service = {s: [0.0, 0.0] for s in SERVICES}
        self.by_step = {}
        self.saved = {s: 0.0 for s in SERVICES}

    def add(self, step, service, lo, hi, note):
        self.by_service[service][0] += lo
        self.by_service[service][1] += hi
        d = self.by_step.setdefault(step, {"low_cny": 0.0, "high_cny": 0.0, "items": []})
        d["low_cny"] += lo
        d["high_cny"] += hi
        d["items"].append({"service": service, "low_cny": round(lo, 4), "high_cny": round(hi, 4),
                           "basis": note})

    def save(self, service, hi):
        self.saved[service] += hi


def _calls(acc, step, service, n_lo, n_hi, tok, note):
    (ilo, ihi), (olo, ohi) = tok
    acc.add(step, service, n_lo * _tok_cny(service, ilo, olo, True),
            n_hi * _tok_cny(service, ihi, ohi, False),
            f"{note}：{_fmt(n_lo)}～{_fmt(n_hi)} 次，每次输入 {ilo}～{ihi}、输出 {olo}～{ohi} tokens")


def _fmt(x):
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def _plan_actions(root, article, ct):
    """与 `plan` 相同的跳过判定（含定稿锁）：{步骤: (run|skip, 原因)}。"""
    from pipeline.engine import cli
    steps = spec.steps_for(ct)
    order = spec.order_for(ct)
    doc = ledger.load(root) or ledger.new_doc(article, ct)

    class _A:
        cmd, from_step, to_step = "plan", None, None
    res, _ = cli._survey(root, _A, ct, steps, order, doc)
    lock = cli._read_narration_lock(root)
    if lock is not None and lock != "__corrupt__" and not cli._narration_source_changed(root, lock):
        cli._apply_narration_flags(res, order, lock, False, False)
    return res, steps, order


def _illus_remaining(root):
    """已有出图需求时：(总张数, 还没出也没缓存的张数)。没有清单返回 None。"""
    p = Path(root) / "06-编排" / "出图需求.json"
    if not p.is_file():
        return None
    try:
        items = [it for it in json.loads(p.read_text(encoding="utf-8")).get("items") or []
                 if it.get("请求内容哈希")]
    except Exception:
        return None
    from pipeline.engine import steps as st
    cache = Path(root) / "07-出图" / "cache"
    # R101 B：与 S8 同一判定——已有图片只有缓存键对得上才算"已出"，否则看缓存
    plan = st.illustration_plan(root, items)
    left = 0
    for it in items:
        if plan.get(it.get("文件名")) == "reuse":
            continue
        if paid.load_cached(cache, it["请求内容哈希"])[0]:
            continue
        left += 1
    return len(items), left


def replay_narration(root, article):
    """R101 K：口播能不能从缓存原样回放（清理过中间文件的文章 narration.txt 没了，但 01-口播/cache 还在）。

    用 `--no-paid` 的上下文跑 S1 的计算部分：命中缓存不写盘、不联网；有任何一次没命中就抛 PaidBlocked。
    能回放返回口播全文（和 S1 写进 narration.txt 的一样，不含末尾换行），否则 None。
    """
    from pipeline.engine import steps as st
    from pipeline.engine.errors import PaidBlocked
    root = Path(root)
    if not (root / "00-输入" / "文章.md").is_file() or not (root / "01-口播" / "cache").is_dir():
        return None
    ctx = st.Ctx(root, article, None, None, True, lambda m: None, runtime_node_modules=root, stub=False)
    try:
        return st.compose_narration(ctx)["text"]
    except (PaidBlocked, st.StepError, OSError, ValueError):
        return None


def _tts_remaining(root, narration=None):
    """已有口播时：(要提交的非空白字数, 是否已缓存)。没有口播返回 None。

    R101 K：`narration` 给了就用它（清理后从缓存回放出来的口播），不读 narration.txt。
    """
    from pipeline.engine import steps as st
    p = Path(root) / "01-口播" / "narration.txt"
    if narration is None and not p.is_file():
        return None
    text = st._tts_text(narration + "\n" if narration is not None else p.read_text(encoding="utf-8"))
    key = paid.cache_key(st._SPEECH_MODEL, text,
                         {"service": "doubao-speech", "speaker": st.tts_speaker(),
                          "speech_rate": st._TTS_SPEECH_RATE, "sample_rate": 24000, "format": "wav"})
    return _nonspace(text), paid.load_cached(Path(root) / "03-配音" / "cache", key)[0]


# R101 K：口播、配音都能从缓存原样回放时，下面这几步的请求预计与上次完全相同。
_REPLAY_STEPS = ("S6", "S7", "S8")


def _cache_replay_steps(root, res, replayed):
    """R101 K：清理过中间文件、但缓存还在的文章，哪些付费步骤预计全部命中缓存。

    条件（都满足才算）：口播能原样回放（`replayed`）；配音缓存命中（音频同上次）；`00-输入` 没变
    （S0 判为跳过）；账本里这一步上次真跑完成（status=done，不是导入）；这一步的缓存目录还在。
    依据：上次跑完时这一步的每个请求结果都写进了缓存，输入（口播、音频、原文、原图）都没变，请求就和上次一样。
    """
    from pipeline.engine import steps as st
    tts = _tts_remaining(root, replayed)
    if not tts or not tts[1] or (res.get("S0") or ("run",))[0] != "skip":
        return set()
    doc = ledger.load(root) or {}
    dirs = {"S6": [st._CACHE_DIR["S6_DIRECTOR"], st._CACHE_DIR["S6_STAY"]],
            "S7": [st._CACHE_DIR["S7"]], "S8": [st._CACHE_DIR["S8"]]}
    out = set()
    for sid in _REPLAY_STEPS:
        rec = ledger.find_step(doc, sid) or {}
        if rec.get("status") != ledger.STATUS_DONE:
            continue
        if all((Path(root).joinpath(*d)).is_dir() and any(Path(root).joinpath(*d).iterdir()) for d in dirs[sid]):
            out.add(sid)
    return out


def _hit_step(acc, step, parts):
    """预计全部命中缓存的步骤：记 0，按高值记进「缓存省掉的」。parts: [(服务, 次数高值, TOKENS)]。"""
    for service, n_hi, tok in parts:
        (_, ihi), (_, ohi) = tok
        acc.save(service, n_hi * _tok_cny(service, ihi, ohi, False))
        acc.add(step, service, 0.0, 0.0, "口播、配音能从缓存原样回放，上次这一步已完成且缓存还在：预计全部命中缓存")


def _prev_images_cached(root):
    """账本里上次 S8 出的图（张数），以及其中字节还在 `07-出图/cache` 里的张数。"""
    doc = ledger.load(root) or {}
    rec = ledger.find_step(doc, "S8") or {}
    shas = {o.get("sha256") for o in rec.get("outputs") or []
            if str(o.get("path", "")).startswith("07-出图/images/")}
    shas.discard(None)
    have = set()
    cache = Path(root) / "07-出图" / "cache"
    if cache.is_dir():
        for m in cache.glob("*.json"):
            try:
                have.add(json.loads(m.read_text(encoding="utf-8")).get("sha256"))
            except (OSError, ValueError):
                continue
    return len(shas), len(shas & have)


def estimate(root, article, ct_preview, fresh=False):
    """算费用范围，返回 `estimate` 事件的字段。`fresh=True`：当作什么都没跑过、没有缓存（校准用）。"""
    from pipeline.engine import steps as st
    root = Path(root)
    inp = root / "00-输入"
    md = (inp / "文章.md").read_text(encoding="utf-8") if (inp / "文章.md").is_file() else ""
    C = _nonspace(md)
    _, n_img = intake.check_article(inp) if md else ([], 0)
    I = n_img

    # 内容类型：已知（来源.json / 图片分类）> 推测（没有图 → 理论型）> 未知（两种都算，取并集）
    if ct_preview:
        types, basis = [ct_preview], "已知"
    elif I == 0:
        types, basis = [spec.CONTENT_TYPE_THEORY], "推测（没有原图，按理论型）"
    else:
        types, basis = [spec.CONTENT_TYPE_THEORY, spec.CONTENT_TYPE_SCREENSHOT], "未知（两种都算，取并集）"

    narr_p = root / "01-口播" / "narration.txt"
    gr_p = root / "02-分组" / "groups_raw.json"
    exact_n = None if fresh or not narr_p.is_file() else _nonspace(narr_p.read_text(encoding="utf-8"))
    # R101 K：清理过中间文件的文章没有 narration.txt，但口播缓存还在 → 先看能不能原样回放
    replayed = None if fresh or narr_p.is_file() else replay_narration(root, article)
    if replayed is not None:
        exact_n = _nonspace(replayed)
    exact_g = None
    if not fresh and gr_p.is_file():
        try:
            exact_g = len(json.loads(gr_p.read_text(encoding="utf-8")).get("groups") or [])
        except Exception:
            exact_g = None
    N = (exact_n, exact_n) if exact_n is not None else (
        C * RATIOS["narration_per_char"][0], C * RATIOS["narration_per_char"][1])
    G = (exact_g, exact_g) if exact_g is not None else (
        C / RATIOS["chars_per_group"][1], C / RATIOS["chars_per_group"][0])

    results = []
    for ct in types:
        acc = _Acc()
        if fresh:
            order = spec.order_for(ct)
            res = {s: ("run", "按从头跑估") for s in order}
            steps = spec.steps_for(ct)
        else:
            res, steps, order = _plan_actions(root, article, ct)
        run = {s for s in order if res[s][0] == "run" and steps[s]["cost"]}
        # 上游这一轮要重跑的付费步骤：开跑前判为跳过，但轮到时可能因输入变化改为执行（cli._recheck）。
        # 高值照全额算，低值记 0（上游重跑后产出没变时照旧跳过）。定稿锁锁住的 S1～S3 不算。
        maybe = set()
        if not fresh:
            locked = {s for s in ("S1", "S2", "S3") if "定稿" in (res.get(s) or ("", ""))[1]}
            for s in order:
                if res[s][0] == "run":
                    maybe |= {d for d in spec.downstream_of(ct, s)
                              if steps[d]["cost"] and d not in run and d not in locked}
            run |= maybe
        skipped_paid = [s for s in order if steps[s]["cost"] and s not in run]

        # S0：内容类型没定、又有图时，每张图识一次
        if "S0" in run and not ct_preview and I:
            _calls(acc, "S0", "ark", 0, I, TOKENS["s0_vision"], f"识图分类（{I} 张原图）")
        hit_like = _cache_replay_steps(root, res, replayed) if replayed is not None else set()
        # S1：口播
        if "S1" in run and replayed is not None:
            tpl = (len(Path(st._NARRATION_TEMPLATE).read_text(encoding="utf-8"))
                   + len(st._ELEMENT_RULE) + len(st._OPENING_RULE))
            acc.save("deepseek", RATIOS["s1_calls"][1]
                     * _tok_cny("deepseek", tpl + C, N[1] + TOKENS["s1_reasoning"][1], False))
            acc.add("S1", "deepseek", 0.0, 0.0, "口播能从缓存原样回放（清理过中间文件），不花钱")
        elif "S1" in run:
            tpl = (len(Path(st._NARRATION_TEMPLATE).read_text(encoding="utf-8"))
                   + len(st._ELEMENT_RULE) + len(st._OPENING_RULE))
            lo_n, hi_n = RATIOS["s1_calls"]
            r_lo, r_hi = TOKENS["s1_reasoning"]
            acc.add("S1", "deepseek",
                    lo_n * _tok_cny("deepseek", tpl + C, N[0] + r_lo, True),
                    hi_n * _tok_cny("deepseek", tpl + C, N[1] + r_hi, False),
                    f"口播生成 {lo_n}～{hi_n} 次：输入约 {tpl + C} tokens（模板 + 原文 {C} 字），"
                    f"输出 {int(N[0] + r_lo)}～{int(N[1] + r_hi)} tokens（口播 + 思考）")
        # S3：配音
        if "S3" in run:
            tts = None if fresh else _tts_remaining(root, replayed)
            lo_s, hi_s = RATIOS["s3_submits"]
            up = pricing.unit_price_cny("doubao_speech")
            if tts is not None and tts[1]:
                acc.save("doubao_speech", tts[0] * up * hi_s)
            else:
                n_lo, n_hi = (tts[0], tts[0]) if tts else (N[0] * 0.97, N[1])
                acc.add("S3", "doubao_speech", lo_s * n_lo * up, hi_s * n_hi * up,
                        f"配音 {_fmt(n_lo)}～{_fmt(n_hi)} 字 × {lo_s}～{hi_s} 次提交")
        elif "S3" in order:
            n = N[1]
            acc.save("doubao_speech", n * pricing.unit_price_cny("doubao_speech"))
        # S6：截图分析
        if "S6" in run and I and "S6" in hit_like:
            _hit_step(acc, "S6", [("deepseek", RATIOS["s6_text_per_image"][1] * I, TOKENS["s6_text"]),
                                  ("ark", RATIOS["s6_vision_per_image"][1] * I, TOKENS["s6_vision"])])
        elif "S6" in run and I:
            lo, hi = RATIOS["s6_text_per_image"]
            _calls(acc, "S6", "deepseek", lo * I, hi * I, TOKENS["s6_text"], f"导演链/匹配/标签/停留（{I} 张截图）")
            lo, hi = RATIOS["s6_vision_per_image"]
            _calls(acc, "S6", "ark", lo * I, hi * I, TOKENS["s6_vision"], "导演链识图")
            if not fresh and (root / "06-编排" / "cache").is_dir():
                acc.by_step["S6"]["low_cny"] = 0.0
                for s in ("deepseek", "ark"):
                    acc.by_service[s][0] = max(0.0, acc.by_service[s][0]
                                               - sum(it["low_cny"] for it in acc.by_step["S6"]["items"]
                                                     if it["service"] == s))
                acc.by_step["S6"]["note"] = "已有缓存，可能全部命中：低值记 0"
        # S7：编排
        if "S7" in run and "S7" in hit_like:
            a_hi = RATIOS["s7_annot_per_image"][1] if ct == spec.CONTENT_TYPE_SCREENSHOT else 0
            _hit_step(acc, "S7", [("deepseek", RATIOS["s7_per_group"][1] * G[1] + a_hi * I, TOKENS["s7"])])
        elif "S7" in run:
            lo, hi = RATIOS["s7_per_group"]
            a_lo, a_hi = (RATIOS["s7_annot_per_image"] if ct == spec.CONTENT_TYPE_SCREENSHOT
                          else (0, 0))
            _calls(acc, "S7", "deepseek", lo * G[0] + a_lo * I, hi * G[1] + a_hi * I, TOKENS["s7"],
                   f"插画决策（{_fmt(G[0])}～{_fmt(G[1])} 组）"
                   + ("与逐句标注" if ct == spec.CONTENT_TYPE_SCREENSHOT else ""))
        # S8：出图
        up = pricing.unit_price_cny(_price_key(IMAGE_BUCKET))
        known = None if fresh else _illus_remaining(root)
        if known is None and "S8" in run and "S8" in hit_like:
            prev, cached = _prev_images_cached(root)
            left = prev - cached
            acc.add("S8", "runninghub", left * up, left * up,
                    f"清理前出过 {prev} 张图，{cached} 张还在出图缓存里；口播、配音能原样回放，"
                    f"出图需求预计与上次相同：只算缓存里没有的 {left} 张")
            acc.save("runninghub", cached * up)
        elif "S8" in run or (known and known[1]):
            if known is not None and "S7" not in run:
                total, left = known
                acc.add("S8", "runninghub", left * up, left * up,
                        f"出图需求 {total} 张，还没出、也没缓存的 {left} 张")
                acc.save("runninghub", (total - left) * up)
            else:
                k = "illus_per_group_shot" if ct == spec.CONTENT_TYPE_SCREENSHOT else "illus_per_group_theory"
                lo, hi = RATIOS[k]
                n_lo, n_hi = lo * G[0], hi * G[1]
                acc.add("S8", "runninghub", n_lo * up, n_hi * up,
                        f"出图 {_fmt(n_lo)}～{_fmt(n_hi)} 张（{_fmt(G[0])}～{_fmt(G[1])} 组）")
        elif known is not None:
            acc.save("runninghub", known[0] * up)
        for s in maybe:
            d = acc.by_step.get(s)
            if not d:
                continue
            for it in d["items"]:
                acc.by_service[it["service"]][0] -= it["low_cny"]
                it["low_cny"] = 0.0
            d["low_cny"] = 0.0
            d["note"] = "上游这一轮要重跑，可能跟着重跑：低值记 0"
        results.append((ct, acc, sorted(skipped_paid)))

    by_service = {}
    for s in SERVICES:
        lo = min(a.by_service[s][0] for _, a, _ in results)
        hi = max(a.by_service[s][1] for _, a, _ in results)
        by_service[s] = {"service_name": pricing.entry(_price_key(s))["service"],
                         "low_cny": round(lo, 2), "high_cny": round(hi, 2),
                         "price_estimated": bool(pricing.entry(_price_key(s)).get("estimated"))}
    totals = [(sum(v[0] for v in a.by_service.values()), sum(v[1] for v in a.by_service.values()))
              for _, a, _ in results]
    saved = max(sum(a.saved.values()) for _, a, _ in results)
    by_type = {ct: {"low_cny": round(lo, 2), "high_cny": round(hi, 2),
                    "steps": {k: dict(v, low_cny=round(v["low_cny"], 4), high_cny=round(v["high_cny"], 4))
                              for k, v in a.by_step.items()},
                    "paid_steps_skipped": sk}
               for (ct, a, sk), (lo, hi) in zip(results, totals)}
    return {
        "low_cny": round(min(t[0] for t in totals), 2),
        "high_cny": round(max(t[1] for t in totals), 2),
        "by_service": by_service,
        "by_type": by_type,
        "saved_by_cache_cny": round(saved, 2),
        "basis": {
            "chars": C, "images": I,
            "content_type": ct_preview, "content_type_basis": basis,
            "narration_chars": [int(N[0]), int(N[1])], "narration_exact": exact_n is not None,
            "groups": [int(G[0]), int(G[1])], "groups_exact": exact_g is not None,
            "illustrations": _illus_range(results, G),
            "fresh": bool(fresh),
        },
        "prices": {s: pricing.describe(_price_key(s)) for s in SERVICES},
    }


def _illus_range(results, G):
    out = []
    for ct, acc, _ in results:
        s8 = acc.by_step.get("S8")
        if not s8:
            continue
        up = pricing.unit_price_cny(_price_key(IMAGE_BUCKET))
        out.append((s8["low_cny"] / up, s8["high_cny"] / up))
    if not out:
        return [0, 0]
    return [int(min(a for a, _ in out)), int(round(max(b for _, b in out)))]
