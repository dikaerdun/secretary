from datetime import datetime

import pytest

from secretary.agenda import render_agenda


def ts(value):
    return datetime.fromisoformat(value).timestamp()


def task(task_id, start, duration=30, title=None, deadline=None):
    return {
        "id": task_id,
        "title": title or f"事项 {task_id}",
        "remind_at": None if start is None else ts(start),
        "duration_minutes": duration,
        "deadline_at": None if deadline is None else ts(deadline),
    }


def test_day_uses_shanghai_midnight_even_when_now_is_utc():
    tasks = [
        task(1, "2026-09-30T15:30:00+00:00"),  # Ends exactly at Shanghai midnight.
        task(2, "2026-09-30T16:00:00+00:00"),
        task(3, "2026-10-01T16:00:00+00:00"),
    ]
    result = render_agenda(tasks, "day", ts("2026-09-30T16:05:00+00:00"))
    assert "今天安排｜2026-10-01" in result
    assert "任务#2" in result
    assert "任务#1" not in result and "任务#3" not in result
    assert "00:00–00:30" in result


def test_week_starts_monday_and_handles_year_boundary():
    tasks = [
        task(1, "2026-12-27T23:00:00+08:00"),
        task(2, "2026-12-28T00:00:00+08:00"),
        task(3, "2027-01-03T23:30:00+08:00"),
        task(4, "2027-01-04T00:00:00+08:00"),
    ]
    result = render_agenda(tasks, "week", ts("2027-01-03T12:00:00+08:00"))
    assert "2026-12-28 至 2027-01-03" in result
    assert "任务#2" in result and "任务#3" in result
    assert "任务#1" not in result and "任务#4" not in result
    monday = render_agenda(tasks, "week", ts("2027-01-03T16:00:00+00:00"))
    assert "任务#4" in monday and "任务#3" not in monday


@pytest.mark.parametrize("now,inside,outside,expected", [
    ("2026-12-31T23:59:00+08:00", "2026-12-31T23:30:00+08:00", "2027-01-01T00:00:00+08:00", "2026-12-01 至 2026-12-31"),
    ("2028-02-29T12:00:00+08:00", "2028-02-29T23:30:00+08:00", "2028-03-01T00:00:00+08:00", "2028-02-01 至 2028-02-29"),
    ("2026-04-30T12:00:00+08:00", "2026-04-30T23:30:00+08:00", "2026-05-01T00:00:00+08:00", "2026-04-01 至 2026-04-30"),
])
def test_month_windows_include_last_day_and_exclude_next_month(now, inside, outside, expected):
    result = render_agenda([task(1, inside), task(2, outside)], "month", ts(now))
    assert expected in result and "任务#1" in result and "任务#2" not in result


def test_cross_midnight_events_overlap_window_and_total_counts_only_intersection():
    tasks = [
        task(1, "2026-09-30T23:30:00+08:00", 60),
        task(2, "2026-10-01T23:30:00+08:00", 120),
        task(3, "2026-09-30T22:00:00+08:00", 120),
    ]
    result = render_agenda(tasks, "day", ts("2026-10-01T12:00:00+08:00"))
    assert "共 2 项，本时段预计 1 小时" in result
    assert "2026-09-30 周三" in result and "2026-10-01 周四" in result
    assert "23:30–00:30（结束于 2026-10-01） 任务#1" in result
    assert "23:30–01:30（结束于 2026-10-02） 任务#2" in result
    assert "任务#3" not in result
    assert "任务#1 事项 1｜待反馈" in result
    assert "任务#2 事项 2｜待反馈" not in result


def test_event_spanning_entire_month_is_included_but_only_month_duration_counts():
    result = render_agenda(
        [task(1, "2026-09-30T00:00:00+08:00", 33 * 24 * 60)],
        "month", ts("2026-10-15T12:00:00+08:00"),
    )
    assert "共 1 项，本时段预计 744 小时" in result
    assert "2026-09-30 周三" in result


def test_sorting_grouping_pending_feedback_and_deadline():
    tasks = [
        task(3, "2026-10-02T09:00:00+08:00"),
        task(2, "2026-10-01T10:00:00+08:00", deadline="2026-10-03T17:00:00+08:00"),
        task(1, "2026-10-01T10:00:00+08:00", 120),
    ]
    result = render_agenda(tasks, "month", ts("2026-10-01T10:30:00+08:00"))
    assert result.index("任务#1") < result.index("任务#2") < result.index("任务#3")
    assert result.count("2026-10-01 周四") == 1
    assert "任务#1 事项 1｜待反馈" not in result
    assert "任务#2 事项 2｜待反馈｜截止 2026-10-03 17:00" in result
    assert "已完成 0 项，未完成 3 项" in result
    assert "事项 1｜已完成" not in result
    assert "事项 2｜已完成" not in result
    assert "事项 3｜已完成" not in result


@pytest.mark.parametrize("period", ["day", "week", "month"])
def test_completed_tasks_remain_in_calendar_history_without_waiting_feedback(period):
    completed = {**task(1, "2026-10-01T09:00:00+08:00", 60), "status": "completed"}
    result = render_agenda([completed], period, ts("2026-10-01T12:00:00+08:00"))
    assert "09:00–10:00 任务#1 事项 1｜已完成" in result
    assert "共 1 项，本时段预计 1 小时" in result
    assert "已完成 1 项，未完成 0 项" in result
    assert "待反馈" not in result
    assert "暂没有已确认安排" not in result


