"""Talk to a Bitwarden (or Vaultwarden) vault through the official `bw` CLI.

The CLI does all the encryption. This module only:
  * logs in with a personal API key (BW_CLIENTID / BW_CLIENTSECRET),
  * unlocks with the master password typed into the web UI (never stored),
  * lists login items, keeping only name, URLs, username, folder and dates,
  * moves single items to the Bitwarden trash.

The unlock "session key" lives only in this process's memory and is dropped
after BW_LOCK_MINUTES of inactivity, on Lock, or when the container restarts.
Secrets are passed to `bw` through environment variables, never on the command
line, so they don't show up in `ps`.
"""
import json
import os
import subprocess
import threading
import time

BW_BIN = os.environ.get("BW_BIN", "bw")
CLIENT_ID = os.environ.get("BW_CLIENTID", "")
CLIENT_SECRET = os.environ.get("BW_CLIENTSECRET", "")
SERVER = os.environ.get("BW_SERVER", "").strip()
LOCK_MINUTES = int(os.environ.get("BW_LOCK_MINUTES", "15") or 15)
APPDATA_DIR = os.environ.get(
    "BITWARDENCLI_APPDATA_DIR",
    os.path.join(os.path.dirname(os.environ.get("DATABASE_PATH", "/data/accounts.db")) or ".", "bitwarden"),
)

_lock = threading.RLock()
_session = None
_last_used = 0.0


class BitwardenError(RuntimeError):
    pass


def configured():
    return bool(CLIENT_ID and CLIENT_SECRET)


def _run(args, session=None, extra_env=None, timeout=60):
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("BW_SESSION", "BW_PASSWORD", "BW_CLIENTID", "BW_CLIENTSECRET")
    }
    env["BITWARDENCLI_APPDATA_DIR"] = APPDATA_DIR
    env.setdefault("HOME", APPDATA_DIR)
    env["BW_NOINTERACTION"] = "true"
    if session:
        env["BW_SESSION"] = session
    env.update(extra_env or {})
    os.makedirs(APPDATA_DIR, exist_ok=True)
    try:
        proc = subprocess.run(
            [BW_BIN, "--nointeraction", *args],
            env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
        )
    except FileNotFoundError:
        raise BitwardenError("The Bitwarden CLI (bw) isn't installed in this container.") from None
    except subprocess.TimeoutExpired:
        raise BitwardenError(f"Bitwarden CLI timed out ({args[0]}).") from None
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise BitwardenError(message[-1] if message else f"bw {args[0]} failed.")
    return proc.stdout


def _expire_if_idle():
    global _session
    if _session and time.monotonic() - _last_used > LOCK_MINUTES * 60:
        _session = None
        try:
            _run(["lock"])
        except BitwardenError:
            pass


def _touch():
    global _last_used
    _last_used = time.monotonic()


def status():
    """Return a dict: configured, state (unauthenticated/locked/unlocked), email, server, last_sync."""
    if not configured():
        return {"configured": False, "state": "unconfigured"}
    with _lock:
        _expire_if_idle()
        try:
            data = json.loads(_run(["status"], session=_session) or "{}")
        except (BitwardenError, json.JSONDecodeError) as exc:
            return {"configured": True, "state": "error", "error": str(exc)}
    state = data.get("status", "unauthenticated")
    if state == "unlocked" and not _session:
        state = "locked"  # the CLI may think it's unlocked, but we hold no key
    return {
        "configured": True,
        "state": state,
        "email": data.get("userEmail") or "",
        "server": data.get("serverUrl") or "https://vault.bitwarden.com",
        "last_sync": data.get("lastSync") or "",
        "lock_minutes": LOCK_MINUTES,
    }


def _ensure_logged_in():
    data = json.loads(_run(["status"]) or "{}")
    if data.get("status") != "unauthenticated":
        return
    if SERVER:
        _run(["config", "server", SERVER])
    _run(["login", "--apikey"], extra_env={"BW_CLIENTID": CLIENT_ID, "BW_CLIENTSECRET": CLIENT_SECRET})


def unlock(master_password):
    global _session
    if not configured():
        raise BitwardenError("Set BW_CLIENTID and BW_CLIENTSECRET first.")
    if not master_password:
        raise BitwardenError("Enter your master password.")
    with _lock:
        _ensure_logged_in()
        key = _run(
            ["unlock", "--passwordenv", "BW_UNLOCK_PASSWORD", "--raw"],
            extra_env={"BW_UNLOCK_PASSWORD": master_password},
        ).strip()
        if not key:
            raise BitwardenError("Unlock failed.")
        _session = key
        _touch()


def lock():
    global _session
    with _lock:
        _session = None
        try:
            _run(["lock"])
        except BitwardenError:
            pass


def _require_session():
    _expire_if_idle()
    if not _session:
        raise BitwardenError("The vault is locked. Unlock it on the Import page first.")
    _touch()
    return _session


def fetch_logins():
    """Sync with the server and return login items reduced to an allow-list of fields.

    Passwords, password history, TOTP seeds, passkeys, notes and custom fields are never copied.
    """
    with _lock:
        session = _require_session()
        _run(["sync"], session=session, timeout=180)
        folders = {f.get("id"): f.get("name", "") for f in json.loads(_run(["list", "folders"], session=session))}
        items = json.loads(_run(["list", "items"], session=session, timeout=180))

    logins = []
    for item in items:
        # type 1 = login; skip secure notes, cards, identities and trashed items
        if item.get("type") != 1 or item.get("deletedDate"):
            continue
        login = item.get("login") or {}
        folder = folders.get(item.get("folderId"), "")
        logins.append({
            "vault_id": item.get("id"),
            "name": item.get("name") or "",
            "uris": [u.get("uri") for u in login.get("uris") or [] if isinstance(u, dict)],
            "username": login.get("username") or "",
            "folder": "" if folder == "No Folder" else folder,
            "created": item.get("creationDate"),
            "updated": item.get("revisionDate"),
        })
    del items  # drop the full items (with passwords) as soon as possible
    return logins


def trash_item(vault_id):
    """Move one item to the Bitwarden trash (restorable for 30 days)."""
    with _lock:
        session = _require_session()
        _run(["delete", "item", vault_id], session=session)
