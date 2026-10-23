"""定时巩固按上次完成的时间排下一次:重启不会立刻再跑一遍全量巩固。"""

from datetime import datetime, timedelta, timezone

from app.routers.schedule import ScheduleState

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def test_never_run_is_due_now():
    assert ScheduleState().due_in(NOW) == 0


def test_waits_until_interval_after_last_run():
    s = ScheduleState(interval=6 * 3600)
    s.last_run = (NOW - timedelta(hours=1)).isoformat()
    assert s.due_in(NOW) == 5 * 3600                 # 1 小时前刚跑完:再等 5 小时
    s.last_run = (NOW - timedelta(hours=7)).isoformat()
    assert s.due_in(NOW) < 0                         # 已经超过 6 小时:该跑了


def test_naive_or_bad_timestamps():
    s = ScheduleState(interval=3600)
    s.last_run = (NOW - timedelta(minutes=30)).replace(tzinfo=None).isoformat()
    assert s.due_in(NOW) == 1800                     # 不带时区的按 UTC
    s.last_run = "not-a-time"
    assert s.due_in(NOW) == 0
