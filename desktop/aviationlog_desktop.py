"""AviationLog desktop launcher.

Runs the web app on this computer without Docker:

1. starts a private PostgreSQL server (bundled with the app) in the user's data folder,
2. starts AviationLog on http://127.0.0.1:<port>,
3. shows it in an app window (pywebview: WebView2 / WKWebView / WebKitGTK); closing the window
   stops everything. With ``--browser``, ``"window": "browser"`` in config.json or when the system
   has no web view: a small control window plus the normal browser instead.

Data folder (database, backups, thumbnails, uploaded photos, config.json, logs):

- Windows: %LOCALAPPDATA%\\AviationLog
- macOS:   ~/Library/Application Support/AviationLog
- Linux:   $XDG_DATA_HOME/aviationlog (~/.local/share/aviationlog)

``AVIATIONLOG_HOME`` overrides the data folder, ``AVIATIONLOG_PG_DIR`` the PostgreSQL
installation (folder containing bin/, lib/, share/). ``--headless`` runs without a window,
``--self-test`` opens the window, checks the login page and exits (build pipeline).
"""
from __future__ import annotations

import json
import locale
import logging
import logging.handlers
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "AviationLog"
DEFAULT_PORT = 18090
DB_USER = "aviationlog"
DB_NAME = "aviationlog"
FROZEN = getattr(sys, "frozen", False)
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

logger = logging.getLogger("desktop")
_fault_log = None


# ---- Paths -------------------------------------------------------------------------

def data_home() -> Path:
    if os.environ.get("AVIATIONLOG_HOME"):
        return Path(os.environ["AVIATIONLOG_HOME"]).expanduser()
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "aviationlog"


def bundle_dir() -> Path:
    """Folder with the bundled resources (PyInstaller) or the repository root."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def app_dir() -> Path:
    """Folder with main.py, templates/, static/, i18n/."""
    return bundle_dir() if FROZEN else bundle_dir() / "app"


def pg_dir() -> Path:
    """PostgreSQL installation (bin/, lib/, share/).

    The desktop builds ship the "pgserver" package files unchanged in <app>/pgsql, because
    its programs find their shared libraries by relative paths (pgserver.libs, .dylibs).
    """
    if os.environ.get("AVIATIONLOG_PG_DIR"):
        return Path(os.environ["AVIATIONLOG_PG_DIR"])
    if FROZEN:
        exe_dir = Path(sys.executable).resolve().parent
        for base in (exe_dir / "pgsql", exe_dir.parent / "Resources" / "pgsql", bundle_dir() / "pgsql"):
            if (base / "pgserver" / "pginstall" / "bin").is_dir():
                return base / "pgserver" / "pginstall"
        raise RuntimeError(f"Bundled PostgreSQL not found next to {exe_dir}")
    import importlib.util

    spec = importlib.util.find_spec("pgserver")
    if spec and spec.submodule_search_locations:
        return Path(list(spec.submodule_search_locations)[0]) / "pginstall"
    raise RuntimeError("PostgreSQL not found: install the 'pgserver' package or set AVIATIONLOG_PG_DIR")


def pg_environment(env: dict) -> dict:
    """Environment for the PostgreSQL programs (initdb, pg_ctl, pg_dump, psql)."""
    env = dict(env)
    env.pop("PGDATA", None)
    if sys.platform.startswith("linux"):
        # PyInstaller points LD_LIBRARY_PATH at its own libraries; the PostgreSQL programs
        # must use the system ones and their own (found via RPATH).
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def pg_bin(name: str) -> str:
    exe = pg_dir() / "bin" / (name + (".exe" if IS_WINDOWS else ""))
    if not exe.exists():
        raise RuntimeError(f"PostgreSQL program missing: {exe}")
    return str(exe)


# ---- Config ------------------------------------------------------------------------

def system_language() -> str:
    for value in (os.environ.get("LC_ALL"), os.environ.get("LANG"), locale.getlocale()[0]):
        if value and value.lower().startswith(("de", "german")):
            return "de"
    if IS_MAC:
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True,
                                 text=True, timeout=5).stdout
            first = out.replace("(", "").strip().split(",")[0].strip().strip('"')
            if first.lower().startswith("de"):
                return "de"
        except Exception:
            pass
    return "en"


def load_config(home: Path) -> dict:
    path = home / "config.json"
    cfg = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    defaults = {
        "port": DEFAULT_PORT,
        "language": system_language(),
        "secret_key": secrets.token_hex(32),
        "db_password": secrets.token_urlsafe(24),
        "admin_username": "admin",
        "admin_password": secrets.token_urlsafe(9),
        "photo_mode": "upload",
        "photo_dir": "",
        "open_browser": True,
        "smtp": {},
    }
    changed = not path.exists()
    for key, value in defaults.items():
        if key not in cfg:
            cfg[key] = value
            changed = True
    if changed:
        save_config(home, cfg)
    return cfg


def save_config(home: Path, cfg: dict):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    if not IS_WINDOWS:
        os.chmod(path, 0o600)


# ---- Ports -------------------------------------------------------------------------

def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if not IS_WINDOWS:
            # like uvicorn: a port still in TIME_WAIT from the last start counts as free
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def running_aviationlog(port: int) -> bool:
    """True if an AviationLog is already answering on this port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
            return b'"version"' in r.read()
    except Exception:
        return False


