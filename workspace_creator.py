"""
workspace_creator.py

Watches the "Post Exchange Automation" folder in Rahul's Outlook mailbox for
emails sent by Actionstep when a matter moves to the POST EXCHANGE (P) step.

Each email is parsed and a workspace is auto-created in the Legal World portal
(Firebase Firestore) via the createWorkspaceFromEmail Cloud Function.

Processed emails are moved to a "Processed" subfolder inside the watched folder
to prevent re-processing on the next poll.

Environment variables required:
  FIREBASE_WORKSPACE_FUNCTION_URL  — full URL of the createWorkspaceFromEmail function
  FIREBASE_WORKSPACE_TOKEN         — shared secret token (query param ?token=...)
  POST_EXCHANGE_FOLDER_NAME        — Outlook folder to watch (default: "Post Exchange Automation")
  POST_EXCHANGE_ARCHIVE_FOLDER     — subfolder for processed emails (default: "Processed")

The GraphClient (graph_client.py) already has Azure AD credentials set up.
"""

import os
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

FIREBASE_FUNCTION_URL = os.getenv("FIREBASE_WORKSPACE_FUNCTION_URL", "")
FIREBASE_TOKEN        = os.getenv("FIREBASE_WORKSPACE_TOKEN", "")
FOLDER_NAME           = os.getenv("POST_EXCHANGE_FOLDER_NAME", "Post Exchange Automation")
ARCHIVE_FOLDER_NAME   = os.getenv("POST_EXCHANGE_ARCHIVE_FOLDER", "Processed")

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"


