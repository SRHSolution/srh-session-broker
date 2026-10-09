"""새 PC 준비: 설치 점검(doctor), Claude·Codex 연결 설정 적용(setup --apply), 예시 데이터(demo).

- doctor 는 읽기만 한다. 고칠 명령을 함께 보여 준다.
- setup --apply 는 사용자 설정 파일을 바꾸기 전에 같은 폴더에 .bak-<시각> 백업을 남기고, 이미 있는 항목은 건드리지 않는다.
- demo 는 지정한 브로커 홈(기본: 임시 폴더)에만 쓴다. 실제 세션·작업에는 손대지 않는다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import native

MCP_TOOLS = ["sessions", "whoami", "register", "send", "route", "approve", "inbox", "reply", "status", "wait",
             "cancel", "recent"]
CODEX_ENV_VARS = ["SRHBROKER_SELF", "SRHBROKER_TASK", "SRHBROKER_HOME", "TYPESAFE_API_KEY",
                  "HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_BIN_PATH", "HERDR_HOME", "HERDR_PANE_ID", "HERDR_TAB_ID",
                  "HERDR_WORKSPACE_ID"]


def exe_command() -> str:
    """hook·MCP 설정에 쓸 srhbroker 실행 명령. PATH 에 있으면 이름만, 없으면 지금 실행 중인 실행 파일의 절대 경로."""
    if shutil.which("srhbroker"):
        return "srhbroker"
    scripts = Path(sys.executable).parent
    for cand in (scripts / "srhbroker.exe", scripts / "srhbroker", scripts / "Scripts" / "srhbroker.exe"):
        if cand.exists():
            return str(cand)
    return "srhbroker"


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    bak = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, bak)
    return bak


def _claude_json() -> Path:
    # Claude Code 는 사용자 범위 MCP 서버를 ~/.claude.json 에 둔다 (CLAUDE_CONFIG_DIR 를 바꾸면 그 안)
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(d) / ".claude.json" if d else Path.home() / ".claude.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _hook_cmds(settings: dict[str, Any], event: str) -> list[str]:
    return [h.get("command", "") for g in (settings.get("hooks") or {}).get(event, []) or []
            for h in g.get("hooks", []) or []]


def _codex_cfg() -> tuple[Path, dict[str, Any]]:
    path = native.codex_home() / "config.toml"
    try:
        return path, tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return path, {}


# ── setup --apply ─────────────────────────────────────────────────────────

def apply_claude(run: Callable[[list[str]], int] | None = None) -> list[str]:
    """Claude Code: 사용자 범위 MCP 서버 등록 + Stop hook(받은 메시지 주입) + PostToolUse 관찰 hook."""
    run = run or (lambda cmd: subprocess.run(cmd, check=False).returncode)
    exe, done = exe_command(), []
    if "srhbroker" in (_read_json(_claude_json()).get("mcpServers") or {}):
        done.append("MCP 서버: 이미 등록됨 (claude mcp list 로 확인)")
    elif shutil.which("claude"):
        rc = run(["claude", "mcp", "add", "--scope", "user", "srhbroker", "--", exe, "mcp"])
        done.append("MCP 서버: 등록함 (claude mcp add --scope user srhbroker)" if rc == 0
                    else f"MCP 서버: 등록 실패(rc={rc}) — 직접 실행: claude mcp add --scope user srhbroker -- {exe} mcp")
    else:
        done.append(f"MCP 서버: claude 명령을 찾지 못함 — Claude Code 설치 후: claude mcp add --scope user srhbroker -- {exe} mcp")

    path = native.claude_home() / "settings.json"
    settings = _read_json(path)
    hooks = settings.setdefault("hooks", {})
    q = f'"{exe}"' if " " in exe else exe       # Claude Code 는 hook 을 셸(bash)로 실행 — 공백 경로만 따옴표
    changed = False
    if not any("hook claude-stop" in c for c in _hook_cmds(settings, "Stop")):
        hooks.setdefault("Stop", []).append({"hooks": [{"type": "command", "command": f"{q} hook claude-stop"}]})
        done.append("Stop hook: 추가함 (응답을 마칠 때 받은 메시지 주입)")
        changed = True
    else:
        done.append("Stop hook: 이미 있음")
    if not any("hook claude-observe" in c for c in _hook_cmds(settings, "PostToolUse")):
        hooks.setdefault("PostToolUse", []).append(
            {"matcher": "SendMessage", "hooks": [{"type": "command", "command": f"{q} hook claude-observe"}]})
        done.append("PostToolUse 관찰 hook: 추가함 (Claude ↔ Claude 메시지를 대시보드에 기록만)")
        changed = True
    else:
        done.append("PostToolUse 관찰 hook: 이미 있음")
    if changed:
        bak = _backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        done.append(f"저장: {path}" + (f" (백업 {bak.name})" if bak else ""))
    return done


def apply_codex() -> list[str]:
    """Codex: config.toml 에 MCP 서버(환경 변수 전달 포함)와 srhbroker 도구 자동 승인을 추가한다."""
    path, cfg = _codex_cfg()
    srv = (cfg.get("mcp_servers") or {}).get("srhbroker")
    if srv:
        missing = [v for v in CODEX_ENV_VARS if v not in (srv.get("env_vars") or [])]
        out = ["MCP 서버: 이미 있음 — 기존 항목은 바꾸지 않습니다"]
        if missing:
            out.append(f"주의: env_vars 에 빠진 변수 {missing} — 직접 추가하세요 (Codex 는 환경 변수를 넘기지 않음)")
        return out
    exe = exe_command()
    if exe != "srhbroker":
        exe = str(Path(exe).resolve())
    block = ["", "# srhbroker — Claude ↔ Codex 세션 브로커 (srhbroker setup codex --apply 로 추가)",
             "[mcp_servers.srhbroker]", f"command = '{exe}'", 'args = ["mcp"]',
             "env_vars = [" + ", ".join(f'"{v}"' for v in CODEX_ENV_VARS) + "]",
             "startup_timeout_sec = 20", "tool_timeout_sec = 3600   # wait 도구가 오래 기다릴 수 있도록", "",
             "# srhbroker 도구 자동 승인 — 승인 창에 걸리면 herdr 즉시 전달이 멈춘다.",
             "# approve 는 AI 가 대화창에서 사용자 승인을 받은 뒤에만 호출한다 (confirmation 필수)"]
    for t in MCP_TOOLS:
        block += [f"[mcp_servers.srhbroker.tools.{t}]", 'approval_mode = "approve"', ""]
    bak = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(block) + "\n")
    return [f"MCP 서버·도구 자동 승인: {path} 에 추가함" + (f" (백업 {bak.name})" if bak else ""),
            "Codex 를 다시 열면 적용됩니다"]


# ── doctor ────────────────────────────────────────────────────────────────

def doctor(b: Any) -> list[dict[str, str]]:
    """설치·연결 점검. 각 항목: level(ok|warn|fail|info), item, detail, fix."""
    from .monitor import daemon_state
    rows: list[dict[str, str]] = []
    add = lambda level, item, detail="", fix="": rows.append({"level": level, "item": item, "detail": detail, "fix": fix})

    add("ok" if sys.version_info >= (3, 11) else "fail", "Python", sys.version.split()[0], "Python 3.11 이상 필요")
    on_path = shutil.which("srhbroker")
    add("ok" if on_path else "warn", "srhbroker 명령", on_path or f"PATH 에 없음 (실행 파일: {exe_command()})",
        "" if on_path else "uv tool install / pipx 로 설치하면 PATH 에 들어갑니다. 아니면 설정에 절대 경로를 씁니다")
    cfg_path = b.cfg.home / "config.toml"
    add("ok" if cfg_path.exists() else "warn", "브로커 설정", str(cfg_path) if cfg_path.exists() else "없음 (기본값 사용)",
        "" if cfg_path.exists() else "srhbroker init")
    n = len(b.store.list_sessions())
    add("ok" if n else "info", "등록된 세션", f"{n}개", "" if n else "각 세션에서 'broker에 이 세션 등록해줘' 또는 srhbroker register --session <ID>")

    mode = b.cfg.router.get("mode", "shadow")
    if mode in ("jev", "shadow"):
        key = bool(os.environ.get("TYPESAFE_API_KEY"))
        try:
            import typesafe_sdk  # noqa: F401
            sdk = True
        except ImportError:
            sdk = False
        lvl = "ok" if key and sdk else "warn"
        add(lvl, f"Jev 라우터 (mode={mode})", f"키 {'있음' if key else '없음'} · SDK {'있음' if sdk else '없음'}",
            "" if lvl == "ok" else "선택 사항 — 없으면 이름·역할·규칙으로만 라우팅. 쓰려면 pip install 'srh-session-broker[jev]' 와 TYPESAFE_API_KEY")
    else:
        add("ok", "라우터", f"mode={mode} (Jev 사용 안 함)")

    # Claude Code
    if shutil.which("claude"):
        mcp = "srhbroker" in (_read_json(_claude_json()).get("mcpServers") or {})
        add("ok" if mcp else "fail", "Claude: MCP 서버", "등록됨" if mcp else "미등록", "" if mcp else "srhbroker setup claude --apply")
        st = _read_json(native.claude_home() / "settings.json")
        stop = any("hook claude-stop" in c for c in _hook_cmds(st, "Stop"))
        add("ok" if stop else "warn", "Claude: Stop hook", "있음" if stop else "없음 (herdr 밖에서 받은 메시지를 받을 수 없음)",
            "" if stop else "srhbroker setup claude --apply")
    else:
        add("info", "Claude Code", "claude 명령 없음 (Claude 세션을 쓰지 않으면 무시)")

    # Codex
    if shutil.which("codex"):
        path, cfg = _codex_cfg()
        srv = (cfg.get("mcp_servers") or {}).get("srhbroker")
        add("ok" if srv else "fail", "Codex: MCP 서버", str(path) if srv else "config.toml 에 없음",
            "" if srv else "srhbroker setup codex --apply")
        if srv:
            missing = [v for v in ("SRHBROKER_HOME", "HERDR_ENV", "HERDR_SOCKET_PATH") if v not in (srv.get("env_vars") or [])]
            add("ok" if not missing else "warn", "Codex: env_vars", "필요한 변수 전달" if not missing else f"빠짐: {missing}",
                "" if not missing else "config.toml [mcp_servers.srhbroker] env_vars 에 추가 (srhbroker setup codex 참고)")
            tools = srv.get("tools") or {}
            auto = sum(1 for t in MCP_TOOLS if (tools.get(t) or {}).get("approval_mode") == "approve")
            add("ok" if auto == len(MCP_TOOLS) else "warn", "Codex: 도구 자동 승인", f"{auto}/{len(MCP_TOOLS)}",
                "" if auto == len(MCP_TOOLS) else "승인 창에 걸리면 즉시 전달이 멈춥니다 — srhbroker setup codex 출력 참고")
    else:
        add("info", "Codex CLI", "codex 명령 없음 (Codex 세션을 쓰지 않으면 무시)")

    # herdr · 데몬
    herdr = b.herdr.binary() if hasattr(b.herdr, "binary") else shutil.which("herdr")
    if herdr:
        add("ok", "herdr", f"{herdr}" + (" · 이 셸은 herdr 창 안" if os.environ.get("HERDR_ENV") == "1" else " · 이 셸은 herdr 밖"))
    else:
        add("info", "herdr (선택)", "없음 — 받는 창에 바로 넣기 대신 Stop hook·inbox 로 받습니다", "https://herdr.dev")
    d = daemon_state(b.cfg.home)
    add("ok" if d.get("running") else "warn", "데몬", "실행 중" if d.get("running") else "실행 중 아님",
        "" if d.get("running") else "herdr 창 하나(또는 터미널)에서 srhbroker daemon")
    return rows


def print_doctor(rows: list[dict[str, str]]) -> int:
    mark = {"ok": "✔", "warn": "!", "fail": "✖", "info": "·"}
    for r in rows:
        print(f" {mark[r['level']]} {r['item']}: {r['detail']}")
        if r["fix"] and r["level"] != "ok":
            print(f"     → {r['fix']}")
    bad = sum(r["level"] == "fail" for r in rows)
    warn = sum(r["level"] == "warn" for r in rows)
    print(f"\n결과: 실패 {bad} · 확인 필요 {warn}")
    return 1 if bad else 0


# ── demo ──────────────────────────────────────────────────────────────────

DEMO_CONFIG = """# srhbroker demo 가 만든 예시 설정 — 실제 설정과 분리된 폴더에만 쓰인다
[router]
mode = "rules"

