from datetime import UTC, datetime, timedelta

from app.runtime.session_state import derive_session_state

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def assistant(message_id, *, completed=True, finish="stop", error=None):
    info = {"id": message_id, "role": "assistant"}
    if completed:
        info["time"] = {"completed": NOW.timestamp()}
    if finish is not None:
        info["finish"] = finish
    if error is not None:
        info["error"] = error
    return {"info": info, "parts": [{"type": "text", "text": "done"}]}


def user(message_id):
    return {"info": {"id": message_id, "role": "user"}, "parts": []}


def derive(messages, **overrides):
    kwargs = {
        "messages": messages,
        "server_status": "idle",
        "has_pending_question": False,
        "has_pending_permission": False,
        "pending_ack": False,
        "turn_anchor_id": None,
        "turn_started_at": NOW - timedelta(minutes=1),
        "now": NOW,
    }
    kwargs.update(overrides)
    return derive_session_state(**kwargs)


def test_abort_wins_over_everything():
    state = derive([assistant("a1")], aborted=True)
    assert state.status == "INTERRUPTED"
    assert state.terminal_reason == "aborted"


def test_timeout_marks_interrupted():
    state = derive([assistant("a1")], timed_out=True)
    assert state.status == "INTERRUPTED"
    assert state.terminal_reason == "timeout"


def test_pending_question_beats_busy():
    state = derive([], server_status="busy", has_pending_question=True)
    assert state.status == "WAITING"


def test_pending_permission_is_waiting():
    state = derive([], has_pending_permission=True)
    assert state.status == "WAITING"


def test_busy_is_running():
    assert derive([user("u1")], server_status="busy").status == "RUNNING"
    assert derive([user("u1")], server_status="retry").status == "RUNNING"


def test_unacknowledged_submission_is_running():
    assert derive([], pending_ack=True).status == "RUNNING"


def test_assistant_error_is_failed():
    state = derive([assistant("a1", error={"name": "UnknownError"})])
    assert state.status == "FAILED"
    assert state.terminal_reason == "assistant_error"


def test_completed_stop_is_succeeded():
    state = derive([user("u1"), assistant("a1")], turn_anchor_id="u1")
    assert state.status == "SUCCEEDED"
    assert state.finish_reason == "stop"
    assert state.finished_at == NOW


def test_completed_end_turn_is_succeeded():
    state = derive([assistant("a1", finish="end_turn")])
    assert state.status == "SUCCEEDED"


def test_completed_turn_without_visible_final_reply_is_failed():
    message = assistant("a1")
    message["parts"] = []
    state = derive([message])
    assert state.status == "FAILED"
    assert state.terminal_reason == "no_reply"


def test_new_user_turn_after_completion_is_not_succeeded():
    state = derive(
        [assistant("a1"), user("u2")],
        turn_anchor_id="u2",
        server_status="idle",
        turn_started_at=NOW - timedelta(seconds=2),
    )
    assert state.status == "RUNNING"


def test_previous_completed_turn_does_not_complete_fresh_new_submission():
    state = derive(
        [assistant("old"), user("new"), assistant("placeholder", completed=False, finish=None)],
        turn_anchor_id="new",
        turn_started_at=NOW - timedelta(seconds=2),
    )
    assert state.status == "RUNNING"


def test_aged_idle_incomplete_assistant_requires_recovery():
    state = derive(
        [user("new"), assistant("placeholder", completed=False, finish=None)],
        turn_anchor_id="new",
        turn_started_at=NOW - timedelta(minutes=5),
    )
    assert state.status == "INTERRUPTED"
    assert state.terminal_reason == "incomplete"


def test_just_submitted_without_reply_stays_running():
    state = derive(
        [user("u1")],
        turn_anchor_id=None,
        server_status="idle",
        turn_started_at=NOW - timedelta(seconds=2),
        grace_seconds=15,
    )
    assert state.status == "RUNNING"


def test_aged_turn_without_reply_is_failed():
    state = derive(
        [user("u1")],
        server_status="idle",
        turn_started_at=NOW - timedelta(minutes=5),
        grace_seconds=15,
    )
    assert state.status == "FAILED"
    assert state.terminal_reason == "no_reply"


def test_no_anchor_exposes_last_message_id():
    state = derive([user("u1"), user("u2")], server_status="busy")
    assert state.last_message_id == "u2"


def test_retry_with_assistant_error_stays_running():
    state = derive(
        [user("u1"), assistant("a1", error={"name": "APIError"})],
        server_status="retry",
        turn_anchor_id="u1",
    )
    assert state.status == "RUNNING"


def test_older_assistant_error_does_not_fail_the_new_turn():
    state = derive(
        [assistant("old", error={"name": "APIError"}), user("new")],
        turn_anchor_id="old-user",
        server_status="idle",
        turn_started_at=NOW - timedelta(seconds=2),
    )
    # Anchor is missing, so the historical error is not this round.
    assert state.status == "RUNNING"


def test_missing_anchor_converges_after_grace_without_inventing_success():
    state = derive(
        [assistant("old")],
        turn_anchor_id="msg_missing",
        server_status="idle",
        turn_started_at=NOW - timedelta(minutes=5),
    )
    assert state.status == "INTERRUPTED"
    assert state.terminal_reason == "unconfirmed"


def test_new_user_after_old_error_is_not_failed_before_grace():
    state = derive(
        [user("u1"), assistant("a1", error={"name": "APIError"}), user("u2")],
        turn_anchor_id="u1",
        server_status="idle",
        turn_started_at=NOW - timedelta(seconds=2),
    )
    assert state.status == "RUNNING"


def test_aged_new_user_after_old_error_is_no_reply():
    state = derive(
        [user("u1"), assistant("a1", error={"name": "APIError"}), user("u2")],
        turn_anchor_id="u1",
        turn_started_at=NOW - timedelta(minutes=5),
    )
    assert state.status == "FAILED"
    assert state.terminal_reason == "no_reply"