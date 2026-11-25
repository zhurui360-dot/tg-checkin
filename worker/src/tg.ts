/**
 * Telegram Bot API 封装。
 *
 * Cloudflare 版没有 MTProto（Telethon），所以只有机器人一条路：
 * 消息靠 webhook 推进来，回复靠 sendMessage 推出去。好处是天然不需要
 * 常驻进程，也不需要保活。
 */

export class TgError extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.status = status;
  }
}

interface TgResponse {
  ok?: boolean;
  result?: unknown;
  description?: string;
  error_code?: number;
  parameters?: { retry_after?: number };
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** 调一次 Bot API，非 2xx 或 ok=false 都抛 TgError。 */
export async function tgApi<T = unknown>(
  token: string,
  method: string,
  payload: Record<string, unknown> = {},
  timeoutMs = 15000,
): Promise<T> {
  if (!token) throw new TgError("机器人 token 为空");
  const url = `https://api.telegram.org/bot${token}/${method}`;

  for (let attempt = 0; attempt < 2; attempt += 1) {
    let response: Response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      throw new TgError(`无法连接 Telegram：${error instanceof Error ? error.message : String(error)}`);
    }

    let data: TgResponse = {};
    try {
      data = (await response.json()) as TgResponse;
    } catch {
      data = {};
    }

    if (response.ok && data.ok !== false) {
      return data.result as T;
    }

    const retryAfter = Number(data.parameters?.retry_after || 0);
    if (response.status === 429 && retryAfter > 0 && attempt === 0) {
      await sleep(Math.min(retryAfter * 1000, 5000));
      continue;
    }

    const description = data.description || `HTTP ${response.status}`;
    throw new TgError(friendlyTgError(description), response.status);
  }

  throw new TgError("Telegram 接口调用失败");
}

/** 把 Telegram 的报错翻成面板上能看懂的人话。 */
export function friendlyTgError(description: string): string {
  const text = (description || "").toLowerCase();
  if (text.includes("unauthorized") || text.includes("not found")) {
    return "机器人 token 无效或已被删除，请到 @BotFather 重新复制";
  }
  if (text.includes("chat not found")) {
    return "找不到这个会话：机器人可能没被拉进群，或群 ID 填错了（群 ID 是 -100 开头的数字）";
  }
  if (text.includes("bot was blocked")) return "对方把机器人拉黑了";
  if (text.includes("not enough rights") || text.includes("chat_write_forbidden")) {
    return "机器人在这个群里没有发言权限，请给它管理员或允许发言";
  }
  if (text.includes("message is too long")) return "消息超过 4096 字，已截断";
  if (text.includes("webhook")) return `设置 webhook 失败：${description}`;
  return description;
}

export interface TgMe {
  id: number;
  first_name?: string;
  username?: string;
  can_join_groups?: boolean;
  can_read_all_group_messages?: boolean;
  can_connect_to_business?: boolean;
}

export async function getMe(token: string): Promise<TgMe> {
  return await tgApi<TgMe>(token, "getMe", {}, 12000);
}

export async function sendMessage(
  token: string,
  chatId: string | number,
  text: string,
  options: { replyTo?: number; businessConnectionId?: string } = {},
): Promise<void> {
  const payload: Record<string, unknown> = {
    chat_id: chatId,
    text: text.slice(0, 4000),
    disable_web_page_preview: true,
  };
  if (options.replyTo) payload.reply_to_message_id = options.replyTo;
  if (options.businessConnectionId) payload.business_connection_id = options.businessConnectionId;
  await tgApi(token, "sendMessage", payload, 20000);
}

export async function setWebhook(
  token: string,
  url: string,
  secretToken: string,
): Promise<unknown> {
  return await tgApi(token, "setWebhook", {
    url,
    secret_token: secretToken,
    allowed_updates: ["message", "edited_message", "my_chat_member", "business_connection", "business_message"],
    drop_pending_updates: true,
    max_connections: 40,
  }, 20000);
}

export async function deleteWebhook(token: string): Promise<unknown> {
  return await tgApi(token, "deleteWebhook", { drop_pending_updates: false }, 15000);
}

