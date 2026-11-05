"use strict";

const $ = (selector) => document.querySelector(selector);
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const state = {
  csrf: null,
  settings: null,
  status: null,
  initialized: false,
  page: "",
  selected: null,
  detail: null,
  offset: 0,
  query: "",
  source: "",
  total: 0,
  generation: 0,
  dirty: false,
  timer: null,
  modelSyncError: "",
  modelWarnings: [],
};
const pageTitles = {
  conversations: "会话记录",
  persona: "人格提示词",
  models: "模型路由",
};
const statusLabels = {
  queued: "等待处理",
  running: "正在生成",
  completed: "已完成",
  failed: "生成失败",
  delivery_failed: "发送失败",
};
let noticeTimer;

function notify(message, error = false) {
  clearTimeout(noticeTimer);
  const box = $("#notice");
  box.textContent = message;
  box.className = error ? "error-notice" : "";
  box.hidden = false;
  noticeTimer = setTimeout(
    () => {
      box.hidden = true;
    },
    error ? 9000 : 3500,
  );
}

async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (options.method && options.method !== "GET") {
    headers["Content-Type"] = "application/json";
    if (state.csrf) headers["X-CSRF-Token"] = state.csrf;
  }
  const response = await fetch(`/api${path}`, { ...options, headers });
  const body = await response.json();
  if (!response.ok) {
    if (response.status === 401 && state.csrf) {
      state.csrf = null;
      showAuth();
    }
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `请求失败（${response.status}）`,
    );
  }
  return body;
}

function formatTime(value, short = false) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(
    "zh-CN",
    short
      ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
      : { dateStyle: "medium", timeStyle: "short" },
  ).format(new Date(value));
}

function showAuth() {
  clearTimeout(state.timer);
  $("#workspace").hidden = true;
  $("#auth-view").hidden = false;
  $("#auth-title").textContent = state.initialized ? "欢迎回来" : "创建管理员";
  $("#auth-description").textContent = state.initialized
    ? "登录以管理机器人，查看会话。"
    : "首次使用，请为这个后台设置一个密码。";
  $("#auth-submit").textContent = state.initialized
    ? "进入后台 →"
    : "创建并进入 →";
  $("#confirm-field").hidden = state.initialized;
  $("#confirm-password").required = !state.initialized;
  $("#password").autocomplete = state.initialized
    ? "current-password"
    : "new-password";
}

async function boot() {
  try {
    const auth = await api("/auth/status");
    state.initialized = auth.initialized;
    state.csrf = auth.csrf;
    if (!auth.authenticated) {
      showAuth();
      return;
    }
    await enter();
  } catch (error) {
    showAuth();
    $("#auth-error").textContent = error.message;
  }
}

async function enter() {
  [state.settings, state.status] = await Promise.all([
    api("/settings"),
    api("/status"),
  ]);
  $("#auth-view").hidden = true;
  $("#workspace").hidden = false;
  renderConnections();
  await syncModels();
  await route();
}

async function syncModels() {
  state.modelSyncError = "";
  state.modelWarnings = [];
  if (!state.status.openai_configured) {
    state.modelSyncError = "尚未配置模型 API，无法获取上游模型列表。";
    return;
  }
  try {
    const result = await api("/models/sync", { method: "POST", body: "{}" });
    state.settings = result.settings;
    state.modelWarnings = result.warnings;
  } catch (error) {
    state.modelSyncError = `同步失败：${error.message} 以下保留上次保存的配置。`;
    notify(state.modelSyncError, true);
  }
}

function modelNotice() {
  const notes = [...state.modelWarnings];
  if (state.modelSyncError) notes.push(state.modelSyncError);
  if (!state.settings.default_model)
    notes.push("尚未选择默认模型，请在模型路由中选择并保存后再使用群机器人。");
  return notes.map((note) => `<p class="connection-warning">${escapeHTML(note)}</p>`).join("");
}

