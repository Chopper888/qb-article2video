# -*- coding: utf-8 -*-
"""pipeline 调度层 · 编排主循环（P2）。

从 R163/r163_gen.py 的 main() + _ds / ds_retry / extract_title_scene / load_groups
逐字抽取。副作用（API 调用、文件下载）通过 deps 参数显式化，用于记录-回放等价性验证；
build_table_prompt 属 prompt 文本边界外（不抽），由调用方作为参数传入。

抽取原则（R164 P2）：搬家不改写。决策树分支顺序、补跑循环终止条件、ds_retry 重试语义
均与 r163_gen.py 逐字一致，仅副作用调用点改走 deps、路径改走 round_dir 参数。
"""
import json
import random
import re
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

import requests

# 引擎根随代码走（ST1-P4a3）：仓库根由本文件位置推出，不写死主仓路径。
_REPO = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(_REPO / "pipeline" / "vendor" / "wz-sp-ctsc" / "scripts"))  # vendor（ST1-P5b 乙）
import type_match as ctsc
import generate as gen
import title_generate as tgen

sys.path.insert(0, str(_REPO))
from pipeline import routing


# 截图子系统路由五态枚举（P4 预留，M4 收成后由 Migration 轮接上）。
# 默认无截图接管时 route 恒为 ai_default，走现有决策树，行为不变。
ROUTE_SCREENSHOT = "screenshot"
ROUTE_TABLE_FALLBACK = "table_fallback"
ROUTE_AI_DEFAULT = "ai_default"
ROUTE_SKIP = "skip"
ROUTE_BLOCKED = "blocked"


def _ds(system, user):
    return ctsc.extract_json(ctsc.call_deepseek(ctsc.resolve_api_key(), system, user))


def ds_retry(fn, *args, retries=3, **kwargs):
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise


def extract_title_scene(title_text):
    system = (
        "你是一个视频章节标题拆解器。给定一个章节标题，拆成三样东西，严格输出 JSON："
        '{"title":"干净的大标题","subtitle":"一行副标题/定位语","element":"一个简洁核心视觉元素的描述"}。'
        "element 只写一个视觉元素，不写流程图或卡片列表。只输出 JSON。"
    )
    r = _ds(system, f"标题：{title_text}")
    return r.get("title", title_text), r.get("subtitle", ""), r.get("element", "")


def load_groups(round_dir):
    data = json.loads((round_dir / "groups_raw.json").read_text(encoding="utf-8"))
    groups = data["groups"]
    titles = [g for g in groups if g["starts_with_bracket_title"]]
    body = [g for g in groups if not g["starts_with_bracket_title"]]
    return groups, titles, body


def _default_deps():
    """默认真实副作用实现。回放等价性验证时替换成 stub。

    deps 契约（P3/P4 按此契约接，键的签名 / 默认实现 / 可替换语义）：

      "match"(segs) -> list
          默认 ctsc.match。Type 判断：输入 final_content 组的 [{"id","text"}]，
          返回每组的 [{"id","text","matched_type","matched_name","score","reason","considered"}]。
      "extract_subject"(text, name) -> (subject, labels)
          默认 gen.extract_subject_and_labels。画面提炼：subject 为 None 表示弃权（不出图）。
      "extract_title"(title_text) -> (title, subtitle, element)
          默认 extract_title_scene。标题场景拆解（内部经 _ds 调 DeepSeek）。
      "extract_overview"(text) -> [points]
          默认 gen.extract_overview_points。导览点提取。
      "generate"(prompt, rh_key, aspect, resolution) -> (url, usage)
          默认 gen.generate。RunningHub 出图：失败抛异常（如 TimeoutError）。
      "download"(url, path) -> None
          默认 urllib.request.urlretrieve。下载图片到 path（回放时替换为创建占位文件）。
    """
    return {
        "match": ctsc.match,
        "extract_subject": gen.extract_subject_and_labels,
        "extract_title": extract_title_scene,
        "extract_overview": gen.extract_overview_points,
        "generate": gen.generate,
        "download": urllib.request.urlretrieve,
    }


