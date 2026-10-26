#!/usr/bin/env python3
"""
NFL analyzer data pipeline.
Pulls current-season rosters, weekly player stats, snap counts, and the
season schedule (incl. closing betting lines) from nflverse's public
GitHub release assets, and builds one compact JSON file that the
prototype site embeds directly (no server, no API key needed).

Run this weekly during the season (e.g. Tuesday mornings after MNF)
to refresh the data. Usage:
    python3 build_data.py [season]
Defaults to the current season inferred from today's date.
"""
import csv, json, sys, io, gzip, urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone

BASE = "https://github.com/nflverse/nflverse-data/releases/download"

def _get(url, tries=4):
    """GET with retries: a single network blip shouldn't silently drop a dataset."""
    import time
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nfl-analyzer-pipeline/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:
            if i == tries - 1:
                raise
            print(f"  (retrying {url.rsplit('/', 1)[-1]} after: {e})")
            time.sleep(2 * (i + 1))

def fetch_csv(path):
    data = _get(f"{BASE}/{path}").decode("utf-8", errors="replace")
    return list(csv.DictReader(io.StringIO(data)))

_RAW = {}
def fetch_bytes(path):
    """Download once per run (play-by-play is read by two different passes)."""
    if path not in _RAW:
        _RAW[path] = _get(f"{BASE}/{path}")
    return _RAW[path]

def fetch_csv_gz(path):
    raw = gzip.decompress(fetch_bytes(path))
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace"))))

def iter_csv_gz(path):
    """Stream a big gzip CSV row by row instead of holding ~170MB of dicts in memory."""
    gz = gzip.GzipFile(fileobj=io.BytesIO(fetch_bytes(path)))
    yield from csv.DictReader(io.TextIOWrapper(gz, "utf-8", errors="replace"))

# --- Team colors -----------------------------------------------------------------
# nflverse's teams_colors_logos.csv is the base. Where its 3rd/4th colors are near
# duplicates (e.g. two silvers) or logo accents rather than uniform colors, use the
# team's official three. Keep this list short and only for real gaps.
TEAM_COLOR_OVERRIDES = {
    "BAL": ["#241773", "#000000", "#9E7C0C"], "BUF": ["#00338D", "#C60C30", "#FFFFFF"],
    "CHI": ["#0B162A", "#C83803", "#FFFFFF"], "CIN": ["#FB4F14", "#000000", "#FFFFFF"],
    "CLE": ["#311D00", "#FF3C00", "#FFFFFF"], "DAL": ["#002244", "#003594", "#869397"],
    "DEN": ["#FB4F14", "#002244", "#FFFFFF"], "GB": ["#203731", "#FFB612", "#FFFFFF"],
    "HOU": ["#03202F", "#A71930", "#FFFFFF"], "IND": ["#002C5F", "#A2AAAD", "#FFFFFF"],
    "LA": ["#003594", "#FFD100", "#FFFFFF"], "LV": ["#000000", "#A5ACAF", "#FFFFFF"],
    "MIA": ["#008E97", "#FC4C02", "#005778"], "MIN": ["#4F2683", "#FFC62F", "#FFFFFF"],
    "NO": ["#D3BC8D", "#101820", "#FFFFFF"], "NYJ": ["#125740", "#000000", "#FFFFFF"],
    "PIT": ["#000000", "#FFB612", "#FFFFFF"], "TB": ["#D50A0A", "#34302B", "#FF7900"],
    "TEN": ["#0C2340", "#4B92DB", "#C8102E"],
}

# Backstop: the three colors for every club, used if the colors file can't be fetched.
DEFAULT_TEAM_COLORS = {"ARI":["#97233F","#000000","#FFB612"],"ATL":["#A71930","#000000","#A5ACAF"],"BAL":["#241773","#000000","#9E7C0C"],"BUF":["#00338D","#C60C30","#FFFFFF"],"CAR":["#0085CA","#000000","#BFC0BF"],"CHI":["#0B162A","#C83803","#FFFFFF"],"CIN":["#FB4F14","#000000","#FFFFFF"],"CLE":["#311D00","#FF3C00","#FFFFFF"],"DAL":["#002244","#003594","#869397"],"DEN":["#FB4F14","#002244","#FFFFFF"],"DET":["#0076B6","#B0B7BC","#000000"],"GB":["#203731","#FFB612","#FFFFFF"],"HOU":["#03202F","#A71930","#FFFFFF"],"IND":["#002C5F","#A2AAAD","#FFFFFF"],"JAX":["#006778","#000000","#9F792C"],"KC":["#E31837","#FFB612","#000000"],"LA":["#003594","#FFD100","#FFFFFF"],"LAC":["#007BC7","#FFC20E","#001532"],"LV":["#000000","#A5ACAF","#FFFFFF"],"MIA":["#008E97","#FC4C02","#005778"],"MIN":["#4F2683","#FFC62F","#FFFFFF"],"NE":["#002244","#C60C30","#B0B7BC"],"NO":["#D3BC8D","#101820","#FFFFFF"],"NYG":["#0B2265","#A71930","#A5ACAF"],"NYJ":["#125740","#000000","#FFFFFF"],"PHI":["#004C54","#A5ACAF","#000000"],"PIT":["#000000","#FFB612","#FFFFFF"],"SEA":["#002244","#69BE28","#A5ACAF"],"SF":["#AA0000","#B3995D","#000000"],"TB":["#D50A0A","#34302B","#FF7900"],"TEN":["#0C2340","#4B92DB","#C8102E"],"WAS":["#5A1414","#FFB612","#000000"]}

