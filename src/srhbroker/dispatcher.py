"""디스패처 데몬: queued 작업을 worker 세션에 전달하고 결과를 회신으로 기록한다.

- 세션(주소)별 동시 실행 1개 (G5), 전체 동시 실행 max_parallel.
- 데몬은 한 PC 에서 하나만 실행한다 (잠금 파일).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import IO, Any

from .adapters import Adapter, build_adapters, envelope
from .models import Sandbox, Task, TaskStatus
from .service import Broker

log = logging.getLogger(__name__)


def code_mtime() -> float:
    """설치된 srhbroker 코드 중 가장 최근 수정 시각 (데몬이 이후 바뀐 코드로 재시작해야 하는지 판단)."""
    return max(p.stat().st_mtime for p in Path(__file__).parent.rglob("*.py"))


class SingleInstance:
    """프로세스 단일 실행 잠금 (Windows: msvcrt, POSIX: fcntl)."""

    def __init__(self, path: Path):
        self.path = path
        self._f: IO[Any] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self._f = f
        return True

    def release(self) -> None:
        if self._f:
            self._f.close()
            self._f = None


class Dispatcher:
    def __init__(self, broker: Broker, adapters: dict[str, Adapter] | None = None):
        self.broker = broker
        self.store = broker.store
        self.cfg = broker.cfg
        self.adapters = adapters or build_adapters(self.cfg.data["adapters"])
        self.max_parallel = int(self.cfg.broker["max_parallel"])
        self.poll_s = float(self.cfg.broker["poll_interval_s"])
        self._running: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()

    def recover(self) -> None:
        """재시작 복구: 이전 데몬이 남긴 잠금 해제, 실행 중이던 작업은 실패 처리."""
        n = self.store.release_all_sessions()
        for t in self.store.running_tasks():
            self.store.transition(t.id, (TaskStatus.RUNNING,), TaskStatus.FAILED, error="데몬 재시작으로 중단됨")
        if n:
            log.info("세션 잠금 %d개 해제", n)

    def stop(self) -> None:
        self._stop.set()

    async def deliver_interactive(self) -> None:
        """interactive 세션의 밀린 메시지를 herdr 창에 넣는다 (데몬이 herdr 창 안에서 돌 때만 동작)."""
        if not self.broker.herdr.available():
            return
        for s in self.store.list_sessions():
            if s.mode.value == "interactive" and self.store.pending_count(s.name):
                await asyncio.to_thread(self.broker.deliver_now, s.name)

    async def tick(self) -> int:
        """한 번 훑어서 시작 가능한 작업을 실행한다. 시작한 개수를 반환."""
        started = 0
        for t in self.store.queued_for_workers():
            if len(self._running) >= self.max_parallel:
                break
            if not t.to_addr or not self.store.try_claim_session(t.to_addr, t.id):
                continue
            if not self.store.transition(t.id, (TaskStatus.QUEUED,), TaskStatus.RUNNING,
                                         started_at=self.broker.timestamp()):
                self.store.release_session(t.to_addr, t.id)
                continue
            self._running[t.id] = asyncio.create_task(self._run(t), name=f"run:{t.id}")
            started += 1
        return started

    async def _run(self, t: Task) -> None:
        name = t.to_addr or ""
        try:
            s = self.store.get_session(name)
            if s is None:
                self.broker.complete(t.id, TaskStatus.FAILED, error=f"세션 '{name}' 이 삭제되었습니다",
                                     from_status=(TaskStatus.RUNNING,))
                return
            sandbox = Sandbox.narrower(t.sandbox, s.sandbox)
            env = {"SRHBROKER_SELF": s.name, "SRHBROKER_TASK": t.id, "SRHBROKER_HOME": str(self.cfg.home)}

            def is_cancelled() -> bool:
                cur = self.store.get_task(t.id)
                return cur is None or cur.status == TaskStatus.CANCELLED

            log.info("▶ %s → %s (%s, %s)", t.id, s.name, s.provider, sandbox.value)
            res = await self.adapters[s.provider].deliver(
                s, t, envelope(t, s, sandbox), sandbox, env=env, timeout_s=float(t.deadline_s),
                is_cancelled=is_cancelled)
            if res.ok and res.native_id and res.native_id != s.native_id:
                self.store.set_native_id(s.name, res.native_id)
            if is_cancelled():
                log.info("■ %s 취소됨", t.id)
            elif res.ok:
                # worker 가 턴 중에 reply 도구로 이미 회신했다면 전이는 실패하고 그 회신이 유지된다
                self.broker.complete(t.id, TaskStatus.DONE, result=res.text, from_status=(TaskStatus.RUNNING,))
                log.info("✔ %s 완료", t.id)
            else:
                st = TaskStatus.TIMEOUT if (res.error or "").startswith("시간 초과") else TaskStatus.FAILED
                self.broker.complete(t.id, st, result=res.text or None, error=res.error,
                                     from_status=(TaskStatus.RUNNING,))
                log.warning("✘ %s %s: %s", t.id, st.value, res.error)
        except Exception as e:  # 어댑터 예외도 작업 실패로 기록하고 데몬은 계속 돈다
            log.exception("작업 %s 실행 오류", t.id)
            self.broker.complete(t.id, TaskStatus.FAILED, error=f"{type(e).__name__}: {e}",
                                 from_status=(TaskStatus.RUNNING,))
        finally:
            self.store.release_session(name, t.id)
            self._running.pop(t.id, None)

    async def drain(self) -> None:
        """실행 중인 작업이 끝날 때까지 대기 (테스트·종료용)."""
        while self._running:
            await asyncio.gather(*list(self._running.values()), return_exceptions=True)

    def write_state(self, started: float) -> None:
        """srhbroker status 가 읽는 데몬 상태 (실행 정보·herdr 여부·마지막 신호·시작 시점 코드 시각)."""
        state = {"pid": os.getpid(), "started_at": started, "heartbeat": time.time(),
                 "herdr": self.broker.herdr.available(), "herdr_pane": os.environ.get("HERDR_PANE_ID"),
                 "code_mtime": code_mtime(), "db": str(self.store.path)}
        try:
            (self.cfg.home / "daemon.json").write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass

    async def run_forever(self) -> None:
        self.recover()
        started = time.time()
        log.info("디스패처 시작 (DB: %s, 동시 실행 %d, herdr 전달 %s)", self.store.path, self.max_parallel,
                 "가능" if self.broker.herdr.available() else "불가 — herdr 창 안에서 실행해야 interactive 세션에 바로 넣는다")
        n = 0
        while not self._stop.is_set():
            try:
                await self.tick()
                await self.deliver_interactive()
                if n % 60 == 0:  # 약 1분마다: 오래 대기한 작업 만료
                    expired = await asyncio.to_thread(self.broker.expire_stale)
                    if expired:
                        log.info("대기 기한 초과로 만료: %s", expired)
                if n % 5 == 0:
                    self.write_state(started)
                n += 1
            except Exception:
                log.exception("tick 오류")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_s)
            except asyncio.TimeoutError:
                pass
        await self.drain()