def _ai_illustration_rec(gid, g, ill_results, subject_rows, IMG, prev, first_img):
    """混合型：给「有 `kind=ai` 原图」的组出一条理论型 AI 插图记录（A8.7 第 4 条）。

    与决策树末段的正文分支同构（`matched_type` → 提示词）；返回单条记录，**不改调用方状态**。
    """
    rec = {"group_id": gid, "text": g["group_text"],
           "starts_with_bracket_title": g["starts_with_bracket_title"]}
    ill = ill_results.get(gid, {})
    ty = ill.get("matched_type", "generic")
    name = ill.get("matched_name", "兜底")
    rec["ill_type"] = ty
    rec["path"] = ("generic兜底" if ty == "generic" else f"Type:{name}")
    row = next((x for x in subject_rows if x["group_id"] == gid), None)
    subject = row["subject"] if row else ""
    labels = row["labels"] if row else []
    if subject is None:
        rec["path"] = "不出图(弃权)"
        rec["result"] = None
        return rec
    rec["subject"], rec["labels"] = subject, labels
    kind, trans = routing.pick_transition(prev, g, first_img)
    rec["transition"] = trans
    rec["transition_kind"] = kind
    rec["prompt"] = gen.build_prompt(ty, subject, labels)
    rec["result"] = "待出图"
    rec["image"] = str(IMG / f"{gid}-{ty}.png")
    return rec


def _original_image_rec(gid, g, ims, prev, first_img):
    """混合型（ST1-P4c C1）：给「有 `kind=ai` 原图」的组出一条原图展示记录。

    与决策树末段的「原图」分支同构（`rec["images"]` → 原图文件名），返回单条记录，
    **不改调用方状态**。只在 `original_images=True` 时由截图路由分支调用。
    """
    rec = {"group_id": gid, "text": g["group_text"],
           "starts_with_bracket_title": g["starts_with_bracket_title"]}
    rec["path"] = "原图"
    ims = sorted(ims, key=lambda x: x["line"])
    rec["images"] = [Path(im.get("source_relative_path")
                          or urllib.parse.unquote(im["image_path"])).name for im in ims]
    rec["image"] = rec["images"][0]
    rec["result"] = "原图"
    kind, trans = routing.pick_transition(prev, g, first_img)
    rec["transition"] = trans
    rec["transition_kind"] = kind
    return rec


