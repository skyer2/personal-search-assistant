#!/usr/bin/env bash
# Research Agent Harness — 裸机一键安装
#
# 主目标：Alibaba Cloud Linux 4（uname … 6.6.*-*.alnx4.x86_64）
# 兼容：Alibaba Cloud Linux 3 / Anolis / openEuler / RHEL 8+ 等 dnf 系统
#
# 不安装：Docker、MySQL、RAGFlow、MCP、Redis、多 worker uvicorn
#
# ---------------------------------------------------------------------------
# 一键（root，推荐 sudo -E 保留密钥环境变量）
# ---------------------------------------------------------------------------
#   export OPENAI_API_KEY='sk-...'
#   export TAVILY_API_KEY='tvly-...'
#   curl -fsSL https://raw.githubusercontent.com/skyer2/personal-search-assistant/main/scripts/bootstrap_bare_metal.sh \
#     | sudo -E bash
#
# 已 clone 的仓库内：
#   sudo -E bash scripts/bootstrap_bare_metal.sh
#
# 常用变量：
#   REPO_URL / REPO_BRANCH     默认 GitHub main
#   HARNESS_HOME               默认 /opt/research-agent-harness
#   HARNESS_USER               默认 harness
#   OPENAI_API_KEY / TAVILY_API_KEY / OPENAI_BASE_URL
#   LLM_QWEN_MAX / LLM_COMPRESSION_MODEL
#   USE_CN_MIRROR              auto | 1 | 0
#   INSTALL_UI                 默认 1
#   START_API / START_UI       默认 1（systemd 拉起 API + Vite）
#   SKIP_CLONE                 1 = 使用当前仓库目录
#   API_PORT / UI_PORT         默认 8000 / 5173
# ---------------------------------------------------------------------------

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/skyer2/personal-search-assistant.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
HARNESS_HOME="${HARNESS_HOME:-/opt/research-agent-harness}"
HARNESS_USER="${HARNESS_USER:-harness}"
NODE_VER="${NODE_VER:-20.19.0}"
PNPM_VER="${PNPM_VER:-10.33.0}"
API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-5173}"
USE_CN_MIRROR="${USE_CN_MIRROR:-auto}"
INSTALL_UI="${INSTALL_UI:-1}"
START_API="${START_API:-1}"
START_UI="${START_UI:-1}"
INSTALL_FONTS="${INSTALL_FONTS:-1}"
SKIP_CLONE="${SKIP_CLONE:-0}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://dashscope.aliyuncs.com/compatible-mode/v1}"
LLM_QWEN_MAX="${LLM_QWEN_MAX:-qwen-max}"
LLM_COMPRESSION_MODEL="${LLM_COMPRESSION_MODEL:-qwen-turbo}"

log()  { printf '\n\033[1;36m[harness]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[harness warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[harness error]\033[0m %s\n' "$*" >&2; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,32p' "$0"
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  die "请用 root 执行（curl 管道务必加 -E 保留密钥）：

  export OPENAI_API_KEY=...
  export TAVILY_API_KEY=...
  curl -fsSL https://raw.githubusercontent.com/skyer2/personal-search-assistant/main/scripts/bootstrap_bare_metal.sh | sudo -E bash

仓库内：sudo -E bash scripts/bootstrap_bare_metal.sh"
fi

need_cmd dnf || die "需要 dnf（Alibaba Cloud Linux / openEuler / RHEL 系）。"

as_harness() {
  local env_kv=(
    HOME="/home/${HARNESS_USER}"
    PATH="/home/${HARNESS_USER}/.local/bin:/usr/local/node/bin:/usr/bin:/bin"
    UV_INSTALL_DIR="/home/${HARNESS_USER}/.local"
  )
  [[ -n "${UV_INDEX_URL:-}" ]] && env_kv+=(UV_INDEX_URL="$UV_INDEX_URL")
  [[ -n "${UV_PYTHON_INSTALL_MIRROR:-}" ]] && env_kv+=(UV_PYTHON_INSTALL_MIRROR="$UV_PYTHON_INSTALL_MIRROR")
  sudo -u "$HARNESS_USER" -H env "${env_kv[@]}" "$@"
}

append_bashrc() {
  local line="$1" file="/home/${HARNESS_USER}/.bashrc"
  touch "$file"
  grep -Fqx "$line" "$file" 2>/dev/null || printf '%s\n' "$line" >> "$file"
  chown "${HARNESS_USER}:${HARNESS_USER}" "$file"
}

detect_cn_mirror() {
  case "$USE_CN_MIRROR" in
    1|true|yes|on) return 0 ;;
    0|false|no|off) return 1 ;;
  esac
  if timedatectl 2>/dev/null | grep -qE 'Asia/Shanghai|CST'; then
    return 0
  fi
  if curl -fsS --max-time 4 https://github.com >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

