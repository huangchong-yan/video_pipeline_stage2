# Dify-to-Video Pipeline

This project turns a source document into a narrated course-style video.

For copy-paste-ready PowerShell commands with all runtime parameters, see `RUN_COMMANDS.md`.

## Demo

<video src="demo/data_jobs_industry_guide_1_5x.mp4" controls width="100%"></video>

If the embedded player is not shown by GitHub, open the demo video directly:
[`demo/data_jobs_industry_guide_1_5x.mp4`](demo/data_jobs_industry_guide_1_5x.mp4)

Pipeline:

```text
Source document -> Dify or direct LLM -> production JSON -> HTML slides -> screenshots -> TTS -> subtitles -> MP4
```

Generated outputs are intentionally ignored by Git.

## What To Commit

Commit source files, config templates, and documentation.

Do not commit:

- `outputs/`
- `html_deck/`
- `html_deck_obsidian/`
- `pipeline_config.json`
- API keys or `.env` files
- generated audio/video/images

## Requirements

- Python 3.10+
- Google Chrome, for headless slide screenshots
- A Dify application API key, if using Dify
- An OpenAI, Anthropic/Claude, or Gemini API key, if using direct LLM generation
- A DashScope or OpenAI API key, if using those TTS providers
- The HTML slide assets referenced by `asset_prefix`, currently `../html-ppt-skill` from this project directory

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Configuration

Create a local config from the template:

```powershell
Copy-Item pipeline_config.example.json pipeline_config.json
```

Set API keys in the shell. Do not put keys in JSON files.

```powershell
$env:DIFY_API_KEY = "app-..."
$env:DASHSCOPE_API_KEY = "sk-..."
$env:OPENAI_API_KEY = "..."
$env:ANTHROPIC_API_KEY = "..."
$env:GEMINI_API_KEY = "..."
```

## Full Run With Dify

Run with Dify enabled and a local document:

```powershell
python run_video_pipeline.py `
  --config pipeline_config.json `
  --enable-dify `
  --dify-proxy "http://127.0.0.1:7897" `
  --dify-response-mode streaming `
  --document "C:\path\to\source.docx" `
  --slide-count 8 `
  --video-style "professional paid-course style, clear and beginner-friendly" `
  --highlight-style "orange_box" `
  --generation-goal "Generate a complete course-style PPT production JSON that can be composed directly into video."
```

If you do not need a proxy, omit `--dify-proxy`.

For long Dify workflows, `--dify-response-mode streaming` is recommended because it is more robust through local proxies than one long blocking request.

## Full Run With Direct LLM

You can skip Dify and generate the production JSON directly with OpenAI, Claude, or Gemini:

```powershell
python run_video_pipeline.py `
  --config pipeline_config.json `
  --enable-llm `
  --llm-provider openai `
  --llm-model gpt-4.1 `
  --llm-source-document "C:\path\to\source.docx" `
  --stage all
```

Provider examples:

```powershell
--llm-provider openai --llm-model gpt-4.1
--llm-provider claude --llm-model claude-sonnet-4-5
--llm-provider gemini --llm-model gemini-2.5-pro
```

The direct LLM path reads DOCX, PDF, TXT, MD, CSV, JSON, HTML, and XML files locally, then asks the selected model to return the same production JSON shape used by the rest of the pipeline.

If the selected provider needs a local proxy, set `llm.proxy` in `pipeline_config.json`, for example:

```json
"proxy": "http://127.0.0.1:7897"
```

## Dify Start Node Inputs

The current pipeline is aligned with this Dify Start node shape:

```text
source_doc       array[file]
slide_count      text
video_style      text
highlight_style  text
generation_goal  text
audio_style      text
```

`--document` is uploaded to Dify and passed as `source_doc`.

Recommended LLM prompt wording inside Dify:

```text
Audio style:
{{audio_style}}

