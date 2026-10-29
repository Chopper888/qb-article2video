#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
插画生成

按类型拼装最终 prompt（自然语言描述），调用 RunningHub GPT Image 2 文生图，下载结果。

用法：
  py -3 generate.py --type illustration --subject "..." [--text "..."] [--out-dir <目录>] [--aspect-ratio 16:9]
"""
import argparse
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

RH_BASE = "https://www.runninghub.ai"
RH_KEY_ENV = "RUNNINGHUB_API_KEY"
DEFAULT_OUT_DIR = Path.cwd()
DEFAULT_ASPECT = "16:9"
DEFAULT_RESOLUTION = "1k"
POLL_SECONDS = 3
MAX_POLLS = 60
# M3: 轮询超时后复用同一 taskId 重新轮询的次数（不重新提交，省费用）。
# R160 实测：G61/G91 首跑 TimeoutError 是网络抖动所致，任务在服务端其实已成功，
# 复用 taskId 再轮询即可拿到结果，无需重新提交浪费一张图的钱。
POLL_RETRIES = 2

# 生成请求失败自动重试（S1-R102）：网络/SSL/超时等瞬时错误、HTTP 5xx、429 限流 → 重试；
# 4xx（prompt 内容审核拒绝、参数/鉴权错误）重试无意义 → 不重试。
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = [1, 3, 5]

# Type → 用途/场景定位句（与 prompt-construction.md §3 一致）
TYPE_OPENING = {
    "illustration": "这是一张用于知识科普视频的内容插画",
    "title": "这是一张用于知识科普视频的标题场景插画",
    "generic": "这是一张用于知识科普视频的插画",
}


def skill_base() -> Path:
    return Path(__file__).resolve().parent.parent


# ---------------- RunningHub key 解析（ST1-P5b 乙：只读环境变量，删文件兜底） ----------------
def read_rh_key(api_key=None):
    v = api_key or os.environ.get(RH_KEY_ENV)
    if v and v.strip():
        return v.strip()
    raise RuntimeError("RunningHub API key 未找到：请设 RUNNINGHUB_API_KEY")


# ---------------- 画面主体 + 标注文字提炼 ----------------
def extract_subject_and_labels(text, type_name, api_key=None):
    """提炼画面主体 + 标注文字清单（摘取自原文，不编造）。

    返回 (subject, labels)。labels 是从原文实际出现的关键词/数字/术语，
    用于画进图里的标注文字，不引申、不添加原文没有的结论。
    """
    import type_match as tm
    api_key = tm.resolve_api_key(api_key)
    system = (
        "你是一个插画画面主体提炼器。给定一段口播内容和它的插画类型，输出两样东西，严格 JSON："
        '{"subject":"可以用一幅简洁插画画出来的具体画面主体（一句中文，不含文字/数字/标签）",'
        '"labels":["从原文中摘取、要画进图里的关键词/数字/术语"]}。'
        "labels 必须来自原文真实出现的词，不能引申，也不能添加原文没有的结论。"
        "subject 要具体、可画：优先画内容点名的事物本身；内容抽象时，用一个真实的物件或场景去承载，"
        "不要用「抽象画面」「概念图」这类没有信息量的占位描述，也不要用「放大镜观察某概念」来代替真正的画面。"
        "原文里如果有多个对象或角色，注意保持它们之间的对应关系，标注不要张冠李戴。"
        "如果这段内容没有任何可以画出来的对象或话题，返回 {\"subject\": null, \"labels\": []} 表示放弃出图。"
        "只输出 JSON，不要任何解释。"
    )
    user = f"插画类型：{type_name}\n\n内容：{text}\n\n输出 JSON："
    raw = tm.call_deepseek(api_key, system, user)
    r = tm.extract_json(raw)
    # F1: 弃权——模型返回 subject=null 表示「没有可画的具体内容」，上层据此不出图、沿用上一组画面
    if r.get("subject") is None:
        return None, []
    subject = (r.get("subject") or "").strip().strip('"').strip("'").strip().rstrip("。，；：、")
    labels = [str(x).strip() for x in (r.get("labels") or []) if str(x).strip()]
    return subject, labels


# ---------------- 开篇导览（R109 新增，规则v2 3.4） ----------------
def extract_overview_points(text, api_key=None):
    """开篇导览：从预告性开场白里提取本期要讲的每个话题点。"""
    import type_match as tm
    api_key = tm.resolve_api_key(api_key)
    system = (
        "从下面这段开场白中提取本期要讲的各个话题，严格输出 JSON："
        '{"points":["话题点1","话题点2","..."]}。'
        "话题点要与原文列举一致：不要合并、不要遗漏，也不要概括成笼统说法。"
        "只输出 JSON，不要任何解释。"
    )
    user = f"内容：{text}\n\n输出 JSON："
    raw = tm.call_deepseek(api_key, system, user)
    r = tm.extract_json(raw)
    return [str(x).strip() for x in (r.get("points") or []) if str(x).strip()]


def build_overview_prompt(points):
    """开篇导览模板：每个话题点各画一张卡片，不笼统抽象化（区别于轻量标题模板）。"""
    point_str = "、".join(f"「{p}」" for p in points)
    return (
        f"一张用于视频开篇导览的简洁扁平插画，非摄影质感。\n"
        f"画面背景为白色。\n"
        f"构图：开篇导览清单，每个话题一张独立卡片，卡片之间留白分隔，不要合并。"
        f"本期话题：{point_str}。\n"
        f"每个话题都要在画面上有对应的卡片或标签，不能简化成一两个抽象符号。\n"
        f"视觉处理：以深灰线条与色块为主，蓝色作为唯一强调色；不做渐变与大面积装饰，保持留白。\n"
        f"标注文字：卡片上写清对应话题的名称，不要添加其他文字或结论。"
    )


# ---------------- Prompt 拼装 ----------------
def build_prompt(typ, subject, labels=None):
    opening = TYPE_OPENING.get(typ, "这是一张用于知识科普视频的插画")
    if typ == "generic":
        subject = f"{subject}（用简洁直观的画面呈现这段内容的核心意思，不必套用特定图表结构）"
    # 标注文字：忠于原文，只用清单里的词
    if labels:
        label_str = "、".join(f"「{l}」" for l in labels)
        annotation = (
            f"标注文字：把以下词语（均来自原文）以清晰文字直接画进图里：{label_str}；"
            f"不要添加清单之外的文字，也不要遗漏清单里的任何一个词。"
        )
    else:
        annotation = (
            "标注文字：需要指向元素说明的文字（元素名称、关键词）用简洁中文直接画进图里，"
            "不要添加原文没有的其他文字或结论性表述。"
        )
    prompt = (
        f"{opening}，简洁扁平插画风格，非摄影质感。\n"
        f"画面背景为白色。\n"
        f"画面主体：{subject}。\n"
        f"画面构图：核心概念居中突出，周边可配少量关键词标签与箭头指向，整体简洁清晰。\n"
        f"视觉处理：以深灰线条与色块为主，蓝色作为唯一强调色；不做渐变与大面积装饰，保持留白。\n"
        f"{annotation}"
    )
    return prompt


# ---------------- RunningHub 调用（复用 R92/R94） ----------------
def _request(url, payload, api_key, timeout=120):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _network_error(e):
    # 网络/连接层瞬时错误（重试值得）：SSL UNEXPECTED_EOF_WHILE_READING、超时、连接重置、
    # 拒绝连接等。URLError / ssl.SSLError / socket.timeout / ConnectionResetError /
    # TimeoutError 都是 OSError 子类，http.client.HTTPException 单独补上。
    return isinstance(e, (OSError, http.client.HTTPException))


def _retryable_http_status(status):
    # 5xx 服务端瞬时错误、429 限流 → 可重试；其余 4xx（prompt 内容审核拒绝、参数/鉴权错误）
    # 重试无意义 → 不重试。
    return status >= 500 or status == 429


def _request_retry(url, payload, api_key, timeout=120):
    """带自动重试的请求：网络错误 / 5xx / 429 按 1s/3s/5s 退避重试，最多 RETRY_ATTEMPTS 次；
    4xx 立即返回不重试；重试用尽仍失败则明确抛出，交上层处理（不静默）。"""
    for attempt in range(RETRY_ATTEMPTS + 1):
        try:
            st, raw = _request(url, payload, api_key, timeout)
        except Exception as e:
            if _network_error(e) and attempt < RETRY_ATTEMPTS:
                wait = RETRY_BACKOFF_SECONDS[attempt]
                print(f"[generate] 网络错误 {type(e).__name__}: {e}；{wait}s 后第 {attempt + 1} 次重试")
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"请求失败：网络错误 {type(e).__name__}: {e}（重试 {RETRY_ATTEMPTS} 次后仍失败，已抛出）"
            )
        if _retryable_http_status(st) and attempt < RETRY_ATTEMPTS:
            wait = RETRY_BACKOFF_SECONDS[attempt]
            print(f"[generate] HTTP {st} 瞬时错误；{wait}s 后第 {attempt + 1} 次重试")
            time.sleep(wait)
            continue
        return st, raw


def _walk(v):
    if isinstance(v, dict):
        for x in v.values():
            yield from _walk(x)
    elif isinstance(v, list):
        for x in v:
            yield from _walk(x)
    else:
        yield v


def _poll_task(tid, api_key, max_polls=MAX_POLLS, poll_seconds=POLL_SECONDS):
    """轮询单个任务直到成功/失败/超时。成功返回 (url, usage)；失败抛 RuntimeError；超时抛 TimeoutError。"""
    for _ in range(max_polls):
        time.sleep(poll_seconds)
        st2, qraw = _request_retry(f"{RH_BASE}/openapi/v2/query", {"taskId": tid}, api_key, timeout=60)
        try:
            qj = json.loads(qraw)
        except Exception:
            qj = {}
        d = qj.get("data", qj) if isinstance(qj, dict) else qj
        status = ""
        if isinstance(d, dict):
            status = str(d.get("status") or d.get("taskStatus") or "")
        elif isinstance(d, str):
            status = d
        if status.lower() in ("success", "succeeded", "complete", "completed"):
            url = None
            for v in _walk(d):
                if isinstance(v, str) and v.startswith("http"):
                    url = v
                    break
            if url:
                return url, d.get("usage") if isinstance(d, dict) else None
            raise RuntimeError(f"任务成功但无输出 URL: {qraw[:300]}")
        if status.lower() in ("fail", "failed", "error", "cancel", "cancelled"):
            raise RuntimeError(f"生成失败: {qraw[:300]}")
    raise TimeoutError(f"轮询超时（{poll_seconds}s × {max_polls}）")


def generate(prompt, api_key, aspect_ratio, resolution,
             max_polls=MAX_POLLS, poll_seconds=POLL_SECONDS, poll_retries=POLL_RETRIES):
    st, raw = _request_retry(f"{RH_BASE}/openapi/v2/rhart-image-g-2/text-to-image",
                             {"prompt": prompt, "aspectRatio": aspect_ratio, "resolution": resolution},
                             api_key)
    tid = None
    try:
        j = json.loads(raw)
        for v in _walk(j):
            if isinstance(v, str) and v.isdigit() and len(v) >= 12:
                tid = v
                break
    except Exception:
        pass
    if not tid:
        raise RuntimeError(f"提交失败 HTTP {st}: {raw[:300]}")

    # M3: 轮询超时是网络抖动(R160 G61/G91 实测)，任务在服务端往往已成功。
    # 复用同一 taskId 重新轮询(不重新提交省费用)，最多 poll_retries 次；用尽仍失败明确报次数，不静默。
    for attempt in range(poll_retries + 1):
        try:
            return _poll_task(tid, api_key, max_polls, poll_seconds)
        except TimeoutError:
            if attempt < poll_retries:
                print(f"[generate] 轮询超时({poll_seconds}s×{max_polls})，复用 taskId {tid} 第 {attempt + 1} 次重新轮询")
                continue
            raise RuntimeError(
                f"轮询超时（{poll_seconds}s × {max_polls}），复用 taskId 重新轮询 {poll_retries} 次后仍超时，taskId={tid}"
            )


def main():
    parser = argparse.ArgumentParser(description="wz-sp-ctsc 插画生成（Prompt 拼装 + RunningHub 文生图）")
    parser.add_argument("--type", required=True, help="插画类型 id（illustration/title/generic）")
    parser.add_argument("--subject", required=True, help="画面主体描述（从文章内容提炼的具象场景）")
    parser.add_argument("--text", help="原始内容文本（仅记录用，不参与生成）")
    parser.add_argument("--aspect-ratio", default=DEFAULT_ASPECT, help=f"宽高比（默认 {DEFAULT_ASPECT}）")
    parser.add_argument("--resolution", default=DEFAULT_RESOLUTION, help=f"分辨率（默认 {DEFAULT_RESOLUTION}）")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help=f"输出目录（默认 {DEFAULT_OUT_DIR}）")
    parser.add_argument("--name", help="输出文件名前缀（默认 rh-ctsc-{type}）")
    args = parser.parse_args()

    prompt = build_prompt(args.type, args.subject)
    print("===== 最终 Prompt（喂给 API 的完整文本） =====")
    print(prompt)
    print("=" * 50)

    api_key = read_rh_key()
    url, usage = generate(prompt, api_key, args.aspect_ratio, args.resolution)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.name or f"rh-ctsc-{args.type}"
    out = out_dir / f"{prefix}-{int(time.time())}.png"
    urllib.request.urlretrieve(url, out)
    print(f"\n生成成功：{out}")
    if usage:
        print(f"计费：{json.dumps(usage, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
