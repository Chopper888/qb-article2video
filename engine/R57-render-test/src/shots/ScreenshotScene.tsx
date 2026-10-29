import React, { useEffect, useState } from "react";
import {
  AbsoluteFill,
  cancelRender,
  continueRender,
  delayRender,
  Img,
  staticFile,
  useCurrentFrame,
} from "remotion";
import { FontFaces, TITLE_FONT } from "../fonts";
import {
  COLOR_BG,
  COLOR_INK,
  COLOR_TAG_MUTED,
  COLOR_TRACK,
  COLOR_RING,
} from "../colors";
import { shotImageMap } from "./shots_data";

// 高亮框 / 箭头 / 圆点在 src/shots 内本地定义（与 pipeline/shot_director.py 同步）
const HIGHLIGHT_BLUE = "#2F6FEB";
const ARROW_BLUE = "#1B4FBF";

const EB_FONT = 28, EB_LH = 36;
const TITLE_FONT_SIZE = 64, TITLE_LH = 80;
const ITEM_FONT = 40, ITEM_LH = 88;
const DOT_W = 10, ITEM_GAP = 14;
const PAR_LABEL_LH = 56;

const easeOutCubic = (t: number) => 1 - Math.pow(1 - t, 3);


// ST3-P3 B2：竖长截图滚动显示。开头静止 1 秒，线性匀速往下滚，结尾静止 1 秒；
// 速度用「每滚过一屏需要几秒」表示（sec_per_screen）。时长不够滚完整张图时只滚前面几屏、
// 不加速；一屏都滚不完时静止显示顶部这一屏。红框本轮不显示（见回执）。
const SCROLL_FPS = 30;
const SCROLL_HOLD = SCROLL_FPS; // 1 秒

const ScrollImage: React.FC<{ el: any; shot: any }> = ({ el, shot }) => {
  const frame = useCurrentFrame();
  const r = el.rect;
  const sc = el.scroll;
  const viewW = r.w;
  const viewH = r.h;
  const scaledH = sc.img_h * (viewW / sc.img_w);
  const maxScroll = Math.max(0, scaledH - viewH);
  const secPerScreen = sc.sec_per_screen;
  const speed = viewH / (secPerScreen * SCROLL_FPS);
  const duration = Math.max(1, (shot.timing?.duration_frames ?? 1) + (shot.lead_in ?? 0));
  const available = duration - 2 * SCROLL_HOLD;
  let scrollY = 0;
  if (available >= secPerScreen * SCROLL_FPS) {
    const dist = Math.min(maxScroll, speed * available);
    scrollY = Math.min(dist, Math.max(0, (frame - SCROLL_HOLD) * speed));
  }
  const pos = {
    position: "absolute" as const,
    left: el.rel_x != null ? el.rel_x : r.x,
    top: el.rel_y != null ? el.rel_y : r.y,
    width: viewW,
    height: viewH,
    overflow: "hidden" as const,
  };
  return (
    <div style={{ ...pos, overflow: "hidden" }}>
      <Img
        src={staticFile((shotImageMap as Record<string, string>)[el.image] ?? el.image)}
        style={{ position: "absolute", top: -scrollY, left: 0, width: viewW, height: scaledH }}
      />
    </div>
  );
};

function ArrowShape({ w, h, direction, color }: { w: number; h: number; direction: string; color: string }) {
  const SHAFT = 52, HEAD = 28, SW = 8;
  if (direction === "left" || direction === "right") {
    const flip = direction === "right";
    return <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}><rect x={flip ? HEAD : 0} y={10} width={SHAFT} height={SW} fill={color} /><polygon points={flip ? "0,0 0,28 28,14" : "80,0 80,28 52,14"} fill={color} /></svg>;
  } else {
    const flip = direction === "down";
    return <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}><rect x={10} y={flip ? HEAD : 0} width={SW} height={SHAFT} fill={color} /><polygon points={flip ? "0,0 28,0 14,28" : "0,80 28,80 14,52"} fill={color} /></svg>;
  }
}

