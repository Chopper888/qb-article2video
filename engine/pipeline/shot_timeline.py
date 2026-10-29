# -*- coding: utf-8 -*-
"""shot_timeline：SHOT-M5-P8 v1.2 截图编排（以文章位置为准 + 小节内停留）。

替代 shot_director.build_shots 的截图编排逻辑（D' 节）：
  D'1 小节约束映射：越界图在本小节内用原回填匹配方式重找最近一组；
  D'2 图组 = 原文连续出现的图（同一 mapped_group_id，按 line 序）；
  D'3 小节内停留：图组在其映射组起点出现，停留到下一图组或小节结束；
  D'4 概述层（网格）之后按文章顺序逐张轮流放大，每张 ≥2s / ≤6s。

本轮识图调用为 0；不上红框；不搬运；不插停顿；不按锚词/两层分档。

输出 shots 结构与 shot_director.build_shots 的 shots 列表字段兼容（供 scene_build + 渲染层消费）：
  grid 镜头（概述）：type="grid" / layer="overview"，grid={images, rows, cols, cell_w, cell_h, display}
  detail 镜头（逐张/单图）：type="detail" / layer="detail"，image + elements
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

# 引擎根随代码走（ST1-P4a3）：仓库根由本文件位置推出，不写死主仓路径。
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from pipeline import word_align
from pipeline.layout import classify_and_layout, is_scroll_candidate
from pipeline.shot_director import (
    pick_grid_layout, grid_rows_cols, est_w, wrap_title_balanced,
    rect_of, contains, check_elements, fn_of, norm, scroll_params,
    SAFE_L, SAFE_R, STAGE_Y, STAGE_H, RIGHT_EDGE, TITLEBAR_H,
    EB_FONT, EB_LH, TITLE_FONT, TITLE_LH, ITEM_FONT, ITEM_LH,
    DOT_W, ITEM_GAP, GRID_GAP, GRID_MIN_SHORT, GRID_MIN_SHORT_OVERVIEW,
    OVERVIEW_MAX_N, LEAD_IN, FPS, GRID_MAX_ASPECT,
)
from pipeline import backfill
from pipeline import allocate
from pipeline import shot_annotate
from pipeline.shot_ai import parse_json
from pipeline.scene_build import _split_subtitle
import r148_subtitle_align as salign

FONT_W_FACTOR = 1.02
GAP_MAX = 200
MIN_GAP = 64
EB_CARD_GAP = 24              # P12c 第5种：只有眉标时，眉标下沿到卡片顶的间距（沿用 _layout_ambient 的 gap）
OVERVIEW_DUR_FRAMES = 60      # 概述层每屏 2 秒（沿用 P7）
DETAIL_MIN_FRAMES = 60        # 逐张每张至少 2 秒
DETAIL_MAX_FRAMES = 180       # 逐张每张最长 6 秒

# P9 B1 硬规则词表 = v1.1 A3 + 补「下面是」「像下面」（与 shot_invariants.A1_WORDS_V2 一致）
A1_WORDS = ["下面这张图", "下面这两张图", "下面这几张图", "下面这些图", "下面这些报错截图",
            "下面这张", "下图", "如图", "如下图", "这张截图", "这张图", "这些图", "这几张图",
            "比如下图", "下图这种", "上面这张", "下面的截图", "下面的答复", "下面是", "像下面"]

# 复数指代词表：一行字幕出现任一词语时不触发切换
PLURAL_WORDS = ["这两张", "这几张", "这些图", "这些截图", "这些报错",
                "下面这些", "这两种", "这几种", "下面几种"]


# 版式统一为左标注、右截图；只有条状图（宽高比 > STRIP_ASPECT，且放进左右版式后
# 显示高度 < STRIP_MAX_H 像素）保留整图版式。
STRIP_ASPECT = 5.0
STRIP_MAX_H = 250

# 左侧命令框（深色底）。字号和标注条目相当，内边距加宽；
# 字体使用系统字体（等宽字体栈），宽度、行数按实际量宽得到，不按字符个数估算。
CMD_FONT = 38
CMD_LH = 52
CMD_PAD_X = 28
CMD_PAD_Y = 16
CMD_PROMPT_GAP = 14                 # 提示符 ">" 右边的间距
CMD_GAP = 16                        # 命令框和它上面那条内容之间的间距
CMD_MAX_LINES = 2                   # 完整命令在命令框里两行以内写得下才进命令框
CMD_NEAR_SENTS = 4                  # 命令对应的口播句只在这张图锚点句的前后几句里找
AMBIENT_MAX_ITEMS = 2               # 整图版式（条状图）图下方最多放 2 条标注


def is_strip(w, h, detail_img_h):
    """R2：保留整图版式的条状图 = 宽高比 > STRIP_ASPECT，且放进左右版式后显示高度 < STRIP_MAX_H。"""
    return w / h > STRIP_ASPECT and detail_img_h < STRIP_MAX_H


def _cmd_prompt_w():
    return shot_annotate.measure(">", CMD_FONT) + CMD_PROMPT_GAP


def cmd_lines(text):
    """一条命令在命令框里排成几行：只在空格处换行，单个词比一行还长才在中间断开（按渲染字体量宽）。"""
    inner = EB_MAX_W - CMD_PAD_X * 2 - _cmd_prompt_w() - shot_annotate.MEASURE_PAD
    return [ln.rstrip() for ln in shot_annotate.wrap_words(
        text, lambda t: shot_annotate.measure(t, CMD_FONT), inner)]


def cmd_box_size(lines):
    """命令框的宽、高（像素）。"""
    w = (CMD_PAD_X * 2 + _cmd_prompt_w() + shot_annotate.MEASURE_PAD
         + max(shot_annotate.measure(ln, CMD_FONT) for ln in lines))
    return round(w, 1), CMD_PAD_Y * 2 + CMD_LH * len(lines)


def has_cjk(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def cmd_destination(c, has_image):
    """ST3-P12 第 3 步：一条正文命令去截图左边的命令框，还是单独出命令行卡片。

    - 原文没写全的命令（命令里含汉字）：一律进命令框；
    - 完整命令：旁边有截图，且在命令框里两行以内写得下 → 命令框；否则出卡片；
    - 旁边没有截图：没有命令框可进，出卡片。
    返回 ("box" | "card", 命令框里的各行文字, 依据)。
    """
    lines = cmd_lines(c["text"])
    if not has_image:
        return "card", lines, "旁边没有截图"
    if has_cjk(c["text"]):
        return "box", lines, "原文没写全（含汉字）"
    if len(lines) <= CMD_MAX_LINES:
        return "box", lines, f"{len(lines)} 行写得下"
    return "card", lines, f"要 {len(lines)} 行，两行写不下"


def attach_orphans(im, body_gids):
    """ST3-P11 R11：没挂上任何口播组的截图也要出现。

    按原文位置就近挂靠（前一张已挂组的图 → 后一张 → 第一个正文组），只为让它进时间线；
    出现时间仍由原文位置定锚（resolve_anchors 的「原文位置 / 等比分布」）。
    返回 (新的 image_map, 被兜底的文件名列表)。
    """
    images = sorted(im.get("images", []), key=lambda e: e.get("line") or 0)
    mapped = [e for e in images if e.get("mapped_group_id")]
    out, orphans = [], []
    for e in images:
        if e.get("mapped_group_id"):
            out.append(e)
            continue
        line = e.get("line") or 0
        prev = [m for m in mapped if (m.get("line") or 0) < line]
        nxt = [m for m in mapped if (m.get("line") or 0) > line]
        gid = (prev[-1] if prev else nxt[0])["mapped_group_id"] if (prev or nxt) else (
            body_gids[0] if body_gids else None)
        if gid is None:
            continue
        out.append(dict(e, mapped_group_id=gid, match_basis="orphan_position", orphan=True))
        orphans.append(fn_of(e))
    return {**im, "images": out}, orphans


def adjacent_runs(fns, line_of, art_lines=None):
    """ST3-P11 R4：把一组图按「原文里连着贴」切成若干段。连着贴 = 两张图之间只有空行。

    没有文章原文时（旧调用方）退回按行号判断：相邻两张图的行号相差不超过 2。
    """
    runs = []
    for fn in sorted(fns, key=lambda f: line_of.get(f, 0)):
        if runs:
            a, b = line_of.get(runs[-1][-1], 0), line_of.get(fn, 0)
            if art_lines is not None:
                joined = b > a and all(not ln.strip() for ln in art_lines[a:b - 1])
            else:
                joined = 0 < b - a <= 2
            if joined:
                runs[-1].append(fn)
                continue
        runs.append([fn])
    return runs


# C 项（v1.1）：推荐文章句识别。一行字幕含《，且书名号内以数字.数字开头，或同句含"文章"二字。
_RECOMMEND_TITLE_RE = re.compile(r"^\d+\.\d+")


def _is_recommend_line(text, group_text):
    if "《" not in text:
        return False
    m = re.search(r"《([^》]*)", text)
    title = m.group(1) if m else ""
    if _RECOMMEND_TITLE_RE.match(title):
        return True
    if "文章" in group_text:
        return True
    return False


def _sentence_first_frame(lines, idx):
    """返回 lines[idx] 所在完整句子（以 。？！ 为界）的第一行 start_frame。lines 为 [(sf, text)] 同组按帧序。"""
    j = idx
    while j > 0:
        prev = lines[j - 1][1]
        if prev and prev[-1] in "。？！":
            break
        j -= 1
    return lines[j][0]


def title_name(t):
    name = t.strip("【】")
    name = re.sub(r"^[一二三四五六七八九十]+[、，,]\s*", "", name)
    return name.strip()


_ARTICLE_TITLE_RE = re.compile(r"^\*?\d+(?:\.\d+)+")


def _is_article_title(nm):
    """文章标题（如 "*2.3.7 基础篇-…" / "3.1.4.2 实战篇-…"），不作为小节眉标。"""
    return bool(_ARTICLE_TITLE_RE.match(nm or ""))


def subsection_groups(gr):
    """返回 {小节名: [body group_id...]}（按文章顺序），标题组都视为小节边界。"""
    titles = sorted([g for g in gr["groups"] if g.get("starts_with_bracket_title")],
                    key=lambda g: g["paragraph_index"])
    body = [g for g in gr["groups"] if not g.get("starts_with_bracket_title")]
    out = {}
    for i, t in enumerate(titles):
        nm = title_name(t["group_text"])
        start = t["paragraph_index"]
        end = titles[i + 1]["paragraph_index"] if i + 1 < len(titles) else 10 ** 9
        gids = [g["group_id"] for g in body if start < g["paragraph_index"] < end]
        out.setdefault(nm, []).extend(gids)
    return out


def fix_subsection_mapping(im, gr):
    """D'1：修正映射。两类：越界（小节外）+ 映射到"相关文章"链接组（图不描述图本身）。

    返回 (corrected {fn: gid}, fix_report [dict])。corrected 覆盖全部图（未修正的保持原值）。
    """
    subsec = subsection_groups(gr)
    body = [g for g in gr["groups"] if not g.get("starts_with_bracket_title")]
    g_text = {g["group_id"]: g["group_text"] for g in body}
    A1 = ["下面这张图", "下面这两张图", "下面这几张图", "下面这些图", "下面这些报错截图",
          "下面这张", "下图", "如图", "如下图", "这张截图", "这张图", "这些图", "这几张图",
          "比如下图", "下图这种", "上面这张", "下面的截图", "下面的答复"]
    RELATED = ["可以看这篇文章", "可以看看这篇文章", "可以看这些文章", "可以看看这些文章"]

    def _is_related(txt):
        return any(w in (txt or "") for w in RELATED)

    corrected = {}
    fix_report = []
    for e in im["images"]:
        fn = fn_of(e)
        gid = e["mapped_group_id"]
        heading = e["nearest_heading"]
        gids = subsec.get(heading, [])
        new_gid = gid
        reason = None
        conf = None

        if gid in gids:
            if _is_related(g_text.get(gid, "")):
                # 映射组是"相关文章"链接（不描述图本身）→ 重映射到本小节内、之前的最近指代句
                pos = gids.index(gid)
                refer_before = [c for c in gids[:pos] if any(w in g_text.get(c, "") for w in A1)]
                if refer_before:
                    new_gid = refer_before[-1]
                    reason = "相关文章链接组→指代句"
        else:
            # 越界：在本小节内重匹配
            before = e["before"] or ""
            after = e["after"] or ""
            ctx = after if (not before or backfill.is_heading(before)) else before
            ctx_n = backfill.norm(ctx)
            best = None
            for cand in gids:
                ratio = difflib_ratio(ctx_n, backfill.norm(g_text.get(cand, "")))
                if best is None or ratio > best[1]:
                    best = (cand, ratio)
            new_gid = best[0] if best else gid
            conf = round(best[1], 3) if best else None
            reason = "越界重匹配"

        corrected[fn] = new_gid
        if reason:
            fix_report.append({"image": fn, "heading": heading, "from": gid, "to": new_gid,
                               "reason": reason, "confidence": conf})
    return corrected, fix_report


def difflib_ratio(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def _split_screens(images, k):
    """把 images 均分成 k 屏，每屏 ≤ OVERVIEW_MAX_N。"""
    images = images[:k * OVERVIEW_MAX_N]
    if not images:
        return []
    base = len(images) // k
    extra = len(images) % k
    screens = []
    idx = 0
    for i in range(k):
        size = base + (1 if i < extra else 0)
        screens.append(images[idx:idx + size])
        idx += size
    return screens


def _finalize_els(els):
    """登记 rel_x/rel_y（子元素相对父元素，顶层绝对），与 shot_director 一致。"""
    by_id = {el["id"]: el for el in els}
    for el in els:
        if el.get("parent") and el["parent"] in by_id:
            p = by_id[el["parent"]]
            el["rel_x"] = round(el["rect"]["x"] - p["rect"]["x"], 1)
            el["rel_y"] = round(el["rect"]["y"] - p["rect"]["y"], 1)
        else:
            el["rel_x"] = el["rect"]["x"]
            el["rel_y"] = el["rect"]["y"]
    return els


EB_MAX_W = 645  # 侧栏元素宽度上限（与 shot_director.check_elements 的 645 一致）
EB_FONT_SMALL = 24  # ST3-P3d：眉标超宽时先缩一档


def _cmd_el(k, c, y):
    lines = c.get("box_lines") or cmd_lines(c["text"])
    w, h = cmd_box_size(lines)
    return {"id": f"cmd-{k}", "kind": "cmd", "rect": rect_of(SAFE_L, y, w, h),
            "parent": None, "text": c["text"], "lines": lines, "font": CMD_FONT, "lh": CMD_LH,
            "pad_x": CMD_PAD_X, "prompt_gap": CMD_PROMPT_GAP, "cmd_id": c.get("id"),
            "anchor_abs": c.get("anchor_abs"), "anchor_rel": None}


def _eyebrow_font(eyebrow):
    """ST3-P3d：眉标不截断——超宽先缩字号一档，还放不下就不显示（返回 None）。"""
    if not eyebrow:
        return None
    if est_w(eyebrow, EB_FONT) <= EB_MAX_W:
        return EB_FONT
    if est_w(eyebrow, EB_FONT_SMALL) <= EB_MAX_W:
        return EB_FONT_SMALL
    return None


def _title_lh(font):
    return round((font or TITLE_FONT) * TITLE_LH / TITLE_FONT)


def sidebar_capacity(eyebrow, title, title_font, cmds):
    """ST3-P12 R1：左栏还能放几条标注。标题、命令框占的高度先扣掉（有标题、没有命令框时是 6 条）。"""
    used = (EB_LH if _eyebrow_font(eyebrow) else 0) + ((16 + _title_lh(title_font)) if title else 0) + 24
    for c in cmds or []:
        used += CMD_GAP + cmd_box_size(c.get("box_lines") or cmd_lines(c["text"]))[1]
    return int((STAGE_H - used) // ITEM_LH)


def cap_items(items, cap):
    """条目超过左栏上限：按句子顺序留前面的，超出上限的句子不出条（登记）。返回 (留下的, 没出的)。"""
    cap = max(0, cap)
    return list(items[:cap]), list(items[cap:])


def _sidebar_els(eyebrow, title, items, cmds=None, annot=None):
    """构造侧栏元素（eyebrow/title/item，以及命令框），返回 (els, y_end)。

    annot 不为 None（ST3-P12 逐图标注）：标题一行、字号取 annot["title_font"]；条目和命令框
    按出现时间依次往下排，每一个都紧贴在上一条已显示内容的下面；宽度按渲染字体实际量。
    """
    els = []
    y = STAGE_Y
    efont = _eyebrow_font(eyebrow)
    if efont is not None:
        els.append({"id": "eyebrow", "kind": "eyebrow", "rect": rect_of(SAFE_L, y, est_w(eyebrow, efont), EB_LH),
                    "parent": None, "text": eyebrow, "font": efont})
        y += EB_LH
    if annot is not None:
        if title:
            y += 16
            tf = annot.get("title_font") or TITLE_FONT
            els.append({"id": "title", "kind": "title",
                        "rect": rect_of(SAFE_L, y, shot_annotate.text_w(title, tf), _title_lh(tf)),
                        "parent": None, "text": [title], "font": tf, "lh": _title_lh(tf), "lines": 1})
            y += _title_lh(tf)
        y += 24
        # 同一句话既有条目又有命令框：条目在上，命令框在它正下方。那句话不在屏的命令框和图一起出现，排最前。
        rows = [((it.get("anchor_abs") if it.get("anchor_abs") is not None else -1), 0, i, it)
                for i, it in enumerate(items)]
        rows += [((c.get("anchor_abs") if c.get("anchor_abs") is not None else -1), 1, k, c)
                 for k, c in enumerate(cmds or [])]
        for _t, is_cmd, i, x in sorted(rows, key=lambda r: r[:3]):
            if is_cmd:
                y += CMD_GAP
                el = _cmd_el(i, x, y)
            else:
                el = {"id": f"item-{i}", "kind": "item",
                      "rect": rect_of(SAFE_L, y, shot_annotate.item_w(x.get("text", "")), ITEM_LH),
                      "parent": None, "text": x.get("text", ""), "font": ITEM_FONT, "active": True}
            els.append(el)
            y += el["rect"]["h"]
        return els, y
    if title:
        y += 16
        title_lines = wrap_title_balanced(title)
        th = len(title_lines) * TITLE_LH
        tw = max(est_w(l, TITLE_FONT) for l in title_lines)
        els.append({"id": "title", "kind": "title", "rect": rect_of(SAFE_L, y, tw, th),
                    "parent": None, "text": title_lines, "font": TITLE_FONT, "lines": len(title_lines)})
        y += th
    y += 24
    for i, it in enumerate(items):
        txt = it.get("text", "")
        rw = est_w(txt, ITEM_FONT) + DOT_W + ITEM_GAP
        els.append({"id": f"item-{i}", "kind": "item", "rect": rect_of(SAFE_L, y, rw, ITEM_LH),
                    "parent": None, "text": txt, "font": ITEM_FONT, "active": True})
        y += ITEM_LH
    for k, c in enumerate(cmds or []):
        # ST3-P11 R3：命令框排在标注条目下方，一条命令一个框。
        y += CMD_GAP
        el = _cmd_el(k, c, y)
        els.append(el)
        y += el["rect"]["h"]
    return els, y


def _layout_grid(shot, labels, x_group=None, textW_group=None, no_titlebar=False):
    """概述层网格布局（无红框）。返回 (els, textW, rows, cols, cell_w, cell_h, disp)。"""
    eyebrow = shot.get("eyebrow", "")
    title = shot.get("title", "")
    items = shot.get("items", [])
    images = shot["grid"]["images"]
    n = len(images)
    els, _ = _sidebar_els(eyebrow, title, items)
    textW = max(el["rect"]["w"] for el in els if el["parent"] is None)
    if x_group is None and not title and not items:
        # P12c 第5种：只有眉标——网格在[眉标下沿+间距, 字幕区上沿]内、水平铺满（中心即 960）
        grid_w = RIGHT_EDGE - SAFE_L
        grid_h = STAGE_H - EB_LH - EB_CARD_GAP
        picked = pick_grid_layout(images, grid_w, grid_h)
        if picked is None:
            raise SystemExit(f"P12c 只有眉标 grid 放不下: {shot.get('image')}")
        rows, cols, cell_w, cell_h, _, min_short = picked
        if min_short < GRID_MIN_SHORT_OVERVIEW:
            raise SystemExit(f"P12c 只有眉标 grid 短边不足: {shot.get('image')} short={min_short:.1f} < {GRID_MIN_SHORT_OVERVIEW}")
        text_x = SAFE_L
        grid_x = SAFE_L
        grid_y = STAGE_Y + EB_LH + EB_CARD_GAP
        img_h = cell_h if no_titlebar else cell_h - TITLEBAR_H
    else:
        grid_w = RIGHT_EDGE - SAFE_L - textW - GAP_MAX
        picked = pick_grid_layout(images, grid_w, STAGE_H)
        if picked is None:
            rows, cols = grid_rows_cols(min(n, OVERVIEW_MAX_N))
            cell_w = (grid_w - GRID_GAP * (cols - 1)) / cols
            cell_h = (STAGE_H - GRID_GAP * (rows - 1)) / rows
        else:
            rows, cols, cell_w, cell_h, _, _ = picked
        img_h = cell_h if no_titlebar else cell_h - TITLEBAR_H
        if x_group is None:
            text_x = SAFE_L
            grid_x = SAFE_L + textW + GAP_MAX
        else:
            text_x = x_group
            grid_x = min(x_group + textW_group + GAP_MAX, RIGHT_EDGE - grid_w)
        grid_y = STAGE_Y
    for el in els:
        if el["parent"] is None:
            el["rect"]["x"] = text_x
    disp = []
    # ST3-F2：一屏最多 OVERVIEW_MAX_N 张。超出的由调用方分屏（_split_screens），这里不再截断丢图。
    if n > OVERVIEW_MAX_N:
        raise SystemExit(f"网格一屏超过 {OVERVIEW_MAX_N} 张（{n} 张），调用方应先分屏: {shot.get('image')}")
    for i, im in enumerate(images):
        r, c = i // cols, i % cols
        cx = grid_x + c * (cell_w + GRID_GAP)
        cy = grid_y + r * (cell_h + GRID_GAP)
        iw, ih = im["w"], im["h"]
        scale = min(cell_w / iw, img_h / ih)
        dw, dh = iw * scale, ih * scale
        ix = cx + (cell_w - dw) / 2
        iy = cy + (0 if no_titlebar else TITLEBAR_H) + (img_h - dh) / 2
        els.append({"id": f"card-{i}", "kind": "card", "rect": rect_of(cx, cy, cell_w, cell_h), "parent": None})
        if not no_titlebar:
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
        disp.append({"image": im["image"], "w": round(dw, 1), "h": round(dh, 1), "short": round(min(dw, dh), 1)})
    return els, textW, rows, cols, round(cell_w, 1), round(cell_h, 1), disp


def _layout_grid_wide(shot, wide_imgs, normal_imgs, labels, x_group=None, textW_group=None):
    """P9 裁定一：极宽图（宽高比>2.5）独占一整行，其余图按择优排在下方。

    返回 (els, textW, disp)；任一图短边<200 或放不下时返回 None。
    """
    eyebrow = shot.get("eyebrow", "")
    title = shot.get("title", "")
    items = shot.get("items", [])
    els, _ = _sidebar_els(eyebrow, title, items)
    textW = max(el["rect"]["w"] for el in els if el["parent"] is None)
    grid_w = RIGHT_EDGE - SAFE_L - textW - GAP_MAX
    if x_group is None:
        text_x = SAFE_L
        if not title and not items:
            # P12c 第5种：只有眉标 + 极宽图概述网格，缩小/短边规则方案端未明确，当前无此组合，触发即停汇报
            raise SystemExit(f"P12c 只有眉标 + 极宽图概述网格未定义做法: {shot.get('image')}")
        else:
            grid_x = SAFE_L + textW + GAP_MAX
    else:
        text_x = x_group
        grid_x = min(x_group + textW_group + GAP_MAX, RIGHT_EDGE - grid_w)
    for el in els:
        if el["parent"] is None:
            el["rect"]["x"] = text_x
    grid_y = STAGE_Y
    disp = []
    y = grid_y

    # 极宽图各占一整行
    for idx, im in enumerate(wide_imgs):
        iw, ih = im["w"], im["h"]
        img_h = grid_w * (ih / iw)
        if img_h < GRID_MIN_SHORT_OVERVIEW:
            return None
        cell_h = img_h + TITLEBAR_H
        if y + cell_h > STAGE_Y + STAGE_H:
            return None
        els.append({"id": f"card-w{idx}", "kind": "card", "rect": rect_of(grid_x, y, grid_w, cell_h), "parent": None})
        els.append({"id": f"titlebar-w{idx}", "kind": "titlebar", "rect": rect_of(grid_x, y, grid_w, TITLEBAR_H),
                    "parent": f"card-w{idx}"})
        tbt = labels.get(im["image"], "") if labels else ""
        avail = grid_w - 100
        t = tbt
        while len(t) > 1 and est_w(t + "…", 22) > avail:
            t = t[:-1]
        if len(t) != len(tbt):
            t = t + "…"
        if t:
            els.append({"id": f"titlebar-text-w{idx}", "kind": "titlebar-text",
                        "rect": rect_of(grid_x + 16 + 68, y, est_w(t, 22), TITLEBAR_H),
                        "parent": f"titlebar-w{idx}", "text": t, "font": 22})
        els.append({"id": f"image-w{idx}", "kind": "image", "rect": rect_of(grid_x, y + TITLEBAR_H, grid_w, img_h),
                    "parent": f"card-w{idx}", "image": im["image"]})
        disp.append({"image": im["image"], "w": round(grid_w, 1), "h": round(img_h, 1), "short": round(img_h, 1)})
        y += cell_h + GRID_GAP

    # 普通图子网格（排在极宽行下方）
    if not normal_imgs:
        return els, textW, disp
    sub_h = (STAGE_Y + STAGE_H) - y + GRID_GAP
    n = len(normal_imgs)
    picked = pick_grid_layout(normal_imgs, grid_w, sub_h)
    if picked is None:
        rows, cols = grid_rows_cols(min(n, OVERVIEW_MAX_N))
        cell_w = (grid_w - GRID_GAP * (cols - 1)) / cols
        cell_h = (sub_h - GRID_GAP * (rows - 1)) / rows
    else:
        rows, cols, cell_w, cell_h, _, _ = picked
    img_h = cell_h - TITLEBAR_H
    if cell_w <= 0 or img_h <= 0:
        return None
    for i, im in enumerate(normal_imgs):
        r, c = i // cols, i % cols
        cx = grid_x + c * (cell_w + GRID_GAP)
        cy = y + r * (cell_h + GRID_GAP)
        iw, ih = im["w"], im["h"]
        scale = min(cell_w / iw, img_h / ih)
        dw, dh = iw * scale, ih * scale
        if min(dw, dh) < GRID_MIN_SHORT_OVERVIEW:
            return None
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
        disp.append({"image": im["image"], "w": round(dw, 1), "h": round(dh, 1), "short": round(min(dw, dh), 1)})
    return els, textW, disp


def _detail_short_ok(w, h, textW):
    """逐张细节层短边 ≥320px 检查（与 P7 detail_short_ok 一致）。"""
    dispW = 680 * (w / h)
    available = RIGHT_EDGE - SAFE_L - textW - MIN_GAP
    cardW = min(dispW, available)
    img_h = 680 if cardW >= dispW else cardW * (h / w)
    return min(cardW, img_h) >= GRID_MIN_SHORT


def _degrade_eyebrow_only_short(cardW, img_h, cardH):
    """「只有眉标 + 竖长图」短边不足的降级（ST1-P4b 方案端裁定）。

    有效短边 < GRID_MIN_SHORT 时按比例放大使短边=GRID_MIN_SHORT（放大倍数上限 2.0）；
    放大到上限仍不足（短边 < GRID_MIN_SHORT/2）时保持原尺寸（居中原图、无放大/高亮）。
    返回 (cardW, img_h, cardH)。放大后卡片可能超出舞台，由调用方居中放置（可溢出裁剪）。
    """
    short = min(cardW, img_h)
    scale = GRID_MIN_SHORT / short
    if scale <= 2.0:
        cardW = cardW * scale
        img_h = img_h * scale
        cardH = TITLEBAR_H + img_h
    return cardW, img_h, cardH


def _layout_detail(shot, x_group=None, textW_group=None):
    """单图细节（逐张/单图）布局（无红框/箭头/crop）。返回 (els, textW, cardX, cardY, cardW, cardH)。"""
    eyebrow = shot.get("eyebrow", "")
    title = shot.get("title", "")
    items = shot.get("items", [])
    w, h = shot["w"], shot["h"]
    cmds = shot.get("cmds") or []
    els, _ = _sidebar_els(eyebrow, title, items, cmds, annot=shot if shot.get("annot") else None)
    textW = max([el["rect"]["w"] for el in els if el["parent"] is None] or [0])
    # ST3-P3 B2：竖长截图改用滚动显示（卡宽取满，图纵向滚动）。
    scroll = scroll_params(w, h) if is_scroll_candidate(w, h) else None
    if scroll:
        shot["scroll"] = scroll
        cardW = scroll["viewport_w"]
        cardH = scroll["viewport_h"] + TITLEBAR_H
        if x_group is None:
            natural = (RIGHT_EDGE - cardW) - (SAFE_L + textW)
            if natural > GAP_MAX:
                total = textW + GAP_MAX + cardW
                left = SAFE_L + ((RIGHT_EDGE - SAFE_L) - total) / 2
                textX = left
                cardX = left + textW + GAP_MAX
            else:
                textX = SAFE_L
                cardX = RIGHT_EDGE - cardW
        else:
            textX = x_group
            cardX = min(x_group + textW_group + GAP_MAX, RIGHT_EDGE - cardW)
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
                    "parent": "card", "image": shot["image"], "scroll": scroll})
        return els, textW, cardX, cardY, cardW, cardH
    dispW = 680 * (w / h)
    if x_group is None and not title and not items and not cmds:
        # P12c 第5种：只有眉标——眉标左上角，卡片在[眉标下沿+间距, 字幕区上沿]内垂直居中、水平居中
        available = RIGHT_EDGE - SAFE_L
        cardW = min(dispW, available)
        img_h = 680 if cardW >= dispW else cardW * (h / w)
        cardH = TITLEBAR_H + img_h
        top = STAGE_Y + EB_LH + EB_CARD_GAP
        avail_h = STAGE_H - EB_LH - EB_CARD_GAP
        if cardH > avail_h:
            scale = (avail_h - TITLEBAR_H) / img_h
            cardW = cardW * scale
            img_h = avail_h - TITLEBAR_H
            cardH = avail_h
        if min(cardW, img_h) < GRID_MIN_SHORT:
            cardW, img_h, cardH = _degrade_eyebrow_only_short(cardW, img_h, cardH)
        # ST3-P2：降级放大后卡片高度仍超出舞台可用高度时，等比缩小回可用高度上限
        # （宽高同比、不裁切不拉伸、保持居中），消除竖长 detail 截图的越界。
        if cardH > avail_h:
            scale = (avail_h - TITLEBAR_H) / img_h
            cardW = cardW * scale
            img_h = avail_h - TITLEBAR_H
            cardH = avail_h
        textX = SAFE_L
        cardX = SAFE_L + (RIGHT_EDGE - SAFE_L - cardW) / 2
        cardY = top + (avail_h - cardH) / 2
    else:
        available = RIGHT_EDGE - SAFE_L - textW - MIN_GAP
        cardW = min(dispW, available)
        cardH = TITLEBAR_H + (680 if cardW >= dispW else cardW * (h / w))
        if x_group is None:
            natural = (RIGHT_EDGE - cardW) - (SAFE_L + textW)
            if natural > GAP_MAX:
                total = textW + GAP_MAX + cardW
                left = SAFE_L + ((RIGHT_EDGE - SAFE_L) - total) / 2
                textX = left
                cardX = left + textW + GAP_MAX
            else:
                textX = SAFE_L
                cardX = RIGHT_EDGE - cardW
        else:
            textX = x_group
            cardX = min(x_group + textW_group + GAP_MAX, RIGHT_EDGE - cardW)
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
    return els, textW, cardX, cardY, cardW, cardH


def _layout_ambient(shot):
    """环境型（kind A 极宽图）全屏布局：眉标/标题在上、图 full-width、条目在下。"""
    els = []
    eyebrow = shot.get("eyebrow", "")
    title = shot.get("title", "")
    items = shot.get("items", [])
    w, h = shot["w"], shot["h"]
    annot = bool(shot.get("annot"))
    tf = (shot.get("title_font") or TITLE_FONT) if annot else TITLE_FONT
    tlh = _title_lh(tf)
    iw_of = (lambda t: shot_annotate.item_w(t)) if annot else (lambda t: est_w(t, ITEM_FONT) + DOT_W + ITEM_GAP)
    band_h = EB_LH + (tlh if title else 0)
    item_h = ITEM_LH
    gap = 24
    img_avail_h = STAGE_H - band_h - item_h - gap * 2
    y = STAGE_Y
    els.append({"id": "eyebrow", "kind": "eyebrow", "rect": rect_of(SAFE_L, y, est_w(eyebrow, EB_FONT), EB_LH),
                "parent": None, "text": eyebrow, "font": EB_FONT})
    y += EB_LH
    if title:
        tw = shot_annotate.text_w(title, tf) if annot else est_w(title, TITLE_FONT)
        els.append({"id": "title", "kind": "title", "rect": rect_of(SAFE_L, y, tw, tlh),
                    "parent": None, "text": [title], "font": tf, "lh": tlh})
        y += tlh
    s = min((RIGHT_EDGE - SAFE_L) / w, img_avail_h / h)
    dw, dh = w * s, h * s
    ix = SAFE_L + ((RIGHT_EDGE - SAFE_L) - dw) / 2
    # P12 C1（v1.2）：图+条目整体在 [眉标(及标题)下沿+间距, 字幕区上沿] 之间上下居中；块高超过区间则贴上沿
    top = STAGE_Y + band_h + gap
    avail = STAGE_H - band_h - gap
    block_h = dh + gap + item_h
    iy = top if block_h > avail else top + (avail - block_h) / 2
    els.append({"id": "image", "kind": "image", "rect": rect_of(ix, iy, dw, dh),
                "parent": None, "image": shot["image"]})
    total_w = sum(iw_of(it["text"]) for it in items) + 48 * (len(items) - 1)
    x = SAFE_L + ((RIGHT_EDGE - SAFE_L) - total_w) / 2
    iy2 = iy + dh + gap
    for i, it in enumerate(items):
        iw = iw_of(it["text"])
        els.append({"id": f"item-{i}", "kind": "item", "rect": rect_of(x, iy2, iw, item_h),
                    "parent": None, "text": it["text"], "font": ITEM_FONT})
        x += iw + 48
    return els


def _sidebar_textW(eyebrow, title, items):
    """侧栏文字宽（B4 用：同组镜头共用左边界时的组级文字宽）。"""
    els, _ = _sidebar_els(eyebrow, title, items)
    return max(el["rect"]["w"] for el in els if el["parent"] is None)


def _group_context(fns, img_by_fn):
    """图组上下文 = 首图 before + 末图 after（原文，原样摘录）。"""
    first = img_by_fn.get(fns[0], {})
    last = img_by_fn.get(fns[-1], {})
    before = (first.get("before") or "").strip()
    after = (last.get("after") or "").strip()
    return f"前文：{before}\n后文：{after}"


def _judge_one(tpl, ctx, label, prev, nar, text_call):
    """单组停留判断：返回 (related, reason)。调用失败/校验不过按 False。"""
    prompt = (tpl.replace("{context}", ctx).replace("{label}", label)
              .replace("{prev_narration}", prev).replace("{narration}", nar))
    related = False
    reason = ""
    for _ in range(2):
        content = text_call(prompt, max_tokens=256)
        if content is None:
            continue
        o, _ = parse_json(content)
        if o is None:
            continue
        if isinstance(o.get("related"), bool):
            related = o["related"]
            reason = o.get("reason", "")
            break
    return related, reason


def judge_stay_endings(im, gr, corrected, subsec, g_text, labels, text_call, cache_dir=None):
    """A 步：逐组判断"是否还在讲这组截图"。

    对每个图组，判断其后（本小节内、下一图组之前）每一组口播是否 related。
    返回 {gid: {"judgments": [{"gid","related","reason"}, ...], "stay_end_gid": gid|None}}。
    stay_end_gid = 第一个判断为 False 的组；全 True 则为 None（停到下一图组/小节结束）。
    """
    from pipeline import shot_ai
    tpl = shot_ai._load_prompt("停留判断.txt")
    body = [g for g in gr["groups"] if not g.get("starts_with_bracket_title")]
    img_by_fn = {fn_of(e): e for e in im["images"]}

    # 图组（按 corrected gid，组内按 line 序）
    g_imgs = {}
    for e in sorted(im["images"], key=lambda x: x["line"]):
        fn = fn_of(e)
        g_imgs.setdefault(corrected[fn], []).append(fn)

    appear_gids = sorted(g_imgs.keys(), key=lambda gid: min(
        g["paragraph_index"] for g in body if g["group_id"] == gid))
    gid_subsec = {}
    for nm, gids in subsec.items():
        for gid in gids:
            gid_subsec[gid] = nm
    subsec_appear = {}
    for gid in appear_gids:
        subsec_appear.setdefault(gid_subsec.get(gid), []).append(gid)

    result = {}
    for nm, gids in subsec_appear.items():
        sub_gids = subsec.get(nm, [])
        for i, gid in enumerate(gids):
            pos = sub_gids.index(gid) if gid in sub_gids else -1
            end_pos = sub_gids.index(gids[i + 1]) if i + 1 < len(gids) else len(sub_gids)
            candidates = sub_gids[pos + 1:end_pos]
            fns = g_imgs[gid]
            ctx = _group_context(fns, img_by_fn)
            label = labels.get(fns[0], "") if labels else ""
            judgments = []
            stay_end_gid = None
            for j, gid2 in enumerate(candidates):
                prev = g_text.get(sub_gids[pos + j], "")
                nar = g_text.get(gid2, "")
                related, reason = _judge_one(tpl, ctx, label, prev, nar, text_call)
                judgments.append({"gid": gid2, "related": related, "reason": reason})
                # B1 硬规则：命中指代词的组画面必须是截图，停留不在此结束
                is_refer = any(w in g_text.get(gid2, "") for w in A1_WORDS)
                if not related and stay_end_gid is None and not is_refer:
                    stay_end_gid = gid2
            result[gid] = {"judgments": judgments, "stay_end_gid": stay_end_gid}
    return result


def _compute_item_anchors(shots, g_text, sentences):
    """§16.7 条目锚点：为每个镜头的条目计算 anchor_rel（相对帧）。

    条目锚点 = 锚词（anchor）在口播中的绝对帧，从 source_quote 位置往后找 anchor；
    找不到时回退到 at_frame（相对图组起点的帧）+ 图组起点。anchor_rel = 锚点绝对帧 − 镜头起点帧。
    """
    flow = []
    for s in sentences:
        for w in s.get("words", []):
            fr = round(w.get("startTime", 0) * FPS)
            for ch in w.get("word", ""):
                flow.append((ch, fr))
    norm_flow = [(c, f) for c, f in flow if re.match(r"[\w]", c)]
    norm_text = "".join(c for c, _ in norm_flow)
    g_narr_norm = {gid: norm(t) for gid, t in g_text.items()}
    g_start = {gid: norm_text.find(nn) for gid, nn in g_narr_norm.items()}
    for s in shots:
        items = s.get("items", [])
        if not items:
            continue
        if s.get("annot"):
            # ST3-P12：逐图标注的条目自带对应句子的开始帧，不按锚词重算。
            for it in items:
                ab = it.get("anchor_abs")
                it["anchor_rel"] = (ab - s["timing"]["start_frame"]) if ab is not None else None
            continue
        gid = s["group"]
        nn = g_narr_norm.get(gid, "")
        gstart = g_start.get(gid, -1)
        prev_local = 0
        for it in items:
            sq = it.get("source_quote", "")
            ap = it.get("anchor", "")
            sq_local = nn.find(norm(sq), prev_local) if sq else -1
            if sq_local < 0:
                sq_local = prev_local
            ap_local = nn.find(norm(ap), sq_local) if ap else -1
            old_at = it.get("at_frame")
            # ST3-F2：at_frame 相对图组起点；镜头起点已改为各图锚点，不能再拿镜头起点当基准。
            old_abs = s.get("stay_appear", s["timing"]["start_frame"]) + old_at if old_at is not None else None
            if gstart >= 0 and ap_local >= 0 and gstart + ap_local < len(norm_flow):
                af = norm_flow[gstart + ap_local][1]
                prev_local = ap_local
            else:
                af = old_abs
            it["anchor_abs"] = af
            it["anchor_rel"] = (af - s["timing"]["start_frame"]) if af is not None else None


TOOL_WORDS = {"Snipaste", "iShot", "image", "微信", "Screenshot", "IMG", "Image"}


def _norm_text(s):
    """统一大小写、去空格（纯字符串，不调模型）。"""
    return re.sub(r"\s+", "", (s or "").lower())


def _norm_label(s):
    """C3/D：统一大小写、全半角、去空格。"""
    s = (s or "").lower()
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r"\s+", "", "".join(out))


def _lcs(a, b):
    """最长公共子串长度。"""
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    best = 0
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
                if dp[i][j] > best:
                    best = dp[i][j]
    return best


def _keyword_matches(kw, text):
    """C3 命中判定：完整出现，或最长公共子串 ≥ 关键词长度一半 且 ≥3 字。"""
    kw_n = _norm_text(kw)
    if not kw_n:
        return False
    text_n = _norm_text(text)
    if kw_n in text_n:
        return True
    l = _lcs(kw_n, text_n)
    return l >= 3 and l * 2 >= len(kw_n)


def _is_meaningless_filename(fn):
    """C2：去掉工具名/日期/时间/数字/分隔符后为空 → 无意义文件名。"""
    stem = re.sub(r"\.[^.]+$", "", fn)
    s = stem
    for w in TOOL_WORDS:
        s = re.sub(re.escape(w), "", s, flags=re.IGNORECASE)
    s = re.sub(r"[\d\-_\.\s]+", "", s)
    return s == ""


def image_hints(im, labels):
    """ST3-P12 R1：给标注模型参考的图片线索 {文件名: 文字}——有意义的文件名、标签链的短标签。"""
    out = {}
    for e in im.get("images", []):
        fn = fn_of(e)
        parts = []
        if not _is_meaningless_filename(fn):
            parts.append("文件名：" + re.sub(r"\.[^.]+$", "", fn))
        if (labels or {}).get(fn):
            parts.append("短标签：" + labels[fn])
        if parts:
            out[fn] = "；".join(parts)
    return out


def image_titles(im, labels):
    """ST3-P12 R1：在屏期间没有分到口播句的图（不调模型）用什么当标题 {文件名: 文字}——
    标签链的短标签；没有短标签就用有意义的文件名。"""
    out = {}
    for e in im.get("images", []):
        fn = fn_of(e)
        if (labels or {}).get(fn):
            out[fn] = labels[fn]
        elif not _is_meaningless_filename(fn):
            out[fn] = re.sub(r"\.[^.]+$", "", fn)
    return out


def _image_keywords(fn, labels):
    """C1/C2：每图关键词 = 文件名子词 + 短标签子词。"""
    kws = []
    stem = re.sub(r"\.[^.]+$", "", fn)
    for part in re.split(r"[-_\s]+", stem):
        if part:
            kws.append(part)
    label = (labels or {}).get(fn, "")
    for part in re.split(r"[-_\s]+", label):
        if part:
            kws.append(part)
    return kws


def build_timeline(inputs, stay_endings=None):
    """v1.2/v1.3(P9) 截图编排总入口。

    inputs: {
      image_map, groups_raw, sentences, direction, image_sizes, labels,
      matches_v2,   # ST3-F2：图句匹配（可选）。给了就按「讲到这张图的那句话」定每张图的期望出现帧。
      annotations,  # ST3-P12 R1：逐图标注 {文件名: {"title", "title_font", "items", "source"}}（可选）。
                    # 给了（哪怕是空字典）单图镜头的左栏就只用它，不再取按图组共用的导演链侧栏；
                    # 不给（旧调用方）保持原来的导演链侧栏。
    }
    stay_endings: P9 停留判断结果 {gid: {"judgments": [...], "stay_end_gid": gid|None}}；
      None 时按 P8 行为（停到下一图组/小节结束）。
    返回 dict：{
      shots: [...],           # 与 shot_director 的 shots 列表字段兼容
      stay_covered: set,      # 被截图停留覆盖的口播组（scene_build 跳过插画）
      mapping_fix: [...],     # D'1 越界修正清单
      image_groups: {...},    # {gid: [fn...]} 图组
      stays: [...],           # 停留窗清单（供回执）
    }
    """
    im = inputs["image_map"]
    gr = inputs["groups_raw"]
    sent = inputs["sentences"]
    direction = inputs["direction"]
    image_sizes = inputs["image_sizes"]
    labels = inputs.get("labels", {})
    # ST3-P11：文章原文（R4 判断「连着贴」）与正文里的命令（R3 命令框）。都可缺省。
    article_text = inputs.get("article_text")
    art_lines = article_text.splitlines() if article_text else None
    line_of_fn = {fn_of(e): e.get("line") or 0 for e in im["images"]}
    annotations = inputs.get("annotations")
    new_chain = annotations is not None
    annot_over = []          # 超出左栏上限没出条的标注（登记）
    # ST3-P12 第 3 步：命令去向按新规则定（含汉字一律进命令框；完整命令两行写得下才进）。
    inline_cmds = []
    box_by_fn = {}
    for c in inputs.get("inline_commands") or []:
        dest, lines, why = cmd_destination(c, c.get("near_image") in line_of_fn)
        w, h = cmd_box_size(lines)
        c = dict(c, box_lines=lines, box_w=w, box_h=h, dest_rule=dest, dest_why=why)
        inline_cmds.append(c)
        if dest == "box":
            box_by_fn.setdefault(c["near_image"], []).append(c)
    boxed = set()            # 真正进了命令框的命令 id
    sidx = None
    cmd_units = []           # 口播的句子单位（长句切成半句），给命令框定出现时间用

    body = [g for g in gr["groups"] if not g.get("starts_with_bracket_title")]
    g_text = {g["group_id"]: g["group_text"] for g in body}
    g_by_pidx = {g["paragraph_index"]: g["group_id"] for g in body}

    # D'1 小节约束映射
    corrected, mapping_fix = fix_subsection_mapping(im, gr)

    # 图组：按 corrected gid 聚合，组内按 line 序
    g_imgs = {}
    for e in sorted(im["images"], key=lambda x: x["line"]):
        fn = fn_of(e)
        gid = corrected[fn]
        g_imgs.setdefault(gid, []).append(fn)

    # 小节 → body groups（按文章顺序）
    subsec = subsection_groups(gr)
    # gid → 小节名
    gid_subsec = {}
    for nm, gids in subsec.items():
        for gid in gids:
            gid_subsec[gid] = nm

    # 组窗口（字级对齐）
    align = word_align.align_groups(sent, [{"group_id": g["group_id"], "text": g["group_text"]} for g in body])
    g_win = {r["group_id"]: (round(r["start_time"] * FPS), round(r["end_time"] * FPS)) for r in align}

    # 字幕行（字级对齐 + F3 分句），供 C 项推荐句切点 / B 项复数指代 / 点名切换用
    char_align = {r["group_id"]: r for r in salign.align_groups(
        sent, [{"group_id": g["group_id"], "text": g["group_text"]} for g in body])}
    subtitle_lines = []
    for g in body:
        ca = char_align.get(g["group_id"])
        if not ca or not ca.get("char_times"):
            continue
        for seg in _split_subtitle(ca["char_times"]):
            if not seg:
                continue
            st0 = seg[0].get("startTime")
            sf = round(st0 * FPS) if st0 is not None else 0
            subtitle_lines.append((sf, "".join(c["c"] for c in seg), g["group_id"]))
    subtitle_lines.sort(key=lambda x: x[0])

    # C 项（v1.1）输入：有 P7 插画的组集合（C5 用）
    img_gids = inputs.get("img_gids")

    # 每组在图组中的出现顺序（按文章 paragraph_index）
    appear_gids = sorted(g_imgs.keys(), key=lambda gid: min(
        g["paragraph_index"] for g in body if g["group_id"] == gid))
    appear_gids_set = set(appear_gids)

    # 小节内图组顺序
    subsec_appear = {}
    for gid in appear_gids:
        subsec_appear.setdefault(gid_subsec.get(gid), []).append(gid)

    # 计算停留窗（P9：停留终点按判断结果，False 组起恢复插画）
    stays = []       # {gid, images, appear, stay_end, subsec, stay_end_gid}
    stay_covered = set()
    partial_recover = {}      # C 项：{gid: cut_frame}，该组插画自切点起显示
    recommend_log = []        # C 项：书名号全表明细（供 C6/E4）
    pending_no_img = []       # C 项：无 P7 插画、保持截图的位置（C5 待办）
    for nm, gids in subsec_appear.items():
        sub_gids = subsec.get(nm, [])
        last_gid = sub_gids[-1]
        for i, gid in enumerate(gids):
            appear = g_win.get(gid, (0, 0))[0]
            pos = sub_gids.index(gid) if gid in sub_gids else -1
            end_pos = sub_gids.index(gids[i + 1]) if i + 1 < len(gids) else len(sub_gids)
            se = (stay_endings or {}).get(gid, {}).get("stay_end_gid")
            if se is not None and se in sub_gids and sub_gids.index(se) < end_pos:
                stay_end = g_win.get(se, (0, 0))[0]
                cover_end = sub_gids.index(se)
                stay_end_gid = se
            else:
                if i + 1 < len(gids):
                    stay_end = g_win.get(gids[i + 1], (0, 0))[0]
                else:
                    stay_end = g_win.get(last_gid, (0, 0))[1]
                cover_end = end_pos
                stay_end_gid = None
            stay_end = max(stay_end, appear + DETAIL_MIN_FRAMES)
            # C 项（v1.1）：覆盖组内的"推荐文章句"所在字幕行开始时刻 = 切点
            for k in range(pos + 1, cover_end):
                gk = sub_gids[k]
                gt = g_text.get(gk, "")
                if "《" not in gt:
                    continue
                glines = [(sf, text) for sf, text, lgid in subtitle_lines if lgid == gk]
                rec_idx = None
                for idx, (sf, text) in enumerate(glines):
                    if sf < appear:
                        continue
                    if not _is_recommend_line(text, gt):
                        continue
                    rec_idx = idx
                    break
                if rec_idx is None:
                    continue
                cut = _sentence_first_frame(glines, rec_idx)
                cut_text = glines[rec_idx][1]
                if any(w in cut_text for w in A1_WORDS):
                    # C4 指代词优先：仍显示截图，不作为切点
                    recommend_log.append({"stay": gid, "group": gk, "frame": cut,
                                          "text": cut_text, "result": "指代词优先·不切"})
                    continue
                if img_gids is not None and gk not in img_gids:
                    # C5 无 P7 插画：不结束停留，保持截图，列入待办
                    recommend_log.append({"stay": gid, "group": gk, "frame": cut,
                                          "text": cut_text, "result": "无插画·保持截图(待办)"})
                    pending_no_img.append({"group": gk, "stay": gid, "frame": cut, "text": cut_text})
                    continue
                group_end = g_win.get(gk, (0, 0))[1]
                if group_end - cut < DETAIL_MIN_FRAMES:
                    # C3 切点到组结束不足 2 秒：推迟到下一组开始
                    if k + 1 < cover_end and k + 1 < len(sub_gids):
                        nxt = sub_gids[k + 1]
                        stay_end = g_win.get(nxt, (0, 0))[0]
                        cover_end = k + 1
                        recommend_log.append({"stay": gid, "group": gk, "frame": cut,
                                              "text": cut_text, "result": f"不足2秒·推迟到 {nxt}"})
                    else:
                        recommend_log.append({"stay": gid, "group": gk, "frame": cut,
                                              "text": cut_text, "result": "不足2秒·无后续组·不结束"})
                    break
                stay_end = cut
                cover_end = k
                partial_recover[gk] = cut
                recommend_log.append({"stay": gid, "group": gk, "frame": cut,
                                      "text": cut_text, "result": "结束停留"})
                break
            # C 项（v1.1）：stay_end 之后连续无插画组（fallback 延伸）内的推荐文章句 → C5 保持截图
            for j in range(cover_end, len(sub_gids)):
                gj = sub_gids[j]
                if img_gids is not None and gj in img_gids:
                    break
                if gj in appear_gids_set:
                    break
                gt = g_text.get(gj, "")
                if "《" not in gt:
                    continue
                glines = [(sf, text) for sf, text, lgid in subtitle_lines if lgid == gj]
                rec_idx = None
                for idx, (sf, text) in enumerate(glines):
                    if not _is_recommend_line(text, gt):
                        continue
                    rec_idx = idx
                    break
                if rec_idx is None:
                    continue
                cut = _sentence_first_frame(glines, rec_idx)
                cut_text = glines[rec_idx][1]
                recommend_log.append({"stay": gid, "group": gj, "frame": cut,
                                      "text": cut_text, "result": "无插画·保持截图(待办)"})
                pending_no_img.append({"group": gj, "stay": gid, "frame": cut, "text": cut_text})
            stays.append({"gid": gid, "images": g_imgs[gid], "appear": appear,
                          "stay_end": stay_end, "subsec": nm, "stay_end_gid": stay_end_gid})
            # 覆盖组：本图组出现组之后、停留终点之前的所有组
            for k in range(pos + 1, cover_end):
                stay_covered.add(sub_gids[k])

    # A3：侧栏取用以 corrected gid 查 direction（按原始 mapped_group_id 索引，而非按文件名）
    dir_by_gid = {}
    for d in direction:
        dir_by_gid.setdefault(d["mapped_group_id"], []).append(d)

    def _sidebar_of(d):
        sb = (d or {}).get("sidebar") or {}
        eyebrow = (sb.get("eyebrow", "") or "").replace("\\", "")
        title = (sb.get("title", "") or "") if sb else ""
        items = sb.get("items", []) if sb else []
        return eyebrow, title, items

    def _group_sidebar(gid):
        """A3 规则 1：本组（corrected gid）在 direction 里 mapped_group_id==gid 的记录，
        取第一个有标题或条目的侧栏。返回 (eyebrow, title, items) 或 None。"""
        for d in dir_by_gid.get(gid, []):
            eyebrow, title, items = _sidebar_of(d)
            if title or items:
                return (eyebrow, title, items)
        return None

    def _shot_sidebar(fn, gid):
        """A：侧栏取用规则（本组 → 只眉标，取消借用）。返回 (eyebrow, title, items, borrowed, source)。"""
        sb = _group_sidebar(gid)
        if sb is not None:
            eyebrow = "" if _is_article_title(sb[0]) else sb[0]
            return eyebrow, sb[1], sb[2], False, "本组"
        nm = gid_subsec.get(gid, "") or ""
        # ST3-P3d 第2项：眉标只取小节标题，永远不用文章标题。
        nm = "" if _is_article_title(nm) else nm
        # ST3-P3c 修复三：无本组侧栏时，若标签链已生成短标签，用作左侧短标签。
        lbl = (labels or {}).get(fn, "")
        if lbl:
            return nm, "", [{"text": lbl, "anchor": "", "source_quote": "",
                             "at_frame": None}], True, "标签"
        return nm, "", [], True, "眉标"

    def _items_full(items):
        """条目透传：保留 text/anchor/source_quote/at_frame 供 §16.7 逐条淡入。"""
        return [{"text": it.get("text", ""), "anchor": it.get("anchor", ""),
                 "source_quote": it.get("source_quote", ""), "at_frame": it.get("at_frame")}
                for it in items]

    shots = []

    def _mk_shot(fn, gid, typ, layer, start, dur, images=None):
        parent = gid_subsec.get(gid, "") or ""
        ann = None
        if new_chain:
            # ST3-P12 R1：左栏 = 小节名（眉标）+ 这张图自己的标题和逐句标注。
            ann = annotations.get(fn) or {}
            eyebrow = "" if _is_article_title(parent) else parent
            title, items = ann.get("title") or "", [dict(it) for it in ann.get("items") or []]
            fb, src = False, ann.get("source") or "未生成"
        else:
            eyebrow, title, items, fb, src = _shot_sidebar(fn, gid)
            items = _items_full(items)
        w, h = image_sizes[fn]
        kind = classify_and_layout(w, h)["kind"]
        shot = {"shot_index": None, "image": fn, "group": gid, "group_orig": gid,
                "type": typ, "layer": layer,
                "eyebrow": eyebrow, "title": title, "items": items,
                "fallback": fb, "sidebar_source": src, "parent_heading": parent, "w": w, "h": h,
                "timing": {"start_frame": start, "duration_frames": dur, "lead_in": LEAD_IN,
                           "end_frame": start + dur},
                "kind": kind, "gid_images": g_imgs.get(gid, []), "sort_key": start}
        if ann is not None:
            shot["annot"] = True
            shot["title_font"] = ann.get("title_font") or TITLE_FONT
        return shot

    def _mk_grid_shot(scr, gid, fns_all, eyebrow, title, items, fb, parent, start, dur, x_group=None, textW_group=None, no_titlebar=False, src="本组"):
        w0, h0 = image_sizes[scr[0]]
        images = [{"image": f, "w": image_sizes[f][0], "h": image_sizes[f][1]} for f in scr]
        gshot = {"shot_index": None, "image": scr[0], "group": gid, "group_orig": gid,
                 "type": "grid", "layer": "overview",
                 "eyebrow": eyebrow, "title": title, "items": _items_full(items),
                 "fallback": fb, "sidebar_source": src, "parent_heading": parent, "w": w0, "h": h0,
                 "timing": {"start_frame": start, "duration_frames": dur, "lead_in": LEAD_IN,
                            "end_frame": start + dur},
                 "kind": classify_and_layout(w0, h0)["kind"], "gid_images": fns_all,
                 "sort_key": start,
                 "grid": {"images": images, "cap": OVERVIEW_MAX_N}}
        els, textW, rows, cols, cell_w, cell_h, disp = _layout_grid(gshot, labels, x_group, textW_group, no_titlebar)
        _finalize_els(els)
        gshot["elements"] = els
        gshot["grid"]["rows"] = rows
        gshot["grid"]["cols"] = cols
        gshot["grid"]["cell_w"] = cell_w
        gshot["grid"]["cell_h"] = cell_h
        gshot["grid"]["display"] = disp
        gshot["layout"] = {"textW": round(textW, 1)}
        gshot["self_check"] = {"pass": True, "errors": []}
        return gshot

    def _mk_grid_shot_wide(gid, fns_all, wide, normal, eyebrow, title, items, fb, parent, start, dur, x_group=None, textW_group=None, src="本组"):
        """P9 裁定一：极宽图独占整行的概述镜头。放不下/短边<200 时返回 None。"""
        w0, h0 = image_sizes[wide[0]]
        all_images = [{"image": f, "w": image_sizes[f][0], "h": image_sizes[f][1]} for f in (wide + normal)]
        wide_imgs = [{"image": f, "w": image_sizes[f][0], "h": image_sizes[f][1]} for f in wide]
        normal_imgs = [{"image": f, "w": image_sizes[f][0], "h": image_sizes[f][1]} for f in normal]
        gshot = {"shot_index": None, "image": wide[0], "group": gid, "group_orig": gid,
                 "type": "grid", "layer": "overview",
                 "eyebrow": eyebrow, "title": title, "items": _items_full(items),
                 "fallback": fb, "sidebar_source": src, "parent_heading": parent, "w": w0, "h": h0,
                 "timing": {"start_frame": start, "duration_frames": dur, "lead_in": LEAD_IN,
                            "end_frame": start + dur},
                 "kind": classify_and_layout(w0, h0)["kind"], "gid_images": fns_all,
                 "sort_key": start, "grid": {"images": all_images, "cap": OVERVIEW_MAX_N}}
        r = _layout_grid_wide(gshot, wide_imgs, normal_imgs, labels, x_group, textW_group)
        if r is None:
            return None
        els, textW, disp = r
        _finalize_els(els)
        gshot["elements"] = els
        gshot["grid"]["display"] = disp
        gshot["layout"] = {"textW": round(textW, 1)}
        gshot["self_check"] = {"pass": True, "errors": []}
        return gshot

    def _cmd_anchor(c, img_frame):
        """命令框的出现帧 = 它对应的口播句的开始帧（只在这张图锚点句的前后几句里找）。找不到返回 None。"""
        if sidx is None or not sidx["sentences"]:
            return None
        si = allocate._sentence_at(sidx, img_frame)
        lo, hi = max(0, si - CMD_NEAR_SENTS), min(len(sidx["sentences"]) - 1, si + CMD_NEAR_SENTS)
        for text in (c.get("context"), c.get("sentence")):
            hit, ratio = allocate._fuzzy(sidx, text, lo, hi)
            if hit is not None and ratio >= allocate.MATCH_MIN_RATIO:
                s = sidx["sentences"][hit]
                # ST3-P12：这句话比较长、被切成了半句，而且口播里念到了这条命令：从念到它的那半句开始出现
                # （和那半句的标注同时出现，排在它正下方）。口播没念命令的，仍从整句开始。
                key = allocate.norm(c.get("text"))
                for u in cmd_units:
                    if s["frame"] <= u["start"] < s["end_frame"] and key and key in allocate.norm(u["text"]):
                        return u["start"]
                return s["frame"]
        return None

    def _detail(fn, gid, start, dur, x_group, textW_group, cmds, keep=None):
        s = _mk_shot(fn, gid, "detail", "detail", round(start), round(dur))
        if keep is not None:
            s["items"] = [dict(it) for it in keep]     # ST3-P12：只排左栏放得下的那几条
        if cmds:
            s["cmds"] = cmds
            x_group = textW_group = None     # 带命令框的镜头自己排，不跟同组共用左边界
        els, textW, cardX, cardY, cardW, cardH = _layout_detail(s, x_group, textW_group)
        s["layout"] = {"textW": round(textW, 1), "cardX": round(cardX, 1), "cardY": round(cardY, 1),
                       "cardW": round(cardW, 1), "cardH": round(cardH, 1), "v2": False, "crop": None}
        return s, els

    def _mk_detail_or_ambient(fn, gid, start, dur, kind, x_group=None, textW_group=None):
        # ST3-P11 R2：一律左标注右截图；只有条状图（宽高比 > 5 且左右版式下高度 < 250）保留整图。
        w, h = image_sizes[fn]
        cmds = [dict(c, anchor_abs=_cmd_anchor(c, round(start))) for c in box_by_fn.get(fn, [])]
        keep, over = None, []
        if new_chain:
            # ST3-P12 R1：条数上限按左栏剩余高度算，命令框占的高度先扣掉；自己排，不跟同组共用左边界。
            x_group = textW_group = None
            probe = _mk_shot(fn, gid, "detail", "detail", round(start), round(dur))
            cap = sidebar_capacity(probe["eyebrow"], probe["title"], probe.get("title_font"), cmds)
            if cap < 0:
                cmds, cap = [], sidebar_capacity(probe["eyebrow"], probe["title"], probe.get("title_font"), [])
            keep, over = cap_items(probe["items"], cap)
        s, els = _detail(fn, gid, start, dur, x_group, textW_group, cmds, keep)
        if cmds and not check_elements(_finalize_els(els))[0]:
            cmds = []                        # 命令框放不下（条目太多）：这几条命令改出卡片
            s, els = _detail(fn, gid, start, dur, x_group, textW_group, cmds, keep)
        img_h = next(el["rect"]["h"] for el in els if el["kind"] == "image")
        # ST3-P12：条状图左栏什么都没有（没有标注、没有命令框）时没有「左右版式」可言，也按整图版式排。
        bare_strip = new_chain and w / h > STRIP_ASPECT and not (s["title"] or s["items"] or cmds)
        if is_strip(w, h, img_h) or bare_strip:
            s = _mk_shot(fn, gid, "ambient", "ambient", round(start), round(dur))
            if new_chain:
                # 整图版式（条状图）：标注在图下方，最多 2 条。
                keep, over = cap_items(s["items"], AMBIENT_MAX_ITEMS)
                s["items"] = [dict(it) for it in keep]
            els = _layout_ambient(s)
            s["layout"] = {}
            cmds = []                        # 整图版式没有左栏，命令改出卡片
        for it in over:
            annot_over.append({"image": fn, "text": it.get("text"), "quote": it.get("quote"),
                               "anchor_abs": it.get("anchor_abs"), "reason": "超出左栏上限"})
        boxed.update(c["id"] for c in cmds)
        _finalize_els(els)
        s["elements"] = els
        s["self_check"] = {"pass": True, "errors": []}
        return s

    # ST3-F2：每张截图的期望出现帧 = 讲到它的那句话的开始帧。
    # 来源依次：matches_v2 口播句 → 原文前文对应的口播句 → 按原文顺序在所属小节内等比分布。
    # 挂在哪个口播组不再决定出现时间（图常被挂到 1～2 秒的引子组上）。
    matches_v2 = inputs.get("matches_v2")
    img_anchor = {}
    if matches_v2 is not None:
        char_rows = [(g["group_id"], (char_align.get(g["group_id"]) or {}).get("char_times") or []) for g in body]
        sidx = allocate.sentence_index(char_rows, FPS)
        cmd_units = shot_annotate.build_units(char_rows, FPS)
        sub_win = {}
        for nm, gids in subsec.items():
            wins = [g_win[x] for x in gids if x in g_win]
            if wins:
                sub_win[nm] = (min(w[0] for w in wins), max(w[1] for w in wins))
        m_all = matches_v2.get("matches") or {}
        img_anchor = allocate.resolve_anchors([
            {"key": fn_of(e), "line": e["line"], "match": m_all.get(fn_of(e)),
             "texts": [e.get("before") or "", e.get("after") or ""],
             "fallback": g_win.get(corrected[fn_of(e)], (0, 0))[0],
             "window": sub_win.get(gid_subsec.get(corrected[fn_of(e)]))}
            for e in sorted(im["images"], key=lambda x: x["line"])], sidx)

    def _want(fn, i, n, appear, T, named):
        """一张图的期望出现帧与依据。无 matches_v2（老样本）时：点名命中 → 停留窗内按顺序等分。"""
        a = img_anchor.get(fn)
        if a is not None and a["basis"] != "等比分布":
            return a["frame"], a["basis"]
        if fn in named:
            return named[fn], "点名"
        if a is not None:
            return a["frame"], a["basis"]
        return appear + round(i * T / n), "图组起点" if n == 1 else "停留窗等分"

    # C4：切换明细与无意义文件名（subtitle_lines 已在停留窗计算前构建）
    switch_log = []       # 每图组点名的切换明细（供 E2）
    meaningless_fns = set()  # 无意义文件名（供 E2）

    for st in stays:
        gid = st["gid"]
        fns = st["images"]
        appear = st["appear"]
        stay_end = st["stay_end"]
        T = stay_end - appear
        N = len(fns)
        eyebrow, title, items, fb, src = _shot_sidebar(fns[0], gid)
        parent = gid_subsec.get(gid, "") or ""
        kind_of = {fn: classify_and_layout(image_sizes[fn][0], image_sizes[fn][1])["kind"] for fn in fns}
        wide = [fn for fn in fns if image_sizes[fn][0] / image_sizes[fn][1] > GRID_MAX_ASPECT]
        normal = [fn for fn in fns if fn not in wide]

        if N == 1:
            fn = fns[0]
            af, basis = _want(fn, 0, 1, appear, T, {})
            s = _mk_detail_or_ambient(fn, gid, af, max(DETAIL_MIN_FRAMES, stay_end - af), kind_of[fn])
            s["stay_appear"] = appear
            s["alloc"] = {"role": "detail", "stay": gid, "anchor": af, "basis": basis,
                          "in_overview": False}
            shots.append(s)
            continue

        # C3/D：同组非空短标签单一判定（统一大小写、全半角、去空格；空标签不参与"相同"比较）
        _lbls = {_norm_label(labels.get(fn) or "") for fn in fns if (labels.get(fn) or "")}
        labels_all_same = (len(fns) >= 2 and len(_lbls) == 1)

        # C1/C2/C3：每图关键词（文件名子词 + 短标签子词；C3 标签全相同时只用文件名）
        kws = {}
        for fn in fns:
            stem = re.sub(r"\.[^.]+$", "", fn)
            kws[fn] = [p for p in re.split(r"[-_\s]+", stem) if p]
            if not labels_all_same:
                for p in re.split(r"[-_\s]+", (labels or {}).get(fn, "")):
                    if p:
                        kws[fn].append(p)
        if len(fns) > 1:
            # 去共有词（去共有前后缀），避免并列命中
            common = set(kws[fns[0]])
            for fn in fns[1:]:
                common &= set(kws[fn])
            for fn in fns:
                kws[fn] = [w for w in kws[fn] if w not in common]

        # A 项：排除与标题/眉标相同的子词。
        # A1：多子词文件名中的标题/眉标词不参与；A2：完整文件名（去共有前后缀后的单个子词）照常参与；
        # A3：短标签子词与标题/眉标相同不参与。
        _title_n = _norm_label(gid_subsec.get(gid, "") or "")
        _eb_n = _norm_label(eyebrow or "")
        _exclude_n = {_title_n, _eb_n} - {""}
        if _exclude_n:
            for fn in fns:
                stem = re.sub(r"\.[^.]+$", "", fn)
                fparts = [p for p in re.split(r"[-_\s]+", stem) if p]
                kws[fn] = [w for w in kws[fn] if _norm_label(w) not in _exclude_n or len(fparts) == 1]

        # C1/C5：找候选命中（复数指代行跳过；并列命中即时记"不切·并列"）
        candidates = []
        for sf, text, _gid in subtitle_lines:
            if sf < appear or sf >= stay_end:
                continue
            if any(w in text for w in PLURAL_WORDS):
                switch_log.append({"gid": gid, "frame": sf, "image": None, "keyword": None,
                                   "text": text, "result": "不切·复数指代"})
                continue
            hits = []
            for fn in fns:
                for kw in kws[fn]:
                    if _keyword_matches(kw, text):
                        hits.append((fn, kw))
                        break
            if len(hits) == 1:
                candidates.append((sf, hits[0][0], hits[0][1], text))
            elif len(hits) > 1:
                switch_log.append({"gid": gid, "frame": sf, "image": None, "keyword": None,
                                   "text": text, "result": "不切·并列"})

        # 相邻同图合并（重复命中记"不切·当前已显示"）
        merged = []
        for sf, fn, kw, text in candidates:
            if merged and merged[-1][1] == fn:
                switch_log.append({"gid": gid, "frame": sf, "image": fn, "keyword": kw,
                                   "text": text, "result": "不切·当前已显示"})
            else:
                merged.append((sf, fn, kw, text))
        candidates = merged
        named = {}
        for sf, fn, kw, text in candidates:
            named.setdefault(fn, sf)

        # 概述层（网格）是否存在：沿用按图片类型的判断——极宽图（宽高比 > GRID_MAX_ASPECT）不进普通网格，
        # 普通图 ≥2 张才有概述；极宽+普通混排先试「极宽图独占一行」的宽网格。
        # ST3-P11 R4：只有原文里连着贴的 ≥2 张图才出网格（图与图之间只有空行，没有正文）。
        runs = [r for r in adjacent_runs(fns, line_of_fn, art_lines) if len(r) >= 2]
        runs = [r for r in runs
                if (any(f in wide for f in r) and any(f in normal for f in r))
                or sum(1 for f in r if f in normal) >= 2]
        has_overview = bool(runs)

        # B4：预计算同组侧栏共用左边界 X_group（只作用于有侧栏的组）。
        # P12c A2：只有眉标（无标题、无条目）的组不共用左边界，各镜头走 C2 水平居中。
        if title or items:
            textW_group = _sidebar_textW(eyebrow, title, items)
            cardWs = []
            if has_overview:
                cardWs.append(RIGHT_EDGE - SAFE_L - textW_group - GAP_MAX)
            for fn in fns:
                if image_sizes[fn][0] / image_sizes[fn][1] <= STRIP_ASPECT:
                    w, h = image_sizes[fn]
                    dispW = 680 * (w / h)
                    available = RIGHT_EDGE - SAFE_L - textW_group - MIN_GAP
                    cardWs.append(min(dispW, available))
            maxCardW = max(cardWs) if cardWs else textW_group
            x_group = max(SAFE_L, SAFE_L + (RIGHT_EDGE - SAFE_L - (textW_group + GAP_MAX + maxCardW)) / 2)
        else:
            x_group = None
            textW_group = None

        # 概述层（网格）。ST3-F2：这里只产出「期望时间」，是否真的上屏由分配器（pipeline/allocate.py）定：
        # 逐张排得下时概述是可选项（它前面有 ≥2 秒空档才出现）；本章节借完仍排不下逐张时，改用概述网格。
        # 超过 OVERVIEW_MAX_N 张一律分屏，不截断。
        wants = sorted(((_want(fn, i, N, appear, T, named), i, fn) for i, fn in enumerate(fns)),
                       key=lambda x: (x[0][0], x[1]))
        want_of = {fn: w[0] for w, _i, fn in wants}
        in_overview = {}                     # 文件名 → 所属连排段的标识
        for run in runs:
            r_wide = [f for f in run if f in wide]
            r_normal = [f for f in run if f in normal]
            run_key = f"{gid}#{run[0]}"
            # 出现时间：前后有统称句（锚点句或它的前一句带复数指代）→ 从统称句开始；
            # 没有 → 从本段第一张图的锚点开始。
            first = min(want_of[f] for f in run)
            g_anchor, g_basis = first, "首图锚点"
            if sidx is not None and sidx["sentences"]:
                si = allocate._sentence_at(sidx, first)
                for j in (si - 1, si):          # 前一句和锚点句都是统称句时，从更早的那句开始
                    if j >= 0 and any(allocate.norm(w) in sidx["sentences"][j]["norm"] for w in PLURAL_WORDS):
                        g_anchor, g_basis = sidx["sentences"][j]["frame"], "统称句"
                        break
            overview = []
            if r_wide and r_normal:
                wshot = _mk_grid_shot_wide(gid, fns, r_wide, r_normal, eyebrow, title, items, fb, parent,
                                           g_anchor, OVERVIEW_DUR_FRAMES, x_group, textW_group, src)
                if wshot is not None:
                    overview.append(wshot)
            if not overview and len(r_normal) >= 2:
                k = max(1, (len(r_normal) + OVERVIEW_MAX_N - 1) // OVERVIEW_MAX_N)
                for i, scr in enumerate(_split_screens(r_normal, k)):
                    overview.append(_mk_grid_shot(scr, gid, fns, eyebrow, title, items, fb, parent,
                                                  g_anchor + i * OVERVIEW_DUR_FRAMES, OVERVIEW_DUR_FRAMES,
                                                  x_group, textW_group, src=src))
            for o in overview:
                for d in o["grid"]["display"]:
                    in_overview[d["image"]] = run_key
                o["stay_appear"] = appear
                o["alloc"] = {"role": "overview", "stay": run_key, "anchor": o["timing"]["start_frame"],
                              "basis": g_basis}
                shots.append(o)

        # 逐张：每张图都有自己的镜头，期望出现帧见 _want。
        for sf, fn, kw, text in candidates:
            switch_log.append({"gid": gid, "frame": sf, "image": fn, "keyword": kw,
                               "text": text, "result": "点名"})
        for k, ((af, basis), _i, fn) in enumerate(wants):
            nxt = wants[k + 1][0][0] if k + 1 < len(wants) else stay_end
            dur = max(DETAIL_MIN_FRAMES, nxt - af)
            s = _mk_detail_or_ambient(fn, gid, af, dur, kind_of[fn], x_group, textW_group)
            s["stay_appear"] = appear
            s["alloc"] = {"role": "detail", "stay": in_overview.get(fn, gid), "anchor": af, "basis": basis,
                          "in_overview": fn in in_overview}
            shots.append(s)

    # C4：全片无意义文件名判定（覆盖所有图，含单图组）
    for e in im["images"]:
        fn = fn_of(e)
        if _is_meaningless_filename(fn):
            meaningless_fns.add(fn)

    _compute_item_anchors(shots, g_text, sent)

    shots.sort(key=lambda s: s["sort_key"])
    g_seq = {}
    for i, s in enumerate(shots):
        s["shot_index"] = i
        gid = s["group"]
        s["group_seq"] = g_seq.get(gid, 0)
        g_seq[gid] = g_seq.get(gid, 0) + 1
        ok, errs = check_elements(s["elements"])
        if not ok:
            s["self_check"] = {"pass": False, "errors": errs}
        # 渲染层（ShotCheck/ShotSampleReel 合成）需要的字段
        s["total_frames"] = s["timing"]["duration_frames"] + LEAD_IN
        s["audio_start_frame"] = s["timing"]["start_frame"]
        s["lead_in"] = LEAD_IN
        s["subtitle_lines"] = []

    return {"shots": shots, "stay_covered": stay_covered, "mapping_fix": mapping_fix,
            "image_groups": g_imgs, "stays": stays, "corrected": corrected,
            "switch_log": switch_log, "meaningless_fns": sorted(meaningless_fns),
            "partial_recover": partial_recover, "recommend_log": recommend_log,
            "pending_no_img": pending_no_img,
            # ST3-P11 R3：没进命令框的正文命令（长命令、旁边没有截图、命令框放不下）一律出卡片。
            "inline_cards": [c for c in inline_cmds if c.get("id") not in boxed],
            "inline_boxed": sorted(boxed),
            # ST3-P12：每条命令的上屏文字、命令框里的宽度和行数、去向；超出左栏上限没出条的标注。
            "inline_log": [{"id": c.get("id"), "line": c.get("line"), "text": c.get("text"),
                            "box_w": c.get("box_w"), "box_lines": c.get("box_lines"),
                            "near_image": c.get("near_image"), "rule": c.get("dest_rule"),
                            "why": c.get("dest_why"),
                            "dest": "命令框" if c.get("id") in boxed else "卡片"} for c in inline_cmds],
            "annot_over": annot_over}
