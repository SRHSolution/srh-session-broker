"""도메인 모델: 세션 이름·역할, 세션, 작업(계약).

주소 체계
- 세션 이름은 provider 와 무관하게 전역 유일하다 (예: builder, reviewer). provider 는 레지스트리에서 결정된다.
- 별칭(aliases)도 전역 유일하다 (예: b, rev).
- 'codex:builder' 형식도 받아들이되, provider 가 레지스트리와 다르면 오류.
- 역할(roles)은 여러 세션이 공유할 수 있다. 라우터(Jev)는 역할을 고르고, 브로커가 그 역할의 세션을 고른다.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

PROVIDERS = ("claude", "codex")
USER_ADDR = "user"  # 사람(CLI) 발신자

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_QUALIFIED_RE = re.compile(r"^(claude|codex):([a-z0-9][a-z0-9_-]{0,31})$")
# 본문 맨 앞의 명시 지정: "@builder 구현해줘", "@codex:builder ..."
_MENTION_RE = re.compile(r"^\s*@((?:claude:|codex:)?[a-z0-9][a-z0-9_-]{0,31})(?=\s|$)\s*", re.IGNORECASE)
RESERVED_NAMES = {USER_ADDR, "auto", "any", "claude", "codex"}


class BrokerError(Exception):
    """브로커 규칙 위반(잘못된 이름, 홉 초과 등). MCP 도구에서는 오류 응답으로 변환된다."""


class SessionMode(StrEnum):
    WORKER = "worker"            # 브로커가 headless로 깨워 실행한다
    INTERACTIVE = "interactive"  # 사람이 보는 세션. hook/inbox 로 pull 한다


class TaskKind(StrEnum):
    TASK = "task"        # 회신 의무 있음
    MESSAGE = "message"  # 정보 전달, 회신 선택
    REPLY = "reply"      # resume_on_reply 로 발신자에게 되돌려 보내는 결과 통지


class TaskStatus(StrEnum):
    NEEDS_ROUTING = "needs_routing"  # G2: 라우터 확신 부족 → 사용자 지정 대기
    HELD = "held"                    # G3: 사람 승인 필요 → approve 대기
    QUEUED = "queued"
    RUNNING = "running"              # worker 실행 중
    DELIVERED = "delivered"          # interactive 세션에 전달됨(회신 대기)
    DONE = "done"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.TIMEOUT, TaskStatus.CANCELLED)


class Sandbox(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"

    @staticmethod
    def narrower(a: "Sandbox | None", b: "Sandbox | None") -> "Sandbox":
        """둘 중 더 좁은 권한. None 은 제약 없음으로 본다."""
        if Sandbox.READ_ONLY in (a, b):
            return Sandbox.READ_ONLY
        return Sandbox.WORKSPACE_WRITE


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_task_id() -> str:
    return f"t_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{secrets.token_hex(3)}"


def validate_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not _NAME_RE.match(n) or n in RESERVED_NAMES:
        raise BrokerError(f"잘못된 세션 이름 '{name}': 소문자·숫자·_·- 32자 이내, 예약어({', '.join(sorted(RESERVED_NAMES))}) 불가")
    return n


_TITLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]*$")


def normalize_name(title: str | None) -> str | None:
    """Claude/Codex rename 이름 → 브로커 이름. 'Project Lead' → 'project-lead'.

    영문·숫자·공백·_·.·- 로만 된 이름만 바꾼다. 한글 등이 섞인 이름(Codex 자동 제목 포함)은 None.
    """
    t = (title or "").strip()
    if not _TITLE_RE.match(t):
        return None
    n = re.sub(r"[\s.]+", "-", t.lower())
    n = re.sub(r"-{2,}", "-", n).strip("-_")[:32].rstrip("-_")
    return n if _NAME_RE.match(n) and n not in RESERVED_NAMES else None


def split_qualified(ref: str) -> tuple[str | None, str]:
    """'codex:builder' → ('codex', 'builder'), 'builder' → (None, 'builder').
    rename 이름 그대로('Project Lead')도 받아 브로커 이름으로 바꾼다."""
    r = (ref or "").strip().lower()
    r = re.sub(r"-{2,}", "-", re.sub(r"\s+", "-", r))
    m = _QUALIFIED_RE.match(r)
    if m:
        return m.group(1), m.group(2)
    return None, r


def extract_mention(body: str) -> tuple[str | None, str]:
    """본문 앞의 '@이름' 을 떼어 낸다. (지정값, 나머지 본문)."""
    m = _MENTION_RE.match(body or "")
    if not m:
        return None, body
    return m.group(1).lower(), body[m.end():]


@dataclass(slots=True)
class Session:
    name: str                          # 전역 유일 이름 (주소)
    provider: str
    mode: SessionMode
    roles: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    cwd: str | None = None
    native_id: str | None = None      # Claude session_id / Codex thread_id
    sandbox: Sandbox = Sandbox.WORKSPACE_WRITE  # 이 세션이 허용하는 최대 권한
    model: str | None = None
    busy_task_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def qualified(self) -> str:
        return f"{self.provider}:{self.name}"

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name, "provider": self.provider, "mode": self.mode.value, "roles": self.roles,
            "aliases": self.aliases, "description": self.description, "cwd": self.cwd,
            "sandbox": self.sandbox.value, "busy_task_id": self.busy_task_id,
            "has_native_id": bool(self.native_id),
        }


@dataclass(slots=True)
class Role:
    """라우팅 단위. Jev 는 역할 중에서 고른다."""
    name: str
    description: str
    preferred_provider: str | None = None


@dataclass(slots=True)
class Task:
    id: str
    kind: TaskKind
    from_addr: str
    to_addr: str | None
    body: str
    title: str = ""
    to_hint: str | None = None         # 사용자가 준 원래 지정값/힌트
    status: TaskStatus = TaskStatus.QUEUED
    acceptance: list[str] = field(default_factory=list)
    reply_schema: dict[str, Any] | None = None
    sandbox: Sandbox | None = None     # None → 대상 세션 기본값
    deadline_s: int = 1800
    hop: int = 0
    root_id: str | None = None
    parent_id: str | None = None       # 이 작업을 만든 작업(작업 안에서 send 한 경우)
    reply_for: str | None = None       # kind=reply 일 때 원 작업 ID
    resume_on_reply: bool = False      # 완료 시 발신 세션을 결과와 함께 다시 깨운다
    result: str | None = None
    result_json: dict[str, Any] | None = None
    error: str | None = None
    routed_by: str | None = None       # name | alias | role | rules | jev | user
    route_meta: dict[str, Any] | None = None
    reply_seen: bool = False           # 발신자가 결과를 inbox 로 확인했는가
    created_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "task_id": self.id, "kind": self.kind.value, "from": self.from_addr, "to": self.to_addr,
            "title": self.title, "status": self.status.value, "hop": self.hop, "routed_by": self.routed_by,
            "sandbox": self.sandbox.value if self.sandbox else None,
            "created_at": self.created_at, "finished_at": self.finished_at,
        }
