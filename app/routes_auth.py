"""Login, two-factor authentication, password reset and the user's own profile."""
import os

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

import emailer
import i18n
import models
import security
from database import get_db
from web import THEMES, flash, get_user, load_user, render, t

router = APIRouter()


def _redirect(url: str):
    return RedirectResponse(url, status_code=303)


@router.get("/login")
def login_form(request: Request, db: Session = Depends(get_db)):
    if load_user(request, db):
        return _redirect("/")
    return render(request, "login.html", {"error": None, "smtp": emailer.is_configured()})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    load_user(request, db)
    user = db.query(models.User).filter(func.lower(models.User.username) == username.strip().lower()).first()
    if not user or not security.verify_password(password, user.password_hash):
        return render(request, "login.html", {"error": t(request, "login.failed"),
                                              "smtp": emailer.is_configured()}, status_code=401)
    lang = request.session.get("lang")
    request.session.clear()
    if lang:
        request.session["lang"] = lang
    if user.totp_enabled:
        request.session["pending_2fa_user_id"] = user.id
        return _redirect("/login/2fa")
    request.session["user_id"] = user.id
    return _redirect("/")


@router.get("/login/2fa")
def login_2fa_form(request: Request, db: Session = Depends(get_db)):
    load_user(request, db)
    if not request.session.get("pending_2fa_user_id"):
        return _redirect("/login")
    return render(request, "login_2fa.html", {"error": None})


