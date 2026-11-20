"""
Post-game recaps for Game Script (standard library only).

For every finished game this season it writes recaps/<game_id>.json, plus a small
nfl_recap.json index with each team's running projection accuracy.

What a recap compares, and why it's honest:
  * Projections: the matchup board's candidates are frozen at kickoff (nfl_learn.json;
    backtest weeks were built only from earlier games). Those frozen numbers are
    compared with the official box score. Nothing is re-projected after the fact.
  * "Played to form": each team's season-to-date numbers from the weeks BEFORE this
    game, blended with what the opponent's defense allowed before this game, vs what
    happened in it.
  * Drives, where plays were made (run lanes and pass zones), quarter by quarter flow,
    big plays and player lines all come from nflverse play-by-play.
"""
import json
import os
from collections import defaultdict

LANES = ["L-end", "L-tackle", "L-guard", "mid", "R-guard", "R-tackle", "R-end"]
ZONES = [f"{d}-{l}" for d in ("deep", "int", "short") for l in ("left", "middle", "right")]
FLOOR = {"pyd": 60, "ptd": 1, "ryd": 15, "recyd": 15, "rec": 2}
TOL = {"epa": .10, "succ": .06, "ypc": .8, "ypa": 1.2, "sack": .04, "expl": .04, "npr": .08}


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _i(x):
    return x in ("1", "1.0", 1, True)


def _yl(s, pos):
    """'IND 20' -> yards from the offense's own goal line (20); 'HOU 16' for IND -> 84; '50' -> 50."""
    if not s:
        return None
    p = s.split()
    if len(p) == 1:
        return _f(p[0], 50)
    side, n = p[0], _f(p[1], 50)
    return n if side == pos else 100 - n


def _lane(r):
    loc, gap = r.get("run_location"), r.get("run_gap")
    if loc == "middle":
        return "mid"
    if loc in ("left", "right"):
        return ("L-" if loc == "left" else "R-") + (gap or "guard")
    return None


def _zone(r):
    loc = r.get("pass_location")
    if loc not in ("left", "middle", "right") or r.get("air_yards") in ("", None):
        return None
    ay = _f(r["air_yards"])
    return ("deep" if ay >= 20 else "int" if ay >= 10 else "short") + "-" + loc


def _slim(r):
    keys = ("game_id", "week", "posteam", "defteam", "home_team", "away_team", "qtr", "time", "down", "ydstogo", "yardline_100",
            "play_type", "epa", "success", "yards_gained", "run_location", "run_gap", "pass_location", "air_yards",
            "complete_pass", "interception", "fumble_lost", "sack", "touchdown", "td_team", "qb_dropback", "qb_scramble",
            "first_down", "third_down_converted", "third_down_failed", "fourth_down_converted", "fourth_down_failed",
            "rusher_player_id", "rusher_player_name", "receiver_player_id", "receiver_player_name", "passer_player_id",
            "passer_player_name", "desc", "score_differential", "fixed_drive", "fixed_drive_result", "drive_start_transition",
            "drive_start_yard_line", "drive_end_yard_line", "drive_play_count", "ydsnet", "drive_time_of_possession",
            "drive_quarter_start", "drive_game_clock_start", "total_home_score", "total_away_score", "two_point_attempt",
            "penalty", "penalty_team", "penalty_yards", "pass_oe", "passing_yards", "rushing_yards", "receiving_yards",
            "season_type", "game_seconds_remaining")
    return {k: r.get(k, "") for k in keys}


def _scrim(r):
    return r["play_type"] in ("pass", "run") and not _i(r["two_point_attempt"]) and r["epa"] not in ("", "NA")


def _neutral(r):
    return _f(r["down"]) in (1, 2) and _f(r["qtr"]) <= 3 and abs(_f(r["score_differential"])) <= 7


