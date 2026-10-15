"""Email to support, through the Cloudflare Worker in front of the domain (deploy/cloudflare/worker.js, POST
/_edge/mail): Cloudflare Email Routing sends it from no-reply@algo-rhythm.in to support's verified inbox.

Nothing here holds up a request or raises: a failed email is logged, and the account can still be approved by hand
(python -m algo_rhythm.accounts activate NAME). Env: EDGE_SECRET (shared with the Worker; no secret, no email) and
EDGE_MAIL_URL (default https://algo-rhythm.in/_edge/mail).
"""
import datetime
import html
import logging
import os
from zoneinfo import ZoneInfo

import requests

from algo_rhythm import accounts

log = logging.getLogger("algo_rhythm.notify")
MAIL_URL = "https://algo-rhythm.in/_edge/mail"
IST = ZoneInfo("Asia/Kolkata")
ROSE, TEAL, INK = "#e8679a", "#2dd4bf", "#0e0e10"


def send(subject, text, html_body, post=requests.post):
    """Ask the Worker to email support. True when it says it sent it."""
    secret = os.environ.get("EDGE_SECRET")
    if not secret:
        log.info("no EDGE_SECRET: not emailing %r", subject)
        return False
    try:
        r = post(os.environ.get("EDGE_MAIL_URL", MAIL_URL), headers={"x-edge-secret": secret},
                 json={"subject": subject, "text": text, "html": html_body}, timeout=15)
        if r.status_code != 200:
            log.warning("email %r not sent: Worker answered HTTP %d %s", subject, r.status_code, r.text[:200])
            return False
    except requests.RequestException as e:
        log.warning("email %r not sent: %s", subject, type(e).__name__)
        return False
    log.info("emailed support: %s", subject)
    return True


def signup_email(user, ip, site):
    """(subject, text, html) for a new signup, with its Approve link on `site` (the address the user signed up on)."""
    token = accounts.approval_token(user["id"], user["username"])
    link = f"https://{site}/admin/approve?t={token}"
    when = user["created_at"]
    when = (when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)).astimezone(IST)
    staging = site.startswith("staging.")
    rows = [("Username", user["username"]), ("Email", user["email"]),
            ("Signed up", when.strftime("%d %b %Y, %I:%M %p IST")), ("From address", ip or "unknown"),
            ("Site", site + (" (staging)" if staging else "")), ("Account id", str(user["id"]))]
    subject = ("[staging] " if staging else "") + "New User onboard"
    text = "\n".join([f"{subject}: {user['username']} signed up and is waiting for approval.", ""]
                     + [f"{k}: {v}" for k, v in rows]
                     + ["", f"Approve: {link}", "",
                        f"The link works for {accounts.APPROVE_DAYS} days and shows these details again before "
                        "anything changes."])
    esc = html.escape
    cells = "".join(
        f'<tr><td style="padding:10px 0;color:#6b7280;font-size:14px;width:130px;vertical-align:top">{esc(k)}</td>'
        f'<td style="padding:10px 0;color:#111827;font-size:15px;font-weight:600;word-break:break-all">{esc(v)}</td></tr>'
        for k, v in rows)
    body = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f3f4f6">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:32px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:16px;overflow:hidden;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
  <tr><td style="background:{INK};padding:22px 28px">
    <span style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:.2px">alg<span style="color:{ROSE}">&#9679;</span><span style="color:{TEAL}">-</span>rhythm</span>
    {'<span style="float:right;background:#f59e0b;color:#111;font-size:12px;font-weight:700;padding:3px 8px;border-radius:6px">STAGING</span>' if staging else ''}
  </td></tr>
  <tr><td style="padding:28px 28px 8px">
    <h1 style="margin:0 0 6px;font-size:22px;color:#111827">New User onboard</h1>
    <p style="margin:0;color:#4b5563;font-size:15px;line-height:1.5"><b>{esc(user["username"])}</b> signed up to host parties and is waiting for approval.</p>
  </td></tr>
  <tr><td style="padding:12px 28px 4px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #e5e7eb">{cells}</table>
  </td></tr>
  <tr><td align="center" style="padding:24px 28px 8px">
    <a href="{esc(link)}" style="display:inline-block;background:{ROSE};color:#ffffff;text-decoration:none;font-size:16px;font-weight:700;padding:14px 34px;border-radius:999px">Approve account</a>
  </td></tr>
  <tr><td style="padding:8px 28px 28px;color:#6b7280;font-size:13px;line-height:1.5;text-align:center">
    The button opens a page that shows these details again; nothing changes until you press Approve there.
    The link works for {accounts.APPROVE_DAYS} days.
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
    return subject, text, body


def new_signup(username, ip, site):
    """Email support about a signup. Called after the response is sent; never raises."""
    try:
        user = accounts.signup(username)
        if user:
            send(*signup_email(user, ip, site))
    except Exception as e:  # an email must never break a signup
        log.warning("signup email for %s failed: %s", username, type(e).__name__)
