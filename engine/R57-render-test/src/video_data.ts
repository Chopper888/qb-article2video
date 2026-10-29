// 占位数据：每次出片时由引擎按文章生成并覆盖本文件。
export type SubtitleChar = { c: string; f: number };
export type SubtitleLine = { start_frame: number; end_frame: number; chars: SubtitleChar[] };
export type SceneEntry = {
  group_id: string;
  eff_role: string;
  image: string;
  start_frame: number;
  end_frame: number;
  is_original?: boolean;
  transition?: string;
  transition_kind?: string;
  shot?: unknown;
};
export type ChapterEntry = {
  name: string;
  start_time: number;
  end_time: number;
  start_frame: number;
  end_frame: number;
};

export const videoData = {
  fps: 30,
  total_frames: 30,
  content_frames: 30,
  audio: "audio.wav",
  scenes: [] as SceneEntry[],
  subtitle_lines: [] as SubtitleLine[],
  chapters: [] as ChapterEntry[],
};
