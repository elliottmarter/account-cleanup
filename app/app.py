import csv
import io
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from urllib.parse import urlsplit

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for

import bitwarden
import db
from bitwarden import BitwardenError
from importer import ExportError, parse_export, vault_record

STATUSES = {
    "unreviewed": "To review",
    "delete": "To delete",
    "requested": "Deletion requested",
    "deleted": "Deleted",
    "gone": "Site gone",
    "keep": "Keeping",
}
CLOSED = ("deleted", "gone")
SEGMENT_ORDER = ("deleted", "gone", "requested", "delete", "unreviewed", "keep")

# Oldest-touched accounts first: the longer since you changed it, the more likely it's forgotten.
PICK_ORDER = "priority DESC, COALESCE(NULLIF(vault_updated, ''), imported_at) ASC, id ASC"

SORTS = {
    "name": "name COLLATE NOCASE",
    "domain": "domain, name COLLATE NOCASE",
    "status": "status, name COLLATE NOCASE",
    "updated": "COALESCE(NULLIF(vault_updated, ''), imported_at), name COLLATE NOCASE",
}

TZ = ZoneInfo(os.environ.get("TZ") or "UTC")
APP_USERNAME = os.environ.get("APP_USERNAME", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
if not (APP_USERNAME and APP_PASSWORD):
    raise SystemExit("APP_USERNAME and APP_PASSWORD must both be set. Refusing to start.")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.secret_key = db.init_db()
app.teardown_appcontext(db.close_db)

_picks_lock = threading.Lock()


def now():
    return datetime.now(TZ)


def today():
    return now().date().isoformat()


def now_iso():
    return now().isoformat(timespec="seconds")


# "Deletion requested" accounts resurface on Today after this many days.
FOLLOW_UP_DAYS = 14


def safe_next(default):
    target = request.form.get("next") or request.args.get("next") or ""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return default


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@app.before_request
def require_auth():
    if request.path == "/healthz":
        return None
    auth = request.authorization
    if (
        auth
        and secrets.compare_digest((auth.username or "").encode(), APP_USERNAME.encode())
        and secrets.compare_digest((auth.password or "").encode(), APP_PASSWORD.encode())
    ):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Account Cleanup"'})


@app.before_request
def same_origin_for_vault_actions():
    # The vault endpoints can delete passwords, so refuse POSTs another website triggers.
    if request.method != "POST" or not request.path.startswith(("/bitwarden/", "/account/")):
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin and origin != "null" and urlsplit(origin).netloc != request.host:
        abort(403)
    return None


@app.context_processor
def inject_globals():
    to_review = db.get_db().execute(
        "SELECT COUNT(*) FROM accounts WHERE status = 'unreviewed'"
    ).fetchone()[0]
    return {
        "STATUSES": STATUSES,
        "CLOSED": CLOSED,
        "nav_to_review": to_review,
        "bw_enabled": bitwarden.configured(),
        "bw_unlocked": bitwarden.is_unlocked,
        "bw_last_sync": lambda: db.get_setting("bw_last_sync", ""),
    }


@app.template_filter("nicedate")
def nicedate(value):
    dt = parse_dt(value)
    return f"{dt.day} {dt:%b %Y}" if dt else "—"


@app.template_filter("ago")
def ago(value):
    dt = parse_dt(value)
    if not dt:
        return ""
    days = (datetime.now(timezone.utc) - dt).days
    if days < 1:
        return "today"
    if days < 31:
        return f"{days} day{'s' if days != 1 else ''} ago"
    if days < 365:
        months = days // 30
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


@app.template_filter("weburl")
def weburl(url):
    if not url or url.lower().startswith(("androidapp://", "iosapp://")):
        return ""
    return url if "://" in url else "https://" + url


# --- core actions ------------------------------------------------------------

def get_account(conn, account_id):
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def set_status(conn, account_id, status):
    if status not in STATUSES:
        abort(400)
    current = get_account(conn, account_id)["status"]
    if current == status:
        return
    stamp = now_iso()
    conn.execute(
        "UPDATE accounts SET status = ?, status_changed_at = ?, snoozed_until = NULL WHERE id = ?",
        (status, stamp, account_id),
    )
    conn.execute(
        "INSERT INTO events (account_id, at, day, from_status, to_status) VALUES (?, ?, ?, ?, ?)",
        (account_id, stamp, today(), current, status),
    )


def choose_next(conn, day):
    """Queued deletions first, then accounts nobody has looked at yet."""
    for status in ("delete", "unreviewed"):
        row = conn.execute(
            f"""SELECT id FROM accounts
                WHERE status = ? AND (snoozed_until IS NULL OR snoozed_until <= ?)
                  AND id NOT IN (SELECT account_id FROM daily_picks WHERE day = ?)
                ORDER BY {PICK_ORDER} LIMIT 1""",
            (status, day, day),
        ).fetchone()
        if row:
            return row["id"]
    return None


def add_pick(conn, day):
    account_id = choose_next(conn, day)
    if account_id is not None:
        conn.execute("INSERT OR IGNORE INTO daily_picks (day, account_id) VALUES (?, ?)", (day, account_id))
    return account_id


def todays_picks(conn):
    day = today()
    with _picks_lock:
        has_picks = conn.execute("SELECT 1 FROM daily_picks WHERE day = ?", (day,)).fetchone()
        if db.get_setting("picks_generated_for") != day or not has_picks:
            add_pick(conn, day)
            db.set_setting("picks_generated_for", day)
            conn.commit()
    return conn.execute(
        """SELECT a.* FROM daily_picks d JOIN accounts a ON a.id = d.account_id
           WHERE d.day = ? ORDER BY d.rowid""",
        (day,),
    ).fetchall()


def get_stats(conn):
    counts = dict.fromkeys(STATUSES, 0)
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM accounts GROUP BY status"):
        counts[row["status"]] = row["n"]
    total = sum(counts.values())
    done = counts["deleted"] + counts["gone"]
    target = total - counts["keep"]

    return {
        "counts": counts,
        "total": total,
        "done": done,
        "target": target,
        "pct": round(100 * done / target) if target else 0,
        "reviewed_pct": round(100 * (total - counts["unreviewed"]) / total) if total else 0,
        # Segments of the progress bar, left to right: finished, in progress, untouched, kept.
        "segments": [(key, STATUSES[key], counts[key]) for key in SEGMENT_ORDER if counts[key]],
        "processed_today": conn.execute(
            "SELECT COUNT(DISTINCT account_id) FROM events WHERE day = ?", (today(),)
        ).fetchone()[0],
    }


# --- pages -------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    db.get_db().execute("SELECT 1")
    return "ok"


@app.get("/")
def today_page():
    conn = db.get_db()
    cutoff = (now() - timedelta(days=FOLLOW_UP_DAYS)).isoformat(timespec="seconds")
    followups = conn.execute(
        """SELECT * FROM accounts WHERE status = 'requested' AND status_changed_at <= ?
           ORDER BY status_changed_at""",
        (cutoff,),
    ).fetchall()
    vault_cleanup = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE status IN ('deleted', 'gone') AND vault_removed = 0"
    ).fetchone()[0]
    picks = todays_picks(conn)
    n = request.args.get("n", type=int) or len(picks)
    n = max(1, min(n, len(picks))) if picks else 0
    return render_template(
        "today.html",
        pick=picks[n - 1] if picks else None,
        n=n,
        pick_count=len(picks),
        followups=followups,
        vault_cleanup=vault_cleanup,
        stats=get_stats(conn),
        day=now(),
        follow_up_days=FOLLOW_UP_DAYS,
    )


