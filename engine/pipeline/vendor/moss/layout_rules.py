# -*- coding: utf-8 -*-
"""MOSS 版式规则 v3 正式版（v3draft + 定稿要求）。

相对 v3draft 的改动（对应《方案端同步-Hermes-03-定稿》第一节）：
  1. 长句折行：主标题与标签，宽度 >12 单位时优先在 `，、；：` 或空格处断成两行
     （取最接近一半的断点），无断点按 12 单位硬断；最多 2 行；>24 单位不折、原样交给 OCR。
     折行写法可切换（WRAP_MODE），用于验证模型是否真的画成两行：
        "flow"  单框 + 原文本（v1 行为）
        "nl"    单框 + 文本里插入换行符
        "split" 两个独立框上下排列，各放一行
  2. 边距：文字框距左右边 ≥ W×5%（1360 画布 = 68px），距上边 ≥ H×5%（=38px）；
     底部 22% 安全区不变。
  3. 4 字标签偏宽：LH / PADW 可切换（见 LH、PADW 常量），定稿取实测更优者。
  4. 多词英文：宽度按 0.55/字符 + 空格 0.3 计算，连字符按 0.35；OCR 侧大小写不敏感。
  5. S4 纳入正式风格表。

对外接口与 v1 完全一致：labels_of / default_scene / build_layout / check_overlap。
新增可选关键字参数 wrap_mode（默认取模块常量），不影响旧调用。
"""
import json, math, re

LAYOUT_RULES_VERSION = "v3"

STYLES = ("S0", "S1", "S2", "S3", "S4")
STYLE_NAMES = {"S0": "手绘线稿", "S1": "日系水彩", "S2": "漫画网点",
               "S3": "科技霓虹", "S4": "扁平矢量科普"}

# ---------- 折行与尺寸常量 ----------
WRAP_MAX_UNITS = 12        # 超过就折两行
WRAP_NO_WRAP_UNITS = 24    # 超过就不折，原样交给 OCR 判定
WRAP_BREAKS = "，、；：,;: "
WRAP_MODE = "flow"         # 定稿：flow（单框+原文本，实测模型会自己折两行）

K_TITLE = 46
K_LABEL = 38
LH = 1.25                  # 框高 = 字高 × LH
PADW = 0.98                # 框宽 = max(单位数,1.6) × 字高 × PADW（定稿：A/B 选中 0.98）
MIN_UNITS = 1.6
MARGIN_RATIO = 0.05        # 左右与上边距 = 画布尺寸 × 5%
GAP_Y = 16
GAP_X = 22
TITLE_TOP_MIN = 44
SAFE_RATIO = 0.22

CARRIER_WORDS = ("screen", "window", "browser", "monitor", "paper", "card",
                 "chart", "thumbnail", "panel", "phone", "laptop", "board", "sheet")

# ---------- 按插画类型的通用场景（确定性模板） ----------
TYPE_SCENE = {
    "标题→轻量模板": ("one simple symbolic object floating in the centre of the canvas", None),
    "Type:对比": ("one window split into two halves, left half a plain panel, right half a workbench",
                  "the left panel holds only faint thin horizontal placeholder lines, "
                  "the right panel holds a simple file-tree list"),
    "Type:流程图": ("one person at a desk with a laptop, a small window open on the screen",
                    "the window holds plain grey placeholder blocks and a thin progress bar"),
    "Type:框架": ("a stack of plain cards floating above a desk",
                  "the cards hold only simple icon shapes and thin bars"),
    "Type:场景": ("one phone standing upright beside a laptop on a desk",
                  "the phone screen shows a plain ring, the laptop screen shows a plain grey window"),
    "Type:信息图": ("one large plain panel standing in the centre, a person pointing at it",
                    "the panel is divided into four empty quadrants by thin guide lines only"),
    "Type:时间轴": ("one long horizontal rail across the middle, three small markers on it",
                    "the rail is a thin line, each marker is a plain dot, no text inside"),
    "generic兜底": ("one person working at a desk with a laptop",
                    "the laptop screen shows a plain grey window and a thin progress bar"),
    "空档插画": ("one person working at a desk with a laptop",
                 "the laptop screen shows a plain grey window and a thin progress bar"),
}
DEFAULT_SCENE_KEY = "generic兜底"

