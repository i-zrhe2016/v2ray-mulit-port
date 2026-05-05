# v2ray

一个基于 Docker Compose 的多端口 V2Ray 管理面板，支持单机栈和 NGINX/Panel 与 V2Ray 分离的双机栈。

现在的结构是三部分：

- `v2ray`：运行单一固定上游 V2Ray 实例
- `v2ray-panel`：提供管理页面、JSON API、流量统计、到期控制、订阅链接，并生成 NGINX 端口转发 include
- `subconverter`：把每个端口的 V2Ray 订阅即时转换成 Clash/Mihomo 订阅

## 已实现能力

- 支持多端口，一个端口对应一个用户订阅
- 支持添加、删除、启用、禁用端口
- 支持每个端口独立 UUID、备注、WS 路径
- 支持每个端口总流量上限
- 支持每个端口到期时间
- 支持每个端口查看已用流量、剩余流量
- 支持每个端口流量清零
- 支持每个端口独立的 V2Ray 订阅链接和 Clash 订阅链接
- 提供 Web 管理面板 `/admin`
- 状态持久化到 `data/ports.json`
- 面板根据 JSON 状态重写 NGINX managed include，并在变更后执行 NGINX reload

## 目录

```text
.
├── api/
│   ├── models.py
│   ├── runtime.py
│   ├── server.py
│   ├── settings.py
│   ├── service.py
│   ├── store.py
│   └── subscriptions.py
├── data/
│   └── .gitkeep
├── docker/
│   ├── api.Dockerfile
│   ├── Dockerfile
│   └── entrypoint.sh
├── deploy/
│   └── split-architecture/
│       ├── panel-host/
│       ├── v2ray-host/
│       └── README.md
├── tests/
│   └── test_panel.py
├── v2ray/
│   └── config.template.json
└── docker-compose.yml
```

## 工作方式

- `v2ray-panel` 监听固定管理端口，默认 `2016`
- NGINX 对外发布一段固定端口范围，默认 `20000-20100`
- 面板创建、修改、启用、禁用、删除端口时，会重写一份受管的 NGINX include 文件，把活跃端口转发到固定 V2Ray upstream
- 面板后台按“服务器设置”读取流量累计值，默认使用 V2Ray StatsService，也可以改为读取 NGINX 导出的 JSON 统计文件
- 固定 V2Ray upstream、公开订阅主机、TLS 标记、流量统计来源、NGINX JSON 路径、NGINX include 输出路径和 reload 命令会持久化在 `data/ports.json` 的 `server` 对象中
- 端口流量耗尽或到期后，面板会自动把该端口从生成的 NGINX 转发表中移除

## 快速开始

### 1. 配置 `.env`

建议最少包含这些值：

```env
PANEL_PORT=2016
V2RAY_API_PORT=10085
V2RAY_API_SERVER=127.0.0.1:10085
V2RAY_PORT_RANGE_START=20000
V2RAY_PORT_RANGE_END=20100
V2RAY_UPSTREAM_HOST=127.0.0.1
V2RAY_UPSTREAM_PORT=10085
V2RAY_PUBLIC_HOST=your-server-ip-or-domain
V2RAY_PUBLIC_TLS=false
TRAFFIC_STATS_SOURCE=v2ray
NGINX_TRAFFIC_STATS_FILE=/data/nginx-traffic.json
NGINX_MANAGED_CONFIG_PATH=/data/nginx-managed-http.conf
NGINX_RELOAD_COMMAND=nginx -s reload
V2RAY_SUBCONVERTER_TEMPLATE=https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/config/ACL4SSR_Online.ini
```

说明：

- `V2RAY_PUBLIC_HOST` 会写入每个端口生成出来的 VMess 配置
- 如果你在外层自己做了 TLS 终止，可以设置 `V2RAY_PUBLIC_TLS=true`
- 用户端口必须落在 `V2RAY_PORT_RANGE_START` 到 `V2RAY_PORT_RANGE_END` 之间
- `V2RAY_UPSTREAM_HOST` 和 `V2RAY_UPSTREAM_PORT` 指向单一固定上游 V2Ray；双机部署时通常配置成远端 V2Ray 服务器地址
- `V2RAY_API_SERVER` 是面板访问 V2Ray Stats/API 的地址，和固定 upstream 分开配置；双机部署时 panel-host 必须能连到 v2ray-host
- `NGINX_MANAGED_CONFIG_PATH` 是面板生成的 NGINX include 文件路径
- `NGINX_RELOAD_COMMAND` 是每次端口变更后执行的 reload 命令
- 如果由 NGINX 负责对外代理和流量计量，把 `TRAFFIC_STATS_SOURCE` 设为 `nginx_json`
- 这些变量是新状态或缺失字段的启动默认值；保存到面板“服务器设置”后，后续同步、订阅生成和流量读取都会使用持久化设置

