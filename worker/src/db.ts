/**
 * D1 数据层。
 *
 * 和 Python 版一样是 SQLite 语法，所以 SQL 几乎原样搬过来；差别在于：
 * - D1 没有长连接，每次都是 prepare/bind/all，因此不做连接池；
 * - 时间统一存 Unix 秒（整数），避免 UTC / 本地时区在统计上算错；
 * - 首次请求自动建表，部署完不用先跑一遍 CLI，省一步。
 */

export interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  PANEL_PASSWORD?: string;
  APP_TITLE?: string;
}

export const nowSec = (): number => Math.floor(Date.now() / 1000);

/** 面板可配置项。key 与前端字段一一对应。 */
export const DEFAULTS: Record<string, string> = {
  timezone: "Asia/Shanghai",
  // 全局兜底：具体机器人可以各自覆盖
  bot_trigger: "mention",
  ai_base_url: "https://api.deepseek.com/v1",
  ai_api_key: "",
  ai_model: "deepseek-chat",
  ai_system_prompt: "你是一个在 Telegram 里聊天的助手，回答简洁、口语化，用中文回复。不确定的事不要编造。",
  ai_history_len: "12",
  ai_max_tokens: "800",
  ai_temperature: "0.7",
  ai_min_interval: "3",
  ai_agent_id: "",
  // 记忆
  memory_enabled: "1",
  memory_learn: "1",
  memory_max_notes: "8",
  // 面板
  panel_title: "AI 机器人控制台",
  installed_at: "",
};

const SCHEMA: string[] = [
  `CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')`,
  `CREATE TABLE IF NOT EXISTS ai_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    temperature TEXT NOT NULL DEFAULT '0.7',
    max_tokens INTEGER NOT NULL DEFAULT 800,
    history_len INTEGER NOT NULL DEFAULT 12,
    min_interval INTEGER NOT NULL DEFAULT 3,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
  )`,
  `CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    trigger TEXT NOT NULL DEFAULT 'mention',
    webhook_secret TEXT NOT NULL DEFAULT '',
    ai_agent_id INTEGER,
    business_connections TEXT NOT NULL DEFAULT '',
    known_chats TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
  )`,
  `CREATE TABLE IF NOT EXISTS ai_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id INTEGER NOT NULL DEFAULT 0,
    chat_id TEXT NOT NULL,
    chat_title TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    ts INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
  )`,
  `CREATE INDEX IF NOT EXISTS idx_ai_chat ON ai_messages(scope_id, chat_id, id DESC)`,
  `CREATE TABLE IF NOT EXISTS ai_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id INTEGER NOT NULL DEFAULT 0,
    scope TEXT NOT NULL DEFAULT 'global',
    scope_key TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    used_at INTEGER NOT NULL DEFAULT 0
  )`,
  `CREATE INDEX IF NOT EXISTS idx_notes_agent ON ai_notes(agent_id, scope, scope_key)`,
  `CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id INTEGER NOT NULL DEFAULT 0,
    chat_id TEXT NOT NULL DEFAULT '',
    chat_title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    preview TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
  )`,
  `CREATE INDEX IF NOT EXISTS idx_events ON events(id DESC)`,
  `CREATE TABLE IF NOT EXISTS seen_updates (
    update_id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
  )`,
  `CREATE TABLE IF NOT EXISTS learn_state (
    scope_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0,
    last_ts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope_id, chat_id)
  )`,
];

let schemaReady = false;

/** 建表 + 写入默认配置。同一个 isolate 只跑一次。 */
export async function ensureSchema(env: Env): Promise<void> {
  if (schemaReady) return;
  await env.DB.batch(SCHEMA.map((sql) => env.DB.prepare(sql)));

  const entries = Object.entries(DEFAULTS);
  for (let i = 0; i < entries.length; i += 20) {
    const chunk = entries.slice(i, i + 20);
    const placeholders = chunk.map(() => "(?,?)").join(",");
    const values = chunk.flatMap(([k, v]) => [k, v]);
    await env.DB.prepare(
      `INSERT OR IGNORE INTO settings(key, value) VALUES ${placeholders}`,
    ).bind(...values).run();
  }
  schemaReady = true;
}

