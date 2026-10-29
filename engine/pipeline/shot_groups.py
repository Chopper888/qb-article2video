# -*- coding: utf-8 -*-
"""shot_groups：截图分组生成（SHOT-M4-P8 B1 收成，自 P7 p7_shot_groups.py 改写）。

规则（与 P7 完全一致）：
  - group_id / images：取自 image_map 的 mapped_group_id（以回填结果为准）；
  - narration：取自 groups_raw 中该组的 group_text；
  - article：在原文中找到该组第一张图所在位置，往前找最近的一个标题（任意级别），
    从这个标题的下一行截到下一个标题（任意级别）之前，删掉其中的图片行，其余正文原样保留。
    若该组图片分布在多个小节，按原文顺序把这些小节的正文拼接起来。

对外提供一个函数 gen_shot_groups(article_path, image_map, groups_raw) -> list。
路径一律由调用方传入，模块内无写死路径。
"""
import json
import re
import urllib.parse
from pathlib import Path

HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def parse_article(text):
    lines = text.splitlines()
    headings = []  # (idx0, level, text)
    for i, ln in enumerate(lines):
        m = HEAD_RE.match(ln)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip()))
    return lines, headings


def nearest_heading(headings, line_idx):
    """返回 line_idx 处（含）往前最近的标题，无则 None。"""
    h = None
    for hi, lvl, txt in headings:
        if hi <= line_idx:
            h = (hi, lvl, txt)
        else:
            break
    return h


def subsection_body(lines, headings, h_start):
    """取 h_start 标题的正文：从标题下一行到下一个标题（任意级别）之前，删图片行。"""
    hi = h_start[0]
    end = len(lines)
    for hj, lvl, txt in headings:
        if hj > hi:
            end = hj
            break
    body = []
    for i in range(hi + 1, end):
        ln = lines[i]
        if IMG_RE.search(ln):
            continue
        s = ln.strip()
        if s:
            body.append(s)
    return body


def gen_shot_groups(article_path, image_map, groups_raw):
    """生成截图分组列表。

    article_path: 文章原文 md 路径（str 或 Path）。
    image_map: image_map 回填结果（dict，含 images[].mapped_group_id / line / image_path）。
    groups_raw: groups_raw（dict，含 groups[].group_id / group_text）。
    返回 [{group_id, images, narration, article}, ...]。
    """
    text = Path(article_path).read_text(encoding="utf-8")
    lines, headings = parse_article(text)

    narr_by_gid = {g["group_id"]: g["group_text"] for g in groups_raw["groups"]}

    # 按 mapped_group_id 归组图片，按 line 排序
    by_gid = {}
    for e in image_map["images"]:
        gid = e.get("mapped_group_id")
        if not gid:
            continue
        by_gid.setdefault(gid, []).append(e)
    for gid in by_gid:
        by_gid[gid].sort(key=lambda e: e["line"])

    groups = []
    for gid in sorted(by_gid, key=lambda g: int(g[1:])):
        es = by_gid[gid]
        images = [Path(urllib.parse.unquote(e["image_path"])).name for e in es]
        narration = narr_by_gid.get(gid, "")
        # 每个图所在小节（去重，按原文顺序）
        subs = {}
        for e in es:
            h = nearest_heading(headings, e["line"] - 1)  # line 是 1-based
            if h is not None:
                subs[h[0]] = h
        ordered = [subs[k] for k in sorted(subs)]
        parts = []
        for h in ordered:
            parts.extend(subsection_body(lines, headings, h))
        article = " ".join(parts)
        groups.append({"group_id": gid, "images": images,
                       "narration": narration, "article": article})
    return groups
