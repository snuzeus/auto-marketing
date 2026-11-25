"""One-time Naver OAuth helper: log in once, store NAVER_ACCESS_TOKEN / NAVER_REFRESH_TOKEN in .env.

Usage (stop the web studio first if it uses port 8000):
    uv run python scripts/naver_token.py

Requires NAVER_CLIENT_ID and NAVER_CLIENT_SECRET in .env and the Callback URL
`http://localhost:8000/naver/callback` registered on the Naver application.
Tokens are written to .env and never printed.
"""

from __future__ import annotations

import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.core.env_file import update_env_file  # noqa: E402

ENV_PATH = Path(".env")
AUTHORIZE_URL = "https://nid.naver.com/oauth2.0/authorize"
TOKEN_URL = "https://nid.naver.com/oauth2.0/token"
REDIRECT_URI = "http://localhost:8000/naver/callback"


def main() -> int:
    env = dotenv_values(ENV_PATH)
    client_id = (env.get("NAVER_CLIENT_ID") or "").strip()
    client_secret = (env.get("NAVER_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret or client_id.startswith("your_"):
        print("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 을 .env 에 먼저 설정하세요.")
        return 2

    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/naver/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            result.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>완료되었습니다. 이 창을 닫고 터미널로 돌아가세요.</h3>".encode())
            done.set()

        def log_message(self, *args: object) -> None:  # silence request logs (contain the code)
            return

    server = HTTPServer(("127.0.0.1", 8000), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": state,
            # Show the consent screen again so newly added API permissions (e.g. cafe) get granted.
            "auth_type": "reprompt",
        }
    )
    print("브라우저에서 네이버 로그인/동의를 진행하세요. 열리지 않으면 아래 주소를 직접 여세요:\n")
    print(auth_url, "\n")
    webbrowser.open(auth_url)

    if not done.wait(timeout=300):
        print("5분 안에 로그인이 완료되지 않았습니다.")
        server.shutdown()
        return 1
    server.shutdown()

    if result.get("state") != state:
        print("state 값이 일치하지 않아 중단합니다 (잘못된 요청일 수 있음).")
        return 1
    if "code" not in result:
        print(f"인증 실패: {result.get('error')} {result.get('error_description', '')}")
        return 1

    resp = httpx.post(
        TOKEN_URL,
        params={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": result["code"],
            "state": state,
        },
        timeout=20.0,
    )
    payload = resp.json() if resp.content else {}
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    if resp.status_code != 200 or not access:
        print(f"토큰 발급 실패 (HTTP {resp.status_code}): {payload.get('error')} "
              f"{payload.get('error_description', '')}")
        return 1

    updates = {"NAVER_ACCESS_TOKEN": access}
    if refresh:
        updates["NAVER_REFRESH_TOKEN"] = refresh
    update_env_file(ENV_PATH, updates)
    print(f".env 에 {', '.join(updates)} 를 저장했습니다. (expires_in={payload.get('expires_in')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
