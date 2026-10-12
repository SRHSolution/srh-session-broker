"""README 사용법 GIF — 예시(팬텀) 데이터로 웹 대시보드를 실제로 조작하며 프레임을 찍어 GIF 로 묶는다.

모든 세션·작업은 `srhbroker demo` 와 같은 가짜 데이터이고, herdr 창과 데몬 상태도 가짜다. 실제 브로커 홈은 건드리지 않는다.
화면 위에 가짜 마우스 커서·클릭 표시를 덧씌우고, 화면 아래에 영문 단계 자막 띠를 붙인다(대시보드 화면 자체는 그대로).
화면·예시 데이터 언어는 --lang (기본 en — 영문 README 용). ko 로 만들면 파일 이름 끝에 -ko 가 붙는다.

필요: pip install -e ".[screenshots]"  (playwright·Pillow) + 설치된 Chrome (없으면 `playwright install chromium` 후 --bundled)
실행: python scripts/render_gifs.py [--lang en|ko] [--out docs/images] [--only tour,route,approve,watch] [--bundled]
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from render_screenshots import PhantomHerdr, ansi_html, free_port, terminal_page  # noqa: E402
from srhbroker import i18n, onboard, watch  # noqa: E402
from srhbroker.config import load_config  # noqa: E402
from srhbroker.dashboard import make_handler  # noqa: E402
from srhbroker.dispatcher import SingleInstance, code_mtime  # noqa: E402
from srhbroker.monitor import snapshot  # noqa: E402
from srhbroker.service import Broker  # noqa: E402

VIEWPORT = {"width": 1440, "height": 860}
LANG = "en"   # main() 이 --lang 으로 바꾼다
# 장면이 고르는 예시 작업 제목·검색어·자막 속 화면 이름 (onboard.DEMO_TEXT 와 같은 내용)
TEXT = {
    "en": {"review": "Coupon API review", "coupon": "Cart coupon API", "search": "coupon", "mail": "Email wording",
           "needs_you": "Needs you", "held": "Awaiting approval", "routed": "Routed to {to}"},
    "ko": {"review": "쿠폰 API 리뷰", "coupon": "쿠폰 적용 API 구현", "search": "쿠폰", "mail": "메일 문구",
           "needs_you": "사람이 처리할 것", "held": "승인 대기", "routed": "대상 지정: {to}"},
}


def tx(key: str) -> str:
    return TEXT[LANG][key]
TOKEN = "demo-token"
CAPTION_H = 60
# 자막 글꼴 (Windows · macOS · Linux 순). 영문은 라틴 글꼴, 따옴표로 함께 적는 한글 UI 이름은 한글 글꼴로 쓴다
LATIN_FONTS = [r"C:\Windows\Fonts\seguisb.ttf", "/System/Library/Fonts/Helvetica.ttc",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
HANGUL_FONTS = [r"C:\Windows\Fonts\malgunbd.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc",
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"]


def _font(size: int, hangul: bool = False):
    for f in (HANGUL_FONTS if hangul else LATIN_FONTS + HANGUL_FONTS):
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


def _is_hangul(ch: str) -> bool:
    o = ord(ch)
    return 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F   # 한글 음절·자모


def _text(d: ImageDraw.ImageDraw, x: float, cy: float, text: str, size: int, fill) -> None:
    """영문·한글이 섞인 한 줄을 글꼴을 바꿔 가며 같은 기준선에 그린다."""
    runs: list[tuple[bool, str]] = []
    for ch in text:
        h = _is_hangul(ch) or (ch == " " and runs and runs[-1][0])
        if runs and runs[-1][0] == h:
            runs[-1] = (h, runs[-1][1] + ch)
        else:
            runs.append((h, ch))
    base = cy + size * 0.36
    for h, part in runs:
        f = _font(size, h)
        d.text((x, base), part, font=f, fill=fill, anchor="ls")
        x += d.textlength(part, font=f)


def caption_strip(width: int, n: str | None, text: str) -> Image.Image:
    """화면 아래에 붙이는 자막 띠 — 화면을 가리지 않게 스크린샷 밖에 그린다."""
    im = Image.new("RGB", (width, CAPTION_H), (11, 16, 30))
    d = ImageDraw.Draw(im)
    d.line([(0, 0), (width, 0)], fill=(46, 60, 85), width=1)
    x, cy = 24, CAPTION_H // 2
    if n:
        d.ellipse([x, cy - 15, x + 30, cy + 15], fill=(255, 229, 138))
        d.text((x + 15, cy), n, font=_font(17), fill=(28, 36, 48), anchor="mm")
        x += 44
    _text(d, x, cy, text, 20, (242, 245, 249))
    return im


class GifHerdr(PhantomHerdr):
    """가짜 herdr — 대상 지정·승인 뒤 받는 창에 넣는 것까지 성공한 것처럼 보이게 한다."""

    def pane(self, pane_id):
        return next((a for a in self.agents_ if a["pane_id"] == pane_id), None)

    def prompt(self, pane_id, text):
        return True

    def wait_ready(self, pane_id, timeout_s):
        return True


# 4초 자동 새로고침을 멈추고(장면 중간에 화면이 바뀌지 않게) 필요할 때 window.__tick() 으로 부른다
INIT_JS = """
(() => {
  const si = window.setInterval;
  window.setInterval = function (fn, ms, ...rest) {
    if (ms === 4000) { window.__tick = fn; return 0; }
    return si.call(window, fn, ms, ...rest);
  };
})();
"""

OVERLAY_CSS = """
html { scrollbar-width: none; }
#gif-cursor { position: fixed; left: 0; top: 0; z-index: 2147483001; width: 26px; height: 26px; pointer-events: none;
  filter: drop-shadow(0 2px 3px rgba(0,0,0,.55)); }
