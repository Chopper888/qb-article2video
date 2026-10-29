# -*- coding: utf-8 -*-
"""layout：截图判型 + 版式矩形（纯计算，M2-P4 版式定稿）。

判型依据真实截图在成片里的实际占位。规则见同目录 LAYOUT_RULES.md。
版式只在 Python 算一次，渲染层将来只读这里的矩形，不再在 TSX 里重算。

本模块不读、不写、不知道 Codex 的 render_mode / crop_box，也不执行任何裁切；
C 型只输出 crop_suggested 建议，版式矩形按 B 型兜底给出。

M2-P4 起：把 M2-P2~P2c 样片验证过的版式写回——侧栏宽按文字算、侧栏与截图卡整体居中、
间距 66、截图卡顶部含 40 高标题栏。
"""

CANVAS_W = 1920
CANVAS_H = 1080
# SAFE/RESERVE 必须与 R57-render-test/src/R125Root.tsx 一致（契约 LAY08 兜底）
SAFE = {"top": 60, "bottom": 60, "left": 80, "right": 80}
RESERVE = 300
SIDEBAR_MAX_W = 645      # 侧栏宽度上限
SIDEBAR_GAP = 66         # 侧栏与截图卡间距
CARD_TITLEBAR_H = 40     # 截图卡标题栏高
C_WHITESPACE_RATIO = 0.40  # C 型留白阈值
# 竖长截图改用滚动的阈值：缩放后宽度小于可用宽度的这个比例时转为滚动显示，
# 否则会缩得太窄、看不清。
SCROLL_WIDTH_RATIO = 0.40
# 滚动速度：每滚过一屏所需秒数（默认值，可调整）。
SCROLL_SEC_PER_SCREEN = 4.0
# 字体宽度系数：按默认中文字体实测校准；换字体后需要重新校准
FONT_WIDTH_FACTOR = 1.02
# 侧栏纵向间距（元素之间的空隙，M2-P2 样片定稿值）
GAP_EYEBROW_TITLE = 12
GAP_TITLE_ITEMS = 24


def _stage():
    """画面区 STAGE = (x=80, y=60, w=1760, h=720)，由常量派生，不另写死。"""
    return {
        "x": SAFE["left"],
        "y": SAFE["top"],
        "w": CANVAS_W - SAFE["left"] - SAFE["right"],
        "h": CANVAS_H - SAFE["top"] - RESERVE,
    }


def _shot_region():
    """右侧截图区 SHOT_REGION = (x=791, y=60, w=1049, h=720)。"""
    stage = _stage()
    return {
        "x": SAFE["left"] + SIDEBAR_MAX_W + SIDEBAR_GAP,
        "y": SAFE["top"],
        "w": stage["w"] - SIDEBAR_MAX_W - SIDEBAR_GAP,
        "h": stage["h"],
    }


def _side_region():
    """左侧侧栏区 SIDE_REGION = (x=80, y=60, w=645, h=720)。"""
    stage = _stage()
    return {
        "x": SAFE["left"],
        "y": SAFE["top"],
        "w": SIDEBAR_MAX_W,
        "h": stage["h"],
    }


STAGE = _stage()
SHOT_REGION = _shot_region()
SIDE_REGION = _side_region()


def _contain(img_w, img_h, box_w, box_h):
    """等比缩放 (img_w, img_h) 完整放进 (box_w, box_h)，返回 (disp_w, disp_h) 浮点。"""
    scale = min(box_w / img_w, box_h / img_h)
    return img_w * scale, img_h * scale


# —— 文字宽度估算（逐字搬移自 M2-P2-样片/m2p2_resolve.py，M2-P4）——
def units_of(ch):
    if ch == " ":
        return 0.3
    return 0.55 if ord(ch) < 0x2e80 else 1.0


def units_of_str(s):
    return sum(units_of(ch) for ch in s)


def estimate_width(text, font):
    """元素估算宽 = unitsOf(文字) × 字号 × FONT_WIDTH_FACTOR + 4。"""
    return units_of_str(text) * font * FONT_WIDTH_FACTOR + 4


