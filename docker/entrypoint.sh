#!/usr/bin/env bash
# ArkSwarm 容器入口：镜像封装 Python / Node / git 环境，并内置一份当前代码与 venv
# 作为离线兜底；应用版本仍由 /app 卷与更新器管理。
#
# 目录约定（与 app/selfupdate.py 的托管模式一致）：
#   /app/releases/<Vx.y>/   各版本目录（backend/ + frontend/dist + …）
#   /app/releases/current   软链 → 正在运行的版本目录
#   /app/data               数据目录（SQLite / 工作区 / 密钥），独立卷
#   /app/data/home          HOME：内核安装态、用户配置与会话（独立卷内，重建不丢）
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

# HOME 指向数据卷：npm 全局包与各内核自己的 ~/.claude / ~/.codex / ~/.hermes /
# ~/.gemini 都跟着数据卷走。允许运维覆盖 HOME / NPM_CONFIG_PREFIX，未覆盖时取默认值。
export HOME="${HOME:-$DATA_DIR/home}"
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-$HOME/.local}"
case ":${PATH:-}:" in
  *":$HOME/.local/bin:"*) ;;
  *) export PATH="$HOME/.local/bin:${PATH:-/usr/local/bin:/usr/bin:/bin}" ;;
esac

mkdir -p "$RELEASES" "$DATA_DIR" "$HOME" "$NPM_CONFIG_PREFIX"

# ---- 工具函数 ----
log() { echo "[entrypoint] $*"; }
die() { echo "[entrypoint] ✗ $*" >&2; exit 1; }

#: 安装期代理：只作用于「下 Release / 装依赖 / 装内核」这些一次性动作，
#: 不污染智能体运行时（它们要直连网关）。用法：ARKSWARM_INSTALL_PROXY=http://host:port
install_proxy() {
  [ -n "${ARKSWARM_INSTALL_PROXY:-}" ] && printf '%s' "$ARKSWARM_INSTALL_PROXY" || true
}

# 在（可选）代理环境下执行命令：curl / git / uv / npm / pip 都认这几个变量。
#: 注意 pip **不认** ARKSWARM_INSTALL_PROXY，只认 HTTP(S)_PROXY —— 所以装依赖
#: 必须经由这里，否则就是一个直连 PyPI 的裸奔进程。
run_with_install_proxy() {
  local proxy
  proxy="$(install_proxy)"
  if [ -n "$proxy" ]; then
    HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy" http_proxy="$proxy" https_proxy="$proxy" "$@"
  else
    "$@"
  fi
}

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
  # 代理优先级：显式 HTTP(S)_PROXY > ARKSWARM_INSTALL_PROXY（安装期代理）
  local proxy="${HTTPS_PROXY:-${HTTP_PROXY:-${https_proxy:-${http_proxy:-$(install_proxy)}}}}"
  if [ -n "$proxy" ]; then
    curl_args+=(--proxy "$proxy")
  fi
  local meta asset_url tag version
  meta="$(curl "${curl_args[@]}" -H 'Accept: application/vnd.github+json' "$api")" \
    || { install_bundled "GitHub 不可达（$api）"; return; }
  tag="$(printf '%s' "$meta" | grep -oP '(?<="tag_name": ")[^"]+' | head -1)"
  asset_url="$(printf '%s' "$meta" | grep -oP '(?<="browser_download_url": ")[^"]+arkswarm\.tar\.gz' | head -1)"
  if [ -z "$tag" ] || [ -z "$asset_url" ]; then
    install_bundled "最新 Release（$tag）没有 arkswarm.tar.gz 产物"
    return
  fi
  version="V${tag#v}"; version="V${version#V}"

  local tmp sha
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  log "下载 $version ← $asset_url"
  if ! curl "${curl_args[@]}" --output "$tmp/arkswarm.tar.gz" "$asset_url"; then
    install_bundled "下载产物包失败"
    return
  fi
  # .sha256 资产存在就校验
  if curl "${curl_args[@]}" --output "$tmp/arkswarm.tar.gz.sha256" "${asset_url}.sha256" 2>/dev/null; then
    sha="$(grep -oE '[0-9a-f]{64}' "$tmp/arkswarm.tar.gz.sha256" | head -1 || true)"
    if [ -n "$sha" ]; then
      echo "$sha  $tmp/arkswarm.tar.gz" | sha256sum --check --strict - \
        || { install_bundled "产物包 sha256 校验失败"; return; }
      log "sha256 校验通过"
    fi
  fi

  mkdir -p "$RELEASES/$version"
  tar -xzf "$tmp/arkswarm.tar.gz" -C "$RELEASES/$version"
  [ -f "$RELEASES/$version/backend/app/__init__.py" ] \
    || { install_bundled "产物包内容不完整（缺 backend/app/__init__.py）"; return; }
  log "已安装 $version"
}