@app.post("/today/another")
def another_pick():
    """Next account: step forward through today's suggestions, adding a new one at the end."""
    conn = db.get_db()
    day = today()
    count = conn.execute("SELECT COUNT(*) FROM daily_picks WHERE day = ?", (day,)).fetchone()[0]
    n = request.form.get("n", type=int) or count
    if n < count:
        return redirect(url_for("today_page", n=n + 1))
    if add_pick(conn, day) is None:
        flash("Nothing left to suggest - everything is reviewed, queued or snoozed.", "info")
        return redirect(url_for("today_page", n=n))
    conn.commit()
    return redirect(url_for("today_page", n=count + 1))


@app.get("/triage")
def triage():
    conn = db.get_db()
    account = conn.execute(
        f"""SELECT * FROM accounts WHERE status = 'unreviewed'
            AND (snoozed_until IS NULL OR snoozed_until <= ?)
            ORDER BY {PICK_ORDER} LIMIT 1""",
        (today(),),
    ).fetchone()
    return render_template("triage.html", account=account, stats=get_stats(conn))


@app.get("/accounts")
def accounts():
    conn = db.get_db()
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    sort = request.args.get("sort", "name")
    where, params = [], []
    if status in STATUSES:
        where.append("status = ?")
        params.append(status)
    elif status == "vault":
        where.append("status IN ('deleted', 'gone') AND vault_removed = 0")
    elif status == "snoozed":
        where.append("snoozed_until > ?")
        params.append(today())
    if q:
        where.append("(name LIKE ? OR domain LIKE ? OR username LIKE ? OR folder LIKE ? OR notes LIKE ?)")
        params += [f"%{q}%"] * 5
    sql = "SELECT * FROM accounts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + SORTS.get(sort, SORTS["name"])
    rows = conn.execute(sql, params).fetchall()
    return render_template("accounts.html", rows=rows, q=q, status=status, sort=sort)


