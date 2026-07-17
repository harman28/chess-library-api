import html
import io
import os
import re
import secrets

import chess
import chess.pgn
import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from PIL import Image, ImageDraw
from werkzeug.security import generate_password_hash, check_password_hash

MAX_PGN_BYTES = 5 * 1024 * 1024  # 5MB, generous headroom over real collections
SHARE_MAX_PGN_BYTES = 64 * 1024  # a single shared game never needs anywhere near the whole-library cap
SHARE_RATE_LIMIT_PER_DAY = 50  # per-IP; blast radius here is just a DB row, same as the existing anonymous save endpoint
ID_BYTES = 18  # secrets.token_urlsafe(18) -> 24 url-safe chars, ~144 bits of entropy
SESSION_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) -> ~43 url-safe chars, ~256 bits
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128
SHARE_HOST = "https://library.chessscenes.com"

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PIECES_DIR = os.path.join(STATIC_DIR, "pieces")
BOARD_SQUARE_PX = 64
BOARD_LIGHT_RGB = (0x8C, 0xA2, 0xB4)  # matches .cw-sq.light in the live board widget
BOARD_DARK_RGB = (0x5F, 0x78, 0x91)  # matches .cw-sq.dark

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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS share_events (
                    id SERIAL PRIMARY KEY,
                    ip TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS share_events_ip_created_idx ON share_events (ip, created_at)")
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


# ---- single-game sharing ----
# A shared game is stored in the exact same libraries/libraries_dev tables as the
# anonymous "Save My Library" flow above - it's just a smaller instance of the same
# {playerName, pgn} shape (one game instead of a whole collection). What's new here is
# a dynamically-rendered page per share (real per-game <meta> tags + a board preview
# image), since library.chessscenes.com used to be pure static GitHub Pages and could
# never vary per-URL - see chess-library-api/CLAUDE.md for the full history.

PIECE_IMAGES = {}


def load_piece_images():
    # Runs at import time - must never raise, or it takes the whole app down with it
    # (including every unrelated already-working endpoint). Worst case on failure:
    # PIECE_IMAGES stays empty and share_preview_png() 500s for that one feature.
    try:
        for color in ("w", "b"):
            for kind in ("K", "Q", "R", "B", "N", "P"):
                key = color + kind
                path = os.path.join(PIECES_DIR, key + ".png")
                PIECE_IMAGES[key] = Image.open(path).convert("RGBA").resize(
                    (BOARD_SQUARE_PX, BOARD_SQUARE_PX), Image.LANCZOS
                )
    except Exception:
        PIECE_IMAGES.clear()


def render_board_png(board):
    size = BOARD_SQUARE_PX
    img = Image.new("RGB", (size * 8, size * 8))
    draw = ImageDraw.Draw(img)
    for file in range(8):
        for rank in range(8):
            color = BOARD_DARK_RGB if (file + rank) % 2 == 0 else BOARD_LIGHT_RGB
            x0, y0 = file * size, (7 - rank) * size
            draw.rectangle([x0, y0, x0 + size, y0 + size], fill=color)
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is None:
            continue
        file, rank = chess.square_file(square), chess.square_rank(square)
        key = ("w" if piece.color == chess.WHITE else "b") + piece.symbol().upper()
        img.paste(PIECE_IMAGES[key], (file * size, (7 - rank) * size), PIECE_IMAGES[key])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def parse_single_game(pgn_text):
    try:
        return chess.pgn.read_game(io.StringIO(pgn_text))
    except Exception:
        return None


def board_at_halfway(game):
    board = game.board()
    if game is None:
        return chess.Board()
    try:
        moves = list(game.mainline_moves())
    except Exception:
        moves = []
    for move in moves[: len(moves) // 2]:
        try:
            board.push(move)
        except Exception:
            break
    return board


def _clean_header(value):
    value = (value or "").strip()
    if not value or value in ("?", "*", "????.??.??"):
        return ""
    return value[:200]


def build_share_meta(game, share_url, image_url):
    if game is not None:
        white = _clean_header(game.headers.get("White"))
        black = _clean_header(game.headers.get("Black"))
        event = _clean_header(game.headers.get("Event"))
        date = _clean_header(game.headers.get("Date"))
        result = _clean_header(game.headers.get("Result"))
    else:
        white = black = event = date = result = ""

    title = f"{white or 'White'} vs {black or 'Black'}"
    description = " · ".join(p for p in (event, date, result) if p) or "A shared game from Chess Library"

    return {
        "title": html.escape(title[:200]),
        "description": html.escape(description[:200]),
        "image": image_url,
        "url": share_url,
    }


def _replace_tag(html_content, pattern, new_value):
    def repl(m):
        return m.group(1) + new_value + m.group(2)

    return re.sub(pattern, repl, html_content, count=1)


def inject_share_meta(html_content, meta):
    html_content = _replace_tag(html_content, r"(<title>).*?(</title>)", meta["title"])
    html_content = _replace_tag(html_content, r'(<meta property="og:title" content=")[^"]*(")', meta["title"])
    html_content = _replace_tag(
        html_content, r'(<meta property="og:description" content=")[^"]*(")', meta["description"]
    )
    html_content = _replace_tag(html_content, r'(<meta property="og:image" content=")[^"]*(")', meta["image"])
    html_content = _replace_tag(html_content, r'(<meta property="og:url" content=")[^"]*(")', meta["url"])
    html_content = _replace_tag(html_content, r'(<meta name="twitter:title" content=")[^"]*(")', meta["title"])
    html_content = _replace_tag(
        html_content, r'(<meta name="twitter:description" content=")[^"]*(")', meta["description"]
    )
    html_content = _replace_tag(html_content, r'(<meta name="twitter:image" content=")[^"]*(")', meta["image"])
    return html_content


def read_static_html(env):
    filename = os.path.join(STATIC_DIR, "dev", "index.html") if env == "dev" else os.path.join(STATIC_DIR, "index.html")
    with open(filename, "r", encoding="utf-8") as f:
        return f.read()


def share_url_for(env, library_id):
    prefix = "dev/" if env == "dev" else ""
    return f"{SHARE_HOST}/{prefix}g/{library_id}"


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def create_game_share(env):
    ip = client_ip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM share_events WHERE ip = %s AND created_at > now() - interval '1 day'",
                (ip,),
            )
            if cur.fetchone()[0] >= SHARE_RATE_LIMIT_PER_DAY:
                return jsonify(error="Sharing limit reached, please try again tomorrow"), 429
    finally:
        put_conn(conn)

    data = request.get_json(silent=True) or {}
    player_name = (data.get("playerName") or "").strip()
    pgn = data.get("pgn") or ""

    if not player_name:
        return jsonify(error="playerName is required"), 400
    if not pgn.strip():
        return jsonify(error="pgn is required"), 400
    if len(pgn.encode("utf-8")) > SHARE_MAX_PGN_BYTES:
        return jsonify(error="pgn is too large for a single-game share"), 413

    library_id = secrets.token_urlsafe(ID_BYTES)
    libraries_table = LIBRARIES_TABLES[env]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {libraries_table} (id, player_name, pgn) VALUES (%s, %s, %s)",
                (library_id, player_name, pgn),
            )
            cur.execute("INSERT INTO share_events (ip) VALUES (%s)", (ip,))
        conn.commit()
    finally:
        put_conn(conn)

    return jsonify(id=library_id, url=share_url_for(env, library_id)), 201


