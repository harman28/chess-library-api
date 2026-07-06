# Chess Library API

Minimal Flask + Postgres backend supporting the "Save My Library" feature of the
`chess-library` frontend (separate repo — see its CLAUDE.md for full context). Deployed
on Railway. No accounts, no auth — this is an unlisted-link blob store, same trust model
as an unlisted Lichess game link: possession of the random id is the only access control.

## Endpoints

- `POST /api/libraries` — body `{playerName, pgn}` → `{id}` (201). Validates non-empty
  fields and a 5MB payload cap (413 if exceeded).
- `GET /api/libraries/<id>` — `{playerName, pgn}` (200) or `{"error": "not found"}` (404).
- `GET /api/health` — `{"status": "ok"}`, plain-browser-visitable, no special headers
  needed (this is just a normal GET, CORS only restricts cross-origin JS `fetch`, not a
  direct browser visit).

Schema: single table `libraries(id TEXT PRIMARY KEY, player_name TEXT, pgn TEXT,
created_at TIMESTAMPTZ DEFAULT now())`, created automatically on startup via `init_db()`.
The frontend stores the **rebuilt PGN text**, not its internal JS object shape — this is
deliberate, so a saved library's format doesn't drift out of sync if the frontend's
internal game-record shape changes later.

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
