"""
DBDOME Installer — Full Setup Script
=====================================
Installs DBDOME database monitoring platform from DVD or local source.

Steps:
  1. Detect source media (DVD or local path)
  2. Copy files to C:\\ProgramData\\DBDOME
  3. Install ODBC Driver for SQL Server
  4. Install PostgreSQL 18.3 (port 5444)
  5. Configure pg_hba.conf for local trust auth
  6. Create database, users, restore backup
  7. Configure Grafana datasource (PostgreSQL → dbanalytics)
  8. Create .env configuration files
  9. Configure Windows Firewall
 10. Install dbdome_dbanalytics Windows service (scheduler + HTTP)
 11. Start the Windows service + Grafana process
 12. Validate installation
 13. Create desktop shortcut

Requires: Administrator privileges
Build:    pyinstaller setup.spec
"""

import subprocess
import os
import re
import sys
import time
import stat
import socket
import shutil
import ctypes
import json
import logging
from datetime import datetime
from pathlib import Path

# ════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════

# Version: 2.02.<Build_No>, shared with the service. build.ps1 drops
# version_build.json beside this script (and PyInstaller bundles it), so all
# three artifacts of one build report the same number. Falls back to build 000
# when built outside build.ps1 - a visible marker that it was not a real build.
#
# NOTE: only `build_no` comes from version_build.json - the major/minor live
# HERE, and build.ps1 does NOT patch them (it reads the service's utils/version.py
# for its own display string only). So bumping the stamp alone is not enough:
# 2.02 needs VERSION_MINOR = 2. This sat at 1 while the stamp said 2.02.001, so
# that installer reported 2.01.001 at runtime. Same trap existed in
# dbdome_update/update.py.
VERSION_MAJOR = 2
VERSION_MINOR = 2


def _build_no():
    import json
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, getattr(sys, "_MEIPASS", None),
              os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None):
        if not d:
            continue
        p = os.path.join(d, "version_build.json")
        if os.path.isfile(p):
            try:
                # utf-8-sig: a PS-written stamp carries a BOM and json.load rejects it
                with open(p, encoding="utf-8-sig") as f:
                    return int(json.load(f).get("build_no", 0))
            except Exception:
                continue
    return 0


VERSION = "{}.{:02d}.{:03d}".format(VERSION_MAJOR, VERSION_MINOR, _build_no())

# Paths
DEST_DIR       = r"C:\ProgramData\DBDOME"
PG_INSTALL_DIR = r"C:\Program Files\PostgreSQL\18"
PG_BIN         = os.path.join(PG_INSTALL_DIR, "bin")
PG_DATA        = os.path.join(PG_INSTALL_DIR, "data")
PG_HBA         = os.path.join(PG_DATA, "pg_hba.conf")
PG_CONF        = os.path.join(PG_DATA, "postgresql.conf")
PSQL           = os.path.join(PG_BIN, "psql.exe")
PG_RESTORE     = os.path.join(PG_BIN, "pg_restore.exe")
ENV_FILE       = os.path.join(DEST_DIR, ".env")

# The Windows service a PostgreSQL 18 install registers. Presence of THIS is what
# decides install-vs-adopt in install_postgresql() - see the comment there.
PG_SERVICE_NAME = "postgresql-x64-18"

# Database
# These are DEFAULTS. The setup window (see "SETUP WINDOW" below) lets the
# operator override them before anything is installed; _apply_params() writes the
# chosen values back onto these globals, so every step downstream keeps reading
# the same names it always did. Run with --no-gui to skip the window and use the
# defaults exactly as before.
PG_PORT    = "5432"
DB_NAME    = "dbanalytics"
GEN_DB     = "postgres"
GEN_USER   = "postgres"
USER_MON   = "dbdome_mon_usr"
USER_ADM   = "dbdome_adm"
DB_PASSWORD = "Yd2243796Anz!!"

# Defaults kept for comparison: the PostgreSQL unattended installer is only told
# --datadir / --serverport when the operator actually changed them, so a default
# install runs byte-identically to before this window existed.
_DEFAULT_PG_PORT = PG_PORT
_DEFAULT_PG_DATA = PG_DATA

# Services
BIN_DIR              = os.path.join(DEST_DIR, "bin")
# Public code-signing .cer shipped in the package; imported into the machine
# trust stores so signed DBDOME binaries are trusted here (no "Unknown Publisher").
CERT_FILE            = os.path.join(BIN_DIR, "certification", "dbdome_cert.cer")
GRAFANA_SERVER_EXE   = os.path.join(BIN_DIR, "dbdomeg-server.exe")
# The DBDOME exes load .env from next to the binary, so bin\.env is the one the
# running services actually read. We update both it and the top-level .env.
BIN_ENV_FILE         = os.path.join(BIN_DIR, ".env")

# Grafana is a plain console app (no native SCM support), so we wrap it as a
# Windows service with NSSM. nssm.exe is expected in BIN_DIR (bundled) or PATH.
NSSM_EXE             = os.path.join(BIN_DIR, "nssm.exe")
GRAFANA_SERVICE_NAME = "DBDOME_Grafana"

# DBDOME dbanalytics — two Windows services registered by dbdome_service.exe
DBDOME_SERVICE_EXE       = os.path.join(BIN_DIR, "dbdome_service.exe")
DBDOME_SERVICE_SCHEDULER = "DBDOME_scheduler"   # --service scheduler
DBDOME_SERVICE_WEB       = "DBDOME_web"          # --service web
DBDOME_SERVICES = [
    ("scheduler", DBDOME_SERVICE_SCHEDULER),
    ("web",       DBDOME_SERVICE_WEB),
]
# Single-service builds of dbdome_service.exe register one combined service
# (scheduler + HTTP) and do NOT understand the `--service` flag.
DBDOME_SERVICE_COMBINED = "DBDOME_dbanalytics"

# Resolved at install time once the exe's capability is known.
# Each entry is (svc_arg, svc_name); svc_arg "" => invoke the exe WITHOUT --service.
ACTIVE_SERVICES = []

# Grafana
GRAFANA_CONF_DIR = os.path.join(DEST_DIR, "conf")
GRAFANA_DATA_DIR = os.path.join(DEST_DIR, "data")
GRAFANA_DB_PATH  = os.path.join(GRAFANA_DATA_DIR, "grafana.db")

# Ports
HTTP_PORT    = 8080
GRAFANA_PORT = 3000

# DVD
DVD_LABEL = "DBDOME_DVD_ROM"

# ════════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════════

def _get_log_path():
    """Find a writable location for the install log."""
    candidates = [
        os.path.join(DEST_DIR, "install.log"),
        os.path.join(os.environ.get("TEMP", r"C:\Windows\Temp"), "dbdome_install.log"),
        os.path.join(os.environ.get("USERPROFILE", r"C:\Users\Public"), "dbdome_install.log"),
    ]
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a"):
                pass
            return path
        except (PermissionError, OSError):
            continue
    return None


LOG_FILE = _get_log_path()

