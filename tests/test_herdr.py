"""herdr 즉시 전달: 받는 세션이 herdr 창에서 쉬고 있으면 받은 메시지를 프롬프트로 바로 넣는다."""

from __future__ import annotations

import pytest

from srhbroker import herdr as herdr_mod
from srhbroker.models import TaskStatus
from tests.conftest import make_broker


class FakeHerdr:
    """herdr CLI 대역. 창(세션 ID → 상태)과 넣은 프롬프트를 기록한다."""

    def __init__(self, panes: dict[str, str] | None = None, prompt_ok: bool = True):
        self.panes = panes or {}          # native_id → agent_status
        self.prompts: list[tuple[str, str]] = []
        self.prompt_ok = prompt_ok

    def available(self) -> bool:
        return True

    def find(self, native_id):
        st = self.panes.get(native_id)
        return {"pane_id": f"p-{native_id}", "agent_status": st} if st else None

    def at_menu(self, pane_id):
        return False

    def agents(self):
        return [{"pane_id": f"p-{sid}", "agent_status": st, "agent_session": {"value": sid}}
                for sid, st in self.panes.items()]

    def prompt(self, pane_id, text):
        self.prompts.append((pane_id, text))
        return self.prompt_ok


@pytest.fixture
def hb(tmp_path, monkeypatch):
    b = make_broker(tmp_path)
    b.register("lead", "claude", mode="interactive", native_id="sid-lead")
    b.register("dev", "codex", mode="interactive", native_id="sid-dev")
    b.herdr = FakeHerdr({"sid-lead": "idle", "sid-dev": "idle"})
    spawned: list[str] = []
    monkeypatch.setattr(herdr_mod, "spawn_waiter", spawned.append)
    b.spawned = spawned
    return b


async def test_send_pushes_to_idle_pane_and_reply_comes_back(hb):
    r = await hb.send("로그 확인해줘", sender="lead", to="dev")
    assert r["delivery"] == "delivered"
    pane, text = hb.herdr.prompts[-1]
    assert pane == "p-sid-dev" and r["task_id"] in text and "reply(task_id=" in text
    assert hb.store.get_task(r["task_id"]).status == TaskStatus.DELIVERED   # 선점됨 → Stop hook 이 다시 넣지 않음

    hb.reply(r["task_id"], "이상 없음", sender="dev")                       # 회신은 보낸 쪽 창으로 바로
    pane, text = hb.herdr.prompts[-1]
    assert pane == "p-sid-lead" and "이상 없음" in text and "다시 send 하지 마세요" in text
    assert hb.inbox("lead", mark=False)["replies"] == []


async def test_working_pane_gets_message_mid_turn(hb):
    """작업 중이어도 바로 넣는다 (Claude: 턴 중 반영, Codex: steer). 안내문이 '작업 중 도착'으로 바뀐다."""
    hb.herdr.panes["sid-dev"] = "working"
    r = await hb.send("방금 작업 취소하고 이걸 해", sender="lead", to="dev")
    assert r["delivery"] == "delivered" and hb.spawned == []
    assert "작업 중에 새 메시지가 도착" in hb.herdr.prompts[-1][1]


async def test_working_waits_when_disabled(hb):
    hb.cfg.data["herdr"]["push_while_working"] = False
    hb.herdr.panes["sid-dev"] = "working"
    r = await hb.send("작업", sender="lead", to="dev")
    assert r["delivery"] == "waiting" and hb.spawned == ["dev"] and hb.herdr.prompts == []


async def test_blocked_pane_spawns_waiter_and_keeps_inbox(hb):
    """승인·질문 창이 떠 있으면(blocked) 넣지 않는다 — Enter 가 선택지를 고르게 된다."""
    hb.herdr.panes["sid-dev"] = "blocked"
    r = await hb.send("작업", sender="lead", to="dev")
    assert r["delivery"] == "waiting" and hb.spawned == ["dev"] and hb.herdr.prompts == []
    assert hb.store.get_task(r["task_id"]).status == TaskStatus.QUEUED       # Stop hook·대기 프로세스가 넣을 수 있게 남김
    hb.herdr.panes["sid-dev"] = "done"
    assert hb.deliver_now("dev") == "delivered"
    assert hb.deliver_now("dev") == "nothing"


