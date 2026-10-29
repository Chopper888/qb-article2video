# -*- coding: utf-8 -*-
"""shot_ai：截图导演层模型链（SHOT-M4-P2 收成）。

从 M3-P3 run_pipeline_v3.py（文案链 + 高亮链）、M3-P5 run_p5.py（标注检测）、
M3-P6 gen_m3p6.py（并列判定 v2）、M3-P3 build_timeline.py（direction 组装）逐字抽取。

契约：
  - 本模块不直接发 HTTP；模型调用一律经调用方注入的 text_call / vision_call 完成。
  - text_call(prompt, **params) -> str|None（文本调用，失败返回 None）
  - vision_call(image_bytes, prompt, **params) -> str|None（看图调用，失败返回 None）
  - 提示词全部来自 pipeline/prompts/（迁移时 sha256 与原文件一致），迁移后不再改。
  - 回放模式（replay 非 None）直接注入各环节最终产出，跳过拼提示词与重试；机器校验仍真实执行。
"""
import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path

from pipeline import word_align
from pipeline.layout import classify_and_layout
from pipeline.shot_director import compute_mapping_override, is_steps_group, fn_of

# ============ 常量（来源注释） ============
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-pro"
ARK_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
ARK_MODEL = "doubao-seed-2-1-pro-260915"
FPS = 30
LEAD_IN = 8

_PROMPT_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name):
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


# ============ 解析与校验（自 run_pipeline_v3.py / validate_v4.py） ============
def parse_json(content):
    """从模型返回里解析 JSON（容忍 markdown 代码块包裹）。"""
    s = (content or "").strip()
    m = re.match(r"```(?:json)?\s*\n?(.*?)\n?```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    elif s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s), None
    except Exception as e:
        return None, repr(e)


def norm_match(s):
    return re.sub(r"[\s\W]", "", s or "")


def _norm_ws(s):
    return re.sub(r"\s", "", s or "")


def _norm_ws_punct(s):
    return re.sub(r"[^\w]", "", s or "")


def _extract_ascii_digits(s):
    return re.findall(r"[A-Za-z0-9]+", s or "")


def validate(out, narration, article, points=None):
    """十一项机器校验（自 validate_v4.py V.validate，逐字迁移）。"""
    fails = []
    if not isinstance(out, dict):
        return [("V6", "输出不是 JSON 对象")]
    title = out.get("title", "")
    items = out.get("items", [])
    n_ns = _norm_ws(narration)
    a_ns = _norm_ws(article)
    n_np = _norm_ws_punct(narration)

    if title and len(title) > 14:
        fails.append(("V5", f"title 长度 {len(title)}>14"))
    if isinstance(items, list):
        for i, it in enumerate(items):
            t = it.get("text", "") if isinstance(it, dict) else ""
            if len(t) > 10:
                fails.append(("V5", f"item{i} text 长度 {len(t)}>10"))

    if isinstance(items, list):
        for i, it in enumerate(items):
            sq = _norm_ws(it.get("source_quote", "") if isinstance(it, dict) else "")
            if not sq:
                fails.append(("V1", f"item{i} source_quote 为空"))
            elif sq not in n_ns and sq not in a_ns:
                fails.append(("V1", f"item{i} source_quote 未在口播/原文中逐字出现"))

    if isinstance(items, list):
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                fails.append(("V2a", f"item{i} 非对象"))
                continue
            t = it.get("text", "")
            sq = it.get("source_quote", "")
            sq_chars = set(sq)
            for ch in t:
                if not ch.isspace() and ch not in sq_chars:
                    fails.append(("V2a", f"item{i} text 字 {ch!r} 不在 source_quote"))
                    break

    for tok in _extract_ascii_digits(title):
        if tok not in narration and tok not in article:
            fails.append(("V2b", f"title 的 ASCII/数字 {tok!r} 不在口播/原文"))

    positions = []
    if isinstance(items, list):
        for i, it in enumerate(items):
            ap = _norm_ws_punct(it.get("anchor_phrase", "") if isinstance(it, dict) else "")
            if ap not in n_np:
                fails.append(("V3", f"item{i} anchor_phrase 未在口播中出现"))
                positions.append(-1)
            else:
                positions.append(n_np.find(ap))
        if len(positions) >= 2 and all(p != -1 for p in positions) and positions != sorted(positions):
            fails.append(("V4", f"锚点首次出现位置 {positions} 非递增"))

    if isinstance(items, list):
        texts = [it.get("text", "") if isinstance(it, dict) else "" for it in items]
        for i in range(len(texts)):
            for j in range(len(texts)):
                if i != j and texts[i] and texts[j] and texts[i] in texts[j]:
                    fails.append(("V7", f"text{i} '{texts[i]}' 是 text{j} '{texts[j]}' 的子串"))
                    break
            else:
                continue
            break

    if points is not None:
        pids = [p.get("id") for p in points if isinstance(p, dict)]
        iids = [it.get("point_id", "") if isinstance(it, dict) else "" for it in items]
        if len(items) != len(pids):
            fails.append(("V8", f"items 数 {len(items)} != 信息点数 {len(pids)}"))
        elif set(iids) != set(pids):
            fails.append(("V8", f"point_id 不对应：items={iids} vs points={pids}"))

    if len(positions) >= 2 and all(p != -1 for p in positions):
        for i in range(len(positions) - 1):
            gap = positions[i + 1] - positions[i]
            if gap < 6:
                fails.append(("V10", f"锚点 {i}→{i+1} 位置差 {gap} < 6"))

    if points is not None:
        n = len(points)
        if n == 1 and title != "":
            fails.append(("V11", f"信息点=1 时 title 必须为空，实为 {title!r}"))
        elif n >= 2 and title == "":
            fails.append(("V11", "信息点≥2 时 title 必须非空"))

    return fails


# ============ provider：真实实现（可注入，不直接发 HTTP 的入口由调用方注入） ============
def _reg_key(name):
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ)
        v, _ = winreg.QueryValueEx(k, name)
        if v:
            return v
    except Exception:
        pass
    v = os.environ.get(name)
    if v:
        return v
    raise RuntimeError("NO_" + name)


