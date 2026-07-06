# 部署手册(宿主机 cron + docker 容器)

架构:**宿主机 cron → `docker exec zhangmy-dev` → 容器内 `.venv` 脚本**。
数据/产物都落在挂载卷 `/satelite_data/himawari/` 下。所有时间均为 **UTC**。

- 容器:`zhangmy-dev`,项目 `/workspace/hima-download`
- 数据:`/satelite_data/himawari/data`(NetCDF)、`.../zarr`(Zarr 产物)、`.../logs`

---

## 0. 一次性准备(容器内)

```bash
docker exec -it zhangmy-dev bash
cd /workspace/hima-download
git pull                       # 确保含 Hour 修复 / HDF5 读锁 / rechunk 命令
uv sync                        # 生成 .venv/bin/hima-download、hima-restore
mkdir -p /satelite_data/himawari/{data,zarr,logs}

# .env:FTP 凭据 + 数据落地(挂载卷)
cat > .env <<'EOF'
HIMA_FTP_HOST=ftp.ptree.jaxa.jp
HIMA_FTP_USER=<你的UID>
HIMA_FTP_PASS=<你的PW>
HIMA_DATA_DIR=/satelite_data/himawari/data
HIMA_CONCURRENCY=4
HIMA_LOG_FILE=/satelite_data/himawari/logs/hima-download.log
EOF

# restore.yaml:产物落挂载卷;HDF5 读锁已修好,workers 可用 4
cat > restore.yaml <<'EOF'
output_dir: /satelite_data/himawari/zarr
bbox: [70.0, 140.0, 15.0, 55.0]
products: [PAR, CLP, ARP]
minutes: ["00", "10"]                   # 期望帧槽 -> 规则网格(须与 HIMA_MINUTES 一致)
chunks: {time: -1, latitude: 256, longitude: 256}   # time 在规则网格下自动按天分块
compressor: zstd
clevel: 3
consolidated: true
workers: 4
EOF

# 冒烟测试
./.venv/bin/hima-download probe


```

核对:`which docker`(宿主机,一般 `/usr/bin/docker`)、`/satelite_data` 是挂载卷
(`docker inspect zhangmy-dev --format '{{json .Mounts}}'`)、容器常驻
(`docker update --restart=always zhangmy-dev`)。

---

## 1. 定时任务(实时下载 + 裁剪)

把宿主机脚本就位(脚本已在仓库 `deploy/hima-realtime.sh`):

```bash
# 宿主机
docker cp zhangmy-dev:/workspace/hima-download/deploy/hima-realtime.sh /usr/local/bin/hima-realtime.sh
chmod +x /usr/local/bin/hima-realtime.sh
bash /usr/local/bin/hima-realtime.sh          # 手动跑一次验证
tail -f /var/log/hima-realtime.cron.log
```

宿主机 `crontab -e`,两条:

```cron
# ① 每 10 分钟:下新帧 + 就地写入当月规则网格 Zarr(脚本自带 flock 防重叠)
*/10 * * * * /bin/bash /usr/local/bin/hima-realtime.sh

# ② 每月 1 号 04:30:从 NetCDF 整月重建"上个月"做正确性兜底(healing + 吸收 JAXA 订正)
30 4 1 * * /usr/bin/docker exec zhangmy-dev sh -c 'cd /workspace/hima-download && m=$(date -u -d "last month" +%Y%m 2>/dev/null || date -u -v-1m +%Y%m); for p in PAR CLP ARP; do ./.venv/bin/hima-restore run $p $m --config restore.yaml --force --no-progress; done' >> /var/log/hima-rebuild.log 2>&1
```

- ①保证 Zarr 分钟级刷新(下游直接读 Zarr);缺帧=NaN 行、延时帧补下来后**就地写入固定 slot**,
  时间轴不增长、**不产生碎片**——所以旧的"每晚 rechunk 整理碎片"那条已删除、不再需要。
- ②每月一次从源头重建,顺带修复任何"数据丢失/被订正"的月(见下方幂等盲点)。
  > `rechunk` 命令仍在,但只用于把**已封口的月**合成整月单块以优化读;**别对当月可写 store 跑**
  > (会破坏按天分块、让下次就地写变贵)。

---

## 2. 下载某个时间区间(一次性 backfill)

`[start, end)` UTC,已存在且大小匹配的自动跳过。容器内跑:

```bash
docker exec zhangmy-dev sh -c 'cd /workspace/hima-download && \
  ./.venv/bin/hima-download backfill 2026-01-01 2026-02-01 --restore --restore-config restore.yaml'
```

- 只看会下什么、多大:去掉 `--restore` 加 `--dry-run`。
- 只下某产品:`--products PAR`。
- 大区间(几个月/一年)建议放后台 + 日志:

```bash
docker exec -d zhangmy-dev sh -c 'cd /workspace/hima-download && \
  ./.venv/bin/hima-download backfill 2025-01-01 2026-01-01 --restore --restore-config restore.yaml \
  >> /satelite_data/himawari/logs/backfill-2025.log 2>&1'
```

> `--restore` 走整月重建(幂等);若你想"先全下完再统一裁剪",可先不加 `--restore` 下完,
> 再用第 3 节的批量裁剪。

---

## 3. 批量裁剪历史文件

对**已经下好**的历史 NetCDF 批量裁成月度 Zarr,可续跑、可按月过滤。容器内跑:

```bash
# 先看会处理哪些(产品,月),不执行
docker exec zhangmy-dev sh -c 'cd /workspace/hima-download && \
  ./.venv/bin/python batch_restore.py --dry-run \
  --data-dir /satelite_data/himawari/data --output-dir /satelite_data/himawari/zarr'

# 实际批量裁(可加 --month-start/--month-end 限范围;已完整的默认跳过,可 --force 重建)
docker exec -d zhangmy-dev sh -c 'cd /workspace/hima-download && \
  ./.venv/bin/python batch_restore.py \
  --data-dir /satelite_data/himawari/data --output-dir /satelite_data/himawari/zarr \
  --products PAR,CLP,ARP --month-start 202501 --month-end 202512 --workers 4 \
  >> /satelite_data/himawari/logs/batch-2025.log 2>&1'
```

批量走的是整月重建 -> 直接产出**规则网格**(缺帧=NaN 行、按天分块、CLP 缺测已掩膜为 NaN),
无碎片、无需再整理。`--force` 会重建已存在产物(升级到规则网格/换 bbox 时用)。

---

## 注意事项

- **workers**:HDF5 读锁已修复,restore 侧 `workers: 4` 安全且快;网络盘上加速有限。
- **幂等盲点**:`run`/`scan-once` 判"已完成"只看 `time 步数 == 帧数`,**不校验 chunk 数据是否真在**。
  元数据在、数据丢了的坏 store 会被当成完好跳过 —— 只有 `--force` 会重建。第 1 节 ③ 的月度
  force 重建就是为此兜底。
- **日志**:cron 层在 `/var/log/hima-*.log`;业务细节在容器内 `HIMA_LOG_FILE`。
- **读取产物**:`xr.open_zarr("/satelite_data/himawari/zarr/ARP/202601_ARP.zarr")`,已解码为物理量;
  纬度降序、经度 0–360;夜间/低太阳角整片 NaN 属正常;已不含 `Hour`。
