# -*- coding: utf-8 -*-
"""pipeline 调度层 · 场景构建（P3）。

从 R160/r160_prep_video.py 逐字抽取的场景构建逻辑（纯计算、无 IO）：
title_win（场景时间窗）、ov_s/ov_e（导览场景边界）、scenes 挂载、chapters、
subtitle_lines、body_win。纯 IO（读 manifest/alignment/sentences/groups_raw/audio、
写 video_data）留在 r165_prep_video.py 薄壳。

抽取原则（R165 P3）：搬家不改写。场景挂载是顺序敏感的（scenes[-1] 依赖前一元素），
分支顺序与 r160_prep_video.py 逐字一致，仅依赖注入（routing）+ 参数化（audio_dur/audio_name）。
"""
import copy
import json
import re
import sys
from pathlib import Path, PureWindowsPath

# 引擎根随代码走（ST1-P4a3）：仓库根由本文件位置推出，不写死主仓路径。
_REPO = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(_REPO))
from pipeline import routing
from pipeline import allocate
from pipeline import shot_annotate
from pipeline.shot_director import check_elements
sys.path.insert(0, str(_REPO / "R148"))
import r148_subtitle_align as salign

FPS = 30
TITLE_MIN_DUR = 2.0
BLACK_BUFFER_SEC = 3.0
CHAPTER_RE = re.compile(r"^【([一二三四五六七八九十]+)[、，,]")

# 字幕每屏显示上限与切分方式：句末标点直接断开；超过上限时退回软停顿处切分。
SUBTITLE_SENTENCE_END = "。！？；"
SUBTITLE_SOFT_PAUSE = "，、："
SUBTITLE_MAX_CHARS = 30

# 指代截图的词表（固定清单，请勿自行增删）。
# 已补充「比如下图 / 下图这种 / 这张报错信息」三个词；「出现问题的原因」经评估不计入。
A1_WORDS = ["下面这张图", "下面这些图", "这张图", "这些图", "这张截图", "如图", "如下图",
            "看一下这张", "从后台日志", "从日志", "这个报错", "下面这个", "上面这张",
            "下面这两张图", "下面这几张图", "下面这些报错", "下面这张", "下图", "这几张图", "这张报错",
            "比如下图", "下图这种", "这张报错信息"]

# ST3-P12 R1 兜底第二层（标注空档插画）在 ST3-P12b 去掉了。P12 留下的旧出图需求里如果还有这一类，不排。
FILL_PATH = "标注空档插画"
# R101 C：这几种定锚依据是按口播找到的那一句，不用另找实际对应句（其余是兜底估的位置）
_EXACT_BASES = ("口播句", "原文位置", "点名", "统称句")

# B1: 提前后前一个组剩余时长下限（60 帧）；不足则整组并入截图场景。
B1_MIN_REMAIN_FRAMES = 60
# B1: 只有截图晚于指代句超过 1 秒（30 帧）时才提前。
B1_LATE_FRAMES = 30


def _a1_frames_in_group(char_times, text):
    """返回 text 中所有命中 A1 词表的指代句的起始帧（去重、升序）。

    char_times: salign 对齐产出的逐字 [{c, startTime, endTime}, ...]；
    在逐字串里找词表命中，用命中首字 startTime 换算绝对帧。
    长词优先、命中区间覆盖标记，避免「这张图」在「下面这张图」内部重复命中。
    """
    chars = "".join(ct["c"] for ct in char_times)
    words = sorted(A1_WORDS, key=len, reverse=True)
    frames = set()
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
                    frames.add(t2f(st))
                for k in range(pos, pos + len(w)):
                    covered[k] = True
            start = pos + 1
    return sorted(frames)


def frames_for_audio(audio_dur):
    """配音时长 → (内容帧数, 总帧数)。总帧数 = 内容帧数 + 收尾黑场 BLACK_BUFFER_SEC。

    S10 出片后核对成片声音时长也用这一个公式（ST3-P14），两处不各算各的。
    """
    content_frames = int(audio_dur * FPS) + 1
    return content_frames, content_frames + int(BLACK_BUFFER_SEC * FPS)