class Agg:
    """Running sums for one side of the ball."""
    __slots__ = ("pl", "epa", "succ", "rush", "ryd", "db", "dbyd", "sk", "expl", "np", "npass", "lanes", "zones")

    def __init__(self):
        self.pl = self.epa = self.succ = self.rush = self.ryd = self.db = self.dbyd = self.sk = self.expl = self.np = self.npass = 0.0
        self.lanes = defaultdict(lambda: [0, 0.0, 0, 0.0])          # carries, yards, successes, epa
        self.zones = defaultdict(lambda: [0, 0, 0.0, 0, 0.0, 0, 0])  # att, cmp, yds, successes, epa, td, int

    def add(self, r):
        e, s, y = _f(r["epa"]), _i(r["success"]), _f(r["yards_gained"])
        self.pl += 1; self.epa += e; self.succ += s
        if r["play_type"] == "run":
            self.rush += 1; self.ryd += y
            if y >= 10: self.expl += 1
            ln = _lane(r)
            if ln:
                L = self.lanes[ln]; L[0] += 1; L[1] += y; L[2] += s; L[3] += e
        else:
            if _i(r["qb_dropback"]):
                self.db += 1; self.dbyd += y
                if _i(r["sack"]): self.sk += 1
            if _i(r["complete_pass"]) and y >= 20: self.expl += 1
            z = _zone(r)
            if z and not _i(r["sack"]):
                Z = self.zones[z]; Z[0] += 1; Z[1] += _i(r["complete_pass"]); Z[2] += y if _i(r["complete_pass"]) else 0
                Z[3] += s; Z[4] += e; Z[5] += _i(r["touchdown"]) and _i(r["complete_pass"]); Z[6] += _i(r["interception"])
        if _neutral(r):
            self.np += 1; self.npass += r["play_type"] == "pass"

    def merge(self, o):
        for k in ("pl", "epa", "succ", "rush", "ryd", "db", "dbyd", "sk", "expl", "np", "npass"):
            setattr(self, k, getattr(self, k) + getattr(o, k))
        for k, v in o.lanes.items():
            L = self.lanes[k]
            for i in range(4): L[i] += v[i]
        for k, v in o.zones.items():
            Z = self.zones[k]
            for i in range(7): Z[i] += v[i]

    def rates(self):
        d = lambda a, b: a / b if b else None
        return {"plays": int(self.pl), "epa": d(self.epa, self.pl), "succ": d(self.succ, self.pl), "ypc": d(self.ryd, self.rush),
                "ypa": d(self.dbyd, self.db), "sack": d(self.sk, self.db), "expl": d(self.expl, self.pl), "npr": d(self.npass, self.np)}


def _r(x, n=3):
    return None if x is None else round(x, n)


def _rates_out(a):
    return {k: (_r(v) if isinstance(v, float) else v) for k, v in a.rates().items()}


