"""어댑터: 가짜 claude/codex 실행 파일로 실제 프로세스 경로(stdin 전달·출력 해석·타임아웃 종료)를 검증."""

import json
import os
import stat
import sys
import textwrap

import pytest

from srhbroker.adapters.base import fill, run_process
from srhbroker.adapters.claude import ClaudeAdapter, parse_claude_output
from srhbroker.adapters.codex import CodexAdapter, parse_codex_events
from srhbroker.config import DEFAULTS
from srhbroker.models import Sandbox, Session, SessionMode, Task, TaskKind

def _exe(tmp_path, name, body):
    """가짜 실행 파일. Windows 에서는 실제 claude/codex(npm 전역 설치)처럼 .cmd shim 으로 감싼다
    — .cmd 실행, stdin 전달, taskkill /T 로 프로세스 트리 종료 경로까지 함께 검증된다."""
    if sys.platform == "win32":
        script = tmp_path / f"{name}.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        p = tmp_path / f"{name}.cmd"
        p.write_text(f'@set PYTHONIOENCODING=utf-8\r\n@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(p)
    p = tmp_path / name
    p.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _task():
    return Task(id="t_1", kind=TaskKind.TASK, from_addr="planner", to_addr="w", body="안녕")


def test_fill_drops_option_with_missing_value():
    assert fill(["-p", "--resume", "{native_id}", "--x", "{y}"], {"native_id": None, "y": "1"}) == ["-p", "--x", "1"]


def test_parse_claude_variants():
    assert parse_claude_output('{"type":"result","result":"a","session_id":"s"}')["result"] == "a"
    assert parse_claude_output('noise\n{"type":"result","result":"b"}')["result"] == "b"
    assert parse_claude_output('[{"type":"system"},{"type":"result","result":"c"}]')["result"] == "c"
    assert parse_claude_output("") is None


def test_parse_codex_events_variants():
    out = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "th-1"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "최종"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}}),
    ])
    ev = parse_codex_events(out)
    assert (ev["thread_id"], ev["last_message"], ev["error"]) == ("th-1", "최종", None)
    old = json.dumps({"id": "0", "msg": {"type": "session_configured", "session_id": "s-9"}})
    assert parse_codex_events(old)["thread_id"] == "s-9"


def test_codex_transient_error_is_not_failure():
    # 실제 codex-cli 0.160 출력: 재연결 중 error 이벤트 후 정상 완료
    out = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "th"}),
        json.dumps({"type": "error", "message": "Reconnecting... 2/5"}),
        json.dumps({"type": "item.completed", "item": {"type": "error", "message": "Falling back"}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ])
    ev = parse_codex_events(out)
    assert ev["error"] is None and ev["completed"] and ev["last_message"] == "ok"
    failed = json.dumps({"type": "turn.failed", "error": {"message": "quota"}})
    assert parse_codex_events(failed)["error"] == "quota"
    assert not parse_codex_events(json.dumps({"type": "error", "message": "x"}))["completed"]


