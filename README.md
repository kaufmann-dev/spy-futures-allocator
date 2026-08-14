# SPY Futures Allocator

SPY Futures Allocator is a single-user, read-only web application that turns a deterministic SPY signal into an ES/MES futures allocation. It uses manually configured strategy capital and signed current ES/MES quantities; it never reads an IBKR account or places, modifies, approves, or cancels orders.

The application shows the current and pending exposure targets, a non-overshooting ES/MES allocation, position differences, strategy state, signal history, and historical SPY-based charts. Recommendations are notional sizing only. They do not account for margin requirements, financing, futures basis and rolls, fees, slippage, taxes, or liquidation risk.

## Strategy

Signals use completed, regular-session, dividend- and split-adjusted SPY closes requested from IBKR as `ADJUSTED_LAST` bars. The application requests up to 40 years of history and fails closed when the configured lookback is unavailable.

The default strategy configuration is:

- fast and slow moving averages: 100 and 500 sessions;
- rebound threshold: 25% from the lowest post-exit close;
- realized-volatility window and annualization: 20 sessions and 252;
- annualized realized-volatility threshold: 25%;
- normal-volatility exposure: 1.60x;
- high-volatility exposure: 0.60x;
- exposure review closes: Monday and Thursday.

At the first session with enough history, the regime starts risk-on when the fast SMA is at or above the slow SMA and risk-off otherwise. A later transition from fast SMA at or above slow SMA to fast SMA below slow SMA exits to 0.00x exposure at the next actual XNYS session open.

After an exit, the crossover close initializes the trough and every later lower close updates it. A close at or above `trough × (1 + rebound fraction)` re-enters at the next actual XNYS session open. Re-entry evaluates realized volatility on that close even when it is not a configured review weekday. Another bearish exit cannot occur until the fast SMA has first moved strictly above the slow SMA.

During an ordinary risk-on period, the volatility bucket is reviewed only on configured weekday closes. A holiday does not move a missed review to another day. Annualized realized volatility is the sample standard deviation of daily SPY returns over the configured window multiplied by the square root of the annualization factor. Values below the threshold select normal exposure; values at or above it select high-volatility exposure. Risk-off always selects zero exposure. There is no built-in maximum-exposure cap.

Strategy parameters, strategy capital, signed current ES/MES quantities, and the IBKR connection are configured in the website. Resetting strategy defaults restores only the strategy parameters; it preserves capital, current quantities, and IBKR settings. Saving either action immediately replays available history under the selected configuration.

## Futures allocation

The target notional is `strategy capital × target exposure`. The allocator resolves same-expiry quarterly ES and MES contracts with the expected multipliers of 50 and 5, then:

1. floors the number of ES contracts against the target notional;
2. floors the number of MES contracts against the remaining notional;
3. verifies that achieved notional and exposure do not exceed the target.

Both contracts use the existing eight-calendar-day roll convention. A live allocation requires valid same-expiry contracts plus fresh, live quotes for both ES and MES. Delayed, frozen, stale, incomplete, or mismatched market data fails closed. The signal target may remain visible when quotes are unavailable, but no live contract allocation is published.

## Architecture

One FastAPI process serves the authenticated dashboard and API and runs the in-process market-data schedule. SQLite stores settings, adjusted SPY bars, ES/MES contracts and quotes, strategy configurations, replay snapshots, recommendations, sessions, and audit entries. Pocket ID provides OIDC authentication. TWS or IB Gateway is the only market-data integration.

There is one deployable application and one persistent SQLite volume. No PostgreSQL database, worker, or separate scheduler service is needed.

## Authentication setup

Create one confidential Pocket ID OIDC client and assign only the intended operator.

- Public Client: off
- Callback URL: `https://spy-futures-allocator.kaufmann.dev/auth/oidc/callback`
- Logout callback URL: `https://spy-futures-allocator.kaufmann.dev/auth/oidc/logout/callback`
- Flow: Authorization Code with PKCE S256
- Token endpoint authentication: `client_secret_post`

The app stores only an opaque local session cookie. Sessions use a 24-hour sliding idle timeout and a seven-day absolute limit; access and refresh tokens are never stored.

Generate the session and encryption secrets separately:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## Coolify Deployment

Connect Coolify to the private repository [kaufmann-dev/spy-futures-allocator](https://github.com/kaufmann-dev/spy-futures-allocator) and grant its GitHub integration access to that repository. Point the existing application at this new source, or create a replacement application, then use these Coolify settings:

- Build Pack: Nixpacks
- Base Directory: `/`
- Domain: `https://spy-futures-allocator.kaufmann.dev`
- Health Check Path: `/api/health`
- Instances: exactly one
- Persistent Storage: mount a persistent volume at `/data`

Required environment variables:

- `APP_ORIGIN` — public origin without a trailing slash; use `https://spy-futures-allocator.kaufmann.dev` in production.
- `OIDC_ISSUER_URL` — Pocket ID issuer URL without a trailing slash.
- `OIDC_CLIENT_ID` — confidential Pocket ID client ID.
- `OIDC_CLIENT_SECRET` — confidential Pocket ID client secret.
- `SESSION_SECRET` — independently generated random value of at least 32 characters.
- `SETTINGS_ENCRYPTION_KEY` — URL-safe base64 encoding of exactly 32 random bytes.
- `DATABASE_PATH` — use `/data/spy-futures-allocator.sqlite3`.

Optional environment variables:

- `SESSION_COOKIE_NAME` — defaults to `spy_futures_allocator_session`.
- `LOG_LEVEL` — defaults to `INFO`.

Do not configure pre- or post-deployment commands. The repository's `nixpacks.toml` supplies the `spy-futures-allocator` start command, the app binds to Coolify's injected `PORT`, and the SQLite schema is initialized at startup. After changing the source or domain, update the Pocket ID callback and logout callback URLs to the values in **Authentication setup**, then redeploy.

## Interactive Brokers

The website makes only read-only market-data requests through the official `ibapi` package:

- SPY contract details and completed regular-session `ADJUSTED_LAST` daily bars;
- active quarterly ES and MES contract details;
- live ES and MES bid, ask, last, timestamp, and market-data type.

In TWS or IB Gateway:

1. enable the socket API and Read-Only API;
2. allow the Coolify application to reach the API socket through a private network or tunnel;
3. configure an unused client ID in the website;
4. ensure the account has the required live CME ES/MES and SPY historical-data entitlements.

## Local development

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```bash
cp .env.example .env
uv sync --all-groups
uv run spy-futures-allocator
```

Fill every required value in `.env` before starting. Plain HTTP is accepted only when `APP_ORIGIN` uses `localhost` or `127.0.0.1`. Keep the SQLite file outside the repository or use the ignored local default.

Run the checks independently:

```bash
uv run mypy app tests
uv run ruff check app tests
uv run pytest
uv build
```

## API

`GET /api/health` is public. OIDC entry and callback routes are accessible as needed for authentication. All other endpoints require the local session; state-changing requests also require CSRF protection.

- `GET /api/current` — current and pending strategy state and ES/MES allocation.
- `GET /api/history` — persisted recommendation history.
- `PUT /api/settings` — update capital, current positions, IBKR settings, and strategy configuration.
- `POST /settings/reset-strategy` — restore strategy defaults while preserving capital, positions, and IBKR settings.

There are deliberately no order, execution, account, position, margin, or optimization endpoints.
