# Rental watcher

Polls Craigslist Vancouver for new studio/1-bedroom rentals within walking
distance of Amazon YVR14 (402 Dunsmuir St) and emails you each one with a link.
Reply to that email and it sends the poster your pre-set intro message.

Email is the default because it needs nothing but a Gmail App Password. SMS is
supported too, but needs a paid Twilio account and a public URL.

## Is Craigslist scrapable?

Yes in practice, with two caveats worth knowing before you rely on this.

**Finding listings: works well.** Craigslist has no public API and its terms
prohibit scraping, but the site's own front end talks to an internal JSON
endpoint (`sapi.craigslist.org`) that returns every result *with coordinates
already attached* and honours a radius filter server-side. That means one HTTP
request per poll rather than hundreds of page fetches — far gentler on them and
much faster for you. At one request every 10 minutes from a home IP you should be
fine; poll aggressively and you'll get your IP blocked. The response format is
undocumented compact arrays, so it can change without notice; `craigslist.py`
documents the layout it decodes and degrades instead of crashing when a field
moves.

**Replying: cannot be automated.** This is the part that doesn't work the way you
hoped, and it's worth being upfront about. Craigslist replies go through an
anonymised relay address that is revealed only after an **hCaptcha image
challenge**. Verified against the live site:

- the reply button's URL is a placeholder, `/reply/van/apa/<id>/__SERVICE_ID__`;
  the real path is `/reply/van/apa/<id>/init`
- that path returns 404 over plain HTTP even with the post's own cookies — it
  resolves only inside a session their JavaScript established
- in a real browser it does resolve, then immediately loads hCaptcha before
  showing the address

So "reply to my alert and the app posts my message" isn't achievable without
defeating a captcha, which this project won't do. What it does instead is
**assisted reply**: replying opens a browser window on the machine running the
app, already on the reply form with your message typed in, so you solve the
captcha and hit send. It also retries unattended each time against a persistent
browser profile — hCaptcha often stops challenging a profile that has passed
before, so unattended sends may start working after you've done a few by hand.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install playwright && .venv/bin/playwright install chromium  # for assisted reply
cp .env.example .env    # then fill it in
```

Check the office coordinates before trusting the walking-distance filter — OpenStreetMap
has no rooftop pin for 402 Dunsmuir, so the default is a street-level estimate:

```bash
.venv/bin/python scripts/check_office.py
```

Open the map link it prints. If the pin isn't on the building, right-click the
building in Google Maps, copy the coordinates, and set `OFFICE_LAT`/`OFFICE_LON`.

Try it without sending anything real by setting `DRY_RUN=1` — texts and emails go
to the log instead.

```bash
.venv/bin/python app.py     # dashboard on http://localhost:5000
```

## Receiving texts (only if NOTIFY_CHANNEL includes sms)

Twilio needs a public URL for the inbound webhook, so expose the app and point
your Twilio number's "A message comes in" webhook at `https://<your-url>/sms`:

```bash
cloudflared tunnel --url http://localhost:5000     # or: ngrok http 5000
```

Inbound requests are rejected unless they carry a valid Twilio signature, since
that endpoint is public.

## Alerts: email by default

`NOTIFY_CHANNEL=email` is the default because it needs one credential and nothing
else: a Gmail App Password. No phone number, no paid account, and crucially **no
public URL** — replies are read by connecting out over IMAP rather than having a
provider push a webhook at you, so there is no tunnel to keep running.

The loop:

1. A new listing arrives, and you get an email subjected `[#12] $2,395 1br - ...`
2. You reply to that email. Anything you type counts as "go ahead"; opening the
   reply with `no` or `skip` means do nothing.
3. Within a minute the app notices, matches `[#12]` in your `Re:` subject back to
   the listing, sends your intro, and emails you what happened.

Turn reply-watching off with `WATCH_INBOX=0` if you only want the alerts.

### Why not SMS?

`NOTIFY_CHANNEL=sms` still works but needs a **paid** Twilio account. Trial
accounts can only send from a fixed list of canned templates, so arbitrary text
like a listing title is rejected outright:

