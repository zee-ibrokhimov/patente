import { api, ApiError, sessions } from "./api";
import { lang, setLang, t } from "./i18n";
import { haptic, inTelegram, initTelegram, tg } from "./telegram";
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
} = { me: null, screen: "home", run: null, results: null, stats: null, profile: null };

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

function advance(): void {
  const run = state.run;
  if (!run) return;
  run.verdict = null;
  if (run.index < run.session.question_count - 1) run.index += 1;
  render();
}

async function finishRun(timedOut = false): Promise<void> {
  const run = state.run;
  if (!run) return;
  try {
    const results = await sessions.finish(run.session.id);
    stopTicking();
    state.results = results;
    state.run = null;
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

  const hero = el("div", "hero");
  hero.append(el("h1", "display", "Quiz Patente"));
  hero.append(el("span", "label", t("tagline")));
  wrap.append(hero);

  const modes = el("div", "modes");
  const card = (mode: Mode, title: string, desc: string) => {
    const b = el("button", `mode ${mode}`);
    b.append(el("div", "mode-title", title), el("div", "mode-desc", desc));
    b.onclick = () => void startRun(mode);
    return b;
  };
  modes.append(
    card("exam", t("exam"), t("exam_desc")),
    card("practice", t("practice"), t("practice_desc")),
  );
  wrap.append(modes);

  if (state.me && !state.me.has_pass) {
    const note = el("p", "hint");
    note.append(document.createTextNode(t("unlock_in_chat")));
    wrap.append(el("h2", "", t("translations")), note);
  }
  return wrap;
}

function currentQuestion(run: Run): Question | undefined {
  return run.session.questions[run.index];
}

function answerSheet(run: Run): HTMLElement {
  const sheet = el("div", "sheet");
  for (let i = 0; i < run.session.question_count; i++) {
    const cell = el("i", "cell");
    if (run.answered.has(i + 1)) cell.classList.add("done");
    if (i === run.index) cell.classList.add("here");
    sheet.append(cell);
  }
  return sheet;
}

function runScreen(): HTMLElement {
  const run = state.run!;
  const wrap = el("section", "screen");
  const question = currentQuestion(run);

  if (run.deadline) {
    const bar = el("div", "exam-bar");
    timerNode = el("div", "timer display", "--:--");
    const count = el("div", "exam-count label");
    count.append(
      document.createTextNode(`${t("answered_n")} `),
      el("b", "", `${run.answered.size}/${run.session.question_count}`),
    );
    bar.append(timerNode, count);
    wrap.append(bar);
    tick();
  }

  wrap.append(answerSheet(run));
  wrap.append(el("div", "label", t("question_of", {
    n: run.index + 1, total: run.session.question_count,
  })));

  if (!question) {
    wrap.append(el("div", "spinner"));
    return wrap;
  }

  if (question.image) {
    const plate = el("div", "plate");
    const img = el("img");
    img.src = api.figureUrl(question.image);
    img.alt = "";
    plate.append(img);
    wrap.append(plate);
  }

  if (question.stem_it) wrap.append(el("p", "stem", question.stem_it));
  wrap.append(el("p", "statement", question.statement_it));

  // The translation sits UNDER the Italian and never replaces it: the exam is sat in
  // Italian and the Italian is the thing being learned.
  if (question.translation_state === "shown" && question.translation) {
    const tr = el("div", "translation");
    if (question.translation.stem) tr.append(el("p", "stem", question.translation.stem));
    tr.append(el("p", "", question.translation.statement));
    wrap.append(tr);
  } else if (question.translation_state === "locked") {
    wrap.append(el("p", "hint locked", t("translation_locked")));
  }

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

  const finish = el("button", "btn ghost",
    run.session.mode === "exam" ? t("submit") : t("end_test"));
  finish.onclick = () => {
    if (run.session.mode === "exam" && !confirm(t("confirm_submit"))) return;
    void finishRun();
  };
  wrap.append(finish);
  return wrap;
}

function verdictBox(a: AnswerResult): HTMLElement {
  const box = el("div", `verdict ${a.correct ? "ok" : "bad"}`);
  box.append(el("p", "verdict-line display", a.correct ? t("correct") : t("wrong")));
  if (!a.correct) {
    box.append(el("p", "hint",
      `${t("the_answer_is")}: ${a.correct_answer ? t("vero") : t("falso")}`));
  }

  if (a.explanation_state === "shown" && a.explanation) {
    box.append(el("p", "explanation", a.explanation));
  } else if (a.explanation_state === "available") {
    const why = el("button", "btn ghost", t("why"));
    why.onclick = () => void askWhy(why, a.question_id);
    box.append(why);
  } else if (a.explanation_state === "locked") {
    box.append(el("p", "hint locked", t("explanation_locked")));
    box.append(el("p", "hint", t("unlock_in_chat")));
  } else if (a.explanation_state === "unavailable") {
    box.append(el("p", "hint", t("explanation_unavailable")));
  }

  const next = el("button", "btn primary", t("next"));
  next.onclick = advance;
  box.append(next);
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

function tabs(): HTMLElement {
  const bar = el("nav", "tabs");
  const add = (id: Screen, label: string) => {
    const b = el("button", `tab ${state.screen === id ? "on" : ""}`, label);
    b.onclick = () => { state.screen = id; render(); };
    bar.append(b);
  };
  add("home", t("home"));
  add("profile", t("profile"));
  add("stats", t("stats"));
  add("settings", t("settings"));
  return bar;
}

function render(): void {
  root.replaceChildren();

  let screen: HTMLElement;
  switch (state.screen) {
    case "run": screen = runScreen(); break;
    case "results": screen = resultsScreen(); break;
    case "profile": screen = profileScreen(); break;
    case "stats": screen = statsScreen(); break;
    case "settings": screen = settingsScreen(); break;
    default: screen = homeScreen();
  }
  root.append(screen);

  // The tab bar is suppressed while a sitting is in flight. Previously it was appended
  // unconditionally, so a candidate could tap Stats mid-exam and silently abandon a
  // timed run — with no warning and no way back.
  if (state.screen !== "run") {
    root.append(tabs());
  }

  const tri = el("div", "tricolore");
  tri.append(el("i"), el("i"), el("i"));
  root.append(tri);
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
