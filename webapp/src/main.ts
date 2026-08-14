import { admin, api, ApiError, categories, leaderboard, sessions, vocab } from "./api";
import { readinessGauge } from "./gauge";
import { icons } from "./icons";
import { TRANSLATION_LANGUAGES, type Key, lang, setLang, t, tips } from "./i18n";
import {
  applyTheme,
  ask,
  haptic,
  inTelegram,
  initTelegram,
  openChat,
  preferredTheme,
  setBackButton,
  setTheme,
  tg,
} from "./telegram";
import type { Theme } from "./telegram";
import type {
  Analysis,
  AnswerResult,
  Coach,
  ExamAnswer,
  Me,
  Mode,
  PracticeAnswer,
  Profile,
  Question,
  ResetPreview,
  Session,
  SessionResults,
  Stats,
  VocabAnswer,
  VocabList,
  AdminLink,
  AdminButton,
  AdminOverview,
  AdminPerson,
  AdminReport,
  AdminSuggestion,
  Category,
  AdminUser,
  Leaderboard,
  RepeatSource,
  VocabRound,
  VocabStats,
  VocabTerm,
} from "./types";
import "./style.css";

type Screen = "home" | "run" | "results" | "profile" | "stats" | "settings" | "vocab"
  | "ratings" | "admin" | "analysis" | "subjects" | "practice";

/** The author of the vocabulary list, and the condition on which it may be used.
 *
 *  The glossary was compiled by Zukhriddin Kamolov, who gave permission to use it
 *  provided he is credited as its author. That makes the credit a TERM OF USE rather than a
 *  courtesy: if this constant stops being rendered, the app is using someone else's work
 *  outside the terms it was given under.
 *
 *  Here rather than in i18n.ts because a person's name is not a translatable string, and
 *  putting it in four locale blocks is four chances to get it wrong. Only the surrounding
 *  sentence is translated.
 */
const VOCAB_AUTHOR = { name: "Zukhriddin Kamolov", handle: "TTYMI_OKMK2" } as const;

/** Below this many ranked learners, a "league" is a handful of people in a fixed order
 *  where somebody is permanently last — demoralising rather than motivating. Mirrors
 *  LEADERBOARD_MIN_PLAYERS in shared/constants.py. The board is still shown; it is
 *  labelled as quiet rather than presented as a competition. */
const RATINGS_MIN_PLAYERS = 5;

/** How many questions ahead to keep prepared, and how long the start screen will wait.
 *
 *  Five is the window; the learner is topped back up to five ahead every time they advance,
 *  so the preparation stays in front of them for the whole sitting without ever fetching a
 *  paper they might abandon at question three.
 *
 *  The wait is capped because a slow model must delay a quiz by a second, never hold it
 *  hostage. Everything caches, and each card re-asks for its own translation regardless, so
 *  a miss costs one spinner rather than an unreadable question. */
const PREFETCH_WINDOW = 5;
// (PREFETCH_WAIT_MS is gone. It capped the loading screen at 2500ms against an endpoint
//  that answered in milliseconds without doing the work, so it never bounded anything real
//  — the wait now ends when the questions are ready.)

/** A sitting in flight.
 *
 *  `skew` is measured ONCE from the server's own clock at creation, and every countdown
 *  is rendered as `deadline - (Date.now() + skew)`. The device clock is not trusted for
 *  anything: phones are wrong, and a paid feature whose timer lives in the client is
 *  editable by anyone who can open devtools. The server enforces the deadline regardless
 *  — this only makes the display honest.
 */
interface Run {
  session: Session;
  index: number;
  answered: Set<number>;
  /** Practice only: the verdict for the question currently on screen. */
  verdict: AnswerResult | null;
  deadline: number | null;
  skew: number;
  busy: boolean;
  /** The highest ordinal already asked for. Stops every answer re-requesting a window
   *  that mostly overlaps the last one. */
  prefetchedTo: number;
  /** Fetching translations for a sitting that was started without them. Distinct from
   *  `busy`, which is an answer in flight: this one leaves the question readable and only
   *  covers the strip under it. */
  warming: boolean;
}

/** The vocabulary trainer's own state.
 *
 *  `current` is the graded verdict for the item on screen: its presence is what switches
 *  the card from "type your answer" to "here is how you did", so there is one source of
 *  truth for which half is showing rather than a separate boolean that can disagree. */
interface VocabRun {
  view: "test" | "cards" | "list";
  round: VocabRound | null;
  index: number;
  current: VocabAnswer | null;
  right: number;
  typed: string;
  /** The FLIP-CARD round, kept apart from `round` on purpose. Switching tabs mid-round
   *  would otherwise throw away whichever one you were not looking at, and the two are
   *  different exercises: typing is recall, flipping is recognition. */
  cards: VocabRound | null;
  cardIndex: number;
  /** Whether the card on screen is showing its back. The whole interaction. */
  flipped: boolean;
  /** How many the learner said they knew. Their own verdict, not a graded score — which
   *  is why the summary calls it differently from the typing round's. */
  knew: number;
  busy: boolean;
  list: VocabList | null;
  query: string;
  stats: VocabStats | null;
  /** Set when the API answers 402. Rendering a paywall is the honest response to a
   *  feature the user has not bought; pretending it is broken is not. */
  locked: boolean;
}

const state: {
  me: Me | null;
  screen: Screen;
  vocab: VocabRun;
  run: Run | null;
  results: SessionResults | null;
  stats: Stats | null;
  profile: Profile | null;
  /** A sitting the user walked away from. Held so the back button cannot lose an exam. */
  resumable: Session | null;
  /** Review list filter. Mistakes-first by default: someone who got 27 right does not
   *  want to scroll past them to reach the three that matter. */
  reviewWrongOnly: boolean;
  /** This week's league, fetched on entering the tab. Null means "not loaded yet", which
   *  is a different screen from an empty board. */
  ratings: Leaderboard | null;
  /** The seven subjects, ranked by the marks each is costing THIS learner. Loaded on
   *  entry, because the ranking is personal and a cached one goes stale as they study. */
  subjects: Category[] | null;
  /** Which family's ministerial topics are expanded. One at a time: seven families with
   *  every topic open is thirty-two rows and no shape. */
  openFamily: string | null;
  /** The owner's console. Loaded on entry and never at boot — it is one person's screen
   *  and everybody else would be paying for a request that 404s. */
  adminData: { overview: AdminOverview | null; users: AdminUser[]; links: AdminLink[];
               reports: AdminReport[]; openReports: number; userTotal: number;
               /** What learners asked for. The form has existed since the suggestions
                *  migration and nothing ever displayed what it collected — the inbox was
                *  written on the server and never opened. */
               suggestions: AdminSuggestion[]; openSuggestions: number;
               /** Which page of the panel. People is its own screen: finding one learner
                *  and acting on them is a different job from "how is the product doing",
                *  done at a different moment. */
               view: "home" | "people";
               query: string; segment: string; busy: boolean } | null;
  /** A quiz being prepared. Non-null only between tapping Start and the first question. */
  preparing: { mode: Mode; source: RepeatSource } | null;
  /** The error breakdown. Fetched when the screen opens, never on boot: it is one screen
   *  behind a tap and costs a query nobody has asked for otherwise. */
  analysis: Analysis | null;
  /** The AI advice. Separate from `analysis` because it is asked for, not loaded: it may
   *  cost money, so nothing fetches it on entering the screen. */
  coach: Coach | null;
  coachBusy: boolean;
} = { me: null, screen: "home", run: null, results: null, stats: null, profile: null,
      resumable: null, reviewWrongOnly: true, ratings: null, adminData: null,
      subjects: null, openFamily: null,
      preparing: null, analysis: null, coach: null, coachBusy: false,
      vocab: { view: "test", round: null, index: 0, current: null, right: 0, typed: "",
               cards: null, cardIndex: 0, flipped: false, knew: 0,
               busy: false, list: null, query: "", stats: null, locked: false } };

const root = document.getElementById("app")!;

/** textContent everywhere, never innerHTML with content from the API. Explanations are
 *  model-generated and translations come back from an LLM; neither is trusted markup. */
function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// ---------------------------------------------------------------------------
// errors that do not destroy the app
// ---------------------------------------------------------------------------

let toastTimer: number | undefined;

/** A failed request shows a toast and leaves the screen alone.
 *
 *  The previous version replaced the entire app with an error page whose Retry button
 *  called boot() — so one flaky POST in minute 12 of a timed exam wiped the exam. An
 *  in-flight sitting is the thing most worth protecting and was the thing least
 *  protected.
 */
function toast(message: string): void {
  document.querySelector(".toast")?.remove();
  const node = el("div", "toast", message);
  document.body.append(node);
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => node.remove(), 3200);
}

/** A toast with something to do in it.
 *
 *  Held longer than a plain one — a message you are expected to act on has to outlast the
 *  time it takes to read it — and dismissed by the action, so tapping Undo does not leave
 *  the strip sitting there claiming the word is still saved. */
function actionToast(message: string, sub: string, label: string,
                     act: () => void): void {
  document.querySelector(".toast")?.remove();
  // `ok`, because `.toast` is the ERROR toast — red, with a red shadow, since every
  // message it has ever carried was a failure. A confirmation in that colour reads as
  // something having gone wrong.
  const node = el("div", "toast toast-action ok");
  const text = el("span", "toast-text");
  text.append(el("span", "toast-title", message));
  if (sub) text.append(el("span", "toast-sub", sub));
  node.append(text);
  const button = el("button", "toast-btn", label);
  button.type = "button";
  button.onclick = () => { node.remove(); act(); };
  node.append(button);
  document.body.append(node);
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => node.remove(), 6000);
}

function reportError(err: unknown): void {
  if (err instanceof ApiError && err.status === 401) {
    toast(t("outside_telegram"));
    return;
  }
  toast(t("error"));
}

// ---------------------------------------------------------------------------
// the countdown
// ---------------------------------------------------------------------------

let timerNode: HTMLElement | null = null;
let ticking: number | undefined;

/** Updated IN PLACE, outside render().
 *
 *  render() calls replaceChildren() and rebuilds the DOM, re-assigning every <img src>.
 *  A 1 Hz countdown routed through it would reload the figure once a second and discard
 *  any in-flight button state. So the clock owns one retained node and touches nothing
 *  else.
 */
function startTicking(): void {
  window.clearInterval(ticking);
  ticking = window.setInterval(tick, 500);
  tick();
}

function stopTicking(): void {
  window.clearInterval(ticking);
  ticking = undefined;
  timerNode = null;
}

function remainingMs(): number {
  const run = state.run;
  if (!run?.deadline) return 0;
  return run.deadline - (Date.now() + run.skew);
}

