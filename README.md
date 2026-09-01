# YC Launch Monitor

**Live:** https://yc-launch-monitor-production.up.railway.app

A Slack bot that alerts you to new Y Combinator and SPEEDRUN companies — including the ones whose founders have announced on social media **before the official directory lists them**.

Built as a Pond Protocol agent, so it runs autonomously *and* answers on demand.

**Runs with no LLM and no AI API key.** Classification is a deterministic rules engine; model judgment is an optional add-on.

---

## What it does

```
YC Directory ─┐
SPEEDRUN ─────┼─→ collect → prefilter → classify → dedupe → VERIFY → Slack
X ────────────┤                                             │
LinkedIn ─────┘                              is it in YC's API yet?
                                              ├─ no  → ⚡ EARLY SIGNAL (the scoop)
                                              └─ yes → ✅ CONFIRMED BY YC
```

The `Status` line on every alert is a verified fact, not a guess: before sending, the bot queries YC's own API to confirm the company is genuinely not yet listed.

---

## Quick start

```bash
git clone <your-repo-url> && cd yc-launch-monitor
pip install -r requirements.txt
cp .env.example .env          # fill in your keys
uvicorn src.server:app --host 0.0.0.0 --port 8000
```

Then trigger a scan by hand:

```bash
curl -X POST localhost:8000/admin/scan
curl localhost:8000/health
```

**One secret and one setting get you running:** `SLACK_BOT_TOKEN`, and `SLACK_CHANNEL` (a destination, not a secret). No AI API key, no paid accounts — the YC and SPEEDRUN sources are free, so you get real alerts on the first run.

### Getting the Slack token

Slack calls it a **token**, not an API key — there is no separate key to generate.

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**
2. Pick your workspace, paste [`slack-app-manifest.yml`](slack-app-manifest.yml), and create
3. **Install to Workspace** → **Allow**
4. **OAuth & Permissions** → copy the **Bot User OAuth Token** (starts `xoxb-`)

That token is `SLACK_BOT_TOKEN`. The manifest requests `chat:write.public`, so the bot can post to any public channel without being invited. If you remove that scope, or you are posting to a **private** channel, run `/invite @YC Monitor` in the target channel first — otherwise delivery fails with `not_in_channel`.

For `SLACK_CHANNEL` use `#yc-alerts`, a channel ID (`C08AB12CD`, from **Copy link** on the channel), or your own member ID (`U…`, from your profile menu → **Copy member ID**) to receive alerts as a DM.

### Deploying

Any host that gives you a public HTTPS URL. A `Dockerfile` is included.

```bash
docker build -t yc-monitor . && docker run -p 8000:8000 --env-file .env yc-monitor
```

Render / Railway / Fly free tier all work. You need a public URL because Pond calls your server (see below).

### Publishing on Pond

1. Deploy and note your base URL.
2. Go to <https://joinpond.ai/agent/create> and paste it. Pond reads `GET /manifest` and prefills the listing.
3. Pond issues an Access Key. Set it as `POND_ACCESS_KEY` and redeploy.

Verify the protocol side without leaving your terminal:

```bash
curl https://your-app.onrender.com/manifest

curl -X POST https://your-app.onrender.com/runs \
  -H "Authorization: Bearer $POND_ACCESS_KEY" \
  -H "X-Agent-Protocol-Version: 1.0" \
  -H "Idempotency-Key: run_demo_1" \
  -H "Content-Type: application/json" \
  -d '{"run_id":"run_demo_1","action_id":"query_state","parameters":{"prompt":"what have you found?"}}'
```

---

## Configuration

Behaviour lives in `config.yml` (no code changes needed); secrets live in the environment.

| Variable | Required | Purpose |
|---|---|---|
| `SLACK_BOT_TOKEN` | yes | Bot token with `chat:write` |
| `SLACK_CHANNEL` | yes | Channel name, channel ID, or a user ID for a DM |
| `POND_ACCESS_KEY` | on publish | Authenticates Pond's calls |
| `LLM_MODE` | optional | `off` (default), `auto`, or `always` — see below |
| `ANTHROPIC_API_KEY` | only if `LLM_MODE` is not `off` | Optional model judgment |
| `APIFY_TOKEN` | optional | Enables LinkedIn and the X keyword tier |
| `X_KEYWORD_TIER` | optional | `off` (default) or `apify` |
| `DAILY_SPEND_CAP_USD` | optional | Halts metered sources when hit (default `2.00`) |

---

## How classification works (no LLM required)

This bot ships **rules-only by default**. There is no AI API key to obtain, no per-cycle cost, and the `anthropic` package is not even installed by `requirements.txt`. Everything runs on `src/classify_rules.py`.

### What the rules do well

Rejecting is the hard-working half, and patterns handle it cleanly — these are the dominant false positives:

| Post | Verdict |
|---|---|
| `We got into YC F26! building https://acme.ai` | ✅ `early` — name from the linked domain, checked against YC's API |
| `We got into YC F26!! so hyped` | ✅ `early_unverified` — still a lead, keyed on the founder's handle |
| `Our YC interview is next week` | ❌ Dropped — interview, not acceptance |
| `Just applied to YC W27` | ❌ Dropped — application |
| `We got rejected from YC` | ❌ Dropped — rejection post-mortem |
| `Back in 2019 when we did YC` | ❌ Dropped — alumni |
| `Congrats to @foo on getting into YC!` | ❌ Dropped — third-party, not the founder |

### Why a missing company name doesn't cost you a lead

