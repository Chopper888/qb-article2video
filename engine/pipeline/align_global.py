# -*- coding: utf-8 -*-
"""ST3-P28：整篇有序对齐（S4 组级时间 + 字幕字级时间只算一次、两边共用）。

旧做法（word_align + R148）是"每段到全文里找最长匹配、指针只进不退"：附近有重复词
（Hermes、xxsj-fs-ob）时会跳到后文，而且错了回不来。本模块改成**整篇一次性有序对齐**：

1. 两边（口播全文按组顺序 / 配音返回的全部字按时间顺序）先做同一套规范化；
2. 用带状动态规划做全局序列对齐（Needleman-Wunsch），路径只能单调向前，不能跳后文；
3. 读法不同的局部（数字、版本号、网址、大小写、标点）在规范化或 DP 里成为局部不匹配，
   不影响后文；
4. 对齐结果同时给出：每组的起止时间（吸附到配音字边界）和每个字幕字的起止时间。

规范化规则集中在本文件 `normalize_char` / `normalize`，别处不再各写一套。
"""

import hashlib
import json
from pathlib import Path

# 阿拉伯数字 -> 中文数字（逐位；版本号/年份这类念法一致）
_CN_DIGITS = str.maketrans("0123456789", "零一二三四五六七八九")
# 数字之间的点、斜杠、连字符：统一替换成实际读出的中文写法
_SPOKEN_SYMBOLS = {
    ".": "点", "．": "点",
    "/": "斜杠", "／": "斜杠",
    "-": "杠", "－": "杠", "—": "杠", "–": "杠",
}

# DP 打分：命中 +2、替换 -1、空位 -1。并列时固定优先级（对角 > 上 > 左），保证确定性。
_MATCH = 2
_MISMATCH = -1
_GAP = -1
_NEG = -10 ** 9
# 豆包把网址里的符号念成英文单词（slash/colon/dot）；整词当"读法不同"的局部噪声，
# 不让里面的字母和口播的普通字母互相匹配（否则 "s" 会配到 "slash" 里）。
_OPAQUE = "\x00"
_OPAQUE_WORDS = frozenset({
    "slash", "dot", "colon", "comma", "period", "semicolon", "hyphen",
    "dash", "underscore", "backslash", "asterisk", "tilde",
})


def normalize_char(ch):
    """单个字符 -> 规范化后的 0~2 个字符（统一大小写、数字、点/斜杠/连字符写法）。

    标点、空白、无法朗读的符号返回空串。
    """
    if not ch:
        return ""
    if ch in _SPOKEN_SYMBOLS:
        return _SPOKEN_SYMBOLS[ch]
    if "0" <= ch <= "9":
        return ch.translate(_CN_DIGITS)
    if "\uff10" <= ch <= "\uff19":          # 全角数字
        return chr(ord(ch) - 0xFF10 + ord("0")).translate(_CN_DIGITS)
    low = ch.lower()
    if ("a" <= low <= "z") or ("\u4e00" <= low <= "\u9fff"):
        return low
    return ""


def normalize(text):
    """整段文本规范化（只用于比对；不保留下标映射）。"""
    return "".join(normalize_char(ch) for ch in (text or ""))


def build_narration(groups):
    """口播全文（按组顺序）-> 规范化字符序列 + 每个字符的归属。

    返回 dict：
      - chars: 规范化字符串
      - owner: [ (组下标, 显示字符下标), ... ]，与 chars 等长
      - group_ranges: [ (起, 止), ... ]，每个组的规范化字符区间
      - char_spans: [ [ (首个规范化下标, 末个规范化下标 或 None), ... ], ... ]，按组、按显示字符
    """
    chars = []
    owner = []
    group_ranges = []
    char_spans = []
    for gi, g in enumerate(groups):
        start = len(chars)
        spans = []
        for ci, ch in enumerate(g.get("text") or ""):
            first = len(chars)
            norm = normalize_char(ch)
            for nc in norm:
                chars.append(nc)
                owner.append((gi, ci))
            last = len(chars) - 1 if norm else None
            spans.append((first, last))
        char_spans.append(spans)
        group_ranges.append((start, len(chars)))
    return {"chars": "".join(chars), "owner": owner,
            "group_ranges": group_ranges, "char_spans": char_spans}


