# -*- coding: utf-8 -*-
"""步骤表：编号、名称、输入、产物、是否花钱（与 `ASSEMBLY_LINE.md` A3、A4 一致）。

理论型 = COMMON_HEAD + THEORY_TAIL（S1..S5、S7、S8、S9、S10）。
截图型 = COMMON_HEAD + SHOT_TAIL（S6 截图分析、S7 编排由引擎真跑，S8 出图）。

**ST1-P4a 起两种类型都是"先编排后出图"（S7 → S8）**：
  - S7 产出 `06-编排/出图需求.json`（组号 / 插画类型 / 提示词 / 尺寸参数 / 请求内容哈希）；
  - S8 只读这份清单出图，已有的图不重出。
  理论型因此新增 S7（原来 S5 直接到 S8）；截图型把 S8 从 S7 之前挪到之后。
  **ST1-P4a2 起 S7 的"插画决策"可导入、也可真算**：有 `06-编排/插图决策.json` 就导入
  （旧付费结果，A6「计算 + 子项导入」口径）；没有就调模型真算
  （`orchestrate.decide_records`，4 个依赖经统一付费入口 `paid.perform`）。
"""

COMMON_HEAD = {
    "S0": {"name": "内容类型判断", "cost": True,
           "in": ["00-输入/文章.md", "00-输入/来源.json", "00-输入/图片分类.json"],
           "out": ["00-输入/类型判断.json"]},
    "S1": {"name": "口播", "cost": True,
           "in": ["00-输入/文章.md"],
           "out": ["01-口播/narration.txt", "01-口播/narration_meta.json"]},
    "S2": {"name": "分组", "cost": False,
           "in": ["01-口播/narration.txt"],
           "out": ["02-分组/groups_raw.json"]},
    "S3": {"name": "配音", "cost": True,
           "in": ["02-分组/groups_raw.json"],
           "out": ["03-配音/narration_tts.txt", "03-配音/sentences.json",
                   "03-配音/query_response.json", "03-配音/audio/*.wav"]},
    "S4": {"name": "对齐", "cost": False,
           "in": ["03-配音/sentences.json", "02-分组/groups_raw.json"],
           "out": ["04-对齐/alignment.json", "04-对齐/char_times.json"]},
}

THEORY_TAIL = {
    "S5": {"name": "图片定位", "cost": False,
           "in": ["00-输入/文章.md", "02-分组/groups_raw.json"],
           "out": ["06-编排/image_map.json"]},
    # S7（ST1-P4a 新增）：编排——决定"哪些组要插画、每张是什么"，产出出图需求清单。
    # `插图决策.json` 是导入的旧付费结果（导演链/画面提炼的决策），本步据此构建清单；
    # 真跑时由 S7 自己调模型（DeepSeek/方舟）产出决策，故 `cost=True`。
    "S7": {"name": "编排（出图需求）", "cost": True,
           "in": ["02-分组/groups_raw.json", "06-编排/image_map.json",
                  "06-编排/插图决策.json"],
           "out": ["06-编排/出图需求.json"]},
    "S8": {"name": "出图", "cost": True,
           "in": ["02-分组/groups_raw.json", "06-编排/image_map.json",
                  "06-编排/出图需求.json"],
           "out": ["07-出图/manifest.json", "07-出图/images/*.png"]},
    "S9": {"name": "场景构建", "cost": False,
           "in": ["07-出图/manifest.json", "04-对齐/alignment.json", "03-配音/sentences.json",
                  "02-分组/groups_raw.json", "03-配音/audio/*.wav", "07-出图/缺图.json"],
           "out": ["08-场景/video_data.json"]},
    "S10": {"name": "成片渲染", "cost": False,
            "in": ["08-场景/video_data.json", "07-出图/images/*.png"],
            "out": ["09-成片/*.mp4"]},
}

