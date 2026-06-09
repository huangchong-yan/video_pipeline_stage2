import argparse
import html
import json
import re
from pathlib import Path


def load_json(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    last_think = raw.rfind("</think>")
    if last_think != -1:
        raw = raw[last_think + len("</think>") :]
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found in input")
    return json.loads(raw[start : end + 1])


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def paragraphs(text: str):
    text = (text or "").strip()
    if not text:
        return []
    # Break long scripts into presenter-friendly chunks.
    chunks = re.split(r"(?<=[。！？])", text)
    paras = []
    current = ""
    for chunk in chunks:
        if not chunk.strip():
            continue
        if len(current) + len(chunk) > 95 and current:
            paras.append(current.strip())
            current = chunk
        else:
            current += chunk
    if current.strip():
        paras.append(current.strip())
    return paras


def render_notes(slide: dict) -> str:
    script = slide.get("tts_script") or slide.get("speaker_script") or ""
    paras = paragraphs(script)
    if not paras:
        return '<aside class="notes"><p>本页围绕当前标题展开讲解，并自然过渡到下一页。</p></aside>'
    body = "\n".join(f"      <p>{esc(p)}</p>" for p in paras)
    return f'<aside class="notes">\n{body}\n    </aside>'


def render_kicker(slide: dict, total: int) -> str:
    slide_id = int(slide.get("slide_id", 1))
    layout = esc(slide.get("layout", "slide"))
    return f'<p class="kicker">slide {slide_id:02d} / {total:02d} · {layout}</p>'


def render_footer(slide: dict, total: int) -> str:
    slide_id = int(slide.get("slide_id", 1))
    return (
        '<div class="deck-footer">'
        '<span class="mono">AI course video pipeline</span>'
        f'<span class="slide-number" data-current="{slide_id}" data-total="{total}"></span>'
        "</div>"
    )


def element_attr(element: dict) -> str:
    return f'data-element-id="{esc(element.get("element_id"))}"'


def body_elements(slide: dict):
    return [e for e in slide.get("elements", []) if e.get("type") != "title"]


def render_title_and_bullets(slide: dict) -> str:
    items = body_elements(slide)
    if not items:
        return ""
    hero = items[0]
    rows = [
        f'<div class="hero-statement focusable" {element_attr(hero)}>{esc(hero.get("text"))}</div>'
    ]
    bullets = []
    for item in items[1:]:
        bullets.append(
            f'<li class="focusable" {element_attr(item)}><span></span><p>{esc(item.get("text"))}</p></li>'
        )
    rows.append(f'<ul class="bullet-panel">{"".join(bullets)}</ul>')
    return "\n".join(rows)


def render_comparison(slide: dict) -> str:
    items = body_elements(slide)
    cards = []
    for item in items:
        label = item.get("element_id", "").split("_")[-2:]
        label = " ".join(part.upper() for part in label if part)
        cards.append(
            f"""
      <article class="compare-card focusable" {element_attr(item)}>
        <div class="card-label">{esc(label)}</div>
        <p>{esc(item.get("text"))}</p>
      </article>"""
        )
    return f'<div class="compare-grid cols-{min(max(len(items), 1), 4)}">{"".join(cards)}</div>'


def render_process(slide: dict) -> str:
    items = body_elements(slide)
    steps = []
    for idx, item in enumerate(items, start=1):
        steps.append(
            f"""
      <div class="process-step focusable" {element_attr(item)}>
        <span class="step-num">{idx}</span>
        <p>{esc(item.get("text"))}</p>
      </div>"""
        )
    return f'<div class="process-list">{"".join(steps)}</div>'


def render_quote(slide: dict) -> str:
    items = body_elements(slide)
    if not items:
        return ""
    quote = items[0]
    extras = []
    for item in items[1:]:
        extras.append(f'<div class="quote-example focusable" {element_attr(item)}>{esc(item.get("text"))}</div>')
    return (
        f'<blockquote class="main-quote focusable" {element_attr(quote)}>{esc(quote.get("text"))}</blockquote>'
        f'<div class="quote-examples">{"".join(extras)}</div>'
    )


def render_summary(slide: dict) -> str:
    return render_title_and_bullets(slide)


def render_slide(slide: dict, total: int) -> str:
    layout = slide.get("layout", "title_and_bullets")
    title = esc(slide.get("title"))
    if layout == "comparison":
        content = render_comparison(slide)
    elif layout == "process":
        content = render_process(slide)
    elif layout == "quote":
        content = render_quote(slide)
    elif layout == "summary":
        content = render_summary(slide)
    else:
        content = render_title_and_bullets(slide)

    highlight_json = esc(json.dumps(slide.get("highlight_steps", []), ensure_ascii=False))
    return f"""
  <section class="slide" data-title="{title}" data-highlight-steps="{highlight_json}">
    {render_kicker(slide, total)}
    <h1 class="slide-title focusable" data-element-id="s{int(slide.get("slide_id", 1))}_title">{title}</h1>
    <div class="slide-content layout-{esc(layout)}">
      {content}
    </div>
    {render_footer(slide, total)}
    {render_notes(slide)}
  </section>
"""


def build_index(data: dict, asset_prefix: str) -> str:
    slides = data.get("slides", [])
    total = len(slides)
    title = esc(data.get("course_title", "AI Course Deck"))
    sections = "\n".join(render_slide(slide, total) for slide in slides)
    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-themes="corporate-clean,nord,tokyo-night,dracula,catppuccin-mocha">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{asset_prefix}/assets/fonts.css">
<link rel="stylesheet" href="{asset_prefix}/assets/base.css">
<link rel="stylesheet" id="theme-link" href="{asset_prefix}/assets/themes/corporate-clean.css">
<link rel="stylesheet" href="{asset_prefix}/assets/animations/animations.css">
<link rel="stylesheet" href="style.css">
</head>
<body class="tpl-ai-course-video">
<div class="deck">
{sections}
</div>
<script src="{asset_prefix}/assets/runtime.js"></script>
<script src="highlight-preview.js"></script>
</body>
</html>
"""


STYLE_CSS = r"""/* AI course video deck generated from Dify JSON. */
.tpl-ai-course-video .slide {
  padding: 70px 96px;
}

.tpl-ai-course-video .kicker {
  font-family: var(--font-mono, monospace);
  font-size: 13px;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  color: var(--text-3);
  margin: 0 0 16px 0;
}

.tpl-ai-course-video .slide-title {
  font-size: clamp(38px, 4.4vw, 62px);
  line-height: 1.12;
  letter-spacing: 0;
  margin: 0 0 28px 0;
  max-width: 1120px;
}

.tpl-ai-course-video .slide-content {
  margin-top: 24px;
}

.tpl-ai-course-video .hero-statement {
  padding: 26px 30px;
  border: 1px solid var(--border);
  border-left: 7px solid var(--accent);
  border-radius: 12px;
  background: var(--surface);
  font-size: clamp(22px, 2.2vw, 34px);
  line-height: 1.48;
  color: var(--text-1);
}

.tpl-ai-course-video .bullet-panel {
  list-style: none;
  padding: 0;
  margin: 28px 0 0 0;
  display: grid;
  gap: 15px;
}

.tpl-ai-course-video .bullet-panel li {
  display: grid;
  grid-template-columns: 18px 1fr;
  gap: 18px;
  align-items: start;
  padding: 16px 20px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: color-mix(in srgb, var(--surface) 90%, transparent);
}

.tpl-ai-course-video .bullet-panel li span {
  width: 12px;
  height: 12px;
  border-radius: 999px;
  background: var(--accent);
  margin-top: 11px;
}

.tpl-ai-course-video .bullet-panel p,
.tpl-ai-course-video .process-step p,
.tpl-ai-course-video .compare-card p {
  margin: 0;
  font-size: clamp(19px, 1.7vw, 28px);
  line-height: 1.46;
}

.tpl-ai-course-video .compare-grid {
  display: grid;
  gap: 20px;
}

.tpl-ai-course-video .compare-grid.cols-2 { grid-template-columns: repeat(2, 1fr); }
.tpl-ai-course-video .compare-grid.cols-3 { grid-template-columns: repeat(3, 1fr); }
.tpl-ai-course-video .compare-grid.cols-4 { grid-template-columns: repeat(4, 1fr); }

.tpl-ai-course-video .compare-card {
  min-height: 360px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--surface);
  padding: 26px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.tpl-ai-course-video .card-label {
  font-family: var(--font-mono, monospace);
  color: var(--accent);
  font-weight: 700;
  font-size: 15px;
  margin-bottom: 18px;
}

.tpl-ai-course-video .process-list {
  display: grid;
  gap: 18px;
  max-width: 1100px;
}

.tpl-ai-course-video .process-step {
  display: grid;
  grid-template-columns: 54px 1fr;
  gap: 20px;
  align-items: center;
  padding: 18px 24px;
  border-radius: 14px;
  border: 1px solid var(--border);
  background: var(--surface);
}

.tpl-ai-course-video .step-num {
  width: 46px;
  height: 46px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: var(--accent);
  color: var(--bg);
  font-weight: 800;
  font-family: var(--font-mono, monospace);
}

.tpl-ai-course-video .main-quote {
  margin: 20px 0 26px 0;
  padding: 34px 42px;
  border-radius: 16px;
  border: 1px solid var(--border);
  border-left: 8px solid var(--accent);
  background: var(--surface);
  font-size: clamp(28px, 3vw, 46px);
  line-height: 1.38;
}

.tpl-ai-course-video .quote-examples {
  display: grid;
  gap: 16px;
}

.tpl-ai-course-video .quote-example {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px 22px;
  font-size: clamp(18px, 1.65vw, 26px);
  line-height: 1.45;
  background: var(--surface);
}

.tpl-ai-course-video .deck-footer {
  position: absolute;
  left: 96px;
  right: 96px;
  bottom: 34px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--text-3);
}