CN=0
if detect_cn_mirror; then
  CN=1
  log "使用国内镜像（USE_CN_MIRROR=$USE_CN_MIRROR）"
  export UV_INDEX_URL="${UV_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
  export UV_PYTHON_INSTALL_MIRROR="${UV_PYTHON_INSTALL_MIRROR:-https://ghproxy.net/https://github.com/astral-sh/python-build-standalone/releases/download}"
fi

if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  log "系统: ${PRETTY_NAME:-$ID}  kernel=$(uname -r)"
else
  warn "没有 /etc/os-release，按通用 EL 继续"
fi

# ---------------------------------------------------------------------------
# 1. 系统包
# ---------------------------------------------------------------------------
log "安装系统编译与运行依赖"
dnf -y install sudo python3 git curl tar xz unzip which findutils \
  gcc gcc-c++ make \
  openssl-devel bzip2-devel libffi-devel \
  zlib-devel readline-devel sqlite-devel xz-devel \
  wget ca-certificates shadow-utils fontconfig \
  || dnf -y install sudo python3 git curl tar xz gcc make openssl-devel zlib-devel sqlite-devel ca-certificates shadow-utils

if [[ "$INSTALL_FONTS" == "1" ]]; then
  dnf -y install wqy-microhei-fonts wqy-zenhei-fonts google-droid-sans-fonts 2>/dev/null \
    || dnf -y install wqy-microhei-fonts 2>/dev/null \
    || warn "未装上文泉驿字体，PDF 中文会回退 STSong-Light"
fi

if need_cmd firewall-cmd && systemctl is-active --quiet firewalld 2>/dev/null; then
  log "开放本机防火墙 ${API_PORT}/tcp ${UI_PORT}/tcp（阿里云 ECS 还要在安全组放行）"
  firewall-cmd --permanent --add-port="${API_PORT}/tcp" || true
  firewall-cmd --permanent --add-port="${UI_PORT}/tcp" || true
  firewall-cmd --reload || true
else
  warn "本机无 firewalld。阿里云请在 ECS 安全组放行 ${API_PORT} 与 ${UI_PORT}。"
fi

# ---------------------------------------------------------------------------
# 2. 运行用户
# ---------------------------------------------------------------------------
if ! id "$HARNESS_USER" >/dev/null 2>&1; then
  log "创建用户 $HARNESS_USER"
  useradd -m -d "/home/${HARNESS_USER}" -s /bin/bash "$HARNESS_USER" || true
fi
id "$HARNESS_USER" >/dev/null 2>&1 || die "无法创建用户 $HARNESS_USER"
mkdir -p "/home/${HARNESS_USER}/.local/bin" /usr/local/node
chown -R "${HARNESS_USER}:${HARNESS_USER}" "/home/${HARNESS_USER}"

# ---------------------------------------------------------------------------
# 3. Python 3.12 + uv
# ---------------------------------------------------------------------------
if dnf list python3.12 >/dev/null 2>&1; then
  dnf -y install python3.12 python3.12-devel python3.12-pip 2>/dev/null || true
fi

UV_BIN="/home/${HARNESS_USER}/.local/bin/uv"
export UV_INSTALL_DIR="/home/${HARNESS_USER}/.local"

