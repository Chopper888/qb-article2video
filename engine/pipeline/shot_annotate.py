# -*- coding: utf-8 -*-
"""ST3-P12 R1：左侧标注逐图、逐句生成（替代按图组共用的导演链侧栏）。

每张截图一份标注：一行标题 + 若干条目，每条对应这张图在屏期间的一句口播，在那句话开始时出现。
本模块只管「一张图」：量宽、切句、拼提示词、校验、规则兜底、算无新内容的间隔。
什么时候生成（S7 场景试排之后）、放在哪（左栏排版）、最终去留（S9）由调用方管。
模型调用由调用方注入（`text_call(prompt, max_tokens=…) -> str | None`），这里不联网。
"""
import os
import re
from pathlib import Path

from pipeline.shot_ai import parse_json
from pipeline.shot_director import fn_of

_REPO = Path(__file__).resolve().parents[1]
_PROMPT = _REPO / "pipeline" / "prompts" / "逐句标注.txt"
FONT_FILE = "LXGWWenKai-Regular.ttf"       # 渲染端左栏用的字体（fonts.tsx 的 LXGW WenKai），随运行时包分发

FPS = 30
SIDE_MAX_W = 645            # 左栏每个元素的宽度上限（像素，约 15 个 40 号汉字）
ITEM_FONT = 40
ITEM_DECOR_W = 10 + 14      # 条目前面的橙点 + 间距
TITLE_FONTS = (64, 52)      # 标题字号：放不下缩一档，再放不下截短
MEASURE_PAD = 4             # 量宽留的余量（与 est_w 的 +4 一致）

SENT_END = "。！？!?；;\n"
CLAUSE_END = "，,：:"
LONG_SENT_FRAMES = 6 * FPS      # 一句话超过 6 秒：按逗号切成半句。目标是每 8 秒内有新内容，
                                # 因此切分单位要比 8 秒明显更短
PIECE_MIN_FRAMES = 3 * FPS      # 半句至少 3 秒才切一刀
PIECE_TAIL_FRAMES = 2 * FPS     # 切完剩下的不足 2 秒就不切
IN_WINDOW_MIN = 2 * FPS         # 在这张图上屏之后才开始的句子，至少有 2 秒在屏才算这张图的
GAP_MAX_FRAMES = 8 * FPS        # 目标：停留 ≥ 10 秒的截图，每 8 秒内要有新内容
LONG_STAY_FRAMES = 10 * FPS
RULE_PHRASE_CHARS = 12

# --------------------------------------------------------------------------
# 量宽：和渲染端用同一个字体文件
# --------------------------------------------------------------------------

_font_path = None
_fonts = {}


def use_font(path):
    """指定量宽用的字体文件（运行时目录里的 fonts/LXGWWenKai-Regular.ttf）。传 None 清除。"""
    global _font_path
    _font_path = str(path) if path and Path(path).is_file() else None
    _fonts.clear()
    return _font_path


SYSTEM_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",                 # Windows 微软雅黑
    "/System/Library/Fonts/PingFang.ttc",        # macOS 苹方
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def font_path():
    """当前量宽用的字体文件；没指定时依次找运行时字体、系统字体，都找不到返回 None（改用估算）。"""
    if _font_path:
        return _font_path
    rd = os.environ.get("A2V_RUNTIME_DIR")
    if rd and (Path(rd) / "fonts" / FONT_FILE).is_file():
        return str(Path(rd) / "fonts" / FONT_FILE)
    for candidate in SYSTEM_FONTS:
        if Path(candidate).is_file():
            return candidate
    return None


def _estimate(text, size):
    # 没有字体文件时的估算（同 shot_director.est_w 的口径）：汉字 1、ASCII 0.55、空格 0.3 个字号。
    units = sum(0.3 if ch == " " else (0.55 if ord(ch) < 0x2e80 else 1.0) for ch in text)
    return units * size * 1.02


def measure(text, size):
    """一行文字按渲染字体量出来的像素宽度（不含余量）。"""
    p = font_path()
    if not p:
        return _estimate(text, size)
    key = (p, size)
    if key not in _fonts:
        from PIL import ImageFont
        _fonts[key] = ImageFont.truetype(p, size)
    return float(_fonts[key].getlength(text))


def text_w(text, size):
    """排版登记用的宽度 = 实际宽度 + 余量。"""
    return measure(text, size) + MEASURE_PAD


