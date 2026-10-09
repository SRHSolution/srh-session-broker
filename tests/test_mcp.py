"""MCP 도구 계층: 환경 변수 기반 발신자·부모 작업, 오류 변환, approve 차단."""

import json

import pytest
from mcp import Client

from srhbroker import mcp_server
from tests.conftest import make_broker


@pytest.fixture
def mcp_broker(tmp_path, monkeypatch):
    b = make_broker(tmp_path)
    b.register("planner", "claude", mode="interactive", roles=["planner"])
    b.register("builder", "codex", roles=["builder"], aliases=["b"])
    monkeypatch.setattr(mcp_server, "_broker", b)
    monkeypatch.setenv("SRHBROKER_SELF", "planner")
    monkeypatch.delenv("SRHBROKER_TASK", raising=False)
    return b


def _data(res):
    assert not res.is_error, res.content[0].text
    return json.loads(res.content[0].text)


def _list(res):
    """목록을 돌려주는 도구: 항목마다 content 가 따로 오고 전체는 structured_content['result']."""
    assert not res.is_error, res.content[0].text
    return res.structured_content["result"]


async def _call(name, args):
    """실제 MCP 프로토콜 경로(in-process 연결)로 호출."""
    async with Client(mcp_server.mcp) as c:
        return await c.call_tool(name, args)


async def test_tools_listed():
    async with Client(mcp_server.mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert {"send", "inbox", "reply", "wait", "route", "approve", "sessions", "register"} <= names


async def test_send_uses_env_sender_and_parent(mcp_broker, monkeypatch):
    r = _data(await _call("send", {"body": "@b 구현해"}))
    assert (r["from"], r["to"], r["hop"]) == ("planner", "builder", 0)
    monkeypatch.setenv("SRHBROKER_SELF", "builder")
    monkeypatch.setenv("SRHBROKER_TASK", r["task_id"])
    r2 = _data(await _call("send", {"body": "질문", "to": "planner"}))
    assert r2["hop"] == 1


async def test_errors_become_tool_errors(mcp_broker):
    res = await _call("send", {"body": "x", "to": "codex:nobody"})
    assert res.is_error and "세션이 없습니다" in res.content[0].text


async def test_same_provider_send_returns_native_handoff(mcp_broker):
    mcp_broker.register("peer", "claude", native_id="peer-id")
    r = _data(await _call("send", {"body": "직접 전달", "to": "peer", "kind": "message"}))
    assert r["status"] == "native_required" and r["task_id"] is None
    assert r["delivered"] is False and r["targets"][0]["native_id"] == "peer-id"
    assert "ListAgents" in r["next"] and "SendMessage" in r["next"]
    assert not mcp_broker.recent()


async def test_approve_blocked_by_default(mcp_broker):
    res = await _call("approve", {"task_id": "t_x", "confirmation": "승인해"})
    assert res.is_error and "srhbroker approve" in res.content[0].text


async def test_approve_follows_config_without_restart(mcp_broker, tmp_path):
    """allow_mcp_approve 는 호출마다 다시 읽는다 — 이미 떠 있는 MCP 서버에도 바로 반영."""
    from srhbroker.models import TaskStatus
    r = await mcp_broker.send("x", sender="planner", to="builder")
    mcp_broker.store.transition(r["task_id"], (TaskStatus.QUEUED,), TaskStatus.HELD)
    res = await _call("approve", {"task_id": r["task_id"], "confirmation": "승인해"})
    assert res.is_error and "srhbroker approve" in res.content[0].text
    (mcp_broker.cfg.home / "config.toml").write_text("[broker]\nallow_mcp_approve = true\n", encoding="utf-8")
    res = await _call("approve", {"task_id": r["task_id"], "confirmation": "  "})
    assert res.is_error and "confirmation" in res.content[0].text            # 사용자 승인 말 없이는 거부
    assert _data(await _call("approve", {"task_id": r["task_id"], "confirmation": "응 승인해"}))["status"] == "queued"
    log = [x for x in mcp_broker.store.route_log(10) if x["router"] == "approve"]
    assert log and log[0]["meta"]["confirmation"] == "응 승인해" and log[0]["meta"]["via"] == "mcp"


async def test_recent_mine_active_lists_only_my_pending_sends(mcp_broker, monkeypatch):
    from srhbroker.models import TaskStatus
    mcp_broker.register("rev", "codex", roles=["reviewer"])
    mine1 = await mcp_broker.send("a", sender="planner", to="builder")          # queued
    mine2 = await mcp_broker.send("b", sender="planner", to="rev")              # queued → done
    mcp_broker.complete(mine2["task_id"], TaskStatus.DONE, result="ok")
    other = await mcp_broker.send("c", sender="user", to="builder")             # 다른 발신자
    got = _list(await _call("recent", {"mine": True, "active": True}))
    assert [t["task_id"] for t in got] == [mine1["task_id"]]
    assert {t["task_id"] for t in _list(await _call("recent", {"mine": True}))} == {mine1["task_id"], mine2["task_id"]}
    assert other["task_id"] in {t["task_id"] for t in _list(await _call("recent", {"active": True}))}
    monkeypatch.delenv("SRHBROKER_SELF")
    res = await _call("recent", {"mine": True})
    assert res.is_error and "등록되어 있지 않아" in res.content[0].text


async def test_cancel_only_by_sender_or_user(mcp_broker, monkeypatch, capsys):
    from srhbroker import cli
    r = await mcp_broker.send("작업", sender="planner", to="builder")
    monkeypatch.setenv("SRHBROKER_SELF", "builder")                            # 받는 쪽은 취소 불가
    res = await _call("cancel", {"task_id": r["task_id"]})
    assert res.is_error and "보낸 세션이나 사용자" in res.content[0].text and "reply(status='failed')" in res.content[0].text
    monkeypatch.delenv("SRHBROKER_SELF")                                       # 등록 안 된 세션도 불가
    res = await _call("cancel", {"task_id": r["task_id"]})
    assert res.is_error and "취소 권한" in res.content[0].text
    monkeypatch.setenv("SRHBROKER_SELF", "planner")                            # 보낸 세션은 가능
    assert _data(await _call("cancel", {"task_id": r["task_id"]}))["status"] == "cancelled"
    r2 = await mcp_broker.send("작업2", sender="planner", to="builder")         # 터미널의 사람은 무엇이든 가능
    monkeypatch.setattr(cli, "_broker", lambda: mcp_broker)
    assert cli.main(["cancel", r2["task_id"]]) == 0 and '"cancelled"' in capsys.readouterr().out


def test_cli_tasks_filters(mcp_broker, monkeypatch, capsys):
    import asyncio
    from srhbroker import cli
    asyncio.run(mcp_broker.send("내 것", sender="planner", to="builder"))
    asyncio.run(mcp_broker.send("남의 것", sender="user", to="builder"))
    monkeypatch.setattr(cli, "_broker", lambda: mcp_broker)
    assert cli.main(["tasks", "--from", "planner", "--active"]) == 0
    out = capsys.readouterr().out
    assert "내 것" in out and "남의 것" not in out
