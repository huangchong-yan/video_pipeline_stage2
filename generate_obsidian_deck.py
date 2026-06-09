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
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found")
    return json.loads(raw[start : end + 1])


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def body_elements(slide):
    return [e for e in slide.get("elements", []) if e.get("type") != "title"]


def split_notes(text: str):
    text = (text or "").strip()
    if not text:
        return ["本页围绕当前核心观点展开讲解，然后自然过渡到下一页。"]
    parts = re.split(r"(?<=[。！？])", text)
    out = []
    current = ""
    for part in parts:
        if not part.strip():
            continue
        if current and len(current) + len(part) > 110:
            out.append(current.strip())
            current = part
        else:
            current += part
    if current.strip():
        out.append(current.strip())
    return out


def sentence_snippets(text: str, limit=2):
    chunks = [x.strip() for x in re.split(r"(?<=[。！？])", text or "") if x.strip()]
    return chunks[:limit]


def keyword_chips(slide):
    title = slide.get("title", "")
    script = (slide.get("speaker_script") or "") + " " + " ".join(e.get("text", "") for e in body_elements(slide))
    pool = [
        "DA", "BA", "DS", "SQL", "Python", "Excel", "A/B测试", "A/B testing",
        "产品感", "风险", "信用", "欺诈", "销售", "渠道", "促销", "库存",
        "建模", "分析", "行业", "业务问题", "项目经历", "XGBoost",
        "Power BI", "forecasting", "用户行为", "留存", "转化"
    ]
    found = []
    for key in pool:
        if key in script and key not in found:
            found.append(key)
    if not found:
        found = [part for part in re.split(r"[：、，, ]+", title) if part][:3]
    return found[:5]


def teaching_prompt(slide):
    sid = int(slide.get("slide_id", 1))
    prompts = {
        1: "你能用一句话说清：为什么同名数据岗位在不同行业会完全不同吗？",
        2: "如果一个岗位天天写SQL做看板，它更像 DA、BA 还是 DS？为什么？",
        3: "科技公司问 DAU 下降，你会先看用户、产品还是渠道？",
        4: "金融风控模型为什么不能只看 AUC，还要关注可解释性？",
        5: "如果促销销量上升，怎样判断是真的增量，而不是提前透支需求？",
        6: "你现在的技能树更像科技、金融，还是快消？缺哪一块？",
        7: "请把“我想做数据岗”改写成一个更具体的目标岗位。",
        8: "把你的一个项目用业务语言重写，不要只写模型和工具。"
    }
    return prompts.get(sid, "这一页的核心判断，你能用自己的话复述一遍吗？")


def insight_box(slide):
    steps = slide.get("highlight_steps", [])
    if steps:
        return steps[0].get("reason") or steps[0].get("cue_text") or "抓住这一页的核心判断。"
    return "抓住这一页的核心判断。"


def notes(slide):
    text = slide.get("tts_script") or slide.get("speaker_script") or ""
    return "<aside class=\"notes\">\n" + "\n".join(f"      <p>{esc(p)}</p>" for p in split_notes(text)) + "\n    </aside>"


def tag(slide, total):
    sid = int(slide.get("slide_id", 1))
    return (
        '<div class="oc-cbg"></div><div class="oc-cgrid"></div>'
        f'<div class="oc-snum">{sid:02d} / {total:02d}</div>'
    )


def element_attrs(e):
    return f'data-element-id="{esc(e.get("element_id"))}"'


def card_label(e, idx):
    raw = e.get("element_id", "").split("_")[-1]
    if raw.isdigit():
        raw = f"POINT {raw}"
    return raw.upper() if raw else f"POINT {idx}"