_handlers = [logging.StreamHandler(sys.stdout)]
if LOG_FILE:
    _handlers.append(logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("dbdome_setup")


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def get_local_ip():
    # Primary: ask the OS which local interface would route outbound traffic.
    # (UDP connect sends no packets, so this works without real internet as
    # long as a default route exists.)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
            return ip
    except Exception:
        pass
    finally:
        s.close()

    # Fallback: enumerate resolved addresses for this host and pick the first
    # real IPv4 (skips loopback and APIPA/link-local 169.254.x.x).
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def run(cmd, check=False, timeout=None):
    """Run a shell command, log it, return the CompletedProcess."""
    log.info(f"  $ {cmd}")
    try:
        # errors="replace": console tools (robocopy, sc, netsh) emit bytes in the
        # OEM/locale codepage; on a non-UTF-8 Windows (e.g. Hebrew cp1255) a strict
        # decode crashes the reader thread and leaves stdout=None -> .strip() fails.
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        result.stdout = result.stdout or ""
        result.stderr = result.stderr or ""
        if result.stdout.strip():
            for line in result.stdout.strip().split("\n")[:10]:
                log.info(f"    {line}")
        if result.returncode != 0 and result.stderr.strip():
            for line in result.stderr.strip().split("\n")[:5]:
                log.warning(f"    STDERR: {line}")
        if check and result.returncode != 0:
            raise RuntimeError(f"Command failed (exit {result.returncode}): {cmd}")
        return result
    except subprocess.TimeoutExpired:
        log.error(f"  Command timed out after {timeout}s: {cmd}")
        raise


def run_visible(cmd, wait=False):
    """Run a command in a new visible console window."""
    log.info(f"  Starting: {cmd}")
    return subprocess.Popen(cmd, shell=True, creationflags=subprocess.CREATE_NEW_CONSOLE)


def psql_cmd(sql, db=None, user=None):
    """Execute a SQL command via psql."""
    db = db or GEN_DB
    user = user or GEN_USER
    return run(f'"{PSQL}" -U {user} -p {PG_PORT} -d {db} -c "{sql}"')


def step(num, title):
    """Log a step header."""
    log.info("")
    log.info(f"{'═' * 60}")
    log.info(f"  STEP {num}: {title}")
    log.info(f"{'═' * 60}")


# ════════════════════════════════════════════════════════════════
# PREFLIGHT
# ════════════════════════════════════════════════════════════════

def preflight():
    log.info("")
    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║          DBDOME Installation — v" + VERSION.ljust(25) + "  ║")
    log.info("║          " + datetime.now().strftime("%Y-%m-%d %H:%M:%S").ljust(40) + "║")
    log.info("╚══════════════════════════════════════════════════════════╝")
    log.info("")

    if not is_admin():
        log.error("This installer must run as Administrator. Right-click → Run as Administrator.")
        sys.exit(1)

    log.info(f"  Local IP:    {get_local_ip()}")
    log.info(f"  Hostname:    {socket.gethostname()}")
    log.info(f"  Install to:  {DEST_DIR}")


# ════════════════════════════════════════════════════════════════
# STEP 1: DETECT SOURCE & COPY FILES
# ════════════════════════════════════════════════════════════════

def find_source():
    """Find the DBDOME payload folder (the one containing bin\\).

    Order: next to the running exe (ship exe + DBDOME\\ together, run from any
    drive) -> DVD by label -> every drive letter -> dev/runtime locations. A folder
    qualifies only if it contains a bin\\ subdirectory, and the install target is
    never returned as its own source.
    """
    import string
    exe_dir = os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, "frozen", False) else __file__))
    dest_norm = os.path.normcase(os.path.abspath(DEST_DIR))

    candidates = [os.path.join(exe_dir, "DBDOME"), exe_dir]

    try:
        # wmi ships in the build venv (and is a hiddenimport in setup.spec) but is
        # not installed for every interpreter an editor might point at, so the
        # static-analysis pragma keeps it from being flagged. The except below is
        # what actually matters at runtime: no WMI simply means no DVD detection.
        import wmi  # type: ignore[import-not-found]  # noqa: F401
        for disk in wmi.WMI().Win32_LogicalDisk():
            if disk.DriveType == 5 and disk.VolumeName == DVD_LABEL:
                candidates.append(f"{disk.DeviceID}\\DBDOME")
    except Exception:
        log.warning("  WMI not available, skipping DVD detection")

    for d in string.ascii_uppercase:
        candidates.append(rf"{d}:\DBDOME")
    candidates += [r"C:\dbdome", r"C:\dev\dbdome", r"C:\dev\grafana.dbexpert.ai"]

    for path in candidates:
        try:
            if os.path.normcase(os.path.abspath(path)) == dest_norm:
                continue  # never treat the install target itself as the source
            if os.path.exists(path) and os.path.isdir(os.path.join(path, "bin")):
                log.info(f"  Source: {path}")
                return path
        except OSError:
            continue

    log.error("No DBDOME source found. Place the 'DBDOME' payload folder (with a bin\\ "
              "subfolder) next to " + os.path.basename(sys.executable) +
              ", at <drive>:\\DBDOME, or insert the DVD labeled 'DBDOME_DVD_ROM'.")
    sys.exit(1)


def copy_files(src_dir):
    step(1, "Copy DBDOME files")

    os.makedirs(DEST_DIR, exist_ok=True)

    # Robocopy with retry
    cmd = f'robocopy "{src_dir}" "{DEST_DIR}" /E /COPYALL /R:2 /W:3 /NFL /NDL /NP'
    result = run(cmd)

    # Robocopy exit codes: 0-7 = success, 8+ = error
    if result.returncode >= 8:
        log.error(f"Robocopy failed with exit code {result.returncode}")
        sys.exit(1)

    # Remove read-only attributes (DVD source marks everything read-only)
    run(f'attrib -R "{DEST_DIR}\\*.*" /S /D')

    # Set permissions
    run(f'icacls "{DEST_DIR}" /grant "Everyone:(OI)(CI)F" /T /C /Q')

    log.info(f"  Files copied to {DEST_DIR}")


# ════════════════════════════════════════════════════════════════
# STEP 2: INSTALL ODBC DRIVER
# ════════════════════════════════════════════════════════════════

def install_odbc_driver():
    step(2, "Install ODBC Driver for SQL Server")

    # Check if any ODBC driver version is already installed (17 or 18)
    for ver in ("18", "17"):
        result = run(f'reg query "HKLM\\SOFTWARE\\ODBC\\ODBCINST.INI\\ODBC Driver {ver} for SQL Server" /v Driver 2>nul')
        if result.returncode == 0:
            log.info(f"  ODBC Driver {ver} already installed. Skipping.")
            return

    msi_path = os.path.join(DEST_DIR, "postgres", "install", "msodbcsql.msi")
    if not os.path.exists(msi_path):
        log.warning(f"  ODBC installer not found at {msi_path}. SQL Server monitoring may not work.")
        return

    log.info("  Installing ODBC Driver...")
    result = run(f'msiexec /i "{msi_path}" /quiet /norestart IACCEPTMSODBCSQLLICENSETERMS=YES')

    if result.returncode == 0:
        log.info("  ODBC Driver installed.")
    elif result.returncode == 1603:
        # 1603 = Fatal MSI error — usually means a newer/same version already installed
        log.warning("  ODBC installer returned 1603 (already installed or conflict). Continuing.")
    elif result.returncode == 3010:
        # 3010 = Success but reboot required
        log.info("  ODBC Driver installed (reboot may be needed).")
    else:
        log.warning(f"  ODBC installer returned exit code {result.returncode}. Continuing.")


# ════════════════════════════════════════════════════════════════
# STEP 3: INSTALL POSTGRESQL
# ════════════════════════════════════════════════════════════════

def _pg_service_exists():
    """True when the PostgreSQL 18 Windows service is registered on this box."""
    return run(f'sc query "{PG_SERVICE_NAME}"').returncode == 0


