from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from app import __version__
from app.auth import (
    AuthGuard,
    AuthService,
    AuthSettings,
    OidcProvider,
    SqliteAuthStore,
    build_auth_router,
    login_redirect,
)
from app.config import AppConfig, get_config
from app.db import (
    DashboardView,
    Database,
    OperationalOverrides,
    Repository,
    build_dashboard_view,
)
from app.domain.errors import DomainError
from app.security import SecretBox
from app.services.jobs import bootstrap_job, build_scheduler, configure_runtime
from app.services.runtime import ApplicationRuntime

logger = logging.getLogger(__name__)

PRODUCT_ID = "ARL5-MES-SIZING-EVENTS-1.0.0"
SAFE_MODEL = re.compile(r"^gpt-[A-Za-z0-9._-]{1,90}$")


class SettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_amount_usd: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    current_mes_quantity: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    ibkr_host: str = Field(min_length=1, max_length=253)
    ibkr_port: int = Field(ge=1, le=65_535)
    ibkr_client_id: int = Field(ge=0, le=2_147_483_647)
    openai_api_key: str | None = Field(default=None, min_length=20, max_length=512)
    detector_model: str = Field(min_length=5, max_length=100)
    verifier_model: str = Field(min_length=5, max_length=100)

    @field_validator("strategy_amount_usd")
    @classmethod
    def finite_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("strategy amount must be finite")
        return value

    @field_validator("ibkr_host")
    @classmethod
    def safe_host(cls, value: str) -> str:
        value = value.strip()
        if not value or any(character.isspace() for character in value):
            raise ValueError("IBKR host must be a hostname or IP address")
        if any(character in value for character in "/?#@"):
            raise ValueError("IBKR host must not be a URL")
        return value

    @field_validator("openai_api_key")
    @classmethod
    def clean_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("detector_model", "verifier_model")
    @classmethod
    def safe_model(cls, value: str) -> str:
        value = value.strip()
        if not SAFE_MODEL.fullmatch(value):
            raise ValueError("model must be a valid OpenAI GPT model identifier")
        return value


def _decode_settings_key(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise ValueError("SETTINGS_ENCRYPTION_KEY must be URL-safe base64") from exc
    if len(decoded) != 32:
        raise ValueError("SETTINGS_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return decoded


def _csrf_token(raw_session: str, secret: str) -> str:
    return hmac.new(secret.encode(), raw_session.encode(), hashlib.sha256).hexdigest()


def _verify_csrf(raw_session: str, submitted: str, secret: str) -> None:
    expected = _csrf_token(raw_session, secret)
    if not submitted or not hmac.compare_digest(expected, submitted):
        raise HTTPException(status_code=403, detail="Invalid form token")


def _same_origin(request: Request, origin: str) -> bool:
    supplied = request.headers.get("origin")
    if supplied:
        return supplied.rstrip("/") == origin
    referer = request.headers.get("referer")
    if not referer:
        return False
    parsed = urlsplit(referer)
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/") == origin


def _security_headers(response: Response, *, https: bool) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'; script-src 'none'; "
        "style-src 'self'; img-src 'self' data:; connect-src 'self'",
    )
    if https:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )


