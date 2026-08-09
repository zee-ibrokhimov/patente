/** HTTP client for /webapp/*. No business logic lives here or anywhere else in this
 *  directory — plan §14.2: `bot/` and `webapp/` contain no business logic and no DB
 *  access. Both call the API. */

import { tg } from "./telegram";
import type {
  AnswerResult,
  ExamAnswer,
  ExplanationResult,
  Me,
  Mode,
  PracticeAnswer,
  Profile,
  Question,
  AdminLink,
  AdminOverview,
  AdminUser,
  BroadcastSent,
  Leaderboard,
  QuestionTranslation,
  RepeatSource,
  ResetPreview,
  Session,
  SessionResults,
  Stats,
  VocabAnswer,
  VocabDirection,
  VocabList,
  VocabRound,
  VocabStats,
} from "./types";

const BASE = "/webapp";

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  // Sent verbatim. See the note in telegram.ts — the HMAC covers these exact bytes.
  headers.set("X-Telegram-Init-Data", tg?.initData ?? "");
  if (init.body) headers.set("Content-Type", "application/json");

  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json())?.detail ?? detail;
    } catch {
      /* a non-JSON error body is still an error; keep the status text */
    }
    throw new ApiError(res.status, detail);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

export const api = {
  me: () => request<Me>("/me"),

  settings: (body: { lang?: string; translations_on?: boolean; leaderboard_opt_out?: boolean }) =>
    request<Me>("/settings", { method: "PATCH", body: JSON.stringify(body) }),

  nextQuestion: (opts: { topicId?: number; excludeId?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.topicId != null) q.set("topic_id", String(opts.topicId));
    if (opts.excludeId != null) q.set("exclude_id", String(opts.excludeId));
    const qs = q.toString();
    return request<Question>(`/next-question${qs ? `?${qs}` : ""}`);
  },

  answer: (questionId: number, answer: boolean) =>
    request<AnswerResult>("/answers", {
      method: "POST",
      body: JSON.stringify({ question_id: questionId, answer }),
    }),

  /** Fetched after the Italian is already on screen, because it takes a few seconds
   *  on a cold cache and nobody waits that long in front of every question (§15). */
  translation: (questionId: number) =>
    request<QuestionTranslation>(`/questions/${questionId}/translation`, {
      method: "POST",
    }),

  /** Costs a call and possibly a lifetime taster, which is why it is POST and why it
   *  is only sent when the user actually asks. */
  explanation: (questionId: number) =>
    request<ExplanationResult>(`/questions/${questionId}/explanation`, {
      method: "POST",
    }),

  stats: () => request<Stats>("/stats"),

  /** "This explanation is wrong." The path existed server-side and was reachable only
   *  from the loopback route the bot used, so since drilling moved here nobody could
   *  report anything. */
  report: (questionId: number) =>
    request<unknown>("/reports", {
      method: "POST",
      body: JSON.stringify({ question_id: questionId }),
    }),

  /** What a reset would destroy — so the confirmation can name real numbers instead of
   *  saying "your progress", which people click past. */
  resetPreview: () => request<ResetPreview>("/reset/preview"),
  resetProgress: () => request<ResetPreview>("/reset", { method: "POST" }),

  profile: () => request<Profile>("/profile"),

  /** Static, not under /webapp — an <img src> cannot carry the initData header, so a
   *  figure behind that auth is a guaranteed 401. nginx serves these directly. */
  figureUrl: (image: string) => `/figures/${image.replace(/^images\//, "")}`,
};

/** --- vocabulary trainer -------------------------------------------------- */

