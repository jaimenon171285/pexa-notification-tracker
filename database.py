import os
import re
import sqlite3
from datetime import datetime, timedelta

# --- SQLite database path ---
# Use DB_PATH env var if set (for Render persistent disk), otherwise store next to app
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pexa_tracker.db"))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _fetchall(cur):
    """Return all rows as list of dicts."""
    return [dict(row) for row in cur.fetchall()]


def _fetchone(cur):
    """Return one row as dict or None."""
    row = cur.fetchone()
    return dict(row) if row else None


# --- Schema and migrations ---

def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id TEXT UNIQUE,
            received_at TEXT,
            subject TEXT,
            matter_number TEXT,
            settlement_date TEXT,
            workspace_number TEXT,
            workspace_status TEXT,
            notification_type TEXT,
            summary TEXT,
            sender TEXT,
            full_body TEXT,
            category TEXT DEFAULT 'info',
            status TEXT DEFAULT 'new',
            assigned_to TEXT,
            notes TEXT DEFAULT '',
            actioned_by TEXT,
            actioned_at TEXT,
            message_from TEXT DEFAULT '',
            action_token TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Indexes
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_matter_number ON notifications(matter_number)",
        "CREATE INDEX IF NOT EXISTS idx_status ON notifications(status)",
        "CREATE INDEX IF NOT EXISTS idx_category ON notifications(category)",
        "CREATE INDEX IF NOT EXISTS idx_received_at ON notifications(received_at)",
    ]:
        cur.execute(idx_sql)

    # Migrations: add columns if they don't exist
    for col_sql in [
        "ALTER TABLE notifications ADD COLUMN message_from TEXT DEFAULT ''",
        "ALTER TABLE notifications ADD COLUMN action_token TEXT DEFAULT ''",
        "ALTER TABLE notifications ADD COLUMN emailed_to TEXT DEFAULT ''",
        "ALTER TABLE notifications ADD COLUMN emailed_at TEXT DEFAULT ''",
        "ALTER TABLE notifications ADD COLUMN reminder_sent INTEGER DEFAULT 0",
    ]:
        try:
            cur.execute(col_sql)
        except Exception:
            pass

    conn.commit()

    # Backfill NULL reminder_sent values to 0
    cur.execute("UPDATE notifications SET reminder_sent = 0 WHERE reminder_sent IS NULL")

    # Backfill message_from for existing records
    _backfill_message_from(conn)

    # Fix bad settlement dates (text instead of actual dates)
    _fix_bad_settlement_dates(conn)

    conn.commit()
    cur.close()
    conn.close()


def _backfill_message_from(conn):
    """Extract message_from from full_body for existing records that don't have it set."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, full_body, summary FROM notifications WHERE (message_from IS NULL OR message_from = '')"
    )
    rows = _fetchall(cur)
    for row in rows:
        text = row["full_body"] or row["summary"] or ""
        sender = _extract_from_party(text)
        if sender:
            cur.execute("UPDATE notifications SET message_from = ? WHERE id = ?", (sender, row["id"]))
    cur.close()


def _extract_from_party(text):
    """Extract the 'from' party name from notification text."""
    if not text:
        return ""

    # Pattern 1: "New message from COMPANY NAME"
    match = re.search(r"(?:new\s+)?message\s+from\s+(.+?)(?:\n|:|subject|$)", text, re.IGNORECASE)
    if match:
        name = match.group(1).strip().rstrip('.')
        if len(name) > 2 and name.upper() != "PEXA":
            return name

    # Pattern 2: "conversation message received" followed by sender info
    match = re.search(r"from\s+([A-Z][A-Z\s&.,()]+(?:PTY\s+LTD|LIMITED|LTD|CORP|BANK|INC)?)", text)
    if match:
        name = match.group(1).strip().rstrip('.')
        if len(name) > 2 and name.upper() != "PEXA":
            return name

    return ""


def _fix_bad_settlement_dates(conn):
    """Re-parse settlement dates for records where the value doesn't look like a date."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, full_body, settlement_date FROM notifications WHERE settlement_date IS NOT NULL AND settlement_date != ''"
    )
    rows = _fetchall(cur)
    fixed = 0
    for row in rows:
        sd = row["settlement_date"] or ""
        # Check if settlement_date looks like a valid date (contains dd/mm/yyyy)
        if re.search(r"\d{1,2}/\d{1,2}/\d{4}", sd):
            continue  # Already a valid date

        # Try to re-extract from full_body
        body = row["full_body"] or ""
        match = re.search(
            r"SETTLEMENT\s+DATE\s*(?:&|AND)\s*TIME\s*\n\s*(\d{1,2}/\d{1,2}/\d{4}[^\n]*)",
            body, re.IGNORECASE
        )
        if not match:
            match = re.search(
                r"SETTLEMENT\s+DATE\s*(?:&|AND)\s*TIME\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4}[^\n]*)",
                body, re.IGNORECASE
            )
        new_sd = match.group(1).strip() if match else None
        cur.execute(
            "UPDATE notifications SET settlement_date = ? WHERE id = ?",
            (new_sd, row["id"])
        )
        fixed += 1
    if fixed:
        conn.commit()
    cur.close()