def make_real_text_call(api_key=None):
    """真实 DeepSeek 文本调用。返回 text_call(prompt, max_tokens=4096) -> str|None。

    api_key：可选，显式传入时直接使用（引擎侧只读环境变量）；不传时走 `_reg_key` 旧兜底。
    """
    import requests
    calls = []

    def text_call(prompt, max_tokens=4096):
        key = api_key or _reg_key("DEEPSEEK_API_KEY")
        body = {"model": DEEPSEEK_MODEL, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "stream": False, "thinking": {"type": "disabled"}}
        try:
            r = requests.post(DEEPSEEK_URL, headers={"Authorization": f"Bearer {key}",
                                                     "Content-Type": "application/json"},
                              json=body, timeout=180)
            if r.status_code != 200:
                # ST3-P22：记下状态码和返回正文开头，引擎据此分辨密钥错误 / 余额不足（返回值不变，仍是 None）
                calls.append({"api": "deepseek", "error": f"HTTP {r.status_code}",
                              "status": r.status_code, "body": (r.text or "")[:300]})
                return None
            data = r.json()
            u = data.get("usage", {})
            calls.append({"api": "deepseek", "prompt": u.get("prompt_tokens"),
                          "completion": u.get("completion_tokens"), "total": u.get("total_tokens")})
            return data["choices"][0]["message"].get("content")
        except Exception as e:
            calls.append({"api": "deepseek", "error": type(e).__name__})
            return None

    text_call.calls = calls
    return text_call


def make_real_vision_call(api_key=None):
    """真实方舟看图调用。返回 vision_call(image_bytes, prompt, max_tokens=512, timeout=60) -> str|None。

    api_key：可选，显式传入时直接使用（引擎侧只读环境变量）；不传时走 `_reg_key` 旧兜底。
    """
    import requests
    calls = []

    def vision_call(image_bytes, prompt, max_tokens=512, timeout=60):
        key = api_key or _reg_key("ARK_API_KEY")
        b64 = base64.b64encode(image_bytes).decode("ascii")
        body = {"model": ARK_MODEL, "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}],
            "max_tokens": max_tokens, "stream": False, "thinking": {"type": "disabled"}}
        try:
            r = requests.post(ARK_URL, headers={"Authorization": f"Bearer {key}",
                                                "Content-Type": "application/json"},
                              json=body, timeout=timeout)
            if r.status_code != 200:
                # ST3-P22：记下状态码和返回正文开头，引擎据此分辨密钥错误 / 余额不足（返回值不变，仍是 None）
                calls.append({"api": "ark", "error": f"HTTP {r.status_code}",
                              "status": r.status_code, "body": (r.text or "")[:300]})
                return None
            data = r.json()
            u = data.get("usage", {})
            calls.append({"api": "ark", "prompt": u.get("prompt_tokens"),
                          "completion": u.get("completion_tokens"), "total": u.get("total_tokens")})
            return data["choices"][0]["message"].get("content")
        except Exception as e:
            calls.append({"api": "ark", "error": type(e).__name__})
            return None

    vision_call.calls = calls
    return vision_call


# ============ 缓存（B4） ============
def _cache_key(model, prompt, params, extra=""):
    s = model + "|" + prompt + "|" + extra + "|" + json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _read_cached_content(p):
    """读旧格式缓存 `{"content": …}`。没有文件、或里面是失败结果（None / 空串）都返回 None（ST3-P23：毒缓存当没命中）。"""
    if not p.exists():
        return None
    content = json.loads(p.read_text(encoding="utf-8")).get("content")
    if content is None or (isinstance(content, str) and not content.strip()):
        return None
    return content


def _write_cached_content(p, content):
    """失败结果（None / 空串）不写缓存（ST3-P23 §3.2）。"""
    if content is None or (isinstance(content, str) and not content.strip()):
        return
    p.write_text(json.dumps({"content": content}, ensure_ascii=False), encoding="utf-8")


def make_cached_text_call(text_call, cache_dir, model=DEEPSEEK_MODEL):
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    stat = {"calls": 0, "hits": 0}

    def cached(prompt, max_tokens=4096):
        key = _cache_key(model, prompt, {"max_tokens": max_tokens})
        p = cache_dir / (key + ".json")
        hit = _read_cached_content(p)
        if hit is not None:
            stat["hits"] += 1
            return hit
        stat["calls"] += 1
        content = text_call(prompt, max_tokens=max_tokens)
        _write_cached_content(p, content)
        return content

    cached.stat = stat
    return cached


def make_cached_vision_call(vision_call, cache_dir, model=ARK_MODEL):
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    stat = {"calls": 0, "hits": 0}

    def cached(image_bytes, prompt, max_tokens=512, timeout=60):
        extra = hashlib.sha256(image_bytes).hexdigest()
        key = _cache_key(model, prompt, {"max_tokens": max_tokens}, extra=extra)
        p = cache_dir / (key + ".json")
        hit = _read_cached_content(p)
        if hit is not None:
            stat["hits"] += 1
            return hit
        stat["calls"] += 1
        content = vision_call(image_bytes, prompt, max_tokens=max_tokens, timeout=timeout)
        _write_cached_content(p, content)
        return content

    cached.stat = stat
    return cached


# ============ 文案链 ============
def _chain_dissect(groups, text_call, cache_dir):
    tpl_cd = _load_prompt("拆点.txt")
    A = {}
    for g in groups:
        gid = g["group_id"]
        base = tpl_cd.replace("{{口播原文}}", g["narration"]).replace("{{文章正文原文}}", g["article"])
        points = None
        retries = 0
        for attempt in range(1, 4):
            content = text_call(base)
            if content is None:
                if retries < 2:
                    retries += 1
                    continue
                break
            obj, perr = parse_json(content)
            if obj is None:
                if retries < 2:
                    retries += 1
                    continue
                break
            pts = obj.get("points", []) if isinstance(obj, dict) else []
            ok = isinstance(pts, list) and 1 <= len(pts) <= 3
            if ok:
                for p in pts:
                    q = re.sub(r"\s", "", p.get("quote", ""))
                    if not q or q not in re.sub(r"\s", "", g["narration"]):
                        ok = False
                        break
            if ok:
                points = pts
                break
            if retries < 2:
                retries += 1
                continue
            break
        A[gid] = {"points": points, "retries": retries}
    return A


