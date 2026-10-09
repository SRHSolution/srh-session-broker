"""새 PC 준비: setup --apply(백업·중복 없음), doctor(읽기 전용 점검), demo(예시 데이터, 실제 홈 보호)."""

from __future__ import annotations

import json
import tomllib

import pytest

from srhbroker import cli, onboard
from tests.conftest import make_broker


@pytest.fixture
def homes(tmp_path, monkeypatch):
    ch, xh = tmp_path / "claude", tmp_path / "codex"
    ch.mkdir()
    xh.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ch))
    monkeypatch.setenv("CODEX_HOME", str(xh))
    return ch, xh


def test_apply_claude_adds_hooks_once_and_keeps_existing(homes, monkeypatch):
    ch, _ = homes
    (ch / "settings.json").write_text(json.dumps({"theme": "dark", "hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "other-tool stop"}]}]}}), encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(onboard.shutil, "which", lambda n: f"/bin/{n}" if n in ("claude", "srhbroker") else None)
    out = onboard.apply_claude(run=lambda cmd: calls.append(cmd) or 0)
    exe = str(onboard.Path("/bin/srhbroker"))                       # PATH 에 있어도 절대 경로로 쓴다
    assert calls == [["claude", "mcp", "add", "--scope", "user", "srhbroker", "--", exe, "mcp"]]
    st = json.loads((ch / "settings.json").read_text(encoding="utf-8"))
    stop = [h["command"] for g in st["hooks"]["Stop"] for h in g["hooks"]]
    hook = f'"{exe}" hook claude-stop' if "\\" in exe else f"{exe} hook claude-stop"   # Windows 경로는 따옴표(bash)
    assert stop == ["other-tool stop", hook] and st["theme"] == "dark"
    assert st["hooks"]["PostToolUse"][0]["matcher"] == "SendMessage"
    assert list(ch.glob("settings.json.bak-*")) and any("저장" in x for x in out)
    # 두 번째 적용: 바뀌는 것 없음 (MCP 는 ~/.claude.json 에 등록된 것으로 본다)
    (ch / ".claude.json").write_text(json.dumps({"mcpServers": {"srhbroker": {}}}), encoding="utf-8")
    out2 = onboard.apply_claude(run=lambda cmd: calls.append(cmd) or 0)
    assert len(calls) == 1 and not any("저장" in x for x in out2)


def test_apply_codex_appends_server_and_tool_approvals_once(homes):
    _, xh = homes
    (xh / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    onboard.apply_codex()
    cfg = tomllib.loads((xh / "config.toml").read_text(encoding="utf-8"))
    srv = cfg["mcp_servers"]["srhbroker"]
    assert cfg["model"] == "gpt-5" and srv["args"] == ["mcp"] and "HERDR_ENV" in srv["env_vars"]
    assert all(srv["tools"][t]["approval_mode"] == "approve" for t in onboard.MCP_TOOLS)
    before = (xh / "config.toml").read_text(encoding="utf-8")
    assert "이미 있음" in onboard.apply_codex()[0]
    assert (xh / "config.toml").read_text(encoding="utf-8") == before


def test_doctor_reports_missing_setup(tmp_path, homes, monkeypatch, capsys):
    monkeypatch.setattr(onboard.shutil, "which", lambda n: f"/bin/{n}" if n in ("claude", "codex") else None)
    b = make_broker(tmp_path / "b")
    rows = {r["item"]: r for r in onboard.doctor(b)}
    assert rows["Claude: MCP 서버"]["level"] == "fail" and "setup claude --apply" in rows["Claude: MCP 서버"]["fix"]
    assert rows["Codex: MCP 서버"]["level"] == "fail"
    assert onboard.print_doctor(list(rows.values())) == 1
    assert "srhbroker setup codex --apply" in capsys.readouterr().out


def test_demo_seeds_separate_home_and_protects_real_home(tmp_path, monkeypatch, capsys):
    real = tmp_path / "real"
    monkeypatch.setenv("SRHBROKER_HOME", str(real))
    assert cli.main(["demo", "--home", str(real)]) == 2                  # 실제 홈에는 만들지 않는다
    demo = tmp_path / "demo"
    assert cli.main(["demo", "--home", str(demo)]) == 0
    out = capsys.readouterr().out
    assert "세션 6" in out and "SRHBROKER_HOME" in out
    monkeypatch.setenv("SRHBROKER_HOME", str(demo))
    assert cli.main(["tasks"]) == 0
    rows = capsys.readouterr().out
    for st in ("done", "delivered", "held", "needs_routing", "cancelled"):
        assert st in rows
    assert not (real / "broker.db").exists()
