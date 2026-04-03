import re
from bs4 import BeautifulSoup


# Notification type patterns and their categories
# category: "action_required" | "review" | "info"
NOTIFICATION_RULES = [
    # ACTION REQUIRED - needs someone to do something
    {"pattern": r"settlement.*(?:fail|reschedul|cancel|postpon)", "type": "Settlement Failed/Rescheduled", "category": "action_required"},
    {"pattern": r"settlement date.*(?:changed|updated|modified)", "type": "Settlement Date Changed", "category": "action_required"},
    {"pattern": r"settlement.*(?:time|date).*(?:changed|updated)", "type": "Settlement Date Changed", "category": "action_required"},
    {"pattern": r"(?:sign|signing).*required", "type": "Signing Required", "category": "action_required"},
    {"pattern": r"requires?\s+(?:your\s+)?(?:sign|action|attention)", "type": "Action Required", "category": "action_required"},
    {"pattern": r"duty.*(?:issue|problem|error|reject)", "type": "Duty Issue", "category": "action_required"},
    {"pattern": r"stamp duty.*(?:verif|issue|reject)", "type": "Stamp Duty Issue", "category": "action_required"},
    {"pattern": r"workspace.*(?:withdraw|cancel)", "type": "Workspace Withdrawn", "category": "action_required"},
    {"pattern": r"(?:lodg|submit).*(?:reject|fail|error)", "type": "Lodgement Rejected", "category": "action_required"},
    {"pattern": r"financial settlement schedule.*(?:amended|changed|updated)", "type": "Financial Schedule Changed", "category": "action_required"},
    {"pattern": r"(?:invit|added).*(?:workspace|participant)", "type": "Workspace Invitation", "category": "action_required"},

    # REVIEW - should be looked at
    {"pattern": r"loan proceeds.*(?:creat|added|line item)", "type": "Loan Proceeds Created", "category": "review"},
    {"pattern": r"(?:creat|added).*loan proceeds", "type": "Loan Proceeds Created", "category": "review"},
    {"pattern": r"new message from", "type": "New Message", "category": "review"},
    {"pattern": r"financial settlement.*(?:schedule|summary).*creat", "type": "Financial Schedule Created", "category": "review"},
    {"pattern": r"document.*(?:upload|attach|added)", "type": "Document Uploaded", "category": "review"},
    {"pattern": r"(?:transfer|mortgage|caveat|dealing).*(?:creat|lodg|prepar)", "type": "Dealing Prepared", "category": "review"},
    {"pattern": r"participant.*(?:join|accept|chang)", "type": "Participant Change", "category": "review"},
    {"pattern": r"land registry.*(?:deal|lodg)", "type": "Land Registry Dealing", "category": "review"},
    {"pattern": r"priority notice", "type": "Priority Notice", "category": "review"},
    {"pattern": r"duty.*(?:amount|calculat|lodg)", "type": "Duty Lodged", "category": "review"},
    {"pattern": r"(?:incoming|outgoing).*mortgage", "type": "Mortgage Activity", "category": "review"},
    {"pattern": r"(?:source\s*fund|fund\s*source)", "type": "Source Funds", "category": "review"},
    {"pattern": r"settlement\s*(?:check|verif)", "type": "Settlement Verification", "category": "review"},

    # INFO - usually can be ignored
    {"pattern": r"workspace.*(?:creat|status)", "type": "Workspace Update", "category": "info"},
    {"pattern": r"status.*(?:changed|updated)", "type": "Status Change", "category": "info"},
    {"pattern": r"settlement.*complet.*success", "type": "Settlement Completed", "category": "info"},
    {"pattern": r"please ignore", "type": "Ignore Notice", "category": "info"},
    {"pattern": r"(?:apologi|disregard|ignore.*(?:below|above|previous))", "type": "Ignore Notice", "category": "info"},
    {"pattern": r"no action required", "type": "No Action Required", "category": "info"},
    {"pattern": r"(?:reminder|notification).*(?:sent|deliver)", "type": "Reminder", "category": "info"},
]