// ---------------- settings ----------------

export async function getSettings(env: Env): Promise<Record<string, string>> {
  const { results } = await env.DB.prepare("SELECT key, value FROM settings").all<{ key: string; value: string }>();
  const map: Record<string, string> = { ...DEFAULTS };
  for (const row of results || []) map[row.key] = row.value;
  return map;
}

export async function getSetting(env: Env, key: string, fallback = ""): Promise<string> {
  const row = await env.DB.prepare("SELECT value FROM settings WHERE key=?").bind(key).first<{ value: string }>();
  if (row && row.value !== undefined && row.value !== null) return row.value;
  return key in DEFAULTS ? DEFAULTS[key] : fallback;
}

export async function setSettings(env: Env, data: Record<string, string | number | boolean>): Promise<void> {
  const entries = Object.entries(data).filter(([key]) => key in DEFAULTS);
  if (!entries.length) return;
  await env.DB.batch(
    entries.map(([key, value]) =>
      env.DB.prepare(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      ).bind(key, String(value)),
    ),
  );
}

// ---------------- 智能体 ----------------

export interface AgentRow {
  id: number;
  name: string;
  base_url: string;
  api_key: string;
  model: string;
  system_prompt: string;
  temperature: string;
  max_tokens: number;
  history_len: number;
  min_interval: number;
  enabled: number;
  created_at: number;
}

const AGENT_FIELDS = [
  "name", "base_url", "api_key", "model", "system_prompt",
  "temperature", "max_tokens", "history_len", "min_interval", "enabled",
] as const;

export function publicAgent(row: AgentRow): Record<string, unknown> {
  return {
    id: row.id,
    name: row.name,
    base_url: row.base_url,
    model: row.model,
    system_prompt: row.system_prompt,
    temperature: row.temperature,
    max_tokens: row.max_tokens,
    history_len: row.history_len,
    min_interval: row.min_interval,
    enabled: Boolean(row.enabled),
    key_set: Boolean((row.api_key || "").trim()),
  };
}

export async function listAgents(env: Env): Promise<AgentRow[]> {
  const { results } = await env.DB.prepare("SELECT * FROM ai_agents ORDER BY id").all<AgentRow>();
  return results || [];
}

export async function getAgent(env: Env, id: number): Promise<AgentRow | null> {
  return await env.DB.prepare("SELECT * FROM ai_agents WHERE id=?").bind(id).first<AgentRow>();
}

function pickAgentValue(key: string, value: unknown): unknown {
  if (["max_tokens", "history_len", "min_interval"].includes(key)) {
    const n = Number(value);
    return Number.isFinite(n) ? Math.round(n) : 0;
  }
  if (key === "enabled") return value ? 1 : 0;
  return String(value ?? "");
}

export async function addAgent(env: Env, data: Record<string, unknown>): Promise<number> {
  const row: Record<string, unknown> = {};
  for (const key of AGENT_FIELDS) row[key] = pickAgentValue(key, data[key] ?? "");
  if (!row.name) row.name = "未命名智能体";
  if (!row.temperature) row.temperature = "0.7";
  row.max_tokens = row.max_tokens || 800;
  row.history_len = Number(row.history_len) || 12;
  row.min_interval = Number(row.min_interval) || 3;
  if (data.enabled === undefined) row.enabled = 1;

  const keys = AGENT_FIELDS.join(",");
  const marks = AGENT_FIELDS.map(() => "?").join(",");
  const result = await env.DB.prepare(`INSERT INTO ai_agents(${keys}) VALUES(${marks})`)
    .bind(...AGENT_FIELDS.map((k) => row[k]))
    .run();
  return Number(result.meta.last_row_id || 0);
}

