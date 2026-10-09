"""설정 로드. %USERPROFILE%\\.srhbroker\\config.toml (SRHBROKER_HOME 으로 변경 가능)."""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def broker_home() -> Path:
    env = os.environ.get("SRHBROKER_HOME")
    return Path(env).expanduser() if env else Path.home() / ".srhbroker"


def _project_root() -> Path | None:
    # editable 설치(pip install -e)일 때만 저장소 루트를 돌려준다
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").exists() else None


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value:
            out[key] = value
    return out


def load_dotenv() -> list[Path]:
    """.env 를 읽어 os.environ 에 없는 값만 채운다. 이미 설정된 환경 변수가 항상 우선한다.

    MCP 서버·hook 은 다른 프로젝트 폴더에서 실행되므로 현재 폴더만 보면 안 된다.
    순서: 현재 폴더 → 저장소 루트(editable 설치) → 브로커 홈(SRHBROKER_HOME). 먼저 찾은 값이 이긴다.
    """
    loaded: list[Path] = []
    seen: set[Path] = set()
    for d in (Path.cwd(), _project_root(), None):
        d = broker_home() if d is None else d  # 앞선 .env 가 SRHBROKER_HOME 을 정할 수 있게 마지막에 계산
        path = (d / ".env").resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for k, v in _parse_dotenv(path).items():
            os.environ.setdefault(k, v)
        loaded.append(path)
    return loaded


