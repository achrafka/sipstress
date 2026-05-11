"""Render SipStress HTML dashboard (Plotly) to PDF."""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("sipstress.html_to_pdf")

_CHROME_NAMES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
)


def _pdf_via_chromium(html_path: Path, pdf_path: Path) -> bool:
    """Headless Chromium --print-to-pdf. Plotly may be incomplete (no JS wait)."""
    html_path = html_path.resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    url = html_path.as_uri()
    for name in _CHROME_NAMES:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            subprocess.run(
                [
                    exe,
                    "--headless",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf_path.resolve()}",
                    url,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            log.info("PDF written via %s → %s", exe, pdf_path)
            return True
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as e:
            log.debug("PDF via %s failed: %s", exe, e)
            continue
    return False


def _pdf_via_playwright(html_path: Path, pdf_path: Path) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    html_path = html_path.resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    url = html_path.as_uri()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=120_000)
            page.wait_for_timeout(3500)
            page.pdf(
                path=str(pdf_path.resolve()),
                print_background=True,
                format="A4",
            )
            browser.close()
        log.info("PDF written via Playwright → %s", pdf_path)
        return True
    except Exception as exc:
        log.warning("Playwright PDF failed (%s); trying Chromium...", exc)
        return False


def write_pdf_from_html(
    html_path: Path | str,
    pdf_path: Path | str,
    *,
    prefer_playwright: bool = True,
) -> None:
    """Write PDF from an existing SipStress dashboard HTML file."""
    hp = Path(html_path)
    pp = Path(pdf_path)
    if not hp.is_file():
        raise FileNotFoundError(f"HTML file not found: {hp}")

    if prefer_playwright and _pdf_via_playwright(hp, pp):
        return
    if _pdf_via_chromium(hp, pp):
        return
    if not prefer_playwright and _pdf_via_playwright(hp, pp):
        return

    raise RuntimeError(
        "Could not produce PDF. Either install Chromium/Chrome "
        "(google-chrome-stable, chromium, …) or: pip install 'sipstress[pdf]' "
        "&& playwright install chromium"
    )


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(
        prog="sipstress-html2pdf",
        description=(
            "Convert SipStress HTML dashboard (Plotly) to PDF. "
            "Prefers Playwright if installed."
        ),
    )
    p.add_argument("html", type=Path, help="Input HTML report")
    p.add_argument("pdf", type=Path, help="Output PDF path")
    p.add_argument(
        "--chrome-only",
        action="store_true",
        help="Skip Playwright and use Chromium --print-to-pdf only (graphs may be empty).",
    )
    args = p.parse_args(argv)
    try:
        write_pdf_from_html(
            args.html,
            args.pdf,
            prefer_playwright=not args.chrome_only,
        )
    except (FileNotFoundError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
