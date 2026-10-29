import React from "react";
import {
  AbsoluteFill,
  Audio,
  Composition,
  Img,
  Sequence,
  staticFile,
  useCurrentFrame,
} from "remotion";
import { videoData } from "./video_data";
import { ScreenshotScene } from "./shots/ScreenshotScene";
import { CodeCard, TerminalWindow, TableCard, ElementStage } from "./ST3Components"; // ST3-P2 要素卡片
import { FontFaces, TITLE_FONT } from "./fonts"; // 使用系统字体，TITLE_FONT 为统一字体栈
// R161 E3: 颜色 tokens 收成到 colors.ts（单一来源声明见该文件）
import {
  COLOR_BG,
  COLOR_ACCENT,
  COLOR_INK,
  COLOR_SUB_BG,
  COLOR_SUB_BORDER,
  COLOR_SUB_SHADOW,
  COLOR_TRACK,
  COLOR_RING,
  COLOR_NODE_IDLE,
  COLOR_TAG_BG,
  COLOR_TAG_FG,
  COLOR_TAG_MUTED,
  COLOR_BLACK,
} from "./colors";

const FPS = videoData.fps;
// R169 I: 字体常量复用 fonts.tsx 的 TITLE_FONT（原自建 FONT 内容相同，改一处忘另一处会漂移）

// R125：字号放大（42 -> 50）
const FONT_SIZE = 50;
// R125：画面整体上移后，底部为字幕预留的专用区高度(px)，插画只画在 0..(1080-RESERVE)
const RESERVE = 300;
// E2: 全局安全区（防 B站/抖音/知识星球播放器边缘 UI 遮挡/裁切），集中定义一处。
// 依据：1920×1080 横屏，顶部弹幕/标题约 5%、底部进度/点赞栏约 5-8%、两侧点赞/关注栏约 4%。
// 取 top/bottom 60px(≈5.6%)、left/right 80px(≈4.2%)：关键内容不入边缘 4-5% 覆盖区，且不过度挤压画面。
const SAFE = { top: 60, bottom: 60, left: 80, right: 80 };

type CapChar = { c: string; f: number };
export type CapLine = (typeof videoData.subtitle_lines)[number];
export type Chunk = { start_frame: number; end_frame: number; text: string };

// ===== 字幕分句：过长的整句按标点切成更短的卡片，整条显示，不做逐字动画 =====
// 注意：chars 里的 c 不一定是单字，可能是多字 token（如"展开："、"站？"），
// 所以标点判定取 token 的末字符，宽度按 token 逐字累计，避免漏切与错算。
const PUNCT_END = "，、；;：:。！？…—–―";
function endsWithPunct(tok: string): boolean {
  return PUNCT_END.includes(tok[tok.length - 1]);
}
// 语义切分：遇到句末标点（句号、分号、问号）强制断开，序数词前也强制断开
const SENTENCE_END = "。；;？?！!";
const ORDINAL_WORDS = [
  "第一", "第二", "第三", "第四", "第五", "第六", "第七", "第八", "第九", "第十",
  "首先", "其次", "然后", "最后", "接着", "另外", "还有",
  "一是", "二是", "三是", "四是", "五是", "六是",
];
function isSentenceEnd(tok: string): boolean {
  return SENTENCE_END.includes(tok[tok.length - 1]);
}
function startsOrdinal(text: string): boolean {
  return ORDINAL_WORDS.some((o) => text.startsWith(o));
}
// 视觉宽度估算：CJK/全角≈1，ASCII≈0.55，空格≈0.3
function unitOf(ch: string): number {
  if (ch === " ") return 0.3;
  return ch.codePointAt(0)! < 0x2e80 ? 0.55 : 1;
}
function unitsOf(str: string): number {
  let s = 0;
  for (const ch of str) s += unitOf(ch);
  return s;
}
export function chunkLine(line: CapLine): Chunk[] {
  const toks = line.chars;
  const n = toks.length;
  const groups: [number, number][] = [];
  let i = 0;
  const CAP = 15; // R146 C1: 24→15, 单卡目标视觉宽度上限
  const MINF = 8; // 最小分片宽度，避免切出过碎残片
  while (i < n) {
    if (unitsOf(toks.slice(i, n).map((t) => t.c).join("")) <= CAP) {
      groups.push([i, n - 1]);
      break;
    }
    let cut = -1;
    let acc = 0;
    for (let t = i; t < n; t++) {
      acc += unitsOf(toks[t].c);
      // R146 C3: 序数词前强制断开（跨 token 检测）
      if (t > i) {
        const ahead = toks.slice(t, t + 2).map((x) => x.c).join("");
        if (startsOrdinal(ahead)) {
          cut = t - 1;
          break;
        }
      }
      if (acc > CAP + 16) break;
      // R146 C3: 句号/分号/问号强制断开（优先于普通标点）
      if (isSentenceEnd(toks[t].c) && acc >= MINF) {
        cut = t;
        break;
      }
      // 原逻辑：普通标点（逗号等）
      if (endsWithPunct(toks[t].c) && acc >= MINF &&
          unitsOf(toks.slice(t + 1, n).map((x) => x.c).join("")) >= MINF) {
        cut = t;
      }
    }
    if (cut < 0) {
      groups.push([i, n - 1]);
      break;
    }
    groups.push([i, cut]);
    i = cut + 1;
  }
  // 每片的起止帧按 token 帧对齐：前一卡在下一卡首个 token 起帧前一帧结束
  return groups.map(([a, b], gi) => {
    const start_frame = toks[a].f;
    const end_frame =
      gi + 1 < groups.length
        ? Math.max(start_frame, toks[groups[gi + 1][0]].f - 1)
        : line.end_frame;
    return {
      start_frame,
      end_frame,
      text: toks.slice(a, b + 1).map((t) => t.c).join(""),
    };
  });
}