def t2f(t):
    return int(round(t * FPS))


def _is_ascii_alnum(ch):
    """ASCII 字母或数字（区分于 Python 把汉字也判 isalnum）。"""
    return ch.isascii() and ch.isalnum()


# R101 F：英文词里用这几个符号连起来的整体（skill-creator、huggingface_hub、python.exe、a/b）不切开
_WORD_JOINERS = "-_./"


def _is_word_char(ch):
    return _is_ascii_alnum(ch) or ch in _WORD_JOINERS


def _find_safe_cut(cur):
    """J: 在 cur 中找最靠后的安全切点（从后往前）。

    安全 = 不切断英文单词（cur[k] 末字与 cur[k+1] 首字不同时是 ASCII 字母/数字或 `-_./`），
    也不隔断 U+2060（word joiner，语义"不要在这里断开"）。返回下标或 -1。
    R101 F：`-_./` 连起来的英文词整体不可切分（以前只保护字母和数字，`skill-|creator` 会被切开）；
    多字 token 按相邻两个字判断。
    """
    for k in range(len(cur) - 2, -1, -1):
        ch = cur[k]["c"]
        nxt = cur[k + 1]["c"]
        if ch and nxt and _is_word_char(ch[-1]) and _is_word_char(nxt[0]):
            continue
        if ch == "\u2060" or nxt == "\u2060":
            continue
        return k
    return -1


def _split_subtitle(char_times):
    """F3: 把一个正文组的逐字时间戳按句子边界切成多段。

    char_times: [{c, startTime, endTime}, ...]，c 为单字。
    返回 list[list[dict]]，每段 ≤ SUBTITLE_MAX_CHARS 字符；段边界优先落在句末标点(。！？；)，
    超上限时回退到软停顿(，、：)切分；无软停顿则回退到英文单词边界(不切断英文单词/不隔断 U+2060)。
    目的是避免"一个正文组=一行字幕=整组一次显示"导致长段落字幕一次堆满屏幕(示例篇最长组 180 字)，
    同时避免硬砍点落在英文单词中间(R168 15:05 "Claude c|ode" 教训)。不改场景数/时间窗/分组。
    """
    segs = []
    cur = []
    for ct in char_times:
        cur.append(ct)
        if ct["c"] in SUBTITLE_SENTENCE_END:
            segs.append(cur)
            cur = []
        elif len(cur) >= SUBTITLE_MAX_CHARS:
            cut = -1
            for k in range(len(cur) - 1, -1, -1):
                if cur[k]["c"] in SUBTITLE_SOFT_PAUSE or cur[k]["c"] in SUBTITLE_SENTENCE_END:
                    cut = k
                    break
            if cut >= 1:
                segs.append(cur[:cut + 1])
                cur = cur[cut + 1:]
            else:
                # J: 无软停顿 → 回退到英文单词边界(空格/中英文交界)，避免切断英文单词
                cut2 = _find_safe_cut(cur)
                if cut2 >= 1:
                    segs.append(cur[:cut2 + 1])
                    cur = cur[cut2 + 1:]
                else:
                    segs.append(cur)
                    cur = []
    if cur:
        segs.append(cur)
    return segs


def _highlight_steps(el, st, en, highlight_map):
    """ST3-P2 C：把 S7 的高亮对应（有序行号范围）按该要素时间窗均分，铺成帧对齐的高亮步骤。

    返回 [{"start_frame": int, "lines": [...]}, ...]；无对应（回退整卡）时返回空列表。
    """
    mapping = (highlight_map or {}).get(el.get("id"))
    if not mapping:
        return []
    dur = en - st
    if dur <= 0:
        return []
    m = len(mapping)
    out = []
    for i, r in enumerate(mapping):
        f = st + round(i * dur / m)
        out.append({"start_frame": f, "lines": r.get("lines", [])})
    return out


