/**
 * 长期记忆：自动提炼 + 按相关度注入。
 *
 * 上下文是「最近 N 条对话」，滚出窗口就没了；这里的记忆是主动提炼的稳定事实
 * （称呼、偏好、身份、约定），存在 D1 里，跨天、跨会话都在。
 *
 * 不引向量库：D1 没有向量索引，而且记忆只有几十条，字符二元组打分足够，
 * 一次查询加排序在 Workers 上只消耗几十微秒 CPU。
 */

import {
  addNote,
  deleteNote,
  listNotes,
  notePool,
  nowSec,
  shouldLearn,
  touchNotes,
  type AgentRow,
  type Env,
} from "./db";
import { agentConfig, callAi, type AgentConfig } from "./ai";
import { isSimilar, pickNotes } from "./lib/text";

const MAX_NOTES_PER_SCOPE = 12;
const MAX_NOTES_PER_AGENT = 120;

const EXTRACT_SYSTEM =
  "你是记忆整理器。从给定的一轮对话里抽取值得长期记住的稳定事实："
  + "对方的名字或称呼、身份职业、长期偏好与忌讳、明确说过的约定、正在做的事。"
  + "不要记录寒暄、一次性问题、模型自己的猜测。"
  + '只输出 JSON 数组，元素形如 {"scope":"user","content":"..."}；'
  + "scope 取 user（关于这个人）、chat（关于这个群或这个会话）、global（跟谁聊都适用）。"
  + "每条不超过 50 个字，用陈述句，不要带「用户说」这类前缀。没有值得记的就输出 []。";

export interface MemoryOptions {
  env: Env;
  settings: Record<string, string>;
  agent: AgentRow | null;
  chatId: string;
  author: string;
  query: string;
}

/** 挑出和当前这句话最相关的几条记忆，拼成一段系统提示。 */
export async function memoryContext(opts: MemoryOptions): Promise<string> {
  if (opts.settings.memory_enabled !== "1") return "";
  const agentId = Number(opts.agent?.id || 0);
  const pool = await notePool(opts.env, agentId, opts.chatId, opts.author);
  if (!pool.length) return "";

  const limit = Number(opts.settings.memory_max_notes || 8) || 8;
  const picked = pickNotes(
    pool.map((n) => ({ id: Number(n.id), scope: n.scope, content: n.content })),
    opts.query,
    opts.author,
    limit,
    700,
  );
  if (!picked.length) return "";

  await touchNotes(opts.env, picked.map((n) => n.id));
  const labels: Record<string, string> = { user: "关于对方", chat: "关于这个会话", global: "通用" };
  const lines = picked.map((n) => `- （${labels[n.scope] || "通用"}）${n.content}`);
  return "以下是你已经记住的信息，自然使用即可：不要逐条复述，也不要编造没记过的事。\n" + lines.join("\n");
}

function parseExtract(raw: string): Array<{ scope: string; content: string }> {
  const text = (raw || "").trim();
  const start = text.indexOf("[");
  const end = text.lastIndexOf("]");
  if (start < 0 || end <= start) return [];
  let data: unknown;
  try {
    data = JSON.parse(text.slice(start, end + 1));
  } catch {
    return [];
  }
  if (!Array.isArray(data)) return [];
  const result: Array<{ scope: string; content: string }> = [];
  for (const item of data.slice(0, 4)) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const content = String(record.content ?? "").trim();
    let scope = String(record.scope ?? "user").trim();
    if (!["user", "chat", "global"].includes(scope)) scope = "user";
    if (!content || content.length > 80) continue;
    result.push({ scope, content });
  }
  return result;
}

/** 每个作用域只留最近若干条，避免记忆无限膨胀。 */
async function trimNotes(env: Env, agentId: number, chatId: string, author: string): Promise<void> {
  const scopes: Array<[string, string]> = [
    ["global", ""],
    ["chat", chatId],
    ["user", (author || "").slice(0, 120)],
  ];
  for (const [scope, key] of scopes) {
    const rows = (await listNotes(env, agentId, scope, MAX_NOTES_PER_SCOPE + 20))
      .filter((row) => (scope === "global" ? true : row.scope_key === key));
    for (const row of rows.slice(MAX_NOTES_PER_SCOPE)) {
      await deleteNote(env, Number(row.id));
    }
  }
  const all = await listNotes(env, agentId, undefined, MAX_NOTES_PER_AGENT + 50);
  for (const row of all.slice(MAX_NOTES_PER_AGENT)) {
    await deleteNote(env, Number(row.id));
  }
}

export interface LearnOptions {
  env: Env;
  settings: Record<string, string>;
  agent: AgentRow | null;
  chatId: string;
  chatTitle: string;
  author: string;
  userText: string;
  answer: string;
}

/** 让模型从这一轮对话里提炼记忆并入库。返回新加的条目。 */
export async function learnNotes(opts: LearnOptions): Promise<string[]> {
  if (opts.settings.memory_learn !== "1") return [];
  if ((opts.userText || "").trim().length < 6) return [];

  const agentId = Number(opts.agent?.id || 0);
  if (!(await shouldLearn(opts.env, agentId, opts.chatId))) return [];

  const cfg: AgentConfig = agentConfig(opts.agent, opts.settings);
  const prompt = `会话：${opts.chatTitle || opts.chatId}\n`
    + `对方：${opts.author || "未知"}\n`
    + `对方说：${opts.userText.slice(0, 800)}\n`
    + `你回答：${opts.answer.slice(0, 800)}`;

  let raw = "";
  try {
    raw = await callAi(cfg, [
      { role: "system", content: EXTRACT_SYSTEM },
      { role: "user", content: prompt },
    ], 25000);
  } catch {
    // 记忆提炼失败不能影响正常回复，静默跳过
    return [];
  }

  const added: string[] = [];
  const existing = await notePool(opts.env, agentId, opts.chatId, opts.author);
  const known = existing.map((n) => n.content);

  for (const item of parseExtract(raw)) {
    if (known.some((content) => isSimilar(item.content, content))) continue;
    const key = item.scope === "global" ? "" : item.scope === "chat" ? opts.chatId : (opts.author || "").slice(0, 120);
    await addNote(opts.env, agentId, item.scope, key, item.content, "auto");
    known.push(item.content);
    added.push(item.content);
  }

  if (added.length) await trimNotes(opts.env, agentId, opts.chatId, opts.author);
  return added;
}

/** 面板里手动加一条记忆。 */
export async function rememberManually(
  env: Env,
  agentId: number,
  scope: string,
  scopeKey: string,
  content: string,
): Promise<number> {
  const safeScope = ["user", "chat", "global"].includes(scope) ? scope : "global";
  return await addNote(env, agentId, safeScope, safeScope === "global" ? "" : scopeKey, content, "manual");
}

export const memoryStamp = (): number => nowSec();
