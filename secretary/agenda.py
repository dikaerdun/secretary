"""Render owner-filtered, confirmed tasks and completion history in Shanghai time."""

from __future__ import annotations

from datetime import datetime, timedelta
import math
from typing import Literal
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
PAGE_SIZE = 20
MAX_TITLE_LENGTH = 120
_LABELS = {"day": "今天", "week": "本周", "month": "本月"}
_WEEKDAYS = "一二三四五六日"


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}必须是有效数字。")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"{field}超出支持范围。") from None
    if not math.isfinite(result):
        raise ValueError(f"{field}必须是有效数字。")
    return result


def _datetime(timestamp: object, field: str) -> datetime:
    timestamp = _number(timestamp, field)
    try:
        return datetime.fromtimestamp(timestamp, SHANGHAI)
    except (ValueError, OverflowError, OSError):
        raise ValueError(f"{field}超出支持范围。") from None


def _window(period: str, now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        if period == "day":
            return start, start + timedelta(days=1)
        if period == "week":
            start -= timedelta(days=start.weekday())
            return start, start + timedelta(days=7)
        start = start.replace(day=1)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        return start, end
    except (ValueError, OverflowError):
        raise ValueError("日程日期超出支持范围。") from None


def _duration_text(seconds: float) -> str:
    # Round once after summing intersections; do not round each task upward.
    minutes = seconds / 60
    if math.isclose(minutes, round(minutes), abs_tol=1e-7):
        minutes = round(minutes)
    if minutes < 1:
        return "不足 1 分钟"
    if isinstance(minutes, int):
        hours, remainder = divmod(minutes, 60)
        if hours and remainder:
            return f"{hours} 小时 {remainder} 分钟"
        return f"{hours} 小时" if hours else f"{remainder} 分钟"
    return f"{minutes:.1f} 分钟"


def render_agenda(
    tasks: list[dict],
    period: Literal["day", "week", "month"],
    now: float,
    page: int = 1,
) -> str:
    """Render confirmed tasks supplied by an owner-isolated caller.

    Unscheduled tasks do not occupy calendar time. Intervals are half-open:
    an event ending at midnight does not also appear in the following day.
    Pending and completed tasks remain visible; cancelled tasks are omitted.
    Missing status means pending for compatibility with existing callers.
    Completion is read from status, never inferred from elapsed time.
    Summaries cover the entire selected period, even on paginated replies.
    Invalid argument types/ranges raise ValueError; a positive page beyond
    the result set returns a useful page command instead of an empty reply.
    This function does not fetch tasks, proposals, or infer completion.
    """
    if not isinstance(period, str) or period not in _LABELS:
        raise ValueError("日程范围必须为 day、week 或 month。")
    if type(page) is not int or page < 1:
        raise ValueError("页码必须是从 1 开始的整数。")
    current = _datetime(now, "当前时间")
    window_start, window_end = _window(period, current)
    start_timestamp, end_timestamp = window_start.timestamp(), window_end.timestamp()
    label = _LABELS[period]
    entries = []
    total_seconds = 0.0

    for task in tasks:
        status = task.get("status", "pending")
        if status not in ("pending", "completed"):
            continue
        if task.get("remind_at") is None:
            continue
        start = _datetime(task["remind_at"], "安排时间")
        duration = task.get("duration_minutes")
        duration = 30 if duration is None else _number(duration, "预计时长")
        if duration <= 0:
            raise ValueError("预计时长必须大于 0。")
        task_start = start.timestamp()
        task_end = task_start + duration * 60
        end = _datetime(task_end, "结束时间")
        intersection = min(task_end, end_timestamp) - max(task_start, start_timestamp)
        if intersection <= 0:
            continue
        task_id = task.get("id")
        if type(task_id) is not int or not 0 < task_id <= 2**63 - 1:
            raise ValueError("任务编号必须是有效的正整数。")
        title = task.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("事项标题不能为空。")
        title = " ".join(title.split())
        if len(title) > MAX_TITLE_LENGTH:
            title = title[: MAX_TITLE_LENGTH - 1] + "…"
        deadline = task.get("deadline_at")
        deadline = None if deadline is None else _datetime(deadline, "截止时间")
        entries.append((start, end, task_id, title, deadline, status))
        total_seconds += intersection

    if not entries:
        return f"{label}暂没有已确认安排。未确认事项请发送“待确认”。"

    entries.sort(key=lambda entry: (entry[0], entry[2]))
    pages = (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE
    if page > pages:
        return f"{label}安排只有 {pages} 页，请发送“{label}安排 {pages}”。"
    final_date = (window_end - timedelta(days=1)).date()
    date_range = str(window_start.date())
    if final_date != window_start.date():
        date_range += f" 至 {final_date}"
    completed_count = sum(entry[5] == "completed" for entry in entries)
    lines = [
        f"{label}安排｜{date_range}（北京时间）",
        f"共 {len(entries)} 项，本时段预计 {_duration_text(total_seconds)}"
        f"｜已完成 {completed_count} 项，未完成 {len(entries) - completed_count} 项｜第 {page}/{pages} 页",
    ]
    previous_date = None
    for start, end, task_id, title, deadline, status in entries[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]:
        if start.date() != previous_date:
            lines.append(f"\n{start:%Y-%m-%d} 周{_WEEKDAYS[start.weekday()]}")
            previous_date = start.date()
        time_text = f"{start:%H:%M}–{end:%H:%M}"
        if end.date() != start.date():
            time_text += f"（结束于 {end:%Y-%m-%d}）"
        line = f"{time_text} 任务#{task_id} {title}"
        if status == "completed":
            line += "｜已完成"
        elif end <= current:
            line += "｜待反馈"
        if deadline is not None:
            line += f"｜截止 {deadline:%Y-%m-%d %H:%M}"
        lines.append(line)
    if page < pages:
        lines.append(f"\n下一页：发送“{label}安排 {page + 1}”。")
    return "\n".join(lines)
