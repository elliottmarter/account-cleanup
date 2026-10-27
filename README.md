# Account Cleanup

A small self-hosted web app for working through your old online accounts and
deleting the ones you no longer need — a little at a time.

1. **Import** a Bitwarden export (`.json` or `.csv`; other password managers' CSVs work too).
   Only names, URLs, usernames, folders and dates are kept — passwords, 2FA seeds and notes are discarded.
2. **Triage** (optional): rapid-fire Keep / Delete / Site gone, with keyboard shortcuts `K` `D` `G` `S`.
3. **Today**: the app suggests one account per day (configurable). Accounts you queued for deletion
   come first, then unreviewed ones — oldest-touched first, since those are most likely forgotten.
   Each card has a link to the site, a "how do I delete this?" search, and a GDPR erasure email template.
4. **Track** each account as *To review → To delete → Deletion requested → Deleted* (or *Keeping* / *Site gone*).
   Requests older than 14 days resurface as follow-ups, and deleted accounts remind you to remove them
   from your password manager.

## Run it

```bash
docker compose up -d --build
```

Then open <http://localhost:8080> (or `http://<server-ip>:8080`).

Data lives in the `account-data` Docker volume (`/data/accounts.db`, SQLite). Re-importing a newer
export is safe — existing accounts keep their status. **Tracker → Download CSV** on the Import page
exports everything.

### On Unraid

1. Copy this whole `account-cleanup` folder onto the server, e.g. to
   `/mnt/user/appdata/account-cleanup/src` (via a network share, or WinSCP).
2. In the Unraid web UI open the terminal (`>_` icon, top right) and run:

   ```bash
   cd /mnt/user/appdata/account-cleanup/src && bash unraid-install.sh
   ```

3. Open `http://<unraid-ip>:8080`. The container appears in the Docker tab with a WebUI link
   and starts automatically with the array.

Data is stored in `/mnt/user/appdata/account-cleanup/data`, so appdata backups include it.
To update, copy the new code over `src` and run the script again. Override defaults inline, e.g.
`PORT=8090 TZ=Europe/Paris bash unraid-install.sh`.

### Settings (environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `TZ` | `UTC` | Time zone used to decide when "today" changes |
| `APP_PASSWORD` | *(unset)* | If set, the app asks for a login (HTTP basic auth) |
| `APP_USERNAME` | `admin` | Username for the login prompt |
| `DATABASE_PATH` | `/data/accounts.db` | SQLite file location |

The app has no login by default. The tracker holds a list of your accounts and usernames, so set
`APP_PASSWORD` (or keep the port on your LAN / behind a reverse proxy with auth) if anyone else can reach it.

### Security notes

- Bitwarden's unencrypted export contains your passwords in plain text. Delete the file after importing.
- Uploads are parsed in memory; the raw file is never written to disk.

## Develop without Docker

```bash
pip install -r requirements.txt
DATABASE_PATH=./data/accounts.db python app/app.py
```
