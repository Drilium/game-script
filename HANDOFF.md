# Game Script — Handoff Notes

This file is for whoever (human or Claude session) picks this project up next.
It captures the state, the decisions already made, the known bugs, and the
open questions — so you don't have to re-derive any of it from scratch.

Read this *before* touching `build_data.py` or `index.html`.

## Live site (2026-10-20)

- https://drilium.github.io/game-script/ — repo `Drilium/game-script`, Pages source "GitHub Actions".
- First refresh and publish runs succeeded 2026-10-20 (~1:10am ET). Data commits come from `nfl-data-bot`.

## Product vision and design rules (from Dre, keep these)

- **What makes this app different:** comparison, and ease of access. Anyone
  can come, learn and understand more. Give as much data AND context as
  possible. Most apps hide the data behind hit rates and trends and leave out
  matchup differences, weather and injuries, and how those change player and
  team matchups. This app shows them.
- **Design rule:** data sits in its own raised boxes, a different color from
  the page background (matte stone page, white boxes in light mode; graphite
  boxes on near-black in dark mode), so it pops off the page. Apply this to
  every new table, chart, list and comparison (CSS: `.scroll`, `.tugs`,
  `.viz`, `.tiles`, `.kp`, `.ldr>div`, `.lines`, `.gterm`).
- **Whole picture:** every game since 2024 plus the next matchup's context,
  not "last 5 / last 8" windows.
- **Props and fantasy are separate (2026-10-20, later):** prop research and
  fantasy help both stay, but each lives in its own labeled section (dark
  "Player props" / "Fantasy (PPR)" / "Context for both" bands on This week and
  player pages, plus a dedicated Fantasy page). Props first, fantasy second.
- **Contrast:** data boxes must stand well apart from the page (light: white
  boxes, `--edge` border and `--lift` shadow on a #C7C2B6 page; dark: #202725
  boxes on #050606).
- **Props research first, fantasy second (2026-10-20):** player-prop research
  is the main focus. Fantasy stays, but it always comes after the prop data:
  last in stat lists, tiles, charts, the home page and the glossary.
- **Everything links:** stat titles go to the glossary; every "defense
  allows" number goes to the games and players behind it; players and teams
  link to their pages, so you can move around the data by clicking.

## What this project is

"Game Script" is an all-in-one, free, real-data NFL analysis web app. The
founding principle, stated explicitly by the owner (Dre) and still the
north star: **analysis first, odds second.** The app should be able to say
things like "Troy Franklin's reception prop is mispriced relative to his
real usage" based on real averages — not built around betting, built
around understanding matchups, usage, and game script, with odds layered
on top later only to flag under/overpriced players.

A second original idea — a "fake news detector" that compares crowd
narrative/pre-game buzz against what the data actually says — was part of
the original pitch and has **not been built yet**. It's still a good
differentiator if there's appetite to pursue it (would need a news/injury
feed — see Roadmap below).

## Architecture (as simple as it gets, on purpose)

- **`index.html`** — a single-file vanilla-JS SPA, hash-routed
  (`#/game`, `#/players`, `#/compare`, `#/props`, `#/player/<id>`). No
  framework, no build step.
- **`nfl_data.json`** — the entire dataset, fetched at page load via
  `fetch('nfl_data.json', {cache:'no-store'})`. This is why opening
  `index.html` directly as a `file://` URL does NOT work — browsers block
  `fetch()` of local files. It must be served over real HTTP (Netlify in
  prod, `python3 -m http.server` locally).
- **`build_data.py`** — the entire data pipeline. Pulls from nflverse's
  free public GitHub release assets (rosters, weekly player stats, snap
  counts, schedule w/ closing lines, FTN charting, Next Gen Stats,
  play-by-play), reshapes it, and writes `nfl_data.json`.
- **`.github/workflows/refresh-data.yml`** — GitHub Actions: runs after
  every game window (Fri, Sun x3, Mon x2, Tue x2 in UTC cron) plus manual
  `workflow_dispatch`; reruns `build_data.py`, commits the four JSON files,
  then publishes to GitHub Pages (Pages source must be "GitHub Actions").
  Code pushes to `main` republish without rebuilding data. No server, no
  database, anywhere in this stack.
- **`netlify.toml`** — `publish = "."`, no build command. Pure static
  hosting.

This repo folder now has the `.github/workflows/refresh-data.yml` file
placed correctly (it was previously lost on a prior drag-and-drop upload
to GitHub's web UI — `.github` is a hidden folder and drag-and-drop can
silently skip it). **When pushing this repo to GitHub, do not drag-and-drop
the whole folder through the web UI** — either `git push` from a real git
client, or use "Add file → Create new file" and type the full path
(`.github/workflows/refresh-data.yml`) so GitHub creates the folder itself.