SHOT_TAIL = {
    "S5": {"name": "图片定位", "cost": False,
           "in": ["00-输入/文章.md", "02-分组/groups_raw.json"],
           "out": ["05-截图分析/image_map.json"]},
    # S6：同一步内「引擎真跑 + 命中历史缓存」（ST1-P4a2 §一.3）。shot_groups.json 由引擎
    # 真跑（pipeline/shot_groups.py）；导演链/图句匹配/标签走 `06-编排/cache`，停留判断走
    # `05-截图分析/cache`——缓存由样本准备脚本按内容键预置（P7 230 条 / P9 48 条）。
    # 某个子项缓存没命中且 `--no-paid` 时，**这一项退回导入**并逐项写明原因。
    "S6": {"name": "截图分析", "cost": True,
           "in": ["00-输入/文章.md", "05-截图分析/image_map.json", "02-分组/groups_raw.json"],
           "out": ["05-截图分析/shot_groups.json",
                   "05-截图分析/labels.json", "05-截图分析/matches_v2.json",
                   "05-截图分析/stay_judgments.json", "05-截图分析/stay_endings.json",
                   "05-截图分析/镜头计划.json",
                   "05-截图分析/cache/**",
                   "06-编排/ai_out.json", "06-编排/cache/**"]},
    # S7（ST1-P4a 起排在 S8 之前）：编排——出图需求清单 + 最终时间轴。
    # 时间轴由 `pipeline/shot_timeline.py::build_timeline` 真跑（纯计算）；
    # "需要插画的组"取自 `插图决策.json`（有则导入）或 `decide_records` 真算。
    # **不再依赖 `07-出图/manifest.json`**——否则与 S8「读 S7 的清单」成环。
    # 截图型真算时的路由依据是 S6 的 `05-截图分析/镜头计划.json`（导演层产出），
    # **不读**自身产物 `06-编排/shots_plan.json`（ST1-P4b 甲.1，消除循环依赖）。
    "S7": {"name": "编排", "cost": True,
           "in": ["02-分组/groups_raw.json", "05-截图分析/image_map.json",
                  "03-配音/sentences.json",
                  "05-截图分析/shot_groups.json", "05-截图分析/labels.json",
                  "05-截图分析/stay_endings.json", "06-编排/ai_out.json",
                  "05-截图分析/镜头计划.json",
                  "06-编排/插图决策.json"],
           "out": ["06-编排/shots_plan.json", "06-编排/出图需求.json"]},
    # S8：出图——只读 S7 的 `出图需求.json`，已有的图不重出（全部走按内容缓存，见 A5）。
    "S8": {"name": "出图", "cost": True,
           "in": ["02-分组/groups_raw.json", "05-截图分析/image_map.json",
                  "06-编排/出图需求.json"],
           "out": ["07-出图/manifest.json", "07-出图/images/*.png"]},
    "S9": {"name": "场景构建", "cost": False,
           "in": ["07-出图/manifest.json", "04-对齐/alignment.json", "03-配音/sentences.json",
                  "02-分组/groups_raw.json", "03-配音/audio/*.wav",
                  "06-编排/shots_plan.json", "05-截图分析/image_map.json",
                  "00-输入/图片和附件/*", "07-出图/缺图.json"],
           "out": ["08-场景/video_data.json", "08-场景/video_data.ts",
                   "08-场景/shots_data.ts", "08-场景/shotImageMap.json"]},
    "S10": {"name": "成片渲染", "cost": False,
            "in": ["08-场景/video_data.json", "08-场景/shots_data.ts",
                   "07-出图/images/*.png", "00-输入/图片和附件/*", "03-配音/audio/*.wav"],
            "out": ["09-成片/*.mp4"]},
}

# ST1-P4a：两种类型统一"先编排后出图"（S7 → S8）。
STEP_ORDER_THEORY = ["S0", "S1", "S2", "S3", "S4", "S5", "S7", "S8", "S9", "S10"]
STEP_ORDER_SCREENSHOT = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10"]
STEP_ORDER = STEP_ORDER_SCREENSHOT   # 兼容旧引用（并集顺序）

