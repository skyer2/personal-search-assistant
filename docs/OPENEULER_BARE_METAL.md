# openEuler 裸机部署：Research Agent Harness

> 适用系统：openEuler 22.03 SP3 x86_64（`uname -a` 类似 `Linux … 5.10.0-*.oe2203sp3.x86_64`）  
> 形态：**单机裸机**，不用 Docker / K8s / MySQL / RAGFlow / MCP。  
> 代码身份：Research Agent Harness。GitHub 仓库名仍可能是 `personal-search-assistant`。  
> 权威范围：[ARCHITECTURE.md](./ARCHITECTURE.md)

本文按「能照着敲、能沉淀进运维手册」写。复制到内部 Wiki 时，把文中的 `/opt/research-agent-harness`、密钥、IP 换成你们的值即可。

---

## 1. 这套东西跑起来是什么

两个常驻进程：

| 进程 | 监听 | 作用 |
|------|------|------|
| FastAPI / Uvicorn | `0.0.0.0:8000` | Harness：任务、上传、WebSocket、health |
| Vite 开发服务 **或** Nginx 静态页 | `0.0.0.0:5173` 或 `:80` | 实验台 UI |

浏览器 → 前端 → `/api` 与 `/ws` → 后端 → LLM（默认阿里云百炼兼容接口）+ Tavily search。

**不需要：** MySQL、PostgreSQL、Redis、Elasticsearch、RAGFlow、MCP Server。

**必须能出网：**

| 目标 | 用途 |
|------|------|
| 百炼 / OpenAI 兼容 `OPENAI_BASE_URL` | 规划、工人、综合 |
| `api.tavily.com` | environment `search` |
| 被 fetch 的公开网页 | `fetch_url` |
| （安装阶段）GitHub / PyPI / npm 或对应国内镜像 | 拉代码和依赖 |

Python 版本锁死 **3.12.x**（`>=3.12,<3.13`）。系统自带 3.9/3.11 **不能**直接用。

---

## 2. 建议目录与账号

```text
/opt/research-agent-harness     # 代码（git clone）
  ├── .env                      # 密钥，权限 600，勿提交
  ├── .venv/                    # Python 虚拟环境（gitignore）
  ├── frontend/                 # UI
  ├── output/                   # 会话产物与 graph checkpoint
  └── logs/                     # 可选 trace
/opt/python/3.12                # 仅当源码编译 CPython 时
/usr/local/node                 # Node 20 官方二进制
```

建议独立用户，不要用 root 跑服务：

```bash
sudo useradd -r -m -d /home/harness -s /bin/bash harness
sudo mkdir -p /opt/research-agent-harness
sudo chown harness:harness /opt/research-agent-harness
```

下文命令在 `harness` 用户下执行；需要 `dnf` 的步骤用 `sudo`。

---

## 3. 系统包

```bash
sudo dnf -y update
sudo dnf -y groupinstall "Development Tools"
sudo dnf -y install \
  git curl tar xz which \
  gcc gcc-c++ make \
  openssl-devel bzip2-devel libffi-devel \
  zlib-devel readline-devel sqlite-devel xz-devel \
  wget ca-certificates
```

防火墙（本机浏览器可跳过；局域网/公网访问才开）：

```bash
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --permanent --add-port=5173/tcp
# 若用附录 Nginx 反代，改开放 80/443，不要对公网裸放 8000
sudo firewall-cmd --reload
sudo firewall-cmd --list-ports
```

SELinux 保持 Enforcing 一般可跑 Uvicorn。若绑定端口或读家目录被拒：

```bash
getenforce
# 临时排查：sudo setenforce 0
# 生产应写策略，不要长期关闭
```

时钟建议 NTP，避免 TLS 校验证书失败。

---

## 4. 安装 Python 3.12

先探测仓库是否已有包：

```bash
dnf list python3.12 2>/dev/null || true
python3.12 --version 2>/dev/null || true
```

openEuler 22.03 SP3 **多数没有** 3.12 系统包。任选下面一条路径。

### 路径 A（推荐）：uv 自带 CPython

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
uv python install 3.12
uv python pin 3.12
uv python list
```

国内若拉 `astral.sh` / GitHub 失败，用代理，或改走路径 B / C。

### 路径 B：Miniforge（conda-forge 的 3.12）

```bash
cd /tmp
curl -L -o Miniforge3.sh \
  https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash Miniforge3.sh -b -p "$HOME/miniforge3"
