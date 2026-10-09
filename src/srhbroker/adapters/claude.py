"""Claude Code 어댑터: `claude -p --output-format json [--session-id|--resume] <id>` (프롬프트는 stdin)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from ..models import Sandbox, Session, Task
from .base import TurnResult, fill, resolve_command, run_process


def parse_claude_output(stdout: str) -> dict[str, Any] | None:
    """--output-format json 결과(단일 객체). 앞뒤에 다른 출력이 섞여도 마지막 result 객체를 찾는다."""
    text = stdout.strip()
    if not text:
        return None
    try:
        v = json.loads(text)
        if isinstance(v, list):  # 일부 버전은 이벤트 배열을 출력
            v = next((e for e in reversed(v) if isinstance(e, dict) and e.get("type") == "result"), None)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(v, dict) and v.get("type") == "result":
                return v
    return None


class ClaudeAdapter:
    provider = "claude"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def build_argv(self, session: Session, sandbox: Sandbox, native_id: str, is_new: bool) -> list[str]:
        perm = self.cfg.get("permission_modes", {}).get(sandbox.value, "plan")
        vals = {"native_id": native_id, "permission_mode": perm, "model": session.model}
        args = fill(self.cfg["new_args"] if is_new else self.cfg["resume_args"], vals)
        return [resolve_command(self.cfg["command"]), *args, *self.cfg.get("extra_args", [])]

    async def deliver(self, session: Session, task: Task, prompt: str, sandbox: Sandbox, *,
                      env: dict[str, str], timeout_s: float, is_cancelled: Callable[[], bool]) -> TurnResult:
        is_new = not session.native_id
        native_id = session.native_id or str(uuid.uuid4())  # 새 세션은 ID 를 미리 정해 --session-id 로 생성
        argv = self.build_argv(session, sandbox, native_id, is_new)
        pr = await run_process(argv, stdin_text=prompt, cwd=session.cwd, env=env, timeout_s=timeout_s,
                               is_cancelled=is_cancelled)
        if pr.cancelled:
            return TurnResult(ok=False, error="취소됨", native_id=native_id)
        if pr.timed_out:
            return TurnResult(ok=False, error=f"시간 초과({timeout_s:.0f}s)", native_id=native_id)
        out = parse_claude_output(pr.stdout)
        if out is None:
            return TurnResult(ok=False, native_id=native_id if not is_new else None,
                              error=f"claude 출력 해석 실패 (rc={pr.returncode}): {pr.stderr.strip()[-800:] or pr.stdout[-800:]}")
        sid = out.get("session_id") or native_id
        if out.get("is_error") or out.get("subtype") not in (None, "success"):
            return TurnResult(ok=False, native_id=sid, text=str(out.get("result") or ""),
                              error=f"claude 오류: {out.get('subtype')} {str(out.get('result') or '')[:500]}")
        return TurnResult(ok=True, text=str(out.get("result") or ""), native_id=sid,
                          meta={"cost_usd": out.get("total_cost_usd"), "num_turns": out.get("num_turns"),
                                "duration_ms": out.get("duration_ms")})