function renderConnections() {
  const onebot = state.status.qq_transport === "onebot";
  const qqStatus = !state.status.qq_configured
    ? "未配置"
    : onebot
      ? state.status.onebot_online ? "QQ 已在线" : "等待登录 / 未连接"
      : "已配置";
  $("#connections").innerHTML =
    `<div class="connection-row"><span>模型 API</span><span>${state.status.openai_configured ? "已配置" : "未配置"}</span></div><div class="connection-row"><span>${onebot ? "NapCat / QQ" : "QQ 官方机器人"}</span><span>${qqStatus}</span></div>${state.status.onebot_error ? `<p class="small muted">${escapeHTML(state.status.onebot_error)}</p>` : ""}`;
}

async function route() {
  if (!state.csrf) return;
  clearTimeout(state.timer);
  const target = location.hash.slice(1).split("/")[0];
  state.page = Object.hasOwn(pageTitles, target) ? target : "conversations";
  state.generation++;
  state.dirty = false;
  $("#breadcrumb").textContent = pageTitles[state.page];
  document
    .querySelectorAll("[data-nav]")
    .forEach((a) => a.classList.toggle("active", a.dataset.nav === state.page));
  $("#page").innerHTML = '<div class="loading">正在读取…</div>';
  try {
    state.settings = await api("/settings");
    if (state.page === "persona") renderPersona();
    else if (state.page === "models") renderModels();
    else await renderConversations();
  } catch (error) {
    $("#page").innerHTML =
      `<div class="empty-state"><h3>无法读取页面</h3><p>${escapeHTML(error.message)}</p><button data-action="refresh">重新加载</button></div>`;
  }
}

function heading(title, description, actions = "") {
  return `<div class="page-heading"><div><h1>${title}</h1><p>${description}</p></div><div class="page-actions">${actions}</div></div>`;
}

function renderPersona() {
  const s = state.settings;
  $("#page").innerHTML =
    heading("人格提示词", "定义它是谁，以及如何参与一场对话。") +
    `
    <div class="settings-layout"><form id="persona-form" class="panel">
      <div class="panel-title"><h3>人格设定</h3><span class="small muted">配置版本 ${s.revision}</span></div>
      <div class="panel-body"><label for="persona">系统提示词</label><textarea id="persona" class="persona-editor" maxlength="16000" required>${escapeHTML(s.persona)}</textarea>
      <div class="editor-footer"><span>保存后，对新收到的提问生效</span><span id="char-count">${s.persona.length.toLocaleString()} / 16,000</span></div>
      <div class="form-grid"><div><label for="context-messages">上下文消息数</label><input id="context-messages" type="number" min="1" max="100" value="${s.context_messages}" required><p class="small muted">每次回答最多读取本群最近多少条消息。</p></div><div><label for="max-output">最大输出 Token</label><input id="max-output" type="number" min="256" max="16384" value="${s.max_output_tokens}" required><p class="small muted">包括模型推理用量；过小可能无法生成完整回答。</p></div></div></div>
      <div class="form-footer"><span id="save-state" class="small muted">已保存</span><button class="primary" type="submit">保存人格设定</button></div>
    </form><aside class="settings-aside"><section><h3>写出你想要的性格</h3><p>可以描述说话语气、兴趣、回复长度，以及它与群成员的相处方式。</p></section><section><h3>上下文按群隔离</h3><p>机器人只会使用当前会话中的消息。QQ 端需开启相应消息权限，才能提供普通群聊上下文。</p></section><section><h3>修改不会改写历史</h3><p>每次回答保留当时的人格与模型配置，可在会话详情中查看。</p></section><section><button class="text-button" data-action="new-conversation">创建测试会话 →</button></section></aside></div>`;
}

