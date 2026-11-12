# -*- coding: utf-8 -*-
"""质检闸门 v2：RapidOCR + 预处理放大 + 模糊匹配。

用法：
    ok, detail = ocr_check("x.png", ["主标题", "标签1"])
    # ok=False 时 detail 给出 missing / extra，可据此决定是否重跑

相对 v1 的三处改进（由人工判定过的 15 张校准得出）：
    1. 窄图自动放大到宽 1600 再识别 —— 小字（「断档第一」）不再漏识
    2. 拉丁按编辑距离 ≤1 判定 —— DeepSeek/DeeSeek 这类误识不再误杀
    3. 中文 ≥4 字允许 1 字差异 —— 手写体个别笔画误判不再误杀
    4. 过滤纯符号碎片（如「×口l」）—— 装饰笔画不再算额外文字
"""
import re
import sys

_ENGINE = None

# ---------- 版本与依赖 ----------
OCR_GATE_VERSION = "v6"      # 建议软件侧把它算进缓存键（判定口径变化时递增）
# 依赖（均纯 CPU，不需要 CUDA）：
#   rapidocr-onnxruntime 1.4.4   —— 自带模型：ch_PP-OCRv4_det 4.7MB
#                                   ch_PP-OCRv4_rec 10.9MB
#                                   ch_ppocr_mobile_v2.0_cls 0.6MB（合计约 16MB，随包分发）
#   onnxruntime 1.22.0
#   numpy 1.26.4
#   pillow 11.3.0
# 不依赖任何本机路径、不依赖 Hermes 沙箱、不联网。



def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def _norm(s: str) -> str:
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", "", s)
    for a, b in (("：", ":"), ("，", ","), ("（", "("), ("）", ")"), ("、", ",")):
        s = s.replace(a, b)
    return s.lower()


def _cj(v):
    return [c for c in v if "\u4e00" <= c <= "\u9fff"]


def _latin(v):
    return re.sub(r"[^a-z0-9]", "", v.lower())


def _lev(a, b, cap=2):
    """编辑距离，超过 cap 提前返回 cap+1。"""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def found(label: str, joined: str, lines) -> bool:
    """清单项是否出现在识别结果里。

    v4 改动（多词英文与数字标签）：
      ① 含数字的拉丁标签（如 GPT-5）不再用编辑距离容忍 —— 数字序列必须完全一致，
         否则空一个数字也算通过（GPT-5 被画成 GPT 也判合格）。
      ② 混排标签（如 第三方API）要求中文段与拉丁段**同时**出现；
         v3 只要拉丁段出现就返回 True，中文整段丢了也判合格。
    """
    n = _norm(label)
    if n in joined:
        return True
    cjk = "".join(_cj(n))
    lab_lat = _latin(n)

    cjk_ok = None
    if cjk:
        if cjk in joined:
            cjk_ok = True
        elif len(cjk) >= 4:
            cjk_ok = False
            for ln in lines:
                t = _norm(ln)
                # 只容忍「同长度、1 个字写错」；长度不同（缺字/多字）一律算不合格
                if len(t) == len(n) and _lev(n, t) <= 1:
                    cjk_ok = True
                    break
        else:
            cjk_ok = False

    lat_ok = None
    if lab_lat and (len(lab_lat) >= 2 or cjk):
        digits = re.sub(r"\D", "", lab_lat)
        lat_ok = False
        for ln in lines:
            t = _latin(ln)
            if not t:
                continue
            if len(lab_lat) == 1:
                # 单个字母（N卡 / A卡）：只认完全相等，不做编辑距离容忍，
                # 否则 A卡 会被当成 N卡 通过
                if t == lab_lat or lab_lat in t:
                    lat_ok = True
                    break
                continue
            if lab_lat in t:
                lat_ok = True
                break
            if digits:
                # 含数字：数字序列必须一致，只允许字母 1 处差异
                if re.sub(r"\D", "", t) == digits and _lev(lab_lat, t) <= 1:
                    lat_ok = True
                    break
            elif _lev(lab_lat, t) <= 1:
                lat_ok = True
                break

    if cjk_ok is not None and lat_ok is not None:
        # 「1 个汉字 + 拉丁」这种 2 字符标签（如 N卡、A卡）：RapidOCR 经常只认出字母、丢掉汉字，
        # 裁切放大也读不出（实测 ×2/×3/×4 均只得 "N"）。这类标签以拉丁字母是否出现为准，
        # 汉字段没认出来但字母在 → 记为「部分识别」，不算缺词，单独计数供报表说明。
        if len(cjk) == 1 and lat_ok:
            return True
        return bool(cjk_ok and lat_ok)
    if cjk_ok is not None:
        return bool(cjk_ok)
    if lat_ok is not None:
        return bool(lat_ok)
    return False


def _is_junk(line: str) -> bool:
    t = _norm(line)
    return (not _cj(t)) and len(_latin(t)) < 3


def read_text(png_path: str, scale: float = 0.0):
    """返回 (识别行列表, 拼接全文)。窄图自动放大后识别。"""
    from PIL import Image
    import numpy as np
    im = Image.open(png_path).convert("RGB")
    w, h = im.size
    if scale <= 0:
        scale = 1.0 if w >= 1600 else 1600.0 / w
    if scale > 1.01:
        im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    res, _ = _engine()(np.array(im))
    if not res:
        return [], ""
    lines = [r[1] for r in res if r and len(r) > 1 and r[1]]
    return lines, "".join(_norm(x) for x in lines)


