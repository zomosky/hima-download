# hima-download

从 JAXA P-Tree FTP（`ftp.ptree.jaxa.jp`）下载葵花 8/9 卫星 L2 级光伏相关产品的命令行工具。

面向光伏/电价预测场景：每小时只取 `:00` 和 `:10` 两帧（互为冗余，应对卫星数据偶尔延迟/缺失），三个产品全套覆盖辐照、云、气溶胶。

## 产品清单

| 代码 | 产品 | 时间分辨率 | 空间分辨率 | 用途 |
|------|------|-----------|-----------|------|
| **PAR** | 短波辐射 SWR + 光合有效辐射 | 10 min | 5 km 全圆盘 | 直接对应 PV 入射辐照 |
| **CLP** | 云属性（云光学厚度 COT、有效粒径、云顶、云相态，仅日间） | 10 min | 5 km 全圆盘 | 云对 PV 出力波动的主因 |
| **ARP** | 气溶胶（AOD 等，仅日间） | 10 min | 5 km 全圆盘 | 散射辐照修正 |

数据起始日期：**2015-07-07**（葵花 8 号上线）。Himawari-8 / 9 自动切换。

## 安装

依赖 [uv](https://github.com/astral-sh/uv)（已要求 Python ≥ 3.12）。

```bash
git clone <repo>
cd hima-download
uv sync
cp .env.example .env   # 已预填 FTP 凭据
```

## 配置

工具支持两种配置叠加（按优先级从低到高）：

1. **`.env`** —— 持久化基础默认（凭据、并发、日志路径等）
2. **`config.yaml`** —— 一次任务的运行参数（mode、时间范围、目标分钟等）；仅对 `run` 子命令生效
3. **CLI 参数** —— 临时覆盖单次调用

### `.env`（参考 `.env.example`）

```ini
HIMA_FTP_HOST=ftp.ptree.jaxa.jp
HIMA_FTP_USER=<your_uid>
HIMA_FTP_PASS=<your_pw>

HIMA_DATA_DIR=./data           # 数据落地根目录
HIMA_CONCURRENCY=4             # 并发连接数（FTP 服务器建议 ≤ 6）
HIMA_PRODUCTS=PAR,CLP,ARP      # 下载哪些产品
HIMA_MINUTES=00,10             # 每小时下哪几帧 (t0 + t+10min 冗余)
HIMA_PAR_INCLUDE_JAPAN=false   # 是否额外下 1km 日本区文件
HIMA_REALTIME_WINDOW_HOURS=6   # 实时模式扫描窗口
HIMA_REALTIME_INTERVAL_SEC=300 # 实时模式轮询间隔
HIMA_MAX_RETRIES=5
HIMA_RETRY_BACKOFF_SEC=10

# 日志（同时写 stderr 与文件）
HIMA_LOG_FILE=./logs/hima-download.log
HIMA_LOG_ROTATION=50 MB
HIMA_LOG_RETENTION=14 days
```

### `config.yaml`（参考 `config.example.yaml`）

声明式地描述一次任务："下历史" 还是 "追新"、什么时间段、放哪。

```yaml
mode: backfill                 # backfill | realtime

data_dir: ./data
products: [PAR, CLP, ARP]
minutes: ["00", "10"]
concurrency: 4

backfill:
  start: "2026-06-09T01:00"    # UTC，含
  end:   "2026-06-09T02:00"    # UTC，不含
  dry_run: false

realtime:
  window_hours: 6
  interval_sec: 300
  once: false                  # true = 单次循环退出（cron 用）

log_file: ./logs/hima-download.log
log_rotation: "50 MB"
log_retention: "14 days"
```

用法：

```bash
uv run hima-download run --config config.yaml
```

`config.yaml` 里出现的字段会覆盖 `.env` 默认。

## 文件落地

```
data/<PRODUCT>/<YYYYMM>/<filename>.nc
```

示例：
```
data/PAR/202606/H09_20260609_0100_RFL021_FLDK.02801_02401.nc
data/CLP/202606/NC_H09_20260609_0100_L2CLP010_FLDK.02401_02401.nc
data/ARP/202606/NC_H09_20260609_0100_L2ARP031_FLDK.02401_02401.nc
```

文件名时间戳是 **UTC**。北京时间换算：`UTC + 8h`。

## 命令

> 所有时间参数都是 **UTC**。支持格式：`YYYY-MM-DD`、`YYYY-MM-DDTHH:MM`、`YYYY-MM-DD HH:MM`、`YYYYMMDDHHMM`。

### `info` — 查看当前配置

```bash
uv run hima-download info
```

### `probe` — FTP 健康检查 + 各产品当前延迟

```bash
uv run hima-download probe
```

输出示例：
```
Login OK. Root entries: 2
  PAR  /pub/himawari/L2/PAR/021/202606/11/02  2 files, latest delay ≈ 0h
  CLP  /pub/himawari/L2/CLP/010/202606/11/01  3 files, latest delay ≈ 1h
  ARP  /pub/himawari/L2/ARP/031/202606/11/01  3 files, latest delay ≈ 1h
```

### `backfill` — 历史回填 / 补缺

下载 `[start, end)` UTC 区间内所有目标时刻；本地已存在且大小匹配的自动跳过，相当于"补缺"。

```bash
# 北京时间 2026-06-09 09:00 那一小时 (UTC 01:00)
uv run hima-download backfill 2026-06-09T01:00 2026-06-09T02:00

# 整一天 UTC
uv run hima-download backfill 2026-06-09 2026-06-10

# 只规划不下载，看看会下哪些文件、多大
uv run hima-download backfill 2026-06-09 2026-06-10 --dry-run

# 临时覆盖产品列表
uv run hima-download backfill 2026-06-09 2026-06-10 --products PAR
```

### `verify` — 检查某区间缺哪些文件

不下载，只报告本地缺失。适合定期巡检。

```bash
uv run hima-download verify 2026-06-01 2026-06-10
```

### `realtime` — 实时模式

每 `interval_sec` 扫一次最近 `window_hours` 小时窗口，发现新生成的文件就下。

**逻辑（非"猜文件名"）**：
1. `now` = 当前 UTC 整分时刻
2. 生成 `[now - window_hours, now+1min)` 范围内、`minutes` 列表对应的全部预期时刻
3. 对每个 `(产品 × 小时)` LIST 一次远端目录
4. 用时间戳子串匹配出该时刻应有的文件 `(name, size)`
5. 与本地 `data/<PROD>/<YYYYMM>/<file>` 比对：缺失或字节数不符 → 加入下载队列
6. 4 个工作线程并发下载，每个线程独立 FTP 连接

```bash
# 常驻进程
uv run hima-download realtime

# 单次循环（适合 cron 调度）
uv run hima-download realtime --once

# 临时覆盖参数
uv run hima-download realtime --window-hours 3 --interval-sec 600
```

#### 定时调度（推荐 cron 方案）

每 10 分钟跑一次单次循环：

```cron
*/10 * * * * cd /path/to/hima-download && /opt/homebrew/bin/uv run hima-download realtime --once >> data/cron.log 2>&1
```

### `run` — 用 YAML 配置驱动

读 `config.yaml`，按 `mode` 自动分发到 backfill 或 realtime。

```bash
uv run hima-download run --config config.yaml
```

适合：把"下哪段、放哪、并发几、是历史还是追新"统一写到一个版本化文件里，CI/部署时一键跑。

## 典型用法场景

| 场景 | 命令 |
|------|------|
| 初次部署，先做健康检查 | `uv run hima-download probe` |
| 训练用，回填过去 1 年 | `uv run hima-download backfill 2025-06-09 2026-06-09` |
| 日常运行，自动追新 | cron + `realtime --once` |
| 补一个数据缺口 | `uv run hima-download backfill <start> <end>`（已存在的自动跳过） |
| 检查哪些时刻还缺 | `uv run hima-download verify <start> <end>` |

## 数据量估算（默认配置 PAR+CLP+ARP，每小时 2 帧）

- **~75 MB / 小时**
- **~1.3 GB / 天**（CLP/ARP 仅日间所以略低于满 2× ARP+CLP 估计）
- **~470 GB / 年**
- 跨国 FTP 实测速度 0.1–0.4 MB/s（受网络波动影响大）

## 健壮性

- **原子写入**：先写 `<file>.part`，下完字节数校验通过后才 `rename` 到正式名。中断不会留半文件。
- **指数退避重试**：默认 5 次，可在 `.env` 调整；进度条会在每次重试前回退本次累计字节，避免虚高。
- **断点幂等**：重跑相同区间会跳过已存在且大小匹配的文件。
- **目录不存在优雅处理**：缺失目录（如 CLP/ARP 夜间或尚未生成的时刻）返回空清单，不抛异常。
- **并发独立连接**：每个工作线程独立 FTP 会话，互不干扰；进度条按字节实时刷新（线程安全）。

## 日志

- 同时输出到 stderr 和 `HIMA_LOG_FILE`（默认 `./logs/hima-download.log`）
- 按大小轮转（默认 50MB 切一份），按时长保留（默认 14 天）
- 多线程写入安全（`enqueue=True`）
- 想看更详细日志加 `-v / --verbose`

## 再加工：裁剪 → Zarr（`hima-restore`）

下载得到的是 JAXA 全盘网格 NetCDF（规则经纬度、0.05°、纬度降序、经度 0–360）。`hima-restore`
子命令把它们裁剪到一个经纬度框（默认中国区），并按 **每个产品每个月** 拼成一个扁平 Zarr
（沿新建的 `time` 维），方便下游做区域时序 / 训练，直接 `xr.open_zarr(...)['AOT']` 取
`(time, lat, lon)`，无需再碰 NetCDF。它不联网，只读 `data/` 下已下载的文件。

依赖随 `uv sync` 一并装好（xarray / netcdf4 / zarr / dask / numcodecs）。

### 命令

```bash
# 单个 (产品, 月)：可用 --start/--end 限定一小段做快速验证
uv run hima-restore run ARP 202601 --config restore.yaml
uv run hima-restore run ARP 202601 --start 2026-01-01T00:00 --end 2026-01-01T02:00 \
    --output-dir /tmp/hima_out

# 扫一遍 data/ 下全部产品/全部月，幂等，适合 cron
uv run hima-restore scan-once --config restore.yaml

uv run hima-restore list-products
```

公共 flag（`run` / `scan-once` 通用，覆盖 YAML）：`--data-dir` / `--output-dir`
/ `--bbox W,E,S,N` / `--chunks time=-1,latitude=256,longitude=256` / `--workers N`
/ `--no-progress` / `--force`；`scan-once` 另有 `--products PAR,CLP`。

**并发与进度**：读文件、裁剪、编码、写 Zarr 走 dask 线程调度并发，`--workers N`（或
YAML `workers:`，默认 `min(cpu, 4)`）设线程数。交互终端下默认显示 dask 进度条（打开阶段 +
写出阶段各一条），非交互（管道/cron）自动关闭，也可 `--no-progress` 手动关。

### 配置（`restore.yaml`，全部可选，见 `restore.example.yaml`）

```yaml
data_dir: data                          # 下载输出根（<data_dir>/<产品>/<YYYYMM>/*.nc）
output_dir: zarr                        # 产物根目录
bbox: [70.0, 140.0, 15.0, 55.0]        # west, east, south, north（中国区）
products: [PAR, CLP, ARP]              # PAR 只取宽网格 02801
chunks: {time: -1, latitude: 256, longitude: 256}   # 偏向区域时序：整段时间一块 + 空间分块
compressor: zstd                       # zstd | lz4 | blosclz | zlib | none
clevel: 3
consolidated: true
```

### 产物与读取

```
<output_dir>/<产品>/<YYYYMM>_<产品>.zarr      # 如 zarr/ARP/202601_ARP.zarr
```

```python
import xarray as xr
ds = xr.open_zarr("zarr/ARP/202601_ARP.zarr")     # consolidated，开启快
ds["AOT"].sel(latitude=slice(45, 35), longitude=slice(110, 120))   # (time, lat, lon)
ds.time.values                                     # 该月每帧的 UTC 时刻
```

### 说明

- **无 manifest**：下载侧不写清单，再加工靠 glob `data/<产品>/<YYYYMM>/*.nc`，时间从文件名
  `_YYYYMMDD_HHMM_`（UTC）解析。
- **PAR 两种网格**：同目录有 `.02801_02401`（经度 70–210，整月）和 `.02401_02401`
  （经度 80–200，仅 1 月 1–5 日)，`hima-restore` **只取 2801**，避免时刻重复。
