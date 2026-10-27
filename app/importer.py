"""Turn password-manager exports into account records.

Only names, URLs, usernames, folders and dates are read. Passwords, TOTP
seeds, notes and custom fields are never copied out of the uploaded file.
"""
import csv
import io
import json
from urllib.parse import urlsplit

csv.field_size_limit(10_000_000)


class ExportError(ValueError):
    pass


# Common second-level suffixes, so "shop.example.co.uk" groups as "example.co.uk".
TWO_PART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "co.nz", "co.jp", "com.br", "co.za",
    "co.in", "com.mx", "com.tr", "com.cn", "co.kr",
}

APP_URI_PREFIXES = ("androidapp://", "iosapp://")

CSV_ALIASES = {
    "name": ("name", "title", "account", "site"),
    "url": ("login_uri", "url", "website", "login url", "web site", "uri", "origin_url"),
    "username": ("login_username", "username", "user name", "email", "login", "user"),
    "folder": ("folder", "group", "grouping", "category"),
}


def domain_of(url):
    url = (url or "").strip()
    if not url or url.lower().startswith(APP_URI_PREFIXES):
        return ""
    if "://" not in url:
        url = "http://" + url
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if "." not in host or host.replace(".", "").isdigit():
        return host
    parts = host.split(".")
    keep = 3 if ".".join(parts[-2:]) in TWO_PART_SUFFIXES else 2
    return ".".join(parts[-keep:])


def pick_url(uris):
    """Prefer a web URL over an Android/iOS app URI."""
    uris = [u.strip() for u in uris if u and u.strip()]
    for uri in uris:
        if not uri.lower().startswith(APP_URI_PREFIXES):
            return uri
    return uris[0] if uris else ""


def dedupe_key(record):
    return f"{(record['domain'] or record['name']).lower()}|{record['username'].lower()}"


def _record(name, url, username, folder="", created=None, updated=None):
    domain = domain_of(url)
    record = {
        "name": (name or "").strip() or domain or "(unnamed)",
        "url": url,
        "domain": domain,
        "username": (username or "").strip(),
        "folder": (folder or "").strip(),
        "created": created,
        "updated": updated,
    }
    record["dedupe_key"] = dedupe_key(record)
    return record


def _parse_bitwarden_json(text):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExportError(f"That doesn't look like valid JSON ({exc.msg}).") from None
    if not isinstance(data, dict) or "items" not in data:
        raise ExportError("JSON file has no 'items' list - is it a Bitwarden export?")
    if data.get("encrypted"):
        raise ExportError(
            "This is an encrypted Bitwarden export. Export again choosing "
            "'.json' or '.csv' (not '.json (Encrypted)')."
        )
    folders = {f.get("id"): f.get("name", "") for f in data.get("folders") or []}
    records = []
    for item in data["items"]:
        # type 1 = login; skip secure notes, cards, identities and trashed items
        if item.get("type") != 1 or item.get("deletedDate"):
            continue
        login = item.get("login") or {}
        uris = [u.get("uri") for u in login.get("uris") or [] if isinstance(u, dict)]
        records.append(_record(
            item.get("name"),
            pick_url(uris),
            login.get("username"),
            folders.get(item.get("folderId"), ""),
            item.get("creationDate"),
            item.get("revisionDate"),
        ))
    return records


def _parse_csv(text):
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ExportError("The CSV file is empty or has no header row.")
    headers = {h.strip().lower(): h for h in reader.fieldnames if h}
    cols = {
        field: next((headers[a] for a in aliases if a in headers), None)
        for field, aliases in CSV_ALIASES.items()
    }
    if not cols["name"] and not cols["url"]:
        raise ExportError(
            "Couldn't find a name or URL column. Expected a Bitwarden CSV, or a CSV "
            "with columns like name, url, username."
        )
    type_col = headers.get("type")

    records = []
    for row in reader:
        if type_col and (row.get(type_col) or "").strip().lower() not in ("", "login"):
            continue

        def get(field):
            col = cols[field]
            return (row.get(col) or "").strip() if col else ""

        records.append(_record(get("name"), pick_url(get("url").split(",")), get("username"), get("folder")))
    return records


def parse_export(filename, raw):
    """Return (records, source_label) for an uploaded export file."""
    text = raw.decode("utf-8-sig", errors="replace")
    if filename.lower().endswith(".json") or text.lstrip().startswith("{"):
        records, source = _parse_bitwarden_json(text), "bitwarden-json"
    else:
        records, source = _parse_csv(text), "csv"
    if not records:
        raise ExportError("No login items were found in that file.")
    return records, source
