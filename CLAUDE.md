# Chess Library API

Flask + Postgres backend **and, as of the single-game-sharing feature, the entire
`library.chessscenes.com` site** — this is no longer just an API. Deployed on Railway.

**There used to be a separate `chess-library` repo/GitHub Pages site for the frontend.
That's been retired and consolidated into this repo (`static/`).** The reason: GitHub
Pages can only serve pre-existing static files — every URL returned byte-identical HTML
with one fixed `<meta og:image>`/`og:title`, no matter what. That was fine until sharing
a single game needed a real, distinct WhatsApp preview per shared game (actual board
position, real player names) — which is fundamentally impossible on a host that can't
run code per request. Rather than work around that with increasingly awkward tricks (an
earlier draft of this feature tried publishing static files into the frontend's GitHub
repo via commits on every share — rejected for treating a public source repo as a
runtime database), the frontend was moved here, onto a host that can actually render
dynamic content, and the domain now points at this Railway service instead of GitHub
Pages. See "Static frontend + single-game sharing" below for the full design.

Supports two independent library storage models side by side:

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
- `POST /api/auth/signup` — body `{username, password, email?}` → `{token, username}`
  (201). Username must match `^[A-Za-z0-9_.\-]{3,32}$`; password 8–128 chars. Username
  uniqueness is case-insensitive (`LOWER(username)` unique index) but original casing is
  preserved for display. `email` is optional (validated with a loose `EMAIL_RE` if
  given) — it's what forgot-password sends a reset link to; an account with no email on
  file simply can't self-serve a reset until one is added via `PUT /api/auth/email`.
  409 if username taken, 400 for validation failures.
- `POST /api/auth/login` — body `{username, password}` → `{token, username}` (200) or 401.
  Username lookup is case-insensitive.
