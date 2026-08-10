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
  /** The language QUESTIONS are translated into. Null means "follow `lang`" — sent
   *  unresolved so the control can show which option is actually selected rather than
   *  inferring it from the interface language. */
  translation_lang: string | null;
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
  /** Hidden from the weekly league. The switch that makes showing real first names to
   *  other learners defensible. */
  leaderboard_opt_out: boolean;

  /** How many terms the glossary holds. Counted server-side and never written into a
   *  string: the headline used to read "1090 exam words" directly above "0 of 1104
   *  learned", because only one of the two numbers was ever real. */
  vocab_terms: number;
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

/** What a PRACTICE sitting draws from. Mirrors REPEAT_SOURCES in shared/constants.py.
 *
 *  Only the draw changes — a repeat round grades answers and moves the Leitner schedule
 *  exactly like any other practice, because the learner is genuinely studying. An exam
 *  ignores this outright: a simulator built from your own mistakes reports a score that
 *  means nothing. */
export type RepeatSource = "smart" | "wrong" | "correct";
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

/** --- where the marks are lost -------------------------------------------- */

export interface ErrorHeadline {
  /** NULL below `min_sample`. Not a placeholder for zero — the screen declining to put a
   *  percentage in front of somebody deciding whether to book a paid exam. */
  rate: number | null;
  sample: number;
  min_sample: number;
  lifetime_answers: number;
}

export interface FamilyStat {
  family: string;
  questions_in_bank: number;
  share: number;
  per_exam: number;
  answered: number;
  wrong: number;
  error_rate: number | null;
  enough: boolean;
  coverage: number;
  predicted_mistakes: number | null;
}

export interface Analysis {
  headline: ErrorHeadline;
  families: FamilyStat[];
  predicted_mistakes: number | null;
  /** What share of an exam that prediction speaks for. Without showing it, a number
   *  measured on a third of the paper reads as describing all of it. */
  predicted_covers: number;
  exam_questions: number;
  exam_max_errors: number;
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
  /** Streak freezes in hand. A freeze nobody can see protects nobody psychologically —
   *  the point of having one is knowing you are covered. */
  streak_freezes: number;
  /** The real bar: 27 of 30 correct. Without it a percentage means nothing. */
  pass_accuracy: number;
  exams: {
    taken: number;
    passed: number;
    avg_errors: number | null;
    recent: ExamHistory[];
  };
}

/** --- the weekly league --------------------------------------------------- */

/** One row. Deliberately the smallest thing that can be rendered.
 *
 *  No chat id and no username: this is the only payload in the product carrying one
 *  learner's data to another, and a first name plus a score cannot be used to FIND
 *  somebody. See api/services/leaderboard.py. */
export interface LeaderboardEntry {
  /** Null for a word the learner added — it has no place in the shared sheet. */
  rank: number | null;
  name: string | null;
  score: number;
  is_me: boolean;
}

export interface Leaderboard {
  week_start: string;
  /** Everyone ranked, not just those returned — so the client can tell a real competition
   *  from three people and say so rather than drawing a podium nobody can move on. */
  ranked: number;
  entries: LeaderboardEntry[];
  me: { rank: number | null; score: number; opted_out: boolean };
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
  /** Present only on a CARDS round. The typing round withholds it on purpose — there is
   *  nothing to read ahead when grading happens server-side. */
  answer?: string;
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
  /** Null for a word the learner added — it has no place in the shared sheet. */
  rank: number | null;
  it: string;
  gloss: string;
  /** 0 when never answered. */
  box: number;
  /** Their own addition. Nothing else in the list may be edited or deleted. */
  mine?: boolean;
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

/** --- the owner's console --------------------------------------------------
 *
 *  Every one of these is served only to a staff caller; the server decides, and this
 *  client never does. See api/routes/webapp_admin.py. */

/** How much of the bank is written. Both translations and explanations are generated on
 *  demand, so the content fills in whatever order users happen to wander through it — these
 *  are the only numbers that say how far that has got. */
export interface AdminContent {
  questions_total: number;
  translated: number;
  clusters_total: number;
  explained: number;
  /** How many QUESTIONS those rules answer. Reporting only the rule count understates the
   *  real position five-fold, because clusters are wildly uneven. */
  questions_covered: number;
  /** Written, then withheld by a gate: an ungroundable number, or the model arguing with
   *  the answer key. The only visible measure of how often generation misfires. */
  explanations_withheld: number;
  explanations_disputed: number;
}

export interface AdminOverview {
  content: AdminContent;
  users: number;
  premium: number;
  on_trial: number;
  paid_purchases: number;
  active_24h: number;
  sales_contact: string;
}

export interface AdminUser {
  /** Last event of any kind. From the log, because there is no last_seen column — and it
   *  is the only thing on a row that says whether this person is still here. */
  last_seen?: string | null;
  chat_id: number;
  name: string | null;
  lang: string;
  premium: boolean;
  pass_expires_at: string | null;
  source: string | null;
  created_at: string;
}

export interface AdminLink {
  code: string;
  label: string;
  trial_days: number;
  active: boolean;
  max_uses: number | null;
  uses: number;
  url: string;
  created_at: string;
}

export interface BroadcastSent {
  at: string;
  recipients: number;
  delivered: number;
  failed: number;
  label: string;
  preview: string;
}

/** One inline button under a newsletter. Exactly one of the three shapes is meaningful:
 *  `webapp` opens the Mini App in place, `chat` becomes a t.me link, `url` must be https. */
export interface AdminButton {
  text: string;
  webapp?: boolean;
  chat?: string;
  url?: string;
}

/** A learner's "this explanation is wrong", with the text it is about.
 *
 *  `statement` and `explanation` are joined in server-side on purpose: a report is only
 *  actionable next to the sentence being reported, and looking the cluster up by hand is
 *  the friction that leaves a queue unread. */
export interface AdminReport {
  id: number;
  chat_id: number;
  question_id: number;
  cluster_id: number | null;
  lang: string;
  statement: string;
  explanation: string | null;
  created_at: string;
  resolved_at: string | null;
}


/** One learner, in full. Nothing here is new data — it was all recorded already and none
 *  of it was reachable from the panel. */
export interface AdminPerson {
  chat_id: number;
  name: string | null;
  lang: string;
  created_at: string;
  source: string | null;
  last_seen: string | null;
  pass_expires_at: string | null;
  premium: boolean;
  /** WHY they have it. "The app locked me out" has a different answer for each. */
  premium_via: "pass" | "channel" | "staff" | "none";
  paid_cents: number;
  payments: {
    id: number; amount_cents: number; currency: string; tier: string;
    created_at: string; refunded_at: string | null;
    /** A hand sale rather than a Tribute subscription. They renew differently. */
    manual: boolean;
  }[];
  answers: number;
  exams: number;
  reports: number;
}

export interface AdminPayments {
  this_month_cents: number;
  all_time_cents: number;
  payments: {
    chat_id: number; name: string | null; amount_cents: number; currency: string;
    tier: string; created_at: string; refunded_at: string | null;
  }[];
}

/** Tokens rather than euros: prices change and are per-model, so a figure computed here
 *  from a hardcoded rate would be quietly wrong the first time the model does. */
export interface AdminSpend {
  this_month: { calls: number; tokens_in: number; tokens_out: number };
  all_time: { calls: number; tokens_in: number; tokens_out: number };
}