def _chain_writing(groups, A, heading_by_fn, text_call, cache_dir):
    tpl_w = _load_prompt("写作.txt")
    BC = {}
    for g in groups:
        gid = g["group_id"]
        points = A[gid]["points"]
        if not points:
            BC[gid] = {"output": None, "fails": [("拆点失败", "")], "retries": 0, "passed": False}
            continue
        eyebrow, parent = heading_by_fn.get(g["images"][0], ("", ""))
        base = (tpl_w.replace("{{眉标}}", eyebrow).replace("{{上一级标题}}", parent or "")
                .replace("{{口播原文}}", g["narration"]).replace("{{文章正文原文}}", g["article"])
                .replace("{{信息点}}", json.dumps(A[gid], ensure_ascii=False)))
        out = None
        retries = 0
        fails = []
        cur = base
        for attempt in range(1, 4):
            content = text_call(cur)
            if content is None:
                if retries < 2:
                    retries += 1
                    cur = base + "\n\n（上一次调用失败。请重新只输出 JSON。）"
                    continue
                break
            obj, perr = parse_json(content)
            if obj is None:
                if retries < 2:
                    retries += 1
                    cur = base + "\n\n（上一次不是合法 JSON，请重新只输出 JSON。）"
                    continue
                fails = [("V6", f"JSON 解析失败 {perr}")]
                break
            fails = validate(obj, g["narration"], g["article"], points)
            if not fails:
                out = obj
                break
            if retries < 2:
                retries += 1
                reason = "；".join(f"{c}:{x}" for c, x in fails)
                cur = base + f"\n\n（上一次输出校验未通过，具体原因：{reason}。请严格按规则修正后只输出 JSON。）"
                continue
            break
        BC[gid] = {"output": out, "fails": fails, "retries": retries, "passed": not fails}
    return BC


def _chain_review(groups, A, BC, heading_by_fn, text_call, cache_dir):
    tpl_rv = _load_prompt("复核.txt")
    tpl_w = _load_prompt("写作.txt")
    D = {}
    for g in groups:
        gid = g["group_id"]
        if not BC[gid]["passed"]:
            D[gid] = {"verdict": "skip", "note": "B 未通过"}
            continue
        points = A[gid]["points"]
        out = BC[gid]["output"]
        base = (tpl_rv.replace("{{口播原文}}", g["narration"])
                .replace("{{信息点}}", json.dumps(A[gid], ensure_ascii=False))
                .replace("{{title}}", out.get("title", ""))
                .replace("{{items}}", json.dumps(out.get("items", []), ensure_ascii=False)))
        content = text_call(base)
        review = None
        if content is not None:
            review, _ = parse_json(content)
        if review is None:
            D[gid] = {"verdict": "error", "note": "解析失败"}
        elif review.get("verdict") == "pass":
            D[gid] = {"verdict": "pass", "review": review}
        else:
            eyebrow, parent = heading_by_fn.get(g["images"][0], ("", ""))
            regen = (tpl_w.replace("{{眉标}}", eyebrow).replace("{{上一级标题}}", parent or "")
                     .replace("{{口播原文}}", g["narration"]).replace("{{文章正文原文}}", g["article"])
                     .replace("{{信息点}}", json.dumps(A[gid], ensure_ascii=False))
                     + "\n\n（复核意见：" + json.dumps(review, ensure_ascii=False) + "。请按意见修正后只输出 JSON。）")
            content2 = text_call(regen)
            obj2 = None
            if content2 is not None:
                obj2, _ = parse_json(content2)
            if obj2 is not None and not validate(obj2, g["narration"], g["article"], points):
                BC[gid] = {"output": obj2, "fails": [], "retries": BC[gid]["retries"], "passed": True, "regenerated": True}
                D[gid] = {"verdict": "pass_after_regen", "review": review}
            else:
                D[gid] = {"verdict": "fail", "review": review, "note": "重生成仍不合格"}
    return D, BC


# ============ 高亮链 ============
def _chain_highlight_decision(groups, images, BC, heading_by_fn, g_narr, g_art, text_call, cache_dir):
    tpl_hl = _load_prompt("高亮目标.txt")
    group_headings = {}
    for g in groups:
        group_headings[g["group_id"]] = [heading_by_fn[fn][0] for fn in g["images"]]
    E = []
    for img in images:
        fn = img["filename"]
        gid = img["group_id"]
        decision = {"filename": fn, "group_id": gid, "need_highlight": False, "reasons": []}
        if not BC[gid]["passed"]:
            decision["reasons"].append("skip_sidebar_failed")
            E.append(decision)
            continue
        kind = classify_and_layout(img["w"], img["h"])["kind"]
        decision["kind"] = kind
        if kind == "A":
            decision["reasons"].append("skip_a_type")
            E.append(decision)
            continue
        hs = group_headings.get(gid, [])
        first_h = hs[0] if hs else None
        mismatch = (img["nearest_heading"] != first_h) if first_h else False
        if (img["confidence"] is not None and img["confidence"] < 0.6) or mismatch:
            decision["reasons"].append("skip_low_confidence")
            E.append(decision)
            continue
        bc = BC[gid]
        sidebar_items = json.dumps(bc.get("output", {}).get("items", []), ensure_ascii=False) if bc.get("output") else "[]"
        base = (tpl_hl.replace("{{口播原文}}", g_narr[gid]).replace("{{文章正文原文}}", g_art[gid])
                .replace("{{侧栏条目}}", sidebar_items))
        content = text_call(base)
        obj = None
        if content is not None:
            obj, _ = parse_json(content)
        if obj is None:
            decision["reasons"].append("target_judge_failed")
        elif obj.get("need_highlight") is True and obj.get("target"):
            decision["need_highlight"] = True
            decision["target"] = obj.get("target")
            decision["anchor_item_id"] = obj.get("anchor_item_id")
            decision["reason"] = obj.get("reason")
        else:
            decision["reason"] = obj.get("reason", "")
        E.append(decision)
    return E


