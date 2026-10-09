"""srhbroker watch — herdr 창에 띄워 두는 터미널 대시보드.

몇 초마다 monitor.snapshot 을 다시 그린다. 키: a 승인 · c 취소 · t 대상 지정 · d 상세 · f 흐름 필터 · r 새로고침 · q 종료.
작업은 ID 또는 '처리할 것' 목록 번호로 고른다. 승인·취소는 한 번 더 확인한다 (터미널의 사람 = CLI 권한).
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import unicodedata
from datetime import datetime
from typing import Any

from . import monitor
from .models import USER_ADDR, BrokerError

# ── 표시 폭 (한글은 2칸) ─────────────────────────────────────────────────


def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def fit(s: Any, n: int) -> str:
    """표시 폭 n 칸에 맞춰 자르거나(…) 공백으로 채운다."""
    s = " ".join(str("" if s is None else s).split())
    if width(s) <= n:
        return s + " " * (n - width(s))
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > n - 1:
            break
        out, w = out + ch, w + cw
    out += "…"
    return out + " " * (n - width(out))


_C = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36"}


def col(s: str, *names: str) -> str:
    if not names or os.environ.get("NO_COLOR"):
        return s
    return "\x1b[" + ";".join(_C[n] for n in names) + "m" + s + "\x1b[0m"


def _ago(ts: float | None) -> str:
    if not ts:
        return "-"
    s = max(0, int(time.time() - ts))
    return f"{s}초" if s < 120 else f"{s // 60}분" if s < 7200 else f"{s // 3600}시간"


def _iso_ago(iso: str | None) -> str:
    return _ago(datetime.fromisoformat(iso).timestamp()) if iso else "-"


_PANE = {"open": ("●", "green"), "closed": ("○", "dim"), "mismatch": ("◆", "red"), "unknown": ("?", "dim"),
         "worker": ("◇", "cyan")}
_KIND = {"approve": ("승인 대기", "yellow"), "route": ("대상 미정", "yellow"), "closed-pane": ("창 닫힘", "magenta"),
         "failed": ("실패", "red"), "mismatch": ("창 짝 불일치", "red"), "engine": ("엔진", "red")}
_TRANSPORT = {"broker": ("중계", "blue"), "claude-native": ("Claude", "magenta"), "herdr-direct": ("Codex직접", "cyan")}
_STATUS_COLOR = {"done": "green", "delivered": "cyan", "queued": "yellow", "held": "yellow", "needs_routing": "yellow",
                 "running": "cyan", "failed": "red", "timeout": "red", "expired": "red", "cancelled": "dim", "dropped": "dim", "sent": "magenta",
                 "not_delivered": "red"}
FILTERS = [None, "broker", "claude-native", "herdr-direct"]


def render(snap: dict[str, Any], flow_filter: str | None = None, cols: int = 120, rows: int = 40) -> list[str]:
    out: list[str] = []
    d = snap["daemon"]
    if not d["running"]:
        eng = col("데몬 꺼짐", "red", "bold")
    elif d.get("legacy"):
        eng = col("데몬 실행(이전 버전)", "yellow")
    else:
        eng = col("데몬 실행", "green") + f" pid {d['pid']} · 신호 {_ago(d.get('heartbeat'))} 전 · " + (
            col("herdr 전달 가능", "green") if d.get("herdr") else col("herdr 밖", "red"))
        if d.get("restart_needed"):
            eng += " · " + col("재시작 필요", "yellow", "bold")
    st = snap["stats"]
    f24 = st["flows_24h"]
    up = snap.get("update") or {}
    if up.get("available"):
        eng += " · " + col(f"새 버전 {up['latest']} — srhbroker update", "yellow")
    out.append(col(" SRH Broker ", "bold") + f" {datetime.now():%H:%M:%S}  {eng}")
    out.append(col(f" 24시간: 중계 {f24['broker']} · Claude 직접 {f24['claude-native']} · Codex 직접 {f24['herdr-direct']}"
                   + (f" · 전달 중앙값 {st['median_delivery_s_24h']}초" if st["median_delivery_s_24h"] is not None else ""),
                   "dim"))
    out.append("")

    att = snap["attention"]
    out.append(col(f"처리할 것 ({len(att)})", "bold") + ("" if att else col("  없음", "green")))
    for i, a in enumerate(att[:8], 1):
        label, c = _KIND.get(a["kind"], (a["kind"], "dim"))
        ref = a.get("task_id") or a.get("session") or ""
        line = f" [{i}] " + col(fit(label, 12), c) + " " + fit(ref, 26) + " " + fit(a["title"], max(20, cols - 46))
        out.append(line)
        if a.get("detail") and a["kind"] in ("approve", "engine", "closed-pane", "mismatch"):
            out.append("      " + col(fit(a["detail"], cols - 8), "dim"))
    out.append("")

    out.append(col("세션", "bold"))
    out.append(col(" " + fit("이름", 24) + fit("종류", 8) + fit("창", 18) + fit("받을것", 7) + fit("처리중", 7)
                   + fit("승인대기", 9) + "역할", "dim"))
    for s in snap["sessions"]:
        mark, c = _PANE[s["pane_state"]]
        pane = {"open": f"{s['pane_id']} {s['agent_status']}", "closed": "닫힘", "mismatch": f"{s['pane_id']} 불일치",
                "unknown": "?", "worker": "worker"}[s["pane_state"]]
        out.append(" " + fit(s["name"], 24) + fit(s["provider"], 8) + col(mark, c) + " " + fit(pane, 16)
                   + fit(s["queued"] or "", 7) + fit((s["delivered"] + s["running"]) or "", 7)
                   + fit(s["held"] or "", 9) + ",".join(s["roles"]))
    out.append("")

    flows = [f for f in snap["flows"] if not flow_filter or f["transport"] == flow_filter]
    title = {None: "전체", "broker": "중계", "claude-native": "Claude 직접", "herdr-direct": "Codex 직접"}[flow_filter]
    out.append(col(f"흐름 ({title})", "bold") + col("  f: 필터 전환", "dim"))
    room = max(3, rows - len(out) - 3)
    for f in flows[:room]:
        tlabel, tc = _TRANSPORT.get(f["transport"], (f["transport"], "dim"))
        sc = _STATUS_COLOR.get(f["status"], "dim")
        out.append(" " + fit(_iso_ago(f["at"]), 7) + col(fit(tlabel, 10), tc) + fit(f["from"], 22) + "→ "
                   + fit(f["to"] or "?", 22) + col(fit(f["status"], 14), sc)
                   + fit(f.get("summary") or "", max(10, cols - 80)))
    out.append("")
    out.append(col(" a 승인 · c 취소 · t 대상 지정 · d 상세 · f 필터 · r 새로고침 · q 종료   (작업은 ID 또는 [번호])", "dim"))
    return out


# ── 키 입력 (Windows: msvcrt, POSIX: select) ────────────────────────────


def _key(timeout: float) -> str | None:
    end = time.monotonic() + timeout
    if sys.platform == "win32":
        import msvcrt
        while time.monotonic() < end:
            if msvcrt.kbhit():
                return msvcrt.getwch()
            time.sleep(0.05)
        return None
    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1) if r else None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _pick(snap: dict[str, Any], raw: str) -> str | None:
    raw = raw.strip()
    if raw.isdigit():
        i = int(raw) - 1
        att = snap["attention"]
        return att[i].get("task_id") if 0 <= i < len(att) else None
    return raw or None


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _act(b: Any, snap: dict[str, Any], key: str) -> str:
    """키에 해당하는 조작을 하고 결과 한 줄을 돌려준다."""
    tid = _pick(snap, _ask("작업 ID 또는 [번호]: "))
    if not tid:
        return "취소됨"
    try:
        if key == "d":
            t = monitor.task_detail(b, tid)
            print(f"\n{t['task_id']} [{t['status']}] {t['from']} → {t['to']}" + (f"  권한 {t['sandbox']}" if t.get("sandbox") else ""))
            print(f"제목: {t['title']}\n\n중계 경과:")
            for e in t.get("timeline") or []:
                when = datetime.fromisoformat(e["at"]).astimezone().strftime("%m/%d %H:%M:%S") if e["at"] else "아직"
                after = "" if e["after_s"] is None else f"+{e['after_s']}초" if e["after_s"] < 120 else f"+{e['after_s'] // 60}분"
                tone = {"ok": "green", "wait": "yellow", "bad": "red"}.get(e["tone"])
                print(f"  {when} {after:>6}  " + (col(e["step"], tone) if tone else e["step"]) + f"  {e['text'][:200]}")
            print(f"\n보낸 내용:\n{t['body']}\n")
            if t.get("result") or t.get("error"):
                print("결과:\n" + (t.get("result") or t.get("error") or ""))
            _ask("\nEnter 로 돌아가기")
            return ""
        if key == "c":
            if _ask(f"{tid} 를 취소할까요? (y/N) ").strip().lower() != "y":
                return "취소 안 함"
            return f"취소: {b.cancel(tid, by=USER_ADDR)['status']}"
        if key == "a":
            t = monitor.task_detail(b, tid)
            print(f"제목: {t['title']}\n보류 사유: {t.get('safety')}")
            if _ask(f"{tid} 를 승인할까요? (y/N) ").strip().lower() != "y":
                return "승인 안 함"
            return f"승인: {b.approve(tid, confirmation='watch 화면에서 사용자가 승인', via='watch')['status']}"
        if key == "t":
            to = _ask("보낼 대상 (세션 이름·별칭·역할): ").strip()
            return f"대상 지정: {b.assign(tid, to)['to']}" if to else "취소됨"
    except BrokerError as e:
        return f"오류: {e}"
    return ""


def run(b: Any, interval: float = 3.0, once: bool = False) -> int:
    if sys.platform == "win32":
        os.system("")  # Windows 콘솔 ANSI 색상 켜기
    flow_filter: str | None = None
    note = ""
    while True:
        snap = monitor.snapshot(b)
        size = shutil.get_terminal_size((120, 40))
        lines = render(snap, flow_filter, size.columns, size.lines - (1 if note else 0))
        sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(lines) + (f"\n {note}" if note else "") + "\n")
        sys.stdout.flush()
        if once:
            return 0
        k = _key(interval)
        if not k:
            continue
        note = ""
        if k in ("q", "Q", "\x03"):
            return 0
        if k == "f":
            flow_filter = FILTERS[(FILTERS.index(flow_filter) + 1) % len(FILTERS)]
        elif k in ("a", "c", "t", "d"):
            note = _act(b, snap, k)
