# -*- coding: utf-8 -*-
"""每篇文章的隔离渲染工程（任务书 §C1）。

结构（`<文章>\\09-成片\\render\\`）：

  src\\            从引擎根 `R57-render-test\\src` **复制**（读取引擎根，不写引擎根）；
  package.json    \\ 复制
  tsconfig.json   /
  node_modules\\   目录联接 -> `_runtime\\node_modules`（运行时副本，见 paths.py）。
                   打包缓存因此落在副本内 **`_runtime\\node_modules\\.cache`**，
                   引擎根 `R57-render-test\\node_modules` 不再被写。
  public\\         **真实目录**，只放本篇素材（清单由调用方从场景数据的 staticFile
                    引用推出，见 steps._scene_assets）：
                    音频与插画 -> 硬链接（建不了就复制）；字体 -> 复制。
                    这里绝不放联接、也绝不去主仓共享 `public\\` 兜底。

安全约定：
  - 本模块**只读**主仓（src / package.json / tsconfig.json / fonts）；
  - 不做任何递归删除。删联接只允许 `rmdir`（本模块不删，工程保留）；
  - 已存在的 `node_modules` 联接原样沿用（运行时依赖，不重建）；
  - ST3-P14：`public\\` 里的素材**按内容**核对——内容相同才沿用，不同就替换（先写临时文件再改名），
    不按"文件已存在"沿用（59 篇成片装进旧配音就是这么来的）。本次清单没用到的旧文件不删，只登记。
"""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from pipeline.engine import paths

RENDER_ENTRY = "src/index-r125.ts"
COMPOSITION = "R125-字幕调整"
CONCURRENCY = 4