def _chain_locate_readback(E, images, image_dir, vision_call, cache_dir, crop_dir):
    tpl_loc = _load_prompt("定位.txt")
    tpl_rb = _load_prompt("回读.txt")
    from PIL import Image as PILImage
    crop_dir = Path(crop_dir)
    crop_dir.mkdir(parents=True, exist_ok=True)
    F = []
    for d in E:
        if not d["need_highlight"]:
            continue
        fn = d["filename"]
        img_path = Path(image_dir) / fn
        w, h = PILImage.open(img_path).size
        target = d["target"]
        located = False
        box = None
        reason = ""
        for attempt in range(1, 3):
            content = vision_call(img_path.read_bytes(),
                                  tpl_loc.replace("{目标描述}", target + "这一整行 / 这个控件"),
                                  max_tokens=512, timeout=60)
            if content is None:
                reason = "调用失败"
                continue
            m = re.search(r"FOUND\s*\|\s*<bbox>\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*</bbox>", content or "")
            if not m:
                reason = f"返回 {(content or '').strip()[:40]}"
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            bx, by = round(x1 * w / 1000), round(y1 * h / 1000)
            bw, bh = round((x2 - x1) * w / 1000), round((y2 - y1) * h / 1000)
            in_img = 0 <= bx and 0 <= by and bx + bw <= w and by + bh <= h
            area = bw * bh
            area_ok = area <= 0.5 * w * h and area >= 0.0005 * w * h
            size_ok = bw >= 8 and bh >= 8
            if in_img and area_ok and size_ok:
                located = True
                box = {"x": bx, "y": by, "width": bw, "height": bh}
                break
            reason = f"几何不合格 box={[bx, by, bw, bh]}"
        if not located:
            F.append({"filename": fn, "target": target, "failed_reason": reason})
            continue
        pad = 10
        im = PILImage.open(img_path)
        crop = im.crop((max(0, box["x"] - pad), max(0, box["y"] - pad),
                        min(w, box["x"] + box["width"] + pad), min(h, box["y"] + box["height"] + pad)))
        crop_path = crop_dir / f"{fn}-crop.png"
        crop.save(crop_path)
        content = vision_call(crop_path.read_bytes(), tpl_rb, max_tokens=512, timeout=60)
        raw = (content or "").strip() if content is not None else ""
        m = re.match(r"NAME\s*\|\s*(.+)", raw)
        answer = m.group(1).strip() if m else ""
        matched = False
        if answer:
            t = norm_match(target)
            a = norm_match(answer)
            if t and a and (t in a or a in t):
                matched = True
        if matched:
            F.append({"filename": fn, "target": target, "box": box, "readback": "match",
                      "answer": answer, "crop": str(crop_path)})
        else:
            F.append({"filename": fn, "target": target, "box": box, "readback": "mismatch",
                      "answer": answer, "failed_reason": "skip_readback_mismatch", "crop": str(crop_path)})
    return F


# ============ 标注检测（自 run_p5.py ark_annotate） ============
def _chain_annotate(fns, image_dir, vision_call, cache_dir):
    tpl_ann = _load_prompt("标注检测.txt")
    ann = {}
    for fn in fns:
        p = Path(image_dir) / fn
        content = vision_call(p.read_bytes(), tpl_ann, max_tokens=64, timeout=60)
        ann[fn] = (content or "").strip() if content is not None else ""
    return ann


# ============ 并列判定 v2（自 gen_m3p6.py） ============
def _chain_parallel(multi_groups, g_narr, text_call, cache_dir):
    tpl_par = _load_prompt("并列判定.txt")
    parallel_v2_raw = {}
    manual_list = []
    for gid, fns in multi_groups.items():
        n = len(fns)
        narration = g_narr.get(gid, "")
        prompt = tpl_par.replace("{n}", str(n)).replace("{narration}", narration)
        obj = None
        for attempt in range(2):
            content = text_call(prompt, max_tokens=1024)
            if content is None:
                continue
            o, err = parse_json(content)
            if o is None:
                continue
            if not isinstance(o.get("parallel"), bool):
                continue
            labels = o.get("labels", [])
            if o["parallel"]:
                if not (isinstance(labels, list) and len(labels) == n):
                    continue
                if not all(isinstance(lb, str) and len(lb) <= 8 and all(ch in narration for ch in lb) for lb in labels):
                    continue
            obj = o
            break
        if obj is None:
            parallel_v2_raw[gid] = {"parallel": False, "labels": [], "reason": "judge_failed"}
            manual_list.append({"gid": gid, "reason": "并列判定失败，兜底为不并列"})
        else:
            parallel_v2_raw[gid] = obj
    return parallel_v2_raw, manual_list


# ============ 文字密度判定（SHOT-M5-P2） ============
def _chain_density(images, image_dir, vision_call, cache_dir):
    """对每张截图判文字密度 dense/medium/sparse（决定网格每屏张数）。

    结果缓存（同一张图只判一次）。失败重试 1 次，仍失败按 dense（最保守）并记入人工清单。
    """
    tpl_den = _load_prompt("文字密度.txt")
    density = {}
    manual = []
    for r in images:
        fn = r["filename"]
        p = Path(image_dir) / fn
        obj = None
        for _ in range(2):
            content = vision_call(p.read_bytes(), tpl_den, max_tokens=128, timeout=60)
            if content is None:
                continue
            o, _err = parse_json(content)
            if o is None:
                continue
            d = o.get("density")
            if d in ("dense", "medium", "sparse"):
                obj = d
                break
        if obj is None:
            obj = "dense"
            manual.append({"image": fn, "reason": "文字密度判定失败，按 dense 处理"})
        density[fn] = obj
    return density, manual