def test_codex_argv_templates(tmp_path):
    ad = CodexAdapter({**DEFAULTS["adapters"]["codex"], "command": sys.executable})
    s = Session(name="w", provider="codex", mode=SessionMode.WORKER)
    a1 = ad.build_argv(s, Sandbox.READ_ONLY, "/tmp/o.txt")[1:]
    assert a1 == ["exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "-o", "/tmp/o.txt", "-"]
    s.native_id, s.model = "th-1", "gpt-5"
    a2 = ad.build_argv(s, Sandbox.WORKSPACE_WRITE, "/tmp/o.txt")[1:]
    assert a2 == ["exec", "resume", "--json", "--skip-git-repo-check", "-c", "sandbox_mode=workspace-write",
                  "-m", "gpt-5", "-o", "/tmp/o.txt", "th-1", "-"]


async def test_claude_adapter_new_then_resume(tmp_path):
    log = tmp_path / "argv.jsonl"
    exe = _exe(tmp_path, "claude", f"""
        import json, sys
        argv = sys.argv[1:]
        prompt = sys.stdin.read()
        open({str(log)!r}, "a").write(json.dumps(argv) + "\\n")
        sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else argv[argv.index("--resume") + 1]
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                           "result": "echo:" + prompt, "session_id": sid}}))
    """)
    cfg = {**DEFAULTS["adapters"]["claude"], "command": exe}
    ad = ClaudeAdapter(cfg)
    s = Session(name="w", provider="claude", mode=SessionMode.WORKER, cwd=str(tmp_path))
    r = await ad.deliver(s, _task(), "프롬프트 & \"특수문자\"", Sandbox.READ_ONLY, env={}, timeout_s=10,
                         is_cancelled=lambda: False)
    assert r.ok and r.text == 'echo:프롬프트 & "특수문자"' and r.native_id
    argv1 = json.loads(log.read_text().splitlines()[0])
    assert "--session-id" in argv1 and argv1[argv1.index("--permission-mode") + 1] == "plan"
    s.native_id = r.native_id
    r2 = await ad.deliver(s, _task(), "다음", Sandbox.WORKSPACE_WRITE, env={}, timeout_s=10, is_cancelled=lambda: False)
    argv2 = json.loads(log.read_text().splitlines()[1])
    assert r2.ok and "--resume" in argv2 and argv2[argv2.index("--resume") + 1] == r.native_id
    assert argv2[argv2.index("--permission-mode") + 1] == "acceptEdits"


async def test_claude_adapter_error_result(tmp_path):
    exe = _exe(tmp_path, "claude", """
        import json, sys
        sys.stdin.read()
        print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "session_id": "x"}))
    """)
    ad = ClaudeAdapter({**DEFAULTS["adapters"]["claude"], "command": exe})
    s = Session(name="w", provider="claude", mode=SessionMode.WORKER)
    r = await ad.deliver(s, _task(), "p", Sandbox.READ_ONLY, env={}, timeout_s=10, is_cancelled=lambda: False)
    assert not r.ok and "error_max_turns" in r.error


async def test_codex_adapter_reads_last_message_file(tmp_path):
    exe = _exe(tmp_path, "codex", """
        import json, sys
        argv = sys.argv[1:]
        prompt = sys.stdin.read()
        out = argv[argv.index("-o") + 1]
        open(out, "w", encoding="utf-8").write("마지막:" + prompt)
        tid = argv[-2] if "resume" in argv else "th-new"   # resume: ... <thread_id> -
        log = open(out + ".argv", "w"); log.write(json.dumps(argv)); log.close()
        print(json.dumps({"type": "thread.started", "thread_id": tid}))
        print(json.dumps({"type": "turn.completed", "usage": {}}))
    """)
    ad = CodexAdapter({**DEFAULTS["adapters"]["codex"], "command": exe})
    s = Session(name="w", provider="codex", mode=SessionMode.WORKER, cwd=str(tmp_path))
    r = await ad.deliver(s, _task(), "구현", Sandbox.WORKSPACE_WRITE, env={}, timeout_s=10, is_cancelled=lambda: False)
    assert r.ok and r.text == "마지막:구현" and r.native_id == "th-new"
    s.native_id = "th-new"
    r2 = await ad.deliver(s, _task(), "계속", Sandbox.READ_ONLY, env={}, timeout_s=10, is_cancelled=lambda: False)
    assert r2.ok and r2.native_id == "th-new"


async def test_run_process_timeout_kills_tree(tmp_path):
    exe = _exe(tmp_path, "slow", """
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        time.sleep(30)
    """)
    pr = await run_process([exe], stdin_text="", cwd=None, env={}, timeout_s=0.5, is_cancelled=lambda: False,
                           poll_s=0.1)
    assert pr.timed_out


async def test_env_passed_to_child(tmp_path):
    exe = _exe(tmp_path, "envp", """
        import os
        print(os.environ.get("SRHBROKER_SELF"))
    """)
    pr = await run_process([exe], stdin_text="", cwd=None, env={"SRHBROKER_SELF": "builder"}, timeout_s=5,
                           is_cancelled=lambda: False)
    assert pr.stdout.strip() == "builder"
