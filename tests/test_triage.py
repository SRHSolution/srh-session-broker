"""작업 중 도착 판단: 받는 창이 작업 중일 때 온 요청 — 취소·수정은 바로 넣고, 무관한 요청은 보낸 세션에 알린 뒤
지금 작업을 마치면(창이 쉬면) 순서대로 하나씩 넣는다. 받는 에이전트도 defer 로 미룰 수 있다."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from srhbroker import herdr as herdr_mod
from srhbroker import hooks, i18n, monitor
from srhbroker.models import BrokerError, TaskStatus
from tests.conftest import FakeJudge, make_broker
from tests.test_herdr import FakeHerdr


class InterruptJudge(FakeJudge):
    """Jev 대역: '바로 반영해야 한다' 확률을 본문에 따라 돌려주고, 받은 상태를 기록한다."""

    def __init__(self, p=0.1, by_word: dict[str, float] | None = None, error: Exception | None = None):
        super().__init__()
        self.p, self.by_word, self.err, self.states = p, by_word or {}, error, []

    async def interrupt(self, state):
        self.states.append(state)
        if self.err:
            raise self.err
        return next((v for w, v in self.by_word.items() if w in state["request"]), self.p)


def _broker(tmp_path, monkeypatch, judge=None):
    b = make_broker(tmp_path, judge=judge)
    b.register("lead", "claude", mode="interactive", native_id="sid-lead")
    b.register("dev", "codex", mode="interactive", native_id="sid-dev")
    b.register("web", "codex", mode="interactive", native_id="sid-web")
    b.herdr = FakeHerdr({"sid-lead": "idle", "sid-dev": "working", "sid-web": "idle"})
    b.router.tcfg["release_gap_s"] = 0
    spawned: list[str] = []
    monkeypatch.setattr(herdr_mod, "spawn_waiter", spawned.append)
    b.spawned = spawned
    return b


@pytest.fixture
def tb(tmp_path, monkeypatch):
    return _broker(tmp_path, monkeypatch)


def _route(b, task_id, router):
    return [r for r in b.store.route_log(500) if r["task_id"] == task_id and r["router"] == router]


async def test_unrelated_request_to_working_pane_is_deferred_and_sender_told(tb):
    r = await tb.send("회원가입 화면 문구 정리", sender="lead", to="dev")
    assert r["delivery"] == "deferred" and r["deferred"]["position"] == 1
    assert "지금 작업을 마친 뒤 순서대로" in r["delivery_note"] and "1번째" in r["delivery_note"]
    assert tb.herdr.prompts == [] and tb.store.get_task(r["task_id"]).status == TaskStatus.QUEUED
    assert tb.spawned == ["dev"]                                    # 창이 쉬면 넣어 줄 대기 프로세스
    assert _route(tb, r["task_id"], "defer")[0]["meta"]["by"] == "rules"
    r2 = await tb.send("README 오타 수정", sender="lead", to="dev")   # 수정 말이 있어도 lead 의 작업을 하는 중이 아니면 미룸
    assert r2["deferred"]["position"] == 2


async def test_cancel_or_urgent_words_go_in_right_away(tb):
    r = await tb.send("방금 보낸 배포 작업 취소해 주세요", sender="lead", to="dev")
    assert r["delivery"] == "delivered" and "작업 중에 새 메시지가 도착" in tb.herdr.prompts[-1][1]
    assert "defer(task_id=" in tb.herdr.prompts[-1][1]             # 무관하면 미루는 방법도 알려 준다
    assert _route(tb, r["task_id"], "triage")[0]["meta"]["rules"]


async def test_modify_words_go_in_when_receiver_works_on_that_senders_task(tb):
    tb.herdr.panes["sid-dev"] = "idle"
    first = await tb.send("로그인 API 구현", sender="lead", to="dev")
    assert first["delivery"] == "delivered"
    tb.herdr.panes["sid-dev"] = "working"
    r = await tb.send("응답 형식을 JSON 으로 변경해 주세요", sender="lead", to="dev")
    assert r["delivery"] == "delivered"                            # 지금 하는 lead 의 작업을 바꾸는 요청
    other = await tb.send("응답 형식 변경 검토", sender="lead", to="web")   # web 은 쉬는 중 → 판단 없이 바로
    assert other["delivery"] == "delivered" and not _route(tb, other["task_id"], "triage")


async def test_jev_decides_with_current_work_and_cancel_words_still_win(tmp_path, monkeypatch):
    j = InterruptJudge(p=0.1, by_word={"범위": 0.9})
    b = _broker(tmp_path, monkeypatch, judge=j)
    b.herdr.panes["sid-dev"] = "idle"
    await b.send("결제 모듈 리팩터링", sender="lead", to="dev")
    b.herdr.panes["sid-dev"] = "working"
    r = await b.send("결제 모듈은 환불 부분만 하도록 범위를 줄여 주세요", sender="lead", to="dev")
    assert r["delivery"] == "delivered"
    meta = _route(b, r["task_id"], "triage")[0]["meta"]
    assert meta["by"] == "jev" and meta["jev"] == 0.9
    assert j.states[-1]["current_work"][0]["title"] == "결제 모듈 리팩터링"        # 진행 중인 작업을 함께 보낸다
    r = await b.send("결제 모듈 변경 사항을 문서로도 남겨 주세요", sender="lead", to="dev")
    assert r["delivery"] == "deferred"                             # 규칙(수정 말)보다 Jev 판단 우선
    r = await b.send("결제 모듈 작업 중단해 주세요", sender="lead", to="dev")
    assert r["delivery"] == "delivered"                            # Jev 가 낮게 봐도 취소·중단은 바로


async def test_jev_failure_falls_back_to_rules(tmp_path, monkeypatch):
    b = _broker(tmp_path, monkeypatch, judge=InterruptJudge(error=TimeoutError("느림")))
    r = await b.send("새 기능 아이디어 정리", sender="lead", to="dev")
    meta = _route(b, r["task_id"], "defer")[0]["meta"]
    assert r["delivery"] == "deferred" and meta["by"] == "rules" and "TimeoutError" in meta["jev_error"]


async def test_deferred_requests_go_in_one_at_a_time_when_pane_rests(tb):
    a = await tb.send("작업 A", sender="lead", to="dev")
    n = await tb.send("참고: 빌드 캐시 비움", sender="lead", to="dev", kind="message")
    b2 = await tb.send("작업 B", sender="lead", to="dev")
    assert {a["delivery"], n["delivery"], b2["delivery"]} == {"deferred"}
    assert tb.deliver_now("dev") == "deferred" and tb.herdr.prompts == []   # 아직 작업 중

    tb.herdr.panes["sid-dev"] = "idle"
    assert tb.deliver_now("dev") == "delivered"
    text = tb.herdr.prompts[-1][1]
    assert "미뤄 두었던 요청" in text and a["task_id"] in text and n["task_id"] in text   # 알림은 함께, 작업은 하나
    assert b2["task_id"] not in text and "1건 더" in text
    assert tb.store.get_task(a["task_id"]).status == TaskStatus.DELIVERED

    tb.herdr.panes["sid-dev"] = "working"
    assert tb.deliver_now("dev") == "deferred"                     # A 를 하는 동안 B 는 기다린다
    tb.herdr.panes["sid-dev"] = "done"
    assert tb.deliver_now("dev") == "delivered" and b2["task_id"] in tb.herdr.prompts[-1][1]
    assert tb.store.deferred("dev") == [] and tb.deliver_now("dev") == "nothing"


async def test_release_waits_for_gap_after_last_push(tb):
    tb.router.tcfg["release_gap_s"] = 60
    await tb.send("작업 A", sender="lead", to="dev")
    tb.herdr.panes["sid-dev"] = "idle"
    tb.store.log_push("dev")                                       # 방금 넣었다 — 창이 아직 working 이 아닐 수 있다
    assert tb.deliver_now("dev") == "deferred"


async def test_replies_are_not_deferred(tb):
    tb.herdr.panes["sid-dev"] = "idle"
    r = await tb.send("dev 에게 맡긴 일", sender="dev", to="lead")   # dev 가 보낸 작업의 회신은 dev 에게 바로
    tb.herdr.panes["sid-dev"] = "working"
    tb.reply(r["task_id"], "끝났습니다", sender="lead")
    assert "끝났습니다" in tb.herdr.prompts[-1][1] and tb.herdr.prompts[-1][0] == "p-sid-dev"


async def test_agent_defer_requeues_and_tells_sender(tb):
    tb.herdr.panes["sid-dev"] = "idle"
    r = await tb.send("문서 사이트 빌드", sender="lead", to="dev")
    with pytest.raises(BrokerError):
        tb.defer(r["task_id"], sender="web")                       # 받은 세션만 미룰 수 있다
    out = tb.defer(r["task_id"], sender="dev", note="지금 결제 작업 중")
    assert out["deferred"]["position"] == 1 and tb.store.get_task(r["task_id"]).status == TaskStatus.QUEUED
    pane, text = tb.herdr.prompts[-1]
    assert pane == "p-sid-lead" and "순서대로 처리합니다 (대기 1번째)" in text and "지금 결제 작업 중" in text
    assert _route(tb, r["task_id"], "defer")[0]["meta"]["by"] == "agent"
    tb.herdr.panes["sid-dev"] = "working"
    assert tb.deliver_now("dev") == "deferred"
    tb.herdr.panes["sid-dev"] = "idle"
    assert tb.deliver_now("dev") == "delivered" and r["task_id"] in tb.herdr.prompts[-1][1]


async def test_stop_hook_releases_next_deferred_and_inbox_marks_it(tb):
    tb.herdr.available = lambda: True
    tb.herdr.panes["sid-lead"] = "working"
    a = await tb.send("작업 A", sender="dev", to="lead")
    b2 = await tb.send("작업 B", sender="dev", to="lead")
    assert a["delivery"] == b2["delivery"] == "deferred"
    seen = tb.inbox("lead", mark=False)["incoming"]
    assert all(x["deferred"] for x in seen)
    assert tb.inbox("lead", mark=True)["incoming"] == []           # 작업 중에 스스로 꺼내도 미룬 요청은 빠진다
    out = io.StringIO()
    tb.store.log_push = lambda name: None
    assert hooks.claude_stop(tb, "lead", io.StringIO("{}"), out) == 0   # 응답을 마친 순간 = 쉴 때
    reason = json.loads(out.getvalue())["reason"]
    assert "미뤄 두었던 요청" in reason and a["task_id"] in reason and b2["task_id"] not in reason


async def test_codex_direct_messages_are_triaged_by_rules_only(tmp_path, monkeypatch):
    j = InterruptJudge(p=0.99)
    b = _broker(tmp_path, monkeypatch, judge=j)
    r = await b.send("상품 목록 페이지네이션 추가", sender="web", to="dev")
    assert r["transport"] == "herdr-direct" and r["delivery"] == "deferred" and r["deferred"]["position"] == 1
    assert j.states == []                                          # 직접 전달은 Jev 로 보내지 않는다
    r = await b.send("방금 보낸 것 취소", sender="web", to="dev")
    assert r["delivery"] == "delivered"
    b.herdr.panes["sid-dev"] = "idle"
    assert b.deliver_now("dev") == "delivered" and "페이지네이션" in b.herdr.prompts[-1][1]


async def test_cancelled_deferred_request_leaves_the_queue(tb):
    r = await tb.send("작업 A", sender="lead", to="dev")
    tb.cancel(r["task_id"], by="lead")
    assert tb.store.deferred("dev") == []
    tb.herdr.panes["sid-dev"] = "idle"
    assert tb.deliver_now("dev") == "nothing"


async def test_triage_off_keeps_old_behaviour(tb):
    tb.router.tcfg["mode"] = "off"
    r = await tb.send("무관한 작업", sender="lead", to="dev")
    assert r["delivery"] == "delivered" and tb.store.deferred("dev") == []


async def test_overdue_reply_is_flagged_once_and_shown(tb):
    tb.herdr.panes["sid-dev"] = "idle"
    r = await tb.send("오래 걸리는 작업", sender="lead", to="dev")
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")
    with tb.store.tx() as c:
        c.execute("UPDATE tasks SET started_at=? WHERE id=?", (old, r["task_id"]))
    assert tb.flag_overdue() == [r["task_id"]] and tb.flag_overdue() == []
    assert "회신이 없습니다" in tb.herdr.prompts[-1][1] and tb.herdr.prompts[-1][0] == "p-sid-lead"
    snap = monitor.snapshot(tb)
    assert any(a["kind"] == "overdue" and a["task_id"] == r["task_id"] for a in snap["attention"])


async def test_timeline_and_dashboard_show_deferral(tb):
    r = await tb.send("작업 A", sender="lead", to="dev")
    flows = monitor.search_flows(tb)
    assert next(f for f in flows if f["id"] == r["task_id"])["deferred"] is True
    assert next(s for s in monitor.snapshot(tb)["sessions"] if s["name"] == "dev")["deferred"] == 1
    steps = [e["step"] for e in monitor.task_detail(tb, r["task_id"])["timeline"]]
    assert "미룸" in steps and monitor.task_detail(tb, r["task_id"])["deferred"] is True
    with i18n.use("en"):
        tl = monitor.task_detail(tb, r["task_id"])["timeline"]
    e = next(x for x in tl if x["step"] == "Deferred")
    assert "judged unrelated" in e["text"] and "rule: no matching words" in e["text"]


def test_jev_state_respects_send_body(tmp_path):
    b = make_broker(tmp_path)
    b.router.jcfg["send_body"] = False
    from srhbroker.models import Task, TaskKind
    t = Task(id="t_1", kind=TaskKind.TASK, from_addr="lead", to_addr="dev", body="비밀 본문", title="제목", deadline_s=60)
    cur = Task(id="t_0", kind=TaskKind.TASK, from_addr="lead", to_addr="dev", body="진행 본문", title="진행 중", deadline_s=60)
    st = b.router._state(t, None, [cur])
    assert st["request"] == "" and st["current_work"] == [{"title": "진행 중", "request": "", "sender": "lead"}]
