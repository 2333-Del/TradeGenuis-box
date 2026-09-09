# Docker 部署（服务器）

Docker 相关内容都收在本目录：

```
docker/
├── Dockerfile              # 镜像：华为云 python:3.12.9-slim + 清华 pip 源
├── Dockerfile.dockerignore # 构建上下文排除规则（相对仓库根；需 Docker BuildKit，Compose v2 默认开启）
├── docker-compose.yml      # 编排：端口/密码/数据卷（环境变量从本目录 .env 插值）
├── .env.example            # 配置模板：复制为 .env 填真实值
└── README.md
```

## 配置（docker/.env）

密码、推送渠道（Telegram/飞书）都从 `docker/.env` 进来，不进 git：

```bash
cp docker/.env.example docker/.env
vi docker/.env     # 填 DASHBOARD_PASSWORD 和 POSTGRES_PASSWORD（必填）；TG_*/FEISHU_* 推送可选
```

- Compose 启动时自动读取 compose 文件同目录的 `.env`（`-f docker/docker-compose.yml` 和 `cd docker` 两种方式都会命中）；
- 改完 `.env` 需 `up -d` 重建容器才生效；
- 临时覆盖：启动前 `export DASHBOARD_PASSWORD=xxx`（shell 变量优先级高于 `.env`）；
- **不设密码 = 服务只允许本机访问，容器内等于全部拒绝**，公网部署必设。

## PostgreSQL 历史数据

Compose 启动 `postgres:17-bookworm` 与看板两个服务。数据库只有容器内部网络访问，不映射宿主机端口。设置 `POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`；应用使用标准 PG 环境变量，密码中包含 `@`、`:` 等字符也无需 URL 编码。

应用等待数据库 `pg_isready` 健康后运行版本化迁移，再启动 HTTP 服务。迁移失败则进程退出，不以空库伪装成功。`up -d --build`、应用容器重建及普通 `down` 不删除历史。

- `pgdata` 命名卷：PostgreSQL 历史快照、复盘记录和任务。
- `../data:/app/data`：原有配置和缓存、待归档队列、旧JSON导入标记。
- **不要执行 `docker compose down -v`**，它会删除数据库卷。数据卷不是备份。
- 首次建库后修改 `.env` 的密码不会自动修改数据库已有用户密码；需先通过数据库管理工具修改用户密码，再更新应用配置。跨 PostgreSQL 主版本升级使用备份恢复，不直接替换镜像使用旧数据目录。

以下命令显式指定 `.env`，适用于从仓库根目录操作：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml config --quiet
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d --build
docker compose --env-file docker/.env -f docker/docker-compose.yml ps
```

### 备份与恢复

使用容器内文件和 `docker compose cp`，避免 PowerShell 二进制重定向损坏备份。先自行创建宿主机 `backups` 目录；备份也包含所保存的原始选股数据。

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/history.dump'
docker compose --env-file docker/.env -f docker/docker-compose.yml cp postgres:/tmp/history.dump ./backups/history.dump
```

恢复到**新建的空数据库部署**，先只启动 PostgreSQL，再复制备份恢复，最后启动应用执行必要的新迁移；不要覆盖正在使用的库：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d postgres
docker compose --env-file docker/.env -f docker/docker-compose.yml cp ./backups/history.dump postgres:/tmp/history.dump
docker compose --env-file docker/.env -f docker/docker-compose.yml exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error /tmp/history.dump'
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d dashboard
```

同时备份宿主机 `data` 目录（其中配置可能含凭据）。建议定期保留多份备份，并在独立部署中实际验证恢复。

### 容器验收

1. 新建部署：数据库健康、应用迁移成功后能登录。
2. 完成一次扫描，记下历史批次ID；重建应用容器后ID及原箱顶不变。
3. 临时停止数据库并完成扫描：日志显示待归档；恢复数据库后等待30秒，历史只新增一次。
4. 手动更新历史后重启应用，已完成结果仍在；正在运行的任务显示中断，可以重新发起。
5. 使用上述流程备份并恢复至独立部署，核对批次数、原快照和复盘结果。

## 上线步骤

```bash
# 0. 服务器上拿到完整代码（git clone 或 rsync；确保 market_data.py / notifications.py 等新文件都在）
git clone <repo-url> && cd TradeGenuis-box

# 1. 配置：复制模板并填密码/推送渠道（Telegram/飞书）
cp docker/.env.example docker/.env && vi docker/.env

# 2. 构建并启动（在仓库根执行；首次拉镜像+构建约 1-3 分钟）
docker compose -f docker/docker-compose.yml up -d --build

# 3. 看日志 / 管理命令
docker compose -f docker/docker-compose.yml logs -f
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml down
```

也可以 `cd docker && docker compose up -d --build`，效果相同（compose 会以本目录解析相对路径）。

## 路径关系（改动前先看）

- **构建上下文是仓库根**（compose 里 `context: ..`），Dockerfile 的 `COPY scanner.py ...` 等路径相对仓库根——新增 Python 模块时须同步加进 `docker/Dockerfile` 的 COPY 行。
- **数据卷挂到仓库根的 `data/`**（`../data` 相对本目录）：扫描结果、自选池、配置、缓存都持久化在宿主机，重建容器不丢。目录不存在时 compose 会自动创建。
- 对公网开放建议把端口映射改为 `"127.0.0.1:12300:8808"`，前置 Nginx/Caddy 反代加 HTTPS。

## 云服务器限流须知

行情源全部为公开接口，云 IP 属机房段，东财 clist（股票/ETF 清单）风控比家用宽带严。若清单接口被限流，扫描自动降级（缓存兜底、当日列表为空），次日自动恢复；必要时把 `scanner.py` 的 `MARKET_WORKERS` 降到 4。看板行情刷新走腾讯批量接口（一次 50 只），常驻压力很小。

## 更新部署

```bash
git pull            # 或 rsync 覆盖代码
docker compose -f docker/docker-compose.yml up -d --build
```

镜像内不含代码热更新；改代码后必须重新 build。数据在 `data/` 卷里，不受影响。
