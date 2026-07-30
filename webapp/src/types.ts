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
