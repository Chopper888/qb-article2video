# -*- coding: utf-8 -*-
"""飞书读取模块（ST2-P1 A）：用飞书只读接口**实时**读取文档，转成引擎的 `00-输入`。

职责（任务书 §A）：
  - 输入一个飞书链接（`/wiki/<node_token>` 或 `/docx/<obj_token>`，带不带查询参数都支持），
    解析出文档 ID（obj_token），实时拉取全部块，按 23 种块类型规则转成 `文章.md`，
    图片全部下载到 `00-输入\\图片和附件\\`（**不允许缺图继续**）。
  - 输出 `00-输入\\文章.md` + `00-输入\\图片和附件\\` + `00-输入\\来源.json`。

凭据（任务书 §A.2）：**只读环境变量**，值不落盘、不打印：
  - `FEISHU_APP_ID`     —— 飞书应用 app_id（应用身份，tenant_access_token 用）
  - `FEISHU_APP_SECRET` —— 飞书应用 app_secret

应用身份（tenant_access_token）只用这两个变量，**不用用户 OAuth / refresh_token**。

错误约定（由 cli.py 映射到退出码）：
  - `FeishuArgumentError` —— 链接格式无效（退出码 4，reason=argument_error）
  - `FeishuConfigError`   —— 缺凭据 / 无权限 / 不是文档 / 网络失败（退出码 4，reason=config_error）
  - `FeishuImageError`    —— 图片下载失败，重试 1 次仍失败（退出码 2，message 写明哪张图）
三者都带 `kind`（error_kind，ST3-P22 §3.5）：退出码和 reason 不变，细分原因看 kind。

本模块**不读本地块缓存**，块一律实时从接口拉；只读，不写飞书。
"""
import csv
import datetime
import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pipeline.engine import errors, intake, netretry

# --------------------------------------------------------------------------
# 凭据（环境变量名）
# --------------------------------------------------------------------------

ENV_APP_ID = "FEISHU_APP_ID"
ENV_APP_SECRET = "FEISHU_APP_SECRET"

_BASE = "https://open.feishu.cn/open-apis"


class _KindError(Exception):
    kind = errors.KIND_INTERNAL
    # P31：失败来自哪家服务（事件 `service`）；本地 md 导入的错误抛出时传 local=True
    service = errors.SERVICE_FEISHU

    def __init__(self, message="", kind=None, missing_keys=None, local=False):
        super().__init__(message)
        if kind is not None:
            self.kind = kind
        self.missing_keys = missing_keys
        if local:
            self.service = None


class FeishuArgumentError(_KindError):
    """链接格式无效（退出码 4，reason=argument_error）。"""

    kind = errors.KIND_INPUT_INVALID
    service = None


class FeishuConfigError(_KindError):
    """配置类错误（退出码 4，reason=config_error）。"""

    kind = errors.KIND_SERVICE


class FeishuImageError(_KindError):
    """图片下载失败（退出码 2）。"""

    kind = errors.KIND_NETWORK


_PERM_WORDS = ("permission", "forbidden", "denied", "no access", "unauthorized", "权限")
_NOTFOUND_WORDS = ("not found", "not exist", "notexist", "不存在")


def _code_kind(msg):
    """飞书接口 code≠0 时按 msg 粗分：无权限 → key_invalid；文档不存在 → input_invalid；其他 → service_error。"""
    m = (msg or "").lower()
    if any(w in m for w in _PERM_WORDS):
        return errors.KIND_KEY_INVALID
    if any(w in m for w in _NOTFOUND_WORDS):
        return errors.KIND_INPUT_INVALID
    return errors.KIND_SERVICE


# --------------------------------------------------------------------------
# HTTP 小工具
# --------------------------------------------------------------------------


# 飞书明确返回的"限流"（请求频率超限）：按临时故障重试。
_RATE_LIMIT_CODES = (99991400, 99991401)


