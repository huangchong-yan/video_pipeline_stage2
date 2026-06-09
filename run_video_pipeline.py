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
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class DifyUnsupportedFileType(RuntimeError):
    pass


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def as_path(value: str | Path, base: Path = ROOT) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path


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


def run_dify_stage(config: dict) -> Path:
    import requests

    dify = config.get("dify", {})
    if not dify.get("enabled", False):
        return as_path(config["input_json"])

    api_key_env = dify.get("api_key_env", "DIFY_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise SystemExit(f"{api_key_env} is required when dify.enabled=true")

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
    parsed = json.loads(json_text)
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
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
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
        event_name = event.get("event")
        if event_name in {"workflow_started", "node_started", "node_finished"}:
            print(f"Dify event: {event_name}")
        elif event_name == "workflow_finished":
            finished = event
            print("Dify event: workflow_finished")
            break
        elif event_name == "error":
            raise SystemExit(f"Dify workflow error: {event}")

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
    for attempt in range(1, max(1, attempts) + 1):
        try:
            response = request_once(method, url, trust_env=trust_env, proxy=proxy, **kwargs)
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
    if suffix in {".txt", ".md", ".csv", ".json", ".html", ".xml"}:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        return truncate_text(text, max_chars)
    raise SystemExit(
        f"Dify rejected upload and local text fallback does not support {suffix}. "
        "Use PDF/TXT/MD/CSV/JSON/HTML/XML, or enable this file type in Dify."
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
    ]
    if capture_cfg.get("include_highlights", True):
        cmd.append("--include-highlights")
    run_step("capture-html-deck", cmd)


def run_compose_stage(config: dict, deck_dir: Path, frames_dir: Path, video_dir: Path, voice_id: str) -> None:
    tts = config.get("tts", {})
    ensure_clean_dir(video_dir, clean=False)
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
        tts.get("provider", "dashscope"),
        "--audio-granularity",
        tts.get("audio_granularity", "segment"),
        "--segment-gap",
        str(float(tts.get("segment_gap", 0.18))),
        "--pause",
        str(float(tts.get("slide_pause", 0.45))),
    ]

    if tts.get("provider", "dashscope") == "dashscope":
        cmd.extend(
            [
                "--dashscope-model",
                tts.get("dashscope_model", "cosyvoice-v3.5-plus"),
                "--dashscope-voice-id",
                voice_id,
                "--dashscope-instruction",
                tts.get("dashscope_instruction", "Speak like a calm professional online-course lecturer with natural pauses."),
            ]
        )
    if tts.get("force", False):
        cmd.append("--force-tts")
    run_step("compose-video", cmd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the HTML slide to narrated MP4 pipeline.")
    parser.add_argument("--config", type=Path, default=ROOT / "pipeline_config.example.json")
    parser.add_argument("--stage", choices=["all", "dify", "deck", "capture", "compose"], default="all")
    parser.add_argument("--input-json", type=Path, help="Override config input_json")
    parser.add_argument("--enable-dify", action="store_true", help="Override config dify.enabled=true")
    parser.add_argument("--topic", help="Override dify.inputs.topic")
    parser.add_argument("--audience", help="Override dify.inputs.audience")
    parser.add_argument("--slide-count", help="Override dify.inputs.slide_count")
    parser.add_argument("--style", help="Override dify.inputs.style")
    parser.add_argument("--video-style", help="Override dify.inputs.video_style")
    parser.add_argument("--highlight-style", help="Override dify.inputs.highlight_style")
    parser.add_argument("--generation-goal", help="Override dify.inputs.generation_goal")
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
    parser.add_argument("--dify-proxy", help="Explicit proxy for Dify requests, e.g. http://127.0.0.1:7897")
    parser.add_argument("--dify-no-proxy", action="store_true", help="Do not let requests read proxy settings from environment.")
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
    if args.force_tts:
        config.setdefault("tts", {})["force"] = True
    if args.skip_capture_if_exists:
        config.setdefault("capture", {})["skip_if_manifest_exists"] = True
    if args.dify_output_key is not None:
        config.setdefault("dify", {})["output_key"] = args.dify_output_key
    if args.dify_response_mode is not None:
        config.setdefault("dify", {})["response_mode"] = args.dify_response_mode
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

    if args.stage in {"all", "dify"} and config.get("dify", {}).get("enabled", False):
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
