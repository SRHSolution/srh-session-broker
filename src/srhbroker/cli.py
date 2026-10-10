"""srhbroker CLI.

  srhbroker init                      설정 디렉터리·예시 config 생성
  srhbroker mcp                       MCP 서버(stdio) — Claude/Codex 설정에 등록
  srhbroker daemon                    디스패처 데몬 (worker 세션 실행)
  srhbroker register <이름> <provider> [--mode worker|interactive] [--role r]... [--alias a]...
  srhbroker register --session <세션ID> [--role r]...   기존 Claude/Codex 세션 등록 (이름 = rename 이름)
  srhbroker discover [--days 7] [--all]                 등록할 수 있는 기존 세션 목록
  srhbroker rename <지금 이름> <새 이름>               등록 이름 바꾸기 (세션 ID·역할·별칭 유지, 옛 이름은 별칭)
  srhbroker reconnect <이름> [--session ID]           그 이름을 rename 이름이 같은 다른 세션에 다시 연결
  srhbroker deliver <이름> [--wait]                     받은 메시지를 herdr 창에 바로 넣기 (바쁘면 쉴 때까지 대기)
  srhbroker status                                      데몬·herdr·세션별 창 연결·대기 작업 점검 (읽기 전용)
  srhbroker watch [--interval 3]                        터미널 대시보드 (herdr 창에 띄워 두기)
  srhbroker dashboard [--port 8765] [--open]            로컬 웹 대시보드 (127.0.0.1, 토큰 필요)
  srhbroker sessions | tasks [--from 이름] [--active] | show <id> | routes
  srhbroker send "<본문>" [--to 이름|역할|provider] [--title ..] [--wait]
  srhbroker route <id> <대상> | approve <id> | cancel <id>
  srhbroker hook claude-stop|codex-stop [--as 이름]
  srhbroker hook claude-observe              Claude SendMessage 관찰 기록 (PostToolUse)
  srhbroker setup claude|codex [--apply]  MCP·hook 설정 예시 출력 (--apply: 백업 후 사용자 설정에 적용)
  srhbroker doctor                    설치·연결 점검 (읽기 전용, 고칠 명령 안내)
  srhbroker demo [--home DIR]         예시(팬텀) 데이터로 watch·대시보드 체험 (실제 데이터와 분리)
  srhbroker update [--check] [--to vX.Y.Z] [--stop-all]   git 배포(저장소 태그)로 업데이트
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

from .config import EXAMPLE_CONFIG, broker_home, load_config, load_dotenv
from .models import USER_ADDR, BrokerError


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


_OPENED: list[Any] = []


def _broker():
    from .service import Broker
    b = Broker(load_config())
    _OPENED.append(b)
    return b


def _table(rows: list[dict[str, Any]], cols: list[str]) -> None:
    from .watch import fit, width
    if not rows:
        print("(없음)")
        return
    w = {c: max(width(c), *(width(" ".join(str(r.get(c) or "").split())) for r in rows)) for c in cols}
    print("  ".join(fit(c, w[c]) for c in cols).rstrip())
    for r in rows:
        print("  ".join(fit(r.get(c) or "", w[c]) for c in cols).rstrip())


SETUP_CLAUDE = """\
# 통신 정책: Claude ↔ Claude는 ListAgents → SendMessage로 직접 전달.
# srhbroker는 Claude ↔ Codex만 중계하며 native_required는 아직 미전달을 뜻한다.
# 1) MCP 서버 등록 (사용자 범위 — 모든 프로젝트에서 사용)
claude mcp add --scope user srhbroker -- srhbroker mcp

# 2) Stop hook — 대화형 세션이 응답을 마칠 때 받은 메시지를 자동 주입
#    %USERPROFILE%\\.claude\\settings.json (또는 프로젝트 .claude/settings.json) 에 추가:
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "srhbroker hook claude-stop"}]}]
  }
}

