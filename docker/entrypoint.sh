#!/usr/bin/env bash
# ArkSwarm 容器入口：镜像封装「环境 + 内核」，应用代码在 /app 卷里，
# 由本脚本负责首次下载安装、完整性校验与每次启动时的版本选择。
#
# 目录约定（与 app/selfupdate.py 的托管模式一致）：
#   /app/releases/<Vx.y>/   各版本目录（backend/ + frontend/dist + …）
#   /app/releases/current   软链 → 正在运行的版本目录
#   /app/data               数据目录（SQLite / 工作区 / 密钥），独立卷
#
# 启动逻辑：
#   1. current 已指向完整版本 → 直接启动（界面里更新后 execv 重启也走这里）
#   2. /app/releases 下有完整版本但没有 current → 选最高版本、建链、启动
#   3. 空卷 → 从 GitHub 下载最新 Release 装进去
#   4. 网络不可达 / 没有 Release → 用镜像自带的 /image-app 兜底
# 产物包按 selfupdate.py 的同一套安全基线处理：sha256 校验、拒绝路径穿越与符号链接、
# 校验标志文件；校验失败 / 包不安全时直接拒绝安装，不会拿旧代码顶替。
set -euo pipefail

APP_ROOT="/app"
RELEASES="$APP_ROOT/releases"
DATA_DIR="${ARKSWARM_DATA_DIR:-/app/data}"
REPO_SLUG="${ARKSWARM_UPDATE_REPO:-Sustainedg/ArkSwarm}"

# 一个版本目录算「完整」的标志文件（与 selfupdate.py 的 _PAYLOAD_MARKERS 一致）
PAYLOAD_MARKERS=(backend/app/__init__.py backend/app/main.py frontend/dist/index.html)

mkdir -p "$RELEASES" "$DATA_DIR"
# 上次被强杀（EXIT trap 没来得及跑）留下的安装临时目录
rm -rf "$RELEASES"/.install.* "$RELEASES"/.bundled.* 2>/dev/null || true

# ---- 工具函数 ----
log() { echo "[entrypoint] $*"; }
die() { echo "[entrypoint] ✗ $*" >&2; exit 1; }

# 需要清理的临时目录：成功路径显式清理，失败 / die 由 EXIT trap 兜底
CLEANUP_DIRS=()
cleanup_add() { CLEANUP_DIRS+=("$1"); }
cleanup_now() { rm -rf "$1" 2>/dev/null || true; }
cleanup_all() {
  local d
  for d in "${CLEANUP_DIRS[@]}"; do
    rm -rf "$d" 2>/dev/null || true
  done
}
trap cleanup_all EXIT

version_key() {
  # V0.44.1 → 000044001（用 0 填充对齐，sort 能直接比）
  local v="${1#V}"; v="${v#v}"
  printf '%s\n' "$v" | awk -F. '{ printf "%03d%03d%03d\n", $1+0, $2+0, $3+0 }'
}

payload_complete() {
  local dir="$1" marker
  for marker in "${PAYLOAD_MARKERS[@]}"; do
    [ -f "$dir/$marker" ] || return 1
  done
  return 0
}

pick_latest_dir() {
  local best="" best_key="" d key
  for d in "$RELEASES"/V*; do
    [ -d "$d" ] || continue
    # 半截解包 / 被删文件的目录不参与选择，避免选中后起不来
    payload_complete "$d" || continue
    key="$(version_key "$(basename "$d")")"
    if [ -z "$best_key" ] || [ "$key" -gt "$best_key" ]; then
      best="$d"; best_key="$key"
    fi
  done
  [ -n "$best" ] && echo "$best" || return 1
}

