"""Key/value settings stored in the database."""
import models

DEFAULTS = {
    "backup_time": "03:00",
    "backup_retention": "14",
}


def get(db, key: str) -> str:
    row = db.get(models.Setting, key)
    return row.value if row else DEFAULTS.get(key, "")


def set(db, key: str, value: str):
    row = db.get(models.Setting, key)
    if row:
        row.value = value
    else:
        db.add(models.Setting(key=key, value=value))
    db.commit()
