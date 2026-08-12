# OIDC redirect blocked by form-action CSP

- Fixed: 2026-08-12 12:23:01 UTC (+0000)
- Starting commit: `c3c0ed009a594bd50cd91d89f84b3f6fe41bcb43`

## Symptom

Submitting “Continue with Pocket ID” posted successfully to `/auth/oidc/start`, but Chromium stayed on the local login page and never requested the Pocket ID authorization URL. A direct HTTP client still observed the expected `303` response.

## Confirmed root cause

The login response declared `form-action 'self'`. Chromium applies `form-action` to redirected form-submission destinations, so it blocked the local POST's redirect to the external OIDC issuer. The browser network log showed only the same-origin POST, while the same POST made with `curl` returned a `303` whose location was the configured Pocket ID issuer.

## Fix

The authentication router now derives the origin from the validated OIDC issuer URL and adds only that origin to `form-action` alongside `'self'`. The policy remains restrictive and does not allow arbitrary external form targets. A server test asserts that the configured issuer origin is present in the login CSP.