#gif-ripple { position: fixed; left: 0; top: 0; z-index: 2147483000; width: 46px; height: 46px; margin: -23px 0 0 -23px;
  border-radius: 50%; border: 3px solid #FFE58A; background: rgba(255, 229, 138, .22); pointer-events: none; opacity: 0; }
"""

OVERLAY_JS = """
(css) => {
  const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
  const rip = document.createElement("div"); rip.id = "gif-ripple"; document.body.appendChild(rip);
  const cur = document.createElement("div"); cur.id = "gif-cursor";
  cur.innerHTML = '<svg viewBox="0 0 26 26" width="26" height="26"><path d="M3 2 L3 21 L8.2 16.4 L11.6 24 L15 22.5 L11.7 15 L19 15 Z" '
    + 'fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  document.body.appendChild(cur);
  window.__cur = (x, y) => { cur.style.transform = `translate(${x - 3}px, ${y - 2}px)`; };
  window.__rip = (x, y, s, o) => { rip.style.left = x + "px"; rip.style.top = y + "px"; rip.style.transform = `scale(${s})`; rip.style.opacity = o; };
}
"""


class Recorder:
    """스크린샷 한 장 = 프레임 하나. 같은 화면이 이어지면 Pillow 가 저장할 때 길이를 합친다."""

    def __init__(self, page, start=(640, 420), el=None):
        """el: 이 요소만 찍는다 (터미널 창). 없으면 화면 전체에 가짜 커서를 쓴다."""
        self.page = page
        self.el = el
        self.frames: list[tuple[Image.Image, int]] = []
        self.pos = start
        self.cap: tuple[str | None, str] = (None, "")
        if el is None:
            page.evaluate("([x, y]) => window.__cur(x, y)", list(start))
            page.mouse.move(*start)

    def shot(self, ms: int) -> None:
        png = self.el.screenshot() if self.el is not None else self.page.screenshot()
        img = Image.open(io.BytesIO(png)).convert("RGB")
        out = Image.new("RGB", (img.width, img.height + CAPTION_H))
        out.paste(img, (0, 0))
        out.paste(caption_strip(img.width, *self.cap), (0, img.height))
        self.frames.append((out, ms))

    def caption(self, n: str | None, text: str) -> None:
        self.cap = (n, text)

    def scroll(self, dy: int, steps: int = 8, ms: int = 40) -> None:
        """페이지를 부드럽게 내린다 (세션·상세 패널은 위에 붙어 있고 상세의 아래쪽이 드러난다)."""
        y0 = self.page.evaluate("document.scrollingElement.scrollTop")
        dy = min(dy, self.page.evaluate("document.scrollingElement.scrollHeight - innerHeight") - y0)
        for i in range(1, steps + 1):
            e = 1 - (1 - i / steps) ** 3
            self.page.evaluate("y => { document.scrollingElement.scrollTop = y; }", y0 + dy * e)
            self.shot(ms)

    def _center(self, target) -> tuple[float, float]:
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        return box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2

    def move(self, target, steps: int = 9, ms: int = 40) -> tuple[float, float]:
        x1, y1 = target if isinstance(target, tuple) else self._center(target)
        x0, y0 = self.pos
        for i in range(1, steps + 1):
            t = i / steps
            e = 1 - (1 - t) ** 3   # 끝에서 느려지는 이동
            x, y = x0 + (x1 - x0) * e, y0 + (y1 - y0) * e
            self.page.evaluate("([x, y]) => window.__cur(x, y)", [x, y])
            self.page.mouse.move(x, y)
            self.shot(ms)
        self.pos = (x1, y1)
        return x1, y1

    def click(self, target, settle_ms: int = 450, nudge: bool = False) -> None:
        """누른다. nudge: 누른 뒤 커서를 살짝 비켜 바뀐 버튼 글자가 보이게 한다."""
        x, y = self.move(target)
        self.page.evaluate("([x, y]) => window.__rip(x, y, .45, 1)", [x, y])
        self.shot(70)
        self.page.mouse.click(x, y)
        self.page.wait_for_timeout(settle_ms)
        for s, o in ((.8, .8), (1.1, .45)):
            self.page.evaluate("([x, y, s, o]) => window.__rip(x, y, s, o)", [x, y, s, o])
            self.shot(60)
        self.page.evaluate("([x, y]) => window.__rip(x, y, 1, 0)", [x, y])
        if nudge:
            self.move((x + 26, y + 30), steps=4)

    def type(self, text: str, ms: int = 110) -> None:
        for ch in text:
            self.page.keyboard.type(ch)
            self.shot(ms)

    def key(self, key: str, settle_ms: int = 350) -> None:
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(settle_ms)

    def hold(self, ms: int) -> None:
        self.shot(ms)


def save_gif(frames: list[tuple[Image.Image, int]], path: Path) -> dict:
    """한 팔레트(255색, 디더링 없음)로 묶는다 — 프레임마다 색이 흔들리지 않고, 바뀐 부분만 저장돼 작다."""
    imgs = [f for f, _ in frames]
    step = max(1, len(imgs) // 14)
    sample = imgs[::step][:14]
    w, h = sample[0].size
    mont = Image.new("RGB", (w, h * len(sample)))
    for i, im in enumerate(sample):
        mont.paste(im, (0, h * i))
    pal = mont.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    q = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im in imgs]
    durs = [max(20, int(round(d / 10.0)) * 10) for _, d in frames]
    q[0].save(path, save_all=True, append_images=q[1:], duration=durs, loop=0, optimize=False, disposal=1)
    with Image.open(path) as g:
        n = g.n_frames
    return {"path": path, "kb": path.stat().st_size // 1024, "seconds": round(sum(durs) / 1000, 1), "frames": n}


def open_page(browser, port: int):
    ctx = browser.new_context(color_scheme="dark", viewport=VIEWPORT, device_scale_factor=1,
                              locale="en-US" if LANG == "en" else "ko-KR")
    ctx.add_init_script(INIT_JS)
    pg = ctx.new_page()
    pg.goto(f"http://127.0.0.1:{port}/?t={TOKEN}")
    pg.wait_for_selector("li.row")
    pg.evaluate("document.fonts.ready")
    pg.evaluate(OVERLAY_JS, OVERLAY_CSS)
    pg.wait_for_timeout(300)
    return ctx, pg


def row(pg, title: str):
    return pg.locator("li.row").filter(has=pg.locator(".sum", has_text=re.compile(f"^{re.escape(title)}$"))).first


# ── 장면 ──────────────────────────────────────────────────────────────────

def scene_tour(pg) -> list:
    r = Recorder(pg, start=(1180, 560))
    r.caption(None, "Every Claude ↔ Codex hand-off on one local page")
    r.hold(2400)

    r.caption("1", "Click a session to see only its traffic")
    r.hold(500)
    r.click(pg.locator('.node[data-session="api-codex"]'))
    r.hold(1900)

    r.caption("2", "Open a flow to see every relay step, from sent to reply")
    r.hold(400)
    r.click(row(pg, tx("review")))
    pg.locator("#detail .tl").wait_for()
    r.move((1365, 470), steps=8)
    r.hold(3000)

    r.caption("3", "Press / to search senders, titles, bodies and results")
    r.hold(400)
    r.key("/", settle_ms=150)
    r.hold(500)
    r.type(tx("search"), ms=160)
    pg.wait_for_timeout(700)    # 입력 뒤 0.3초 묶었다가 서버 검색
    r.hold(1800)

    r.caption("4", "Each task keeps its acceptance criteria and the full result")
    r.click(row(pg, tx("coupon")))
    pg.locator("#detail .tl").wait_for()
    r.move((1380, 600), steps=8)
    r.hold(900)
    r.scroll(300)
    r.hold(3000)
    return r.frames


def scene_route(pg) -> list:
    r = Recorder(pg, start=(760, 300))
    item = pg.locator("#todo li.route")
    r.caption(None, f"No target? The task waits under “{tx('needs_you')}” until you route it")
    r.move(item.locator(".t"), steps=10)
    r.hold(2300)

    r.caption("1", "Type a session or role to send it to")
    r.hold(300)
    r.click(item.locator(".route-in"), settle_ms=150, nudge=True)
    r.type("docs-claude", ms=105)
    r.hold(1300)

    r.caption("2", "Press Enter — the button asks you to confirm")
    r.key("Enter", settle_ms=200)
    r.hold(1700)

    r.caption("3", "Click once more: it goes straight into that session's pane")
    r.click(item.locator('[data-act="route"]'), settle_ms=600)
    r.hold(2300)

    r.caption("4", "The flow now shows who received it, and its timeline records your routing")
    r.click(row(pg, tx("mail")))
    pg.locator("#detail .tl").wait_for()
    r.move((1340, 560), steps=8)
    r.hold(3200)
    return r.frames


def scene_approve(pg) -> list:
    r = Recorder(pg, start=(760, 330))
    item = pg.locator("#todo li.approve")
    r.caption(None, f"Risky work waits for you — “{tx('held')}” says why it was held")
    r.move(item.locator(".d"), steps=10)
    r.hold(2500)

    r.caption("1", "Open it to read the request and the safety check")
    r.click(item.locator("[data-open]"))
    pg.locator("#detail .tl").wait_for()
    r.move((1370, 618), steps=8)
    r.hold(2800)

    r.caption("2", "Approve — the button asks you to confirm")
    r.click(pg.locator('#detail [data-act="approve"]'), settle_ms=200, nudge=True)
    r.hold(1600)

    r.caption("3", "Click once more: it is delivered and the approval is logged")
    r.click(pg.locator('#detail [data-act="approve"]'), settle_ms=700)
    r.move((1370, 690), steps=8)
    r.hold(3200)
    return r.frames


def scene_watch(pg, b) -> list:
    """터미널 대시보드(srhbroker watch) — 화면은 watch.render 그대로, 키 입력 뒤 나오는 질문 줄을 덧붙여 그린다."""
    os.environ.pop("NO_COLOR", None)
    snap = snapshot(b)
    route = next(a for a in snap["attention"] if a["kind"] == "route")
    num = str(snap["attention"].index(route) + 1)
    base = len(watch.render(snap, cols=118, rows=30)) + 3

    def draw(extra: list[str] = (), note: str = "") -> None:
        lines = watch.render(snapshot(b), cols=118, rows=30) + ([" " + note] if note else []) + list(extra)
        lines += [" "] * max(0, base - len(lines))   # 빈 줄 대신 공백 — <pre> 끝의 빈 줄은 높이가 없어 프레임 크기가 달라진다
        pg.evaluate("h => { document.querySelector('pre').innerHTML = h; }", ansi_html("\n".join(lines[:base])))

    pg.set_content(terminal_page("srhbroker watch — demo data", "", 118), wait_until="networkidle")
    pg.evaluate("document.fonts.ready")
    draw()
    pg.wait_for_timeout(300)
    r = Recorder(pg, el=pg.locator(".win"))
    cur = "\x1b[2m█\x1b[0m"
    ask1, ask2 = i18n.tr("작업 ID 또는 [번호]: "), i18n.tr("보낼 대상 (세션 이름·별칭·역할): ")
    r.caption(None, "The same view in a terminal: srhbroker watch (keys at the bottom)")
    r.hold(2800)

    r.caption("1", f"Press t to route a task, then pick it by its [number] — here [{num}]")
    draw([ask1 + cur])
    r.hold(1100)
    draw([ask1 + num + cur])
    r.hold(900)

    r.caption("2", "Type the session or role to send it to")
    typed = ""
    draw([ask1 + num, ask2 + cur])
    r.hold(700)
    for ch in "docs-claude":
        typed += ch
        draw([ask1 + num, ask2 + typed + cur])
        r.hold(105)
    r.hold(900)

    r.caption("3", "Enter: it goes to docs-claude and leaves the list")
    res = b.assign(route["task_id"], typed)
    draw(note=tx("routed").format(to=res["to"]))
    r.hold(3400)
    return r.frames


SCENES = {"tour": ("dashboard-tour.gif", scene_tour), "route": ("dashboard-route.gif", scene_route),
          "approve": ("dashboard-approve.gif", scene_approve), "watch": ("watch.gif", scene_watch)}


def make_broker():
    """장면마다 새 예시 데이터 (대상 지정·승인이 앞 장면에 영향을 주지 않게)."""
    home = Path(tempfile.mkdtemp(prefix="srhbroker-gifs-"))
    os.environ["SRHBROKER_HOME"] = str(home)   # 혹시 홈을 환경 변수로 찾는 코드가 있어도 예시 폴더를 보게
    (home / "config.toml").write_text(onboard.DEMO_CONFIG + f'\n[ui]\nlang = "{LANG}"\n', encoding="utf-8")
    b = Broker(load_config(home=home))
    with i18n.use(LANG):
        asyncio.run(onboard.seed_demo(b))
    b.herdr = GifHerdr()
    return b, home


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "images"))
    ap.add_argument("--only", default=",".join(SCENES), help="만들 장면 (쉼표로): " + ", ".join(SCENES))
    ap.add_argument("--bundled", action="store_true", help="설치된 Chrome 대신 playwright 가 받은 Chromium 사용")
    ap.add_argument("--lang", choices=list(TEXT), default="en", help="화면·예시 데이터 언어 (ko 는 파일 이름에 -ko)")
    a = ap.parse_args()
    global LANG
    LANG = a.lang
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    # 예시 데이터는 playwright 를 띄우기 전에 만든다 (sync playwright 가 이벤트 루프를 쥐고 있어 asyncio.run 을 못 씀)
    jobs = [(k, *make_broker()) for k in [k.strip() for k in a.only.split(",") if k.strip()]]
    from playwright.sync_api import sync_playwright
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(**({} if a.bundled else {"channel": "chrome"}))
        for key, b, home in jobs:
            name, scene = SCENES[key]
            if LANG != "en":
                name = name.replace(".gif", f"-{LANG}.gif")
            # 데몬이 실행 중인 것처럼: 잠금을 쥐고 상태 파일의 신호 시각을 계속 갱신한다 (가짜)
            lock = SingleInstance(home / "daemon.lock")
            lock.acquire()
            stop = threading.Event()

            def beat(home=home, stop=stop):
                started = time.time() - 3 * 3600
                while not stop.is_set():
                    (home / "daemon.json").write_text(json.dumps({
                        "pid": 4242, "started_at": started, "heartbeat": time.time(), "herdr": True,
                        "herdr_pane": "w9:p1",
                        # 코드가 바뀌어 '재시작 필요' 로 보이지 않게 (화면을 만드는 동안 소스를 고쳐도)
                        "code_mtime": code_mtime() + 10 ** 9}), encoding="utf-8")
                    stop.wait(1.0)
            threading.Thread(target=beat, daemon=True).start()
            time.sleep(0.2)

            httpd = None
            if key == "watch":   # 터미널 화면은 브로커 객체로 바로 그린다
                ctx = browser.new_context(viewport={"width": 1500, "height": 1000}, device_scale_factor=1)
                pg = ctx.new_page()
                run = lambda: scene(pg, b)  # noqa: E731
            else:
                port = free_port()
                httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(b, TOKEN, port))
                threading.Thread(target=httpd.serve_forever, daemon=True).start()
                ctx, pg = open_page(browser, port)
                run = lambda: scene(pg)  # noqa: E731
            try:
                with i18n.use(LANG):   # watch 화면은 이 프로세스에서 그린다 (웹은 페이지가 lang 을 넘김)
                    results.append(save_gif(run(), out / name))
            finally:
                ctx.close()
                if httpd:
                    httpd.shutdown()
                stop.set()
                lock.release()
                b.store.close()
        browser.close()
    for r in results:
        print(f"{r['path']}  {r['kb']} KB · {r['seconds']} s · {r['frames']} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
