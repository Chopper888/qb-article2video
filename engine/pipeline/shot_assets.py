# -*- coding: utf-8 -*-
"""shot_assets：截图原图复制进 public\\shots\\<subdir>（SHOT-M4-P3 C 步收成）。

copy_shot_images 把原图复制到渲染工程的 public 目录，返回「原图文件名 → staticFile 引用路径」。
规则：目标已存在且 sha256 相同则跳过；存在但内容不同则报错；原图不存在则报错。
函数不写死任何路径，全部由调用方传入。
"""
import hashlib
import os
import shutil


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_shot_images(image_paths, public_dir, subdir):
    """把 image_paths 里的原图复制到 public_dir/shots/<subdir>/<原文件名>。

    返回 {原图文件名: staticFile 引用路径}。staticFile 引用路径不含 public/ 前缀。
    """
    dest_dir = os.path.join(public_dir, "shots", subdir)
    os.makedirs(dest_dir, exist_ok=True)

    result = {}
    for src in image_paths:
        if not os.path.exists(src):
            raise FileNotFoundError(f"原图不存在: {src}")
        fn = os.path.basename(src)
        dest = os.path.join(dest_dir, fn)
        src_sha = _sha256(src)
        if os.path.exists(dest):
            if _sha256(dest) != src_sha:
                raise RuntimeError(f"目标已存在但内容不同（sha256 不一致）: {fn}")
            # 相同则跳过
        else:
            shutil.copyfile(src, dest)
        result[fn] = f"shots/{subdir}/{fn}"
    return result
