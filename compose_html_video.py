from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import urlopen

from PIL import Image, ImageDraw, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


@dataclass
class Segment:
    image: Path
    duration: float
    subtitle: str
    slide_id: int
    segment_index: int
    related_element_id: str = ""
    start: float = 0.0
    end: float = 0.0
    transition_to: Path | None = None
    video: Path | None = None


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
    openai_tts_model: str,
    openai_voice: str,
    openai_instructions: str,
    tts_proxy: str,
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
            "cache_version": 3,
            "provider": provider,
            "text": text,
            "voice": voice,
            "rate": rate,
            "sapi_voice": sapi_voice,
            "dashscope_voice_id": dashscope_voice_id,
            "dashscope_model": dashscope_model,
            "dashscope_instruction": dashscope_instruction,
            "openai_tts_model": openai_tts_model,
            "openai_voice": openai_voice,
            "openai_instructions": openai_instructions,
            "slide_id": slide_id,
        }
        if not force and audio_cache_valid(audio_path, meta_path, cache_payload):
            continue

        await synthesize_tts_audio(
            text=text,
            audio_path=audio_path,
            provider=provider,
            voice=voice,
            rate=rate,
            sapi_voice=sapi_voice,
            ffmpeg=ffmpeg,
            dashscope_voice_id=dashscope_voice_id,
            dashscope_model=dashscope_model,
            dashscope_instruction=dashscope_instruction,
            openai_tts_model=openai_tts_model,
            openai_voice=openai_voice,
            openai_instructions=openai_instructions,
            tts_proxy=tts_proxy,
        )
        write_audio_cache_meta(meta_path, cache_payload)

    return audio_files


