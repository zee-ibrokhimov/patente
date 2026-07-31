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
  /** The language the explanation is actually in — Uzbek falls back to Russian. */
  explanation_lang: string | null;
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
  /** The language the text is ACTUALLY in. Uzbek falls back to Russian. */
  explanation_lang: string | null;
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
  /** True once the user has actually bought something. Distinguishes a paying subscriber
   *  from someone on the free trial, who also has a pass. */
  purchased: boolean;
  /** A sitting left open on the server. Without this the client only knew about one it
   *  had watched the user leave in the same page load — so closing the app mid-exam lost
   *  it, while the clock kept running. */
  open_session_id: number | null;

  /** The single question every paid surface should ask. `has_pass` is only ONE of the
   *  three ways to have it — reading that instead showed paywalls to channel members
   *  the server was already serving in full. */
  premium: boolean;
  premium_via: "pass" | "channel" | "staff" | "none";

  /** Where to send someone who wants to pay. Telegram does not tell a Mini App which bot
   *  opened it, so without this the buy button has nowhere to go. */
  bot_username: string;
  support_contact: string;
  /** A Tribute trial with a card attached, as opposed to a hand-granted pass. */
  trialing: boolean;
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
  /** Which ordinals already have an answer, so a resumed sitting can paint its answer
   *  sheet. Ordinals only — never the answers themselves, which would breach "an exam
   *  reveals nothing until it is over". Absent on older responses, hence optional. */
  answered_ordinals?: number[];
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
  /** The question itself, so a failed exam can be reviewed rather than merely counted. */
  statement: string;
  stem: string | null;
  answer: boolean | null;
  image: string | null;
  translation: string | null;
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

/** --- vocabulary trainer -------------------------------------------------- */

export type VocabDirection = "it_to_lang" | "lang_to_it";

/** One thing to type an answer to.
 *
 *  Deliberately carries NO expected answer: the server grades, so the paper cannot be
 *  read out of the network tab. `answer_lang` is which language the reply must be in,
 *  so the placeholder and the keyboard hint can be right. */
export interface VocabItem {
  term_id: number;
  direction: VocabDirection;
  prompt: string;
  answer_lang: string;
}

export interface VocabRound {
  lang: string;
  size: number;
  items: VocabItem[];
}

export type VocabVerdict = "correct" | "almost" | "wrong";

export interface VocabAnswer {
  term_id: number;
  verdict: VocabVerdict;
  /** Sent for every verdict, including a wrong one — the moment after committing to an
   *  answer is the moment the learner was going to learn from. */
  expected: string;
  /** For `almost`: the exact form they nearly typed. */
  correction: string | null;
  it: string;
  gloss: string;
  box: number;
}

export interface VocabTerm {
  id: number;
  rank: number;
  it: string;
  gloss: string;
  /** 0 when never answered. */
  box: number;
}

export interface VocabList {
  lang: string;
  total: number;
  offset: number;
  terms: VocabTerm[];
}

export interface VocabStats {
  total: number;
  started: number;
  learned: number;
  almost: number;
}

/** What a progress reset would destroy. */
export interface ResetPreview {
  answers: number;
  questions: number;
  sittings: number;
  words: number;
}