def build_spoken(sentences):
    """配音返回的全部字（按时间顺序）-> 规范化字符序列 + 每字 (startTime, endTime)。"""
    chars = []
    times = []
    for s in sentences or []:
        for w in s.get("words") or []:
            st, et = w.get("startTime"), w.get("endTime")
            word = (w.get("word") or "").strip().lower()
            if word in _OPAQUE_WORDS:
                chars.append(_OPAQUE)
                times.append((st, et))
                continue
            for ch in w.get("word") or "":
                for nc in normalize_char(ch):
                    chars.append(nc)
                    times.append((st, et))
    return "".join(chars), times


def _banded(a, b, width):
    """带状全局对齐。返回 a 每个下标对应的 b 下标（None = 空位）；路径出带返回 None。"""
    n, m = len(a), len(b)
    loos = [0] * (n + 1)
    his = [0] * (n + 1)
    for i in range(n + 1):
        center = (i * m) // n if n else 0
        loos[i] = max(0, center - width)
        his[i] = min(m, center + width)
    rows = [None] * (n + 1)

    lo0, hi0 = loos[0], his[0]
    row0 = [0] * (hi0 - lo0 + 1)
    ops0 = bytearray(hi0 - lo0 + 1)
    for j in range(lo0, hi0 + 1):
        row0[j - lo0] = -j
        ops0[j - lo0] = 2 if j > 0 else 3
    rows[0] = ops0
    prev, prev_lo, prev_hi = row0, lo0, hi0

    for i in range(1, n + 1):
        lo, hi = loos[i], his[i]
        cur = [_NEG] * (hi - lo + 1)
        ops = bytearray(hi - lo + 1)
        ai = a[i - 1]
        for j in range(lo, hi + 1):
            best = _NEG
            op = 0
            if prev_lo <= j - 1 <= prev_hi:
                bj = b[j - 1]
                if ai == bj:
                    score = _MATCH
                elif bj == _OPAQUE:
                    score = -3          # 局部噪声：宁可用空位跳过，也不拿普通字去配它
                else:
                    score = _MISMATCH
                best = prev[j - 1 - prev_lo] + score
                op = 0
            if prev_lo <= j <= prev_hi:
                v = prev[j - prev_lo] + _GAP
                if v > best:
                    best, op = v, 1
            if j - 1 >= lo:
                v = cur[j - 1 - lo] + _GAP
                if v > best:
                    best, op = v, 2
            cur[j - lo] = best
            ops[j - lo] = op
        rows[i] = ops
        prev, prev_lo, prev_hi = cur, lo, hi

    end_lo, end_hi = loos[n], his[n]
    if not (end_lo <= m <= end_hi):
        return None
    a_to_b = [None] * n
    i, j = n, m
    max_dev = 0.0
    while i > 0 or j > 0:
        if i == 0:
            j -= 1
            continue
        lo, hi = loos[i], his[i]
        if not (lo <= j <= hi):
            return None
        op = rows[i][j - lo]
        if op == 0:
            a_to_b[i - 1] = j - 1
            i -= 1
            j -= 1
        elif op == 1:
            i -= 1
        elif op == 2:
            j -= 1
        else:                       # 只会出现在 (0,0)
            if i == 0 and j == 0:
                break
            return None
        if i:
            dev = abs(j - (i * m) / n)
            if dev > max_dev:
                max_dev = dev
    if max_dev > width * 0.8:
        return None                 # 路径贴着带边，可能被截断：加宽带重算
    return a_to_b


def align_ordered(a, b, width=None):
    """把规范化的口播串 a 有序对到配音串 b。返回 a_to_b（None = 局部空位）。

    带状 DP，先窄后宽；20k 级别的口播足够快，结果确定（同输入逐字节同输出）。
    """
    n, m = len(a), len(b)
    if not n:
        return []
    if not m:
        return [None] * n
    if width is None:
        width = max(96, min(512, abs(n - m) + 96))
    # 带宽至少要能覆盖「整条串」：短文本（几十字）初值 96 可能大于 2*limit，
    # 不能因此一次都不试。
    max_width = max(512, max(n, m) * 2)
    while width <= max_width:
        out = _banded(a, b, width)
        if out is not None:
            return out
        width *= 2
    return [None] * n


