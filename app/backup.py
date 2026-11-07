import gzip
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("backup")

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/data/backups"))
BACKUP_PREFIX = "aviationlog_backup_"


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql://aviationlog:aviationlog@db:5432/aviationlog")


def list_backups():
    """All existing backups, newest first."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(BACKUP_DIR.glob(f"{BACKUP_PREFIX}*.sql.gz"), reverse=True)
    result = []
    for f in files:
        stat = f.stat()
        result.append(
            {
                "filename": f.name,
                "size_kb": round(stat.st_size / 1024, 1),
                "created_at": datetime.fromtimestamp(stat.st_mtime),
            }
        )
    return result


def run_backup(retention: int = 14) -> str:
    """Creates a compressed pg_dump and removes backups beyond the retention limit."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sql_path = BACKUP_DIR / f"{BACKUP_PREFIX}{timestamp}.sql"
    gz_path = BACKUP_DIR / f"{BACKUP_PREFIX}{timestamp}.sql.gz"

    cmd = [
        "pg_dump",
        _database_url(),
        "--clean",
        "--if-exists",
        "-f",
        str(sql_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr}")

    with open(sql_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    sql_path.unlink()

    _cleanup_old_backups(retention)
    logger.info(f"Backup created: {gz_path.name}")
    return gz_path.name


def _cleanup_old_backups(retention: int):
    files = sorted(
        BACKUP_DIR.glob(f"{BACKUP_PREFIX}*.sql.gz"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for old_file in files[retention:]:
        old_file.unlink()
        logger.info(f"Old backup removed: {old_file.name}")


def restore_backup(filename: str):
    """Restores a backup (overwrites all current data)."""
    gz_path = BACKUP_DIR / os.path.basename(filename)
    if not gz_path.exists() or not gz_path.name.startswith(BACKUP_PREFIX):
        raise FileNotFoundError("Backup not found")

    sql_path = gz_path.with_suffix("")  # strips .gz
    with gzip.open(gz_path, "rb") as f_in, open(sql_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    try:
        cmd = ["psql", _database_url(), "-f", str(sql_path)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            raise RuntimeError(f"Restore failed: {result.stderr}")
    finally:
        sql_path.unlink(missing_ok=True)


def save_uploaded(fileobj, filename: str) -> str:
    """Stores an uploaded backup file so it can be restored."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    safe = os.path.basename(filename or "")
    if not safe.startswith(BACKUP_PREFIX) or not safe.endswith(".sql.gz"):
        safe = f"{BACKUP_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S')}_upload.sql.gz"
    with open(BACKUP_DIR / safe, "wb") as out:
        shutil.copyfileobj(fileobj, out)
    return safe


def delete_backup(filename: str):
    gz_path = BACKUP_DIR / os.path.basename(filename)
    if gz_path.exists() and gz_path.name.startswith(BACKUP_PREFIX):
        gz_path.unlink()


def backup_path(filename: str) -> Path:
    gz_path = BACKUP_DIR / os.path.basename(filename)
    if not gz_path.exists() or not gz_path.name.startswith(BACKUP_PREFIX):
        raise FileNotFoundError("Backup not found")
    return gz_path
