import { api, ApiError, sessions, vocab } from "./api";
import { readinessGauge } from "./gauge";
import { art, icons } from "./icons";
import { TRANSLATION_LANGUAGES, lang, setLang, t } from "./i18n";
import { haptic, inTelegram, initTelegram, openChat, setBackButton, tg } from "./telegram";
import type {
  AnswerResult,
  ExamAnswer,
  Me,
  Mode,
  PracticeAnswer,
  Profile,
  Question,
  Session,
  SessionResults,
  Stats,
  VocabAnswer,
  VocabList,
  VocabRound,
  VocabStats,
} from "./types";
import "./style.css";

type Screen = "home" | "run" | "results" | "profile" | "stats" | "settings" | "vocab";

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
} = { me: null, screen: "home", run: null, results: null, stats: null, profile: null,
      resumable: null, reviewWrongOnly: true,
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

async function startRun(mode: Mode): Promise<void> {
  try {
    const session = await sessions.start(mode);
    enterRun(session);
  } catch (err) {
    reportError(err);
  }
}

function enterRun(session: Session): void {
  const serverNow = Date.parse(session.server_now);
  state.run = {
    session,
    index: Math.min(session.answered, session.question_count - 1),
    answered: new Set(),
    verdict: null,
    deadline: session.expires_at ? Date.parse(session.expires_at) : null,
    skew: serverNow - Date.now(),
    busy: false,
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
  try {
    const res = await sessions.answer(run.session.id, ordinal, given);
    run.answered.add(ordinal);
    haptic("success");

    if (run.session.mode === "practice") {
      run.verdict = res as PracticeAnswer;
      haptic((res as PracticeAnswer).correct ? "success" : "error");
      if (state.me) state.me.free_explanations_left = (res as PracticeAnswer).free_explanations_left;
    } else {
      // Exam: the response carries no verdict at all, by design. Advance immediately.
      void (res as ExamAnswer);
      if (run.index < run.session.question_count - 1) run.index += 1;
    }
  } catch (err) {
    // Non-destructive: the sitting survives, the user can tap again.
    reportError(err);
  } finally {
    run.busy = false;
    render();
  }
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
    /* a missing translation is never worth interrupting a sitting for */
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
function vocabEntry(): HTMLElement {
  const card = el("button", "v-entry");
  card.type = "button";
  card.append(el("span", "v-entry-icon", "\u{1F4DA}"));
  const body = el("span", "v-entry-body");
  body.append(el("span", "v-entry-title", t("v_title")),
              el("span", "v-entry-sub", t("v_sub")));
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
  const vocab = { title: t("f_vocab"), sub: t("f_vocab_s") };
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

  const foot = el("div", "run-foot");
  const finish = el("button", "link-btn");
  finish.append(icons.flag(18),
    document.createTextNode(run.session.mode === "exam" ? t("submit_short") : t("end_test")));
  finish.onclick = confirmFinish;
  foot.append(finish);
  wrap.append(foot);
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
function leaveRun(): void {
  const run = state.run;
  if (!run) { goHome(); return; }
  if (run.session.mode === "exam" && !confirm(t("confirm_leave"))) return;
  stopTicking();
  state.resumable = run.session;
  state.run = null;
  goHome();
}

function goHome(): void {
  state.screen = "home";
  render();
}

function confirmFinish(): void {
  const run = state.run;
  if (!run) return;
  if (run.session.mode === "exam" && !confirm(t("confirm_submit"))) return;
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
    button.disabled = false;
    reportError(err);
  }
}

function resultsScreen(): HTMLElement {
  const r = state.results!;
  const wrap = el("section", "screen");

  const passed = r.passed === true;
  const esito = el("div", `esito ${passed ? "pass" : "fail"}`);
  if (r.mode === "exam") {
    esito.append(el("p", "esito-verdict", passed ? t("passed") : t("failed")));
    esito.append(el("p", "esito-line",
      `${r.wrong} ${t("errors").toLowerCase()} / ${r.max_errors} ${t("allowed").toLowerCase()}`));
  } else {
    esito.append(el("p", "esito-verdict display",
      `${r.answered - r.wrong}/${r.answered}`));
    esito.append(el("p", "esito-line", t("answers_given")));
  }

  const tally = el("div", "tally");
  const stat = (n: string | number, label: string) => {
    const d = el("div");
    d.append(el("div", "n display", String(n)), el("div", "label", label));
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

  const home = el("button", "btn ghost", t("back_home"));
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
        state.stats = null;
        state.profile = null;
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
  head.append(el("h2", "v-title", t("v_title")), el("p", "v-sub", t("v_sub")));
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
  return wrap;
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
  // Debounced: a request per keystroke over 1090 rows is a lot of round trips for a
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

function tabs(): HTMLElement {
  const bar = el("nav", "tabs");
  const add = (id: Screen, label: string, icon: SVGSVGElement) => {
    const b = el("button", `tab ${state.screen === id ? "on" : ""}`);
    b.type = "button";
    b.append(icon, el("span", "", label));
    b.onclick = () => { state.screen = id; render(); };
    bar.append(b);
  };
  add("home", t("home"), icons.home(23));
  add("profile", t("profile"), icons.person(23));
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
  return null;
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
    default: screen = homeScreen();
  }
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

async function boot(): Promise<void> {
  initTelegram();
  if (!inTelegram) {
    root.replaceChildren(el("p", "error", "Open this page from the bot in Telegram."));
    return;
  }
  try {
    state.me = await api.me();
    setLang(state.me.lang);
    document.documentElement.lang = lang();
    render();
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