// 预处理一次：把所有句按 R125 分句逻辑拍平成字幕卡片序列
const subtitle_chunks: Chunk[] = videoData.subtitle_lines.flatMap(chunkLine);

// ===== F2：进度条标签防重叠（估算标签像素宽，超出相邻节点间距则截断加省略号）=====
// 复用 chunkLine 的 unitOf 视觉宽度模型（CJK≈1、ASCII≈0.55），字号 18。
// 问题：示例篇第 5/6 章节点仅隔约 104px，而「使用雷区&常见问题」标签约 154px，
// 两标签居中必然重叠。此处按相邻节点间距算出每标签可用宽度，超出的标签截断。
function labelWidthPx(name: string): number {
  return unitsOf(name) * 18;
}
function truncateLabel(name: string, maxPx: number): string {
  if (unitsOf(name) * 18 <= maxPx) return name;
  const ell = "…";
  let s = "";
  for (const ch of name) {
    if (unitsOf(s + ch + ell) * 18 > maxPx) break;
    s += ch;
  }
  return s.length ? s + ell : name.slice(0, 1) + ell;
}

// 单场景：硬切换（ST3-P3b 第1项取消所有转场，不再淡入/缩放）
const Scene: React.FC<{ image: string; eff_role: string; fadeIn?: boolean; reserve?: number; leftOffset?: number; isOriginal?: boolean; noFade?: boolean }> = ({ image, eff_role, fadeIn, reserve, leftOffset = 0, isOriginal, noFade }) => {
  const isTitle = eff_role === "title";
  return (
    <AbsoluteFill style={{ background: isTitle ? COLOR_BG : "transparent", zIndex: 10 }}>
      {/* R125：插画整体上移——只画在 0..(1080-RESERVE) 区域，contain 适配不裁切主体 */}
      <div
        style={{
          position: "absolute",
          // E2: 插画区遵守安全区(上/左/右内缩)，底部仍由 reserve 预留字幕+进度条
          top: SAFE.top,
          left: SAFE.left + leftOffset,
          width: 1920 - SAFE.left - SAFE.right - leftOffset,
          height: 1080 - SAFE.top - (reserve ?? RESERVE),
          overflow: "hidden",
        }}
      >
        <Img
          src={staticFile(image)}
          style={{
            width: "100%",
            height: "100%",
            objectFit: "contain",
            objectPosition: isOriginal ? "center" : "center top",
          }}
        />
      </div>
    </AbsoluteFill>
  );
};

