# v2ray

一个基于 Docker Compose 的单机多端口 V2Ray 管理面板。

现在的结构不是“单端口订阅生成器”，而是三部分：

- `v2ray`：运行 `v2fly/v2fly-core`，通过官方 API 动态增删入站端口
- `v2ray-panel`：提供管理页面、JSON API、流量统计、到期控制和订阅链接
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
- V2Ray 重启后，面板会根据 JSON 状态重新下发有效端口

## 目录

```text
.
├── api/
│   ├── models.py
│   ├── runtime.py
│   ├── server.py
│   ├── service.py
│   ├── store.py
│   └── subscriptions.py
├── data/
│   └── .gitkeep
├── docker/
│   ├── api.Dockerfile
│   ├── Dockerfile
│   └── entrypoint.sh
├── tests/
│   └── test_panel.py
├── v2ray/
│   └── config.template.json
└── docker-compose.yml
```

## 工作方式

- `v2ray-panel` 监听固定管理端口，默认 `2016`
- `v2ray` 对外发布一段固定端口范围，默认 `20000-20100`
- 面板创建端口时，会通过 `v2ray api adi` 调用 V2Ray 官方 API 动态注册入站
- 面板后台轮询 V2Ray 统计接口，更新每个端口的已用流量
- 端口流量耗尽或到期后，面板会自动把该端口从运行中的 V2Ray 实例移除

## 快速开始

### 1. 配置 `.env`

建议最少包含这些值：

```env
PANEL_PORT=2016
V2RAY_API_PORT=10085
V2RAY_PORT_RANGE_START=20000
V2RAY_PORT_RANGE_END=20100
V2RAY_PUBLIC_HOST=your-server-ip-or-domain
V2RAY_PUBLIC_TLS=false
V2RAY_SUBCONVERTER_TEMPLATE=https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/config/ACL4SSR_Online.ini
```

说明：

- `V2RAY_PUBLIC_HOST` 会写入每个端口生成出来的 VMess 配置
- 如果你在外层自己做了 TLS 终止，可以设置 `V2RAY_PUBLIC_TLS=true`
- 用户端口必须落在 `V2RAY_PORT_RANGE_START` 到 `V2RAY_PORT_RANGE_END` 之间

### 2. 启动

```bash
docker compose up -d --build
```

### 3. 打开面板

```text
http://your-host:2016/admin
```

如果你改了 `PANEL_PORT`，把 `2016` 换成实际值。

### 4. 创建端口

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
- `V2RAY_API_COMMAND`
  面板内调用 V2Ray CLI 的路径，默认 `/usr/local/bin/v2ray`
- `V2RAY_API_SERVER`
  面板访问 V2Ray 官方 API 的地址，默认 `v2ray:10085`
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
- `SUBCONVERTER_INTERNAL_URL`
  内部 `subconverter` 地址，默认 `http://subconverter:25500/sub`
- `V2RAY_SUBCONVERTER_TEMPLATE`
  Clash 模板地址

## 持久化

- 所有端口状态保存在 `data/ports.json`
- `docker compose down` 后，只要 `data/` 目录还在，端口配置就会保留
- V2Ray 本身的动态入站不写回核心配置文件，所以容器重启后的恢复由面板完成

## 测试

```bash
python3 -m unittest discover -s tests -v
```

当前覆盖：

- 状态文件校验和持久化
- 订阅链接生成
- 端口增删改查
- 启用禁用
- 流量清零

## 已知限制

- 目前只做单机，不做多节点调度
- 目前只支持 `vmess + ws`
- 面板没有登录鉴权，默认用于自用或内网环境
- 流量统计依赖 V2Ray 内部统计接口，精度受同步周期影响
