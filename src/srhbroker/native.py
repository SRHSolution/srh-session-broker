"""Claude Code / Codex 가 자체 저장하는 세션 정보(세션 ID·rename 이름·작업 폴더) 조회.

- Claude: <CLAUDE_CONFIG_DIR|~/.claude>/projects/<프로젝트>/<session_id>.jsonl
  rename 이름은 {"type":"custom-title","customTitle":...} 줄로 남고 마지막 줄이 현재 이름이다.
- Codex : <CODEX_HOME|~/.codex>/sessions/YYYY/MM/DD/rollout-...-<thread_id>.jsonl (첫 줄 session_meta 에 cwd)
  이름은 session_index.jsonl 의 thread_name (같은 id 의 마지막 줄이 현재 이름). rename 하지 않으면 자동 제목이 들어간다.

실행 중인 세션이 자기 ID 를 아는 방법 (claude 2.1 / codex-cli 0.159 에서 확인):
- Claude MCP 서버·hook: 환경 변수 CLAUDE_CODE_SESSION_ID, hook stdin 의 session_id
- Codex MCP 서버: 도구 호출 _meta.threadId (환경 변수로는 전달되지 않는다), hook stdin 의 session_id
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class NativeSession:
    provider: str
    id: str
    title: str | None
    cwd: str | None
    updated: float  # 마지막 수정 시각 (epoch)


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _line_at(data: bytes, idx: int) -> dict[str, Any] | None:
    start = data.rfind(b"\n", 0, idx) + 1
    end = data.find(b"\n", idx)
    try:
        v = json.loads(data[start:end if end != -1 else len(data)])
        return v if isinstance(v, dict) else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _read_claude(path: Path) -> NativeSession:
    # 대화 기록은 수십 MB 까지 커질 수 있어 줄 단위 파싱 대신 바이트 검색으로 필요한 줄만 읽는다
    data = path.read_bytes()
    title = cwd = None
    i = data.rfind(b'"custom-title"')
    if i != -1 and (o := _line_at(data, i)):
        title = o.get("customTitle")
    i = data.find(b'"cwd":')
    if i != -1 and (o := _line_at(data, i)):
        cwd = o.get("cwd")
    return NativeSession("claude", path.stem, title, cwd, path.stat().st_mtime)


def _codex_titles() -> dict[str, str]:
    out: dict[str, str] = {}
    p = codex_home() / "session_index.jsonl"
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                o = json.loads(line)
                if o.get("id") and o.get("thread_name"):
                    out[o["id"]] = o["thread_name"]
            except json.JSONDecodeError:
                continue
    return out


def _read_codex(path: Path, titles: dict[str, str]) -> NativeSession:
    sid, cwd = path.stem[-36:], None
    with path.open(encoding="utf-8", errors="replace") as f:
        try:
            meta = json.loads(f.readline())
            if meta.get("type") == "session_meta":
                sid = meta["payload"].get("id", sid)
                cwd = meta["payload"].get("cwd")
        except (json.JSONDecodeError, KeyError):
            pass
    return NativeSession("codex", sid, titles.get(sid), cwd, path.stat().st_mtime)


def find(session_id: str, provider: str | None = None, transcript_path: str | None = None) -> NativeSession | None:
    """세션 ID 로 provider 세션 정보를 찾는다. hook 이 넘겨준 transcript_path 가 있으면 그 파일을 바로 읽는다."""
    if not session_id:
        return None
    tp = Path(transcript_path) if transcript_path else None
    if provider in (None, "claude"):
        path = tp if tp and tp.is_file() and tp.stem == session_id else None
        path = path or next((claude_home() / "projects").glob(f"*/{session_id}.jsonl"), None)
        if path:
            return _read_claude(path)
    if provider in (None, "codex"):
        path = tp if tp and tp.is_file() and tp.stem.endswith(session_id) else None
        path = path or next((codex_home() / "sessions").rglob(f"rollout-*{session_id}.jsonl"), None)
        if path:
            return _read_codex(path, _codex_titles())
    return None


def recent(days: float = 7, *, provider: str | None = None, titled_only: bool = True) -> list[NativeSession]:
    """최근 수정된 세션 목록 (최신순). titled_only 면 이름이 있는 세션만."""
    cut = time.time() - days * 86400
    out: list[NativeSession] = []
    if provider in (None, "claude"):
        for p in (claude_home() / "projects").glob("*/*.jsonl"):
            if p.stat().st_mtime >= cut:
                out.append(_read_claude(p))
    if provider in (None, "codex"):
        titles = _codex_titles()
        for p in (codex_home() / "sessions").rglob("rollout-*.jsonl"):
            if p.stat().st_mtime >= cut:
                out.append(_read_codex(p, titles))
    if titled_only:
        out = [s for s in out if s.title]
    return sorted(out, key=lambda s: -s.updated)
