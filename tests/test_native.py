"""기존 Claude/Codex 세션 연결: rename 이름 → 브로커 이름, 세션 ID 로 자기 자신 식별."""

from __future__ import annotations

import io
import json

import pytest
from mcp import Client

from srhbroker import mcp_server, native
from srhbroker.hooks import claude_stop
from srhbroker.models import BrokerError, normalize_name
from tests.conftest import make_broker

C_ID = "aaaaaaaa-1111-2222-3333-444444444444"
C_ID2 = "bbbbbbbb-1111-2222-3333-444444444444"
X_ID = "01a0f785-ab89-7531-b545-e6e7c771fdfd"


def _claude_session(home, sid, cwd, *titles):
    d = home / "projects" / "D--proj"
    d.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "cwd": cwd, "sessionId": sid, "message": {"content": "안녕"}}]
    lines += [{"type": "custom-title", "customTitle": t, "sessionId": sid} for t in titles]
    path = d / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    return path


def _codex_session(home, sid, cwd, title=None):
    d = home / "sessions" / "2026" / "10" / "01"
    d.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}
    (d / f"rollout-2026-10-01T21-53-00-{sid}.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")
    if title:
        with (home / "session_index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": sid, "thread_name": title}, ensure_ascii=False) + "\n")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    ch, xh = tmp_path / "claude", tmp_path / "codex"
    ch.mkdir()
    xh.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ch))
    monkeypatch.setenv("CODEX_HOME", str(xh))
    monkeypatch.delenv("SRHBROKER_SELF", raising=False)
    monkeypatch.delenv("SRHBROKER_TASK", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return ch, xh


@pytest.fixture
def nb(tmp_path, homes):
    return make_broker(tmp_path / "broker")


@pytest.mark.parametrize("title, expected", [
    ("Project Lead", "project-lead"),
    ("Project-Lead", "project-lead"),
    ("Alpha-cam-codex", "alpha-cam-codex"),
    ("proto.reconstruct  v2", "proto-reconstruct-v2"),
    ("정리해줘 KiCad MCP 구조", None),      # Codex 자동 제목(한글)
    ("Codex", None),                         # 예약어
    ("", None),
    (None, None),
])
def test_normalize_name(title, expected):
    assert normalize_name(title) == expected


def test_find_reads_latest_rename(homes):
    ch, xh = homes
    _claude_session(ch, C_ID, r"D:\proj", "Project", "Project Lead")
    _codex_session(xh, X_ID, r"D:\proj2", "Alpha-cam-codex")
    c = native.find(C_ID)
    assert (c.provider, c.title, c.cwd) == ("claude", "Project Lead", r"D:\proj")
    x = native.find(X_ID, "codex")
    assert (x.provider, x.title, x.cwd) == ("codex", "Alpha-cam-codex", r"D:\proj2")
    assert native.find("nope") is None
    assert [s.id for s in native.recent(1)] and all(s.title for s in native.recent(1))


def test_register_native_uses_rename_title(nb, homes):
    ch, xh = homes
    _claude_session(ch, C_ID, r"D:\proj", "Project Lead")
    _codex_session(xh, X_ID, r"D:\proj2", "Alpha-cam-codex")
    s = nb.register_native(C_ID, roles=["planner"])
    assert (s.name, s.provider, s.mode.value, s.cwd, s.native_id) == ("project-lead", "claude", "interactive", r"D:\proj", C_ID)
    x = nb.register_native(X_ID, mode="worker", roles=["builder"])
    assert (x.name, x.provider) == ("alpha-cam-codex", "codex")
    # rename 이름 그대로(공백 포함)도 주소로 쓸 수 있다
    assert nb.store.resolve_name("Project Lead")[0].name == "project-lead"
    # 같은 세션 재등록 = 갱신
    assert nb.register_native(C_ID, roles=["reviewer"]).roles == ["reviewer"]


def test_register_native_errors(nb, homes):
    ch, xh = homes
    _codex_session(xh, X_ID, r"D:\p", "정리해줘 KiCad MCP 구조")
    with pytest.raises(BrokerError, match="직접 지정"):
        nb.register_native(X_ID)
    assert nb.register_native(X_ID, name="kicad").name == "kicad"
    with pytest.raises(BrokerError, match="찾을 수 없습니다"):
        nb.register_native("nope")
    # 같은 rename 이름의 두 세션이 모두 살아 있으면(herdr 밖: 최근 10분 안에 기록이 바뀜) 덮어쓰지 않고 사용자에게 묻게 한다
    _claude_session(ch, C_ID, r"D:\a", "alpha-cam")
    _claude_session(ch, C_ID2, r"D:\b", "alpha-cam")
    nb.register_native(C_ID)
    with pytest.raises(BrokerError, match="창이 지금 열려 있습니다.*임의의 이름을 만들지 말고"):
        nb.register_native(C_ID2)
    assert nb.register_native(C_ID2, name="alpha-cam-old").native_id == C_ID2
    # 이미 등록된 세션에 다른 이름을 주면 그 이름으로 옮긴다 (이전 이름은 별칭)
    s = nb.register_native(C_ID, name="other")
    assert (s.name, s.native_id, s.aliases) == ("other", C_ID, ["alpha-cam"])
    with pytest.raises(BrokerError, match="claude 세션이 쓰고 있습니다"):   # 다른 provider 가 쓰는 이름은 이어받지 않는다
        _codex_session(xh, X_ID, r"D:\p", "other")
        nb.register_native(X_ID, provider="codex")


class _Panes:
    """herdr 대역: 열려 있는 창(세션 ID)만 찾는다."""

    def __init__(self, *open_ids):
        self.open = set(open_ids)

    def available(self):
        return True

    def find(self, native_id):
        return {"pane_id": f"p-{native_id}", "agent_status": "idle"} if native_id in self.open else None


X_OLD = "01a10176-5d77-7511-82a3-8a09b17e7593"
X_NEW = "01a1205d-501a-7bd1-8d98-2d3a20d21c0c"


async def test_new_session_with_same_rename_takes_over_closed_name(nb, homes):
    """이전 세션(창 닫힘)이 쥐고 있던 rename 이름을, 같은 이름으로 만든 새 세션이 이어받는다. 대기 메시지도 새 세션이 받는다."""
    _, xh = homes
    _codex_session(xh, X_OLD, r"D:\proj", "project-lead-codex")
    nb.register_native(X_OLD, aliases=["umc"])
    nb.register("lead", "claude")
    r = await nb.send("캡처해 줘", sender="lead", to="project-lead-codex")
    _codex_session(xh, X_NEW, r"D:\proj", "project-lead-codex")
    nb.herdr = _Panes(X_NEW)                                  # 이전 창은 닫혔고 새 창만 열림
    notes: list[str] = []
    s = nb.register_native(X_NEW, notes=notes)
    assert (s.name, s.native_id, s.aliases) == ("project-lead-codex", X_NEW, ["umc"])
    assert "이어받았습니다" in notes[0]
    assert nb.identify(X_NEW).name == "project-lead-codex" and nb.identify(X_OLD) is None
    assert [t["task_id"] for t in nb.inbox("project-lead-codex", mark=False)["incoming"]] == [r["task_id"]]


def test_both_windows_open_is_refused(nb, homes):
    _, xh = homes
    _codex_session(xh, X_OLD, r"D:\proj", "project-lead-codex")
    _codex_session(xh, X_NEW, r"D:\proj", "project-lead-codex")
    nb.register_native(X_OLD)
    nb.herdr = _Panes(X_OLD, X_NEW)
    with pytest.raises(BrokerError, match="같은 이름의 창이 둘입니다"):
        nb.register_native(X_NEW)


async def test_reregister_moves_invented_name_to_rename_title(nb, homes):
    """실제로 겪은 경우: 이름이 막히자 임의 이름으로 등록된 세션 → register() 다시 호출하면 rename 이름으로 옮기고,
    닫힌 이전 세션의 이름을 이어받으며, 임의 이름 앞으로 쌓인 메시지·임의 이름(별칭)도 함께 정리된다."""
    _, xh = homes
    _codex_session(xh, X_OLD, r"D:\proj", "project-lead-codex")
    nb.register_native(X_OLD)
    nb.register("lead", "claude")
    to_old = await nb.send("이전 이름으로 온 요청", sender="lead", to="project-lead-codex")
    _codex_session(xh, X_NEW, r"D:\proj", "project-lead-codex")
    nb.register_native(X_NEW, name="myproj-codex-01a1205d")            # 임의 이름으로 등록된 상태
    to_tmp = await nb.send("임의 이름으로 온 요청", sender="lead", to="myproj-codex-01a1205d")
    nb.herdr = _Panes(X_NEW)
    notes: list[str] = []
    s = nb.register_native(X_NEW, notes=notes)
    assert (s.name, s.native_id) == ("project-lead-codex", X_NEW) and "myproj-codex-01a1205d" in s.aliases
    assert len(notes) == 2 and nb.store.get_session("myproj-codex-01a1205d") is None
    box = {t["task_id"] for t in nb.inbox("project-lead-codex", mark=False)["incoming"]}
    assert box == {to_old["task_id"], to_tmp["task_id"]}
    assert nb.store.resolve_name("myproj-codex-01a1205d")[0].name == "project-lead-codex"


def test_closed_session_detected_without_herdr_by_age(nb, homes):
    """herdr 밖이면 이전 세션 기록이 10분 넘게 그대로일 때 닫힌 것으로 보고 이어받는다."""
    import os
    import time
    _, xh = homes
    _codex_session(xh, X_OLD, r"D:\proj", "project-lead-codex")
    nb.register_native(X_OLD)
    old = next(xh.rglob(f"*{X_OLD}.jsonl"))
    t = time.time() - 3600
    os.utime(old, (t, t))
    _codex_session(xh, X_NEW, r"D:\proj", "project-lead-codex")
    assert nb.register_native(X_NEW).native_id == X_NEW


async def test_mcp_whoami_hints_rename_mismatch_and_register_fixes_it(mcp_nb):
    mcp_nb.register_native(X_ID, name="tmp-name")
    meta = {"threadId": X_ID}
    async with Client(mcp_server.mcp) as c:
        who = _data(await c.call_tool("whoami", {}, meta=meta))
        assert who["self"] == "tmp-name" and who["rename_title"] == "Alpha-cam-codex" and "register()" in who["hint"]
        reg = _data(await c.call_tool("register", {}, meta=meta))
        assert reg["name"] == "alpha-cam-codex" and "tmp-name" in reg["notes"][0]
        who = _data(await c.call_tool("whoami", {}, meta=meta))
        assert who["self"] == "alpha-cam-codex" and "hint" not in who


async def test_hook_identifies_by_session_id_and_syncs_rename(nb, homes):
    ch, _ = homes
    path = _claude_session(ch, C_ID, r"D:\proj", "Project Lead")
    nb.register_native(C_ID, roles=["planner"])
    nb.register("builder", "codex", roles=["builder"])
    r = await nb.send("확인 부탁", sender="builder", to="Project Lead")
    assert r["to"] == "project-lead"

    # SRHBROKER_SELF 없이 stdin session_id 만으로 자기 편지함을 받는다
    out = io.StringIO()
    claude_stop(nb, None, stdin=io.StringIO(json.dumps({"session_id": C_ID, "transcript_path": str(path)})), stdout=out)
    assert r["task_id"] in json.loads(out.getvalue())["reason"]

    # rename 하면 새 이름이 별칭으로 추가된다
    _claude_session(ch, C_ID, r"D:\proj", "Project Lead", "PL Lead")
    claude_stop(nb, None, stdin=io.StringIO(json.dumps({"session_id": C_ID})), stdout=io.StringIO())
    assert nb.store.get_session("project-lead").aliases == ["pl-lead"]
    assert nb.store.resolve_name("pl-lead")[0].name == "project-lead"


def test_hook_ignores_unregistered_session(nb, homes):
    ch, _ = homes
    _claude_session(ch, C_ID, r"D:\proj", "Somebody")
    out = io.StringIO()
    assert claude_stop(nb, None, stdin=io.StringIO(json.dumps({"session_id": C_ID})), stdout=out) == 0
    assert out.getvalue() == ""
    assert nb.store.list_sessions() == []


@pytest.fixture
def mcp_nb(nb, homes, monkeypatch):
    ch, xh = homes
    _claude_session(ch, C_ID, r"D:\proj", "Project Lead")
    _codex_session(xh, X_ID, r"D:\proj2", "Alpha-cam-codex")
    monkeypatch.setattr(mcp_server, "_broker", nb)
    return nb


def _data(res):
    assert not res.is_error, res.content[0].text
    return json.loads(res.content[0].text)


async def test_mcp_claude_identity_from_env(mcp_nb, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", C_ID)
    async with Client(mcp_server.mcp) as c:
        who = _data(await c.call_tool("whoami", {}))
        assert who["self"] is None and who["native_id"] == C_ID
        reg = _data(await c.call_tool("register", {"roles": ["planner"]}))   # 이름 생략 = 지금 이 세션
        assert (reg["name"], reg["provider"]) == ("project-lead", "claude")
        mcp_nb.register("builder", "codex", roles=["builder"])
        sent = _data(await c.call_tool("send", {"body": "구현해", "to": "builder"}))
        assert sent["from"] == "project-lead"
        who = _data(await c.call_tool("whoami", {}))
        assert (who["self"], who["identified_by"]) == ("project-lead", "claude:CLAUDE_CODE_SESSION_ID")


async def test_mcp_codex_identity_from_meta(mcp_nb):
    mcp_nb.register_native(X_ID, roles=["builder"])
    mcp_nb.register("planner", "claude", roles=["planner"])
    meta = {"threadId": X_ID, "x-codex-turn-metadata": {"thread_id": X_ID}}
    async with Client(mcp_server.mcp) as c:
        sent = _data(await c.call_tool("send", {"body": "검토해", "to": "planner"}, meta=meta))
        assert sent["from"] == "alpha-cam-codex"
        box = _data(await c.call_tool("inbox", {"mark": False}, meta=meta))
        assert box == {"incoming": [], "replies": []}


async def test_mcp_unregistered_session_gets_clear_error(mcp_nb, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", C_ID)
    async with Client(mcp_server.mcp) as c:
        res = await c.call_tool("send", {"body": "x", "to": "anyone"})
    assert res.is_error and "register" in res.content[0].text


def test_cli_register_session_with_name(tmp_path, homes, monkeypatch, capsys):
    from srhbroker import cli
    _, xh = homes
    _codex_session(xh, X_ID, r"D:\p", "정리해줘 KiCad MCP 구조")
    monkeypatch.setenv("SRHBROKER_HOME", str(tmp_path / "cli-home"))
    assert cli.main(["register", "--session", X_ID]) == 2          # 한글 자동 제목 → 이름 지정 필요
    assert "직접 지정" in capsys.readouterr().err
    assert cli.main(["register", "--session", X_ID, "--name", "kicad", "--mode", "worker"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["name"], out["provider"], out["mode"], out["cwd"]) == ("kicad", "codex", "worker", r"D:\p")


async def test_hook_reads_utf8_stdin_bytes(nb, homes):
    """Windows 에서 stdin 기본 인코딩(cp949)과 무관하게 UTF-8 JSON(한글 포함)을 읽는다."""
    ch, _ = homes
    _claude_session(ch, C_ID, r"D:\proj", "Project Lead")
    nb.register_native(C_ID)
    nb.register("builder", "codex")
    r = await nb.send("확인", sender="builder", to="project-lead")
    raw = json.dumps({"session_id": C_ID, "last_assistant_message": "안녕하세요 — 완료"}, ensure_ascii=False).encode("utf-8")
    stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="cp949", errors="strict")
    out = io.StringIO()
    claude_stop(nb, None, stdin=stdin, stdout=out, provider="codex")
    assert r["task_id"] in json.loads(out.getvalue())["reason"]


def test_hook_cli_never_fails(tmp_path, monkeypatch):
    """stdin 이 없거나 내부 오류가 나도 hook 은 0 으로 끝나고 hook-error.log 에 남긴다."""
    import sys
    from srhbroker import cli, hooks
    monkeypatch.setenv("SRHBROKER_HOME", str(tmp_path))
    monkeypatch.delenv("SRHBROKER_SELF", raising=False)
    monkeypatch.delenv("SRHBROKER_TASK", raising=False)
    monkeypatch.setattr(sys, "stdin", None)
    assert cli.main(["hook", "codex-stop"]) == 0
    monkeypatch.setattr(hooks, "claude_stop", lambda *a, **k: 1 / 0)
    assert cli.main(["hook", "claude-stop"]) == 0
    assert "ZeroDivisionError" in (tmp_path / "hook-error.log").read_text(encoding="utf-8")


# ── 등록 이름 바꾸기 (rename) ─────────────────────────────────────────────

async def test_rename_keeps_identity_and_moves_pending_refs(nb, homes):
    ch, _ = homes
    _claude_session(ch, C_ID, r"D:\proj", "alpha-cam")
    nb.register_native(C_ID, roles=["planner"], aliases=["ac"], description="설명", sandbox="read-only")
    nb.register("builder", "codex", roles=["builder"])
    to_me = await nb.send("확인해 줘", sender="builder", to="alpha-cam")
    mine = await nb.send("구현해 줘", sender="alpha-cam", to="builder")
    notes: list[str] = []
    s = nb.rename("alpha-cam-claude", old="alpha-cam", notes=notes)
    assert (s.name, s.native_id, s.roles, s.description, s.sandbox.value) == ("alpha-cam-claude", C_ID, ["planner"], "설명", "read-only")
    assert set(s.aliases) == {"ac", "alpha-cam"} and "옛 이름은 별칭" in notes[0]
    assert nb.store.get_session("alpha-cam") is None and nb.identify(C_ID).name == "alpha-cam-claude"
    assert [t["task_id"] for t in nb.inbox("alpha-cam-claude", mark=False)["incoming"]] == [to_me["task_id"]]
    nb.store.inbox("builder", mark=True)
    nb.reply(mine["task_id"], "끝", sender="builder")                    # 옛 이름으로 보낸 작업의 회신도 새 이름으로
    assert [t["task_id"] for t in nb.inbox("alpha-cam-claude", mark=False)["replies"]] == [mine["task_id"]]
    r = await nb.send("옛 이름으로", sender="builder", to="alpha-cam")    # 옛 이름(별칭)으로 보내도 닿는다
    assert r["to"] == "alpha-cam-claude"


def test_rename_conflicts(nb, homes):
    ch, xh = homes
    _claude_session(ch, C_ID, r"D:\a", "alpha-cam")
    _claude_session(ch, C_ID2, r"D:\b", "other-cam")
    nb.register_native(C_ID)
    nb.register_native(C_ID2)
    nb.herdr = _Panes(C_ID, C_ID2)
    with pytest.raises(BrokerError, match="창이 지금 열려"):              # 열려 있는 다른 세션의 이름
        nb.rename("other-cam", old="alpha-cam")
    nb.register("codex-x", "codex")
    with pytest.raises(BrokerError, match="codex 세션이 쓰고"):           # 다른 provider 이름
        nb.rename("codex-x", old="alpha-cam")
    nb.register("alpha-cam-claude", "claude", mode="interactive")       # 세션 ID 없는 실수 항목 → 정리하고 이어받음
    notes: list[str] = []
    assert nb.rename("alpha-cam-claude", old="alpha-cam", notes=notes).native_id == C_ID
    assert "정리하고 이어받았습니다" in notes[0]


async def test_rename_adopts_records_of_deleted_old_name(nb, homes):
    """실제로 겪은 경우: 옛 이름을 unregister 하고 새 이름으로 다시 등록 → 옛 이름 기록·미확인 회신을 이어받는다."""
    ch, _ = homes
    _claude_session(ch, C_ID, r"D:\proj", "alpha-cam")
    nb.register_native(C_ID)
    nb.register("builder", "codex")
    mine = await nb.send("구현해 줘", sender="alpha-cam", to="builder")
    nb.store.inbox("builder", mark=True)
    nb.reply(mine["task_id"], "끝", sender="builder")
    nb.store.remove_session("alpha-cam")
    nb.register_native(C_ID, name="alpha-cam-claude")
    assert nb.inbox("alpha-cam-claude", mark=False)["replies"] == []      # 끊긴 상태
    notes: list[str] = []
    s = nb.rename("alpha-cam-claude", old="alpha-cam", notes=notes)
    assert "alpha-cam" in s.aliases and "기록" in notes[0]
    assert [t["task_id"] for t in nb.inbox("alpha-cam-claude", mark=False)["replies"]] == [mine["task_id"]]


async def test_mcp_register_with_provider_links_own_session_and_rename_tool(mcp_nb, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", C_ID)
    mcp_nb.register("other", "codex")
    async with Client(mcp_server.mcp) as c:
        reg = _data(await c.call_tool("register", {"name": "project-lead", "provider": "claude"}))
        assert reg["has_native_id"] is True                              # 세션 ID 없는 별도 항목을 만들지 않는다
        out = _data(await c.call_tool("rename", {"new_name": "project-lead-claude"}))
        assert out["name"] == "project-lead-claude" and out["notes"]
        who = _data(await c.call_tool("whoami", {}))
        assert who["self"] == "project-lead-claude"
        res = await c.call_tool("rename", {"new_name": "x", "old_name": "other"})
        assert res.is_error and "다른 세션" in res.content[0].text
    assert [s.name for s in mcp_nb.store.list_sessions()] == ["other", "project-lead-claude"]
