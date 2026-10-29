# -*- coding: utf-8 -*-
"""pipeline 调度层 · 画面时间分配器（ST3-F2）。纯函数，无 IO。

全系统只有这一处决定「哪个主视觉占哪段时间」。三件事：

1. 定锚（resolve_anchors）：每个画面项「讲到它的那句话」的开始帧。
   口播句（matches_v2）→ 原文位置对应的句子（前文与口播句相似度）→ 按原文顺序等比分布。
2. 分配（allocate）：在每个章节内一次切分。相邻两项的分界点 = 后一项的锚点；
   正向扫保证最短时长（向后借），越出章节末尾再反向扫（向前借）；章节装不下才按比例压缩。
   结果天然无空白、无重叠，不需要事后修。
3. 断言（check_baselines）：五条底线逐条核对，返回违反清单。
   回归测试里有违反就失败；正式运行照常出片，违反项进检查报告标红、退出码 1（ST3-P9）。

画面项（item）字段：
  id       唯一标识
  kind     title / shot / grid / original / element / illustration
  anchor   期望出现帧
  min      最短时长（帧）
  must     必须出现（False = 可选项：插画、概述网格，排不下就让给相邻画面）
  order    构建顺序（锚点相同时按它排）
  track    是否纳入底线 5（画面跟着口播走）
  alt      可选 {"key": 图组, "role": "detail" | "overview"}——章节装不下时，
           该图组改用网格：role=detail 的逐张项让位，role=overview 的网格转为必须出现
           （锚点换成 alt_anchor）。同一图组的概述网格要么全上、要么全不上。
  hold     可选，最多占多久（帧）。超过后由下一项提前补位——标题卡只占自己那一段，不拖着等图。
           ST3-P12 第 7 步：补位的画面最多比它的锚点句早 EARLY_MAX 帧（6 秒）。标题卡后面的空档
           不超过 6 秒：下一个画面直接提前出现来填，标题卡不延长；超过 6 秒：保持 ST3-P11 R6 的做法——
           下一个画面最多早 EARLY_NEAR 帧（2 秒），空出来的时间超过 GAP_FILL_MAX 帧且有空档插画
           就用空档插画，否则标题卡延长。
  fill_for 可选，空档插画专用：它只用来填哪个标题卡（id）后面的空档，不参加普通排位。

ST3-P11 R7：锚点相同的几张图（截图 / 原图），在「这句话开始」到「下一个不同锚点」之间平分时间。
ST3-P12 第 8 步：网格至少停 4 秒，不够时向后面逐张展示的截图借（正向扫），借完每张截图仍 ≥ 2 秒；
借不够按比例分（登记「时间不足」），后面的截图因此晚于锚点句超过 2 秒的登记「超时」。
"""
import bisect
import difflib
import re

FPS = 30
MIN_FRAMES = {"title": 60, "shot": 60, "grid": 120, "original": 60,
              "element": 90, "illustration": 60}
LATE_TOL = 60                    # 底线 5：锚点句开始后 2 秒内出现
EARLY_MAX = 180                  # ST3-P12 第 7 步：画面最多比锚点句早 6 秒出现
EARLY_NEAR = 60                  # 标题卡后面的空档超过 6 秒时，下一个画面最多早 2 秒（R6 原做法）
GAP_FILL_MAX = 180               # R6：空出来的时间超过 6 秒且有空档插画才用空档插画，否则标题卡延长
SHARE_KINDS = ("shot", "original")   # R7：同一句话对应多张图时参与平分的画面
EL_TYPES = ("code_card", "terminal", "table")
SENT_END = "。！？!?\n"
MATCH_MIN_RATIO = 0.5            # 原文前文 vs 口播句的相似度下限
# R101 C：原文位置对不上（< MATCH_MIN_RATIO）时，在这张图所属小节的口播范围里找最像的一句；
# 相似度不低于它就放在那一句，否则在小节范围内按原文顺序等比分布。
# 提前 / 推后核对的「实际对应句」也用它：全片最像的一句相似度不低于它才拿来比，否则退回和锚点比。
NEAR_MIN_RATIO = 0.35
LATE_MAX = EARLY_MAX             # R101 C：推后出现核对，与提前出现对称（6 秒）


def norm(s):
    return re.sub(r"[^\w]", "", s or "").lower()


