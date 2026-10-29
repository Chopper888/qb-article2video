import React from "react";
import { useCurrentFrame } from "remotion";
import { TITLE_FONT } from "./fonts";
import { COLOR_BG, COLOR_ACCENT, COLOR_INK } from "./colors";
import { MONO_W, autoFontSize, wrapCommand, fitCard, scrollRowsOffset } from "./st3Text";

// =====================================================================
// ST3-P2：代码卡片 / 命令行窗口 / 表格卡片（收编自 ST3-P1 样片 1/3/4）。
//
// 这些组件只渲染新的场景类型（type=code_card/terminal/table），不影响既有
// image/screenshot 场景。场景由 R125Root 的 Sequence 包裹，组件内 useCurrentFrame()
// 返回的是**相对**帧（0 起），故高亮步骤的绝对 start_frame 需减去场景 start_frame。
// =====================================================================

// —— 代码卡配色（深色终端，含中性语法高亮）——
const CODE_BG = "#1E1E1E";
const CODE_HEADER = "#2D2D2D";
const KEY_COLOR = "#9CDCFE";
const STR_COLOR = "#CE9178";
const NUM_COLOR = "#B5CEA8";
const PUNCT_COLOR = "#D4D4D4";
const LINE_NO_COLOR = "#6E7681";
const HILITE_BG = "rgba(47,111,235,0.22)";

type Tok = { text: string; color: string };

function jsonTokens(line: string): Tok[] {
  const toks: Tok[] = [];
  let i = 0;
  const n = line.length;
  while (i < n) {
    const ch = line[i];
    if (ch === '"') {
      let j = i + 1;
      while (j < n && line[j] !== '"') j++;
      const str = line.slice(i, Math.min(j + 1, n));
      let k = Math.min(j + 1, n);
      while (k < n && line[k] === " ") k++;
      const isKey = k < n && line[k] === ":";
      toks.push({ text: str, color: isKey ? KEY_COLOR : STR_COLOR });
      i = Math.min(j + 1, n);
    } else if (/[{}\[\],]/.test(ch)) {
      toks.push({ text: ch, color: PUNCT_COLOR });
      i++;
    } else if (/[0-9]/.test(ch)) {
      let j = i;
      while (j < n && /[0-9a-zA-Z.\-]/.test(line[j])) j++;
      toks.push({ text: line.slice(i, j), color: NUM_COLOR });
      i = j;
    } else {
      toks.push({ text: ch, color: PUNCT_COLOR });
      i++;
    }
  }
  return toks;
}

// 当前高亮行（1-based → 0-based 下标集合）。ST3-P3 A6：一个高亮步骤含多行时，
// 按该步骤时长均分逐行点亮（"当前讲到的行"），避免整卡同时高亮（表格所有行一起粉）。
function currentHighlight(
  frame: number,
  scene: { start_frame?: number; end_frame?: number; highlight?: { start_frame: number; lines: number[] }[] }
): Set<number> {
  const start = scene.start_frame ?? 0;
  const end = scene.end_frame ?? start + 1;
  const steps = scene.highlight ?? [];
  let activeIdx = -1;
  for (let i = 0; i < steps.length; i++) {
    if (frame >= (steps[i].start_frame ?? 0) - start) activeIdx = i;
  }
  if (activeIdx < 0) return new Set();
  const lines = (steps[activeIdx].lines ?? []).map((x) => x - 1).filter((x) => x >= 0);
  if (lines.length <= 1) return new Set(lines);
  const stepStart = (steps[activeIdx].start_frame ?? 0) - start;
  const stepEnd =
    activeIdx + 1 < steps.length
      ? (steps[activeIdx + 1].start_frame ?? 0) - start
      : end - start;
  const dur = Math.max(1, stepEnd - stepStart);
  const idx = Math.min(lines.length - 1, Math.floor(((frame - stepStart) / dur) * lines.length));
  return new Set([lines[idx]]);
}

// 卡片内滚动（D.3）：内容超过可视行数时，随高亮行缓慢上滚，保证当前高亮行在可视区。
// 无高亮时按帧线性滚动到底部。
function scrollOffsetPx(
  frame: number,
  totalLines: number,
  visibleLines: number,
  lineHeight: number,
  hl: Set<number>,
  durFrames: number
): number {
  if (totalLines <= visibleLines) return 0;
  const maxScroll = (totalLines - visibleLines) * lineHeight;
  // 有高亮 → 跟随高亮行（取最后一个被高亮的行）
  const hlArr = Array.from(hl).sort((a, b) => a - b);
  if (hlArr.length > 0) {
    const focus = hlArr[hlArr.length - 1];
    return Math.min(maxScroll, Math.max(0, (focus - Math.floor(visibleLines / 2)) * lineHeight));
  }
  // 无高亮 → 线性缓慢滚动
  const p = Math.min(1, Math.max(0, frame / Math.max(1, durFrames)));
  return Math.round(maxScroll * p);
}