async def synthesize_segment_audio(
    slides: list[dict],
    audio_dir: Path,
    provider: str,
    voice: str,
    rate: str,
    force: bool,
    sapi_voice: str,
    ffmpeg: str,
    dashscope_voice_id: str,
    dashscope_model: str,
    dashscope_instruction: str,
    openai_tts_model: str,
    openai_voice: str,
    openai_instructions: str,
    tts_proxy: str,
    segment_gap: float,
    slide_pause: float,
) -> list[AudioUnit]:
    audio_dir.mkdir(parents=True, exist_ok=True)
    units: list[AudioUnit] = []
    last_slide_id = int(slides[-1]["slide_id"])

    for slide in slides:
        slide_id = int(slide["slide_id"])
        subtitles = slide.get("subtitle_segments") or [{"text": slide.get("tts_script", ""), "related_element_id": ""}]

        for idx, subtitle in enumerate(subtitles, start=1):
            text = naturalize_tts_text(subtitle["text"])
            audio_path = audio_dir / f"slide_{slide_id:02d}_seg_{idx:02d}.mp3"
            meta_path = audio_path.with_suffix(".meta.json")
            if provider == "dashscope":
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
            else:
                cache_payload = {
                    "cache_version": 3,
                    "provider": provider,
                    "text": text,
                    "voice": openai_voice if provider == "openai" else voice,
                    "rate": rate,
                    "sapi_voice": sapi_voice,
                    "openai_tts_model": openai_tts_model,
                    "openai_voice": openai_voice,
                    "openai_instructions": openai_instructions,
                    "slide_id": slide_id,
                    "segment_index": idx,
                }
            if audio_path.exists() and audio_path.stat().st_size == 0:
                audio_path.unlink()
            if force or not audio_cache_valid(audio_path, meta_path, cache_payload):
                await synthesize_tts_audio(
                    text=text,
                    audio_path=audio_path,
                    provider=provider,
                    voice=voice,
                    rate=rate,
                    sapi_voice=sapi_voice,
                    ffmpeg=ffmpeg,
                    dashscope_voice_id=dashscope_voice_id,
                    dashscope_model=dashscope_model,
                    dashscope_instruction=dashscope_instruction,
                    openai_tts_model=openai_tts_model,
                    openai_voice=openai_voice,
                    openai_instructions=openai_instructions,
                    tts_proxy=tts_proxy,
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


async def synthesize_tts_audio(
    text: str,
    audio_path: Path,
    provider: str,
    voice: str,
    rate: str,
    sapi_voice: str,
    ffmpeg: str,
    dashscope_voice_id: str,
    dashscope_model: str,
    dashscope_instruction: str,
    openai_tts_model: str,
    openai_voice: str,
    openai_instructions: str,
    tts_proxy: str,
) -> None:
    if provider == "sapi":
        synthesize_sapi_audio(text, audio_path, sapi_voice, ffmpeg)
        return
    if provider == "dashscope":
        synthesize_dashscope_audio(
            text=text,
            audio_path=audio_path,
            voice_id=dashscope_voice_id,
            model=dashscope_model,
            instruction=dashscope_instruction,
            proxy=tts_proxy,
        )
        return
    if provider == "openai":
        synthesize_openai_audio(
            text=text,
            audio_path=audio_path,
            model=openai_tts_model,
            voice=openai_voice,
            instructions=openai_instructions,
            proxy=tts_proxy,
        )
        return
    if provider != "edge":
        raise RuntimeError(f"Unsupported TTS provider: {provider}")

    import edge_tts

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
            await communicate.save(str(audio_path))
            if audio_path.exists() and audio_path.stat().st_size > 0:
                return
        except Exception as exc:  # Edge's websocket service is occasionally flaky.
            last_error = exc
            if audio_path.exists() and audio_path.stat().st_size == 0:
                audio_path.unlink()
            await asyncio.sleep(attempt * 2)
    raise RuntimeError("Edge TTS failed") from last_error


def request_proxy_kwargs(proxy: str) -> dict:
    return {"proxies": {"http": proxy, "https": proxy}} if proxy else {}


def synthesize_dashscope_audio(text: str, audio_path: Path, voice_id: str, model: str, instruction: str, proxy: str = "") -> None:
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
        **request_proxy_kwargs(proxy),
    )
    if not response.ok:
        raise RuntimeError(f"DashScope TTS failed: {response.status_code} {response.text[:500]}")

    data = response.json()
    audio_url = data["output"]["audio"]["url"]
    audio_response = requests.get(audio_url, timeout=180, **request_proxy_kwargs(proxy))
    if not audio_response.ok:
        raise RuntimeError(f"DashScope audio download failed: {audio_response.status_code}")
    audio_path.write_bytes(audio_response.content)


def synthesize_openai_audio(text: str, audio_path: Path, model: str, voice: str, instructions: str, proxy: str = "") -> None:
    import requests

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for --tts-provider openai.")

    payload = {
        "model": model,
        "voice": voice,
        "input": text,
        "response_format": "mp3",
    }
    if instructions:
        payload["instructions"] = instructions

    response = requests.post(
        "https://api.openai.com/v1/audio/speech",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=180,
        **request_proxy_kwargs(proxy),
    )
    if not response.ok:
        raise RuntimeError(f"OpenAI TTS failed: {response.status_code} {response.text[:500]}")
    audio_path.write_bytes(response.content)


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


def add_subtitle_overlay(source: Image.Image, subtitle: str) -> Image.Image:
    image = source.convert("RGBA")
    if not subtitle.strip():
        return image.convert("RGB")

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

    return image.convert("RGB")