def _compute_sidebar(sidebar):
    """按内容计算侧栏：返回 (sidebar_w, sidebar_h, elements)。

    sidebar 结构：eyebrow/title 各含 text/font/line_height；items 为列表，
    各含 id/text/font/line_height/dot_w/item_gap。任一元素估算宽超过 645 → ValueError。
    elements = [{"name", "top", "width"}, ...]（width 为浮点估算宽）。
    """
    eyebrow_w = estimate_width(sidebar["eyebrow"]["text"], sidebar["eyebrow"]["font"])
    title_w = estimate_width(sidebar["title"]["text"], sidebar["title"]["font"])
    item_ws = [it["dot_w"] + it["item_gap"] + estimate_width(it["text"], it["font"])
               for it in sidebar["items"]]
    widths = {"eyebrow": eyebrow_w, "title": title_w}
    for it, w in zip(sidebar["items"], item_ws):
        widths[it["id"]] = w
    for name, w in widths.items():
        if w > SIDEBAR_MAX_W:
            raise ValueError(f"侧栏元素「{name}」估算宽 {w:.1f} 超过上限 645，本轮不支持自动换行")
    sidebar_w = min(SIDEBAR_MAX_W, max(widths.values()))

    eyebrow_lh = sidebar["eyebrow"]["line_height"]
    title_lh = sidebar["title"]["line_height"]
    item_lh = sidebar["items"][0]["line_height"]

    eyebrow_top = 0
    title_top = eyebrow_top + eyebrow_lh + GAP_EYEBROW_TITLE
    item_base_top = title_top + title_lh + GAP_TITLE_ITEMS
    item_tops = [item_base_top + i * item_lh for i in range(len(sidebar["items"]))]
    total_h = item_tops[-1] + item_lh

    elements = [
        {"name": "eyebrow", "top": eyebrow_top, "width": eyebrow_w},
        {"name": "title", "top": title_top, "width": title_w},
    ]
    for idx, it in enumerate(sidebar["items"]):
        elements.append({"name": it["id"], "top": item_tops[idx], "width": item_ws[idx]})

    return sidebar_w, total_h, elements


