import { admin, api, ApiError, leaderboard, sessions, vocab } from "./api";
import { readinessGauge } from "./gauge";
import { art, icons } from "./icons";
import { TRANSLATION_LANGUAGES, lang, setLang, t } from "./i18n";
import { ask, haptic, inTelegram, initTelegram, openChat, setBackButton, tg } from "./telegram";
import type {
  AnswerResult,
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
  AdminReport,
  AdminUser,
  Leaderboard,
  RepeatSource,
  VocabRound,
  VocabStats,
} from "./types";
import "./style.css";

type Screen = "home" | "run" | "results" | "profile" | "stats" | "settings" | "vocab"
  | "ratings" | "admin";

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
}

/** The vocabulary trainer's own state.
 *
 *  `current` is the graded verdict for the item on screen: its presence is what switches
 *  the card from "type your answer" to "here is how you did", so there is one source of
 *  truth for which half is showing rather than a separate boolean that can disagree. */
interface VocabRun {
  view: "test" | "list";
  round: VocabRound | null;
  index: number;
  current: VocabAnswer | null;
  right: number;
  typed: string;
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
  /** The owner's console. Loaded on entry and never at boot — it is one person's screen
   *  and everybody else would be paying for a request that 404s. */
  adminData: { overview: AdminOverview | null; users: AdminUser[]; links: AdminLink[];
               reports: AdminReport[]; openReports: number;
               query: string; segment: string; busy: boolean } | null;
  /** A quiz being prepared. Non-null only between tapping Start and the first question. */
  preparing: { mode: Mode; source: RepeatSource } | null;
} = { me: null, screen: "home", run: null, results: null, stats: null, profile: null,
      resumable: null, reviewWrongOnly: true, ratings: null, adminData: null,
      preparing: null,
      vocab: { view: "test", round: null, index: 0, current: null, right: 0, typed: "",
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

async function startRun(mode: Mode, source: RepeatSource = "smart"): Promise<void> {
  // A loading screen, and it is not decoration.
  //
  // The first question of a cold paper needs a translation the learner cannot read without,
  // and fetching it after the question is on screen means they stare at Italian they do not
  // understand. A start screen is the one moment in a quiz where waiting is EXPECTED, so it
  // is the cheapest place to put the wait — and it is where the first five get prepared.
  state.preparing = { mode, source };
  render();
  try {
    const session = await sessions.start(mode, source);
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
      if (run.index < run.session.question_count - 1) run.index += 1;
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
      if (run.session.mode !== "practice" && run.index < run.session.question_count - 1) {
        run.index += 1;
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

  wrap.append(repeatRow());
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
function repeatRow(): HTMLElement {
  const row = el("div", "repeat-row");
  for (const [source, label] of [
    ["wrong", t("repeat_wrong")],
    ["correct", t("repeat_correct")],
  ] as const) {
    const chip = el("button", `repeat-chip ${source}`, label);
    chip.type = "button";
    chip.onclick = () => void startRun("practice", source);
    row.append(chip);
  }
  return row;
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
  card.append(el("span", "v-entry-icon", "\u{1F4DA}"));
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

  const artwork = el("div", "mode-art");
  artwork.append(mode === "exam" ? art.exam() : art.practice());
  card.append(artwork);

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

  card.onclick = () => void startRun(mode);
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
  card.style.cssText = "background:#eff6ff;border-color:#bfdbfe;margin-top:var(--lg)";
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
function translationToggle(): HTMLElement | null {
  const me = state.me;
  if (!me || !me.premium) return null;
  if (!TRANSLATION_LANGUAGES.includes(me.lang)) return null;

  const row = el("label", "q-tr");
  row.append(el("span", "q-tr-label", t("tr_toggle")));
  const sw = el("button", `switch small ${me.translations_on ? "on" : ""}`);
  sw.type = "button";
  sw.setAttribute("role", "switch");
  sw.setAttribute("aria-checked", String(me.translations_on));
  sw.onclick = async () => {
    try {
      state.me = await api.settings({ translations_on: !me.translations_on });
      render();
      // Turning it ON mid-question must fetch the translation for the question already
      // on screen; the usual fetch only runs when a question is first rendered.
      if (state.me?.translations_on) void hydrateTranslation();
    } catch (err) {
      reportError(err);
    }
  };
  row.append(sw);
  return row;
}

function runScreen(): HTMLElement {
  const run = state.run!;
  const wrap = el("section", "screen");
  const question = currentQuestion(run);

  if (run.deadline) {
    const bar = el("div", "timer-card");
    bar.append(timerDial());
    const mid = el("div", "timer-mid");
    timerNode = el("div", "timer-value", "--:--");
    mid.append(timerNode, el("div", "timer-label", t("time_left")));
    bar.append(mid);

    const submit = el("button", "timer-submit");
    submit.append(icons.flag(18), document.createTextNode(t("submit_short")));
    submit.onclick = confirmFinish;
    bar.append(submit);
    wrap.append(bar);
    tick();
  }

  // The answer sheet is an EXAM object: thirty numbered cells standing for the paper in
  // front of you, showing which you have done. Practice has no paper — it is a stream
  // that ends when you end it — so the row would grow without bound and imply a finish
  // line that does not exist.
  if (run.session.mode === "exam") wrap.append(answerSheet(run));

  const meta = el("div", "q-meta");
  // "Question 5 of 30" is a promise in practice, and a false one: the total is only the
  // current batch and silently becomes 60, then 90. Practice counts up, with no total.
  meta.append(el("div", "q-index", run.session.mode === "exam"
    ? t("question_of", { n: run.index + 1, total: run.session.question_count })
    : t("question_n", { n: run.index + 1 })));
  const tr = translationToggle();
  if (tr) meta.append(tr);
  wrap.append(meta);

  if (!question) { wrap.append(el("div", "spinner")); return wrap; }

  if (question.image) {
    const plate = el("div", "plate");
    const img = el("img");
    img.src = api.figureUrl(question.image);
    img.alt = "";
    plate.append(img);
    wrap.append(plate);
  }

  if (question.stem_it) wrap.append(el("p", "caption", question.stem_it));
  wrap.append(el("p", "statement", question.statement_it));
  wrap.append(translationSlot(question));

  const answeredHere = run.answered.has(run.index + 1);
  if (!answeredHere) {
    const row = el("div", "answers");
    const vero = el("button", "btn vero", t("vero"));
    const falso = el("button", "btn falso", t("falso"));
    vero.disabled = falso.disabled = run.busy;
    vero.onclick = () => void submitAnswer(true);
    falso.onclick = () => void submitAnswer(false);
    row.append(vero, falso);
    wrap.append(row);
  } else if (run.session.mode === "practice" && run.verdict) {
    wrap.append(verdictBox(run.verdict));
  } else {
    const next = el("button", "btn primary", t("next"));
    next.onclick = advance;
    wrap.append(next);
  }

  // The practice verdict box carries its own "End test" beside Next, where the decision is
  // actually being made. Rendering the footer as well put two identical controls one above
  // the other, a few pixels apart — which reads as a mistake and makes the user wonder
  // whether the two do different things.
  const duplicated = run.session.mode === "practice" && !!run.verdict && answeredHere;

  const foot = el("div", "run-foot");
  const finish = el("button", "link-btn");
  finish.append(icons.flag(18),
    document.createTextNode(run.session.mode === "exam" ? t("submit_short") : t("end_test")));
  finish.onclick = confirmFinish;
  foot.append(finish);
  if (!duplicated) wrap.append(foot);
  return wrap;
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

async function confirmFinish(): Promise<void> {
  const run = state.run;
  if (!run) return;
  if (run.session.mode === "exam" && !(await ask(t("confirm_submit")))) return;
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
  node.innerHTML = `<circle cx="24" cy="24" r="17" fill="#fff" stroke="currentColor" stroke-width="3"/>
    <path d="M24 24 V9 A15 15 0 0 1 37.5 30 Z" fill="currentColor" opacity=".35"/>
    <path d="M24 24 L33 19" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
    <circle cx="24" cy="24" r="2.4" fill="currentColor"/>
    <rect x="20.5" y="2.5" width="7" height="4" rx="1.6" fill="currentColor"/>`;
  return node;
}

/** The answer sheet: numbered, and it NEVER shows correctness. In a real exam you do not
 *  find out until the end, and that is the property exam mode exists to preserve. */
function answerSheet(run: Run): HTMLElement {
  const sheet = el("div", "sheet");
  for (let i = 0; i < run.session.question_count; i++) {
    const cell = el("i", "cell", String(i + 1));
    if (run.answered.has(i + 1)) cell.classList.add("done");
    if (i === run.index) cell.classList.add("here");
    sheet.append(cell);
  }
  return sheet;
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

  // `correct === false` and not `!correct`: an unanswered question is null, and in an
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

  const passed = r.passed === true;
  const esito = el("div", `esito ${passed ? "pass" : "fail"}`);

  // The medallion. `.esito-mark` and its pass/fail tints have been in style.css from the
  // start and NO .ts file ever emitted the class, so the end of an exam — the moment this
  // product exists to deliver — was a coloured word and nothing else. Both icons default
  // to size 44, a size used nowhere else in the app: they were drawn for this 72-96px
  // circle and then never wired to it.
  const mark = el("div", "esito-mark");
  mark.append(passed ? icons.tick(44) : icons.cross(44));
  esito.append(mark);

  // `esito-sub` below, not `esito-line`. style.css styles the former; the latter has no
  // rule anywhere in the project, so this sentence rendered at browser-default <p> size
  // and margins instead of the 600-weight body copy it was drawn as.
  if (r.mode === "exam") {
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
    r.mode === "exam"
      ? stat(r.question_count - r.answered, t("unanswered"))
      : stat(r.answered - r.wrong, t("correct")),
  );
  esito.append(tally);
  wrap.append(esito);
  if (r.items?.length) wrap.append(reviewList());

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

const BOX_TINT = ["#fef2f2", "#fff7ed", "#fefce8", "#f0fdf4", "#ecfdf5"];
const BOX_INK = ["var(--bad)", "#ea580c", "#ca8a04", "var(--ok)", "var(--ok)"];

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
    tile(icons.eye(20), "#eff6ff", t("questions_seen"), String(s.questions_seen), `/${s.questions_total}`),
    tile(icons.check(20), "#f0fdf4", t("answers_given"), String(s.answers_given)),
    tile(icons.target(20), "#fef2f2", t("error_rate"), `${Math.round(s.error_rate * 100)}`, "%"),
  );
  wrap.append(tiles);

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
    n.style.cssText = `background:#fff;color:${BOX_INK[i - 1]}`;
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

function settingsScreen(): HTMLElement {
  const wrap = el("section", "screen");
  const me = state.me;
  if (!me) { wrap.append(el("div", "spinner")); return wrap; }

  wrap.append(el("h1", "h1", t("settings")));
  wrap.append(el("p", "sub", t("settings_sub")));

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
                  right: 0, typed: "", locked: false };

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
  for (const [id, label] of [["test", t("v_test")], ["list", t("v_list")]] as const) {
    const b = el("button", `v-seg-btn ${v.view === id ? "on" : ""}`, label);
    b.type = "button";
    b.onclick = () => {
      v.view = id as "test" | "list";
      render();
      if (id === "list" && !v.list) void loadVocabList("");
      if (id === "test" && !v.round) void startVocabRound();
    };
    seg.append(b);
  }
  wrap.append(seg);

  if (v.locked) { wrap.append(vocabLocked()); return wrap; }
  wrap.append(v.view === "test" ? vocabTest() : vocabList());
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

  if (!v.list) { box.append(el("p", "v-muted", "…")); return box; }
  if (v.list.terms.length === 0) { box.append(el("p", "v-muted", t("v_empty"))); return box; }

  const list = el("div", "v-list");
  for (const term of v.list.terms) {
    const row = el("div", "v-row");
    const left = el("div", "v-row-main");
    left.append(el("div", "v-row-it", term.it), el("div", "v-row-gloss", term.gloss));
    row.append(left);
    if (term.box >= 4) {
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
                      query: "", segment: "", busy: true };
  render();
  await refreshAdmin();
}

async function refreshAdmin(): Promise<void> {
  if (!state.adminData) return;
  try {
    const [overview, users, links, reports] = await Promise.all([
      admin.overview(),
      admin.users(state.adminData.query, state.adminData.segment),
      admin.links(),
      admin.reports(),
    ]);
    state.adminData = {
      ...state.adminData,
      overview,
      users: users.users,
      links: links.links,
      reports: reports.reports,
      openReports: reports.open,
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

  const head = el("div", "v-head");
  head.append(el("h2", "v-title", "Admin"));
  wrap.append(head);

  if (!data || data.busy) {
    wrap.append(el("p", "caption", t("loading")));
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
  wrap.append(adminReports(data.reports, data.openReports));
  wrap.append(adminUsers(data.users));
  wrap.append(adminGrantMany());
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


/** Find somebody and give them access. This is how the product is sold now. */
function adminUsers(users: AdminUser[]): HTMLElement {
  const card = el("div", "card");
  card.style.marginTop = "var(--md)";
  card.append(el("div", "row-title", "Users"));

  // The same segments the group grant targets — now something to LOOK at rather than only
  // a destination for free days. "Who runs out this week so I can ask them to renew" is the
  // conversation that produces money, and it had no screen.
  const chips = el("div", "chips");
  for (const [value, label] of [
    ["", "All"], ["expiring", "Ending soon"], ["lapsed", "Expired"],
    ["quiet", "Quiet"], ["trial", "Trial"], ["active", "Active"],
  ] as const) {
    const chip = el("button", `chip${state.adminData?.segment === value ? " on" : ""}`, label);
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

  for (const u of users.slice(0, 25)) {
    const row = el("div", "admin-row");
    const who = el("div", "admin-who");
    who.append(el("div", "admin-name", u.name || String(u.chat_id)));
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
  return card;
}

/** The tiers, priced. One tap sets the length and the money together.
 *
 *  Every euro this business records used to enter through three chained window.prompt
 *  boxes — days, then an amount that defaulted to 10.99 whichever length had just been
 *  chosen, then a reason. On a phone that is three modal dialogs, cancelling the middle one
 *  aborted the sale with no trace, and the default price was wrong two times in three.
 *
 *  Prices mirror TIER_PRICE_CENTS in shared/constants.py. */
const GRANT_PRESETS = [
  { label: "1 month · €2.99", days: 30, cents: 299 },
  { label: "3 months · €7.99", days: 90, cents: 799 },
  { label: "6 months · €10.99", days: 180, cents: 1099 },
  { label: "Gift · no payment", days: 30, cents: 0 },
] as const;

async function grantTo(u: AdminUser): Promise<void> {
  const who = u.name || String(u.chat_id);
  const sheet = el("div", "sheet");
  const card = el("div", "sheet-card");
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
      if (!(await ask(`Delete link ${link.code}?`))) return;
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
  const head = el("div", "v-head");
  head.append(el("h2", "v-title", t("ratings")), el("p", "v-sub", t("ratings_week")));
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
    podium.append(seat);
  }
  wrap.append(podium);

  const list = el("div", "rank-list");
  for (const entry of board.entries.slice(3)) {
    const row = el("div", `rank-row${entry.is_me ? " me" : ""}`);
    row.append(el("span", "rank-n", String(entry.rank)));
    row.append(el("span", "rank-name", entry.name || t("ratings_anon")));
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
  if (state.screen === "admin") return () => { state.screen = "settings"; render(); };
  return null;
}

/** The start screen: what is being prepared, while it is prepared.
 *
 *  Names the mode so the wait is attached to something the learner just chose, and says
 *  what is happening rather than spinning silently — "preparing your questions" is a
 *  reason to wait, an unlabelled spinner is not. */
function preparingScreen(): HTMLElement {
  const wrap = el("section", "screen prep");
  const box = el("div", "prep-box");
  box.append(el("div", "prep-spinner"));
  box.append(el("h2", "prep-title",
    state.preparing?.mode === "exam" ? t("exam") : t("practice")));
  box.append(el("p", "prep-sub", t("preparing_quiz")));

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
    case "settings": screen = settingsScreen(); break;
    case "vocab": screen = vocabScreen(); break;
    case "ratings": screen = ratingsScreen(); break;
    case "admin": screen = adminScreen(); break;
    default: screen = homeScreen();
  }
  // Preparing outranks every screen: the learner has tapped Start and nothing else is
  // happening until the quiz opens.
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
