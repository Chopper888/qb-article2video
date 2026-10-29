# -*- coding: utf-8 -*-
"""shot_director：截图导演层确定性计算（SHOT-M4-P1a 收成）。

从 M3-P6c-序列对齐/gen_m3p6c.py 逐字抽取的确定性计算逻辑（不调任何模型），
外加 M3-P5-导演层收官/run_p5.py 的「章节一致性 + override」规则（A4，自算 image 19 迁组）。

分层契约：
  - 核心函数纯计算：不读写文件、不调 PIL、不 import 模型；需要图片尺寸/文件内容时由调用方传参。
  - 文件读取集中在 load_inputs(paths)，所有路径由 paths 传入，模块内无写死路径。
  - 总入口 build_shots(inputs) -> dict，输出结构等于 m3p6c_data.json。
  - 字级对齐用 pipeline 内部 word_align（C3 迁入），判型现场调 pipeline.layout.classify_and_layout（C1）。
"""
import json
import os
import re
from collections import Counter
from pathlib import Path

from pipeline import word_align
from pipeline.layout import (classify_and_layout, is_scroll_candidate,
                             SCROLL_SEC_PER_SCREEN, SHOT_REGION, CARD_TITLEBAR_H)

# ============ 常量（集中定义，来源注释） ============
FPS = 30
LEAD_IN = 8          # 交接文档 v4 §3.6：入场帧 8，镜头前 8 帧无声
FADE_FRAMES = 10     # 交接文档 v4 §3.6：条目淡入持续 10 帧（渲染层消费，此处集中声明）
MIN_SHOT_FRAMES = 60  # 交接文档 v4 §3.6：任一镜头短于 60 帧(2s)整组退回均分

# 指代截图的词表（与 scene_build.A1_WORDS 保持一致，固定清单）。
# 已补充「比如下图 / 下图这种 / 这张报错信息」三个词；「出现问题的原因」经评估不计入。
A1_WORDS = ["下面这张图", "下面这些图", "这张图", "这些图", "这张截图", "如图", "如下图",
            "看一下这张", "从后台日志", "从日志", "这个报错", "下面这个", "上面这张",
            "下面这两张图", "下面这几张图", "下面这些报错", "下面这张", "下图", "这几张图", "这张报错",
            "比如下图", "下图这种", "这张报错信息"]

SAFE_L = 80          # 画面区左安全边
SAFE_R = 80          # 画面区右安全边
STAGE_Y = 60         # 画面区上边界
STAGE_H = 720        # 画面区高度（1920×1080，底部预留 300 字幕）
RIGHT_EDGE = 1920 - SAFE_R  # = 1840

TITLEBAR_H = 40      # 截图卡标题栏高（交接文档 v4 §3.3 V0）
TITLEBAR_PAD_L = 16  # 标题栏左内边距（交接文档 v4 §3.3）
TITLEBAR_DOTS = 68   # 标题栏三个圆点区宽（交接文档 v4 §3.3）
TITLEBAR_PAD_R = 16  # 标题栏右内边距（交接文档 v4 §3.3）
TITLEBAR_FONT = 22   # 标题栏字号（交接文档 v4 §3.3）

EB_FONT = 28         # 眉标字号
EB_LH = 36           # 眉标行高
TITLE_FONT = 64      # 标题字号
TITLE_LH = 80        # 标题行高
ITEM_FONT = 40       # 条目字号
ITEM_LH = 88         # 条目行高
DOT_W = 10           # 橙点直径
ITEM_GAP = 14        # 橙点与文字间距
FONT_W_FACTOR = 1.02  # 中文字体实测宽度系数，换字体须重校准

GAP_MAX = 200        # 交接文档 v4 §3.3 V1：文字与卡自然间距>200 时整体居中、间距固定 200
MIN_GAP = 64         # 交接文档 v4 §3.3 V0：cardW = min(680×宽/高, 1760 − textW − 64)
V2_SCALE_MAX = 1.5   # 交接文档 v4 §3.3 V2：C 型定位框裁切放大缩放上限 1.5

PAR_TITLE_H = 80     # 并列标题带高
PAR_IMG_TOP = 164    # 并列图顶边
PAR_IMG_H = 616      # 并列图区高
PAR_LABEL_LH = 56    # 并列标签行高
PAR_GAP = 48         # 并列图间距
PAR_MAX_N = 3        # v1.2 规则一：并列最多 3 张图，≥4 张拆为逐图镜头

# SHOT-M5-P2 B2：文字密度 → 每屏最多张数（dense 2 / medium 4 / sparse 4，方案端裁定 sparse 6→4）。
DENSITY_CAP = {"dense": 2, "medium": 4, "sparse": 4}
# SHOT-M5-P2 B4：网格格子间距 + 每张图显示短边下限。
GRID_GAP = 24
GRID_MIN_SHORT = 320
# SHOT-M5-P2 方案端裁定：宽高比 > 2.5 不进网格，按单图镜头处理。
GRID_MAX_ASPECT = 2.5
# SHOT-M5-P3 B1：概述层短边下限放宽为 200px（细节层仍 320px）。
GRID_MIN_SHORT_OVERVIEW = 200
# 概述层每屏最多 4 张。
OVERVIEW_MAX_N = 4
# SHOT-M5-P3 B4：细节层停留时长按密度分档（dense 4s / medium 3s / sparse 2s，上限 6s）。
DETAIL_DUR_FRAMES = {"dense": 120, "medium": 90, "sparse": 60}
DETAIL_DUR_CAP = 180


def grid_rows_cols(n):
    """B4：按屏内张数定行列。2 张→1x2；3~4 张→2x2；5~6 张→2x3。"""
    if n <= 2:
        return 1, 2
    if n <= 4:
        return 2, 2
    return 2, 3


def grid_layout_candidates(n):
    """SHOT-M5-P7 B1：给定屏内张数，枚举候选排法（1×N / N×1 / 2×2 / 2×3）。"""
    cands = [(1, n)]
    if n > 1:
        cands.append((n, 1))
    if 3 <= n <= 4:
        cands.append((2, 2))
    if 5 <= n <= 6:
        cands.append((2, 3))
    return cands


def pick_grid_layout(images, grid_w, grid_h):
    """SHOT-M5-P7 B2/B3：枚举择优。返回 (rows, cols, cell_w, cell_h, disp, min_short)。

    取「显示面积最小的那张图，其面积最大」的排法；面积相同时取行数少的。
    唯一硬约束：每张图显示短边 ≥200px。所有排法都达不到时返回 None（由调用方减少张数重新分屏）。
    """
    n = len(images)
    best = None  # (min_area, rows, cols, cell_w, cell_h, disp, min_short)
    for rows, cols in grid_layout_candidates(n):
        cell_w = (grid_w - GRID_GAP * (cols - 1)) / cols
        cell_h = (grid_h - GRID_GAP * (rows - 1)) / rows
        img_h = cell_h - TITLEBAR_H
        if cell_w <= 0 or img_h <= 0:
            continue
        disp = []
        min_area = None
        min_short = None
        for im in images:
            iw, ih = im["w"], im["h"]
            scale = min(cell_w / iw, img_h / ih)
            dw, dh = iw * scale, ih * scale
            area = dw * dh
            short = min(dw, dh)
            disp.append({"image": im["image"], "w": round(dw, 1), "h": round(dh, 1),
                         "short": round(short, 1)})
            if min_area is None or area < min_area:
                min_area = area
            if min_short is None or short < min_short:
                min_short = short
        if min_short < GRID_MIN_SHORT_OVERVIEW:
            continue  # B3 硬约束不满足
        if best is None or min_area > best[0] or (min_area == best[0] and rows < best[1]):
            best = (min_area, rows, cols, cell_w, cell_h, disp, min_short)
    if best is None:
        return None
    _, rows, cols, cell_w, cell_h, disp, min_short = best
    return rows, cols, cell_w, cell_h, disp, min_short

ARROW_LEN = 80       # 交接文档 v4 §3.5：箭头总长 80
ARROW_SHAFT = 8      # 杆粗 8
ARROW_HEAD = 28      # 三角箭头长 28 宽 28

COLOR_ANNOTATED = "#1B4FBF"  # 深蓝（已有标注时箭头用）
COLOR_CLEAN = "#2F6FEB"      # 强调蓝（无标注时高亮框用）


# ============ 纯辅助函数 ============
def units_of(ch):
    """单字宽度单位：中文/全角 1，ASCII 0.55，空格 0.3。纯函数。"""
    if ch == " ":
        return 0.3
    return 0.55 if ord(ch) < 0x2e80 else 1.0


def units_of_str(s):
    """字符串宽度单位合计。纯函数。"""
    return sum(units_of(ch) for ch in s)


def est_w(text, font):
    """元素估算宽 = unitsOf(文字) × 字号 × FONT_W_FACTOR + 4。纯函数。"""
    return units_of_str(text) * font * FONT_W_FACTOR + 4


def wrap_title_balanced(text, max_w=645):
    """G 步（P6c）：单行 ≤645 保持单行；>645 才均分折两行。纯函数。"""
    if est_w(text, TITLE_FONT) <= max_w:
        return [text]
    n = len(text)
    best = None
    for split in range(1, n):
        l1, l2 = text[:split], text[split:]
        w1, w2 = est_w(l1, TITLE_FONT), est_w(l2, TITLE_FONT)
        if w1 > max_w or w2 > max_w:
            continue
        mw = max(w1, w2)
        if best is None or mw < best[0] or (mw == best[0] and len(l1) > len(best[1])):
            best = (mw, l1, l2)
    return [best[1], best[2]] if best else [text]


def build_step_words():
    """生成「第N步/步骤N」词表（N 一~九或 1~9）。纯函数。"""
    w = []
    for n in "一二三四五六七八九123456789":
        w.append(f"第{n}步")
        w.append(f"步骤{n}")
    return w


STEP_WORDS = build_step_words()


def is_steps_group(text):
    """步骤组判定（P6 收紧版：只认第N步/步骤N，去首先/接着/然后/最后）。纯函数。"""
    t = (text or "").strip()
    t = re.sub(r"^[\s，。、；：！？]+", "", t)
    return any(t.startswith(w) for w in STEP_WORDS)


def norm(s):
    """去非字母数字归一化。纯函数。"""
    return re.sub(r"[^\w]", "", s or "")


def fn_of(e):
    """image_path → basename（%20 解码）。纯函数。"""
    return os.path.basename(e["image_path"]).replace("%20", " ")


def rect_of(x, y, w, h):
    """构造 rect dict。纯函数。"""
    return {"x": round(x, 1), "y": round(y, 1), "w": round(w, 1), "h": round(h, 1)}


