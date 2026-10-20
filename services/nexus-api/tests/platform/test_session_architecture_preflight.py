from app.models import Base
from app.runtime.celery_app import celery


def test_session_architecture_has_no_legacy_execution_tables():
    legacy = {
        "runs",
        "tool_usages",
        "evidence",
        "task_sessions",
        "session_messages",
        "run_inquiries",
    }
    assert not legacy.intersection(Base.metadata.tables)
    assert "t_nexus_runtime_sessions" in Base.metadata.tables
    assert "t_nexus_task_summaries" in Base.metadata.tables
    assert "result" not in Base.metadata.tables["t_nexus_tasks"].c


def test_celery_is_not_a_second_session_state_owner():
    schedule = celery.conf.beat_schedule or {}
    assert "astra-recovery" not in schedule
    assert all(item.get("task") != "astra.recovery" for item in schedule.values())