install_uv() {
  if [[ -x "$UV_BIN" ]]; then
    log "uv 已存在: $UV_BIN"
    return 0
  fi
  log "安装 uv → $UV_BIN"
  mkdir -p "/home/${HARNESS_USER}/.local/bin"
  if [[ "$CN" -eq 1 ]]; then
    curl -fLsS https://ghproxy.net/https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz -o /tmp/uv.tgz \
      || curl -fLsS https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz -o /tmp/uv.tgz
    rm -rf /tmp/uv-extract
    mkdir -p /tmp/uv-extract
    tar -xzf /tmp/uv.tgz -C /tmp/uv-extract
    UV_SRC="$(find /tmp/uv-extract -type f -name uv | head -n1)"
    [[ -n "$UV_SRC" ]] || die "解压 uv 失败"
    install -m 0755 "$UV_SRC" "$UV_BIN"
  else
    curl -LsSf https://astral.sh/uv/install.sh | sudo -u "$HARNESS_USER" -H env UV_INSTALL_DIR="/home/${HARNESS_USER}/.local" sh \
      || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/home/${HARNESS_USER}/.local" sh
  fi
  [[ -x "$UV_BIN" ]] || die "uv 安装失败（$UV_BIN 不存在）"
  chown "${HARNESS_USER}:${HARNESS_USER}" "$UV_BIN"
}

install_uv

append_bashrc 'export PATH="$HOME/.local/bin:$PATH"'
if [[ "$CN" -eq 1 ]]; then
  append_bashrc "export UV_INDEX_URL=${UV_INDEX_URL}"
fi

log "安装 CPython 3.12（uv）"
as_harness uv python install 3.12 \
  || as_harness env -u UV_PYTHON_INSTALL_MIRROR uv python install 3.12
if command -v python3.12 >/dev/null 2>&1; then
  log "系统 python3.12: $(python3.12 -V 2>/dev/null || true)"
fi

# ---------------------------------------------------------------------------
# 4. Node 20 + pnpm
# ---------------------------------------------------------------------------
if [[ "$INSTALL_UI" == "1" ]]; then
  log "安装 Node ${NODE_VER} + pnpm ${PNPM_VER}"
  NODE_TARBALL="node-v${NODE_VER}-linux-x64.tar.xz"
  NODE_URL="https://nodejs.org/dist/v${NODE_VER}/${NODE_TARBALL}"
  if [[ "$CN" -eq 1 ]]; then
    NODE_URL="https://npmmirror.com/mirrors/node/v${NODE_VER}/${NODE_TARBALL}"
  fi
  if [[ ! -x /usr/local/node/bin/node ]] || ! /usr/local/node/bin/node -v | grep -q "^v20"; then
    curl -fL "$NODE_URL" -o "/tmp/${NODE_TARBALL}"
    mkdir -p /usr/local/node
    tar -xJf "/tmp/${NODE_TARBALL}" -C /usr/local/node --strip-components=1
  fi
  printf 'export PATH="/usr/local/node/bin:$PATH"\n' > /etc/profile.d/node.sh
  chmod 644 /etc/profile.d/node.sh
  export PATH="/usr/local/node/bin:$PATH"
  hash -r
  /usr/local/node/bin/node -v
  if [[ "$CN" -eq 1 ]]; then
    /usr/local/node/bin/npm config set registry https://registry.npmmirror.com
    as_harness /usr/local/node/bin/npm config set registry https://registry.npmmirror.com
  fi
  /usr/local/node/bin/corepack enable || true
  /usr/local/node/bin/corepack prepare "pnpm@${PNPM_VER}" --activate \
    || /usr/local/node/bin/npm install -g "pnpm@${PNPM_VER}"
  /usr/local/node/bin/pnpm -v
  append_bashrc 'export PATH="/usr/local/node/bin:$PATH"'
fi

# ---------------------------------------------------------------------------
# 5. 代码
# ---------------------------------------------------------------------------
if [[ "$SKIP_CLONE" == "1" ]] || [[ -f "${PWD}/pyproject.toml" && -d "${PWD}/app" ]]; then
  HARNESS_HOME="$PWD"
  log "使用已有仓库: $HARNESS_HOME"
else
  log "同步代码 $REPO_URL ($REPO_BRANCH) → $HARNESS_HOME"
  mkdir -p "$(dirname "$HARNESS_HOME")"
  if [[ -d "${HARNESS_HOME}/.git" ]]; then
    git -C "$HARNESS_HOME" fetch origin "$REPO_BRANCH" || true
    git -C "$HARNESS_HOME" checkout "$REPO_BRANCH"
    git -C "$HARNESS_HOME" pull origin "$REPO_BRANCH" || true
  else
    if [[ "$CN" -eq 1 ]]; then
      git clone --branch "$REPO_BRANCH" "$REPO_URL" "$HARNESS_HOME" \
        || git clone --branch "$REPO_BRANCH" "https://ghproxy.net/${REPO_URL#https://}" "$HARNESS_HOME"
    else
      git clone --branch "$REPO_BRANCH" "$REPO_URL" "$HARNESS_HOME"
    fi
  fi
