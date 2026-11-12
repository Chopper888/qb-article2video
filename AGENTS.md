# AGENTS.md

给 Claude Code、Codex 等 Agent 的使用说明。用户让你"把这篇文章做成视频"时，按这里做。

## 项目是什么

文章成片：把一篇图文教程（飞书文档、本地 Markdown、文件夹、zip）自动做成带配音、字幕、插画、截图标注的讲解视频。

- 引擎在 `engine/`，入口 `python -m pipeline.engine`，**所有命令在 `engine` 目录下执行**。
- 桌面应用在 `app/`，出片不需要它。
- 完整参数和退出码：[docs/命令行与常见问题.md](docs/命令行与常见问题.md)。

## 开始前确认

1. Python 3.12 依赖已装（`engine/requirements.txt`），`engine/R57-render-test` 下已 `npm install`，`ffmpeg`、`ffprobe` 在 PATH 里。缺什么先告诉用户，不要擅自安装。
2. 密钥在当前终端的环境变量里：`DEEPSEEK_API_KEY`、`ARK_API_KEY`、`MODEL_SPEECH_API_KEY`、`RUNNINGHUB_API_KEY`；导入飞书文档还要 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`。用户有 `.env` 时按 README「配置密钥」的方法加载。可以用 `python -m pipeline.engine check-keys` 检测。
3. 和用户约定三个目录：工作目录（`--work-root`）、成片目录（`--delivery-root`）、运行时目录（`--runtime-dir`，从源码运行时是 `<仓库>\engine\R57-render-test`）。

## 标准流程

```powershell
# 1. 导入：stdout 的 input_resolved 事件里有 article（文章文件夹名），记下来
python -m pipeline.engine import --work-root "<工作目录>" --input "<飞书链接或本地路径>"

# 2. 估算：把 estimate 事件里的 low_cny ~ high_cny 报给用户，用户同意后再出片
python -m pipeline.engine estimate --work-root "<工作目录>" --article "<文章文件夹名>"

# 3. 带上限出片：上限取用户同意的数，没有约定时取估算的高值并向上取整
python -m pipeline.engine run --work-root "<工作目录>" --article "<文章文件夹名>" --delivery-root "<成片目录>" --runtime-dir "<运行时目录>" --max-cost-cny <上限>

# 4. 读检查报告
#    <工作目录>\<文章文件夹名>\10-报告\检查报告.md
```

5. 向用户汇报：成片路径（`run_done` 的 `delivery_path`）、实际花费（`run_done` 的 `totals`）、检查报告第一行的总体结论；有标红就把标红项原文列出来。

`run` 运行时间从几分钟到几十分钟不等（渲染最慢），可以放到后台跑，读 stdout 的事件看进度。

## 按退出码处理

| 退出码 | 处理 |
|---|---|
| `0` | 成功。汇报成片路径、费用、检查结论 |
| `1` | 已出片，但检查报告有标红。把标红项原文报给用户，由用户决定是否处理。**不要自己重跑** |
| `2` | 某步失败。读最后一条 `run_done` / `step_fail` 的 `message`、`error_kind`、`service` 报给用户。`network` 可以等一会儿原样再跑一次；`balance_insufficient`、`key_invalid`、`cost_limit` 需要用户充值 / 换密钥 / 同意提高上限，不要盲目重跑 |
| `3` | 被中断。告诉用户，问是否继续；继续就用同样的命令再跑（自动续跑） |
| `4` | 参数或配置错误，还没花钱。按 `message` 修正参数后再跑；`reason` 为 `already_running` 时说明这篇正在跑，不要再启动一个；口播已定稿但文件被改时，问用户选 `--unlock-narration` 还是 `--accept-narration-edit` |

## 安全规则

- **不打印、不回显、不写入任何文件**里的密钥值；检查密钥只用 `check-keys` 或判断变量是否存在。
- `run` **一律带 `--max-cost-cny`**，数值经用户同意。提高上限前先问。
- **不要随意用 `--unlock-narration`**：它会丢掉已定稿的口播并重新配音，要花钱，也会覆盖用户手改的口播。只在用户明确要求重做口播时用。
- 不要用 `--stub`（仅开发测试用，桩结果不能出片）。
- 不确定会不会花钱时，先用 `plan` 看哪些步骤会执行，或加 `--no-paid` 试跑（只用缓存，遇到付费请求就停）。
- 不要删除文章文件夹里的 `cache/`、`进度账本.json`、`01-口播/定稿锁.json`：删了会导致重复付费或口播被重做。

## 用户想改风格时

- 先读 [docs/风格定制指南.md](docs/风格定制指南.md)，里面有每类风格文件的位置、改提示词必须保留的占位符和输出格式、改完怎么验证、怎么避免缓存失效重复付费。
- 代码结构和工作目录结构见 [docs/架构说明.md](docs/架构说明.md)。
- 改提示词、插画提示词前，先告诉用户哪些已有缓存会失效、大概要重新花多少钱。