def _pg_service_image_path():
    """The service's ImagePath command line, or None.

    Read from the REGISTRY, not `sc qc`: sc.exe wraps its output at the console
    width, and the wrap lands mid-path on the default install - a line-anchored
    regex over that text silently loses the -D data directory. The registry value
    is also immune to sc.exe's localized field labels. `sc qc` stays as a fallback,
    with a regex that spans the continuation lines.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            rf"SYSTEM\CurrentControlSet\Services\{PG_SERVICE_NAME}") as key:
            value = winreg.QueryValueEx(key, "ImagePath")[0]
            if value:
                return value
    except OSError:
        pass
    r = run(f'sc qc "{PG_SERVICE_NAME}"')
    if r.returncode != 0:
        return None
    m = re.search(r"BINARY_PATH_NAME\s*:\s*(.*?)(?=\r?\n\s*[A-Z_]{4,}\s*:|\Z)",
                  r.stdout or "", re.S)
    return " ".join(m.group(1).split()) if m else None


def _pg_service_paths():
    """(bin_dir, data_dir) of the registered PostgreSQL service, or (None, None).

    Read out of the service's OWN ImagePath instead of assumed: a PostgreSQL the
    customer installed themselves may sit outside PG_INSTALL_DIR, and guessing
    the path is what makes setup try to lay a second cluster on top of it.
    """
    image = _pg_service_image_path()
    if not image:
        return None, None
    image = image.strip()
    # e.g.  "C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe" runservice -N "..." -D "C:\...\data" -w
    exe = re.match(r'"([^"]+)"', image)
    exe = exe.group(1) if exe else image.split()[0]
    bin_dir = os.path.dirname(exe) or None
    d = re.search(r'-D\s+"([^"]+)"', image) or re.search(r"-D\s+(\S+)", image)
    return bin_dir, (d.group(1) if d else None)


def _pg_conf_port(data_dir):
    """The port the EXISTING cluster is configured for, from its postgresql.conf."""
    conf = os.path.join(data_dir or "", "postgresql.conf")
    if not os.path.isfile(conf):
        return None
    try:
        with open(conf, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                m = re.match(r"^port\s*=\s*(\d+)", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def _pg_port_reachable(port, host="localhost", timeout=3):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _adopt_existing_postgresql():
    """Point the remaining steps at the PostgreSQL that is ALREADY installed.

    The configured port wins: it is what the operator typed on the setup window,
    what STEP 5 creates the database on, and what lands in .env. A cluster that
    disagrees is reported loudly rather than silently preferred - creating the
    database in the wrong cluster is not something re-running setup undoes.
    """
    global PG_INSTALL_DIR, PG_BIN, PSQL, PG_RESTORE, PG_DATA, PG_HBA, PG_CONF

    bin_dir, data_dir = _pg_service_paths()

    if bin_dir and os.path.isfile(os.path.join(bin_dir, "psql.exe")) and not os.path.exists(PSQL):
        PG_INSTALL_DIR = os.path.dirname(bin_dir)
        PG_BIN         = bin_dir
        PSQL           = os.path.join(bin_dir, "psql.exe")
        PG_RESTORE     = os.path.join(bin_dir, "pg_restore.exe")
        log.info(f"  Existing PostgreSQL is outside the default location: {PG_INSTALL_DIR}")

    # pg_hba.conf / postgresql.conf live in the cluster's data dir. Only follow the
    # service's data dir when the operator did NOT name one on the setup window.
    if (data_dir and os.path.isdir(data_dir)
            and os.path.normcase(os.path.abspath(PG_DATA))
            == os.path.normcase(os.path.abspath(_DEFAULT_PG_DATA))):
        PG_DATA = data_dir
        PG_HBA  = os.path.join(PG_DATA, "pg_hba.conf")
        PG_CONF = os.path.join(PG_DATA, "postgresql.conf")
        log.info(f"  Existing cluster data directory: {PG_DATA}")

    actual = _pg_conf_port(PG_DATA)
    log.info(f"  Port from the setup window: {PG_PORT}")
    if actual:
        log.info(f"  Port in the existing postgresql.conf: {actual}")
        if actual != PG_PORT:
            log.warning(f"  PORT MISMATCH: the installed PostgreSQL is configured for {actual}, "
                        f"the setup window says {PG_PORT}. Setup uses {PG_PORT}.")

    if not _pg_port_reachable(PG_PORT):
        log.error(f"Nothing is accepting connections on port {PG_PORT}.")
        if actual and actual != PG_PORT:
            log.error(f"The '{PG_SERVICE_NAME}' service is configured for port {actual}. "
                      f"Re-run setup with PostgreSQL port = {actual}, or start a server on {PG_PORT}.")
        else:
            log.error(f"Start the '{PG_SERVICE_NAME}' service and re-run setup.")
        sys.exit(1)

    log.info(f"  Adopted the installed PostgreSQL on port {PG_PORT}; "
             f"database '{DB_NAME}' will be created there (STEP 5).")


def install_postgresql():
    step(3, "Install PostgreSQL 18.3")

    # An already-installed PostgreSQL is ADOPTED, not replaced: the database named
    # on the setup window is created inside that cluster, on the port from the same
    # window. Detection is by the SERVICE rather than by a path, because a customer
    # install can live anywhere and a path check would miss it and then install a
    # second cluster over the top.
    if _pg_service_exists():
        log.info(f"  PostgreSQL service '{PG_SERVICE_NAME}' is registered - adopting it "
                 f"instead of installing.")
        _adopt_existing_postgresql()
        return

    # Binaries present but no service (e.g. a half-removed install): keep the old
    # skip so this stays no worse than before.
    if os.path.exists(PSQL):
        log.info("  PostgreSQL binaries already present (no service registered). Skipping.")
        return

    import glob as _glob
    pg_dir = os.path.join(DEST_DIR, "postgres", "install")
    # Accept any PostgreSQL 18 build in the package (e.g. postgresql-18.3-3-windows-x64.exe).
    matches = sorted(_glob.glob(os.path.join(pg_dir, "postgresql-18*.exe")))
    installer = matches[0] if matches else os.path.join(pg_dir, "postgresql-18.3-3-windows-x64.exe")

    if not os.path.exists(installer):
        # No bundled installer and no local PostgreSQL: don't hard-fail. Continue —
        # the database steps will install the schema onto an existing PostgreSQL if
        # one is reachable (and fail clearly there if there is none).
        log.warning(f"  PostgreSQL not installed and no postgresql-18*.exe in {pg_dir}. "
                    f"Skipping PG install; the database will be installed on an existing "
                    f"PostgreSQL if one is reachable.")
        return
    log.info(f"  PostgreSQL 18 installer: {os.path.basename(installer)}")

    log.info("  Installing PostgreSQL (unattended)... this may take a few minutes.")
    # Only pass the flags the operator actually changed in the setup window: with
    # neither, this is the exact command line used before the window existed.
    extra = ""
    if os.path.normcase(os.path.abspath(PG_DATA)) != os.path.normcase(os.path.abspath(_DEFAULT_PG_DATA)):
        extra += f' --datadir "{PG_DATA}"'
        log.info(f"  Data directory: {PG_DATA}")
    if PG_PORT != _DEFAULT_PG_PORT:
        extra += f' --serverport {PG_PORT}'
        log.info(f"  Server port:    {PG_PORT}")
    run(f'"{installer}" --mode unattended{extra}', timeout=None)   # no time limit on the installer

    if not os.path.exists(PSQL):
        log.error("PostgreSQL installation failed — psql.exe not found")
        sys.exit(1)

    log.info("  PostgreSQL installed successfully.")


# ════════════════════════════════════════════════════════════════
# STEP 4: CONFIGURE PG_HBA.CONF
# ════════════════════════════════════════════════════════════════

def configure_pg_hba():
    step(4, "Configure pg_hba.conf")

    if not os.path.exists(PG_HBA):
        log.warning(f"  pg_hba.conf not found at {PG_HBA}. Skipping.")
        return

    # Backup original
    backup = PG_HBA + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(PG_HBA, backup)
        log.info(f"  Backed up to {backup}")

    with open(PG_HBA, "r") as f:
        lines = f.readlines()

    # Rules we need
    rules = {
        "127.0.0.1/32": "host    all    all    127.0.0.1/32    trust\n",
        "::1/128":       "host    all    all    ::1/128         trust\n",
    }

    for key, rule in rules.items():
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith("host") and key in line:
                lines[i] = rule
                found = True
                break
        if not found:
            lines.append(rule)

    with open(PG_HBA, "w") as f:
        f.writelines(lines)

    log.info("  pg_hba.conf updated with trust auth for localhost")

    # Apply postgresql.conf tuning before the restart so both changes land in one cycle
    _apply_postgresql_conf_settings()

    # Restart PostgreSQL to pick up changes
    log.info("  Restarting PostgreSQL...")
    run("net stop postgresql-x64-18")
    time.sleep(2)
    run("net start postgresql-x64-18")
    time.sleep(3)
    log.info("  PostgreSQL restarted.")


def _apply_postgresql_conf_settings():
    """Set DBDOME-required values in postgresql.conf, replacing any existing
    line for each key. Restart of the service is the caller's responsibility."""
    if not os.path.exists(PG_CONF):
        log.warning(f"  postgresql.conf not found at {PG_CONF}. Skipping conf tuning.")
        return

    settings = {
        "max_connections":   "1000",   # change requires restart
        "logging_collector": "off",
        "shared_buffers":    "512MB",  # change requires restart
    }

    backup = PG_CONF + ".bak"
    if not os.path.exists(backup):
        shutil.copy2(PG_CONF, backup)
        log.info(f"  Backed up to {backup}")

    with open(PG_CONF, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    import re
    remaining = dict(settings)
    for i, line in enumerate(lines):
        for key in list(remaining):
            # Match "key = ..." or "#key = ..." (commented), with optional whitespace
            if re.match(rf"^\s*#?\s*{re.escape(key)}\s*=", line):
                lines[i] = f"{key} = {remaining[key]}\n"
                del remaining[key]
                break

    # Append any keys that weren't already in the file
    if remaining:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append("\n# DBDOME tuning\n")
        for key, val in remaining.items():
            lines.append(f"{key} = {val}\n")

    with open(PG_CONF, "w", encoding="utf-8") as f:
        f.writelines(lines)

    for key, val in settings.items():
        log.info(f"  postgresql.conf: {key} = {val}")


# ════════════════════════════════════════════════════════════════
# STEP 5: CREATE DATABASE & USERS
# ════════════════════════════════════════════════════════════════

def create_database():
    step(5, "Create database and users")

    # Idempotent by CHECK, not by ignored error. CREATE DATABASE has no
    # IF NOT EXISTS, and on an ADOPTED PostgreSQL the cluster may already hold
    # unrelated databases - so the log has to say plainly whether this run
    # created the database or found it, and on which port.
    probe = run(f'"{PSQL}" -U {GEN_USER} -p {PG_PORT} -d {GEN_DB} -tA '
                f'-c "SELECT 1 FROM pg_database WHERE datname = \'{DB_NAME}\';"')
    if (probe.stdout or "").strip() == "1":
        log.info(f"  Database '{DB_NAME}' already exists on port {PG_PORT} - keeping it.")
    else:
        psql_cmd(f"CREATE DATABASE {DB_NAME};")
        log.info(f"  Database '{DB_NAME}' created on port {PG_PORT}.")

    # Create users
    for user in [USER_MON, USER_ADM, "postgres"]:
        psql_cmd(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{user}') THEN CREATE ROLE {user} LOGIN PASSWORD '{DB_PASSWORD}'; END IF; END $$;")
        psql_cmd(f"ALTER ROLE {user} WITH SUPERUSER;")

    log.info(f"  Users created: {USER_MON}, {USER_ADM}, postgres")


# ════════════════════════════════════════════════════════════════
# STEP 6: RESTORE BACKUP
# ════════════════════════════════════════════════════════════════

def restore_database():
    step(6, "Restore dbanalytics backup")

    backup_file = os.path.join(DEST_DIR, "postgres", "install", "dbanalytics_install.backup")
    if not os.path.exists(backup_file):
        log.error(f"Backup file not found: {backup_file}")
        sys.exit(1)

    # Check if database already has data
    result = run(f'"{PSQL}" -U {GEN_USER} -p {PG_PORT} -d {DB_NAME} -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = \'rootcause\';"')
    if result.stdout.strip() and int(result.stdout.strip()) > 0:
        log.info("  Database already has data. Skipping restore.")
        return

    log.info("  Restoring backup... this may take a minute.")

    # Determine if it's a pg_dump custom format or plain SQL
    with open(backup_file, "rb") as f:
        header = f.read(5)

    if header[:5] == b"PGDMP":
        # Custom format — use pg_restore
        run(f'"{PG_RESTORE}" -h localhost -U {GEN_USER} -p {PG_PORT} -d {DB_NAME} --no-owner --no-acl "{backup_file}"')
    else:
        # Plain SQL — use psql
        run(f'"{PSQL}" -h localhost -U {GEN_USER} -p {PG_PORT} -d {DB_NAME} -f "{backup_file}"')

    log.info("  Database restored.")


def set_rootcause_key_defaults():
    """Give the app/reporting roles the rootcause decryption key as a role-level
    default GUC (rootcause.k), so sessions that bypass the app connection factory
    -- the Grafana PostgreSQL datasource, report tools, ad-hoc psql -- can still
    decrypt detection logic via rootcause.dec(). Without this, v_rootcauses
    returns NULL content in Grafana. The key is read from the deployed bin\\.env
    (shared, never hardcoded); it lands in pg_db_role_setting, NOT in a plain
    pg_dump of the data, so the seed backup stays ciphertext-only.
    NOTE: intentionally NOT applied to *_grafana_ro (kept unable to decrypt)."""
    log.info("  Setting rootcause.k role defaults (Grafana/report decryption)...")
    env_path = os.path.join(DEST_DIR, "bin", ".env")
    key = None
    try:
        with open(env_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("DBDOME_SECRET_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    except Exception as e:
        log.warning(f"  Could not read DBDOME_SECRET_KEY ({e}); skipping role key defaults.")
        return
    if not key:
        log.warning("  DBDOME_SECRET_KEY not found in .env; skipping role key defaults.")
        return
    key_sql = key.replace("'", "''")
    for role in (USER_MON, USER_ADM, "postgres"):
        psql_cmd(f"ALTER ROLE {role} SET rootcause.k = '{key_sql}';", db=DB_NAME)
    log.info(f"  rootcause.k set as role default on: {USER_MON}, {USER_ADM}, postgres")


# ════════════════════════════════════════════════════════════════
# STEP 7: CONFIGURE GRAFANA
# ════════════════════════════════════════════════════════════════

def configure_grafana():
    step(7, "Configure Grafana")

    # Create datasource provisioning for dbanalytics
    ds_dir = os.path.join(GRAFANA_CONF_DIR, "provisioning", "datasources")
    os.makedirs(ds_dir, exist_ok=True)

    ds_file = os.path.join(ds_dir, "dbdome-datasource.yaml")
    with open(ds_file, "w") as f:
        f.write("apiVersion: 1\n\n")
        f.write("datasources:\n")
        f.write("  - name: DBDOME-PostgreSQL\n")
        f.write("    type: postgres\n")
        f.write("    access: proxy\n")
        f.write(f"    url: localhost:{PG_PORT}\n")
        f.write(f"    user: {USER_MON}\n")
        f.write(f"    database: {DB_NAME}\n")
        f.write("    isDefault: true\n")
        f.write("    jsonData:\n")
        f.write("      sslmode: disable\n")
        f.write("      postgresVersion: 1700\n")
        f.write("    secureJsonData:\n")
        f.write(f"      password: {DB_PASSWORD}\n")
        f.write("    editable: true\n")

    log.info(f"  Grafana datasource configured: {ds_file}")

    # Update Grafana custom.ini with http port if not set
    custom_ini = os.path.join(GRAFANA_CONF_DIR, "custom.ini")
    if os.path.exists(custom_ini):
        # Fix permissions on conf directory (files may be read-only from DVD copy)
        run(f'icacls "{GRAFANA_CONF_DIR}" /grant "Everyone:(OI)(CI)F" /T /C /Q')
        try:
            os.chmod(custom_ini, stat.S_IWRITE | stat.S_IREAD)
        except Exception:
            pass

        with open(custom_ini, "r") as f:
            content = f.read()

        if "[server]" not in content:
            with open(custom_ini, "a") as f:
                f.write(f"\n[server]\nhttp_port = {GRAFANA_PORT}\nroot_url = http://localhost:{GRAFANA_PORT}/\n")
            log.info(f"  Grafana port set to {GRAFANA_PORT}")
        else:
            log.info(f"  Grafana [server] section already configured.")

    # Record grafana.db location in config.global_params so http_server's
    # update_dashboard_ip can find it on every startup.
    grafana_db_sql = (
        f"INSERT INTO config.global_params (key, value) VALUES ('grafana_db', '{GRAFANA_DB_PATH.replace(chr(92), chr(92)+chr(92))}') "
        f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;"
    )
    psql_cmd(grafana_db_sql, db=DB_NAME)
    log.info(f"  config.global_params: grafana_db = {GRAFANA_DB_PATH}")

    log.info("  Grafana configuration complete.")


# ════════════════════════════════════════════════════════════════
# STEP 8: CREATE .ENV FILES
# ════════════════════════════════════════════════════════════════

def _merge_env(path, updates):
    """Set each key in `updates` in the .env at `path`, replacing the existing
    line for that key in place and preserving every other line (comments,
    extra keys like ORACLE_CLIENT_LIB_DIR / GRAFANA_EXE). Creates the file if
    it doesn't exist."""
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}\n"

    # Append any keys that weren't already present
    if remaining:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        for key, val in remaining.items():
            lines.append(f"{key}={val}\n")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ── Self-contained secret encryption ─────────────────────────────
# Same Fernet/enc:v1: scheme as the updater and the app's
# utils.secrets_crypto. The key is DBDOME_SECRET_KEY in bin\.env (ships on the
# media; generated here if absent), so the installed .env never holds the DB
# password in plaintext.
_ENC_PREFIX = "enc:v1:"


def _sc_key():
    key = None
    try:
        with open(BIN_ENV_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("DBDOME_SECRET_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    except OSError:
        pass
    if key:
        return key.encode()
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    _merge_env(BIN_ENV_FILE, {"DBDOME_SECRET_KEY": key})
    log.info("  generated DBDOME_SECRET_KEY in bin\\.env")
    return key.encode()


def _sc_encrypt(v):
    if v is None or v == "" or (isinstance(v, str) and v.startswith(_ENC_PREFIX)):
        return v
    from cryptography.fernet import Fernet
    return _ENC_PREFIX + Fernet(_sc_key()).encrypt(str(v).encode("utf-8")).decode("ascii")


def create_env_files():
    step(8, "Create configuration files")

    local_ip = get_local_ip()
    if local_ip.startswith("127.") or local_ip.startswith("169.254."):
        log.warning(f"  Could not determine a real machine IP — ORG_IP set to {local_ip}. "
                    "Check the network adapter/default route.")

    # PG_PASSWORD is written encrypted (enc:v1:, key = DBDOME_SECRET_KEY in
    # bin\.env) — the services decrypt it via utils.secrets_crypto. Falls back
    # to plaintext only if encryption is impossible, so the install never breaks.
    try:
        pg_password_env = _sc_encrypt(DB_PASSWORD)
    except Exception as e:
        log.warning(f"  Could not encrypt PG_PASSWORD ({e}); writing plaintext.")
        pg_password_env = DB_PASSWORD

    # Managed keys the installer owns. Other keys in the file are left untouched.
    updates = {
        "PG_HOST":     "localhost",
        "PG_PORT":     PG_PORT,
        "PG_USER":     USER_MON,
        "PG_PASSWORD": pg_password_env,
        "PG_DB":       DB_NAME,
        "ORG_IP":      local_ip,
        "LLM":         os.path.join(DEST_DIR, "all-MiniLM-L6-v2"),
    }

    # Update both the top-level .env and bin\.env. bin\.env is the one the
    # running exes actually read (they load .env next to the binary), so the
    # ORG_IP fix only takes effect once that file is updated in place.
    for path in (ENV_FILE, BIN_ENV_FILE):
        _merge_env(path, updates)
        log.info(f"  Updated {path} (ORG_IP={local_ip})")
        # Set read permissions for all users
        run(f'icacls "{path}" /grant "Everyone:R" /T /C /Q')

    # Update global vars in database
    try:
        import psycopg2
        conn = psycopg2.connect(
            host="localhost", port=PG_PORT, dbname=DB_NAME,
            user=USER_ADM, password=DB_PASSWORD
        )
        cur = conn.cursor()
        cur.execute("CALL config.set_local_ip(%s);", (local_ip,))
        conn.commit()
        cur.close()
        conn.close()
        log.info(f"  Updated config.local_ip = {local_ip}")
    except Exception as e:
        log.warning(f"  Could not update global vars: {e}")

    log.info("  Configuration files created.")


# ════════════════════════════════════════════════════════════════
# STEP 9: FIREWALL RULES
# ════════════════════════════════════════════════════════════════

def configure_firewall():
    step(9, "Configure Windows Firewall")

    rules = [
        ("DBDOME HTTP Server",    HTTP_PORT),
        ("DBDOME Grafana",        GRAFANA_PORT),
        ("DBDOME PostgreSQL",     int(PG_PORT)),
    ]

    for name, port in rules:
        # Delete old rule if exists
        run(f'netsh advfirewall firewall delete rule name="{name}" >nul 2>&1')
        # Create new rule
        result = run(
            f'netsh advfirewall firewall add rule name="{name}" '
            f'dir=in action=allow protocol=TCP localport={port} '
            f'profile=domain,private'
        )
        if result.returncode == 0:
            log.info(f"  Firewall rule: {name} (TCP {port}) — OK")
        else:
            log.warning(f"  Firewall rule: {name} (TCP {port}) — FAILED")

    log.info("  Firewall rules configured.")


def trust_signing_cert():
    """Import the self-signed dbdome code-signing cert into the machine's Trusted
    Root + Trusted Publishers stores so signed DBDOME binaries are trusted here
    (no "Unknown Publisher"). Self-signed -> needs BOTH stores. Runs elevated (the
    installer requires admin). Skips gracefully if no cert shipped."""
    step(16, "Trust DBDOME code-signing certificate")
    if not os.path.exists(CERT_FILE):
        log.info(f"  Signing cert not found ({CERT_FILE}) — skipping "
                 f"(unsigned or CA-signed build).")
        return
    for store in ("Root", "TrustedPublisher"):
        result = run(f'certutil -addstore -f {store} "{CERT_FILE}"')
        if result.returncode == 0:
            log.info(f"  Import into LocalMachine\\{store} — OK")
        else:
            log.warning(f"  Import into LocalMachine\\{store} — FAILED (rc={result.returncode})")


# ════════════════════════════════════════════════════════════════
# STEP 10: INSTALL DBDOME WINDOWS SERVICE
# ════════════════════════════════════════════════════════════════

def _service_supports_mode_flag(exe):
    """Detect whether dbdome_service.exe understands `--service <mode>`.

    Newer builds expose full/scheduler/web; older single-service builds reject
    the flag ("option --service not recognized"). We probe with an INVALID mode
    so neither build actually starts the backend:
      - new build (argparse): exits with "invalid choice: '__probe__'"
      - old build (pywin32):  exits with "option --service not recognized"
    """
    try:
        r = subprocess.run(f'"{exe}" --service __probe__', shell=True,
                           capture_output=True, text=True, timeout=60)
        text = (r.stdout + r.stderr).lower()
        return "not recognized" not in text
    except Exception as e:
        log.warning(f"  Could not probe --service support ({e}); assuming single-service build.")
        return False


def _svc_cmd(svc_arg, verb):
    """Build a dbdome_service.exe command. svc_arg '' omits the --service flag."""
    flag = f"--service {svc_arg} " if svc_arg else ""
    return f'"{DBDOME_SERVICE_EXE}" {flag}{verb}'


def install_dbdome_service():
    step(10, "Install DBDOME dbanalytics Windows service(s)")

    global ACTIVE_SERVICES

    if not os.path.exists(DBDOME_SERVICE_EXE):
        log.error(f"  Service exe not found: {DBDOME_SERVICE_EXE}")
        log.error("  Skipping service install — fix the path or re-copy the bundle.")
        return False

    # Decide which service model this exe supports.
    if _service_supports_mode_flag(DBDOME_SERVICE_EXE):
        ACTIVE_SERVICES = list(DBDOME_SERVICES)            # split scheduler + web
        log.info("  Service exe supports --service: installing split scheduler + web services.")
    else:
        ACTIVE_SERVICES = [("", DBDOME_SERVICE_COMBINED)]  # combined single service
        log.info(f"  Service exe is a single-service build (no --service): installing combined "
                 f"'{DBDOME_SERVICE_COMBINED}' (scheduler + HTTP).")

    # Remove the legacy DBDOME_Watchdog scheduled task (older installers used a
    # scheduled task to keep services alive; SCM auto-restart now handles that).
    legacy_task = "DBDOME_Watchdog"
    if run(f'schtasks /Query /TN "{legacy_task}"').returncode == 0:
        log.info(f"  Removing legacy scheduled task: {legacy_task}")
        run(f'schtasks /Delete /TN "{legacy_task}" /F')

    # Remove any DBDOME services that are NOT in our target set so old
    # split/combined leftovers from a previous install don't linger.
    target_names = {name for _, name in ACTIVE_SERVICES}
    for stale in ("DBDOME_dbanalytics", "DBDOME_scheduler", "DBDOME_web"):
        if stale not in target_names and run(f'sc query "{stale}"').returncode == 0:
            log.info(f"  Removing stale service '{stale}'")
            run(f'sc stop "{stale}"')
            time.sleep(1)
            run(f'sc delete "{stale}"')
            time.sleep(1)

    success = True
    for svc_arg, svc_name in ACTIVE_SERVICES:
        # Stop + remove if already registered so a clean install takes.
        if run(f'sc query "{svc_name}"').returncode == 0:
            log.info(f"  Existing service '{svc_name}' found — stopping and removing first.")
            run(_svc_cmd(svc_arg, "stop"))
            time.sleep(2)
            run(_svc_cmd(svc_arg, "remove"))
            time.sleep(2)

        result = run(_svc_cmd(svc_arg, "install"))
        if result.returncode != 0:
            log.error(f"  Service install failed for '{svc_name}' (exit {result.returncode}).")
            success = False
            continue

        run(f'sc config "{svc_name}" start= auto')
        # Auto-restart on failure: 3 attempts, 5s apart, daily counter reset.
        run(f'sc failure "{svc_name}" reset= 86400 actions= restart/5000/restart/5000/restart/5000')
        log.info(f"  Service '{svc_name}' installed (auto-start, auto-restart on failure).")

    return success


# ════════════════════════════════════════════════════════════════
# STEP 11: START SERVICES
# ════════════════════════════════════════════════════════════════

def start_services():
    step(11, "Start DBDOME services")

    # Start the DBDOME Windows service(s) that install_dbdome_service() registered.
    if os.path.exists(DBDOME_SERVICE_EXE):
        for svc_arg, svc_name in (ACTIVE_SERVICES or DBDOME_SERVICES):
            sc_query = run(f'sc query "{svc_name}"')
            if sc_query.returncode == 0 and "RUNNING" in sc_query.stdout:
                log.info(f"  {svc_name}: already running")
            else:
                result = run(_svc_cmd(svc_arg, "start"))
                if result.returncode == 0:
                    log.info(f"  {svc_name}: started")
                else:
                    log.warning(f"  {svc_name}: start returned exit {result.returncode}")
        time.sleep(3)
    else:
        log.warning(f"  Service exe not found: {DBDOME_SERVICE_EXE}")

    log.info("  All services started.")


# ════════════════════════════════════════════════════════════════
# STEP 12: VALIDATE INSTALLATION
# ════════════════════════════════════════════════════════════════

def validate():
    step(12, "Validate installation")

    checks = []

    # Check PostgreSQL
    result = run(f'"{PSQL}" -U {GEN_USER} -p {PG_PORT} -d {DB_NAME} -t -c "SELECT 1;"')
    ok = result.returncode == 0
    checks.append(("PostgreSQL connection", ok))

    # Check rootcause schema
    result = run(f'"{PSQL}" -U {GEN_USER} -p {PG_PORT} -d {DB_NAME} -t -c "SELECT COUNT(*) FROM rootcause.root_causes;"')
    count = result.stdout.strip() if ok else "0"
    checks.append((f"Rootcause taxonomy ({count} root causes)", int(count or 0) > 0))

    # Check the DBDOME Windows service(s) we actually installed are running
    for _, svc_name in (ACTIVE_SERVICES or DBDOME_SERVICES):
        sc_query = run(f'sc query "{svc_name}"')
        service_running = sc_query.returncode == 0 and "RUNNING" in sc_query.stdout
        checks.append((f"Windows service: {svc_name}", service_running))

    # Grafana is launched at the very end of setup (after validation),
    # so we don't check it here.

    # Check .env
    checks.append((".env configuration", os.path.exists(ENV_FILE)))

    # Print results
    log.info("")
    log.info("  Validation Results:")
    log.info("  " + "─" * 50)
    all_ok = True
    for name, ok in checks:
        status = "PASS" if ok else "FAIL"
        icon = "✓" if ok else "✗"
        log.info(f"    {icon} {name}: {status}")
        if not ok:
            all_ok = False

    log.info("  " + "─" * 50)
    if all_ok:
        log.info("  All checks PASSED")
    else:
        log.warning("  Some checks FAILED — review the log above")

    return all_ok


# ════════════════════════════════════════════════════════════════
# STEP 13: CREATE DESKTOP SHORTCUT
# ════════════════════════════════════════════════════════════════

def create_shortcut():
    step(13, "Create desktop shortcut")

    try:
        import win32com.client

        desktop_path = os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop")
        shortcut_path = os.path.join(desktop_path, "DBDOME.lnk")
        icon_path = os.path.join(DEST_DIR, "bin", "icons", "dbdome.ico")

        # Fallback icon path
        if not os.path.exists(icon_path):
            icon_path = r"C:\dev\dbanalytics\icons\dbdome.ico"

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(shortcut_path)
        shortcut.TargetPath = f"https://localhost:{HTTP_PORT}"
        if os.path.exists(icon_path):
            shortcut.IconLocation = icon_path
        shortcut.Save()
        log.info(f"  Desktop shortcut created: {shortcut_path}")

    except ImportError:
        log.warning("  win32com not available — shortcut not created")
    except Exception as e:
        log.warning(f"  Shortcut creation failed: {e}")


# ════════════════════════════════════════════════════════════════
# STEP 14: INSTALL GRAFANA WINDOWS SERVICE (dbdomeg-server.exe via NSSM)
# ════════════════════════════════════════════════════════════════

def _find_nssm():
    """Return a usable nssm.exe path: bundled in BIN_DIR, else on PATH."""
    if os.path.exists(NSSM_EXE):
        return NSSM_EXE
    found = shutil.which("nssm")
    return found  # may be None


def _launch_grafana_fallback():
    """Last-resort foreground launch if NSSM isn't available. Dies on logoff —
    only used when the exe can't be registered as a service."""
    exe_name = os.path.basename(GRAFANA_SERVER_EXE)
    result = run(f'tasklist /FI "IMAGENAME eq {exe_name}" /NH 2>nul')
    if exe_name.lower() in result.stdout.lower():
        log.info("  DBDOME Grafana: already running")
        return
    log.info(f"  Starting (fallback, no service): {GRAFANA_SERVER_EXE}  (cwd={BIN_DIR})")
    subprocess.Popen(
        [GRAFANA_SERVER_EXE],
        cwd=BIN_DIR,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    time.sleep(2)


def install_grafana_service():
    step(14, "Install DBDOME Grafana Windows service (dbdomeg-server.exe)")

    if not os.path.exists(GRAFANA_SERVER_EXE):
        log.warning(f"  Grafana exe not found: {GRAFANA_SERVER_EXE} — skipping.")
        return

    nssm = _find_nssm()
    if not nssm:
        log.warning("  nssm.exe not found in BIN_DIR or PATH — falling back to a "
                    "foreground launch (no Windows service).")
        _launch_grafana_fallback()
        return

    log.info(f"  Using NSSM: {nssm}")

    # Remove any existing service so a clean install takes.
    if run(f'sc query "{GRAFANA_SERVICE_NAME}"').returncode == 0:
        log.info(f"  Existing service '{GRAFANA_SERVICE_NAME}' found — stopping and removing first.")
        run(f'"{nssm}" stop "{GRAFANA_SERVICE_NAME}"')
        time.sleep(2)
        run(f'"{nssm}" remove "{GRAFANA_SERVICE_NAME}" confirm')
        time.sleep(2)

    # Install + configure. AppDirectory=BIN_DIR so grafana resolves conf/data
    # relative to its install root (same as the old cwd=BIN_DIR launch).
    svc_log = os.path.join(DEST_DIR, "grafana-svc.log")
    result = run(f'"{nssm}" install "{GRAFANA_SERVICE_NAME}" "{GRAFANA_SERVER_EXE}"')
    if result.returncode != 0:
        log.error(f"  NSSM install failed (exit {result.returncode}) — falling back to foreground launch.")
        _launch_grafana_fallback()
        return

    run(f'"{nssm}" set "{GRAFANA_SERVICE_NAME}" AppDirectory "{BIN_DIR}"')
    run(f'"{nssm}" set "{GRAFANA_SERVICE_NAME}" Start SERVICE_AUTO_START')
    run(f'"{nssm}" set "{GRAFANA_SERVICE_NAME}" AppStdout "{svc_log}"')
    run(f'"{nssm}" set "{GRAFANA_SERVICE_NAME}" AppStderr "{svc_log}"')
    # Restart the exe automatically if it exits unexpectedly.
    run(f'"{nssm}" set "{GRAFANA_SERVICE_NAME}" AppExit Default Restart')
    log.info(f"  Service '{GRAFANA_SERVICE_NAME}' installed (auto-start, auto-restart, log={svc_log}).")

    # Start it now.
    result = run(f'"{nssm}" start "{GRAFANA_SERVICE_NAME}"')
    time.sleep(3)
    sc_query = run(f'sc query "{GRAFANA_SERVICE_NAME}"')
    if sc_query.returncode == 0 and "RUNNING" in sc_query.stdout:
        log.info(f"  {GRAFANA_SERVICE_NAME}: running")
    else:
        log.warning(f"  {GRAFANA_SERVICE_NAME}: not running yet — check {svc_log}")


# ════════════════════════════════════════════════════════════════
# STEP 15: RESTART GRAFANA SERVICE (clear first-start plugin errors)
# ════════════════════════════════════════════════════════════════

def restart_grafana_service():
    step(15, "Restart DBDOME Grafana service (clear plugin issues)")

    # Only meaningful if the service actually exists (NSSM path succeeded).
    if run(f'sc query "{GRAFANA_SERVICE_NAME}"').returncode != 0:
        log.info(f"  {GRAFANA_SERVICE_NAME} not installed as a service — nothing to restart.")
        return

    nssm = _find_nssm()
    if nssm:
        run(f'"{nssm}" restart "{GRAFANA_SERVICE_NAME}"')
    else:
        # Fall back to SCM stop/start if nssm.exe isn't available.
        run(f'sc stop "{GRAFANA_SERVICE_NAME}"')
        time.sleep(3)
        run(f'sc start "{GRAFANA_SERVICE_NAME}"')

    time.sleep(4)
    sc_query = run(f'sc query "{GRAFANA_SERVICE_NAME}"')
    if sc_query.returncode == 0 and "RUNNING" in sc_query.stdout:
        log.info(f"  {GRAFANA_SERVICE_NAME}: restarted and running")
    else:
        log.warning(f"  {GRAFANA_SERVICE_NAME}: not running after restart — check the service log")


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════
# SETUP WINDOW
# ════════════════════════════════════════════════════════════════
#
# Double-clicking setup.exe opens a window to confirm the install parameters
# first; "Start installation" then swaps the same window to a log pane carrying
# exactly what used to scroll past on the console (the install still logs through
# `log`, so nothing about the steps themselves changed).
#
# Styling mirrors static/css/dbdome-theme.css so it reads as one of the pages:
# the same dark tokens, the 52px header band, the lowercase "dbdome" brand and
# the product logo.
#
# --no-gui (or a machine without tkinter) falls straight through to the original
# console behaviour.

_THEME = {
    "canvas":    "#111217",   # --bg-canvas
    "primary":   "#181b1f",   # --bg-primary
    "secondary": "#22252b",   # --bg-secondary
    "hover":     "#2e3034",   # --bg-hover
    "border":    "#343436",   # --border
    "text":      "#ccccdc",   # --text-primary
    "muted":     "#8e8e9b",   # --text-secondary
    "emphasis":  "#ffffff",   # --text-emphasis
    "blue":      "#3d71d9",   # --blue-base
    "blue_lit":  "#33a2e5",   # --blue
    "green":     "#6ccf8e",   # --green-text
    "red":       "#ff5286",   # --red-text
    "yellow":    "#ecbb13",   # --yellow
}

# label, global name, hint, is_password
_PARAM_FIELDS = [
    ("PostgreSQL port",      "PG_PORT",     "Port the DBDOME PostgreSQL listens on", False),
    ("Database name",        "DB_NAME",     "DBDOME analytics database",             False),
    ("Maintenance database", "GEN_DB",      "Database used to create the others",    False),
    ("Superuser",            "GEN_USER",    "PostgreSQL superuser",                  False),
    ("Monitoring user",      "USER_MON",    "Read-only collector / Grafana login",   False),
    ("Admin user",           "USER_ADM",    "DBDOME application login",              False),
    ("Database password",    "DB_PASSWORD", "Applied to the users created above",    True),
]


def _asset(*names):
    """Locate a bundled asset (logo / icon) next to the exe, inside the PyInstaller
    bundle, or in the dev tree. Returns None when it isn't shipped — the window
    then renders the wordmark alone rather than failing."""
    roots = [
        getattr(sys, "_MEIPASS", None),
        os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)),
        DEST_DIR,
        os.path.join(DEST_DIR, "bin"),
        r"C:\dev\dbanalytics\icons",
    ]
    for root in roots:
        if not root:
            continue
        for name in names:
            p = os.path.join(root, name)
            if os.path.isfile(p):
                return p
    return None


def _apply_params(values):
    """Write the window's values onto the module globals every step already reads."""
    global PG_PORT, DB_NAME, GEN_DB, GEN_USER, USER_MON, USER_ADM, DB_PASSWORD
    global PG_DATA, PG_HBA, PG_CONF

    PG_PORT     = values["PG_PORT"].strip()
    DB_NAME     = values["DB_NAME"].strip()
    GEN_DB      = values["GEN_DB"].strip()
    GEN_USER    = values["GEN_USER"].strip()
    USER_MON    = values["USER_MON"].strip()
    USER_ADM    = values["USER_ADM"].strip()
    DB_PASSWORD = values["DB_PASSWORD"]

    data_dir = values["PG_DATA"].strip()
    if data_dir:
        PG_DATA = data_dir
        # pg_hba.conf / postgresql.conf live in the data directory, so they move with it.
        PG_HBA  = os.path.join(PG_DATA, "pg_hba.conf")
        PG_CONF = os.path.join(PG_DATA, "postgresql.conf")


def _validate_params(values):
    """Return a list of human-readable problems (empty list = good to go)."""
    problems = []
    port = values["PG_PORT"].strip()
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        problems.append("PostgreSQL port must be a number between 1 and 65535.")
    for label, key, _hint, _pw in _PARAM_FIELDS:
        if key == "PG_PORT":
            continue
        if not values[key].strip():
            problems.append(f"{label} cannot be empty.")
    for label, key in (("Database name", "DB_NAME"), ("Maintenance database", "GEN_DB"),
                       ("Superuser", "GEN_USER"), ("Monitoring user", "USER_MON"),
                       ("Admin user", "USER_ADM")):
        v = values[key].strip()
        if v and (not v[0].isalpha() or not all(c.isalnum() or c == "_" for c in v)):
            problems.append(f"{label} must start with a letter and contain only letters, digits or _.")
    if "'" in values["DB_PASSWORD"] or '"' in values["DB_PASSWORD"]:
        problems.append("Database password cannot contain quote characters.")
    data_dir = values["PG_DATA"].strip()
    if data_dir:
        if not os.path.isabs(data_dir):
            problems.append("PostgreSQL data directory must be an absolute path.")
        elif not os.path.exists(PSQL) and os.path.isdir(data_dir) and os.listdir(data_dir):
            # Only when THIS run will create the cluster does the directory have to be
            # empty. When PostgreSQL is already installed, install_postgresql() skips
            # and the field just tells the later steps where the EXISTING cluster's
            # pg_hba.conf / postgresql.conf live -- which is necessarily non-empty.
            problems.append("PostgreSQL data directory must be empty or not yet exist "
                            "(a new cluster will be created there).")
    return problems


def run_gui():
    """Show the parameter window, then the live install log. Returns the process
    exit code. Falls back to the console installer when tkinter is unavailable."""
    try:
        import queue
        import threading
        import tkinter as tk
        from tkinter import filedialog
        from tkinter import font as tkfont
    except Exception as e:                      # no tkinter -> behave exactly as before
        log.warning(f"Setup window unavailable ({e}); continuing on the console.")
        main()
        return 0

    T = _THEME
    root = tk.Tk()
    root.title(f"DBDOME Setup — v{VERSION}")
    root.configure(bg=T["canvas"])
    root.minsize(720, 600)

    ico = _asset("dbdome.ico")
    if ico:
        try:
            root.iconbitmap(ico)
        except Exception:
            pass

    families = set(tkfont.families())
    ui_family = next((f for f in ("Inter", "Segoe UI", "Helvetica", "Arial") if f in families), "TkDefaultFont")
    mono_family = next((f for f in ("Consolas", "Cascadia Mono", "Courier New") if f in families), "TkFixedFont")
    f_body  = tkfont.Font(family=ui_family, size=10)
    f_label = tkfont.Font(family=ui_family, size=9)
    f_head  = tkfont.Font(family=ui_family, size=15, weight="bold")
    f_brand = tkfont.Font(family=ui_family, size=13)
    f_log   = tkfont.Font(family=mono_family, size=9)

    # ── header band (52px, --bg-primary, bottom border) ──────────────────────
    header = tk.Frame(root, bg=T["primary"], height=52)
    header.pack(fill="x", side="top")
    header.pack_propagate(False)
    tk.Frame(root, bg=T["border"], height=1).pack(fill="x", side="top")

    logo_img = None
    logo_path = _asset("LOGO.png")
    if logo_path:
        try:
            img = tk.PhotoImage(file=logo_path)
            factor = max(1, round(img.height() / 32))     # PhotoImage only does integer downscale
            logo_img = img.subsample(factor, factor)
            tk.Label(header, image=logo_img, bg=T["primary"]).pack(side="left", padx=(16, 8))
        except Exception:
            logo_img = None
    tk.Label(header, text="dbdome", bg=T["primary"], fg=T["emphasis"], font=f_brand).pack(side="left",
             padx=(0 if logo_img else 16, 0))
    tk.Label(header, text=f"setup {VERSION}", bg=T["primary"], fg=T["muted"], font=f_label).pack(side="right", padx=16)

    body = tk.Frame(root, bg=T["canvas"])
    body.pack(fill="both", expand=True)

    state = {"code": 0}

    # ── panel helpers, matching .custom-form ─────────────────────────────────
    def panel(parent):
        outer = tk.Frame(parent, bg=T["border"])                 # 1px border
        inner = tk.Frame(outer, bg=T["primary"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return outer, inner

    def entry(parent, textvar, show=None):
        wrap = tk.Frame(parent, bg=T["border"])
        e = tk.Entry(wrap, textvariable=textvar, show=show, bg=T["secondary"], fg=T["text"],
                     insertbackground=T["text"], relief="flat", font=f_body,
                     disabledbackground=T["secondary"], highlightthickness=0, bd=0)
        e.pack(fill="x", padx=1, pady=1, ipady=5, ipadx=6)
        e.bind("<FocusIn>",  lambda _e: wrap.configure(bg=T["blue_lit"]))
        e.bind("<FocusOut>", lambda _e: wrap.configure(bg=T["border"]))
        return wrap, e

    def button(parent, text, command, primary=False):
        bg = T["blue"] if primary else T["secondary"]
        fg = "#ffffff" if primary else T["text"]
        b = tk.Button(parent, text=text, command=command, bg=bg, fg=fg, font=f_body,
                      relief="flat", bd=0, padx=18, pady=7, cursor="hand2",
                      activebackground=T["blue_lit"] if primary else T["hover"],
                      activeforeground="#ffffff", highlightthickness=0,
                      disabledforeground=T["muted"])
        return b

    # ── view 1: parameters ───────────────────────────────────────────────────
    form_outer, form = panel(body)
    form_outer.pack(fill="both", expand=True, padx=24, pady=24)

    tk.Label(form, text="Installation parameters", bg=T["primary"], fg=T["emphasis"],
             font=f_head, anchor="w").pack(fill="x", padx=24, pady=(20, 2))
    tk.Label(form, text="Confirm the database settings before the installation starts.",
             bg=T["primary"], fg=T["muted"], font=f_label, anchor="w").pack(fill="x", padx=24, pady=(0, 14))

    fields = tk.Frame(form, bg=T["primary"])
    fields.pack(fill="both", expand=True, padx=24)
    fields.columnconfigure(1, weight=1)

    vars_ = {}
    row = 0
    for label, key, hint, is_pw in _PARAM_FIELDS:
        vars_[key] = tk.StringVar(value=globals()[key])
        tk.Label(fields, text=label, bg=T["primary"], fg=T["text"], font=f_label,
                 anchor="w").grid(row=row, column=0, sticky="w", pady=(0, 2))
        wrap, ent = entry(fields, vars_[key], show="\u2022" if is_pw else None)
        if is_pw:
            # the password row trades its hint for the reveal toggle (same cell)
            show_var = tk.BooleanVar(value=False)
            tk.Checkbutton(fields, text="show", variable=show_var, bg=T["primary"], fg=T["muted"],
                           selectcolor=T["secondary"], activebackground=T["primary"],
                           activeforeground=T["text"], font=f_label, bd=0, highlightthickness=0,
                           command=lambda e=ent, v=show_var: e.configure(show="" if v.get() else "\u2022")
                           ).grid(row=row, column=1, sticky="e", pady=(0, 2))
        else:
            tk.Label(fields, text=hint, bg=T["primary"], fg=T["muted"], font=f_label,
                     anchor="e").grid(row=row, column=1, sticky="e", pady=(0, 2))
        row += 1
        wrap.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        row += 1

    # data directory + Browse
    vars_["PG_DATA"] = tk.StringVar(value=PG_DATA)
    tk.Label(fields, text="PostgreSQL data directory", bg=T["primary"], fg=T["text"],
             font=f_label, anchor="w").grid(row=row, column=0, sticky="w", pady=(0, 2))
    tk.Label(fields, text="cluster location (must be empty)", bg=T["primary"], fg=T["muted"],
             font=f_label, anchor="e").grid(row=row, column=1, sticky="e", pady=(0, 2))
    row += 1
    dd_row = tk.Frame(fields, bg=T["primary"])
    dd_row.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 4))
    dd_row.columnconfigure(0, weight=1)
    dd_wrap, _dd_entry = entry(dd_row, vars_["PG_DATA"])
    dd_wrap.grid(row=0, column=0, sticky="ew")

    def browse():
        chosen = filedialog.askdirectory(title="PostgreSQL data directory",
                                         initialdir=os.path.dirname(vars_["PG_DATA"].get() or PG_DATA))
        if chosen:
            vars_["PG_DATA"].set(os.path.normpath(chosen))
    button(dd_row, "Browse…", browse).grid(row=0, column=1, padx=(8, 0))
    row += 1

    problems = tk.Label(form, text="", bg=T["primary"], fg=T["red"], font=f_label,
                        anchor="w", justify="left", wraplength=620)
    problems.pack(fill="x", padx=24, pady=(6, 0))

    actions = tk.Frame(form, bg=T["primary"])
    actions.pack(fill="x", padx=24, pady=(12, 22))

    def cancel():
        state["code"] = 1
        root.destroy()

    def start():
        values = {k: v.get() for k, v in vars_.items()}
        found = _validate_params(values)
        if found:
            problems.configure(text="\n".join(found))
            return
        _apply_params(values)
        show_log_view()

    button(actions, "Cancel", cancel).pack(side="right")
    start_btn = button(actions, "Start installation", start, primary=True)
    start_btn.pack(side="right", padx=(0, 8))
    root.bind("<Return>", lambda _e: start())

    # ── view 2: install log ──────────────────────────────────────────────────
    def show_log_view():
        form_outer.destroy()
        root.unbind("<Return>")
        root.geometry("980x680")

        log_outer, log_panel = panel(body)
        log_outer.pack(fill="both", expand=True, padx=24, pady=24)

        head = tk.Frame(log_panel, bg=T["primary"])
        head.pack(fill="x", padx=20, pady=(16, 8))
        tk.Label(head, text="Installing DBDOME", bg=T["primary"], fg=T["emphasis"],
                 font=f_head).pack(side="left")
        status = tk.Label(head, text="running…", bg=T["primary"], fg=T["blue_lit"], font=f_label)
        status.pack(side="right")

        text_wrap = tk.Frame(log_panel, bg=T["border"])
        text_wrap.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        text = tk.Text(text_wrap, bg=T["canvas"], fg=T["text"], font=f_log, relief="flat",
                       bd=0, wrap="none", insertbackground=T["text"], highlightthickness=0)
        scroll = tk.Scrollbar(text_wrap, command=text.yview, bg=T["secondary"],
                              troughcolor=T["canvas"], bd=0, highlightthickness=0)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y", padx=(0, 1), pady=1)
        text.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        text.tag_configure("WARNING", foreground=T["yellow"] if "yellow" in T else "#ecbb13")
        text.tag_configure("ERROR", foreground=T["red"])
        text.tag_configure("STEP", foreground=T["blue_lit"])
        text.configure(state="disabled")

        foot = tk.Frame(log_panel, bg=T["primary"])
        foot.pack(fill="x", padx=20, pady=(0, 18))
        close_btn = button(foot, "Close", lambda: root.destroy(), primary=True)
        close_btn.configure(state="disabled")
        close_btn.pack(side="right")

        # The install thread never touches Tk: it pushes formatted lines onto a
        # queue that the UI drains on a timer.
        q = queue.Queue()

        class _QueueHandler(logging.Handler):
            def emit(self, record):
                try:
                    q.put((record.levelname, self.format(record)))
                except Exception:
                    pass

        qh = _QueueHandler()
        qh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(qh)

        def worker():
            try:
                main()
                q.put(("__DONE__", "0"))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                q.put(("__DONE__", str(code)))
            except Exception as e:
                log.error(f"Installation failed: {e}", exc_info=True)
                q.put(("__DONE__", "1"))

        threading.Thread(target=worker, daemon=True).start()

        def pump():
            done = None
            while True:
                try:
                    level, line = q.get_nowait()
                except queue.Empty:
                    break
                if level == "__DONE__":
                    done = int(line)
                    continue
                tag = level if level in ("WARNING", "ERROR") else (
                    "STEP" if "  STEP " in line or "═" in line else "")
                text.configure(state="normal")
                text.insert("end", line + "\n", tag)
                text.see("end")
                text.configure(state="disabled")
            if done is None:
                root.after(120, pump)
                return
            log.removeHandler(qh)
            state["code"] = done
            ok = done == 0
            status.configure(text="completed" if ok else "finished with errors",
                             fg=T["green"] if ok else T["red"])
            close_btn.configure(state="normal")

        root.after(120, pump)
        # Closing mid-install would orphan the steps: ignore the X until it's done.
        root.protocol("WM_DELETE_WINDOW", lambda: None if close_btn["state"] == "disabled" else root.destroy())

    # centre on screen
    root.update_idletasks()
    w, h = max(root.winfo_width(), 760), max(root.winfo_height(), 620)
    x = (root.winfo_screenwidth() - w) // 2
    y = max(0, (root.winfo_screenheight() - h) // 3)
    root.geometry(f"{w}x{h}+{x}+{y}")

    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()
    return state["code"]


def main():
    global LOG_FILE
    start_time = time.time()

    preflight()

    # Step 1: Find source and copy
    src = find_source()
    copy_files(src)

    # Move log file to final location now that DEST_DIR exists
    final_log = os.path.join(DEST_DIR, "install.log")
    if LOG_FILE and LOG_FILE != final_log:
        # Replace the temp file handler with the final one
        for h in log.handlers[:]:
            if isinstance(h, logging.FileHandler):
                h.close()
                log.removeHandler(h)
        try:
            # Copy temp log content to final location
            if os.path.exists(LOG_FILE):
                shutil.copy2(LOG_FILE, final_log)
            fh = logging.FileHandler(final_log, mode="a", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            log.addHandler(fh)
            LOG_FILE = final_log
        except Exception:
            pass

    # Step 2: ODBC driver
    install_odbc_driver()

    # Step 3: PostgreSQL
    install_postgresql()

    # Step 4: pg_hba.conf
    configure_pg_hba()

    # Step 5: Database & users
    create_database()

    # Step 6: Restore backup
    restore_database()

    # Step 7: Grafana config
    try:
        configure_grafana()
    except Exception as e:
        log.warning(f"  Grafana configuration error (non-fatal): {e}")

    # Step 8: .env files
    create_env_files()

    # Step 8b: rootcause decryption key as role default (must be after .env is
    # written and before services/Grafana start, so their sessions pick it up).
    try:
        set_rootcause_key_defaults()
    except Exception as e:
        log.warning(f"  set_rootcause_key_defaults error (non-fatal): {e}")

    # Step 9: Firewall
    configure_firewall()

    # Step 10: Install DBDOME Windows service
    install_dbdome_service()

    # Step 11: Start services
    start_services()

    # Step 12: Validate
    time.sleep(5)  # give services a moment to start
    all_ok = validate()

    # Step 13: Shortcut
    create_shortcut()

    # Step 14: Install + start Grafana as a Windows service (last — after
    # validation, after shortcut)
    install_grafana_service()

    # Step 15: Restart Grafana once more — its first start often logs plugin
    # errors that a clean restart clears.
    time.sleep(3)
    restart_grafana_service()

    # Step 16: Trust the DBDOME code-signing certificate
    trust_signing_cert()

    # Summary
    elapsed = int(time.time() - start_time)
    log.info("")
    log.info("╔══════════════════════════════════════════════════════════╗")
    if all_ok:
        log.info("║       DBDOME Installation Completed Successfully       ║")
    else:
        log.info("║    DBDOME Installation Completed (with warnings)       ║")
    log.info("╠══════════════════════════════════════════════════════════╣")
    log.info(f"║  Version:     {VERSION.ljust(40)}║")
    log.info(f"║  Install dir: {DEST_DIR.ljust(40)}║")
    log.info(f"║  PostgreSQL:  localhost:{PG_PORT.ljust(34)}║")
    log.info(f"║  Web UI:      https://localhost:{HTTP_PORT}/dbdome".ljust(57) + "║")
    log.info(f"║  Grafana:     http://localhost:{GRAFANA_PORT}".ljust(57) + "║")
    log.info(f"║  Local IP:    {get_local_ip().ljust(40)}║")
    log.info(f"║  Duration:    {elapsed} seconds".ljust(57) + "║")
    log.info(f"║  Log file:    {LOG_FILE.ljust(40)}║")
    log.info("╚══════════════════════════════════════════════════════════╝")


if __name__ == "__main__":
    # Default: open the setup window (parameters -> live log). --no-gui / --silent
    # keeps the original console-only behaviour for unattended runs.
    _console_only = any(a in ("--no-gui", "--silent", "--console") for a in sys.argv[1:])
    try:
        if _console_only:
            main()
        else:
            sys.exit(run_gui())
    except KeyboardInterrupt:
        log.info("\nInstallation cancelled by user.")
        sys.exit(1)
    except Exception as e:
        log.error(f"\nInstallation failed: {e}", exc_info=True)
        sys.exit(1)
