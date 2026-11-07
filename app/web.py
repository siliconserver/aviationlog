"""Shared web helpers: templates, current user, permissions, flash messages."""
import os
from datetime import date, datetime

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import i18n
import models
import photos
from database import get_db


class LoginRequired(Exception):
    pass


def _load_changelog():
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "CHANGELOG.md"), os.path.join(here, "..", "CHANGELOG.md")):
        if os.path.exists(path):
            break
    else:
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("## "):
                version, _, day = line[3:].strip().partition(" ")
                entries.append({"version": version, "date": day.strip("()"), "items": []})
            elif line.startswith("- ") and entries:
                entries[-1]["items"].append(line[2:].strip())
    return entries


CHANGELOG = _load_changelog()
APP_VERSION = CHANGELOG[0]["version"] if CHANGELOG else "dev"
THEMES = ("system", "light", "dark")


def _context(request: Request) -> dict:
    lang = getattr(request.state, "lang", None) or i18n.default_language()
    return {
        "user": getattr(request.state, "user", None),
        "lang": lang,
        "t": i18n.translator(lang),
        "languages": i18n.languages(),
        "photo_mode": photos.photo_mode(),
    }


templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"),
                            context_processors=[_context])
templates.env.globals.update(APP_VERSION=APP_VERSION, CHANGELOG=CHANGELOG, THEMES=THEMES)


def _fmt_date(value, lang="en"):
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M") if lang == "de" else value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y") if lang == "de" else value.isoformat()
    return str(value)


def _fmt_num(value, lang="en"):
    if value is None:
        return ""
    s = f"{value:,}"
    return s.replace(",", ".") if lang == "de" else s


templates.env.filters["d"] = _fmt_date
templates.env.filters["n"] = _fmt_num


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)


def t(request: Request, key: str, **kwargs) -> str:
    return i18n.translate(getattr(request.state, "lang", "en"), key, **kwargs)


def load_user(request: Request, db: Session):
    user_id = request.session.get("user_id")
    user = db.get(models.User, user_id) if user_id else None
    request.state.user = user
    request.state.lang = i18n.pick_language(
        (user.language if user else None) or request.session.get("lang"),
        request.headers.get("accept-language"),
    )
    return user


def get_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    user = load_user(request, db)
    if not user:
        raise LoginRequired()
    return user


def get_admin(user: models.User = Depends(get_user)) -> models.User:
    if user.role != models.Role.admin:
        raise HTTPException(403, "Admins only")
    return user


def flash(request: Request, key: str, **kwargs):
    request.session["flash"] = {"key": key, "args": kwargs}


def pop_flash(request: Request):
    data = request.session.pop("flash", None)
    if not data:
        return None
    return i18n.translate(getattr(request.state, "lang", "en"), "msg." + data["key"], **data.get("args", {}))


templates.env.globals["pop_flash"] = pop_flash