export async function updateAgent(env: Env, id: number, data: Record<string, unknown>): Promise<boolean> {
  const current = await getAgent(env, id);
  if (!current) return false;
  const merged: Record<string, unknown> = { ...current };
  for (const key of AGENT_FIELDS) {
    if (data[key] !== undefined && data[key] !== null) merged[key] = pickAgentValue(key, data[key]);
  }
  if (!merged.name) merged.name = "未命名智能体";
  const sets = AGENT_FIELDS.map((k) => `${k}=?`).join(", ");
  const result = await env.DB.prepare(`UPDATE ai_agents SET ${sets} WHERE id=?`)
    .bind(...AGENT_FIELDS.map((k) => merged[k]), id)
    .run();
  return Number(result.meta.changes || 0) > 0;
}

export async function deleteAgent(env: Env, id: number): Promise<boolean> {
  const result = await env.DB.prepare("DELETE FROM ai_agents WHERE id=?").bind(id).run();
  await env.DB.prepare("UPDATE bots SET ai_agent_id=NULL WHERE ai_agent_id=?").bind(id).run();
  await env.DB.prepare("DELETE FROM ai_notes WHERE agent_id=?").bind(id).run();
  return Number(result.meta.changes || 0) > 0;
}

export async function defaultAgentId(env: Env): Promise<number | null> {
  const raw = (await getSetting(env, "ai_agent_id", "")).trim();
  if (/^\d+$/.test(raw) && (await getAgent(env, Number(raw)))) return Number(raw);
  const enabled = (await listAgents(env)).filter((a) => a.enabled);
  return enabled.length ? Number(enabled[0].id) : null;
}

/** 机器人实际用哪个智能体：自己绑的 → 全局默认 → 第一个启用的。 */
export async function agentForBot(env: Env, bot: BotRow | null): Promise<AgentRow | null> {
  if (bot && bot.ai_agent_id) {
    const own = await getAgent(env, Number(bot.ai_agent_id));
    if (own) return own;
  }
  const id = await defaultAgentId(env);
  return id ? await getAgent(env, id) : null;
}

// ---------------- 机器人 ----------------

export interface BotRow {
  id: number;
  name: string;
  token: string;
  username: string;
  enabled: number;
  trigger: string;
  webhook_secret: string;
  ai_agent_id: number | null;
  business_connections: string;
  known_chats: string;
  created_at: number;
}

const BOT_FIELDS = [
  "name", "token", "username", "enabled", "trigger", "webhook_secret", "ai_agent_id",
  "business_connections", "known_chats",
] as const;

export function publicBot(row: BotRow): Record<string, unknown> {
  let chats: Array<Record<string, unknown>> = [];
  try {
    const parsed = JSON.parse(row.known_chats || "[]");
    if (Array.isArray(parsed)) chats = parsed;
  } catch {
    chats = [];
  }
  return {
    id: row.id,
    name: row.name,
    username: row.username,
    enabled: Boolean(row.enabled),
    trigger: row.trigger || "mention",
    ai_agent_id: row.ai_agent_id,
    token_set: Boolean((row.token || "").trim()),
    secret_set: Boolean((row.webhook_secret || "").trim()),
    known_chats: chats,
  };
}

export async function listBots(env: Env): Promise<BotRow[]> {
  const { results } = await env.DB.prepare("SELECT * FROM bots ORDER BY id").all<BotRow>();
  return results || [];
}

export async function getBot(env: Env, id: number): Promise<BotRow | null> {
  return await env.DB.prepare("SELECT * FROM bots WHERE id=?").bind(id).first<BotRow>();
}

export async function getBotByToken(env: Env, token: string): Promise<BotRow | null> {
  return await env.DB.prepare("SELECT * FROM bots WHERE token=?").bind(token).first<BotRow>();
}

export async function addBot(env: Env, data: Record<string, unknown>): Promise<number> {
  const result = await env.DB.prepare(
    "INSERT INTO bots(name, token, username, enabled, trigger, webhook_secret, ai_agent_id) VALUES(?,?,?,?,?,?,?)",
  ).bind(
    String(data.name || data.username || "机器人"),
    String(data.token || ""),
    String(data.username || ""),
    1,
    String(data.trigger || "mention"),
    String(data.webhook_secret || ""),
    data.ai_agent_id ? Number(data.ai_agent_id) : null,
  ).run();
  return Number(result.meta.last_row_id || 0);
}

