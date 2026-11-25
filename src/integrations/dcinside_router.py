from __future__ import annotations

import html
import json
import mimetypes
from pathlib import Path
from typing import Any

import httpx
import pyperclip
from rich.console import Console
from rich.panel import Panel

TELEGRAM_API_BASE = "https://api.telegram.org"


class DCInsideIntegrationError(RuntimeError):
    """Raised when neither local clipboard nor Telegram bot is available/configured."""


class DCInsideRouter:
    """Clipboard 1-second copier + Rich terminal preview + Telegram Bot Photo/Inline-button sender."""

    def __init__(
        self,
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        enable_local_clipboard: bool = True,
        http_client: httpx.AsyncClient | None = None,
        console: Console | None = None,
    ) -> None:
        self.telegram_bot_token = telegram_bot_token.strip()
        self.telegram_chat_id = telegram_chat_id.strip()
        self.enable_local_clipboard = enable_local_clipboard
        self._http = http_client
        self._console = console or Console()

    def _has_valid_telegram(self) -> bool:
        return (
            bool(self.telegram_bot_token)
            and bool(self.telegram_chat_id)
            and not self.telegram_bot_token.startswith("123456789:")
            and self.telegram_chat_id != "987654321"
        )

    @staticmethod
    def format_clipboard_pack(title: str, body: str) -> str:
        return f"[제목]\n{title}\n\n[본문]\n{body}"

    @staticmethod
    def gallery_url(gallery_id: str) -> str:
        return f"https://gall.dcinside.com/board/lists?id={gallery_id}"

    async def dispatch_pack(
        self,
        gallery_id: str,
        title: str,
        body: str,
        image_path: str,
        require_telegram: bool = False,
    ) -> dict[str, Any]:
        """Copy formatted post to clipboard, render terminal preview, and send Telegram photo pack."""
        formatted = self.format_clipboard_pack(title, body)
        target_url = self.gallery_url(gallery_id)

        clipboard_copied = False
        if self.enable_local_clipboard:
            try:
                pyperclip.copy(formatted)
                clipboard_copied = True
            except Exception:
                clipboard_copied = False

        self._console.print(
            Panel(
                f"{formatted}\n\n갤러리 바로가기: {target_url}",
                title=f"[bold cyan]디시인사이드 원고 패키지 ({gallery_id})[/bold cyan]",
                expand=False,
            )
        )

        telegram_sent = False
        if self._has_valid_telegram():
            await self._send_telegram_photo_pack(
                gallery_id=gallery_id,
                formatted_text=formatted,
                target_url=target_url,
                image_path=image_path,
            )
            telegram_sent = True
        elif require_telegram:
            msg = (
                "텔레그램 봇 환경변수(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)가 "
                "유효하게 설정되지 않았습니다."
            )
            raise DCInsideIntegrationError(msg)

        if not clipboard_copied and not telegram_sent:
            msg = (
                "디시인사이드 원고 전달 실패: 로컬 클립보드 복사와 텔레그램 전송 모두 비활성화되었거나 "
                "자격증명이 설정되지 않았습니다."
            )
            raise DCInsideIntegrationError(msg)

        return {
            "gallery_id": gallery_id,
            "gallery_url": target_url,
            "clipboard_copied": clipboard_copied,
            "telegram_sent": telegram_sent,
            "formatted_text": formatted,
        }

    async def _send_telegram_photo_pack(
        self,
        gallery_id: str,
        formatted_text: str,
        target_url: str,
        image_path: str,
    ) -> None:
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"텔레그램으로 전송할 이미지가 없습니다: {image_path}")

        url = f"{TELEGRAM_API_BASE}/bot{self.telegram_bot_token}/sendPhoto"
        mime_type, _ = mimetypes.guess_type(str(path))
        if not mime_type:
            mime_type = "image/jpeg"

        # HTML mode with escaping: user text containing `_`, `*` or backticks would break Markdown.
        caption = (
            f"📋 디시인사이드 [{html.escape(gallery_id)}] 갤러리 원고\n\n"
            f"<pre>{html.escape(formatted_text[:850])}</pre>"
        )
        reply_markup = {
            "inline_keyboard": [
                [{"text": f"🔗 {gallery_id} 갤러리 바로가기", "url": target_url}]
            ]
        }

        data = {
            "chat_id": self.telegram_chat_id,
            "caption": caption,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(reply_markup, ensure_ascii=False),
        }
        files = {"photo": (path.name, path.read_bytes(), mime_type)}

        try:
            if self._http is not None:
                resp = await self._http.post(url, data=data, files=files)
            else:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(url, data=data, files=files)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            msg = f"텔레그램 sendPhoto 호출 실패: {exc}"
            raise DCInsideIntegrationError(msg) from exc
