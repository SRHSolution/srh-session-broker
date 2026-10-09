"""라우팅 게이트: G1 대상 해석 · G2 확신도 · G3 안전 · G4 루프."""

import pytest

from srhbroker.models import BrokerError
from tests.conftest import FakeJudge, make_broker


def _setup(tmp_path, **kw):
    b = make_broker(tmp_path, **kw)
    b.register("planner", "claude", mode="interactive", roles=["planner"])
    b.register("builder", "codex", roles=["builder"], aliases=["b"])
    b.register("builder2", "claude", roles=["builder"])
    b.register("reviewer", "claude", roles=["reviewer"], sandbox="read-only")
    return b


async def test_explicit_name_skips_jev_routing(tmp_path):
    j = FakeJudge(probs={"reviewer": 1.0})
    b = _setup(tmp_path, judge=j)
    r = await b.send("화면 정렬 수정", sender="planner", to="b")
    assert (r["to"], r["routed_by"], r["status"]) == ("builder", "alias", "queued")
    # 명시 지정이어도 안전 판단은 수행하지만 route 질문은 하지 않는다
    assert j.calls and j.calls[0]["ask_route"] is False and j.calls[0]["ask_safety"] is True


async def test_mention_in_body(tmp_path):
    b = _setup(tmp_path)
    r = await b.send("@reviewer 이 diff 봐줘", sender="builder")
    assert r["to"] == "reviewer"
    assert b.store.require_task(r["task_id"]).body == "이 diff 봐줘"


async def test_role_hint_picks_idle_preferred_instance(tmp_path):
    b = _setup(tmp_path)
    r = await b.send("구현", sender="planner", to="builder")   # 'builder' 는 세션 이름이므로 이름 우선
    assert r["routed_by"] == "name"
    b.store.remove_session("builder")
    b.register("cx", "codex", roles=["builder"])
    r = await b.send("구현", sender="planner", to="builder")   # 이제 역할로 해석 → 선호 provider(codex)
    assert (r["to"], r["routed_by"]) == ("cx", "role")
    b.store.try_claim_session("cx", "busy")
    r = await b.send("구현", sender="planner", to="builder")   # 같은 provider로 우회하지 않는다
    assert r["to"] == "cx"
    r = await b.send("구현", sender="user", to="builder")      # 사람의 요청은 쉬는 세션 우선
    assert r["to"] == "builder2"


async def test_jev_confident_routes_by_role(tmp_path):
    b = _setup(tmp_path, judge=FakeJudge(probs={"builder": 0.9, "reviewer": 0.05, "planner": 0.05}))
    b.register("cx-review", "codex", roles=["reviewer"])
    r = await b.send("PlcService 재연결 백오프 넣어줘", sender="planner")
    assert (r["to"], r["routed_by"]) == ("builder", "jev")


async def test_jev_low_confidence_needs_routing_then_user_assigns(tmp_path):
    b = _setup(tmp_path, judge=FakeJudge(probs={"builder": 0.5, "reviewer": 0.45}))
    b.register("cx-review", "codex", roles=["reviewer"])
    r = await b.send("이거 좀 봐줘", sender="planner")
    assert r["status"] == "needs_routing" and "builder" in r["next"]
    out = b.assign(r["task_id"], "cx-review")
    assert (out["to"], out["routed_by"], out["status"]) == ("cx-review", "user", "queued")


async def test_provider_hint_filters_candidates(tmp_path):
    j = FakeJudge(probs={"builder": 0.95})
    b = _setup(tmp_path, judge=j)
    r = await b.send("구현", sender="builder", to="claude")
    assert r["to"] == "builder2"                     # builder 역할 중 claude 세션
    assert set(j.calls[0]["options"]) == {"planner", "builder", "reviewer"}


async def test_shadow_mode_logs_but_does_not_apply(tmp_path):
    b = _setup(tmp_path, mode="shadow", judge=FakeJudge(probs={"builder": 0.99}, human=0.9),
               rules=[{"pattern": "(?i)리뷰", "to": "reviewer"}])
    r = await b.send("리뷰 부탁", sender="builder")
    assert (r["to"], r["routed_by"], r["status"]) == ("reviewer", "rules", "queued")  # needs_human 미적용
    r2 = await b.send("구현 부탁", sender="builder")
    assert r2["status"] == "needs_routing" and "builder" in r2["next"]   # 제안만
    logs = b.store.route_log()
    assert any(x["router"] == "jev" and not x["applied"] for x in logs)