# 어댑터 명령 템플릿. {placeholder} 는 어댑터가 치환한다.
# 프롬프트는 argv 가 아니라 stdin 으로 넘긴다 — Windows .cmd shim 의 인자 이스케이프 문제를 피하기 위함.
DEFAULTS: dict[str, Any] = {
    "broker": {
        "poll_interval_s": 1.0,
        "max_parallel": 4,        # 동시에 실행할 worker 세션 수 (주소별로는 항상 1)
        "max_hops": 4,            # 작업 안에서 다시 send 할 수 있는 깊이
        "max_tasks_per_root": 20, # 하나의 최초 작업에서 파생될 수 있는 작업 수
        "stale_after_s": 86400,   # held·needs_routing·queued 로 이보다 오래 머문 작업은 timeout 처리 (데몬)
        "default_deadline_s": 1800,
        "allow_mcp_approve": False,  # True 면 LLM 이 MCP 로 held 작업을 승인할 수 있다 (권장하지 않음)
    },
    "adapters": {
        "claude": {
            "command": "claude",
            # 검증: claude 2.x — -p 는 stdin 프롬프트, --session-id 로 새 세션 생성, --resume 으로 문맥 유지
            "new_args": ["-p", "--output-format", "json", "--session-id", "{native_id}",
                         "--permission-mode", "{permission_mode}", "--model", "{model}"],
            "resume_args": ["-p", "--output-format", "json", "--resume", "{native_id}",
                            "--permission-mode", "{permission_mode}", "--model", "{model}"],
            "permission_modes": {"read-only": "plan", "workspace-write": "acceptEdits"},
            "extra_args": [],
        },
        "codex": {
            "command": "codex",
            # 검증: codex-cli 0.160 — `exec resume` 에는 --sandbox 가 없어 -c sandbox_mode=... 로 지정한다.
            # 프롬프트 '-' 는 stdin. 값이 None 인 {placeholder} 는 앞 옵션과 함께 빠진다(예: 모델 미지정).
            "new_args": ["exec", "--json", "--skip-git-repo-check", "--sandbox", "{sandbox}",
                         "-m", "{model}", "-o", "{last_message_file}", "-"],
            "resume_args": ["exec", "resume", "--json", "--skip-git-repo-check", "-c", "sandbox_mode={sandbox}",
                            "-m", "{model}", "-o", "{last_message_file}", "{native_id}", "-"],
            "extra_args": [],
        },
    },
    # herdr 창 안에서 실행 중이면, 받는 세션이 쉬고 있을 때 받은 메시지를 프롬프트로 바로 넣는다
    # git 배포 업데이트: 저장소의 vX.Y.Z 태그로 새 버전을 확인한다 (srhbroker update)
    "update": {
        "repo": "https://github.com/SRHSolution/srh-session-broker",
        "check": True,          # 데몬·doctor 가 새 버전을 확인해 watch·대시보드에 알린다
        "check_hours": 12,      # 확인 간격 (그 사이에는 캐시만 읽음)
    },
    # 관찰 기록(flow_log)·대시보드
    "monitor": {
        "store_body_chars": 2000,  # Claude 직접 메시지 관찰 기록에 남길 본문 길이 (0 이면 본문을 남기지 않음)
        "dashboard_port": 8765,
    },
    "herdr": {
        "enabled": True,
        "command": None,           # None → HERDR_BIN_PATH → PATH 의 herdr
        "timeout_s": 15,
        "wait_timeout_s": 3600,    # 받는 창이 바쁠 때 쉬기를 기다리는 최대 시간
        "max_push_chars": 6000,    # 이보다 길면 본문을 줄이고 status(task_id) 로 전체를 보게 한다
        "max_push_per_10min": 30,  # 세션당 상한 — 세션끼리 서로 계속 깨우는 핑퐁 방지
        "push_while_working": True,  # 받는 창이 작업 중이어도 넣는다 (Claude: 턴 중 반영, Codex: steer)
        "drop_notices_when_closed": True,  # 받는 창이 닫혀 있으면 알림(message)은 기다리지 않고 소멸 (작업·회신은 대기)
        # 같은 provider 인데 기본 도구로 닿지 않는 provider: 독립 세션끼리 herdr 로 바로 넣는다 (작업 기록 없음)
        "direct_providers": ["codex"],
    },
    # 라우팅 단위. Jev 는 이 목록에서 고른다. 비어 있으면 세션 단위로 고른다.
    "roles": [],                   # [{name="reviewer", description="...", preferred_provider="claude"}]
    "router": {
        "mode": "shadow",          # rules | shadow | jev
        "rules": [],               # [{pattern="(?i)리뷰|review", to="reviewer"}]  to = 역할 또는 세션 이름
        # 하드웨어·안전 경계면 규칙 (대소문자 무시). '하드웨어 대상 + 제어 동작'이 함께 있을 때만 읽기 전용을 강제한다
        "hw_risk_patterns": [],    # (이전 방식) 여기 넣은 패턴은 단어 하나만으로 위험 1.0
        "hw_rules": {
            # 하드웨어 대상: 장비·제어 신호·제어 코드 경로
            "targets": [
                r"X-?ray\s*(발생기|튜브|tube|generator|source)", r"발생기", r"\bkVp?\b", r"\bmAs?\b",
                r"노출\s*(시간|조건|값)|\bexposure\b", r"충격파|shock\s*wave", r"\bPLC\b|PlcService",
                r"MC\s*Protocol", r"펄스|\bpulse", r"C-?arm", r"(?<!임)베드|환자\s*테이블|patient\s*table",
                r"모터|\bmotor\b|서보|\bservo", r"로봇\s*암|robot(ic)?\s*arm", r"시리얼|\bserial\b|\bCOM\d+\b|RS-?(232|485)",
                r"펌웨어|firmware", r"인터록|\binterlock\b", r"E-?stop|비상\s*정지",
                r"(디텍터|검출기|detector)\s*(SDK|게인|gain|설정|오프셋|offset|캘리브)", r"선량|\bdose\b",
            ],
            # 프로젝트 고유 대상(제어 코드 클래스명·장비 SDK 이름 등)은 config.toml 의 extra_targets 에 추가한다
            # (targets 를 통째로 바꾸지 않고 기본 목록에 더해진다). 예: extra_targets = ["MyPlcService", "\\bVendorSdk"]
            "extra_targets": [],
            # 제어 동작: 장비를 움직이거나, 설정을 바꾸거나, 제어 코드를 바꾸는 일
            "actions": [
                r"제어|구동|발사|조사|노출\s*시작|이동|레지스터|쓰기|\bwrite\b|설정\s*(변경|적용)|파라미터|변경|수정",
                r"적용|플래시|\bflash\b|재부팅|\breboot|캘리브레이션|calibrat|실기|장비에서|장비\s*(테스트|시험)",
                r"구현|고쳐|고치|\bfix|리팩터|refactor|패치|\bpatch\b|\bfire\b|\btrigger\b|\bmove\b|\bdrive\b|on/off|off\s*처리",
            ],
            # 영상 도메인 용어: 단독이면 소폭 가중만 (늘 쓰는 용어라 이것만으로 읽기 전용이 되지 않게)
            "domain_terms": [r"X-?ray|엑스선|투시|fluoro", r"디텍터|검출기|\bdetector\b", r"\bDICOM\b|\bPACS\b|방사선\s*영상"],
            "domain_weight": 0.15,
        },
        "hw_risk_sandbox": "read-only",
        "jev": {
            "model": None,
            "permutations": 2,       # 선택지 순서를 바꿔 묻고 평균 (순서 민감도 완화)
            "safety_on_explicit": True,  # 이름을 명시한 send 에도 안전 판단(G3)을 수행
            "min_confidence": 0.7,   # τ
            "min_margin": 0.2,       # δ (1위-2위 확률 차)
            "needs_human_threshold": 0.3,
            "hw_hold_threshold": 0.3,   # 장비 규칙에 걸린 쓰기 작업은 needs_human 이 이 값 이상일 때 승인 (검토 요청은 읽기 전용만)
            "hw_risk_threshold": 0.2,
            "max_body_chars": 1500,  # 외부 전송 최소화: 본문은 잘라서 보낸다
            "send_body": True,       # False 면 제목만 보낸다
            "timeout_s": 5.0,
        },
    },
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass(slots=True)
class Config:
    home: Path
    data: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))

    @property
    def db_path(self) -> Path:
        return self.home / "broker.db"

    @property
    def broker(self) -> dict[str, Any]:
        return self.data["broker"]

    @property
    def router(self) -> dict[str, Any]:
        return self.data["router"]

    @property
    def roles(self) -> list[dict[str, Any]]:
        return self.data.get("roles", [])

    def adapter(self, provider: str) -> dict[str, Any]:
        return self.data["adapters"][provider]