def render_cover(slide, total):
    items = body_elements(slide)
    pills = "".join(f'<span class="oc-pill focusable" data-element-id="{esc(e["element_id"])}">{esc(e["text"])}</span>' for e in items[1:4])
    hero = items[0]["text"] if items else "用数据解决行业核心业务问题"
    return f"""
  <section class="slide" data-title="{esc(slide.get("title"))}" data-highlight-steps="{esc(json.dumps(slide.get("highlight_steps", []), ensure_ascii=False))}">
    {tag(slide, total)}
    <div class="oc-tag">● AI COURSE VIDEO · DATA CAREERS</div>
    <h1 class="oc-h1">{esc(slide.get("title")).replace('，', '<br>')}</h1>
    <p class="oc-sub focusable" data-element-id="{esc(items[0]["element_id"] if items else "s1_core")}">{esc(hero)}</p>
    <div class="pill-row">{pills}</div>
    <div class="deepening-row">
      <div class="mini-panel"><b>本课目标</b><span>看懂岗位名背后的行业逻辑</span></div>
      <div class="mini-panel"><b>学习产出</b><span>写出自己的目标行业 + 目标岗位</span></div>
      <div class="mini-panel"><b>互动任务</b><span>{esc(teaching_prompt(slide))}</span></div>
    </div>
    {notes(slide)}
  </section>
"""


def render_comparison(slide, total):
    items = body_elements(slide)
    cards = []
    palette = ["oc-bp", "oc-bb", "oc-bg", "oc-bo"]
    for idx, e in enumerate(items, start=1):
        cards.append(f"""
      <div class="oc-card gradient-card focusable" {element_attrs(e)}>
        <span class="oc-badge {palette[(idx - 1) % len(palette)]}">{esc(card_label(e, idx))}</span>
        <p>{esc(e.get("text"))}</p>
      </div>""")
    cols = min(max(len(items), 2), 4)
    return f"""
  <section class="slide" data-title="{esc(slide.get("title"))}" data-highlight-steps="{esc(json.dumps(slide.get("highlight_steps", []), ensure_ascii=False))}">
    {tag(slide, total)}
    <div class="oc-tag">● COMPARE · ROLE MAP</div>
    <h2 class="oc-h2 focusable" data-element-id="s{int(slide.get("slide_id", 1))}_title">{esc(slide.get("title"))}</h2>
    <div class="oc-grid-{cols} obsidian-grid">{"".join(cards)}</div>
    <div class="deepening-row compact">
      <div class="mini-panel"><b>课堂判断</b><span>{esc(teaching_prompt(slide))}</span></div>
      <div class="mini-panel"><b>讲解抓手</b><span>{esc(insight_box(slide))}</span></div>
    </div>
    {notes(slide)}
  </section>
"""


def render_process(slide, total):
    items = body_elements(slide)
    steps = []
    for idx, e in enumerate(items, start=1):
        steps.append(f"""
      <div class="oc-step focusable" {element_attrs(e)}>
        <div class="oc-sn">{idx}</div>
        <div class="oc-sc"><h4>{esc(e.get("text"))}</h4><p>根据你的兴趣、技能和目标行业，逐步缩小选择范围。</p></div>
      </div>""")
    return f"""
  <section class="slide" data-title="{esc(slide.get("title"))}" data-highlight-steps="{esc(json.dumps(slide.get("highlight_steps", []), ensure_ascii=False))}">
    {tag(slide, total)}
    <div class="oc-tag">● ACTION · DECISION PATH</div>
    <h2 class="oc-h2 focusable" data-element-id="s{int(slide.get("slide_id", 1))}_title">{esc(slide.get("title"))}</h2>
    <div class="oc-steps obsidian-steps">{"".join(steps)}</div>
    <div class="action-strip">
      <span>课后动作</span>
      <b>选 1 个目标行业</b>
      <b>找 3 条 JD</b>
      <b>标出重复技能词</b>
    </div>
    {notes(slide)}
  </section>
"""


