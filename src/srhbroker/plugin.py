"""herdr 플러그인 연결부 — 저장소 루트의 herdr-plugin.toml 이 부르는 `srhbroker plugin <명령>`.

herdr 는 플러그인 명령에 HERDR_BIN_PATH·HERDR_SOCKET_PATH·HERDR_PLUGIN_ID 등을 넘긴다. 액션·startup 의 출력은 사람에게
보이지 않고 플러그인 기록(`herdr plugin log list`)에만 남으므로, 사람이 볼 결과는 팝업 창(output 진입점)에 띄운다.
액션은 실행 시점에 포커스된 창의 HERDR_PANE_ID 를 받으므로 데몬을 액션에서 직접 띄우지 않고 엔진 창(engine 진입점)을 연다.

  startup       herdr 가 켜질 때 한 번 — 데몬이 없으면 엔진 탭을 띄운다 ([plugin] autostart_engine, 기본 켬)
  engine        엔진 탭 안에서 데몬 실행. 이미 실행 중이면 조용히 닫히고, 시작에 실패하면 Enter 를 기다린다
  start-engine  액션 — 데몬이 없으면 엔진 탭, 있으면 알림만
  dashboard     액션 — 웹 대시보드가 떠 있으면 브라우저로 열고, 없으면 대시보드 탭을 띄운다
  watch         액션 — 터미널 대시보드를 옆 창(split)으로
  show <명령>   액션 — 팝업 창에서 srhbroker <명령> 을 실행해 결과를 보여 준다 (status·doctor·setup)
  output        팝업 창 안: SRHBROKER_PLUGIN_ARGS 의 명령을 실행하고 Enter 를 기다린다
  run <명령>    탭·옆 창 안: srhbroker <명령> 을 실행하고, 실패하면 창이 바로 닫히지 않게 Enter 를 기다린다
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Any, Callable

log = logging.getLogger("srhbroker.plugin")

PLUGIN_ID = "srhbroker"          # herdr-plugin.toml 의 id
ENGINE, DASHBOARD, WATCH, OUTPUT = "engine", "dashboard", "watch", "output"   # 창 진입점 id
ARGS_ENV = "SRHBROKER_PLUGIN_ARGS"
ENGINE_TAB, DASHBOARD_TAB = "srhbroker", "broker web"
COMMANDS = ("startup", "engine", "start-engine", "dashboard", "watch", "show", "output", "run")

_run: Callable[..., subprocess.CompletedProcess] = subprocess.run   # 테스트에서 바꿔 끼운다


def herdr_bin() -> str | None:
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")


def plugin_id() -> str:
    return os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID


def pane_open_cmd(herdr: str, entry: str, placement: str, *, plugin: str | None = None, workspace: str | None = None,
                  env: dict[str, str] | None = None, focus: bool = False) -> list[str]:
    """플러그인 창을 여는 herdr 명령 (updater 가 데몬을 엔진 탭으로 다시 띄울 때도 쓴다)."""
    cmd = [herdr, "plugin", "pane", "open", "--plugin", plugin or plugin_id(), "--entrypoint", entry,
           "--placement", placement, "--focus" if focus else "--no-focus"]
    if workspace:
        cmd += ["--workspace", workspace]
    for k, v in (env or {}).items():
        cmd += ["--env", f"{k}={v}"]
    return cmd


def open_pane(entry: str, placement: str, *, tab_label: str | None = None, **kw: Any) -> dict[str, Any] | None:
    """플러그인 창을 열고 herdr 가 돌려준 창 정보를 돌려준다 (팝업은 타일 창이 아니어서 정보가 없다 → {}).
    실패하면 None (기록만)."""
    herdr = herdr_bin()
    if not herdr:
        log.warning("herdr 를 찾지 못해 %s 창을 열지 못했습니다", entry)
        return None
    r = _run(pane_open_cmd(herdr, entry, placement, **kw), capture_output=True, text=True, timeout=20)
    try:
        res = json.loads(r.stdout)["result"]
    except (ValueError, KeyError, TypeError):
        res = None
    if r.returncode != 0 or not isinstance(res, dict):
        log.warning("%s 창 열기 실패: %s", entry, (r.stderr or r.stdout or "").strip()[:300])
        return None
    pane = (res.get("plugin_pane") or {}).get("pane") or {}
    if tab_label and pane.get("tab_id"):
        _run([herdr, "tab", "rename", pane["tab_id"], tab_label], capture_output=True, text=True, timeout=10)
    return pane


def notify(title: str, body: str = "") -> None:
    herdr = herdr_bin()
    if herdr:
        _run([herdr, "notification", "show", title, *(["--body", body] if body else [])],
             capture_output=True, text=True, timeout=10)


def _engine_running(b: Any) -> dict[str, Any]:
    from .monitor import daemon_state
    return daemon_state(b.cfg.home)


def open_engine() -> dict[str, Any] | None:
    return open_pane(ENGINE, "tab")


def startup(b: Any, *, delay_s: float = 2.0) -> int:
    """herdr 시작 hook. 어떤 경우에도 herdr 를 막지 않도록 항상 0 을 돌려준다."""
    if not b.cfg.data.get("plugin", {}).get("autostart_engine", True):
        print("autostart_engine = false — engine not started")
        return 0
    if os.environ.get("HERDR_ENV") != "1":
        print("not inside herdr — engine not started")
        return 0
    time.sleep(delay_s)   # 세션 복원으로 다시 뜨는 엔진 창이 잠금을 잡을 시간을 준다
    d = _engine_running(b)
    if d.get("running"):
        print(f"engine already running (pid {d.get('pid')}, pane {d.get('herdr_pane')})")
        return 0
    pane = open_engine()
    print("could not open the engine pane" if pane is None else f"engine started in pane {pane.get('pane_id')}")
    return 0


def start_engine(b: Any) -> int:
    d = _engine_running(b)
    if d.get("running"):
        where = f"pane {d['herdr_pane']}" if d.get("herdr_pane") else "outside herdr"
        notify("SRH Broker engine is already running", f"pid {d.get('pid')} · {where}")
        return 0
    if open_engine() is None:
        notify("Could not start the SRH Broker engine", "See: herdr plugin log list --plugin " + plugin_id())
        return 1
    return 0


def engine(daemon: Callable[[], int]) -> int:
    """엔진 탭: 데몬을 이 창에서 실행한다. 창이 닫히면 데몬도 멈춘다."""
    from .config import broker_home
    from .dispatcher import SingleInstance
    lock = SingleInstance(broker_home() / "daemon.lock")
    if not lock.acquire():   # 이미 다른 창·터미널에서 실행 중 — 중복 창은 조용히 닫는다
        print("SRH Broker engine is already running elsewhere — closing this pane.")
        time.sleep(3)
        return 0
    lock.release()
    herdr, tab = herdr_bin(), os.environ.get("HERDR_TAB_ID")
    if herdr and tab:   # 어떤 경로로 열렸든(startup·액션·업데이트 뒤 다시 띄우기) 엔진 탭 이름을 붙인다
        _run([herdr, "tab", "rename", tab, ENGINE_TAB], capture_output=True, text=True, timeout=10)
    rc = daemon()
    if rc:
        _hold(f"\nThe engine stopped (exit {rc}). Press Enter to close.")
    return rc


def dashboard_running(port: int, timeout: float = 1.0) -> bool:
    """그 포트에 srhbroker 웹 대시보드가 떠 있는지 (토큰 없이 열면 403 이어도 Server 헤더로 안다)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
            server = r.headers.get("Server", "")
    except urllib.error.HTTPError as e:
        server = e.headers.get("Server", "") if e.headers else ""
    except (OSError, ValueError):
        return False
    return server.startswith("srhbroker-dashboard")


