# -*- coding: utf-8 -*-
"""R148 C2：原文逐字/逐词 → 豆包 word 时间戳 的对齐层。

用途：字幕显示用 display_text(原文, 含 "/v1/"、"cc-AWS"、"sk-"), 时间戳来自豆包字级 word 流。

锚点思路(R146 提出)：
  display_text(原文) --narration_prep.prep_tts_text(1:1, 除产品连字符删1)--> tts_text(提交原文=sentence.text)
  tts_text --豆包归一化(数字→中文、/→斜杠、-→杠 等)--> words[].word(朗读版, 带时间戳)

算法(处理长度不等)：
  1. 用 difflib 把 tts_text 对齐到 spoken 逐字流(每字挂其所属 word 的 startTime/endTime)。
  2. 匹配块内的 tts 字符 → 对应流字符的时间戳。
  3. 未匹配的 tts 字符(数字→中文、/→斜杠 这类豆包归一化导致的不等) → 取「最近的已匹配/相邻流字符」的时间戳(就近挂靠, 不漂移)。
  4. display 字符 ↔ tts 字符 1:1(narration_prep 契约), 产品连字符被删的槽位给 0 时长。

本模块不修改 word_align.py, 只在分组级对齐之外新增逐字映射。
"""
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline" / "vendor" / "zhongzhuanzhan"))
import narration_prep  # 仅用 prep_tts_text 做 display→tts 1:1 桥接（ST1-P5b 乙：改从 vendor 导入）


def build_flow(sentences):
    """spoken word 逐字流 + 每字 (startTime, endTime)。保留 spoken 文本, 不做 lossy _norm。"""
    chars = []
    times = []
    for s in sentences:
        for w in s.get("words", []):
            wt = w.get("word", "")
            st, et = w.get("startTime"), w.get("endTime")
            for ch in wt:
                chars.append(ch)
                times.append((st, et))
    return "".join(chars), times


def align_group_chars(display_text, flow, times, pos):
    """把一段 display_text 对齐到 spoken flow 的 [pos:] 窗口, 返回每 display 字符的时间戳。

    返回 (char_times, new_pos): char_times 是 [(startTime,endTime), ...] 与 display_text 等长。
    """
    import narration_prep
    tts_text = narration_prep.prep_tts_text(display_text)  # display → tts (1:1 桥接)
    if not tts_text:
        return [], pos

    # ST3-P2c 裁定2：把搜索窗口收窄到「文本长度 3 倍 + 20 字符」以内，
    # 防止 difflib 因重复子串（如 H1/H2/H3、句号）匹配到很远的错误位置，导致组时长
    # 远超按字数估算时长、并让 pos 向后连锁错位。
    win_size = max(3 * len(tts_text), len(tts_text) + 20)
    window = flow[pos:pos + win_size]
    sm = difflib.SequenceMatcher(None, window, tts_text, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size > 0]

    # tts 字符下标 -> 流字符下标 (匹配块内 1:1)
    tts_to_flow = {}  # tts_idx -> flow_idx
    for b in blocks:
        for k in range(b.size):
            tts_to_flow[b.b + k] = b.a + k

    # 为每个 tts 字符确定流下标: 匹配的用匹配, 未匹配的用「就近已匹配」插值
    flow_idx_for_tts = []
    last_flow = None
    # 先收集所有已匹配的 (tts_idx, flow_idx) 有序对
    matched_pairs = sorted(tts_to_flow.items())
    for ti in range(len(tts_text)):
        if ti in tts_to_flow:
            last_flow = tts_to_flow[ti]
            flow_idx_for_tts.append(tts_to_flow[ti])
        else:
            # 未匹配: 找最近的已匹配 tts 下标, 用其 flow 下标(就近挂靠); 否则用上一个 flow 或 0
            if last_flow is not None:
                flow_idx_for_tts.append(last_flow)
            else:
                # 前面还没匹配到, 用后面第一个匹配的
                nxt = next((f for t, f in matched_pairs if t > ti), None)
                flow_idx_for_tts.append(nxt if nxt is not None else 0)

    char_times = []
    for ti in range(len(tts_text)):
        fi = flow_idx_for_tts[ti]
        fi = min(fi, len(times) - pos - 1) if pos + fi < len(times) else len(times) - pos - 1
        if fi < 0:
            char_times.append((None, None))
        else:
            char_times.append(times[pos + fi])

    # 推进 pos 到这段 tts 在流里的结束位置(单调, 防回退)
    max_fi = max(flow_idx_for_tts) if flow_idx_for_tts else 0
    new_pos = pos + max_fi + 1

    # F2(R158): 把 tts 长度的 char_times 映射回 display 长度。
    # prep_tts_text 中 product 类连字符被删(display 有、tts 无)，其槽位给 (None,None)(0 时长)，
    # 使返回 char_times 与 display_text 等长，避免上层 zip 静默截断丢字。
    d2t = []
    ti2 = 0
    for di, ch in enumerate(display_text):
        if ch == "-" and narration_prep.classify_hyphen(display_text, di) == "product":
            d2t.append(None)
        elif ch == "第" and narration_prep.is_section_di(display_text, di):
            # ST3-P3d 修复一：点分序号前的「第」被删(豆包念「第二点三点七」语调奇怪)，给 0 时长槽。
            d2t.append(None)
        else:
            d2t.append(ti2)
            ti2 += 1
    display_char_times = [char_times[d2t[di]] if d2t[di] is not None else (None, None)
                          for di in range(len(display_text))]
    return display_char_times, new_pos


def align_groups(sentences, groups):
    """对每个分组, 返回 display_text 逐字时间戳(与 word_align.align_groups 的分组级结果并存, 不替代)。

    ST3-P28：S4 判定整篇改用新方法时，这里直接返回新方法的同一份结果
    （见 pipeline.align_global）；沿用旧结果时没有覆盖，代码路径与 v5.3 完全一致。

    ST3-P28b：对齐结果以磁盘为准。进程内没有 S4 的覆盖（例如续跑时 S4 被跳过）时，
    `align_global.active_results` 会读本篇 `04-对齐/char_times.json`，分组对得上就返回，
    不再退回旧算法；只有 S4 自己在计算旧方法时才 suspend 磁盘读取。
    """
    try:
        from pipeline import align_global
        hit = align_global.active_results("char", sentences, groups)
        if hit is not None:
            return hit
    except Exception:
        pass

    flow, times = build_flow(sentences)
    pos = 0
    out = []
    for g in groups:
        gid = g["group_id"]
        text = g["text"]
        char_times, pos = align_group_chars(text, flow, times, pos)
        # F2(R158): 长度不等显式报警，绝不静默截断
        if len(char_times) != len(text):
            print(f"[ALIGN-WARN] {gid}: display_text 长度 {len(text)} != char_times 长度 {len(char_times)}，存在对齐缺口",
                  file=sys.stderr)
        # 首尾时间
        valid = [(a, b) for a, b in char_times if a is not None]
        start = valid[0][0] if valid else None
        end = valid[-1][1] if valid else None
        out.append({
            "group_id": gid,
            "text": text,
            "char_times": [{"c": c, "startTime": t[0], "endTime": t[1]} for c, t in zip(text, char_times)],
            "start_time": start,
            "end_time": end,
        })
    return out
