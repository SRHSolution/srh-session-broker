"""모니터링: 같은 provider 흐름 관찰 기록, 스냅샷, 터미널·웹 대시보드."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from srhbroker import herdr as herdr_mod
from srhbroker import monitor, watch
from srhbroker.dashboard import make_handler
from srhbroker.hooks import claude_observe
from srhbroker.models import TaskStatus
from tests.conftest import make_broker
from tests.test_herdr import FakeHerdr


@pytest.fixture
def mb(tmp_path, monkeypatch):
    b = make_broker(tmp_path)
    b.register("pix-claude", "claude", mode="interactive", native_id="sid-pc", cwd=r"D:\proj\pix")
    b.register("pix-codex", "codex", mode="interactive", native_id="sid-px", cwd=r"D:\proj\pix")
    b.register("nat-claude", "claude", mode="interactive", native_id="sid-nc", cwd=r"D:\proj\nat")
    b.register("nat-codex", "codex", mode="interactive", native_id="sid-nx", cwd=r"D:\proj\nat")
    b.herdr = FakeHerdr({"sid-pc": "idle", "sid-nc": "working", "sid-nx": "idle"})   # pix-codex 창 닫힘
    monkeypatch.setattr(herdr_mod, "spawn_waiter", lambda name: None)
    return b


def _observe(b, payload):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return claude_observe(b, io.TextIOWrapper(io.BytesIO(raw), encoding="cp949"))


def test_claude_sendmessage_is_observed_not_relayed(mb):
    base = {"session_id": "sid-pc", "hook_event_name": "PostToolUse", "tool_name": "SendMessage"}
    _observe(mb, {**base, "tool_input": {"to": "nat-claude [3fa9c1]", "message": "검토 부탁 — 커밋 abc"}})
    _observe(mb, {**base, "tool_input": {"to": "main", "message": "하위 에이전트 → 본 대화"}})        # 제외
    _observe(mb, {**base, "tool_input": {"to": "nat-claude", "notify_when_idle": True}})           # 구독만 — 제외
    _observe(mb, {**base, "tool_name": "Bash", "tool_input": {"command": "ls"}})                   # 다른 도구 — 제외
    _observe(mb, {**base, "tool_input": {"to": "teammate-x", "message": {"type": "shutdown_response"}}})
    flows = mb.store.list_flows()
    assert [(f["transport"], f["from_name"], f["to_name"], f["to_raw"]) for f in reversed(flows)] == [
        ("claude-native", "pix-claude", "nat-claude", "nat-claude [3fa9c1]"),
        ("claude-native", "pix-claude", None, "teammate-x")]
    assert flows[-1]["summary"] == "검토 부탁 — 커밋 abc"
    assert mb.recent() == [] and mb.herdr.prompts == []                    # broker 작업·전달 없음


def test_body_is_not_stored_when_disabled(mb):
    mb.cfg.data["monitor"]["store_body_chars"] = 0
    mb.observe_native(provider="claude", from_native="sid-pc", to_raw="nat-claude", message="비밀 본문")
    assert mb.store.list_flows()[0]["summary"] is None


async def test_codex_direct_push_is_recorded(mb):
    r = await mb.send("빌드 로그 봐줘", sender="pix-codex", to="nat-codex")
    assert r["transport"] == "herdr-direct" and r["delivered"]
    f = [x for x in monitor.snapshot(mb)["flows"] if x["transport"] == "herdr-direct"][0]
    assert (f["from"], f["to"], f["status"], f["task_id"]) == ("pix-codex", "nat-codex", "delivered", r["direct_id"])
    d = monitor.task_detail(mb, r["direct_id"])
    assert d["kind"] == "direct" and d["body"] == "빌드 로그 봐줘" and d["terminal"]


async def test_snapshot_attention_sessions_and_merged_flows(mb):
    await mb.send("구현해", sender="pix-claude", to="pix-codex")                        # 창 닫힘 → 대기
    held = await mb.send("승인 필요", sender="nat-claude", to="nat-codex")
    mb.store.transition(held["task_id"], (TaskStatus.DELIVERED, TaskStatus.QUEUED), TaskStatus.HELD)
    bad = await mb.send("실패할 것", sender="nat-claude", to="nat-codex")
    mb.complete(bad["task_id"], TaskStatus.FAILED, error="빌드 실패", from_status=(TaskStatus.DELIVERED, TaskStatus.QUEUED))
    mb.observe_native(provider="claude", from_native="sid-pc", to_raw="nat-claude", message="직접")
    s = monitor.snapshot(mb)
    kinds = [a["kind"] for a in s["attention"]]
    assert kinds[0] == "engine"                                                          # 데몬 없음
    assert {"closed-pane", "approve", "failed"} <= set(kinds)
    st = {x["name"]: x["pane_state"] for x in s["sessions"]}
    assert st == {"pix-claude": "open", "pix-codex": "closed", "nat-claude": "open", "nat-codex": "open"}
    assert {f["transport"] for f in s["flows"]} == {"broker", "claude-native"}
    assert s["stats"]["flows_24h"]["claude-native"] == 1
    d = monitor.task_detail(mb, bad["task_id"])
    assert d["error"] == "빌드 실패" and d["terminal"]


def test_watch_render_and_korean_width(mb):
    assert watch.width("가a") == 3 and watch.width(watch.fit("한글한글한글", 7)) == 7
    assert watch.fit("abc", 5) == "abc  " and watch.fit("가나다라", 5).endswith("…")
    lines = watch.render(monitor.snapshot(mb), None, 120, 40)
    text = "\n".join(lines)
    assert "pix-codex" in text and "처리할 것" in text and "a 승인" in text
    assert "Codex직접" not in "\n".join(watch.render(monitor.snapshot(mb), "broker", 120, 40))


@pytest.fixture
def dash(mb):
    token = "tok-123"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(mb, token, 0))
    port = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(mb, token, port)   # 실제 포트로 Host·Origin 검사
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield mb, token, port
    srv.shutdown()
    srv.server_close()


def _req(port, path, *, token=None, method="GET", body=None, origin=None, host=None, cookie=None, headers=None):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                               data=json.dumps(body).encode() if body is not None else None)
    if token:
        r.add_header("X-Token", token)
    if cookie:
        r.add_header("Cookie", cookie)
    if origin:
        r.add_header("Origin", origin)
    if host:
        r.add_header("Host", host)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            if headers is not None:
                headers.update(resp.headers.items())
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


async def test_dashboard_security_and_actions(dash):
    b, token, port = dash
    origin = f"http://127.0.0.1:{port}"
    assert _req(port, "/")[0] == 403
    code, page = _req(port, f"/?t={token}")
    assert code == 200 and b"<title>Session Broker Dashboard</title>" in page
    assert _req(port, "/api/snapshot")[0] == 403
    assert _req(port, "/api/snapshot", token=token, host="evil.example")[0] == 403           # DNS 리바인딩
    code, body = _req(port, "/api/snapshot", token=token)
    assert code == 200 and json.loads(body)["sessions"]

    r = await b.send("취소할 것", sender="pix-claude", to="pix-codex")
    assert _req(port, "/api/cancel", token=token, method="POST", body={"task_id": r["task_id"]})[0] == 403
    assert _req(port, "/api/cancel", token=token, method="POST", body={"task_id": r["task_id"]},
                origin="http://evil.example")[0] == 403                                    # CSRF
    code, body = _req(port, "/api/cancel", token=token, method="POST", body={"task_id": r["task_id"]}, origin=origin)
    assert code == 200 and json.loads(body)["status"] == "cancelled"

    h = await b.send("승인 필요", sender="nat-claude", to="nat-codex")
    b.store.transition(h["task_id"], (TaskStatus.DELIVERED, TaskStatus.QUEUED), TaskStatus.HELD)
    code, body = _req(port, "/api/approve", token=token, method="POST", body={"task_id": h["task_id"]}, origin=origin)
    assert code == 200
    log = [x for x in b.store.route_log(20) if x["router"] == "approve"]
    assert log[0]["meta"]["via"] == "dashboard"
    code, body = _req(port, "/api/cancel", token=token, method="POST", body={"task_id": "t_없음"}, origin=origin)
    assert code == 400 and "error" in json.loads(body)


async def test_relay_timeline_records_route_hold_approve_push_and_reply(mb):
    r = await mb.send("PLC 재연결 로직 고쳐줘", sender="nat-claude", to="nat-codex", sandbox="workspace-write")
    mb.store.transition(r["task_id"], (TaskStatus.DELIVERED, TaskStatus.QUEUED, TaskStatus.HELD), TaskStatus.HELD)
    mb.approve(r["task_id"], "workspace-write", confirmation="쓰기권한 승인해", via="mcp")
    mb.reply(r["task_id"], "고쳤습니다", sender="nat-codex")
    tl = monitor.task_detail(mb, r["task_id"])["timeline"]
    steps = [e["step"] for e in tl]
    assert steps[:3] == ["보냄", "라우팅", "Jev"] and "안전 판단" in steps
    by = {e["step"]: e["text"] for e in tl}
    assert "읽기 전용으로 낮춤" in by["안전 판단"] and "승인 대기로 보류" in by["안전 판단"]
    assert "세션 대화창" in by["승인"] and "쓰기권한 승인해" in by["승인"] and "권한 workspace-write" in by["승인"]
    assert "herdr 로 받는 창에 바로 넣음" in by["전달"]
    assert by["회신"].startswith("완료로 회신") and "보낸 창(nat-claude)" in by["알림"]
    assert all(e["after_s"] is not None and e["after_s"] >= 0 for e in tl)


async def test_timeline_message_cancel_and_direct(mb):
    m = await mb.send("참고만", sender="nat-claude", to="nat-codex", kind="message")
    steps = [e["step"] for e in monitor.task_detail(mb, m["task_id"])["timeline"]]
    assert "전달" in steps and "회신" not in steps                     # message 는 넣으면 끝
    q = await mb.send("닫힌 창에", sender="nat-claude", to="pix-codex")   # 창 닫힘 → 대기
    tl = monitor.task_detail(mb, q["task_id"])["timeline"]
    assert tl[-1]["step"] == "대기" and tl[-1]["at"] is None
    mb.cancel(q["task_id"], by="user")
    tl = monitor.task_detail(mb, q["task_id"])["timeline"]
    c = [e for e in tl if e["step"] == "취소"]
    assert len(c) == 1 and "사람" in c[0]["text"] and "넣기 전" in c[0]["text"]
    d = await mb.send("빌드 봐줘", sender="pix-codex", to="nat-codex")
    assert [e["step"] for e in monitor.task_detail(mb, d["direct_id"])["timeline"]] == ["보냄", "전달"]


async def test_search_flows_filters(mb):
    a = await mb.send("PLC 백오프 구현", sender="pix-claude", to="pix-codex")          # 창 닫힘 → active
    done = await mb.send("검토 결과 정리", sender="nat-claude", to="nat-codex")
    mb.reply(done["task_id"], "끝", sender="nat-codex")
    d = await mb.send("빌드 로그 100%_확인", sender="pix-codex", to="nat-codex")       # Codex 직접
    mb.observe_native(provider="claude", from_native="sid-pc", to_raw="nat-claude", message="세션 간 메시지\n둘째 줄")
    mb.observe_native(provider="claude", from_native="sid-pc", to_raw="builder-c1", message="팀원에게")
    ids = lambda **kw: {f["id"] for f in monitor.search_flows(mb, **kw)}
    assert len(ids()) == 5 and len(ids(teammates=False)) == 4                         # 팀원 메시지 빼기
    assert ids(q="백오프") == {a["task_id"]} and ids(q="100%_") == {d["direct_id"]}    # % _ 는 글자 그대로
    assert ids(kind="herdr-direct") == {d["direct_id"]}
    assert ids(status="active") == {a["task_id"]} and done["task_id"] in ids(status="done")
    assert ids(session="nat-codex") == {done["task_id"], d["direct_id"]}
    assert ids(q="pix-claude", kind="claude-native", teammates=False) and not ids(since_hours=-1)
    row = next(f for f in monitor.search_flows(mb, kind="claude-native", teammates=False))
    assert (row["summary"], row["excerpt"]) == ("세션 간 메시지", "둘째 줄")              # 미리보기는 제목 다음부터
    det = monitor.task_detail(mb, row["id"])
    assert det["kind"] == "native" and "둘째 줄" in det["body"] and det["timeline"][0]["step"] == "보냄"


def test_observe_skips_subagent_chatter(mb):
    base = {"session_id": "sid-pc", "tool_name": "SendMessage", "agent_id": "a1b2"}
    _observe(mb, {**base, "tool_input": {"to": "team-lead", "message": "팀장에게 보고"}})      # 세션 내부 — 제외
    _observe(mb, {**base, "tool_input": {"to": "nat-claude", "message": "다른 세션으로"}})     # 등록 세션 — 기록
    assert [f["to_name"] for f in mb.store.list_flows()] == ["nat-claude"]


async def test_dashboard_flows_api(dash):
    b, token, port = dash
    await b.send("검색될 작업", sender="pix-claude", to="pix-codex")
    code, body = _req(port, "/api/flows?q=" + urllib.request.quote("검색될") + "&since=24", token=token)
    assert code == 200 and [f["summary"] for f in json.loads(body)["flows"]] == ["검색될 작업"]
    assert _req(port, "/api/flows?since=abc", token=token)[0] == 400
    assert _req(port, "/api/flows")[0] == 403


async def test_timeline_explains_hardware_rule(mb):
    hw = await mb.send("PLC 펄스 off 처리 수정", sender="nat-claude", to="nat-codex", sandbox="workspace-write")
    text = {e["step"]: e["text"] for e in monitor.task_detail(mb, hw["task_id"])["timeline"]}["안전 판단"]
    assert "하드웨어 대상(PLC" in text and "제어 동작(" in text and "읽기 전용으로 낮춤" in text
    web = await mb.send("합성 X-ray 팬텀 MP4 복사하고 빌드", sender="nat-claude", to="nat-codex", sandbox="workspace-write")
    t = monitor.task_detail(mb, web["task_id"])
    assert t["sandbox"] == "workspace-write"
    text = {e["step"]: e["text"] for e in t["timeline"]}["안전 판단"]
    assert "X-ray" in text and "읽기 전용 아님" in text


async def test_dashboard_remembers_token_in_cookie_for_refresh(dash):
    """처음 토큰 주소로 열면 쿠키를 남겨, 새로고침(토큰 없는 주소)·API·조작이 쿠키로 통한다. 보안 검사는 그대로."""
    b, token, port = dash
    origin = f"http://127.0.0.1:{port}"
    hdrs: dict = {}
    code, _ = _req(port, f"/?t={token}", headers=hdrs)
    set_cookie = hdrs.get("Set-Cookie", "")
    assert code == 200 and f"srhb_token_{port}={token}" in set_cookie
    assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie and "Max-Age=" in set_cookie
    cookie = f"srhb_token_{port}={token}"
    code, page = _req(port, "/", cookie=cookie)                                          # 새로고침
    assert code == 200 and b"<title>Session Broker Dashboard</title>" in page
    assert _req(port, "/api/snapshot", cookie=cookie)[0] == 200                         # 새 탭(헤더 없음)
    code, page = _req(port, "/", cookie=f"srhb_token_{port}=wrong")
    assert code == 403 and "srhbroker dashboard".encode() in page                       # 안내 페이지
    assert _req(port, "/", cookie=f"srhb_token_1={token}")[0] == 403                    # 다른 포트용 쿠키
    assert _req(port, "/api/snapshot", cookie=cookie, host="evil.example")[0] == 403    # DNS 리바인딩
    r = await b.send("취소할 것", sender="pix-claude", to="pix-codex")
    assert _req(port, "/api/cancel", cookie=cookie, method="POST", body={"task_id": r["task_id"]},
                origin="http://evil.example")[0] == 403                                  # CSRF
    code, body = _req(port, "/api/cancel", cookie=cookie, method="POST", body={"task_id": r["task_id"]}, origin=origin)
    assert code == 200 and json.loads(body)["status"] == "cancelled"