def item_w(text):
    """一条标注占的宽度（橙点 + 间距 + 文字）。"""
    return text_w(text, ITEM_FONT) + ITEM_DECOR_W


def wrap_words(text, width_of, first_w, rest_w=None):
    """只在空格处换行；单个词比一行还长时才在中间断开。和渲染端 st3Text.ts 的 wrapCommand 同一套规则。

    width_of(字符串) -> 宽度；first_w / rest_w 是第一行和后续各行能放下的宽度。返回各行文字（空格留在行尾）。
    """
    rest_w = first_w if rest_w is None else rest_w
    out, cur = [], ""

    def cap():
        return first_w if not out else rest_w

    for w in re.findall(r"\S+\s*|\s+", text):
        if cur and width_of((cur + w).rstrip()) > cap():
            out.append(cur)
            cur = ""
        if width_of(w.rstrip()) > cap():
            for ch in w:                       # 单个词比一行还长：逐字填满再断
                if cur and width_of(cur + ch) > cap():
                    out.append(cur)
                    cur = ""
                cur += ch
        else:
            cur += w
    if cur:
        out.append(cur)
    return out


# --------------------------------------------------------------------------
# 口播切句
# --------------------------------------------------------------------------

def _norm(s):
    return re.sub(r"[^\w]", "", s or "").lower()


def build_units(char_rows, fps=FPS):
    """口播逐字时间 → 句子单位（生成标注、判断「有没有新内容」都按它算）。

    char_rows: [(group_id, [{c, startTime, endTime}, ...]), ...]，按口播顺序。
    按 。！？； 和换行切句；一句话超过 6 秒时再按逗号切成半句（每段至少 3 秒）。
    返回 [{"i", "gid", "text", "start", "end"}]，start / end 是帧。
    """
    sents = []
    for gid, char_times in char_rows:
        cur, last_f = [], None
        for ct in char_times:
            st, et = ct.get("startTime"), ct.get("endTime")
            sf = int(round(st * fps)) if st is not None else last_f
            ef = int(round(et * fps)) if et is not None else sf
            last_f = ef if ef is not None else last_f
            cur.append((ct.get("c", ""), sf, ef))
            if ct.get("c", "") and ct["c"] in SENT_END:
                sents.append((gid, cur))
                cur = []
        if cur:
            sents.append((gid, cur))

    units = []

    def _emit(gid, chars):
        text = "".join(c for c, _s, _e in chars).strip()
        timed = [(s, e) for c, s, e in chars if _norm(c) and s is not None]
        if not _norm(text) or not timed:
            return
        units.append({"i": len(units), "gid": gid, "text": text,
                      "start": timed[0][0], "end": max(timed[-1][1] or timed[-1][0], timed[0][0])})

    for gid, chars in sents:
        timed = [(s, e) for c, s, e in chars if _norm(c) and s is not None]
        if not timed:
            continue
        s0, e0 = timed[0][0], timed[-1][1] or timed[-1][0]
        if e0 - s0 <= LONG_SENT_FRAMES:
            _emit(gid, chars)
            continue
        piece, piece_start = [], s0
        for k, (c, s, e) in enumerate(chars):
            piece.append((c, s, e))
            at = e if e is not None else s
            if (c in CLAUSE_END and at is not None and at - piece_start >= PIECE_MIN_FRAMES
                    and e0 - at >= PIECE_TAIL_FRAMES and _norm("".join(x[0] for x in chars[k + 1:]))):
                _emit(gid, piece)
                piece, piece_start = [], at
        _emit(gid, piece)
    return units


def _overlap(u, st, en):
    return min(en, u["end"]) - max(st, u["start"])


def _in_window(u, st, en):
    dur = max(1, u["end"] - u["start"])
    ov = _overlap(u, st, en)
    return ov * 2 >= dur or (u["start"] >= st and ov >= min(IN_WINDOW_MIN, dur))


def units_in_window(units, st, en):
    """一张图在屏期间 [st, en) 的口播句。

    这张图上屏时已经讲了一半的句子：大半句在屏才算；上屏之后才开始的句子：至少 2 秒在屏
    （或者整句都在屏）才算。
    """
    return [u for u in units if _in_window(u, st, en)]


