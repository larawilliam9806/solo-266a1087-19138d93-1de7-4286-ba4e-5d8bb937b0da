"""逐站车门计数：接口与页面。

质控流程：
1. 选择已发布运行图 + 运营日开行的一趟班次，填写核载人数，上传逐站计数 CSV。
2. 系统整趟核验；不合规则标记「待核」，并列出所有“只改本站上下车人数”
   就能让整趟合规的候选站次，各给一组可行值。
3. 质控员对当前待核表的候选站录入实际复点值；只有整趟核验通过才生效，
   原值保留供网页对照。
4. 只有核验通过的计数表才能下载 CSV。
5. 重新整表换交后按新表核验，旧候选不能确认，旧明细停止供数。

计数表以 (运行图版本, 运营日, 班次) 为键，每个键任何时刻只有一张当前表；
供数与核验只认当前表，因此换交后的旧表既不能确认也不能下载。
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date as date_type

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from . import db
from .counts import evaluate_counts, find_candidates, parse_counts_csv
from .gtfs import WEEKDAY_COLUMNS

router = APIRouter(prefix="/api/counts", tags=["counts"])


# ---------------- 公共辅助 ----------------

def _parse_date(value: str) -> date_type:
    try:
        return date_type.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"运营日 '{value}' 不是合法的 YYYY-MM-DD 日期")


def _published_version(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, label, status FROM versions WHERE id = ?",
        (version_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"运行图版本 {version_id} 不存在")
    if row["status"] != "published":
        raise HTTPException(409, f"运行图版本 {version_id} 未发布，不能用于计数核验")
    return row


def _trip_for_date(conn: sqlite3.Connection, version_id: int, service_date: str,
                   trip_id: str) -> sqlite3.Row | None:
    """该运行图在该运营日实际开行的班次；未开行返回 None。"""
    weekday_col = WEEKDAY_COLUMNS[_parse_date(service_date).weekday()]
    return conn.execute(
        f"SELECT t.trip_id, t.route_id, t.service_id, t.trip_headsign, "
        f"       r.route_short_name, r.route_long_name "
        f"FROM trips t JOIN routes r "
        f"  ON r.version_id = t.version_id AND r.route_id = t.route_id "
        f"WHERE t.version_id = ? AND t.trip_id = ? "
        f"  AND EXISTS (SELECT 1 FROM calendar c "
        f"              WHERE c.version_id = t.version_id "
        f"                AND c.service_id = t.service_id "
        f"                AND c.start_date <= ? AND c.end_date >= ? "
        f"                AND c.{weekday_col} = 1)",
        (version_id, trip_id, service_date, service_date)).fetchone()


def _trip_stops(conn: sqlite3.Connection, version_id: int,
                trip_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT st.stop_sequence, st.stop_id, s.stop_name "
        "FROM stop_times st JOIN stops s "
        "  ON s.version_id = st.version_id AND s.stop_id = st.stop_id "
        "WHERE st.version_id = ? AND st.trip_id = ? "
        "ORDER BY st.stop_sequence", (version_id, trip_id)).fetchall()


def _parse_capacity(value: str) -> int:
    text = (value or "").strip()
    if not text.isdigit():
        raise HTTPException(422, f"核载人数 '{value}' 不是非负整数")
    capacity = int(text)
    if capacity <= 0:
        raise HTTPException(422, "核载人数必须为正整数")
    return capacity


def _parse_nonneg(value: str, label: str) -> int:
    text = (value or "").strip()
    if not text.isdigit():
        raise HTTPException(422, f"{label} '{value}' 不是非负整数")
    return int(text)


def _table_payload(conn: sqlite3.Connection, table: sqlite3.Row) -> dict:
    rows = [dict(r) for r in db.count_rows(conn, table["id"])]
    verified = table["status"] == "verified"
    return {
        "table_id": table["id"],
        "version_id": table["version_id"],
        "service_date": table["service_date"],
        "trip_id": table["trip_id"],
        "capacity": table["capacity"],
        "status": table["status"],
        "verified": verified,
        "created_at": table["created_at"],
        "verified_at": table["verified_at"],
        "rows": rows,
        "issues": evaluate_counts(rows, table["capacity"]),
        # 已核验通过的表不再给出候选
        "candidates": ([] if verified
                       else find_candidates(rows, table["capacity"])),
    }


def _is_current(conn: sqlite3.Connection, table: sqlite3.Row) -> bool:
    """该表是否仍是对应 (运行图, 运营日, 班次) 的当前表。"""
    current = db.current_count_table(
        conn, table["version_id"], table["service_date"], table["trip_id"])
    return current is not None and current["id"] == table["id"]


# ---------------- 已发布运行图 / 班次列表 ----------------

@router.get("/versions")
def list_count_versions():
    """可用于计数的已发布运行图（不可变的版本快照）。"""
    conn = db.connect()
    try:
        current = db.current_version_id(conn)
        rows = conn.execute(
            "SELECT id, label, published_at FROM versions "
            "WHERE status = 'published' ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    return {"current_version_id": current,
            "versions": [dict(r) for r in rows]}


@router.get("/trips")
def list_trips(
    date: str = Query(..., description="运营日，格式 YYYY-MM-DD"),
    version_id: int | None = Query(None, description="运行图版本，默认当前正式版"),
):
    """该运行图在该运营日开行的全部班次，并标注当前计数表的状态。"""
    _parse_date(date)
    conn = db.connect()
    try:
        vid = version_id if version_id is not None else db.current_version_id(conn)
        if vid is None:
            raise HTTPException(404, "尚未发布任何版本")
        _published_version(conn, vid)
        weekday_col = WEEKDAY_COLUMNS[_parse_date(date).weekday()]
        rows = conn.execute(
            f"SELECT t.trip_id, t.route_id, t.service_id, t.trip_headsign, "
            f"       r.route_short_name, r.route_long_name "
            f"FROM trips t JOIN routes r "
            f"  ON r.version_id = t.version_id AND r.route_id = t.route_id "
            f"JOIN calendar c "
            f"  ON c.version_id = t.version_id AND c.service_id = t.service_id "
            f"WHERE t.version_id = ? AND c.start_date <= ? AND c.end_date >= ? "
            f"  AND c.{weekday_col} = 1 "
            f"ORDER BY t.route_id, t.trip_id",
            (vid, date, date)).fetchall()
        status = {r["trip_id"]: r["status"] for r in conn.execute(
            "SELECT trip_id, status FROM count_tables "
            "WHERE version_id = ? AND service_date = ?", (vid, date)).fetchall()}
    finally:
        conn.close()
    trips = []
    for r in rows:
        item = dict(r)
        item["count_status"] = status.get(r["trip_id"])
        trips.append(item)
    return {"version_id": vid, "date": date, "trips": trips}


# ---------------- 整表换交 ----------------

@router.post("", status_code=201)
async def submit_counts(
    version_id: int = Form(...),
    date: str = Form(..., description="运营日 YYYY-MM-DD"),
    trip_id: str = Form(...),
    capacity: str = Form(...),
    counts: UploadFile = File(...),
):
    """上传逐站计数 CSV 并整表换交。

    格式错误直接 422，已存记录与核验结果保持不变；格式合法时按新表核验，
    整表换交成功后旧候选与旧明细立即失效。
    """
    _parse_date(date)
    cap = _parse_capacity(capacity)
    content = await counts.read()

    conn = db.connect()
    try:
        _published_version(conn, version_id)
        trip = _trip_for_date(conn, version_id, date, trip_id)
        if trip is None:
            raise HTTPException(
                422, f"班次 '{trip_id}' 在运营日 {date} 不开行（或不存在）")
        stops = _trip_stops(conn, version_id, trip_id)
        if not stops:
            raise HTTPException(422, f"班次 '{trip_id}' 没有停站记录")

        sequences = [s["stop_sequence"] for s in stops]
        parsed, errors = parse_counts_csv(content, sequences)
        if parsed is None:
            raise HTTPException(
                422, {"message": "计数 CSV 格式错误，未保存；已存记录与核验结果保持不变",
                      "errors": errors, "error_count": len(errors)})

        stop_names = {s["stop_sequence"]: s["stop_name"] for s in stops}
        stop_ids = {s["stop_sequence"]: s["stop_id"] for s in stops}
        rows = [{"stop_sequence": r["stop_sequence"],
                 "stop_id": stop_ids[r["stop_sequence"]],
                 "stop_name": stop_names[r["stop_sequence"]],
                 "alight": r["alight"], "board": r["board"]} for r in parsed]
        # 整表核验与换交在同一事务内完成：合规的新表直接以 verified 落库。
        status = "pending" if evaluate_counts(rows, cap) else "verified"
        table_id = db.save_count_table(conn, version_id, date, trip_id, cap,
                                       rows, status=status)
        payload = _table_payload(conn, db.count_table(conn, table_id))
    finally:
        conn.close()

    payload["trip"] = dict(trip)
    payload["message"] = ("整趟计数合规，已核验通过" if payload["verified"]
                          else "计数不合规，已标记为待核")
    return payload


# ---------------- 查看当前表 ----------------

@router.get("")
def get_current_counts(
    version_id: int = Query(...),
    date: str = Query(...),
    trip_id: str = Query(...),
):
    _parse_date(date)
    conn = db.connect()
    try:
        _published_version(conn, version_id)
        table = db.current_count_table(conn, version_id, date, trip_id)
        if table is None:
            raise HTTPException(404, "该班次在该运行图与该运营日下没有计数表")
        payload = _table_payload(conn, table)
    finally:
        conn.close()
    return payload


# ---------------- 复点 ----------------

@router.post("/{table_id}/recount")
def recount(
    table_id: int,
    stop_sequence: int = Form(...),
    alight: str = Form(...),
    board: str = Form(...),
):
    """录入一个候选站的实际复点值；整趟核验通过才生效。

    输入格式错误、站次不是当前候选或核验不通过时一律 422，且不修改任何
    已存数据；网页据此保留原值供对照。旧表（已换交）不能确认。
    """
    alight_val = _parse_nonneg(alight, "下车人数")
    board_val = _parse_nonneg(board, "上车人数")

    conn = db.connect()
    try:
        table = db.count_table(conn, table_id)
        if table is None:
            raise HTTPException(404, f"计数表 {table_id} 不存在（整表可能已换交）")
        if not _is_current(conn, table):
            raise HTTPException(
                409, "该计数表已不是当前表（整表已换交），旧候选不能确认")
        if table["status"] == "verified":
            raise HTTPException(409, "该计数表已核验通过，无需复点")

        rows = [dict(r) for r in db.count_rows(conn, table_id)]
        candidates = {c["stop_sequence"] for c in
                      find_candidates(rows, table["capacity"])}
        if stop_sequence not in candidates:
            raise HTTPException(
                422, f"第 {stop_sequence} 站不是当前待核表的候选站次，"
                     f"只允许对候选站复点")

        new_rows = []
        for r in rows:
            row = dict(r)
            if r["stop_sequence"] == stop_sequence:
                row["alight"] = alight_val
                row["board"] = board_val
                row["recounted"] = 1
            new_rows.append(row)
        issues = evaluate_counts(new_rows, table["capacity"])
        if issues:
            original = next(r for r in rows
                            if r["stop_sequence"] == stop_sequence)
            raise HTTPException(
                422, {"message": "复点后整趟核验未通过，复点值未生效；已存记录保持不变",
                      "issues": issues,
                      "original": {"alight": original["alight"],
                                   "board": original["board"]}})

        db.apply_recount(conn, table_id, new_rows, verified=True)
        payload = _table_payload(conn, db.count_table(conn, table_id))
    finally:
        conn.close()
    payload["message"] = "复点录入成功，整趟核验通过"
    return payload


# ---------------- 下载 ----------------

@router.get("/{table_id}/csv")
def download_csv(table_id: int):
    """只有核验通过、且仍是当前表的计数才能下载。"""
    conn = db.connect()
    try:
        table = db.count_table(conn, table_id)
        if table is None:
            raise HTTPException(404, f"计数表 {table_id} 不存在（整表可能已换交）")
        if not _is_current(conn, table):
            raise HTTPException(409, "该计数表已不是当前表，不再供数")
        if table["status"] != "verified":
            raise HTTPException(409, "计数表尚未核验通过，不能下载")
        rows = db.count_rows(conn, table_id)
    finally:
        conn.close()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["trip_id", "service_date", "stop_sequence", "stop_id",
                     "alight", "board", "capacity"])
    for r in rows:
        writer.writerow([table["trip_id"], table["service_date"],
                         r["stop_sequence"], r["stop_id"],
                         r["alight"], r["board"], table["capacity"]])
    filename = (f"counts_{table['service_date']}_{table['trip_id']}"
                f"_v{table['version_id']}.csv")
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