# --------------------------------------------------------------------------
# 定锚
# --------------------------------------------------------------------------

def sentence_index(char_rows, fps=FPS):
    """口播逐字时间 → 句子索引。

    char_rows: [(group_id, [{c, startTime, endTime}, ...]), ...]，按口播顺序。
    返回 {"text": 归一化全文, "sent_of": [每个归一化字符所属句号], "char_frame": [每个归一化字符的帧],
          "sentences": [{"i","frame","end_frame","gid","norm"}]}。
    """
    text, sent_of, sentences, char_frame = [], [], [], []
    cur, last_f = None, 0

    def _close():
        nonlocal cur
        if cur is not None and cur["norm"]:
            char_frame.extend(cur.pop("frames"))
            cur["norm"] = "".join(cur["norm"])
            cur["i"] = len(sentences)
            sent_of.extend([cur["i"]] * len(cur["norm"]))
            text.append(cur["norm"])
            sentences.append(cur)
        cur = None

    for gid, char_times in char_rows:
        for ct in char_times:
            st = ct.get("startTime")
            if st is not None:
                last_f = int(round(st * fps))
            et = ct.get("endTime")
            c = ct.get("c", "")
            if cur is None:
                cur = {"frame": last_f, "end_frame": last_f, "gid": gid, "norm": [], "frames": []}
            n = norm(c)
            if n:
                if not cur["norm"]:
                    cur["frame"] = last_f
                cur["norm"].append(n)
                cur["frames"].extend([last_f] * len(n))
            cur["end_frame"] = int(round(et * fps)) if et is not None else last_f
            if c and c in SENT_END:
                _close()
        _close()
    return {"text": "".join(text), "sent_of": sent_of, "sentences": sentences, "char_frame": char_frame}


def _find_all(text, sub):
    out, pos = [], text.find(sub) if sub else -1
    while pos >= 0:
        out.append(pos)
        pos = text.find(sub, pos + 1)
    return out


def _sentence_at(sidx, frame):
    """frame 所在句号（最后一个开始帧 ≤ frame 的句子）。"""
    starts = [s["frame"] for s in sidx["sentences"]]
    return max(0, bisect.bisect_right(starts, frame) - 1)


def _locate(sidx, match, after_sent):
    """matches_v2 的一条匹配 → 句号。同一句话出现多次时，取不早于前一张图的那一次。"""
    for key in ("sentence", "anchor"):
        sub = norm((match or {}).get(key))
        if len(sub) < 2:
            continue
        cands = sorted({sidx["sent_of"][p] for p in _find_all(sidx["text"], sub)})
        if cands:
            later = [c for c in cands if c >= after_sent]
            return later[0] if later else cands[-1]
    return None


def _fuzzy(sidx, text, lo, hi):
    """一段原文（取其中各句）在口播句 [lo, hi] 里找最像的一句。返回 (句号, 相似度)。"""
    parts = []
    for p in re.split(r"[。！？!?；;\n]", text or ""):
        n = norm(p)
        if len(n) >= 4:
            parts.append(n)
    best = (None, 0.0)
    for s in sidx["sentences"][lo:hi + 1]:
        for p in parts:
            r = difflib.SequenceMatcher(None, p, s["norm"]).ratio()
            if r > best[1]:
                best = (s["i"], r)
    return best


def _sents_in(sidx, window):
    """帧范围 [a, b) 里开始的句子的句号范围 (lo, hi)；没有返回 None。"""
    idx = [s["i"] for s in sidx["sentences"] if window[0] <= s["frame"] < window[1]]
    return (idx[0], idx[-1]) if idx else None


def reference_sentence(sidx, texts, min_ratio=NEAR_MIN_RATIO):
    """R101 C：一张图在口播里「实际对应的句子」——原文前文 / 后文在全片口播里最像的一句。
    相似度不低于 min_ratio 才算数。返回 (帧, 相似度)；找不到返回 (None, 最高相似度)。"""
    sents = sidx["sentences"]
    best = (None, 0.0)
    for text in texts or []:
        si, r = _fuzzy(sidx, text, 0, len(sents) - 1)
        if si is not None and r > best[1]:
            best = (si, r)
    if best[0] is None or best[1] < min_ratio:
        return None, best[1]
    return sents[best[0]]["frame"], best[1]


