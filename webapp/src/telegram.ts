/** The Telegram WebApp bridge, narrowed to what this app uses.
 *
 *  `initData` is the signed blob the API verifies on every request. It must be sent
 *  back byte-for-byte: it is a query string whose HMAC covers exactly those bytes, so
 *  parsing and re-serialising it breaks the signature. Treat it as opaque. */

interface WebApp {
  initData: string;
  initDataUnsafe: { user?: { id: number; language_code?: string; first_name?: string; username?: string; photo_url?: string } };
  colorScheme: "light" | "dark";
  ready(): void;
  expand(): void;
  close(): void;
  /** Paint the client chrome around the Mini App. Bot API 6.1 / 6.9; older clients
   *  leave these undefined, hence the guards at the call site. */
  setHeaderColor?(color: string): void;
  setBackgroundColor?(color: string): void;
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

/** The Mini App does NOT follow Telegram's theme.
 *
 *  `themeParams` is deliberately ignored. This app's palette carries MEANING that a
 *  repaint would destroy: the soft red mode card means "the test you can fail", the soft
 *  green one means practice, and gold means Premium and nothing else. Recoloured by a
 *  dark theme, those stop being signals and become decoration.
 *
 *  This used to read themeParams and write the result as inline styles on
 *  documentElement — which beat the :root rules in tokens.css, so a user with Telegram in
 *  dark mode got a dark app while the stylesheet claimed otherwise. Inline styles win;
 *  that is the whole reason it looked like the tokens were being ignored.
 *
 *  What we DO tell Telegram is the reverse: paint YOUR chrome to match US. Without it a
 *  dark-mode user gets a dark header sitting above a light app, which reads as a broken
 *  page rather than a deliberate one.
 */
const CHROME_BG = "#f8fafc";   // must stay in step with --bg in tokens.css

export function initTelegram(): void {
  if (!tg) return;
  tg.ready();
  tg.expand();
  // Guarded: these arrived in Bot API 6.1/6.9 and are undefined on older clients, where
  // the app simply renders light inside whatever chrome the client already has.
  try {
    tg.setBackgroundColor?.(CHROME_BG);
    tg.setHeaderColor?.(CHROME_BG);
  } catch {
    /* an old client that has the method but rejects the value is not worth failing over */
  }
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
