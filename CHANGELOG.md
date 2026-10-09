# 변경 이력

버전은 [유의적 버전](https://semver.org/lang/ko/)을 따릅니다. 0.x 동안에는 기능이 늘면 두 번째 자리, 고치기만 하면 세 번째 자리를 올립니다.

## 0.4.2 — 2026-10-10

### 고침
- 웹 대시보드를 새로고침하면 "토큰이 필요합니다"로 막혀 매번 링크를 다시 눌러야 하던 문제. 출력된 주소로 한 번 열면 그 브라우저에 토큰 쿠키(HttpOnly · SameSite=Strict · 30일, 포트별 이름)를 남겨 새로고침·새 탭·북마크가 토큰 없이 열립니다. Host·Origin 검사는 그대로이고, `--new-token` 으로 바꾸면 쿠키도 무효가 됩니다. 토큰 없이 열면 여는 방법을 알려 주는 안내 페이지를 보여 줍니다.

## 0.4.1 — 2026-10-10

### 고침
- `update` 뒤 데몬 재시작: 종료 직후 셸이 프롬프트로 돌아오기 전에 넣은 `srhbroker daemon` 입력이 사라져 데몬이 꺼진 채 남던 문제. 잠시 기다렸다 넣고, 새 데몬이 실제로 떴는지 확인해 안 떴으면 한 번 더 시도하며, 실패하면 성공으로 보고하지 않습니다.

## 0.4.0 — 2026-10-10

### 추가
- **git 배포 업데이트** `srhbroker update [--check] [--to vX.Y.Z] [--stop-all] [--no-restart]`: 저장소의 vX.Y.Z 태그로 새 버전을 찾아 설치 방식(uv tool · pipx · pip · 소스 checkout)에 맞게 올립니다. 업데이트 뒤 Codex 새 도구 자동 승인을 적용하고, herdr 창의 데몬을 새 코드로 다시 띄웁니다.
- **Windows 안전 장치**: 실행 중인 srhbroker 가 설치 파일을 잠가 재설치가 실패하며 설치가 반쯤 지워지는 문제(실측)를 막습니다. 업데이트할 설치의 프로세스만 골라(다른 설치·자기 자신 제외) 보여 주고 멈추며, `--stop-all` 이면 끝낸 뒤 설치를 별도 프로세스로 이어서 합니다(`update.log`).
- **새 버전 알림**: 데몬이 주기적으로 확인(`[update] check_hours`, 기본 12시간)해 `watch`·웹 대시보드·`doctor` 에 표시. `srhbroker --version`.

## 0.3.0 — 2026-10-10

### 추가
- **등록 이름 바꾸기**: MCP 도구 `rename(new_name, old_name?)`, CLI `srhbroker rename <지금 이름> <새 이름>`.
  세션 ID·provider·작업 폴더·역할·별칭·설명·권한은 그대로 두고 이름만 바꿉니다. 옛 이름은 별칭으로 남고, 그 이름 앞 작업·회신·직접 전달·관찰 기록도 새 이름으로 옮깁니다.
- 등록에서 지워졌지만 기록이 남은 옛 이름을 새 이름 세션이 이어받기 (`rename <옛 이름> <새 이름>`) — unregister 후 다시 등록해 끊긴 회신 경로를 복구합니다.
- `setup codex --apply`: 이미 등록된 경우에도 새 버전에서 늘어난 도구의 자동 승인만 덧붙입니다.

### 바뀜
- 웹 대시보드 제목을 **Session Broker Dashboard** 로 바꿈 (README 화면도 새로 렌더링).
- 세션 안에서 `register(name, provider)` 를 불러도 지금 이 세션(같은 provider·interactive)이면 세션 ID 에 연결해 등록합니다. 세션 ID 없는 별도 항목을 만들지 않습니다.
- 이름 충돌 처리: 열려 있는 다른 세션·다른 provider·worker 이름은 거부, 세션 ID 없는 실수 항목·같은 세션 중복 항목은 정리 후 이어받음.

## 0.2.1 — 2026-10-09

### 고침
- `setup --apply` 가 MCP·hook 설정에 항상 `srhbroker` 절대 경로를 씁니다 (GUI·다른 셸에서 실행돼 PATH 가 달라도 동작).
- Windows 에서 Claude Code hook 경로(역슬래시·공백)를 따옴표로 감쌉니다 (hook 은 bash 로 실행됨).
- `doctor` 의 Jev 안내를 키만 없을 때와 SDK 가 없을 때로 나눔.

### 문서
- 시스템 Python 이 낮아도 uv 가 맞는 Python 을 받는다는 안내, PATH 경고 시 `uv tool update-shell`.
- macOS 26.6 (Apple Silicon) 검증 결과: 설치·연결·점검·예시·웹 대시보드, 테스트 195개 통과, 데몬이 실제 `codex exec` worker 실행·회신.

## 0.2.0 — 2026-10-09 (첫 공개)

- Claude Code ↔ Codex 세션을 rename 이름으로 묶는 MCP 서버, SQLite 작업 저장소(라우팅 G1~G5·안전 게이트), herdr 즉시 전달, 터미널(`watch`)·웹(`dashboard`) 대시보드.
- 승인 정책: 스스로 판단해 보낸 장비 제어 코드 변경·되돌리기 어려운 외부 작업만 승인, 알림·검토·사용자 직접 지시는 면제.
- 알림(`message`)은 받는 창이 닫혀 있으면 대기하지 않고 소멸.
- 세션 등록: 닫힌 이전 세션의 rename 이름 이어받기, 재등록 시 rename 이름으로 옮기기, `whoami` 불일치 알림.
- 새 PC 도구: `setup claude|codex --apply`, `doctor`, `demo`.
- 하드웨어 안전 규칙의 프로젝트 고유 대상은 `[router.hw_rules] extra_targets` 로 분리.
