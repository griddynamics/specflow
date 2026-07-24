"""ResumeGeneration semantics — the resume-loop budget contract, verbatim.

| Attempt outcome              | Effect                                          |
|------------------------------|-------------------------------------------------|
| validator complete           | stop (loop-level; not a budget concern)         |
| clean but validator incomplete | validator_resumes += 1; crash schedule reset  |
| crash WITH saved progress    | schedule reset to start -> next wait = 1 min    |
| crash WITHOUT progress       | schedule advances -> waits 1/3/5/10/30/60 min   |

No total cap: progress keeps resetting the schedule indefinitely.
"""

from datetime import timedelta

from app.services.claude_code import (
    _CRASH_BACKOFF_MINUTES,
    RESUME_PROGRESS_MIN_TOOL_USES,
    ResumeGeneration,
)


class TestCrashSchedule:
    def test_every_crash_waits_at_least_a_minute(self):
        r = ResumeGeneration()
        wait = r.record_crash(tool_uses=999, head_advanced=True)
        assert wait == timedelta(minutes=1)

    def test_zero_progress_crashes_follow_the_exact_schedule_then_exhaust(self):
        r = ResumeGeneration()
        waits = [r.record_crash(tool_uses=0, head_advanced=False) for _ in _CRASH_BACKOFF_MINUTES]
        assert waits == [timedelta(minutes=m) for m in _CRASH_BACKOFF_MINUTES]
        # The next consecutive zero-progress crash exhausts the schedule.
        assert r.record_crash(tool_uses=0, head_advanced=False) is None

    def test_progress_resets_the_schedule(self):
        r = ResumeGeneration()
        r.record_crash(tool_uses=0, head_advanced=False)  # 1m
        r.record_crash(tool_uses=0, head_advanced=False)  # 3m
        # Saved progress (commit) -> streak reset, wait back to 1 minute.
        assert r.record_crash(tool_uses=0, head_advanced=True) == timedelta(minutes=1)
        # Next zero-progress crash starts the schedule over.
        assert r.record_crash(tool_uses=0, head_advanced=False) == timedelta(minutes=1)

    def test_no_total_cap_progress_crashes_never_exhaust(self):
        r = ResumeGeneration()
        for _ in range(50):
            assert r.record_crash(tool_uses=RESUME_PROGRESS_MIN_TOOL_USES, head_advanced=False) is not None

    def test_progress_definition_commit_or_tool_use_floor(self):
        r = ResumeGeneration()
        assert r.saved_progress(tool_uses=0, head_advanced=True) is True
        assert r.saved_progress(tool_uses=RESUME_PROGRESS_MIN_TOOL_USES, head_advanced=False) is True
        assert r.saved_progress(tool_uses=RESUME_PROGRESS_MIN_TOOL_USES - 1, head_advanced=False) is False


class TestValidatorBudget:
    def test_two_resumes_allowed_third_denied(self):
        r = ResumeGeneration()
        assert r.record_incomplete() is True   # schedule resume 1
        assert r.record_incomplete() is True   # schedule resume 2
        assert r.record_incomplete() is False  # a third resume is over budget

    def test_clean_incomplete_run_resets_crash_streak(self):
        r = ResumeGeneration()
        r.record_crash(tool_uses=0, head_advanced=False)
        r.record_crash(tool_uses=0, head_advanced=False)
        r.record_incomplete()  # a clean run proves the connection is alive
        assert r.crash_streak == 0
        assert r.record_crash(tool_uses=0, head_advanced=False) == timedelta(minutes=1)


class TestDescribe:
    def test_describe_matches_counters(self):
        r = ResumeGeneration()
        r.record_crash(tool_uses=0, head_advanced=False)
        r.record_crash(tool_uses=0, head_advanced=False)
        r.record_incomplete()
        assert r.describe() == "crash backoff 0/6, validator resumes 1/2"

    def test_backoff_label_tracks_streak(self):
        r = ResumeGeneration()
        assert r.crash_backoff_label() == "0/6"
        r.record_crash(tool_uses=0, head_advanced=False)
        assert r.crash_backoff_label() == "1/6"
        for _ in range(10):
            r.record_crash(tool_uses=0, head_advanced=False)
        assert r.crash_backoff_label() == "6/6"