def assign_units(units, windows):
    """把口播句分给各张图：{文件名: [句子]}。一句话横跨两张图时只归在屏时间更长的那一张
    （一样长归前一张），不让两张图各写一条。"""
    out = {fn: [] for fn in windows}
    order = sorted(windows, key=lambda fn: windows[fn])
    for u in units:
        best = None
        for fn in order:
            st, en = windows[fn]
            if _in_window(u, st, en) and (best is None or _overlap(u, st, en) > best[0]):
                best = (_overlap(u, st, en), fn)
        if best:
            out[best[1]].append(u)
    return out


def unit_on_screen(start, end, st, en):
    """S9 最终分配后的去留：这条标注对应的句子和这张图的在屏时间 [st, en) 有没有交集。"""
    return start < en and (end is None or end > st or start >= st)


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------

def _mmss(frame, fps=FPS):
    sec = frame / fps
    return f"{int(sec // 60):02d}:{sec % 60:04.1f}"


def prompt_template():
    return _PROMPT.read_text(encoding="utf-8")


def build_prompt(heading, before, after, cmds, win_units, feedback=None, stay_frames=None, hint=None):
    """一张图的提示词。feedback：上一次输出没过校验的地方（第二次调用时附在最后）。"""
    sents = "\n".join(f"{k + 1}. [{_mmss(u['start'])}] {u['text']}" for k, u in enumerate(win_units))
    if stay_frames is None and win_units:
        stay_frames = win_units[-1]["end"] - win_units[0]["start"]
    p = (prompt_template()
         .replace("{{停留秒数}}", str(max(1, round((stay_frames or 0) / FPS))))
         .replace("{{小节标题}}", heading or "（无）")
         .replace("{{图前原文}}", (before or "").strip() or "（无）")
         .replace("{{图后原文}}", (after or "").strip() or "（无）")
         .replace("{{图片线索}}", (hint or "").strip() or "（无）")
         .replace("{{命令框}}", "\n".join(cmds) if cmds else "（无）")
         .replace("{{口播句}}", sents))
    if feedback:
        p += ("\n【上一次输出未通过检查，请修正后重新输出完整 JSON】\n"
              + "\n".join(f"- {x}" for x in feedback) + "\n")
    return p


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

# 允许出现在条目开头、不要求在原文里找到的前缀：「原因：」「方案一：」「第 2 步：」这一类。
_PREFIX_RE = re.compile(
    r"^\s*(?:第\s*[一二三四五六七八九十百\d]+\s*[步种点个条项类]|步骤\s*[一二三四五六七八九十\d]+"
    r"|(?:方案|方法|做法|办法|情况|选项|原因|问题|步骤)\s*[一二三四五六七八九十\d]*"
    r"|[一-鿿]{1,6})\s*[：:]\s*")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+#\-/\\]*")
_QUOTED_RE = re.compile(r"[“\"「『]([^”\"」』]+)[”\"」』]")
BAD_SHAPE = "输出不是约定的 JSON 结构"


def foreign_tokens(text, source_norm):
    """条目里在原文和口播里都找不到的英文、数字、专有名词（引号里的词）。汉字不逐字核对。"""
    body = _PREFIX_RE.sub("", text, count=1)
    bad = []
    for tok in _ASCII_TOKEN_RE.findall(body) + _QUOTED_RE.findall(body):
        t = _norm(tok)
        if t and t not in source_norm:
            bad.append(tok)
    return bad


def fit_title(title):
    """标题按宽度检查：放不下缩一档字号，再放不下截短。返回 (文字, 字号, 登记 or None)。"""
    title = re.sub(r"\s+", " ", (title or "").strip()).strip("。，；：、")
    if not title:
        return "", TITLE_FONTS[0], None
    for size in TITLE_FONTS:
        if text_w(title, size) <= SIDE_MAX_W:
            return title, size, (None if size == TITLE_FONTS[0] else f"标题缩一档字号（{size}）")
    size, cut = TITLE_FONTS[-1], title
    while len(cut) > 1 and text_w(cut + "…", size) > SIDE_MAX_W:
        cut = cut[:-1]
    return cut + "…", size, f"标题截短：{title} → {cut}…"