// 字幕卡片：R125 分句后整条显示；保留 R109 整句显示逻辑（无逐字动画）
// SHOT-M4-P3 B4: export 供截图镜头复用；新增可选 chunks 参数，缺省仍用全局 subtitle_chunks，逻辑不变。
export const Subtitle: React.FC<{ bottom?: number; chunks?: Chunk[] }> = ({ bottom = 120, chunks }) => {
  const frame = useCurrentFrame();
  let active: Chunk | null = null;
  for (const c of chunks ?? subtitle_chunks) {
    if (c.start_frame <= frame && frame <= c.end_frame) {
      active = c;
      break;
    }
  }
  if (!active) return null;
  return (
    <div
      style={{
        position: "absolute",
        bottom,
        left: 0,
        right: 0,
        display: "flex",
        justifyContent: "center",
        zIndex: 20,
      }}
    >
      <div
        style={{
          background: COLOR_SUB_BG,
          borderRadius: 16,
          border: `2px solid ${COLOR_SUB_BORDER}`,
          boxShadow: `6px 6px 0 0 ${COLOR_SUB_SHADOW}`,
          padding: "14px 40px",
          fontSize: FONT_SIZE,
          fontWeight: 700,
          color: COLOR_INK,
          fontFamily: TITLE_FONT,
          maxWidth: "82%",
          textAlign: "center",
          lineHeight: 1.32,
        }}
      >
        {active.text}
      </div>
    </div>
  );
};

// R150 X1: 场景软切淡入时长(帧)。ST3-P3b 第1项取消所有转场，改为 0（硬切换）。
const TRANSITION_DUR = 0;

