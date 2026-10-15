"""라우터: 대상 해석(G1) · 확신도(G2) · 안전(G3) 게이트.

구조: "Jev 는 역할을 고르고, 브로커는 그 역할의 세션을 고른다"
  ① 지정값이 세션 이름/별칭과 정확히 일치 → 그대로 (Jev 생략)
  ② 지정값이 역할 이름과 일치 → 브로커가 세션 선택
  ③ 그 외(지정 없음, provider 이름, 자유 힌트) → 규칙 → Jev(역할 Choice) → 브로커가 세션 선택
  + 안전 판단은 명시 지정 여부와 관계없이 수행 (정규식 규칙 + Jev Noul)
  + 작업 중 도착 판단(triage): 받는 창이 작업 중일 때 온 요청을 지금 넣을지(취소·수정 등 지금 작업과 관련),
    지금 작업을 마친 뒤 넣을지 (규칙 + Jev Noul)
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
    # 선택: async def interrupt(self, state) -> float | dict | None — 작업 중 도착한 요청을 바로 반영해야 할 확률
    #       (dict 면 {"interrupt": p, "priority": q} — q: 대기 중인 다른 요청보다 먼저 처리해야 할 확률)
    # 선택: async def relate(self, state, targets) -> {"relation": {라벨: p}, "target": {작업 ID: p}}


_HW_Q = ("이 요청이 하드웨어·안전 경계면(PLC/MC Protocol, X-ray 검출기 SDK, DICOM, 장비 동기화·제어 서비스, "
         "X-ray 조사 제어, 선량 계산, DI 등록, 설정 스키마)의 동작을 바꾸는 작업을 포함한다.")
_HUMAN_Q = ("이 요청은 되돌리기 어렵거나 외부에 영향을 주는 작업(배포, 삭제, 푸시, 장비 제어, 데이터 이전 등) 또는 "
            "장비 제어·안전·데이터 무결성에 영향을 주는 중요 코드 변경을 실행하게 하는 요청이어서, 사람의 승인 없이 "
            "진행하면 위험하다. 단순 알림·보고·검토·분석 요청은 해당하지 않는다.")
_INTERRUPT_Q = ("받는 세션은 지금 다른 작업을 하는 중이다(current_work: 받는 세션이 진행 중인 broker 작업, 비어 있으면 "
                "사용자가 직접 시킨 작업). 이 새 요청은 진행 중인 작업을 취소·중단·수정하거나 방향·범위·우선순위를 바꾸라는 "
                "내용, 또는 진행 중인 작업의 결과를 바로 바꿔야 하는 정정·긴급 요청이어서, 지금 작업을 마치기 전에 바로 "
                "반영해야 한다. 진행 중인 작업과 무관한 새 작업·알림·참고 자료는 해당하지 않는다.")

_PRIORITY_Q = ("이 새 요청은 받는 세션 앞에 대기 중인 다른 요청들보다 먼저 처리해야 할 만큼 급하거나 중요하다 "
               "(장애·차단 문제, 마감이 임박한 일 등).")
_RELATE_Q = ("보낸 세션이 같은 받는 세션에 보냈지만 아직 전달되지 않고 대기 중인 요청이 있다(pending_requests). "
             "이 새 요청(title·request)은 그 대기 요청과 어떤 관계인가?")
_RELATE_OPTS = {
    "cancel": "대기 요청 하나를 취소·철회하라는 것뿐이다 (다른 일을 새로 시키지 않는다)",
    "replace": "대기 요청 하나를 취소하고, 대신 다른 일을 하라는 요청이다",
    "modify": "대기 요청 하나의 내용을 고치거나, 범위·조건·대상을 바꾸거나, 내용을 더한다",
    "new": "대기 요청과 무관한 새 요청이나 알림이다",
}
_TARGET_Q = "새 요청이 가리키는 대기 요청은 어느 것인가?"


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

    async def interrupt(self, state: dict[str, Any]) -> dict[str, float | None]:
        from typesafe_sdk import Noul
        res = await self._call(state, {"interrupt": Noul(instructions=_INTERRUPT_Q),
                                       "priority": Noul(instructions=_PRIORITY_Q)})
        n = res.get("nouls", {})
        return {"interrupt": n.get("interrupt"), "priority": n.get("priority")}

    async def relate(self, state: dict[str, Any], targets: dict[str, str]) -> dict[str, dict[str, float]]:
        from typesafe_sdk import Choice
        q: dict[str, Any] = {"relation": Choice(instructions=_RELATE_Q, criteria=_RELATE_OPTS)}
        if len(targets) > 1:
            q["target"] = Choice(instructions=_TARGET_Q, criteria=targets)
        res = await self._call(state, q)
        ch = res.get("choices", {})
        return {"relation": ch.get("relation", {}), "target": ch.get("target", {})}

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


# 앞서 보낸 요청을 가리키는 말 (규칙으로 대기 요청과의 관계를 볼 때)
def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[\w가-힣]{2,}", s.lower())}


_REFER = re.compile(r"아까|방금|앞서|앞에|이전|먼저\s*보낸|그\s*(요청|작업|건)|\b(previous|earlier|last|that\s+(task|request))\b",
                    re.I)

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
        self.tcfg: dict[str, Any] = cfg.get("triage", {})
        self.urgent_patterns = [re.compile(p) for p in self.tcfg.get("urgent_patterns", [])]
        self.modify_patterns = [re.compile(p) for p in self.tcfg.get("modify_patterns", [])]
        self.cancel_patterns = [re.compile(p) for p in self.tcfg.get("cancel_patterns", [])]

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

    def _state(self, task: Task, hint: str | None, current: list[Task] | None = None,
               pending: list[Task] | None = None) -> dict[str, Any]:
        """외부(Jev)로 나가는 상태. 본문은 잘라서 보내고, 원하면 제목만 보낸다.
        current: 작업 중 도착 판단일 때 받는 세션이 진행 중인 작업 (최대 3건, 본문은 앞 300자).
        pending: 대기 요청과의 관계 판단일 때 같은 보낸 세션의 아직 넣지 않은 요청 (최대 5건, 본문은 앞 300자)."""
        send_body = self.jcfg.get("send_body", True)
        body = task.body if send_body else ""
        max_chars = int(self.jcfg.get("max_body_chars", 1500))
        state: dict[str, Any] = {"title": task.title, "request": body[:max_chars], "hint": hint or "",
                                 "sender": task.from_addr}
        if current is not None:
            state["current_work"] = [{"title": c.title, "request": (c.body if send_body else "")[:300],
                                      "sender": c.from_addr} for c in current[:3]]
        if pending is not None:
            state["pending_requests"] = [{"id": p.id, "title": p.title, "request": (p.body if send_body else "")[:300]}
                                         for p in pending[:5]]
        return state

    def _target_label(self, p: Task) -> str:
        """Jev 대상 선택지 설명 (작업 ID → 제목, 본문 앞부분)."""
        body = p.body if self.jcfg.get("send_body", True) else ""
        return (p.title + (f" — {body[:120]}" if body and body.strip() != p.title else "")).strip()

    # ── 작업 중 도착 판단 ────────────────────────────────────────────────

    def triage_rules(self, text: str, *, sender_active: bool, strong_words: bool = True) -> dict[str, Any]:
        """규칙: 취소·중단·긴급 말이 있으면 바로, 수정·변경 말은 그 보낸 세션의 작업을 지금 하고 있을 때만 바로.
        strong_words=False: 취소 말이 이미 처리한 대기 요청을 가리킨 경우(취소 후 대체) — 그 말로 바로 넣지 않는다."""
        strong = [m.group(0) for p in self.urgent_patterns if (m := p.search(text))] if strong_words else []
        modify = [m.group(0) for p in self.modify_patterns if (m := p.search(text))] if sender_active else []
        return {"urgent": bool(strong or modify), "by": "rules", "rules": strong + modify, "strong": bool(strong),
                "sender_active": sender_active}

    async def triage(self, task: Task, current: list[Task], *, sender_active: bool,
                     strong_words: bool = True) -> dict[str, Any]:
        """받는 창이 작업 중일 때 온 요청: urgent=True 면 지금 넣고, 아니면 지금 작업을 마친 뒤 넣는다.
        Jev 를 쓸 수 있으면 Jev 확률(threshold 이상)로 정하되, 취소·중단·긴급 말이 있으면 항상 바로 넣는다."""
        out = self.triage_rules(f"{task.title}\n{task.body}", sender_active=sender_active, strong_words=strong_words)
        ask = getattr(self.judge, "interrupt", None) if self.tcfg.get("mode", "jev") == "jev" else None
        if ask is None:
            return out
        try:
            p = await asyncio.wait_for(ask(self._state(task, None, current)),
                                       timeout=float(self.jcfg.get("timeout_s", 5.0)) * 2)
        except Exception as e:  # Jev 장애는 전달을 막지 않는다 → 규칙으로
            log.warning("Jev 작업 중 도착 판단 실패: %s", e)
            out["jev_error"] = f"{type(e).__name__}: {e}"
            return out
        prio = None
        if isinstance(p, dict):
            p, prio = p.get("interrupt"), p.get("priority")
        if prio is not None:
            out["priority"] = float(prio)
            out["priority_high"] = float(prio) >= float(self.tcfg.get("priority_threshold", 0.7))
        if p is None:
            return out
        out["jev"] = float(p)
        out["urgent"] = float(p) >= float(self.tcfg.get("threshold", 0.5)) or out["strong"]
        out["by"] = "jev"
        return out

    # ── 대기 요청과의 관계 ─────────────────────────────────────────────

    def relate_rules(self, text: str, pending: list[Task]) -> dict[str, Any]:
        """규칙: 취소·수정 말 + 대상(대기 요청이 하나뿐이거나, 제목 낱말이 한 요청에만 겹침). 규칙만으로는 취소하지 않는다."""
        cancel = [m.group(0) for p in self.cancel_patterns if (m := p.search(text))]
        modify = [m.group(0) for p in self.modify_patterns if (m := p.search(text))]
        rel = "cancel" if cancel else "modify" if modify else "new"
        target = None
        if rel != "new":
            # 대상: 제목 낱말이 한 요청에만 겹치거나, 대기 요청이 하나뿐이고 '아까·방금' 같은 지칭어가 있을 때
            hits = [p for p in pending if _words(p.title) & _words(text)]
            refer = _REFER.search(text)
            target = hits[0].id if len(hits) == 1 else pending[0].id if len(pending) == 1 and refer else None
        return {"relation": rel if target else "new", "target": target, "p": None, "target_sure": False,
                "by": "rules", "rules": cancel + modify}

    async def relate(self, task: Task, pending: list[Task]) -> dict[str, Any]:
        """새 메시지가 같은 보낸 세션의 대기 요청(pending)을 취소·대체·수정하는지, 무관한지.
        반환: relation(cancel|replace|modify|new) · target(작업 ID) · p(그 관계의 Jev 확률) · target_sure · by(jev|rules)"""
        text = f"{task.title}\n{task.body}"
        out = self.relate_rules(text, pending)
        ask = getattr(self.judge, "relate", None) if self.tcfg.get("mode", "jev") == "jev" else None
        if ask is None or not pending:
            return out
        try:
            res = await asyncio.wait_for(ask(self._state(task, None, pending=pending),
                                             {p.id: self._target_label(p) for p in pending[:5]}),
                                         timeout=float(self.jcfg.get("timeout_s", 5.0)) * 2)
        except Exception as e:  # Jev 장애 → 규칙 (규칙은 취소하지 않고 함께 넣기만 한다)
            log.warning("Jev 대기 요청 관계 판단 실패: %s", e)
            out["jev_error"] = f"{type(e).__name__}: {e}"
            return out
        rel_probs = res.get("relation") or {}
        if not rel_probs:
            return out
        rel, p = max(rel_probs.items(), key=lambda kv: kv[1])
        if len(pending) == 1:
            target, sure = pending[0].id, True
        else:
            ranked = sorted((res.get("target") or {}).items(), key=lambda kv: kv[1], reverse=True)
            (target, t1), t2 = (ranked[0] if ranked else (None, 0.0)), (ranked[1][1] if len(ranked) > 1 else 0.0)
            sure = target is not None and t1 >= float(self.tcfg.get("target_min", 0.6)) and t1 - t2 >= 0.2
            # 여럿 중 고를 때는 메시지에 그 요청의 제목 낱말이 있어야 분명하다고 본다 ('그거 취소' 같은 말로 엉뚱한 요청을 지우지 않게)
            chosen = next((x for x in pending if x.id == target), None)
            sure = sure and chosen is not None and bool(_words(chosen.title) & _words(text))
        return {"relation": rel if target else "new", "target": target, "p": float(p), "probs": rel_probs,
                "target_sure": bool(sure), "by": "jev", "rules": out["rules"]}

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