def render_subtitle_frame(source: Path, dest: Path, subtitle: str) -> None:
    image = Image.open(source)
    add_subtitle_overlay(image, subtitle).save(dest, quality=95)


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

    for slide_index, (slide, audio_file) in enumerate(zip(deck["slides"], audio_files)):
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
                    related_element_id=related_id,
                )
            )

        if pause > 0 and slide_id != int(deck["slides"][-1]["slide_id"]):
            dest = output_frames_dir / f"seg_{len(segments) + 1:04d}.jpg"
            render_subtitle_frame(slide_frames["base"], dest, "")
            next_slide_id = int(deck["slides"][slide_index + 1]["slide_id"])
            segments.append(
                Segment(
                    image=dest,
                    duration=pause,
                    subtitle="",
                    slide_id=slide_id,
                    segment_index=len(subtitles) + 1,
                    related_element_id="",
                    transition_to=frame_map[next_slide_id]["base"],
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
    slide_ids = sorted(frame_map)
    next_slide_by_id = {
        slide_id: slide_ids[idx + 1]
        for idx, slide_id in enumerate(slide_ids[:-1])
    }
    segments: list[Segment] = []

    for unit in audio_units:
        slide_frames = frame_map[unit.slide_id]
        if unit.is_pause:
            source = slide_frames["base"]
        else:
            source = slide_frames["highlight_by_target"].get(unit.related_element_id, slide_frames["base"])
        dest = output_frames_dir / f"seg_{len(segments) + 1:04d}.jpg"
        render_subtitle_frame(source, dest, unit.subtitle)
        transition_to = None
        if unit.is_pause and not unit.subtitle.strip() and not unit.related_element_id and unit.slide_id in next_slide_by_id:
            transition_to = frame_map[next_slide_by_id[unit.slide_id]]["base"]
        segments.append(
            Segment(
                image=dest,
                duration=unit.duration,
                subtitle=unit.subtitle,
                slide_id=unit.slide_id,
                segment_index=unit.segment_index,
                related_element_id=unit.related_element_id,
                transition_to=transition_to,
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


def write_video_concat(videos: list[Path], out_dir: Path, path: Path) -> None:
    lines = [f"file '{video.relative_to(out_dir).as_posix()}'" for video in videos]
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


def still_video_filter(width: int, height: int, fps: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"fps={fps},format=yuv420p"
    )


def render_still_clip(ffmpeg: str, segment: Segment, dest: Path, fps: int, width: int, height: int) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-i",
            str(segment.image),
            "-t",
            f"{segment.duration:.4f}",
            "-vf",
            still_video_filter(width, height, fps),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
    )
    segment.video = dest


def render_transition_clip(
    ffmpeg: str,
    segment: Segment,
    dest: Path,
    fps: int,
    width: int,
    height: int,
    transition: str,
) -> None:
    if not segment.transition_to:
        render_still_clip(ffmpeg, segment, dest, fps=fps, width=width, height=height)
        return
    frames_dir = dest.with_suffix("")
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    frame_count = max(2, int(round(segment.duration * fps)))
    src = cover_image(Image.open(segment.image).convert("RGB"), width, height)
    nxt = cover_image(Image.open(segment.transition_to).convert("RGB"), width, height)
    for frame_no in range(frame_count):
        progress = frame_no / max(frame_count - 1, 1)
        frame = transition_frame(src, nxt, progress, transition)
        frame.save(frames_dir / f"frame_{frame_no + 1:04d}.jpg", quality=94)
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(frames_dir / "frame_%04d.jpg"),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
    )
    segment.video = dest


def cover_image(image: Image.Image, width: int, height: int) -> Image.Image:
    src_w, src_h = image.size
    scale = max(width / src_w, height / src_h)
    new_size = (max(width, int(src_w * scale + 0.5)), max(height, int(src_h * scale + 0.5)))
    resized = image.resize(new_size, Image.Resampling.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def transition_frame(src: Image.Image, nxt: Image.Image, progress: float, transition: str) -> Image.Image:
    progress = max(0.0, min(1.0, progress))
    if transition == "fade":
        return Image.blend(src, nxt, progress)

    frame = src.copy()
    width, height = src.size
    if transition == "wiperight":
        w = int(width * progress)
        if w > 0:
            box = (width - w, 0, width, height)
            frame.paste(nxt.crop(box), box)
    elif transition == "wipeup":
        h = int(height * progress)
        if h > 0:
            box = (0, height - h, width, height)
            frame.paste(nxt.crop(box), box)
    elif transition == "wipedown":
        h = int(height * progress)
        if h > 0:
            box = (0, 0, width, h)
            frame.paste(nxt.crop(box), box)
    else:
        w = int(width * progress)
        if w > 0:
            box = (0, 0, w, height)
            frame.paste(nxt.crop(box), box)
    return frame


def file_url(path: Path, slide_no: int) -> str:
    resolved = path.resolve()
    url_path = resolved.as_posix()
    if not url_path.startswith("/"):
        url_path = "/" + url_path
    return f"file://{quote(url_path, safe='/:')}#/{slide_no}"


class WebSocketConnection:
    def __init__(self, url: str):
        parsed = urlparse(url)
        if parsed.scheme != "ws":
            raise ValueError(f"Only ws:// DevTools URLs are supported: {url}")
        self.sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port or 80}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self.sock.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket handshake failed: {response[:200]!r}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        chunks: list[bytes] = []
        while True:
            first = self._read_exact(1)[0]
            second = self._read_exact(1)[0]
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(payload))
            if opcode == 8:
                raise RuntimeError("DevTools WebSocket closed")
            if opcode == 9:
                self._send_control(0xA, payload)
                continue
            if opcode in {1, 0}:
                chunks.append(payload)
                if fin:
                    return b"".join(chunks).decode("utf-8")

    def _send_control(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode, 0x80 | len(payload)])
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("DevTools WebSocket connection ended")
            data.extend(chunk)
        return bytes(data)


