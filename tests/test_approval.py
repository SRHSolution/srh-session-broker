"""승인(held) 정책: 세션이 스스로 판단해 보낸 '쓰기' 작업 중 장비 제어 코드·중요 코드 변경·되돌리기 어려운 외부 작업만.
알림·읽기 전용 요청·사용자 직접 지시·사람(CLI)은 승인하지 않는다. 승인하면 요청한 권한으로 실행한다."""

from __future__ import annotations

import pytest

from srhbroker import herdr as herdr_mod
from srhbroker import monitor
from srhbroker.models import BrokerError, TaskStatus
from tests.conftest import FakeJudge, make_broker


def _b(tmp_path, monkeypatch, *, human=0.0, hw=0.0):
    b = make_broker(tmp_path, judge=FakeJudge(hw=hw, human=human), roles=[])
    b.register("lead", "claude", mode="interactive", native_id="sid-lead")
    b.register("dev", "codex", mode="interactive", native_id="sid-dev")       # 사람이 보는 Codex 창
    b.register("bot", "codex", mode="worker")                                   # 데몬이 실행하는 Codex
    monkeypatch.setattr(herdr_mod, "spawn_waiter", lambda name: None)
    return b


def _st(b, r):
    """(승인 대기 여부, 실제 권한, 승인 판단). 권한을 지정하지 않았으면 받는 세션의 최대 권한."""
    t = b.store.require_task(r["task_id"])
    pol = (t.route_meta or {}).get("policy", {})
    return t.status == TaskStatus.HELD, (t.sandbox.value if t.sandbox else pol.get("requested_sandbox")), pol.get("approval")


async def test_notice_to_interactive_is_never_held(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, human=0.9)
    r = await b.send("HW 시험 창 종료 — 재개 가능", sender="lead", to="dev", kind="message")
    assert _st(b, r)[0] is False and _st(b, r)[2] == "notice"
    r = await b.send("PLC 펄스 off 처리 수정 완료 공유", sender="lead", to="dev", kind="message")   # 장비 관련 알림
    assert _st(b, r)[0] is False


async def test_message_to_worker_is_judged_like_a_task(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, human=0.9)
    r = await b.send("origin 에 push 해", sender="lead", to="bot", kind="message")   # worker 는 메시지도 실행한다
    assert _st(b, r)[0] is True and _st(b, r)[2] == "irreversible"


async def test_read_only_request_is_not_held(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, human=0.9)
    r = await b.send("PLC 펄스 off 처리 수정안을 검토만 해줘", sender="lead", to="dev", sandbox="read-only")
    assert _st(b, r) == (False, "read-only", "read-only")


async def test_self_judged_irreversible_write_is_held_then_approved(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, human=0.9)
    r = await b.send("운영 서버에 배포해", sender="lead", to="dev")
    assert _st(b, r) == (True, "workspace-write", "irreversible")
    assert "되돌리기 어려운" in r["next"]
    out = b.approve(r["task_id"], confirmation="응 배포해", via="mcp")
    assert out["status"] != "held" and b.store.require_task(r["task_id"]).sandbox.value == "workspace-write"


async def test_hw_control_code_change_is_held_read_only_then_restored_on_approve(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, human=0.6)                 # Jev: 실제로 변경을 실행하게 하는 요청
    r = await b.send("PLC 펄스 off 처리 수정", sender="lead", to="dev", sandbox="workspace-write")
    assert _st(b, r) == (True, "read-only", "hw-code")          # 승인 전에는 읽기 전용
    b.approve(r["task_id"], confirmation="대시보드에서 승인", via="dashboard")  # 권한을 지정하지 않아도
    assert b.store.require_task(r["task_id"]).sandbox.value == "workspace-write"   # 요청한 권한으로 실행
    log = [x for x in b.store.route_log(20) if x["router"] == "approve"][0]["meta"]
    assert (log["sandbox_before"], log["sandbox"]) == ("read-only", "workspace-write")


