import csv
import io
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, flash, redirect, render_template, request, url_for

import db
from importer import ExportError, parse_export

STATUSES = {
    "unreviewed": "To review",
    "delete": "To delete",
    "requested": "Deletion requested",
    "deleted": "Deleted",
    "gone": "Site gone",
    "keep": "Keeping",
}
CLOSED = ("deleted", "gone")

# Oldest-touched accounts first: the longer since you changed it, the more likely it's forgotten.
PICK_ORDER = "priority DESC, COALESCE(NULLIF(vault_updated, ''), imported_at) ASC, id ASC"

SORTS = {
    "name": "name COLLATE NOCASE",
    "domain": "domain, name COLLATE NOCASE",
    "status": "status, name COLLATE NOCASE",
    "updated": "COALESCE(NULLIF(vault_updated, ''), imported_at), name COLLATE NOCASE",
}

TZ = ZoneInfo(os.environ.get("TZ") or "UTC")
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

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


def setting_int(key, default):
    try:
        return int(db.get_setting(key, default))
    except (TypeError, ValueError):
        return default


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
    if not APP_PASSWORD or request.path == "/healthz":
        return None
    auth = request.authorization
    if (
        auth
        and secrets.compare_digest((auth.username or "").encode(), APP_USERNAME.encode())
        and secrets.compare_digest((auth.password or "").encode(), APP_PASSWORD.encode())
    ):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Account Cleanup"'})


@app.context_processor
def inject_globals():
    to_review = db.get_db().execute(
        "SELECT COUNT(*) FROM accounts WHERE status = 'unreviewed'"
    ).fetchone()[0]
    return {"STATUSES": STATUSES, "CLOSED": CLOSED, "nav_to_review": to_review}


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


@app.template_filter("searchurl")
def searchurl(account):
    return "https://duckduckgo.com/?q=" + quote_plus(f"how to delete {account['name']} account")


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
            for _ in range(setting_int("daily_count", 1)):
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

    active_days = {r["day"] for r in conn.execute("SELECT DISTINCT day FROM events")}
    streak, d = 0, now().date()
    if d.isoformat() not in active_days:
        d -= timedelta(days=1)  # today isn't over yet, so don't break the streak
    while d.isoformat() in active_days:
        streak += 1
        d -= timedelta(days=1)

    return {
        "counts": counts,
        "total": total,
        "done": done,
        "target": target,
        "pct": round(100 * done / target) if target else 0,
        "reviewed_pct": round(100 * (total - counts["unreviewed"]) / total) if total else 0,
        "streak": streak,
    }


# --- pages -------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    db.get_db().execute("SELECT 1")
    return "ok"


@app.get("/")
def today_page():
    conn = db.get_db()
    follow_up_days = setting_int("follow_up_days", 14)
    cutoff = (now() - timedelta(days=follow_up_days)).isoformat(timespec="seconds")
    followups = conn.execute(
        """SELECT * FROM accounts WHERE status = 'requested' AND status_changed_at <= ?
           ORDER BY status_changed_at""",
        (cutoff,),
    ).fetchall()
    vault_cleanup = conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE status IN ('deleted', 'gone') AND vault_removed = 0"
    ).fetchone()[0]
    return render_template(
        "today.html",
        picks=todays_picks(conn),
        followups=followups,
        vault_cleanup=vault_cleanup,
        stats=get_stats(conn),
        day=now(),
        daily_count=setting_int("daily_count", 1),
        follow_up_days=follow_up_days,
    )


@app.post("/today/another")
def another_pick():
    conn = db.get_db()
    if add_pick(conn, today()) is None:
        flash("Nothing left to suggest - everything is reviewed, queued or snoozed.", "info")
    conn.commit()
    return redirect(url_for("today_page"))


@app.post("/settings")
def save_settings():
    for key, low, high in (("daily_count", 1, 20), ("follow_up_days", 1, 365)):
        try:
            value = max(low, min(high, int(request.form.get(key, ""))))
        except ValueError:
            continue
        db.set_setting(key, value)
    db.get_db().commit()
    flash("Settings saved.", "success")
    return redirect(url_for("today_page"))


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
    return render_template("import.html", stats=get_stats(db.get_db()))


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