def resolve_anchors(entries, sidx, pre=None, near_min=None):
    """给一批画面项定锚。

    entries: [{"key", "line", "match": matches_v2 条目或 None, "texts": [原文前文, ...],
               "fallback": 兜底帧, "window": (lo, hi) 所属段帧范围或 None}]
    pre: [{"key", "line", "frame"}] 已定锚的项（只当邻居用，不重算）。
    near_min: R101 C。给了时，原文位置对不上的项先在自己的 window（所属小节的口播范围）里找最像的一句，
      相似度 ≥ near_min 就用它（basis=小节内最相近句）；等比分布也不越出 window。不给时与 1.0.0 相同。
    返回 {key: {"frame", "basis"}}，basis ∈ 口播句 / 原文位置 / 小节内最相近句 / 等比分布。
    """
    sents = sidx["sentences"]
    if not sents:
        return {e["key"]: {"frame": e.get("fallback") or 0, "basis": "等比分布"} for e in entries}
    rows = [dict(e, _pre=False) for e in entries]
    rows += [dict(p, _pre=True, sent=_sentence_at(sidx, p["frame"])) for p in (pre or [])]
    rows.sort(key=lambda r: (r["line"], 0 if r["_pre"] else 1))

    # ① 口播句
    prev = 0
    for r in rows:
        if r["_pre"]:
            prev = r["sent"]
            continue
        si = _locate(sidx, r.get("match"), prev) if r.get("match") else None
        if si is not None:
            r["sent"], r["frame"], r["basis"] = si, sents[si]["frame"], "口播句"
            prev = si

    # ② 原文位置对应的句子：只在前后已定锚邻居之间找
    def _neighbors(i):
        lo = next((rows[j]["sent"] for j in range(i - 1, -1, -1) if "sent" in rows[j]), 0)
        hi = next((rows[j]["sent"] for j in range(i + 1, len(rows)) if "sent" in rows[j]),
                  len(sents) - 1)
        return (lo, hi) if lo <= hi else (0, len(sents) - 1)

    for i, r in enumerate(rows):
        if "sent" in r or not r.get("texts"):
            continue
        lo, hi = _neighbors(i)
        for text in r["texts"]:              # 先用图前面那段原文；对不上再用后面那段
            si, ratio = _fuzzy(sidx, text, lo, hi)
            if si is not None and ratio >= MATCH_MIN_RATIO:
                r["sent"], r["frame"], r["basis"] = si, sents[si]["frame"], "原文位置"
                break

    # ②' R101 C：对不上的项在所属小节的口播范围内找最像的一句（不再跨小节等比分布）
    if near_min is not None:
        for i, r in enumerate(rows):
            if "sent" in r or not r.get("texts") or not r.get("window"):
                continue
            rng = _sents_in(sidx, r["window"])
            if rng is None:
                continue
            lo, hi = _neighbors(i)
            lo, hi = (max(lo, rng[0]), min(hi, rng[1])) if max(lo, rng[0]) <= min(hi, rng[1]) else rng
            best = (None, 0.0)
            for text in r["texts"]:
                si, ratio = _fuzzy(sidx, text, lo, hi)
                if si is not None and ratio > best[1]:
                    best = (si, ratio)
            if best[0] is not None and best[1] >= near_min:
                r["sent"], r["frame"], r["basis"] = best[0], sents[best[0]]["frame"], "小节内最相近句"

    # ③ 等比分布：连续未定锚的项，在前后邻居的锚点之间按原文顺序等分（不越出所属段）
    i = 0
    while i < len(rows):
        if "frame" in rows[i]:
            i += 1
            continue
        j = i
        while j < len(rows) and "frame" not in rows[j]:
            j += 1
        m = j - i
        lo = rows[i - 1]["frame"] if i > 0 else None
        hi = rows[j]["frame"] if j < len(rows) else None
        for k in range(m):
            r = rows[i + k]
            a = lo if lo is not None else (r.get("fallback") or 0)
            b = hi if hi is not None else a + MIN_FRAMES["shot"] * (m + 1)
            if r.get("window"):
                a = min(max(a, r["window"][0]), r["window"][1])
                b = min(max(b, r["window"][0]), r["window"][1])
            b = max(a, b)
            r["frame"], r["basis"] = int(round(a + (b - a) * (k + 1) / (m + 1))), "等比分布"
        i = j
    return {r["key"]: {"frame": r["frame"], "basis": r["basis"]} for r in rows if not r["_pre"]}


