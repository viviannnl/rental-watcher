# Rental watcher

Polls Craigslist Vancouver for new studio/1-bedroom rentals within walking
distance of Amazon YVR14 (402 Dunsmuir St) and texts you each one with a link.
Text the listing number back and it opens a reply with your pre-set message.

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

So "reply to my text and the app posts my message" isn't achievable without
defeating a captcha, which this project won't do. What it does instead is
**assisted reply**: texting back opens a browser window on the machine running
the app, already on the reply form with your message typed in, so you solve the
captcha and hit send. It also retries unattended each time against a persistent
browser profile — hCaptcha often stops challenging a profile that has passed
before, so unattended sends may start working after you've done a few by hand.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install playwright && .venv/bin/playwright install chromium  # for assisted reply
cp .env.example .env    # then fill it in
```

Check the office coordinates before trusting the distance filter — OpenStreetMap
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

## Receiving texts

Twilio needs a public URL for the inbound webhook, so expose the app and point
your Twilio number's "A message comes in" webhook at `https://<your-url>/sms`:

```bash
cloudflared tunnel --url http://localhost:5000     # or: ngrok http 5000
```

Inbound requests are rejected unless they carry a valid Twilio signature, since
that endpoint is public.

## How it behaves

- **First run alerts nothing.** Every listing currently up is already old news, so
  the first poll records them silently and only later arrivals get texted.
- **At most 5 texts per poll**, with a summary text if more matched. Without this
  a widened filter would dump a hundred messages at once.
- **Room shares are filtered out.** Craigslist counts a shared room as "1br", so
  titles matching `EXCLUDE_KEYWORDS` are dropped.
- **Distance is straight-line.** The default 700 m is tuned so it corresponds to
  roughly a 10-minute walk once you account for routing around blocks; a literal
  10 min of walking is ~830 m of pavement but only ~700 m of crow-flies in
  downtown Vancouver's grid. Set `RADIUS_M=830` if you'd rather cast wider.

## Layout

| File | Role |
|---|---|
| `craigslist.py` | JSON search endpoint, response decoding, distance filter |
| `notifier.py` | Twilio outbound texts and alert formatting |
| `replier.py` | Assisted and unattended reply, SMTP send |
| `app.py` | Flask routes, background poller, inbound SMS handling |
| `db.py` | SQLite storage and dedupe |

## Fair warning

Scraping is against Craigslist's terms of service, and this exists for personal
apartment hunting. Keep `POLL_MINUTES` at 10 or higher, don't run it from a
datacenter IP, and don't point it at anyone else's behalf.