async def test_hw_rule_with_read_only_request_just_stays_read_only(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, human=0.9)
    r = await b.send("PLC 펄스 off 처리 수정 방향 검토", sender="lead", to="dev", sandbox="read-only")
    assert _st(b, r) == (False, "read-only", "read-only")


async def test_user_directed_task_skips_second_approval_and_keeps_permission(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, human=0.9)
    r = await b.send("PLC 펄스 off 처리 수정 후 장비에서 확인", sender="lead", to="dev", sandbox="workspace-write",
                     user_directed=True, user_request="codex 한테 PLC 펄스 off 처리 고치라고 해")
    assert _st(b, r) == (False, "workspace-write", "user")
    pol = b.store.require_task(r["task_id"]).route_meta["policy"]
    assert pol["user_request"] == "codex 한테 PLC 펄스 off 처리 고치라고 해" and pol["user_directed"]
    tl = {e["step"]: e["text"] for e in monitor.task_detail(b, r["task_id"])["timeline"]}
    assert "사용자가 직접 지시" in tl["승인 판단"] and "PLC 펄스 off 처리 고치라고" in tl["승인 판단"]
    assert "권한 유지" in tl["안전 판단"]


async def test_user_directed_requires_quote_and_is_not_allowed_inside_a_task(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch)
    with pytest.raises(BrokerError, match="user_request"):
        await b.send("배포해", sender="lead", to="dev", user_directed=True, user_request="  ")
    parent = await b.send("상위 작업", sender="lead", to="bot")
    with pytest.raises(BrokerError, match="작업 실행 중"):
        await b.send("하위", sender="bot", to="lead", parent_id=parent["task_id"], user_directed=True, user_request="해")


async def test_human_cli_sender_is_never_held(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, human=0.9)
    r = await b.send("PLC 펄스 off 처리 수정", sender="user", to="dev", sandbox="workspace-write")
    assert _st(b, r) == (False, "workspace-write", "user")


async def test_mcp_send_accepts_user_directed(tmp_path, monkeypatch):
    import json
    from mcp import Client
    from srhbroker import mcp_server
    b = _b(tmp_path, monkeypatch, human=0.9)
    monkeypatch.setattr(mcp_server, "_broker", b)
    monkeypatch.setenv("SRHBROKER_SELF", "lead")
    monkeypatch.delenv("SRHBROKER_TASK", raising=False)
    async with Client(mcp_server.mcp) as c:
        res = await c.call_tool("send", {"body": "운영 서버에 배포해", "to": "dev", "user_directed": True,
                                         "user_request": "dev 한테 배포하라고 해"})
    out = json.loads(res.content[0].text)
    assert out["status"] != "held"


async def test_hw_rule_review_request_is_read_only_without_approval_when_jev_says_not_executing(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, human=0.1)                 # Jev: 변경을 실행하게 하는 요청이 아님 (검토)
    r = await b.send("제어면 작업 완료 공유: PLC 주소 쓰기 로직 변경분을 검토해 주세요", sender="lead", to="dev")
    assert _st(b, r) == (False, "read-only", "hw-read-only")
    b2 = _b(tmp_path / "2", monkeypatch, human=0.6)          # Jev: 실제 변경 요청
    r = await b2.send("PLC 주소 쓰기 로직 변경해", sender="lead", to="dev")
    assert _st(b2, r) == (True, "read-only", "hw-code")


async def test_hw_rule_without_jev_judgement_is_held_for_safety(tmp_path, monkeypatch):
    b = make_broker(tmp_path, mode="rules", roles=[])        # Jev 를 쓰지 않는 모드
    b.register("lead", "claude", mode="interactive", native_id="sid-lead")
    b.register("dev", "codex", mode="interactive", native_id="sid-dev")
    monkeypatch.setattr(herdr_mod, "spawn_waiter", lambda name: None)
    r = await b.send("PLC 펄스 off 처리 수정", sender="lead", to="dev")
    assert _st(b, r) == (True, "read-only", "hw-code")
