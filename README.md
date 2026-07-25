# WhatsApp + Email Task Agent

A self-hosted agent that captures every client message from **one WhatsApp
Business number (Cloud API)** and **multiple Gmail inboxes** (e.g. 4), uses
the **Claude API** to extract actionable tasks on an **hourly scan**, and
serves a clean **web dashboard** of today's task list. No human intervention
required once deployed.

```
Client WhatsApp msg ──► Meta Cloud API ──push──► POST /webhook ──► SQLite
Client email (x4 inboxes) ──► Gmail API ◄──pull (every scan)
                                     │
                     every hour:     ▼
                     Claude API extracts & merges tasks ──► tasks table
                                     │
                     you open:       ▼
                     GET /?token=...  →  "Today's Tasks" dashboard
```

## Project layout

```
app/
  main.py         FastAPI app: webhook routes, dashboard, scheduler
  config.py       All settings (from .env)
  db.py           SQLite schema + queries
  whatsapp.py     Meta webhook verification, parsing, media download
  gmail_client.py Gmail API pull (read-only)
  extractor.py    Claude API prompt + daily digest job
  templates/dashboard.html
scripts/
  gmail_auth.py   One-time OAuth flow (run on your laptop)
data/             SQLite DB, media files, gmail token (created at runtime)
```

---

## Setup

### 1. Meta / WhatsApp Cloud API

1. Create a **Meta Business Portfolio** at business.facebook.com (free).
2. At **developers.facebook.com** create an app → type *Business* → add the
   **WhatsApp** product. You get a free **test number** immediately — use it
   for the whole setup before touching your real number.
3. **Permanent token:** Business Settings → Users → System Users → create one
   (admin), assign your app, generate a token with
   `whatsapp_business_messaging` + `whatsapp_business_management` scopes.
   Put it in `.env` as `WHATSAPP_ACCESS_TOKEN`.
4. **App secret:** App dashboard → Settings → Basic → App Secret →
   `WHATSAPP_APP_SECRET`.
5. Invent a random string for `WHATSAPP_VERIFY_TOKEN`.
6. Deploy this app first (step 4 below), then in the Meta app dashboard:
   WhatsApp → Configuration → **Webhook** → set
   - Callback URL: `https://YOUR-DOMAIN/webhook`
   - Verify token: the same string as `WHATSAPP_VERIFY_TOKEN`
   and click *Verify and save*. Then **subscribe to the `messages` field**.
7. Send a WhatsApp message to the test number → it should appear in the
   database (check the dashboard after "Scan now").
8. When ready for production: register your real business number under
   WhatsApp → API Setup. (The number must not be active in the regular
   WhatsApp/WhatsApp Business app.)

### 2. Gmail (read-only, works for any number of accounts)

1. console.cloud.google.com → new project → enable **Gmail API**.
2. OAuth consent screen → External → add **all 4 addresses** as test users.
3. Credentials → Create credentials → **OAuth client ID** → *Desktop app* →
   download JSON as `scripts/credentials.json` (one client serves all accounts).
4. On your own computer, run the auth script **once per inbox**, signing into
   the matching account in the browser each time:
   ```bash
   pip install google-auth-oauthlib google-api-python-client
   python scripts/gmail_auth.py sales
   python scripts/gmail_auth.py support
   python scripts/gmail_auth.py accounts
   python scripts/gmail_auth.py personal
   ```
   This writes `data/gmail_<name>.json` for each — copy them to the server's
   `data/` directory and list them in `.env`:
   ```
   GMAIL_TOKEN_FILES=./data/gmail_sales.json,./data/gmail_support.json,./data/gmail_accounts.json,./data/gmail_personal.json
   ```
5. Optionally tune `GMAIL_QUERY` in `.env` (default: inbox mail from the last
   day, excluding promotions/social). Already-seen mail is deduplicated by
   Gmail message ID, so overlapping scans never create duplicates.

### 3. Anthropic

Create an API key at console.anthropic.com → `.env` `ANTHROPIC_API_KEY`.
Even scanning hourly, cost stays low at typical small-business volume:
scans with no new messages skip the Claude call entirely, and each active
scan is one small request — usually a few dollars per month.

### 4. Deploy

Any host that gives you a public HTTPS URL works. Two easy options:

**Railway / Render (simplest):** connect the repo, they detect the
Dockerfile, set the environment variables from `.env.example`, attach a
persistent volume at `/app/data`, deploy. You get an HTTPS URL immediately.

**VPS (Hetzner/DigitalOcean, ~$5/mo):**
```bash
docker build -t task-agent .
docker run -d --name task-agent --env-file .env \
  -v $(pwd)/data:/app/data -p 8000:8000 task-agent
```
Put Caddy or nginx in front for HTTPS (Meta requires HTTPS for webhooks).

**Local development:**
```bash
pip install -r requirements.txt
cp .env.example .env   # fill it in
uvicorn app.main:app --reload
# expose to Meta during testing with: ngrok http 8000
```

### 5. Use it

- Dashboard: `https://YOUR-DOMAIN/?token=YOUR_DASHBOARD_TOKEN` — bookmark it.
- The agent scans WhatsApp + all 4 inboxes every `SCAN_INTERVAL_MINUTES`
  (hourly by default) and updates the task list. "Scan now" runs it on
  demand. When a scan finds no new messages, no Claude API call is made, so
  quiet hours cost nothing.
- Mark tasks **Done** on the dashboard; unfinished tasks carry over
  automatically. Claude also closes tasks it can see are resolved from new
  messages, and merges duplicate WhatsApp+email requests.

---

## Notes & limits

- **Inbound WhatsApp is free**; this app never sends messages, so Meta
  message fees don't apply.
- Group chats are not delivered by the Cloud API — only 1:1 client chats.
- Media (images/PDFs/voice notes) is downloaded to `data/media/`; captions and
  filenames are used for task extraction, but audio is not transcribed
  (an easy future upgrade: pipe audio files through a speech-to-text API and
  feed the transcript to the extractor).
- The dashboard is protected by a single token in the URL. For a team, put
  proper auth (e.g. Cloudflare Access or basic auth in nginx) in front.
- Webhook deliveries are deduplicated by Meta message ID, so Meta's retries
  never create duplicate rows.

## Troubleshooting

- *Webhook verification fails:* `WHATSAPP_VERIFY_TOKEN` in `.env` must match
  the "Verify token" field in the Meta dashboard exactly; the app must be
  publicly reachable over HTTPS at the callback URL.
- *403 "bad signature" on POSTs:* `WHATSAPP_APP_SECRET` doesn't match the app
  secret in Meta Settings → Basic.
- *"Gmail token not found":* run `scripts/gmail_auth.py` locally and copy
  `data/gmail_token.json` to the server.
- *No tasks extracted:* check container logs — the extractor logs message and
  task counts on every run.