def render_quote(slide, total):
    items = body_elements(slide)
    quote = items[0] if items else {"element_id": "quote", "text": "用业务语言描述项目"}
    rest = items[1:]
    blocks = "".join(f'<div class="quote-chip focusable" {element_attrs(e)}>{esc(e.get("text"))}</div>' for e in rest)
    return f"""
  <section class="slide" data-title="{esc(slide.get("title"))}" data-highlight-steps="{esc(json.dumps(slide.get("highlight_steps", []), ensure_ascii=False))}">
    {tag(slide, total)}
    <div class="oc-tag">● INTERVIEW · STORYTELLING</div>
    <h2 class="oc-h2 focusable" data-element-id="s{int(slide.get("slide_id", 1))}_title">{esc(slide.get("title"))}</h2>
    <div class="oc-quote">
      <blockquote class="focusable" data-element-id="{esc(quote.get("element_id"))}">{esc(quote.get("text"))}</blockquote>
    </div>
    <div class="quote-grid">{blocks}</div>
    <div class="deepening-row compact">
      <div class="mini-panel"><b>面试表达</b><span>少讲工具，多讲业务价值和决策影响</span></div>
      <div class="mini-panel"><b>课堂提问</b><span>{esc(teaching_prompt(slide))}</span></div>
    </div>
    {notes(slide)}
  </section>
"""


def render_industry(slide, total):
    items = body_elements(slide)
    core = items[0] if items else None
    bullets = items[1:]
    signal_cards = "".join(
        f'<div class="signal-card focusable" {element_attrs(e)}><span>{idx:02d}</span><p>{esc(e.get("text"))}</p></div>'
        for idx, e in enumerate(bullets, start=1)
    )
    core_html = ""
    if core:
        core_html = f'<div class="oc-hl industry-core focusable" data-element-id="{esc(core.get("element_id"))}"><b>核心信号</b><br>{esc(core.get("text"))}</div>'
    keywords = keyword_chips(slide)
    chip_html = "".join(f'<span class="skill-chip">{esc(chip)}</span>' for chip in keywords)
    examples = sentence_snippets(slide.get("speaker_script", ""), 2)
    example_html = "".join(f'<li>{esc(item)}</li>' for item in examples)
    return f"""
  <section class="slide" data-title="{esc(slide.get("title"))}" data-highlight-steps="{esc(json.dumps(slide.get("highlight_steps", []), ensure_ascii=False))}">
    {tag(slide, total)}
    <div class="oc-tag">● INDUSTRY LENS</div>
    <h2 class="oc-h2 focusable" data-element-id="s{int(slide.get("slide_id", 1))}_title">{esc(slide.get("title"))}</h2>
    {core_html}
    <div class="signal-grid">{signal_cards}</div>
    <div class="content-extension">
      <div class="extension-card">
        <b>关键词</b>
        <div class="skill-row">{chip_html}</div>
      </div>
      <div class="extension-card">
        <b>讲解补充</b>
        <ul>{example_html}</ul>
      </div>
      <div class="extension-card">
        <b>课堂提问</b>
        <p>{esc(teaching_prompt(slide))}</p>
      </div>
    </div>
    {notes(slide)}
  </section>
"""


def render_slide(slide, total):
    sid = int(slide.get("slide_id", 1))
    layout = slide.get("layout", "")
    if sid == 1:
        return render_cover(slide, total)
    if layout == "comparison":
        return render_comparison(slide, total)
    if layout == "process":
        return render_process(slide, total)
    if layout == "quote":
        return render_quote(slide, total)
    return render_industry(slide, total)


