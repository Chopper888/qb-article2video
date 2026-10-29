# 第三方组件与许可说明

本仓库自身代码以 MIT 许可发布（见 `LICENSE`）。运行本项目依赖以下第三方组件，它们各自保留原有许可。

## Remotion（重点）

- 组件：`remotion`、`@remotion/cli`、`@remotion/renderer` 4.0.505
- 许可：**Remotion License**（不是 OSI 开源许可）
- 要点：个人、非营利组织、以及**员工不超过 3 人**的营利组织可免费使用；规模更大的公司需自行购买 [Remotion Company License](https://www.remotion.dev/license)。
- 许可禁止出售、出租、再许可、转授权 Remotion 及其衍生品；分发时须一并保留 Remotion 的 `LICENSE.md`。
- 本仓库随附许可原文：[`licenses/remotion-LICENSE.md`](licenses/remotion-LICENSE.md)。
- 本仓库只提供源码，不包含 Remotion 二进制；使用者应自行确认自身规模是否符合免费条件。

## FFmpeg

- 用途：视频合并、抽帧、音频处理。
- 许可：**LGPL**（本项目使用的 Windows 构建为 LGPL 动态库版本）。
- 本仓库**不包含** FFmpeg 二进制，需要自行安装（见 README）。安装包中随附的 FFmpeg 为 LGPL shared 构建，其许可文本随安装包提供；对应源码可从 <https://ffmpeg.org/download.html> 获取，Windows LGPL 构建亦可从 <https://github.com/BtbN/FFmpeg-Builds> 获取。
- 使用 FFmpeg 时请遵守 LGPL 的可重新链接与调试性要求。

## Python 依赖（`engine/requirements.txt`）

| 组件 | 许可 |
|---|---|
| Pillow | MIT-CMU (HPND) |
| requests | Apache-2.0 |
| urllib3 | MIT |
| certifi | MPL-2.0 |
| charset-normalizer | MIT |
| idna | BSD-3-Clause |
| numpy | BSD-3-Clause |

以上均以 `pip install` 方式获取，不在本仓库中分发其二进制。

## 渲染工程依赖（`engine/R57-render-test/package.json`）

| 组件 | 许可 |
|---|---|
| remotion / @remotion/cli / @remotion/renderer | Remotion License（见上） |
| react / react-dom | MIT |
| typescript | Apache-2.0 |

## APP 外壳依赖（`app/package.json`）

| 组件 | 许可 |
|---|---|
| electron | MIT |
| react / react-dom | MIT |
| lucide-react | ISC |
| electron-builder | MIT |
| electron-vite / vite | MIT |
| vitest / jsdom / @testing-library/* | MIT |
| typescript | Apache-2.0 |

## 字体

本仓库**不包含**字体文件，渲染使用系统中文字体（Windows 上的微软雅黑 / PingFang SC / Noto Sans CJK 等）。
安装包内随附的字体（如有）按其自身许可（SIL OFL 1.1）分发，不以本仓库许可覆盖。

## 演示素材

`docs/demo.mp4`、`docs/demo.gif`、`docs/wechat.jpg`、`docs/gzh.jpg` 均为作者本人制作，仅用于说明本项目的实际输出效果，随仓库一并以 MIT 许可提供。