def load_config(home: Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    home = home or broker_home()
    data = copy.deepcopy(DEFAULTS)
    path = home / "config.toml"
    if path.exists():
        with path.open("rb") as f:
            data = _merge(data, tomllib.load(f))
    if overrides:
        data = _merge(data, overrides)
    return Config(home=home, data=data)


EXAMPLE_CONFIG = """\
# SRH Session Broker 설정 — 생략한 항목은 기본값을 쓴다.

[broker]
max_parallel = 4
max_hops = 4

[router]
# rules  : 규칙만 사용
# shadow : 규칙으로 결정하고 Jev 판단은 기록만 (권장 시작값)
# jev    : 규칙 우선, 규칙에 없으면 Jev 판단 (확신 부족 시 사용자에게 확인)
mode = "shadow"

# 역할: Jev 가 고르는 단위. 세션은 register 시 roles 로 소속을 밝힌다.
[[roles]]
name = "planner"
description = "요구사항 분석, 설계, 작업 분해. 코드를 직접 고치지 않는다."
preferred_provider = "claude"

[[roles]]
name = "builder"
description = "코드 구현, 버그 수정, 리팩터링, 빌드·테스트 실행."
preferred_provider = "codex"

[[roles]]
name = "reviewer"
description = "변경분 코드 리뷰, 하드웨어·안전 경계면 교차 검증. 읽기 전용."
preferred_provider = "claude"

# 규칙: Jev 보다 먼저 적용된다. to = 역할 또는 세션 이름.
[[router.rules]]
pattern = "(?i)리뷰|review|검토"
to = "reviewer"

[router.jev]
min_confidence = 0.7
min_margin = 0.2
send_body = true
max_body_chars = 1500
"""