# ---- PostgreSQL --------------------------------------------------------------------

class Postgres:
    """A private PostgreSQL cluster in <data>/postgres, reachable only on 127.0.0.1."""

    def __init__(self, home: Path, password: str):
        self.home = home
        self.data = home / "postgres"
        self.log = home / "logs" / "postgres.log"
        self.password = password
        self.port = None

    def _run(self, args: list[str], timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
        env = pg_environment(os.environ)
        logger.info("Running %s", " ".join(Path(a).name if i == 0 else a for i, a in enumerate(args)))
        # Temporary files instead of pipes: "pg_ctl start" leaves the server running, and on
        # Windows it would keep inherited pipe handles open, so reading them would never end.
        with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as out, \
                tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
            proc = subprocess.run(args, stdout=out, stderr=err, timeout=timeout, env=env,
                                  stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
            out.seek(0)
            err.seek(0)
            result = subprocess.CompletedProcess(args, proc.returncode, out.read(), err.read())
        if result.stdout.strip():
            logger.info(result.stdout.strip())
        if result.stderr.strip():
            logger.info(result.stderr.strip())
        if check and result.returncode != 0:
            raise RuntimeError(f"{Path(args[0]).name} failed ({result.returncode}): "
                               f"{(result.stderr or result.stdout).strip()[-800:]}")
        return result

    def initialized(self) -> bool:
        return (self.data / "PG_VERSION").exists()

    def init(self):
        if self.initialized():
            return
        self.home.mkdir(parents=True, exist_ok=True)
        pwfile = self.home / ".pgpass-init"
        pwfile.write_text(self.password + "\n", encoding="utf-8")
        # No ICU in the bundled PostgreSQL: use a UTF-8 libc locale so that upper/lower
        # (case-insensitive search) also work for umlauts; "C" is the last resort.
        if IS_WINDOWS:
            locales = ["en-US", "C"]
        elif IS_MAC:
            locales = ["en_US.UTF-8", "C"]
        else:
            locales = ["C.UTF-8", "en_US.UTF-8", "C"]
        try:
            last_error = None
            for loc in locales:
                if self.data.exists():
                    shutil.rmtree(self.data, ignore_errors=True)
                try:
                    self._run([pg_bin("initdb"), "-D", str(self.data), "-U", DB_USER, "--pwfile", str(pwfile),
                               "--auth=scram-sha-256", "--encoding=UTF8", f"--locale={loc}"], timeout=300)
                    logger.info("Database cluster created (locale %s)", loc)
                    return
                except RuntimeError as exc:
                    last_error = exc
                    logger.warning("initdb with locale %s failed, trying the next one", loc)
            raise last_error
        finally:
            pwfile.unlink(missing_ok=True)

    def _ctl(self, *args: str, timeout: int = 120, check: bool = True):
        return self._run([pg_bin("pg_ctl"), "-D", str(self.data), *args], timeout=timeout, check=check)

    def running(self) -> bool:
        return self._ctl("status", check=False).returncode == 0

    def start(self):
        self.log.parent.mkdir(parents=True, exist_ok=True)
        if self.running():
            # left over from a crash of the launcher; the port may have changed since
            logger.info("PostgreSQL is still running from a previous start, restarting it")
            self.stop()
        self.port = free_port()
        opts = f"-p {self.port} -c listen_addresses=127.0.0.1"
        if not IS_WINDOWS:
            opts += " -c unix_socket_directories="
        self._ctl("-l", str(self.log), "-w", "-t", "120", "-o", opts, "start", timeout=180)
        self._ensure_database()

    def _ensure_database(self):
        import psycopg2

        deadline = time.time() + 60
        while True:
            try:
                conn = psycopg2.connect(host="127.0.0.1", port=self.port, user=DB_USER, password=self.password,
                                        dbname="postgres", connect_timeout=10)
                break
            except psycopg2.OperationalError:
                # on Windows "pg_ctl -w start" may return slightly before connections are accepted
                if time.time() > deadline:
                    raise
                time.sleep(1)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
                if not cur.fetchone():
                    cur.execute(f'CREATE DATABASE "{DB_NAME}"')
                    logger.info("Database %s created", DB_NAME)
        finally:
            conn.close()

    def stop(self):
        try:
            if self.running():
                self._ctl("-m", "fast", "-w", "-t", "60", "stop", timeout=90, check=False)
        except Exception:
            logger.exception("Stopping PostgreSQL failed")

    @property
    def url(self) -> str:
        from urllib.parse import quote

        return f"postgresql://{DB_USER}:{quote(self.password, safe='')}@127.0.0.1:{self.port}/{DB_NAME}"


# ---- AviationLog server ------------------------------------------------------------

def configure_environment(home: Path, cfg: dict, db_url: str, port: int):
    env = {
        "DATABASE_URL": db_url,
        "SECRET_KEY": cfg["secret_key"],
        "ADMIN_USERNAME": cfg["admin_username"],
        "ADMIN_PASSWORD": cfg["admin_password"],
        "DEFAULT_LANGUAGE": cfg["language"],
        "DATA_DIR": str(home),
        "BACKUP_DIR": str(home / "backups"),
        "SESSION_HTTPS_ONLY": "false",
        "BASE_URL": f"http://127.0.0.1:{port}",
        "AVIATIONLOG_DESKTOP": "1",
    }
    env.update(photo_environment(home, cfg))
    for key, value in (cfg.get("smtp") or {}).items():
        env[f"SMTP_{key.upper()}"] = str(value)
    os.environ.update(env)
    # pg_dump / psql for the backups come from the bundled PostgreSQL
    os.environ["PATH"] = str(pg_dir() / "bin") + os.pathsep + os.environ.get("PATH", "")
    if FROZEN and sys.platform.startswith("linux"):
        # the app starts pg_dump/psql (and xdg-open) itself, so fix the library path for them
        cleaned = pg_environment(os.environ)
        os.environ.pop("LD_LIBRARY_PATH", None)
        if "LD_LIBRARY_PATH" in cleaned:
            os.environ["LD_LIBRARY_PATH"] = cleaned["LD_LIBRARY_PATH"]
    for sub in ("backups", "thumbs", "imports", "photos", "logs"):
        (home / sub).mkdir(parents=True, exist_ok=True)


def photo_environment(home: Path, cfg: dict) -> dict:
    if cfg.get("photo_mode") == "nas" and cfg.get("photo_dir"):
        return {"PHOTO_MODE": "nas", "PHOTO_DIR": cfg["photo_dir"]}
    return {"PHOTO_MODE": "upload", "PHOTO_DIR": str(home / "photos")}


class AppServer:
    def __init__(self, port: int):
        self.port = port
        self.server = None
        self.thread = None
        self.error = None

    def start(self):
        sys.path.insert(0, str(app_dir()))
        import uvicorn

        import main  # noqa: E402  (needs the environment set above)

        config = uvicorn.Config(main.app, host="127.0.0.1", port=self.port, log_config=None,
                                proxy_headers=False, access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._serve, name="uvicorn", daemon=True)
        self.thread.start()
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.error:
                raise self.error
            if self.server.started:
                return
            if not self.thread.is_alive():
                raise RuntimeError("AviationLog stopped during start, see logs/aviationlog.log")
            time.sleep(0.2)
        raise RuntimeError("AviationLog did not start within 2 minutes")

    def _serve(self):
        try:
            self.server.run()
        except BaseException as exc:  # SystemExit from uvicorn on startup errors
            logger.exception("Server stopped with an error")
            self.error = RuntimeError(str(exc) or exc.__class__.__name__)

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=15)


# ---- Control window ----------------------------------------------------------------

TEXTS = {
    "en": {
        "starting": "Starting AviationLog …",
        "running": "AviationLog is running at",
        "open": "Open in browser",
        "folder": "Open data folder",
        "quit": "Quit",
        "login": "First login: user {user}, password {password}\nChange the password under Profile after logging in.",
        "photos": "Photos",
        "photos_upload": "Photos are stored in the data folder (upload with each sighting).",
        "photos_nas": "Photo archive (read only): {dir}",
        "choose_archive": "Use photo archive …",
        "use_upload": "Store photos in AviationLog",
        "choose_title": "Choose your photo archive (it is only read, never changed)",
        "error": "AviationLog could not start",
        "stopping": "Stopping AviationLog …",
        "keep_open": "Closing this window stops AviationLog.",
        "menu": "AviationLog",
        "menu_photos": "Photos",
        "download_failed": "Download failed: {error}",
        "login_expired": "Please log in again, then repeat the download.",
    },
    "de": {
        "starting": "AviationLog wird gestartet …",
        "running": "AviationLog läuft unter",
        "open": "Im Browser öffnen",
        "folder": "Datenordner öffnen",
        "quit": "Beenden",
        "login": "Erste Anmeldung: Benutzer {user}, Passwort {password}\n"
                 "Ändere das Passwort nach dem Anmelden unter Profil.",
        "photos": "Fotos",
        "photos_upload": "Fotos werden im Datenordner gespeichert (Upload bei jeder Sichtung).",
        "photos_nas": "Fotoarchiv (nur lesen): {dir}",
        "choose_archive": "Fotoarchiv verwenden …",
        "use_upload": "Fotos in AviationLog speichern",
        "choose_title": "Fotoarchiv wählen (wird nur gelesen, nie verändert)",
        "error": "AviationLog konnte nicht starten",
        "stopping": "AviationLog wird beendet …",
        "keep_open": "Wenn du dieses Fenster schließt, wird AviationLog beendet.",
        "menu": "AviationLog",
        "menu_photos": "Fotos",
        "download_failed": "Download fehlgeschlagen: {error}",
        "login_expired": "Bitte melde dich neu an und starte den Download dann noch einmal.",
    },
}


def open_path(path: Path):
    if IS_WINDOWS:
        os.startfile(str(path))  # noqa: S606
    elif IS_MAC:
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Launcher:
    def __init__(self):
        self.home = data_home()
        self.home.mkdir(parents=True, exist_ok=True)
        setup_logging(self.home)
        self.cfg = load_config(self.home)
        self.t = TEXTS.get(self.cfg["language"], TEXTS["en"])
        self.pg = Postgres(self.home, self.cfg["db_password"])
        self.server = None
        self.url = None
        self.stopped = threading.Event()
        self.ready = threading.Event()  # set when the start finished, successfully or not
        self.boot_error = None
        self._boot_thread = None

    # -- lifecycle --

    def boot(self):
        """Database and web server; runs in a background thread when there is a window."""
        port = int(self.cfg.get("port") or DEFAULT_PORT)
        if not port_free(port):
            port = free_port()
            logger.info("Port %s is busy, using %s for this start", self.cfg.get("port"), port)
        self.pg.init()
        self.pg.start()
        configure_environment(self.home, self.cfg, self.pg.url, port)
        self.server = AppServer(port)
        self.server.start()
        self.url = f"http://127.0.0.1:{port}"
        logger.info("AviationLog is running at %s", self.url)

    def start_boot(self):
        """Starts database and server in the background (the window shows meanwhile)."""
        if self._boot_thread:
            return

        def run():
            try:
                self.boot()
            except Exception as exc:
                logger.exception("Start failed")
                self.boot_error = exc
            finally:
                self.ready.set()

        self._boot_thread = threading.Thread(target=run, name="boot", daemon=True)
        self._boot_thread.start()

    def shutdown(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        logger.info("Stopping")
        if self.server:
            try:
                self.server.stop()  # the app's lifespan also stops the backup scheduler
            except Exception:
                logger.exception("Stopping the server failed")
        try:
            from database import engine

            engine.dispose()
        except Exception:
            pass
        self.pg.stop()

    # -- headless --

    def run_headless(self):
        try:
            self.boot()
            print(f"AviationLog is running at {self.url} (Ctrl+C to stop)")
            print(f"First login: {self.cfg['admin_username']} / {self.cfg['admin_password']}")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    # -- app window --

    def run_app(self, self_test: bool = False):
        """AviationLog in its own window (system web view), no browser needed."""
        import webview
        from webview.menu import Menu, MenuAction

        t = self.t
        self.self_test_ok = False
        self.start_boot()
        loading = LOADING_HTML.replace("__BODY__", f'<div class="spin"></div><div>{t["starting"]}</div>')
        self.window = webview.create_window(
            APP_NAME, html=loading, js_api=DesktopApi(self), width=1280, height=860, min_size=(760, 520),
            text_select=True, zoomable=True)

        def choose_archive():
            result = self.window.create_file_dialog(webview.FileDialog.FOLDER, directory=self.cfg.get("photo_dir") or "")
            if result:
                self.cfg["photo_mode"] = "nas"
                self.cfg["photo_dir"] = result if isinstance(result, str) else result[0]
                self._apply_photo_mode()

        def use_upload():
            self.cfg["photo_mode"] = "upload"
            self._apply_photo_mode()

        menu = [
            Menu(t["menu"], [
                MenuAction(t["open"], lambda: self.url and webbrowser.open(self.url)),
                MenuAction(t["folder"], lambda: open_path(self.home)),
            ]),
            Menu(t["menu_photos"], [
                MenuAction(t["choose_archive"], choose_archive),
                MenuAction(t["use_upload"], use_upload),
            ]),
        ]

        def after_start():
            try:
                show_app()
            except Exception:
                logger.exception("Loading the app window failed")
                if self_test:
                    self.window.destroy()

        def show_app():
            self.ready.wait()
            if self.boot_error:
                body = f'<h2>{t["error"]}</h2><pre>{self.boot_error}\n\n{self.home / "logs"}</pre>'
                self.window.load_html(LOADING_HTML.replace("__BODY__", body))
                if self_test:
                    self.window.destroy()
                return
            self.window.load_url(self.url)
            if self_test:
                self._self_test()

        # window icon: only GTK takes a PNG (Windows uses the .exe icon, macOS the app bundle's)
        icon = app_dir() / "static" / "icons" / "icon-512.png"
        use_icon = sys.platform.startswith("linux") and icon.exists()
        if sys.platform.startswith("linux"):
            # WebKitGTK's DMA-BUF renderer leaves the window blank with some graphics drivers
            # and in virtual machines; the fallback renderer works everywhere
            os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
        webview.start(after_start, menu=menu, private_mode=False, storage_path=str(self.home / "webview"),
                      icon=str(icon) if use_icon else None)
        self.shutdown()

    def _self_test(self):
        """--self-test: the window shows the login page, then closes (used by the build pipeline)."""
        try:
            if not self.window.events.loaded.wait(120):
                logger.error("SELF-TEST: page did not load")
                return
            for _ in range(30):
                found = self.window.evaluate_js(
                    "document.querySelector('form[action=\"/login\"]') ? 'login' : String(document.title)")
                if found == "login":
                    break
                time.sleep(1)
            api = self.window.evaluate_js("typeof window.pywebview === 'object' ? 'api' : 'none'")
            logger.info("SELF-TEST: page=%s bridge=%s", found, api)
            self.self_test_ok = found == "login" and api == "api"
        except Exception:
            logger.exception("SELF-TEST failed")
        finally:
            self.window.destroy()

    # -- window --

    def run_window(self):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk, self.messagebox = tk, messagebox
        root = tk.Tk()
        self.root = root
        root.title(APP_NAME)
        root.resizable(False, False)
        self._set_icon(root)
        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")

        ttk.Label(frame, text=APP_NAME, font=("TkDefaultFont", 16, "bold")).grid(row=0, column=0, columnspan=3,
                                                                                 sticky="w")
        self.status = tk.StringVar(value=self.t["starting"])
        ttk.Label(frame, textvariable=self.status, wraplength=440).grid(row=1, column=0, columnspan=3,
                                                                        sticky="w", pady=(8, 0))
        self.link = ttk.Label(frame, text="", foreground="#1a6fd1", cursor="hand2")
        self.link.grid(row=2, column=0, columnspan=3, sticky="w")
        self.link.bind("<Button-1>", lambda e: self.url and webbrowser.open(self.url))

        login = self.t["login"].format(user=self.cfg["admin_username"], password=self.cfg["admin_password"])
        login_label = ttk.Entry(frame, width=60)
        login_label.insert(0, login.split("\n")[0])
        login_label.configure(state="readonly")
        login_label.grid(row=3, column=0, columnspan=3, sticky="we", pady=(12, 0))
        ttk.Label(frame, text=login.split("\n")[1], wraplength=440, foreground="#666").grid(
            row=4, column=0, columnspan=3, sticky="w")

        ttk.Separator(frame).grid(row=5, column=0, columnspan=3, sticky="we", pady=12)
        ttk.Label(frame, text=self.t["photos"], font=("TkDefaultFont", 11, "bold")).grid(row=6, column=0,
                                                                                       sticky="w")
        self.photo_text = tk.StringVar()
        ttk.Label(frame, textvariable=self.photo_text, wraplength=440).grid(row=7, column=0, columnspan=3,
                                                                           sticky="w")

        def choose_archive():
            path = filedialog.askdirectory(title=self.t["choose_title"], mustexist=True)
            if path:
                self.cfg["photo_mode"], self.cfg["photo_dir"] = "nas", path
                self._apply_photo_mode()

        def use_upload():
            self.cfg["photo_mode"] = "upload"
            self._apply_photo_mode()

        ttk.Button(frame, text=self.t["choose_archive"], command=choose_archive).grid(row=8, column=0, sticky="w",
                                                                                      pady=(6, 0))
        self.upload_btn = ttk.Button(frame, text=self.t["use_upload"], command=use_upload)
        self.upload_btn.grid(row=8, column=1, sticky="w", pady=(6, 0), padx=(6, 0))
        self._show_photo_mode()

        ttk.Separator(frame).grid(row=9, column=0, columnspan=3, sticky="we", pady=12)
        self.open_btn = ttk.Button(frame, text=self.t["open"], state="disabled",
                                   command=lambda: webbrowser.open(self.url))
        self.open_btn.grid(row=10, column=0, sticky="w")
        ttk.Button(frame, text=self.t["folder"], command=lambda: open_path(self.home)).grid(
            row=10, column=1, sticky="w", padx=(6, 0))
        ttk.Button(frame, text=self.t["quit"], command=self.quit_window).grid(row=10, column=2, sticky="e",
                                                                              padx=(6, 0))
        ttk.Label(frame, text=self.t["keep_open"], foreground="#666").grid(row=11, column=0, columnspan=3,
                                                                          sticky="w", pady=(10, 0))
        root.protocol("WM_DELETE_WINDOW", self.quit_window)

        self.start_boot()
        self._wait_ready()
        self._tick()
        root.mainloop()

    def _tick(self):
        # Python runs signal handlers (SIGTERM on logout) only between Tk events
        self.root.after(500, self._tick)

    def _set_icon(self, root):
        icon = app_dir() / "static" / "icons" / "icon-192.png"
        try:
            self._icon = self.tk.PhotoImage(file=str(icon))
            root.iconphoto(True, self._icon)
        except Exception:
            pass

    def _show_photo_mode(self):
        if self.cfg.get("photo_mode") == "nas" and self.cfg.get("photo_dir"):
            self.photo_text.set(self.t["photos_nas"].format(dir=self.cfg["photo_dir"]))
            self.upload_btn.state(["!disabled"])
        else:
            self.photo_text.set(self.t["photos_upload"])
            self.upload_btn.state(["disabled"])

    def _apply_photo_mode(self):
        save_config(self.home, self.cfg)
        # read by the app on every request, so no restart is needed
        os.environ.update(photo_environment(self.home, self.cfg))
        logger.info("Photo mode: %s", photo_environment(self.home, self.cfg))
        if getattr(self, "window", None) is not None:
            self.window.evaluate_js("location.reload()")
        else:
            self._show_photo_mode()

    def _wait_ready(self):
        if not self.ready.is_set():
            self.root.after(200, self._wait_ready)
        elif self.boot_error:
            self._failed(self.boot_error)
        else:
            self._booted()
            if self.cfg.get("open_browser", True):
                webbrowser.open(self.url)

    def _booted(self):
        self.status.set(self.t["running"])
        self.link.configure(text=self.url)
        self.open_btn.state(["!disabled"])

    def _failed(self, exc):
        self.status.set(f"{self.t['error']}: {exc}")
        self.messagebox.showerror(APP_NAME, f"{self.t['error']}:\n\n{exc}\n\n{self.home / 'logs'}")

    def quit_window(self):
        self.status.set(self.t["stopping"])
        self.root.update()
        self.shutdown()
        self.root.destroy()




# ---- App window (pywebview) ---------------------------------------------------------

LOADING_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
font-family:-apple-system,"Segoe UI",Roboto,sans-serif;background:#f5f7fa;color:#334}
@media (prefers-color-scheme:dark){body{background:#15181d;color:#dde}}
.spin{width:36px;height:36px;border:4px solid #8aa4c8;border-top-color:transparent;border-radius:50%;
animation:r 1s linear infinite;margin-bottom:18px}@keyframes r{to{transform:rotate(360deg)}}
pre{white-space:pre-wrap;max-width:80%;font-size:13px;opacity:.8}</style></head>
<body>__BODY__</body></html>"""


def filename_from_disposition(header: str | None) -> str | None:
    """File name from a Content-Disposition header (RFC 6266, incl. filename*=UTF-8'')."""
    if not header:
        return None
    from urllib.parse import unquote

    name = None
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "filename*" and "''" in value:
            return os.path.basename(unquote(value.split("''", 1)[1]))
        if key == "filename":
            name = value.strip('"')
    return os.path.basename(name) if name else None


def downloads_dir() -> str:
    for candidate in (Path.home() / "Downloads", Path.home()):
        if candidate.is_dir():
            return str(candidate)
    return ""


class DesktopApi:
    """Functions the pages call through window.pywebview.api (see static/desktop.js).

    Only public methods are exposed; the launcher reference is private on purpose.
    """

    def __init__(self, launcher: "Launcher"):
        self._launcher = launcher

    def _url(self, path: str) -> str:
        from urllib.parse import urljoin, urlparse

        full = urljoin(self._launcher.url + "/", path)
        if urlparse(full).netloc != urlparse(self._launcher.url).netloc:
            raise ValueError("only AviationLog addresses")
        return full

    def download(self, path: str, suggested: str = ""):
        """Saves a file of the app (CSV export, backup, original photo) with a save dialog."""
        import webview

        launcher = self._launcher
        t = launcher.t
        window = launcher.window
        tmp = None
        try:
            url = self._url(path)
            cookies = "; ".join(
                f"{morsel.key}={morsel.value}"
                for jar in (window.get_cookies() or []) for morsel in jar.values()
            )
            request = urllib.request.Request(url, headers={"Cookie": cookies})
            tmp_dir = launcher.home / "tmp"
            tmp_dir.mkdir(exist_ok=True)
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.geturl().rstrip("/").endswith("/login"):
                    return {"error": t["login_expired"]}
                name = (filename_from_disposition(response.headers.get("Content-Disposition"))
                        or suggested or os.path.basename(url.split("?")[0]) or "download")
                with tempfile.NamedTemporaryFile(dir=tmp_dir, delete=False) as out:
                    tmp = Path(out.name)
                    shutil.copyfileobj(response, out, 1024 * 1024)
            result = window.create_file_dialog(webview.FileDialog.SAVE, directory=downloads_dir(),
                                               save_filename=name)
            if not result:
                return {"cancelled": True}
            target = result if isinstance(result, str) else result[0]
            shutil.move(str(tmp), target)
            tmp = None
            logger.info("Saved download to %s", target)
            return {"path": target}
        except Exception as exc:
            logger.exception("Download of %s failed", path)
            return {"error": t["download_failed"].format(error=exc)}
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)

    def open_window(self, path: str):
        """Links that open a new tab in a browser (e.g. a photo in full size) get their own window."""
        import webview

        webview.create_window(APP_NAME, url=self._url(path), js_api=self, width=1100, height=800,
                              text_select=True, zoomable=True)

    def open_external(self, url: str):
        """Links to other websites open in the normal browser."""
        if url.startswith(("http://", "https://", "mailto:")):
            webbrowser.open(url)



def setup_logging(home: Path):
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    # A windowed app (PyInstaller --windowed) has no stdout/stderr
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    try:
        # crashes in native code (web view) still leave a Python stack trace in the logs
        import faulthandler

        global _fault_log
        _fault_log = open(logs / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(_fault_log)
    except Exception:
        pass
    handler = logging.handlers.RotatingFileHandler(logs / "aviationlog.log", maxBytes=2_000_000, backupCount=3,
                                                   encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)


def main(argv: list[str] | None = None):
    """Modes: app window (default), --browser (small window + browser), --headless, --self-test."""
    argv = list(sys.argv[1:] if argv is None else argv)
    headless = "--headless" in argv
    self_test = "--self-test" in argv
    # An already running AviationLog (e.g. a second double click): open it in the browser.
    home = data_home()
    cfg_path = home / "config.json"
    if cfg_path.exists() and not self_test:
        try:
            port = int(json.loads(cfg_path.read_text(encoding="utf-8")).get("port") or DEFAULT_PORT)
            if running_aviationlog(port):
                webbrowser.open(f"http://127.0.0.1:{port}")
                return
        except Exception:
            pass

    launcher = Launcher()

    def on_terminate(signum, frame):
        # logout / shutdown / kill: stop PostgreSQL cleanly instead of leaving it running
        launcher.shutdown()
        os._exit(0)

    signal.signal(signal.SIGTERM, on_terminate)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, on_terminate)

    if headless:
        launcher.run_headless()
        return
    if self_test:
        watchdog = threading.Timer(300, lambda: (logger.error("SELF-TEST: timeout"), os._exit(3)))
        watchdog.daemon = True
        watchdog.start()
    use_browser = "--browser" in argv or launcher.cfg.get("window") == "browser"
    if not use_browser:
        try:
            launcher.run_app(self_test=self_test)
            if self_test:
                sys.exit(0 if launcher.self_test_ok else 1)
            return
        except SystemExit:
            raise
        except Exception:
            # e.g. no WebView2 runtime on Windows or no WebKitGTK on Linux
            logger.exception("App window not available, using the browser instead")
            if self_test:
                sys.exit(2)
    try:
        import tkinter  # noqa: F401
    except Exception:
        logger.warning("No tkinter available, running without a window")
        launcher.run_headless()
        return
    launcher.run_window()


if __name__ == "__main__":
    main()