- **变量**：保留二维科学场（含 QA），丢弃导航/辅助量（band / geometry / 标量 start/end time）。
  另丢弃 `Hour`（逐像元观测 UT）：它按每帧不同的 `add_offset` 打包，整月拼进一个 store 无法统一
  重编码（int16×1e-4 只覆盖 ±3.27h，非零点帧会溢出），且与 `time` 坐标信息重复。
- **紧凑无损**：保留源文件的 int16 打包 + `scale_factor`/`add_offset`/`missing_value`，store 体积
  约为 float32 的一半；`open_zarr` 默认会自动解码为物理量。
- **幂等**：store 记录源文件数；`scan-once` 重跑时若该月文件数未变就跳过，月内新增了帧则重建；
  半途崩溃留下的残缺 store 会被重新处理。`--force` 强制重建。

### 下载完成后自动裁剪

给下载命令加 `--restore`，下完就自动把**受影响的 (产品, 月)** 裁剪进 Zarr，无需再手动/定时跑
`hima-restore`：

- `backfill --restore`：补完这段区间后，对涉及的每个月**整月重建**（幂等，已完整则跳过）。
- `realtime --restore`：每个轮询周期下到新帧后，只把**新帧沿 `time` 维增量 append** 进当月
  store（省算力；多次 append 会让 `time` 分块变碎，想恢复整块布局跑一次
  `hima-restore scan-once --force`）。
