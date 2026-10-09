from __future__ import annotations

import os

from srhbroker import config


def test_load_dotenv_fills_missing_only(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".env").write_text(
        '# 주석\nTYPESAFE_API_KEY="cwd-key"\nexport SRHBROKER_X=1\nEMPTY=\nKEEP=from-file\n', encoding="utf-8")
    (home / ".env").write_text("TYPESAFE_API_KEY=home-key\nSRHBROKER_Y=2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "_project_root", lambda: None)
    monkeypatch.setenv("SRHBROKER_HOME", str(home))
    monkeypatch.setenv("KEEP", "from-env")
    for k in ("TYPESAFE_API_KEY", "SRHBROKER_X", "SRHBROKER_Y", "EMPTY"):
        monkeypatch.delenv(k, raising=False)

    loaded = config.load_dotenv()

    assert loaded == [(tmp_path / ".env").resolve(), (home / ".env").resolve()]
    assert os.environ["TYPESAFE_API_KEY"] == "cwd-key"  # 먼저 찾은 파일이 이긴다
    assert os.environ["SRHBROKER_X"] == "1"
    assert os.environ["SRHBROKER_Y"] == "2"
    assert os.environ["KEEP"] == "from-env"  # 기존 환경 변수가 우선
    assert "EMPTY" not in os.environ


def test_load_dotenv_no_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "_project_root", lambda: None)
    monkeypatch.setenv("SRHBROKER_HOME", str(tmp_path / "none"))
    assert config.load_dotenv() == []
