/**
 * AI 能力：调模型、拼上下文、自我认知。
 *
 * 与 Python 版的关键差异：这里没有 urllib 的代理配置，全部走平台出口；
 * 也没有线程，耗时操作只能用 await，因此把「记忆提炼」放到 waitUntil 里
 * 异步做，不阻塞回复。
 */

import { getSetting, type AgentRow, type Env, type MessageRow } from "./db";

export class AiError extends Error {}

export interface AgentConfig {
  baseUrl: string;
  apiKey: string;
  model: string;
  systemPrompt: string;
  temperature: number;
  maxTokens: number;
  historyLen: number;
  minInterval: number;
}

/** 把用户填的接口地址收拾成「到 /v1 为止」。 */
export function normalizeBase(baseUrl: string): string {
  let base = (baseUrl || "").trim().replace(/\/+$/, "");
  for (const suffix of ["/chat/completions", "/completions", "/models", "/embeddings", "/responses"]) {
    if (base.endsWith(suffix)) {
      base = base.slice(0, -suffix.length).replace(/\/+$/, "");
    }
  }
  return base;
}

function clamp(value: number, lo: number, hi: number, fallback: number): number {
  if (!Number.isFinite(value)) return fallback;
  return Math.min(Math.max(value, lo), hi);
}

/** 智能体优先，没绑智能体时回退到全局老配置。 */
export function agentConfig(agent: AgentRow | null, settings: Record<string, string>): AgentConfig {
  const s = settings;
  const pick = (own: unknown, key: string, fallback: string): string => {
    const value = own === null || own === undefined ? "" : String(own).trim();
    return value || s[key] || fallback;
  };
  return {
    baseUrl: normalizeBase(pick(agent?.base_url, "ai_base_url", "https://api.deepseek.com/v1")),
    apiKey: pick(agent?.api_key, "ai_api_key", ""),
    model: pick(agent?.model, "ai_model", "deepseek-chat"),
    systemPrompt: pick(agent?.system_prompt, "ai_system_prompt", ""),
    temperature: clamp(Number(agent?.temperature ?? s.ai_temperature ?? 0.7), 0, 2, 0.7),
    maxTokens: Math.round(clamp(Number(agent?.max_tokens ?? s.ai_max_tokens ?? 800), 64, 4096, 800)),
    historyLen: Math.round(clamp(Number(agent?.history_len ?? s.ai_history_len ?? 12), 0, 40, 12)),
    minInterval: Math.round(clamp(Number(agent?.min_interval ?? s.ai_min_interval ?? 3), 0, 60, 3)),
  };
}

/**
 * 自我认知：把名字写进系统提示，模型才知道「我叫什么」。
 * 空名和默认占位名不注入，免得它张口就自称「未命名智能体」。
 */
export function selfIdentity(agent: AgentRow | null): string {
  const name = (agent?.name || "").trim();
  if (!name || name === "未命名智能体" || name === "默认机器人") return "";
  return `你的名字叫「${name}」。当别人问你是谁、你叫什么名字、你叫什么时，要如实回答这个名字，`
    + "不要自称 AI 助手、大模型、语言模型或任何其他身份。";
}

export interface BuildOptions {
  agent: AgentRow | null;
  cfg: AgentConfig;
  author: string;
  isPrivate: boolean;
  text: string;
  history: MessageRow[];
  memoryBlock?: string;
}

/**
 * 消息序列：自我认知（最高优先级）→ 人设 → 长期记忆 → 最近对话 → 这一句。
 * 顺序是有意为之：命名后必须先知道自己叫啥，再谈性格。
 */
export function buildMessages(opts: BuildOptions): Array<{ role: string; content: string }> {
  const messages: Array<{ role: string; content: string }> = [];
  const identity = selfIdentity(opts.agent);
  if (identity) messages.push({ role: "system", content: identity });
  if (opts.cfg.systemPrompt) messages.push({ role: "system", content: opts.cfg.systemPrompt });
  if (opts.memoryBlock) messages.push({ role: "system", content: opts.memoryBlock });

  for (const item of opts.history) {
    if (item.role !== "user" && item.role !== "assistant") continue;
    const content = (item.content || "").trim();
    if (!content) continue;
    if (item.role === "user" && item.author && !opts.isPrivate) {
      messages.push({ role: "user", content: `${item.author}：${content}` });
    } else {
      messages.push({ role: item.role, content });
    }
  }

  const prefix = opts.isPrivate ? "" : `${opts.author}：`;
  messages.push({ role: "user", content: prefix + opts.text });
  return messages;
}

