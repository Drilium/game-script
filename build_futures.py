"""
Futures model for Game Script's password-locked Futures page (standard library only).

What it does, once per refresh:
  1. Team ratings (points better than an average team on a neutral field), from
     game results this season with last season as a light prior, plus a small
     blend of what past point spreads imply. Posted lines are used as-is for
     the games they cover.
  2. Plays the rest of the regular season and the playoffs N_SIMS times, with
     each simulated season drawing its own rating errors (we never know a
     team's true strength exactly, and that uncertainty is what makes long
     shots win futures).
  3. Projects every relevant player's season totals (this season's pace,
     steadied by last season, adjusted for each remaining opponent, with
     injury risk), inside the same simulated seasons, and scores the awards in
     each one.
  4. Writes nfl_futures.json ENCRYPTED with a password that only lives in the
     FUTURES_PASS environment variable (a GitHub Actions secret). The site
     stores ciphertext, never the password.

Encryption: PBKDF2-HMAC-SHA256 (600,000 iterations) derives two keys from the
password; the gzipped JSON is XORed with an HMAC-SHA256 counter-mode keystream
and authenticated with HMAC-SHA256 (encrypt-then-MAC). All of it is in Python's
standard library and in the browser's built-in WebCrypto.
"""
import base64
import gzip
import hashlib
import hmac
import json
import math
import os
import random
import statistics
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

DIVS = {
    "AFC East": ["BUF", "MIA", "NE", "NYJ"], "AFC North": ["BAL", "CIN", "CLE", "PIT"],
    "AFC South": ["HOU", "IND", "JAX", "TEN"], "AFC West": ["DEN", "KC", "LAC", "LV"],
    "NFC East": ["DAL", "NYG", "PHI", "WAS"], "NFC North": ["CHI", "DET", "GB", "MIN"],
    "NFC South": ["ATL", "CAR", "NO", "TB"], "NFC West": ["ARI", "LA", "SEA", "SF"],
}
DIV_OF = {t: d for d, ts in DIVS.items() for t in ts}
CONF_OF = {t: d[:3] for t, d in DIV_OF.items()}
TEAMS = sorted(DIV_OF)
TIX = {t: i for i, t in enumerate(TEAMS)}

SD_GAME = 13.5          # spread of a game's final margin around its expectation
N_SIMS = 10000
RATING = {"decay": .95, "prior": .25, "lam": 8.0, "mkt_w": 0.0, "mkt_decay": .75, "mkt_lam": .2}
RATING_SD_MULT = 1.4   # chosen by the season backtest (wider than the naive estimate: true strength is less knowable than it looks)
KDF_ITER = 600000
OUT = "nfl_futures.json"


def phi(x):
    return .5 * (1 + math.erf(x / math.sqrt(2)))


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


# ------------------------------------------------------------------ ratings
def _fit(obs, lam, iters=80):
    """Weighted ridge fit of margin = hfa + r_home - r_away (Gauss-Seidel)."""
    r = {t: 0.0 for t in TEAMS}
    h = 1.5
    if not obs:
        return r, h
    by = defaultdict(list)
    for o in obs:
        by[o[0]].append(o)
        by[o[1]].append(o)
    for _ in range(iters):
        num = den = 0.0
        for a, b, v, wt, n in obs:
            if not n:
                num += wt * (v - (r[a] - r[b])); den += wt
        h = min(3.0, max(0.0, num / den)) if den else 1.5
        for t in TEAMS:
            num, den = 0.0, lam
            for a, b, v, wt, n in by[t]:
                hh = 0 if n else h
                if a == t:
                    num += wt * (v - hh + r[b]); den += wt
                else:
                    num += wt * (-(v - hh) + r[a]); den += wt
            r[t] = num / den
        m = sum(r.values()) / len(r)
        for t in TEAMS:
            r[t] -= m
    return r, h


def _obs(rows, season, week, kind, decay, prior_w, include_future_lines=False):
    o = []
    for g in rows:
        if g["game_type"] != "REG" or g["home_team"] not in TIX or g["away_team"] not in TIX:
            continue
        s, w = int(g["season"]), int(g["week"])
        if s < season - 1 or s > season:
            continue
        future = (s, w) >= (season, week)
        if kind == "mkt":
            if not g["spread_line"] or (future and not (include_future_lines and not g["home_score"])):
                continue
            v = float(g["spread_line"])
        else:
            if future or not g["home_score"]:
                continue
            v = max(-24, min(24, int(g["home_score"]) - int(g["away_score"])))
        if s == season:
            wt = decay ** max(0, week - 1 - w)
        else:
            wt = prior_w * (decay ** (18 - w)) * .5
        o.append((g["home_team"], g["away_team"], v, wt, g["location"] == "Neutral"))
    return o


def ratings_as_of(rows, season, week, include_future_lines=False):
    """Ratings using only what was known before `week` of `season`."""
    rm, hm = _fit(_obs(rows, season, week, "mrg", RATING["decay"], RATING["prior"]), RATING["lam"])
    rk, hk = _fit(_obs(rows, season, week, "mkt", RATING["mkt_decay"], 1.0, include_future_lines), RATING["mkt_lam"])
    w = RATING["mkt_w"]
    r = {t: (1 - w) * rm[t] + w * rk[t] for t in TEAMS}
    return r, (1 - w) * hm + w * hk, rm, rk


# ------------------------------------------------------------- season sim
def _season_games(rows, season):
    out = []
    for g in rows:
        if int(g["season"]) != season or g["game_type"] != "REG":
            continue
        if g["home_team"] not in TIX or g["away_team"] not in TIX:
            continue
        out.append({"id": g["game_id"], "wk": int(g["week"]), "h": g["home_team"], "a": g["away_team"],
                    "hs": int(g["home_score"]) if g["home_score"] else None,
                    "as": int(g["away_score"]) if g["away_score"] else None,
                    "sp": float(g["spread_line"]) if g["spread_line"] else None,
                    "n": g["location"] == "Neutral"})
    return out