@app.post("/accounts/bulk")
def bulk_update():
    conn = db.get_db()
    ids = request.form.getlist("ids", type=int)
    status = request.form.get("status", "")
    if not ids or status not in STATUSES:
        flash("Select some accounts and a status.", "error")
    else:
        for account_id in ids:
            set_status(conn, account_id, status)
        conn.commit()
        flash(f"Marked {len(ids)} account{'s' if len(ids) != 1 else ''} as '{STATUSES[status]}'.", "success")
    return redirect(safe_next(url_for("accounts")))


@app.get("/account/<int:account_id>")
def account_detail(account_id):
    conn = db.get_db()
    events = conn.execute(
        "SELECT * FROM events WHERE account_id = ? ORDER BY at DESC, id DESC", (account_id,)
    ).fetchall()
    return render_template("account.html", a=get_account(conn, account_id), events=events)


@app.post("/account/<int:account_id>")
def account_save(account_id):
    conn = db.get_db()
    get_account(conn, account_id)
    priority = request.form.get("priority", type=int, default=0)
    conn.execute(
        "UPDATE accounts SET notes = ?, priority = ?, vault_removed = ? WHERE id = ?",
        (
            request.form.get("notes", "").strip(),
            max(0, min(2, priority)),
            1 if request.form.get("vault_removed") else 0,
            account_id,
        ),
    )
    conn.commit()
    flash("Saved.", "success")
    return redirect(url_for("account_detail", account_id=account_id))


@app.post("/account/<int:account_id>/status")
def change_status(account_id):
    conn = db.get_db()
    status = request.form.get("status", "")
    set_status(conn, account_id, status)
    conn.commit()
    name = get_account(conn, account_id)["name"]
    flash(f"{name}: {STATUSES[status]}.", "success")
    return redirect(safe_next(url_for("today_page")))


