# -*- coding: utf-8 -*-
"""shot_reconcile：截图镜头对账（SHOT-M4-P3 D2 步收成）。

把「对账静帧」（ShotCheck 合成渲染出的灰图 + 红高亮 + 橙箭头实色帧）与镜头数据的
「登记位置」逐元素比对，输出实际位置与登记位置的误差。

容差（与 P6b 相同）：
  - 颜色差值之和 ≤ 60（识别目标色像素时，|ΔR|+|ΔG|+|ΔB| ≤ 60）；
  - 四边误差 ≤ 2px（实测 bbox 与登记 rect 的 x/y/w/h 各自相差 ≤ 2px）。

登记位置按 rel_x/rel_y 沿父链重建为绝对坐标（与渲染端一致的定位方式），
因此改 rel_y 会反映到登记位置，能被负向验证捕获。
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

HIGHLIGHT_RGB = (0x2F, 0x6F, 0xEB)  # #2F6FEB（与 ScreenshotScene 的高亮框色同步）
ARROW_RGB = (0x1B, 0x4F, 0xBF)      # #1B4FBF（与 ScreenshotScene 的箭头色同步）
COLOR_TOL = 60
SIDE_TOL = 2.0


def load_rgb(frame_path):
    return np.asarray(Image.open(frame_path).convert("RGB")).astype(int)


def measure_color_bbox(arr, target, tol=COLOR_TOL):
    """扫描全图，返回与 target 颜色差值之和 ≤ tol 的像素的最小外接矩形 (x, y, w, h)。
    无匹配像素返回 None。"""
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    diff = np.abs(r - target[0]) + np.abs(g - target[1]) + np.abs(b - target[2])
    mask = diff <= tol
    if not mask.any():
        return None
    ys, xs = np.where(mask)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    return (x0, y0, x1 - x0 + 1, y1 - y0 + 1)


def absolute_xy(el, by_id):
    """沿父链累加 rel_x/rel_y（缺省回退 rect），得到元素绝对左上角 (x, y)。"""
    x, y = 0.0, 0.0
    cur = el
    while cur is not None:
        rx = cur.get("rel_x")
        ry = cur.get("rel_y")
        x += rx if rx is not None else cur["rect"]["x"]
        y += ry if ry is not None else cur["rect"]["y"]
        cur = by_id.get(cur["parent"]) if cur.get("parent") else None
    return x, y


def reconcile_frame(frame_path, shot):
    """对单张对账静帧 + 单镜头数据做比对。返回 (results, ok)。

    results 每项：{id, kind, registered, measured, error, ok}
      registered/measured 为 (x, y, w, h)；无匹配像素时 measured/error 为 None。
    """
    arr = load_rgb(frame_path)
    by_id = {el["id"]: el for el in shot.get("elements", [])}
    results = []
    ok = True
    for el in shot.get("elements", []):
        if el["kind"] == "highlight":
            target = HIGHLIGHT_RGB
        elif el["kind"] == "arrow":
            target = ARROW_RGB
        else:
            continue
        bbox = measure_color_bbox(arr, target)
        rx, ry = absolute_xy(el, by_id)
        reg = (rx, ry, el["rect"]["w"], el["rect"]["h"])
        if bbox is None:
            results.append({"id": el["id"], "kind": el["kind"],
                            "registered": reg, "measured": None, "error": None, "ok": False})
            ok = False
        else:
            err = max(abs(bbox[0] - reg[0]), abs(bbox[1] - reg[1]),
                      abs(bbox[2] - reg[2]), abs(bbox[3] - reg[3]))
            el_ok = err <= SIDE_TOL
            results.append({"id": el["id"], "kind": el["kind"],
                            "registered": reg, "measured": bbox, "error": round(err, 3), "ok": el_ok})
            if not el_ok:
                ok = False
    return results, ok


def _load_shots(path):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(d, dict) and "shots" in d:
        return d["shots"]
    if isinstance(d, list):
        return d
    return [d]


def main():
    ap = argparse.ArgumentParser(description="截图镜头对账")
    ap.add_argument("--frame", help="单张对账静帧 PNG 路径（与 --shot 配用）")
    ap.add_argument("--shot", help="单镜头数据 JSON 路径（与 --frame 配用）")
    ap.add_argument("--shots", help="镜头数据 JSON（含 shots 列表或单个镜头），批量模式")
    ap.add_argument("--frames-dir", help="批量模式：对账静帧目录，帧名 shotNN.png（NN 为 shot_index 两位）")
    args = ap.parse_args()

    if args.frame and args.shot:
        shot = _load_shots(args.shot)[0]
        results, ok = reconcile_frame(args.frame, shot)
        _print_one(shot, results, ok)
        sys.exit(0 if ok else 1)

    if args.shots and args.frames_dir:
        shots = _load_shots(args.shots)
        all_ok = True
        print("=" * 70)
        for s in shots:
            idx = s.get("shot_index", 0)
            frame = Path(args.frames_dir) / f"shot{idx:02d}.png"
            if not frame.exists():
                print(f"shot{idx:02d} [{s.get('image')}]: 静帧缺失 {frame}")
                all_ok = False
                continue
            results, ok = reconcile_frame(str(frame), s)
            _print_one(s, results, ok)
            all_ok = all_ok and ok
        print("=" * 70)
        print("对账总体:", "PASS" if all_ok else "FAIL")
        sys.exit(0 if all_ok else 1)

    ap.print_help()
    sys.exit(2)


def _print_one(shot, results, ok):
    img = shot.get("image", "?")
    if not results:
        print(f"shot{shot.get('shot_index', 0):02d} [{img}]: 无高亮/箭头元素，跳过")
        return
    print(f"shot{shot.get('shot_index', 0):02d} [{img}]: {'PASS' if ok else 'FAIL'}")
    for r in results:
        reg = r["registered"]
        if r["measured"] is None:
            print(f"  {r['id']} ({r['kind']}): 登记 {reg[0]:.1f},{reg[1]:.1f},{reg[2]:.1f},{reg[3]:.1f}  实测 N/A  -> FAIL")
        else:
            m = r["measured"]
            print(f"  {r['id']} ({r['kind']}): 登记 {reg[0]:.1f},{reg[1]:.1f},{reg[2]:.1f},{reg[3]:.1f}  实测 {m[0]},{m[1]},{m[2]},{m[3]}  误差 {r['error']}  -> {'PASS' if r['ok'] else 'FAIL'}")


if __name__ == "__main__":
    main()
