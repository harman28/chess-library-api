import os
import re
import secrets

import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

MAX_PGN_BYTES = 5 * 1024 * 1024  # 5MB, generous headroom over real collections
ID_BYTES = 18  # secrets.token_urlsafe(18) -> 24 url-safe chars, ~144 bits of entropy
SESSION_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) -> ~43 url-safe chars, ~256 bits
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128

app = Flask(__name__)
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGIN", "*").split(",") if o.strip()]
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}})

DATABASE_URL = os.environ["DATABASE_URL"]
pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")


def get_conn():
    return pool.getconn()


def put_conn(conn):
    pool.putconn(conn)


LIBRARIES_TABLES = {"prod": "libraries", "dev": "libraries_dev"}
USERS_TABLES = {"prod": "users", "dev": "users_dev"}
SESSIONS_TABLES = {"prod": "sessions", "dev": "sessions_dev"}


def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for table in LIBRARIES_TABLES.values():
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id TEXT PRIMARY KEY,
                        player_name TEXT NOT NULL,
                        pgn TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
            for users_table in USERS_TABLES.values():
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {users_table} (
                        id TEXT PRIMARY KEY,
                        username TEXT NOT NULL,
                        password_hash TEXT,
                        google_sub TEXT UNIQUE,
                        email TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {users_table}_username_lower_idx ON {users_table} (LOWER(username))"
                )
            for env in ("prod", "dev"):
                sessions_table = SESSIONS_TABLES[env]
                users_table = USERS_TABLES[env]
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {sessions_table} (
                        token TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL REFERENCES {users_table}(id) ON DELETE CASCADE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                libraries_table = LIBRARIES_TABLES[env]
                cur.execute(
                    f"ALTER TABLE {libraries_table} ADD COLUMN IF NOT EXISTS user_id TEXT REFERENCES {users_table}(id) ON DELETE CASCADE"
                )
                cur.execute(
                    f"ALTER TABLE {libraries_table} ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                )
                cur.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {libraries_table}_user_id_unique ON {libraries_table} (user_id)"
                )
        conn.commit()
    finally:
        put_conn(conn)


# ---- anonymous link-based library (existing "Save My Library" flow; left in place, not used by new signups) ----


def create_library(table):
    data = request.get_json(silent=True) or {}
    player_name = (data.get("playerName") or "").strip()
    pgn = data.get("pgn") or ""

    if not player_name:
        return jsonify(error="playerName is required"), 400
    if not pgn.strip():
        return jsonify(error="pgn is required"), 400
    if len(pgn.encode("utf-8")) > MAX_PGN_BYTES:
        return jsonify(error="pgn is too large"), 413

    library_id = secrets.token_urlsafe(ID_BYTES)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {table} (id, player_name, pgn) VALUES (%s, %s, %s)",
                (library_id, player_name, pgn),
            )
        conn.commit()
    finally:
        put_conn(conn)

    return jsonify(id=library_id), 201


def get_library(table, library_id):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT player_name, pgn FROM {table} WHERE id = %s",
                (library_id,),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)

    if row is None:
        return jsonify(error="not found"), 404

    player_name, pgn = row
    return jsonify(playerName=player_name, pgn=pgn)


@app.post("/api/libraries")
def create_library_prod():
    return create_library(LIBRARIES_TABLES["prod"])


@app.get("/api/libraries/<library_id>")
def get_library_prod(library_id):
    return get_library(LIBRARIES_TABLES["prod"], library_id)


@app.post("/api/dev/libraries")
def create_library_dev():
    return create_library(LIBRARIES_TABLES["dev"])


@app.get("/api/dev/libraries/<library_id>")
def get_library_dev(library_id):
    return get_library(LIBRARIES_TABLES["dev"], library_id)


# ---- accounts ----


