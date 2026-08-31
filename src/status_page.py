"""The one human-facing surface on an otherwise machine-facing service.

Kept out of server.py so the routing module stays about routing. The page
reads the same store the /health endpoint reads, so the two cannot disagree
about whether a source is healthy.

The look is a single committed choice rather than a themed one: this is a
monitoring daemon, so the page reads as the terminal you would watch it in.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

SOURCES = (
    ("yc_directory", "verification oracle"),
    ("yc_speedrun", "a16z portfolio watch"),
    ("x", "timelines + keyword sweep"),
    ("linkedin", "post search"),
)

# Trailing backslashes are load-bearing here, so the closing quotes go on
# their own line - otherwise the last one escapes the delimiter.
BANNER = r"""  _   _____   __  __  ___  _  _ ___ _____ ___  ___
 | | / / __| |  \/  |/ _ \| \| |_ _|_   _/ _ \| _ \
  \ V / (__  | |\/| | (_) | .` || |  | || (_) |   /
   |_| \___| |_|  |_|\___/|_|\_|___| |_| \___/|_|_\
"""

CSS = """
  :root {
    --bg:#0b0f0d; --chrome:#151b18; --panel:#0f1512; --line:#20291f;
    --ink:#d6e0d5; --dim:#6f7d6d; --green:#5ee08a; --amber:#e8b44a;
    --red:#e06a5e; --cyan:#63c8d6; --mag:#c98bdc;
  }
  * { box-sizing:border-box }
  body {
    margin:0; background:var(--bg); color:var(--ink);
    font:14px/1.7 ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,
         Consolas,monospace;
    padding:2.5rem 1.25rem 4rem;
  }
  .term {
    max-width:60rem; margin:0 auto; background:var(--panel);
    border:1px solid var(--line); border-radius:9px; overflow:hidden;
    box-shadow:0 18px 50px rgba(0,0,0,.5);
  }
  .bar {
    display:flex; align-items:center; gap:.5rem; padding:.6rem .9rem;
    background:var(--chrome); border-bottom:1px solid var(--line);
  }
  .dot { width:11px; height:11px; border-radius:50%; flex:none }
  .d1{background:#e06a5e} .d2{background:#e8b44a} .d3{background:#5ee08a}
  .bar .t { margin-left:.6rem; color:var(--dim); font-size:.8rem }
  .body { padding:1.4rem 1.35rem 1.75rem; overflow-x:auto }
  .cmd { margin:1.6rem 0 .55rem; white-space:nowrap }
  .p { color:var(--green) } .p2 { color:var(--cyan) }
  pre { margin:0; font:inherit; color:var(--ink); white-space:pre }
  .dim { color:var(--dim) }
  .g { color:var(--green) } .a { color:var(--amber) }
  .r { color:var(--red) } .m { color:var(--mag) }
  .banner { color:var(--green); line-height:1.35; margin:0 0 .4rem;
            font-size:clamp(.5rem,1.85vw,.82rem) }
  a { color:var(--cyan) }
  a:hover { color:var(--green) }
  .cur { display:inline-block; width:.6em; height:1.05em;
         background:var(--green); vertical-align:-.18em;
         animation:b 1.1s steps(1) infinite }
  @keyframes b { 50% { opacity:0 } }
  @media (prefers-reduced-motion:reduce) { .cur { animation:none } }
"""


def _ago(iso: str | None) -> str:
    """Human 'last run' text. The raw timestamp stays on /health."""
    if not iso:
        return "not yet run"
    try:
        then = datetime.fromisoformat(iso)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - then).total_seconds()
    except Exception:  # noqa: BLE001 - a bad timestamp must not 500 the page
        return "last run unknown"
    if secs < 90:
        return "last run just now"
    if secs < 5400:
        return f"last run {secs / 60:.0f} min ago"
    if secs < 172800:
        return f"last run {secs / 3600:.0f} h ago"
    return f"last run {secs / 86400:.0f} d ago"


def _median(values: list[float]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def render(store, notifier, config, repo_url: str) -> str:
    """Build the status page from live state."""
    by_name = {h["source"]: h for h in store.health()}

    rows = []
    for name, role in SOURCES:
        h = by_name.get(name)
        if h is None:
            state, cls, when = "IDLE", "dim", "not yet run"
        elif h["healthy"]:
            state, cls, when = "HEALTHY", "g", _ago(h["last_run_at"])
        else:
            state, cls, when = "DEGRADED", "a", _ago(h["last_run_at"])
        rows.append(
            f'{name:<14}<span class="{cls}">[{state:^8}]</span>  '
            f'<span class="dim">{when:<21}{role}</span>'
        )
    sources = "\n".join(rows)

    leads = store.lead_times_hours()
    median = ""
    if leads:
        median = (f'\nmedian_lead       <span class="g">{_median(leads):.0f}h</span>'
                  f' <span class="dim">ahead of official listing</span>')

    alerted = len(store.recent_alerts(500))
    slack_cls, slack_txt = (
        ("g", "configured") if notifier.configured else ("r", "NOT CONFIGURED"))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YC Launch Monitor</title>
<style>{CSS}</style></head><body>
<div class="term">
  <div class="bar">
    <span class="dot d1"></span><span class="dot d2"></span>
    <span class="dot d3"></span>
    <span class="t">yc-launch-monitor &mdash; monitoring daemon &mdash; live</span>
  </div>
  <div class="body">

<pre class="banner">{escape(BANNER)}</pre>
<pre class="dim">early detection for Y Combinator + a16z SPEEDRUN &middot; Pond Protocol agent</pre>

<div class="cmd"><span class="p">$</span> monitor --describe</div>
<pre>Watches X and LinkedIn for founders announcing an acceptance, then checks
the <span class="m">official directory</span> before every alert &mdash; so <span class="g">"not yet announced"</span>
is a <span class="g">verified fact</span>, not a guess.  <span class="dim">Rules engine only; no LLM required.</span></pre>

<div class="cmd"><span class="p">$</span> monitor sources --status</div>
<pre>{sources}</pre>

<div class="cmd"><span class="p">$</span> monitor stats</div>
<pre>leads_alerted     <span class="g">{alerted}</span>{median}
spend_today       <span class="a">${store.spend_today():.2f}</span> <span class="dim">/ cap ${config.daily_spend_cap:.2f}</span>
slack_delivery    <span class="{slack_cls}">{slack_txt}</span>
llm_mode          <span class="dim">off (deterministic)</span></pre>

<div class="cmd"><span class="p">$</span> monitor endpoints</div>
<pre><a href="/health">GET  /health</a>      <span class="dim">source health, spend</span>
<a href="/manifest">GET  /manifest</a>    <span class="dim">Pond Protocol v1.0 descriptor</span>
POST /runs        <span class="dim">Pond runtime &mdash; authenticated</span></pre>

<div class="cmd"><span class="p2">$</span> git remote -v</div>
<pre class="dim">origin  <a href="{repo_url}">{repo_url.replace('https://', '')}</a></pre>

<div class="cmd"><span class="p">$</span> <span class="cur"></span></div>

  </div>
</div>
</body></html>"""
