"""S1-R4 prototype: narration -> tts_input_segments.json.

Reproduces the production segmentation logic from
phase5.4hf-m7-t0-article2-preflight/m7-t0-b-full-tts/generate_full_tts.py:52-74
(paragraph split on \\n, then sentence split on 。！？). No TTS inference here;
only the structural fields needed for semantic annotation are filled.
"""
from __future__ import annotations
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NARRATION = HERE / "narration" / "narration_candidate.md"
OUT = HERE / "tts_input_segments.json"

PRON_RULES = []  # prototype: no pronunciation normalization rules needed


def normalize(text):
    applied = []
    result = text
    for src, spk in PRON_RULES:
        if src in result:
            result = result.replace(src, spk)
            applied.append({"source": src, "spoken": spk})
    return result, applied


def split_narration(narration: str) -> list[dict]:
    raw = narration.replace("\n\n", "\n").replace("\r", "")
    paragraphs = [p.strip() for p in raw.split("\n") if p.strip()]
    segments = []
    seg_id = 0
    for pi, para in enumerate(paragraphs):
        sentences = re.split(r"(?<=[。！？])(?=\S)", para)
        for si, sent in enumerate(sentences):
            sent = sent.strip()
            if not sent:
                continue
            tts_text, norms = normalize(sent)
            segments.append({
                "segment_id": f"seg-{seg_id:04d}",
                "paragraph_index": pi,
                "sentence_index": si,
                "source_text": sent,
                "tts_spoken_text": tts_text,
                "normalizations_applied": norms,
                "expected_boundary_type": "paragraph" if si == 0 else "sentence",
                "status": "segmentation_only_no_tts",
            })
            seg_id += 1
    return segments


def main() -> int:
    narration = NARRATION.read_text(encoding="utf-8").strip()
    segments = split_narration(narration)
    payload = {
        "schema": "tts-input-segments-v1",
        "producer": "S1-R4-prototype-splitter",
        "segmentation_source": "phase5.4hf-m7-t0-article2-preflight/m7-t0-b-full-tts/generate_full_tts.py:52-74",
        "narration_path": str(NARRATION),
        "narration_sha256": hashlib.sha256(narration.encode("utf-8")).hexdigest(),
        "total_segments": len(segments),
        "segments": segments,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"segments={len(segments)} -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
