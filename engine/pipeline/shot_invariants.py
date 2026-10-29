# -*- coding: utf-8 -*-
"""不变量检查器（SHOT-M5-P8 A）：对全片做程序化检查，输出七条不变量的违反清单。

只读检查，不改任何数据。输入全片场景数据 + 字级时间戳 + 分组 + 截图清单 + 镜头，
输出 {invariant: [violation, ...]}，violation 位置用 分:秒。
"""
import re
import sys
from pathlib import Path

# 引擎根随代码走（ST1-P4a3）：仓库根由本文件位置推出，不写死主仓路径。
_REPO = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "R148"))

import r148_subtitle_align as salign

from pipeline.scene_build import _split_subtitle
from pipeline.shot_timeline import PLURAL_WORDS, _is_recommend_line, _sentence_first_frame, DETAIL_MIN_FRAMES

FPS = 30
MIN_SHOT_FRAMES = 60        # 2 秒底线
OVERVIEW_MIN_SHORT = 200
DETAIL_MIN_SHORT = 320

# 指代截图词表 v1.0（P2 补齐后的旧版，含"这个报错""这张报错"等泛指）
A1_WORDS = ["下面这张图", "下面这些图", "这张图", "这些图", "这张截图", "如图", "如下图",
            "看一下这张", "从后台日志", "从日志", "这个报错", "下面这个", "上面这张",
            "下面这两张图", "下面这几张图", "下面这些报错", "下面这张", "下图", "这几张图", "这张报错",
            "比如下图", "下图这种", "这张报错信息"]

# 指代截图词表 v1.1（A3 收窄，只保留明确指向画面的说法，去掉泛指）+ E'1 补「下面是」「像下面」
A1_WORDS_V2 = ["下面这张图", "下面这两张图", "下面这几张图", "下面这些图", "下面这些报错截图",
               "下面这张", "下图", "如图", "如下图", "这张截图", "这张图", "这些图", "这几张图",
               "比如下图", "下图这种", "上面这张", "下面的截图", "下面的答复", "下面是", "像下面"]