class Sim:
    """Rest-of-season + playoff simulator. Records per-sim wins and seeds so awards can use them."""

    def __init__(self, games, r, hfa, rating_sd, cut_week=None, use_lines=True, seed=7):
        self.games, self.r, self.hfa, self.sd = games, r, hfa, rating_sd
        self.rng = random.Random(seed)
        self.played, self.rem = [], []
        for g in games:
            done = g["hs"] is not None and (cut_week is None or g["wk"] < cut_week)
            (self.played if done else self.rem).append(g)
        self.use_lines = use_lines

    def _base(self):
        n = len(TEAMS)
        W = [0.0] * n; G = [0] * n; DW = [0.0] * n; DG = [0] * n; CW = [0.0] * n; CG = [0] * n
        H2 = defaultdict(float); H2G = defaultdict(int)
        for g in self.played:
            self._rec(W, G, DW, DG, CW, CG, H2, H2G, g["h"], g["a"], 1.0 if g["hs"] > g["as"] else 0.0 if g["hs"] < g["as"] else .5)
        return W, G, DW, DG, CW, CG, H2, H2G

    @staticmethod
    def _rec(W, G, DW, DG, CW, CG, H2, H2G, h, a, hw):
        i, j = TIX[h], TIX[a]
        W[i] += hw; W[j] += 1 - hw; G[i] += 1; G[j] += 1
        if DIV_OF[h] == DIV_OF[a]:
            DW[i] += hw; DW[j] += 1 - hw; DG[i] += 1; DG[j] += 1
        if CONF_OF[h] == CONF_OF[a]:
            CW[i] += hw; CW[j] += 1 - hw; CG[i] += 1; CG[j] += 1
        H2[(i, j)] += hw; H2[(j, i)] += 1 - hw; H2G[(i, j)] += 1; H2G[(j, i)] += 1

    def _order(self, group, st, use_div):
        """Simplified NFL tiebreakers: win %, head-to-head among the tied, division %, conference %, then strength/coin."""
        W, G, DW, DG, CW, CG, H2, H2G, e = st
        pct = lambda i: W[i] / G[i] if G[i] else 0
        by = defaultdict(list)
        for i in group:
            by[round(pct(i), 4)].append(i)
        out = []
        for k in sorted(by, reverse=True):
            tied = by[k]
            if len(tied) > 1:
                def key(i):
                    hw = sum(H2[(i, j)] for j in tied if j != i); hg = sum(H2G[(i, j)] for j in tied if j != i)
                    return (hw / hg if hg else .5, (DW[i] / DG[i] if DG[i] else .5) if use_div else 0,
                            CW[i] / CG[i] if CG[i] else .5, e[i] + self.rng.random() * 1e-3)
                tied = sorted(tied, key=key, reverse=True)
            out += tied
        return out

    def _pwin(self, h, a, e, neutral=False):
        mu = self.r[TEAMS[h]] + e[h] - self.r[TEAMS[a]] - e[a] + (0 if neutral else self.hfa)
        return phi(mu / SD_GAME)

    def run(self, n=N_SIMS, keep=False):
        base = self._base()
        nt = len(TEAMS)
        acc = {k: [0] * nt for k in ("po", "div", "s1", "conf", "sb", "wc_home")}
        wdist = [[0] * 18 for _ in range(nt)]
        wsum = [0.0] * nt
        self.wins_by = [[0.0] * n for _ in range(nt)] if keep else None
        self.seed_by = [[0] * n for _ in range(nt)] if keep else None
        self.sb_by = [0] * n if keep else None
        rem = [(TIX[g["h"]], TIX[g["a"]], g["sp"] if self.use_lines else None, g["n"], g["h"], g["a"]) for g in self.rem]
        conf_teams = {c: [TIX[t] for t in TEAMS if CONF_OF[t] == c] for c in ("AFC", "NFC")}
        div_teams = {d: [TIX[t] for t in ts] for d, ts in DIVS.items()}
        rng = self.rng
        for s in range(n):
            e = [rng.gauss(0, self.sd) for _ in range(nt)]
            W, G, DW, DG, CW, CG = (list(x) for x in base[:6])
            H2, H2G = defaultdict(float, base[6]), defaultdict(int, base[7])
            for i, j, sp, neu, h, a in rem:
                if sp is not None:
                    mu = sp + .5 * (e[i] - e[j])
                else:
                    mu = self.r[h] + e[i] - self.r[a] - e[j] + (0 if neu else self.hfa)
                hw = 1.0 if rng.random() < phi(mu / SD_GAME) else 0.0
                self._rec(W, G, DW, DG, CW, CG, H2, H2G, h, a, hw)
            st = (W, G, DW, DG, CW, CG, H2, H2G, e)
            seeds = {}
            for c, ct in conf_teams.items():
                winners = [self._order(div_teams[d], st, True)[0] for d in DIVS if d.startswith(c)]
                top4 = self._order(winners, st, False)
                rest = self._order([i for i in ct if i not in winners], st, False)[:3]
                order = top4 + rest
                for k, i in enumerate(order):
                    seeds[i] = k + 1
                for i in winners:
                    acc["div"][i] += 1
                acc["s1"][order[0]] += 1
                seeds[c] = order
            champs = {}
            for c in ("AFC", "NFC"):
                o = seeds[c]
                alive = [o[0]]
                for hi, lo in ((1, 6), (2, 5), (3, 4)):
                    alive.append(o[hi] if rng.random() < self._pwin(o[hi], o[lo], e) else o[lo])
                alive.sort(key=lambda i: seeds[i])
                a1 = alive[0] if rng.random() < self._pwin(alive[0], alive[3], e) else alive[3]
                a2 = alive[1] if rng.random() < self._pwin(alive[1], alive[2], e) else alive[2]
                hi_, lo_ = (a1, a2) if seeds[a1] < seeds[a2] else (a2, a1)
                champs[c] = hi_ if rng.random() < self._pwin(hi_, lo_, e) else lo_
                acc["conf"][champs[c]] += 1
            a, b = champs["AFC"], champs["NFC"]
            sb = a if rng.random() < self._pwin(a, b, e, neutral=True) else b
            acc["sb"][sb] += 1
            for i in range(nt):
                wi = W[i]
                wsum[i] += wi
                wdist[i][min(17, int(wi))] += 1
                if i in seeds and isinstance(seeds[i], int):
                    acc["po"][i] += 1
                if keep:
                    self.wins_by[i][s] = wi
                    self.seed_by[i][s] = seeds.get(i, 0) if isinstance(seeds.get(i, 0), int) else 0
            if keep:
                self.sb_by[s] = sb
        self.n = n
        return {TEAMS[i]: {"po": acc["po"][i] / n, "div": acc["div"][i] / n, "s1": acc["s1"][i] / n,
                           "conf": acc["conf"][i] / n, "sb": acc["sb"][i] / n, "w": wsum[i] / n,
                           "wd": [round(x / n, 4) for x in wdist[i]]} for i in range(nt)}


def rating_sd_for(games_played):
    """How unsure we are of a team's true strength: about 4 points at midseason, wider early."""
    return RATING_SD_MULT * SD_GAME / math.sqrt(games_played + 12)


