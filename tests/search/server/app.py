import json
import re
import sqlite3
from pathlib import Path

from activitypub_client_search.parser import parse_query
from activitypub_client_search.sql import ast_to_sql
from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "sample_objects.json"


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["DB"] = create_in_memory_db()

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/query")
    def run_query():
        payload = request.get_json(silent=True) or {}
        search_query = (payload.get("query") or "").strip()
        if not search_query:
            return jsonify({"error": "Search query is required."}), 400

        conn: sqlite3.Connection = app.config["DB"]
        try:
            ast = parse_query(search_query)
            where_sql, params = ast_to_sql(ast)
            sql = f"""
            SELECT
                uri,
                type,
                json_extract(document, '$.preferredUsername') AS preferred_username,
                json_extract(document, '$.summary') AS summary
            FROM objects
            WHERE {where_sql}
            ORDER BY uri
            LIMIT 100
            """
            cur = conn.execute(sql, params)
            columns = [col[0] for col in cur.description]
            rows = [list(row) for row in cur.fetchall()]
            return jsonify(
                {
                    "columns": columns,
                    "rows": rows,
                    "sql": sql.strip(),
                    "params": params,
                }
            )
        except (sqlite3.Error, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/autocomplete")
    def autocomplete():
        prefix = (request.args.get("prefix") or "").strip()
        if not prefix:
            return jsonify([])

        pattern = f"{prefix}%"
        conn: sqlite3.Connection = app.config["DB"]
        rows = conn.execute(
            """
            SELECT
                json_extract(document, '$.preferredUsername') AS preferred_username
            FROM objects
            WHERE type = 'Person'
              AND json_extract(document, '$.preferredUsername') LIKE ?
            ORDER BY preferred_username
            LIMIT 10
            """,
            (pattern,),
        ).fetchall()
        return jsonify([row[0] for row in rows if row[0]])

    @app.get("/actor-search")
    def actor_search():
        prefix = (request.args.get("prefix") or "").strip()
        if not prefix:
            return jsonify([])

        pattern = f"{prefix}%"
        conn: sqlite3.Connection = app.config["DB"]
        rows = conn.execute(
            """
            SELECT
                uri,
                json_extract(document, '$.preferredUsername') AS preferred_username,
                json_extract(document, '$.summary') AS summary
            FROM objects
            WHERE type = 'Person'
              AND json_extract(document, '$.preferredUsername') LIKE ?
            ORDER BY preferred_username
            LIMIT 25
            """,
            (pattern,),
        ).fetchall()

        return jsonify(
            [
                {
                    "uri": row[0],
                    "preferredUsername": row[1],
                    "summary": row[2] or "",
                }
                for row in rows
            ]
        )

    @app.get("/object")
    def get_object():
        uri = (request.args.get("uri") or "").strip()
        if not uri:
            return jsonify({"error": "uri is required."}), 400

        conn: sqlite3.Connection = app.config["DB"]
        row = conn.execute(
            "SELECT document FROM objects WHERE uri = ?",
            (uri,),
        ).fetchone()

        if row is None:
            return jsonify({"error": "Object not found."}), 404

        return jsonify(json.loads(row[0]))

    return app


def create_in_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("SELECT json('{\"ok\": true}')")
    conn.create_function(
        "regexp",
        2,
        lambda pattern, value: 1 if re.search(pattern, value or "") else 0,
    )
    conn.execute(
        """
        CREATE TABLE objects (
            uri TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            document TEXT NOT NULL
        )
        """
    )

    with DATA_FILE.open("r", encoding="utf-8") as infile:
        rows = json.load(infile)

    for row in rows:
        conn.execute(
            "INSERT INTO objects (uri, type, document) VALUES (?, ?, ?)",
            (row["uri"], row["type"], json.dumps(row["doc"])),
        )
    conn.commit()
    return conn


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
