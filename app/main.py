from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
from app.domain import DomainError, StrategyConfig
from app.services.jobs import bootstrap_job, build_scheduler, configure_runtime
from app.services.runtime import ApplicationRuntime, RuntimeUnavailable

logger = logging.getLogger(__name__)

PRODUCT_ID = "SPY-FUTURES-ALLOCATOR-1.0.0"
CHART_VIEWS = ("performance", "drawdowns", "exposure", "trend", "risk")


def _chart_view(value: str | None) -> str:
    return value if value in CHART_VIEWS else "performance"


def _integer_input(value: object) -> object:
    """Accept HTML integer text or a real int, but never numeric coercions."""

    if value is None or type(value) is int:
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        digits = cleaned[1:] if cleaned[:1] in {"+", "-"} else cleaned
        if digits and digits.isascii() and digits.isdigit():
            return cleaned
    raise ValueError("integer settings must use whole numbers")


class SettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_amount_usd: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    current_es_quantity: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    current_mes_quantity: int | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    ibkr_host: str = Field(min_length=1, max_length=253)
    ibkr_port: int = Field(ge=1, le=65_535)
    ibkr_client_id: int = Field(ge=0, le=2_147_483_647)
    fast_sma_sessions: int = Field(ge=1, le=100_000)
    slow_sma_sessions: int = Field(ge=2, le=100_000)
    rebound_fraction: Decimal = Field(gt=0)
    rv_window_sessions: int = Field(ge=2, le=100_000)
    annualization_sessions: int = Field(ge=1, le=100_000)
    rv_threshold: Decimal = Field(gt=0)
    normal_exposure: Decimal = Field(gt=0)
    high_vol_exposure: Decimal = Field(gt=0)
    review_weekdays: tuple[int, ...] = Field(min_length=1, max_length=5)

    @field_validator(
        "strategy_amount_usd",
        "rebound_fraction",
        "rv_threshold",
        "normal_exposure",
        "high_vol_exposure",
    )
    @classmethod
    def finite_decimal(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("numeric settings must be finite")
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

    @field_validator(
        "current_es_quantity",
        "current_mes_quantity",
        "ibkr_port",
        "ibkr_client_id",
        "fast_sma_sessions",
        "slow_sma_sessions",
        "rv_window_sessions",
        "annualization_sessions",
        mode="before",
    )
    @classmethod
    def integer_settings_are_not_coerced(cls, value: object) -> object:
        return _integer_input(value)

    @field_validator("review_weekdays", mode="before")
    @classmethod
    def weekdays_are_integers(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            raise ValueError("review weekdays must be integers")
        try:
            normalized = tuple(_integer_input(day) for day in value)
        except ValueError as exc:
            raise ValueError("review weekdays must be integers") from exc
        if any(day is None for day in normalized):
            raise ValueError("review weekdays must be integers")
        return normalized

    @field_validator("review_weekdays")
    @classmethod
    def canonical_weekdays(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(day < 0 or day > 4 for day in value):
            raise ValueError("review weekdays must be between Monday and Friday")
        if len(set(value)) != len(value):
            raise ValueError("review weekdays must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def valid_strategy(self) -> Self:
        StrategyConfig(
            fast_sma_sessions=self.fast_sma_sessions,
            slow_sma_sessions=self.slow_sma_sessions,
            rebound_fraction=self.rebound_fraction,
            rv_window_sessions=self.rv_window_sessions,
            annualization_sessions=self.annualization_sessions,
            rv_threshold=self.rv_threshold,
            normal_exposure=self.normal_exposure,
            high_vol_exposure=self.high_vol_exposure,
            review_weekdays=self.review_weekdays,
        )
        return self


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


def _optional_integer(form: Any, name: str) -> str | None:
    value = str(form.get(name, "")).strip()
    return value or None


def _form_settings(form: Any) -> SettingsPayload:
    weekdays = tuple(form.getlist("review_weekdays"))
    return SettingsPayload.model_validate(
        {
            "strategy_amount_usd": form.get("strategy_amount_usd"),
            "current_es_quantity": _optional_integer(form, "current_es_quantity"),
            "current_mes_quantity": _optional_integer(form, "current_mes_quantity"),
            "ibkr_host": form.get("ibkr_host"),
            "ibkr_port": form.get("ibkr_port"),
            "ibkr_client_id": form.get("ibkr_client_id"),
            "fast_sma_sessions": form.get("fast_sma_sessions"),
            "slow_sma_sessions": form.get("slow_sma_sessions"),
            "rebound_fraction": Decimal(str(form.get("rebound_percent"))) / Decimal(100),
            "rv_window_sessions": form.get("rv_window_sessions"),
            "annualization_sessions": form.get("annualization_sessions"),
            "rv_threshold": Decimal(str(form.get("rv_threshold_percent"))) / Decimal(100),
            "normal_exposure": form.get("normal_exposure"),
            "high_vol_exposure": form.get("high_vol_exposure"),
            "review_weekdays": weekdays,
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
    runtime = ApplicationRuntime(repository, auth_store)
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
        title="SPY Futures Allocator",
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
            operational = OperationalOverrides(
                ibkr_connection=runtime.ibkr_state,
                config_fingerprint=runtime.configuration_fingerprint,
                next_regime_evaluation=runtime.next_regime_close(),
                next_volatility_review=runtime.next_review_close(),
                next_market_open=runtime.next_market_open(),
            )
        except (DomainError, RuntimeUnavailable):
            operational = OperationalOverrides(
                ibkr_connection=runtime.ibkr_state,
                config_fingerprint=runtime.configuration_fingerprint,
            )
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
        message_key = request.query_params.get("message")
        messages = {
            "settings-saved": "Settings saved and strategy history replayed.",
            "strategy-reset": "Strategy defaults restored and history replayed.",
        }
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "view": dashboard(csrf=csrf),
                "message": messages.get(message_key) if message_key is not None else None,
                "error": None,
                "chart_view": _chart_view(request.query_params.get("chart_view")),
            },
        )

    def settings_error(request: Request, raw_session: str) -> Response:
        csrf = _csrf_token(raw_session, config.session_secret.get_secret_value())
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "view": dashboard(csrf=csrf),
                "message": None,
                "error": "Please correct the invalid settings values.",
                "chart_view": "performance",
            },
            status_code=422,
        )

    @app.post("/settings")
    async def settings_form(request: Request, _session: object = Depends(active_guard)) -> Response:
        form = await request.form(max_fields=24, max_files=0)
        raw_session = request.cookies.get(auth_settings.cookie_name, "")
        _verify_csrf(
            raw_session,
            str(form.get("csrf_token", "")),
            config.session_secret.get_secret_value(),
        )
        try:
            payload = _form_settings(form)
            runtime.save_settings(**payload.model_dump())
        except (ArithmeticError, DomainError, RuntimeUnavailable, ValueError):
            return settings_error(request, raw_session)
        return RedirectResponse("/?message=settings-saved", status_code=303)

    @app.post("/settings/reset-strategy")
    async def reset_strategy(
        request: Request, _session: object = Depends(active_guard)
    ) -> Response:
        form = await request.form(max_fields=2, max_files=0)
        raw_session = request.cookies.get(auth_settings.cookie_name, "")
        _verify_csrf(
            raw_session,
            str(form.get("csrf_token", "")),
            config.session_secret.get_secret_value(),
        )
        runtime.reset_strategy()
        return RedirectResponse("/?message=strategy-reset", status_code=303)

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

    return app


def run() -> None:
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port)  # noqa: S104
