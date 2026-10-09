"""같은 provider는 기본 도구로 인계하고 broker에는 교차 provider 작업만 남긴다."""

from unittest.mock import AsyncMock, Mock

import pytest

from srhbroker import cli
from srhbroker.models import BrokerError
from tests.conftest import FakeJudge, make_broker


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("kind", ["task", "message", "reply"])
@pytest.mark.parametrize("address", ["peer", "alias", "qualified", "mention"])
async def test_same_provider_handoff_has_no_broker_side_effects(tmp_path, provider, kind, address):
    b = make_broker(tmp_path)
    b.register("source", provider, aliases=["src"])
    b.register("peer", provider, aliases=["p"], native_id="peer-native-id")
    b.router.route = AsyncMock(side_effect=AssertionError("라우터 호출 금지"))
    b.notify = Mock(side_effect=AssertionError("herdr/inbox 전달 금지"))
    b.store.insert_task = Mock(side_effect=AssertionError("작업 저장 금지"))
    b.store.log_route = Mock(side_effect=AssertionError("라우팅 기록 금지"))
    to = {"peer": "peer", "alias": "p", "qualified": f"{provider}:peer", "mention": None}[address]
    r = await b.send("@p 본문" if address == "mention" else "본문", sender="src", to=to,
                     kind=kind, title="요청", acceptance=["조건"], reply_schema={"ok": "bool"},
                     sandbox="read-only", deadline_s=123, resume_on_reply=True)
    assert (r["status"], r["transport"], r["delivered"], r["task_id"]) == ("native_required", "native", False, None)
    assert (r["from"], r["to"], r["provider"]) == ("source", "peer", provider)
    assert r["targets"][0]["native_id"] == "peer-native-id"
    assert r["request"] == {
        "body": "본문", "title": "요청", "kind": kind, "acceptance": ["조건"],
        "reply_schema": {"ok": "bool"}, "sandbox": "read-only", "deadline_s": 123,
        "resume_on_reply": True,
    }
    assert b.recent() == [] and b.store.route_log() == []
    assert b.inbox("peer")["incoming"] == []
    b.router.route.assert_not_called()
    b.notify.assert_not_called()


@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_same_provider_hint_returns_candidates_without_selecting(tmp_path, provider):
    j = FakeJudge()
    b = make_broker(tmp_path, judge=j)
    b.register("source", provider)
    r = await b.send("본문", sender="source", to=provider)
    assert r["status"] == "native_required" and r["targets"] == []
    for name in ("peer1", "peer2"):
        b.register(name, provider)
    b.register("other", "codex" if provider == "claude" else "claude")
    r = await b.send("본문", sender="source", to=provider)
    assert r["to"] is None
    assert {s["name"] for s in r["targets"]} == {"peer1", "peer2"}
    assert all(s["native_id"] is None for s in r["targets"])
    assert not j.calls and not b.recent()


@pytest.mark.parametrize("sender_provider,receiver_provider", [("claude", "codex"), ("codex", "claude")])
async def test_automatic_routing_and_jev_see_only_other_provider(tmp_path, sender_provider, receiver_provider):
    j = FakeJudge(probs={"builder": 0.9, "reviewer": 0.1})
    b = make_broker(tmp_path, judge=j)
    b.register("source", sender_provider)
    b.register("same-planner", sender_provider, roles=["planner"])
    b.register("same-builder", sender_provider, roles=["builder"])
    b.register("other-builder", receiver_provider, roles=["builder"])
    b.register("other-reviewer", receiver_provider, roles=["reviewer"])
    r = await b.send("구현", sender="source")
    assert (r["to"], r["routed_by"]) == ("other-builder", "jev")
    assert set(j.calls[0]["options"]) == {"builder", "reviewer"}


async def test_rule_cannot_select_same_provider(tmp_path):
    b = make_broker(tmp_path, mode="rules", roles=[], rules=[{"pattern": ".*", "to": "peer"}])
    b.register("source", "claude")
    b.register("peer", "claude")
    b.register("other", "codex")
    r = await b.send("본문", sender="source")
    assert (r["to"], r["routed_by"]) == ("other", "only")


async def test_assign_cannot_bypass_provider_boundary(tmp_path):
    b = make_broker(tmp_path, mode="rules")
    b.register("source", "claude")
    b.register("peer", "claude", aliases=["p"], roles=["reviewer", "planner"])
    b.register("other", "codex", roles=["reviewer"])
    r = await b.send("본문", sender="source", to="planner")
    assert r["status"] == "needs_routing"
    log = b.store.route_log()
    for to in ("peer", "p", "claude:peer", "planner"):
        with pytest.raises(BrokerError, match="provider"):
            b.assign(r["task_id"], to)
        assert b.status(r["task_id"])["status"] == "needs_routing"
        assert b.store.route_log() == log
    assert b.assign(r["task_id"], "reviewer")["to"] == "other"


async def test_native_handoff_does_not_consume_parent_budget(broker):
    parent = await broker.send("기존 작업", sender="builder", to="planner")
    broker.cfg.broker["max_hops"] = 0
    r = await broker.send("직접 전달", sender="planner", to="reviewer", parent_id=parent["task_id"])
    assert r["status"] == "native_required"
    assert broker.store.count_root(parent["task_id"]) == 1


@pytest.mark.parametrize("body,to", [("@rev", None), ("본문", "planner")])
async def test_native_handoff_still_checks_empty_body_and_self(broker, body, to):
    with pytest.raises(BrokerError, match="본문이 비어|자기 자신"):
        await broker.send(body, sender="planner", to=to)


def test_cli_wait_does_not_wait_for_native_handoff(broker, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_broker", lambda: broker)
    monkeypatch.setattr(broker, "wait", AsyncMock(side_effect=AssertionError("대기 금지")))
    assert cli.main(["send", "본문", "--from", "planner", "--to", "reviewer", "--wait"]) == 0
    assert '"native_required"' in capsys.readouterr().out
    assert not broker.recent()