# ============ 图句匹配（SHOT-M5-P3 A） ============
def _validate_match(ms, fns, narration):
    """A2 五项程序校验。返回 (fails, matches)。

    fails 为空 → 通过。任一项不过 → 整组视为无匹配（只走概述层）。
    """
    fails = []
    matches = []
    if not isinstance(ms, list) or len(ms) != len(fns):
        fails.append(("M2", f"matches 数 {len(ms) if isinstance(ms, list) else 'N/A'} != 图数 {len(fns)}"))
        return fails, matches
    anchors = {}
    for i, m in enumerate(ms):
        if not isinstance(m, dict):
            fails.append(("M2", f"match[{i}] 非对象"))
            continue
        img = m.get("image")
        sent = m.get("sentence")
        if img != fns[i]:
            fails.append(("M2", f"match[{i}] image={img!r} != 期望 {fns[i]!r}"))
        if isinstance(sent, str) and sent.strip().lower() == "null":
            sent = None  # 模型把 JSON null 当成字符串 "null" 返回
        if sent is not None:
            if not isinstance(sent, str):
                fails.append(("M2", f"match[{i}] sentence 非字符串非 null"))
                continue
            if sent not in narration:
                fails.append(("M3", f"锚句非口播连续子串: {sent!r}"))
            else:
                anchors[fns[i]] = sent
        matches.append({"image": fns[i], "sentence": sent})
    non_null = [m["sentence"] for m in matches if m["sentence"] is not None]
    if len(set(non_null)) != len(non_null):
        fails.append(("M4", "同一句被多张图使用"))
    pos = [narration.find(s) for s in non_null]
    for i in range(len(pos) - 1):
        if pos[i] >= pos[i + 1]:
            fails.append(("M5", "锚句顺序非单调递增"))
            break
    return fails, matches


def _chain_match(multi_groups, g_narr, im_by_fn, text_call, cache_dir):
    """图句匹配（SHOT-M5-P3 A）：每个多图组调一次 DeepSeek，判断"哪句口播讲哪张图"。

    multi_groups: {gid: [fns]}（有效分组，含 override + B3 短组并入）。
    g_narr: {gid: narration}。im_by_fn: {fn: image_map entry}（取 before/after 上下文）。
    返回 {gid: {"passed": bool, "matches": [{image, sentence}], "fails": [...]}}。
    """
    tpl = _load_prompt("图句匹配.txt")
    out = {}
    manual = []
    for gid, fns in multi_groups.items():
        narration = g_narr.get(gid, "")
        lines = []
        for fn in fns:
            e = im_by_fn.get(fn, {})
            lines.append(f"{fn} | 原文上文：{e.get('before', '')} | 原文下文：{e.get('after', '')}")
        images_text = "\n".join(lines)
        prompt = tpl.replace("{narration}", narration).replace("{images}", images_text)
        obj = None
        for _ in range(2):
            content = text_call(prompt, max_tokens=1024)
            if content is None:
                continue
            o, _err = parse_json(content)
            if o is None:
                continue
            obj = o
            break
        if obj is None:
            out[gid] = {"passed": False, "matches": [], "fails": [("A0", "调用失败或 JSON 解析失败")]}
            manual.append({"gid": gid, "reason": "图句匹配调用失败"})
            continue
        ms = obj.get("matches") if isinstance(obj, dict) else None
        fails, matches = _validate_match(ms, fns, narration)
        out[gid] = {"passed": len(fails) == 0, "matches": matches, "fails": fails}
        if fails:
            manual.append({"gid": gid, "reason": "校验不通过: " + ";".join(f"{c}:{x}" for c, x in fails)})
    return out, manual


# ============ 图句匹配 v2（SHOT-M5-P4 B：整章 + 一句多词） ============
def _validate_match_v2(ms, fns, narration, subsec_of=None):
    """B5 四项程序校验（v1.1 裁定）。返回 (fails, matches)。

    保留：①JSON 可解析（调用方）②每图给锚词或 null ③锚词是章口播连续子串 ④锚词 ≥3 字。
    删除原规则4（同一锚词不得被两张图使用）与规则5（同一小节内单调递增）。
    新增规则5'：同一锚词最多对应 4 张图，超过 4 张该章判不通过。
    """
    fails = []
    matches = []
    if not isinstance(ms, list) or len(ms) != len(fns):
        fails.append(("M2", f"matches 数 {len(ms) if isinstance(ms, list) else 'N/A'} != 图数 {len(fns)}"))
        return fails, matches
    for i, m in enumerate(ms):
        if not isinstance(m, dict):
            fails.append(("M2", f"match[{i}] 非对象"))
            continue
        img = m.get("image")
        anchor = m.get("anchor")
        sent = m.get("sentence")
        if img != fns[i]:
            fails.append(("M2", f"match[{i}] image={img!r} != 期望 {fns[i]!r}"))
        if isinstance(anchor, str) and anchor.strip().lower() == "null":
            anchor = None
        if isinstance(sent, str) and sent.strip().lower() == "null":
            sent = None
        if anchor is not None:
            if not isinstance(anchor, str):
                fails.append(("M2", f"match[{i}] anchor 非字符串非 null"))
                continue
            if len(anchor) < 3:
                fails.append(("M6", f"锚词长度 {len(anchor)}<3"))
            if anchor not in narration:
                fails.append(("M3", f"锚词非口播连续子串: {anchor!r}"))
        matches.append({"image": fns[i], "anchor": anchor, "sentence": sent,
                        "level": m.get("level") if isinstance(m, dict) else None})
    # 新增规则 5'：同一锚词最多对应 4 张图
    cnt = {}
    for m in matches:
        a = m["anchor"]
        if a is not None:
            cnt[a] = cnt.get(a, 0) + 1
    for a, n in cnt.items():
        if n > 4:
            fails.append(("M4", f"同一锚词 {a!r} 被 {n} 张图使用（>4）"))
    return fails, matches


