"""Codex CLI 어댑터: `codex exec --json -o <파일> [resume <thread_id>] -` (프롬프트는 stdin).

- 최종 메시지는 -o(--output-last-message) 파일에서 읽는다 (가장 안정적).
- thread ID 는 --json 이벤트 스트림에서 찾는다. 이벤트 이름이 버전마다 달라 여러 키를 허용한다.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from typing import Any

from ..models import Sandbox, Session, Task
from .base import TurnResult, fill, resolve_command, run_process

_ID_KEYS = ("thread_id", "session_id", "conversation_id")


def parse_codex_events(stdout: str) -> dict[str, Any]:
    """JSONL 이벤트에서 thread_id, 마지막 agent 메시지, 오류를 추출.

    주의: {"type":"error","message":"Reconnecting..."} 는 재시도 중 일시 오류다 → 실패로 보지 않는다.
    실패는 turn.failed 이벤트, 또는 turn.completed 없이 끝난 경우에만 판단한다.
    """
    thread_id = last_msg = error = last_transient = None
    completed = False
    usage = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = str(ev.get("type", ""))
        msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else {}
        for src in (ev, msg):
            for k in _ID_KEYS:
                if not thread_id and isinstance(src.get(k), str):
                    thread_id = src[k]
        if typ in ("thread.started", "session.created", "session_configured") and not thread_id:
            thread_id = ev.get("id")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else None
        if item and item.get("type") in ("agent_message", "assistant_message") and isinstance(item.get("text"), str):
            last_msg = item["text"]
        if msg.get("type") == "agent_message" and isinstance(msg.get("message"), str):
            last_msg = msg["message"]
        if typ == "turn.failed":
            e = ev.get("error") or ev.get("message")
            error = e.get("message") if isinstance(e, dict) else str(e)
        elif typ == "error" or msg.get("type") == "error":
            last_transient = str(ev.get("message") or msg.get("message") or "")
        if typ == "turn.completed":
            completed = True
            if isinstance(ev.get("usage"), dict):
                usage = ev["usage"]
    return {"thread_id": thread_id, "last_message": last_msg, "error": error, "usage": usage,
            "completed": completed, "last_transient": last_transient}


class CodexAdapter:
    provider = "codex"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def build_argv(self, session: Session, sandbox: Sandbox, last_file: str) -> list[str]:
        vals = {"native_id": session.native_id, "sandbox": sandbox.value, "last_message_file": last_file,
                "model": session.model}
        args = fill(self.cfg["new_args"] if not session.native_id else self.cfg["resume_args"], vals)
        return [resolve_command(self.cfg["command"]), *args, *self.cfg.get("extra_args", [])]

    async def deliver(self, session: Session, task: Task, prompt: str, sandbox: Sandbox, *,
                      env: dict[str, str], timeout_s: float, is_cancelled: Callable[[], bool]) -> TurnResult:
        fd, last_file = tempfile.mkstemp(prefix="srhbroker_codex_", suffix=".txt")
        os.close(fd)
        try:
            argv = self.build_argv(session, sandbox, last_file)
            pr = await run_process(argv, stdin_text=prompt, cwd=session.cwd, env=env, timeout_s=timeout_s,
                                   is_cancelled=is_cancelled)
            ev = parse_codex_events(pr.stdout)
            tid = ev["thread_id"] or session.native_id
            if pr.cancelled:
                return TurnResult(ok=False, error="취소됨", native_id=tid)
            if pr.timed_out:
                return TurnResult(ok=False, error=f"시간 초과({timeout_s:.0f}s)", native_id=tid)
            try:
                with open(last_file, encoding="utf-8") as f:
                    text = f.read().strip()
            except OSError:
                text = ""
            text = text or (ev["last_message"] or "")
            if pr.returncode != 0 or ev["error"] or not ev["completed"]:
                detail = ev["error"] or ev["last_transient"] or pr.stderr.strip()[-800:] or "turn.completed 없음"
                return TurnResult(ok=False, native_id=tid, text=text,
                                  error=f"codex 오류 (rc={pr.returncode}): {detail}")
            if not tid:
                return TurnResult(ok=True, text=text, native_id=None,
                                  meta={"warning": "thread_id 를 찾지 못했습니다 — 다음 턴은 새 스레드로 시작됩니다",
                                        "usage": ev["usage"]})
            return TurnResult(ok=True, text=text, native_id=tid, meta={"usage": ev["usage"]})
        finally:
            try:
                os.unlink(last_file)
            except OSError:
                pass
