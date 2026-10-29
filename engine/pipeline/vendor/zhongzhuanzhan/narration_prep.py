# -*- coding: utf-8 -*-
"""
口播稿送 TTS 前的文本预处理（R124 实现；R127 扩展为四类连字符发音规则）。

连字符 "-" 的发音规则（按优先级从高到低）：
  1. 日期范围   （2024-2025）          -> 读"至"，字幕不变（仍是 "-"）
  2. 负数       （句首/空格后的 -3）    -> 读"负"，字幕不变
  3. 产品/模型代号（GPT-4、Claude-3.5） -> 连字符不读（跳过，连读成"GPT四""Claude三点五"）
  4. 厂商代号组合（CC-AWS、cc-逆向 等至少一侧 ASCII 字母/数字） -> 读"杠"，字幕不变

判定优先级（避免冲突）：
  先判日期（两侧都是 4 位数字年份），再判负数（前面非字母数字、后面紧跟数字，
  与日期天然互斥），再判已知产品代号（前面是清单里的模型名、后面跟版本号），
  最后都不命中才按"厂商代号"处理（原 R124 的"字母/数字-字母/数字"规则作为兜底）。

关于 1:1 长度契约：
  日期/负数/厂商 三类都是"一个 '-' -> 一个汉字"，tts_text 与 display_text 逐字符
  同索引（1:1），字幕映射不会错位。
  产品代号 这类是"'-' -> 空"（删掉连字符），会打破 1:1。需要上层在把 tts 对齐
  时间戳映射回 display 字符时，对"被删掉的连字符"槽位做 0 时长处理（见
  prep_tts_text 的注释与 classify_hyphen 返回的 product 类别）。
"""
import os
import re

# 1.25 倍速对应的 speech_rate（火山 v3 TTS：整数，speed = 1 + speech_rate/100，
# 0=1.0x、100=2.0x、-50=0.5x → 25=1.25x）
SPEECH_RATE_1_25X = 25

# TTS 参数常量（默认公共音色，可用环境变量 A2V_TTS_SPEAKER 覆盖）。
DEFAULT_PUBLIC_SPEAKER = "zh_female_shuangkuaisisi_uranus_bigtts"  # 音色
DEFAULT_PUBLIC_RESOURCE_ID = "seed-tts-2.0"      # 公共音色默认资源（合成大模型 2.0）
CLONE_RESOURCE_ID = "seed-icl-2.0"               # 复刻音色资源
PUBLIC_SPEAKER_RESOURCES = (("_uranus_bigtts", "seed-tts-2.0"), ("_mars_bigtts", "seed-tts-1.0"))


def speaker():
    """环境变量 A2V_TTS_SPEAKER，留空用默认公共音色（与 steps.tts_speaker 口径一致）。"""
    return (os.environ.get("A2V_TTS_SPEAKER") or "").strip() or DEFAULT_PUBLIC_SPEAKER


def resource_id(spk=None):
    """按音色选 X-Api-Resource-Id；A2V_TTS_RESOURCE_ID 非空时手动覆盖。"""
    override = (os.environ.get("A2V_TTS_RESOURCE_ID") or "").strip()
    if override:
        return override
    spk = (spk if spk is not None else speaker()).strip()
    if spk.startswith("S_"):
        return CLONE_RESOURCE_ID
    for suffix, rid in PUBLIC_SPEAKER_RESOURCES:
        if spk.endswith(suffix):
            return rid
    return DEFAULT_PUBLIC_RESOURCE_ID


SPEAKER = speaker()
SAMPLE_RATE = 24000                     # 采样率
TITLE_PAT = re.compile(r"【[^】]*】")     # 标题过滤（正文组无【】，no-op）

# 产品/模型代号清单（按常见写法整理，可按需扩展）。
# 只收会带版本号连字符的模型家族名（如 GPT-4、Claude-3.5、DeepSeek-V3）。
KNOWN_MODEL_FAMILIES = [
    "ChatGPT", "DeepSeek", "Claude", "Gemini", "Sonnet",
    "Opus", "Haiku", "Fable", "GPT", "Kimi",
]
# 按长度降序，避免长名被更短的前缀先匹配（如 ChatGPT 中的 GPT）
KNOWN_MODEL_FAMILIES.sort(key=len, reverse=True)