// ================= 代码卡片（样片1：整卡出现 + 行高亮） =================
// ST3-P3b 第3项：字号调大约 30%（26 → 34）
const CODE_FONT_SIZE = 34;
const CODE_LINE_HEIGHT = 1.55;

const CODE_MAX_VISIBLE = 14;

const CodeHeader: React.FC<{ lang: string }> = ({ lang }) => (
  <div
    style={{
      background: CODE_HEADER,
      borderTopLeftRadius: 14,
      borderTopRightRadius: 14,
      padding: "12px 22px",
      display: "flex",
      alignItems: "center",
      gap: 10,
    }}
  >
    <span style={{ width: 12, height: 12, borderRadius: "50%", background: "#FF5F57" }} />
    <span style={{ width: 12, height: 12, borderRadius: "50%", background: "#FEBC2E" }} />
    <span style={{ width: 12, height: 12, borderRadius: "50%", background: "#28C840" }} />
    <span style={{ marginLeft: 14, color: "#9DA5B4", fontSize: 16, fontFamily: TITLE_FONT }}>
      {lang || "text"}
    </span>
  </div>
);

export const CodeCard: React.FC<{ scene: any }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const lines: string[] = scene.lines ?? [];
  const hl = currentHighlight(frame, scene);
  // R8：行号栏 44+20、左右内边距 26×2，余下是代码能用的宽度；放大后一行必须放得下。
  // ST3-P16：整张卡片比要素舞台高就逐档缩小字号，缩到下限仍放不下就固定高度、卡片内滚动（fitCard）。
  const fit = fitCard("code", lines, autoFontSize(lines, CODE_FONT_SIZE, 1100 - 52 - 64));
  const codeFont = fit.font;
  const lineHeightPx = Math.round(codeFont * CODE_LINE_HEIGHT);
  // ST3-P12 第 5 步：和命令行卡片共用换行函数——只在空格处换行，单个词比一行还长才在中间断开。
  const codeCapEm = (1100 - 52 - 64) / codeFont;
  const durFrames = Math.max(1, (scene.end_frame ?? 1) - (scene.start_frame ?? 0));
  // 放得下的卡片照旧；缩小字号后放得下的整卡显示不滚动；滚动时高亮行居中（与表格卡片同一做法）。
  const hlLast = Array.from(hl).sort((a, b) => a - b).pop();
  const off =
    fit.mode === "fit"
      ? scrollOffsetPx(frame, lines.length, CODE_MAX_VISIBLE, lineHeightPx, hl, durFrames)
      : fit.mode === "scroll"
        ? scrollRowsOffset(fit, hlLast ?? null, frame, durFrames)
        : 0;
  const body = (
    <div style={{ transform: `translateY(-${off}px)` }}>
      {lines.map((ln, i) => (
        <div
          key={i}
          style={{
            display: "flex",
            background: hl.has(i) ? HILITE_BG : "transparent",
            padding: "0 26px",
          }}
        >
          <span
            style={{
              width: 44,
              textAlign: "right",
              color: LINE_NO_COLOR,
              userSelect: "none",
              paddingRight: 20,
              flexShrink: 0,
            }}
          >
            {i + 1}
          </span>
          <span style={{ whiteSpace: "pre" }}>
            {(ln.length ? wrapCommand(ln, codeCapEm, codeCapEm) : [""]).map((row, r) => (
              <div key={r} style={{ minHeight: lineHeightPx }}>
                {jsonTokens(row).map((t, j) => (
                  <span key={j} style={{ color: t.color }}>
                    {t.text}
                  </span>
                ))}
              </div>
            ))}
          </span>
        </div>
      ))}
    </div>
  );

  return (
    <div style={{ width: 1100, margin: "0 auto" }}>
      <CodeHeader lang={scene.lang ?? ""} />
      <div
        style={{
          borderBottomLeftRadius: 14,
          borderBottomRightRadius: 14,
          overflow: "hidden",
          background: CODE_BG,
          padding: "22px 0",
          fontFamily: "'Cascadia Code','Consolas','Menlo',monospace",
          fontSize: codeFont,
          lineHeight: CODE_LINE_HEIGHT,
        }}
      >
        {fit.mode === "scroll" ? (
          <div style={{ overflow: "hidden", height: fit.visibleRows * fit.rowH }}>{body}</div>
        ) : (
          body
        )}
      </div>
    </div>
  );
};

