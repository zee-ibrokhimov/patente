# Turning payments on

Everything on the server is built and inert. This is the list of things only you can do.

Nothing here is reversible-by-accident: until `TRIBUTE_WEBHOOK_SECRET` is set, the
webhook refuses every delivery and `/plan` tells users payments are not connected. So you
can do steps 1–4 at your own pace and nothing changes for anyone.

---

## 1. A channel, because Tribute needs one

Tribute is built for creators selling access to a Telegram channel. Its subscription flow
begins "select the channel", and its bot connects only to channels and private groups —
so a subscription cannot float free of one.

Your product is a Mini App, not a channel, so the channel is a formality. Make it useful
rather than empty:

- Create a **private channel**, e.g. *Quiz Patente Premium*
- Post occasionally: new features, exam-rule changes, study tips
- Subscribers get added automatically — a small extra thing they are buying

**Verify this yourself before building anything on it.** Open `@tribute`, start the
create-subscription flow, and see whether it will proceed without a channel. Their
documentation does not say outright that one is mandatory; the flow implies it. Two
minutes of clicking beats my inference.

## 2. Create the three subscriptions

In `@tribute` → **Menu** → select your channel → **Subscription**.

| Period to choose | Price | Maps to |
|---|---|---|
| monthly | €2.99 | `pass_1m` |
| quarterly | €7.99 | `pass_3m` |
| halfyearly | €10.99 | `pass_6m` |

The period is how the server decides what to grant — a subscription webhook carries no
product id, so `monthly`/`quarterly`/`halfyearly` **must** match the price you set. Pick
`quarterly` and charge €10.99 and the buyer gets 3 months for the 6-month price.

Currency **EUR**. Name and description are shown at checkout, so write them for a buyer,
not for you.

## 3. The trial

While creating or editing a subscription: **Payments** → enable **Trial period**.

Offered: 1, 12 or 24 hours, or 3 or 7 days. Choose **7 days**.

What it does, from Tribute's documentation:

- The user must **link a card** to start
- At the end it **renews automatically as paid** unless cancelled first
- They can cancel during the trial and pay nothing

Two things to know:

- **Adding a trial to an existing subscription affects new subscribers only.**
- **To offer the trial only on the 3-month plan**, enable it on that subscription and
  leave it off the other two. No code change — the server does not care which tiers have
  one.

⚠️ Before this goes live, talk to your commercialista. Auto-charging a card after a free
trial is regulated in the EU: pre-contractual disclosure, explicit consent to the
recurring charge, and the 14-day withdrawal right. Your plan §4.2 currently says the
trial ends *without* charging, so this reverses a deliberate decision. The exposure is on
your VAT number, not on the code.

## 4. Collect four values

**The API key** — in Tribute's API/developer section.

**The webhook secret** — per Tribute's docs the signature is *"HMAC-SHA256 of request
body signed with your API key"*, so **the secret is the API key**. Set both to the same
value unless their dashboard gives you a separate one.

**The three checkout links** — the URL Tribute gives you when each subscription is
published. One per tier. They look like `https://t.me/tribute/app?startapp=…`.

## 5. Point Tribute at the server

Webhook URL:

```
https://patente.zeehub.xyz/webhooks/tribute
```

Already live and verified from the internet: `POST` reaches the API, `GET` is refused
403 at the edge, and every other path under `/webhooks/` is 404. Right now it answers
400 *"TRIBUTE_WEBHOOK_SECRET is not configured"* — which is correct, and becomes a
working endpoint the moment step 6 is done.

## 6. Send me the values

I set them in Coolify and redeploy:

```
TRIBUTE_API_KEY
TRIBUTE_WEBHOOK_SECRET     (same as the API key)
TRIBUTE_LINK_1M
TRIBUTE_LINK_3M
TRIBUTE_LINK_6M
```

`TRIBUTE_PRODUCT_1M/3M/6M` stay **empty** — those are for one-off digital products, and
a subscription has no product id.

The moment the secret and one link are set, `/plan` stops saying "payments are not
connected" and grows a button per tier.

---

## What happens then, end to end

1. User taps a tier in `/plan` → Tribute checkout
2. They link a card → Tribute sends `new_subscription` with `type: "trial"`
3. Server grants access to Tribute's `expires_at`, records a trial, and does **not**
   count them as a paying customer
4. Seven days later Tribute charges and sends `renewed_subscription` with
   `type: "regular"` → the pass extends and they become a customer
5. If they cancel: `cancelled_subscription` → **access is not revoked**; it runs to the
   end of the paid period and then lapses
6. A refund sends `digital_product_refunded` → the time that purchase granted is taken
   back

Every one of those paths has tests. What none of them have is a real delivery from
Tribute, so watch the first one:

```bash
docker logs -f $(docker ps -q --filter "name=api-rboj5u2xk5dj4o4yto89ablh") 2>&1 | grep -i tribute
```

A payload that does not parse is logged **in full** at ERROR, deliberately — the first
real delivery is the documentation, and adapting is a change in one function.

## First-sale checklist

- [ ] `/plan` shows three buttons, each opening the right checkout
- [ ] Buy the cheapest tier yourself with a real card
- [ ] `docker logs` shows the webhook arriving and being applied
- [ ] Your own `/plan` now shows an expiry date
- [ ] Cancel it in Tribute, confirm access **stays** until the period ends
- [ ] Refund it, confirm the time is taken back