def get_user_from_request(env):
    """Returns {"id", "username"} for a valid Authorization: Bearer <token> header, else None."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer ") :].strip()
    if not token:
        return None
    users_table = USERS_TABLES[env]
    sessions_table = SESSIONS_TABLES[env]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT u.id, u.username FROM {sessions_table} s JOIN {users_table} u ON u.id = s.user_id WHERE s.token = %s",
                (token,),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if not row:
        return None
    return {"id": row[0], "username": row[1]}


def create_session(sessions_table, user_id):
    conn = get_conn()
    try:
        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO {sessions_table} (token, user_id) VALUES (%s, %s)", (token, user_id))
        conn.commit()
        return token
    finally:
        put_conn(conn)


def signup(env):
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not USERNAME_RE.match(username):
        return jsonify(error="Username must be 3-32 characters (letters, numbers, underscore, period, hyphen)"), 400
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify(error=f"Password must be at least {MIN_PASSWORD_LEN} characters"), 400
    if len(password) > MAX_PASSWORD_LEN:
        return jsonify(error="Password is too long"), 400

    users_table = USERS_TABLES[env]
    sessions_table = SESSIONS_TABLES[env]
    user_id = secrets.token_urlsafe(ID_BYTES)
    # Explicit pbkdf2 rather than werkzeug's newer scrypt default: scrypt needs
    # hashlib built against an OpenSSL with scrypt support, which isn't a safe
    # assumption across environments (hit this locally against LibreSSL).
    password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {users_table} WHERE LOWER(username) = LOWER(%s)", (username,))
            if cur.fetchone():
                return jsonify(error="That username is already taken"), 409
            cur.execute(
                f"INSERT INTO {users_table} (id, username, password_hash) VALUES (%s, %s, %s)",
                (user_id, username, password_hash),
            )
        conn.commit()
    finally:
        put_conn(conn)

    token = create_session(sessions_table, user_id)
    return jsonify(token=token, username=username), 201


def login(env):
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    users_table = USERS_TABLES[env]
    sessions_table = SESSIONS_TABLES[env]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, username, password_hash FROM {users_table} WHERE LOWER(username) = LOWER(%s)",
                (username,),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)

    if not row or not row[2] or not check_password_hash(row[2], password):
        return jsonify(error="Incorrect username or password"), 401

    user_id, real_username, _ = row
    token = create_session(sessions_table, user_id)
    return jsonify(token=token, username=real_username), 200


def logout(env):
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer ") :].strip()
        if token:
            sessions_table = SESSIONS_TABLES[env]
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {sessions_table} WHERE token = %s", (token,))
                conn.commit()
            finally:
                put_conn(conn)
    return jsonify(status="ok")


def me(env):
    user = get_user_from_request(env)
    if not user:
        return jsonify(error="not authenticated"), 401
    return jsonify(username=user["username"])


def google_login(env):
    if not GOOGLE_CLIENT_ID:
        return jsonify(error="Google sign-in is not configured on this server"), 503

    data = request.get_json(silent=True) or {}
    credential = data.get("credential") or ""
    if not credential:
        return jsonify(error="credential is required"), 400

    try:
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests

        idinfo = google_id_token.verify_oauth2_token(credential, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception:
        return jsonify(error="Invalid Google credential"), 401

    google_sub = idinfo.get("sub")
    if not google_sub:
        return jsonify(error="Invalid Google credential"), 401
    email = idinfo.get("email") or ""
    display_name = idinfo.get("name") or email or "Google User"

    users_table = USERS_TABLES[env]
    sessions_table = SESSIONS_TABLES[env]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT id, username FROM {users_table} WHERE google_sub = %s", (google_sub,))
            row = cur.fetchone()
            if row:
                user_id, username = row
            else:
                user_id = secrets.token_urlsafe(ID_BYTES)
                username = display_name
                suffix = 1
                while True:
                    cur.execute(f"SELECT 1 FROM {users_table} WHERE LOWER(username) = LOWER(%s)", (username,))
                    if not cur.fetchone():
                        break
                    suffix += 1
                    username = f"{display_name}{suffix}"
                cur.execute(
                    f"INSERT INTO {users_table} (id, username, google_sub, email) VALUES (%s, %s, %s, %s)",
                    (user_id, username, google_sub, email),
                )
        conn.commit()
    finally:
        put_conn(conn)

    token = create_session(sessions_table, user_id)
    return jsonify(token=token, username=username), 200


def get_my_library(env):
    user = get_user_from_request(env)
    if not user:
        return jsonify(error="not authenticated"), 401
    libraries_table = LIBRARIES_TABLES[env]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT player_name, pgn FROM {libraries_table} WHERE user_id = %s",
                (user["id"],),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if row is None:
        return jsonify(error="not found"), 404
    player_name, pgn = row
    return jsonify(playerName=player_name, pgn=pgn)


def put_my_library(env):
    user = get_user_from_request(env)
    if not user:
        return jsonify(error="not authenticated"), 401

    data = request.get_json(silent=True) or {}
    player_name = (data.get("playerName") or "").strip()
    pgn = data.get("pgn") or ""

    if len(pgn.encode("utf-8")) > MAX_PGN_BYTES:
        return jsonify(error="pgn is too large"), 413

    libraries_table = LIBRARIES_TABLES[env]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {libraries_table} (id, user_id, player_name, pgn, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (user_id) DO UPDATE SET player_name = EXCLUDED.player_name, pgn = EXCLUDED.pgn, updated_at = now()
                """,
                (secrets.token_urlsafe(ID_BYTES), user["id"], player_name, pgn),
            )
        conn.commit()
    finally:
        put_conn(conn)
    return jsonify(status="ok")


@app.post("/api/auth/signup")
def signup_prod():
    return signup("prod")


@app.post("/api/auth/login")
def login_prod():
    return login("prod")


@app.post("/api/auth/logout")
def logout_prod():
    return logout("prod")


@app.get("/api/auth/me")
def me_prod():
    return me("prod")


@app.post("/api/auth/google")
def google_login_prod():
    return google_login("prod")


@app.get("/api/library/mine")
def get_my_library_prod():
    return get_my_library("prod")


@app.put("/api/library/mine")
def put_my_library_prod():
    return put_my_library("prod")


@app.post("/api/dev/auth/signup")
def signup_dev():
    return signup("dev")


@app.post("/api/dev/auth/login")
def login_dev():
    return login("dev")


@app.post("/api/dev/auth/logout")
def logout_dev():
    return logout("dev")


@app.get("/api/dev/auth/me")
def me_dev():
    return me("dev")


@app.post("/api/dev/auth/google")
def google_login_dev():
    return google_login("dev")


@app.get("/api/dev/library/mine")
def get_my_library_dev():
    return get_my_library("dev")


@app.put("/api/dev/library/mine")
def put_my_library_dev():
    return put_my_library("dev")


@app.get("/api/health")
def health():
    return jsonify(status="ok")


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
