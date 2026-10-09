"""git 배포 업데이트: 태그로 최신 버전 확인(캐시), 설치 방식별 명령, 소스 checkout 업데이트, Windows 잠금 대응, 알림."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from srhbroker import monitor, updater, watch
from tests.conftest import make_broker


def test_versions_and_latest_tag():
    assert updater.parse_version("v0.3.10") == (0, 3, 10) and updater.parse_version("0.3.0") == (0, 3, 0)
    assert updater.parse_version("v1.0") is None and updater.parse_version("release-1") is None
    assert updater.latest_tag(["v0.2.0", "v0.10.0", "v0.9.9", "nightly"]) == "v0.10.0"


def test_check_caches_and_reports_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "current_version", lambda: "0.3.0")
    calls = []
    fetch = lambda repo: calls.append(repo) or ["v0.3.0", "v0.3.1"]
    s = updater.check(tmp_path, "https://example/repo", fetch=fetch)
    assert (s["latest"], s["available"]) == ("v0.3.1", True)
    updater.check(tmp_path, "https://example/repo", fetch=fetch)          # 캐시 — 다시 묻지 않는다
    assert len(calls) == 1
    updater.check(tmp_path, "https://example/repo", fetch=fetch, force=True)
    assert len(calls) == 2
    assert updater.cached(tmp_path)["available"] is True

    def boom(repo):
        raise RuntimeError("network down")
    s = updater.check(tmp_path, "https://example/repo", fetch=boom, force=True)
    assert "network down" in s["error"] and s["latest"] == "v0.3.1"      # 이전 결과는 남긴다


def test_plan_commands_by_install_kind(monkeypatch):
    repo = "https://github.com/SRHSolution/srh-session-broker"
    info = {"kind": "uv-tool", "jev": True}
    assert updater.plan(info, repo, "v0.3.1") == [
        ["uv", "tool", "install", "--force", f"srh-session-broker[jev] @ git+{repo}@v0.3.1"]]
    assert updater.plan({"kind": "pipx", "jev": False}, repo, "v0.3.1")[0][:3] == ["pipx", "install", "--force"]
    with pytest.raises(RuntimeError, match="지원하지 않는"):
        updater.plan({"kind": "other"}, repo, "v0.3.1")


def test_install_info_detects_kind(monkeypatch, tmp_path):
    class D:
        metadata = {"Name": "srh-session-broker"}

        def __init__(self, direct):
            self.direct = direct

        def read_text(self, name):
            return json.dumps(self.direct)

    monkeypatch.setattr(updater.sys, "prefix", str(tmp_path / "env"))
    monkeypatch.setattr(updater, "_dist", lambda: D({"url": tmp_path.as_uri(), "dir_info": {"editable": True}}))
    i = updater.install_info()
    assert i["kind"] == "editable" and Path(i["path"]) == tmp_path
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "uv-receipt.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(updater, "_dist", lambda: D({"url": "git+https://github.com/x/y.git",
                                                    "vcs_info": {"vcs": "git", "commit_id": "abc"}}))
    i = updater.install_info()
    assert (i["kind"], i["repo"]) == ("uv-tool", "https://github.com/x/y")


@pytest.mark.skipif(not shutil.which("git"), reason="git 필요")
def test_update_editable_pulls_and_refuses_dirty_tree(tmp_path):
    def g(cwd, *a):
        subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)
    origin, clone = tmp_path / "origin", tmp_path / "clone"
    origin.mkdir()
    g(origin, "init", "-q", "-b", "main")
    g(origin, "config", "user.email", "t@t")
    g(origin, "config", "user.name", "t")
    (origin / "a.py").write_text("v = 1\n", encoding="utf-8")
    g(origin, "add", "-A")
    g(origin, "commit", "-q", "-m", "1")
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    (origin / "a.py").write_text("v = 2\n", encoding="utf-8")
    g(origin, "commit", "-q", "-am", "2")
    msgs: list[str] = []
    assert updater.update_editable(str(clone), None, msgs.append)
    assert (clone / "a.py").read_text(encoding="utf-8") == "v = 2\n" and "소스 갱신" in msgs[-1]
    (clone / "a.py").write_text("local\n", encoding="utf-8")
    assert not updater.update_editable(str(clone), None, msgs.append)
    assert "커밋하지 않은 변경" in msgs[-1]


def test_windows_refuses_while_other_processes_hold_the_install(tmp_path, monkeypatch):
    b = make_broker(tmp_path)
    monkeypatch.setattr(updater, "current_version", lambda: "0.2.1")
    monkeypatch.setattr(updater, "install_info", lambda: {"kind": "uv-tool", "jev": False, "prefix": "C:/env", "repo": None})
    monkeypatch.setattr(updater, "_windows", lambda: True)
    monkeypatch.setattr(updater, "other_processes",
                        lambda prefix: [{"pid": 11, "kind": "mcp", "cmd": "srhbroker mcp"}, {"pid": 12, "kind": "daemon", "cmd": "srhbroker daemon"}])
    spawned, stopped, msgs = [], [], []
    monkeypatch.setattr(updater, "spawn_deferred", lambda *a: spawned.append(a))
    monkeypatch.setattr(updater, "stop", lambda procs: stopped.extend(procs))
    assert updater.run(b, out=msgs.append) == 1                            # 잠그는 프로세스가 있으면 멈춘다
    assert not spawned and any("설치가 깨집니다" in m for m in msgs) and any("pid 11 [mcp]" in m for m in msgs)
    msgs.clear()
    assert updater.run(b, stop_all=True, out=msgs.append) == 0             # --stop-all: 끝내고 이어서 설치
    assert [p["pid"] for p in stopped] == [11, 12] and spawned
    steps = spawned[0][0]
    assert steps[0][:4] == ["uv", "tool", "install", "--force"] and steps[0][4].endswith("@v0.3.0")


def test_check_only_and_already_latest(tmp_path, monkeypatch):
    b = make_broker(tmp_path)
    monkeypatch.setattr(updater, "install_info", lambda: {"kind": "uv-tool", "jev": False, "prefix": "x", "repo": None})
    msgs: list[str] = []
    monkeypatch.setattr(updater, "current_version", lambda: "0.2.1")
    assert updater.run(b, check_only=True, out=msgs.append) == 0
    assert "최신 버전: v0.3.0" in msgs[1] and "srhbroker update 로" in msgs[-1]
    monkeypatch.setattr(updater, "current_version", lambda: "0.3.0")
    msgs.clear()
    assert updater.run(b, out=msgs.append) == 0 and "이미 최신" in msgs[-1]


def test_new_version_is_shown_in_snapshot_and_watch(tmp_path, monkeypatch):
    b = make_broker(tmp_path)
    monkeypatch.setattr(updater, "current_version", lambda: "0.2.1")
    updater.check(b.cfg.home, updater.DEFAULT_REPO)
    snap = monitor.snapshot(b)
    assert snap["update"]["available"] and snap["update"]["latest"] == "v0.3.0"
    head = watch.render(snap, cols=120, rows=30)[0]
    assert "새 버전 v0.3.0" in head and "srhbroker update" in head


def test_select_env_processes_ignores_other_installs(monkeypatch):
    """업데이트할 환경의 python 과 그 실행기만 고른다 — PATH 의 다른 설치(개발용 데몬 등)는 건드리지 않는다."""
    monkeypatch.setattr(updater.os, "sep", "\\")
    env = r"C:\tools\srh-session-broker"
    rows = [
        {"ProcessId": 1, "ParentProcessId": 0, "ExecutablePath": r"C:\Users\u\.local\bin\srhbroker.exe", "CommandLine": "srhbroker.exe daemon"},
        {"ProcessId": 2, "ParentProcessId": 1, "ExecutablePath": r"C:\other-env\Scripts\python.exe", "CommandLine": "python srhbroker daemon"},
        {"ProcessId": 3, "ParentProcessId": 0, "ExecutablePath": r"C:\bin\srhbroker.exe", "CommandLine": "srhbroker.exe watch"},
        {"ProcessId": 4, "ParentProcessId": 3, "ExecutablePath": env + r"\Scripts\python.exe", "CommandLine": "python srhbroker watch"},
        {"ProcessId": 5, "ParentProcessId": 0, "ExecutablePath": env + r"\Scripts\python.exe", "CommandLine": "python srhbroker update"},
        {"ProcessId": 6, "ParentProcessId": 0, "ExecutablePath": env + "-old\Scripts\python.exe", "CommandLine": "python srhbroker mcp"},
    ]
    got = updater.select_env_processes(rows, env, me={5})
    assert [(p["pid"], p["kind"]) for p in got] == [(3, "watch"), (4, "watch")]


def test_ancestors_excludes_own_launcher_chain():
    rows = [{"ProcessId": 10, "ParentProcessId": 1}, {"ProcessId": 11, "ParentProcessId": 10},
            {"ProcessId": 12, "ParentProcessId": 11}, {"ProcessId": 20, "ParentProcessId": 1}]
    assert updater.ancestors(rows, 12) == {10, 11, 12}                   # 실행기 → 리디렉터 → 실제 python
