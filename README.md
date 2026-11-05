# QQ 群机器人管理后台

本地运行的管理后台：人格提示词、服务端模型白名单与默认模型、会话搜索、逐条消息和调用详情。后端为 FastAPI，数据保存在 SQLite，前端为原生 HTML/CSS/JavaScript。

## 启动

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

macOS 已安装依赖后，可双击项目中的 `server.command`，或在项目目录执行：

```bash
./server.command start
./server.command status
./server.command stop
```

`start` 使用独立进程会话运行服务，标准输入断开，输出写入 `data/server.log`，不会依赖启动终端保持打开。PID 保存在 `data/server.pid`，重复启动会检查已有服务，不会创建多个实例。此方式不会设置开机自动启动；注销或重启电脑后需重新启动。`stop` 只会停止 PID 与本项目启动命令匹配的进程。

首次安装依赖或在其他系统上以前台方式运行：

```bash
uv sync --locked
cp .env.example .env
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765>，首次访问创建管理员密码（至少 8 个字符），此后用该密码登录。没有默认密码。首次创建仅允许本机访问；远程服务器可以使用 SSH 端口转发完成初始化。应用默认只监听本机，不会自动公开。

未配置 OpenAI / QQ 凭证时，后台配置与历史查看仍可使用；连接状态明确标为“未配置”。不会生成模拟回复。测试会话按钮只有在 OpenAI Key 已配置后才能发送；服务端仍会校验真实调用结果。

## 模型 API 配置（New API / OpenAI 兼容接口）

将 New API 签发的调用令牌和地址填入 `.env`。当前本机部署使用：

```dotenv
OPENAI_BASE_URL=http://127.0.0.1:3000/v1
OPENAI_API_KEY=New API签发的调用令牌
```

重启服务后生效。地址由配置显式传入模型客户端，不会失败后切换服务或模型。未设置地址时使用 `https://api.openai.com/v1`；空地址或带内嵌凭证的地址会明确报错。Key 不经过前端，不写入机器人数据库或提交到 Git。当前配置从本机中转站的私有凭证文件写入，未在聊天中展示。

模型列表来自当前 `OPENAI_BASE_URL` 的 `/models` 接口，不再预置两个固定模型。进入后台时自动同步，也可以在“模型路由”点击“刷新上游模型”。新增模型自动加入并启用，已存在模型保留启用状态；默认模型仍在上游时保留，移除时清空并提示重新选择，不会自动切换模型。首次使用需选择默认模型并保存。

同步失败明确显示错误，保留上次配置；空列表表示当前令牌没有可用模型。重复 ID 去重，空白或无效 ID 会排除并显示告警。测试会话的下拉框与模型路由共用这份列表。可见不等于一定支持文字 Responses 请求，具体权限、额度、速率限制仍以实际请求为准。

后台测试可选择任意已启用模型，QQ群提问使用默认模型。未允许的模型在消息入队前拒绝；若管理员在请求排队期间禁用模型，执行前会再次拒绝。已经发出的上游请求不会因为修改配置而被撤回。调用失败不会自动换模型或重试。

