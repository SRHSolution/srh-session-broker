"""디스패처·회신·interactive 편지함·hook."""

import asyncio
import io
import json

from srhbroker.adapters.base import TurnResult
from srhbroker.dispatcher import Dispatcher
from srhbroker.hooks import claude_stop
from srhbroker.models import TaskStatus


class FakeAdapter:
    def __init__(self, provider, *, delay=0.0, text="완료", ok=True, error=None, on_run=None):
        self.provider = provider
        self.delay, self.text, self.ok, self.error, self.on_run = delay, text, ok, error, on_run
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def deliver(self, session, task, prompt, sandbox, *, env, timeout_s, is_cancelled):
        self.calls.append({"session": session.name, "task": task.id, "sandbox": sandbox.value, "env": env,
                           "prompt": prompt, "native_id": session.native_id})
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.on_run:
                await self.on_run(task)
            end = asyncio.get_running_loop().time() + self.delay
            while asyncio.get_running_loop().time() < end:
                if is_cancelled():
                    return TurnResult(ok=False, error="취소됨")
                await asyncio.sleep(0.01)
            return TurnResult(ok=self.ok, text=self.text, error=self.error, native_id=f"nid-{session.name}")
        finally:
            self.active -= 1


def _disp(broker, **kw):
    a = {"claude": FakeAdapter("claude", **kw), "codex": FakeAdapter("codex", **kw)}
    return Dispatcher(broker, adapters=a), a


async def test_worker_roundtrip_and_native_id(broker):
    d, a = _disp(broker, text='결과\n```json\n{"ok": true}\n```')
    r = await broker.send("구현", sender="planner", to="builder", reply_schema={"ok": "bool"})
    assert await d.tick() == 1
    await d.drain()
    st = broker.status(r["task_id"])
    assert st["status"] == "done" and st["result_json"] == {"ok": True}
    call = a["codex"].calls[0]
    assert call["env"]["SRHBROKER_SELF"] == "builder" and call["env"]["SRHBROKER_TASK"] == r["task_id"]
    assert "[SRH Broker]" in call["prompt"]
    assert broker.store.get_session("builder").native_id == "nid-builder"
    assert broker.store.get_session("builder").busy_task_id is None


async def test_per_session_serialization(broker):
    d, a = _disp(broker, delay=0.1)
    for i in range(3):
        await broker.send(f"작업{i}", sender="planner", to="builder")
    assert await d.tick() == 1          # 같은 세션은 한 번에 하나
    assert await d.tick() == 0
    await d.drain()
    assert await d.tick() == 1
    await d.drain()
    await d.tick()
    await d.drain()
    assert a["codex"].max_active == 1 and len(a["codex"].calls) == 3


async def test_session_sandbox_caps_task(broker):
    d, a = _disp(broker)
    await broker.send("리뷰", sender="builder", to="reviewer", sandbox="workspace-write")
    await d.tick()
    await d.drain()
    assert a["claude"].calls[0]["sandbox"] == "read-only"  # 세션 최대 권한이 상한


async def test_failure_and_timeout_status(broker):
    d, _ = _disp(broker, ok=False, error="시간 초과(1s)")
    r = await broker.send("x", sender="planner", to="builder")
    await d.tick()
    await d.drain()
    assert broker.status(r["task_id"])["status"] == "timeout"


async def test_cancel_running(broker):
    d, _ = _disp(broker, delay=5)
    r = await broker.send("오래 걸림", sender="planner", to="builder")
    await d.tick()
    await asyncio.sleep(0.05)
    broker.cancel(r["task_id"])
    await asyncio.wait_for(d.drain(), 2)
    assert broker.status(r["task_id"])["status"] == "cancelled"
    assert broker.store.get_session("builder").busy_task_id is None


async def test_explicit_reply_during_turn_wins(broker):
    async def on_run(task):
        broker.reply(task.id, "도구로 회신", sender="builder")
    d, _ = _disp(broker, text="최종 텍스트", on_run=on_run)
    r = await broker.send("x", sender="planner", to="builder")
    await d.tick()
    await d.drain()
    assert broker.status(r["task_id"])["result"] == "도구로 회신"


