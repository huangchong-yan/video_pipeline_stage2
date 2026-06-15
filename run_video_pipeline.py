from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
NATURAL_DASHSCOPE_TTS_INSTRUCTION = (
    "自然中文口语讲解，像一位性格活跃、表达清楚的老师在小班课堂里讲课。语气有亲和力，"
    "带一点轻松感和互动感，但不要夸张表演。根据逗号、顿号、破折号和省略号自然停顿；"
    "遇到重点概念稍微放慢，举例和转折处语气更灵活。避免播音腔、AI腔和每句相同节奏。"
)
NATURAL_OPENAI_TTS_INSTRUCTION = (
    "Speak in natural conversational Mandarin, like an energetic but not exaggerated teacher in a small classroom. "
    "Keep it clear, friendly, and lightly interactive. Follow punctuation for realistic pauses, especially commas, "
    "dashes, and ellipses. Vary rhythm between explanation, examples, and transitions. Slow down slightly for key "
    "concepts. Avoid announcer style, sales tone, robotic pacing, and identical sentence cadence."
)
AUDIO_STYLE_DEFAULT = (
    "性格活跃的课堂老师风格：自然、清楚、有亲和力，像在小班课堂里边讲边带学生理解。"
    "旁白必须包含真实口语标点和节奏提示：多用逗号、顿号、分号、破折号、省略号；"
    "关键概念前后留出自然停顿；转折、举例、提醒处要有语气变化。不要写成书面稿，"
    "不要每句都用完整句号收尾，不要播音腔，不要AI腔。"
)
ACTION_STYLE_GUIDELINES = (
    "可选生成 teaching_actions，借鉴 OpenMAIC 的课堂动作序列："
    "用 spotlight/laser 动作先指向元素，再用 text 讲解。"
    "text 是老师真正说出口的内容，不要说“我现在高亮这里”“我来添加这个元素”等动作描述。"
    "学生会看到画面动作，旁白只需要自然讲课。"
)


class DifyUnsupportedFileType(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_element_id(element: dict) -> str:
    return str(element.get("element_id") or element.get("id") or "").strip()


def valid_element_ids(slide: dict) -> set[str]:
    return {eid for eid in (normalize_element_id(item) for item in slide.get("elements", []) or []) if eid}


def action_name(action: dict) -> str:
    if action.get("type") == "action":
        return str(action.get("name") or "").strip()
    return str(action.get("type") or "").strip()


def action_element_id(action: dict) -> str:
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    return str(
        params.get("elementId")
        or params.get("element_id")
        or action.get("elementId")
        or action.get("element_id")
        or action.get("target_element_id")
        or ""
    ).strip()


def clean_spoken_text(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^[（(【\[][^）)】\]]{1,20}[）)】\]]\s*[:：]?\s*", "", cleaned)
    cleaned = re.sub(r"^(我(现在|来|会|要)?|接下来我(们)?)(先)?(高亮|标出|指出|添加|创建|展示|切到|打开)[^。！？；;，,]*[，,。！？；;]?", "", cleaned)
    cleaned = cleaned.replace("让我们高亮看一下", "请注意")
    cleaned = cleaned.replace("我来高亮", "请注意")
    return cleaned.strip()


