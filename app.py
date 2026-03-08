import os
import hmac
import hashlib
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, request, make_response
from apscheduler.schedulers.background import BackgroundScheduler

from database import init_db, get_notifications, get_notification, update_notification_status, \
    update_notification_assignment, add_note, get_stats, insert_notification
from email_parser import parse_pexa_email
from graph_client import GraphClient

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

graph_client = GraphClient()
last_sync_time = None
last_sync_status = "Never synced"


def sync_emails():
    """Fetch new emails from Graph API, parse them, and store in database."""
    global last_sync_time, last_sync_status
    try:
        # Fetch emails from last 7 days on first sync, then from last sync time
        since = None
        if last_sync_time:
            # Go back a bit to catch any we might have missed
            since = (last_sync_time - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

        logger.info(f"Syncing emails since {since}...")
        emails = graph_client.fetch_emails(since=since, max_results=100)

        new_count = 0
        for email in emails:
            parsed = parse_pexa_email(
                email_id=email["id"],
                subject=email["subject"],
                body_html=email["body_html"],
                body_text=email["body_text"],
                received_at=email["received_at"],
                sender=email["sender_email"],
            )
            if insert_notification(parsed):
                new_count += 1

        last_sync_time = datetime.utcnow()
        last_sync_status = f"OK - {new_count} new notifications"
        logger.info(f"Sync complete: {new_count} new notifications from {len(emails)} emails")
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
    stats["last_sync_time"] = last_sync_time.isoformat() if last_sync_time else None
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
        graph_client.send_email(to_email, subject, message, from_mailbox=send_mailbox)

        # Add a note to the notification recording the email
        if notification_id:
            add_note(notification_id, f"Task emailed to {to_email} by {from_user} (with Mark as Done link)", from_user)

        logger.info(f"Task email sent to {to_email} for notification {notification_id} by {from_user}")
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


# --- Startup ---

# Always init the database and scheduler (works for both gunicorn and flask dev)
init_db()

sync_interval = int(os.getenv("SYNC_INTERVAL_MINUTES", "5"))
scheduler = BackgroundScheduler()
scheduler.add_job(sync_emails, "interval", minutes=sync_interval, id="email_sync")
scheduler.start()

# Initial sync
logger.info("Running initial email sync...")
sync_emails()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    logger.info(f"Starting PEXA Tracker on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
