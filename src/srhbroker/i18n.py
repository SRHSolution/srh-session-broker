"""화면 언어 — 웹 대시보드 API·watch·예시 데이터가 쓰는 한국어 원문 → 영어 문구.

- 원문(한국어)을 그대로 열쇠로 쓴다: tr("{name} 창이 닫혀 있어 …", name=…). 한국어면 원문, 영어면 EN 의 번역에 값을 채운다.
- 언어는 요청·명령마다 use(lang) 으로 정한다(ContextVar — 대시보드의 요청 스레드마다 따로). 정하지 않으면 한국어.
- 설정 [ui] lang = "auto" | "ko" | "en". auto 는 시스템 언어(LANG·Windows UI 언어)가 한국어면 ko, 아니면 en.
  웹 대시보드는 auto 일 때 브라우저 언어를 따르고, 화면 위 버튼으로 바꿀 수 있다.
- DB 에 이미 한국어로 저장된 문구(라우팅 사유·오류·승인 확인 문구)는 stored() 가 보여 줄 때 바꾼다.
- CLI 메시지·MCP 도구 결과는 아직 한국어다.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

LANGS = ("ko", "en")
_LANG: ContextVar[str] = ContextVar("srhbroker_lang", default="ko")


def system_lang() -> str:
    """시스템 언어: LC_ALL·LC_MESSAGES·LANG, 없으면 Windows UI 언어. 한국어가 아니면 en."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var, "")
        if v and v not in ("C", "POSIX") and not v.startswith("C."):
            return "ko" if v.lower().startswith("ko") else "en"
    if sys.platform == "win32":
        try:
            import ctypes
            return "ko" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x12 else "en"  # LANG_KOREAN
        except (AttributeError, OSError):
            pass
    return "en"


def setting(cfg: Any) -> str:
    """config.toml [ui] lang 값 (auto·ko·en, 그 밖의 값은 auto)."""
    v = str((getattr(cfg, "data", None) or {}).get("ui", {}).get("lang", "auto")).lower()
    return v if v in LANGS else "auto"


def resolve(value: str | None) -> str:
    v = (value or "").lower()
    return v if v in LANGS else system_lang()


def config_lang(cfg: Any) -> str:
    return resolve(setting(cfg))


def current() -> str:
    return _LANG.get()


@contextmanager
def use(lang: str | None) -> Iterator[str]:
    tok = _LANG.set(lang if lang in LANGS else "ko")
    try:
        yield _LANG.get()
    finally:
        _LANG.reset(tok)


def tr(ko: str, /, **kw: Any) -> str:
    """ko 가 '원문|구분' 이면 한국어는 원문만 쓰고, 영어는 구분까지 포함한 열쇠로 찾는다 (같은 원문, 다른 번역)."""
    s = EN.get(ko) if _LANG.get() == "en" else None
    if s is None:
        s = ko.split("|", 1)[0]
    return s.format(**kw) if kw else s


def stored(text: str | None) -> str | None:
    """DB 에 한국어로 남은 문구를 지금 언어로 (모르는 문구는 그대로)."""
    if not text or _LANG.get() != "en":
        return text
    if text in STORED:
        return STORED[text]
    for pat, repl in STORED_RE:
        m = pat.fullmatch(text)
        if m:
            return repl.format(*m.groups())
    return text


# ── 영어 번역 (열쇠 = 코드의 한국어 원문) ────────────────────────────────────

