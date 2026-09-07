# Rental watcher

Polls Craigslist Vancouver for new studio/1-bedroom rentals within walking
distance of Amazon YVR14 (402 Dunsmuir St) and shows them on a dashboard, marked
new until you've looked at them. Hit **Send** on a row to reply to the poster with
your pre-set intro message.

**One email per check, not one per listing.** It used to send a message for every
match plus an overflow summary, so a busy evening meant six emails that each said
less than the dashboard already showed. Now a check that finds anything sends a
single truncated digest — the first few places, then a count of the rest — and the
dashboard is where you actually look.

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

`DRY_RUN=1` writes intro messages to the log instead of contacting anyone, so it only
affects **Send**. Listings are still found and shown either way, and the digest still
reaches your own inbox — the point is not to message a stranger by accident, and
mailing yourself isn't that.

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

## How you find out about new places

**The dashboard is the place you look.** New listings carry a **new** badge and a
count in the header until the page has shown them to you, then the badge clears
itself. "New" means "you haven't had a chance to look at this", not "arrived
recently" — so a place found overnight is still marked new in the morning.

Only rows actually on the page count as looked at. Anything past the 100-row cut, or
hidden by the **Matching** toggle, stays new.

**The digest email is the nudge.** A check that finds something sends exactly one
message, whether it found one place or thirty:

```
Subject: 3 new rentals near the office

#331  $2,150 · 1br · 6 min walk
    Renovated 1BR w/ balcony steps from Waterfront
    555 W HASTINGS ST, built 1998
    https://vancouver.craigslist.org/van/apa/d/vancouver-1br/7891.html
...
```

- **It's truncated at `DIGEST_MAX` (8).** Past a handful you're going to open the
  dashboard regardless, so the rest are counted, not listed. A widened radius that
  turns up forty places is still one email.
- **A single find puts its price, size and walk in the subject line**, so you can
  triage it without opening anything. Several can't be summarised that way, so the
  subject counts them rather than picking one arbitrarily.
- **It also says how many listings are still unopened**, which is the part that
  catches an evening you ignored.
- **Nothing is sent when nothing was found.** Silence means silence, not a broken
  poller — the header dot on the dashboard is what tells you it's alive.
- **It's sent even when `DRY_RUN=1`**, because dry run exists so you don't contact a
  stranger by accident, and this only goes to your own address. Set `EMAIL_DIGEST=0`
  to stop it entirely and rely on the dashboard.
- **Addresses and years appear for the newest few**, since enrichment runs before the
  digest is built and is capped per poll (`ENRICH_PER_POLL`).

Set `DASHBOARD_URL` if you reach the app at something other than
`http://localhost:5000`; it's the link at the foot of every digest.

### Replying to the digest does nothing

The old path let you reply to a per-listing alert to fire off your intro message —
matched back to the listing by the `[#12]` in the subject. A digest covers several
listings, so there's no single poster to forward your words to, and the email says
so rather than swallowing them. **Send** on the dashboard row is the reply path.

`WATCH_INBOX` is therefore `0` by default. Left on, it produced a captcha-blocked
send plus a report about it for every message you sent that mailbox.

### Why not SMS?

Not worth it: a **paid** Twilio account is required, because trial accounts can only
send from a fixed list of canned templates, so arbitrary text like a listing title is
rejected outright:

```
HTTP 400: Invalid template name. Trial accounts can only use predefined SMS templates.
```

The `/sms` webhook survives so you can text a listing number to reply to it, which
needs a public URL and therefore a tunnel. `NOTIFY_CHANNEL` doesn't govern the
digest — `EMAIL_DIGEST` does.

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

- **Widening the radius or price range can produce a burst of new rows.** Listings
  filtered out before were never recorded, so they look new when they come into
  range. Harmless now that a burst is a longer page rather than a stack of emails.
- **Moving the office recomputes every stored distance**, so old rows don't keep
  showing how far they were from the previous location.

## How old is the building?

Craigslist has no year-built field, so this uses two sources in order:

1. **The posting text**, when a landlord happens to write "built in 1998".
2. **City of Vancouver open data** — the `property-tax-report` dataset carries
   `year_built` for every assessed property, looked up by the street address
   Craigslist exposes in its `mapaddress` element.