def build_team_meta(team_abbrs):
    def rgb(h):
        h = h.lstrip("#"); return [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    def dist(a, b):
        return sum((x - y) ** 2 for x, y in zip(rgb(a), rgb(b))) ** .5
    out = {}
    try:
        rows = fetch_csv("teams/teams_colors_logos.csv")
    except Exception as e:
        print(f"  (team colors unavailable: {e})"); rows = []
    for r in rows:
        t = r["team_abbr"]
        if t not in team_abbrs:
            continue
        pick = []
        for c in (r["team_color"], r["team_color2"], r["team_color3"], r["team_color4"]):
            if c and all(dist(c, p) > 80 for p in pick):
                pick.append(c.upper())
        while len(pick) < 3:
            pick.append("#FFFFFF")
        out[t] = {"name": r["team_name"], "nick": r["team_nick"],
                  "c": TEAM_COLOR_OVERRIDES.get(t, pick[:3]),
                  "logo": r.get("team_logo_espn") or ""}
    for t in team_abbrs:
        if t not in out and t in DEFAULT_TEAM_COLORS:
            out[t] = {"name": t, "nick": t, "c": DEFAULT_TEAM_COLORS[t]}
    return out

# --- Defensive player stats -----------------------------------------------------
# short key -> source columns (summed). tkl is per game on the page; the rest are
# shown as season totals because they are rare events.
DEF_FIELDS = {
    "tkl": ["def_tackles_solo", "def_tackles_with_assist"], "sacks": ["def_sacks"],
    "int": ["def_interceptions"], "pd": ["def_pass_defended"], "qbh": ["def_qb_hits"],
    "tfl": ["def_tackles_for_loss"], "ff": ["def_fumbles_forced"],
}

def def_line(row):
    return {k: sum(num(row.get(c)) for c in cols) for k, cols in DEF_FIELDS.items()}

def build_h2h(seasons, roster, pos_group):
    """
    Player-vs-defender history from play-by-play, plus a profile of how each
    defender is involved on plays this season.

    Free data does NOT say who covered whom. It does say which defenders were
    CREDITED on each play: tackler(s), pass defensed, interception, sack, QB hit.
    So a "head-to-head play" here means: on a throw to this receiver (or a run by
    this back, or a dropback by this QB), that defender made the tackle, broke up
    the pass, intercepted it, sacked or hit the QB. That is real and verifiable,
    but it is not a coverage grade, and the UI says so.

    Returns (pairs, profiles):
      pairs[offense_id][defender_id] = [plays, yards, td, pd, int, sacks, qb_hits, seasons_bitmask]
      profiles[defender_id] (current season only) = {wr|te|rb: [plays, catches, yards],
                                                     run: [tackles, yards, stuffs]}
    """
    TKL = ["solo_tackle_1_player_id", "solo_tackle_2_player_id",
           "assist_tackle_1_player_id", "assist_tackle_2_player_id",
           "assist_tackle_3_player_id", "assist_tackle_4_player_id",
           "tackle_with_assist_1_player_id", "tackle_with_assist_2_player_id"]
    PD = ["pass_defense_1_player_id", "pass_defense_2_player_id"]
    SK = ["sack_player_id", "half_sack_1_player_id", "half_sack_2_player_id"]
    QH = ["qb_hit_1_player_id", "qb_hit_2_player_id"]
    pairs = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0, 0]))
    prof = defaultdict(lambda: {"wr": [0, 0, 0], "te": [0, 0, 0], "rb": [0, 0, 0], "run": [0, 0, 0]})
    cur = max(seasons)
    # Longest plays (not in the weekly stats file): per player-game, and per
    # defense + receiver position per game, for "longest reception" style analysis.
    longs = defaultdict(dict)                       # (pid, season, week) -> {"lr": yds, "lru": yds}
    dmax = defaultdict(float)                       # (defteam, season, week, pos) -> longest allowed
    d20 = defaultdict(int)                          # (defteam, pos) -> catches of 20+ allowed
    d20g = defaultdict(int)                         # (defteam, season, week, pos) -> same, per game
    for i, season in enumerate(sorted(seasons)):
        bit = 1 << i
        path = f"pbp/play_by_play_{season}.csv.gz"
        print(f"Scanning {path} for player-vs-defender plays ...")
        try:
            rows = iter_csv_gz(path)
            for r in rows:
                if r.get("season_type") != "REG":
                    continue
                ids = lambda cols: {r[c] for c in cols if r.get(c)}
                tk, pd = ids(TKL), ids(PD)
                it = r.get("interception_player_id") or ""
                yds, td = num(r.get("yards_gained")), 1 if flag(r.get("touchdown")) else 0
                wk = int(r.get("week") or 0)
                dt = r.get("defteam") or ""
                if flag(r.get("pass_attempt")) and r.get("receiver_player_id") and flag(r.get("complete_pass")):
                    o = r["receiver_player_id"]; L = longs[(o, season, wk)]
                    L["lr"] = max(L.get("lr", 0), yds)
                    rp = pos_group.get(o, "")
                    if dt and rp in ("WR", "TE", "RB"):
                        k = (dt, season, wk, rp); dmax[k] = max(dmax[k], yds)
                        if yds >= 20:
                            d20[(dt, rp)] += 1; d20g[(dt, season, wk, rp)] += 1
                if flag(r.get("rush_attempt")) and r.get("rusher_player_id"):
                    o = r["rusher_player_id"]; L = longs[(o, season, wk)]
                    L["lru"] = max(L.get("lru", 0), yds)
                    if dt and pos_group.get(o) == "RB":
                        k = (dt, season, wk, "RB_RUN"); dmax[k] = max(dmax[k], yds)
                if flag(r.get("pass_attempt")) and r.get("receiver_player_id"):
                    o, comp = r["receiver_player_id"], flag(r.get("complete_pass"))
                    grp = {"WR": "wr", "TE": "te", "RB": "rb"}.get(pos_group.get(o, ""))
                    for d in tk | pd | ({it} if it else set()):
                        a = pairs[o][d]; a[0] += 1; a[7] |= bit
                        if comp: a[1] += yds; a[2] += td
                        if d in pd: a[3] += 1
                        if d == it: a[4] += 1
                        if season == cur and grp:
                            p = prof[d][grp]; p[0] += 1
                            if comp and d in tk: p[1] += 1; p[2] += yds
                if flag(r.get("rush_attempt")) and r.get("rusher_player_id"):
                    o = r["rusher_player_id"]
                    for d in tk:
                        a = pairs[o][d]; a[0] += 1; a[1] += yds; a[7] |= bit
                        if season == cur:
                            p = prof[d]["run"]; p[0] += 1; p[1] += yds; p[2] += 1 if yds <= 0 else 0
                q = r.get("passer_player_id")
                if q and (flag(r.get("qb_dropback")) or flag(r.get("sack"))):
                    sk, qh = ids(SK), ids(QH)
                    for d in sk | qh | ({it} if it else set()):
                        a = pairs[q][d]; a[0] += 1; a[7] |= bit
                        if d in sk: a[5] += 1 if d == r.get("sack_player_id") else .5
                        if d in qh: a[6] += 1
                        if d == it: a[4] += 1
        except Exception as e:
            print(f"  (skipping {season} play-by-play: {e})")
    out = {}
    for o, ds in pairs.items():
        if o not in roster:
            continue
        keep = {d: [round(v, 1) if isinstance(v, float) else v for v in a] for d, a in ds.items()
                if d in roster and roster[d]["p"] in DEF_POS}
        if keep:
            out[o] = keep
    profiles = {d: v for d, v in prof.items() if d in roster}
    dlong = defaultdict(lambda: {"sum_lr": 0.0, "sum_lru": 0.0, "x20": 0})
    for (dt, _, _, rp), v in dmax.items():
        if rp == "RB_RUN": dlong[(dt, "RB")]["sum_lru"] += v
        else: dlong[(dt, rp)]["sum_lr"] += v
    for (dt, rp), n in d20.items():
        dlong[(dt, rp)]["x20"] = n
    return out, profiles, longs, dlong, d20g