def scroll_params(w, h):
    """ST3-P3 B2：竖长截图滚动参数。viewport = 卡内截图区（SHOT_REGION.w × 高），
    速度「每滚过一屏需要几秒」。渲染层据此把这张截图改成滚动显示。"""
    return {
        "img_w": w, "img_h": h,
        "viewport_w": SHOT_REGION["w"],
        "viewport_h": SHOT_REGION["h"] - CARD_TITLEBAR_H,
        "sec_per_screen": SCROLL_SEC_PER_SCREEN,
    }


# ST3-P9：矩形坐标登记时保留 1 位小数，两个小数相加会带出浮点误差
# （89.3 + 661.4 = 750.6999…，而 129.3 + 621.4 = 750.7），恰好贴边的矩形会被误判。
# 容差取登记精度的一半，小于它的差距在画面上不存在。
RECT_EPS = 0.05


def rect_overlap(a, b):
    """矩形重叠判定（贴边不算重叠）。纯函数。"""
    return not (a["x"] + a["w"] <= b["x"] + RECT_EPS or b["x"] + b["w"] <= a["x"] + RECT_EPS or
                a["y"] + a["h"] <= b["y"] + RECT_EPS or b["y"] + b["h"] <= a["y"] + RECT_EPS)


def contains(outer, inner):
    """包含判定（贴边算包含）。纯函数。"""
    return (outer["x"] <= inner["x"] + RECT_EPS and outer["y"] <= inner["y"] + RECT_EPS and
            inner["x"] + inner["w"] <= outer["x"] + outer["w"] + RECT_EPS and
            inner["y"] + inner["h"] <= outer["y"] + outer["h"] + RECT_EPS)


def check_elements(els):
    """登记制自检（六项）：越界 / 高亮箭头越界 / 重叠 / 标题栏单行超宽 /
    标题误折行(单行≤645却折2行) / 侧栏宽≤645。返回 (pass, errors)。纯函数。"""
    errors = []
    by_id = {el["id"]: el for el in els}
    for el in els:
        if el["kind"] in ("highlight", "arrow"):
            continue
        r = el["rect"]
        if not (SAFE_L <= r["x"] + RECT_EPS and r["x"] + r["w"] <= RIGHT_EDGE + RECT_EPS and
                STAGE_Y <= r["y"] + RECT_EPS and r["y"] + r["h"] <= STAGE_Y + STAGE_H + RECT_EPS):
            errors.append(f"越界: {el['id']} rect={r}")
    for el in els:
        if el["kind"] in ("highlight", "arrow"):
            parent = by_id.get(el["parent"])
            if parent is None or not contains(parent["rect"], el["rect"]):
                errors.append(f"高亮/箭头越界: {el['id']} 不在父 {el['parent']} 内")
            # A5: 起止两态都要登记并检查（起始态=放大1.08）
            if el.get("start_rect") is not None and (parent is None or not contains(parent["rect"], el["start_rect"])):
                errors.append(f"高亮起始态越界: {el['id']} start_rect 不在父 {el['parent']} 内")

    def is_ancestor(a, b):
        cur = b
        seen = 0
        while cur["parent"] and seen < 20:
            if cur["parent"] == a["id"]:
                return True
            cur = by_id.get(cur["parent"])
            if cur is None:
                break
            seen += 1
        return False

    for i in range(len(els)):
        for j in range(i + 1, len(els)):
            a, b = els[i], els[j]
            if not rect_overlap(a["rect"], b["rect"]):
                continue
            if a.get("grid_border") or b.get("grid_border"):
                continue  # 概述层高亮红框环绕图，与图重叠属预期（SHOT-M5-P5）
            if is_ancestor(a, b) and contains(a["rect"], b["rect"]):
                continue
            if is_ancestor(b, a) and contains(b["rect"], a["rect"]):
                continue
            errors.append(f"重叠: {a['id']} 与 {b['id']}")
    for el in els:
        if el["kind"] == "titlebar-text":
            card = by_id.get("card")
            if card is not None:
                avail = card["rect"]["w"] - 100
                if el["rect"]["w"] > avail:
                    errors.append(f"单行超宽: titlebar-text w={el['rect']['w']:.1f} > 可用 {avail:.1f}")
    for el in els:
        if el["kind"] == "title" and el.get("lines", 1) > 2:
            errors.append(f"标题超2行: {el['id']}")
        if el["kind"] == "title" and el.get("lines", 1) == 2:
            full = "".join(el["text"]) if isinstance(el["text"], list) else el["text"]
            if est_w(full, TITLE_FONT) <= 645:
                errors.append(f"标题误折行: {el['id']} 单行宽 {est_w(full, TITLE_FONT):.1f} ≤645 却折两行")
    if "card" in by_id:
        sidebar_w = max((el["rect"]["w"] for el in els if el["parent"] is None and el["kind"] not in ("card", "image", "titlebar")), default=0)
        if sidebar_w > 645:
            errors.append(f"侧栏超宽: {sidebar_w:.1f} > 645")
    return (len(errors) == 0), errors


# ============ A4：章节一致性 + override（自 run_p5.py A 步，确定性规则） ============
# C2：严格词表只保留一份（STEP_WORDS，第N步/步骤N），is_steps_group_p5 已并入 is_steps_group。


def ordinal_num(before):
    """从 before 上下文提取步骤序号（1-9），无则 None。纯函数。

    步骤序号词统一由 STEP_WORDS 派生（唯一严格词表），避免重复定义。
    """
    m = re.match(r"^\s*(\d+)[\.、．]\s*", before or "")
    if m:
        return int(m.group(1))
    t = (before or "").strip()
    for i, w in enumerate(STEP_WORDS):
        if t.startswith(w):
            return i // 2 % 9 + 1
    return None


def compute_mapping_override(im, body):
    """A4：章节一致性校验 + 分组修正（自 run_p5.py A 步，确定性，不调模型）。

    返回 (override, degraded)。override = {fn: {"from_group","to_group","basis"}}。
    """
    body_sorted = sorted(body, key=lambda g: g["paragraph_index"])
    seqs = []
    cur = []
    for g in body_sorted:
        if is_steps_group(g["group_text"]):
            cur.append(g["group_id"])
        else:
            if cur:
                seqs.append(cur)
                cur = []
    if cur:
        seqs.append(cur)
    step_pos = {}
    for si, seq in enumerate(seqs):
        for pos, gid in enumerate(seq, 1):
            step_pos[gid] = (si, pos)

    def seq_chapter(seq):
        nhs = []
        for gid in seq:
            for e in im["images"]:
                if e["mapped_group_id"] == gid:
                    nhs.append(e["nearest_heading"].replace("\\", ""))
        if not nhs:
            return ""
        return Counter(nhs).most_common(1)[0][0]

    seq_chapters = {tuple(seq): seq_chapter(seq) for seq in seqs}

    img_by_fn = {fn_of(e): e for e in im["images"]}
    g_imgs = {}
    for e in im["images"]:
        g_imgs.setdefault(e["mapped_group_id"], []).append(fn_of(e))

    consistency = []
    override = {}
    degraded = []
    for e in im["images"]:
        fn = fn_of(e)
        gid = e["mapped_group_id"]
        nh = e["nearest_heading"].replace("\\", "")
        si, _ = step_pos.get(gid, (None, None))
        inconsistent = False
        reason = ""
        if si is not None:
            chap = seq_chapters.get(tuple(seqs[si]), "")
            if chap and nh != chap:
                inconsistent = True
                reason = f"nearest_heading={nh} 与所属步骤序列章节={chap} 不同"
        consistency.append({"image": fn, "group": gid, "nearest_heading": nh,
                            "inconsistent": inconsistent, "reason": reason})

    inconsistent_imgs = [c for c in consistency if c["inconsistent"]]
    for c in inconsistent_imgs:
        fn = c["image"]
        gid = c["group"]
        nh = c["nearest_heading"]
        e = img_by_fn[fn]
        num = ordinal_num(e.get("before"))
        target_seq = None
        for seq in seqs:
            if seq_chapters.get(tuple(seq), "") == nh:
                target_seq = seq
                break
        candidates = []
        if target_seq and num:
            if 1 <= num <= len(target_seq):
                g_candidate = target_seq[num - 1]
                if g_candidate not in g_imgs:
                    candidates.append(g_candidate)
        if len(candidates) == 1:
            override[fn] = {"from_group": gid, "to_group": candidates[0],
                            "basis": f"nearest_heading={nh}，before 序号={num}，{nh} 序列第{num}步={candidates[0]} 且无截图，唯一命中"}
        else:
            degraded.append({"image": fn, "group": gid, "nearest_heading": nh,
                             "candidates": candidates, "reason": "无命中或多个候选"})
    return override, degraded


def compute_b3_override(im, gr, sent, g_narr):
    """SHOT-M5-P1 B3：截图组 <60 帧且指代句落在相邻组（前/后一组）时并入该相邻组。

    纯计算，与 build_shots 内部 B3 逻辑逐字一致（供 shot_ai 图句匹配复用同一有效分组）。
    返回 {fn: target_gid}。
    """
    body = [g for g in gr["groups"] if not g.get("starts_with_bracket_title")]
    align = word_align.align_groups(sent, [{"group_id": g["group_id"], "text": g["group_text"]} for g in body])
    g_win = {r["group_id"]: (round(r["start_time"] * FPS), round(r["end_time"] * FPS)) for r in align}
    all_gids = [g["group_id"] for g in gr["groups"]]
    prev_gid, next_gid = {}, {}
    for i, gid in enumerate(all_gids):
        if i > 0:
            prev_gid[gid] = all_gids[i - 1]
        if i + 1 < len(all_gids):
            next_gid[gid] = all_gids[i + 1]

    def _has_a1(text):
        return any(w in (text or "") for w in A1_WORDS)

    g_imgs = {}
    for e in im["images"]:
        g_imgs.setdefault(e["mapped_group_id"], []).append(fn_of(e))
    b3_override = {}
    for gid, fns in g_imgs.items():
        ws, we = g_win.get(gid, (0, 0))
        if we - ws >= MIN_SHOT_FRAMES:
            continue
        if _has_a1(g_narr.get(gid, "")):
            continue
        target = None
        pg, ng = prev_gid.get(gid), next_gid.get(gid)
        if pg and _has_a1(g_narr.get(pg, "")):
            target = pg
        elif ng and _has_a1(g_narr.get(ng, "")):
            target = ng
        if target:
            for fn in fns:
                b3_override[fn] = target
    return b3_override


