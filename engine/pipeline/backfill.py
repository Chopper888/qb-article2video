# -*- coding: utf-8 -*-
"""backfill：用上下文把图片机械回填到切段后的正文组（收成自 R168 r168_backfill.py，M2-P1 B 步）。

对每张图，取「紧邻上文 before」（若 before 是标题/为空则取 after）作匹配上下文，
对每个正文组做归一化后 difflib 相似度匹配，取最高分且超过阈值者为命中组。
输出更新后的 image_map.json（含 mapped_group_id / match_basis / confidence）。

收成改动（仅两类，遵守「搬家不改写」）：
  1. 硬编码绝对路径（R168/IMAGE_MAP/GROUPS）→ 函数参数；
  2. 文件读写与纯逻辑分离：backfill_images() 纯计算，build_backfill() 薄入口。
"""
import json
import re
import difflib
from pathlib import Path

THRESHOLD = 0.35  # 归一化后 difflib ratio 阈值，低于此判「匹配不上」


def norm(s):
    return re.sub(r"[\s&、，,。．.？?！!：:；;【】“”\"'（）()\-—:：/\\·～~…]", "", s or "")


def is_heading(s):
    return bool(s) and (s.startswith("#") or s.strip().startswith("（") or re.match(r"^[一二三四五六七八九十]+[、.．]", s))


def backfill_images(imap, groups):
    """纯逻辑：把 imap 里的每张图回填 mapped_group_id/match_basis/confidence（就地更新 imap 后返回）。"""
    body = [g for g in groups if not g["starts_with_bracket_title"]]

    results = []
    for im in imap["images"]:
        before = im["before"] or ""
        after = im["after"] or ""
        # 匹配上下文：before 是标题/空则用 after（图片在段落开头）
        ctx = after if (not before or is_heading(before)) else before
        ctx_n = norm(ctx)
        best = None
        for g in body:
            ratio = difflib.SequenceMatcher(None, ctx_n, norm(g["group_text"])).ratio()
            if best is None or ratio > best[1]:
                best = (g, ratio)
        match_basis = ("after" if (not before or is_heading(before)) else "before")
        if best is None:
            # ST1-P5b 丙：无正文组（纯图片段落/全文无正文）时不再解包 None 崩溃；
            # 留空组号，交给 step_s5 的位置兜底挂组。
            im["mapped_group_id"] = None
            im["match_basis"] = match_basis
            im["confidence"] = 0.0
            im["matched_text_head"] = ""
        else:
            g, ratio = best
            im["mapped_group_id"] = g["group_id"] if ratio >= THRESHOLD else None
            im["match_basis"] = match_basis
            im["confidence"] = round(ratio, 3)
            im["matched_text_head"] = g["group_text"][:40]
        results.append(im)

    imap["images"] = results
    return imap


def build_backfill(image_map_path, groups_path):
    """薄入口：读 image_map.json + groups_raw.json，回填后写回 image_map.json。"""
    imap = json.loads(Path(image_map_path).read_text(encoding="utf-8"))
    groups = json.loads(Path(groups_path).read_text(encoding="utf-8"))["groups"]
    backfill_images(imap, groups)
    Path(image_map_path).write_text(json.dumps(imap, ensure_ascii=False, indent=1), encoding="utf-8")
    return imap


if __name__ == "__main__":
    import sys
    build_backfill(sys.argv[1], sys.argv[2])
    print("backfill done")
