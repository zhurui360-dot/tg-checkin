(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    data: null,
    tab: "dashboard",
    editingGroupId: null,
    editingAccountId: null,
    editingBotId: null,
    loginAccountId: null,
    accounts: [],
    bots: [],
    botDetail: {},
    agents: [],
    editingAgentId: null,
    settingsFilled: false,
    backupFilled: false,
    modalHandler: null,
  };

  const TITLES = {
    dashboard: ["概览", "自动签到任务运行状态一览"],
    groups: ["群组管理", "配置需要签到的群组、指令与执行时间"],
    bot: ["AI 机器人", "接入 AI 陪聊机器人，私聊与群内被 @ 时自动回复"],
    account: ["账号与登录", "管理多个 Telegram 账号，分别扫码或验证码登录"],
    settings: ["系统设置", "重试、通知、补签等全局参数"],
    logs: ["运行日志", "查看每一次签到执行的结果与机器人回复"],
  };

  const TRIGGER_TEXT = {
    mention: "私聊 + 群里被 @",
    all: "群内所有消息",
    private: "只回私聊",
  };

  const SOURCE_TEXT = {
    manual: "手动",
    auto: "定时",
    scheduled: "定时",
    catchup: "补签",
  };

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  let toastTimer = null;
  function toast(message, kind = "", duration = 0) {
    const el = $("toast");
    el.textContent = message;
    el.className = "toast " + kind;
    clearTimeout(toastTimer);
    // 报错信息通常更长（比如 GitHub 的诊断），多留点时间让人看完
    toastTimer = setTimeout(() => el.classList.add("hidden"), duration || (kind === "err" ? 7000 : 4200));
  }

  async function api(path, options = {}) {
    const opts = { method: options.method || "GET", headers: {}, ...options };
    if (options.body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(options.body);
    }
    let response;
    try {
      response = await fetch(path, opts);
    } catch (err) {
      throw new Error("无法连接到服务，请检查容器是否运行");
    }
    if (response.status === 401) {
      $("loginScreen").classList.remove("hidden");
      throw new Error("请先登录管理面板");
    }
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* ignore */ }
    if (!response.ok) {
      throw new Error(payload.detail || `请求失败（${response.status}）`);
    }
    return payload;
  }

  function switchTab(tab) {
    if (!TITLES[tab]) return;
    state.tab = tab;
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === "tab-" + tab);
    });
    $("pageTitle").textContent = TITLES[tab][0];
    $("pageDesc").textContent = TITLES[tab][1];
    if (tab === "bot") {
      loadBotConfig();
      loadBotMessages();
      refreshBots();
    }
    if (tab === "account") refreshAccounts();
    if (tab === "settings") {
      loadBackupPanel();
      loadWebdavFiles(true);
      loadGithubFiles(true);
    }
  }

  // ---------- 渲染 ----------

  function statusBadge(status) {
    return status === "success"
      ? `<span class="badge ok">成功</span>`
      : `<span class="badge err">失败</span>`;
  }

  function renderDashboard(data) {
    const s = data.stats;
    $("statEnabled").textContent = s.enabled;
    $("statGroups").textContent = `共 ${s.groups} 个群组`;
    $("statSuccess").textContent = s.today_success;
    $("statFailed").textContent = s.today_failed;
    $("statTotal").textContent = s.total_runs;
    const cu = data.catchup || {};
    const cuText = cu.enabled === false
      ? "补签已关闭"
      : cu.running ? "补签运行中" : "补签未运行";
    $("statJobs").textContent = `调度任务 ${data.scheduler_jobs} 个 · ${cuText}`;

    const logs = data.logs.slice(0, 6);
    $("recentLogs").innerHTML = logs.length
      ? `<table><thead><tr><th>时间</th><th>群组</th><th>状态</th><th>备注</th></tr></thead><tbody>${logs.map((log) => `
          <tr>
            <td class="mono">${esc(log.ts)}</td>
            <td>${esc(log.group_name || "-")}</td>
            <td>${statusBadge(log.status)}</td>
            <td class="note">${esc(log.message || "-")}</td>
          </tr>`).join("")}</tbody></table>`
      : `<div class="empty">暂无运行记录</div>`;

    const accounts = data.accounts || [];
    const authed = accounts.filter((a) => a.authorized).length;
    const badge = $("accountBadge");
    if (!accounts.length) {
      badge.textContent = "还没有 Telegram 账号";
      badge.className = "badge warn";
    } else {
      badge.textContent = `${accounts.length} 个账号 · ${authed} 个已登录`;
      badge.className = "badge " + (authed ? "ok" : "warn");
    }
    $("versionText").textContent = "v" + data.version;
  }

  function renderGroups(data) {
    const tbody = $("groupTableBody");
    const groups = data.groups;
    $("groupCount").textContent = `${groups.length} 个`;
    if (!groups.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty">还没有配置群组，请在上方添加</td></tr>`;
      return;
    }
    const globalMode = (data.settings && data.settings.checkin_mode) === "user" ? "我的账号" : "机器人";
    tbody.innerHTML = groups.map((g) => {
      let expressions = [];
      try { expressions = JSON.parse(g.schedule || "[]"); } catch (_) { expressions = [g.schedule]; }
      const runs = data.next_runs[String(g.id)] || [];
      const runInfo = runs.length
        ? runs.map((r) => `<div class="next-line">下次：${esc(r.next)}</div>`).join("")
        : `<div class="next-line">未启用</div>`;

      const acc = state.accounts.find((a) => a.id === g.account_id);
      const bt = state.bots.find((b) => b.id === g.bot_id);
      const modeText = g.mode === "user" ? "用账号" : g.mode === "bot" ? "用机器人" : `跟随全局（${globalMode}）`;
      const who = [];
      if (acc) who.push(esc(acc.name || acc.phone || `#${acc.id}`));
      if (bt) who.push("@" + esc(bt.username || bt.name || bt.id));
      const executor = who.length ? `${modeText}<div class="next-line">${who.join(" · ")}</div>` : modeText;

      return `<tr>
        <td><strong>${esc(g.name)}</strong></td>
        <td class="mono">${esc(g.chat)}</td>
        <td class="mono">${esc(g.command)}</td>
        <td>${executor}</td>
        <td>${expressions.map((e) => `<div class="cron-line">${esc(e)}</div>`).join("")}${runInfo}</td>
        <td>${g.enabled ? `<span class="badge ok">启用</span>` : `<span class="badge">暂停</span>`}</td>
        <td><div class="row-actions">
          <button class="btn small" data-action="test" data-id="${g.id}">测试</button>
          <button class="btn small" data-action="edit" data-id="${g.id}">编辑</button>
          <button class="btn small danger" data-action="delete" data-id="${g.id}">删除</button>
        </div></td>
      </tr>`;
    }).join("");
  }

  function renderLogs(data) {
    const tbody = $("logTableBody");
    const logs = data.logs;
    if (!logs.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty">暂无日志</td></tr>`;
      return;
    }
    tbody.innerHTML = logs.map((log) => `<tr>
      <td class="mono">${esc(log.ts)}</td>
      <td>${esc(log.group_name || "-")}</td>
      <td>${esc(SOURCE_TEXT[log.source] || log.source || "-")}</td>
      <td>${statusBadge(log.status)}</td>
      <td class="note">${esc(log.message || "-")}</td>
      <td>${log.duration} ms</td>
    </tr>`).join("");
  }

  // ---------- 账号 ----------

  function renderGroupSelects() {
    const accSel = $("groupAccountSelect");
    if (accSel) {
      const current = accSel.value;
      accSel.innerHTML = `<option value="">默认账号</option>` + state.accounts.map((a) =>
        `<option value="${a.id}">${esc(a.name || a.phone || "账号" + a.id)}${a.authorized ? "" : "（未登录）"}</option>`
      ).join("");
      accSel.value = current;
    }
    const botSel = $("groupBotSelect");
    if (botSel) {
      const current = botSel.value;
      botSel.innerHTML = `<option value="">默认机器人</option>` + state.bots.map((b) =>
        `<option value="${b.id}">${esc(b.name || b.username || "机器人" + b.id)}${b.username ? "（@" + esc(b.username) + "）" : ""}</option>`
      ).join("");
      botSel.value = current;
    }
  }

  function renderAccounts() {
    const tbody = $("accountTableBody");
    if (!tbody) return;
    const accounts = state.accounts;
    if ($("accountCount")) $("accountCount").textContent = `${accounts.length} 个`;
    if (!accounts.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty">还没有账号，用下面的表单添加一个，然后扫码登录</td></tr>`;
      renderAccountInfo();
      return;
    }
    tbody.innerHTML = accounts.map((a) => {
      const logged = a.authorized
        ? `<span class="badge ok">已登录</span>`
        : a.api_id ? `<span class="badge warn">未登录</span>` : `<span class="badge err">缺 API 凭据</span>`;
      const current = a.id === state.loginAccountId ? `<span class="badge">当前</span>` : "";
      return `<tr>
        <td><strong>${esc(a.name || "-")}</strong>${a.default ? ` <span class="badge">默认</span>` : ""}${current}</td>
        <td class="mono">${esc(a.phone || "-")}</td>
        <td class="mono">${esc(a.api_id || "-")}</td>
        <td>${logged}</td>
        <td>${a.enabled ? `<span class="badge ok">启用</span>` : `<span class="badge">停用</span>`}</td>
        <td><div class="row-actions">
          <button class="btn small" data-action="login" data-id="${a.id}">登录</button>
          <button class="btn small" data-action="verify" data-id="${a.id}">验证</button>
          <button class="btn small" data-action="edit" data-id="${a.id}">编辑</button>
          <button class="btn small danger" data-action="delete" data-id="${a.id}">删除</button>
        </div></td>
      </tr>`;
    }).join("");
    renderAccountInfo();
  }

  function currentAccount() {
    return state.accounts.find((a) => a.id === state.loginAccountId) || null;
  }

  function renderAccountInfo() {
    const info = $("accountInfo");
    if (!info) return;
    const acc = currentAccount();
    if (!acc) {
      info.textContent = state.accounts.length
        ? "先在上面的列表里点某个账号的「登录」，再扫码或收验证码。"
        : "还没有账号，先添加一个账号并填好 API ID / API Hash。";
      const st = $("accountState");
      if (st) { st.textContent = "未选择"; st.className = "badge"; }
      return;
    }
    info.innerHTML = `当前操作账号：<strong>${esc(acc.name || acc.phone || "账号" + acc.id)}</strong>` +
      (acc.authorized ? "（已登录）" : "（未登录）");
  }

  function applyAccountToForm(account) {
    const form = $("accountForm");
    if (!form) return;
    form.elements.name.value = account ? (account.name || "") : "";
    form.elements.api_id.value = account ? (account.api_id || "") : "";
    form.elements.proxy_url.value = account ? (account.proxy_url || "") : "";
    form.elements.phone.value = "";
    form.elements.phone.placeholder = account
      ? `当前 ${account.phone || "未填写"}，留空不修改`
      : "+8613800138000";
    form.elements.api_hash.value = "";
    form.elements.api_hash.placeholder = account
      ? "已配置，留空表示不修改"
      : "32 位字符串";
  }

  async function refreshAccounts() {
    try {
      const data = await api("/api/accounts");
      state.accounts = data.accounts || [];
      if (state.loginAccountId && !state.accounts.some((a) => a.id === state.loginAccountId)) {
        state.loginAccountId = null;
      }
      renderAccounts();
      renderGroupSelects();
    } catch (err) {
      if (!String(err.message).includes("登录")) toast(err.message, "err");
    }
  }

  async function checkAccountStatus() {
    const acc = currentAccount();
    const st = $("accountState");
    try {
      const status = await api("/api/tg/status" + (acc ? `?account_id=${acc.id}` : ""));
      if (st) {
        st.textContent = status.authorized ? "已登录" : "未登录";
        st.className = "badge " + (status.authorized ? "ok" : "warn");
      }
      const info = $("accountInfo");
      if (info) {
        const who = status.name || status.phone || "Telegram 用户";
        info.innerHTML = status.authorized
          ? `当前账号：<strong>${esc(who)}</strong>` + (status.user_id ? `（ID ${esc(status.user_id)}）` : "")
          : esc(status.error || "这个账号还没有登录会话，请用扫码或验证码登录");
      }
      await refreshAccounts();
    } catch (err) { toast(err.message, "err"); }
  }

  // ---------- 机器人 ----------

  function renderBots() {
    const tbody = $("botTableBody");
    const bots = state.bots;
    if (!tbody) return;
    if (!bots.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty">还没有机器人，用下面的表单从 @BotFather 拿个 token 加进来</td></tr>`;
      renderBotSummary();
      return;
    }
    tbody.innerHTML = bots.map((b) => {
      const detail = state.botDetail[b.id] || b.status || {};
      const running = detail.running !== undefined ? detail.running : b.running;
      const poll = running
        ? `<span class="badge ok">运行中</span>`
        : b.token_set === false || b.token_set === 0
          ? `<span class="badge err">无 Token</span>`
          : `<span class="badge">已停止</span>`;
      const bound = b.ai_agent_id ? agentById(b.ai_agent_id) : null;
      const agentCell = bound
        ? esc(bound.name)
        : (() => {
            const def = defaultAgent();
            return def ? `<span class="muted">${esc(def.name)}（默认）</span>` : `<span class="muted">未配置</span>`;
          })();
      return `<tr>
        <td><strong>${esc(b.name || "-")}</strong>${b.default ? ` <span class="badge">默认</span>` : ""}</td>
        <td class="mono">${b.username ? "@" + esc(b.username) : "-"}</td>
        <td>${agentCell}</td>
        <td>${esc(TRIGGER_TEXT[b.trigger] || b.trigger || "-")}</td>
        <td>${poll}</td>
        <td>${detail.replies != null ? detail.replies : (b.replies || 0)}</td>
        <td>${b.enabled ? `<span class="badge ok">启用</span>` : `<span class="badge">关闭</span>`}</td>
        <td><div class="row-actions">
          <button class="btn small" data-action="verify" data-id="${b.id}">验证</button>
          <button class="btn small" data-action="restart" data-id="${b.id}">重启</button>
          <button class="btn small" data-action="edit" data-id="${b.id}">编辑</button>
          <button class="btn small danger" data-action="delete" data-id="${b.id}">删除</button>
        </div></td>
      </tr>`;
    }).join("");
    renderBotSummary();
  }

  function renderBotSummary() {
    const box = $("botStatusText");
    if (!box) return;
    const bots = state.bots;
    if (!bots.length) {
      box.textContent = "还没配置 Bot Token。按下面步骤从 @BotFather 拿一个 token 填进来即可，不需要 api_id。";
      return;
    }
    const running = bots.filter((b) => (state.botDetail[b.id] || b.status || {}).running || b.running).length;
    const totalReplies = bots.reduce((sum, b) => {
      const d = state.botDetail[b.id] || b.status || {};
      return sum + Number(d.replies != null ? d.replies : (b.replies || 0));
    }, 0);
    const errs = bots
      .map((b) => {
        const d = state.botDetail[b.id] || b.status || {};
        return d.last_error ? `${b.name || b.username}：${d.last_error}` : "";
      })
      .filter(Boolean);
    box.textContent = `共 ${bots.length} 个机器人，${running} 个轮询运行中，累计回复 ${totalReplies} 条。` +
      (errs.length ? ` 最近错误：${errs.slice(0, 2).join("；")}` : "");
  }

  async function refreshBots() {
    try {
      const data = await api("/api/bots");
      state.bots = data.bots || [];
      state.botDetail = {};
      state.bots.forEach((b) => { if (b.status) state.botDetail[b.id] = b.status; });
      renderBots();
      renderGroupSelects();
    } catch (err) {
      if (!String(err.message).includes("登录")) toast(err.message, "err");
    }
  }

  function applyBotToForm(botInst) {
    const form = $("botForm");
    if (!form) return;
    form.elements.name.value = botInst ? (botInst.name || "") : "";
    form.elements.bot_token.value = "";
    form.elements.bot_token.placeholder = botInst
      ? "已配置，留空表示不修改"
      : "123456789:AAH...（在 @BotFather 获取）";
    if (form.elements.bot_trigger) {
      form.elements.bot_trigger.value = (botInst && botInst.trigger) || "mention";
    }
    if (form.elements.bot_enabled) {
      form.elements.bot_enabled.value = botInst ? (botInst.enabled ? "1" : "0") : "1";
    }
    fillAgentSelect();
    if (form.elements.bot_agent_id) {
      form.elements.bot_agent_id.value = botInst && botInst.ai_agent_id ? String(botInst.ai_agent_id) : "";
    }
  }

  // ---------- AI 智能体 ----------

  function agentById(id) {
    return state.agents.find((a) => String(a.id) === String(id));
  }

  function defaultAgent() {
    return state.agents.find((a) => a.is_default) || state.agents[0];
  }

  function renderAgents() {
    const tbody = $("agentTableBody");
    if (!tbody) return;
    if (!state.agents.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="empty">还没有智能体，点右上角「新增智能体」建一个，配上模型和 Key 就能用</td></tr>`;
    } else {
      tbody.innerHTML = state.agents.map((a) => {
        const isDefault = a.is_default;
        const status = a.enabled
          ? a.key_set ? `<span class="badge ok">可用</span>` : `<span class="badge err">缺 Key</span>`
          : `<span class="badge">已关闭</span>`;
        return `<tr>
          <td><strong>${esc(a.name || "未命名")}</strong>${isDefault ? ` <span class="badge">默认</span>` : ""}</td>
          <td class="mono">${esc(a.model || "-")}</td>
          <td class="mono" title="${esc(a.base_url || "")}">${esc((a.base_url || "-").replace(/^https?:\/\//, ""))}</td>
          <td>${status}</td>
          <td><div class="row-actions">
            <button class="btn small" data-action="test" data-id="${a.id}">测试</button>
            <button class="btn small" data-action="edit" data-id="${a.id}">编辑</button>
            ${isDefault ? "" : `<button class="btn small" data-action="default" data-id="${a.id}">设为默认</button>`}
            <button class="btn small danger" data-action="delete" data-id="${a.id}">删除</button>
          </div></td>
        </tr>`;
      }).join("");
    }
    fillAgentSelect();
    renderBots();
  }

  function fillAgentSelect() {
    const select = $("botAgentSelect");
    if (!select) return;
    const current = select.value;
    const def = defaultAgent();
    select.innerHTML = `<option value="">跟随默认${def ? `（${esc(def.name)}）` : ""}</option>` +
      state.agents.map((a) => `<option value="${a.id}">${esc(a.name)}${a.model ? ` · ${esc(a.model)}` : ""}</option>`).join("");
    if (current) select.value = current;
  }

  function applyAgentToForm(agent) {
    const form = $("agentForm");
    if (!form) return;
    state.editingAgentId = agent ? agent.id : null;
    form.elements.name.value = agent ? (agent.name || "") : "";
    form.elements.model.value = agent ? (agent.model || "") : "";
    form.elements.base_url.value = agent ? (agent.base_url || "") : "";
    form.elements.system_prompt.value = agent ? (agent.system_prompt || "") : "";
    form.elements.history_len.value = agent ? (agent.history_len ?? "") : "";
    form.elements.max_tokens.value = agent ? (agent.max_tokens ?? "") : "";
    form.elements.temperature.value = agent ? (agent.temperature ?? "") : "";
    form.elements.min_interval.value = agent ? (agent.min_interval ?? "") : "";
    form.elements.proxy.value = agent ? (agent.proxy || "") : "";
    form.elements.enabled.value = agent ? (agent.enabled ? "1" : "0") : "1";
    form.elements.api_key.value = "";
    form.elements.api_key.placeholder = agent && agent.key_set
      ? "已配置，留空表示不修改"
      : "未配置，请填写 API Key";
    $("agentFormTitle").textContent = agent ? `编辑智能体：${agent.name}` : "新增智能体";
    $("agentFormHint").textContent = agent
      ? "修改后点保存，正在用它的机器人下一条消息就生效"
      : "取个名字，配上模型和 Key 就能用";
    $("agentSaveBtn").textContent = agent ? "保存修改" : "创建智能体";
    $("agentCancelBtn").classList.toggle("hidden", !agent);
  }

  async function refreshAgents() {
    try {
      const data = await api("/api/ai/agents");
      state.agents = data.agents || [];
      renderAgents();
    } catch (err) {
      if (!String(err.message).includes("登录")) toast(err.message, "err");
    }
  }

  // ---------- 设置 ----------

  function fillSettings(data) {
    if (state.settingsFilled) return;
    const form = $("settingsForm");
    const s = data.settings;
    ["timezone", "retry_count", "retry_delay", "jitter", "notify_chat", "fail_keywords",
      "dry_run", "checkin_mode", "keepalive_enabled", "keepalive_interval", "keepalive_url",
      "catchup_enabled", "catchup_interval", "catchup_cooldown"].forEach((key) => {
      if (form.elements[key]) form.elements[key].value = s[key] ?? "";
    });
    state.settingsFilled = true;
    if (s.using_default_password) {
      toast("当前面板密码为默认值 admin123，建议在「系统设置」中修改", "err");
    }
  }

  function renderCatchup(info) {
    const box = $("catchupText");
    if (!box) return;
    if (!info) { box.textContent = "加载中…"; return; }
    if (!info.enabled) {
      box.textContent = "已关闭。开启后，定时点没在线（容器休眠/重启）时，上线会立刻补一次，保证每天至少签到成功一次。";
      return;
    }
    box.textContent = `${info.running ? "检查线程运行中" : "检查线程未运行"} · 每 ${info.interval} 秒核对一次 · 同一群失败后冷却 ${info.cooldown} 秒。` +
      "判定条件：群组已启用 + 今天有计划时间已过 + 今天还没有成功记录。补签日志来源会标记为「补签」。";
  }

  function renderKeepalive(info) {
    const box = $("keepaliveText");
    if (!box) return;
    if (!info || !info.url) {
      box.textContent = info && info.enabled
        ? "已开启，但还没探测到公网地址。请先用公网域名访问一次面板，或手动填写保活目标地址。"
        : "未启用。若部署在会休眠的平台上，建议开启以避免调度器停止。";
      return;
    }
    const stateText = info.enabled ? "已开启" : "已关闭";
    const lastText = info.last_ping
      ? `${info.last_ping} ${info.last_ok ? "成功" : "失败"}` + (info.last_status ? `（${info.last_status}）` : "")
      : "尚未执行";
    box.textContent = `${stateText} · 间隔 ${info.interval} 秒 · 目标 ${info.url} · 上次 ${lastText} · 成功 ${info.ok_count} 次` +
      (info.fail_streak ? ` · 连续失败 ${info.fail_streak} 次` : "") +
      "。注意：实例被平台停机后本进程的线程会一起停止，无法唤醒自己，" +
      "真正防休眠请配置仓库里的外部定时任务（.github/workflows/keepalive.yml）。";
  }

  // ---------- 备份 / 云备份 ----------

  function formatSize(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  }

  function backupNameText(name) {
    // tgck-20261125-034512.json → 2026-11-25 03:45:12
    const m = String(name || "").match(/^tgck-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})\.json$/);
    if (!m) return name;
    return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
  }

  function renderCloudFiles(bodyId, files, kind, emptyText) {
    const body = $(bodyId);
    if (!body) return;
    if (!files || !files.length) {
      body.innerHTML = `<tr><td colspan="2" class="muted" style="text-align:center;padding:20px">${esc(emptyText)}</td></tr>`;
      return;
    }
    body.innerHTML = files.map((item) => `
      <tr>
        <td>${esc(backupNameText(item.name))}${item.size ? ` <span class="muted">${formatSize(item.size)}</span>` : ""}</td>
        <td><div class="row-actions">
          <button class="btn small" data-action="restore" data-kind="${kind}" data-name="${esc(item.name)}">恢复这份</button>
        </div></td>
      </tr>`).join("");
  }

  function renderBackupStatus(info) {
    const summary = $("backupSummary");
    const text = $("backupStatusText");
    if (!summary || !text) return;
    if (!info) { summary.textContent = "加载中…"; return; }
    const auto = info.auto || {};
    const on = auto.enabled ? "自动备份已开启" : "自动备份已关闭";
    summary.textContent = `${on} · 本地 ${(info.local || []).length} 份`;
    const parts = [];
    parts.push(auto.running ? "备份线程运行中" : "备份线程未运行");
    parts.push(`每 ${auto.interval} 小时一次 · 本地保留 ${auto.keep} 份`);
    parts.push(auto.last_run ? `上次 ${auto.last_run}` + (auto.last_target ? `（${auto.last_target}）` : "") : "还没备份过");
    if (auto.last_error) parts.push(`上次出错：${auto.last_error}`);
    parts.push(auto.restore_on_boot ? "容器重建后会自动从云端拉回数据" : "启动时自动恢复已关闭");
    text.textContent = parts.join(" · ") + "。";

    const dav = info.webdav || {};
    $("webdavState").textContent = dav.configured
      ? `${dav.auto ? "已配置 · 自动上传" : "已配置 · 仅手动"}${dav.dir ? " · " + dav.dir : ""}`
      : "未配置";
    $("githubState").textContent = info.github && info.github.configured
      ? (info.github.auto ? "已配置 · 自动同步" : "已配置 · 仅手动")
      : "未配置";
  }

  function renderLocalBackups(files) {
    const body = $("backupLocalBody");
    if (!body) return;
    if (!files || !files.length) {
      body.innerHTML = '<tr><td colspan="4" class="muted" style="text-align:center;padding:20px">还没有本地备份，点「立即备份一次」</td></tr>';
      return;
    }
    body.innerHTML = files.map((item) => `
      <tr>
        <td class="mono">${esc(item.name)}</td>
        <td>${formatSize(item.size)}</td>
        <td class="mono">${esc(item.created_at)}</td>
        <td><div class="row-actions">
          <button class="btn small" data-action="restore" data-name="${esc(item.name)}">恢复</button>
          <button class="btn small danger" data-action="delete" data-name="${esc(item.name)}">删除</button>
        </div></td>
      </tr>`).join("");
  }

  async function loadBackupPanel() {
    try {
      const info = await api("/api/backup/status");
      renderBackupStatus(info);
      renderLocalBackups(info.local);
    } catch (err) {
      const text = $("backupStatusText");
      if (text) text.textContent = err.message;
    }
  }

  const CLOUD_LABEL = { webdav: "WebDAV", github: "GitHub" };

  async function loadCloudFiles(kind, silent = false) {
    const bodyId = kind === "webdav" ? "webdavBody" : "githubBody";
    const empty = kind === "webdav"
      ? "还没连上或云端还没有备份"
      : "还没连上或仓库里还没有备份";
    try {
      const data = await api(`/api/backup/${kind}`);
      renderCloudFiles(bodyId, data.files, kind, empty);
      return data.files || [];
    } catch (err) {
      renderCloudFiles(bodyId, [], kind, err.message);
      if (!silent) toast(err.message, "err");
      return [];
    }
  }

  function loadWebdavFiles(silent) { return loadCloudFiles("webdav", silent); }
  function loadGithubFiles(silent) { return loadCloudFiles("github", silent); }

  function fillBackupForms(data) {
    if (state.backupFilled) return;
    const s = data.settings || {};
    const map = {
      backupForm: ["backup_auto", "backup_interval", "backup_keep", "backup_restore_on_boot"],
      webdavForm: ["webdav_url", "webdav_dir", "webdav_username", "webdav_auto", "webdav_keep"],
      githubForm: ["github_repo", "github_branch", "github_path", "github_auto", "github_keep"],
    };
    Object.entries(map).forEach(([formId, keys]) => {
      const form = $(formId);
      if (!form) return;
      keys.forEach((key) => {
        if (form.elements[key]) form.elements[key].value = s[key] ?? "";
      });
    });
    const ghToken = $("githubForm")?.elements.github_token;
    if (ghToken) {
      ghToken.value = "";
      ghToken.placeholder = s.github_token ? "已配置，留空表示不修改" : "ghp_... 需要 repo 权限";
    }
    const davPass = $("webdavForm")?.elements.webdav_password;
    if (davPass) {
      davPass.value = "";
      davPass.placeholder = s.webdav_password ? "已配置，留空表示不修改" : "应用密码";
    }
    state.backupFilled = true;
  }

  async function saveBackupSettings(formId, keys) {
    const form = $(formId);
    const payload = {};
    keys.forEach((key) => {
      if (form.elements[key]) payload[key] = form.elements[key].value.trim();
    });
    return api("/api/settings", { method: "PUT", body: payload });
  }

  async function restoreFromCloud(kind, name, btn) {
    const label = CLOUD_LABEL[kind] || kind;
    if (!window.confirm(`用 ${label} 上的备份「${backupNameText(name)}」替换当前数据吗？\n当前群组、账号、机器人会被清空，面板密码也会恢复成备份里的。`)) return;
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "恢复中…";
    try {
      const result = await api(`/api/backup/${kind}/restore`, {
        method: "POST", body: { name },
      });
      toast(`已从 ${label} 恢复：${result.groups} 个群组、${result.accounts} 个账号、${result.bots} 个机器人`, "ok");
      state.settingsFilled = false;
      state.backupFilled = false;
      await loadBootstrap();
      await loadBackupPanel();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  async function runBackupNow(btn, label) {
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "备份中…";
    try {
      const result = await api("/api/backup/run", { method: "POST" });
      const targets = [];
      if (result.local) targets.push("本地");
      if (result.webdav) targets.push("WebDAV");
      if (result.github) targets.push("GitHub");
      if (result.errors && result.errors.length) {
        toast(`备份部分失败：${result.errors.join("；")}`, "err");
      } else {
        toast(`已备份到 ${targets.join(" + ") || "（无目标）"}`, "ok");
      }
      await loadBackupPanel();
      await loadWebdavFiles(true);
      await loadGithubFiles(true);
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = label || old;
    }
  }

  function renderAll(data) {
    state.data = data;
    state.accounts = data.accounts || [];
    state.bots = data.bots || [];
    state.agents = data.ai_agents || [];
    if (!state.loginAccountId && state.accounts.length) {
      const first = state.accounts.find((a) => a.authorized) || state.accounts[0];
      state.loginAccountId = first.id;
    }
    renderDashboard(data);
    renderGroups(data);
    renderLogs(data);
    fillSettings(data);
    fillBackupForms(data);
    renderBackupStatus(data.backup);
    renderKeepalive(data.keepalive);
    renderCatchup(data.catchup);
    renderAccounts();
    renderBots();
    renderAgents();
    renderGroupSelects();
    loadKnownChats();
    refreshBots();
    ensureCredentials();
  }

  async function ensureCredentials() {
    // 首次进入账号页时，用默认账号的凭据回填空表单，省得手打
    const form = $("accountForm");
    if (!form || state.editingAccountId || state.accounts.length) return;
    try {
      const credentials = await api("/api/tg/credentials");
      if (!form.elements.api_id.value) form.elements.api_id.value = credentials.api_id || "";
      if (!form.elements.api_hash.value) {
        form.elements.api_hash.value = credentials.api_hash || "";
      }
      if (!form.elements.phone.value) form.elements.phone.value = credentials.phone || "";
      if (!form.elements.proxy_url.value) form.elements.proxy_url.value = credentials.proxy_url || "";
    } catch (err) {
      // 凭据回填失败不影响其他页面使用
    }
  }

  async function loadBotConfig() {
    // 智能体列表从 bootstrap 已经拿到，进页签时再刷一次，保证多开面板时不显示旧数据
    await refreshAgents();
  }

  async function loadBotMessages() {
    try {
      const data = await api("/api/bot/messages?limit=60");
      const body = $("botMsgBody");
      if (!body) return;
      if (!data.messages || !data.messages.length) {
        body.innerHTML = '<tr><td colspan="4" class="muted" style="text-align:center;padding:24px">还没有对话记录</td></tr>';
        return;
      }
      body.innerHTML = data.messages.map((m) => `
        <tr>
          <td class="mono">${esc(m.ts)}</td>
          <td>${esc(m.chat_title || m.chat_id)}</td>
          <td><span class="badge ${m.role === "assistant" ? "ok" : ""}">${m.role === "assistant" ? "机器人" : esc(m.author || "用户")}</span></td>
          <td class="wrap">${esc(m.content)}</td>
        </tr>`).join("");
    } catch (err) {
      toast(err.message, "err");
    }
  }

  async function loadKnownChats() {
    const box = $("knownChatsBox");
    const list = $("knownChatsList");
    if (!box || !list) return;
    try {
      const data = await api("/api/bot/chats");
      const chats = (data.chats || []).slice(0, 12);
      list.innerHTML = "";
      box.hidden = chats.length === 0;
      chats.forEach((chat) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chip";
        btn.textContent = `${chat.title}（${chat.id}）`;
        btn.addEventListener("click", () => {
          const form = $("groupForm");
          if (form) form.elements.chat.value = String(chat.id);
          toast("已填入群 ID，确认后保存", "ok");
        });
        list.appendChild(btn);
      });
    } catch (err) {
      box.hidden = true;
    }
  }

  async function loadBootstrap() {
    try {
      renderAll(await api("/api/bootstrap"));
    } catch (err) {
      if (!String(err.message).includes("登录")) toast(err.message, "err");
    }
  }

  // ---------- 弹窗 ----------

  function openModal({ title, message, placeholder, type = "text", onConfirm }) {
    $("modalTitle").textContent = title;
    $("modalMessage").textContent = message || "";
    $("modalInput").type = type;
    $("modalInput").value = "";
    $("modalInput").placeholder = placeholder || "请输入";
    $("modalError").textContent = "";
    state.modalHandler = onConfirm;
    $("modal").classList.remove("hidden");
    setTimeout(() => $("modalInput").focus(), 50);
  }

  function closeModal() {
    $("modal").classList.add("hidden");
    state.modalHandler = null;
  }

  async function confirmModal() {
    if (!state.modalHandler) return;
    const value = $("modalInput").value.trim();
    if (!value) { $("modalError").textContent = "内容不能为空"; return; }
    const btn = $("modalConfirm");
    btn.disabled = true;
    try {
      await state.modalHandler(value);
    } catch (err) {
      $("modalError").textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  }

  // ---------- 表单重置 ----------

  function resetGroupForm() {
    const form = $("groupForm");
    form.reset();
    form.elements.command.value = "签到";
    form.elements.mode.value = "";
    form.elements.account_id.value = "";
    form.elements.bot_id.value = "";
    state.editingGroupId = null;
    $("groupFormTitle").textContent = "添加群组";
    $("groupSubmitBtn").textContent = "添加群组";
    $("groupCancelBtn").classList.add("hidden");
  }

  function resetAccountForm() {
    state.editingAccountId = null;
    applyAccountToForm(null);
    $("accountFormTitle").textContent = "添加账号";
    $("accountSubmitBtn").textContent = "保存账号";
    $("accountCancelBtn").classList.add("hidden");
    const form = $("accountForm");
    form.elements.name.value = "";
    form.elements.api_id.value = "";
    form.elements.api_hash.value = "";
    form.elements.phone.value = "";
    form.elements.proxy_url.value = "";
    ensureCredentials();
  }

  function resetBotForm() {
    state.editingBotId = null;
    applyBotToForm(null);
    $("botFormTitle").textContent = "添加机器人";
    $("botVerifyBtn").textContent = "校验并添加";
    $("botCancelBtn").classList.add("hidden");
  }

  // ---------- 事件绑定 ----------

  function bindEvents() {
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });
    document.querySelectorAll("[data-goto]").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.goto));
    });

    $("refreshBtn").addEventListener("click", loadBootstrap);
    $("quickRefresh").addEventListener("click", loadBootstrap);
    $("quickAccount").addEventListener("click", () => switchTab("account"));
    $("logoutBtn").addEventListener("click", async () => {
      await api("/api/panel/logout", { method: "POST" });
      $("loginScreen").classList.remove("hidden");
    });

    $("panelLoginBtn").addEventListener("click", async () => {
      const password = $("panelPassword").value;
      try {
        await api("/api/panel/login", { method: "POST", body: { password } });
        $("panelLoginError").textContent = "";
        $("loginScreen").classList.add("hidden");
        loadBootstrap();
      } catch (err) {
        $("panelLoginError").textContent = err.message;
      }
    });
    $("panelPassword").addEventListener("keydown", (e) => {
      if (e.key === "Enter") $("panelLoginBtn").click();
    });

    document.querySelectorAll("[data-cron]").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("groupForm").elements.schedule.value = btn.dataset.cron;
      });
    });

    // ----- 群组 -----

    $("groupForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const payload = {
        name: form.elements.name.value.trim(),
        chat: form.elements.chat.value.trim(),
        command: form.elements.command.value.trim() || "签到",
        schedule: form.elements.schedule.value,
        enabled: form.elements.enabled.value === "1",
        mode: form.elements.mode.value || "",
        account_id: form.elements.account_id.value || null,
        bot_id: form.elements.bot_id.value || null,
      };
      const editing = state.editingGroupId;
      try {
        await api(editing ? `/api/groups/${editing}` : "/api/groups", {
          method: editing ? "PUT" : "POST", body: payload,
        });
        toast(editing ? "群组已更新" : "群组已添加", "ok");
        resetGroupForm();
        await loadBootstrap();
      } catch (err) { toast(err.message, "err"); }
    });

    $("groupCancelBtn").addEventListener("click", resetGroupForm);
    $("groupTableBody").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      const id = Number(btn.dataset.id);
      const group = (state.data?.groups || []).find((g) => g.id === id);
      if (!group) return;

      if (btn.dataset.action === "test") {
        btn.disabled = true;
        try {
          const result = await api(`/api/groups/${id}/test`, { method: "POST" });
          toast(`${result.group_name} 签到成功：${String(result.message || "").slice(0, 80)}`, "ok");
        } catch (err) { toast(`签到失败：${err.message}`, "err"); }
        finally { btn.disabled = false; await loadBootstrap(); }
      } else if (btn.dataset.action === "edit") {
        const form = $("groupForm");
        let expressions = [];
        try { expressions = JSON.parse(group.schedule || "[]"); } catch (_) { expressions = []; }
        form.elements.name.value = group.name;
        form.elements.chat.value = group.chat;
        form.elements.command.value = group.command;
        form.elements.schedule.value = expressions.join("\n");
        form.elements.enabled.value = group.enabled ? "1" : "0";
        form.elements.mode.value = group.mode || "";
        form.elements.account_id.value = group.account_id ? String(group.account_id) : "";
        form.elements.bot_id.value = group.bot_id ? String(group.bot_id) : "";
        state.editingGroupId = id;
        $("groupFormTitle").textContent = "编辑群组";
        $("groupSubmitBtn").textContent = "保存修改";
        $("groupCancelBtn").classList.remove("hidden");
        switchTab("groups");
        window.scrollTo({ top: 0, behavior: "smooth" });
      } else if (btn.dataset.action === "delete") {
        if (!window.confirm(`确定删除群组「${group.name}」吗？`)) return;
        try {
          await api(`/api/groups/${id}`, { method: "DELETE" });
          toast("群组已删除", "ok");
          await loadBootstrap();
        } catch (err) { toast(err.message, "err"); }
      }
    });

    // ----- 账号 -----

    $("accountForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const editing = state.editingAccountId;
      const payload = {};
      ["name", "phone", "api_id", "api_hash", "proxy_url"].forEach((key) => {
        const value = form.elements[key].value.trim();
        if (value) payload[key] = value;
      });
      if (!editing && !payload.name && !payload.phone) {
        toast("请至少填写备注名或手机号", "err");
        return;
      }
      try {
        if (editing) {
          await api(`/api/accounts/${editing}`, { method: "PUT", body: payload });
          toast("账号已更新", "ok");
        } else {
          const result = await api("/api/accounts", { method: "POST", body: payload });
          state.loginAccountId = result.id;
          toast("账号已添加，接着扫码或验证码登录", "ok");
        }
        resetAccountForm();
        await refreshAccounts();
        await loadBootstrap();
      } catch (err) { toast(err.message, "err"); }
    });

    $("accountCancelBtn").addEventListener("click", resetAccountForm);

    $("accountTableBody").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      const id = Number(btn.dataset.id);
      const account = state.accounts.find((a) => a.id === id);
      if (!account) return;

      if (btn.dataset.action === "login") {
        state.loginAccountId = id;
        renderAccounts();
        await checkAccountStatus();
        if (!account.api_id) {
          toast("这个账号还没填 API ID / API Hash，先编辑补上", "err");
        }
      } else if (btn.dataset.action === "verify") {
        btn.disabled = true;
        try {
          const info = await api(`/api/accounts/${id}/verify`, { method: "POST" });
          toast(`${account.name || account.phone} 会话有效：${info.name || info.phone || ""}`, "ok");
        } catch (err) { toast(err.message, "err"); }
        finally { btn.disabled = false; await refreshAccounts(); }
      } else if (btn.dataset.action === "edit") {
        state.editingAccountId = id;
        applyAccountToForm(account);
        $("accountFormTitle").textContent = "编辑账号";
        $("accountSubmitBtn").textContent = "保存修改";
        $("accountCancelBtn").classList.remove("hidden");
        switchTab("account");
        window.scrollTo({ top: 0, behavior: "smooth" });
      } else if (btn.dataset.action === "delete") {
        if (!window.confirm(`确定删除账号「${account.name || account.phone}」吗？\n删除后引用它的群组会改回默认账号。`)) return;
        try {
          await api(`/api/accounts/${id}`, { method: "DELETE" });
          if (state.loginAccountId === id) state.loginAccountId = null;
          toast("账号已删除", "ok");
          await loadBootstrap();
        } catch (err) { toast(err.message, "err"); }
      }
    });

    $("checkAccountBtn").addEventListener("click", async () => {
      if (!currentAccount()) { toast("先在上面的列表里选一个账号", "err"); return; }
      await checkAccountStatus();
    });

    function accountPayload() {
      const form = $("accountForm");
      const acc = currentAccount();
      return {
        account_id: acc ? acc.id : null,
        api_id: form.elements.api_id.value.trim() || (acc ? acc.api_id : ""),
        api_hash: form.elements.api_hash.value.trim(),
        phone: form.elements.phone.value.trim() || (acc ? "" : ""),
        proxy_url: form.elements.proxy_url.value.trim() || (acc ? acc.proxy_url : ""),
      };
    }

    $("loginStartBtn").addEventListener("click", async () => {
      if (!currentAccount()) { toast("先在上面的列表里点某个账号的「登录」", "err"); return; }
      const payload = accountPayload();
      if (!payload.phone) { toast("这个账号还没手机号，先编辑补上", "err"); return; }
      try {
        const result = await api("/api/tg/login-start", { method: "POST", body: payload });
        toast(result.message, "ok");
        verifyCode("");
      } catch (err) { toast(err.message, "err"); }
    });

    // ---------- 扫码登录 ----------
    let qrTimer = null;

    function stopQrPolling() {
      if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
    }

    function renderQr(info) {
      const card = $("qrCard");
      const badge = $("qrBadge");
      const tip = $("qrTip");
      const pwdForm = $("qrPasswordForm");
      if (!card) return;
      card.hidden = false;
      const statusText = {
        starting: "正在申请",
        waiting: "等待扫码",
        need_password: "需要验证密码",
        authorized: "登录成功",
        expired: "已过期",
        error: "出错",
        idle: "已取消",
      };
      badge.textContent = statusText[info.status] || info.status || "等待扫码";
      badge.className = "badge " + (info.status === "authorized" ? "ok" : info.status === "error" ? "err" : "warn");

      if (info.svg) $("qrImage").innerHTML = info.svg;
      else if (!info.svg && info.status === "starting") $("qrImage").innerHTML = '<span class="muted">正在申请二维码…</span>';

      let text = info.message || "";
      if (info.status === "waiting" && info.seconds_left > 0) {
        text = `用手机 Telegram 扫码（剩余 ${info.seconds_left} 秒后自动换新码）。路径：设置 → 设备 → 连接设备。`;
      }
      tip.textContent = text;
      pwdForm.hidden = info.status !== "need_password";

      if (["authorized", "error", "expired", "idle"].includes(info.status)) {
        stopQrPolling();
      }
      if (info.status === "authorized") {
        toast(info.message || "扫码登录成功", "ok");
        $("qrCard").hidden = true;
        if (info.account_id) state.loginAccountId = Number(info.account_id);
        loadBootstrap();
      }
    }

    async function pollQr() {
      try {
        const info = await api("/api/tg/qr/status");
        renderQr(info);
      } catch (err) {
        stopQrPolling();
      }
    }

    function startQrPolling() {
      stopQrPolling();
      qrTimer = setInterval(pollQr, 2000);
    }

    // 页面加载时若已有等待中的二维码，直接展示并开始轮询（避免重新生成冲掉）
    (async function resumeQr() {
      try {
        const info = await api("/api/tg/qr/status");
        if (info && info.active) {
          renderQr(info);
          startQrPolling();
        }
      } catch (err) { /* 忽略 */ }
    })();

    $("qrStartBtn").addEventListener("click", async () => {
      const acc = currentAccount();
      if (!acc) { toast("先在上面的列表里点某个账号的「登录」", "err"); return; }
      const payload = accountPayload();
      if (!payload.api_id || (!payload.api_hash && !acc.api_id)) {
        toast("这个账号还没填 API ID / API Hash，先编辑补上", "err");
        return;
      }
      try {
        const info = await api("/api/tg/qr/start", { method: "POST", body: {
          account_id: acc.id, api_id: payload.api_id, api_hash: payload.api_hash, proxy_url: payload.proxy_url,
        }});
        renderQr(info);
        startQrPolling();
        toast("二维码已生成，用手机 Telegram 扫一下", "ok");
      } catch (err) { toast(err.message, "err"); }
    });

    $("qrRefreshBtn").addEventListener("click", async () => {
      try {
        await api("/api/tg/qr/refresh", { method: "POST" });
        toast("正在刷新二维码", "ok");
      } catch (err) { toast(err.message, "err"); }
    });

    $("qrCancelBtn").addEventListener("click", async () => {
      try {
        await api("/api/tg/qr/cancel", { method: "POST" });
        stopQrPolling();
        $("qrCard").hidden = true;
        toast("已取消扫码登录", "ok");
      } catch (err) { toast(err.message, "err"); }
    });

    $("qrPasswordForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const password = e.currentTarget.elements.password.value;
      try {
        const info = await api("/api/tg/qr/password", { method: "POST", body: { password } });
        renderQr(info);
        startQrPolling();
      } catch (err) { toast(err.message, "err"); }
    });

    $("tgLogoutBtn").addEventListener("click", async () => {
      const acc = currentAccount();
      const label = acc ? (acc.name || acc.phone) : "默认账号";
      if (!window.confirm(`确定清除「${label}」的 Telegram 登录会话吗？`)) return;
      try {
        await api("/api/tg/logout", { method: "POST", body: { account_id: acc ? acc.id : null } });
        toast("已退出该账号", "ok");
        await loadBootstrap();
      } catch (err) { toast(err.message, "err"); }
    });

    // ----- 设置 -----

    $("settingsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const payload = {};
      ["timezone", "retry_count", "retry_delay", "jitter", "notify_chat", "fail_keywords",
        "dry_run", "checkin_mode", "panel_password", "keepalive_enabled", "keepalive_interval",
        "keepalive_url", "catchup_enabled", "catchup_interval", "catchup_cooldown"].forEach((key) => {
        if (form.elements[key]) payload[key] = form.elements[key].value.trim();
      });
      try {
        await api("/api/settings", { method: "PUT", body: payload });
        form.elements.panel_password.value = "";
        toast("设置已保存", "ok");
        await loadBootstrap();
        renderCatchup(await api("/api/catchup"));
      } catch (err) { toast(err.message, "err"); }
    });

    $("catchupRunBtn").addEventListener("click", async () => {
      const btn = $("catchupRunBtn");
      btn.disabled = true;
      btn.textContent = "补签中…";
      try {
        const result = await api("/api/catchup/run", { method: "POST" });
        toast(result.message, result.count ? "ok" : "");
        await loadBootstrap();
      } catch (err) {
        toast(err.message, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = "立即补签一次";
      }
    });

    $("keepalivePingBtn").addEventListener("click", async () => {
      try {
        const info = await api("/api/keepalive/ping", { method: "POST" });
        renderKeepalive(info);
        toast(info.last_ok ? "自检成功，实例处于活跃状态" : `自检失败：${info.last_status}`, info.last_ok ? "ok" : "err");
      } catch (err) { toast(err.message, "err"); }
    });

    $("exportConfigBtn").addEventListener("click", async () => {
      try {
        const config = await api("/api/config/export");
        const blob = new Blob([JSON.stringify(config, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "tg-checkin-config.json";
        link.click();
        URL.revokeObjectURL(url);
        toast("已导出配置文件，请妥善保管：内含 Telegram 登录会话", "ok");
      } catch (err) { toast(err.message, "err"); }
    });

    // ----- 机器人 -----

    $("botVerifyBtn").addEventListener("click", async () => {
      const btn = $("botVerifyBtn");
      const form = $("botForm");
      const token = form.elements.bot_token.value.trim();
      const editing = state.editingBotId;
      if (!editing && !token) { toast("先填 Bot Token", "err"); return; }
      btn.disabled = true;
      btn.textContent = "校验中…";
      try {
        if (editing) {
          const body = {
            name: form.elements.name.value.trim(),
            trigger: form.elements.bot_trigger.value,
            enabled: form.elements.bot_enabled.value === "1",
            ai_agent_id: form.elements.bot_agent_id ? (form.elements.bot_agent_id.value || "") : "",
          };
          if (token) body.token = token;
          await api(`/api/bots/${editing}`, { method: "PUT", body });
          toast("机器人已更新", "ok");
        } else {
          const info = await api("/api/bots", { method: "POST", body: {
            name: form.elements.name.value.trim(),
            token,
            trigger: form.elements.bot_trigger.value,
            enabled: form.elements.bot_enabled.value === "1",
            ai_agent_id: form.elements.bot_agent_id ? (form.elements.bot_agent_id.value || "") : "",
          }});
          toast(`校验通过：${info.name}${info.username ? "（@" + info.username + "）" : ""}`, "ok");
        }
        resetBotForm();
        await loadBootstrap();
        await refreshBots();
      } catch (err) {
        toast(err.message, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = state.editingBotId ? "保存修改" : "校验并添加";
      }
    });

    $("botCancelBtn").addEventListener("click", resetBotForm);

    $("botTableBody").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      const id = Number(btn.dataset.id);
      const botInst = state.bots.find((b) => b.id === id);
      if (!botInst) return;

      if (btn.dataset.action === "verify") {
        btn.disabled = true;
        try {
          const info = await api(`/api/bots/${id}/verify`, { method: "POST" });
          toast(`token 有效：${info.name || ""}${info.username ? "（@" + info.username + "）" : ""}`, "ok");
        } catch (err) { toast(err.message, "err"); }
        finally { btn.disabled = false; await refreshBots(); }
      } else if (btn.dataset.action === "restart") {
        btn.disabled = true;
        try {
          const detail = await api(`/api/bots/${id}/restart`, { method: "POST" });
          state.botDetail[id] = detail;
          renderBots();
          toast(detail.running ? "轮询已重启" : "轮询未运行（检查 token 与启用状态）", detail.running ? "ok" : "");
        } catch (err) { toast(err.message, "err"); }
        finally { btn.disabled = false; }
      } else if (btn.dataset.action === "edit") {
        state.editingBotId = id;
        applyBotToForm(botInst);
        $("botFormTitle").textContent = "编辑机器人";
        $("botVerifyBtn").textContent = "保存修改";
        $("botCancelBtn").classList.remove("hidden");
        switchTab("bot");
        window.scrollTo({ top: 0, behavior: "smooth" });
      } else if (btn.dataset.action === "delete") {
        if (!window.confirm(`确定删除机器人「${botInst.name || botInst.username}」吗？\n删除后引用它的群组会改回默认机器人。`)) return;
        try {
          await api(`/api/bots/${id}`, { method: "DELETE" });
          toast("机器人已删除", "ok");
          await loadBootstrap();
          await refreshBots();
        } catch (err) { toast(err.message, "err"); }
      }
    });

    $("botRestartBtn").addEventListener("click", async () => {
      try {
        await api("/api/bot/restart", { method: "POST" });
        await refreshBots();
        toast("已请求重启全部轮询", "ok");
      } catch (err) { toast(err.message, "err"); }
    });

    $("agentForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const payload = {};
      ["name", "model", "base_url", "api_key", "system_prompt", "history_len",
        "max_tokens", "temperature", "min_interval", "proxy"].forEach((key) => {
        if (form.elements[key]) payload[key] = form.elements[key].value.trim();
      });
      if (form.elements.enabled) payload.enabled = form.elements.enabled.value === "1";
      const btn = $("agentSaveBtn");
      btn.disabled = true;
      try {
        if (state.editingAgentId) {
          await api(`/api/ai/agents/${state.editingAgentId}`, { method: "PUT", body: payload });
          toast("智能体已保存", "ok");
        } else {
          await api("/api/ai/agents", { method: "POST", body: payload });
          toast("智能体已创建", "ok");
        }
        applyAgentToForm(null);
        await refreshAgents();
        await loadBootstrap();
      } catch (err) {
        toast(err.message, "err");
      } finally {
        btn.disabled = false;
      }
    });

    $("agentNewBtn").addEventListener("click", () => {
      applyAgentToForm(null);
      const form = $("agentForm");
      if (form) form.elements.name.focus();
    });

    $("agentCancelBtn").addEventListener("click", () => applyAgentToForm(null));

    $("agentTestBtn").addEventListener("click", async () => {
      const btn = $("agentTestBtn");
      const agent = state.editingAgentId ? agentById(state.editingAgentId) : defaultAgent();
      if (!agent) { toast("先保存一个智能体再测试", "err"); return; }
      btn.disabled = true;
      btn.textContent = "测试中…";
      try {
        const result = await api(`/api/ai/agents/${agent.id}/test`, { method: "POST" });
        toast(`${agent.name} 连通（${result.ms} ms）：${result.reply}`, "ok");
      } catch (err) {
        toast(`${agent.name}：${err.message}`, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = "测试这个智能体";
      }
    });

    $("agentTableBody").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      const id = Number(btn.dataset.id);
      const agent = agentById(id);
      if (!agent) return;
      const action = btn.dataset.action;
      if (action === "edit") {
        applyAgentToForm(agent);
        return;
      }
      if (action === "test") {
        btn.disabled = true;
        try {
          const result = await api(`/api/ai/agents/${id}/test`, { method: "POST" });
          toast(`${agent.name} 连通（${result.ms} ms）：${result.reply}`, "ok");
        } catch (err) { toast(`${agent.name}：${err.message}`, "err"); }
        finally { btn.disabled = false; }
        return;
      }
      if (action === "default") {
        try {
          await api(`/api/ai/agents/${id}/default`, { method: "POST" });
          toast(`已把「${agent.name}」设为默认智能体`, "ok");
          await refreshAgents();
          await loadBootstrap();
        } catch (err) { toast(err.message, "err"); }
        return;
      }
      if (action === "delete") {
        if (!window.confirm(`删除智能体「${agent.name}」？正在用它的机器人会退回跟随默认。`)) return;
        try {
          await api(`/api/ai/agents/${id}`, { method: "DELETE" });
          if (String(state.editingAgentId) === String(id)) applyAgentToForm(null);
          toast("已删除", "ok");
          await refreshAgents();
          await loadBootstrap();
        } catch (err) { toast(err.message, "err"); }
      }
    });

    $("botClearMsgBtn").addEventListener("click", async () => {
      if (!window.confirm("确定清空所有 AI 对话记录吗？这会同时清掉上下文记忆。")) return;
      try {
        await api("/api/bot/messages", { method: "DELETE" });
        toast("对话记录已清空", "ok");
        await loadBotMessages();
      } catch (err) { toast(err.message, "err"); }
    });

    // ----- 备份 / 云备份 -----

    $("backupForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await saveBackupSettings("backupForm", [
          "backup_auto", "backup_interval", "backup_keep", "backup_restore_on_boot",
        ]);
        toast("备份设置已保存", "ok");
        await loadBackupPanel();
        if ($("backupForm").elements.backup_auto.value === "1") {
          toast("自动备份已开启，后台每 " + $("backupForm").elements.backup_interval.value + " 小时跑一次", "ok");
        }
      } catch (err) { toast(err.message, "err"); }
    });

    $("backupRunBtn").addEventListener("click", () => runBackupNow($("backupRunBtn"), "立即备份一次"));

    $("backupExportBtn").addEventListener("click", async () => {
      const btn = $("backupExportBtn");
      btn.disabled = true;
      btn.textContent = "准备中…";
      try {
        const response = await fetch("/api/backup/export?include_logs=1");
        if (!response.ok) throw new Error(`导出失败（${response.status}）`);
        const blob = await response.blob();
        const disposition = response.headers.get("Content-Disposition") || "";
        const match = disposition.match(/filename="([^"]+)"/);
        const name = match ? match[1] : `tgck-${Date.now()}.json`;
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = name;
        link.click();
        URL.revokeObjectURL(url);
        toast("备份已下载，请妥善保管（内含登录会话）", "ok");
      } catch (err) {
        toast(err.message, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = "下载备份文件";
      }
    });

    $("backupUploadBtn").addEventListener("click", () => $("backupFile").click());

    $("backupFile").addEventListener("change", async (e) => {
      const file = e.target.files && e.target.files[0];
      e.target.value = "";
      if (!file) return;
      let data;
      try {
        data = JSON.parse(await file.text());
      } catch (err) {
        toast("这个文件不是合法 JSON", "err");
        return;
      }
      const form = $("backupForm");
      const mode = form.elements.restore_mode.value;
      const restorePassword = form.elements.restore_password.value === "1";
      const warn = mode === "replace"
        ? `将先清空当前的群组、账号、机器人，再用「${file.name}」里的数据替换。\n面板设置会被备份里的值覆盖${restorePassword ? "（包括面板密码）" : "（面板密码保持不变）"}。\n\n确定继续吗？`
        : `将把「${file.name}」里的数据合并进来，已存在的群组/账号/机器人不会重复添加。\n\n确定继续吗？`;
      if (!window.confirm(warn)) return;
      const btn = $("backupUploadBtn");
      btn.disabled = true;
      btn.textContent = "恢复中…";
      try {
        const result = await api("/api/backup/restore", {
          method: "POST",
          body: { data, mode, restore_password: restorePassword },
        });
        toast(`恢复完成：${result.groups} 个群组、${result.accounts} 个账号、${result.bots} 个机器人、${result.settings} 项设置`, "ok");
        if (restorePassword) {
          toast("面板密码已随之恢复，稍后需要重新登录", "");
        }
        state.settingsFilled = false;
        state.backupFilled = false;
        await loadBootstrap();
        await loadBackupPanel();
      } catch (err) {
        toast(err.message, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = "从文件恢复…";
      }
    });

    $("backupLocalBody").addEventListener("click", async (e) => {
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      const name = btn.dataset.name;
      if (btn.dataset.action === "restore") {
        if (!window.confirm(`用本地备份「${backupNameText(name)}」替换当前数据吗？\n当前群组、账号、机器人会被清空，面板密码也会恢复成备份里的。`)) return;
        btn.disabled = true;
        try {
          const result = await api(`/api/backup/local/${encodeURIComponent(name)}/restore`, { method: "POST" });
          toast(`已恢复：${result.groups} 个群组、${result.accounts} 个账号、${result.bots} 个机器人`, "ok");
          state.settingsFilled = false;
          state.backupFilled = false;
          await loadBootstrap();
          await loadBackupPanel();
        } catch (err) { toast(err.message, "err"); }
        finally { btn.disabled = false; }
      } else if (btn.dataset.action === "delete") {
        if (!window.confirm(`确定删除本地备份「${backupNameText(name)}」吗？`)) return;
        btn.disabled = true;
        try {
          await api(`/api/backup/local/${encodeURIComponent(name)}`, { method: "DELETE" });
          toast("备份已删除", "ok");
          await loadBackupPanel();
        } catch (err) { toast(err.message, "err"); }
        finally { btn.disabled = false; }
      }
    });

    // WebDAV
    $("webdavForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const result = await saveBackupSettings("webdavForm", [
          "webdav_url", "webdav_dir", "webdav_username", "webdav_password",
          "webdav_auto", "webdav_keep",
        ]);
        $("webdavForm").elements.webdav_password.value = "";
        await loadBackupPanel();
        const info = result && result.webdav;
        if (info && info.ok) {
          toast(info.created ? `设置已保存，已自动创建文件夹：${info.dir}` : `设置已保存，文件夹已就绪：${info.dir}`, "ok");
          await loadWebdavFiles(true);
        } else if (info && info.ok === false) {
          toast(`设置已保存，但文件夹没建成：${info.error}`, "err");
        } else {
          toast("WebDAV 设置已保存", "ok");
        }
      } catch (err) { toast(err.message, "err"); }
    });

    $("webdavTestBtn").addEventListener("click", async () => {
      const btn = $("webdavTestBtn");
      btn.disabled = true;
      btn.textContent = "测试中…";
      try {
        const info = await api("/api/backup/webdav/test", { method: "POST" });
        toast(info.message || "连接成功", "ok");
        await loadWebdavFiles(true);
      } catch (err) { toast(err.message, "err"); }
      finally { btn.disabled = false; btn.textContent = "测试连接"; }
    });

    $("webdavUploadBtn").addEventListener("click", async () => {
      const btn = $("webdavUploadBtn");
      btn.disabled = true;
      btn.textContent = "上传中…";
      try {
        const result = await api("/api/backup/webdav/upload", { method: "POST" });
        toast(`已上传 ${backupNameText(result.name)}（${formatSize(result.size)}）`, "ok");
        await loadWebdavFiles(true);
        await loadBackupPanel();
      } catch (err) { toast(err.message, "err"); }
      finally { btn.disabled = false; btn.textContent = "立即上传"; }
    });

    $("webdavListBtn").addEventListener("click", () => loadWebdavFiles(false));
    $("webdavBody").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-action]");
      if (btn) restoreFromCloud("webdav", btn.dataset.name, btn);
    });

    // GitHub
    $("githubForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await saveBackupSettings("githubForm", [
          "github_token", "github_repo", "github_branch", "github_path", "github_auto", "github_keep",
        ]);
        $("githubForm").elements.github_token.value = "";
        await loadBackupPanel();
        toast("GitHub 设置已保存", "ok");
        if (!$("githubForm").elements.github_repo.value.trim()) return;
        // 存完立刻验一次，仓库名/权限不对当场就能看到具体原因
        try {
          const info = await api("/api/backup/github/test", { method: "POST" });
          toast(`${info.message || "连接成功"}${info.private ? "（私有仓库）" : ""}`, "ok");
          await loadGithubFiles(true);
        } catch (err) {
          toast(`已保存，但连不上仓库：${err.message}`, "err");
        }
      } catch (err) { toast(err.message, "err"); }
    });

    $("githubTestBtn").addEventListener("click", async () => {
      const btn = $("githubTestBtn");
      btn.disabled = true;
      btn.textContent = "测试中…";
      try {
        const info = await api("/api/backup/github/test", { method: "POST" });
        toast(`${info.message || "连接成功"}${info.private ? "（私有仓库）" : ""}`, "ok");
        await loadGithubFiles(true);
      } catch (err) { toast(err.message, "err"); }
      finally { btn.disabled = false; btn.textContent = "测试连接"; }
    });

    $("githubUploadBtn").addEventListener("click", async () => {
      const btn = $("githubUploadBtn");
      btn.disabled = true;
      btn.textContent = "同步中…";
      try {
        const result = await api("/api/backup/github/upload", { method: "POST" });
        toast(`已同步 ${backupNameText(result.name)}（${formatSize(result.size)}）`, "ok");
        await loadGithubFiles(true);
        await loadBackupPanel();
      } catch (err) { toast(err.message, "err"); }
      finally { btn.disabled = false; btn.textContent = "立即同步"; }
    });

    $("githubListBtn").addEventListener("click", () => loadGithubFiles(false));
    $("githubBody").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-action]");
      if (btn) restoreFromCloud("github", btn.dataset.name, btn);
    });

    // ----- 日志 -----

    $("logStatus").addEventListener("change", loadLogsFiltered);
    $("logLimit").addEventListener("change", loadLogsFiltered);
    $("clearLogsBtn").addEventListener("click", async () => {
      if (!window.confirm("确定清空全部运行日志吗？")) return;
      try {
        await api("/api/logs", { method: "DELETE" });
        toast("日志已清空", "ok");
        await loadBootstrap();
      } catch (err) { toast(err.message, "err"); }
    });

    $("modalCancel").addEventListener("click", closeModal);
    $("modalConfirm").addEventListener("click", confirmModal);
    $("modalInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") confirmModal();
    });

    setInterval(() => {
      if (state.tab === "dashboard" || state.tab === "logs") {
        if ($("modal").classList.contains("hidden")) loadBootstrap();
      }
    }, 30000);
  }

  async function loadLogsFiltered() {
    const params = new URLSearchParams({ limit: $("logLimit").value });
    if ($("logStatus").value) params.set("status", $("logStatus").value);
    try {
      const result = await api("/api/logs?" + params.toString());
      if (state.data) { state.data.logs = result.logs; renderLogs(state.data); }
    } catch (err) { toast(err.message, "err"); }
  }

  function verifyCode(code) {
    openModal({
      title: "输入 Telegram 验证码",
      message: "验证码由 Telegram 官方发送到该账号，通常出现在「Saved Messages」或官方通知对话中。",
      placeholder: "例如 12345",
      onConfirm: async (value) => {
        const result = await api("/api/tg/login-verify", {
          method: "POST", body: { code: value },
        });
        if (result.need_password) {
          verifyPassword();
          return;
        }
        closeModal();
        toast(result.message || "登录成功", "ok");
        await loadBootstrap();
      },
    });
    if (code) $("modalInput").value = code;
  }

  function verifyPassword() {
    openModal({
      title: "两步验证密码",
      message: "该账号启用了两步验证，请输入你设置的验证密码。",
      placeholder: "两步验证密码",
      type: "password",
      onConfirm: async (value) => {
        const result = await api("/api/tg/login-verify", {
          method: "POST", body: { code: "", password: value },
        });
        closeModal();
        toast(result.message || "登录成功", "ok");
        await loadBootstrap();
      },
    });
  }

  bindEvents();
  loadBootstrap();
})();