def flag(v):
    return v in ("1", "TRUE", "True", "true", 1, True)

def season_for_today():
    t = date.today()
    return t.year if t.month >= 3 else t.year - 1

def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

# Position -> (season stat keys -> short output keys), and the single
# metric used for the per-game bar chart on that position's player page.
POS_STATS = {
    "QB": {"chart": "passing_yards", "fields": {
        "attempts": "att", "completions": "cmp", "passing_yards": "pyd",
        "passing_tds": "ptd", "passing_interceptions": "int",
        "passing_epa": "pepa", "rushing_yards": "ryd", "carries": "car"}},
    "RB": {"chart": "rushing_yards", "fields": {
        "carries": "car", "rushing_yards": "ryd", "rushing_tds": "rtd",
        "targets": "tgt", "receptions": "rec", "receiving_yards": "recyd"}},
    "WR": {"chart": "receiving_yards", "fields": {
        "targets": "tgt", "receptions": "rec", "receiving_yards": "recyd",
        "receiving_tds": "rectd", "target_share": "tshr"}},
    "TE": {"chart": "receiving_yards", "fields": {
        "targets": "tgt", "receptions": "rec", "receiving_yards": "recyd",
        "receiving_tds": "rectd", "target_share": "tshr"}},
    "DB": {"chart": "tackles", "fields": {}},
    "LB": {"chart": "tackles", "fields": {}},
    "DL": {"chart": "tackles", "fields": {}},
}
DEF_POS = {"DB", "LB", "DL"}

# How many prior full seasons to pull for "vs this opponent" history and
# for the real defense-allowed-by-position numbers. Keep this small —
# each extra season is another multi-MB download.
HIST_SEASONS_BACK = 2

def fetch_season_stats(season):
    """Fetch one season's weekly stats file; return [] if not published yet."""
    try:
        return fetch_csv(f"stats_player/stats_player_week_{season}.csv")
    except Exception as e:
        print(f"  (skipping {season}: {e})")
        return []

def build_scheme(season, roster):
    """
    Real play-level scheme and quality-of-play metrics, from nflverse's
    play-by-play (pbp) merged with FTN's play-charting data. FTN charts
    essentially every offensive snap (confirmed: ~97% of all plays, 100%
    of pass/dropback plays, for the 2026 season at build time).

    This does NOT include coverage shell (man/zone) or personnel/alignment
    groupings — FTN's free charting doesn't carry those fields. That gap
    is noted explicitly in the UI rather than guessed at.
    """
    print(f"Fetching play_by_play_{season}.csv.gz ...")
    pbp = fetch_csv_gz(f"pbp/play_by_play_{season}.csv.gz")
    print(f"Fetching ftn_charting_{season}.csv ...")
    ftn_rows = fetch_csv(f"ftn_charting/ftn_charting_{season}.csv")
    ftn = {(r["nflverse_game_id"], r["nflverse_play_id"]): r for r in ftn_rows}

    off = defaultdict(lambda: defaultdict(float))
    dfn = defaultdict(lambda: defaultdict(float))
    rz_off = defaultdict(lambda: defaultdict(set))
    rz_def = defaultdict(lambda: defaultdict(set))
    rec_g = defaultdict(lambda: defaultdict(float))
    qb_g = defaultdict(lambda: defaultdict(float))

    for r in pbp:
        if r.get("season_type") != "REG":
            continue
        pos, dft = r.get("posteam"), r.get("defteam")
        if not pos or not dft:
            continue
        k = (r["game_id"], r["play_id"])
        ch = ftn.get(k)

        if flag(r.get("qb_dropback")):
            off[pos]["dropbacks"] += 1
            dfn[dft]["dropbacks_faced"] += 1
            if flag(r.get("sack")) or flag(r.get("qb_hit")):
                off[pos]["pressured"] += 1
                dfn[dft]["pressures"] += 1

        fd = r.get("fixed_drive")
        if fd and num(r.get("yardline_100"), 100) <= 20:
            rz_off[pos].setdefault("trips", set()).add((r["game_id"], fd))
            rz_def[dft].setdefault("trips", set()).add((r["game_id"], fd))
            if r.get("fixed_drive_result") == "Touchdown":
                rz_off[pos].setdefault("tds", set()).add((r["game_id"], fd))
                rz_def[dft].setdefault("tds", set()).add((r["game_id"], fd))

        if not ch:
            continue

        off[pos]["charted"] += 1
        dfn[dft]["charted_faced"] += 1
        for src_flag, key in [("is_play_action", "pa"), ("is_rpo", "rpo"),
                               ("is_motion", "motion"), ("is_screen_pass", "screen"),
                               ("is_no_huddle", "no_huddle")]:
            if flag(ch.get(src_flag)):
                off[pos][key] += 1
                dfn[dft][key + "_faced"] += 1

        if flag(r.get("rush_attempt")):
            off[pos]["run_plays"] += 1
            off[pos]["box_sum"] += num(ch.get("n_defense_box"))
            dfn[dft]["run_plays_faced"] += 1
            dfn[dft]["box_sum_shown"] += num(ch.get("n_defense_box"))

        if flag(r.get("pass_attempt")) or flag(r.get("qb_dropback")):
            nb = num(ch.get("n_blitzers"))
            off[pos]["blitz_sum"] += nb
            off[pos]["pass_plays_c"] += 1
            dfn[dft]["blitz_sum_sent"] += nb
            dfn[dft]["pass_plays_c_faced"] += 1

        rid = r.get("receiver_player_id")
        if rid and flag(r.get("pass_attempt")):
            rec_g[rid]["targets"] += 1
            if flag(ch.get("is_catchable_ball")): rec_g[rid]["catchable"] += 1
            if flag(ch.get("is_contested_ball")): rec_g[rid]["contested"] += 1
            if flag(ch.get("is_drop")): rec_g[rid]["drops"] += 1

        pid = r.get("passer_player_id")
        if pid and (flag(r.get("pass_attempt")) or flag(r.get("sack"))):
            qb_g[pid]["dropbacks"] += 1
            if flag(ch.get("is_interception_worthy")): qb_g[pid]["int_worthy"] += 1
            if flag(ch.get("is_throw_away")): qb_g[pid]["throwaway"] += 1
            if flag(r.get("sack")) and flag(ch.get("is_qb_fault_sack")): qb_g[pid]["fault_sack"] += 1

    def pct(n, d):
        return round(n / d * 100, 1) if d else None

    def g(d, k):
        return d.get(k, 0) or 0

    team_tendencies = {}
    for team in set(list(off.keys()) + list(dfn.keys())):
        o, d = off.get(team, {}), dfn.get(team, {})
        rzo_trips, rzo_tds = len(rz_off.get(team, {}).get("trips", ())), len(rz_off.get(team, {}).get("tds", ()))
        rzd_trips, rzd_tds = len(rz_def.get(team, {}).get("trips", ())), len(rz_def.get(team, {}).get("tds", ()))
        team_tendencies[team] = {
            "off": {
                "plays": int(g(o, "charted")),
                "pa_pct": pct(g(o, "pa"), g(o, "charted")),
                "rpo_pct": pct(g(o, "rpo"), g(o, "charted")),
                "motion_pct": pct(g(o, "motion"), g(o, "charted")),
                "screen_pct": pct(g(o, "screen"), g(o, "charted")),
                "no_huddle_pct": pct(g(o, "no_huddle"), g(o, "charted")),
                "pressured_pct": pct(g(o, "pressured"), g(o, "dropbacks")),
                "avg_box_faced": round(g(o, "box_sum") / g(o, "run_plays"), 2) if g(o, "run_plays") else None,
                "avg_blitz_faced": round(g(o, "blitz_sum") / g(o, "pass_plays_c"), 2) if g(o, "pass_plays_c") else None,
                "rz_trips": rzo_trips,
                "rz_td_pct": pct(rzo_tds, rzo_trips),
            },
            "def": {
                "plays": int(g(d, "charted_faced")),
                "pa_pct": pct(g(d, "pa_faced"), g(d, "charted_faced")),
                "rpo_pct": pct(g(d, "rpo_faced"), g(d, "charted_faced")),
                "motion_pct": pct(g(d, "motion_faced"), g(d, "charted_faced")),
                "screen_pct": pct(g(d, "screen_faced"), g(d, "charted_faced")),
                "pressure_forced_pct": pct(g(d, "pressures"), g(d, "dropbacks_faced")),
                "avg_box_shown": round(g(d, "box_sum_shown") / g(d, "run_plays_faced"), 2) if g(d, "run_plays_faced") else None,
                "avg_blitz_sent": round(g(d, "blitz_sum_sent") / g(d, "pass_plays_c_faced"), 2) if g(d, "pass_plays_c_faced") else None,
                "rz_trips_allowed": rzd_trips,
                "rz_td_pct_allowed": pct(rzd_tds, rzd_trips),
            },
        }

    receiver_grades = {}
    for pid, g in rec_g.items():
        if pid not in roster or g.get("targets", 0) < 3:
            continue
        receiver_grades[pid] = {
            "targets": int(g["targets"]),
            "catchable_pct": pct(g.get("catchable", 0), g["targets"]),
            "contested_pct": pct(g.get("contested", 0), g["targets"]),
            "drop_pct": pct(g.get("drops", 0), g.get("catchable", 0) or g["targets"]),
        }

    qb_grades = {}
    for pid, g in qb_g.items():
        if pid not in roster or g.get("dropbacks", 0) < 5:
            continue
        qb_grades[pid] = {
            "dropbacks": int(g["dropbacks"]),
            "int_worthy_pct": pct(g.get("int_worthy", 0), g["dropbacks"]),
            "throwaway_pct": pct(g.get("throwaway", 0), g["dropbacks"]),
            "fault_sack_pct": pct(g.get("fault_sack", 0), g["dropbacks"]),
        }

    return {"team": team_tendencies, "receivers": receiver_grades, "qbs": qb_grades}