# --------------------------------------------------- backtest of the sim
def backtest(rows, seasons=(2024, 2025), weeks=(6, 10, 14), n=2000):
    """Brier score of playoff and division probabilities, model vs. a record-only baseline (every remaining game a coin flip)."""
    out = []
    for s in seasons:
        po_teams, div_w = set(), set()
        wc_teams = set()
        for g in rows:
            if int(g["season"]) != s:
                continue
            if g["game_type"] == "WC":
                po_teams |= {g["home_team"], g["away_team"]}; div_w.add(g["home_team"]); wc_teams |= {g["home_team"], g["away_team"]}
        for g in rows:
            if int(g["season"]) == s and g["game_type"] == "DIV":
                for t in (g["home_team"], g["away_team"]):
                    if t not in wc_teams:
                        po_teams.add(t); div_w.add(t)
        if len(po_teams) != 14:
            continue
        games = _season_games(rows, s)
        for wk in weeks:
            r, h, _, _ = ratings_as_of(rows, s, wk)
            gp = sum(1 for g in games if g["wk"] < wk) * 2 / 32
            m = Sim(games, r, h, rating_sd_for(gp), cut_week=wk, use_lines=False, seed=11).run(n)
            z = {t: 0.0 for t in TEAMS}
            b = Sim(games, z, 0.0, 0.0, cut_week=wk, use_lines=False, seed=11).run(n)
            row = {"s": s, "wk": wk}
            for k, truth in (("po", po_teams), ("div", div_w)):
                row[k] = round(sum((m[t][k] - (t in truth)) ** 2 for t in TEAMS) / 32, 4)
                row[k + "_base"] = round(sum((b[t][k] - (t in truth)) ** 2 for t in TEAMS) / 32, 4)
            out.append(row)
    return out


# ------------------------------------------------------- player seasons
OFF_KEYS = {"QB": ["pyd", "ptd", "int", "ryd", "rtd", "fp"], "RB": ["ryd", "rtd", "rec", "recyd", "rectd", "fp"],
            "WR": ["rec", "recyd", "rectd", "ryd", "rtd", "fp"], "TE": ["rec", "recyd", "rectd", "fp"]}
DEF_KEYS = ["sacks", "int", "ff", "tfl", "pd", "qbh", "tkl"]
INJ_Q = {"QB": .025, "RB": .045, "WR": .035, "TE": .035, "DEF": .03}
DA_KEY = {"pyd": "pyd", "ptd": "ptd", "int": "int", "ryd": "ryd", "rtd": "rtd", "rec": "rec", "recyd": "recyd", "rectd": "rectd", "fp": "fp"}


def _wk_val(w, k):
    v = w.get(k)
    return float(v) if isinstance(v, (int, float)) else 0.0


def _recent(cur, last, m=3):
    """This season's per-game mean, steadied by last season counted as m games (Game Script's recent-form rule)."""
    if not cur and not last:
        return 0.0
    if not last:
        return sum(cur) / len(cur)
    ml = sum(last) / len(last)
    return (sum(cur) + m * ml) / (len(cur) + m)


def player_projections(data, remaining, sim_wk):
    """Per player: season-to-date totals and, per remaining game, the matchup-adjusted per-game mean and sd."""
    season = data["season"]
    DA = data.get("defense_allowed") or {}
    TS = data.get("team_stats") or {}
    lg = {}
    for pos in ("QB", "RB", "WR", "TE"):
        for k in DA_KEY:
            vals = [DA[t][pos][k] for t in DA if pos in DA[t] and isinstance(DA[t][pos].get(k), (int, float))]
            lg[(pos, k)] = sum(vals) / len(vals) if vals else None
    lg_sk = statistics.mean([TS[t]["off"]["skpg"] for t in TS if TS[t].get("off")]) if TS else None
    lg_to = statistics.mean([TS[t]["off"]["topg"] for t in TS if TS[t].get("off")]) if TS else None

    def opp_fac(opp, pos, k):
        if pos == "DEF":
            o = TS.get(opp, {}).get("off") or {}
            if k == "sacks" and lg_sk and o.get("skpg"):
                return min(1.25, max(.8, 1 + .5 * (o["skpg"] / lg_sk - 1)))
            if k == "int" and lg_to and o.get("topg"):
                return min(1.25, max(.8, 1 + .5 * (o["topg"] / lg_to - 1)))
            return 1.0
        d = DA.get(opp, {}).get(pos, {})
        L = lg.get((pos, DA_KEY.get(k, k)))
        v = d.get(DA_KEY.get(k, k))
        if not L or not isinstance(v, (int, float)):
            return 1.0
        return min(1.2, max(.8, 1 + .5 * (v / L - 1)))

    # who starts at QB: the schedule's listed starter for the next game, else depth chart QB1
    next_qb = {}
    for g in sorted([g for g in data["games"] if g["hs"] is None and g.get("gt", "REG") == "REG"], key=lambda g: g["wk"]):
        for t, q in ((g["home"], g.get("hqb")), (g["away"], g.get("aqb"))):
            if t not in next_qb and q:
                next_qb[t] = q
    team_games = defaultdict(list)
    for g in data["games"]:
        if g["hs"] is not None and g.get("gt", "REG") == "REG":
            team_games[g["home"]].append((g["wk"], g["away"]))
            team_games[g["away"]].append((g["wk"], g["home"]))
    # what a regular starter at each position averages per game: thin samples are pulled toward it
    starter_mean = {}
    for grp, keys_ in (("QB", OFF_KEYS["QB"]), ("RB", OFF_KEYS["RB"]), ("WR", OFF_KEYS["WR"]), ("TE", OFF_KEYS["TE"]),
                       ("DL", DEF_KEYS), ("LB", DEF_KEYS), ("DB", DEF_KEYS)):
        reg = [p for p in data["players"] if p["p"] == grp and len(p.get("wk") or []) >= 5 and
               (statistics.mean([w.get("sp") or 0 for w in p["wk"]]) >= (60 if grp not in ("QB",) else 80))]
        for k in keys_:
            vals = [statistics.mean([_wk_val(w, k) for w in p["wk"]]) for p in reg]
            starter_mean[(grp, k)] = statistics.mean(vals) if vals else 0.0
    out = {}
    for p in data["players"]:
        pos = p["p"]
        t = p["t"]
        if t not in TIX or not p.get("wk"):
            continue
        is_def = pos in ("DL", "LB", "DB")
        if not is_def and pos not in OFF_KEYS:
            continue
        keys = DEF_KEYS if is_def else OFF_KEYS[pos]
        wk = p["wk"]
        last = [h for h in (p.get("hist") or []) if h.get("s") == season - 1]
        if pos == "QB":
            starter = (next_qb.get(t) == p["n"]) or (next_qb.get(t) is None and p.get("dc") == 1)
            if not starter:
                continue
        # has he been on the field lately? (missed the team's last 2 games = probably still out)
        tg = sorted(team_games.get(t, []))[-2:]
        played_wks = {(w["w"], w.get("opp")) for w in wk}
        absent = len(tg) == 2 and all(x not in played_wks for x in tg)
        inj = p.get("inj") or {}
        out_next = inj.get("wk") == sim_wk and inj.get("st") in ("Out", "Doubtful")
        cur_tot = {k: sum(_wk_val(w, k) for w in wk) for k in keys}
        per = {}
        for k in keys:
            cv = [_wk_val(w, k) for w in wk]
            lv = [_wk_val(h, k) for h in last] if not is_def else []
            mu = _recent(cv, lv)
            if not lv:                                   # no last season to steady him: lean on a typical starter
                kk = 4 if is_def else 2
                mu = (sum(cv) + kk * starter_mean.get((pos, k), 0.0)) / (len(cv) + kk)
            allv = cv + lv
            sd = statistics.pstdev(allv) if len(allv) >= 3 else (mu * .6 + .3)
            per[k] = (mu, sd)
        games = remaining.get(t, [])
        facs = {k: [opp_fac(o, "DEF" if is_def else pos, k) for _, o in games] for k in keys}
        out[p["id"]] = {"id": p["id"], "n": p["n"], "t": t, "p": "DEF" if is_def else pos, "pos": pos, "x": p.get("x"),
                        "gp": len(wk), "cur": cur_tot, "per": per, "facs": facs, "games": games,
                        "absent": absent, "out_next": out_next, "last_gp": len(last),
                        "last_fppg": (sum(_wk_val(h, "fp") for h in last) / len(last)) if last else None,
                        "two_back_gp": len([h for h in (p.get("hist") or []) if h.get("s") == season - 2]),
                        "two_back_fppg": None}
        tb = [h for h in (p.get("hist") or []) if h.get("s") == season - 2]
        if tb:
            out[p["id"]]["two_back_fppg"] = sum(_wk_val(h, "fp") for h in tb) / len(tb)
    return out


