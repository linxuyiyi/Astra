from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config.settings import get_settings


def day_bounds():
    timezone = ZoneInfo(get_settings().timezone)
    today = datetime.now(timezone).date()
    start = datetime.combine(today, time.min, tzinfo=timezone).astimezone(UTC)
    return start, start + timedelta(days=1)
