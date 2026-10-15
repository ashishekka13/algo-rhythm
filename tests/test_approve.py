"""Signup approval by email: a signup emails support an Approve link; the link's page changes nothing; its button does.
No song library needed: only accounts."""
import datetime
import os
import re
import tempfile
from pathlib import Path


def main():
    tmp = Path(tempfile.mkdtemp())
    os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp / 'approve.db'}"
    os.environ["EDGE_SECRET"] = "test-edge-secret"
    from fastapi.testclient import TestClient
    from algo_rhythm import accounts, notify, server

    sent = []
    notify.send = lambda subject, text, html_body: sent.append((subject, text, html_body)) or True
    to_users = []
    notify.send_user = lambda to, subject, text, html_body: to_users.append((to, subject, text, html_body)) or True
    c = TestClient(server.create_app())  # no `with`: the startup library load is not needed here

    r = c.post("/api/auth/register", json={"username": "dj.ravi", "email": "ravi@example.com", "password": "longenough1"})
    assert r.status_code == 201, r.text
    assert len(sent) == 1, "one email per signup"
    subject, text, body = sent[0]
    assert subject == "New User onboard" and "dj.ravi" in body and "ravi@example.com" in body and "Approve account" in body
    link = re.search(r"https://testserver/admin/approve\?t=(\S+)", text)
    assert link, text
    token = link.group(1)

    r = c.post("/api/auth/login", json={"username": "dj.ravi", "password": "longenough1"})
    assert r.status_code == 401 and r.json()["error"] == accounts.WAITING and "24" not in accounts.WAITING

    page = c.get(f"/admin/approve?t={token}")
    assert page.status_code == 200 and "Approve dj.ravi?" in page.text and "ravi@example.com" in page.text
    assert page.headers["referrer-policy"] == "no-referrer" and page.headers["cache-control"] == "no-store"
    assert not accounts.signup("dj.ravi")["active"], "opening the emailed link changes nothing (mail scanners open links)"

    r = c.post("/api/admin/approve", json={"t": token})
    assert r.status_code == 200 and r.json() == {"ok": True, "username": "dj.ravi", "already": False}, r.text
    assert len(to_users) == 1, "the user is emailed once, when approved"
    to, subj, txt, body = to_users[0]
    assert to == "ravi@example.com" and subj == "Your algo-rhythm account is approved"
    assert "Start listening" in body and 'href="https://testserver/"' in body and "https://testserver/" in txt
    assert c.post("/api/admin/approve", json={"t": token}).json()["already"] is True
    assert len(to_users) == 1, "approving again sends nothing"
    assert "Already approved" in c.get(f"/admin/approve?t={token}").text
    assert c.post("/api/auth/login", json={"username": "dj.ravi", "password": "longenough1"}).status_code == 200

    # forged, tampered, expired and wrong-account tokens approve nothing
    forged = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert c.post("/api/admin/approve", json={"t": forged}).status_code == 400
    other = token.replace("dj.ravi", "dj.ravj", 1)
    assert c.post("/api/admin/approve", json={"t": other}).status_code == 400
    assert c.post("/api/admin/approve", json={"t": "x"}).status_code == 400
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=accounts.APPROVE_DAYS + 1)
    try:
        accounts.check_approval(token, now=later)
        raise AssertionError("an expired link still works")
    except accounts.AuthError as e:
        assert "expired" in str(e)
    evil = c.get("/admin/approve", params={"t": "</script><script>alert(1)</script>"})
    assert "not valid" in evil.text and "<script>alert(1)" not in evil.text, "the token never lands in the page raw"

    # the email itself: staging is labelled, values are escaped
    user = accounts.signup("dj.ravi")
    subj, _, html_body = notify.signup_email(dict(user, username="<b>x</b>"), "1.2.3.4", "staging.algo-rhythm.in")
    assert subj == "[staging] New User onboard" and "STAGING" in html_body and "<b>x</b>" not in html_body

    # notify.send without a secret, or when the Worker refuses: no email, no exception
    from algo_rhythm import notify as n2
    import importlib
    importlib.reload(n2)
    class R:
        status_code, text = 502, "not sent"
    assert n2.send("s", "t", "h", post=lambda *a, **k: R()) is False
    os.environ.pop("RESEND_API_KEY", None)
    assert n2.send_user("a@b.c", "s", "t", "h", post=lambda *a, **k: 1 / 0) is False, "no key: no call"
    os.environ["RESEND_API_KEY"] = "re_test"
    got = {}
    class OK:
        status_code, text = 200, "{}"
    assert n2.send_user("a@b.c", "s", "t", "h", post=lambda url, **k: got.update(url=url, **k) or OK()) is True
    assert got["url"] == n2.RESEND_URL and got["headers"]["Authorization"] == "Bearer re_test"
    assert got["json"]["to"] == ["a@b.c"] and got["json"]["reply_to"] == "support@algo-rhythm.in"
    assert n2.send_user("a@b.c", "s", "t", "h", post=lambda *a, **k: R()) is False
    os.environ.pop("RESEND_API_KEY")
    s2, _, h2 = n2.approved_email({"username": "a<b", "email": "x@y.z"}, "staging.algo-rhythm.in")
    assert s2.startswith("[staging] ") and "a&lt;b" in h2 and 'href="https://staging.algo-rhythm.in/"' in h2
    os.environ.pop("EDGE_SECRET")
    assert n2.send("s", "t", "h", post=lambda *a, **k: 1 / 0) is False
    print("APPROVE PASS")


if __name__ == "__main__":
    main()