def dashboard(b: Any, *, opener: Callable[[str], Any] = webbrowser.open) -> int:
    from .dashboard import _token
    port = int(b.cfg.data.get("monitor", {}).get("dashboard_port", 8765))
    if dashboard_running(port):
        opener(f"http://127.0.0.1:{port}/?t={_token(b.cfg.home, False)}")
        return 0
    if open_pane(DASHBOARD, "tab", tab_label=DASHBOARD_TAB) is None:
        notify("Could not start the SRH Broker web dashboard", "See: herdr plugin log list --plugin " + plugin_id())
        return 1
    return 0


def watch() -> int:
    return 0 if open_pane(WATCH, "split") is not None else 1


def show(args: list[str]) -> int:
    """팝업 창에서 srhbroker <args> 를 실행한다."""
    if not args:
        print("usage: srhbroker plugin show <command> [args...]")
        return 2
    return 0 if open_pane(OUTPUT, "popup", focus=True, env={ARGS_ENV: json.dumps(args)}) is not None else 1


def _hold(msg: str) -> None:
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass


def output(main: Callable[[list[str]], int]) -> int:
    """팝업 창 안: 환경 변수로 받은 명령을 실행하고, 결과를 읽을 수 있게 Enter 를 기다린다."""
    try:
        args = json.loads(os.environ.get(ARGS_ENV) or "[]")
    except ValueError:
        args = []
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        _hold(f"{ARGS_ENV} is missing. Press Enter to close.")
        return 2
    print("$ srhbroker " + " ".join(args) + "\n", flush=True)
    try:
        rc = main(args)
    except SystemExit as e:   # argparse 오류 등
        rc = e.code if isinstance(e.code, int) else 1
    _hold("\nPress Enter to close.")
    return rc


def run(main: Callable[[list[str]], int], args: list[str]) -> int:
    """탭·옆 창 안: srhbroker <args> 를 실행하고, 실패하면 오류를 읽을 수 있게 창을 잡아 둔다."""
    try:
        rc = main(args)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    except Exception as e:   # 포트 충돌 등 — 창이 바로 닫히면 원인을 볼 수 없다
        print(f"error: {type(e).__name__}: {e}")
        rc = 1
    if rc:
        _hold(f"\nsrhbroker {' '.join(args)} exited with {rc}. Press Enter to close.")
    return rc
