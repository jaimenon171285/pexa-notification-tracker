import msal
import requests
import os
import logging

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"


class GraphClient:
    def __init__(self):
        self.tenant_id = os.getenv("AZURE_TENANT_ID")
        self.client_id = os.getenv("AZURE_CLIENT_ID")
        self.client_secret = os.getenv("AZURE_CLIENT_SECRET")
        self.mailbox = os.getenv("PEXA_MAILBOX", "rahul@legalworld.com.au")
        self.folder_name = os.getenv("PEXA_FOLDER_NAME", "PEXA Notifications")
        self.archive_folder_name = "Processed"
        self._token = None
        self._folder_id = None
        self._archive_folder_id = None

    def _get_token(self):
        """Acquire an access token using client credentials flow."""
        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=authority,
            client_credential=self.client_secret,
        )
        result = app.acquire_token_for_client(
            scopes=["https://graph.microsoft.com/.default"]
        )
        if "access_token" in result:
            self._token = result["access_token"]
            return self._token
        else:
            error = result.get("error_description", result.get("error", "Unknown error"))
            logger.error(f"Failed to acquire token: {error}")
            raise Exception(f"Failed to acquire Graph API token: {error}")

    def _headers(self):
        if not self._token:
            self._get_token()
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _request(self, method, url, **kwargs):
        """Make an authenticated Graph API request with token refresh on 401."""
        response = requests.request(method, url, headers=self._headers(), **kwargs)
        if response.status_code == 401:
            self._get_token()
            response = requests.request(method, url, headers=self._headers(), **kwargs)
        response.raise_for_status()
        return response.json()

    def _find_folder_id(self):
        """Find the mail folder ID by name."""
        if self._folder_id:
            return self._folder_id

        url = f"{GRAPH_API_BASE}/users/{self.mailbox}/mailFolders"
        data = self._request("GET", url, params={"$top": 100})

        for folder in data.get("value", []):
            if folder["displayName"].lower() == self.folder_name.lower():
                self._folder_id = folder["id"]
                return self._folder_id

        # Check child folders (one level deep)
        for folder in data.get("value", []):
            child_url = f"{GRAPH_API_BASE}/users/{self.mailbox}/mailFolders/{folder['id']}/childFolders"
            try:
                child_data = self._request("GET", child_url, params={"$top": 100})
                for child in child_data.get("value", []):
                    if child["displayName"].lower() == self.folder_name.lower():
                        self._folder_id = child["id"]
                        return self._folder_id
            except Exception:
                continue

        raise Exception(
            f"Folder '{self.folder_name}' not found in mailbox {self.mailbox}. "
            f"Available folders: {[f['displayName'] for f in data.get('value', [])]}"
        )

    def fetch_emails(self, since=None, max_results=50):
        """
        Fetch emails from the PEXA notifications folder.
        Returns a list of email objects with id, subject, body, receivedDateTime, sender.
        """
        folder_id = self._find_folder_id()
        url = f"{GRAPH_API_BASE}/users/{self.mailbox}/mailFolders/{folder_id}/messages"

        params = {
            "$top": max_results,
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,body,bodyPreview,receivedDateTime,from,isRead",
        }

        if since:
            params["$filter"] = f"receivedDateTime ge {since}"

        data = self._request("GET", url, params=params)
        emails = []

        for msg in data.get("value", []):
            sender_email = ""
            sender_name = ""
            if msg.get("from", {}).get("emailAddress"):
                sender_email = msg["from"]["emailAddress"].get("address", "")
                sender_name = msg["from"]["emailAddress"].get("name", "")

            body = msg.get("body", {})
            emails.append({
                "id": msg["id"],
                "subject": msg.get("subject", ""),
                "body_html": body.get("content", "") if body.get("contentType") == "html" else "",
                "body_text": body.get("content", "") if body.get("contentType") == "text" else "",
                "body_preview": msg.get("bodyPreview", ""),
                "received_at": msg.get("receivedDateTime", ""),
                "sender_email": sender_email,
                "sender_name": sender_name,
            })

        # Check for pagination
        next_link = data.get("@odata.nextLink")
        while next_link and len(emails) < max_results:
            data = self._request("GET", next_link)
            for msg in data.get("value", []):
                sender_email = ""
                sender_name = ""
                if msg.get("from", {}).get("emailAddress"):
                    sender_email = msg["from"]["emailAddress"].get("address", "")
                    sender_name = msg["from"]["emailAddress"].get("name", "")
                body = msg.get("body", {})
                emails.append({
                    "id": msg["id"],
                    "subject": msg.get("subject", ""),
                    "body_html": body.get("content", "") if body.get("contentType") == "html" else "",
                    "body_text": body.get("content", "") if body.get("contentType") == "text" else "",
                    "body_preview": msg.get("bodyPreview", ""),
                    "received_at": msg.get("receivedDateTime", ""),
                    "sender_email": sender_email,
                    "sender_name": sender_name,
                })
            next_link = data.get("@odata.nextLink")

        return emails

    def send_email(self, to_email, subject, body_text, from_mailbox=None, cc_emails=None, body_html=None):
        """Send an email via Graph API using the specified mailbox.
        If body_html is provided, sends as HTML. Otherwise sends as plain text.
        to_email can be a single address string or a list of addresses."""
        sender = from_mailbox or self.mailbox
        url = f"{GRAPH_API_BASE}/users/{sender}/sendMail"
        if body_html:
            body_payload = {"contentType": "HTML", "content": body_html}
        else:
            body_payload = {"contentType": "Text", "content": body_text}

        # Support single or multiple TO recipients
        if isinstance(to_email, str):
            to_list = [to_email]
        else:
            to_list = list(to_email)

        message = {
            "subject": subject,
            "body": body_payload,
            "toRecipients": [
                {"emailAddress": {"address": addr.strip()}} for addr in to_list if addr.strip()
            ],
        }

        # Add CC recipients if provided
        if cc_emails:
            if isinstance(cc_emails, str):
                cc_emails = [cc_emails]
            message["ccRecipients"] = [
                {"emailAddress": {"address": email}} for email in cc_emails
            ]

        payload = {
            "message": message,
            "saveToSentItems": True,
        }
        response = requests.post(url, headers=self._headers(), json=payload)
        if response.status_code == 401:
            self._get_token()
            response = requests.post(url, headers=self._headers(), json=payload)
        if response.status_code == 202:
            return True
        response.raise_for_status()
        return True

    def _get_or_create_archive_folder(self):
        """Find or create the 'Processed' subfolder inside the PEXA folder."""
        if self._archive_folder_id:
            return self._archive_folder_id

        parent_folder_id = self._find_folder_id()

        # Check if 'Processed' subfolder already exists
        url = f"{GRAPH_API_BASE}/users/{self.mailbox}/mailFolders/{parent_folder_id}/childFolders"
        try:
            data = self._request("GET", url, params={"$top": 100})
            for folder in data.get("value", []):
                if folder["displayName"].lower() == self.archive_folder_name.lower():
                    self._archive_folder_id = folder["id"]
                    logger.info(f"Found existing archive folder: {self.archive_folder_name}")
                    return self._archive_folder_id
        except Exception as e:
            logger.warning(f"Error checking for archive folder: {e}")

        # Create the subfolder if it doesn't exist
        try:
            create_url = f"{GRAPH_API_BASE}/users/{self.mailbox}/mailFolders/{parent_folder_id}/childFolders"
            result = self._request("POST", create_url, json={
                "displayName": self.archive_folder_name,
            })
            self._archive_folder_id = result["id"]
            logger.info(f"Created archive folder: {self.archive_folder_name}")
            return self._archive_folder_id
        except Exception as e:
            logger.error(f"Failed to create archive folder: {e}")
            raise

    def move_email_to_archive(self, message_id):
        """Move an email to the Processed/archive subfolder."""
        try:
            archive_id = self._get_or_create_archive_folder()
            url = f"{GRAPH_API_BASE}/users/{self.mailbox}/messages/{message_id}/move"
            self._request("POST", url, json={
                "destinationId": archive_id,
            })
            return True
        except Exception as e:
            logger.warning(f"Failed to move email {message_id[:20]}... to archive: {e}")
            return False

    def fetch_emails_from_archive(self, since=None, max_results=200):
        """Fetch emails from the Processed/archive subfolder.
        Used for re-importing when database is empty but emails were already archived."""
        try:
            archive_id = self._get_or_create_archive_folder()
        except Exception as e:
            logger.info(f"No archive folder found, skipping archive import: {e}")
            return []

        url = f"{GRAPH_API_BASE}/users/{self.mailbox}/mailFolders/{archive_id}/messages"
        params = {
            "$top": max_results,
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,body,bodyPreview,receivedDateTime,from,isRead",
        }
        if since:
            params["$filter"] = f"receivedDateTime ge {since}"

        try:
            data = self._request("GET", url, params=params)
        except Exception as e:
            logger.warning(f"Failed to fetch from archive: {e}")
            return []

        emails = []
        for msg in data.get("value", []):
            sender_email = ""
            sender_name = ""
            if msg.get("from", {}).get("emailAddress"):
                sender_email = msg["from"]["emailAddress"].get("address", "")
                sender_name = msg["from"]["emailAddress"].get("name", "")
            body = msg.get("body", {})
            emails.append({
                "id": msg["id"],
                "subject": msg.get("subject", ""),
                "body_html": body.get("content", "") if body.get("contentType") == "html" else "",
                "body_text": body.get("content", "") if body.get("contentType") == "text" else "",
                "body_preview": msg.get("bodyPreview", ""),
                "received_at": msg.get("receivedDateTime", ""),
                "sender_email": sender_email,
                "sender_name": sender_name,
            })

        # Handle pagination
        next_link = data.get("@odata.nextLink")
        while next_link and len(emails) < max_results:
            try:
                data = self._request("GET", next_link)
            except Exception:
                break
            for msg in data.get("value", []):
                sender_email = ""
                sender_name = ""
                if msg.get("from", {}).get("emailAddress"):
                    sender_email = msg["from"]["emailAddress"].get("address", "")
                    sender_name = msg["from"]["emailAddress"].get("name", "")
                body = msg.get("body", {})
                emails.append({
                    "id": msg["id"],
                    "subject": msg.get("subject", ""),
                    "body_html": body.get("content", "") if body.get("contentType") == "html" else "",
                    "body_text": body.get("content", "") if body.get("contentType") == "text" else "",
                    "body_preview": msg.get("bodyPreview", ""),
                    "received_at": msg.get("receivedDateTime", ""),
                    "sender_email": sender_email,
                    "sender_name": sender_name,
                })
            next_link = data.get("@odata.nextLink")

        logger.info(f"Fetched {len(emails)} emails from archive/Processed folder")
        return emails

    # --- SharePoint / Excel methods ---

    _sp_drive_id = None
    _sp_item_id = None

    def resolve_sharing_url(self, sharing_url):
        """Resolve a SharePoint sharing URL to driveId and itemId.
        Caches the result so subsequent calls don't re-resolve."""
        if self._sp_drive_id and self._sp_item_id:
            return self._sp_drive_id, self._sp_item_id

        import base64
        # Encode sharing URL as base64url per MS Graph spec
        encoded = base64.b64encode(sharing_url.encode("utf-8")).decode("utf-8")
        encoded = encoded.rstrip("=").replace("/", "_").replace("+", "-")
        share_token = f"u!{encoded}"

        url = f"{GRAPH_API_BASE}/shares/{share_token}/driveItem"
        data = self._request("GET", url)
        self._sp_drive_id = data["parentReference"]["driveId"]
        self._sp_item_id = data["id"]
        logger.info(f"Resolved SharePoint file: driveId={self._sp_drive_id[:20]}..., itemId={self._sp_item_id[:20]}...")
        return self._sp_drive_id, self._sp_item_id

    def get_excel_worksheets(self, drive_id, item_id):
        """List all worksheet names in the Excel workbook."""
        url = f"{GRAPH_API_BASE}/drives/{drive_id}/items/{item_id}/workbook/worksheets"
        data = self._request("GET", url)
        return [ws["name"] for ws in data.get("value", [])]

    def get_excel_range(self, drive_id, item_id, sheet_name, range_addr):
        """Read a range from a worksheet. Returns the values as a 2D list."""
        import urllib.parse
        safe_sheet = urllib.parse.quote(sheet_name, safe="")
        url = f"{GRAPH_API_BASE}/drives/{drive_id}/items/{item_id}/workbook/worksheets/{safe_sheet}/range(address='{range_addr}')"
        data = self._request("GET", url)
        return data.get("values", [])

    def get_excel_used_range(self, drive_id, item_id, sheet_name):
        """Read the used range of a worksheet. Returns values as 2D list and the address."""
        import urllib.parse
        safe_sheet = urllib.parse.quote(sheet_name, safe="")
        url = f"{GRAPH_API_BASE}/drives/{drive_id}/items/{item_id}/workbook/worksheets/{safe_sheet}/usedRange"
        data = self._request("GET", url, params={"$select": "values,address"})
        return data.get("values", []), data.get("address", "")

    def update_excel_cell(self, drive_id, item_id, sheet_name, cell_addr, value, fill=None):
        """Write a value to a specific cell in a worksheet, with no text wrapping.
        Pass fill="#RRGGBB" to also shade the cell (used to mark Apollo's notes)."""
        import urllib.parse
        safe_sheet = urllib.parse.quote(sheet_name, safe="")
        url = f"{GRAPH_API_BASE}/drives/{drive_id}/items/{item_id}/workbook/worksheets/{safe_sheet}/range(address='{cell_addr}')"
        payload = {"values": [[value]]}
        response = requests.patch(url, headers=self._headers(), json=payload)
        if response.status_code == 401:
            self._get_token()
            response = requests.patch(url, headers=self._headers(), json=payload)
        response.raise_for_status()

        # Disable text wrapping only — don't touch cell colours unless asked to
        try:
            fmt_url = f"{url}/format"
            fmt_payload = {"wrapText": False}
            requests.patch(fmt_url, headers=self._headers(), json=fmt_payload)
        except Exception:
            pass  # Non-critical

        if fill:
            try:
                requests.patch(f"{url}/format/fill", headers=self._headers(),
                               json={"color": fill})
            except Exception:
                pass  # Non-critical — the note itself already landed

        return response.json()

    def get_excel_cell_fill(self, drive_id, item_id, sheet_name, cell_addr):
        """Read a cell's background colour, e.g. "#ADD8E6". Diagnostic use."""
        import urllib.parse
        safe_sheet = urllib.parse.quote(sheet_name, safe="")
        url = (f"{GRAPH_API_BASE}/drives/{drive_id}/items/{item_id}/workbook/worksheets/"
               f"{safe_sheet}/range(address='{cell_addr}')/format/fill")
        return self._request("GET", url).get("color")

    def insert_excel_column(self, drive_id, item_id, sheet_name, col_letter):
        """Insert a new column at the given column letter, shifting existing columns right."""
        import urllib.parse
        safe_sheet = urllib.parse.quote(sheet_name, safe="")
        range_addr = f"{col_letter}:{col_letter}"
        url = f"{GRAPH_API_BASE}/drives/{drive_id}/items/{item_id}/workbook/worksheets/{safe_sheet}/range(address='{range_addr}')/insert"
        payload = {"shift": "Right"}
        response = requests.post(url, headers=self._headers(), json=payload)
        if response.status_code == 401:
            self._get_token()
            response = requests.post(url, headers=self._headers(), json=payload)
        response.raise_for_status()
        return response.json()

    def test_connection(self):
        """Test the Graph API connection and return status info."""
        try:
            self._get_token()
            folder_id = self._find_folder_id()
            url = f"{GRAPH_API_BASE}/users/{self.mailbox}/mailFolders/{folder_id}"
            data = self._request("GET", url)
            return {
                "connected": True,
                "mailbox": self.mailbox,
                "folder": data.get("displayName", self.folder_name),
                "total_items": data.get("totalItemCount", 0),
                "unread_items": data.get("unreadItemCount", 0),
            }
        except Exception as e:
            return {
                "connected": False,
                "error": str(e),
                "mailbox": self.mailbox,
            }
