from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass
class Segment:
    image: Path
    duration: float
    subtitle: str
    slide_id: int
    segment_index: int
    start: float = 0.0
    end: float = 0.0


@dataclass
class AudioUnit:
    audio: Path
    duration: float
    subtitle: str
    related_element_id: str
    slide_id: int
    segment_index: int
    is_pause: bool = False


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def get_audio_duration(path: Path) -> float:
    from mutagen.mp3 import MP3

    return float(MP3(path).info.length)


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def cache_fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def audio_cache_valid(audio_path: Path, meta_path: Path, payload: dict) -> bool:
    if not audio_path.exists() or audio_path.stat().st_size == 0 or not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return meta.get("fingerprint") == cache_fingerprint(payload)


def write_audio_cache_meta(meta_path: Path, payload: dict) -> None:
    meta_path.write_text(
        json.dumps(
            {
                "fingerprint": cache_fingerprint(payload),
                "payload": payload,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


async def synthesize_slide_audio(
    slides: list[dict],
    audio_dir: Path,
    voice: str,
    rate: str,
    force: bool,
    provider: str,
    sapi_voice: str,
    ffmpeg: str,
    dashscope_voice_id: str,
    dashscope_model: str,
    dashscope_instruction: str,
) -> list[Path]:
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_files: list[Path] = []

    for slide in slides:
        slide_id = int(slide["slide_id"])
        audio_path = audio_dir / f"slide_{slide_id:02d}.mp3"
        audio_files.append(audio_path)
        if audio_path.exists() and audio_path.stat().st_size == 0:
            audio_path.unlink()

        text = slide.get("tts_script") or slide.get("speaker_script") or slide["title"]
        meta_path = audio_path.with_suffix(".meta.json")
        cache_payload = {
            "cache_version": 2,
            "provider": provider,
            "text": text,
            "voice": voice,
            "rate": rate,
            "sapi_voice": sapi_voice,
            "dashscope_voice_id": dashscope_voice_id,
            "dashscope_model": dashscope_model,
            "dashscope_instruction": dashscope_instruction,
            "slide_id": slide_id,
        }
        if not force and audio_cache_valid(audio_path, meta_path, cache_payload):
            continue

        if provider == "sapi":
            synthesize_sapi_audio(text, audio_path, sapi_voice, ffmpeg)
            write_audio_cache_meta(meta_path, cache_payload)
            continue
        if provider == "dashscope":
            synthesize_dashscope_audio(
                text=text,
                audio_path=audio_path,
                voice_id=dashscope_voice_id,
                model=dashscope_model,
                instruction=dashscope_instruction,
            )
            write_audio_cache_meta(meta_path, cache_payload)
            continue

        import edge_tts
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
                await communicate.save(str(audio_path))
                if audio_path.exists() and audio_path.stat().st_size > 0:
                    write_audio_cache_meta(meta_path, cache_payload)
                    break
            except Exception as exc:  # Edge's websocket service is occasionally flaky.
                last_error = exc
                if audio_path.exists() and audio_path.stat().st_size == 0:
                    audio_path.unlink()
                await asyncio.sleep(attempt * 2)
        else:
            raise RuntimeError(f"Edge TTS failed for slide {slide_id}") from last_error

    return audio_files


async def synthesize_segment_audio(
    slides: list[dict],
    audio_dir: Path,
    provider: str,
    force: bool,
    sapi_voice: str,
    ffmpeg: str,
    dashscope_voice_id: str,
    dashscope_model: str,
    dashscope_instruction: str,
    segment_gap: float,
    slide_pause: float,
) -> list[AudioUnit]:
    audio_dir.mkdir(parents=True, exist_ok=True)
    units: list[AudioUnit] = []
    last_slide_id = int(slides[-1]["slide_id"])

    if provider != "dashscope":
        raise RuntimeError("--audio-granularity segment currently supports --tts-provider dashscope.")

    for slide in slides:
        slide_id = int(slide["slide_id"])
        subtitles = slide.get("subtitle_segments") or [{"text": slide.get("tts_script", ""), "related_element_id": ""}]

        for idx, subtitle in enumerate(subtitles, start=1):
            text = naturalize_tts_text(subtitle["text"])
            audio_path = audio_dir / f"slide_{slide_id:02d}_seg_{idx:02d}.mp3"
            meta_path = audio_path.with_suffix(".meta.json")
            cache_payload = {
                "cache_version": 2,
                "provider": provider,
                "text": text,
                "voice_id": dashscope_voice_id,
                "model": dashscope_model,
                "instruction": dashscope_instruction,
                "slide_id": slide_id,
                "segment_index": idx,
            }
            if audio_path.exists() and audio_path.stat().st_size == 0:
                audio_path.unlink()
            if force or not audio_cache_valid(audio_path, meta_path, cache_payload):
                synthesize_dashscope_audio(
                    text=text,
                    audio_path=audio_path,
                    voice_id=dashscope_voice_id,
                    model=dashscope_model,
                    instruction=dashscope_instruction,
                )
                write_audio_cache_meta(meta_path, cache_payload)
            units.append(
                AudioUnit(
                    audio=audio_path,
                    duration=get_audio_duration(audio_path),
                    subtitle=subtitle["text"],
                    related_element_id=subtitle.get("related_element_id", ""),
                    slide_id=slide_id,
                    segment_index=idx,
                )
            )

            if segment_gap > 0 and idx != len(subtitles):
                pause_path = audio_dir / f"pause_seg_{int(segment_gap * 1000):03d}ms.mp3"
                make_silence(ffmpeg, pause_path, segment_gap)
                units.append(
                    AudioUnit(
                        audio=pause_path,
                        duration=segment_gap,
                        subtitle="",
                        related_element_id=subtitle.get("related_element_id", ""),
                        slide_id=slide_id,
                        segment_index=idx,
                        is_pause=True,
                    )
                )

        if slide_pause > 0 and slide_id != last_slide_id:
            pause_path = audio_dir / f"pause_slide_{int(slide_pause * 1000):03d}ms.mp3"
            make_silence(ffmpeg, pause_path, slide_pause)
            units.append(
                AudioUnit(
                    audio=pause_path,
                    duration=slide_pause,
                    subtitle="",
                    related_element_id="",
                    slide_id=slide_id,
                    segment_index=len(subtitles) + 1,
                    is_pause=True,
                )
            )

    return units


def naturalize_tts_text(text: str) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return cleaned
    if cleaned[-1] not in "。！？!?":
        cleaned += "。"
    return cleaned


def synthesize_dashscope_audio(text: str, audio_path: Path, voice_id: str, model: str, instruction: str) -> None:
    import os
    import requests

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is required for --tts-provider dashscope.")
    if not voice_id:
        raise RuntimeError("--dashscope-voice-id is required for --tts-provider dashscope.")

    payload = {
        "model": model,
        "input": {
            "text": text,
            "voice": voice_id,
            "format": "mp3",
            "sample_rate": 24000,
            "rate": 0.92,
            "volume": 60,
            "language_hints": ["zh"],
        },
    }
    if instruction:
        payload["input"]["instruction"] = instruction

    response = requests.post(
        "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if not response.ok:
        raise RuntimeError(f"DashScope TTS failed: {response.status_code} {response.text[:500]}")

    data = response.json()
    audio_url = data["output"]["audio"]["url"]
    audio_response = requests.get(audio_url, timeout=180)
    if not audio_response.ok:
        raise RuntimeError(f"DashScope audio download failed: {audio_response.status_code}")
    audio_path.write_bytes(audio_response.content)


def synthesize_sapi_audio(text: str, audio_path: Path, voice_name: str, ffmpeg: str) -> None:
    text_path = audio_path.with_suffix(".txt")
    wav_path = audio_path.with_suffix(".wav")
    ps_path = audio_path.parent / "sapi_tts.ps1"

    text_path.write_text(text, encoding="utf-8")
    ps_path.write_text(
        """
param(
  [string]$TextPath,
  [string]$OutPath,
  [string]$VoiceName
)
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($TextPath, [System.Text.Encoding]::UTF8)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($VoiceName) {
  $synth.SelectVoice($VoiceName)
}
$synth.Rate = 0
$synth.Volume = 100
$synth.SetOutputToWaveFile($OutPath)
$synth.Speak($text)
$synth.Dispose()
""".strip()
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ps_path),
            "-TextPath",
            str(text_path),
            "-OutPath",
            str(wav_path),
            "-VoiceName",
            voice_name,
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(wav_path),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(audio_path),
        ],
        check=True,
    )


def subtitle_weight(text: str) -> int:
    return max(8, sum(2 if ord(ch) > 127 else 1 for ch in text.strip()))


def allocate_durations(subtitles: list[dict], total: float) -> list[float]:
    if not subtitles:
        return [total]

    weights = [subtitle_weight(item["text"]) for item in subtitles]
    weight_total = sum(weights)
    durations = [max(1.15, total * weight / weight_total) for weight in weights]
    scale = total / sum(durations)
    return [duration * scale for duration in durations]


def find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        Path(r"C:\Windows\Fonts\msyhbd.ttc") if bold else Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def wrap_text(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    probe = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(probe)

    for ch in text.strip():
        candidate = current + ch
        width = draw.textbbox((0, 0), candidate, font=font)[2]
        if width <= max_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = ch

    if current:
        lines.append(current)
    return lines


def fit_subtitle(text: str, max_width: int) -> tuple[ImageFont.ImageFont, list[str]]:
    for size in [46, 44, 42, 40, 38, 36, 34, 32]:
        font = find_font(size, bold=True)
        lines = wrap_text(text, font, max_width)
        if len(lines) <= 2:
            return font, lines

    font = find_font(30, bold=True)
    lines = wrap_text(text, font, max_width)
    if len(lines) > 2:
        lines = [lines[0], "".join(lines[1:])]
    return font, lines[:2]


def render_subtitle_frame(source: Path, dest: Path, subtitle: str) -> None:
    image = Image.open(source).convert("RGBA")
    if not subtitle.strip():
        image.convert("RGB").save(dest, quality=95)
        return

    width, height = image.size
    panel_width = int(width * 0.82)
    max_text_width = panel_width - 96
    font, lines = fit_subtitle(subtitle, max_text_width)
    line_boxes = [ImageDraw.Draw(image).textbbox((0, 0), line, font=font) for line in lines]
    line_heights = [box[3] - box[1] for box in line_boxes]
    text_height = sum(line_heights) + (len(lines) - 1) * 14

    panel_height = max(122, text_height + 54)
    x1 = (width - panel_width) // 2
    x2 = x1 + panel_width
    y2 = height - 38
    y1 = y2 - panel_height

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(
        (x1, y1, x2, y2),
        radius=18,
        fill=(9, 12, 18, 198),
        outline=(255, 255, 255, 32),
        width=2,
    )
    image = Image.alpha_composite(image, overlay)
    draw = ImageDraw.Draw(image)

    y = y1 + (panel_height - text_height) // 2 - 2
    for line, line_height in zip(lines, line_heights):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        x = (width - text_width) // 2
        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 135))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height + 14

    image.convert("RGB").save(dest, quality=95)


def manifest_slide_map(capture_manifest: dict, frames_dir: Path) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for item in capture_manifest["slides"]:
        highlight_by_target = {
            h["target_element_id"]: frames_dir / h["file"]
            for h in item.get("highlight_files", [])
        }
        result[int(item["slide_id"])] = {
            "base": frames_dir / item["file"],
            "highlight_by_target": highlight_by_target,
        }
    return result


def build_segments(
    deck: dict,
    capture_manifest: dict,
    frames_dir: Path,
    audio_files: list[Path],
    output_frames_dir: Path,
    pause: float,
) -> list[Segment]:
    if output_frames_dir.exists():
        shutil.rmtree(output_frames_dir)
    output_frames_dir.mkdir(parents=True, exist_ok=True)

    frame_map = manifest_slide_map(capture_manifest, frames_dir)
    segments: list[Segment] = []

    for slide, audio_file in zip(deck["slides"], audio_files):
        slide_id = int(slide["slide_id"])
        audio_duration = get_audio_duration(audio_file)
        subtitles = slide.get("subtitle_segments") or [{"text": slide.get("tts_script", ""), "related_element_id": ""}]
        durations = allocate_durations(subtitles, audio_duration)
        slide_frames = frame_map[slide_id]

        for idx, (subtitle_item, duration) in enumerate(zip(subtitles, durations), start=1):
            related_id = subtitle_item.get("related_element_id", "")
            source = slide_frames["highlight_by_target"].get(related_id, slide_frames["base"])
            dest = output_frames_dir / f"seg_{len(segments) + 1:04d}.jpg"
            render_subtitle_frame(source, dest, subtitle_item["text"])
            segments.append(
                Segment(
                    image=dest,
                    duration=duration,
                    subtitle=subtitle_item["text"],
                    slide_id=slide_id,
                    segment_index=idx,
                )
            )

        if pause > 0 and slide_id != int(deck["slides"][-1]["slide_id"]):
            dest = output_frames_dir / f"seg_{len(segments) + 1:04d}.jpg"
            render_subtitle_frame(slide_frames["base"], dest, "")
            segments.append(
                Segment(
                    image=dest,
                    duration=pause,
                    subtitle="",
                    slide_id=slide_id,
                    segment_index=len(subtitles) + 1,
                )
            )

    cursor = 0.0
    for segment in segments:
        segment.start = cursor
        cursor += segment.duration
        segment.end = cursor

    return segments


def build_segments_from_audio_units(
    audio_units: list[AudioUnit],
    capture_manifest: dict,
    frames_dir: Path,
    output_frames_dir: Path,
) -> list[Segment]:
    if output_frames_dir.exists():
        shutil.rmtree(output_frames_dir)
    output_frames_dir.mkdir(parents=True, exist_ok=True)

    frame_map = manifest_slide_map(capture_manifest, frames_dir)
    segments: list[Segment] = []

    for unit in audio_units:
        slide_frames = frame_map[unit.slide_id]
        if unit.is_pause:
            source = slide_frames["base"]
        else:
            source = slide_frames["highlight_by_target"].get(unit.related_element_id, slide_frames["base"])
        dest = output_frames_dir / f"seg_{len(segments) + 1:04d}.jpg"
        render_subtitle_frame(source, dest, unit.subtitle)
        segments.append(
            Segment(
                image=dest,
                duration=unit.duration,
                subtitle=unit.subtitle,
                slide_id=unit.slide_id,
                segment_index=unit.segment_index,
            )
        )

    cursor = 0.0
    for segment in segments:
        segment.start = cursor
        cursor += segment.duration
        segment.end = cursor

    return segments


def format_srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(segments: list[Segment], path: Path) -> None:
    blocks: list[str] = []
    index = 1
    for segment in segments:
        if not segment.subtitle.strip():
            continue
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{format_srt_time(segment.start)} --> {format_srt_time(segment.end)}",
                    segment.subtitle,
                ]
            )
        )
        index += 1
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def write_image_concat(segments: list[Segment], out_dir: Path, path: Path) -> None:
    lines: list[str] = []
    for segment in segments:
        rel = segment.image.relative_to(out_dir).as_posix()
        lines.append(f"file '{rel}'")
        lines.append(f"duration {segment.duration:.4f}")
    rel = segments[-1].image.relative_to(out_dir).as_posix()
    lines.append(f"file '{rel}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_audio_concat(audio_files: list[Path], out_dir: Path, path: Path) -> None:
    lines = [f"file '{audio.relative_to(out_dir).as_posix()}'" for audio in audio_files]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_silence(ffmpeg: str, path: Path, seconds: float) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            f"{seconds:.3f}",
            "-q:a",
            "9",
            "-acodec",
            "libmp3lame",
            str(path),
        ],
        check=True,
    )