def strip_text_markup(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"</?(strong|em|b|i|mark)>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def text_value(value) -> str:
    if isinstance(value, list):
        return " ".join(text_value(item) for item in value)
    if isinstance(value, dict):
        return " ".join(text_value(item) for item in value.values())
    return strip_text_markup(str(value or ""))


def normalize_slide_element_ids(slide: dict) -> bool:
    changed = False
    slide_no = int(slide.get("slide_id") or 1)
    for index, element in enumerate(slide.get("elements", []) or [], start=1):
        if not isinstance(element, dict):
            continue
        element_id = normalize_element_id(element)
        if not element_id:
            element_type = str(element.get("type") or "item").strip().lower() or "item"
            element_type = re.sub(r"[^a-z0-9_]+", "_", element_type)
            element_id = f"s{slide_no}_{element_type}_{index}"
            element["element_id"] = element_id
            changed = True
        elif element.get("element_id") != element_id:
            element["element_id"] = element_id
            changed = True
        if not element.get("id"):
            element["id"] = element_id
            changed = True
    return changed


def slide_element_texts(slide: dict) -> dict[str, str]:
    texts: dict[str, str] = {}
    for element in slide.get("elements", []) or []:
        if not isinstance(element, dict):
            continue
        element_id = normalize_element_id(element)
        if element_id:
            texts[element_id] = text_value(element.get("text") or element.get("content") or element.get("title"))
    return texts


def split_spoken_segments(text: str, max_chars: int = 30) -> list[str]:
    cleaned = strip_text_markup(text)
    if not cleaned:
        return []
    pieces = [piece.strip() for piece in re.split(r"(?<=[。！？!?；;，,、…])", cleaned) if piece.strip()]
    segments: list[str] = []
    current = ""
    for piece in pieces or [cleaned]:
        if current and len(current) + len(piece) <= max_chars:
            current += piece
            continue
        if current:
            segments.append(current.strip())
        while len(piece) > max_chars:
            segments.append(piece[:max_chars].strip())
            piece = piece[max_chars:]
        current = piece
    if current.strip():
        segments.append(current.strip())
    return [segment for segment in segments if segment][:80]


def extract_emphasized_keywords(slide: dict, limit: int = 4) -> list[str]:
    raw_keywords = slide.get("emphasized_keywords") or slide.get("keywords") or slide.get("key_points") or []
    if isinstance(raw_keywords, str):
        raw_keywords = re.split(r"[,，、;；\n]+", raw_keywords)
    keywords: list[str] = []
    if isinstance(raw_keywords, list):
        for item in raw_keywords:
            keyword = strip_text_markup(item)
            if keyword and keyword not in keywords:
                keywords.append(keyword)

    script_text = f"{slide.get('speaker_script', '')}\n{slide.get('tts_script', '')}"
    for match in re.findall(r"<strong>(.*?)</strong>", script_text, flags=re.IGNORECASE | re.DOTALL):
        keyword = strip_text_markup(match)
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return keywords[:limit]


def normalize_emphasized_keywords(slide: dict) -> bool:
    keywords = extract_emphasized_keywords(slide)
    if not keywords:
        return False
    if slide.get("emphasized_keywords") != keywords:
        slide["emphasized_keywords"] = keywords
        return True
    return False


def first_body_element_id(slide: dict) -> str:
    elements = slide.get("elements", []) or []
    for element in elements:
        if isinstance(element, dict) and element.get("type") != "title" and normalize_element_id(element):
            return normalize_element_id(element)
    for element in elements:
        if isinstance(element, dict) and normalize_element_id(element):
            return normalize_element_id(element)
    return ""


def element_id_for_keyword(slide: dict, keyword: str) -> str:
    for element_id, text in slide_element_texts(slide).items():
        if keyword and keyword in text:
            return element_id
    return first_body_element_id(slide)


def normalize_subtitle_segments(slide: dict) -> bool:
    ids = valid_element_ids(slide)
    segments = slide.get("subtitle_segments")
    fallback_id = first_body_element_id(slide)
    if not isinstance(segments, list) or not segments:
        slide["subtitle_segments"] = [
            {"text": segment, "related_element_id": fallback_id}
            for segment in split_spoken_segments(slide.get("tts_script") or slide.get("speaker_script") or slide.get("title") or "")
        ]
        return bool(slide["subtitle_segments"])

    changed = False
    normalized: list[dict] = []
    for segment in segments:
        if isinstance(segment, str):
            normalized.append({"text": strip_text_markup(segment), "related_element_id": fallback_id})
            changed = True
            continue
        if not isinstance(segment, dict):
            changed = True
            continue
        text = strip_text_markup(segment.get("text") or segment.get("content") or "")
        if not text:
            changed = True
            continue
        related = str(segment.get("related_element_id") or segment.get("target_element_id") or "").strip()
        if related and related not in ids:
            related = fallback_id
            changed = True
        normalized.append({"text": text, "related_element_id": related or fallback_id})
    if normalized != segments:
        slide["subtitle_segments"] = normalized
        changed = True
    return changed


def normalize_highlight_steps(slide: dict) -> bool:
    ids = valid_element_ids(slide)
    if not ids:
        return False
    steps = slide.get("highlight_steps")
    normalized: list[dict] = []
    changed = False
    if isinstance(steps, list):
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                changed = True
                continue
            target = str(step.get("target_element_id") or step.get("element_id") or step.get("id") or "").strip()
            if target not in ids:
                cue = strip_text_markup(step.get("cue_text") or step.get("text") or step.get("reason") or "")
                target = element_id_for_keyword(slide, cue) if cue else first_body_element_id(slide)
                changed = True
            item = dict(step)
            item["step"] = int(item.get("step") or index)
            item["target_element_id"] = target
            item["highlight_type"] = item.get("highlight_type") or "orange_box"
            item["cue_text"] = strip_text_markup(item.get("cue_text") or item.get("text") or "")
            normalized.append(item)

    if len(normalized) < 2:
        existing_targets = {item["target_element_id"] for item in normalized}
        for keyword in slide.get("emphasized_keywords") or extract_emphasized_keywords(slide):
            target = element_id_for_keyword(slide, keyword)
            if not target or target in existing_targets:
                continue
            normalized.append(
                {
                    "step": len(normalized) + 1,
                    "cue_text": keyword,
                    "target_element_id": target,
                    "highlight_type": "orange_box",
                    "reason": "Lecturify-style emphasized keyword",
                }
            )
            existing_targets.add(target)
            changed = True
            if len(normalized) >= 4:
                break

    if len(normalized) < 2:
        existing_targets = {item["target_element_id"] for item in normalized}
        for element in slide.get("elements", []) or []:
            if not isinstance(element, dict) or element.get("type") == "title":
                continue
            target = normalize_element_id(element)
            if not target or target in existing_targets:
                continue
            normalized.append(
                {
                    "step": len(normalized) + 1,
                    "cue_text": strip_text_markup(element.get("text") or element.get("title") or ""),
                    "target_element_id": target,
                    "highlight_type": "orange_box",
                    "reason": "content allocation fallback focus",
                }
            )
            existing_targets.add(target)
            changed = True
            if len(normalized) >= 2:
                break

    if not normalized:
        fallback_id = first_body_element_id(slide)
        if fallback_id:
            normalized.append(
                {
                    "step": 1,
                    "cue_text": strip_text_markup(slide.get("title", "")),
                    "target_element_id": fallback_id,
                    "highlight_type": "orange_box",
                    "reason": "fallback focus element",
                }
            )
            changed = True

    if normalized != steps:
        slide["highlight_steps"] = normalized[:4]
        changed = True
    return changed


def normalize_teaching_actions(slide: dict) -> bool:
    actions = slide.get("teaching_actions") or slide.get("actions")
    if not isinstance(actions, list) or not actions:
        return False

    ids = valid_element_ids(slide)
    current_element_id = ""
    subtitles: list[dict] = []
    highlights: list[dict] = []
    speech_parts: list[str] = []
    step = 1

    for action in actions:
        if not isinstance(action, dict):
            continue
        name = action_name(action)
        if name in {"spotlight", "laser", "highlight"}:
            element_id = action_element_id(action)
            if element_id in ids:
                current_element_id = element_id
                highlights.append(
                    {
                        "step": step,
                        "cue_text": "",
                        "target_element_id": element_id,
                        "highlight_type": "orange_box",
                        "reason": "OpenMAIC-style focus action",
                    }
                )
                step += 1
            continue
        if action.get("type") in {"text", "speech"} or name in {"text", "speech"}:
            text = clean_spoken_text(action.get("content") or action.get("text") or "")
            if not text:
                continue
            subtitles.append({"text": text, "related_element_id": current_element_id})
            speech_parts.append(text)

    if not subtitles:
        return False

    slide["subtitle_segments"] = subtitles
    slide["tts_script"] = " ".join(speech_parts)
    if not slide.get("speaker_script"):
        slide["speaker_script"] = slide["tts_script"]
    if highlights:
        slide["highlight_steps"] = highlights
    return True


def normalize_course_json(data: dict) -> dict:
    changed = False
    for slide in data.get("slides", []) or []:
        if normalize_slide_element_ids(slide):
            changed = True
        if normalize_teaching_actions(slide):
            changed = True
        if normalize_emphasized_keywords(slide):
            changed = True
        if normalize_subtitle_segments(slide):
            changed = True
        if normalize_highlight_steps(slide):
            changed = True
    if changed:
        data.setdefault("metadata", {})["openmaic_actions_normalized"] = True
        data.setdefault("metadata", {})["lecturify_content_allocation_normalized"] = True
    return data


def normalize_course_json_file(path: Path) -> Path:
    data = normalize_course_json(load_json(path))
    write_json(path, data)
    return path


def as_path(value: str | Path, base: Path = ROOT) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path


def validate_http_bearer_key(api_key: str, env_name: str) -> None:
    placeholder_markers = (
        "your_",
        "your-",
        "YOUR_",
        "你的",
        "浣犵殑",
        "真实",
        "real-",
        "app-your",
        "sk-xxx",
        "xxxxxxxx",
    )
    if any(marker in api_key for marker in placeholder_markers):
        raise SystemExit(f"{env_name} is still a placeholder. Set it to your real API key.")
    try:
        api_key.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise SystemExit(f"{env_name} contains non-ASCII characters. Paste the real API key, not Chinese placeholder text.") from exc


def run_step(name: str, cmd: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print(f"\n== {name} ==")
    print(" ".join(f'"{part}"' if " " in part else part for part in cmd))
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(cmd, cwd=cwd, env=merged_env)
    if result.returncode != 0:
        raise SystemExit(f"{name} failed with exit code {result.returncode}")


def ensure_clean_dir(path: Path, clean: bool) -> None:
    if clean and path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def deck_data_path(deck_dir: Path) -> Path:
    return deck_dir / "deck-data.json"


def capture_manifest_path(frames_dir: Path) -> Path:
    return frames_dir / "capture_manifest.json"


def resolve_voice_id(config: dict) -> str:
    tts = config.get("tts", {})
    voice_id = (tts.get("dashscope_voice_id") or "").strip()
    if voice_id:
        return voice_id
    voice_id_file = tts.get("dashscope_voice_id_file")
    if voice_id_file:
        path = as_path(voice_id_file)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return ""


def preview_text_from_slide(input_json: Path, slide_id: int) -> str:
    data = load_json(input_json)
    slides = data.get("slides", [])
    slide = next((item for item in slides if int(item.get("slide_id", 0)) == slide_id), slides[0])
    segments = slide.get("subtitle_segments") or []
    if segments:
        text = "".join(item.get("text", "") for item in segments[:2])
    else:
        text = slide.get("tts_script") or slide.get("speaker_script") or slide.get("title", "")
    return text.strip()


def extract_json_text(value) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, indent=2)
    if not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, indent=2)

    raw = value.strip()
    last_think = raw.rfind("</think>")
    if last_think != -1:
        raw = raw[last_think + len("</think>") :].strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = raw[start : end + 1]
        json.loads(candidate)
        return candidate
    json.loads(raw)
    return raw