@router.post("/login/2fa")
def login_2fa(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    load_user(request, db)
    user_id = request.session.get("pending_2fa_user_id")
    user = db.get(models.User, user_id) if user_id else None
    if not user:
        return _redirect("/login")
    ok = security.verify_totp(user.totp_secret, code)
    if not ok:
        remaining = security.use_recovery_code(user.recovery_codes, code)
        if remaining is not None:
            user.recovery_codes = remaining
            db.commit()
            ok = True
            flash(request, "recovery_code_used", count=len(remaining))
    if not ok:
        return render(request, "login_2fa.html", {"error": t(request, "login.code_invalid")}, status_code=401)
    request.session.pop("pending_2fa_user_id", None)
    request.session["user_id"] = user.id
    return _redirect("/")


@router.get("/logout")
def logout(request: Request):
    lang = request.session.get("lang")
    request.session.clear()
    if lang:
        request.session["lang"] = lang
    return _redirect("/login")


@router.get("/lang/{code}")
def set_language(code: str, request: Request, db: Session = Depends(get_db)):
    if code in i18n.languages():
        request.session["lang"] = code
        user = load_user(request, db)
        if user:
            user.language = code
            db.commit()
    return _redirect(request.headers.get("referer") or "/")


@router.get("/about")
def about(request: Request, db: Session = Depends(get_db)):
    load_user(request, db)
    return render(request, "about.html")


# ---- password reset --------------------------------------------------------------------

@router.get("/forgot")
def forgot_form(request: Request, db: Session = Depends(get_db)):
    load_user(request, db)
    return render(request, "forgot.html", {"sent": False, "smtp": emailer.is_configured()})


@router.post("/forgot")
def forgot(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    load_user(request, db)
    user = db.query(models.User).filter(func.lower(models.User.email) == email.strip().lower()).first()
    if user and emailer.is_configured():
        base = os.environ.get("BASE_URL") or str(request.base_url).rstrip("/")
        link = f"{base.rstrip('/')}/reset/{security.make_reset_token(user)}"
        lang = i18n.pick_language(user.language, None)
        emailer.send_mail(
            user.email,
            i18n.translate(lang, "reset.mail_subject"),
            i18n.translate(lang, "reset.mail_body", username=user.username, link=link),
        )
    # same answer whether the address exists or not
    return render(request, "forgot.html", {"sent": True, "smtp": emailer.is_configured()})


def _user_from_token(db, token: str):
    data = security.read_reset_token(token)
    if not data:
        return None
    user = db.get(models.User, data.get("u"))
    if not user or user.password_hash[-12:] != data.get("p"):
        return None
    return user


@router.get("/reset/{token}")
def reset_form(token: str, request: Request, db: Session = Depends(get_db)):
    load_user(request, db)
    user = _user_from_token(db, token)
    return render(request, "reset.html", {"valid": bool(user), "token": token, "error": None})


@router.post("/reset/{token}")
def reset(token: str, request: Request, password: str = Form(...), password_confirm: str = Form(...),
          db: Session = Depends(get_db)):
    load_user(request, db)
    user = _user_from_token(db, token)
    if not user:
        return render(request, "reset.html", {"valid": False, "token": token, "error": None})
    if password != password_confirm or len(password) < 8:
        return render(request, "reset.html", {"valid": True, "token": token,
                                              "error": t(request, "msg.password_invalid")})
    user.password_hash = security.hash_password(password)
    db.commit()
    flash(request, "password_updated")
    return _redirect("/login")


# ---- profile ----------------------------------------------------------------------------

@router.get("/profile")
def profile(request: Request, user: models.User = Depends(get_user)):
    codes = request.session.pop("new_recovery_codes", None)
    return render(request, "profile.html", {"recovery_codes": codes})


@router.post("/profile/settings")
def profile_settings(request: Request, language: str = Form(""), theme: str = Form("system"),
                     email: str = Form(""), user: models.User = Depends(get_user),
                     db: Session = Depends(get_db)):
    if language in i18n.languages():
        user.language = language
        request.session["lang"] = language
    if theme in THEMES:
        user.theme = theme
    user.email = email.strip() or None
    db.commit()
    flash(request, "saved")
    return _redirect("/profile")


@router.post("/profile/password")
def profile_password(request: Request, current_password: str = Form(...), new_password: str = Form(...),
                     new_password_confirm: str = Form(...), user: models.User = Depends(get_user),
                     db: Session = Depends(get_db)):
    if not security.verify_password(current_password, user.password_hash):
        flash(request, "password_wrong")
    elif new_password != new_password_confirm or len(new_password) < 8:
        flash(request, "password_invalid")
    else:
        user.password_hash = security.hash_password(new_password)
        db.commit()
        flash(request, "password_updated")
    return _redirect("/profile")


@router.get("/profile/2fa/setup")
def totp_setup_form(request: Request, user: models.User = Depends(get_user)):
    if user.totp_enabled:
        return _redirect("/profile")
    secret = request.session.get("pending_totp_secret") or security.new_totp_secret()
    request.session["pending_totp_secret"] = secret
    return render(request, "totp_setup.html", {
        "secret": secret,
        "qr_svg": security.totp_qr_svg(secret, user.username),
    })


@router.post("/profile/2fa/setup")
def totp_setup(request: Request, code: str = Form(...), user: models.User = Depends(get_user),
               db: Session = Depends(get_db)):
    secret = request.session.get("pending_totp_secret")
    if not secret or not security.verify_totp(secret, code):
        flash(request, "code_invalid")
        return _redirect("/profile/2fa/setup")
    codes, hashes = security.new_recovery_codes()
    user.totp_secret = secret
    user.totp_enabled = True
    user.recovery_codes = hashes
    db.commit()
    request.session.pop("pending_totp_secret", None)
    request.session["new_recovery_codes"] = codes
    flash(request, "totp_enabled")
    return _redirect("/profile#security")


@router.post("/profile/2fa/recovery")
def totp_recovery(request: Request, password: str = Form(...), user: models.User = Depends(get_user),
                  db: Session = Depends(get_db)):
    if not user.totp_enabled or not security.verify_password(password, user.password_hash):
        flash(request, "password_wrong")
        return _redirect("/profile#security")
    codes, hashes = security.new_recovery_codes()
    user.recovery_codes = hashes
    db.commit()
    request.session["new_recovery_codes"] = codes
    return _redirect("/profile#security")


@router.post("/profile/2fa/disable")
def totp_disable(request: Request, password: str = Form(...), user: models.User = Depends(get_user),
                 db: Session = Depends(get_db)):
    if not security.verify_password(password, user.password_hash):
        flash(request, "password_wrong")
        return _redirect("/profile#security")
    user.totp_enabled = False
    user.totp_secret = None
    user.recovery_codes = []
    db.commit()
    flash(request, "totp_disabled")
    return _redirect("/profile#security")