def _request(method, url, *, params=None, json_body=None, headers=None, timeout=40, tries=None):
    """带退避的请求。返回 data；非 0 code 抛 FeishuConfigError（限流 / 临时故障除外）。

    ST3-P26：临时故障（连接 / SSL / 读超时、HTTP 429 / 5xx、非 JSON 回复、限流 code）统一经 `netretry`
    退避重试，用完 → network。`tries` 是旧参数，不再使用（次数由 netretry 的等待上限决定）。
    """
    full = url
    if params:
        full = url + "?" + urllib.parse.urlencode(params, doseq=True)

    def _once():
        req = urllib.request.Request(full, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return "http", e.code
        try:
            return "ok", json.loads(raw)
        except Exception:
            return "nonjson", None

    def _transient(res):
        what, v = res
        if what == "http" and v in netretry.TRANSIENT_HTTP_IDEMPOTENT:
            return f"HTTP {v}"
        if what == "nonjson":
            return "回复不是 JSON"
        if what == "ok" and isinstance(v, dict) and v.get("code") in _RATE_LIMIT_CODES:
            return f"限流 code={v.get('code')}"
        return None

    try:
        what, v = netretry.call(_once, service="飞书", what="接口", retry_on=_transient)
    except Exception:
        # 网络失败重试用完（或请求本身抛了别的异常）：临时故障（退出码仍是 4，error_kind=network）
        raise FeishuConfigError("飞书接口重试耗尽", kind=errors.KIND_NETWORK)
    if _transient((what, v)):
        raise FeishuConfigError("飞书接口重试耗尽", kind=errors.KIND_NETWORK)
    if what == "http":
        kind = (errors.http_kind(v)
                or (errors.KIND_INPUT_INVALID if v == 404 else errors.KIND_SERVICE))
        raise FeishuConfigError(f"HTTP {v}", kind=kind)
    d = v
    code = d.get("code")
    if code == 0:
        return d.get("data", d)
    raise FeishuConfigError(f"飞书接口 code={code} msg={d.get('msg')}",
                            kind=_code_kind(d.get("msg")))


def _get(url, params=None, headers=None, **kw):
    return _request("GET", url, params=params, headers=headers, **kw)


def _post(url, json_body=None, headers=None, **kw):
    return _request("POST", url, json_body=json_body, headers=headers, **kw)


# --------------------------------------------------------------------------
# 凭据解析
# --------------------------------------------------------------------------


_tenant_token = None


def _tenant_access_token():
    """应用身份 tenant_access_token（内存缓存，不落盘、不打印）。"""
    global _tenant_token
    if _tenant_token:
        return _tenant_token
    app_id = os.environ.get(ENV_APP_ID)
    app_secret = os.environ.get(ENV_APP_SECRET)
    if not app_id or not app_secret:
        miss = [k for k, v in ((ENV_APP_ID, app_id), (ENV_APP_SECRET, app_secret)) if not v]
        raise FeishuConfigError(f"缺少凭据：{ENV_APP_ID} / {ENV_APP_SECRET}",
                                kind=errors.KIND_KEY_MISSING, missing_keys=miss)
    try:
        d = _post(f"{_BASE}/auth/v3/tenant_access_token/internal",
                  json_body={"app_id": app_id, "app_secret": app_secret})
    except FeishuConfigError as e:
        if e.kind != errors.KIND_NETWORK:
            e.kind = errors.KIND_KEY_INVALID   # 换 token 被拒 = 应用 ID / Secret 不对
        raise
    tok = d.get("tenant_access_token")
    if not tok:
        raise FeishuConfigError("获取 tenant_access_token 失败", kind=errors.KIND_KEY_INVALID)
    _tenant_token = tok
    return tok


def auth_headers():
    return {"Authorization": f"Bearer {_tenant_access_token()}"}


# --------------------------------------------------------------------------
# 链接解析与文档读取
# --------------------------------------------------------------------------

_WIKI_RE = re.compile(r"/wiki/([A-Za-z0-9]+)")
_DOCX_RE = re.compile(r"/docx/([A-Za-z0-9]+)")


def resolve_document(url):
    """解析链接 → (obj_token, 标题, 栏目路径)。"""
    url = (url or "").strip()
    m = _WIKI_RE.search(url)
    if m:
        node_token = m.group(1)
        d = _get(f"{_BASE}/wiki/v2/spaces/get_node",
                 params={"token": node_token}, headers=auth_headers())
        node = d.get("node") or {}
        obj_token = node.get("obj_token")
        if not obj_token:
            raise FeishuConfigError("无效链接：wiki 节点无文档", kind=errors.KIND_INPUT_INVALID)
        return obj_token, node.get("title") or "", ""
    m = _DOCX_RE.search(url)
    if m:
        obj_token = m.group(1)
        title, _ = _docx_meta(obj_token)
        return obj_token, title, ""
    raise FeishuArgumentError("无效链接：不是飞书 wiki 或 docx 链接")


def _docx_meta(obj_token):
    d = _get(f"{_BASE}/docx/v1/documents/{obj_token}/raw_content",
             headers=auth_headers())
    doc = d.get("document") or {}
    return doc.get("title") or "", ""


def fetch_blocks(obj_token):
    """实时拉取文档全部块（分页），返回扁平 list。"""
    blocks = []
    page_token = None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        d = _get(f"{_BASE}/docx/v1/documents/{obj_token}/blocks",
                 params=params, headers=auth_headers())
        blocks.extend(d.get("items") or [])
        if not d.get("has_more"):
            break
        page_token = d.get("page_token")
    return blocks


# --------------------------------------------------------------------------
# 块 → Markdown
# --------------------------------------------------------------------------

_HEADING = {3: "# ", 4: "## ", 5: "### ", 6: "#### ", 7: "##### "}

_TEXT_FIELDS = {
    2: "text", 3: "heading1", 4: "heading2", 5: "heading3",
    6: "heading4", 7: "heading5", 12: "bullet", 13: "ordered",
    15: "quote", 17: "todo",
}

_UNHANDLED = {33: "view", 23: "file", 30: "sheet"}

# R101 A：子块每深一层缩进 4 个空格（"- " 和 "1. " 下面都算同一列表项的续行）
_INDENT = "    "

_META_KEYS = ("block_id", "parent_id", "children", "block_type")

# 飞书 code block `style.language` 枚举（CodeLanguage）。只收录**本轮已核实**的值：
# 用 98 篇原始块数据实测：language=7 是 Bash（`npm install`/`export` 命令）、=46 是
# PowerShell（`$env:` 语法）、=28 是 JSON（`{...}`）。旧映射把 7 误标成 cpp（Bash 命令
# 被当成 C++ 代码卡，ST3-P3 A4），且 3~17 的猜测值对不上飞书真实枚举，故全部移除。
# 纯文本(1)与未收录语言一律当作「没有语言标识」（空串），由 elements 的命令前缀规则兜底。
_CODE_LANG = {
    1: "",            # PlainText 纯文本
    7: "bash",        # Bash（shell 命令）
    28: "json",       # JSON
    46: "powershell",  # PowerShell
}


def _extract_text(block):
    out = []
    for k, v in block.items():
        if k in _META_KEYS or not isinstance(v, dict):
            continue
        for el in (v.get("elements") or []):
            tr = el.get("text_run")
            if tr:
                out.append(tr.get("content", ""))
            md = el.get("mention_doc")
            if md:
                out.append(md.get("title", ""))
    return "".join(out)


def _code_language(blk):
    style = ((blk.get("code") or {}).get("style")) or {}
    lang = style.get("language")
    return _CODE_LANG.get(lang, "")


class _Renderer:
    """一次块渲染：产出 md 正文（不含标题 H1）、图片 token 顺序、未处理块清单。

    `tok_to_name` 给出时（{token: 文件名}），图片块输出 `![name](…)` 引用；否则只收集 token。
    """

    def __init__(self, blocks, tok_to_name=None):
        self.idx = {b["block_id"]: b for b in blocks}
        self.tok_to_name = tok_to_name or {}
        self.lines = []
        self.image_tokens = []
        self.unhandled = []
        self.depth = 0          # R101 A：列表等文字块的子块缩进层级
        self.seen = set()       # R101 A：已渲染的 block_id（同一块不渲染两次）

    def _children(self, blk):
        for cid in (blk.get("children") or []):
            self._block(cid)

    def _nested_children(self, blk):
        """R101 A：文字 / 列表块的子块（飞书里列表项下面的子文字、子图片、子列表）缩进一层输出。"""
        self.depth += 1
        try:
            self._children(blk)
        finally:
            self.depth -= 1

    def _emit(self, *lines):
        """按当前层级缩进输出（空行不缩进）。顶层（depth=0）与旧版逐字节相同。"""
        ind = _INDENT * self.depth
        self.lines += [(ind + ln) if ln else ln for ln in lines]

    def _block(self, block_id):
        blk = self.idx.get(block_id)
        if blk is None or block_id in self.seen:
            return
        self.seen.add(block_id)
        bt = blk.get("block_type")

        if bt == 1:  # page
            self._children(blk)
            return
        if bt in _HEADING:
            t = _extract_text(blk).strip()
            if t:
                self._emit(_HEADING[bt] + t, "")
            self._children(blk)   # R101 A：折叠标题的内容挂在标题块的 children 里
            return
        if bt in _TEXT_FIELDS:
            t = _extract_text(blk).strip()
            if t:
                if bt == 12:
                    self._emit("- " + t, "")
                elif bt == 13:
                    self._emit("1. " + t, "")
                elif bt == 17:
                    self._emit("- [ ] " + t, "")
                elif bt == 15:
                    self._emit("> " + t, "")
                else:
                    self._emit(t, "")
            self._nested_children(blk)
            return
        if bt == 14:  # code
            # 代码块不缩进：要素抽取（elements）按原样取代码正文，缩进会进到画面上的命令里
            self.lines += ["```" + _code_language(blk),
                           _extract_text(blk).rstrip("\n"), "```", ""]
            return
        if bt == 27:  # image
            tok = (blk.get("image") or {}).get("token")
            if tok:
                self.image_tokens.append(tok)
                name = self.tok_to_name.get(tok)
                if name:
                    q = urllib.parse.quote(name)
                    self._emit(f"![{name}](图片和附件/{q})", "")
            return
        if bt == 22:  # divider
            return
        if bt == 31:  # table
            self._table(blk)   # 表格同代码块，不缩进
            return
        if bt in (24, 25, 32):  # grid / grid_column / table_cell：透传 children
            self._children(blk)
            return
        if bt in (34, 19):  # quote_container / callout
            before = len(self.lines)
            self._children(blk)
            ind = _INDENT * self.depth
            for i in range(before, len(self.lines)):
                ln = self.lines[i]
                if ln.strip():
                    rest = ln[len(ind):] if ln.startswith(ind) else ln
                    self.lines[i] = ind + "> " + rest
            return
        if bt in _UNHANDLED:
            kind = _UNHANDLED[bt]
            self.unhandled.append({"类型": kind, "block_id": block_id,
                                   "说明": "视频/附件/表格等，本阶段不下载不展开"})
            self.lines += [f"<!-- 飞书未处理块：{kind} -->", ""]
            return
        # 其他未知类型
        self.unhandled.append({"类型": f"type_{bt}", "block_id": block_id,
                               "说明": "未知块类型"})

    def _table(self, blk):
        pr = (blk.get("table") or {}).get("property") or {}
        rows = pr.get("row_size") or 0
        cols = pr.get("column_size") or 0
        cells = blk.get("children") or []
        if rows <= 0 or cols <= 0:
            self.unhandled.append({"类型": "table", "block_id": blk.get("block_id"),
                                   "说明": "表格尺寸缺失"})
            return
        grid = [self._cell_text(self.idx.get(cid)) for cid in cells]
        total = rows * cols
        while len(grid) < total:
            grid.append("")
        grid = grid[:total]
        self.lines.append("| " + " | ".join(_md_cell(x) for x in grid[:cols]) + " |")
        self.lines.append("| " + " | ".join(["---"] * cols) + " |")
        for r in range(1, rows):
            row = grid[r * cols:(r + 1) * cols]
            self.lines.append("| " + " | ".join(_md_cell(x) for x in row) + " |")
        self.lines.append("")

    def _cell_text(self, cell_blk):
        if cell_blk is None:
            return ""
        buf = []
        self._collect_text(cell_blk, buf)
        return " ".join(x.strip() for x in buf if x.strip())

    def _collect_text(self, blk, buf):
        t = _extract_text(blk).strip()
        if t:
            buf.append(t)
        for cid in (blk.get("children") or []):
            cb = self.idx.get(cid)
            if cb is not None:
                self._collect_text(cb, buf)

    def render_body(self):
        root = next((b for b in self.idx.values() if b.get("block_type") == 1), None)
        if root is not None:
            self._children(root)
        else:
            for b in self.idx.values():
                if b.get("block_type") == 1 or b.get("parent_id") in self.idx:
                    continue   # R101 A：子块由父块递归输出
                self._block(b["block_id"])
        return "\n".join(self.lines).rstrip() + "\n"


def _md_cell(s):
    return s.replace("|", "\\|").replace("\n", " ").strip()


def blocks_to_markdown(blocks, tok_to_name=None):
    """块列表 → (md 正文[含图片引用，不含标题 H1], 图片 token 顺序, 未处理块清单)。

    `tok_to_name` 为 {token: 文件名}；缺省时图片块只收集 token、不输出引用。
    """
    r = _Renderer(blocks, tok_to_name=tok_to_name)
    return r.render_body(), r.image_tokens, r.unhandled


# --------------------------------------------------------------------------
# 图片下载
# --------------------------------------------------------------------------


def fetch_tmp_urls(tokens, obj_token):
    out = {}
    for i in range(0, len(tokens), 5):
        chunk = tokens[i:i + 5]
        params = [("file_tokens", t) for t in chunk]
        params.append(("extra", json.dumps({"docType": "docx", "docToken": obj_token})))
        d = _get(f"{_BASE}/drive/v1/medias/batch_get_tmp_download_url",
                 params=params, headers=auth_headers(), tries=5)
        for item in d.get("tmp_download_urls") or []:
            out[item.get("file_token")] = item.get("tmp_download_url")
    return out


def _detect_ext(data, ctype):
    ctype = (ctype or "").split(";")[0].lower()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if "jpeg" in ctype or "jpg" in ctype:
        return ".jpg"
    if "gif" in ctype:
        return ".gif"
    if "webp" in ctype:
        return ".webp"
    return ".png"


def download_images(tokens, obj_token, dest_dir):
    """下载全部原图，返回 {token: 文件名}。任一张失败（重试 1 次）即抛 FeishuImageError。"""
    urls = fetch_tmp_urls(tokens, obj_token)
    tok_to_name = {}
    for i, tk in enumerate(tokens, 1):
        if tk not in urls:
            raise FeishuImageError(f"图片下载失败：第 {i} 张取不到临时链接（token {tk}）",
                                   kind=errors.KIND_SERVICE)
        def _fetch(u=urls[tk]):
            req = urllib.request.Request(u, method="GET")
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read(), r.headers.get("Content-Type", "")

        # ST3-P26：临时故障、空回复按 netretry 退避重试；用完仍没拿到按下载失败处理。
        try:
            data, ctype = netretry.call(_fetch, service="飞书", what=f"下载第 {i} 张图",
                                        retry_on=lambda r: None if r[0] else "空回复")
        except Exception:
            data, ctype = None, ""
        if not data:
            raise FeishuImageError(f"图片下载失败：第 {i} 张（token {tk}）")
        name = f"image-{i:02d}{_detect_ext(data, ctype)}"
        (dest_dir / name).write_bytes(data)
        tok_to_name[tk] = name
        time.sleep(0.05)
    return tok_to_name


# --------------------------------------------------------------------------
# 文件夹命名与清单
# --------------------------------------------------------------------------

_ILLEGAL = {'\\': '＼', '/': '／', ':': '：', '*': '＊', '?': '？',
            '"': '＂', '<': '＜', '>': '＞', '|': '｜'}


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def _tidy(s):
    """Windows 文件夹名收尾：去掉首尾空白、行尾的点和空格。"""
    return s.strip().rstrip(". ")


def clean_title(t, limit=60):
    """A1.1：Windows 非法字符换全角（不删），长度截断到 `limit`。

    ST3-P22（G10）补全 Windows 规则：去掉控制字符和制表符；去掉行尾的点和空格（截断前后各做一次）；
    Windows 保留名（CON、NUL、COM1 …，不分大小写、带扩展名也算）前面加 `_`。
    """
    s = "".join(_ILLEGAL.get(c, c) for c in (t or "")
                if unicodedata.category(c) != "Cc")
    s = _tidy(s)
    if s.split(".")[0].strip().upper() in _RESERVED:
        s = "_" + s
    return _tidy(s[:limit])


def short_title(t, n):
    """文件夹名里的标题段：清洗后取前 n 个字符；清洗完为空时用「未命名」。"""
    return clean_title(t, limit=n) or "未命名"


def _load_list(list_csv):
    """读待转清单 v3，返回 ({文档ID/节点token: (序号,标题)}, {链接: (序号,标题)})。"""
    by_token, by_url = {}, {}
    if not list_csv or not Path(list_csv).is_file():
        return by_token, by_url
    with open(list_csv, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            seq = (r.get("序号") or "").strip()
            title = (r.get("标题") or "").strip()
            if not seq or not title:
                continue
            url = (r.get("链接") or "").strip()
            token = (r.get("文档ID") or r.get("obj_token") or "").strip()
            if token:
                by_token[token] = (seq, title)
            if url:
                by_url[url] = (seq, title)
                m = _WIKI_RE.search(url) or _DOCX_RE.search(url)
                if m:
                    by_token[m.group(1)] = (seq, title)
    return by_token, by_url


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def find_existing(work_root, obj_token):
    """在 work_root 下找「来源.json 中文档 ID 相同」的文件夹，返回 Path 或 None。"""
    root = Path(work_root)
    if not root.is_dir():
        return None
    for sub in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in intake.NOT_ARTICLE_DIRS):
        src = sub / "00-输入" / "来源.json"
        if not src.is_file():
            continue
        try:
            d = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (d.get("文档ID") or d.get("obj_token")) == obj_token:
            return sub
    return None


def _dir_snapshot(d):
    """{文件名: sha256}（只看这一层的文件）。"""
    d = Path(d)
    if not d.is_dir():
        return {}
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest()
            for f in d.iterdir() if f.is_file()}


