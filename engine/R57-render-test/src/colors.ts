// 渲染端颜色 tokens（基础风格：白底、深灰字、单一蓝色强调）。
// 引擎侧自检（pipeline/engine/frame_check.py 等）读取本文件的常量名，
// 两边的颜色值必须同步修改。

export const COLOR_BG = "#FFFFFF";      // 背景 白
export const COLOR_ACCENT = "#2F6FEB";  // 强调 蓝（全片唯一强调色）

export const COLOR_INK = "#1F1F1F";        // 正文文字
export const COLOR_SUB_BG = "rgba(255,255,255,0.94)"; // 字幕背景
export const COLOR_SUB_BORDER = "#D0D0D0"; // 字幕边框
export const COLOR_SUB_SHADOW = "transparent"; // 字幕无投影
export const COLOR_TRACK = "#E5E5E5";      // 进度条轨道灰
export const COLOR_RING = "#CCCCCC";       // 进度条节点未来环灰
export const COLOR_NODE_IDLE = "#FFFFFF";  // 进度条节点未完成白底
export const COLOR_TAG_BG = "#EAF1FD";     // 章节标签底（强调色浅色版）
export const COLOR_TAG_FG = "#2F6FEB";     // 章节标签字
export const COLOR_TAG_MUTED = "#888888";  // 章节标签其他字
export const COLOR_BLACK = "#000000";      // 片尾黑场
