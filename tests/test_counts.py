"""逐站车门计数：解析、整趟核验、候选站与复点/下载语义。"""
import csv
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conftest import upload, valid_files
from app.counts import evaluate_counts, find_candidates, parse_counts_csv

# T1：S1 S2 S3 S4 四站
T1_SEQS = [1, 2, 3, 4]


def counts_csv(rows, header="stop_sequence,alight,board"):
    return (header + "\n" + "\n".join(
        f"{seq},{a},{b}" for seq, a, b in rows) + "\n").encode()


# 合规：始发空车，终点清空
GOOD_ROWS = [(1, 0, 10), (2, 3, 2), (3, 5, 0), (4, 4, 0)]
# 不合规：终点还剩 1 人
BAD_ROWS = [(1, 0, 10), (2, 3, 2), (3, 5, 0), (4, 3, 0)]


def publish(client):
    assert upload(client, valid_files(), label="counts-src").status_code == 201
    return client.get("/api/versions/current").json()["id"]


def submit(client, vid, rows, trip="T1", date="2026-11-23", capacity="40"):
    return client.post(
        "/api/counts",
        data={"version_id": vid, "date": date, "trip_id": trip,
              "capacity": capacity},
        files={"counts": ("counts.csv", counts_csv(rows), "text/csv")})


def rows_of(body):
    return [(r["stop_sequence"], r["alight"], r["board"]) for r in body["rows"]]


# ---------------- 纯函数 ----------------

def test_evaluate_counts_rules():
    rows = [{"stop_sequence": s, "alight": a, "board": b} for s, a, b in GOOD_ROWS]
    assert evaluate_counts(rows, 40) == []
    bad = [{"stop_sequence": s, "alight": a, "board": b} for s, a, b in BAD_ROWS]
    assert [i["field"] for i in evaluate_counts(bad, 40)] == ["final"]
    # 始发站有人下车
    assert evaluate_counts([{"stop_sequence": 1, "alight": 1, "board": 0},
                            {"stop_sequence": 2, "alight": 0, "board": 0}], 40)
    # 超载
    assert any(i["field"] == "board" for i in evaluate_counts(
        [{"stop_sequence": 1, "alight": 0, "board": 9},
         {"stop_sequence": 2, "alight": 9, "board": 0}], 8))


def test_find_candidates_are_feasible():
    rows = [{"stop_sequence": s, "alight": a, "board": b} for s, a, b in BAD_ROWS]
    cands = find_candidates(rows, 40)
    assert cands  # 终点差 1 人，至少终点站可改
    for c in cands:
        patched = [dict(r) for r in rows]
        for r in patched:
            if r["stop_sequence"] == c["stop_sequence"]:
                r["alight"], r["board"] = c["alight"], c["board"]
        assert evaluate_counts(patched, 40) == []
        assert c["alight"] >= 0 and c["board"] >= 0
    # 核载过小导致任何单站修改都不可行
    assert find_candidates(rows, 2) == [] or all(
        evaluate_counts([dict(r, **({"alight": c["alight"],
                                     "board": c["board"]}
                                    if r["stop_sequence"] == c["stop_sequence"] else {}))
                         for r in rows], 2) == [] for c in find_candidates(rows, 2))


def test_parse_counts_csv_errors():
    parsed, errs = parse_counts_csv(counts_csv(GOOD_ROWS), T1_SEQS)
    assert errs == [] and [r["stop_sequence"] for r in parsed] == T1_SEQS

    _, errs = parse_counts_csv(counts_csv(GOOD_ROWS[:3]), T1_SEQS)
    assert "缺少" in errs[0]["message"]

    _, errs = parse_counts_csv(
        counts_csv([(1, 0, 10), (1, 0, 1), (2, 0, 0), (3, 0, 0), (4, 0, 0)]), T1_SEQS)
    assert any("重复" in e["message"] for e in errs)

    _, errs = parse_counts_csv(
        counts_csv([(1, 0, 10), (2, 0, 0), (3, 0, 0), (4, 0, 0), (9, 0, 0)]), T1_SEQS)
    assert any(e["field"] == "stop_sequence" for e in errs)

    _, errs = parse_counts_csv(
        b"stop_sequence,alight,board\n1,0,-1\n2,0,0\n3,0,0\n4,0,0\n", T1_SEQS)
    assert any(e["field"] == "board" for e in errs)

    _, errs = parse_counts_csv(
        b"stop_sequence,alight\n1,0\n", T1_SEQS)
    assert any("缺少必备列" in e["message"] for e in errs)


# ---------------- API ----------------

def test_counts_trips_listing(client):
    vid = publish(client)
    got = client.get("/api/counts/trips?date=2026-11-23").json()
    assert got["version_id"] == vid
    assert "T1" in {t["trip_id"] for t in got["trips"]}
    # 周六 R1 的 T1 不开行
    got_sat = client.get("/api/counts/trips?date=2026-11-21").json()
    assert "T1" not in {t["trip_id"] for t in got_sat["trips"]}


def test_submit_good_and_download(client):
    vid = publish(client)
    r = submit(client, vid, GOOD_ROWS)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["verified"] is True and body["status"] == "verified"
    assert body["candidates"] == []
    assert rows_of(body) == GOOD_ROWS

    dl = client.get(f"/api/counts/{body['table_id']}/csv")
    assert dl.status_code == 200
    text = dl.content.decode("utf-8-sig")
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert [p["stop_sequence"] for p in parsed] == ["1", "2", "3", "4"]
    assert parsed[0]["trip_id"] == "T1" and parsed[0]["capacity"] == "40"


