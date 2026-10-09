"""모니터링 데이터 계층: status · watch(터미널) · dashboard(웹)가 같은 스냅샷을 쓴다. 읽기 전용.

흐름(flows)은 세 경로를 합친다.
- broker      : Claude ↔ Codex 중계 작업 (tasks 테이블)
- herdr-direct: Codex ↔ Codex 직접 전달 (direct_queue — 대기·전달·취소·만료)
- claude-native: Claude ↔ Claude SendMessage (flow_log, PostToolUse hook 이 관찰만 기록)
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import herdr as herdr_mod
from .models import BrokerError, Task, TaskStatus

ACTIVE = ("queued", "running", "delivered", "held", "needs_routing")


def _ts(iso: str | None) -> float | None:
    return datetime.fromisoformat(iso).timestamp() if iso else None


def daemon_state(home: Path) -> dict[str, Any]:
    """데몬 실행 여부(잠금 파일)와 상태 파일(daemon.json)."""
    from .dispatcher import SingleInstance, code_mtime
    lock = SingleInstance(home / "daemon.lock")
    running = not lock.acquire()
    if not running:
        lock.release()
        return {"running": False}
    try:
        st = json.loads((home / "daemon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": True, "legacy": True}
    return {"running": True, "legacy": False, "pid": st.get("pid"), "started_at": st.get("started_at"),
            "heartbeat": st.get("heartbeat"), "herdr": bool(st.get("herdr")), "herdr_pane": st.get("herdr_pane"),
            "restart_needed": code_mtime() > float(st.get("code_mtime", 0)) + 1}


def _hold_reason(t: Task) -> str:
    """승인 대기 사유를 사람이 읽는 말로: 승인 판단 · 걸린 하드웨어 규칙 · Jev 판단."""
    rm = t.route_meta or {}
    sf = rm.get("safety") or {}
    srcs = [x.removeprefix("rule:") for x in sf.get("sources") or [] if x.startswith("rule:")]
    targets = [x.split("=", 1)[1] for x in srcs if x.startswith("target=")]
    actions = [x.split("=", 1)[1] for x in srcs if x.startswith("action=")]
    parts = [{"hw-code": "장비 제어 코드 변경", "irreversible": "되돌리기 어려운 작업·중요 코드 변경"}.get(
        (rm.get("policy") or {}).get("approval"), "사람 승인 필요")]
    if targets:
        parts.append(f"대상 {', '.join(dict.fromkeys(targets))}" + (f" + 동작 {', '.join(dict.fromkeys(actions))}" if actions else ""))
    if sf.get("needs_human"):
        parts.append(f"Jev 사람 승인 필요 {_pct(sf['needs_human'])}")
    return " · ".join(parts)


def _task_row(t: Task) -> dict[str, Any]:
    c, s, f = _ts(t.created_at), _ts(t.started_at), _ts(t.finished_at)
    rm = t.route_meta or {}
    return {**t.summary(), "started_at": t.started_at, "error": t.error,
            "wait_s": round(s - c, 1) if c and s else None, "total_s": round(f - c, 1) if c and f else None,
            "reason": rm.get("reason"), "jev": (rm.get("jev") or {}).get("probs"),
            "safety": rm.get("safety")}


_NATIVE_TITLES: dict[str, str | None] = {}


def _native_name(native_id: str | None) -> str:
    """등록되지 않은 Claude 세션은 rename 이름으로 보여 준다 (대화 파일이 커서 한 번 찾은 이름은 기억)."""
    if not native_id:
        return "?"
    if native_id not in _NATIVE_TITLES:
        try:
            from . import native
            ns = native.find(native_id, "claude")
            _NATIVE_TITLES[native_id] = ns.title if ns else None
        except OSError:
            _NATIVE_TITLES[native_id] = None
    return _NATIVE_TITLES[native_id] or native_id[:8]


def _rest(body: str | None, first: str | None) -> str:
    """본문 미리보기: 제목(첫 줄)과 같은 앞부분은 빼고 그다음 내용부터."""
    text = " ".join((body or "").split())
    head = " ".join((first or "").split())
    if head and text.startswith(head):
        text = text[len(head):].lstrip(" -—:·")
    return text[:400]


# 상태 이름이 기록마다 달라 네 묶음으로 맞춘다 (작업 · 직접 전달 · Claude 관찰)
STATUS_GROUPS: dict[str, tuple[list[str], list[str], list[str]]] = {
    "active": (["queued", "running", "delivered", "held", "needs_routing"], ["queued"], []),
    "done": (["done"], ["delivered"], ["sent"]),
    "failed": (["failed", "timeout"], ["expired"], ["not_delivered"]),
    "cancelled": (["cancelled"], ["cancelled", "dropped"], []),
}
KINDS = ("broker", "herdr-direct", "claude-native")


def search_flows(b: Any, *, q: str | None = None, kind: str | None = None, session: str | None = None,
                 status: str | None = None, since_hours: float | None = None, teammates: bool = True,
                 limit: int = 100) -> list[dict[str, Any]]:
    """세 경로(broker 중계 · Codex 직접 전달 · Claude 직접 메시지 관찰)를 같은 조건으로 서버에서 검색한다.
    q: 보낸 쪽·받는 쪽·제목·본문·결과·ID, status: active|done|failed|cancelled, teammates=False 면
    세션 안 에이전트 팀원에게 보낸 Claude 메시지를 뺀다."""
    q = (q or "").strip() or None
    since = ((datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat(timespec="seconds")
             if since_hours else None)
    st = STATUS_GROUPS.get(status or "")
    rows: list[dict[str, Any]] = []
    if kind in (None, "broker"):
        for t in b.store.search_tasks(q=q, session=session, statuses=st[0] if st else None, since=since, limit=limit):
            rows.append({"id": t.id, "task_id": t.id, "at": t.created_at, "transport": "broker", "from": t.from_addr,
                         "to": t.to_addr, "status": t.status.value, "summary": t.title,
                         "excerpt": _rest(t.body, t.title), "kind": t.kind.value, "teammate": False})
    if kind in (None, "herdr-direct"):
        for d in b.store.search_direct(q=q, session=session, statuses=st[1] if st else None, since=since, limit=limit):
            req = d["request"]
            first = req.get("title") or (req.get("body") or "").partition("\n")[0][:120]
            rows.append({"id": d["id"], "task_id": d["id"], "at": d["created_at"], "transport": "herdr-direct",
                         "from": d["from_name"], "to": d["to_name"], "status": d["status"], "summary": first,
                         "excerpt": _rest(req.get("body"), first), "kind": req.get("kind"), "teammate": False})
    if kind in (None, "claude-native"):
        for f in b.store.search_flow_log(q=q, session=session, statuses=st[2] if st else None, since=since,
                                         teammates=teammates, limit=limit):
            body = f["summary"] or ""
            first = body.partition("\n")[0][:120]
            rows.append({"id": f"f_{f['id']}", "task_id": f"f_{f['id']}", "at": f["at"], "transport": f["transport"],
                         "from": f["from_name"] or _native_name(f["from_native"]), "to": f["to_name"] or f["to_raw"],
                         "status": f["status"], "summary": first, "excerpt": _rest(body, first),
                         "kind": "message", "teammate": not f["to_name"]})
    rows.sort(key=lambda x: x["at"], reverse=True)
    return rows[:limit]


def snapshot(b: Any, *, task_limit: int = 50, flow_limit: int = 100) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    home = b.cfg.home
    avail = b.herdr.available()
    agents = b.herdr.agents() if avail else []
    by_sid = {(a.get("agent_session") or {}).get("value"): a for a in agents}

    sessions, attention = [], []
    for s in b.store.list_sessions():
        a = by_sid.get(s.native_id) if s.native_id else None
        if s.mode.value != "interactive":
            state = "worker"
        elif not avail:
            state = "unknown"
        elif not a:
            state = "closed"
        elif not herdr_mod.same_pane(a, s.provider, s.cwd):
            state = "mismatch"
        else:
            state = "open"
        c = b.store.status_counts(s.name)
        row = {"name": s.name, "provider": s.provider, "mode": s.mode.value, "roles": s.roles, "aliases": s.aliases,
               "cwd": s.cwd, "pane_state": state, "pane_id": a["pane_id"] if a else None,
               "agent_status": a.get("agent_status") if a else None,
               **{k: c.get(k, 0) for k in ACTIVE}}
        row["queued"] += b.store.direct_queued_count(s.name)   # 같은 provider 직접 전달 대기분 포함
        sessions.append(row)
        if state == "closed" and row["queued"]:
            attention.append({"kind": "closed-pane", "session": s.name,
                              "title": f"{s.name} 창이 닫혀 있어 메시지 {row['queued']}건이 대기 중",
                              "detail": "창을 열면(resume) 데몬이 넣습니다."})
        if state == "mismatch":
            attention.append({"kind": "mismatch", "session": s.name, "title": f"{s.name}: herdr 창 짝 불일치",
                              "detail": f"herdr 가 창 {row['pane_id']} 을 이 세션으로 보지만 종류·폴더가 다릅니다. 창을 다시 여세요."})

    for st, kind in (("held", "approve"), ("needs_routing", "route")):
        for t in b.store.list_tasks(status=st, limit=50):
            attention.append({"kind": kind, "task_id": t.id, "title": t.title, "from": t.from_addr, "to": t.to_addr,
                              "age_s": int(now.timestamp() - (_ts(t.created_at) or 0)),
                              "detail": _hold_reason(t) if kind == "approve" else (t.route_meta or {}).get("suggestion")})
    day_ago = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    for st in ("failed", "timeout"):
        for t in b.store.list_tasks(status=st, limit=20):
            if (t.finished_at or t.created_at) >= day_ago:
                attention.append({"kind": "failed", "task_id": t.id, "title": t.title, "from": t.from_addr,
                                  "to": t.to_addr, "detail": (t.error or "")[:200]})

    daemon = daemon_state(home)
    if not daemon["running"]:
        attention.insert(0, {"kind": "engine", "title": "데몬이 실행 중이 아닙니다",
                             "detail": "herdr 창 하나에서 `srhbroker daemon` 을 띄우세요."})
    elif not daemon.get("legacy") and not daemon.get("herdr"):
        attention.insert(0, {"kind": "engine", "title": "데몬이 herdr 창 밖에서 실행 중",
                             "detail": "interactive 세션에 바로 넣지 못합니다. herdr 창에서 다시 띄우세요."})
    elif daemon.get("restart_needed") or daemon.get("legacy"):
        attention.insert(0, {"kind": "engine", "title": "데몬 재시작 필요",
                             "detail": "데몬 시작 뒤 코드가 바뀌었습니다. Ctrl+C 후 `srhbroker daemon`."})

    tasks = [_task_row(t) for t in b.store.list_tasks(limit=task_limit)]

    flows = search_flows(b, limit=flow_limit, teammates=False)   # 세션 안 팀원 메시지는 기본에서 뺀다

    recent = search_flows(b, since_hours=24, limit=100000, teammates=False)
    waits = [t["wait_s"] for t in tasks if t["wait_s"] is not None and t["created_at"] >= day_ago]
    since10 = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
    stats = {"flows_24h": {k: sum(1 for f in recent if f["transport"] == k)
                           for k in ("broker", "herdr-direct", "claude-native")},
             "teammate_msgs_24h": len(b.store.search_flow_log(since=day_ago, limit=100000))
                                  - sum(1 for f in recent if f["transport"] == "claude-native"),
             "median_delivery_s_24h": round(statistics.median(waits), 1) if waits else None,
             "pushes_10min": {s["name"]: n for s in sessions if (n := b.store.push_count_since(s["name"], since10))}}

    err = home / "hook-error.log"
    errors = err.read_text(encoding="utf-8", errors="replace")[-2000:] if err.exists() else ""
    from .updater import cached
    return {"generated_at": now.isoformat(timespec="seconds"), "home": str(home), "daemon": daemon, "herdr": avail,
            "update": cached(home),
            "sessions": sessions, "attention": attention, "tasks": tasks, "flows": flows, "stats": stats,
            "hook_errors": errors}


def _iso_shift(iso: str, seconds: float) -> str:
    return (datetime.fromisoformat(iso) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _pct(v: Any) -> str:
    return f"{float(v) * 100:.0f}%" if isinstance(v, (int, float)) else "-"


def relay_timeline(b: Any, t: Task) -> list[dict[str, Any]]:
    """broker 중계 작업의 경과: 보냄 → 라우팅·Jev·안전 판단 → (승인) → 받는 창에 들어감 → 회신 → 보낸 창에 알림.
    각 항목: at(ISO), step(짧은 이름), text(설명), tone(ok|wait|bad|info)."""
    ev: list[dict[str, Any]] = []
    rm = t.route_meta or {}
    rcv = b.store.get_session(t.to_addr) if t.to_addr else None
    snd = b.store.get_session(t.from_addr)
    kind = {"task": "작업 (회신 필요)", "message": "메시지 (회신 없음)", "reply": "회신 알림"}.get(t.kind.value, t.kind.value)
    ev.append({"at": t.created_at, "step": "보냄", "tone": "info",
               "text": f"{t.from_addr} → {t.to_hint or t.to_addr or '(대상 생략)'} · {kind}"})
    logs = [r for r in sorted(b.store.route_log(500), key=lambda x: x["id"]) if r["task_id"] == t.id]
    approved = next((r for r in logs if r["router"] == "approve"), None)
    for r in logs:
        meta = r.get("meta") or {}
        if r["router"] == "jev":
            probs = meta.get("probs") or {}
            top = max(probs.items(), key=lambda kv: kv[1]) if probs else None
            text = ("Jev 판단: " + (f"{top[0]} {_pct(top[1])}" if top else "역할 선택 없음 (이름 지정)")
                    + f" · 하드웨어 위험 {_pct(meta.get('hw_risk'))} · 사람 승인 필요 {_pct(meta.get('needs_human'))}"
                    + ("" if r["applied"] else " · 참고만 (적용 안 함)") + (f" · 오류 {meta['error']}" if meta.get("error") else ""))
            ev.append({"at": r["created_at"], "step": "Jev", "tone": "info", "text": text})
        elif r["router"] == "approve":
            perm = meta.get("sandbox")
            ev.append({"at": r["created_at"], "step": "승인", "tone": "ok",
                       "text": f"{ {'mcp': '세션 대화창', 'cli': '터미널', 'dashboard': '대시보드', 'watch': 'watch 화면'}.get(meta.get('via'), meta.get('via', '?')) } 에서 승인"
                               + (f" — “{meta['confirmation']}”" if meta.get("confirmation") else "")
                               + (f" · 권한 {perm}" if perm else "")})
        elif r["router"] == "cancel":
            who = meta.get("by")
            ev.append({"at": r["created_at"], "step": "취소", "tone": "bad",
                       "text": ("사람(터미널·대시보드)이" if who == "user" else f"{who} 가" if who and who != "?" else "")
                               + f" 취소 ({ {'queued': '넣기 전', 'delivered': '받는 쪽이 처리 중', 'held': '승인 대기 중', 'needs_routing': '대상 미정 상태', 'running': '실행 중'}.get(meta.get('was'), meta.get('was', '')) })"})
        elif r["router"] == "drop":
            ev.append({"at": r["created_at"], "step": "소멸", "tone": "bad",
                       "text": f"{r['decision']} 창이 닫혀 있어 알림을 기다리지 않고 버림 (알림은 대기열에 두지 않음)"})
        elif r["router"] == "user":
            ev.append({"at": r["created_at"], "step": "대상 지정", "tone": "ok", "text": f"사람이 대상을 {r['decision']} 로 지정"})
        else:
            how = {"name": "이름 일치", "alias": "별칭 일치", "role": "역할", "rules": "규칙", "jev": "Jev 판단",
                   "only": "유일한 후보", "none": "대상 못 정함"}.get(r["router"], r["router"])
            ev.append({"at": r["created_at"], "step": "라우팅", "tone": "wait" if not r["decision"] else "info",
                       "text": f"{how} → {r['decision'] or '대상 미정 (사람이 지정해야 함)'}"
                               + (f" · {meta['reason']}" if meta.get("reason") and meta.get("reason") != f"{r['router']} 일치" else "")})
    sf = rm.get("safety") or {}
    srcs = [x.removeprefix("rule:") for x in sf.get("sources") or [] if x.startswith("rule:")]
    pick = lambda key: [x.split("=", 1)[1] for x in srcs if x.startswith(key + "=")]
    targets, actions, domain = pick("target"), pick("action"), pick("domain")
    legacy = [x for x in srcs if "=" not in x]                       # 이전 방식(단어 하나) 규칙
    forced = float(sf.get("hw_risk") or 0) >= float(b.router.jcfg.get("hw_risk_threshold", 0.2))
    held = approved is not None or t.status.value == "held"
    pol = rm.get("policy") or {}
    skip = {"user": "사용자가 직접 지시한 작업 — 승인 생략" + (f": “{pol['user_request']}”" if pol.get("user_request") else ""),
            "notice": "알림·회신이라 승인 대상 아님", "read-only": "읽기 전용 요청이라 승인 대상 아님",
            "hw-read-only": "장비 관련이지만 변경을 실행하게 하는 요청은 아님 — 승인 없이 읽기 전용으로 전달"}.get(pol.get("approval"))
    if skip and (srcs or forced or float(sf.get("needs_human") or 0) >= 0.3):
        ev.append({"at": t.created_at, "step": "승인 판단", "tone": "ok", "text": skip})
    if srcs or held or forced:
        when = next((r["created_at"] for r in logs if r["router"] == "jev"), t.created_at)
        parts = []
        lowered = "→ 사용자 직접 지시라 요청한 권한 유지" if pol.get("approval") == "user" else "→ 읽기 전용으로 낮춤"
        if targets and actions:
            parts.append(f"하드웨어 대상({', '.join(targets)}) + 제어 동작({', '.join(actions)}) {lowered}")
        elif legacy:
            parts.append(f"하드웨어 경계면 규칙(" + ", ".join(legacy) + ")에 걸려 읽기 전용으로 낮춤")
        elif forced:
            parts.append(f"Jev 하드웨어 위험 판단 {lowered}")
        elif domain:
            parts.append(f"영상·장비 용어({', '.join(domain)})만 있어 위험을 소폭 반영 (읽기 전용 아님)")
        if held:
            parts.append("장비 제어 코드 변경 → 승인 대기로 보류" if pol.get("approval") == "hw-code"
                         else f"사람 승인 필요 {_pct(sf.get('needs_human'))} (되돌리기 어려운 작업·중요 코드 변경) → 승인 대기로 보류")
        ev.append({"at": when, "step": "안전 판단", "tone": "wait", "text": " · ".join(parts)})
    if t.started_at:
        pushed = b.store.push_times(t.to_addr, _iso_shift(t.started_at, -3), _iso_shift(t.started_at, 3)) if t.to_addr else []
        if rcv and rcv.mode.value == "worker":
            how, step = "데몬이 worker 로 실행 시작", "실행"
        elif pushed:
            how, step = "herdr 로 받는 창에 바로 넣음", "전달"
        else:
            how, step = "받는 쪽이 응답을 마칠 때(Stop hook)나 inbox 로 가져감", "전달"
        ev.append({"at": t.started_at, "step": step, "tone": "ok", "text": f"{t.to_addr} — {how}"})
    elif t.status.value == "queued" and rcv and rcv.mode.value == "interactive":
        ev.append({"at": None, "step": "대기", "tone": "wait", "text": f"{t.to_addr} 창에 아직 넣지 못함 (창이 닫혀 있거나 승인 창)"})
    if t.finished_at and not (t.kind.value != "task" and t.status.value == "done"):   # message·reply 는 넣으면 끝
        label = {"done": ("회신", "ok", "완료로 회신"), "failed": ("회신", "bad", "실패로 회신"),
                 "timeout": ("만료", "bad", "기한 초과"), "cancelled": ("취소", "bad", "취소됨")}.get(
            t.status.value, (t.status.value, "info", t.status.value))
        if t.status.value != "cancelled" or not any(r["router"] in ("cancel", "drop") for r in logs):   # 취소·소멸은 위에서 보여 줌
            ev.append({"at": t.finished_at, "step": label[0], "tone": label[1],
                       "text": label[2] + (f" — {t.error}" if t.error and t.error != "취소됨" and t.status.value != "done" else "")})
        if snd and snd.mode.value == "interactive":
            back = b.store.push_times(t.from_addr, _iso_shift(t.finished_at, -3), _iso_shift(t.finished_at, 120))
            if back:
                ev.append({"at": back[0], "step": "알림", "tone": "ok", "text": f"결과를 보낸 창({t.from_addr})에 바로 넣음"})
    base = datetime.fromisoformat(t.created_at)
    for e in ev:
        e["after_s"] = round((datetime.fromisoformat(e["at"]) - base).total_seconds()) if e["at"] else None
    order = {"보냄": 0, "라우팅": 1, "Jev": 2, "안전 판단": 3}
    return sorted(ev, key=lambda e: (e["at"] is None, e["at"] or "", order.get(e["step"], 9)))


def direct_timeline(d: dict[str, Any]) -> list[dict[str, Any]]:
    ev = [{"at": d["created_at"], "step": "보냄", "tone": "info", "after_s": 0,
           "text": f"{d['from_name']} → {d['to_name']} · 같은 provider 직접 전달 (broker 작업 아님)"}]
    if d["status"] == "delivered" and d["delivered_at"]:
        ev.append({"at": d["delivered_at"], "step": "전달", "tone": "ok", "text": "herdr 로 받는 창에 넣음",
                   "after_s": round((datetime.fromisoformat(d["delivered_at"]) - datetime.fromisoformat(d["created_at"])).total_seconds())})
    elif d["status"] == "queued":
        ev.append({"at": None, "step": "대기", "tone": "wait", "after_s": None, "text": "받는 창이 열리거나 풀리면 넣음"})
    else:
        ev.append({"at": None, "step": {"cancelled": "취소", "expired": "만료", "dropped": "소멸"}.get(d["status"], d["status"]),
                   "tone": "bad", "after_s": None,
                   "text": "받는 창이 닫혀 있어 알림을 버림" if d["status"] == "dropped" else "넣지 않고 끝남"})
    return ev


def task_detail(b: Any, task_id: str) -> dict[str, Any]:
    if task_id.startswith("f_"):
        f = b.store.get_flow(int(task_id[2:])) if task_id[2:].isdigit() else None
        if not f:
            raise BrokerError(f"관찰 기록 {task_id} 이 없습니다")
        body = f["summary"] or ""
        frm = f["from_name"] or _native_name(f["from_native"])
        to = f["to_name"] or f["to_raw"]
        n = int(b.cfg.data.get("monitor", {}).get("store_body_chars", 2000))
        return {"task_id": task_id, "kind": "native", "from": frm, "to": to, "title": body.partition("\n")[0][:80],
                "status": f["status"], "routed_by": "claude-native", "created_at": f["at"], "finished_at": None,
                "body": body or "(본문을 저장하지 않았습니다 — [monitor] store_body_chars)", "acceptance": [],
                "result": None, "error": None, "jev": None, "safety": None, "terminal": True, "route_log": [],
                "note": (f"Claude 자체 SendMessage 를 관찰한 기록입니다 (broker 가 전달하지 않음). 본문은 앞 {n}자까지만 저장되며, "
                         "이전 기록은 200자까지만 남아 있습니다."),
                "teammate": not f["to_name"],
                "timeline": [{"at": f["at"], "step": "보냄", "tone": "info", "after_s": 0,
                              "text": f"{frm} → {f['to_raw']} · Claude SendMessage"
                                      + ("" if f["to_name"] else " (세션 안 에이전트 팀원 또는 미등록 대상)")}],
                "from_session": _sess(b, f["from_name"]), "to_session": _sess(b, f["to_name"])}
    if task_id.startswith("d_"):
        d = b.store.get_direct(task_id)
        if not d:
            raise BrokerError(f"직접 전달 {task_id} 이 없습니다")
        req = d["request"]
        c, f = _ts(d["created_at"]), _ts(d["delivered_at"])
        return {"task_id": d["id"], "kind": "direct", "from": d["from_name"], "to": d["to_name"],
                "title": req.get("title") or (req.get("body") or "")[:80], "status": d["status"],
                "routed_by": "direct", "reason": "같은 provider 직접 전달 (broker 작업 아님)",
                "created_at": d["created_at"], "finished_at": d["delivered_at"], "body": req.get("body"),
                "acceptance": req.get("acceptance"), "wait_s": round(f - c, 1) if c and f else None,
                "total_s": None, "result": None, "error": None, "jev": None, "safety": None,
                "terminal": d["status"] != "queued", "route_log": [], "timeline": direct_timeline(d)}
    t = b.store.require_task(task_id)
    return {**_task_row(t), "body": t.body, "result": t.result, "acceptance": t.acceptance,
            "route_meta": t.route_meta, "finished_at": t.finished_at,
            "terminal": TaskStatus(t.status).terminal,
            "route_log": [r for r in b.store.route_log(200) if r["task_id"] == task_id],
            "timeline": relay_timeline(b, t),
            "from_session": _sess(b, t.from_addr), "to_session": _sess(b, t.to_addr)}


def _sess(b: Any, name: str | None) -> dict[str, Any] | None:
    s = b.store.get_session(name) if name else None
    return {"name": s.name, "provider": s.provider, "mode": s.mode.value, "cwd": s.cwd} if s else None
