"""Host accounts: who may start a party. Guests never need one.

A signup is saved inactive; someone turns it on by hand (below) before it can sign in. Passwords are kept as scrypt
hashes (standard library, per-user salt); a sign-in gets a random session token, of which only the sha256 is stored,
sent back as an HttpOnly cookie. Failed sign-ins and signups are rate limited per address and per name.

    python -m algo_rhythm.accounts add NAME EMAIL [--active]    a new account; prints its generated password once
    python -m algo_rhythm.accounts activate NAME | deactivate NAME
    python -m algo_rhythm.accounts list
(DATABASE_URL picks the database, as everywhere.)
"""
import argparse
import datetime
import hashlib
import hmac
import re
import secrets
import sys

from sqlalchemy import func, select

from algo_rhythm import db, repository

SESSION_DAYS = 30
COOKIE = "algo_session"
USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{2,31}")
EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]{1,189}\.[A-Za-z]{2,24}")
PASSWORD_MIN, PASSWORD_MAX = 8, 128
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
LOGIN_FAILS = 10       # failed sign-ins allowed per address, and per name, in RATE_WINDOW_MIN
SIGNUPS = 5            # signups allowed per address in RATE_WINDOW_MIN
RATE_WINDOW_MIN = 15


class AuthError(Exception):
    """A sign-in or signup refused; the message is safe to show."""


class Limited(Exception):
    pass


def hash_password(password):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${salt.hex()}${h.hex()}"


def check_password(password, stored):
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(h) // 2)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(got.hex(), h)


_DUMMY = hash_password(secrets.token_hex(8))  # checked when the name is unknown, so both cases take as long


def _clean(username, email=None, password=None):
    username = str(username or "").strip().lower()
    if not USERNAME.fullmatch(username):
        raise AuthError("Username: 3 to 32 letters, digits, '.', '_' or '-', starting with a letter or digit.")
    if email is not None:
        email = str(email).strip()
        if len(email) > 254 or not EMAIL.fullmatch(email):
            raise AuthError("That email address does not look right.")
    if password is not None and not (isinstance(password, str) and PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        raise AuthError(f"Password: {PASSWORD_MIN} to {PASSWORD_MAX} characters.")
    return username, email


def _limit(s, key, kind, most):
    since = db.now() - datetime.timedelta(minutes=RATE_WINDOW_MIN)
    n = s.scalar(select(func.count()).select_from(db.AuthEvent).where(
        db.AuthEvent.key == key, db.AuthEvent.kind == kind, db.AuthEvent.created_at >= since))
    if n >= most:
        raise Limited("Too many tries. Wait a few minutes and try again.")


def _note(key, kind):
    with repository.session() as s:
        s.add(db.AuthEvent(key=key[:128], kind=kind))
        s.commit()


def create(username, email, password, active=False):
    """A new account (inactive unless active=True). AuthError when the name or email is taken or not valid."""
    username, email = _clean(username, email, password)
    with repository.session() as s:
        if s.scalar(select(db.User.id).where(db.User.username == username)):
            raise AuthError("That username is taken.")
        if s.scalar(select(db.User.id).where(func.lower(db.User.email) == email.lower())):
            raise AuthError("An account with that email already exists.")
        s.add(db.User(username=username, email=email, password_hash=hash_password(password), active=active,
                      approved_at=db.now() if active else None))
        s.commit()
    return username


def register(username, email, password, ip):
    """A signup from the page: saved inactive, waiting for someone to approve it."""
    with repository.session() as s:
        _limit(s, f"ip:{ip}", "signup", SIGNUPS)
    name = create(username, email, password, active=False)
    _note(f"ip:{ip}", "signup")
    return name


def login(username, password, ip):
    """A session token for an active account. AuthError (wrong name or password, or not approved yet) or Limited."""
    try:
        username, _ = _clean(username)
    except AuthError:
        username = ""
    with repository.session() as s:
        _limit(s, f"ip:{ip}", "login_fail", LOGIN_FAILS)
        _limit(s, f"user:{username}", "login_fail", LOGIN_FAILS)
        user = s.scalar(select(db.User).where(db.User.username == username)) if username else None
    if not check_password(str(password or ""), user.password_hash if user else _DUMMY) or user is None:
        _note(f"ip:{ip}", "login_fail")
        _note(f"user:{username}", "login_fail")
        raise AuthError("Wrong username or password.")
    if not user.active:
        raise AuthError("Your account is waiting for approval (up to 24 hours after signing up).")
    token = secrets.token_urlsafe(32)
    with repository.session() as s:
        s.add(db.LoginSession(token_hash=_hash(token), user_id=user.id,
                              expires_at=db.now() + datetime.timedelta(days=SESSION_DAYS)))
        s.commit()
    return token, user.username


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def user_for(token):
    """{"id", "username"} of the active account signed in with this token, or None (no token, expired, deactivated)."""
    if not token or len(token) > 100:
        return None
    with repository.session() as s:
        row = s.execute(select(db.User.id, db.User.username, db.User.active, db.LoginSession.expires_at)
                        .join(db.LoginSession, db.LoginSession.user_id == db.User.id)
                        .where(db.LoginSession.token_hash == _hash(token))).first()
    if not row or not row.active:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=datetime.timezone.utc)
    return {"id": row.id, "username": row.username} if expires > db.now() else None


def logout(token):
    if token:
        with repository.session() as s:
            row = s.get(db.LoginSession, _hash(token))
            if row:
                s.delete(row)
                s.commit()


def set_active(username, active):
    with repository.session() as s:
        user = s.scalar(select(db.User).where(db.User.username == username.strip().lower()))
        if not user:
            raise AuthError(f"no account {username!r}")
        user.active = active
        user.approved_at = db.now() if active else None
        s.commit()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("email")
    a.add_argument("--active", action="store_true", help="able to sign in at once (default: waiting for approval)")
    for cmd in ("activate", "deactivate"):
        sub.add_parser(cmd).add_argument("username")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "add":
            password = secrets.token_urlsafe(12)
            name = create(args.username, args.email, password, active=args.active)
            print(f"{name}\t{password}\t{'active' if args.active else 'waiting for approval'}")
        elif args.cmd in ("activate", "deactivate"):
            set_active(args.username, args.cmd == "activate")
            print(f"{args.username}: {'active' if args.cmd == 'activate' else 'inactive'}")
        else:
            with repository.session() as s:
                for u in s.scalars(select(db.User).order_by(db.User.created_at)):
                    print(f"{u.username}\t{u.email}\t{'active' if u.active else 'waiting'}\t{u.created_at:%Y-%m-%d %H:%M}")
    except AuthError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
