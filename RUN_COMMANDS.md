# Run Commands

These commands are PowerShell-ready. Secrets are passed through environment variables and should not be committed.

## One-Time Environment Variables

```powershell
$env:DIFY_API_KEY = "replace-with-your-dify-workflow-api-key"
$env:DASHSCOPE_API_KEY = "replace-with-your-dashscope-api-key"
$env:OPENAI_API_KEY = "replace-with-your-openai-api-key"
$env:MINIMAX_TTS_API_KEY = "replace-with-your-minimax-relay-token"
```

## Fast Full Pipeline

This is the recommended path for normal use. It captures only base slide screenshots. Element entrance and highlights are rendered during compose, which avoids launching Chrome for every highlight state.

```powershell
cd D:\agent\video_pipeline_stage2

D:\Anaconda3\python.exe D:\agent\video_pipeline_stage2\run_video_pipeline.py --config D:\agent\video_pipeline_stage2\pipeline_config.json --stage all --enable-dify --dify-proxy http://127.0.0.1:7897 --dify-response-mode streaming --dify-timeout 900 --dify-network-retries 5 --document "C:\Users\Administrator\Desktop\your_document.docx" --slide-count 8 --video-style xhs_white_editorial --highlight-style orange_box --generation-goal "Generate a clear short-video course guide from the source document." --tts-provider edge --tts-audio-granularity segment --visual-mode element-entrance --slide-transition wipeleft --entrance-duration 0.62 --tts-proxy http://127.0.0.1:7897 --no-capture-highlights --force-tts
```

## Resume Capture Only

Use this when Dify and deck generation already succeeded.

```powershell
cd D:\agent\video_pipeline_stage2

D:\Anaconda3\python.exe D:\agent\video_pipeline_stage2\run_video_pipeline.py --config D:\agent\video_pipeline_stage2\pipeline_config.json --stage capture --skip-capture-if-exists --no-capture-highlights --capture-timeout 30 --capture-retries 0
```

## Compose Only

Use this after `capture_manifest.json` exists.

```powershell
cd D:\agent\video_pipeline_stage2

D:\Anaconda3\python.exe D:\agent\video_pipeline_stage2\run_video_pipeline.py --config D:\agent\video_pipeline_stage2\pipeline_config.json --stage compose --tts-provider edge --tts-audio-granularity segment --visual-mode element-entrance --slide-transition wipeleft --entrance-duration 0.62 --tts-proxy http://127.0.0.1:7897 --force-tts
```

## Direct LLM Instead Of Dify

```powershell
cd D:\agent\video_pipeline_stage2

D:\Anaconda3\python.exe D:\agent\video_pipeline_stage2\run_video_pipeline.py --config D:\agent\video_pipeline_stage2\pipeline_config.json --stage all --enable-llm --llm-provider openai --llm-model gpt-4.1 --llm-api-key-env OPENAI_API_KEY --llm-source-document "C:\Users\Administrator\Desktop\your_document.docx" --slide-count 8 --video-style xhs_white_editorial --highlight-style orange_box --generation-goal "Generate a clear short-video course guide from the source document." --tts-provider edge --tts-audio-granularity segment --visual-mode element-entrance --slide-transition wipeleft --entrance-duration 0.62 --no-capture-highlights --force-tts
```