async def test_hw_rule_forces_read_only_even_when_explicit(tmp_path):
    b = _setup(tmp_path)
    r = await b.send("PLC 주소 D100 쓰기 로직 변경", sender="planner", to="builder", sandbox="workspace-write")
    assert r["sandbox"] == "read-only"


async def test_jev_safety_hold_requires_approval(tmp_path):
    b = _setup(tmp_path, judge=FakeJudge(human=0.8))
    r = await b.send("운영 서버에 배포", sender="planner", to="builder")
    assert r["status"] == "held"
    assert b.approve(r["task_id"])["status"] == "queued"


async def test_jev_failure_falls_back(tmp_path):
    b = _setup(tmp_path, judge=FakeJudge(error=RuntimeError("no key")))
    r = await b.send("구현", sender="planner", to="b")
    assert r["status"] == "queued"
    r = await b.send("뭔가 해줘", sender="builder")
    assert r["status"] == "needs_routing"
    assert b.store.require_task(r["task_id"]).route_meta["jev"]["error"]


async def test_single_candidate_no_jev(tmp_path):
    j = FakeJudge()
    b = make_broker(tmp_path, judge=j, roles=[])  # 역할 미정의 → 세션 단위
    b.register("planner", "claude", mode="interactive")
    b.register("worker", "codex")
    r = await b.send("뭐든", sender="planner")
    assert (r["to"], r["routed_by"]) == ("worker", "only")
    assert all(not c["ask_route"] for c in j.calls)


async def test_self_send_and_unknown_qualified(tmp_path):
    b = _setup(tmp_path)
    with pytest.raises(BrokerError, match="자기 자신"):
        await b.send("x", sender="builder", to="b")
    with pytest.raises(BrokerError, match="세션이 없습니다"):
        await b.send("x", sender="planner", to="codex:nobody")
    with pytest.raises(BrokerError, match="등록되지 않은 발신자"):
        await b.send("x", sender="ghost", to="b")


async def test_hop_and_root_budget(tmp_path):
    b = _setup(tmp_path, max_hops=2, max_tasks_per_root=3)
    t0 = await b.send("1", sender="planner", to="b")
    t1 = await b.send("2", sender="builder", to="reviewer", parent_id=t0["task_id"])
    assert t1["hop"] == 1
    t2 = await b.send("3", sender="reviewer", to="b", parent_id=t1["task_id"])
    assert t2["hop"] == 2
    with pytest.raises(BrokerError, match="파생된 작업 수|전달 깊이"):
        await b.send("4", sender="builder", to="reviewer", parent_id=t2["task_id"])


async def test_jev_judge_permutation_average():
    pytest.importorskip("typesafe_sdk")
    from srhbroker.router import JevJudge

    class J(JevJudge):
        def __init__(self):
            super().__init__(permutations=2)
            self.orders = []

        async def _call(self, state, questions):
            route = questions.get("route")
            order = list(route.criteria) if route else []
            self.orders.append(order)
            # 첫 번째로 나온 선택지를 과대평가하는 '순서 편향' 모델
            probs = {k: (0.7 if i == 0 else 0.3 / (len(order) - 1)) for i, k in enumerate(order)}
            return {"choices": {"route": probs}, "nouls": {"hw_risk": 0.1, "needs_human": 0.0}}

    j = J()
    v = await j.judge({"request": "x"}, {"a": "A", "b": "B"}, ask_route=True, ask_safety=True)
    assert j.orders == [["a", "b"], ["b", "a"]]
    assert v.route_probs == pytest.approx({"a": 0.5, "b": 0.5})  # 편향이 상쇄된다
    assert v.hw_risk == 0.1


# ── 하드웨어 안전 규칙: '하드웨어 대상 + 제어 동작' 조합일 때만 읽기 전용 ─────────────