def proj_mean(pp, k):
    """Expected final total for a stat, ignoring injuries (for picking candidates and explaining)."""
    mu = pp["per"][k][0]
    return pp["cur"][k] + sum(mu * f for f in pp["facs"][k])


class PlayerSim:
    """Simulates each candidate's remaining games: injury risk and absences, then a correlated draw of his totals."""

    def __init__(self, cands, n, seed=21):
        self.c = cands
        self.n = n
        self.rng = random.Random(seed)

    def run(self):
        rng = self.rng
        res = {}
        for pid, pp in self.c.items():
            G = len(pp["games"])
            q = INJ_Q.get(pp["p"], .03) / 3.0          # chance per game of an injury event; each costs ~3 games
            keys = list(pp["per"])
            means = {k: [pp["per"][k][0] * f for f in pp["facs"][k]] for k in keys}
            sds = {k: pp["per"][k][1] for k in keys}
            tot = {k: [0.0] * self.n for k in keys}
            for s in range(self.n):
                avail = []
                miss = 0
                for gi in range(G):
                    if gi == 0 and pp["out_next"]:
                        avail.append(False); continue
                    if pp["absent"] and gi < 4 and rng.random() < (.8 - .2 * gi):
                        avail.append(False); continue
                    if miss > 0:
                        miss -= 1; avail.append(False); continue
                    if rng.random() < q:
                        miss = int(rng.expovariate(1 / 3.0)) + 1
                        avail.append(rng.random() < .3)       # sometimes hurt mid-game, after producing a little
                        continue
                    avail.append(True)
                ng = sum(avail)
                zc = rng.gauss(0, 1)
                for k in keys:
                    m = sum(means[k][i] for i in range(G) if avail[i])
                    v = sds[k] * math.sqrt(ng) if ng else 0.0
                    draw = m + v * (.75 * zc + .66 * rng.gauss(0, 1))
                    tot[k][s] = pp["cur"][k] + max(0.0, draw)
            res[pid] = tot
        return res


# ------------------------------------------------------------- helpers
def american(p):
    if p <= 0:
        return None
    if p >= 1:
        return -100000
    return int(round(-100 * p / (1 - p))) if p >= .5 else int(round(100 * (1 - p) / p))


def _z(vals):
    m = statistics.mean(vals) if vals else 0
    s = statistics.pstdev(vals) if len(vals) > 1 else 1
    return m, (s or 1)


def _softmax_wins(scores, rng, n, noise):
    """scores: {key: [per-sim score]} -> share of sims each key has the top score after voter noise."""
    keys = list(scores)
    wins = defaultdict(float)
    for s in range(n):
        best, bk = -1e9, None
        for k in keys:
            v = scores[k][s] + rng.gauss(0, noise)
            if v > best:
                best, bk = v, k
        wins[bk] += 1
    return {k: wins[k] / n for k in keys}