.tpl-ai-course-video .mono {
  font-family: var(--font-mono, monospace);
  font-size: 13px;
  padding: 3px 8px;
  border-radius: 7px;
  background: color-mix(in srgb, var(--surface) 70%, transparent);
}

.tpl-ai-course-video .focusable {
  position: relative;
}

.tpl-ai-course-video .focusable.is-highlighted::after {
  content: "";
  position: absolute;
  inset: -10px;
  border: 6px solid #f58420;
  border-radius: 16px;
  pointer-events: none;
  box-shadow: 0 0 0 4px rgba(245, 132, 32, 0.15);
}

.tpl-ai-course-video .notes {
  display: none;
}
"""


HIGHLIGHT_JS = r"""(() => {
  const getActiveSlide = () => document.querySelector('.slide.is-active') || document.querySelector('.slide');

  function clearHighlights(slide) {
    slide?.querySelectorAll('.is-highlighted').forEach((el) => el.classList.remove('is-highlighted'));
  }

  function highlightStep(slide, stepIndex) {
    clearHighlights(slide);
    if (!slide) return;
    let steps = [];
    try {
      steps = JSON.parse(slide.dataset.highlightSteps || '[]');
    } catch {
      steps = [];
    }
    if (!steps.length) return;
    const step = steps[stepIndex % steps.length];
    const target = slide.querySelector(`[data-element-id="${step.target_element_id}"]`);
    target?.classList.add('is-highlighted');
    slide.dataset.highlightIndex = String((stepIndex + 1) % steps.length);
  }

  window.addEventListener('keydown', (event) => {
    if (event.key.toLowerCase() !== 'h') return;
    const slide = getActiveSlide();
    const idx = Number(slide?.dataset.highlightIndex || '0');
    highlightStep(slide, idx);
  });

  window.addEventListener('hashchange', () => {
    clearHighlights(getActiveSlide());
  });
})();
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Dify JSON output")
    parser.add_argument("--out-dir", required=True, help="Output deck directory")
    parser.add_argument("--asset-prefix", default="../../html-ppt-skill")
    args = parser.parse_args()

    data = load_json(Path(args.input))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(build_index(data, args.asset_prefix), encoding="utf-8")
    (out_dir / "style.css").write_text(STYLE_CSS, encoding="utf-8")
    (out_dir / "highlight-preview.js").write_text(HIGHLIGHT_JS, encoding="utf-8")
    (out_dir / "deck-data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote HTML deck to {out_dir / 'index.html'}")


if __name__ == "__main__":
    main()