@app.post("/account/<int:account_id>/snooze")
def snooze(account_id):
    conn = db.get_db()
    get_account(conn, account_id)
    days = max(1, min(365, request.form.get("days", type=int, default=7)))
    day = today()
    until = (now().date() + timedelta(days=days)).isoformat()
    conn.execute("UPDATE accounts SET snoozed_until = ? WHERE id = ?", (until, account_id))
    removed = conn.execute(
        "DELETE FROM daily_picks WHERE day = ? AND account_id = ?", (day, account_id)
    ).rowcount
    if removed:
        add_pick(conn, day)  # swap in a replacement for today's suggestion
    conn.commit()
    flash(f"Snoozed for {days} day{'s' if days != 1 else ''}.", "info")
    return redirect(safe_next(url_for("today_page")))


@app.post("/account/<int:account_id>/vault")
def vault_removed(account_id):
    conn = db.get_db()
    get_account(conn, account_id)
    conn.execute("UPDATE accounts SET vault_removed = 1 WHERE id = ?", (account_id,))
    conn.commit()
    return redirect(safe_next(url_for("today_page")))


@app.post("/account/<int:account_id>/remove")
def remove_account(account_id):
    conn = db.get_db()
    name = get_account(conn, account_id)["name"]
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    flash(f"Removed {name} from the tracker.", "info")
    return redirect(url_for("accounts"))


# --- import / export -----------------------------------------------------------

@app.get("/import")
def import_page():
    return render_template(
        "import.html",
        stats=get_stats(db.get_db()),
        bw=bitwarden.status(),
    )


# --- Bitwarden vault -----------------------------------------------------------

@app.post("/bitwarden/unlock")
def bitwarden_unlock():
    try:
        bitwarden.unlock(request.form.get("master_password", ""))
        flash("Vault unlocked.", "success")
    except BitwardenError as exc:
        flash(f"Couldn't unlock: {exc}", "error")
        return redirect(url_for("import_page", next=request.form.get("next") or None))
    return redirect(safe_next(url_for("import_page")))


@app.post("/bitwarden/lock")
def bitwarden_lock():
    bitwarden.lock()
    flash("Vault locked.", "info")
    return redirect(safe_next(url_for("import_page")))


def link_or_insert(conn, r, stamp):
    """Find the tracker row for a vault item: by vault id, then by site + username."""
    row = conn.execute("SELECT id FROM accounts WHERE vault_id = ?", (r["vault_id"],)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id FROM accounts WHERE dedupe_key = ? AND vault_id IS NULL", (r["dedupe_key"],)
        ).fetchone()
    if row is not None:
        conn.execute(
            """UPDATE accounts SET vault_id = ?, name = ?, domain = ?, url = ?, username = ?, folder = ?,
               vault_created = COALESCE(?, vault_created),
               vault_updated = COALESCE(?, vault_updated), vault_removed = 0 WHERE id = ?""",
            (r["vault_id"], r["name"], r["domain"], r["url"], r["username"], r["folder"],
             r["created"], r["updated"], row["id"]),
        )
        return False
    key = r["dedupe_key"]
    if conn.execute("SELECT 1 FROM accounts WHERE dedupe_key = ?", (key,)).fetchone():
        key = f"{key}|{r['vault_id']}"  # same site + username twice in the vault
    conn.execute(
        """INSERT INTO accounts (dedupe_key, name, domain, url, username, folder, source,
                                 vault_created, vault_updated, imported_at, vault_id)
           VALUES (?, ?, ?, ?, ?, ?, 'bitwarden-sync', ?, ?, ?, ?)""",
        (key, r["name"], r["domain"], r["url"], r["username"], r["folder"],
         r["created"], r["updated"], stamp, r["vault_id"]),
    )
    return True


