"""Apollo health check — an alarm that lives OUTSIDE Google (Jai, 23 Oct 2026).

On 23 Oct 2026 Google stopped starting Apollo's Cloud Functions at 8:15am. Every
function answered 500 in 0 s and staff found out by email ~90 minutes later. A
check inside Google would have gone down with it, so this one runs here, on the
Render tracker, every 5 minutes.

Each check is a CORS preflight (OPTIONS) to a function the portal uses all day.
A healthy function answers 204 from the platform's CORS layer without running
Apollo's code — no email, no write, nothing billed beyond the request. Two runs
in a row with most checks failing = DOWN: one email to Jai (Cc teams@), and one
more when it is back. Nothing in between, so a long outage is not a flood.

The fix that worked on 23 Oct is in the email: delete and recreate the
functions — `node tools/recreate-functions.js` in the ITA Page repo.
"""
import logging
import requests

logger = logging.getLogger(__name__)

BASE = "https://australia-southeast1-post-exchange-lw-platform.cloudfunctions.net"
ORIGIN = "https://post-exchange-lw-platform.web.app"
CHECKS = ["sendKeyDatesEmail", "sendItaEmail", "sendFsoEmail", "uploadSdForm", "actionstepLookup"]
ALERT_TO = ["jai@legalworld.com.au"]
ALERT_CC = ["teams@legalworld.com.au"]
ALERT_FROM = "jai@legalworld.com.au"
DOWN_AFTER = 2          # consecutive bad runs before the alarm
TIMEOUT = 20            # seconds per check

_state = {"bad_runs": 0, "down": False, "since": None, "last": None}


def check_one(name, session=requests):
    """→ (ok, detail). ok = the function answered and not with a 5xx."""
    try:
        r = session.options(f"{BASE}/{name}", timeout=TIMEOUT, headers={
            "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        return (r.status_code < 500, f"HTTP {r.status_code}")
    except requests.RequestException as e:
        return (False, f"no answer ({type(e).__name__})")


def run_checks(session=requests):
    results = {name: check_one(name, session) for name in CHECKS}
    failed = [n for n, (ok, _) in results.items() if not ok]
    bad = len(failed) * 2 > len(CHECKS)          # most of them failing, not one hiccup
    return {"bad": bad, "failed": failed, "results": {n: d for n, (_, d) in results.items()}}


def step(run, now_iso, state=None):
    """Advance the alarm with one run's outcome → 'down' | 'up' | None (no email)."""
    s = _state if state is None else state
    s["last"] = {"at": now_iso, **run}
    if run["bad"]:
        s["bad_runs"] += 1
        if not s["down"] and s["bad_runs"] >= DOWN_AFTER:
            s["down"], s["since"] = True, now_iso
            return "down"
        return None
    s["bad_runs"] = 0
    if s["down"]:
        s["down"] = False
        return "up"
    return None


def email_for(event, run, state):
    rows = "".join(f"<tr><td style='padding:2px 12px 2px 0'>{n}</td><td>{d}</td></tr>" for n, d in run["results"].items())
    if event == "down":
        subject = "🔴 Apollo is DOWN — functions not answering"
        lead = ("Apollo's functions are not answering (two checks in a row, 5 minutes apart). "
                "Staff will see 'Failed to fetch' / 'the connection dropped' on sends and uploads.")
        todo = ("<p><b>What fixed it on 23 Oct 2026:</b> delete and recreate the functions — in the ITA Page repo run "
                "<code>node tools/recreate-functions.js</code> (portal functions first; about 15 minutes until staff can send again). "
                "Check first that hosting and Firestore are fine and Google has no published outage.</p>")
    else:
        subject = "✅ Apollo is back — functions answering again"
        lead = f"Apollo's functions are answering again (down since {state.get('since') or '?'})."
        todo = ""
    html = (f"<div style='font-family:Arial,sans-serif;font-size:14px;color:#222'><p>{lead}</p>"
            f"<table style='font-size:13px;border-collapse:collapse'>{rows}</table>{todo}"
            f"<p style='font-size:12px;color:#888'>— Apollo health check, from the Render tracker (outside Google), every 5 minutes</p></div>")
    return subject, html


def tick(graph_client, now_iso, session=requests, state=None):
    """One scheduled run: check, advance, email on a change. Never raises."""
    s = _state if state is None else state
    try:
        run = run_checks(session)
        event = step(run, now_iso, s)
        if event:
            subject, html = email_for(event, run, s)
            graph_client.send_email(ALERT_TO, subject, "", from_mailbox=ALERT_FROM, cc_emails=ALERT_CC, body_html=html)
            logger.warning("apollo_health: %s emailed (%s failed)", event, ", ".join(run["failed"]) or "none")
        return event
    except Exception as e:      # the alarm must never take the tracker down
        logger.error("apollo_health: tick failed: %s", e)
        return None