When generating tts_script and subtitle_segments.text:
- Write spoken Chinese, not written prose.
- Use natural punctuation for speech rhythm: commas, semicolons, dashes, ellipses, short rhetorical questions.
- Make the speaker sound like an energetic classroom teacher: friendly, clear, lightly interactive, but not exaggerated.
- Encode delivery through wording and punctuation. Do not output bracketed stage directions like [pause] or (smile).
- Avoid announcer style, AI cadence, and ending every sentence with the same full stop rhythm.
```

The script uploads local files via:

```text
POST /files/upload
```

Then it passes the result to `/workflows/run` as:

```json
"source_doc": [
  {
    "transfer_method": "local_file",
    "upload_file_id": "...",
    "type": "document"
  }
]
```

If Dify rejects an uploaded file type, the pipeline can fall back to local text extraction for PDF/TXT/MD/CSV/JSON/HTML/XML files and pass the extracted text into the same input variable.

## Stage Runs

Run only one stage:

```powershell
python run_video_pipeline.py --config pipeline_config.json --stage llm
python run_video_pipeline.py --config pipeline_config.json --stage dify
python run_video_pipeline.py --config pipeline_config.json --stage deck
python run_video_pipeline.py --config pipeline_config.json --stage capture
python run_video_pipeline.py --config pipeline_config.json --stage compose
```

Force TTS regeneration:

```powershell
python run_video_pipeline.py --config pipeline_config.json --stage compose --force-tts
```

Choose a TTS provider:

```powershell
python run_video_pipeline.py --config pipeline_config.json --stage compose --tts-provider dashscope --tts-audio-granularity slide --force-tts
python run_video_pipeline.py --config pipeline_config.json --stage compose --tts-provider edge --tts-audio-granularity segment --force-tts
```

For HTTP TTS providers such as OpenAI or DashScope, add `--tts-proxy "http://127.0.0.1:7897"` when needed.

Notes:

- Claude is used for PPT structure and narration text generation only; it is not a TTS provider.
- `segment` granularity gives better subtitle and highlight timing because each subtitle segment gets its own audio file.
- `slide` granularity is faster and cheaper, but subtitle timing is estimated inside each slide.

Skip screenshot recapture if the manifest already exists:

```powershell
python run_video_pipeline.py --config pipeline_config.json --stage all --skip-capture-if-exists
```

## Direct JSON Input

If you already have a valid production JSON and do not want to call Dify:

```powershell
python run_video_pipeline.py `
  --config pipeline_config.json `
  --input-json "C:\path\to\production_json.json" `
  --stage all
```

In this mode, keep `dify.enabled` false or omit `--enable-dify`.

## Key Outputs

Generated under `outputs/`:

- `outputs/dify/raw_response.json`
- `outputs/dify/production_json.json`
- `outputs/html_obsidian_slides/`
- `outputs/html_video/data_jobs_industry_guide.mp4`
- `outputs/html_video/subtitles.srt`
- `outputs/html_video/compose_manifest.json`

## Model Notes

Content generation providers:

- `dify`: use your Dify Workflow as the orchestration layer.
- `openai`: call the OpenAI Responses API directly.
- `claude` / `anthropic`: call the Anthropic Messages API directly.
- `gemini`: call the Gemini Generate Content API directly.

TTS providers:

- `edge`: convenient local/free fallback through `edge-tts`.
- `sapi`: Windows built-in speech fallback.
- `dashscope`: DashScope CosyVoice-compatible API.
- `openai`: OpenAI audio speech API.

The project supports optional DashScope voice design.

Voice prompts must describe an original voice style only. Do not request imitation of a real person.

## Main Scripts

- `run_video_pipeline.py`: orchestration entry point
- `generate_obsidian_deck.py`: production JSON to HTML deck
- `capture_html_deck.py`: HTML deck to PNG screenshots
- `compose_html_video.py`: screenshots + TTS + subtitles to MP4
- `render_slides.py`: earlier PIL-based slide renderer