def decide_records(round_dir, deps=None, build_table_prompt=None, route_hook=None,
                   img_dir=None, groups_json=None, image_map_json=None,
                   mixed_kinds=False, original_images=False, element_gids=None):
    """**只算决策、不出图**（ST1-P4a2 §一.1）：跑完决策树，返回 `records`。

    与 `run_generation` 的决策段**同源**：Phase 1（类型判断 + 画面主体提炼）与 Phase 2
    的逐组分支（标题/导览/合并/不出图/原图/表格/正文）逐行照搬，随机种子同样是 42，
    所以同样的输入得到同样的 records。

    两处**唯一**差别（这也是不能直接调 run_generation 的原因）：
      1. 不调 `deps["generate"]` / `deps["download"]`——出图由 S8 统一走付费入口，
         本函数只把目标文件名写进 `rec["image"]`，`rec["result"]` 记为 `"待出图"`；
      2. 目标目录由参数 `img_dir` 指定（`run_generation` 里写死 `round_dir/images`）。

    `run_generation` 一行未改，两者并存。**同步风险**：今后若改动其中一个的决策树，
    必须同步另一个（本函数头部就写着这句，回执 §一 也记了这一条）。

    `mixed_kinds`（ST1-P4b 新增，默认 `False`＝原有行为不变）：**只截图型**才开混杂分流——
    凡是 `image_map` 里 `kind != "screenshot"` 的原图，不进 `原图` 分支，改由
    `_ai_illustration_rec` 按理论型出一条 AI 插图（A8.7 第 4/5 条）。理论型恒为 `False`：
    它的文章配图按 W1「图片段落画面用原图」办，不出 AI 插画。

    `original_images`（ST1-P4c C1 新增，默认 `False`＝原有行为不变）：置 `True` 时
    `kind=ai` 的原图**一律按原图展示**（进 `原图` 分支），不再做 AI 插图——遵循固定规则
    「原文图片不论是否 AI 生成一律原样展示；AI 插画只用于没有配图的段落」。只在
    `mixed_kinds=True`（截图/混合型）下有意义；理论型本就不走 AI 插图分流。

    `element_gids`（ST3-P2 新增，默认 `None`＝原有行为不变）：含代码块/命令块/表格的
    组 id 集合。命中的组产出 `path="元素卡片"` 记录（无 prompt、无 image），S8 因此不为
    它们出图，画面由 S9 的要素卡片（code_card/terminal/table）接管。
    """
    if deps is None:
        deps = _default_deps()
    if img_dir is None:
        img_dir = round_dir / "images"
    IMG = Path(img_dir)

    random.seed(42)
    # `groups_json` / `image_map_json` 不给时按 `run_generation` 的老约定从 round_dir 取；
    # 引擎的目录布局不同（`02-分组/groups_raw.json`、`06-编排/image_map.json`），显式传入。
    if groups_json is not None:
        data = json.loads(Path(groups_json).read_text(encoding="utf-8"))
        groups = data["groups"]
        titles = [g for g in groups if g["starts_with_bracket_title"]]
        body = [g for g in groups if not g["starts_with_bracket_title"]]
    else:
        groups, titles, body = load_groups(round_dir)
    hits, units = routing.detect_overview(body)
    if units is None:
        raise RuntimeError("导览判定异常")
    trigger_gids = {unit[0]["group_id"] for unit in units}
    absorbed_ids = [g["group_id"] for unit in units for g in unit[1]]
    merged_by_trigger = {
        unit[0]["group_id"]: "\n".join([unit[0]["group_text"]] + [g["group_text"] for g in unit[1]])
        for unit in units
    }

    transition_ids, func_short_ids = routing.identify_no_image_groups(groups)
    no_img = transition_ids | func_short_ids

    img_map = json.loads(Path(image_map_json or (round_dir / "image_map.json"))
                         .read_text(encoding="utf-8"))
    img_by_gid = {}
    table_by_gid = {}
    ai_img_gids = set()
    ai_imgs_by_gid = {}
    for im in img_map["images"]:
        gid = im.get("mapped_group_id")
        if not gid:
            continue
        # 混合型（A8.7）：`kind=ai` 的原图不走截图那条（不占 `原图`），改走理论型插图。
        # 旧样本 / 理论型没有 `kind` 字段 → 默认 `screenshot`，行为与改动前一致。
        if mixed_kinds and im.get("kind", "screenshot") != "screenshot":
            if original_images:
                # ST1-P4c C1：原图原样展示——`kind=ai` 也进 img_by_gid 走「原图」分支，
                # 不再 split 出去做 AI 插图；另记一份供截图路由同组追加原图记录。
                ai_imgs_by_gid.setdefault(gid, []).append(im)
            else:
                ai_img_gids.add(gid)
                continue
        if im.get("has_table"):
            table_by_gid[gid] = im.get("table_content", "")
            continue
        if im.get("insert") is False:
            continue
        img_by_gid.setdefault(gid, []).append(im)
    img_gids = set(img_by_gid.keys())

    final_content = [g for g in body
                     if g["group_id"] not in trigger_gids
                     and g["group_id"] not in absorbed_ids
                     and g["group_id"] not in no_img
                     and (g["group_id"] not in img_gids or g["group_id"] in ai_img_gids)
                     and g["group_id"] not in table_by_gid
                     and g["group_id"] not in (element_gids or set())]

    ill_results = {}
    BATCH = 20
    segs = [{"id": g["group_id"], "text": g["group_text"]} for g in final_content]
    for k in range(0, len(segs), BATCH):
        for r in ds_retry(deps["match"], segs[k:k + BATCH]):
            ill_results[r["id"]] = r

    subject_rows = []
    for g in final_content:
        gid = g["group_id"]
        ill = ill_results.get(gid, {})
        ty = ill.get("matched_type", "generic")
        name = ill.get("matched_name", "兜底")
        try:
            subject, labels = ds_retry(deps["extract_subject"], g["group_text"], name)
        except Exception:
            subject, labels = "", []
        subject_rows.append({"group_id": gid, "type": ty, "type_name": name,
                             "subject": subject, "labels": labels})

    records = []
    prev = None
    first_img = True
    for g in groups:
        gid = g["group_id"]
        rec = {"group_id": gid, "text": g["group_text"],
               "starts_with_bracket_title": g["starts_with_bracket_title"]}
        route = route_hook(gid, g) if route_hook else None
        route = route or ROUTE_AI_DEFAULT
        if route == ROUTE_SCREENSHOT:
            rec["path"] = "截图"
            rec["result"] = "截图"
            records.append(rec)
            if gid in ai_img_gids:
                # 混合型（A8.7 第 4 条）：同组内的非截图原图另出一条理论型 AI 插图记录，
                # 与截图镜头**同组并存**（同组内 `kind=screenshot` 与 `kind=ai` 各走各的）。
                records.append(_ai_illustration_rec(
                    gid, g, ill_results, subject_rows, IMG, prev, first_img))
                first_img = False
            elif gid in ai_imgs_by_gid:
                # ST1-P4c C1：原图原样展示——同组内的非截图原图另出一条原图记录，
                # 与截图镜头同组并存（不再做 AI 插图）。
                records.append(_original_image_rec(gid, g, ai_imgs_by_gid[gid], prev, first_img))
                first_img = False
            prev = g
            continue
        if route == ROUTE_BLOCKED:
            raise RuntimeError(f"截图路由 blocked: gid={gid}，组「{g['group_text'][:40]}」映射到截图但导演层未产出镜头，禁止降级为 AI 出图")
        if route in (ROUTE_SKIP, ROUTE_TABLE_FALLBACK):
            raise RuntimeError(f"截图路由 {route} 本轮不应出现: gid={gid}")
        if element_gids and gid in element_gids and gid not in img_gids:
            # ST3-P2：纯要素组（无原图/截图的代码块/命令块/表格）不出 AI 插图——画面由 S9 的要素卡片接管。
            # 若同组还有原图（gid in img_gids），走下方「原图」分支，S9 里原图+要素都要出现。
            rec["path"] = "元素卡片"
            rec["result"] = "元素卡片"
            records.append(rec)
            prev = g
            continue
        if g["starts_with_bracket_title"]:
            rec["path"] = "标题→轻量模板"
            t, s, e = ds_retry(deps["extract_title"], g["group_text"])
            rec["title"], rec["subtitle"], rec["element"] = t, s, e
            prompt = tgen.build_title_prompt(t, s, e)
            fname = f"{gid}-title.png"
        elif gid in trigger_gids:
            rec["path"] = "开篇导览模板"
            points = ds_retry(deps["extract_overview"], merged_by_trigger[gid])
            prompt = gen.build_overview_prompt(points)
            fname = f"{gid}-overview.png"
        elif gid in absorbed_ids:
            rec["path"] = "合并进导览(不出图)"
            records.append(rec); prev = g; continue
        elif gid in no_img:
            if gid in ai_imgs_by_gid:
                # 缺陷b：no_img（过渡句/功能短句）组若挂着 kind=ai 非截图原图，展示原图，不出 AI 插画。
                records.append(_original_image_rec(gid, g, ai_imgs_by_gid[gid], prev, first_img))
                first_img = False
                prev = g
                continue
            rec["path"] = "不出图(过渡语段或功能短句)"
            records.append(rec); prev = g; continue
        elif gid in img_gids and gid not in ai_img_gids:
            rec["path"] = "原图"
            ims = sorted(img_by_gid[gid], key=lambda x: x["line"])
            rec["images"] = [Path(im.get("source_relative_path")
                                  or urllib.parse.unquote(im["image_path"])).name for im in ims]
            rec["image"] = rec["images"][0]
            rec["result"] = "原图"
            kind, trans = routing.pick_transition(prev, g, first_img)
            rec["transition"] = trans
            rec["transition_kind"] = kind
            records.append(rec); prev = g; continue
        elif gid in table_by_gid:
            rec["path"] = "表格横版插画"
            prompt = build_table_prompt(table_by_gid[gid])
            fname = f"{gid}-table.png"
        else:
            ill = ill_results.get(gid, {})
            ty = ill.get("matched_type", "generic")
            name = ill.get("matched_name", "兜底")
            rec["ill_type"] = ty
            rec["path"] = ("generic兜底" if ty == "generic" else f"Type:{name}")
            row = next((x for x in subject_rows if x["group_id"] == gid), None)
            subject = row["subject"] if row else ""
            labels = row["labels"] if row else []
            if subject is None:
                rec["path"] = "不出图(弃权)"
                rec["result"] = None
                records.append(rec); prev = g; continue
            rec["subject"], rec["labels"] = subject, labels
            prompt = gen.build_prompt(ty, subject, labels)
            fname = f"{gid}-{ty}.png"
        kind, trans = routing.pick_transition(prev, g, first_img)
        rec["transition"] = trans
        rec["transition_kind"] = kind
        rec["prompt"] = prompt
        rec["result"] = "待出图"
        rec["image"] = str(IMG / fname)
        first_img = False
        records.append(rec)
        prev = g
    return records