function renderModels() {
  const s = state.settings;
  $("#page").innerHTML = heading(
    "模型路由", "从当前模型服务获取可选模型，并设置默认选择。",
    '<button data-action="sync-models">刷新上游模型</button>',
  ) + modelNotice() + `
    <div class="settings-layout"><div class="panel"><div class="panel-title"><h3>上游可选模型</h3><span class="small muted">配置版本 ${s.revision}</span></div>
    <table class="models-table"><thead><tr><th>模型</th><th>允许调用</th><th>默认模型</th></tr></thead><tbody id="model-rows"></tbody></table>
    <div class="model-intro"><p class="small muted">新增模型自动加入，上游移除的模型同步移除；保留已有启用状态。模型是否支持文本 Responses 请求，以实际调用结果为准。</p></div>
    <div class="form-footer"><span id="save-state" class="small muted">已保存</span><button class="primary" data-action="save-models">保存路由规则</button></div></div>
    <aside class="settings-aside"><section><h3>自动同步</h3><p>进入后台时从已配置的 API 获取模型，也可以手动刷新。不再预置固定的模型名称。</p></section><section><h3>默认模型</h3><p>QQ群提问使用默认模型，后台测试可以选择其他已启用的模型。默认模型下线时需要重新选择，不会自动改用其他模型。</p></section><section><h3>同步失败</h3><p>明确显示错误并保留上次配置，避免暂时的网络故障清空你的选择。</p></section></aside></div>`;
  renderModelRows();
}

function renderModelRows() {
  $("#model-rows").innerHTML = state.settings.models.map((m, index) =>
    `<tr><td><div class="model-name">${escapeHTML(m.id)}</div></td><td><input type="checkbox" data-enable-model="${index}" aria-label="允许 ${escapeHTML(m.id)}" ${m.enabled ? "checked" : ""}></td><td><input type="radio" name="default-model" data-default-model="${index}" aria-label="默认 ${escapeHTML(m.id)}" ${m.id === state.settings.default_model ? "checked" : ""} ${!m.enabled ? "disabled" : ""}></td></tr>`
  ).join("") || '<tr><td colspan="3">暂无可用模型，请配置上游后刷新。</td></tr>';
}

function markDirty() {
  state.dirty = true;
  if ($("#save-state")) $("#save-state").textContent = "有未保存的修改";
}

async function saveSettings(settings) {
  state.settings = await api("/settings", {
    method: "PUT",
    body: JSON.stringify(settings),
  });
  state.dirty = false;
  if (state.page === "persona") renderPersona();
  else renderModels();
  notify("已保存，新提问将使用最新配置");
}

function warning() {
  if (state.status.openai_configured) return "";
  return '<div class="connection-warning"><span>尚未配置 OpenAI API Key。可以先设置人格和模型，配置后即可开始对话。</span><button class="text-button" data-action="connection-help">配置说明 ↗</button></div>';
}

async function renderConversations() {
  $("#page").innerHTML =
    heading(
      "会话记录",
      "回看群里的对话，了解每一次回答的上下文。",
      '<button data-action="refresh-conversations">刷新</button><button class="primary" data-action="new-conversation">＋ 测试会话</button>',
    ) +
    warning() +
    modelNotice() +
    `
    <form id="search-form" class="toolbar"><input id="search" aria-label="搜索会话或消息" placeholder="搜索会话名称、群 ID 或消息内容" value="${escapeHTML(state.query)}"><select id="source-filter" aria-label="筛选来源"><option value="">所有来源</option><option value="qq" ${state.source === "qq" ? "selected" : ""}>QQ群</option><option value="console" ${state.source === "console" ? "selected" : ""}>后台测试</option></select><button type="submit">搜索</button><span class="summary" id="conversation-count"></span></form>
    <div class="conversation-layout panel"><div class="conversation-list"><div id="list-items" class="list-scroll"></div><div id="pagination" class="pagination"></div></div><section id="conversation-detail" class="conversation-detail" aria-label="会话详情"></section></div>`;
  await loadList();
  if (state.selected) await selectConversation(state.selected);
  else renderEmptyDetail();
}