class ChromeDeckCapture:
    def __init__(self, chrome: Path, deck: Path, width: int, height: int):
        self.chrome = chrome
        self.deck = deck
        self.width = width
        self.height = height
        self.port = self._free_port()
        self.user_data_dir = Path(tempfile.mkdtemp(prefix="deck_chrome_"))
        self.process: subprocess.Popen | None = None
        self.ws: WebSocketConnection | None = None
        self.command_id = 0
        self.current_slide_id: int | None = None

    def __enter__(self) -> "ChromeDeckCapture":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        self.process = subprocess.Popen(
            [
                str(self.chrome),
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--allow-file-access-from-files",
                "--run-all-compositor-stages-before-draw",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.user_data_dir}",
                f"--window-size={self.width},{self.height}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        page = self._wait_for_page()
        self.ws = WebSocketConnection(page["webSocketDebuggerUrl"])
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": self.width,
                "height": self.height,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )

    def close(self) -> None:
        if self.ws:
            self.ws.close()
            self.ws = None
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        shutil.rmtree(self.user_data_dir, ignore_errors=True)

    def navigate(self, slide_id: int) -> None:
        if self.current_slide_id == slide_id:
            return
        self.call("Page.navigate", {"url": file_url(self.deck, slide_id)})
        deadline = time.time() + 10
        while time.time() < deadline:
            result = self.call("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True})
            if result.get("result", {}).get("value") == "complete":
                self.current_slide_id = slide_id
                time.sleep(0.08)
                return
            time.sleep(0.05)
        raise RuntimeError(f"Timed out loading slide {slide_id}")

    def apply_state(
        self,
        slide_id: int,
        visible_element_ids: list[str],
        entering_element_id: str,
        highlight_element_id: str,
        progress: float,
    ) -> None:
        self.navigate(slide_id)
        state = {
            "slideId": slide_id,
            "visible": visible_element_ids,
            "entering": entering_element_id,
            "highlight": highlight_element_id,
            "progress": progress,
        }
        script = f"""
(() => {{
  const state = {json.dumps(state, ensure_ascii=False)};
  const slides = Array.from(document.querySelectorAll('.slide'));
  const slide = slides[state.slideId - 1] || document.querySelector('.slide');
  slides.forEach((item, index) => {{
    const active = item === slide;
    item.classList.toggle('is-active', active);
    item.style.display = active ? '' : 'none';
  }});
  if (!slide) return false;
  const clamp = (value) => Math.max(0, Math.min(1, Number(value) || 0));
  const easeOut = (value) => 1 - Math.pow(1 - clamp(value), 3);
  const shown = new Set(state.visible || []);
  if (state.entering) shown.add(state.entering);
  slide.querySelectorAll('.is-highlighted').forEach((el) => el.classList.remove('is-highlighted'));
  slide.querySelectorAll('[data-element-id]').forEach((el) => {{
    const id = el.dataset.elementId || '';
    const visible = shown.has(id);
    el.style.transition = 'none';
    el.style.willChange = 'opacity, transform, filter';
    if (!visible) {{
      el.style.visibility = 'hidden';
      el.style.opacity = '0';
      el.style.transform = 'translateY(30px) scale(0.985)';
      el.style.filter = 'blur(4px)';
      return;
    }}
    const p = id === state.entering ? easeOut(state.progress) : 1;
    el.style.visibility = 'visible';
    el.style.opacity = String(p);
    el.style.transform = `translateY(${{(1 - p) * 30}}px) scale(${{0.985 + p * 0.015}})`;
    el.style.filter = `blur(${{(1 - p) * 4}}px)`;
  }});
  const target = state.highlight ? slide.querySelector(`[data-element-id="${{state.highlight}}"]`) : null;
  if (target) target.classList.add('is-highlighted');
  return true;
}})()
"""
        self.call("Runtime.evaluate", {"expression": script, "awaitPromise": False, "returnByValue": True})

    def screenshot(self) -> Image.Image:
        result = self.call("Page.captureScreenshot", {"format": "jpeg", "quality": 94, "captureBeyondViewport": False})
        data = base64.b64decode(result["data"])
        from io import BytesIO

        return Image.open(BytesIO(data)).convert("RGB")

    def call(self, method: str, params: dict | None = None) -> dict:
        if not self.ws:
            raise RuntimeError("Chrome DevTools is not connected")
        self.command_id += 1
        command_id = self.command_id
        self.ws.send_text(json.dumps({"id": command_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv_text())
            if message.get("id") != command_id:
                continue
            if "error" in message:
                raise RuntimeError(f"CDP {method} failed: {message['error']}")
            return message.get("result", {})

    def _wait_for_page(self) -> dict:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=1) as response:
                    pages = json.loads(response.read().decode("utf-8"))
                for page in pages:
                    if page.get("type") == "page" and page.get("webSocketDebuggerUrl"):
                        return page
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("Timed out waiting for Chrome DevTools page")

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])