def classify_and_layout(img_w, img_h, sidebar=None):
    """判型 + 版式矩形。

    sidebar 传入时（见 _compute_sidebar 的结构说明），按内容算侧栏宽并整体居中排版；
    不传则侧栏按上限 645、卡在 SHOT_REGION 内，仅用于判型。

    返回字段：kind / aspect_ratio / scale / whitespace_ratio / crop_suggested / reason
              + card_rect（含标题栏整卡）/ titlebar_rect / image_rect（卡内截图显示区）
              + side_rect / side_elements（各元素 top 与宽）。
    shot_rect 为兼容别名，恒等于 image_rect。
    """
    aspect = img_w / img_h
    stage_aspect = STAGE["w"] / STAGE["h"]

    if aspect >= stage_aspect:
        # A 型：截图在画面区 STAGE 内等比完整放下，水平垂直居中。
        # A 型是否加标题栏本轮不定（待 M4），故 card_rect 暂不加标题栏、等于截图显示区。
        disp_w, disp_h = _contain(img_w, img_h, STAGE["w"], STAGE["h"])
        dx = STAGE["x"]
        dw = round(disp_w)
        dy = STAGE["y"] + round((STAGE["h"] - disp_h) / 2)
        dh = round(disp_h)
        top_band = dy - STAGE["y"]
        bottom_band = (STAGE["y"] + STAGE["h"]) - (dy + dh)
        return {
            "kind": "A",
            "aspect_ratio": round(aspect, 4),
            "scale": round(disp_w / img_w, 4),
            "card_rect": {"x": dx, "y": dy, "w": dw, "h": dh},
            "titlebar_rect": None,
            "image_rect": {"x": dx, "y": dy, "w": dw, "h": dh},
            "side_rect": None,
            "side_elements": None,
            "shot_rect": {"x": dx, "y": dy, "w": dw, "h": dh},
            "text_zones": [
                {"x": STAGE["x"], "y": STAGE["y"], "w": STAGE["w"], "h": top_band},
                {"x": STAGE["x"], "y": dy + dh, "w": STAGE["w"], "h": bottom_band},
            ],
            "whitespace_ratio": None,
            "crop_suggested": False,
            "reason": "A 型：宽高比 >= 画面区宽高比，截图在画面区完整放下并居中，上下留白带做文字位",
        }

    # 非 A 型：截图在卡内截图区（SHOT_REGION.w × (SHOT_REGION.h - CARD_TITLEBAR_H)）内 contain
    image_box_w = SHOT_REGION["w"]
    image_box_h = SHOT_REGION["h"] - CARD_TITLEBAR_H
    disp_w, disp_h = _contain(img_w, img_h, image_box_w, image_box_h)
    whitespace_ratio = (image_box_w - disp_w) / image_box_w

    if whitespace_ratio < C_WHITESPACE_RATIO:
        kind, crop = "B", False
        reason = f"B 型：留白比 {whitespace_ratio:.3f} < {C_WHITESPACE_RATIO}，截图宽度占卡内截图区足够"
    else:
        kind, crop = "C", True
        reason = f"C 型：留白比 {whitespace_ratio:.3f} >= {C_WHITESPACE_RATIO}，截图过窄，建议裁切"

    disp_w_r = round(disp_w)
    disp_h_r = round(disp_h)
    card_w = disp_w_r
    card_h = disp_h_r + CARD_TITLEBAR_H

    if sidebar is None:
        # 只判型不排版：侧栏按上限 645，卡在 SHOT_REGION 内水平垂直居中
        side_w = SIDEBAR_MAX_W
        side_h = STAGE["h"]
        side_x = SIDE_REGION["x"]
        side_y = SIDE_REGION["y"]
        card_x = SHOT_REGION["x"] + round((SHOT_REGION["w"] - card_w) / 2)
        card_y = SHOT_REGION["y"] + round((SHOT_REGION["h"] - card_h) / 2)
        side_elements = None
    else:
        side_w_float, side_h, side_elements = _compute_sidebar(sidebar)
        side_w = round(side_w_float)
        overall_w = side_w_float + SIDEBAR_GAP + card_w
        overall_x = STAGE["x"] + round((STAGE["w"] - overall_w) / 2)
        side_x = overall_x
        card_x = overall_x + round(side_w_float) + SIDEBAR_GAP
        card_y = STAGE["y"] + round((STAGE["h"] - card_h) / 2)
        side_y = card_y + round((card_h - side_h) / 2)

    titlebar_y = card_y
    image_y = card_y + CARD_TITLEBAR_H

    return {
        "kind": kind,
        "aspect_ratio": round(aspect, 4),
        "scale": round(disp_w / img_w, 4),
        "card_rect": {"x": card_x, "y": card_y, "w": card_w, "h": card_h},
        "titlebar_rect": {"x": card_x, "y": titlebar_y, "w": card_w, "h": CARD_TITLEBAR_H},
        "image_rect": {"x": card_x, "y": image_y, "w": card_w, "h": disp_h_r},
        "side_rect": {"x": round(side_x), "y": round(side_y), "w": side_w, "h": side_h},
        "side_elements": side_elements,
        "shot_rect": {"x": card_x, "y": image_y, "w": card_w, "h": disp_h_r},
        "text_zones": None,
        "whitespace_ratio": round(whitespace_ratio, 4),
        "crop_suggested": crop,
        "reason": reason,
    }


def is_scroll_candidate(img_w, img_h):
    """ST3-P3b 第2项：取消截图自动滚动，截图全部静态显示。

    原 ST3-P3 竖长截图判定（缩放后宽度 < 40% 可用宽）不再触发滚动，恒返回 False。
    `ScrollImage` 组件保留在渲染层留给 1.1 网页画中画，但不被任何场景调用。
    """
    return False


if __name__ == "__main__":
    import sys
    w, h = int(sys.argv[1]), int(sys.argv[2])
    print(classify_and_layout(w, h))
