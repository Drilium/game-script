# Game Script: notes for Claude Code

Dre's free NFL player-prop analysis site (portfolio project, not sold).
Live at https://drilium.github.io/game-script/ from the GitHub repo
`Drilium/game-script`. **Read `HANDOFF.md` first**: it holds every design
decision, the data sources, what is and isn't free, and the history of
each feature (newest sections at the top).

## Layout
- `index.html`: the whole site, one file (HTML + CSS + JS), hash routes
  (`#/board`, `#/props`, `#/ladders`, `#/player/<id>`, `#/game/<id>`,
  `#/sim/<gid>`...). No build step, no framework.
- `build_data.py`: the pipeline (Python standard library only). It pulls
  nflverse release files (schedules, weekly stats, play-by-play 2024-2026,
  snap counts, injuries, depth charts, participation/coverage charting,
  FTN charting) and writes the JSON files below. It calls
  `build_script.py` (game-state / simulator data) and `build_board.py`
  (matchup board, per-game best props, grading and self-learning).
- Data (all committed, all regenerated each refresh):
  - `nfl_data.json`: loaded on every page.
  - `nfl_h2h.json`, `nfl_allowed.json`: player vs defenders, and the games behind each "defense allows" number.
  - `nfl_script.json`: score-state play calling, shares, plays for the simulator.
  - `nfl_board.json`: matchup board, change log, report cards, learned model. Stateful: each run diffs against the committed copy.
  - `nfl_learn.json`: frozen weekly candidates (training data). Stateful; not published.
- `.github/workflows/refresh-data.yml`: runs `python3 build_data.py`
  daily and after game windows, commits the JSON, publishes to GitHub
  Pages. A push to `main` republishes.

## Run it locally
```
python3 build_data.py          # ~1-2 min; needs internet (GitHub release downloads)
python3 -m http.server 8765    # then open http://localhost:8765/
```
Weather (Open-Meteo) and sportsbook lines (The Odds API, needs the
optional `ODDS_API_KEY` secret) only fetch when reachable; both fail soft.

## Product rules (from Dre)
- Player props first; fantasy kept but separate and second.
- Props run on **recent form**: this season, steadied by last season counted
  as a few games; two seasons back is history only (not a prop input).
- Data sits in high-contrast raised boxes; dark "broadcast" theme by default
  (StatTree-style), light mode as a toggle.
- Every "defense allows" number links to the games behind it.
- Show reasoning for every pick; be honest about what free data can't do
  and about backtest results (no inflated records).
- Keep it free: no paid APIs unless optional and capped.
- Starting QBs come from the schedule's listed starter, then depth-chart
  QB1, never "most attempts".

## Conventions
- Pipeline: standard library only (runs on a bare GitHub runner).
- Site: plain JS, no dependencies; theme tokens on `:root`
  (`:root[data-theme="light"]` for light); the "Broadcast skin" block at
  the end of the stylesheet wins.
- When the board's model inputs change, bump `METHOD` in
  `build_board.py`: backtests and the learned model rebuild and the live
  week logs the switch as the reason for its changes.
- Commit data and code separately when possible; the refresh bot commits
  data too, so `git pull --rebase` before pushing.
