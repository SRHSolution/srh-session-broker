"""srhbroker update — git 배포(저장소의 vX.Y.Z 태그)로 업데이트한다.

최신 버전은 저장소 태그에서 찾고(`git ls-remote`, 없으면 GitHub API) <브로커 홈>/update-check.json 에 캐시한다.
데몬은 몇 시간마다 이 캐시를 갱신하고, doctor·watch·대시보드는 캐시만 읽어 새 버전을 알린다(네트워크 없음).

설치 방식(패키지의 direct_url.json·uv 영수증)에 따라:
- editable  : 소스 git checkout → git pull --ff-only (태그에 멈춰 있으면 새 태그 checkout). 의존성이 바뀌었으면 재설치.
              .py 만 바뀌므로 Windows 에서도 실행 중에 된다.
- uv-tool   : uv tool install --force "<패키지> @ git+<저장소>@<태그>"
- pipx      : pipx install --force …
- pip-git   : pip(또는 uv pip) install --upgrade …
Windows 에서 uv tool·pipx·pip 재설치는 실행 중인 srhbroker 프로세스가 환경 파일을 잠가 실패하고, 환경이 반쯤 지워진다(실측).
그래서 다른 srhbroker 프로세스(데몬·대시보드·세션 MCP 서버)가 있으면 멈추고(--stop-all 이면 끝내고), 설치는 이 명령이
끝난 뒤 별도 프로세스(PowerShell)가 이어서 한다. 결과는 <브로커 홈>/update.log.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

DEFAULT_REPO = "https://github.com/SRHSolution/srh-session-broker"
DIST_NAMES = ("srh-session-broker", "srh-provider-broker")   # 두 번째는 0.2 이전 패키지 이름
_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _windows() -> bool:
    return os.name == "nt"


def parse_version(s: str | None) -> tuple[int, int, int] | None:
    m = _TAG.match((s or "").strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _dist() -> importlib.metadata.Distribution | None:
    for name in DIST_NAMES:
        try:
            return importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def current_version() -> str:
    from . import __version__
    return __version__


# ── 최신 버전 확인 ────────────────────────────────────────────────────────

def remote_versions(repo: str, timeout: float = 10.0) -> list[str]:
    """저장소의 vX.Y.Z 태그 목록. git 이 있으면 ls-remote, 없으면 GitHub API."""
    git = shutil.which("git")
    if git:
        r = subprocess.run([git, "ls-remote", "--tags", "--refs", repo], capture_output=True, text=True,
                           timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode == 0:
            return [ln.rsplit("refs/tags/", 1)[1] for ln in r.stdout.splitlines() if "refs/tags/" in ln]
    m = re.match(r"https://github\.com/([^/]+)/([^/.]+)", repo)
    if not m:
        raise RuntimeError(f"태그를 읽지 못했습니다: {repo} (git 이 없거나 저장소에 접근할 수 없음)")
    with urllib.request.urlopen(f"https://api.github.com/repos/{m[1]}/{m[2]}/tags?per_page=100", timeout=timeout) as resp:
        return [t["name"] for t in json.loads(resp.read().decode("utf-8"))]


def latest_tag(tags: list[str]) -> str | None:
    vs = [(parse_version(t), t) for t in tags]
    vs = [x for x in vs if x[0]]
    return max(vs)[1] if vs else None


def _summary(cache: dict[str, Any]) -> dict[str, Any]:
    cur, latest = current_version(), cache.get("latest")
    out = {"current": cur, "latest": latest, "checked_at": cache.get("checked_at"), "repo": cache.get("repo"),
           "available": bool(parse_version(latest) and parse_version(cur) and parse_version(latest) > parse_version(cur))}
    if cache.get("error"):
        out["error"] = cache["error"]
    return out


def check(home: Path, repo: str = DEFAULT_REPO, *, max_age_h: float = 12, force: bool = False,
          fetch: Callable[[str], list[str]] | None = None) -> dict[str, Any]:
    """새 버전 확인. 캐시가 max_age_h 시간 안이면 네트워크를 쓰지 않는다."""
    path = home / "update-check.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    if not force and cache.get("repo") == repo and time.time() - float(cache.get("checked_at") or 0) < max_age_h * 3600:
        return _summary(cache)
    try:
        cache = {"repo": repo, "checked_at": time.time(), "latest": latest_tag((fetch or remote_versions)(repo))}
    except Exception as e:  # 네트워크 오류로 아무것도 막지 않는다
        cache = {**cache, "repo": repo, "checked_at": time.time(), "error": str(e)[:200]}
    try:
        home.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
    return _summary(cache)


def cached(home: Path) -> dict[str, Any] | None:
    """캐시만 읽는다 (watch·대시보드용, 네트워크 없음)."""
    try:
        return _summary(json.loads((home / "update-check.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


# ── 설치 방식 ─────────────────────────────────────────────────────────────

def install_info() -> dict[str, Any]:
    d = _dist()
    direct: dict[str, Any] = {}
    if d is not None:
        try:
            direct = json.loads(d.read_text("direct_url.json") or "{}")
        except ValueError:
            direct = {}
    prefix = Path(sys.prefix)
    info: dict[str, Any] = {"dist": d.metadata["Name"] if d else None, "prefix": str(prefix),
                            "jev": importlib.util.find_spec("typesafe_sdk") is not None, "repo": None}
    url = direct.get("url", "")
    if (direct.get("dir_info") or {}).get("editable"):
        info.update(kind="editable", path=urllib.request.url2pathname(urllib.parse.urlparse(url).path))
    elif (prefix / "uv-receipt.toml").exists():
        info["kind"] = "uv-tool"
    elif "pipx" in prefix.parts and "venvs" in prefix.parts:
        info["kind"] = "pipx"
    elif direct.get("vcs_info"):
        info["kind"] = "pip-git"
    else:
        info["kind"] = "other"
    if direct.get("vcs_info") and url:
        info["repo"] = re.sub(r"\.git$", "", url.removeprefix("git+"))
    return info


def _spec(info: dict[str, Any], repo: str, tag: str) -> str:
    return f"srh-session-broker{'[jev]' if info.get('jev') else ''} @ git+{repo}@{tag}"


def plan(info: dict[str, Any], repo: str, tag: str) -> list[list[str]]:
    """재설치형(uv-tool·pipx·pip-git) 업데이트 명령."""
    spec = _spec(info, repo, tag)
    if info["kind"] == "uv-tool":
        return [["uv", "tool", "install", "--force", spec]]
    if info["kind"] == "pipx":
        return [["pipx", "install", "--force", spec]]
    if info["kind"] == "pip-git":
        if importlib.util.find_spec("pip"):
            return [[sys.executable, "-m", "pip", "install", "--upgrade", spec]]
        return [["uv", "pip", "install", "--python", sys.executable, "--upgrade", spec]]
    raise RuntimeError(f"자동 업데이트를 지원하지 않는 설치 방식입니다 ({info['kind']}). README 의 설치 방법으로 다시 설치하세요")


def _git(path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)


def update_editable(path: str, tag: str | None, out: Callable[[str], None]) -> bool:
    """소스 checkout 업데이트: 깨끗한 작업 트리에서만. 브랜치면 pull --ff-only, 태그에 멈춰 있으면 새 태그 checkout."""
    if _git(path, "rev-parse", "--is-inside-work-tree").returncode != 0:
        out(f"✖ {path} 는 git 저장소가 아닙니다 — 직접 업데이트하세요")
        return False
    if _git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        out(f"✖ {path} 에 커밋하지 않은 변경이 있습니다 — 정리한 뒤 다시 실행하세요")
        return False
    before = _git(path, "rev-parse", "HEAD").stdout.strip()
    _git(path, "fetch", "--tags", "--quiet")
    branch = _git(path, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if branch:
        if _git(path, "rev-parse", "--abbrev-ref", "@{u}").returncode != 0:
            out(f"✖ 브랜치 {branch} 에 추적 원격이 없습니다 — git pull 할 대상을 정하세요")
            return False
        r = _git(path, "pull", "--ff-only", "--quiet")
        if r.returncode != 0:
            out(f"✖ git pull --ff-only 실패 (갈라진 이력?): {r.stderr.strip()[:200]}")
            return False
    elif tag:
        r = _git(path, "checkout", "--quiet", tag)
        if r.returncode != 0:
            out(f"✖ git checkout {tag} 실패: {r.stderr.strip()[:200]}")
            return False
    after = _git(path, "rev-parse", "HEAD").stdout.strip()
    if before == after:
        out("· 소스가 이미 최신입니다")
        return True
    out(f"✔ 소스 갱신 {before[:7]} → {after[:7]}" + (f" ({branch})" if branch else f" ({tag})"))
    if _git(path, "diff", "--name-only", before, after, "--", "pyproject.toml").stdout.strip():
        cmd = (["uv", "pip", "install", "--python", sys.executable, "-e", path] if shutil.which("uv")
               else [sys.executable, "-m", "pip", "install", "-e", path])
        r = subprocess.run(cmd, capture_output=True, text=True)
        out("✔ 의존성 다시 설치" if r.returncode == 0 else f"! 의존성 설치 실패 — 직접 실행: {' '.join(cmd)}")
    return True


# ── Windows: 환경을 잠그는 다른 srhbroker 프로세스 ─────────────────────────

def other_processes(prefix: str) -> list[dict[str, Any]]:
    """같은 설치 환경에서 실행 중인 다른 srhbroker 프로세스 (Windows 만 의미 있음). 이 명령 자신과 부모는 뺀다."""
    if not _windows():
        return []
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'srhbroker' } | "
          "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
        rows = json.loads(r.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    rows = rows if isinstance(rows, list) else [rows]
    return select_env_processes(rows, prefix, ancestors(rows, os.getpid()) | {os.getppid()})


def ancestors(rows: list[dict[str, Any]], pid: int) -> set[int]:
    """이 프로세스와 그 조상들. Windows 가상환경은 실행기(srhbroker.exe) → 가상환경 python(리디렉터) → 실제 python
    으로 이어지므로 부모 하나만 빼면 자기 실행기가 '다른 프로세스'로 잡혀 --stop-all 이 자신을 끝낸다."""
    parent = {p["ProcessId"]: p.get("ParentProcessId") for p in rows}
    out, cur = {pid}, pid
    while parent.get(cur) in parent and parent[cur] not in out:
        cur = parent[cur]
        out.add(cur)
    return out


def select_env_processes(rows: list[dict[str, Any]], prefix: str, me: set[int]) -> list[dict[str, Any]]:
    """업데이트할 환경의 python(경로가 prefix 아래)을 실행 중인 프로세스와 그 실행기(부모 srhbroker.exe)만 고른다.
    PATH 에 있는 다른 설치의 srhbroker(예: 개발용 다른 환경의 데몬)는 고르지 않는다."""
    pre = prefix.rstrip("\\/").lower() + os.sep if prefix else ""
    norm = lambda p: (p.get("ExecutablePath") or "").replace("/", os.sep).lower()
    env_pids = {p["ProcessId"] for p in rows if pre and norm(p).startswith(pre.replace("/", os.sep))}
    parents = {p.get("ParentProcessId") for p in rows if p["ProcessId"] in env_pids}
    out = []
    for p in rows:
        pid, cmd = p["ProcessId"], (p.get("CommandLine") or "")
        if pid in me or not (pid in env_pids or (pid in parents and norm(p).endswith("srhbroker.exe"))):
            continue
        kind = next((k for k in ("daemon", "dashboard", "watch", "mcp", "hook", "deliver", "update") if f" {k}" in cmd), "기타")
        out.append({"pid": pid, "kind": kind, "cmd": cmd[:160]})
    return out


def stop(procs: list[dict[str, Any]]) -> None:
    for p in procs:
        subprocess.run(["taskkill", "/PID", str(p["pid"]), "/T", "/F"], capture_output=True)


def spawn_deferred(steps: list[list[str]], post: list[list[str]], log: Path, wait_pid: int) -> None:
    """이 프로세스가 끝난 뒤 설치를 이어서 할 PowerShell 을 띄운다 (Windows)."""
    q = lambda a: "'" + a.replace("'", "''") + "'"
    out = f"Out-File -Append -Encoding utf8 {q(str(log))}"
    lines = [f"Wait-Process -Id {wait_pid} -ErrorAction SilentlyContinue", "Start-Sleep -Seconds 1",
             "$ErrorActionPreference = 'Continue'", "$rc = 0",
             f"'--- ' + (Get-Date -Format s) + ' srhbroker update' | {out}"]
    for i, cmd in enumerate(steps + post):
        # 네이티브 명령의 stderr 를 오류 레코드가 아닌 평범한 줄로 남긴다 ("$_" 로 문자열화)
        lines.append(f"'$ ' + {q(' '.join(cmd))} | {out}")
        lines.append(f"& {q(cmd[0])} {' '.join(q(a) for a in cmd[1:])} 2>&1 | ForEach-Object {{ "
                     "if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { \"$_\" } }"
                     f" | {out}")
        if i < len(steps):
            lines.append("if ($LASTEXITCODE -ne 0) { $rc = $LASTEXITCODE }")
    lines.append(f"'--- done ' + $(if ($rc -eq 0) {{ 'ok' }} else {{ 'failed rc=' + $rc }}) | {out}")
    script = Path(tempfile.gettempdir()) / f"srhbroker-update-{os.getpid()}.ps1"
    script.write_text("\n".join(lines), encoding="utf-8-sig")
    # 이 명령을 실행한 터미널·도구가 끝날 때 자식 프로세스를 함께 정리해도 살아남도록 작업 그룹에서 빠져나와 창 없이 실행한다
    # (DETACHED_PROCESS 만으로는 정리되는 것을 실측). 빠져나오기가 허용되지 않는 환경이면 일반 분리 실행으로
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    base = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(cmd, creationflags=base | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0), close_fds=True)
    except OSError:
        subprocess.Popen(cmd, creationflags=base, close_fds=True)


# ── 업데이트 실행 ─────────────────────────────────────────────────────────

def _codex_has_broker() -> bool:
    from .onboard import _codex_cfg
    return bool((_codex_cfg()[1].get("mcp_servers") or {}).get("srhbroker"))


def _post_steps(exe: str) -> list[list[str]]:
    """새 코드로 실행할 후속 작업: 새 버전에서 늘어난 Codex 도구 자동 승인."""
    return [[exe, "setup", "codex", "--apply"]] if _codex_has_broker() else []


def restart_daemon(b: Any, out: Callable[[str], None]) -> None:
    """herdr 창에서 돌던 데몬을 그 창에서 다시 띄운다. 실행 중인 worker 작업이 있으면 하지 않는다."""
    from .dispatcher import SingleInstance
    from .monitor import daemon_state
    from .models import TaskStatus
    d = daemon_state(b.cfg.home)
    if not d.get("running"):
        return
    pane = d.get("herdr_pane")
    if not pane or not b.herdr.available():
        out("! 데몬은 예전 코드로 실행 중입니다 — 데몬 창에서 Ctrl+C 후 srhbroker daemon 으로 다시 띄우세요")
        return
    if b.store.list_tasks(status=TaskStatus.RUNNING.value, limit=1):
        out(f"! 실행 중인 worker 작업이 있어 데몬을 재시작하지 않았습니다 — 끝난 뒤 창 {pane} 에서 다시 띄우세요")
        return
    herdr = b.herdr.binary()
    subprocess.run([herdr, "pane", "send-keys", pane, "ctrl+c"], capture_output=True)
    for _ in range(20):
        lock = SingleInstance(b.cfg.home / "daemon.lock")
        if lock.acquire():
            lock.release()
            break
        time.sleep(0.5)
    r = subprocess.run([herdr, "pane", "run", pane, "srhbroker daemon"], capture_output=True)
    out(f"✔ 데몬을 창 {pane} 에서 새 코드로 다시 띄웠습니다" if r.returncode == 0
        else f"! 데몬 재시작 실패 — 창 {pane} 에서 srhbroker daemon")


def repo_for(b: Any) -> str:
    return install_info().get("repo") or b.cfg.data.get("update", {}).get("repo") or DEFAULT_REPO


def background_check(b: Any) -> dict[str, Any] | None:
    """데몬·doctor 용: 설정이 켜져 있으면 캐시 주기(check_hours)에 맞춰 확인한다."""
    u = b.cfg.data.get("update", {})
    if not u.get("check", True):
        return cached(b.cfg.home)
    return check(b.cfg.home, repo_for(b), max_age_h=float(u.get("check_hours", 12)))


def self_exe() -> str:
    """지금 실행 중인 설치의 srhbroker 실행 파일 (후속 단계는 업데이트한 그 설치로 실행해야 한다).
    PATH 의 srhbroker 는 다른 설치일 수 있다."""
    p = Path(sys.argv[0])
    for cand in (p, p.with_suffix(".exe")):
        if cand.name.lower().startswith("srhbroker") and cand.is_file():
            return str(cand.resolve())
    return shutil.which("srhbroker") or "srhbroker"


def run(b: Any, *, check_only: bool = False, to: str | None = None, stop_all: bool = False,
        restart: bool = True, out: Callable[[str], None] = print) -> int:
    ucfg = b.cfg.data.get("update", {})
    info = install_info()
    repo = info.get("repo") or ucfg.get("repo") or DEFAULT_REPO
    st = check(b.cfg.home, repo, force=True)
    target = to or st.get("latest")
    out(f"설치 방식: {info['kind']}" + (f" ({info.get('path')})" if info.get("path") else "") + f" · 저장소: {repo}")
    out(f"지금 버전: {st['current']} · 최신 버전: {st.get('latest') or '알 수 없음'}" + (f" ({st['error']})" if st.get("error") else ""))
    if check_only:
        out("→ srhbroker update 로 올릴 수 있습니다" if st["available"] else "→ 최신입니다")
        return 0
    if to and not parse_version(to):
        out(f"✖ 버전 형식이 아닙니다: {to} (예: v0.3.0)")
        return 2
    if not target:
        out("✖ 최신 버전을 알 수 없습니다 — 네트워크·저장소 주소를 확인하세요")
        return 1
    if not to and not st["available"] and info["kind"] != "editable":
        out("✔ 이미 최신입니다")
        return 0
    exe = self_exe()

    if info["kind"] == "editable":
        if not update_editable(info["path"], target if to else None, out):
            return 1
        for cmd in _post_steps(exe):
            subprocess.run(cmd)
        if restart:
            restart_daemon(b, out)
        _notes(out, in_place=True)
        return 0

    try:
        steps = plan(info, repo, target)
    except RuntimeError as e:
        out(f"✖ {e}")
        return 1
    if _windows():
        procs = other_processes(info["prefix"])
        if procs and not stop_all:
            out("✖ Windows 에서는 실행 중인 srhbroker 가 설치 파일을 잠가, 이대로 바꾸면 설치가 깨집니다. 먼저 끝내야 할 프로세스:")
            for p in procs:
                out(f"   - pid {p['pid']} [{p['kind']}] {p['cmd']}")
            out("  → 데몬·대시보드 창을 닫고 Claude/Codex 창을 모두 닫은 뒤 다시 실행하거나,")
            out("    srhbroker update --stop-all 로 모두 끝내고 업데이트하세요 (열린 Claude 창은 /mcp 에서 srhbroker 를 다시 연결,")
            out("    Codex 창은 다시 열어야 합니다)")
            return 1
        daemon_pane = None
        if procs:
            from .monitor import daemon_state
            d = daemon_state(b.cfg.home)
            daemon_pane = d.get("herdr_pane") if d.get("running") and b.herdr.available() else None
            stop(procs)
            out(f"✔ srhbroker 프로세스 {len(procs)}개를 끝냈습니다")
        log = b.cfg.home / "update.log"
        post = _post_steps(exe)
        if daemon_pane and restart:
            post.append([b.herdr.binary(), "pane", "run", daemon_pane, "srhbroker daemon"])
        spawn_deferred(steps, post, log, os.getpid())
        out(f"✔ {target} 설치를 이어서 진행합니다 (이 명령이 끝나면 시작, 기록: {log}).")
        out("  몇 초 뒤 srhbroker doctor 로 버전을 확인하세요.")
        _notes(out, in_place=False)
        return 0

    for cmd in steps:
        out("$ " + " ".join(cmd))
        r = subprocess.run(cmd)
        if r.returncode != 0:
            out(f"✖ 업데이트 실패 (rc={r.returncode})")
            return 1
    for cmd in _post_steps(exe):
        subprocess.run(cmd)
    out(f"✔ {target} 로 업데이트했습니다")
    if restart:
        restart_daemon(b, out)
    _notes(out, in_place=True)
    return 0


def _notes(out: Callable[[str], None], *, in_place: bool) -> None:
    out("참고: 이미 열린 Claude 창은 /mcp 에서 srhbroker 를 다시 연결해야 새 MCP 도구가 보이고, Codex 창은 다시 열어야 합니다.")
    if in_place:
        out("      웹 대시보드(srhbroker dashboard)를 띄워 두었다면 다시 시작하세요.")
