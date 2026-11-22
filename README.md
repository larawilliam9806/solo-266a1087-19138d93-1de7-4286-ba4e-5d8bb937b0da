# 公交 GTFS 时刻表发布系统

FastAPI + SQLite + 本地文件的公交时刻表发布与查询网页。数据工程师一次性上传同一版本的
5 个 CSV（stops / routes / trips / stop_times / calendar），系统整版校验通过后才发布；
公众按「线路 + 运营日」查询有序停站时刻。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `gtfs-data` 中，容器重启数据不丢。

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

### 运行测试

```bash
pip install pytest httpx
pytest tests/ -q
```

## 网页功能

- **时刻查询**：选择运营日和线路，展示当日各班次的停站序列与到发时刻；
  跨午夜时刻显示为 `24:20:00` 并标注「+1天」。
- **车门计数**：选择已发布运行图、运营日开行的班次，填写核载人数并上传逐站
  计数 CSV；不合规的表标为「待核」并列出可单站修正的候选站次，支持复点与
  核验通过后下载（见下）。
- **发布新版本**：一次上传 5 个 CSV + 版本名称。通过则立即生效；
  失败则列出全部错误行（文件 / 行号 / 字段 / 原因），当前正式版不受影响。
- **版本记录**：查看所有版本（含失败版本）及其错误明细。

也可以用 `sample_data/valid/`（可通过）和 `sample_data/invalid/`（含典型错误）
两组样例数据在网页上直接试。

## 逐站车门计数

质控员上传某班次某运营日的逐站上下车人数，系统整趟核验；不合规的表标为
「待核」并给出可回查录像的候选站次。计数表以 (运行图版本, 运营日, 班次)
为键，每个键任何时刻只有一张当前表。

### 计数 CSV 格式

每个停站序号一行，必须恰好覆盖该班次全部停站序号：

```csv
stop_sequence,alight,board
1,0,10
2,3,2
3,5,0
4,4,0
```

- 表头也接受 `停站序号/站序`、`下车人数`、`上车人数` 等别名。
- 上下车人数必须是非负整数；缺失、重复或未知的停站序号视为格式错误。
- 格式错误时整表拒绝保存，**已存记录与核验结果保持不变**。

### 整趟核验

按停站顺序模拟：始发前空车 → 每站先下后上（下车人数不得超过到站车内人数）
→ 离站不超核载人数 → 终点站离站为空车。

### 待核与候选

不合规的整表状态为 `pending`（待核）。系统列出所有**只改本站上下车人数**
就能让整趟合规的站次，每个站次给出一组可行值（在满足先下后上与非负整数的
前提下最接近原值）。没有任何候选时提示扩大复核范围。

### 复点与生效

质控员对当前待核表的一个候选站录入实际复点值：

- 只有复点后整趟核验通过才生效，整表转为 `verified`；
- 复点无效、录入格式错误或站次不是当前候选时拒绝，已存记录不变；
- 网页与接口都保留整表上传时的原值，供对照复点值。

### 整表换交与供数

- 重新上传同一 (运行图, 运营日, 班次) 的计数表即整表换交：新表按自身内容
  重新核验，旧表在同一事务内被替换。换交失败（含格式错误）会整体回滚。
- 换交后旧表的候选不能再确认（409），旧明细也不再供数（下载 409）。
- 只有 `verified` 且仍是当前表的计数才能下载 CSV（UTF-8 BOM，供客流日报）。

## 校验规则

任一规则不满足，整版拒绝发布：

1. **结构**：必备列齐全；字段类型合法（非负整数序号、YYYYMMDD 日期、0/1 星期标志、
   HH:MM:SS 时刻）。
2. **表内唯一**：`stop_id` / `route_id` / `service_id` / `trip_id` 各自唯一；
   `stop_times` 内 `(trip_id, stop_sequence)` 唯一。
3. **跨表引用**：`trips.route_id → routes`、`trips.service_id → calendar`、
   `stop_times.trip_id → trips`、`stop_times.stop_id → stops`；
   每个 trip 至少有一条 stop_times。
4. **时刻递增**：同一 trip 按 `stop_sequence` 排序后，每行 `departure ≥ arrival`，
   且下一站到站时刻严格大于上一站发车时刻。
5. **跨午夜**：时刻允许 `HH ≥ 24`（如 `25:10:00`），内部统一换算为
  「自运营日 00:00 起的秒数」比较与存储；`24:10` 之后出现 `23:59` 视为倒退报错。

## 发布与并发语义

- **整版原子发布**：所有校验在内存中完成；通过后在**单个 SQLite 事务**内写入
  整版数据并拨动 `meta.current_version_id` 指针。查询方要么看到旧完整版本、
  要么看到新完整版本，绝不出现半新半旧。
- **失败保旧**：校验失败的版本只记录 `versions(status='failed')` 和错误明细，
  不写入任何时刻数据、不触碰指针。
- **版本不可变**：已发布版本的数据永不修改或删除；请求开始时读一次指针，
  之后该请求内所有查询都固定读取同一版本，换版期间查询结果始终完整一致。
- SQLite 开启 **WAL**，发布写入不阻塞并发查询。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/versions` | multipart 上传 `label` + 5 个 CSV；成功 201，失败 422 并返回错误行 |
| GET | `/api/versions` | 版本列表（含当前正式版指针） |
| GET | `/api/versions/current` | 当前正式版 |
| GET | `/api/versions/{id}` | 版本详情与错误明细 |
| GET | `/api/routes` | 当前版本的线路列表 |
| GET | `/api/routes/{route_id}/schedule?date=YYYY-MM-DD` | 该运营日的班次与有序停站时刻 |
| GET | `/api/counts/versions` | 已发布运行图列表（可用于计数核验） |
| GET | `/api/counts/trips?date=&version_id=` | 该运营日开行的班次及计数表状态 |
| POST | `/api/counts` | multipart 上传 `version_id/date/trip_id/capacity/counts`；整表换交并核验，201 |
| GET | `/api/counts?version_id=&date=&trip_id=` | 当前计数表、核验结果与候选站次 |
| POST | `/api/counts/{table_id}/recount` | 候选站复点（`stop_sequence/alight/board`）；整趟通过才生效 |
| GET | `/api/counts/{table_id}/csv` | 下载核验通过且仍为当前表的计数 CSV |

### 命令行示例

```bash
# 发布
curl -F "label=2026冬季运行图" \
     -F "stops=@sample_data/valid/stops.csv" \
     -F "routes=@sample_data/valid/routes.csv" \
     -F "trips=@sample_data/valid/trips.csv" \
     -F "stop_times=@sample_data/valid/stop_times.csv" \
     -F "calendar=@sample_data/valid/calendar.csv" \
     http://localhost:8000/api/versions

# 查询夜班线路（含 24:00 之后的跨午夜班次）
curl "http://localhost:8000/api/routes/R2/schedule?date=2026-11-23"
```

## 目录结构

```
app/
  main.py        FastAPI 端点（发布 / 查询 / 版本）
  gtfs.py        CSV 解析与整版校验（纯标准库）
  counts.py      逐站计数解析、整趟核验、单站候选（纯标准库）
  counts_api.py  车门计数端点（上传换交 / 候选 / 复点 / 下载）
  db.py          SQLite schema、原子发布、版本指针、计数表
  static/index.html  网页（查询 / 计数 / 发布 / 版本记录）
tests/           pytest 单元与集成测试
sample_data/     可通过与含错误的样例 CSV
Dockerfile / docker-compose.yml
```
