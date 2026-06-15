import argparse
import json
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote


DEFAULT_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def file_url(path: Path, slide_no: int, highlight_step: int | None = None) -> str:
    resolved = path.resolve()
    url_path = resolved.as_posix()
    if not url_path.startswith("/"):
        url_path = "/" + url_path
    # Hash routing is handled by html-ppt runtime.js.
    query = f"?highlight={highlight_step}" if highlight_step else ""
    return f"file://{quote(url_path, safe='/:')}{query}#/{slide_no}"


def run_chrome(
    chrome: Path,
    url: str,
    output: Path,
    width: int,
    height: int,
    timeout: int,
    retries: int,
    skip_existing: bool = False,
):
    if skip_existing and output.exists() and output.stat().st_size > 0:
        return

    last_error: Exception | None = None
    for attempt in range(1, retries + 2):
        with TemporaryDirectory(prefix="html-deck-chrome-") as user_data_dir:
            cmd = [
                str(chrome),
                "--headless=new",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                "--allow-file-access-from-files",
                "--run-all-compositor-stages-before-draw",
                "--virtual-time-budget=2000",
                f"--user-data-dir={user_data_dir}",
                f"--window-size={width},{height}",
                f"--screenshot={output}",
                url,
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                if output.exists() and output.stat().st_size > 0:
                    print(f"Chrome screenshot was written before timeout; accepting {output.name}")
                    return
                last_error = exc
                if attempt <= retries:
                    print(f"Chrome screenshot timed out; retrying [{attempt}/{retries}] {url}")
                    time.sleep(1.0)
                    continue
                raise RuntimeError(f"Chrome screenshot timed out after {timeout}s for {url}") from exc
            if result.returncode == 0 and output.exists() and output.stat().st_size > 0:
                return
            last_error = RuntimeError(
                f"Chrome screenshot failed for {url}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
            if attempt <= retries:
                print(f"Chrome screenshot failed; retrying [{attempt}/{retries}] {url}")
                time.sleep(1.0)
                continue
    raise last_error or RuntimeError(f"Chrome screenshot failed for {url}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deck", required=True, help="Path to index.html")
    parser.add_argument("--data", required=True, help="Path to deck-data.json")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--chrome", default=DEFAULT_CHROME)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--include-highlights", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--skip-existing", action="store_true", help="Reuse existing non-empty screenshots and continue missing frames.")
    args = parser.parse_args()

    deck = Path(args.deck)
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    slides = data.get("slides", [])
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chrome = Path(args.chrome)
    if not chrome.exists():
        raise FileNotFoundError(f"Chrome not found: {chrome}")

    manifest = {
        "deck": str(deck.resolve()),
        "slide_size": {"width": args.width, "height": args.height},
        "slides": [],
    }

    for slide in slides:
        slide_no = int(slide.get("slide_id", len(manifest["slides"]) + 1))
        out_file = out_dir / f"html_slide_{slide_no:02d}.png"
        print(f"Capturing slide {slide_no:02d}/{len(slides):02d}: {out_file.name}")
        run_chrome(
            chrome,
            file_url(deck, slide_no),
            out_file,
            args.width,
            args.height,
            args.timeout,
            args.retries,
            args.skip_existing,
        )
        # Give Windows a moment to flush screenshot files before next Chrome process.
        time.sleep(0.15)
        manifest["slides"].append(
            {
                "slide_id": slide_no,
                "title": slide.get("title", ""),
                "file": out_file.name,
                "source_url": file_url(deck, slide_no),
                "highlight_files": [],
            }
        )
        if args.include_highlights:
            for step in slide.get("highlight_steps", []):
                step_no = int(step.get("step", 1))
                hi_file = out_dir / f"html_slide_{slide_no:02d}_highlight_{step_no:02d}.png"
                print(f"Capturing slide {slide_no:02d} highlight {step_no:02d}: {hi_file.name}")
                run_chrome(
                    chrome,
                    file_url(deck, slide_no, step_no),
                    hi_file,
                    args.width,
                    args.height,
                    args.timeout,
                    args.retries,
                    args.skip_existing,
                )
                time.sleep(0.15)
                manifest["slides"][-1]["highlight_files"].append(
                    {
                        "step": step_no,
                        "target_element_id": step.get("target_element_id", ""),
                        "file": hi_file.name,
                        "source_url": file_url(deck, slide_no, step_no),
                    }
                )

    manifest_path = out_dir / "capture_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Captured {len(slides)} slides to {out_dir}")
    print(f"Wrote manifest to {manifest_path}")


if __name__ == "__main__":
    main()
