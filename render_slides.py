import argparse
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SLIDE_W = 1920
SLIDE_H = 1080
MARGIN_X = 120
TITLE_Y = 74
CONTENT_TOP = 190
ORANGE = (245, 132, 32)
INK = (35, 39, 47)
MUTED = (96, 105, 118)
BLUE = (31, 92, 188)
LIGHT_BLUE = (232, 240, 255)
PANEL = (248, 250, 252)
BORDER = (218, 225, 235)


def font_path() -> str:
    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("No Chinese font found in C:\\Windows\\Fonts")


FONT_PATH = font_path()


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    # Microsoft YaHei TTC renders Chinese cleanly. Bold is simulated by drawing twice.
    return ImageFont.truetype(FONT_PATH, size=size)


def load_dify_json(path: Path) -> dict:
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


def text_size(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont):
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0], box[3] - box[1]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_width: int):
    lines = []
    current = ""
    for char in text:
        trial = current + char
        if text_size(draw, trial, fnt)[0] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def draw_wrapped(
    draw: ImageDraw.ImageDraw,
    xy,
    text: str,
    fnt: ImageFont.FreeTypeFont,
    fill=INK,
    max_width=1000,
    line_gap=12,
):
    x, y = xy
    lines = wrap_text(draw, text, fnt, max_width)
    line_h = text_size(draw, "高", fnt)[1] + line_gap
    for line in lines:
        draw.text((x, y), line, font=fnt, fill=fill)
        y += line_h
    width = max((text_size(draw, line, fnt)[0] for line in lines), default=0)
    return (x, xy[1], x + min(width, max_width), y - line_gap)


def rounded_rect(draw, box, radius=22, fill=PANEL, outline=BORDER, width=2):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def draw_title(draw, slide, slide_no: int):
    title_font = font(56)
    subtitle_font = font(25)
    draw.text((MARGIN_X, TITLE_Y), slide["title"], font=title_font, fill=INK)
    draw.text((SLIDE_W - 230, 92), f"{slide_no:02d}", font=subtitle_font, fill=MUTED)
    draw.line((MARGIN_X, 154, SLIDE_W - MARGIN_X, 154), fill=BORDER, width=2)


def body_elements(slide):
    return [e for e in slide.get("elements", []) if e.get("type") != "title"]


def render_cards(draw, slide, layout_boxes):
    items = body_elements(slide)
    if not items:
        return
    card_gap = 30
    card_w = (SLIDE_W - 2 * MARGIN_X - card_gap * (len(items) - 1)) // max(len(items), 1)
    card_h = 540
    y = CONTENT_TOP + 105
    for idx, item in enumerate(items):
        x = MARGIN_X + idx * (card_w + card_gap)
        box = (x, y, x + card_w, y + card_h)
        rounded_rect(draw, box, radius=18, fill=(250, 252, 255))
        label = item.get("text", "")
        title = item.get("element_id", "").split("_")[-1].upper()
        draw.text((x + 30, y + 28), title, font=font(24), fill=BLUE)
        text_box = draw_wrapped(draw, (x + 30, y + 88), label, font(32), max_width=card_w - 60)
        layout_boxes[item["element_id"]] = box


def render_bullets(draw, slide, layout_boxes):
    items = body_elements(slide)
    if not items:
        return
    first = items[0]
    hero_box = (MARGIN_X, CONTENT_TOP, SLIDE_W - MARGIN_X, CONTENT_TOP + 190)
    rounded_rect(draw, hero_box, radius=24, fill=LIGHT_BLUE, outline=(196, 214, 250))
    layout_boxes[first["element_id"]] = hero_box
    draw_wrapped(draw, (MARGIN_X + 42, CONTENT_TOP + 44), first["text"], font(38), max_width=SLIDE_W - 2 * MARGIN_X - 84)

    y = CONTENT_TOP + 240
    for item in items[1:]:
        box = (MARGIN_X + 30, y, SLIDE_W - MARGIN_X - 30, y + 112)
        rounded_rect(draw, box, radius=18, fill=PANEL)
        draw.ellipse((box[0] + 24, y + 39, box[0] + 42, y + 57), fill=ORANGE)
        draw_wrapped(draw, (box[0] + 70, y + 30), item["text"], font(31), max_width=box[2] - box[0] - 110)
        layout_boxes[item["element_id"]] = box
        y += 132


def render_process(draw, slide, layout_boxes):
    items = body_elements(slide)
    y = CONTENT_TOP + 70
    for idx, item in enumerate(items):
        box = (MARGIN_X + 120, y, SLIDE_W - MARGIN_X - 120, y + 125)
        rounded_rect(draw, box, radius=22, fill=(255, 252, 247), outline=(244, 211, 176))
        circle = (box[0] - 72, y + 29, box[0] - 14, y + 87)
        draw.ellipse(circle, fill=ORANGE)
        draw.text((circle[0] + 19, circle[1] + 9), str(idx + 1), font=font(28), fill=(255, 255, 255))
        draw_wrapped(draw, (box[0] + 34, y + 34), item["text"], font(32), max_width=box[2] - box[0] - 68)
        layout_boxes[item["element_id"]] = box
        y += 152


