# Lecturify / OpenMAIC Integration Notes

## Local Sources

- Lecturify was downloaded to `D:\agent\Lecturify`.
- OpenMAIC was inspected from `D:\agent\OpenMAIC-main-AI教育`.
- Current production pipeline remains `D:\agent\video_pipeline_stage2`.

## What Lecturify Does Differently

Lecturify is closer to an end-to-end AI course production studio:

- It has a planner/designer/content-allocation pipeline before rendering.
- It allocates visible slide text and narration separately, so pages stay lighter.
- It tracks emphasized keywords and maps them to highlight timing.
- It uses HyperFrames for HTML animation rendering.
- It includes a frontend/backend workflow with SSE progress updates.
- It has multiple TTS/subtitle modules, including MiniMax, Fish Audio, ChatTTS, mock TTS, and subtitle alignment.

The practical visual effect is more "produced course video": fewer overloaded pages, more keyword-level focus, and smoother animation if its HyperFrames stack is used.

## What OpenMAIC Does Differently

OpenMAIC is stronger at classroom interaction design:

- It models teaching as action sequences, such as `spotlight`, `laser`, text explanation, and whiteboard actions.
- It treats visual focus as part of the lesson flow rather than just decoration.
- It is better suited to "teacher guiding the viewer through a screen" style videos.

The practical effect is more like a live class: the viewer sees the teacher's attention move across the screen, and narration can stay natural instead of saying "now I highlight this".

## What Was Integrated Into This Pipeline

Implemented in `run_video_pipeline.py`:

- Lecturify-style `emphasized_keywords` support.
- Optional `timing_map` compatibility in the generation prompt.
- Automatic element ID normalization from `id` to `element_id`.
- Automatic subtitle segment fallback when the model omits `subtitle_segments`.
- Automatic highlight repair when target IDs are missing or invalid.
- Keyword-to-highlight conversion so important concepts become focus points.
- OpenMAIC-style `teaching_actions` normalization remains supported.
- MiniMax-compatible direct LLM generation is supported through `/v1/chat/completions`.

Implemented in `generate_obsidian_deck.py`:

- The XiaoHongShu deck style now uses `emphasized_keywords` as visible keyword chips when present.

## What Was Not Merged Yet

HyperFrames from Lecturify was not merged into the main pipeline yet.

Reason: it would add a Node service, extra runtime dependencies, and another rendering path. The current Chrome screenshot plus FFmpeg path is simpler and already working. HyperFrames is worth adding later as an optional `renderer=hyperframes` mode if the goal becomes more continuous motion and CSS animation.

The Lecturify web UI was also not merged. The current project is command-line first; a UI can be added later without blocking video output quality.

## Expected Effect Difference After This Integration

- Slides should have cleaner visible content because prompts now ask for one teaching goal per page.
- Highlights should be more stable because missing or invalid element IDs are repaired.
- Highlight steps should feel less random because they can be generated from emphasized keywords.
- XiaoHongShu-style pages can show keyword chips based on the same keywords used for narration focus.
- Narration alignment should be easier because subtitle segments are always present after normalization.
- Videos should feel closer to a guided lesson, especially when `teaching_actions` are emitted by Dify or the direct LLM.

## Suggested Next Integration

The next high-value step is adding an optional keyword timing mode:

1. Use TTS word timestamps when the provider supports them.
2. Fall back to subtitle timing when word timestamps are unavailable.
3. Trigger highlight changes from keyword timing rather than only subtitle segment order.

That would bring this pipeline closer to Lecturify's strongest effect without replacing the whole renderer.
