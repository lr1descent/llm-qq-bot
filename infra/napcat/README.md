# NapCat：普通 QQ 号接入

使用 [NapCat-Docker 官方镜像](https://github.com/NapNeko/NapCat-Docker)，固定 `v4.18.42` 和镜像 digest，已在本机 OrbStack ARM64 环境启动。

## 本机入口

- NapCat WebUI：<http://127.0.0.1:6099/webui>
- 机器人后台：<http://127.0.0.1:8765>
- OneBot HTTP API：`http://127.0.0.1:3001`
- New API：<http://127.0.0.1:3000>

本目录 `credentials.json` 保存 WebUI Token、OneBot API Token、HTTP 上报签名密钥，已排除 Git。
QQ 登录使用手机扫码；不会读取其他 QQ 客户端的凭证。二维码过期时点击“刷新二维码”。

## 运行

在本目录执行：

```bash
docker compose up -d
docker compose ps
docker compose stop
```

在项目根目录执行 `./server.command start` 启动机器人后端。更新 `.env` 后执行 `stop`，再执行 `start`。
不要同时运行两个后端实例共享同一个机器人数据库。

## 网络配置

`data/config/onebot11.json` 已预配置 HTTP 服务与 HTTP 客户端。首次登录时 NapCat 会按账号加载配置；后续以 WebUI 当前账号的网络配置为准，修改通用模板不会覆盖已存在的账号配置。

| 配置 | 值 |
|---|---|
| HTTP 服务 | 容器内 `0.0.0.0:3000`，宿主机只发布 `127.0.0.1:3001` |
| API 鉴权 | `Authorization: Bearer ONEBOT_API_TOKEN` |
| HTTP 客户端 URL | `http://host.docker.internal:8765/webhooks/onebot` |
| HTTP 客户端 Token | `ONEBOT_EVENT_SECRET`，作为 HMAC-SHA1 密钥 |
| 消息格式 | `array` |
| 上报自身消息 | 关闭 |

WebUI 的 Token 与以上两种密钥独立。容器内不能用 `127.0.0.1:8765` 指代宿主机的后端。

## 数据与维护

- `data/qq/`：QQ 登录状态与客户端数据。
- `data/config/`：NapCat 配置，含敏感凭证。
- `data/plugins/`：插件持久化目录。
- 后端群聊与调用历史在项目根目录 `data/bot.sqlite3`。

容器日志限制为 3 份，每份 10 MB。上游启动日志可能包含 WebUI 登录 Token 和二维码链接，不要将原始日志公开。
升级前备份 QQ/配置目录，按发布记录更新 Compose 中的 tag 与 digest，再验证登录、上报和回复。

本机监听范围仅为 loopback，没有申请公网域名或暴露管理面板。Mac 休眠、OrbStack 停止或后端停止时不能保证群消息接收。
NapCat 上报失败没有可靠重放队列；后端仅对已经收到并写入数据库的请求负责。

源码依据：[网络 schema](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/config/config.ts)、[HTTP 上报签名](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/network/http-client.ts)、[群消息发送](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/group/SendGroupMsg.ts)。
