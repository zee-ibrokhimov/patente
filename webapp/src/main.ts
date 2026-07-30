import { api, ApiError } from "./api";
import { lang, setLang, t } from "./i18n";
import { haptic, inTelegram, initTelegram } from "./telegram";
import type { AnswerResult, Me, Question, Stats } from "./types";
import "./style.css";

type Tab = "study" | "stats" | "settings";

const state: {
  me: Me | null;
  question: Question | null;
  answer: AnswerResult | null;
  tab: Tab;
} = { me: null, question: null, answer: null, tab: "study" };

const root = document.getElementById("app")!;

/** textContent everywhere, never innerHTML with content from the API. Explanations are
 *  model-generated and translations come back from an LLM; neither is a trusted source
 *  of markup. */
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
// study
// ---------------------------------------------------------------------------

async function loadQuestion(excludeId?: number): Promise<void> {
  state.answer = null;
  state.question = null;
  render();
  try {
    state.question = await api.nextQuestion({ excludeId });
  } catch (err) {
    return renderError(err);
  }
  render();
  void hydrateTranslation();
}

/** The Italian is already on screen by the time this runs. §15: the question appears
 *  instantly and the translation edits it when it lands — the alternative puts three
 *  to five seconds in front of every interaction. */
async function hydrateTranslation(): Promise<void> {
  const q = state.question;
  if (!q || q.translation_state !== "available") return;
  try {
    const res = await api.translation(q.id);
    if (state.question?.id !== q.id) return; // user moved on; discard
    state.question.translation_state = res.translation_state;
    state.question.translation = res.translation;
    render();
  } catch {
    /* a missing translation is not worth interrupting the session for */
  }
}

async function submit(answer: boolean): Promise<void> {
  const q = state.question;
  if (!q || state.answer) return;
  try {
    state.answer = await api.answer(q.id, answer);
    haptic(state.answer.correct ? "success" : "error");
    if (state.me) state.me.free_explanations_left = state.answer.free_explanations_left;
    render();
  } catch (err) {
    renderError(err);
  }
}

/** The "Why?" fallback, for when background warming has not landed. This one pays for
 *  a model call, which is why it is a deliberate tap and not automatic (STATUS §13). */
async function askWhy(button: HTMLButtonElement): Promise<void> {
  const q = state.question;
  if (!q || !state.answer) return;
  button.disabled = true;
  button.textContent = t("preparing");
  try {
    const res = await api.explanation(q.id);
    state.answer.explanation_state = res.explanation_state;
    state.answer.explanation = res.explanation;
    state.answer.free_explanations_left = res.free_explanations_left;
    if (state.me) state.me.free_explanations_left = res.free_explanations_left;
    render();
  } catch (err) {
    renderError(err);
  }
}

function studyScreen(): HTMLElement {
  const wrap = el("section", "screen");
  const q = state.question;

  if (!q) {
    wrap.append(el("p", "hint", t("loading")));
    return wrap;
  }

  if (q.image) {
    const img = el("img", "figure");
    img.src = api.figureUrl(q.image);
    img.alt = "";
    img.loading = "eager";
    wrap.append(img);
  }

  if (q.stem_it) wrap.append(el("p", "stem", q.stem_it));
  wrap.append(el("p", "statement", q.statement_it));

  // The translation sits under the Italian, never replacing it: the Italian is the
  // thing being learned and the exam is sat in Italian.
  if (q.translation_state === "shown" && q.translation) {
    const tr = el("div", "translation");
    if (q.translation.stem) tr.append(el("p", "stem", q.translation.stem));
    tr.append(el("p", "", q.translation.statement));
    wrap.append(tr);
  } else if (q.translation_state === "available") {
    wrap.append(el("p", "hint", t("translating")));
  } else if (q.translation_state === "locked") {
    wrap.append(el("p", "hint locked", t("translation_locked")));
  }

  if (!state.answer) {
    const row = el("div", "answers");
    const vero = el("button", "btn vero", t("vero"));
    const falso = el("button", "btn falso", t("falso"));
    vero.onclick = () => void submit(true);
    falso.onclick = () => void submit(false);
    row.append(vero, falso);
    wrap.append(row);
  } else {
    wrap.append(verdict(state.answer));
  }
  return wrap;
}

function verdict(a: AnswerResult): HTMLElement {
  const box = el("div", `verdict ${a.correct ? "ok" : "bad"}`);
  box.append(
    el("p", "verdict-line", a.correct ? t("correct") : t("wrong")),
  );
  if (!a.correct) {
    box.append(
      el(
        "p",
        "hint",
        `${t("the_answer_is")}: ${a.correct_answer ? t("vero") : t("falso")}`,
      ),
    );
  }

  if (a.explanation_state === "shown" && a.explanation) {
    box.append(el("p", "explanation", a.explanation));
  } else if (a.explanation_state === "available") {
    const why = el("button", "btn ghost", t("why"));
    why.onclick = () => void askWhy(why);
    box.append(why);
  } else if (a.explanation_state === "locked") {
    box.append(el("p", "hint locked", t("explanation_locked")));
    box.append(el("p", "hint", t("unlock_in_chat")));
  } else if (a.explanation_state === "unavailable") {
    box.append(el("p", "hint", t("explanation_unavailable")));
  }

  if (a.free_explanations_left > 0 && !state.me?.has_pass) {
    box.append(
      el("p", "hint", `${a.free_explanations_left} ${t("free_left")}`),
    );
  }

  const next = el("button", "btn primary", t("next"));
  next.onclick = () => void loadQuestion(a.question_id);
  box.append(next);
  return box;
}