def build_ngs(season, roster):
    """Next Gen Stats season-to-date advanced metrics (free, nflverse-hosted,
    all seasons live in one combined file per stat type — filter to this
    season's week==0 rows, which nflverse publishes as the running season
    aggregate)."""
    out = {}
    for kind, fields in [
        ("receiving", {"avg_separation": "sep", "avg_cushion": "cush",
                        "avg_intended_air_yards": "aiy", "avg_yac_above_expectation": "yacoe"}),
        ("rushing", {"efficiency": "eff", "rush_yards_over_expected_per_att": "ryoe",
                      "percent_attempts_gte_eight_defenders": "stacked_box_pct"}),
        ("passing", {"avg_time_to_throw": "ttt", "aggressiveness": "agg",
                      "completion_percentage_above_expectation": "cpoe"}),
    ]:
        print(f"Fetching ngs_{kind}.csv.gz ...")
        rows = fetch_csv_gz(f"nextgen_stats/ngs_{kind}.csv.gz")
        # nflverse doesn't reliably publish a week==0 season-aggregate row this
        # early in a season, so average the per-week (week>0) rows ourselves.
        sums = defaultdict(lambda: defaultdict(float))
        counts = defaultdict(int)
        for r in rows:
            if r.get("season") != str(season) or r.get("week") == "0" or r.get("season_type") != "REG":
                continue
            pid = r.get("player_gsis_id")
            if not pid or pid not in roster:
                continue
            counts[pid] += 1
            for full in fields:
                v = r.get(full)
                if v not in (None, ""):
                    sums[pid][full] += num(v)
        for pid, n in counts.items():
            entry = out.setdefault(pid, {})
            for full, short in fields.items():
                if full in sums[pid]:
                    entry[short] = round(sums[pid][full] / n, 2)
    return out


TEAM_FIELDS = {
    # output key -> source columns summed from every player row for that team-week
    "att": ["attempts"], "cmp": ["completions"], "pyd": ["passing_yards"],
    "ptd": ["passing_tds"], "int": ["passing_interceptions"], "sk": ["sacks_suffered"],
    "car": ["carries"], "ryd": ["rushing_yards"], "rtd": ["rushing_tds"],
    "pepa": ["passing_epa"], "repa": ["rushing_epa"],
    "fl": ["rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost"],
    "fd": ["passing_first_downs", "rushing_first_downs"],
}

