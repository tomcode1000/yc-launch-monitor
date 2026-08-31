# YC Launch Monitor

A Slack bot that alerts you to new Y Combinator and SPEEDRUN companies — including the ones whose founders have announced on social media **before the official directory lists them**.

Built as a Pond Protocol agent, so it runs autonomously *and* answers on demand.

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

**Two secrets and one setting get you running:** `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, and `SLACK_CHANNEL` (which is just a destination, not a secret). The YC and SPEEDRUN sources need no paid accounts, so you get real alerts on the first run.

Want zero API cost? Set `LLM_MODE=off` and skip the Anthropic key entirely — see [Running without an LLM](#running-without-an-llm).

After creating the Slack app, remember to invite the bot to the channel (`/invite @YC Monitor`), or `chat:write` fails with `not_in_channel`.

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
| `ANTHROPIC_API_KEY` | unless `LLM_MODE=off` | Powers the agent's judgment |
| `SLACK_BOT_TOKEN` | yes | Bot token with `chat:write` |
| `SLACK_CHANNEL` | yes | Channel name, channel ID, or a user ID for a DM |
| `POND_ACCESS_KEY` | on publish | Authenticates Pond's calls |
| `LLM_MODE` | optional | `auto` (default), `off`, or `always` — see below |
| `APIFY_TOKEN` | optional | Enables LinkedIn and the X keyword tier |
| `X_KEYWORD_TIER` | optional | `off` (default) or `apify` |
| `DAILY_SPEND_CAP_USD` | optional | Halts metered sources when hit (default `2.00`) |

---

## Running without an LLM

The model is not on the critical path for most of what this bot does, and `LLM_MODE` controls how much it is used.

| Mode | Behaviour | Cost |
|---|---|---|
| `auto` *(default)* | Rules handle everything they can. The model is called **only** when a social signal survives the free prefilter — typically a few times a day, not every cycle. | Low |
| `off` | No model, no Anthropic key needed. Pure rules. | Zero |
| `always` | Model runs every cycle. | High |

**Why `auto` is the default.** A directory poll runs every 60 seconds, but a company appearing in YC's own API needs no interpretation — so that path never calls a model. Without this gate, a 60s poll would fire a full tool-calling loop every minute.

**What you lose at `off`.** Rules are good at *rejecting* and weak at *extracting*. Deciding that "our YC interview is next week" is not an acceptance is pattern matching, and `classify_rules.py` handles it. But naming the company is not:

| Post | Rules result |
|---|---|
| `We got into YC F26! building https://acme.ai` | ✅ Alerts — name from the linked domain |
| `Our YC interview is next week` | ✅ Correctly dropped, for free |
| `We got into YC F26!! so hyped` | ⚠️ **Skipped** — no recoverable company name |

That third row is the cost of `off`: roughly a third to a half of early detections are lost. The rules engine **declines rather than guessing**, deliberately — a wrong name here becomes a cold email to a company that never got in.

Everything else — the confirmed-alert path, dedupe, verification, Slack, Pond, scheduling — is fully deterministic and identical in every mode.

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