# ------------------------------------------------------------- awards
def awards(proj, ptot, sim, coaches, pre_exp, rng, n):
    W = {t: sim.wins_by[TIX[t]] for t in TEAMS}
    S = {t: sim.seed_by[TIX[t]] for t in TEAMS}
    wm, ws = _z([w for t in TEAMS for w in W[t][:500]])
    out = {}

    def finals(pid, k):
        return ptot[pid][k] if pid in ptot and k in ptot[pid] else None

    def mean_of(pid, k):
        a = finals(pid, k)
        return sum(a) / len(a) if a else 0

    def card(pid, extra):
        pp = proj[pid]
        return dict({"id": pid, "n": pp["n"], "t": pp["t"], "pos": pp["pos"]}, **extra)

    # MVP: production (PPR points as a cross-position value), team wins and the 1 seed; non-QBs need far more
    qbs = [pid for pid in ptot if proj[pid]["pos"] == "QB"]
    skill = sorted([pid for pid in ptot if proj[pid]["pos"] in ("RB", "WR", "TE")], key=lambda x: -mean_of(x, "fp"))[:8]
    pool = qbs + skill
    if pool:
        fm, fs = _z([mean_of(p, "fp") for p in qbs] or [0])
        sc = {}
        for pid in pool:
            t = proj[pid]["t"]
            fp, w, sd = ptot[pid]["fp"], W[t], S[t]
            pen = 0 if proj[pid]["pos"] == "QB" else 1.6
            sc[pid] = [1.0 * (fp[s] - fm) / fs + 1.3 * (w[s] - wm) / ws + (.45 if sd[s] == 1 else 0) - pen for s in range(n)]
        pr = _softmax_wins(sc, rng, n, .7)
        out["mvp"] = sorted([card(p, {"pr": pr[p], "fp": round(mean_of(p, "fp"), 1), "w": round(sum(W[proj[p]["t"]]) / n, 1),
                                      "s1": round(sum(1 for x in S[proj[p]["t"]] if x == 1) / n, 3),
                                      "pyd": round(mean_of(p, "pyd")) if proj[p]["pos"] == "QB" else None,
                                      "td": round(mean_of(p, "ptd") + mean_of(p, "rtd"), 1) if proj[p]["pos"] == "QB" else round(mean_of(p, "rtd") + mean_of(p, "rectd"), 1),
                                      "int": round(mean_of(p, "int"), 1) if proj[p]["pos"] == "QB" else None}) for p in pool],
                            key=lambda x: -x["pr"])[:15]

    # OPOY: scrimmage production; QBs can win (Mahomes 2018) but voters lean to RB/WR
    pool = sorted([pid for pid in ptot if proj[pid]["pos"] in ("RB", "WR", "TE")], key=lambda x: -mean_of(x, "fp"))[:18] + \
        sorted(qbs, key=lambda x: -mean_of(x, "fp"))[:5]
    if pool:
        def scrim(pid, s):
            P = ptot[pid]
            return P.get("ryd", [0] * n)[s] + P.get("recyd", [0] * n)[s] if proj[pid]["pos"] != "QB" else 0
        sm, ss = _z([sum(scrim(p, s) for s in range(0, n, 50)) / len(range(0, n, 50)) for p in pool if proj[p]["pos"] != "QB"])
        fm, fs = _z([mean_of(p, "fp") for p in pool])
        sc = {}
        for pid in pool:
            t = proj[pid]["t"]; P = ptot[pid]
            if proj[pid]["pos"] == "QB":
                sc[pid] = [1.1 * (P["fp"][s] - fm) / fs + .25 * (W[t][s] - wm) / ws - 1.0 for s in range(n)]
            else:
                td = [P.get("rtd", [0] * n)[s] + P.get("rectd", [0] * n)[s] for s in range(n)]
                sc[pid] = [1.2 * (scrim(pid, s) - sm) / ss + .5 * (P["fp"][s] - fm) / fs + .08 * td[s] + .25 * (W[t][s] - wm) / ws for s in range(n)]
        pr = _softmax_wins(sc, rng, n, .65)
        out["opoy"] = sorted([card(p, {"pr": pr[p], "fp": round(mean_of(p, "fp"), 1),
                                       "scrim": round(mean_of(p, "ryd") + mean_of(p, "recyd")) if proj[p]["pos"] != "QB" else None,
                                       "pyd": round(mean_of(p, "pyd")) if proj[p]["pos"] == "QB" else None,
                                       "td": round(mean_of(p, "rtd") + mean_of(p, "rectd") + mean_of(p, "ptd"), 1)}) for p in pool],
                             key=lambda x: -x["pr"])[:15]

    # DPOY / DROY: a defensive impact score (sacks count most, then takeaways, TFLs, pass breakups)
    def dscore(P, s):
        return (P["sacks"][s] * 1.0 + P["int"][s] * 1.6 + P["ff"][s] * .8 + P["tfl"][s] * .35 + P["pd"][s] * .25 + P["qbh"][s] * .12 + P["tkl"][s] * .02)
    defs = [pid for pid in ptot if proj[pid]["p"] == "DEF"]

    def def_award(pool, key, noise):
        if not pool:
            return
        mm, msd = _z([sum(dscore(ptot[p], s) for s in range(0, n, 50)) / len(range(0, n, 50)) for p in pool])
        sc = {p: [(dscore(ptot[p], s) - mm) / msd + .2 * (W[proj[p]["t"]][s] - wm) / ws for s in range(n)] for p in pool}
        pr = _softmax_wins(sc, rng, n, noise)
        out[key] = sorted([card(p, {"pr": pr[p], "sacks": round(mean_of(p, "sacks"), 1), "int": round(mean_of(p, "int"), 1),
                                    "tfl": round(mean_of(p, "tfl"), 1), "ff": round(mean_of(p, "ff"), 1), "pd": round(mean_of(p, "pd"), 1),
                                    "tkl": round(mean_of(p, "tkl")), "pos": proj[p]["pos"]}) for p in pool], key=lambda x: -x["pr"])[:15]
    dmean = {p: sum(dscore(ptot[p], s) for s in range(0, n, 50)) / len(range(0, n, 50)) for p in defs}
    def_award(sorted(defs, key=lambda p: -dmean[p])[:25], "dpoy", .6)
    def_award(sorted([p for p in defs if proj[p]["x"] == 0], key=lambda p: -dmean[p])[:15], "droy", .6)

    # OROY: rookie production, with a nudge for quarterbacks (voters' habit: Daniels, Stroud, Herbert)
    rk = sorted([pid for pid in ptot if proj[pid]["x"] == 0 and proj[pid]["p"] != "DEF"], key=lambda x: -mean_of(x, "fp"))[:15]
    if rk:
        fm, fs = _z([mean_of(p, "fp") for p in rk])
        sc = {p: [(ptot[p]["fp"][s] - fm) / fs + .3 * (W[proj[p]["t"]][s] - wm) / ws + (.3 if proj[p]["pos"] == "QB" else 0) for s in range(n)] for p in rk}
        pr = _softmax_wins(sc, rng, n, .6)
        out["oroy"] = sorted([card(p, {"pr": pr[p], "fp": round(mean_of(p, "fp"), 1),
                                       "pyd": round(mean_of(p, "pyd")) if proj[p]["pos"] == "QB" else None,
                                       "scrim": round(mean_of(p, "ryd") + mean_of(p, "recyd")) if proj[p]["pos"] != "QB" else None,
                                       "td": round(mean_of(p, "rtd") + mean_of(p, "rectd") + mean_of(p, "ptd"), 1)}) for p in rk],
                             key=lambda x: -x["pr"])[:12]

    # CPOY: productive now after a season lost to injury or a big drop-off (the data can't see off-field stories)
    cb = []
    for pid in ptot:
        pp = proj[pid]
        if pp["p"] == "DEF":
            continue
        lg_, lf, tf = pp["last_gp"], pp["last_fppg"], pp["two_back_fppg"]
        why, strength = None, 0
        if lg_ <= 8 and (pp["two_back_gp"] >= 8 or (lf or 0) >= 8 or pp["x"] and pp["x"] >= 3):
            why, strength = f"played {lg_} games last season", (17 - lg_) / 17
        elif lf is not None and tf and tf >= 10 and lf <= tf * .6:
            why, strength = f"fell from {tf:.1f} to {lf:.1f} PPR points a game last season", 1 - lf / tf
        if why and mean_of(pid, "fp") >= 120:
            cb.append((pid, why, strength))
    if cb:
        fm, fs = _z([mean_of(p, "fp") for p, _, _ in cb])
        sc = {p: [(ptot[p]["fp"][s] - fm) / fs + .7 * st + .35 * (W[proj[p]["t"]][s] - wm) / ws for s in range(n)] for p, _, st in cb}
        pr = _softmax_wins(sc, rng, n, .9)
        out["cpoy"] = sorted([card(p, {"pr": pr[p], "fp": round(mean_of(p, "fp"), 1), "why": why}) for p, why, _ in cb], key=lambda x: -x["pr"])[:12]

    # Coach of the Year: wins above what was expected before the season, plus making (and winning) the playoffs
    sc = {}
    for t in TEAMS:
        sc[t] = [(W[t][s] - pre_exp[t]) * .55 + (1.0 if S[t][s] else 0) + (.5 if 1 <= S[t][s] <= 4 else 0) + (.4 if S[t][s] == 1 else 0) for s in range(n)]
    pr = _softmax_wins(sc, rng, n, 1.4)
    out["coy"] = sorted([{"t": t, "coach": coaches.get(t, ""), "pr": pr[t], "pre": round(pre_exp[t], 1), "w": round(sum(W[t]) / n, 1),
                          "po": round(sum(1 for x in S[t] if x) / n, 3)} for t in TEAMS], key=lambda x: -x["pr"])[:15]
    return out


