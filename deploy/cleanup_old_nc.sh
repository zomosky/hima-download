#!/bin/sh
# 删除超过 KEEP_MONTHS 个月的葵花【NetCDF 源文件】,只保留最近 KEEP_MONTHS 个 UTC 月。
# Zarr 产物不动(已裁剪、长期保留)。保留窗口也够每月"整月重建上个月"用到上个月的 nc。
#
# 容器内直接跑:   sh deploy/cleanup_old_nc.sh
# 宿主机 cron:    docker exec zhangmy-dev sh /workspace/hima-download/deploy/cleanup_old_nc.sh
# 先干跑核对:     DRY_RUN=1 sh deploy/cleanup_old_nc.sh
set -u

DATA="${HIMA_DATA_DIR:-/satelite_data/himawari/data}"   # <DATA>/<PROD>/<YYYYMM>/*.nc
PRODUCTS="${HIMA_CLEAN_PRODUCTS:-PAR CLP ARP}"
KEEP_MONTHS="${KEEP_MONTHS:-2}"     # 保留最近 N 个 UTC 月(含当月)
DRY_RUN="${DRY_RUN:-0}"             # 1=只打印不删

# 保留起点 KEEP_FROM = 当前 UTC 月往前 (KEEP_MONTHS-1) 个月。
# 纯 POSIX 算术,不依赖 `date -d`(兼容 busybox/alpine)。
Y=$(date -u +%Y); M=$(date -u +%m); M=$((10#$M))
tot=$((Y * 12 + (M - 1) - (KEEP_MONTHS - 1)))
KEEP_FROM=$(printf '%04d%02d' $((tot / 12)) $((tot % 12 + 1)))
echo "[$(date -u '+%F %T UTC')] 清理 nc:保留 >= ${KEEP_FROM}(最近 ${KEEP_MONTHS} 个 UTC 月) dry_run=${DRY_RUN}"

for p in $PRODUCTS; do
    [ -d "$DATA/$p" ] || continue
    for d in "$DATA/$p"/*/; do
        [ -d "$d" ] || continue
        m=$(basename "$d")
        case "$m" in [0-9][0-9][0-9][0-9][0-9][0-9]) ;; *) continue ;; esac  # 只碰 YYYYMM 目录
        [ "$m" -lt "$KEEP_FROM" ] || continue
        sz=$(du -sh "$d" 2>/dev/null | cut -f1)
        echo "  rm nc ${p} ${m} (${sz})"
        [ "$DRY_RUN" = "1" ] || rm -rf "$d"
    done
done
echo "[$(date -u '+%F %T UTC')] cleanup done"