# --- CRUD operations ---

def insert_notification(data):
    """Insert a new notification. Returns True if inserted, False if duplicate."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO notifications (
                email_id, received_at, subject, matter_number,
                settlement_date, workspace_number, workspace_status,
                notification_type, summary, sender, full_body, category,
                message_from
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["email_id"], data["received_at"], data["subject"],
            data["matter_number"], data["settlement_date"],
            data["workspace_number"], data["workspace_status"],
            data["notification_type"], data["summary"],
            data["sender"], data["full_body"], data["category"],
            data.get("message_from", "")
        ))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        cur.close()
        conn.close()


def get_notifications(filters=None):
    """Get notifications with optional filters."""
    conn = get_db()
    cur = conn.cursor()
    query = "SELECT * FROM notifications WHERE 1=1"
    params = []

    if filters:
        if filters.get("hide_closed"):
            query += " AND status NOT IN ('dismissed', 'reviewed', 'actioned')"
        if filters.get("category"):
            query += " AND category = ?"
            params.append(filters["category"])
        if filters.get("status"):
            query += " AND status = ?"
            params.append(filters["status"])
        if filters.get("matter_number"):
            query += " AND matter_number LIKE ?"
            params.append(f"%{filters['matter_number']}%")
        if filters.get("search"):
            query += " AND (summary LIKE ? OR subject LIKE ? OR notification_type LIKE ?)"
            term = f"%{filters['search']}%"
            params.extend([term, term, term])
        if filters.get("date_from"):
            query += " AND received_at >= ?"
            params.append(filters["date_from"])
        if filters.get("date_to"):
            query += " AND received_at <= ?"
            params.append(filters["date_to"])

    query += " ORDER BY received_at DESC"

    if filters and filters.get("limit"):
        query += " LIMIT ?"
        params.append(filters["limit"])

    cur.execute(query, params)
    rows = _fetchall(cur)
    cur.close()
    conn.close()
    return rows


def get_notification(notification_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,))
    row = _fetchone(cur)
    cur.close()
    conn.close()
    return row


def update_notification_status(notification_id, status, user=None, notes=None):
    conn = get_db()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()

    updates = ["status = ?", "updated_at = ?"]
    params = [status, now]

    if user:
        updates.append("actioned_by = ?")
        params.append(user)
    if status in ("actioned", "reviewed"):
        updates.append("actioned_at = ?")
        params.append(now)
    if status == "new":
        # Clear actioned fields when reopening
        updates.append("actioned_by = ?")
        params.append(None)
        updates.append("actioned_at = ?")
        params.append(None)
    if notes is not None:
        updates.append("notes = ?")
        params.append(notes)

    params.append(notification_id)
    cur.execute(f"UPDATE notifications SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    cur.close()
    conn.close()


def update_notification_assignment(notification_id, assigned_to):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE notifications SET assigned_to = ?, updated_at = ? WHERE id = ?",
        (assigned_to, datetime.utcnow().isoformat(), notification_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def add_note(notification_id, note_text, user):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT notes FROM notifications WHERE id = ?", (notification_id,))
    existing = _fetchone(cur)
    if existing:
        current_notes = existing["notes"] or ""
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
        new_note = f"[{timestamp} - {user}] {note_text}"
        updated_notes = f"{current_notes}\n{new_note}".strip()
        cur.execute(
            "UPDATE notifications SET notes = ?, updated_at = ? WHERE id = ?",
            (updated_notes, datetime.utcnow().isoformat(), notification_id)
        )
        conn.commit()
    cur.close()
    conn.close()


def get_notification_count():
    """Return total number of notifications in the database."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as count FROM notifications")
    row = _fetchone(cur)
    cur.close()
    conn.close()
    return row["count"]


