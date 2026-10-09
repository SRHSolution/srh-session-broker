"""SQLite 저장소. 여러 프로세스(세션마다 뜨는 MCP 서버 + 디스패처 데몬)가 같은 DB 를 공유한다.

- WAL 모드 + busy_timeout 으로 동시 접근을 처리한다.
- 상태 전이는 조건부 UPDATE(compare-and-set)로 원자적으로 수행한다.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .models import (
    BrokerError, Sandbox, Session, SessionMode, Task, TaskKind, TaskStatus, now_iso, split_qualified,
    validate_name,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    name TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    mode TEXT NOT NULL,
    roles TEXT NOT NULL DEFAULT '[]',
    aliases TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    cwd TEXT,
    native_id TEXT,
    sandbox TEXT NOT NULL,
    model TEXT,
    busy_task_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT PRIMARY KEY,
    name TEXT NOT NULL REFERENCES sessions(name) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    from_addr TEXT NOT NULL,
    to_addr TEXT,
    to_hint TEXT,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    acceptance TEXT NOT NULL DEFAULT '[]',
    reply_schema TEXT,
    sandbox TEXT,
    deadline_s INTEGER NOT NULL,
    hop INTEGER NOT NULL DEFAULT 0,
    root_id TEXT,
    parent_id TEXT,
    reply_for TEXT,
    resume_on_reply INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    result_json TEXT,
    error TEXT,
    routed_by TEXT,
    route_meta TEXT,
    reply_seen INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_to_status ON tasks(to_addr, status);
CREATE INDEX IF NOT EXISTS ix_tasks_from ON tasks(from_addr, status, reply_seen);
CREATE INDEX IF NOT EXISTS ix_tasks_root ON tasks(root_id);
-- 같은 provider 직접 전달 대기열 (Codex ↔ Codex). broker 작업이 아니다: Jev·안전 판단·회신 추적 없음.
-- 받는 창이 닫혀 있거나 승인 창이 떠 있으면 여기서 기다렸다가 herdr 로 넣는다
CREATE TABLE IF NOT EXISTS direct_queue (
    id TEXT PRIMARY KEY,          -- d_...
    created_at TEXT NOT NULL,
    provider TEXT,
    from_name TEXT NOT NULL,
    to_name TEXT NOT NULL,
    request TEXT NOT NULL,        -- JSON: body·title·kind·acceptance·reply_schema
    status TEXT NOT NULL,         -- queued | delivered | cancelled | expired | dropped(창이 닫혀 알림 소멸)
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_direct_to ON direct_queue(to_name, status);
-- broker 가 중계하지 않는 흐름(관찰 기록): Claude 자체 SendMessage
CREATE TABLE IF NOT EXISTS flow_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    transport TEXT NOT NULL,      -- claude-native | herdr-direct
    provider TEXT,
    from_name TEXT,               -- 등록된 이름 (없으면 NULL)
    from_native TEXT,             -- Claude session_id / Codex thread_id
    to_name TEXT,                 -- 등록된 이름으로 해석되면
    to_raw TEXT,                  -- 원래 대상 표기 (SendMessage 의 to 등)
    status TEXT NOT NULL,         -- sent | delivered | not_delivered
    summary TEXT                  -- 본문 앞부분 (monitor.store_body_chars)
);
CREATE INDEX IF NOT EXISTS ix_flow_at ON flow_log(at);
CREATE TABLE IF NOT EXISTS push_log (
    session TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_push_log ON push_log(session, at);
CREATE TABLE IF NOT EXISTS route_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    router TEXT NOT NULL,
    decision TEXT,
    confidence REAL,
    applied INTEGER NOT NULL,
    meta TEXT,
    created_at TEXT NOT NULL
);
"""

