# -*- coding: utf-8 -*-
"""文章要素识别：代码块 / 命令块 / 表格（ST3-P2 A）。

从原始 markdown 机械抽取三类要素，不依赖 LLM：

  - **代码块**：``` 围栏的代码块，且不属于下面的「命令块」。
  - **命令块**：``` 围栏的语言标识是 shell/bash/sh/cmd/bat/powershell/console；
    或者没有语言标识，但每个非空行都以常见命令开头（命令前缀表写在 `COMMAND_PREFIXES`）。
  - **表格**：连续的 `|` 行（含表头行 + 分隔行）。

  - **正文里的命令**（ST3-P11 R3）：不在围栏里、直接写在正文句子中的命令，见
    `extract_inline_commands`。它不改变上面三类的识别结果，也不计入 `has_elements`。

每个要素记录：类型、语言标识、原文内容、在文中的行号、最近标题。
`has_elements(text)` 与 `extract_elements(text)` 用同一套解析，保证 S1 的
「是否追加口播规则」与 S5 的「识别并挂组」判断一致。
"""
import re

# 语言标识 → 命令块（任务书 A 表第二行）
COMMAND_LANGS = {"shell", "bash", "sh", "cmd", "bat", "powershell", "console",
                 "zsh", "fish", "terminal", "dos"}

# 纯文本类语言标识：一律视为没有语言标识，按命令前缀规则处理
PLAIN_LANGS = {"text", "txt", "plain", "plaintext", "none", "undefined", "unknown"}

# 常见命令前缀（git、pip、python、cd、npm 等）。匹配时要求命令名后跟空格、
# 或能够独立成词，以免把 `pythonism` 这类普通单词当成 `python` 命令。
COMMAND_PREFIXES = (
    "git", "pip", "pip3", "python", "python3", "py", "cd", "npm", "npx", "pnpm", "yarn",
    "conda", "set", "export", "unset", "curl", "wget", "ls", "dir", "mkdir",
    "rm", "rmdir", "cp", "mv", "cat", "echo", "chmod", "chown", "sudo", "apt",
    "apt-get", "yum", "brew", "docker", "kubectl", "node", "go", "cargo", "make",
    "cmake", "ninja", "gcc", "g++", "clang", "java", "javac", "ffmpeg", "nvcc",
    "git", "cd", "start", "call", "winget", "scoop", "choco", "pacman", "dnf",
    "cmd",
)

_FENCE_RE = re.compile(r"^(\s*)```(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")


def _is_command_line(line, lang_given):
    """判断单个代码行是否像一条命令。lang_given 为 False 时启用前缀匹配。"""
    s = line.strip()
    if not s:
        return False
    if lang_given:
        return True  # 有 shell 系语言标识 → 整块都是命令
    if s.startswith(("#", "//", "/*", "<!--")):
        return True  # 注释行不算破坏命令块
    low = s.lower()
    for p in COMMAND_PREFIXES:
        if low == p or low.startswith(p + " ") or low.startswith(p + "\\"):
            return True
        # 形如 `pip install`（前缀后紧跟空格）或 `python setup.py`
        if low.startswith(p) and len(low) > len(p) and low[len(p)] in (" ", "\t", "\\", "/"):
            return True
    return False


def _split_table(lines, start_idx):
    """从 start_idx 起收集连续的 `|` 行，返回 (table_lines, next_idx)。"""
    tbl = []
    j = start_idx
    while j < len(lines) and _TABLE_ROW_RE.match(lines[j]):
        tbl.append(lines[j].strip())
        j += 1
    return tbl, j


def _parse_table_rows(tbl):
    """把 `| a | b |` 行解析成 (header, rows)。分隔行（---）会被剔除。"""
    def cells(line):
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    parsed = [cells(l) for l in tbl]
    header = None
    rows = []
    for row in parsed:
        if all(re.fullmatch(r":?-{1,}:?", c or "") for c in row):
            continue  # 分隔行
        if header is None:
            header = row
        else:
            rows.append(row)
    if header is None:
        header = parsed[0] if parsed else []
    return header, rows


def extract_elements(text):
    """从 markdown 文本抽取代码块/命令块/表格，返回要素列表（按原文出现顺序）。"""
    lines = text.splitlines()
    n = len(lines)
    elements = []
    current_heading = None
    i = 0
    while i < n:
        line = lines[i]
        h = _HEADING_RE.match(line)
        if h:
            current_heading = h.group(2).strip()
            i += 1
            continue

        fm = _FENCE_RE.match(line)
        if fm:
            # 代码围栏块
            lang = fm.group(2).strip()
            fence_line = i + 1
            body = []
            j = i + 1
            while j < n and not _FENCE_RE.match(lines[j]):
                body.append(lines[j])
                j += 1
            # j 停在闭合 ```（或越界）
            raw = "\n".join(body).rstrip("\n")
            body_lines = body
            # 判断命令块
            is_command = False
            if lang.lower() in COMMAND_LANGS:
                is_command = True
            elif not lang or lang.lower() in PLAIN_LANGS:
                # 无语言标识（含纯文本 text/plaintext 等）：每个非空行都以命令开头才算命令块
                nonempty = [l for l in body_lines if l.strip()]
                if nonempty and all(_is_command_line(l, False) for l in nonempty):
                    is_command = True
            elements.append({
                "id": f"el_{len(elements) + 1:03d}",
                "type": "command" if is_command else "code",
                "lang": lang,
                "lines": body_lines,
                "raw": raw,
                "line": fence_line,
                "nearest_heading": current_heading,
            })
            i = j + 1  # 跳过闭合 ```
            continue

        if _TABLE_ROW_RE.match(line):
            tbl, j = _split_table(lines, i)
            if len(tbl) >= 2:
                header, rows = _parse_table_rows(tbl)
                # 至少要有表头 + 分隔行才算表格；分隔行已被 _parse_table_rows 剔除
                elements.append({
                    "id": f"el_{len(elements) + 1:03d}",
                    "type": "table",
                    "lang": "",
                    "header": header,
                    "rows": rows,
                    "raw": "\n".join(tbl),
                    "line": i + 1,
                    "nearest_heading": current_heading,
                })
                i = j
                continue
        i += 1
    return elements


