"""S1-R4 prototype: automatic semantic annotation (group -> classify -> evidence check).

Inputs:
  tts_input_segments.json   (from splitter.py)
  narration/narration_candidate.md
  input/source_article.md   (used ONLY for mechanical image lookup)
  criteria.md               (judgment standard, human-readable)

Outputs:
  annotations.json  (aligned to M8-P3-B-SEMANTIC-ROLE-SCHEMA.json)
  review_report.md  (human-readable review report with blank score columns)

Deliberately NOT wired into any production code. Rules are deterministic string
matching per criteria.md. This is a prototype to be judged by a human.
"""
from __future__ import annotations
import re

# 注意（ST1-P5b 乙）：本副本移除了原型导入时的副作用——原来模块顶层会读取
# tts_input_segments.json / narration_candidate.md / source_article.md（原型样本数据），
# 引擎只用 group_segments / group_text / classify_group 等纯函数，不需要这些样本，
# 故删掉顶层读取，分组结果不变。

# --------------------------------------------------------------------------
# 1. Grouping: base = paragraph; subdivide at mid-paragraph discourse openers.
# --------------------------------------------------------------------------
SUBDIVIDE_OPENERS = (
    "不过", "但是", "但需要注意", "需要提醒", "这里要注意", "另外", "补充一下",
    "区别在于", "这里也有一个", "不过这里", "不过需要", "需要你自己",
)

def _starts_with_ordinal(text):
    return bool(RE_ORDINAL_START.match(text.strip()))


def _emit(groups, gid, pi, current):
    groups.append({"gid": f"G{gid:02d}", "paragraph_index": pi,
                   "segment_ids": [x["segment_id"] for x in current]})
    return gid + 1


def group_segments(segments):
    groups = []                      # {gid, paragraph_index, segment_ids}
    by_para = {}
    for s in segments:
        by_para.setdefault(s["paragraph_index"], []).append(s)
    gid = 0
    for pi in sorted(by_para):
        segs = by_para[pi]
        # 段落内若 ≥2 个句首序数锚点,按序数锚点二次切分:总起 + 各项
        ordinal_starts = [i for i, s in enumerate(segs) if _starts_with_ordinal(s["source_text"])]
        if len(ordinal_starts) >= 2:
            current = []
            for s in segs:
                if current and _starts_with_ordinal(s["source_text"]):
                    gid = _emit(groups, gid, pi, current)
                    current = []
                current.append(s)
            gid = _emit(groups, gid, pi, current)
            continue
        # 原有:按语篇标记(不过/但是/另外…)细分
        current = []
        for s in segs:
            if current and any(s["source_text"].startswith(op) for op in SUBDIVIDE_OPENERS):
                gid = _emit(groups, gid, pi, current)
                current = []
            current.append(s)
        gid = _emit(groups, gid, pi, current)
    return groups

def group_text(segments, g):
    ids = g["segment_ids"]
    return "".join(x["source_text"] for x in segments if x["segment_id"] in ids)

# --------------------------------------------------------------------------
# 2. Classification rules (per criteria.md)
# --------------------------------------------------------------------------
RE_EVIDENCE = re.compile(r"(这张图|这张图片|可以看到|界面上|页面|如图|截图|界面|菜单|按钮|报错|出现.*错误|操作失败|这个界面|这个页面|下面的图|上图|图中|对话框)")
# 「不过」需是独立转折词:前面不紧贴汉字,排除「刷不过/说不过/打不过」这类 V+不过(不能…)
RE_CONSTRAINT = re.compile(
    r"(需要注意|注意[:：]|只能.*不能|并不能|但这里|但有一个|"
    r"(?<![\u4e00-\u9fa5])不过"
    r"|有概率|有个限定|前提|限定条件|风险)")
RE_COMPARISON = re.compile(r"(两个选择|两种方式|两种方法|一个是.*另一个是|既可以.*也可以|要么.*要么)")
RE_ORDINAL = re.compile(
    r"(第[一二三四五六七八九十\d]+[道个种类条步]"   # 第X道/第X个/第X种/第X类/第X条/第X步
    r"|第[一二三四五六七八九十\d]+[,，]"              # 裸序数 第一，/第二，(无量词直接跟逗号)
    r"|[一二三四五六七八九十]+是)")                   # 一是/二是/三是…(序数+是)

# 句首序数锚点:用于 group_segments 的段落内二次切分(锚点须在句首才能切)
RE_ORDINAL_START = re.compile(
    r"^(?:第[一二三四五六七八九十\d]+[道个种类条步]"   # 第X道/个/种/类/条/步 在句首
    r"|第[一二三四五六七八九十\d]+[,，]"                # 第一，/第二， 在句首
    r"|[一二三四五六七八九十]+是)")                     # 一是/二是 在句首

