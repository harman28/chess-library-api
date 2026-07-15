# Chess Library API

Flask + Postgres backend for the `chess-library` frontend (separate repo — see its
CLAUDE.md for full context). Deployed on Railway. Supports two independent storage
models side by side:

1. **Anonymous unlisted-link libraries** (original design, kept for backward
   compatibility with links already shared) — same trust model as an unlisted Lichess
   game link: possession of the random id is the only access control. No login needed.
2. **Username/password (and optionally Google) accounts** — added later so a user can
   log in from any device/browser and have their library follow them, instead of having
   to save/paste a link. This is now the primary flow the frontend steers users toward;
   the anonymous link flow still works (old links, and anyone who doesn't want an
   account) but is no longer surfaced as the main "save" action in the UI.

Every table and route below is duplicated for **prod** and **dev**, both served by the
*same* Railway deployment — see "Prod/dev split" below before changing anything here.

## Endpoints

Anonymous libraries (unchanged):
- `POST /api/libraries` — body `{playerName, pgn}` → `{id}` (201). Validates non-empty
  fields and a 5MB payload cap (413 if exceeded).
- `GET /api/libraries/<id>` — `{playerName, pgn}` (200) or `{"error": "not found"}` (404).

Accounts:
- `POST /api/auth/signup` — body `{username, password}` → `{token, username}` (201).
  Username must match `^[A-Za-z0-9_.\-]{3,32}$`; password 8–128 chars. Username
  uniqueness is case-insensitive (`LOWER(username)` unique index) but original casing is
  preserved for display. 409 if taken, 400 for validation failures.
- `POST /api/auth/login` — body `{username, password}` → `{token, username}` (200) or 401.
  Username lookup is case-insensitive.
- `POST /api/auth/logout` — header `Authorization: Bearer <token>` → 200. Invalidates
  only that one session token; other active sessions for the same account are untouched
  (e.g. logging out on one device doesn't log you out everywhere).
- `GET /api/auth/me` — header `Authorization: Bearer <token>` → `{username}` (200) or 401.
  Used by the frontend on page load to silently check whether a stored token is still
  valid.
- `POST /api/auth/google` — body `{credential}` (a Google ID token) → `{token, username}`.
  Returns 503 while `GOOGLE_CLIENT_ID` is unset (see below) — this is deliberate graceful
  degradation, not an error state, since the frontend's Google button stays hidden until
  this is configured.
- `GET /api/library/mine` / `PUT /api/library/mine` — header `Authorization: Bearer
  <token>` required. One library per account (`UNIQUE` on `user_id`, upserted via
  `ON CONFLICT (user_id) DO UPDATE`). Same `{playerName, pgn}` shape as the anonymous
  endpoints, for the same reason (frontend sends rebuilt PGN text, not its internal
  object shape).

Health:
- `GET /api/health` — `{"status": "ok"}`, plain-browser-visitable, no special headers
  needed (this is just a normal GET, CORS only restricts cross-origin JS `fetch`, not a
  direct browser visit).

## Auth design notes

- **Bearer tokens, not cookies.** The frontend (`library.chessscenes.com`) and backend
  (`chess-library-api.up.railway.app`) are different origins; a cross-site cookie would
  need `SameSite=None; Secure` plus `credentials:'include'` on every fetch. A custom
  `Authorization` header is simpler and was chosen deliberately over cookie sessions.
- **Password hashing is pinned to `pbkdf2:sha256`**, not werkzeug's newer `scrypt`
  default — `generate_password_hash(pw, method="pbkdf2:sha256")`. `scrypt` needs a
  scrypt-capable OpenSSL build of `hashlib`, which isn't guaranteed everywhere (confirmed
  broken locally against a LibreSSL-linked Python 3.9). Don't drop this explicit method
  argument.
- **Session tokens** are `secrets.token_urlsafe(32)` (~256 bits), stored server-side in
  the sessions table and checked on every authenticated request — not a signed/stateless
  JWT. Logout deletes the one row; there's no expiry sweep yet.
- A user can be logged in from multiple sessions/devices simultaneously; all of them can
  read/write the one shared library row for that account.

## Schema

- `libraries` / `libraries_dev` — unchanged: `(id TEXT PRIMARY KEY, player_name TEXT,
  pgn TEXT, created_at TIMESTAMPTZ DEFAULT now())`.
- `users` / `users_dev` — `(id SERIAL PRIMARY KEY, username TEXT, password_hash TEXT,
  created_at TIMESTAMPTZ DEFAULT now())` plus a functional unique index on
  `LOWER(username)` for case-insensitive uniqueness while preserving display casing.
