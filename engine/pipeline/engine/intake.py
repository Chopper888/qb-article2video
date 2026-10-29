# -*- coding: utf-8 -*-
r"""新文章导入：输入检查、文件夹命名、导入锁（ST3-P22 §3.2～§3.4）。

`import` 子命令和 `run --input` 都走 `run_import()`，只有这一份实现。

检查（`check_article`）在花第一分钱之前把"后面才会失败"的输入问题全部查出来：
  - 编码：必须 UTF-8（BOM 去掉、标题照常识别）；不是 UTF-8 的在建文件夹之前就拦下；
  - 图片引用：用 `image_map.extract_images` 抽引用、`input_image_path` 解析路径——
    与 S0 / S5 用的是同一个正则、同一个解析函数；`<img>` 标签另查（引擎不认它，图会悄悄丢）。
每个问题是 `{"kind", "detail", "hint"}`（另带 `line` 时是 md 里的行号）。

文件夹名：`<序号>-<清洗后标题前 N 个字符>`（N 默认 12）。序号：待转清单里有就用清单的，
否则取 `--work-root` 下现有 4 位数字序号的最大值 + 1。重名（来源不同）加 `-2`、`-3`……
同一 `--work-root` 下的导入用 `.import.lock` 文件锁串行化。
"""
import contextlib
import hashlib
import json
import os
import re
import shutil
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

from pipeline import image_map
from pipeline.engine import tempdir

INPUT_DIR = "00-输入"
IMG_DIR = "图片和附件"
ALLOWED_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
UTF8_BOM = b"\xef\xbb\xbf"

DEFAULT_TITLE_CHARS = 12
TITLE_CHARS_MIN, TITLE_CHARS_MAX = 1, 60
IMPORT_LOCK = ".import.lock"
# v5.8：导入 zip 时的解压目录 <work-root>/_a2v_import/<编号>/；和 _a2v_tmp 一样不是文章文件夹
IMPORT_DIRNAME = "_a2v_import"
NOT_ARTICLE_DIRS = (tempdir.TMP_DIRNAME, IMPORT_DIRNAME)
# 来源.json 的「导入规则」：2 = v5.8 起（标题去转义）。没有这个字段的是旧规则导入的，
# 再导入时仍按旧规则生成 00-输入，保证内容没变时 changed=false（--refetch 才升到新规则）。
IMPORT_RULES = 2

CONTENT_TYPE_CHOICES = ("auto", "theory", "screenshot")
# 写进 来源.json 的「内容类型」（S0 的 TYPE_ALIASES 认这两个写法）
CONTENT_TYPE_LABELS = {"theory": "理论型", "screenshot": "截图型"}

# 问题种类 -> 给用户看的处理办法
# v5.8（APP4）：图片可以放在文章文件夹里任意位置（含与 md 同一层、任意子文件夹），文案不再要求「图片和附件」。
HINTS = {
    "encoding_invalid": "文章不是 UTF-8 编码：请用记事本等工具另存为 UTF-8 后重新导入",
    "image_missing": "文章文件夹里找不到这个文件：补上图片，或改正 md 里的路径和文件名",
    "image_outside_folder": "图片要放在文章文件夹里面（与 md 同一层或任意子文件夹都行），"
                            "不能用 ../ 指向文章文件夹外面",
    "image_absolute_path": "不要用绝对路径：把图片复制进文章文件夹，md 里写相对路径，例如 图片和附件/文件名",
    "image_remote_url": "不支持网络图片：先把图片下载到文章文件夹里，md 里写相对路径",
    "image_title_syntax": "去掉括号里文件名后面的标题文字，只保留图片路径",
    "image_html_tag": "引擎不认 <img> 标签，图片会丢失：改成 ![](相对路径) 的写法",
    "image_format_unsupported": "只支持 png / jpg / jpeg / webp / gif 格式，请先把图片转成 png",
    "unhandled_block": "飞书里的视频、附件、嵌入表格等不会进视频；需要的话请改成图片或文字",
    # 以下 v5.8 新增
    "image_fallback": "已自动改用文章文件夹里唯一的同名图片；如果用错了图，请改正 md 里的路径",
    "zip_corrupt": "压缩包打不开：请重新导出或重新压缩后再导入",
    "zip_unsafe_path": "压缩包里有指向压缩包外面的路径，为安全起见不导入：请重新导出或重新压缩",
    "zip_no_md": "压缩包里没有 md 文件：请确认导出的是 Markdown 格式",
    "zip_multiple_md": "压缩包里有多个 md 文件，不知道导入哪一个：一个压缩包只放一篇文章",
    # R101 L
    "zip_too_large": "压缩包太大（超过 2 GB），请删掉不需要的文件后重试",
}


