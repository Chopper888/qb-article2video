# -*- coding: utf-8 -*-
"""
字级 words 对齐模块（R108 实现，可复用）

把"分组文本"映射到豆包 TTS 返回的"字级 words 念白流"的时间戳。

背景（R104 结论）：
- 句级 sentences[].text = 原文（不能当念白用）
- 字级 sentences[].words[].word = 归一化念白（逐字 + startTime/endTime）
- 豆包归一化不稳定（数字→中文、=→等于、+→加、ASCII 转小写、去空格、【】→（）、序数 1.→一.），
  且会把多个短句合并成一句（句级边界消失）。

本模块的策略（尽力而为，非精确）：
- 构建"念白流"：words 拼接成归一化字符序列 + 每字符时间戳。
- 每个分组文本做"轻度归一化"（只保留汉字/字母/数字，字母转小写，丢标点/空白/符号），
  在念白流里用 difflib 找最佳近似匹配区间，得到起止时间戳。
- 返回每个分组的 ratio（匹配质量），用于标识"精确/模糊"（归一化差异大的分组 ratio 低）。
- 数字不转中文（因为豆包数字→中文规则上下文相关、不稳定，自建规则做不准），
  所以含数字的分组 ratio 会偏低——这是已知局限，如实暴露给上层。
"""
import difflib


def _norm(s):
    """轻度归一化：只保留汉字/字母/数字，字母转小写，丢其他。数字保留原样（不转中文）。"""
    out = []
    for ch in s:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            out.append(ch.lower())
    return "".join(out)


def build_flow(sentences):
    """构建念白流：归一化字符序列 + 每个字符的 (startTime, endTime)。

    返回 (chars, times)，chars[i] 对应 times[i]。

    关键（R127 修复）：豆包字级 words 里，英文单词是"多字符的单一 word"
    （如 "OpenAI"、"Claude"、"Cursor"）。align_groups 把念白流当逐字符序列、
    用字符下标去索引 times；若一个 word 只占 times 一个槽位、却在 flow 里
    展开成多个字符，chars 与 times 下标就会错位，导致起止时间整体后漂
    （R123 发现的 G25/G27 约 7~8 秒漂移即由此而来）。因此这里必须把每个
    word 展开成逐字符、每个字符都挂同一个 (startTime, endTime)，
    保证 len(chars) == len(times) == len(flow)。
    """
    chars = []
    times = []
    for s in sentences:
        for w in s.get("words", []):
            c = _norm(w.get("word", ""))
            if c:
                st, et = w.get("startTime"), w.get("endTime")
                for ch in c:
                    chars.append(ch)
                    times.append((st, et))
    return chars, times


def align_groups(sentences, groups):
    """把分组文本映射到念白流时间戳（按分组顺序，单调推进）。

    groups: [{group_id, text}, ...]
    返回 [{group_id, start_time, end_time, ratio, matched_chars}]
      - ratio: difflib 匹配质量（0~1），<0.9 表示归一化差异较大（如含数字/英文）。
      - matched_chars: 匹配到的念白流字符数（用于诊断）。

    ST3-P28：S4 判定整篇改用新方法时，这里直接返回新方法的同一份结果
    （见 pipeline.align_global）；沿用旧结果时没有覆盖，代码路径与 v5.3 完全一致。

    ST3-P28b：对齐结果以磁盘为准。进程内没有 S4 的覆盖（例如续跑时 S4 被跳过）时，
    `align_global.active_results` 会读本篇 `04-对齐/alignment.json`，分组对得上就返回，
    不再退回旧算法；只有 S4 自己在计算旧方法时才 suspend 磁盘读取。
    """
    try:
        from pipeline import align_global
        hit = align_global.active_results("word", sentences, groups)
        if hit is not None:
            return hit
    except Exception:
        pass

    chars, times = build_flow(sentences)
    flow = "".join(chars)
    results = []
    pos = 0  # 念白流里的搜索起点（单调递增，避免回退）

    for g in groups:
        gid = g.get("group_id", "?")
        gnorm = _norm(g.get("text", ""))
        if not gnorm:
            # ST1-P5b 丙：空文本组（如 markdown 表格分隔行 `| --- | --- |` 归一化后为空）
            # 归 0 时长而非 None，避免下游 `start_time * FPS` 崩溃。
            results.append({"group_id": gid, "start_time": 0.0, "end_time": 0.0,
                            "ratio": 0, "matched_chars": 0})
            continue

        window = flow[pos:]
        if not window:
            results.append({"group_id": gid, "start_time": 0.0, "end_time": 0.0,
                            "ratio": 0, "matched_chars": 0})
            continue

        sm = difflib.SequenceMatcher(None, window, gnorm, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size > 0]

        if not blocks:
            results.append({"group_id": gid, "start_time": 0.0, "end_time": 0.0,
                            "ratio": 0, "matched_chars": 0})
            continue

        start_off = blocks[0].a
        end_off = min(blocks[-1].a + blocks[-1].size, len(window))  # clamp 到窗口末尾

        start_time = times[pos + start_off][0] if pos + start_off < len(times) else 0.0
        # end_time 取最后一个匹配字的时间戳（clamp 到念白流末尾）
        end_idx = min(pos + end_off - 1, len(times) - 1) if end_off > 0 else None
        end_time = times[end_idx][1] if end_idx is not None else 0.0
        matched = sum(b.size for b in blocks)
        # 覆盖度：分组文本有多少被匹配到（比 difflib.ratio 对整窗口更直观）
        coverage = round(matched / len(gnorm), 3) if gnorm else 0.0

        # 推进搜索起点（到分组结束之后）
        pos += end_off

        results.append({"group_id": gid, "start_time": start_time, "end_time": end_time,
                        "ratio": coverage, "matched_chars": matched})

    return results