# ============ 加载函数（读文件 + PIL 读尺寸，路径全部来自 paths） ============
def load_inputs(paths):
    """读取全部输入文件。paths 为 dict，键见下；模块内无写死路径。

    paths 需要的键：
      image_map, groups_raw, direction, sentences, subtitle_lines, bc, f,
      parallel_v2, annotations, article_md, image_dir
    """
    def _load_json(p):
        return json.loads(Path(p).read_text(encoding="utf-8"))

    from PIL import Image as PILImage

    im = _load_json(paths["image_map"])
    gr = _load_json(paths["groups_raw"])
    direction = _load_json(paths["direction"])
    sent = _load_json(paths["sentences"])
    subtitle_lines = _load_json(paths["subtitle_lines"])
    bc = _load_json(paths["bc"])
    f = _load_json(paths["f"])
    parallel_v2 = _load_json(paths["parallel_v2"])
    annotations = _load_json(paths["annotations"])
    article_lines = Path(paths["article_md"]).read_text(encoding="utf-8").splitlines()

    img_dir = Path(paths["image_dir"])
    image_sizes = {}
    for e in im["images"]:
        fn = fn_of(e)
        image_sizes[fn] = PILImage.open(img_dir / fn).size

    return {
        "image_map": im,
        "groups_raw": gr,
        "direction": direction,
        "sentences": sent,
        "subtitle_lines": subtitle_lines,
        "bc": bc,
        "f": f,
        "parallel_v2": parallel_v2,
        "annotations": annotations,
        "article_lines": article_lines,
        "image_sizes": image_sizes,
    }