class InputProblems(Exception):
    """导入前就发现的阻断问题（还没建文件夹，例如编码不对）。`problems` 同 check_article。"""

    def __init__(self, problems):
        super().__init__("；".join(p["detail"] for p in problems))
        self.problems = problems


def problem(kind, detail, **extra):
    d = {"kind": kind, "detail": detail, "hint": HINTS[kind]}
    d.update(extra)
    return d


# --------------------------------------------------------------------------
# 编码
# --------------------------------------------------------------------------


def decode_md(md_bytes, name="文章.md"):
    """md 字节 → 文本（去掉 UTF-8 BOM）。不是 UTF-8 抛 InputProblems。"""
    if md_bytes.startswith(UTF8_BOM):
        md_bytes = md_bytes[len(UTF8_BOM):]
    try:
        return md_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise InputProblems([problem("encoding_invalid",
                                     f"{name} 第 {e.start + 1} 个字节起不是 UTF-8")])


# --------------------------------------------------------------------------
# 图片引用
# --------------------------------------------------------------------------


def input_image_path(inp_dir, im):
    """md 里一条图片引用（`image_map.extract_images` 的一项）→ (相对路径, 在 00-输入 下的路径)。

    S0 识图、S6 按内容去重和导入检查都用这一个函数，规则只有一份。
    """
    rel = im["source_relative_path"].replace("\\", "/")
    return rel, Path(inp_dir) / rel


_URL_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//|^data:", re.I)
_TITLE_RE = re.compile(r"""\s+(?:"[^"]*"|'[^']*')\s*$""")
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[/\\]")
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>?", re.I)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _nfc(name):
    return unicodedata.normalize("NFC", name)


class _NameIndex:
    """文章文件夹里（含子文件夹）的文件按文件名索引；第一次用到时才扫（同名兜底用）。"""

    def __init__(self, base):
        self.base, self._idx = Path(base), None

    def find(self, name):
        if self._idx is None:
            self._idx = {}
            for dp, dns, fns in os.walk(self.base):
                dns[:] = sorted(d for d in dns if not d.startswith(".") and d not in NOT_ARTICLE_DIRS)
                for fn in sorted(fns):
                    self._idx.setdefault(_nfc(fn), []).append(Path(dp) / fn)
        return self._idx.get(_nfc(name), [])


def _resolve_ref(raw, rel, path, base, names):
    """一条引用 → (问题种类或 None, 图片文件或 None, 兜底说明或 None)。

    v5.8：图片可以在文章文件夹 `base` 里任意位置；路径找不到、但文件夹里恰好有一个同名文件时用它。
    """
    if _URL_RE.search(raw):
        return "image_remote_url", None, None
    if _TITLE_RE.search(raw):
        return "image_title_syntax", None, None
    if rel.startswith("/") or _WIN_ABS_RE.match(rel) or Path(rel).is_absolute():
        return "image_absolute_path", None, None
    try:
        real, root = path.resolve(), base.resolve()
        inside = real != root and real.is_relative_to(root)
    except (OSError, ValueError):
        inside = False
    if not inside:
        return "image_outside_folder", None, None
    if Path(rel).suffix.lower() not in ALLOWED_IMAGE_EXTS:
        return "image_format_unsupported", None, None
    if real.is_file():
        return None, real, None
    cands = names.find(PurePosixPath(rel).name)
    if len(cands) == 1:
        return None, cands[0].resolve(), cands[0].relative_to(base).as_posix()
    if len(cands) > 1:
        return "image_missing", None, f"文章文件夹里有 {len(cands)} 个同名文件，无法确定用哪一个"
    return "image_missing", None, None


def _html_img_tags(lines):
    """md 里（代码块之外）的 `<img>` 标签：[(行号, 原文)]。"""
    out, fence = [], False
    for i, ln in enumerate(lines, 1):
        if _FENCE_RE.match(ln):
            fence = not fence
            continue
        if fence:
            continue
        for m in _IMG_TAG_RE.finditer(ln):
            out.append((i, m.group(0)[:200]))
    return out


