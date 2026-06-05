"""
imap_smtp — generic email connector for everything that isn't Gmail/Outlook.

Covers Yahoo, iCloud, AOL, Fastmail, ProtonMail (with bridge), any custom
domain, any ISP email. Each user can connect multiple accounts.

Storage:
    data/users/<username>/imap_accounts.json — file mode 0o600.
    Passwords are stored as plain text inside that file. Acceptable for
    the customer-install model (data lives on the customer's own box, file
    is owner-only). Future v2 can swap to Fernet encryption with a key in
    config.json without touching the public API.

Public functions:
    PROVIDER_PRESETS  — server settings keyed by provider id
    list_accounts(user_dir)              -> list[dict]
    add_account(user_dir, ...)           -> {id, ok, error?}
    remove_account(user_dir, account_id) -> bool
    test_account(user_dir, account_id)   -> {ok, error?}
    pull_inbox(user_dir, account_id, limit, query) -> list[dict]  (messages)
    send_email(user_dir, account_id, to, subject, body, in_reply_to=None)
        -> {ok, message_id?, error?}

Yahoo specifically: users must generate an "App Password" at
    https://login.yahoo.com/account/security
because the regular password doesn't work for IMAP/SMTP since 2020.
"""

from __future__ import annotations

import email
import email.message
import email.utils
import imaplib
import json
import logging
import os
import secrets
import smtplib
import socket
import ssl
import threading
import time
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

log = logging.getLogger("orbi.imap_smtp")
_LOCK = threading.Lock()

ACCOUNTS_FILE = "imap_accounts.json"


# Pre-built server settings for common providers. Keyed by provider id.
# Users pick from this list in the UI; "custom" prompts for all fields.
PROVIDER_PRESETS: dict[str, dict] = {
    "yahoo": {
        "label":        "Yahoo Mail",
        "imap_host":    "imap.mail.yahoo.com",
        "imap_port":    993,
        "imap_ssl":     True,
        "smtp_host":    "smtp.mail.yahoo.com",
        "smtp_port":    587,
        "smtp_starttls":True,
        "help_url":     "https://login.yahoo.com/account/security",
        "help":         ("Yahoo requires an App Password. Go to Account Security, "
                         "click 'Generate app password', and paste the 16-character "
                         "password here (NOT your normal Yahoo password)."),
    },
    "icloud": {
        "label":        "iCloud Mail",
        "imap_host":    "imap.mail.me.com",
        "imap_port":    993,
        "imap_ssl":     True,
        "smtp_host":    "smtp.mail.me.com",
        "smtp_port":    587,
        "smtp_starttls":True,
        "help_url":     "https://appleid.apple.com",
        "help":         ("iCloud requires an App-Specific Password. At appleid.apple.com, "
                         "Sign-in & Security → App-Specific Passwords → Generate. Paste "
                         "the password here."),
    },
    "aol": {
        "label":        "AOL Mail",
        "imap_host":    "imap.aol.com",
        "imap_port":    993,
        "imap_ssl":     True,
        "smtp_host":    "smtp.aol.com",
        "smtp_port":    587,
        "smtp_starttls":True,
        "help_url":     "https://login.aol.com/account/security",
        "help":         ("AOL requires an App Password generated under Account Security."),
    },
    "fastmail": {
        "label":        "Fastmail",
        "imap_host":    "imap.fastmail.com",
        "imap_port":    993,
        "imap_ssl":     True,
        "smtp_host":    "smtp.fastmail.com",
        "smtp_port":    587,
        "smtp_starttls":True,
        "help_url":     "https://www.fastmail.help/hc/en-us/articles/360060591293",
        "help":         ("Fastmail recommends generating an app-specific password under "
                         "Settings → Password & Security → App Passwords."),
    },
    "custom": {
        "label":        "Other / Custom",
        "imap_host":    "",
        "imap_port":    993,
        "imap_ssl":     True,
        "smtp_host":    "",
        "smtp_port":    587,
        "smtp_starttls":True,
        "help":         ("Enter your provider's IMAP and SMTP server names. "
                         "These are usually on the provider's 'Email setup' help page."),
    },
}


