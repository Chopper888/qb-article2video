# -*- coding: utf-8 -*-
r"""各付费点的桩（ST1-P4a2 任务书 §三.A）。

**桩只用于开发测试**（`--stub`，见 cli.py 的 `--help`），产物在账本里标 `桩`。
每个桩的返回格式必须与真实接口**被下游解析的那部分**一致，依据的解析代码见各函数
docstring 里的「依据」——回执 §A 逐个列出（文件 + 行号）。

本轮**零真实调用**：新文章路径全靠这些桩跑通；两篇样本靠缓存回放/导入。
"""
import hashlib
import io
import json
import wave
from pathlib import Path

# 与真实豆包 TTS 输出一致的音频参数（依据：001-示例篇/03-配音/audio/*.wav 实测
# = 单声道 / 16 bit / 24000 Hz；下游 pipeline/render_mux.py 只按容器解码，不挑采样率，
# 但 S4 对齐用的是 sentences.json 的时间戳，不是波形，所以这里只要时长对得上）。
TTF_RATE = 24000
TTF_WIDTH = 2
CHAR_SEC = 0.15          # 每字时长（秒），量级与真人语速一致，保证 S4 能对齐

# 出图桩尺寸：RunningHub 的 "1k" + 16:9 实际给的画布（依据：07-出图/images 里
# 导入图的实测尺寸 1024×576；S7/S9 的 image_sizes 只关心宽高比，取同一组）。
IMAGE_SIZE = (1024, 576)


# --------------------------------------------------------------------------
# S0 识图
# --------------------------------------------------------------------------


def stub_vision_classify(image_bytes, prompt, max_tokens=512, timeout=60):
    """S0 识图桩。依据：`steps.step_s0` 读 `图片分类.json` 的 `items[sha256]["分类"]`，
    取值必须落在 `steps.SCREENSHOT_LABELS`（`截图`/`真实截图`）里才算截图，否则算配图。

    桩一律判「非截图」——新文章样本（示例篇-仅输入）是理论型，判成配图才会走理论线。
    真实接口是 ARK 视觉模型返回 JSON（见 M3-P0 选型结论），这里返回同形的 JSON 文本。
    """
    return '{"分类": "非截图", "依据": "桩"}'


# --------------------------------------------------------------------------
# S1 口播
# --------------------------------------------------------------------------


def make_stub_narration(article_text):
    """S1 口播桩：直接返回原文。依据：`steps.step_s2` 用 `splitter.split_narration`
    切 `01-口播/narration.txt`——它按中文标点/换行切分，原文本身就能切，不会空手。

    真实接口是 DeepSeek 按模板重写的口播稿；桩不做重写，只为把通路跑通。
    """
    def stub(prompt, max_tokens=4096):
        return article_text
    return stub


# --------------------------------------------------------------------------
# S3 配音
# --------------------------------------------------------------------------


def silence_wav_bytes(seconds):
    n = max(1, int(round(seconds * TTF_RATE)))
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(TTF_WIDTH)
    w.setframerate(TTF_RATE)
    w.writeframes(b"\x00\x00" * n)
    w.close()
    return buf.getvalue()


def stub_tts(text):
    """配音桩：按文本长度给出**字级时间戳**，音频时长与之相称。

    依据：`steps.step_s4` → `pipeline/word_align.py:align_groups` 读
    `sentences.json` 的 `words[].{word,startTime,endTime}`，并要求每个词
    **按字符展开**与 `group_text` 对齐；`scene_build` 读 `sentences[].{text,startTime,endTime}`。

    返回**纯 JSON**（可进缓存）`{"sentences": [...], "audio_seconds": 总时长}`；
    WAV 字节由调用方用 `silence_wav_bytes(seconds)` 现生成——真实接口返回的是
    `audio_url`，音频本来也不进 JSON 缓存。
    """
    text = text or ""
    # 按换行/中文句末标点切句，保留标点（真实返回的 text 也是原文切片）
    sentences = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？\n；":
            sentences.append(buf)
            buf = ""
    if buf:
        sentences.append(buf)

    out, t = [], 0.0
    for s in sentences:
        if not s:
            continue
        words, st = [], t
        for ch in s:
            if ch == "\n":
                continue
            et = round(st + CHAR_SEC, 3)
            words.append({"word": ch, "startTime": round(st, 3), "endTime": et,
                          "confidence": 0})
            st = et
        # 换行符也要计入时轴，避免相邻句首尾重叠
        if s.endswith("\n"):
            st = round(st + CHAR_SEC, 3)
        out.append({"text": s, "startTime": round(t, 3), "endTime": round(st, 3),
                    "words": words})
        t = st
    total = out[-1]["endTime"] if out else CHAR_SEC
    return {"sentences": out, "audio_seconds": total}


