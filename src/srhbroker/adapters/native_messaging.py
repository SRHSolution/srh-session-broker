"""같은 provider 통신을 호출 세션의 기본 도구로 넘기기 위한 안내.

MCP 서버는 호스트의 기본 도구를 호출할 수 없다. 따라서 전달 성공을 가장하거나
별도 CLI 세션을 실행하지 않고, 호출자가 사용할 대상 정보와 원래 요청을 돌려준다.
"""

from __future__ import annotations

from typing import Any

from ..models import Session


def native_handoff(sender: Session, targets: list[Session], request: dict[str, Any]) -> dict[str, Any]:
    if sender.provider == "claude":
        guidance = ("Claude의 ListAgents로 대상 세션 또는 팀원을 확인한 뒤 SendMessage로 직접 전달하세요. "
                    "수신자는 ListAgents가 반환한 실제 주소를 사용하세요.")
    else:
        # Codex 협업 도구(spawn_agent·send_message·list_agents 등)는 이 세션이 띄운 하위 에이전트에만 닿는다.
        # 다른 창의 독립 Codex 세션 하나를 지정하면 broker 가 herdr 로 넣는다 — 여기는 대상이 여럿이거나 worker 인 경우
        guidance = ("독립 Codex 세션끼리는 Codex 기본 협업 도구(하위 에이전트용)로 닿지 않습니다. "
                    "대상 세션 하나를 이름으로 지정해 다시 send 하면 broker 가 받는 창에 넣습니다(작업 기록 없음). "
                    "worker 세션에는 직접 전달할 수 없으니 사용자에게 알리세요.")
    return {
        "status": "native_required", "transport": "native", "delivered": False, "task_id": None,
        "from": sender.name, "to": targets[0].name if len(targets) == 1 else None,
        "provider": sender.provider,
        "targets": [{**s.public(), "native_id": s.native_id} for s in targets],
        "request": request,
        "next": (guidance + " 본문과 완료 조건 등 request 전체를 전달하고, 결과도 기본 도구로 받으세요. "
                 "등록 이름·native_id만으로 독립 세션에 접근할 수 있다고 가정하지 마세요. "
                 "대상 또는 기본 도구가 없으면 전달 불가를 알리세요. "
                 "이 응답은 전달 완료가 아니며 broker 작업은 생성되지 않았습니다. "
                 "srhbroker send/route/wait/reply 또는 herdr로 우회하지 마세요."),
    }
