# -*- coding: utf-8 -*-
"""shot_route：截图路由接管钩子（SHOT-M4-P4 C1 收成）。

make_route_hook(shots_plan, screenshot_gids=None) -> route_hook。

route_hook(gid, group) 返回：
  - "screenshot"：该组有截图镜头（含占位镜头）。
  - "blocked"   ：该组映射到了截图（在 screenshot_gids 里），但导演层没能为它产出镜头。
  - None        ：走 ai_default（主线原有 AI 出图流程）。

判定依据：
  - 有截图镜头 = 按 override 之后的有效组（shot["group"]）在 shots 里。
  - 无截图步骤（占位，如 G36/G43）= 导演层 include_placeholders=True 时生成的 placeholder 镜头。
  - 映射到截图但无镜头 = gid ∈ screenshot_gids 且不在 shots 的有效组里（缺图、构建失败等）。
"""
ROUTE_SCREENSHOT = "screenshot"
ROUTE_TABLE_FALLBACK = "table_fallback"
ROUTE_AI_DEFAULT = "ai_default"
ROUTE_SKIP = "skip"
ROUTE_BLOCKED = "blocked"


def make_route_hook(shots_plan, screenshot_gids=None):
    """构造 route_hook。

    shots_plan: build_shots 输出（含 shots 列表，每项有 group / placeholder 等）。
    screenshot_gids: 全部「映射到截图」的有效组集合（可选）。传入后，映射到截图但无镜头的组返回 blocked。
    """
    shots = shots_plan.get("shots", []) if isinstance(shots_plan, dict) else shots_plan
    shot_by_group = {}
    for s in shots:
        shot_by_group[s["group"]] = s

    def route_hook(gid, group):
        if gid in shot_by_group:
            return ROUTE_SCREENSHOT
        if screenshot_gids is not None and gid in screenshot_gids:
            return ROUTE_BLOCKED
        return None

    return route_hook
