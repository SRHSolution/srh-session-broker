"""herdr 플러그인: manifest 와 코드의 약속, startup·액션·창 명령, 업데이트 뒤 엔진 탭 다시 띄우기, build 단계."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from srhbroker import __version__, cli, plugin, updater
from srhbroker.dashboard import _Server, make_handler
from tests.conftest import make_broker

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = tomllib.loads((ROOT / "herdr-plugin.toml").read_text(encoding="utf-8"))


class FakeRun:
    """subprocess.run 대역: herdr 명령을 기록하고 정해 둔 JSON 을 돌려준다."""

    def __init__(self, result: dict[str, Any] | None = None, rc: int = 0):
        self.calls: list[list[str]] = []
        self.result = {"type": "plugin_pane_opened",
                       "plugin_pane": {"pane": {"pane_id": "w1:p9", "tab_id": "w1:t5"}}} if result is None else result
        self.rc = rc

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        out = json.dumps({"id": "cli:plugin", "result": self.result}) if "pane" in cmd and "open" in cmd else "{}"
        return subprocess.CompletedProcess(cmd, self.rc, out, "")


@pytest.fixture
def herdr_env(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(plugin, "_run", run)
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_BIN_PATH", "herdr")
    monkeypatch.setenv("HERDR_PLUGIN_ID", "srhbroker")
    return run


# ── manifest ↔ 코드 ────────────────────────────────────────────────────────

def test_manifest_matches_package():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert MANIFEST["version"] == project["version"] == __version__
    assert MANIFEST["id"] == plugin.PLUGIN_ID
    for key in ("name", "min_herdr_version", "description"):
        assert MANIFEST[key]
    for section in ("actions", "panes"):
        ids = [x["id"] for x in MANIFEST[section]]
        assert len(ids) == len(set(ids)), f"{section} id 중복 (herdr 가 링크를 거부)"
    assert {p["id"] for p in MANIFEST["panes"]} == {plugin.ENGINE, plugin.DASHBOARD, plugin.WATCH, plugin.OUTPUT}


def test_manifest_commands_are_valid_srhbroker_commands(capsys):
    cmds = [x["command"] for s in ("actions", "panes", "startup") for x in MANIFEST[s]]
    assert cmds and all(c[:2] == ["srhbroker", "plugin"] and c[2] in plugin.COMMANDS for c in cmds)
    # show·run 이 넘기는 명령은 실제 CLI 가 받는 명령이어야 한다 (--help 로 파싱만 확인)
    for c in cmds:
        if c[2] in ("show", "run"):
            with pytest.raises(SystemExit) as e:
                cli.main(c[3:] + ["--help"])
            assert e.value.code == 0, c
    assert MANIFEST["build"][0]["command"][-1] == "scripts/herdr_plugin_build.py"
    assert (ROOT / "scripts" / "herdr_plugin_build.py").is_file()


# ── 창 열기 ───────────────────────────────────────────────────────────────

def test_pane_open_cmd():
    assert plugin.pane_open_cmd("herdr", "engine", "tab", plugin="srhbroker", workspace="w2",
                                env={"SRHBROKER_HOME": "/h"}) == [
        "herdr", "plugin", "pane", "open", "--plugin", "srhbroker", "--entrypoint", "engine", "--placement", "tab",
        "--no-focus", "--workspace", "w2", "--env", "SRHBROKER_HOME=/h"]


def test_open_pane_renames_tab_and_accepts_popup_result(herdr_env, monkeypatch):
    assert plugin.open_pane("dashboard", "tab", tab_label="broker web")["pane_id"] == "w1:p9"
    assert herdr_env.calls[-1] == ["herdr", "tab", "rename", "w1:t5", "broker web"]
    monkeypatch.setattr(plugin, "_run", FakeRun({"type": "ok"}))   # 팝업은 창 정보 없이 ok
    assert plugin.open_pane("output", "popup") == {}
    monkeypatch.setattr(plugin, "_run", FakeRun(rc=1))
    assert plugin.open_pane("output", "popup") is None


# ── startup·엔진 ──────────────────────────────────────────────────────────

def _state(monkeypatch, **d):
    monkeypatch.setattr(plugin, "_engine_running", lambda b: d)


def test_startup_opens_engine_only_when_needed(tmp_path, herdr_env, monkeypatch):
    b = make_broker(tmp_path)
    _state(monkeypatch, running=True, pid=1, herdr_pane="w1:p2")
    assert plugin.startup(b, delay_s=0) == 0 and herdr_env.calls == []
    _state(monkeypatch, running=False)
    assert plugin.startup(b, delay_s=0) == 0
    assert herdr_env.calls[0][:9] == ["herdr", "plugin", "pane", "open", "--plugin", "srhbroker", "--entrypoint", "engine",
                                      "--placement"]
    herdr_env.calls.clear()
    b.cfg.data["plugin"] = {"autostart_engine": False}
    assert plugin.startup(b, delay_s=0) == 0 and herdr_env.calls == []
    b.cfg.data["plugin"] = {"autostart_engine": True}
    monkeypatch.setenv("HERDR_ENV", "0")
    assert plugin.startup(b, delay_s=0) == 0 and herdr_env.calls == []


def test_start_engine_action_notifies_when_running(tmp_path, herdr_env, monkeypatch):
    b = make_broker(tmp_path)
    _state(monkeypatch, running=True, pid=7, herdr_pane="w1:p2")
    assert plugin.start_engine(b) == 0
    assert herdr_env.calls[0][:3] == ["herdr", "notification", "show"]


def test_engine_runs_daemon_names_tab_and_skips_duplicate(tmp_path, herdr_env, monkeypatch):
    monkeypatch.setenv("SRHBROKER_HOME", str(tmp_path))
    monkeypatch.setenv("HERDR_TAB_ID", "w1:t3")
    monkeypatch.setattr(plugin.time, "sleep", lambda s: None)
    held: list[str] = []
    monkeypatch.setattr("builtins.input", lambda msg="": held.append(msg) or "")
    ran: list[int] = []
    assert plugin.engine(lambda: ran.append(1) or 0) == 0
    assert ran == [1] and ["herdr", "tab", "rename", "w1:t3", plugin.ENGINE_TAB] in herdr_env.calls and not held
    assert plugin.engine(lambda: 1) == 1 and held   # 시작 실패: 창을 잡아 두고 Enter 를 기다린다

    from srhbroker.dispatcher import SingleInstance
    lock = SingleInstance(tmp_path / "daemon.lock")
    assert lock.acquire()
    try:
        assert plugin.engine(lambda: pytest.fail("이미 실행 중이면 데몬을 띄우지 않는다")) == 0
    finally:
        lock.release()


# ── 액션·창 안 명령 ───────────────────────────────────────────────────────

def test_dashboard_action_opens_browser_or_tab(tmp_path, herdr_env, monkeypatch):
    b = make_broker(tmp_path)
    opened: list[str] = []
    monkeypatch.setattr(plugin, "dashboard_running", lambda port, timeout=1.0: True)
    assert plugin.dashboard(b, opener=opened.append) == 0
    token = (tmp_path / "dashboard.token").read_text(encoding="utf-8").strip()
    assert opened == [f"http://127.0.0.1:8765/?t={token}"] and herdr_env.calls == []
    monkeypatch.setattr(plugin, "dashboard_running", lambda port, timeout=1.0: False)
    assert plugin.dashboard(b, opener=opened.append) == 0
    assert "dashboard" in herdr_env.calls[0] and herdr_env.calls[-1][:3] == ["herdr", "tab", "rename"]


def test_show_passes_args_to_popup(herdr_env):
    assert plugin.show(["setup", "codex", "--apply"]) == 0
    cmd = herdr_env.calls[0]
    assert cmd[cmd.index("--placement") + 1] == "popup" and "--focus" in cmd
    assert cmd[cmd.index("--env") + 1] == f'{plugin.ARGS_ENV}=["setup", "codex", "--apply"]'
    assert plugin.show([]) == 2


def test_output_runs_command_then_waits(monkeypatch, capsys):
    held: list[str] = []
    monkeypatch.setattr("builtins.input", lambda msg="": held.append(msg) or "")
    got: list[list[str]] = []
    monkeypatch.setenv(plugin.ARGS_ENV, json.dumps(["doctor"]))
    assert plugin.output(lambda a: got.append(a) or 0) == 0
    assert got == [["doctor"]] and held and "$ srhbroker doctor" in capsys.readouterr().out
    monkeypatch.setenv(plugin.ARGS_ENV, "not json")
    assert plugin.output(lambda a: pytest.fail("잘못된 인자로 실행하지 않는다")) == 2


def test_run_holds_pane_on_failure(monkeypatch):
    held: list[str] = []
    monkeypatch.setattr("builtins.input", lambda msg="": held.append(msg) or "")
    assert plugin.run(lambda a: 0, ["watch"]) == 0 and not held

    def boom(a):
        raise OSError("port in use")
    assert plugin.run(boom, ["dashboard"]) == 1 and held


def test_dashboard_refuses_second_server_on_same_port(tmp_path):
    b = make_broker(tmp_path)
    first = _Server(("127.0.0.1", 0), make_handler(b, "t" * 24, 0))
    try:
        port = first.server_address[1]
        with pytest.raises(OSError):
            _Server(("127.0.0.1", port), make_handler(b, "t" * 24, port))
        assert plugin.dashboard_running(port, timeout=0.5) is False   # 응답하지 않는(serve_forever 전) 서버는 아님
    finally:
        first.server_close()


# ── 업데이트 뒤 다시 띄우기 ───────────────────────────────────────────────

def test_relaunch_command_by_daemon_kind():
    assert updater.relaunch_command("herdr", {"plugin": "srhbroker", "herdr_pane": "w1:p4", "herdr_workspace": "w1",
                                              "home_env": "/h"}) == plugin.pane_open_cmd(
        "herdr", "engine", "tab", plugin="srhbroker", workspace="w1", env={"SRHBROKER_HOME": "/h"})
    assert updater.relaunch_command("herdr", {"herdr_pane": "w1:p4"}) == ["herdr", "pane", "run", "w1:p4", "srhbroker daemon"]
    assert updater.relaunch_command("herdr", {}) is None


def test_daemon_state_records_plugin_engine(tmp_path, monkeypatch):
    from srhbroker.dispatcher import Dispatcher, SingleInstance
    from srhbroker.monitor import daemon_state
    b = make_broker(tmp_path)
    monkeypatch.setenv("HERDR_PLUGIN_ID", "srhbroker")
    monkeypatch.setenv("HERDR_PLUGIN_ENTRYPOINT_ID", "engine")
    monkeypatch.setenv("HERDR_WORKSPACE_ID", "w3")
    Dispatcher(b, adapters={}).write_state(0.0)
    lock = SingleInstance(tmp_path / "daemon.lock")
    assert lock.acquire()
    try:
        d = daemon_state(tmp_path)
    finally:
        lock.release()
    assert d["plugin"] == "srhbroker" and d["herdr_workspace"] == "w3"
    monkeypatch.setenv("HERDR_PLUGIN_ENTRYPOINT_ID", "watch")   # 엔진 탭이 아닌 플러그인 창(예: 사용자가 손으로 띄움)
    Dispatcher(b, adapters={}).write_state(0.0)
    assert json.loads((tmp_path / "daemon.json").read_text(encoding="utf-8"))["plugin"] is None


# ── build 단계 (scripts/herdr_plugin_build.py) ────────────────────────────

@pytest.fixture
def build(monkeypatch):
    spec = importlib.util.spec_from_file_location("herdr_plugin_build", ROOT / "scripts" / "herdr_plugin_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ran: list[list[str]] = []

    def fake_run(cmd, **kw):
        ran.append(list(cmd))
        return subprocess.CompletedProcess(cmd, mod.RC.get(tuple(cmd[1:2]), 0), "", "")
    mod.RC = {}
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/srhbroker" if name == "srhbroker" else None)
    mod.ran = ran
    return mod


def test_build_keeps_same_or_newer_install(build, monkeypatch):
    monkeypatch.setattr(build, "find_srhbroker", lambda: "/bin/srhbroker")
    monkeypatch.setattr(build, "installed_version", lambda exe: (99, 0, 0))
    assert build.main() == 0
    assert build.ran == [["/bin/srhbroker", "init"]]   # 설치를 건드리지 않는다


def test_build_installs_when_missing(build, monkeypatch):
    found = iter([None, "/bin/srhbroker"])
    monkeypatch.setattr(build, "find_srhbroker", lambda: next(found))
    assert build.main() == 0
    assert build.ran[0][:4] == ["uv", "tool", "install", "--force"] and "[jev] @ file:" in build.ran[0][4]
    assert build.ran[-1] == ["/bin/srhbroker", "init"]


def test_build_updates_older_install_and_stops_on_failure(build, monkeypatch):
    monkeypatch.setattr(build, "find_srhbroker", lambda: "/bin/srhbroker")
    versions = iter([(0, 5, 2), (99, 0, 0)])
    monkeypatch.setattr(build, "installed_version", lambda exe: next(versions))
    assert build.main() == 0 and ["/bin/srhbroker", "update"] in build.ran

    build.ran.clear()
    monkeypatch.setattr(build, "installed_version", lambda exe: (0, 5, 2))
    build.RC[("update",)] = 1          # Windows: 실행 중인 srhbroker 가 설치를 잠가 update 가 멈춤
    assert build.main() == 1 and ["/bin/srhbroker", "init"] not in build.ran

    monkeypatch.setattr(build, "installed_version", lambda exe: (0, 3, 0))   # update 명령이 없는 버전
    build.ran.clear()
    assert build.main() == 1 and build.ran == []
