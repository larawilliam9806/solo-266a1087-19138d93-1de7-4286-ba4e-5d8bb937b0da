"""SQLite 存储层。

设计要点：
- 每个版本的数据整体写入、永不修改（immutable），通过 meta 表中的
  current_version_id 指针标识当前正式版。
- 发布 = 单个事务内插入全部数据 + 拨动指针，对查询方而言要么看到
  旧完整版本、要么看到新完整版本，绝不出现半新半旧。
- 失败版本只写 versions/version_errors 元数据，不写任何时刻数据，
  也不触碰指针，当前正式版保持不变。
- WAL 模式：写入期间不阻塞读取。
"""
from __future__ import annotations

import os
import sqlite3

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "data", "gtfs.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('published', 'failed')),
    error_count  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stops (
    version_id INTEGER NOT NULL,
    stop_id    TEXT NOT NULL,
    stop_name  TEXT NOT NULL DEFAULT '',
    stop_lat   REAL,
    stop_lon   REAL,
    PRIMARY KEY (version_id, stop_id)
);

CREATE TABLE IF NOT EXISTS routes (
    version_id       INTEGER NOT NULL,
    route_id         TEXT NOT NULL,
    route_short_name TEXT NOT NULL DEFAULT '',
    route_long_name  TEXT NOT NULL DEFAULT '',
    route_type       INTEGER,
    PRIMARY KEY (version_id, route_id)
);

CREATE TABLE IF NOT EXISTS calendar (
    version_id INTEGER NOT NULL,
    service_id TEXT NOT NULL,
    monday     INTEGER NOT NULL,
    tuesday    INTEGER NOT NULL,
    wednesday  INTEGER NOT NULL,
    thursday   INTEGER NOT NULL,
    friday     INTEGER NOT NULL,
    saturday   INTEGER NOT NULL,
    sunday     INTEGER NOT NULL,
    start_date TEXT NOT NULL,   -- ISO YYYY-MM-DD，可字典序比较
    end_date   TEXT NOT NULL,
    PRIMARY KEY (version_id, service_id)
);

CREATE TABLE IF NOT EXISTS trips (
    version_id    INTEGER NOT NULL,
    route_id      TEXT NOT NULL,
    service_id    TEXT NOT NULL,
    trip_id       TEXT NOT NULL,
    trip_headsign TEXT NOT NULL DEFAULT '',
    direction_id  INTEGER,
    PRIMARY KEY (version_id, trip_id)
);
CREATE INDEX IF NOT EXISTS idx_trips_route ON trips (version_id, route_id);

CREATE TABLE IF NOT EXISTS stop_times (
    version_id     INTEGER NOT NULL,
    trip_id        TEXT NOT NULL,
    stop_sequence  INTEGER NOT NULL,
    stop_id        TEXT NOT NULL,
    arrival_secs   INTEGER NOT NULL,   -- 自运营日 00:00 起的秒数，可 >= 86400
    departure_secs INTEGER NOT NULL,
    PRIMARY KEY (version_id, trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS idx_stop_times_stop ON stop_times (version_id, stop_id);

CREATE TABLE IF NOT EXISTS version_errors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    file       TEXT NOT NULL,
    row        INTEGER,
    field      TEXT,
    message    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_version_errors ON version_errors (version_id);

-- 逐站车门计数：每 (运行图版本, 运营日, 班次) 只保留一张“当前”计数表。
-- 整表换交成功时在同一事务内替换，因此旧表 id 立即失效（旧候选不能确认、
-- 旧明细不再供数）。换交失败/格式错误时事务不提交，旧表原样保留。
CREATE TABLE IF NOT EXISTS count_tables (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL,
    service_date TEXT NOT NULL,             -- ISO YYYY-MM-DD
    trip_id      TEXT NOT NULL,
    capacity     INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'verified')),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    verified_at  TEXT,
    UNIQUE (version_id, service_date, trip_id)
);

