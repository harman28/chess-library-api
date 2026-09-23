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
SHARE_MAX_PGN_BYTES = 512 * 1024  # comfortable headroom for SHARE_MAX_GAMES games, well short of the whole-library cap
SHARE_MAX_GAMES = 25  # keeps "share a selection" meaningfully distinct from "share my whole library"
SHARE_RATE_LIMIT_PER_DAY = 50  # per-IP; blast radius here is just a DB row, same as the existing anonymous save endpoint
ID_BYTES = 18  # secrets.token_urlsafe(18) -> 24 url-safe chars, ~144 bits of entropy
SESSION_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) -> ~43 url-safe chars, ~256 bits
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128

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


# Each deployment (the real "web" service, the "chess-library-api-staging" service)
# now has its own dedicated Postgres database, so "which environment" is entirely a
# function of which DATABASE_URL this process was started with - there's no longer a
# prod/dev split within a single database, and no /dev/ URL path or /api/dev/... route
# prefix. Table names are plain and unsuffixed as a result.


def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS libraries (
                    id TEXT PRIMARY KEY,
                    player_name TEXT NOT NULL,
                    pgn TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
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
                "CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_idx ON users (LOWER(username))"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                "ALTER TABLE libraries ADD COLUMN IF NOT EXISTS user_id TEXT REFERENCES users(id) ON DELETE CASCADE"
            )
            cur.execute(
                "ALTER TABLE libraries ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS libraries_user_id_unique ON libraries (user_id)"
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


@app.post("/api/libraries")
def create_library():
    data = request.get_json(silent=True) or {}
    # Empty playerName is allowed here (unlike /api/games/share): a guest saving their
    # own fresh library from someone else's shared-game view has no name of their own
    # yet to send, and the frontend deliberately sends "" rather than the sharer's name
    # in that case — see loadSharedGame/isSharedGameGuest in static/index.html.
    player_name = (data.get("playerName") or "").strip()
    pgn = data.get("pgn") or ""

    if not pgn.strip():
        return jsonify(error="pgn is required"), 400
    if len(pgn.encode("utf-8")) > MAX_PGN_BYTES:
        return jsonify(error="pgn is too large"), 413

    library_id = secrets.token_urlsafe(ID_BYTES)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO libraries (id, player_name, pgn) VALUES (%s, %s, %s)",
                (library_id, player_name, pgn),
            )
        conn.commit()
    finally:
        put_conn(conn)

    return jsonify(id=library_id), 201


@app.get("/api/libraries/<library_id>")
def get_library(library_id):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT player_name, pgn FROM libraries WHERE id = %s",
                (library_id,),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)

    if row is None:
        return jsonify(error="not found"), 404

    player_name, pgn = row
    return jsonify(playerName=player_name, pgn=pgn)


# ---- single-game sharing ----
# A shared game is stored in the exact same libraries table as the anonymous "Save My
# Library" flow above - it's just a smaller instance of the same {playerName, pgn}
# shape (one game instead of a whole collection). What's new here is a
# dynamically-rendered page per share (real per-game <meta> tags + a board preview
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


def parse_all_games(pgn_text):
    games = []
    try:
        stream = io.StringIO(pgn_text)
        while True:
            game = chess.pgn.read_game(stream)
            if game is None:
                break
            games.append(game)
    except Exception:
        pass
    return games


def board_at_halfway(game):
    board = game.board()
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


def build_share_meta(games, player_name, share_url, image_url):
    count = len(games)
    if count == 1:
        game = games[0]
        white = _clean_header(game.headers.get("White"))
        black = _clean_header(game.headers.get("Black"))
        event = _clean_header(game.headers.get("Event"))
        date = _clean_header(game.headers.get("Date"))
        result = _clean_header(game.headers.get("Result"))
        title = f"{white or 'White'} vs {black or 'Black'}"
        description = " · ".join(p for p in (event, date, result) if p) or "A shared game from Chess Library"
    else:
        player_name = _clean_header(player_name)
        title = f"{count} games from {player_name}" if player_name else f"{count} shared games"
        distinct_events = []
        for g in games:
            e = _clean_header(g.headers.get("Event"))
            if e and e not in distinct_events:
                distinct_events.append(e)
        if distinct_events:
            description = ", ".join(distinct_events[:3])
            if len(distinct_events) > 3:
                description += ", and more"
        else:
            description = f"{count} games shared from Chess Library"

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


