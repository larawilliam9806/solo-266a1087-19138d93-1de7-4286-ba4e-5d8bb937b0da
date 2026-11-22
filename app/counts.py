"""逐站车门计数的解析、整趟核验与单站候选计算。

纯标准库实现，便于单元测试。

计数表约定（CSV，UTF-8）：
- 每个停站序号一行；表头可用 ``stop_sequence,alight,board``
  （也接受下车人数/上车人数等常见别名）。
- 上下车人数均为非负整数；行必须恰好覆盖该班次的全部停站序号。

整趟核验（按停站顺序）：
- 始发前车内为空车（0 人）；
- 每站先下后上，下车人数不得超过到站车内人数；
- 离站车内人数不得超过核载人数；
- 终点站离站必须为空车。

候选站：保持其他各行不变，只改某一站的上、下车人数，就能让整趟合规的
站次。对每个候选站同时给出一组可行值（见 :func:`find_candidates`）。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

INT_RE = re.compile(r"^\d+$")

# 表头别名（比较前会 strip / 去 BOM / 英文转小写）
SEQUENCE_HEADERS = ("stop_sequence", "stop_seq", "seq", "sequence",
                    "停站序号", "站序", "站次")
ALIGHT_HEADERS = ("alight", "alight_count", "alightings", "off",
                  "下车人数", "下车")
BOARD_HEADERS = ("board", "board_count", "boardings", "on",
                 "上车人数", "上车")


@dataclass
class CountError:
    row: int | None
    field: str | None
    message: str
    file: str = "counts"

    def as_dict(self) -> dict:
        return {"file": self.file, "row": self.row,
                "field": self.field, "message": self.message}


def _norm_header(value: str) -> str:
    return value.replace("﻿", "").strip().lower()


def _pick_header(headers: list[str], aliases: tuple[str, ...]) -> str | None:
    normalized = {_norm_header(h): h for h in headers if h is not None}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return None


def _parse_nonneg_int(value: str) -> int | None:
    value = (value or "").strip()
    if not INT_RE.match(value):
        return None
    return int(value)


def parse_counts_csv(content: bytes, expected_sequences: list[int]
                     ) -> tuple[list[dict] | None, list[dict]]:
    """解析逐站计数 CSV。

    返回 ``(rows, errors)``。``rows`` 按 stop_sequence 排序，每项含
    ``stop_sequence / alight / board / row``；有任何格式错误时 rows 为
    ``None``，旧记录不会被覆盖。
    """
    errors: list[CountError] = []
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, [CountError(None, None, "文件不是有效的 UTF-8 编码").as_dict()]

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return None, [CountError(None, None, "文件为空或缺少表头").as_dict()]

    headers = list(reader.fieldnames)
    seq_col = _pick_header(headers, SEQUENCE_HEADERS)
    alight_col = _pick_header(headers, ALIGHT_HEADERS)
    board_col = _pick_header(headers, BOARD_HEADERS)
    missing = []
    if seq_col is None:
        missing.append("stop_sequence")
    if alight_col is None:
        missing.append("alight")
    if board_col is None:
        missing.append("board")
    if missing:
        return None, [CountError(1, None,
                                 f"缺少必备列: {', '.join(missing)}").as_dict()]

    expected = set(expected_sequences)
    seen: dict[int, int] = {}
    by_seq: dict[int, dict] = {}

    for lineno, raw in enumerate(reader, start=2):
        if None in raw:
            errors.append(CountError(lineno, None, "行列数多于表头列数").as_dict())
            continue
        values = [(v or "").strip() if isinstance(v, str) else ""
                  for v in raw.values()]
        if not any(values):
            continue  # 跳过完全空行

        seq_raw = raw.get(seq_col) or ""
        seq = _parse_nonneg_int(seq_raw)
        if seq is None:
            errors.append(CountError(
                lineno, "stop_sequence",
                f"stop_sequence '{seq_raw}' 不是非负整数").as_dict())
            continue

        row_ok = True
        if seq not in expected:
            errors.append(CountError(
                lineno, "stop_sequence",
                f"stop_sequence {seq} 不是该班次的停站序号").as_dict())
            row_ok = False
        if seq in seen:
            errors.append(CountError(
                lineno, "stop_sequence",
                f"stop_sequence {seq} 重复（首次出现于第 {seen[seq]} 行）").as_dict())
            row_ok = False

        alight_raw = raw.get(alight_col) or ""
        board_raw = raw.get(board_col) or ""
        alight = _parse_nonneg_int(alight_raw)
        board = _parse_nonneg_int(board_raw)
        if alight is None:
            errors.append(CountError(
                lineno, "alight",
                f"下车人数 '{alight_raw}' 不是非负整数").as_dict())
            row_ok = False
        if board is None:
            errors.append(CountError(
                lineno, "board",
                f"上车人数 '{board_raw}' 不是非负整数").as_dict())
            row_ok = False

        seen.setdefault(seq, lineno)
        if row_ok and seq not in by_seq:
            by_seq[seq] = {"stop_sequence": seq, "alight": alight,
                           "board": board, "row": lineno}

    for seq in expected_sequences:
        if seq not in seen:
            errors.append(CountError(
                None, "stop_sequence",
                f"缺少 stop_sequence {seq} 的计数行").as_dict())

    if errors:
        return None, errors
    rows = [by_seq[s] for s in sorted(expected)]
    return rows, []


def evaluate_counts(rows: list[dict], capacity: int) -> list[dict]:
    """按顺序模拟整趟车门计数，返回不合规项（空列表表示整趟合规）。"""
    issues: list[dict] = []
    load = 0
    for row in rows:
        seq = row["stop_sequence"]
        alight = row["alight"]
        board = row["board"]
        if alight > load:
            issues.append({
                "stop_sequence": seq,
                "field": "alight",
                "message": (f"第 {seq} 站下车 {alight} 人超过到站车内 "
                            f"{load} 人（始发前应为空车，每站先下后上）"),
            })
        load -= alight
        if load < 0:
            # 只用于后续展示；负值本身已由上面的下车超员项记录。
            load = 0
        if load + board > capacity:
            issues.append({
                "stop_sequence": seq,
                "field": "board",
                "message": (f"第 {seq} 站离站车内 {load + board} 人，"
                            f"超过核载 {capacity} 人"),
            })
        load += board
    if load != 0:
        last_seq = rows[-1]["stop_sequence"] if rows else None
        issues.append({
            "stop_sequence": last_seq,
            "field": "final",
            "message": f"终点站离站仍有 {load} 人，未清空",
        })
    return issues


def find_candidates(rows: list[dict], capacity: int) -> list[dict]:
    """找出所有“只改本站上下车人数即可整趟合规”的站次。

    对每个候选站返回一组可行值。数学推导：设第 i 站后原车内人数为 L[i]，
    全表净增量为 L[N]。只改第 j 站时，为使终点清空，j 站之后的累计人数
    必须整体平移 ``-L[N]``；因此新第 j 站离站人数固定为
    ``L[j] - L[N]``，且必须落在 ``[0, capacity]``。j 之前的各行必须原本
    就合规，j 之后的行在平移后仍须满足“先下后上”和核载限制。
    """
    if not rows:
        return []
    a = [r["alight"] for r in rows]
    b = [r["board"] for r in rows]
    n = len(rows)
    loads = [0] * n
    running = 0
    for i in range(n):
        running += b[i] - a[i]
        loads[i] = running

    final_load = loads[-1]
    candidates: list[dict] = []
    prior_ok = True
    for j in range(n):
        prev_load = loads[j - 1] if j > 0 else 0
        if prior_ok:
            target = loads[j] - final_load
            if 0 <= target <= capacity:
                downstream_ok = True
                for i in range(j + 1, n):
                    if a[i] > loads[i - 1] - final_load:
                        downstream_ok = False
                        break
                    if loads[i] - final_load > capacity:
                        downstream_ok = False
                        break
                if downstream_ok:
                    # 只改第 j 站时其离站人数固定为 target。可行组合为
                    # alight ∈ [max(0, prev_load-target), prev_load]，
                    # board = alight - (prev_load - target)；
                    # 取最接近原值的一组。
                    lo = max(0, prev_load - target)
                    hi = prev_load
                    suggested_alight = min(max(a[j], lo), hi)
                    suggested_board = target - (prev_load - suggested_alight)
                    candidates.append({
                        "stop_sequence": rows[j]["stop_sequence"],
                        "alight": suggested_alight,
                        "board": suggested_board,
                        "original_alight": a[j],
                        "original_board": b[j],
                    })

        # 第 j 行要成为“j 之前原本合规”的一部分，供 j+1 及以后判断。
        if a[j] > prev_load or loads[j] > capacity:
            prior_ok = False

    return candidates