def _prev_next_times(a_to_b, times):
    """prev[k] = 严格在 k 之前的最近有时间下标的时间；next[k] = 从 k 起最近的（含 k）。

    长度都是 len(a_to_b)+1；+1 用于"标点插在最后一个字之后"的位置。
    """
    n = len(a_to_b)
    prev = [None] * (n + 1)
    for k in range(1, n + 1):
        prev[k] = times[a_to_b[k - 1]] if a_to_b[k - 1] is not None else prev[k - 1]
    nxt = [None] * (n + 1)
    for k in range(n - 1, -1, -1):
        nxt[k] = times[a_to_b[k]] if a_to_b[k] is not None else nxt[k + 1]
    return prev, nxt


def _char_time(span, prev, nxt, a_to_b, times):
    first, last = span
    if last is not None:
        st = et = None
        for k in range(first, last + 1):
            if a_to_b[k] is not None:
                t = times[a_to_b[k]]
                if st is None:
                    st = t[0]
                et = t[1]
        if st is not None:
            return st, et
        # 该字规范化后全部是局部空位：向前挂靠，其次向后
        t = prev[first] if first > 0 else None
        return t if t is not None else (nxt[min(last + 1, len(nxt) - 1)] or (None, None))
    # 标点/空白本身不朗读：挂到前一个字（与旧 R148 口径一致），没有前字才挂后字
    t = prev[first] if first > 0 else None
    if t is None:
        t = nxt[first]
    return t if t is not None else (None, None)


def align_all(sentences, groups):
    """一次算出：组级时间 + 每个显示字的字级时间。

    返回 {"groups": [word_align 同结构], "chars": [R148 同结构], "chars_norm": [...], "spoken_norm": [...]}
    """
    nar = build_narration(groups)
    spoken, times = build_spoken(sentences)
    a_to_b = align_ordered(nar["chars"], spoken)
    prev, nxt = _prev_next_times(a_to_b, times)

    out_groups = []
    out_chars = []
    for gi, g in enumerate(groups):
        gid = g.get("group_id", "?")
        text = g.get("text") or ""
        gs, ge = nar["group_ranges"][gi]
        mapped = [(k, a_to_b[k]) for k in range(gs, ge) if a_to_b[k] is not None]
        exact = sum(1 for k, j in mapped if nar["chars"][k] == spoken[j])
        length = ge - gs
        if mapped:
            start = times[mapped[0][1]][0]
            end = times[mapped[-1][1]][1]
        else:
            start = end = 0.0
        out_groups.append({
            "group_id": gid,
            "start_time": start,
            "end_time": end,
            "ratio": round(exact / length, 3) if length else 0,
            "matched_chars": len(mapped),
        })
        char_times = []
        for ch, span in zip(text, nar["char_spans"][gi]):
            st, et = _char_time(span, prev, nxt, a_to_b, times)
            char_times.append({"c": ch, "startTime": st, "endTime": et})
        out_chars.append({
            "group_id": gid,
            "text": text,
            "char_times": char_times,
            "start_time": start,
            "end_time": end,
        })
    return {"groups": out_groups, "chars": out_chars,
            "chars_norm": nar["chars"], "spoken_norm": spoken}


# --------------------------------------------------------------------------
# 3.2 新旧对照（方式甲）
# --------------------------------------------------------------------------

def _line_times(char_times, splitter):
    """按现有分行规则切出的每行 (起, 止) 秒；与 scene_build 的口径一致。"""
    lines = []
    for seg in splitter(char_times):
        if not seg:
            continue
        st = seg[0]["startTime"]
        et = seg[-1]["endTime"]
        lines.append((st if st is not None else 0.0, et if et is not None else 0.0))
    return lines


def _cjk(ch):
    return "\u4e00" <= ch <= "\u9fff"


def _line_points(char_times, splitter):
    """每行的 (实际起止, 稳定起止)。

    稳定起止取行内第一个/最后一个汉字的时间：旧 R148 遇到行首的英文/数字（大小写、
    读法不同）会挂到前一个字，和新方法最多差一个词；这属于 3.1.2 的"局部不匹配"，
    不算行时间漂移。行内没有汉字时退回实际起止。
    """
    out = []
    for seg in splitter(char_times):
        if not seg:
            continue
        actual = (seg[0]["startTime"] or 0.0, seg[-1]["endTime"] or 0.0)
        first = next((x for x in seg if _cjk(x.get("c") or "") and x.get("startTime") is not None), None)
        last = next((x for x in reversed(seg) if _cjk(x.get("c") or "") and x.get("endTime") is not None), None)
        stable = ((first["startTime"] if first else actual[0]),
                  (last["endTime"] if last else actual[1]))
        out.append((actual, stable))
    return out