# --------------------------------------------------------------------------
# S6 导演链 / 图句匹配 / 标签 / 停留判断（文本 + 识图）
# --------------------------------------------------------------------------


def stub_text(prompt, max_tokens=4096):
    """S6 系文本桩。依据：各链都走 `shot_ai._load_json_or_none(text_call(...))` 一类的
    解析，桩返回一个**空 JSON 对象**，让链走"解析不出 → 按兜底"的分支，不抛异常。

    本轮真实运行不会走到这里：示例篇-截图/混合型的 S6 全部命中历史缓存
    （`prepare_sample` 预置的 P7 230 条 / P9 48 条）。桩的分支覆盖缺口记在回执 §六。
    """
    return "{}"


def stub_vision(image_bytes, prompt, max_tokens=512, timeout=60):
    """S6 系识图桩（方舟）。依据同 `stub_text`：解析不出即走兜底，不抛异常。"""
    return "{}"


# --------------------------------------------------------------------------
# S7 决策（类型判断 / 主体提炼 / 标题场景 / 导览点）
# --------------------------------------------------------------------------


def stub_match(segs):
    """`deps["match"]` 桩。依据：`orchestrate.decide_records` 读
    `r["id"] / r["matched_type"] / r["matched_name"]`。

    一律判 `generic`（走 `generic兜底` 分支，不碰 route_hook 的截图分支）。
    """
    return [{"id": s["id"], "text": s["text"], "matched_type": "generic",
             "matched_name": "兜底", "score": 0.0, "reason": "桩", "considered": []}
            for s in segs]


def stub_extract_subject(text, name):
    """`deps["extract_subject"]` 桩。依据：`decide_records` 拿 `(subject, labels)`，
    `subject is None` 表示弃权。桩取正文前若干字当主体，**不返回 None**（保证出图）。
    """
    return (text.strip()[:24] or "画面", [])


def stub_extract_title(title_text):
    """`deps["extract_title"]` 桩。依据：`decide_records` 拿 `(title, subtitle, element)`，
    三者喂给 `tgen.build_title_prompt`。"""
    t = title_text.strip().strip("【】")
    return (t or "标题", "", "一个简洁的几何图形")


def stub_extract_overview(text):
    """`deps["extract_overview"]` 桩。依据：`decide_records` 拿一个 points 列表喂给
    `gen.build_overview_prompt`。"""
    head = text.strip().split("\n")[0][:30]
    return [{"point": head or "导览", "detail": ""}]


# --------------------------------------------------------------------------
# S8 出图
# --------------------------------------------------------------------------


def stub_generate_image(prompt, size=IMAGE_SIZE):
    """出图桩：生成一张**占位 PNG**，尺寸与真实参数一致（1k / 16:9 → 1024×576）。

    依据：`steps.step_s8` 把返回的 bytes 直接写进 `07-出图/images/<文件名>`；
    `steps.step_s7` 的 `_img_size` 用 PIL 读宽高算 `image_sizes`。

    PNG 由 zlib 压缩纯色像素手工拼出（不依赖 PIL 的写图能力），内容随提示词变化，
    便于"同一提示词 → 同一字节"的缓存验证。
    """
    import struct
    import zlib
    w, h = size
    seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:6], 16)
    r, g, b = 0x20 + (seed & 0x3F), 0x40 + ((seed >> 6) & 0x3F), 0x80 + ((seed >> 12) & 0x3F)
    row = b"\x00" + bytes([r, g, b]) * w
    raw = row * h

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    return png