```
HTTP 400: Invalid template name. Trial accounts can only use predefined SMS templates.
```

SMS also needs a public webhook for replies, which means keeping a tunnel up. Use
`NOTIFY_CHANNEL=both` if you want texts and email together.

## Twilio credentials

You need the **Account SID** and **Auth Token** from the console home page. The
Auth Token is not optional even if you'd rather use a scoped credential: Twilio
signs inbound webhooks with it, so it is the only thing that can verify that a
text claiming to be from Twilio really is.

Optionally set `TWILIO_API_KEY_SID` / `TWILIO_API_KEY_SECRET` to a **restricted API
key** with just `Messaging > Messages > Create`. Sending then uses that key rather
than the Auth Token, so the credential doing the sending can't reconfigure or bill
your account, and you can revoke it without rotating anything else.

## Adjusting the search

The dashboard has a **Search filters** panel for distance, price range, bedrooms,
and the office coordinates. Changes are validated, saved to the database, and
picked up by the next check — no restart, and they survive one.

`.env` supplies the starting value for each; anything you change in the UI wins
from then on, and a field that differs shows what `.env` says underneath it.
**Reset to .env** clears every override.

Two things worth knowing:

- **Widening the radius or price range can produce a burst of alerts.** Listings
  filtered out before were never recorded, so they look new when they come into
  range. The 5-per-poll cap absorbs it and the rest land on the dashboard.
- **Moving the office recomputes every stored distance**, so old rows don't keep
  showing how far they were from the previous location.

## The reply message

`REPLY_MESSAGE` in `.env` is the intro sent to a listing. Two things about it:

- **`{availability}` is substituted from `AVAILABILITY`**, so you can change your
  viewing hours without rewriting the sentence around them.
- **Bracketed placeholders block sending.** The template ships with
  `[MOVE-IN DATE]`, `[YOUR PHONE]` and similar. Unattended replies refuse while any
  remain, assisted replies still open the browser but flag what needs editing, and
  the dashboard and startup log both warn. Sending `[YOUR PHONE]` to a landlord
  verbatim is worse than not replying at all.

Keep it under ~1500 characters; the Craigslist relay truncates longer replies.

## How it behaves

- **First run alerts nothing.** Every listing currently up is already old news, so
  the first poll records them silently and only later arrivals get sent.
- **At most 5 alerts per poll**, with a summary message if more matched. Without
  this a widened filter would dump a hundred messages at once.
- **Room shares are filtered out.** Craigslist counts a shared room as "1br", so
  titles matching `EXCLUDE_KEYWORDS` are dropped.
- **Distance is set in walking minutes**, not metres, because that's the number
  you actually care about. The radius is derived: Craigslist is queried by
  straight-line distance, and a real pavement route is roughly 25% longer than
  that in downtown Vancouver's grid, so 10 minutes becomes a ~667 m radius.
  Change `WALK_MINUTES` in `.env` or use the dashboard. An older `.env` setting
  `RADIUS_M` in metres is still honoured, and a radius saved by a previous version
  is migrated to its equivalent in minutes on first run.

## Layout

| File | Role |
|---|---|
| `craigslist.py` | JSON search endpoint, response decoding, distance filter |
| `mailer.py` | Alert emails to you, and the SMTP send used for intros |
| `inbox.py` | Reads your replies over IMAP; the email answer to a webhook |
| `notifier.py` | Twilio outbound texts, if SMS is enabled |
| `replier.py` | Assisted and unattended reply to a listing |
| `app.py` | Flask routes, background poller, inbox watcher |
| `settings.py` | Dashboard-adjustable filters, validated and persisted |
| `db.py` | SQLite storage and dedupe |

## Fair warning

Scraping is against Craigslist's terms of service, and this exists for personal
apartment hunting. Keep `POLL_MINUTES` at 10 or higher, don't run it from a
datacenter IP, and don't point it at anyone else's behalf.