def build_team_stats(stat_rows, games):
    """
    Team-level offense and defense from the same weekly player file, grouped by
    each row's own `team` (offense) and `opponent_team` (defense = what the
    opponent's offense did). Every row counts, whether or not the player is on a
    current roster. Points, records, against-the-spread and over/under come from
    the schedule's final scores and closing lines.
    """
    off = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))  # team -> week -> key
    for row in stat_rows:
        if row["season_type"] != "REG" or not row.get("team") or not row.get("opponent_team"):
            continue
        wk = int(row["week"])
        cell = off[row["team"]][wk]
        cell["opp"] = row["opponent_team"]
        for key, cols in TEAM_FIELDS.items():
            cell[key] += sum(num(row.get(c)) for c in cols)

    def summarize(weeks):
        n = len(weeks)
        if not n:
            return None
        t = defaultdict(float)
        for c in weeks:
            for k in TEAM_FIELDS:
                t[k] += c.get(k, 0)
        plays = t["att"] + t["sk"] + t["car"]
        dropbacks = t["att"] + t["sk"]
        r1 = lambda v: round(v, 1)
        return {
            "ypg": r1((t["pyd"] + t["ryd"]) / n), "pypg": r1(t["pyd"] / n), "rypg": r1(t["ryd"] / n),
            "ppg_plays": r1(plays / n),
            "epa": round((t["pepa"] + t["repa"]) / plays, 3) if plays else None,
            "pepa": round(t["pepa"] / dropbacks, 3) if dropbacks else None,
            "repa": round(t["repa"] / t["car"], 3) if t["car"] else None,
            "ypa": r1(t["pyd"] / t["att"]) if t["att"] else None,
            "ypc": r1(t["ryd"] / t["car"]) if t["car"] else None,
            "cmp_pct": r1(t["cmp"] / t["att"] * 100) if t["att"] else None,
            "pass_rate": r1(dropbacks / plays * 100) if plays else None,
            "tdpg": round((t["ptd"] + t["rtd"]) / n, 2),
            "topg": round((t["int"] + t["fl"]) / n, 2),
            "skpg": round(t["sk"] / n, 2),
            "fdpg": r1(t["fd"] / n),
        }

    finals = [g for g in games if g["hs"] is not None and g["as"] is not None]
    out = {}
    for team in sorted(off):
        my_weeks = off[team]
        # Defense = every opponent's offensive line in the week they played this team.
        allowed = [off[o][w] for o in off for w, c in off[o].items() if c.get("opp") == team]
        rec = {"w": 0, "l": 0, "t": 0, "pf": 0, "pa": 0, "ats_w": 0, "ats_l": 0, "ats_p": 0,
               "ov": 0, "un": 0, "push": 0}
        log = []
        for g in sorted(finals, key=lambda g: g["wk"]):
            if team not in (g["home"], g["away"]):
                continue
            home = g["home"] == team
            pf, pa = (g["hs"], g["as"]) if home else (g["as"], g["hs"])
            rec["pf"] += pf; rec["pa"] += pa
            rec["w" if pf > pa else "l" if pf < pa else "t"] += 1
            if g["spread"] is not None:
                # nflverse spread_line = expected HOME margin (positive = home favored)
                margin_vs_line = (g["hs"] - g["as"] - g["spread"]) * (1 if home else -1)
                rec["ats_w" if margin_vs_line > 0 else "ats_l" if margin_vs_line < 0 else "ats_p"] += 1
            if g["total"] is not None:
                tot = g["hs"] + g["as"]
                rec["ov" if tot > g["total"] else "un" if tot < g["total"] else "push"] += 1
            wc = my_weeks.get(g["wk"], {})
            oc = off.get(g["home"] if not home else g["away"], {}).get(g["wk"], {})
            log.append({"wk": g["wk"], "opp": g["away"] if home else g["home"], "home": home,
                        "pf": pf, "pa": pa,
                        "yds": round(wc.get("pyd", 0) + wc.get("ryd", 0)),
                        "ydsa": round(oc.get("pyd", 0) + oc.get("ryd", 0))})
        n = len(log)
        out[team] = {
            "g": n, **rec,
            "pfpg": round(rec["pf"] / n, 1) if n else None,
            "papg": round(rec["pa"] / n, 1) if n else None,
            "off": summarize(list(my_weeks.values())),
            "def": summarize(allowed),
            "log": log,
        }
    return out


# Per position, the stats kept for every player-game in the allowed log.
# (source column, short key); None = longest play from play-by-play.
ALLOWED_VALS = {
    "QB": [("attempts", "att"), ("completions", "cmp"), ("passing_yards", "pyd"),
           ("passing_tds", "ptd"), ("passing_interceptions", "int"), ("rushing_yards", "ryd")],
    "RB": [("carries", "car"), ("rushing_yards", "ryd"), (None, "lru"), ("rushing_tds", "rtd"),
           ("targets", "tgt"), ("receptions", "rec"), ("receiving_yards", "recyd"), (None, "lr")],
    "WR": [("targets", "tgt"), ("receptions", "rec"), ("receiving_yards", "recyd"), (None, "lr"),
           ("receiving_tds", "rectd")],
}
ALLOWED_VALS["TE"] = ALLOWED_VALS["WR"]
for _p in ("QB", "RB", "WR"):
    ALLOWED_VALS[_p] = ALLOWED_VALS[_p] + [("fantasy_points_ppr", "fp")]
ALLOWED_VALS["TE"] = ALLOWED_VALS["WR"]

def build_allowed_log(all_season_rows, longs, d20g, game_rows, seasons):
    """
    Every game every defense has played since the first history season, with each
    opposing QB/RB/WR/TE's line in that game. This is what sits behind every
    "defense allows X per game" number, so the site can list the exact players.
    Includes players who are no longer on a roster (names kept separately).
    Weather and home/away come from the schedule.
    """
    wx = {}
    for g in game_rows:
        if int(g["season"]) not in seasons or g["game_type"] != "REG":
            continue
        w = [int(float(g["temp"])) if g["temp"] else None, int(float(g["wind"])) if g["wind"] else None, g["roof"] or None]
        for t, home in ((g["home_team"], 1), (g["away_team"], 0)):
            wx[(int(g["season"]), int(g["week"]), t)] = (w, home)
    teams, names = defaultdict(dict), {}
    for szn, row in all_season_rows:
        if row["season_type"] != "REG":
            continue
        dt, ot, pos = row.get("opponent_team"), row.get("team"), row.get("position_group") or ""
        if not dt or not ot:
            continue
        wk = int(row["week"]); key = (szn, wk)
        if key not in teams[dt]:
            w, home = wx.get((szn, wk, dt), ([None, None, None], None))
            teams[dt][key] = {"opp": ot, "wx": w, "h": home, "p": []}
        if pos not in ALLOWED_VALS:
            continue
        pid = row["player_id"]
        vals = []
        for col, short in ALLOWED_VALS[pos]:
            v = num(row.get(col)) if col else longs.get((pid, szn, wk), {}).get(short, 0)
            vals.append(int(v) if float(v).is_integer() else round(v, 1))
        if not any(vals):
            continue
        names[pid] = row.get("player_display_name") or row.get("player_name") or pid
        teams[dt][key]["p"].append([pid, pos] + vals)
    out = {}
    for dt, games in teams.items():
        rows = []
        for (szn, wk), g in sorted(games.items()):
            x20 = {p: d20g[(dt, szn, wk, p)] for p in ("WR", "TE", "RB") if d20g.get((dt, szn, wk, p))}
            rows.append([szn, wk, g["opp"], g["h"], g["wx"], x20, g["p"]])
        out[dt] = rows
    return {"seasons": seasons, "names": names,
            "keys": {p: [k for _, k in v] for p, v in ALLOWED_VALS.items()}, "teams": out}