STYLE_EN = {
 "S0": ("hand-drawn editorial illustration, black ink line art with a slight natural wobble, "
        "flat light grey-white background, colour only in small accents, generous white space"),
 "S1": ("Japanese hand-drawn animation film style, soft watercolour background, warm natural light, "
        "pale blue sky and soft shadows, fresh translucent colours, simple expressive character lines"),
 "S2": ("Japanese manga style, black and white line art with screentone shading, strong bold lines, "
        "high contrast, speed lines and focus lines, one accent colour only (coral red)"),
 "S3": ("tech-flavoured anime illustration, deep blue to purple gradient background, neon cyan and "
        "magenta glowing lines, translucent holographic panels, clean anime character art"),
 "S4": ("flat vector illustration for a popular-science explainer, clean geometric shapes, no outline "
        "or a very thin outline, saturated harmonious palette of deep blue, orange, teal and cream, "
        "soft long shadows, rounded friendly characters"),
}
TITLE_CAP = {
 "S0": "Large black hand-lettered main label, the biggest text on the canvas. Each character keeps its natural proportions: never stretch a character taller or wider than it is written.",
 "S1": "Large warm hand-lettered main label, the biggest text on the canvas. Each character keeps its natural proportions: never stretch a character taller or wider than it is written.",
 "S2": "Large bold comic lettering main label with a thick black outline, the biggest text on the canvas. Each character keeps its natural proportions.",
 "S3": "Large glowing neon main label, the biggest text on the canvas. Each character keeps its natural proportions: never stretch a character taller or wider than it is written.",
 "S4": "Large clean bold sans-serif main label, the biggest text on the canvas. Each character keeps its natural proportions.",
}
LABEL_CAP = {
 "S0": "Plain black hand-lettered Chinese text on the bare background, with a thin hand-drawn arrow. Every character keeps its natural square proportions, never stretched. No box, no frame, no icon, no fill.",
 "S1": "Plain warm hand-lettered Chinese text on the bare background, with a thin hand-drawn arrow. Every character keeps its natural square proportions, never stretched. No box, no frame, no icon, no fill.",
 "S2": "Plain bold comic Chinese lettering on the bare background. Every character keeps its natural square proportions, never stretched. No box, no frame, no icon, no fill.",
 "S3": "Plain glowing neon Chinese text on the bare background, with a thin glowing line. Every character keeps its natural square proportions, never stretched. No box, no frame, no icon, no fill.",
 "S4": "Plain clean bold sans-serif Chinese text on the bare background. Every character keeps its natural square proportions, never stretched. No box, no frame, no icon, no fill.",
}
LABEL_CAP_LATIN = {
 "S0": "Plain black hand-lettered English word on the bare background, with a thin hand-drawn arrow. No box, no frame, no icon, no fill.",
 "S1": "Plain warm hand-lettered English word on the bare background, with a thin hand-drawn arrow. No box, no frame, no icon, no fill.",
 "S2": "Plain bold comic English lettering on the bare background. No box, no frame, no icon, no fill.",
 "S3": "Plain glowing neon English word on the bare background, with a thin glowing line. No box, no frame, no icon, no fill.",
 "S4": "Plain clean bold sans-serif English word on the bare background. No box, no frame, no icon, no fill.",
}


def default_scene(illus_type):
    return TYPE_SCENE.get(illus_type, TYPE_SCENE[DEFAULT_SCENE_KEY])


def labels_of(prompt):
    body = prompt.split("注意「Claude」")[0]
    return [s.strip() for s in re.findall(r"「([^」]*)」", body) if s.strip()]


# ---------- 过滤画不出来的标签 ----------
_SECTION_RE = re.compile(
    r"^(第?[一二三四五六七八九十百]+[章节部分步]?[、.．)]?"
    r"|[0-9]+(\.[0-9]+)*[、.．)]?"
    r"|[（(][一二三四五六七八九十0-9]+[)）][、.．]?)$")
_PUNCT_RE = re.compile(r"^[\s、，。；：！？·—…\-–_/\\|~`\"'“”‘’（）()《》<>【】\[\]{}]+$")


def is_drawable(label: str) -> bool:
    s = (label or "").strip()
    return bool(s) and not _PUNCT_RE.match(s) and not _SECTION_RE.match(s)


