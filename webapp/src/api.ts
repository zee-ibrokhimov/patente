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
  QuestionTranslation,
  Session,
  SessionResults,
  Stats,
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

  settings: (body: { lang?: string; translations_on?: boolean }) =>
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

  profile: () => request<Profile>("/profile"),

  /** Static, not under /webapp — an <img src> cannot carry the initData header, so a
   *  figure behind that auth is a guaranteed 401. nginx serves these directly. */
  figureUrl: (image: string) => `/figures/${image.replace(/^images\//, "")}`,
};

/** --- quiz sessions ------------------------------------------------------ */

export const sessions = {
  /** Returns the sitting AND its whole paper. The paper is frozen server-side, so there
   *  is nothing to fetch per question and no round trip on a running clock. */
  start: (mode: Mode) =>
    request<Session>("/sessions", { method: "POST", body: JSON.stringify({ mode }) }),

  /** Resume. The app persists nothing across a reopen, so this is how a backgrounded
   *  exam comes back — with the server's deadline, not a remembered one. */
  read: (id: number) => request<Session>(`/sessions/${id}`),

  answer: (id: number, ordinal: number, answer: boolean) =>
    request<ExamAnswer | PracticeAnswer>(`/sessions/${id}/answers`, {
      method: "POST",
      body: JSON.stringify({ ordinal, answer }),
    }),

  finish: (id: number) =>
    request<SessionResults>(`/sessions/${id}/finish`, { method: "POST" }),

  results: (id: number) => request<SessionResults>(`/sessions/${id}/results`),
};
