# -*- coding: utf-8 -*-
"""ST3-P2c：画面级要素上屏核对（以成片画面为准，不是场景数据）。

从成片 mp4 抽帧，对要素时间窗内的画面做像素判定。判定公式（写进回执）：

  深色背景像素占比 = 卡片区域内「每通道亮度 < DARK_MAX」的像素数 / 区域像素数
  - terminal / code_card：卡片顶部区域（x 430~1490、y 90~300）深色占比 >= BG_MIN_RATIO 即命中；
  - table：表头区域（x 430~1490、y 90~180）深色占比 >= HEADER_MIN_RATIO 即命中。

「深色」以 ST3Components.tsx 的目标背景色（terminal #0D1117 / code_card #1E1E1E /
table 表头 COLOR_INK）为基准，宽化为 DARK_MAX（容忍入场淡入把背景色稀释为中等灰、
以及抗锯齿/JPEG 压缩），不写死具体颜色值。
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path

from pipeline.engine import paths

# 判定阈值（写进回执）
DARK_MAX = 140               # 「深色」= 每通道亮度 < 140（目标色 #0D1117/#1E1E1E 的宽化）
BG_MIN_RATIO = 0.25          # 卡片顶部区域深色占比阈值
HEADER_MIN_RATIO = 0.15      # 表格表头区域深色占比阈值
CARD_X_RANGE = (430, 1490)   # 卡片水平范围（卡片宽 1000~1120 居中）
CARD_Y_RANGE = (90, 300)     # 卡片顶部区域（ElementStage top=90）
TABLE_HEADER_Y_RANGE = (90, 180)  # 表格表头 y 区间

# 抽帧只取「完全显示区间」：要素起点加转场时长，到终点减转场时长。
# 当前不做转场，时长为 0，直接取要素的时间窗。
TRANSITION_FRAMES = 0

# 标题栏的通道容差。目标色为深灰（CODE_HEADER #2D2D2D、终端 #1F2428、
# 表格表头 COLOR_INK #1d1d1f）；半透明时会和浅灰背景混成偏亮的灰，
# 所以容差按深浅区间的一半取（约 (245-45)/2=100），100 足够区分。
TITLEBAR_MAX_CHANNEL = 100

# 插画/标题卡特征色（强调蓝 #2F6FEB，与 colors.ts 的 COLOR_ACCENT 同步），卡片区域内不应出现。
ACCENT_RGB = (0x2F, 0x6F, 0xEB)
ACCENT_TOLERANCE = 60        # 每通道偏差容忍（压缩/抗锯齿）
# 代码卡片里的字符串颜色（STR_COLOR #CE9178）也可能落在容差范围内；
# 距离字符串色比距离强调色更近的像素不计为强调色。
CODE_STR_RGB = (0xCE, 0x91, 0x78)


def _named(src, name):
    m = re.search(r"\bconst\s+" + re.escape(name) + r'\s*=\s*"#([0-9a-fA-F]{6})"', src)
    return m.group(1) if m else None


def read_element_colors():
    """读要素配色（ST3Components.tsx / colors.ts），返回 {type: 目标背景色 hex}。"""
    st3 = (paths.ENGINE_ROOT / "R57-render-test" / "src" / "ST3Components.tsx").read_text(encoding="utf-8")
    colors = (paths.ENGINE_ROOT / "R57-render-test" / "src" / "colors.ts").read_text(encoding="utf-8")

    term_bg = re.search(r'background:\s*"#([0-9a-fA-F]{6})"', st3)
    term_bg = term_bg.group(1) if term_bg else "0D1117"
    code_bg = _named(st3, "CODE_BG") or "1E1E1E"
    color_ink = _named(colors, "COLOR_INK") or "1d1d1f"
    return {
        "terminal": term_bg,
        "code_card": code_bg,
        "table": color_ink,
    }


def _extract_frame(mp4, frame, fps, tmpdir):
    from PIL import Image
    t = frame / fps
    out = os.path.join(tmpdir, f"f{frame}.jpg")
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(mp4),
         "-frames:v", "1", out],
        capture_output=True, timeout=120)
    if r.returncode != 0 or not os.path.isfile(out):
        return None
    return Image.open(out).convert("RGB")


def _dark_ratio(img, region):
    """区域内「每通道亮度 < DARK_MAX」的像素占比。"""
    (x0, x1), (y0, y1) = region
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(img.width, x1); y1 = min(img.height, y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    px = img.crop((x0, y0, x1, y1)).load()
    w = x1 - x0; h = y1 - y0
    hit = 0
    for yy in range(0, h, 2):
        for xx in range(0, w, 2):
            r, g, b = px[xx, yy]
            if r < DARK_MAX and g < DARK_MAX and b < DARK_MAX:
                hit += 1
    total = (h // 2 + (1 if h % 2 else 0)) * (w // 2 + (1 if w % 2 else 0))
    return hit / total if total else 0.0


def _avg_max_channel(img, region):
    """区域平均色的最大通道值（用于标题栏「是否仍是深灰」判定）。"""
    (x0, x1), (y0, y1) = region
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(img.width, x1); y1 = min(img.height, y1)
    if x1 <= x0 or y1 <= y0:
        return 255
    px = img.crop((x0, y0, x1, y1)).load()
    w = x1 - x0; h = y1 - y0
    s = 0; n = 0
    for yy in range(0, h, 2):
        for xx in range(0, w, 2):
            r, g, b = px[xx, yy]
            s += max(r, g, b); n += 1
    return s / n if n else 255


def _d2(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b))


def _accent_ratio(img, region):
    """区域内「接近强调蓝 #2F6FEB」的像素占比（标题卡/插画特征色）。"""
    (x0, x1), (y0, y1) = region
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(img.width, x1); y1 = min(img.height, y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    px = img.crop((x0, y0, x1, y1)).load()
    w = x1 - x0; h = y1 - y0
    hit = 0
    for yy in range(0, h, 2):
        for xx in range(0, w, 2):
            r, g, b = px[xx, yy]
            if (abs(r - ACCENT_RGB[0]) <= ACCENT_TOLERANCE
                    and abs(g - ACCENT_RGB[1]) <= ACCENT_TOLERANCE
                    and abs(b - ACCENT_RGB[2]) <= ACCENT_TOLERANCE
                    and _d2((r, g, b), ACCENT_RGB) < _d2((r, g, b), CODE_STR_RGB)):
                hit += 1
    total = (h // 2 + (1 if h % 2 else 0)) * (w // 2 + (1 if w % 2 else 0))
    return hit / total if total else 0.0


# 卡片区域（判定「要素上屏」的整卡范围；A5 居中后卡片仍在此区内）
CARD_FULL_Y_RANGE = (90, 780)


def _header_band(img):
    """扫描整卡区，找「最暗的 40px 高水平带」（卡片标题栏）。

    返回 (dark_ratio, avg_max_channel)。卡片标题栏是整宽深灰带（暗占比高），
    标题卡文字是细字（暗占比低），故用暗占比区分两者。
    """
    x0, x1 = CARD_X_RANGE
    best = (0.0, 255)
    for y in range(90, 740, 8):
        region = ((x0, x1), (y, y + 40))
        ratio = _dark_ratio(img, region)
        if ratio > best[0]:
            best = (ratio, _avg_max_channel(img, region))
    return best


def judge_frame(img, etype):
    """对单帧做「要素上屏」判定：标题栏整宽深灰带 + 无强调色。返回 True/False。"""
    # 标题栏核对：应有一条整宽深灰带（半透明/未上屏时该带被背景混亮、或只是细字）
    ratio, avg = _header_band(img)
    if ratio < 0.5 or avg > TITLEBAR_MAX_CHANNEL:
        return False
    # 无强调色：卡片区域内出现强调蓝（标题卡/插画特征色）判未上屏
    if _accent_ratio(img, (CARD_X_RANGE, CARD_FULL_Y_RANGE)) > 0.02:
        return False
    return True


def _sample_frames(st, en, fps):
    """完全显示区间内的抽帧点：[起点+转场, 终点-转场]。

    ST3-P16：场景的 end_frame 不含在场景里（它是下一个场景的第一帧），最后一个抽帧点取 end_frame − 1。
    """
    inner_s = st + TRANSITION_FRAMES
    inner_e = en - 1 - TRANSITION_FRAMES
    if inner_e - inner_s >= TRANSITION_FRAMES:
        # 完全显示区间 ≥ 转场长度：取 起点、中点、终点 三点
        return [inner_s, (inner_s + inner_e) // 2, inner_e]
    if inner_e > inner_s:
        return [(inner_s + inner_e) // 2]
    return [st + max(0, (en - st) // 2)]


def check_element_frames(data, mp4):
    """对成片所有要素场景做画面级核对（收紧后口径）。返回 [{"element_id","type","frames","hits","ok"}]。"""
    fps = int(data.get("fps") or 30)
    results = []
    tmpdir = tempfile.mkdtemp(prefix="elemcheck_")
    try:
        for sc in data.get("scenes", []):
            etype = sc.get("type")
            if etype not in ("code_card", "terminal", "table"):
                continue
            st, en = sc.get("start_frame", 0), sc.get("end_frame", 0)
            frames = _sample_frames(st, en, fps)
            hits = 0
            for f in frames:
                img = _extract_frame(mp4, f, fps, tmpdir)
                if img is not None and judge_frame(img, etype):
                    hits += 1
            need = 1 if len(frames) == 1 else 2
            results.append({
                "element_id": sc.get("element_id"),
                "type": etype,
                "frames": len(frames),
                "hits": hits,
                "ok": hits >= need,
            })
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    return results