def _is_date(text, i):
    """i 处 '-' 两侧都是 4 位数字（年份）→ 日期范围。"""
    return (i >= 4 and text[i - 4:i].isdigit()
            and i + 5 <= len(text) and text[i + 1:i + 5].isdigit())


def _is_negative(text, i):
    """i 处 '-' 前不是字母/数字（句首/空格/左括号后），后紧跟数字 → 负数。"""
    prev = text[i - 1] if i - 1 >= 0 else ""
    return (not prev.isalnum()) and i + 1 < len(text) and text[i + 1].isdigit()


def _is_product(text, i):
    """i 处 '-' 前面是已知模型名（大小写不敏感、左侧非字母数字边界），后跟版本字符。"""
    for name in KNOWN_MODEL_FAMILIES:
        n = len(name)
        if i < n or text[i - n:i].lower() != name.lower():
            continue
        before = text[i - n - 1] if i - n - 1 >= 0 else ""
        # 边界：名字前面不能是 ASCII 字母/数字（避免 AGPT-4 里的 GPT 被误判）
        if before.isascii() and before.isalnum():
            continue
        if i + 1 < len(text) and (text[i + 1].isalnum() or text[i + 1] in "._"):
            return True
    return False


def _is_ascii_alnum(ch):
    """ASCII 字母或数字（区分于 Python 把汉字也判成 isalnum）。"""
    return ch.isascii() and ch.isalnum()


def _is_vendor(text, i):
    """i 处 '-' 至少一侧是 ASCII 字母/数字（原 R124 技术字符规则，作为兜底）。

    R158 F1：由「两侧都是 ASCII」放宽为「至少一侧 ASCII」，使 cc-逆向、科普篇-Agent
    这类「ASCII-汉字」代号也能命中读「杠」，而不是连字符原样提交豆包被念成「减」。
    仍用 ASCII 判断：汉字「逆」在 Python 里 isalnum()==True，若用 isalnum 会把
    中-中 这类纯中文也误判成厂商代号；「至少一侧 ASCII」恰好排除中-中。
    """
    prev = text[i - 1] if i - 1 >= 0 else ""
    nxt = text[i + 1] if i + 1 < len(text) else ""
    return _is_ascii_alnum(prev) or _is_ascii_alnum(nxt)


def classify_hyphen(text, i):
    """返回 text 中下标 i 的 '-' 所属类别：date / negative / product / vendor / None。

    None 表示不属于四类之一（如 中-中 两侧皆汉字、行首列表标记 "- x"），保持原样。
    """
    if i < 0 or i >= len(text) or text[i] != "-":
        return None
    if _is_date(text, i):
        return "date"
    if _is_negative(text, i):
        return "negative"
    if _is_product(text, i):
        return "product"
    if _is_vendor(text, i):
        return "vendor"
    return None


_REPLACE = {"date": "至", "negative": "负", "product": "", "vendor": "杠"}

# R157 F2: '/' 符号处理。并列/单位做 1:1 替换；路径/日期/分数不替换(保持 '/')。
_SLASH_REPLACE = {"parallel": "或", "unit": "每"}


def _is_cjk(ch):
    return "\u4e00" <= ch <= "\u9fff"


def _slash_in_path_word(text, i):
    """判断 '/' 是否属于路径：前后都紧贴非空白字符(排除 'A / B' 带空格并列)，
    且所在非空白段含 >=2 个 '/' 或该段以 '/' 开头。"""
    prev = text[i - 1] if i - 1 >= 0 else ""
    nxt = text[i + 1] if i + 1 < len(text) else ""
    if prev.isspace() or nxt.isspace():
        return False
    sep = " \t\n，。；：、（）【】「」“”\"'`|"
    lo = i
    while lo > 0 and text[lo - 1] not in sep:
        lo -= 1
    hi = i
    while hi < len(text) - 1 and text[hi + 1] not in sep:
        hi += 1
    word = text[lo:hi + 1]
    return word.count("/") >= 2 or word.startswith("/")