def _make_junction(link, target):
    """建目录联接；已存在则原样沿用（不删除、不重建）。返回可用性说明。"""
    if link.exists():
        ok = (link / ".bin").exists()
        return f"沿用已存在: {link}"
    target = Path(target)
    if not target.is_dir():
        raise RuntimeError(f"联接目标不是目录: {target}")
    if os.name != "nt":
        # ST3-P9：非 Windows（Mac 自查渲染）没有目录联接，改用符号链接；Windows 走下面原路径。
        os.symlink(target, link, target_is_directory=True)
        if not (link / ".bin").exists():
            raise RuntimeError(f"符号链接不可用: {link} -> {target}")
        return f"新建（符号链接）: {link} -> {target}"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not (link / ".bin").exists():
        raise RuntimeError(
            f"目录联接不可用: rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
    return f"新建: {link} -> {target}"


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def same_content(a, b):
    """两个文件内容是否相同：大小不同直接判不同，否则比 sha256。

    不比路径、不比 inode：目标是硬链接时，源文件被重新生成后已是另一个文件，
    硬链接仍指向旧内容（ST3-P14）。
    """
    a, b = Path(a), Path(b)
    if a.stat().st_size != b.stat().st_size:
        return False
    return sha256_file(a) == sha256_file(b)


def _place(src, dst, allow_link):
    """把 `src` 放到 `dst`（`dst` 不能已存在）：硬链接优先，失败退回复制。返回 'hardlink' / 'copy'。"""
    if allow_link:
        try:
            os.link(src, dst)
            return "hardlink"
        except OSError:
            pass
    shutil.copy2(src, dst)
    return "copy"


def _link_or_copy(src, dst, allow_link=True):
    """按内容把 `src` 落到 `dst`。返回 (动作, 方式)：

    - ('new', 'hardlink'/'copy')：目标不存在，新建；
    - ('same', None)：目标已存在且内容相同，沿用；
    - ('replace', 'hardlink'/'copy')：目标已存在但内容不同，替换——先在同目录写临时文件，
      再 `os.replace` 改名覆盖，不留半成品。

    `dst` 可含子目录（截图素材走 `shots/<子目录>/<文件名>`），父目录按需新建。
    """
    src, dst = Path(src), Path(dst)
    if dst.exists():
        if not dst.is_file():
            raise RuntimeError(f"渲染素材目标已存在且不是文件: {dst}")
        if same_content(src, dst):
            return "same", None
        tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
        if tmp.exists():
            tmp.unlink()
        try:
            mode = _place(src, tmp, allow_link)
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()
        return "replace", mode
    dst.parent.mkdir(parents=True, exist_ok=True)
    return "new", _place(src, dst, allow_link)


def _unused_files(pub, used):
    """`public\\` 下本次清单没用到的文件（相对路径，`/` 分隔，排序）。只登记，不删除。"""
    out = []
    for p in pub.rglob("*"):
        if p.is_file():
            rel = p.relative_to(pub).as_posix()
            if rel not in used:
                out.append(rel)
    return sorted(out)


def verify(rp, files):
    """出片前核对（ST3-P14 第 3.1 条）：`public\\` 里每个本次要用的素材，内容必须和源文件一致。

    `files`：`prepare()` 返回的 `files`（每项带 `src` 绝对路径与 `name`）。
    返回不一致的清单 [{name, source, src_sha256, dst_sha256}]；一致返回空列表。
    """
    pub = Path(rp) / "public"
    bad = []
    for f in files:
        dst = pub / f["name"]
        src_sha = sha256_file(f["src"])
        dst_sha = sha256_file(dst) if dst.is_file() else "<缺>"
        if src_sha != dst_sha:
            bad.append({"name": f["name"], "source": f["source"],
                        "src_sha256": src_sha, "dst_sha256": dst_sha})
    return bad


def _ensure_real_dir(pub):
    """`public\\` 必须是**真实目录**。

    ST1-P2b A：ST1-P2 曾经把 `public\\` 当成共享素材箱的入口，本轮起只放本篇素材。
    若这里已是联接/符号链接，`mkdir(exist_ok=True)` 会静默成功，随后写进去的字就会落到
    主仓共享 `public\\` 上——所以先拦住，绝不沿用一个指向别处的 `public\\`。
    """
    if pub.exists() or pub.is_symlink():
        if pub.is_symlink() or pub.is_junction():
            raise RuntimeError(f"public 是联接/符号链接，拒绝写入（应为真实目录）: {pub}")
        if not pub.is_dir():
            raise RuntimeError(f"public 已存在且不是目录: {pub}")
    pub.mkdir(parents=True, exist_ok=True)


def prepare(ctx, rp, assets):
    """建（或复用）隔离渲染工程。

    `assets`：[(源相对路径, public 下的相对路径)]，源相对 `ctx.root`（`/` 分隔）。
    目标路径取自场景数据的 staticFile 引用原样（截图 = `shots/<子目录>/<文件名>`，
    插画 = `<子目录>/<文件名>`，音频 = 文件名），所以可以带子目录。
    素材清单由调用方**从场景数据推出来**（见 `steps._scene_assets`），本模块不再自己 glob。

    返回 {"render_project","junction","new","same","replace","hardlink","copy","replaced",
    "unused","files"}：`new`/`same`/`replace` 是新建 / 沿用（内容相同）/ 替换（内容不同）的个数，
    `hardlink`/`copy` 是新建与替换里各用了哪种方式；`replaced` 是被替换的文件名；
    `unused` 是 `public\\` 里本次没用到的旧文件（不删）；`files` 每一项记
    {name, source, src, action, mode}，供回执逐一列出、供 `verify()` 出片前核对。
    """
    rp = Path(rp)
    rp.mkdir(parents=True, exist_ok=True)
    real = Path(paths.RENDER_MAIN)

    shutil.copytree(real / "src", rp / "src", dirs_exist_ok=True)
    for f in ("package.json", "tsconfig.json"):
        shutil.copy2(real / f, rp / f)

    junction = _make_junction(rp / "node_modules", ctx.runtime_node_modules)

    pub = rp / "public"
    _ensure_real_dir(pub)
    # 字体可选：本仓库用系统字体，运行时没有字体文件时直接跳过。
    font_src = Path(ctx.runtime_node_modules).parent / paths.FONT_REL
    todo = [(Path(ctx.root) / rel, name, rel, True) for rel, name in assets]
    if font_src.is_file():
        todo.append((font_src, paths.FONT_REL, f"运行时 {paths.FONT_REL}", False))

    counts = {"new": 0, "same": 0, "replace": 0, "hardlink": 0, "copy": 0}
    files, replaced = [], []
    for src, name, source, allow_link in todo:
        action, mode = _link_or_copy(src, pub / name, allow_link=allow_link)
        counts[action] += 1
        if mode:
            counts[mode] += 1
        if action == "replace":
            replaced.append(name)
        files.append({"name": name, "source": source, "src": str(src), "action": action, "mode": mode})

    unused = _unused_files(pub, {f["name"] for f in files})
    return {"render_project": str(rp), "junction": junction, **counts,
            "replaced": replaced, "unused": unused, "files": files}
