# srh-session-broker (srhbroker)

Claude Code ↔ Codex 세션 간 작업 위임 브로커 (Python 3.11+, MCP SDK 2.x, SQLite, typesafe-sdk).

- 응답·주석은 한국어
- 구조: models(도메인) · native(Claude/Codex 세션 기록 조회) → store(SQLite, CAS 전이) → router(G1~G3) → service(Broker, G4) → dispatcher(G5)/mcp_server/cli
- provider 차이는 adapters/ 에서만 흡수한다. CLI 플래그는 config.DEFAULTS 템플릿으로 두고 코드에 박지 않는다
- 세션 간 broker 중계는 Claude ↔ Codex만 허용한다. 같은 provider는 기본 통신 도구로 직접 전달하며, `native_required`는 미전달 인계 응답이다. broker 작업·Jev·herdr로 우회하지 않는다
- 외부(Jev)로 나가는 데이터는 router._state 한 곳에서만 만든다
- 검증: `python -m pytest -q` (어댑터 테스트는 POSIX 전용 가짜 실행 파일 사용)