## Data sourcing — what's free and already wired up

All of it comes from `nflverse/nflverse-data` on GitHub (public release
assets, CSV/CSV.GZ). No paid API is involved in anything currently built.

- Rosters, weekly player stats, snap counts, full schedule (incl. closing
  betting lines) — plain CSV.
- FTN charting data (`stats_player_week` + a separate FTN file) — joined
  into play-by-play via `(game_id, play_id)` ⇄
  `(nflverse_game_id, nflverse_play_id)`. Covers ~97% of all plays, 100% of
  pass/dropback plays. Gives: `n_defense_box`, `n_blitzers`,
  `n_pass_rushers`, `is_play_action`, `is_rpo`, `is_motion`,
  `is_screen_pass`, `is_no_huddle`, `is_drop`, `is_catchable_ball`,
  `is_contested_ball`, `is_interception_worthy`, `is_throw_away`,
  `is_qb_fault_sack`.
  - **FTN does NOT include man/zone coverage shell, personnel groupings
    (11/12/21 personnel), or alignment (slot vs. wide).** This was
    confirmed by directly inspecting the CSV header — don't assume it's in
    there under a different field name.
- Next Gen Stats — `ngs_receiving.csv.gz`, `ngs_rushing.csv.gz`,
  `ngs_passing.csv.gz`. These are single combined files spanning *all*
  seasons, not per-season files — don't re-fetch per season.
  - `avg_separation`, `avg_cushion`, `avg_intended_air_yards`,
    `avg_yac_above_expectation` (receiving)
  - `efficiency`, `rush_yards_over_expected_per_att`,
    `percent_attempts_gte_eight_defenders` (rushing)
  - `avg_time_to_throw`, `aggressiveness`,
    `completion_percentage_above_expectation` (passing)
  - **The `week==0` "season aggregate" row is NOT reliably published
    early in a season.** Don't rely on it — average the `week>0` rows
    yourself instead. (Confirmed empirically: Puka Nacua had zero
    `week==0` rows checked early in 2026.)
- Red zone trip/TD rate — computed directly from play-by-play, no FTN
  needed: group by `(game_id, posteam/defteam, fixed_drive)`, a drive is a
  "trip" if any play has `yardline_100<=20`, a "TD" if
  `fixed_drive_result=='Touchdown'`.

**Gotchas that will burn you if forgotten:**
- Every nflverse fetch **must** send a `User-Agent` header, or GitHub
  returns empty/blocked content with no obvious error.
- Play-by-play and NGS files are gzip — use `gzip.decompress()`.

## What's explicitly NOT free / NOT solvable without a vendor contract