[[roles]]
name = "planner"
description = "요구사항 분석, 설계, 작업 분해."
preferred_provider = "claude"

[[roles]]
name = "builder"
description = "코드 구현, 버그 수정, 빌드·테스트."
preferred_provider = "codex"

[[roles]]
name = "reviewer"
description = "변경분 코드 리뷰. 읽기 전용."
preferred_provider = "claude"
"""

DEMO_SESSIONS = [
    ("lead-claude", "claude", "interactive", ["planner"], "설계·작업 분해 (Claude)"),
    ("web-codex", "codex", "interactive", ["builder"], "웹 프런트엔드 구현 (Codex)"),
    ("api-codex", "codex", "interactive", ["builder"], "API·DB 구현 (Codex)"),
    ("review-claude", "claude", "interactive", ["reviewer"], "코드 리뷰 (Claude, 읽기 전용)"),
    ("docs-claude", "claude", "interactive", [], "문서 정리 (Claude)"),
    ("ci-codex", "codex", "worker", ["builder"], "빌드·테스트 (데몬이 실행하는 Codex)"),
]
# 대시보드·watch 화면 캡처용 가짜 창 상태 (native_id → herdr 상태)
DEMO_PANES = {"demo-lead-claude": "working", "demo-web-codex": "idle", "demo-api-codex": "working",
              "demo-review-claude": "idle", "demo-docs-claude": "idle"}


class _NoHerdr:
    """예시 데이터를 만드는 동안 실제 herdr 창을 건드리지 않게 한다."""

    def available(self) -> bool:
        return False


async def seed_demo(b: Any) -> dict[str, int]:
    """예시(팬텀) 세션·작업·흐름을 만든다. 모든 이름·내용은 가짜이며 실제 프로젝트와 무관하다."""
    real, b.herdr = b.herdr, _NoHerdr()
    try:
        return await _seed(b)
    finally:
        b.herdr = real


async def _seed(b: Any) -> dict[str, int]:
    from .models import TaskStatus
    for name, prov, mode, roles, desc in DEMO_SESSIONS:
        b.register(name, prov, mode=mode, roles=roles, description=desc, native_id=f"demo-{name}",
                   cwd="~/work/demo-shop", sandbox="read-only" if name.startswith("review") else "workspace-write")
    store = b.store
    now = datetime.now(timezone.utc)

    async def send(body, frm, to, **kw):
        return await b.send(body, sender=frm, to=to, **kw)

    def at(task_id: str, minutes_ago: float, run_after_s: float | None = None, done_after_s: float | None = None):
        c = now - timedelta(minutes=minutes_ago)
        fields = {"created_at": c.isoformat(timespec="seconds")}
        if run_after_s is not None:
            fields["started_at"] = (c + timedelta(seconds=run_after_s)).isoformat(timespec="seconds")
        if done_after_s is not None:
            fields["finished_at"] = (c + timedelta(seconds=done_after_s)).isoformat(timespec="seconds")
        with store.tx() as cx:
            cx.execute("UPDATE tasks SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), task_id))
            cx.execute("UPDATE route_log SET created_at=? WHERE task_id=?", (fields["created_at"], task_id))
            t = store.require_task(task_id)
            if "started_at" in fields and t.to_addr:      # herdr 로 받는 창에 바로 넣은 것처럼 (예시)
                cx.execute("INSERT INTO push_log(session, at) VALUES (?,?)", (t.to_addr, fields["started_at"]))
            if "finished_at" in fields and t.kind.value == "task":   # 회신을 보낸 창에 바로 넣은 것처럼
                cx.execute("INSERT INTO push_log(session, at) VALUES (?,?)", (t.from_addr, fields["finished_at"]))

    # 1) 완료된 위임: Claude → Codex 구현 → 회신
    r = await send("장바구니 쿠폰 적용 API 구현 — 할인 상한·중복 쿠폰 규칙 포함", "lead-claude", "api-codex",
                   title="쿠폰 적용 API 구현", acceptance=["단위 테스트 통과", "OpenAPI 문서 갱신"])
    store.inbox("api-codex", mark=True)
    b.reply(r["task_id"], "구현 완료: POST /cart/coupons, 테스트 24개 통과, openapi.yaml 갱신", sender="api-codex")
    at(r["task_id"], 52, 4, 1260)
    # 2) 작업 중(전달됨): Claude → Codex
    r = await send("상품 목록 무한 스크롤 구현, 스켈레톤 로딩 포함", "lead-claude", "web-codex", title="상품 목록 무한 스크롤")
    store.inbox("web-codex", mark=True)
    at(r["task_id"], 18, 3)
    # 3) 검토 요청: Codex → Claude reviewer (읽기 전용)
    r = await send("쿠폰 API 변경분 리뷰 부탁 — 동시성(재고 차감) 위주", "api-codex", "reviewer", title="쿠폰 API 리뷰",
                   sandbox="read-only")
    store.inbox("review-claude", mark=True)
    b.reply(r["task_id"], "중복 적용 경합 1건 지적: SELECT … FOR UPDATE 필요. 그 외 문제 없음", sender="review-claude")
    at(r["task_id"], 31, 6, 540)
    # 4) 승인 대기: 장비 제어 코드 변경 (하드웨어 대상 + 제어 동작 → 승인 전까지 읽기 전용)
    r = await send("출고 라인 PLC 재연결 로직 수정 후 장비에서 시험", "lead-claude", "api-codex", title="PLC 재연결 로직 수정")
    at(r["task_id"], 6)
    # 5) 대상 미정: 역할을 못 고름
    r = await send("결제 실패 알림 메일 문구 다듬기", "api-codex", None, title="메일 문구")
    at(r["task_id"], 3)
    # 6) 알림: 받는 창이 닫혀 있어 소멸
    r = await send("빌드 캐시를 비웠습니다 (참고만)", "web-codex", "docs-claude", kind="message", title="빌드 캐시 비움")
    store.transition(r["task_id"], (TaskStatus.QUEUED,), TaskStatus.CANCELLED, error="받는 창이 닫혀 있어 알림 소멸")
    store.log_route(r["task_id"], "drop", "docs-claude", None, True, {"reason": "no-pane"})
    at(r["task_id"], 11, None, 1)
    # 7) Codex ↔ Codex 직접 전달
    did = "d_" + now.strftime("%Y%m%d_%H%M%S") + "_demo01"
    store.enqueue_direct(did, "codex", "web-codex", "api-codex",
                         {"body": "상품 목록 API 에 cursor 페이지네이션 추가 가능할까요?", "title": "cursor 페이지네이션", "kind": "task"})
    store.claim_direct("api-codex")
    # 8) Claude ↔ Claude 기본 도구 메시지 (관찰 기록)
    store.log_flow(transport="claude-native", provider="claude", from_name="lead-claude", from_native="demo-lead-claude",
                   to_name="review-claude", to_raw="review-claude", status="sent",
                   summary="쿠폰 API 리뷰가 끝나면 결과를 api-codex 에도 공유해 주세요")
    store.log_flow(transport="claude-native", provider="claude", from_name="review-claude",
                   from_native="demo-review-claude", to_name="docs-claude", to_raw="docs-claude", status="sent",
                   summary="리뷰 체크리스트 문서에 동시성 항목 추가 부탁")
    return {"sessions": len(DEMO_SESSIONS), "tasks": len(store.list_tasks()), "direct": 1, "observed": 2}