export const vocab = {
  /** A round of terms to type, mixed in both directions. Carries prompts and no
   *  answers — grading is server-side, so there is nothing here to read ahead. */
  round: () => request<VocabRound>("/vocab/round"),

  answer: (termId: number, direction: VocabDirection, given: string) =>
    request<VocabAnswer>("/vocab/answer", {
      method: "POST",
      body: JSON.stringify({ term_id: termId, direction, given }),
    }),

  terms: (opts: { q?: string; offset?: number; limit?: number } = {}) => {
    const p = new URLSearchParams();
    if (opts.q) p.set("q", opts.q);
    if (opts.offset != null) p.set("offset", String(opts.offset));
    if (opts.limit != null) p.set("limit", String(opts.limit));
    const qs = p.toString();
    return request<VocabList>(`/vocab/terms${qs ? `?${qs}` : ""}`);
  },

  /** Outside the paywall, so it can be shown to someone deciding whether to buy. */
  stats: () => request<VocabStats>("/vocab/stats"),
};

/** --- quiz sessions ------------------------------------------------------ */

/** The owner's console. Every call 404s for anyone who is not staff — the server decides,
 *  and this client never gates anything itself. */
export const admin = {
  overview: () => request<AdminOverview>("/admin/overview"),
  users: (q: string) =>
    request<{ users: AdminUser[] }>(`/admin/users?q=${encodeURIComponent(q)}`),
  grant: (chatId: number, days: number, reason: string, notify: boolean) =>
    request<{ pass_expires_at: string }>(`/admin/users/${chatId}/grant`, {
      method: "POST",
      body: JSON.stringify({ days, reason, notify }),
    }),
  links: () => request<{ links: AdminLink[] }>("/admin/links"),
  createLink: (body: { code: string; label: string; trial_days: number; max_uses: number | null }) =>
    request<{ code: string }>("/admin/links", { method: "POST", body: JSON.stringify(body) }),
  updateLink: (code: string, body: { active?: boolean }) =>
    request<{ code: string }>(`/admin/links/${encodeURIComponent(code)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  message: (chatId: number, text: string) =>
    request<{ delivered: boolean }>("/admin/message", {
      method: "POST",
      body: JSON.stringify({ chat_id: chatId, text }),
    }),
  /** Count first, ALWAYS. The send refuses unless this number is echoed back, because a
   *  newsletter cannot be unsent and the population can change between the two calls. */
  previewBroadcast: (body: { text: string; lang: string | null; premium_only: boolean }) =>
    request<{ recipients: number }>("/admin/broadcast/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  broadcast: (body: {
    text: string; lang: string | null; premium_only: boolean;
    label: string; confirm_recipients: number;
  }) => request<{ queued: number }>("/admin/broadcast", {
    method: "POST",
    body: JSON.stringify(body),
  }),
  history: () => request<{ sent: BroadcastSent[] }>("/admin/broadcast/history"),
};

export const leaderboard = {
  /** This week's league. The only endpoint that returns other people. */
  board: () => request<Leaderboard>("/leaderboard"),
};

export const sessions = {
  /** Returns the sitting AND its whole paper. The paper is frozen server-side, so there
   *  is nothing to fetch per question and no round trip on a running clock. */
  start: (mode: Mode, source: RepeatSource = "smart") =>
    request<Session>("/sessions", {
      method: "POST",
      body: JSON.stringify({ mode, source }),
    }),

  /** Resume. The app persists nothing across a reopen, so this is how a backgrounded
   *  exam comes back — with the server's deadline, not a remembered one. */
  read: (id: number) => request<Session>(`/sessions/${id}`),

  answer: (id: number, ordinal: number, answer: boolean) =>
    request<ExamAnswer | PracticeAnswer>(`/sessions/${id}/answers`, {
      method: "POST",
      body: JSON.stringify({ ordinal, answer }),
    }),

  /** Practice only. Adds another batch to a sitting the learner has worked to the end
   *  of, and returns the sitting with its whole paper — including the new items. */
  extend: (id: number) =>
    request<Session>(`/sessions/${id}/extend`, { method: "POST" }),

  finish: (id: number) =>
    request<SessionResults>(`/sessions/${id}/finish`, { method: "POST" }),

  results: (id: number) => request<SessionResults>(`/sessions/${id}/results`),
};