This was researched thoroughly — don't re-research it, the answer won't
have changed:
- **Man/zone coverage shell, personnel groupings, alignment data** — not
  published free anywhere *live*. (Correction 2026-10-26: nflverse's
  `pbp_participation` files do carry man/zone, coverage shell and routes
  for completed seasons, 2024 and 2025 — now used. The current season
  still isn't available.) PFF, Sports Info Solutions, and Pro
  Football Reference all have it, but **all three prohibit automated
  scraping in their terms of service** — don't build a scraper against
  them. A real subscription/API contract is the only compliant path.
- **NFL Big Data Bowl (Kaggle)** has real, human-verified coverage labels
  tied to real player-tracking data — but it's released once a year as a
  historical slice of *past* seasons. It can never be "live for the
  current season," no matter how good a model built on it is, because the
  raw tracking data for current games isn't released in real time. Good
  for an offline research/portfolio project; not a fix for weekly
  auto-update.
- **NFL Pro** ($14.99/mo or $99.99/yr) — human-only dashboard + film, no
  API/export. Useful for personal film study, not automatable.
- **Sportradar** — genuine 30-day free trial exists, production-equivalent
  data at throttled rate limits, but note some products (likely including
  NGS/tracking-specific ones) are sales-gated even inside a trial account,
  per their own docs. Worth trying for odds/injury data; don't expect
  coverage-shell data to be included.
- **"Build a bot that watches the game and calls coverages"** — technically
  a real field of research (that's literally what Big Data Bowl entrants
  do), but needs live broadcast-feed computer vision or real-time
  tracking-data access neither of which is available to an indie builder
  today. Flagged as a hard, multi-month ML project, not a weekend feature.

## Position-attribution bug — FIXED (2026-10-19)

**Was:** the multi-season history / defense-allowed loop in `build_data.py`
joined every stat row to the **current roster** (`roster.get(pid)`). That
silently dropped every row for a player no longer on an active roster
(13,212 of 43,087 REG rows across 2024-26 — 31%) and misfiled anyone who
changed position. Dallas looked ~13th in pass yds allowed; really 27th
(250 yds/g) and 32nd in pass TDs allowed. CIN and WAS were even further off.

**Fix as shipped:** defense-allowed now uses each row's own
**`position_group`** column — NOT `position`, as the earlier note
suggested. In `stats_player_week_*.csv`, `position` is granular (CB, SAF,
DT, DE, FB, OT...) while the app/roster vocabulary is grouped (QB, RB, WR,
TE, DB, LB, DL, OL, SPEC). `position_group` matches; raw `position` would
have dropped fullbacks from "allowed to RBs". Per-player history (`hist`)
is still limited to current-roster players and uses their *current*
position's columns so the table on their page lines up.

`build_scheme()` was checked: it does not have this bug. Team tendencies
come from `posteam`/`defteam` per play; the roster is only used to limit
grades to current players, which is intended.

Keep the string-matching discipline: a loose substring match like
`if fn in nm or nm in fn` matches an **empty** `player_display_name` to
everything (`"" in nm` is always true). Use exact equality.

## Front-end fixes (2026-10-19)

- The top banner said "refreshed undefined": `NOTE` was a string built at
  parse time, before `nfl_data.json` loaded. It is now a function.
- Props verdict badges used `.flag.ok/.d/.q` classes with no CSS, so they
  had no color. Green, red and neutral styles were added.

## UI redesign + Compare (2026-10-19)

`index.html` was rewritten (still one file, vanilla JS, hash routes, no build):
- Routes: `#/` this week, `#/game/<id>` matchup, `#/teams`, `#/team/<ABBR>`,
  `#/players`, `#/player/<gsis_id>`, `#/compare/players/<a>/<b>`,
  `#/compare/teams/<A>/<B>`, `#/props`. Old `#/compare/<a>/<b>` links still work.
- Global search box in the header (press `/`), keyboard-navigable combobox.
- Compare: "tug of war" rows. A = blue, B = orange (a CVD-checked pair).
  Bar length = percentile at the position (players, among players with a real
  role: `qualifies()`) or league rank (teams). Yellow tick = league median.
  Style metrics (pass rate, motion %, aDOT, etc.) show rank but never pick a
  winner. Season / last-3-games toggle for players.
- The yellow line is the one visual signature: league median on bars, the
  user's line on player game charts and Props strips.
- Matchup page: tale of the tape, each offense vs the opposing defense,
  defense-allowed by position, key players with matchup notes, head to head.
- Defense-allowed ranks: 1 = allows the least. On player-facing views it is
  shown from the player's side (a high rank = friendly matchup, green).

`build_data.py` additions that power this:
- `team_stats[team]`: record, points, ATS and over/under vs closing lines,
  per-game offense and defense (yards, EPA/play, EPA/dropback, EPA/rush, TDs,
  turnovers, sacks, pass rate, plays), and a weekly `log`. Built from every
  stat row by its own `team` / `opponent_team`, never the current roster.
  Note nflverse `spread_line` is the expected HOME margin (positive = home favored).
- `past_games`: prior-season results for head-to-head.
- `wk[].fp`: PPR fantasy points per game in each player's game log.

## Player vs defense, team colors, full-sample stats (2026-10-19, later)

Owner asked for: (1) comparing a skill player against the opposing team's
individual defenders, (2) three team colors instead of one, (3) no
"last N games" windows: show the whole picture plus the next matchup.

**Defenders, data side (`build_data.py`):**
- Roster `depth_chart_position` is kept as `pp` (CB, FS, SS, ILB, OLB, DE,
  DT, NT). The UI groups these into roles CB / S / LB / DL and computes
  percentiles within the role, so corners rank against corners.
- Defender season stats now include QB hits, tackles for loss, forced
  fumbles; weekly logs include them too. Season splits `sz` for prior
  seasons exist for every player (offense per game; defense tackles per
  game, splash plays as season totals).
- `build_h2h()` streams 3 seasons of play-by-play (2024-26, ~45 MB
  compressed total) and records, for every offensive player vs every
  defender, the plays where the defender was CREDITED: tackler, pass
  defensed, interception, sack/half sack, QB hit. Output goes to
  `nfl_h2h.json` (~0.9 MB) as
  `pairs[offense_id][defender_id] = [plays, yards, td, pd, int, sacks, qb_hits, seasons_bitmask]`.
  The site loads it lazily.
- **Honest limit, keep it in the UI:** free data does not record coverage
  assignments. A credited play means the defender was around the ball
  (made the tackle / broke it up), not that he was in coverage. The UI says
  this wherever the numbers appear (`CREDIT_NOTE`).
- `dv` on each defender (this season): plays vs WR/TE/RB, catches he
  tackled and yards on them, run tackles, yards on them, stuffs (runs
  tackled at or behind the line).
- `teams` meta: name + 3 colors from nflverse `teams_colors_logos.csv`,
  near-duplicate colors removed, with `TEAM_COLOR_OVERRIDES` for teams whose
  file colors are short or are logo accents rather than uniform colors.

**UI:**
- New `#/compare/defense/<player_id>/<TEAM>` ("Player vs defense" tab):
  what that defense allows to his position (every stat, ranked), team
  defensive tendencies, every defender at the spots that matter for his
  position (WR: CB/S/LB; TE: LB/S/CB; RB: LB/DL/S; QB: DL/LB/CB/S) with
  percentiles and their credited plays against him, a side-by-side card
  for any defender, every credited play against him by current defenders
  league-wide, and his games against that team.
- Defender player pages: role percentiles, every season, game log, the
  next opponent's players he'll see with head-to-head, and everyone he has
  made plays against.
- Matchup and team pages list every defender who has played.
- Team mark everywhere = three equal color bands (`flag()`), bigger.
- Removed: the "last 3 games" toggle and the 300-row cap on Players.
  Added: "Every season" tables (2024 on, plus an all-seasons row), game
  charts covering every game since 2024 with a season filter, compare
  toggle "This season / Every game since 2024", and Props showing both
  this season and since-2024 averages and hit rates. When those disagree,
  the read says so instead of picking one.

## Schedule ahead + Compare tab carry-over (2026-10-19, later)

- `games[]` now carries everything nflverse publishes in advance for every
  game of the season, including future ones: `time` (kickoff, ET), `gt`
  (game type; playoff games appear automatically once scheduled), `loc`
  (`Neutral` = international/neutral site), `stad`, `roof`, `surf`, `ar`/`hr`
  (away/home rest days), `div`, `aqb`/`hqb` (listed starting QBs).
- Shown on: game tiles and the matchup page (kickoff, venue, rest, listed
  QBs); team pages (full 18-week schedule with bye, opponent record and
  O/D EPA ranks, venue, rest edge, remaining strength of schedule); teams
  list (next game, combined record of opponents left); players list (next
  game); player pages ("Schedule ahead": every remaining game rated for
  him, from defense-allowed rank for skill players or opponent offense EPA
  rank for defenders); Compare players and Compare teams (both schedules
  week by week, side by side).
- Compare carry-over (owner's rule): the player last picked on Players
  (player A) carries into "Player vs defense" on the next tab switch, set
  against his next opponent; the player last picked on "Player vs defense"
  carries back to Players as A. After that first switch, choices made
  inside a tab stay independent until a new player is searched. A
  defender picked as A can't carry into "Player vs defense" (offense only),
  so that tab keeps its own player. State lives in `CARRY` in index.html.

## Outlook, player vs player, longest plays (2026-10-20)

**Data (`build_data.py`, inside the play-by-play scan):**
- Longest reception `lr` and longest run `lru` for every player-game since
  2024 (not in the weekly stats file), attached to `hist` and `wk` lines.
- Defense-allowed now also has `lr` (average longest catch allowed per game
  to that position), `x20` (20+ yard catches allowed per game), and `lru`
  for RBs.

**Projection model (`project()` in index.html), deliberately simple and
explained in the UI ("How the projection works"):**
1. Base = per-game average, blending this season with every game since
   2024. This season's weight = min(games, 6)/12, so at most half.
2. Defense factor = 1 + half of (opp allowed to his position / league avg
   - 1), capped 0.8 to 1.2.
3. Game-script factor = implied team total (from closing/current spread and
   total) vs his team's points per game; volume stats move 30% of the gap,
   TDs 60%, capped 0.9 to 1.1. No lines posted means factor 1.
4. Range = 25th to 75th percentile of his games since 2024 times the same
   factors.
It is not a betting model; the UI says so. Good next steps if it's
extended: weight defense allowed toward this season, use team pass/run rate
for volume, and backtest projections against actual results.

**Likely one-on-ones (`likelyPairs()`):** WR1-3 by targets vs CB1-3 by
snap %, top TE vs top safety (or LB), lead RB vs top LB, starting QB vs top
pass rusher (2 x sacks + QB hits). This is a role-based pairing, not
charted coverage, and the UI says that everywhere it appears.

**UI:**
- New `#/outlook` page: every player's projection for any remaining week
  (defaults to the next full week), by position, sortable, with the likely
  one-on-one and a link to the player-vs-defense view.
- New Compare tab "Player vs player" (`#/compare/duel/<offense>/<defender>`):
  defaults to his likely one-on-one in his next game; head to head, how the
  defender handles that position, both players' seasons, the game outlook,
  other likely one-on-ones in that game.
- Matchup page has a "Player vs player" section for both sides.
- Every player-context matchup link now opens that player's stats against
  that defense: the player page "Next" button, the Players list "Next game"
  column, Schedule ahead, key player lists, Outlook "Details".
- Player pages and the player-vs-defense view show the outlook table.
- Props shows the next-game projection for the stat.
- Compare carry-over now spans Players, Player vs defense and Player vs
  player (`PICK`/`SEEN` in index.html): the most recently searched player
  carries into the tab you switch to, once.

## Design overhaul (2026-10-20)

Goal from the owner: tighter, cleaner, data-first; less "cartoony"; team
marks as circles; aligned city/team names; charts that keep every value
readable when one game is an outlier.

- Type: Archivo at normal width. Page titles 34px (28 on phones), section
  heads 19px, body 15px, tables 14px with 12px headers, stat figures 24px
  with tabular numbers. The giant condensed display type is gone.
- Chrome: hairline rules (1px) instead of 2px ink rules; 6-8px radii;
  smaller buttons and controls.
- Team mark: three overlapping circles in the club's colors (`flag()`;
  sizes `sm`, default, `mid`, `lg`). The mark sits inline with the full team
  name everywhere (team header, scoreboard), which fixes the misalignment.
- Percentiles: Baseball Savant-style slider (`pctBar()`): track, fill,
  yellow median tick, and a circle with the percentile number, on a 5-step
  red-to-green scale (`--p0`..`--p4`). Tables use the small circle `pcDot()`.
- Charts (`gameBars`, `lineChart`, `marginChart`):
  - `capScale()` finds outliers (beyond Q3 + max(2 x IQR, 0.6 x Q3)). When
    one exists, the scale fits the normal games and the outlier runs past
    the top into an overflow band with a cut mark and its real value
    ("break the frame", PolicyViz). A note under the chart explains it.
  - Every bar is value-labeled when there are 20 or fewer games; labels are
    drawn after the yellow line with a halo so the line never hides a value.
  - Negative values (e.g. -8 receiving yards) draw below zero.
  - Line charts carry a compact data table underneath; the higher value
    each week is bold.
  - The canvas is 980 units wide so chart text renders at normal size on
    desktop; on phones charts scroll sideways at a readable size.

## Glossary, linked titles, team-color fix (2026-10-20)

- **Bug fixed:** team marks showed one color three times because a rebuild
  hit a temporary network error fetching `teams_colors_logos.csv` and wrote
  an empty `teams` block. Now every download retries (`_get`, 4 tries with
  backoff), `build_data.py` carries `DEFAULT_TEAM_COLORS` for all 32 clubs
  as a backstop, and `index.html` starts `TC` from the same table, so marks
  can't fall back to a single color again.
- **Category titles** (tile labels, comparison row labels, line labels such
  as Spread/Total) are bold in their own indigo `--title` color (#4B3FB0
  light, #B1A9F7 dark), kept distinct from green/red status colors and the
  blue/orange A/B identity colors. Each title links to its definition.
- **Glossary page** `#/glossary` (also in the nav): ~80 entries in 13
  groups, each with a plain-language definition, "how to read it", and the
  source. Data lives in `GLOSS` in index.html:
  `[id, group, term, definition, how to read it, source, stat keys]`.
  `glab(text, key, id)` renders a linked title; `GLK` maps stat keys to
  entries, `lblId()` maps comparison-row labels. Add a stat? Add its key to
  the matching entry's key list and the title links automatically.
  `#/glossary/<id>` scrolls to and briefly highlights the entry.
- **Scroll memory:** the site remembers where you were on each page. The
  glossary's "Back to the page you were on" button and the browser back
  button both return to the same scroll position.

## Drill-down behind every defense number + boxed design (2026-10-20)

- New data file `nfl_allowed.json` (~0.5 MB, loaded only on drill-down pages,
  committed by the weekly workflow): for every defense, every regular-season
  game since 2024 with each opposing QB/RB/WR/TE's line, including players no
  longer on a roster (names kept), plus weather (temp, wind, roof), home/away,
  and 20+ yard catches allowed. Built by `build_allowed_log()` from the same
  rows as `defense_allowed`, so the averages match exactly (checked: NE allows
  215.6 pass yds/g to QBs in both).
- New page `#/allowed/<TEAM>/<POS>/<stat>`: allowed per game with rank, league
  average, most and fewest in a game, games above league average; a chart of
  every game against the league average (yellow line); every game with the
  opposing players and their numbers (hover for the full line); biggest
  individual games against them; who faces them next. Season filter, position
  and stat chips, and a defense picker, so pages link into each other.
- Every "allows" number on the site (matchup page table, team page table,
  player-page matchup tiles, team compare, outlook) is a dotted-underline link
  into that page. Back returns to the same scroll spot.
- **Free injury reports exist:** nflverse publishes `injuries/injuries_<season>.csv`
  (weekly report status and practice participation), including 2026. The
  earlier note that injuries need a paid feed was wrong for the official
  report. Next step: show report status on player, matchup and outlook pages.

## Best props page + game script (2026-10-25)

- `#/best` (`vBest`): the strongest over/under spots per game for the next
  full week. For each qualifying QB/RB/WR/TE and main prop stats (`BP_KEYS`):
  reference line = his median this season (4+ games, else since 2024) to the
  nearest .5 (`halfLine`; whole-number medians lean toward the mean). Projection
  = `project()` x `scriptFactor()`. His whole spread of games since 2024 is
  centered on the projection (scaled by projection / career average) and
  checked against the line; projection and that hit rate must agree. Signal
  = distance from 50% with at most 16 games counted: Strong >= 2, Solid >= 1.4,
  Lean >= 1. Minimum lines (`BP_MIN`) drop tiny roles. Top 10 and each game
  show one pick per player; the full table shows all.
- Game script (`build_script_model` in build_data.py, `data.script`): within
  each team-season, pass rate vs how much more or less favored than usual,
  every REG game since 2024. Slope about -0.25 pass-rate points per point of
  spread (r about -0.13, n about 1,300). `gameScript()` turns this week's spread
  into an expected pass rate; pass stats x exp/usual, rush stats x the
  complement, capped at 10%.
- `project()` is now role-aware (`roleChanged`: new team, or usage changed by
  more than a third): this season's weight rises to full at 5 games.
- This week's Player props band opens with the top 4 Best props.
- Known limits: no real sportsbook lines (books already price matchups), no
  injury status. Glossary entries `best-props`, `game-script`.

## Fantasy page + props/fantasy separation + higher contrast (2026-10-20)

- `#/fantasy` (`vFantasy`): week picker, QB/RB/WR/TE/FLEX. Projected PPR
  points (`fpProj`: his blended PPR average scaled by the prop model's
  defense + market adjustments applied to the scoring stats, factor capped
  0.75-1.25), typical range, starter-level weeks (cut = per-game PPR of the
  12th QB/TE, 24th RB/WR this season, `fpCut`), opportunity, and PPR points
  the opponent allows to his position. Then a 32-team table of PPR points
  allowed to QB/RB/WR/TE, each linked to the drill-down.
- Data: `defense_allowed[team][pos].fp` (PPR allowed per game) and `fp` in
  every allowed-log player line; drill-down accepts `#/allowed/TEAM/POS/fp`.
- Player pages (offense): Player props band (outlook + hit-rate chart),
  Context band (season, how he wins, defenders, schedule), Fantasy band
  (`fantasySection`: PPR/g with percentile and position rank, since 2024,
  next-game projection, opponent PPR allowed, starter weeks, opportunity,
  best/worst game, PPR chart vs the starter line, PPR by season). Fantasy
  points no longer appear in the prop tiles, game chart or season table.
- This week: Player props band, Fantasy band (top projected PPR per
  position), Context band (stat leaders, best units).

## Stat leaders + props-first order (2026-10-20)

- **Leaders page** (`#/leaders`, `#/leaders/<group>`, `#/leaders/<group>/<stat>`):
  Passing, Rushing, Receiving, Scrimmage, Defense, Fantasy. Season totals or
  per game (`LD.mode`), position chips (`LD.pos`). Totals are summed from each
  player's weekly log (`ldTot`), so they agree with per-game numbers exactly;
  defender sacks/PD/QB hits/TFL/FF/INT use the season totals. Also: longest run
  and catch, 100-yard rushing/receiving games, 300-yard passing games, yards
  from scrimmage, rushing plus receiving TDs. Ties share a rank.
- Qualifying: per game needs half of the team's games; rates need 14 pass
  attempts, 6.25 carries or 3.5 targets per team game (`LD_MIN`).
- **Full list** for each stat shows every qualifier with every other stat in
  the group, plus the next game and what that defense allows to his position
  for that stat (linked to the drill-down); defenders get the opposing
  offense's EPA rank.
- **Props first:** nav is This week, Props, Outlook, Leaders, Players, Teams,
  Compare, Glossary. This week shows Prop research (top projections per
  position plus the matchups moving numbers most, `propResearch`), then stat
  leaders, best units, and fantasy leaders last. The Props page shows the same
  research under the watchlist. Player pages open with the next-game prop
  outlook and the hit-rate chart; fantasy points moved to the end of every
  stat list, and the default chart stat is the main prop stat. Key players
  show pass/rush/rec yards instead of fantasy points; the Players list "All"
  view sorts by yards from scrimmage.

## Game simulator + game-state data + injuries + coverage (2026-10-26)

Built because Dre wants the model to read play-by-play the way a game is
actually played: score state, penalties, where throws go, and lines that
look impossible but are just what a team has to do to win.

- **`build_script.py` (new)** — called from `build_data.py`, writes
  **`nfl_script.json`** (~400 KB, lazy-loaded by the simulator, player and
  team pages). From pbp 2024–2026 it builds:
  - Score state for every snap, offense view, before the play: trailing 9+,
    trailing 1–8, tied, leading 1–8, leading 9+.
  - Team pass rate per state (shrunk toward the league and the team's own
    2024–25 identity, prior strength 40), plays per game, targets and
    carries per dropback/run, average depth of target.
  - Every player's share of targets and carries per state. Denominators are
    every game he played offensive snaps in (snap counts keyed by name +
    team), not just games where he touched the ball. That fix took the sim
    from ~0.77 to ~0.9–0.96 of season averages.
  - Targets by field zone (short/deep × left/middle/right) for offenses and
    for defenses, against the league.
  - Penalties by team and season: offensive/defensive flags and yards,
    flags drawn, most common types. Defensive pass interference is credited
    to the targeted receiver (PI yards are not receiving yards).
  - Coverage from **`pbp_participation_<season>.csv`** (nflverse release
    `pbp_participation`): `defense_man_zone_type`, `defense_coverage_type`,
    `route`. **This is free, but only for completed seasons (2024 and 2025
    exist; 2026 isn't published).** It powers team man-coverage rate and
    shells, and each receiver's yards per target against man vs zone.
  - A "bank" of ~1,300 real team-games with final margin and share of plays
    in each state.
- **Simulator (`#/sim`, `#/sim/<gid>`, `#/sim/<gid>/<pid>`)** — 8,000 games
  per matchup. Home margin = spread + 13.5·N(0,1); a real team-game with a
  similar margin from the bank sets how many plays happen in each score
  state; play count blends team pace, the bank game and the total. Each
  state's dropbacks use the team's pass rate there; targets and carries go
  to players by their share in that state. Every target or carry is a real
  play from that player's history (thin samples mixed with a position
  pool), scaled by the opponent's per-opportunity defense and the QB's
  yards per attempt (both capped). Players' shares are normalized to what
  the whole roster normally takes, so an injury redistributes volume
  rather than losing it. The page shows how the game goes (margin buckets,
  dropbacks, carries, passing yards with ranges), lets you sit/put in any
  player and type any share, and gives the chance of any line plus a
  histogram for any player and stat.
- **Injuries:** `injuries/injuries_<season>.csv` (free nflverse). Latest
  week's status per player goes in `nfl_data.json` as `inj`. Out/Doubtful
  for that game's week sits him out automatically in the sim; Best props
  skips Out/Doubtful players and badges Questionable ones; player headers
  and game pages show the report.
- **Player pages:** "How his role changes with the score" (target/carry
  share by state), depth, field zones, red-zone looks, vs man/zone.
- **Team pages:** "Play calling by game state", "Where passes beat their
  defense", "Penalties", "Coverage".
- **Best props:** each card now also shows the simulated chance.
- Calibration check (week 8 slate): team targets, carries and attempts land
  within ~3–8% of season rates; player yardage means ~0.92–0.96 of season
  averages (some of that is intentional regression toward 2024–25 roles).
  **Not backtested yet** — the next real improvement is replaying past weeks
  and scoring the sim's probabilities.
- Limits: no live odds (lines come from the schedule file), coverage stops
  at 2025, in-game injuries and weather aren't modeled.

## Feature inventory (what's actually built and shipped)

- Game-day view, full player pages (bio, season stats, per-week game log,
  fantasy ppg), head-to-head player comparison, multi-season
  opponent-matchup history per player.
- Scheme/tendency blocks per team (pressure/blitz rate, PA/RPO/motion %),
  "grade" blocks derived from FTN data (catchable/contested/drop % for
  pass-catchers; turnover-worthy/throwaway/fault-sack % for QBs), NGS
  advanced metrics shown per position.
- Real defense-allowed-by-position numbers per team (attribution bug fixed
  2026-10-19).
- Search via `<datalist>` (deliberately chosen over giant `<select>`
  lists — far less HTML payload) and improved list navigation.
- **Props & Edges** — compares a player's real season average against a
  given over/under line to flag mispriced props (built from the
  Troy Franklin / Adam Trautman 0.5-reception example, where real averages
  were far above the line). Has a `localStorage`-backed watchlist
  (wrapped in try/catch, as it should be).

## Open threads — pick one of these up next

1. ~~Port the position-attribution bug fix~~ — done 2026-10-19 (see above).
2. **Confirm the GitHub Actions workflow actually runs green.** Last known
   status: the workflow file had gone missing from the live repo (hidden
   `.github` folder dropped on upload) and was being recreated via GitHub's
   web UI. It is now also bundled correctly in this repo folder structure.
   Still unverified as of 2026-10-19 (no GitHub access from that session).
   The pipeline itself was run end to end then and completed in ~7s with
   only the standard library, so a failure would be in git/permissions,
   not Python. Whoever continues should verify in GitHub's Actions tab that the
   workflow (a) appears at all, and (b) a manual `workflow_dispatch` run
   completes successfully end-to-end (checkout → python → commit → push).
3. **Refresh cadence — DECIDED (2026-10-20):** after every game window plus
   Tuesday morning, published to GitHub Pages. Old notes kept below.
   **Decide the refresh cadence properly.** Dre asked to make the refresh
   "automatic based on lineups," which was never pinned down. Options on
   the table, none chosen:
   - Keep the current weekly Tuesday-morning cadence (simplest, already
     built).
   - Refresh multiple times a week around the NFL calendar (e.g., also
     right after Thursday/Sunday/Monday games finish).
   - Wait for a live injury/news feed and refresh right before kickoff —
     this requires solving the odds/news-API roadmap item below first, so
     it's a bigger lift than it sounds.
4. **An unanswered live football question** was left mid-analysis for a
   TB@DAL Thursday-night game: whether a George Pickens "revenge game"
   storyline and a CeeDee Lamb double-team were live possibilities given
   Tampa's run-funnel/pass-funnel profile, and whether Javonte Williams'
   receptions prop should be set at 3 or 4. Partial numbers were computed
   (avg targets 4.25/g, avg receptions 3.75/g, hit rate at 3+ receptions =
   3/4 games this season, at 4+ = 2/4, overall catch rate 88.2%) but no
   conclusion was ever written up. That game has likely already been
   played by the time anyone reads this, so it's really a worked example
   of the kind of question the app should be able to answer well — not a
   live TODO.

## Roadmap (researched but not started)

- **Live odds/props API** for actual current lines, to power Props & Edges
  with real market prices instead of hypothetical lines: The Odds API
  (~$25/mo) or SportsGameOdds (~$99-499/mo) are the realistic entry points;
  OddsJam/OpticOdds are enterprise/sales-gated.
- **Injury reports:** the official weekly report is free in nflverse (see above). A paid feed is only needed for real-time news.
- **Live injury/news feed** for the original "fake news detector" concept:
  ESPN's injury feed via Apify (~$0.10/1000 rows) is the cheap option;
  PFF Pro has a "coming soon" consumer API tier (~$199.99/yr) that may be
  worth checking on by the time this is picked up.
- **Backtest the simulator** — replay weeks 1–7 with only prior data and
  score its probabilities (Brier score / calibration buckets).
- **Coverage-shell/personnel data** — past seasons now wired in (see the
  simulator section); live current-season data still needs a vendor. Only
  real paths are a paid PFF/SIS contract, or a from-scratch ML project on
  Big Data Bowl historical data (understood going in that it can never be
  live for the current season).

## A note on tooling

Dre asked whether paying for a Claude subscription + Opus would meaningfully
improve the build. Current answer researched at the time: the Opus vs.
Sonnet coding-benchmark gap is narrow (within ~1-2 points on SWE-bench),
and the Pro plan ($20/mo) bundles both Claude Code and Opus access. Worth
re-checking if pricing/plans have changed by the time this is read, but the
practical takeaway was: either model is capable of carrying this project
forward — the bottleneck has been scope and data-source research, not raw
coding ability.
