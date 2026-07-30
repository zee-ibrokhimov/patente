/** Copy for the Mini App.
 *
 *  The bot's locales in bot/locales/*.json are written for chat — they carry HTML tags,
 *  emoji buttons and message-shaped phrasing that do not fit a screen. Rather than
 *  strip them at runtime, the handful of strings this app needs live here, in the same
 *  three languages the bot supports (shared/constants.UI_LANGUAGES).
 *
 *  If a string exists in both places it must say the same thing in both. The paywall
 *  wording especially: a user who sees one message in chat and a different one here
 *  will read it as two different products. */

export type Lang = "it" | "ru" | "en";

const STRINGS = {
  it: {
    study: "Studio",
    stats: "Statistiche",
    settings: "Impostazioni",
    vero: "VERO",
    falso: "FALSO",
    correct: "Corretto",
    wrong: "Sbagliato",
    the_answer_is: "La risposta è",
    next: "Avanti",
    why: "Perché?",
    preparing: "Sto preparando la spiegazione…",
    translating: "Traduzione…",
    explanation_locked: "Le spiegazioni sono incluse nell'abbonamento.",
    explanation_unavailable: "Spiegazione non ancora disponibile per questa domanda.",
    translation_locked: "Le traduzioni sono incluse nell'abbonamento.",
    unlock_in_chat: "Apri il bot in chat per attivare l'abbonamento.",
    free_left: "spiegazioni gratuite rimaste",
    questions_seen: "Domande viste",
    answers_given: "Risposte date",
    error_rate: "Percentuale di errore",
    boxes: "Ripetizione dilazionata",
    by_topic: "Per argomento",
    language: "Lingua",
    translations: "Traduzioni",
    on: "Attive",
    off: "Disattivate",
    no_questions: "Nessuna domanda disponibile.",
    error: "Qualcosa è andato storto.",
    retry: "Riprova",
    outside_telegram: "Apri questa pagina dal bot su Telegram.",
    pass_active: "Abbonamento attivo",
    no_pass: "Nessun abbonamento",
    loading: "Caricamento…",
  },
  ru: {
    study: "Учёба",
    stats: "Статистика",
    settings: "Настройки",
    vero: "ВЕРНО",
    falso: "НЕВЕРНО",
    correct: "Правильно",
    wrong: "Неправильно",
    the_answer_is: "Правильный ответ",
    next: "Дальше",
    why: "Почему?",
    preparing: "Готовлю объяснение…",
    translating: "Перевод…",
    explanation_locked: "Объяснения входят в подписку.",
    explanation_unavailable: "Объяснение для этого вопроса пока недоступно.",
    translation_locked: "Переводы входят в подписку.",
    unlock_in_chat: "Откройте бота в чате, чтобы оформить подписку.",
    free_left: "бесплатных объяснений осталось",
    questions_seen: "Просмотрено вопросов",
    answers_given: "Дано ответов",
    error_rate: "Доля ошибок",
    boxes: "Интервальное повторение",
    by_topic: "По темам",
    language: "Язык",
    translations: "Переводы",
    on: "Включены",
    off: "Выключены",
    no_questions: "Нет доступных вопросов.",
    error: "Что-то пошло не так.",
    retry: "Повторить",
    outside_telegram: "Откройте эту страницу через бота в Telegram.",
    pass_active: "Подписка активна",
    no_pass: "Подписки нет",
    loading: "Загрузка…",
  },
  en: {
    study: "Study",
    stats: "Stats",
    settings: "Settings",
    vero: "TRUE",
    falso: "FALSE",
    correct: "Correct",
    wrong: "Wrong",
    the_answer_is: "The answer is",
    next: "Next",
    why: "Why?",
    preparing: "Preparing the explanation…",
    translating: "Translating…",
    explanation_locked: "Explanations are part of the subscription.",
    explanation_unavailable: "No explanation available for this question yet.",
    translation_locked: "Translations are part of the subscription.",
    unlock_in_chat: "Open the bot in chat to subscribe.",
    free_left: "free explanations left",
    questions_seen: "Questions seen",
    answers_given: "Answers given",
    error_rate: "Error rate",
    boxes: "Spaced repetition",
    by_topic: "By topic",
    language: "Language",
    translations: "Translations",
    on: "On",
    off: "Off",
    no_questions: "No question available.",
    error: "Something went wrong.",
    retry: "Retry",
    outside_telegram: "Open this page from the bot in Telegram.",
    pass_active: "Subscription active",
    no_pass: "No subscription",
    loading: "Loading…",
  },
} as const;

export type Key = keyof (typeof STRINGS)["en"];

let current: Lang = "it";

export function setLang(lang: string): void {
  current = (["it", "ru", "en"] as const).includes(lang as Lang) ? (lang as Lang) : "it";
}

export function t(key: Key): string {
  return STRINGS[current][key];
}

export function lang(): Lang {
  return current;
}
