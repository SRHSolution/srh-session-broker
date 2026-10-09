from __future__ import annotations

from typing import Any

import pytest

from srhbroker.config import load_config
from srhbroker.router import JevVerdict, build_router
from srhbroker.service import Broker
from srhbroker.store import Store

ROLES = [
    {"name": "planner", "description": "설계·작업 분해", "preferred_provider": "claude"},
    {"name": "builder", "description": "구현·빌드·테스트", "preferred_provider": "codex"},
    {"name": "reviewer", "description": "코드 리뷰, 읽기 전용", "preferred_provider": "claude"},
]


class FakeJudge:
    """Jev 대역. route 확률·안전 확률을 고정값으로 돌려주고 호출 기록을 남긴다."""

    def __init__(self, probs: dict[str, float] | None = None, hw: float = 0.0, human: float = 0.0,
                 error: Exception | None = None):
        self.probs = probs or {}
        self.hw = hw
        self.human = human
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def judge(self, state, options, *, ask_route, ask_safety):
        self.calls.append({"state": state, "options": dict(options), "ask_route": ask_route, "ask_safety": ask_safety})
        if self.error:
            raise self.error
        probs = {k: self.probs.get(k, 0.0) for k in options} if ask_route else {}
        return JevVerdict(route_probs=probs, hw_risk=self.hw if ask_safety else None,
                          needs_human=self.human if ask_safety else None)


def make_broker(tmp_path, *, mode: str = "jev", judge: Any = None, rules: list | None = None,
                roles: list | None = None, **broker_over) -> Broker:
    over = {"roles": ROLES if roles is None else roles,
            "router": {"mode": mode, "rules": rules or []},
            "broker": {"poll_interval_s": 0.05, **broker_over}}
    cfg = load_config(home=tmp_path, overrides=over)
    router = build_router(cfg.router, cfg.roles, judge=judge or FakeJudge())
    store = Store(cfg.db_path)
    _OPEN_STORES.append(store)
    return Broker(cfg, store=store, router=router)


_OPEN_STORES: list[Store] = []


@pytest.fixture(autouse=True)
def _no_update_network(monkeypatch):
    """새 버전 확인이 테스트 중에 GitHub 에 접속하지 않게 한다."""
    from srhbroker import updater
    monkeypatch.setattr(updater, "remote_versions", lambda repo, timeout=10.0: ["v0.2.0", "v0.2.1", "v0.3.0"])


@pytest.fixture(autouse=True)
def _close_stores():
    """테스트가 만든 SQLite 연결을 닫는다 (ResourceWarning 방지)."""
    yield
    while _OPEN_STORES:
        _OPEN_STORES.pop().close()


@pytest.fixture(autouse=True)
def _no_real_herdr(monkeypatch):
    """herdr 창 안에서 테스트를 돌려도 실제 herdr 를 건드리지 않는다."""
    monkeypatch.delenv("HERDR_ENV", raising=False)


@pytest.fixture
def broker(tmp_path):
    b = make_broker(tmp_path)
    b.register("planner", "claude", mode="interactive", roles=["planner"], aliases=["p"])
    b.register("builder", "codex", roles=["builder"], aliases=["b"])
    b.register("reviewer", "claude", roles=["reviewer"], aliases=["rev"], sandbox="read-only")
    return b