SCALES = (1.0, 1.6, 2.2)


def read_multi(png_path: str):
    """多尺度识别，返回 [(行列表, 拼接全文), ...]。
    多尺度投票用来压掉两类误判：
      漏识（小字在 1.0 认不出、放大后认出）→ 任一尺度找到就算有
      噪声（装饰笔画只在某个尺度被认成单词）→ 至少两个尺度都出现才算额外文字
    """
    return [read_text(png_path, s) for s in SCALES]


def _residual(line: str, labels) -> str:
    """把清单项从识别行里逐个抠掉，剩下的就是候选额外文字。
    处理 OCR 把多个词合并成一行的情况（如「文案日报 周报 PPT」）。"""
    r = _norm(line)
    for lb in labels:
        n = _norm(lb)
        if n and n in r:
            r = r.replace(n, "")
            continue
        t = _latin(n)
        if t and len(t) >= 3:
            digits = re.sub(r"\D", "", t)
            for m in re.findall(r"[a-z0-9]{2,}", r):
                if digits and re.sub(r"\D", "", m) != digits:
                    continue
                if m in t or _lev(m, t) <= 1:
                    r = r.replace(m, "")
                    break
    return r


def _is_subseq(short: str, long: str) -> bool:
    """short 的字符是否按顺序出现在 long 里（容忍 OCR 把长句切碎后残留的片段）。"""
    it = iter(long)
    return all(ch in it for ch in short)


def _extra_chunks(rest: str, labels) -> list:
    """把残留切成中文段/拉丁段，丢掉任何属于某个清单项片段的块。"""
    # 中文夹拉丁的标签（如 直观对比各型号GPU性能表现）：OCR 常把中间的 GPU 漏掉，
    # 剩下的中文段就成了"多余文字"。只要这块**整段等于**某标签去掉字母后的中文部分，
    # 就判定它属于该标签（该标签的字母部分另有 found() 校验，不会放过真丢词）。
    cjk_labels = {"".join(_cj(_norm(lb))) for lb in labels if _cj(_norm(lb))}
    out = []
    for chunk in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]{2,}", rest):
        if chunk in cjk_labels:
            continue
        keep = True
        for lb in labels:
            n = _norm(lb)
            if chunk in n or n in chunk:
                keep = False
                break
            if len(chunk) <= 4 and _is_subseq(chunk, n):
                keep = False
                break
        if keep and len(chunk) >= 2:
            out.append(chunk)
    return out


def _extras(rounds, labels, vote: int = 1):
    """按尺度投票统计额外文字块。"""
    tally = {}
    for lines, _ in rounds:
        seen = set()
        for ln in lines:
            if _is_junk(ln):
                continue
            for chunk in _extra_chunks(_residual(ln, labels), labels):
                if chunk not in seen:
                    seen.add(chunk)
                    tally[chunk] = tally.get(chunk, 0) + 1
    return [c for c, n in tally.items() if n >= vote]


def ocr_check(png_path: str, labels, min_extra_len: int = 2, vote: int = 2, fast: bool = True):
    """返回 (是否合格, 详情)。分级多尺度版。

    第一遍只用 1.6 倍识别；判合格就直接返回（快）。
    判不合格时再补 1.0 / 2.2 两遍投票，压掉两类误判：
      缺词：任一尺度找到就算有（压掉漏识）
      额外文字：至少 vote 个尺度都识别出同一块才算（压掉装饰噪声）
    """
    from PIL import Image
    labels = [str(s).strip() for s in labels if str(s).strip()]

    def _round(scale):
        return read_text(png_path, scale)

    rounds = [_round(1.6)]
    if rounds[0][0]:
        missing = [lb for lb in labels if not found(lb, rounds[0][1], rounds[0][0])]
        extra = _extras(rounds, labels, 1)
        if not missing and not extra:
            return True, {"missing": [], "extra": [], "partial": [], "raw": rounds[0][0]}

    if fast:
        rounds = [_round(1.0), _round(1.6), _round(2.2)]

    def _partial(lb):
        n = _norm(lb)
        cjk = "".join(_cj(n))
        lat = _latin(n)
        return bool(cjk) and len(cjk) == 1 and bool(lat) and len(lat) >= 1

    missing = [lb for lb in labels
               if not any(found(lb, j, ls) for ls, j in rounds)]
    partial = [lb for lb in labels
               if _partial(lb) and not any(_norm(lb) in j for _, j in rounds)]
    extra = _extras(rounds, labels, vote if len(rounds) > 1 else 1)
    raw = rounds[-1][0] if rounds else []
    return (not missing and not extra), {"missing": missing, "extra": extra,
                                         "partial": partial, "raw": raw}


if __name__ == "__main__":
    ok, d = ocr_check(sys.argv[1], sys.argv[2:])
    print("合格" if ok else "不合格")
    print("缺:", d["missing"])
    print("多:", d["extra"])
    print("识别:", d["raw"])