def _chain_match_v2(chapters, chapter_narr, im_by_fn, subsec_of, text_call, cache_dir,
                    prompt_name="图句匹配-v2.txt"):
    """整章图句匹配 v2（SHOT-M5-P4 B）：每章一次 DeepSeek，max_tokens=4096。

    chapters: {章名: [fns]}。chapter_narr: {章名: 章全部口播}。subsec_of: {fn: 所属小节}。
    prompt_name: 提示词文件名（v2 默认；v3 传「图句匹配-v3.txt」）。
    返回 {"chapter_passed": {章名: bool}, "matches": {fn: {anchor, sentence, chapter, level}},
          "manual": [{chapter, reason}]}。
    """
    tpl = _load_prompt(prompt_name)
    out = {"chapter_passed": {}, "matches": {}, "manual": []}
    for chap, fns in chapters.items():
        narration = chapter_narr.get(chap, "")
        lines = []
        for fn in fns:
            e = im_by_fn.get(fn, {})
            lines.append(f"{fn} | 原文上文：{e.get('before', '')} | 原文下文：{e.get('after', '')}")
        images_text = "\n".join(lines)
        prompt = tpl.replace("{narration}", narration).replace("{images}", images_text)
        obj = None
        for _ in range(2):
            content = text_call(prompt, max_tokens=4096)
            if content is None:
                continue
            o, _err = parse_json(content)
            if o is None:
                continue
            obj = o
            break
        if obj is None:
            out["chapter_passed"][chap] = False
            out["manual"].append({"chapter": chap, "reason": "调用失败或 JSON 解析失败"})
            for fn in fns:
                out["matches"][fn] = {"anchor": None, "sentence": None, "chapter": chap}
            continue
        ms = obj.get("matches") if isinstance(obj, dict) else None
        fails, matches = _validate_match_v2(ms, fns, narration, subsec_of)
        passed = len(fails) == 0
        out["chapter_passed"][chap] = passed
        for m in matches:
            out["matches"][m["image"]] = {"anchor": m["anchor"], "sentence": m["sentence"],
                                         "chapter": chap, "level": m.get("level")}
        if fails:
            out["manual"].append({"chapter": chap, "reason": "校验不通过: " + ";".join(f"{c}:{x}" for c, x in fails)})
    return out


# ============ 看图定锚（SHOT-M5-P6 A：识图复核锚词） ============
def _chain_vision_anchor(fns, image_dir, chapter_groups, g_text, img_gid, vision_call, cache_dir):
    """看图定锚（SHOT-M5-P6 A）：对每张图做一次识图复核，选出真正讲图里内容的句子。

    fns: 全部截图文件名（image_map 顺序）。image_dir: 截图目录（Path）。
    chapter_groups: {章名: [group_id...]}（正文组按文章顺序）。
    g_text: {group_id: group_text}。img_gid: {fn: mapped_group_id}。
    vision_call: 缓存过的识图调用 vision_call(image_bytes, prompt, max_tokens=, timeout=)。
    返回 (out, manual)。out: {fn: {"sentence": 选中句或 None, "reason": ..., "valid": bool, "raw": 原始返回}}。
    """
    tpl = _load_prompt("看图定锚.txt")
    out = {}
    manual = []
    gid_chap = {}
    for chap, gids in chapter_groups.items():
        for gid in gids:
            gid_chap[gid] = chap
    chap_order = {chap: {gid: i for i, gid in enumerate(gids)} for chap, gids in chapter_groups.items()}

    for fn in fns:
        gid = img_gid.get(fn)
        chap = gid_chap.get(gid) if gid is not None else None
        if chap is None or chap not in chapter_groups:
            out[fn] = {"sentence": None, "reason": "无章或无组", "valid": False, "raw": ""}
            manual.append({"image": fn, "reason": "看图定锚：无章或无组，沿用文本模型锚词"})
            continue
        gids = chapter_groups[chap]
        pos = chap_order[chap].get(gid, 0)
        lo = max(0, pos - 2)
        hi = min(len(gids), pos + 3)
        cand_ids = gids[lo:hi]
        cand_texts = [g_text.get(g, "") for g in cand_ids]
        candidates = "\n".join(cand_texts)
        prompt = tpl.replace("{candidates}", candidates)
        img_path = Path(image_dir) / fn
        obj = None
        raw = ""
        for _ in range(2):
            content = vision_call(img_path.read_bytes(), prompt, max_tokens=256, timeout=60)
            if content is None:
                continue
            raw = content
            o, _err = parse_json(content)
            if o is None:
                continue
            obj = o
            break
        if obj is None:
            out[fn] = {"sentence": None, "reason": "调用失败或JSON解析失败", "valid": False, "raw": raw}
            manual.append({"image": fn, "reason": "看图定锚调用失败，沿用文本模型锚词"})
            continue
        sent = obj.get("sentence")
        reason = obj.get("reason", "")
        if isinstance(sent, str) and sent.strip().lower() == "null":
            sent = None
        valid = True
        if sent is not None:
            if not isinstance(sent, str):
                valid = False
            elif sent not in cand_texts:
                valid = False
        if not valid:
            out[fn] = {"sentence": None, "reason": f"校验不过(句子不在候选或非字符串): {reason}", "valid": False, "raw": raw}
            manual.append({"image": fn, "reason": "看图定锚校验不过，沿用文本模型锚词"})
        else:
            out[fn] = {"sentence": sent, "reason": reason, "valid": True, "raw": raw}
    return out, manual