// ================= 命令行窗口（样片3：命令逐字打出） =================
type TermStep = { cmd: string; out: string[] };

// ST3-P3b 第3项：字号调大约 30%（24 → 31）
const TERM_FONT_SIZE = 31;

export const TerminalWindow: React.FC<{ scene: any }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const steps: TermStep[] = (scene.steps ?? []).filter((s: any) => s && (s.cmd ?? "").length > 0);
  const start = scene.start_frame ?? 0;
  const end = scene.end_frame ?? start + 1;
  const frames = Math.max(1, end - start);
  const total = steps.length;
  const rel = Math.max(0, frame);
  const perStep = frames / Math.max(1, total);

  let doneSteps = 0;
  let curTyped = 0;
  for (let i = 0; i < total; i++) {
    const t0 = i * perStep;
    const t1 = (i + 1) * perStep;
    if (rel >= t1) {
      doneSteps++;
    } else if (rel >= t0) {
      const stepLen = steps[i].cmd.length;
      const typeMs = Math.min(perStep * 0.75, stepLen / 22);
      const typed = rel - t0;
      curTyped = Math.min(stepLen, Math.floor((typed / Math.max(0.001, typeMs)) * stepLen));
      break;
    }
  }

  // R8：只有一两条命令时按最长一条放大字号（含提示符 "C:\> " 的 5 个字符）。
  const PROMPT = "C:\\> ";
  const innerW = 1120 - 26 * 2;
  // ST3-P16：全部命令打完后整张卡片比要素舞台高，就逐档缩小字号；缩到下限仍放不下就固定高度，
  // 随打字进度往上滚，正在打的那一行始终在卡片里。
  const fit = fitCard("terminal", steps.map((s) => s.cmd), autoFontSize(steps.map((s) => PROMPT + s.cmd), TERM_FONT_SIZE, innerW));
  const termFont = fit.font;
  // R3：只在空格处换行。先按整条命令排好行，再按打字进度逐字显示，打字过程中行不会跳。
  const capEm = innerW / termFont;
  const renderCmd = (cmd: string, typed: number, cursor: boolean) => {
    const rows = wrapCommand(cmd, capEm - PROMPT.length * MONO_W, capEm);
    let left = typed;
    const lastRow = Math.max(0, rows.findIndex((_r, i) => rows.slice(0, i + 1).join("").length >= typed));
    return rows.map((row, i) => {
      const show = row.slice(0, Math.max(0, left));
      left -= row.length;
      if (i > 0 && show.length === 0) return null;
      return (
        <div key={i} style={{ color: "#D4D4D4", whiteSpace: "pre" }}>
          {i === 0 && <span style={{ color: "#6ACB6A" }}>{PROMPT}</span>}
          {show}
          {cursor && i === lastRow && (
            <span
              style={{
                display: "inline-block",
                width: Math.round(termFont * 0.3),
                height: Math.round(termFont * 0.65),
                background: "#D4D4D4",
                marginLeft: 2,
                verticalAlign: "text-bottom",
              }}
            />
          )}
        </div>
      );
    });
  };

  const termBody = (
    <>
      {steps.slice(0, doneSteps).map((s, i) => (
        <div key={i}>
          {renderCmd(s.cmd, s.cmd.length, false)}
          {(s.out ?? []).map((o, j) => (
            <div key={j} style={{ color: "#8B949E" }}>
              {o}
            </div>
          ))}
        </div>
      ))}
      {doneSteps < total && renderCmd(steps[doneSteps].cmd, curTyped, true)}
    </>
  );
  // 滚动模式：已经显示出来的行数超过可见行数，就把多出来的往上推。
  let termOff = 0;
  if (fit.mode === "scroll") {
    let shown = fit.rows.slice(0, doneSteps).reduce((a, b) => a + b, 0);
    if (doneSteps < total) {
      const rows = wrapCommand(steps[doneSteps].cmd, capEm - PROMPT.length * MONO_W, capEm);
      let acc = 0;
      let n = 1;
      for (let i = 0; i < rows.length; i++) {
        if (i > 0 && acc >= curTyped) break;
        n = i + 1;
        acc += rows[i].length;
      }
      shown += n;
    }
    termOff = Math.max(0, shown - fit.visibleRows) * fit.rowH;
  }

  return (
    <div
      style={{
        width: 1120,
        margin: "0 auto",
        background: "#0D1117",
        borderRadius: 14,
        fontFamily: "'Cascadia Code','Consolas','Menlo',monospace",
        fontSize: termFont,
        lineHeight: 1.6,
        boxShadow: "0 18px 50px rgba(0,0,0,0.25)",
      }}
    >
      <div
        style={{
          background: "#1F2428",
          borderTopLeftRadius: 14,
          borderTopRightRadius: 14,
          padding: "10px 18px",
          color: "#9DA5B4",
          fontSize: 15,
          fontFamily: TITLE_FONT,
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span style={{ width: 11, height: 11, borderRadius: "50%", background: "#FF5F57" }} />
        <span style={{ width: 11, height: 11, borderRadius: "50%", background: "#FEBC2E" }} />
        <span style={{ width: 11, height: 11, borderRadius: "50%", background: "#28C840" }} />
        <span style={{ marginLeft: 12 }}>命令提示符</span>
      </div>
      <div style={{ padding: "20px 26px 26px" }}>
        {fit.mode === "scroll" ? (
          <div style={{ overflow: "hidden", height: fit.visibleRows * fit.rowH }}>
            <div style={{ transform: `translateY(-${termOff}px)` }}>{termBody}</div>
          </div>
        ) : (
          termBody
        )}
      </div>
    </div>
  );
};

// ================= 表格卡片（样片4：逐行高亮 + 高表滚动） =================
const TABLE_MAX_VISIBLE = 8;
const TABLE_ROW_HEIGHT = 64;

export const TableCard: React.FC<{ scene: any }> = ({ scene }) => {
  const frame = useCurrentFrame();
  const table = scene.table ?? { header: [], rows: [] };
  const rows: string[][] = table.rows ?? [];
  const header: string[] = table.header ?? [];
  const hl = currentHighlight(frame, scene);
  const durFrames = Math.max(1, (scene.end_frame ?? 1) - (scene.start_frame ?? 0));
  const off = scrollOffsetPx(frame, rows.length, TABLE_MAX_VISIBLE, TABLE_ROW_HEIGHT, hl, durFrames);

  return (
    <div
      style={{
        width: 1000,
        margin: "0 auto",
        background: "#FFFFFF",
        borderRadius: 16,
        border: "2px solid #E3E4E8",
        overflow: "hidden",
        boxShadow: "0 14px 40px rgba(0,0,0,0.08)",
        fontFamily: TITLE_FONT,
      }}
    >
      {/* ST3-P2c 裁定2：表头固定不随高亮滚动，观众始终看到列名 */}
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 32, tableLayout: "fixed" }}>
        <thead>
          <tr style={{ background: COLOR_INK, color: "#FFFFFF" }}>
            {header.map((h, i) => (
              <th
                key={i}
                style={{
                  padding: "18px 28px",
                  textAlign: "left",
                  fontWeight: 700,
                  fontSize: 30,
                }}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
      </table>
      {/* ST3-P3 A6：滚动容器固定可视高度（整行对齐，滚不过最后一行、底部不留空白） */}
      <div style={{ overflow: "hidden", height: TABLE_MAX_VISIBLE * TABLE_ROW_HEIGHT }}>
        <div style={{ transform: `translateY(-${off}px)` }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 32, tableLayout: "fixed" }}>
            <tbody>
              {rows.map((row, ri) => (
                <tr
                  key={ri}
                  style={{
                    height: TABLE_ROW_HEIGHT,
                    background: hl.has(ri) ? "rgba(232,101,90,0.12)" : "transparent",
                    borderTop: "1px solid #EDEEF1",
                    color: COLOR_INK,
                  }}
                >
                  {row.map((c, ci) => (
                    <td
                      key={ci}
                      style={{
                        padding: "0 28px",
                        verticalAlign: "middle",
                        fontWeight: ci === 0 ? 600 : 400,
                        color: ci === 0 ? COLOR_ACCENT : COLOR_INK,
                        fontSize: ci === 0 ? 30 : 28,
                      }}
                    >
                      {c}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};

// ================= 要素舞台：居中、上部区域、字幕在下 =================
// ST3-P3 A5：卡片高度小于可用区域时，在内容区域内垂直居中（不再贴顶留下大片空白）。
export const ElementStage: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div
    style={{
      position: "absolute",
      top: 90,
      left: 0,
      right: 0,
      height: 690,
      display: "flex",
      justifyContent: "center",
      alignItems: "center",
    }}
  >
    {children}
  </div>
);

export { COLOR_BG, COLOR_ACCENT, COLOR_INK };