def has_elements(text):
    """是否包含代码块/命令块/表格（与 extract_elements 同一套解析）。"""
    return len(extract_elements(text)) > 0


# --------------------------------------------------------------------------
# 正文里的命令（ST3-P11 R3）
# --------------------------------------------------------------------------

# 一行放得下的上限：36 个等宽英文字符的宽度，一个汉字（全角字符）按 2 个字符计。
INLINE_CMD_MAX_UNITS = 36
# 片段到句末或中文句读为止。
_CN_STOPS = "。，、；：？！（）【】《》“”‘’"
_QUOTE_PAIRS = {"“": "”", "‘": "’", "「": "」", "`": "`", '"': '"', "'": "'"}
# 前缀前若紧贴这些字符，说明它属于某个单词、路径或网址，不是命令开头。
_NOT_BEFORE = re.compile(r"[A-Za-z0-9_\-./\\:@#]")
# 光秃秃一个命令名（后面没有参数）时，前面必须是这些动词才算命令："输入cmd" 算，"Python（程序语言）" 不算。
_INPUT_VERBS = ("输入", "键入", "运行", "执行", "敲入", "敲")
_INLINE_RE = re.compile(
    r"(?<![A-Za-z0-9_\-./\\:@#])(" + "|".join(
        re.escape(p) for p in sorted(set(COMMAND_PREFIXES), key=len, reverse=True)) + r")(?![A-Za-z0-9_])",
    re.I)
# 带参数的命令：命令名后面是空白，再跟字母或参数符号（数字不算，"Python 3.10" 是版本号不是命令）。
_ARG_START = re.compile(r"[ \t]+[A-Za-z\-./\\~\"'$%+]")


def cmd_units(text):
    """命令的显示宽度（等宽英文字符数）：ASCII 算 1，其余（汉字、全角符号）算 2。"""
    return sum(1 if ord(ch) < 128 else 2 for ch in text)


def display_command(text):
    """上屏文字：原文照抄，只在 `+` 两侧补空格。"""
    return re.sub(r"[ \t]*\+[ \t]*", " + ", text).strip()


def _sentence_around(line, start, end):
    """命令所在的那句话（按 。！？； 切），返回 (整句, 去掉命令片段后的那句)。"""
    lo = max(line.rfind(ch, 0, start) for ch in "。！？；") + 1
    his = [i for i in (line.find(ch, end) for ch in "。！？；") if i >= 0]
    hi = min(his) + 1 if his else len(line)
    return line[lo:hi].strip(), (line[lo:start] + line[end:hi]).strip()


def extract_inline_commands(text):
    """从正文里找出直接写在句子中的命令（不含围栏代码块、表格、标题行、图片行）。

    规则（对所有文章相同）：
      - 以 `COMMAND_PREFIXES` 里的命令名开头，且它前面不是字母、数字、路径或网址字符；
      - 命令名后面带参数（空白 + 字母或参数符号），或者光秃秃一个命令名但紧跟在
        "输入 / 运行 / 执行"这类动词（可隔一个引号或"命令"二字）后面；
      - 片段从命令名开始，到句末或中文句读为止；命令被引号包着时到右引号为止。
    返回 [{"line", "start", "raw", "text", "units", "long", "sentence", "context"}]，按原文顺序。
    `text` 是上屏文字（`display_command`），`long` 表示超过一行（> INLINE_CMD_MAX_UNITS）。
    """
    out = []
    in_fence = False
    for ln_no, line in enumerate(text.splitlines(), 1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        s = line.strip()
        if in_fence or not s or s.startswith("![") or _HEADING_RE.match(line) or _TABLE_ROW_RE.match(line):
            continue
        pos = 0
        while True:
            m = _INLINE_RE.search(line, pos)
            if not m:
                break
            st = m.start()
            before = line[:st]
            quote = _QUOTE_PAIRS.get(before[-1:]) if before else None
            # 片段终点
            if quote and line.find(quote, m.end()) >= 0:
                en = line.find(quote, m.end())
            else:
                en = len(line)
                for i in range(m.end(), len(line)):
                    if line[i] in _CN_STOPS:
                        en = i
                        break
            raw = line[st:en].rstrip()
            has_arg = bool(_ARG_START.match(line, m.end())) and len(raw) > len(m.group(1))
            if not has_arg:
                # 光秃秃的命令名：片段里不能再有别的内容，且前面要有输入类动词
                lead = before[:-1] if quote else before
                lead = lead.rstrip()
                if lead.endswith("命令"):
                    lead = lead[:-2].rstrip()
                if raw.lower() != m.group(1).lower() or not lead.endswith(_INPUT_VERBS):
                    pos = m.end()
                    continue
            shown = display_command(raw)
            sentence, context = _sentence_around(line, st, en)
            units = cmd_units(shown)
            out.append({"line": ln_no, "start": st, "raw": raw, "text": shown, "units": units,
                        "long": units > INLINE_CMD_MAX_UNITS,
                        "sentence": re.sub(r"^\s*(?:\d+[.、]|[-*])\s*", "", sentence),
                        "context": re.sub(r"^\s*(?:\d+[.、]|[-*])\s*", "", context)})
            pos = max(en, m.end())
    return out