def _file_sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def _read_src_doc(folder):
    p = Path(folder) / "00-输入" / "来源.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_src_doc(folder, doc):
    (Path(folder) / "00-输入" / "来源.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def import_feishu(url, work_root, *, list_csv=None, refetch=False, content_type="auto",
                  title_chars=intake.DEFAULT_TITLE_CHARS):
    """导入一篇飞书文章 → 返回 {article, source_type, title, reused, changed}。

    复用已有文件夹（按文档 ID）时不重新抓，`--refetch` 才在同一文件夹里重抓。
    新文件夹名 `<序号>-<标题前 title_chars 字>`（ST3-P22 §3.4）。调用方负责加导入锁。
    """
    work_root = Path(work_root)
    obj_token, title, path = resolve_document(url)

    existing = find_existing(work_root, obj_token)
    if existing is not None and not refetch:
        doc = _read_src_doc(existing)
        changed = intake.apply_content_type(doc, content_type)
        if changed:
            _write_src_doc(existing, doc)
        return {"article": existing.name, "source_type": "feishu",
                "title": doc.get("原标题") or title, "reused": True, "changed": changed}

    by_token, by_url = _load_list(list_csv)
    seq_title = by_token.get(obj_token) or by_url.get(url)
    clean = clean_title(title)

    if existing is not None:
        folder = existing
        seq = _read_src_doc(existing).get("序号") or (seq_title[0] if seq_title else "")
    else:
        seq = seq_title[0] if seq_title else intake.next_seq(work_root)
        folder = intake.pick_folder(work_root, seq, short_title(title, title_chars))
    inp = folder / "00-输入"
    img_dir = inp / "图片和附件"
    before = (_file_sha(inp / "文章.md"), _dir_snapshot(img_dir))
    old_doc = _read_src_doc(folder) if existing is not None else {}

    blocks = fetch_blocks(obj_token)
    folder.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)
    _, img_tokens, unhandled = blocks_to_markdown(blocks)
    tok_to_name = download_images(img_tokens, obj_token, img_dir) if img_tokens else {}
    md, _, _ = blocks_to_markdown(blocks, tok_to_name=tok_to_name)
    md = f"# {title}\n\n{md}"

    (inp / "文章.md").write_text(md, encoding="utf-8")
    doc = {
        "schema_version": 1,
        "来源": "飞书实时读取",
        "飞书链接": url,
        "文档ID": obj_token,
        "原标题": title,
        "清洗后标题": clean,
        "栏目路径": path,
        "读取时间": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "图片清单": tok_to_name,
        "未处理块": unhandled,
        "序号": seq,
        "文件夹名": folder.name,
    }
    if old_doc.get("内容类型"):
        doc["内容类型"] = old_doc["内容类型"]
    ct_changed = intake.apply_content_type(doc, content_type)
    _write_src_doc(folder, doc)
    after = (_file_sha(inp / "文章.md"), _dir_snapshot(img_dir))
    reused = existing is not None
    return {"article": folder.name, "source_type": "feishu", "title": title,
            "reused": reused, "changed": reused and (before != after or ct_changed)}


