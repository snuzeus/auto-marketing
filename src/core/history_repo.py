from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

DEFAULT_DB_PATH = Path("config/campaigns_history.db")


class CampaignHistorySummary(BaseModel):
    session_id: str
    created_at: str
    notice_category: str
    user_prompt: str
    primary_subject: str
    enabled_channels: list[str]


def _approved_text_for_channel(channel: str, spec: dict[str, Any]) -> str:
    """승인된 채널 원고에서 해시태그·ID 등을 뺀 본문 텍스트만 뽑는다."""
    if channel == "instagram":
        parts = [spec.get("hook_headline"), spec.get("body")]
    elif channel == "x_twitter":
        parts = [spec.get("body")]
    elif channel == "naver_cafe":
        parts = [spec.get("subject"), spec.get("plain_body_preview")]
    else:
        parts = [spec.get("title"), spec.get("body")]
    return "\n\n".join(str(p).strip() for p in parts if isinstance(p, str) and p.strip())


class CampaignSQLiteRepository:
    """SQLite persistence repository for storing and reloading past notice campaigns."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS campaign_sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    notice_category TEXT NOT NULL,
                    user_prompt TEXT NOT NULL,
                    primary_subject TEXT NOT NULL,
                    enabled_channels_json TEXT NOT NULL,
                    session_json TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def save_session(self, session_model: Any) -> None:
        created_at = getattr(session_model, "created_at", None) or datetime.now(UTC).isoformat()
        session_json = session_model.model_dump_json()
        import json

        channels_json = json.dumps(list(session_model.enabled_channels), ensure_ascii=False)
        primary_subject = session_model.vision_result.primary_subject
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO campaign_sessions (
                    session_id, created_at, notice_category, user_prompt,
                    primary_subject, enabled_channels_json, session_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    notice_category = excluded.notice_category,
                    user_prompt = excluded.user_prompt,
                    primary_subject = excluded.primary_subject,
                    enabled_channels_json = excluded.enabled_channels_json,
                    session_json = excluded.session_json
                """,
                (
                    session_model.session_id,
                    created_at,
                    session_model.notice_category,
                    session_model.user_prompt,
                    primary_subject,
                    channels_json,
                    session_json,
                ),
            )
            conn.commit()

    def load_session_json(self, session_id: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT session_json FROM campaign_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def list_recent(self, limit: int = 30) -> list[CampaignHistorySummary]:
        import json

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT session_id, created_at, notice_category, user_prompt,
                       primary_subject, enabled_channels_json
                FROM campaign_sessions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        results: list[CampaignHistorySummary] = []
        for r in rows:
            try:
                ch_list = json.loads(r[5])
            except Exception:
                ch_list = []
            results.append(
                CampaignHistorySummary(
                    session_id=str(r[0]),
                    created_at=str(r[1]),
                    notice_category=str(r[2]),
                    user_prompt=str(r[3]),
                    primary_subject=str(r[4]),
                    enabled_channels=ch_list,
                )
            )
        return results

    def list_approved_examples(
        self,
        channel: str,
        notice_category: str,
        limit: int = 2,
        exclude_session_id: str | None = None,
        scan_limit: int = 60,
    ) -> list[str]:
        """
        같은 채널·같은 공지 유형에서 관리자가 '승인'한 최근 원고 본문을 최신순으로 돌려준다.
        다음 생성 때 문체 참고용 예시(few-shot)로 쓴다.
        """
        import json

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT session_id, session_json
                FROM campaign_sessions
                WHERE notice_category = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (notice_category, scan_limit),
            ).fetchall()

        examples: list[str] = []
        for session_id, session_json in rows:
            if exclude_session_id and session_id == exclude_session_id:
                continue
            try:
                draft = json.loads(session_json).get("drafts", {}).get(channel)
            except Exception:
                continue
            if not isinstance(draft, dict) or draft.get("status") != "approved":
                continue
            text = _approved_text_for_channel(channel, draft.get("spec") or {})
            if text and text not in examples:
                examples.append(text)
            if len(examples) >= limit:
                break
        return examples

    def delete_session(self, session_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM campaign_sessions WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