async def test_cancel_of_delivered_task_notifies_receiver(hb):
    r = await hb.send("오래 걸리는 시험", sender="lead", to="dev")
    assert hb.store.get_task(r["task_id"]).status == TaskStatus.DELIVERED
    out = hb.cancel(r["task_id"])
    assert out["status"] == "cancelled" and out["delivery"] == "delivered"
    text = hb.herdr.prompts[-1][1]
    assert hb.herdr.prompts[-1][0] == "p-sid-dev" and r["task_id"] in text and "중단" in text
    # 아직 전달 전(queued)이면 받는 쪽은 모르므로 알리지 않는다
    hb.herdr.panes["sid-dev"] = "blocked"
    r2 = await hb.send("다른 작업", sender="lead", to="dev")
    n = len(hb.herdr.prompts)
    assert "delivery" not in hb.cancel(r2["task_id"]) and len(hb.herdr.prompts) == n


async def test_failed_prompt_is_unclaimed(hb):
    hb.herdr.prompt_ok = False
    r = await hb.send("작업", sender="lead", to="dev")
    assert r["delivery"] == "failed"
    assert hb.store.get_task(r["task_id"]).status == TaskStatus.QUEUED
    assert hb.inbox("dev", mark=False)["incoming"]


async def test_no_pane_or_not_herdr_falls_back_to_inbox(hb):
    hb.herdr.panes.pop("sid-dev")
    assert (await hb.send("a", sender="lead", to="dev"))["delivery"] == "no-pane"
    hb.herdr = herdr_mod.Herdr({})                     # HERDR_ENV 없음 (conftest)
    assert (await hb.send("b", sender="lead", to="dev"))["delivery"] == "not-herdr"
    assert len(hb.inbox("dev", mark=False)["incoming"]) == 2


async def test_rate_limit_stops_ping_pong(tmp_path, monkeypatch):
    b = make_broker(tmp_path)
    b.cfg.data["herdr"]["max_push_per_10min"] = 2
    b.register("lead", "claude", mode="interactive", native_id="sid-lead")
    b.register("dev", "codex", mode="interactive", native_id="sid-dev")
    b.herdr = FakeHerdr({"sid-dev": "idle"})
    got = [(await b.send(f"m{i}", sender="lead", to="dev"))["delivery"] for i in range(3)]
    assert got == ["delivered", "delivered", "rate-limited"]


async def test_long_body_is_truncated_with_status_hint(hb):
    hb.cfg.data["herdr"]["max_push_chars"] = 500
    r = await hb.send("가" * 3000, sender="lead", to="dev")
    text = hb.herdr.prompts[-1][1]
    assert len(text) < 1500 and f"status(task_id='{r['task_id']}')" in text


def test_herdr_finds_pane_by_session_id(monkeypatch):
    h = herdr_mod.Herdr({})
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(h, "binary", lambda: "herdr")
    monkeypatch.setattr(h, "_run", lambda *a, **k: {"result": {"agents": [
        {"pane_id": "w4:p1", "agent_status": "done", "agent_session": {"value": "b83f"}},
        {"pane_id": "w4:pB", "agent_status": "working", "agent_session": {"value": "01a0"}}]}})
    assert h.available() and h.find("01a0")["pane_id"] == "w4:pB" and h.find("nope") is None


@pytest.mark.parametrize("screen, menu", [
    # Codex 업데이트 안내 (실제로 Enter 가 '지금 업데이트' 를 선택했던 화면)
    ("  Update available! 0.159.3 -> 0.160.0\n› 1. Update now (runs `npm install -g @openai/codex`)\n  2. Skip\n", True),
    ("Do you want to proceed?\n❯ 1. Yes\n  2. No\n", True),
    # 평소 입력칸 — 위쪽 기록에 '› 1. …' 이 있어도 맨 아래 표시줄은 입력칸
    ("› 1. 첫째 항목을 고쳐줘\n• 고쳤습니다.\n\n› Ask Codex to do anything\n  GPT-6.1-Sol low\n", False),
    ("● 완료\n❯ \n  ⏸ manual mode on\n", False),
    ("", False),
])
def test_menu_detection(screen, menu):
    assert herdr_mod.looks_like_menu(screen) is menu