def _one_element_scene(gid, el, st, en, highlight_map):
    """ST3-P2 D：把一个要素（代码/命令/表格）铺成一个场景。st/en 为分配器给的最终帧。"""
    base = {
        "group_id": gid,
        "element_id": el.get("id"),
        "start_frame": st,
        "end_frame": en,
        "transition": "cut",
        "transition_kind": "inbetween",
    }
    t = el.get("type")
    if t == "command":
        base["type"] = "terminal"
        base["steps"] = [{"cmd": ln, "out": []} for ln in (el.get("lines") or []) if ln.strip()]
    elif t == "table":
        base["type"] = "table"
        base["table"] = {"header": el.get("header") or [], "rows": el.get("rows") or []}
        base["highlight"] = _highlight_steps(el, st, en, highlight_map)
    else:
        base["type"] = "code_card"
        base["lang"] = el.get("lang") or ""
        base["lines"] = el.get("lines") or []
        base["highlight"] = _highlight_steps(el, st, en, highlight_map)
    return base


def _para_before(lines, line):
    """原文第 line 行（1 起）之前最近的一段正文（跳过空行、图片行、代码块）。"""
    i, in_code = line - 2, False
    while i >= 0:
        t = lines[i].strip()
        if t.startswith("```"):
            in_code = not in_code
        elif not in_code and t and not t.startswith("!["):
            return re.sub(r"^#+\s*", "", t)
        i -= 1
    return ""


def _restack_sidebar(sh, drop_ids):
    """去掉几条标注后，把左栏里排在它们下面的条目和命令框往上收（整图版式的条目横排，不动）。"""
    els, out, shift = sh.get("elements") or [], [], 0
    stacked = sh.get("type") != "ambient"
    for el in sorted(els, key=lambda e: e["rect"]["y"]) if stacked else els:
        if el.get("kind") == "item" and el["id"] in drop_ids:
            shift += el["rect"]["h"] if stacked else 0
            continue
        if shift and el.get("kind") in ("item", "cmd") and not el.get("parent"):
            el["rect"]["y"] = round(el["rect"]["y"] - shift, 1)
            el["rel_y"] = el["rect"]["y"]
        out.append(el)
    k = 0
    for el in sorted([e for e in out if e.get("kind") == "item"], key=lambda e: int(e["id"].split("-")[1])):
        el["id"] = f"item-{k}"
        k += 1
    order = {id(el): i for i, el in enumerate(els)}
    sh["elements"] = sorted(out, key=lambda e: order[id(e)])


def _write_back_shot(sh, st, en, seq, index):
    """用分配器给的最终时间窗回写镜头内部时间——全系统只有一套时间。

    旧版侧栏（导演链）：条目出现帧（anchor_rel）按口播绝对帧重算，落到镜头窗之外的收进窗内。
    ST3-P12 逐图标注：一条标注对应的句子落到这张图的在屏时间之外，就去掉这条（不挪位置），
    返回值第二项是这一段里显示的条目，供调用方登记被去掉的。
    """
    sh = copy.deepcopy(sh)
    dur = en - st
    lead_in = sh["timing"].get("lead_in", 0)
    sh["timing"] = {"start_frame": st, "duration_frames": dur, "lead_in": lead_in, "end_frame": en}
    shown = None
    if sh.get("annot"):
        keep, drop_ids = [], set()
        for i, it in enumerate(sh.get("items") or []):
            ab = it.get("anchor_abs")
            ok = ab is None or shot_annotate.unit_on_screen(ab, it.get("anchor_end"), st, en)
            if ok:
                it["anchor_rel"] = (ab - st) if ab is not None else 0
                keep.append(it)
            else:
                drop_ids.add(f"item-{i}")
        if drop_ids:
            _restack_sidebar(sh, drop_ids)
        sh["items"] = keep
        shown = [(it.get("anchor_abs"), it.get("text")) for it in keep]
    else:
        for it in sh.get("items") or []:
            ab = it.get("anchor_abs")
            if ab is not None:
                it["anchor_rel"] = min(ab - st, max(0, dur - 20))
    for el in sh.get("elements") or []:
        if el.get("kind") == "cmd":
            # ST3-P11 R3：命令框在它对应的口播句开始时出现；那句话不在这张图的在屏时间内，就和图一起出现。
            ab = el.get("anchor_abs")
            el["anchor_rel"] = (ab - st) if (ab is not None and st <= ab < en) else 0
    sh["shot_index"] = index
    sh["group_seq"] = seq
    sh["sort_key"] = st
    sh["total_frames"] = dur + lead_in
    sh["audio_start_frame"] = st
    # ST3-P9：排版自检按最终送去渲染的这份镜头重新登记（与 S7 同一个函数 check_elements）。
    # 渲染端只认这里的结论；只重跑 S9 时，镜头计划里旧代码留下的登记不会原样带进成片。
    if sh.get("elements"):
        ok, errs = check_elements(sh["elements"])
        sh["self_check"] = {"pass": ok, "errors": errs}
    return sh, shown