export interface WebhookInfo {
  url?: string;
  pending_update_count?: number;
  last_error_message?: string;
  last_error_date?: number;
}

export async function getWebhookInfo(token: string): Promise<WebhookInfo> {
  return await tgApi<WebhookInfo>(token, "getWebhookInfo", {}, 12000);
}

export interface BusinessConnection {
  id: string;
  user?: { id: number; first_name?: string; last_name?: string; username?: string };
  user_chat_id?: number;
  is_enabled?: boolean;
  can_reply?: boolean;
}

export async function getBusinessConnection(token: string, id: string): Promise<BusinessConnection> {
  return await tgApi<BusinessConnection>(token, "getBusinessConnection", { business_connection_id: id }, 12000);
}

// ---------------- 消息解析 ----------------

export interface TgUser {
  id?: number;
  is_bot?: boolean;
  first_name?: string;
  last_name?: string;
  username?: string;
}

export interface TgChat {
  id: number;
  type?: string;
  title?: string;
  username?: string;
  first_name?: string;
  last_name?: string;
}

export interface TgMessage {
  message_id?: number;
  from?: TgUser;
  chat?: TgChat;
  text?: string;
  caption?: string;
  business_connection_id?: string;
  reply_to_message?: TgMessage;
}

export function chatTitle(message: TgMessage): string {
  const chat = message.chat || ({} as TgChat);
  return (
    chat.title
    || [chat.first_name, chat.last_name].filter(Boolean).join(" ")
    || chat.username
    || String(chat.id ?? "")
  );
}

export function authorName(message: TgMessage): string {
  const sender = message.from || {};
  return (
    [sender.first_name, sender.last_name].filter(Boolean).join(" ")
    || sender.username
    || String(sender.id ?? "某人")
  );
}

export function messageText(message: TgMessage): string {
  return (message.text || message.caption || "").trim();
}

export interface ReplyDecision {
  should: boolean;
  text: string;
  reason: string;
}

/**
 * 判断这条消息要不要回。
 * 触发方式：mention（被 @ 或被回复）/ all（群里每句都接）/ private（只回私聊）。
 */
export function shouldReply(message: TgMessage, botUsername: string, trigger: string): ReplyDecision {
  const text = messageText(message);
  if (!text) return { should: false, text: "", reason: "没有文本内容（贴纸/图片/文件不处理）" };

  const sender = message.from || {};
  if (sender.is_bot) return { should: false, text: "", reason: "发送者是机器人，忽略" };

  const isPrivate = message.chat?.type === "private";
  if (isPrivate) return { should: true, text, reason: "私聊" };

  if (trigger === "private") {
    return { should: false, text: "", reason: "触发方式设为「只回私聊」，群消息跳过" };
  }
  if (trigger === "all") return { should: true, text, reason: "触发方式为「群里每句都回」" };

  const lowered = text.toLowerCase();
  const mentioned = Boolean(botUsername) && lowered.includes(`@${botUsername.toLowerCase()}`);
  const replySender = message.reply_to_message?.from || {};
  const repliedToMe = Boolean(botUsername)
    && String(replySender.username || "").toLowerCase() === botUsername.toLowerCase();

  if (!mentioned && !repliedToMe) {
    return { should: false, text: "", reason: "群里没有 @ 你、也不是回复你的消息" };
  }

  const cleaned = botUsername
    ? text.replace(new RegExp(`@${botUsername}`, "gi"), "").trim()
    : text;
  return { should: true, text: cleaned || text, reason: mentioned ? "被 @ 提及" : "回复了你的消息" };
}

/** 群 ID 可能是 -100123 这种数字，也可能是 @channelname，统一成 Bot API 能吃的形态。 */
export function normalizeChat(value: string): string | number {
  const raw = (value || "").trim();
  if (!raw) return "";
  if (/^-?\d+$/.test(raw)) return Number(raw);
  return raw.startsWith("@") ? raw : `@${raw.replace(/^https?:\/\/t\.me\//, "")}`;
}