# 安全解压：约束与 app/selfupdate.py:_safe_extract 同一套。
#   拒绝绝对路径 / 父级穿越 / 符号链接 / 设备文件，限制文件数与解压体积。
# 退出码：0 成功；65 包内容不安全；66 不是有效 tar.gz；67 超出数量 / 体积上限。
safe_extract() {
  local archive="$1" dest="$2"
  python3 -I - "$archive" "$dest" <<'PY'
import os
import sys
import tarfile

UNSAFE, BAD, TOO_MANY = 65, 66, 67
MAX_FILES = 20_000
MAX_BYTES = 512 * 1024 * 1024

archive, dest = sys.argv[1], sys.argv[2]
root = os.path.realpath(dest)
count = 0
total = 0

try:
    tf = tarfile.open(archive, mode="r:gz")
except (tarfile.TarError, OSError):
    print("下载到的不是有效的 tar.gz 更新包", file=sys.stderr)
    sys.exit(BAD)

with tf:
    for member in tf.getmembers():
        parts = [p for p in member.name.replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts or ".." in parts or member.name.startswith(("/", "\\")):
            print(f"更新包内路径不安全：{member.name}", file=sys.stderr)
            sys.exit(UNSAFE)
        if member.issym() or member.islnk():
            print(f"更新包内含符号链接：{member.name}", file=sys.stderr)
            sys.exit(UNSAFE)
        if not (member.isfile() or member.isdir()):
            print(f"更新包内含不支持的条目：{member.name}", file=sys.stderr)
            sys.exit(UNSAFE)
        target = os.path.realpath(os.path.join(root, *parts))
        # resolve 后必须仍在 dest 之下（双保险，防拼接逃逸）
        if target != root and not target.startswith(root + os.sep):
            print(f"更新包内路径不安全：{member.name}", file=sys.stderr)
            sys.exit(UNSAFE)
        if member.isdir():
            os.makedirs(target, exist_ok=True)
            continue
        count += 1
        total += member.size
        if count > MAX_FILES:
            print("更新包内文件过多，已拒绝安装", file=sys.stderr)
            sys.exit(TOO_MANY)
        if total > MAX_BYTES:
            print("更新包解压后体积过大，已拒绝安装", file=sys.stderr)
            sys.exit(TOO_MANY)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        source = tf.extractfile(member)
        if source is None:
            print(f"更新包内条目无法读取：{member.name}", file=sys.stderr)
            sys.exit(BAD)
        with source, open(target, "wb") as out:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
PY
}

install_latest_release() {
  log "空卷：从 GitHub 下载最新 Release（$REPO_SLUG）"
  # 下载 Release 元数据（走代理：容器内访问 GitHub 常需要）
  local api="https://api.github.com/repos/$REPO_SLUG/releases/latest"
  local curl_args=(--silent --show-error --fail --location)
  if [ -n "${HTTP_PROXY:-}${HTTPS_PROXY:-}${http_proxy:-}${https_proxy:-}" ]; then
    curl_args+=(--proxy "${HTTPS_PROXY:-${HTTP_PROXY:-${https_proxy:-${http_proxy}}}}")
  fi
  local meta asset_url tag version
  meta="$(curl "${curl_args[@]}" --max-time 60 -H 'Accept: application/vnd.github+json' "$api")" \
    || { install_bundled "GitHub 不可达（$api）"; return; }
  tag="$(printf '%s' "$meta" | grep -oP '(?<="tag_name": ")[^"]+' | head -1 || true)"
  asset_url="$(printf '%s' "$meta" | grep -oP '(?<="browser_download_url": ")[^"]+arkswarm\.tar\.gz' | head -1 || true)"
  if [ -z "$tag" ] || [ -z "$asset_url" ]; then
    install_bundled "最新 Release（${tag:-未知版本}）没有 arkswarm.tar.gz 产物"
    return
  fi
  version="V${tag#v}"; version="V${version#V}"

  local tmp stage rc sha actual
  tmp="$(mktemp -d)"
  cleanup_add "$tmp"
  log "下载 $version ← $asset_url"
  # 产物包可能几十 MB，桥接慢链路要给足时间（元数据请求才用 60 秒）
  if ! curl "${curl_args[@]}" --max-time 600 --output "$tmp/arkswarm.tar.gz" "$asset_url"; then
    cleanup_now "$tmp"
    install_bundled "下载产物包失败"
    return
  fi
  # .sha256 资产存在就强制校验；取不到时只警告（与 selfupdate.py 的「无校验值则跳过」一致）
  if curl "${curl_args[@]}" --max-time 60 --output "$tmp/arkswarm.tar.gz.sha256" "${asset_url}.sha256" 2>/dev/null; then
    sha="$(grep -oE '[0-9a-fA-F]{64}' "$tmp/arkswarm.tar.gz.sha256" | head -1 || true)"
    if [ -n "$sha" ]; then
      sha="${sha,,}"
      actual="$(sha256sum "$tmp/arkswarm.tar.gz" | cut -d' ' -f1)"
      if [ "$actual" != "$sha" ]; then
        # 校验失败是安全信号：宁可起不来，也不悄悄退回旧代码
        die "产物包 sha256 校验失败（期望 ${sha:0:12}…，实际 ${actual:0:12}…），拒绝安装"
      fi
      log "sha256 校验通过"
    fi
  else
    log "⚠ 未取到 .sha256 资产，跳过校验"
  fi

  # 先解到同卷的临时目录，校验完整后再原子改名：中途失败不会留下半截版本
  stage="$(mktemp -d "$RELEASES/.install.XXXXXX")"
  cleanup_add "$stage"
  rc=0
  safe_extract "$tmp/arkswarm.tar.gz" "$stage" || rc=$?
  case "$rc" in
    0) ;;
    65|67) die "产物包未通过安全校验（见上一行），拒绝安装" ;;
    *) cleanup_now "$tmp"; cleanup_now "$stage"; install_bundled "产物包解压失败"; return ;;
  esac
  if ! payload_complete "$stage"; then
    cleanup_now "$tmp"; cleanup_now "$stage"
    install_bundled "产物包内容不完整"
    return
  fi
  rm -rf "$RELEASES/$version"
  mv "$stage" "$RELEASES/$version"
  cleanup_now "$tmp"
  cleanup_now "$stage"
  log "已安装 $version"
}

