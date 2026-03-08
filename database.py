import os
import re
from datetime import datetime

# --- Dual-mode database: PostgreSQL (Render) or SQLite (local dev) ---

DATABASE_URL = os.getenv("DATABASE_URL")

# Render provides "postgres://" but psycopg2 requires "postgresql://"
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

USE_PG = DATABASE_URL is not None

if USE_PG:
    import psycopg2
    import psycopg2.extras
    DBIntegrityError = psycopg2.IntegrityError
else:
    import sqlite3
    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pexa_tracker.db")
    DBIntegrityError = sqlite3.IntegrityError


# --- Connection and helpers ---

def get_db():
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def _cursor(conn):
    """Return a cursor that produces dict-like rows."""
    if USE_PG:
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        return conn.cursor()


def _param(sql):
    """Convert ? placeholders to %s for PostgreSQL."""
    if USE_PG:
        return sql.replace("?", "%s")
    return sql


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
    cur = _cursor(conn)

    if USE_PG:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
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
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
    else:
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

    # Indexes (syntax identical for both)
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_matter_number ON notifications(matter_number)",
        "CREATE INDEX IF NOT EXISTS idx_status ON notifications(status)",
        "CREATE INDEX IF NOT EXISTS idx_category ON notifications(category)",
        "CREATE INDEX IF NOT EXISTS idx_received_at ON notifications(received_at)",
    ]:
        cur.execute(idx_sql)

    # Migrations: add columns if they don't exist
    if USE_PG:
        cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS message_from TEXT DEFAULT ''")
        cur.execute("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS action_token TEXT DEFAULT ''")
    else:
        try:
            cur.execute("ALTER TABLE notifications ADD COLUMN message_from TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE notifications ADD COLUMN action_token TEXT DEFAULT ''")
        except Exception:
            pass

    conn.commit()

    # Backfill message_from for existing records
    _backfill_message_from(conn)

    conn.commit()
    cur.close()
    conn.close()


def _backfill_message_from(conn):
    """Extract message_from from full_body for existing records that don't have it set."""
    cur = _cursor(conn)
    cur.execute(
        "SELECT id, full_body, summary FROM notifications WHERE (message_from IS NULL OR message_from = '')"
    )
    rows = _fetchall(cur)
    for row in rows:
        text = row["full_body"] or row["summary"] or ""
        sender = _extract_from_party(text)
        if sender:
            cur.execute(_param("UPDATE notifications SET message_from = ? WHERE id = ?"), (sender, row["id"]))
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


# --- CRUD operations ---

def insert_notification(data):
    """Insert a new notification. Returns True if inserted, False if duplicate."""
    conn = get_db()
    cur = _cursor(conn)
    try:
        cur.execute(_param("""
            INSERT INTO notifications (
                email_id, received_at, subject, matter_number,
                settlement_date, workspace_number, workspace_status,
                notification_type, summary, sender, full_body, category,
                message_from
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """), (
            data["email_id"], data["received_at"], data["subject"],
            data["matter_number"], data["settlement_date"],
            data["workspace_number"], data["workspace_status"],
            data["notification_type"], data["summary"],
            data["sender"], data["full_body"], data["category"],
            data.get("message_from", "")
        ))
        conn.commit()
        return True
    except DBIntegrityError:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


def get_notifications(filters=None):
    """Get notifications with optional filters."""
    conn = get_db()
    cur = _cursor(conn)
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

    cur.execute(_param(query), params)
    rows = _fetchall(cur)
    cur.close()
    conn.close()
    return rows


def get_notification(notification_id):
    conn = get_db()
    cur = _cursor(conn)
    cur.execute(_param("SELECT * FROM notifications WHERE id = ?"), (notification_id,))
    row = _fetchone(cur)
    cur.close()
    conn.close()
    return row


def update_notification_status(notification_id, status, user=None, notes=None):
    conn = get_db()
    cur = _cursor(conn)
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
    cur.execute(_param(f"UPDATE notifications SET {', '.join(updates)} WHERE id = ?"), params)
    conn.commit()
    cur.close()
    conn.close()


def update_notification_assignment(notification_id, assigned_to):
    conn = get_db()
    cur = _cursor(conn)
    cur.execute(
        _param("UPDATE notifications SET assigned_to = ?, updated_at = ? WHERE id = ?"),
        (assigned_to, datetime.utcnow().isoformat(), notification_id)
    )
    conn.commit()
    cur.close()
    conn.close()


def add_note(notification_id, note_text, user):
    conn = get_db()
    cur = _cursor(conn)
    cur.execute(_param("SELECT notes FROM notifications WHERE id = ?"), (notification_id,))
    existing = _fetchone(cur)
    if existing:
        current_notes = existing["notes"] or ""
        timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
        new_note = f"[{timestamp} - {user}] {note_text}"
        updated_notes = f"{current_notes}\n{new_note}".strip()
        cur.execute(
            _param("UPDATE notifications SET notes = ?, updated_at = ? WHERE id = ?"),
            (updated_notes, datetime.utcnow().isoformat(), notification_id)
        )
        conn.commit()
    cur.close()
    conn.close()


def get_stats():
    conn = get_db()
    cur = _cursor(conn)
    stats = {}

    for category in ("action_required", "review", "info"):
        cur.execute(
            _param("SELECT COUNT(*) as count FROM notifications WHERE category = ? AND status NOT IN ('actioned', 'dismissed')"),
            (category,)
        )
        row = _fetchone(cur)
        stats[category] = row["count"]

    cur.execute(
        "SELECT COUNT(*) as count FROM notifications WHERE status IN ('actioned', 'dismissed')"
    )
    row = _fetchone(cur)
    stats["completed"] = row["count"]

    cur.execute(
        "SELECT COUNT(*) as count FROM notifications WHERE status = 'new'"
    )
    row = _fetchone(cur)
    stats["new"] = row["count"]

    cur.close()
    conn.close()
    return stats
