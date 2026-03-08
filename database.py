import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pexa_tracker.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
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
        );

        CREATE INDEX IF NOT EXISTS idx_matter_number ON notifications(matter_number);
        CREATE INDEX IF NOT EXISTS idx_status ON notifications(status);
        CREATE INDEX IF NOT EXISTS idx_category ON notifications(category);
        CREATE INDEX IF NOT EXISTS idx_received_at ON notifications(received_at);
    """)

    # Migration: add message_from column if it doesn't exist (for existing DBs)
    try:
        conn.execute("ALTER TABLE notifications ADD COLUMN message_from TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass  # Column already exists

    # Migration: add action_token column if it doesn't exist
    try:
        conn.execute("ALTER TABLE notifications ADD COLUMN action_token TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass  # Column already exists

    # Backfill message_from for existing records that have it in full_body
    _backfill_message_from(conn)

    conn.commit()
    conn.close()


def _backfill_message_from(conn):
    """Extract message_from from full_body for existing records that don't have it set."""
    import re
    rows = conn.execute(
        "SELECT id, full_body, summary FROM notifications WHERE (message_from IS NULL OR message_from = '')"
    ).fetchall()
    for row in rows:
        text = row["full_body"] or row["summary"] or ""
        sender = _extract_from_party(text)
        if sender:
            conn.execute("UPDATE notifications SET message_from = ? WHERE id = ?", (sender, row["id"]))


def _extract_from_party(text):
    """Extract the 'from' party name from notification text."""
    import re
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


def insert_notification(data):
    """Insert a new notification. Returns True if inserted, False if duplicate."""
    conn = get_db()
    try:
        conn.execute("""
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
        conn.close()


def get_notifications(filters=None):
    """Get notifications with optional filters."""
    conn = get_db()
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

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_notification(notification_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_notification_status(notification_id, status, user=None, notes=None):
    conn = get_db()
    now = datetime.utcnow().isoformat()

    updates = ["status = ?", "updated_at = ?"]
    params = [status, now]

    if user:
        updates.append("actioned_by = ?")
        params.append(user)
    if status in ("actioned", "reviewed"):
        updates.append("actioned_at = ?")
        params.append(now)
    if notes is not None:
        updates.append("notes = ?")
        params.append(notes)

    params.append(notification_id)
    conn.execute(f"UPDATE notifications SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    conn.close()


def update_notification_assignment(notification_id, assigned_to):
    conn = get_db()
    conn.execute(
        "UPDATE notifications SET assigned_to = ?, updated_at = ? WHERE id = ?",
        (assigned_to, datetime.utcnow().isoformat(), notification_id)
    )
    conn.commit()
    conn.close()


def add_note(notification_id, note_text, user):
    conn = get_db()
    existing = conn.execute("SELECT notes FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    if existing:
        current_notes = existing["notes"] or ""
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
        new_note = f"[{timestamp} - {user}] {note_text}"
        updated_notes = f"{current_notes}\n{new_note}".strip()
        conn.execute(
            "UPDATE notifications SET notes = ?, updated_at = ? WHERE id = ?",
            (updated_notes, datetime.utcnow().isoformat(), notification_id)
        )
        conn.commit()
    conn.close()


def get_stats():
    conn = get_db()
    stats = {}
    for category in ("action_required", "review", "info"):
        row = conn.execute(
            "SELECT COUNT(*) as count FROM notifications WHERE category = ? AND status NOT IN ('actioned', 'dismissed')",
            (category,)
        ).fetchone()
        stats[category] = row["count"]

    row = conn.execute(
        "SELECT COUNT(*) as count FROM notifications WHERE status IN ('actioned', 'dismissed')"
    ).fetchone()
    stats["completed"] = row["count"]

    row = conn.execute(
        "SELECT COUNT(*) as count FROM notifications WHERE status = 'new'"
    ).fetchone()
    stats["new"] = row["count"]

    conn.close()
    return stats
