import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

import i18n
import importers  # noqa: F401  registers the import jobs
import jobs
import models
import scan  # noqa: F401  registers the scan jobs
import scheduler
import security
import web
from database import Base, SessionLocal, engine
from routes_admin import router as admin_router
from routes_auth import router as auth_router
from routes_collection import router as collection_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("aviationlog")

HERE = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup()
    yield
    sched = getattr(app.state, "scheduler", None)
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="AviationLog", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SECRET_KEY", "change-me-please"),
    https_only=os.environ.get("SESSION_HTTPS_ONLY", "false").lower() == "true",
    same_site="lax",
    max_age=60 * 60 * 24 * 30,
)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
app.include_router(auth_router)
app.include_router(collection_router)
app.include_router(admin_router)


@app.exception_handler(web.LoginRequired)
def login_required(request: Request, exc: web.LoginRequired):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(StarletteHTTPException)
def http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith(("/api/", "/static/")):
        return Response(str(exc.detail), status_code=exc.status_code)
    db = SessionLocal()
    try:
        web.load_user(request, db)
        return web.render(request, "error.html", {"status": exc.status_code, "detail": exc.detail},
                          status_code=exc.status_code)
    finally:
        db.close()


@app.get("/sw.js")
def service_worker():
    # served from the root so its scope covers the whole app
    with open(os.path.join(HERE, "static", "sw.js"), encoding="utf-8") as f:
        js = f.read().replace("__APP_VERSION__", web.APP_VERSION)
    return Response(js, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": web.APP_VERSION}


def migrate():
    """create_all does not add columns to existing tables; add them here when needed."""
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_aircraft_registration_trgm ON aircraft "
            "USING gin (registration gin_trgm_ops)"
        ))


def startup():
    i18n.load()
    migrate()
    db = SessionLocal()
    try:
        if db.query(models.User).count() == 0:
            username = os.environ.get("ADMIN_USERNAME", "admin")
            password = os.environ.get("ADMIN_PASSWORD") or "changeme"
            db.add(models.User(
                username=username,
                email=os.environ.get("ADMIN_EMAIL") or None,
                password_hash=security.hash_password(password),
                role=models.Role.admin,
                recovery_codes=[],
            ))
            db.commit()
            logger.info("Admin user '%s' created", username)
        jobs.recover(db)
    finally:
        db.close()
    if os.environ.get("DISABLE_SCHEDULER") != "1":
        app.state.scheduler = scheduler.start_scheduler()