def build_recap(rows, data, learn, out_dir="recaps", index_file="nfl_recap.json"):
    season = data["season"]
    games = {g["id"]: g for g in data["games"]}
    finals = {gid for gid, g in games.items() if g.get("hs") is not None}
    by_id = {p["id"]: p for p in data["players"]}
    plays = defaultdict(list)
    off_wk = defaultdict(Agg)    # (team, week) -> offense
    def_wk = defaultdict(Agg)    # (team, week) -> defense (what it allowed)
    for r in rows:
        if r.get("season_type") != "REG":
            continue
        s = _slim(r)
        plays[s["game_id"]].append(s)
        if s["posteam"] and _scrim(s):
            w = int(_f(s["week"]))
            off_wk[(s["posteam"], w)].add(s)
            def_wk[(s["defteam"], w)].add(s)
    weeks = sorted({w for _, w in off_wk})
    teams = sorted({t for t, _ in off_wk})

    def pre(store, t, W):
        a = Agg()
        for w in weeks:
            if w < W and (t, w) in store:
                a.merge(store[(t, w)])
        return a

    lg_cache = {}

    def league(W):
        if W not in lg_cache:
            a = Agg()
            for (t, w), v in off_wk.items():
                if w < W:
                    a.merge(v)
            lg_cache[W] = a
        return lg_cache[W]

    # frozen projections, by game
    proj_by_game = defaultdict(list)
    for sw, L in (learn.get("weeks") or {}).items():
        for c in (L.get("cand") or {}).values():
            proj_by_game[c["gid"]].append(dict(c, wk=int(sw)))

    def actual(pid, wk, k):
        p = by_id.get(pid)
        if not p:
            return None
        for w in p.get("wk") or []:
            if w["w"] == wk:
                v = w.get(k)
                return float(v) if isinstance(v, (int, float)) else 0.0
        return None

    os.makedirs(out_dir, exist_ok=True)
    index = {"season": season, "games": {}, "teams": defaultdict(list)}
    wrote = 0
    for gid in sorted(finals):
        P = plays.get(gid)
        if not P:
            continue
        g = games[gid]
        W, H, A = g["wk"], g["home"], g["away"]
        out = {"gid": gid, "wk": W, "home": H, "away": A, "hs": g["hs"], "as": g["as"], "spread": g.get("spread"),
               "total": g.get("total"), "teams": {}, "proj": [], "notes": []}
        # score by quarter
        q_end = {}
        for r in P:
            q = int(_f(r["qtr"], 1))
            if r["total_home_score"] != "":
                q_end[q] = (int(_f(r["total_home_score"])), int(_f(r["total_away_score"])))
        qs = sorted(q_end)
        prev = (0, 0)
        qh, qa = [], []
        for q in qs:
            qh.append(q_end[q][0] - prev[0]); qa.append(q_end[q][1] - prev[1]); prev = q_end[q]
        out["q"] = {H: qh, A: qa}
        lg = league(W)
        lgr = lg.rates()
        for t, o in ((H, A), (A, H)):
            mine = [r for r in P if r["posteam"] == t]
            sc = [r for r in mine if _scrim(r)]
            ga = Agg()
            for r in sc:
                ga.add(r)
            # drives
            drives, seen = [], {}
            for r in mine:
                fd = r["fixed_drive"]
                if not fd or fd in seen:
                    continue
                seen[fd] = 1
                st, en = _yl(r["drive_start_yard_line"], t), _yl(r["drive_end_yard_line"], t)
                res = r["fixed_drive_result"]
                if res == "Touchdown":
                    en = 100
                tp = r["drive_time_of_possession"] or "0:00"
                mm, ss = (tp.split(":") + ["0"])[:2]
                drives.append({"q": int(_f(r["drive_quarter_start"], 1)), "clk": r["drive_game_clock_start"], "st": st, "en": en,
                               "how": (r["drive_start_transition"] or "").replace("_", " ").title(), "res": res,
                               "pl": int(_f(r["drive_play_count"])), "yds": int(_f(r["ydsnet"])), "top": int(_f(mm)) * 60 + int(_f(ss))})
            # situational
            third = [sum(_i(r["third_down_converted"]) for r in mine), sum(_i(r["third_down_converted"]) or _i(r["third_down_failed"]) for r in mine)]
            fourth = [sum(_i(r["fourth_down_converted"]) for r in mine), sum(_i(r["fourth_down_converted"]) or _i(r["fourth_down_failed"]) for r in mine)]
            rz_trips = [d for d in drives if d["en"] is not None and d["en"] >= 80]
            rz_td = sum(1 for d in drives if d["en"] is not None and d["en"] >= 80 and d["res"] == "Touchdown")
            pen = [r for r in P if _i(r["penalty"]) and r["penalty_team"] == t]
            to = sum(_i(r["interception"]) + _i(r["fumble_lost"]) for r in mine)
            top = sum(d["top"] for d in drives)
            starts = [d["st"] for d in drives if d["st"] is not None]
            pts = g["hs"] if t == H else g["as"]
            summ = dict(_rates_out(ga), drives=len(drives), ppd=_r(pts / len(drives), 2) if drives else None,
                        start=_r(sum(starts) / len(starts), 1) if starts else None, opp40=sum(1 for d in drives if d["en"] is not None and d["en"] >= 60),
                        rz=[rz_td, len(rz_trips)], third=third, fourth=fourth, to=to, sk_taken=int(ga.sk),
                        pen=[len(pen), int(sum(_f(r["penalty_yards"]) for r in pen))], top=top,
                        yds=int(sum(_f(r["yards_gained"]) for r in sc)), ypp=_r(sum(_f(r["yards_gained"]) for r in sc) / len(sc), 2) if sc else None)
            po, pd = pre(off_wk, t, W), pre(def_wk, o, W)
            por, pdr = po.rates(), pd.rates()
            exp = {}
            for k in ("epa", "succ", "ypc", "ypa", "sack", "expl"):
                if por.get(k) is not None and pdr.get(k) is not None and lgr.get(k) is not None:
                    exp[k] = _r(por[k] + pdr[k] - lgr[k])
            if por.get("npr") is not None:
                exp["npr"] = _r(por["npr"])
            # by quarter
            byq = []
            for q in sorted({int(_f(r["qtr"], 1)) for r in sc}):
                qq = [r for r in sc if int(_f(r["qtr"], 1)) == q]
                byq.append({"q": q, "pl": len(qq), "pass": sum(1 for r in qq if r["play_type"] == "pass"),
                            "epa": _r(sum(_f(r["epa"]) for r in qq) / len(qq)), "succ": _r(sum(_i(r["success"]) for r in qq) / len(qq)),
                            "yds": int(sum(_f(r["yards_gained"]) for r in qq))})
            # big plays
            big = sorted([r for r in sc if (_f(r["yards_gained"]) >= 20 and r["play_type"] == "pass") or (_f(r["yards_gained"]) >= 12 and r["play_type"] == "run")
                          or _i(r["touchdown"])], key=lambda r: -_f(r["epa"]))[:7]
            big = [{"q": int(_f(r["qtr"], 1)), "clk": r["time"], "yds": int(_f(r["yards_gained"])), "epa": _r(_f(r["epa"]), 2),
                    "d": (r["desc"] or "")[:170]} for r in big]
            # players
            pl = defaultdict(lambda: {"tgt": 0, "rec": 0, "recyd": 0, "ay": 0.0, "car": 0, "ryd": 0, "td": 0, "att": 0, "cmp": 0, "pyd": 0,
                                      "int": 0, "sk": 0, "in": 0, "out": 0, "L": 0, "M": 0, "R": 0, "succ": 0, "epa": 0.0, "n": ""})
            for r in sc:
                if r["play_type"] == "run" and r["rusher_player_id"]:
                    x = pl[r["rusher_player_id"]]; x["n"] = r["rusher_player_name"]
                    x["car"] += 1; x["ryd"] += int(_f(r["rushing_yards"] or r["yards_gained"])); x["succ"] += _i(r["success"]); x["epa"] += _f(r["epa"])
                    ln = _lane(r)
                    if ln in ("L-guard", "mid", "R-guard"): x["in"] += 1
                    elif ln: x["out"] += 1
                    if _i(r["touchdown"]) and r["td_team"] == t: x["td"] += 1
                if r["play_type"] == "pass":
                    if r["passer_player_id"]:
                        x = pl[r["passer_player_id"]]; x["n"] = r["passer_player_name"]
                        if _i(r["sack"]): x["sk"] += 1
                        elif _i(r["qb_scramble"]):
                            x["car"] += 1; x["ryd"] += int(_f(r["yards_gained"]))
                        else:
                            x["att"] += 1; x["cmp"] += _i(r["complete_pass"]); x["pyd"] += int(_f(r["passing_yards"])); x["int"] += _i(r["interception"])
                            if _i(r["touchdown"]) and _i(r["complete_pass"]) and r["td_team"] == t: x["td"] += 1
                    if r["receiver_player_id"] and not _i(r["sack"]):
                        x = pl[r["receiver_player_id"]]; x["n"] = r["receiver_player_name"]
                        x["tgt"] += 1; x["rec"] += _i(r["complete_pass"]); x["recyd"] += int(_f(r["receiving_yards"])); x["ay"] += _f(r["air_yards"])
                        loc = r["pass_location"]
                        if loc in ("left", "middle", "right"): x[loc[0].upper()] += 1
                        if _i(r["touchdown"]) and _i(r["complete_pass"]) and r["td_team"] == t: x["td"] += 1
            players = []
            for pid, x in pl.items():
                if x["tgt"] + x["car"] + x["att"] == 0:
                    continue
                pp = by_id.get(pid) or {}
                x = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in x.items()}
                x.update(id=pid, pos=pp.get("p", ""), name=pp.get("n") or x["n"])
                players.append(x)
            players.sort(key=lambda x: -(x["pyd"] + x["ryd"] + x["recyd"]))
            lanes = {k: [v[0], int(v[1]), v[2], _r(v[3], 2)] for k, v in ga.lanes.items()}
            zones = {k: [v[0], v[1], int(v[2]), v[3], _r(v[4], 2), v[5], v[6]] for k, v in ga.zones.items()}
            lanes_pre = {k: [v[0], int(v[1]), v[2]] for k, v in po.lanes.items()}
            lanes_dpre = {k: [v[0], int(v[1]), v[2]] for k, v in pd.lanes.items()}
            zones_pre = {k: [v[0], int(v[2]), v[3], _r(v[4], 1)] for k, v in po.zones.items()}
            zones_dpre = {k: [v[0], int(v[2]), v[3], _r(v[4], 1)] for k, v in pd.zones.items()}
            out["teams"][t] = {"sum": summ, "pre": _rates_out(po), "dpre": _rates_out(pd), "exp": exp, "drives": drives, "byq": byq,
                               "big": big, "players": players[:14], "lanes": lanes, "zones": zones, "lanes_pre": lanes_pre,
                               "lanes_dpre": lanes_dpre, "zones_pre": zones_pre, "zones_dpre": zones_dpre, "pre_g": int(sum(1 for w in weeks if w < W and (t, w) in off_wk))}
            # did the expectation hold?
            lab = {"epa": "EPA per play", "succ": "success rate", "ypc": "yards per carry", "ypa": "yards per dropback", "sack": "sack rate", "expl": "explosive-play rate", "npr": "pass rate in neutral situations"}
            gr = ga.rates()
            for k, e in exp.items():
                a = gr.get(k)
                if a is None or e is None:
                    continue
                if k in ("ypc",) and ga.rush < 8 or k in ("ypa", "sack") and ga.db < 10 or k == "npr" and ga.np < 10:
                    continue
                held = abs(a - e) <= TOL[k]
                better = (a < e) if k == "sack" else (a > e)
                out["notes"].append({"t": t, "k": k, "label": lab[k], "exp": _r(e), "act": _r(a), "held": held,
                                     "dir": 0 if held else (1 if better else -1),
                                     "off": _r(por.get(k)), "def": _r(pdr.get(k)) if k != "npr" else None})
        # projections vs actual
        rows_p = []
        for c in proj_by_game.get(gid, []):
            act = actual(c["pid"], W, c["k"])
            if act is None:
                continue
            push = act == c["line"]
            side_hit = None if push else ((act > c["line"]) == (c["side"] == "over"))
            fl = FLOOR.get(c["k"], 10)
            rows_p.append({"pid": c["pid"], "n": c["n"], "t": c["t"], "pos": c.get("pos"), "k": c["k"], "line": c["line"], "proj": _r(c["proj"], 1),
                           "side": c["side"], "p": _r(c["p"]), "act": act, "hit": side_hit, "err": _r((act - c["proj"]) / max(abs(c["proj"]), fl), 3),
                           "src": c.get("lsrc"), "fx": sorted(((k, v) for k, v in (c.get("fx") or {}).items()), key=lambda kv: -abs(kv[1]))[:3]})
        rows_p.sort(key=lambda x: (x["t"], x["n"], x["k"]))
        out["proj"] = rows_p

        def acc(rs):
            dec = [r for r in rs if r["hit"] is not None]
            return {"n": len(rs), "hit": sum(1 for r in dec if r["hit"]), "dec": len(dec),
                    "err": _r(sum(abs(r["err"]) for r in rs) / len(rs)) if rs else None,
                    "bias": _r(sum(r["err"] for r in rs) / len(rs)) if rs else None}
        out["acc"] = {t: acc([r for r in rows_p if r["t"] == t]) for t in (H, A)}
        with open(os.path.join(out_dir, gid + ".json"), "w") as f:
            json.dump(out, f, separators=(",", ":"))
        wrote += 1
        index["games"][gid] = {"wk": W, "home": H, "away": A, "hs": g["hs"], "as": g["as"], "spread": g.get("spread"), "total": g.get("total"),
                               "acc": out["acc"]}
        for t in (H, A):
            a = out["acc"][t]
            if a["n"]:
                index["teams"][t].append(dict(a, wk=W, gid=gid, opp=A if t == H else H))
    for t in index["teams"]:
        index["teams"][t].sort(key=lambda x: x["wk"])
    with open(index_file, "w") as f:
        json.dump(index, f, separators=(",", ":"))
    print(f"Recaps: {wrote} games written to {out_dir}/, index {index_file}")
    return index