export async function updateBot(env: Env, id: number, data: Record<string, unknown>): Promise<boolean> {
  const current = await getBot(env, id);
  if (!current) return false;
  const merged: Record<string, unknown> = { ...current };
  for (const key of BOT_FIELDS) {
    if (data[key] !== undefined && data[key] !== null) merged[key] = data[key];
  }
  const result = await env.DB.prepare(
    "UPDATE bots SET name=?, token=?, username=?, enabled=?, trigger=?, webhook_secret=?, "
    + "ai_agent_id=?, business_connections=?, known_chats=? WHERE id=?",
  ).bind(
    String(merged.name || ""),
    String(merged.token || ""),
    String(merged.username || ""),
    merged.enabled ? 1 : 0,
    String(merged.trigger || "mention"),
    String(merged.webhook_secret || ""),
    merged.ai_agent_id ? Number(merged.ai_agent_id) : null,
    String(merged.business_connections || ""),
    String(merged.known_chats || ""),
    id,
  ).run();
  return Number(result.meta.changes || 0) > 0;
}

export async function deleteBot(env: Env, id: number): Promise<boolean> {
  const result = await env.DB.prepare("DELETE FROM bots WHERE id=?").bind(id).run();
  return Number(result.meta.changes || 0) > 0;
}

// ---------------- 上下文 ----------------

const MAX_HISTORY_ROWS = 60;

export async function addAiMessage(
  env: Env,
  scopeId: number,
  chatId: string,
  chatTitle: string,
  role: string,
  author: string,
  content: string,
): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO ai_messages(scope_id, chat_id, chat_title, role, author, content) VALUES(?,?,?,?,?,?)",
  ).bind(scopeId, String(chatId), (chatTitle || "").slice(0, 120), role, (author || "").slice(0, 80), (content || "").slice(0, 4000)).run();
  // 每个会话只留最近 60 条，D1 免费 5GB 也经不起无限涨
  await env.DB.prepare(
    "DELETE FROM ai_messages WHERE scope_id=? AND chat_id=? AND id NOT IN "
    + "(SELECT id FROM ai_messages WHERE scope_id=? AND chat_id=? ORDER BY id DESC LIMIT ?)",
  ).bind(scopeId, String(chatId), scopeId, String(chatId), MAX_HISTORY_ROWS).run();
}

export interface MessageRow {
  role: string;
  author: string;
  content: string;
  ts: number;
  chat_title?: string;
  chat_id?: string;
}

export async function aiHistory(env: Env, scopeId: number, chatId: string, limit: number): Promise<MessageRow[]> {
  const cap = Math.max(1, Math.min(limit, 40));
  const { results } = await env.DB.prepare(
    "SELECT role, author, content, ts FROM ai_messages WHERE scope_id=? AND chat_id=? ORDER BY id DESC LIMIT ?",
  ).bind(scopeId, String(chatId), cap).all<MessageRow>();
  return (results || []).reverse();
}

export async function listAiMessages(env: Env, limit = 60, scopeId?: number): Promise<Record<string, unknown>[]> {
  const cap = Math.max(1, Math.min(limit, 300));
  const sql = scopeId === undefined
    ? "SELECT * FROM ai_messages ORDER BY id DESC LIMIT ?"
    : "SELECT * FROM ai_messages WHERE scope_id=? ORDER BY id DESC LIMIT ?";
  const stmt = env.DB.prepare(sql);
  const { results } = scopeId === undefined
    ? await stmt.bind(cap).all()
    : await stmt.bind(scopeId, cap).all();
  return results || [];
}

export async function clearAiMessages(env: Env, scopeId?: number): Promise<void> {
  if (scopeId === undefined) {
    await env.DB.prepare("DELETE FROM ai_messages").run();
  } else {
    await env.DB.prepare("DELETE FROM ai_messages WHERE scope_id=?").bind(scopeId).run();
  }
}