# 离线 / 无 Release 兜底：用镜像自带的代码（/image-app）起一个版本。
# 版本号从自带代码的 __version__ 读，落进 releases/ 后与正常版本同构 ——
# 之后 GitHub 有了 Release，界面「检测更新」照样能升级。
install_bundled() {
  local reason="${1:-GitHub 不可达}"
  local bundled="/image-app"
  if ! payload_complete "$bundled"; then
    die "$reason，且镜像没有完整的自带代码兜底（/image-app 缺少标志文件）。离线部署请先手动把版本目录放进 $RELEASES 再启动。"
  fi
  local version stage
  version="$(grep -oP '(?<=^__version__ = ")[^"]+' "$bundled/backend/app/__init__.py" 2>/dev/null || true)"
  [ -n "$version" ] || die "$reason，且读不到镜像自带代码的版本号（/image-app/backend/app/__init__.py）"
  log "$reason → 用镜像自带代码 $version 兜底"
  stage="$(mktemp -d "$RELEASES/.bundled.XXXXXX")"
  cleanup_add "$stage"
  cp -a "$bundled/." "$stage/"
  payload_complete "$stage" || die "复制镜像自带代码后仍不完整，无法启动"
  rm -rf "$RELEASES/$version"
  mv "$stage" "$RELEASES/$version"
  cleanup_now "$stage"
  log "已安装 $version（兜底；有 Release 后可在界面里点击更新）"
}

# ---- 选定要运行的版本 ----
# current 指向的版本必须完整（半截解包 / 文件被删时重新选择）
if [ -d "$RELEASES/current/" ] && ! payload_complete "$RELEASES/current"; then
  log "current 指向的版本不完整，重新选择"
  rm -f "$RELEASES/current"
