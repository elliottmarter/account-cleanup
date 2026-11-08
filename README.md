# Account Cleanup

> [!WARNING]
> **This app was written by AI (Claude) for my own personal use.**
> It has not been professionally reviewed or audited. You're free to use, copy and modify it,
> but **entirely at your own risk**. It comes with no warranty and no support, and I accept no
> responsibility for any data loss, security issues or other problems that result from using it.
> Released under the [MIT License](LICENSE).

A small self-hosted web app for working through your old online accounts and
deleting the ones you no longer need — a little at a time.

<img width="877" height="788" alt="image" src="https://github.com/user-attachments/assets/aac9ae19-365c-4166-addd-c1aea8fe2a87" />

1. **Import** a Bitwarden export (`.json` or `.csv`; other password managers' CSVs work too).
   Only names, URLs, usernames, folders and dates are kept — passwords, 2FA seeds and notes are discarded.
   **Recommended:** export as `.csv` and delete the password columns yourself before uploading
   (see [Preparing your export](#preparing-your-export)).
   **Or sync straight from Bitwarden** with an API key, and delete logins from your vault from inside the
   app (see [Bitwarden sync](#bitwarden-sync-optional)).
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
| `BW_CLIENTID` / `BW_CLIENTSECRET` | *(unset)* | Bitwarden personal API key; turns on [Bitwarden sync](#bitwarden-sync-optional) |
| `BW_SERVER` | bitwarden.com | `https://vault.bitwarden.eu` for the EU cloud, or your Vaultwarden URL |
| `BW_LOCK_MINUTES` | `15` | Lock the vault again after this many idle minutes |
| `PUID` / `PGID` | `1000` / `1000` | User and group the app runs as; `/data` is chowned to them on start (Unraid: `99` / `100`) |

The app has no login by default. The tracker holds a list of your accounts and usernames, so set
`APP_PASSWORD` (or keep the port on your LAN / behind a reverse proxy with auth) if anyone else can reach it.

### Bitwarden sync (optional)

Instead of uploading an export, the app can read your vault directly using the official
[Bitwarden CLI](https://bitwarden.com/help/cli/), which is bundled in the image. It works with
bitwarden.com, the EU cloud and Vaultwarden.

1. In the Bitwarden web vault go to **Settings → Security → Keys → View API key** and copy the
   `client_id` and `client_secret` into `BW_CLIENTID` and `BW_CLIENTSECRET` on the container.
   (For the EU cloud or Vaultwarden, also set `BW_SERVER`.)
2. Open **Import**, type your master password and click **Unlock**.
3. Click **Sync now**. Only each login's name, website, username, folder and dates are copied in.
   Accounts you already track (from an earlier import) are matched by site and username and keep their status.
4. On an account's page, or on a finished card, **Delete from Bitwarden** moves that login to your
   Bitwarden trash, where you can still restore it for 30 days.

How it's handled:

- The API key only logs the CLI in. Your vault stays encrypted until you unlock it with your master
  password, which is passed straight to the CLI and never saved.
- The unlocked session exists only in memory. It's dropped after `BW_LOCK_MINUTES` without use, when you
  click **Lock**, or when the container restarts.
- The CLI returns full items, passwords included, to the app's process. The app keeps only the fields
  listed above and discards the rest straight away. Passwords, password history, 2FA seeds, passkeys,
  notes and custom fields are never written to the database, shown in the UI or logged.
- The only network traffic is between the bundled `bw` CLI and your own Bitwarden server (bitwarden.com,
  the EU cloud, or the `BW_SERVER` you set). The app itself makes no outbound requests and loads nothing
  from the internet.
- The CLI keeps its login and an encrypted copy of the vault in `/data/bitwarden`, like any Bitwarden client.
- While the vault is unlocked, anyone who can reach the app can delete vault items, so **set
  `APP_PASSWORD`** and don't expose the port to the internet. Requests coming from other websites are refused.
- Deleting only removes the entry from Bitwarden. It doesn't close the account on the website itself.

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