def update_emailed_info(notification_id, emailed_to, emailed_at):
    """Record that a task email was sent for this notification."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE notifications SET emailed_to = ?, emailed_at = ?, reminder_sent = 0, updated_at = ? WHERE id = ?",
        (emailed_to, emailed_at, datetime.utcnow().isoformat(), notification_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def get_overdue_emailed_tasks(hours=24):
    """Get notifications that were emailed but not actioned within the given hours.
    Only returns tasks where reminder_sent is 0 or NULL (haven't been reminded yet)."""
    conn = get_db()
    cur = conn.cursor()
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    cur.execute("""
        SELECT * FROM notifications
        WHERE emailed_to != '' AND emailed_to IS NOT NULL
          AND emailed_at != '' AND emailed_at IS NOT NULL
          AND emailed_at < ?
          AND status NOT IN ('actioned', 'dismissed')
          AND (reminder_sent = 0 OR reminder_sent IS NULL)
    """, (cutoff,))
    rows = _fetchall(cur)
    cur.close()
    conn.close()
    return rows


def mark_reminder_sent(notification_id):
    """Mark that a reminder has been sent for this notification."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE notifications SET reminder_sent = 1, updated_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), notification_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def get_all_reviewed_emailed_tasks():
    """Get all notifications in 'reviewed' status that have been emailed to someone."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM notifications
        WHERE emailed_to != '' AND emailed_to IS NOT NULL
          AND status = 'reviewed'
    """)
    rows = _fetchall(cur)
    cur.close()
    conn.close()
    return rows


def get_all_unremindered_emailed_tasks():
    """Get all reviewed+emailed notifications where reminder_sent = 0 (regardless of timing)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM notifications
        WHERE emailed_to != '' AND emailed_to IS NOT NULL
          AND emailed_at != '' AND emailed_at IS NOT NULL
          AND status NOT IN ('actioned', 'dismissed')
          AND (reminder_sent = 0 OR reminder_sent IS NULL)
    """)
    rows = _fetchall(cur)
    cur.close()
    conn.close()
    return rows


def get_auto_push_eligible():
    """Return all 'new' notifications, oldest first.
    Used by the hourly auto-push job; further filtering (by settlement-date
    cap) happens in app.py."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM notifications
        WHERE status = 'new'
        ORDER BY received_at ASC
    """)
    rows = _fetchall(cur)
    cur.close()
    conn.close()
    return rows


def reset_reminder_sent(notification_id):
    """Reset reminder_sent to 0 so a reminder can be sent again."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE notifications SET reminder_sent = 0, updated_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), notification_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def get_stats():
    conn = get_db()
    cur = conn.cursor()
    stats = {}

    for status in ("new", "reviewed", "actioned", "dismissed"):
        cur.execute(
            "SELECT COUNT(*) as count FROM notifications WHERE status = ?",
            (status,)
        )
        row = _fetchone(cur)
        stats[status] = row["count"]

    cur.close()
    conn.close()
    return stats