def check_text(text, md_path, base):
    """检查一篇 md 正文的图片引用。返回 (阻断问题列表, {原文写法: 解析出的图片文件}, 提示列表)。

    `base` 是文章文件夹（本地导入时是**源文件夹**，所以问题说的是用户写的原文和原文件名，
    ST3-P23 §3.3；飞书导入后检查时是 `00-输入`）。提示目前只有同名兜底（v5.8）。
    """
    base = Path(base)
    names = _NameIndex(base)
    problems, ok, warns = [], {}, []
    for im in image_map.extract_images(text, str(md_path))["images"]:
        raw = im["image_path"]
        rel, p = input_image_path(base, im)
        if len(rel) > 1 and rel.startswith("<") and rel.endswith(">"):
            rel = rel[1:-1].strip()
            p = base / rel
        kind, f, note = _resolve_ref(raw, rel, p, base, names)
        if kind:
            detail = f"第 {im['line']} 行：{raw}" + (f"（{note}）" if note else "")
            problems.append(problem(kind, detail, line=im["line"]))
            continue
        if note and raw not in ok:
            warns.append(problem("image_fallback",
                                 f"第 {im['line']} 行：{raw} 找不到，改用 {note}", line=im["line"]))
        ok.setdefault(raw, f)
    for line, tag in _html_img_tags(text.splitlines()):
        problems.append(problem("image_html_tag", f"第 {line} 行：{tag}", line=line))
    problems.sort(key=lambda d: d.get("line") or 0)
    return problems, ok, warns


def check_article(inp_dir):
    """检查导入后的 `00-输入`：返回 (阻断问题列表, 能用的图片张数)。"""
    inp_dir = Path(inp_dir)
    md = inp_dir / "文章.md"
    try:
        text = decode_md(md.read_bytes())
    except InputProblems as e:
        return e.problems, 0
    problems, ok, _ = check_text(text, md, inp_dir)
    return problems, len(set(ok.values()))


# --------------------------------------------------------------------------
# Markdown 反斜杠转义（v5.8）
# --------------------------------------------------------------------------

# CommonMark：反斜杠后面跟任意 ASCII 标点就是转义（飞书导出会写 \. \- \& \_ \* 等）
_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_CODE_SPAN_RE = re.compile(r"(`+)(?:.*?[^`])?\1(?!`)")
_HEADING_LINE_RE = re.compile(r"^( {0,3}#{1,6}[ \t]+)(.*?)(\r?\n)?$", re.S)


