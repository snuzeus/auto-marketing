"""Settings page API: lets a non-technical user enter keys and store info from the browser.

Values are written to the project's `.env` and applied to the running process. Secrets are
never sent back to the browser; only whether they are set. Every route here is restricted to
requests from this machine because it reads and writes credentials.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.core.config_loader import load_app_settings
from src.core.env_file import update_env_file

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
NAVER_AUTHORIZE_URL = "https://nid.naver.com/oauth2.0/authorize"
NAVER_TOKEN_URL = "https://nid.naver.com/oauth2.0/token"
NAVER_REDIRECT_URI = "http://localhost:8000/naver/callback"

# Values shipped in `.env.example`; treated as "not set".
PLACEHOLDERS = {
    "",
    "AIzaSyYourGoogleGeminiApiKey...",
    "pz_live_...",
    "ch_insta_01",
    "ch_x_01",
    "your_naver_developers_client_id",
    "your_naver_developers_client_secret",
    "your_oauth2_access_token",
    "your_oauth2_refresh_token",
    "12345678",
    "123456789:ABCDEF...",
    "987654321",
}


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    group: str
    secret: bool = False
    postiz: bool = False  # read by the Postiz container -> needs a container restart
    hint: str = ""


FIELDS: tuple[SettingField, ...] = (
    SettingField("STORE_NAME", "매장 이름", "store", hint="공지 카드 하단에 표시됩니다"),
    SettingField("GEMINI_API_KEY", "Gemini API 키", "gemini", secret=True, hint="aistudio.google.com 에서 발급"),
    SettingField("POSTIZ_API_KEY", "Postiz API 키", "postiz", secret=True, hint="Postiz 설정 > Agents / Dev"),
    SettingField("X_API_KEY", "X API Key", "postiz", secret=True, postiz=True),
    SettingField("X_API_SECRET", "X API Key Secret", "postiz", secret=True, postiz=True),
    SettingField("INSTAGRAM_APP_ID", "Instagram App ID", "postiz", secret=True, postiz=True),
    SettingField("INSTAGRAM_APP_SECRET", "Instagram App Secret", "postiz", secret=True, postiz=True),
    SettingField("TELEGRAM_BOT_TOKEN", "텔레그램 봇 토큰", "telegram", secret=True, hint="@BotFather 에서 발급"),
    SettingField("TELEGRAM_CHAT_ID", "텔레그램 chat ID", "telegram", hint="@userinfobot 의 Id 숫자"),
    SettingField("NAVER_CLIENT_ID", "네이버 Client ID", "naver", secret=True),
    SettingField("NAVER_CLIENT_SECRET", "네이버 Client Secret", "naver", secret=True),
    SettingField("NAVER_CAFE_CLUB_ID", "카페 ID (숫자)", "naver", hint="게시판 주소의 clubid 값"),
    SettingField("NAVER_CAFE_MENU_ID", "게시판 ID (숫자)", "naver", hint="게시판 주소의 menuid 값"),
    SettingField("DCINSIDE_DEFAULT_GALLERY", "디시 기본 갤러리", "dcinside", hint="갤러리 ID 또는 주소"),
)
FIELDS_BY_KEY = {f.key: f for f in FIELDS}


def require_local(request: Request) -> None:
    """Credentials pages must not be reachable from other machines (e.g. with --host 0.0.0.0)."""
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(status_code=403, detail="설정은 이 컴퓨터에서만 바꿀 수 있습니다.")


def _current(key: str) -> str:
    value = os.environ.get(key, "").strip()
    return "" if value in PLACEHOLDERS else value


def save_env_values(values: dict[str, str]) -> None:
    """Persist to `.env` and apply to this process so the change takes effect without restart."""
    if not values:
        return
    update_env_file(ENV_PATH, values)
    for key, value in values.items():
        os.environ[key] = value


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_local)])
    naver_states: set[str] = set()

    @router.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        load_app_settings()  # make sure .env is loaded into the environment
        fields = []
        for f in FIELDS:
            value = _current(f.key)
            fields.append(
                {
                    "key": f.key,
                    "label": f.label,
                    "group": f.group,
                    "secret": f.secret,
                    "hint": f.hint,
                    "is_set": bool(value),
                    "value": "" if f.secret else value,
                }
            )
        return {
            "fields": fields,
            "naver_connected": bool(_current("NAVER_ACCESS_TOKEN")),
            "naver_redirect_uri": NAVER_REDIRECT_URI,
            "x_redirect_uri": "http://localhost:4007/integrations/social/x",
            "instagram_redirect_uri": "http://localhost:4007/integrations/social/instagram-standalone",
        }

    @router.put("/api/settings")
    async def put_settings(payload: dict[str, str]) -> dict[str, Any]:
        updates: dict[str, str] = {}
        for key, raw in payload.items():
            field = FIELDS_BY_KEY.get(key)
            if field is None:
                raise HTTPException(status_code=400, detail=f"알 수 없는 설정 항목입니다: {key}")
            value = str(raw).strip()
            if '"' in value or "\n" in value:
                raise HTTPException(status_code=400, detail=f"{field.label}에 쓸 수 없는 문자가 있습니다.")
            if field.secret and not value:
                continue  # empty secret box means "keep the current value"
            if key == "NAVER_CAFE_MENU_ID" and value and not value.isdigit():
                raise HTTPException(status_code=400, detail="게시판 ID는 숫자여야 합니다.")
            updates[key] = value
        save_env_values(updates)
        postiz_changed = any(FIELDS_BY_KEY[k].postiz for k in updates)
        return {"saved": sorted(updates), "postiz_restart_needed": postiz_changed}

    @router.post("/api/settings/restart-postiz")
    async def restart_postiz() -> dict[str, Any]:
        """Recreate the Postiz containers so they pick up new X / Instagram app keys."""
        try:
            proc = subprocess.run(
                ["docker", "compose", "up", "-d"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail="Docker를 찾을 수 없습니다. Docker Desktop을 설치하세요.") from exc
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail="Postiz 재시작이 너무 오래 걸립니다.") from exc
        if proc.returncode != 0:
            raise HTTPException(
                status_code=503,
                detail="Postiz를 다시 시작하지 못했습니다. Docker Desktop이 켜져 있는지 확인하세요.\n"
                + proc.stderr[-500:],
            )
        return {"restarted": True}

    @router.post("/api/settings/test/{service}")
    async def test_service(service: str) -> dict[str, Any]:
        """Cheap, side-effect-free connectivity checks."""
        s = load_app_settings()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                if service == "gemini":
                    if not s.has_gemini_key:
                        return {"ok": False, "message": "Gemini API 키가 없습니다."}
                    r = await client.get(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{s.gemini_main_model}",
                        headers={"x-goog-api-key": s.gemini_api_key},
                    )
                    ok = r.status_code == 200
                    return {"ok": ok, "message": "연결됨" if ok else f"키를 확인하세요 (HTTP {r.status_code})"}
                if service == "postiz":
                    if not s.has_postiz_credentials:
                        return {"ok": False, "message": "Postiz API 키가 없습니다."}
                    r = await client.get(
                        f"{s.postiz_base_url.rstrip('/')}/integrations",
                        headers={"Authorization": s.postiz_api_key},
                    )
                    if r.status_code != 200:
                        return {"ok": False, "message": f"키를 확인하세요 (HTTP {r.status_code})"}
                    names = [str(i.get("identifier")) for i in r.json() if not i.get("disabled")]
                    label = ", ".join(names) if names else "연결된 채널 없음"
                    return {"ok": True, "message": f"연결됨 · 채널: {label}"}
                if service == "telegram":
                    if not s.has_telegram_credentials:
                        return {"ok": False, "message": "봇 토큰과 chat ID를 모두 입력하세요."}
                    r = await client.post(
                        f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                        json={"chat_id": s.telegram_chat_id, "text": "✅ 공지 스튜디오 연결 확인"},
                    )
                    ok = r.status_code == 200
                    return {
                        "ok": ok,
                        "message": "텔레그램으로 확인 메시지를 보냈습니다" if ok else "토큰이나 chat ID를 확인하세요. 봇에게 먼저 말을 걸어야 합니다.",
                    }
        except httpx.ConnectError:
            hint = " Docker Desktop과 Postiz가 켜져 있는지 확인하세요." if service == "postiz" else ""
            return {"ok": False, "message": "서버에 연결할 수 없습니다." + hint}
        except httpx.HTTPError as exc:
            return {"ok": False, "message": f"연결 확인 실패: {type(exc).__name__}"}
        raise HTTPException(status_code=404, detail="알 수 없는 서비스입니다.")

    @router.get("/naver/login")
    async def naver_login() -> RedirectResponse:
        client_id = _current("NAVER_CLIENT_ID")
        if not client_id or not _current("NAVER_CLIENT_SECRET"):
            raise HTTPException(status_code=400, detail="네이버 Client ID와 Secret을 먼저 저장하세요.")
        state = secrets.token_urlsafe(16)
        naver_states.add(state)
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": NAVER_REDIRECT_URI,
                "state": state,
                "auth_type": "reprompt",
            }
        )
        return RedirectResponse(f"{NAVER_AUTHORIZE_URL}?{query}")

    @router.get("/naver/callback", response_class=HTMLResponse)
    async def naver_callback(
        state: str = "", code: str = "", error: str = "", error_description: str = ""
    ) -> HTMLResponse:
        def page(title: str, body: str) -> HTMLResponse:
            return HTMLResponse(
                f"<!doctype html><meta charset='utf-8'><title>{title}</title>"
                f"<body style='font-family:sans-serif;padding:40px'><h2>{title}</h2><p>{body}</p>"
                "<p><a href='/'>공지 스튜디오로 돌아가기</a></p></body>"
            )

        if state not in naver_states:
            return page("네이버 연결 실패", "요청이 만료되었습니다. 설정 화면에서 다시 눌러 주세요.")
        naver_states.discard(state)
        if not code:
            return page("네이버 연결 실패", f"{error} {error_description}".strip() or "로그인이 취소되었습니다.")

        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                NAVER_TOKEN_URL,
                params={
                    "grant_type": "authorization_code",
                    "client_id": _current("NAVER_CLIENT_ID"),
                    "client_secret": _current("NAVER_CLIENT_SECRET"),
                    "code": code,
                    "state": state,
                },
            )
        data = r.json() if r.content else {}
        access = str(data.get("access_token") or "")
        if r.status_code != 200 or not access:
            return page("네이버 연결 실패", f"토큰을 받지 못했습니다: {data.get('error_description') or data.get('error') or r.status_code}")
        values = {"NAVER_ACCESS_TOKEN": access}
        if data.get("refresh_token"):
            values["NAVER_REFRESH_TOKEN"] = str(data["refresh_token"])
        save_env_values(values)
        return page("네이버 카페 연결 완료", "이 창을 닫고 공지 스튜디오로 돌아가세요.")

    return router