def encode_frame_sequence(ffmpeg: str, frames_dir: Path, fps: int, dest: Path) -> None:
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(frames_dir / "frame_%04d.jpg"),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
    )


def concat_segment_parts(ffmpeg: str, parts: list[Path], dest: Path) -> None:
    if len(parts) == 1:
        shutil.move(str(parts[0]), dest)
        return
    concat_file = dest.with_suffix(".txt")
    concat_file.write_text("\n".join(f"file '{part.as_posix()}'" for part in parts) + "\n", encoding="utf-8")
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
            "-c",
            "copy",
            str(dest),
        ],
        check=True,
    )


def render_element_still(
    ffmpeg: str,
    capture: ChromeDeckCapture,
    segment: Segment,
    visible_ids: list[str],
    highlight_id: str,
    image_dir: Path,
    dest: Path,
    fps: int,
    width: int,
    height: int,
) -> None:
    capture.apply_state(segment.slide_id, visible_ids, "", highlight_id, 1)
    image_path = image_dir / f"seg_{segment.slide_id:02d}_{segment.segment_index:02d}_{len(list(image_dir.glob('*.jpg'))) + 1:04d}.jpg"
    add_subtitle_overlay(capture.screenshot(), segment.subtitle).save(image_path, quality=95)
    still_segment = Segment(
        image=image_path,
        duration=segment.duration,
        subtitle=segment.subtitle,
        slide_id=segment.slide_id,
        segment_index=segment.segment_index,
        related_element_id=segment.related_element_id,
    )
    render_still_clip(ffmpeg, still_segment, dest, fps=fps, width=width, height=height)
    segment.video = dest


