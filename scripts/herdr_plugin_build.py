# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""herdr 플러그인 build 단계 — `herdr plugin install` 이 플러그인 폴더(이 저장소 checkout)에서 실행한다.

- srhbroker 가 없으면 이 checkout 을 uv tool 로 설치한다 ([jev] 포함, 실패하면 기본만).
- 이미 같거나 새 버전이 있으면 그대로 둔다 (소스 checkout·pipx 등 다른 설치 방식을 덮어쓰지 않는다).
- 예전 버전이면 그 설치의 `srhbroker update` 에 맡긴다. Windows 에서 실행 중인 srhbroker 가 설치를 잠그면 update 가
  멈추고 무엇을 끝내야 하는지 알려 준다 — 그때는 build 를 실패로 끝내 플러그인 설치를 멈춘다.
herdr 는 build 명령에 플러그인 환경 변수를 넘기지 않는다. 출력은 `herdr plugin install` 화면에 보인다.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = "srh-session-broker"
FIRST_WITH_UPDATE = (0, 4, 0)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def vtuple(s: str | None) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", s or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def tool_bin_dir() -> Path | None:
    try:
        r = subprocess.run(["uv", "tool", "dir", "--bin"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def find_srhbroker() -> str | None:
    """PATH 의 srhbroker, 없으면 uv tool 실행 파일 폴더 (PATH 에 아직 없을 수 있다)."""
    exe = shutil.which("srhbroker")
    if exe:
        return exe
    d = tool_bin_dir()
    for name in ("srhbroker.exe", "srhbroker"):
        if d and (d / name).is_file():
            return str(d / name)
    return None


def installed_version(exe: str) -> tuple[int, int, int] | None:
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return vtuple(r.stdout)


def install() -> bool:
    for i, spec in enumerate((f"{DIST}[jev] @ {ROOT.as_uri()}", f"{DIST} @ {ROOT.as_uri()}")):
        say(f"$ uv tool install --force \"{spec}\"")
        if subprocess.run(["uv", "tool", "install", "--force", spec]).returncode == 0:
            return True
        if i == 0:
            say("  retrying without the optional AI router extra [jev]")
    return False


def main() -> int:
    for stream in (sys.stdout, sys.stderr):   # Windows 콘솔·파이프 기본 인코딩(cp949 등)으로 출력이 깨지지 않게
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    want_s = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    want = vtuple(want_s)
    exe = find_srhbroker()
    have = installed_version(exe) if exe else None

    if exe and have and have >= want:
        say(f"srhbroker {'.'.join(map(str, have))} is already installed ({exe}) - keeping it.")
    elif exe and have:
        cur = ".".join(map(str, have))
        if have < FIRST_WITH_UPDATE:
            say(f"srhbroker {cur} is too old to update itself. Run:  uv tool upgrade {DIST}  (or pipx upgrade {DIST})")
            say("then install the plugin again.")
            return 1
        say(f"srhbroker {cur} is older than the plugin ({want_s}) - updating it with `srhbroker update`.")
        if subprocess.run([exe, "update"]).returncode != 0:
            say()
            say("The update did not finish (see above). On Windows, running srhbroker processes lock the install:")
            say("run  srhbroker update --stop-all  and then install the plugin again.")
            return 1
        now = installed_version(exe)
        if not now or now < want:
            say(f"srhbroker is still {'.'.join(map(str, now or have))} (the plugin needs {want_s}). Install this version with:")
            say(f"  uv tool install --force \"{DIST}[jev] @ {ROOT.as_uri()}\"")
            return 1
    else:
        say(f"Installing srhbroker {want_s} with uv ...")
        if not install():
            say("Install failed. Install manually and retry:")
            say(f"  uv tool install \"{DIST}[jev] @ git+https://github.com/SRHSolution/srh-session-broker\"")
            return 1
        exe = find_srhbroker()
        if not exe:
            say("Installed, but the srhbroker executable was not found.")
            return 1

    subprocess.run([exe, "init"])   # ~/.srhbroker/config.toml (없을 때만)
    say()
    if not shutil.which("srhbroker"):
        say(f"! {Path(exe).parent} is not on PATH, so herdr cannot run the plugin commands yet.")
        say("  Run  uv tool update-shell , then restart herdr (herdr server stop, then herdr).")
        say()
    say("Next steps in herdr (plugin actions):")
    say("  1. Connect Claude Code to the broker  /  Connect Codex to the broker")
    say("  2. Check broker setup")
    say("  3. In each Claude Code / Codex pane: /rename <name>, then ask it to 'register this session with the broker'")
    say("The broker engine starts in its own tab when herdr starts ([plugin] autostart_engine in ~/.srhbroker/config.toml).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
