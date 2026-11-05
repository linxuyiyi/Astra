from app.runtime.celery_app import celery


def test_celery_is_not_a_second_session_state_owner():
    schedule = celery.conf.beat_schedule or {}
    assert "astra-recovery" not in schedule
    assert all(item.get("task") != "astra.recovery" for item in schedule.values())