CREATE TABLE IF NOT EXISTS count_rows (
    table_id       INTEGER NOT NULL
                   REFERENCES count_tables(id) ON DELETE CASCADE,
    stop_sequence  INTEGER NOT NULL,
    stop_id        TEXT NOT NULL,
    alight         INTEGER NOT NULL,
    board          INTEGER NOT NULL,
    -- 整表上传时的原值；复点生效后仍保留，供网页对照
    original_alight INTEGER NOT NULL,
    original_board  INTEGER NOT NULL,
    recounted       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (table_id, stop_sequence)
);
"""


def db_path() -> str:
    return os.environ.get("GTFS_DB_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def current_version_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'current_version_id'").fetchone()
    return int(row["value"]) if row else None


def publish_version(conn: sqlite3.Connection, label: str,
                    tables: dict[str, list[tuple]]) -> int:
    """单事务写入整版数据并拨动指针。调用方保证 tables 已通过校验。"""
    with conn:  # 事务：全部成功才提交，异常自动回滚
        cur = conn.execute(
            "INSERT INTO versions (label, status, published_at) "
            "VALUES (?, 'published', strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (label,))
        vid = cur.lastrowid
        conn.executemany(
            "INSERT INTO stops (version_id, stop_id, stop_name, stop_lat, stop_lon) "
            "VALUES (?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["stops"]])
        conn.executemany(
            "INSERT INTO routes (version_id, route_id, route_short_name, "
            "route_long_name, route_type) VALUES (?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["routes"]])
        conn.executemany(
            "INSERT INTO calendar (version_id, service_id, monday, tuesday, "
            "wednesday, thursday, friday, saturday, sunday, start_date, end_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["calendar"]])
        conn.executemany(
            "INSERT INTO trips (version_id, route_id, service_id, trip_id, "
            "trip_headsign, direction_id) VALUES (?, ?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["trips"]])
        conn.executemany(
            "INSERT INTO stop_times (version_id, trip_id, stop_sequence, stop_id, "
            "arrival_secs, departure_secs) VALUES (?, ?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["stop_times"]])
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('current_version_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(vid),))
    return vid


# ---------------- 逐站车门计数 ----------------

def count_table(conn: sqlite3.Connection, table_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, version_id, service_date, trip_id, capacity, status, "
        "created_at, verified_at FROM count_tables WHERE id = ?",
        (table_id,)).fetchone()


def current_count_table(conn: sqlite3.Connection, version_id: int,
                        service_date: str, trip_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, version_id, service_date, trip_id, capacity, status, "
        "created_at, verified_at FROM count_tables "
        "WHERE version_id = ? AND service_date = ? AND trip_id = ?",
        (version_id, service_date, trip_id)).fetchone()


def count_rows(conn: sqlite3.Connection, table_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT stop_sequence, stop_id, alight, board, original_alight, "
        "original_board, recounted FROM count_rows "
        "WHERE table_id = ? ORDER BY stop_sequence", (table_id,)).fetchall()


def save_count_table(conn: sqlite3.Connection, version_id: int, service_date: str,
                     trip_id: str, capacity: int, rows: list[dict],
                     status: str = "pending") -> int:
    """整表换交（含首次上传）。

    在单个事务内替换该 (版本, 运营日, 班次) 的当前计数表；任何异常都会
    回滚整个事务，旧表与旧核验结果保持不变。
    """
    with conn:
        # 先删旧表再插入新表；两步同处一个事务，新表插入失败会整体回滚，
        # 旧表与旧核验结果仍然保留。
        old = conn.execute(
            "SELECT id FROM count_tables WHERE version_id = ? AND service_date = ? "
            "AND trip_id = ?", (version_id, service_date, trip_id)).fetchone()
        if old is not None:
            conn.execute("DELETE FROM count_tables WHERE id = ?", (old["id"],))
        cur = conn.execute(
            "INSERT INTO count_tables (version_id, service_date, trip_id, capacity, "
            "status, verified_at) VALUES (?, ?, ?, ?, ?, "
            "CASE WHEN ? = 'verified' "
            "THEN strftime('%Y-%m-%dT%H:%M:%SZ','now') ELSE NULL END)",
            (version_id, service_date, trip_id, capacity, status, status))
        table_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO count_rows (table_id, stop_sequence, stop_id, alight, "
            "board, original_alight, original_board, recounted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            [(table_id, r["stop_sequence"], r["stop_id"], r["alight"],
              r["board"], r["alight"], r["board"]) for r in rows])
    return table_id


def apply_recount(conn: sqlite3.Connection, table_id: int, rows: list[dict],
                  verified: bool) -> None:
    """写入复点值；核验通过则同事务内把整表标记为 verified。

    ``rows`` 为复点后的整表；只更新被复点的行并保留其整表上传时的原值。
    """
    with conn:
        conn.executemany(
            "UPDATE count_rows SET alight = ?, board = ?, recounted = 1 "
            "WHERE table_id = ? AND stop_sequence = ?",
            [(r["alight"], r["board"], table_id, r["stop_sequence"])
             for r in rows if r.get("recounted")])
        if verified:
            conn.execute(
                "UPDATE count_tables SET status = 'verified', "
                "verified_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                "WHERE id = ?", (table_id,))


def record_failed_version(conn: sqlite3.Connection, label: str,
                          errors: list[dict]) -> int:
    """记录失败版本及其错误明细；不触碰正式版指针。"""
    with conn:
        cur = conn.execute(
            "INSERT INTO versions (label, status, error_count) VALUES (?, 'failed', ?)",
            (label, len(errors)))
        vid = cur.lastrowid
        conn.executemany(
            "INSERT INTO version_errors (version_id, file, row, field, message) "
            "VALUES (?, ?, ?, ?, ?)",
            [(vid, e["file"], e["row"], e["field"], e["message"]) for e in errors])
    return vid