def select_dify_output(response: dict, output_key: str = ""):
    outputs = response.get("data", {}).get("outputs")
    if outputs is None:
        outputs = response.get("outputs")
    if outputs is None:
        raise SystemExit("Dify response did not contain data.outputs.")

    if output_key:
        if output_key not in outputs:
            raise SystemExit(f"Dify output_key '{output_key}' not found. Available keys: {list(outputs)}")
        return outputs[output_key]

    if isinstance(outputs, dict):
        for key in ["json", "result", "output", "text", "answer", "data"]:
            if key in outputs:
                try:
                    extract_json_text(outputs[key])
                    return outputs[key]
                except Exception:
                    pass
        for value in outputs.values():
            try:
                extract_json_text(value)
                return value
            except Exception:
                continue
    raise SystemExit("Could not find a JSON-like value in Dify data.outputs. Set dify.output_key in config.")


def run_llm_stage(config: dict) -> Path:
    llm = config.get("llm", {})
    if not llm.get("enabled", False):
        return as_path(config["input_json"])

    provider = llm.get("provider", "openai").lower()
    model = llm.get("model", "").strip()
    if not model:
        raise SystemExit("llm.model is required when llm.enabled=true")

    api_key_env = llm.get("api_key_env") or {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "claude": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "minimax": "MINIMAX_API_KEY",
    }.get(provider, "")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise SystemExit(f"{api_key_env} is required when llm.enabled=true")
    validate_http_bearer_key(api_key, api_key_env)

    source_document = llm.get("source_document") or first_configured_document(config)
    if not source_document:
        raise SystemExit("llm.source_document or dify.files[0].path is required for direct LLM generation.")
    source_text = extract_document_text(as_path(source_document), max_chars=int(llm.get("max_text_chars", 120_000)))

    inputs = dict(config.get("dify", {}).get("inputs", {}))
    inputs.update(dict(llm.get("inputs", {})))
    prompt = build_production_json_prompt(
        source_text=source_text,
        slide_count=str(inputs.get("slide_count", "8")),
        video_style=str(inputs.get("video_style", "")),
        highlight_style=str(inputs.get("highlight_style", "orange_box")),
        generation_goal=str(inputs.get("generation_goal", "")),
        audio_style=str(inputs.get("audio_style", AUDIO_STYLE_DEFAULT)),
    )

    system_prompt = llm.get(
        "system_prompt",
        "You are a senior instructional designer. Return only valid JSON that follows the requested schema.",
    )
    raw_response_path = as_path(llm.get("raw_response", "outputs/llm/raw_response.json"))
    output_json_path = as_path(llm.get("output_json", "outputs/llm/production_json.json"))
    raw_response_path.parent.mkdir(parents=True, exist_ok=True)
    output_json_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n== llm-production-json ==\nprovider={provider} model={model}")
    if provider == "openai":
        raw = call_openai_llm(llm, api_key, model, system_prompt, prompt)
        text = extract_openai_text(raw)
    elif provider == "minimax":
        raw = call_minimax_llm(llm, api_key, model, system_prompt, prompt)
        text = extract_openai_chat_text(raw, "MiniMax")
    elif provider in {"anthropic", "claude"}:
        raw = call_anthropic_llm(llm, api_key, model, system_prompt, prompt)
        text = extract_anthropic_text(raw)
    elif provider == "gemini":
        raw = call_gemini_llm(llm, api_key, model, system_prompt, prompt)
        text = extract_gemini_text(raw)
    else:
        raise SystemExit(f"Unsupported llm.provider: {provider}")

    parsed = normalize_course_json(json.loads(extract_json_text(text)))
    write_json(raw_response_path, raw)
    write_json(output_json_path, parsed)
    print(f"Wrote LLM raw response to {raw_response_path}")
    print(f"Wrote LLM production JSON to {output_json_path}")
    return output_json_path


def first_configured_document(config: dict) -> str:
    for item in config.get("dify", {}).get("files", []) or []:
        path = (item.get("path") or "").strip()
        if path:
            return path
    return ""


def build_production_json_prompt(
    source_text: str,
    slide_count: str,
    video_style: str,
    highlight_style: str,
    generation_goal: str,
    audio_style: str = AUDIO_STYLE_DEFAULT,
) -> str:
    return f"""
Generate a complete course-style PPT production JSON from the source document.

Generation goal:
{generation_goal}

Constraints:
- Use a Lecturify-style content allocation pass: one clear teaching goal per slide, compact visible text, and most explanation carried by narration.
- Add emphasized_keywords for every slide, preferably 2 to 4 items. Each keyword must appear verbatim in slide text or narration.
- If you output timing_map, keep it optional and simple: element_id, start, end. The element_id must exist on the same slide.
- Do not invent exact numbers, rankings, cases, salaries, rates, or company facts unless they appear in the source document.
- Keep highlight_steps focused: 2 to 4 per slide. Prefer concept-level keywords and page turning points over highlighting every sentence.
- slide_count: {slide_count}
- video_style: {video_style}
- highlight_style: {highlight_style}
- audio_style: {audio_style}
- Language: Chinese, unless the source document clearly requires another language.
- Return only JSON. Do not wrap in Markdown.
- Each slide must include subtitle_segments so the video composer can align subtitles and highlight frames.
- Every subtitle_segments item should be short enough for one subtitle line or two compact lines.
- tts_script must be natural spoken Chinese, not written prose.
- tts_script and subtitle_segments.text must include expressive punctuation for speech timing, such as commas, pauses, dashes, ellipses, rhetorical questions, and short interjections where appropriate.
- Add classroom-like speaking cues through wording and punctuation, for example: "这里先别急，...", "你可以把它理解成——", "注意，这一步很关键。"
- Do not output bracketed stage directions like [pause] or (smile). Encode delivery through punctuation and natural wording.
- highlight_steps.target_element_id must reference an existing element id in the same slide.
- {ACTION_STYLE_GUIDELINES}

Required JSON shape:
{{
  "title": "course title",
  "slides": [
    {{
      "slide_id": 1,
      "title": "slide title",
      "layout": "cover | comparison | process | quote | industry",
      "elements": [
        {{
          "id": "s1_item_1",
          "type": "bullet | card | quote | step | title",
          "text": "visible slide text"
        }}
      ],
      "speaker_script": "natural, lively classroom teacher script for this slide",
      "tts_script": "spoken narration text with natural punctuation and expressive classroom delivery",
      "emphasized_keywords": ["keyword visible or spoken on this slide"],
      "timing_map": [
        {{
          "element_id": "s1_item_1",
          "start": 0.0,
          "end": 2.4
        }}
      ],
      "subtitle_segments": [
        {{
          "text": "subtitle text",
          "related_element_id": "s1_item_1"
        }}
      ],
      "highlight_steps": [
        {{
          "target_element_id": "s1_item_1"
        }}
      ],
      "teaching_actions": [
        {{
          "type": "action",
          "name": "spotlight",
          "params": {{"elementId": "s1_item_1"}}
        }},
        {{
          "type": "text",
          "content": "老师真正说出口的自然讲解，不要描述高亮动作。"
        }}
      ]
    }}
  ]
}}

Source document:
<<<SOURCE_DOCUMENT
{source_text}
SOURCE_DOCUMENT
>>>
""".strip()