// 截图镜头视觉组件：只画画面元素（卡片/标题栏/侧栏/红框/箭头/并列标签），不含字幕与音频。
// 元素位置只读 rect / rel_x / rel_y，无坐标加减；元素都是真实定位容器，不用 Fragment。
export const ScreenshotScene: React.FC<{ shot: any; grayImages?: boolean }> = ({ shot, grayImages }) => {
  const frame = useCurrentFrame();
  const d = shot;
  const leadIn = d.lead_in;
  const entrance = Math.min(1, frame / leadIn);
  const e = easeOutCubic(entrance);

  const [handle] = useState(() => delayRender("shot-layout-check"));
  useEffect(() => {
    const errs: string[] = [];
    if (!d.self_check || !d.self_check.pass) errs.push("自检未通过: " + JSON.stringify(d.self_check?.errors));
    if (d.layout?.cardW != null && d.layout.cardW > 0 && d.layout.cardW < 200) errs.push("截图卡过窄 " + d.layout.cardW);
    if (errs.length > 0) cancelRender(new Error("排版自检失败:\n" + errs.join("\n")));
    else continueRender(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handle]);

  // ST3-P12：标注空档插画之后回到这张截图的那一段（resumed），之前出过的内容直接在，不再淡入。
  const isFirstInGroup = (d.group_seq ?? 0) === 0 && !d.resumed;
  const headP = isFirstInGroup ? Math.min(1, Math.max(0, (frame - 8) / 8)) : 1;
  const els = d.elements || [];

  function renderEl(el: any, parentRect?: any): React.ReactNode {
    const r = el.rect;
    const left = el.rel_x != null ? el.rel_x : r.x;
    const top = el.rel_y != null ? el.rel_y : r.y;
    const pos = { position: "absolute" as const, left, top, width: r.w, height: r.h };
    const children = els.filter((c: any) => c.parent === el.id);
    switch (el.kind) {
      case "card":
        return <div key={el.id} style={{ ...pos, background: "#FFFFFF", borderRadius: 16, border: `1px solid ${COLOR_RING}`, boxShadow: "0 8px 24px rgba(0,0,0,0.08)", opacity: e, overflow: "hidden", zIndex: 10 }}>{children.map((c: any) => renderEl(c, r))}</div>;
      case "titlebar":
        return <div key={el.id} style={{ ...pos, background: COLOR_TRACK, display: "flex", alignItems: "center", paddingLeft: 16, paddingRight: 16, boxSizing: "border-box" }}>{children.map((c: any) => renderEl(c, r))}</div>;
      case "titlebar-text":
        return <div key={el.id} style={{ ...pos, fontSize: 22, color: COLOR_INK, fontFamily: TITLE_FONT, lineHeight: r.h + "px", whiteSpace: "nowrap", display: "flex", alignItems: "center" }}>{el.text}</div>;
      case "image":
        if (el.scroll) {
          return <ScrollImage el={el} shot={d} />;
        }
        if (grayImages) {
          return <div key={el.id} style={{ ...pos, background: "#808080" }}>{children.map((c: any) => renderEl(c, r))}</div>;
        }
        if (el.crop) {
          return <div key={el.id} style={{ ...pos, overflow: "hidden" }}><Img src={staticFile((shotImageMap as Record<string, string>)[el.image] ?? el.image)} style={{ position: "absolute", left: -(el.crop.x || 0) * el.crop.scale, top: -el.crop.y * el.crop.scale, width: el.src_w * el.crop.scale, height: el.src_h * el.crop.scale }} />{children.map((c: any) => renderEl(c, r))}</div>;
        }
        return <div key={el.id} style={{ ...pos, overflow: "hidden" }}><Img src={staticFile((shotImageMap as Record<string, string>)[el.image] ?? el.image)} style={{ width: "100%", height: "100%", objectFit: "contain" }} />{children.map((c: any) => renderEl(c, r))}</div>;
      case "highlight": {
        // A3: 入场动画（淡入 + 缩放 1.08→1.00，以红框中心为基准）
        const anim = el.anim;
        let opacity = 1;
        let scale = 1;
        if (el.grid_border) {
          // SHOT-M5-P5 概述层高亮：只淡入淡出、不缩放，在 show_start..show_start+show_duration 内可见
          const s = el.show_start ?? 0;
          const d = el.show_duration ?? 0;
          const fade = el.fade_frames ?? 5;
          const pIn = Math.min(1, Math.max(0, (frame - s) / fade));
          const pOut = Math.min(1, Math.max(0, (s + d - frame) / fade));
          opacity = Math.min(pIn, pOut);
          scale = 1;
        } else if (anim) {
          const p = Math.min(1, Math.max(0, (frame - anim.start_frame) / anim.duration));
          opacity = anim.fade ? p : 1;
          scale = anim.scale_from - (anim.scale_from - anim.scale_to) * p;
        }
        const borderW = el.grid_border ? 8 : 4;
        return <div key={el.id} style={{ ...pos, border: `${borderW}px solid ${el.color || HIGHLIGHT_BLUE}`, borderRadius: 8, zIndex: 12, opacity, transform: `scale(${scale})` }} />;
      }
      case "arrow":
        return <div key={el.id} style={{ ...pos, zIndex: 13 }}><ArrowShape w={r.w} h={r.h} direction={el.direction} color={el.color || ARROW_BLUE} /></div>;
      case "eyebrow":
        return <div key={el.id} style={{ ...pos, fontSize: EB_FONT, lineHeight: EB_LH + "px", color: COLOR_TAG_MUTED, fontFamily: TITLE_FONT, whiteSpace: "nowrap", zIndex: 11, opacity: headP }}>{el.text}</div>;
      case "title":
        return <div key={el.id} style={{ ...pos, zIndex: 11, opacity: headP }}>{(Array.isArray(el.text) ? el.text : [el.text]).map((line: string, i: number) => <div key={i} style={{ fontSize: el.font ?? TITLE_FONT_SIZE, lineHeight: (el.lh ?? TITLE_LH) + "px", color: COLOR_INK, fontFamily: TITLE_FONT, fontWeight: 700, whiteSpace: "nowrap" }}>{line}</div>)}</div>;
      case "item": {
        const idx = parseInt(el.id.split("-")[1] || "0", 10);
        const itemData = (d.items || [])[idx];
        const anchorRel = itemData?.anchor_rel;
        let p: number;
        if (!isFirstInGroup && !d.annot && anchorRel != null && anchorRel < 0) {
          p = 1; // 同组后续镜头：切换时刻前已完整显示的条目，第 0 帧起不透明度为 1，不淡入
        } else if (d.resumed && anchorRel != null && anchorRel < 0) {
          p = 1; // 回到这张截图：之前出过的条目直接在
        } else {
          const start = Math.max(leadIn, anchorRel != null ? anchorRel : 0);
          p = Math.min(1, Math.max(0, (frame - start) / 10));
        }
        return <div key={el.id} style={{ ...pos, display: "flex", alignItems: "center", lineHeight: ITEM_LH + "px", zIndex: 11, opacity: p }}><span style={{ width: DOT_W, height: DOT_W, borderRadius: "50%", background: ARROW_BLUE, marginRight: ITEM_GAP, flexShrink: 0 }} /><span style={{ fontSize: ITEM_FONT, color: COLOR_INK, fontFamily: TITLE_FONT, whiteSpace: "nowrap" }}>{el.text}</span></div>;
      }
      case "cmd": {
        // ST3-P11 R3：左侧命令框（深色底）。在它对应的口播句开始时出现（anchor_rel，镜头内相对帧）。
        // 字号、行高、内边距、每一行的文字都由排版端给（el.font / el.lh / el.pad_x / el.lines），这里不再自己换行。
        const start = Math.max(leadIn, el.anchor_rel != null ? el.anchor_rel : 0);
        const p = d.resumed && el.anchor_rel != null && el.anchor_rel < 0 ? 1 : Math.min(1, Math.max(0, (frame - start) / 10));
        const rows: string[] = el.lines ?? [el.text];
        const lh = el.lh ?? Math.round(el.font * 1.4);
        return <div key={el.id} style={{ ...pos, background: "#0D1117", borderRadius: 14, boxSizing: "border-box", padding: `0 ${el.pad_x ?? 20}px`, display: "flex", alignItems: "center", zIndex: 11, opacity: p, boxShadow: "0 8px 24px rgba(0,0,0,0.18)" }}><span style={{ color: "#6ACB6A", fontFamily: TITLE_FONT, fontSize: el.font, lineHeight: lh + "px", marginRight: el.prompt_gap ?? 12, flexShrink: 0, alignSelf: "flex-start", marginTop: (r.h - lh * rows.length) / 2 }}>&gt;</span><span style={{ color: "#E6EDF3", fontFamily: TITLE_FONT, fontSize: el.font, lineHeight: lh + "px", whiteSpace: "pre" }}>{rows.map((row, i) => <div key={i}>{row}</div>)}</span></div>;
      }
      case "step":
        return <div key={el.id} style={{ ...pos, display: "flex", alignItems: "center", lineHeight: ITEM_LH + "px", zIndex: 11, opacity: headP }}><span style={{ width: DOT_W, height: DOT_W, borderRadius: "50%", background: ARROW_BLUE, marginRight: ITEM_GAP, flexShrink: 0 }} /><span style={{ fontSize: ITEM_FONT, color: el.active ? COLOR_INK : "#9aa0a6", fontFamily: TITLE_FONT, whiteSpace: "nowrap" }}>{el.text}</span></div>;
      case "label":
        return <div key={el.id} style={{ ...pos, display: "flex", alignItems: "center", justifyContent: "center", lineHeight: PAR_LABEL_LH + "px", zIndex: 11, opacity: headP }}><span style={{ width: DOT_W, height: DOT_W, borderRadius: "50%", background: ARROW_BLUE, marginRight: ITEM_GAP, flexShrink: 0 }} /><span style={{ fontSize: ITEM_FONT, color: COLOR_INK, fontFamily: TITLE_FONT }}>{el.text}</span></div>;
      case "hint-card":
        return <div key={el.id} style={{ ...pos, background: "#FFFFFF", borderRadius: 16, border: `2px solid ${COLOR_RING}`, opacity: e, zIndex: 10 }}>{children.map((c: any) => renderEl(c, r))}</div>;
      case "hint-triangle":
        return <div key={el.id} style={{ ...pos, zIndex: 11 }}><svg width={r.w} height={r.h} viewBox={`0 0 ${r.w} ${r.h}`}><polygon points="20,10 20,110 105,60" fill={ARROW_BLUE} /></svg></div>;
      case "hint-text":
        return <div key={el.id} style={{ ...pos, fontSize: ITEM_FONT, color: COLOR_INK, fontFamily: TITLE_FONT, textAlign: "center", lineHeight: r.h + "px", whiteSpace: "nowrap", zIndex: 11 }}>{el.text}</div>;
      default:
        return null;
    }
  }

  const topLevel = els.filter((el: any) => !el.parent);

  return (
    <AbsoluteFill style={{ zIndex: 10 }}>
      {/* B2：底色随入场不透明度淡入（与卡片一致），消除进入截图时底色已实心、卡片尚未显现的单帧空白 */}
      <AbsoluteFill style={{ background: COLOR_BG, opacity: e }} />
      <FontFaces />
      {topLevel.map((el: any) => renderEl(el))}
    </AbsoluteFill>
  );
};