@pytest.mark.parametrize("body", [
    "합성 X-ray 팬텀 영상 MP4를 public/work/에 복사하고 빌드",
    "X-ray 영상처리 필터 문서 갱신",
    "X-ray 카드 루프 포스터 교체",
    "웹 포트폴리오 콘텐츠 반영: X-ray는 합성 팬텀, JSON 1건 + MP4/WebP 5개 복사 후 npm build",   # 실제 사례
    "DICOM 뷰어 화면의 버튼 정렬 수정",                                   # 도메인 용어 + 동작이어도 대상 없음
    "PLC 통신 로그를 분석해서 원인만 알려줘",                              # 대상만 (동작 없음)
    "이 버그 원인을 조사해줘",                                            # '조사'(investigate) — 대상 없음
    "MP4/WebM 임베드로 충분합니다. 다중 프로젝트 구조 설계·구현",            # '임베드'의 '베드' 아님
    "카운터: Create 성공 직후 Interlocked +1 입니다.\n\n[제어면 참고] 병렬 실행 설정 확인",  # 다른 문단 + .NET Interlocked
])
async def test_domain_terms_alone_do_not_force_read_only(tmp_path, body):
    b = _setup(tmp_path, mode="rules")
    r = await b.send(body, sender="planner", to="builder", sandbox="workspace-write")
    assert r["sandbox"] == "workspace-write", r
    assert b.store.require_task(r["task_id"]).route_meta["safety"]["hw_risk"] < 0.2


@pytest.mark.parametrize("body", [
    "X-ray 발생기 kV·mA·노출 시간 변경 후 조사 테스트",
    "PLC 펄스 off 처리 수정 후 장비에서 충격파 발사 확인",
    "C-arm 0° 이동 시퀀스 수정",
    "디텍터 SDK 게인 설정을 실기에 적용",
    "PlcService 재연결 백오프 구현",                                      # 제어 코드 경로 변경
    "검출기 SDK 오프셋 캘리브 코드 고쳐줘",
    "로봇 암 모터 속도 파라미터 변경",
])
async def test_hardware_target_with_control_action_forces_read_only(tmp_path, body):
    b = _setup(tmp_path, mode="rules")
    r = await b.send(body, sender="planner", to="builder", sandbox="workspace-write")
    assert r["sandbox"] == "read-only", r
    srcs = b.store.require_task(r["task_id"]).route_meta["safety"]["sources"]
    assert any(s.startswith("rule:target=") for s in srcs) and any(s.startswith("rule:action=") for s in srcs)


async def test_project_specific_targets_extend_defaults(tmp_path):
    """프로젝트 고유 제어 코드 이름은 config 의 extra_targets 로 기본 목록에 더한다 (기본 대상은 그대로 유지)."""
    (tmp_path / "config.toml").write_text('[router.hw_rules]\nextra_targets = [\'FrameService\']\n', encoding="utf-8")
    b = _setup(tmp_path, mode="rules")
    r = await b.send("FrameService 프레임 드롭 고쳐줘", sender="planner", to="builder", sandbox="workspace-write")
    assert r["sandbox"] == "read-only"
    r = await b.send("PLC 펄스 off 처리 수정", sender="planner", to="builder", sandbox="workspace-write")
    assert r["sandbox"] == "read-only"                                    # 기본 대상도 계속 적용


async def test_jev_hw_risk_still_forces_read_only_without_rule(tmp_path):
    b = _setup(tmp_path, judge=FakeJudge(probs={"builder": 1.0}, hw=0.6))
    r = await b.send("장비 쪽 작업 부탁", sender="planner", to="builder", sandbox="workspace-write")
    assert r["sandbox"] == "read-only"                                     # 규칙에 안 걸려도 Jev 판단은 그대로


async def test_legacy_single_word_patterns_still_supported(tmp_path):
    b = _setup(tmp_path, mode="rules")
    b.router.hw_patterns = [__import__("re").compile(r"X-?ray", 2)]       # config 에 hw_risk_patterns 를 직접 넣은 경우
    r = await b.send("X-ray 카드 포스터 교체", sender="planner", to="builder", sandbox="workspace-write")
    assert r["sandbox"] == "read-only"