def compare(old_groups, old_chars, new_groups, new_chars, groups, splitter):
    """新旧结果逐组、逐行比差值。返回最大差值、第一处超 0.5 秒、行数是否一致。"""
    old_g = {r["group_id"]: r for r in old_groups}
    new_g = {r["group_id"]: r for r in new_groups}
    old_c = {r["group_id"]: r for r in old_chars}
    new_c = {r["group_id"]: r for r in new_chars}

    max_diff = -1.0
    max_at = None
    first_over = None
    all_within = True
    lines_match = True

    def consider(diff, gid, line_no, old_pair, new_pair, text, kind):
        nonlocal max_diff, max_at, first_over, all_within
        if diff > max_diff:
            max_diff = diff
            max_at = {"group_id": gid, "line": line_no}
        if diff > 0.5 and all_within:
            all_within = False
            if first_over is None:
                first_over = {"kind": kind, "group_id": gid, "line": line_no,
                              "text": text,
                              "old": {"start": old_pair[0], "end": old_pair[1]},
                              "new": {"start": new_pair[0], "end": new_pair[1]}}

    for g in groups:
        gid = g["group_id"]
        text = g.get("text") or ""
        og, ng = old_g.get(gid), new_g.get(gid)
        if not og or not ng:
            all_within = False
            continue
        for key in ("start_time", "end_time"):
            a, b = og.get(key), ng.get(key)
            if a is None or b is None:
                all_within = False
                continue
            consider(abs(b - a), gid, None,
                     (og.get("start_time"), og.get("end_time")),
                     (ng.get("start_time"), ng.get("end_time")), text, f"组{key}")
        oc = (old_c.get(gid) or {}).get("char_times") or []
        nc = (new_c.get(gid) or {}).get("char_times") or []
        ol = _line_points(oc, splitter)
        nl = _line_points(nc, splitter)
        if len(ol) != len(nl):
            lines_match = False
            all_within = False
            continue
        for li, ((o_act, o_sta), (n_act, n_sta)) in enumerate(zip(ol, nl)):
            ds = min(abs(n_act[0] - o_act[0]), abs(n_sta[0] - o_sta[0]))
            de = min(abs(n_act[1] - o_act[1]), abs(n_sta[1] - o_sta[1]))
            consider(ds, gid, li + 1, o_act, n_act, text, "字幕行起点")
            consider(de, gid, li + 1, o_act, n_act, text, "字幕行终点")

    return {"max_diff_seconds": round(max(0.0, max_diff), 3),
            "max_at": max_at, "first_over_0_5": first_over,
            "all_within_0_5": all_within, "line_count_match": lines_match}