export async function aiStats(env: Env): Promise<{ total: number; today: number; chats: number }> {
  const total = await env.DB.prepare("SELECT COUNT(*) AS n FROM ai_messages").first<{ n: number }>();
  const chats = await env.DB.prepare("SELECT COUNT(DISTINCT chat_id) AS n FROM ai_messages").first<{ n: number }>();
  const todayStart = Math.floor(new Date(new Date().toISOString().slice(0, 10) + "T00:00:00Z").getTime() / 1000);
  const today = await env.DB.prepare(
    "SELECT COUNT(*) AS n FROM ai_messages WHERE role='assistant' AND ts>=?",
  ).bind(todayStart).first<{ n: number }>();
  return {
    total: Number(total?.n || 0),
    today: Number(today?.n || 0),
    chats: Number(chats?.n || 0),
  };
}

// ---------------- 长期记忆 ----------------

export interface NoteRow {
  id: number;
  agent_id: number;
  scope: string;
  scope_key: string;
  content: string;
  source: string;
  created_at: number;
  used_at: number;
}

export async function addNote(
  env: Env,
  agentId: number,
  scope: string,
  scopeKey: string,
  content: string,
  source = "auto",
): Promise<number> {
  const result = await env.DB.prepare(
    "INSERT INTO ai_notes(agent_id, scope, scope_key, content, source) VALUES(?,?,?,?,?)",
  ).bind(agentId, scope, (scopeKey || "").slice(0, 120), content.slice(0, 400), source).run();
  return Number(result.meta.last_row_id || 0);
}

export async function listNotes(
  env: Env,
  agentId?: number,
  scope?: string,
  limit = 200,
): Promise<NoteRow[]> {
  let sql = "SELECT * FROM ai_notes WHERE 1=1";
  const params: unknown[] = [];
  if (agentId !== undefined) {
    sql += " AND agent_id=?";
    params.push(agentId);
  }
  if (scope) {
    sql += " AND scope=?";
    params.push(scope);
  }
  sql += " ORDER BY id DESC LIMIT ?";
  params.push(Math.max(1, Math.min(limit, 500)));
  const { results } = await env.DB.prepare(sql).bind(...params).all<NoteRow>();
  return results || [];
}

/** 候选池：全局 + 当前会话 + 当前这个人。 */
export async function notePool(env: Env, agentId: number, chatId: string, author: string): Promise<NoteRow[]> {
  const { results } = await env.DB.prepare(
    "SELECT * FROM ai_notes WHERE agent_id=? AND ((scope='global') OR (scope='chat' AND scope_key=?) "
    + "OR (scope='user' AND scope_key=?)) ORDER BY id DESC LIMIT 300",
  ).bind(agentId, String(chatId), (author || "").slice(0, 120)).all<NoteRow>();
  return results || [];
}

export async function deleteNote(env: Env, id: number): Promise<boolean> {
  const result = await env.DB.prepare("DELETE FROM ai_notes WHERE id=?").bind(id).run();
  return Number(result.meta.changes || 0) > 0;
}

export async function clearNotes(env: Env, agentId: number, scope = ""): Promise<number> {
  const sql = scope
    ? "DELETE FROM ai_notes WHERE agent_id=? AND scope=?"
    : "DELETE FROM ai_notes WHERE agent_id=?";
  const stmt = env.DB.prepare(sql);
  const result = scope ? await stmt.bind(agentId, scope).run() : await stmt.bind(agentId).run();
  return Number(result.meta.changes || 0);
}

export async function touchNotes(env: Env, ids: number[]): Promise<void> {
  if (!ids.length) return;
  const marks = ids.map(() => "?").join(",");
  await env.DB.prepare(`UPDATE ai_notes SET used_at=? WHERE id IN (${marks})`)
    .bind(nowSec(), ...ids).run();
}