// R148 E1: Timeline 进度条（按一级章节真实时长比例分段，当前章高亮）
// R150 X2: variant 支持三种布局——"bottom"(R148默认) / "A"(底部下移) / "B"(左侧竖排)
const ProgressBar: React.FC<{ variant?: "bottom" | "A" | "B" }> = ({ variant = "bottom" }) => {
  const frame = useCurrentFrame();
  const chapters = (videoData as any).chapters ?? [];
  if (chapters.length === 0) return null;
  const content = (videoData as any).content_frames ?? videoData.total_frames;
  let cur = 0;
  for (let i = 0; i < chapters.length; i++) {
    if (frame >= chapters[i].start_frame) cur = i;
  }
  const progress = Math.min(1, frame / content);

  // 方案 B：左侧竖排（从上往下推进，完全避开底部字幕）
  if (variant === "B") {
    const top = 100;
    const bottom = 220;
    const trackH = 1080 - top - bottom;
    const x = 80;
    const yOf = (f: number) => top + (f / content) * trackH;
    return (
      <div style={{ position: "absolute", left: 40, top, bottom, width: 220, zIndex: 25 }}>
        <div style={{ position: "absolute", left: x, top: 0, bottom: 0, width: 4, background: COLOR_TRACK, borderRadius: 2 }}>
          <div style={{ position: "absolute", left: 0, top: 0, width: 4, height: `${progress * 100}%`, background: COLOR_ACCENT, borderRadius: 2 }} />
        </div>
        {chapters.map((c: any, i: number) => {
          const y = yOf(c.start_frame) - top;
          const isCur = i === cur;
          const isDone = i < cur;
          const d = isCur ? 26 : 18;
          const ring = isCur ? 5 : 2;
          return (
            <div key={i} style={{ position: "absolute", left: x - d / 2, top: y - d / 2, width: d, height: d }}>
              <div style={{
                width: d, height: d, borderRadius: "50%",
                background: isDone ? COLOR_ACCENT : COLOR_NODE_IDLE,
                border: `${ring}px solid ${isCur ? COLOR_ACCENT : COLOR_RING}`,
                boxSizing: "border-box",
              }} />
              <div style={{
                position: "absolute", top: -d / 2 + 4, left: d + 10,
                whiteSpace: "nowrap", fontSize: 18, fontWeight: 600,
                fontFamily: TITLE_FONT,
                background: isCur ? COLOR_TAG_BG : "transparent",
                color: isCur ? COLOR_TAG_FG : COLOR_TAG_MUTED,
                padding: isCur ? "2px 8px" : "0",
                borderRadius: 6,
                maxWidth: 150, overflow: "hidden", textOverflow: "ellipsis",
              }}>
                {c.name}
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  // 方案 A / 默认：底部横排。E2: A 的 bottom 从 24 提到 SAFE.bottom(60)，避免被平台底部进度/点赞栏遮挡；
  // 横向 left/right 96 已 > SAFE 80，且涉及 R154 节点位置修复，保持不变。默认 R148 为 84(已 > SAFE)。
  const bottomOffset = variant === "A" ? SAFE.bottom : 84;
  const left = 96;
  const right = 96;
  const trackW = 1920 - left - right;
  // R154 W3: posOf 返回容器内相对位置(0..trackW)。此前含 left 偏移，节点被二次偏移 96px(≈5.6%≈43s)。
  const posOf = (f: number) => (f / content) * trackW;
  // F2: 相邻标签碰撞检测 + 截断。按 labelWidthPx 估算标签宽，超出相邻节点间距的标签截断加省略号。
  const nodeX = chapters.map((c: any) => posOf(c.start_frame));
  const LABEL_GAP = 12;   // 覆盖单个当前章高亮 padding(16px 半宽 +8px) + 4px 视觉留白
  const LABEL_MIN_W = 40; // 标签截断下限(再截无意义)
  const labelW = chapters.map((c: any) => Math.min(180, labelWidthPx(c.name)));
  for (let i = 0; i < labelW.length - 1; i++) {
    const gap = nodeX[i + 1] - nodeX[i];
    const budget = 2 * (gap - LABEL_GAP);
    const total = labelW[i] + labelW[i + 1];
    if (total > budget) {
      let overflow = total - budget;
      const wider = labelW[i] >= labelW[i + 1] ? i : i + 1;
      const narrower = wider === i ? i + 1 : i;
      const take = Math.min(overflow, labelW[wider] - LABEL_MIN_W);
      labelW[wider] -= take;
      overflow -= take;
      if (overflow > 0) labelW[narrower] = Math.max(LABEL_MIN_W, labelW[narrower] - overflow);
    }
  }
  const labels = chapters.map((c: any, i: number) => truncateLabel(c.name, labelW[i]));
  return (
    <div style={{ position: "absolute", left, right, bottom: bottomOffset, zIndex: 25, height: 60 }}>
      <div style={{ position: "relative", height: 4, background: COLOR_TRACK, borderRadius: 2, top: 13 }}>
        <div style={{ position: "absolute", left: 0, top: 0, height: 4, width: `${progress * 100}%`, background: COLOR_ACCENT, borderRadius: 2 }} />
      </div>
      {chapters.map((c: any, i: number) => {
        const x = posOf(c.start_frame);
        const isCur = i === cur;
        const isDone = i < cur;
        const d = isCur ? 26 : 18;
        const ring = isCur ? 5 : 2;
        return (
          <div key={i} style={{ position: "absolute", left: x - d / 2, top: 13 - d / 2, width: d, height: d }}>
            <div style={{
              width: d, height: d, borderRadius: "50%",
              background: isDone ? COLOR_ACCENT : COLOR_NODE_IDLE,
              border: `${ring}px solid ${isCur ? COLOR_ACCENT : COLOR_RING}`,
              boxSizing: "border-box",
            }} />
            <div style={{
              position: "absolute", top: d + 5, left: "50%", transform: "translateX(-50%)",
              whiteSpace: "nowrap", fontSize: 18, fontWeight: 600,
              fontFamily: TITLE_FONT,
              background: isCur ? COLOR_TAG_BG : "transparent",
              color: isCur ? COLOR_TAG_FG : COLOR_TAG_MUTED,
              padding: isCur ? "2px 8px" : "0",
              borderRadius: 6,
              maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis",
            }}>
              {labels[i]}
            </div>
          </div>
        );
      })}
    </div>
  );
};

// R148 N4-2: 片尾 3 秒黑场（不显示字幕/插画，留片尾引流扩展位）
const BlackBuffer: React.FC = () => {
  const frame = useCurrentFrame();
  const content = (videoData as any).content_frames ?? videoData.total_frames;
  if (frame < content) return null;
  return <AbsoluteFill style={{ background: COLOR_BLACK, zIndex: 50 }} />;
};

// ST3-P2：要素场景（代码卡/命令行/表格）。与普通内容场景一致做淡入；卡片居中置于上部区域。
const isElementScene = (s: any) =>
  s && (s.type === "code_card" || s.type === "terminal" || s.type === "table");

const ElementScene: React.FC<{ scene: any }> = ({ scene }) => {
  const frame = useCurrentFrame();
  // ST3-P3b 第3项：要素场景整屏不透明背景（COLOR_BG），卡片置于其上；取消淡入淡出（硬切换）。
  const inner =
    scene.type === "code_card" ? (
      <CodeCard scene={scene} />
    ) : scene.type === "terminal" ? (
      <TerminalWindow scene={scene} />
    ) : (
      <TableCard scene={scene} />
    );
  return (
    <div style={{ position: "absolute", top: 0, left: 0, right: 0, bottom: 0, background: COLOR_BG, zIndex: 15 }}>
      <ElementStage>{inner}</ElementStage>
    </div>
  );
};

const R125Video: React.FC<{ variant?: "bottom" | "A" | "B" }> = ({ variant = "A" }) => {
  // R153 T2：默认方案 A（插画区 0..780、字幕 bottom 120、进度条 bottom 24）；B 左侧竖排保留供参考
  const reserve = variant === "A" ? 300 : RESERVE;
  const subtitleBottom = variant === "A" ? 120 : 26;
  const leftOffset = variant === "B" ? 200 : 0;
  // P12c：hold 判定 = 同一画面身份延续。截图场景用 (shot.type, shot.group_seq) 区分概述/逐张，
  // 普通场景两者缺失（空串），退化为只比 image。
  const shotIdOf = (sc: any) => {
    const sh = sc?.shot;
    return sh ? `${sh.type}:${sh.group_seq}` : "";
  };
  // ST3-P2c：要素场景排到最后渲染（DOM 靠后即在上层），避免被重叠的截图/标题卡盖住。
  // 不影响「hold」判定：hold 只比较非要素场景的 image，要素挪到末尾不改变非要素之间的相邻关系。
  const orderedScenes = [...videoData.scenes].sort(
    (a, b) => (isElementScene(a) ? 1 : 0) - (isElementScene(b) ? 1 : 0)
  );
  return (
    <AbsoluteFill style={{ background: COLOR_BG }}>
      <FontFaces />
      <Audio src={staticFile(videoData.audio)} />
      {orderedScenes.map((s, i) => {
        const prev = i > 0 ? orderedScenes[i - 1] : null;
        // ST3-P2：要素场景不算「同画面延续」，始终淡入（避免与相邻场景误判为 hold）。
        const isHold = prev
          ? !isElementScene(s) && !isElementScene(prev) &&
            s.image === prev.image && shotIdOf(s) === shotIdOf(prev)
          : false; // 不出图组画面沿用前一张：同画面身份不淡入，避免闪烁
        const baseDur = Math.max(1, s.end_frame - s.start_frame);
        // 不使用转场：场景按帧区间首尾相接、硬切换，不再延长 seqDur。
        const seqDur = baseDur;
        return (
          <Sequence
            key={i}
            from={s.start_frame}
            durationInFrames={seqDur}
          >
            {(s as any).shot ? (
              <ScreenshotScene shot={(s as any).shot} />
            ) : isElementScene(s) ? (
              <ElementScene scene={s} />
            ) : (
              <Scene image={s.image} eff_role={s.eff_role} fadeIn={!isHold} reserve={reserve} leftOffset={leftOffset} isOriginal={(s as any).is_original} noFade={s.start_frame === 0} />
            )}
          </Sequence>
        );
      })}
      <Subtitle bottom={subtitleBottom} />
      <ProgressBar variant={variant} />
      <BlackBuffer />
    </AbsoluteFill>
  );
};

export const R125Root: React.FC = () => (
  <>
    <Composition
      id="R125-字幕调整"
      component={R125Video}
      durationInFrames={videoData.total_frames}
      fps={FPS}
      width={1920}
      height={1080}
    />
    <Composition
      id="R150-progress-A"
      component={R125Video}
      defaultProps={{ variant: "A" }}
      durationInFrames={videoData.total_frames}
      fps={FPS}
      width={1920}
      height={1080}
    />
    <Composition
      id="R150-progress-B"
      component={R125Video}
      defaultProps={{ variant: "B" }}
      durationInFrames={videoData.total_frames}
      fps={FPS}
      width={1920}
      height={1080}
    />
  </>
);