def _too_wide(text, w):
    """条目太宽时回给模型的话：说清楚超了多少、英文数字本身占了多宽、其余还能写几个字。"""
    budget = (SIDE_MAX_W - ITEM_DECOR_W - MEASURE_PAD) / ITEM_FONT
    toks = [t for t in _ASCII_TOKEN_RE.findall(text) if len(t) >= 3]
    msg = f"「{text}」太宽：相当于 {(w - ITEM_DECOR_W) / ITEM_FONT:.1f} 个汉字，上限 {budget:.0f} 个。"
    if toks:
        ascii_w = sum(measure(t, ITEM_FONT) for t in toks) / ITEM_FONT
        msg += (f"其中英文和数字（{'、'.join(toks)}）就占了 {ascii_w:.1f} 个汉字，"
                f"其余的汉字、标点、空格加起来最多只能再写 {max(0, int(budget - ascii_w))} 个。"
                "请换个更短的说法：英文词只保留最关键的一个，不必全部列出；也可以去掉前缀")
    else:
        msg += f"请再缩短至少 {int((w - SIDE_MAX_W) / ITEM_FONT) + 1} 个字，可以去掉前缀、只留关键词"
    return msg


def validate(obj, win_units, source_norm):
    """校验模型输出。返回 (title, items, problems)。

    items: 通过校验的条目 [{"n": 窗口内第几句（1 起）, "text"}]，按句序；
    problems: 给人看、也回给模型的问题清单（空 = 全过）。
    每条按像素宽度检查；英文、数字、专有名词必须能在原文或口播里找到；条目顺序与句子顺序一致。
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        return "", [], [BAD_SHAPE]
    problems, items, last_n = [], [], 0
    for it in obj["items"]:
        if not isinstance(it, dict):
            problems.append("items 里有不是对象的元素")
            continue
        text = re.sub(r"\s+", " ", str(it.get("text") or "")).strip().rstrip("。；;，,")
        try:
            n = int(it.get("n"))
        except (TypeError, ValueError):
            problems.append(f"「{text}」没有标明对应第几句")
            continue
        if not text:
            continue
        if not 1 <= n <= len(win_units):
            problems.append(f"「{text}」对应的句子序号 {n} 不存在（只有 1～{len(win_units)}）")
            continue
        if n <= last_n:
            problems.append(f"「{text}」的顺序有误：每句至多一条，条目要与句子顺序一致（第 {n} 句）")
            continue
        w = item_w(text)
        if w > SIDE_MAX_W:
            problems.append(_too_wide(text, w))
            continue
        bad = foreign_tokens(text, source_norm)
        if bad:
            problems.append(f"「{text}」中的 {'、'.join(bad)} 在口播和原文里均未出现，请照原文抄写或删去")
            continue
        items.append({"n": n, "text": text})
        last_n = n
    return str(obj.get("title") or ""), items, problems


# --------------------------------------------------------------------------
# 规则兜底（第一层）
# --------------------------------------------------------------------------

_LEAD_RE = re.compile(
    r"^\s*(?:第\s*[一二三四五六七八九十百\d]+\s*[步种点个条项类]|步骤\s*[一二三四五六七八九十\d]+"
    r"|首先|然后|接着|接下来|其次|再次|最后|另外|此外|同时|那么|所以|因此|不过|但是|而且|并且|比如|例如|这时候?|此时)"
    r"\s*[，,：:、]?\s*")
_STOP = "，。；：！？、,;:!?\n"


def _is_word_char(ch):
    return ch.isascii() and (ch.isalnum() or ch in "._-+")


def rule_phrase(sentence):
    """从一句口播里截一个短语：去掉「第 N 步」这类前缀后取前 12 个字，到中文句读为止。截不出来返回空串。"""
    s = (sentence or "").strip()
    while True:
        t = _LEAD_RE.sub("", s, count=1)
        if t == s:
            break
        s = t
    for k, ch in enumerate(s):
        if ch in _STOP:
            s = s[:k]
            break
    s = s.strip()
    if len(s.replace(" ", "")) > RULE_PHRASE_CHARS:
        cut, n = 0, 0                                # 数到第 12 个字（空格不算字）
        while n < RULE_PHRASE_CHARS:
            n += s[cut] != " "
            cut += 1
        # 不把英文单词、数字从中间截断：截点落在一串英文数字中间时，退到这一串的开头
        while cut > 0 and _is_word_char(s[cut - 1]) and _is_word_char(s[cut]):
            cut -= 1
        s = s[:cut]
    for lq, rq in ("“”", "「」", "『』", "（）", "()"):       # 截断后落单的引号、括号去掉
        if s.count(lq) != s.count(rq):
            s = s.replace(lq, "").replace(rq, "")
    if s.count('"') % 2:
        s = s.replace('"', "")
    s = s.strip()
    while s and item_w(s) > SIDE_MAX_W:
        s = s[:-1]
    return s if len(_norm(s)) >= 2 else ""


# ST3-P12b：条目开头的「第 N 步」（「第 2 步：」「第二步，」等）一律去掉，连同紧跟的标点。其他前缀不动。
_STEP_PREFIX_RE = re.compile(r"^\s*第\s*[一二三四五六七八九十百零两\d]+\s*步\s*[：:，,、．.。;；\s]*")


def strip_step_prefix(text):
    """去掉条目开头的「第 N 步」前缀和紧跟的标点（确定性的后处理，不调模型）。"""
    return _STEP_PREFIX_RE.sub("", text or "", count=1).strip()


def rule_items(win_units):
    """整张图走规则兜底：每句截一个短语（截不出来的句子不出条）。"""
    out = []
    for k, u in enumerate(win_units):
        p = rule_phrase(u["text"])
        if p and all(p != x["text"] for x in out):
            out.append({"n": k + 1, "text": p, "src": "规则"})
    return out


# --------------------------------------------------------------------------
# 一张图
# --------------------------------------------------------------------------

def annotate_one(win_units, heading, before, after, cmds, source_norm, text_call, max_tokens=1024,
                 soft_errors=(), stay_frames=None, hint=None):
    """给一张图生成标注。返回 {"title", "title_font", "items", "source", "log"}。

    模型失败或校验两次不过：没过的条目换成规则从那一句口播里截的短语；两次都拿不到合格的 JSON，
    整张图走规则兜底。source：逐句链 / 逐句链+规则 / 规则兜底 / 只有标题 / 无口播。
    soft_errors：这些异常算「这一次调用失败」（比如模型没有返回）；其余异常（禁止付费、缺密钥）照常抛出。
    """
    log = {"attempts": [], "notes": []}
    if not win_units:
        return {"title": "", "title_font": TITLE_FONTS[0], "items": [], "source": "无口播", "log": log}

    tries = []                        # 每次解析成功的输出：(问题, 标题, 合格条目, 原始输出)
    feedback = None
    for attempt in (1, 2):
        prompt = build_prompt(heading, before, after, cmds, win_units, feedback, stay_frames, hint)
        raw = None
        if text_call is not None:
            try:
                raw = text_call(prompt, max_tokens=max_tokens)
            except soft_errors as e:      # 模型没有返回：按这一次失败算
                log["attempts"].append({"n": attempt, "error": f"{type(e).__name__}: {e}"[:200]})
                continue
        obj, err = parse_json(raw) if raw else (None, "没有返回")
        if obj is None:
            log["attempts"].append({"n": attempt, "error": f"解析失败：{err}", "raw": (raw or "")[:300]})
            feedback = ["上一次的输出不是合法的 JSON 对象"]
            continue
        title, ok_items, problems = validate(obj, win_units, source_norm)
        log["attempts"].append({"n": attempt, "raw": obj, "problems": problems})
        if problems == [BAD_SHAPE]:
            feedback = problems
            continue
        tries.append((problems, title, ok_items, obj))
        if not problems:
            break
        feedback = problems
    if text_call is None:
        log["attempts"] = []

    if not tries:
        title, items = "", rule_items(win_units)
        log["notes"].append("模型两次都没有给出合格的输出，整张图走规则兜底" if text_call is not None
                            else "没有模型调用，整张图走规则兜底")
    else:
        # 以问题少的那一次为准（一样多取后一次）
        problems, title, items, obj = min(reversed(tries), key=lambda t: len(t[0]))
        items = [dict(it, src="模型") for it in items]
        if problems:
            # 两次都没过的条目，依次试：另一次输出里这一句的合格条目 → 这一条去掉前缀后的写法
            # （常见的不过是带了「第 N 步：」之后太宽）→ 用规则从这一句口播里截短语。已有合格条目的句子不补。
            log["notes"] += ["两次校验不过：" + p for p in problems]
            have = {it["n"] for it in items}
            other = {}
            for t in tries:
                for it in t[2]:
                    other.setdefault(it["n"], it["text"])
            for it in obj.get("items") or []:
                try:
                    n = int(it.get("n"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if n in have or not 1 <= n <= len(win_units):
                    continue
                have.add(n)
                if n in other:
                    items.append({"n": n, "text": other[n], "src": "模型"})
                    log["notes"].append(f"第 {n} 句用另一次输出里的合格条目：{other[n]}")
                    continue
                bare = _PREFIX_RE.sub("", str(it.get("text") or ""), count=1).strip()
                if bare and bare != it.get("text"):
                    _t, ok, _p = validate({"items": [{"n": 1, "text": bare}]}, [win_units[n - 1]], source_norm)
                    if ok:
                        items.append({"n": n, "text": ok[0]["text"], "src": "模型"})
                        log["notes"].append(f"去掉前缀后通过：{ok[0]['text']}")
                        continue
                ph = rule_phrase(win_units[n - 1]["text"])
                if ph:
                    items.append({"n": n, "text": ph, "src": "规则"})
            items.sort(key=lambda x: x["n"])

    # ST3-P12b：生成完成之后（两次调用、取长补短、规则兜底都做完）去掉「第 N 步」前缀，再过一遍校验。
    # 放在两次调用之后，第二次调用的反馈和提示词不变，标注缓存照常命中。去掉后为空的条目不出，登记。
    kept = []
    for it in items:
        bare = strip_step_prefix(it["text"])
        if bare == it["text"]:
            kept.append(it)
            continue
        _t, ok, _p = validate({"items": [{"n": 1, "text": bare}]}, [win_units[it["n"] - 1]], source_norm)
        if not ok:
            log["notes"].append(f"去掉「第 N 步」前缀后为空或不合格，不出：{it['text']}")
            log.setdefault("step_prefix", []).append({"n": it["n"], "from": it["text"], "to": None})
            continue
        log["notes"].append(f"去掉「第 N 步」前缀：{it['text']} → {ok[0]['text']}")
        log.setdefault("step_prefix", []).append({"n": it["n"], "from": it["text"], "to": ok[0]["text"]})
        kept.append(dict(it, text=ok[0]["text"]))
    items = kept
    n_rule = sum(1 for it in items if it["src"] == "规则")
    source = ("只有标题" if not items else "逐句链" if n_rule == 0
              else "规则兜底" if n_rule == len(items) else "逐句链+规则")

    title, font, note = fit_title(title)
    if note:
        log["notes"].append(note)
    for it in items:
        u = win_units[it["n"] - 1]
        it.update(unit=u["i"], anchor_abs=u["start"], anchor_end=u["end"], quote=u["text"])
    return {"title": title, "title_font": font, "items": items, "source": source, "log": log}


def annotate_all(image_map, groups, sentences, article, windows, boxed, text_call, soft_errors=(),
                 labels=None, hints=None, progress=None):
    """给一篇文章里的每张截图生成标注。纯计算 + 注入的模型调用（text_call=None 时全部走规则兜底）。

    image_map: 截图的 image_map（每项有 line / before / after / nearest_heading）；
    windows: {文件名: (起帧, 止帧)}，每张图的实际在屏时间（场景试排之后才有）；
    boxed: {文件名: [这张图命令框里的命令]}；
    labels: {文件名: 备用标题}（标签链的短标签，没有就是有意义的文件名）。在屏期间没有分到口播句的图
            （同一句话对应好几张图、每张只停两秒）不调模型，标题用它；
    hints: {文件名: 给模型参考的图片线索}（有意义的文件名、短标签）。
    progress(done, total)：每标完一张图回调一次（ST3-P23 步骤内进度；不给就不回调）。
    返回 ({文件名: {"title", "title_font", "items", "source"}}, 生成记录)。
    """
    from pipeline import scene_build  # noqa: F401（负责把 R148 加进搜索路径）
    import r148_subtitle_align as salign
    body = [g for g in groups if not g.get("starts_with_bracket_title")]
    ca = {r["group_id"]: r for r in salign.align_groups(
        sentences, [{"group_id": g["group_id"], "text": g["group_text"]} for g in body])}
    units = build_units([(g["group_id"], (ca.get(g["group_id"]) or {}).get("char_times") or []) for g in body])
    source_norm = _norm((article or "") + "".join(g.get("group_text", "") for g in groups))
    assigned = assign_units(units, windows)
    ann, log = {}, []
    todo = [e for e in sorted(image_map["images"], key=lambda x: x.get("line") or 0)
            if fn_of(e) in windows]
    for i, e in enumerate(todo):
        fn = fn_of(e)
        st, en = windows[fn]
        win_units = assigned[fn]
        a = annotate_one(win_units, e.get("nearest_heading") or "", e.get("before") or "",
                         e.get("after") or "", (boxed or {}).get(fn) or [], source_norm, text_call,
                         soft_errors=soft_errors, stay_frames=en - st, hint=(hints or {}).get(fn))
        if not a["title"] and (labels or {}).get(fn):
            a["title"], a["title_font"], note = fit_title(labels[fn])
            a["log"]["notes"].append("标题用短标签 / 文件名" + (f"；{note}" if note else ""))
            if a["source"] == "无口播":
                a["source"] = "只有标题"
        ann[fn] = {k: a[k] for k in ("title", "title_font", "items", "source")}
        log.append({"image": fn, "window": [st, en], "source": a["source"], "title": a["title"],
                    "units": [{"n": k + 1, "start": u["start"], "end": u["end"], "text": u["text"]}
                              for k, u in enumerate(win_units)],
                    "items": [{"n": it["n"], "text": it["text"], "src": it["src"]} for it in a["items"]],
                    "attempts": a["log"]["attempts"], "notes": a["log"]["notes"],
                    "step_prefix": a["log"].get("step_prefix", [])})
        if progress is not None:
            progress(i + 1, len(todo))
    return ann, log


def shot_windows(data):
    """场景数据里每张截图（单图镜头）的在屏时间 {文件名: (起, 止)}。"""
    win = {}
    for sc in data["scenes"]:
        sh = sc.get("shot")
        if sh and sh.get("type") != "grid":
            a, b = win.get(sh["image"], (sc["start_frame"], sc["end_frame"]))
            win[sh["image"]] = (min(a, sc["start_frame"]), max(b, sc["end_frame"]))
    return win


# --------------------------------------------------------------------------
# 无新内容的间隔
# --------------------------------------------------------------------------

def content_gaps(st, en, event_frames):
    """一张图在屏期间 [st, en) 里「没有新内容」的各段间隔。

    新内容 = 图自己出现、每条标注、每个命令框。返回 [(起, 止)]，按时间顺序。
    """
    ev = sorted({st} | {min(max(f, st), en) for f in event_frames if f is not None and f < en})
    return [(a, b) for a, b in zip(ev, ev[1:] + [en]) if b > a]


def longest_gap(st, en, event_frames):
    gaps = content_gaps(st, en, event_frames)
    return max((b - a for a, b in gaps), default=0)


def stay_rows(scenes):
    """按最终的场景数据，列出每张截图（单图镜头）的停留情况。

    新内容 = 图出现、条目、命令框。
    返回 [{"image", "group", "start", "end", "dur", "items", "cmds", "source", "title",
           "gaps": [(起, 止)], "longest_gap"}]，按出现时间排。
    """
    rows = {}
    for sc in scenes:
        sh = sc.get("shot")
        if not sh or sh.get("type") == "grid":
            continue
        r = rows.setdefault(sh["image"], {
            "image": sh["image"], "group": sh.get("group"), "start": sc["start_frame"], "end": sc["end_frame"],
            "events": set(), "texts": [], "cmds": 0, "title": sh.get("title") or "",
            "source": sh.get("sidebar_source"), "layout": sh.get("type")})
        r["start"], r["end"] = min(r["start"], sc["start_frame"]), max(r["end"], sc["end_frame"])
        st = sc["start_frame"]
        for it in sh.get("items") or []:
            ab = it.get("anchor_abs")
            key = (ab, it.get("text"))
            if key not in r["texts"]:
                r["texts"].append(key)
                r["events"].add(max(ab, st) if ab is not None else st)
        for el in sh.get("elements") or []:
            if el.get("kind") == "cmd":
                r["events"].add(st + max(0, el.get("anchor_rel") or 0))
                r["cmds"] += 1
    out = []
    for r in sorted(rows.values(), key=lambda x: x["start"]):
        gaps = content_gaps(r["start"], r["end"], r.pop("events"))
        texts = r.pop("texts")
        out.append(dict(r, dur=r["end"] - r["start"], items=len(texts), gaps=gaps,
                        longest_gap=max((b - a for a, b in gaps), default=0)))
    return out
