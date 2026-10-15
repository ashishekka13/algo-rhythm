"""Email: to support about a signup, and to the user once their account is approved.

- To support: through the Cloudflare Worker in front of the domain (deploy/cloudflare/worker.js, POST /_edge/mail);
  Cloudflare Email Routing sends it from no-reply@ to support's verified inbox. Env EDGE_SECRET (shared with the
  Worker; no secret, no email) and EDGE_MAIL_URL (default https://algo-rhythm.in/_edge/mail).
- To a user: Cloudflare sends only to verified addresses, so through Resend (deploy/resend.sh), from no-reply@ with
  replies to support@. Env RESEND_API_KEY (a sending-only key; no key, no email).

Nothing here holds up a request or raises: a failed email is logged. An account can still be approved by hand
(python -m algo_rhythm.accounts activate NAME).
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
RESEND_URL = "https://api.resend.com/emails"
SENDER, REPLY_TO = "algo-rhythm <no-reply@algo-rhythm.in>", "support@algo-rhythm.in"
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


def send_user(to, subject, text, html_body, post=requests.post):
    """Email a user through Resend. True when Resend accepted it."""
    key = os.environ.get("RESEND_API_KEY")
    if not key:
        log.info("no RESEND_API_KEY: not emailing %r", subject)
        return False
    try:
        r = post(RESEND_URL, headers={"Authorization": f"Bearer {key}"}, timeout=15,
                 json={"from": SENDER, "to": [to], "reply_to": REPLY_TO, "subject": subject, "text": text,
                       "html": html_body})
        if r.status_code not in (200, 201):
            log.warning("email %r not sent: Resend answered HTTP %d %s", subject, r.status_code, r.text[:200])
            return False
    except requests.RequestException as e:
        log.warning("email %r not sent: %s", subject, type(e).__name__)
        return False
    log.info("emailed a user: %s", subject)
    return True


def _page(content, staging=False):
    """The emails' frame: the logo bar (with a STAGING badge on staging), then `content` rows, in a white card."""
    badge = ('<span style="float:right;background:#f59e0b;color:#111;font-size:12px;font-weight:700;padding:3px 8px;'
             'border-radius:6px">STAGING</span>') if staging else ""
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f3f4f6">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:32px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:16px;overflow:hidden;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
  <tr><td style="background:{INK};padding:22px 28px">
    <span style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:.2px">alg<span style="color:{ROSE}">&#9679;</span><span style="color:{TEAL}">-</span>rhythm</span>
    {badge}
  </td></tr>
{content}
</table>
</td></tr></table>
</body></html>"""


def _button(href, label):
    return (f'<a href="{html.escape(href)}" style="display:inline-block;background:{ROSE};color:#ffffff;'
            f'text-decoration:none;font-size:16px;font-weight:700;padding:14px 34px;border-radius:999px">'
            f'{html.escape(label)}</a>')


def approved_email(user, site):
    """(subject, text, html) telling a user their account is approved, with a Start listening button to `site`."""
    url = f"https://{site}/"
    staging = site.startswith("staging.")
    name = html.escape(user["username"])
    subject = ("[staging] " if staging else "") + "Your algo-rhythm account is approved"
    text = (f"Hi {user['username']},\n\nYour algo-rhythm account is approved. Sign in and start a party: the AI DJ picks "
            f"the songs, mixes them live and takes requests from your guests' phones.\n\nStart listening: {url}\n\n"
            f"Questions? Reply to this email or write to {REPLY_TO}.\n\nalgo-rhythm")
    content = f"""  <tr><td style="padding:32px 28px 8px">
    <h1 style="margin:0 0 10px;font-size:24px;color:#111827">You're in, {name}!</h1>
    <p style="margin:0 0 12px;color:#4b5563;font-size:16px;line-height:1.6">Your algo-rhythm account is approved. Sign in to host a party: the AI DJ picks the songs, mixes them live, and takes requests from your guests' phones.</p>
    <p style="margin:0;color:#4b5563;font-size:16px;line-height:1.6">Sign in with your username <b>{name}</b>.</p>
  </td></tr>
  <tr><td align="center" style="padding:24px 28px 8px">{_button(url, "Start listening")}</td></tr>
  <tr><td style="padding:16px 28px 28px;color:#6b7280;font-size:13px;line-height:1.5;text-align:center">
    Questions? Just reply to this email or write to <a href="mailto:{REPLY_TO}" style="color:{ROSE}">{REPLY_TO}</a>.
  </td></tr>"""
    return subject, text, _page(content, staging)


def account_approved(username, site):
    """Email a newly approved user. Called after the approval's response is sent; never raises."""
    try:
        user = accounts.signup(username)
        if user and user["active"]:
            subject, text, body = approved_email(user, site)
            send_user(user["email"], subject, text, body)
    except Exception as e:  # an email must never break an approval
        log.warning("approval email for %s failed: %s", username, type(e).__name__)


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
    content = f"""  <tr><td style="padding:28px 28px 8px">
    <h1 style="margin:0 0 6px;font-size:22px;color:#111827">New User onboard</h1>
    <p style="margin:0;color:#4b5563;font-size:15px;line-height:1.5"><b>{esc(user["username"])}</b> signed up to host parties and is waiting for approval.</p>
  </td></tr>
  <tr><td style="padding:12px 28px 4px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #e5e7eb">{cells}</table>
  </td></tr>
  <tr><td align="center" style="padding:24px 28px 8px">{_button(link, "Approve account")}</td></tr>
  <tr><td style="padding:8px 28px 28px;color:#6b7280;font-size:13px;line-height:1.5;text-align:center">
    The button opens a page that shows these details again; nothing changes until you press Approve there.
    The link works for {accounts.APPROVE_DAYS} days.
  </td></tr>"""
    return subject, text, _page(content, staging)


def new_signup(username, ip, site):
    """Email support about a signup. Called after the response is sent; never raises."""
    try:
        user = accounts.signup(username)
        if user:
            send(*signup_email(user, ip, site))
    except Exception as e:  # an email must never break a signup
        log.warning("signup email for %s failed: %s", username, type(e).__name__)