You act on these alerts by **messaging the founder through their post link**. The company name is enrichment, not the lead. So a post that names no company still produces a complete, actionable alert keyed on the author's handle — it just carries `early_unverified` status, because with no company name there is nothing to look up in YC's directory. The card says exactly that rather than claiming a check that never happened.

### Optional: adding model judgment

If you later want an LLM to handle the ambiguous middle — unusual phrasing, company names buried in prose — it is two steps and no code change:

```bash
pip install -r requirements-llm.txt
# then set in .env:
#   LLM_MODE=auto
#   ANTHROPIC_API_KEY=sk-ant-...
```

`auto` keeps the rules engine in front: the model is called **only** on social signals that survive the free prefilter, never on directory arrivals, which need no interpretation. If the package or key is missing the bot logs a warning and falls back to rules-only rather than failing.

---

## Cadence: why it is per source, not global

The four sources have very different cost and fragility profiles, so each has its own interval in `config.yml`:

| Source | Interval | Cost | Why |
|---|---|---|---|
| YC Directory | 60s | free | Public JSON API, no meaningful limit |
| SPEEDRUN | 5 min | free | Static page, changes rarely |
| X (watchlist) | 3 min | free | **Rate-limited by IP** — see below |
| X (keyword) | 30 min | free/paid | The fragile tier, off by default |
| LinkedIn | 6 h | **~$22/mo** | Metered per item — the hard ceiling |

**On "check every minute":** the sources that actually produce the scoop — the YC directory and the X watchlist — run at minute-level freshness. LinkedIn cannot. At $0.005/item a 50-post search costs ~$0.25 per run; every 6 hours is about **$22/month**, but every minute would be **~$360/day**. That is a bill constraint, not an effort one. The `DAILY_SPEND_CAP_USD` governor puts metered sources to sleep when tripped and reports it in the next Slack digest; the free sources keep running.

---

## Things you should know

### "YC Speedrun" is not a YC program

The task brief lists a *"YC Speedrun page — YC's dedicated Speedrun program directory"*. There is no such YC program. All 50 batches in YC's own dataset, plus every tag and industry label, contain **zero** occurrences of "speedrun" (checked against `api.ycombinator.com`, not assumed). SPEEDRUN is an **a16z** accelerator, and its portfolio is at `speedrun.a16z.com`.

The source is still implemented and still named `yc_speedrun` — it is the required fourth source — but it points at the directory where the companies actually are, and also catches SPEEDRUN mentions from X and LinkedIn.

### YC runs four batches a year, with unexpected codes

Winter, Spring, Summer, Fall → `W`, **`P`**, `S`, `F`. Spring 2026 is `P26`, not `X26`. Matching only `[WS]` silently misses half the year. Also, the two YC data sources disagree on format (`"S26"` vs `"Summer 2026"`), so everything is normalized on ingest.

### X's free endpoint rate-limits

`syndication.twitter.com` returns structured JSON with no key and no account — but it **429s by IP** after a handful of rapid requests (measured, not assumed). The adapter therefore polls a slice of the watchlist round-robin, spaces requests with jitter, and backs off globally for 15 minutes on a 429. Every account still gets covered, just spread across ticks.

### LinkedIn is against LinkedIn's terms

LinkedIn forbids automated collection and actively blocks it. This uses a maintained third-party Apify actor, which is pragmatic rather than durable. Both configured actors are "no cookies" builds — the alternatives want a session cookie from a real account, which is the setup most likely to get *your* profile restricted. If the actor breaks, change the ID in `config.yml`; no code change needed.

### First boot does not flood your channel

A cold start would otherwise treat all ~420 companies already in the configured batches as new discoveries. On an empty database the bot seeds them silently and starts alerting from the next arrival — which is what "only push incremental updates" means.

---

## Architecture

```
src/
├── server.py       FastAPI: /manifest, /runs, /tasks, /health, /admin/scan
├── manifest.py     Pond Protocol v1.0 manifest
├── scheduler.py    per-source intervals + cost governor
├── agent.py        tool runner, cycle prompt, guardrails
├── slack.py        Block Kit alert cards
├── store.py        SQLite: companies, signals, pond_runs, spend, health
├── models.py       RawSignal, batch/company normalization, prefilter
└── sources/
    ├── base.py         the Source ABC — add a platform in one file
    ├── yc_directory.py public JSON API + the verification oracle
    ├── yc_speedrun.py  a16z portfolio
    ├── x_source.py     3 tiers: watchlist, reverse-discovery, keyword
    ├── linkedin.py     Apify actor + fallback
    └── apify.py        thin client, actor IDs from config
```

### Where the agent's judgment ends and code begins

Claude decides what a post *means* — acceptance vs. interview announcement vs. rejection post-mortem, whether two posts describe the same company, whether evidence is strong enough. Those are genuine judgment calls.

Deduplication, state, and delivery are not. `record_alert` is an **atomic check-and-set**, and `post_slack_alert` refuses to fire without a reservation. If the model tries to double-post, it structurally cannot. That is deliberate: if the model *can* send a duplicate, eventually it will.

### Adding a platform

One subclass of `Source` implementing `fetch(since) -> list[RawSignal]`, plus an entry in `config.yml`. Nothing in the scheduler, agent, or alerting path needs to change.

---

## Tests

```bash
python -m tests.test_core
```

Covers batch-code normalization across all four cycles, company identity resolution, the prefilter, structural dedupe, lead-time tracking, spend accounting, and Pond idempotency.

---

## Measuring whether it works

Every early alert is stored with its detection time. When the company later appears in YC's directory, the gap is recorded as **lead time** — query it any time via the `query_state` action or `/health`. That is the number that matters: *"median N hours ahead of YC's own listing."*