def compose_audio(
    ffmpeg: str,
    slide_audio_files: list[Path],
    out_dir: Path,
    pause: float,
) -> Path:
    audio_inputs: list[Path] = []
    if pause > 0:
        silence = out_dir / "audio" / f"silence_{int(pause * 1000):03d}ms.mp3"
        make_silence(ffmpeg, silence, pause)
    else:
        silence = None

    for idx, audio in enumerate(slide_audio_files):
        audio_inputs.append(audio)
        if silence and idx != len(slide_audio_files) - 1:
            audio_inputs.append(silence)

    concat_file = out_dir / "audio_concat.txt"
    write_audio_concat(audio_inputs, out_dir, concat_file)

    narration = out_dir / "narration_full.mp3"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(narration),
        ],
        cwd=out_dir,
        check=True,
    )
    return narration


def compose_audio_from_units(ffmpeg: str, audio_units: list[AudioUnit], out_dir: Path) -> Path:
    concat_file = out_dir / "audio_concat.txt"
    write_audio_concat([unit.audio for unit in audio_units], out_dir, concat_file)

    narration = out_dir / "narration_full.mp3"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(narration),
        ],
        cwd=out_dir,
        check=True,
    )
    return narration


def compose_video(ffmpeg: str, out_dir: Path, image_concat: Path, narration: Path, output: Path) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(image_concat),
            "-i",
            str(narration),
            "-vf",
            "fps=30,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(output),
        ],
        cwd=out_dir,
        check=True,
    )


