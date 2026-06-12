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
