import os
import hmac
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, request, make_response
from apscheduler.schedulers.background import BackgroundScheduler

from database import init_db, get_notifications, get_notification, update_notification_status, \
    update_notification_assignment, add_note, get_stats, insert_notification, \
    update_emailed_info, get_overdue_emailed_tasks, mark_reminder_sent, \
    get_all_reviewed_emailed_tasks, reset_reminder_sent, get_auto_push_eligible
from email_parser import parse_pexa_email
from graph_client import GraphClient
from workspace_creator import WorkspaceCreator

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

graph_client = GraphClient()
workspace_creator = WorkspaceCreator(graph_client)
last_sync_time = None
last_sync_status = "Never synced"
last_workspace_sync_time = None
last_workspace_sync_status = "Never synced"


def _now_sydney():
    """Get current time in Sydney timezone."""
    return datetime.now(SYDNEY_TZ)


def sync_emails():
    """Fetch new emails from Graph API, parse them, and store in database.
    After processing, moves emails to a 'Processed' subfolder to avoid duplicates.
    Always fetches ALL emails from the folder (no date filter) because processed
    emails are moved to the Processed subfolder, so only unprocessed emails remain."""
    global last_sync_time, last_sync_status
    try:
        # Always fetch ALL emails from the folder - no date filter needed.
        # Processed emails are moved to the "Processed" subfolder after import,
        # so only unprocessed emails remain in the main folder.
        logger.info("Fetching all emails from PEXA folder...")
        emails = graph_client.fetch_emails(since=None, max_results=100)

        new_count = 0
        moved_count = 0
        for email in emails:
            parsed = parse_pexa_email(
                email_id=email["id"],
                subject=email["subject"],
                body_html=email["body_html"],
                body_text=email["body_text"],
                received_at=email["received_at"],
                sender=email["sender_email"],
            )
            was_new = insert_notification(parsed)
            if was_new:
                new_count += 1

            # Move all processed emails (new or duplicate) to archive folder
            # This keeps the PEXA folder clean and prevents re-processing
            if graph_client.move_email_to_archive(email["id"]):
                moved_count += 1

        last_sync_time = _now_sydney()
        last_sync_status = f"OK - {new_count} new notifications"
        logger.info(f"Sync complete: {new_count} new notifications from {len(emails)} emails, {moved_count} moved to Processed")
        return new_count

    except Exception as e:
        last_sync_status = f"Error: {str(e)}"
        logger.error(f"Sync failed: {e}")
        return -1


def sync_workspaces():
    """Check the Post Exchange Automation folder for new Actionstep emails
    and auto-create portal workspaces for each one."""
    global last_workspace_sync_time, last_workspace_sync_status
    try:
        results = workspace_creator.sync()
        last_workspace_sync_time = _now_sydney()
        last_workspace_sync_status = (
            f"OK — {results['created']} created, {results['exists']} existed, "
            f"{results['errors']} errors"
        )
        return results
    except Exception as e:
        last_workspace_sync_status = f"Error: {str(e)}"
        logger.error(f"Workspace sync failed: {e}")
        return None


# --- Web Routes ---

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


# --- API Routes ---

@app.route("/api/notifications")
def api_notifications():
    filters = {
        "category": request.args.get("category"),
        "status": request.args.get("status"),
        "matter_number": request.args.get("matter"),
        "search": request.args.get("search"),
        "date_from": request.args.get("date_from"),
        "date_to": request.args.get("date_to"),
        "limit": request.args.get("limit", type=int),
        "hide_closed": request.args.get("hide_closed"),
    }
    # Remove None values
    filters = {k: v for k, v in filters.items() if v is not None}
    notifications = get_notifications(filters if filters else None)
    return jsonify(notifications)


@app.route("/api/notifications/<int:notification_id>")
def api_notification_detail(notification_id):
    notification = get_notification(notification_id)
    if notification:
        return jsonify(notification)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/notifications/<int:notification_id>/status", methods=["POST"])
def api_update_status(notification_id):
    data = request.json
    status = data.get("status")
    user = data.get("user", "Unknown")
    notes = data.get("notes")

    if status not in ("new", "reviewed", "actioned", "dismissed"):
        return jsonify({"error": "Invalid status"}), 400

    update_notification_status(notification_id, status, user, notes)
    return jsonify({"success": True})


@app.route("/api/notifications/<int:notification_id>/assign", methods=["POST"])
def api_assign(notification_id):
    data = request.json
    assigned_to = data.get("assigned_to")
    update_notification_assignment(notification_id, assigned_to)
    return jsonify({"success": True})


@app.route("/api/notifications/<int:notification_id>/note", methods=["POST"])
def api_add_note(notification_id):
    data = request.json
    note_text = data.get("note", "")
    user = data.get("user", "Unknown")
    if note_text:
        add_note(notification_id, note_text, user)
    return jsonify({"success": True})


@app.route("/api/stats")
def api_stats():
    stats = get_stats()
    stats["last_sync"] = last_sync_status
    stats["sync_interval_minutes"] = sync_interval
    if last_sync_time:
        stats["last_sync_time"] = last_sync_time.isoformat()
        next_sync = last_sync_time + timedelta(minutes=sync_interval)
        stats["next_sync_time"] = next_sync.isoformat()
    else:
        stats["last_sync_time"] = None
        stats["next_sync_time"] = None
    return jsonify(stats)


@app.route("/api/sync", methods=["POST"])
def api_sync():
    count = sync_emails()
    return jsonify({
        "success": count >= 0,
        "new_count": count,
        "status": last_sync_status,
    })


@app.route("/api/workspace-sync", methods=["POST"])
def api_workspace_sync():
    results = sync_workspaces()
    if results is None:
        return jsonify({"success": False, "status": last_workspace_sync_status})
    return jsonify({
        "success": True,
        "created": results["created"],
        "exists": results["exists"],
        "errors": results["errors"],
        "processed": results["processed"],
        "status": last_workspace_sync_status,
    })