def _find_local(work_root, src_path):
    """按「本地路径」找已有文件夹（ST3-P22 §3.6：复用键不再含 md 的 sha256）。

    旧版 `--refetch` 留下的多个同源文件夹，取名字排序最前（最老）的那个。
    """
    root = Path(work_root)
    if not root.is_dir():
        return None
    for sub in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in intake.NOT_ARTICLE_DIRS):
        src = sub / "00-输入" / "来源.json"
        if not src.is_file():
            continue
        try:
            d = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("本地路径") == src_path:
            return sub
    return None


def _sync_images(files, img_dir, force=False):
    """把这次引用到的图片按短名字同步进 `00-输入/图片和附件/`（ST3-P23 §3.3）。

    files: {短名字: 源文件}。新增和改动的复制进来；不在清单里的（源里删掉的、没被引用的、
    旧版按原文件名复制进来的）删掉。只看这一层的文件。返回是否有变化。
    """
    img_dir.mkdir(parents=True, exist_ok=True)
    have = _dir_snapshot(img_dir)
    changed = False
    for name, f in files.items():
        data = Path(f).read_bytes()
        differs = have.get(name) != hashlib.sha256(data).hexdigest()
        changed = changed or differs
        if differs or force:
            (img_dir / name).write_bytes(data)
    for name in have:
        if name not in files:
            (img_dir / name).unlink()
            changed = True
    return changed