def build_script_model(all_season_rows, game_rows, seasons, season):
    """
    Game script from the betting market: how a team's pass rate moves with the
    pregame spread. Every regular-season game since the first history season.
    Within each team-season, compare how much more or less a team was favored
    than usual with how much more or less it passed than usual, so team
    identity (some teams always pass more) does not leak into the slope.
    Pass rate = dropbacks (attempts + sacks) / (dropbacks + carries).
    """
    tg = defaultdict(lambda: [0.0, 0.0])     # (season, week, team) -> [dropbacks, carries]
    for szn, row in all_season_rows:
        if row["season_type"] != "REG" or not row.get("team"):
            continue
        c = tg[(szn, int(row["week"]), row["team"])]
        c[0] += num(row.get("attempts")) + num(row.get("sacks_suffered"))
        c[1] += num(row.get("carries"))
    fav = {}
    for g in game_rows:
        if g["game_type"] != "REG" or not g["spread_line"] or int(g["season"]) not in seasons:
            continue
        s, w, sp = int(g["season"]), int(g["week"]), float(g["spread_line"])
        fav[(s, w, g["home_team"])] = sp       # nflverse spread_line > 0 = home favored
        fav[(s, w, g["away_team"])] = -sp
    by_ts = defaultdict(list)
    for key, (db, car) in tg.items():
        if key in fav and db + car >= 30:
            by_ts[(key[0], key[2])].append((fav[key], db / (db + car)))
    sxy = sxx = syy = 0.0; n = 0
    teams = {}
    for (szn, team), pts in by_ts.items():
        mf = sum(p[0] for p in pts) / len(pts); mr = sum(p[1] for p in pts) / len(pts)
        for f, r in pts:
            x, y = f - mf, r - mr
            sxy += x * y; sxx += x * x; syy += y * y; n += 1
        if szn == season:
            teams[team] = {"pr": round(mr, 4), "fav": round(mf, 2), "g": len(pts)}
    slope = sxy / sxx if sxx else 0.0
    r = sxy / (sxx * syy) ** .5 if sxx and syy else 0.0
    return {"b": round(slope, 5), "r": round(r, 3), "n": n, "teams": teams}