EN: dict[str, str] = {
    # 승인 대기 사유
    "장비 제어 코드 변경": "Device-control code change",
    "되돌리기 어려운 작업·중요 코드 변경": "Hard-to-undo action or critical code change",
    "사람 승인 필요": "Needs human approval",
    "대상 {targets}": "target {targets}",
    "대상 {targets} + 동작 {actions}": "target {targets} + action {actions}",
    "Jev 사람 승인 필요 {pct}": "Jev: needs a human {pct}",
    # 처리할 것
    "{name} 창이 닫혀 있어 메시지 {n}건이 대기 중": "{name}'s pane is closed — {n} message(s) waiting",
    "창을 열면(resume) 데몬이 넣습니다.": "The engine types them in when the pane opens (resume).",
    "그 창은 '{other}' 세션 창입니다(창 제목). 이 세션은 창 없이(백그라운드 등) 실행 중일 수 있습니다":
        "that pane belongs to the '{other}' session (pane title). This session may be running without a pane, "
        "in the background for example",
    "종류·폴더가 다릅니다": "the agent kind or folder differs",
    "{name}: herdr 창 짝 불일치": "{name}: herdr pane mismatch",
    "herdr 가 창 {pane} 을 이 세션으로 보지만 {why}. 메시지는 이 세션이 응답을 마칠 때 받습니다. 바로 받게 하려면 자기 창에서 다시 여세요.":
        "herdr pairs pane {pane} with this session, but {why}. The session gets messages when it finishes a "
        "response. To get them right away, reopen it in its own pane.",
    "데몬이 실행 중이 아닙니다": "The engine is not running",
    "herdr 창 하나에서 `srhbroker daemon` 을 띄우세요 (플러그인: `{cmd}`).":
        "Run `srhbroker daemon` in a herdr pane (plugin: `{cmd}`).",
    "데몬이 herdr 창 밖에서 실행 중": "The engine is running outside herdr",
    "interactive 세션에 바로 넣지 못합니다. herdr 창에서 다시 띄우세요.":
        "It cannot type into interactive sessions. Restart it in a herdr pane.",
    "데몬 재시작 필요": "Engine restart needed",
    "데몬 시작 뒤 코드가 바뀌었습니다. Ctrl+C 후 `srhbroker daemon`.":
        "The code changed after the engine started. Press Ctrl+C, then run `srhbroker daemon`.",
    # 중계 경과: 단계 이름
    "보냄": "Sent", "라우팅": "Routed", "안전 판단": "Safety check", "승인 판단": "Approval check", "승인": "Approved",
    "취소": "Cancelled", "소멸": "Dropped", "대상 지정": "Routed by you", "실행": "Started", "전달": "Delivered",
    "대기": "Waiting", "회신": "Reply", "만료": "Expired", "알림": "Notified",
    # 중계 경과: 설명
    "작업 (회신 필요)": "task (reply expected)", "메시지 (회신 없음)": "message (no reply)", "회신 알림": "reply notice",
    "(대상 생략)": "(no target)",
    "Jev 판단: {top}": "Jev: {top}",
    "역할 선택 없음 (이름 지정)": "no role chosen (named target)",
    " · 하드웨어 위험 {pct}": " · hardware risk {pct}",
    " · 사람 승인 필요 {pct}": " · needs a human {pct}",
    " · 참고만 (적용 안 함)": " · advisory only (not applied)",
    " · 오류 {error}": " · error {error}",
    "세션 대화창": "the session's chat", "터미널": "the terminal", "대시보드": "the dashboard", "watch 화면": "watch",
    "{via} 에서 승인": "Approved in {via}",
    " · 권한 {perm}": " · permission {perm}",
    "넣기 전": "before delivery", "받는 쪽이 처리 중": "while the receiver was working", "승인 대기 중": "while awaiting approval",
    "대상 미정 상태": "while it had no target", "실행 중": "while running",
    "사람(터미널·대시보드)이 취소 ({was})": "Cancelled by a person in the terminal or dashboard ({was})",
    "{who} 가 취소 ({was})": "Cancelled by {who} ({was})",
    "취소 ({was})": "Cancelled ({was})",
    "{name} 창이 닫혀 있어 알림을 기다리지 않고 버림 (알림은 대기열에 두지 않음)":
        "{name}'s pane was closed, so the notice was dropped (notices are not queued)",
    "사람이 대상을 {to} 로 지정": "A person routed it to {to}",
    "이름 일치": "name match", "별칭 일치": "alias match", "역할": "role", "규칙": "rule", "Jev 판단": "Jev",
    "유일한 후보": "only candidate", "대상 못 정함": "no target found",
    "대상 미정 (사람이 지정해야 함)": "no target (a person must route it)",
    "사용자가 직접 지시한 작업 — 승인 생략": "The user asked for this directly — no approval needed",
    "알림·회신이라 승인 대상 아님": "A notice or reply — not subject to approval",
    "읽기 전용 요청이라 승인 대상 아님": "A read-only request — not subject to approval",
    "장비 관련이지만 변경을 실행하게 하는 요청은 아님 — 승인 없이 읽기 전용으로 전달":
        "Mentions hardware but asks for no change — sent read-only without approval",
    "→ 사용자 직접 지시라 요청한 권한 유지": "→ kept the requested permission (the user asked directly)",
    "→ 읽기 전용으로 낮춤": "→ lowered to read-only",
    "하드웨어 대상({targets}) + 제어 동작({actions}) {lowered}": "Hardware target ({targets}) + control action ({actions}) {lowered}",
    "하드웨어 경계면 규칙({rules})에 걸려 읽기 전용으로 낮춤": "Matched hardware boundary rules ({rules}) → lowered to read-only",
    "Jev 하드웨어 위험 판단 {lowered}": "Jev judged it a hardware risk {lowered}",
    "영상·장비 용어({terms})만 있어 위험을 소폭 반영 (읽기 전용 아님)":
        "Only imaging or device terms ({terms}), so the risk was raised slightly (not read-only)",
    "장비 제어 코드 변경 → 승인 대기로 보류": "Device-control code change → held for approval",
    "사람 승인 필요 {pct} (되돌리기 어려운 작업·중요 코드 변경) → 승인 대기로 보류":
        "Needs human approval {pct} (hard-to-undo action or critical code change) → held for approval",
    "데몬이 worker 로 실행 시작": "the engine started it as a worker",
    "herdr 로 받는 창에 바로 넣음": "typed straight into the receiving pane through herdr",
    "받는 쪽이 응답을 마칠 때(Stop hook)나 inbox 로 가져감":
        "picked up when the receiver finished a response (Stop hook) or from its inbox",
    "{to} 창에 아직 넣지 못함 (창이 닫혀 있거나 승인 창)":
        "Not typed into {to}'s pane yet (the pane is closed or showing a prompt)",
    "완료로 회신": "replied: done", "실패로 회신": "replied: failed", "기한 초과": "deadline passed", "취소됨": "cancelled",
    "결과를 보낸 창({name})에 바로 넣음": "The result was typed straight into the sender's pane ({name})",
    "같은 provider 직접 전달 (broker 작업 아님)": "same-provider direct delivery (not a broker task)",
    "herdr 로 받는 창에 넣음": "typed into the receiving pane through herdr",
    "받는 창이 열리거나 풀리면 넣음": "typed in when the receiving pane opens or frees up",
    "받는 창이 닫혀 있어 알림을 버림": "the receiving pane was closed, so the notice was dropped",
    "넣지 않고 끝남": "ended without being delivered",
    # 상세
    "관찰 기록 {id} 이 없습니다": "No observed record {id}",
    "직접 전달 {id} 이 없습니다": "No direct delivery {id}",
    "(본문을 저장하지 않았습니다 — [monitor] store_body_chars)": "(body not stored — [monitor] store_body_chars)",
    "Claude 자체 SendMessage 를 관찰한 기록입니다 (broker 가 전달하지 않음). 본문은 앞 {n}자까지만 저장되며, 이전 기록은 200자까지만 남아 있습니다.":
        "Observed from Claude's own SendMessage (the broker did not deliver it). Only the first {n} characters of "
        "the body are stored; older records keep 200.",
    " (세션 안 에이전트 팀원 또는 미등록 대상)": " (an agent teammate inside the session, or an unregistered target)",
    "잘못된 검색 조건": "Invalid search", "잘못된 요청": "Invalid request",
    # watch (터미널 대시보드)
    "{n}초": "{n}s", "{n}분": "{n}m", "{n}시간": "{n}h",
    "승인 대기": "Approval", "대상 미정": "No target", "창 닫힘": "Pane closed", "실패": "Failed", "창 짝 불일치": "Pane mismatch",
    "엔진": "Engine", "중계": "Relay", "Codex직접": "Codex dir",
    "데몬 꺼짐": "Engine stopped", "데몬 실행(이전 버전)": "Engine running (old version)", "데몬 실행": "Engine running",
    " pid {pid} · 신호 {ago} 전 · ": " pid {pid} · heartbeat {ago} ago · ",
    "herdr 전달 가능": "herdr delivery on", "herdr 밖": "outside herdr", "재시작 필요": "restart needed",
    "새 버전 {v} — srhbroker update": "new version {v} — srhbroker update",
    " 24시간: 중계 {broker} · Claude 직접 {native} · Codex 직접 {direct}":
        " 24h: relayed {broker} · Claude direct {native} · Codex direct {direct}",
    " · 전달 중앙값 {s}초": " · median delivery {s}s",
    "처리할 것 ({n})": "Needs you ({n})", "  없음": "  nothing",
    "세션": "Sessions", "이름": "Name", "종류": "Kind", "창": "Pane", "받을것": "Inbox", "처리중": "Active", "승인대기": "Held", "역할|열": "Roles",
    "닫힘": "closed", "{pane} 불일치": "{pane} mismatch",
    "  * herdr 짝이 어긋나 창 제목으로 찾은 창": "  * found by pane title because the herdr pairing is off",
    "전체": "all", "Claude 직접": "Claude direct", "Codex 직접": "Codex direct",
    "흐름 ({title})": "Flows ({title})", "  f: 필터 전환": "  f: change filter",
    " a 승인 · c 취소 · t 대상 지정 · d 상세 · f 필터 · r 새로고침 · q 종료   (작업은 ID 또는 [번호])":
        " a approve · c cancel · t route · d detail · f filter · r refresh · q quit   (pick a task by ID or [number])",
    "작업 ID 또는 [번호]: ": "Task ID or [number]: ",
    "  권한 {perm}": "  permission {perm}",
    "제목: {title}\n\n중계 경과:": "Title: {title}\n\nRelay steps:",
    "아직": "not yet",
    "+{n}초": "+{n}s", "+{n}분": "+{n}m",
    "\n보낸 내용:\n{body}\n": "\nRequest:\n{body}\n",
    "결과:\n": "Result:\n",
    "\nEnter 로 돌아가기": "\nPress Enter to go back",
    "{tid} 를 취소할까요? (y/N) ": "Cancel {tid}? (y/N) ",
    "취소 안 함": "Not cancelled",
    "취소: {status}": "Cancelled: {status}",
    "제목: {title}\n보류 사유: {safety}": "Title: {title}\nHeld because: {safety}",
    "{tid} 를 승인할까요? (y/N) ": "Approve {tid}? (y/N) ",
    "승인 안 함": "Not approved",
    "승인: {status}": "Approved: {status}",
    "보낼 대상 (세션 이름·별칭·역할): ": "Send to (session name, alias or role): ",
    "대상 지정: {to}": "Routed to {to}",
    "오류: {e}": "Error: {e}",
    # 예시 데이터 (srhbroker demo — 영어 화면이면 영어 예시)
    "설계·작업 분해 (Claude)": "Design and task breakdown (Claude)",
    "웹 프런트엔드 구현 (Codex)": "Web front end (Codex)",
    "API·DB 구현 (Codex)": "API and database (Codex)",
    "코드 리뷰 (Claude, 읽기 전용)": "Code review (Claude, read-only)",
    "문서 정리 (Claude)": "Docs (Claude)",
    "빌드·테스트 (데몬이 실행하는 Codex)": "Build and test (Codex run by the engine)",
}