// ---------------------------------------------------------------------------
// stats
// ---------------------------------------------------------------------------

let statsCache: Stats | null = null;

async function loadStats(): Promise<void> {
  try {
    statsCache = await api.stats();
    render();
  } catch (err) {
    renderError(err);
  }
}

function statsScreen(): HTMLElement {
  const wrap = el("section", "screen");
  if (!statsCache) {
    wrap.append(el("p", "hint", t("loading")));
    void loadStats();
    return wrap;
  }
  const s = statsCache;

  const grid = el("div", "grid");
  const tile = (label: string, value: string) => {
    const d = el("div", "tile");
    d.append(el("div", "tile-value", value), el("div", "tile-label", label));
    return d;
  };
  grid.append(
    tile(t("questions_seen"), `${s.questions_seen} / ${s.questions_total}`),
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
      r.append(
        el("div", "topic-name", row.topic),
        el("div", "topic-rate", `${Math.round(row.error_rate * 100)}%`),
      );
      list.append(r);
    }
    wrap.append(list);
  }
  return wrap;
}

// ---------------------------------------------------------------------------
// settings
// ---------------------------------------------------------------------------

function settingsScreen(): HTMLElement {
  const wrap = el("section", "screen");
  const me = state.me;
  if (!me) {
    wrap.append(el("p", "hint", t("loading")));
    return wrap;
  }

  wrap.append(el("h2", "", t("language")));
  const langs = el("div", "choices");
  for (const code of ["it", "ru", "en"] as const) {
    const b = el("button", `chip ${me.lang === code ? "on" : ""}`, code.toUpperCase());
    b.onclick = async () => {
      try {
        state.me = await api.settings({ lang: code });
        setLang(state.me.lang);
        statsCache = null;
        render();
      } catch (err) {
        renderError(err);
      }
    };
    langs.append(b);
  }
  wrap.append(langs);

  wrap.append(el("h2", "", t("translations")));
  const toggle = el(
    "button",
    `chip ${me.translations_on ? "on" : ""}`,
    me.translations_on ? t("on") : t("off"),
  );
  toggle.onclick = async () => {
    try {
      state.me = await api.settings({ translations_on: !me.translations_on });
      render();
    } catch (err) {
      renderError(err);
    }
  };
  wrap.append(toggle);

  const status = el(
    "p",
    "hint",
    me.has_pass ? t("pass_active") : t("no_pass"),
  );
  wrap.append(status);
  // Payments happen in chat, never here — plan §6.2: Mini Apps selling digital goods
  // sit closer to the Stars-only rule and to Apple's review guidelines.
  if (!me.has_pass) wrap.append(el("p", "hint", t("unlock_in_chat")));
  return wrap;
}

// ---------------------------------------------------------------------------
// shell
// ---------------------------------------------------------------------------

function renderError(err: unknown): void {
  root.replaceChildren();
  const wrap = el("section", "screen");
  const message =
    err instanceof ApiError && err.status === 401
      ? t("outside_telegram")
      : t("error");
  wrap.append(el("p", "error", message));
  const retry = el("button", "btn primary", t("retry"));
  retry.onclick = () => void boot();
  wrap.append(retry);
  root.append(wrap, tabs());
}

function tabs(): HTMLElement {
  const bar = el("nav", "tabs");
  const add = (id: Tab, label: string) => {
    const b = el("button", `tab ${state.tab === id ? "on" : ""}`, label);
    b.onclick = () => {
      state.tab = id;
      if (id === "study" && !state.question) void loadQuestion();
      render();
    };
    bar.append(b);
  };
  add("study", t("study"));
  add("stats", t("stats"));
  add("settings", t("settings"));
  return bar;
}

function render(): void {
  root.replaceChildren();
  const screen =
    state.tab === "study"
      ? studyScreen()
      : state.tab === "stats"
        ? statsScreen()
        : settingsScreen();
  root.append(screen, tabs());
}

async function boot(): Promise<void> {
  initTelegram();
  if (!inTelegram) {
    root.replaceChildren();
    root.append(el("p", "error", "Open this page from the bot in Telegram."));
    return;
  }
  try {
    state.me = await api.me();
    setLang(state.me.lang);
    document.documentElement.lang = lang();
    await loadQuestion();
  } catch (err) {
    renderError(err);
  }
}

void boot();
