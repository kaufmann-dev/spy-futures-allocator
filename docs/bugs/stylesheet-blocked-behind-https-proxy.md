# Stylesheet blocked behind HTTPS proxy

- Fixed: 2026-08-12 12:36:47 UTC (+0000)
- Starting commit: `c20683cc24dd3b8156fe4aad508741a62f68a8b3`

## Symptom

The deployed HTTPS dashboard rendered without CSS. The browser reported that loading
`http://arl5-simplified.kaufmann.dev/static/app.css` violated the `style-src 'self'` Content
Security Policy.

## Confirmed root cause

The base template generated an absolute static-asset URL with FastAPI's request-aware `url_for`.
Coolify terminates HTTPS before proxying the request to the application over HTTP, so the URL was
rendered with an `http` scheme. On the HTTPS page that is a different origin, and the restrictive
CSP correctly blocked it.

## Fix

The stylesheet now uses the origin-relative `/static/app.css` URL. This keeps the same-origin CSP
protection while making the asset URL independent of the reverse proxy's internal transport
scheme. The template regression test asserts that the stylesheet URL is relative and that no HTTP
URL is rendered.
