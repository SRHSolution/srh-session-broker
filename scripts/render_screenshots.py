"""README 화면 캡처 — 예시(팬텀) 데이터로 터미널 대시보드(watch)·CLI·웹 대시보드를 그린다.

모든 세션·작업은 `srhbroker demo` 와 같은 가짜 데이터이고, herdr 창 상태도 가짜다. 실제 브로커 홈은 건드리지 않는다.

필요: pip install -e ".[screenshots]"  (playwright) + 설치된 Chrome (없으면 `playwright install chromium` 후 --bundled)
실행: python scripts/render_screenshots.py [--out docs/images] [--bundled]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from srhbroker import cli, onboard, watch  # noqa: E402
from srhbroker.config import load_config  # noqa: E402
from srhbroker.dashboard import make_handler  # noqa: E402
from srhbroker.dispatcher import SingleInstance, code_mtime  # noqa: E402
from srhbroker.monitor import snapshot  # noqa: E402
from srhbroker.service import Broker  # noqa: E402


class PhantomHerdr:
    """가짜 herdr: 예시 세션들이 herdr 창에 떠 있는 것처럼 보이게 한다."""

    def __init__(self):
        prov = {f"demo-{n}": (p, i) for i, (n, p, *_rest) in enumerate(onboard.DEMO_SESSIONS)}
        self.agents_ = [{"pane_id": f"w{prov[sid][1] + 1}:p1", "agent_status": st, "agent": prov[sid][0],
                         "cwd": "~/work/demo-shop", "agent_session": {"value": sid}}
                        for sid, st in onboard.DEMO_PANES.items()]

    def available(self):
        return True

    def binary(self):
        return "herdr"

    def agents(self):
        return self.agents_

    def find(self, sid):
        return next((a for a in self.agents_ if a["agent_session"]["value"] == sid), None)

    def at_menu(self, pane_id):
        return False


# ── ANSI → HTML 터미널 ────────────────────────────────────────────────────
COLORS = {"31": "#f2798c", "32": "#6fcf97", "33": "#e3b055", "34": "#7aa2f7", "35": "#c39ff5", "36": "#4cc7ba"}
SGR = re.compile(r"(\x1b\[[0-9;]*m)")


def ansi_html(text: str) -> str:
    out = []
    for line in text.splitlines():
        cur: set[str] = set()
        buf = []
        for part in SGR.split(line):
            if part.startswith("\x1b["):
                codes = [c for c in part[2:-1].split(";") if c]
                cur = set() if not codes or codes == ["0"] else cur | set(codes)
                continue
            if not part:
                continue
            body = "".join(f'<span class="w">{html.escape(ch)}</span>' if unicodedata.east_asian_width(ch) in "WF"
                           else html.escape(ch) for ch in part)
            style = ";".join(([f"color:{COLORS[c]}" for c in cur if c in COLORS]) + (["font-weight:700"] if "1" in cur else [])
                             + (["opacity:.62"] if "2" in cur else []))
            buf.append(f'<span style="{style}">{body}</span>' if style else body)
        out.append("".join(buf))
    return "\n".join(out)


def terminal_page(title: str, body_html: str, cols: int) -> str:
    return f"""<!doctype html><meta charset="utf-8"><style>
