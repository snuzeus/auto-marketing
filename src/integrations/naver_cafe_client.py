from __future__ import annotations

import mimetypes
import urllib.parse
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
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.access_token = access_token.strip()
        self.refresh_token = refresh_token.strip()
        self.telegram_bot_token = telegram_bot_token.strip()
        self.telegram_chat_id = telegram_chat_id.strip()
        self.enable_local_clipboard = enable_local_clipboard
        self._http = http_client

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

        if approval_mode == "telegram_confirm_api" and self._has_valid_telegram():
            await self._send_telegram_preview(
                subject=encoded["raw_subject"],
                html_body=html_body,
                search_tags=search_tags,
                club_id=club_id,
                menu_id=menu_id,
            )

        return await self._post_to_naver_api(
            club_id=club_id,
            menu_id=menu_id,
            encoded_subject=encoded["subject"],
            encoded_content=encoded["content"],
            image_path=path,
        )

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
