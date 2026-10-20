# Game Script — NFL analyzer prototype

A static site (`index.html`) that fetches real 2026 NFL data (`nfl_data.json`)
at load time. Pages: This week (slate, prop research, stat leaders, best units, fantasy last), Stat leaders (every category, totals or per game, with next matchups), Teams and team
pages, Players and player pages (offense and defense), Compare (player vs
player, player vs an opposing defense's individual defenders, and team vs
team), a matchup page for every game, and Props.

Three data files are produced: `nfl_data.json` (loaded on every page),
`nfl_h2h.json` (player-vs-defender plays since 2024) and `nfl_allowed.json`
(every game behind every "defense allows" number). The last two load only on
views that need them. All three must be committed next to `index.html`. The data is pulled from nflverse's public datasets by
`build_data.py` — rosters, weekly stats, snap counts, the full schedule with
closing betting lines, multi-season opponent history, and real
defense-allowed-by-position numbers.

## One-time setup (about 10 minutes)

1. **Create a GitHub repo** and push everything in this folder to it
   (`index.html`, `nfl_data.json`, `nfl_h2h.json`, `nfl_allowed.json`, `build_data.py`, `netlify.toml`, and the
   `.github/workflows/` folder — keep that folder structure as-is).

2. **Connect the repo to Netlify**:
   - Netlify → Add new site → Import an existing project → pick this repo
   - Build command: leave blank (there isn't one — it's just static files)
   - Publish directory: `.` (the repo root — `netlify.toml` already sets this)
   - Deploy. Netlify will redeploy automatically every time something is
     pushed to this repo, which is what makes the weekly refresh below work.

3. **Nothing to configure for the weekly refresh** — the GitHub Actions
   workflow in `.github/workflows/refresh-data.yml` is already set up to run
   on its own. GitHub Actions is enabled by default on every repo.

## How the refresh and publishing work

One GitHub Actions workflow (`.github/workflows/refresh-data.yml`) runs after
every game window and again Tuesday morning (times in ET, approximate):
Friday 5am (after Thursday night), Sunday 1am (after Saturday games), Sunday
6pm (after the 1pm games), Sunday 9:30pm (after the 4pm games), Monday 1am
(after Sunday night), Monday 8am (overnight corrections), Tuesday 1am (after
Monday night) and Tuesday 9am (full weekly refresh). Each run:

1. Runs `build_data.py`, which re-pulls rosters, stats, snap counts, the
   schedule and three seasons of play-by-play from nflverse and rewrites
   `nfl_data.json`, `nfl_h2h.json` and `nfl_allowed.json` (about 15 seconds).
2. Commits the data to `main` if it changed.
3. Publishes `index.html` plus the three data files to GitHub Pages.

Any code push to `main` also republishes the site (no data rebuild).
Scores usually appear in the evening runs; full player stats depend on when
nflverse publishes them, usually overnight, so the morning runs fill in the
rest.

**One-time setup:** repo **Settings → Pages → Build and deployment →
Source: GitHub Actions**. Netlify still works too if you prefer it: connect
the repo and it redeploys on every data commit (`netlify.toml` is kept).

### Running it manually

**Actions** tab → "Refresh NFL data and publish the site" → **Run workflow**.

### Checking it's working

The page footer shows when the data was last generated (`generated` in
`nfl_data.json`, a UTC timestamp shown in ET) and the latest completed week. If
it is more than a few days old during the season, check the
Actions tab for a failed run — the most common cause is nflverse not having
published a file yet (rare, but the script skips that season gracefully
rather than crashing).

## Local development

```
python3 build_data.py        # rebuilds nfl_data.json using today's date to pick the season
python3 -m http.server 8000  # serve the folder locally
```

Then open `http://localhost:8000`. Opening `index.html` directly as a local
file (`file://...`) will NOT work — browsers block `fetch()` of local JSON
files for security reasons. It has to be served over HTTP, which is also
exactly how Netlify serves it in production.