def self_check(groups, group_times, char_align, sentences, audio_dur, splitter=None):
    """3.3 对齐自查。返回 {"passed": bool, "failures": [ ... ]}。

    groups 为正文组（已排除标题组）。group_times / char_align 都是 dict 或 list。
    splitter 给定（scene_build._split_subtitle）时按真实分行检查行起点和 0/0 行。
    """
    if isinstance(group_times, list):
        group_times = {r["group_id"]: r for r in group_times}
    if isinstance(char_align, list):
        char_align = {r["group_id"]: r for r in char_align}
    failures = []

    def fail(item, gid=None, t=None, text="", detail=""):
        failures.append({"item": item, "group_id": gid, "time": t,
                         "text": text, "detail": detail})

    # 1a. 组起点单调不减
    prev = None
    prev_gid = None
    for g in groups:
        gid = g["group_id"]
        st = (group_times.get(gid) or {}).get("start_time")
        if st is None:
            fail("组起点为空", gid, None, g.get("text", ""))
            break
        if prev is not None and st < prev - 1e-6:
            fail("组起点不单调", gid, st, g.get("text", ""),
                 f"上一组 {prev_gid} 起点 {prev:.2f}s，本组 {st:.2f}s")
            break
        prev, prev_gid = st, gid

    # 1b. 字幕行起点单调不减 + 2b. 没有起止都为 0 的行 + 2a. 组时长 > 0
    prev_line = None
    prev_line_gid = None
    for g in groups:
        gid = g["group_id"]
        gt = group_times.get(gid) or {}
        st, en = gt.get("start_time"), gt.get("end_time")
        if st is not None and en is not None and en - st <= 0:
            fail("组时长不大于 0", gid, st, g.get("text", ""),
                 f"起 {st:.2f}s 止 {en:.2f}s")
            break
        ct = (char_align.get(gid) or {}).get("char_times") or []
        if splitter is not None:
            line_pairs = _line_times(ct, splitter)
        else:
            # 没有分行器时按句末标点近似（S4 生产路径总会传 scene_build._split_subtitle）
            line_pairs = []
            cur = None
            for item in ct:
                if cur is None and item.get("startTime") is not None:
                    cur = item["startTime"]
                if item.get("c") in "。！？；":
                    if cur is not None:
                        line_pairs.append((cur, item.get("endTime") or cur))
                    cur = None
            if cur is not None:
                line_pairs.append((cur, cur))
        for ls, le in line_pairs:
            if ls == 0 and le == 0:
                fail("字幕行起止都为 0", gid, 0, g.get("text", ""))
                return {"passed": False, "failures": failures}
            if prev_line is not None and ls < prev_line - 1e-6:
                fail("字幕行起点不单调", gid, ls, g.get("text", ""),
                     f"上一行在 {prev_line_gid} 起点 {prev_line:.2f}s，本行 {ls:.2f}s")
                return {"passed": False, "failures": failures}
            prev_line, prev_line_gid = ls, gid

    # 3. 最后一组结束时间与音频结束相差 ≤ 2 秒
    if groups and audio_dur:
        last = groups[-1]
        et = (group_times.get(last["group_id"]) or {}).get("end_time")
        if et is None or abs(audio_dur - et) > 2.0:
            fail("最后一组结束时间与配音结束相差超过 2 秒", last["group_id"], et,
                 last.get("text", ""), f"音频 {audio_dur:.2f}s，最后一组止 {et}" if et is not None
                 else f"音频 {audio_dur:.2f}s，最后一组没有结束时间")
        # 4. 每句配音都落在某个组的时间范围里（按句子起点判定）
        ranges = []
        for g in groups:
            gt = group_times.get(g["group_id"]) or {}
            s0, e0 = gt.get("start_time"), gt.get("end_time")
            if s0 is not None and e0 is not None and e0 > s0:
                ranges.append((s0, e0, g["group_id"], g.get("text", "")))
        ranges.sort()
        for s in sentences or []:
            st = s.get("startTime")
            if st is None:
                continue
            hit = next((r for r in ranges if r[0] - 0.5 <= st <= r[1] + 0.5), None)
            if hit is None:
                fail("配音句子不在任何组的时间范围内", None, st,
                     (s.get("text") or "")[:40],
                     f"配音句起点 {st:.2f}s")
                break
        # 1b/2b 的“行”检查交给上层用真实分行结果再做一次，这里只覆盖组级。

    return {"passed": not failures, "failures": failures}


def decide(old_groups, old_chars, new_groups, new_chars, groups, sentences,
           audio_dur, splitter):
    """3.2 判定：新旧都在 0.5 秒内且旧结果自查通过 → 沿用旧结果；否则整篇改用新结果。"""
    cmp_ = compare(old_groups, old_chars, new_groups, new_chars, groups, splitter)
    old_check = self_check(groups, old_groups, old_chars, sentences, audio_dur, splitter)
    use_old = (cmp_["all_within_0_5"] and cmp_["line_count_match"]
               and old_check["passed"])
    first = cmp_["first_over_0_5"]
    if use_old:
        reason = "新旧结果一致，沿用原结果"
    elif first:
        reason = f"原方法在 {first['group_id']} 起偏差 {abs(first['new']['start'] - first['old']['start']):.1f} 秒，已改用新方法"
    else:
        f0 = (old_check["failures"] or [{}])[0]
        reason = f"原方法自查未通过（{f0.get('item', '未知')}），已改用新方法"
    return {
        "method": "old" if use_old else "new",
        "method_text": "沿用原结果" if use_old else "改用新方法",
        "reason": reason,
        "max_diff_seconds": cmp_["max_diff_seconds"],
        "max_diff_at": cmp_["max_at"],
        "first_over_0_5": first,
        "old_self_check": old_check,
        "all_within_0_5": cmp_["all_within_0_5"],
        "line_count_match": cmp_["line_count_match"],
    }