LEADERS = [("pyd", "Passing yards", ("QB",)), ("ptd", "Passing TDs", ("QB",)), ("ryd", "Rushing yards", ("RB", "QB")),
           ("rtd", "Rushing TDs", ("RB", "QB")), ("recyd", "Receiving yards", ("WR", "TE", "RB")),
           ("rectd", "Receiving TDs", ("WR", "TE", "RB")), ("rec", "Receptions", ("WR", "TE", "RB")),
           ("sacks", "Sacks", ("DL", "LB", "DB")), ("int", "Interceptions (defense)", ("DB", "LB"))]
MILESTONES = {"pyd": [4000, 4500, 5000], "ptd": [30, 35, 40], "ryd": [1000, 1250, 1500, 1750], "rtd": [10, 12, 15],
              "recyd": [1000, 1250, 1500], "rectd": [8, 10, 12], "rec": [90, 100, 110], "sacks": [10, 12.5, 15], "int": [5, 6, 8]}


def leaders(proj, ptot, n):
    out = {}
    for k, label, poss in LEADERS:
        pool = [p for p in ptot if proj[p]["pos"] in poss and k in ptot[p] and not (k == "int" and proj[p]["p"] != "DEF")]
        pool = sorted(pool, key=lambda p: -sum(ptot[p][k][::25]))[:14]
        if not pool:
            continue
        wins = defaultdict(float)
        for s in range(n):
            vals = [(ptot[p][k][s], p) for p in pool]
            top = max(v for v, _ in vals)
            ws = [p for v, p in vals if v >= top - 1e-9]
            for p in ws:
                wins[p] += 1 / len(ws)
        rows = []
        for p in pool:
            a = sorted(ptot[p][k])
            rows.append({"id": p, "n": proj[p]["n"], "t": proj[p]["t"], "pos": proj[p]["pos"], "pr": wins[p] / n,
                         "cur": round(proj[p]["cur"][k], 1), "m": round(sum(a) / n, 1), "lo": round(a[int(.1 * n)], 1), "hi": round(a[int(.9 * n)], 1),
                         "gl": len(proj[p]["games"]),
                         "ms": {str(th): round(sum(1 for v in a if v >= th) / n, 3) for th in MILESTONES.get(k, [])}})
        out[k] = {"label": label, "rows": sorted(rows, key=lambda r: -r["pr"])}
    return out