- `sessions` / `sessions_dev` — `(token TEXT PRIMARY KEY, user_id INTEGER, created_at
  TIMESTAMPTZ DEFAULT now())`.
- A `libraries`/`libraries_dev` row can now optionally carry a `user_id` (nullable,
  `UNIQUE`) linking it to an account. Existing anonymous rows have `user_id IS NULL`,
  which Postgres allows any number of under a unique index/constraint — they don't
  conflict with each other or with the new per-account uniqueness rule.
- `init_db()` runs on every deploy/restart (Railway does this automatically) and must
  stay idempotent — every new statement uses `IF NOT EXISTS` / `ADD COLUMN IF NOT
  EXISTS`. Tested by running it three times in a row against an already-migrated,
  populated database with no errors and no data loss; keep that property when touching
  this function.

## Prod/dev split

There is **one Railway deployment** serving both prod and dev traffic — the split is by
table name suffix (`_dev`) and URL path prefix (`/api/dev/...` vs `/api/...`), not by
separate services. This means:
- Every route above is registered twice (see `app.py`'s `*_prod`/`*_dev` wrapper
  functions calling the shared `signup()`/`login()`/etc. with `env="prod"` or `"dev"`).
- A push to `main` always touches the one live service that both the prod and staging
  frontends depend on — there's no way to deploy "just staging" at the backend level.
  New endpoints should stay additive; avoid changing the behavior of existing ones
  without checking both frontends.
- A dev-env session token is not valid against a prod-env endpoint and vice versa
  (verified by test) — the two environments are fully data-isolated even though they
  share a process and a Postgres instance.

## Environment variables (set on Railway)

- `DATABASE_URL` — Railway's managed Postgres, auto-linked via a variable reference
  (`${{Postgres.DATABASE_URL}}`), not something to hardcode.
- `ALLOWED_ORIGIN` — **comma-separated list** of allowed CORS origins (parsed via
  `.split(",")` in `app.py`, passed to `flask_cors.CORS` as a list). As of the custom
  domain migration, this should include both the old and new frontend origins, e.g.:
  `https://harman28.github.io,https://library.chessscenes.com`
  Update this whenever the frontend's origin changes (e.g., once the custom domain
  migration is fully cut over, the old github.io origin could eventually be dropped from
  this list, but there's no urgency — leaving both doesn't hurt).
- `GOOGLE_CLIENT_ID` — **not yet set**. Required for `/api/auth/google` to work; until
  it's provisioned, that endpoint returns 503 and the frontend keeps its Google button
  hidden. This has to come from the user via Google Cloud Console (create an OAuth 2.0
  Client ID, web application type, with `https://library.chessscenes.com` as an
  authorized JavaScript origin) — not something obtainable programmatically. Once set,
  the frontend also needs the same Client ID wired into its Google Identity Services
  initialization (currently not implemented, since there's nothing to configure it
  with yet).

## Local testing

A local Postgres is available (`psql`, `pg_ctl` via Homebrew, `postgresql@17`). Pattern
used throughout development: `createdb <tempname>`, set `DATABASE_URL=postgresql:///
<tempname>`, exercise the Flask app via its test client (`app.test_client()`) or real
`curl`/`requests` calls, then `dropdb <tempname>` when done. Always test against a real
Postgres, not a mock — this project has consistently verified against the real deployed
API (including live CORS preflight simulation via `curl -X OPTIONS`) before shipping
frontend changes that depend on it, not just local mocks.

## Deployment

Railway auto-deploys on push to `main` (connected via "Deploy from GitHub repo"). No
Railway CLI available locally as of this writing — env var changes and dashboard-level
config need the user to do them directly; verification after deploy is done via `curl`
against the live Railway URL (`https://chess-library-api.up.railway.app`), not by
assuming a push succeeded.

## Design decisions worth preserving

- Stack (Flask + Postgres) was chosen to match the user's existing familiarity from
  another Railway project (`chessscenes`, which uses Flask + SQLite) — Postgres instead
  of SQLite specifically because that project's SQLite file is committed straight into
  git (fine for scraped/rebuildable data), which would silently lose every runtime write
  on this project's next deploy. Don't switch this back to SQLite-in-repo.
- IDs are `secrets.token_urlsafe(18)` → 24 url-safe chars, ~144 bits of entropy —
  deliberately longer than Lichess's 8-char game ids since this protects a whole library,
  not one game.