CONTENT_TYPE_THEORY = "theory"
CONTENT_TYPE_SCREENSHOT = "screenshot"

# 付费步骤 -> 本轮可能用到的密钥（仅用于配置错误检查；`--stub` 时不查）。
# ST3-P22（G15）：S7 只调 DeepSeek，不再要求方舟；S0 只在真要识图时才要求方舟
# （cli 开跑前用 `steps.s0_needs_vision` 判断）。
PAID_KEYS = {
    "S0": ["ARK_API_KEY"],
    "S1": ["DEEPSEEK_API_KEY"],
    "S3": ["MODEL_SPEECH_API_KEY"],
    "S6": ["DEEPSEEK_API_KEY", "ARK_API_KEY"],
    "S7": ["DEEPSEEK_API_KEY"],
    "S8": ["RUNNINGHUB_API_KEY"],
}

# 引擎读的全部密钥变量（渲染子进程启动前从环境里去掉，ST3-P22 G18）。
SECRET_ENV_VARS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "ARK_API_KEY", "DEEPSEEK_API_KEY",
                   "MODEL_SPEECH_API_KEY", "RUNNINGHUB_API_KEY")


def steps_for(content_type):
    return {**COMMON_HEAD, **(SHOT_TAIL if content_type == "screenshot" else THEORY_TAIL)}


def order_for(content_type):
    """执行顺序。`content_type` 为 None（S0 判不出）时按理论型列——S0 随后就会失败。"""
    steps = steps_for(content_type)
    seq = STEP_ORDER_SCREENSHOT if content_type == "screenshot" else STEP_ORDER_THEORY
    return [s for s in seq if s in steps]


# ST3-P28b：运行时读 `04-对齐/` 的步骤（S4 写出的对齐结果以磁盘为准）。
# 这些读取**不进输入指纹**——否则仅仅"新增一条声明"就会让已完成的文章在付费步骤上
# 白白重跑。新鲜度由 `steps.rerun_reason` 按 `align_global.alignment_signature` 判定；
# 这里同时用于 `--from` 的下游展开（S4 → 读者）。
# 实测：S6（导演链/图句匹配/镜头计划）、S7（时间轴、试排场景、句子索引）、S9（场景构建）
# 都调 word_align / R148 拿对齐时间；S5（图片定位）不读对齐。
ALIGN_READS = {
    CONTENT_TYPE_THEORY: ("S7", "S9"),
    CONTENT_TYPE_SCREENSHOT: ("S6", "S7", "S9"),
}


def alignment_readers(content_type):
    """读 `04-对齐/` 的步骤集合（只返回该类型实际存在的步骤）。"""
    steps = steps_for(content_type)
    return {s for s in ALIGN_READS.get(content_type, ()) if s in steps}


def downstream_of(content_type, start):
    """按 A3 声明的输入→输出数据依赖算下游（精确到文件通配），返回 {步骤}。

    与 ST1-P1 `e_resume.py` 的算法一致：生产者产物被消费者输入通配命中即连边。
    `ALIGN_READS` 补上"代码里读对齐、但不进输入指纹"的那几条边（只影响下游展开）。
    """
    import fnmatch
    steps = steps_for(content_type)
    ids = order_for(content_type)
    edges = {s: set() for s in ids}
    for cons in ids:
        for i in steps[cons]["in"]:
            for prod in ids:
                if prod == cons:
                    continue
                if any(fnmatch.fnmatch(i, o) for o in steps[prod]["out"]):
                    edges[prod].add(cons)
    if "S4" in edges:
        for cons in alignment_readers(content_type):
            edges["S4"].add(cons)
    need, frontier = set(), {start}
    while frontier:
        cur = frontier.pop()
        for nxt in edges.get(cur, ()):
            if nxt not in need:
                need.add(nxt)
                frontier.add(nxt)
    return need