def build(season):
    print(f"Fetching roster_{season}.csv ...")
    roster_rows = fetch_csv(f"rosters/roster_{season}.csv")
    print(f"Fetching stats_player_week_{season}.csv ...")
    stat_rows = fetch_csv(f"stats_player/stats_player_week_{season}.csv")
    print(f"Fetching snap_counts_{season}.csv ...")
    snap_rows = fetch_csv(f"snap_counts/snap_counts_{season}.csv")
    print("Fetching games.csv (full schedule + closing lines) ...")
    game_rows = fetch_csv("schedules/games.csv")

    hist_seasons = list(range(season - HIST_SEASONS_BACK, season))
    hist_stat_rows = {}
    for hs in hist_seasons:
        print(f"Fetching stats_player_week_{hs}.csv (history) ...")
        hist_stat_rows[hs] = fetch_season_stats(hs)

    roster = {}
    for row in roster_rows:
        if row["status"] != "ACT" or not row["gsis_id"]:
            continue
        roster[row["gsis_id"]] = {
            "id": row["gsis_id"], "n": row["full_name"], "t": row["team"],
            "p": row["position"], "j": row["jersey_number"] or None,
            "col": row["college"] or None, "x": int(row["years_exp"] or 0),
            # granular position for defenders (CB/FS/SS/ILB/OLB/DE/DT/NT); shown and used
            # to compare a corner against corners rather than all defensive backs
            "pp": row.get("depth_chart_position") or row["position"],
        }

    agg = defaultdict(lambda: defaultdict(float))
    games_played = defaultdict(int)
    weekly = defaultdict(list)
    for row in stat_rows:
        if row["season_type"] != "REG":
            continue
        pid = row["player_id"]
        pos = roster.get(pid, {}).get("p")
        games_played[pid] += 1
        tkl = num(row.get("def_tackles_solo")) + num(row.get("def_tackles_with_assist"))
        agg[pid]["tackles"] += tkl
        agg[pid]["sacks"] = agg[pid].get("sacks", 0) + num(row.get("def_sacks"))
        agg[pid]["def_int"] = agg[pid].get("def_int", 0) + num(row.get("def_interceptions"))
        agg[pid]["pd"] = agg[pid].get("pd", 0) + num(row.get("def_pass_defended"))
        agg[pid]["fp"] = agg[pid].get("fp", 0) + num(row.get("fantasy_points_ppr"))
        wk_entry = {"w": int(row["week"]), "opp": row["opponent_team"], "tkl": tkl,
                    "fp": num(row.get("fantasy_points_ppr"))}
        if pos in DEF_POS:
            dl = def_line(row)
            for k in ("sacks", "int", "pd", "qbh", "tfl", "ff"):
                agg[pid]["d_" + k] += dl[k]
                wk_entry["d_" + k] = dl[k]
        spec = POS_STATS.get(pos)
        if spec:
            for k in spec["fields"]:
                v = num(row.get(k))
                agg[pid][k] += v
                wk_entry[k] = v
        # keep the chart-relevant raw fields for every player regardless of spec, for weekly display
        for k in ("passing_yards", "rushing_yards", "receiving_yards"):
            wk_entry.setdefault(k, num(row.get(k)))
        weekly[pid].append(wk_entry)

    snaps_by_key = defaultdict(list)
    for row in snap_rows:
        key = (row["player"].strip(), row["team"].strip())
        snaps_by_key[key].append(num(row.get("offense_pct")) + num(row.get("defense_pct")))

    players = []
    for pid, r in roster.items():
        entry = dict(r)
        g = games_played.get(pid, 0)
        entry["g"] = g
        sk = snaps_by_key.get((r["n"], r["t"]))
        entry["sn"] = round(sum(sk) / len(sk) * 100, 1) if sk else None
        if g > 0:
            a = agg[pid]
            spec = POS_STATS.get(r["p"])
            szn = {}
            if spec:
                for full, short in spec["fields"].items():
                    szn[short] = round(a.get(full, 0) / g, 2)
            if r["p"] in DEF_POS:
                szn["tkl"] = round(a["tackles"] / g, 1)
                szn["sacks"] = round(a.get("sacks", 0), 1)
                szn["int"] = int(a.get("def_int", 0))
                szn["pd"] = int(a.get("pd", 0))
                szn["qbh"] = int(a.get("d_qbh", 0))
                szn["tfl"] = round(a.get("d_tfl", 0), 1)
                szn["ff"] = int(a.get("d_ff", 0))
            szn["fp"] = round(a.get("fp", 0) / g, 1)
            entry["ss"] = szn
            chart_key = spec["chart"] if spec else "tkl"
            wk_field = {"passing_yards": "passing_yards", "rushing_yards": "rushing_yards",
                        "receiving_yards": "receiving_yards", "tackles": "tkl"}.get(chart_key, "tkl")
            wk_list = []
            for w in sorted(weekly[pid], key=lambda x: x["w"]):
                line = {"w": w["w"], "opp": w["opp"], "tkl": round(w.get("tkl", 0), 1),
                        "fp": round(w.get("fp", 0), 1)}
                if spec:
                    for full, short in spec["fields"].items():
                        if full in w:
                            line[short] = round(w[full], 1)
                for k in ("sacks", "int", "pd", "qbh", "tfl"):
                    if "d_" + k in w:
                        line[k] = round(w["d_" + k], 1)
                line["v"] = round(w.get(wk_field, w.get("tkl", 0)), 1)
                wk_list.append(line)
            entry["wk"] = wk_list
        else:
            entry["ss"] = None
            entry["wk"] = []
        players.append(entry)

    players.sort(key=lambda p: (p["t"], p["p"], -(p.get("ss") or {}).get("fp", 0) if p.get("ss") else 0))

    # --- Multi-season "vs this opponent" history, and real defense-allowed-by-position ---
    # Each stat row is attributed using ITS OWN position, never the current roster.
    # Joining to the current roster used to (a) silently drop every row for a player who
    # is no longer on an active roster (retired, cut, practice squad, IR), and
    # (b) misfile anyone who changed position. That made e.g. Dallas's pass defense look
    # top-3 when it was really ~21st. `opponent_team` is already per-row, so it is correct
    # regardless of which team the player is on now.
    #
    # Note the column: the stats files' `position` is granular (CB, SAF, DT, FB, ...), while
    # the roster/app vocabulary is grouped (DB, DL, RB, ...). `position_group` is the column
    # that matches, so use it — with raw `position`, fullbacks would vanish from RB numbers.
    all_season_rows = [(season, r) for r in stat_rows] + \
                       [(hs, r) for hs in hist_seasons for r in hist_stat_rows.get(hs, [])]

    player_hist = defaultdict(list)
    season_split = defaultdict(dict)      # pid -> prior season -> totals (full seasons, no windows)
    def_games = defaultdict(set)          # team -> set of (season, week) it played defense
    def_agg = defaultdict(lambda: defaultdict(float))  # (team,pos) -> field -> total

    for szn, row in all_season_rows:
        if row["season_type"] != "REG":
            continue
        pid = row["player_id"]
        opp = row["opponent_team"]
        if not opp:
            continue
        wk = int(row["week"])

        # Defense-allowed: every row counts, using the row's own position group.
        row_pos = row.get("position_group") or ""
        row_spec = POS_STATS.get(row_pos)
        if row_spec and row_spec["fields"]:
            def_games[opp].add((szn, wk))
            for full in row_spec["fields"]:
                def_agg[(opp, row_pos)][full] += num(row.get(full))
            if row_pos in ("QB", "RB", "WR", "TE"):
                def_agg[(opp, row_pos)]["fantasy_points_ppr"] += num(row.get("fantasy_points_ppr"))

        # Per-player history is only shown for players on a current roster, with columns
        # matching their CURRENT position so the table lines up on their page.
        r = roster.get(pid)
        if not r:
            continue
        spec = POS_STATS.get(r["p"])
        if not spec:
            continue
        line = {"s": szn, "w": wk, "opp": opp, "fp": round(num(row.get("fantasy_points_ppr")), 1)}
        for full, short in spec["fields"].items():
            line[short] = round(num(row.get(full)), 1)
        if r["p"] in DEF_POS:
            # defenders: full season splits only (below); per-game history would
            # add ~2MB to the page for little use
            for k, v in def_line(row).items():
                line[k] = round(v, 1)
        else:
            player_hist[pid].append(line)
        if szn != season:
            sp = season_split[pid].setdefault(szn, {"g": 0, "teams": defaultdict(int), "sum": defaultdict(float)})
            sp["g"] += 1
            sp["teams"][row.get("team") or ""] += 1
            for k, v in line.items():
                if k not in ("s", "w", "opp"):
                    sp["sum"][k] += v

    for pid, rows in player_hist.items():
        rows.sort(key=lambda x: (x["s"], x["w"]))
    RATE_KEEP_TOTAL = {"sacks", "int", "pd", "qbh", "tfl", "ff"}
    for p in players:
        p["hist"] = player_hist.get(p["id"], [])
        sz = []
        for szn, sp in sorted(season_split.get(p["id"], {}).items()):
            g = sp["g"]
            row = {"s": szn, "g": g, "t": max(sp["teams"], key=sp["teams"].get)}
            for k, v in sp["sum"].items():
                if p["p"] in DEF_POS and k in RATE_KEEP_TOTAL:
                    row[k] = round(v, 1)          # defensive splash plays: season totals
                else:
                    row[k] = round(v / g, 2)      # everything else: per game
            sz.append(row)
        p["sz"] = sz

    defense_allowed = {}
    for (team, pos), sums in def_agg.items():
        g = len(def_games.get(team, ())) or 1
        spec = POS_STATS[pos]
        row_out = {short: round(sums.get(full, 0) / g, 2) for full, short in spec["fields"].items()}
        row_out["g"] = len(def_games.get(team, ()))
        if pos in ("QB", "RB", "WR", "TE"):
            row_out["fp"] = round(sums.get("fantasy_points_ppr", 0) / g, 2)   # PPR points allowed per game
        defense_allowed.setdefault(team, {})[pos] = row_out

    games = []
    past_games = []   # prior seasons, for team head-to-head history
    for row in game_rows:
        if int(row["season"]) in hist_seasons and row["away_score"] and row["home_score"]:
            past_games.append({
                "s": int(row["season"]), "wk": int(row["week"]), "gt": row["game_type"],
                "away": row["away_team"], "home": row["home_team"],
                "as": int(row["away_score"]), "hs": int(row["home_score"]),
            })
        if int(row["season"]) != season:
            continue
        games.append({
            "id": row["game_id"], "wk": int(row["week"]), "date": row["gameday"],
            "away": row["away_team"], "home": row["home_team"],
            "as": int(row["away_score"]) if row["away_score"] else None,
            "hs": int(row["home_score"]) if row["home_score"] else None,
            "spread": num(row["spread_line"], None) if row["spread_line"] else None,
            "total": num(row["total_line"], None) if row["total_line"] else None,
            "aml": int(row["away_moneyline"]) if row["away_moneyline"] else None,
            "hml": int(row["home_moneyline"]) if row["home_moneyline"] else None,
            # The full season is published in advance, so every future game is here
            # with its kickoff (ET), venue and rest. Playoff games appear once scheduled.
            "gt": row["game_type"], "time": row["gametime"] or None,
            "loc": row["location"] or None, "stad": row["stadium"] or None,
            "roof": row["roof"] or None, "surf": row["surface"] or None,
            "ar": int(row["away_rest"]) if row["away_rest"] else None,
            "hr": int(row["home_rest"]) if row["home_rest"] else None,
            "div": row["div_game"] == "1",
            "aqb": row["away_qb_name"] or None, "hqb": row["home_qb_name"] or None,
        })
    games.sort(key=lambda g: (g["wk"], g["date"]))

    team_stats = build_team_stats(stat_rows, games)

    scheme = build_scheme(season, roster)
    for p in players:
        p["grade"] = scheme["receivers"].get(p["id"]) or scheme["qbs"].get(p["id"])

    ngs = build_ngs(season, roster)
    for p in players:
        if ngs.get(p["id"]):
            p["ngs"] = ngs[p["id"]]

    # Player-vs-defender history (all seasons) + defender involvement profiles (this season)
    pos_group = {}
    for _, row in all_season_rows:
        if row.get("position_group"):
            pos_group[row["player_id"]] = row["position_group"]
    h2h, dprof, longs, dlong, d20g = build_h2h(hist_seasons + [season], roster, pos_group)
    allowed_log = build_allowed_log(all_season_rows, longs, d20g, game_rows, hist_seasons + [season])
    script = build_script_model(all_season_rows, game_rows, hist_seasons + [season], season)
    # Game-state layer (situational usage, outcome samples, location/depth, penalties, coverage)
    try:
        from build_script import build_script
        snap_games = defaultdict(set)      # (name, team) -> games he played offensive snaps in
        for row in snap_rows:
            if num(row.get("offense_snaps")) > 0:
                snap_games[(row["player"].strip(), row["team"].strip())].add(row["game_id"])
        states = build_script(fetch_bytes, hist_seasons + [season], season, roster, pos_group,
                              drop_raw=lambda p: _RAW.pop(p, None), snap_games=snap_games)
    except Exception as e:
        print(f"  (game-state layer skipped: {e})")
        states = None
    # Free weekly injury reports: each player's latest designation this season
    try:
        inj = {}
        for row in fetch_csv(f"injuries/injuries_{season}.csv"):
            if row.get("season_type") != "REG" or not row.get("gsis_id"):
                continue
            wk = int(row.get("week") or 0)
            if row["gsis_id"] not in inj or wk >= inj[row["gsis_id"]]["wk"]:
                inj[row["gsis_id"]] = {"wk": wk, "st": row.get("report_status") or "",
                                       "i": row.get("report_primary_injury") or row.get("practice_primary_injury") or "",
                                       "pr": row.get("practice_status") or ""}
        for p in players:
            if p["id"] in inj:
                p["inj"] = inj[p["id"]]
        print(f"  injury reports: {len(inj)} players")
    except Exception as e:
        print(f"  (injury reports unavailable: {e})")
    # attach longest reception / run to every game line we show
    for p in players:
        for line in p["hist"]:
            line.update({k: round(v) for k, v in longs.get((p["id"], line["s"], line["w"]), {}).items()})
        for line in p["wk"]:
            line.update({k: round(v) for k, v in longs.get((p["id"], season, line["w"]), {}).items()})
    # ...and what each defense allows: average longest catch per game, 20+ yd catches per game
    for team, by_pos in defense_allowed.items():
        for pos, row in by_pos.items():
            dl, g = dlong.get((team, pos)), row.get("g") or 0
            if dl and g:
                row["lr"] = round(dl["sum_lr"] / g, 1)
                row["x20"] = round(dl["x20"] / g, 2)
                if pos == "RB":
                    row["lru"] = round(dl["sum_lru"] / g, 1)
    for p in players:
        if p["id"] in dprof:
            p["dv"] = dprof[p["id"]]

    teams_meta = build_team_meta({g["home"] for g in games} | {g["away"] for g in games})

    out = {"season": season, "hist_seasons": hist_seasons, "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "players": players, "games": games, "past_games": past_games,
           "defense_allowed": defense_allowed, "team_scheme": scheme["team"],
           "team_stats": team_stats, "teams": teams_meta, "script": script}
    return out, h2h, allowed_log, states