def unescape_md(s):
    r"""去掉 Markdown 反斜杠转义：`3\.2 基础篇\-a\&b` → `3.2 基础篇-a&b`。行内代码里的不动。"""
    out, pos = [], 0
    for m in _CODE_SPAN_RE.finditer(s):
        out.append(_MD_ESCAPE_RE.sub(r"\1", s[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_MD_ESCAPE_RE.sub(r"\1", s[pos:]))
    return "".join(out)


def unescape_headings(text):
    """md 里各级标题行（代码块之外）去转义；正文其他行原样不动。

    APP4 查实：转义只经标题进到了口播的章节标记【…】和进度条章节名；正文句子、字幕、标注里没有。
    """
    out, fence = [], False
    for ln in text.splitlines(keepends=True):
        if _FENCE_RE.match(ln):
            fence = not fence
        elif not fence:
            m = _HEADING_LINE_RE.match(ln)
            if m:
                ln = m.group(1) + unescape_md(m.group(2)) + (m.group(3) or "")
        out.append(ln)
    return "".join(out)


def md_title(text, fallback):
    """md 第一个一级标题（没有时用 fallback）。"""
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else fallback


# --------------------------------------------------------------------------
# 本地输入：选 md、解压 zip（v5.8）
# --------------------------------------------------------------------------


def pick_md(folder):
    """文件夹里的文章 md：`文章.md`，否则唯一一个 `*.md`；返回 (Path 或 None, 这一层 md 的个数)。"""
    folder = Path(folder)
    if (folder / "文章.md").is_file():
        return folder / "文章.md", 1
    mds = sorted(p for p in folder.glob("*.md") if p.is_file())
    return (mds[0] if len(mds) == 1 else None), len(mds)


_ZIP_JUNK = ("__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini")
# R101 L：zip 导入上限 2 GB——压缩包本身、登记的解压后总大小、实际解压出来的累计字节，任一超过就不导入
ZIP_MAX_BYTES = 2 * 1024 ** 3
_ZIP_TOO_LARGE = "压缩包太大（超过 2 GB），请删掉不需要的文件后重试"
_ZIP_ABS_RE = re.compile(r"^(?:/|[A-Za-z]:|//)")


def _zip_member_name(info):
    """zip 成员名：没标 UTF-8 的（Windows 资源管理器打的包）按 UTF-8、再按 GBK 还原中文。"""
    name = info.filename
    if not info.flag_bits & 0x800:
        try:
            raw = name.encode("cp437")
        except UnicodeEncodeError:
            raw = None
        if raw is not None:
            for enc in ("utf-8", "gbk"):
                try:
                    name = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
    return name.replace("\\", "/")


class _ZipTooLarge(Exception):
    pass


def _zip_problem(kind, detail):
    return InputProblems([problem(kind, detail)])


def zip_extract_dir(work_root, zip_path):
    """zip 的解压位置 `<work-root>/_a2v_import/<编号>`；编号由 zip 的绝对路径算出，同一个 zip 每次相同。"""
    key = str(Path(zip_path).resolve())
    return Path(work_root) / IMPORT_DIRNAME / ("zip-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12])


def extract_zip(zip_path, work_root):
    """解压 zip，返回 (文章文件夹, 文章 md)。zip 本身有问题时抛 InputProblems（解压目录不留）。

    zip 里只有一层外包文件夹时自动进入那一层；md 按文件夹规则挑（`文章.md`，否则唯一一个 md）。
    """
    zip_path = Path(zip_path)
    name = zip_path.name
    try:
        too_big = zip_path.stat().st_size > ZIP_MAX_BYTES
    except OSError:
        too_big = False
    if too_big:
        raise _zip_problem("zip_too_large", _ZIP_TOO_LARGE)
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        raise _zip_problem("zip_corrupt", f"{name} 不是有效的压缩包或已损坏（{type(e).__name__}）")
    with zf:
        members = []
        for info in zf.infolist():
            n = _zip_member_name(info)
            parts = [x for x in n.split("/") if x not in ("", ".")]
            if _ZIP_ABS_RE.match(n) or ".." in parts:
                raise _zip_problem("zip_unsafe_path", f"{name} 里的 {n} 指向压缩包外面")
            if not parts or parts[0] in _ZIP_JUNK or parts[-1] in _ZIP_JUNK:
                continue
            if info.flag_bits & 0x1:
                raise _zip_problem("zip_corrupt", f"{name} 加了密码，打不开")
            members.append((info, parts))
        if sum(max(0, info.file_size) for info, _ in members) > ZIP_MAX_BYTES:
            raise _zip_problem("zip_too_large", _ZIP_TOO_LARGE)
        dest = zip_extract_dir(work_root, zip_path)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        base = dest.resolve()
        written = 0
        try:
            for info, parts in members:
                target = dest.joinpath(*parts)
                # R101 L：兜底——不管成员名怎么写（例如 Windows 上的 C:x 盘符相对路径），解出来必须在解压目录里
                if not target.resolve().is_relative_to(base) or target.resolve() == base:
                    shutil.rmtree(dest, ignore_errors=True)
                    raise _zip_problem("zip_unsafe_path", f"{name} 里的 {'/'.join(parts)} 指向压缩包外面")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fi, open(target, "wb") as fo:
                    # 按块复制并累计：登记的大小可能是假的（zip 炸弹），实际解出来超过上限就停
                    while True:
                        chunk = fi.read(1 << 20)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > ZIP_MAX_BYTES:
                            raise _ZipTooLarge()
                        fo.write(chunk)
        except _ZipTooLarge:
            shutil.rmtree(dest, ignore_errors=True)
            raise _zip_problem("zip_too_large", _ZIP_TOO_LARGE)
        except (zipfile.BadZipFile, OSError, EOFError, ValueError, RuntimeError) as e:
            shutil.rmtree(dest, ignore_errors=True)
            raise _zip_problem("zip_corrupt", f"{name} 解压失败，压缩包可能已损坏（{type(e).__name__}）")
    root = dest
    top = [p for p in root.iterdir()]
    if len(top) == 1 and top[0].is_dir():
        root = top[0]
    md, n = pick_md(root)
    if md is None:
        shutil.rmtree(dest, ignore_errors=True)
        if n == 0:
            raise _zip_problem("zip_no_md", f"{name} 里没有 md 文件")
        raise _zip_problem("zip_multiple_md", f"{name} 里有 {n} 个 md 文件")
    return root, md


# --------------------------------------------------------------------------
# 本地 md 的图片短名字（ST3-P23 §3.3）
# --------------------------------------------------------------------------

SHORT_IMAGE_FMT = "image-{:02d}{}"
_EXT_ALIASES = {".jpeg": ".jpg"}


def short_ext(name):
    ext = Path(name).suffix.lower()
    return _EXT_ALIASES.get(ext, ext)


def assign_short_names(ok, old_map, next_no):
    """给 md 里引用到的图片分配短名字。

    ok: {原文写法: 源图片文件}（按 md 里第一次出现的顺序，见 check_text）；
    old_map: 上次的对照 {原文件名: 短名字}（保持不变）；next_no: 下一个可用编号（删过的编号不再复用）。
    返回 (新对照 {原文件名: 短名字}（只含这次引用到的）, {原文写法: 短名字}, 下一个可用编号)。
    """
    mapping, by_raw = {}, {}
    for raw, f in ok.items():
        orig = f.name
        if orig not in mapping:
            if orig in old_map:
                mapping[orig] = old_map[orig]
            else:
                mapping[orig] = SHORT_IMAGE_FMT.format(next_no, short_ext(orig))
                next_no += 1
        by_raw[raw] = mapping[orig]
    return mapping, by_raw, next_no


def rewrite_image_refs(text, by_raw):
    """把 md 里能用的图片引用改成 `图片和附件/<短名字>`；有问题的引用原样不动。"""
    def sub(m):
        raw = m.group(1).strip()
        if raw not in by_raw:
            return m.group(0)
        s, e = m.span(1)
        whole = m.group(0)
        off = m.start(0)
        return whole[:s - off] + f"{IMG_DIR}/{by_raw[raw]}" + whole[e - off:]
    return image_map.IMG_RE.sub(sub, text)


def warnings_of(inp_dir):
    """来源.json 的 `未处理块`（飞书视频、附件等）→ 提示列表（不阻断）。"""
    try:
        d = json.loads((Path(inp_dir) / "来源.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for b in d.get("未处理块") or []:
        out.append(problem("unhandled_block",
                           f"飞书未处理块：{b.get('类型')}（{b.get('说明') or ''}）",
                           block_id=b.get("block_id")))
    return out


# --------------------------------------------------------------------------
# 文件夹命名与导入锁
# --------------------------------------------------------------------------

_SEQ_RE = re.compile(r"^(\d{4})-")


def next_seq(work_root):
    """`work_root` 下现有 4 位数字序号的最大值 + 1，补零到 4 位（没有时为 0001）。"""
    top = 0
    root = Path(work_root)
    if root.is_dir():
        for p in root.iterdir():
            if p.name in NOT_ARTICLE_DIRS:   # 引擎自己的临时 / 解压目录，不是文章文件夹（v5.6、v5.8）
                continue
            m = _SEQ_RE.match(p.name)
            if m and p.is_dir():
                top = max(top, int(m.group(1)))
    return f"{top + 1:04d}"


def pick_folder(work_root, seq, short_title):
    """`<序号>-<短标题>`；已被别的文章占用时依次加 -2、-3……"""
    base = f"{seq}-{short_title}"
    cand, n = base, 1
    while (Path(work_root) / cand).exists():
        n += 1
        cand = f"{base}-{n}"
    return Path(work_root) / cand


@contextlib.contextmanager
def import_lock(work_root):
    """同一 `--work-root` 下的导入串行化（`<work-root>/.import.lock`，进程退出锁自动释放）。"""
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    f = open(root / IMPORT_LOCK, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            import time
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.5)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def apply_content_type(src_doc, content_type):
    """`--content-type` 非 auto 时写进来源.json 的 `内容类型`；返回是否有变化。auto 不动已有值。"""
    if not content_type or content_type == "auto":
        return False
    label = CONTENT_TYPE_LABELS[content_type]
    if src_doc.get("内容类型") == label:
        return False
    src_doc["内容类型"] = label
    return True


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------


def run_import(src, work_root, *, refetch=False, content_type="auto",
               title_chars=DEFAULT_TITLE_CHARS, list_csv=None):
    """导入（或复用）一篇文章并做输入检查。返回 dict：

    article / work_dir / source_type / title / reused / changed / image_count /
    warnings / problems（阻断问题，空表示可以开跑）。
    `src`：飞书链接，或本地的文件夹 / `.zip` / 单个 `.md` 文件（v5.8）。
    导入本身失败抛 feishu_read 的错误或 InputProblems（此时还没建文件夹）。
    """
    from pipeline.engine import feishu_read
    work_root = Path(work_root)
    with import_lock(work_root):
        if src.startswith(("http://", "https://")):
            info = feishu_read.import_feishu(src, work_root, list_csv=list_csv, refetch=refetch,
                                             content_type=content_type, title_chars=title_chars)
        else:
            info = feishu_read.import_local_md(src, work_root, refetch=refetch,
                                               content_type=content_type, title_chars=title_chars)
    root = work_root / info["article"]
    inp = root / INPUT_DIR
    if "problems" in info:
        # 本地 md：已在导入时对源文件夹里的原文写法检查过（ST3-P23 §3.3）
        problems, n_img = info["problems"], info["image_count"]
    else:
        problems, n_img = check_article(inp)
    info.update(work_dir=str(root.resolve()), image_count=n_img,
                warnings=warnings_of(inp) + info.pop("warnings", []), problems=problems)
    return info