def share_page(env, library_id):
    libraries_table = LIBRARIES_TABLES[env]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT pgn FROM {libraries_table} WHERE id = %s", (library_id,))
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if row is None:
        return jsonify(error="not found"), 404

    game = parse_single_game(row[0])
    share_url = share_url_for(env, library_id)
    meta = build_share_meta(game, share_url, share_url + "/preview.png")

    html_content = inject_share_meta(read_static_html(env), meta)
    resp = app.response_class(html_content, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def share_preview_png(env, library_id):
    libraries_table = LIBRARIES_TABLES[env]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT pgn FROM {libraries_table} WHERE id = %s", (library_id,))
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if row is None:
        return jsonify(error="not found"), 404
    if not PIECE_IMAGES:
        return jsonify(error="preview image is temporarily unavailable"), 503

    game = parse_single_game(row[0])
    board = board_at_halfway(game) if game is not None else chess.Board()
    buf = render_board_png(board)

    resp = app.response_class(buf.read(), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


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


# ---- static frontend + share pages ----
# The frontend used to be a separate static site on GitHub Pages, which could never
# vary its HTML per-URL - see chess-library-api/CLAUDE.md for why that made per-game
# WhatsApp previews impossible and why the whole site now lives here instead.


@app.get("/")
def serve_root():
    resp = app.response_class(read_static_html("prod"), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/dev/")
def serve_dev_root():
    resp = app.response_class(read_static_html("dev"), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/og-image.png")
def serve_og_image():
    return send_from_directory(STATIC_DIR, "og-image.png")


@app.get("/logos/<path:filename>")
def serve_logos(filename):
    return send_from_directory(os.path.join(STATIC_DIR, "logos"), filename)


@app.get("/amsterdam_games.pgn")
def serve_amsterdam_games():
    return send_from_directory(STATIC_DIR, "amsterdam_games.pgn")


@app.post("/api/games/share")
def create_game_share_prod():
    return create_game_share("prod")


@app.post("/api/dev/games/share")
def create_game_share_dev():
    return create_game_share("dev")


@app.get("/g/<library_id>")
def share_page_prod(library_id):
    return share_page("prod", library_id)


@app.get("/g/<library_id>/preview.png")
def share_preview_png_prod(library_id):
    return share_preview_png("prod", library_id)


@app.get("/dev/g/<library_id>")
def share_page_dev(library_id):
    return share_page("dev", library_id)


@app.get("/dev/g/<library_id>/preview.png")
def share_preview_png_dev(library_id):
    return share_preview_png("dev", library_id)


load_piece_images()
init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