def _html_to_text(html):
    """Strip HTML tags to get plain text."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text(separator="\n").splitlines()]
    return "\n".join(line for line in lines if line)


class WorkspaceCreator:
    """Polls a specific Outlook folder and creates portal workspaces from emails."""

    def __init__(self, graph_client):
        self.gc = graph_client
        self._folder_id   = None
        self._archive_id  = None

    # ── Folder helpers ──────────────────────────────────────────────────────

    def _find_folder(self):
        """Locate the Post Exchange Automation folder in the mailbox."""
        if self._folder_id:
            return self._folder_id

        url = f"{GRAPH_API_BASE}/users/{self.gc.mailbox}/mailFolders"
        data = self.gc._request("GET", url, params={"$top": 100})

        # Check top-level folders
        for folder in data.get("value", []):
            if folder["displayName"].lower() == FOLDER_NAME.lower():
                self._folder_id = folder["id"]
                logger.info(f"WorkspaceCreator: found folder '{FOLDER_NAME}'")
                return self._folder_id

        # Check one level of child folders
        for folder in data.get("value", []):
            try:
                child_url  = f"{GRAPH_API_BASE}/users/{self.gc.mailbox}/mailFolders/{folder['id']}/childFolders"
                child_data = self.gc._request("GET", child_url, params={"$top": 100})
                for child in child_data.get("value", []):
                    if child["displayName"].lower() == FOLDER_NAME.lower():
                        self._folder_id = child["id"]
                        logger.info(f"WorkspaceCreator: found folder '{FOLDER_NAME}' as child of '{folder['displayName']}'")
                        return self._folder_id
            except Exception:
                continue

        raise Exception(
            f"WorkspaceCreator: folder '{FOLDER_NAME}' not found in mailbox {self.gc.mailbox}. "
            f"Please create the folder in Outlook and configure an Actionstep rule to deliver emails there."
        )

    def _find_or_create_archive(self):
        """Find or create the 'Processed' subfolder inside the watched folder."""
        if self._archive_id:
            return self._archive_id

        parent_id = self._find_folder()
        url = f"{GRAPH_API_BASE}/users/{self.gc.mailbox}/mailFolders/{parent_id}/childFolders"

        try:
            data = self.gc._request("GET", url, params={"$top": 100})
            for folder in data.get("value", []):
                if folder["displayName"].lower() == ARCHIVE_FOLDER_NAME.lower():
                    self._archive_id = folder["id"]
                    return self._archive_id
        except Exception as e:
            logger.warning(f"WorkspaceCreator: error checking archive folder: {e}")

        # Create it
        result = self.gc._request("POST", url, json={"displayName": ARCHIVE_FOLDER_NAME})
        self._archive_id = result["id"]
        logger.info(f"WorkspaceCreator: created archive folder '{ARCHIVE_FOLDER_NAME}'")
        return self._archive_id

    def _move_to_archive(self, message_id):
        archive_id = self._find_or_create_archive()
        url = f"{GRAPH_API_BASE}/users/{self.gc.mailbox}/messages/{message_id}/move"
        try:
            self.gc._request("POST", url, json={"destinationId": archive_id})
        except Exception as e:
            logger.warning(f"WorkspaceCreator: failed to archive message {message_id[:20]}: {e}")

    # ── Inbox search (diagnostic) ─────────────────────────────────────────────

    def search_inbox_for_actionstep(self, max_results=20):
        """Fetch recent emails from inbox to find where Actionstep emails are landing."""
        # Get the inbox folder ID
        inbox_url = f"{GRAPH_API_BASE}/users/{self.gc.mailbox}/mailFolders/Inbox"
        try:
            inbox = self.gc._request("GET", inbox_url)
            inbox_id = inbox.get("id", "Inbox")
        except Exception:
            inbox_id = "Inbox"

        url = f"{GRAPH_API_BASE}/users/{self.gc.mailbox}/mailFolders/{inbox_id}/messages"
        params = {
            "$top":     max_results,
            "$orderby": "receivedDateTime desc",
            "$select":  "id,subject,receivedDateTime,from",
        }
        try:
            data = self.gc._request("GET", url, params=params)
            return [
                {
                    "subject":  m.get("subject", ""),
                    "from":     m.get("from", {}).get("emailAddress", {}).get("address", ""),
                    "received": m.get("receivedDateTime", ""),
                }
                for m in data.get("value", [])
            ]
        except Exception as e:
            return [{"error": str(e)}]

    def search_mailbox_for_actionstep(self, max_results=20):
        """Search entire mailbox for any emails from actionstep domain or with POST EXCHANGE in subject."""
        results = {}

        # Search for emails from actionstep.com domain across entire mailbox
        try:
            url = f"{GRAPH_API_BASE}/users/{self.gc.mailbox}/messages"
            params = {
                "$search":  '"actionstep"',
                "$top":     max_results,
                "$select":  "id,subject,receivedDateTime,from,parentFolderId",
            }
            data = self.gc._request("GET", url, params=params)
            results["search_actionstep"] = [
                {
                    "subject":  m.get("subject", ""),
                    "from":     m.get("from", {}).get("emailAddress", {}).get("address", ""),
                    "received": m.get("receivedDateTime", ""),
                }
                for m in data.get("value", [])
            ]
        except Exception as e:
            results["search_actionstep"] = [{"error": str(e)}]

        # Also search for POST EXCHANGE
        try:
            url = f"{GRAPH_API_BASE}/users/{self.gc.mailbox}/messages"
            params = {
                "$search":  '"POST EXCHANGE"',
                "$top":     max_results,
                "$select":  "id,subject,receivedDateTime,from",
            }
            data = self.gc._request("GET", url, params=params)
            results["search_post_exchange"] = [
                {
                    "subject":  m.get("subject", ""),
                    "from":     m.get("from", {}).get("emailAddress", {}).get("address", ""),
                    "received": m.get("receivedDateTime", ""),
                }
                for m in data.get("value", [])
            ]
        except Exception as e:
            results["search_post_exchange"] = [{"error": str(e)}]

        return results

    def search_all_folders_for_actionstep(self, max_results=10):
        """Search Junk + all top-level folders for recent emails that look like Actionstep notifications."""
        results = {}

        # Well-known folders to check (Graph API well-known names)
        well_known = ["junkemail", "deleteditems", "archive"]
        for wk in well_known:
            try:
                url = f"{GRAPH_API_BASE}/users/{self.gc.mailbox}/mailFolders/{wk}/messages"
                params = {
                    "$top":     max_results,
                    "$orderby": "receivedDateTime desc",
                    "$select":  "id,subject,receivedDateTime,from",
                }
                data = self.gc._request("GET", url, params=params)
                msgs = data.get("value", [])
                results[wk] = [
                    {
                        "subject":  m.get("subject", ""),
                        "from":     m.get("from", {}).get("emailAddress", {}).get("address", ""),
                        "received": m.get("receivedDateTime", ""),
                    }
                    for m in msgs
                ]
            except Exception as e:
                results[wk] = [{"error": str(e)}]

        # Also list ALL top-level folder names so we can see what folders exist
        try:
            folders_url = f"{GRAPH_API_BASE}/users/{self.gc.mailbox}/mailFolders"
            folders_data = self.gc._request("GET", folders_url, params={"$top": 100, "$select": "displayName,unreadItemCount,totalItemCount"})
            results["all_folders"] = [
                {"name": f["displayName"], "total": f.get("totalItemCount", 0), "unread": f.get("unreadItemCount", 0)}
                for f in folders_data.get("value", [])
            ]
        except Exception as e:
            results["all_folders"] = [{"error": str(e)}]

        return results

    # ── Email fetching ───────────────────────────────────────────────────────

    def fetch_emails(self, max_results=50):
        """Fetch unprocessed emails from the Post Exchange folder."""
        folder_id = self._find_folder()
        url = f"{GRAPH_API_BASE}/users/{self.gc.mailbox}/mailFolders/{folder_id}/messages"
        params = {
            "$top":     max_results,
            "$orderby": "receivedDateTime asc",
            "$select":  "id,subject,body,receivedDateTime,from",
        }
        data = self.gc._request("GET", url, params=params)
        emails = []
        for msg in data.get("value", []):
            body = msg.get("body", {})
            body_html = body.get("content", "") if body.get("contentType") == "html" else ""
            body_text = body.get("content", "") if body.get("contentType") == "text" else ""
            emails.append({
                "id":       msg["id"],
                "subject":  msg.get("subject", ""),
                "body_html": body_html,
                "body_text": body_text,
            })
        return emails

    # ── Workspace creation ───────────────────────────────────────────────────

    def _call_firebase(self, email_body):
        """POST the email body to the Firebase Cloud Function."""
        if not FIREBASE_FUNCTION_URL or not FIREBASE_TOKEN:
            raise Exception("WorkspaceCreator: FIREBASE_WORKSPACE_FUNCTION_URL and FIREBASE_WORKSPACE_TOKEN must be set")

        url = f"{FIREBASE_FUNCTION_URL}?token={FIREBASE_TOKEN}"
        response = requests.post(url, json={"emailBody": email_body}, timeout=30)
        response.raise_for_status()
        return response.json()

    # ── Main sync ────────────────────────────────────────────────────────────

    def sync(self):
        """
        Check the Post Exchange folder for new emails, create workspaces in
        Firebase for each, then archive the processed emails.

        Returns: dict with created, exists, error counts.
        """
        results = {"created": 0, "exists": 0, "errors": 0, "processed": 0}

        # Let exceptions from fetch_emails propagate so sync_workspaces() can
        # capture and surface the real error (e.g. auth failure, folder missing)
        emails = self.fetch_emails()

        for email in emails:
            results["processed"] += 1
            try:
                # Get plain text — prefer text, fall back to stripping HTML
                body = email["body_text"] or _html_to_text(email["body_html"])

                if not body.strip():
                    logger.warning(f"WorkspaceCreator: empty body in email {email['id'][:20]} subject='{email['subject']}'")
                    self._move_to_archive(email["id"])
                    continue

                result = self._call_firebase(body)
                status = result.get("status", "unknown")

                if status == "created":
                    results["created"] += 1
                    logger.info(
                        f"WorkspaceCreator: ✅ created workspace {result.get('wsId', '?')} "
                        f"for matter {result.get('matterNumber', '?')} (subject: '{email['subject']}')"
                    )
                elif status == "exists":
                    results["exists"] += 1
                    logger.info(
                        f"WorkspaceCreator: ⚠️  workspace already exists for matter "
                        f"{result.get('matterNumber', '?')} — skipping"
                    )
                else:
                    logger.warning(f"WorkspaceCreator: unexpected status '{status}' for email '{email['subject']}'")

            except Exception as e:
                results["errors"] += 1
                logger.error(f"WorkspaceCreator: error processing email '{email['subject']}': {e}")
                # Don't archive on error — leave in folder so it can be retried
                continue

            # Archive the email (whether created or already exists)
            self._move_to_archive(email["id"])

        if results["processed"] > 0:
            logger.info(
                f"WorkspaceCreator.sync complete: "
                f"{results['created']} created, {results['exists']} already existed, "
                f"{results['errors']} errors out of {results['processed']} emails"
            )

        return results