def filter_labels(labels):
    return [str(s).strip() for s in labels if is_drawable(s)]


def is_pure_latin(s: str) -> bool:
    """纯拉丁（含空格、连字符、数字），用来选 caption。"""
    return bool(re.search(r"[A-Za-z]", s)) and not any(ord(c) > 0x2E80 for c in s)


def units(s: str) -> float:
    """宽度单位：中文 1、拉丁字母/数字 0.55、空格 0.3、连字符 0.35、其它符号 0.6。"""
    u = 0.0
    for c in s:
        if ord(c) > 0x2E80:
            u += 1.0
        elif c == " ":
            u += 0.3
        elif c in "-－—–_":
            u += 0.35
        elif c.isalnum():
            u += 0.55
        else:
            u += 0.6
    return u


def text_w(s: str, k: int) -> int:
    return int(max(units(s), MIN_UNITS) * k * PADW)


def box_h(k: int) -> int:
    return int(k * LH)


# ---------- 长句折行（定稿第 1 条） ----------
def wrap_text(s: str, max_units=WRAP_MAX_UNITS):
    """>max_units 时折两行；优先在标点/空格处断，取最接近一半的断点；无断点硬断。

    返回行列表：1 行或 2 行；总宽 >WRAP_NO_WRAP_UNITS 时返回原样单行。
    """
    s = s.strip()
    if units(s) <= max_units or units(s) > WRAP_NO_WRAP_UNITS:
        return [s]
    half = units(s) / 2.0
    best, best_cost, acc = None, 1e9, 0.0
    for i, c in enumerate(s[:-1]):
        acc += units(c)
        if c in WRAP_BREAKS:
            cost = abs(acc - half)
            if cost < best_cost:
                best, best_cost = i + 1, cost
    if best is None:                      # 没有断点：按单位硬断
        acc, cut = 0.0, len(s)
        for i, c in enumerate(s):
            acc += units(c)
            if acc >= max_units:
                cut = i + 1
                break
        best = cut
    a, b = s[:best].strip(), s[best:].strip()
    if not a or not b:
        return [s]
    return [a, b]


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _pack_rows(items, avail):
    """items=[(text, w, h)]，按可用宽度分行，行高取行内最大。"""
    rows, row, row_w = [], [], 0
    for it in items:
        w = min(it[1], avail)
        need = w if not row else w + GAP_X
        if row and row_w + need > avail:
            rows.append(row)
            row, row_w = [], 0
            need = w
        row.append(it)
        row_w += need
    if row:
        rows.append(row)
    return rows


def _emit_text(s, k, caption, x1, x2, y, lh):
    """把一条文字落成 1 或 2 个 item（按 WRAP_MODE）。"""
    lines = wrap_text(s)
    if len(lines) == 1:
        return [{"label": "text", "caption": caption, "text": s, "bbox": [x1, y, x2, y + lh]}]
    if WRAP_MODE == "split":
        out, yy = [], y
        for ln in lines:
            w = text_w(ln, k)
            out.append({"label": "text", "caption": caption, "text": ln,
                        "bbox": [x1, yy, x1 + w, yy + lh]})
            yy += lh
        return out
    text = "\n".join(lines) if WRAP_MODE == "nl" else s
    return [{"label": "text", "caption": caption, "text": text, "bbox": [x1, y, x2, y + lh * 2]}]


