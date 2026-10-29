#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
标题插画生成（轻量标题模板）

与内容插画（generate.py）的区别：标题插画约束信息密度——大标题 + 一行副标题 +
一个核心视觉元素，不画流程图和多卡片。

用法：
  py -3 title_generate.py --title "什么是X？" --subtitle "一句话定位" \
      --element "一个简洁的视觉元素" [--out-dir <目录>]
"""
import argparse
import time
import urllib.request
from pathlib import Path

import generate as g

DEFAULT_OUT_DIR = Path.cwd()


def build_title_prompt(title, subtitle, visual_element):
    """轻量标题插画模板：大标题 + 一行副标题 + 1 个核心视觉元素，禁止流程图/多卡片。"""
    return (
        f"一张用于视频章节标题的简洁扁平插画，非摄影质感。\n"
        f"画面背景为白色。\n"
        f"画面构图为极简标题卡：顶部居中一个大标题「{title}」，"
        f"标题下方最多一行小字副标题「{subtitle}」；"
        f"画面主体只用一个简洁的核心视觉元素：{visual_element}。\n"
        f"约束：不画流程图和分步骤的节点；画面里至多出现一个独立的信息模块或卡片，"
        f"整张图只承载标题这一个信息。\n"
        f"视觉处理：以深灰线条与色块为主，蓝色作为唯一强调色；保持留白，不使用渐变。\n"
        f"标注文字：只把标题和副标题用清晰文字画进图里，不要出现其他文字。"
    )


def generate_title(title, subtitle, visual_element, api_key, out_dir, name,
                   aspect_ratio="16:9", resolution="1k"):
    """拼 prompt + 调 RunningHub 文生图，下载结果，返回 (输出路径, usage)。"""
    prompt = build_title_prompt(title, subtitle, visual_element)
    url, usage = g.generate(prompt, api_key, aspect_ratio, resolution)
    out = Path(out_dir) / f"{name}-{int(time.time())}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, out)
    return str(out), usage, prompt


def main():
    parser = argparse.ArgumentParser(description="wz-sp-ctsc 标题插画生成（轻量标题模板）")
    parser.add_argument("--title", required=True, help="标题文字（画进图里的大标题）")
    parser.add_argument("--subtitle", required=True, help="一行副标题/定位语")
    parser.add_argument("--element", required=True, help="1 个核心视觉元素的描述")
    parser.add_argument("--aspect-ratio", default="16:9", help="宽高比（默认 16:9）")
    parser.add_argument("--resolution", default="1k", help="分辨率（默认 1k）")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="输出目录")
    parser.add_argument("--name", default="rh-ctsc-title", help="输出文件名前缀")
    args = parser.parse_args()

    prompt = build_title_prompt(args.title, args.subtitle, args.element)
    print("===== 最终 Prompt（喂给 API 的完整文本） =====")
    print(prompt)
    print("=" * 50)

    api_key = g.read_rh_key()
    out, usage, _ = generate_title(args.title, args.subtitle, args.element, api_key,
                                   args.out_dir, args.name, args.aspect_ratio, args.resolution)
    print(f"\n生成成功：{out}")
    if usage:
        print(f"计费：{usage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