def build_scenes(manifest_all, alignment, sentences, groups, audio_dur=None, audio_name="audio.wav", shots=None, asset_subdir="", stay_covered=None, partial_recover=None, elements=None, highlight_map=None, image_map=None, article_text=None, strict=True):
    """场景构建（纯计算，无 IO）。

    manifest_all: manifest.json 全量记录列表。
    alignment: {group_id: {"start_time","end_time"}}。
    sentences: 豆包 sentences.json。
    groups: groups_raw.json 的 groups 列表。
    audio_dur: 音频时长（秒）；None 时用场景/字幕 end_frame 推算（fallback）。
    audio_name: 音频文件名（写进 data["audio"]）。
    shots: 截图导演层 build_shots 输出的 shots 列表（可选）。传入后，按有效组接入 screenshot 场景。
    asset_subdir: AI 插画分目录前缀（可选，默认空=行为不变）。非空时 AI 插画路径 = {asset_subdir}/{filename}。
    stay_covered: 被截图停留覆盖的口播组集合（SHOT-M5-P8 v1.2，可选）。这些组沿用当前截图场景、不插画。
    image_map: image_map.json（可选）。给了就按原文位置给非截图原图定锚，并核对「原图齐全」。
    article_text: 文章原文（可选）。给了就按要素在原文里的位置给要素定锚。
    strict: 五条底线不过就抛错（回归测试用）；False 时只把结果挂在 data["baselines"]，
            由调用方决定怎么办（正式运行：照常出片、标红、退出码 1）。
    返回 video_data dict（scenes/subtitle_lines/chapters/content_frames/total_frames/allocation_notes）。

    ST3-F2：时间分配只有一处——收集画面项 → pipeline.allocate 在每个章节内一次切分 → 回写镜头内部时间。

    截图 scene plan 消费点（SHOT-M4-P4 C4）：在 scenes 挂载循环里 no_img 分支之后、原图分支之前，
    把 shots 里该组的镜头展开为 type="screenshot" 场景（一组多镜头按导演层切分比例铺满该组）。
    """
    def _ai_img(fname):
        return f"{asset_subdir}/{fname}" if asset_subdir else fname

    def _shot_img(fname):
        return f"shots/{asset_subdir}/{fname}" if asset_subdir else f"shots/m4p4/{fname}"

    titles = [g for g in groups if g["starts_with_bracket_title"]]
    body = [g for g in groups if not g["starts_with_bracket_title"]]

    manifest = [r for r in manifest_all if r.get("image") and r.get("path") not in ("空档插画", FILL_PATH)]
    # F1: 弃权组(画面提炼返回 subject=None)并入无图组, 沿用上一组画面
    abstain_ids = {r["group_id"] for r in manifest_all if r.get("path") == "不出图(弃权)"}

    hits, units = routing.detect_overview(body)
    if units is None:
        raise SystemExit("ERROR: 导览判定异常")
    trigger_gids = {unit[0]["group_id"] for unit in units}
    absorbed_by_trigger = {unit[0]["group_id"]: [g["group_id"] for g in unit[1]] for unit in units}
    absorbed = [g["group_id"] for unit in units for g in unit[1]]
    transition_ids, func_short_ids = routing.identify_no_image_groups(groups)
    no_img = transition_ids | func_short_ids | abstain_ids

    missing = [g["group_id"] for g in body
               if not alignment.get(g["group_id"]) or alignment[g["group_id"]].get("start_time") is None]
    if missing:
        raise SystemExit("ERROR: 无时间戳: " + ", ".join(missing))

    # 字幕行（绝对帧，逐字对齐层）。F3: 组内按句子边界分段，每段一行(不再一组一行)。
    char_align = {r["group_id"]: r for r in salign.align_groups(
        sentences, [{"group_id": g["group_id"], "text": g["group_text"]} for g in body])}
    subtitle_lines = []
    for g in body:
        ca = char_align[g["group_id"]]
        for seg in _split_subtitle(ca["char_times"]):
            chars = [{"c": c["c"], "f": t2f(c["startTime"]) if c["startTime"] is not None else 0}
                     for c in seg]
            if not chars:
                continue
            end_t = seg[-1]["endTime"]
            subtitle_lines.append({
                "start_frame": chars[0]["f"],
                "end_frame": t2f(end_t) if end_t is not None else chars[-1]["f"],
                "chars": chars,
            })

    body_win = {g["group_id"]: (alignment[g["group_id"]]["start_time"], alignment[g["group_id"]]["end_time"])
                for g in body}

    # ST3-P2：要素按组归集（按原文顺序）。只有挂到 body 组、且该组有时间窗的要素才参与铺场景。
    elements_by_gid = {}
    for el in (elements or []):
        gid = el.get("mapped_group_id")
        if gid in body_win:
            elements_by_gid.setdefault(gid, []).append(el)

    # 标题卡：出现在其后第一个正文组开口时。若标题后紧跟标题, 跳过中间标题找第一个正文组。
    own_pic_gids = set(elements_by_gid.keys())
    if shots:
        own_pic_gids |= {sh["group"] for sh in shots}
    own_pic_gids |= {r["group_id"] for r in manifest_all if r.get("result") == "原图"}
    img_by_gid = {r["group_id"]: r for r in manifest}
    title_win = {}
    title_covers = set()     # 自己没有画面的首个正文组：整组由标题卡占着，不另出插画
    trailing_titles = set()  # 结尾标题组（其后无任何正文组）：不出标题卡，时长自然并入前一个画面
    for i, g in enumerate(groups):
        if not g["starts_with_bracket_title"]:
            continue
        tr = next((x for x in groups[i + 1:] if not x["starts_with_bracket_title"]), None)
        if tr is None:
            trailing_titles.add(g["group_id"])
            continue
        tr_s, tr_e = body_win[tr["group_id"]]
        # 标题卡占多久：其后第一个正文组自己没有画面 → 占满这个过渡语段（下限 2 秒）；有画面 → 只占 2 秒。
        own = tr["group_id"] in own_pic_gids
        title_win[g["group_id"]] = (tr_s, tr_s + (TITLE_MIN_DUR if own else max(tr_e - tr_s, TITLE_MIN_DUR)))
        if g["group_id"] in img_by_gid and not own:
            title_covers.add(tr["group_id"])

    if audio_dur is None:
        audio_dur = max([t2f(e) for _, e in body_win.values()] + [l["end_frame"] for l in subtitle_lines]) / FPS
    content_frames, total_frames = frames_for_audio(audio_dur)

    chapters = []
    chapter_titles = [g for g in titles
                      if CHAPTER_RE.match(g["group_text"]) and g["group_id"] not in trailing_titles]
    for idx, ct in enumerate(chapter_titles):
        name = re.sub(r"^【[一二三四五六七八九十]+[、，,]", "", ct["group_text"]).strip("】")
        start_t = title_win[ct["group_id"]][0]
        end_t = (title_win[chapter_titles[idx + 1]["group_id"]][0]
                 if idx + 1 < len(chapter_titles) else audio_dur)
        chapters.append({"name": name, "start_time": round(start_t, 2), "end_time": round(end_t, 2),
                         "start_frame": t2f(start_t), "end_frame": t2f(end_t)})

    # ---- 收集画面项 ----
    items = []

    def _add(aid, kind, anchor, payload, must=True, track=False, alt=None, basis=None, **extra):
        items.append(dict({"id": aid, "kind": kind, "anchor": anchor, "min": allocate.MIN_FRAMES[kind],
                           "must": must, "order": len(items), "track": track, "alt": alt,
                           "basis": basis, "payload": payload}, **extra))

    # 截图镜头：期望出现帧由 S7 按「讲到这张图的那句话」给出（旧版镜头计划没有 alloc，按原时间、全部必须出现）。
    shot_pre = []
    line_of = {Path(e.get("source_relative_path") or e["image_path"]).name: e
               for e in (image_map or {}).get("images", [])}
    # 概述网格紧贴在本图组第一张图之前（共 m 屏、每屏 2 秒），那里有空档才上；
    # 改用网格时（本章节排不下逐张）则从第一张图的锚点起依次上。
    first_detail, n_screens, screen_no = {}, {}, {}
    for sh in shots or []:
        al = sh.get("alloc") or {}
        if al.get("role") == "detail":
            first_detail[al["stay"]] = min(first_detail.get(al["stay"], al["anchor"]), al["anchor"])
        elif al.get("role") == "overview":
            n_screens[al["stay"]] = n_screens.get(al["stay"], 0) + 1
    for n, sh in enumerate(shots or []):
        al = sh.get("alloc") or {}
        anchor = al.get("anchor", sh["timing"]["start_frame"])
        if al.get("role") == "overview" and al.get("basis") in ("统称句", "首图锚点"):
            # ST3-P11 R4：连排图的网格必须出现——有统称句从统称句开始，没有就从本段第一张图的锚点开始
            # （锚点由 S7 给）。章节排不下逐张时，逐张让位、只留网格（alt 机制不变）。
            _add(f"grid:{sh['group']}:{n}", "grid", anchor, sh, track=True, basis=al["basis"],
                 alt={"key": al["stay"], "role": "overview"}, alt_anchor=anchor)
            continue
        if al.get("role") == "overview":
            k = screen_no.get(al["stay"], 0)
            screen_no[al["stay"]] = k + 1
            base = first_detail.get(al["stay"], anchor)
            step = allocate.MIN_FRAMES["grid"]
            _add(f"grid:{sh['group']}:{n}", "grid", base - step * (n_screens[al["stay"]] - k), sh,
                 must=False, track=True, alt={"key": al["stay"], "role": "overview"}, basis="图组概述",
                 alt_anchor=base + step * k)
            continue
        is_grid = sh.get("type") == "grid"
        aid = f"{'grid' if is_grid else 'shot'}:{sh['group']}:{n}:{sh['image']}"
        _add(aid, "grid" if is_grid else "shot",
             anchor, sh, track=True, basis=al.get("basis", "镜头计划"),
             alt={"key": al["stay"], "role": "detail"} if al.get("in_overview") else None)
        if sh["image"] in line_of and not is_grid:
            shot_pre.append({"key": sh["image"], "line": line_of[sh["image"]]["line"], "frame": anchor})

    # 非截图原图 + 要素：兜底锚点 = 在所属组口播窗内按原文顺序等比分布；
    # 有 image_map / 文章原文时，改按它在原文里的位置对应的句子定锚。
    pending = []
    art_lines = article_text.splitlines() if article_text else None
    for g in body:
        gid = g["group_id"]
        rec = img_by_gid.get(gid, {})
        origs = (rec.get("images") or [rec["image"]]) if rec.get("result") == "原图" else []
        els_here = elements_by_gid.get(gid) or []
        n_all = len(origs) + len(els_here)
        s, e = body_win[gid]
        for k, img in enumerate(origs):
            ent = line_of.get(Path(img).name)
            pending.append({"key": f"img:{gid}:{img}", "kind": "original", "payload": (gid, rec, img),
                            "fallback": t2f(s + k * (e - s) / n_all),
                            "line": ent["line"] if ent else None,
                            "texts": [ent.get("before") or "", ent.get("after") or ""] if ent else None})
        for k, el in enumerate(els_here):
            k += len(origs)
            has_line = art_lines is not None and el.get("line")
            pending.append({"key": f"el:{el.get('id')}", "kind": "element", "payload": (gid, el),
                            "fallback": t2f(s + k * (e - s) / n_all),
                            "line": el["line"] if has_line else None,
                            # 正文里的命令（R3）按它所在的那句原文定锚；围栏要素按它前面那段原文。
                            "texts": ((el.get("anchor_texts") or [_para_before(art_lines, el["line"])])
                                      if has_line else None)})
    # R101 C：每个正文组所属小节（【标题】到下一个【标题】）的口播范围（帧）。原文位置对不上的原图 / 要素
    # 只在这个范围里找位置，不再跨小节按前后邻居等比分布（1.0 实测科普篇 0001：一张命令截图因此提前 23 秒）。
    sec_win = {}
    t_starts = [t2f(title_win[g["group_id"]][0]) for g in groups if g["group_id"] in title_win]
    cur = 0
    for g in groups:
        if g["group_id"] in title_win:
            cur = t2f(title_win[g["group_id"]][0])
        elif not g["starts_with_bracket_title"]:
            nxt = next((s for s in t_starts if s > cur), content_frames)
            sec_win[g["group_id"]] = (cur, max(cur + 1, nxt))
    sidx = None
    located = [p for p in pending if p["line"] is not None]
    anchors = {}
    if located or line_of:
        sidx = allocate.sentence_index(
            [(g["group_id"], char_align[g["group_id"]]["char_times"]) for g in body], FPS)
    if located:
        anchors = allocate.resolve_anchors(
            [{"key": p["key"], "line": p["line"], "texts": p["texts"], "fallback": p["fallback"],
              "window": sec_win.get(p["payload"][0])}
             for p in located], sidx, pre=shot_pre, near_min=allocate.NEAR_MIN_RATIO)
    texts_of = {p["key"]: p["texts"] for p in pending}
    for p in pending:
        a = anchors.get(p["key"]) or {"frame": p["fallback"], "basis": "所属组等比分布"}
        _add(p["key"], p["kind"], a["frame"], p["payload"], track=True, basis=a["basis"])

    # 标题卡 + 插画（分支顺序与原挂载循环一致）
    for g in groups:
        gid = g["group_id"]
        rec = img_by_gid.get(gid)
        if g["starts_with_bracket_title"]:
            if gid in trailing_titles or rec is None:
                continue
            _add(f"title:{gid}", "title", t2f(title_win[gid][0]), (gid, rec, "title"),
                 hold=t2f(title_win[gid][1]) - t2f(title_win[gid][0]))
        elif gid in trigger_gids:
            if rec is not None:
                # 导览插画：盖住触发组及被吸收的序数组（被吸收的组不出画面，自然延续）。
                _add(f"ill:{gid}", "illustration", t2f(body_win[gid][0]), (gid, rec, "overview"), must=False)
        elif gid in absorbed or gid in own_pic_gids:
            continue
        elif gid in no_img or (stay_covered and gid in stay_covered):
            continue   # 不出图组 / 截图停留覆盖的组：沿用前一个画面
        elif rec is None or gid in title_covers:
            continue   # 没有插画 → 沿用前一个画面；标题卡占着的组不另出插画
        else:
            start = partial_recover[gid] if (partial_recover and gid in partial_recover) else t2f(body_win[gid][0])
            _add(f"ill:{gid}", "illustration", start, (gid, rec, "content"), must=False)

    # ST3-P11 R6：空档插画——只用来填它那张标题卡后面超过 6 秒的空档，用不上就不出现。
    for r in manifest_all:
        if r.get("path") == "空档插画" and r.get("image") and r.get("gap_for") in title_win:
            tgid = r["gap_for"]
            _add(f"ill:gap:{tgid}", "illustration", t2f(title_win[tgid][1]), (tgid, r, "content"),
                 must=False, fill_for=f"title:{tgid}")

    # ---- 章节内一次切分 ----
    placed, notes = allocate.allocate(items, [c["start_frame"] for c in chapters], content_frames)

    # ---- 生成场景，并回写镜头内部时间 ----
    scenes = []
    g_seq = {}
    annot_seen = {}          # id(镜头) → (镜头, 各段里显示过的条目)
    for it in placed:
        st, en, kind, pl = it["start"], it["end"], it["kind"], it["payload"]
        if kind in ("shot", "grid"):
            seq = g_seq.get(pl["group"], 0)
            g_seq[pl["group"]] = seq + 1
            sh, shown = _write_back_shot(pl, st, en, seq, sum(g_seq.values()) - 1)
            if shown is not None:
                annot_seen.setdefault(id(pl), (pl, set()))[1].update(shown)
            sc = {"group_id": pl["group"], "type": "screenshot", "shot": sh,
                  "image": _shot_img(sh["image"]), "start_frame": st, "end_frame": en,
                  "is_original": True, "transition": "cut", "transition_kind": "inbetween"}
        elif kind == "element":
            sc = _one_element_scene(pl[0], pl[1], st, en, highlight_map)
        elif kind == "original":
            gid, rec, img = pl
            sc = {"group_id": gid, "eff_role": "content", "image": img,
                  "start_frame": st, "end_frame": en, "is_original": True,
                  "transition": rec.get("transition", "cut"),
                  "transition_kind": rec.get("transition_kind", "inbetween")}
        else:
            gid, rec, role = pl
            sc = {"group_id": gid, "eff_role": role, "image": _ai_img(PureWindowsPath(rec["image"]).name),
                  "start_frame": st, "end_frame": en, "is_original": False,
                  "transition": rec.get("transition", "cut"),
                  "transition_kind": rec.get("transition_kind", "inbetween")}
        sc["alloc_id"] = it["id"]
        if it.get("track"):
            sc["anchor_frame"] = it["anchor"]
            sc["anchor_basis"] = it.get("basis")
            # R101 C：锚点不是按口播句 / 原文位置精确找到的（兜底估的），另找它在口播里实际对应的句子，
            # 提前 / 推后核对拿它比（只用于核对，不改位置）。
            if it.get("basis") not in _EXACT_BASES and sidx is not None:
                if kind == "original" or kind == "element":
                    texts = texts_of.get(it["id"])
                else:
                    ent = line_of.get(pl.get("image")) if isinstance(pl, dict) else None
                    texts = [ent.get("before") or "", ent.get("after") or ""] if ent else None
                if texts:
                    ref, ratio = allocate.reference_sentence(sidx, texts)
                    if ref is not None:
                        sc["ref_frame"], sc["ref_ratio"] = ref, round(ratio, 4)
        scenes.append(sc)

    # ST3-P12 R1：对应的句子落到这张图在屏时间之外、因此被去掉的标注（登记，不挪位置）。
    annotation_drops = [{"image": pl["image"], "text": x.get("text"), "quote": x.get("quote"),
                         "anchor_abs": x.get("anchor_abs"), "reason": "对应的句子不在这张图的在屏时间内"}
                        for pl, seen in annot_seen.values() for x in pl.get("items") or []
                        if (x.get("anchor_abs"), x.get("text")) not in seen]
    data = {"fps": FPS, "total_frames": total_frames, "content_frames": content_frames,
            "audio": audio_name, "scenes": scenes, "subtitle_lines": subtitle_lines,
            "chapters": chapters, "allocation_notes": notes, "annotation_drops": annotation_drops}
    # 五条底线：不空白 / 不重叠 / 原图齐全 / 最短时长 / 画面跟着口播走。
    need_imgs = sorted({fn for fn, e in line_of.items() if e.get("insert", True) is not False}
                       | {sh["image"] for sh in (shots or [])}
                       | {g["image"] for sh in (shots or []) for g in (sh.get("grid") or {}).get("images") or []}
                       | {Path(p["payload"][2]).name for p in pending if p["kind"] == "original"})
    need_els = [p["payload"][1].get("id") for p in pending if p["kind"] == "element"]
    check = allocate.assert_baselines if strict else allocate.check_baselines
    data["baselines"] = check(data, need_imgs, need_els, notes)
    return data