def read_static_html():
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


def request_origin():
    # Derived from the incoming request rather than a hardcoded domain, so share links
    # are correct on whichever host actually served the request - the real custom
    # domain, its raw *.up.railway.app address, or the separate staging deployment's
    # own domain - with nothing to update here when a domain changes. Railway
    # terminates TLS at its edge and forwards plain HTTP internally, so the scheme has
    # to be read from X-Forwarded-Proto rather than trusted from the request itself;
    # default to https since every path a real visitor takes to this app is https.
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    return f"{scheme}://{request.host}"


def share_url_for(library_id):
    return f"{request_origin()}/g/{library_id}"


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


@app.post("/api/games/share")
def create_game_share():
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
        return jsonify(error="pgn is too large for a share link"), 413

    game_count = len(parse_all_games(pgn))
    if game_count == 0:
        return jsonify(error="Couldn't find any valid games in that PGN"), 400
    if game_count > SHARE_MAX_GAMES:
        return jsonify(error=f"You can share at most {SHARE_MAX_GAMES} games in one link"), 400

    library_id = secrets.token_urlsafe(ID_BYTES)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO libraries (id, player_name, pgn) VALUES (%s, %s, %s)",
                (library_id, player_name, pgn),
            )
            cur.execute("INSERT INTO share_events (ip) VALUES (%s)", (ip,))
        conn.commit()
    finally:
        put_conn(conn)

    return jsonify(id=library_id, url=share_url_for(library_id)), 201


