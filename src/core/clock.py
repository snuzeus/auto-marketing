from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Windows 기본 환경에는 tzdata가 없어 zoneinfo("Asia/Seoul")가 실패할 수 있으므로 고정 오프셋을 쓴다.
KST = timezone(timedelta(hours=9), name="KST")
_WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")


def now_kst() -> datetime:
    return datetime.now(KST)


def today_kst_label(now: datetime | None = None) -> str:
    """프롬프트에 주입할 오늘 날짜 문자열. 예: '2026년 11월 16일 (월요일)'."""
    current = now or now_kst()
    return f"{current.year}년 {current.month}월 {current.day}일 ({_WEEKDAYS_KO[current.weekday()]}요일)"