# --------------------------------------------------------------------------
# 分配
# --------------------------------------------------------------------------

def _note(kind, it, **kw):
    return dict({"type": kind, "id": it["id"], "kind": it["kind"], "anchor": it["anchor"]}, **kw)


def allocate(items, bounds, total, tol=LATE_TOL):
    """章节内一次切分。

    items: 画面项列表（见模块说明）；bounds: 各章节起点帧；total: 内容总帧数。
    返回 (placed, notes)。placed 按时间排序、首尾相接铺满 [0, total)，每项带 start / end。
    notes 是登记清单：改用网格 / 借时长 / 超时 / 时间不足 / 未排入。
    """
    edges = sorted({0, total} | {b for b in bounds if 0 < b < total})
    n_sec = len(edges) - 1
    notes = []

    def _sec(frame):
        return min(n_sec - 1, max(0, bisect.bisect_right(edges, frame) - 1))

    items = [dict(it) for it in items]
    for it in items:
        it["anchor"] = min(max(0, int(it["anchor"])), max(0, total - 1))
        it["sec"] = _sec(it["anchor"])

    # 章节装不下全部逐张 → 该章节的多图图组改用网格（省得最多的先改，够了就停）
    for k in range(n_sec):
        length = edges[k + 1] - edges[k]
        while True:
            must = [it for it in items if it["must"] and it["sec"] == k]
            need = sum(it["min"] for it in must)
            if need <= length:
                break
            saving = {}
            for it in must:
                alt = it.get("alt") or {}
                if alt.get("role") == "detail":
                    saving[alt["key"]] = saving.get(alt["key"], 0) + it["min"]
            for it in items:
                alt = it.get("alt") or {}
                if alt.get("role") == "overview" and not it["must"] and alt["key"] in saving:
                    saving[alt["key"]] -= it["min"]
            saving = {key: v for key, v in saving.items() if v > 0}
            if not saving:
                break
            key = max(saving, key=lambda x: saving[x])
            kept = []
            for it in items:
                alt = it.get("alt") or {}
                if alt.get("key") == key and alt.get("role") == "detail" and it["sec"] == k:
                    notes.append(_note("改用网格", it, reason="本章节借完仍不够逐张展示"))
                    continue
                if alt.get("key") == key and alt.get("role") == "overview":
                    it["must"] = True
                    it["alt"] = None
                    it["anchor"] = min(max(0, int(it.get("alt_anchor", it["anchor"]))), max(0, total - 1))
                    it["sec"] = _sec(it["anchor"])
                kept.append(it)
            items = kept

    placed = []
    for k in range(n_sec):
        S, E = edges[k], edges[k + 1]
        # 锚点相同时标题卡在前：标题卡出现在它那一段开口处，这一段的图跟在它后面。
        # 反过来排的话，图只占最短时长，标题卡让位时还会把后面很远的画面提前拉过来（ST3-P9，59 篇开头）。
        must = sorted([it for it in items if it["must"] and it["sec"] == k],
                      key=lambda it: (it["anchor"], it["kind"] != "title", it["order"]))
        need = sum(it["min"] for it in must)
        if need > E - S:
            # 借完仍不够：按最短时长的比例分，全部照样出现
            acc = 0
            for it in must:
                it["start"] = S + (E - S) * acc // need
                acc += it["min"]
                it["squeezed"] = True
        else:
            cur = S
            for it in must:                      # 正向：前一项不足最短时长 → 分界点后推
                it["start"] = max(it["anchor"], cur)
                cur = it["start"] + it["min"]
            limit = E
            for it in reversed(must):            # 反向：推出章节末尾 → 往前借
                it["start"] = min(it["start"], limit - it["min"])
                limit = it["start"]
            # R7：锚点相同的几张图平分「这句话开始 → 下一个不同锚点」这段时间；
            # 平分后不够最短时长的，保持上面借时长的结果。
            lo = 0
            while lo < len(must):
                hi = lo
                while (hi + 1 < len(must) and must[lo]["kind"] in SHARE_KINDS
                       and must[hi + 1]["kind"] in SHARE_KINDS and must[hi + 1]["anchor"] == must[lo]["anchor"]):
                    hi += 1
                cnt = hi - lo + 1
                if cnt >= 2:
                    a = must[lo]["start"]
                    b = must[hi + 1]["start"] if hi + 1 < len(must) else E
                    # 「下一个不同锚点」也包括后面插画的锚点：不把本该给插画的时间分掉
                    later = [it["anchor"] for it in items
                             if not it["must"] and it["sec"] == k and not it.get("fill_for")
                             and it["anchor"] > must[lo]["anchor"]]
                    b = min([b] + later)
                    if (b - a) // cnt >= max(it["min"] for it in must[lo:hi + 1]):
                        for m in range(1, cnt):
                            must[lo + m]["start"] = a + (b - a) * m // cnt
                            must[lo + m]["shared"] = True
                lo = hi + 1
        # 可选项：只在不挤占必须项的前提下排入（必须项先保住自己的最短时长）
        opts = sorted([it for it in items if not it["must"] and it["sec"] == k and not it.get("fill_for")],
                      key=lambda it: (it["anchor"], it["order"]))
        starts = [it["start"] for it in must]
        cursor = {}
        got = []
        for it in opts:
            j = bisect.bisect_right(starts, it["anchor"]) - 1
            lower = cursor.get(j, (must[j]["start"] + must[j]["min"]) if j >= 0 else S)
            upper = must[j + 1]["start"] if j + 1 < len(must) else E
            st = max(it["anchor"], lower)
            if upper - st >= it["min"]:
                it["start"] = st
                cursor[j] = st + it["min"]
                got.append(it)
            else:
                notes.append(_note("未排入", it, reason="可选画面排不下，时间让给相邻画面"))
        # 概述网格要么全上、要么全不上（只上一屏会漏掉另一屏的图）
        lost = {n["id"] for n in notes if n["type"] == "未排入"}
        partial = {(it.get("alt") or {}).get("key") for it in opts
                   if it["id"] in lost and (it.get("alt") or {}).get("role") == "overview"}
        for it in got:
            if (it.get("alt") or {}).get("role") == "overview" and it["alt"]["key"] in partial:
                notes.append(_note("未排入", it, reason="同图组另一屏概述排不下，整组概述不上"))
            else:
                placed.append(it)
        placed.extend(must)

    placed.sort(key=lambda it: (it["start"], it["order"]))
    if placed:
        placed[0]["start"] = 0
    # 占满自己那一段就让位：下一项提前补位。被跟踪的画面（截图、原图、要素、网格）提前的上限：
    # 空档（标题卡让位处 → 下一个画面的锚点句）不超过 EARLY_MAX 帧 → 直接提前来填，标题卡不延长；
    # 超过 → 最多早 EARLY_NEAR 帧，空出来的时间超过 GAP_FILL_MAX 帧且有空档插画就用空档插画，否则标题卡延长。
    fillers = {}
    for it in items:
        if it.get("fill_for"):
            fillers.setdefault(it["fill_for"], it)
    out = []
    for i, p in enumerate(placed):
        out.append(p)
        q = placed[i + 1] if i + 1 < len(placed) else None
        if q is None or p.get("hold") is None:
            continue
        limit = p["start"] + max(p["hold"], p["min"])
        if q["start"] <= limit:
            continue
        target = limit
        if q.get("track") and q["anchor"] - limit > EARLY_MAX:
            target = max(limit, min(q["start"], q["anchor"] - EARLY_NEAR))
        gap = target - limit
        fill = fillers.get(p["id"])
        if gap > GAP_FILL_MAX and fill is not None:
            fillers.pop(p["id"])
            fill["start"] = limit
            out.append(fill)
        elif gap > 0:
            notes.append(_note("标题卡延长", p, start=p["start"], frames=gap, gap_from=limit, gap_to=target,
                               next=q["id"], long=gap > GAP_FILL_MAX,
                               reason=("空档超过 6 秒，后一个画面最多提前 2 秒；没有空档插画，标题卡延长"
                                       if gap > GAP_FILL_MAX
                                       else "空档超过 6 秒，后一个画面最多提前 2 秒，余下的由标题卡延长")))
        if target < q["start"]:
            q["start"] = target
            q["pulled"] = True
    for fill in fillers.values():
        notes.append(_note("未排入", fill, reason="空档已不足 6 秒，空档插画不用"))
    placed = out
    for i, it in enumerate(placed):
        it["end"] = placed[i + 1]["start"] if i + 1 < len(placed) else total
        if not it["must"]:
            continue
        delta = it["start"] - it["anchor"]
        dur = it["end"] - it["start"]
        if dur < it["min"]:
            notes.append(_note("时间不足", it, start=it["start"], frames=dur, min=it["min"],
                               reason="本章节借完仍不够最短时长，照样出现"))
        if delta > tol and it.get("track"):
            notes.append(_note("超时", it, start=it["start"], late=delta, shared=bool(it.get("shared")),
                               reason=("同一句话对应多张图，平分时间" if it.get("shared")
                                       else "同章节内借时长，晚于锚点句超过 2 秒")))
        elif delta != 0 and i > 0:
            notes.append(_note("提前补位" if it.get("pulled") else "借时长", it,
                               start=it["start"], delta=delta))
    return placed, notes


