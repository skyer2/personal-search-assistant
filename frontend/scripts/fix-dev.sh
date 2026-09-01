#!/usr/bin/env bash
# 修复裸机上 Vite 白屏：pnpm 10 忽略 esbuild 安装脚本 + 5173 被旧 node 占用。
# 用法（在任意目录）：
#   bash /opt/research-agent-harness/frontend/scripts/fix-dev.sh
#   bash /opt/research-agent-harness/frontend/scripts/fix-dev.sh --start
set -euo pipefail

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

START=0
if [[ "${1:-}" == "--start" ]]; then
  START=1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/../package.json" ]] && grep -q '"research-agent-harness-frontend"' "${SCRIPT_DIR}/../package.json"; then
  FRONTEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
elif [[ -f /opt/research-agent-harness/frontend/package.json ]]; then
  FRONTEND_DIR=/opt/research-agent-harness/frontend
elif [[ -f ./package.json ]] && grep -q '"research-agent-harness-frontend"' ./package.json; then
  FRONTEND_DIR="$(pwd)"
else
  die "找不到 frontend 目录。请：cd /opt/research-agent-harness/frontend && bash scripts/fix-dev.sh --start"
fi
cd "${FRONTEND_DIR}"

command -v pnpm >/dev/null || die "找不到 pnpm，先 source /etc/profile.d/node.sh"
command -v node >/dev/null || die "找不到 node"

pids_on_port() {
  local port="$1"
  ss -lntp 2>/dev/null | awk -v p=":${port}" '
    index($4, p) {
      while (match($0, /pid=[0-9]+/)) {
        print substr($0, RSTART+4, RLENGTH-4)
        $0 = substr($0, RSTART+RLENGTH)
      }
    }
  ' | sort -u
}

kill_port() {
  local port="$1"
  local pid
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" 2>/dev/null || true
  fi
  for pid in $(pids_on_port "${port}"); do
    log "杀掉占用 ${port} 的 pid=${pid}"
    /bin/kill -TERM "${pid}" 2>/dev/null || true
    sleep 0.3
    /bin/kill -KILL "${pid}" 2>/dev/null || true
  done
  sleep 0.5
}

log "工作目录 ${FRONTEND_DIR}"

log "停掉 5173/5174 上的旧 Vite（不动 8000）"
kill_port 5173
kill_port 5174

STILL="$(pids_on_port 5173; pids_on_port 5174)"
if [[ -n "${STILL}" ]]; then
  log "端口仍被占用，进程状态："
  ps -o pid,ppid,stat,cmd -p ${STILL} || true
  die "5173/5174 杀不掉。另开一个 SSH 看是否有 tmux/systemd 在重启 Vite：systemctl list-units --all | grep -iE 'vite|frontend|pnpm'"
fi

log "清掉 pnpm 10 的 ignoredBuiltDependencies / 忽略脚本配置"
pnpm config delete ignore-scripts >/dev/null 2>&1 || true
if [[ -f "${HOME}/.npmrc" ]]; then
  sed -i '/^ignored-built-dependencies/d; /^never-built-dependencies/d; /^ignore-scripts=/d' "${HOME}/.npmrc" || true
fi
if [[ -f /root/.npmrc ]]; then
  sed -i '/^ignored-built-dependencies/d; /^never-built-dependencies/d; /^ignore-scripts=/d' /root/.npmrc || true
fi

python3 - <<'PY'
import json
from pathlib import Path
path = Path("package.json")
data = json.loads(path.read_text(encoding="utf-8"))
pnpm = data.setdefault("pnpm", {})
pnpm.pop("ignoredBuiltDependencies", None)
pnpm.pop("neverBuiltDependencies", None)
allowed = list(pnpm.get("onlyBuiltDependencies") or [])
if "esbuild" not in allowed:
    allowed.append("esbuild")
pnpm["onlyBuiltDependencies"] = allowed
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("package.json pnpm.onlyBuiltDependencies =", allowed)
PY

log "重装 frontend 依赖（允许 esbuild 跑安装脚本）"
rm -rf node_modules
# pnpm 10 默认跳过依赖 scripts；--ignore-scripts=false + package.json onlyBuiltDependencies
if ! pnpm install --ignore-scripts=false; then
  pnpm install
fi
if [[ ! -x node_modules/.bin/esbuild ]]; then
  log "esbuild 仍未链接，显式补装"
  pnpm add -D esbuild --ignore-scripts=false || pnpm add -D esbuild
fi

log "校验 esbuild 二进制"
BIN=""
if [[ -x node_modules/.bin/esbuild ]]; then
  BIN=node_modules/.bin/esbuild
else
  BIN="$(find node_modules -path '*esbuild*' -name esbuild -type f 2>/dev/null | head -n 1 || true)"
fi
[[ -n "${BIN}" ]] || die "esbuild 仍未安装。检查网络/registry，或：pnpm config get registry"
"${BIN}" --version || die "esbuild 无法执行：${BIN}"
log "esbuild OK (${BIN})"

if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  log "放行 5173/tcp"
  firewall-cmd --permanent --add-port=5173/tcp >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
fi

if [[ "${START}" -eq 1 ]]; then
  log "启动 Vite（前台）。浏览器打开 http://<本机IP>:5173/ 并强制刷新"
  exec pnpm dev
fi

log "修复完成。另开终端启动："
echo "  cd ${FRONTEND_DIR} && pnpm dev"
echo "浏览器：http://<裸机IP>:5173/  （Ctrl+Shift+R）"
echo "本机自检："
echo "  curl -sS -o /dev/null -w 'html %{http_code}\\n' http://127.0.0.1:5173/"
echo "  curl -sS -o /dev/null -w 'tsx  %{http_code}\\n' http://127.0.0.1:5173/src/main.tsx"
echo "两个都应为 200。uvicorn 8000 保持原样不要停。"
