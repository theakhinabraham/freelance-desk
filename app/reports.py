"""Load the named queries in db/reports.sql, so the SQL lives in one readable file.

File format:
    -- explanation comments ...
    -- name: some_query
    SELECT ... ;          <- the query ends at the first line ending with ';'

The query text is used exactly as written; values are passed separately as
parameters (?1, ?2 ...), so nothing is ever inserted into the SQL string.
"""

import sqlite3
from functools import lru_cache
from pathlib import Path

REPORTS_PATH = Path(__file__).resolve().parent.parent / "db" / "reports.sql"
NAME_PREFIX = "-- name:"


@lru_cache(maxsize=1)
def load_queries() -> dict[str, dict]:
    """name -> {"sql": query text, "comment": the comment block above it}."""
    queries: dict[str, dict] = {}
    comment: list[str] = []
    current: str | None = None
    body: list[str] = []
    for line in REPORTS_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if current is None:
            text = stripped[2:].strip()
            if stripped.startswith(NAME_PREFIX):
                current, body = stripped[len(NAME_PREFIX):].strip(), []
            elif text and set(text) == {"="}:
                comment = []                    # "====" ruler: end of the file header
            elif stripped.startswith("--") and not (text and set(text) == {"-"}):
                comment.append(text)            # explanation line ("----" rulers are skipped)
            continue
        body.append(line)
        if stripped.endswith(";"):
            queries[current] = {"sql": "\n".join(body).strip(), "comment": "\n".join(comment).strip()}
            current, comment = None, []
    return queries


def run(conn: sqlite3.Connection, name: str, *params) -> list[sqlite3.Row]:
    return conn.execute(load_queries()[name]["sql"], params).fetchall()