# ============ 出场点判定 v1.1（SHOT-M5-P8 B2：识图只判定讲解点，候选句限定在小节） ============
def _chain_appearance_points(fns, image_dir, subsec_groups, g_text, img_subsec, vision_call, cache_dir):
    """出场点判定 v1.1（SHOT-M5-P8 B2）：每图识图判定讲解点，候选句限定在该图所属小节。

    fns: 全部截图文件名（image_map 顺序）。image_dir: 截图目录（Path）。
    subsec_groups: {小节名: [group_id...]}（该小节下的正文组，按文章顺序）。
    g_text: {group_id: group_text}。img_subsec: {fn: 小节名}。
    vision_call: 缓存过的识图调用 vision_call(image_bytes, prompt, max_tokens=, timeout=)。
    返回 (out, manual)。out: {fn: {"explain": [{"id","keyword"}], "reason", "valid"}}。
    """
    tpl = _load_prompt("出场点判定.txt")
    out = {}
    manual = []
    for fn in fns:
        sub = img_subsec.get(fn)
        gids = subsec_groups.get(sub) if sub else None
        if not gids:
            out[fn] = {"explain": [], "reason": "无小节或无句", "valid": False}
            manual.append({"image": fn, "reason": "出场点判定：无小节或无句"})
            continue
        sentences_lines = "\n".join(f"{i + 1}: {g_text.get(g, '')}" for i, g in enumerate(gids))
        prompt = tpl.replace("{sentences}", sentences_lines)
        img_path = Path(image_dir) / fn
        obj = None
        for _ in range(2):
            content = vision_call(img_path.read_bytes(), prompt, max_tokens=1024, timeout=90)
            if content is None:
                continue
            o, _err = parse_json(content)
            if o is None:
                continue
            obj = o
            break
        if obj is None:
            out[fn] = {"explain": [], "reason": "调用失败或JSON解析失败", "valid": False}
            manual.append({"image": fn, "reason": "出场点判定调用失败"})
            continue
        explain = obj.get("explain") or []
        reason = obj.get("reason", "")
        valid = True
        if isinstance(explain, list):
            for e in explain:
                if not isinstance(e, dict):
                    valid = False
                    break
                eid = e.get("id")
                kw = e.get("keyword")
                if not isinstance(eid, int) or not (1 <= eid <= len(gids)):
                    valid = False
                    break
                sent = g_text.get(gids[eid - 1], "")
                if not isinstance(kw, str) or kw not in sent or len(kw) < 3:
                    valid = False
                    break
        else:
            valid = False
        out[fn] = {"explain": explain, "reason": reason, "valid": valid}
        if not valid:
            manual.append({"image": fn, "reason": "出场点判定校验不过"})
    return out, manual


# ============ 截图标签（SHOT-M5-P4 D） ============
def _label_width(s):
    """标签宽度：汉字=1，ASCII 字母/数字=0.5（ST3-P3d 第5项放宽口径）。"""
    return sum(0.5 if (ch.isascii() and ch.isalnum()) else 1.0 for ch in s)


def _truncate_label(s, max_w=16.0):
    """按宽度截断标签到 max_w（汉字=1，ASCII 字母数字=0.5）。"""
    w = 0.0
    out = []
    for ch in s:
        cw = 0.5 if (ch.isascii() and ch.isalnum()) else 1.0
        if w + cw > max_w:
            break
        out.append(ch)
        w += cw
    return "".join(out)


def _chain_label(fns, g_narr, im_by_fn, text_call, cache_dir):
    """为截图生成 ≤16 字宽标签（ST3-P3d 第5项：放宽到 16 字宽、取消「不得造词」）。返回 (labels, manual)。"""
    tpl = _load_prompt("截图标签.txt")
    labels = {}
    manual = []
    for fn in fns:
        e = im_by_fn.get(fn, {})
        context = f"原文上文：{e.get('before', '')} 原文下文：{e.get('after', '')}"
        narration = g_narr.get(e.get("mapped_group_id", ""), "")
        prompt = tpl.replace("{context}", context).replace("{narration}", narration)
        obj = None
        for _ in range(2):
            content = text_call(prompt, max_tokens=128)
            if content is None:
                continue
            o, _ = parse_json(content)
            if o is None:
                continue
            obj = o
            break
        label = None
        if obj is not None and isinstance(obj, dict):
            lb = obj.get("label")
            if isinstance(lb, str) and lb.strip().lower() != "null":
                label = lb.strip()
        if label is not None:
            label = _truncate_label(label)
            # ST3-P3d 第5项：取消「不得造词」，只要求至少一个 CJK 关键词在原文里能找到。
            cjk = [ch for ch in label if "\u4e00" <= ch <= "\u9fff"]
            pool = context + narration
            if cjk and not any(ch in pool for ch in cjk):
                manual.append({"image": fn, "reason": f"关键词不在原文: {label!r}"})
                label = None
        if not label:
            # ST3-P3d 第5项兜底：原文 before 截断到 16 字宽。
            label = _truncate_label(e.get("before", "") or "")
        labels[fn] = label or ""
    return labels, manual


# ============ direction 组装（自 build_timeline.py） ============
def _build_direction(im, gr, sent, BC, F):
    body = [g for g in gr["groups"] if not g.get("starts_with_bracket_title")]
    groups = [{"group_id": g["group_id"], "text": g["group_text"]} for g in body]
    align = word_align.align_groups(sent, groups)
    g_win = {}
    for r in align:
        g_win[r["group_id"]] = (round(r["start_time"] * FPS), round(r["end_time"] * FPS))

    flow = []
    for s in sent:
        for w in s.get("words", []):
            fr = round(w.get("startTime", 0) * FPS)
            for ch in w.get("word", ""):
                flow.append((ch, fr))
    norm_flow = [(c, f) for c, f in flow if re.match(r"[\w]", c)]
    norm_text = "".join(c for c, _ in norm_flow)

    def find_anchor_frame(anchor):
        a = re.sub(r"[^\w]", "", anchor or "")
        if not a:
            return None
        idx = norm_text.find(a)
        if idx < 0:
            return None
        return norm_flow[idx][1]

    g_imgs = {}
    for e in im["images"]:
        fn = fn_of(e)
        g_imgs.setdefault(e["mapped_group_id"], []).append({"filename": fn, "image_path": e["image_path"]})

    direction = []
    shot_idx = 0
    for e in im["images"]:
        fn = fn_of(e)
        gid = e["mapped_group_id"]
        w, h = e["_w"], e["_h"]
        ws, we = g_win.get(gid, (0, 0))
        group_imgs = g_imgs[gid]
        n = len(group_imgs)
        pos_in_group = next(i for i, x in enumerate(group_imgs) if x["filename"] == fn)
        seg = (we - ws) / n
        c_start = round(ws + pos_in_group * seg)
        c_end = round(ws + (pos_in_group + 1) * seg)
        duration = max(1, c_end - c_start)
        bcout = BC[gid]["output"]
        sidebar = None
        if bcout:
            items = []
            for it in bcout.get("items", []):
                af = find_anchor_frame(it.get("anchor_phrase", ""))
                rec = {"text": it.get("text", ""), "anchor": it.get("anchor_phrase", ""),
                       "source_quote": it.get("source_quote", ""), "point_id": it.get("point_id", "")}
                if af is None:
                    rec["at_frame"] = None
                else:
                    rel = af - c_start
                    if rel < 0:
                        rel = LEAD_IN
                        rec["clamped"] = True
                    elif rel > duration:
                        rel = duration - LEAD_IN
                        rec["clamped"] = True
                    rec["at_frame"] = rel
                items.append(rec)
            sidebar = {"eyebrow": e["nearest_heading"].replace("\\", ""), "title": bcout.get("title", ""), "items": items}
        highlights = []
        fr = next((x for x in F if x["filename"] == fn and x.get("readback") == "match"), None)
        if fr and "box" in fr:
            af = find_anchor_frame(fr.get("target", ""))
            at_frame = None
            if af is not None:
                at_frame = max(0, af - c_start)
            highlights.append({"coord_space": "source_image_px", "box": fr["box"], "anchor": fr.get("target", ""),
                               "at_frame": at_frame, "linked_item": fr.get("anchor_item_id", "")})
        layout = classify_and_layout(w, h)
        direction.append({"shot_index": shot_idx, "mapped_group_id": gid, "image_ref": fn,
                          "source_relative_path": _unquote(e["image_path"]),
                          "source_image": {"width": w, "height": h},
                          "timing": {"start_frame": c_start, "duration_frames": duration, "lead_in": LEAD_IN,
                                     "end_frame": c_end},
                          "layout": {"kind": layout.get("kind"), "card_rect": layout.get("card_rect"),
                                     "image_rect": layout.get("image_rect"), "side_rect": layout.get("side_rect"),
                                     "text_zones": layout.get("text_zones")},
                          "sidebar": sidebar, "highlights": highlights, "review_flags": []})
        shot_idx += 1
    return direction


