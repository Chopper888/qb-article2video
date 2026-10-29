#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
插画类型判断

用 DeepSeek（deepseek-v4-pro，temperature=0，thinking disabled）做语义匹配，
输出每个候选的 score + reason + considered；score < 0.6 的候选排除，
全部不达标返回 type="generic"（兜底插画）。

候选清单见 references/type-manifest.json。
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

BASE_URL = "https://api.deepseek.com/chat/completions"
KEY_ENV = "DEEPSEEK_API_KEY"
MODEL = "deepseek-v4-pro"

SCORE_THRESHOLD = 0.6

# R154 成本反馈：记录最近一次 DeepSeek 调用的 usage（调用方读取累计，不改变返回签名）
_last_usage = None


def skill_base() -> Path:
    return Path(__file__).resolve().parent.parent


def load_types() -> list[dict]:
    manifest = skill_base() / "references" / "type-manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    return data["types"]


# ---------------- API key 解析（ST1-P5b 乙：只读环境变量，删注册表/dotenv 兜底） ----------------
def resolve_api_key(api_key=None):
    v = api_key or os.environ.get(KEY_ENV)
    if v and v.strip():
        return v.strip()
    raise RuntimeError("BLOCKED_MISSING_DEEPSEEK_API_KEY")


# ---------------- 提示词 ----------------
def build_system_prompt(types):
    lines = [
        f"下面给出 {len(types)} 种插画类型，请判断每段口播分别适合哪一种。",
        "",
        "插画类型及适用场景：",
    ]
    for i, t in enumerate(types, 1):
        lines.append(f"{i}. {t['name']}（id={t['id']}）：{t['desc']}")
    lines += [
        "",
        "匹配规则：",
        "1. 按内容的语义与结构判断，不看表面特征。",
        "2. 没有任何类型贴合度足够高（score < 0.6）时返回 type=\"generic\"。",
        "3. 记录候选过程：每段列出认真考虑过的其他 1~3 个候选，各给 score 与未选原因。",
        "4. score 为 0~1 的主观贴合度，保留一位小数。",
        "",
        "输出格式（严格 JSON，不要输出任何其他文字或解释）：",
        '{"results": [',
        '  {"id": "<段落标识>", "type": "<类型id 或 generic>", "score": 0.0,',
        '   "reason": "<为什么选这个>",',
        '   "considered": [{"type": "<候选id>", "score": 0.0, "why_not": "<为什么没选>"}]}',
        ']}',
        "",
        "results 数组长度必须等于输入段落数量，id 与输入一一对应。",
    ]
    return "\n".join(lines)


def build_user_prompt(segments):
    lines = ["以下是待判断的口播内容片段（共 %d 段）：" % len(segments), ""]
    for s in segments:
        lines.append(f"[{s['id']}] {s['text']}")
    lines.append("")
    lines.append("请输出 JSON 结果。")
    return "\n".join(lines)


# ---------------- API 调用（照搬 R75） ----------------
def call_deepseek(api_key, system, user):
    global _last_usage
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 8000,
        "stream": False,
        "temperature": 0,
        "thinking": {"type": "disabled"},
    }
    r = requests.post(BASE_URL, headers=headers, json=body, timeout=240)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {(r.text or '')[:300]}")
    data = r.json()
    _last_usage = data.get("usage")
    content = data["choices"][0]["message"].get("content")
    if not content or not content.strip():
        raise RuntimeError("empty content")
    return content


def extract_json(content):
    content = content.strip()
    m = re.search(r"\{[\s\S]*\}", content)
    if m:
        content = m.group(0)
    return json.loads(content)


def build_out(r, seg_by_id, type_by_id):
    considered = []
    for c in (r.get("considered") or []):
        ct = c.get("type", "none")
        considered.append({
            "type": ct,
            "name": type_by_id.get(ct, {}).get("name", "（不匹配）"),
            "score": c.get("score"),
            "why_not": c.get("why_not", ""),
        })
    ty = r.get("type", "generic")
    return {
        "id": r["id"],
        "text": seg_by_id[r["id"]]["text"],
        "matched_type": ty,
        "matched_name": type_by_id.get(ty, {}).get("name", "兜底" if ty == "generic" else "（不配图）"),
        "score": r.get("score"),
        "reason": r.get("reason", ""),
        "considered": considered,
    }


def match(segments, api_key=None):
    """对给定 segments（[{id,text}]）做类型判断，返回 results 列表。"""
    api_key = resolve_api_key(api_key)
    types = load_types()
    type_by_id = {t["id"]: t for t in types}
    system = build_system_prompt(types)
    user = build_user_prompt(segments)
    raw = call_deepseek(api_key, system, user)
    parsed = extract_json(raw)
    llm_results = parsed["results"]

    seg_by_id = {s["id"]: s for s in segments}
    results = [build_out(r, seg_by_id, type_by_id) for r in llm_results]
    # 确保 id 顺序与输入一致
    order = [s["id"] for s in segments]
    results.sort(key=lambda x: order.index(x["id"]) if x["id"] in order else len(order))
    return results


def main():
    parser = argparse.ArgumentParser(description="wz-sp-ctsc 插画类型判断（DeepSeek 语义匹配）")
    parser.add_argument("--text", help="单段内容文本（直接传）")
    parser.add_argument("--file", help="JSON 文件，内容为 [{id, text}, ...]")
    parser.add_argument("--out", help="结果输出 JSON 路径（可选）")
    args = parser.parse_args()

    if args.file:
        segments = json.loads(Path(args.file).read_text(encoding="utf-8"))
    elif args.text:
        segments = [{"id": "seg1", "text": args.text}]
    else:
        print("用法：--text '内容' 或 --file input.json", file=sys.stderr)
        return 2

    results = match(segments)

    payload = {"types": load_types(), "segments": segments, "results": results}
    if args.out:
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    # 人读摘要
    print("\n===== Type 判断结果 =====", file=sys.stderr)
    for o in results:
        cons = "; ".join(f"{c['name']}({c.get('score')})" for c in o["considered"]) or "无"
        print(f"\n{o['id']}: {o['matched_name']} (score={o['score']})", file=sys.stderr)
        print(f"  理由: {o['reason']}", file=sys.stderr)
        print(f"  考虑过但未选: {cons}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
