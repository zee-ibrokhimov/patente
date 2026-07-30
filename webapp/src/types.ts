/** Mirrors api/schemas.py. Kept hand-written and small rather than generated —
 *  the surface is nine endpoints and a generator would be more machinery than payload.
 *
 *  Every gated field arrives with a `*_state` saying which of shown / locked /
 *  unavailable / off / available applies, so this client never decides entitlement.
 *  See the module docstring in api/schemas.py. */

export type Access = "shown" | "locked" | "unavailable" | "off" | "available";

export interface Translation {
  lang: string;
  stem: string | null;
  statement: string;
}

export interface Question {
  id: number;
  quesito_id: number;
  topic_id: number;
  statement_it: string;
  stem_it: string | null;
  image: string | null;
  image_file_id: string | null;
  translation_state: Access;
  translation: Translation | null;
}

export interface AnswerResult {
  question_id: number;
  given: boolean;
  correct: boolean;
  correct_answer: boolean;
  box: number;
  due_at: string;
  explanation_state: Access;
  explanation: string | null;
  free_explanations_left: number;
}

export interface QuestionTranslation {
  question_id: number;
  translation_state: Access;
  translation: Translation | null;
}

export interface ExplanationResult {
  question_id: number;
  explanation_state: Access;
  explanation: string | null;
  free_explanations_left: number;
}

export interface Me {
  chat_id: number;
  lang: string;
  translations_on: boolean;
  pass_expires_at: string | null;
  has_pass: boolean;
  free_explanations_left: number;
  onboarded_at: string | null;
  created_at: string;
}

export interface TopicStat {
  topic_id: number;
  topic: string;
  questions_seen: number;
  answers_given: number;
  wrong: number;
  error_rate: number;
}

export interface Stats {
  questions_seen: number;
  questions_total: number;
  answers_given: number;
  wrong: number;
  error_rate: number;
  boxes: Record<string, number>;
  by_topic: TopicStat[];
}

/** --- quiz sessions ------------------------------------------------------ */

export type Mode = "exam" | "practice";
export type SessionState = "open" | "submitted" | "expired" | "abandoned";

export interface Session {
  id: number;
  mode: Mode;
  state: SessionState;
  started_at: string;
  /** Null in practice. The ONLY authority on when an exam ends. */
  expires_at: string | null;
  /** The server's clock at the moment it answered, so the client can measure its own
   *  offset once instead of trusting the device clock. */
  server_now: string;
  question_count: number;
  max_errors: number | null;
  answered: number;
  questions: Question[];
}

/** What an exam answer returns. Note what is absent: no verdict, no correct answer, no
 *  explanation. The server builds this from a whitelist. */
export interface ExamAnswer {
  session_id: number;
  ordinal: number;
  answered: number;
  remaining: number;
}

export interface PracticeAnswer extends AnswerResult {
  session_id: number;
  ordinal: number;
  answered: number;
  remaining: number;
}

export interface SessionItem {
  ordinal: number;
  question_id: number;
  given: boolean | null;
  correct: boolean | null;
}

export interface SessionResults {
  session_id: number;
  mode: Mode;
  state: SessionState;
  started_at: string;
  finished_at: string | null;
  question_count: number;
  answered: number;
  wrong: number;
  max_errors: number | null;
  passed: boolean | null;
  items: SessionItem[];
}

/** --- profile ------------------------------------------------------------ */

export interface ExamHistory {
  id: number;
  finished_at: string | null;
  wrong: number;
  answered: number;
  question_count: number;
  passed: boolean | null;
  state: SessionState;
}

export interface Profile {
  streak_days: number;
  /** Null below `readiness_min_sample` answers. The server refuses to guess, and this
   *  client must render that refusal rather than treating null as zero. */
  readiness: number | null;
  readiness_sample: number;
  readiness_min_sample: number;
  /** The real bar: 27 of 30 correct. Without it a percentage means nothing. */
  pass_accuracy: number;
  exams: {
    taken: number;
    passed: number;
    avg_errors: number | null;
    recent: ExamHistory[];
  };
}