@app.post("/bitwarden/sync")
def bitwarden_sync():
    try:
        items = bitwarden.fetch_logins()
    except BitwardenError as exc:
        flash(f"Sync failed: {exc}", "error")
        return redirect(url_for("import_page"))

    conn = db.get_db()
    stamp = now_iso()
    added = 0
    seen = []
    for item in items:
        if not item["vault_id"]:
            continue
        added += link_or_insert(conn, vault_record(item), stamp)
        seen.append(item["vault_id"])
    # Items that disappeared from the vault (deleted elsewhere) count as removed from it.
    placeholders = ",".join("?" * len(seen)) or "''"
    gone = conn.execute(
        f"""UPDATE accounts SET vault_removed = 1
            WHERE vault_id IS NOT NULL AND vault_removed = 0 AND vault_id NOT IN ({placeholders})""",
        seen,
    ).rowcount
    db.set_setting("bw_last_sync", stamp)
    conn.commit()
    msg = f"Synced {len(seen)} logins from Bitwarden: {added} new, {len(seen) - added} updated."
    if gone:
        msg += f" {gone} no longer in the vault."
    flash(msg + " Passwords were not stored.", "success")
    return redirect(url_for("import_page"))


@app.post("/account/<int:account_id>/vault-trash")
def vault_trash(account_id):
    conn = db.get_db()
    a = get_account(conn, account_id)
    if not a["vault_id"]:
        flash(f"{a['name']} isn't linked to a Bitwarden item. Sync from Bitwarden first.", "error")
    else:
        try:
            bitwarden.trash_item(a["vault_id"])
        except BitwardenError as exc:
            flash(f"Couldn't delete {a['name']} from Bitwarden: {exc}", "error")
        else:
            conn.execute("UPDATE accounts SET vault_removed = 1 WHERE id = ?", (account_id,))
            conn.commit()
            flash(f"Moved {a['name']} to the Bitwarden trash. You can restore it there for 30 days.", "success")
    return redirect(safe_next(url_for("account_detail", account_id=account_id)))


@app.post("/import")
def import_upload():
    upload = request.files.get("export")
    if not upload or not upload.filename:
        flash("Choose a file to upload.", "error")
        return redirect(url_for("import_page"))
    try:
        records, source = parse_export(upload.filename, upload.read())
    except ExportError as exc:
        flash(str(exc), "error")
        return redirect(url_for("import_page"))

    conn = db.get_db()
    stamp = now_iso()
    added = updated = 0
    for r in records:
        existing = conn.execute(
            "SELECT id FROM accounts WHERE dedupe_key = ?", (r["dedupe_key"],)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE accounts SET name = ?, url = ?, folder = ?,
                   vault_created = COALESCE(?, vault_created), vault_updated = COALESCE(?, vault_updated)
                   WHERE id = ?""",
                (r["name"], r["url"], r["folder"], r["created"], r["updated"], existing["id"]),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO accounts (dedupe_key, name, domain, url, username, folder, source,
                                         vault_created, vault_updated, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r["dedupe_key"], r["name"], r["domain"], r["url"], r["username"], r["folder"],
                 source, r["created"], r["updated"], stamp),
            )
            added += 1
    conn.commit()
    flash(
        f"Imported {added} new account{'s' if added != 1 else ''}"
        f" ({updated} already tracked or duplicate). Passwords were not stored."
        " You can now delete the export file.",
        "success",
    )
    return redirect(url_for("today_page"))


@app.get("/export.csv")
def export_csv():
    rows = db.get_db().execute(
        f"SELECT * FROM accounts ORDER BY {SORTS['name']}"
    ).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    cols = ["name", "domain", "url", "username", "folder", "status", "notes",
            "vault_removed", "status_changed_at", "vault_updated"]
    writer.writerow(cols)
    for row in rows:
        writer.writerow([STATUSES.get(row[c], row[c]) if c == "status" else row[c] for c in cols])
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=account-cleanup-{today()}.csv"},
    )


@app.post("/reset")
def reset():
    if request.form.get("confirm", "").strip().upper() != "DELETE":
        flash("Type DELETE to confirm the reset.", "error")
        return redirect(url_for("import_page"))
    conn = db.get_db()
    conn.execute("DELETE FROM daily_picks")
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM accounts")
    conn.execute("DELETE FROM settings WHERE key = 'picks_generated_for'")
    conn.commit()
    flash("All tracker data was deleted.", "info")
    return redirect(url_for("import_page"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
