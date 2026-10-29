# -*- coding: utf-8 -*-
"""render_mux：主线出片两步封装（SHOT-M4-P4 B4 收成）。

Remotion 直接渲染 mp4 时 AAC 编码器引入约 42.7ms priming 且不写音频轨 edit list，
导致音画偏移约 1.28 帧。两步法消除该偏移：
  1) `npx remotion render ... --separate-audio-to <wav>`：导出无 AAC 延迟的 WAV；
  2) `ffmpeg -i video -i wav -map 0:v -map 1:a -c:v copy -c:a aac`：重封装成 AAC（ffmpeg
     会写正确的 edit list，解码后音画对齐）。

参数全部由调用方传入，本模块不写死任何路径。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys


def _run(cmd, cwd=None):
    print("  $", " ".join(str(c) for c in cmd))
    # Windows 下 npx 是 npx.cmd，必须走 shell=True 才能被 CreateProcess 找到；
    # 参数用 list2cmdline 做 Windows 风格引用（只给含空格的参数加双引号，中文 id 不引），
    # 否则含空格路径会被 shell 打散、含中文 id 会被误加引号（ST1-P4b 首篇新文章）。
    if os.name != "nt":
        # ST3-P9：非 Windows（Mac 自查渲染）直接按参数列表执行，不经 shell；Windows 走下面原路径。
        subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)
        return
    subprocess.run(subprocess.list2cmdline([str(c) for c in cmd]), cwd=cwd, check=True, shell=True)


def render_mux(entry, composition, output_mp4, cwd, props=None, concurrency=None,
               extra_args=None, keep_temp=False):
    """两步渲染 + 重封装。返回 output_mp4 路径。

    entry: remotion 入口文件（如 src/index-r125.ts）。
    composition: 合成 id。
    output_mp4: 最终 mp4 绝对路径。
    cwd: 渲染工程目录（npx/ffmpeg 在此目录执行）。
    props: remotion input props（dict，序列化为 JSON）。
    concurrency: 渲染并发数。
    extra_args: 额外传给 remotion render 的参数列表（可选）。
    keep_temp: True 时保留中间 video/wav 临时文件。
    """
    video_tmp = os.path.splitext(output_mp4)[0] + ".video.tmp.mp4"
    audio_tmp = os.path.splitext(output_mp4)[0] + ".audio.tmp.wav"

    cmd = ["npx", "remotion", "render", entry, composition, video_tmp,
           "--separate-audio-to", audio_tmp]
    if props is not None:
        cmd += ["--props", json.dumps(props)]
    if concurrency is not None:
        cmd += ["--concurrency", str(concurrency)]
    if extra_args:
        cmd += extra_args

    print("[render_mux] 步骤1: 渲染视频 + 导出无延迟 WAV")
    _run(cmd, cwd=cwd)

    print("[render_mux] 步骤2: ffmpeg 重封装 AAC")
    _run(["ffmpeg", "-y", "-v", "error", "-i", video_tmp, "-i", audio_tmp,
          "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", output_mp4])

    if not keep_temp:
        for p in (video_tmp, audio_tmp):
            if os.path.exists(p):
                os.remove(p)

    print(f"[render_mux] 完成 -> {output_mp4}")
    return output_mp4


def main():
    ap = argparse.ArgumentParser(description="主线出片两步封装（消除 AAC priming 偏移）")
    ap.add_argument("entry", help="remotion 入口文件")
    ap.add_argument("composition", help="合成 id")
    ap.add_argument("output", help="最终 mp4 路径")
    ap.add_argument("--cwd", default=".", help="渲染工程目录")
    ap.add_argument("--props", default=None, help="JSON props")
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--keep-temp", action="store_true")
    args = ap.parse_args()

    props = json.loads(args.props) if args.props else None
    render_mux(args.entry, args.composition, args.output, args.cwd,
               props=props, concurrency=args.concurrency, keep_temp=args.keep_temp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
