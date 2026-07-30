import { api, ApiError, sessions } from "./api";
import { art, icons } from "./icons";
import { lang, setLang, t } from "./i18n";
import { haptic, inTelegram, initTelegram, setBackButton, tg } from "./telegram";
import type {
  AnswerResult,
  ExamAnswer,
  Me,
  Mode,
  PracticeAnswer,
  Question,
  Profile,
  Session,
  SessionResults,
  Stats,
} from "./types";
import "./style.css";

type Screen = "home" | "run" | "results" | "profile" | "stats" | "settings";

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

const state: {
  me: Me | null;
  screen: Screen;
  run: Run | null;
  results: SessionResults | null;
  stats: Stats | null;
  profile: Profile | null;
  /** A sitting the user walked away from. Held so the back button cannot lose an exam. */
  resumable: Session | null;
} = { me: null, screen: "home", run: null, results: null, stats: null, profile: null,
      resumable: null };

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
  if (run.index < run.session.question_count - 1) run.index += 1;
  render();   // render() kicks off hydrateTranslation for the new question
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

  // If a sitting was left without finishing, offer it back BEFORE the promotion. Losing
  // an exam by tapping back would make the back button a trap.
  if (state.resumable) wrap.append(resumeCard(state.resumable));

  // The promotion is the B variant of this screen and sits BELOW the cards, so it can
  // never push the two things this screen exists for off the fold.
  if (state.me && !state.me.has_pass) wrap.append(premiumBlock());
  return wrap;
}

/** One mode card. Artwork left, everything readable right.
 *
 *  The artwork is decoration: the tag, title and description carry the meaning, because
 *  artwork is the first thing to be clipped on a narrow phone. */
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
  const tr = { title: t("f_tr"), sub: t("f_tr_s") };
  const expl = { title: t("f_expl"), sub: t("f_expl_s") };
  const stats = { title: t("f_stats"), sub: t("f_stats_s") };
  const all = { title: t("f_all"), sub: t("f_all_s") };
  // "explain" leads with explanations because the user got something wrong and is
  // asking why; "sell" leads with translation, which is the broader hook.
  return order === "explain" ? [expl, tr, stats, all] : [tr, expl, stats, all];
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
function openSubscribe(): void {
  toast(t("unlock_in_chat"));
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

  wrap.append(answerSheet(run));
  wrap.append(el("div", "q-index",
    t("question_of", { n: run.index + 1, total: run.session.question_count })));

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
    stat(r.question_count - r.answered, t("unanswered")),
  );
  esito.append(tally);
  wrap.append(esito);

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

  // Identity comes from initDataUnsafe, which is fine for DISPLAY only — it is the
  // unverified copy. Anything that matters is keyed off the signed blob server-side.
  const tgUser = tg?.initDataUnsafe?.user;
  const name = tgUser?.first_name ?? "";
  const who = el("div", "who");
  const avatar = el("div", "avatar");
  if (tgUser?.photo_url) {
    const img = el("img");
    img.src = tgUser.photo_url;
    img.alt = "";
    avatar.append(img);
  } else {
    avatar.textContent = (name.trim()[0] ?? "?").toUpperCase();
  }
  const who_text = el("div");
  who_text.append(el("div", "who-name", name || t("profile")));
  if (p.streak_days > 0) {
    const streak = el("div", "who-streak");
    streak.append(el("b", "", `🔥 ${p.streak_days}`),
                  document.createTextNode(` ${t("streak_days")}`));
    who_text.append(streak);
  }
  who.append(avatar, who_text);
  wrap.append(who);

  // --- readiness ---
  const pct = p.readiness == null ? null : Math.round(p.readiness * 100);
  const ready = pct != null && p.readiness! >= p.pass_accuracy;
  const gauge = el("div", `gauge ${pct == null ? "" : ready ? "ready" : "notyet"}`);
  const head = el("div", "gauge-head");
  head.append(el("div", "label", t("ready_title")));
  head.append(el("div", "gauge-value", pct == null ? "—" : `${pct}%`));
  gauge.append(head);

  if (pct == null) {
    // The server refuses to estimate below a minimum sample, and this renders that
    // refusal rather than drawing a 0% bar that would read as "you know nothing".
    gauge.append(el("p", "gauge-empty",
      t("need_more", { n: p.readiness_min_sample })));
  } else {
    const track = el("div", "gauge-track");
    const fill = el("div", "gauge-fill");
    fill.style.width = `${pct}%`;
    const mark = el("div", "gauge-mark");
    mark.style.left = `${Math.round(p.pass_accuracy * 100)}%`;
    track.append(fill, mark);
    gauge.append(track);

    const foot = el("div", "gauge-foot");
    foot.append(el("span", "label", t("based_on", { n: p.readiness_sample })));
    foot.append(el("span", "label",
      t("pass_bar", { n: Math.round(p.pass_accuracy * 100) })));
    gauge.append(foot);
  }
  wrap.append(gauge);

  // --- exams ---
  const grid = el("div", "grid");
  const tile = (label: string, value: string) => {
    const d = el("div", "tile");
    d.append(el("div", "tile-value", value), el("div", "tile-label", label));
    return d;
  };
  grid.append(
    tile(t("exams_taken"), String(p.exams.taken)),
    tile(t("exams_passed"), String(p.exams.passed)),
    tile(t("avg_errors"), p.exams.avg_errors == null ? "—" : String(p.exams.avg_errors)),
  );
  wrap.append(el("h2", "", t("history")), grid);

  if (!p.exams.recent.length) {
    wrap.append(el("p", "hint", t("no_exams")));
  } else {
    const list = el("div", "history");
    for (const run of p.exams.recent) {
      const row = el("div", "run-row");
      row.append(el("span", `run-badge ${run.passed ? "pass" : "fail"}`,
        run.passed ? t("passed") : t("failed")));
      const when = run.finished_at
        ? new Date(run.finished_at).toLocaleDateString(lang())
        : "";
      row.append(el("span", "run-meta", when));
      row.append(el("span", "run-score", `${run.wrong}/${run.question_count}`));
      list.append(row);
    }
    wrap.append(list);
  }

  // The weakest topics live on Stats; pointing at them from here is the whole job of
  // this screen - it is where someone decides what to do next.
  const go = el("button", "btn ghost", t("by_topic"));
  go.onclick = () => { state.screen = "stats"; render(); };
  wrap.append(go);
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