source "$HOME/miniforge3/etc/profile.d/conda.sh"
conda create -y -n harness python=3.12
conda activate harness
python -c "import sys; print(sys.version)"
```

### 路径 C：源码编译（完全离线可复现）

```bash
sudo mkdir -p /opt/python
cd /tmp
curl -LO https://www.python.org/ftp/python/3.12.10/Python-3.12.10.tgz
tar xf Python-3.12.10.tgz
cd Python-3.12.10
./configure --prefix=/opt/python/3.12 --enable-optimizations --with-ensurepip=install
make -j"$(nproc)"
sudo make altinstall
/opt/python/3.12/bin/python3.12 --version
echo 'export PATH="/opt/python/3.12/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

必须看到 `Python 3.12.x`。不要用 `python3`（多半仍是系统 3.9）。

---

## 5. 安装 Node.js 20 与 pnpm

前端 `packageManager` 为 **pnpm@10.33.0**。系统 `dnf install nodejs` 通常版本过旧，用官方二进制。

**不要对 `corepack`/`pnpm` 使用裸的 `sudo`。** `sudo` 默认 PATH 不含 `/usr/local/node/bin`，会报 `corepack: command not found`。用绝对路径，或先 `export PATH` 再执行（你已是 root 则全程不必 sudo）。

```bash
NODE_VER=20.19.0
cd /tmp
curl -LO "https://nodejs.org/dist/v${NODE_VER}/node-v${NODE_VER}-linux-x64.tar.xz"
# 国内镜像示例：
# curl -LO "https://npmmirror.com/mirrors/node/v${NODE_VER}/node-v${NODE_VER}-linux-x64.tar.xz"
mkdir -p /usr/local/node
tar -xJf "node-v${NODE_VER}-linux-x64.tar.xz" -C /usr/local/node --strip-components=1
echo 'export PATH="/usr/local/node/bin:$PATH"' > /etc/profile.d/node.sh
source /etc/profile.d/node.sh
hash -r
node -v
npm -v

# 必须用绝对路径（sudo 会丢掉上面的 PATH）
/usr/local/node/bin/corepack enable
/usr/local/node/bin/corepack prepare pnpm@10.33.0 --activate
hash -r
command -v pnpm
pnpm -v
```

若 `corepack` 仍不可用，改用 npm 全局安装（同样用绝对路径）：

```bash
/usr/local/node/bin/npm install -g pnpm@10.33.0
hash -r
pnpm -v
```

国内 npm：

```bash
pnpm config set registry https://registry.npmmirror.com
```

**检查：** `node -v` 为 `v20.x`，`pnpm -v` 为 `10.33.x`。新开的 SSH 会话会自动读 `/etc/profile.d/node.sh`；当前会话若找不到 `pnpm`，再执行一次 `source /etc/profile.d/node.sh && hash -r`。

---

## 6. 获取代码

```bash
cd /opt
sudo -u harness git clone https://github.com/skyer2/personal-search-assistant.git research-agent-harness
cd /opt/research-agent-harness
git checkout main
git log -1 --oneline
```

内网无 GitHub 时：在能上网的机器 clone 后 `tar` 拷过来，保证带 `.git` 便于以后 `git pull`。

---

## 7. 环境变量（`.env`）

```bash
cd /opt/research-agent-harness
cp .env.example .env
chmod 600 .env
```

`.env` 必须放在**仓库根目录**。`python-dotenv` 从这里加载。不要只 `export` 一次就关终端。

### 7.1 最小可跑（必填）

```bash
# LLM：默认阿里云百炼 OpenAI 兼容模式
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_API_KEY=sk-替换成你的密钥
LLM_QWEN_MAX=qwen-max
LLM_COMPRESSION_MODEL=qwen-turbo
LLM_TIMEOUT_SEC=120

# Search 环境工具（不是产品搜索引擎）
TAVILY_API_KEY=tvly-替换成你的密钥
TAVILY_TIMEOUT_SEC=120
```

密钥来源：

- 百炼：https://bailian.console.aliyun.com/ （兼容模式 Base URL 即上面这一条）
- Tavily：https://app.tavily.com/

