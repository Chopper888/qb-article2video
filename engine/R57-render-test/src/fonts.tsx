import React from "react";

// 使用系统字体，不随仓库分发字体文件。
export const TITLE_FONT =
  '"PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC", sans-serif';

// 保留该组件供各处引用；使用系统字体后无需注入 @font-face。
export const FontFaces: React.FC = () => null;