def run_generation(round_dir, deps=None, build_table_prompt=None, route_hook=None):
    """编排主循环：读 groups_raw.json → 决策树 → 出图 → 写 manifest.json。

    round_dir: 本轮目录（读 groups_raw.json/image_map.json，写 manifest.json/images/）。
    deps: 可替换副作用函数（默认真实实现），键见 _default_deps。
    build_table_prompt: 表格 prompt 构造器（prompt 文本边界外不抽，由调用方注入）。
    route_hook: 截图路由接管钩子（P4 预留，Migration 前不接）。签名 route_hook(gid, group)
        -> route|None，route ∈ 五态枚举（screenshot/table_fallback/ai_default/skip/blocked）。
        默认 None（走 ai_default，即现有行为）。语义：**blocked 必须阻断、禁止 silent
        fallback**——将来接上后，route=blocked 时绝不允许悄悄降级成 AI 出图。route_hook 位于
        "要不要出图"判断之前（已冻结原则：截图系统若已决定用真实截图，AI 出图判断不得介入）。
    """
    if deps is None:
        deps = _default_deps()
    IMG = round_dir / "images"
    IMG.mkdir(parents=True, exist_ok=True)

    random.seed(42)
    groups, titles, body = load_groups(round_dir)
    hits, units = routing.detect_overview(body)
    if units is None:
        print("!! 导览判定异常")
        return 1
    trigger_gids = {unit[0]["group_id"] for unit in units}
    absorbed_ids = [g["group_id"] for unit in units for g in unit[1]]
    merged_by_trigger = {
        unit[0]["group_id"]: "\n".join([unit[0]["group_text"]] + [g["group_text"] for g in unit[1]])
        for unit in units
    }
    print("导览 units:", [(unit[0]["group_id"], [g["group_id"] for g in unit[1]]) for unit in units])

    transition_ids, func_short_ids = routing.identify_no_image_groups(groups)
    no_img = transition_ids | func_short_ids
    print("N2 过渡语段(紧邻标题+短+内容重合):", sorted(transition_ids))
    print("N3 纯功能性短句(极短, 非紧邻标题):", sorted(func_short_ids))

    # I1: 图片段落(原文图位置)不出 AI 插画；W2 第一个一级标题前图片不插入；W6 表格图改用表格生成横版插画
    img_map = json.loads((round_dir / "image_map.json").read_text(encoding="utf-8"))
    img_by_gid = {}
    table_by_gid = {}
    for im in img_map["images"]:
        gid = im.get("mapped_group_id")
        if not gid:
            continue
        if im.get("has_table"):
            table_by_gid[gid] = im.get("table_content", "")
            continue
        if im.get("insert") is False:
            continue  # W2: 第一个一级标题之前的图片不插入
        img_by_gid.setdefault(gid, []).append(im)
    img_gids = set(img_by_gid.keys())
    print("I1 图片段落(原图):", {g: [f"L{im['line']}" for im in sorted(ims, key=lambda x: x['line'])] for g, ims in sorted(img_by_gid.items())})
    print("W6 表格横版插画组:", sorted(table_by_gid.keys()))

    final_content = [g for g in body
                     if g["group_id"] not in trigger_gids
                     and g["group_id"] not in absorbed_ids
                     and g["group_id"] not in no_img
                     and g["group_id"] not in img_gids
                     and g["group_id"] not in table_by_gid]
    print("最终实际出图正文段数:", len(final_content),
          "= 全部正文 - 导览", len(trigger_gids), "- 合并", len(absorbed_ids), "- 过渡", len(transition_ids), "- 功能短句", len(func_short_ids))
    print("应有图片数 =", len(titles), "标题 +", len(trigger_gids), "导览 +", len(final_content), "正文 =",
          len(titles) + len(trigger_gids) + len(final_content))

    rh_key = gen.read_rh_key()

    print("\n[Phase 1] Type 判断 + 画面主体提炼...")
    ill_results = {}
    BATCH = 20
    segs = [{"id": g["group_id"], "text": g["group_text"]} for g in final_content]

    # 成本反馈: 累计 Type判断 / 画面提炼 两个环节的 DeepSeek usage
    type_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
    subject_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}

    def _accum(bucket, u):
        if u:
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                bucket[k] += u.get(k, 0)
            bucket["calls"] += 1

    for k in range(0, len(segs), BATCH):
        for r in ds_retry(deps["match"], segs[k:k + BATCH]):
            ill_results[r["id"]] = r
        _accum(type_usage, getattr(ctsc, "_last_usage", None))

    subject_rows = []
    ladder = []
    for g in final_content:
        gid = g["group_id"]
        ill = ill_results.get(gid, {})
        ty = ill.get("matched_type", "generic")
        name = ill.get("matched_name", "兜底")
        try:
            subject, labels = ds_retry(deps["extract_subject"], g["group_text"], name)
        except Exception as e:
            subject, labels = "", []
        _accum(subject_usage, getattr(ctsc, "_last_usage", None))
        if "梯子" in (subject or ""):
            ladder.append((gid, subject, g["group_text"]))
        subject_rows.append({"group_id": gid, "type": ty, "type_name": name,
                             "subject": subject, "labels": labels})
    print("subject 含「梯子」:", len(ladder), [x[0] for x in ladder])
    print("DeepSeek usage - Type判断:", type_usage)
    print("DeepSeek usage - 画面提炼:", subject_usage)

    print("\n[Phase 2] 出图...")
    records = []
    prev = None
    first_img = True
    for g in groups:
        gid = g["group_id"]
        rec = {"group_id": gid, "text": g["group_text"], "starts_with_bracket_title": g["starts_with_bracket_title"]}
        # 截图接入点（SHOT-M4-P4 C2）：route_hook 位于"要不要出图"判断之前。
        # route ∈ {screenshot, blocked, skip, table_fallback} 时在此接管，AI 出图判断不再介入；
        # blocked 必须阻断、禁止悄悄降级成 AI 出图。ai_default 走下方现有决策树，一行不改。
        route = route_hook(gid, g) if route_hook else None
        route = route or ROUTE_AI_DEFAULT
        if route == ROUTE_SCREENSHOT:
            rec["path"] = "截图"
            rec["result"] = "截图"
            records.append(rec)
            prev = g
            print(f"  [{gid}] 截图 | 跳过AI出图")
            continue
        if route == ROUTE_BLOCKED:
            raise RuntimeError(f"截图路由 blocked: gid={gid}，组「{g['group_text'][:40]}」映射到截图但导演层未产出镜头，禁止降级为 AI 出图")
        if route in (ROUTE_SKIP, ROUTE_TABLE_FALLBACK):
            raise RuntimeError(f"截图路由 {route} 本轮不应出现: gid={gid}")
        try:
            if g["starts_with_bracket_title"]:
                rec["path"] = "标题→轻量模板"
                t, s, e = ds_retry(deps["extract_title"], g["group_text"])
                rec["title"], rec["subtitle"], rec["element"] = t, s, e
                prompt = tgen.build_title_prompt(t, s, e)
                fname = f"{gid}-title.png"
            elif gid in trigger_gids:
                rec["path"] = "开篇导览模板"
                points = ds_retry(deps["extract_overview"], merged_by_trigger[gid])
                prompt = gen.build_overview_prompt(points)
                fname = f"{gid}-overview.png"
            elif gid in absorbed_ids:
                rec["path"] = "合并进导览(不出图)"
                records.append(rec); prev = g; continue
            elif gid in no_img:
                rec["path"] = "不出图(过渡语段或功能短句)"
                records.append(rec); prev = g; continue
            elif gid in img_gids:
                # I1: 图片段落画面用原图，不出 AI 插画
                rec["path"] = "原图"
                ims = sorted(img_by_gid[gid], key=lambda x: x["line"])
                # R168 F5: 用 image_map 里抽取的真实文件名(exec-...png)，不再按
                # r{轮次}_img_L{行号}.png 构造一个不存在的假名。原 R162 判定该分支为
                # "死代码保留"，R167 首篇真插图时触发并 404，需手工复制改名才能渲染。
                # 真实文件名是 UUID 式(唯一、无特殊字符)，可直接用作渲染 basename，无需改名。
                rec["images"] = [Path(im.get("source_relative_path") or urllib.parse.unquote(im["image_path"])).name for im in ims]
                rec["image"] = rec["images"][0]
                rec["result"] = "原图"
                kind, trans = routing.pick_transition(prev, g, first_img)
                rec["transition"] = trans
                rec["transition_kind"] = kind
                records.append(rec); prev = g; continue
            elif gid in table_by_gid:
                # W6: 表格内容生成横版插画(原图不插入, 横向排开)
                rec["path"] = "表格横版插画"
                prompt = build_table_prompt(table_by_gid[gid])
                fname = f"{gid}-table.png"
            else:
                ill = ill_results.get(gid, {})
                ty = ill.get("matched_type", "generic")
                name = ill.get("matched_name", "兜底")
                rec["ill_type"] = ty
                rec["path"] = ("generic兜底" if ty == "generic" else f"Type:{name}")
                row = next((x for x in subject_rows if x["group_id"] == gid), None)
                subject = row["subject"] if row else ""
                labels = row["labels"] if row else []
                # F1: 弃权——画面提炼返回 subject=None 表示"没有可画的具体内容", 不出图, 沿用上一组画面
                if subject is None:
                    rec["path"] = "不出图(弃权)"
                    rec["result"] = None
                    records.append(rec); prev = g; continue
                rec["subject"], rec["labels"] = subject, labels
                prompt = gen.build_prompt(ty, subject, labels)
                fname = f"{gid}-{ty}.png"
            kind, trans = routing.pick_transition(prev, g, first_img)
            rec["transition"] = trans
            rec["transition_kind"] = kind
            rec["prompt"] = prompt  # P1: 失败记录必须带 prompt，补跑才能自动重试
            url, usage = deps["generate"](prompt, rh_key, "16:9", "1k")
            deps["download"](url, IMG / fname)
            rec["result"] = "成功"
            rec["image"] = str(IMG / fname)
            first_img = False
        except Exception as e:
            rec["result"] = f"失败:{type(e).__name__}"
            rec["error"] = str(e)[:300]
        records.append(rec)
        prev = g
        print(f"  [{gid}] {rec['path']} | {rec['result']}")

    failed = [r for r in records if r.get("result") != "成功" and r.get("prompt")]
    if failed:
        print("\n[补跑]", len(failed), "组...")
        for r in failed:
            gid = r["group_id"]
            try:
                url, usage = deps["generate"](r["prompt"], rh_key, "16:9", "1k")
                if r["path"] == "标题→轻量模板":
                    fname = f"{gid}-title.png"
                elif r["path"] == "开篇导览模板":
                    fname = f"{gid}-overview.png"
                else:
                    fname = f"{gid}-{r.get('ill_type', 'generic')}.png"
                deps["download"](url, IMG / fname)
                r["result"] = "成功"
                r["image"] = str(IMG / fname)
            except Exception as e:
                r["error"] = f"{type(e).__name__}: {e}"

    (round_dir / "manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    still_failed = [r for r in records if r.get("result") != "成功" and r.get("prompt")]
    total_img = len(titles) + len(trigger_gids) + len(final_content)
    ok = sum(1 for r in records if r.get("result") == "成功")
    print(f"\n出图完成：成功 {ok} / 应出 {total_img} | 补跑后仍失败 {len(still_failed)}")
    json.dump([{"group_id": r["group_id"], "path": r.get("path"), "result": r.get("result"),
                "error": r.get("error"), "prompt": r.get("prompt", "")} for r in still_failed],
              open(round_dir / "failed.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 成本反馈: 写 Type判断 / 画面提炼 usage
    json.dump({"type_usage": type_usage, "subject_usage": subject_usage,
               "total_prompt_tokens": type_usage["prompt_tokens"] + subject_usage["prompt_tokens"],
               "total_completion_tokens": type_usage["completion_tokens"] + subject_usage["completion_tokens"],
               "total_tokens": type_usage["total_tokens"] + subject_usage["total_tokens"]},
              open(round_dir / "gen_usage.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return 0