@import url("https://fonts.googleapis.com/css2?family=Nanum+Gothic+Coding:wght@400;700&display=block");
body {{ margin: 0; padding: 28px; background: #e9edf3; font-family: system-ui, sans-serif; }}
.win {{ display: inline-block; background: #11192a; border-radius: 12px; box-shadow: 0 18px 40px rgba(16,24,40,.28); overflow: hidden; }}
.bar {{ height: 34px; background: #1b2638; display: flex; align-items: center; gap: 8px; padding: 0 14px; color: #9aa7b8; font-size: 13px; }}
.dot {{ width: 12px; height: 12px; border-radius: 50%; }}
pre {{ margin: 0; padding: 16px 20px 20px; color: #dfe6ef; font: 16px/1.5 "Nanum Gothic Coding", "D2Coding", monospace;
      width: {cols}ch; white-space: pre; }}
.w {{ display: inline-block; width: 2ch; text-align: center; }}
</style><div class="win"><div class="bar"><span class="dot" style="background:#f2798c"></span><span class="dot" style="background:#e3b055"></span>
<span class="dot" style="background:#6fcf97"></span><span style="margin-left:10px">{html.escape(title)}</span></div><pre>{body_html}</pre></div>"""


def run_cli(args: list[str]) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(args)
    return buf.getvalue()


def sanitize(text: str, home: Path) -> str:
    text = text.replace(str(home), "~/.srhbroker")
    return re.sub(r"[A-Z]:\\Users\\[^\\\s]+", "~", text).replace("\\", "/")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "images"))
    ap.add_argument("--bundled", action="store_true", help="설치된 Chrome 대신 playwright 가 받은 Chromium 사용")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    home = Path(tempfile.mkdtemp(prefix="srhbroker-shots-"))
    os.environ["SRHBROKER_HOME"] = str(home)
    os.environ.pop("NO_COLOR", None)
    (home / "config.toml").write_text(onboard.DEMO_CONFIG, encoding="utf-8")
    b = Broker(load_config(home=home))
    asyncio.run(onboard.seed_demo(b))
    b.herdr = PhantomHerdr()
    # 데몬이 실행 중인 것처럼: 잠금을 쥐고 상태 파일을 쓴다 (가짜)
    lock = SingleInstance(home / "daemon.lock")
    lock.acquire()
    (home / "daemon.json").write_text(json.dumps({"pid": 4242, "started_at": time.time() - 3 * 3600, "heartbeat": time.time(),
                                                  "herdr": True, "herdr_pane": "w9:p1", "code_mtime": code_mtime()}),
                                      encoding="utf-8")

    pages = {}
    lines = watch.render(snapshot(b), cols=118, rows=34)
    pages["watch"] = terminal_page("srhbroker watch — 예시 데이터", ansi_html("\n".join(lines)), 118)
    doc = sanitize(run_cli(["doctor"]), home)
    tasks = sanitize(run_cli(["tasks", "-n", "8"]), home)
    term = (f"\x1b[36m$\x1b[0m srhbroker doctor\n{doc}\n\x1b[36m$\x1b[0m srhbroker tasks -n 8\n{tasks}")
    pages["cli"] = terminal_page("PowerShell — srhbroker", ansi_html(term), 150)

    port = free_port()
    token = "demo-token"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(b, token, port))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(**({} if a.bundled else {"channel": "chrome"}))
        for name, page_html in pages.items():
            pg = browser.new_page(device_scale_factor=2, viewport={"width": 1500, "height": 900})
            pg.set_content(page_html, wait_until="networkidle")
            pg.evaluate("document.fonts.ready")
            pg.wait_for_timeout(300)
            pg.locator(".win").screenshot(path=str(out / f"{name}.png"))
            pg.close()
        for scheme in ("dark", "light"):
            ctx = browser.new_context(color_scheme=scheme, viewport={"width": 1440, "height": 860}, device_scale_factor=2)
            pg = ctx.new_page()
            pg.goto(f"http://127.0.0.1:{port}/?t={token}")
            pg.wait_for_selector("li.row")
            pg.locator("li.row").filter(has=pg.locator(".sum", has_text=re.compile(r"^쿠폰 API 리뷰$"))).first.click()
            pg.wait_for_timeout(700)
            pg.evaluate("window.scrollTo(0, 0)")
            pg.wait_for_timeout(200)
            pg.screenshot(path=str(out / f"dashboard-{scheme}.png"))
            ctx.close()
        browser.close()
    httpd.shutdown()
    lock.release()
    b.store.close()
    for f in sorted(out.glob("*.png")):
        print(f"{f}  {f.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
