"""
Game-state layer for Game Script: everything the simulator and the situational
views need, from free nflverse play-by-play (and play participation for coverage).

Writes nfl_script.json, loaded by the site only on the pages that need it.

  teams[T]      pass rate by score state, plays and targets per game, throws by
                location/depth (offense and defense), penalties, coverage mix
  players[pid]  target/carry share by score state, outcome samples per target /
                carry / attempt (for simulation), depth and location profile,
                red-zone share, vs man/zone splits, pass interference drawn
  bank          [final margin, share of plays in each score state] for every
                team-game since the first history season (how games unfold)
  league        league pass rate by state and per-target yards baselines

Score states (from the offense's view, before the snap):
  0 trailing by 9+ · 1 trailing by 1-8 · 2 tied · 3 leading by 1-8 · 4 leading by 9+
"""
import csv, io, gzip
from collections import defaultdict

STATES = 5
def state_of(diff):
    return 0 if diff <= -9 else 1 if diff < 0 else 2 if diff == 0 else 3 if diff <= 8 else 4

def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d

def _flag(x):
    return x in ("1", "1.0", "TRUE", "True", "true")

SAMPLE_N = {"tgt": 220, "car": 260, "att": 450, "scr": 80}

def build_script(fetch_bytes, seasons, cur, roster, pos_group, drop_raw=None, snap_games=None):
    """
    fetch_bytes(path) -> bytes of an nflverse release asset.
    roster: {gsis_id: {"t": team, "p": pos, ...}} for current active players.
    pos_group: {gsis_id: position group} from weekly stats (covers former players).
    """
    # --- coverage from play participation (only published after a season) ---
    cov = {}
    cov_seasons = []
    for s in sorted(seasons):
        if s == cur:
            continue
        path = f"pbp_participation/pbp_participation_{s}.csv"
        try:
            raw = fetch_bytes(path)
        except Exception as e:
            print(f"  (no participation file for {s}: {e})")
            continue
        n = 0
        for r in csv.DictReader(io.TextIOWrapper(io.BytesIO(raw), "utf-8", errors="replace")):
            mz, ct = r.get("defense_man_zone_type") or "", r.get("defense_coverage_type") or ""
            if mz or ct:
                cov[(r["nflverse_game_id"], r["play_id"])] = ("M" if mz.startswith("MAN") else "Z" if mz.startswith("ZONE") else "", ct)
                n += 1
        if drop_raw:
            drop_raw(path)
        cov_seasons.append(s)
        print(f"  coverage tags for {s}: {n:,} plays")

    # --- accumulators ---
    T = lambda: defaultdict(float)
    team_state = defaultdict(lambda: {s: [[0, 0] for _ in range(STATES)] for s in seasons})  # [plays, dropbacks]
    team_games = defaultdict(lambda: defaultdict(lambda: {"plays": 0, "db": 0, "tgt": 0, "car": 0, "att": 0, "st": [0] * STATES, "opp": "", "pts": 0}))
    game_final = {}                                   # game_id -> (home, away, home_pts, away_pts)
    loc_off = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))   # (team, season) -> zone -> [tgt, comp, yds]
    loc_def = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))
    loc_lg = defaultdict(lambda: defaultdict(lambda: [0, 0, 0.0]))    # season -> zone -> ...
    airy = defaultdict(lambda: [0, 0.0])               # (team, season) -> [att with air yards, sum air]
    pen = defaultdict(lambda: defaultdict(float))      # (team, season) -> {"off_n","off_y","def_n","def_y","games"}
    pen_types = defaultdict(lambda: defaultdict(int))  # (team, season, side) -> type -> n
    covmix = defaultdict(lambda: defaultdict(int))     # (defteam, season) -> "M"/"Z"/shell -> n
    # players
    P = lambda: {"tgt": [0] * STATES, "car": [0] * STATES, "rz_t": 0, "rz_c": 0,
                 "deep": 0, "airs": 0.0, "airn": 0, "loc": [0, 0, 0],
                 "ty": [], "cy": [], "rec_td": 0, "rush_td": 0, "tg": 0, "cr": 0,
                 "att": 0, "ay": [], "ptd": 0, "pint": 0, "db": 0, "sk": 0, "scr": 0, "sy": [],
                 "vs": {"M": [0, 0, 0.0], "Z": [0, 0, 0.0]}, "dpi_n": 0, "dpi_y": 0.0,
                 "games": set(), "gt": {}}
    pl = defaultdict(lambda: defaultdict(P))           # season -> pid -> stats
    team_state_tgt = defaultdict(lambda: [0] * STATES) # (team, season, game_id) handled below
    tg_state = defaultdict(lambda: defaultdict(lambda: [[0] * STATES, [0] * STATES]))  # season -> (team, game) -> [targets by state, carries by state]

    for s in sorted(seasons):
        path = f"pbp/play_by_play_{s}.csv.gz"
        print(f"Scanning {path} for game states ...")
        try:
            gz = gzip.GzipFile(fileobj=io.BytesIO(fetch_bytes(path)))
            rows = csv.DictReader(io.TextIOWrapper(gz, "utf-8", errors="replace"))
        except Exception as e:
            print(f"  (skipping {s}: {e})")
            continue
        for r in rows:
            if r.get("season_type") != "REG":
                continue
            gid, off, de = r.get("game_id") or "", r.get("posteam") or "", r.get("defteam") or ""
            h, a = r.get("home_team"), r.get("away_team")
            if gid:
                game_final[gid] = (h, a, _f(r.get("total_home_score")), _f(r.get("total_away_score")), s)
            # penalties (accepted)
            if _flag(r.get("penalty")) and r.get("penalty_team"):
                pt, ty, py = r["penalty_team"], r.get("penalty_type") or "Other", _f(r.get("penalty_yards"))
                side = "off" if pt == off else "def"
                pen[(pt, s)][side + "_n"] += 1; pen[(pt, s)][side + "_y"] += py
                pen_types[(pt, s, side)][ty] += 1
                other = de if pt == off else off
                if other:
                    pen[(other, s)]["drawn_" + side] += 1
                if ty == "Defensive Pass Interference" and r.get("receiver_player_id"):
                    q = pl[s][r["receiver_player_id"]]; q["dpi_n"] += 1; q["dpi_y"] += py
            if not off or _flag(r.get("two_point_attempt")):
                continue
            ptype = r.get("play_type") or ""
            if ptype not in ("pass", "run"):
                continue
            st = state_of(int(_f(r.get("score_differential"))))
            g = team_games[(off, s)][gid]
            g["opp"] = de
            db = _flag(r.get("qb_dropback"))
            scr = _flag(r.get("qb_scramble"))
            g["plays"] += 1; g["st"][st] += 1
            ts = team_state[off][s][st]; ts[0] += 1
            if db:
                g["db"] += 1; ts[1] += 1
            rz = _f(r.get("yardline_100"), 99) <= 20
            # dropback detail for the passer
            qb = r.get("passer_player_id") or (r.get("rusher_player_id") if scr else "")
            if db and qb:
                Q = pl[s][qb]; Q["db"] += 1; Q["games"].add(gid)
                if _flag(r.get("sack")):
                    Q["sk"] += 1
                if scr:
                    Q["scr"] += 1; Q["sy"].append(int(_f(r.get("rushing_yards") or r.get("yards_gained"))))
            # targets
            rec = r.get("receiver_player_id")
            if _flag(r.get("pass_attempt")) and not _flag(r.get("sack")):
                passer = r.get("passer_player_id")
                comp = _flag(r.get("complete_pass"))
                yds = _f(r.get("receiving_yards") or r.get("yards_gained")) if comp else 0.0
                if passer:
                    Q = pl[s][passer]; Q["att"] += 1
                    Q["ay"].append(int(_f(r.get("passing_yards"))) if comp else -99)
                    if _flag(r.get("pass_touchdown")): Q["ptd"] += 1
                    if _flag(r.get("interception")): Q["pint"] += 1
                    g["att"] += 1
                if rec:
                    g["tgt"] += 1
                    tg_state[s][(off, gid)][0][st] += 1
                    R = pl[s][rec]; R["tgt"][st] += 1; R["tg"] += 1; R["games"].add(gid); R["gt"][gid] = off
                    R["ty"].append(int(yds) if comp else None)
                    if comp and _flag(r.get("pass_touchdown")): R["rec_td"] += 1
                    if rz: R["rz_t"] += 1
                    ay = r.get("air_yards")
                    if ay not in (None, ""):
                        R["airs"] += _f(ay); R["airn"] += 1
                        airy[(off, s)][0] += 1; airy[(off, s)][1] += _f(ay)
                    ln, ll = r.get("pass_length") or "", r.get("pass_location") or ""
                    if ln == "deep": R["deep"] += 1
                    li = {"left": 0, "middle": 1, "right": 2}.get(ll)
                    if li is not None:
                        R["loc"][li] += 1
                    if ln and ll:
                        z = ll[0] + ln[0]           # ls, md, rd ...
                        for D in (loc_off[(off, s)][z], loc_def[(de, s)][z], loc_lg[s][z]):
                            D[0] += 1; D[1] += 1 if comp else 0; D[2] += yds
                    c = cov.get((gid, r.get("play_id")))
                    if c and c[0]:
                        v = R["vs"][c[0]]; v[0] += 1; v[1] += 1 if comp else 0; v[2] += yds
            # carries (designed runs; scrambles stay with the dropback)
            if ptype == "run" and not scr and r.get("rusher_player_id"):
                ru = r["rusher_player_id"]
                g["car"] += 1
                tg_state[s][(off, gid)][1][st] += 1
                R = pl[s][ru]; R["car"][st] += 1; R["cr"] += 1; R["games"].add(gid); R["gt"][gid] = off
                R["cy"].append(int(_f(r.get("rushing_yards") or r.get("yards_gained"))))
                if _flag(r.get("rush_touchdown")): R["rush_td"] += 1
                if rz: R["rz_c"] += 1
            # coverage mix faced by each defense
            if db:
                c = cov.get((gid, r.get("play_id")))
                if c:
                    if c[0]: covmix[(de, s)][c[0]] += 1
                    if c[1]: covmix[(de, s)][c[1]] += 1
        if drop_raw and s != cur:
            drop_raw(path)

    # --- league baselines ---
    lg_state = [[0, 0] for _ in range(STATES)]
    for t, by in team_state.items():
        for s, arr in by.items():
            for i in range(STATES):
                lg_state[i][0] += arr[i][0]; lg_state[i][1] += arr[i][1]
    lg_pr = [round(x[1] / x[0], 4) if x[0] else .58 for x in lg_state]

    # --- how games unfold: final margin -> share of plays in each state ---
    bank = []
    for (t, s), games in team_games.items():
        for gid, g in games.items():
            if g["plays"] < 30 or gid not in game_final:
                continue
            h, a, hp, ap, _ = game_final[gid]
            m = (hp - ap) if t == h else (ap - hp)
            bank.append([int(m)] + [round(x / g["plays"], 3) for x in g["st"]] + [g["plays"]])
    bank.sort()

    # --- teams ---
    teams = {}
    for t in sorted(team_state):
        if t not in {v["t"] for v in roster.values()}:
            continue
        def pr_for(seas):
            tot = [[0, 0] for _ in range(STATES)]
            for s in seas:
                arr = team_state[t].get(s)
                if not arr: continue
                for i in range(STATES):
                    tot[i][0] += arr[i][0]; tot[i][1] += arr[i][1]
            return tot
        cur_ = pr_for([cur]); all_ = pr_for(seasons)
        # team identity: overall pass rate vs league, applied to every state, then shrunk
        def overall(tt):
            p = sum(x[0] for x in tt); d = sum(x[1] for x in tt)
            return d / p if p else None
        lg_all = sum(x[1] for x in lg_state) / max(1, sum(x[0] for x in lg_state))
        off_all = (overall(all_) or lg_all) - lg_all
        off_cur = (overall(cur_) or lg_all) - lg_all
        ident = off_cur * .65 + off_all * .35 if overall(cur_) is not None else off_all
        pr = []
        for i in range(STATES):
            prior = min(.92, max(.2, lg_pr[i] + ident))
            n, d = cur_[i]
            pr.append(round((d + 40 * prior) / (n + 40), 4))
        gcur = team_games[(t, cur)]
        nG = len([g for g in gcur.values() if g["plays"] >= 30]) or 1
        plays = sum(g["plays"] for g in gcur.values()) / nG
        db = sum(g["db"] for g in gcur.values()); tg = sum(g["tgt"] for g in gcur.values())
        att = sum(g["att"] for g in gcur.values()); car = sum(g["car"] for g in gcur.values())
        # location/depth, offense and defense, this season and since the first season
        def loc_pack(src):
            out = {}
            for s in (cur, "all"):
                agg = defaultdict(lambda: [0, 0, 0.0])
                for ss in (seasons if s == "all" else [cur]):
                    for z, v in src.get((t, ss), {}).items():
                        A = agg[z]; A[0] += v[0]; A[1] += v[1]; A[2] += v[2]
                out[str(s)] = {z: [v[0], v[1], round(v[2])] for z, v in agg.items()}
            return out
        pn = {}
        for s in seasons:
            gp = len([g for g in team_games[(t, s)].values() if g["plays"] >= 30]) or None
            if not gp: continue
            v = pen.get((t, s), {})
            pn[str(s)] = {"g": gp, "off": round(v.get("off_n", 0) / gp, 2), "def": round(v.get("def_n", 0) / gp, 2),
                          "offy": round(v.get("off_y", 0) / gp, 1), "defy": round(v.get("def_y", 0) / gp, 1),
                          "drawn": round((v.get("drawn_off", 0) + v.get("drawn_def", 0)) / gp, 2),
                          "top_def": sorted(pen_types[(t, s, "def")].items(), key=lambda x: -x[1])[:4],
                          "top_off": sorted(pen_types[(t, s, "off")].items(), key=lambda x: -x[1])[:4]}
        cm = {}
        for s in cov_seasons:
            v = covmix.get((t, s))
            if v:
                tot = v.get("M", 0) + v.get("Z", 0)
                shells = {k: n for k, n in v.items() if k not in ("M", "Z")}
                stot = sum(shells.values()) or 1
                cm[str(s)] = {"man": round(v.get("M", 0) / tot, 3) if tot else None,
                              "shells": {k: round(n / stot, 3) for k, n in sorted(shells.items(), key=lambda x: -x[1])[:6]}}
        ad = airy.get((t, cur))
        teams[t] = {"pr": pr, "prc": [[x[0], x[1]] for x in cur_], "plays": round(plays, 1), "g": nG,
                    "tpd": round(tg / db, 3) if db else .86, "apd": round(att / db, 3) if db else .9,
                    "carpg": round(car / nG, 1), "adot": round(ad[1] / ad[0], 1) if ad and ad[0] else None,
                    "loc_off": loc_pack(loc_off), "loc_def": loc_pack(loc_def), "pen": pn, "cov": cm}

    lg_loc = {}
    for s in (cur, "all"):
        agg = defaultdict(lambda: [0, 0, 0.0])
        for ss in (seasons if s == "all" else [cur]):
            for z, v in loc_lg[ss].items():
                A = agg[z]; A[0] += v[0]; A[1] += v[1]; A[2] += v[2]
        lg_loc[str(s)] = {z: [v[0], v[1], round(v[2])] for z, v in agg.items()}

    # --- players (current active roster, offense) ---
    players = {}
    for pid, info in roster.items():
        if info["p"] not in ("QB", "RB", "WR", "TE"):
            continue
        C = pl[cur].get(pid)
        team = info["t"]
        # team totals by state over the games he played this season
        tt, tc = [0] * STATES, [0] * STATES
        on_field = set((snap_games or {}).get((info.get("n"), team), set())) | (C["games"] if C else set())
        if on_field:
            for gid in on_field:
                x = tg_state[cur].get((team, gid))
                if x:
                    for i in range(STATES): tt[i] += x[0][i]; tc[i] += x[1][i]
        # his overall share this season (fallback: since first season)
        def share(kind, tot):
            mine = C[kind] if C else [0] * STATES
            n, d = sum(mine), sum(tot)
            base = n / d if d else 0
            return [round((mine[i] + 25 * base) / (tot[i] + 25), 4) for i in range(STATES)], round(base, 4), n, d
        tsh, tbase, tn, td_ = share("tgt", tt)
        csh, cbase, cn, cd = share("car", tc)
        # outcome samples: most recent first across seasons
        ty, cy, ay, sy = [], [], [], []
        rec_td = rush_td = tg = cr = att = ptd = pint = db = sk = scr = 0
        deep = airs = airn = 0
        loc = [0, 0, 0]; vs = {"M": [0, 0, 0.0], "Z": [0, 0, 0.0]}; dpi_n = dpi_y = 0
        rz_t = rz_c = 0
        all_tt = all_tc = 0
        for s in sorted(seasons, reverse=True):
            X = pl[s].get(pid)
            if not X: continue
            for gid, tm in X["gt"].items():
                x = tg_state[s].get((tm, gid))
                if x: all_tt += sum(x[0]); all_tc += sum(x[1])
            ty += list(reversed(X["ty"])); cy += list(reversed(X["cy"])); ay += list(reversed(X["ay"])); sy += list(reversed(X["sy"]))
            rec_td += X["rec_td"]; rush_td += X["rush_td"]; tg += X["tg"]; cr += X["cr"]
            att += X["att"]; ptd += X["ptd"]; pint += X["pint"]; db += X["db"]; sk += X["sk"]; scr += X["scr"]
            deep += X["deep"]; airs += X["airs"]; airn += X["airn"]
            for i in range(3): loc[i] += X["loc"][i]
            for k in ("M", "Z"):
                for i in range(3): vs[k][i] += X["vs"][k][i]
            dpi_n += X["dpi_n"]; dpi_y += X["dpi_y"]
            if s == cur:
                rz_t, rz_c = X["rz_t"], X["rz_c"]
        if not (tg or cr or att):
            continue
        o = {"t": team, "sh": {"t": tsh, "c": csh, "tb": tbase, "cb": cbase, "tn": tn, "cn": cn, "td": td_, "cd": cd,
                               "ta": round(tg / all_tt, 4) if all_tt else 0, "ca": round(cr / all_tc, 4) if all_tc else 0},
             "ty": [(-99 if v is None else v) for v in ty[:SAMPLE_N["tgt"]]],
             "cy": cy[:SAMPLE_N["car"]],
             "rtd": [rec_td, tg], "utd": [rush_td, cr],
             "rz": [rz_t, rz_c], "deep": round(deep / tg, 3) if tg else None,
             "adot": round(airs / airn, 1) if airn else None,
             "loc": [round(x / sum(loc), 3) for x in loc] if sum(loc) else None,
             "vs": {k: [v[0], v[1], round(v[2])] for k, v in vs.items()} if vs["M"][0] + vs["Z"][0] else None,
             "dpi": [dpi_n, round(dpi_y)] if dpi_n else None}
        if info["p"] == "QB" and att:
            o["qb"] = {"ay": ay[:SAMPLE_N["att"]], "td": [ptd, att], "int": [pint, att], "sk": [sk, db], "scr": [scr, db],
                       "sy": sy[:SAMPLE_N["scr"]]}
        players[pid] = o
    return {"season": cur, "states": ["Trailing by 9+", "Trailing by 1-8", "Tied", "Leading by 1-8", "Leading by 9+"],
            "cov_seasons": cov_seasons, "league": {"pr": lg_pr, "loc": lg_loc},
            "bank": bank, "teams": teams, "players": players}