def call_openai_llm(llm: dict, api_key: str, model: str, system_prompt: str, prompt: str) -> dict:
    base_url = llm.get("base_url", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": float(llm.get("temperature", 0.2)),
    }
    max_output_tokens = llm.get("max_output_tokens")
    if max_output_tokens:
        payload["max_output_tokens"] = int(max_output_tokens)
    response = request_with_retries(
        "post",
        base_url + "/responses",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=int(llm.get("timeout", 600)),
        trust_env=bool(llm.get("trust_env_proxy", True)),
        proxy=llm.get("proxy", ""),
        attempts=int(llm.get("network_retries", 3)),
    )
    if not response.ok:
        raise SystemExit(f"OpenAI generation failed: {response.status_code} {response.text[:1000]}")
    return response.json()


def extract_openai_text(raw: dict) -> str:
    if raw.get("output_text"):
        return raw["output_text"]
    chunks: list[str] = []
    for item in raw.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])
    if not chunks:
        raise SystemExit("OpenAI response did not contain output text.")
    return "\n".join(chunks)


def call_minimax_llm(llm: dict, api_key: str, model: str, system_prompt: str, prompt: str) -> dict:
    base_url = llm.get("base_url", "").rstrip("/")
    if not base_url:
        raise SystemExit("llm.base_url or --llm-base-url is required when llm.provider=minimax")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": float(llm.get("temperature", 0.2)),
    }
    max_tokens = llm.get("max_tokens") or llm.get("max_output_tokens")
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    response = request_with_retries(
        "post",
        base_url + "/chat/completions",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=int(llm.get("timeout", 600)),
        trust_env=bool(llm.get("trust_env_proxy", True)),
        proxy=llm.get("proxy", ""),
        attempts=int(llm.get("network_retries", 3)),
    )
    if not response.ok:
        raise SystemExit(f"MiniMax generation failed: {response.status_code} {response.text[:1000]}")
    return response.json()


def extract_openai_chat_text(raw: dict, provider_name: str) -> str:
    choices = raw.get("choices") or []
    chunks: list[str] = []
    for choice in choices:
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("text"):
                    chunks.append(str(part["text"]))
    if not chunks:
        raise SystemExit(f"{provider_name} response did not contain chat completion text.")
    return "\n".join(chunks)


def call_anthropic_llm(llm: dict, api_key: str, model: str, system_prompt: str, prompt: str) -> dict:
    base_url = llm.get("base_url", "https://api.anthropic.com/v1").rstrip("/")
    payload = {
        "model": model,
        "max_tokens": int(llm.get("max_tokens", 8192)),
        "temperature": float(llm.get("temperature", 0.2)),
        "system": system_prompt,
        "messages": [{"role": "user", "content": prompt}],
    }
    response = request_with_retries(
        "post",
        base_url + "/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": llm.get("anthropic_version", "2023-06-01"),
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=int(llm.get("timeout", 600)),
        trust_env=bool(llm.get("trust_env_proxy", True)),
        proxy=llm.get("proxy", ""),
        attempts=int(llm.get("network_retries", 3)),
    )
    if not response.ok:
        raise SystemExit(f"Anthropic generation failed: {response.status_code} {response.text[:1000]}")
    return response.json()


def extract_anthropic_text(raw: dict) -> str:
    chunks = [item.get("text", "") for item in raw.get("content", []) if item.get("type") == "text"]
    if not chunks:
        raise SystemExit("Anthropic response did not contain text content.")
    return "\n".join(chunks)


def call_gemini_llm(llm: dict, api_key: str, model: str, system_prompt: str, prompt: str) -> dict:
    base_url = llm.get("base_url", "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    url = f"{base_url}/models/{model}:generateContent?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": float(llm.get("temperature", 0.2)),
        },
    }
    max_output_tokens = llm.get("max_output_tokens")
    if max_output_tokens:
        payload["generationConfig"]["maxOutputTokens"] = int(max_output_tokens)
    response = request_with_retries(
        "post",
        url,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=int(llm.get("timeout", 600)),
        trust_env=bool(llm.get("trust_env_proxy", True)),
        proxy=llm.get("proxy", ""),
        attempts=int(llm.get("network_retries", 3)),
    )
    if not response.ok:
        raise SystemExit(f"Gemini generation failed: {response.status_code} {response.text[:1000]}")
    return response.json()


def extract_gemini_text(raw: dict) -> str:
    candidates = raw.get("candidates") or []
    chunks: list[str] = []
    for candidate in candidates:
        for part in candidate.get("content", {}).get("parts", []):
            if part.get("text"):
                chunks.append(part["text"])
    if not chunks:
        raise SystemExit("Gemini response did not contain text content.")
    return "\n".join(chunks)


