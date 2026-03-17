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
    get_all_reviewed_emailed_tasks, reset_reminder_sent
from email_parser import parse_pexa_email
from graph_client import GraphClient

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

graph_client = GraphClient()
last_sync_time = None
last_sync_status = "Never synced"


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
    """Manually send reminders to ALL reviewed tasks that have been emailed out,
    regardless of how long ago they were emailed. Resets reminder_sent so they get re-sent."""
    try:
        from database import get_all_reviewed_emailed_tasks, reset_reminder_sent
        tasks = get_all_reviewed_emailed_tasks()
        if not tasks:
            return jsonify({"success": True, "count": 0, "message": "No reviewed tasks to send reminders for"})

        # Reset reminder_sent for all these tasks so they get picked up
        for task in tasks:
            reset_reminder_sent(task["id"])

        # Now run the overdue check which will send to all of them
        logger.info(f"Manual reminder trigger: {len(tasks)} reviewed tasks reset for reminders")
        check_overdue_tasks_force()
        return jsonify({"success": True, "count": len(tasks), "message": f"Reminders sent to {len(tasks)} reviewed task(s)"})
    except Exception as e:
        logger.error(f"Manual reminder send failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# --- Startup ---

# Always init the database and scheduler (works for both gunicorn and flask dev)
init_db()

sync_interval = int(os.getenv("SYNC_INTERVAL_MINUTES", "5"))
scheduler = BackgroundScheduler()
scheduler.add_job(sync_emails, "interval", minutes=sync_interval, id="email_sync")
scheduler.add_job(check_overdue_tasks, "interval", hours=1, id="overdue_check")
scheduler.start()

# Initial sync
logger.info("Running initial email sync...")
sync_emails()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    logger.info(f"Starting PEXA Tracker on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