async def test_resume_on_reply_wakes_worker_sender(broker):
    d, a = _disp(broker)
    t0 = await broker.send("체인 시작", sender="user", to="builder")
    r = await broker.send("리뷰해줘", sender="builder", to="reviewer", resume_on_reply=True, parent_id=t0["task_id"])
    await d.tick()
    await d.drain()
    await d.tick()          # builder 는 t0 처리로 바빴다 → 이제 reply 통지 실행
    await d.drain()
    await d.tick()
    await d.drain()
    kinds = [broker.store.require_task(c["task"]).kind.value for c in a["codex"].calls]
    assert "reply" in kinds
    assert broker.store.require_task(r["task_id"]).reply_seen


async def test_recover_marks_stale_running(broker):
    r = await broker.send("x", sender="planner", to="builder")
    broker.store.try_claim_session("builder", r["task_id"])
    broker.store.transition(r["task_id"], (TaskStatus.QUEUED,), TaskStatus.RUNNING)
    d, _ = _disp(broker)
    d.recover()
    assert broker.status(r["task_id"])["status"] == "failed"
    assert broker.store.get_session("builder").busy_task_id is None


async def test_interactive_inbox_and_reply_flow(broker):
    r = await broker.send("설계 검토 부탁", sender="builder", to="planner")
    d, a = _disp(broker)
    assert await d.tick() == 0                      # interactive 는 디스패처가 실행하지 않는다
    box = broker.inbox("planner")
    assert [t["task_id"] for t in box["incoming"]] == [r["task_id"]]
    assert broker.status(r["task_id"])["status"] == "delivered"
    assert broker.inbox("planner")["incoming"] == []  # 두 번 전달되지 않는다
    broker.reply(r["task_id"], "검토 완료", sender="planner")
    replies = broker.inbox("builder")["replies"]
    assert replies and replies[0]["result"] == "검토 완료"
    assert broker.inbox("builder")["replies"] == []


async def test_reply_authorization(broker):
    import pytest
    from srhbroker.models import BrokerError
    r = await broker.send("x", sender="planner", to="builder")
    with pytest.raises(BrokerError, match="담당은 builder"):
        broker.reply(r["task_id"], "가로채기", sender="reviewer")


async def test_stop_hook_blocks_with_inbox(broker, monkeypatch):
    monkeypatch.delenv("SRHBROKER_TASK", raising=False)
    monkeypatch.setenv("SRHBROKER_SELF", "planner")
    r = await broker.send("질문 있어요", sender="builder", to="planner")
    out = io.StringIO()
    claude_stop(broker, None, stdin=io.StringIO(json.dumps({"session_id": "sid-1"})), stdout=out)
    res = json.loads(out.getvalue())
    assert res["decision"] == "block" and r["task_id"] in res["reason"] and "reply(" in res["reason"]
    assert broker.store.get_session("planner").native_id == "sid-1"
    out2 = io.StringIO()
    claude_stop(broker, None, stdin=io.StringIO("{}"), stdout=out2)
    assert out2.getvalue() == ""                    # 비어 있으면 아무것도 출력하지 않음 → 정상 종료


async def test_stop_hook_ignored_in_worker(broker, monkeypatch):
    monkeypatch.setenv("SRHBROKER_TASK", "t_x")
    await broker.send("질문", sender="builder", to="planner")
    out = io.StringIO()
    claude_stop(broker, "planner", stdin=io.StringIO("{}"), stdout=out)
    assert out.getvalue() == ""
    assert broker.inbox("planner", mark=False)["incoming"]  # 소비되지 않았다


async def test_wait_returns_on_completion(broker):
    r = await broker.send("x", sender="planner", to="builder")

    async def finish():
        await asyncio.sleep(0.1)
        broker.complete(r["task_id"], TaskStatus.DONE, result="끝")
    asyncio.create_task(finish())
    res = await broker.wait(r["task_id"], timeout_s=3, caller="planner", poll_s=0.02)
    assert res["status"] == "done" and broker.store.require_task(r["task_id"]).reply_seen
    r2 = await broker.send("y", sender="planner", to="builder")
    res2 = await broker.wait(r2["task_id"], timeout_s=0.05, poll_s=0.02)
    assert res2["waiting"] is True