# --------------------------------------------------------------------------
# 断言
# --------------------------------------------------------------------------

def _runs(flags):
    out, st = [], None
    for i, v in enumerate(flags):
        if v and st is None:
            st = i
        elif not v and st is not None:
            out.append((st, i))
            st = None
    if st is not None:
        out.append((st, len(flags)))
    return out


def scene_images(sc):
    """一个场景里出现的原图文件名（单张 + 网格子图）。"""
    fns = set()
    if sc.get("image") and (sc.get("is_original") or sc.get("shot")):
        fns.add(str(sc["image"]).replace("\\", "/").split("/")[-1])
    for g in ((sc.get("shot") or {}).get("grid") or {}).get("display") or []:
        fns.add(g["image"])
    return fns


def check_baselines(data, required_images=(), required_elements=(), notes=None, tol=LATE_TOL):
    """五条底线逐条核对。返回 {"1": [...], ..., "5": [...]}，每条是违反清单（空 = 通过）。

    notes: 分配登记清单；已登记的「时间不足」「超时」不算违反。
    """
    scenes = data["scenes"]
    content = data["content_frames"]
    notes = data.get("allocation_notes", []) if notes is None else notes
    cnt = [0] * content
    for sc in scenes:
        for f in range(max(0, sc["start_frame"]), min(content, sc["end_frame"])):
            cnt[f] += 1
    v = {"1": [{"start_frame": a, "end_frame": b} for a, b in _runs([c == 0 for c in cnt])],
         "2": [{"start_frame": a, "end_frame": b} for a, b in _runs([c >= 2 for c in cnt])],
         "3": [], "4": [], "5": []}
    v["2"] += [{"scene": sc.get("alloc_id"), "end_frame": sc["end_frame"], "reason": "越过音频结尾"}
               for sc in scenes if sc["end_frame"] > content]

    seen = set()
    for sc in scenes:
        if sc["end_frame"] > sc["start_frame"]:
            seen |= scene_images(sc)
            if sc.get("element_id"):
                seen.add(sc["element_id"])
    v["3"] = [x for x in list(required_images) + list(required_elements) if x not in seen]

    short_ok = {n["id"] for n in notes if n["type"] == "时间不足"}
    late_ok = {n["id"] for n in notes if n["type"] == "超时"}
    for sc in scenes:
        dur = sc["end_frame"] - sc["start_frame"]
        if sc.get("type") in EL_TYPES:
            need = MIN_FRAMES["element"]
        elif sc.get("is_original"):
            need = MIN_FRAMES["shot"]
        else:
            need = 0
        aid = sc.get("alloc_id")
        if dur < need and aid not in short_ok:
            v["4"].append({"scene": aid, "frames": dur, "min": need})
        anchor = sc.get("anchor_frame")
        if need and anchor is not None and sc["start_frame"] - anchor > tol and aid not in late_ok:
            v["5"].append({"scene": aid, "anchor": anchor, "start_frame": sc["start_frame"]})
    return v