@app.get("/g/<library_id>")
def share_page(library_id):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT player_name, pgn FROM libraries WHERE id = %s", (library_id,))
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if row is None:
        return jsonify(error="not found"), 404

    player_name, pgn = row
    games = parse_all_games(pgn)
    share_url = share_url_for(library_id)
    meta = build_share_meta(games, player_name, share_url, share_url + "/preview.png")

    html_content = inject_share_meta(read_static_html(), meta)
    resp = app.response_class(html_content, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/g/<library_id>/preview.png")
def share_preview_png(library_id):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pgn FROM libraries WHERE id = %s", (library_id,))
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if row is None:
        return jsonify(error="not found"), 404
    if not PIECE_IMAGES:
        return jsonify(error="preview image is temporarily unavailable"), 503

    games = parse_all_games(row[0])
    board = board_at_halfway(games[0]) if games else chess.Board()
    buf = render_board_png(board)

    resp = app.response_class(buf.read(), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ---- accounts ----


def get_user_from_request():
    """Returns {"id", "username"} for a valid Authorization: Bearer <token> header, else None."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer ") :].strip()
    if not token:
        return None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = %s",
                (token,),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if not row:
        return None
    return {"id": row[0], "username": row[1]}


def create_session(user_id):
    conn = get_conn()
    try:
        token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO sessions (token, user_id) VALUES (%s, %s)", (token, user_id))
        conn.commit()
        return token
    finally:
        put_conn(conn)


@app.post("/api/auth/signup")
def signup():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not USERNAME_RE.match(username):
        return jsonify(error="Username must be 3-32 characters (letters, numbers, underscore, period, hyphen)"), 400
    if len(password) < MIN_PASSWORD_LEN:
        return jsonify(error=f"Password must be at least {MIN_PASSWORD_LEN} characters"), 400
    if len(password) > MAX_PASSWORD_LEN:
        return jsonify(error="Password is too long"), 400

    user_id = secrets.token_urlsafe(ID_BYTES)
    # Explicit pbkdf2 rather than werkzeug's newer scrypt default: scrypt needs
    # hashlib built against an OpenSSL with scrypt support, which isn't a safe
    # assumption across environments (hit this locally against LibreSSL).
    password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
            if cur.fetchone():
                return jsonify(error="That username is already taken"), 409
            cur.execute(
                "INSERT INTO users (id, username, password_hash) VALUES (%s, %s, %s)",
                (user_id, username, password_hash),
            )
        conn.commit()
    finally:
        put_conn(conn)

    token = create_session(user_id)
    return jsonify(token=token, username=username), 201


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, password_hash FROM users WHERE LOWER(username) = LOWER(%s)",
                (username,),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)

    if not row or not row[2] or not check_password_hash(row[2], password):
        return jsonify(error="Incorrect username or password"), 401

    user_id, real_username, _ = row
    token = create_session(user_id)
    return jsonify(token=token, username=real_username), 200


@app.post("/api/auth/logout")
def logout():
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer ") :].strip()
        if token:
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM sessions WHERE token = %s", (token,))
                conn.commit()
            finally:
                put_conn(conn)
    return jsonify(status="ok")


@app.get("/api/auth/me")
def me():
    user = get_user_from_request()
    if not user:
        return jsonify(error="not authenticated"), 401
    return jsonify(username=user["username"])


@app.post("/api/auth/google")
def google_login():
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

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username FROM users WHERE google_sub = %s", (google_sub,))
            row = cur.fetchone()
            if row:
                user_id, username = row
            else:
                user_id = secrets.token_urlsafe(ID_BYTES)
                username = display_name
                suffix = 1
                while True:
                    cur.execute("SELECT 1 FROM users WHERE LOWER(username) = LOWER(%s)", (username,))
                    if not cur.fetchone():
                        break
                    suffix += 1
                    username = f"{display_name}{suffix}"
                cur.execute(
                    "INSERT INTO users (id, username, google_sub, email) VALUES (%s, %s, %s, %s)",
                    (user_id, username, google_sub, email),
                )
        conn.commit()
    finally:
        put_conn(conn)

    token = create_session(user_id)
    return jsonify(token=token, username=username), 200


@app.get("/api/library/mine")
def get_my_library():
    user = get_user_from_request()
    if not user:
        return jsonify(error="not authenticated"), 401
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT player_name, pgn FROM libraries WHERE user_id = %s",
                (user["id"],),
            )
            row = cur.fetchone()
    finally:
        put_conn(conn)
    if row is None:
        return jsonify(error="not found"), 404
    player_name, pgn = row
    return jsonify(playerName=player_name, pgn=pgn)


@app.put("/api/library/mine")
def put_my_library():
    user = get_user_from_request()
    if not user:
        return jsonify(error="not authenticated"), 401

    data = request.get_json(silent=True) or {}
    player_name = (data.get("playerName") or "").strip()
    pgn = data.get("pgn") or ""

    if len(pgn.encode("utf-8")) > MAX_PGN_BYTES:
        return jsonify(error="pgn is too large"), 413

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO libraries (id, user_id, player_name, pgn, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (user_id) DO UPDATE SET player_name = EXCLUDED.player_name, pgn = EXCLUDED.pgn, updated_at = now()
                """,
                (secrets.token_urlsafe(ID_BYTES), user["id"], player_name, pgn),
            )
        conn.commit()
    finally:
        put_conn(conn)
    return jsonify(status="ok")


@app.get("/api/health")
def health():
    return jsonify(status="ok")


# ---- static frontend ----
# The frontend used to be a separate static site on GitHub Pages, which could never
# vary its HTML per-URL - see chess-library-api/CLAUDE.md for why that made per-game
# WhatsApp previews impossible and why the whole site now lives here instead.


@app.get("/")
def serve_root():
    resp = app.response_class(read_static_html(), mimetype="text/html")
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


load_piece_images()
init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