CUSTOM_CSS = r"""
.tpl-obsidian-claude-gradient .slide {
  padding: 58px 76px;
}
.tpl-obsidian-claude-gradient .oc-h1 {
  font-size: clamp(62px, 7vw, 104px);
  max-width: 980px;
}
.tpl-obsidian-claude-gradient .oc-h2 {
  max-width: 1080px;
}
.tpl-obsidian-claude-gradient .pill-row {
  margin-top: 34px;
  max-width: 1000px;
}
.tpl-obsidian-claude-gradient .deepening-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  width: min(1060px, 94%);
  margin-top: 34px;
}
.tpl-obsidian-claude-gradient .deepening-row.compact {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin-top: 24px;
}
.tpl-obsidian-claude-gradient .mini-panel {
  text-align: left;
  border: 1px solid rgba(168,85,247,.24);
  background: rgba(124,58,237,.07);
  border-radius: 14px;
  padding: 16px 18px;
}
.tpl-obsidian-claude-gradient .mini-panel b {
  display: block;
  color: var(--oc-accent3);
  font-size: 13px;
  letter-spacing: .05em;
  margin-bottom: 8px;
}
.tpl-obsidian-claude-gradient .mini-panel span {
  display: block;
  color: var(--oc-text);
  font-size: 17px;
  line-height: 1.38;
}
.tpl-obsidian-claude-gradient .obsidian-grid {
  margin-top: 34px;
}
.tpl-obsidian-claude-gradient .gradient-card {
  min-height: 285px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  box-shadow: 0 22px 80px rgba(0,0,0,.22);
}
.tpl-obsidian-claude-gradient .gradient-card p {
  font-size: clamp(22px, 2vw, 34px);
  line-height: 1.4;
  color: var(--oc-text);
}
.tpl-obsidian-claude-gradient .industry-core {
  font-size: 22px;
  margin: 18px auto 28px;
  width: min(960px, 90%);
}
.tpl-obsidian-claude-gradient .signal-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  width: min(1060px, 94%);
}
.tpl-obsidian-claude-gradient .signal-card {
  background: rgba(22,27,34,.88);
  border: 1px solid var(--oc-border);
  border-radius: 16px;
  padding: 22px;
  text-align: left;
  min-height: 148px;
}
.tpl-obsidian-claude-gradient .signal-card span {
  display: inline-block;
  font-family: var(--font-mono, monospace);
  color: var(--oc-accent3);
  font-weight: 800;
  margin-bottom: 10px;
}
.tpl-obsidian-claude-gradient .signal-card p {
  color: var(--oc-text);
  font-size: 19px;
  line-height: 1.45;
  margin: 0;
}
.tpl-obsidian-claude-gradient .content-extension {
  display: grid;
  grid-template-columns: 1.1fr 1.4fr 1.1fr;
  gap: 14px;
  width: min(1080px, 94%);
  margin-top: 22px;
}
.tpl-obsidian-claude-gradient .extension-card {
  text-align: left;
  border: 1px solid var(--oc-border);
  background: rgba(22,27,34,.64);
  border-radius: 14px;
  padding: 16px 18px;
}
.tpl-obsidian-claude-gradient .extension-card b {
  display: block;
  color: var(--oc-accent3);
  margin-bottom: 10px;
}
.tpl-obsidian-claude-gradient .extension-card p,
.tpl-obsidian-claude-gradient .extension-card li {
  color: var(--oc-dim);
  font-size: 15px;
  line-height: 1.48;
}
.tpl-obsidian-claude-gradient .extension-card ul {
  margin: 0;
  padding-left: 18px;
}
.tpl-obsidian-claude-gradient .skill-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.tpl-obsidian-claude-gradient .skill-chip {
  display: inline-flex;
  border: 1px solid rgba(88,166,255,.28);
  background: rgba(88,166,255,.08);
  border-radius: 999px;
  padding: 5px 10px;
  color: var(--oc-blue);
  font-size: 13px;
}
.tpl-obsidian-claude-gradient .obsidian-steps {
  margin-top: 24px;
}
.tpl-obsidian-claude-gradient .obsidian-steps .oc-step {
  background: rgba(22,27,34,.7);
  border: 1px solid var(--oc-border);
  border-radius: 16px;
  padding: 18px 22px;
  margin-bottom: 12px;
}
.tpl-obsidian-claude-gradient .quote-grid {
  width: min(980px, 94%);
  display: grid;
  gap: 16px;
  margin-top: 28px;
}
.tpl-obsidian-claude-gradient .quote-chip {
  text-align: left;
  border: 1px solid rgba(168,85,247,.28);
  background: rgba(124,58,237,.08);
  border-radius: 14px;
  padding: 18px 22px;
  font-size: 21px;
  line-height: 1.45;
}
.tpl-obsidian-claude-gradient .action-strip {
  width: min(900px, 90%);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  margin-top: 24px;
  padding: 12px 16px;
  border: 1px solid rgba(52,211,153,.25);
  border-radius: 999px;
  background: rgba(52,211,153,.08);
}
.tpl-obsidian-claude-gradient .action-strip span {
  color: var(--oc-green);
  font-size: 13px;
  font-weight: 800;
}
.tpl-obsidian-claude-gradient .action-strip b {
  color: var(--oc-text);
  font-size: 15px;
}
.tpl-obsidian-claude-gradient .focusable {
  position: relative;
}
.tpl-obsidian-claude-gradient .focusable.is-highlighted::after {
  content: "";
  position: absolute;
  inset: -10px;
  border: 6px solid #f97316;
  border-radius: 18px;
  pointer-events: none;
  box-shadow: 0 0 0 5px rgba(249,115,22,.16), 0 0 38px rgba(249,115,22,.35);
}
.tpl-obsidian-claude-gradient .notes {
  display: none;
}
"""


