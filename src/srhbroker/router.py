"""라우터: 대상 해석(G1) · 확신도(G2) · 안전(G3) 게이트.

구조: "Jev 는 역할을 고르고, 브로커는 그 역할의 세션을 고른다"
  ① 지정값이 세션 이름/별칭과 정확히 일치 → 그대로 (Jev 생략)
  ② 지정값이 역할 이름과 일치 → 브로커가 세션 선택
  ③ 그 외(지정 없음, provider 이름, 자유 힌트) → 규칙 → Jev(역할 Choice) → 브로커가 세션 선택
  + 안전 판단은 명시 지정 여부와 관계없이 수행 (정규식 규칙 + Jev Noul)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import PROVIDERS, Role, Sandbox, Session, Task

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Safety:
    hw_risk: float = 0.0          # 0~1. 규칙 매칭이면 1.0
    needs_human: float = 0.0
    sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RouteDecision:
    to: str | None                     # 세션 이름. None 이면 needs_routing
    routed_by: str | None              # name | alias | role | only | rules | jev
    confidence: float | None = None
    suggestion: str | None = None      # needs_routing 일 때 제안 대상(역할 또는 세션)
    reason: str = ""
    sandbox_floor: Sandbox | None = None  # 안전 게이트가 강제하는 최대 권한
    hold: bool = False                 # 사람 승인 필요(G3)
    safety: Safety = field(default_factory=Safety)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class JevVerdict:
    route_probs: dict[str, float]      # 선택지 → 평균 확률
    hw_risk: float | None = None
    needs_human: float | None = None
    error: str | None = None

    def top2(self) -> tuple[tuple[str, float], tuple[str, float]]:
        ranked = sorted(self.route_probs.items(), key=lambda kv: kv[1], reverse=True)
        first = ranked[0] if ranked else ("", 0.0)
        second = ranked[1] if len(ranked) > 1 else ("", 0.0)
        return first, second


class Judge(Protocol):
    async def judge(self, state: dict[str, Any], options: dict[str, str], *, ask_route: bool,
                    ask_safety: bool) -> JevVerdict: ...


_HW_Q = ("이 요청이 하드웨어·안전 경계면(PLC/MC Protocol, X-ray 검출기 SDK, DICOM, 장비 동기화·제어 서비스, "
         "X-ray 조사 제어, 선량 계산, DI 등록, 설정 스키마)의 동작을 바꾸는 작업을 포함한다.")
_HUMAN_Q = ("이 요청은 되돌리기 어렵거나 외부에 영향을 주는 작업(배포, 삭제, 푸시, 장비 제어, 데이터 이전 등) 또는 "
            "장비 제어·안전·데이터 무결성에 영향을 주는 중요 코드 변경을 실행하게 하는 요청이어서, 사람의 승인 없이 "
            "진행하면 위험하다. 단순 알림·보고·검토·분석 요청은 해당하지 않는다.")


class JevJudge:
    """TypeSafe Jev(System One) 호출. SDK 는 선택 의존성이며 TYPESAFE_API_KEY 를 사용한다.

    선택지 순서 민감도를 줄이기 위해 permutations 회 순서를 회전시켜 물은 뒤 확률을 평균한다.
    안전 질문(Noul)은 첫 호출에서만 묻는다.
    """

    def __init__(self, *, model: str | None = None, permutations: int = 2, timeout_s: float = 5.0):
        self.model = model
        self.permutations = max(1, permutations)
        self.timeout_s = timeout_s
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from typesafe_sdk import AsyncTypeSafeClient  # 지연 임포트: 선택 의존성
            self._client = AsyncTypeSafeClient()
        return self._client

    async def _call(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        """SDK 경계. {'choices': {q: {label: p}}, 'nouls': {q: p}} 로 정규화해 반환."""
        resp = await self._get_client().system_one(
            state=state, questions=questions, model=self.model, timeout=self.timeout_s)
        return {
            "choices": {k: dict(v.probabilities) for k, v in resp.choices.items()},
            "nouls": {k: float(v.noul) for k, v in resp.nouls.items()},
        }

    def _questions(self, options: dict[str, str], *, ask_route: bool, ask_safety: bool) -> dict[str, Any]:
        from typesafe_sdk import Choice, Noul
        q: dict[str, Any] = {}
        if ask_route:
            q["route"] = Choice(instructions="이 요청을 처리하기에 가장 적합한 담당은 누구인가?", criteria=options)
        if ask_safety:
            q["hw_risk"] = Noul(instructions=_HW_Q)
            q["needs_human"] = Noul(instructions=_HUMAN_Q)
        return q

    async def judge(self, state: dict[str, Any], options: dict[str, str], *, ask_route: bool,
                    ask_safety: bool) -> JevVerdict:
        labels = list(options)
        rounds = self.permutations if (ask_route and len(labels) > 1) else 1
        sums: dict[str, float] = {k: 0.0 for k in labels}
        hw = human = None
        for i in range(rounds):
            rot = labels[i % len(labels):] + labels[: i % len(labels)] if labels else []
            opts = {k: options[k] for k in rot}
            res = await self._call(state, self._questions(opts, ask_route=ask_route, ask_safety=ask_safety and i == 0))
            for k, p in res.get("choices", {}).get("route", {}).items():
                if k in sums:
                    sums[k] += p
            if i == 0:
                hw = res.get("nouls", {}).get("hw_risk")
                human = res.get("nouls", {}).get("needs_human")
        probs = {k: v / rounds for k, v in sums.items()} if ask_route else {}
        return JevVerdict(route_probs=probs, hw_risk=hw, needs_human=human)


# 문장 경계: 줄바꿈, 또는 문장부호(. ! ? 。 ;) 뒤의 공백
_SENTENCE = re.compile(r"[\r\n]+|(?<=[.!?。;])\s+")


class Router:
    def __init__(self, cfg: dict[str, Any], roles: list[Role], judge: Judge | None):
        self.cfg = cfg
        self.mode: str = cfg.get("mode", "shadow")
        self.roles = {r.name: r for r in roles}
        self.judge = judge if self.mode in ("shadow", "jev") else None
        self.jcfg: dict[str, Any] = cfg.get("jev", {})
        self.rules = [(re.compile(r["pattern"]), r["to"]) for r in cfg.get("rules", [])]
        # 이전 방식(단어 하나로 1.0) — 사용자가 config 에 직접 넣은 경우만 쓴다
        self.hw_patterns = [re.compile(p, re.I) for p in cfg.get("hw_risk_patterns", [])]
        hr = cfg.get("hw_rules", {})
        self.hw_targets = [re.compile(p, re.I) for p in [*hr.get("targets", []), *hr.get("extra_targets", [])]]
        self.hw_actions = [re.compile(p, re.I) for p in hr.get("actions", [])]
        self.hw_domain = [re.compile(p, re.I) for p in hr.get("domain_terms", [])]
        self.hw_domain_weight = float(hr.get("domain_weight", 0.15))
        self.hw_sandbox = Sandbox(cfg.get("hw_risk_sandbox", "read-only"))

    # ── 세션 선택 (역할 → 인스턴스) ──────────────────────────────────────

    def pick_instance(self, role: str, sessions: list[Session], provider: str | None = None,
                      exclude: str | None = None) -> Session | None:
        cands = [s for s in sessions if role in s.roles and s.name != exclude
                 and (provider is None or s.provider == provider)]
        if not cands:
            return None
        pref = self.roles[role].preferred_provider if role in self.roles else None
        # 쉬는 세션 > 선호 provider > worker > 이름순
        cands.sort(key=lambda s: (s.busy_task_id is not None, s.provider != pref, s.mode.value != "worker", s.name))
        return cands[0]

    def _options(self, sessions: list[Session], provider: str | None, exclude: str | None) -> tuple[str, dict[str, str]]:
        """Jev 선택지. 역할이 정의되어 있으면 '실제 세션이 있는' 역할, 없으면 세션 단위."""
        live = [s for s in sessions if s.name != exclude and (provider is None or s.provider == provider)]
        if self.roles:
            opts = {}
            for name, r in self.roles.items():
                if any(name in s.roles for s in live):
                    opts[name] = r.description
            return "role", opts
        return "session", {s.name: (s.description or ", ".join(s.roles) or s.provider) for s in live}

    def _target(self, unit: str, label: str, sessions: list[Session], provider: str | None,
                exclude: str | None) -> Session | None:
        if unit == "role":
            return self.pick_instance(label, sessions, provider, exclude)
        return next((s for s in sessions if s.name == label), None)

    # ── 안전 ────────────────────────────────────────────────────────────

    def _rule_safety(self, text: str) -> Safety:
        """규칙 기반 하드웨어 위험.
        - 하드웨어 대상(장비·제어 코드 경로) + 제어 동작이 작업 안에 함께 있으면 1.0 → 읽기 전용 강제
        - 영상 도메인 용어(X-ray·DICOM·디텍터 등)만 있으면 소폭 가중(domain_weight, 기본 0.15 — 임계값 0.2 미만)
        - 대상만·동작만 있으면 0 (Jev 판단에 맡긴다)
        X-ray 처럼 늘 쓰는 용어 하나로 웹 콘텐츠·문서 작업까지 읽기 전용이 되던 것을 막는다."""
        legacy = [p.pattern for p in self.hw_patterns if p.search(text)]
        if legacy:
            return Safety(hw_risk=1.0, sources=[f"rule:{h}" for h in legacy])
        # 대상과 동작이 '같은 문장(줄)' 안에 있어야 조합으로 본다 — 긴 본문의 서로 다른 문단에 흩어진 단어는 제외
        tg_all: list[str] = []
        hit_t: list[str] = []
        hit_a: list[str] = []
        for seg in _SENTENCE.split(text):
            tg = [m.group(0) for p in self.hw_targets if (m := p.search(seg))]
            tg_all += tg
            if tg and (ac := [m.group(0) for p in self.hw_actions if (m := p.search(seg))]):
                hit_t += tg
                hit_a += ac
        if hit_t:
            return Safety(hw_risk=1.0, sources=[f"rule:target={t}" for t in dict.fromkeys(hit_t)][:4]
                          + [f"rule:action={a}" for a in dict.fromkeys(hit_a)][:4])
        tg = tg_all
        dm = [m.group(0) for p in self.hw_domain if (m := p.search(text))]
        if dm or tg:
            return Safety(hw_risk=self.hw_domain_weight,
                          sources=[f"rule:domain={d}" for d in dict.fromkeys(dm + tg)][:4])
        return Safety()

    def _state(self, task: Task, hint: str | None) -> dict[str, Any]:
        """외부(Jev)로 나가는 상태. 본문은 잘라서 보내고, 원하면 제목만 보낸다."""
        body = task.body if self.jcfg.get("send_body", True) else ""
        max_chars = int(self.jcfg.get("max_body_chars", 1500))
        return {"title": task.title, "request": body[:max_chars], "hint": hint or "", "sender": task.from_addr}

    # ── 진입점 ──────────────────────────────────────────────────────────

    async def route(self, task: Task, hint: str | None, sessions: list[Session],
                    explicit: tuple[Session, str] | None) -> RouteDecision:
        text = f"{task.title}\n{task.body}"
        safety = self._rule_safety(text)
        exclude = task.from_addr  # 자기 자신에게 보내지 않는다 (G4)
        provider = hint if hint in PROVIDERS else None
        hint_role = hint if (hint and hint in self.roles) else None

        # ① 이름/별칭 명시
        if explicit:
            s, how = explicit
            d = RouteDecision(to=s.name, routed_by=how, confidence=1.0, reason=f"{how} 일치")
            ask_route = False
        # ② 역할 명시
        elif hint_role:
            s = self.pick_instance(hint_role, sessions, None, exclude)
            d = (RouteDecision(to=s.name, routed_by="role", confidence=1.0, reason=f"역할 '{hint_role}'")
                 if s else RouteDecision(to=None, routed_by=None, reason=f"역할 '{hint_role}' 에 등록된 세션이 없습니다"))
            ask_route = False
        else:
            d = self._rules(text, sessions, provider, exclude)
            ask_route = True

        unit, options = self._options(sessions, provider, exclude)
        d.meta["unit"] = unit

        # 선택지가 하나뿐이면 Jev 없이 결정
        if ask_route and d.to is None and len(options) == 1:
            only = next(iter(options))
            s = self._target(unit, only, sessions, provider, exclude)
            if s:
                d = RouteDecision(to=s.name, routed_by="only", confidence=1.0, reason="후보가 하나뿐", meta=d.meta)
            ask_route = False

        ask_safety = explicit is None or bool(self.jcfg.get("safety_on_explicit", True))
        verdict: JevVerdict | None = None
        if self.judge and (ask_route and len(options) > 1 or ask_safety):
            verdict = await self._safe_judge(task, hint, options, ask_route=ask_route and len(options) > 1,
                                             ask_safety=ask_safety)
            d.meta["jev"] = {"probs": verdict.route_probs, "hw_risk": verdict.hw_risk,
                             "needs_human": verdict.needs_human, "error": verdict.error}

        applied_jev = self.mode == "jev" and verdict is not None and verdict.error is None
        if verdict and verdict.route_probs and d.to is None:
            (top, p1), (_, p2) = verdict.top2()
            tau, delta = float(self.jcfg.get("min_confidence", 0.7)), float(self.jcfg.get("min_margin", 0.2))
            confident = p1 >= tau and (p1 - p2) >= delta
            d.suggestion = top
            d.meta["jev_top"] = {"label": top, "p": p1, "margin": p1 - p2, "confident": confident}
            if applied_jev and confident:
                s = self._target(unit, top, sessions, provider, exclude)
                if s:
                    d.to, d.routed_by, d.confidence, d.reason = s.name, "jev", p1, f"Jev {unit}='{top}' p={p1:.2f}"
            elif not d.reason:
                d.reason = "확신 부족" if applied_jev else "규칙에 해당 없음 (Jev 는 그림자 모드: 제안만)"
        elif d.to is None and not d.reason:
            d.reason = "규칙에 해당 없음"

        # 안전 판단 결합: 규칙은 항상 적용, Jev 는 jev 모드에서만 적용(그림자 모드는 기록만)
        if verdict and verdict.error is None:
            if verdict.hw_risk is not None:
                safety.sources.append(f"jev:hw_risk={verdict.hw_risk:.2f}")
                if applied_jev:
                    safety.hw_risk = max(safety.hw_risk, verdict.hw_risk)
            if verdict.needs_human is not None:
                safety.sources.append(f"jev:needs_human={verdict.needs_human:.2f}")
                if applied_jev:
                    safety.needs_human = verdict.needs_human
        d.safety = safety
        if safety.hw_risk >= float(self.jcfg.get("hw_risk_threshold", 0.2)):
            d.sandbox_floor = self.hw_sandbox
        if safety.needs_human >= float(self.jcfg.get("needs_human_threshold", 0.3)):
            d.hold = True
        return d

    def _rules(self, text: str, sessions: list[Session], provider: str | None, exclude: str | None) -> RouteDecision:
        for pat, to in self.rules:
            if not pat.search(text):
                continue
            if to in self.roles or any(to in s.roles for s in sessions):
                s = self.pick_instance(to, sessions, provider, exclude)
            else:
                s = next((x for x in sessions if x.name == to and x.name != exclude
                          and (provider is None or x.provider == provider)), None)
            if s:
                return RouteDecision(to=s.name, routed_by="rules", confidence=1.0, reason=f"규칙 /{pat.pattern}/ → {to}")
        return RouteDecision(to=None, routed_by=None)

    async def _safe_judge(self, task: Task, hint: str | None, options: dict[str, str], *, ask_route: bool,
                          ask_safety: bool) -> JevVerdict:
        assert self.judge is not None
        try:
            return await asyncio.wait_for(
                self.judge.judge(self._state(task, hint), options, ask_route=ask_route, ask_safety=ask_safety),
                timeout=float(self.jcfg.get("timeout_s", 5.0)) * 3)
        except Exception as e:  # Jev 장애는 라우팅을 막지 않는다 → 규칙/사용자 지정으로 대체
            log.warning("Jev 판단 실패: %s", e)
            return JevVerdict(route_probs={}, error=f"{type(e).__name__}: {e}")


def build_router(router_cfg: dict[str, Any], roles_cfg: list[dict[str, Any]], judge: Judge | None = None) -> Router:
    roles = [Role(name=r["name"], description=r.get("description", ""), preferred_provider=r.get("preferred_provider"))
             for r in roles_cfg]
    if judge is None and router_cfg.get("mode", "shadow") in ("shadow", "jev"):
        if not os.environ.get("TYPESAFE_API_KEY"):
            log.info("TYPESAFE_API_KEY 미설정 — Jev 판단 없이 이름·역할·규칙으로만 라우팅합니다")
            return Router(router_cfg, roles, None)
        j = router_cfg.get("jev", {})
        judge = JevJudge(model=j.get("model"), permutations=int(j.get("permutations", 2)),
                         timeout_s=float(j.get("timeout_s", 5.0)))
    return Router(router_cfg, roles, judge)