def import_local_md(src_dir, work_root, *, refetch=False, content_type="auto",
                    title_chars=intake.DEFAULT_TITLE_CHARS):
    """把本地 md 导入成引擎工作文件夹（任务书 B.5；ST3-P22 §3.3、§3.4、§3.6；APP4 §1.2～§1.4）。

    `src_dir` 可以是（v5.8）：
      - 文件夹：含 `文章.md`（或唯一一个 `*.md`）；
      - `.zip`：解压到 `<work-root>/_a2v_import/<编号>/`（只有一层外包文件夹时进入那一层），再按文件夹导入；
      - 单个 `.md` 文件：它所在的文件夹是文章文件夹，只用这一个 md。
    图片可以在文章文件夹里任意位置（v5.8；路径找不到时用唯一的同名文件兜底，见 intake.check_text）。
    md 必须是 UTF-8（BOM 去掉），不是就抛 InputProblems，不建文件夹；zip 本身有问题也抛 InputProblems。
    ST3-P23 §3.3：只复制 md 里引用到的图片，改名为 `image-01.<小写扩展名>`…（按第一次引用的顺序，
    同一文件只占一个编号，jpeg 记为 jpg），`00-输入/文章.md` 里的引用跟着改；对照写进
    `来源.json` 的 `图片改名`（已有对照不变，删掉的编号不再复用，见 `图片编号下一个`、`图片改名_已删除`）。
    v5.8：新导入的标题行去掉 Markdown 转义（`来源.json` 记 `导入规则: 2`）；旧规则导入的文章
    （没有这个字段）再导入时仍按旧规则，内容没变就 changed=false，`--refetch` 才升到新规则。
    复用键 = 输入的绝对路径（文件夹 / zip / md 文件）：同一路径再导入就复用原文件夹，把新的 md 和图片
    同步进 `00-输入/`（`--refetch` = 强制重新同步）。返回 {article, source_type, title, reused, changed,
    problems, image_count, warnings}。调用方负责加导入锁。
    """
    work_root = Path(work_root)
    src = Path(src_dir)
    extra_doc = {}
    if src.is_file() and src.suffix.lower() == ".zip":
        origin = "本地 zip"
        art_dir, md_file = intake.extract_zip(src, work_root)
        extra_doc["解压位置"] = str(art_dir.resolve())
    elif src.is_file() and src.suffix.lower() == ".md":
        origin = "本地 md 文件"
        art_dir, md_file = src.parent, src
    elif src.is_dir():
        origin = "本地 md 文件夹"
        art_dir = src
        md_file, _ = intake.pick_md(src)
        if md_file is None:
            raise FeishuConfigError("本地 md 文件夹需含唯一的 文章.md",
                                    kind=errors.KIND_INPUT_INVALID, local=True)
    elif src.exists():
        raise FeishuConfigError(f"只能导入文件夹、.zip 或 .md 文件：{src}",
                                kind=errors.KIND_INPUT_INVALID, local=True)
    else:
        raise FeishuConfigError(f"本地 md 文件夹不存在：{src}", kind=errors.KIND_INPUT_INVALID, local=True)
    md_bytes = md_file.read_bytes()
    text = intake.decode_md(md_bytes, md_file.name)        # 编码检查在建文件夹之前
    md_sha = hashlib.sha256(md_bytes).hexdigest()
    src_path = str(src.resolve())

    existing = _find_local(work_root, src_path)
    doc = _read_src_doc(existing) if existing is not None else {}
    new_rules = existing is None or refetch or int(doc.get("导入规则") or 1) >= intake.IMPORT_RULES
    if new_rules:
        text_in = intake.unescape_headings(text)
        title = intake.md_title(text_in, src.stem if src.is_file() else src.resolve().name)
    else:
        text_in = text
        title = intake.md_title(text, src_path.split("\\")[-1].split("/")[-1])
    # ST3-P23 §3.3：检查对源文件夹里的原文写法做（问题里是用户认得出的原文件名）。
    problems, ok, warns = intake.check_text(text_in, md_file, art_dir)
    folder = existing if existing is not None else intake.pick_folder(
        work_root, intake.next_seq(work_root), short_title(title, title_chars))
    inp = folder / "00-输入"
    img_dir = inp / "图片和附件"
    inp.mkdir(parents=True, exist_ok=True)

    old_map = doc.get("图片改名") or {}
    next_no = int(doc.get("图片编号下一个") or 1)
    mapping, by_raw, next_no = intake.assign_short_names(ok, old_map, next_no)
    md_out = intake.rewrite_image_refs(text_in, by_raw).encode("utf-8")

    md_p = inp / "文章.md"
    md_changed = (not md_p.is_file()) or md_p.read_bytes() != md_out
    if md_changed or refetch:
        md_p.write_bytes(md_out)
    src_of = {}
    for raw, f in ok.items():
        src_of.setdefault(by_raw[raw], f)
    img_changed = _sync_images(src_of, img_dir, force=refetch)

    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if existing is None:
        doc = {
            "schema_version": 1,
            "来源": origin,
            "本地路径": src_path,
            "md_sha256": md_sha,
            "原标题": title,
            "清洗后标题": clean_title(title),
            "读取时间": now,
            "图片清单": {},
            "未处理块": [],
            "文件夹名": folder.name,
            "导入规则": intake.IMPORT_RULES,
            **extra_doc,
        }
    removed = dict(doc.get("图片改名_已删除") or {})
    removed.update({k: v for k, v in old_map.items() if k not in mapping})
    map_changed = (mapping != old_map or next_no != int(doc.get("图片编号下一个") or 1)
                   or removed != (doc.get("图片改名_已删除") or {}))
    doc["图片改名"] = mapping
    doc["图片编号下一个"] = next_no
    if removed:
        doc["图片改名_已删除"] = removed
    rules_changed = new_rules and doc.get("导入规则") != intake.IMPORT_RULES
    if rules_changed:
        doc["导入规则"] = intake.IMPORT_RULES
    extra_changed = any(doc.get(k) != v for k, v in extra_doc.items())
    doc.update(extra_doc)
    ct_changed = intake.apply_content_type(doc, content_type)
    changed = existing is not None and (md_changed or img_changed or ct_changed)
    if (existing is None or changed or refetch or map_changed or rules_changed or extra_changed
            or doc.get("md_sha256") != md_sha):
        if existing is not None:
            doc.update({"md_sha256": md_sha, "原标题": title, "清洗后标题": clean_title(title),
                        "同步时间": now})
        _write_src_doc(folder, doc)
    return {"article": folder.name, "source_type": "markdown", "title": intake.unescape_md(title),
            "reused": existing is not None, "changed": changed,
            "problems": problems, "image_count": len(src_of), "warnings": warns}
