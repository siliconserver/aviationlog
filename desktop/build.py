"""Builds the AviationLog desktop app for the current platform.

    pip install -r requirements.txt -r desktop/requirements.txt
    python desktop/build.py [--version 0.3]

Result in dist/:
- Windows: AviationLog-<version>-windows-x64-setup.exe (Inno Setup, per-user install)
- macOS:   AviationLog-<version>-macos-arm64.dmg
- Linux:   aviationlog_<version>_amd64.deb

The bundled PostgreSQL comes from the "pgserver" package (PostgreSQL 16 binaries for
Windows, macOS and Linux). Its files are copied unchanged to <app>/pgsql, because the
programs find their shared libraries by relative paths.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
DESKTOP = ROOT / "desktop"
BUILD = ROOT / "build" / "desktop"
DIST = ROOT / "dist"
NAME = "AviationLog"
BUNDLE_ID = "io.github.siliconserver.aviationlog"


def changelog_version() -> str:
    for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            return line[3:].split()[0]
    return "0.0"


def run(cmd: list, **kwargs):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def pgserver_files() -> tuple[Path, list[Path]]:
    """site-packages folder and the pgserver folders to ship (package + relocated libraries)."""
    spec = importlib.util.find_spec("pgserver")
    if not spec or not spec.submodule_search_locations:
        sys.exit("pgserver is not installed: pip install -r desktop/requirements.txt")
    pkg = Path(list(spec.submodule_search_locations)[0])
    site = pkg.parent
    folders = [pkg]
    libs = site / "pgserver.libs"  # Linux wheels (auditwheel)
    if libs.is_dir():
        folders.append(libs)
    return site, folders


def copy_postgres(target: Path):
    """Copies the PostgreSQL installation without headers, static libraries and Python code."""
    site, folders = pgserver_files()
    if target.exists():
        shutil.rmtree(target)

    def ignore(directory, names):
        rel = Path(directory).relative_to(site)
        skip = set()
        for n in names:
            if n in ("include", "pkgconfig", "__pycache__") or n.endswith((".a", ".py", ".pyc")):
                skip.add(n)
        if rel == Path("pgserver/pginstall/bin"):
            # only the programs the app uses; keeps the download small
            keep = {"initdb", "pg_ctl", "postgres", "pg_dump", "psql", "pg_isready"}
            for n in names:
                stem = n[:-4] if n.lower().endswith(".exe") else n
                if not n.lower().endswith(".dll") and stem not in keep:
                    skip.add(n)
        return skip

    for folder in folders:
        shutil.copytree(folder, target / folder.relative_to(site), ignore=ignore, symlinks=True)
    print(f"PostgreSQL copied to {target}")


def make_icons() -> dict:
    from PIL import Image

    BUILD.mkdir(parents=True, exist_ok=True)
    src = Image.open(APP / "static" / "icons" / "icon-512.png").convert("RGBA")
    ico = BUILD / "aviationlog.ico"
    src.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    icns = BUILD / "aviationlog.icns"
    src.save(icns)
    png = BUILD / "aviationlog.png"
    src.save(png)
    return {"ico": ico, "icns": icns, "png": png}


def pyinstaller(icons: dict):
    sep = os.pathsep
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--name", NAME, "--windowed",
        "--distpath", DIST, "--workpath", BUILD / "work", "--specpath", BUILD,
        "--paths", APP,
        "--add-data", f"{APP / 'static'}{sep}static",
        "--add-data", f"{APP / 'templates'}{sep}templates",
        "--add-data", f"{APP / 'i18n'}{sep}i18n",
        "--add-data", f"{ROOT / 'CHANGELOG.md'}{sep}.",
        "--collect-submodules", "uvicorn",
        "--hidden-import", "psycopg2",
        "--hidden-import", "multipart",
        "--hidden-import", "python_multipart",
        "--exclude-module", "pytest",
        "--exclude-module", "pgserver",
        "--collect-submodules", "webview",
    ]
    if sys.platform.startswith("linux"):
        # GTK and WebKitGTK come from the system (package dependencies), not from the bundle:
        # a bundled WebKit would not find the matching helper processes of the system.
        args += ["--collect-submodules", "gi"]  # incl. gi.overrides (Python helpers for GLib, Gtk)
        for module in LINUX_SYSTEM_GI:
            args += ["--exclude-module", f"gi.repository.{module}"]
    else:
        args += ["--exclude-module", "gi"]
    # every module of the web app (some are only imported for their side effects)
    for module in sorted(p.stem for p in APP.glob("*.py")):
        args += ["--hidden-import", module]
    if sys.platform == "win32":
        args += ["--icon", icons["ico"]]
    elif sys.platform == "darwin":
        args += ["--icon", icons["icns"], "--osx-bundle-identifier", BUNDLE_ID]
    args.append(DESKTOP / "aviationlog_desktop.py")
    run(args, cwd=ROOT)


LINUX_SYSTEM_GI = ("Gtk", "Gdk", "GdkPixbuf", "GLib", "GObject", "Gio", "Pango", "PangoCairo", "cairo",
                   "Atk", "HarfBuzz", "WebKit2", "JavaScriptCore", "Soup", "xlib", "freetype2")
# Shared libraries of the system that must not be bundled on Linux (they belong to GTK/GLib,
# which the app takes from the system); matched against the start of the file name.
LINUX_SYSTEM_LIBS = ("libglib-2.0", "libgobject-2.0", "libgio-2.0", "libgmodule-2.0", "libgthread-2.0",
                     "libgirepository-1.0", "libffi", "libgtk-3", "libgdk-3", "libgdk_pixbuf", "libwebkit2gtk",
                     "libjavascriptcoregtk", "libsoup", "libcairo", "libpango", "libharfbuzz", "libatk",
                     "libfontconfig", "libfreetype", "libX", "libxcb", "libwayland", "libepoxy", "libpixman",
                     "libpng16", "libmount", "libblkid", "libselinux", "libpcre", "libdbus", "libsystemd",
                     "libgcrypt", "libgpg-error", "liblzma", "libzstd", "libbrotli", "libexpat", "libuuid",
                     "libfribidi", "libthai", "libdatrie", "libgraphite2", "libxkbcommon")


def strip_system_libraries(app_dir: Path):
    internal = app_dir / "_internal"
    removed = []
    for path in internal.iterdir():
        # libraries of Python wheels carry a hash in their name (libpng16-1d1ba958.so…); they stay
        wheel_lib = re.search(r"-[0-9a-f]{8}\.so", path.name)
        if path.is_file() and path.name.startswith(LINUX_SYSTEM_LIBS) and ".so" in path.name and not wheel_lib:
            path.unlink()
            removed.append(path.name)
    typelibs = internal / "gi_typelibs"
    if typelibs.is_dir():
        shutil.rmtree(typelibs)
        removed.append("gi_typelibs/")
    print("Removed system libraries from the bundle:", ", ".join(sorted(removed)) or "none")


# ---- Packages ----------------------------------------------------------------------

def package_windows(version: str) -> Path:
    app_dir = DIST / NAME
    copy_postgres(app_dir / "pgsql")
    iscc = shutil.which("iscc") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if not Path(iscc).exists() and not shutil.which("iscc"):
        sys.exit("Inno Setup (ISCC.exe) not found")
    run([iscc, f"/DAppVersion={version}", f"/DSourceDir={app_dir}", f"/DOutputDir={DIST}",
         f"/DIconFile={BUILD / 'aviationlog.ico'}", DESKTOP / "windows" / "aviationlog.iss"])
    out = DIST / f"{NAME}-{version}-windows-x64-setup.exe"
    if not out.exists():
        sys.exit(f"Installer missing: {out}")
    return out


def package_macos(version: str) -> Path:
    app = DIST / f"{NAME}.app"
    copy_postgres(app / "Contents" / "Resources" / "pgsql")
    info_path = app / "Contents" / "Info.plist"
    with open(info_path, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleDisplayName": NAME,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.photography",
    })
    with open(info_path, "wb") as f:
        plistlib.dump(info, f)
    # Ad-hoc signatures (Apple Silicon only runs signed code; no Apple ID needed): first every
    # PostgreSQL program and library, then the whole app.
    macho_magic = {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xce\xfa\xed\xfe"}
    for path in sorted((app / "Contents" / "Resources" / "pgsql").rglob("*")):
        if path.is_file() and not path.is_symlink():
            with open(path, "rb") as f:
                if f.read(4) in macho_magic:
                    run(["codesign", "--force", "--sign", "-", path])
    run(["codesign", "--force", "--deep", "--sign", "-", app])
    run(["codesign", "--verify", "--deep", "--verbose=2", app])

    staging = BUILD / "dmg"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    run(["ditto", app, staging / app.name])
    os.symlink("/Applications", staging / "Applications")
    out = DIST / f"{NAME}-{version}-macos-arm64.dmg"
    out.unlink(missing_ok=True)
    run(["hdiutil", "create", "-volname", NAME, "-srcfolder", staging, "-ov", "-format", "UDZO", out])
    return out


def package_linux(version: str) -> Path:
    app_dir = DIST / NAME
    copy_postgres(app_dir / "pgsql")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    root = BUILD / "deb"
    if root.exists():
        shutil.rmtree(root)
    opt = root / "opt" / "aviationlog"
    shutil.copytree(app_dir, opt, symlinks=True)
    (root / "usr" / "bin").mkdir(parents=True)
    os.symlink("/opt/aviationlog/AviationLog", root / "usr" / "bin" / "aviationlog")
    apps = root / "usr" / "share" / "applications"
    apps.mkdir(parents=True)
    shutil.copy(DESKTOP / "linux" / "aviationlog.desktop", apps / "aviationlog.desktop")
    icons = root / "usr" / "share" / "icons" / "hicolor"
    for size in (48, 128, 256, 512):
        target = icons / f"{size}x{size}" / "apps"
        target.mkdir(parents=True)
        from PIL import Image

        Image.open(BUILD / "aviationlog.png").resize((size, size), Image.LANCZOS).save(target / "aviationlog.png")
    size_kb = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink()) // 1024
    debian = root / "DEBIAN"
    debian.mkdir()
    control = (DESKTOP / "linux" / "control").read_text(encoding="utf-8")
    control = control.replace("@VERSION@", version).replace("@ARCH@", arch).replace("@SIZE@", str(size_kb))
    (debian / "control").write_text(control, encoding="utf-8")
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            p = Path(dirpath) / d
            if not p.is_symlink():
                p.chmod(0o755)
        for fn in filenames:
            p = Path(dirpath) / fn
            if not p.is_symlink():
                p.chmod(0o755 if os.access(p, os.X_OK) else 0o644)
    out = DIST / f"aviationlog_{version}_{arch}.deb"
    out.unlink(missing_ok=True)
    run(["dpkg-deb", "--build", "--root-owner-group", "-Zxz", root, out])
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", default=changelog_version())
    parser.add_argument("--skip-package", action="store_true", help="only build the app folder")
    args = parser.parse_args()

    icons = make_icons()
    pyinstaller(icons)
    if sys.platform.startswith("linux"):
        strip_system_libraries(DIST / NAME)
    if args.skip_package:
        target = DIST / f"{NAME}.app" / "Contents" / "Resources" / "pgsql" if sys.platform == "darwin" \
            else DIST / NAME / "pgsql"
        copy_postgres(target)
        return
    if sys.platform == "win32":
        out = package_windows(args.version)
    elif sys.platform == "darwin":
        out = package_macos(args.version)
    else:
        out = package_linux(args.version)
    print(f"Built {out}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"file={out}\n")


if __name__ == "__main__":
    main()