# ============ 总入口 ============
def build_shots(inputs, lead_in=LEAD_IN, include_placeholders=False):
    """总入口：inputs -> dict（结构等于 m3p6c_data.json）。字级对齐用 pipeline 内部 word_align。

    lead_in: 入场帧（默认 8，金样行为不变）。正式出片传 0，入场动画与口播同时开始。
    include_placeholders: 默认 False；True 时为无图步骤（如 G36/G43）生成占位镜头（复用序列上一个有图镜头）。
    """

    im = inputs["image_map"]
    gr = inputs["groups_raw"]
    direction = inputs["direction"]
    sent = inputs["sentences"]
    subtitle_lines_abs = inputs["subtitle_lines"]
    BC = inputs["bc"]
    F = inputs["f"]
    parallel_v2_raw = inputs["parallel_v2"]
    ann = inputs["annotations"]
    image_sizes = inputs["image_sizes"]
    article_lines = inputs["article_lines"]
    density = inputs.get("density", {})
    matches = inputs.get("matches", {})
    matches_v2 = inputs.get("matches_v2", {})
    labels = inputs.get("labels", {})
    chapter_of_gid = inputs.get("chapter_of_gid", {})

    parallel_v2 = {gid: v.get("parallel") for gid, v in parallel_v2_raw.items()}

    body = [g for g in gr["groups"] if not g.get("starts_with_bracket_title")]
    g_narr = {g["group_id"]: g["group_text"] for g in body}
    groups = [{"group_id": g["group_id"], "text": g["group_text"]} for g in body]
    align = word_align.align_groups(sent, groups)
    g_win = {r["group_id"]: (round(r["start_time"] * FPS), round(r["end_time"] * FPS)) for r in align}

    flow = []
    for s in sent:
        for w in s.get("words", []):
            fr = round(w.get("startTime", 0) * FPS)
            for ch in w.get("word", ""):
                flow.append((ch, fr))
    norm_flow = [(c, f) for c, f in flow if re.match(r"[\w]", c)]
    norm_text = "".join(c for c, _ in norm_flow)

    def anchor_frame_after(a, start_idx):
        a = re.sub(r"[^\w]", "", a or "")
        if not a:
            return None, None
        i = norm_text.find(a, start_idx)
        if i < 0:
            return None, None
        return i, norm_flow[i][1]

    # SHOT-M5-P3 A：把图句匹配的锚句换算成绝对帧（供细节层排序/起始帧 + A3 报告）
    g_narr_norm = {gid: norm(g_narr[gid]) for gid in g_narr}
    g_start = {gid: norm_text.find(nn) for gid, nn in g_narr_norm.items()}
    for gid, m_info in matches.items():
        if not isinstance(m_info, dict):
            continue
        anchors = {}
        if m_info.get("passed"):
            nn = g_narr_norm.get(gid, "")
            gstart = g_start.get(gid, -1)
            for m in m_info.get("matches", []):
                sent = m.get("sentence")
                fn = m.get("image")
                if not sent:
                    anchors[fn] = None
                    continue
                a = norm(sent)
                abs_i = -1
                if a:
                    if gstart >= 0:
                        i = nn.find(a)
                        if i >= 0:
                            abs_i = gstart + i
                    if abs_i < 0:
                        abs_i = norm_text.find(a)  # 兜底：组窗口对齐失败（ASCII 归一化）时全局直查
                if 0 <= abs_i < len(norm_flow):
                    anchors[fn] = norm_flow[abs_i][1]
                else:
                    anchors[fn] = None
        m_info["anchors"] = anchors

    headings = []
    for i, ln in enumerate(article_lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).replace("\\", "").strip()))

    def parent_of(nearest):
        tlvl = tline = None
        for i, lvl, txt in headings:
            if txt == nearest.replace("\\", ""):
                tlvl, tline = lvl, i
                break
        if tlvl is None:
            return ""
        p = ""
        for i, lvl, txt in headings:
            if i < tline and lvl < tlvl:
                p = txt
        return p

    # A4：override 自算
    override, _degraded = compute_mapping_override(im, body)

    g_imgs = {}
    for e in im["images"]:
        g_imgs.setdefault(e["mapped_group_id"], []).append(fn_of(e))
    img_by_fn = {fn_of(e): e for e in im["images"]}

    # B3（SHOT-M5-P1）：截图组时长 <60 帧且指代句落在相邻组（前/后一组）时，并入该相邻组。
    # 只改分组（fn -> 相邻组），不重跑模型；多图切分由下方 cut_group 按现有规则处理。
    all_gids = [g["group_id"] for g in gr["groups"]]
    prev_gid = {}
    next_gid = {}
    for i, gid in enumerate(all_gids):
        if i > 0:
            prev_gid[gid] = all_gids[i - 1]
        if i + 1 < len(all_gids):
            next_gid[gid] = all_gids[i + 1]

    def _has_a1(text):
        return any(w in (text or "") for w in A1_WORDS)

    b3_override = {}
    for gid, fns in g_imgs.items():
        ws, we = g_win.get(gid, (0, 0))
        if we - ws >= MIN_SHOT_FRAMES:
            continue  # 时长充足，不并入
        if _has_a1(g_narr.get(gid, "")):
            continue  # 指代句在本组，不并入
        target = None
        pg = prev_gid.get(gid)
        ng = next_gid.get(gid)
        if pg and _has_a1(g_narr.get(pg, "")):
            target = pg
        elif ng and _has_a1(g_narr.get(ng, "")):
            target = ng
        if target:
            for fn in fns:
                b3_override[fn] = target

    # SHOT-M5-P4 C：v2 锚词帧 + 跨组搬运（图跟着口播走）
    transfer = {}
    transfer_voided = []
    anchor_frame_of_fn = {}
    if matches_v2:
        chapter_passed = matches_v2.get("chapter_passed", {})

        def group_at_frame(frame):
            for gid, (ws, we) in g_win.items():
                if ws <= frame < we:
                    return gid
            return None

        # 章时间范围：锚词命中约束在本章内（避免同名锚词命中到别的章）
        chapter_range = {}
        for gid, chap in chapter_of_gid.items():
            if chap and gid in g_win:
                ws, we = g_win[gid]
                if chap not in chapter_range:
                    chapter_range[chap] = [ws, we]
                else:
                    chapter_range[chap][0] = min(chapter_range[chap][0], ws)
                    chapter_range[chap][1] = max(chapter_range[chap][1], we)

        def find_anchor_frame(anchor, sentence, chap, base_gid):
            """返回 (frame, is_cross_chapter)。frame=None 表示锚词在全片口播里找不到。"""
            lo, hi = chapter_range.get(chap, (0, float("inf")))
            bw = g_win.get(base_gid)
            def in_range(f):
                return lo <= f <= hi
            # 1) 句子优先（更长更唯一），且必须在本章内
            if sentence:
                sn = norm(sentence)
                si = norm_text.find(sn)
                if si >= 0:
                    an = norm(anchor)
                    ai = sn.find(an)
                    if ai >= 0:
                        abs_i = si + ai
                        if 0 <= abs_i < len(norm_flow):
                            f = norm_flow[abs_i][1]
                            if in_range(f):
                                return f, False
            # 2) 锚词直接搜（章内多命中时选最接近原组的）
            an = norm(anchor)
            if not an:
                return None, False
            best = None
            pos = 0
            while True:
                ai = norm_text.find(an, pos)
                if ai < 0:
                    break
                f = norm_flow[ai][1]
                if in_range(f):
                    if best is None:
                        best = f
                    elif bw is not None:
                        center = (bw[0] + bw[1]) / 2
                        if abs(f - center) < abs(best - center):
                            best = f
                pos = ai + 1
            if best is not None:
                return best, False
            # 3) 章内找不到，但全片能找到 → 跨章
            ai = norm_text.find(an)
            if ai >= 0 and ai < len(norm_flow):
                return norm_flow[ai][1], True
            return None, False

        for fn, m in matches_v2.get("matches", {}).items():
            if not m.get("anchor"):
                continue
            chap = m.get("chapter")
            if not chapter_passed.get(chap, False):
                continue
            base_gid = b3_override.get(fn, override.get(fn, {}).get("to_group", img_by_fn[fn]["mapped_group_id"]))
            af, cross = find_anchor_frame(m["anchor"], m.get("sentence"), chap, base_gid)
            if af is None:
                continue
            if cross:
                transfer_voided.append({"image": fn, "from": base_gid, "reason": "跨章搬运作废（锚词在别的章）"})
                continue
            anchor_frame_of_fn[fn] = af
            # SHOT-M5-P6 二：宽图（宽高比 > 2.5）不参与搬运，留在原组单独成镜
            w, h = image_sizes[fn]
            if w / h > GRID_MAX_ASPECT:
                continue
            agid = group_at_frame(af)
            if agid is None:
                continue
            if agid == base_gid:
                continue
            # C3：搬运距离不得跨章（兜底）
            if (chapter_of_gid.get(agid) and chapter_of_gid.get(base_gid)
                    and chapter_of_gid[agid] != chapter_of_gid[base_gid]):
                transfer_voided.append({"image": fn, "from": base_gid, "to": agid, "reason": "跨章搬运作废"})
                continue
            transfer[fn] = agid

    def base_effective(fn):
        if fn in transfer:
            return transfer[fn]
        if fn in b3_override:
            return b3_override[fn]
        return override.get(fn, {}).get("to_group", img_by_fn[fn]["mapped_group_id"])

    # SHOT-M5-P6 C：把搬散的图重新合并（同一原始组的网格可达图被拆到相邻组 → 合并到较晚组概述层）
    merge = {}
    merge_report = []
    orig_by_gid = {}
    for e in im["images"]:
        orig_by_gid.setdefault(img_by_fn[fn_of(e)]["mapped_group_id"], []).append(fn_of(e))

    def _is_gridable(fn):
        w, h = image_sizes[fn]
        return classify_and_layout(w, h)["kind"] != "A"

    def _g_start(gid):
        return g_win.get(gid, (0, 0))[0]

    for orig, fns in orig_by_gid.items():
        if len(fns) < 2:
            continue
        grid_eff = {fn: base_effective(fn) for fn in fns if _is_gridable(fn)}
        if len(grid_eff) < 2:
            continue
        eff_groups = sorted(set(grid_eff.values()), key=_g_start)
        stayed = [fn for fn, e in grid_eff.items() if e == orig]
        if not stayed:
            continue
        moved_groups = [g for g in eff_groups if g != orig]
        if not moved_groups:
            continue
        target = moved_groups[0]  # 起点最早的搬走组（与原组相邻）
        if _g_start(target) - _g_start(orig) > 15 * FPS:
            continue
        if (chapter_of_gid.get(target) and chapter_of_gid.get(orig)
                and chapter_of_gid[target] != chapter_of_gid[orig]):
            continue
        for fn in stayed:
            merge[fn] = target
            merge_report.append({"image": fn, "from": orig, "to": target, "reason": "搬散重合并（相邻组）"})

    def effective_group(fn):
        if fn in merge:
            return merge[fn]
        return base_effective(fn)

    g_imgs_eff = {}
    for e in im["images"]:
        g_imgs_eff.setdefault(effective_group(fn_of(e)), []).append(fn_of(e))

    # 步骤序列聚合（文章顺序）
    body_sorted = sorted(body, key=lambda g: g["paragraph_index"])
    seqs = []
    cur = []
    for g in body_sorted:
        if is_steps_group(g["group_text"]):
            cur.append(g["group_id"])
        else:
            if cur:
                seqs.append(cur)
                cur = []
    if cur:
        seqs.append(cur)
    step_pos = {}
    for si, seq in enumerate(seqs):
        for pos, gid in enumerate(seq, 1):
            step_pos[gid] = (si, pos)

    # 镜头类型判定（parallel → steps → ambient → detail）
    rows = []
    for d in direction:
        fn = d["image_ref"]
        gid_orig = d["mapped_group_id"]
        gid = effective_group(fn)
        # C1：判型现场计算，不再读 m3p3_direction.json 里的 layout.kind
        kind = classify_and_layout(d["source_image"]["width"], d["source_image"]["height"])["kind"]
        narr = g_narr.get(gid, "")
        n_imgs = len(g_imgs_eff.get(gid, []))
        # v1.2 规则一：并列最多 PAR_MAX_N 张；≥4 张拆为逐图镜头（走下方 cut_group）
        if 2 <= n_imgs <= PAR_MAX_N and parallel_v2.get(gid):
            typ = "parallel"
        elif is_steps_group(narr):
            typ = "steps"
        elif kind == "A":
            typ = "ambient"
        else:
            typ = "detail"
        rows.append({"image": fn, "group": gid, "group_orig": gid_orig, "overridden": gid != gid_orig,
                     "type": typ, "kind": kind})

    parallel_groups = {r["group"] for r in rows if r["type"] == "parallel"}

    # v1.2 规则一：并列图数超过 PAR_MAX_N 被拆开的组，记入人工清单
    parallel_split_manual = []
    for gid_orig in g_imgs_eff:
        if len(g_imgs_eff[gid_orig]) > PAR_MAX_N and parallel_v2.get(gid_orig):
            parallel_split_manual.append({"gid": gid_orig, "reason": "并列图数超过 3，已拆为逐图镜头"})

    # 多图切分（SHOT-M5-P2：非并列多图组走网格，不再逐图切分）
    def group_textW(gid):
        for d in direction:
            if effective_group(d["image_ref"]) == gid:
                sb = d["sidebar"]
                eb = (sb or {}).get("eyebrow", "").replace("\\", "")
                t = (sb or {}).get("title", "") if sb else ""
                its = (sb or {}).get("items", []) if sb else []
                tw = est_w(eb, EB_FONT)
                if t:
                    tw = max(tw, max(est_w(l, TITLE_FONT) for l in wrap_title_balanced(t)))
                for it in its:
                    tw = max(tw, est_w(it["text"], ITEM_FONT) + DOT_W + ITEM_GAP)
                return tw
        return 0

    def grid_short_edge_ok(fns, n, grid_w, min_short=GRID_MIN_SHORT):
        rows, cols = grid_rows_cols(n)
        cell_h = (STAGE_H - GRID_GAP * (rows - 1)) / rows - TITLEBAR_H
        cell_w = (grid_w - GRID_GAP * (cols - 1)) / cols
        for fn in fns:
            iw, ih = image_sizes[fn]
            scale = min(cell_w / iw, cell_h / ih)
            if min(iw, ih) * scale < min_short:
                return False
        return True

    def split_evenly(G, k):
        """把 G 均分成 k 屏，每屏 ≤ OVERVIEW_MAX_N。返回 [[fn, ...], ...]。"""
        G = G[:k * OVERVIEW_MAX_N]
        if not G:
            return []
        base = len(G) // k
        extra = len(G) % k
        screens = []
        idx = 0
        for i in range(k):
            size = base + (1 if i < extra else 0)
            screens.append(G[idx:idx + size])
            idx += size
        return screens

    def cut_group(gid):
        fns = g_imgs_eff.get(gid, [])
        ws, we = g_win.get(gid, (0, 0))
        if len(fns) < 2:
            return None
        total = we - ws
        grid_w = RIGHT_EDGE - SAFE_L - group_textW(gid) - GAP_MAX
        # B1: 概述层 grid-able——短边 ≥200（宽高比 >2.5 的图照常收录，不再单图成镜）
        def grid_able(fn):
            return grid_short_edge_ok([fn], 2, grid_w, GRID_MIN_SHORT_OVERVIEW)
        G = [fn for fn in fns if grid_able(fn)]
        N = [fn for fn in fns if fn not in G]

        # B2/C1: 细节层——v2 锚词（P4），镜头起点 = 锚词所在帧
        anchor_fns = [fn for fn in fns if fn in anchor_frame_of_fn]
        anchor_fns.sort(key=lambda fn: anchor_frame_of_fn[fn])

        # B2: 细节层短边 ≥320px，铺不满的（极端宽图）不进细节层
        def detail_short_ok(fn):
            w, h = image_sizes[fn]
            dispW = 680 * (w / h)
            available = RIGHT_EDGE - SAFE_L - group_textW(gid) - MIN_GAP
            cardW = min(dispW, available)
            img_h = 680 if cardW >= dispW else cardW * (h / w)
            return min(cardW, img_h) >= GRID_MIN_SHORT

        def detail_dur(fn):
            base = DETAIL_DUR_FRAMES.get(density.get(fn, "dense"), 60)
            return min(base, DETAIL_DUR_CAP)

        if len(G) >= 2:
            wide_ambient = list(N)
            # 细节候选（网格可达 + 锚词 + 细节短边达标），按锚词先后（稳定排序，同锚词保持原文顺序）
            detail_candidates = [fn for fn in anchor_fns if fn in G and detail_short_ok(fn)]
            n_dc = len(detail_candidates)

            def _greedy_detail(start_from):
                det = []
                prev_end = start_from
                for fn in detail_candidates:
                    af = anchor_frame_of_fn[fn]
                    d = detail_dur(fn)
                    start = max(af, prev_end)
                    if start + d > we:
                        break
                    # 同句均分：锚词帧 = 实际起点（同锚词顺延后的分片位置）
                    det.append({"image": fn, "anchor_frame": start, "start": start, "dur": d})
                    prev_end = start + d
                return det

            def _build_overview():
                k = max(1, (len(G) + OVERVIEW_MAX_N - 1) // OVERVIEW_MAX_N)
                screens = split_evenly(G, k)
                while k > 1 and k * 60 > total:
                    k -= 1
                    screens = split_evenly(G, k)
                on_screen = [fn for scr in screens for fn in scr]
                drop = [fn for fn in G if fn not in on_screen]
                ov = [{"images": scr, "start": ws + i * 60, "dur": 60} for i, scr in enumerate(screens)]
                return ov, drop, screens

            # SHOT-M5-P7 C：按时间自动分档。先贪心放细节，全放得下→C1；否则概述层(+细节)。
            det_full = _greedy_detail(ws)
            if n_dc >= 2 and len(det_full) >= n_dc:
                return {"mode": "two_layer", "overview": [], "detail": det_full, "drop": [], "grid_w": grid_w,
                        "extreme": list(N), "wide_ambient": wide_ambient,
                        "detail_dropped": [], "detail_no_shot": [],
                        "overview_highlights": [], "highlight_dropped": []}

            # C2/C3：概述层（+ 细节）。细节从概述层之后贪心放；放不下的留在概述层（红框带过）。
            ov, drop, screens = _build_overview()
            on_screen_set = set(fn for scr in screens for fn in scr)
            ov_end = ws + len(screens) * 60
            det = _greedy_detail(ov_end)
            det_pick_set = set(x["image"] for x in det)
            detail_no_shot = [{"image": fn, "reason": "组时长不足，留在概述层不单独出细节镜头"}
                              for fn in detail_candidates if fn not in det_pick_set]

            hl_fns = [fn for fn in anchor_fns if fn in on_screen_set and fn not in det_pick_set]

            # SHOT-M5-P7 D：高亮顺序按锚词先后，同句按原文顺序（stable 排序已保证）；每屏均分，单张 ≥1.5s
            hl_schedule = []
            hl_dropped = []
            for scr in screens:
                scr_hl = [fn for fn in hl_fns if fn in scr]
                if not scr_hl:
                    continue
                per = 60 / len(scr_hl)
                if per >= 45:
                    for i, fn in enumerate(scr_hl):
                        hl_schedule.append((fn, round(i * per), round(per)))
                else:
                    max_n = int(60 // 45)
                    for i, fn in enumerate(scr_hl[:max_n]):
                        hl_schedule.append((fn, i * 45, 45))
                    hl_dropped.extend({"image": fn, "reason": "概述层时长均分不足1.5s，未高亮"} for fn in scr_hl[max_n:])
            return {"mode": "two_layer", "overview": ov, "detail": det, "drop": drop, "grid_w": grid_w,
                    "extreme": list(N), "wide_ambient": wide_ambient,
                    "detail_dropped": [], "detail_no_shot": detail_no_shot,
                    "overview_highlights": hl_schedule, "highlight_dropped": hl_dropped}
        # 网格可达图不足 2 张 → 旧的逐图切分（内容锚点 / 均分 / 单镜头兜底）
        even = [(fn, round(ws + (we - ws) * i / len(fns)), round(ws + (we - ws) * (i + 1) / len(fns))) for i, fn in enumerate(fns)]
        anchors_before = [anchor_frame_after(img_by_fn[fn]["before"], 0)[1] for fn in fns]
        if any(a is None for a in anchors_before) or any(anchors_before[i] > anchors_before[i + 1] for i in range(len(anchors_before) - 1)):
            if any(e - s < MIN_SHOT_FRAMES for _, s, e in even):
                return {"mode": "single_shot_fallback", "keep": fns[0], "drop": fns[1:]}
            return {"mode": "split_fallback_even"}
        bounds = [(fn, anchors_before[i], anchors_before[i + 1] if i + 1 < len(fns) else we) for i, fn in enumerate(fns)]
        if any(e - s < MIN_SHOT_FRAMES for _, s, e in bounds):
            if any(e - s < MIN_SHOT_FRAMES for _, s, e in even):
                return {"mode": "single_shot_fallback", "keep": fns[0], "drop": fns[1:]}
            return {"mode": "split_fallback_even"}
        return {"mode": "content", "bounds": [{"image": fn, "start": s, "end": e, "dur": e - s} for fn, s, e in bounds]}

    cut_results = {}
    for gid in g_imgs_eff:
        if len(g_imgs_eff[gid]) >= 2 and gid not in parallel_groups:
            cut_results[gid] = cut_group(gid)
    two_layer_groups = {gid for gid, cr in cut_results.items() if cr and cr["mode"] == "two_layer"}
    single_shot_drop = []
    single_shot_manual = []
    detail_dropped_manual = []
    detail_no_shot_manual = []
    overview_highlight_manual = []
    highlight_dropped_manual = []
    for gid, cr in cut_results.items():
        if not cr:
            continue
        if cr["mode"] == "two_layer":
            extreme = set(cr.get("extreme", []))
            for fn in cr.get("drop", []):
                if fn in extreme:
                    reason = "短边不足 200px，概述层未上屏"
                else:
                    reason = "组时长不足，未上屏"
                single_shot_manual.append({"image": fn, "group": gid, "reason": reason})
            single_shot_drop.extend(cr.get("drop", []))
            for dd in cr.get("detail_dropped", []):
                detail_dropped_manual.append({"image": dd["image"], "group": gid, "reason": dd["reason"]})
            for ns in cr.get("detail_no_shot", []):
                detail_no_shot_manual.append({"image": ns["image"], "group": gid, "reason": ns["reason"]})
            for fn, rel_start, dur in cr.get("overview_highlights", []):
                overview_highlight_manual.append({"image": fn, "group": gid,
                                                  "reason": "锚词落在概述层内，改为概述层高亮"})
            for hd in cr.get("highlight_dropped", []):
                highlight_dropped_manual.append({"image": hd["image"], "group": gid, "reason": hd["reason"]})
        elif cr["mode"] == "single_shot_fallback":
            single_shot_drop.extend(cr["drop"])
            for fn in cr["drop"]:
                single_shot_manual.append({"image": fn, "group": gid, "reason": "组时长不足，未上屏"})

    def timing_for(fn):
        gid = effective_group(fn)
        ws, we = g_win.get(gid, (0, 0))
        if gid in parallel_groups:
            return ws, we
        fns = g_imgs_eff.get(gid, [])
        if len(fns) < 2:
            return ws, we
        cr = cut_results.get(gid)
        if cr and cr["mode"] == "two_layer":
            return (None, None)  # 两层镜头单独生成，不在逐图循环里
        if cr and cr["mode"] == "content":
            for b in cr["bounds"]:
                if b["image"] == fn:
                    return b["start"], b["end"]
        if cr and cr["mode"] == "single_shot_fallback":
            return (ws, we) if fn == cr["keep"] else (None, None)
        pos = next(i for i, x in enumerate(fns) if x == fn)
        seg = (we - ws) / len(fns)
        return round(ws + pos * seg), round(ws + (pos + 1) * seg)

    def is_annotated(fn):
        r = ann.get(fn, "")
        return r.upper().startswith("ANNOTATED")

    F_by_fn = {x["filename"]: x for x in F}

    def get_hl_box(fn):
        fr = F_by_fn.get(fn)
        if fr and fr.get("readback") == "match" and "box" in fr:
            return fr["box"], fr.get("target", "")
        return None, ""

    def compute_arrow(T, img_rect):
        cx, cy = T["x"] + T["w"] / 2, T["y"] + T["h"] / 2
        cands = [
            ("left", rect_of(T["x"] - 8 - ARROW_LEN, cy - ARROW_HEAD / 2, ARROW_LEN, ARROW_HEAD)),
            ("right", rect_of(T["x"] + T["w"] + 8, cy - ARROW_HEAD / 2, ARROW_LEN, ARROW_HEAD)),
            ("up", rect_of(cx - ARROW_HEAD / 2, T["y"] - 8 - ARROW_LEN, ARROW_HEAD, ARROW_LEN)),
            ("down", rect_of(cx - ARROW_HEAD / 2, T["y"] + T["h"] + 8, ARROW_HEAD, ARROW_LEN)),
        ]
        for direction, r in cands:
            if (r["x"] - img_rect["x"] >= 4 and r["y"] - img_rect["y"] >= 4 and
                    img_rect["x"] + img_rect["w"] - (r["x"] + r["w"]) >= 4 and
                    img_rect["y"] + img_rect["h"] - (r["y"] + r["h"]) >= 4 and
                    not (r["x"] < T["x"] + T["w"] and r["x"] + r["w"] > T["x"] and
                         r["y"] < T["y"] + T["h"] and r["y"] + r["h"] > T["y"])):
                return {"id": "arrow", "kind": "arrow", "rect": r, "parent": "image",
                        "direction": direction, "color": COLOR_ANNOTATED, "tip": (T["x"], T["y"], T["w"], T["h"])}
        return None

    def sidebar_textW(shot):
        eb = shot.get("eyebrow", "")
        tw = est_w(eb, EB_FONT)
        if shot["type"] == "detail":
            title = shot.get("title", "")
            rows = shot.get("items", [])
        else:
            title = shot.get("seq_title", "")
            rows = shot.get("steps_acc", [])
        if title:
            lines = wrap_title_balanced(title)
            tw = max(tw, max(est_w(l, TITLE_FONT) for l in lines))
        for row in rows:
            txt = row["text"] if shot["type"] == "detail" else f"第{row['no']}步 {row['text']}"
            tw = max(tw, est_w(txt, ITEM_FONT) + DOT_W + ITEM_GAP)
        return tw

    def raw_cardW(shot, textW, eff_textW=None):
        w, h = shot["w"], shot["h"]
        box, _ = get_hl_box(shot["image"])
        kind = shot["kind"]
        is_v2 = (kind == "C" and box is not None)
        crop = None
        if is_v2:
            W = (RIGHT_EDGE - SAFE_L) - (eff_textW if eff_textW is not None else textW) - GAP_MAX
            H_ = STAGE_H - TITLEBAR_H
            crop_w = w
            crop_h = w * H_ / W
            hl_cx = box["x"] + box["width"] / 2
            hl_cy = box["y"] + box["height"] / 2
            crop_cy = hl_cy
            if crop_cy - crop_h / 2 < 0:
                crop_cy = crop_h / 2
            if crop_cy + crop_h / 2 > h:
                crop_cy = h - crop_h / 2
            crop_y = crop_cy - crop_h / 2
            scale = min(W / w, V2_SCALE_MAX)
            if scale >= V2_SCALE_MAX:
                scale = V2_SCALE_MAX
                disp_w = w * V2_SCALE_MAX
                crop_h = min(h, (STAGE_H - TITLEBAR_H) / V2_SCALE_MAX)
                crop_y = max(0, min(h - crop_h, hl_cy - crop_h / 2))
                disp_h = crop_h * V2_SCALE_MAX
            else:
                disp_w = w * scale
                disp_h = crop_h * scale
            cardW = disp_w
            cardH = disp_h + TITLEBAR_H
            crop = {"w": crop_w, "h": round(crop_h, 1), "y": round(crop_y, 1), "scale": round(scale, 4)}
        else:
            dispW = 680 * (w / h)
            available = RIGHT_EDGE - SAFE_L - textW - MIN_GAP
            cardW = min(dispW, available)
            cardH = TITLEBAR_H + (680 if cardW >= dispW else cardW * (h / w))
        return cardW, cardH, is_v2, crop

    def layout_side_card(shot, seq=None):
        els = []
        w, h = shot["w"], shot["h"]
        y = STAGE_Y
        eb = shot.get("eyebrow", "")
        els.append({"id": "eyebrow", "kind": "eyebrow", "rect": rect_of(SAFE_L, y, est_w(eb, EB_FONT), EB_LH),
                    "parent": None, "text": eb, "font": EB_FONT})
        y += EB_LH
        if shot["type"] == "detail":
            title = shot.get("title", "")
            rows = shot.get("items", [])
        else:
            title = shot.get("seq_title", "")
            rows = shot.get("steps_acc", [])
        if title:
            y += 16
            title_lines = wrap_title_balanced(title)
            th = len(title_lines) * TITLE_LH
            tw = max(est_w(l, TITLE_FONT) for l in title_lines)
            els.append({"id": "title", "kind": "title", "rect": rect_of(SAFE_L, y, tw, th),
                        "parent": None, "text": title_lines, "font": TITLE_FONT, "lines": len(title_lines)})
            y += th
        y += 24
        for i, row in enumerate(rows):
            if shot["type"] == "detail":
                txt, active, rid, kind = row["text"], True, f"item-{i}", "item"
            else:
                txt, active, rid, kind = f"第{row['no']}步 {row['text']}", row.get("active", False), f"step-{i}", "step"
            rw = est_w(txt, ITEM_FONT) + DOT_W + ITEM_GAP
            els.append({"id": rid, "kind": kind, "rect": rect_of(SAFE_L, y, rw, ITEM_LH),
                        "parent": None, "text": txt, "font": ITEM_FONT, "active": active})
            y += ITEM_LH
        textW = max(el["rect"]["w"] for el in els if el["parent"] is None)

        # B: 占位镜头（无截图步骤）改提示卡，不再沿用上一张截图
        if shot.get("placeholder"):
            textW_seq = seq["textW_seq"] if seq else textW
            hintW = min(880, (RIGHT_EDGE - SAFE_L) - textW_seq - GAP_MAX)
            hintH = hintW * 0.5
            hintX = seq["textX"] + textW_seq + GAP_MAX
            hintY = STAGE_Y + (STAGE_H - hintH) / 2
            els.append({"id": "hint-card", "kind": "hint-card", "rect": rect_of(hintX, hintY, hintW, hintH), "parent": None})
            tri_cx = hintX + hintW / 2
            tri_cy = hintY + hintH * 0.40
            els.append({"id": "hint-triangle", "kind": "hint-triangle",
                        "rect": rect_of(tri_cx - 60, tri_cy - 60, 120, 120), "parent": "hint-card"})
            active = next((row for row in rows if row.get("active")), None)
            txt = f"第{active['no']}步 {active['text']}" if active else (shot.get("seq_title", "") or "步骤")
            tw = est_w(txt, ITEM_FONT)
            els.append({"id": "hint-text", "kind": "hint-text",
                        "rect": rect_of(hintX + hintW / 2 - tw / 2, tri_cy + 92, tw, 40),
                        "parent": "hint-card", "text": txt, "font": ITEM_FONT})
            return els, textW, hintX, hintY, hintW, hintH, False, None

        box, target = get_hl_box(shot["image"])
        eff_textW = seq["textW_seq"] if seq else None
        cardW, cardH, is_v2, crop = raw_cardW(shot, textW, eff_textW)
        # ST3-P3 B2：竖长截图滚动模式——卡宽取满 SHOT_REGION，图按框宽缩放、纵向滚动，
        # 不再按 680 高 contain 缩成窄条。
        scroll = shot.get("scroll")
        if scroll:
            cardW = SHOT_REGION["w"]
            cardH = SHOT_REGION["h"]
            is_v2 = False
            crop = None

        if seq:
            textX = seq["textX"]
            cardX = min(seq["textX"] + seq["textW_seq"] + GAP_MAX, RIGHT_EDGE - cardW)
        else:
            natural = (RIGHT_EDGE - cardW) - (SAFE_L + textW)
            if natural > GAP_MAX:
                total = textW + GAP_MAX + cardW
                left = SAFE_L + ((RIGHT_EDGE - SAFE_L) - total) / 2
                textX = left
                cardX = left + textW + GAP_MAX
            else:
                textX = SAFE_L
                cardX = RIGHT_EDGE - cardW
        cardY = STAGE_Y + (STAGE_H - cardH) / 2

        for el in els:
            if el["parent"] is None:
                el["rect"]["x"] = textX
        els.append({"id": "card", "kind": "card", "rect": rect_of(cardX, cardY, cardW, cardH), "parent": None})
        els.append({"id": "titlebar", "kind": "titlebar", "rect": rect_of(cardX, cardY, cardW, TITLEBAR_H), "parent": "card"})
        tbt = shot.get("parent_heading", "") or "截图"
        avail = cardW - 100
        t = tbt
        while len(t) > 1 and est_w(t + "…", 22) > avail:
            t = t[:-1]
        if len(t) != len(tbt):
            t = t + "…"
        els.append({"id": "titlebar-text", "kind": "titlebar-text",
                    "rect": rect_of(cardX + 16 + 68, cardY, est_w(t, 22), TITLEBAR_H), "parent": "titlebar", "text": t, "font": 22})
        els.append({"id": "image", "kind": "image",
                    "rect": rect_of(cardX, cardY + TITLEBAR_H, cardW, cardH - TITLEBAR_H),
                    "parent": "card", "image": shot["image"]})
        if crop:
            els[-1]["crop"] = crop
            els[-1]["src_w"] = w
            els[-1]["src_h"] = h
        # ST3-P3 B2：滚动模式把滚动参数挂到 image 元素，渲染层据此纵向滚动；红框本轮不显示。
        if scroll:
            els[-1]["scroll"] = scroll
        if box is not None and not shot.get("placeholder") and not scroll:
            if is_v2:
                T = {"x": cardX + (box["x"]) * (cardW / w),
                     "y": cardY + TITLEBAR_H + (box["y"] - crop["y"]) * ((cardH - TITLEBAR_H) / crop["h"]),
                     "w": box["width"] * (cardW / w),
                     "h": box["height"] * ((cardH - TITLEBAR_H) / crop["h"])}
            else:
                sx = cardW / w
                sy = (cardH - TITLEBAR_H) / h
                T = {"x": cardX + box["x"] * sx, "y": cardY + TITLEBAR_H + box["y"] * sy,
                     "w": box["width"] * sx, "h": box["height"] * sy}
            # A1: 取消箭头，一律红框。A2: 原图已有标注时外扩 8px（放不下退回定位框）。
            img_rect = els[-1]["rect"]
            hl_rect = rect_of(T["x"], T["y"], T["w"], T["h"])
            if is_annotated(shot["image"]):
                expanded = rect_of(T["x"] - 8, T["y"] - 8, T["w"] + 16, T["h"] + 16)
                if contains(img_rect, expanded):
                    hl_rect = expanded
                else:
                    highlight_manual.append({"image": shot["image"], "group": shot["group"],
                                             "reason": "标注外扩8px超出截图，退回定位框"})
            # A3: 入场动画（淡入 + 缩放 1.08→1.00 以中心为基准）；起始态外接框需在截图内
            scale_from = 1.08
            start_rect = rect_of(hl_rect["x"] - 0.04 * hl_rect["w"], hl_rect["y"] - 0.04 * hl_rect["h"],
                                 hl_rect["w"] * 1.08, hl_rect["h"] * 1.08)
            if not contains(img_rect, start_rect):
                highlight_manual.append({"image": shot["image"], "group": shot["group"],
                                         "reason": "起始态(放大1.08)超出截图，只做淡入不做缩放"})
                scale_from = 1.00
                start_rect = hl_rect
            els.append({"id": "highlight", "kind": "highlight",
                        "rect": hl_rect, "parent": "image", "color": COLOR_CLEAN,
                        "anim": {"scale_from": scale_from, "scale_to": 1.00, "fade": True,
                                 "start_frame": lead_in, "duration": FADE_FRAMES},
                        "start_rect": start_rect})
        return els, textW, cardX, cardY, cardW, cardH, is_v2, crop

    def layout_parallel(shot):
        els = []
        images = shot["parallel"]["images"]
        n = len(images)
        title = shot["parallel"].get("title", "") or shot.get("eyebrow", "")
        els.append({"id": "title", "kind": "title", "rect": rect_of(SAFE_L, STAGE_Y, est_w(title, TITLE_FONT), TITLE_LH),
                    "parent": None, "text": [title], "font": TITLE_FONT})
        # v1.2 规则二：V 方向每图高度按图数 n 计算（n=2 时与旧值 228 一致）
        v_h = (PAR_IMG_H - PAR_GAP * (n - 1) - PAR_LABEL_LH * n) / n
        v_rects = []
        for im_ in images:
            s = min((RIGHT_EDGE - SAFE_L) / im_["w"], v_h / im_["h"])
            v_rects.append({"w": im_["w"] * s, "h": im_["h"] * s, "s": s})
        v_min = min(r["w"] * r["h"] for r in v_rects)
        h_h = min(560, ((RIGHT_EDGE - SAFE_L) - PAR_GAP * (n - 1)) / sum(im_["w"] / im_["h"] for im_ in images))
        h_rects = [{"w": h_h * im_["w"] / im_["h"], "h": h_h, "s": h_h / im_["h"]} for im_ in images]
        h_min = min(r["w"] * r["h"] for r in h_rects)
        direction = "V" if v_min >= h_min else "H"
        if direction == "V":
            B = n * v_h + (n - 1) * PAR_GAP + n * PAR_LABEL_LH
            y = PAR_IMG_TOP + (PAR_IMG_H - B) / 2
            for i, im_ in enumerate(images):
                s = min((RIGHT_EDGE - SAFE_L) / im_["w"], v_h / im_["h"])
                dw, dh = im_["w"] * s, im_["h"] * s
                x = SAFE_L + ((RIGHT_EDGE - SAFE_L) - dw) / 2
                els.append({"id": f"image-{i}", "kind": "image", "rect": rect_of(x, y, dw, dh),
                            "parent": None, "image": im_["image"]})
                els.append({"id": f"label-{i}", "kind": "label", "rect": rect_of(x, y + dh, dw, PAR_LABEL_LH),
                            "parent": None, "text": im_["label"], "font": ITEM_FONT})
                y += dh + PAR_LABEL_LH + PAR_GAP
        else:
            B = h_h + PAR_LABEL_LH
            total_w = sum(h_h * im_["w"] / im_["h"] for im_ in images) + PAR_GAP * (n - 1)
            x = SAFE_L + ((RIGHT_EDGE - SAFE_L) - total_w) / 2
            y = PAR_IMG_TOP + (PAR_IMG_H - B) / 2
            for i, im_ in enumerate(images):
                dw = h_h * im_["w"] / im_["h"]
                els.append({"id": f"image-{i}", "kind": "image", "rect": rect_of(x, y, dw, h_h),
                            "parent": None, "image": im_["image"]})
                els.append({"id": f"label-{i}", "kind": "label", "rect": rect_of(x, y + h_h, dw, PAR_LABEL_LH),
                            "parent": None, "text": im_["label"], "font": ITEM_FONT})
                x += dw + PAR_GAP
        return els, direction, v_min, h_min

    def layout_ambient(shot):
        els = []
        eyebrow = shot.get("eyebrow", "")
        title = shot.get("title", "")
        items = shot.get("items", [])
        w, h = shot["w"], shot["h"]
        band_h = EB_LH + (TITLE_LH if title else 0)
        item_h = ITEM_LH
        gap = 24
        img_avail_h = STAGE_H - band_h - item_h - gap * 2
        y = STAGE_Y
        els.append({"id": "eyebrow", "kind": "eyebrow", "rect": rect_of(SAFE_L, y, est_w(eyebrow, EB_FONT), EB_LH),
                    "parent": None, "text": eyebrow, "font": EB_FONT})
        y += EB_LH
        if title:
            els.append({"id": "title", "kind": "title", "rect": rect_of(SAFE_L, y, est_w(title, TITLE_FONT), TITLE_LH),
                        "parent": None, "text": [title], "font": TITLE_FONT})
            y += TITLE_LH
        s = min((RIGHT_EDGE - SAFE_L) / w, img_avail_h / h)
        dw, dh = w * s, h * s
        ix = SAFE_L + ((RIGHT_EDGE - SAFE_L) - dw) / 2
        iy = STAGE_Y + band_h + gap
        els.append({"id": "image", "kind": "image", "rect": rect_of(ix, iy, dw, dh), "parent": None, "image": shot["image"]})
        total_w = sum(est_w(it["text"], ITEM_FONT) + DOT_W + ITEM_GAP for it in items) + 48 * (len(items) - 1)
        x = SAFE_L + ((RIGHT_EDGE - SAFE_L) - total_w) / 2
        iy2 = iy + dh + gap
        for i, it in enumerate(items):
            iw = est_w(it["text"], ITEM_FONT) + DOT_W + ITEM_GAP
            els.append({"id": f"item-{i}", "kind": "item", "rect": rect_of(x, iy2, iw, item_h),
                        "parent": None, "text": it["text"], "font": ITEM_FONT})
            x += iw + 48
        return els

    def layout_grid(shot):
        """SHOT-M5-P3 B1：概述层网格。每格=卡片边框+标题栏+图（标题栏 40，图片填剩余格子）。"""
        els = []
        eyebrow = shot.get("eyebrow", "")
        title = shot.get("title", "")
        items = shot.get("items", [])
        images = shot["grid"]["images"]  # [{image, w, h}]
        n = len(images)
        # 侧栏（同 detail）
        y = STAGE_Y
        els.append({"id": "eyebrow", "kind": "eyebrow", "rect": rect_of(SAFE_L, y, est_w(eyebrow, EB_FONT), EB_LH),
                    "parent": None, "text": eyebrow, "font": EB_FONT})
        y += EB_LH
        if title:
            y += 16
            title_lines = wrap_title_balanced(title)
            th = len(title_lines) * TITLE_LH
            tw = max(est_w(l, TITLE_FONT) for l in title_lines)
            els.append({"id": "title", "kind": "title", "rect": rect_of(SAFE_L, y, tw, th),
                        "parent": None, "text": title_lines, "font": TITLE_FONT, "lines": len(title_lines)})
            y += th
        y += 24
        for i, row in enumerate(items):
            rw = est_w(row["text"], ITEM_FONT) + DOT_W + ITEM_GAP
            els.append({"id": f"item-{i}", "kind": "item", "rect": rect_of(SAFE_L, y, rw, ITEM_LH),
                        "parent": None, "text": row["text"], "font": ITEM_FONT, "active": True})
            y += ITEM_LH
        textW = max(el["rect"]["w"] for el in els if el["parent"] is None)
        # 网格区（SHOT-M5-P7 B：枚举择优）
        grid_w = RIGHT_EDGE - SAFE_L - textW - GAP_MAX
        picked = pick_grid_layout(images, grid_w, STAGE_H)
        if picked is None:
            rows, cols = grid_rows_cols(min(n, 4))
            cell_w = (grid_w - GRID_GAP * (cols - 1)) / cols
            cell_h = (STAGE_H - GRID_GAP * (rows - 1)) / rows
        else:
            rows, cols, cell_w, cell_h, _, _ = picked
        img_h = cell_h - TITLEBAR_H
        grid_x = SAFE_L + textW + GAP_MAX
        grid_y = STAGE_Y
        disp = []
        img_geom = {}
        for i, im in enumerate(images):
            r, c = i // cols, i % cols
            cx = grid_x + c * (cell_w + GRID_GAP)
            cy = grid_y + r * (cell_h + GRID_GAP)
            iw, ih = im["w"], im["h"]
            scale = min(cell_w / iw, img_h / ih)
            dw, dh = iw * scale, ih * scale
            ix = cx + (cell_w - dw) / 2
            iy = cy + TITLEBAR_H + (img_h - dh) / 2
            els.append({"id": f"card-{i}", "kind": "card", "rect": rect_of(cx, cy, cell_w, cell_h), "parent": None})
            els.append({"id": f"titlebar-{i}", "kind": "titlebar", "rect": rect_of(cx, cy, cell_w, TITLEBAR_H),
                        "parent": f"card-{i}"})
            tbt = labels.get(im["image"], "") if labels else ""
            avail = cell_w - 100
            t = tbt
            while len(t) > 1 and est_w(t + "…", 22) > avail:
                t = t[:-1]
            if len(t) != len(tbt):
                t = t + "…"
            if t:
                els.append({"id": f"titlebar-text-{i}", "kind": "titlebar-text",
                            "rect": rect_of(cx + 16 + 68, cy, est_w(t, 22), TITLEBAR_H),
                            "parent": f"titlebar-{i}", "text": t, "font": 22})
            els.append({"id": f"image-{i}", "kind": "image", "rect": rect_of(ix, iy, dw, dh),
                        "parent": f"card-{i}", "image": im["image"]})
            disp.append({"image": im["image"], "w": round(dw, 1), "h": round(dh, 1),
                         "short": round(min(dw, dh), 1)})
            img_geom[im["image"]] = (i, ix, iy, dw, dh, cx, cy, cell_w, cell_h)
        # SHOT-M5-P5：概述层高亮（锚词落在概述层内的图，红框外扩8px钳制在格子内）
        for hl in shot.get("overview_highlights", []):
            fn = hl.get("image")
            if fn not in img_geom:
                continue
            i, ix, iy, dw, dh, cx, cy, cell_w, cell_h = img_geom[fn]
            hlx = max(cx, ix - 8)
            hly = max(cy, iy - 8)
            hlx2 = min(cx + cell_w, ix + dw + 8)
            hly2 = min(cy + cell_h, iy + dh + 8)
            hl_rect = rect_of(hlx, hly, hlx2 - hlx, hly2 - hly)
            els.append({"id": f"highlight-{i}", "kind": "highlight", "rect": hl_rect,
                        "parent": f"card-{i}", "color": COLOR_CLEAN, "grid_border": True,
                        "show_start": hl.get("start_frame", 0),
                        "show_duration": hl.get("duration_frames", 0),
                        "fade_frames": 5})
        return els, textW, rows, cols, round(cell_w, 1), round(cell_h, 1), disp


    def build_shot(d):
        fn = d["image_ref"]
        gid = effective_group(fn)
        gid_orig = d["mapped_group_id"]
        row = next(r for r in rows if r["image"] == fn)
        typ = row["type"]
        kind = row["kind"]
        w, h = d["source_image"]["width"], d["source_image"]["height"]
        sb = d["sidebar"]
        eyebrow = (sb or {}).get("eyebrow", "").replace("\\", "")
        title = (sb or {}).get("title", "") if sb else ""
        items = (sb or {}).get("items", []) if sb else []
        fallback = not sb or (not title and not items)
        if fallback:
            title, items = "", []
        parent = parent_of(eyebrow)
        if gid in two_layer_groups:
            return None  # 两层镜头（概述网格 + 细节单图）单独生成，不在逐图循环里
        start, end = timing_for(fn)
        if start is None:
            return None
        duration = max(1, end - start)
        shot = {"shot_index": None, "image": fn, "group": gid, "group_orig": gid_orig, "type": typ,
                "eyebrow": eyebrow, "title": title, "items": items, "fallback": fallback,
                "parent_heading": parent, "w": w, "h": h,
                "timing": {"start_frame": start, "duration_frames": duration, "lead_in": lead_in, "end_frame": end},
                "kind": kind, "gid_images": g_imgs.get(gid_orig, []), "sort_key": start}
        # ST3-P3 B2：竖长截图（detail/steps，非 A 型）改用滚动显示。
        if typ in ("detail", "steps") and kind != "A" and is_scroll_candidate(w, h):
            shot["scroll"] = scroll_params(w, h)
        if typ == "steps":
            si, pos = step_pos.get(gid, (None, 1))
            shot["step_seq"] = si
            shot["step_no"] = pos
            shot["seq_title"] = parent
        if typ == "parallel":
            labels = parallel_v2_raw.get(gid, {}).get("labels") if parallel_v2_raw.get(gid) else []
            images = []
            for f in g_imgs_eff[gid]:
                iw, ih = image_sizes[f]
                images.append({"image": f, "w": iw, "h": ih, "label": labels[len(images)] if len(images) < len(labels) else ""})
            pt = (BC.get(gid, {}) or {}).get("output", {}) or {}
            bc_title = pt.get("title") if pt else ""
            shot["parallel"] = {"images": images, "title": bc_title or img_by_fn[fn]["nearest_heading"].replace("\\", ""),
                                "title_fallback": not bool(bc_title)}
        return shot

    shots_raw = [build_shot(d) for d in direction]
    shots_raw = [s for s in shots_raw if s is not None]

    # SHOT-M5-P3：两层镜头（概述网格 + 细节单图）单独生成
    def group_sidebar(gid):
        for d in direction:
            if effective_group(d["image_ref"]) == gid:
                sb = d["sidebar"]
                eyebrow = (sb or {}).get("eyebrow", "").replace("\\", "")
                title = (sb or {}).get("title", "") if sb else ""
                items = (sb or {}).get("items", []) if sb else []
                fb = not sb or (not title and not items)
                if fb:
                    title, items = "", []
                return eyebrow, title, items, fb
        return "", "", [], True

    for gid, cr in cut_results.items():
        if not cr or cr["mode"] != "two_layer":
            continue
        eyebrow, title, items, fb = group_sidebar(gid)
        parent = parent_of(eyebrow)
        ws = g_win.get(gid, (0, 0))[0]
        for ov in cr["overview"]:
            scr = ov["images"]
            fn = scr[0]
            w, h = image_sizes[fn]
            images = [{"image": f, "w": image_sizes[f][0], "h": image_sizes[f][1]} for f in scr]
            scr_set = set(scr)
            ov_hl = []
            for hfn, rel_start, dur in cr.get("overview_highlights", []):
                if hfn in scr_set:
                    ov_hl.append({"image": hfn, "start_frame": rel_start,
                                  "duration_frames": dur})
            shots_raw.append({
                "shot_index": None, "image": fn, "group": gid, "group_orig": gid,
                "type": "grid", "layer": "overview",
                "eyebrow": eyebrow, "title": title, "items": items, "fallback": fb,
                "parent_heading": parent, "w": w, "h": h,
                "timing": {"start_frame": ov["start"], "duration_frames": ov["dur"], "lead_in": lead_in,
                           "end_frame": ov["start"] + ov["dur"]},
                "kind": classify_and_layout(w, h)["kind"], "gid_images": g_imgs_eff.get(gid, []),
                "sort_key": ov["start"],
                "grid": {"images": images, "cap": OVERVIEW_MAX_N},
                "overview_highlights": ov_hl,
            })
        for det in cr["detail"]:
            fn = det["image"]
            w, h = image_sizes[fn]
            dkind = classify_and_layout(w, h)["kind"]
            dshot = {
                "shot_index": None, "image": fn, "group": gid, "group_orig": gid,
                "type": "detail", "layer": "detail",
                "eyebrow": eyebrow, "title": title, "items": items, "fallback": fb,
                "parent_heading": parent, "w": w, "h": h,
                "timing": {"start_frame": det["start"], "duration_frames": det["dur"], "lead_in": lead_in,
                           "end_frame": det["start"] + det["dur"]},
                "kind": dkind, "gid_images": g_imgs_eff.get(gid, []),
                "sort_key": det["start"], "anchor_frame": det["anchor_frame"],
            }
            if dkind != "A" and is_scroll_candidate(w, h):
                dshot["scroll"] = scroll_params(w, h)
            shots_raw.append(dshot)
        # SHOT-M5-P6 二：宽图（N）单独成镜（ambient），放在组末尾，每张 ≥2s
        we = g_win.get(gid, (0, 0))[1]
        wide_ambient = cr.get("wide_ambient", [])
        for wi, wfn in enumerate(wide_ambient):
            w, h = image_sizes[wfn]
            start = we - (len(wide_ambient) - wi) * 60
            shots_raw.append({
                "shot_index": None, "image": wfn, "group": gid, "group_orig": gid,
                "type": "ambient",
                "eyebrow": eyebrow, "title": "", "items": [],
                "fallback": True, "parent_heading": parent, "w": w, "h": h,
                "timing": {"start_frame": start, "duration_frames": 60, "lead_in": lead_in,
                           "end_frame": start + 60},
                "kind": "A", "gid_images": g_imgs_eff.get(gid, []),
                "sort_key": start,
            })

    merged = []
    seen_parallel = set()
    for s in shots_raw:
        if s["type"] == "parallel":
            if s["group"] in seen_parallel:
                continue
            seen_parallel.add(s["group"])
            merged.append(s)
        else:
            merged.append(s)
    merged.sort(key=lambda s: s["sort_key"])
    for i, s in enumerate(merged):
        s["shot_index"] = i

    steps_seq_map = {}
    for si, seq in enumerate(seqs):
        for pos, gid in enumerate(seq, 1):
            img_for_g = [s for s in merged if s["type"] == "steps" and s["group"] == gid]
            if img_for_g:
                txt = img_for_g[0]["items"][0]["text"] if img_for_g[0]["items"] else img_for_g[0]["eyebrow"]
            else:
                txt = re.sub(r"^第[一二三四五六七八九1-9]步[，,、]\s*", "", g_narr.get(gid, ""))
            steps_seq_map.setdefault(si, []).append({"step_no": pos, "group": gid, "text": txt, "has_img": bool(img_for_g)})

    # C3: 为无图步骤生成占位镜头（图片/卡片/版式复用序列上一个有图镜头，步骤列表推进到该步）
    if include_placeholders:
        placeholders = []
        for si, steps in steps_seq_map.items():
            prev = None
            for step in steps:
                if step["has_img"]:
                    for s in merged:
                        if s["type"] == "steps" and s["group"] == step["group"]:
                            prev = s
                            break
                else:
                    if prev is None:
                        continue
                    gid = step["group"]
                    start, end = g_win.get(gid, (0, 0))
                    duration = max(1, end - start)
                    placeholders.append({
                        "shot_index": None, "image": None, "group": gid, "group_orig": gid,
                        "type": "steps", "eyebrow": prev["eyebrow"], "title": "", "items": [],
                        "fallback": False, "parent_heading": prev["parent_heading"],
                        "w": None, "h": None,
                        "timing": {"start_frame": start, "duration_frames": duration, "lead_in": lead_in, "end_frame": end},
                        "kind": None, "gid_images": [], "sort_key": start,
                        "step_seq": si, "step_no": step["step_no"], "seq_title": prev["seq_title"],
                        "placeholder": True,
                    })
        merged.extend(placeholders)
        merged.sort(key=lambda s: s["sort_key"])
        for i, s in enumerate(merged):
            s["shot_index"] = i

    for s in merged:
        if s["type"] != "steps":
            continue
        si = s["step_seq"]
        full = steps_seq_map.get(si, [])
        cur_idx = next(k for k, x in enumerate(full) if x["group"] == s["group"])
        acc = full[max(0, cur_idx - 2):cur_idx + 1]
        s["steps_acc"] = [{"text": x["text"], "no": x["step_no"], "active": x["group"] == s["group"]} for x in acc]

    # F2：步骤序列共用左边界
    for s in merged:
        if s["type"] in ("detail", "steps"):
            s["_textW"] = sidebar_textW(s)
    seq_textW = {}
    for s in merged:
        if s["type"] == "steps" and not s.get("placeholder"):
            si = s["step_seq"]
            seq_textW[si] = max(seq_textW.get(si, 0), s["_textW"])
    for s in merged:
        if s["type"] in ("detail", "steps") and not s.get("placeholder"):
            eff = seq_textW.get(s["step_seq"]) if s["type"] == "steps" else None
            s["_cardW"], s["_cardH"], s["_is_v2"], s["_crop"] = raw_cardW(s, s["_textW"], eff)
    seq_X = {}
    for s in merged:
        if s["type"] == "steps" and not s.get("placeholder"):
            si = s["step_seq"]
            seq_X.setdefault(si, {"textW_seq": seq_textW[si], "maxCardW": 0})
            seq_X[si]["maxCardW"] = max(seq_X[si]["maxCardW"], s["_cardW"])
    for si, info in seq_X.items():
        info["X_seq"] = max(SAFE_L, SAFE_L + ((RIGHT_EDGE - SAFE_L) - (info["textW_seq"] + GAP_MAX + info["maxCardW"])) / 2)

    # 正式布局 + 自检 + 锚点
    anchor_diag = []
    highlight_manual = []
    for s in merged:
        if s["type"] == "parallel":
            els, direction_, v_min, h_min = layout_parallel(s)
            s["parallel"]["direction"] = direction_
            s["parallel"]["v_min_area"] = round(v_min, 1)
            s["parallel"]["h_min_area"] = round(h_min, 1)
            s["layout"] = {}
        elif s["type"] == "ambient":
            els = layout_ambient(s)
            s["layout"] = {}
        elif s["type"] == "grid":
            els, textW, rows, cols, cell_w, cell_h, disp = layout_grid(s)
            s["grid"]["rows"] = rows
            s["grid"]["cols"] = cols
            s["grid"]["cell_w"] = cell_w
            s["grid"]["cell_h"] = cell_h
            s["grid"]["display"] = disp
            s["layout"] = {"textW": round(textW, 1)}
        else:
            seq = None
            if s["type"] == "steps":
                info = seq_X[s["step_seq"]]
                seq = {"textW_seq": info["textW_seq"], "textX": info["X_seq"]}
            els, textW, cardX, cardY, cardW, cardH, is_v2, crop = layout_side_card(s, seq)
            s["layout"] = {"textW": round(textW, 1), "cardX": round(cardX, 1), "cardY": round(cardY, 1),
                           "cardW": round(cardW, 1), "cardH": round(cardH, 1), "v2": is_v2, "crop": crop,
                           "seq_textX": round(seq["textX"], 1) if seq else None}
            s["titlebar_truncated"] = None
        by_id = {el["id"]: el for el in els}
        for el in els:
            if el.get("parent") and el["parent"] in by_id:
                p = by_id[el["parent"]]
                el["rel_x"] = round(el["rect"]["x"] - p["rect"]["x"], 1)
                el["rel_y"] = round(el["rect"]["y"] - p["rect"]["y"], 1)
            else:
                el["rel_x"] = el["rect"]["x"]
                el["rel_y"] = el["rect"]["y"]
        s["elements"] = els
        ok, errs = check_elements(els)
        s["self_check"] = {"pass": ok, "errors": errs}

    # C 步：序列对齐检查（跨镜头）
    seq_align_errs = []
    seq_x_map = {}
    for s in merged:
        if s["type"] == "steps":
            seq_x_map.setdefault(s["step_seq"], []).append((s["image"], s["layout"].get("seq_textX")))
    for si, lst in seq_x_map.items():
        xs = [x for _, x in lst if x is not None]
        if xs and any(abs(x - xs[0]) > 0.5 for x in xs):
            seq_align_errs.append(f"序列{si} x 不一致: {lst}")
            for s in merged:
                if s["type"] == "steps" and s["step_seq"] == si:
                    s["self_check"]["pass"] = False
                    s["self_check"]["errors"].append(f"序列对齐: x={s['layout'].get('seq_textX')}")

    # E 步：条目锚点
    g_narr_norm = {gid: norm(g_narr[gid]) for gid in g_narr}
    g_start = {gid: norm_text.find(nn) for gid, nn in g_narr_norm.items()}
    anchor_miss = []
    for s in merged:
        items = s.get("items", [])
        if not items:
            continue
        gid = s["group"]
        nn = g_narr_norm.get(gid, "")
        gstart = g_start.get(gid, 0)
        prev_local = 0
        for it in items:
            sq = it.get("source_quote", "")
            ap = it.get("anchor", "")
            sq_local = nn.find(norm(sq), prev_local)
            if sq_local < 0:
                sq_local = prev_local
            ap_local = nn.find(norm(ap), sq_local)
            old_at = it.get("at_frame")
            old_abs = s["timing"]["start_frame"] + old_at if old_at is not None else None
            if gstart >= 0 and ap_local >= 0 and gstart + ap_local < len(norm_flow):
                af = norm_flow[gstart + ap_local][1]
                prev_local = ap_local
            else:
                af = old_abs
                anchor_miss.append({"image": s["image"], "item": it["text"], "anchor": ap})
            new_rel = (af - s["timing"]["start_frame"]) if af is not None else None
            it["anchor_abs"] = af
            it["anchor_rel"] = new_rel
            anchor_diag.append({"image": s["image"], "item": it["text"], "anchor": ap,
                                "old_abs": old_abs, "new_abs": af, "new_rel": new_rel})

    for s in merged:
        start, end = s["timing"]["start_frame"], s["timing"]["end_frame"]
        lines = []
        for ln in subtitle_lines_abs:
            ls, le = ln["start_frame"], ln["end_frame"]
            if le <= start or ls >= end:
                continue
            chars = [{"c": c["c"], "f": c["f"] - start} for c in ln["chars"] if start <= c["f"] <= end]
            lines.append({"start_frame": max(0, ls - start), "end_frame": min(end, le) - start, "chars": chars})
        s["subtitle_lines"] = lines
        s["audio_start_frame"] = start
        s["total_frames"] = s["timing"]["duration_frames"] + lead_in
        s["lead_in"] = lead_in

    return {"shots": merged, "steps_seq": seqs, "anchor_diag": anchor_diag,
            "single_shot_drop": single_shot_drop, "highlight_manual": highlight_manual,
            "parallel_split_manual": parallel_split_manual,
            "single_shot_manual": single_shot_manual,
            "detail_dropped_manual": detail_dropped_manual,
            "detail_no_shot_manual": detail_no_shot_manual,
            "overview_highlight_manual": overview_highlight_manual,
            "highlight_dropped_manual": highlight_dropped_manual,
            "transfer": transfer, "transfer_voided": transfer_voided,
            "merge": merge, "merge_report": merge_report,
            "anchor_frame_of_fn": anchor_frame_of_fn}