# ------------------------------------------------------------- team notes
def team_notes(data, r, res, remaining, coaches):
    TS = data.get("team_stats") or {}
    lg_off = [TS[t]["off"]["epa"] for t in TS if TS[t].get("off") and isinstance(TS[t]["off"].get("epa"), (int, float))]
    rank = lambda t, side, k, low_good: 1 + sum(1 for u in TS if TS[u].get(side) and isinstance(TS[u][side].get(k), (int, float)) and
                                                   ((TS[u][side][k] < TS[t][side][k]) if low_good else (TS[u][side][k] > TS[t][side][k])))
    rr = sorted(TEAMS, key=lambda t: -r[t])
    sos_p, sos_r = {}, {}
    for t in TEAMS:
        log = (TS.get(t) or {}).get("log") or []
        sos_p[t] = statistics.mean([r[x["opp"]] for x in log if x["opp"] in r]) if log else 0
        sos_r[t] = statistics.mean([r[o] for _, o in remaining.get(t, [])]) if remaining.get(t) else 0
    rk_p = {t: 1 + sum(1 for u in TEAMS if sos_p[u] > sos_p[t]) for t in TEAMS}
    rk_r = {t: 1 + sum(1 for u in TEAMS if sos_r[u] > sos_r[t]) for t in TEAMS}
    out = {}
    for t in TEAMS:
        s = TS.get(t) or {}
        log = s.get("log") or []
        g = len(log)
        notes = []
        if not g:
            out[t] = {"notes": []}
            continue
        pf, pa = s.get("pf", 0), s.get("pa", 0)
        w, l, ti = s.get("w", 0), s.get("l", 0), s.get("t", 0)
        pyth = g * (pf ** 2.37) / ((pf ** 2.37) + (pa ** 2.37)) if pf + pa else g / 2
        luck = (w + .5 * ti) - pyth
        close = [x for x in log if abs(x["pf"] - x["pa"]) <= 8]
        cw = sum(1 for x in close if x["pf"] > x["pa"]); cl = sum(1 for x in close if x["pf"] < x["pa"])
        off, de = s.get("off") or {}, s.get("def") or {}
        to = (de.get("topg") or 0) - (off.get("topg") or 0)
        ro = rank(t, "off", "epa", False) if isinstance(off.get("epa"), (int, float)) else None
        rd = rank(t, "def", "epa", True) if isinstance(de.get("epa"), (int, float)) else None
        sdpa = statistics.pstdev([x["pa"] for x in log]) if g >= 3 else 0
        sdpf = statistics.pstdev([x["pf"] for x in log]) if g >= 3 else 0
        bad_d = [x for x in log if x["pa"] >= 30]
        dud_o = [x for x in log if x["pf"] <= 13]
        pace = (w + .5 * ti) / g * 17
        m = res[t]["w"]

        def add(tag, sign, text, weight):
            notes.append({"tag": tag, "s": sign, "t": text, "wt": round(weight, 2)})
        if abs(luck) >= .9:
            add("Record vs scoring", -1 if luck > 0 else 1,
                f"Their points scored and allowed ({pf}-{pa}) are worth about {pyth:.1f} wins, but they have {w + .5 * ti:g}. "
                + ("They've won more than their scoring says; that usually evens out." if luck > 0 else "They've lost more than their scoring says; that usually turns around."), abs(luck))
        if len(close) >= 3 and abs(cw - cl) >= 3:
            add("Close games", -1 if cw > cl else 1, f"{cw}-{cl} in one-score games (8 points or fewer). Close-game records are mostly luck and drift back toward .500.", abs(cw - cl) * .35)
        if abs(to) >= .5:
            add("Turnovers", -1 if to > 0 else 1, f"Turnover margin of {to:+.1f} a game. Takeaways swing a lot from year to year and half-season to half-season, so this tends to fade.", abs(to))
        if ro and rd:
            if ro <= 10 and rd >= 20:
                add("Offense carries them", 0, f"Offense ranks {_ord(ro)} in EPA per play, defense {_ord(rd)}. "
                    + (f"The defense has given up 30+ in {len(bad_d)} of {g} games; " if bad_d else "") + "games against good offenses are where losses come from.", 1.2)
            elif rd <= 10 and ro >= 20:
                add("Defense carries them", 0, f"Defense ranks {_ord(rd)} in EPA per play allowed, offense {_ord(ro)}. "
                    + (f"The offense scored 13 or fewer in {len(dud_o)} of {g} games. " if dud_o else "") + "Low-scoring teams play more coin-flip games.", 1.2)
            elif ro <= 8 and rd <= 8:
                add("Balanced and good", 1, f"Top 8 on both sides of the ball by EPA per play (offense {_ord(ro)}, defense {_ord(rd)}).", 1.0)
            elif ro >= 24 and rd >= 24:
                add("Struggling on both sides", -1, f"Bottom 9 on both sides by EPA per play (offense {_ord(ro)}, defense {_ord(rd)}).", 1.0)
        if g >= 5 and sdpa >= 10:
            add("Boom-or-bust defense", 0, f"Points allowed swing a lot from game to game (standard deviation {sdpa:.1f}). Good days hide bad ones in the average.", .6)
        if g >= 5 and sdpf >= 10:
            add("Streaky offense", 0, f"Points scored swing a lot (standard deviation {sdpf:.1f}).", .5)
        if remaining.get(t):
            d = rk_p[t] - rk_r[t]
            if abs(d) >= 10:
                add("Schedule", 1 if d < 0 else -1,
                    f"Opponents so far rank {_ord(rk_p[t])} hardest by current ratings; what's left ranks {_ord(rk_r[t])}. "
                    + ("The back half is easier than what they've already faced." if d < 0 else "The back half is tougher than what they've already faced."), abs(d) * .08)
        if abs(m - pace) >= 1.2:
            add("Model vs pace", 1 if m > pace else -1, f"On pace for {pace:.1f} wins at their current win rate; the simulation expects {m:.1f}, because of who they play and how well they've really played.", abs(m - pace) * .5)
        hrem = sum(1 for wk_, o in remaining.get(t, []) if wk_ < 0)
        notes.sort(key=lambda x: -x["wt"])
        out[t] = {"notes": notes, "pyth": round(pyth, 1), "luck": round(luck, 1), "close": [cw, cl], "to": round(to, 2),
                  "ro": ro, "rd": rd, "sdpa": round(sdpa, 1), "sdpf": round(sdpf, 1), "bad_d": len(bad_d), "dud_o": len(dud_o),
                  "sos_p": round(sos_p[t], 2), "sos_r": round(sos_r[t], 2), "rk_sos_p": rk_p[t], "rk_sos_r": rk_r[t],
                  "pace": round(pace, 1), "rank": rr.index(t) + 1}
    return out


def _ord(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# ------------------------------------------------------------- encryption
def _derive(password, salt):
    k = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, KDF_ITER, 64)
    return k[:32], k[32:]


def _stream(key, nonce, n):
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def encrypt(obj, password, salt):
    ke, km = _derive(password, salt)
    nonce = os.urandom(16)
    pt = gzip.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 9)
    ct = bytes(a ^ b for a, b in zip(pt, _stream(ke, nonce, len(pt))))
    tag = hmac.new(km, salt + nonce + ct, hashlib.sha256).digest()
    b = lambda x: base64.b64encode(x).decode()
    return {"v": 1, "kdf": "pbkdf2-sha256", "it": KDF_ITER, "salt": b(salt), "nonce": b(nonce), "ct": b(ct), "tag": b(tag)}


def decrypt(env, password):
    d = lambda x: base64.b64decode(x)
    salt, nonce, ct, tag = d(env["salt"]), d(env["nonce"]), d(env["ct"]), d(env["tag"])
    ke, km = _derive(password, salt)
    if not hmac.compare_digest(hmac.new(km, salt + nonce + ct, hashlib.sha256).digest(), tag):
        return None
    pt = bytes(a ^ b for a, b in zip(ct, _stream(ke, nonce, len(ct))))
    return json.loads(gzip.decompress(pt))


# ------------------------------------------------------------- odds (optional)
def fetch_sb_odds(key):
    """Super Bowl winner prices from The Odds API (free tier covers outrights). One request; fails soft."""
    try:
        url = f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl_super_bowl_winner/odds?apiKey={key}&regions=us&markets=outrights&oddsFormat=american"
        with urllib.request.urlopen(url, timeout=20) as r:
            js = json.loads(r.read())
    except Exception as e:
        print(f"  (futures odds skipped: {e})")
        return None
    names = {}
    best = defaultdict(lambda: None)
    for ev in js:
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                for o in mk.get("outcomes", []):
                    nm, pr = o.get("name"), o.get("price")
                    if nm is None or pr is None:
                        continue
                    names[nm] = 1
                    if best[nm] is None or pr > best[nm][0]:
                        best[nm] = (pr, bk.get("title"))
    return {k: {"price": v[0], "book": v[1]} for k, v in best.items() if v}


