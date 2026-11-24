(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    data: null,
    tab: "dashboard",
    editingGroupId: null,
    accountFilled: false,
    settingsFilled: false,
    modalHandler: null,
  };

  const TITLES = {
    dashboard: ["概览", "自动签到任务运行状态一览"],
    groups: ["群组管理", "配置需要签到的群组、指令与执行时间"],
    account: ["账号与登录", "配置 Telegram API 凭据并完成账号登录"],
    settings: ["系统设置", "重试、通知、模拟运行等全局参数"],
    logs: ["运行日志", "查看每一次签到执行的结果与机器人回复"],
  };

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  let toastTimer = null;
  function toast(message, kind = "") {
    const el = $("toast");
    el.textContent = message;
    el.className = "toast " + kind;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
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
  }

  // ---------- 渲染 ----------

  function renderDashboard(data) {
    const s = data.stats;
    $("statEnabled").textContent = s.enabled;
    $("statGroups").textContent = `共 ${s.groups} 个群组`;
    $("statSuccess").textContent = s.today_success;
    $("statFailed").textContent = s.today_failed;
    $("statTotal").textContent = s.total_runs;
    $("statJobs").textContent = `调度任务 ${data.scheduler_jobs} 个`;

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

    const account = data.account;
    $("accountBadge").textContent = account.authorized
      ? `已登录：${account.name || account.phone || "Telegram"}`
      : "Telegram 未登录";
    $("accountBadge").className = "badge " + (account.authorized ? "ok" : "warn");
    $("versionText").textContent = "v" + data.version;
  }

  function statusBadge(status) {
    return status === "success"
      ? `<span class="badge ok">成功</span>`
      : `<span class="badge err">失败</span>`;
  }

  function renderGroups(data) {
    const tbody = $("groupTableBody");
    const groups = data.groups;
    $("groupCount").textContent = `${groups.length} 个`;
    if (!groups.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty">还没有配置群组，请在上方添加</td></tr>`;
      return;
    }
    tbody.innerHTML = groups.map((g) => {
      let expressions = [];
      try { expressions = JSON.parse(g.schedule || "[]"); } catch (_) { expressions = [g.schedule]; }
      const runs = data.next_runs[String(g.id)] || [];
      const runInfo = runs.length
        ? runs.map((r) => `<div class="next-line">下次：${esc(r.next)}</div>`).join("")
        : `<div class="next-line">未启用</div>`;
      return `<tr>
        <td><strong>${esc(g.name)}</strong></td>
        <td class="mono">${esc(g.chat)}</td>
        <td class="mono">${esc(g.command)}</td>
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
      <td>${log.source === "manual" ? "手动" : "自动"}</td>
      <td>${statusBadge(log.status)}</td>
      <td class="note">${esc(log.message || "-")}</td>
      <td>${log.duration} ms</td>
    </tr>`).join("");
  }

  async function ensureCredentials() {
    if (state.accountFilled) return;
    try {
      const credentials = await api("/api/tg/credentials");
      const form = $("accountForm");
      ["api_id", "api_hash", "phone", "proxy_url"].forEach((key) => {
        form.elements[key].value = credentials[key] || "";
      });
      state.accountFilled = true;
    } catch (err) {
      // 凭据回填失败不影响其他页面使用
    }
  }

  function fillSettings(data) {
    if (state.settingsFilled) return;
    const form = $("settingsForm");
    const s = data.settings;
    ["timezone", "retry_count", "retry_delay", "jitter", "notify_chat", "fail_keywords", "dry_run"].forEach((key) => {
      if (form.elements[key]) form.elements[key].value = s[key] ?? "";
    });
    state.settingsFilled = true;
    if (s.using_default_password) {
      toast("当前面板密码为默认值 admin123，建议在「系统设置」中修改", "err");
    }
  }

  function renderAll(data) {
    state.data = data;
    renderDashboard(data);
    renderGroups(data);
    renderLogs(data);
    fillSettings(data);
    ensureCredentials();
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

    $("groupForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const payload = {
        name: form.elements.name.value.trim(),
        chat: form.elements.chat.value.trim(),
        command: form.elements.command.value.trim() || "/checkin",
        schedule: form.elements.schedule.value,
        enabled: form.elements.enabled.value === "1",
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
          toast(`${result.group_name} 签到成功：${result.message.slice(0, 80)}`, "ok");
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

    $("accountForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const payload = {
        api_id: form.elements.api_id.value.trim(),
        api_hash: form.elements.api_hash.value.trim(),
        phone: form.elements.phone.value.trim(),
        proxy_url: form.elements.proxy_url.value.trim(),
      };
      try {
        await api("/api/settings", { method: "PUT", body: payload });
        toast("API 凭据已保存", "ok");
        await loadBootstrap();
      } catch (err) { toast(err.message, "err"); }
    });

    $("checkAccountBtn").addEventListener("click", async () => {
      try {
        const status = await api("/api/tg/status");
        $("accountState").textContent = status.authorized ? "已登录" : "未登录";
        $("accountState").className = "badge " + (status.authorized ? "ok" : "warn");
        $("accountInfo").innerHTML = status.authorized
          ? `当前账号：<strong>${esc(status.name || status.phone || "Telegram 用户")}</strong>`
          : esc(status.error || "尚未登录 Telegram 账号，请先获取验证码");
      } catch (err) { toast(err.message, "err"); }
    });

    $("loginStartBtn").addEventListener("click", async () => {
      const form = $("accountForm");
      const payload = {
        api_id: form.elements.api_id.value.trim(),
        api_hash: form.elements.api_hash.value.trim(),
        phone: form.elements.phone.value.trim(),
        proxy_url: form.elements.proxy_url.value.trim(),
      };
      try {
        const result = await api("/api/tg/login-start", { method: "POST", body: payload });
        toast(result.message, "ok");
        verifyCode("");
      } catch (err) { toast(err.message, "err"); }
    });

    $("tgLogoutBtn").addEventListener("click", async () => {
      if (!window.confirm("确定清除本地 Telegram 登录会话吗？")) return;
      try {
        await api("/api/tg/logout", { method: "POST" });
        toast("已退出 Telegram 账号", "ok");
        await loadBootstrap();
      } catch (err) { toast(err.message, "err"); }
    });

    $("settingsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.currentTarget;
      const payload = {};
      ["timezone", "retry_count", "retry_delay", "jitter", "notify_chat", "fail_keywords", "dry_run", "panel_password"].forEach((key) => {
        payload[key] = form.elements[key].value.trim();
      });
      try {
        await api("/api/settings", { method: "PUT", body: payload });
        form.elements.panel_password.value = "";
        toast("设置已保存", "ok");
        await loadBootstrap();
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

  function resetGroupForm() {
    const form = $("groupForm");
    form.reset();
    form.elements.command.value = "/checkin";
    state.editingGroupId = null;
    $("groupFormTitle").textContent = "添加群组";
    $("groupSubmitBtn").textContent = "添加群组";
    $("groupCancelBtn").classList.add("hidden");
  }

  async function loadLogsFiltered() {
    const params = new URLSearchParams({ limit: $("logLimit").value });
    if ($("logStatus").value) params.set("status", $("logStatus").value);
    try {
      const result = await api("/api/logs?" + params.toString());
      if (state.data) { state.data.logs = result.logs; renderLogs(state.data); }
    } catch (err) { toast(err.message, "err"); }
  }

  async function verifyCode(code) {
    openModal({
      title: "输入 Telegram 验证码",
      message: "验证码由 Telegram 官方发送到你的账号，通常出现在「Saved Messages」或官方通知对话中。",
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