BASELINE_NAMES = {"1": "空白", "2": "重叠", "3": "原图/要素缺失", "4": "不足最短时长", "5": "晚于口播"}


def _mmss(frame, fps=FPS):
    sec = int(frame // fps)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def describe_violations(baselines, fps=FPS):
    """把底线违反清单写成给人看的条目，每项 {"baseline", "tag", "text"}。

    tag 是短标识（进账本的缺失清单），text 是完整的一句话（进检查报告的标红区）。
    检查报告、登记清单、退出码都只读这里，不另算一遍。
    """
    out = []

    def add(k, tag, detail):
        out.append({"baseline": k, "tag": tag, "text": f"底线{k}（{BASELINE_NAMES[k]}）：{detail}"})

    for k in ("1", "2"):
        for x in baselines.get(k) or []:
            if "start_frame" in x:
                a, b = x["start_frame"], x["end_frame"]
                add(k, f"{'空白帧' if k == '1' else '重叠帧'} {a}-{b - 1}",
                    f"{_mmss(a, fps)}～{_mmss(b, fps)}（第 {a}～{b - 1} 帧，共 {b - a} 帧）")
            else:
                add(k, f"越过音频结尾 {x.get('scene')}", f"{x.get('scene')} {x.get('reason', '')}")
    for x in baselines.get("3") or []:
        add("3", x, f"{x} 没有出现")
    for x in baselines.get("4") or []:
        add("4", f"时长不足 {x['scene']}",
            f"{x['scene']} 只有 {x['frames'] / fps:.1f} 秒（最短 {x['min'] / fps:.0f} 秒）")
    for x in baselines.get("5") or []:
        add("5", f"晚于口播 {x['scene']}",
            f"{x['scene']} 比讲到它的那句话（{_mmss(x['anchor'], fps)}）晚 "
            f"{(x['start_frame'] - x['anchor']) / fps:.1f} 秒")
    return out


def assert_baselines(data, required_images=(), required_elements=(), notes=None):
    """回归测试用：不满足就抛错。正式运行不走这里（底线不过照常出片，退出码 1、检查报告标红）。"""
    v = check_baselines(data, required_images, required_elements, notes)
    bad = {k: x for k, x in v.items() if x}
    if bad:
        raise SystemExit("ERROR: 画面时间分配未过底线：" + "；".join(
            f"底线{k}（{BASELINE_NAMES[k]}）{len(x)} 处 {x[:3]}" for k, x in bad.items()))
    return v


def _ref_of(sc):
    """R101 C：核对用的「实际对应句」帧——有 ref_frame（场景构建按原文全片找到的最像一句）就用它，
    没有就用锚点句（老数据、或全片都找不到像样的一句）。"""
    r = sc.get("ref_frame")
    return (r, "实际对应句") if r is not None else (sc.get("anchor_frame"), "锚点句")


def early_items(data, limit=EARLY_MAX):
    """提前出现核对：比对应口播句早出现超过 limit 帧（默认 6 秒）的画面（截图、原图、要素、网格）。空 = 没有。

    R101 C：对应句优先用实际对应句（ref_frame），不再只和自己的锚点比——锚点是兜底估出来的时，
    画面和锚点一致，但和真正讲到它的那句差很远。"""
    out = []
    for sc in data.get("scenes", []):
        a, basis = _ref_of(sc)
        if a is not None and a - sc["start_frame"] > limit:
            out.append({"scene": sc.get("alloc_id"), "anchor": a, "start_frame": sc["start_frame"],
                        "early": a - sc["start_frame"], "basis": basis})
    return out


def late_items(data, limit=LATE_MAX):
    """R101 C：推后出现核对（与提前出现对称）：比对应口播句晚出现超过 limit 帧（默认 6 秒）的画面。"""
    out = []
    for sc in data.get("scenes", []):
        a, basis = _ref_of(sc)
        if a is not None and sc["start_frame"] - a > limit:
            out.append({"scene": sc.get("alloc_id"), "anchor": a, "start_frame": sc["start_frame"],
                        "late": sc["start_frame"] - a, "basis": basis})
    return out


def group_sentences(text):
    """把一段口播按句切开（与 sentence_index 同一套句末标点），返回带标点的句子列表。"""
    out, cur = [], []
    for ch in text or "":
        cur.append(ch)
        if ch in SENT_END:
            if norm("".join(cur)):
                out.append("".join(cur))
            cur = []
    if norm("".join(cur)):
        out.append("".join(cur))
    return out


def first_mention(sidx, word, from_frame=0):
    """一个词在口播里从 from_frame 起第一次讲到的帧（精确到字）；没讲到返回 None。"""
    w = norm(word)
    if len(w) < 2:
        return None
    for pos in _find_all(sidx["text"], w):
        if sidx["char_frame"][pos] >= from_frame:
            return sidx["char_frame"][pos]
    return None