function statsScreen(): HTMLElement {
  const wrap = el("section", "screen");
  if (!state.stats) {
    wrap.append(el("div", "spinner"));
    void loadStats();
    return wrap;
  }
  const s = state.stats;

  const grid = el("div", "grid");
  const tile = (label: string, value: string) => {
    const d = el("div", "tile");
    d.append(el("div", "tile-value", value), el("div", "tile-label", label));
    return d;
  };
  grid.append(
    tile(t("questions_seen"), `${s.questions_seen}/${s.questions_total}`),
    tile(t("answers_given"), String(s.answers_given)),
    tile(t("error_rate"), `${Math.round(s.error_rate * 100)}%`),
  );
  wrap.append(grid);

  wrap.append(el("h2", "", t("boxes")));
  const boxes = el("div", "boxes");
  for (const [box, count] of Object.entries(s.boxes)) {
    const b = el("div", "box");
    b.append(el("div", "box-n", box), el("div", "box-c", String(count)));
    boxes.append(b);
  }
  wrap.append(boxes);

  if (s.by_topic.length) {
    wrap.append(el("h2", "", t("by_topic")));
    const list = el("div", "topics");
    for (const row of [...s.by_topic].sort((a, b) => b.error_rate - a.error_rate)) {
      const r = el("div", "topic");
      r.append(el("div", "topic-name", row.topic),
               el("div", "topic-rate", `${Math.round(row.error_rate * 100)}%`));
      list.append(r);
    }
    wrap.append(list);
  }
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

function settingsScreen(): HTMLElement {
  const wrap = el("section", "screen");
  const me = state.me;
  if (!me) { wrap.append(el("div", "spinner")); return wrap; }

  wrap.append(el("h2", "", t("language")));
  const langs = el("div", "chips");
  for (const code of ["it", "ru", "en"] as const) {
    const b = el("button", `chip ${me.lang === code ? "on" : ""}`, code.toUpperCase());
    b.onclick = async () => {
      try {
        state.me = await api.settings({ lang: code });
        setLang(state.me.lang);
        document.documentElement.lang = lang();
        state.stats = null;
        render();
      } catch (err) { reportError(err); }
    };
    langs.append(b);
  }
  wrap.append(langs);

  wrap.append(el("h2", "", t("translations")));
  const toggle = el("button", `chip ${me.translations_on ? "on" : ""}`,
    me.translations_on ? t("on") : t("off"));
  toggle.onclick = async () => {
    try {
      state.me = await api.settings({ translations_on: !me.translations_on });
      render();
    } catch (err) { reportError(err); }
  };
  wrap.append(toggle);

  wrap.append(el("p", "hint", me.has_pass ? t("pass_active") : t("no_pass")));
  if (!me.has_pass) wrap.append(el("p", "hint", t("unlock_in_chat")));
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