def write_compose_manifest(path: Path, segments: list[Segment], narration: Path, output: Path) -> None:
    payload = {
        "output_video": str(output),
        "narration_audio": str(narration),
        "duration_seconds": segments[-1].end if segments else 0,
        "segment_count": len(segments),
        "segments": [
            {
                "slide_id": segment.slide_id,
                "segment_index": segment.segment_index,
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "duration": round(segment.duration, 3),
                "image": str(segment.image),
                "subtitle": segment.subtitle,
            }
            for segment in segments
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compose HTML slide screenshots, TTS, highlights, and subtitles into MP4.")
    parser.add_argument("--data", type=Path, default=PROJECT_ROOT / "html_deck_obsidian" / "deck-data.json")
    parser.add_argument("--capture-manifest", type=Path, default=PROJECT_ROOT / "outputs" / "html_obsidian_slides" / "capture_manifest.json")
    parser.add_argument("--frames-dir", type=Path, default=PROJECT_ROOT / "outputs" / "html_obsidian_slides")
    parser.add_argument("--out-dir", type=Path, default=PROJECT_ROOT / "outputs" / "html_video")
    parser.add_argument("--voice", default="zh-CN-YunxiNeural")
    parser.add_argument("--rate", default="+0%")
    parser.add_argument("--tts-provider", choices=["auto", "edge", "sapi", "dashscope"], default="auto")
    parser.add_argument("--sapi-voice", default="Microsoft Huihui Desktop")
    parser.add_argument("--dashscope-voice-id", default="")
    parser.add_argument("--dashscope-model", default="cosyvoice-v3.5-plus")
    parser.add_argument(
        "--dashscope-instruction",
        default="Speak like a calm professional online-course lecturer with natural pauses.",
    )
    parser.add_argument("--audio-granularity", choices=["slide", "segment"], default="slide")
    parser.add_argument("--segment-gap", type=float, default=0.16)
    parser.add_argument("--pause", type=float, default=0.35)
    parser.add_argument("--force-tts", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "audio").mkdir(parents=True, exist_ok=True)

    deck = load_json(args.data)
    capture_manifest = load_json(args.capture_manifest)
    ffmpeg = ffmpeg_exe()

    if args.audio_granularity == "segment":
        audio_units = asyncio.run(
            synthesize_segment_audio(
                slides=deck["slides"],
                audio_dir=args.out_dir / "audio",
                provider="edge" if args.tts_provider == "auto" else args.tts_provider,
                force=args.force_tts,
                sapi_voice=args.sapi_voice,
                ffmpeg=ffmpeg,
                dashscope_voice_id=args.dashscope_voice_id,
                dashscope_model=args.dashscope_model,
                dashscope_instruction=args.dashscope_instruction,
                segment_gap=args.segment_gap,
                slide_pause=args.pause,
            )
        )
        segments = build_segments_from_audio_units(
            audio_units,
            capture_manifest,
            args.frames_dir,
            args.out_dir / "subtitle_frames",
        )
        narration = compose_audio_from_units(ffmpeg, audio_units, args.out_dir)
    else:
        try:
            audio_files = asyncio.run(
                synthesize_slide_audio(
                    deck["slides"],
                    args.out_dir / "audio",
                    voice=args.voice,
                    rate=args.rate,
                    force=args.force_tts,
                    provider="edge" if args.tts_provider == "auto" else args.tts_provider,
                    sapi_voice=args.sapi_voice,
                    ffmpeg=ffmpeg,
                    dashscope_voice_id=args.dashscope_voice_id,
                    dashscope_model=args.dashscope_model,
                    dashscope_instruction=args.dashscope_instruction,
                )
            )
        except Exception:
            if args.tts_provider != "auto":
                raise
            print("Edge TTS failed; falling back to SAPI for all slides to keep one consistent voice.")
            audio_files = asyncio.run(
                synthesize_slide_audio(
                    deck["slides"],
                    args.out_dir / "audio",
                    voice=args.voice,
                    rate=args.rate,
                    force=True,
                    provider="sapi",
                    sapi_voice=args.sapi_voice,
                    ffmpeg=ffmpeg,
                    dashscope_voice_id=args.dashscope_voice_id,
                    dashscope_model=args.dashscope_model,
                    dashscope_instruction=args.dashscope_instruction,
                )
            )

        segments = build_segments(
            deck,
            capture_manifest,
            args.frames_dir,
            audio_files,
            args.out_dir / "subtitle_frames",
            pause=args.pause,
        )
        narration = compose_audio(ffmpeg, audio_files, args.out_dir, pause=args.pause)

    srt_path = args.out_dir / "subtitles.srt"
    write_srt(segments, srt_path)

    image_concat = args.out_dir / "image_concat.txt"
    write_image_concat(segments, args.out_dir, image_concat)

    output = args.out_dir / "data_jobs_industry_guide.mp4"
    compose_video(ffmpeg, args.out_dir, image_concat, narration, output)

    write_compose_manifest(args.out_dir / "compose_manifest.json", segments, narration, output)
    print(f"Wrote {output}")
    print(f"Wrote {srt_path}")
    print(f"Duration: {segments[-1].end:.2f}s, segments: {len(segments)}")


if __name__ == "__main__":
    main()