HIGHLIGHT_JS = r"""
(() => {
  const active = () => document.querySelector('.slide.is-active') || document.querySelector('.slide');
  const clear = (slide) => slide?.querySelectorAll('.is-highlighted').forEach((el) => el.classList.remove('is-highlighted'));
  const step = (slide, forcedIndex = null) => {
    if (!slide) return;
    clear(slide);
    let steps = [];
    try { steps = JSON.parse(slide.dataset.highlightSteps || '[]'); } catch {}
    if (!steps.length) return;
    const idx = forcedIndex === null ? Number(slide.dataset.highlightIndex || '0') % steps.length : forcedIndex % steps.length;
    const target = slide.querySelector(`[data-element-id="${steps[idx].target_element_id}"]`);
    target?.classList.add('is-highlighted');
    slide.dataset.highlightIndex = String(idx + 1);
  };
  const applyUrlHighlight = () => {
    const params = new URLSearchParams(window.location.search);
    const raw = params.get('highlight');
    if (!raw) return;
    const idx = Math.max(0, Number(raw) - 1);
    setTimeout(() => step(active(), idx), 150);
  };
  window.addEventListener('keydown', (event) => {
    if (event.key.toLowerCase() === 'h') step(active());
  });
  window.addEventListener('hashchange', () => {
    clear(active());
    applyUrlHighlight();
  });
  window.addEventListener('load', applyUrlHighlight);
})();
"""


def build(data, asset_prefix):
    slides = data.get("slides", [])
    total = len(slides)
    content = "\n".join(render_slide(slide, total) for slide in slides)
    title = esc(data.get("course_title", "AI Course Deck"))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Obsidian Claude Gradient</title>
<link rel="stylesheet" href="{asset_prefix}/assets/fonts.css">
<link rel="stylesheet" href="{asset_prefix}/assets/base.css">
<link rel="stylesheet" href="{asset_prefix}/templates/full-decks/obsidian-claude-gradient/style.css">
<link rel="stylesheet" href="style.css">
</head>
<body class="tpl-obsidian-claude-gradient">
<div class="deck">
{content}
</div>
<script src="{asset_prefix}/assets/runtime.js"></script>
<script src="highlight-preview.js"></script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--asset-prefix", default="../../html-ppt-skill")
    args = parser.parse_args()
    data = load_json(Path(args.input))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(build(data, args.asset_prefix), encoding="utf-8")
    (out_dir / "style.css").write_text(CUSTOM_CSS, encoding="utf-8")
    (out_dir / "highlight-preview.js").write_text(HIGHLIGHT_JS, encoding="utf-8")
    (out_dir / "deck-data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote Obsidian deck to {out_dir / 'index.html'}")


if __name__ == "__main__":
    main()