async function loadList() {
  const generation = state.generation;
  const result = await api(
    `/conversations?${new URLSearchParams({ q: state.query, source: state.source, offset: state.offset, limit: 20 })}`,
  );
  if (generation !== state.generation || state.page !== "conversations") return;
  state.total = result.total;
  $("#conversation-count").textContent = `${result.total} 个会话`;
  $("#list-items").innerHTML = result.items.length
    ? result.items
        .map(
          (c) =>
            `<button data-action="select-conversation" data-id="${escapeHTML(c.id)}" class="conversation-item ${state.selected === c.id ? "selected" : ""}"><div class="item-heading"><strong>${escapeHTML(c.title)}</strong><time>${formatTime(c.updated_at, true)}</time></div><p>${escapeHTML(c.preview || "还没有消息")}</p><div class="item-meta"><span>${c.source === "qq" ? "QQ群" : "后台测试"} · ${c.message_count} 条消息</span><span>${escapeHTML(c.model || "—")}</span></div></button>`,
        )
        .join("")
    : '<div class="empty-list">暂无会话。<br>接入 QQ 后会自动记录群聊，也可以创建测试会话。</div>';
  $("#pagination").innerHTML =
    `<button data-action="prev-page" ${state.offset === 0 ? "disabled" : ""}>上一页</button><span>${result.total ? state.offset + 1 : 0}–${Math.min(state.offset + 20, result.total)} / ${result.total}</span><button data-action="next-page" ${state.offset + 20 >= result.total ? "disabled" : ""}>下一页</button>`;
}

function renderEmptyDetail() {
  $("#conversation-detail").innerHTML =
    '<div class="empty-state"><div class="symbol" aria-hidden="true">“ ”</div><h3>每一段对话，都有迹可循</h3><p>从左侧选择会话，查看完整消息与模型调用详情。</p><button data-action="new-conversation">创建测试会话</button></div>';
}

async function selectConversation(id, quiet = false) {
  clearTimeout(state.timer);
  state.selected = id;
  const generation = state.generation;
  const result = await api(`/conversations/${encodeURIComponent(id)}`);
  if (generation !== state.generation || state.selected !== id) return;
  state.detail = result;
  const draft = quiet ? $("#compose-text")?.value : "";
  const selectedModel = quiet ? $("#compose-model")?.value : null;
  renderDetail();
  if (draft && $("#compose-text")) $("#compose-text").value = draft;
  if (selectedModel && $("#compose-model"))
    $("#compose-model").value = selectedModel;
  document
    .querySelectorAll(".conversation-item")
    .forEach((b) => b.classList.toggle("selected", b.dataset.id === id));
  if (result.runs.some((r) => ["queued", "running"].includes(r.status))) {
    state.timer = setTimeout(async () => {
      try {
        await selectConversation(id, true);
        await loadList();
      } catch (error) {
        notify(error.message, true);
      }
    }, 1500);
  }
}

function messageHTML(message, runs) {
  const run = runs.find(
    (r) => r.reply_id === message.id || r.trigger_id === message.id,
  );
  return `<article class="message ${message.role === "assistant" ? "assistant" : ""}"><div class="message-label"><strong>${escapeHTML(message.sender)}</strong><time>${formatTime(message.created_at)}</time>${message.mentioned ? '<span class="small muted">@机器人</span>' : ""}</div><p class="message-text">${escapeHTML(message.content)}</p><div class="message-meta">${message.role === "assistant" ? `<span>${escapeHTML(run?.model || "")}</span><span>${{ sent: "已发送到 QQ", local: "后台测试", pending: "待发送", failed: "发送失败" }[message.delivery] || ""}</span>` : ""}${run ? `<button class="text-button" data-action="inspect-run" data-id="${escapeHTML(run.id)}">${message.role === "assistant" ? "查看调用详情 ↗" : statusLabels[run.status] + " · 详情 ↗"}</button>` : ""}</div></article>`;
}

