"""herdr 연동: 받는 세션이 herdr 창에서 쉬고 있으면 받은 메시지를 프롬프트로 바로 넣는다.

- herdr `agent list` 의 agent_session.value 가 Claude session_id / Codex thread_id 와 같다
  → 등록된 세션의 native_id 로 지금 그 세션이 떠 있는 창(pane)을 찾는다 (pane ID 는 창을 옮기면 바뀐다).
- herdr 가 관리하는 창 안에서 실행 중일 때(HERDR_ENV=1)만 동작한다. herdr CLI 는 그 창의
  HERDR_SOCKET_PATH 로 현재 세션 서버와 통신한다.
- `agent prompt` 는 bracketed paste + Enter 를 한 번에 보낸다. 상태가 idle·done 일 때만 보낸다
  (working 중에 넣으면 사용자 입력·진행 중인 턴과 섞인다).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Any

log = logging.getLogger(__name__)

READY = ("idle", "done")
# 번호 선택 화면(선택된 항목이 '1.' 로 시작): Codex 업데이트 안내 '› 1. Update now', Claude 확인 창 '❯ 1. Yes' 등.
# herdr 는 이런 화면을 idle 로 볼 때가 있다 — 그 상태에서 프롬프트+Enter 를 넣으면 메뉴가 선택된다 (실제로 겪음)
_MARK_RE = re.compile(r"^\s*[›❯]")
_MENU_RE = re.compile(r"^\s*[›❯]\s*1\.\s")


def _norm_path(p: str | None) -> str:
    return (p or "").replace("/", "\\").rstrip("\\").lower()


def same_pane(agent: dict[str, Any], provider: str, cwd: str | None) -> bool:
    """herdr 가 짝지은 창이 정말 그 세션의 창인지: 에이전트 종류와 (둘 다 알면) 작업 폴더가 같아야 한다."""
    if agent.get("agent") and agent["agent"] != provider:
        return False
    return not (cwd and agent.get("cwd")) or _norm_path(cwd) == _norm_path(agent["cwd"])


def looks_like_menu(screen: str) -> bool:
    marked = [ln for ln in screen.splitlines() if _MARK_RE.match(ln)]
    return bool(marked) and bool(_MENU_RE.match(marked[-1]))


class Herdr:
    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = cfg or {}
        self.timeout_s = float(self.cfg.get("timeout_s", 15))

    def available(self) -> bool:
        return bool(self.cfg.get("enabled", True)) and os.environ.get("HERDR_ENV") == "1" and self.binary() is not None

    def binary(self) -> str | None:
        # herdr 가 창에 넘겨 주는 HERDR_BIN_PATH 가 가장 정확하다 (PATH 에 없을 수 있음)
        return self.cfg.get("command") or os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")

    def _exec(self, *args: str, timeout_s: float | None = None) -> str | None:
        """herdr CLI 를 실행해 stdout 을 돌려준다. 실패하면 None."""
        exe = self.binary()
        if not exe:
            return None
        try:
            pr = subprocess.run([exe, *args], capture_output=True, timeout=timeout_s or self.timeout_s,
                                stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("herdr %s 실패: %s", args[:2], e)
            return None
        if pr.returncode != 0:
            log.warning("herdr %s 실패(code %s): %s", args[:2], pr.returncode,
                        pr.stderr.decode("utf-8", "replace")[:300])
            return None
        return pr.stdout.decode("utf-8", "replace")

    def _run(self, *args: str, timeout_s: float | None = None) -> dict[str, Any] | None:
        out = self._exec(*args, timeout_s=timeout_s)
        if out is None:
            return None
        try:
            return json.loads(out or "{}")
        except json.JSONDecodeError:
            return {}

    def agents(self) -> list[dict[str, Any]]:
        out = self._run("agent", "list")
        return ((out or {}).get("result") or {}).get("agents") or []

    def find(self, native_id: str | None) -> dict[str, Any] | None:
        if not native_id:
            return None
        return next((a for a in self.agents() if (a.get("agent_session") or {}).get("value") == native_id), None)

    def at_menu(self, pane_id: str) -> bool:
        """창이 번호 선택 화면이면 True (그때는 넣지 않는다).
        화면의 맨 아래 입력 표시줄(›·❯)만 본다: 평소에는 입력칸, 메뉴가 떠 있으면 선택된 '1.' 항목이다.
        (위쪽 대화 기록에 '› 1. …' 이 있어도 메뉴로 보지 않는다)"""
        return looks_like_menu(self._exec("pane", "read", pane_id, "--source", "detection") or "")

    def prompt(self, pane_id: str, text: str) -> bool:
        return self._run("agent", "prompt", pane_id, text) is not None

    def wait_ready(self, pane_id: str, timeout_s: float) -> bool:
        """창이 idle·done 이 될 때까지 기다린다 (herdr agent wait 의 기본 = idle·done·blocked)."""
        args = ["agent", "wait", pane_id, "--until", "idle", "--until", "done", "--timeout", str(int(timeout_s * 1000))]
        return self._run(*args, timeout_s=timeout_s + 10) is not None


def spawn_waiter(name: str) -> None:
    """받는 창이 바쁠 때: 쉬는 순간 넣어 주는 백그라운드 프로세스(srhbroker deliver <이름> --wait)를 띄운다."""
    exe = shutil.which("srhbroker", path=os.path.dirname(sys.executable)) or shutil.which("srhbroker")
    argv = [exe, "deliver", name, "--wait"] if exe else [sys.executable, "-m", "srhbroker.cli", "deliver", name, "--wait"]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags, start_new_session=sys.platform != "win32")
    except OSError as e:
        log.warning("전달 대기 프로세스 실행 실패: %s", e)