def render_quote(draw, slide, layout_boxes):
    items = body_elements(slide)
    if not items:
        return
    quote = items[0]
    box = (MARGIN_X + 90, CONTENT_TOP + 70, SLIDE_W - MARGIN_X - 90, CONTENT_TOP + 300)
    rounded_rect(draw, box, radius=28, fill=(255, 249, 241), outline=(242, 203, 156), width=3)
    draw.text((box[0] + 40, box[1] + 32), "“", font=font(80), fill=ORANGE)
    draw_wrapped(draw, (box[0] + 100, box[1] + 76), quote["text"], font(42), max_width=box[2] - box[0] - 170)
    layout_boxes[quote["element_id"]] = box

    y = CONTENT_TOP + 350
    for item in items[1:]:
        card = (MARGIN_X + 120, y, SLIDE_W - MARGIN_X - 120, y + 130)
        rounded_rect(draw, card, radius=18, fill=PANEL)
        draw_wrapped(draw, (card[0] + 34, y + 32), item["text"], font(30), max_width=card[2] - card[0] - 68)
        layout_boxes[item["element_id"]] = card
        y += 152


def render_slide(slide: dict, slide_no: int, highlight_target: str | None = None):
    image = Image.new("RGB", (SLIDE_W, SLIDE_H), (255, 255, 255))
    draw = ImageDraw.Draw(image)

    # subtle top-left accent
    draw.rectangle((0, 0, 30, SLIDE_H), fill=BLUE)
    draw.rectangle((30, 0, 48, SLIDE_H), fill=ORANGE)
    draw_title(draw, slide, slide_no)

    layout_boxes = {}
    for element in slide.get("elements", []):
        if element.get("type") == "title":
            layout_boxes[element["element_id"]] = (MARGIN_X, TITLE_Y, SLIDE_W - MARGIN_X, 150)

    layout = slide.get("layout", "title_and_bullets")
    if layout == "comparison":
        render_cards(draw, slide, layout_boxes)
    elif layout == "process":
        render_process(draw, slide, layout_boxes)
    elif layout == "quote":
        render_quote(draw, slide, layout_boxes)
    else:
        render_bullets(draw, slide, layout_boxes)

    if highlight_target and highlight_target in layout_boxes:
        x1, y1, x2, y2 = layout_boxes[highlight_target]
        pad = 12
        draw.rounded_rectangle(
            (x1 - pad, y1 - pad, x2 + pad, y2 + pad),
            radius=24,
            outline=ORANGE,
            width=9,
        )

    footer_font = font(22)
    draw.text((MARGIN_X, SLIDE_H - 58), "AI course video pipeline draft", font=footer_font, fill=(140, 148, 160))
    return image, layout_boxes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Dify output text or clean JSON file")
    parser.add_argument("--out-slides", default="outputs/slides")
    parser.add_argument("--out-metadata", default="outputs/metadata/layout.json")
    parser.add_argument("--out-json", default="outputs/metadata/production_json.json")
    args = parser.parse_args()

    data = load_dify_json(Path(args.input))
    out_slides = Path(args.out_slides)
    out_slides.mkdir(parents=True, exist_ok=True)
    out_metadata = Path(args.out_metadata)
    out_metadata.parent.mkdir(parents=True, exist_ok=True)
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    layout_metadata = {
        "course_title": data.get("course_title", ""),
        "slide_size": {"width": SLIDE_W, "height": SLIDE_H},
        "slides": [],
    }

    for slide in data.get("slides", []):
        slide_no = int(slide.get("slide_id", len(layout_metadata["slides"]) + 1))
        base_image, boxes = render_slide(slide, slide_no)
        base_name = f"slide_{slide_no:02d}.png"
        base_image.save(out_slides / base_name)

        highlight_files = []
        for step in slide.get("highlight_steps", []):
            target = step.get("target_element_id")
            step_no = int(step.get("step", len(highlight_files) + 1))
            hi_image, _ = render_slide(slide, slide_no, target)
            hi_name = f"slide_{slide_no:02d}_highlight_{step_no:02d}.png"
            hi_image.save(out_slides / hi_name)
            highlight_files.append({"step": step_no, "target_element_id": target, "file": hi_name})

        layout_metadata["slides"].append(
            {
                "slide_id": slide_no,
                "base_file": base_name,
                "element_boxes": {k: list(v) for k, v in boxes.items()},
                "highlight_files": highlight_files,
            }
        )

    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    out_metadata.write_text(json.dumps(layout_metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rendered {len(data.get('slides', []))} slides to {out_slides}")
    print(f"Wrote clean JSON to {out_json}")
    print(f"Wrote layout metadata to {out_metadata}")


if __name__ == "__main__":
    main()