- `POST /api/auth/logout` — header `Authorization: Bearer <token>` → 200. Invalidates
  only that one session token; other active sessions for the same account are untouched
  (e.g. logging out on one device doesn't log you out everywhere).
- `GET /api/auth/me` — header `Authorization: Bearer <token>` → `{username, email}` (200)
  or 401. Used by the frontend on page load to silently check whether a stored token is
  still valid, and to populate the account panel's recovery-email field.
- `PUT /api/auth/email` — header `Authorization: Bearer <token>` required, body
  `{email}` → `{email}` (200). Adds/changes/clears (empty string) the logged-in
  account's recovery email. Surfaced in the frontend's account panel, mainly so
  pre-existing accounts (created before email existed, or via a bare-username signup)
  can still opt into forgot-password later.
- `POST /api/auth/google` — body `{credential}` (a Google ID token) → `{token, username}`.
  Returns 503 while `GOOGLE_CLIENT_ID` is unset (see below) — this is deliberate graceful
  degradation, not an error state, since the frontend's Google button stays hidden until
  this is configured. **Frontend wiring is now complete** (added alongside forgot-password,
  see below): on load the page calls `GET /api/config`, and only if `googleClientId` is
  non-empty does it lazy-load `https://accounts.google.com/gsi/client`, `initialize()`,
  and `renderButton()` into `#googleSignInBtn`, unhiding `#authGoogleWrap`. So the button
  will start actually appearing the moment `GOOGLE_CLIENT_ID` is set on Railway — no
  further frontend work needed for that.
- `POST /api/auth/forgot-password` — body `{username}` → `{message}` (**always 200**,
  even for an unknown username or one with no email on file) — deliberately generic so
  this endpoint can't be used to enumerate registered usernames. If the username exists
  and has an email on file: any earlier unused reset token for that account is deleted
  (one live link at a time), a new single-use token is generated
  (`secrets.token_urlsafe(32)`, `RESET_TOKEN_TTL_MINUTES` = 60min expiry, stored in
  `password_resets`/`password_resets_dev`), and an email is sent via `send_email()`
  pointing at `{SHARE_HOST}/?resetToken=<token>` (or `/dev/?resetToken=...`).
- `POST /api/auth/reset-password` — body `{token, password}` → `{status: "ok"}` (200) or
  400 (`"This reset link is invalid or has expired"` for an unknown/expired/already-used
  token). On success: updates `password_hash`, deletes the token (single-use), and
  **deletes every session for that account** — same rationale as most account systems,
  a reset is often triggered because the old password may be compromised, so old
  sessions shouldn't survive it. The frontend's reset-password modal auto-opens when the
  page loads with a `?resetToken=` query param (the emailed link), and reopens the
  login modal on success so the user immediately signs in with the new password.
- `GET /api/library/mine` / `PUT /api/library/mine` — header `Authorization: Bearer
  <token>` required. One library per account (`UNIQUE` on `user_id`, upserted via
  `ON CONFLICT (user_id) DO UPDATE`). Same `{playerName, pgn}` shape as the anonymous
  endpoints, for the same reason (frontend sends rebuilt PGN text, not its internal
  object shape).

Config:
- `GET /api/config` — `{googleClientId}` (empty string if unset). Not env-suffixed
  (prod/dev) like the auth/library routes — `GOOGLE_CLIENT_ID` is one Google Cloud OAuth
  client shared by both, same as `SHARE_HOST`. Purely a frontend convenience so
  `static/index.html` can decide at load time whether to show the Google Sign-In button,
  without needing a templated/rendered index page just for one flag.

Health:
- `GET /api/health` — `{"status": "ok"}`, plain-browser-visitable, no special headers
  needed (this is just a normal GET, CORS only restricts cross-origin JS `fetch`, not a
  direct browser visit).

Single-game sharing (see the full section below for design rationale):
- `POST /api/games/share` — body `{playerName, pgn}` for **one game**. Reuses the same
  `libraries` table as the anonymous whole-library flow — just a smaller row. Its own
  64KB size cap (a lot smaller than the 5MB whole-library cap) and a per-IP daily rate
  limit backed by the `share_events` table. Returns `{id, url}`.
- `GET /g/<id>` (and `/dev/g/<id>`) — renders `static/index.html` (or `static/dev/
  index.html`) per request, with `<title>`/`og:*`/`twitter:*` tags swapped in for that
  specific game (escaped — see below). This **is** the real app, not a redirect or a
  stripped viewer; the frontend's `init()` detects this path and loads just that one game.
- `GET /g/<id>/preview.png` (and `/dev/g/<id>/preview.png`) — a board-position PNG,
  rendered on demand (not stored) from the game's PGN, halfway through the mainline.

## Static frontend + single-game sharing

**Serving** (new `static/` folder, moved here from the old `chess-library` repo):
- `GET /` → `static/index.html`, `GET /dev/` → `static/dev/index.html` — byte-identical
  to what GitHub Pages used to serve. `Cache-Control: no-cache` so a push goes live
  immediately, matching the old behavior.
- `GET /og-image.png`, `GET /logos/<file>`, `GET /amsterdam_games.pgn` — static assets,
  served via `send_from_directory`.
- The frontend's `API_BASE` is now `""` (same-origin), not a hardcoded cross-origin
  Railway URL — frontend and API are the same process now, so there's nothing to CORS
  around for the app's own traffic. `CORS(...)` is still scoped to `/api/*` only; it's
  irrelevant to the page-serving routes (top-level navigation isn't subject to CORS).

**The `/g/<id>` page** is rendered by string-substituting the *stable* part of each meta
tag (via `_replace_tag`, a small regex-with-function-replacement helper — deliberately
**not** a plain-string `re.sub` replacement, which would misinterpret a literal backslash
in attacker-controlled header text as a regex backreference). Every interpolated value
(White/Black/Event/Date/Result from the PGN headers) is run through `html.escape()` and
truncated to 200 chars first.

**This escaping is deliberate and contradicts this project's own existing convention** —
`chess-library`'s old CLAUDE.md (and the comments still in the frontend) say `g.pgn` must
stay raw, unescaped text, and that rule is *correct* for its context (PGN text used for
clipboard/export/Lichess-import, never inserted into `innerHTML`). A PGN header being
spliced into a **served HTML document's attributes** here is a different sink entirely —
unauthenticated, attacker-controllable text going straight into markup every real visitor
gets served, not just crawlers. Getting this backwards here is a real HTML-injection bug,
not a style nit. If you're touching `share_page`/`build_share_meta`/`inject_share_meta`,
keep the escaping; don't "fix" it to match the PGN-export convention.

**Preview image rendering** is pure Pillow, not a live SVG-to-PNG pipeline. The frontend's
board widget already has its own piece set (`PIECE_SVG` in `static/index.html`, 12
cburnett-style SVGs). Rather than parse/rasterize SVG at request time — `svglib` (the
obvious pure-Python choice) has known gaps resolving this exact SVG dialect's `href`
gradient shorthand, which fails *silently* as flat/wrong-colored pieces rather than an
error — the 12 pieces were rasterized to PNG **once, offline** (via a headless-browser
screenshot of each `PIECE_SVG` data URI) and committed as `static/pieces/{w,b}{K,Q,R,B,N,
P}.png`. `render_board_png()` just draws the grid (`BOARD_LIGHT_RGB`/`BOARD_DARK_RGB`,
matching `.cw-sq.light`/`.cw-sq.dark` in the live widget exactly) and pastes the
pre-rendered pieces. If the piece set ever changes in the frontend, the 12 PNGs need
re-rasterizing to match — they will not update themselves.

**`load_piece_images()` runs at import time and must never raise** — an exception there
would take down the *entire* app (every already-working endpoint) at startup, not just
the preview-image feature. It's wrapped in `try/except`; on failure `PIECE_IMAGES` stays
empty and `share_preview_png()` returns a 503 for that one route instead. Keep this
defensive shape if you touch it — the blast radius of "the new sharing feature has a bug"
should never be "the whole site is down."

**Rate limiting** (`share_events` table, checked in `create_game_share`): per-IP, backed
by Postgres rather than an in-process counter — `Procfile` runs `gunicorn app:app` with
no explicit worker count, so an in-memory counter isn't safe against that changing later.
The blast radius of spamming this endpoint is "extra DB rows" (same as the pre-existing
anonymous library-save endpoint's already-accepted risk), not anything more serious —
there's no GitHub token, no git commits, no public exposure beyond what any anonymous
library link already has. Client IP is read from `X-Forwarded-For` first (Railway sits
behind a proxy), falling back to `request.remote_addr`.

**Known pending step**: `SHARE_HOST` is hardcoded to `https://library.chessscenes.com` —
correct once DNS points here, but **the domain hasn't been cut over yet** as of this
writing (still resolves to the old GitHub Pages site). Until that happens, share links
generated by this service point at a domain that isn't serving this content — sharing
only actually works end-to-end via the Railway-provided URL directly. Don't be surprised
if a share link "doesn't work" for someone before the DNS/custom-domain step (see
`README`/conversation history for exact cutover steps) is done.

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
  read/write the one shared library row for that account. This is the actual mechanism
  behind "cross-device support" — there's nothing further to build for that, it's just
  what having an account already gets you (the landing page's hero copy says this
  explicitly: "your library syncs to your account so you can pick up where you left off
  on any device").
- **Forgot-password reset tokens are a separate, deliberately weaker-lived credential
  than session tokens** — also `secrets.token_urlsafe`, but 60-minute expiry and
  single-use (deleted on redemption, and any earlier unused one is deleted the moment a
  new reset is requested). Stored in their own table rather than reusing `sessions`,
  since a reset token authorizes one specific action (setting a new password), not
  general API access.
- **`forgot_password()` never reveals whether a username exists or has an email on
  file** — it always returns the same 200 message. Don't "improve" this by returning a
  different message/status for "no such user" vs "no email on file"; that reintroduces
  the enumeration hole it's specifically designed to close.
- **`/api/auth/forgot-password` is rate limited**, same pattern as `/api/games/share`
  but its own table (`password_reset_events`, not env-suffixed - an abuse counter, not
  user data, same reasoning as `share_events`) and a much lower cap
  (`FORGOT_PASSWORD_RATE_LIMIT_PER_DAY` = 5/IP/day vs share's 50) — the blast radius of
  abuse here is mail-bombing a real inbox, not just extra DB rows. A row is inserted for
  *every* request that reaches the check, before the username lookup, so it can't be
  bypassed by trying many different usernames from one IP.

## Schema

- `libraries` / `libraries_dev` — unchanged: `(id TEXT PRIMARY KEY, player_name TEXT,
  pgn TEXT, created_at TIMESTAMPTZ DEFAULT now())`.
- `users` / `users_dev` — `(id SERIAL PRIMARY KEY, username TEXT, password_hash TEXT,
  created_at TIMESTAMPTZ DEFAULT now())` plus a functional unique index on
  `LOWER(username)` for case-insensitive uniqueness while preserving display casing.
  `email` is nullable — always set for Google accounts, optional for password accounts
  (collected at signup or added later via `PUT /api/auth/email`); forgot-password
  requires it to be non-null to do anything.
- `sessions` / `sessions_dev` — `(token TEXT PRIMARY KEY, user_id INTEGER, created_at
  TIMESTAMPTZ DEFAULT now())`.
- `password_resets` / `password_resets_dev` — `(token TEXT PRIMARY KEY, user_id TEXT NOT
  NULL REFERENCES users(id) ON DELETE CASCADE, expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now())`, indexed on `user_id`. A row's existence past
  `expires_at`, or past being redeemed, doesn't mean anything — `reset_password()` checks
  `expires_at > now()` and deletes the row on redemption; nothing currently sweeps
  expired-but-never-redeemed rows, but the table stays tiny at this project's scale so
  that hasn't mattered.
- A `libraries`/`libraries_dev` row can now optionally carry a `user_id` (nullable,
  `UNIQUE`) linking it to an account. Existing anonymous rows have `user_id IS NULL`,
  which Postgres allows any number of under a unique index/constraint — they don't
  conflict with each other or with the new per-account uniqueness rule.
- `init_db()` runs on every deploy/restart (Railway does this automatically) and must
  stay idempotent — every new statement uses `IF NOT EXISTS` / `ADD COLUMN IF NOT
  EXISTS`. Tested by running it three times in a row against an already-migrated,
  populated database with no errors and no data loss; keep that property when touching
  this function.
- `share_events` — `(id SERIAL PRIMARY KEY, ip TEXT, created_at TIMESTAMPTZ DEFAULT
  now())`, an append-only log used only to compute a per-IP rolling-24h count for the
  share endpoint's rate limit. Not env-suffixed (shared across prod/dev) — it's an abuse
  counter, not user data, doesn't need environment isolation.
- `password_reset_events` — same shape and same rationale as `share_events` above, just
  for `/api/auth/forgot-password`'s own (much lower) rate limit.

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
  `.split(",")` in `app.py`, passed to `flask_cors.CORS` as a list). Now that the
  frontend is served by this same service (`API_BASE=""`, same-origin), CORS mostly
  matters for any *other* origin calling this API, not the app's own traffic. Currently
  `https://harman28.github.io,https://library.chessscenes.com` — the `github.io` entry
  is now vestigial (that site no longer exists post-consolidation) and can be dropped
  whenever, no urgency.
- `GOOGLE_CLIENT_ID` — **not yet set**. Required for `/api/auth/google` to work; until
  it's provisioned, that endpoint returns 503 and the frontend keeps its Google button
  hidden (via `GET /api/config` returning an empty `googleClientId`). This has to come
  from the user via Google Cloud Console (create an OAuth 2.0 Client ID, web application
  type, with `https://library.chessscenes.com` as an authorized JavaScript origin) — not
  something obtainable programmatically. **Unlike before, the frontend side is now fully
  built and just waiting on this value** — no further code changes needed once it's set,
  the button will start rendering itself on the next page load.
- `SMTP_HOST` / `SMTP_PORT` (default `587`) / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM`
  — **not yet set**. Required for forgot-password emails to actually be delivered; until
  configured, `send_email()` logs the would-be email (including the real reset link) to
  stdout instead of sending it — same graceful-degradation pattern as
  `GOOGLE_CLIENT_ID`, so the whole forgot/reset-password flow is fully exercisable in
  dev/staging (read the reset link out of the Railway logs) before real SMTP credentials
  exist. Any standard SMTP-with-STARTTLS provider works (e.g. an app password from a
  personal Gmail account, or a transactional-email provider's SMTP endpoint) — this was
  deliberately kept to Python's stdlib `smtplib` rather than adding a provider-specific
  SDK dependency, same instinct as the `pbkdf2:sha256`/pure-Python-deps choices below.

## Local testing

A local Postgres is available (`psql`, `pg_ctl` via Homebrew, `postgresql@17`). Pattern
used throughout development: `createdb <tempname>`, set `DATABASE_URL=postgresql:///
<tempname>`, exercise the Flask app via its test client (`app.test_client()`) or real
`curl`/`requests` calls, then `dropdb <tempname>` when done. Always test against a real
Postgres, not a mock — this project has consistently verified against the real deployed
API (including live CORS preflight simulation via `curl -X OPTIONS`) before shipping
frontend changes that depend on it, not just local mocks.

## Deployment

Railway auto-deploys the `web` service on push to `main` (connected via "Deploy from
GitHub repo"). Verification after deploy is done via `curl` against the live Railway URL
(`https://chess-library-api.up.railway.app`), not by assuming a push succeeded.

**Railway CLI is now available and usable from a session** (as of 2026-08-18 — this was
previously undocumented/unused, unlike e.g. `zoosnap`'s CLAUDE.md which records egress to
`railway.app` as blocked by default; that restriction does not apply in this repo's
environment, confirmed by a successful `railway login --browserless`). `npm install -g
@railway/cli`, then `railway login --browserless` prints a one-click link + short code for
the user to approve from their own browser/phone. Once authenticated, prefer the CLI's
`--json`-flagged, non-interactive forms (`railway list --json`, `railway variable set
KEY=VAL --service <name> --json`, `railway add --repo owner/repo --branch <branch>
--service <name> --json`, etc.) over asking the user to click through the dashboard —
`railway link -p <projectId> -e <env> -s <service> --json` links non-interactively without
a picker. Project id for this repo: `9e4b43b3-48c7-4d9e-afa1-d29c94143c80`, production
environment id: `e31642c4-7afd-4cf1-a924-e91de49037b9`.

**Custom domain**: `library.chessscenes.com` needs to be added as a custom domain in
Railway's dashboard, and its DNS record (currently a CNAME to `harman28.github.io`,
managed at Namecheap — confirmed via `whois`/`dig`) repointed at the CNAME target Railway
provides. Both of those are manual, credential-gated steps only the user can do. Until
that's done, this Railway service is fully built/tested/live at its own `.up.railway.app`
URL, but the real domain still serves the old (now-stale) GitHub Pages content. Once DNS
is cut over and verified, disable GitHub Pages on the old `chess-library` repo and archive
that repo — this one is now the sole source of truth for both the API and the site.

## Staging environment

**A second Railway service, `chess-library-api-staging`, exists in the same Railway
project** (id `624a762e-56b9-4858-b624-fe1bc0aeaa88`), set up 2026-08-18 specifically so
changes can be reviewed on a live URL before they touch the real site — this repo
previously had no way to do that (see the old "Prod/dev split" caveat below: `/dev/` is
just a *path* on the one production deployment, so before this it was impossible to
preview anything without also shipping it to real users). Mirrors the
`chessscenes-rebuild` / `zoosnap-staging` pattern used in this user's other projects, one
Railway service ≈ one deploy target, adapted to this repo not having a git-branch-per-env
split before now.

- **Deploys from the `staging` branch**, not `main` — connected via `railway add --repo
  harman28/chess-library-api --branch staging --service chess-library-api-staging`. A
  push to `staging` deploys only this service; `main` is untouched and keeps deploying only
  to the real `web` service as before.
- **Domain**: `https://chess-library-api-staging-production.up.railway.app` (the
  `-production` in the hostname is just this project's one Railway *environment* being
  named `production` — cosmetic, not worth an environment rename to fix, unlike the
  `zoosnap` project where a similar naming confusion did get fixed by renaming the
  environment; this project only has the one environment either way, so there's no
  cross-environment confusion to resolve).
- **Shares the same Postgres as prod** (`DATABASE_URL` set to the reference
  `${{Postgres.DATABASE_URL}}`, same as the real `web` service) — deliberately, per
  explicit choice over provisioning a second database: this app's own prod/dev split is
  already by table suffix (see "Prod/dev split" below), so `/dev/` on this staging service
  hits the exact same `_dev`-suffixed tables that `/dev/` on the real site already writes
  to. **The sharp edge this creates: this staging service's own bare `/` path (not
  `/dev/`) reads/writes the exact same tables real users' production data lives in.**
  Always test against this service's `/dev/` path, never its `/`, for anything that writes
  data — same discipline as the real site, just doubly important here since there's no
  separate database to fall back on as a safety net.
  `ALLOWED_ORIGIN` was copied verbatim from the `web` service.
- **`SHARE_HOST` is a hardcoded constant** (`app.py`, `https://library.chessscenes.com`) —
  a share link generated by hitting this staging service's `/api/games/share` will still
  read `library.chessscenes.com/g/<id>` in the response, not the staging domain. The share
  *feature* (POST succeeds, row created, `GET /g/<id>` renders correctly) is still fully
  testable here — just visit the staging domain's own `/g/<id>` manually rather than
  trusting the returned link text to point back at staging.
- **Workflow**: branch off `main` → merge into `staging` → push → review at the staging
  domain (desktop and real mobile emulation per the frontend's own testing convention) →
  once approved, merge `staging` into `main` → push → deploys to the real site. `staging`
  should generally track `main` (fast-forward it after every prod promotion) so it doesn't
  drift into being its own permanent fork.

## Design decisions worth preserving

- Stack (Flask + Postgres) was chosen to match the user's existing familiarity from
  another Railway project (`chessscenes`, which uses Flask + SQLite) — Postgres instead
  of SQLite specifically because that project's SQLite file is committed straight into
  git (fine for scraped/rebuildable data), which would silently lose every runtime write
  on this project's next deploy. Don't switch this back to SQLite-in-repo.
- IDs are `secrets.token_urlsafe(18)` → 24 url-safe chars, ~144 bits of entropy —
  deliberately longer than Lichess's 8-char game ids since this protects a whole library,
  not one game.
- `python-chess` and `Pillow` (for single-game sharing) are both pure Python — no C
  extension for `python-chess`, and Pillow ships prebuilt wheels bundling its own
  libjpeg/zlib. Same instinct as pinning `pbkdf2:sha256` over `scrypt`: prefer
  dependencies that can't fail because Railway's build environment doesn't happen to
  have some particular system library installed. Don't reach for `cairosvg`/`svglib` or
  anything else that needs a live SVG renderer for this feature — see the sharing
  section above for why that's a real, previously-identified failure mode here
  specifically, not just generic caution.
