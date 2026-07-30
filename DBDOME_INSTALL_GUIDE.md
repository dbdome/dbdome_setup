# DBDOME — Step-by-Step Installation & Configuration Guide

**Applies to:** DBDOME installer v2.0.0 (`dbdome_setup.exe`) on Windows Server 2022 / 2025 (x64)
**Covers:** mounting the media, installation (console + PowerShell), first login, adding a monitored server, whitelisting an applicative login, mail configuration, and updating.

---

## 1. Before You Start

### 1.1 Server requirements

| Item | Requirement |
|---|---|
| OS | Windows Server 2022 or 2025, x64 |
| CPU / RAM / Disk | 4 cores / 16 GB / 250 GB (SSD or NVMe recommended) |
| Privileges | Local **Administrator** (the installer refuses to run otherwise) |
| Free TCP ports | **8080** (DBDOME Web UI), **3000** (Grafana), **5432** (PostgreSQL) |
| Network | Outbound reachability to every monitored database on its listener port (SQL Server 1433, Oracle 1521, PostgreSQL 5432, MySQL/MariaDB 3306, ClickHouse 8123) |
| Static IP | Strongly recommended — the installer records the machine's LAN IP as `ORG_IP` and uses it in dashboard links |

Nothing else needs pre-installing: Python is embedded in the installer, PostgreSQL 18 and the Microsoft ODBC Driver for SQL Server are bundled and installed automatically.
**Oracle monitoring only:** install Oracle Instant Client yourself and point `ORACLE_CLIENT_LIB_DIR` in `C:\ProgramData\DBDOME\bin\.env` at it (e.g. `C:\oracle\instantclient_23_0`).

### 1.2 What gets installed