# 用镜像自带的代码（/image-app）起一个版本 —— 这是**空卷时的默认路径**。
# 构建镜像时源码就已经 COPY 进去了，「构建什么就运行什么」，版本升级统一走
# 界面里的更新机制（下载 Release → releases/<版本>/ → 原子切 current → execv 重启）。
install_bundled() {
  local reason="${1:-使用镜像自带代码}"
  local bundled="/image-app"
  if [ ! -f "$bundled/backend/app/__init__.py" ]; then
    die "$reason，但镜像里没有自带代码（/image-app）。请检查 Dockerfile 的 COPY 步骤。"
  fi
  local version
  version="$(grep -oP '(?<=^__version__ = ")[^"]+' "$bundled/backend/app/__init__.py" 2>/dev/null || echo "V0.0.0")"
  log "$reason → 安装 $version 到 $RELEASES/$version"
  mkdir -p "$RELEASES/$version"
  cp -a "$bundled/." "$RELEASES/$version/"
  log "已安装 $version（与镜像同源；后续升级走「设置 → 关于」的更新机制）"
}

# ---- 选定要运行的版本 ----
if [ ! -e "$RELEASES/current" ] || [ ! -d "$RELEASES/current/" ]; then
  if ! pick_latest_dir >/dev/null 2>&1; then
    # 空卷：默认用镜像自带代码。镜像构建时已经把源码打进去了，
    # 再去 GitHub 拉 Release 只会把「构建进去的新代码」换成线上旧包。
    # 确实想沿用旧行为（空卷先联网拉最新 Release）时设
    # ARKSWARM_FETCH_RELEASE_ON_EMPTY=1。
    if [ "${ARKSWARM_FETCH_RELEASE_ON_EMPTY:-0}" = "1" ]; then
      install_latest_release
    else
      install_bundled
    fi
  fi
  TARGET="$(pick_latest_dir)"
  ln -sfn "$TARGET" "$RELEASES/current"
  log "current → $(basename "$TARGET")"
fi

RUN_DIR="$RELEASES/current"
[ -f "$RUN_DIR/backend/requirements.txt" ] || die "$RUN_DIR 里没有 backend/requirements.txt"

# ---- 依赖（版本变了 requirements 也会变；用标记文件避免每次启动重装）----
# 镜像里预装了一份 venv 模板（/image-venv），首启直接复制它，避免容器里联网 pip install；
# requirements 与模板不一致（Release 带了新依赖）才回退到在线安装。
#
# 在线安装必须**又快又不挂住启动**：
#   * 走 ARKSWARM_INSTALL_PROXY（pip 只认 HTTP(S)_PROXY，不认那个变量名本身）；
#   * 可用 ARKSWARM_PIP_INDEX_URL 指到国内镜像（清华 / 阿里），绕开 PyPI 直连；
#   * 收紧重试与超时 —— 网络不通时快速失败并给出可执行的提示，而不是静默卡二十分钟。
VENV="$APP_ROOT/venv"
MARKER="$VENV/.requirements-stamp"
STAMP="$(cat "$RUN_DIR/backend/requirements.txt" | sha256sum | cut -d' ' -f1)"

#: pip 安装参数：镜像源可选，重试/超时写死成「快速失败」。
pip_install_args() {
  local args=(-q --disable-pip-version-check --retries 2 --timeout 15)
  local index="${ARKSWARM_PIP_INDEX_URL:-}"
  if [ -n "$index" ]; then
    args+=(-i "$index")
    # 国内镜像多是纯 https 单源，额外源反而会拖慢解析；用 host 信任即可。
    args+=(--trusted-host "$(printf '%s' "$index" | sed -E 's#^[a-z]+://##; s#/.*$##')")
  fi
  printf '%s\n' "${args[@]}"
}

if [ ! -x "$VENV/bin/python" ] || [ "$(cat "$MARKER" 2>/dev/null || true)" != "$STAMP" ]; then
  IMAGE_VENV_STAMP=""
  if [ -x /image-venv/bin/python ] && [ -f /image-venv/.requirements-stamp ]; then
    IMAGE_VENV_STAMP="$(cat /image-venv/.requirements-stamp 2>/dev/null || true)"
  fi
  if [ "$IMAGE_VENV_STAMP" = "$STAMP" ]; then
    log "从镜像预装 venv 复制虚拟环境（离线可用）"
    rm -rf "$VENV"
    cp -a /image-venv "$VENV"
  else
    log "准备虚拟环境（requirements 与镜像模板不一致，在线安装）"
    if [ -n "$(install_proxy)" ]; then
      log "pip 走安装期代理 $(install_proxy)"
    elif [ -n "${ARKSWARM_PIP_INDEX_URL:-}" ]; then
      log "pip 走镜像源 $ARKSWARM_PIP_INDEX_URL"
    else
      log "⚠ 未配置安装期代理或镜像源，将直连 PyPI（国内网络可能很慢）"
    fi
    python3 -m venv "$VENV"
    mapfile -t PIP_ARGS < <(pip_install_args)
    run_with_install_proxy "$VENV/bin/pip" install "${PIP_ARGS[@]}" -U pip
    run_with_install_proxy "$VENV/bin/pip" install "${PIP_ARGS[@]}" \
      -r "$RUN_DIR/backend/requirements.txt"
  fi
  echo "$STAMP" > "$MARKER"
