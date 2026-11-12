# -*- coding: utf-8 -*-
"""render_mux：主线出片两步封装（SHOT-M4-P4 B4 收成）。

Remotion 直接渲染 mp4 时 AAC 编码器引入约 42.7ms priming 且不写音频轨 edit list，
导致音画偏移约 1.28 帧。两步法消除该偏移：
  1) `<node> <remotion-cli.js> render ... --separate-audio-to <wav>`：导出无 AAC 延迟的 WAV；
  2) `ffmpeg -i video -i wav -map 0:v -map 1:a -c:v copy -c:a aac`：重封装成 AAC（ffmpeg
     会写正确的 edit list，解码后音画对齐）。

参数全部由调用方传入，本模块不写死任何路径。

R120M2 Q1：不再经 shell 调裸命令 `npx` / `ffmpeg`（没装 Node.js 的电脑上 Windows 报
"'npx' 不是内部或外部命令"）。node 与 ffmpeg 都用绝对路径（默认由 `pipeline.engine.paths`
定位运行包自带版本），直接执行 Remotion CLI 的 JS 入口，不依赖 PATH。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys


def _run(cmd, cwd=None):
    print("  $", " ".join(str(c) for c in cmd))
    # R120M2：第一个元素是绝对路径的可执行文件，一律按参数列表直接执行、不经 shell
    # （原来 Windows 为了找到 npx.cmd 走 shell=True，现在用不着了）。
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def render_command(node, cli_js, entry, composition, video_tmp, audio_tmp, props=None,
                   concurrency=None, extra_args=None):
    """组装步骤 1 的命令：[node 绝对路径, remotion-cli.js 绝对路径, "render", ...]。"""
    cmd = [str(node), str(cli_js), "render", entry, composition, video_tmp,
           "--separate-audio-to", audio_tmp]
    if props is not None:
        cmd += ["--props", json.dumps(props)]
    if concurrency is not None:
        cmd += ["--concurrency", str(concurrency)]
    if extra_args:
        cmd += extra_args
    return cmd


def _tools(cwd, node=None, ffmpeg=None):
    """定位 node / remotion-cli.js / ffmpeg（绝对路径），并打印用的是哪一个。"""
    from pipeline.engine import paths
    node_src = ffmpeg_src = "调用方指定"
    if node is None:
        node, node_src = paths.find_node()
    if ffmpeg is None:
        ffmpeg, ffmpeg_src = paths.find_ffmpeg_tool("ffmpeg")
    cli_js = paths.remotion_cli_js(cwd)
    print(f"[render_mux] node: {node}（{node_src}）")
    print(f"[render_mux] ffmpeg: {ffmpeg}（{ffmpeg_src}）")
    return node, cli_js, ffmpeg


def render_mux(entry, composition, output_mp4, cwd, props=None, concurrency=None,
               extra_args=None, keep_temp=False, node=None, ffmpeg=None):
    """两步渲染 + 重封装。返回 output_mp4 路径。

    entry: remotion 入口文件（如 src/index-r125.ts）。
    composition: 合成 id。
    output_mp4: 最终 mp4 绝对路径。
    cwd: 渲染工程目录（node/ffmpeg 在此目录执行；其下 node_modules 里有 @remotion/cli）。
    props: remotion input props（dict，序列化为 JSON）。
    concurrency: 渲染并发数。
    extra_args: 额外传给 remotion render 的参数列表（可选）。
    keep_temp: True 时保留中间 video/wav 临时文件。
    node / ffmpeg: 可执行文件绝对路径；不给时用运行包自带版本（见 pipeline.engine.paths）。
    """
    video_tmp = os.path.splitext(output_mp4)[0] + ".video.tmp.mp4"
    audio_tmp = os.path.splitext(output_mp4)[0] + ".audio.tmp.wav"

    node, cli_js, ffmpeg = _tools(cwd, node, ffmpeg)
    cmd = render_command(node, cli_js, entry, composition, video_tmp, audio_tmp,
                         props=props, concurrency=concurrency, extra_args=extra_args)

    print("[render_mux] 步骤1: 渲染视频 + 导出无延迟 WAV")
    _run(cmd, cwd=cwd)

    print("[render_mux] 步骤2: ffmpeg 重封装 AAC")
    _run([ffmpeg, "-y", "-v", "error", "-i", video_tmp, "-i", audio_tmp,
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
