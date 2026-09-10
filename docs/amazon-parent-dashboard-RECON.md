# Amazon Kids tablets — recon brief (adapter #4, not yet built)

**Status:** recon tool written, nothing wired up. `warden/adapters/amazon/login.py` captures a
session and records what the dashboard calls. No adapter, no config entry, no signals — the
package is inert until `config/warden.yaml` names it.

## 1. Why bother — what only Amazon can do

Warden's existing control over a tablet is DNS: AdGuard blocks names, per client or per group.
That stops streaming, downloads, app stores and most of the web. It does **not** stop a Fire
tablet playing a film that is already on it, reading a downloaded Kids+ book, or playing an
installed game — which is most of what a Fire tablet is for. A tablet can also be walked out of
range of your DNS entirely (mobile hotspot).

The Parent Dashboard (parents.amazon.co.uk — UK; parents.amazon.com is the US one) reaches the
device itself, through the child's Amazon Kids profile:

| Control | What it gives Warden |
|---|---|
| **Pause / resume device** | A real kill switch that works offline. The prize. |
| **Daily screen-time limits**, per day of week | A budget Warden could tighten or extend by rule |
| **Bedtime / curfew** | Belt-and-braces with the 8pm switchoff |
| **Category limits** (apps / books / video) | "Reading still works after bedtime" |
| **Learn First** | Amazon's own version of the Atom gate, enforced on-device |
| **Activity** (apps, books, videos, time) | Signals: what was used, and for how long |

"Learn First" is worth noticing: it blocks games and cartoons until educational goals are met.
It is coarser than Warden's Atom rule (it can't know about islands or set work), but it works
with the tablet offline, so the two are complementary rather than competing.

## 2. What we know

- Free — an Amazon Kids+ subscription is **not** required for the parental controls.
- Changes sync through the account, not the device: setting it from a phone reaches the tablet.
- Up to 4 child profiles; the same dashboard covers Fire tablets, Kindle, Echo and Fire TV.
- **There is no public API.** Amazon documents none, and no community integration was found
  (searched Sept 2026: no Home Assistant integration, no library). Everything below the UI is
  undocumented, unversioned and free to change without notice.

## 3. What the recon has to answer, in order

Each one is a gate. A "no" at 1 or 3 kills the adapter; the honest answer is then "network-level
control only", and that is fine.

1. **Does a captured session survive, headlessly, days later?**
   The Atom bargain is capture-once-runs-forever, kept alive by a sliding cookie that the
   adapter re-saves on every poll. Amazon's sessions are shorter and better defended. Run
   `--check` today, tomorrow, and a week later. If it dies in a day, no adapter: a control that
   needs a human to sign in every morning is worse than no control.
2. **Is it JSON?** If the dashboard renders server-side, we are scraping a DOM that Amazon
   redesigns at will — the ParentPay `.aspx` lesson (see INTERFACES module F).
3. **How is a call authorised?** Cookie alone is replayable from httpx in the container. A
   bearer token or a per-page anti-CSRF token means fetching a page to mint one before every
   action, or keeping a browser alive — a different, heavier adapter.
4. **What does pause actually call?** Method, path, payload, and what it returns. Recorded by
   the parent pressing Pause themselves in `--watch` mode — a script must not go hunting for
   that button on a real child's tablet.

## 4. How to run it

```bash
cd warden                                          # the app directory
.venv/Scripts/python -m warden.adapters.amazon.login           # sign in by hand, save session
.venv/Scripts/python -m warden.adapters.amazon.login --watch   # record while you click around
.venv/Scripts/python -m warden.adapters.amazon.login --check   # is the saved session still alive?
```

`--watch` opens a real browser and records; **you** drive it. Open each child, open Screen Time,
and press Pause then Resume if you want Warden to be able to do that. Output:

- `data/amazon_state.json` — the session (cookies + localStorage)
- `data/amazon_recon.json` — every XHR: method, path, auth style, payload, response shape

Both are gitignored, and both contain real session material and your children's names and
usage. They stay on this machine.

## 5. Posture, if it gets built

- **Read-only by default.** Collect activity and limits as signals; the only mutation worth
  having is pause/resume, and it should be an explicit action kind (`set_kids_device`), auditable
  like every other switch, and subject to the same manual-hold rules.
- **Never automate the sign-in.** The tool opens a browser for a human. Warden must never hold
  Amazon credentials: it is an account with a payment method on it, sign-in is defended by OTP
  and captcha, and scripted logins are what get accounts flagged.
- **Expect it to break.** Undocumented endpoints change. Whatever is built needs the same
  posture as the Atom adapter: a live failure is a failure, never fixtures passed off as real,
  and never a stale "paused" state presented as current.