官方资料：[Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)、[Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)、[API Quickstart](https://developers.openai.com/api/docs/quickstart)。

## 人格和会话

- 人格、默认模型、上下文条数、最大输出 Token 保存于数据库，重启后保留。配置带版本号，多个页面同时修改会明确报冲突。
- 新提问入队时保存配置快照，后续修改不改变历史记录。模型规则仍会在真正调用前重新核对。
- QQ 每个群是一个独立会话；后台测试会话单独隔离。上下文按消息顺序截取，最多 100 条、64,000 字符。超出字符预算时从最早的消息开始排除，调用详情记录实际选取的内容。
- “清空上下文”只设立新的上下文起点，历史记录与调用快照继续保留。处理中不能清空。
- 会话可按名称、群 OpenID、消息内容搜索，并按 QQ / 后台测试筛选。消息历史分页加载，调用详情包括请求模型、人格版本、实际上下文、Token 用量、耗时、Response ID 和错误。
- “实际模型”字段是实际发送给 OpenAI 的模型 ID（如果使用别名，平台可能将其解析到内部快照）。
- 历史仅包含服务接入后实际收到的消息，不会自动拉取 QQ 旧聊天记录。当前版本支持文本；非文本消息显示明确说明，不声称看懂图片或语音。
- 群友的姓名、正文等作为 `user` 数据发送，不拼接为系统指令。后台不提供执行命令或工具调用能力。

当前版本没有自动删除历史：SQLite、人格快照、上下文快照均会持续保留；部署者需按群的需求安排数据保留与备份。聊天上下文会发送至配置的模型服务及其上游，Responses 请求使用 `store=false`；这不等于承诺上游完全不保留任何日志。启用群上下文前应向群成员说明用途。

## 普通 QQ 号入群（本次部署使用）

### 回复时 @成员

机器人在被 @ 后生成的回复中，可以按语义提及本次上下文中的成员。例如在群中 @机器人后说“请 @我并说收到”，或真正 @另一位成员并要求向对方打招呼。普通群消息仍只作为上下文，不会主动触发发言。

可提及对象来自当前群已收到的发言者和实际 @消息段；不会按昵称猜 QQ 号，也不会读取其他群的成员。尚未出现在上下文中的成员，需要先发言或被真实 @。同名时应使用实际 @来明确目标。清空上下文也会清除旧记录提供的可提及对象。

模型使用内部标记 `[[at:成员ID]]` 指定目标，后端统一校验后转换：NapCat / OneBot 生成独立 `at` 消息段；官方 QQ 生成 Markdown 消息中的 `<qqbot-at-user>` 标签。普通回复保留文本发送。未知 ID、@全体成员或不完整的内部标记会明确报错并停止发送；CQ 字符串不会被当作动作执行。行内代码、代码块、引用块中的内部标记作为示例文字保留，不会触发 @。

历史消息显示可读的 `@成员名`，调用详情的上下文保留可提及成员 ID。新增 `message_members` 表保存真实 @关系；既有 OneBot 发言者使用已保存的 `sender_id`，无需重新建立成员列表。更新不会改写旧消息。

协议参考：[OneBot @消息段](https://github.com/botuniverse/onebot-11/blob/master/message/segment.md)、[QQ 官方文本交互](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/text-chain.html)。

### 接入方式

链路：普通 QQ 号 → NapCat → OneBot HTTP 上报 → 本后台 → New API → 模型；回复通过 NapCat 发回同一个群。

1. 保持 OrbStack 运行，在 `infra/napcat` 执行 `docker compose up -d`。
2. 打开 <http://127.0.0.1:6099/webui>，用 `infra/napcat/credentials.json` 中的 `webui_token` 登录管理面板，再用手机 QQ 扫码确认。
3. 将登录的 QQ 号加入目标群，用另一个账号在群里真正 @ 它并发送文字。手动输入名字或 CQ 代码不是有效的 @。
4. 在 <http://127.0.0.1:8765> 查看会话与调用详情；“QQ 已在线”来自 NapCat 状态查询，“已发送到 QQ”要求发送接口实际返回消息 ID。

```dotenv
QQ_TRANSPORT=onebot
ONEBOT_API_URL=http://127.0.0.1:3001
ONEBOT_API_TOKEN=与NapCat的HTTP服务Token一致
ONEBOT_EVENT_SECRET=与NapCat的HTTP客户端Token一致
ALLOWED_HOSTS=localhost,127.0.0.1,[::1],host.docker.internal
```

本机运行配置已经生成，不需要再次手工填 Key。`ONEBOT_API_TOKEN` 以 Bearer 方式鉴权；`ONEBOT_EVENT_SECRET` 用于校验原始上报正文的 HMAC-SHA1，二者不是同一种协议。
HTTP 上报地址为 `http://host.docker.internal:8765/webhooks/onebot`，格式为 `array`。QQ 登录完成后 NapCat 才会加载 OneBot 网络配置。

只处理群消息，忽略私聊、自己的消息和非消息事件；普通群聊只进入上下文，准确 @ 当前账号才入队回复，@全体成员不会触发。会话按“QQ账号 + 群号”隔离，重复消息 ID 不重复调用。切换登录账号后不会用新账号发送旧账号的排队回复。
文字中的 CQ 字符串按纯文字发送，不会被解释为 @全体或其他动作。图片、语音等只记录非文本标记，当前版本不分析其内容。

NapCat 是非官方 QQ 接入方式，有登录风控和客户端变动风险；本项目使用独立服务和公开 OneBot 接口，没有复制其内部代码。登录、配置和插件数据保存在 `infra/napcat/data/`。
NapCat 反向 HTTP 上报失败只记录错误，不保证离线期间消息补发。保持 Mac 唤醒、OrbStack 和本后台运行；无需公网域名或 QQ 开放平台凭证。

部署、密钥位置及更新说明见 [NapCat 部署说明](infra/napcat/README.md)。

## 接入 QQ 官方机器人

这是保留的另一种接入方式；普通 QQ 部署无需执行本节。使用时显式设置 `QQ_TRANSPORT=official`。两种消息入口不会同时接收。

1. 在 [QQ 开放平台](https://q.qq.com/) 创建机器人，取得 `AppID` / `AppSecret`，填入 `.env` 的 `QQ_APP_ID` 和 `QQ_APP_SECRET`，重启服务。
2. 给 `/webhooks/qq` 配置公网 HTTPS 回调地址，在 QQ 后台订阅群消息事件。当前实现支持官方 Webhook，不提供 WebSocket 客户端。
3. `GROUP_AT_MESSAGE_CREATE` 触发回答。要把普通群聊作为上下文，需要开启 QQ 的“接收所有消息”权限，接收 `GROUP_MESSAGE_CREATE`。
4. 全量模式必须配置 `QQ_BOT_ID` 为 `mentions` 中机器人对应的 OpenID（不是自行假定为 AppID）。仅当消息明确 @ 这个 ID 时回复。未配置时返回明确 503，避免将所有群消息误判为提问。
5. QQ 普通回调使用 AppID 和 Ed25519 签名校验；`op=13` 完成官方地址验证。消息持久化后立即确认回调，后台队列生成并发送回答。

地址验证的 `event_ts` 必须为数字，`plain_token` 接受字母、数字和 Base64 / URL-safe token 字符。不能把 JSON 事件正文作为验证 token 提交签名，避免公开验证端点被用来伪造事件。该校验覆盖官方验证示例；若平台改变 token 格式，应根据实际协议调整校验并补充测试。

后台 QQ 会话只读，不能从后台直接对真实群发送消息。使用“测试会话”调试人格，避免把测试消息发给群友。自动回复会通过 QQ 官方发送接口发到触发群。

公开 QQ 回调时，建议反向代理只公开 `/webhooks/qq`，后台通过本机或 SSH 隧道访问。若需要公网访问整个后台，必须使用 HTTPS，在 `.env` 的 `ALLOWED_HOSTS` 增加实际域名，并设置 `SECURE_COOKIE=true`。管理员密码使用 scrypt 哈希；Session 在数据库中存储散列，12 小时过期，修改接口校验 CSRF。前端对消息使用文本转义和 CSP。

```dotenv
ALLOWED_HOSTS=localhost,127.0.0.1,bot.example.com
SECURE_COOKIE=true
```

`SECURE_COOKIE=true` 时请通过 HTTPS 访问，浏览器不会在普通 HTTP 发送该 cookie。

官方资料：[回调与事件](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)、[签名](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html)、[全量消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html)、[发送群消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)。

## 队列与部署边界

第一版面向小规模群聊：单进程、单个持久化队列按顺序处理。请勿使用多个 uvicorn worker 或启动多个实例共享同一数据库。官方机器人有被动回复窗口；NapCat 普通号不使用这套官方窗口规则，但密集消息仍会增加等待时间。错误记录在会话详情中。扩大使用规模前需改为按群串行、群间并行的队列。

服务重启后保留未开始的请求；处理中被中断的请求标为失败，不自动重发，以免重复回复。QQ 发送超时无法确定对端是否收到，记录为发送失败；不会伪装为送达，也不会自动重试。

## 验证

```bash
uv run python -c 'import subprocess; subprocess.run([".venv/bin/pytest", "-q"], timeout=60, check=True)'
uv run ruff check app tests
uv run ruff format --check app tests
node --check app/static/app.js
```

测试使用临时数据库和显式测试替身，覆盖管理员权限、CSRF、配置冲突、模型白名单、人格快照、会话隔离、上下文重置、历史分页、官方 QQ 签名向量、去重、发送失败及 OpenAI 请求格式。真实 API / QQ 端到端联调仍需要有效凭证及测试群。

浏览器测试使用独立临时数据库，不会修改实际后台配置：

```bash
npm ci
npx playwright install chromium
npm run test:ui
```

## 主要文件

```text
app/main.py           HTTP 接口、后台权限与 QQ 回调
app/auth.py           密码、Session、CSRF
app/schemas.py        输入与模型路由不变量
app/store.py          SQLite 配置、消息、调用快照与队列
app/service.py        上下文选取、调用与发送流程
app/providers.py      OpenAI / QQ 官方接口适配
app/onebot.py         NapCat OneBot 群消息与发送适配
app/static/           管理后台页面
tests/                后端和浏览器测试
data/bot.sqlite3      本地运行数据（已忽略）
```
