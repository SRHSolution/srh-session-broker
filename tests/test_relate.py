"""대기 요청과의 관계: 같은 보낸 세션이 아직 넣지 않은 요청이 있을 때 뒤에 온 메시지가 그것을 취소·대체·수정하는지 판단한다.
Jev 가 확신(0.8 이상, 대상 분명)하면 넣기 전에 취소하고, 수정이거나 확신이 부족하면 대상과 묶어 함께 넣는다."""

from __future__ import annotations

import pytest

from srhbroker import i18n, monitor
from srhbroker.models import TaskStatus
from tests.test_triage import InterruptJudge, _broker, _route


class RelateJudge(InterruptJudge):
    def __init__(self, relation=None, target=None, *, p=0.1, priority=None, by_word=None):
        super().__init__(p=p, by_word=by_word)
        self.relation, self.target, self.priority, self.relate_calls = relation or {}, target or {}, priority, []

    async def interrupt(self, state):
        p = await super().interrupt(state)
        return {"interrupt": p, "priority": (self.priority or {}).get(state["title"])} if self.priority else p

    async def relate(self, state, targets):
        self.relate_calls.append((state, targets))
        rel = next((v for w, v in self.relation.items() if w in state["request"]), {"new": 0.9})
        return {"relation": rel, "target": self.target}


def _b(tmp_path, monkeypatch, **kw):
    return _broker(tmp_path, monkeypatch, judge=RelateJudge(**kw))


async def test_confident_cancel_removes_waiting_request_before_delivery(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, relation={"취소": {"cancel": 0.95, "new": 0.05}})
    a = await b.send("회원가입 화면 문구 정리", sender="lead", to="dev")
    assert a["delivery"] == "deferred"
    r = await b.send("아까 보낸 문구 정리는 취소해 주세요", sender="lead", to="dev")
    assert r["delivery"] == "not-needed" and r["relation"]["applied"] == "cancelled" and r["relation"]["target"] == a["task_id"]
    assert "넣기 전에 취소" in r["next"]
    assert b.store.get_task(a["task_id"]).status == TaskStatus.CANCELLED
    assert b.store.get_task(r["task_id"]).status == TaskStatus.DONE and b.herdr.prompts == []   # 받는 쪽은 둘 다 모른다
    assert b.store.deferred("dev") == []
    state, targets = b.judge_calls = b.router.judge.relate_calls[-1]
    assert state["pending_requests"][0]["id"] == a["task_id"] and a["task_id"] in targets
    texts = [e["text"] for e in monitor.task_detail(b, a["task_id"])["timeline"]]
    assert any("넣기 전에 취소" in x and r["task_id"] in x for x in texts)


async def test_unsure_cancel_is_bundled_and_goes_in_with_its_target(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, relation={"취소": {"cancel": 0.6, "new": 0.4}})
    a = await b.send("회원가입 화면 문구 정리", sender="lead", to="dev")
    x = await b.send("다른 세션 작업", sender="web", to="dev")      # 다른 세션의 대기 요청 (관계 판단 대상 아님, 직접 전달)
    r = await b.send("문구 정리 취소해 주세요", sender="lead", to="dev")
    assert r["relation"]["applied"] == "bundled" and r["delivery"] == "deferred"
    assert b.herdr.prompts == []                                   # '취소' 말이 있어도 대상이 아직 안 들어갔으니 지금 넣지 않는다
    assert b.store.get_task(a["task_id"]).status == TaskStatus.QUEUED
    b.herdr.panes["sid-dev"] = "idle"
    assert b.deliver_now("dev") == "delivered"
    text = b.herdr.prompts[-1][1]
    assert a["task_id"] in text and r["task_id"] in text and x["direct_id"] not in text   # 대상과 함께, 다음 것은 다음에
    assert "취소하려는 것으로 보입니다" in text and text.index(a["task_id"]) < text.index(r["task_id"])


async def test_modify_is_bundled_with_its_target(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, relation={"JSON": {"modify": 0.9, "new": 0.1}})
    a = await b.send("주문 내역 내보내기", sender="lead", to="dev")
    r = await b.send("내보내기 형식은 CSV 말고 JSON 으로 해 주세요", sender="lead", to="dev")
    assert r["relation"]["applied"] == "bundled" and r["relation"]["relation"] == "modify"
    b.herdr.panes["sid-dev"] = "idle"
    b.deliver_now("dev")
    assert "고치는 내용입니다" in b.herdr.prompts[-1][1] and a["task_id"] in b.herdr.prompts[-1][1]


async def test_replace_cancels_target_and_sends_new_request(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, relation={"대신": {"replace": 0.9, "new": 0.1}})
    a = await b.send("주문 내역 내보내기", sender="lead", to="dev")
    r = await b.send("내보내기는 취소하고 대신 주문 통계 화면을 만들어 주세요", sender="lead", to="dev")
    assert r["relation"]["applied"] == "replaced" and b.store.get_task(a["task_id"]).status == TaskStatus.CANCELLED
    assert r["delivery"] == "deferred" and b.store.get_task(r["task_id"]).status == TaskStatus.QUEUED  # 새 요청은 정상 처리


