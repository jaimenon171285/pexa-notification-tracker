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
    update_emailed_info, get_overdue_emailed_tasks, mark_reminder_sent
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


@app.route("/api/send-task", methods=["POST"])
def api_send_task():
    data = request.json
    notification_id = data.get("notification_id")
    to_email = data.get("to_email")
    subject = data.get("subject")
    message = data.get("message")
    from_user = data.get("from_user", "Unknown")

    if not to_email or not subject:
        return jsonify({"error": "Recipient and subject required"}), 400

    try:
        # Generate "Mark as Done" link if we have a notification ID
        done_link = ""
        if notification_id:
            token = generate_action_token(notification_id)
            base_url = request.host_url.rstrip("/")
            done_link = f"{base_url}/done/{notification_id}?token={token}"

            # Append the "Mark as Done" link to the message
            message += f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            message += f"✅ MARK THIS TASK AS DONE:\n{done_link}\n"
            message += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            message += f"Click the link above when you've completed this task.\n"
            message += f"It will automatically update the PEXA Tracker.\n"

        # Send via Graph API using the PEXA mailbox
        send_mailbox = os.getenv("SEND_FROM_MAILBOX", graph_client.mailbox)
        cc_address = os.getenv("CC_MAILBOX", "teams@legalworld.com.au")
        graph_client.send_email(to_email, subject, message, from_mailbox=send_mailbox, cc_emails=cc_address)

        # Add a note and auto-set status to "reviewed" (To Review)
        if notification_id:
            add_note(notification_id, f"Task emailed to {to_email} by {from_user} (with Mark as Done link)", from_user)
            update_notification_status(notification_id, "reviewed", from_user)
            update_emailed_info(notification_id, to_email, datetime.utcnow().isoformat())

        logger.info(f"Task email sent to {to_email} for notification {notification_id} by {from_user} - status set to reviewed")
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
        return make_response(f"""
        <!DOCTYPE html>
        <html><head><title>Already Done</title>
        <style>body{{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f5f5}}
        .card{{background:white;padding:40px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.1);text-align:center;max-width:500px}}
        h2{{color:#3498db}}p{{color:#666}}.info{{background:#f0f8ff;padding:12px;border-radius:8px;margin:16px 0}}</style></head>
        <body><div class="card"><h2>Already Marked as Done</h2>
        <p>This task was already marked as actioned.</p>
        <div class="info"><strong>Matter #{notification["matter_number"]}</strong><br>{notification["notification_type"]}</div>
        <p>Actioned by {notification.get("actioned_by", "Unknown")} at {notification.get("actioned_at", "Unknown")}</p>
        </div></body></html>
        """, 200)

    # Mark as actioned
    update_notification_status(notification_id, "actioned", user="Via Email Link")
    add_note(notification_id, "Marked as done via email link", "Email Link")
    logger.info(f"Notification {notification_id} marked as done via email link")

    return make_response(f"""
    <!DOCTYPE html>
    <html><head><title>Task Complete</title>
    <style>body{{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f5f5}}
    .card{{background:white;padding:40px;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,0.1);text-align:center;max-width:500px}}
    h2{{color:#27ae60}}.check{{font-size:64px;margin-bottom:16px}}p{{color:#666}}
    .info{{background:#f0fff4;padding:12px;border-radius:8px;margin:16px 0}}</style></head>
    <body><div class="card">
    <div class="check">✅</div>
    <h2>Task Marked as Done!</h2>
    <div class="info"><strong>Matter #{notification["matter_number"]}</strong><br>{notification["notification_type"]}<br>{notification["summary"][:100]}</div>
    <p>This notification has been marked as actioned in the PEXA Tracker.</p>
    </div></body></html>
    """, 200)


# --- 48-Hour Reminder Check ---

def check_overdue_tasks():
    """Check for tasks emailed more than 48 hours ago that haven't been actioned.
    Sends a reminder email to the original recipient with the Mark as Done link
    and asks them to contact Sheriff/Jai if they need help."""
    try:
        overdue = get_overdue_emailed_tasks(hours=48)
        if not overdue:
            logger.info("Overdue check: no overdue tasks found")
            return

        # Base URL for Mark as Done links (no request context in scheduled jobs)
        base_url = os.getenv("APP_URL", "https://pexa-notification-tracker.onrender.com")
        send_mailbox = os.getenv("SEND_FROM_MAILBOX", graph_client.mailbox)

        reminder_count = 0
        for task in overdue:
            try:
                nid = task["id"]
                to_email = task["emailed_to"]
                matter = task.get("matter_number", "Unknown")
                ntype = task.get("notification_type", "PEXA Notification")
                summary = task.get("summary", "")[:200]

                # Generate Mark as Done link
                token = generate_action_token(nid)
                done_link = f"{base_url}/done/{nid}?token={token}"

                # Build reminder email
                subject = f"REMINDER: Outstanding PEXA Task - Matter #{matter}"
                body = f"Hi,\n\n"
                body += f"This is a reminder that the following PEXA task was sent to you over 48 hours ago and has not yet been marked as complete:\n\n"
                body += f"Matter #: {matter}\n"
                body += f"Type: {ntype}\n"
                body += f"Summary: {summary}\n\n"
                body += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                body += f"If you have completed this task, please click the link below to mark it as done:\n\n"
                body += f"✅ MARK AS DONE: {done_link}\n\n"
                body += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                body += f"If this task has NOT been completed, or if you need help, please email:\n"
                body += f"  • sheriff@legalworld.com.au\n"
                body += f"  • jai@legalworld.com.au\n\n"
                body += f"Please let them know what is happening with this task and if you require any assistance.\n\n"
                body += f"Thank you,\nPEXA Notification Tracker\n"

                # Send reminder - CC Sheriff and Jai so they're aware
                cc_emails = "sheriff@legalworld.com.au,jai@legalworld.com.au"
                graph_client.send_email(to_email, subject, body, from_mailbox=send_mailbox, cc_emails=cc_emails)

                # Mark reminder as sent so we don't send again
                mark_reminder_sent(nid)
                add_note(nid, f"48-hour reminder sent to {to_email}", "System")

                reminder_count += 1
                logger.info(f"Reminder sent to {to_email} for notification {nid} (Matter #{matter})")

            except Exception as e:
                logger.error(f"Failed to send reminder for notification {task['id']}: {e}")

        logger.info(f"Overdue check complete: {reminder_count} reminders sent out of {len(overdue)} overdue tasks")

    except Exception as e:
        logger.error(f"Overdue task check failed: {e}")


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