function rateLimitHint(detail: string): string {
  const lowered = detail.toLowerCase();
  if (lowered.includes("tpm") || (lowered.includes("token") && lowered.includes("limit"))) {
    return "接口限流（429）：这一分钟的 token 用量超了。等 1 分钟再试，或把「最大回复长度」调小。";
  }
  if (lowered.includes("rpm") || lowered.includes("request")) {
    return "接口限流（429）：这一分钟的请求次数超了。等 1 分钟再试。";
  }
  return "接口限流（429）：调用太频繁了。等 1 分钟再试，或换一个额度更大的模型。";
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * 调兼容 OpenAI 的 chat/completions。
 * 429 与 5xx 重试；401/402/404 直接翻译成人话，重试没有意义。
 */
export async function callAi(
  cfg: AgentConfig,
  messages: Array<{ role: string; content: string }>,
  timeoutMs = 45000,
): Promise<string> {
  if (!cfg.baseUrl) throw new AiError("请填写 AI 接口地址（例如 https://api.deepseek.com/v1）");
  if (!cfg.apiKey) throw new AiError("请填写 AI API Key");
  if (!cfg.model) throw new AiError("请填写模型名（例如 deepseek-chat）");

  const url = `${cfg.baseUrl}/chat/completions`;
  const body = JSON.stringify({
    model: cfg.model,
    messages,
    max_tokens: cfg.maxTokens,
    temperature: cfg.temperature,
    stream: false,
  });

  let lastHttp: { code: number; text: string } | null = null;
  let lastNet: string | null = null;
  const delays = [1500, 4000];

  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${cfg.apiKey}`,
          "User-Agent": "tg-checkin-cf/1.0",
        },
        body,
        signal: AbortSignal.timeout(timeoutMs),
      });

      if (!response.ok) {
        const text = (await response.text()).slice(0, 400);
        if (response.status === 401) throw new AiError("AI 接口鉴权失败，请检查 API Key");
        if (response.status === 402) throw new AiError(`账户余额不足（HTTP 402）：${text.slice(0, 160)}`);
        if (response.status === 404) {
          throw new AiError(
            `AI 接口地址或模型名有误（HTTP 404）：${text.slice(0, 200)}。`
            + "确认 base_url 填到 /v1 为止、模型名拼写正确。",
          );
        }
        lastHttp = { code: response.status, text };
        lastNet = null;
        const retryable = response.status === 429 || response.status >= 500;
        if (!retryable || attempt === 2) break;
        const retryAfter = Number(response.headers.get("retry-after") || 0);
        await sleep(Math.max(delays[attempt], Math.min(retryAfter * 1000, 8000)));
        continue;
      }

      const data = (await response.json()) as {
        choices?: Array<{ message?: { content?: string } }>;
      };
      const text = data?.choices?.[0]?.message?.content;
      if (typeof text !== "string") {
        throw new AiError(`AI 返回格式不符合预期：${JSON.stringify(data).slice(0, 200)}`);
      }
      return text.trim();
    } catch (error) {
      if (error instanceof AiError) throw error;
      const detail = error instanceof Error ? error.message : String(error);
      lastNet = detail;
      lastHttp = null;
      if (attempt < 2) await sleep(delays[attempt]);
    }
  }

  if (lastHttp) {
    if (lastHttp.code === 429) throw new AiError(rateLimitHint(lastHttp.text));
    throw new AiError(`AI 接口返回 HTTP ${lastHttp.code}：${lastHttp.text.slice(0, 200)}`);
  }
  throw new AiError(
    `无法连接 AI 接口：${lastNet || "未知错误"}。请确认这个地址在 Cloudflare 网络上可达，`
    + "常见可用的是 https://api.deepseek.com/v1、https://api.siliconflow.cn/v1、https://openrouter.ai/api/v1。",
  );
}

/** 用一句最短的话验证配置是否可用。 */
export async function testAgent(
  cfg: AgentConfig,
  agent: AgentRow | null,
): Promise<{ ok: boolean; reply: string; model: string; latency_ms: number }> {
  const started = Date.now();
  const identity = selfIdentity(agent);
  const messages: Array<{ role: string; content: string }> = [];
  if (identity) messages.push({ role: "system", content: identity });
  if (cfg.systemPrompt) messages.push({ role: "system", content: cfg.systemPrompt });
  messages.push({ role: "user", content: "用一句话打个招呼，并说出你自己的名字。" });
  const reply = await callAi(cfg, messages, 30000);
  return { ok: true, reply, model: cfg.model, latency_ms: Date.now() - started };
}

/** 拉取模型列表，面板上可以点一下看看这个 Key 能用哪些模型。 */
export async function listModels(
  settings: Record<string, string>,
  baseUrl: string,
  apiKey: string,
): Promise<{ ok: boolean; models: string[]; error?: string }> {
  const base = normalizeBase(baseUrl || settings.ai_base_url || "");
  const key = (apiKey || settings.ai_api_key || "").trim();
  if (!base) return { ok: false, models: [], error: "请先填写接口地址" };
  if (!key) return { ok: false, models: [], error: "请先填写 API Key" };
  try {
    const response = await fetch(`${base}/models`, {
      headers: { Authorization: `Bearer ${key}` },
      signal: AbortSignal.timeout(20000),
    });
    if (!response.ok) {
      return { ok: false, models: [], error: `HTTP ${response.status}：${(await response.text()).slice(0, 160)}` };
    }
    const data = (await response.json()) as { data?: Array<{ id?: string }> };
    const models = (data?.data || [])
      .map((item) => String(item.id || "").trim())
      .filter(Boolean)
      .sort();
    return { ok: true, models };
  } catch (error) {
    return { ok: false, models: [], error: error instanceof Error ? error.message : String(error) };
  }
}

export const memoryEnabled = async (env: Env): Promise<boolean> =>
  (await getSetting(env, "memory_enabled", "1")) === "1";
