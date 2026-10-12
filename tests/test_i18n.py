"""화면 언어: 번역 목록이 코드의 원문을 모두 갖는지, 영어 화면(웹 API·watch·예시 데이터)에 한국어가 남지 않는지."""

from __future__ import annotations

import ast
import asyncio
import json
import re
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from srhbroker import cli, i18n, monitor, onboard, watch
from srhbroker.config import load_config
from srhbroker.dashboard import _page, make_handler
from srhbroker.service import Broker
from tests.test_monitor import _req

SRC = Path(__file__).resolve().parents[1] / "src" / "srhbroker"
HANGUL = re.compile(r"[가-힣]")


def _tr_keys(path: Path) -> set[str]:
    """tr("…") 로 부르는 원문 (문자열 그대로 넘긴 것만)."""
    keys = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "tr"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
    return keys


@pytest.mark.parametrize("name", ["monitor.py", "watch.py", "dashboard.py", "onboard.py"])
def test_every_python_source_text_has_english(name):
    missing = sorted(k for k in _tr_keys(SRC / name) if k not in i18n.EN)
    assert not missing, f"i18n.EN 에 없는 원문: {missing}"


def test_tr_context_and_format():
    with i18n.use("ko"):
        assert i18n.tr("역할|열") == "역할" and i18n.tr("{n}초", n=3) == "3초"
    with i18n.use("en"):
        assert i18n.tr("역할|열") == "Roles" and i18n.tr("{n}초", n=3) == "3s"
        assert i18n.tr("번역 없는 문구|x") == "번역 없는 문구"
        assert i18n.stored("역할 'reviewer'") == "role 'reviewer'" and i18n.stored("name 일치") == "name match"
        assert i18n.stored("시간 초과(30s)") == "timed out (30s)" and i18n.stored("자유 문구") == "자유 문구"
    assert i18n.stored("취소됨") == "취소됨"                  # 한국어 화면은 그대로
    assert i18n.current() == "ko"                            # use() 가 끝나면 원래대로


def test_language_setting_and_system_language(monkeypatch):
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "ko_KR.UTF-8")
    assert i18n.system_lang() == "ko"
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert i18n.system_lang() == "en"

    class Cfg:
        data = {"ui": {"lang": "EN"}}
    assert i18n.setting(Cfg) == "en" and i18n.config_lang(Cfg) == "en"
    Cfg.data = {"ui": {"lang": "fr"}}
    assert i18n.setting(Cfg) == "auto" and i18n.config_lang(Cfg) == "en"   # auto → 시스템(LANG=en)


@pytest.fixture
def demo_en(tmp_path):
    (tmp_path / "config.toml").write_text(onboard.DEMO_CONFIG + '\n[ui]\nlang = "en"\n', encoding="utf-8")
    b = Broker(load_config(home=tmp_path))
    with i18n.use("en"):
        asyncio.run(onboard.seed_demo(b))
    yield b
    b.store.close()


def _hangul(o, path="") -> list[str]:
    if isinstance(o, str):
        return [f"{path}: {o}"] if HANGUL.search(o) else []
    if isinstance(o, dict):
        return [x for k, v in o.items() if k not in ("route_meta", "route_log", "home") for x in _hangul(v, f"{path}.{k}")]
    if isinstance(o, list):
        return [x for i, v in enumerate(o) for x in _hangul(v, f"{path}[{i}]")]
    return []


def test_english_screens_have_no_korean(demo_en):
    """영어 예시 데이터 + 영어 화면: 처리할 것·세션·흐름·상세(중계 경과)·watch 에 한국어가 없다 (DB 원본 기록 제외)."""
    b = demo_en
    with i18n.use("en"):
        snap = monitor.snapshot(b)
        left = _hangul(snap)
        for f in snap["flows"]:
            left += _hangul(monitor.task_detail(b, f["id"]), f["id"])
        left += [x for x in watch.render(snap, None, 120, 50) if HANGUL.search(x)]
    assert not left, left
    kinds = {a["kind"] for a in snap["attention"]}
    assert {"approve", "route"} <= kinds                       # 영어 예시도 승인 대기·대상 미정 장면이 있다
    held = next(a for a in snap["attention"] if a["kind"] == "approve")
    assert held["detail"].startswith("Device-control code change")


