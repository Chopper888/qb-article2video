# 文章成片

**文章成片 —— 把一篇图文教程一键变成带配音、字幕、插画的讲解视频。**

作者：乔巴（Chopper888）

![效果演示](docs/demo.gif)

- [观看完整演示视频（42 秒）](docs/demo.mp4)
- 更多完整成片：[B 站主页](https://space.bilibili.com/522625412)

> 注意：本仓库是**框架 + 基础风格**，能出片，但效果朴素；下方「下载安装包」使用的是作者的完整风格。

## 两种用法（先看这里）

### 方式一：下载安装包（推荐，不用折腾）

不用装 Python / Node / FFmpeg，装完填好 API Key 就能用。

- GitHub Releases：<https://github.com/Chopper888/qb-article2video/releases>
- 夸克网盘下载（国内推荐）：<https://pan.quark.cn/s/03ebea3c47ef>

最新版本：1.1.0。安装包使用作者完整风格，出片效果与上方演示一致。

### 方式二：从源码运行

适合想改代码、换风格、做二次开发的人。需要自己安装环境（见下方「从源码运行」）。
本仓库自带的风格是基础的：白底、深灰字、单一蓝色强调、系统字体、普通白底字幕。

想换成自己的风格，主要改这些文件：

| 想改什么 | 文件 |
|---|---|
| 配色 | `engine/R57-render-test/src/colors.ts` |
| 字体 | `engine/R57-render-test/src/fonts.tsx` |
| 标题卡 / 代码卡 / 终端卡 / 表格卡 | `engine/R57-render-test/src/ST3Components.tsx` |
| 字幕样式与画面布局 | `engine/R57-render-test/src/R125Root.tsx` |
| 各步骤提示词（17 个） | `engine/pipeline/prompts/*.txt` |
| 口播稿模板 | `engine/pipeline/vendor/zhongzhuanzhan/Article-to-Narration提示词模板-v2_3.md` |
| 插画类型与提示词 | `engine/pipeline/vendor/wz-sp-ctsc/references/type-manifest.json`、`scripts/generate.py`、`scripts/title_generate.py` |

## 安装包使用

- 系统要求：Windows 10 / 11（64 位）
- 首次运行时 Windows SmartScreen 可能提示「Windows 已保护你的电脑」：
  点「更多信息」→「仍要运行」即可。
- 在「设置」里填入各服务的 API Key 后即可导入文章、开始生成。

## 从源码运行

### 环境要求

| 组件 | 版本 |
|---|---|
| Python | 3.12 |
| Node.js | 24（含 npm） |
| FFmpeg | 任意较新版本（需在 PATH 中，且带 `ffprobe`） |

FFmpeg 可从 <https://ffmpeg.org/download.html> 获取，Windows 也可用
<https://www.gyan.dev/ffmpeg/builds/> 的 full 构建。装好后在终端里执行 `ffmpeg -version` 能显示版本即可。

### 安装依赖

```powershell
# Python 依赖（在仓库的 engine 目录下）
cd engine
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 渲染工程的 Node 依赖（只需要装一次）
cd R57-render-test
npm install
```

`engine\R57-render-test` 装完依赖后就是运行时目录，执行时用 `--runtime-dir` 指向它即可。

### 配置密钥

```powershell
# 在仓库根目录
Copy-Item .env.example .env
notepad .env   # 填入真实密钥
```

引擎只从**进程环境变量**读取密钥，不会自动解析 `.env`。请在运行引擎的同一个终端里加载：

```powershell
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process')
}
```

需要设置的环境变量：

| 变量 | 用途 | 是否必填 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 口播稿、分组、标注等文本生成 | 必填 |
| `ARK_API_KEY` | 火山方舟，文章配图识别 | 必填 |
| `MODEL_SPEECH_API_KEY` | 豆包语音，配音合成 | 必填 |
| `RUNNINGHUB_API_KEY` | RunningHub，插画与标题图 | 必填 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书文档导入 | 可选 |
| `A2V_TTS_SPEAKER` | 配音音色。留空用公共音色 `zh_female_shuangkuaisisi_uranus_bigtts`（需开通「语音合成大模型」）；填自己复刻的音色编号（`S_` 开头）则用 `seed-icl-2.0`（需开通「声音复刻」） | 可选 |
| `A2V_TTS_RESOURCE_ID` | 手动覆盖资源编号；留空按音色自动选择（`S_`=seed-icl-2.0、`_uranus_bigtts`=seed-tts-2.0、`_mars_bigtts`=seed-tts-1.0） | 可选 |

### 命令行用法

所有命令在 `engine` 目录下执行，入口是 `python -m pipeline.engine`。

```powershell
# 1) 导入文章（飞书链接 / 本地 .md / 文件夹 / .zip）
python -m pipeline.engine import --work-root "$env:USERPROFILE\article2video\工作" --input "https://my.feishu.cn/wiki/..."

# 2) 估算费用（不联网、不需要密钥）
python -m pipeline.engine estimate --work-root "$env:USERPROFILE\article2video\工作" --article "文章文件夹名"

# 3) 出片
python -m pipeline.engine run --work-root "$env:USERPROFILE\article2video\工作" --article "文章文件夹名" --delivery-root "$env:USERPROFILE\article2video\成片" --runtime-dir "<仓库路径>\engine\R57-render-test" --max-cost-cny 20
```

也可以一步导入并出片：把 `--article` 换成 `--input "文章链接或路径"`。

成片输出到 `--delivery-root\<文章文件夹名>\<文章文件夹名>.mp4`；
检查报告在 `<work-root>\<文章文件夹名>\10-报告\检查报告.md`。

常用参数：

| 参数 | 说明 |
|---|---|
| `--work-root` | 工作根目录，每篇文章一个子文件夹 |
| `--article` / `--input` | 二选一：已有文章文件夹名，或新导入的链接/路径 |
| `--delivery-root` | 成片交付目录（不传则留在工作目录里） |
| `--runtime-dir` | 运行时目录（其下有 `node_modules`），默认 `<work-root 上一级>\_runtime` |
| `--tmp-dir` | 临时目录（会把 TEMP/TMP 指向它） |
| `--max-cost-cny` | 本篇费用上限（元），防止超支 |
| `--content-type` | `auto` / `theory` / `screenshot`，配合 `--input` 使用 |
| `--from` / `--to` | 从某步骤开始 / 跑到某步骤停 |
| `--no-paid` | 禁止一切付费调用 |
| `--refetch` | 配合 `--input`：重新抓取飞书原文 |
| `--unlock-narration` | 删除口播定稿锁，重新生成口播与配音 |

退出码：`0` 成功；`1` 检查标红（已出片，需人工看一眼）；`2` 执行失败；`3` 中断；`4` 参数或配置错误。

## 让 Agent 调用

Claude Code、Codex 这类 Agent 可以直接用上面的命令行完成一整篇文章的出片。把下面的说明交给 Agent 即可：

```text
请在本机用「文章成片」引擎把这篇文章做成视频：
1. 仓库位置：<仓库路径>；引擎在 engine 目录，命令用 python -m pipeline.engine。
2. 先在终端里加载 .env 里的环境变量（见 README「配置密钥」）。
3. 运行：
   python -m pipeline.engine run --work-root "<工作目录>" --input "<文章链接或本地路径>" --delivery-root "<成片目录>" --runtime-dir "<仓库路径>\engine\R57-render-test" --max-cost-cny 20
4. 结束后读取 <工作目录>\<文章文件夹名>\10-报告\检查报告.md：
   - 第一行是总体结论；有「标红」就把标红项原文报给我；
   - 退出码 0 = 成功；1 = 已出片但有检查项标红；2/3/4 按报告说明原因，不要盲目重跑。
5. 成片路径是 <成片目录>\<文章文件夹名>\<文章文件夹名>.mp4。
```

## 申请 API Key

本项目需要以下服务，均为按量计费，充值少量即可跑通一篇文章：

- **DeepSeek**：<https://platform.deepseek.com/>
- **火山方舟（豆包大模型 / 识图）**：<https://www.volcengine.com/product/ark>
- **豆包语音合成（TTS）**：<https://www.volcengine.com/product/tts> —— 默认使用公共音色，需要在火山引擎开通**「语音合成大模型」**；如果想用自己的声音，另外开通**「声音复刻」**，并在设置 / 环境变量 `A2V_TTS_SPEAKER` 中填音色编号。
- **RunningHub（插画与标题图）**：<https://www.runninghub.ai> —— 没有账号？[点这里注册](https://www.runninghub.ai?inviteCode=rh-v1419)
- **飞书开放平台（可选，用于导入飞书文档）**：<https://open.feishu.cn/>

各平台的具体开通步骤以官方文档为准，这里不再展开。

## 自己打包

打包脚本在 `app/scripts/packaging/`，需要 Windows + PowerShell：

```powershell
cd app
./scripts/packaging/build-installer-v5.7.ps1 `
  -BuildPython <宿主 Python 3.12> `
  -EngineSource <引擎目录> `
  -RuntimeSource <运行时源目录> `
  -SampleSource <样本源目录> `
  -DeliveryRoot <交付目录>
```

脚本会自动下载并校验所需的 Python、Node、wheel、FFmpeg 等外部资产（默认先查 `app/assets-mirror`），
然后组装运行时并打 NSIS 安装包。打出的是**本仓库的基础风格**，不含作者的完整风格。

## 参与贡献

欢迎提 Issue 和 PR。遇到问题请尽量附上：运行命令、退出码、`10-报告\检查报告.md` 的内容。

## 交流与反馈

使用中遇到问题、有功能建议，欢迎交流；也可以直接在 GitHub 提 Issue 或 PR。

| 微信（进交流群） | 公众号 |
|:---:|:---:|
| <img src="docs/wechat.jpg" width="220"> | <img src="docs/gzh.jpg" width="220"> |
| 加好友备注「**文章成片**」，拉你进群 | 关注获取更新和教程 |

🔗 [知识星球](https://t.zsxq.com/GFfis) · [B站主页](https://space.bilibili.com/522625412)

## 许可证

本项目以 MIT 许可发布，见 [LICENSE](LICENSE)。

Remotion、FFmpeg 等第三方组件的许可与使用限制见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
