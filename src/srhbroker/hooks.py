"""Claude Code Stop hook: interactive 세션이 응답을 마칠 때 받은 편지함을 확인해 주입한다.

settings.json 예:
  "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "srhbroker hook claude-stop"}]}]}

세션 이름 결정 순서: --as → SRHBROKER_SELF → stdin 의 session_id 로 등록된 세션 찾기.
`srhbroker register --session <ID>` 로 등록해 두면 환경 변수 없이 평소처럼 열어도 자기 편지함을 받는다.
등록되지 않은 세션에서는 아무것도 하지 않는다. 브로커가 띄운 worker 세션(SRHBROKER_TASK 설정됨)도 마찬가지.

- 받은 메시지가 있으면 {"decision": "block", "reason": <내용>} 을 출력 → Claude 가 이어서 처리한다.
- 메시지는 꺼내는 즉시 delivered 로 표시되므로 같은 메시지로 무한 반복되지 않는다.
- 이름으로 찾은 경우 stdin 의 session_id 로 세션의 native_id 를 자동 갱신한다.
- 세션을 rename 했으면 새 이름을 별칭으로 추가한다 (Broker.sync_title).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from . import native
from .service import Broker


def _debug(d: Path, provider: str, payload: dict[str, Any], **info: Any) -> None:
    """브로커 홈에 hook-debug 파일이 있으면 hook 입력·판단을 hook-debug.log 에 남긴다 (문제 진단용)."""
    if not (d / "hook-debug").exists():
        return
    rec = {"at": datetime.now().isoformat(timespec="seconds"), "provider": provider,
           "payload_keys": sorted(payload), "session_id": payload.get("session_id"),
           "env": {k: os.environ.get(k) for k in ("SRHBROKER_SELF", "SRHBROKER_TASK", "SRHBROKER_HOME")}, **info}
    with (d / "hook-debug.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def claude_stop(broker: Broker, name: str | None, stdin: TextIO | None = None, stdout: TextIO | None = None,
                provider: str = "claude") -> int:
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    name = name or os.environ.get("SRHBROKER_SELF")
    if os.environ.get("SRHBROKER_TASK"):
        return 0
    payload: dict[str, Any] = {}
    raw, err = "", None
    try:
        # Windows 기본 stdin 인코딩(cp949)으로 읽으면 한글이 든 JSON(Codex 의 last_assistant_message 등)이 깨진다
        buf = getattr(stdin, "buffer", None)
        if buf is not None:
            raw = buf.read().decode("utf-8-sig", "replace")
        elif stdin is not None:  # hook 실행기가 stdin 을 넘기지 않으면 None 일 수 있다
            raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError) as e:
        err = repr(e)[:200]
    sid = payload.get("session_id")
    _debug(broker.cfg.home, provider, payload, name=name, db=str(broker.cfg.db_path), raw_len=len(raw), error=err)
    if not name and not sid:
        return 0
    try:
        if name:
            r = broker.store.resolve_name(name)
            if not r:
                print(f"srhbroker hook: 등록되지 않은 세션 '{name}'", file=sys.stderr)
                return 0
            s = r[0]
            if sid:
                broker.touch_native_id(s.name, sid)
        elif not (s := broker.identify(sid)):
            return 0  # 브로커에 등록되지 않은 일반 세션
        name = s.name
        if sid and (ns := native.find(sid, provider, payload.get("transcript_path"))):
            broker.sync_title(s, ns.title)
        box = broker.inbox(name, mark=True)
    except Exception as e:  # hook 오류로 사용자의 세션을 막지 않는다
        print(f"srhbroker hook 오류: {e}", file=sys.stderr)
        return 0
    if not box["incoming"] and not box["replies"]:
        return 0
    reason = "[SRH Broker] 새 메시지가 도착했습니다. 아래 내용을 처리하세요.\n\n" + broker.render_inbox(box)
    stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    stdout.flush()
    return 0


def claude_observe(broker: Broker, stdin: TextIO | None = None) -> int:
    """Claude PostToolUse hook (matcher: SendMessage): broker 를 거치지 않는 Claude ↔ Claude 메시지를 관찰 기록만 한다.
    전달에는 관여하지 않고 아무것도 출력하지 않는다. 대시보드의 흐름에 보이게 하기 위한 것."""
    stdin = sys.stdin if stdin is None else stdin
    buf = getattr(stdin, "buffer", None)
    raw = buf.read().decode("utf-8-sig", "replace") if buf is not None else (stdin.read() if stdin else "")
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    if payload.get("tool_name") != "SendMessage":
        return 0
    ti = payload.get("tool_input") or {}
    to, message = (ti.get("to") or "").strip(), ti.get("message")
    if not to or to == "main" or message in (None, ""):   # 하위 에이전트 → 본 대화, 구독만 하는 호출은 제외
        return 0
    if payload.get("agent_id"):
        # 하위 에이전트(팀원)가 보낸 것: 팀원끼리·팀장에게 보내는 세션 내부 대화는 기록하지 않는다.
        # 등록된 다른 세션으로 보낸 것만 남긴다 (세션 간 흐름)
        try:
            if not broker.store.resolve_name(to.split(" [")[0].strip()):
                return 0
        except Exception:
            return 0
    broker.observe_native(provider="claude", from_native=payload.get("session_id"), to_raw=to, message=message)
    return 0