def _unquote(p):
    from urllib.parse import unquote
    return unquote(p)


# ============ 总入口（B5） ============
def run_director(inputs, text_call, vision_call, cache_dir, replay=None):
    """导演层模型链总入口。

    inputs: dict，需含
      groups（12 组 {group_id, narration, article, images}）
      image_map（image_map.json）
      groups_raw（groups_raw.json）
      sentences（m3p3_sentences.json）
      image_dir（图片和附件目录）
    text_call/vision_call: 模型调用（可注入真实或回放 provider）。
    cache_dir: 缓存目录（不写死，由调用方指定）。
    replay: 可选，回放各环节最终产出 dict = {"A","BC","F","annotations","parallel_v2"}，
      非 None 时跳过模型调用与重试，直接用注入结果。

    返回 {"direction","bc","f","parallel_v2","annotations"}，交给 shot_director.build_shots。
    """
    from PIL import Image as PILImage
    im = inputs["image_map"]
    gr = inputs["groups_raw"]
    sent = inputs["sentences"]
    groups = inputs["groups"]
    image_dir = inputs["image_dir"]

    # 图片基础信息（自 run_pipeline_v3.py load_images）
    article_lines = inputs.get("article_lines")
    headings = []
    if article_lines:
        for i, ln in enumerate(article_lines):
            m = re.match(r"^(#{1,6})\s+(.*)$", ln)
            if m:
                headings.append((i, len(m.group(1)), m.group(2).replace("\\", "").strip()))

    def parent(nearest):
        tlvl = tline = None
        for i, lvl, txt in headings:
            if txt == nearest.replace("\\", ""):
                tlvl, tline = lvl, i
                break
        if tlvl is None:
            return None
        p = None
        for i, lvl, txt in headings:
            if i < tline and lvl < tlvl:
                p = txt
        return p

    images = []
    for e in im["images"]:
        fn = fn_of(e)
        nearest = e["nearest_heading"].replace("\\", "")
        w, h = PILImage.open(Path(image_dir) / fn).size
        e["_w"] = w
        e["_h"] = h
        images.append({"filename": fn, "line": e["line"], "group_id": e["mapped_group_id"],
                       "nearest_heading": nearest, "parent_heading": parent(nearest),
                       "confidence": e.get("confidence"), "w": w, "h": h})
    heading_by_fn = {r["filename"]: (r["nearest_heading"], r["parent_heading"]) for r in images}
    g_narr = {g["group_id"]: g["narration"] for g in groups}
    g_art = {g["group_id"]: g["article"] for g in groups}

    if replay is not None:
        A = replay["A"]
        BC = replay["BC"]
        F = replay["F"]
        annotations = replay["annotations"]
        parallel_v2_raw = replay["parallel_v2"]
        parallel_manual = []
        density = replay.get("density", {})
        density_manual = []
    else:
        A = _chain_dissect(groups, text_call, cache_dir)
        BC = _chain_writing(groups, A, heading_by_fn, text_call, cache_dir)
        D, BC = _chain_review(groups, A, BC, heading_by_fn, text_call, cache_dir)
        E = _chain_highlight_decision(groups, images, BC, heading_by_fn, g_narr, g_art, text_call, cache_dir)
        crop_dir = Path(cache_dir) / "crops"
        F = _chain_locate_readback(E, images, image_dir, vision_call, cache_dir, crop_dir)
        hl_fns = [x["filename"] for x in F if x.get("readback") == "match"]
        # A4: 停止调用标注检测（代码保留，不删）；本轮方舟 0 次。annotations 置空。
        annotations = {}

        # 并列判定：多图组（override 后）
        body = [g for g in gr["groups"] if not g.get("starts_with_bracket_title")]
        override, _ = compute_mapping_override(im, body)
        g_imgs_eff = {}
        for e in im["images"]:
            fn = fn_of(e)
            gid_eff = override.get(fn, {}).get("to_group", e["mapped_group_id"])
            g_imgs_eff.setdefault(gid_eff, []).append(fn)
        multi_groups = {gid: fns for gid, fns in g_imgs_eff.items() if len(fns) >= 2}
        parallel_v2_raw, parallel_manual = _chain_parallel(multi_groups, g_narr, text_call, cache_dir)
        density, density_manual = _chain_density(images, image_dir, vision_call, cache_dir)

    direction = _build_direction(im, gr, sent, BC, F)
    return {"direction": direction, "bc": BC, "f": F,
            "parallel_v2": parallel_v2_raw, "annotations": annotations,
            "parallel_manual": parallel_manual,
            "density": density, "density_manual": density_manual}