function renderDetail() {
  const { conversation: c, messages, runs, has_more } = state.detail;
  const isConsole = c.source === "console";
  const pending = runs.some((r) => ["queued", "running"].includes(r.status));
  const failures = runs.filter((r) =>
    ["queued", "running", "failed", "delivery_failed"].includes(r.status),
  );
  $("#conversation-detail").innerHTML =
    `<div class="panel-title"><div class="detail-heading"><h3>${escapeHTML(c.title)}</h3><p>${isConsole ? "后台测试 · 不发送到QQ群" : "QQ群 · " + escapeHTML(c.external_id)}</p></div><button class="text-button small" data-action="reset-context" ${pending ? "disabled" : ""}>清空上下文</button></div>
    <div class="message-scroll" id="message-scroll">${has_more ? '<div class="more-messages"><button data-action="older-messages">加载更早的消息</button></div>' : ""}${!messages.length ? '<div class="empty-state"><h3>还没有消息</h3><p>发送一句话，看看当前人格会如何回应。</p></div>' : messages.map((m) => (m.id > c.context_after && messages.filter((x) => x.id > c.context_after)[0]?.id === m.id && c.context_after ? '<div class="history-mark">新上下文从这里开始 · 更早记录仍保留</div>' : "") + messageHTML(m, runs)).join("")}
    ${failures
      .slice(0, 5)
      .reverse()
      .map(
        (r) =>
          `<div class="run-state">${statusLabels[r.status]}${r.error ? "：" + escapeHTML(r.error) : "…"}<br><button class="text-button" data-action="inspect-run" data-id="${escapeHTML(r.id)}">查看调用记录 ↗</button></div>`,
      )
      .join("")}</div>
    ${
      isConsole
        ? `<form id="compose-form" class="compose"><textarea id="compose-text" aria-label="测试消息" placeholder="试着与机器人聊一聊…" maxlength="12000" required></textarea><div class="compose-controls"><select id="compose-model" aria-label="本次使用的模型" required>${state.settings.default_model ? "" : '<option value="" selected disabled>请选择模型</option>'}${state.settings.models
            .filter((m) => m.enabled)
            .map(
              (m) =>
                `<option value="${escapeHTML(m.id)}" ${m.id === state.settings.default_model ? "selected" : ""}>${escapeHTML(m.id)}</option>`,
            )
            .join(
              "",
            )}</select><button type="submit" class="primary" ${pending || !state.status.openai_configured || !state.settings.models.some((m) => m.enabled) ? "disabled" : ""}>${pending ? "生成中…" : "发送消息 ↑"}</button></div></form>`
        : '<div class="read-only">此处展示机器人实际收到的消息。普通群消息仅作为上下文，被 @ 时才生成回复。</div>'
    }`;
  const scroll = $("#message-scroll");
  scroll.scrollTop = scroll.scrollHeight;
}

function modal(title, content) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = content;
  if (!$("#modal").open) $("#modal").showModal();
}

async function inspectRun(id) {
  const run = await api(`/runs/${encodeURIComponent(id)}`);
  const facts = [
    ["实际模型", run.model],
    ["状态", statusLabels[run.status]],
    ["人格配置版本", `v${run.settings.revision}`],
    ["创建时间", formatTime(run.created_at)],
    [
      "输入 / 输出 Token",
      `${run.input_tokens ?? "—"} / ${run.output_tokens ?? "—"}`,
    ],
    [
      "生成耗时",
      run.latency_ms == null ? "—" : `${(run.latency_ms / 1000).toFixed(2)} 秒`,
    ],
  ];
  modal(
    "调用详情",
    `<div class="run-details"><div class="facts">${facts.map(([k, v]) => `<div><span>${k}</span>${escapeHTML(v)}</div>`).join("")}</div>${run.error ? `<p class="error">${escapeHTML(run.error)}</p>` : ""}<section><h3>当时的人格提示词</h3><pre>${escapeHTML(run.settings.persona)}</pre></section><section><h3>实际提供的会话上下文 · ${run.context.length} 条</h3><p class="small muted">${run.context.length ? "以下为本次调用选取的消息，按模型接收顺序展示。" : "调用尚未开始，或在构造上下文前失败。"}</p><pre>${escapeHTML(
      JSON.stringify(
        run.context.map((m) => ({
          role: m.role,
          sender: m.sender,
          sender_id: m.sender_id,
          time: m.created_at,
          content: m.content,
          mention_targets: m.mention_targets || [],
        })),
        null,
        2,
      ),
    )}</pre></section><p class="small muted">调用 ID：${escapeHTML(run.id)}<br>OpenAI Response ID：${escapeHTML(run.response_id || "—")}</p></div>`,
  );
}

