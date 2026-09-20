"""SQLite storage: per-group settings and warnings."""
import os
import sqlite3

_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    chat_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    PRIMARY KEY (chat_id, key)
);
CREATE TABLE IF NOT EXISTS warns (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    count   INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
"""


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(os.getenv("DB_PATH", "bot.db"), check_same_thread=False)
        _conn.executescript(_SCHEMA)
    return _conn


def execute(sql: str, params: tuple = ()) -> None:
    conn = _db()
    conn.execute(sql, params)
    conn.commit()


def query(sql: str, params: tuple = ()) -> list[tuple]:
    return _db().execute(sql, params).fetchall()


def ensure(schema_sql: str) -> None:
    """Create extra tables (used by the feature modules)."""
    conn = _db()
    conn.executescript(schema_sql)
    conn.commit()


def get_value(chat_id: int, key: str, default: str | None = None) -> str | None:
    rows = query("SELECT value FROM settings WHERE chat_id=? AND key=?", (chat_id, key))
    return rows[0][0] if rows else default


def set_value(chat_id: int, key: str, value: str) -> None:
    execute("INSERT OR REPLACE INTO settings (chat_id, key, value) VALUES (?, ?, ?)", (chat_id, key, value))


def del_value(chat_id: int, key: str) -> None:
    execute("DELETE FROM settings WHERE chat_id=? AND key=?", (chat_id, key))


def get_bool(chat_id: int, key: str, default: bool = False) -> bool:
    value = get_value(chat_id, key)
    return default if value is None else value == "1"


class WarnStore:
    """dict-like store: warns[(chat_id, user_id)] -> number of warnings, kept in SQLite."""

    def get(self, key: tuple[int, int], default: int = 0) -> int:
        rows = query("SELECT count FROM warns WHERE chat_id=? AND user_id=?", key)
        return rows[0][0] if rows else default

    def __getitem__(self, key: tuple[int, int]) -> int:
        return self.get(key, 0)

    def __setitem__(self, key: tuple[int, int], value: int) -> None:
        if value <= 0:
            self.pop(key)
        else:
            execute(
                "INSERT OR REPLACE INTO warns (chat_id, user_id, count) VALUES (?, ?, ?)",
                (key[0], key[1], value),
            )

    def pop(self, key: tuple[int, int], default=None):
        execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", key)
        return default