def f2t(f):
    if f is None:
        return None
    m = int(f // 1800)
    s = round((f % 1800) / 30, 1)
    if s >= 60.0:
        m += 1
        s -= 60.0
    return f"{m}:{s:04.1f}"


def _t2f(t):
    return int(round(t * FPS))


def _a1_hits(char_times, word_list=None):
    """在 char_times 里找指代词表命中，返回 [(start_frame, word)]（长词优先、区间去重）。"""
    chars = "".join(ct["c"] for ct in char_times)
    words = sorted(word_list or A1_WORDS_V2, key=len, reverse=True)
    hits = []
    covered = [False] * len(chars)
    for w in words:
        start = 0
        while True:
            pos = chars.find(w, start)
            if pos < 0:
                break
            if not any(covered[pos:pos + len(w)]):
                st = char_times[pos]["startTime"]
                if st is not None:
                    hits.append((_t2f(st), w))
                for k in range(pos, pos + len(w)):
                    covered[k] = True
            start = pos + 1
    return sorted(hits)


def _shot_images(shot):
    if shot.get("type") == "grid":
        return [x["image"] for x in shot.get("grid", {}).get("images", [])]
    if shot.get("type") == "parallel":
        return [x["image"] for x in shot.get("parallel", {}).get("images", [])]
    if shot.get("image"):
        return [shot["image"]]
    return []


def _effective_durations(scenes):
    """按 scenes 渲染顺序（后面的覆盖前面的）计算每个 screenshot 场景的有效显示时长（帧）。
    返回 {scenes_index: effective_frames}。被切换挤成几帧的镜头（被后续场景覆盖）有效时长 < 时长。"""
    eff = {}
    for i, sc in enumerate(scenes):
        if sc.get("type") != "screenshot":
            continue
        d = sc["end_frame"] - sc["start_frame"]
        for j in range(i + 1, len(scenes)):
            other = scenes[j]
            if other.get("type") != "screenshot":
                continue
            if other["start_frame"] < sc["end_frame"] and other["end_frame"] > sc["start_frame"]:
                overlap = min(sc["end_frame"], other["end_frame"]) - max(sc["start_frame"], other["start_frame"])
                if overlap > 0:
                    d -= overlap
        eff[i] = max(0, d)
    return eff


def check_invariants(scenes, sentences, groups, image_map, shots, explain_points=None, manifest=None, word_list=None, stay_covered=None, fallback_groups=None, direction=None):
    """七条不变量检查。返回 {invariant: [violation, ...]}。

    scenes: video_data["scenes"]。
    sentences: 豆包 sentences（字级时间戳）。
    groups: groups_raw["groups"]。
    image_map: image_map.json。
    shots: shots_plan["shots"]。
    explain_points: {image: [(frame, keyword)]}，I3 用；None 时用 shots 的 anchor_frame 兜底。
    manifest: AI 插画清单（I5 用）。
    word_list: I1 指代词表（默认 A1_WORDS_V2）。
    stay_covered: 被截图停留覆盖的口播组（I5 用，SHOT-M5-P8 v1.2）。
    fallback_groups: 需要恢复插画但无 P7 插画、沿用前一张画面的组（I5 用，SHOT-M5-P9 裁定二）。
    """
    body = [g for g in groups if not g.get("starts_with_bracket_title")]
    char_align = {r["group_id"]: r for r in salign.align_groups(
        sentences, [{"group_id": g["group_id"], "text": g["group_text"]} for g in body])}

    screenshot_scenes = [s for s in scenes if s.get("type") == "screenshot"]
    # 场景 -> 包含的图
    def scene_images(sc):
        sh = sc.get("shot") or {}
        return _shot_images(sh)

    v = {f"I{i}": [] for i in range(1, 18)}

    # I1 字幕指到图 → 画面有截图
    for g in body:
        gid = g["group_id"]
        ca = char_align.get(gid)
        if not ca or not ca.get("char_times"):
            continue
        for frame, word in _a1_hits(ca["char_times"], word_list):
            covered = any(sc["start_frame"] <= frame < sc["end_frame"] for sc in screenshot_scenes)
            if not covered:
                v["I1"].append({"group": gid, "frame": frame, "word": word, "text": g["group_text"]})

    # I2 每张截图至少出现在一个实际显示时长 ≥2 秒的镜头里（按最终渲染数据 scenes）
    eff = _effective_durations(scenes)
    img_eff = {}
    for i, sc in enumerate(scenes):
        if sc.get("type") != "screenshot":
            continue
        if eff.get(i, 0) < MIN_SHOT_FRAMES:
            continue
        for img in scene_images(sc):
            img_eff[img] = max(img_eff.get(img, 0), eff[i])
    all_imgs = [__import__("os").path.basename(e["image_path"]).replace("%20", " ") for e in image_map["images"]]
    for fn in all_imgs:
        if img_eff.get(fn, 0) < MIN_SHOT_FRAMES:
            v["I2"].append({"image": fn, "reason": f"无 ≥2 秒上屏（有效时长 {img_eff.get(fn, 0)} 帧）"})

    # I3 讲解点被覆盖（关键词帧落在包含该图的镜头内，误差 ≤1s）
    points = explain_points
    if points is None:
        # 兜底：用 shots 里 detail 镜头的 anchor_frame
        points = {}
        for s in shots:
            if s.get("layer") == "detail" and s.get("anchor_frame") is not None and s.get("image"):
                points.setdefault(s["image"], []).append((s["anchor_frame"], None))
    for img, pts in (points or {}).items():
        for frame, keyword in pts:
            if frame is None:
                continue
            covered = any(
                img in scene_images(sc) and sc["start_frame"] - FPS <= frame < sc["end_frame"] + FPS
                for sc in screenshot_scenes)
            if not covered:
                v["I3"].append({"image": img, "frame": frame, "keyword": keyword})

    # I4 画面约束：有效显示时长 <2 秒的截图镜头（含被切换挤成几帧的镜头）
    for i, sc in enumerate(scenes):
        if sc.get("type") != "screenshot":
            continue
        if eff.get(i, 0) < MIN_SHOT_FRAMES:
            sh = sc.get("shot", {})
            imgs = _shot_images(sh)
            v["I4"].append({"group": sc.get("group_id"), "image": imgs[0] if imgs else None,
                            "reason": f"有效显示时长 {eff.get(i, 0)} 帧 < {MIN_SHOT_FRAMES}"})
    for s in shots:
        if s.get("layer") == "overview":
            for d in s.get("grid", {}).get("display", []):
                if d["short"] < OVERVIEW_MIN_SHORT:
                    v["I4"].append({"group": s["group"], "image": d["image"], "reason": f"概述短边 {d['short']} < {OVERVIEW_MIN_SHORT}"})
        if s.get("layer") == "detail":
            el = next((e for e in s.get("elements", []) if e.get("kind") == "image"), None)
            if el and min(el["rect"]["w"], el["rect"]["h"]) < DETAIL_MIN_SHORT:
                v["I4"].append({"group": s["group"], "image": s.get("image"), "reason": "细节短边 < 320"})

    # I5 无白屏（正文位置既无截图也无插画）
    scene_gids = {sc.get("group_id") for sc in scenes if sc.get("group_id")}
    from pipeline import routing
    transition_ids, func_short_ids = routing.identify_no_image_groups(groups)
    abstain_ids = {r["group_id"] for r in (manifest or []) if r.get("path") == "不出图(弃权)"}
    no_img = transition_ids | func_short_ids | abstain_ids
    stay_ids = set(stay_covered or [])
    fallback_ids = set(fallback_groups or [])
    for g in body:
        gid = g["group_id"]
        if gid in no_img or gid in stay_ids or gid in fallback_ids:
            continue
        if gid in scene_gids:
            continue
        # P12 B2：短插画并入相邻插画后，该组场景被移除，但其时间窗被相邻场景覆盖，不算白屏
        ca = char_align.get(gid)
        if ca and ca.get("start_time") is not None:
            st = round(ca["start_time"] * FPS)
            en = round(ca["end_time"] * FPS)
            if any(sc["start_frame"] <= st and en <= sc["end_frame"] for sc in scenes):
                continue
        v["I5"].append({"group": gid, "text": g["group_text"]})

    # I6 红框用得有理由（只出现在"时间不够、改为概述层里逐张点到"；其余一律违规）
    for s in shots:
        for hl in s.get("overview_highlights", []):
            # 基线（P7）：红框是 P5「概述层高亮替代放弃」旧机制，一律记为违规（待第二段 D3 重建）
            v["I6"].append({"group": s["group"], "image": hl["image"],
                            "reason": "旧机制红框（概述层高亮替代放弃），非 D3 时间不够改红框"})

    # I8 侧栏完整（SHOT-M5-P11）
    if direction:
        import os
        dir_by_fn = {d["image_ref"]: d for d in direction}
        subsec_has_sidebar = {}
        dir_items_by_gid = {}
        for e in image_map["images"]:
            fn = os.path.basename(e["image_path"]).replace("%20", " ")
            d = dir_by_fn.get(fn)
            sb = (d or {}).get("sidebar") or {}
            if sb.get("eyebrow") or sb.get("title") or sb.get("items"):
                subsec_has_sidebar[e["nearest_heading"]] = True
            gid = (d or {}).get("mapped_group_id")
            if gid:
                dir_items_by_gid[gid] = sb.get("items", [])
        for s in shots:
            if s.get("type") not in ("grid", "detail", "ambient"):
                continue
            heading = s.get("parent_heading", "")
            # 子检查 1：小节有眉标/标题，但镜头 eyebrow/title 全空
            if subsec_has_sidebar.get(heading) and not (s.get("eyebrow") or s.get("title")):
                v["I8"].append({"group": s["group"], "image": s.get("image"),
                                "reason": "小节有眉标/标题但镜头无侧栏"})
            # 子检查 2：方向条目 ≥2 且 at_frame 不同，但镜头条目 anchor_rel 全 None/相同
            dir_items = dir_items_by_gid.get(s.get("group"), [])
            dir_ats = [it.get("at_frame") for it in dir_items if it.get("at_frame") is not None]
            if len(dir_items) >= 2 and len(set(dir_ats)) >= 2:
                shot_items = s.get("items", [])
                rels = [it.get("anchor_rel") for it in shot_items]
                if all(r is None for r in rels) or (len(rels) >= 2 and all(r is not None for r in rels) and len(set(rels)) == 1):
                    v["I8"].append({"group": s["group"], "image": s.get("image"),
                                    "reason": "条目锚点时刻不同但淡入起点全相同"})

    # I9 概述不回返（SHOT-M5-P11）：一次停留内，概述层镜头只能出现在开头
    from collections import defaultdict
    g_shots = defaultdict(list)
    for s in shots:
        if s.get("type") in ("grid", "detail", "ambient"):
            g_shots[s["group"]].append(s)
    for gid, ss in g_shots.items():
        ss.sort(key=lambda x: x["timing"]["start_frame"])
        seen_grid = False
        seen_detail_after_grid = False
        for s in ss:
            if s["type"] == "grid":
                if seen_detail_after_grid:
                    v["I9"].append({"group": gid, "image": s.get("image"), "reason": "概述层回返"})
                seen_grid = True
            else:
                if seen_grid:
                    seen_detail_after_grid = True

    # I10 侧栏来源合法（SHOT-M5-P11d）：来源只能是本组；本组没有则只有眉标（眉标/标题/条目同源）
    if direction:
        import os
        from pipeline.shot_timeline import subsection_groups, fix_subsection_mapping
        corrected, _ = fix_subsection_mapping(image_map, {"groups": groups})
        subsec = subsection_groups({"groups": groups})
        gid_subsec = {}
        for nm, gids in subsec.items():
            for gid in gids:
                gid_subsec[gid] = nm
        dir_by_gid = {}
        for d in direction:
            dir_by_gid.setdefault(d["mapped_group_id"], []).append(d)

        def _sb(d):
            sb = (d or {}).get("sidebar") or {}
            return ((sb.get("eyebrow", "") or "").replace("\\", ""),
                    (sb.get("title", "") or "") if sb else "",
                    sb.get("items", []) if sb else [])

        def _gsb(gid):
            for d in dir_by_gid.get(gid, []):
                e, t, its = _sb(d)
                if t or its:
                    return (e, t, its)
            return None

        for s in shots:
            if s.get("type") not in ("grid", "detail", "ambient"):
                continue
            gid = s["group"]
            nm = gid_subsec.get(gid, "")
            legal = _gsb(gid)
            shot_eb = s.get("eyebrow", "") or ""
            shot_t = s.get("title", "") or ""
            shot_items = [it.get("text", "") for it in s.get("items", [])]
            if legal is not None:
                e, t, its = legal
                legal_items = [it.get("text", "") for it in its]
                if shot_eb != e or shot_t != t or shot_items != legal_items:
                    v["I10"].append({"group": gid, "image": s.get("image"),
                                     "reason": f"侧栏不同源（应来自本组，实际眉标 {shot_eb!r}/标题 {shot_t!r}/条目 {shot_items!r}）"})
            else:
                if shot_t or shot_items:
                    v["I10"].append({"group": gid, "image": s.get("image"),
                                     "reason": f"无合法来源却显示标题/条目（应只眉标 {nm}）"})
                if shot_eb != nm:
                    v["I10"].append({"group": gid, "image": s.get("image"),
                                     "reason": f"眉标 {shot_eb!r} ≠ 小节名 {nm!r}"})

    # I13 复数指代行期间画面是概述（SHOT-M5-P11d）：有概述层的图组，其停留期间每一行复数指代字幕，
    # 按最终渲染数据检查该行显示期间的镜头类型均为概述层（grid）。
    grid_groups = {s["group"] for s in shots if s.get("type") == "grid"}
    for g in body:
        gid = g["group_id"]
        if gid not in grid_groups:
            continue
        ca = char_align.get(gid)
        if not ca or not ca.get("char_times"):
            continue
        for seg in _split_subtitle(ca["char_times"]):
            txt = "".join(c["c"] for c in seg)
            if not any(w in txt for w in PLURAL_WORDS):
                continue
            sf = round(seg[0]["startTime"] * FPS)
            cover_type = None
            for s in shots:
                if s["group"] == gid and s["timing"]["start_frame"] <= sf < s["timing"]["end_frame"]:
                    cover_type = s["type"]
                    break
            if cover_type is not None and cover_type != "grid":
                v["I13"].append({"group": gid, "frame": sf,
                                 "reason": f"复数指代行期间镜头类型 {cover_type} ≠ 概述层（{txt[:16]}）"})

    # I14 推荐文章句不配截图（SHOT-M5-P11d）：停留窗内每一行推荐文章句显示期间画面不是截图。
    # 例外：命中指代词(C4)、2秒底线推迟(C3)、无 P7 插画(C5)。
    img_gids = {r["group_id"] for r in (manifest or []) if r.get("image")} if manifest else None
    for g in body:
        gid = g["group_id"]
        gt = g["group_text"]
        if "《" not in gt:
            continue
        ca = char_align.get(gid)
        if not ca or not ca.get("char_times"):
            continue
        glines = []
        for seg in _split_subtitle(ca["char_times"]):
            if not seg:
                continue
            sf = round(seg[0]["startTime"] * FPS)
            glines.append((sf, "".join(c["c"] for c in seg)))
        group_end = round(ca["end_time"] * FPS)
        for idx, (sf, txt) in enumerate(glines):
            if not _is_recommend_line(txt, gt):
                continue
            if any(w in txt for w in A1_WORDS_V2):
                continue  # C4 指代词优先
            if img_gids is not None and gid not in img_gids:
                continue  # C5 无 P7 插画
            if group_end - _sentence_first_frame(glines, idx) < DETAIL_MIN_FRAMES:
                continue  # C3 2 秒底线推迟
            for sc in scenes:
                if sc["start_frame"] <= sf < sc["end_frame"]:
                    if sc.get("type") == "screenshot":
                        v["I14"].append({"group": sc.get("group_id", gid), "frame": sf,
                                         "reason": f"推荐文章句（{gid}）期间画面为截图（{txt[:16]}）"})
                    break

    # I15 插画镜头不短于 2 秒（SHOT-M5-P12）：短于 60 帧的插画（content/overview，非截图/标题）应已并入相邻插画。
    # 例外只有 B2「前后都不是插画」的，逐条列出（不算违反）。
    sscenes = sorted(scenes, key=lambda sc: sc["start_frame"])
    def _is_ill(sc):
        return sc.get("type") != "screenshot" and sc.get("eff_role") in ("content", "overview")
    for i, sc in enumerate(sscenes):
        if not _is_ill(sc):
            continue
        if (sc["end_frame"] - sc["start_frame"]) >= 60:
            continue
        prev_ill = i > 0 and _is_ill(sscenes[i - 1])
        next_ill = i + 1 < len(sscenes) and _is_ill(sscenes[i + 1])
        if not prev_ill and not next_ill:
            v["I15"].append({"group": sc.get("group_id"), "frame": sc["start_frame"],
                             "reason": f"短插画 {sc['end_frame'] - sc['start_frame']} 帧，前后都不是插画（B2 例外）"})
        else:
            v["I15"].append({"group": sc.get("group_id"), "frame": sc["start_frame"],
                             "reason": f"短插画 {sc['end_frame'] - sc['start_frame']} 帧未合并（前后有插画）"})

    # I16 空白帧占比（SHOT-M5-P12）：由 scan_blank 脚本测画面区空白，此处占位（画面级，外部脚本）。
    # I17 换场叠层（SHOT-M5-P12b A3）：画面级，由外部脚本 p12b_i17.py 渲染逐帧比对判定，此处占位。
    # I7 不改坏主线：由外部回归脚本检查，这里只占位
    return v


if __name__ == "__main__":
    import json
    import sys as _sys
    _sys.stdout.reconfigure(encoding="utf-8")
    from pathlib import Path
    BASE = _REPO
    P7R = BASE / "SHOT-M5-P7-排版自适应"
    P7 = BASE / "SHOT-M4-P7-新文章整篇"
    P6 = BASE / "SHOT-M5-P6-看图定锚"
    vd = json.loads((P7R / "video_data.json").read_text(encoding="utf-8"))
    sent = json.loads((P7R / "sentences_shifted.json").read_text(encoding="utf-8"))
    gr = json.loads((P7 / "groups_raw.json").read_text(encoding="utf-8"))
    im = json.loads((P7 / "image_map.json").read_text(encoding="utf-8"))
    plan = json.loads((P7R / "shots_plan.json").read_text(encoding="utf-8"))
    manifest = json.loads((P7 / "manifest.json").read_text(encoding="utf-8"))
    v = check_invariants(vd["scenes"], sent, gr["groups"], im, plan["shots"], manifest=manifest)
    for k in sorted(v):
        print(f"[{k}] 违反 {len(v[k])} 处")
        for x in v[k][:10]:
            print("   ", x.get("frame") and f2t(x["frame"]), x.get("group") or x.get("image"), x.get("word") or x.get("reason") or x.get("keyword") or "", (x.get("text") or "")[:40])