fi
if [ ! -e "$RELEASES/current" ] || [ ! -d "$RELEASES/current/" ]; then
  if ! pick_latest_dir >/dev/null 2>&1; then
    install_latest_release
  fi
  TARGET="$(pick_latest_dir || true)"
  [ -n "$TARGET" ] || die "没有可运行的版本（$RELEASES 为空），请检查 Release 下载与自带代码兜底是否成功"
  ln -sfn "$TARGET" "$RELEASES/current"
  log "current → $(basename "$TARGET")"
fi

RUN_DIR="$RELEASES/current"
[ -f "$RUN_DIR/backend/requirements.txt" ] || die "$RUN_DIR 里没有 backend/requirements.txt"

# ---- 依赖（版本变了 requirements 也会变；用标记文件避免每次启动重装）----
# 镜像里预装了一份 venv 模板（/image-venv），首启直接复制它，避免容器里联网 pip install；
# requirements 与模板不一致（Release 带了新依赖）才回退到在线安装。
VENV="$APP_ROOT/venv"
MARKER="$VENV/.requirements-stamp"
STAMP="$(cat "$RUN_DIR/backend/requirements.txt" | sha256sum | cut -d' ' -f1)"
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
    python3 -m venv "$VENV"
    "$VENV/bin/pip" -q install -U pip
    "$VENV/bin/pip" -q install -r "$RUN_DIR/backend/requirements.txt"
  fi
  echo "$STAMP" > "$MARKER"
fi

# ---- 内核：镜像已预装，这里只补 PATH + 把被卸载过的补回来 ----
# 镜像里 npm install -g 装在 /usr/lib/node_modules，bin 在 /usr/bin（已在 PATH）。
# 用户在界面里「卸载」某个内核时删的是 npm 全局目录里的包，重启后这里补回去，
# 让容器始终「自带」内核；装失败不致命（界面里仍可手动装）。
ensure_kernels() {
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
      log "补装内核 $bin（$pkg）"
      npm install -g "$pkg" >/dev/null 2>&1 || log "⚠ 补装 $bin 失败，可在界面里手动安装"
    fi
  done
  # Hermes：curl 脚本安装（不是 npm 包）。它的安装要 clone 仓库 + 下 uv + 下 Python + 装依赖，
  # 网络不稳时容易卡住，所以加 5 分钟超时：超时就跳过，界面里仍可手动点装。
  if ! command -v hermes >/dev/null 2>&1; then
    log "补装内核 hermes（最多 5 分钟，超时跳过）"
    timeout 300 bash -c 'curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash' >/dev/null 2>&1 \
      || log "⚠ 补装 hermes 失败或超时，可在界面里手动安装"
  fi
}
# npm 全局 bin 与用户 ~/.local/bin 都放进 PATH，让安装后的内核命令能被找到
# 没有 npm 时保持原样（与 ensure_kernels 的兜底意图一致，不能在 set -e 下直接退出）
if command -v npm >/dev/null 2>&1; then
  NPM_BIN="$(npm prefix -g 2>/dev/null || true)/bin"
  if [ -d "$NPM_BIN" ]; then
    case ":$PATH:" in *":$NPM_BIN:"*) ;; *) export PATH="$NPM_BIN:$PATH" ;; esac
  fi
fi
if [ -d "$HOME/.local/bin" ]; then
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
fi
ensure_kernels

# ---- 启动（exec：uvicorn 接管 PID 1，界面更新时 os.execv 原地替换的就是它）----
export ARKSWARM_DATA_DIR="$DATA_DIR"
log "启动 ArkSwarm（$(basename "$RUN_DIR")）→ :${ARKSWARM_PORT:-8010}"
cd "$RUN_DIR/backend"
exec "$VENV/bin/python" -m uvicorn app.main:app \
  --host "${ARKSWARM_HOST:-0.0.0.0}" --port "${ARKSWARM_PORT:-8010}"
