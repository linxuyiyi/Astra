/** Group-chat @mention parsing shared by the composer and its unit tests. */

export interface MentionCandidate {
  id: string;
  name: string;
  avatar?: string;
}

export interface MentionTrigger {
  /** Index of the ``@`` character. */
  start: number;
  /** Text typed after ``@`` before the caret. */
  query: string;
}

const TRIGGER = /(^|[\s，,。！？!?：:、（(【「『《])@([^\s@]{0,32})$/u

/**
 * Return the active mention token ending at the caret.
 *
 * The ``@`` must start a word: ``hello@example`` is an email, not a mention.
 * Whitespace after ``@`` ends the token so a sent sentence does not keep the
 * picker open.
 */
export function mentionTrigger(text: string, caret: number): MentionTrigger | null {
  const before = text.slice(0, Math.max(0, Math.min(caret, text.length)))
  const match = TRIGGER.exec(before)
  if (!match) return null
  return { start: match.index + match[1].length, query: match[2] }
}

/** Filter and rank group employees by the current query. */
export function mentionCandidates(
  members: MentionCandidate[],
  query: string,
): MentionCandidate[] {
  const needle = query.trim().toLowerCase()
  const unique = new Map<string, MentionCandidate>()
  for (const member of members) {
    const key = member.name.trim().toLowerCase()
    if (!key || unique.has(key)) continue
    unique.set(key, member)
  }
  return [...unique.values()]
    .filter(member => !needle || member.name.toLowerCase().includes(needle))
    .sort((left, right) => {
      const leftStarts = left.name.toLowerCase().startsWith(needle) ? 0 : 1
      const rightStarts = right.name.toLowerCase().startsWith(needle) ? 0 : 1
      return leftStarts - rightStarts || left.name.localeCompare(right.name, 'zh-CN')
    })
}

/** Replace the active ``@query`` with ``@name `` and return the new caret. */
export function insertMention(
  text: string,
  trigger: MentionTrigger,
  name: string,
  caret: number,
): { text: string; caret: number } {
  const before = text.slice(0, trigger.start)
  const after = text.slice(Math.max(0, Math.min(caret, text.length)))
  const separator = after && /^\s/.test(after) ? '' : ' '
  const inserted = `@${name}${separator}`
  return { text: before + inserted + after, caret: before.length + inserted.length }
}
