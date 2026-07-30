/** The Telegram WebApp bridge, narrowed to what this app uses.
 *
 *  `initData` is the signed blob the API verifies on every request. It must be sent
 *  back byte-for-byte: it is a query string whose HMAC covers exactly those bytes, so
 *  parsing and re-serialising it breaks the signature. Treat it as opaque. */

interface ThemeParams {
  bg_color?: string;
  text_color?: string;
  hint_color?: string;
  link_color?: string;
  button_color?: string;
  button_text_color?: string;
  secondary_bg_color?: string;
}

interface WebApp {
  initData: string;
  initDataUnsafe: { user?: { id: number; language_code?: string; first_name?: string; username?: string; photo_url?: string } };
  colorScheme: "light" | "dark";
  themeParams: ThemeParams;
  ready(): void;
  expand(): void;
  close(): void;
  /** Telegram's own back arrow, in the client header. Present from Bot API 6.1; older
   *  clients leave it undefined, which is why every call site guards. */
  BackButton?: {
    isVisible: boolean;
    show(): void;
    hide(): void;
    onClick(cb: () => void): void;
    offClick(cb: () => void): void;
  };
  HapticFeedback?: {
    notificationOccurred(type: "error" | "success" | "warning"): void;
    impactOccurred(style: "light" | "medium" | "heavy"): void;
  };
}

declare global {
  interface Window {
    Telegram?: { WebApp: WebApp };
  }
}

export const tg = window.Telegram?.WebApp;

/** Outside the Telegram client there is no initData and every request will 401.
 *  That is correct — the API has no other way to know who is asking — so the UI says
 *  so plainly rather than showing an app that silently fails on every tap. */
export const inTelegram = Boolean(tg && tg.initData);

export function initTelegram(): void {
  if (!tg) return;
  tg.ready();
  tg.expand();
  applyTheme(tg);
}

function applyTheme(app: WebApp): void {
  const root = document.documentElement;
  const p = app.themeParams;
  const set = (name: string, value: string | undefined) => {
    if (value) root.style.setProperty(name, value);
  };
  set("--bg", p.bg_color);
  set("--text", p.text_color);
  set("--hint", p.hint_color);
  set("--link", p.link_color);
  set("--button", p.button_color);
  set("--button-text", p.button_text_color);
  set("--surface", p.secondary_bg_color);
  root.dataset.scheme = app.colorScheme;
}

export function haptic(kind: "success" | "error"): void {
  tg?.HapticFeedback?.notificationOccurred(kind);
}


/** Drive Telegram's header back arrow.
 *
 *  Only one handler is ever registered: the previous one is removed first, because
 *  onClick appends rather than replaces and a screen rendered twice would otherwise fire
 *  its handler twice — which, on the exam screen, means two confirm dialogs.
 *
 *  Returns false when the client is too old to have a back button, so the caller can
 *  render an in-screen control instead rather than leaving the user trapped.
 */
let backHandler: (() => void) | null = null;

export function setBackButton(handler: (() => void) | null): boolean {
  const button = tg?.BackButton;
  if (!button) return false;

  if (backHandler) {
    button.offClick(backHandler);
    backHandler = null;
  }
  if (handler) {
    backHandler = handler;
    button.onClick(handler);
    button.show();
  } else {
    button.hide();
  }
  return true;
}
