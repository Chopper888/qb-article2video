# -*- coding: utf-8 -*-
"""pipeline 调度层 · 决策判断（P1）。

从 R160/r160_gen.py 逐字抽取的纯决策函数：输入确定、输出确定、无副作用
（不调 API、不读写文件）。含导览检测、无图组识别(N2/N3)、转场选择、标题场景。
编排主循环(P2)、场景构建(P3)不在此文件。

抽取原则（R163 P1）：搬家不改写，函数体与 r160_gen.py 逐字一致。
例外：ST3-P25 改了 detect_overview 的「≥2 个导览候选」分支（见该函数说明）。
"""
import random
import re

OVERVIEW_SIGNALS = ["主要围绕", "主要讲", "主要聊", "围绕几个", "分为几个"]
ORDINAL_TOPIC_RE = re.compile(r"^第([一二三四五六七八九十百\d]+)\s*[，,、]")
ARABIC_ORDINAL_RE = re.compile(r"^(\d+)\s*[.．、，]")  # R160: 口播稿可能用 "1." 而非 "第一，"
CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
TRANSITION_MAX_LEN = 30  # N2: 过渡语段非空白字符数上限
FUNC_SHORT_MAX_LEN = 6   # N3: 纯功能性短句非空白字符数上限
# R160 F3: 去掉 FUNC_OPEN_RE 词表。N3 只保留「非空白字符数<=6」这条纯长度规则，
# 不再用「^接下来/^最后」等词表拦截长句(会把 30+ 字实质长句错杀)。长句交给弃权机制判断。
# R160 F1: 二级导览句——主 trigger 之后紧跟的「从X个角度/方面/维度/层面」结构句，
# 是导览段落的一部分，须在弃权判定之前被识别(见 detect_overview)。
ANGLE_INTRO_RE = re.compile(r"从.{0,6}个?(?:角度|方面|维度|层面)")


def cnum(s):
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        p = s.split("十")
        a = CN[p[0]] if p[0] else 1
        b = CN[p[1]] if len(p) > 1 and p[1] else 0
        return a * 10 + b
    return sum(CN.get(ch, 0) for ch in s) if all(ch in CN for ch in s) else None


def is_overview(text):
    return any(m in text for m in OVERVIEW_SIGNALS)


def ordinal_val(text):
    m = ORDINAL_TOPIC_RE.match(text.strip()) or ARABIC_ORDINAL_RE.match(text.strip())
    if not m:
        return None
    return cnum(m.group(1))


def collect_overview_groups(body_groups, trigger_idx):
    collected = []
    # R160: 触发组文本末尾可能已含"第一，xxx"(序数并入触发句), 探测其序数作为起点
    trigger_text = body_groups[trigger_idx]["group_text"]
    prev = 0
    for m in re.finditer(r"第([一二三四五六七八九十百\d]+)\s*[，,、]", trigger_text):
        v = cnum(m.group(1))
        if v is not None:
            prev = v
    for j in range(trigger_idx + 1, len(body_groups)):
        v = ordinal_val(body_groups[j]["group_text"])
        if v is None or v != prev + 1:
            break
        collected.append(body_groups[j])
        prev = v
    return collected


def _norm(s):
    return re.sub(r"[\s&、，,。．.？?！!：:；;【】“”\"'（）()]", "", s or "")


def title_core(title_text):
    core = title_text.strip("【】")
    core = re.sub(r"^[一二三四五六七八九十]+[、，,]\s*", "", core)
    return _norm(core)


def content_overlaps_title(title_text, body_text):
    """N2: 内容是否与标题高度重合(标题核心词是正文子串, 或覆盖度>=0.6)。"""
    core = title_core(title_text)
    nb = _norm(body_text)
    if core and core in nb:
        return True
    if core and nb and len(core) >= 2:
        import difflib
        sm = difflib.SequenceMatcher(None, nb, core)
        # 覆盖度 = 所有匹配块字符数 / core 长度(允许"这些词"这类插入, 不要求连续)
        matched = sum(b.size for b in sm.get_matching_blocks())
        return matched / max(len(core), 1) >= 0.6
    return False