# 2-1) (선택) Claude ↔ Claude SendMessage 를 대시보드에서 보려면 관찰 hook 추가 (전달에는 관여하지 않음)
#   "PostToolUse": [{"matcher": "SendMessage", "hooks": [{"type": "command", "command": "srhbroker hook claude-observe"}]}]

# 3) 세션 등록 — 기존 세션은 rename 이름 그대로 (환경 변수 없이 평소처럼 열면 된다)
srhbroker discover
srhbroker register --session <세션ID> --role planner
#    새 이름으로 쓰려면:  $env:SRHBROKER_SELF = "planner"; claude
#                         srhbroker register planner claude --mode interactive --role planner
"""

SETUP_CODEX = """\
# 통신 정책: Codex ↔ Codex는 현재 세션의 기본 에이전트 통신 도구로 직접 전달.
# srhbroker는 Codex ↔ Claude만 중계하며 native_required는 아직 미전달을 뜻한다.
# 1) %USERPROFILE%\\.codex\\config.toml 에 추가
#    주의: Codex 는 MCP 서버에 환경 변수를 기본으로 넘기지 않는다 → env_vars 필수 (codex-cli 0.160 에서 확인)
[mcp_servers.srhbroker]
command = "srhbroker"
args = ["mcp"]
env_vars = ["SRHBROKER_SELF", "SRHBROKER_TASK", "SRHBROKER_HOME", "TYPESAFE_API_KEY",
            # herdr 창 안에서 보낼 때 받는 창에 바로 넣으려면 herdr 변수도 넘겨야 한다
            "HERDR_ENV", "HERDR_SOCKET_PATH", "HERDR_BIN_PATH", "HERDR_HOME", "HERDR_PANE_ID", "HERDR_TAB_ID",
            "HERDR_WORKSPACE_ID"]
startup_timeout_sec = 20
tool_timeout_sec = 3600   # wait 도구가 오래 기다릴 수 있도록

# 2) 수신: herdr 창 안에서 쓰면 받는 창이 쉴 때 브로커가 프롬프트로 바로 넣는다 (Codex Stop hook 불필요).
#    Codex Stop hook 은 세션 정보를 넘기지 않아 쓰지 않는다. Windows 에서는 hook 명령이 PowerShell 로 실행되므로
#    '"경로" hook ...' 처럼 따옴표로 시작하면 ParserError(hook exited with code 1) 가 난다.

