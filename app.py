import os
import secrets

import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify
from flask_cors import CORS

MAX_PGN_BYTES = 5 * 1024 * 1024  # 5MB, generous headroom over real collections
ID_BYTES = 18  # secrets.token_urlsafe(18) -> 24 url-safe chars, ~144 bits of entropy

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": os.environ.get("ALLOWED_ORIGIN", "*")}})

DATABASE_URL = os.environ["DATABASE_URL"]
pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)


def get_conn():
    return pool.getconn()


def put_conn(conn):
    pool.putconn(conn)


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
        conn.commit()
    finally:
        put_conn(conn)


@app.post("/api/libraries")
def create_library():
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


@app.get("/api/health")
def health():
    return jsonify(status="ok")


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