改用 OpenAI 官方时：

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
LLM_QWEN_MAX=gpt-4o
LLM_COMPRESSION_MODEL=gpt-4o-mini
```

`LLM_QWEN_MAX` 这个名字是历史变量名，值填**实际模型 ID**，不要求一定是 Qwen。

### 7.1.1 火山 AI 网关（蓝区 Chat 兼容）

本仓库只走 **OpenAI Chat Completions**（`POST …/v1/chat/completions`）。`.env` 必须用网关说明里的 **「OpenAI 协议、chat 格式」** 地址，**不要**填 Responses（`/v1/responses`），**不要**填 Anthropic（`/compatible`）。

网络：仅蓝区；须 **断开 XGate**。密钥向网关重新申请（一个 key 可调该网关上已开通的模型）。内测若偶发失败，先重试一次。

```bash
OPENAI_BASE_URL=https://st8tp3ajl0df3n8b8l8qu.apigateway-cn-beijing.volceapi.com/v1
OPENAI_API_KEY=网关新发的key
LLM_QWEN_MAX=控制台里的模型ID
LLM_COMPRESSION_MODEL=控制台里的轻量模型ID
LLM_TIMEOUT_SEC=180
```

`LLM_QWEN_MAX` / `LLM_COMPRESSION_MODEL` 填网关模型列表中的 ID（例如豆包、DeepSeek 等），不要再写 `qwen-max`，除非网关确实挂了同名模型。

Tavily 仍要单独配：`search` 不走火山网关。

改完 `.env` 后必须重启 uvicorn。若 `GET /v1/models` 网关未实现，新版 `/health` 会改打一条极短 chat；仍 `llm=down` 则检查蓝区网络、XGate、Key、模型 ID。

### 7.2 建议保持默认（Phase 1）

```bash
HARNESS_LLM_COMPRESSION=true
HARNESS_TOKEN_MODEL=glm-5.2
HARNESS_MEMORY_ENABLED=false
HARNESS_GRAPH_RUNTIME=true
HARNESS_PERSIST_LOOP_STATE=false
HARNESS_HITL_ENABLED=false
```

不要把 `HARNESS_PERSIST_LOOP_STATE` 设成 `true`（双 checkpoint 已废弃）。

### 7.3 可选

| 变量 | 默认/说明 |
|------|-----------|
| `HARNESS_LANGFUSE_ENABLED` | 不配则无云端 Trace |
| `LANGFUSE_PUBLIC_KEY` / `SECRET_KEY` / `HOST` | Langfuse |
| `HARNESS_GRAPH_CHECKPOINT_PATH` | 默认 `output/.harness/graph_checkpoints.sqlite` |
| `HARNESS_MAX_RUN_SEC` | 单次任务墙钟上限，yaml 默认 600 |
| `HARNESS_MAX_PARALLEL_WORKERS` | 默认 3 |
| `HARNESS_STEP_TIMEOUT_SEC` | 单步超时，默认 120 |
| `BROWSECOMP_PLUS_*` | 固定语料评测，见 [BROWSECOMP_PLUS_EVAL.md](./BROWSECOMP_PLUS_EVAL.md)；开启后 **search 不再打 Tavily** |

更细的开关在 `app/config/harness.yml`，环境变量可覆盖其中一部分（见 `app/config/loader.py`）。

### 7.4 前端（通常不用建）

开发态 Vite 已把 `/api`、`/ws` 代理到 `127.0.0.1:8000`。

- **本机浏览器**打开 `http://127.0.0.1:5173`：可不建 `frontend/.env.local`。
- **别的电脑访问这台裸机**：不要把 `VITE_API_BASE_URL=http://localhost:8000` 写进 `frontend/.env.local`，远程浏览器会连到**访问者自己的电脑**。不配 env，走同源代理。
- 若必须直连后端 IP：

```bash
# frontend/.env.local 仅在你清楚浏览器所在机器时使用
VITE_API_BASE_URL=http://<裸机IP>:8000
VITE_WS_BASE_URL=ws://<裸机IP>:8000
```

---

## 8. 安装项目依赖

仓库根目录：

```bash
cd /opt/research-agent-harness
```

### 用 uv（与仓库 `uv.lock` 一致）

```bash
export PATH="$HOME/.local/bin:$PATH"
# 国内 PyPI 示例
# export UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
uv sync
uv run python -c "import sys; print(sys.version); import fastapi, langgraph; print('ok')"
```

### 用 venv + pip

```bash
# 按你选的 3.12 解释器
PY=python3.12
# 或: PY=/opt/python/3.12/bin/python3.12
# 或: conda activate harness && PY=python

$PY -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
# pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
pip install -r requirements.txt
python -c "import sys; print(sys.version); import fastapi, langgraph; print('ok')"
```

前端：

```bash
cd /opt/research-agent-harness/frontend
pnpm install
```