# 3) 세션 등록 — 기존 세션은 rename 이름 그대로 (Codex 는 도구 호출 _meta.threadId 로 세션을 알린다)
srhbroker discover --provider codex
srhbroker register --session <thread_id> --role builder --mode worker
#    새 이름으로 쓰려면:  srhbroker register builder codex --mode worker --role builder --alias b
"""


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        for s in (sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            except Exception:
                pass
    load_dotenv()  # mcp·hook·daemon 모두 이 진입점을 거친다
    p = argparse.ArgumentParser(prog="srhbroker", description="Claude ↔ Codex 세션 브로커")
    p.add_argument("-v", "--verbose", action="store_true")
    from . import __version__
    p.add_argument("--version", action="version", version=f"srhbroker {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("mcp")
    sub.add_parser("daemon")
    sub.add_parser("status")
    wt = sub.add_parser("watch")
    wt.add_argument("--interval", type=float, default=3.0)
    wt.add_argument("--once", action="store_true", help="한 번만 그리고 끝낸다")
    db = sub.add_parser("dashboard")
    db.add_argument("--port", type=int, default=None)
    db.add_argument("--open", action="store_true", help="브라우저로 연다")
    db.add_argument("--new-token", action="store_true", help="토큰을 새로 만든다 (기존 주소는 더 이상 열리지 않음)")
    dl = sub.add_parser("deliver")
    dl.add_argument("name")
    dl.add_argument("--wait", action="store_true", help="창이 바쁘면 쉴 때까지 기다렸다가 넣는다")

    r = sub.add_parser("register")
    r.add_argument("name", nargs="?", help="--session 이면 생략 → rename 이름 사용")
    r.add_argument("provider", nargs="?", choices=["claude", "codex"])
    r.add_argument("--session", default=None, help="기존 Claude session_id / Codex thread_id (이름·provider·cwd 자동)")
    r.add_argument("--name", dest="name_opt", default=None, help="브로커 이름 직접 지정 (--session 과 함께: rename 이름 대신)")
    r.add_argument("--mode", default=None, choices=["worker", "interactive"],
                   help="기본값: --session 이면 interactive, 아니면 worker")
    r.add_argument("--role", action="append", default=[])
    r.add_argument("--alias", action="append", default=[])
    r.add_argument("--desc", default="")
    r.add_argument("--cwd", default=None)
    r.add_argument("--sandbox", default="workspace-write", choices=["read-only", "workspace-write"])
    r.add_argument("--model", default=None)
    r.add_argument("--native-id", default=None, help="기존 Claude session_id / Codex thread_id 에 연결")

    dc = sub.add_parser("discover")
    dc.add_argument("--days", type=float, default=7)
    dc.add_argument("--provider", default=None, choices=["claude", "codex"])
    dc.add_argument("--all", action="store_true", help="rename 하지 않은 세션도 표시")
    dc.add_argument("-n", type=int, default=30)

    u = sub.add_parser("unregister")
    u.add_argument("name")
    rc = sub.add_parser("reconnect", help="등록 이름을 rename 이름이 같은 다른 세션(다시 연·계정을 바꿔 연 세션)에 다시 연결")
    rc.add_argument("name")
    rc.add_argument("--session", default=None, help="연결할 세션 ID (생략: rename 이름이 같은 가장 최근 세션)")
    rn = sub.add_parser("rename", help="등록 이름 바꾸기 (세션 ID·역할·별칭 유지, 옛 이름은 별칭, 기록 이관)")
    rn.add_argument("old", help="지금 이름 (등록에서 지워진 옛 이름이면 그 기록을 new 세션이 이어받음)")
    rn.add_argument("new")

    sub.add_parser("sessions")
    t = sub.add_parser("tasks")
    t.add_argument("--status", default=None)
    t.add_argument("--from", dest="sender", default=None, help="이 세션이 보낸 작업만")
    t.add_argument("--active", action="store_true", help="아직 끝나지 않은 작업만 (queued·held·needs_routing·delivered·running)")
    t.add_argument("-n", type=int, default=30)
    sh = sub.add_parser("show")
    sh.add_argument("task_id")
    rl = sub.add_parser("routes")
    rl.add_argument("-n", type=int, default=30)

    s = sub.add_parser("send")
    s.add_argument("body")
    s.add_argument("--to", default=None)
    s.add_argument("--title", default="")
    s.add_argument("--kind", default="task", choices=["task", "message"])
    s.add_argument("--accept", action="append", default=[], help="완료 조건 (여러 번)")
    s.add_argument("--sandbox", default=None, choices=["read-only", "workspace-write"])
    s.add_argument("--deadline", type=int, default=None)
    s.add_argument("--from", dest="sender", default=USER_ADDR)
    s.add_argument("--wait", action="store_true")

    ro = sub.add_parser("route")
    ro.add_argument("task_id")
    ro.add_argument("to")
    ap = sub.add_parser("approve")
    ap.add_argument("task_id")
    ap.add_argument("--sandbox", default=None, choices=["read-only", "workspace-write"])
    ca = sub.add_parser("cancel")
    ca.add_argument("task_id")

    h = sub.add_parser("hook")
    h.add_argument("kind", choices=["claude-stop", "codex-stop", "claude-observe"])
    h.add_argument("--as", dest="name", default=None)

    su = sub.add_parser("setup")
    su.add_argument("provider", choices=["claude", "codex"])
    su.add_argument("--apply", action="store_true", help="사용자 설정 파일에 적용 (백업을 남기고, 이미 있는 항목은 두고 없는 것만 추가)")
    sub.add_parser("doctor")
    up = sub.add_parser("update", help="git 배포(저장소의 vX.Y.Z 태그)로 업데이트")
    up.add_argument("--check", action="store_true", help="새 버전이 있는지만 본다")
    up.add_argument("--to", default=None, help="이 태그로 (예: v0.3.0) — 기본: 최신 태그")
    up.add_argument("--stop-all", action="store_true",
                    help="Windows: 설치를 잠그는 srhbroker 프로세스(데몬·대시보드·세션 MCP 서버)를 끝내고 진행")
    up.add_argument("--no-restart", action="store_true", help="업데이트 뒤 데몬을 다시 띄우지 않는다")
    dm = sub.add_parser("demo")
    dm.add_argument("--home", default=None, help="예시 데이터를 둘 폴더 (기본: 임시 폴더의 srhbroker-demo)")

    a = p.parse_args(argv)
    quiet = a.cmd in ("init", "setup", "doctor", "demo", "update")   # 사람에게 보여 주는 점검·설정 명령은 진행 로그를 숨긴다
    logging.basicConfig(level=logging.DEBUG if a.verbose else (logging.WARNING if quiet else logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        return _dispatch(a)
    except BrokerError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
    finally:
        while _OPENED:  # 이 명령에서 연 DB 연결을 닫는다
            _OPENED.pop().store.close()


def _dispatch(a: argparse.Namespace) -> int:
    if a.cmd == "init":
        home = broker_home()
        home.mkdir(parents=True, exist_ok=True)
        cfg = home / "config.toml"
        if cfg.exists():
            print(f"이미 있음: {cfg}")
        else:
            cfg.write_text(EXAMPLE_CONFIG, encoding="utf-8")
            print(f"생성: {cfg}")
        if not os.environ.get("TYPESAFE_API_KEY"):
            print("참고: TYPESAFE_API_KEY 가 설정되지 않았습니다 — Jev 판단은 건너뛰고 규칙/사용자 지정으로 동작합니다.")
        return 0
    if a.cmd == "mcp":
        from .mcp_server import run
        run()
        return 0
    if a.cmd == "daemon":
        from .dispatcher import Dispatcher, SingleInstance
        b = _broker()
        lock = SingleInstance(b.cfg.home / "daemon.lock")
        if not lock.acquire():
            print("오류: 디스패처가 이미 실행 중입니다", file=sys.stderr)
            return 1
        try:
            asyncio.run(Dispatcher(b).run_forever())
        except KeyboardInterrupt:
            pass
        finally:
            lock.release()
        return 0
    if a.cmd == "deliver":
        return _deliver(_broker(), a.name, a.wait)
    if a.cmd == "status":
        return _status(_broker())
    if a.cmd == "watch":
        from .watch import run
        try:
            return run(_broker(), interval=a.interval, once=a.once)
        except KeyboardInterrupt:
            return 0
    if a.cmd == "dashboard":
        from .dashboard import serve
        b = _broker()
        return serve(b, port=a.port or int(b.cfg.data.get("monitor", {}).get("dashboard_port", 8765)),
                     open_browser=a.open, new_token=a.new_token)
    if a.cmd == "hook":
        # Claude Code 와 Codex(0.160+) 의 Stop hook 입출력 형식이 같아 같은 구현을 쓴다
        # hook 이 실패하면 Claude/Codex 창에 'hook failed' 가 뜬다. 어떤 오류도 세션을 막지 않게 하고 기록만 남긴다
        try:
            from .hooks import claude_observe, claude_stop
            if a.kind == "claude-observe":
                return claude_observe(_broker())
            return claude_stop(_broker(), a.name, provider=a.kind.split("-")[0])
        except Exception:
            import traceback
            try:
                home = broker_home()
                home.mkdir(parents=True, exist_ok=True)
                with (home / "hook-error.log").open("a", encoding="utf-8") as f:
                    f.write(f"--- {datetime.now().isoformat(timespec='seconds')} {a.kind} cwd={os.getcwd()}\n")
                    f.write(traceback.format_exc())
            except Exception:
                pass
            return 0
    if a.cmd == "setup":
        if not a.apply:
            print(SETUP_CLAUDE if a.provider == "claude" else SETUP_CODEX)
            print("# 위 내용을 자동으로 적용하려면: srhbroker setup " + a.provider + " --apply")
            return 0
        from .onboard import apply_claude, apply_codex
        for line in (apply_claude() if a.provider == "claude" else apply_codex()):
            print(f"- {line}")
        print("\n점검: srhbroker doctor")
        return 0
    if a.cmd == "doctor":
        from .onboard import doctor, print_doctor
        return print_doctor(doctor(_broker()))
    if a.cmd == "demo":
        return _demo(a.home)
    if a.cmd == "update":
        from .updater import run as run_update
        return run_update(_broker(), check_only=a.check, to=a.to, stop_all=a.stop_all, restart=not a.no_restart)

    b = _broker()
    if a.cmd == "register":
        a.name = a.name_opt or a.name
        notes: list[str] = []
        if a.session:
            s = b.register_native(a.session, name=a.name, provider=a.provider, mode=a.mode or "interactive",
                                  roles=a.role or None, aliases=a.alias or None, description=a.desc, sandbox=a.sandbox,
                                  model=a.model, notes=notes)
        elif a.name and a.provider:
            s = b.register(a.name, a.provider, mode=a.mode or "worker", roles=a.role, aliases=a.alias,
                           description=a.desc, cwd=a.cwd or os.getcwd(), native_id=a.native_id, sandbox=a.sandbox,
                           model=a.model)
        else:
            print("오류: <이름> <provider> 또는 --session <세션ID> 를 지정하세요", file=sys.stderr)
            return 2
        _print({**s.public(), **({"notes": notes} if notes else {})})
    elif a.cmd == "discover":
        from . import native
        from .models import normalize_name
        rows = []
        for ns in native.recent(a.days, provider=a.provider, titled_only=not a.all)[:a.n]:
            reg = b.identify(ns.id)
            rows.append({"updated": f"{datetime.fromtimestamp(ns.updated):%m-%d %H:%M}", "provider": ns.provider,
                         "session_id": ns.id, "title": (ns.title or "")[:30],
                         "name": reg.name if reg else (normalize_name(ns.title) or "(직접 지정)"),
                         "registered": "✔" if reg else "", "cwd": ns.cwd or ""})
        _table(rows, ["updated", "provider", "session_id", "title", "name", "registered", "cwd"])
    elif a.cmd == "reconnect":
        notes = []
        _print({**b.reconnect(a.name, session_id=a.session, notes=notes).public(), "notes": notes})
    elif a.cmd == "rename":
        notes = []
        _print({**b.rename(a.new, old=a.old, notes=notes).public(), "notes": notes})
    elif a.cmd == "unregister":
        print("삭제됨" if b.store.remove_session(a.name) else "없음")
    elif a.cmd == "sessions":
        rows = [{**x.public(), "roles": ",".join(x.roles), "aliases": ",".join(x.aliases)} for x in b.store.list_sessions()]
        _table(rows, ["name", "provider", "mode", "roles", "aliases", "sandbox", "busy_task_id", "has_native_id"])
    elif a.cmd == "tasks":
        _table(b.recent(limit=a.n, status=a.status, mine=a.sender, active=a.active),
               ["task_id", "status", "from", "to", "routed_by", "created_at", "title"])
    elif a.cmd == "show":
        _print(b.status(a.task_id))
    elif a.cmd == "routes":
        _table(b.store.route_log(a.n), ["task_id", "router", "decision", "confidence", "applied", "created_at"])
    elif a.cmd == "send":
        res = asyncio.run(b.send(a.body, sender=a.sender, to=a.to, title=a.title, kind=a.kind, acceptance=a.accept,
                                 sandbox=a.sandbox, deadline_s=a.deadline))
        _print(res)
        if a.wait and res["status"] in ("queued", "running"):
            _print(asyncio.run(b.wait(res["task_id"], timeout_s=float(a.deadline or 3600), caller=a.sender)))
    elif a.cmd == "route":
        _print(b.assign(a.task_id, a.to))
    elif a.cmd == "approve":
        _print(b.approve(a.task_id, a.sandbox))
    elif a.cmd == "cancel":
        _print(b.cancel(a.task_id, by=USER_ADDR))  # 터미널의 사람은 어떤 작업이든 취소할 수 있다
    return 0


def _ago(ts: float | None) -> str:
    if not ts:
        return "?"
    s = max(0, int(datetime.now().timestamp() - ts))
    return f"{s}초 전" if s < 120 else f"{s // 60}분 전" if s < 7200 else f"{s // 3600}시간 전"


def _status(b: Any) -> int:
    """데몬·herdr·세션별 창 연결·대기 작업을 한눈에 본다. 읽기 전용 — 아무것도 넣거나 바꾸지 않는다."""
    from . import herdr as herdr_mod
    from .dispatcher import SingleInstance, code_mtime
    home = b.cfg.home
    lock = SingleInstance(home / "daemon.lock")
    running = not lock.acquire()
    if not running:
        lock.release()
    try:
        st = json.loads((home / "daemon.json").read_text(encoding="utf-8")) if running else {}
    except (OSError, ValueError):
        st = {}
    print(f"브로커 홈: {home}")
    if not running:
        print("데몬: 실행 중 아님 — herdr 창 하나에서 `srhbroker daemon` 을 띄우세요 "
              "(worker 실행, 밀린 메시지 전달, 오래된 대기 작업 만료)")
    elif not st:
        print("데몬: 실행 중 (이전 버전 — 상태 정보 없음). 재시작하면 herdr 여부·코드 변경 여부가 표시됩니다")
    else:
        print(f"데몬: 실행 중 (pid {st.get('pid')}, 시작 {_ago(st.get('started_at'))}, 마지막 신호 {_ago(st.get('heartbeat'))})")
        print("  herdr 전달: " + (f"가능 (창 {st.get('herdr_pane')})" if st.get("herdr")
                                  else "불가 — herdr 창 밖에서 실행됨. herdr 창에서 다시 띄우세요"))
        if code_mtime() > float(st.get("code_mtime", 0)) + 1:
            print("  ⚠ 데몬 시작 뒤 코드가 바뀌었습니다 — 재시작하세요 (Ctrl+C 후 `srhbroker daemon`)")
    avail = b.herdr.available()
    print(f"이 셸의 herdr: {'사용 가능' if avail else '사용 불가 (herdr 창 밖)'}")

    agents = b.herdr.agents() if avail else []
    rows = []
    for s in b.store.list_sessions():
        a, how = b.locate_pane(s, agents) if avail else (None, "none")
        if s.mode.value != "interactive":
            pane = "(worker)"
        elif not avail:
            pane = "?"
        elif how == "none":
            pane = "닫힘"
        elif how == "mismatch":
            other = b.title_owner(t) if (t := herdr_mod.title_name(a)) else None
            pane = f"{a['pane_id']} 짝 불일치" + (f"({other} 창)" if other else "")
        else:
            pane = f"{a['pane_id']} {a.get('agent_status')}" + (" (창 제목)" if how == "title" else "")
        c = b.store.status_counts(s.name)
        rows.append({"name": s.name, "provider": s.provider, "mode": s.mode.value, "창": pane,
                     "받을것": c.get("queued", 0) or "", "처리중": (c.get("delivered", 0) + c.get("running", 0)) or "",
                     "승인대기": c.get("held", 0) or ""})
    print()
    _table(rows, ["name", "provider", "mode", "창", "받을것", "처리중", "승인대기"])

    waiting = [t for st_ in ("held", "needs_routing") for t in b.store.list_tasks(status=st_, limit=50)]
    if waiting:
        age = int(b.cfg.broker.get("stale_after_s", 86400))
        print(f"\n사람의 결정이 필요한 작업 ({age // 3600}시간 지나면 데몬이 timeout 처리):")
        _table([{"task_id": t.id, "status": t.status.value, "from": t.from_addr, "to": t.to_addr or "?",
                 "경과": _ago(datetime.fromisoformat(t.created_at).timestamp()), "title": t.title[:40]}
                for t in waiting], ["task_id", "status", "from", "to", "경과", "title"])
    return 0


def _demo(home_arg: str | None) -> int:
    import tempfile
    from pathlib import Path
    from .onboard import DEMO_CONFIG, seed_demo
    from .service import Broker
    home = Path(home_arg).expanduser() if home_arg else Path(tempfile.gettempdir()) / "srhbroker-demo"
    if home.resolve() == broker_home().resolve():
        print("오류: 실제 브로커 홈에는 예시 데이터를 만들지 않습니다 — 다른 --home 을 지정하세요", file=sys.stderr)
        return 2
    if (home / "broker.db").exists():
        print(f"이미 예시 데이터가 있습니다: {home} (새로 만들려면 폴더를 지우세요)")
    else:
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(DEMO_CONFIG, encoding="utf-8")
        b = Broker(load_config(home=home))
        _OPENED.append(b)
        n = asyncio.run(seed_demo(b))
        print(f"예시 데이터 생성: {home} — 세션 {n['sessions']} · 작업 {n['tasks']} · 직접 전달 {n['direct']} · 관찰 {n['observed']}")
    env = f'$env:SRHBROKER_HOME = "{home}"' if sys.platform == "win32" else f'export SRHBROKER_HOME="{home}"'
    print(f"\n보려면 (이 셸에서만 예시 홈 사용):\n  {env}\n  srhbroker watch          # 터미널 대시보드 (q 로 종료)\n"
          "  srhbroker dashboard --open   # 웹 대시보드\n  srhbroker tasks\n끝나면 SRHBROKER_HOME 을 지우거나 새 터미널을 여세요.")
    return 0


def _deliver(b: Any, name: str, wait: bool) -> int:
    """받은 메시지를 herdr 창에 넣는다. --wait: 창이 바쁘면 쉴 때까지 기다리며, 남은 메시지가 없을 때까지 반복."""
    import time
    from .dispatcher import SingleInstance
    lock = SingleInstance(b.cfg.home / f"deliver-{name}.lock")
    if wait and not lock.acquire():
        print("already-waiting")  # 같은 세션의 대기 프로세스가 이미 있다
        return 0
    try:
        end = time.monotonic() + float(b.cfg.data.get("herdr", {}).get("wait_timeout_s", 3600))
        while True:
            r = b.deliver_now(name)
            if not wait or r not in ("busy", "delivered"):
                print(r)
                return 0
            s = b.store.get_session(name)
            agent, how = b.locate_pane(s) if s else (None, "none")
            agent = agent if how in ("id", "title") else None
            remaining = end - time.monotonic()
            if not agent or remaining <= 0:
                print("no-pane" if not agent else "timeout")
                return 0
            if r == "busy":
                b.herdr.wait_ready(agent["pane_id"], min(remaining, 300))
            time.sleep(1)  # 넣은 직후에는 창이 working 으로 바뀔 때까지 잠깐 둔다
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