def _ascii_word_left(text, i):
    j = i - 1
    while j >= 0 and text[j].isascii() and text[j].isalpha():
        j -= 1
    return text[j + 1:i]


def _ascii_word_right(text, i):
    j = i + 1
    while j < len(text) and text[j].isascii() and text[j].isalpha():
        j += 1
    return text[i + 1:j]


def _prev_nonspace(text, i):
    j = i - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    return text[j] if j >= 0 else ""


def _next_nonspace(text, i):
    j = i + 1
    while j < len(text) and text[j].isspace():
        j += 1
    return text[j] if j < len(text) else ""


def classify_slash(text, i):
    """返回 text 中下标 i 的 '/' 所属类别：path/unit/parallel/number/None。

    判定顺序（重要）：先数字/数字(number, 日期分数先于路径、不处理)，再路径(多 / 或词首)，
    再单位(两侧 ≤2 字符 ASCII 缩写)，最后并列(两侧跳过空格的字符是中文/字母/数字)。None=未分类。
    """
    if i < 0 or i >= len(text) or text[i] != "/":
        return None
    prev = text[i - 1] if i - 1 >= 0 else ""
    nxt = text[i + 1] if i + 1 < len(text) else ""
    if prev.isdigit() and nxt.isdigit():
        return "number"
    if _slash_in_path_word(text, i):
        return "path"
    left = _ascii_word_left(text, i)
    right = _ascii_word_right(text, i)
    if (left and right and left.isalpha() and len(left) <= 2
            and right.isalpha() and len(right) <= 2):
        return "unit"
    p = _prev_nonspace(text, i)
    n = _next_nonspace(text, i)
    if (p and n and (p.isalnum() or _is_cjk(p)) and (n.isalnum() or _is_cjk(n))):
        return "parallel"
    return None


def iter_slashes(text):
    """返回所有 '/' 的 (下标, 类别, 上下文)。用于 F2 报警与诊断。"""
    hits = []
    for i, ch in enumerate(text):
        if ch == "/":
            start = max(0, i - 12)
            end = min(len(text), i + 12)
            hits.append({
                "index": i,
                "category": classify_slash(text, i),
                "context": text[start:end],
            })
    return hits

# 中文数字（列表序号 1:1 逐字符映射用，见 R154 W1）
_CN = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
_LIST_MARKER_RE = re.compile(r"^(\d+)\s*([.、．)])")


def _cn_number(n):
    """阿拉伯数字 -> 中文数字（1..99，本文列表序号均为个位数）。"""
    if n < 10:
        return _CN[n]
    if n < 20:
        return "十" + ("" if n == 10 else _CN[n % 10])
    tens, ones = divmod(n, 10)
    return _CN[tens] + "十" + ("" if ones == 0 else _CN[ones])


def is_section_di(text, i):
    """text[i] 是否「第」且其后紧跟点分序号（如 2.3.7）——豆包会把「第 2.3.7 节」念成
    「第二点三点七节」语调奇怪，故删掉这个「第」。返回 bool。"""
    if i < 0 or i >= len(text) or text[i] != "第":
        return False
    j = i + 1
    while j < len(text) and text[j].isspace():
        j += 1
    k = j
    seen_dot = False
    while k < len(text) and (text[k].isdigit() or text[k] == "."):
        if text[k] == ".":
            seen_dot = True
        k += 1
    return seen_dot and k > j