# --------------------------------------------------------------------------
# 运行期共用：S7/S9 等步骤里所有 word_align / R148 调用拿 S4 写出的同一份结果。
#
# ST3-P28b：结果**以磁盘文件为准**（`04-对齐/alignment.json` + `04-对齐/char_times.json`）。
# 旧版只在同一次运行的内存里传递，续跑时 S4 被跳过，后面的步骤就退回旧方法；
# 现在引擎启动时 `set_source(文章根目录)`，任何步骤都能从磁盘读到同一份对齐。
# S4 自己算新旧两套时必须先 `suspend_source()`，否则会把上一次写的盘上结果当成"旧方法"。
# --------------------------------------------------------------------------

_ALIGNMENT_REL = ("04-对齐", "alignment.json")
_CHAR_TIMES_REL = ("04-对齐", "char_times.json")

_ACTIVE = None                 # 进程内共用（兼容旧调用；S4 现在主要走磁盘）
_DISK_ROOT = None
_DISK_SUSPENDED = False
_DISK_CACHE = {}               # str(path) -> ((mtime_ns, size), 解析结果)


def set_source(root):
    """设定对齐结果的来源文章根目录（引擎每个进程的开头调用）。"""
    global _DISK_ROOT, _DISK_SUSPENDED
    _DISK_ROOT = Path(root) if root is not None else None
    _DISK_SUSPENDED = False
    _DISK_CACHE.clear()


def suspend_source():
    """临时不读磁盘（S4 计算新旧两套对齐时用，避免读到上一轮写下的结果）。"""
    global _DISK_SUSPENDED
    _DISK_SUSPENDED = True


def set_active(word_results, char_results, groups, sentences):
    """S4 选定新结果后调用；只有同一批分组/同一份配音才会命中。"""
    global _ACTIVE
    _ACTIVE = {
        "groups": [(g.get("group_id"), g.get("text") or "") for g in groups],
        "sent_sig": (len(sentences or []),
                     (sentences[0].get("startTime") if sentences else None),
                     (sentences[-1].get("endTime") if sentences else None)),
        "word": word_results,
        "char": char_results,
    }


def clear_active():
    global _ACTIVE
    _ACTIVE = None


def _load_json_file(path):
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    hit = _DISK_CACHE.get(str(path))
    if hit is not None and hit[0] == sig:
        return hit[1]
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    _DISK_CACHE[str(path)] = (sig, obj)
    return obj


def _disk_results(kind, groups):
    """从磁盘读 S4 的成果；文件缺失或与本批分组不一致时返回 None（调用方退回计算）。"""
    if _DISK_ROOT is None or _DISK_SUSPENDED:
        return None
    align = _load_json_file(_DISK_ROOT.joinpath(*_ALIGNMENT_REL))
    chars = _load_json_file(_DISK_ROOT.joinpath(*_CHAR_TIMES_REL))
    if not isinstance(align, list) or not isinstance(chars, list):
        return None
    gids = [g.get("group_id") for g in groups]
    if [r.get("group_id") for r in align] != gids:
        return None
    if [r.get("group_id") for r in chars] != gids:
        return None
    for row, g in zip(chars, groups):
        if (row.get("text") or "") != (g.get("text") or ""):
            return None
    return align if kind == "word" else chars


def alignment_signature(root):
    """当前对齐文件的指纹（S6/S7/S9 判新鲜度用；文件缺失也如实入算）。"""
    h = hashlib.sha256()
    for parts in (_ALIGNMENT_REL, _CHAR_TIMES_REL):
        h.update("/".join(parts).encode("utf-8"))
        p = Path(root).joinpath(*parts)
        h.update(p.read_bytes() if p.is_file() else b"<missing>")
    return h.hexdigest()


def active_results(kind, sentences, groups):
    """word_align / R148 的统一入口：进程内结果优先，其次磁盘；都没有返回 None。"""
    a = _ACTIVE
    if a:
        sig = (len(sentences or []),
               (sentences[0].get("startTime") if sentences else None),
               (sentences[-1].get("endTime") if sentences else None))
        pairs = [(g.get("group_id"), g.get("text") or "") for g in groups]
        if sig == a["sent_sig"] and pairs == a["groups"]:
            return a[kind]
    return _disk_results(kind, groups)