_TASK_COLS = (
    "id", "kind", "from_addr", "to_addr", "to_hint", "title", "body", "status", "acceptance", "reply_schema",
    "sandbox", "deadline_s", "hop", "root_id", "parent_id", "reply_for", "resume_on_reply", "result",
    "result_json", "error", "routed_by", "route_meta", "reply_seen", "created_at", "started_at", "finished_at",
)


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _uj(v: str | None) -> Any:
    return None if v is None else json.loads(v)


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """쓰기 트랜잭션. BEGIN IMMEDIATE 로 쓰기 잠금을 먼저 잡아 경쟁 상태를 막는다."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ── 세션 ─────────────────────────────────────────────────────────────

    def upsert_session(self, s: Session) -> Session:
        s.name = validate_name(s.name)
        aliases = [validate_name(a) for a in s.aliases]
        s.aliases = aliases
        with self.tx() as c:
            # 이름·별칭 전역 유일성 검사 (세션 이름 ↔ 별칭 교차 포함)
            for a in aliases:
                if a == s.name:
                    raise BrokerError(f"별칭 '{a}' 이 세션 이름과 같습니다")
                row = c.execute("SELECT name FROM aliases WHERE alias=?", (a,)).fetchone()
                if row and row["name"] != s.name:
                    raise BrokerError(f"별칭 '{a}' 은 이미 세션 '{row['name']}' 이 사용 중입니다")
                if c.execute("SELECT 1 FROM sessions WHERE name=?", (a,)).fetchone():
                    raise BrokerError(f"별칭 '{a}' 이 다른 세션 이름과 겹칩니다")
            if c.execute("SELECT 1 FROM aliases WHERE alias=? AND name<>?", (s.name, s.name)).fetchone():
                raise BrokerError(f"이름 '{s.name}' 이 다른 세션의 별칭과 겹칩니다")
            old = c.execute("SELECT * FROM sessions WHERE name=?", (s.name,)).fetchone()
            if old and old["provider"] != s.provider:
                raise BrokerError(f"세션 '{s.name}' 은 이미 {old['provider']} 로 등록되어 있습니다 (이름은 provider 와 무관하게 유일)")
            if old:
                s.created_at = old["created_at"]
                s.native_id = s.native_id or old["native_id"]
                s.busy_task_id = old["busy_task_id"]
            s.updated_at = now_iso()
            c.execute(
                "INSERT OR REPLACE INTO sessions(name, provider, mode, roles, aliases, description, cwd, native_id,"
                " sandbox, model, busy_task_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.name, s.provider, s.mode.value, _j(s.roles), _j(aliases), s.description, s.cwd, s.native_id,
                 s.sandbox.value, s.model, s.busy_task_id, s.created_at, s.updated_at),
            )
            c.execute("DELETE FROM aliases WHERE name=?", (s.name,))
            c.executemany("INSERT INTO aliases(alias, name) VALUES (?,?)", [(a, s.name) for a in aliases])
        return s

    def rename_session(self, old: str, new: str, *, replace: bool = False) -> Session:
        """등록 이름을 바꾼다. 이전 이름은 별칭으로 남기고, 그 이름 앞으로 쌓인 작업·회신·직접 전달도 새 이름으로 옮긴다.
        replace: new 를 쓰던 다른 세션 등록(닫힌 이전 세션)을 지우고 그 별칭을 넘겨받는다."""
        new = validate_name(new)
        with self.tx() as c:
            row = c.execute("SELECT * FROM sessions WHERE name=?", (old,)).fetchone()
            if not row:
                raise BrokerError(f"등록되지 않은 세션 '{old}'")
            src = self._row_session(row)
            inherited: list[str] = []
            other = c.execute("SELECT * FROM sessions WHERE name=?", (new,)).fetchone()
            if other:
                if not replace:
                    raise BrokerError(f"이름 '{new}' 은 다른 세션이 쓰고 있습니다")
                inherited = _uj(other["aliases"]) or []
                c.execute("DELETE FROM aliases WHERE name=?", (new,))
                c.execute("DELETE FROM sessions WHERE name=?", (new,))
            a = c.execute("SELECT name FROM aliases WHERE alias=?", (new,)).fetchone()
            if a and a["name"] != old:
                raise BrokerError(f"이름 '{new}' 은 세션 '{a['name']}' 의 별칭입니다")
            aliases = [x for x in dict.fromkeys([*src.aliases, *inherited, old]) if x != new]
            c.execute("DELETE FROM aliases WHERE name=?", (old,))
            c.execute("UPDATE sessions SET name=?, aliases=?, updated_at=? WHERE name=?", (new, _j(aliases), now_iso(), old))
            c.executemany("INSERT INTO aliases(alias, name) VALUES (?,?)", [(x, new) for x in aliases])
            for sql in ("UPDATE tasks SET to_addr=? WHERE to_addr=?", "UPDATE tasks SET from_addr=? WHERE from_addr=?",
                        "UPDATE direct_queue SET to_name=? WHERE to_name=?",
                        "UPDATE direct_queue SET from_name=? WHERE from_name=?",
                        "UPDATE push_log SET session=? WHERE session=?",
                        "UPDATE flow_log SET from_name=? WHERE from_name=?", "UPDATE flow_log SET to_name=? WHERE to_name=?"):
                c.execute(sql, (new, old))
        s = self.get_session(new)
        assert s is not None
        return s

    def remove_session(self, name: str) -> bool:
        with self.tx() as c:
            c.execute("DELETE FROM aliases WHERE name=?", (name,))
            return c.execute("DELETE FROM sessions WHERE name=?", (name,)).rowcount > 0

    @staticmethod
    def _row_session(r: sqlite3.Row) -> Session:
        return Session(
            name=r["name"], provider=r["provider"], mode=SessionMode(r["mode"]), roles=_uj(r["roles"]) or [],
            aliases=_uj(r["aliases"]) or [], description=r["description"], cwd=r["cwd"], native_id=r["native_id"],
            sandbox=Sandbox(r["sandbox"]), model=r["model"], busy_task_id=r["busy_task_id"],
            created_at=r["created_at"], updated_at=r["updated_at"],
        )

    def get_session(self, name: str) -> Session | None:
        rows = self._q("SELECT * FROM sessions WHERE name=?", (name,))
        return self._row_session(rows[0]) if rows else None

    def list_sessions(self) -> list[Session]:
        return [self._row_session(r) for r in self._q("SELECT * FROM sessions ORDER BY name")]

    def resolve_name(self, ref: str) -> tuple[Session, str] | None:
        """이름·별칭·'provider:name' 을 세션으로 해석. (세션, 'name'|'alias')."""
        provider, n = split_qualified(ref)
        s = self.get_session(n)
        how = "name"
        if s is None:
            rows = self._q("SELECT name FROM aliases WHERE alias=?", (n,))
            if not rows:
                return None
            s, how = self.get_session(rows[0]["name"]), "alias"
        if s is None:
            return None
        if provider and provider != s.provider:
            raise BrokerError(f"'{ref}': 세션 '{s.name}' 은 {s.provider} 세션입니다")
        return s, how

    def find_by_native_id(self, native_id: str) -> Session | None:
        rows = self._q("SELECT * FROM sessions WHERE native_id=?", (native_id,))
        return self._row_session(rows[0]) if rows else None

    def set_native_id(self, name: str, native_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE sessions SET native_id=?, updated_at=? WHERE name=?", (native_id, now_iso(), name))

    def try_claim_session(self, name: str, task_id: str) -> bool:
        """세션 실행 잠금(G5). 이미 다른 작업이 실행 중이면 False."""
        with self.tx() as c:
            return c.execute(
                "UPDATE sessions SET busy_task_id=?, updated_at=? WHERE name=? AND busy_task_id IS NULL",
                (task_id, now_iso(), name),
            ).rowcount == 1

    def release_session(self, name: str, task_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE sessions SET busy_task_id=NULL, updated_at=? WHERE name=? AND busy_task_id=?",
                      (now_iso(), name, task_id))

    def release_all_sessions(self) -> int:
        """데몬 재시작 시 잔여 잠금 해제."""
        with self.tx() as c:
            return c.execute("UPDATE sessions SET busy_task_id=NULL WHERE busy_task_id IS NOT NULL").rowcount

    # ── 작업 ─────────────────────────────────────────────────────────────

    def insert_task(self, t: Task) -> Task:
        with self.tx() as c:
            c.execute(f"INSERT INTO tasks({', '.join(_TASK_COLS)}) VALUES ({', '.join('?' * len(_TASK_COLS))})",
                      self._task_values(t))
        return t

    @staticmethod
    def _task_values(t: Task) -> tuple:
        return (
            t.id, t.kind.value, t.from_addr, t.to_addr, t.to_hint, t.title, t.body, t.status.value, _j(t.acceptance),
            _j(t.reply_schema), t.sandbox.value if t.sandbox else None, t.deadline_s, t.hop, t.root_id, t.parent_id,
            t.reply_for, int(t.resume_on_reply), t.result, _j(t.result_json), t.error, t.routed_by, _j(t.route_meta),
            int(t.reply_seen), t.created_at, t.started_at, t.finished_at,
        )

    @staticmethod
    def _row_task(r: sqlite3.Row) -> Task:
        return Task(
            id=r["id"], kind=TaskKind(r["kind"]), from_addr=r["from_addr"], to_addr=r["to_addr"], to_hint=r["to_hint"],
            title=r["title"], body=r["body"], status=TaskStatus(r["status"]), acceptance=_uj(r["acceptance"]) or [],
            reply_schema=_uj(r["reply_schema"]), sandbox=Sandbox(r["sandbox"]) if r["sandbox"] else None,
            deadline_s=r["deadline_s"], hop=r["hop"], root_id=r["root_id"], parent_id=r["parent_id"],
            reply_for=r["reply_for"], resume_on_reply=bool(r["resume_on_reply"]), result=r["result"],
            result_json=_uj(r["result_json"]), error=r["error"], routed_by=r["routed_by"],
            route_meta=_uj(r["route_meta"]), reply_seen=bool(r["reply_seen"]), created_at=r["created_at"],
            started_at=r["started_at"], finished_at=r["finished_at"],
        )

    def get_task(self, task_id: str) -> Task | None:
        rows = self._q("SELECT * FROM tasks WHERE id=?", (task_id,))
        return self._row_task(rows[0]) if rows else None

    def require_task(self, task_id: str) -> Task:
        t = self.get_task(task_id)
        if t is None:
            raise BrokerError(f"작업 '{task_id}' 이 없습니다")
        return t

    def list_tasks(self, *, status: str | None = None, limit: int = 30, from_addr: str | None = None,
                   active: bool = False) -> list[Task]:
        """최근 작업. from_addr: 그 세션이 보낸 것만, active: 아직 끝나지 않은 것만(queued·running·delivered·held·needs_routing)."""
        where, args = [], []
        if status:
            where.append("status=?"); args.append(status)
        if active:
            where.append("status IN ('queued','running','delivered','held','needs_routing')")
        if from_addr:
            where.append("from_addr=?"); args.append(from_addr)
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        return [self._row_task(r) for r in self._q(sql, (*args, limit))]

    def expire_stale(self, cutoff_iso: str) -> list[Task]:
        """created_at 이 cutoff 보다 오래됐는데 아직 대기(held·needs_routing·queued) 중인 작업을 timeout 으로 끝낸다."""
        rows = self._q("SELECT * FROM tasks WHERE status IN ('held','needs_routing','queued') AND created_at < ?",
                       (cutoff_iso,))
        done: list[Task] = []
        for t in (self._row_task(r) for r in rows):
            if self.transition(t.id, (t.status,), TaskStatus.TIMEOUT,
                               error=f"대기 기한 초과 ({t.status.value} 상태로 처리되지 않음)"):
                done.append(t)
        return done

    def status_counts(self, to_name: str) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._q(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE to_addr=? GROUP BY status", (to_name,))}

    def count_root(self, root_id: str) -> int:
        return self._q("SELECT COUNT(*) AS n FROM tasks WHERE root_id=?", (root_id,))[0]["n"]

    def transition(self, task_id: str, from_status: tuple[TaskStatus, ...], to_status: TaskStatus,
                   **fields: Any) -> bool:
        """조건부 상태 전이(CAS). 현재 상태가 from_status 중 하나일 때만 바뀐다."""
        sets = ["status=?"]
        vals: list[Any] = [to_status.value]
        for k, v in fields.items():
            if k in ("result_json", "route_meta", "acceptance", "reply_schema"):
                v = _j(v)
            elif isinstance(v, (Sandbox, TaskStatus, TaskKind)):
                v = v.value
            elif isinstance(v, bool):
                v = int(v)
            sets.append(f"{k}=?")
            vals.append(v)
        if to_status.terminal and "finished_at" not in fields:
            sets.append("finished_at=?")
            vals.append(now_iso())
        ph = ",".join("?" * len(from_status))
        with self.tx() as c:
            return c.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id=? AND status IN ({ph})",
                (*vals, task_id, *[s.value for s in from_status]),
            ).rowcount == 1

    def queued_for_workers(self, limit: int = 50) -> list[Task]:
        rows = self._q(
            "SELECT t.* FROM tasks t JOIN sessions s ON s.name = t.to_addr "
            "WHERE t.status='queued' AND s.mode='worker' ORDER BY t.created_at LIMIT ?", (limit,))
        return [self._row_task(r) for r in rows]

    def running_tasks(self) -> list[Task]:
        return [self._row_task(r) for r in self._q("SELECT * FROM tasks WHERE status='running'")]

    def inbox(self, name: str, *, mark: bool) -> dict[str, list[Task]]:
        """interactive 세션의 받은 편지함.
        incoming: 나에게 온 queued 작업·메시지 (mark=True 면 delivered/done 으로 전이)
        replies : 내가 보낸 작업 중 끝났는데 아직 확인 안 한 것 (mark=True 면 확인 처리)
        """
        incoming = [self._row_task(r) for r in self._q(
            "SELECT * FROM tasks WHERE to_addr=? AND status='queued' ORDER BY created_at", (name,))]
        replies = [self._row_task(r) for r in self._q(
            "SELECT * FROM tasks WHERE from_addr=? AND reply_seen=0 AND kind<>'message' "
            "AND status IN ('done','failed','timeout','cancelled') ORDER BY finished_at", (name,))]
        if mark:
            got: list[Task] = []
            for t in incoming:
                # task 는 회신 대기(delivered), message/reply 는 전달로 끝(done)
                to = TaskStatus.DELIVERED if t.kind == TaskKind.TASK else TaskStatus.DONE
                if self.transition(t.id, (TaskStatus.QUEUED,), to, started_at=now_iso()):
                    t.status = to
                    got.append(t)
            incoming = got
            seen: list[Task] = []
            with self.tx() as c:
                for t in replies:  # 여러 프로세스(hook·herdr 전달)가 동시에 꺼내도 한쪽만 갖도록 CAS
                    if c.execute("UPDATE tasks SET reply_seen=1 WHERE id=? AND reply_seen=0", (t.id,)).rowcount:
                        seen.append(t)
            replies = seen
        return {"incoming": incoming, "replies": replies}

    def unclaim(self, box: dict[str, list[Task]]) -> None:
        """inbox(mark=True) 로 꺼낸 것을 되돌린다 (전달 실패 시)."""
        with self.tx() as c:
            for t in box["incoming"]:
                c.execute("UPDATE tasks SET status='queued', started_at=NULL WHERE id=? AND status=?",
                          (t.id, t.status.value))
            for t in box["replies"]:
                c.execute("UPDATE tasks SET reply_seen=0 WHERE id=?", (t.id,))

    def pending_count(self, name: str) -> int:
        r = self._q("SELECT (SELECT COUNT(*) FROM tasks WHERE to_addr=? AND status='queued') + "
                    "(SELECT COUNT(*) FROM tasks WHERE from_addr=? AND reply_seen=0 AND kind<>'message' "
                    "AND status IN ('done','failed','timeout','cancelled')) + "
                    "(SELECT COUNT(*) FROM direct_queue WHERE to_name=? AND status='queued')", (name, name, name))
        return int(r[0][0])

    # ── 같은 provider 직접 전달 대기열 ─────────────────────────────────────

    @staticmethod
    def _row_direct(r: sqlite3.Row) -> dict[str, Any]:
        return {**dict(r), "request": _uj(r["request"])}

    def enqueue_direct(self, did: str, provider: str, from_name: str, to_name: str, request: dict[str, Any]) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO direct_queue(id, created_at, provider, from_name, to_name, request, status)"
                      " VALUES (?,?,?,?,?,?,'queued')", (did, now_iso(), provider, from_name, to_name, _j(request)))

    def get_direct(self, did: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM direct_queue WHERE id=?", (did,))
        return self._row_direct(rows[0]) if rows else None

    def claim_direct(self, to_name: str) -> list[dict[str, Any]]:
        """받는 세션 앞 대기 항목을 선점(CAS)해 delivered 로 바꾸고 돌려준다."""
        rows = [self._row_direct(r) for r in self._q(
            "SELECT * FROM direct_queue WHERE to_name=? AND status='queued' ORDER BY created_at", (to_name,))]
        got = []
        with self.tx() as c:
            for d in rows:
                if c.execute("UPDATE direct_queue SET status='delivered', delivered_at=? WHERE id=? AND status='queued'",
                             (now_iso(), d["id"])).rowcount:
                    got.append(d)
        return got

    def unclaim_direct(self, items: list[dict[str, Any]]) -> None:
        with self.tx() as c:
            c.executemany("UPDATE direct_queue SET status='queued', delivered_at=NULL WHERE id=? AND status='delivered'",
                          [(d["id"],) for d in items])

    def set_direct_status(self, did: str, frm: str, to: str) -> bool:
        with self.tx() as c:
            return c.execute("UPDATE direct_queue SET status=? WHERE id=? AND status=?", (to, did, frm)).rowcount == 1

    def drop_queued_messages(self, name: str, reason: str) -> tuple[list[str], list[str]]:
        """받는 창이 닫힌 세션 앞으로 대기 중인 알림(kind=message)을 소멸시킨다 — 알림은 창이 열릴 때까지 기다리지 않는다.
        broker 작업은 cancelled(error=reason), 직접 전달은 dropped. 반환: (작업 ID, 직접 전달 ID)"""
        tids = [r["id"] for r in self._q("SELECT id FROM tasks WHERE to_addr=? AND status='queued' AND kind='message'", (name,))]
        tasks = [i for i in tids if self.transition(i, (TaskStatus.QUEUED,), TaskStatus.CANCELLED, error=reason)]
        rows = [self._row_direct(r) for r in self._q("SELECT * FROM direct_queue WHERE to_name=? AND status='queued'", (name,))]
        directs = [d["id"] for d in rows
                   if d["request"].get("kind") == "message" and self.set_direct_status(d["id"], "queued", "dropped")]
        return tasks, directs

    def expire_direct(self, cutoff_iso: str) -> list[dict[str, Any]]:
        rows = [self._row_direct(r) for r in self._q(
            "SELECT * FROM direct_queue WHERE status='queued' AND created_at < ?", (cutoff_iso,))]
        return [d for d in rows if self.set_direct_status(d["id"], "queued", "expired")]

    def list_direct(self, limit: int = 100, *, from_name: str | None = None, active: bool = False) -> list[dict[str, Any]]:
        where, args = [], []
        if from_name:
            where.append("from_name=?"); args.append(from_name)
        if active:
            where.append("status='queued'")
        sql = ("SELECT * FROM direct_queue" + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY created_at DESC LIMIT ?")
        return [self._row_direct(r) for r in self._q(sql, (*args, limit))]

    def direct_queued_count(self, to_name: str) -> int:
        return int(self._q("SELECT COUNT(*) FROM direct_queue WHERE to_name=? AND status='queued'", (to_name,))[0][0])

    def log_flow(self, *, transport: str, provider: str | None, from_name: str | None, from_native: str | None,
                 to_name: str | None, to_raw: str | None, status: str, summary: str | None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO flow_log(at, transport, provider, from_name, from_native, to_name, to_raw, status,"
                      " summary) VALUES (?,?,?,?,?,?,?,?,?)",
                      (now_iso(), transport, provider, from_name, from_native, to_name, to_raw, status, summary))

    def list_flows(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM flow_log ORDER BY id DESC LIMIT ?", (limit,))]

    def get_flow(self, fid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM flow_log WHERE id=?", (fid,))
        return dict(rows[0]) if rows else None

    # ── 대시보드 검색 (세 기록을 같은 조건으로) ────────────────────────────

    @staticmethod
    def _search_where(cols: list[str], q: str | None, who: tuple[str, str], session: str | None,
                      statuses: list[str] | None, time_col: str, since: str | None) -> tuple[str, list[Any]]:
        where, args = [], []
        if q:
            # 검색어의 % _ 는 글자 그대로 찾는다 (이스케이프 문자 '!')
            like = "%" + q.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
            where.append("(" + " OR ".join(f"IFNULL({c},'') LIKE ? ESCAPE '!'" for c in cols) + ")")
            args += [like] * len(cols)
        if session:
            where.append(f"({who[0]}=? OR {who[1]}=?)")
            args += [session, session]
        if statuses is not None:
            if not statuses:
                return "WHERE 0", []
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args += statuses
        if since:
            where.append(f"{time_col}>=?")
            args.append(since)
        return ("WHERE " + " AND ".join(where)) if where else "", args

    def search_tasks(self, *, q=None, session=None, statuses=None, since=None, limit=200) -> list[Task]:
        w, args = self._search_where(["id", "title", "body", "from_addr", "to_addr", "result", "error"], q,
                                     ("from_addr", "to_addr"), session, statuses, "created_at", since)
        return [self._row_task(r) for r in self._q(f"SELECT * FROM tasks {w} ORDER BY created_at DESC LIMIT ?",
                                                   (*args, limit))]

    def search_direct(self, *, q=None, session=None, statuses=None, since=None, limit=200) -> list[dict[str, Any]]:
        w, args = self._search_where(["id", "from_name", "to_name", "request"], q, ("from_name", "to_name"),
                                     session, statuses, "created_at", since)
        return [self._row_direct(r) for r in self._q(
            f"SELECT * FROM direct_queue {w} ORDER BY created_at DESC LIMIT ?", (*args, limit))]

    def search_flow_log(self, *, q=None, session=None, statuses=None, since=None, teammates=True,
                        limit=200) -> list[dict[str, Any]]:
        w, args = self._search_where(["from_name", "to_name", "to_raw", "summary", "from_native"], q,
                                     ("from_name", "to_name"), session, statuses, "at", since)
        w = (w + (" AND " if w else "WHERE ") + "transport<>'herdr-direct'")   # 이전 버전 직접 전달 기록 제외
        if not teammates:
            w += " AND to_name IS NOT NULL"          # 세션 안 에이전트 팀원에게 보낸 것 제외
        return [dict(r) for r in self._q(f"SELECT * FROM flow_log {w} ORDER BY id DESC LIMIT ?", (*args, limit))]

    def log_push(self, name: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO push_log(session, at) VALUES (?,?)", (name, now_iso()))

    def push_times(self, name: str, start_iso: str, end_iso: str) -> list[str]:
        """그 세션 창에 herdr 로 넣은 시각들 (start~end)."""
        return [r["at"] for r in self._q("SELECT at FROM push_log WHERE session=? AND at>=? AND at<=? ORDER BY at",
                                         (name, start_iso, end_iso))]

    def push_count_since(self, name: str, since_iso: str) -> int:
        return int(self._q("SELECT COUNT(*) FROM push_log WHERE session=? AND at>=?", (name, since_iso))[0][0])

    def mark_reply_seen(self, task_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE tasks SET reply_seen=1 WHERE id=?", (task_id,))

    # ── 라우팅 로그 (Jev 그림자 모드 평가용) ─────────────────────────────

    def log_route(self, task_id: str, router: str, decision: str | None, confidence: float | None,
                  applied: bool, meta: dict[str, Any] | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO route_log(task_id, router, decision, confidence, applied, meta, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (task_id, router, decision, confidence, int(applied), _j(meta), now_iso()),
            )

    def route_log(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM route_log ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "meta": _uj(r["meta"])} for r in rows]
