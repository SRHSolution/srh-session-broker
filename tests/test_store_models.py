import pytest

from srhbroker.models import BrokerError, Sandbox, TaskStatus, extract_mention, split_qualified, validate_name


def test_mention_extraction():
    assert extract_mention("@builder 구현해") == ("builder", "구현해")
    assert extract_mention("@codex:builder  구현") == ("codex:builder", "구현")
    assert extract_mention("이메일 a@b.com 확인") == (None, "이메일 a@b.com 확인")
    assert extract_mention("@B 대문자") == ("b", "대문자")


def test_name_rules():
    assert validate_name("Builder") == "builder"
    for bad in ("user", "codex", "a b", "", "x" * 40):
        with pytest.raises(BrokerError):
            validate_name(bad)
    assert split_qualified("codex:builder") == ("codex", "builder")
    assert split_qualified("builder") == (None, "builder")


def test_sandbox_narrower():
    assert Sandbox.narrower(None, Sandbox.WORKSPACE_WRITE) == Sandbox.WORKSPACE_WRITE
    assert Sandbox.narrower(Sandbox.WORKSPACE_WRITE, Sandbox.READ_ONLY) == Sandbox.READ_ONLY


def test_names_and_aliases_globally_unique(broker):
    with pytest.raises(BrokerError, match="이미 codex"):
        broker.register("builder", "claude")          # 이름은 provider 와 무관하게 유일
    with pytest.raises(BrokerError, match="이미 세션"):
        broker.register("other", "claude", aliases=["b"])  # 별칭 중복
    with pytest.raises(BrokerError, match="별칭과 겹칩니다"):
        broker.register("rev", "codex")               # 이름이 다른 세션의 별칭과 충돌
    with pytest.raises(BrokerError, match="정의되지 않은 역할"):
        broker.register("x", "codex", roles=["nope"])


def test_resolve_name_alias_qualified(broker):
    s, how = broker.store.resolve_name("b")
    assert (s.name, how) == ("builder", "alias")
    s, how = broker.store.resolve_name("codex:builder")
    assert (s.name, how) == ("builder", "name")
    with pytest.raises(BrokerError, match="codex 세션"):
        broker.store.resolve_name("claude:builder")
    assert broker.store.resolve_name("nobody") is None


def test_session_claim_is_exclusive(broker):
    st = broker.store
    assert st.try_claim_session("builder", "t1")
    assert not st.try_claim_session("builder", "t2")
    st.release_session("builder", "t2")               # 다른 작업 ID 로는 해제 불가
    assert not st.try_claim_session("builder", "t3")
    st.release_session("builder", "t1")
    assert st.try_claim_session("builder", "t3")


async def test_transition_is_cas(broker):
    r = await broker.send("구현", sender="planner", to="builder")
    st = broker.store
    assert st.transition(r["task_id"], (TaskStatus.QUEUED,), TaskStatus.RUNNING)
    assert not st.transition(r["task_id"], (TaskStatus.QUEUED,), TaskStatus.RUNNING)
    assert st.transition(r["task_id"], (TaskStatus.RUNNING,), TaskStatus.DONE)
    assert st.require_task(r["task_id"]).finished_at