def render_element_entrance_clip(
    ffmpeg: str,
    capture: ChromeDeckCapture,
    segment: Segment,
    prior_visible_ids: list[str],
    target_visible_ids: list[str],
    entering_id: str,
    highlight_id: str,
    work_dir: Path,
    image_dir: Path,
    dest: Path,
    fps: int,
    width: int,
    height: int,
    entrance_duration: float,
) -> None:
    animation_duration = min(segment.duration, entrance_duration)
    frame_count = max(2, int(round(animation_duration * fps)))
    frame_count = min(frame_count, max(2, int(segment.duration * fps)))
    frames_dir = work_dir / f"{dest.stem}_frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)

    for frame_no in range(frame_count):
        progress = frame_no / max(frame_count - 1, 1)
        capture.apply_state(segment.slide_id, prior_visible_ids, entering_id, highlight_id, progress)
        frame = add_subtitle_overlay(capture.screenshot(), segment.subtitle)
        frame.save(frames_dir / f"frame_{frame_no + 1:04d}.jpg", quality=94)

    animation_clip = work_dir / f"{dest.stem}_enter.mp4"
    encode_frame_sequence(ffmpeg, frames_dir, fps, animation_clip)
    parts = [animation_clip]
    actual_animation_duration = frame_count / fps
    hold_duration = segment.duration - actual_animation_duration
    if hold_duration > 0.05:
        capture.apply_state(segment.slide_id, target_visible_ids, "", highlight_id, 1)
        final_image = image_dir / f"{dest.stem}_final.jpg"
        add_subtitle_overlay(capture.screenshot(), segment.subtitle).save(final_image, quality=95)
        hold_clip = work_dir / f"{dest.stem}_hold.mp4"
        hold_segment = Segment(
            image=final_image,
            duration=hold_duration,
            subtitle=segment.subtitle,
            slide_id=segment.slide_id,
            segment_index=segment.segment_index,
            related_element_id=segment.related_element_id,
        )
        render_still_clip(ffmpeg, hold_segment, hold_clip, fps=fps, width=width, height=height)
        parts.append(hold_clip)
    concat_segment_parts(ffmpeg, parts, dest)
    segment.video = dest


def render_ppt_transition_clips(
    ffmpeg: str,
    segments: list[Segment],
    out_dir: Path,
    fps: int,
    width: int,
    height: int,
    transition: str,
) -> list[Path]:
    clips_dir = out_dir / "transition_clips"
    if clips_dir.exists():
        shutil.rmtree(clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)

    videos: list[Path] = []
    for idx, segment in enumerate(segments, start=1):
        dest = clips_dir / f"clip_{idx:04d}.mp4"
        if segment.transition_to:
            render_transition_clip(ffmpeg, segment, dest, fps=fps, width=width, height=height, transition=transition)
        else:
            render_still_clip(ffmpeg, segment, dest, fps=fps, width=width, height=height)
        videos.append(dest)
    return videos


def render_element_entrance_clips(
    ffmpeg: str,
    segments: list[Segment],
    deck_html: Path,
    chrome: Path,
    out_dir: Path,
    fps: int,
    width: int,
    height: int,
    transition: str,
    entrance_duration: float,
) -> list[Path]:
    clips_dir = out_dir / "element_entrance_clips"
    if clips_dir.exists():
        shutil.rmtree(clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)
    state_images = out_dir / "element_entrance_frames"
    if state_images.exists():
        shutil.rmtree(state_images)
    state_images.mkdir(parents=True, exist_ok=True)

    visible_by_slide: dict[int, list[str]] = {}
    videos: list[Path] = []
    with ChromeDeckCapture(chrome, deck_html, width, height) as capture:
        for idx, segment in enumerate(segments, start=1):
            dest = clips_dir / f"clip_{idx:04d}.mp4"
            if segment.transition_to:
                render_transition_clip(ffmpeg, segment, dest, fps=fps, width=width, height=height, transition=transition)
                videos.append(dest)
                continue

            visible_ids = visible_by_slide.setdefault(segment.slide_id, [])
            related_id = segment.related_element_id
            entering_id = related_id if related_id and related_id not in visible_ids else ""
            prior_visible = list(visible_ids)
            if related_id and related_id not in visible_ids:
                visible_ids.append(related_id)
            target_visible = list(visible_ids)
            highlight_id = related_id if segment.subtitle.strip() else ""

            if entering_id:
                render_element_entrance_clip(
                    ffmpeg,
                    capture,
                    segment,
                    prior_visible,
                    target_visible,
                    entering_id,
                    highlight_id,
                    clips_dir,
                    state_images,
                    dest,
                    fps=fps,
                    width=width,
                    height=height,
                    entrance_duration=entrance_duration,
                )
            else:
                render_element_still(
                    ffmpeg,
                    capture,
                    segment,
                    target_visible,
                    highlight_id,
                    state_images,
                    dest,
                    fps=fps,
                    width=width,
                    height=height,
                )
            videos.append(dest)
    return videos