function connectionHelp() {
  const onebot = state.status.qq_transport === "onebot";
  const qqHelp = onebot
    ? `<p>在 <a class="link" href="http://127.0.0.1:6099/webui" target="_blank" rel="noreferrer">NapCat 面板 ↗</a> 扫码登录普通 QQ 号，再把该账号加入目标群。群里 @ 此账号时才会回复；普通文字会作为该群的上下文。</p><p>NapCat API 地址与密钥使用 ONEBOT_API_URL / ONEBOT_API_TOKEN；事件上报使用 ONEBOT_EVENT_SECRET 签名。上报地址为 <code>http://host.docker.internal:8765/webhooks/onebot</code>，发送格式为 array。</p><p class="small muted">本地接入不需要 QQ 开放平台 AppID，也不需要公网回调。登录信息保存在 NapCat 的数据目录。</p>`
    : `<p>QQ 官方机器人使用 QQ_APP_ID、QQ_APP_SECRET 及公网 HTTPS 回调 <code>/webhooks/qq</code>。普通 QQ 号请设置 QQ_TRANSPORT=onebot，使用 NapCat。</p>`;
  modal(
    "接入机器人",
    `<p>模型服务地址：<code>${escapeHTML(state.status.model_endpoint)}</code></p><p>在项目 .env 中设置 OPENAI_BASE_URL 和 OPENAI_API_KEY 后重启服务。使用 New API 时，Key 应为它签发的调用令牌。</p>${qqHelp}<p class="small muted">群聊上下文会发送至配置的模型服务，历史保存在本机数据库。密钥不应出现在群消息或人格提示词中。完整配置见项目 README。</p>`,
  );
}

const actions = {
  "close-modal": () => $("#modal").close(),
  "connection-help": connectionHelp,
  refresh: route,
  logout: async () => {
    await api("/auth/logout", { method: "POST", body: "{}" });
    state.csrf = null;
    state.initialized = true;
    state.selected = null;
    state.detail = null;
    showAuth();
  },
  "new-conversation": () =>
    modal(
      "新建测试会话",
      '<form id="new-conversation-form" class="modal-form"><label for="conversation-title">会话名称</label><input id="conversation-title" placeholder="例如：测试新的说话风格" maxlength="120" required><p class="small muted">测试会话使用真实模型调用并消耗 API 额度，消息不会发送到 QQ。</p><div class="modal-actions"><button type="button" data-action="close-modal">取消</button><button type="submit" class="primary">创建会话</button></div></form>',
    ),
  "select-conversation": (button) => selectConversation(button.dataset.id),
  "refresh-conversations": async () => {
    state.status = await api("/status");
    renderConnections();
    await loadList();
    if (state.selected) await selectConversation(state.selected);
  },
  "prev-page": async () => {
    state.offset = Math.max(0, state.offset - 20);
    await loadList();
  },
  "next-page": async () => {
    state.offset += 20;
    await loadList();
  },
  "save-models": () => saveSettings(state.settings),
  "sync-models": async () => {
    if (state.dirty) throw new Error("请先保存当前模型选择，再刷新上游列表。");
    await syncModels();
    renderModels();
    if (!state.modelSyncError) notify(`已同步 ${state.settings.models.length} 个上游模型`);
  },
  "inspect-run": (button) => inspectRun(button.dataset.id),
  "reset-context": () =>
    modal(
      "清空当前上下文？",
      '<p>下一次回答将从新的上下文开始，已保存的会话历史和调用记录仍可查看。</p><div class="modal-actions"><button data-action="close-modal">取消</button><button class="primary" data-action="confirm-reset">清空上下文</button></div>',
    ),
  "confirm-reset": async () => {
    await api(`/conversations/${state.selected}/reset-context`, {
      method: "POST",
      body: "{}",
    });
    $("#modal").close();
    await selectConversation(state.selected);
    notify("上下文已清空，历史记录仍保留");
  },
  "older-messages": async () => {
    const id = state.selected;
    const detail = await api(
      `/conversations/${id}?before=${state.detail.messages[0].id}`,
    );
    if (state.selected !== id) return;
    state.detail.messages = [...detail.messages, ...state.detail.messages];
    state.detail.runs = [
      ...new Map(
        [...state.detail.runs, ...detail.runs].map((run) => [run.id, run]),
      ).values(),
    ];
    state.detail.has_more = detail.has_more;
    const scroll = $("#message-scroll"),
      height = scroll.scrollHeight;
    renderDetail();
    $("#message-scroll").scrollTop = $("#message-scroll").scrollHeight - height;
  },
};

