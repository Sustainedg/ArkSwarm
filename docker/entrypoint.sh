#!/usr/bin/env bash
# ArkSwarm 容器入口：镜像只封装「环境」（python + 依赖），应用代码在 /app 卷里，
# 由本脚本负责首次下载安装与每次启动时的版本选择。
#
# 目录约定（与 app/selfupdate.py 的托管模式一致）：
#   /app/releases/<Vx.y>/   各版本目录（backend/ + frontend/dist + …）
#   /app/releases/current   软链 → 正在运行的版本目录
#   /app/data               数据目录（SQLite / 工作区 / 密钥），独立卷
#
# 启动逻辑：
#   1. current 已指向有效版本 → 直接启动（界面里更新后 execv 重启也走这里）
#   2. /app/releases 下有版本但没有 current → 选最高版本、建链、启动
#   3. 空卷 → 从 GitHub 下载最新 Release 装进去（离线时给出清晰报错）
set -euo pipefail

APP_ROOT="/app"
RELEASES="$APP_ROOT/releases"
DATA_DIR="${ARKSWARM_DATA_DIR:-/app/data}"
REPO_SLUG="${ARKSWARM_UPDATE_REPO:-Sustainedg/ArkSwarm}"

mkdir -p "$RELEASES" "$DATA_DIR"

# ---- 工具函数 ----
log() { echo "[entrypoint] $*"; }
die() { echo "[entrypoint] ✗ $*" >&2; exit 1; }

version_key() {
  # V0.44.1 → "0 044 001"（用 0 填充对齐，sort 能直接比）
  local v="${1#V}"; v="${v#v}"
  printf '%s\n' "$v" | awk -F. '{ printf "%03d%03d%03d\n", $1+0, $2+0, $3+0 }'
}

pick_latest_dir() {
  local best="" best_key="" d key
  for d in "$RELEASES"/V*; do
    [ -d "$d" ] || continue
    key="$(version_key "$(basename "$d")")"
    if [ -z "$best_key" ] || [ "$key" -gt "$best_key" ]; then
      best="$d"; best_key="$key"
    fi
  done
  [ -n "$best" ] && echo "$best" || return 1
}

install_latest_release() {
  log "空卷：从 GitHub 下载最新 Release（$REPO_SLUG）"
  # 下载 Release 元数据（走代理：容器内访问 GitHub 常需要）
  local api="https://api.github.com/repos/$REPO_SLUG/releases/latest"
  local curl_args=(--silent --show-error --fail --location --max-time 60)
  if [ -n "${HTTP_PROXY:-}${HTTPS_PROXY:-}${http_proxy:-}${https_proxy:-}" ]; then
    curl_args+=(--proxy "${HTTPS_PROXY:-${HTTP_PROXY:-${https_proxy:-${http_proxy}}}}")
  fi
  local meta asset_url tag version
  meta="$(curl "${curl_args[@]}" -H 'Accept: application/vnd.github+json' "$api")" \
    || die "访问 GitHub 失败（$api）。离线部署请先手动把版本目录放进 $RELEASES 再启动。"
  tag="$(printf '%s' "$meta" | grep -oP '(?<="tag_name": ")[^"]+' | head -1)"
  asset_url="$(printf '%s' "$meta" | grep -oP '(?<="browser_download_url": ")[^"]+arkswarm\.tar\.gz' | head -1)"
  [ -n "$tag" ] && [ -n "$asset_url" ] \
    || die "最新 Release（$tag）没有 arkswarm.tar.gz 产物。请用 scripts/release.sh 发布。"
  version="V${tag#v}"; version="V${version#V}"

  local tmp sha
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  log "下载 $version ← $asset_url"
  curl "${curl_args[@]}" --output "$tmp/arkswarm.tar.gz" "$asset_url" \
    || die "下载产物包失败"
  # .sha256 资产存在就校验
  if curl "${curl_args[@]}" --output "$tmp/arkswarm.tar.gz.sha256" "${asset_url}.sha256" 2>/dev/null; then
    sha="$(grep -oE '[0-9a-f]{64}' "$tmp/arkswarm.tar.gz.sha256" | head -1 || true)"
    if [ -n "$sha" ]; then
      echo "$sha  $tmp/arkswarm.tar.gz" | sha256sum --check --strict - \
        || die "产物包 sha256 校验失败"
      log "sha256 校验通过"
    fi
  fi

  mkdir -p "$RELEASES/$version"
  tar -xzf "$tmp/arkswarm.tar.gz" -C "$RELEASES/$version"
  [ -f "$RELEASES/$version/backend/app/__init__.py" ] \
    || die "产物包内容不完整（缺 backend/app/__init__.py）"
  log "已安装 $version"
}

# ---- 选定要运行的版本 ----
if [ ! -e "$RELEASES/current" ] || [ ! -d "$RELEASES/current/" ]; then
  if ! pick_latest_dir >/dev/null 2>&1; then
    install_latest_release
  fi
  TARGET="$(pick_latest_dir)"
  ln -sfn "$TARGET" "$RELEASES/current"
  log "current → $(basename "$TARGET")"
fi

RUN_DIR="$RELEASES/current"
[ -f "$RUN_DIR/backend/requirements.txt" ] || die "$RUN_DIR 里没有 backend/requirements.txt"

# ---- 依赖（版本变了 requirements 也会变；用标记文件避免每次启动重装）----
VENV="$APP_ROOT/venv"
MARKER="$VENV/.requirements-stamp"
STAMP="$(cat "$RUN_DIR/backend/requirements.txt" | sha256sum | cut -d' ' -f1)"
if [ ! -x "$VENV/bin/python" ] || [ "$(cat "$MARKER" 2>/dev/null || true)" != "$STAMP" ]; then
  log "准备虚拟环境（requirements 变化或首次启动）"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" -q install -U pip
  "$VENV/bin/pip" -q install -r "$RUN_DIR/backend/requirements.txt"
  echo "$STAMP" > "$MARKER"
fi

# ---- 启动（exec：uvicorn 接管 PID 1，界面更新时 os.execv 原地替换的就是它）----
export ARKSWARM_DATA_DIR="$DATA_DIR"
log "启动 ArkSwarm（$(basename "$RUN_DIR")）→ :${ARKSWARM_PORT:-8010}"
cd "$RUN_DIR/backend"
exec "$VENV/bin/python" -m uvicorn app.main:app \
  --host "${ARKSWARM_HOST:-0.0.0.0}" --port "${ARKSWARM_PORT:-8010}"