def compose_video_from_clips(ffmpeg: str, out_dir: Path, videos: list[Path], narration: Path, output: Path) -> None:
    video_concat = out_dir / "video_concat.txt"
    video_track = out_dir / "video_track_ppt_transition.mp4"
    write_video_concat(videos, out_dir, video_concat)
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(video_concat),
            "-c",
            "copy",
            str(video_track),
        ],
        cwd=out_dir,
        check=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_track),
            "-i",
            str(narration),
            "-c:v",
            "copy",
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
                "related_element_id": segment.related_element_id,
                "transition_to": str(segment.transition_to) if segment.transition_to else "",
                "video": str(segment.video) if segment.video else "",
                "subtitle": segment.subtitle,
            }
            for segment in segments
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compose HTML slide screenshots, TTS, highlights, and subtitles into MP4.")
    parser.add_argument("--data", type=Path, default=PROJECT_ROOT / "html_deck_obsidian" / "deck-data.json", help="Path to deck-data.json.")
    parser.add_argument("--capture-manifest", type=Path, default=PROJECT_ROOT / "outputs" / "html_obsidian_slides" / "capture_manifest.json", help="Path to screenshot capture_manifest.json.")
    parser.add_argument("--frames-dir", type=Path, default=PROJECT_ROOT / "outputs" / "html_obsidian_slides", help="Directory containing captured slide PNG files.")
    parser.add_argument("--out-dir", type=Path, default=PROJECT_ROOT / "outputs" / "html_video", help="Directory for audio, intermediate clips, subtitles, and final MP4.")
    parser.add_argument("--voice", default="zh-CN-YunxiNeural", help="Edge TTS voice name when --tts-provider is edge or auto.")
    parser.add_argument("--rate", default="+0%", help="Edge TTS speaking rate, for example +0%%, -10%%, or +15%%.")
    parser.add_argument("--tts-provider", choices=["auto", "edge", "sapi", "dashscope", "openai"], default="auto", help="TTS backend. auto tries Edge and falls back to SAPI for slide-level audio.")
    parser.add_argument("--sapi-voice", default="Microsoft Huihui Desktop", help="Windows SAPI voice name used by --tts-provider sapi.")
    parser.add_argument("--dashscope-voice-id", default="", help="DashScope voice id used by --tts-provider dashscope.")
    parser.add_argument("--dashscope-model", default="cosyvoice-v3.5-plus", help="DashScope TTS model name.")
    parser.add_argument(
        "--dashscope-instruction",
        default="Speak like a calm professional online-course lecturer with natural pauses.",
        help="Speaking style instruction sent to DashScope TTS.",
    )
    parser.add_argument("--openai-tts-model", default="gpt-4o-mini-tts", help="OpenAI speech model used by --tts-provider openai.")
    parser.add_argument("--openai-voice", default="alloy", help="OpenAI TTS voice used by --tts-provider openai.")
    parser.add_argument(
        "--openai-instructions",
        default="Speak like a calm professional online-course lecturer with natural pauses.",
        help="Speaking style instruction sent to OpenAI TTS.",
    )
    parser.add_argument("--tts-proxy", default="", help="HTTP(S) proxy for DashScope/OpenAI TTS requests.")
    parser.add_argument("--audio-granularity", choices=["slide", "segment"], default="slide", help="Generate one audio file per slide or per subtitle segment.")
    # Visual mode controls how the video track is rendered from the HTML deck.
    parser.add_argument("--visual-mode", choices=["static", "ppt-transition", "element-entrance"], default="static", help="static: still screenshots; ppt-transition: page transitions; element-entrance: reveal elements by subtitle timing.")
    parser.add_argument("--slide-transition", choices=["fade", "wipeleft", "wiperight", "wipeup", "wipedown"], default="wipeleft", help="Transition used during silent slide-change pauses.")
    parser.add_argument("--entrance-duration", type=float, default=0.62, help="Seconds used for each newly referenced element's entrance animation.")
    parser.add_argument("--fps", type=int, default=30, help="Video frame rate for rendered transition and animation clips.")
    parser.add_argument("--width", type=int, default=1920, help="Output video width in pixels.")
    parser.add_argument("--height", type=int, default=1080, help="Output video height in pixels.")
    parser.add_argument("--chrome", type=Path, default=DEFAULT_CHROME, help="Chrome executable used for real HTML rendering in element-entrance mode.")
    parser.add_argument("--segment-gap", type=float, default=0.16, help="Silent gap in seconds inserted between subtitle-segment audio files.")
    parser.add_argument("--pause", type=float, default=0.35, help="Silent pause in seconds inserted between slides.")
    parser.add_argument("--force-tts", action="store_true", help="Regenerate TTS audio even when cache files exist.")
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
                voice=args.voice,
                rate=args.rate,
                force=args.force_tts,
                sapi_voice=args.sapi_voice,
                ffmpeg=ffmpeg,
                dashscope_voice_id=args.dashscope_voice_id,
                dashscope_model=args.dashscope_model,
                dashscope_instruction=args.dashscope_instruction,
                openai_tts_model=args.openai_tts_model,
                openai_voice=args.openai_voice,
                openai_instructions=args.openai_instructions,
                tts_proxy=args.tts_proxy,
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
                    openai_tts_model=args.openai_tts_model,
                    openai_voice=args.openai_voice,
                    openai_instructions=args.openai_instructions,
                    tts_proxy=args.tts_proxy,
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
                    openai_tts_model=args.openai_tts_model,
                    openai_voice=args.openai_voice,
                    openai_instructions=args.openai_instructions,
                    tts_proxy=args.tts_proxy,
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

    output = args.out_dir / "data_jobs_industry_guide.mp4"
    if args.visual_mode == "ppt-transition":
        videos = render_ppt_transition_clips(
            ffmpeg,
            segments,
            args.out_dir,
            fps=args.fps,
            width=args.width,
            height=args.height,
            transition=args.slide_transition,
        )
        compose_video_from_clips(ffmpeg, args.out_dir, videos, narration, output)
    elif args.visual_mode == "element-entrance":
        videos = render_element_entrance_clips(
            ffmpeg,
            segments,
            Path(capture_manifest["deck"]),
            args.chrome,
            args.out_dir,
            fps=args.fps,
            width=args.width,
            height=args.height,
            transition=args.slide_transition,
            entrance_duration=args.entrance_duration,
        )
        compose_video_from_clips(ffmpeg, args.out_dir, videos, narration, output)
    else:
        image_concat = args.out_dir / "image_concat.txt"
        write_image_concat(segments, args.out_dir, image_concat)
        compose_video(ffmpeg, args.out_dir, image_concat, narration, output)

    write_compose_manifest(args.out_dir / "compose_manifest.json", segments, narration, output)
    print(f"Wrote {output}")
    print(f"Wrote {srt_path}")
    print(f"Duration: {segments[-1].end:.2f}s, segments: {len(segments)}")


if __name__ == "__main__":
    main()
