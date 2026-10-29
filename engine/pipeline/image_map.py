# -*- coding: utf-8 -*-
"""image_map：从原始 markdown 机械抽取图片位置映射（收成自 R168 r168_image_map.py，M2-P1 B 步）。

纯脚本机械抽取，不依赖 LLM。每张图记录：文件路径、原文行号、前后各一段非空上下文。
预留后续字段（render_mode/crop_box/annotation/group_index），本轮填空值/默认值不消费。

收成改动（仅两类，遵守「搬家不改写」）：
  1. 硬编码绝对路径（ARTICLE/OUT）→ 函数参数；
  2. 文件读写与纯逻辑分离：extract_images() 纯计算，build_image_map() 薄入口。
"""
import json
import re
import urllib.parse
from pathlib import Path

IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
# 第一个一级标题指第一个「# 数字、」形式的章节标题，不是文章标题
SECTION_H1_RE = re.compile(r"^#\s*[一二三四五六七八九十]+\s*[、，,.]")


def detect_following_table(lines, img_idx):
    """返回图片行之后紧跟的 markdown 表格文本(若有), 否则 None。跳过空行后连续 | 行。"""
    j = img_idx + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    tbl = []
    while j < len(lines) and lines[j].strip().startswith("|"):
        tbl.append(lines[j].strip())
        j += 1
    return "\n".join(tbl) if len(tbl) >= 2 else None


def first_section_h1_line(lines):
    for i, line in enumerate(lines):
        if SECTION_H1_RE.match(line):
            return i + 1  # 1-based
    return None


def extract_images(text, article_path):
    """纯逻辑：从 markdown 文本抽取图片映射，返回 payload dict（不读写文件）。"""
    lines = text.splitlines()
    first_h1 = first_section_h1_line(lines)

    current_heading = None
    images = []
    for i, line in enumerate(lines):
        h = HEADING_RE.match(line)
        if h:
            current_heading = h.group(2).strip()
            continue
        for m in IMG_RE.finditer(line):
            before = None
            for j in range(i - 1, -1, -1):
                if lines[j].strip() and not IMG_RE.search(lines[j]):
                    before = lines[j].strip()
                    break
            after = None
            for j in range(i + 1, len(lines)):
                if lines[j].strip() and not IMG_RE.search(lines[j]):
                    after = lines[j].strip()
                    break
            line_no = i + 1
            table = detect_following_table(lines, i)
            # 第一个一级标题之前的图片一律不插入
            if first_h1 is not None and line_no < first_h1:
                insert, exclude_reason = False, "before_first_h1"
            elif table is not None:
                # W6: 图片后紧跟表格 -> 用表格生成横版插画, 原图不插入
                insert, exclude_reason = False, "replaced_by_table"
            else:
                insert, exclude_reason = True, None
            images.append({
                "image_path": m.group(1).strip(),
                # C: 图片文件真实相对路径（%20 已解码），供 orchestrate 取真实文件名；image_path 原值不动
                "source_relative_path": urllib.parse.unquote(m.group(1).strip()),
                "line": line_no,
                "nearest_heading": current_heading,
                "before": before,
                "after": after,
                # 预留字段（本轮不消费）
                "render_mode": "full",
                "crop_box": None,
                "annotation": None,
                "group_index": 0,
                # W2/W6: 插入控制字段
                "insert": insert,
                "exclude_reason": exclude_reason,
                "has_table": table is not None,
                "table_content": table,
                # 回填字段（由 backfill.py 填）
                "mapped_group_id": None,
                "match_basis": None,
                "confidence": None,
            })

    return {
        "article_path": article_path,
        "image_count": len(images),
        "note": "机械抽取: 每张图 = 路径 + 原文行号 + 前后各一段非空上下文; R168 W2/W6 新增 insert/exclude_reason/has_table/table_content 字段",
        "images": images,
    }


def build_image_map(article_path, out_path):
    """薄入口：读 article_path，抽取并写 out_path，返回 payload。"""
    text = Path(article_path).read_text(encoding="utf-8")
    payload = extract_images(text, article_path)
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


if __name__ == "__main__":
    import sys
    payload = build_image_map(sys.argv[1], sys.argv[2])
    print("image_count:", payload["image_count"])
    for im in payload["images"]:
        ins = "插入" if im["insert"] else f"不插入({im['exclude_reason']})"
        tbl = " 有表格" if im["has_table"] else ""
        print(f"  L{im['line']} {im['image_path']} | {ins}{tbl} | 标题[{im['nearest_heading']}]")