def test_dashboard_page_and_api_follow_language(demo_en):
    b, token = demo_en, "tok-456"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(b, token, 0))
    port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(b, token, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, page = _req(port, f"/?t={token}")
        assert code == 200 and b'data-lang="en"' in page          # 설정 [ui] lang 을 페이지에 넘긴다
        en = json.loads(_req(port, "/api/snapshot?lang=en", token=token)[1])
        ko = json.loads(_req(port, "/api/snapshot?lang=ko", token=token)[1])
        assert not _hangul(en["attention"]) and any(HANGUL.search(a["title"]) for a in ko["attention"])
        tid = next(a["task_id"] for a in en["attention"] if a["kind"] == "approve")
        steps = [e["step"] for e in json.loads(_req(port, f"/api/task/{tid}?lang=en", token=token)[1])["timeline"]]
        assert steps[0] == "Sent" and "Safety check" in steps
        assert "Open the dashboard with its token" in _req(port, "/")[1].decode()   # 토큰 안내는 두 언어
    finally:
        srv.shutdown()
        srv.server_close()
    assert b'data-lang="auto"' in _page()


def test_demo_uses_configured_language(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    (real / "config.toml").write_text('[ui]\nlang = "en"\n', encoding="utf-8")
    monkeypatch.setenv("SRHBROKER_HOME", str(real))
    demo = tmp_path / "demo"
    assert cli.main(["demo", "--home", str(demo)]) == 0
    assert 'lang = "en"' in (demo / "config.toml").read_text(encoding="utf-8")
    b = Broker(load_config(home=demo))
    try:
        assert "Cart coupon API" in {t.title for t in b.store.list_tasks()}
    finally:
        b.store.close()


# ── 웹 대시보드 (dashboard.html) ─────────────────────────────────────────────

HTML = (SRC / "static" / "dashboard.html").read_text(encoding="utf-8")


def _js_en() -> dict[str, str]:
    block = HTML[HTML.index("const EN = {"):HTML.index("};", HTML.index("const EN = {"))]
    return dict(re.findall(r'"((?:[^"\\]|\\.)*)":\s*"((?:[^"\\]|\\.)*)"', block))


def test_dashboard_html_korean_texts_all_have_english():
    """화면에 나가는 한국어(마크업 data-t·aria-label·placeholder·title, 스크립트의 문자열)가 모두 EN 에 있다."""
    en = _js_en()
    script = HTML[HTML.index("<script>"):HTML.index("</script>")]
    script = script[:script.index("const EN = {")] + script[script.index("};", script.index("const EN = {")):]
    script = re.sub(r"(?m)//.*$|/\*.*?\*/", "", script)                       # 주석 제외
    texts = set(re.findall(r'"([^"\n]*[가-힣][^"\n]*)"', script))
    markup = HTML[HTML.index("<body>"):HTML.index("<script>")]
    texts |= {m.strip() for m in re.findall(r"<[^>]*\bdata-t\b[^>]*>([^<]+)<", markup)}
    texts |= set(re.findall(r'(?:aria-label|placeholder|title)="([^"]*[가-힣][^"]*)"', markup))
    texts -= {"한국어"}                                                          # 언어 바꾸기 버튼 이름
    missing = sorted(t for t in texts if t not in en)
    assert not missing, f"dashboard.html EN 에 없는 원문: {missing}"
    assert not [k for k, v in en.items() if HANGUL.search(v)]
    # 마크업에 남은 한국어 글자는 모두 번역되는 자리(data-t·속성)여야 한다
    bare = re.sub(r'(?:aria-label|placeholder|title)="[^"]*"', "", markup)
    bare = re.sub(r"<[^>]*\bdata-t\b[^>]*>[^<]+<", "<", bare)
    assert not HANGUL.search(re.sub(r"<!--.*?-->", "", bare, flags=re.S))