RE_SUMMARY = re.compile(r"(所以|也就是说|其实|总之|总结一下|归根结底|核心是|最终|真正要|并不是.*而是|不是.*而是)")
RE_ENUMERATED_ITEM = re.compile(r"(第[一二三四五六七八九十\d]+[个种类条步])([,，:：、]|$)")

def extract_items(text):
    """Extract ordinal items (第一…第二…). Returns list of item strings or []."""
    parts = re.split(r"(?=第[一二三四五六七八九十\d]+[个种类条步][,，:：、])", text)
    items = [p.strip() for p in parts if re.match(r"^第[一二三四五六七八九十\d]+[个种类条步]", p.strip())]
    # fallback: 一个是…另一个是…
    if not items:
        m = re.findall(r"(一个是[^。]*。|另一个是[^。]*。)", text)
        items = [x for x in m]
    return items

def classify_group(text):
    if RE_EVIDENCE.search(text):
        return {"semantic_role": "evidence", "relation_type": "evidence_support", "confidence": 1.0, "rule": "R1"}
    if RE_CONSTRAINT.search(text):
        return {"semantic_role": "constraint", "relation_type": "warning_about", "confidence": 1.0, "rule": "R2"}
    if RE_COMPARISON.search(text):
        return {"semantic_role": "comparison", "relation_type": "alternative", "confidence": 1.0, "rule": "R3"}
    ordinals = RE_ORDINAL.findall(text)
    if len(ordinals) >= 2:
        return {"semantic_role": "principle", "relation_type": "parallel",
                "confidence": 1.0 if len(ordinals) >= 3 else 0.7, "rule": "R4", "ordinal_count": len(ordinals)}
    if RE_SUMMARY.search(text):
        return {"semantic_role": "summary", "relation_type": "summary_of", "confidence": 1.0, "rule": "R5"}
    return {"semantic_role": "explanation", "relation_type": "hierarchy", "confidence": 0.5,
            "rule": "R6_default", "unmatched": True}

# --------------------------------------------------------------------------
# 3. Evidence check (mechanical lookup in source_article.md)
# --------------------------------------------------------------------------
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

def build_source_index(source):
    """Return (sections, images_by_section) and heading key-terms."""
    headings = []   # (line, level, text)
    images = []     # {line, filename, nearest_heading, before, after}
    lines = source.splitlines()
    current_heading = None
    for i, line in enumerate(lines):
        h = HEADING_RE.match(line)
        if h:
            current_heading = h.group(2).strip()
            headings.append({"line": i + 1, "level": len(h.group(1)), "text": current_heading})
            continue
        for m in IMG_RE.finditer(line):
            images.append({"line": i + 1, "filename": m.group(1), "nearest_heading": current_heading,
                           "before": next((lines[j].strip() for j in range(i - 1, -1, -1) if lines[j].strip()), ""),
                           "after": next((lines[j].strip() for j in range(i + 1, len(lines)) if lines[j].strip()), "")})
    return headings, images

def section_keyterms():
    """Section -> list of key terms that should appear in narration group text."""
    return [
        ("intro", ["收藏", "吃灰", "造工具", "问题我也遇见", "5亿Token", "标题"]),
        ("sec1_tool_intro", ["这个工具到底是什么", "工具叫", "启动方式", "脚本", "接收端", "10步", "流程"]),
        ("sec2_problems", ["可以解决什么问题", "使用场景", "第二大脑", "下载器", "收集", "视频转译", "知识库", "多维表格"]),
        ("sec3_get_install", ["如何获取工具", "安装部署", "GitHub", "源码安装", "Agent代安装", "需要你自己准备", "四大模块", "飞书", "DeepSeek", "obsidian"]),
        ("sec4_usage", ["使用方法", "把链接发给", "发给接收端", "教你如何安装Hermes"]),
        ("sec5_future", ["未来更新计划", "优化方向", "打包成一个app"]),
        ("sec6_summary", ["总结", "AI+知识库", "素材管理"]),
    ]

def map_group_to_section(group_text_):
    best, best_score = None, 0
    for name, terms in section_keyterms():
        score = sum(1 for t in terms if t in group_text_)
        if score > best_score:
            best, best_score = name, score
    return best, best_score

def section_has_image(section_name, images, headings):
    if section_name == "intro":
        return False
    # map section name to heading prefix keywords
    key = {
        "sec1_tool_intro": "这个工具到底是什么",
        "sec2_problems": "这个工具可以解决什么问题",
        "sec3_get_install": "如何获取工具",
        "sec4_usage": "工具的使用方法",
        "sec5_future": "未来更新计划",
        "sec6_summary": "总结",
    }[section_name]
    section_start = next((h["line"] for h in headings if key in h["text"]), None)
    if section_start is None:
        return False
    # find next heading after section_start to bound the range
    section_end = None
    for h in headings:
        if h["line"] > section_start:
            section_end = h["line"]
            break
    hit = [im for im in images if im["line"] >= section_start and (section_end is None or im["line"] < section_end)]
    return bool(hit), [im["filename"] for im in hit]
