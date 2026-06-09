# Dify-to-Video Pipeline

This project turns a Dify Workflow output into a narrated course-style video.

Pipeline:

```text
Dify Workflow -> production JSON -> HTML slides -> slide screenshots -> TTS -> subtitles -> MP4
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
- A DashScope API key, if using DashScope TTS
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
```

## Full Run

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

## Dify Start Node Inputs

The current pipeline is aligned with this Dify Start node shape:

```text
source_doc       array[file]
slide_count      text
video_style      text
highlight_style  text
generation_goal  text
```

`--document` is uploaded to Dify and passed as `source_doc`.

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
python run_video_pipeline.py --config pipeline_config.json --stage dify
python run_video_pipeline.py --config pipeline_config.json --stage deck
python run_video_pipeline.py --config pipeline_config.json --stage capture
python run_video_pipeline.py --config pipeline_config.json --stage compose
```

Force TTS regeneration:

```powershell
python run_video_pipeline.py --config pipeline_config.json --stage compose --force-tts
```

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

These files are ignored by Git.

## Voice Notes

The project supports DashScope TTS and optional DashScope voice design.

Voice prompts must describe an original voice style only. Do not request imitation of a real person.

## Main Scripts

- `run_video_pipeline.py`: orchestration entry point
- `generate_obsidian_deck.py`: production JSON to HTML deck
- `capture_html_deck.py`: HTML deck to PNG screenshots
- `compose_html_video.py`: screenshots + TTS + subtitles to MP4
- `render_slides.py`: earlier PIL-based slide renderer
