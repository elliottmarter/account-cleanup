# Account Cleanup

> [!WARNING]
> **This app was written by AI (Claude) for my own personal use.**
> It has not been professionally reviewed or audited. You're free to use, copy and modify it,
> but **entirely at your own risk**. It comes with no warranty and no support, and I accept no
> responsibility for any data loss, security issues or other problems that result from using it.
> Released under the [MIT License](LICENSE).

A small self-hosted web app for working through your old online accounts and
deleting the ones you no longer need — a little at a time.

1. **Import** a Bitwarden export (`.json` or `.csv`; other password managers' CSVs work too).
   Only names, URLs, usernames, folders and dates are kept — passwords, 2FA seeds and notes are discarded.
   **Recommended:** export as `.csv` and delete the password columns yourself before uploading
   (see [Preparing your export](#preparing-your-export)).
2. **Triage** (optional): rapid-fire Keep / Delete / Site gone, with keyboard shortcuts `K` `D` `G` `S`.
3. **Today**: the app suggests one account per day (configurable). Accounts you queued for deletion
   come first, then unreviewed ones — oldest-touched first, since those are most likely forgotten.
   Each card has a link to the site, a "how do I delete this?" search, and a GDPR erasure email template.
4. **Track** each account as *To review → To delete → Deletion requested → Deleted* (or *Keeping* / *Site gone*).
   Requests older than 14 days resurface as follow-ups, and deleted accounts remind you to remove them
   from your password manager.

## Run it

```bash
docker run -d --name account-cleanup -p 8080:8080 -v account-cleanup:/data -e TZ=Europe/London --restart unless-stopped ghcr.io/elliottmarter/account-cleanup:latest
```

Then open <http://localhost:8080> (or `http://<server-ip>:8080`). Images are published for
`amd64` and `arm64` (e.g. Raspberry Pi).

Or with Docker Compose, using the [docker-compose.yml](docker-compose.yml) in this repo:

```bash
docker compose up -d
```

Data lives in `/data/accounts.db` (SQLite) inside the container, so keep `/data` on a volume or
host folder. Re-importing a newer export is safe — existing accounts keep their status.
**Download tracker as CSV** on the Import page exports everything.

### On Unraid

1. Open the Unraid terminal (`>_` icon, top right) and download the template:

   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-account-cleanup.xml https://raw.githubusercontent.com/elliottmarter/account-cleanup/main/unraid/account-cleanup.xml
   ```

2. Go to **Docker → Add Container**, pick **account-cleanup** from the *Template* dropdown,
   check the settings and click **Apply**.

Data is stored in `/mnt/user/appdata/account-cleanup/data`, so appdata backups include it. Unraid
shows when a new version is available; click **Update** on the container to get it.

### Updating

```bash
docker pull ghcr.io/elliottmarter/account-cleanup:latest
docker rm -f account-cleanup
```

then run the `docker run` command again (your data stays in the volume). With Compose:
`docker compose pull && docker compose up -d`.

Version tags such as `:1.0.0` are published for tagged releases, if you'd rather pin a version.

### Settings (environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `TZ` | `UTC` | Time zone used to decide when "today" changes |
| `APP_PASSWORD` | *(unset)* | If set, the app asks for a login (HTTP basic auth) |
| `APP_USERNAME` | `admin` | Username for the login prompt |
| `PUID` / `PGID` | `1000` / `1000` | User and group the app runs as; `/data` is chowned to them on start (Unraid: `99` / `100`) |

The app has no login by default. The tracker holds a list of your accounts and usernames, so set
`APP_PASSWORD` (or keep the port on your LAN / behind a reverse proxy with auth) if anyone else can reach it.

### Preparing your export

The app ignores passwords, but you shouldn't have to take its word for it. **It's recommended to
export as CSV and remove the passwords yourself, so none are ever uploaded to the app:**

1. In Bitwarden choose **Tools → Export vault** and pick **`.csv`** (not `.json`).
2. Open the file in Excel, LibreOffice Calc or a text editor and **delete the `login_password` and
   `login_totp` columns**. You can also delete `notes` and `fields`, which may contain secrets too.
   The app only needs `name`, `login_uri` and `login_username` (`folder` and `type` are optional).
3. Save it, still as CSV, and upload that file.
4. Afterwards, permanently delete both the original and the edited export (and empty the recycle bin).

The same applies to other password managers' CSV exports: keep the name, URL and username
columns and delete the rest.

### Security notes

- Bitwarden's unencrypted export contains your passwords in plain text. Strip them as described
  above, and delete the file after importing.
- Uploads are parsed in memory; the raw file is never written to disk.

## Build from source

```bash
docker build -t account-cleanup .
```

Or without Docker:

```bash
pip install -r requirements.txt
DATABASE_PATH=./data/accounts.db python app/app.py
```

Every push to `main` builds and publishes `:latest` via GitHub Actions
([workflow](.github/workflows/docker.yml)); pushing a tag like `v1.2.0` also publishes `:1.2.0` and `:1.2`.
