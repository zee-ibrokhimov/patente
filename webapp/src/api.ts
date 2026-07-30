/** HTTP client for /webapp/*. No business logic lives here or anywhere else in this
 *  directory — plan §14.2: `bot/` and `webapp/` contain no business logic and no DB
 *  access. Both call the API. */

import { tg } from "./telegram";
import type {
  AnswerResult,
  ExplanationResult,
  Me,
  Question,
  QuestionTranslation,
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

  figureUrl: (image: string) => `${BASE}/figures/${image.replace(/^images\//, "")}`,
};