# ─── Storage ────────────────────────────────────────────────────────────

def _accounts_path(user_dir: Path) -> Path:
    return Path(user_dir) / ACCOUNTS_FILE


def _read_accounts(user_dir: Path) -> list[dict]:
    p = _accounts_path(user_dir)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")) or []
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"imap accounts read failed: {e}")
        return []


def _write_accounts(user_dir: Path, accounts: list[dict]) -> None:
    p = _accounts_path(user_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(accounts, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except (OSError, NotImplementedError):
            pass


def _redact(account: dict) -> dict:
    """Strip password before returning to the UI / API."""
    return {k: v for k, v in account.items() if k != "password"}


# ─── Public API ─────────────────────────────────────────────────────────

def list_accounts(user_dir: Path) -> list[dict]:
    """Accounts the user has connected. Passwords are stripped."""
    return [_redact(a) for a in _read_accounts(user_dir)]


def add_account(user_dir: Path, *, email_addr: str, password: str,
                provider: str = "custom",
                imap_host: str | None = None, imap_port: int | None = None,
                imap_ssl: bool | None = None,
                smtp_host: str | None = None, smtp_port: int | None = None,
                smtp_starttls: bool | None = None,
                label: str | None = None) -> dict:
    """Add an IMAP/SMTP account. Tests the connection first; if it fails,
    the account is not saved and we return the error verbatim so the user
    can fix it (wrong password is by far the most common cause)."""
    email_addr = (email_addr or "").strip()
    password = password or ""
    if not email_addr or "@" not in email_addr:
        return {"ok": False, "error": "valid email address required"}
    if not password:
        return {"ok": False, "error": "password required"}

    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["custom"])
    account = {
        "id":            "imap_" + secrets.token_urlsafe(8),
        "email":         email_addr,
        "label":         label or email_addr,
        "provider":      provider,
        "imap_host":     imap_host    if imap_host    is not None else preset["imap_host"],
        "imap_port":     int(imap_port    if imap_port    is not None else preset["imap_port"]),
        "imap_ssl":      imap_ssl     if imap_ssl     is not None else preset["imap_ssl"],
        "smtp_host":     smtp_host    if smtp_host    is not None else preset["smtp_host"],
        "smtp_port":     int(smtp_port    if smtp_port    is not None else preset["smtp_port"]),
        "smtp_starttls": smtp_starttls if smtp_starttls is not None else preset["smtp_starttls"],
        "password":      password,
        "added_at":      int(time.time()),
        "last_test":     None,
    }

    if not account["imap_host"] or not account["smtp_host"]:
        return {"ok": False, "error": "IMAP and SMTP server names are required"}

    test = _test_connection(account)
    if not test["ok"]:
        return {"ok": False, "error": test["error"]}
    account["last_test"] = int(time.time())

    accounts = _read_accounts(user_dir)
    # Refuse duplicates on email+host
    for a in accounts:
        if a["email"].lower() == email_addr.lower() and a["imap_host"] == account["imap_host"]:
            return {"ok": False, "error": f"already connected: {email_addr}"}
    accounts.append(account)
    _write_accounts(user_dir, accounts)
    return {"ok": True, "id": account["id"], "account": _redact(account)}


def remove_account(user_dir: Path, account_id: str) -> bool:
    accounts = _read_accounts(user_dir)
    before = len(accounts)
    accounts = [a for a in accounts if a["id"] != account_id]
    if len(accounts) < before:
        _write_accounts(user_dir, accounts)
        return True
    return False


def test_account(user_dir: Path, account_id: str) -> dict:
    """Re-test a saved account. Useful after a password change."""
    for a in _read_accounts(user_dir):
        if a["id"] == account_id:
            result = _test_connection(a)
            if result["ok"]:
                # Update last_test timestamp
                a["last_test"] = int(time.time())
                accounts = _read_accounts(user_dir)
                for x in accounts:
                    if x["id"] == account_id:
                        x["last_test"] = a["last_test"]
                _write_accounts(user_dir, accounts)
            return result
    return {"ok": False, "error": "account not found"}


def _test_connection(account: dict) -> dict:
    """Try to log in via IMAP and bail cleanly. Doesn't touch any mail."""
    try:
        with _imap_connect(account) as m:
            m.select("INBOX", readonly=True)
        return {"ok": True}
    except (imaplib.IMAP4.error, ssl.SSLError, socket.gaierror, socket.timeout, OSError) as e:
        return {"ok": False, "error": _friendly_imap_error(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _friendly_imap_error(e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    if "authenticationfailed" in low or "auth" in low or "login" in low or "invalid credentials" in low:
        return ("Login failed. Most providers (Yahoo / iCloud / AOL / Fastmail) "
                "require an APP PASSWORD instead of your normal password — your "
                "regular password will look correct but be rejected. Check your "
                "provider's 'app passwords' page.")
    if "name or service not known" in low or "gaierror" in low:
        return ("Can't reach the IMAP server. Double-check the host name.")
    if "timeout" in low or "timed out" in low:
        return ("Connection timed out. The server name may be wrong, or your "
                "internet is offline.")
    if "ssl" in low or "wrong version number" in low:
        return ("SSL error — make sure SSL is enabled on port 993 (or try "
                "STARTTLS on port 143).")
    return msg


# ─── IMAP connection helper ────────────────────────────────────────────

class _ImapCtx:
    """Tiny context manager so we always logout cleanly."""
    def __init__(self, m):
        self._m = m
    def __enter__(self):
        return self._m
    def __exit__(self, *a):
        try:
            self._m.logout()
        except Exception:
            pass

def _imap_connect(account: dict) -> _ImapCtx:
    host = account["imap_host"]
    port = int(account["imap_port"])
    if account.get("imap_ssl", True):
        m = imaplib.IMAP4_SSL(host, port, timeout=20)
    else:
        m = imaplib.IMAP4(host, port, timeout=20)
        m.starttls()
    m.login(account["email"], account["password"])
    return _ImapCtx(m)


# ─── Inbox pull ────────────────────────────────────────────────────────

def pull_inbox(user_dir: Path, account_id: str | None = None,
               limit: int = 50, query: str = "") -> list[dict]:
    """Pull recent messages from one account, or all if account_id is None.

    Returns the same shape as email_inbox._pull_gmail / _pull_outlook so the
    rest of the pipeline (tagging, flagging, aggregation) Just Works:
        [{id, provider, account_email, subject, from, snippet, date, unread, tags, ...}, ...]
    """
    accounts = _read_accounts(user_dir)
    if account_id:
        accounts = [a for a in accounts if a["id"] == account_id]
    out = []
    for a in accounts:
        try:
            out.extend(_pull_one(a, limit, query))
        except Exception as e:
            log.warning(f"imap pull failed for {a['email']}: {e}")
    return out


def _pull_one(account: dict, limit: int, query: str) -> list[dict]:
    """Pull the most recent `limit` messages from INBOX.

    Strategy:
      - UID SEARCH (not SEQ-based) so the id we cache for each message
        survives new arrivals, deletions, or another client moving
        things around. SEQ numbers reshuffle whenever the mailbox state
        changes — that caused Frank's iCloud test email body to show up
        under the McAfee header (the labels and bodies were paired off
        the wrong SEQ positions).
      - PEEK headers only (FROM, TO, SUBJECT, DATE) — much faster than
        full RFC822 and doesn't mark messages as read.
      - Final ordering by Date header is done in email_inbox.fetch_inbox
        (it sorts the combined gmail+outlook+imap result), so we don't
        need IMAP-side sort here.
    """
    out = []
    timeout_s = 40  # generous — Yahoo + 20 large headers can be slow
    try:
        m = imaplib.IMAP4_SSL(account["imap_host"], int(account["imap_port"]),
                              timeout=timeout_s) \
            if account.get("imap_ssl", True) \
            else imaplib.IMAP4(account["imap_host"], int(account["imap_port"]),
                               timeout=timeout_s)
        if not account.get("imap_ssl", True):
            m.starttls()
        m.login(account["email"], account["password"])
        try:
            m.select("INBOX", readonly=True)
            criteria = f'(SUBJECT "{query}")' if query else "ALL"
            typ, data = m.uid("search", None, criteria)
            if typ != "OK" or not data or not data[0]:
                return []
            uids = data[0].split()
            uids = list(reversed(uids))[:limit]  # highest UIDs are newest
            for uid in uids:
                typ, msg_data = m.uid(
                    "fetch", uid,
                    "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])",
                )
                if typ != "OK" or not msg_data:
                    continue
                raw = next((x[1] for x in msg_data if isinstance(x, tuple)), None)
                flags_raw = b"".join(x for x in msg_data if isinstance(x, bytes))
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                # uid here is bytes (e.g. b'12345'); _format_message stores
                # it as the message id so fetch_one_body can re-fetch it.
                out.append(_format_message(account, uid, msg, flags_raw))
        finally:
            try: m.logout()
            except Exception: pass
    except Exception as e:
        log.warning(f"_pull_one {account['email']}: {e}")
    return out


def fetch_one_body(user_dir: Path, account_id: str, uid,
                    max_chars: int = 4000) -> dict:
    """Fetch the full plain-text body of a specific message. Used by the
    "what does the email from X say" / "read X's email" fast-path so the
    LLM never has to guess the contents."""
    accounts = _read_accounts(user_dir)
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        return {"error": "account_not_found"}
    m = None
    try:
        m = imaplib.IMAP4_SSL(account["imap_host"], int(account["imap_port"]),
                              timeout=40) \
            if account.get("imap_ssl", True) \
            else imaplib.IMAP4(account["imap_host"], int(account["imap_port"]),
                               timeout=40)
        if not account.get("imap_ssl", True):
            m.starttls()
        m.login(account["email"], account["password"])
        m.select("INBOX", readonly=True)
        uid_b = str(uid).encode() if not isinstance(uid, bytes) else uid
        typ, data = m.uid("fetch", uid_b, "(BODY.PEEK[])")
        if typ != "OK" or not data:
            return {"error": "fetch_failed"}
        raw = next((x[1] for x in data if isinstance(x, tuple)), None)
        if not raw:
            return {"error": "no_body"}
        msg = email.message_from_bytes(raw)
        return {
            "subject":    _decode_header(msg.get("Subject")),
            "from":       _decode_header(msg.get("From")),
            "date":       msg.get("Date") or "",
            "body":       _extract_text(msg, max_chars=max_chars),
            "message_id": _decode_header(msg.get("Message-ID")) or "",
        }
    except Exception as e:
        return {"error": f"imap_error: {e}"}
    finally:
        if m is not None:
            try: m.logout()
            except Exception: pass


def _decode_header(value) -> str:
    if value is None:
        return ""
    parts = decode_header(value)
    bits = []
    for raw, enc in parts:
        if isinstance(raw, bytes):
            try:
                bits.append(raw.decode(enc or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                bits.append(raw.decode("utf-8", errors="replace"))
        else:
            bits.append(raw)
    return "".join(bits).strip()


def _extract_text(msg: email.message.Message, max_chars: int = 400) -> str:
    """Pull the plain-text body for a snippet. Falls back to stripped HTML."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp  = part.get_content_disposition()
            if ctype == "text/plain" and disp != "attachment":
                try:
                    payload = part.get_payload(decode=True) or b""
                    return _clean_text(payload.decode(
                        part.get_content_charset() or "utf-8",
                        errors="replace"))[:max_chars]
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    payload = part.get_payload(decode=True) or b""
                    return _strip_html(payload.decode(
                        part.get_content_charset() or "utf-8",
                        errors="replace"))[:max_chars]
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            text = payload.decode(msg.get_content_charset() or "utf-8",
                                  errors="replace")
            if msg.get_content_type() == "text/html":
                return _strip_html(text)[:max_chars]
            return _clean_text(text)[:max_chars]
        except Exception:
            return ""
    return ""


def _clean_text(s: str) -> str:
    return " ".join(s.split())


def _strip_html(s: str) -> str:
    import re
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
           .replace("&lt;", "<").replace("&gt;", ">")
           .replace("&quot;", '"').replace("&#39;", "'"))
    return _clean_text(s)


def _format_message(account: dict, uid: bytes, msg: email.message.Message,
                    flags_raw: bytes, folder: str = "INBOX") -> dict:
    subject = _decode_header(msg.get("Subject"))
    sender  = _decode_header(msg.get("From"))
    date_h  = msg.get("Date") or ""
    try:
        parsed = email.utils.parsedate_to_datetime(date_h)
        date_iso = parsed.astimezone().isoformat()
    except (TypeError, ValueError):
        date_iso = ""
    snippet = _extract_text(msg)
    seen = b"\\Seen" in flags_raw
    return {
        "id":            f"imap-{account['id']}-{uid.decode('ascii', errors='replace')}",
        "provider":      "imap",
        "account_email": account["email"],
        "folder":        folder,
        "subject":       subject or "(no subject)",
        "from":          sender,
        "snippet":       snippet,
        "date":          date_iso,
        "unread":        not seen,
        "tags":          [],
        "flagged":       False,
        "flag_reason":   "",
        "message_id":    _decode_header(msg.get("Message-ID")) or "",
    }


def list_folders(user_dir: Path, account_id: str | None = None) -> list[dict]:
    """List the folders available on each connected IMAP account.
    Diagnostic: confirms which folders Orby has access to. Each dict:
        {account_email, folders: [name, ...]}"""
    accounts = _read_accounts(user_dir)
    if account_id:
        accounts = [a for a in accounts if a["id"] == account_id]
    out = []
    for a in accounts:
        names = []
        try:
            with _imap_connect(a) as m:
                typ, data = m.list()
                if typ == "OK" and data:
                    for line in data:
                        if not line:
                            continue
                        s = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
                        # IMAP LIST format: (FLAGS) "/" "FolderName"
                        # Grab the part after the last `"`.
                        if '"' in s:
                            names.append(s.rsplit('"', 2)[-2])
                        else:
                            names.append(s.split()[-1])
        except Exception as e:
            log.warning(f"list_folders {a['email']}: {e}")
        out.append({"account_email": a["email"], "folders": names})
    return out


# ─── Send via SMTP ─────────────────────────────────────────────────────

def send_email(user_dir: Path, account_id: str, *, to: str,
               subject: str, body: str,
               in_reply_to: str | None = None) -> dict:
    """Send a message via the account's SMTP settings. Returns
    {ok, message_id?, error?}."""
    for a in _read_accounts(user_dir):
        if a["id"] == account_id:
            return _send_one(a, to, subject, body, in_reply_to)
    return {"ok": False, "error": "account not found"}


def _send_one(account: dict, to: str, subject: str, body: str,
              in_reply_to: str | None) -> dict:
    if not to or "@" not in to:
        return {"ok": False, "error": "valid 'to' address required"}
    msg = MIMEMultipart("alternative")
    msg["From"]    = account["email"]
    msg["To"]      = to
    msg["Subject"] = subject or "(no subject)"
    msg["Date"]    = email.utils.formatdate(localtime=True)
    msg_id         = email.utils.make_msgid(domain=account["email"].split("@", 1)[-1])
    msg["Message-ID"] = msg_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"]  = in_reply_to
    msg.attach(MIMEText(body, "plain", "utf-8"))

    host = account["smtp_host"]
    port = int(account["smtp_port"])
    try:
        if account.get("smtp_starttls", True):
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo(); s.starttls(); s.ehlo()
                s.login(account["email"], account["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(account["email"], account["password"])
                s.send_message(msg)
        return {"ok": True, "message_id": msg_id}
    except smtplib.SMTPAuthenticationError as e:
        return {"ok": False, "error": _friendly_imap_error(e)}
    except (smtplib.SMTPException, socket.timeout, OSError) as e:
        return {"ok": False, "error": str(e)}
