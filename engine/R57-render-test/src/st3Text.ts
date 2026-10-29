// ST3-P11：要素卡片用到的几个纯函数（字号自适应、命令行 / 代码卡片换行）。
// 不依赖 React / Remotion，单元测试（tests/test_render_helpers.py）直接转译后调用。

// ST3-P11 R8：卡片只有一两行时，字号按最长一行自动放大，让文字占卡片宽度约七成，上限 56 号。
// 三行及以上保持原字号。等宽字体：英文字符宽 0.6 个字号，汉字等全角字符宽 1 个字号。
export const AUTO_FONT_MAX = 56;
export const AUTO_FONT_FILL = 0.7;
export const MONO_W = 0.6;
export const charEm = (ch: string) => (ch.charCodeAt(0) < 128 ? MONO_W : 1);
export const textEm = (s: string) => Array.from(s).reduce((n, ch) => n + charEm(ch), 0);

export function autoFontSize(lines: string[], base: number, cardWidth: number): number {
  const shown = lines.filter((l) => l.length > 0);
  if (shown.length === 0 || shown.length > 2) return base;
  const widest = Math.max(...shown.map(textEm));
  if (widest <= 0) return base;
  return Math.max(base, Math.min(AUTO_FONT_MAX, Math.floor((AUTO_FONT_FILL * cardWidth) / widest)));
}

// ST3-P11 R3：命令行换行——只在空格处换行；单个词比一行还长时才在中间断开。
// ST3-P12 第 5 步：代码卡片也用这个函数（原来是浏览器按字符硬断，会把 wheel 断成 w / heel）。
// firstEm / restEm 是第一行和后续各行能放下的宽度（单位：字号）。返回各行文字（空格留在行尾）。
export function wrapCommand(cmd: string, firstEm: number, restEm: number): string[] {
  const words = cmd.match(/\S+\s*|\s+/g) ?? [];
  const out: string[] = [];
  let cur = "";
  let curEm = 0;
  const cap = () => (out.length === 0 ? firstEm : restEm);
  for (const w of words) {
    const wEm = textEm(w.replace(/\s+$/, ""));
    if (cur && curEm + wEm > cap()) {
      out.push(cur);
      cur = "";
      curEm = 0;
    }
    if (wEm > cap()) {
      // 单个词比一行还长：逐字填满再断
      for (const ch of Array.from(w)) {
        if (cur && curEm + charEm(ch) > cap()) {
          out.push(cur);
          cur = "";
          curEm = 0;
        }
        cur += ch;
        curEm += charEm(ch);
      }
    } else {
      cur += w;
      curEm += textEm(w);
    }
  }
  if (cur) out.push(cur);
  return out;
}

// ST3-P16：代码卡片 / 命令行卡片放不下时自动适配。
// 按现有规则（字号、只在空格处换行）排好后，整张卡片比要素舞台（ElementStage，高 690）高，就逐档缩小字号
// 直到放得下；缩到下限还放不下，就固定卡片高度、卡片内滚动（和表格卡片一样）。放得下的卡片一律不变。
export const STAGE_H = 690;
export const FIT_FONT_STEPS = [32, 30, 28, 26, 24];
export const FIT_FONT_MIN = 24;

// 两种卡片的版式常数（与 ST3Components.tsx 一致）：
//   代码卡片：宽 1100，行号栏 44+20、左右内边距 26×2 → 代码宽 984；行高 1.55（每行 div 最小高度取整）；
//            标题栏 = 上下内边距 12×2 + 16 号字体的默认行高（16×1.184≈19）= 43；正文上下内边距 22×2。
//   命令行卡片：宽 1120，左右内边距 26×2 → 1068；行高 1.6；每条命令第一行让出提示符 "C:\> "（5 个字符）；
//            标题栏 = 10×2 + 15×1.6 = 44；正文内边距 20 + 26。
export const CARD_SPECS = {
  code: { innerW: 1100 - 52 - 64, lineH: 1.55, roundRow: true, firstLessEm: 0, chrome: 43 + 44 },
  terminal: { innerW: 1120 - 26 * 2, lineH: 1.6, roundRow: false, firstLessEm: 5 * MONO_W, chrome: 44 + 46 },
};
export type CardKind = keyof typeof CARD_SPECS;

export type CardFit = {
  font: number;            // 用的字号
  mode: "fit" | "shrink" | "scroll";   // 原样放得下 / 缩小字号后放得下 / 缩到下限仍放不下、滚动
  rowH: number;            // 一个显示行的高度（px）
  rows: number[];          // 每一行原文折成几个显示行
  height: number;          // 卡片全部展开的高度（px）
  visibleRows: number;     // 滚动时卡片里能看到几个显示行（不滚动时 = 总显示行数）
};

export function cardRowHeight(kind: CardKind, font: number): number {
  const s = CARD_SPECS[kind];
  return s.roundRow ? Math.max(Math.round(font * s.lineH), font * s.lineH) : font * s.lineH;
}

export function cardRows(kind: CardKind, lines: string[], font: number): number[] {
  const s = CARD_SPECS[kind];
  const cap = s.innerW / font;
  return lines.map((ln) => (ln.length ? Math.max(1, wrapCommand(ln, cap - s.firstLessEm, cap).length) : 1));
}

export function cardHeight(kind: CardKind, lines: string[], font: number): number {
  const n = cardRows(kind, lines, font).reduce((a, b) => a + b, 0);
  return CARD_SPECS[kind].chrome + n * cardRowHeight(kind, font);
}

export function fitCard(kind: CardKind, lines: string[], base: number, stageH: number = STAGE_H): CardFit {
  const make = (font: number, mode: CardFit["mode"]): CardFit => {
    const rows = cardRows(kind, lines, font);
    const rowH = cardRowHeight(kind, font);
    const total = rows.reduce((a, b) => a + b, 0);
    const visible =
      mode === "scroll" ? Math.max(1, Math.floor((stageH - CARD_SPECS[kind].chrome) / rowH)) : total;
    return { font, mode, rowH, rows, height: CARD_SPECS[kind].chrome + total * rowH, visibleRows: visible };
  };
  if (cardHeight(kind, lines, base) <= stageH) return make(base, "fit");
  for (const f of FIT_FONT_STEPS.filter((x) => x < base && x >= FIT_FONT_MIN)) {
    if (cardHeight(kind, lines, f) <= stageH) return make(f, "shrink");
  }
  return make(Math.min(base, FIT_FONT_MIN), "scroll");
}

// 滚动模式的上移量（px）：有高亮时让高亮的那一行（含它折出来的几行）居中，没有高亮按时间线性滚到底
// （与表格卡片 scrollOffsetPx 同一个做法，只是单位换成显示行）。
export function scrollRowsOffset(
  fit: CardFit,
  focusLine: number | null,
  frame: number,
  durFrames: number
): number {
  const total = fit.rows.reduce((a, b) => a + b, 0);
  if (total <= fit.visibleRows) return 0;
  const maxRows = total - fit.visibleRows;
  if (focusLine !== null && focusLine >= 0 && focusLine < fit.rows.length) {
    const start = fit.rows.slice(0, focusLine).reduce((a, b) => a + b, 0);
    const want = start - Math.floor((fit.visibleRows - fit.rows[focusLine]) / 2);
    return Math.min(maxRows, Math.max(0, want)) * fit.rowH;
  }
  const p = Math.min(1, Math.max(0, frame / Math.max(1, durFrames)));
  return Math.round(maxRows * fit.rowH * p);
}