fi

# ---- 内核：镜像不预装，全部装在 HOME 数据卷（$NPM_CONFIG_PREFIX）里 ----
# 这样 Claude Code / Codex / Gemini 的安装态跟着数据卷走，容器重建不需要重装。
# 用户在界面里「卸载」某个内核时删的是 HOME 下 npm 全局目录里的包，重启后这里补回去；
# 装失败不致命（界面里仍可手动装）。网络不通时可用 ARKSWARM_SKIP_KERNEL_INSTALL=1 跳过。
ensure_kernels() {
  # 内核已经物化在数据卷里、或运维自己管理内核时，可跳过启动期补装，
  # 避免 GitHub / npm 不通时把启动拖住（界面里仍可手动安装）。
  if [ "${ARKSWARM_SKIP_KERNEL_INSTALL:-0}" = "1" ]; then
    log "跳过启动期内核补装（ARKSWARM_SKIP_KERNEL_INSTALL=1）"
    return 0
  fi
  command -v npm >/dev/null 2>&1 || return 0
  local pkg
  for pkg in @anthropic-ai/claude-code @openai/codex @google/gemini-cli; do
    local bin
    case "$pkg" in
      @anthropic-ai/claude-code) bin=claude ;;
      @openai/codex)             bin=codex ;;
      @google/gemini-cli)        bin=gemini ;;
    esac
    if ! command -v "$bin" >/dev/null 2>&1; then
      log "补装内核 $bin（$pkg，最多 5 分钟）"
      timeout 300 run_with_install_proxy npm install -g "$pkg" >/dev/null 2>&1 \
        || log "⚠ 补装 $bin 失败或超时，可在界面里手动安装"
    fi
  done
  # Hermes：curl 脚本安装（不是 npm 包）。它要 clone 仓库 + 下 uv / Python / Node +
  # 装依赖，慢网络下 5 分钟远远不够；默认给 20 分钟，可用 ARKSWARM_KERNEL_INSTALL_TIMEOUT 覆盖。
  if ! command -v hermes >/dev/null 2>&1; then
    log "补装内核 hermes（最多 ${ARKSWARM_KERNEL_INSTALL_TIMEOUT:-1200} 秒，超时跳过）"
    timeout "${ARKSWARM_KERNEL_INSTALL_TIMEOUT:-1200}" \
      run_with_install_proxy bash -c 'curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash' \
      >/dev/null 2>&1 \
      || log "⚠ 补装 hermes 失败或超时，可在界面里手动安装"
  fi
}

# ---- 技能播种：让「技能」页在内核刚装好时就有内容 ----
# 各内核的内置技能由**各自的安装器 / 首次运行**播种进 HOME，镜像不预置：
#   Codex  → ~/.codex/skills/.system（`codex debug prompt-input` 触发，离线可跑）
#   Hermes → ~/.hermes/skills（`hermes --run-module tools.skills_sync`，幂等）
#   Claude → ~/.claude/skills（用户技能，随用户自己的配置走；镜像不代建）
# 只做**幂等补齐**：已有技能就跳过，绝不覆盖用户改过的内容。
seed_kernel_skills() {
  [ "${ARKSWARM_SKIP_KERNEL_INSTALL:-0}" = "1" ] && return 0

  if command -v codex >/dev/null 2>&1 && [ ! -d "$HOME/.codex/skills/.system" ]; then
    log "播种 Codex 系统技能"
    timeout 120 codex debug prompt-input >/dev/null 2>&1 || true
  fi

  if command -v hermes >/dev/null 2>&1; then
    local skills_dir="$HOME/.hermes/skills"
    if [ ! -d "$skills_dir" ] \
      || [ -z "$(find "$skills_dir" -name SKILL.md -print -quit 2>/dev/null)" ]; then
      log "播种 Hermes 内置技能"
      timeout 300 hermes --run-module tools.skills_sync >/dev/null 2>&1 || true
    fi
  fi
}
# npm 全局 bin 与用户 ~/.local/bin 都放进 PATH，让安装后的内核命令能被找到
NPM_BIN="$(npm prefix -g 2>/dev/null)/bin"
[ -d "$NPM_BIN" ] && case ":$PATH:" in *":$NPM_BIN:"*) ;; *) export PATH="$NPM_BIN:$PATH" ;; esac
[ -d "$HOME/.local/bin" ] && case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
ensure_kernels
seed_kernel_skills

# ---- 启动（exec：uvicorn 接管 PID 1，界面更新时 os.execv 原地替换的就是它）----
export ARKSWARM_DATA_DIR="$DATA_DIR"
log "启动 ArkSwarm（$(basename "$RUN_DIR")）→ :${ARKSWARM_PORT:-8010}"
cd "$RUN_DIR/backend"
exec "$VENV/bin/python" -m uvicorn app.main:app \
  --host "${ARKSWARM_HOST:-0.0.0.0}" --port "${ARKSWARM_PORT:-8010}"