磁盘：`node_modules` + Python 依赖大约数 GB 量级，给 `/opt` 留足空间。`output/` 会随任务增长。

---

## 9. 启动（开发 / 验证）

开两个会话（`tmux` / `screen` 均可）。

**会话 A — API：**

```bash
cd /opt/research-agent-harness
export PATH="$HOME/.local/bin:$PATH"

# uv
uv run uvicorn app.api.server:app --app-dir . --host 0.0.0.0 --port 8000

# venv
# source .venv/bin/activate
# uvicorn app.api.server:app --app-dir . --host 0.0.0.0 --port 8000
```

`--app-dir .` 不能省。`--reload` 仅开发用，裸机常驻不要加。

**会话 B — UI：**

```bash
cd /opt/research-agent-harness/frontend
pnpm dev
```

浏览器：

- 本机：http://127.0.0.1:5173
- 局域网：http://\<裸机IP\>:5173

---

## 10. 验收

```bash
# 进程
ss -lntp | grep -E '8000|5173'

# 健康：llm 与 tavily 应为 ok
curl -sS http://127.0.0.1:8000/health | python3.12 -m json.tool

# 能力清单
curl -sS http://127.0.0.1:8000/api/harness/capabilities | python3.12 -m json.tool
```

`health` 含义：

| 字段 | ok | down |
|------|----|------|
| `dependencies.llm` | Key + Base URL 能列出模型 | 缺 Key、URL 错、出网被墙 |
| `dependencies.tavily` | 配了 `TAVILY_API_KEY` | 没配（此探针不真发搜索） |
| `dependencies.langfuse` | 未开则为 `disabled` | 正常 |

不跑 UI 的冒烟：

```bash
curl -sS http://127.0.0.1:8000/api/task \
  -H 'Content-Type: application/json' \
  -d '{"query":"用三句话说明 LangGraph checkpoint 是什么","mode":"agent"}'
```

立刻返回 `thread_id`。答案在 WebSocket `/ws/{thread_id}`，不在这个 HTTP 响应里。

无 LLM 的单测（不消耗 Key）：

```bash
cd /opt/research-agent-harness
uv run python tests/test_architecture_p0.py
uv run python tests/test_environment_tools.py
uv run python tests/test_research_harness.py
```

---

## 11. 怎么用实验台

1. 左侧 WebSocket 为「已连接」。
2. 输入研究任务，可附文件；发送固定 `mode=agent`。
3. 对话区走 Brief → Plan → Workers → Progress / Replan → 答案。
4. 产物在 `output/` 下对应 session 目录。
5. 侧栏 Eval / Trace 给研究者看机制，不是搜索产品功能。
6. 「新建任务」换 `thread_id`。同一会话再提交会取消旧任务。

对照实验 `direct`（单 Agent + search，无完整 Harness）走 API：

```bash
curl -sS http://127.0.0.1:8000/api/task \
  -H 'Content-Type: application/json' \
  -d '{"query":"…","mode":"direct"}'
```

前端默认不提供该档切换。

---

## 12. 运行时数据（备份 / 清理）

| 路径 | 内容 | 是否提交 Git |
|------|------|----------------|
| `.env` | 密钥 | 否 |
| `output/` | 会话文件、checkpoint | 否 |
| `output/.harness/graph_checkpoints.sqlite` | 图状态 | 否 |
| `updated/` | 上传暂存 | 否 |
| `frontend/node_modules/` | 前端依赖 | 否 |
| `.venv/` | Python 环境 | 否 |

崩溃恢复靠 LangGraph SQLite checkpoint，不靠 MySQL。

---

## 13. 升级

```bash
cd /opt/research-agent-harness
git fetch origin
git checkout main
git pull origin main
uv sync          # 或 pip install -r requirements.txt
cd frontend && pnpm install
# 重启 uvicorn 与 pnpm dev / systemd
```

`.env` 不在 git 里，升级不会覆盖密钥。对照新的 `.env.example` 手工补变量。

---

## 14. 排障