def prep_tts_text(text):
    """按四类规则替换连字符，并把行首阿拉伯序数列表标记转中文数字，得到 TTS 朗读版。

    date/negative/vendor 三类为 1:1 替换（长度不变）；product 会删除 '-'（长度减 1）。

    R154 W1 修复：豆包把 markdown 有序列表标记 "1." "2." 读成「文档级全局计数器」
    （目录读一二三四，后续列表继续计数读五六七八）。这里在行首把 "1." -> "一、"（1:1），
    让豆包按中文数字字面朗读，避免字幕「1」念成「5」。
    """
    m = _LIST_MARKER_RE.match(text)
    if m:
        text = _cn_number(int(m.group(1))) + "、" + text[m.end():]
    # R168 F4: 谐音梗例外——原文「A➗」("➗" U+2797 除号，"除"谐音"畜")应念「A畜」。
    # 豆包把 "➗" 念成"除"；这里把 A 后面的 "➗" 单字符替换成"畜"，A 与 ➗ 之间可有空格
    # ("A➗" 2字符 → "A畜" 2字符 / "A ➗" 3字符 → "A 畜" 3字符)，严格 1:1 不破坏逐字符映射。
    # 只特判 A 后跟除号这一个谐音梗组合(不扩展成谐音梗词表)；其余位置的 "➗" 保持豆包默认念法不变。
    text = re.sub(r"A(\s*)➗", lambda m: "A" + m.group(1) + "畜", text)
    out = []
    for i, ch in enumerate(text):
        if ch == "-":
            cat = classify_hyphen(text, i)
            out.append(_REPLACE.get(cat, ch))
        elif ch == "/":
            cat = classify_slash(text, i)
            out.append(_SLASH_REPLACE.get(cat, ch))
        elif ch == "、":
            # R160 F3: 豆包在英文专名列表里吞掉顿号(如 "Claude code、Hermes" 连读成
            # "code Hermes")。顿号→逗号 1:1 替换，让豆包识别为停顿，不破坏 display↔tts 1:1 契约。
            out.append("，")
        elif ch == " ":
            # R167 T2: 多词英文专名(如 "Claude code")的普通空格，豆包会拆成两个词，
            # 顿号→逗号后 claude-code 边界被放大(念成"Claude——code")。空格→U+2060
            # (word joiner, 1:1) 让豆包不拆词、当一个词念。只对"两侧都是 ASCII 字母/数字"
            # 的空格生效(英文专名)，不碰 "GPT 系列" 这类英文+中文的空格。
            if i > 0 and i + 1 < len(text) and _is_ascii_alnum(text[i-1]) and _is_ascii_alnum(text[i+1]):
                out.append("\u2060")
            else:
                out.append(ch)
        elif ch == "_":
            # ST3-P3c 修复四：英文标识符(如 DISTUTILS_USE_SDK)里的下划线 → 空格，
            # 让豆包按词边界念("DISTUTILS USE SDK")，而不是把 _ 念成 "underscore"。
            # 1:1 替换(长度不变)，只改 TTS 文本、不改字幕(display 仍显示 "_")。
            if i > 0 and i + 1 < len(text) and _is_ascii_alnum(text[i-1]) and _is_ascii_alnum(text[i+1]):
                out.append(" ")
            else:
                out.append(ch)
        elif ch == "第":
            # ST3-P3d 修复一：「第 2.3.7 节」这类点分序号的「第」删除（豆包念成
            # 「第二点三点七」语调奇怪）；display 保留「第」，由 align_group_chars 给 0 时长槽。
            if is_section_di(text, i):
                out.append("")
            else:
                out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def display_text(text):
    """字幕显示版 = 原始文本（保持 '-' 不变）。此处为显式契约说明。"""
    return text


def iter_hyphens(text):
    """返回所有连字符的 (下标, 类别, 上下文)。用于诊断与测试。"""
    hits = []
    for i, ch in enumerate(text):
        if ch == "-":
            start = max(0, i - 12)
            end = min(len(text), i + 12)
            hits.append({
                "index": i,
                "category": classify_hyphen(text, i),
                "context": text[start:end],
            })
    return hits


def iter_tech_hyphens(text):
    """兼容旧接口：返回"厂商代号"类（原技术字符规则）的连字符命中。"""
    return [h for h in iter_hyphens(text) if h["category"] == "vendor"]
