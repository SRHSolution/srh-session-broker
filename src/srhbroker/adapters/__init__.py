"""provider 별 세션 실행 어댑터."""

from __future__ import annotations

from typing import Any

from .base import Adapter, TurnResult, envelope
from .claude import ClaudeAdapter
from .codex import CodexAdapter


def build_adapters(adapters_cfg: dict[str, Any]) -> dict[str, Adapter]:
    return {"claude": ClaudeAdapter(adapters_cfg["claude"]), "codex": CodexAdapter(adapters_cfg["codex"])}


__all__ = ["Adapter", "TurnResult", "envelope", "build_adapters", "ClaudeAdapter", "CodexAdapter"]
