from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.executors.usage import parse_usage_breakdowns


def _ms(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000


def test_parse_usage_breakdowns_uses_real_message_day_and_model():
    timezone = ZoneInfo("UTC")
    messages = [
        {
            "info": {
                "role": "assistant",
                "modelID": "m1",
                "providerID": "p",
                "time": {"created": _ms(2026, 1, 1, 23, 50)},
                "tokens": {"input": 10, "output": 5},
            },
            "parts": [],
        },
        {
            "info": {
                "role": "assistant",
                "modelID": "m2",
                "providerID": "p",
                "time": {"created": _ms(2026, 1, 2, 0, 10)},
                "tokens": {"input": 3, "output": 2},
            },
            "parts": [{"type": "tool", "tool": "bash"}],
        },
    ]

    days, models = parse_usage_breakdowns(messages, timezone=timezone, fallback_date="2026-01-01")

    by_day = {entry["date"]: entry for entry in days}
    assert set(by_day) == {"2026-01-01", "2026-01-02"}
    assert by_day["2026-01-01"]["total_tokens"] == 15
    assert by_day["2026-01-02"]["total_tokens"] == 5
    assert by_day["2026-01-02"]["tool_call_count"] == 1

    by_model = {entry["model_name"]: entry for entry in models}
    assert set(by_model) == {"p/m1", "p/m2"}
    assert by_model["p/m1"]["total_tokens"] == 15
    assert by_model["p/m2"]["llm_call_count"] == 1