def build_layout(labels, style_key="S0", scene="", carrier=None, W=1360, H=768, wrap_mode=None):
    """生成 MOSS Layout JSON 字符串。接口与 v1 一致，多一个可选 wrap_mode。"""
    global WRAP_MODE
    if wrap_mode:
        WRAP_MODE = wrap_mode
    raw = [str(s).strip() for s in labels if str(s).strip()]
    labels = filter_labels(raw)
    if not labels:
        raise ValueError("文字清单为空（过滤掉不可画标签后）")
    if style_key not in STYLE_EN:
        raise ValueError(f"未知风格 {style_key}")

    has_carrier = any(w in (scene or "").lower() for w in CARRIER_WORDS)
    if has_carrier and not carrier:
        raise ValueError("R3 违反：场景里有屏幕/窗口/卡片等载体，但未提供成品描述 carrier")

    main, rest = labels[0], labels[1:]
    obj_cap = STYLE_EN[style_key] + (", " + scene if scene else "") + (" . " + carrier if carrier else "")

    margin_x = max(int(W * MARGIN_RATIO), 24)      # 68px @1360
    margin_top = max(int(H * MARGIN_RATIO), 24)    # 38px @768
    safe_bottom = int(H * (1 - SAFE_RATIO))        # 599px @768
    title_top = max(TITLE_TOP_MIN, margin_top)
    avail_w = W - 2 * margin_x

    last = None
    for k_label in (K_LABEL, 34, 30, 27, 24, 21):
        k_title = max(int(k_label * 1.2), 30)
        lh = box_h(k_label)
        items = []

        # ---- 主标题 ----
        tlines = wrap_text(main, WRAP_MAX_UNITS)
        t_lh = box_h(k_title)
        t_w = min(max(text_w(ln, k_title) for ln in tlines), avail_w)
        t_h = t_lh * len(tlines)
        t_box = [(W - t_w) // 2, title_top, (W + t_w) // 2, title_top + t_h]
        if len(tlines) == 1:
            items.append({"label": "text", "caption": TITLE_CAP[style_key],
                          "text": main, "bbox": t_box})
        else:
            items += _emit_text(main, k_title, TITLE_CAP[style_key],
                                (W - t_w) // 2, (W + t_w) // 2, title_top, t_lh)

        y_top = title_top + t_h + 14

        # ---- 标签：分侧栏 / 底带 ----
        side_w = int(W * 0.24)
        entries = []
        for s in rest:
            lines = wrap_text(s)
            w = max(text_w(ln, k_label) for ln in lines)
            h = lh * len(lines)
            entries.append((s, w, h))
        wide = [e for e in entries if e[1] > side_w]
        side = [e for e in entries if e[1] <= side_w]

        band_rows, band_h = [], 0
        if wide:
            band_rows = _pack_rows(wide, avail_w)
            band_h = sum(max(it[2] for it in row) + GAP_Y for row in band_rows)
        band_top = safe_bottom - band_h - 8 if band_rows else safe_bottom

        room = band_top - 10 - y_top
        per_col = 0
        y = y_top
        count = 0
        for it in side:
            if y + it[2] > band_top - 10:
                break
            y += it[2] + GAP_Y
            count += 1
        per_col = max(1, count)
        if len(side) > per_col * 2:                 # 放不下的挪到底带
            overflow = side[per_col * 2:]
            side = side[:per_col * 2]
            band_rows = _pack_rows(overflow, avail_w) + band_rows
            band_h = sum(max(it[2] for it in row) + GAP_Y for row in band_rows)
            band_top = safe_bottom - band_h - 8
            per_col = max(1, (band_top - 10 - y_top) // (lh + GAP_Y))

        half = (len(side) + 1) // 2
        for group, is_left in ((side[:half], True), (side[half:], False)):
            y = y_top
            for s, w, h in group:
                cap = LABEL_CAP_LATIN[style_key] if is_pure_latin(s) else LABEL_CAP[style_key]
                x1 = margin_x if is_left else W - margin_x - w
                items += _emit_text(s, k_label, cap, x1, x1 + w, y, lh)
                y += h + GAP_Y

        y = band_top
        for row in band_rows:
            x = margin_x
            rh = max(it[2] for it in row)
            for s, w, h in row:
                cap = LABEL_CAP_LATIN[style_key] if is_pure_latin(s) else LABEL_CAP[style_key]
                items += _emit_text(s, k_label, cap, x, min(x + w, W - margin_x), y, lh)
                x += w + GAP_X
            y += rh + GAP_Y

        # ---- 主体框 ----
        ox1 = margin_x + side_w + 12 if side else int(W * 0.30)
        ox2 = W - margin_x - side_w - 12 if side else int(W * 0.70)
        oy1, oy2 = y_top, band_top - 12
        if oy2 - oy1 < 220:
            oy1 = max(oy1, oy2 - 220)
        obj_item = {"label": "object", "caption": obj_cap, "bbox": [ox1, oy1, ox2, oy2]}

        # ---- 校验：重叠 / 边距 / 安全区 ----
        texts = [it["bbox"] for it in items]
        bad = False
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                if _overlap(texts[i], texts[j]):
                    bad = True
        for b in texts:
            if b[0] < margin_x or b[2] > W - margin_x or b[1] < margin_top or b[3] > safe_bottom:
                bad = True
        if oy2 - oy1 < 160:
            bad = True
        if not bad:
            items.append(obj_item)
            last = _to_moss(items, obj_cap, W, H)
            return last
        last = _to_moss(items + [obj_item], obj_cap, W, H)

    return last


def _to_moss(items, obj_cap, W, H):
    out = []
    for it in items:
        x1, y1, x2, y2 = it["bbox"]
        x1 = _clamp(x1, 0, W - 1); x2 = _clamp(x2, x1 + 1, W)
        y1 = _clamp(y1, 0, H - 1); y2 = _clamp(y2, y1 + 1, H)
        o = dict(it)
        o["bbox"] = [math.floor(y1) - H // 2, math.floor(x1) - W // 2,
                     math.ceil(y2) - H // 2, math.ceil(x2) - W // 2]
        out.append(o)
    return json.dumps({"summary": {"meta": ["illustration"], "canvas": "single", "caption": obj_cap},
                       "items": out, "bbox_coord_space": "abs_centralized",
                       "bbox_format": "yxyx"}, ensure_ascii=False, separators=(",", ":"))


def check_overlap(layout_json, W=1360, H=768, safe_ratio=SAFE_RATIO, check_margin=True):
    """自检：文字框互不重叠、不越界、不落安全区、不越 5% 边距。返回问题列表。"""
    d = json.loads(layout_json)
    safe_bottom = int(H * (1 - safe_ratio))
    margin_x = int(W * MARGIN_RATIO)
    margin_top = int(H * MARGIN_RATIO)
    boxes, bad = [], []
    for it in d["items"]:
        y1, x1, y2, x2 = it["bbox"]
        b = (x1 + W // 2, y1 + H // 2, x2 + W // 2, y2 + H // 2)
        if it["label"] == "text":
            boxes.append((it.get("text"), b))
            if b[3] > safe_bottom:
                bad.append((it.get("text"), f"越过安全区 {b[3]}>{safe_bottom}"))
            if b[0] < 0 or b[2] > W or b[1] < 0:
                bad.append((it.get("text"), "越界"))
            elif check_margin and (b[0] < margin_x or b[2] > W - margin_x or b[1] < margin_top):
                bad.append((it.get("text"), f"越过 5% 边距 {b}"))
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if _overlap(boxes[i][1], boxes[j][1]):
                bad.append((boxes[i][0], boxes[j][0]))
    return bad


if __name__ == "__main__":
    cases = [
        ["codex", "新手教程", "进阶技巧"],
        ["Codex与Claude Code的区别", "核心差异对比"],
        ["相同的地方", "1.1"],
        ["好的skill不是写出来的，是用出来的"],
        ["写完只是开始，真正的考验在后面", "三点"],
        ["没有最好的开发工具，只有最适合的", "GitHub"],
        ["针对不同智能体角色的技能匹配与调整策略"],
        ["Claude code", "Codex", "Anthropic", "OpenAI", "PPT"],
        ["GPT-5", "稳定性"],
        ["T"] + [f"标签{i}" for i in range(1, 9)],
        ["T"] + [f"很长的标签名称{i}号说明" for i in range(1, 9)],
    ]
    for mode in ("flow", "nl", "split"):
        print(f"\n===== WRAP_MODE={mode} =====")
        for lb in cases:
            try:
                j = build_layout(lb, "S0", "one person at a desk with a laptop",
                                 "the laptop screen shows a plain grey window",
                                 W=1360, H=768, wrap_mode=mode)
            except ValueError as e:
                print("  跳过", lb[:6], e); continue
            d = json.loads(j)
            bad = check_overlap(j)
            items = [i for i in d["items"] if i["label"] == "text"]
            shapes = [f'{i["text"][:8]}:{abs(i["bbox"][3]-i["bbox"][1])}x{abs(i["bbox"][2]-i["bbox"][0])}'
                      for i in items]
            print(f"  原始 {len(lb):>2} → 文字项 {len(items):>2}  问题={bad if bad else '无'}")
            print("     ", "  ".join(shapes))
