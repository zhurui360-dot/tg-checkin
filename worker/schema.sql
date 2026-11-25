-- Cloudflare Workers + D1 版数据库结构
-- 这个文件用于 `wrangler d1 execute --file`；Worker 首次请求时也会自动建表，
-- 所以正常情况下不需要手动执行，留着是为了让你能提前查看/初始化。

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

-- AI 智能体：名字、接口、模型、人设、上下文与冷却
CREATE TABLE IF NOT EXISTS ai_agents (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL DEFAULT '',
  base_url      TEXT NOT NULL DEFAULT '',
  api_key       TEXT NOT NULL DEFAULT '',
  model         TEXT NOT NULL DEFAULT '',
  system_prompt TEXT NOT NULL DEFAULT '',
  temperature   TEXT NOT NULL DEFAULT '0.7',
  max_tokens    INTEGER NOT NULL DEFAULT 800,
  history_len   INTEGER NOT NULL DEFAULT 12,
  min_interval  INTEGER NOT NULL DEFAULT 3,
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

-- Telegram 机器人：token（等于 webhook 路径的凭据之一）、触发方式、绑定智能体
CREATE TABLE IF NOT EXISTS bots (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  name                 TEXT NOT NULL DEFAULT '',
  token                TEXT NOT NULL DEFAULT '',
  username             TEXT NOT NULL DEFAULT '',
  enabled              INTEGER NOT NULL DEFAULT 1,
  trigger              TEXT NOT NULL DEFAULT 'mention',
  webhook_secret       TEXT NOT NULL DEFAULT '',
  ai_agent_id          INTEGER,
  business_connections TEXT NOT NULL DEFAULT '',
  known_chats          TEXT NOT NULL DEFAULT '',
  created_at           INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

-- 上下文：最近 N 条对话。scope_id = 智能体 id，换机器人接手同一智能体也能接上前文
CREATE TABLE IF NOT EXISTS ai_messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_id   INTEGER NOT NULL DEFAULT 0,
  chat_id    TEXT NOT NULL,
  chat_title TEXT NOT NULL DEFAULT '',
  role       TEXT NOT NULL,
  author     TEXT NOT NULL DEFAULT '',
  content    TEXT NOT NULL,
  ts         INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE INDEX IF NOT EXISTS idx_ai_chat ON ai_messages(scope_id, chat_id, id DESC);

-- 长期记忆：scope = global / chat / user
CREATE TABLE IF NOT EXISTS ai_notes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id   INTEGER NOT NULL DEFAULT 0,
  scope      TEXT NOT NULL DEFAULT 'global',
  scope_key  TEXT NOT NULL DEFAULT '',
  content    TEXT NOT NULL,
  source     TEXT NOT NULL DEFAULT 'auto',
  created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
  used_at    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notes_agent ON ai_notes(agent_id, scope, scope_key);

-- 处理流水：每条进来的消息记一行「回了 / 为什么没回」，否则面板上只看得到没回复
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id     INTEGER NOT NULL DEFAULT 0,
  chat_id    TEXT NOT NULL DEFAULT '',
  chat_title TEXT NOT NULL DEFAULT '',
  author     TEXT NOT NULL DEFAULT '',
  preview    TEXT NOT NULL DEFAULT '',
  action     TEXT NOT NULL DEFAULT '',
  reason     TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '',
  ts         INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE INDEX IF NOT EXISTS idx_events ON events(id DESC);

-- 去重：同一 update_id 只处理一次。Telegram 在超时重试时会重复推送
CREATE TABLE IF NOT EXISTS seen_updates (
  update_id INTEGER PRIMARY KEY,
  ts        INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

-- 记忆提炼节流：至少隔 3 轮、90 秒，避免每句话都多花一次模型调用
CREATE TABLE IF NOT EXISTS learn_state (
  scope_id INTEGER NOT NULL,
  chat_id  TEXT NOT NULL,
  turns    INTEGER NOT NULL DEFAULT 0,
  last_ts  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (scope_id, chat_id)
);
