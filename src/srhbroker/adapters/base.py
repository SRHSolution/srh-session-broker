"""어댑터 공통: 프로세스 실행(Windows 호환), 프롬프트 봉투, 결과 형식."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import Sandbox, Session, Task

IS_WINDOWS = sys.platform == "win32"


@dataclass(slots=True)
class TurnResult:
    ok: bool
    text: str = ""
    native_id: str | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProcResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False


class Adapter(Protocol):
    provider: str

    async def deliver(self, session: Session, task: Task, prompt: str, sandbox: Sandbox, *,
                      env: dict[str, str], timeout_s: float, is_cancelled: Callable[[], bool]) -> TurnResult: ...


def resolve_command(cmd: str) -> str:
    """'claude' → Windows 에서는 claude.cmd 등 실제 경로로 해석."""
    found = shutil.which(cmd)
    if not found:
        raise FileNotFoundError(f"실행 파일 '{cmd}' 를 PATH 에서 찾을 수 없습니다")
    return found


def fill(args: list[str], values: dict[str, str | None]) -> list[str]:
    """{placeholder} 치환. 값이 None 인 placeholder 가 들어간 인자는 앞의 옵션과 함께 뺀다."""
    out: list[str] = []
    for a in args:
        keys = [k for k in values if "{" + k + "}" in a]
        if any(values[k] is None for k in keys):
            if out and out[-1].startswith("-"):
                out.pop()
            continue
        for k in keys:
            a = a.replace("{" + k + "}", str(values[k]))
        out.append(a)
    return out


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if IS_WINDOWS:
            # .cmd shim → node 자식 프로세스까지 종료
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        pass


def child_env(extra: dict[str, str]) -> dict[str, str]:
    """worker 로 띄우는 claude/codex 의 환경. HERDR_* 는 넘기지 않는다:
    herdr 창 안의 데몬이 띄운 자식 Claude 의 SessionStart hook 이 HERDR_PANE_ID 로 자기 세션을 보고하면
    herdr 가 그 창(데몬 창)을 worker 세션과 짝지어 버린다."""
    return {**{k: v for k, v in os.environ.items() if not k.upper().startswith("HERDR_")}, **extra}


async def run_process(argv: list[str], *, stdin_text: str, cwd: str | None, env: dict[str, str],
                      timeout_s: float, is_cancelled: Callable[[], bool], poll_s: float = 1.0) -> ProcResult:
    """프롬프트는 stdin 으로 전달한다 (argv 이스케이프·길이 제한 회피)."""
    kwargs: dict[str, Any] = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=cwd or None, env=child_env(env), **kwargs)
    comm = asyncio.ensure_future(proc.communicate(stdin_text.encode("utf-8")))
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    timed_out = cancelled = False
    while not comm.done():
        done, _ = await asyncio.wait({comm}, timeout=poll_s)
        if done:
            break
        if is_cancelled():
            cancelled = True
        elif loop.time() >= end:
            timed_out = True
        if cancelled or timed_out:
            await _kill_tree(proc)
            break
    try:
        out, err = await asyncio.wait_for(comm, 15)
    except (asyncio.TimeoutError, Exception):
        out, err = b"", b""
    return ProcResult(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                      timed_out=timed_out, cancelled=cancelled)


def envelope(task: Task, session: Session, sandbox: Sandbox) -> str:
    """worker 세션에 넘기는 프롬프트. 최종 응답이 자동으로 회신된다는 점을 명시한다."""
    parts = [
        f"[SRH Broker] 작업 {task.id} — 보낸 세션: {task.from_addr} → 담당: {session.name}",
        f"제목: {task.title}",
        f"권한: {sandbox.value}" + (" (파일을 수정하지 말고 분석·리뷰만 하세요)" if sandbox == Sandbox.READ_ONLY else ""),
        "",
        task.body,
    ]
    if task.acceptance:
        parts += ["", "완료 조건:", *[f"- {a}" for a in task.acceptance]]
    if task.reply_schema:
        import json
        parts += ["", "응답 마지막에 다음 스키마의 JSON 을 ```json 코드블록으로 포함하세요:",
                  json.dumps(task.reply_schema, ensure_ascii=False)]
    parts += ["", "이 턴의 최종 응답이 그대로 발신자에게 회신됩니다. 다른 세션의 도움이 필요하면 "
              "같은 provider에는 기본 통신 도구로 직접 전달하고, 다른 provider에만 srhbroker MCP 의 send 도구를 쓰세요."]
    return "\n".join(parts)