async def test_menu_on_screen_is_treated_as_busy(hb):
    hb.herdr.panes["sid-dev"] = "working"
    hb.herdr.at_menu = lambda pane: True
    r = await hb.send("작업", sender="lead", to="dev")
    assert r["delivery"] == "waiting" and hb.herdr.prompts == []


# ── 알림(message)은 받는 창이 닫혀 있으면 기다리지 않고 소멸 ─────────────────

async def test_notice_to_closed_pane_is_dropped_but_task_waits(hb):
    from srhbroker import monitor
    hb.herdr.panes.pop("sid-dev")
    n = await hb.send("HW 시험 창 종료 — 재개 가능", sender="lead", to="dev", kind="message")
    assert (n["status"], n["delivery"]) == ("cancelled", "dropped") and "버렸습니다" in n["delivery_note"]
    t = hb.store.get_task(n["task_id"])
    assert t.status == TaskStatus.CANCELLED and "닫혀" in t.error
    tl = {e["step"]: e["text"] for e in monitor.task_detail(hb, n["task_id"])["timeline"]}
    assert "소멸" in tl and "dev 창이 닫혀" in tl["소멸"] and "취소" not in tl
    w = await hb.send("로그 확인해줘", sender="lead", to="dev")          # 작업은 창이 열릴 때까지 대기
    assert (w["status"], w["delivery"]) == ("queued", "no-pane")
    hb.herdr.panes["sid-dev"] = "idle"
    assert hb.deliver_now("dev") == "delivered" and n["task_id"] not in hb.herdr.prompts[-1][1]


async def test_waiting_notice_is_dropped_when_pane_closes(hb):
    """승인 창이라 기다리던 알림도, 그 사이 창이 닫히면 (데몬·대기 프로세스가 볼 때) 소멸한다."""
    hb.herdr.panes["sid-dev"] = "blocked"
    n = await hb.send("참고만", sender="lead", to="dev", kind="message")
    assert n["delivery"] == "waiting" and hb.store.get_task(n["task_id"]).status == TaskStatus.QUEUED
    hb.herdr.panes.pop("sid-dev")
    assert hb.deliver_now("dev") == "dropped"
    assert hb.store.get_task(n["task_id"]).status == TaskStatus.CANCELLED


async def test_notice_kept_when_herdr_unavailable_or_disabled(hb):
    hb.herdr.panes.pop("sid-dev")
    hb.cfg.data["herdr"]["drop_notices_when_closed"] = False
    n = await hb.send("참고만", sender="lead", to="dev", kind="message")
    assert (n["status"], n["delivery"]) == ("queued", "no-pane")
    hb.cfg.data["herdr"]["drop_notices_when_closed"] = True
    hb.herdr.available = lambda: False                                 # herdr 밖: 창 상태를 모르면 버리지 않는다
    m = await hb.send("참고만 2", sender="lead", to="dev", kind="message")
    assert (m["status"], m["delivery"]) == ("queued", "not-herdr")


async def test_held_notice_approved_after_pane_closed_is_dropped(hb):
    hb.herdr.panes.pop("sid-dev")
    from srhbroker.models import Task, TaskKind, new_task_id
    t = Task(id=new_task_id(), kind=TaskKind.MESSAGE, from_addr="lead", to_addr="dev", body="예전 규칙으로 보류된 알림",
             title="알림", status=TaskStatus.HELD)
    hb.store.insert_task(t)
    r = hb.approve(t.id, via="cli")
    assert (r["status"], r["delivery"]) == ("cancelled", "dropped")


# ── 같은 provider 직접 전달 (Codex ↔ Codex) ─────────────────────────────

@pytest.fixture
def cb(tmp_path, monkeypatch):
    """Codex 2개 + Claude 2개, 모두 herdr 창에서 쉬는 중."""
    b = make_broker(tmp_path)
    for name, prov in (("cx-a", "codex"), ("cx-b", "codex"), ("cl-a", "claude"), ("cl-b", "claude")):
        b.register(name, prov, mode="interactive", native_id=f"sid-{name}")
    b.herdr = FakeHerdr({f"sid-{n}": "idle" for n in ("cx-a", "cx-b", "cl-a", "cl-b")})
    monkeypatch.setattr(herdr_mod, "spawn_waiter", lambda name: None)
    return b


