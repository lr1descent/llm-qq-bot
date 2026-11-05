# Hekun AI Gateway

基于 [QuantumNous/new-api](https://github.com/QuantumNous/new-api) 的本机自用中转站。

## 入口与当前状态

- New API 控制台：<http://127.0.0.1:3000>
- 客户端 Base URL：`http://127.0.0.1:3000/v1`
- CPA 管理面板：<http://127.0.0.1:8317/management.html>
- 管理员用户名：`hekun`
- 管理员密码、New API 调用令牌、CPA 管理密钥：本目录的 `credentials.json`，权限为 `0600`。

已完成 New API 初始化、自用模式、站名、关闭公开注册，以及 `qq-bot` 调用令牌创建。
CPA 渠道已创建，初始状态为禁用、模型列表为空，等待账号授权。没有导入机器上其他应用的账号凭证。
`qq-bot` 令牌未设置站内额外额度上限；它不授予管理权限，也不解除上游账号的额度限制。

实际链路：客户端 → New API → CPA → 你授权的上游账号。
New API 的账户余额是本站内部记账，不能作为 OpenAI 实际余额或订阅剩余额度的证据。

## 版本与运行

- New API：`v1.0.0-rc.48`，固定镜像 digest。版本名仍带 `rc`，不标称稳定版。
- CLIProxyAPI：`v8.0.56`，固定镜像 digest。
- New API 使用 SQLite，数据持久化至 `data/new-api/`。
- CPA 授权目录为 `data/cpa/auths/`，配置为 `.cpa.yaml`。
- 主机端口仅绑定 `127.0.0.1`；当前是本地 HTTP 服务，没有公网域名。

在本目录运行：

```bash
docker compose up -d --wait
docker compose ps
docker compose logs --tail 80
docker compose stop
```

需要先启动 OrbStack。容器可在 Docker 启动后自动恢复，Mac 休眠时不能提供持续服务。

## 完成上游账号接入

1. 打开 CPA 管理面板，以 `credentials.json` 中的 `cpa_management_key` 登录。
2. 通过面板的 Codex OAuth 登录流程授权你自己的账号；若使用 SIWC，继续完成该凭证的 SIWC 绑定。
3. 在 New API 控制台的渠道管理中编辑 `CPA · 待账号授权`。
4. 从上游获取真实模型列表，选择需要的模型，保存并启用渠道，再执行一次真实模型测试。

也可以在本目录启动 Codex 登录命令，由浏览器完成登录：

```bash
docker compose exec cpa /CLIProxyAPI/CLIProxyAPI --codex-login --no-browser
```

该流程使用本机 `1455` 回调端口，要求端口未被其他登录流程占用。不要同时开启多个 OAuth 登录流程。

渠道类型为 OpenAI，渠道 Base URL 必须保持 `http://cpa:8317`，**不加 `/v1`**。
New API 会拼接请求路径；客户端访问 New API 时的 Base URL 则需要 `/v1`。
渠道密钥已经设为 CPA 客户端密钥，不是 CPA 管理密钥，也不是官方 API Key。

授权完成后，先确认真实推理成功，再把 QQ 机器人接到此站点。
QQ 机器人现已支持 `OPENAI_BASE_URL`，本机 `.env` 已设为此站点的 `/v1` 地址，并使用本站签发的调用令牌。普通 QQ 号通过 NapCat 接入，详见项目根目录 README。

## 验证边界

健康检查、管理员 API、API Key 鉴权、模型发现以及重启后的配置持久化可以在账号授权前验证。
没有上游账号时，模型列表应为空；这不代表推理已成功。
真实推理、订阅额度路径、QQ 消息收发尚需授权后验证。

## 备份与升级

SQLite 备份时先停止 New API，复制整个 `data/new-api/`，再启动服务。
同时备份 `.env`、`.cpa.yaml`、`credentials.json` 和 CPA 授权目录，备份中含敏感凭证。
不要在运行中只复制 SQLite 主文件而遗漏 WAL。

升级前备份；修改固定版本和 digest 后重新拉取并启动，验证数据库迁移和真实推理。
不要用 `latest` 自动替换目前验证过的部署。

## 公网部署与许可证

此配置是本机自用版本。迁移公网时需要确定服务器、域名与 HTTPS，按上游文档配置
`SESSION_COOKIE_SECURE=true`、精确的 `SESSION_COOKIE_TRUSTED_URL` 和反向代理，不能直接暴露本地 HTTP 配置。

New API 为 AGPL-3.0，CPA 为 MIT。本部署使用原版镜像，保留原有许可证与署名。
修改后对外提供服务时应遵循各项目许可证。

参考：[New API 发布版](https://github.com/QuantumNous/new-api/releases/tag/v1.0.0-rc.48)、
[New API 会话说明](https://github.com/QuantumNous/new-api/blob/v1.0.0-rc.48/docs/authentication.md)、
[CPA v8 管理文档](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.56/docs/management-api-v8.md)。