def html_to_text(html_body):
    """Convert HTML email body to plain text."""
    if not html_body:
        return ""
    soup = BeautifulSoup(html_body, "html.parser")
    # Remove script and style elements
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Clean up whitespace
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_field(text, field_name):
    """Extract a field value from PEXA email text."""
    # Try multiple patterns for field extraction
    patterns = [
        rf"{field_name}\s*[:\-]?\s*(.+?)(?:\n|$)",
        rf"{field_name}\s+(.+?)(?:\n|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def extract_subscriber_ref(text):
    """Extract the subscriber reference (matter number)."""
    # Pattern: "SUBSCRIBER REF   71571"
    match = re.search(r"SUBSCRIBER\s+REF\s*[:\-]?\s*(\d+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    # Also check subject line pattern: "71571: ..."
    match = re.search(r"^(\d{4,6}):", text, re.MULTILINE)
    if match:
        return match.group(1)
    return None


def extract_settlement_date(text):
    """Extract settlement date and time from the structured section of the email.
    Only returns values that actually look like dates (contain dd/mm/yyyy)."""
    # First try: look for the structured "SETTLEMENT DATE & TIME" line followed by a date on next line
    match = re.search(
        r"SETTLEMENT\s+DATE\s*(?:&|AND)\s*TIME\s*\n\s*(\d{1,2}/\d{1,2}/\d{4}[^\n]*)",
        text, re.IGNORECASE
    )
    if match:
        return match.group(1).strip()

    # Second try: same line format
    match = re.search(
        r"SETTLEMENT\s+DATE\s*(?:&|AND)\s*TIME\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4}[^\n]*)",
        text, re.IGNORECASE
    )
    if match:
        return match.group(1).strip()

    # Third try: any line that has "SETTLEMENT DATE" near a date
    match = re.search(
        r"SETTLEMENT\s+DATE[^\n]*?(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM)\s*(?:AEST|AEDT)?)",
        text, re.IGNORECASE
    )
    if match:
        return match.group(1).strip()

    return None


def extract_workspace_number(text):
    """Extract PEXA workspace number."""
    match = re.search(r"WORKSPACE\s+NO\.?\s*[:\-]?\s*(PEXA\d+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    # Also try without PEXA prefix
    match = re.search(r"WORKSPACE\s+NO\.?\s*[:\-]?\s*(\S+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def extract_workspace_status(text):
    """Extract workspace status."""
    match = re.search(
        r"WORKSPACE\s+STATUS\s*[:\-]?\s*(.+?)(?:\n|$)",
        text, re.IGNORECASE
    )
    if match:
        return match.group(1).strip()
    return None


def classify_notification(text, subject=""):
    """Classify a notification based on its content."""
    combined = f"{subject}\n{text}".lower()

    for rule in NOTIFICATION_RULES:
        if re.search(rule["pattern"], combined, re.IGNORECASE):
            return rule["type"], rule["category"]

    # Default classification
    return "General Notification", "review"


def generate_summary(text, notification_type, subject=""):
    """Generate a short summary of the notification."""
    # If the subject has a pattern like "71571: Loan Proceeds line item has been created"
    match = re.search(r"\d+:\s*(.+)", subject)
    if match:
        return match.group(1).strip()

    # Try to extract the first meaningful line
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        # Skip boilerplate lines
        if any(skip in line.lower() for skip in [
            "sensitive details", "received this email", "pexa support",
            "property exchange australia", "all rights reserved",
            "subscriber ref", "settlement date", "workspace no",
            "workspace status", "note:", "pexa2"
        ]):
            continue
        if len(line) > 10:
            return line[:150]

    return notification_type


def extract_message_sender(text):
    """Extract the sender name from 'new message from' notifications."""
    match = re.search(
        r"new message from\s+(.+?)(?:\n|subject:|$)",
        text, re.IGNORECASE
    )
    if match:
        return match.group(1).strip()
    return None


def extract_from_party(text):
    """Extract the organization/party name that the PEXA notification is from."""
    if not text:
        return ""

    # Pattern 1: "New message from COMPANY NAME" or "message from COMPANY NAME"
    match = re.search(
        r"(?:new\s+)?message\s+from\s+(.+?)(?:\n|:|subject|$)",
        text, re.IGNORECASE
    )
    if match:
        name = match.group(1).strip().rstrip('.')
        if len(name) > 2 and name.upper() != "PEXA":
            return name

    # Pattern 2: Look for organization names in caps (common in PEXA messages)
    match = re.search(
        r"from\s+([A-Z][A-Z\s&.,()]+(?:PTY\s+LTD|LIMITED|LTD|CORP(?:ORATION)?|BANK|INC)?)",
        text
    )
    if match:
        name = match.group(1).strip().rstrip('.')
        if len(name) > 2 and name.upper() != "PEXA":
            return name

    # Pattern 3: "conversation message received" - check for org names in the text
    match = re.search(
        r"(?:conversation|message).*(?:from|by)\s+([A-Z][A-Z\s&]+(?:PTY\s+LTD|LIMITED|LTD|BANK)?)",
        text, re.IGNORECASE
    )
    if match:
        name = match.group(1).strip().rstrip('.')
        if len(name) > 2 and name.upper() != "PEXA":
            return name

    return ""


def parse_pexa_email(email_id, subject, body_html, body_text, received_at, sender):
    """Parse a PEXA notification email and return structured data."""
    # Get plain text from HTML if needed
    if body_html:
        text = html_to_text(body_html)
    elif body_text:
        text = body_text
    else:
        text = subject or ""

    # Extract structured fields
    matter_number = extract_subscriber_ref(text) or extract_subscriber_ref(subject or "")
    settlement_date = extract_settlement_date(text)
    workspace_number = extract_workspace_number(text)
    workspace_status = extract_workspace_status(text)

    # Classify the notification
    notification_type, category = classify_notification(text, subject or "")

    # Check for "please ignore" type messages which override category
    if re.search(r"(?:please\s+ignore|apologi.*ignore|disregard)", text, re.IGNORECASE):
        category = "info"
        notification_type = "Ignore Notice"

    # Generate summary
    summary = generate_summary(text, notification_type, subject or "")

    # Extract message sender for message notifications
    message_sender = extract_message_sender(text)
    if message_sender and notification_type == "New Message":
        summary = f"Message from {message_sender}: {summary}"

    # Extract the "from" party (bank, law firm, etc.)
    from_party = extract_from_party(text)

    return {
        "email_id": email_id,
        "received_at": received_at,
        "subject": subject or "",
        "matter_number": matter_number or "Unknown",
        "settlement_date": settlement_date,
        "workspace_number": workspace_number,
        "workspace_status": workspace_status,
        "notification_type": notification_type,
        "summary": summary[:200],
        "sender": sender or "",
        "full_body": text[:5000],
        "category": category,
        "message_from": from_party or "",
    }
