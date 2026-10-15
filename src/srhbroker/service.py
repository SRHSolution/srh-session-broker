"""브로커 핵심 동작. MCP 서버·CLI·디스패처가 공유한다."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from . import herdr as herdr_mod
from . import native
from .adapters.native_messaging import native_handoff
from .config import Config
from .models import (
    PROVIDERS, USER_ADDR, BrokerError, Sandbox, Session, SessionMode, Task, TaskKind, TaskStatus,
    extract_mention, new_task_id, normalize_name, now_iso, validate_name,
)
from .router import Router, build_router
from .store import Store

_ACTIVE = (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.DELIVERED, TaskStatus.HELD, TaskStatus.NEEDS_ROUTING)

_HEAD_WORKING = ("[SRH Broker] 작업 중에 새 메시지가 도착했습니다. 지금 작업과 관련 있으면(취소·수정·우선순위 변경 등) "
                 "바로 반영하세요. 지금 작업과 무관한 새 작업이면 srhbroker MCP 의 defer(task_id=...) 로 미루고 지금 작업을 "
                 "계속하세요 — 지금 작업이 끝나 창이 쉬면 broker 가 순서대로 다시 넣고, 보낸 세션에도 알립니다. "
                 "회신(결과 보고)은 미룰 필요가 없습니다.")
_HEAD_RELEASED = ("[SRH Broker] 앞의 작업 중에 도착해 미뤄 두었던 요청입니다. 이제 아래 내용을 처리하세요.")

# send·route 결과의 delivery 값을 보낸 세션이 사용자에게 설명할 수 있게
DELIVERY_NOTES = {
    "delivered": "받는 창에 바로 넣었습니다.",
    "waiting": "받는 창이 승인·질문 화면이라 기다리는 중입니다. 풀리면 자동으로 넣습니다.",
    "busy": "받는 창이 승인·질문 화면이라 지금은 넣을 수 없습니다.",
    "no-pane": "받는 세션의 창이 herdr 에 열려 있지 않습니다. 사용자에게 그 창을 열어 달라고 알리세요 "
               "(열리면 herdr 창에서 실행 중인 srhbroker daemon 이 넣습니다).",
    "pane-mismatch": "herdr 가 받는 세션을 다른 세션의 창과 짝지어 두어 넣지 않았습니다(백그라운드로 띄운 세션 등). "
                     "받는 쪽이 응답을 마칠 때(Stop hook) 받습니다. 바로 받게 하려면 사용자에게 받는 세션을 자기 창에서 "
                     "다시 열어(resume) 달라고 알리세요.",
    "not-herdr": "herdr 창 밖이라 바로 넣지 못했습니다. 받는 쪽이 응답을 마칠 때(Stop hook)나 inbox 로 받습니다.",
    "rate-limited": "짧은 시간에 너무 많이 보내 잠시 멈췄습니다(핑퐁 방지). 받는 쪽은 inbox 로 받을 수 있습니다.",
    "not-interactive": "받는 세션은 worker 라 데몬이 실행합니다.",
    "dropped": "받는 창이 닫혀 있어 알림을 넣지 않고 버렸습니다(알림은 창이 열릴 때까지 기다리지 않습니다). "
               "꼭 전해야 하는 내용이면 사용자에게 알리세요.",
    "nothing": "넣을 메시지가 없습니다.",
    "deferred": "받는 세션이 다른 작업을 하는 중이고 이 요청은 그 작업과 무관하다고 판단해, 지금 작업을 마친 뒤 순서대로 넣습니다 "
                "(대기 {n}번째). 사용자에게 그렇게 알리세요. 지금 작업을 바꾸는 급한 수정·취소라면 그 뜻을 분명히 적어 다시 보내세요.",
    "failed": "herdr 로 넣는 중 오류가 났습니다. 받는 쪽은 inbox 로 받을 수 있습니다.",
}


def extract_json(text: str | None) -> dict[str, Any] | None:
    """응답 텍스트에서 마지막 JSON 객체를 꺼낸다 (```json 블록 우선)."""
    if not text:
        return None
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    cands = list(reversed(blocks))
    end = text.rfind("}")
    if end != -1:
        depth = 0
        for i in range(end, -1, -1):
            depth += text[i] == "}"
            depth -= text[i] == "{"
            if depth == 0:
                cands.append(text[i:end + 1])
                break
    for c in cands:
        try:
            v = json.loads(c)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            continue
    return None


class Broker:
    def __init__(self, cfg: Config, store: Store | None = None, router: Router | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.router = router or build_router(cfg.router, cfg.roles)
        self.herdr = herdr_mod.Herdr(cfg.data.get("herdr"))

    # ── 세션 ─────────────────────────────────────────────────────────────

    def register(self, name: str, provider: str, *, mode: str = "worker", roles: list[str] | None = None,
                 aliases: list[str] | None = None, description: str = "", cwd: str | None = None,
                 native_id: str | None = None, sandbox: str = "workspace-write", model: str | None = None) -> Session:
        if provider not in PROVIDERS:
            raise BrokerError(f"provider 는 {PROVIDERS} 중 하나여야 합니다")
        known = {r["name"] for r in self.cfg.roles}
        unknown = [r for r in roles or [] if known and r not in known]
        if unknown:
            raise BrokerError(f"정의되지 않은 역할: {unknown} (config.toml [[roles]] 에 먼저 정의)")
        s = Session(name=validate_name(name), provider=provider, mode=SessionMode(mode), roles=roles or [],
                    aliases=aliases or [], description=description, cwd=cwd, native_id=native_id,
                    sandbox=Sandbox(sandbox), model=model)
        return self.store.upsert_session(s)

    def register_native(self, session_id: str, *, name: str | None = None, provider: str | None = None,
                        mode: str = "interactive", roles: list[str] | None = None, aliases: list[str] | None = None,
                        description: str = "", sandbox: str = "workspace-write", model: str | None = None,
                        notes: list[str] | None = None, force: bool = False) -> Session:
        """기존 Claude/Codex 세션을 등록한다. 이름은 rename 이름에서, provider·작업 폴더는 세션 기록에서 가져온다.

        - 이 세션이 이미 다른 이름으로 등록돼 있으면 그 등록을 새 이름으로 옮긴다
          (이전 이름은 별칭으로 남고, 그 이름 앞으로 쌓인 메시지도 따라온다).
        - 같은 이름을 같은 provider 의 다른 세션이 쓰고 있어도 그 세션 창이 닫혀 있으면 이 세션이 이름을 이어받는다
          (같은 이름으로 새 세션을 만든 흔한 경우). 두 창이 모두 열려 있으면 사용자가 정하게 오류를 낸다.
        notes 를 주면 무엇을 옮기고 이어받았는지 설명을 덧붙인다."""
        notes = notes if notes is not None else []
        ns = native.find(session_id, provider)
        if not ns:
            raise BrokerError(f"세션 '{session_id}' 을 찾을 수 없습니다 (Claude: ~/.claude/projects, Codex: ~/.codex/sessions)")
        name = name or normalize_name(ns.title)
        if not name:
            raise BrokerError(f"세션 이름 '{ns.title or '(없음)'}' 을 브로커 이름으로 쓸 수 없습니다 — "
                              "영문 이름으로 rename 하거나 이름을 직접 지정하세요. 임의의 이름을 만들지 말고 사용자에게 물어보세요")
        name = validate_name(name)
        mine = self.store.find_by_native_id(ns.id)
        taken = self.store.get_session(name)
        if taken is None:
            owner = self.store.resolve_name(name)
            if owner and owner[0].native_id != ns.id:
                raise BrokerError(f"이름 '{name}' 은 세션 '{owner[0].name}' 의 별칭입니다 — 임의의 이름을 만들지 말고 "
                                  "사용자에게 어느 쪽이 이 이름을 쓸지 물어보세요")
        takeover = bool(taken and taken.native_id != ns.id)
        if takeover:
            self._check_takeover(taken, ns, skip_live=force)
            notes.append(f"이름 '{name}' 을 쓰던 이전 {taken.provider} 세션({(taken.native_id or '-')[:8]})의 창이 닫혀 있어 "
                         "이 세션이 이름을 이어받았습니다. 그 이름 앞으로 대기 중인 메시지도 이 세션이 받습니다.")
        if mine and mine.name != name:
            self.store.rename_session(mine.name, name, replace=takeover)
            notes.append(f"이 세션의 이전 등록 이름 '{mine.name}' 을 '{name}' 으로 옮겼습니다 "
                         "(이전 이름은 별칭으로 남고, 대기 메시지도 함께 옮겼습니다).")
        cur = self.store.get_session(name)
        keep = cur or taken
        if takeover and not (mine and mine.name != name):
            self.store.remove_session(name)   # 닫힌 이전 세션의 등록을 비우고 이 세션으로 다시 등록
        return self.register(name, ns.provider, mode=mode,
                             roles=roles if roles is not None else (keep.roles if keep else None),
                             aliases=aliases if aliases is not None else (keep.aliases if keep else None),
                             description=description or (keep.description if keep else ""),
                             cwd=ns.cwd, native_id=ns.id, sandbox=sandbox, model=model)

    def rename(self, new: str, *, old: str | None = None, native_id: str | None = None,
               notes: list[str] | None = None) -> Session:
        """등록 이름을 바꾼다. 세션 ID·provider·작업 폴더·역할·별칭·설명·권한은 그대로 두고 이름만 바꾼다.
        옛 이름은 별칭으로 남고, 그 이름 앞으로 쌓인 작업·회신·직접 전달 기록도 새 이름으로 옮긴다.
        old 를 주지 않으면 native_id 로 찾은 '지금 이 세션'. old 가 등록에서 지워졌지만 기록이 남은 이름이면
        그 기록을 new 세션이 이어받는다(옛 이름은 별칭)."""
        notes = notes if notes is not None else []
        new = validate_name(new)
        if old is None:
            cur = self.identify(native_id)
            if not cur:
                raise BrokerError("이 세션은 아직 등록되지 않았습니다 — 먼저 register() 로 등록하세요")
            old = cur.name
        r = self.store.resolve_name(old)
        if r is None:
            n = self.store.adopt_name(old, new)
            notes.append(f"등록에서 지워진 옛 이름 '{old}' 의 기록 {n}건을 '{new}' 로 옮기고 '{old}' 를 별칭으로 붙였습니다.")
            s = self.store.get_session(new)
            assert s is not None
            return s
        src = r[0]
        if src.name == new:
            notes.append(f"이미 '{new}' 입니다.")
            return src
        taken = self.store.get_session(new)
        if taken and not (taken.native_id and taken.native_id == src.native_id):   # 같은 세션의 중복 항목은 정리
            self._check_takeover(taken, native.NativeSession(src.provider, src.native_id or "", None, src.cwd, 0))
            notes.append(f"이름 '{new}' 을 쓰던 항목({taken.provider} {(taken.native_id or '세션 ID 없음')[:8]})은 "
                         "창이 닫혀 있거나 세션 ID 가 없어 정리하고 이어받았습니다.")
        s = self.store.rename_session(src.name, new, replace=bool(taken))
        notes.append(f"'{src.name}' → '{new}': 세션 ID·역할·별칭·설명·권한은 그대로이고, 옛 이름은 별칭으로 남으며 "
                     "그 이름 앞 작업·회신 기록도 옮겼습니다.")
        return s

    def reconnect(self, name: str, *, session_id: str | None = None, notes: list[str] | None = None) -> Session:
        """등록 이름을 rename 이름이 같은 다른 세션(새로 연 세션·계정을 바꿔 연 세션 등)에 다시 연결한다.
        session_id 를 주지 않으면 최근 30일 안에서 rename 이름이 같은 가장 최근 세션을 고른다. 역할·별칭·설명은 그대로."""
        notes = notes if notes is not None else []
        r = self.store.resolve_name(name)
        if not r:
            raise BrokerError(f"등록되지 않은 이름 '{name}'")
        s = r[0]
        if not session_id:
            same = [ns for ns in native.recent(30, provider=s.provider)
                    if ns.title and (normalize_name(ns.title) == s.name or ns.title.strip().lower() == s.name)]
            if not same:
                raise BrokerError(f"rename 이름이 '{s.name}' 인 {s.provider} 세션을 최근 30일 안에서 찾지 못했습니다 — "
                                  "그 세션을 rename 했는지 확인하거나 --session <세션 ID> 로 지정하세요")
            session_id = max(same, key=lambda ns: ns.updated).id
        if session_id == s.native_id:
            notes.append(f"'{s.name}' 은 이미 그 세션({session_id[:8]})에 연결돼 있습니다.")
            return s
        old = s.native_id
        out = self.register_native(session_id, name=s.name, provider=s.provider, mode=s.mode.value, sandbox=s.sandbox.value,
                                   model=s.model, notes=notes, force=True)
        notes.append(f"'{out.name}' 을 세션 {(old or '없음')[:8]} → {out.native_id[:8]} 로 다시 연결했습니다 "
                     "(역할·별칭·설명 유지, 대기 메시지는 새 세션이 받음).")
        return out

    def _check_takeover(self, taken: Session, ns: native.NativeSession, *, skip_live: bool = False) -> None:
        """다른 세션이 쓰던 이름을 이 세션이 이어받아도 되는지. 안 되면 사용자가 정하도록 오류."""
        if taken.provider != ns.provider:
            raise BrokerError(f"이름 '{taken.name}' 은 {taken.provider} 세션이 쓰고 있습니다(이름은 provider 와 무관하게 유일). "
                              "임의의 이름을 만들지 말고 사용자에게 어떻게 할지 물어보세요")
        if taken.mode != SessionMode.INTERACTIVE:
            raise BrokerError(f"이름 '{taken.name}' 은 worker 세션 이름입니다 — 사용자에게 어떻게 할지 물어보세요")
        if not skip_live and taken.native_id and self._native_live(taken):
            raise BrokerError(f"이름 '{taken.name}' 을 쓰는 다른 {taken.provider} 세션({taken.native_id[:8]})의 창이 지금 "
                              "열려 있습니다 — 같은 이름의 창이 둘입니다. 임의의 이름을 만들지 말고, 사용자에게 어느 창이 이 "
                              "이름을 쓸지 묻거나 한쪽을 다른 이름으로 rename 하게 하세요")

    def _native_live(self, s: Session) -> bool:
        """등록된 세션의 창이 지금 열려 있는가. herdr 로 확인하고, herdr 밖이면 최근 10분 안에 기록이 바뀌었는지로 판단."""
        if self.herdr.available():
            return self.locate_pane(s)[1] in ("id", "title")
        ns = native.find(s.native_id, s.provider) if s.native_id else None
        return bool(ns) and datetime.now().timestamp() - ns.updated < 600

    def identify(self, native_id: str | None) -> Session | None:
        """실행 중인 Claude/Codex 세션 ID → 등록된 브로커 세션."""
        return self.store.find_by_native_id(native_id) if native_id else None

    def sync_title(self, s: Session, title: str | None) -> str | None:
        """세션을 rename 했으면 새 이름을 별칭으로 추가한다. 기존 이름도 계속 쓸 수 있다. 추가한 별칭을 반환."""
        n = normalize_name(title)
        if not n or n == s.name or n in s.aliases:
            return None
        try:
            self.store.upsert_session(replace(s, aliases=[*s.aliases, n]))
        except BrokerError:  # 다른 세션이 쓰는 이름이면 건너뛴다
            return None
        return n

    def _check_sender(self, sender: str | None) -> str:
        if not sender:
            raise BrokerError("발신자를 알 수 없습니다: 이 세션을 먼저 등록하거나(register) sender 를 지정하세요")
        if sender == USER_ADDR:
            return sender
        s = self.store.resolve_name(sender)
        if not s:
            raise BrokerError(f"등록되지 않은 발신자 '{sender}' (먼저 register)")
        return s[0].name

    # ── 발송 ─────────────────────────────────────────────────────────────

    def _broker_candidates(self, sender: str) -> list[Session]:
        """사람의 CLI 요청은 유지하고, 세션 발신 요청은 다른 provider만 중계한다."""
        source = self.store.get_session(self._check_sender(sender))
        return [s for s in self.store.list_sessions() if source is None or s.provider != source.provider]

    async def send(self, body: str, *, sender: str | None, to: str | None = None, title: str = "",
                   kind: str = "task", acceptance: list[str] | None = None, reply_schema: dict | None = None,
                   sandbox: str | None = None, deadline_s: int | None = None, resume_on_reply: bool = False,
                   parent_id: str | None = None, user_directed: bool = False,
                   user_request: str | None = None) -> dict[str, Any]:
        """user_directed: 사용자가 이 대화에서 직접 '이걸 보내라/시켜라'고 지시한 작업이면 True — 승인을 다시 받지 않는다.
        이때 user_request 에 사용자가 한 말을 그대로 적어야 한다(기록에 남는다)."""
        sender = self._check_sender(sender)
        if user_directed:
            if not (user_request or "").strip():
                raise BrokerError("user_directed=true 이면 user_request 에 사용자가 지시한 말을 그대로 적어야 합니다")
            if parent_id:
                raise BrokerError("작업 실행 중에 보낸 send 는 사용자 직접 지시로 볼 수 없습니다 (user_directed 불가)")
        if not body or not body.strip():
            raise BrokerError("본문이 비어 있습니다")
        if to is None:
            to, body = extract_mention(body)
        if not body.strip():
            raise BrokerError("본문이 비어 있습니다")
        hint = to.strip().lower() if to and to.strip().lower() not in ("auto", "any", "") else None

        explicit = self.store.resolve_name(hint) if hint else None
        if hint and not explicit and ":" in hint:
            raise BrokerError(f"'{hint}' 세션이 없습니다")
        if explicit and explicit[0].name == sender:
            raise BrokerError("자기 자신에게는 보낼 수 없습니다")
        source = self.store.get_session(sender)
        if source and ((explicit and explicit[0].provider == source.provider) or hint == source.provider):
            targets = ([explicit[0]] if explicit else
                       [s for s in self.store.list_sessions() if s.provider == source.provider and s.name != sender])
            # broker의 Jev·작업 계약·루프 예산·DB 저장을 거치지 않는다.
            request = {
                "body": body.strip(), "title": title, "kind": TaskKind(kind).value,
                "acceptance": acceptance or [], "reply_schema": reply_schema,
                "sandbox": Sandbox(sandbox).value if sandbox else None,
                "deadline_s": deadline_s, "resume_on_reply": resume_on_reply,
            }
            # Codex 의 협업 도구는 자기가 띄운 하위 에이전트에만 닿는다 → 독립 Codex 세션끼리는 herdr 로 바로 넣는다
            direct = self.cfg.data.get("herdr", {}).get("direct_providers", ["codex"])
            if len(targets) == 1 and source.provider in direct and targets[0].mode == SessionMode.INTERACTIVE:
                return self._direct_send(source, targets[0], request)
            return native_handoff(source, targets, request)

        # G4: 루프 방지 — 작업 안에서 보낸 send 는 깊이·총량 제한
        hop, root_id = 0, None
        parent = self.store.get_task(parent_id) if parent_id else None
        if parent:
            hop, root_id = parent.hop + 1, parent.root_id or parent.id
            if hop > int(self.cfg.broker["max_hops"]):
                raise BrokerError(f"전달 깊이 초과(hop={hop} > {self.cfg.broker['max_hops']}): 작업 체인이 너무 깊습니다")
            if self.store.count_root(root_id) >= int(self.cfg.broker["max_tasks_per_root"]):
                raise BrokerError(f"최초 작업 {root_id} 에서 파생된 작업 수가 상한에 도달했습니다")

        t = Task(id=new_task_id(), kind=TaskKind(kind), from_addr=sender, to_addr=None, body=body.strip(),
                 title=title or body.strip().splitlines()[0][:80], to_hint=to, acceptance=acceptance or [],
                 reply_schema=reply_schema, sandbox=Sandbox(sandbox) if sandbox else None,
                 deadline_s=int(deadline_s or self.cfg.broker["default_deadline_s"]), hop=hop,
                 root_id=root_id, parent_id=parent.id if parent else None, resume_on_reply=resume_on_reply)
        t.root_id = t.root_id or t.id

        candidates = self._broker_candidates(sender)
        d = await self.router.route(t, hint, candidates, explicit)
        if d.to == sender:
            raise BrokerError("자기 자신에게는 보낼 수 없습니다")
        if d.to and d.to not in {s.name for s in candidates}:
            raise BrokerError("broker는 다른 provider의 세션에만 라우팅할 수 있습니다")
        t.to_addr = d.to
        t.routed_by = d.routed_by
        t.route_meta = {"reason": d.reason, "confidence": d.confidence, "suggestion": d.suggestion,
                        "safety": {"hw_risk": d.safety.hw_risk, "needs_human": d.safety.needs_human,
                                   "sources": d.safety.sources}, **d.meta}
        hold, why = self._approval_policy(t, d, sender, user_directed)
        t.route_meta["policy"] = {"requested_sandbox": self._requested_sandbox(t, d).value, "approval": why,
                                  "user_directed": bool(user_directed or sender == USER_ADDR),
                                  "user_request": (user_request or "").strip() or None}
        if d.sandbox_floor and why != "user":   # 사용자가 직접 지시한 작업은 요청한 권한 그대로
            t.sandbox = Sandbox.narrower(t.sandbox, d.sandbox_floor)
        if d.to is None:
            t.status = TaskStatus.NEEDS_ROUTING
        elif hold:
            t.status = TaskStatus.HELD
        self.store.insert_task(t)

        self.store.log_route(t.id, d.routed_by or "none", d.to, d.confidence, True,
                             {"reason": d.reason, "hint": hint})
        if "jev" in d.meta:
            jt = d.meta.get("jev_top") or {}
            self.store.log_route(t.id, "jev", jt.get("label"), jt.get("p"), d.routed_by == "jev", d.meta["jev"])

        out = {**t.summary(), "reason": d.reason}
        if t.status == TaskStatus.QUEUED and t.to_addr:
            rel = await self._relate(t)
            if rel and rel["applied"] == "cancelled":       # 대기 요청을 넣기 전에 취소 — 이 메시지는 넣지 않는다
                out.update(self.store.require_task(t.id).summary())
                out.update(delivery="not-needed", relation=rel["public"], next=rel["next"],
                           delivery_note=rel["next"])
                return out
            if not rel or rel["applied"] != "bundled":   # 묶인 메시지는 대상 요청과 함께 넣는다 (작업 중 도착 판단 생략)
                await self._triage(t, strong_words=not rel)   # 대체: '취소' 말은 이미 처리한 대기 요청을 가리킨다
            out.update(self._queued_result(t.id, t.to_addr))
            if rel:
                out["relation"] = rel["public"]
                if rel.get("next"):
                    out["next"] = rel["next"]
        if t.status == TaskStatus.NEEDS_ROUTING:
            out["next"] = (f"대상이 정해지지 않았습니다. 제안: {d.suggestion!r}. "
                           f"route(task_id='{t.id}', to='<세션 이름|역할>') 로 지정하세요.")
        elif t.status == TaskStatus.HELD:
            why = {"hw-code": "장비 제어 코드 변경 — 승인 전까지 읽기 전용, 승인하면 요청한 권한으로 실행",
                   "irreversible": "되돌리기 어려운 외부 작업 또는 중요 코드 변경"}.get(t.route_meta["policy"]["approval"], "")
            out["next"] = (f"사람 승인 대기 중입니다({why}; 판단: {t.route_meta['safety']}). 사용자에게 사유를 보여 주고, "
                           f"사용자가 명시적으로 승인하면 approve(task_id='{t.id}') 를 호출하세요. 사용자가 직접 지시한 작업이었다면 "
                           f"cancel 후 user_directed=true 와 사용자 지시 원문으로 다시 보내세요.")
        return out

    def _requested_sandbox(self, t: Task, d: Any) -> Sandbox:
        """보낸 쪽이 요청한 권한 (지정하지 않으면 받는 세션의 최대 권한)."""
        rcv = self.store.get_session(d.to) if d.to else None
        return Sandbox.narrower(t.sandbox, rcv.sandbox) if rcv else (t.sandbox or Sandbox.WORKSPACE_WRITE)

    def _approval_policy(self, t: Task, d: Any, sender: str, user_directed: bool) -> tuple[bool, str]:
        """승인(held) 여부와 사유.
        승인 대상: 세션이 스스로 판단해 보낸 '쓰기' 작업 중
          - 하드웨어 위험으로 권한이 낮춰지는 경우(장비 제어 코드 변경 등) → 승인하면 요청한 권한으로 실행
          - Jev 가 되돌리기 어려운 외부 작업·중요 코드 변경으로 보는 경우 (needs_human ≥ 기준)
        면제: 사람(CLI)·사용자 직접 지시 / interactive 대상 알림·회신 / 읽기 전용 요청"""
        rcv = self.store.get_session(d.to) if d.to else None
        if sender == USER_ADDR or user_directed:
            return False, "user"
        if t.kind != TaskKind.TASK and (rcv is None or rcv.mode == SessionMode.INTERACTIVE):
            return False, "notice"
        if self._requested_sandbox(t, d) == Sandbox.READ_ONLY:
            return False, "read-only"
        if d.sandbox_floor == Sandbox.READ_ONLY:
            # 장비 규칙에 걸려도 실제로 중요 변경을 '실행하게 하는' 요청일 때만 승인 — 검토·분석 요청은 읽기 전용으로만 전달.
            # Jev 판단이 없으면(규칙 모드·호출 실패) 안전하게 승인 대기
            human = (d.meta.get("jev") or {}).get("needs_human") if isinstance(getattr(d, "meta", None), dict) else None
            if human is None or human >= float(self.router.jcfg.get("hw_hold_threshold", 0.3)):
                return True, "hw-code"
            return False, "hw-read-only"
        if d.hold:
            return True, "irreversible"
        return False, "none"

    def assign(self, task_id: str, to: str) -> dict[str, Any]:
        """needs_routing 작업의 대상을 사람이 지정한다 (역할 이름도 가능)."""
        t = self.store.require_task(task_id)
        r = self.store.resolve_name(to)
        candidates = self._broker_candidates(t.from_addr)
        if r:
            s = r[0]
        elif to in self.router.roles or any(to in x.roles for x in self.store.list_sessions()):
            s = self.router.pick_instance(to, candidates, exclude=t.from_addr)
            if not s:
                raise BrokerError(f"역할 '{to}' 에 중계 가능한 다른 provider의 세션이 없습니다")
        else:
            raise BrokerError(f"'{to}' 은 세션 이름·별칭·역할이 아닙니다")
        if s.name == t.from_addr:
            raise BrokerError("자기 자신에게는 보낼 수 없습니다")
        if s.name not in {x.name for x in candidates}:
            raise BrokerError(f"같은 provider({s.provider})의 세션 '{s.name}' 은 provider 기본 통신 도구로 "
                              "직접 전달하세요. broker의 route로 지정할 수 없습니다")
        if not self.store.transition(task_id, (TaskStatus.NEEDS_ROUTING,), TaskStatus.QUEUED,
                                     to_addr=s.name, routed_by="user"):
            raise BrokerError(f"작업 {task_id} 은 needs_routing 상태가 아닙니다 (현재 {t.status})")
        self.store.log_route(task_id, "user", s.name, 1.0, True, {"previous_suggestion": (t.route_meta or {}).get("suggestion")})
        return self._queued_result(task_id, s.name)

    def approve(self, task_id: str, sandbox: str | None = None, *, confirmation: str | None = None,
                via: str = "cli") -> dict[str, Any]:
        t = self.store.require_task(task_id)
        fields: dict[str, Any] = {}
        # 승인은 '요청된 그대로 진행'을 뜻한다: 하드웨어 위험으로 낮췄던 권한을 요청한 권한으로 되돌린다
        requested = ((t.route_meta or {}).get("policy") or {}).get("requested_sandbox")
        if sandbox or requested:
            fields["sandbox"] = Sandbox(sandbox or requested)
        if not self.store.transition(task_id, (TaskStatus.HELD,), TaskStatus.QUEUED, **fields):
            raise BrokerError(f"작업 {task_id} 은 held 상태가 아닙니다 (현재 {t.status})")
        # 누가 어떤 말로 승인했는지 남긴다 (MCP 승인은 AI 가 대화창에서 사용자 승인을 받아 호출)
        self.store.log_route(task_id, "approve", t.to_addr, None, True,
                             {"via": via, "confirmation": confirmation, "safety": (t.route_meta or {}).get("safety"),
                              "sandbox_before": t.sandbox.value if t.sandbox else None,
                              "sandbox": fields["sandbox"].value if "sandbox" in fields else (t.sandbox.value if t.sandbox else None)})
        return self._queued_result(task_id, t.to_addr)

    def _queued_result(self, task_id: str, to: str | None) -> dict[str, Any]:
        """queued 가 된 작업을 받는 창에 넣어 보고, 결과(상태·delivery·안내)를 돌려준다.
        받는 창이 닫혀 있으면 알림(message)은 그 자리에서 소멸된다(delivery=dropped)."""
        delivery = self.notify(to)
        out = self.store.require_task(task_id).summary()
        if out["status"] == TaskStatus.CANCELLED.value and delivery in ("dropped", "no-pane"):
            delivery = "dropped"
        pos = self._defer_position(to, task_id)
        if pos:
            delivery = "deferred"
            out["deferred"] = {"position": pos}
        out["delivery"] = delivery
        if delivery in DELIVERY_NOTES:
            out["delivery_note"] = DELIVERY_NOTES[delivery].format(n=pos)
        return out

    # ── 작업 중 도착: 지금 넣을지, 지금 작업을 마친 뒤 넣을지 ─────────────────

    def _working_receiver(self, name: str | None) -> Session | None:
        """받는 세션이 herdr 창에서 작업 중(working)이면 그 세션. 작업 중 도착 판단을 할 필요가 없으면 None."""
        if not name or self.router.tcfg.get("mode", "jev") == "off":
            return None
        if not self.cfg.data.get("herdr", {}).get("push_while_working", True):
            return None   # 작업 중에는 어차피 넣지 않고 쉴 때 넣는다
        s = self.store.get_session(name)
        if not s or s.mode != SessionMode.INTERACTIVE or not s.native_id or not self.herdr.available():
            return None
        agent, how = self.locate_pane(s)
        if how in ("none", "mismatch") or (agent or {}).get("agent_status") != "working":
            return None
        return s

    async def _triage(self, t: Task, *, strong_words: bool = True) -> dict[str, Any] | None:
        """받는 창이 작업 중일 때 온 broker 작업·메시지를 판단한다 (규칙 + Jev). 무관하면 미뤄 둔다."""
        rcv = self._working_receiver(t.to_addr)
        if rcv is None:
            return None
        current = self.store.in_progress(rcv.name)
        v = await self.router.triage(t, current, sender_active=any(c.from_addr == t.from_addr for c in current),
                                     strong_words=strong_words)
        v["working_on"] = [c.title for c in current]
        if not v["urgent"]:
            self.store.defer_item(t.id, rcv.name, t.from_addr, t.kind.value, v["by"],
                                  priority=1 if v.get("priority_high") else 0)
        self.store.log_route(t.id, "triage" if v["urgent"] else "defer", rcv.name, v.get("jev"), True, v)
        return v

    async def _relate(self, t: Task) -> dict[str, Any] | None:
        """같은 보낸 세션이 이 받는 세션에 보냈는데 아직 넣지 않은 요청이 있으면, 새 메시지가 그것을 취소·대체·수정하는지
        판단한다 (규칙 + Jev). 다른 세션의 요청은 보지 않는다 — 취소는 보낸 세션만 할 수 있다.
        - Jev 가 취소(또는 취소 후 대체)로 cancel_threshold 이상 확신하고 대상이 분명하면: 대상 요청을 넣기 전에 취소한다.
          취소만이면 이 메시지는 넣지 않고(applied=cancelled), 대체면 새 요청으로 계속 진행한다(applied=replaced).
        - 수정이거나, 취소로 보이지만 확신이 부족하면: 대상 요청과 묶어 함께 넣는다(applied=bundled).
        반환 None 이면 관계 없음(새 요청)."""
        tc = self.router.tcfg
        if tc.get("mode", "jev") == "off" or not tc.get("relate", True) or t.kind == TaskKind.REPLY:
            return None
        rcv = self.store.get_session(t.to_addr) if t.to_addr else None
        pending = self.store.pending_from(t.to_addr, t.from_addr, exclude=t.id) if rcv else []
        if not pending:
            return None
        v = await self.router.relate(t, pending)
        target = next((p for p in pending if p.id == v["target"]), None)
        if v["relation"] == "new" or target is None:
            if v["by"] == "jev":   # Jev 에 물어본 것은 기록한다 (그림자 평가용)
                self.store.log_route(t.id, "relate", None, v.get("p"), False, {**v, "applied": "new"})
            return None
        rel = v["relation"]
        sure = (v["by"] == "jev" and v["target_sure"] and (v.get("p") or 0) >= float(tc.get("cancel_threshold", 0.8)))
        public = {"relation": rel, "target": target.id, "target_title": target.title, "by": v["by"], "p": v.get("p")}
        if rel in ("cancel", "replace") and sure:
            if self.store.transition(target.id, (TaskStatus.QUEUED,), TaskStatus.CANCELLED, error="취소됨"):
                self.store.log_route(target.id, "cancel", target.to_addr, v.get("p"), True,
                                     {"by": t.from_addr, "was": "queued", "via": t.id})
                if rel == "cancel":
                    self.store.transition(t.id, (TaskStatus.QUEUED,), TaskStatus.DONE, started_at=now_iso(),
                                          result=f"대기 중이던 {target.id} 을 받는 쪽에 넣기 전에 취소함")
                    self.store.mark_reply_seen(t.id)
                self.store.log_route(t.id, "relate", target.id, v.get("p"), True,
                                     {**v, "applied": "cancelled" if rel == "cancel" else "replaced"})
                public["applied"] = "cancelled" if rel == "cancel" else "replaced"
                nxt = (f"대기 중이던 {target.id} ('{target.title}') 을 받는 쪽에 넣기 전에 취소했습니다"
                       + (". 이 메시지는 넣지 않았습니다(받는 쪽은 원래 요청을 보지 못했습니다)." if rel == "cancel"
                          else ". 이 메시지는 새 요청으로 보냈습니다.")
                       + " 잘못 취소됐다면 원래 요청을 다시 보내세요. 사용자에게 알리세요.")
                return {"applied": public["applied"], "public": public, "next": nxt}
            # 그 사이 넣어졌으면(취소 실패) 지금 작업을 바꾸는 메시지로 — 작업 중 도착 판단에 맡긴다
            return None
        # 수정, 또는 취소로 보이지만 확실하지 않음 → 대상과 묶어 함께 넣는다
        rm = dict(t.route_meta or {})
        rm["relates_to"] = {"task_id": target.id, "title": target.title, "relation": rel, "sure": bool(sure)}
        self.store.transition(t.id, (TaskStatus.QUEUED,), TaskStatus.QUEUED, route_meta=rm)
        t.route_meta = rm
        deferred = {d["item_id"]: d for d in self.store.deferred(rcv.name)}
        if target.id in deferred:   # 대상이 미뤄져 있으면 이 메시지도 그 뒤에 묶어 둔다
            self.store.defer_item(t.id, rcv.name, t.from_addr, t.kind.value, v["by"], link=target.id,
                                  priority=deferred[target.id]["priority"])
        self.store.log_route(t.id, "relate", target.id, v.get("p"), True, {**v, "applied": "bundled"})
        public["applied"] = "bundled"
        nxt = (f"아직 넣지 않은 {target.id} ('{target.title}') 과 묶어 두었습니다. 받는 쪽에 함께 넣으며 "
               + ("이 메시지가 그 요청을 고치는 내용이라고 알립니다."
                  if rel == "modify" else "이 메시지가 그 요청을 취소하려는 것으로 보인다고 알립니다(확실하지 않아 취소하지 않음)."))
        return {"applied": "bundled", "public": public, "next": nxt}

    def _triage_direct(self, did: str, source: Session, target: Session, request: dict[str, Any]) -> bool:
        """같은 provider 직접 전달은 broker 작업이 아니라 Jev 로 보내지 않고 규칙으로만 판단한다. 미뤘으면 True."""
        if self._working_receiver(target.name) is None:
            return False
        since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        v = self.router.triage_rules(f"{request.get('title') or ''}\n{request.get('body') or ''}",
                                     sender_active=self.store.recent_direct_from(target.name, source.name, since))
        if v["urgent"]:
            return False
        self.store.defer_item(did, target.name, source.name, request.get("kind") or "task", "rules")
        return True

    def _defer_position(self, to: str | None, item_id: str) -> int:
        """미뤄 둔 요청이면 그 세션 앞 대기 순번(1부터), 아니면 0."""
        ids = [d["item_id"] for d in self.store.deferred(to)] if to else []
        return ids.index(item_id) + 1 if item_id in ids else 0

    def _hold_back(self, name: str, working: bool) -> set[str]:
        """이번에 넣지 않을 미룬 요청. 작업 중이면 모두, 쉬고 있으면 알림(message)은 모두 넣고 작업은 맨 앞 하나만 넣는다.
        방금 넣었으면(창이 아직 working 으로 바뀌지 않았을 수 있음) 모두 기다린다."""
        items = self.store.deferred(name)
        ids = {d["item_id"] for d in items}
        if not items or working:
            return ids
        last = self.store.last_push(name)
        gap = float(self.router.tcfg.get("release_gap_s", 15))
        if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < gap:
            return ids
        standalone = [d for d in items if not d.get("link") or d["link"] not in ids]
        tasks = [d["item_id"] for d in standalone if d["kind"] == "task"]
        release = {d["item_id"] for d in standalone if d["kind"] != "task"} | set(tasks[:1])
        release |= {d["item_id"] for d in items if d.get("link") in release}   # 고치는 메시지는 대상과 함께
        return ids - release

    def defer(self, task_id: str, *, sender: str | None, note: str = "") -> dict[str, Any]:
        """받은 작업이 지금 하는 작업과 무관해 나중에 처리한다 (받는 세션이 부른다).
        작업을 다시 대기(queued)로 돌리고, 창이 쉬면 broker 가 순서대로 넣는다. 보낸 세션에 알린다."""
        me = self._check_sender(sender)
        note = (note or "").strip()
        if task_id.startswith("d_"):
            d = self.store.get_direct(task_id)
            if not d or d["to_name"] != me:
                raise BrokerError(f"직접 전달 {task_id} 은 {me} 에게 온 것이 아닙니다")
            if d["status"] == "delivered" and not self.store.set_direct_status(task_id, "delivered", "queued"):
                raise BrokerError(f"직접 전달 {task_id} 을 미룰 수 없습니다 (현재 {d['status']})")
            if d["status"] not in ("delivered", "queued"):
                raise BrokerError(f"직접 전달 {task_id} 을 미룰 수 없습니다 (현재 {d['status']})")
            kind, frm, title = d["request"].get("kind") or "task", d["from_name"], d["request"].get("title") or ""
        else:
            t = self.store.require_task(task_id)
            if t.to_addr != me:
                raise BrokerError(f"작업 {task_id} 의 담당은 {t.to_addr} 입니다 ({me} 는 미룰 수 없음)")
            if not self.store.transition(task_id, (TaskStatus.DELIVERED, TaskStatus.QUEUED), TaskStatus.QUEUED):
                raise BrokerError(f"작업 {task_id} 은 미룰 수 있는 상태가 아닙니다 (현재 {t.status})")
            kind, frm, title = t.kind.value, t.from_addr, t.title
            self.store.log_route(task_id, "defer", me, None, True, {"by": "agent", "note": note or None})
        self.store.defer_item(task_id, me, frm, kind, "agent", note or None)
        pos = self._defer_position(me, task_id)
        body = (f"{me} 이(가) 지금 하는 작업을 마친 뒤 '{title}' ({task_id}) 을 순서대로 처리합니다 (대기 {pos}번째)."
                + (f" 메모: {note}" if note else "") + " 다시 보내지 마세요. 급하면 취소(cancel) 후 급한 이유를 적어 다시 보내세요.")
        notified = None
        if frm != USER_ADDR and (src := self.store.get_session(frm)):
            if task_id.startswith("d_"):
                did = "d_" + new_task_id()[2:]
                self.store.enqueue_direct(did, src.provider, me, frm, {"body": body, "title": f"미룸: {title}"[:80],
                                                                       "kind": "message"})
            else:
                self.store.insert_task(Task(id=new_task_id(), kind=TaskKind.MESSAGE, from_addr=me, to_addr=frm,
                                            title=f"미룸: {title}"[:80], routed_by="defer", parent_id=task_id,
                                            body=body, deadline_s=int(self.cfg.broker["default_deadline_s"])))
            notified = self.notify(frm)
        return {"task_id": task_id, "status": "queued", "deferred": {"position": pos, "by": "agent"},
                "sender_notified": notified,
                "next": "지금 작업을 계속하세요. 지금 회신하지 마세요 — 작업이 끝나 창이 쉬면 broker 가 이 요청을 다시 넣습니다."}

    def flag_overdue(self) -> list[str]:
        """창에 넣은 뒤 오래 회신이 없는 작업을 보낸 세션에 한 번 알린다 (데몬이 주기적으로 부른다)."""
        age = int(self.cfg.broker.get("reply_overdue_s", 14400))
        if age <= 0:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(timespec="seconds")
        flagged = []
        for t in self.store.overdue_tasks(cutoff):
            self.store.log_route(t.id, "overdue", t.to_addr, None, True, {"after_s": age})
            flagged.append(t.id)
            if t.from_addr != USER_ADDR and self.store.get_session(t.from_addr):
                hours = round(age / 3600, 1)
                self.store.insert_task(Task(
                    id=new_task_id(), kind=TaskKind.MESSAGE, from_addr=t.to_addr or "?", to_addr=t.from_addr,
                    title=f"회신 지연: {t.title}"[:80], routed_by="overdue", parent_id=t.id,
                    deadline_s=int(self.cfg.broker["default_deadline_s"]),
                    body=(f"작업 {t.id} ('{t.title}') 을 {t.to_addr} 창에 넣은 지 {hours}시간이 넘었는데 회신이 없습니다. "
                          f"status(task_id='{t.id}') 로 확인하고, 필요 없으면 cancel, 아직 필요하면 사용자에게 알리세요.")))
                self.notify(t.from_addr)
        return flagged

    def cancel(self, task_id: str, *, by: str | None = None) -> dict[str, Any]:
        """작업 취소. by 를 주면 보낸 세션과 사람(CLI, 'user')만 취소할 수 있다 (받는 쪽은 reply(status='failed')).
        d_... 는 같은 provider 직접 전달 대기열 항목 — 아직 넣지 않은 것만 취소된다."""
        if task_id.startswith("d_"):
            return self._cancel_direct(task_id, by)
        t = self.store.require_task(task_id)
        if by is not None:
            who = self._check_sender(by)
            if who != USER_ADDR and who != t.from_addr:
                raise BrokerError(f"작업 {task_id} 은 {t.from_addr} 가 보낸 작업입니다 — 보낸 세션이나 사용자(CLI)만 "
                                  "취소할 수 있습니다. 받은 작업을 그만두려면 reply(status='failed') 로 사유를 회신하세요")
        if not self.store.transition(task_id, _ACTIVE, TaskStatus.CANCELLED, error="취소됨"):
            raise BrokerError(f"작업 {task_id} 은 이미 끝났습니다")
        self.store.log_route(task_id, "cancel", t.to_addr, None, True,
                             {"by": by or "?", "was": t.status.value})  # 누가, 어느 단계에서 취소했는지
        # 실행 중이면 디스패처가 cancelled 상태를 보고 프로세스를 종료한다.
        # 이미 사람이 보는 창에 전달된 작업이면 받는 쪽은 취소를 모른 채 계속하므로 바로 알린다
        out = self.store.require_task(task_id).summary()
        rcv = self.store.get_session(t.to_addr) if t.to_addr else None
        if t.status == TaskStatus.DELIVERED and rcv and rcv.mode == SessionMode.INTERACTIVE:
            notice = Task(id=new_task_id(), kind=TaskKind.MESSAGE, from_addr=t.from_addr, to_addr=t.to_addr,
                          title=f"취소: {t.title}"[:80], routed_by="cancel", parent_id=t.id, root_id=t.root_id,
                          hop=t.hop, deadline_s=t.deadline_s,
                          body=(f"보낸 쪽({t.from_addr})이 작업 {t.id} ('{t.title}') 을 취소했습니다. "
                                "이 작업을 진행 중이면 지금 중단하고, 회신하지 마세요."))
            self.store.insert_task(notice)
            out["delivery"] = self.notify(t.to_addr)
        return out

    # ── 회신 ─────────────────────────────────────────────────────────────

    def complete(self, task_id: str, status: TaskStatus, *, result: str | None = None, error: str | None = None,
                 from_status: tuple[TaskStatus, ...] = (TaskStatus.RUNNING, TaskStatus.DELIVERED, TaskStatus.QUEUED),
                 ) -> bool:
        t = self.store.require_task(task_id)
        rj = extract_json(result) if (t.reply_schema and result) else None
        ok = self.store.transition(task_id, from_status, status, result=result, result_json=rj, error=error)
        if ok and t.resume_on_reply and t.from_addr != USER_ADDR and t.kind == TaskKind.TASK:
            self._enqueue_reply_notice(self.store.require_task(task_id))
        if ok and t.from_addr != USER_ADDR:
            self.notify(t.from_addr)  # 회신을 보낸 쪽 창에 바로 넣는다
        return ok

    def _enqueue_reply_notice(self, t: Task) -> None:
        """resume_on_reply: 발신 세션을 결과와 함께 다시 깨운다 (kind=reply)."""
        if not self.store.get_session(t.from_addr):
            return
        body = (f"작업 {t.id} ('{t.title}') 결과 [{t.status.value}] — 담당 {t.to_addr}\n\n"
                f"{t.result or t.error or '(내용 없음)'}")
        notice = Task(id=new_task_id(), kind=TaskKind.REPLY, from_addr=t.to_addr or USER_ADDR, to_addr=t.from_addr,
                      body=body, title=f"회신: {t.title}"[:80], reply_for=t.id, hop=t.hop, root_id=t.root_id,
                      parent_id=t.id, routed_by="reply", deadline_s=t.deadline_s)
        self.store.insert_task(notice)
        self.store.mark_reply_seen(t.id)

    def reply(self, task_id: str, result: str, *, sender: str | None, status: str = "done") -> dict[str, Any]:
        t = self.store.require_task(task_id)
        if sender and sender != USER_ADDR and t.to_addr and sender != t.to_addr:
            raise BrokerError(f"작업 {task_id} 의 담당은 {t.to_addr} 입니다 ({sender} 는 회신할 수 없음)")
        st = TaskStatus(status)
        if st not in (TaskStatus.DONE, TaskStatus.FAILED):
            raise BrokerError("status 는 done 또는 failed")
        if not self.complete(task_id, st, result=result if st == TaskStatus.DONE else None,
                             error=result if st == TaskStatus.FAILED else None):
            raise BrokerError(f"작업 {task_id} 은 회신 가능한 상태가 아닙니다 (현재 {t.status})")
        return self.store.require_task(task_id).summary()

    # ── 조회 ─────────────────────────────────────────────────────────────

    def status(self, task_id: str) -> dict[str, Any]:
        t = self.store.require_task(task_id)
        return {**t.summary(), "body": t.body, "result": t.result, "result_json": t.result_json, "error": t.error,
                "route": t.route_meta}

    async def wait(self, task_id: str, *, timeout_s: float = 600, caller: str | None = None,
                   poll_s: float = 1.0) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout_s
        while True:
            t = self.store.require_task(task_id)
            if t.status.terminal:
                if caller and caller == t.from_addr:
                    self.store.mark_reply_seen(task_id)
                return self.status(task_id)
            if loop.time() >= end:
                return {**self.status(task_id), "waiting": True, "note": f"{timeout_s:.0f}초 안에 끝나지 않았습니다"}
            await asyncio.sleep(poll_s)

    def inbox(self, name: str, *, mark: bool = True, at_idle: bool = False) -> dict[str, Any]:
        """받은 편지함. mark=True 면 꺼낸 것을 전달 처리한다 — 작업 중에 미뤄 둔 요청은 빼고, at_idle(응답을 마친 순간 =
        Stop hook)이면 미룬 요청을 순서대로 하나 넣는다. mark=False 는 미룬 요청도 deferred 표시와 함께 보여 준다."""
        n = self._check_sender(name)
        deferred = {d["item_id"] for d in self.store.deferred(n)}
        if not mark:
            return self._box_dict(self.store.inbox(n, mark=False), deferred)
        hold = self._hold_back(n, working=False) if at_idle else deferred
        box = self.store.inbox(n, mark=True, exclude=hold)
        released = {t.id for t in box["incoming"]} & deferred
        if released:
            self.store.undefer(released)
        return self._box_dict(box, released)

    @staticmethod
    def _box_dict(box: dict[str, list[Task]], deferred: set[str] = frozenset()) -> dict[str, Any]:
        return {
            "incoming": [{**t.summary(), "body": t.body, "acceptance": t.acceptance, "reply_schema": t.reply_schema,
                          **({"deferred": True} if t.id in deferred else {}),
                          **({"relates_to": (t.route_meta or {})["relates_to"]}
                             if (t.route_meta or {}).get("relates_to") else {})}
                         for t in box["incoming"]],
            "replies": [{**t.summary(), "result": t.result, "error": t.error} for t in box["replies"]],
        }

    def render_inbox(self, box: dict[str, Any], max_chars: int | None = None) -> str:
        """interactive 세션(Stop hook·herdr 프롬프트)에 넣을 텍스트. max_chars 를 넘으면 본문을 줄인다."""
        lines: list[str] = []
        n = len(box["incoming"]) + len(box["replies"])
        total = sum(len(t["body"]) for t in box["incoming"]) + sum(len(t["result"] or "") for t in box["replies"])
        cut = max(400, max_chars // max(1, n)) if max_chars and total > max_chars else None

        def body(text: str, task_id: str) -> str:
            if cut is None or len(text) <= cut:
                return text
            return text[:cut] + f"\n…(이하 생략 — 전체 내용은 srhbroker MCP 의 status(task_id='{task_id}') 로 확인)"

        for t in box["incoming"]:
            rel = t.get("relates_to")
            if rel:
                lines.append(f"↳ 아래 메시지는 같은 세션이 앞서 보낸 {rel['task_id']} ('{rel['title']}') "
                             + ("을(를) 고치는 내용입니다 — 그 요청과 함께 반영하세요."
                                if rel["relation"] == "modify" else
                                "을(를) 취소하려는 것으로 보입니다(확실하지 않아 broker 가 취소하지 않음) — 보낸 세션의 뜻대로 "
                                "반영하고, 취소라면 그 요청은 하지 말고 reply(status='failed') 로 알리세요."))
            lines.append(f"■ [{t['kind']}] {t['task_id']} — 보낸 세션: {t['from']}\n제목: {t['title']}\n"
                         f"{body(t['body'], t['task_id'])}")
            if t.get("acceptance"):
                lines.append("완료 조건:\n" + "\n".join(f"- {a}" for a in t["acceptance"]))
            if t["kind"] == "task":
                lines.append(f"처리 후 srhbroker MCP 의 reply(task_id='{t['task_id']}', result=...) 로 회신하세요.")
        for t in box["replies"]:
            lines.append(f"■ [회신] {t['task_id']} ({t['status']}) — 담당 {t['to']}\n제목: {t['title']}\n"
                         f"{body(t['result'] or t['error'] or '', t['task_id'])}")
        if box["replies"]:
            lines.append("회신은 결과 보고입니다. 사용자가 요청한 후속 작업이 아니면 다시 send 하지 마세요.")
        return "\n\n".join(lines)

    # ── herdr 즉시 전달 ──────────────────────────────────────────────────

    def title_owner(self, title: str) -> str | None:
        """창 제목(브로커 이름 꼴) → 그 이름·별칭의 등록 세션 이름."""
        try:
            r = self.store.resolve_name(title)
        except BrokerError:
            return None
        return r[0].name if r else None

    def locate_pane(self, s: Session, agents: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any] | None, str]:
        """세션이 떠 있는 herdr 창과 찾은 방법(id | title | mismatch | none). herdr 짝이 어긋나면 창 제목으로 찾는다."""
        if agents is None:
            agents = self.herdr.agents()
        return herdr_mod.locate(agents, s.native_id, s.provider, s.cwd, s.name, self.title_owner)

    def _ready_pane(self, s: Session) -> tuple[dict[str, Any] | None, str, bool]:
        """세션이 떠 있는 herdr 창을 찾아 지금 넣어도 되는지 판단한다. (창, 사유, 작업 중 여부)
        사유: ok | not-herdr | no-pane | pane-mismatch | busy | rate-limited"""
        if not self.herdr.available():
            return None, "not-herdr", False
        # herdr 의 창 짝(세션 ID)은 다른 프로세스가 가져갈 수 있다 — 종류·폴더·창 제목으로 확인하고, 어긋나면 창 제목으로 찾는다
        agent, how = self.locate_pane(s)
        if how == "none":
            return None, "no-pane", False
        if how == "mismatch":
            return agent, "pane-mismatch", False
        hcfg = self.cfg.data.get("herdr", {})
        status = agent.get("agent_status")
        # working 중에도 넣는다: Claude 는 작업 중 입력을 진행 중인 턴에 반영하고, Codex 는 steer 로 끼워 넣는다.
        # 승인·질문 창(blocked), 판단 불가(unknown), 번호 선택 화면에는 넣지 않는다 — Enter 가 선택지를 고르게 된다
        working = status == "working" and bool(hcfg.get("push_while_working", True))
        if (status not in herdr_mod.READY and not working) or self.herdr.at_menu(agent["pane_id"]):
            return agent, "busy", working
        since = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
        if self.store.push_count_since(s.name, since) >= int(hcfg.get("max_push_per_10min", 30)):
            return agent, "rate-limited", working
        return agent, "ok", working

    def deliver_now(self, name: str) -> str:
        """interactive 세션이 herdr 창에서 받을 수 있으면 받은 편지함(broker 작업·회신)과
        같은 provider 직접 전달 대기열을 한 번에 프롬프트로 넣는다.
        받는 창이 herdr 에 없으면(no-pane) 대기 중인 알림(message)은 소멸시킨다 — 작업·회신만 창이 열릴 때까지 기다린다.
        작업 중에 와서 미뤄 둔 요청은 창이 쉴 때 하나씩 넣는다 (_hold_back).
        반환: delivered | busy | deferred(미룬 요청만 남음) | no-pane | dropped | pane-mismatch | nothing | not-herdr |
              not-interactive | rate-limited | failed"""
        s = self.store.get_session(name)
        if not s or s.mode != SessionMode.INTERACTIVE or not s.native_id:
            return "not-interactive"
        if not self.store.pending_count(name):
            return "nothing"
        agent, why, working = self._ready_pane(s)
        if why == "no-pane" and self._drop_notices(name) and not self.store.pending_count(name):
            return "dropped"
        if why != "ok":
            return why
        deferred_ids = {d["item_id"] for d in self.store.deferred(name)}   # 선점하면 queued 가 아니게 되므로 먼저
        hold = self._hold_back(name, working)   # 작업 중에 와서 미뤄 둔 요청은 쉴 때 하나씩
        # 먼저 선점(CAS) — 동시에 넣는 다른 프로세스(데몬·대기 프로세스·Stop hook)와 겹치지 않게
        box = self.store.inbox(name, mark=True, exclude=hold)
        directs = self.store.claim_direct(name, exclude=hold)
        if not box["incoming"] and not box["replies"] and not directs:
            return "deferred" if hold else "nothing"
        limit = int(self.cfg.data.get("herdr", {}).get("max_push_chars", 6000))
        released = {t.id for t in box["incoming"]} | {d["id"] for d in directs}
        released &= deferred_ids
        head = (_HEAD_RELEASED if released else
                _HEAD_WORKING if working else "[SRH Broker] 새 메시지가 도착했습니다. 아래 내용을 처리하세요.")
        if released and hold:
            head += f" 미뤄 둔 요청이 {len(hold)}건 더 있습니다 — 이 작업을 마치면 이어서 넣습니다."
        parts = [head]
        if box["incoming"] or box["replies"]:
            parts.append(self.render_inbox(self._box_dict(box), limit))
        parts += [self._render_direct(d, limit) for d in directs]
        if not self.herdr.prompt(agent["pane_id"], "\n\n".join(parts)):
            self.store.unclaim(box)
            self.store.unclaim_direct(directs)
            return "failed"
        self.store.log_push(name)
        if released:
            self.store.undefer(released)
        return "delivered"

    def _drop_notices(self, name: str) -> bool:
        """받는 창이 닫힌 세션 앞으로 대기 중인 알림을 소멸시킨다. [herdr] drop_notices_when_closed=false 면 하지 않는다."""
        if not self.cfg.data.get("herdr", {}).get("drop_notices_when_closed", True):
            return False
        tasks, directs = self.store.drop_queued_messages(name, "받는 창이 닫혀 있어 알림 소멸")
        for tid in tasks:
            self.store.log_route(tid, "drop", name, None, True, {"reason": "no-pane"})
        return bool(tasks or directs)

    @staticmethod
    def _render_direct(d: dict[str, Any], limit: int) -> str:
        req = d["request"]
        lines = [f"■ [직접 전달] {d['id']} — 보낸 세션: {d['from_name']} (같은 provider — broker 작업 아님)"]
        if req.get("title"):
            lines.append(f"제목: {req['title']}")
        body = req.get("body") or ""
        lines.append(body if len(body) <= limit else body[:limit] + "\n…(이하 생략 — 보낸 세션에 전체 내용을 요청하세요)")
        if req.get("acceptance"):
            lines.append("완료 조건:\n" + "\n".join(f"- {a}" for a in req["acceptance"]))
        if req.get("reply_schema"):
            lines.append(f"응답 형식(JSON): {json.dumps(req['reply_schema'], ensure_ascii=False)}")
        if req.get("kind") == "task":
            lines.append(f"처리 후 결과는 srhbroker MCP 의 send(to='{d['from_name']}', body=...) 로 보내세요 "
                         "(같은 provider 직접 전달).")
        return "\n".join(lines)

    def _direct_send(self, source: Session, target: Session, request: dict[str, Any]) -> dict[str, Any]:
        """같은 provider 인데 provider 기본 도구로 닿지 않는 독립 세션끼리(Codex ↔ Codex).
        broker 작업·Jev·안전 판단·회신 추적 없이 직접 전달 대기열에 넣고 herdr 로 바로 넣는다.
        받는 창이 닫혀 있거나 승인 창이면 기다렸다가(데몬·대기 프로세스) 넣는다. 단 알림(message)은 창이 닫혀 있으면 소멸."""
        did = "d_" + new_task_id()[2:]
        self.store.enqueue_direct(did, source.provider, source.name, target.name, request)
        self._triage_direct(did, source, target, request)
        delivery = self.notify(target.name) or "failed"
        status = (self.store.get_direct(did) or {}).get("status")
        done = status == "delivered"
        if status == "dropped":
            delivery = "dropped"
        pos = 0 if done else self._defer_position(target.name, did)
        out: dict[str, Any] = {"transport": "herdr-direct", "task_id": None, "direct_id": did, "from": source.name,
                               "to": target.name, "provider": source.provider, "delivered": done,
                               "status": status if status in ("delivered", "dropped") else "queued", "delivery": delivery}
        if done:
            out["next"] = ("받는 창에 바로 넣었습니다. broker 작업이 아니므로 wait/status 로 추적하지 마세요. "
                           "결과는 받는 세션이 send 로 보내 줍니다.")
        elif status == "dropped":
            out["delivery_note"] = DELIVERY_NOTES["dropped"]
            out["next"] = "알림을 버렸습니다. 다시 보내지 말고, 필요하면 사용자에게 알리세요."
        elif pos:
            out["delivery"], out["deferred"] = "deferred", {"position": pos}
            out["delivery_note"] = DELIVERY_NOTES["deferred"].format(n=pos)
            out["next"] = (f"받는 세션의 지금 작업이 끝나면 넣습니다({did}). broker 작업이 아니므로 wait/status 로 추적하지 마세요. "
                           f"필요 없어지면 cancel(task_id='{did}') 로 취소하세요.")
        else:
            out["delivery_note"] = DELIVERY_NOTES.get(delivery, "")
            out["next"] = (f"아직 넣지 못해 직접 전달 대기열에 두었습니다({did}). 받는 창이 열리거나 풀리면 자동으로 넣습니다. "
                           f"필요 없어지면 cancel(task_id='{did}') 로 취소하세요.")
        return out

    def notify(self, name: str | None) -> str | None:
        """받는 세션에 바로 넣고, 바쁘면 쉬는 순간 넣어 줄 대기 프로세스를 띄운다."""
        if not name:
            return None
        try:
            r = self.deliver_now(name)
        except Exception as e:  # 전달 실패가 send·reply 자체를 실패시키지 않는다 (inbox·Stop hook 으로 받을 수 있음)
            return f"failed: {e}"
        if r == "busy":
            herdr_mod.spawn_waiter(name)
            return "waiting"
        if r == "deferred":   # 미룬 요청: 창이 쉬면 넣어 줄 대기 프로세스 (데몬·Stop hook 도 넣는다 — 먼저 넣은 쪽만)
            herdr_mod.spawn_waiter(name)
        return r

    def recent(self, limit: int = 20, status: str | None = None, *, mine: str | None = None,
               active: bool = False) -> list[dict[str, Any]]:
        """최근 작업. mine: 그 세션이 보낸 것만, active: 아직 끝나지 않은 것만."""
        sender = self._check_sender(mine) if mine else None
        rows = [t.summary() for t in self.store.list_tasks(status=status, limit=limit, from_addr=sender, active=active)]
        if status in (None, "queued", "delivered", "cancelled", "expired"):
            for d in self.store.list_direct(limit, from_name=sender, active=active):
                if status is None or d["status"] == status:
                    rows.append({"task_id": d["id"], "kind": "direct", "from": d["from_name"], "to": d["to_name"],
                                 "title": (d["request"].get("title") or d["request"].get("body") or "")[:80],
                                 "status": d["status"], "routed_by": "direct", "created_at": d["created_at"],
                                 "finished_at": d["delivered_at"]})
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]

    def expire_stale(self) -> list[str]:
        """오래 대기한 작업을 timeout 으로 끝내고, 보낸 세션에 결과(timeout)를 알린다."""
        age = int(self.cfg.broker.get("stale_after_s", 86400))
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(timespec="seconds")
        expired = self.store.expire_stale(cutoff)
        for name in {t.from_addr for t in expired if t.from_addr != USER_ADDR}:
            self.notify(name)
        return [t.id for t in expired] + [d["id"] for d in self.store.expire_direct(cutoff)]

    def _cancel_direct(self, did: str, by: str | None) -> dict[str, Any]:
        d = self.store.get_direct(did)
        if not d:
            raise BrokerError(f"직접 전달 {did} 이 없습니다")
        if by is not None and self._check_sender(by) not in (USER_ADDR, d["from_name"]):
            raise BrokerError(f"직접 전달 {did} 은 {d['from_name']} 가 보낸 것입니다 — 보낸 세션이나 사용자(CLI)만 취소할 수 있습니다")
        if not self.store.set_direct_status(did, "queued", "cancelled"):
            raise BrokerError(f"직접 전달 {did} 은 이미 {d['status']} 상태라 취소할 수 없습니다"
                              + (" — 받는 창에 이미 들어갔으니 같은 대상에게 취소를 다시 send 하세요" if d["status"] == "delivered" else ""))
        return {"task_id": did, "transport": "herdr-direct", "from": d["from_name"], "to": d["to_name"], "status": "cancelled"}

    def _summary(self, text: str | None) -> str | None:
        """관찰 기록에 남길 본문: 앞 store_body_chars 자. 줄바꿈은 살린다 (첫 줄 = 제목, 상세에서 원문 모양 유지)."""
        n = int(self.cfg.data.get("monitor", {}).get("store_body_chars", 2000))
        if n <= 0 or not text:
            return None
        lines = [" ".join(ln.split()) for ln in text.strip().splitlines()]
        return "\n".join(ln for i, ln in enumerate(lines) if ln or (i and lines[i - 1]))[:n]

    def observe_native(self, *, provider: str, from_native: str | None, to_raw: str, message: Any) -> None:
        """broker 를 거치지 않은 같은 provider 메시지(Claude SendMessage)를 관찰 기록만 한다. 전달에는 관여하지 않는다."""
        src = self.identify(from_native) if from_native else None
        target = to_raw.split(" [")[0].strip()          # 'worker [3fa9c1]' → 'worker'
        try:
            dst = self.store.resolve_name(target)
        except BrokerError:
            dst = None
        body = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
        self.store.log_flow(transport=f"{provider}-native", provider=provider, from_name=src.name if src else None,
                            from_native=from_native, to_name=dst[0].name if dst else None, to_raw=to_raw,
                            status="sent", summary=self._summary(body))

    def touch_native_id(self, name: str, native_id: str) -> None:
        s = self.store.get_session(name)
        if s and s.native_id != native_id:
            self.store.set_native_id(name, native_id)

    def timestamp(self) -> str:
        return now_iso()