### 2. 启动单机栈

```bash
docker compose up -d --build
```

根目录 `docker-compose.yml` 会在同一台宿主机上启动 `v2ray`、`v2ray-panel` 和 `subconverter`。

### 3. 启动双机分体栈

如果你的部署是 “NGINX + panel 一台机器，V2Ray 另一台机器”，直接使用仓库里的 bundle：

1. 在 V2Ray 服务器上启动运行时

```bash
cd deploy/split-architecture/v2ray-host
cp .env.example .env
docker compose up -d --build
```

2. 在 NGINX + panel 服务器上启动入口栈

```bash
cd deploy/split-architecture/panel-host
cp .env.example .env
mkdir -p data
docker compose up -d --build
```

3. 修改 `deploy/split-architecture/panel-host/.env` 的关键项

- `V2RAY_API_SERVER`：面板访问远端 V2Ray API/Stats 的地址，例如 `10.0.0.20:10085`
- `V2RAY_UPSTREAM_HOST` / `V2RAY_UPSTREAM_PORT`：受管 NGINX 端口统一转发到的远端固定 upstream
- `V2RAY_PUBLIC_HOST`：写入用户订阅的公开域名或 IP
- `NGINX_PORT_RANGE`：由 panel-host 上的 NGINX 对外监听的端口范围

`panel-host` bundle 会挂载 `/var/run/docker.sock`，这样面板在重写 `/data/nginx-managed-http.conf` 后，可以执行默认的 `docker kill -s HUP v2ray-panel-nginx` 给同机 NGINX 容器发 reload 信号。

完整双机 bundle 说明见 [deploy/split-architecture/README.md](deploy/split-architecture/README.md)。

### 4. 打开面板

```text
http://your-host:2016/admin
```

如果你改了 `PANEL_PORT`，把 `2016` 换成实际值。

### 5. 创建端口

在面板里填写：

- 端口
- 备注
- 流量上限
- 到期时间
- 可选 UUID
- 可选 WebSocket 路径

创建成功后，每个端口会看到三类链接：

- `V2Ray 订阅`
- `Clash 订阅`
- `链接信息`

## 面板接口

### 页面

- `GET /admin`

### 管理 API

- `GET /api/settings`
- `PATCH /api/settings`
- `GET /api/ports`
- `POST /api/ports`
- `PATCH /api/ports/{port}`
- `POST /api/ports/{port}/reset-traffic`
- `POST /api/ports/{port}/sync`
- `POST /api/sync`
- `DELETE /api/ports/{port}`
- `GET /links/{token}`

### 订阅接口

- `GET /subscriptions/{token}/v2ray`
- `GET /subscriptions/{token}/clash`

## 关键环境变量

### V2Ray 容器

- `V2RAY_API_PORT`
  V2Ray 官方 API 监听端口，默认 `10085`
- `V2RAY_LOG_LEVEL`
  日志级别，默认 `warning`
- `V2RAY_PORT_RANGE_START`
  对外发布的用户端口范围起始值
- `V2RAY_PORT_RANGE_END`
  对外发布的用户端口范围结束值

### 面板容器

- `API_PORT`
  面板监听端口，默认 `2016`
- `STATE_FILE`
  状态文件路径，默认 `/data/ports.json`
- `SYNC_INTERVAL_SECONDS`
  后台同步周期，默认 `30`
- `PANEL_PUBLIC_BASE_URL`
  手动指定对外访问面板的基础 URL
- `PANEL_INTERNAL_BASE_URL`
  `subconverter` 访问面板订阅源时使用的内部地址
- `V2RAY_PUBLIC_HOST`
  VMess 配置里使用的服务器主机名或 IP
- `V2RAY_PUBLIC_TLS`
  是否在 VMess 配置中标记 `tls`
- `V2RAY_UPSTREAM_HOST`
  NGINX 转发到的固定 V2Ray 主机
- `V2RAY_UPSTREAM_PORT`
  NGINX 转发到的固定 V2Ray 端口
- `SUBCONVERTER_INTERNAL_URL`
  内部 `subconverter` 地址，默认 `http://subconverter:25500/sub`
- `V2RAY_SUBCONVERTER_TEMPLATE`
  Clash 模板地址
- `TRAFFIC_STATS_SOURCE`
  流量统计来源。默认 `v2ray`，读取 V2Ray StatsService；设置为 `nginx_json` 或 `nginx` 时读取 NGINX JSON 文件