function tick(): void {
  const run = state.run;
  if (!run?.deadline || !timerNode) return;
  const left = Math.max(0, remainingMs());
  const total = Math.floor(left / 1000);
  const mm = String(Math.floor(total / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  timerNode.textContent = `${mm}:${ss}`;
  timerNode.classList.toggle("warn", left <= 5 * 60_000 && left > 60_000);
  timerNode.classList.toggle("crit", left <= 60_000);

  if (left <= 0) {
    stopTicking();
    void finishRun(true);
  }
}

// ---------------------------------------------------------------------------
// running a sitting
// ---------------------------------------------------------------------------

async function startRun(mode: Mode, source: RepeatSource = "smart",
                        scope?: string): Promise<void> {
  // A loading screen, and it is not decoration.
  //
  // The first question of a cold paper needs a translation the learner cannot read without,
  // and fetching it after the question is on screen means they stare at Italian they do not
  // understand. A start screen is the one moment in a quiz where waiting is EXPECTED, so it
  // is the cheapest place to put the wait — and it is where the first five get prepared.
  state.preparing = { mode, source };
  render();
  try {
    const session = await sessions.start(mode, source, scope);
    // Prepare the opening five and WAIT for them, in ONE request.
    //
    // It used to race the request against a 2500ms timer, which could never have worked:
    // the endpoint handed every job to BackgroundTasks and answered immediately, so the
    // race was between a timer and a response that arrived in milliseconds having done
    // nothing. The loading screen always won, and the learner still met an untranslated
    // question one. Now the server waits for the work, and this waits for the server.
    //
    // One call for the whole window, not one per question. An earlier version issued five
    // so the screen could count "2 of 5"; a single round trip is what is wanted, and the
    // server prepares the window inside it under a concurrency bound.
    //
    // The trade is that nothing is knowable between sending and receiving, so the screen
    // shows an unlabelled wait instead of a counted one. Real progress needs either the
    // five calls back or a status endpoint to poll — it is not something the client can
    // honestly infer from one pending request, and a bar that moves on a timer would be a
    // guess dressed as information.
    const total = Math.min(PREFETCH_WINDOW, session.question_count);
    try {
      await sessions.prefetch(session.id, 1, total, true);
    } catch {
      /* a failed prefetch is a slower question, not a failed quiz */
    }
    state.preparing = null;
    enterRun(session);
  } catch (err) {
    state.preparing = null;
    // "Nothing to repeat yet" is a normal state, not a failure — a learner who has never
    // got one wrong asking for their mistakes. The generic red error toast would read as
    // a broken app, so it gets its own sentence.
    if (err instanceof ApiError && err.status === 409 && source !== "smart") {
      toast(source === "wrong" ? t("repeat_none_wrong") : t("repeat_none_correct"));
      return;
    }
    reportError(err);
  }
}

/** Throw away everything the server rendered in the OLD language.
 *
 *  Changing the language changes what the server returns, not just how the app labels it —
 *  and the vocabulary is the clearest case, because its glosses ARE the content:
 *  `vocab.pair_language(user)` picks the column from `user.lang`.
 *
 *  This used to clear `stats` and `profile` and stop there, so a learner who opened the
 *  word list in Uzbek and then switched the app to English kept the cached Uzbek list —
 *  `openVocab` spreads `...state.vocab`, which preserves `list`, `stats` and `query` while
 *  resetting the round. The result was an English interface listing `carreggiata → qatnov
 *  qismi`, reported from a real screenshot.
 *
 *  Clearing rather than refetching: the next screen that needs one asks for it, and a
 *  language change should not fire four requests for screens the user may not open.
 */
function dropLocalisedCaches(): void {
  state.stats = null;
  state.profile = null;
  state.ratings = null;
  state.vocab = {
    ...state.vocab,
    list: null,
    stats: null,
    round: null,
    current: null,
    query: "",
    index: 0,
    right: 0,
    typed: "",
  };
}

/** Keep PREFETCH_WINDOW questions prepared in front of the learner.
 *
 *  Fire and forget, and deduplicated: without `prefetchedTo` every answer would re-request
 *  a window that mostly overlaps the last one, which is a request per answer for work
 *  already done. The server would cache its way out of it, but the round trips are pure
 *  waste on a phone. */
function keepAhead(): void {
  const run = state.run;
  if (!run) return;
  const next = run.index + 2;                       // the ordinal after the one on screen
  const wanted = next + PREFETCH_WINDOW - 1;
  if (wanted <= run.prefetchedTo) return;
  if (next > run.session.question_count) return;

  const from = Math.max(next, run.prefetchedTo + 1);
  run.prefetchedTo = Math.min(wanted, run.session.question_count);
  void sessions.prefetch(run.session.id, from, PREFETCH_WINDOW).catch(() => {
    // Let it be retried on the next answer rather than swallowing the window for good.
    if (state.run) state.run.prefetchedTo = from - 1;
  });
}

function enterRun(session: Session): void {
  const serverNow = Date.parse(session.server_now);
  // Seed from the server rather than starting empty. The app persists nothing across a
  // reopen, so a resumed exam used to paint every circle blank — right question, right
  // clock, an answer sheet claiming nothing had been done. The sheet is the only progress
  // indicator on that screen, and the obvious response to it is to start over, which
  // abandons the sitting.
  const done = new Set(session.answered_ordinals ?? []);
  state.run = {
    session,
    warming: false,
    index: Math.min(session.answered, session.question_count - 1),
    answered: done,
    verdict: null,
    deadline: session.expires_at ? Date.parse(session.expires_at) : null,
    skew: serverNow - Date.now(),
    busy: false,
    // The opening window was prepared before this screen appeared.
    prefetchedTo: Math.min(PREFETCH_WINDOW, session.question_count),
  };
  state.resumable = null;
  state.screen = "run";
  render();
  if (state.run.deadline) startTicking();
}

async function submitAnswer(given: boolean): Promise<void> {
  const run = state.run;
  if (!run || run.busy) return;
  const ordinal = run.index + 1;
  if (run.answered.has(ordinal)) return;

  run.busy = true;
  // Show the tap landed. `run.busy` guarded against double-submits but changed nothing on
  // screen, so between tapping Vero and the answer coming back — a real wait on mobile
  // data — the button looked untouched and the natural response was to tap it again.
  // Disabling in place rather than calling render(): a re-render reassigns the figure's
  // src and makes the browser reload the image. The `finally` render() restores these.
  document.querySelectorAll<HTMLButtonElement>(".answers .btn")
    .forEach((b) => { b.disabled = true; });
  try {
    const res = await sessions.answer(run.session.id, ordinal, given);
    run.answered.add(ordinal);
    haptic("success");

    if (run.session.mode === "practice") {
      keepAhead();
      run.verdict = res as PracticeAnswer;
      haptic((res as PracticeAnswer).correct ? "success" : "error");
      if (state.me) state.me.free_explanations_left = (res as PracticeAnswer).free_explanations_left;
    } else {
      // Exam: the response carries no verdict at all, by design. Advance immediately.
      void (res as ExamAnswer);
      const next = nextUnanswered(run);
      if (next !== null) run.index = next;
      keepAhead();
    }
  } catch (err) {
    // 409 means the SERVER already has this answer — the request landed and its response
    // was lost, or a resume left the client behind. The old comment here said "the user
    // can tap again", which is exactly what does not work: every retry gets the same 409
    // and a generic red toast, with no Next button rendered, so the only way out of a
    // running exam is Submit. Treating it as done is what the state actually is.
    if (isAlreadyAnswered(err)) {
      run.answered.add(ordinal);
      if (run.session.mode !== "practice") {
        const next = nextUnanswered(run);
        if (next !== null) run.index = next;
      }
    } else {
      // Non-destructive: the sitting survives, the user can tap again.
      reportError(err);
    }
  } finally {
    run.busy = false;
    render();
  }
}

/** Did the server refuse because this ordinal is already answered?
 *
 *  Narrow on purpose. Any other failure — offline, 500, an expired sitting — must still
 *  surface, because silently advancing past a question that was NOT recorded would lose
 *  an answer rather than merely confuse.
 */
function isAlreadyAnswered(err: unknown): boolean {
  const e = err as { status?: number; detail?: string; message?: string };
  if (e?.status !== 409) return false;
  const text = `${e.detail ?? ""} ${e.message ?? ""}`.toLowerCase();
  return text.includes("already answered");
}

/** Fetch the translation for the question on screen.
 *
 *  §15's whole design: the Italian appears instantly and the translation lands after,
 *  because a blocking call in front of every question puts 3-5 seconds before every
 *  interaction. Only the first few of a paper are pre-warmed at creation, so most
 *  questions arrive as `available` and must be asked for here.
 *
 *  Patches the DOM in place rather than calling render(): a full rebuild would re-assign
 *  the figure's src and reload the image, and during an exam it would also churn the
 *  retained timer node.
 */
async function hydrateTranslation(): Promise<void> {
  const run = state.run;
  const question = run && currentQuestion(run);
  if (!run || !question || question.translation_state !== "available") return;

  const wanted = question.id;
  try {
    const res = await api.translation(wanted);
    // The user may have moved on while this was in flight.
    const now = state.run && currentQuestion(state.run);
    if (!now || now.id !== wanted) return;
    now.translation_state = res.translation_state;
    now.translation = res.translation;

    const slot = document.getElementById("tr-slot");
    if (slot) slot.replaceWith(translationSlot(now));
  } catch {
    // A missing translation is never worth interrupting a sitting for — but it IS worth
    // clearing. The slot renders "Translating…" for translation_state === "available", so
    // leaving that state untouched on failure left the word sitting under the question for
    // the rest of the sitting, promising something that was never going to arrive.
    // Marking it unavailable collapses the slot instead.
    const now = state.run && currentQuestion(state.run);
    if (!now || now.id !== wanted) return;
    now.translation_state = "unavailable";
    const slot = document.getElementById("tr-slot");
    if (slot) slot.replaceWith(translationSlot(now));
  }
}

/** Turning translations OFF has to take the one on screen with it.
 *
 *  The session's questions are held client-side with whatever `translation_state` and text
 *  the server sent when they were fetched. Flipping the switch changed the SETTING and
 *  re-rendered from that same cached payload, so the translation the learner had just asked
 *  to be rid of stayed under the question until they moved on — the switch appeared not to
 *  work, and then to work one question later.
 *
 *  Cleared for the whole paper, not just the current question, because every question
 *  already fetched carries one. */
function dropLoadedTranslations(): void {
  const run = state.run;
  if (!run) return;
  for (const question of run.session.questions) {
    question.translation = null;
    // "off" is what the server sends for a learner who has the switch down, so the client
    // ends up in the state a fresh fetch would have produced.
    question.translation_state = "off";
  }
  if (run.verdict) run.verdict = { ...run.verdict };
}

/** Turning translations ON mid-sitting has to go and get them.
 *
 *  A quiz started with the switch down was prepared with the switch down: the opening
 *  prefetch skipped translations entirely, and every question already fetched came back
 *  `off`. Flipping the switch used to fetch exactly one — the question on screen — so the
 *  next four arrived untranslated too, one wait at a time, and explanations behaved the
 *  same way. The learner reads that as "I turned it on and nothing happened".
 *
 *  So it re-runs the same warm-up a quiz start does, over the window in front of them, and
 *  shows the same loading state while it waits. The wait is the honest thing here: the
 *  translations genuinely do not exist yet. */
async function warmTranslations(): Promise<void> {
  const run = state.run;
  if (!run) return;

  // The server decides `translation_state` from the setting it has just been told about,
  // so the cached `off` on every question has to go before anything is re-fetched.
  for (const question of run.session.questions) {
    if (question.translation_state === "off") question.translation_state = "available";
  }

  const from = run.index + 1;
  const count = Math.min(PREFETCH_WINDOW, run.session.question_count - run.index);
  run.warming = true;
  render();
  try {
    // `wait: true` — the same blocking call the loading screen makes. Fire-and-forget would
    // put the spinner up and take it down before anything had been fetched.
    await sessions.prefetch(run.session.id, from, count, true);
    run.prefetchedTo = Math.max(run.prefetchedTo, from + count - 1);
  } catch {
    /* a failed warm-up is a slower question, not a broken sitting */
  } finally {
    run.warming = false;
    render();
    void hydrateTranslation();
  }
}

/** The block under the Italian. Always present as a node so it can be swapped in place. */
function translationSlot(question: Question): HTMLElement {
  const slot = el("div");
  slot.id = "tr-slot";
  if (question.translation_state === "shown" && question.translation) {
    const tr = el("div", "translation");
    if (question.translation.stem) tr.append(el("p", "stem", question.translation.stem));
    tr.append(el("p", "", question.translation.statement));
    slot.append(tr);
  } else if (question.translation_state === "available") {
    slot.append(el("p", "hint", t("translating")));
  } else if (question.translation_state === "locked") {
    // Compact on purpose. This person is mid-exam with a clock running; a full pitch
    // here would be an interruption, and interrupting a timed exam is indefensible.
    const strip = el("button", "premium-strip");
    strip.type = "button";
    strip.append(icons.star(26));
    const body = el("div");
    body.append(el("div", "premium-strip-title", t("premium_strip_title")),
                el("div", "premium-strip-text", t("premium_strip_text")));
    strip.append(body);
    const chev = el("span", "chev");
    chev.append(icons.chevron(20));
    strip.append(chev);
    strip.onclick = openSubscribe;
    slot.append(strip);
  }
  return slot;
}

function advance(): void {
  const run = state.run;
  if (!run) return;
  run.verdict = null;
  if (run.index < run.session.question_count - 1) {
    run.index += 1;
    render();   // render() kicks off hydrateTranslation for the new question
    void topUpPractice();
    return;
  }
  // At the end of the paper. An exam stops here — thirty questions is what an exam is.
  // Practice does not: it runs until the learner ends it, so fetch more and carry on.
  if (run.session.mode === "practice") { void extendPractice(); return; }
  render();
}

/** Fetch the next batch when practice reaches the end of the current one. */
async function extendPractice(): Promise<void> {
  const run = state.run;
  if (!run || run.busy) return;
  run.busy = true;
  render();
  try {
    const before = run.session.question_count;
    run.session = await sessions.extend(run.session.id);
    // No more questions in the whole bank: leave them on the last one rather than
    // advancing past the end, and let them end the sitting themselves.
    if (run.session.question_count > before) run.index += 1;
  } catch (err) {
    reportError(err);
  } finally {
    run.busy = false;
    render();
  }
}

/** Extend BEFORE the learner reaches the last question, so the batch boundary is
 *  invisible. Without this they would tap Next and wait on a round trip every 30. */
async function topUpPractice(): Promise<void> {
  const run = state.run;
  if (!run || run.busy) return;
  if (run.session.mode !== "practice") return;
  if (run.index < run.session.question_count - 3) return;
  try {
    run.session = await sessions.extend(run.session.id);
  } catch {
    // Best-effort: if it fails, `advance` will try again at the actual boundary.
  }
}

async function finishRun(timedOut = false): Promise<void> {
  const run = state.run;
  if (!run) return;
  try {
    const results = await sessions.finish(run.session.id);
    stopTicking();
    state.results = results;
    state.run = null;
    state.resumable = null;
    state.profile = null;   // streak, readiness and history all just changed
    state.stats = null;
    state.screen = "results";
    haptic(results.passed === false ? "error" : "success");
    render();
    if (timedOut) toast(t("exam_over_time"));
  } catch (err) {
    reportError(err);
  }
}

// ---------------------------------------------------------------------------
// screens
// ---------------------------------------------------------------------------

function homeScreen(): HTMLElement {
  const wrap = el("section", "screen");
  const brand = el("div", "brand");
  brand.append(el("h1", "", "Quiz Patente"), el("p", "", t("tagline")));
  wrap.append(brand);

  const modes = el("div", "modes");
  modes.append(
    modeCard("exam", t("exam"), t("exam_desc"), t("exam_badge")),
    modeCard("practice", t("practice"), t("practice_desc"), t("practice_badge")),
  );
  wrap.append(modes);

  wrap.append(vocabEntry());

  // If a sitting was left without finishing, offer it back BEFORE the promotion. Losing
  // an exam by tapping back would make the back button a trap.
  if (state.resumable) wrap.append(resumeCard(state.resumable));

  // During the trial the user already HAS everything, so selling to them would be noise.
  // Show what they have and when it ends instead.
  const days = trialDaysLeft();
  if (days !== null) wrap.append(trialBanner(days));

  // The promotion is the B variant of this screen and sits BELOW the cards, so it can
  // never push the two things this screen exists for off the fold.
  if (state.me && !state.me.premium) wrap.append(premiumBlock());
  return wrap;
}

/** One mode card. Artwork left, everything readable right.
 *
 *  The artwork is decoration: the tag, title and description carry the meaning, because
 *  artwork is the first thing to be clipped on a narrow phone. */
/** The way into the vocabulary trainer.
 *
 *  A row below the two mode cards rather than a third card of the same size: exam and
 *  practice are what this app is for, and a third equal card would say the three are
 *  equally important. It carries the gold Premium accent because it is a paid feature,
 *  and the learner should know that before tapping rather than after. */
/** Go back over questions you have already answered.
 *
 *  Practice already resurfaces mistakes, but on the Leitner schedule — when the algorithm
 *  decides, mixed with new material, never on demand. That is the right default and the
 *  wrong tool for "my test is Friday, show me everything I have got wrong".
 *
 *  Two small chips rather than two more mode cards. This screen has already been called too
 *  big once, exam and practice are what the app is FOR, and a third and fourth full-size
 *  card would claim they matter equally. A learner only reaches for these once they have a
 *  history worth repeating, so they are quiet until wanted — and the server answers 409
 *  when there is nothing to repeat, which is what the toast reports.
 */
/** How long a word must be held before it is saved.
 *
 *  The owner asked for two to three seconds, twice, after I argued for a tap. It is one
 *  constant and it drives BOTH the timer and the fill animation — the CSS reads it through
 *  a custom property, so the bar cannot finish at a different moment from the save.
 */
const HOLD_MS = 2000;

/** How far a finger may drift and still count as a hold rather than a scroll. */
const HOLD_SLOP = 10;

let holding: { timer: number; node: HTMLElement; x: number; y: number } | null = null;

function endHold(): void {
  if (!holding) return;
  window.clearTimeout(holding.timer);
  holding.node.classList.remove("holding");
  holding = null;
}

/** The Italian statement, with every word holdable.
 *
 *  Split on whitespace and rendered as spans, so a press can be attributed to one token
 *  without the client guessing where a word ends — the SERVER normalises, because
 *  normalising in two places is normalising in two ways, and it is the server that keys a
 *  shared cache on the result.
 *
 *  A HOLD, NOT A TAP, and three things have to be handled or it does not work on a phone:
 *
 *    · native selection. A long press in a WebView raises the text-selection callout, which
 *      would cover the word being held. Suppressed on the words only — see the CSS — which
 *      costs the ability to select the Italian text, and is the price of this gesture.
 *    · scrolling. A press that turns into a drag is somebody reading, not choosing, so any
 *      movement past a few pixels cancels it. Without this, scrolling the question adds
 *      whatever word the finger started on.
 *    · feedback. Two seconds of nothing happening is indistinguishable from a dead control,
 *      so the word fills as it is held and the fill IS the timer.
 *
 *  Punctuation stays outside the target: otherwise the last word of a sentence includes its
 *  full stop, and a learner aiming at the word holds the gap.
 */
function tappableStatement(text: string): HTMLElement {
  const p = el("p", "statement");
  for (const chunk of text.split(/(\s+)/)) {
    if (!chunk) continue;
    if (/^\s+$/.test(chunk)) { p.append(document.createTextNode(chunk)); continue; }
    const lead = chunk.match(/^[^\p{L}]*/u)?.[0] ?? "";
    const tail = chunk.match(/[^\p{L}]*$/u)?.[0] ?? "";
    const core = chunk.slice(lead.length, chunk.length - tail.length);
    if (lead) p.append(document.createTextNode(lead));
    if (core) p.append(holdableWord(core));
    if (tail) p.append(document.createTextNode(tail));
  }
  return p;
}

function holdableWord(core: string): HTMLElement {
  const word = el("span", "word", core);
  // The fill animation is driven by the same constant as the timer, through a custom
  // property. Two numbers would drift, and a bar that fills before the save lands is a
  // control that lies about what it has done.
  word.style.setProperty("--hold", `${HOLD_MS}ms`);

  // Pointer events rather than touch: Telegram Desktop exists, and a mouse press is the
  // same gesture there.
  word.onpointerdown = (ev) => {
    endHold();
    word.classList.add("holding");
    holding = {
      node: word,
      x: ev.clientX,
      y: ev.clientY,
      timer: window.setTimeout(() => {
        endHold();
        void lookUpWord(core, word);
      }, HOLD_MS),
    };
  };
  word.onpointerup = endHold;
  word.onpointercancel = endHold;
  word.onpointerleave = endHold;
  word.onpointermove = (ev) => {
    if (!holding) return;
    if (Math.abs(ev.clientX - holding.x) > HOLD_SLOP
        || Math.abs(ev.clientY - holding.y) > HOLD_SLOP) endHold();
  };
  // Belt and braces with the CSS: some WebViews raise the callout regardless of
  // -webkit-touch-callout, and a menu over the word being held is the gesture failing.
  word.oncontextmenu = (ev) => { ev.preventDefault(); return false; };
  return word;
}

/** A held word: save it, say so, and offer to undo.
 *
 *  Haptic feedback the moment it lands, because a gesture with no physical confirmation
 *  leaves the learner holding a word and wondering whether two seconds was enough.
 *
 *  The toast says it was added AND what the word means. The owner asked for the
 *  confirmation; the meaning is there because somebody who has just held a word for two
 *  seconds is asking what it is, and making them open another screen to find out wastes the
 *  moment they were curious.
 */
async function lookUpWord(word: string, node: HTMLElement): Promise<void> {
  if (node.classList.contains("busy")) return;
  node.classList.add("busy");
  try {
    const found = await vocab.lookUp(word);
    haptic("success");
    node.classList.add("saved");
    actionToast(`${found.it} — ${found.gloss}`, t("word_added"), t("undo"), () => {
      node.classList.remove("saved");
      void vocab.removeTerm(found.id).catch(reportError);
    });
  } catch (err) {
    haptic("error");
    if (err instanceof ApiError && err.status === 429) {
      toast(t("lookup_enough_today"));
    } else if (err instanceof ApiError && err.status === 402) {
      toast(t("lookup_premium"));
    } else if (err instanceof ApiError && err.status === 503) {
      toast(t("lookup_unavailable"));
    } else {
      reportError(err);
    }
  } finally {
    node.classList.remove("busy");
  }
}

/** Everything practice can be, one level under the card that offers it.
 *
 *  These four used to sit on the HOME screen: two repeat chips and a subject row beneath
 *  the mode cards. That made five entry points for two activities, and it pushed the thing
 *  the screen exists for — pick exam or practice — down the page.
 *
 *  The default is first and is the only primary button. It costs one extra tap compared
 *  with the card starting immediately, and that is the trade: the tap is obvious and lands
 *  on the biggest control on the screen, while the home screen goes back to three choices.
 */
function practiceScreen(): HTMLElement {
  const wrap = el("section", "screen");
  wrap.append(el("h1", "h1", t("practice")));
  wrap.append(el("p", "sub", t("practice_desc")));

  // The default, and deliberately not called "random": it serves what the learner is about
  // to forget, oldest due first. Calling it random would describe the thing it was
  // explicitly fixed for not being.
  const main = el("button", "btn primary practice-main");
  main.type = "button";
  main.append(el("span", "practice-main-title", t("practice_start")));
  main.append(el("span", "practice-main-sub", t("practice_start_sub")));
  main.onclick = () => void startRun("practice");
  wrap.append(main);

  const options: Array<[string, string, () => void]> = [
    [t("repeat_wrong"), t("repeat_wrong_sub"), () => void startRun("practice", "wrong")],
    [t("repeat_correct"), t("repeat_correct_sub"), () => void startRun("practice", "correct")],
    [t("subjects_entry"), t("subjects_entry_sub"),
     () => { state.screen = "subjects"; state.openFamily = null; render(); void loadSubjects(); }],
  ];
  for (const [title, sub, go] of options) {
    const row = el("button", "practice-option");
    row.type = "button";
    const body = el("span", "practice-option-body");
    body.append(el("span", "practice-option-title", title));
    body.append(el("span", "practice-option-sub", sub));
    row.append(body, el("span", "practice-option-go", "\u203a"));
    row.onclick = go;
    wrap.append(row);
  }

  const home = el("button", "btn secondary", t("back_home"));
  home.type = "button";
  home.onclick = () => { state.screen = "home"; render(); };
  wrap.append(home);
  return wrap;
}

async function loadSubjects(): Promise<void> {
  try {
    state.subjects = await categories.list();
    render();
  } catch (err) {
    reportError(err);
  }
}

/** Choose a subject to practise.
 *
 *  Ranked worst-first by the marks each subject is costing THIS learner — the same order,
 *  from the same server call, as the error-analysis screen. A list ordered by size instead
 *  would put road signs at the top for everybody forever, which is a table of contents
 *  rather than advice.
 *
 *  Two levels. The seven families are the top, in plain language, because that is what fits
 *  on a phone and what the analysis screen already speaks. The ministerial topics sit one
 *  tap underneath, under their official untranslated names, because every Italian study
 *  book is organised by exactly those chapters and somebody working through one is looking
 *  for "Segnali di pericolo", not "road signs".
 */
function subjectsScreen(): HTMLElement {
  const wrap = el("section", "screen");
  wrap.append(el("h1", "h1", t("subjects_title")));
  wrap.append(el("p", "sub", t("subjects_sub")));

  const list = state.subjects;
  if (!list) {
    wrap.append(el("div", "spinner"));
    return wrap;
  }

  for (const cat of list) {
    const card = el("div", "card subject");

    const top = el("div", "subject-top");
    top.append(el("div", "subject-name", t(`fam_${cat.family}` as Key)));
    // The learner's own error rate, or an honest refusal. Never a zero standing in for
    // "not measured yet" — on day one every one of these is untested, and a row of 0%
    // would read as mastery of a bank they have never opened.
    //
    // The refusal is styled DOWN, not in the error colour. "Not tested" is an absence of
    // data, and rendering it in the same red as a 40% error rate tells a beginner that
    // every subject in the product is already going badly for them.
    top.append(el("div", `subject-rate${cat.error_rate === null ? " untested" : ""}`,
      cat.error_rate === null
        ? t("subjects_untested")
        : `${Math.round(cat.error_rate * 100)}%`));
    card.append(top);

    card.append(el("p", "subject-meta", t("subjects_meta", {
      n: cat.questions, per: cat.per_exam.toFixed(1),
    })));

    const go = el("button", "btn primary subject-go", t("subjects_start"));
    go.type = "button";
    go.onclick = () => void startRun("practice", "smart", cat.scope);
    card.append(go);

    // The book chapters, one tap down. Collapsed by default: seven families with every
    // topic open is thirty-two rows and no shape at all.
    const toggle = el("button", "subject-more");
    toggle.type = "button";
    const open = state.openFamily === cat.family;
    toggle.textContent = open ? t("subjects_hide_topics") : t("subjects_show_topics");
    toggle.onclick = () => {
      state.openFamily = open ? null : cat.family;
      render();
    };
    card.append(toggle);

    if (open) {
      const topics = el("div", "subject-topics");
      for (const topic of cat.topics) {
        const chip = el("button", "subject-topic");
        chip.type = "button";
        chip.append(el("span", "subject-topic-name", topic.name));
        chip.append(el("span", "subject-topic-n", String(topic.questions)));
        chip.onclick = () => void startRun("practice", "smart", topic.scope);
        topics.append(chip);
      }
      card.append(topics);
    }
    wrap.append(card);
  }

  const home = el("button", "btn secondary", t("back_home"));
  home.type = "button";
  home.onclick = () => { state.screen = "home"; render(); };
  wrap.append(home);
  return wrap;
}

/** How many terms the glossary holds, straight from the server.
 *
 *  Every surface that names a size calls this. The alternative — writing the number into
 *  the locale strings — is what put "1090 exam words" above "0 of 1104 learned" on the
 *  same screen: the seed grew by fourteen terms and four translations of one sentence did
 *  not. Zero only if the glossary really is empty, since `render` runs after `me` loads. */
function vocabSize(): number {
  return state.me?.vocab_terms ?? 0;
}

function vocabEntry(): HTMLElement {
  const card = el("button", "v-entry");
  card.type = "button";
  // The same tile pattern, so the third card on the home screen belongs with the two
  // above it rather than being an emoji in a row of drawn icons.
  card.append(icons.tile(icons.vocabGlyph(), "vocab"));
  const body = el("span", "v-entry-body");
  body.append(el("span", "v-entry-title", t("v_title")),
              el("span", "v-entry-sub", t("v_sub", { n: vocabSize() })));
  card.append(body);
  card.append(el("span", "v-entry-go", "\u203A"));
  card.onclick = () => void openVocab();
  return card;
}

function modeCard(mode: Mode, title: string, desc: string, tag: string): HTMLElement {
  const card = el("button", `mode ${mode}`);
  card.type = "button";

  // A COLOURED TILE, not a tinted card.
  //
  // The card used to carry the meaning in its own background — a red wash for the exam, a
  // green one for practice — and that is precisely what made a dark theme impossible:
  // repaint the card and the signal goes with it. The colour now lives in the tile, so the
  // card can be any surface and the exam still reads as the exam. Taken from the reference
  // app the owner supplied, which does the same thing and is legible in the dark for it.
  //
  // It also replaces a 200x200 illustration that was being drawn at 64-92px. The picture
  // was decoration; the tile, the title and the tag are what carry the meaning.
  card.append(icons.tile(
    mode === "exam" ? icons.examGlyph() : icons.practiceGlyph(), mode));

  const body = el("div", "mode-body");
  const pill = el("div", "mode-tag");
  pill.append(mode === "exam" ? icons.alert(15) : icons.check(15), document.createTextNode(tag));
  body.append(pill, el("div", "mode-title", title));

  const description = el("div", "mode-desc");
  for (const part of desc.split(". ")) {
    const line = part.trim();
    if (!line) continue;
    description.append(el("div", "", line.endsWith(".") ? line : `${line}.`));
  }
  body.append(description);
  card.append(body);

  const go = el("div", "mode-go");
  go.append(icons.chevron(22));
  card.append(go);

  // The exam has one way to sit it, so its card starts it. Practice has four, and they
  // used to be scattered across the home screen as two chips and a row beneath the cards —
  // five competing entry points for two activities. They live behind this card now.
  card.onclick = mode === "exam"
    ? () => void startRun("exam")
    : () => { state.screen = "practice"; render(); };
  return card;
}


/** The four Premium selling points, in one place.
 *
 *  Both promotion blocks list the same features in a different order, and having them
 *  twice is how the pitch on one screen quietly stops matching the pitch on another. */
function premiumFeatures(order: "sell" | "explain"): Array<{ title: string; sub: string }> {
  const ai = { title: t("f_ai"), sub: t("f_ai_s") };
  const lang = { title: t("f_lang"), sub: t("f_lang_s") };
  const vocab = { title: t("f_vocab"), sub: t("f_vocab_s", { n: vocabSize() }) };
  const future = { title: t("f_future"), sub: t("f_future_s") };
  const trial = { title: t("f_trial"), sub: t("f_trial_s") };
  // "explain" leads with the AI explanation because the user has just got something
  // wrong and is asking why; "sell" leads with translation, the broader hook. The trial
  // is last in both: it is the closer, not the pitch.
  return order === "explain"
    ? [ai, lang, vocab, future, trial]
    : [lang, ai, vocab, future, trial];
}

function premiumList(order: "sell" | "explain"): HTMLElement {
  const list = el("div", "premium-list");
  for (const feature of premiumFeatures(order)) {
    const item = el("div", "premium-item");
    const text = el("div");
    text.append(el("b", "", feature.title), document.createTextNode(feature.sub));
    item.append(icons.check(18), text);
    list.append(item);
  }
  return list;
}

/** The full Premium pitch. Gold, and gold is used for nothing else in this app. */
function premiumBlock(): HTMLElement {
  const box = el("div", "premium");
  const head = el("div", "premium-head");
  head.append(icons.star(30));
  const headText = el("div");
  headText.append(el("div", "premium-title", t("premium_title")),
                  el("p", "premium-lead", t("premium_lead")));
  head.append(headText);
  box.append(head);

  box.append(premiumList("sell"));

  const cta = el("button", "btn gold");
  cta.append(icons.crown(20), document.createTextNode(t("premium_cta")));
  cta.onclick = openSubscribe;
  box.append(cta);
  return box;
}

/** Payment happens in the Telegram chat, never here — plan §6.2: a Mini App selling
 *  digital goods sits closer to the Stars-only rule and to Apple's review guidelines. */
/** Take the learner to the place they can actually pay.
 *
 *  This used to be `toast(t("unlock_in_chat"))` and nothing else — four paid surfaces
 *  bound to a sentence that faded after three seconds. Payments were live and there was
 *  no route from wanting to buy to buying.
 *
 *  `?start=plan` makes the bot answer with /plan and its Tribute buttons the moment the
 *  chat opens, so the learner lands on the prices rather than on an empty conversation
 *  they have to work out.
 *
 *  The toast survives as the fallback for clients too old to have openTelegramLink —
 *  telling them where to go is worse than taking them there, and much better than a
 *  button that does nothing.
 */
function openSubscribe(): void {
  const bot = state.me?.bot_username;
  if (bot && openChat(`https://t.me/${bot}?start=plan`)) return;
  toast(t("unlock_in_chat"));
}

/** The Settings row. NOT the same action: it promises "support, news and useful
 *  material", and it was wired to the subscribe toast — so a learner who already had
 *  Premium tapped it and was told to open the bot to subscribe. */
function openSupport(): void {
  const handle = state.me?.support_contact || state.me?.bot_username;
  if (handle && openChat(`https://t.me/${handle.replace(/^@/, "")}`)) return;
  toast(t("unlock_in_chat"));
}

/** Days left on the trial, or null if the user is not on one.
 *
 *  A trial is just a pass with an expiry, so "on a trial" is inferred: they have a pass
 *  and have never bought anything. That keeps trials out of the purchase record entirely
 *  — see the note in api/services/users.py about why a trial is not a Purchase row. */
function trialDaysLeft(): number | null {
  const me = state.me;
  // `has_pass` and not `premium`, deliberately, and the one place that is still right:
  // a trial is a pass with a date on it. Someone Premium through channel membership has
  // no expiry to count down, and showing them "3 days left" would be a lie.
  // `trialing` distinguishes a card-backed Tribute trial from a hand-granted pass.
  if (!me?.has_pass || !me.pass_expires_at || me.purchased) return null;
  const ms = Date.parse(me.pass_expires_at) - Date.now();
  return ms > 0 ? Math.max(1, Math.ceil(ms / 86_400_000)) : null;
}

function trialBanner(days: number): HTMLElement {
  const card = el("div", "premium-strip");
  card.style.marginTop = "var(--lg)";
  card.append(icons.crown(24));
  const body = el("div");
  body.append(el("div", "premium-strip-title", t("trial_active")),
              el("div", "premium-strip-text", t("trial_days_left", { n: days })));
  card.append(body);
  return card;
}

function resumeCard(session: Session): HTMLElement {
  const card = el("button", "premium-strip");
  card.type = "button";
  card.style.cssText = "background:var(--tint-info);border-color:#bfdbfe;margin-top:var(--lg)";
  card.append(icons.refresh(24));
  const body = el("div");
  body.append(el("div", "premium-strip-title", t("resume")),
              el("div", "premium-strip-text", t("resume_desc")));
  card.append(body);
  const chev = el("span", "chev");
  chev.append(icons.chevron(20));
  card.append(chev);
  card.onclick = () => void resumeRun(session.id);
  return card;
}

/** Re-fetch rather than reuse the object we held: the server may have graded it while
 *  the user was away (an exam whose deadline passed), and its answer is authoritative. */
async function resumeRun(id: number): Promise<void> {
  try {
    const session = await sessions.read(id);
    state.resumable = null;
    if (session.state !== "open") {
      state.results = await sessions.results(id);
      state.screen = "results";
      render();
      return;
    }
    enterRun(session);
  } catch (err) {
    state.resumable = null;
    reportError(err);
    render();
  }
}

function currentQuestion(run: Run): Question | undefined {
  return run.session.questions[run.index];
}


/** Turn the translation on or off without leaving the sitting.
 *
 *  It lives here because the exam is exactly where the decision is made: a candidate
 *  practising for the real thing wants the Italian alone, and the same person, stuck on
 *  one phrasing, wants the translation back for that question. Sending them to Settings
 *  means abandoning a running exam to change it — the tab bar is hidden mid-sitting
 *  precisely so that cannot happen by accident.
 *
 *  Returns null for anyone who has no translation to toggle: without a pass, or with a
 *  UI language the questions are not translated into. A switch that does nothing is
 *  worse than no switch.
 */
/** Which language the questions are read in — off, or one of the three we translate into.
 *
 *  Was a two-state switch tied to the interface language, so an Uzbek speaker who reads
 *  Russian more comfortably — common enough that Uzbek shipped as beta — had to change the
 *  whole app to Russian to get Russian translations. The reading language is now its own
 *  choice, and "off" is one of its values rather than a separate control.
 *
 *  A <select> rather than a row of chips: four options do not fit beside a question, and the
 *  native picker is the one control on this screen that costs no vertical space at all.
 */
function translationToggle(): HTMLElement | null {
  const me = state.me;
  if (!me || !me.premium) return null;

  const row = el("label", "q-tr");
  row.append(el("span", "q-tr-label", t("tr_toggle")));

  const pick = el("select", "q-tr-pick");
  const current = me.translations_on ? (me.translation_lang || me.lang) : "";
  for (const [value, label] of [
    ["", t("tr_off")],
    ...TRANSLATION_LANGUAGES.map((code) => [code, t(`lang_${code}` as Key)] as const),
  ] as ReadonlyArray<readonly [string, string]>) {
    const option = el("option", "", label);
    option.value = value;
    if (value === current) option.selected = true;
    pick.append(option);
  }

  pick.onchange = async () => {
    const chosen = pick.value;
    try {
      // Two settings in one call: "off" is translations_on = false, and any language is
      // translations_on = true plus that language. Sending them separately would leave a
      // window where the server had one and not the other.
      state.me = await api.settings({
        translations_on: chosen !== "",
        ...(chosen === "" ? {} : { translation_lang: chosen }),
      });
      if (chosen === "") {
        dropLoadedTranslations();
        render();
      } else {
        // Also on a LANGUAGE change, not just on turning them on: the questions already
        // fetched carry the old language's text, and leaving it there would show Russian
        // under a question the learner has just asked to read in English.
        dropLoadedTranslations();
        await warmTranslations();
      }
    } catch (err) {
      reportError(err);
    }
  };
  row.append(pick);
  return row;
}

/** One row carrying what governs the sitting, where you are in it, and the way out.
 *
 *  Replaces a 136px timer card plus a 119px answer sheet plus a counter row — 295px of
 *  furniture above every question, on a screen whose complaint was that the answer buttons
 *  were below the fold.
 *
 *  The three slots are the same in both modes and only their fillings change: an exam is
 *  governed by a clock, practice by nothing, so practice shows its name where the clock
 *  would be. Two different bars would be two things to learn. */
function runBar(run: Run): HTMLElement {
  const exam = run.session.mode === "exam";
  const bar = el("div", `runbar ${exam ? "exam" : "practice"}`);

  if (run.deadline) {
    bar.append(timerDial());
    timerNode = el("div", "timer-value", "--:--");
    bar.append(timerNode);
  } else {
    // The counter, not the mode name. "Practice" tells a learner who just tapped Practice
    // nothing they do not know, and the row has to hold a labelled control as well —
    // carrying both overflowed the bar by 39px even on a wide phone.
    bar.append(el("div", "runbar-mode", t("question_n", { n: run.index + 1 })));
  }

  // Exam only. "12/30", and tapping it opens the paper — in practice there is no paper: the
  // total is only the current batch and would silently become 60, so the promise is not made,
  // and a third element does not fit beside a mode name and a labelled control anyway. The
  // practice counter lives in the left slot above, where the clock sits in an exam.
  if (exam) {
    const chip = el("button", "runbar-chip");
    chip.type = "button";
    chip.append(el("b", "runbar-at", String(run.index + 1)),
                document.createTextNode(`/${run.session.question_count}`));
    chip.setAttribute("aria-label", t("sheet_title"));
    chip.onclick = openAnswerSheet;
    bar.append(chip);
  }

  // Practice ENDS here and is graded; an exam is handed in from inside the answer sheet,
  // where you can see what is still blank before committing.
  //
  // An exam's control here is EXIT instead, and the difference in cost is what justifies
  // the difference in placement. Submit is irreversible and creates a permanent result, so
  // it was deliberately kept out of the top strip — the band where a downward drag
  // minimises the Mini App on clients below Bot API 7.7. Exit creates no result at all: it
  // closes the sitting as uncounted and hands back the review. Putting it here is the
  // answer to "there is no exit" — a way out that needs no instruction to find.
  // LABELLED in both modes, not a bare glyph. The report was "there is no exit mode" — a
  // discovery failure — and an icon on its own is something the learner has to guess at; an
  // aria-label fixes that for a screen reader and for nobody else.
  //
  // Two different words on purpose, because the two do different things. Exiting an exam
  // produces NO result; finishing a practice round produces one and keeps it. Naming both
  // "Exit" would put the whole difference in a dialog that a learner can dismiss without
  // reading, and the difference is the entire point.
  const end = el("button", `runbar-end ${exam ? "exam" : ""}`);
  end.type = "button";
  // Disabled while an answer is in flight. submitAnswer guards itself and the two answer
  // buttons, but nothing guarded this one — and if the finish request overtakes the answer
  // request the server refuses the answer with 409, so it is never recorded: no Progress
  // row, no Leitner move, and a red error toast over the results screen. A narrow window,
  // and a wider one now that this control is labelled and therefore actually pressed.
  end.disabled = run.busy;
  end.append(exam ? icons.exit(18) : icons.flag(18));
  const label = exam ? t("exit_label") : t("end_test");
  end.append(el("span", "runbar-end-label", label));
  end.setAttribute("aria-label", label);
  end.onclick = exam ? confirmExit : confirmFinish;
  bar.append(end);

  if (run.deadline) tick();
  return bar;
}

/** The next question still needing an answer, wrapping around; null when the paper is full.
 *
 *  `index + 1` was right while an exam was a one-way conveyor. Now that the answer sheet can
 *  jump backwards, answering question 7 after skipping to it would land the candidate on 8 —
 *  which they answered ten minutes ago — and then 9, and so on, walking them through work
 *  they have already done instead of back to the blanks. */
function nextUnanswered(run: Run): number | null {
  const total = run.session.question_count;
  for (let step = 1; step <= total; step++) {
    const index = (run.index + step) % total;
    if (!run.answered.has(index + 1)) return index;
  }
  return null;   // everything is answered; stay where we are and let them submit
}

function runScreen(): HTMLElement {
  const run = state.run!;
  const question = currentQuestion(run);
  const answeredHere = run.answered.has(run.index + 1);
  // A practice verdict carries an explanation of unbounded length. Pinning that inside a
  // viewport-height shell would trap it in a box; while it is up the screen goes back to
  // being an ordinary scrolling page. An exam never shows one, so an exam is always pinned.
  const reading = run.session.mode === "practice" && !!run.verdict && answeredHere;

  const wrap = el("section", reading ? "screen" : "screen run");
  wrap.append(runBar(run));

  const meta = el("div", "q-meta");
  const tr = translationToggle();
  if (tr) meta.append(tr);
  // Rendered even when empty so the translation switch keeps one place to live across
  // modes and entitlements, rather than moving when it happens to be absent.
  wrap.append(meta);

  if (!question) { wrap.append(el("div", "spinner")); return wrap; }

  // Everything the candidate READS. The one part of the screen allowed to scroll, so the
  // controls above and below it never move — which is the actual defect being fixed here:
  // the position of the answer buttons was a function of how long the question happened
  // to be.
  const body = el("div", "run-body");
  if (question.image) {
    const plate = el("div", "plate");
    const img = el("img");
    img.src = api.figureUrl(question.image);
    img.alt = "";
    plate.append(img);
    body.append(plate);
  }
  if (question.stem_it) body.append(el("p", "caption", question.stem_it));
  body.append(tappableStatement(question.statement_it));
  if (run.warming) {
    // The question stays readable and answerable — only the strip that is about to hold a
    // translation says it is being fetched.
    body.append(el("p", "hint", t("fetching_translations")));
  } else {
    body.append(translationSlot(question));
  }
  if (reading && run.verdict) body.append(verdictBox(run.verdict));
  wrap.append(body);
  watchOverflow(body);

  // Every question answered, and the clock still running. Until now the candidate was left
  // on the last question with a Next that wrapped them round the paper and no statement
  // anywhere that they were done — the only way to hand in was the answer sheet, two taps
  // away behind a chip. A paper with nothing left blank should say so.
  const complete = run.session.mode === "exam"
    && run.answered.size >= run.session.question_count;

  if (complete) {
    const done = el("div", "run-done");
    done.append(el("p", "run-done-line", t("all_answered")));
    const hand = el("button", "btn primary", t("finish_now"));
    hand.type = "button";
    hand.disabled = run.busy;
    hand.onclick = () => void confirmHandIn();
    done.append(hand);
    // Still reachable, because "I want to look at 14 again" is the whole reason the
    // confirmation below offers a way out.
    const back = el("button", "link-btn", t("sheet_title"));
    back.type = "button";
    back.onclick = openAnswerSheet;
    done.append(back);
    wrap.append(done);
  } else if (!answeredHere) {
    const row = el("div", "answers");
    const vero = el("button", "btn vero", t("vero"));
    const falso = el("button", "btn falso", t("falso"));
    vero.disabled = falso.disabled = run.busy;
    vero.onclick = () => void submitAnswer(true);
    falso.onclick = () => void submitAnswer(false);
    row.append(vero, falso);
    wrap.append(row);
  } else if (!reading) {
    const next = el("button", "btn primary", t("next"));
    next.onclick = advance;
    wrap.append(next);
  }

  // The exam's Submit lives in the answer sheet and the practice End sits in the bar, so
  // the old footer — which rendered a SECOND identical control a few pixels below the
  // verdict box's own — has nothing left to add.
  return wrap;
}

/** Tell a scrolling region when it has more below, so a clipped line reads as "there is
 *  more" rather than as a rendering fault.
 *
 *  Only the longest few percent of questions overflow at all; a permanent fade would dim
 *  the last line of every other one. */
function watchOverflow(node: HTMLElement): void {
  const update = () => {
    const more = node.scrollHeight - node.scrollTop - node.clientHeight > 4;
    node.classList.toggle("more", more);
  };
  node.addEventListener("scroll", update, { passive: true });
  // After layout: scrollHeight is meaningless until the element is in the document, and
  // taller still once the figure decodes.
  requestAnimationFrame(update);
  window.setTimeout(update, 400);
}

/** Leave a sitting without finishing it.
 *
 *  For an EXAM this warns first, and the warning is honest: the deadline keeps running
 *  while you are away, because a real exam clock does not pause when you look away. The
 *  session stays open on the server rather than being thrown away, so it can be resumed
 *  or graded properly later — abandoning it here would discard answers the user has
 *  already given.
 *
 *  For PRACTICE there is nothing to lose, so it just goes back.
 */
async function leaveRun(): Promise<void> {
  const run = state.run;
  if (!run) { goHome(); return; }
  // `ask`, not `confirm`: some Android Telegram builds suppress the browser dialog inside
  // the webview, and a suppressed confirm here means a learner cannot get out of a running
  // exam at all. It falls back to confirm() where Telegram's own sheet is unavailable.
  if (run.session.mode === "exam" && !(await ask(t("confirm_leave")))) return;
  stopTicking();
  state.resumable = run.session;
  state.run = null;
  goHome();
}

function goHome(): void {
  state.screen = "home";
  render();
}

/** Leave an exam without sitting it.
 *
 *  Asked before it happens, and the question says both halves out loud: this attempt will
 *  not be counted, AND you will see your answers. A learner who thinks Exit throws their
 *  work away will not press it, and one who thinks it counts as a failure will not press it
 *  either — so the confirmation is the feature as much as the button is.
 */
async function confirmExit(): Promise<void> {
  const run = state.run;
  if (!run || run.busy) return;
  if (!(await ask(t("exit_confirm")))) return;
  await exitRun();
}

async function exitRun(): Promise<void> {
  const run = state.run;
  if (!run) return;
  try {
    const results = await sessions.exit(run.session.id);
    stopTicking();
    state.results = results;
    state.run = null;
    // The sitting is closed, so there is nothing left to resume. Leaving `resumable` set
    // would put a "continue your exam" card on the home screen pointing at a session the
    // server has already finished.
    state.resumable = null;
    // Cleared for the same reason finishing clears them: the answers given are still
    // answers, so readiness and the per-topic figures have moved. Not "still count as
    // practice" — an exam answer does not touch the Leitner schedule at all
    // (MODE_UPDATES_SCHEDULE). What moved is the accuracy window; the SITTING is not
    // recorded either way.
    state.profile = null;
    state.stats = null;
    state.screen = "results";
    render();
  } catch (err) {
    reportError(err);
  }
}

/** Handing in a paper with nothing left blank.
 *
 *  Different from the answer-sheet Submit, which is for handing in EARLY and warns about
 *  what is still unanswered. Here nothing is unanswered, so the only thing worth saying is
 *  the thing a candidate in a real exam room weighs: there is still time on the clock, and
 *  checking your work is free. The default answer is to go back and look. */
async function confirmHandIn(): Promise<void> {
  const run = state.run;
  if (!run || run.busy) return;
  const left = Math.max(0, remainingMs());
  const mm = String(Math.floor(left / 60_000)).padStart(2, "0");
  const ss = String(Math.floor((left % 60_000) / 1000)).padStart(2, "0");
  if (!(await ask(t("finish_confirm", { time: `${mm}:${ss}` })))) return;
  void finishRun();
}

async function confirmFinish(): Promise<void> {
  const run = state.run;
  // `run.busy`, same as the answer buttons. Not inside finishRun itself: the deadline path
  // calls it too, and an exam whose twenty minutes are up has to end whatever else is in
  // flight.
  if (!run || run.busy) return;
  // Practice asks too, now that it is reachable from a labelled control rather than from a
  // glyph nobody pressed by accident. Its question is the MIRROR of the exam exit's: this
  // one is kept. A learner offered two ways out of two modes should be told, at the moment
  // of choosing, which of them records anything.
  const question = run.session.mode === "exam" ? t("confirm_submit") : t("confirm_end_practice");
  if (!(await ask(question))) return;
  void finishRun();
}

/** A small clock face beside the countdown. Drawn rather than an emoji so it inherits
 *  the urgency colour along with the digits. */
function timerDial(): SVGSVGElement {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  node.setAttribute("viewBox", "0 0 48 48");
  node.setAttribute("width", "46"); node.setAttribute("height", "46");
  node.setAttribute("class", "timer-dial");
  node.setAttribute("aria-hidden", "true");
  // The face was a literal #fff, which on the dark theme's --bg is a bright white disc
  // punched into the page. --card is white in light mode, so nothing changes there.
  node.innerHTML = `<circle cx="24" cy="24" r="17" fill="var(--card)" stroke="currentColor" stroke-width="3"/>
    <path d="M24 24 V9 A15 15 0 0 1 37.5 30 Z" fill="currentColor" opacity=".35"/>
    <path d="M24 24 L33 19" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
    <circle cx="24" cy="24" r="2.4" fill="currentColor"/>
    <rect x="20.5" y="2.5" width="7" height="4" rx="1.6" fill="currentColor"/>`;
  return node;
}

/** The answer sheet: numbered, and it NEVER shows correctness. In a real exam you do not
 *  find out until the end, and that is the property exam mode exists to preserve.
 *
 *  It used to sit permanently above the question, costing 119px of every exam screen while
 *  being made of <i> elements nothing could tap — the single most expensive piece of pure
 *  decoration in the app. It now lives behind the position chip, where the same thirty
 *  cells are buttons that jump to a question. The server has always allowed that: `answer()`
 *  looks an item up by (session, ordinal) and refuses only a SECOND answer to the same one,
 *  so ordinals may be taken in any order.
 *
 *  `jump` is null when the sheet is being rendered read-only. */
function answerSheet(run: Run, jump: ((index: number) => void) | null = null): HTMLElement {
  const sheet = el("div", "sheet");
  for (let i = 0; i < run.session.question_count; i++) {
    const cell = el(jump ? "button" : "i", "cell", String(i + 1));
    if (run.answered.has(i + 1)) cell.classList.add("done");
    if (i === run.index) cell.classList.add("here");
    if (jump && cell instanceof HTMLButtonElement) {
      cell.type = "button";
      cell.onclick = () => jump(i);
    }
    sheet.append(cell);
  }
  return sheet;
}

/** The paper, on demand: which questions are done, and a way back to any of them.
 *
 *  Also where an exam is handed in. The submit control could sit in the top bar beside the
 *  clock, but the top strip is where a downward drag minimises the Mini App on clients
 *  below Bot API 7.7, and it is the wrong place for the one irreversible control on a timed
 *  screen. Here the order matches the intent: look at what is still blank, then hand in.
 */
function openAnswerSheet(): void {
  const run = state.run;
  if (!run) return;

  const scrim = el("div", "modal");
  const card = el("div", "modal-card");
  const close = () => {
    scrim.remove();
    // The screen underneath owns the back button again. Without this, leaving the sheet
    // leaves Telegram's arrow wired to a closure over a detached node.
    setBackButton(backTarget());
  };

  card.append(el("h3", "sheet-title", t("sheet_title")));
  card.append(el("p", "sheet-sub", t("sheet_left", {
    n: run.session.question_count - run.answered.size,
  })));
  card.append(answerSheet(run, (index) => {
    run.index = index;
    close();
    render();
  }));

  const hand = el("button", "btn primary sheet-submit", t("submit_short"));
  hand.type = "button";
  hand.onclick = () => { close(); void confirmFinish(); };
  card.append(hand);

  // The same exit as the one in the bar. Both ways out live together here, which is where
  // someone looking at what they have left blank is actually deciding between them.
  const leave = el("button", "btn secondary sheet-exit", t("exit_label"));
  leave.type = "button";
  leave.onclick = () => { close(); void confirmExit(); };
  card.append(leave);

  scrim.append(card);
  scrim.onclick = (ev) => { if (ev.target === scrim) close(); };
  setBackButton(close);
  document.body.append(scrim);
}

/** A quiet line when the explanation is not in the reader's own language. */
function explanationLangNote(explanationLang: string | null): HTMLElement {
  const wrap = el("span");
  const mine = state.me?.lang;
  if (!explanationLang || !mine || explanationLang === mine) return wrap;
  wrap.className = "expl-lang";
  wrap.textContent = t("expl_in_other_lang");
  return wrap;
}

/** "This explanation is wrong."
 *
 *  The endpoint was built and tested and reachable only from the loopback route the bot
 *  used, so once drilling moved into the Mini App nobody could report anything and the
 *  owner could not hear about a single bad explanation. Report volume per thousand served
 *  is the quality metric for a feature whose whole pitch is quality.
 */
function reportLink(questionId: number): HTMLElement {
  // The disclosure sits WITH the report button, not in a settings page or a privacy policy.
  // Someone doubting an explanation should learn where it came from at the moment of
  // doubting, beside the control for saying so. And it is a plain description rather than a
  // disclaimer: no explanation in this database has been read by a person before its first
  // reader, so the readers are the review, and they should be told they are.
  const wrap = el("div", "expl-foot");
  wrap.append(el("p", "ai-note", t("ai_note")));

  const link = el("button", "expl-report", t("report_wrong"));
  link.type = "button";
  link.onclick = async () => {
    link.disabled = true;
    try {
      await api.report(questionId);
      link.textContent = t("report_thanks");
    } catch (err) {
      link.disabled = false;
      reportError(err);
    }
  };
  wrap.append(link);
  return wrap;
}

function verdictBox(a: AnswerResult): HTMLElement {
  const box = el("div");

  const verdict = el("div", `verdict ${a.correct ? "ok" : "bad"}`);
  const mark = el("div", "verdict-mark");
  mark.append(a.correct ? icons.tick(28) : icons.cross(26));
  verdict.append(mark);
  const text = el("div");
  text.append(el("div", "verdict-title", a.correct ? t("correct") : t("wrong")));
  if (!a.correct) {
    const sub = el("div", "verdict-sub");
    sub.append(document.createTextNode(`${t("the_answer_is")}: `),
               el("b", "", a.correct_answer ? t("vero") : t("falso")));
    text.append(sub);
  }
  verdict.append(text);
  box.append(verdict);

  // The one answer that finished today's goal. Said here, at the moment it happens, rather
  // than only on the profile: a habit is reinforced where the work was done, and a learner
  // who never opens the profile would otherwise never learn the streak exists.
  if (a.streak_earned_today) {
    const won = el("div", "streak-won");
    won.append(el("span", "streak-won-flame", "🔥"), el("span", "", t("streak_earned")));
    box.append(won);
  }

  // "+1 point" — said at the moment it happens, and only when it happened.
  //
  // Three separate rules can silently make a correct answer worth nothing: the question was
  // already counted this week, the day is capped, the pace was not credited. A learner who
  // sees a green tick and no movement on the board cannot tell any of those from a bug, and
  // writes to support instead. The rules screen explains all three; this line is what makes
  // somebody go and read it.
  if (a.league_point) {
    const point = el("div", "league-point");
    point.append(el("span", "", t("league_point_earned")));
    box.append(point);
  }

  if (a.explanation_state === "shown" && a.explanation) {
    const panel = el("div", "explain");
    panel.append(icons.info(24));
    const body = el("div");
    body.append(el("h3", "", t("explanation")), el("p", "", a.explanation));
    // Say so when the text is not in the language they chose. Uzbek falls back to
    // Russian on purpose — a bad explanation is the only thing on screen and is the
    // thing being sold, so Uzbek ships as translations first — but silently serving a
    // language someone did not pick reads as broken rather than as a known limit.
    body.append(explanationLangNote(a.explanation_lang));
    body.append(reportLink(a.question_id));
    panel.append(body);
    box.append(panel);
  } else if (a.explanation_state === "available") {
    const why = el("button", "btn secondary", t("why"));
    why.style.marginTop = "var(--md)";
    why.onclick = () => void askWhy(why, a.question_id);
    box.append(why);
  } else if (a.explanation_state === "locked") {
    // The single most valuable promotion in the product: the user has just got something
    // wrong and genuinely wants to know why. This is the one moment they will pay.
    box.append(lockedExplanation());
  } else if (a.explanation_state === "unavailable") {
    box.append(el("p", "caption", t("explanation_unavailable")));
  }

  const next = el("button", "btn primary", t("next"));
  next.style.marginTop = "var(--lg)";
  next.onclick = advance;
  box.append(next);

  const end = el("button", "btn secondary", t("end_test"));
  end.style.marginTop = "var(--md)";
  end.onclick = confirmFinish;
  box.append(end);
  return box;
}

function lockedExplanation(): HTMLElement {
  const box = el("div", "premium");
  box.style.marginTop = "var(--md)";

  const head = el("div", "premium-head");
  head.append(icons.crown(30));
  const headText = el("div");
  headText.append(el("div", "premium-title", t("premium_locked_q")),
                  el("p", "premium-lead", t("premium_locked_lead")));
  head.append(headText);
  box.append(head);

  box.append(premiumList("explain"));

  const cta = el("button", "btn gold");
  const label = el("div");
  label.append(el("div", "", t("premium_open")));
  const small = el("div", "", t("premium_with"));
  small.style.cssText = "font-size:14px;font-weight:500;opacity:.9";
  label.append(small);
  cta.append(icons.crown(20), label);
  cta.onclick = openSubscribe;
  box.append(cta);
  box.append(el("p", "premium-foot", t("premium_cancel")));
  return box;
}

/** The "Why?" fallback. This one pays for a model call, which is why it is a deliberate
 *  tap rather than automatic. */
async function askWhy(button: HTMLButtonElement, questionId: number): Promise<void> {
  const run = state.run;
  if (!run?.verdict) return;
  button.disabled = true;
  button.textContent = t("preparing");
  try {
    const res = await api.explanation(questionId);
    run.verdict.explanation_state = res.explanation_state;
    run.verdict.explanation = res.explanation;
    run.verdict.free_explanations_left = res.free_explanations_left;
    if (state.me) state.me.free_explanations_left = res.free_explanations_left;
    render();
  } catch (err) {
    button.disabled = false;
    button.textContent = t("why");
    reportError(err);
  }
}


/** The review list: which questions you got wrong, and what the answer was.
 *
 *  The gap this fills was the largest one in the product. A candidate finished an exam,
 *  was told "11 errors / 3 allowed", and that was the end of it — no way to see WHICH
 *  eleven, no way to learn anything from having sat it. The moment a learner most wants
 *  the material is the moment they have just failed, and it was the one moment the app
 *  gave them nothing.
 *
 *  Defaults to mistakes only. Someone who got 27 right does not want to scroll past 27
 *  correct answers to find the three that matter, and the toggle is there for the rare
 *  case where they do.
 */
function reviewList(): HTMLElement {
  const r = state.results!;
  const wrap = el("section", "review");
  wrap.append(el("h3", "review-title", t("review_title")));

  const wrongOnly = state.reviewWrongOnly;
  const seg = el("div", "v-seg");
  for (const [only, label] of [[true, t("review_wrong_only")], [false, t("review_all")]] as const) {
    const b = el("button", `v-seg-btn ${wrongOnly === only ? "on" : ""}`, label);
    b.type = "button";
    b.onclick = () => { state.reviewWrongOnly = only; render(); };
    seg.append(b);
  }
  wrap.append(seg);

  // No client-side filtering by `given`. The server decides which items belong in a review
  // — only a GRADED EXAM carries the questions that were never reached, because only there
  // does a blank count against the candidate. Doing it here as well would be a second copy
  // of that rule, in the place where it cannot actually help: every item carries the correct
  // answer, so anything the client hides is still on the wire.
  //
  // `correct !== true` and not `!correct`: an unanswered question is null, and in a graded
  // exam that counts against you, so it belongs in the mistakes list too.
  const items = r.items.filter((i) => (wrongOnly ? i.correct !== true : true));
  if (items.length === 0) {
    wrap.append(el("p", "v-muted", t("review_none")));
    return wrap;
  }

  for (const item of items) {
    const card = el("div", `rev ${item.correct === true ? "ok" : "bad"}`);

    const head = el("div", "rev-head");
    head.append(el("span", "rev-n", String(item.ordinal)));
    const mark = el("span", "rev-mark");
    mark.append(item.correct === true ? icons.check(16) : icons.alert(16));
    head.append(mark);
    card.append(head);

    if (item.stem) card.append(el("p", "rev-stem", item.stem));
    card.append(el("p", "rev-q", item.statement));
    if (item.translation) card.append(el("p", "rev-tr", item.translation));

    if (item.image) {
      const plate = el("div", "rev-img");
      const img = el("img");
      img.src = api.figureUrl(item.image);
      img.loading = "lazy";
      img.alt = "";
      plate.append(img);
      card.append(plate);
    }

    const verdict = el("div", "rev-answers");
    const say = (v: boolean | null) =>
      v === null ? t("review_skipped") : v ? t("yes_short") : t("no_short");
    const yours = el("div", `rev-a ${item.correct === true ? "ok" : "bad"}`);
    yours.append(el("span", "rev-a-label", t("review_your")),
                 el("span", "rev-a-value", say(item.given)));
    verdict.append(yours);
    if (item.correct !== true) {
      const right = el("div", "rev-a ok");
      right.append(el("span", "rev-a-label", t("review_right")),
                   el("span", "rev-a-value", say(item.answer)));
      verdict.append(right);
    }
    card.append(verdict);

    // The explanation stays behind the same gate it always had — this screen shows the
    // ministerial content, which is free, and sells the reasoning, which is not.
    if (item.correct !== true) {
      const why = el("button", "rev-why", t("why"));
      why.type = "button";
      why.onclick = () => void explainFromReview(item.question_id, why);
      card.append(why);
    }

    wrap.append(card);
  }
  return wrap;
}

/** Fetch one explanation from the review screen, in place. */
async function explainFromReview(questionId: number, button: HTMLButtonElement): Promise<void> {
  button.disabled = true;
  // Say what is happening, as askWhy already does for the identical control inside a
  // practice run. An explanation can take seconds to generate; a button that only fades
  // out gives no reason to keep waiting, and the review screen is where a learner reads
  // several in a row.
  const label = button.textContent;
  button.textContent = t("preparing");
  try {
    const res = await api.explanation(questionId);
    const box = el("div", "rev-why-text");
    if (res.explanation_state === "shown" && res.explanation) {
      box.textContent = res.explanation;
      button.replaceWith(box);
    } else if (res.explanation_state === "locked") {
      button.replaceWith(premiumBlock());
    } else {
      box.textContent = t("explanation_unavailable");
      button.replaceWith(box);
    }
  } catch (err) {
    // Put the label back with the button. Re-enabling a button still reading "Preparing…"
    // leaves a control that looks busy but is not, and cannot be told apart from one that
    // is still working.
    button.disabled = false;
    button.textContent = label;
    reportError(err);
  }
}

function resultsScreen(): HTMLElement {
  const r = state.results!;
  const wrap = el("section", "screen");

  // `r.state === "abandoned"` and not `r.passed === null`: PRACTICE sittings are also null
  // — their max_errors is null so _grade never assigns one — and they need their own
  // branch, not this one. State is the fact; passed being null is a consequence of it.
  const dropped = r.mode === "exam" && r.state === "abandoned";

  // Only a graded EXAM is a pass or a fail. `const passed = r.passed === true` used to feed
  // the tone directly, so everything null landed in the fail branch — which meant a perfect
  // 10/10 practice round was drawn on a red card under a red cross, on every practice
  // sitting this app has ever finished. Practice is not something you fail.
  const graded = r.mode === "exam" && !dropped;
  const passed = r.passed === true;
  const esito = el("div", `esito ${graded ? (passed ? "pass" : "fail") : "dropped"}`);

  // The medallion. `.esito-mark` and its pass/fail tints have been in style.css from the
  // start and NO .ts file ever emitted the class, so the end of an exam — the moment this
  // product exists to deliver — was a coloured word and nothing else. Both icons default
  // to size 44, a size used nowhere else in the app: they were drawn for this 72-96px
  // circle and then never wired to it.
  const mark = el("div", "esito-mark");
  // The exit door, matching the button they pressed, rather than a clock — a clock on this
  // screen reads as "you ran out of time", which is a different outcome that IS graded.
  // Practice gets the target it has been aiming at, and neither gets a tick or a cross.
  mark.append(graded ? (passed ? icons.tick(44) : icons.cross(44))
                     : dropped ? icons.exit(44) : icons.target(44));
  esito.append(mark);

  // `esito-sub` below, not `esito-line`. style.css styles the former; the latter has no
  // rule anywhere in the project, so this sentence rendered at browser-default <p> size
  // and margins instead of the 600-weight body copy it was drawn as.
  if (dropped) {
    esito.append(el("p", "esito-verdict", t("not_counted")));
    esito.append(el("p", "esito-sub", t("not_counted_sub")));
  } else if (r.mode === "exam") {
    esito.append(el("p", "esito-verdict", passed ? t("passed") : t("failed")));
    esito.append(el("p", "esito-sub",
      `${r.wrong} ${t("errors").toLowerCase()} / ${r.max_errors} ${t("allowed").toLowerCase()}`));
  } else {
    esito.append(el("p", "esito-verdict display",
      `${r.answered - r.wrong}/${r.answered}`));
    esito.append(el("p", "esito-sub", t("answers_given")));
  }

  const tally = el("div", "tally");
  // `tally-n` / `tally-label`, the classes style.css actually defines. This emitted
  // "n display" and "label": there is no `.n` rule at all and `.display` sets weight and
  // tabular figures but NO font-size, so the three numbers a finished exam exists to
  // report — answered, errors, unanswered — came out at 16px body size instead of the
  // 30-40px they were drawn at, under captions rendered in near-black.
  const stat = (n: string | number, label: string) => {
    const d = el("div");
    d.append(el("div", "tally-n display", String(n)), el("div", "tally-label", label));
    return d;
  };
  tally.append(
    stat(r.answered, t("answered_n")),
    stat(r.wrong, t("errors")),
    // In an exam, what you left blank counts against you, so it is worth its own figure.
    // In practice `question_count - answered` is nothing but the unserved tail of the
    // last batch — not questions the learner skipped — so showing it as "unanswered"
    // reports slack in the fetching as if it were a failure. Show what they got right.
    // "Unanswered" is a failure figure: in a real exam a blank counts against you, which is
    // why a submitted or expired sitting reports it. An exited one did not reach those
    // questions at all, so reporting them as a third column would turn stopping early into
    // a score. Show what they got right instead, as practice does.
    r.mode === "exam" && !dropped
      ? stat(r.question_count - r.answered, t("unanswered"))
      : stat(r.answered - r.wrong, t("correct")),
  );
  esito.append(tally);
  wrap.append(esito);
  if (r.items?.length) {
    wrap.append(reviewList());
  } else {
    // Both dialogs that lead here promise a review — practice's says so outright and the
    // exam exit's says "you will see your answers". End a round having answered nothing and
    // the promised section simply was not drawn, leaving a bare 0/0 and no explanation.
    wrap.append(el("p", "esito-note", t("nothing_to_review")));
  }

  const again = el("button", "btn primary", t("again"));
  again.onclick = () => void startRun(r.mode);
  wrap.append(again);

  // `secondary`, the variant that exists. `ghost` was emitted here and defined nowhere —
  // the only such class in the file — so "back home" rendered as a bare .btn: no border,
  // no background, indistinguishable from the primary button stacked directly above it.
  const home = el("button", "btn secondary", t("back_home"));
  home.onclick = () => { state.screen = "home"; render(); };
  wrap.append(home);
  return wrap;
}

/** Today's goal, and the streak it is feeding.
 *
 * The daily goal is the part that has to be on screen. A streak whose rule is invisible is
 * one people only learn by losing it — they answered five questions, felt they had studied,
 * and the number reset overnight with nothing to explain why. So the progress toward today
 * is shown before the streak itself, and the goal comes from the server rather than being
 * repeated here, because two copies of a product rule disagree the moment one is tuned.
 */
function streakCard(p: Profile): HTMLElement {
  const goal = Math.max(1, p.streak_goal);
  const done = Math.min(p.streak_today, goal);
  const met = done >= goal;

  const card = el("div", "card streak");
  const head = el("div", "streak-head");
  const flame = el("div", met || p.streak_days > 0 ? "streak-flame on" : "streak-flame");
  flame.textContent = "🔥";
  const headText = el("div", "streak-headtext");
  headText.append(el("div", "streak-count",
    p.streak_days > 0 ? `${p.streak_days} ${t("streak_days")}` : t("streak_none")));
  headText.append(el("div", "streak-sub",
    met ? t("streak_done_today") : t("streak_left_today", { n: goal - done })));
  head.append(flame, headText);

  // Freezes are shown only when held. A "❄️ 0" is a reminder of something you do not have.
  if (p.streak_freezes > 0) {
    const freeze = el("div", "streak-freeze");
    freeze.title = t("streak_freeze_hint");
    freeze.textContent = `❄️ ${p.streak_freezes}`;
    head.append(freeze);
  }
  card.append(head);

  // One pip per question, not a continuous bar: at a goal of ten, "how many more" is a
  // number people should be able to see without reading it.
  const pips = el("div", "streak-pips");
  for (let i = 0; i < goal; i++) pips.append(el("i", i < done ? "pip on" : "pip"));
  card.append(pips);
  card.append(el("div", "streak-goal", t("streak_goal_line", { done, goal })));
  return card;
}

function profileScreen(): HTMLElement {
  const wrap = el("section", "screen");
  const p = state.profile;
  if (!p) {
    wrap.append(el("div", "spinner"));
    void loadProfile();
    return wrap;
  }

  // Identity is from initDataUnsafe — the UNVERIFIED copy — and is used for DISPLAY only.
  // Anything that matters is keyed off the signed blob, server-side.
  const tgUser = tg?.initDataUnsafe?.user;
  const name = (tgUser?.first_name ?? "").trim();

  const who = el("div", "who");
  const avatar = el("div", "avatar");
  if (tgUser?.photo_url) {
    const img = el("img");
    img.src = tgUser.photo_url;
    img.alt = "";
    avatar.append(img);
  } else {
    avatar.textContent = (name[0] ?? "?").toUpperCase();
  }
  const whoText = el("div");
  whoText.append(el("div", "who-name", name || t("profile")));
  if (p.streak_days > 0) {
    whoText.append(el("div", "who-streak", `🔥 ${p.streak_days} ${t("streak_days")}`));
  }
  const gear = el("button", "who-gear");
  gear.append(icons.gear(24));
  gear.onclick = () => { state.screen = "settings"; render(); };
  who.append(avatar, whoText, gear);
  wrap.append(who);
  wrap.append(streakCard(p));

  // --- readiness ---
  const card = el("div", "card gauge-wrap");
  card.append(el("div", "label gauge-label", t("ready_title")));
  card.append(readinessGauge({ value: p.readiness, threshold: p.pass_accuracy }));

  if (p.readiness === null) {
    // The server refuses to estimate below a minimum sample. Rendering that refusal
    // matters: a 0% bar would read as "you know nothing" rather than "not enough data".
    card.append(el("div", "gauge-value", "—"));
    card.append(el("div", "gauge-empty", t("need_more", { n: p.readiness_min_sample })));
  } else {
    const pct = Math.round(p.readiness * 100);
    const value = el("div", "gauge-value");
    value.append(document.createTextNode(String(pct)), el("small", "", "%"));
    card.append(value);
    card.append(el("div", "gauge-sub", t("based_on", { n: p.readiness_sample })));

    const note = el("div", "gauge-note");
    note.append(icons.target(26));
    const noteText = el("div");
    noteText.append(el("b", "", t("pass_bar", { n: Math.round(p.pass_accuracy * 100) })));
    noteText.append(el("span", "", p.readiness >= p.pass_accuracy ? t("ready_yes") : t("ready_keep")));
    note.append(noteText);
    card.append(note);
  }
  wrap.append(card);

  // --- exam tallies ---
  const stats = el("div", "card");
  stats.style.marginTop = "var(--md)";
  const row = el("div", "stat-row");
  const stat = (icon: SVGSVGElement, label: string, value: string) => {
    const d = el("div");
    const ico = el("div", "stat-ico");
    ico.append(icon);
    d.append(ico, el("div", "stat-label", label), el("div", "stat-n", value));
    return d;
  };
  row.append(
    stat(icons.clipboardCheck(22), t("exams_taken"), String(p.exams.taken)),
    stat(icons.check(22), t("exams_passed"), String(p.exams.passed)),
    stat(icons.cross(20), t("avg_errors"), p.exams.avg_errors == null ? "—" : String(p.exams.avg_errors)),
  );
  stats.append(row);
  wrap.append(stats);

  // --- recent exams ---
  const history = el("div", "card");
  history.style.marginTop = "var(--md)";
  const head = el("div", "section-head");
  head.append(el("h2", "", t("history")));
  history.append(head);

  if (!p.exams.recent.length) {
    history.append(el("p", "caption", t("no_exams")));
  } else {
    for (const run of p.exams.recent) {
      const passed = run.passed === true;
      const r = el("div", "run-row");
      const mark = el("div", `run-mark ${passed ? "pass" : "fail"}`);
      mark.append(passed ? icons.tick(16) : icons.cross(14));
      r.append(mark);
      r.append(el("span", `run-badge ${passed ? "pass" : "fail"}`, passed ? t("passed") : t("failed")));
      const meta = el("div", "run-name");
      meta.append(el("span", "", run.finished_at
        ? new Date(run.finished_at).toLocaleDateString(lang(), { day: "numeric", month: "long", year: "numeric" })
        : ""));
      r.append(meta);
      const score = el("div", `run-score ${passed ? "pass" : "fail"}`);
      score.append(document.createTextNode(String(run.wrong)),
                   el("small", "", `/${run.question_count}`));
      r.append(score);
      history.append(r);
    }
  }
  wrap.append(history);

  // --- weak topics, or the promotion that points at them ---
  if (state.me && !state.me.premium) {
    wrap.append(premiumBlock());
  } else {
    const link = el("button", "link-row");
    link.style.marginTop = "var(--md)";
    link.append(icons.target(24));
    const body = el("div", "row-main");
    body.append(el("div", "row-title", t("by_topic")), el("div", "row-sub", t("by_topic_sub")));
    link.append(body);
    const chev = el("span", "chev");
    chev.append(icons.chevron(20));
    link.append(chev);
    link.onclick = () => { state.screen = "stats"; render(); };
    wrap.append(link);
  }
  return wrap;
}

async function loadProfile(): Promise<void> {
  try {
    state.profile = await api.profile();
    render();
  } catch (err) {
    reportError(err);
  }
}


/** Colour by severity, on a scale the whole screen shares: red is bad, green is good,
 *  and the thresholds are the same for a topic bar and a Leitner box. */
function severity(rate: number): string {
  if (rate >= 0.45) return "var(--bad)";
  if (rate >= 0.30) return "#f97316";
  if (rate >= 0.15) return "#eab308";
  return "var(--ok)";
}

const BOX_TINT = ["var(--exam-tint)", "#fff7ed", "#fefce8", "var(--practice-tint)", "#ecfdf5"];
const BOX_INK = ["var(--bad)", "#ea580c", "#ca8a04", "var(--ok)", "var(--ok)"];

async function loadAnalysis(): Promise<void> {
  try {
    state.analysis = await api.analysis();
    render();
  } catch (err) {
    reportError(err);
  }
}

/** Where the marks are going.
 *
 *  The stats screen already showed an error rate. It was measured over all time, so it
 *  barely moved once a learner had a few hundred answers behind them, and the topic list
 *  under it was sorted by error rate — which puts "2 wrong out of 3" above "120 wrong out of
 *  300". This screen answers the question the number raises: which of these is actually
 *  costing me the exam.
 *
 *  Everything here can refuse to answer. Below 100 answers there is no percentage, and a
 *  family with fewer than ten questions behind it says "not tested yet" rather than
 *  inventing a rate. That makes the COLD START the important case, not an edge case: on day
 *  one almost every row is untested, and without the progress line and the coverage bars
 *  this reads as a broken screen rather than an honest one.
 */
function analysisScreen(): HTMLElement {
  const wrap = el("section", "screen");
  const a = state.analysis;
  if (!a) {
    wrap.append(el("div", "spinner"));
    void loadAnalysis();
    return wrap;
  }

  wrap.append(el("h1", "h1", t("where_errors")));

  // --- the headline ---------------------------------------------------------
  const head = el("div", "card an-head");
  if (a.predicted_mistakes === null) {
    // Nothing measurable yet. Say what is missing rather than printing a zero, and say how
    // far off it is — "47 / 100" is the only thing on this screen a new learner can act on.
    head.append(el("div", "an-big", `${a.headline.sample} / ${a.headline.min_sample}`));
    head.append(el("p", "an-sub", t("an_need_more")));
  } else {
    const pass = a.predicted_mistakes <= a.exam_max_errors;
    head.classList.add(pass ? "ok" : "bad");
    head.append(el("div", "an-big", a.predicted_mistakes.toFixed(1)));
    head.append(el("p", "an-sub",
      t("an_predicted", { total: a.exam_questions, allowed: a.exam_max_errors })));
    // Never printed as a plain exam prediction: practice deliberately re-serves what you
    // got wrong, so this is an upper bound on the questions the learner has actually met,
    // and it speaks for only part of the paper until they have covered more of it.
    head.append(el("p", "an-caveat",
      t("an_covers", { pct: Math.round(a.predicted_covers * 100) })));
  }
  if (a.headline.rate !== null) {
    head.append(el("p", "an-rate",
      t("an_recent", { pct: Math.round(a.headline.rate * 100),
                       n: a.headline.min_sample })));
  }
  head.append(el("p", "an-lifetime",
    t("an_lifetime", { n: a.headline.lifetime_answers })));
  wrap.append(head);

  // --- the families ---------------------------------------------------------
  const list = el("div", "an-list");
  for (const f of a.families) {
    const row = el("div", `an-row ${f.enough ? "" : "untested"}`);

    const top = el("div", "an-row-top");
    top.append(el("div", "an-name", t(`fam_${f.family}` as Key)));
    top.append(el("div", "an-cost", f.predicted_mistakes === null
      ? t("an_untested")
      : t("an_marks", { n: f.predicted_mistakes.toFixed(1) })));
    row.append(top);

    row.append(el("p", "an-meta", t("an_share", {
      per: f.per_exam.toFixed(1), total: a.exam_questions,
      rate: f.error_rate === null ? "—" : `${Math.round(f.error_rate * 100)}%`,
    })));

    // Coverage, on every row and never optional: 0% errors on 12 of 662 information-sign
    // questions is not mastery, and the ranking would otherwise present it as a strength.
    const bar = el("div", "an-bar");
    const fill = el("div", "an-bar-fill");
    fill.style.width = `${Math.max(1, Math.round(f.coverage * 100))}%`;
    bar.append(fill);
    row.append(bar);
    row.append(el("p", "an-cov", t("an_coverage", {
      seen: f.answered, total: f.questions_in_bank,
      pct: Math.round(f.coverage * 100),
    })));

    // THE POINT OF THE WHOLE SCREEN, and it was missing.
    //
    // This screen ranks seven subjects by how many marks each is costing the learner, and
    // then offered nothing to do about any of them. A diagnosis with no treatment is a
    // screen people read once. One tap now starts practice on exactly the row they are
    // looking at.
    const drill = el("button", "an-drill");
    drill.type = "button";
    drill.textContent = t("an_practise");
    drill.onclick = () => void startRun("practice", "smart", f.family);
    row.append(drill);

    list.append(row);
  }
  wrap.append(list);

  // The AI layer, under the numbers rather than over them. It never gates the screen: if
  // the model is slow, refuses, or is not configured, everything above still renders and
  // this is the only part that says so.
  wrap.append(coachBlock());
  return wrap;
}

/** "get analysis then Ai model will give advices in language of mini app".
 *
 *  Asked for rather than loaded. It may spend money, so nothing fetches it on entering the
 *  screen — and a learner who never taps it costs nothing at all.
 */
function coachBlock(): HTMLElement {
  const box = el("div", "coach");
  const c = state.coach;

  if (!c || c.state === "unavailable") {
    box.append(el("p", "coach-lead", t("coach_lead")));
    const ask = el("button", "btn primary", t("coach_ask"));
    ask.type = "button";
    ask.disabled = state.coachBusy;
    if (state.coachBusy) ask.textContent = t("coach_thinking");
    ask.onclick = () => void askCoach();
    box.append(ask);
    if (c?.state === "unavailable") box.append(el("p", "coach-note", t("coach_unavailable")));
    return box;
  }

  if (c.state === "locked" || c.state === "too_early" || c.state === "monthly_cap") {
    box.append(el("p", "coach-lead", t("coach_lead")));
    box.append(el("p", "coach-note", t(`coach_${c.state}` as Key)));
    if (c.state === "locked" && state.me && !state.me.premium) box.append(premiumBlock());
    return box;
  }

  // ready, or cooldown with the previous one still attached — a learner inside the window
  // should re-read what they were given rather than stare at a locked button.
  if (c.summary) box.append(el("p", "coach-summary", c.summary));
  for (const f of c.focus) {
    const item = el("div", "coach-item");
    item.append(el("div", "coach-area", f.area), el("p", "coach-action", f.action));
    box.append(item);
  }
  if (c.habit) box.append(el("p", "coach-habit", c.habit));
  if (c.next_up) {
    // "advice a learner can't act on in one tap is advice they won't take."
    const go = el("button", "btn primary", t("coach_start"));
    go.type = "button";
    go.onclick = () => void startRun("practice");
    box.append(el("p", "coach-next", c.next_up), go);
  }
  if (c.state === "cooldown") box.append(el("p", "coach-note", t("coach_cooldown")));
  return box;
}

async function askCoach(): Promise<void> {
  state.coachBusy = true;
  render();
  try {
    state.coach = await api.coach();
  } catch (err) {
    reportError(err);
  } finally {
    state.coachBusy = false;
    render();
  }
}

function statsScreen(): HTMLElement {
  const wrap = el("section", "screen");
  if (!state.stats) {
    wrap.append(el("div", "spinner"));
    void loadStats();
    return wrap;
  }
  const s = state.stats;

  wrap.append(el("h1", "h1", t("stats_title")));
  wrap.append(el("p", "sub", t("stats_sub")));

  // --- headline tiles ---
  const tiles = el("div", "tiles");
  const tile = (icon: SVGSVGElement, tint: string, label: string, value: string, sub?: string) => {
    const d = el("div", "tile");
    const ico = el("div", "tile-ico");
    ico.style.background = tint;
    ico.append(icon);
    d.append(ico, el("div", "tile-label", label));
    const n = el("div", "tile-n");
    n.append(document.createTextNode(value));
    if (sub) n.append(el("small", "", sub));
    d.append(n);
    return d;
  };
  tiles.append(
    tile(icons.eye(20), "var(--tint-info)", t("questions_seen"), String(s.questions_seen), `/${s.questions_total}`),
    tile(icons.check(20), "var(--practice-tint)", t("answers_given"), String(s.answers_given)),
    tile(icons.target(20), "var(--exam-tint)", t("error_rate"), `${Math.round(s.error_rate * 100)}`, "%"),
  );
  wrap.append(tiles);

  // "Error rate when user will pres this button web app should show where he is making
  // errors". The tile above is the number; this is what to do about it.
  const dig = el("button", "btn secondary");
  dig.type = "button";
  dig.style.marginTop = "var(--md)";
  dig.textContent = t("where_errors");
  dig.onclick = () => { state.screen = "analysis"; render(); };
  wrap.append(dig);

  // --- spaced repetition ---
  const boxCard = el("div", "card");
  boxCard.style.marginTop = "var(--md)";
  boxCard.append(el("h2", "", t("boxes")));
  boxCard.append(el("p", "caption", t("boxes_hint")));

  const boxes = el("div", "boxes");
  for (let i = 1; i <= 5; i++) {
    const box = el("div", "box");
    box.style.background = BOX_TINT[i - 1]!;
    const n = el("div", "box-n", String(i));
    n.style.cssText = `background:var(--card);color:${BOX_INK[i - 1]}`;
    box.append(n);
    box.append(el("div", "box-label", t(`box${i}` as never)));
    const count = el("div", "box-c", String(s.boxes[String(i)] ?? 0));
    count.style.color = BOX_INK[i - 1]!;
    box.append(count);
    boxes.append(box);
  }
  boxCard.append(boxes);

  // A left-to-right gradient under the boxes, so "1 -> 5" reads as a direction of travel
  // rather than as five unrelated buckets.
  const scale = el("div", "box-scale");
  for (const colour of ["var(--bad)", "#f97316", "#eab308", "#4ade80", "var(--ok)"]) {
    const seg = el("i");
    seg.style.background = colour;
    scale.append(seg);
  }
  boxCard.append(scale);
  const ends = el("div", "box-scale-ends");
  const weak = el("span", "", t("weak")); weak.style.color = "var(--bad)";
  const strong = el("span", "", t("strong")); strong.style.color = "var(--ok)";
  ends.append(weak, strong);
  boxCard.append(ends);
  wrap.append(boxCard);

  // --- by topic ---
  const topicCard = el("div", "card");
  topicCard.style.marginTop = "var(--md)";
  topicCard.append(el("h2", "", t("by_topic")));
  topicCard.append(el("p", "caption", t("topics_sub")));

  const weakest = [...s.by_topic].sort((a, b) => b.error_rate - a.error_rate).slice(0, 6);
  if (!weakest.length) {
    topicCard.append(el("p", "caption", t("no_topics")));
  } else {
    for (const topic of weakest) {
      const pct = Math.round(topic.error_rate * 100);
      const colour = severity(topic.error_rate);
      const row = el("div", "topic-row");

      const ico = el("div", "topic-ico");
      ico.style.background = `color-mix(in srgb, ${colour} 12%, #fff)`;
      ico.style.color = colour;
      ico.append(icons.lane(20));
      row.append(ico);

      const main = el("div", "topic-main");
      // Ministerial topic names run to 250 characters; the first clause is the
      // recognisable part, exactly as the bot does it.
      const short = topic.topic.split(";")[0]!.split(" - ")[0]!.trim();
      main.append(el("div", "topic-name", short));
      main.append(el("div", "topic-desc",
        `${topic.wrong}/${topic.answers_given} · ${topic.questions_seen}`));
      row.append(main);

      const right = el("div", "topic-right");
      const p = el("div", "topic-pct", `${pct}%`);
      p.style.color = colour;
      right.append(p);
      const bar = el("div", "topic-bar");
      const fill = el("i");
      fill.style.width = `${Math.min(100, pct)}%`;
      fill.style.background = colour;
      bar.append(fill);
      right.append(bar);
      row.append(right);
      topicCard.append(row);
    }
  }
  wrap.append(topicCard);

  if (state.me && !state.me.premium) wrap.append(premiumBlock());

  const tip = el("div", "tip");
  tip.append(icons.bulb(24));
  const body = el("div");
  body.append(el("h3", "", t("tip_title")), el("p", "", t("tip_stats")));
  tip.append(body);
  wrap.append(tip);
  return wrap;
}

async function loadStats(): Promise<void> {
  try {
    state.stats = await api.stats();
    render();
  } catch (err) {
    reportError(err);
  }
}

const LANG_NAMES: Record<string, string> = {
  it: "Italiano", ru: "Русский", en: "English", uz: "O\u2018zbekcha",
};
// Uzbek ships as UI and question translations but NOT explanations, and its copy has not
// been read by a native speaker. The badge is the honest label for that, and it comes off
// when someone has reviewed it — not when the code stops changing.
const BETA_LANGS = new Set(["uz"]);


/** Start over.
 *
 *  Two steps, and the second one names numbers. "This will delete your progress" is a
 *  sentence people click through; "this deletes 412 answers and 6 exams" is one they
 *  read. The preview is fetched rather than guessed, so the numbers are true.
 *
 *  It also says what SURVIVES, because the fear that stops someone pressing a destructive
 *  button is usually the wrong fear — here it is "will I lose my subscription", and the
 *  answer is no.
 */
function resetRow(): HTMLElement {
  const wrap = el("div", "card reset-card");
  const head = el("div", "row-main");
  head.append(el("div", "row-title", t("reset_title")),
              el("div", "row-sub", t("reset_sub")));
  wrap.append(head);

  const open = el("button", "reset-open", t("reset_title"));
  open.type = "button";
  open.onclick = () => void beginReset(wrap, open);
  wrap.append(open);
  return wrap;
}

async function beginReset(card: HTMLElement, opener: HTMLButtonElement): Promise<void> {
  opener.disabled = true;
  let preview: ResetPreview;
  try {
    preview = await api.resetPreview();
  } catch (err) {
    opener.disabled = false;
    reportError(err);
    return;
  }

  const confirm = el("div", "reset-confirm");
  confirm.append(el("p", "reset-what", t("reset_what", {
    answers: preview.answers, sittings: preview.sittings, words: preview.words,
  })));
  // The reassurance sits directly under the warning, not in a help page nobody opens.
  confirm.append(el("p", "reset-keep", t("reset_keep")));

  const row = el("div", "reset-actions");
  const no = el("button", "reset-no", t("reset_cancel"));
  no.type = "button";
  no.onclick = () => { confirm.remove(); opener.disabled = false; };

  const yes = el("button", "reset-yes", t("reset_confirm"));
  yes.type = "button";
  yes.onclick = async () => {
    yes.disabled = true;
    no.disabled = true;
    try {
      await api.resetProgress();
      // Everything on screen is now describing a past that no longer exists.
      state.profile = null;
      state.stats = null;
      state.results = null;
      state.run = null;
      state.resumable = null;
      state.me = await api.me();
      toast(t("reset_done"));
      state.screen = "home";
      render();
    } catch (err) {
      yes.disabled = false;
      no.disabled = false;
      reportError(err);
    }
  };
  row.append(no, yes);
  confirm.append(row);
  card.append(confirm);
}

/** The suggestion form.
 *
 *  This was a link to the support chat, which was the wrong trade: it asks a learner to
 *  compose a message to a stranger — which almost nobody does — and it drops "add a dark
 *  mode" into the same inbox as "my payment failed". A form asks for one thing, in the
 *  language the app is already speaking.
 *
 *  Sent state rather than a toast. A toast fades in three seconds and leaves the person
 *  looking at the same empty box wondering whether it went; the card says it arrived and
 *  offers to take another. */
function openSuggestion(): void {
  const scrim = el("div", "modal");
  const card = el("div", "modal-card");
  const close = () => { scrim.remove(); setBackButton(backTarget()); };

  const draw = () => {
    card.replaceChildren();
    card.append(el("h3", "sheet-title", t("suggest_title")));
    card.append(el("p", "sheet-sub", t("suggest_form_sub")));

    const box = el("textarea", "suggest-box");
    box.rows = 5;
    box.maxLength = 1000;
    box.placeholder = t("suggest_placeholder");
    card.append(box);

    const send = el("button", "btn primary", t("suggest_send"));
    send.type = "button";
    send.onclick = async () => {
      const text = box.value.trim();
      if (!text) { box.focus(); return; }
      send.disabled = box.disabled = true;
      try {
        await api.suggest(text);
        card.replaceChildren();
        const mark = el("div", "suggest-done");
        mark.append(icons.tick(44));
        card.append(mark);
        card.append(el("h3", "sheet-title", t("suggest_thanks")));
        card.append(el("p", "sheet-sub", t("suggest_thanks_sub")));
        const again = el("button", "btn secondary", t("suggest_another"));
        again.type = "button";
        again.onclick = draw;
        card.append(again);
        const done = el("button", "link-btn", t("close"));
        done.type = "button";
        done.onclick = close;
        card.append(done);
      } catch (err) {
        send.disabled = box.disabled = false;
        reportError(err);
      }
    };
    card.append(send);
    queueMicrotask(() => box.focus());
  };

  draw();
  scrim.append(card);
  scrim.onclick = (ev) => { if (ev.target === scrim) close(); };
  setBackButton(close);
  document.body.append(scrim);
}

function settingsScreen(): HTMLElement {
  const wrap = el("section", "screen");
  const me = state.me;
  if (!me) { wrap.append(el("div", "spinner")); return wrap; }

  wrap.append(el("h1", "h1", t("settings")));
  wrap.append(el("p", "sub", t("settings_sub")));

  // --- "what should we add?" ------------------------------------------------
  //
  // At the top, above the settings themselves, because it is not one. The people who know
  // what is missing from a study app are the people studying for the exam this month, and
  // the only route they had was finding the support handle buried at the bottom of this
  // screen.
  //
  // It opens the existing support chat rather than a form. A form needs a table, a queue,
  // a moderation story and somewhere for the owner to read it; a Telegram message needs
  // none of that and arrives somewhere he already looks. If the volume ever justifies a
  // form, the button stays where it is and only its handler changes.
  const suggest = el("button", "card suggest");
  suggest.type = "button";
  const suggestMain = el("div", "row-main");
  suggestMain.append(el("div", "row-title", t("suggest_title")),
                     el("div", "row-sub", t("suggest_sub")));
  suggest.append(icons.bulb(24), suggestMain);
  const suggestChev = el("span", "chev");
  suggestChev.append(icons.chevron(20));
  suggest.append(suggestChev);
  suggest.onclick = openSuggestion;
  wrap.append(suggest);

  // --- language ---
  const langCard = el("div", "card");
  const langHead = el("div", "row");
  const langText = el("div", "row-main");
  langText.append(el("div", "row-title", t("language")), el("div", "row-sub", t("lang_sub")));
  langHead.append(langText);
  const globe = el("span");
  globe.style.color = "var(--text-3)";
  globe.append(icons.globe(22));
  langHead.append(globe);
  langCard.append(langHead);

  const grid = el("div", "lang-grid");
  grid.style.marginTop = "var(--lg)";
  for (const code of ["it", "ru", "en", "uz"] as const) {
    const button = el("button", `lang ${me.lang === code ? "on" : ""}`);
    button.type = "button";
    button.append(el("div", "lang-code", code.toUpperCase()));
    button.append(el("div", "lang-name", LANG_NAMES[code] ?? code));
    if (me.lang === code) {
      const tick = el("span", "lang-tick");
      tick.append(icons.tick(12));
      button.append(tick);
    } else if (BETA_LANGS.has(code)) {
      button.append(el("span", "lang-beta", "beta"));
    }
    button.onclick = async () => {
      try {
        state.me = await api.settings({ lang: code });
        setLang(state.me.lang);
        document.documentElement.lang = lang();
        dropLocalisedCaches();
        render();
      } catch (err) { reportError(err); }
    };
    grid.append(button);
  }
  langCard.append(grid);
  wrap.append(langCard);

  // --- translations ---
  const trCard = el("div", "card");
  trCard.style.marginTop = "var(--md)";
  const trRow = el("div", "row");
  const trText = el("div", "row-main");
  trText.append(el("div", "row-title", t("translations")), el("div", "row-sub", t("tr_sub")));
  trRow.append(trText);

  const toggle = el("button", `switch ${me.translations_on ? "on" : ""}`);
  toggle.type = "button";
  toggle.setAttribute("role", "switch");
  toggle.setAttribute("aria-checked", String(me.translations_on));
  toggle.onclick = async () => {
    try {
      state.me = await api.settings({ translations_on: !me.translations_on });
      render();
    } catch (err) { reportError(err); }
  };
  trRow.append(toggle);
  trCard.append(trRow);

  const note = el("div", "note");
  note.append(icons.info(20));
  note.append(el("span", "", t("tr_note")));
  trCard.append(note);
  wrap.append(trCard);

  // --- the weekly league ---
  //
  // The switch that makes showing real first names to other learners defensible at all. It
  // has to be findable, and it has to work retroactively — which it does, because the
  // ranking query filters on the column every time it runs rather than at the week's end.
  const lbCard = el("div", "card");
  lbCard.style.marginTop = "var(--md)";
  const lbRow = el("div", "row");
  const lbText = el("div", "row-main");
  lbText.append(el("div", "row-title", t("ratings_visible")),
                el("div", "row-sub", t("ratings_visible_sub")));
  lbRow.append(lbText);

  // Phrased as "show me", never "hide me". A switch that is ON when the thing is OFF is
  // how people end up with their privacy setting backwards.
  const visible = !me.leaderboard_opt_out;
  const lbToggle = el("button", `switch ${visible ? "on" : ""}`);
  lbToggle.type = "button";
  lbToggle.setAttribute("role", "switch");
  lbToggle.setAttribute("aria-checked", String(visible));
  lbToggle.onclick = () => void setLeaderboardOptOut(visible);
  lbRow.append(lbToggle);
  lbCard.append(lbRow);
  wrap.append(lbCard);

  // --- day or night ---
  //
  // Follows Telegram by default and remembers an override on this device. Placed here
  // rather than hidden behind a menu because it is the setting people go looking for, and
  // stored locally rather than on the account: it describes this screen, not this person.
  const themeCard = el("div", "card");
  themeCard.style.marginTop = "var(--md)";
  const themeRow = el("div", "row");
  const themeText = el("div", "row-main");
  themeText.append(el("div", "row-title", t("dark_mode")),
                   el("div", "row-sub", t("dark_mode_sub")));
  themeRow.append(themeText);

  const dark = document.documentElement.dataset.theme === "dark";
  const themeToggle = el("button", `switch ${dark ? "on" : ""}`);
  themeToggle.type = "button";
  themeToggle.onclick = () => {
    const next: Theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    setTheme(next);
    render();
  };
  themeRow.append(themeToggle);
  themeCard.append(themeRow);
  wrap.append(themeCard);

  // --- the owner's console ---
  //
  // Cosmetic gate, deliberately. Every endpoint behind it 404s for anyone who is not staff,
  // decided server-side from a Telegram-signed payload — hiding the entry point only stops
  // a learner tripping over a screen that would give them nothing but errors.
  //
  // In Settings rather than the tab bar: the bar is already five items and a sixth for one
  // person would cost every learner a thumb-width of the screen they actually use.
  if (isStaff()) {
    const adminCard = el("div", "card");
    adminCard.style.marginTop = "var(--md)";
    const adminRow = el("div", "row");
    const adminText = el("div", "row-main");
    adminText.append(el("div", "row-title", "Admin"),
                     el("div", "row-sub", "Grant access, trial links, newsletter"));
    adminRow.append(adminText);
    const go = el("button", "btn secondary", "Open");
    go.type = "button";
    go.onclick = () => void openAdmin();
    adminRow.append(go);
    adminCard.append(adminRow);
    wrap.append(adminCard);
  }

  // --- subscription ---
  if (me.premium) {
    const sub = el("div", "card sub-active");
    sub.style.marginTop = "var(--md)";
    const badge = el("span", "sub-badge");
    badge.append(icons.crown(14), document.createTextNode(t("sub_active")));
    sub.append(badge);
    sub.append(el("div", "row-title", t("sub_active")));
    sub.append(el("div", "row-sub", t("sub_active_sub")));
    if (me.pass_expires_at) {
      const until = el("div", "sub-until");
      until.append(icons.clipboard(18));
      const when = new Date(me.pass_expires_at).toLocaleDateString(lang(), {
        day: "numeric", month: "long", year: "numeric",
      });
      until.append(document.createTextNode(`${t("sub_until")} `), el("b", "", when));
      sub.append(until);
    }
    wrap.append(sub);
  } else {
    const promo = premiumBlock();
    promo.style.marginTop = "var(--md)";
    wrap.append(promo);
  }

  // Payment and support both happen in the chat — plan §6.2: a Mini App selling digital
  // goods sits closer to the Stars-only rule and to Apple's review guidelines.
  const tg = el("button", "link-row");
  tg.style.marginTop = "var(--md)";
  const send = el("span");
  send.style.color = "var(--accent)";
  send.append(icons.send(26));
  tg.append(send);
  const tgBody = el("div", "row-main");
  tgBody.append(el("div", "row-title", t("open_tg")), el("div", "row-sub", t("open_tg_sub")));
  tg.append(tgBody);
  const chev = el("span", "chev");
  chev.append(icons.chevron(20));
  tg.append(chev);
  tg.onclick = openSupport;
  wrap.append(tg);

  wrap.append(resetRow());

  return wrap;
}

// ---------------------------------------------------------------------------
// shell
// ---------------------------------------------------------------------------

/** Used only when Telegram gives us no header back button. Mirrors it rather than
 *  duplicating it, so no client shows two back controls. */
function fallbackBack(handler: () => void): HTMLElement {
  const row = el("div");
  row.style.marginBottom = "var(--md)";
  const button = el("button", "link-btn");
  const chevron = icons.chevron(20);
  chevron.style.transform = "rotate(180deg)";
  button.append(chevron, document.createTextNode(t("back_home")));
  button.onclick = handler;
  row.append(button);
  return row;
}


// ---------------------------------------------------------------------------
// vocabulary
// ---------------------------------------------------------------------------

/** Enter the trainer. Stats are fetched even when the round is refused, because the
 *  progress line is free and is what makes the paywall persuasive rather than blunt. */
async function openVocab(): Promise<void> {
  state.screen = "vocab";
  state.vocab = { ...state.vocab, view: "test", round: null, index: 0, current: null,
                  right: 0, typed: "", cards: null, cardIndex: 0, flipped: false, knew: 0,
                  locked: false };

  // The word list is language-dependent CONTENT, not just labels — the server picks the
  // gloss column from `user.lang`. The spread above deliberately keeps `list` so returning
  // to this screen is instant, which is right only while the language has not moved under
  // it. `dropLocalisedCaches` handles the in-app switch; this catches every other route to
  // the same state, including changing the language from the bot with the app still open.
  if (state.vocab.list && state.vocab.list.lang !== state.me?.lang) {
    state.vocab = { ...state.vocab, list: null, query: "" };
  }

  render();
  void loadVocabStats();
  await startVocabRound();
}

async function loadVocabStats(): Promise<void> {
  try {
    state.vocab.stats = await vocab.stats();
    render();
  } catch {
    /* the progress line is decoration; losing it must not break the screen */
  }
}

async function startVocabRound(): Promise<void> {
  state.vocab.busy = true;
  render();
  try {
    state.vocab.round = await vocab.round();
    state.vocab.index = 0;
    state.vocab.right = 0;
    state.vocab.current = null;
    state.vocab.typed = "";
    state.vocab.locked = false;
  } catch (err) {
    if (err instanceof ApiError && err.status === 402) state.vocab.locked = true;
    else reportError(err);
  } finally {
    state.vocab.busy = false;
    render();
  }
}

/** Deal a fresh deck. Separate from `startVocabRound` because the two rounds coexist:
 *  the server draws each independently and losing one to open the other would punish
 *  curiosity. */
async function startVocabCards(): Promise<void> {
  state.vocab.busy = true;
  render();
  try {
    state.vocab.cards = await vocab.cards();
    state.vocab.cardIndex = 0;
    state.vocab.knew = 0;
    state.vocab.flipped = false;
    state.vocab.locked = false;
  } catch (err) {
    if (err instanceof ApiError && err.status === 402) state.vocab.locked = true;
    else reportError(err);
  } finally {
    state.vocab.busy = false;
    render();
  }
}

/** Answer a card by saying whether you knew it.
 *
 *  Advances immediately and posts in the background. A flashcard's whole appeal is speed —
 *  putting a network round trip between "I knew that" and the next card turns a deck into
 *  a series of waits. The schedule is the server's to keep; if the post fails the card
 *  simply keeps its old box, which is the safe direction to be wrong in. */
function gradeCard(knew: boolean): void {
  const v = state.vocab;
  const item = v.cards?.items[v.cardIndex];
  if (!item || !v.flipped) return;

  if (knew) v.knew += 1;
  haptic(knew ? "success" : "error");
  void vocab.recall(item.term_id, knew).catch(() => {
    /* deliberately silent: see above. Reporting it would interrupt a deck mid-flow over
       something the learner cannot act on. */
  });

  v.cardIndex += 1;
  v.flipped = false;
  render();
  if (v.cardIndex >= (v.cards?.items.length ?? 0)) void loadVocabStats();
}

async function checkVocabAnswer(): Promise<void> {
  const v = state.vocab;
  const item = v.round?.items[v.index];
  if (!item || v.busy || v.current) return;
  v.busy = true;
  render();
  try {
    v.current = await vocab.answer(item.term_id, item.direction, v.typed);
    // ALMOST counts, exactly as it does server-side: the learner produced the word and
    // missed the ending. Scoring it as a miss would contradict the message shown.
    if (v.current.verdict !== "wrong") v.right += 1;
  } catch (err) {
    if (err instanceof ApiError && err.status === 402) v.locked = true;
    else reportError(err);
  } finally {
    v.busy = false;
    render();
  }
}

function nextVocabItem(): void {
  const v = state.vocab;
  v.index += 1;
  v.current = null;
  v.typed = "";
  render();
  if (v.index >= (v.round?.items.length ?? 0)) void loadVocabStats();
}

async function loadVocabList(query: string): Promise<void> {
  state.vocab.query = query;
  state.vocab.busy = true;
  render();
  try {
    state.vocab.list = await vocab.terms({ q: query, limit: 100 });
    state.vocab.locked = false;
  } catch (err) {
    if (err instanceof ApiError && err.status === 402) state.vocab.locked = true;
    else reportError(err);
  } finally {
    state.vocab.busy = false;
    render();
  }
}

function vocabScreen(): HTMLElement {
  const wrap = el("section", "screen");
  const v = state.vocab;

  const head = el("div", "v-head");
  head.append(el("h2", "v-title", t("v_title")), el("p", "v-sub", t("v_sub", { n: vocabSize() })));
  wrap.append(head);

  if (v.stats) {
    const bar = el("div", "v-progress");
    const fill = el("div", "v-progress-fill");
    const pct = v.stats.total ? Math.round((v.stats.learned / v.stats.total) * 100) : 0;
    fill.style.width = `${pct}%`;
    bar.append(fill);
    wrap.append(bar, el("p", "v-progress-label",
      t("v_progress", { learned: v.stats.learned, total: v.stats.total })));
  }

  const seg = el("div", "v-seg");
  const tabs = [["test", t("v_test")], ["cards", t("v_cards")], ["list", t("v_list")]] as const;
  for (const [id, label] of tabs) {
    const b = el("button", `v-seg-btn ${v.view === id ? "on" : ""}`, label);
    b.type = "button";
    b.onclick = () => {
      v.view = id;
      render();
      if (id === "list" && !v.list) void loadVocabList("");
      if (id === "test" && !v.round) void startVocabRound();
      if (id === "cards" && !v.cards) void startVocabCards();
    };
    seg.append(b);
  }
  wrap.append(seg);

  if (v.locked) { wrap.append(vocabLocked()); return wrap; }
  wrap.append(v.view === "test" ? vocabTest() : v.view === "cards" ? vocabCards() : vocabList());
  wrap.append(vocabCredit());
  return wrap;
}

/** Who compiled the word list.
 *
 *  The glossary is not ours. Zukhriddin Kamolov compiled it and gave permission
 *  to use it on the condition that he is credited as its author — so this is a term of use,
 *  not a courtesy, and it belongs on the screen the list is actually used on rather than
 *  buried in a settings page nobody opens.
 *
 *  Shown on BOTH tabs, and outside the `locked` branch above it only because a locked user
 *  sees no list at all; there is nothing to attribute until they do.
 *
 *  The name is not translated. It is a person's name.
 */
function vocabCredit(): HTMLElement {
  const p = el("p", "v-credit");
  p.append(document.createTextNode(`${t("v_credit")} `));
  const link = el("button", "v-credit-link", VOCAB_AUTHOR.name);
  link.type = "button";
  link.onclick = () => {
    if (!openChat(`https://t.me/${VOCAB_AUTHOR.handle}`)) {
      toast(`@${VOCAB_AUTHOR.handle}`);
    }
  };
  p.append(link);
  return p;
}

function vocabLocked(): HTMLElement {
  const card = el("div", "v-locked");
  card.append(el("div", "v-locked-title", t("v_locked")));
  if (state.me && !state.me.premium) card.append(premiumBlock());
  return card;
}

function vocabTest(): HTMLElement {
  const v = state.vocab;
  const box = el("div", "v-card");

  if (v.busy && !v.round) { box.append(el("p", "v-muted", "…")); return box; }
  if (!v.round || v.round.items.length === 0) {
    box.append(el("p", "v-muted", t("v_empty")));
    return box;
  }

  if (v.index >= v.round.items.length) return vocabSummary();

  const item = v.round.items[v.index]!;
  const counter = el("div", "v-counter", `${v.index + 1} / ${v.round.items.length}`);
  const direction = el("div", "v-direction",
    item.direction === "it_to_lang" ? t("v_direction_it") : t("v_direction_lang"));
  box.append(counter, direction, el("div", "v-prompt", item.prompt));

  const field = el("input", "v-input");
  field.type = "text";
  field.autocomplete = "off";
  field.autocapitalize = "off";
  field.spellcheck = false;
  field.placeholder = item.answer_lang === "it"
    ? t("v_placeholder_it") : t("v_placeholder_lang");
  field.value = v.typed;
  field.disabled = v.current !== null;
  field.oninput = () => { v.typed = field.value; };
  field.onkeydown = (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (v.current) nextVocabItem(); else void checkVocabAnswer();
  };
  box.append(field);

  if (v.current) box.append(vocabVerdict(v.current));

  const action = el("button", "v-action");
  action.type = "button";
  action.disabled = v.busy;
  action.textContent = v.current
    ? (v.index + 1 >= v.round.items.length ? t("v_finish") : t("v_next"))
    : t("v_check");
  action.onclick = () => { if (v.current) nextVocabItem(); else void checkVocabAnswer(); };
  box.append(action);

  // Focus after paint, and only while awaiting an answer — stealing focus once the
  // verdict is up would reopen the keyboard over the correction the learner is reading.
  if (!v.current) queueMicrotask(() => field.focus());
  return box;
}

function vocabVerdict(a: VocabAnswer): HTMLElement {
  const wrap = el("div", `v-verdict ${a.verdict}`);
  const label = { correct: t("v_correct"), almost: t("v_almost"), wrong: t("v_wrong") };
  wrap.append(el("div", "v-verdict-label", label[a.verdict]));

  // Shown for every verdict, including a correct one: seeing the pair again is the
  // repetition, and for `almost` it is the entire point.
  const pair = el("div", "v-pair");
  pair.append(el("span", "v-pair-it", a.it), el("span", "v-pair-sep", "—"),
              el("span", "v-pair-gloss", a.gloss));
  wrap.append(pair);

  if (a.verdict !== "correct") {
    wrap.append(el("div", "v-answer", `${t("v_answer_was")}: ${a.expected}`));
  }
  return wrap;
}

function vocabSummary(): HTMLElement {
  const v = state.vocab;
  const total = v.round?.items.length ?? 0;
  const box = el("div", "v-card v-summary");
  box.append(el("div", "v-summary-title", t("v_round_done")));
  box.append(el("div", "v-summary-score", t("v_score", { ok: v.right, total })));
  const again = el("button", "v-action", t("v_again"));
  again.type = "button";
  again.onclick = () => void startVocabRound();
  box.append(again);
  return box;
}

/** The flip-card trainer.
 *
 *  Italian on one side, the learner's language on the other, and which side leads is the
 *  server's mixed draw — recognising a word you are shown and producing one you are asked
 *  for are different skills, and only drilling the easy direction hides that.
 *
 *  The card itself is the button. Tapping anywhere on it flips it, which is the gesture
 *  people already expect from every flashcard app; the two verdict buttons appear only
 *  once the answer is visible, because grading yourself before looking is not a thing you
 *  can meaningfully do. */
function vocabCards(): HTMLElement {
  const v = state.vocab;
  const box = el("div", "v-card");

  if (v.busy && !v.cards) { box.append(el("p", "v-muted", "…")); return box; }
  if (!v.cards || v.cards.items.length === 0) {
    box.append(el("p", "v-muted", t("v_empty")));
    return box;
  }
  if (v.cardIndex >= v.cards.items.length) return vocabCardsSummary();

  const item = v.cards.items[v.cardIndex]!;
  box.append(el("div", "v-counter", `${v.cardIndex + 1} / ${v.cards.items.length}`));
  box.append(el("div", "v-direction",
    item.direction === "it_to_lang" ? t("v_direction_it") : t("v_direction_lang")));

  const card = el("button", `v-flip ${v.flipped ? "back" : "front"}`);
  card.type = "button";
  card.append(el("div", "v-flip-word", v.flipped ? (item.answer ?? "—") : item.prompt));
  card.append(el("div", "v-flip-hint", v.flipped ? t("v_card_grade") : t("v_card_tap")));
  card.onclick = () => {
    if (v.flipped) return;   // flipping back would let a tap undo a reveal by accident
    v.flipped = true;
    render();
  };
  box.append(card);

  if (v.flipped) {
    const row = el("div", "v-card-actions");
    const no = el("button", "v-card-btn no", t("v_card_no"));
    no.type = "button";
    no.onclick = () => gradeCard(false);
    const yes = el("button", "v-card-btn yes", t("v_card_yes"));
    yes.type = "button";
    yes.onclick = () => gradeCard(true);
    row.append(no, yes);
    box.append(row);
  }
  return box;
}

function vocabCardsSummary(): HTMLElement {
  const v = state.vocab;
  const total = v.cards?.items.length ?? 0;
  const box = el("div", "v-card v-summary");
  box.append(el("div", "v-summary-title", t("v_round_done")));
  // "You said you knew", not "you scored" — nobody marked this but the learner.
  box.append(el("div", "v-summary-score", t("v_card_score", { ok: v.knew, total })));
  const again = el("button", "v-action", t("v_again"));
  again.type = "button";
  again.onclick = () => void startVocabCards();
  box.append(again);
  return box;
}

/** Add a word, or change one already added.
 *
 *  One sheet for both, because they are the same three fields and a separate "edit" screen
 *  would be the same form with a different title. Delete lives here rather than as a swipe
 *  or a long-press: those are gestures a learner has to discover, and this list is read far
 *  more often than it is edited.
 */
function openOwnWord(existing: VocabTerm | null): void {
  const scrim = el("div", "modal");
  const card = el("div", "modal-card");
  const close = () => { scrim.remove(); setBackButton(backTarget()); };

  card.append(el("h3", "sheet-title", existing ? t("v_edit") : t("v_add")));
  card.append(el("p", "sheet-sub", t("v_add_sub")));

  const word = el("input", "v-field");
  word.type = "text";
  word.placeholder = t("v_add_it");
  word.autocapitalize = "off";
  word.spellcheck = false;
  word.value = existing?.it ?? "";
  card.append(word);

  const gloss = el("input", "v-field");
  gloss.type = "text";
  gloss.placeholder = t("v_add_gloss");
  gloss.value = existing?.gloss ?? "";
  card.append(gloss);

  const save = el("button", "btn primary", t("v_save"));
  save.type = "button";
  save.onclick = async () => {
    const it = word.value.trim();
    const meaning = gloss.value.trim();
    if (!it) { word.focus(); return; }
    if (!meaning) { gloss.focus(); return; }
    save.disabled = true;
    try {
      if (existing) await vocab.editTerm(existing.id, { it, gloss: meaning });
      else await vocab.addTerm(it, meaning);
      close();
      // Re-read rather than patching the list in place: the server decides the order, and
      // a new word belongs at the top of it.
      await loadVocabList(state.vocab.query);
      void loadVocabStats();
    } catch (err) {
      save.disabled = false;
      reportError(err);
    }
  };
  card.append(save);

  if (existing) {
    const remove = el("button", "btn secondary v-remove", t("v_remove"));
    remove.type = "button";
    remove.onclick = async () => {
      if (!(await ask(t("v_remove_confirm", { word: existing.it })))) return;
      try {
        await vocab.removeTerm(existing.id);
        close();
        await loadVocabList(state.vocab.query);
        void loadVocabStats();
      } catch (err) {
        reportError(err);
      }
    };
    card.append(remove);
  }

  scrim.append(card);
  scrim.onclick = (ev) => { if (ev.target === scrim) close(); };
  setBackButton(close);
  document.body.append(scrim);
  queueMicrotask(() => word.focus());
}

function vocabList(): HTMLElement {
  const v = state.vocab;
  const box = el("div", "v-list-wrap");

  const search = el("input", "v-search");
  search.type = "search";
  search.placeholder = t("v_search");
  search.value = v.query;
  // Debounced: a request per keystroke over a thousand-odd rows is a lot of round trips for a
  // list that is not changing under the user.
  let timer: number | undefined;
  search.oninput = () => {
    window.clearTimeout(timer);
    const q = search.value;
    timer = window.setTimeout(() => void loadVocabList(q), 250);
  };
  box.append(search);

  // "adding function should be in the first" — above the list, not below a thousand rows.
  const add = el("button", "v-add");
  add.type = "button";
  add.append(icons.plus(18), el("span", "", t("v_add")));
  add.onclick = () => openOwnWord(null);
  box.append(add);

  if (!v.list) { box.append(el("p", "v-muted", "…")); return box; }
  if (v.list.terms.length === 0) { box.append(el("p", "v-muted", t("v_empty"))); return box; }

  const list = el("div", "v-list");
  for (const term of v.list.terms) {
    const row = el("div", "v-row");
    const left = el("div", "v-row-main");
    left.append(el("div", "v-row-it", term.it), el("div", "v-row-gloss", term.gloss));
    row.append(left);
    if (term.mine) {
      // Only their own words are editable. A shared entry belongs to the compiler of the
      // glossary and is the same for everybody.
      row.classList.add("mine");
      const edit = el("button", "v-row-edit");
      edit.type = "button";
      edit.setAttribute("aria-label", t("v_edit"));
      edit.append(icons.pencil(16));
      edit.onclick = () => openOwnWord(term);
      row.append(edit);
    } else if (term.box >= 4) {
      const tick = el("span", "v-row-known", t("v_known"));
      row.append(tick);
    }
    list.append(row);
  }
  box.append(list);
  return box;
}


// ---------------------------------------------------------------------------
// the owner's console
// ---------------------------------------------------------------------------

/** Admin screens, reachable only by staff.
 *
 *  The gate here is cosmetic and deliberately so: every endpoint behind it 404s for anyone
 *  who is not staff, decided by the server from a Telegram-signed payload. Hiding the entry
 *  point stops a normal learner tripping over a screen that would only give them errors —
 *  it is NOT what makes the console safe. See api/routes/webapp_admin.py.
 */
function isStaff(): boolean {
  return state.me?.premium_via === "staff";
}

async function openAdmin(): Promise<void> {
  state.screen = "admin";
  state.adminData = { overview: null, users: [], links: [], reports: [], openReports: 0,
                      suggestions: [], openSuggestions: 0,
                      userTotal: 0, view: "home", query: "", segment: "", busy: true };
  render();
  await refreshAdmin();
}

async function refreshAdmin(): Promise<void> {
  if (!state.adminData) return;
  try {
    const [overview, users, links, reports, suggestions] = await Promise.all([
      admin.overview(),
      admin.users(state.adminData.query, state.adminData.segment),
      admin.links(),
      admin.reports(),
      admin.suggestions(),
    ]);
    state.adminData = {
      ...state.adminData,
      overview,
      users: users.users,
      userTotal: users.total ?? users.users.length,
      links: links.links,
      reports: reports.reports,
      openReports: reports.open,
      suggestions: suggestions.suggestions,
      openSuggestions: suggestions.open,
      busy: false,
    };
  } catch (err) {
    state.adminData = { ...state.adminData, busy: false };
    reportError(err);
  }
  render();
}

function adminScreen(): HTMLElement {
  const wrap = el("section", "screen");
  const data = state.adminData;

  const people = data?.view === "people";
  const head = el("div", "v-head");
  head.append(el("h2", "v-title", people ? "People" : "Admin"));
  wrap.append(head);

  if (!data || data.busy) {
    wrap.append(el("p", "caption", t("loading")));
    return wrap;
  }

  // People is its own page.
  //
  // Everything about one learner — finding them, granting, messaging, taking access back,
  // the group grant — belongs together and belongs OFF the page the owner opens. The main
  // screen answers "how is the product doing"; this one answers "what do I do about this
  // person", and they are different jobs done at different moments.
  if (people) {
    wrap.append(adminUsers(data.users));
    wrap.append(adminGrantMany());
    return wrap;
  }

  if (data.overview) {
    const o = data.overview;
    const grid = el("div", "admin-stats");
    for (const [label, value] of [
      ["Users", o.users], ["Premium", o.premium], ["On trial", o.on_trial],
      ["Active 24h", o.active_24h],
    ] as const) {
      const cell = el("div", "admin-stat");
      cell.append(el("div", "admin-stat-n", String(value)),
                  el("div", "admin-stat-l", label));
      grid.append(cell);
    }
    wrap.append(grid);

    // Coverage. Deliberately shown as "259 of 7,106" with the percentage, not a bare
    // percentage: 3.6% reads as a rounding error, while "259 of 7,106" is unmistakably a
    // bank that is almost entirely unwritten — which is the fact worth acting on.
    const c = o.content;
    if (c) {
      const card = el("div", "card");
      card.style.marginTop = "var(--md)";
      card.append(el("div", "row-title", "Content written"));

      const pct = (n: number, total: number) =>
        total ? `${((n / total) * 100).toFixed(1)}%` : "—";

      for (const [label, n, total, note] of [
        ["Questions translated", c.translated, c.questions_total, ""],
        ["Rules explained", c.explained, c.clusters_total, ""],
      ] as const) {
        const line = el("div", "cover");
        line.append(el("div", "cover-label", label));
        line.append(el("div", "cover-n",
          `${n.toLocaleString()} of ${total.toLocaleString()} · ${pct(n, total)}`));
        const bar = el("div", "cover-bar");
        const fill = el("div", "cover-fill");
        fill.style.width = `${total ? Math.max(1, (n / total) * 100) : 0}%`;
        bar.append(fill);
        line.append(bar);
        if (note) line.append(el("div", "caption", note));
        card.append(line);
      }

      // The number that matters. "100 of 3,382 rules · 3.0%" was true and misleading:
      // clusters are uneven, so those 100 already answer 1,157 questions.
      if (c.questions_covered) {
        card.append(el("p", "caption",
          `Those rules answer ${c.questions_covered.toLocaleString()} of `
          + `${c.questions_total.toLocaleString()} questions — `
          + `${((c.questions_covered / c.questions_total) * 100).toFixed(0)}% of what a `
          + `learner actually meets.`));
      }

      // Writing on purpose. The bank filled up by accident: whatever a handful of people
      // happened to open. Biggest clusters first, because that is where the coverage is.
      const more = el("button", "btn secondary", "Write 20 more");
      more.type = "button";
      more.style.marginTop = "var(--md)";

      // The batch takes minutes. Without a bar the only feedback is a toast and then
      // nothing, so the honest reading is that it silently failed — which is exactly what
      // a run that is quietly working looks like.
      const bar = el("div", "cover-bar");
      const fill = el("div", "cover-fill");
      bar.append(fill);
      const note = el("p", "caption", "");
      // `done` is counted from the DATABASE — how many of the requested clusters now hold
      // an explanation — so it survives a deploy and cannot disagree with the coverage
      // number directly above it. The gap between done and total at the end is the clusters
      // the model declined or a gate withheld, which is worth naming rather than hiding.
      const showProgress = (p: { total: number; done: number; running: boolean }) => {
        fill.style.width = `${p.total ? (p.done / p.total) * 100 : 0}%`;
        const missed = p.total - p.done;
        note.textContent = p.running
          ? `Writing… ${p.done} of ${p.total}`
          : `${p.done} written${missed > 0 ? `, ${missed} declined or withheld` : ""}.`;
        more.disabled = p.running;
      };

      let polling = 0;
      const poll = async () => {
        try {
          const p = await admin.contentProgress();
          showProgress(p);
          if (!p.running) {
            window.clearInterval(polling);
            // The coverage numbers above are now stale by exactly what was just written.
            await refreshAdmin();
          }
        } catch { window.clearInterval(polling); }
      };

      more.onclick = async () => {
        // Honest about the wait. "A few minutes" was written before the batch was
        // concurrency-bounded, when twenty releases at once queued behind the rate limit
        // and took the better part of an hour.
        if (!(await ask("Write explanations for the 20 biggest gaps? "
                        + "About 3-5 minutes, and it spends model calls. "
                        + "You can close this — it keeps going."))) return;
        more.disabled = true;
        try {
          const out = await admin.generateContent(20);
          card.append(bar, note);
          showProgress({ total: out.started, done: 0, running: true });
          window.clearInterval(polling);
          // Three seconds: a generation takes 10-30s, so anything faster is polling for
          // its own sake.
          polling = window.setInterval(() => void poll(), 3000);
        } catch (err) { more.disabled = false; reportError(err); }
      };
      card.append(more);

      // A batch already running when this screen opens — a reopened app, or a second
      // device. Without this the bar exists only for whoever pressed the button.
      void (async () => {
        try {
          const p = await admin.contentProgress();
          if (p.running) {
            card.append(bar, note);
            showProgress(p);
            polling = window.setInterval(() => void poll(), 3000);
          }
        } catch { /* the panel is useful without the bar */ }
      })();

      if (c.explanations_withheld || c.explanations_disputed) {
        card.append(el("p", "caption",
          `${c.explanations_withheld} rule(s) written but withheld by a quality gate · `
          + `${c.explanations_disputed} argue with the answer key`));
      }
      wrap.append(card);
    }
  }

  // Reports first. It is the only section that represents somebody waiting for an answer,
  // and a queue placed below three other cards is a queue that gets read once.
  wrap.append(adminMoney());
  wrap.append(adminReports(data.reports, data.openReports));
  wrap.append(adminSuggestions(data.suggestions, data.openSuggestions));

  const peopleCard = el("div", "card");
  peopleCard.style.marginTop = "var(--md)";
  const peopleRow = el("div", "row");
  const peopleText = el("div", "row-main");
  peopleText.append(el("div", "row-title", `People · ${data.userTotal}`),
                    el("div", "row-sub",
                       "Find someone, grant or take back access, message them, "
                       + "or reward a whole group."));
  peopleRow.append(peopleText);
  const open = el("button", "btn secondary", "Open");
  open.type = "button";
  open.onclick = () => {
    if (!state.adminData) return;
    state.adminData = { ...state.adminData, view: "people" };
    render();
  };
  peopleRow.append(open);
  peopleCard.append(peopleRow);
  wrap.append(peopleCard);

  wrap.append(adminLinks(data.links));
  wrap.append(adminBroadcast());
  return wrap;
}

/** Whole days until a pass runs out; negative once it has. Null when there is no pass.
 *
 *  Floored rather than rounded: "1d left" must not appear for something that expires in
 *  four hours, because the whole use of this number is deciding whether to act today. */
function daysLeft(expires: string | null): number | null {
  if (!expires) return null;
  const ms = Date.parse(expires) - Date.now();
  return Math.floor(ms / 86_400_000);
}

/** Give days to everyone in a segment.
 *
 *  "i need a grants so i can give to my users if they are using my project" — the
 *  one-at-a-time Grant cannot say that, because it starts from a search for a name.
 *
 *  ACTIVE is measured from the event log: anyone who did anything in the app in the window.
 *  There is no `last_seen` column and adding one would be a migration to store something
 *  already recorded.
 *
 *  Counts before it grants, and the server refuses a number that does not match what it
 *  just reported. That is not ceremony — the segment is computed twice, the population
 *  moves between the two calls, and there is no way to take access back from somebody who
 *  has already been told they have it.
 */
function adminGrantMany(): HTMLElement {
  const card = el("div", "card");
  card.style.marginTop = "var(--md)";
  card.append(el("div", "row-title", "Grant to a group"));
  card.append(el("p", "caption",
    "Reward people who use the app, or give everyone whose access is ending a few more days."));

  const segment = el("select", "admin-input") as HTMLSelectElement;
  for (const [value, label] of [
    ["active", "Used the app recently"],
    ["expiring", "Access ending soon"],
    ["lapsed", "Access already expired"],
    ["trial", "On a free trial (never paid)"],
  ] as const) {
    const opt = el("option", "", label) as HTMLOptionElement;
    opt.value = value;
    segment.append(opt);
  }
  card.append(segment);

  const window_ = el("input", "admin-input") as HTMLInputElement;
  window_.type = "number";
  window_.min = "1";
  window_.max = "90";
  window_.value = "7";
  card.append(window_);

  const windowHint = el("p", "caption", "");
  card.append(windowHint);

  const days = el("input", "admin-input") as HTMLInputElement;
  days.type = "number";
  days.min = "1";
  days.max = "400";
  days.value = "7";
  days.placeholder = "Days to give";
  card.append(days);

  const tell = el("label", "admin-check");
  const tellBox = el("input") as HTMLInputElement;
  tellBox.type = "checkbox";
  tellBox.checked = true;
  tell.append(tellBox, document.createTextNode(" Tell them"));
  card.append(tell);

  // The window means something different per segment, so the hint is rewritten rather than
  // left as one sentence that is wrong for three of the four.
  const describe = () => {
    const n = window_.value || "7";
    const text: Record<string, string> = {
      active: `Anyone who used the app in the last ${n} days.`,
      expiring: `Anyone whose access runs out within ${n} days.`,
      lapsed: "Everyone whose access has already run out. The number above is ignored.",
      trial: "Everyone with access and no payment behind it. The number above is ignored.",
    };
    windowHint.textContent = text[segment.value] || "";
  };
  segment.onchange = describe;
  window_.oninput = describe;
  describe();

  const go = el("button", "btn", "Count, then grant");
  go.type = "button";
  go.onclick = async () => {
    const body = {
      segment: segment.value,
      days: Number(days.value) || 0,
      within_days: Number(window_.value) || 7,
    };
    if (body.days < 1) { toast("How many days?"); return; }
    go.disabled = true;
    try {
      const { recipients } = await admin.previewGrantMany(body);
      if (!recipients) { toast("Nobody is in that group."); return; }
      const ok = await ask(
        `Give ${body.days} day(s) to ${recipients} people? Access cannot be taken back.`);
      if (!ok) return;
      const out = await admin.grantMany({
        ...body, reason: "", notify: tellBox.checked, confirm_recipients: recipients,
      });
      toast(`Granted to ${out.granted}.`);
      await refreshAdmin();
    } catch (err) { reportError(err); } finally { go.disabled = false; }
  };
  card.append(go);
  return card;
}


/** One learner, full screen, for the moment they message "I paid" or "it's broken".
 *
 *  The row gave one line — `123456 · ru · PREMIUM` — so "did Aziz's 10.99 ever arrive"
 *  was answerable from memory or by asking the customer, which is the question you least
 *  want to put to the person who just paid you.
 *
 *  Everything here was already recorded. None of it was reachable.
 */
async function openPerson(chatId: number): Promise<void> {
  const sheet = el("div", "modal");
  const card = el("div", "modal-card");
  card.append(el("p", "caption", t("loading")));
  sheet.append(card);
  sheet.onclick = (ev) => { if (ev.target === sheet) sheet.remove(); };
  document.body.append(sheet);

  let p: AdminPerson;
  try {
    p = await admin.person(chatId);
  } catch (err) { sheet.remove(); reportError(err); return; }

  card.replaceChildren();
  card.append(el("div", "row-title", p.name || String(p.chat_id)));

  const why: Record<string, string> = {
    pass: "Premium — paid or granted pass", channel: "Premium — via the channel",
    staff: "Staff", none: "No access",
  };
  const left = daysLeft(p.pass_expires_at);
  card.append(el("p", "caption",
    `${why[p.premium_via] || p.premium_via}`
    + (left !== null ? ` · ${left >= 0 ? `${left}d left` : `expired ${-left}d ago`}` : "")));

  const facts = el("div", "admin-stats");
  for (const [label, value] of [
    ["Paid", `€${(p.paid_cents / 100).toFixed(2)}`],
    ["Answers", String(p.answers)],
    ["Exams", String(p.exams)],
    ["Last seen", p.last_seen ? `${-(daysLeft(p.last_seen) ?? 0)}d ago` : "never"],
  ] as const) {
    const cell = el("div", "admin-stat");
    cell.append(el("div", "admin-stat-n", value), el("div", "admin-stat-l", label));
    facts.append(cell);
  }
  card.append(facts);

  if (p.source) card.append(el("p", "caption", `Arrived via ${p.source}`));
  if (p.reports) card.append(el("p", "caption", `${p.reports} report(s) filed`));

  if (p.payments.length) {
    card.append(el("div", "admin-sub-head", "Payments"));
    for (const pay of p.payments) {
      const row = el("div", "pay");
      row.append(el("div", "pay-who",
        `${pay.manual ? "by hand" : "Tribute"} · ${pay.tier}`));
      row.append(el("div", "pay-sum",
        `${pay.refunded_at ? "refunded " : ""}€${(pay.amount_cents / 100).toFixed(2)} · `
        + `${pay.created_at.slice(0, 10)}`));
      card.append(row);
    }
  } else {
    card.append(el("p", "caption", "No payment has ever been recorded for them."));
  }

  const close = el("button", "btn", "Close");
  close.type = "button";
  close.style.marginTop = "var(--md)";
  close.onclick = () => sheet.remove();
  card.append(close);
}


/** Money in, and what the model cost. Loaded on demand — the overview is already four
 *  queries and neither of these is needed to answer "who do I talk to".
 *
 *  Individual payments were visible nowhere: all-time totals existed only by leaving the
 *  Mini App and typing /admin to the bot, and a single payment could not be seen at all.
 */
function adminMoney(): HTMLElement {
  const card = el("div", "card");
  card.style.marginTop = "var(--md)";
  card.append(el("div", "row-title", "Money"));

  const body = el("div");
  card.append(body);
  body.append(el("p", "caption", t("loading")));

  void (async () => {
    try {
      const [money, spend] = await Promise.all([admin.payments(), admin.spend()]);
      body.replaceChildren();

      const eur = (cents: number) => `€${(cents / 100).toFixed(2)}`;
      const head = el("div", "admin-stats");
      for (const [label, value] of [
        ["This month", eur(money.this_month_cents)],
        ["All time", eur(money.all_time_cents)],
      ] as const) {
        const cell = el("div", "admin-stat");
        cell.append(el("div", "admin-stat-n", value), el("div", "admin-stat-l", label));
        head.append(cell);
      }
      body.append(head);

      for (const p of money.payments.slice(0, 8)) {
        const row = el("div", "pay");
        row.append(el("div", "pay-who", p.name || String(p.chat_id)));
        row.append(el("div", "pay-sum",
          `${p.refunded_at ? "refunded " : ""}${eur(p.amount_cents)} · `
          + `${p.created_at.slice(0, 10)}`));
        body.append(row);
      }
      if (!money.payments.length) {
        body.append(el("p", "caption", "No payments recorded yet."));
      }

      // Tokens, not euros. Prices change and are per-model, so a figure computed here from
      // a hardcoded rate would be quietly wrong the first time the model does.
      body.append(el("p", "caption",
        `Model: ${spend.this_month.calls} call(s) this month · `
        + `${(spend.this_month.tokens_in / 1000).toFixed(0)}k in, `
        + `${(spend.this_month.tokens_out / 1000).toFixed(0)}k out. `
        + `All time ${spend.all_time.calls} call(s).`));
    } catch (err) {
      body.replaceChildren(el("p", "caption", "Could not load."));
      reportError(err);
    }
  })();

  return card;
}


/** What learners have told you is wrong.
 *
 *  The report button has shipped since launch and nothing ever read the table, so the
 *  complaints piled up where nobody could see them. That is the worst state for this
 *  particular feature: it asks somebody to tell you the app is wrong and then throws away
 *  what they said. Every row here is a person who cared enough to tap.
 *
 *  Each carries the statement AND the explanation, because a report is only judgeable next
 *  to the text being reported — and "Rewrite" is beside it, because a queue you cannot act
 *  on from the same screen is a queue that gets read once and abandoned.
 */
function adminReports(reports: AdminReport[], open: number): HTMLElement {
  const card = el("div", "card");
  card.style.marginTop = "var(--md)";

  card.append(el("div", "row-title", `Reports${open ? ` · ${open} open` : ""}`));

  if (!reports.length) {
    card.append(el("p", "caption", "Nothing reported. This is the good state."));
    return card;
  }

  for (const report of reports) {
    const item = el("div", "report");
    item.append(el("div", "report-meta",
      `#${report.question_id} · ${report.lang} · ${report.created_at.slice(0, 10)}`));
    item.append(el("p", "report-statement", report.statement));
    item.append(el("p", "report-explanation",
      report.explanation || "— no explanation stored for this cluster —"));

    const actions = el("div", "report-actions");

    const done = el("button", "btn secondary", "Mark read");
    done.type = "button";
    done.onclick = async () => {
      done.disabled = true;
      try {
        await admin.resolveReport(report.id);
        await refreshAdmin();
      } catch (err) { done.disabled = false; reportError(err); }
    };

    const again = el("button", "btn primary", "Rewrite");
    again.type = "button";
    again.disabled = report.cluster_id === null;
    again.onclick = async () => {
      again.disabled = true;
      again.textContent = "Rewriting…";
      try {
        const res = await admin.regenerateReported(report.id);
        toast(res.outcome === "stored" ? "Rewritten" : `Model said: ${res.outcome}`);
        await refreshAdmin();
      } catch (err) {
        again.disabled = false;
        again.textContent = "Rewrite";
        reportError(err);
      }
    };

    actions.append(again, done);
    item.append(actions);
    card.append(item);
  }
  return card;
}


/** What learners asked for, and whether it has been read.
 *
 *  The form in Settings has been collecting these since it shipped, and both endpoints
 *  existed — but nothing rendered them, so every request went into a table nobody opened.
 *  A feedback form with no inbox is worse than no form: it asks people to spend effort and
 *  then silently discards it.
 *
 *  `chat_id` is shown, and it is the only screen in the client that shows one. It is how
 *  the owner replies to the person who wrote in, which is the entire point of asking. The
 *  console is staff-only and every endpoint behind it 404s for anybody else.
 */
function adminSuggestions(rows: AdminSuggestion[], open: number): HTMLElement {
  const card = el("div", "card");
  card.style.marginTop = "var(--md)";
  card.append(el("div", "row-title", `Requests${open ? ` · ${open} new` : ""}`));

  if (!rows.length) {
    card.append(el("p", "caption", "Nobody has asked for anything yet."));
    return card;
  }

  for (const row of rows) {
    // Handled ones stay on the list rather than vanishing: "did I already deal with this"
    // is the question somebody asks a minute after marking it, and a list that only holds
    // the unread has no answer to it.
    const item = el("div", `report${row.handled ? " done" : ""}`);
    item.append(el("div", "report-meta",
      `#${row.id} · ${row.lang} · ${row.created_at.slice(0, 10)} · ${row.chat_id}`));
    item.append(el("p", "report-statement", row.text));

    if (!row.handled) {
      const actions = el("div", "report-actions");
      const done = el("button", "btn secondary", "Mark read");
      done.type = "button";
      done.onclick = async () => {
        done.disabled = true;
        try {
          await admin.handleSuggestion(row.id);
          await refreshAdmin();
        } catch (err) { done.disabled = false; reportError(err); }
      };
      actions.append(done);
      item.append(actions);
    }
    card.append(item);
  }
  return card;
}


/** Find somebody and give them access. This is how the product is sold now. */
function adminUsers(users: AdminUser[]): HTMLElement {
  const card = el("div", "card");
  card.style.marginTop = "var(--md)";
  // The count, not the list.
  //
  // Every user was rendered on the page the owner opens most, capped at 50 — which is 50
  // rows of noise today and no better at 500. Nobody opens an admin panel to read their
  // whole user base; they open it to find ONE person, or to see who is about to lapse.
  // So this answers "how many" and waits to be asked "which".
  const total = state.adminData?.userTotal ?? 0;
  card.append(el("div", "row-title", `Users · ${total}`));

  // The same segments the group grant targets — now something to LOOK at rather than only
  // a destination for free days. "Who runs out this week so I can ask them to renew" is the
  // conversation that produces money, and it had no screen.
  const chips = el("div", "segs");
  for (const [value, label] of [
    ["expiring", "Ending soon"], ["lapsed", "Expired"],
    ["quiet", "Quiet"], ["trial", "Trial"], ["active", "Active"],
  ] as const) {
    const chip = el("button", `seg${state.adminData?.segment === value ? " on" : ""}`, label);
    chip.type = "button";
    chip.onclick = () => {
      if (!state.adminData) return;
      state.adminData = { ...state.adminData, segment: value, busy: true };
      render();
      void refreshAdmin();
    };
    chips.append(chip);
  }
  card.append(chips);

  const search = el("input", "admin-input") as HTMLInputElement;
  search.placeholder = "chat id, name or referral code";
  search.value = state.adminData?.query ?? "";
  search.onchange = () => {
    if (!state.adminData) return;
    state.adminData.query = search.value.trim();
    void refreshAdmin();
  };
  card.append(search);

  // Rows only once asked. See the note on the header: the panel opens on this card, and a
  // list of everybody is noise at six users and unusable at six hundred.
  const asked = Boolean(state.adminData?.query || state.adminData?.segment);
  if (!asked) {
    card.append(el("p", "caption", "Search for someone, or pick a filter above."));
    return card;
  }

  for (const u of users.slice(0, 25)) {
    const row = el("div", "admin-row");
    const who = el("div", "admin-who");
    const name = el("button", "admin-name-btn", u.name || String(u.chat_id));
    name.type = "button";
    name.onclick = () => void openPerson(u.chat_id);
    who.append(name);
    const bits = [String(u.chat_id), u.lang];
    if (u.source) bits.push(`via ${u.source}`);
    // The expiry, in days, on the row. The endpoint has always returned pass_expires_at and
    // the row never showed it — so "take back 10 days" was guesswork, and deciding who to
    // ask for a renewal meant opening each person one at a time.
    const left = daysLeft(u.pass_expires_at);
    if (left !== null) bits.push(left >= 0 ? `${left}d left` : `expired ${-left}d ago`);
    else if (u.premium) bits.push("PREMIUM");
    // How long since they last did ANYTHING. The question retention starts from, and the
    // row could not answer it: "123456 · ru · PREMIUM" is equally true of somebody who
    // left a month ago.
    const quiet = u.last_seen === undefined ? null : -(daysLeft(u.last_seen ?? null) ?? 0);
    if (u.last_seen) bits.push(quiet && quiet > 0 ? `quiet ${quiet}d` : "here today");
    else if (u.last_seen === null) bits.push("never opened it");
    who.append(el("div", "admin-sub", bits.join(" · ")));
    row.append(who);

    const give = el("button", "admin-btn", "Grant");
    give.type = "button";
    give.onclick = () => void grantTo(u);
    row.append(give);

    const dm = el("button", "admin-btn", "Message");
    dm.type = "button";
    dm.onclick = () => void messageOne(u);
    row.append(dm);

    // The only destructive control in the panel, so it is styled as one and it asks.
    // `ask` prefers Telegram's own sheet: some Android clients suppress window.confirm
    // inside the webview, and a suppressed confirm on a DELETE would mean either nothing
    // happens or — worse, if the check were skipped — it happens without being asked.
    // Take back before Delete, and deliberately adjacent to it: ending access is the
    // proportionate correction for a slipped digit, and deleting the learner to fix a date
    // is what people did while this did not exist.
    const back = el("button", "admin-btn danger", "Take back");
    back.type = "button";
    back.disabled = daysLeft(u.pass_expires_at) === null
                    || (daysLeft(u.pass_expires_at) ?? -1) < 0;
    back.onclick = async () => {
      const answer = window.prompt(
        "End access now, or take back how many days? (\"end\", or a number)", "end");
      if (answer === null) return;
      const trimmed = answer.trim().toLowerCase();
      const body = trimmed === "end"
        ? { mode: "end" as const }
        : { mode: "shorten" as const, days: Number(trimmed) };
      if (body.mode === "shorten" && !(body.days > 0)) { toast("Not a number."); return; }
      if (!(await ask(body.mode === "end"
        ? `End access for ${u.name || u.chat_id} now?`
        : `Take ${body.days} day(s) back from ${u.name || u.chat_id}?`))) return;
      back.disabled = true;
      try {
        await admin.revoke(u.chat_id, body);
        toast("Access updated.");
        await refreshAdmin();
      } catch (err) { back.disabled = false; reportError(err); }
    };
    row.append(back);

    const remove = el("button", "admin-btn danger", "Delete");
    remove.type = "button";
    remove.onclick = async () => {
      const who = u.name || String(u.chat_id);
      if (!(await ask(`Delete ${who}? Their progress is gone for good. Payments are kept.`))) {
        return;
      }
      remove.disabled = true;
      try {
        const res = await admin.deleteUser(u.chat_id);
        toast(res.purchases_kept
          ? `Deleted. ${res.purchases_kept} payment record(s) kept.`
          : "Deleted.");
        await refreshAdmin();
      } catch (err) { remove.disabled = false; reportError(err); }
    };
    row.append(remove);

    card.append(row);
  }
  if (!users.length) card.append(el("p", "caption", "Nobody matches."));
  else if (total > users.length) {
    card.append(el("p", "caption",
      `Showing ${users.length} of ${total}. Narrow the search to see the rest.`));
  }
  return card;
}

/** The tiers, priced. One tap sets the length and the money together.
 *
 *  Every euro this business records used to enter through three chained window.prompt
 *  boxes — days, then an amount that defaulted to 10.99 whichever length had just been
 *  chosen, then a reason. On a phone that is three modal dialogs, cancelling the middle one
 *  aborted the sale with no trace, and the default price was wrong two times in three.
 *
 *  Prices mirror TIER_PRICE_CENTS in shared/constants.py — a hand copy, because this is the
 *  owner's console and it must work without a round trip. A test compares the two, since a
 *  copy that drifts records the wrong revenue against a real sale and nothing would notice
 *  until somebody added up the year. */
const GRANT_PRESETS = [
  { label: "1 month · €3.99", days: 30, cents: 399 },
  { label: "3 months · €9.99", days: 90, cents: 999 },
  { label: "6 months · €16.99", days: 180, cents: 1699 },
  { label: "Gift · no payment", days: 30, cents: 0 },
] as const;

async function grantTo(u: AdminUser): Promise<void> {
  const who = u.name || String(u.chat_id);
  const sheet = el("div", "modal");
  const card = el("div", "modal-card");
  card.append(el("div", "row-title", `Grant to ${who}`));
  const left = daysLeft(u.pass_expires_at);
  card.append(el("p", "caption", left !== null && left >= 0
    ? `They have ${left} day(s) left. A grant is ADDED to that.`
    : "They have no access right now."));

  const close = () => sheet.remove();

  for (const preset of GRANT_PRESETS) {
    const b = el("button", "btn secondary", preset.label);
    b.type = "button";
    b.style.marginTop = "var(--sm)";
    b.onclick = async () => {
      const money = preset.cents
        ? `€${(preset.cents / 100).toFixed(2)}`
        : "nothing — a gift";
      if (!(await ask(`${preset.days} days to ${who}, for ${money}?`))) return;
      close();
      try {
        const out = await admin.grant(
          u.chat_id, preset.days,
          // The reason is what makes the event log readable a month later, and it is
          // derivable — nobody should be asked to type it on a phone mid-sale.
          preset.cents ? `sold ${preset.label}` : "gift",
          false, preset.cents);
        toast(`Granted. Now until ${new Date(out.pass_expires_at).toLocaleDateString()}.`);
        await refreshAdmin();
      } catch (err) { reportError(err); }
    };
    card.append(b);
  }

  const cancel = el("button", "btn", "Cancel");
  cancel.type = "button";
  cancel.style.marginTop = "var(--md)";
  cancel.onclick = close;
  card.append(cancel);

  sheet.append(card);
  sheet.onclick = (ev) => { if (ev.target === sheet) close(); };
  document.body.append(sheet);
}

async function messageOne(u: AdminUser): Promise<void> {
  const text = window.prompt(`Message to ${u.name || u.chat_id}:`, "");
  if (!text) return;
  try {
    const out = await admin.message(u.chat_id, text);
    toast(out.delivered ? "Sent." : "Not delivered — they may have blocked the bot.");
  } catch (err) { reportError(err); }
}

/** Referral links: the only thing that grants a trial. */
function adminLinks(links: AdminLink[]): HTMLElement {
  const card = el("div", "card");
  card.style.marginTop = "var(--md)";
  card.append(el("div", "row-title", "Trial links"));
  card.append(el("div", "row-sub",
    "Only these grant a trial. A bare /start grants nothing."));

  for (const link of links) {
    const row = el("div", "admin-row");
    const who = el("div", "admin-who");
    who.append(el("div", "admin-name", `${link.code} — ${link.trial_days}d`));
    const bits = [`${link.uses} used`];
    if (link.max_uses) bits.push(`cap ${link.max_uses}`);
    if (!link.active) bits.push("OFF");
    if (link.label) bits.push(link.label);
    who.append(el("div", "admin-sub", bits.join(" · ")));
    row.append(who);

    const copy = el("button", "admin-btn", "Copy");
    copy.type = "button";
    copy.onclick = () => {
      void navigator.clipboard?.writeText(link.url);
      toast("Link copied.");
    };
    row.append(copy);

    const toggle = el("button", "admin-btn", link.active ? "Turn off" : "Turn on");
    toggle.type = "button";
    toggle.onclick = async () => {
      try {
        await admin.updateLink(link.code, { active: !link.active });
        await refreshAdmin();
      } catch (err) { reportError(err); }
    };
    row.append(toggle);

    // Deleting is offered only for a link NOBODY came through — the server refuses the
    // rest with a 409, because the code is the only record of where those users came from.
    // The button is shown regardless rather than hidden on `uses`, so the refusal explains
    // the rule at the moment it applies instead of a control silently not being there.
    const drop = el("button", "admin-btn danger", "Delete");
    drop.type = "button";
    drop.onclick = async () => {
      // Explain the refusal BEFORE the round trip, and offer the thing that does work.
      //
      // The server refuses to delete a code somebody arrived through — that code is the
      // only record of where they came from — and it says so in the 409. But the message
      // landed in a pill-shaped toast, so the button read as simply broken: reported as
      // "deleting a referral link is not working". An action offered and then always
      // refused is worse than one that is not offered.
      if (link.uses > 0) {
        const off = await ask(
          `${link.uses} user(s) came through ${link.code}, so it cannot be deleted — `
          + `the code is the only record of where they came from. Turn it off instead?`);
        if (!off) return;
        try {
          await admin.updateLink(link.code, { active: false });
          toast("Link turned off. It grants nothing now.");
          await refreshAdmin();
        } catch (err) { reportError(err); }
        return;
      }
      if (!(await ask(`Delete link ${link.code}? Nobody has used it.`))) return;
      drop.disabled = true;
      try {
        await admin.deleteLink(link.code);
        toast("Link deleted.");
        await refreshAdmin();
      } catch (err) { drop.disabled = false; reportError(err); }
    };
    row.append(drop);
    card.append(row);
  }

  const add = el("button", "btn secondary", "New link");
  add.type = "button";
  add.style.marginTop = "var(--sm)";
  add.onclick = () => void createLink();
  card.append(add);
  return card;
}

async function createLink(): Promise<void> {
  const code = window.prompt("Code (letters, digits, - and _):", "");
  if (!code) return;
  const days = Number(window.prompt("Trial days for this link:", "7") || "0");
  if (!Number.isFinite(days) || days < 1) { toast("Not a number of days."); return; }
  const label = window.prompt("What is it for? (your note)", "") || "";
  const capRaw = window.prompt("Maximum uses, or blank for unlimited:", "") || "";
  const max_uses = capRaw.trim() ? Number(capRaw) : null;
  try {
    await admin.createLink({ code, label, trial_days: days, max_uses });
    await refreshAdmin();
    toast("Link created.");
  } catch (err) { reportError(err); }
}

/** A newsletter. Counted before it is sent, because it cannot be unsent. */
function adminBroadcast(): HTMLElement {
  const card = el("div", "card");
  card.style.marginTop = "var(--md)";
  card.append(el("div", "row-title", "Newsletter"));

  const box = el("textarea", "admin-input admin-text") as HTMLTextAreaElement;
  box.placeholder = "Message to send…";
  box.rows = 4;
  card.append(box);

  const langRow = el("div", "admin-row");
  const langSel = el("select", "admin-input") as HTMLSelectElement;
  for (const [value, label] of [["", "All languages"], ["ru", "Russian"],
                                ["en", "English"], ["it", "Italian"],
                                ["uz", "Uzbek"]] as const) {
    const opt = el("option", "", label) as HTMLOptionElement;
    opt.value = value;
    langSel.append(opt);
  }
  langRow.append(langSel);
  card.append(langRow);

  // WHO. Same segments again — a third definition of "expiring" is a third thing that can
  // drift from the other two.
  const who = el("select", "admin-input") as HTMLSelectElement;
  for (const [value, label] of [
    ["", "Everyone"], ["expiring", "Access ending soon"], ["lapsed", "Access expired"],
    ["quiet", "Gone quiet"], ["trial", "On a free trial"], ["active", "Using it now"],
  ] as const) {
    const opt = el("option", "", label) as HTMLOptionElement;
    opt.value = value;
    who.append(opt);
  }
  card.append(who);

  const premium = el("label", "admin-check");
  const premiumBox = el("input") as HTMLInputElement;
  premiumBox.type = "checkbox";
  premium.append(premiumBox, document.createTextNode(" Subscribers only"));
  card.append(premium);

  // An image. A URL rather than an upload: Telegram fetches it itself, so there is no file
  // to store, serve or back up — and note the caption limit, which is why the hint says so
  // rather than letting a long newsletter quietly lose its ending.
  const photo = el("input", "admin-input") as HTMLInputElement;
  photo.type = "url";
  photo.placeholder = "Image URL (optional)";
  card.append(photo);
  card.append(el("p", "caption", "With an image the text is capped at 1024 characters; "
                                 + "over that the image is dropped and the text goes whole."));

  // Buttons. The Mini App one is the point of the whole feature: it puts an offer one tap
  // from the paywall instead of sending someone to a browser, where they are simply gone.
  card.append(el("div", "admin-sub-head", "Buttons (optional, max 3)"));

  const openApp = el("label", "admin-check");
  const openAppBox = el("input") as HTMLInputElement;
  openAppBox.type = "checkbox";
  openApp.append(openAppBox, document.createTextNode(" Open the app"));
  card.append(openApp);

  const openAppLabel = el("input", "admin-input") as HTMLInputElement;
  openAppLabel.placeholder = "Its label — e.g. Открыть приложение";
  card.append(openAppLabel);

  const writeMe = el("label", "admin-check");
  const writeMeBox = el("input") as HTMLInputElement;
  writeMeBox.type = "checkbox";
  writeMe.append(writeMeBox, document.createTextNode(" Message me (to pay)"));
  card.append(writeMe);

  const writeMeLabel = el("input", "admin-input") as HTMLInputElement;
  writeMeLabel.placeholder = "Its label — e.g. Написать мне";
  card.append(writeMeLabel);

  function chosenButtons(): AdminButton[] {
    const out: AdminButton[] = [];
    if (openAppBox.checked) {
      out.push({ text: openAppLabel.value.trim() || "Open", webapp: true });
    }
    if (writeMeBox.checked) {
      const handle = state.me?.support_contact || state.me?.bot_username || "";
      if (handle) out.push({ text: writeMeLabel.value.trim() || "Message me", chat: handle });
    }
    return out;
  }

  const send = el("button", "btn", "Count, then send");
  send.type = "button";
  send.onclick = async () => {
    const text = box.value.trim();
    if (!text) { toast("Nothing to send."); return; }
    const lang = langSel.value || null;
    try {
      // Count FIRST and make the owner confirm the number. The server refuses a send whose
      // confirmed count does not match what it just reported, so this is not decoration —
      // it is the only chance to notice the filter is wrong.
      // Same filter as the send below. If these disagree the confirmed number describes a
      // different population, and the server rejects it — correctly, but confusingly.
      const { recipients } = await admin.previewBroadcast(
        { text, lang, premium_only: premiumBox.checked, segment: who.value });
      if (!recipients) { toast("Nobody matches that filter."); return; }
      const buttons = chosenButtons();
      const extras = [
        photo.value.trim() ? "an image" : "",
        buttons.length ? `${buttons.length} button(s)` : "",
      ].filter(Boolean).join(" and ");
      const ok = await ask(
        `Send to ${recipients} people${extras ? `, with ${extras}` : ""}? `
        + "This cannot be undone.");
      if (!ok) return;
      const out = await admin.broadcast({
        text, lang, premium_only: premiumBox.checked, label: "",
        segment: who.value,
        confirm_recipients: recipients,
        photo_url: photo.value.trim() || null,
        buttons,
      });
      toast(`Sending to ${out.queued}.`);
      box.value = "";
      photo.value = "";
    } catch (err) { reportError(err); }
  };
  card.append(send);
  return card;
}

// ---------------------------------------------------------------------------
// the weekly league
// ---------------------------------------------------------------------------

async function loadRatings(): Promise<void> {
  try {
    state.ratings = await leaderboard.board();
    render();
  } catch (err) {
    reportError(err);
  }
}

/** This week's table: a podium for the top three, a list for the rest.
 *
 *  Monday to Sunday UTC, ranked on CORRECT answers — see api/services/leaderboard.py for
 *  why weekly rather than all-time, and why correct rather than attempted. */
function ratingsScreen(): HTMLElement {
  const wrap = el("section", "screen");
  const head = el("div", "v-head with-rules");
  const heading = el("div");
  heading.append(el("h2", "v-title", t("ratings")), el("p", "v-sub", t("ratings_week")));
  head.append(heading);
  // In the header, not in Settings. The people who need the rules are looking at the board
  // right now and asking why their points did not move; a link they have to go and find is
  // a support message instead.
  const rules = el("button", "v-rules");
  rules.type = "button";
  rules.textContent = "?";
  rules.setAttribute("aria-label", t("league_rules_title"));
  rules.onclick = () => openLeagueRules();
  head.append(rules);
  wrap.append(head);

  const board = state.ratings;
  if (!board) {
    wrap.append(el("p", "caption", t("loading")));
    return wrap;
  }

  // Someone who asked not to appear is told so, rather than shown an empty board and left
  // to wonder whether the feature is broken.
  if (board.me.opted_out) {
    const card = el("div", "card");
    card.append(el("p", "", t("ratings_hidden")));
    const show = el("button", "btn secondary", t("ratings_show_me"));
    show.type = "button";
    show.onclick = () => void setLeaderboardOptOut(false);
    card.append(show);
    wrap.append(card);
    return wrap;
  }

  if (!board.entries.length) {
    wrap.append(el("p", "caption", t("ratings_empty")));
    return wrap;
  }

  // Below a handful of players a "league" is three people in a fixed order where somebody
  // is permanently last. Saying it is quiet is honest, and stops the screen reading as a
  // competition the learner has already lost.
  if (board.ranked < RATINGS_MIN_PLAYERS) {
    wrap.append(el("p", "caption", t("ratings_quiet")));
  }

  const podium = el("div", "podium");
  for (const entry of board.entries.slice(0, 3)) {
    const seat = el("div", `podium-seat p${entry.rank}${entry.is_me ? " me" : ""}`);
    seat.append(el("div", "podium-rank", String(entry.rank)));
    seat.append(el("div", "podium-name", entry.name || t("ratings_anon")));
    seat.append(el("div", "podium-score", String(entry.score)));
    if (entry.medal) seat.append(medalMark(entry.medal));
    podium.append(seat);
  }
  wrap.append(podium);

  const list = el("div", "rank-list");
  for (const entry of board.entries.slice(3)) {
    const row = el("div", `rank-row${entry.is_me ? " me" : ""}`);
    row.append(el("span", "rank-n", String(entry.rank)));
    row.append(el("span", "rank-name", entry.name || t("ratings_anon")));
    if (entry.medal) row.append(medalMark(entry.medal));
    row.append(el("span", "rank-score", String(entry.score)));
    list.append(row);
  }
  if (list.childElementCount) wrap.append(list);

  // Their own line, always — "you are 22nd with 12" is information. A board somebody cannot
  // find themselves on is just a list of other people.
  if (board.me.rank && !board.entries.some((e) => e.is_me)) {
    const mine = el("div", "rank-row me standalone");
    mine.append(el("span", "rank-n", String(board.me.rank)));
    mine.append(el("span", "rank-name", t("ratings_you")));
    mine.append(el("span", "rank-score", String(board.me.score)));
    wrap.append(mine);
  } else if (!board.me.rank) {
    wrap.append(el("p", "caption", t("ratings_not_ranked")));
  }

  return wrap;
}

/** Last season's podium, as its own element and never as part of a name.
 *
 *  Telegram first names are whatever the person typed. Someone renaming themselves
 *  "\u{1F947} Aziz" — free, one tap, no learning — would appear to be wearing a medal on
 *  everyone else's board if the mark were concatenated into the name string. Its own column
 *  means a fake one sits visibly in the wrong place.
 *
 *  Drawn as a numbered marker rather than in gold, silver and bronze. tokens.css reserves
 *  gold exclusively for Premium — "the moment gold means two things it stops working" — and
 *  a league medal is not a purchase.
 */
function medalMark(place: number): HTMLElement {
  const mark = el("span", `medal m${place}`);
  mark.textContent = String(place);
  mark.title = t("league_medal_hint", { n: place });
  return mark;
}

/** How points work, and — the part that decides the support load — why they sometimes do not.
 *
 *  Three separate rules can silently make a correct answer worth nothing: the question was
 *  already answered this week, the day is capped, the pace was not credited. A learner who
 *  is not told cannot tell any of those apart from a bug.
 */
function openLeagueRules(): void {
  const body = el("div");
  const cards: Array<[Key, Key]> = [
    ["league_rule_season", "league_rule_season_body"],
    ["league_rule_points", "league_rule_points_body"],
    ["league_rule_prizes", "league_rule_prizes_body"],
    ["league_rule_seen", "league_rule_seen_body"],
    ["league_rule_why", "league_rule_why_body"],
  ];
  for (const [title, text] of cards) {
    const card = el("div", "card league-rule");
    card.append(el("div", "row-title", t(title)));
    card.append(el("p", "caption", t(text)));
    body.append(card);
  }
  const scrim = el("div", "modal");
  const card = el("div", "modal-card");
  const close = () => { scrim.remove(); setBackButton(backTarget()); };
  card.append(el("h3", "sheet-title", t("league_rules_title")));
  card.append(body);
  const done = el("button", "btn secondary", t("close"));
  done.type = "button";
  done.onclick = close;
  card.append(done);
  scrim.append(card);
  scrim.onclick = (ev) => { if (ev.target === scrim) close(); };
  setBackButton(close);
  document.body.append(scrim);
}

async function setLeaderboardOptOut(optOut: boolean): Promise<void> {
  try {
    state.me = await api.settings({ leaderboard_opt_out: optOut });
    state.ratings = null;
    render();
    if (state.screen === "ratings") void loadRatings();
  } catch (err) {
    reportError(err);
  }
}

function tabs(): HTMLElement {
  const bar = el("nav", "tabs");
  const add = (id: Screen, label: string, icon: SVGSVGElement) => {
    const b = el("button", `tab ${state.screen === id ? "on" : ""}`);
    b.type = "button";
    b.append(icon, el("span", "", label));
    b.onclick = () => {
      state.screen = id;
      render();
      // The league is fetched on entry rather than at boot: it is the one screen whose
      // data is about OTHER people, so it is stale the moment it is cached and there is no
      // reason to pay for it on a visit that never opens this tab.
      if (id === "ratings") void loadRatings();
    };
    bar.append(b);
  };
  add("home", t("home"), icons.home(23));
  add("profile", t("profile"), icons.person(23));
  add("ratings", t("ratings"), icons.crown(23));
  add("stats", t("stats"), icons.chart(23));
  add("settings", t("settings"), icons.gear(23));
  return bar;
}

/** Which screens have somewhere to go back TO.
 *
 *  Home is the root, and the other tabs are reachable from the tab bar — a back arrow
 *  there would be ambiguous. A sitting and its results are the two places the tab bar is
 *  hidden or the flow is linear, so those are the two that need it. */
function backTarget(): (() => void) | null {
  if (state.screen === "run") return leaveRun;
  if (state.screen === "results") return goHome;
  // Vocabulary is entered from the home screen rather than the tab bar, so it is the one
  // tab-bar-visible screen with somewhere unambiguous to go back to.
  if (state.screen === "vocab") return goHome;
  // Reached by tapping the error-rate tile, so Back belongs on the screen that tile is on
  // — not home, which would make the number harder to get back to than it was to leave.
  if (state.screen === "analysis") return () => { state.screen = "stats"; render(); };
  // Subjects sits UNDER practice now, so back goes there rather than skipping a level.
  if (state.screen === "subjects") return () => { state.screen = "practice"; render(); };
  if (state.screen === "practice") return () => { state.screen = "home"; render(); };
  if (state.screen === "admin") {
    // People is a page inside the panel, so Back must land on the panel rather than
    // leaving it — otherwise the only way back to the overview is to reopen Admin.
    if (state.adminData?.view === "people") {
      return () => {
        if (state.adminData) {
          state.adminData = { ...state.adminData, view: "home", query: "", segment: "" };
        }
        render();
      };
    }
    return () => { state.screen = "settings"; render(); };
  }
  return null;
}

/** The start screen: what is being prepared, while it is prepared.
 *
 *  Names the mode so the wait is attached to something the learner just chose, and says
 *  what is happening rather than spinning silently — "preparing your questions" is a
 *  reason to wait, an unlabelled spinner is not. */
/** The car and the road it is driving down.
 *
 *  Drawn rather than shipped as an image so it costs no request on the one screen whose
 *  entire problem is that the user is already waiting, and so it can take its colours from
 *  the palette and work in both themes. Everything that moves is a class, because the
 *  motion is turned off wholesale under prefers-reduced-motion and that is easier to be
 *  sure of when it lives in one stylesheet rather than in inline attributes.
 *
 *  The road moves, not the car. A car that drives off the right edge has to be reset, and
 *  the reset is visible; a scrolling road loops forever with nothing to notice. */
function roadScene(): SVGSVGElement {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 240 112");
  svg.setAttribute("class", "road-scene");
  svg.setAttribute("aria-hidden", "true");   // decoration; the text below says what is happening
  const wheel = (cx: number) => `<g class="car-wheel">
      <circle cx="${cx}" cy="85" r="12" class="car-tyre"/>
      <circle cx="${cx}" cy="85" r="4.5" class="car-hub"/>
      <g class="car-spokes">
        <path d="M${cx} 76.5 v3"/><path d="M${cx} 90.5 v3"/>
        <path d="M${cx - 8.5} 85 h3"/><path d="M${cx + 5.5} 85 h3"/>
      </g>
    </g>`;
  // Static markup with no interpolated user data — the textContent-only rule is about
  // content from the API, and there is none here.
  svg.innerHTML = `
    <line x1="0" y1="97" x2="240" y2="97" class="road-base"/>
    <line x1="6" y1="97" x2="234" y2="97" class="road-dash"/>
    <g class="car">
      <path class="car-body" d="M44 82 h19 a14 14 0 0 0 28 0 h48 a14 14 0 0 0 28 0 h15 q9 0 9-9
        v-6 q0-7-7-9 l-21-4 -15-13 q-6-5-14-5 h-28 q-7 0-12 4 l-14 12 -22 5 q-9 2-9 11
        v6 q0 8 8 8 z"/>
      <path class="car-glass" d="M80 57 l12-10 q3-2 6-2 h11 v14 z"/>
      <path class="car-glass" d="M114 45 h13 q5 0 8 3 l12 10 h-33 z"/>
      <rect class="car-lamp" x="186" y="66" width="10" height="7" rx="3"/>
      ${wheel(77)}${wheel(153)}
    </g>`;
  return svg;
}

/** The rotating tip.
 *
 *  Updated in place on an interval, for the same reason the exam clock is: render()
 *  replaces the whole tree, and routing a 4.5-second text change through it would restart
 *  the road animation and re-run every other effect on the screen.
 *
 *  The order is shuffled per visit. A fixed order means the same two facts every time for
 *  anyone who starts more quizzes than they finish, and the later tips are never read. */
let tipNode: HTMLElement | null = null;
let tipTimer: number | undefined;
let tipOrder: readonly string[] = [];
let tipAt = 0;

const TIP_EVERY = 4_500;
const TIP_FADE = 260;      // must stay in step with the transition in .prep-tip

function startTips(node: HTMLElement): void {
  stopTips();
  const pool = tips();
  if (pool.length === 0) return;
  tipOrder = [...pool].sort(() => Math.random() - 0.5);
  tipAt = 0;
  tipNode = node;
  node.textContent = tipOrder[0]!;
  if (tipOrder.length < 2) return;   // nothing to rotate to
  tipTimer = window.setInterval(() => {
    const target = tipNode;
    if (!target) { stopTips(); return; }
    target.classList.add("out");
    window.setTimeout(() => {
      if (tipNode !== target) return;   // the screen went away mid-fade
      tipAt = (tipAt + 1) % tipOrder.length;
      target.textContent = tipOrder[tipAt]!;
      target.classList.remove("out");
    }, TIP_FADE);
  }, TIP_EVERY);
}

function stopTips(): void {
  window.clearInterval(tipTimer);
  tipTimer = undefined;
  tipNode = null;
}

function preparingScreen(): HTMLElement {
  const wrap = el("section", "screen prep");
  const box = el("div", "prep-box");
  box.append(roadScene());
  box.append(el("h2", "prep-title",
    state.preparing?.mode === "exam" ? t("exam") : t("practice")));
  box.append(el("p", "prep-sub", t("preparing_quiz")));

  // The wait is dead time; a fact the learner will be tested on is the only thing worth
  // putting in it. Reserved height so the box does not resize under a one-line tip.
  // Only when there is something to show. The slot reserves height so the box does not
  // resize under a one-line tip, and reserving it for nothing would leave a hole under the
  // car on any locale whose list is empty.
  if (tips().length) {
    const slot = el("div", "prep-tip-slot");
    const tip = el("p", "prep-tip");
    slot.append(tip);
    box.append(slot);
    startTips(tip);
  }

  // No progress bar. There was one, driven by a per-question call; the window is now
  // prepared in a single request, and between sending it and receiving it the client knows
  // exactly nothing. A bar advanced on a timer would be a guess wearing the clothes of a
  // measurement — worse than no bar, because it is believed. The wait can be genuinely
  // long on cold questions, so the honest version of this screen is the spinner plus a
  // sentence saying what is happening.

  wrap.append(box);
  return wrap;
}

function render(): void {
  root.replaceChildren();
  const back = backTarget();
  // Returns false on clients too old to have a header back button; the screen then
  // renders its own, so a user is never trapped on a screen with no tab bar.
  const nativeBack = setBackButton(back);

  let screen: HTMLElement;
  switch (state.screen) {
    case "run": screen = runScreen(); break;
    case "results": screen = resultsScreen(); break;
    case "profile": screen = profileScreen(); break;
    case "stats": screen = statsScreen(); break;
    case "analysis": screen = analysisScreen(); break;
    case "practice": screen = practiceScreen(); break;
    case "subjects": screen = subjectsScreen(); break;
    case "settings": screen = settingsScreen(); break;
    case "vocab": screen = vocabScreen(); break;
    case "ratings": screen = ratingsScreen(); break;
    case "admin": screen = adminScreen(); break;
    default: screen = homeScreen();
  }
  // Preparing outranks every screen: the learner has tapped Start and nothing else is
  // happening until the quiz opens.
  // Stopped BEFORE the branch below can start it again: leaving it running would leave an
  // interval poking a node that replaceChildren() has already thrown away.
  if (!state.preparing) stopTips();
  if (state.preparing) screen = preparingScreen();
  if (back && !nativeBack) screen.prepend(fallbackBack(back));
  root.append(screen);

  // Ask for the translation of whatever is now on screen. After render, so the Italian
  // is already visible and the wait costs the user nothing.
  if (state.screen === "run") void hydrateTranslation();

  // The tab bar is suppressed while a sitting is in flight. Previously it was appended
  // unconditionally, so a candidate could tap Stats mid-exam and silently abandon a
  // timed run — with no warning and no way back.
  if (state.screen !== "run") {
    root.append(tabs());
  }

}

/** Offer back a sitting the learner left open.
 *
 *  The server enforces the deadline when the sitting is read, so an exam whose twenty
 *  minutes elapsed while the phone was locked comes back GRADED rather than resumable —
 *  which is correct, and better than pretending there is still time on it.
 */
async function recoverOpenSitting(): Promise<void> {
  const id = state.me?.open_session_id;
  if (!id || state.run) return;
  try {
    const session = await sessions.read(id);
    if (session.state === "open") {
      state.resumable = session;
    } else {
      // It ran out while they were away. Show them the result rather than dropping it.
      state.results = await sessions.results(id);
      state.screen = "results";
    }
    render();
  } catch {
    // A sitting that cannot be read is not worth an error banner on the home screen.
  }
}

async function boot(): Promise<void> {
  initTelegram();
  // Before the first render, so nothing paints light and then flips.
  applyTheme(preferredTheme());
  if (!inTelegram) {
    // Wrapped in .screen and routed through i18n, like every other message in the app.
    // This was a bare <p> against the page edge, in English only — and English is the one
    // language this app's audience is least likely to read. The failure branch below has
    // always used .screen; only this one was left behind.
    const wrap = el("section", "screen");
    // `outside_telegram` already carries this exact sentence in all four languages and is
    // used by the catch branch below for the 401 case. Reused rather than adding a second
    // key saying the same thing in four places.
    wrap.append(el("p", "error", t("outside_telegram")));
    root.replaceChildren(wrap);
    return;
  }
  try {
    state.me = await api.me();
    setLang(state.me.lang);
    document.documentElement.lang = lang();
    render();
    // Ask the server what we walked away from. The client cannot remember across a
    // reload, and a Mini App is closed by anything — a phone call, the screen locking,
    // switching apps. Done AFTER the first render so the home screen appears instantly
    // and the resume card arrives a moment later, rather than the app hanging on it.
    void recoverOpenSitting();
  } catch (err) {
    root.replaceChildren();
    const wrap = el("section", "screen");
    wrap.append(el("p", "error",
      err instanceof ApiError && err.status === 401 ? t("outside_telegram") : t("error")));
    const retry = el("button", "btn primary", t("retry"));
    retry.onclick = () => void boot();
    wrap.append(retry);
    root.append(wrap);
  }
}

void boot();
