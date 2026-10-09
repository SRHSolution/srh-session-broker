"""srhbroker dashboard — 로컬 웹 대시보드 (표준 라이브러리 http.server).

보안
- 127.0.0.1 에만 연다. 시작할 때 만든 토큰이 있어야 데이터를 볼 수 있다 (주소에 ?t=<토큰>).
- 조작(승인·취소·대상 지정)은 POST + X-Token 헤더 + Origin·Host 검사 — 다른 웹사이트가 몰래 요청하는 CSRF·DNS 리바인딩 차단.
- 조작 권한은 터미널의 사람(CLI)과 같다. 승인 기록에는 via=dashboard 로 남는다.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import monitor
from .models import USER_ADDR, BrokerError


def _page() -> bytes:
    return resources.files("srhbroker").joinpath("static/dashboard.html").read_bytes()


def make_handler(b: Any, token: str, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    lock = threading.Lock()  # 브로커 객체(DB 연결·herdr 호출)는 한 번에 한 요청만

    class Handler(BaseHTTPRequestHandler):
        server_version = "srhbroker-dashboard"

        def log_message(self, fmt: str, *args: Any) -> None:  # 요청마다 콘솔에 찍지 않는다
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                             "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def _token_ok(self, given: str | None) -> bool:
            return bool(given) and hmac.compare_digest(given, token)

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._send(HTTPStatus.FORBIDDEN, b"forbidden host", "text/plain; charset=utf-8")
            u = urlparse(self.path)
            if u.path == "/":
                if not self._token_ok(parse_qs(u.query).get("t", [None])[0]):
                    return self._send(HTTPStatus.FORBIDDEN, "토큰이 필요합니다 — `srhbroker dashboard` 가 출력한 주소로 여세요"
                                      .encode("utf-8"), "text/plain; charset=utf-8")
                return self._send(HTTPStatus.OK, _page(), "text/html; charset=utf-8")
            if not self._token_ok(self.headers.get("X-Token")):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "token"})
            try:
                with lock:
                    if u.path == "/api/snapshot":
                        return self._json(HTTPStatus.OK, monitor.snapshot(b))
                    if u.path == "/api/flows":
                        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
                        rows = monitor.search_flows(
                            b, q=qs.get("q"), kind=qs.get("kind") or None, session=qs.get("session") or None,
                            status=qs.get("status") or None,
                            since_hours=float(qs["since"]) if qs.get("since") else None,
                            teammates=qs.get("teammates") == "1",
                            limit=max(1, min(int(qs.get("limit") or 100), 2000)))
                        return self._json(HTTPStatus.OK, {"flows": rows})
                    if u.path.startswith("/api/task/"):
                        return self._json(HTTPStatus.OK, monitor.task_detail(b, u.path.rsplit("/", 1)[1]))
            except BrokerError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except ValueError:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "잘못된 검색 조건"})
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            origin = self.headers.get("Origin", "")
            if (not self._host_ok() or not self._token_ok(self.headers.get("X-Token"))
                    or origin not in {f"http://{h}" for h in allowed_hosts}):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                tid = str(req.get("task_id") or "")
                with lock:
                    path = urlparse(self.path).path
                    if path == "/api/cancel":
                        out = b.cancel(tid, by=USER_ADDR)
                    elif path == "/api/approve":
                        out = b.approve(tid, confirmation="대시보드에서 사용자가 승인 버튼을 누름", via="dashboard")
                    elif path == "/api/route":
                        out = b.assign(tid, str(req.get("to") or ""))
                    else:
                        return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return self._json(HTTPStatus.OK, out)
            except BrokerError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except (ValueError, TypeError):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "잘못된 요청"})

    return Handler


def _token(home: Any, renew: bool) -> str:
    """대시보드 토큰. 브로커 홈에 저장해 재시작해도 열어 둔 탭(같은 주소)이 계속 동작하게 한다."""
    p = home / "dashboard.token"
    if not renew:
        try:
            t = p.read_text(encoding="utf-8").strip()
            if len(t) >= 20:
                return t
        except OSError:
            pass
    t = secrets.token_urlsafe(18)
    home.mkdir(parents=True, exist_ok=True)
    p.write_text(t, encoding="utf-8")
    return t


def serve(b: Any, port: int = 8765, open_browser: bool = False, new_token: bool = False) -> int:
    token = _token(b.cfg.home, new_token)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(b, token, port))
    url = f"http://127.0.0.1:{port}/?t={token}"
    print(f"Session Broker Dashboard: {url}\n(이 주소의 토큰이 있어야 열립니다. 재시작해도 같은 주소 — 바꾸려면 --new-token. "
          "Ctrl+C 로 종료)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
