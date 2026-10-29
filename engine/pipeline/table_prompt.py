# -*- coding: utf-8 -*-
"""表格 → 横版插画 prompt（基础版）。"""


def _table_rows(table_text):
    rows = []
    for ln in (table_text or "").split("\n"):
        ln = ln.strip()
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if not cells or all(c == "" for c in cells):
            continue
        if all(c.replace("-", "").replace(":", "") == "" for c in cells):
            continue
        rows.append(cells)
    return rows


def build_table_prompt(table_text):
    """把 markdown 表格内容转成横版并排布局的插画 prompt。"""
    rows = _table_rows(table_text)
    if len(rows) >= 2:
        data = rows[1:]
    else:
        data = rows
    items = []
    for r in data:
        cells = [c for c in r if c]
        if not cells:
            continue
        head = cells[0]
        rest = "，".join(cells[1:])
        items.append(f"「{head}」({rest})" if rest else f"「{head}」")
    body = "；".join(items)
    return (
        "一张用于知识科普视频的简洁扁平插画，非摄影质感。\n"
        "画面背景为白色。\n"
        f"画面构图为横版并排布局：把下面 {len(items)} 个分类各画成一张独立卡片，"
        f"卡片从左到右横向排开，不要竖向堆叠。分类内容：{body}。\n"
        "每个分类一张卡片，卡片上标注该分类的名称。\n"
        "视觉处理：以深灰线条与色块为主，蓝色作为唯一强调色；不做渐变与大面积装饰，保持留白。\n"
        "标注文字：分类名称用清晰文字写在对应卡片上，不要添加其他结论性文字。"
    )