if __name__ == "__main__":
    season = int(sys.argv[1]) if len(sys.argv) > 1 else season_for_today()
    data, h2h, allowed_log, states = build(season)
    with open("nfl_data.json", "w") as f:
        json.dump(data, f, separators=(",", ":"))
    # Head-to-head plays live in their own file; the site loads it only when a
    # player-vs-defense view is opened, so the first page load stays light.
    with open("nfl_h2h.json", "w") as f:
        json.dump({"season": season, "seasons": data["hist_seasons"] + [season], "pairs": h2h}, f, separators=(",", ":"))
    print(f"Wrote nfl_data.json — {len(data['players'])} players, {len(data['games'])} games, season {season}")
    print(f"Wrote nfl_h2h.json — {sum(len(v) for v in h2h.values())} player-vs-defender pairs")
    # Every game behind every "defense allows" number, loaded only by the drill-down pages.
    with open("nfl_allowed.json", "w") as f:
        json.dump(allowed_log, f, separators=(",", ":"))
    print(f"Wrote nfl_allowed.json — {sum(len(v) for v in allowed_log['teams'].values())} defense-games, {len(allowed_log['names'])} players")
    if states:
        with open("nfl_script.json", "w") as f:
            json.dump(states, f, separators=(",", ":"))
        print(f"Wrote nfl_script.json — {len(states['players'])} players, {len(states['bank'])} team-games of game-state history")
