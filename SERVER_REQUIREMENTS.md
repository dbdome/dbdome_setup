# DBDOME — Server Requirements

Requirements to install and run the **DBDOME** database‑monitoring platform on a
single Windows server. Derived from the installer (`setup.py`), the service
definitions, and the runtime configuration.

> Legend: **Required** = must be present/true to run. **Recommended** = sizing
> guidance not enforced by the software (tune to your monitored‑database count).

---

## 1. Overview

DBDOME installs to `C:\ProgramData\DBDOME` and runs three logical components as
Windows services on one host:

| Component | Process / service | Port |
|---|---|---|
| Web / API (FastAPI + Uvicorn) | `dbdome_service.exe` → `DBDOME_web` (or combined `DBDOME_dbanalytics`) | **8080** |
| Scheduler (metric collection) | `dbdome_service.exe` → `DBDOME_scheduler` | — |
| Grafana dashboards | `dbdomeg-server.exe` → `DBDOME_Grafana` (NSSM‑wrapped) | **3000** |
| PostgreSQL (analytics store) | `postgresql-x64-18` | **5432** |

It connects **out** to the customer's database servers (SQL Server, Oracle, etc.)
to collect monitoring metrics.

---

## 2. Hardware

| Resource | Requirement | Notes |
|---|---|---|
| CPU | **4 cores** | Scheduler runs many concurrent metric queries. |
| RAM | **16 GB** | PostgreSQL `shared_buffers=512MB` + up to `max_connections=1000`; the embedding model (all‑MiniLM‑L6‑v2) and Grafana add overhead. |
| Disk | **250 GB** | Install ~ a few GB; PostgreSQL data + logs grow with history/retention; allow room for backups. SSD/NVMe recommended for PostgreSQL write throughput. |

> Scale RAM/CPU with the number of monitored databases and metric frequency
> (M1/M5/M15 collection intervals) if you exceed a typical deployment.

---

## 3. Operating system

- **Required:** Windows Server (x64). Validated on **Windows Server 2022 / 2025**.
- **Required:** Installer and updater must run **as Administrator** (`setup.py`
  aborts otherwise — it installs services, writes firewall rules, configures PostgreSQL).
- **Recommended:** Latest Windows updates; stable hostname; static IP / reserved DHCP.

---

## 4. Software prerequisites

### Bundled by the installer (no manual install)
These ship in the install package and are installed/configured by `setup.py`:

- **PostgreSQL 18.3** (`postgresql-18.3-3-windows-x64.exe`) → `C:\Program Files\PostgreSQL\18`
- **Microsoft ODBC Driver 17/18 for SQL Server** (`msodbcsql.msi`) — for SQL Server monitoring
- **NSSM** (`nssm.exe`) — wraps `dbdomeg-server.exe` (Grafana) as a Windows service
- **DBDOME binaries** — `dbdome_service.exe`, `dbdome_dbanalytics.exe`, `dbdomeg-server.exe`,
  `grafana.exe` (PyInstaller‑frozen; Python runtime is embedded — **no separate Python install required**)
- **Embedding model** — `all-MiniLM-L6-v2` (local; referenced by `.env` `LLM=`)

### Must exist / be reachable
- **Oracle Instant Client** — only if monitoring Oracle. Path set via `.env`
  `ORACLE_CLIENT_LIB_DIR` (e.g. `C:\oracle\instantclient_23_0`).
- **DVD / mounted media or local source** containing the install bundle
  (`bin/`, `postgres/install/`, `data/`).

---

## 5. Network & ports

### Inbound (must be open to dashboard/API users)
| Port | Protocol | Service | Purpose |
|---|---|---|---|
| 8080 | TCP | DBDOME Web/API | Web UI & REST endpoints |
| 3000 | TCP | Grafana | Dashboards |
| 5432 | TCP | PostgreSQL | Analytics DB (open only if remote DB access is needed) |

The installer creates these firewall rules (profiles: **domain, private**):
`DBDOME HTTP Server` (8080), `DBDOME Grafana` (3000), `DBDOME PostgreSQL` (5432).

### Outbound (to monitored databases)
The server must reach each monitored DB on its listener port, e.g.:
- **SQL Server** — TCP **1433** (or named‑instance port)
- **Oracle** — TCP **1521**
- **PostgreSQL/MySQL/etc.** — their respective ports

### IP / addressing
- The server's **local LAN IPv4** is written to `.env` `ORG_IP` and used to build
  dashboard/redirect URLs. Clients must be able to reach the server at that address.
- A static/reserved IP is strongly recommended (URL links break if it changes).

---

## 6. Database (PostgreSQL) configuration

Applied by the installer:

- **Instance:** PostgreSQL 18 on port **5432**, service `postgresql-x64-18`.
- **Database:** `dbanalytics`.
- **Roles:** `postgres`, `dbdome_mon_usr`, `dbdome_adm`.
- **`pg_hba.conf`:** `trust` for `127.0.0.1/32` and `::1/128` (local‑only).
- **`postgresql.conf` tuning:**
  - `max_connections = 1000`
  - `shared_buffers = 512MB`
  - `logging_collector = off`
- **Restore:** baseline `dbanalytics_install.backup` restored on first install.

> `max_connections = 1000` is high — ensure RAM is sized accordingly, or front it
> with a pooler for very large deployments.

---

## 7. Disk layout

| Path | Contents |
|---|---|
| `C:\ProgramData\DBDOME\` | Install root (`.env`, `install.log`, `update.log`) |
| `C:\ProgramData\DBDOME\bin\` | Service exes, `nssm.exe`, Grafana, `icons\`, `.env` (the one the services read) |
| `C:\ProgramData\DBDOME\conf\` | Grafana provisioning / `custom.ini` |
| `C:\ProgramData\DBDOME\data\` | `grafana.db` (~20 MB) |
| `C:\Program Files\PostgreSQL\18\` | PostgreSQL binaries + `data\` |

Public desktop shortcut `DBDOME.lnk` → `http://localhost:8080` (icon
`C:\ProgramData\DBDOME\bin\icons\dbdome.ico`).

---

## 8. Windows services

| Service | Startup | Recovery |
|---|---|---|
| `DBDOME_web` / `DBDOME_scheduler` (or combined `DBDOME_dbanalytics`) | Automatic | Auto‑restart on failure (3×, 5 s apart) |
| `DBDOME_Grafana` (NSSM) | Automatic | Auto‑restart on exit |
| `postgresql-x64-18` | Automatic | Windows default |

All services run with privileges sufficient to read `C:\ProgramData\DBDOME` and
bind their ports.

---

## 9. Security considerations

- Restrict inbound 8080/3000/5432 to trusted networks (firewall profiles are
  domain/private by default — review for your environment).
- PostgreSQL uses local `trust` auth; do **not** expose 5432 publicly without
  changing auth.
- Monitored‑database credentials are stored in the analytics DB — protect the
  host and DB access accordingly.
- Change default service/DB passwords before production use.

---

## 10. Pre‑installation checklist

- [ ] Windows Server x64, fully updated
- [ ] Administrator account to run the installer
- [ ] Static/reserved IPv4; firewall manageable
- [ ] Hardware per §2 (CPU/RAM/SSD)
- [ ] Install media/bundle available (DVD `DBDOME_DVD_ROM` or local path)
- [ ] Outbound network access to every monitored DB (host:port)
- [ ] Oracle Instant Client present (only if monitoring Oracle)
- [ ] Ports 8080 / 3000 / 5432 free (not used by other software)

---

*Source of truth: `C:\dev\dbdome_setup\setup.py` (installer) and the DBDOME service/runtime configuration.*