export async function noteStats(env: Env, agentId: number): Promise<Record<string, number>> {
  const { results } = await env.DB.prepare(
    "SELECT scope, COUNT(*) AS n FROM ai_notes WHERE agent_id=? GROUP BY scope",
  ).bind(agentId).all<{ scope: string; n: number }>();
  const data: Record<string, number> = { total: 0, global: 0, chat: 0, user: 0 };
  for (const row of results || []) {
    data[row.scope] = Number(row.n);
    data.total += Number(row.n);
  }
  return data;
}

// ---------------- 处理流水 ----------------

export interface EventRow {
  id: number;
  bot_id: number;
  chat_id: string;
  chat_title: string;
  author: string;
  preview: string;
  action: string;
  reason: string;
  detail: string;
  ts: number;
}

export async function addEvent(
  env: Env,
  data: { botId: number; chatId?: string; chatTitle?: string; author?: string; preview?: string; action: string; reason?: string; detail?: string },
): Promise<void> {
  await env.DB.prepare(
    "INSERT INTO events(bot_id, chat_id, chat_title, author, preview, action, reason, detail) VALUES(?,?,?,?,?,?,?,?)",
  ).bind(
    data.botId || 0,
    String(data.chatId || "").slice(0, 40),
    String(data.chatTitle || "").slice(0, 120),
    String(data.author || "").slice(0, 80),
    String(data.preview || "").slice(0, 200),
    String(data.action || "").slice(0, 16),
    String(data.reason || "").slice(0, 120),
    String(data.detail || "").slice(0, 500),
  ).run();
  await env.DB.prepare(
    "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)",
  ).run();
}

export async function listEvents(env: Env, limit = 80, action = ""): Promise<EventRow[]> {
  const cap = Math.max(1, Math.min(limit, 500));
  const sql = action
    ? "SELECT * FROM events WHERE action=? ORDER BY id DESC LIMIT ?"
    : "SELECT * FROM events ORDER BY id DESC LIMIT ?";
  const stmt = env.DB.prepare(sql);
  const { results } = action ? await stmt.bind(action, cap).all<EventRow>() : await stmt.bind(cap).all<EventRow>();
  return results || [];
}

export async function clearEvents(env: Env): Promise<void> {
  await env.DB.prepare("DELETE FROM events").run();
}

export async function eventStats(env: Env): Promise<Record<string, number | string>> {
  const { results } = await env.DB.prepare("SELECT action, COUNT(*) AS n FROM events GROUP BY action")
    .all<{ action: string; n: number }>();
  const stats: Record<string, number | string> = { total: 0, replied: 0, skipped: 0, error: 0, last_ts: 0 };
  for (const row of results || []) {
    stats[row.action] = Number(row.n);
    stats.total = Number(stats.total) + Number(row.n);
  }
  const last = await env.DB.prepare("SELECT ts FROM events ORDER BY id DESC LIMIT 1").first<{ ts: number }>();
  stats.last_ts = Number(last?.ts || 0);
  return stats;
}

// ---------------- 记忆提炼节流 ----------------

/** 返回 true 表示这一轮该提炼（每 3 轮且间隔 ≥ 90 秒）。 */
export async function shouldLearn(env: Env, scopeId: number, chatId: string): Promise<boolean> {
  const row = await env.DB.prepare(
    "SELECT turns, last_ts FROM learn_state WHERE scope_id=? AND chat_id=?",
  ).bind(scopeId, String(chatId)).first<{ turns: number; last_ts: number }>();
  const turns = Number(row?.turns || 0) + 1;
  const lastTs = Number(row?.last_ts || 0);
  const now = nowSec();
  const go = turns % 3 === 0 && now - lastTs >= 90;
  await env.DB.prepare(
    "INSERT INTO learn_state(scope_id, chat_id, turns, last_ts) VALUES(?,?,?,?) "
    + "ON CONFLICT(scope_id, chat_id) DO UPDATE SET turns=excluded.turns, last_ts=excluded.last_ts",
  ).bind(scopeId, String(chatId), turns, go ? now : lastTs).run();
  return go;
}
