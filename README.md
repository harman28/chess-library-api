# Chess Library API

Flask + Postgres backend that also serves the entire `library.chessscenes.com` site
(frontend lives in `static/index.html`, no separate build step). Deployed on Railway.

See `CLAUDE.md` for the full design/architecture writeup (endpoints, schema, auth
design, why the frontend moved here, etc). This file is just what's needed to run it
locally and deploy it.

## Local development

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DATABASE_URL=postgresql:///chesslibrary ALLOWED_ORIGIN=* python3 app.py
```

Needs a real local Postgres (`createdb chesslibrary` first) — `init_db()` creates all
tables/indexes idempotently on startup. There's no SQLite fallback.

Visit `http://localhost:5000/`. There's no `/dev/` path or table-suffix split within
the process anymore — see "Prod/dev split" below for why.

## Deployment (Railway)

One Railway project, **`chess-library-api`** (id `9e4b43b3-48c7-4d9e-afa1-d29c94143c80`),
single environment (`production`, id `e31642c4-7afd-4cf1-a924-e91de49037b9`), four services:

| Service | What | Deploys from | Domain |
|---|---|---|---|
| `web` | the real, live site | `main` branch | `library.chessscenes.com`, `chess-library-api.up.railway.app` |
| `chess-library-api-staging` | review environment before promoting to prod | `staging` branch | `library-staging.up.railway.app` |
| `Postgres` | prod's own dedicated DB | — | — |
| `Postgres-wpZy` | staging's own dedicated DB | — | — |

### Prod/dev split

**As of 2026-09-23, `web` and `chess-library-api-staging` each have their own
dedicated Postgres database** — they no longer share one. This replaced an earlier
design where both services pointed at the same Postgres, split only by a `/dev/` URL
path and `_dev`-suffixed tables; that meant staging's own bare `/` path silently
read/wrote real production data, and the only thing making staging safe to test in was
remembering to always use its uglier `/dev/` URL instead. With separate databases,
"which environment am I" is just "which database is this process pointed at" — every
route is now a single plain path (`/`, `/api/...`, `/g/<id>`), the same on every
deployment, and there's nothing unsafe about a service's own root URL. Table names are
correspondingly unsuffixed (`libraries`, `users`, `sessions`, no `_dev` variants).

The old shared Postgres (service `Postgres`, now `web`-only) still has orphaned
`libraries_dev`/`users_dev`/`sessions_dev` tables sitting in it from before this
change — harmless, nothing reads or writes them anymore, left in place rather than
dropped since dropping tables isn't a call to make silently.

### Workflow

Branch off `main` → do the work → merge into `staging` → push → review live at
`library-staging.up.railway.app` → once approved, merge `staging` into `main` → push →
deploys to the real site. Keep `staging` fast-forwarded to `main` after every promotion
so it doesn't drift into its own permanent fork.

### Getting Railway CLI access

```
npm install -g @railway/cli
railway login --browserless   # prints a link + code; approve from your own browser/phone
railway link -p 9e4b43b3-48c7-4d9e-afa1-d29c94143c80 -e e31642c4-7afd-4cf1-a924-e91de49037b9
```

Service IDs (for `--service` on `railway variable`, `railway redeploy`, etc — `railway
status --json` re-derives these if they ever drift):
- `web` (prod): `c36b79e8-78be-4be6-9ae9-51cd19295935`
- `chess-library-api-staging`: `624a762e-56b9-4858-b624-fe1bc0aeaa88`
- `Postgres` (prod's DB): `1ef63dbd-6eef-4697-8c77-3cbef78cbe13`
- `Postgres-wpZy` (staging's DB): `61107523-8529-4519-a891-7b3f5eb3ac80`

### Environment variables (set per-service on Railway, never committed)

Required:
- `DATABASE_URL` — auto-linked via a Railway variable reference to that service's own
  Postgres (`${{Postgres.DATABASE_URL}}` on `web`, `${{Postgres-wpZy.DATABASE_URL}}` on
  staging) — never hardcoded, and never the same reference on both services anymore.
- `ALLOWED_ORIGIN` — comma-separated CORS allowlist for `/api/*`.

Optional:
- `GOOGLE_CLIENT_ID` — enables Google Sign-In. From Google Cloud Console → APIs &
  Credentials → OAuth client ID (Web application), with the site's real domain(s) as
  authorized JavaScript origins. Only the Client ID is needed server-side, no secret.
  The frontend doesn't actually load Google's Identity Services script yet regardless
  of whether this is set — see `static/CLAUDE.md`.
- `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` — for sending
  real emails (e.g. a password-reset flow), if one gets built.
  **Important, learned the hard way**: Railway blocks outbound SMTP entirely (confirmed
  by testing live — an IPv6-routing error, and after forcing IPv4, a silent connection
  timeout on port 587 to Gmail). No amount of code-level fixing gets around a
  platform-level port block. If email sending is ever needed from this app, use an
  HTTP-based transactional email API instead (e.g. Resend, SendGrid, Postmark) — regular
  HTTPS/443 egress works fine from Railway, only raw SMTP ports are blocked.

### Deploying

Railway auto-deploys `web` on push to `main`, and the staging service on push to
`staging` (both connected via "Deploy from GitHub repo"). Verify a deploy actually
landed with `curl` against the live URL rather than assuming a push succeeded —
`railway logs --service <id>` and `railway status --json` (checks running deployment's
commit hash against `git log`) are the fastest ways to confirm.