| Component | Detail |
|---|---|
| Services | `DBDOME_scheduler` + `DBDOME_web` (older builds: one combined `DBDOME_dbanalytics`), `DBDOME_Grafana` (NSSM-wrapped), `postgresql-x64-18` — all Automatic start with crash-restart |
| Install root | `C:\ProgramData\DBDOME` (binaries in `bin\`, Grafana config in `conf\`, `grafana.db` in `data\`) |
| PostgreSQL | `C:\Program Files\PostgreSQL\18`, port **5432**, database `dbanalytics` |
| DB roles | `postgres`, `dbdome_mon_usr`, `dbdome_adm` — initial password `Yd2243796Anz!!` (**change after install**, see §7.1) |
| Firewall | Inbound rules "DBDOME HTTP Server" (8080), "DBDOME Grafana" (3000), "DBDOME PostgreSQL" (5432) for Domain+Private profiles |
| Shortcut | `DBDOME.lnk` on the Public desktop → the Web UI |

---

## 2. Mount the Installation Media

The installer looks for a payload folder named `DBDOME` (it must contain a `bin\` subfolder). It searches, in order: next to `dbdome_setup.exe` → a DVD volume labeled **`DBDOME_DVD_ROM`** → `<any drive>:\DBDOME`.

**Option A — ISO file (PowerShell, run as Administrator):**

```powershell
Mount-DiskImage -ImagePath "C:\path\to\DBDOME.iso"
# note the drive letter it was assigned:
(Get-DiskImage -ImagePath "C:\path\to\DBDOME.iso" | Get-Volume).DriveLetter
```

**Option B — physical DVD:** insert the disc (volume label `DBDOME_DVD_ROM`); no mounting needed.

**Option C — copied folder:** place the `DBDOME` folder in the same directory as `dbdome_setup.exe`, or at the root of any drive (`D:\DBDOME`).

---

## 3. Install DBDOME

The installer is a **console program** — there is no wizard and there are no questions. It auto-detects everything and runs 16 steps unattended (copy files → ODBC driver → PostgreSQL → database restore → Grafana → `.env` → firewall → services → validation → shortcut).

### 3.1 Run it (UI way)

1. Log on as a local Administrator.
2. Right-click `dbdome_setup.exe` → **Run as administrator**.
3. Watch the console; installation ends with a summary banner listing the Web UI, Grafana, and PostgreSQL addresses.

### 3.2 Run it (PowerShell way)

```powershell
# elevated PowerShell
Start-Process -FilePath "D:\dbdome_setup.exe" -Verb RunAs -Wait
Get-Content C:\ProgramData\DBDOME\install.log -Tail 40
```

### 3.3 Verify the installation

The installer runs its own validation (PostgreSQL connectivity, root-cause catalog loaded, services RUNNING, `.env` present). To re-check manually:

```powershell
Get-Service DBDOME_scheduler, DBDOME_web, DBDOME_Grafana, postgresql-x64-18
Get-NetTCPConnection -LocalPort 8080,3000,5432 -State Listen | Select LocalPort -Unique
```

All services should be **Running**. Full log: `C:\ProgramData\DBDOME\install.log`.

---

## 4. First Login

| Interface | URL | Notes |
|---|---|---|
| DBDOME Web UI | `https://<server-ip>:8080/dbdome` | HTTPS with a self-signed certificate — the browser will warn once; proceed. To install your own certificate use the `/ssl_certificate` page, then restart `DBDOME_web`. |
| Grafana dashboards | `http://<server-ip>:3000` | First login `admin` / `admin` (Grafana prompts you to change it). See `docs\DBDOME_Grafana_User_Access_Guide.pdf` for role setup. |

The configuration pages in the next sections are normally reached through buttons on the Grafana dashboards, but can also be opened directly at the URLs given.

---

## 5. Configuration

### 5.1 Add a Monitored Server

1. Open **`https://<server-ip>:8080/serverform`** (or the "Add Server" button on the Grafana *7 – Automation and Workflows* dashboard).
2. Fill in the form:

   | Field | Value |
   |---|---|
   | Action | **Add** (the same form does Update / Delete) |
   | IP Address | address of the database host |
   | Server | display name for the server |
   | Port | listener port (1433 / 1521 / 5432 / 3306 / 8123) |
   | DB Vendor | `mssql`, `Oracle`, `postgres`, `mysql`, `mariadb`, or `clickhouse` |
   | Service name | Oracle only — the service name |
   | Auth Type | `sql` (username + password appear) or `win` (Windows/trusted authentication) |
   | User / Password | the monitoring account on the target database (stored encrypted) |

3. Click **Test Connection** — a live probe against the target; fix any error before saving.
4. Submit. The server is upserted into `metrics.monitored_servers` and you are redirected to the *7 – Automation and Workflows* dashboard.
5. **Verify collection:** within a few scheduler cycles the server should show data on the monitoring dashboards. If nothing arrives, check the collection log (Grafana *Operation Log* panels or `log.operation_log`).

> Grant the monitoring account read/DMV permissions on the target database (e.g. `VIEW SERVER STATE` on SQL Server).

### 5.2 Add a Whitelist (Approved) Applicative Login

DBDOME's **App Login Guard** watches program/login combinations: a session on a *watched program* (e.g. SSMS) whose login is **not whitelisted** raises alert `SEC-SQL-ACC-030-RC01` (and can kill the session, subject to the blocker switch). Whitelisted logins (`white = true`) never raise alerts — the suppression is enforced centrally, so **no DBDOME alert of any kind is created for a whitelisted applicative login**.

1. Open **`https://<server-ip>:8080/app_login_guard`** (or the "Configure App Login Guard" button on the *App Login Guard* Grafana dashboard).
2. Under **Applicative Logins**, enter:
   - *Login* — the account name; SQL LIKE patterns allowed (`svc_app`, `%app%`)
   - *Server* — a specific server name, or blank for **all** servers
3. Click **Add**. New entries are whitelisted (`white = true`) by default.
4. Optionally add entries under **Watched Programs** (e.g. `%SSMS%`, `sqlcmd%`) — the guard only enforces when both lists are populated and the Security/critical *blocker* switch is enabled.
5. The Enable/Disable toggle on each row activates/deactivates the entry (`is_active`).

To turn an approved login into a **watched-only** login (alerts fire when it's used from a watched program), clear its `white` flag — this currently has no UI toggle:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -U dbdome_adm -d dbanalytics -c `
  "UPDATE metrics.app_logins SET white = false WHERE applicative_login = 'svc_app';"
```

> Related page: **`/exclude_logins`** — logins listed there (e.g. the DBA team's own accounts) are excluded from self-activity collection and alerting entirely.

### 5.3 Mail (SMTP) Configuration

1. Open **`https://<server-ip>:8080/mailconfiguration`** (or the "Email Configuration" button in Grafana).
2. Fill in:

   | Field | Value |
   |---|---|
   | SMTP Server / Port | e.g. `smtp.office365.com` / `587` |
   | SMTP User / Password | mailbox credentials (password stored encrypted) |
   | TLS | check for STARTTLS (port 587) |
   | Sender | the From address |
   | Group name / Recipients | a recipient group and its comma-separated address list |

3. Click **Send test email** — sends "DBDOME SMTP test" using the values currently on the form *without saving*, and reports success/failure. Fix errors before saving.
4. Submit. Settings are stored in `config.mail_config`, recipients in `config.mail_groups`; alert and scheduled-report mails use them immediately (no service restart needed).

Additional recipient groups can be added later at `/addrecipients`.

---

## 6. Updating DBDOME

Updates ship as media containing a `dbdome` folder plus `dbdome_update.exe`.

1. Mount the update ISO / insert the disc (or copy the `dbdome` folder to a drive root).
2. Make sure PostgreSQL is running — the updater checks but does not start it:
   ```powershell
   Start-Service postgresql-x64-18
   ```
3. Run the updater elevated:
   ```powershell
   Start-Process -FilePath "E:\dbdome_update.exe" -Verb RunAs -Wait
   # or point it at the payload explicitly:
   # dbdome_update.exe --source "E:\dbdome"
   ```
4. The updater automatically: stops the DBDOME services → overlays new binaries into `C:\ProgramData\DBDOME\bin` (preserving `DBDOME_SECRET_KEY` and `PG_PASSWORD` in `.env`) → reinstalls and restarts the services → swaps `grafana.db` (your Grafana users are migrated; the old file is kept as `grafana.db.bak.<timestamp>`) → applies all pending SQL migration scripts (tracked in `meta.schema_migrations`, so reruns are safe) → re-encrypts secrets.
5. **Check the end-of-run summary** for any "SQL scripts that did not succeed", and review `C:\ProgramData\DBDOME\update.log`.

---

## 7. Post-Install Hardening & Troubleshooting

### 7.1 Change the default database password

All three PostgreSQL roles are installed with the same initial password. Change it and keep `PG_PASSWORD` in `C:\ProgramData\DBDOME\bin\.env` in sync (the updater/services accept it in plain text and encrypt it to `enc:v1:` on the next update run), then restart the DBDOME services.

### 7.2 Where to look when something is wrong

| Symptom | Check |
|---|---|
| Installer failed | `C:\ProgramData\DBDOME\install.log` |
| Web UI down on 8080 | `Get-Service DBDOME_web`; restart it; Event Viewer → Application |
| Dashboards empty | `Get-Service DBDOME_scheduler`; Grafana *Operation Log*; `log.operation_log` table |
| Grafana down on 3000 | `Get-Service DBDOME_Grafana`; `C:\ProgramData\DBDOME\grafana-svc.log` |
| No alert mails | `/mailconfiguration` → **Send test email**; verify recipients in `config.mail_groups` |
| Update issues | `C:\ProgramData\DBDOME\update.log` + the failed-SQL summary |

### 7.3 Service control quick reference (elevated PowerShell)

```powershell
Restart-Service DBDOME_web, DBDOME_scheduler
Restart-Service DBDOME_Grafana
Get-Service DBDOME*, postgresql-x64-18 | Format-Table Name, Status, StartType
```
