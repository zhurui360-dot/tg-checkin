/**
 * 文本相关度：中文按字符二元组做重叠打分，配合新近度做小幅加权。
 *
 * 和 Python 版一致的思路——不引向量库。记忆条数被限制在百来条，
 * 这个算法在 Workers 上跑一次只要几十微秒，不会碰到 CPU 限制。
 */

export function normalize(text: string): string {
  return (text || "")
    .replace(/[\s，。！？、,.!?;；:："'“”‘’（）()\[\]【】]+/g, "")
    .toLowerCase();
}

export function bigrams(text: string): Set<string> {
  const clean = normalize(text);
  if (clean.length < 2) return new Set(clean ? [clean] : []);
  const out = new Set<string>();
  for (let i = 0; i < clean.length - 1; i += 1) out.add(clean.slice(i, i + 2));
  return out;
}

/** 两条记忆是不是同一件事（避免重复入库）。 */
export function isSimilar(a: string, b: string): boolean {
  const na = normalize(a);
  const nb = normalize(b);
  if (!na || !nb) return false;
  if (na === nb || na.includes(nb) || nb.includes(na)) return true;
  const ba = bigrams(a);
  const bb = bigrams(b);
  if (!ba.size || !bb.size) return false;
  let inter = 0;
  for (const g of ba) if (bb.has(g)) inter += 1;
  const union = ba.size + bb.size - inter;
  return union > 0 && inter / union >= 0.72;
}

/** 相关度：二元组覆盖率为主，id 越大（越新）略微加权。 */
export function relevance(query: string, content: string, id: number): number {
  const q = bigrams(query);
  if (!q.size) return Math.min(id, 100000) / 100000;
  const c = bigrams(content);
  let inter = 0;
  for (const g of q) if (c.has(g)) inter += 1;
  const overlap = inter / q.size;
  return overlap * 10 + Math.min(id, 100000) / 100000;
}

export interface NoteLike {
  id: number;
  scope: string;
  content: string;
}

/**
 * 从候选池里挑最相关的几条，控制在字符预算内。
 * 返回挑选后的列表（已去重），调用方负责拼文案。
 */
export function pickNotes(
  pool: NoteLike[],
  query: string,
  author: string,
  limit: number,
  budget: number,
): NoteLike[] {
  if (!pool.length) return [];
  const ranked = [...pool].sort(
    (a, b) => relevance(query, b.content || "", b.id) - relevance(query, a.content || "", a.id),
  );
  const picked: NoteLike[] = [];
  const seen = new Set<string>();
  let used = 0;
  const cap = Math.max(1, Math.min(limit, 20));
  for (const note of ranked) {
    const content = (note.content || "").trim();
    if (!content || seen.has(content)) continue;
    if (used + content.length > budget) continue;
    picked.push({ ...note, content });
    seen.add(content);
    used += content.length;
    if (picked.length >= cap) break;
  }
  return picked;
}
