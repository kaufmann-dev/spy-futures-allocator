"""App-owned login, OIDC callbacks, and authentication guard helpers."""

from __future__ import annotations

import html
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .models import AuthSession
from .security import sanitize_return_to
from .service import AuthError, AuthService, InvalidAuthRequest, Unauthenticated

LOGIN_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = LOGIN_CSP
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _login_page(return_to: str, *, error: str | None = None, logged_out: bool = False) -> str:
    safe_return = html.escape(return_to, quote=True)
    status = "<p class=status>You are signed out.</p>" if logged_out else ""
    if error:
        status = f'<p class="status error">{html.escape(error)}</p>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · ARL-5 Simplified</title>
<style>
:root {{ color-scheme: light dark; font: 16px/1.5 system-ui, sans-serif; }}
body {{
  margin: 0; min-height: 100vh; display: grid; place-items: center;
  background: #f5f5f0; color: #111;
}}
main {{
  width: min(28rem, calc(100% - 3rem)); border: 1px solid currentColor;
  padding: 2rem; background: #fff;
}}
h1 {{ font-size: 1.5rem; margin: 0 0 .5rem; }}
p {{ color: #555; }}
button {{
  width: 100%; margin-top: 1.25rem; border: 1px solid #111; background: #111;
  color: #fff; padding: .8rem 1rem; font: inherit; font-weight: 650; cursor: pointer;
}}
.status {{ border-left: 3px solid #157347; padding-left: .75rem; }}
.error {{ border-color: #b42318; color: #b42318; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #111; color: #eee; }}
  main {{ background: #181818; }}
  p {{ color: #bbb; }}
  button {{ background: #eee; color: #111; border-color: #eee; }}
}}
</style>
</head>
<body>
<main>
<h1>ARL-5 Simplified</h1>
<p>Sign in with Pocket ID to view the sizing monitor.</p>
{status}
<form method="post" action="/auth/oidc/start">
<input type="hidden" name="return_to" value="{safe_return}">
<button type="submit">Continue with Pocket ID</button>
</form>
</main>
</body>
</html>"""


async def _urlencoded_form(request: Request) -> dict[str, list[str]]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise InvalidAuthRequest("Unsupported form submission")
    raw = await request.body()
    if len(raw) > 16_384:
        raise InvalidAuthRequest("Form submission is too large")
    try:
        return parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError as exc:
        raise InvalidAuthRequest("Form submission is invalid") from exc


def _one(values: dict[str, list[str]], name: str) -> str | None:
    found = values.get(name, [])
    if len(found) > 1:
        raise InvalidAuthRequest(f"Duplicate {name} value")
    return found[0] if found else None


def _query_values(request: Request) -> dict[str, list[str]]:
    return {key: request.query_params.getlist(key) for key in request.query_params}


def login_redirect(request: Request) -> RedirectResponse:
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    destination = quote(sanitize_return_to(path), safe="")
    return RedirectResponse(f"/login?return_to={destination}", status_code=303)


class AuthGuard:
    """Explicit dependencies distinguish user activity from passive polling."""

    def __init__(self, service: AuthService, *, user_activity: bool = False) -> None:
        self.service = service
        self.user_activity = user_activity

    async def __call__(self, request: Request) -> AuthSession:
        session = self.service.current_session(
            request.cookies.get(self.service.settings.cookie_name),
            user_activity=self.user_activity,
        )
        if session is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        return session


def build_auth_router(service: AuthService) -> APIRouter:
    router = APIRouter()

    @router.get("/login", response_class=HTMLResponse)
    async def login(request: Request, return_to: str | None = None) -> Response:
        destination = sanitize_return_to(return_to)
        if service.current_session(request.cookies.get(service.settings.cookie_name)) is not None:
            return RedirectResponse(destination, status_code=303)
        response = HTMLResponse(
            _login_page(
                destination,
                logged_out=request.query_params.get("logged_out") == "1",
            )
        )
        return _no_store(response)

    @router.post("/auth/oidc/start")
    async def oidc_start(request: Request) -> Response:
        try:
            form = await _urlencoded_form(request)
            redirect_url = await service.begin_login(_one(form, "return_to"))
        except AuthError as exc:
            return _no_store(HTMLResponse(_login_page("/", error=str(exc)), status_code=400))
        return _no_store(RedirectResponse(redirect_url, status_code=303))

    async def complete_login(request: Request, values: dict[str, list[str]]) -> Response:
        try:
            if _one(values, "error"):
                raise InvalidAuthRequest("Pocket ID did not complete the sign-in")
            result = await service.finish_login(
                code=_one(values, "code"), state=_one(values, "state")
            )
        except AuthError as exc:
            return _no_store(HTMLResponse(_login_page("/", error=str(exc)), status_code=400))
        response = RedirectResponse(result.return_to, status_code=303)
        response.set_cookie(
            service.settings.cookie_name,
            result.session_token,
            max_age=service.settings.absolute_timeout_seconds,
            secure=not service.settings.allow_insecure_http,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return _no_store(response)

    @router.get("/auth/oidc/callback")
    async def oidc_callback_get(request: Request) -> Response:
        return await complete_login(request, _query_values(request))

    @router.post("/auth/oidc/callback")
    async def oidc_callback_post(request: Request) -> Response:
        try:
            form = await _urlencoded_form(request)
        except AuthError as exc:
            return _no_store(HTMLResponse(_login_page("/", error=str(exc)), status_code=400))
        return await complete_login(request, form)

    @router.post("/auth/logout")
    async def logout(request: Request) -> Response:
        raw_session = request.cookies.get(service.settings.cookie_name)
        try:
            result = await service.begin_logout(raw_session)
            response: Response = RedirectResponse(result.redirect_url, status_code=303)
        except Unauthenticated:
            response = RedirectResponse("/login?logged_out=1", status_code=303)
        except AuthError as exc:
            response = _no_store(HTMLResponse(_login_page("/", error=str(exc)), status_code=502))
        response.delete_cookie(
            service.settings.cookie_name,
            path="/",
            secure=not service.settings.allow_insecure_http,
            httponly=True,
            samesite="lax",
        )
        return _no_store(response)

    @router.get("/auth/oidc/logout/callback")
    async def logout_callback(request: Request) -> Response:
        try:
            service.finish_logout(_one(_query_values(request), "state"))
        except AuthError as exc:
            return _no_store(HTMLResponse(_login_page("/", error=str(exc)), status_code=400))
        return _no_store(RedirectResponse("/login?logged_out=1", status_code=303))

    return router