- `--restore-config restore.yaml` 指定 bbox/output_dir/chunks；不传则用内置默认（中国区、`zarr/`）。
  裁剪读取的 `data_dir` 自动对齐下载输出，无需重复配置。

```bash
uv run hima-download backfill 2026-01-01 2026-01-02 --restore
uv run hima-download realtime --once --restore --restore-config restore.yaml
```

YAML（`config.yaml`）里也可开启：

```yaml
mode: realtime
restore: true                 # 下完自动裁剪
restore_config: restore.yaml  # 可选，缺省用内置默认
realtime:
  once: false
```

### cron 定时

```cron
# 每小时把新下载好的帧增量并入当月 Zarr（scan-once 幂等，flock 防重叠）
0 * * * * /usr/bin/flock -n /tmp/hima_restore.lock \
    sh -c 'cd /path/to/hima-download && uv run hima-restore scan-once --config restore.yaml \
        >> logs/restore.log 2>&1'
```

### 历史批量处理（`batch_restore.py`）

对已下载的历史数据一次性 / 可续跑地批量裁剪。等价于 `scan-once`，额外支持**按月区间过滤** +
**dry-run 清单** + 顶部**就地配置块**(适合一次性 backfill)：

```bash
uv run python batch_restore.py --dry-run                      # 只列出待处理 (产品,月)
uv run python batch_restore.py --products ARP,CLP --month-start 202601 --month-end 202601
uv run python batch_restore.py --force                        # 重建已存在产物
```

顶部「用户配置区」可改默认(`DATA_DIR`/`OUTPUT_DIR`/`BBOX`/`PRODUCTS`/月区间/`CHUNKS`…);
已完整产物默认跳过，可随时中断续跑。

### 读取产物（`read_example.py`）

```bash
uv run python read_example.py zarr/ARP/202601_ARP.zarr
```

演示区域时序读取:选一个中国子区域 + 一个变量，一次 `.load()` 读入(区域小只命中少量 chunk)。
注意变量是打包 int16、`open_zarr` 自动解码为物理量;白天产品(PAR/CLP/ARP)在夜间/低太阳角
时段可能整片为 `NaN`(无反演),属正常。