document.addEventListener("click", async (event) => {
  const link = event.target.closest("[data-nav]");
  if (link && state.dirty && !confirm("当前修改尚未保存，确定离开吗？")) {
    event.preventDefault();
    return;
  }
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = actions[button.dataset.action];
  if (!action) return;
  button.disabled = true;
  try {
    await action(button);
  } catch (error) {
    notify(error.message, true);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
});

document.addEventListener("input", (event) => {
  if (event.target.closest("#persona-form")) {
    markDirty();
    $("#char-count").textContent =
      `${$("#persona").value.length.toLocaleString()} / 16,000`;
  }
});

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-enable-model]")) {
    const rule =
      state.settings.models[Number(event.target.dataset.enableModel)];
    if (rule.id === state.settings.default_model && !event.target.checked) {
      event.target.checked = true;
      notify("请先将默认模型切换到其他已启用模型", true);
      return;
    }
    rule.enabled = event.target.checked;
    renderModelRows();
    markDirty();
  }
  if (event.target.matches("[data-default-model]")) {
    state.settings.default_model =
      state.settings.models[Number(event.target.dataset.defaultModel)].id;
    renderModelRows();
    markDirty();
  }
});

document.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target,
    button = form.querySelector('[type="submit"]');
  if (button) button.disabled = true;
  try {
    if (form.id === "auth-form") {
      $("#auth-error").textContent = "";
      const password = $("#password").value;
      if (!state.initialized && password !== $("#confirm-password").value)
        throw new Error("两次输入的密码不一致");
      const result = await api(
        state.initialized ? "/auth/login" : "/auth/setup",
        { method: "POST", body: JSON.stringify({ password }) },
      );
      state.csrf = result.csrf;
      state.initialized = true;
      form.reset();
      await enter();
    } else if (form.id === "persona-form") {
      await saveSettings({
        ...state.settings,
        persona: $("#persona").value,
        context_messages: Number($("#context-messages").value),
        max_output_tokens: Number($("#max-output").value),
      });
    } else if (form.id === "new-conversation-form") {
      const result = await api("/conversations", {
        method: "POST",
        body: JSON.stringify({ title: $("#conversation-title").value }),
      });
      $("#modal").close();
      state.selected = result.id;
      state.offset = 0;
      state.query = "";
      state.source = "";
      if (location.hash !== "#conversations") location.hash = "conversations";
      else await renderConversations();
    } else if (form.id === "search-form") {
      state.query = $("#search").value;
      state.source = $("#source-filter").value;
      state.offset = 0;
      await loadList();
    } else if (form.id === "compose-form") {
      await api(`/conversations/${state.selected}/messages`, {
        method: "POST",
        body: JSON.stringify({
          content: $("#compose-text").value,
          model: $("#compose-model").value,
        }),
      });
      $("#compose-text").value = "";
      await selectConversation(state.selected);
      await loadList();
    }
  } catch (error) {
    if (form.id === "auth-form") $("#auth-error").textContent = error.message;
    else notify(error.message, true);
  } finally {
    if (button?.isConnected) button.disabled = false;
  }
});

window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (event) => {
  if (state.dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});
boot();