def test_submit_bad_pending_candidates_and_download_blocked(client):
    vid = publish(client)
    body = submit(client, vid, BAD_ROWS).json()
    assert body["verified"] is False and body["status"] == "pending"
    assert body["issues"]
    cands = {c["stop_sequence"] for c in body["candidates"]}
    assert cands == {1, 2, 3, 4}  # 终点差 1 人，任何一站都可吸收
    for c in body["candidates"]:
        assert c["alight"] >= 0 and c["board"] >= 0

    assert client.get(f"/api/counts/{body['table_id']}/csv").status_code == 409


def test_recount_only_successful_verification_takes_effect(client):
    vid = publish(client)
    body = submit(client, vid, BAD_ROWS).json()
    table_id = body["table_id"]

    # 非候选站：整表本来合规的站点不接受复点；这里改第 1 站为不可行值
    # 使整趟仍不合规，必须被拒绝且不改动记录。
    r = client.post(f"/api/counts/{table_id}/recount",
                    data={"stop_sequence": 1, "alight": 0, "board": 99})
    assert r.status_code == 422
    cur = client.get(f"/api/counts?version_id={vid}&date=2026-11-23&trip_id=T1").json()
    assert rows_of(cur) == BAD_ROWS

    # 负值不是非负整数
    r = client.post(f"/api/counts/{table_id}/recount",
                    data={"stop_sequence": 4, "alight": "-1", "board": 0})
    assert r.status_code == 422
    # 不在当前表中的站次
    r = client.post(f"/api/counts/{table_id}/recount",
                    data={"stop_sequence": 9, "alight": 0, "board": 0})
    assert r.status_code == 422

    # 终点的可行复点值：多下 1 人（原值 3 -> 4）
    good = next(c for c in body["candidates"] if c["stop_sequence"] == 4)
    r = client.post(f"/api/counts/{table_id}/recount",
                    data={"stop_sequence": 4,
                          "alight": good["alight"], "board": good["board"]})
    assert r.status_code == 200, r.text
    verified = r.json()
    assert verified["verified"] is True
    row4 = next(x for x in verified["rows"] if x["stop_sequence"] == 4)
    assert row4["alight"] == 4 and row4["original_alight"] == 3
    assert row4["recounted"] == 1
    assert verified["candidates"] == []

    # 核验通过后可下载，内容为复点后的值
    dl = client.get(f"/api/counts/{table_id}/csv")
    assert dl.status_code == 200
    parsed = list(csv.DictReader(io.StringIO(dl.content.decode("utf-8-sig"))))
    assert parsed[3]["alight"] == "4"

    # 已核验的表不能再复点
    assert client.post(f"/api/counts/{table_id}/recount",
                       data={"stop_sequence": 4, "alight": 5, "board": 0}
                       ).status_code == 409


def test_invalid_csv_keeps_previous_table(client):
    vid = publish(client)
    first = submit(client, vid, BAD_ROWS).json()
    table_id = first["table_id"]

    bad_csv = b"stop_sequence,alight,board\n1,0,10\n"  # 缺站、格式不全
    r = client.post("/api/counts",
                    data={"version_id": vid, "date": "2026-11-23",
                          "trip_id": "T1", "capacity": "40"},
                    files={"counts": ("counts.csv", bad_csv, "text/csv")})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"]

    # 已存表、候选与核验结果原样保留
    cur = client.get(f"/api/counts?version_id={vid}&date=2026-11-23&trip_id=T1").json()
    assert cur["table_id"] == table_id
    assert rows_of(cur) == BAD_ROWS
    assert cur["verified"] is False
    assert {c["stop_sequence"] for c in cur["candidates"]} == {1, 2, 3, 4}


def test_replacement_invalidates_old_candidates_and_rows(client):
    vid = publish(client)
    old = submit(client, vid, BAD_ROWS).json()
    old_id = old["table_id"]

    new = submit(client, vid, GOOD_ROWS).json()
    assert new["table_id"] != old_id
    assert new["verified"] is True

    # 旧候选不能确认（旧表已随换交删除，返回 409 而不是默默写回）
    r = client.post(f"/api/counts/{old_id}/recount",
                    data={"stop_sequence": 4, "alight": 4, "board": 0})
    assert r.status_code in (404, 409) and r.status_code != 200
    # 旧明细停止供数（下载也失效）
    assert client.get(f"/api/counts/{old_id}/csv").status_code in (404, 409)
    assert client.get(
        f"/api/counts?version_id={vid}&date=2026-11-23&trip_id=T1"
    ).json()["table_id"] == new["table_id"]


def test_capacity_validation_and_trip_must_run(client):
    vid = publish(client)
    r = submit(client, vid, GOOD_ROWS, capacity="0")
    assert r.status_code == 422
    r = submit(client, vid, GOOD_ROWS, capacity="abc")
    assert r.status_code == 422
    # T1 周六不开行
    r = submit(client, vid, GOOD_ROWS, date="2026-11-21")
    assert r.status_code == 422
    # 不存在的运行图版本
    r = submit(client, vid + 999, GOOD_ROWS)
    assert r.status_code == 404


def test_no_single_fix_message(client):
    vid = publish(client)
    # 超载到任何单站怎么改都无法同时满足：终点 5 人且第 2 站已超载
    body = submit(client, vid, [(1, 0, 50), (2, 0, 0), (3, 0, 0), (4, 50, 0)],
                  capacity="40").json()
    assert body["verified"] is False
    assert body["candidates"] == []