- `NGINX_TRAFFIC_STATS_FILE`
  NGINX JSON 统计文件路径，默认 `/data/nginx-traffic.json`。只有 `TRAFFIC_STATS_SOURCE=nginx_json` 或 `nginx` 时使用
- `NGINX_MANAGED_CONFIG_PATH`
  面板生成的 NGINX include 文件路径，默认 `/data/nginx-managed-http.conf`
- `NGINX_RELOAD_COMMAND`
  端口配置变更后执行的 NGINX reload 命令，默认 `nginx -s reload`

## 服务器设置

管理面板的“服务器设置”会保存到 `data/ports.json`:

```json
{
  "server": {
    "v2ray_api_server": "127.0.0.1:10085",
    "fixed_v2ray_upstream_host": "host.docker.internal",
    "fixed_v2ray_upstream_port": 10085,
    "public_v2ray_host": "example.com",
    "public_tls": false,
    "traffic_stats_source": "v2ray",
    "nginx_stats_json_path": "/data/nginx-traffic.json",
    "nginx_config_output_path": "/data/nginx-managed-http.conf",
    "nginx_reload_command": "nginx -s reload"
  }
}
```

`GET /api/settings` 返回当前设置。`PATCH /api/settings` 接受以上字段的部分更新，`traffic_stats_source` 支持 `v2ray` 和 `nginx_json`。保存后无需重建容器：下一次运行时同步会使用新的 V2Ray API 地址、固定 upstream、订阅公开主机和 TLS 标记、NGINX include 输出路径、reload 命令，以及新的流量来源和 NGINX JSON 路径。

## NGINX 受管端口模式

当实际入口是外层 NGINX，且 NGINX 代理到一个固定 V2Ray upstream 时，面板会生成受管的 NGINX include，并在变更后执行 reload。你还可以让 NGINX 或旁路日志聚合作业写出每个端口的累计字节数，面板读取这个文件作为流量来源。

配置示例：

```env
TRAFFIC_STATS_SOURCE=nginx_json
NGINX_TRAFFIC_STATS_FILE=/data/nginx-traffic.json
NGINX_MANAGED_CONFIG_PATH=/data/nginx-managed-http.conf
NGINX_RELOAD_COMMAND=nginx -s reload
```

`NGINX_TRAFFIC_STATS_FILE` 必须是面板容器可读路径。根目录 `docker-compose.yml` 和 `deploy/split-architecture/panel-host/docker-compose.yml` 都会把宿主机 `./data` 挂载到容器 `/data`，所以可以让 NGINX 侧任务把统计结果写到 `./data/nginx-traffic.json`。

`NGINX_MANAGED_CONFIG_PATH` 应该被你的主 NGINX 配置以 `include` 的方式加载，并且这个文件需要位于 HTTP 上下文可接受的位置。面板生成的内容会为每个活跃端口写出一个 `server` 块，只允许该端口自己的 `ws_path` 代理到固定 upstream，其他路径返回 `404`。

文件格式是一个 JSON 对象，key 是端口字符串，value 是该端口从 NGINX 视角看到的累计字节数。每个面板管理中的端口都必须有对应 key：

```json
{
  "20001": 123456789,
  "20002": 987654321
}
```

这些值必须是非负整数，并且是单调递增的累计总量。面板的“流量清零”不会修改 NGINX 文件，而是把当前累计值记录为 `traffic_reset_base_bytes`，之后显示 `当前累计值 - 清零基线`。如果文件不存在、JSON 无效、某个端口缺少累计值或某个值非法，面板会保留上一次已知用量，并在端口的同步错误里显示失败原因，避免把流量猜成 0。

## 持久化

- 所有端口状态保存在 `data/ports.json`
- NGINX JSON 流量模式下，默认统计文件可放在 `data/nginx-traffic.json`
- `docker compose down` 后，只要 `data/` 目录还在，端口配置就会保留
- NGINX 受管 include 会在下一次同步或端口变更时重新生成
- 双机部署时，需要保留 `deploy/split-architecture/panel-host/data/` 目录

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前覆盖：

- 状态文件校验和持久化
- 服务器设置 API、校验和持久化默认值
- 订阅链接生成
- 端口增删改查
- 启用禁用
- 流量清零
- V2Ray/NGINX 流量来源切换和同步错误保留
- NGINX include 生成和 reload 失败保护

## 已知限制

- 当前只支持一个固定 upstream，不做多节点调度
- 目前只支持 `vmess + ws`
- 面板没有登录鉴权，默认用于自用或内网环境
- 流量统计精度受同步周期和所选统计来源影响；NGINX 模式要求外部任务持续写入累计 JSON 文件
- 当前 NGINX 受管 include 依赖部署侧把生成文件正确 `include` 到主配置中
