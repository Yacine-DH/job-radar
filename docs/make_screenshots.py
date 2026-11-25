#!/usr/bin/env python3
"""Regenerate docs/screenshots/*.png from the demo (made-up data only).

    pip install playwright && playwright install chromium     # once, not part of requirements.txt
    .venv/bin/python demo.py &                                 # in another terminal, or let this script start it
    python docs/make_screenshots.py [--url http://127.0.0.1:8091/]

Starts demo.py itself when nothing is listening on the URL. Never touches a real database: demo.py uses a temp dir.
"""
import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"


def listening(url: str) -> bool:
    u = urlsplit(url)
    try:
        socket.create_connection((u.hostname, u.port or 80), timeout=1).close()
        return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8091/")
    args = ap.parse_args()
    server = None
    if not listening(args.url):
        py = ROOT / ".venv" / "bin" / "python"
        server = subprocess.Popen([str(py if py.exists() else sys.executable), str(ROOT / "demo.py")], stdout=subprocess.DEVNULL)
        for _ in range(60):
            if listening(args.url):
                break
            time.sleep(0.5)
        else:
            server.kill()
            sys.exit("the demo did not start")
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()

            def page(width, height, scheme, scale=1):
                ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme, device_scale_factor=scale)
                p = ctx.new_page()
                p.goto(args.url)
                p.wait_for_selector(".card")
                return p

            # 1. desktop, light: the triage list
            p = page(1280, 860, "light")
            p.screenshot(path=OUT / "dashboard-desktop.png")
            # 2. the fit analysis: skill gaps first, matches, learning steps
            card = p.locator('.card[data-id="demo:1"]')
            card.locator("details.fit > summary").click()
            card.scroll_into_view_if_needed()
            card.screenshot(path=OUT / "fit-analysis.png")
            # 3. resume tools: regenerate panel and history with a pinned label
            card.locator("details.fit > summary").click()  # close again
            card.locator("details.hist summary").click()
            card.locator("details.regen summary").click()
            card.screenshot(path=OUT / "resume-history.png")
            # 4. phone, dark
            m = page(390, 844, "dark", scale=2)
            m.screenshot(path=OUT / "dashboard-phone-dark.png")
            browser.close()
    finally:
        if server:
            server.terminate()
    for f in sorted(OUT.glob("*.png")):
        print(f"{f.name}: {f.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
