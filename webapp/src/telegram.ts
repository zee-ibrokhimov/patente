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
  /** Opens a t.me link INSIDE Telegram — the app closes and the chat opens. Absent on
   *  older clients, which is why every caller checks before using it. */
  openTelegramLink?(url: string): void;
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
  /** Stops a downward swipe from minimising the Mini App. Bot API 7.7; undefined below it.
   *  Without it, a scroll gesture that begins near the top of a running exam collapses the
   *  app — with the twenty-minute clock still running. */
  disableVerticalSwipes?(): void;
  /** Telegram's own confirm sheet. Bot API 6.2. `window.confirm` inside the webview is a
   *  bare browser dialog that some Android clients style badly or suppress outright, and a
   *  suppressed confirm means the user cannot leave a running exam at all. */
  showConfirm?(message: string, callback: (ok: boolean) => void): void;
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
    // A timed exam is the one screen where losing the app costs something irreversible,
    // and the default gesture minimises it on a downward swipe. Same optional-call guard
    // as the colours above: absent below Bot API 7.7, where the behaviour is unchanged.
    tg.disableVerticalSwipes?.();
  } catch {
    /* an old client that has the method but rejects the value is not worth failing over */
  }
}

/** Ask a yes/no question, preferring Telegram's sheet over the browser dialog.
 *
 *  `window.confirm` blocks the webview's single thread and some Android Telegram builds
 *  suppress it entirely. When that happens the callback never fires and the user is stuck
 *  in a running exam with no way out — so the fallback is the thing to avoid, not the
 *  thing to rely on. */
export function ask(message: string): Promise<boolean> {
  if (tg?.showConfirm) {
    return new Promise((resolve) => {
      try {
        tg.showConfirm!(message, (ok) => resolve(ok));
      } catch {
        resolve(window.confirm(message));
      }
    });
  }
  return Promise.resolve(window.confirm(message));
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

/** Leave the Mini App and land in a Telegram chat.
 *
 *  The whole reason this exists: every paid surface in the app used to call a function
 *  that showed a toast reading "open the bot in chat to subscribe" and did nothing else.
 *  Payments were live and the buy button was a sentence that faded after three seconds —
 *  a learner who wanted to pay had to close the app, find the bot, and guess that /plan
 *  existed. Nobody told them.
 *
 *  Returns false when the client is too old to have the method, so the caller can fall
 *  back to telling them rather than silently doing nothing.
 */
export function openChat(url: string): boolean {
  try {
    if (tg?.openTelegramLink) {
      tg.openTelegramLink(url);
      return true;
    }
  } catch {
    /* fall through to the caller's fallback */
  }
  return false;
}
