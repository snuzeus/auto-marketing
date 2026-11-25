from __future__ import annotations

import asyncio
import json
import mimetypes
import secrets
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pyperclip

NAVER_CAFE_API_BASE = "https://openapi.naver.com/v1/cafe"
NAVER_OAUTH_TOKEN_URL = "https://nid.naver.com/oauth2.0/token"
TELEGRAM_API_BASE = "https://api.telegram.org"


class NaverCafeIntegrationError(RuntimeError):
    """Raised when Naver Cafe or Telegram credentials are missing or API call fails."""


class NaverCafeClient:
    """Naver Cafe Open API client with OAuth2 token refresh and Telegram/Clipboard routing."""

    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        access_token: str = "",
        refresh_token: str = "",
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        enable_local_clipboard: bool = True,
        http_client: httpx.AsyncClient | None = None,
        on_token_refresh: Callable[[str, str], None] | None = None,
        approval_timeout_s: float = 300.0,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.access_token = access_token.strip()
        self.refresh_token = refresh_token.strip()
        self.telegram_bot_token = telegram_bot_token.strip()
        self.telegram_chat_id = telegram_chat_id.strip()
        self.enable_local_clipboard = enable_local_clipboard
        self._http = http_client
        # Called with (access_token, refresh_token) after a successful refresh so callers can persist them.
        self._on_token_refresh = on_token_refresh
        self.approval_timeout_s = approval_timeout_s
        self.poll_interval_s = poll_interval_s

    def _has_valid_naver_token(self) -> bool:
        return bool(self.access_token) and self.access_token != "your_oauth2_access_token"

    def _has_valid_telegram(self) -> bool:
        return (
            bool(self.telegram_bot_token)
            and bool(self.telegram_chat_id)
            and not self.telegram_bot_token.startswith("123456789:")
            and self.telegram_chat_id != "987654321"
        )

    async def refresh_access_token(self) -> str:
        """Refresh Naver OAuth2 access token using `refresh_token`."""
        if (
            not self.client_id
            or not self.client_secret
            or not self.refresh_token
            or self.refresh_token == "your_oauth2_refresh_token"
        ):
            msg = (
                "네이버 OAuth2 토큰 갱신에 필요한 환경변수(NAVER_CLIENT_ID, "
                "NAVER_CLIENT_SECRET, NAVER_REFRESH_TOKEN)가 설정되지 않았습니다."
            )
            raise NaverCafeIntegrationError(msg)

        params = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }
        try:
            if self._http is not None:
                resp = await self._http.post(NAVER_OAUTH_TOKEN_URL, params=params)
            else:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post(NAVER_OAUTH_TOKEN_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            msg = f"네이버 OAuth2 토큰 갱신 요청 실패: {exc}"
            raise NaverCafeIntegrationError(msg) from exc

        new_token = str(payload.get("access_token") or "")
        if not new_token:
            raise NaverCafeIntegrationError(f"네이버 토큰 갱신 실패: {payload}")
        self.access_token = new_token
        new_refresh = str(payload.get("refresh_token") or "")
        if new_refresh:
            self.refresh_token = new_refresh
        if self._on_token_refresh is not None:
            self._on_token_refresh(self.access_token, self.refresh_token)
        return new_token

    @staticmethod
    def build_encoded_fields(
        head_tag: str,
        subject: str,
        html_body: str,
        search_tags: list[str],
    ) -> dict[str, str]:
        """Encode `subject` and `content` per Naver Cafe Open API URL-encoding rules."""
        full_subject = f"{head_tag} {subject}".strip() if head_tag else subject.strip()
        tags_footer = (
            "<br><br><p><b>검색 키워드:</b> " + ", ".join(search_tags) + "</p>"
            if search_tags
            else ""
        )
        full_html = f"{html_body}{tags_footer}"

        # Double-friendly UTF-8 percent encoding required by Naver Cafe multipart endpoint
        encoded_subject = urllib.parse.quote(full_subject, encoding="utf-8")
        encoded_content = urllib.parse.quote(full_html, encoding="utf-8")
        return {
            "subject": encoded_subject,
            "content": encoded_content,
            "raw_subject": full_subject,
            "raw_html": full_html,
        }

    async def publish_article(
        self,
        club_id: str,
        menu_id: int,
        head_tag: str,
        subject: str,
        html_body: str,
        search_tags: list[str],
        image_path: str,
        approval_mode: str = "telegram_confirm_api",
    ) -> dict[str, Any]:
        """
        Publish or package a Naver Cafe article according to `approval_mode`:
        - `clipboard_and_telegram`: Copy formatted package to clipboard and/or send to Telegram.
        - `direct_api` or `telegram_confirm_api`: Post article via Naver Cafe Open API
          (with automatic 401 refresh retry).
        """
        encoded = self.build_encoded_fields(head_tag, subject, html_body, search_tags)

        if approval_mode == "clipboard_and_telegram":
            clipboard_text = (
                f"[제목] {encoded['raw_subject']}\n\n"
                f"[HTML 본문]\n{encoded['raw_html']}\n\n"
                f"[태그] {', '.join(search_tags)}"
            )
            copied = False
            if self.enable_local_clipboard:
                try:
                    pyperclip.copy(clipboard_text)
                    copied = True
                except Exception:
                    copied = False

            telegram_sent = False
            if self._has_valid_telegram():
                await self._send_telegram_preview(
                    subject=encoded["raw_subject"],
                    html_body=html_body,
                    search_tags=search_tags,
                    club_id=club_id,
                    menu_id=menu_id,
                )
                telegram_sent = True

            if not copied and not telegram_sent:
                msg = (
                    "클립보드 복사와 텔레그램 전송 모두 실행할 수 없습니다. "
                    "ENABLE_LOCAL_CLIPBOARD 또는 TELEGRAM_BOT_TOKEN/CHAT_ID 설정을 확인하세요."
                )
                raise NaverCafeIntegrationError(msg)

            return {
                "mode": "clipboard_and_telegram",
                "clipboard_copied": copied,
                "telegram_sent": telegram_sent,
                "subject": encoded["raw_subject"],
            }

        # Open API call mode
        if not self._has_valid_naver_token():
            msg = (
                "네이버 카페 Open API 연동에 필요한 NAVER_ACCESS_TOKEN 환경변수가 "
                "설정되지 않았습니다."
            )
            raise NaverCafeIntegrationError(msg)

        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"네이버 카페 첨부 이미지를 찾을 수 없습니다: {image_path}")

        approved_via_telegram = False
        if approval_mode == "telegram_confirm_api":
            if not self._has_valid_telegram():
                msg = (
                    "telegram_confirm_api 모드는 텔레그램 승인이 필요하지만 TELEGRAM_BOT_TOKEN/"
                    "TELEGRAM_CHAT_ID가 설정되지 않았습니다. 설정하거나 approval_mode를 변경하세요."
                )
                raise NaverCafeIntegrationError(msg)
            decision = await self._request_telegram_approval(
                subject=encoded["raw_subject"],
                html_body=html_body,
                search_tags=search_tags,
                club_id=club_id,
                menu_id=menu_id,
            )
            if decision != "approved":
                return {
                    "mode": "telegram_confirm_api",
                    "status": decision,
                    "posted": False,
                    "subject": encoded["raw_subject"],
                }
            approved_via_telegram = True

        result = await self._post_to_naver_api(
            club_id=club_id,
            menu_id=menu_id,
            encoded_subject=encoded["subject"],
            encoded_content=encoded["content"],
            image_path=path,
        )
        if approved_via_telegram:
            result["approved_via"] = "telegram"
            result["status"] = "posted"
        return result

    async def _telegram_call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{TELEGRAM_API_BASE}/bot{self.telegram_bot_token}/{method}"
        try:
            if self._http is not None:
                resp = await self._http.post(url, timeout=60.0, **kwargs)
            else:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NaverCafeIntegrationError(f"텔레그램 {method} 호출 실패: {exc}") from exc

    async def _request_telegram_approval(
        self,
        subject: str,
        html_body: str,
        search_tags: list[str],
        club_id: str,
        menu_id: int,
    ) -> str:
        """Send preview with approve/reject buttons and wait. Returns approved|rejected|timeout."""
        token = secrets.token_hex(6)
        approve_data = f"naver_approve:{token}"
        reject_data = f"naver_reject:{token}"
        text = (
            "📌 [네이버 카페 게시 승인 요청]\n"
            f"- 카페 ID: {club_id} (메뉴: {menu_id})\n"
            f"- 제목: {subject}\n"
            f"- 검색 태그: {', '.join(search_tags)}\n\n"
            f"{html_body[:800]}"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ 네이버 카페 게시 승인", "callback_data": approve_data},
                    {"text": "❌ 취소", "callback_data": reject_data},
                ]
            ]
        }
        sent = await self._telegram_call(
            "sendMessage",
            json={
                "chat_id": self.telegram_chat_id,
                "text": text,
                "reply_markup": keyboard,
            },
        )
        message_id = (sent.get("result") or {}).get("message_id")

        deadline = time.monotonic() + self.approval_timeout_s
        offset: int | None = None
        while time.monotonic() < deadline:
            wait = int(max(0, min(25, deadline - time.monotonic())))
            body: dict[str, Any] = {"timeout": wait, "allowed_updates": ["callback_query"]}
            if offset is not None:
                body["offset"] = offset
            updates = await self._telegram_call("getUpdates", json=body)
            decision: str | None = None
            for update in updates.get("result", []):
                offset = int(update["update_id"]) + 1
                cq = update.get("callback_query") or {}
                chat_id = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
                if chat_id != self.telegram_chat_id:
                    continue
                if cq.get("data") in (approve_data, reject_data):
                    decision = "approved" if cq["data"] == approve_data else "rejected"
                    await self._telegram_call(
                        "answerCallbackQuery",
                        json={
                            "callback_query_id": cq.get("id"),
                            "text": "게시를 진행합니다." if decision == "approved" else "취소되었습니다.",
                        },
                    )
                    break
            if decision is not None:
                if message_id is not None:
                    try:
                        await self._telegram_call(
                            "editMessageReplyMarkup",
                            json={
                                "chat_id": self.telegram_chat_id,
                                "message_id": message_id,
                                "reply_markup": json.loads('{"inline_keyboard": []}'),
                            },
                        )
                    except NaverCafeIntegrationError:
                        pass
                return decision
            if self.poll_interval_s > 0:
                await asyncio.sleep(self.poll_interval_s)
        return "timeout"

    async def _post_to_naver_api(
        self,
        club_id: str,
        menu_id: int,
        encoded_subject: str,
        encoded_content: str,
        image_path: Path,
    ) -> dict[str, Any]:
        url = f"{NAVER_CAFE_API_BASE}/{club_id}/menu/{menu_id}/articles"
        mime_type, _ = mimetypes.guess_type(str(image_path))
        if not mime_type:
            mime_type = "image/jpeg"

        image_bytes = image_path.read_bytes()

        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self.access_token}"}
            data = {"subject": encoded_subject, "content": encoded_content}
            files = [("image", (image_path.name, image_bytes, mime_type))]

            try:
                if self._http is not None:
                    resp = await self._http.post(
                        url, headers=headers, data=data, files=files
                    )
                else:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.post(
                            url, headers=headers, data=data, files=files
                        )
            except httpx.HTTPError as exc:
                msg = f"네이버 카페 API 네트워크 오류: {exc}"
                raise NaverCafeIntegrationError(msg) from exc

            if resp.status_code == 401 and attempt == 0 and self.refresh_token:
                await self.refresh_access_token()
                continue

            if resp.status_code >= 400:
                msg = f"네이버 카페 게시글 등록 실패 (HTTP {resp.status_code}): {resp.text}"
                raise NaverCafeIntegrationError(msg)

            payload = resp.json()
            result_data = payload.get("message", {}).get("result", payload)
            article_id = result_data.get("articleId") or payload.get("articleId")
            article_url = result_data.get("articleUrl") or payload.get("articleUrl") or ""
            return {
                "mode": "naver_open_api",
                "article_id": article_id,
                "article_url": article_url,
                "club_id": club_id,
                "menu_id": menu_id,
            }

        raise NaverCafeIntegrationError("네이버 카페 API 인증 갱신 후에도 요청에 실패했습니다.")

    async def _send_telegram_preview(
        self,
        subject: str,
        html_body: str,
        search_tags: list[str],
        club_id: str,
        menu_id: int,
    ) -> None:
        url = f"{TELEGRAM_API_BASE}/bot{self.telegram_bot_token}/sendMessage"
        text = (
            f"📌 [네이버 카페 원고 패키지]\n"
            f"- 카페 ID: {club_id} (메뉴: {menu_id})\n"
            f"- 제목: {subject}\n"
            f"- 검색 태그: {', '.join(search_tags)}\n\n"
            f"{html_body[:800]}"
        )
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": text,
        }
        try:
            if self._http is not None:
                resp = await self._http.post(url, json=payload)
            else:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post(url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            msg = f"텔레그램 카페 프리뷰 전송 실패: {exc}"
            raise NaverCafeIntegrationError(msg) from exc