fi

[[ -f "$HARNESS_HOME/pyproject.toml" ]] || die "仓库不完整：$HARNESS_HOME 没有 pyproject.toml"

git config --global --add safe.directory "$HARNESS_HOME" 2>/dev/null || true
as_harness git config --global --add safe.directory "$HARNESS_HOME" || true
chown -R "${HARNESS_USER}:${HARNESS_USER}" "$HARNESS_HOME"
cd "$HARNESS_HOME"
log "代码: $(git log -1 --oneline)"

# ---------------------------------------------------------------------------
# 6. .env
# ---------------------------------------------------------------------------
log "写入 .env（已有文件只补缺省项；传入的 KEY 会覆盖）"
if [[ ! -f "$HARNESS_HOME/.env" ]]; then
  cp "$HARNESS_HOME/.env.example" "$HARNESS_HOME/.env"
fi
chmod 600 "$HARNESS_HOME/.env"

upsert_env() {
  local key="$1" value="$2" file="$HARNESS_HOME/.env"
  [[ -n "$value" ]] || return 0
  python3 - "$key" "$value" "$file" <<'PY'
import pathlib, re, sys
key, value, path = sys.argv[1], sys.argv[2], sys.argv[3]
text = pathlib.Path(path).read_text(encoding="utf-8")
line = f"{key}={value}"
pat = re.compile(rf"^{re.escape(key)}=.*$", re.M)
if pat.search(text):
    text = pat.sub(line, text, count=1)
else:
    text = text.rstrip() + "\n" + line + "\n"
pathlib.Path(path).write_text(text, encoding="utf-8")
PY
}

upsert_env OPENAI_BASE_URL "$OPENAI_BASE_URL"
upsert_env LLM_QWEN_MAX "$LLM_QWEN_MAX"
upsert_env LLM_COMPRESSION_MODEL "$LLM_COMPRESSION_MODEL"
[[ -n "${OPENAI_API_KEY:-}" ]] && upsert_env OPENAI_API_KEY "$OPENAI_API_KEY"
[[ -n "${TAVILY_API_KEY:-}" ]] && upsert_env TAVILY_API_KEY "$TAVILY_API_KEY"

if grep -qE '你的大模型_API_KEY|你的_TAVILY_API_KEY' "$HARNESS_HOME/.env"; then
  warn ".env 仍是占位密钥。编辑 $HARNESS_HOME/.env 填入 OPENAI_API_KEY 与 TAVILY_API_KEY 后执行：systemctl restart research-harness-api"
fi
chown "${HARNESS_USER}:${HARNESS_USER}" "$HARNESS_HOME/.env"

# ---------------------------------------------------------------------------
# 7. Python / 前端依赖
# ---------------------------------------------------------------------------
log "uv python pin + uv sync（仓库根，锁 3.12）"
as_harness bash -c "cd '$HARNESS_HOME' && uv python pin 3.12 && uv sync"
as_harness bash -c "cd '$HARNESS_HOME' && uv run python -c 'import sys,fastapi,langgraph,reportlab; print(sys.version.split()[0], \"ok\")'"

if [[ "$INSTALL_UI" == "1" ]]; then
  log "pnpm install（frontend）"
  as_harness bash -c "cd '$HARNESS_HOME/frontend' && /usr/local/node/bin/pnpm install"
fi

mkdir -p "$HARNESS_HOME/output" "$HARNESS_HOME/logs"
chown -R "${HARNESS_USER}:${HARNESS_USER}" "$HARNESS_HOME/output" "$HARNESS_HOME/logs" "$HARNESS_HOME/.venv" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 8. systemd
# ---------------------------------------------------------------------------
if [[ "$START_API" == "1" ]]; then
  log "安装 systemd: research-harness-api"
  cat > /etc/systemd/system/research-harness-api.service <<EOF
