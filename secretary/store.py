"""Durable, owner-isolated tasks and an at-least-once reminder outbox.

A delivery that succeeds immediately before a crash or a lost acknowledgement
can be repeated after its lease expires. Callers must serialize task changes
with the claim/send/ack sequence in a single running gateway to avoid sending
a reminder concurrently with its cancellation or rescheduling.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import math
import json
from pathlib import Path
import sqlite3
import threading
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_NOTICE = object()


try:
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    # Windows does not necessarily include the IANA database. China currently
    # uses UTC+08 year-round; this fallback needs no third-party dependency.
    SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("时间必须是 Unix 时间戳")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError("时间超出支持范围") from exc
    if not math.isfinite(value):
        raise ValueError("时间必须是有效值")
    try:
        datetime.fromtimestamp(value, SHANGHAI)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("时间超出支持范围") from exc
    return value


def _time_text(value):
    if value is None:
        return "待安排"
    return datetime.fromtimestamp(value, SHANGHAI).strftime("%Y-%m-%d %H:%M（北京时间）")


def _task_text(task):
    return f"#{task['id']} {task['title']}｜提醒：{_time_text(task['remind_at'])}"


def _duration(value):
    if type(value) is not int or not 1 <= value <= 10080:
        raise ValueError("预计用时应为 1 至 10080 分钟的整数")
    return value


def _proposal_text(proposal):
    if 'change_kind' in proposal.keys() and proposal['change_kind'] == 'cancel':
        return f"P{proposal['id']} 拟取消原提醒：#{proposal['target_task_id']} {proposal['title']}（确认后生效）"
    when = _time_text(proposal["remind_at"]) if proposal["remind_at"] is not None else "待补时间"
    result = (
        f"P{proposal['id']} {proposal['title']}｜建议时间：{when}\n"
        f"预计用时：{proposal['duration_minutes']} 分钟（未指定时默认按 30 分钟估计，可修改）"
    )
    if proposal["deadline_at"] is not None:
        result += f"\n截止时间：{_time_text(proposal['deadline_at'])}"
    return result


class Store:
    """SQLite storage. Each public operation is atomic and safe across threads."""

    LEASE_SECONDS = 120
    RETRY_BASE_SECONDS = 30
    RETRY_MAX_SECONDS = 3600

    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                title TEXT NOT NULL,
                remind_at REAL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'cancelled')),
                revision INTEGER NOT NULL DEFAULT 1,
                duration_minutes INTEGER NOT NULL DEFAULT 30,
                deadline_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tasks_owner_status ON tasks(owner, status);
            CREATE TABLE IF NOT EXISTS command_results (
                owner TEXT NOT NULL,
                source_id TEXT NOT NULL,
                reply TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(owner, source_id)
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                task_revision INTEGER NOT NULL,
                due_at REAL NOT NULL,
                available_at REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'leased', 'sent', 'obsolete')),
                token TEXT,
                lease_until REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                sent_at REAL,
                UNIQUE(task_id, task_revision)
            );
            CREATE INDEX IF NOT EXISTS notifications_due
                ON notifications(status, available_at, lease_until);
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                title TEXT NOT NULL,
                remind_at REAL,
                duration_minutes INTEGER NOT NULL DEFAULT 30,
                deadline_at REAL,
                schedule_note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'rejected')),
                task_id INTEGER REFERENCES tasks(id),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS proposals_owner_status ON proposals(owner, status);
        """)
        # Upgrade the local pre-confirmation MVP without deleting existing data.
        # Inspect and ALTER inside one writer transaction, including on reopen.
        with self._transaction() as db:
            columns = {column["name"] for column in db.execute("PRAGMA table_info(tasks)")}
            if "duration_minutes" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 30")
            if "deadline_at" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN deadline_at REAL")
            columns = {column["name"] for column in db.execute("PRAGMA table_info(proposals)")}
            for name, kind in (("target_task_id", "INTEGER"), ("expected_task_revision", "INTEGER"),
                               ("change_kind", "TEXT NOT NULL DEFAULT 'schedule'"),
                               ("execution_notice_json", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE proposals ADD COLUMN {name} {kind}")

    def close(self):
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def get_result(self, owner: str, source_id: str) -> str | None:
        """Look up the original reply before running a potentially costly parser."""
        with self._lock:
            row = self._db.execute(
                "SELECT reply FROM command_results WHERE owner = ? AND source_id = ?",
                (owner, source_id),
            ).fetchone()
            return row["reply"] if row else None

    def execute(self, owner: str, source_id: str, command: dict, now: float) -> str:
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner 不能为空")
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id 不能为空")
        now = _timestamp(now)
        with self._transaction() as db:
            previous = db.execute(
                "SELECT reply FROM command_results WHERE owner = ? AND source_id = ?",
                (owner, source_id),
            ).fetchone()
            if previous:
                return previous["reply"]
            reply = self._execute(db, owner, command, now)
            db.execute(
                "INSERT INTO command_results(owner, source_id, reply, created_at) VALUES (?, ?, ?, ?)",
                (owner, source_id, reply, now),
            )
            return reply

    def _execute(self, db, owner, command, now):
        if not isinstance(command, dict):
            return "未识别到有效指令，请说“帮助”查看用法。"
        action = command.get("action")
        if action == "help":
            return (
                "随口说一件事，我会整理为待确认提案；没有明确时间时由你补充，不自动安排。\n"
                "例如“准备产品方案”，再说“把提案 P1 改到明天下午三点”；"
                "发送“确认 P1”后才启用提醒，或说“取消提案 P1”。\n"
                "说“待确认”查看提案；说“今天安排”“本周安排”“本月安排”查看已确认日程。\n"
                "说“待办”查看任务编号与提醒时间（翻页用“待办 2”）；说“完成 1”“取消 1”，"
                "或“把任务1推迟到明天下午三点”。时间均按北京时间。"
            )
        if action == "agenda":
            period = command.get("period", "day")
            page = command.get("page", 1)
            if period not in ("day", "week", "month"):
                return "请说“今天安排”“本周安排”或“本月安排”。"
            if type(page) is not int or not 1 <= page <= 1000000:
                return "请提供有效页码，例如“本周安排 2”。"
            from .agenda import render_agenda

            tasks = db.execute(
                "SELECT * FROM tasks WHERE owner = ? AND status IN ('pending', 'completed') "
                "AND remind_at IS NOT NULL ORDER BY remind_at, id", (owner,)
            ).fetchall()
            return render_agenda([dict(task) for task in tasks], period, now, page=page)
        if action in ("propose", "propose_change", "propose_cancel", "confirm", "reject", "reschedule_proposal", "proposals"):
            return self._execute_proposal(db, owner, command, now)
        if action == "list":
            page = command.get("page", 1)
            if type(page) is not int or page < 1 or page > 1000000:
                return "请提供有效页码，例如“待办 2”。"
            total = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE owner = ? AND status = 'pending'", (owner,)
            ).fetchone()[0]
            if not total:
                return "当前没有未完成事项。"
            pages = (total + 19) // 20
            if page > pages:
                return f"待办清单共 {pages} 页，请发送“待办 {pages}”。"
            tasks = db.execute(
                "SELECT * FROM tasks WHERE owner = ? AND status = 'pending' "
                "ORDER BY remind_at IS NULL, remind_at, id LIMIT 20 OFFSET ?", (owner, (page - 1) * 20)
            ).fetchall()
            next_page = f"\n发送“待办 {page + 1}”查看下一页。" if page < pages else ""
            return f"未完成事项（第 {page}/{pages} 页，共 {total} 项）：\n" + "\n".join(map(_task_text, tasks)) + next_page
        if action == "create":
            title = command.get("title")
            if not isinstance(title, str) or not title.strip():
                return "请告诉我要记录的事项。"
            remind_at = command.get("remind_at")
            if remind_at is not None:
                try:
                    remind_at = _timestamp(remind_at)
                except ValueError:
                    return "提醒时间无效，请提供具体日期和时间。"
            task_id = db.execute(
                "INSERT INTO tasks(owner, title, remind_at, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (owner, title.strip(), remind_at, now, now),
            ).lastrowid
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if remind_at is not None:
                self._schedule(db, task)
            return "已记录：" + _task_text(task)
        if action not in ("complete", "cancel", "snooze"):
            return "未识别到有效指令，请说“帮助”查看用法。"

        task_id = command.get("task_id")
        if isinstance(task_id, bool) or not isinstance(task_id, int) or not 0 < task_id <= 2**63 - 1:
            return "请提供有效的任务编号，例如“完成 1”。"
        # Do not reveal another owner's task existence, title, or status.
        task = db.execute("SELECT * FROM tasks WHERE id = ? AND owner = ?", (task_id, owner)).fetchone()
        if task is None:
            return f"未找到你的任务 #{task_id}，请说“待办”查看任务编号与时间。"
        if task["status"] != "pending":
            state = "已完成" if task["status"] == "completed" else "已取消"
            return f"该事项{state}：" + _task_text(task)

        remind_at = task["remind_at"]
        if action == "snooze":
            try:
                remind_at = _timestamp(command.get("remind_at"))
            except ValueError:
                return f"任务 #{task_id} 的新提醒时间无效，请提供具体日期和时间。"
            candidate = dict(task)
            candidate['remind_at'] = remind_at
            problem = self._proposal_problem(db, candidate, now, exclude_task_id=task_id)
            if problem:
                return f"任务 #{task_id} 未调整：{problem}"
        status = {"complete": "completed", "cancel": "cancelled", "snooze": "pending"}[action]
        db.execute(
            "UPDATE tasks SET status = ?, remind_at = ?, revision = revision + 1, updated_at = ? "
            "WHERE id = ? AND owner = ?", (status, remind_at, now, task_id, owner)
        )
        db.execute(
            "UPDATE notifications SET status = 'obsolete', token = NULL, lease_until = NULL "
            "WHERE task_id = ? AND status IN ('queued', 'leased')", (task_id,)
        )
        db.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE owner=? "
                   "AND target_task_id=? AND status='pending'", (now, owner, task_id))
        task = db.execute("SELECT * FROM tasks WHERE id = ? AND owner = ?", (task_id, owner)).fetchone()
        if action == "snooze":
            self._schedule(db, task)
        prefix = {"complete": "已完成：", "cancel": "已取消：", "snooze": "已调整提醒："}[action]
        return prefix + _task_text(task)

    def _execute_proposal(self, db, owner, command, now):
        action = command["action"]
        if action == "proposals":
            page = command.get("page", 1)
            if type(page) is not int or not 1 <= page <= 1000000:
                return "请提供有效页码，例如“待确认 2”。"
            total = db.execute(
                "SELECT count(*) FROM proposals WHERE owner = ? AND status = 'pending'", (owner,)
            ).fetchone()[0]
            if not total:
                return "当前没有待确认提案。"
            pages = (total + 19) // 20
            if page > pages:
                return f"待确认提案共 {pages} 页，请发送“待确认 {pages}”。"
            rows = db.execute(
                "SELECT * FROM proposals WHERE owner = ? AND status = 'pending' "
                "ORDER BY id LIMIT 20 OFFSET ?", (owner, (page - 1) * 20)
            ).fetchall()
            next_page = f"\n发送“待确认 {page + 1}”查看下一页。" if page < pages else ""
            return (
                f"待确认提案（第 {page}/{pages} 页，共 {total} 项；尚未启用提醒）：\n"
                + "\n\n".join(_proposal_text(row) for row in rows)
                + "\n补时间示例：“把提案 P1 改到明天下午三点”；确认用“确认 P1”，取消用“取消提案 P1”。"
                + next_page
            )
        if action in ("propose", "propose_change", "propose_cancel"):
            title = command.get("title")
            if not isinstance(title, str) or not title.strip():
                return "请告诉我要整理的事项。"
            if len(title.strip()) > 120:
                return "事项标题请控制在 120 字以内。"
            try:
                remind_at = None if command.get("remind_at") is None else _timestamp(command["remind_at"])
                deadline_at = None if command.get("deadline_at") is None else _timestamp(command["deadline_at"])
                duration_minutes = _duration(30 if command.get("duration_minutes") is None else command["duration_minutes"])
            except ValueError as exc:
                return f"提案未保存：{exc}。"
            notice_json = None
            if 'execution_notice_at' in command:
                try:
                    notice_at = command['execution_notice_at']
                    if notice_at is not None:
                        notice_at = _timestamp(notice_at)
                        if remind_at is not None and notice_at > remind_at:
                            raise ValueError('提前提醒不能晚于执行开始')
                    notice_json = json.dumps(notice_at, allow_nan=False)
                except ValueError as exc:
                    return f"提案未保存：{exc}。"
            note = command.get("schedule_note") or ""
            if not isinstance(note, str):
                return "提案说明格式无效，请重新描述事项。"
            target = None
            if action in ('propose_change','propose_cancel'):
                target_id = command.get('task_id')
                if type(target_id) is not int or not 0 < target_id <= 2**63 - 1:
                    return '请提供有效的任务编号。'
                target = db.execute("SELECT * FROM tasks WHERE owner=? AND id=? AND status='pending'",
                                    (owner, target_id)).fetchone()
                if target is None:
                    return '这条提醒已完成或取消，请刷新后重新安排。'
                if 'deadline_at' not in command:
                    deadline_at = target['deadline_at']
                if action == 'propose_cancel':
                    remind_at, duration_minutes = target['remind_at'], target['duration_minutes']
                db.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE owner=? "
                           "AND target_task_id=? AND status='pending'", (now, owner, target_id))
            proposal_id = db.execute(
                "INSERT INTO proposals(owner, title, remind_at, duration_minutes, deadline_at, schedule_note, "
                "status, created_at, updated_at,target_task_id,expected_task_revision,change_kind,execution_notice_json) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (owner, title.strip(), remind_at, duration_minutes, deadline_at, note.strip()[:500], now, now,
                 target['id'] if target else None, target['revision'] if target else None,
                 'cancel' if action=='propose_cancel' else 'schedule', notice_json),
            ).lastrowid
            proposal = db.execute("SELECT * FROM proposals WHERE id = ? AND owner = ?", (proposal_id, owner)).fetchone()
            if action == 'propose_cancel':
                return '已拟取消，待你确认：' + _proposal_text(proposal)
            return self._review_proposal(db, proposal, now)

        proposal_id = command.get("proposal_id")
        if type(proposal_id) is not int or not 0 < proposal_id <= 2**63 - 1:
            return "请提供有效提案编号，例如“确认 P1”。"
        proposal = db.execute(
            "SELECT * FROM proposals WHERE id = ? AND owner = ?", (proposal_id, owner)
        ).fetchone()
        if proposal is None:
            return f"未找到你的提案 P{proposal_id}，请说“待确认”查看提案编号。"
        if proposal["status"] == "rejected":
            return f"提案已取消：" + _proposal_text(proposal)
        if proposal["status"] == "confirmed":
            task = db.execute(
                "SELECT * FROM tasks WHERE id = ? AND owner = ?", (proposal["task_id"], owner)
            ).fetchone()
            return (
                f"提案 P{proposal_id} 已确认，不会重复建立任务：" + _task_text(task)
                + f"\n如需修改，请使用任务编号 #{task['id']}。"
            )
        if action == "reject":
            db.execute(
                "UPDATE proposals SET status = 'rejected', updated_at = ? WHERE id = ? AND owner = ?",
                (now, proposal_id, owner),
            )
            return "提案已取消，未启用提醒：" + _proposal_text(proposal)
        if action == "reschedule_proposal":
            if proposal['change_kind'] == 'cancel':
                return '这是待确认取消，请先撤回取消，再拟定新安排。'
            if "remind_at" not in command and "duration_minutes" not in command:
                return f"请补充提案 P{proposal_id} 的具体时间或预计用时。"
            try:
                remind_at = _timestamp(command["remind_at"]) if "remind_at" in command else proposal["remind_at"]
                duration_minutes = _duration(command["duration_minutes"]) if "duration_minutes" in command else proposal["duration_minutes"]
            except ValueError as exc:
                return f"提案 P{proposal_id} 未修改：{exc}。"
            db.execute(
                "UPDATE proposals SET remind_at = ?, duration_minutes = ?, updated_at = ? "
                "WHERE id = ? AND owner = ?", (remind_at, duration_minutes, now, proposal_id, owner),
            )
            if 'execution_notice_at' in command:
                notice_at = command['execution_notice_at']
                if notice_at is not None:
                    notice_at = _timestamp(notice_at)
                    if notice_at > remind_at:
                        raise ValueError('提前提醒不能晚于执行开始。')
                db.execute('UPDATE proposals SET execution_notice_json=? WHERE owner=? AND id=?',
                           (json.dumps(notice_at, allow_nan=False), owner, proposal_id))
            proposal = db.execute("SELECT * FROM proposals WHERE id = ? AND owner = ?", (proposal_id, owner)).fetchone()
            return self._review_proposal(db, proposal, now)

        if self._record_done(db, owner, proposal_id):
            db.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE owner=? AND id=?",
                       (now, owner, proposal_id))
            return f'提案 P{proposal_id} 对应事项已完成，不能再启用提醒。'
        target = None
        if proposal['target_task_id']:
            target = db.execute('SELECT * FROM tasks WHERE owner=? AND id=?',
                                (owner, proposal['target_task_id'])).fetchone()
            if (not target or target['status'] != 'pending'
                    or target['revision'] != proposal['expected_task_revision']):
                db.execute("UPDATE proposals SET status='rejected',updated_at=? WHERE owner=? AND id=?",
                           (now, owner, proposal_id))
                return f'提案 P{proposal_id} 的原提醒已有变化，请重新拟定修改。'
        if proposal['change_kind'] == 'cancel' and target:
            db.execute("UPDATE tasks SET status='cancelled',revision=revision+1,updated_at=? WHERE owner=? AND id=?",
                       (now,owner,target['id']))
            db.execute("UPDATE notifications SET status='obsolete',token=NULL,lease_until=NULL "
                       "WHERE task_id=? AND status IN ('queued','leased')", (target['id'],))
            db.execute("UPDATE proposals SET status='confirmed',task_id=?,updated_at=? WHERE owner=? AND id=?",
                       (target['id'],now,owner,proposal_id))
            return f"提案 P{proposal_id} 已确认，原提醒 #{target['id']} 已取消。"
        problem = self._proposal_problem(db, proposal, now, exclude_task_id=proposal['target_task_id'])
        if problem:
            return f"提案 P{proposal_id} 尚未确认：{problem}\n" + self._proposal_instructions(proposal_id)
        if target:
            task_id = target['id']
            db.execute('UPDATE tasks SET title=?,remind_at=?,duration_minutes=?,deadline_at=?,revision=revision+1,'
                       'updated_at=? WHERE owner=? AND id=?',
                       (proposal['title'], proposal['remind_at'], proposal['duration_minutes'],
                        proposal['deadline_at'], now, owner, task_id))
            db.execute("UPDATE notifications SET status='obsolete',token=NULL,lease_until=NULL "
                       "WHERE task_id=? AND status IN ('queued','leased')", (task_id,))
        else:
            task_id = db.execute(
                "INSERT INTO tasks(owner, title, remind_at, duration_minutes, deadline_at, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (owner, proposal["title"], proposal["remind_at"], proposal["duration_minutes"], proposal["deadline_at"], now, now),
            ).lastrowid
        task = db.execute("SELECT * FROM tasks WHERE id = ? AND owner = ?", (task_id, owner)).fetchone()
        notice_at = (_DEFAULT_NOTICE if proposal['execution_notice_json'] is None
                     else json.loads(proposal['execution_notice_json']))
        self._schedule(db, task, notice_at)
        db.execute(
            "UPDATE proposals SET status = 'confirmed', task_id = ?, updated_at = ? WHERE id = ? AND owner = ?",
            (task_id, now, proposal_id, owner),
        )
        if target and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crm_record_proposals'").fetchone():
            db.execute('UPDATE crm_records SET title=?,updated_at=? WHERE owner=? AND id IN '
                       '(SELECT record_id FROM crm_record_proposals WHERE owner=? AND proposal_id=?)',
                       (proposal['title'], now, owner, owner, proposal_id))
        return f"提案 P{proposal_id} 已确认，" + ('日程已加入，未启用提前提醒：' if notice_at is None else '提醒已启用：') + _task_text(task)

    @staticmethod
    def _record_done(db, owner, proposal_id):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crm_record_proposals'").fetchone():
            return False
        return bool(db.execute("SELECT 1 FROM crm_records r JOIN crm_record_proposals p "
                               "ON r.id=p.record_id AND r.owner=p.owner WHERE p.owner=? "
                               "AND p.proposal_id=? AND r.status='done' LIMIT 1", (owner, proposal_id)).fetchone())

    @staticmethod
    def _proposal_instructions(proposal_id):
        return (
            f"发送“确认 P{proposal_id}”后才启用提醒，或发送“取消提案 P{proposal_id}”。\n"
            f"补充或修改时间示例：“把提案 P{proposal_id} 改到明天下午三点”。"
        )

    def _review_proposal(self, db, proposal, now):
        result = "已整理，待你确认：" + _proposal_text(proposal)
        if proposal["schedule_note"]:
            result += "\n整理说明：" + proposal["schedule_note"]
        if proposal['target_task_id']:
            result += f"\n将修改原提醒 #{proposal['target_task_id']}；确认前原提醒继续有效。"
        problem = self._proposal_problem(db, proposal, now, exclude_task_id=proposal['target_task_id'])
        if problem:
            result += "\n" + problem
        if proposal["remind_at"] is not None:
            start = proposal["remind_at"]
            end = start + proposal["duration_minutes"] * 60
            overlaps = db.execute(
                "SELECT id FROM proposals WHERE owner = ? AND status = 'pending' AND id != ? "
                "AND remind_at < ? AND remind_at + duration_minutes * 60 > ? LIMIT 10",
                (proposal["owner"], proposal["id"], end, start),
            ).fetchall()
            if overlaps:
                references = "、".join(f"P{row['id']}" for row in overlaps)
                result += f"\n与待确认提案 {references} 时间重叠；提案尚未占用正式日程，确认时会重新检查已确认事项。"
        return result + "\n" + self._proposal_instructions(proposal["id"])

    def _proposal_problem(self, db, proposal, now, *, exclude_task_id=None):
        start = proposal["remind_at"]
        if start is None:
            return "待补时间：请先补充明确的提醒时间，再确认；我不会自行安排时间。"
        if start <= now:
            return "提醒时间已过，请先修改为未来时间。"
        end = start + proposal["duration_minutes"] * 60
        if proposal["deadline_at"] is not None and end > proposal["deadline_at"]:
            return "按预计用时无法在截止时间前完成，请调整开始时间或预计用时后再确认。"
        conflicts = db.execute(
            "SELECT '#' || id AS ref FROM tasks WHERE owner = ? AND status = 'pending' "
            "AND remind_at < ? AND remind_at + duration_minutes * 60 > ? AND id != ? LIMIT 10",
            (proposal["owner"], end, start, exclude_task_id or -1),
        ).fetchall()
        if conflicts:
            references = "、".join(row["ref"] for row in conflicts)
            return f"与事项 {references} 时间冲突（按预计用时检查），请调整时间或预计用时后再确认。"
        return None

    @staticmethod
    def _schedule(db, task, notice_at=_DEFAULT_NOTICE):
        if notice_at is None:
            return
        due_at = task['remind_at'] if notice_at is _DEFAULT_NOTICE else _timestamp(notice_at)
        db.execute(
            "INSERT INTO notifications(task_id, task_revision, due_at, available_at, status) "
            "VALUES (?, ?, ?, ?, 'queued')",
            (task["id"], task["revision"], due_at, due_at),
        )

    def claim_due(self, now: float) -> dict | None:
        """Claim one valid due reminder, including an expired delivery lease."""
        now = _timestamp(now)
        with self._transaction() as db:
            row = db.execute(
                "SELECT n.id AS notification_id, t.* FROM notifications n "
                "JOIN tasks t ON t.id = n.task_id AND t.revision = n.task_revision "
                "WHERE t.status = 'pending' AND n.due_at <= ? AND "
                "((n.status = 'queued' AND n.available_at <= ?) OR "
                "(n.status = 'leased' AND n.lease_until <= ?)) "
                "ORDER BY n.due_at, n.id LIMIT 1", (now, now, now)
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE notifications SET status = 'leased', token = ?, lease_until = ?, "
                "attempts = attempts + 1 WHERE id = ?",
                (token, now + self.LEASE_SECONDS, row["notification_id"]),
            )
            context = ''
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='crm_record_proposals'").fetchone():
                record = db.execute('SELECT r.id,c.name,c.contact FROM crm_record_proposals rp '
                    'JOIN proposals p ON p.owner=rp.owner AND p.id=rp.proposal_id '
                    'JOIN crm_records r ON r.owner=rp.owner AND r.id=rp.record_id '
                    'LEFT JOIN crm_customers c ON c.owner=r.owner AND c.id=r.customer_id '
                    'WHERE rp.owner=? AND COALESCE(p.task_id,p.target_task_id)=? ORDER BY rp.proposal_id DESC LIMIT 1',
                    (row['owner'], row['id'])).fetchone()
                if record:
                    context = f"\n来源记录：#{record['id']}"
                    if record['name']:
                        context += '\n客户：' + record['name']
                    if record['contact']:
                        context += '\n联系人：' + record['contact']
            return {
                "id": row["notification_id"], "owner": row["owner"],
                "text": "事项提醒：" + _task_text(row) + context, "token": token,
            }

    def ack(self, notification_id, token, now):
        """Acknowledge only the current lease of a still-valid reminder."""
        now = _timestamp(now)
        with self._transaction() as db:
            result = db.execute(
                "UPDATE notifications SET status = 'sent', token = NULL, lease_until = NULL, sent_at = ? "
                "WHERE id = ? AND token = ? AND status = 'leased' AND EXISTS "
                "(SELECT 1 FROM tasks t WHERE t.id = notifications.task_id "
                "AND t.revision = notifications.task_revision AND t.status = 'pending')",
                (now, notification_id, token),
            )
            return result.rowcount == 1

    def retry(self, notification_id, token, now):
        """Release a failed delivery for retry with capped exponential backoff."""
        now = _timestamp(now)
        with self._transaction() as db:
            row = db.execute(
                "SELECT n.attempts FROM notifications n JOIN tasks t ON t.id = n.task_id "
                "AND t.revision = n.task_revision WHERE n.id = ? AND n.token = ? "
                "AND n.status = 'leased' AND t.status = 'pending'", (notification_id, token)
            ).fetchone()
            if row is None:
                return False
            delay = min(self.RETRY_MAX_SECONDS, self.RETRY_BASE_SECONDS * 2 ** min(row["attempts"] - 1, 7))
            db.execute(
                "UPDATE notifications SET status = 'queued', token = NULL, lease_until = NULL, available_at = ? "
                "WHERE id = ? AND token = ?", (now + delay, notification_id, token)
            )
            return True
