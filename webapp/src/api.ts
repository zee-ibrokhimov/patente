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
  AdminButton,
  AdminOverview,
  AdminPayments,
  AdminPerson,
  AdminReport,
  AdminSpend,
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

  /** A round of FLIP CARDS. Carries the answer, unlike `round` — here revealing it is the
   *  interaction, and fetching it per flip would put a network round trip between a tap
   *  and the thing the learner tapped for. */
  cards: () => request<VocabRound>("/vocab/cards"),

  /** "I knew it" / "I didn't", from a card. Self-graded, so no text is sent: the learner
   *  typed nothing, and inventing an answer would put it into the grading path. */
  recall: (termId: number, knew: boolean) =>
    request<{ term_id: number; box: number; knew: boolean }>("/vocab/recall", {
      method: "POST",
      body: JSON.stringify({ term_id: termId, knew }),
    }),

  /** Outside the paywall, so it can be shown to someone deciding whether to buy. */
  stats: () => request<VocabStats>("/vocab/stats"),
};

/** --- quiz sessions ------------------------------------------------------ */

/** The owner's console. Every call 404s for anyone who is not staff — the server decides,
 *  and this client never gates anything itself. */
export const admin = {
  overview: () => request<AdminOverview>("/admin/overview"),
  /** `segment` reuses the same four the group grant targets, so the list and the grant can
   *  never disagree about who is in one. Empty means everybody. */
  users: (q = "", segment = "") =>
    request<{ users: AdminUser[]; segment: string; total: number }>(
      `/admin/users?q=${encodeURIComponent(q)}&segment=${encodeURIComponent(segment)}`),
  grant: (chatId: number, days: number, reason: string, notify: boolean,
          amountCents = 0) =>
    request<{ pass_expires_at: string }>(`/admin/users/${chatId}/grant`, {
      method: "POST",
      body: JSON.stringify({ days, reason, notify, amount_cents: amountCents }),
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
  previewBroadcast: (body: {
    text: string; lang: string | null; premium_only: boolean; segment?: string;
  }) =>
    request<{ recipients: number }>("/admin/broadcast/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  broadcast: (body: {
    text: string; lang: string | null; premium_only: boolean;
    label: string; confirm_recipients: number; segment?: string;
    photo_url?: string | null;
    /** Up to three. `{text, webapp: true}` opens the Mini App in place, which is what makes
     *  an offer one tap from the paywall instead of a trip through a browser. */
    buttons?: AdminButton[];
  }) => request<{ queued: number }>("/admin/broadcast", {
    method: "POST",
    body: JSON.stringify(body),
  }),
  history: () => request<{ sent: BroadcastSent[] }>("/admin/broadcast/history"),

  deleteLink: (code: string) =>
    request<{ deleted: string }>(`/admin/links/${encodeURIComponent(code)}`, {
      method: "DELETE",
    }),
  deleteUser: (chatId: number) =>
    request<{ deleted: number; purchases_kept: number }>(`/admin/users/${chatId}`, {
      method: "DELETE",
    }),

  /** Everything about one person, for the moment they message "I paid". */
  person: (chatId: number) => request<AdminPerson>(`/admin/users/${chatId}`),
  /** What has been paid, and by whom. Individual payments were visible nowhere. */
  payments: () => request<AdminPayments>("/admin/payments"),
  /** Write explanations for the clusters covering the most questions. */
  generateContent: (count: number) =>
    request<{ started: number; covers_questions: number }>(
      `/admin/content/generate?count=${count}`, { method: "POST" }),
  /** How far the running batch has got. In memory server-side, so it resets on a deploy —
   *  which is right, because the work resets with it. */
  contentProgress: () =>
    request<{ total: number; done: number; running: boolean }>(
      "/admin/content/progress"),
  /** Tokens, not euros — prices change and are per-model. */
  spend: () => request<AdminSpend>("/admin/spend"),

  /** End or shorten somebody's access — the only way down that does not delete them. */
  revoke: (chatId: number, body: { mode: "end" | "shorten"; days?: number }) =>
    request<{ pass_expires_at: string; previous: string }>(
      `/admin/users/${chatId}/revoke`, { method: "POST", body: JSON.stringify(body) }),

  /** Give the same days to a whole segment — the way to reward people who actually use it.
   *  Count first, always: the server refuses a grant whose confirmed number does not match
   *  what it just reported, because access cannot be taken back once somebody has been told
   *  they have it. */
  previewGrantMany: (body: { segment: string; days: number; within_days: number }) =>
    request<{ recipients: number; capped_at: number }>("/admin/grant-many/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  grantMany: (body: {
    segment: string; days: number; within_days: number;
    reason: string; notify: boolean; confirm_recipients: number;
  }) => request<{ granted: number; segment: string; days: number }>("/admin/grant-many", {
    method: "POST",
    body: JSON.stringify(body),
  }),

  /** What learners have told you is wrong. Collected since launch and, until now, read by
   *  nothing. */
  reports: (unresolved = true) =>
    request<{ reports: AdminReport[]; open: number }>(
      `/admin/reports?unresolved=${unresolved}`),
  resolveReport: (id: number) =>
    request<{ id: number }>(`/admin/reports/${id}/resolve`, { method: "POST" }),
  regenerateReported: (id: number) =>
    request<{ id: number; outcome: string; explanation: string | null }>(
      `/admin/reports/${id}/regenerate`, { method: "POST" }),
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

  /** Prepare a slice of the paper: translations, and explanations for their clusters.
   *
   *  `wait` decides whether the server answers when the work is DONE or when it has merely
   *  been queued. The loading screen needs the former, and without it there is nothing to
   *  wait FOR — the response used to come back in milliseconds whatever the model was
   *  doing, so the loading screen was racing a promise rather than the work. The rolling
   *  top-ups mid-quiz want the latter: the learner is reading, and must not be blocked. */
  prefetch: (id: number, fromOrdinal: number, count = 5, wait = false) =>
    request<{ questions: number; ready: number; pending: number; waited: boolean }>(
      `/sessions/${id}/prefetch`,
      {
        method: "POST",
        body: JSON.stringify({ from_ordinal: fromOrdinal, count, wait }),
      },
    ),

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