def run_dify_stage(config: dict) -> Path:
    import requests

    dify = config.get("dify", {})
    if not dify.get("enabled", False):
        return as_path(config["input_json"])

    api_key_env = dify.get("api_key_env", "DIFY_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise SystemExit(f"{api_key_env} is required when dify.enabled=true")
    validate_http_bearer_key(api_key, api_key_env)

    base_url = dify.get("base_url", "https://api.dify.ai/v1").rstrip("/")
    endpoint = dify.get("endpoint", "/workflows/run")
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    url = base_url + endpoint

    inputs = dict(dify.get("inputs", {}))
    apply_dify_file_inputs(
        inputs=inputs,
        file_configs=dify.get("files", []),
        base_url=base_url,
        api_key=api_key,
        user=dify.get("user", "video-pipeline"),
    )

    response_mode = dify.get("response_mode", "blocking")
    payload = {
        "inputs": inputs,
        "response_mode": response_mode,
        "user": dify.get("user", "video-pipeline"),
    }

    print(f"\n== dify-workflow ==\nPOST {url}")
    raw_response_path = as_path(dify.get("raw_response", "outputs/dify/raw_response.json"))
    output_json_path = as_path(dify.get("output_json", "outputs/dify/production_json.json"))
    raw_response_path.parent.mkdir(parents=True, exist_ok=True)
    output_json_path.parent.mkdir(parents=True, exist_ok=True)

    if response_mode == "streaming":
        data = run_dify_streaming_workflow(
            url=url,
            api_key=api_key,
            payload=payload,
            timeout=int(dify.get("timeout", 600)),
            trust_env=bool(dify.get("trust_env_proxy", True)),
            proxy=dify.get("proxy", ""),
            attempts=int(dify.get("network_retries", 3)),
        )
    else:
        response = request_with_retries(
            "post",
            url,
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=int(dify.get("timeout", 600)),
            trust_env=bool(dify.get("trust_env_proxy", True)),
            proxy=dify.get("proxy", ""),
            attempts=int(dify.get("network_retries", 3)),
        )
        if not response.ok:
            raise SystemExit(f"Dify workflow failed: {response.status_code} {response.text[:1000]}")
        data = response.json()

    write_json(raw_response_path, data)
    selected = select_dify_output(data, dify.get("output_key", ""))
    json_text = extract_json_text(selected)
    parsed = normalize_course_json(json.loads(json_text))
    write_json(output_json_path, parsed)
    print(f"Wrote Dify raw response to {raw_response_path}")
    print(f"Wrote Dify production JSON to {output_json_path}")
    return output_json_path


def run_dify_streaming_workflow(
    url: str,
    api_key: str,
    payload: dict,
    timeout: int,
    trust_env: bool,
    proxy: str,
    attempts: int,
) -> dict:
    import requests

    last_exc: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            response = request_once(
                "post",
                url,
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
                trust_env=trust_env,
                proxy=proxy,
                stream=True,
            )
            if not response.ok:
                raise SystemExit(f"Dify workflow failed: {response.status_code} {response.text[:1000]}")
            return parse_dify_sse_response(response)
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
            wait_seconds = min(2 * attempt, 8)
            print(f"Streaming request failed ({type(exc).__name__}); retrying in {wait_seconds}s [{attempt}/{attempts}]")
            time.sleep(wait_seconds)
    raise last_exc or RuntimeError("Dify streaming workflow failed")


def parse_dify_sse_response(response) -> dict:
    events: list[dict] = []
    finished: dict | None = None
    event_count = 0
    last_notice = time.time()
    print("Waiting for Dify streaming events. Press Ctrl+C only if you want to cancel this run.")
    try:
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                if time.time() - last_notice > 30:
                    print(f"Dify still running... received {event_count} events")
                    last_notice = time.time()
                continue
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            events.append(event)
            event_count += 1
            event_name = event.get("event")
            if event_name in {"workflow_started", "node_started", "node_finished"}:
                print(f"Dify event: {event_name}")
            elif event_name in {"text_chunk", "agent_message", "message"}:
                if time.time() - last_notice > 10:
                    print(f"Dify generating... received {event_count} events")
                    last_notice = time.time()
            elif event_name == "workflow_finished":
                finished = event
                print("Dify event: workflow_finished")
                break
            elif event_name == "error":
                raise SystemExit(f"Dify workflow error: {event}")
    except KeyboardInterrupt:
        raise SystemExit("Dify workflow was cancelled by Ctrl+C before it returned workflow_finished.")

    if not finished:
        raise SystemExit("Dify streaming response ended without workflow_finished event.")

    outputs = finished.get("data", {}).get("outputs", {})
    return {
        "data": {
            "outputs": outputs,
            "workflow_run_id": finished.get("workflow_run_id"),
            "task_id": finished.get("task_id"),
            "status": finished.get("data", {}).get("status"),
        },
        "events": events,
    }


def apply_dify_file_inputs(
    inputs: dict,
    file_configs: list[dict],
    base_url: str,
    api_key: str,
    user: str,
) -> None:
    grouped: dict[str, list[dict]] = {}
    text_grouped: dict[str, list[str]] = {}
    for file_config in file_configs or []:
        input_name = (file_config.get("input_name") or "").strip()
        raw_path = (file_config.get("path") or "").strip()
        if not input_name or not raw_path:
            continue

        file_path = as_path(raw_path)
        if not file_path.exists():
            raise SystemExit(f"Dify file input not found: {file_path}")

        try:
            uploaded = upload_dify_file(
                base_url=base_url,
                api_key=api_key,
                file_path=file_path,
                user=user,
                file_type=file_config.get("type", "document"),
                transfer_method=file_config.get("transfer_method", "local_file"),
                trust_env=bool(file_config.get("trust_env_proxy", True)),
                proxy=file_config.get("proxy", ""),
                attempts=int(file_config.get("network_retries", 3)),
            )
            grouped.setdefault(input_name, []).append(uploaded)
        except DifyUnsupportedFileType as exc:
            if not file_config.get("fallback_to_text", True):
                raise SystemExit(str(exc))
            max_chars = int(file_config.get("max_text_chars", 120_000))
            extracted = extract_document_text(file_path, max_chars=max_chars)
            text_grouped.setdefault(input_name, []).append(extracted)
            print(
                f"Dify rejected file upload for {file_path.name}; "
                f"using local text extraction fallback ({len(extracted)} chars)."
            )

    for input_name, files in grouped.items():
        file_config = next((item for item in file_configs if item.get("input_name") == input_name), {})
        as_list = bool(file_config.get("as_list", input_name == "source_doc"))
        if len(files) == 1 and not as_list:
            inputs[input_name] = files[0]
        else:
            inputs[input_name] = files
    for input_name, texts in text_grouped.items():
        inputs[input_name] = "\n\n".join(texts)


def upload_dify_file(
    base_url: str,
    api_key: str,
    file_path: Path,
    user: str,
    file_type: str,
    transfer_method: str,
    trust_env: bool,
    proxy: str,
    attempts: int,
) -> dict:
    import requests

    url = base_url.rstrip("/") + "/files/upload"
    print(f"Uploading Dify file: {file_path}")
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    last_exc: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        with file_path.open("rb") as handle:
            try:
                response = request_once(
                    "post",
                    url,
                    headers={"Authorization": "Bearer " + api_key},
                    data={"user": user},
                    files={"file": (file_path.name, handle, mime_type)},
                    timeout=300,
                    trust_env=trust_env,
                    proxy=proxy,
                )
                break
            except Exception as exc:
                last_exc = exc
                if attempt >= attempts:
                    raise
                wait_seconds = min(2 * attempt, 8)
                print(f"Network request failed ({type(exc).__name__}); retrying in {wait_seconds}s [{attempt}/{attempts}]")
                time.sleep(wait_seconds)
    else:
        raise last_exc or RuntimeError("Dify file upload failed")
    if not response.ok:
        if response.status_code == 415 or "unsupported_file_type" in response.text:
            raise DifyUnsupportedFileType(
                f"Dify file upload failed because this file type is not allowed: {file_path} "
                f"({response.status_code} {response.text[:500]})"
            )
        raise SystemExit(f"Dify file upload failed: {response.status_code} {response.text[:800]}")
    data = response.json()
    file_id = data.get("id") or data.get("upload_file_id")
    if not file_id:
        raise SystemExit(f"Dify file upload response did not contain id: {data}")

    return {
        "transfer_method": transfer_method,
        "upload_file_id": file_id,
        "type": file_type,
    }


def request_once(method: str, url: str, trust_env: bool = True, proxy: str = "", **kwargs):
    import requests

    session = requests.Session()
    session.trust_env = False if proxy else trust_env
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session.request(method, url, **kwargs)


def request_with_retries(method: str, url: str, attempts: int = 3, trust_env: bool = True, proxy: str = "", **kwargs):
    import requests

    last_exc: Exception | None = None
    retry_statuses = {429, 502, 503, 504}
    for attempt in range(1, max(1, attempts) + 1):
        try:
            response = request_once(method, url, trust_env=trust_env, proxy=proxy, **kwargs)
            if response.status_code in retry_statuses and attempt < attempts:
                wait_seconds = min(3 * attempt, 15)
                print(
                    f"Network request returned HTTP {response.status_code}; "
                    f"retrying in {wait_seconds}s [{attempt}/{attempts}]"
                )
                time.sleep(wait_seconds)
                continue
            return response
        except (requests.exceptions.ProxyError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
            wait_seconds = min(2 * attempt, 8)
            print(f"Network request failed ({type(exc).__name__}); retrying in {wait_seconds}s [{attempt}/{attempts}]")
            time.sleep(wait_seconds)
    raise last_exc or RuntimeError("Network request failed")


def extract_document_text(file_path: Path, max_chars: int) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_text(file_path, max_chars=max_chars)
    if suffix == ".docx":
        return extract_docx_text(file_path, max_chars=max_chars)
    if suffix in {".txt", ".md", ".csv", ".json", ".html", ".xml"}:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        return truncate_text(text, max_chars)
    raise SystemExit(
        f"Dify rejected upload and local text fallback does not support {suffix}. "
        "Use DOCX/PDF/TXT/MD/CSV/JSON/HTML/XML, or enable this file type in Dify."
    )


def extract_pdf_text(file_path: Path, max_chars: int) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise SystemExit("pypdf is required for PDF text fallback but is not available.") from exc

    reader = PdfReader(str(file_path))
    chunks: list[str] = []
    total = 0
    for page_no, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if not text.strip():
            continue
        chunk = f"\n\n[Page {page_no}]\n{text.strip()}"
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            break
    if not chunks:
        raise SystemExit(f"No extractable text found in PDF: {file_path}")
    return truncate_text("".join(chunks), max_chars)


def extract_docx_text(file_path: Path, max_chars: int) -> str:
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    chunks: list[str] = []
    with zipfile.ZipFile(file_path) as docx:
        with docx.open("word/document.xml") as handle:
            root = ElementTree.parse(handle).getroot()
    for paragraph in root.findall(".//w:p", namespace):
        texts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        line = "".join(texts).strip()
        if line:
            chunks.append(line)
    if not chunks:
        raise SystemExit(f"No extractable text found in DOCX: {file_path}")
    return truncate_text("\n".join(chunks), max_chars)


def truncate_text(text: str, max_chars: int) -> str:
    cleaned = "\n".join(line.rstrip() for line in text.splitlines())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars] + "\n\n[TRUNCATED: source document exceeded automation max_text_chars]"


def create_dashscope_voice(config: dict, input_json: Path) -> str:
    import requests

    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise SystemExit("DASHSCOPE_API_KEY is required when voice_design.enabled=true")
    validate_http_bearer_key(api_key, "DASHSCOPE_API_KEY")

    voice_cfg = config.get("voice_design", {})
    output_path = as_path(voice_cfg.get("voice_id_output", "outputs/voice_samples/auto_lecturer_voice_id.txt"))
    preview_path = as_path(voice_cfg.get("preview_output", "outputs/voice_samples/auto_lecturer_preview.wav"))
    if output_path.exists() and output_path.read_text(encoding="utf-8").strip():
        return output_path.read_text(encoding="utf-8").strip()

    preview_text = preview_text_from_slide(input_json, int(voice_cfg.get("preview_slide_id", 1)))
    payload = {
        "model": "voice-enrollment",
        "input": {
            "action": "create_voice",
            "target_model": voice_cfg.get("target_model", "cosyvoice-v3.5-plus"),
            "voice_prompt": voice_cfg.get("voice_prompt", ""),
            "preview_text": preview_text,
            "prefix": voice_cfg.get("prefix", "lecturer_auto"),
            "language_hints": ["zh"],
        },
        "parameters": {
            "sample_rate": 24000,
            "response_format": "wav",
        },
    }

    response = requests.post(
        "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if not response.ok:
        raise SystemExit(f"DashScope voice design failed: {response.status_code} {response.text[:800]}")

    data = response.json()
    voice_id = data["output"]["voice_id"]
    preview_audio = data.get("output", {}).get("preview_audio", {}).get("data")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(voice_id, encoding="utf-8")
    if preview_audio:
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(base64.b64decode(preview_audio))
    write_json(output_path.with_suffix(".manifest.json"), {"voice_id": voice_id, "request_id": data.get("request_id")})
    print(f"Created DashScope voice: {voice_id}")
    return voice_id


def run_deck_stage(config: dict, input_json: Path, deck_dir: Path) -> None:
    normalize_course_json_file(input_json)
    ensure_clean_dir(deck_dir, clean=False)
    run_step(
        "generate-html-deck",
        [
            sys.executable,
            str(ROOT / "generate_obsidian_deck.py"),
            "--input",
            str(input_json),
            "--out-dir",
            str(deck_dir),
            "--asset-prefix",
            config.get("asset_prefix", "../../html-ppt-skill"),
        ],
    )


def run_capture_stage(config: dict, deck_dir: Path, frames_dir: Path) -> None:
    capture_cfg = config.get("capture", {})
    manifest = capture_manifest_path(frames_dir)
    if capture_cfg.get("skip_if_manifest_exists", False) and manifest.exists():
        print(f"\n== capture-html-deck ==\nSkipping because manifest exists: {manifest}")
        return

    ensure_clean_dir(frames_dir, clean=not capture_cfg.get("skip_if_manifest_exists", False))
    size = config.get("slide_size", {})
    cmd = [
        sys.executable,
        str(ROOT / "capture_html_deck.py"),
        "--deck",
        str(deck_dir / "index.html"),
        "--data",
        str(deck_data_path(deck_dir)),
        "--out-dir",
        str(frames_dir),
        "--chrome",
        str(as_path(config.get("chrome", r"C:/Program Files/Google/Chrome/Application/chrome.exe"))),
        "--width",
        str(int(size.get("width", 1920))),
        "--height",
        str(int(size.get("height", 1080))),
        "--timeout",
        str(int(capture_cfg.get("timeout", 120))),
        "--retries",
        str(int(capture_cfg.get("retries", 2))),
    ]
    if capture_cfg.get("include_highlights", False):
        cmd.append("--include-highlights")
    if capture_cfg.get("skip_if_manifest_exists", False):
        cmd.append("--skip-existing")
    run_step("capture-html-deck", cmd)


def run_compose_stage(config: dict, deck_dir: Path, frames_dir: Path, video_dir: Path, voice_id: str) -> None:
    tts = config.get("tts", {})
    video_effects = config.get("video_effects", {})
    avatar = config.get("avatar", {})
    size = config.get("slide_size", {})
    provider = tts.get("provider", "dashscope")
    ensure_clean_dir(video_dir, clean=False)
    # video_effects controls the rendered video track; TTS settings still control audio timing.
    cmd = [
        sys.executable,
        str(ROOT / "compose_html_video.py"),
        "--data",
        str(deck_data_path(deck_dir)),
        "--capture-manifest",
        str(capture_manifest_path(frames_dir)),
        "--frames-dir",
        str(frames_dir),
        "--out-dir",
        str(video_dir),
        "--tts-provider",
        provider,
        "--audio-granularity",
        tts.get("audio_granularity", "segment"),
        "--segment-gap",
        str(float(tts.get("segment_gap", 0.06))),
        "--pause",
        str(float(tts.get("slide_pause", 0.45))),
        "--voice",
        tts.get("edge_voice", tts.get("voice", "zh-CN-YunxiNeural")),
        "--rate",
        tts.get("edge_rate", tts.get("rate", "+0%")),
        "--sapi-voice",
        tts.get("sapi_voice", "Microsoft Huihui Desktop"),
        "--visual-mode",
        video_effects.get("mode", "element-entrance"),
        "--slide-transition",
        video_effects.get("slide_transition", "wipeleft"),
        "--entrance-duration",
        str(float(video_effects.get("entrance_duration", 0.62))),
        "--fps",
        str(int(video_effects.get("fps", 30))),
        "--width",
        str(int(size.get("width", 1920))),
        "--height",
        str(int(size.get("height", 1080))),
        "--chrome",
        str(as_path(config.get("chrome", r"C:/Program Files/Google/Chrome/Application/chrome.exe"))),
    ]
    if avatar.get("mode", "none") != "none":
        cmd.extend(
            [
                "--avatar-mode",
                avatar.get("mode", "2d"),
                "--avatar-position",
                avatar.get("position", "bottom-right"),
                "--avatar-scale",
                str(float(avatar.get("scale", 0.22))),
            ]
        )
    if tts.get("proxy"):
        cmd.extend(["--tts-proxy", tts.get("proxy", "")])

    if provider == "dashscope":
        cmd.extend(
            [
                "--dashscope-model",
                tts.get("dashscope_model", "cosyvoice-v3.5-plus"),
                "--dashscope-voice-id",
                voice_id,
                "--dashscope-instruction",
                tts.get("dashscope_instruction", NATURAL_DASHSCOPE_TTS_INSTRUCTION),
            ]
        )
    if provider == "openai":
        cmd.extend(
            [
                "--openai-tts-model",
                tts.get("openai_tts_model", "gpt-4o-mini-tts"),
                "--openai-voice",
                tts.get("openai_voice", "nova"),
                "--openai-speed",
                str(float(tts.get("openai_speed", 1.08))),
                "--openai-instructions",
                tts.get("openai_instructions", NATURAL_OPENAI_TTS_INSTRUCTION),
            ]
        )
    if provider == "minimax":
        cmd.extend(
            [
                "--minimax-base-url",
                tts.get("minimax_base_url", ""),
                "--minimax-api-key-env",
                tts.get("minimax_api_key_env", "MINIMAX_TTS_API_KEY"),
                "--minimax-model",
                tts.get("minimax_model", "speech-02-hd"),
                "--minimax-voice",
                tts.get("minimax_voice", "female-yujie"),
                "--minimax-speed",
                str(float(tts.get("minimax_speed", 1.0))),
                "--minimax-protocol",
                tts.get("minimax_protocol", "auto"),
            ]
        )
    if tts.get("force", False):
        cmd.append("--force-tts")
    run_step("compose-video", cmd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the HTML slide to narrated MP4 pipeline.")
    parser.add_argument("--config", type=Path, default=ROOT / "pipeline_config.example.json")
    parser.add_argument("--stage", choices=["all", "dify", "llm", "deck", "capture", "compose"], default="all")
    parser.add_argument("--input-json", type=Path, help="Override config input_json")
    parser.add_argument("--enable-dify", action="store_true", help="Override config dify.enabled=true")
    parser.add_argument("--enable-llm", action="store_true", help="Override config llm.enabled=true")
    parser.add_argument("--llm-provider", choices=["openai", "anthropic", "claude", "gemini", "minimax"], help="Direct content generation provider.")
    parser.add_argument("--llm-model", help="Direct content generation model name.")
    parser.add_argument("--llm-api-key-env", help="Environment variable that contains the direct LLM API key.")
    parser.add_argument("--llm-base-url", help="Direct LLM API base URL.")
    parser.add_argument("--llm-source-document", type=Path, help="Source document for direct LLM generation.")
    parser.add_argument("--topic", help="Override dify.inputs.topic")
    parser.add_argument("--audience", help="Override dify.inputs.audience")
    parser.add_argument("--slide-count", help="Override dify.inputs.slide_count")
    parser.add_argument("--style", help="Override dify.inputs.style")
    parser.add_argument("--video-style", help="Override dify.inputs.video_style")
    parser.add_argument("--highlight-style", help="Override dify.inputs.highlight_style")
    parser.add_argument("--generation-goal", help="Override dify.inputs.generation_goal")
    parser.add_argument("--audio-style", help="Override dify.inputs.audio_style")
    parser.add_argument("--document", type=Path, help="Override the first dify.files item path")
    parser.add_argument(
        "--dify-input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Override or add a Dify input. Can be used multiple times.",
    )
    parser.add_argument("--dify-output-key", help="Override dify.output_key")
    parser.add_argument("--dify-response-mode", choices=["blocking", "streaming"], help="Override Dify workflow response_mode")
    parser.add_argument("--dify-timeout", type=int, help="Override dify.timeout seconds")
    parser.add_argument("--dify-network-retries", type=int, help="Override dify.network_retries")
    parser.add_argument("--dify-proxy", help="Explicit proxy for Dify requests, e.g. http://127.0.0.1:7897")
    parser.add_argument("--dify-no-proxy", action="store_true", help="Do not let requests read proxy settings from environment.")
    parser.add_argument("--tts-provider", choices=["auto", "edge", "sapi", "dashscope", "openai", "minimax"], help="Override config tts.provider")
    parser.add_argument("--tts-audio-granularity", choices=["slide", "segment"], help="Override config tts.audio_granularity")
    parser.add_argument("--tts-proxy", help="Explicit proxy for TTS HTTP requests, e.g. http://127.0.0.1:7897")
    parser.add_argument(
        "--visual-mode",
        choices=["static", "ppt-transition", "element-entrance"],
        help="Override config video_effects.mode. static: still screenshots; ppt-transition: page transitions; element-entrance: reveal elements by subtitle timing.",
    )
    parser.add_argument(
        "--slide-transition",
        choices=["fade", "wipeleft", "wiperight", "wipeup", "wipedown"],
        help="Override config video_effects.slide_transition, used during silent slide-change pauses.",
    )
    parser.add_argument(
        "--entrance-duration",
        type=float,
        help="Override config video_effects.entrance_duration, in seconds, for each newly referenced element entrance.",
    )
    parser.add_argument("--openai-tts-model", help="Override config tts.openai_tts_model")
    parser.add_argument("--openai-voice", help="Override config tts.openai_voice")
    parser.add_argument("--openai-speed", type=float, help="Override config tts.openai_speed")
    parser.add_argument("--avatar-mode", choices=["none", "2d"], help="Override config avatar.mode")
    parser.add_argument("--avatar-position", choices=["bottom-right", "bottom-left"], help="Override config avatar.position")
    parser.add_argument("--avatar-scale", type=float, help="Override config avatar.scale")
    parser.add_argument("--minimax-base-url", help="Override config tts.minimax_base_url")
    parser.add_argument("--minimax-api-key-env", help="Override config tts.minimax_api_key_env")
    parser.add_argument("--minimax-model", help="Override config tts.minimax_model")
    parser.add_argument("--minimax-voice", help="Override config tts.minimax_voice")
    parser.add_argument("--minimax-speed", type=float, help="Override config tts.minimax_speed")
    parser.add_argument("--minimax-protocol", choices=["auto", "openai", "official"], help="Override config tts.minimax_protocol")
    parser.add_argument("--capture-timeout", type=int, help="Override capture.timeout seconds")
    parser.add_argument("--capture-retries", type=int, help="Override capture.retries")
    parser.add_argument("--no-capture-highlights", action="store_true", help="Only capture base slide screenshots; compose renders highlights when needed.")
    parser.add_argument("--force-tts", action="store_true", help="Override config tts.force=true")
    parser.add_argument("--skip-capture-if-exists", action="store_true")
    return parser.parse_args()


def parse_scalar(value: str):
    lower = value.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower == "null":
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def apply_runtime_overrides(config: dict, args: argparse.Namespace) -> None:
    if args.enable_dify:
        config.setdefault("dify", {})["enabled"] = True
    if args.enable_llm:
        config.setdefault("llm", {})["enabled"] = True
        config.setdefault("dify", {})["enabled"] = False
    if args.llm_provider is not None:
        config.setdefault("llm", {})["provider"] = args.llm_provider
    if args.llm_model is not None:
        config.setdefault("llm", {})["model"] = args.llm_model
    if args.llm_api_key_env is not None:
        config.setdefault("llm", {})["api_key_env"] = args.llm_api_key_env
    if args.llm_base_url is not None:
        config.setdefault("llm", {})["base_url"] = args.llm_base_url
    if args.llm_source_document is not None:
        config.setdefault("llm", {})["source_document"] = str(args.llm_source_document)
    if args.force_tts:
        config.setdefault("tts", {})["force"] = True
    if args.tts_provider is not None:
        config.setdefault("tts", {})["provider"] = args.tts_provider
    if args.tts_audio_granularity is not None:
        config.setdefault("tts", {})["audio_granularity"] = args.tts_audio_granularity
    if args.tts_proxy is not None:
        config.setdefault("tts", {})["proxy"] = args.tts_proxy
    if args.visual_mode is not None:
        config.setdefault("video_effects", {})["mode"] = args.visual_mode
    if args.slide_transition is not None:
        config.setdefault("video_effects", {})["slide_transition"] = args.slide_transition
    if args.entrance_duration is not None:
        config.setdefault("video_effects", {})["entrance_duration"] = args.entrance_duration
    if args.openai_tts_model is not None:
        config.setdefault("tts", {})["openai_tts_model"] = args.openai_tts_model
    if args.openai_voice is not None:
        config.setdefault("tts", {})["openai_voice"] = args.openai_voice
    if args.openai_speed is not None:
        config.setdefault("tts", {})["openai_speed"] = args.openai_speed
    if args.avatar_mode is not None:
        config.setdefault("avatar", {})["mode"] = args.avatar_mode
    if args.avatar_position is not None:
        config.setdefault("avatar", {})["position"] = args.avatar_position
    if args.avatar_scale is not None:
        config.setdefault("avatar", {})["scale"] = args.avatar_scale
    if args.minimax_base_url is not None:
        config.setdefault("tts", {})["minimax_base_url"] = args.minimax_base_url
    if args.minimax_api_key_env is not None:
        config.setdefault("tts", {})["minimax_api_key_env"] = args.minimax_api_key_env
    if args.minimax_model is not None:
        config.setdefault("tts", {})["minimax_model"] = args.minimax_model
    if args.minimax_voice is not None:
        config.setdefault("tts", {})["minimax_voice"] = args.minimax_voice
    if args.minimax_speed is not None:
        config.setdefault("tts", {})["minimax_speed"] = args.minimax_speed
    if args.minimax_protocol is not None:
        config.setdefault("tts", {})["minimax_protocol"] = args.minimax_protocol
    if args.capture_timeout is not None:
        config.setdefault("capture", {})["timeout"] = args.capture_timeout
    if args.capture_retries is not None:
        config.setdefault("capture", {})["retries"] = args.capture_retries
    if args.no_capture_highlights:
        config.setdefault("capture", {})["include_highlights"] = False
    if args.skip_capture_if_exists:
        config.setdefault("capture", {})["skip_if_manifest_exists"] = True
    if args.dify_output_key is not None:
        config.setdefault("dify", {})["output_key"] = args.dify_output_key
    if args.dify_response_mode is not None:
        config.setdefault("dify", {})["response_mode"] = args.dify_response_mode
    if args.dify_timeout is not None:
        config.setdefault("dify", {})["timeout"] = args.dify_timeout
    if args.dify_network_retries is not None:
        config.setdefault("dify", {})["network_retries"] = args.dify_network_retries
    if args.dify_proxy is not None:
        config.setdefault("dify", {})["proxy"] = args.dify_proxy
        config.setdefault("dify", {})["trust_env_proxy"] = False
        for item in config.setdefault("dify", {}).setdefault("files", []):
            item["proxy"] = args.dify_proxy
            item["trust_env_proxy"] = False
    if args.dify_no_proxy:
        config.setdefault("dify", {})["trust_env_proxy"] = False
        for item in config.setdefault("dify", {}).setdefault("files", []):
            item["trust_env_proxy"] = False
    dify_inputs = config.setdefault("dify", {}).setdefault("inputs", {})
    if args.topic is not None:
        dify_inputs["topic"] = args.topic
    if args.audience is not None:
        dify_inputs["audience"] = args.audience
    if args.slide_count is not None:
        dify_inputs["slide_count"] = args.slide_count
    if args.style is not None:
        dify_inputs["style"] = args.style
    if args.video_style is not None:
        dify_inputs["video_style"] = args.video_style
    if args.highlight_style is not None:
        dify_inputs["highlight_style"] = args.highlight_style
    if args.generation_goal is not None:
        dify_inputs["generation_goal"] = args.generation_goal
    if args.audio_style is not None:
        dify_inputs["audio_style"] = args.audio_style

    for item in args.dify_input:
        if "=" not in item:
            raise SystemExit(f"--dify-input must be KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"--dify-input key cannot be empty: {item}")
        dify_inputs[key] = parse_scalar(value.strip())

    if args.document is not None:
        files = config.setdefault("dify", {}).setdefault("files", [])
        if not files:
            files.append(
                {
                    "input_name": "source_doc",
                    "path": "",
                    "type": "document",
                    "transfer_method": "local_file",
                }
            )
        files[0]["path"] = str(args.document)


def main() -> None:
    args = parse_args()
    config = load_json(args.config)
    apply_runtime_overrides(config, args)

    input_json = as_path(args.input_json or config["input_json"])
    deck_dir = as_path(config.get("deck_dir", ROOT / "html_deck_obsidian"))
    frames_dir = as_path(config.get("frames_dir", ROOT / "outputs/html_obsidian_slides"))
    video_dir = as_path(config.get("video_dir", ROOT / "outputs/html_video"))

    if args.stage in {"all", "llm"} and config.get("llm", {}).get("enabled", False):
        input_json = run_llm_stage(config)
    elif args.stage == "llm":
        print("LLM stage is disabled in config; nothing to run.")
        return
    elif args.stage in {"all", "dify"} and config.get("dify", {}).get("enabled", False):
        input_json = run_dify_stage(config)
    elif args.stage == "dify":
        print("Dify stage is disabled in config; nothing to run.")
        return

    if config.get("voice_design", {}).get("enabled", False):
        voice_id = create_dashscope_voice(config, input_json)
        config.setdefault("tts", {})["dashscope_voice_id"] = voice_id
    else:
        voice_id = resolve_voice_id(config)

    if config.get("tts", {}).get("provider", "dashscope") == "dashscope" and args.stage in {"all", "compose"} and not voice_id:
        raise SystemExit("DashScope compose requires tts.dashscope_voice_id or tts.dashscope_voice_id_file.")

    if args.stage in {"all", "deck"}:
        run_deck_stage(config, input_json, deck_dir)
    if args.stage in {"all", "capture"}:
        run_capture_stage(config, deck_dir, frames_dir)
    if args.stage in {"all", "compose"}:
        run_compose_stage(config, deck_dir, frames_dir, video_dir, voice_id)

    print("\nPipeline completed.")
    print(f"Video: {video_dir / 'data_jobs_industry_guide.mp4'}")
    print(f"Manifest: {video_dir / 'compose_manifest.json'}")


if __name__ == "__main__":
    main()