async def test_codex_to_codex_is_pushed_directly_without_task(cb):
    r = await cb.send("빌드 로그 봐줘", sender="cx-a", to="cx-b", title="로그", acceptance=["원인 한 줄"])
    assert (r["status"], r["transport"], r["delivered"], r["task_id"]) == ("delivered", "herdr-direct", True, None)
    assert r["direct_id"].startswith("d_")
    pane, text = cb.herdr.prompts[-1]
    assert pane == "p-sid-cx-b" and "보낸 세션: cx-a" in text and "빌드 로그 봐줘" in text
    assert "원인 한 줄" in text and "send(to='cx-a'" in text           # 회신 방법 안내
    assert cb.store.list_tasks() == [] and cb.store.route_log() == []  # broker 작업·라우팅 기록 없음
    assert cb.store.get_direct(r["direct_id"])["status"] == "delivered"


async def test_codex_direct_waits_when_pane_closed_then_delivers(cb):
    cb.herdr.panes["sid-cx-b"] = "working"
    r = await cb.send("방금 건 취소", sender="cx-a", to="cx-b", kind="message")
    assert r["delivered"] and "작업 중에 새 메시지" in cb.herdr.prompts[-1][1]
    cb.herdr.panes.pop("sid-cx-b")                                     # 창 닫힘 → 대기열
    r = await cb.send("다시", sender="cx-a", to="cx-b")
    assert (r["status"], r["delivery"], r["delivered"]) == ("queued", "no-pane", False)
    assert "창을 열어" in r["delivery_note"] and r["direct_id"] in r["next"]
    assert cb.store.list_tasks() == []
    assert [x["task_id"] for x in cb.recent(mine="cx-a", active=True)] == [r["direct_id"]]
    n = len(cb.herdr.prompts)
    cb.herdr.panes["sid-cx-b"] = "idle"                                # 창이 열리면 (데몬·대기 프로세스가) 넣는다
    assert cb.deliver_now("cx-b") == "delivered" and len(cb.herdr.prompts) == n + 1
    assert r["direct_id"] in cb.herdr.prompts[-1][1]
    assert cb.deliver_now("cx-b") == "nothing"


async def test_codex_direct_notice_to_closed_pane_is_dropped(cb):
    from srhbroker import monitor
    cb.herdr.panes.pop("sid-cx-b")
    r = await cb.send("빌드 끝났습니다", sender="cx-a", to="cx-b", kind="message")
    assert (r["status"], r["delivery"], r["delivered"]) == ("dropped", "dropped", False)
    assert cb.store.get_direct(r["direct_id"])["status"] == "dropped" and "버렸습니다" in r["delivery_note"]
    assert monitor.task_detail(cb, r["direct_id"])["timeline"][-1]["step"] == "소멸"
    assert [x["task_id"] for x in cb.recent(mine="cx-a", active=True)] == []
    rows = monitor.search_flows(cb, status="cancelled")
    assert r["direct_id"] in [x["id"] for x in rows]


async def test_codex_direct_cancel_and_expire(cb):
    cb.herdr.panes.pop("sid-cx-b")
    r1 = await cb.send("취소할 것", sender="cx-a", to="cx-b")
    r2 = await cb.send("만료될 것", sender="cx-a", to="cx-b")
    from srhbroker.models import BrokerError
    with pytest.raises(BrokerError, match="보낸 세션이나 사용자"):
        cb.cancel(r1["direct_id"], by="cx-b")
    assert cb.cancel(r1["direct_id"], by="cx-a")["status"] == "cancelled"
    with pytest.raises(BrokerError, match="이미 cancelled"):
        cb.cancel(r1["direct_id"], by="user")
    with cb.store.tx() as c:
        c.execute("UPDATE direct_queue SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?", (r2["direct_id"],))
    assert cb.expire_stale() == [r2["direct_id"]]
    cb.herdr.panes["sid-cx-b"] = "idle"
    assert cb.deliver_now("cx-b") == "nothing"                         # 취소·만료된 것은 넣지 않는다