async def test_unclear_target_among_several_is_not_cancelled(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, relation={"취소": {"cancel": 0.95}})
    a = await b.send("작업 A", sender="lead", to="dev")
    c = await b.send("작업 C", sender="lead", to="dev")
    b.router.judge.target = {a["task_id"]: 0.5, c["task_id"]: 0.45}
    r = await b.send("그거 취소해 주세요", sender="lead", to="dev")
    assert r["relation"]["applied"] == "bundled"
    assert {b.store.get_task(i).status for i in (a["task_id"], c["task_id"])} == {TaskStatus.QUEUED}


async def test_vague_reference_among_several_needs_title_words_to_cancel(tmp_path, monkeypatch):
    """대기 요청이 여럿이면 Jev 가 대상을 확신해도 메시지에 그 요청의 제목 낱말이 있어야 취소한다 (실측: '그거 취소' → 0.98)."""
    b = _b(tmp_path, monkeypatch, relation={"취소": {"cancel": 0.98}})
    a = await b.send("회원가입 문구 정리", sender="lead", to="dev")
    c = await b.send("주문 내역 내보내기", sender="lead", to="dev")
    b.router.judge.target = {a["task_id"]: 0.95, c["task_id"]: 0.05}
    r = await b.send("그거 취소해 주세요", sender="lead", to="dev")
    assert r["relation"]["applied"] == "bundled" and b.store.get_task(a["task_id"]).status == TaskStatus.QUEUED
    r = await b.send("회원가입 문구는 취소해 주세요", sender="lead", to="dev")
    assert r["relation"]["applied"] == "cancelled" and b.store.get_task(a["task_id"]).status == TaskStatus.CANCELLED
    assert b.store.get_task(c["task_id"]).status == TaskStatus.QUEUED


async def test_rules_without_jev_never_cancel(tb):
    a = await tb.send("회원가입 화면 문구 정리", sender="lead", to="dev")
    r = await tb.send("회원가입 문구 작업은 취소", sender="lead", to="dev")
    assert r["relation"]["applied"] == "bundled" and r["relation"]["by"] == "rules"
    assert tb.store.get_task(a["task_id"]).status == TaskStatus.QUEUED
    r2 = await tb.send("README 오타 수정", sender="lead", to="dev")   # 제목 낱말도 지칭어도 없으면 새 요청
    assert "relation" not in r2 and r2["delivery"] == "deferred"


async def test_urgent_new_request_goes_first_among_deferred(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, priority={"결제 장애 원인 확인": 0.9})
    a = await b.send("문서 정리", sender="lead", to="dev")
    u = await b.send("결제 장애 원인 확인", sender="lead", to="dev")
    assert u["deferred"]["position"] == 1 and b._defer_position("dev", a["task_id"]) == 2
    steps = monitor.task_detail(b, u["task_id"])["timeline"]
    assert any("맨 앞" in e["text"] for e in steps)
    b.herdr.panes["sid-dev"] = "idle"
    b.deliver_now("dev")
    assert u["task_id"] in b.herdr.prompts[-1][1] and a["task_id"] not in b.herdr.prompts[-1][1]


async def test_waiting_for_closed_pane_is_bundled_too(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, relation={"JSON": {"modify": 0.9}})
    del b.herdr.panes["sid-dev"]                                   # 창이 닫혀 있어 기다리는 요청
    a = await b.send("주문 내역 내보내기", sender="lead", to="dev")
    r = await b.send("형식은 JSON 으로", sender="lead", to="dev")
    assert r["relation"]["applied"] == "bundled" and b.store.deferred("dev") == []
    b.herdr.panes["sid-dev"] = "idle"
    assert b.deliver_now("dev") == "delivered"
    text = b.herdr.prompts[-1][1]
    assert a["task_id"] in text and "고치는 내용입니다" in text


async def test_new_relation_is_logged_but_not_applied(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch)
    await b.send("작업 A", sender="lead", to="dev")
    r = await b.send("작업 B", sender="lead", to="dev")
    assert "relation" not in r and _route(b, r["task_id"], "relate")[0]["applied"] == 0
    with i18n.use("en"):
        assert any("unrelated to the waiting" in e["text"] for e in monitor.task_detail(b, r["task_id"])["timeline"])


async def test_relate_off(tmp_path, monkeypatch):
    b = _b(tmp_path, monkeypatch, relation={"취소": {"cancel": 0.99}})
    b.router.tcfg["relate"] = False
    a = await b.send("작업 A", sender="lead", to="dev")
    await b.send("작업 A 취소", sender="lead", to="dev")
    assert b.store.get_task(a["task_id"]).status == TaskStatus.QUEUED and not b.router.judge.relate_calls


@pytest.fixture
def tb(tmp_path, monkeypatch):
    return _broker(tmp_path, monkeypatch)