# DB 에 저장된 한국어 문구 (라우터 사유·오류·승인 확인 문구)
STORED: dict[str, str] = {
    "후보가 하나뿐": "only one candidate",
    "확신 부족": "not confident enough",
    "규칙에 해당 없음 (Jev 는 그림자 모드: 제안만)": "no rule matched (Jev is in shadow mode: suggestion only)",
    "규칙에 해당 없음": "no rule matched",
    "취소됨": "cancelled",
    "받는 창이 닫혀 있어 알림 소멸": "the receiving pane was closed, so the notice was dropped",
    "데몬 재시작으로 중단됨": "stopped because the engine restarted",
    "대시보드에서 사용자가 승인 버튼을 누름": "the user pressed Approve in the dashboard",
    "watch 화면에서 사용자가 승인": "approved by the user in watch",
}
STORED_RE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(\w+) 일치"), "{0} match"),
    (re.compile(r"역할 '(.+)' 에 등록된 세션이 없습니다"), "no session is registered for role '{0}'"),
    (re.compile(r"역할 '(.+)'"), "role '{0}'"),
    (re.compile(r"규칙 /(.+)/ → (.+)"), "rule /{0}/ → {1}"),
    (re.compile(r"세션 '(.+)' 이 삭제되었습니다"), "session '{0}' was removed"),
    (re.compile(r"시간 초과\((.+)\)"), "timed out ({0})"),
    (re.compile(r"대기 기한 초과 \((\w+) 상태로 처리되지 않음\)"), "waited too long (still {0})"),
]