async def test_claude_same_provider_stays_native_and_multi_target_is_handoff(cb):
    r = await cb.send("검토", sender="cl-a", to="cl-b")
    assert r["status"] == "native_required" and "SendMessage" in r["next"]
    r = await cb.send("누가 좀", sender="cx-a", to="codex")            # 다른 Codex 가 하나뿐이면 그 창에 넣는다
    assert r["status"] == "delivered" and r["to"] == "cx-b"
    cb.register("cx-c", "codex", mode="interactive", native_id="sid-cx-c")
    r = await cb.send("누가 좀", sender="cx-a", to="codex")            # 여럿이면 고르지 않는다
    assert r["status"] == "native_required" and {t["name"] for t in r["targets"]} == {"cx-b", "cx-c"}
    assert all(p[0] != "p-sid-cl-b" for p in cb.herdr.prompts)


# ── herdr 창·세션 짝 검증 ────────────────────────────────────────────────

@pytest.mark.parametrize("agent, ok", [
    ({"agent": "claude", "cwd": "D:/Proj/A/"}, True),
    ({"agent": "claude", "cwd": "d:/proj/a"}, True),
    ({"agent": "codex", "cwd": "D:/Proj/A"}, False),                # 종류가 다르면 다른 창
    ({"agent": "claude", "cwd": "D:/Proj/B"}, False),               # 폴더가 다르면 다른 창
    ({"agent": "claude"}, True),                                       # 폴더를 모르면 종류만
])
def test_same_pane(agent, ok):
    assert herdr_mod.same_pane(agent, "claude", "D:/Proj/A") is ok


async def test_pane_mismatch_is_not_pushed(hb):
    real_find = hb.herdr.find
    hb.herdr.find = lambda sid: {**real_find(sid), "agent": "claude"} if real_find(sid) else None  # dev 는 codex
    r = await hb.send("작업", sender="lead", to="dev")
    assert r["delivery"] == "pane-mismatch" and hb.herdr.prompts == []
    assert "다시 열어" in r["delivery_note"]
    assert hb.inbox("dev", mark=False)["incoming"]                     # inbox 에는 남는다


async def test_no_pane_delivery_note(hb):
    hb.herdr.panes.pop("sid-dev")
    r = await hb.send("작업", sender="lead", to="dev")
    assert r["delivery"] == "no-pane" and "창을 열어" in r["delivery_note"]


def test_worker_env_drops_herdr_vars(monkeypatch):
    from srhbroker.adapters.base import child_env
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("HERDR_ENV", "1")
    env = child_env({"SRHBROKER_SELF": "w"})
    assert "HERDR_PANE_ID" not in env and "HERDR_ENV" not in env and env["SRHBROKER_SELF"] == "w"


# ── 오래된 대기 작업 만료 · status ───────────────────────────────────────

async def test_expire_stale_times_out_waiting_tasks_and_tells_sender(hb):
    from srhbroker.models import TaskStatus as TS
    r_old = await hb.send("오래된 승인 대기", sender="lead", to="dev")
    r_new = await hb.send("방금 보낸 것", sender="lead", to="dev")
    for tid in (r_old["task_id"], r_new["task_id"]):
        hb.store.transition(tid, (TS.QUEUED, TS.DELIVERED), TS.HELD)
    with hb.store.tx() as c:
        c.execute("UPDATE tasks SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?", (r_old["task_id"],))
    n = len(hb.herdr.prompts)
    assert hb.expire_stale() == [r_old["task_id"]]
    assert hb.store.get_task(r_old["task_id"]).status == TS.TIMEOUT
    assert hb.store.get_task(r_new["task_id"]).status == TS.HELD
    assert len(hb.herdr.prompts) == n + 1 and "timeout" in hb.herdr.prompts[-1][1]   # 보낸 쪽 창에 결과 알림


def test_status_command_runs_read_only(tmp_path, monkeypatch, capsys):
    from srhbroker import cli
    monkeypatch.setenv("SRHBROKER_HOME", str(tmp_path))
    b = make_broker(tmp_path)
    b.register("lead", "claude", mode="interactive", native_id="sid-lead")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "데몬: 실행 중 아님" in out and "lead" in out and "사용 불가" in out