def test_cancelled_tasks_do_not_count_toward_agenda_or_estimated_duration():
    tasks = [
        {**task(1, "2026-10-01T09:00:00+08:00", 60), "status": "completed"},
        {**task(2, "2026-10-01T10:00:00+08:00", 60), "status": "pending"},
        {**task(3, "2026-10-01T11:00:00+08:00", 60), "status": "cancelled"},
        task(4, "2026-10-01T14:00:00+08:00", 30),  # Missing status stays pending.
    ]
    result = render_agenda(tasks, "day", ts("2026-10-01T12:00:00+08:00"))
    assert "任务#3" not in result
    assert "共 3 项，本时段预计 2 小时 30 分钟" in result
    assert "已完成 1 项，未完成 2 项" in result
    assert "任务#1 事项 1｜已完成" in result
    assert "任务#2 事项 2｜待反馈" in result
    assert "任务#4 事项 4｜待反馈" not in result
    assert render_agenda(tasks[2:3], "day", ts("2026-10-01T12:00:00+08:00")) == (
        "今天暂没有已确认安排。未确认事项请发送“待确认”。"
    )


def test_pagination_completion_counts_cover_all_pages_and_exclude_cancelled():
    tasks = [
        {**task(i, "2026-10-01T10:00:00+08:00"), "status": "completed" if i <= 20 else "pending"}
        for i in range(1, 22)
    ]
    tasks.append({**task(22, "2026-10-01T10:00:00+08:00"), "status": "cancelled"})
    result = render_agenda(tasks, "day", ts("2026-10-01T12:00:00+08:00"), 2)
    assert "共 21 项" in result and "第 2/2 页" in result
    assert "已完成 20 项，未完成 1 项" in result
    assert result.count("任务#") == 1
    assert "任务#21 事项 21｜待反馈" in result


@pytest.mark.parametrize("period,command", [("day", "今天安排"), ("week", "本周安排"), ("month", "本月安排")])
def test_twenty_item_pagination_retains_summary_and_concrete_command(period, command):
    tasks = [task(i, "2026-10-01T10:00:00+08:00") for i in range(1, 22)]
    now = ts("2026-10-01T09:00:00+08:00")
    first = render_agenda(tasks, period, now)
    second = render_agenda(tasks, period, now, 2)
    assert first.count("任务#") == 20 and second.count("任务#") == 1
    assert "共 21 项，本时段预计 10 小时 30 分钟" in first
    assert "共 21 项，本时段预计 10 小时 30 分钟" in second
    assert f"下一页：发送“{command} 2”" in first
    assert "下一页" not in second
    assert "第 1/2 页" in first and "第 2/2 页" in second
    assert "任务#21" in second
    assert f"只有 2 页，请发送“{command} 2”" in render_agenda(tasks, period, now, 3)


def test_page_stays_below_stream_byte_limit_with_long_unicode_titles_and_dates():
    tasks = [task(i, "2026-09-30T23:00:00+08:00", 48 * 60, "😀" * 1000,
                  "2027-01-01T17:00:00+08:00") for i in range(1, 22)]
    result = render_agenda(tasks, "day", ts("2026-10-01T09:00:00+08:00"))
    assert result.count("任务#") == 20
    assert len(result.encode("utf-8")) < 16384
    assert "…" in result


@pytest.mark.parametrize("period,label", [("day", "今天"), ("week", "本周"), ("month", "本月")])
def test_empty_and_unscheduled_items_offer_pending_confirmation(period, label):
    assert render_agenda([task(1, None)], period, ts("2026-10-01T09:00:00+08:00")) == (
        f"{label}暂没有已确认安排。未确认事项请发送“待确认”。"
    )


def test_duration_defaults_to_thirty_minutes_and_title_newlines_are_flattened():
    item = task(1, "2026-10-01T10:00:00+08:00", title="整理\n  本周\t事项")
    del item["duration_minutes"]
    result = render_agenda([item], "day", ts("2026-10-01T09:00:00+08:00"))
    assert "本时段预计 30 分钟" in result
    assert "10:00–10:30 任务#1 整理 本周 事项" in result


@pytest.mark.parametrize("page", [0, -1, True, 1.0, "1", None])
def test_invalid_page_is_rejected(page):
    with pytest.raises(ValueError, match="页码"):
        render_agenda([], "day", ts("2026-10-01T09:00:00+08:00"), page)


@pytest.mark.parametrize("now", [True, float("nan"), float("inf"), "tomorrow", 10**400])
def test_invalid_now_is_rejected(now):
    with pytest.raises(ValueError):
        render_agenda([], "day", now)


@pytest.mark.parametrize("period", ["year", "", None, []])
def test_invalid_period_is_rejected(period):
    with pytest.raises(ValueError, match="范围"):
        render_agenda([], period, ts("2026-10-01T09:00:00+08:00"))


@pytest.mark.parametrize("duration", [0, -30, True, float("inf")])
def test_invalid_duration_is_rejected(duration):
    with pytest.raises(ValueError, match="时长"):
        render_agenda([task(1, "2026-10-01T10:00:00+08:00", duration)], "day",
                      ts("2026-10-01T09:00:00+08:00"))