# ------------------------------------------------------------- main entry
def build_futures(data, game_rows, password, odds_key=None, n=N_SIMS):
    season = data["season"]
    games = _season_games(game_rows, season)
    done = [g for g in games if g["hs"] is not None]
    rem = [g for g in games if g["hs"] is None]
    if not rem:
        print("  (futures: regular season is over)")
    next_wk = min([g["wk"] for g in rem]) if rem else 19
    r, hfa, rm, rk = ratings_as_of(game_rows, season, next_wk, include_future_lines=True)
    gp = len(done) * 2 / 32
    rsd = rating_sd_for(gp)
    print(f"Futures: ratings through week {next_wk - 1}, {len(rem)} games left, rating uncertainty {rsd:.2f} pts, {n} seasons ...")
    sim = Sim(games, r, hfa, rsd, seed=season)
    res = sim.run(n, keep=True)

    # preseason expectations (ratings from last season only), for Coach of the Year and "vs expectations"
    # last season's results, regressed a third of the way to average (a stand-in for preseason expectations)
    r0, h0 = _fit(_obs(game_rows, season, 1, "mrg", 1.0, 2.0), 4.0)
    r0 = {t: .67 * v for t, v in r0.items()}
    pre_exp = defaultdict(float)
    for g in games:
        p = phi((r0[g["h"]] - r0[g["a"]] + (0 if g["n"] else h0)) / SD_GAME)
        pre_exp[g["h"]] += p; pre_exp[g["a"]] += 1 - p

    remaining = defaultdict(list)
    for g in rem:
        remaining[g["h"]].append((g["wk"], g["a"])); remaining[g["a"]].append((g["wk"], g["h"]))
    for t in remaining:
        remaining[t].sort()
    coaches = {}
    for g in sorted([g for g in game_rows if int(g["season"]) == season], key=lambda g: int(g["week"])):
        if g.get("home_coach"):
            coaches[g["home_team"]] = g["home_coach"]
        if g.get("away_coach"):
            coaches[g["away_team"]] = g["away_coach"]

    proj = player_projections(data, remaining, next_wk)
    # candidates: anyone who could plausibly matter for an award, leader or milestone market
    cand = {}
    by = defaultdict(list)
    for pid, pp in proj.items():
        by[pp["pos"] if pp["p"] != "DEF" else "DEF"].append(pid)
    take = {"QB": ("fp", 32), "RB": ("fp", 40), "WR": ("fp", 55), "TE": ("fp", 20)}
    for pos, (k, m) in take.items():
        for pid in sorted(by[pos], key=lambda x: -proj_mean(proj[x], k))[:m]:
            cand[pid] = proj[pid]
    for pid in sorted(by["DEF"], key=lambda x: -(proj_mean(proj[x], "sacks") + 1.6 * proj_mean(proj[x], "int") + .35 * proj_mean(proj[x], "tfl")))[:70]:
        cand[pid] = proj[pid]
    for pid, pp in proj.items():          # every rookie with real production, and possible comeback cases
        if (pp["x"] == 0 and (proj_mean(pp, "fp") >= 60 if pp["p"] != "DEF" else proj_mean(pp, "sacks") + proj_mean(pp, "int") + .3 * proj_mean(pp, "tfl") >= 3)) \
                or (pp["p"] != "DEF" and pp["last_gp"] <= 8 and proj_mean(pp, "fp") >= 110):
            cand[pid] = pp
    print(f"  simulating {len(cand)} players ...")
    ptot = PlayerSim(cand, n, seed=season + 1).run()
    rng = random.Random(season + 2)
    aw = awards(cand, ptot, sim, coaches, pre_exp, rng, n)
    ld = leaders(cand, ptot, n)
    notes = team_notes(data, r, res, remaining, coaches)

    TS = data.get("team_stats") or {}
    teams = {}
    for t in TEAMS:
        x = res[t]
        s = TS.get(t) or {}
        nxt = remaining.get(t, [])[:1]
        teams[t] = dict(x, r=round(r[t], 2), rm=round(rm[t], 2), rk=round(rk[t], 2), cf=CONF_OF[t], dv=DIV_OF[t],
                        rec=[s.get("w", 0), s.get("l", 0), s.get("t", 0)], pf=s.get("pf"), pa=s.get("pa"),
                        pre=round(pre_exp[t], 1), coach=coaches.get(t), left=len(remaining.get(t, [])),
                        sched=[[wk_, o, round(r[o], 1)] for wk_, o in remaining.get(t, [])], **notes.get(t, {}))
    # each remaining game's win probability (posted line when there is one)
    gl = []
    for g in rem:
        mu = g["sp"] if g["sp"] is not None else r[g["h"]] - r[g["a"]] + (0 if g["n"] else hfa)
        gl.append({"wk": g["wk"], "h": g["h"], "a": g["a"], "ph": round(phi(mu / SD_GAME), 3), "mu": round(mu, 1), "line": g["sp"] is not None})

    payload = {"season": season, "gen": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "n": n, "wk": next_wk,
               "hfa": round(hfa, 2), "rsd": round(rsd, 2), "teams": teams, "games": gl, "awards": aw, "leaders": ld,
               "divs": DIVS, "rating": RATING, "rsd_mult": RATING_SD_MULT}

    # history and optional sportsbook prices need the previous (encrypted) file
    prev, salt = None, None
    if os.path.exists(OUT):
        try:
            env = json.load(open(OUT))
            prev = decrypt(env, password)
            if prev is not None:
                salt = base64.b64decode(env["salt"])     # same password: keep the salt so "remember me" keeps working
        except Exception:
            prev = None
    salt = salt or os.urandom(16)
    # the backtest only covers finished seasons, so rerun it only when the model's settings change
    if prev and prev.get("bt") and prev.get("rating") == RATING and prev.get("rsd_mult") == RATING_SD_MULT:
        payload["bt"] = prev["bt"]
    else:
        print("  backtesting the season simulator (2020-2025) ...")
        try:
            payload["bt"] = backtest(game_rows, seasons=(2020, 2021, 2022, 2023, 2024, 2025), n=1500)
        except Exception as e:
            print(f"  (backtest skipped: {e})")
            payload["bt"] = []
    hist = (prev or {}).get("hist", [])
    day = payload["gen"][:10]
    snap = {"d": day, "wk": next_wk, "t": {t: [round(res[t]["po"], 3), round(res[t]["div"], 3), round(res[t]["conf"], 3), round(res[t]["sb"], 4), round(res[t]["w"], 2)] for t in TEAMS},
            "a": {k: {x.get("n") or x.get("t"): round(x["pr"], 3) for x in v[:6]} for k, v in aw.items()}}
    hist = [h for h in hist if h["d"] != day] + [snap]
    payload["hist"] = hist[-200:]
    odds = (prev or {}).get("odds")
    if odds_key and (not odds or odds.get("d") != day):
        o = fetch_sb_odds(odds_key)
        if o:
            odds = {"d": day, "sb": o}
    payload["odds"] = odds
    env = encrypt(payload, password, salt)
    env["gen"] = payload["gen"]
    with open(OUT, "w") as f:
        json.dump(env, f, separators=(",", ":"))
    print(f"Wrote {OUT} (encrypted) — {len(teams)} teams, {sum(len(v) for v in aw.values())} award rows, {len(ld)} leader markets")
    return payload
