"""Starts a built desktop app headless with an empty data folder and checks that it answers.

    python desktop/smoke_test.py <path to the AviationLog executable>
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def stop_leftover_postgres(exe: Path, home: Path):
    """On Windows terminate() kills the app hard, so its PostgreSQL keeps running."""
    for base in (exe.parent / "pgsql", exe.parent.parent / "Resources" / "pgsql"):
        for name in ("pg_ctl.exe", "pg_ctl"):
            pg_ctl = base / "pgserver" / "pginstall" / "bin" / name
            if pg_ctl.exists() and (home / "postgres" / "postmaster.pid").exists():
                subprocess.run([str(pg_ctl), "-D", str(home / "postgres"), "-m", "fast", "-w", "stop"],
                               stdin=subprocess.DEVNULL, timeout=120)
                return


def main():
    exe = Path(sys.argv[1]).resolve()
    home = Path(tempfile.mkdtemp(prefix="aviationlog-smoke-"))
    port = 18190
    (home / "config.json").write_text(json.dumps({"port": port, "open_browser": False}), encoding="utf-8")
    env = dict(os.environ, AVIATIONLOG_HOME=str(home))
    proc = subprocess.Popen([str(exe), "--headless"], env=env)
    ok = False
    try:
        deadline = time.time() + 240
        while time.time() < deadline and proc.poll() is None:
            try:
                status, body = get(f"http://127.0.0.1:{port}/healthz")
                if status == 200 and b'"version"' in body:
                    print("healthz:", body.decode())
                    status, body = get(f"http://127.0.0.1:{port}/login")
                    ok = status == 200 and b"<form" in body
                    print("login page:", status)
                    break
            except Exception:
                time.sleep(2)
    finally:
        proc.terminate()
        try:
            proc.wait(60)
        except subprocess.TimeoutExpired:
            proc.kill()
        stop_leftover_postgres(exe, home)
        for log in sorted((home / "logs").glob("*.log")):
            print(f"===== {log.name}")
            print(log.read_text(encoding="utf-8", errors="replace")[-6000:])
    if not ok:
        sys.exit("Smoke test failed: the app did not answer")
    print("Smoke test passed (server)")
    window_test(exe)


def window_test(exe: Path):
    """The app window (system web view) shows the login page and the JavaScript bridge works."""
    home = Path(tempfile.mkdtemp(prefix="aviationlog-window-"))
    (home / "config.json").write_text(json.dumps({"port": 18191}), encoding="utf-8")
    cmd = [str(exe), "--self-test"]
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        cmd = ["xvfb-run", "-a"] + cmd
    result = subprocess.run(cmd, env=dict(os.environ, AVIATIONLOG_HOME=str(home)), timeout=360)
    stop_leftover_postgres(exe, home)
    log = home / "logs" / "aviationlog.log"
    if log.exists():
        text = log.read_text(encoding="utf-8", errors="replace")
        print("===== window test log")
        print(text[-6000:])
    if result.returncode != 0:
        sys.exit(f"Window test failed (exit code {result.returncode})")
    print("Smoke test passed (app window)")


if __name__ == "__main__":
    main()