[Unit]
Description=Research Agent Harness API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${HARNESS_USER}
Group=${HARNESS_USER}
WorkingDirectory=${HARNESS_HOME}
Environment=PATH=/home/${HARNESS_USER}/.local/bin:/usr/local/node/bin:/usr/bin
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-${HARNESS_HOME}/.env
ExecStart=${UV_BIN} run --directory ${HARNESS_HOME} uvicorn app.api.server:app --app-dir ${HARNESS_HOME} --host 0.0.0.0 --port ${API_PORT}
Restart=on-failure
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
fi

if [[ "$START_UI" == "1" ]] && [[ "$INSTALL_UI" == "1" ]]; then
  log "安装 systemd: research-harness-ui（Vite，验证/实验台用）"
  cat > /etc/systemd/system/research-harness-ui.service <<EOF
[Unit]
Description=Research Agent Harness UI (Vite)
After=network-online.target research-harness-api.service
Wants=network-online.target

[Service]
Type=simple
User=${HARNESS_USER}
Group=${HARNESS_USER}
WorkingDirectory=${HARNESS_HOME}/frontend
Environment=PATH=/usr/local/node/bin:/home/${HARNESS_USER}/.local/bin:/usr/bin
ExecStart=/usr/local/node/bin/pnpm dev --host 0.0.0.0 --port ${UI_PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi

if [[ "$START_API" == "1" ]] || { [[ "$START_UI" == "1" ]] && [[ "$INSTALL_UI" == "1" ]]; }; then
  systemctl daemon-reload
fi
if [[ "$START_API" == "1" ]]; then
  systemctl enable --now research-harness-api
  sleep 2
  systemctl --no-pager --full status research-harness-api || true
fi
if [[ "$START_UI" == "1" ]] && [[ "$INSTALL_UI" == "1" ]]; then
  systemctl enable --now research-harness-ui
  sleep 2
  systemctl --no-pager --full status research-harness-ui || true
fi

# ---------------------------------------------------------------------------
# 9. 无 LLM 冒烟 + health
# ---------------------------------------------------------------------------
log "跑无密钥回归冒烟（dry-run 40/40 不是 Agent Accuracy）"
as_harness bash -c "cd '$HARNESS_HOME' && uv run python tests/eval/run_eval.py --dry-run --baseline tests/eval/results/baseline.json --fail-on-regression" \
  || warn "dry-run eval 未通过，看上面日志"

if [[ "$START_API" == "1" ]]; then
  sleep 1
  if curl -fsS "http://127.0.0.1:${API_PORT}/health" >/tmp/harness-health.json 2>/dev/null; then
    log "GET /health:"
    cat /tmp/harness-health.json || true
    echo
  else
    warn "API 尚未响应 /health。查看: journalctl -u research-harness-api -e"
  fi
fi

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

============================================================
安装完成  ${HARNESS_HOME}
提交: $(git -C "$HARNESS_HOME" log -1 --oneline 2>/dev/null || echo unknown)
Python: $($UV_BIN run --directory "$HARNESS_HOME" python -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || echo unknown)
Node:   $(/usr/local/node/bin/node -v 2>/dev/null || echo skipped)
pnpm:   $(/usr/local/node/bin/pnpm -v 2>/dev/null || echo skipped)

下一步：
  1) 若还没带密钥进脚本，编辑后重启 API
       nano ${HARNESS_HOME}/.env
       systemctl restart research-harness-api

  2) 本机验 API
       curl -sS http://127.0.0.1:${API_PORT}/health

  3) 浏览器（Vite 已代理 /api 与 /ws，远程不要配 localhost）
       http://${HOST_IP:-<裸机IP>}:${UI_PORT}

  4) 评测
       cd ${HARNESS_HOME}
       sudo -u ${HARNESS_USER} -H bash -lc 'cd ${HARNESS_HOME} && uv run python tests/eval/run_eval.py --dry-run --fail-on-regression'
       sudo -u ${HARNESS_USER} -H bash -lc 'cd ${HARNESS_HOME} && uv run python tests/eval/run_eval.py --live --variant full --fixture --limit 5'

日志:
  journalctl -u research-harness-api -f
  journalctl -u research-harness-ui -f

阿里云 ECS：安全组放行 ${API_PORT}、${UI_PORT}（或只放 80 后走 Nginx，见 docs/OPENEULER_BARE_METAL.md 附录 B）
============================================================
EOF