def identify_no_image_groups(groups):
    """返回 (transition_ids, func_short_ids)。按形式判定, 不硬编码。"""
    transition_ids = set()
    func_short_ids = set()
    for i, g in enumerate(groups):
        if not g["starts_with_bracket_title"]:
            continue
        nxt = groups[i + 1] if i + 1 < len(groups) else None
        if nxt is None or nxt["starts_with_bracket_title"]:
            continue
        if (len(re.sub(r"\s", "", nxt["group_text"])) <= TRANSITION_MAX_LEN
                and content_overlaps_title(g["group_text"], nxt["group_text"])):
            transition_ids.add(nxt["group_id"])
    # N3: 纯功能性短句(极短, 非紧邻标题组)。R160 F3: 只保留长度规则, 去掉词表
    for g in groups:
        if g["starts_with_bracket_title"]:
            continue
        if g["group_id"] in transition_ids:
            continue
        t = g["group_text"].strip()
        if len(re.sub(r"\s", "", t)) <= FUNC_SHORT_MAX_LEN:
            func_short_ids.add(g["group_id"])
    return transition_ids, func_short_ids


TRANSITION_POOLS = {
    "opening": ["black-slam", "overexpose-flip"],
    "chapter": ["frosted-glass-wipe", "grid-pixelate-wipe", "whip-pan", "push-through"],
    "inbetween": ["caret-wipe", "pullback-cool", "shape-wipe"],
}


def pick_transition(prev, cur, is_first):
    if is_first:
        kind = "opening"
    elif (prev or {}).get("starts_with_bracket_title") or cur.get("starts_with_bracket_title"):
        kind = "chapter"
    else:
        kind = "inbetween"
    return kind, random.choice(TRANSITION_POOLS[kind])


def overview_hits(body):
    """导览候选：is_overview 命中的正文组，按正文顺序 [(下标, 组), ...]。"""
    return [(i, g) for i, g in enumerate(body) if is_overview(g["group_text"])]


def overview_ambiguity(body):
    """ST3-P25：导览候选 ≥2 个时的判定记录（detect_overview 只认最靠前的一个）；不足 2 个返回 None。

    返回 {"chosen": {...}, "ignored": [{...}, ...]}，每项 {"group_id", "text"}（原文前 60 字）。
    """
    hits = overview_hits(body)
    if len(hits) < 2:
        return None

    def row(g):
        return {"group_id": g["group_id"], "text": g["group_text"][:60]}
    return {"chosen": row(hits[0][1]), "ignored": [row(g) for _i, g in hits[1:]]}


def detect_overview(body):
    """返回 (hits, units)。units = [(trigger_group, [absorbed...]), ...]。

    第一个 unit 是主 trigger(is_overview 命中)及其吸收的序数正文；若主 trigger 之后紧跟
    「从X个角度/方面/维度/层面」二级导览句(非序数、非标题)，则该句也作为一个导览 unit，
    其后序数正文作为它的 absorbed。这样导览组都在弃权判定之前被识别，不进入弃权流程(R160 F1)。

    SHOT-M4-P4 C6：命中 0 个导览组视为合法（返回 (hits=[], units=[])）。
    ST3-P25：命中 2 个及以上时不再返回 units=None（调用方会整篇停下），改为只认按正文顺序
    **最靠前**的一个，返回值与"只命中它一个"完全相同；其余候选由 `overview_ambiguity`
    记下，引擎写进 `logs/导览判定.json`、检查报告提示。0 个、1 个时行为不变。
    """
    hits = overview_hits(body)
    if len(hits) == 0:
        return [], []
    hits = hits[:1]
    idx, trigger = hits[0]
    units = [(trigger, collect_overview_groups(body, idx))]
    nxt_idx = idx + 1 + len(units[0][1])
    if nxt_idx < len(body) and ANGLE_INTRO_RE.search(body[nxt_idx]["group_text"]):
        angle = body[nxt_idx]
        units.append((angle, collect_overview_groups(body, nxt_idx)))
    return hits, units
