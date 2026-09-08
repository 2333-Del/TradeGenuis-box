# Docker 部署（服务器）

Docker 相关内容都收在本目录：

```
docker/
├── Dockerfile              # 镜像：华为云 python:3.12.9-slim + 清华 pip 源
├── Dockerfile.dockerignore # 构建上下文排除规则（相对仓库根；需 Docker BuildKit，Compose v2 默认开启）
├── docker-compose.yml      # 编排：端口/密码/数据卷
└── README.md
```

## 上线步骤

```bash
# 0. 服务器上拿到完整代码（git clone 或 rsync；确保 market_data.py / notifications.py 等新文件都在）
git clone <repo-url> && cd TradeGenuis-box

# 1. 改密码：编辑 docker/docker-compose.yml 里的 DASHBOARD_PASSWORD
#    （不设密码 = 服务只允许本机访问，容器内等于全部拒绝）

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