| 现象 | 处理 |
|------|------|
| `requires-python` / 装包失败 | `python -V` 必须 3.12.x |
| `ModuleNotFoundError: app` | 在仓库根启动，并带 `--app-dir .` |
| `llm=down` | `.env` 路径、Key、`OPENAI_BASE_URL`、机器能否访问百炼 |
| 能规划不能搜 | `TAVILY_API_KEY`；公司网关是否拦 `api.tavily.com` |
| 页面一直转圈、WS 失败 | 8000 没起来；或远程访问却配了 localhost |
| `Address already in use` | `ss -lntp \| grep 8000` 杀掉旧进程 |
| TLS / certificate verify failed | 系统时间、`ca-certificates` |
| `uv python install` 超时 | 代理，或改路径 B/C |
| `sudo: corepack: command not found` | `sudo` 没有 `/usr/local/node/bin`。用 `/usr/local/node/bin/corepack enable`，或 `/usr/local/node/bin/npm install -g pnpm@10.33.0` |
| SELinux AVC | `ausearch -m avc -ts recent` |

看后端终端里 `[MainAgent]`、`[PlannerLLM]`、工具报错。

---

## 15. 明确不要装

- Docker Compose 里的旧 MySQL（已从仓库删除）
- RAGFlow、MCP、Postgres（Memory 默认关）
- 把本项目当「搜索引擎」去接内部检索中台

Search 只是 Worker 可调用的 Tavily + `fetch_url` + 本地读文件。

---

## 附录 A — systemd 常驻 API

`/etc/systemd/system/research-harness-api.service`：

```ini
[Unit]
Description=Research Agent Harness API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=harness
Group=harness
WorkingDirectory=/opt/research-agent-harness
EnvironmentFile=/opt/research-agent-harness/.env
# uv：把 ExecStart 换成 /home/harness/.local/bin/uv run --directory /opt/research-agent-harness uvicorn ...
ExecStart=/opt/research-agent-harness/.venv/bin/uvicorn app.api.server:app --app-dir /opt/research-agent-harness --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now research-harness-api
sudo systemctl status research-harness-api
journalctl -u research-harness-api -f
```

前端开发服务不建议 systemd 长期跑 `pnpm dev`。要常驻 UI 用附录 B。

---

## 附录 B — Nginx 反代 + `pnpm build`

```bash
cd /opt/research-agent-harness/frontend
pnpm install
pnpm build
# 产物：frontend/dist
sudo dnf -y install nginx
```

`/etc/nginx/conf.d/harness.conf` 示例：

```nginx
server {
    listen 80;
    server_name _;

    root /opt/research-agent-harness/frontend/dist;
    index index.html;

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 600s;
    }

    location /health {
        proxy_pass http://127.0.0.1:8000/health;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

```bash
sudo nginx -t && sudo systemctl enable --now nginx
```

此时浏览器只访问 **80**，不要再依赖 5173。

生产构建**不会**走 Vite 开发代理。未设置 `VITE_API_BASE_URL` 时，构建产物会默认连 `http://localhost:8000`，远程浏览器会失败。

在 **build 之前**写入浏览器将访问的 origin（Nginx 在 80 反代 `/api` 与 `/ws` 时不要写 `:8000`）：

```bash
cd /opt/research-agent-harness/frontend
cat > .env.production <<'EOF'
VITE_API_BASE_URL=http://<裸机IP或域名>
VITE_WS_BASE_URL=ws://<裸机IP或域名>
EOF
pnpm build
```

HTTPS 则用 `https://` / `wss://`。改 IP 后必须重新 `pnpm build`。

验证期仍建议用第 9 节的 `pnpm dev`（5173 同源代理），少踩构建变量。

---

## 附录 C — 检查清单（可打印）

```text
[ ] uname 确认 oe2203sp3 x86_64
[ ] python 3.12.x（不是系统 python3）
[ ] node 20.x + pnpm 10.33.x
[ ] 仓库在 /opt/research-agent-harness，分支 main
[ ] .env 权限 600，含 OPENAI_* 与 TAVILY_API_KEY
[ ] uv sync 或 pip install -r requirements.txt 成功
[ ] frontend && pnpm install 成功
[ ] :8000 health → llm=ok, tavily=ok
[ ] 浏览器能开 UI，WebSocket 已连接
[ ] 防火墙仅对需要的网段开放
[ ] 不把 .env / output 拷进 git 或网盘明文
```

---

## 附录 D — 和架构文档的关系

| 文档 | 用途 |
|------|------|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 研究什么、四层、agent/direct |
| [HARNESS_ARCHITECTURE.md](./HARNESS_ARCHITECTURE.md) | StateGraph 运行时 |
| [CONTEXT_SYSTEM.md](./CONTEXT_SYSTEM.md) | 上下文外置 |
| **本文** | 裸机怎么装、怎么配、怎么验 |

配置以仓库根 `.env` + `app/config/harness.yml` 为准；本文与代码冲突时以代码和 `.env.example` 为准。