Expect this to resolve for **roughly half** of listings. The limit is Craigslist,
not the lookup: many posts give a cross-street ("Beatty near Dunsmuir") or no
address at all, and without a civic number there is nothing to query. Rows the
poll didn't cover get a **look up** link on the dashboard, which fills that one cell
in place rather than reloading — a re-render would scroll you away from the row you
were reading. It degrades to a normal form post if JavaScript is off.

What it deliberately does *not* do is reverse-geocode the listing's coordinates to
guess an address. Craigslist rounds coordinates to anonymise them, so that would
confidently report the wrong building's age — worse than admitting it's unknown.

Set `LOOKUP_YEAR_BUILT=0` to skip it. It costs one page fetch plus an open-data
query per listing, so a poll only enriches the newest few (`ENRICH_PER_POLL`, 5) and
leaves the rest to the **look up** link. Results are cached per address.

### Dataset quirks

Worth knowing if this ever breaks, since none of it matches what the field names
suggest:

- `to_civic_number` is the street number. `from_civic_number` is the *unit* for
  strata properties, or null — the pair is not a range.
- Street names put the direction last: `GEORGIA ST W`, not `W GEORGIA ST`.
- A strata building returns one row per unit, so the most common year wins.

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

- **First run flags nothing as new.** Every listing currently up is already old news,
  so the first poll records them silently and only later arrivals are marked.
- **No cap on how many a poll can find.** There was one, back when each match meant
  its own email. A hundred new rows is a long page and one truncated digest.
- **Room shares are filtered out.** Craigslist counts a shared room as "1br", so
  titles matching `EXCLUDE_KEYWORDS` are dropped.
- **Distance is set in walking minutes**, not metres, because that's the number
  you actually care about. The radius is derived: Craigslist is queried by
  straight-line distance, and a real pavement route is roughly 25% longer than
  that in downtown Vancouver's grid, so 10 minutes becomes a ~667 m radius.
  Change `WALK_MINUTES` in `.env` or use the dashboard. An older `.env` setting
  `RADIUS_M` in metres is still honoured, and a radius saved by a previous version
  is migrated to its equivalent in minutes on first run.
- **One crawl serves every saved search.** `crawl()` asks Craigslist for the newest
  postings in the covered area and carries no personal filters; `match()` then decides
  which of them suit a given search, from stored rows. So a second saved search costs
  no extra requests, and the filter rules can be tested without a network.
- **A crawl sees at most 360 postings** and there is no way to page past them —
  measured, not assumed. Every radius saturates that limit, so `CRAWL_RADIUS_KM`
  (default 2) is a real trade: narrower means the same 360 slots cover a smaller area
  and reach further back in time. Keep it just wide enough to contain your search. The
  poll warns if your walking radius reaches past it, and if a crawl ever comes back
  both full and entirely unfamiliar — which would mean listings slipped through the
  gap between polls.

## Tests

```
pytest
```

No network and no database of your own: `tests/conftest.py` points `DB_PATH` at a
temporary file before `db.py` can connect, and the decoder tests run against real API
responses captured into the test file. Covers the filter rules, the response decoding,
the filter-storage migration, and the digest's truncation and quiet-when-empty
behaviour — nothing in the suite can send mail, since `smtp_send` is stubbed.

## Layout

| File | Role |
|---|---|
| `craigslist.py` | The crawl, response decoding, and the filter rules |
| `mailer.py` | The digest email, and SMTP sending |
| `inbox.py` | Reads your replies over IMAP; off unless `WATCH_INBOX=1` |
| `notifier.py` | Twilio outbound texts, if SMS is enabled |
| `replier.py` | Assisted and unattended reply to a listing |
| `app.py` | Flask routes, background poller, inbox watcher |
| `settings.py` | Dashboard-adjustable filters, validated and persisted |
| `enrich.py` | Year built, from the posting or city open data |
| `db.py` | SQLite storage, dedupe, and the users/searches/alerts tables |
| `tests/` | Filter rules, response decoding, the settings migration, the digest |

## Fair warning

Scraping is against Craigslist's terms of service, and this exists for personal
apartment hunting. Keep `POLL_MINUTES` at 10 or higher, don't run it from a
datacenter IP, and don't point it at anyone else's behalf.