@app.route("/api/debug-emails")
def api_debug_emails():
    """List emails in the Post Exchange Automation folder + search inbox/junk/all folders."""
    try:
        emails = workspace_creator.fetch_emails(max_results=20)
        inbox_hits = workspace_creator.search_inbox_for_actionstep(max_results=20)
        all_folder_search = workspace_creator.search_all_folders_for_actionstep(max_results=5)
        mailbox_search = workspace_creator.search_mailbox_for_actionstep(max_results=20)
        return jsonify({
            "success": True,
            "mailbox": workspace_creator.gc.mailbox,
            "folder_email_count": len(emails),
            "folder_emails": [
                {
                    "id": e["id"][:20] + "...",
                    "subject": e["subject"],
                    "has_body": bool(e.get("body_text") or e.get("body_html")),
                }
                for e in emails
            ],
            "inbox_recent": inbox_hits,
            "junk_deleted_archive": all_folder_search,
            "mailbox_search_actionstep": mailbox_search,
            "last_workspace_sync_status": last_workspace_sync_status,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/keepalive")
def api_keepalive():
    """GET endpoint pinged by UptimeRobot every 5 min.
    Runs both syncs so they work even if APScheduler jobs have died."""
    notif_count = sync_emails()
    ws_results   = sync_workspaces()
    return jsonify({
        "ok": True,
        "notifications": notif_count,
        "workspaces": ws_results or {},
        "workspace_status": last_workspace_sync_status,
    })


@app.route("/api/connection")
def api_connection():
    status = graph_client.test_connection()
    return jsonify(status)


def _settlement_date_only(settlement_date_str):
    """Extract just the date portion from a settlement date string.
    E.g. '14/04/2026 02:30 PM AEST' -> '14/04/2026'"""
    import re as _re
    if not settlement_date_str:
        return "N/A"
    match = _re.match(r"(\d{1,2}/\d{1,2}/\d{4})", settlement_date_str)
    return match.group(1) if match else settlement_date_str


def _extract_pexa_message(full_body, notification_type="", subject=""):
    """Extract the actual message content from a PEXA notification.

    For Workspace Update / Workspace Invitation / Signing Required / Loan Proceeds / Mortgage Activity
    and similar non-message types: use the subject line (stripped of matter number prefix).

    For New Message types: extract the message between 'Subject:' and 'SUBSCRIBER REF' in the body."""
    import re as _re

    # For non-"New Message" types, the subject line is more useful
    non_message_types = ("workspace update", "workspace invitation", "signing required",
                         "loan proceeds created", "mortgage activity", "workspace status change",
                         "financial settlement", "lodgement", "lodgment")
    if notification_type and notification_type.strip().lower() in non_message_types:
        if subject:
            # Strip the matter number prefix like "71688 PURCHASE - " or "71688 PURCHASE: "
            cleaned = _re.sub(r"^\d+\s+(?:PURCHASE|SALE)\s*[-:]\s*", "", subject, flags=_re.IGNORECASE).strip()
            if cleaned and len(cleaned) > 5:
                if len(cleaned) > 300:
                    cleaned = cleaned[:297] + "..."
                return cleaned
        # Fall back to summary/body first line
        if full_body:
            first_line = full_body.split("\n")[0].strip()
            if first_line and len(first_line) > 5:
                if len(first_line) > 300:
                    first_line = first_line[:297] + "..."
                return first_line
        return ""

    # For "New Message" type: extract between Subject: and SUBSCRIBER REF / Note:
    if not full_body:
        return ""
    lines = full_body.split("\n")
    subject_idx = -1
    end_idx = len(lines)

    for i, line in enumerate(lines):
        stripped = line.strip().lower()
        if stripped.startswith("subject:") and subject_idx == -1:
            subject_idx = i
        if subject_idx >= 0 and i > subject_idx:
            if stripped.startswith("note:") and "sensitive data" in stripped:
                end_idx = i
                break
            if _re.match(r"^subscriber\s+ref", stripped):
                end_idx = i
                break

    if subject_idx >= 0:
        msg_lines = lines[subject_idx + 1:end_idx]
        while msg_lines and not msg_lines[0].strip():
            msg_lines.pop(0)
        while msg_lines and not msg_lines[-1].strip():
            msg_lines.pop()
        message = " ".join(l.strip() for l in msg_lines if l.strip())
        if len(message) > 300:
            message = message[:297] + "..."
        return message
    return ""


def _is_settlement_today_or_tomorrow(settlement_date_str):
    """Check if the settlement date is today or tomorrow."""
    import re as _re
    if not settlement_date_str:
        return False
    match = _re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", settlement_date_str)
    if not match:
        return False
    try:
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        settlement = datetime(year, month, day, tzinfo=SYDNEY_TZ)
        now = _now_sydney()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        diff_days = (settlement - today).total_seconds() / 86400
        return 0 <= diff_days <= 1
    except Exception:
        return False


def _is_settlement_urgent(settlement_date_str):
    """Check if the settlement date is within 3 days from now.
    Parses Australian format: '14/04/2026 02:30 PM AEST' or just '14/04/2026'."""
    import re as _re
    if not settlement_date_str:
        return False
    match = _re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", settlement_date_str)
    if not match:
        return False
    try:
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        settlement = datetime(year, month, day, tzinfo=SYDNEY_TZ)
        now = _now_sydney()
        diff = (settlement - now).total_seconds() / 86400  # days
        return 0 <= diff <= 3
    except Exception:
        return False


def _build_html_email(message, done_link="", settlement_date=""):
    """Convert a plain-text task message into an HTML email.
    Inside the PEXA message block, only the actual message content (between Subject:
    and Note:/SUBSCRIBER REF) is shown in red. Everything else is normal black text."""
    import re as _re

    urgent = _is_settlement_urgent(settlement_date)

    # Split out the PEXA message section
    pexa_pattern = r"(--- Full PEXA Message ---\s*\n)(.*?)(\n\s*--- End of Message ---)"
    match = _re.search(pexa_pattern, message, _re.DOTALL)

    if match:
        before = message[:match.start()]
        pexa_header = match.group(1).strip()
        pexa_content = match.group(2).strip()
        pexa_footer = match.group(3).strip()
        after = message[match.end():]

        # Within the PEXA content, find the actual message (between Subject: line
        # and Note:/SUBSCRIBER REF) and colour only that part red
        pexa_lines = pexa_content.split("\n")
        subject_idx = -1
        end_idx = len(pexa_lines)
        for i, line in enumerate(pexa_lines):
            stripped = line.strip().lower()
            if stripped.startswith("subject:") and subject_idx == -1:
                subject_idx = i
            if subject_idx >= 0 and i > subject_idx:
                if stripped.startswith("note:") and "sensitive data" in stripped:
                    end_idx = i
                    break
                if _re.match(r"^subscriber\s+ref", stripped):
                    end_idx = i
                    break

        # Build PEXA content with red message section
        if subject_idx >= 0:
            before_msg = pexa_lines[:subject_idx + 1]  # Up to and including Subject:
            msg_lines = pexa_lines[subject_idx + 1:end_idx]
            after_msg = pexa_lines[end_idx:]

            # Strip leading/trailing blank lines from the message
            while msg_lines and not msg_lines[0].strip():
                msg_lines.pop(0)
            while msg_lines and not msg_lines[-1].strip():
                msg_lines.pop()

            def _esc(t):
                return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

            before_msg_html = "<br>\n".join(_esc(l) for l in before_msg)
            msg_html = "<br>\n".join(_esc(l) for l in msg_lines)
            after_msg_html = "<br>\n".join(_esc(l) for l in after_msg)

            pexa_content_html = f"""{before_msg_html}
<br>
<div style="color: #cc0000; padding: 8px 0; margin: 4px 0;">
{msg_html}
</div>
{after_msg_html}"""
        else:
            # Couldn't find Subject: line — show entire PEXA content normally
            pexa_content_html = pexa_content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>\n")

        # Escape the surrounding text
        before_html = before.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>\n")
        after_html = after.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>\n")

        body_html = f"""{before_html}
<br><b>{pexa_header}</b><br><br>
{pexa_content_html}
<br><b>{pexa_footer}</b><br>
{after_html}"""
    else:
        # No PEXA section found — just convert the whole message
        body_html = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>\n")

    # Add urgent settlement warning banner at top if within 3 days
    urgent_banner = ""
    if urgent:
        safe_date = (settlement_date or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        urgent_banner = f"""<div style="background: #cc0000; color: white; padding: 14px 20px; border-radius: 8px; margin-bottom: 16px; text-align: center; font-size: 16px; font-weight: bold;">
⚠️ URGENT — SETTLEMENT DATE IS WITHIN 3 DAYS: {safe_date} ⚠️
</div>
"""

    # Add the "Mark as Done" button if we have a link
    if done_link:
        body_html += f"""<br>
<hr style="border: none; border-top: 2px solid #333; margin: 20px 0;">
<div style="text-align: center; margin: 20px 0; padding: 16px; background: #f0fff4; border: 2px solid #27ae60; border-radius: 8px;">
    <p style="font-size: 18px; font-weight: bold; color: #1a7a3a;">✅ PLEASE MARK THIS TASK AS COMPLETE</p>
    <a href="{done_link}" style="display: inline-block; background: #27ae60; color: white; padding: 14px 32px; font-size: 16px; font-weight: bold; text-decoration: none; border-radius: 8px; margin: 8px 0;">Click Here When Done</a>
    <p style="color: #333; font-size: 14px; font-weight: bold; margin-top: 14px;">You must click the button above once you have completed this task.</p>
    <p style="color: #cc0000; font-size: 13px; font-weight: bold; margin-top: 8px;">⚠️ If this task is not marked as complete within 24 hours, a reminder email will be sent automatically.</p>
</div>
<hr style="border: none; border-top: 2px solid #333; margin: 20px 0;">"""

    # Wrap in a basic HTML template — always black text, no bold
    return f"""<html>
<body style="font-family: Arial, sans-serif; font-size: 14px; color: #333; line-height: 1.5;">
{urgent_banner}{body_html}
</body>
</html>"""


@app.route("/api/send-task", methods=["POST"])
def api_send_task():
    data = request.json
    notification_id = data.get("notification_id")
    to_email = data.get("to_email")  # Can be a single string or a list of strings
    subject = data.get("subject")
    message = data.get("message")
    from_user = data.get("from_user", "Unknown")

    # Normalise to_email to a list
    if isinstance(to_email, str):
        to_list = [e.strip() for e in to_email.split(",") if e.strip()]
    elif isinstance(to_email, list):
        to_list = [e.strip() for e in to_email if e.strip()]
    else:
        to_list = []

    if not to_list or not subject:
        return jsonify({"error": "At least one recipient and subject required"}), 400

    try:
        # Generate "Mark as Done" link if we have a notification ID
        done_link = ""
        settlement_date = ""
        if notification_id:
            token = generate_action_token(notification_id)
            base_url = request.host_url.rstrip("/")
            done_link = f"{base_url}/done/{notification_id}?token={token}"
            # Look up settlement date for urgent styling
            notif = get_notification(notification_id)
            if notif:
                settlement_date = notif.get("settlement_date", "") or ""

        # Build HTML email with PEXA message highlighted in bold red
        # If settlement is within 3 days, entire body will be red + bold
        html_body = _build_html_email(message, done_link, settlement_date=settlement_date)

        # Send via Graph API using the PEXA mailbox (HTML format)
        send_mailbox = os.getenv("SEND_FROM_MAILBOX", graph_client.mailbox)
        cc_address = os.getenv("CC_MAILBOX", "teams@legalworld.com.au")
        graph_client.send_email(to_list, subject, message, from_mailbox=send_mailbox, cc_emails=cc_address, body_html=html_body)

        # Add a note and auto-set status to "reviewed" (To Review)
        to_display = ", ".join(to_list)
        if notification_id:
            add_note(notification_id, f"Task emailed to {to_display} by {from_user} (with Mark as Done link)", from_user)
            update_notification_status(notification_id, "reviewed", from_user)
            # Track the first recipient for 24-hour reminder follow-up
            update_emailed_info(notification_id, to_display, datetime.utcnow().isoformat())

        logger.info(f"Task email sent to {to_display} for notification {notification_id} by {from_user} - status set to reviewed")
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Failed to send task email: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/bulk-action", methods=["POST"])
def api_bulk_action():
    data = request.json
    ids = data.get("ids", [])
    action = data.get("action")
    user = data.get("user", "Unknown")

    if action not in ("reviewed", "actioned", "dismissed", "new"):
        return jsonify({"error": "Invalid action"}), 400

    for nid in ids:
        update_notification_status(nid, action, user)

    return jsonify({"success": True, "count": len(ids)})


# --- Mark as Done (from email link) ---

def generate_action_token(notification_id):
    """Generate a secure token for the 'Mark as Done' email link."""
    secret = app.secret_key or "fallback-secret"
    msg = f"done-{notification_id}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:20]


def verify_action_token(notification_id, token):
    """Verify a 'Mark as Done' token."""
    expected = generate_action_token(notification_id)
    return hmac.compare_digest(expected, token)


@app.route("/done/<int:notification_id>")
def mark_done_from_email(notification_id):
    """Handle the 'Mark as Done' link from task emails."""
    token = request.args.get("token", "")

    if not verify_action_token(notification_id, token):
        return make_response("""
        <!DOCTYPE html>
        <html><head><title>Invalid Link</title>
        <style>body{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f5f5}
        .card{background:white;padding:40px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.1);text-align:center;max-width:500px}
        h2{color:#e74c3c}p{color:#666}</style></head>
        <body><div class="card"><h2>Invalid Link</h2><p>This link is invalid or has expired. Please use the PEXA Tracker dashboard instead.</p></div></body></html>
        """, 403)

    notification = get_notification(notification_id)
    if not notification:
        return make_response("""
        <!DOCTYPE html>
        <html><head><title>Not Found</title>
        <style>body{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f5f5}
        .card{background:white;padding:40px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.1);text-align:center;max-width:500px}
        h2{color:#e74c3c}p{color:#666}</style></head>
        <body><div class="card"><h2>Notification Not Found</h2><p>This notification no longer exists.</p></div></body></html>
        """, 404)

    # Check if already actioned
    if notification["status"] == "actioned":
        return make_response(_build_done_page(notification, already_done=True), 200)

    # Mark as actioned
    update_notification_status(notification_id, "actioned", user="Via Email Link")
    add_note(notification_id, "Marked as done via email link", "Email Link")
    logger.info(f"Notification {notification_id} marked as done via email link")

    return make_response(_build_done_page(notification, already_done=False), 200)


def _build_done_page(notification, already_done=False):
    """Build a fun congratulations page with a random meme when tasks are marked done."""
    import random

    matter = notification["matter_number"]
    actioned_by = notification.get("actioned_by", "Unknown")
    actioned_at = notification.get("actioned_at", "Unknown")

    # Fun rotating meme GIFs - a mix of celebration & motivation
    memes = [
        "https://media.giphy.com/media/3o7abB06u9bNzA8lu8/giphy.gif",       # The Office - celebrate
        "https://media.giphy.com/media/l0MYt5jPR6QX5pnqM/giphy.gif",       # High five
        "https://media.giphy.com/media/xT0xezQGU5xCDJuCPe/giphy.gif",      # Thumbs up kid
        "https://media.giphy.com/media/3oEjHFOscgNwdSRRDy/giphy.gif",      # Success kid
        "https://media.giphy.com/media/26u4cqiYI30juCOGY/giphy.gif",       # Celebrate
        "https://media.giphy.com/media/l3q2Z6S6n38zjPswo/giphy.gif",       # You got this
        "https://media.giphy.com/media/fdyZ3qI0GVZC0/giphy.gif",           # Minion cheer
        "https://media.giphy.com/media/3oz8xRF0v9WMAUG1IQ/giphy.gif",      # Dwight celebrate
        "https://media.giphy.com/media/xUPGGDNsLvqsBOhuU0/giphy.gif",      # Cat thumbs up
        "https://media.giphy.com/media/l0HlMSVVw9zqmClLq/giphy.gif",       # Awesome
        "https://media.giphy.com/media/5GoVLqeAOo6PK/giphy.gif",           # Proud
        "https://media.giphy.com/media/YRuFixSNWFVcXaxpmX/giphy.gif",      # Great job
    ]
    meme_url = random.choice(memes)

    # Fun rotating messages
    messages = [
        "You're on fire today!",
        "Another one bites the dust!",
        "Keep smashing it!",
        "Legend status confirmed!",
        "Productivity level: BEAST MODE!",
        "That's how it's done!",
        "You make it look easy!",
        "Crushing it!",
        "Efficiency at its finest!",
        "One less thing to worry about!",
    ]
    fun_msg = random.choice(messages)

    if already_done:
        heading = "Thank you! Good Work on Marking Another One Done!"
        sub_text = f'<p style="color:#888;font-size:14px;margin-top:4px">Actioned by {actioned_by} at {actioned_at}</p>'
    else:
        heading = "Thank you! Good Work on Marking Another One Done!"
        sub_text = ""

    return f"""<!DOCTYPE html>
<html><head><title>Task Complete!</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body{{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%)}}
.card{{background:white;padding:40px 36px;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,0.2);text-align:center;max-width:480px;width:90%;animation:popIn 0.4s ease}}
@keyframes popIn{{0%{{transform:scale(0.8);opacity:0}}100%{{transform:scale(1);opacity:1}}}}
.check{{font-size:72px;margin-bottom:8px}}
h2{{color:#27ae60;font-size:22px;margin:8px 0 4px}}
.fun-msg{{color:#6c5ce7;font-size:18px;font-weight:600;margin:8px 0 16px}}
.matter{{background:#f0fff4;padding:14px 20px;border-radius:10px;margin:16px 0;font-size:20px;font-weight:700;color:#1a7a3a;border:2px solid #27ae60}}
.meme{{margin:20px 0 12px;border-radius:12px;overflow:hidden;display:inline-block;box-shadow:0 4px 16px rgba(0,0,0,0.1)}}
.meme img{{max-width:100%;height:auto;max-height:280px;display:block}}
p{{color:#666;font-size:13px;line-height:1.5}}
</style></head>
<body><div class="card">
<div class="check">🎉</div>
<h2>{heading}</h2>
<div class="fun-msg">{fun_msg}</div>
<div class="matter">Matter #{matter}</div>
<div class="meme"><img src="{meme_url}" alt="Celebration meme"></div>
{sub_text}
</div></body></html>"""


# --- 24-Hour Reminder Check ---

def check_overdue_tasks():
    """Check for tasks emailed more than 24 hours ago that haven't been actioned.
    Sends a reminder email to the original recipient with the Mark as Done link
    and asks them to contact Sheriff/Jai if they need help."""
    try:
        logger.info("Running 24-hour overdue check...")
        overdue = get_overdue_emailed_tasks(hours=24)
        if not overdue:
            logger.info("Overdue check: no tasks need reminders right now")
            return
        logger.info(f"Found {len(overdue)} overdue task(s) needing reminders")

        # Base URL for Mark as Done links (no request context in scheduled jobs)
        base_url = os.getenv("APP_URL", "https://pexa-notification-tracker.onrender.com")
        send_mailbox = os.getenv("SEND_FROM_MAILBOX", graph_client.mailbox)

        reminder_count = 0
        for task in overdue:
            try:
                nid = task["id"]
                # emailed_to may contain multiple comma-separated addresses
                to_email = [e.strip() for e in task["emailed_to"].split(",") if e.strip()]
                matter = task.get("matter_number", "Unknown")
                ntype = task.get("notification_type", "PEXA Notification")
                summary = task.get("summary", "")[:200]
                settlement_date = task.get("settlement_date", "") or "N/A"

                # Generate Mark as Done link
                token = generate_action_token(nid)
                done_link = f"{base_url}/done/{nid}?token={token}"

                # Build reminder email (HTML)
                urgent_prefix = "URGENT - " if _is_settlement_today_or_tomorrow(settlement_date) else ""
                subject = f"REMINDER: {urgent_prefix}{matter} - Settlement Date {_settlement_date_only(settlement_date)} - PEXA Action Required"
                # Escape HTML in dynamic content
                safe_matter = str(matter).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                safe_ntype = str(ntype).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                safe_summary = str(summary).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

                reminder_html = f"""<html>
<body style="font-family: Arial, sans-serif; font-size: 14px; color: #333; line-height: 1.5;">
<p>Hi,</p>
<p>This is a reminder that the following PEXA task was sent to you over 24 hours ago and has not yet been marked as complete:</p>

<div style="color: #cc0000; font-weight: bold; font-size: 15px; padding: 12px 16px; background: #fff5f5; border-left: 4px solid #cc0000; margin: 12px 0;">
    Matter #: {safe_matter}<br>
    Type: {safe_ntype}<br>
    Summary: {safe_summary}
</div>

<hr style="border: none; border-top: 2px solid #333; margin: 20px 0;">
<div style="text-align: center; margin: 20px 0; padding: 16px; background: #f0fff4; border: 2px solid #27ae60; border-radius: 8px;">
    <p style="font-size: 18px; font-weight: bold; color: #1a7a3a;">✅ PLEASE MARK THIS TASK AS COMPLETE</p>
    <a href="{done_link}" style="display: inline-block; background: #27ae60; color: white; padding: 14px 32px; font-size: 16px; font-weight: bold; text-decoration: none; border-radius: 8px; margin: 8px 0;">Click Here To Mark As Done</a>
    <p style="color: #333; font-size: 14px; font-weight: bold; margin-top: 14px;">You must click the button above once you have completed this task.</p>
</div>
<hr style="border: none; border-top: 2px solid #333; margin: 20px 0;">

<p>If this task has <b>NOT</b> been completed, or if you need help, please email:</p>
<ul>
    <li><a href="mailto:sheriff@legalworld.com.au">sheriff@legalworld.com.au</a></li>
    <li><a href="mailto:jai@legalworld.com.au">jai@legalworld.com.au</a></li>
</ul>
<p>Please let them know what is happening with this task and if you require any assistance.</p>

<p>Thank you,<br>PEXA Notification Tracker</p>
</body>
</html>"""

                # Send reminder - CC Sheriff and Jai so they're aware
                cc_emails = "sheriff@legalworld.com.au,jai@legalworld.com.au"
                graph_client.send_email(to_email, subject, "", from_mailbox=send_mailbox, cc_emails=cc_emails, body_html=reminder_html)

                # Mark reminder as sent so we don't send again
                mark_reminder_sent(nid)
                to_display = ", ".join(to_email) if isinstance(to_email, list) else to_email
                add_note(nid, f"24-hour reminder sent to {to_display}", "System")

                reminder_count += 1
                logger.info(f"Reminder sent to {to_display} for notification {nid} (Matter #{matter})")

            except Exception as e:
                logger.error(f"Failed to send reminder for notification {task['id']}: {e}")

        logger.info(f"Overdue check complete: {reminder_count} reminders sent out of {len(overdue)} overdue tasks")

    except Exception as e:
        logger.error(f"Overdue task check failed: {e}")


def check_overdue_tasks_force():
    """Force-send reminders to all reviewed+emailed tasks where reminder_sent=0,
    regardless of how long ago they were emailed (used by manual Send Reminders button)."""
    try:
        logger.info("Running FORCED reminder send for all reviewed tasks...")
        from database import get_all_unremindered_emailed_tasks
        overdue = get_all_unremindered_emailed_tasks()
        if not overdue:
            logger.info("Force reminder: no tasks to remind")
            return
        logger.info(f"Force reminder: found {len(overdue)} task(s) to send reminders for")

        base_url = os.getenv("APP_URL", "https://pexa-notification-tracker.onrender.com")
        send_mailbox = os.getenv("SEND_FROM_MAILBOX", graph_client.mailbox)

        reminder_count = 0
        for task in overdue:
            try:
                nid = task["id"]
                to_email = [e.strip() for e in task["emailed_to"].split(",") if e.strip()]
                matter = task.get("matter_number", "Unknown")
                ntype = task.get("notification_type", "PEXA Notification")
                summary = task.get("summary", "")[:200]
                settlement_date = task.get("settlement_date", "") or "N/A"

                token = generate_action_token(nid)
                done_link = f"{base_url}/done/{nid}?token={token}"

                urgent_prefix = "URGENT - " if _is_settlement_today_or_tomorrow(settlement_date) else ""
                subject = f"REMINDER: {urgent_prefix}{matter} - Settlement Date {_settlement_date_only(settlement_date)} - PEXA Action Required"
                safe_matter = str(matter).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                safe_ntype = str(ntype).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                safe_summary = str(summary).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

                reminder_html = f"""<html>
<body style="font-family: Arial, sans-serif; font-size: 14px; color: #333; line-height: 1.5;">
<p>Hi,</p>
<p>This is a reminder that the following PEXA task was sent to you and has not yet been marked as complete:</p>

<div style="color: #cc0000; font-weight: bold; font-size: 15px; padding: 12px 16px; background: #fff5f5; border-left: 4px solid #cc0000; margin: 12px 0;">
    Matter #: {safe_matter}<br>
    Type: {safe_ntype}<br>
    Summary: {safe_summary}
</div>

<hr style="border: none; border-top: 2px solid #333; margin: 20px 0;">
<div style="text-align: center; margin: 20px 0; padding: 16px; background: #f0fff4; border: 2px solid #27ae60; border-radius: 8px;">
    <p style="font-size: 18px; font-weight: bold; color: #1a7a3a;">✅ PLEASE MARK THIS TASK AS COMPLETE</p>
    <a href="{done_link}" style="display: inline-block; background: #27ae60; color: white; padding: 14px 32px; font-size: 16px; font-weight: bold; text-decoration: none; border-radius: 8px; margin: 8px 0;">Click Here To Mark As Done</a>
    <p style="color: #333; font-size: 14px; font-weight: bold; margin-top: 14px;">You must click the button above once you have completed this task.</p>
</div>
<hr style="border: none; border-top: 2px solid #333; margin: 20px 0;">

<p>If this task has <b>NOT</b> been completed, or if you need help, please email:</p>
<ul>
    <li><a href="mailto:sheriff@legalworld.com.au">sheriff@legalworld.com.au</a></li>
    <li><a href="mailto:jai@legalworld.com.au">jai@legalworld.com.au</a></li>
</ul>
<p>Please let them know what is happening with this task and if you require any assistance.</p>

<p>Thank you,<br>PEXA Notification Tracker</p>
</body>
</html>"""

                cc_emails = "sheriff@legalworld.com.au,jai@legalworld.com.au"
                graph_client.send_email(to_email, subject, "", from_mailbox=send_mailbox, cc_emails=cc_emails, body_html=reminder_html)

                mark_reminder_sent(nid)
                to_display = ", ".join(to_email) if isinstance(to_email, list) else to_email
                add_note(nid, f"Manual reminder sent to {to_display}", "System")

                reminder_count += 1
                logger.info(f"Force reminder sent to {to_display} for notification {nid} (Matter #{matter})")

            except Exception as e:
                logger.error(f"Failed to send force reminder for notification {task['id']}: {e}")

        logger.info(f"Force reminder complete: {reminder_count} reminders sent out of {len(overdue)} tasks")

    except Exception as e:
        logger.error(f"Force reminder check failed: {e}")


@app.route("/api/check-reminders", methods=["POST"])
def api_check_reminders():
    """Manually trigger the 24-hour overdue reminder check."""
    check_overdue_tasks()
    return jsonify({"success": True, "message": "Reminder check completed - see server logs for details"})


@app.route("/api/send-reminders", methods=["POST"])
def api_send_reminders():
    """Send reminders for specific selected notification IDs.
    Expects JSON body with 'ids' list of notification IDs."""
    try:
        data = request.json or {}
        ids = data.get("ids", [])
        if not ids:
            return jsonify({"success": False, "error": "No notification IDs provided"}), 400

        from database import get_notification, reset_reminder_sent

        # Gather valid tasks that have been emailed
        tasks = []
        for nid in ids:
            n = get_notification(nid)
            if n and n.get("emailed_to"):
                tasks.append(n)

        if not tasks:
            return jsonify({"success": True, "count": 0, "message": "No emailed tasks found in selection"})

        # Reset reminder_sent for selected tasks so they get picked up
        for task in tasks:
            reset_reminder_sent(task["id"])

        logger.info(f"Manual reminder trigger for {len(tasks)} selected task(s): {[t['id'] for t in tasks]}")

        # Send reminders for these specific tasks
        _send_reminders_for_tasks(tasks)

        return jsonify({"success": True, "count": len(tasks), "message": f"Reminders sent for {len(tasks)} selected task(s)"})
    except Exception as e:
        logger.error(f"Manual reminder send failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _send_reminders_for_tasks(tasks):
    """Send reminder emails for a specific list of task dicts."""
    base_url = os.getenv("APP_URL", "https://pexa-notification-tracker.onrender.com")
    send_mailbox = os.getenv("SEND_FROM_MAILBOX", graph_client.mailbox)

    for task in tasks:
        try:
            nid = task["id"]
            to_email = [e.strip() for e in task["emailed_to"].split(",") if e.strip()]
            matter = task.get("matter_number", "Unknown")
            ntype = task.get("notification_type", "PEXA Notification")
            summary = task.get("summary", "")[:200]
            settlement_date = task.get("settlement_date", "") or "N/A"

            token = generate_action_token(nid)
            done_link = f"{base_url}/done/{nid}?token={token}"

            urgent_prefix = "URGENT - " if _is_settlement_today_or_tomorrow(settlement_date) else ""
            subject = f"REMINDER: {urgent_prefix}{matter} - Settlement Date {_settlement_date_only(settlement_date)} - PEXA Action Required"
            safe_matter = str(matter).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            safe_ntype = str(ntype).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            safe_summary = str(summary).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

            reminder_html = f"""<html>
<body style="font-family: Arial, sans-serif; font-size: 14px; color: #333; line-height: 1.5;">
<p>Hi,</p>
<p>This is a reminder that the following PEXA task was sent to you and has not yet been marked as complete:</p>

<div style="color: #cc0000; padding: 12px 16px; background: #fff5f5; border-left: 4px solid #cc0000; margin: 12px 0;">
    Matter #: {safe_matter}<br>
    Type: {safe_ntype}<br>
    Summary: {safe_summary}
</div>

<hr style="border: none; border-top: 2px solid #333; margin: 20px 0;">
<div style="text-align: center; margin: 20px 0; padding: 16px; background: #f0fff4; border: 2px solid #27ae60; border-radius: 8px;">
    <p style="font-size: 18px; font-weight: bold; color: #1a7a3a;">✅ PLEASE MARK THIS TASK AS COMPLETE</p>
    <a href="{done_link}" style="display: inline-block; background: #27ae60; color: white; padding: 14px 32px; font-size: 16px; font-weight: bold; text-decoration: none; border-radius: 8px; margin: 8px 0;">Click Here To Mark As Done</a>
    <p style="color: #333; font-size: 14px; font-weight: bold; margin-top: 14px;">You must click the button above once you have completed this task.</p>
</div>
<hr style="border: none; border-top: 2px solid #333; margin: 20px 0;">

<p>If this task has <b>NOT</b> been completed, or if you need help, please email:</p>
<ul>
    <li><a href="mailto:sheriff@legalworld.com.au">sheriff@legalworld.com.au</a></li>
    <li><a href="mailto:jai@legalworld.com.au">jai@legalworld.com.au</a></li>
</ul>
<p>Please let them know what is happening with this task and if you require any assistance.</p>

<p>Thank you,<br>PEXA Notification Tracker</p>
</body>
</html>"""

            cc_emails = "sheriff@legalworld.com.au,jai@legalworld.com.au"
            graph_client.send_email(to_email, subject, "", from_mailbox=send_mailbox, cc_emails=cc_emails, body_html=reminder_html)

            mark_reminder_sent(nid)
            to_display = ", ".join(to_email) if isinstance(to_email, list) else to_email
            add_note(nid, f"Manual reminder sent to {to_display}", "System")
            logger.info(f"Reminder sent to {to_display} for notification {nid} (Matter #{matter})")

        except Exception as e:
            logger.error(f"Failed to send reminder for notification {task['id']}: {e}")


# --- Push to SharePoint Excel ---

def _do_push_to_excel(ids, skip_complete=False, auto_push=False):
    """Core push logic — runs in a thread for background mode. Returns dict with results.

    When auto_push=True, the cell text is prefixed with '* ' so auto-pushed entries
    can be visually distinguished from manually-pushed ones in the spreadsheet."""
    sharepoint_url = os.getenv("SHAREPOINT_EXCEL_URL", "")
    if not sharepoint_url:
        return {"success": False, "error": "SHAREPOINT_EXCEL_URL not configured"}

    try:
        # Resolve the SharePoint file
        drive_id, item_id = graph_client.resolve_sharing_url(sharepoint_url)

        # Get all worksheet names
        sheets = graph_client.get_excel_worksheets(drive_id, item_id)
        logger.info(f"Excel sheets: {sheets}")

        # Skip non-weekly sheets
        skip_sheets = {"physicals", "master data", "mwsd", "sheet1", "sheet2", "import", "ttb (2)", "invoices"}

        # Cache sheet contents so we only fetch each sheet once per batch
        sheet_cache = {}

        # Gather notifications to push
        updated = []
        errors = []

        for nid in ids:
            n = get_notification(nid)
            if not n:
                errors.append(f"ID {nid}: not found")
                continue

            matter_num = n.get("matter_number", "").strip()
            ntype = n.get("notification_type", "PEXA Notification")
            received = n.get("received_at", "")
            full_body = n.get("full_body", "") or ""

            # Extract the actual PEXA message content
            subject_line = n.get("subject", "") or ""
            pexa_message = _extract_pexa_message(full_body, notification_type=ntype, subject=subject_line)

            # Format the note: actual message + type + date
            try:
                from datetime import datetime as _dt
                dt = _dt.fromisoformat(received.replace("Z", "+00:00"))
                date_str = dt.strftime("%d/%m/%Y %H:%M")
            except Exception:
                date_str = received[:16] if received else "Unknown"

            if pexa_message:
                note_text = f"{pexa_message} ({ntype} - {date_str})"
            else:
                note_text = f"{ntype} - {date_str}"

            if auto_push:
                note_text = f"* {note_text}"

            # Search across weekly sheets for this matter number
            found = False
            for sheet in sheets:
                if sheet.lower().strip() in skip_sheets:
                    continue
                if "pexa check" in sheet.lower():
                    continue

                try:
                    if sheet in sheet_cache:
                        values, address = sheet_cache[sheet]
                    else:
                        values, address = graph_client.get_excel_used_range(drive_id, item_id, sheet)
                        sheet_cache[sheet] = (values, address)
                    if not values or len(values) < 2:
                        continue

                    # Find "PEXA Notes" column by scanning ALL columns in header rows
                    PEXA_COL = None  # Will be set to the column index where "PEXA Notes" is found
                    DEFAULT_COL = 6  # Column G = index 6 (for inserting if not found)
                    DEFAULT_COL_LETTER = "G"
                    pexa_header_exists = False
                    header_row_idx = None

                    for ri in range(min(15, len(values))):
                        for ci in range(len(values[ri])):
                            cell_val = str(values[ri][ci] or "").strip().lower()
                            # Find the header row (contains settlement, adjustments, etc.)
                            if cell_val in ("settlement", "settlement date", "jurisdiction", "adjustments"):
                                header_row_idx = ri
                            # Check if this cell is PEXA Notes
                            if cell_val in ("pexa notes", "pexa note"):
                                pexa_header_exists = True
                                PEXA_COL = ci
                                header_row_idx = ri
                                break
                        if pexa_header_exists:
                            break

                    # Convert column index to letter for cell references
                    def _col_letter(idx):
                        """Convert 0-based column index to Excel column letter (0=A, 6=G, 26=AA)."""
                        result = ""
                        while True:
                            result = chr(65 + idx % 26) + result
                            idx = idx // 26 - 1
                            if idx < 0:
                                break
                        return result

                    if PEXA_COL is not None:
                        PEXA_COL_LETTER = _col_letter(PEXA_COL)
                    else:
                        PEXA_COL = DEFAULT_COL
                        PEXA_COL_LETTER = DEFAULT_COL_LETTER

                    # If no PEXA Notes column found anywhere, insert one at column G
                    if not pexa_header_exists:
                        try:
                            graph_client.insert_excel_column(drive_id, item_id, sheet, DEFAULT_COL_LETTER)
                            PEXA_COL = DEFAULT_COL
                            PEXA_COL_LETTER = DEFAULT_COL_LETTER
                            logger.info(f"Inserted new column {DEFAULT_COL_LETTER} in sheet '{sheet}'")

                            # Set the header
                            if header_row_idx is not None:
                                range_start_row_for_header = 1
                                if address and "!" in address:
                                    range_part = address.split("!")[1]
                                    import re as _re
                                    m = _re.match(r"[A-Z]+(\d+)", range_part)
                                    if m:
                                        range_start_row_for_header = int(m.group(1))
                                header_excel_row = range_start_row_for_header + header_row_idx
                            else:
                                header_excel_row = 1
                            graph_client.update_excel_cell(drive_id, item_id, sheet, f"{PEXA_COL_LETTER}{header_excel_row}", "PEXA Notes")
                            logger.info(f"Added 'PEXA Notes' header at {sheet}!{PEXA_COL_LETTER}{header_excel_row}")

                            # Re-read the used range since columns shifted and update cache
                            values, address = graph_client.get_excel_used_range(drive_id, item_id, sheet)
                            sheet_cache[sheet] = (values, address)
                        except Exception as ins_err:
                            logger.warning(f"Could not insert column G in '{sheet}': {ins_err}")
                            # Fall back — column G might already exist from a previous run
                            pass

                    # Search column A for the matter number
                    for ri in range(len(values)):
                        cell_val = str(values[ri][0] or "").strip()
                        # Match: "71263 PURCHASE" starts with "71263"
                        if cell_val and cell_val.startswith(matter_num):
                            found = True
                            range_start_row = 1
                            if address and "!" in address:
                                range_part = address.split("!")[1]
                                import re
                                match = re.match(r"[A-Z]+(\d+)", range_part)
                                if match:
                                    range_start_row = int(match.group(1))

                            excel_row = range_start_row + ri
                            target_cell = f"{PEXA_COL_LETTER}{excel_row}"

                            # Read existing value to append (don't overwrite)
                            existing = ""
                            if PEXA_COL < len(values[ri]):
                                existing = str(values[ri][PEXA_COL] or "").strip()

                            if existing and existing.lower() != "pexa notes":
                                new_value = f"{note_text}\n{existing}"
                            else:
                                new_value = note_text

                            graph_client.update_excel_cell(drive_id, item_id, sheet, target_cell, new_value)
                            updated.append(f"Matter {matter_num} in '{sheet}' ({target_cell})")
                            logger.info(f"Updated {sheet}!{target_cell} for matter {matter_num}: {note_text}")

                            # Update the cache so subsequent tickets for the same matter see the new value
                            try:
                                # Ensure row has enough columns
                                while len(values[ri]) <= PEXA_COL:
                                    values[ri].append("")
                                values[ri][PEXA_COL] = new_value
                                sheet_cache[sheet] = (values, address)
                            except Exception:
                                pass

                            add_note(nid, f"Pushed to spreadsheet: {sheet}!{target_cell}", "System")
                            break

                except Exception as e:
                    logger.warning(f"Error scanning sheet '{sheet}': {e}")
                    continue

                if found:
                    break

            if not found:
                errors.append(f"Matter {matter_num}: not found in any sheet")

        # Auto-mark successfully pushed tickets as complete (unless frontend already did it)
        if not skip_complete:
            for nid in ids:
                try:
                    n = get_notification(nid)
                    if n and n["status"] != "actioned":
                        update_notification_status(nid, "actioned", user="Push to Spreadsheet")
                        add_note(nid, "Auto-marked complete after pushing to spreadsheet", "System")
                except Exception as e:
                    logger.warning(f"Failed to auto-complete notification {nid}: {e}")

        msg_parts = []
        if updated:
            msg_parts.append(f"Updated {len(updated)} matter(s) in spreadsheet")
        if errors:
            msg_parts.append(f"{len(errors)} not found")

        return {
            "success": True,
            "count": len(updated),
            "updated": updated,
            "errors": errors,
            "message": ". ".join(msg_parts) or "No updates made",
        }

    except Exception as e:
        logger.error(f"Push to Excel failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


# --- Adjustment notes from Apollo (the PE Portal) -------------------------------
# Apollo posts here after it serves the draft adjustment sheet on the other side, so
# the shared settlements spreadsheet shows it next to the matter without anyone
# re-keying it. Deliberately a DEDICATED column (D) — the PEXA Notes column above is
# owned by the notification push and the two must not fight over one cell.
ADJ_COL_LETTER = "D"
ADJ_COL = 3          # 0-based index of column D
ADJ_FILL = "#ADD8E6"  # light blue — makes Apollo-written notes obvious at a glance


def _push_adjustment_note(matter_number, note_text):
    """Write note_text into column D of every weekly tab row for this matter.
    Prepends to whatever is already there, so history is never lost."""
    sharepoint_url = os.getenv("SHAREPOINT_EXCEL_URL", "")
    if not sharepoint_url:
        return {"success": False, "error": "SHAREPOINT_EXCEL_URL not configured"}

    matter_num = str(matter_number or "").strip()
    note_text = str(note_text or "").strip()
    if not matter_num:
        return {"success": False, "error": "matterNumber is required"}
    if not note_text:
        return {"success": False, "error": "note is required"}

    import re
    try:
        drive_id, item_id = graph_client.resolve_sharing_url(sharepoint_url)
        sheets = graph_client.get_excel_worksheets(drive_id, item_id)
        skip_sheets = {"physicals", "master data", "mwsd", "sheet1", "sheet2",
                       "import", "ttb (2)", "invoices"}
        updated, errors = [], []

        for sheet in sheets:
            if sheet.lower().strip() in skip_sheets:
                continue
            if "pexa check" in sheet.lower():
                continue
            try:
                values, address = graph_client.get_excel_used_range(drive_id, item_id, sheet)
                if not values or len(values) < 2:
                    continue

                range_start_row = 1
                if address and "!" in address:
                    m = re.match(r"[A-Z]+(\d+)", address.split("!")[1])
                    if m:
                        range_start_row = int(m.group(1))

                for ri in range(len(values)):
                    cell_val = str(values[ri][0] or "").strip()
                    if not cell_val or not cell_val.startswith(matter_num):
                        continue
                    # "75017" must not match "750171" — the next char has to be a
                    # separator, not another digit.
                    tail = cell_val[len(matter_num):len(matter_num) + 1]
                    if tail.isdigit():
                        continue

                    excel_row = range_start_row + ri
                    target_cell = f"{ADJ_COL_LETTER}{excel_row}"

                    existing = ""
                    if ADJ_COL < len(values[ri]):
                        existing = str(values[ri][ADJ_COL] or "").strip()
                    new_value = f"{note_text}\n{existing}" if existing else note_text

                    graph_client.update_excel_cell(drive_id, item_id, sheet, target_cell,
                                                   new_value, fill=ADJ_FILL)
                    updated.append(f"{sheet}!{target_cell}")
                    logger.info(f"Adj note: {sheet}!{target_cell} for matter {matter_num}: {note_text}")
            except Exception as sheet_err:
                errors.append(f"{sheet}: {sheet_err}")

        return {
            "success": bool(updated),
            "matter": matter_num,
            "note": note_text,
            "updated": updated,
            "errors": errors,
            "error": None if updated else "matter not found on any weekly tab",
        }
    except Exception as e:
        logger.error(f"Adj note push failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


@app.route("/api/adj-note", methods=["POST"])
def api_adj_note():
    """POST {matterNumber, note[, token]} — called by Apollo when adjustments are
    served on the other side. If APOLLO_NOTE_TOKEN is set in the environment a
    matching token is required; if it isn't set the endpoint is open, matching the
    rest of this app's API (recommend setting it)."""
    data = request.get_json(silent=True) or {}
    required = os.getenv("APOLLO_NOTE_TOKEN", "")
    if required and str(data.get("token", "")) != required:
        return jsonify({"success": False, "error": "unauthorized"}), 401

    result = _push_adjustment_note(data.get("matterNumber"), data.get("note"))
    return jsonify(result), (200 if result.get("success") else 404 if result.get("error") == "matter not found on any weekly tab" else 500)


@app.route("/api/adj-note/peek", methods=["GET"])
def api_adj_note_peek():
    """GET ?matter=74254 — read back column A and column D for that matter on every
    weekly tab. Diagnostic only: lets us confirm a note actually landed (and is
    still there) without opening the workbook."""
    import re
    matter_num = str(request.args.get("matter", "")).strip()
    if not matter_num:
        return jsonify({"success": False, "error": "matter is required"}), 400

    sharepoint_url = os.getenv("SHAREPOINT_EXCEL_URL", "")
    if not sharepoint_url:
        return jsonify({"success": False, "error": "SHAREPOINT_EXCEL_URL not configured"}), 500

    try:
        drive_id, item_id = graph_client.resolve_sharing_url(sharepoint_url)
        sheets = graph_client.get_excel_worksheets(drive_id, item_id)
        skip_sheets = {"physicals", "master data", "mwsd", "sheet1", "sheet2",
                       "import", "ttb (2)", "invoices"}
        rows = []
        for sheet in sheets:
            if sheet.lower().strip() in skip_sheets or "pexa check" in sheet.lower():
                continue
            try:
                values, address = graph_client.get_excel_used_range(drive_id, item_id, sheet)
                if not values:
                    continue
                range_start_row = 1
                if address and "!" in address:
                    m = re.match(r"[A-Z]+(\d+)", address.split("!")[1])
                    if m:
                        range_start_row = int(m.group(1))
                for ri in range(len(values)):
                    cell_val = str(values[ri][0] or "").strip()
                    if not cell_val or not cell_val.startswith(matter_num):
                        continue
                    if cell_val[len(matter_num):len(matter_num) + 1].isdigit():
                        continue
                    cell_addr = f"{ADJ_COL_LETTER}{range_start_row + ri}"
                    fill = None
                    try:
                        fill = graph_client.get_excel_cell_fill(drive_id, item_id, sheet, cell_addr)
                    except Exception:
                        pass
                    rows.append({
                        "cell": f"{sheet}!{cell_addr}",
                        "colA": cell_val,
                        "colD": str(values[ri][ADJ_COL] or "") if ADJ_COL < len(values[ri]) else "",
                        "fill": fill,
                    })
            except Exception as sheet_err:
                logger.warning(f"Adj peek {sheet}: {sheet_err}")
        return jsonify({"success": True, "matter": matter_num, "rows": rows})
    except Exception as e:
        logger.error(f"Adj peek failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/push-to-excel", methods=["POST"])
def api_push_to_excel():
    """Push selected notifications to the shared SharePoint Excel spreadsheet.
    If async_mode=true, runs in background thread and returns immediately."""
    data = request.json or {}
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"success": False, "error": "No notification IDs provided"}), 400

    skip_complete = data.get("skip_complete", False)
    async_mode = data.get("async_mode", False)

    if async_mode:
        # Run in background thread so the HTTP response returns immediately
        import threading
        def _bg_push():
            try:
                result = _do_push_to_excel(ids, skip_complete=skip_complete)
                logger.info(f"Background push complete: {result.get('message', 'done')}")
            except Exception as e:
                logger.error(f"Background push failed: {e}", exc_info=True)
        threading.Thread(target=_bg_push, daemon=True).start()
        return jsonify({
            "success": True,
            "count": len(ids),
            "message": f"Queued {len(ids)} ticket(s) for background push",
            "background": True,
        })

    # Synchronous mode
    result = _do_push_to_excel(ids, skip_complete=skip_complete)
    if not result.get("success"):
        return jsonify(result), 500
    return jsonify(result)


@app.route("/api/recover-missing-pushes", methods=["POST"])
def api_recover_missing_pushes():
    """Find all actioned tickets without a spreadsheet push note and push them now.
    Runs in the background."""
    data = request.json or {}
    days = int(data.get("days", 3))
    try:
        from datetime import datetime as _dt, timedelta as _td
        cutoff = _dt.utcnow() - _td(days=days)

        notifs = get_notifications({"status": "actioned"})
        missing_ids = []
        for n in notifs:
            notes = (n.get("notes") or "").lower()
            if "spreadsheet" in notes:
                continue
            actioned_by = n.get("actioned_by") or ""
            if actioned_by in ("System", "Push to Spreadsheet", "Via Email Link"):
                continue
            actioned_at = n.get("actioned_at") or ""
            if not actioned_at:
                continue
            try:
                dt = _dt.fromisoformat(actioned_at.replace("Z", "").split("+")[0])
                if dt < cutoff:
                    continue
            except Exception:
                continue
            missing_ids.append(n["id"])

        if not missing_ids:
            return jsonify({"success": True, "count": 0, "message": "No missing pushes found"})

        logger.info(f"Recovery: queuing {len(missing_ids)} missing pushes in background")

        # Run in background thread
        import threading
        def _bg_recover():
            try:
                result = _do_push_to_excel(missing_ids, skip_complete=True)
                logger.info(f"Recovery push complete: {result.get('message', 'done')}")
            except Exception as e:
                logger.error(f"Recovery push failed: {e}", exc_info=True)
        threading.Thread(target=_bg_recover, daemon=True).start()

        return jsonify({
            "success": True,
            "count": len(missing_ids),
            "message": f"Queued {len(missing_ids)} missing ticket(s) for background recovery",
        })
    except Exception as e:
        logger.error(f"Recovery failed: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


def _col_letter(col_index):
    """Convert 0-based column index to Excel column letter (0=A, 1=B, ..., 25=Z, 26=AA)."""
    result = ""
    idx = col_index
    while True:
        result = chr(65 + idx % 26) + result
        idx = idx // 26 - 1
        if idx < 0:
            break
    return result


# --- Auto-push: hourly job that pushes 'new' notifications to the spreadsheet,
# gated by the end-date of the latest weekly tab (e.g. '6 July - 10 July' → 10 July).

_MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _parse_tab_end_date(tab_name, ref_date=None):
    """Parse the END date from a weekly tab name like '22 June - 26 June' or
    '29 June - 3 July'. Returns a datetime at 23:59:59 Sydney time (inclusive
    end-of-day). Year is inferred from ref_date — if the parsed date is more
    than 6 months in the past, it's bumped a year forward to handle year-end
    rollover. Returns None if the name doesn't match the expected pattern."""
    import re as _re
    if not tab_name:
        return None
    m = _re.search(r"(\d{1,2})\s+(\w+)\s*-\s*(\d{1,2})\s+(\w+)", tab_name.strip())
    if not m:
        return None
    end_day_str, end_month_str = m.group(3), m.group(4)
    month = _MONTH_MAP.get(end_month_str.lower())
    if not month:
        return None
    try:
        day = int(end_day_str)
    except ValueError:
        return None
    ref = ref_date or _now_sydney()
    year = ref.year
    try:
        candidate = datetime(year, month, day, 23, 59, 59, tzinfo=SYDNEY_TZ)
    except ValueError:
        return None
    if (ref - candidate).days > 180:
        try:
            candidate = datetime(year + 1, month, day, 23, 59, 59, tzinfo=SYDNEY_TZ)
        except ValueError:
            return None
    elif (candidate - ref).days > 180:
        try:
            candidate = datetime(year - 1, month, day, 23, 59, 59, tzinfo=SYDNEY_TZ)
        except ValueError:
            return None
    return candidate


_PUSH_SKIP_SHEETS = {"physicals", "master data", "mwsd", "sheet1", "sheet2", "import", "ttb (2)", "invoices"}


def _compute_push_max_date(sheets):
    """Given a list of worksheet names, return the latest weekly-tab end date,
    or None if no parseable weekly tab is found. Excludes admin/reference and
    PEXA CHECK tabs (same skip rules as _do_push_to_excel)."""
    max_date = None
    max_tab = None
    ref = _now_sydney()
    for sheet in sheets:
        s_lower = sheet.lower().strip()
        if s_lower in _PUSH_SKIP_SHEETS:
            continue
        if "pexa check" in s_lower:
            continue
        end = _parse_tab_end_date(sheet, ref_date=ref)
        if end is None:
            continue
        if max_date is None or end > max_date:
            max_date = end
            max_tab = sheet
    if max_date:
        logger.info(f"Auto-push cap: {max_date.strftime('%d/%m/%Y')} (from tab '{max_tab}')")
    return max_date


def _parse_settlement_date(s):
    """Parse a settlement date string like '14/04/2026 02:30 PM AEST' or
    '14/04/2026'. Returns a datetime in Sydney TZ, or None."""
    import re as _re
    if not s:
        return None
    m = _re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(s).strip())
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=SYDNEY_TZ)
    except ValueError:
        return None


def auto_push_notifications():
    """Hourly: push 'new' notifications to the spreadsheet, gated by the cap
    derived from the latest weekly tab name. Notifications without a parseable
    settlement date are pushed regardless (no future-date risk). Successfully
    pushed notifications get auto-marked 'actioned' by the existing push logic;
    those whose matter isn't found in any tab stay 'new' and retry next hour."""
    if not os.getenv("SHAREPOINT_EXCEL_URL"):
        logger.warning("Auto-push: SHAREPOINT_EXCEL_URL not configured, skipping")
        return

    try:
        eligible = get_auto_push_eligible()
        if not eligible:
            logger.info("Auto-push: no 'new' notifications to push")
            return

        logger.info(f"Auto-push: {len(eligible)} 'new' notification(s) — computing cap from spreadsheet tabs")

        try:
            drive_id, item_id = graph_client.resolve_sharing_url(os.getenv("SHAREPOINT_EXCEL_URL"))
            sheets = graph_client.get_excel_worksheets(drive_id, item_id)
        except Exception as e:
            logger.error(f"Auto-push: could not resolve sheets ({e}); skipping run")
            return

        max_date = _compute_push_max_date(sheets)
        if max_date is None:
            logger.warning("Auto-push: no parseable weekly tab end date, skipping run")
            return

        push_ids = []
        skipped = 0
        for n in eligible:
            sd = _parse_settlement_date(n.get("settlement_date"))
            if sd is None or sd <= max_date:
                push_ids.append(n["id"])
            else:
                skipped += 1

        logger.info(
            f"Auto-push: cap={max_date.strftime('%d/%m/%Y')}; "
            f"pushing {len(push_ids)} of {len(eligible)} eligible "
            f"({skipped} skipped — settlement beyond cap)"
        )

        if not push_ids:
            return

        result = _do_push_to_excel(push_ids, auto_push=True)
        logger.info(f"Auto-push complete: {result.get('message', 'done')}")

    except Exception as e:
        logger.error(f"Auto-push job failed: {e}", exc_info=True)


@app.route("/api/auto-push", methods=["POST"])
def api_auto_push():
    """Manually trigger the hourly auto-push job (runs in background thread
    so the HTTP response returns immediately)."""
    import threading
    def _bg():
        try:
            auto_push_notifications()
        except Exception as e:
            logger.error(f"Manual auto-push trigger failed: {e}", exc_info=True)
    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"success": True, "message": "Auto-push triggered in background — see server logs for results"})


# --- Startup ---

# Always init the database and scheduler (works for both gunicorn and flask dev)
init_db()

sync_interval = int(os.getenv("SYNC_INTERVAL_MINUTES", "5"))

# TRACKER_ROLE controls which jobs this instance runs, so the same codebase can be
# deployed as two separate Render services without conflicting:
#   "all"           — everything (default; original single-service behaviour)
#   "notifications" — PEXA notification reading + overdue task reminders only
#   "workspaces"    — PE Portal workspace creation only
TRACKER_ROLE = os.getenv("TRACKER_ROLE", "all").strip().lower()
RUN_NOTIFICATIONS = TRACKER_ROLE in ("all", "notifications")
RUN_WORKSPACES    = TRACKER_ROLE in ("all", "workspaces")
print(f"TRACKER_ROLE={TRACKER_ROLE} | notifications={RUN_NOTIFICATIONS} | workspaces={RUN_WORKSPACES}")

scheduler = BackgroundScheduler()
if RUN_NOTIFICATIONS:
    scheduler.add_job(sync_emails, "interval", minutes=sync_interval, id="email_sync")
    scheduler.add_job(check_overdue_tasks, "interval", hours=1, id="overdue_check")
    scheduler.add_job(auto_push_notifications, "interval", hours=1, id="auto_push")
if RUN_WORKSPACES:
    scheduler.add_job(sync_workspaces, "interval", minutes=5, id="workspace_sync")
scheduler.start()

# Initial sync (on startup) — only for the jobs this instance is responsible for
if RUN_NOTIFICATIONS:
    logger.info("Running initial email sync...")
    sync_emails()
if RUN_WORKSPACES:
    logger.info("Running initial workspace sync...")
    sync_workspaces()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    logger.info(f"Starting PEXA Tracker on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