def _form_settings(form: Any) -> SettingsPayload:
    current = str(form.get("current_mes_quantity", "")).strip()
    api_key = str(form.get("openai_api_key", "")).strip()
    return SettingsPayload.model_validate(
        {
            "strategy_amount_usd": form.get("strategy_amount_usd"),
            "current_mes_quantity": current if current else None,
            "ibkr_host": form.get("ibkr_host"),
            "ibkr_port": form.get("ibkr_port"),
            "ibkr_client_id": form.get("ibkr_client_id"),
            "openai_api_key": api_key if api_key else None,
            "detector_model": form.get("detector_model"),
            "verifier_model": form.get("verifier_model"),
        }
    )


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or get_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    repository = Repository(Database(config.database_path))
    repository.initialize()
    auth_settings = AuthSettings(
        issuer_url=config.issuer,
        client_id=config.oidc_client_id,
        client_secret=config.oidc_client_secret.get_secret_value(),
        app_origin=config.origin,
        encryption_key=_decode_settings_key(config.settings_encryption_key.get_secret_value()),
        database_path=str(config.database_path),
        cookie_name=config.session_cookie_name,
        allow_insecure_http=config.app_origin.scheme == "http",
    )
    auth_store = SqliteAuthStore(str(config.database_path))
    auth_service = AuthService(auth_settings, auth_store, OidcProvider(auth_settings))
    runtime = ApplicationRuntime(
        repository,
        auth_store,
        SecretBox(config.settings_encryption_key.get_secret_value()),
    )
    configure_runtime(runtime)
    scheduler = build_scheduler(config.database_path)

    @asynccontextmanager
    async def lifespan(application: FastAPI):  # type: ignore[no-untyped-def]
        scheduler.start()
        scheduler.add_job(
            bootstrap_job,
            "date",
            id="startup-bootstrap",
            replace_existing=True,
        )
        yield
        scheduler.shutdown(wait=False)
        runtime.reset_ibkr()

    app = FastAPI(
        title="ARL-5 Simplified",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.repository = repository
    app.state.runtime = runtime
    app.state.auth_service = auth_service
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        is_oidc_form_post = request.method == "POST" and request.url.path == "/auth/oidc/callback"
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and not is_oidc_form_post
            and not _same_origin(request, config.origin)
        ):
            response: Response = JSONResponse(
                {"detail": "Cross-site form submissions are forbidden"}, status_code=403
            )
            _security_headers(response, https=config.app_origin.scheme == "https")
            return response
        response = await call_next(request)
        _security_headers(response, https=config.app_origin.scheme == "https")
        if request.url.path == "/login" or request.url.path.startswith("/auth/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(build_auth_router(auth_service))

    passive_guard = AuthGuard(auth_service, user_activity=False)
    active_guard = AuthGuard(auth_service, user_activity=True)

    def dashboard(*, csrf: str = "") -> DashboardView:
        try:
            schedule = runtime.current_schedule()
            operational = OperationalOverrides(
                ibkr_connection=runtime.ibkr_state,
                next_valid_signal=schedule.next_valid_signal,
                next_execution_window=schedule.next_valid_execution_window,
            )
        except DomainError:
            operational = OperationalOverrides(ibkr_connection=runtime.ibkr_state)
        return build_dashboard_view(repository, operational=operational, csrf_token=csrf)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "product": PRODUCT_ID, "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Response:
        raw_session = request.cookies.get(auth_settings.cookie_name)
        session = auth_service.current_session(raw_session, user_activity=True)
        if session is None:
            return login_redirect(request)
        csrf = _csrf_token(raw_session or "", config.session_secret.get_secret_value())
        view = dashboard(csrf=csrf)
        message_key = request.query_params.get("message")
        messages = {
            "settings-saved": "Settings saved. Data jobs will use the new configuration.",
            "event-check-complete": "Event evidence check completed.",
        }
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "view": view,
                "message": messages.get(message_key) if message_key is not None else None,
                "error": None,
            },
        )

    @app.post("/settings")
    async def settings_form(request: Request, _session: object = Depends(active_guard)) -> Response:
        form = await request.form(max_fields=20, max_files=0)
        raw_session = request.cookies.get(auth_settings.cookie_name, "")
        _verify_csrf(
            raw_session,
            str(form.get("csrf_token", "")),
            config.session_secret.get_secret_value(),
        )
        try:
            payload = _form_settings(form)
        except Exception:
            csrf = _csrf_token(raw_session, config.session_secret.get_secret_value())
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                {
                    "view": dashboard(csrf=csrf),
                    "message": None,
                    "error": "Please correct the invalid settings values.",
                },
                status_code=422,
            )
        runtime.save_settings(**payload.model_dump())
        return RedirectResponse("/?message=settings-saved", status_code=303)

    @app.get("/api/current")
    async def current(_session: object = Depends(passive_guard)) -> object:
        return dashboard().as_dict()

    @app.put("/api/settings")
    async def update_settings(
        payload: SettingsPayload, _session: object = Depends(active_guard)
    ) -> object:
        return jsonable_encoder(runtime.save_settings(**payload.model_dump()))

    @app.get("/api/history")
    async def history(_session: object = Depends(passive_guard)) -> object:
        return jsonable_encoder(repository.recommendation_history(100))

    @app.get("/api/events")
    async def events(_session: object = Depends(passive_guard)) -> object:
        view = dashboard().as_dict()
        return {"event": view["event"]}

    @app.post("/api/events/check")
    async def event_check(request: Request, _session: object = Depends(active_guard)) -> Response:
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if content_type == "application/x-www-form-urlencoded":
            form = await request.form(max_fields=5, max_files=0)
            raw_session = request.cookies.get(auth_settings.cookie_name, "")
            _verify_csrf(
                raw_session,
                str(form.get("csrf_token", "")),
                config.session_secret.get_secret_value(),
            )
        try:
            await run_in_threadpool(runtime.check_events)
        except Exception:
            logger.exception("manual event check failed closed")
            if content_type == "application/x-www-form-urlencoded":
                csrf = _csrf_token(
                    request.cookies.get(auth_settings.cookie_name, ""),
                    config.session_secret.get_secret_value(),
                )
                return templates.TemplateResponse(
                    request,
                    "dashboard.html",
                    {
                        "view": dashboard(csrf=csrf),
                        "message": None,
                        "error": "The event check could not complete. No event window was changed.",
                    },
                    status_code=503,
                )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Event check could not complete; no event window was changed",
            ) from None
        if content_type == "application/x-www-form-urlencoded":
            return RedirectResponse("/?message=event-check-complete", status_code=303)
        return JSONResponse({"status": "complete"})

    return app


def run() -> None:
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port)  # noqa: S104
