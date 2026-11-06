"""
Weekly matchup board + report card (the self-learning part of Game Script).

Every run (daily, plus after each game window) this:
  1. Grades any finished week that hasn't been graded: every prop on the board
     at kickoff (hit, miss, near miss, push, didn't play), plus "sneaky looks"
     the board left off and "blind spots" where the model leaned the wrong way,
     each with a plain-language diagnosis (volume vs efficiency, teammates,
     snaps, script, weather, quarterback).
  2. Re-fits the model on every graded week: one weight per schematic factor,
     a bias per prop type and a calibration slope per prop type, all shrunk
     toward their starting values (Bayesian ridge), so a few odd weeks can't
     swing it. Each week's before/after is kept with the reasons.
  3. Builds the board for the coming week with the learned model: every
     qualifying player's props scored on matchup (defense vs position, man/zone
     fit, field zones, target funnel, pass/run funnel, pass rush, red zone),
     game script, weather forecast and injuries, against a sportsbook line when
     an ODDS_API_KEY is set, otherwise an estimated line (his median).
  4. Diffs against the previous run: adds, removes and updates are logged with
     the reason (who got hurt, what the forecast did, how the line moved, which
     factor changed). Picks lock at kickoff. Every week stays in the archive.

Weeks that finished before the board existed are backfilled as backtests built
only from games before that week (a few team-level rates use the full season,
and availability uses who actually played) and are labeled as such.

Standard library only, like the rest of the pipeline.
"""
import json, math, os, urllib.request, urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BOARD_FILE, LEARN_FILE = "nfl_board.json", "nfl_learn.json"
PROPS = {"QB": ["pyd", "ptd", "ryd"], "RB": ["ryd", "rec", "recyd"], "WR": ["recyd", "rec"], "TE": ["recyd", "rec"]}
PNAME = {"pyd": "passing yards", "ptd": "passing TDs", "ryd": "rushing yards", "rec": "receptions", "recyd": "receiving yards"}
VOL = {"pyd": "att", "ptd": "att", "ryd": "car", "rec": "tgt", "recyd": "tgt"}
VOLN = {"att": "pass attempts", "car": "carries", "tgt": "targets"}
VOL1 = {"att": "pass attempt", "car": "carry", "tgt": "target"}
SMOOTH = {"pyd": 25.0, "ryd": 8.0, "recyd": 8.0, "rec": 1.0, "ptd": 0.4}
FACTORS = ["def_pos", "script", "funnel", "tgt_funnel", "cov", "zone", "press", "rz", "weather", "injury", "qb_change", "own_status"]
FNAME = {"def_pos": "Defense vs his position", "script": "Game script", "funnel": "Pass or run funnel",
         "tgt_funnel": "Who gets targeted", "cov": "Man or zone fit", "zone": "Where he’s thrown to",
         "press": "Pass rush", "rz": "Red zone defense", "weather": "Weather", "injury": "Teammates out",
         "qb_change": "Quarterback change", "own_status": "His injury status"}
BOARD_SIZE, MIN_EDGE, BREAKEVEN, PER_GAME_CAP, PER_PLAYER_CAP = 30, 0.03, 0.524, 5, 2
TAU_W, TAU_B, TAU_A, A0 = 0.35, 0.08, 0.3, 0.85          # prior widths: factor weights, bias, calibration
DECAY = 0.9                                               # older weeks count a little less
METHOD = "recent-1"                # bump when the model's inputs change: backtests are rebuilt, the live week logs why
PRIOR_GAMES, DEF_PRIOR = 3, 4      # recent form: last season counts as this many games; two seasons back is out

STADIUMS = {  # home team -> (lat, lon); neutral sites by stadium name
    "ARI": (33.5276, -112.2626), "ATL": (33.7554, -84.4008), "BAL": (39.2780, -76.6227), "BUF": (42.7738, -78.7870),
    "CAR": (35.2258, -80.8528), "CHI": (41.8623, -87.6167), "CIN": (39.0955, -84.5161), "CLE": (41.5061, -81.6995),
    "DAL": (32.7473, -97.0945), "DEN": (39.7439, -105.0201), "DET": (42.3400, -83.0456), "GB": (44.5013, -88.0622),
    "HOU": (29.6847, -95.4107), "IND": (39.7601, -86.1639), "JAX": (30.3239, -81.6373), "KC": (39.0489, -94.4839),
    "LV": (36.0909, -115.1833), "LAC": (33.9535, -118.3392), "LA": (33.9535, -118.3392), "MIA": (25.9580, -80.2389),
    "MIN": (44.9737, -93.2577), "NE": (42.0909, -71.2643), "NO": (29.9511, -90.0812), "NYG": (40.8135, -74.0745),
    "NYJ": (40.8135, -74.0745), "PHI": (39.9008, -75.1675), "PIT": (40.4468, -80.0158), "SF": (37.4030, -121.9700),
    "SEA": (47.5952, -122.3316), "TB": (27.9759, -82.5033), "TEN": (36.1665, -86.7713), "WAS": (38.9078, -76.8645)}
NEUTRAL = {"Bernabeu": (40.4531, -3.6883), "Maracana Stadium": (-22.9122, -43.2302),
           "FC Bayern Munich Stadium": (48.2188, 11.6247), "Tottenham Hotspur Stadium": (51.6043, -0.0664),
           "Wembley Stadium": (51.5560, -0.2796), "Melbourne Cricket Ground": (-37.8200, 144.9834),
           "Stade de France": (48.9245, 2.3602), "Estadio Banorte": (19.3029, -99.1505)}


# ---------------------------------------------------------------- small helpers
def _mean(a):
    a = [x for x in a if x is not None]
    return sum(a) / len(a) if a else None


def _median(a):
    s = sorted(a); n = len(s)
    if not n: return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _wmedian(vals, wts):
    s = sorted(zip(vals, wts)); tot = sum(wts); acc = 0.0
    for v, w in s:
        acc += w
        if acc >= tot / 2: return v
    return s[-1][0] if s else None


def half_line(med, mean):
    r = round(med * 2) / 2
    if r % 1: return max(0.5, r)
    return max(0.5, r + (0.5 if mean > r else -0.5))


def _nth_sunday(y, m, n):
    d = datetime(y, m, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def kickoff_utc(g):
    """Schedule times are US Eastern; convert with the US daylight-saving rule."""
    try:
        h, m = (g.get("time") or "13:00").split(":")
        local = datetime.strptime(g["date"], "%Y-%m-%d").replace(hour=int(h), minute=int(m))
    except Exception:
        return None
    y = local.year
    dst = _nth_sunday(y, 3, 2).replace(hour=2) <= local < _nth_sunday(y, 11, 1).replace(hour=2)
    return (local + timedelta(hours=4 if dst else 5)).replace(tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _final(g):
    return g.get("hs") is not None and g.get("as") is not None


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "game-script-nfl/1.0 (portfolio project)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _norm_name(n):
    n = (n or "").lower().replace(".", "").replace("'", "").replace("’", "").replace("-", " ")
    parts = [w for w in n.split() if w not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return " ".join(parts)


# ---------------------------------------------------------------- context
class Ctx:
    """Everything the model reads, with point-in-time views for backtests."""

    def __init__(self, data, states, allowed):
        self.d, self.S, self.season = data, states or {"players": {}, "teams": {}, "league": {"loc": {}}, "cov_seasons": []}, data["season"]
        self.players = [p for p in data["players"] if p["p"] in PROPS]
        self.by_id = {p["id"]: p for p in data["players"]}
        self.by_team = defaultdict(list)
        for p in self.players: self.by_team[p["t"]].append(p)
        self.games = [g for g in data["games"] if g.get("gt", "REG") == "REG"]
        self.TS, self.SCH = data.get("team_stats", {}), data.get("team_scheme", {})
        self.teams = data.get("teams", {})
        self.hist = {}
        for p in self.players:
            h = p.get("hist") or [dict(w, s=self.season) for w in p.get("wk", [])]
            self.hist[p["id"]] = sorted(h, key=lambda x: (x["s"], x["w"]))
        self.wk_snap = {p["id"]: {w["w"]: w.get("sp") for w in p.get("wk", [])} for p in self.players}
        # schedule lookups: (team, week) -> game, opponent
        self.tg = {}
        for g in self.games:
            self.tg[(g["home"], g["wk"])] = g; self.tg[(g["away"], g["wk"])] = g
        # defense-allowed per game from the game log (this season + last season as DEF_PRIOR games), so backtests only see earlier games
        self.allowed = defaultdict(list)
        keys = (allowed or {}).get("keys", {})
        for t, rows in (allowed or {}).get("teams", {}).items():
            for row in rows:
                s, w, players = row[0], row[1], row[6]
                agg = defaultdict(lambda: defaultdict(float))
                for pr in players:
                    pos = pr[1]
                    for k, v in zip(keys.get(pos, []), pr[2:]):
                        agg[pos][k] += v or 0
                self.allowed[t].append(((s, w), agg))
        self._da = {}
        lg = self.S.get("league", {}).get("loc", {}).get("all") or {}
        self.lg_loc = lg
        self.lg_loc_t = sum(v[0] for v in lg.values()) if lg else 0
        cs = str((self.S.get("cov_seasons") or [""])[-1])
        mans = [t.get("cov", {}).get(cs, {}).get("man") for t in self.S.get("teams", {}).values()]
        self.cov_s, self.lg_man = cs, (_mean([m for m in mans if m is not None]) or 0.3)
        self.lg_ts = {k: _mean([v.get("def", {}).get(k) for v in self.TS.values()]) for k in ("pass_rate",)}
        self.lg_sch = {k: _mean([v.get("def", {}).get(k) for v in self.SCH.values()]) for k in ("pressure_forced_pct", "rz_td_pct_allowed")}
        self.lg_off_pass = _mean([v.get("off", {}).get("pass_rate") for v in self.TS.values()]) or 57.0
        self.script_b = (data.get("script") or {}).get("b", -0.0027)

    def pf_before(self, t, W):
        """Points per game before week W (point-in-time for backtests; the full season so far when live)."""
        lg = [x["pf"] for x in (self.TS.get(t) or {}).get("log", []) if x.get("wk", 99) < W and x.get("pf") is not None]
        return (sum(lg) / len(lg)) if lg else (self.TS.get(t) or {}).get("pfpg")

    def def_pass_rate(self, before):
        """Share of opponents' plays that were passes (attempts / (attempts + carries)), recent form, point-in-time."""
        ck = ("dpr",) + before
        if ck in self._da: return self._da[ck]
        out = {}
        for t, gs in self.allowed.items():
            cur_g = [a for sw, a in gs if sw < before and sw[0] == before[0]]
            last_g = [a for sw, a in gs if sw[0] == before[0] - 1]
            def tot(g):
                att = sum(a.get("QB", {}).get("att", 0) for a in g)
                car = sum(a.get(p, {}).get("car", 0) for a in g for p in ("RB", "WR", "TE"))
                return att, car
            ca, cc = tot(cur_g); la, lc = tot(last_g)
            w = (DEF_PRIOR / len(last_g)) if last_g else 0
            A, Cr = ca + w * la, cc + w * lc
            if A + Cr: out[t] = 100 * A / (A + Cr)
        lg = _mean(list(out.values()))
        self._da[ck] = (out, lg)
        return out, lg

    def script_center(self, W):
        """This week's typical implied-points-to-scoring ratio, so the factor is relative to the league that week."""
        if not hasattr(self, "_scc"): self._scc = {}
        if W not in self._scc:
            rs = []
            for g in self.games:
                if g["wk"] != W or g.get("spread") is None or g.get("total") is None: continue
                for t, sp in ((g["home"], g["spread"]), (g["away"], -g["spread"])):
                    pf = self.pf_before(t, W)
                    if pf: rs.append(((g["total"] + sp) / 2) / pf)
            self._scc[W] = (_mean(rs) or 1.0)
        return self._scc[W]

    def nick(self, t):
        return (self.teams.get(t) or {}).get("nick", t)

    def da(self, before):
        """Defense allowed per game {team: {pos: {k: per game}}} using games before (season, week)."""
        if before in self._da: return self._da[before]
        out = {}
        for t, gs in self.allowed.items():
            cur_g = [a for sw, a in gs if sw < before and sw[0] == before[0]]
            last_g = [a for sw, a in gs if sw[0] == before[0] - 1]
            if not cur_g and not last_g: continue
            o = defaultdict(dict)
            for pos in ("QB", "RB", "WR", "TE"):
                ks = set()
                for a in cur_g + last_g: ks |= set(a.get(pos, {}).keys())
                for k in ks:
                    c = sum(a.get(pos, {}).get(k, 0) for a in cur_g)
                    if last_g:
                        l = sum(a.get(pos, {}).get(k, 0) for a in last_g) / len(last_g)
                        o[pos][k] = (c + DEF_PRIOR * l) / (len(cur_g) + DEF_PRIOR)
                    else:
                        o[pos][k] = c / len(cur_g)
            out[t] = o
        lg = defaultdict(dict)
        for pos in ("QB", "RB", "WR", "TE"):
            for k in ("tgt", "rec", "recyd", "car", "ryd", "pyd", "ptd", "att"):
                v = [o[pos][k] for o in out.values() if pos in o and k in o[pos]]
                if v: lg[pos][k] = sum(v) / len(v)
        self._da[before] = (out, lg)
        return out, lg

    def da_rank(self, before, team, pos, k):
        out, _ = self.da(before)
        vals = [(t, o[pos][k]) for t, o in out.items() if pos in o and k in o[pos]]
        mine = out.get(team, {}).get(pos, {}).get(k)
        if mine is None: return None
        return 1 + sum(1 for _, v in vals if v < mine)

    def usual_qb(self, t, before_wk):
        c = defaultdict(int)
        for g in self.games:
            if g["wk"] < before_wk and _final(g) and t in (g["home"], g["away"]):
                n = g["hqb"] if g["home"] == t else g["aqb"]
                if n: c[n] += 1
        return max(c.items(), key=lambda x: x[1])[0] if c else None


# ---------------------------------------------------------------- one player's props for one game
def _games_before(ctx, p, W):
    """His games before week W this season (with his current team) and last season. Two seasons back is out."""
    out = []
    for h in ctx.hist[p["id"]]:
        if h["s"] < ctx.season - 1: continue
        if h["s"] < ctx.season: out.append(h)
        elif h["w"] < W:
            g = ctx.tg.get((p["t"], h["w"]))
            if g and h.get("opp") in (g["home"], g["away"]) and h.get("opp") != p["t"]: out.append(h)
    return out


def _val(h, k):
    v = h.get(k)
    return float(v) if v is not None else 0.0


def baseline(ctx, p, k, games):
    if p["p"] == "QB" and k in ("pyd", "ptd"): games = [h for h in games if _val(h, "att") >= 10]
    """Recent form: this season's average, steadied by last season counted as PRIOR_GAMES games
    (one game if his role clearly changed). By midseason it is almost all this season."""
    cur = [h for h in games if h["s"] == ctx.season]
    last = [h for h in games if h["s"] == ctx.season - 1]
    if not cur and not last: return None
    vk = VOL.get(k, k)
    cv, lv = _mean([_val(h, vk) for h in cur]), _mean([_val(h, vk) for h in last])
    changed = len(last) >= 4 and cv is not None and (lv or 0) > 0 and not (0.67 <= cv / lv <= 1.5)
    m = 1 if changed else PRIOR_GAMES
    mc, ml = _mean([_val(h, k) for h in cur]), _mean([_val(h, k) for h in last])
    if mc is None: return ml
    if ml is None: return mc
    return (len(cur) * mc + m * ml) / (len(cur) + m)


def factors(ctx, p, k, g, W, mode, avail, wx):
    """{name: (log multiplier, text)} for every factor that applies."""
    t, pos, opp = p["t"], p["p"], (g["away"] if g["home"] == p["t"] else g["home"])
    before = (ctx.season, W)
    da, lg = ctx.da(before)
    on, me = ctx.nick(opp), ctx.nick(t)
    F = {}
    passing = k in ("pyd", "ptd") or (k in ("rec", "recyd"))
    rushing = k == "ryd" and pos == "RB"
    # defense vs position, on this stat
    dk = k if not (pos == "QB" and k == "ryd") else "ryd"
    dv, lv = da.get(opp, {}).get(pos, {}).get(dk), lg.get(pos, {}).get(dk)
    if dv is not None and lv and k != "ptd":      # TDs: red zone + implied points already say this
        m = _clamp(1 + 0.5 * (dv / lv - 1), 0.8, 1.2)
        rk = ctx.da_rank(before, opp, pos, dk)
        F["def_pos"] = (math.log(m), f"{on} allow {dv:.1f} {PNAME[k]} a game to {pos}s this season, steadied by last season ({_ord(rk)} fewest of 32; league {lv:.1f}). Half the gap is applied.")
    # game script: implied points and spread
    sp = g.get("spread"); tot = g.get("total")
    if sp is not None and tot is not None:
        sp_for = sp if g["home"] == t else -sp
        imp = (tot + sp_for) / 2
        pf = ctx.pf_before(t, W)
        ratio = (imp / pf / ctx.script_center(W)) if pf else 1.0
        m = _clamp(ratio ** (0.6 if k == "ptd" else 0.3), 0.85, 1.15)
        pr = ((ctx.TS.get(t) or {}).get("off", {}).get("pass_rate") or ctx.lg_off_pass) / 100
        dp = ctx.script_b * sp_for                               # change in pass rate from the spread
        if passing: m *= _clamp(1 + dp / pr, 0.93, 1.07)
        elif rushing: m *= _clamp(1 - dp / (1 - pr), 0.93, 1.07)
        F["script"] = (math.log(m), f"{me} are {'favored by ' + _n(sp_for) if sp_for > 0 else 'underdogs by ' + _n(-sp_for) if sp_for < 0 else 'a pick’em'} with about {imp:.1f} implied points (they average {pf or 0:.1f}). "
                       + ("Favorites run more late; underdogs throw more." if passing or rushing else ""))
    # pass/run funnel
    dpr, lgp = ctx.def_pass_rate(before)
    r_def = dpr.get(opp)
    if r_def is not None and lgp and (passing or rushing):
        pr = ((ctx.TS.get(t) or {}).get("off", {}).get("pass_rate") or ctx.lg_off_pass) / 100
        shift = _clamp(0.35 * (r_def - lgp) / 100, -0.035, 0.035)
        m = (pr + shift) / pr if passing else (1 - pr - shift) / (1 - pr)
        if abs(m - 1) >= 0.003:
            F["funnel"] = (math.log(m), f"Offenses throw on {r_def:.1f}% of plays against {on} (league {lgp:.1f}%): {'a pass funnel' if shift > 0 else 'a run funnel'}. A third of the gap is applied.")
    # target funnel by position
    if k in ("rec", "recyd") and pos in ("WR", "TE", "RB"):
        od = da.get(opp, {})
        tot_t = sum((od.get(q, {}) or {}).get("tgt", 0) for q in ("WR", "TE", "RB"))
        lg_t = sum(lg.get(q, {}).get("tgt", 0) for q in ("WR", "TE", "RB"))
        if tot_t and lg_t and od.get(pos, {}).get("tgt") is not None:
            sh, lsh = od[pos]["tgt"] / tot_t, lg[pos]["tgt"] / lg_t
            m = _clamp(1 + 0.5 * (sh / lsh - 1), 0.88, 1.12)
            F["tgt_funnel"] = (math.log(m), f"{on} let {sh * 100:.0f}% of targets go to {pos}s (league {lsh * 100:.0f}%).")
    sx = ctx.S.get("players", {}).get(p["id"]) or {}
    # man/zone fit
    if k in ("rec", "recyd") and pos in ("WR", "TE"):
        v, c = sx.get("vs"), (ctx.S.get("teams", {}).get(opp, {}).get("cov") or {}).get(ctx.cov_s)
        if v and v.get("M") and v.get("Z") and c and c.get("man") is not None and v["M"][0] >= 12 and v["Z"][0] >= 12:
            nM, nZ = v["M"][0], v["Z"][0]
            mm = 0.75 * c["man"] + 0.25 * ctx.lg_man
            if k == "recyd":
                a = (v["M"][2] + v["Z"][2]) / (nM + nZ); yM = (v["M"][2] + 40 * a) / (nM + 40); yZ = (v["Z"][2] + 40 * a) / (nZ + 40)
            else:
                a = (v["M"][1] + v["Z"][1]) / (nM + nZ); yM = (v["M"][1] + 40 * a) / (nM + 40); yZ = (v["Z"][1] + 40 * a) / (nZ + 40)
            m = _clamp((mm * yM + (1 - mm) * yZ) / (ctx.lg_man * yM + (1 - ctx.lg_man) * yZ), 0.92, 1.08)
            F["cov"] = (math.log(m), f"{on} played man on {c['man'] * 100:.0f}% of {ctx.cov_s} dropbacks (league {ctx.lg_man * 100:.0f}%). He gets {v['M'][2] / nM:.1f} yards per target against man ({nM}) and {v['Z'][2] / nZ:.1f} against zone ({nZ}).")
    # field zones
    if k == "recyd" and pos in ("WR", "TE") and sx.get("loc") and ctx.lg_loc:
        D = (ctx.S.get("teams", {}).get(opp, {}).get("loc_def") or {}).get("all")
        if D:
            deep = sx.get("deep") if sx.get("deep") is not None else 0.2
            pe = pl = qe = ql = 0.0; best = None
            for i, sd in enumerate("lmr"):
                for dp_ in "sd":
                    z = sd + dp_; L = ctx.lg_loc.get(z)
                    if not L or not L[0]: continue
                    ly = L[2] / L[0]; Dz = D.get(z) or [0, 0, 0]; dy = (Dz[2] + 60 * ly) / (Dz[0] + 60)
                    w = (sx["loc"][i] or 0) * (deep if dp_ == "d" else 1 - deep); q = L[0] / ctx.lg_loc_t
                    pe += w * dy; pl += w * ly; qe += q * dy; ql += q * ly
                    if best is None or w > best[1]: best = (z, w, Dz[2] / Dz[0] if Dz[0] else None, ly)
            if pl and ql:
                m = _clamp(1 + 0.5 * ((pe / pl) / (qe / ql) - 1), 0.93, 1.07)
                zn = {"ls": "short left", "ms": "short middle", "rs": "short right", "ld": "deep left", "md": "deep middle", "rd": "deep right"}[best[0]]
                F["zone"] = (math.log(m), f"His most common target is {zn}" + (f", where {on} allow {best[2]:.1f} yards per target (league {best[3]:.1f})" if best[2] else "") + f". {deep * 100:.0f}% of his targets travel 15+ yards.")
    # pass rush
    s_def = (ctx.SCH.get(opp) or {}).get("def", {})
    if k == "pyd" and s_def.get("pressure_forced_pct") is not None and ctx.lg_sch.get("pressure_forced_pct"):
        r = s_def["pressure_forced_pct"] / ctx.lg_sch["pressure_forced_pct"]
        m = _clamp(1 - 0.12 * (r - 1), 0.95, 1.05)
        F["press"] = (math.log(m), f"{on} pressure the quarterback on {s_def['pressure_forced_pct']:.1f}% of dropbacks (league {ctx.lg_sch['pressure_forced_pct']:.1f}%).")
    # red zone
    if k == "ptd" and s_def.get("rz_td_pct_allowed") is not None and ctx.lg_sch.get("rz_td_pct_allowed"):
        n = s_def.get("rz_trips_allowed") or 0; lgv = ctx.lg_sch["rz_td_pct_allowed"]
        r = (s_def["rz_td_pct_allowed"] * n + lgv * 15) / (n + 15)
        m = _clamp(1 + 0.6 * (r / lgv - 1), 0.85, 1.15)
        F["rz"] = (math.log(m), f"{on} allow touchdowns on {s_def['rz_td_pct_allowed']:.1f}% of red-zone trips ({n} trips; league {lgv:.1f}%).")
    # weather
    if wx and (passing or rushing or k == "ryd"):
        wind, temp, pp = wx.get("wind"), wx.get("temp"), wx.get("pp")
        m = 1.0; bits = []
        if wind is not None and wind >= 12:
            if passing: m *= _clamp(1 - 0.012 * (wind - 12), 0.82, 1)
            elif rushing: m *= _clamp(1 + 0.005 * (wind - 12), 1, 1.06)
            bits.append(f"wind {wind:.0f} mph")
        if temp is not None and temp <= 25 and passing:
            m *= 0.97; bits.append(f"{temp:.0f}°F")
        if pp is not None and pp >= 60 and passing:
            m *= 0.97; bits.append(f"{pp:.0f}% chance of rain or snow")
        if bits and abs(m - 1) > 0.001:
            src = "Forecast at kickoff" if mode == "live" else "Game-time weather"
            F["weather"] = (math.log(m), f"{src}: {', '.join(bits)}. " + ("Wind and wet balls cut passing, especially deep." if passing else "Bad weather tilts teams toward the run."))
    # teammates out (only the part of his sample where they played together)
    vk = VOL.get(k)
    if vk in ("tgt", "car") and avail is not None:
        mine = _games_before(ctx, p, W); keyset = {(h["s"], h["w"]) for h in mine}
        out_names, total, together = [], 0.0, []
        for m_ in ctx.by_team[t]:
            if m_["id"] == p["id"] or m_["p"] == "QB": continue
            if avail.get(m_["id"], True): continue
            msx = (ctx.S.get("players", {}).get(m_["id"]) or {}).get("sh") or {}
            share = (msx.get("tb") if vk == "tgt" else msx.get("cb")) or 0
            if share < (0.07 if vk == "tgt" else 0.15): continue
            mg = {(h["s"], h["w"]) for h in _games_before(ctx, m_, W)}
            frac = len(keyset & mg) / len(keyset) if keyset else 0
            total += share; together.append(frac); out_names.append(f"{m_['n']} ({share * 100:.0f}% of {'targets' if vk == 'tgt' else 'carries'})")
        if total > 0:
            frac = _mean(together) or 0
            m = 1 + (1 / (1 - 0.85 * min(total, 0.6)) - 1) * frac
            if m > 1.005:
                F["injury"] = (math.log(m), f"Out: {', '.join(out_names)}. He played alongside them in {frac * 100:.0f}% of his games, so that part of his sample gets the extra {'targets' if vk == 'tgt' else 'carries'}.")
    # quarterback change (receivers)
    if k in ("rec", "recyd") and avail is not None:
        uq = ctx.usual_qb(t, W)
        starter = next((m_ for m_ in ctx.by_team[t] if m_["n"] == uq), None)
        if starter and not avail.get(starter["id"], True):
            m = 0.88 if k == "recyd" else 0.94
            F["qb_change"] = (math.log(m), f"{uq} is out, so a backup starts at quarterback. Backups average about 12% fewer yards per target.")
    # his own status
    if mode == "live" and p.get("inj") and p["inj"].get("wk") == W and p["inj"].get("st") == "Questionable":
        F["own_status"] = (math.log(0.94), f"He’s Questionable ({p['inj'].get('i') or 'injury'}; {p['inj'].get('pr') or 'practice status unknown'}). Limited snaps are a real risk.")
    return F


def _ord(n):
    if not n: return "—"
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _n(x):
    return f"{x:g}"


def prob_over(vals, wts, base, proj, line, k):
    """Chance of going over: his own games rescaled to the projection, blended with a normal curve."""
    if not vals: return 0.5
    sc = proj / base if base > 0 else 1.0
    xs = [v * sc for v in vals]; W = sum(wts)
    emp = sum(w for x, w in zip(xs, wts) if x > line) / W + 0.5 * sum(w for x, w in zip(xs, wts) if x == line) / W
    mu = sum(x * w for x, w in zip(xs, wts)) / W
    sd = math.sqrt(sum(w * (x - mu) ** 2 for x, w in zip(xs, wts)) / W)
    sd = max(sd, {"pyd": 35, "ryd": 12, "recyd": 14, "rec": 1.3, "ptd": 0.7}[k], 0.35 * proj)
    nrm = 1 - _phi((line - proj) / sd)
    a = min(0.7, len(vals) / 25)
    return _clamp(a * emp + (1 - a) * nrm, 0.02, 0.98)


def candidates(ctx, W, mode, model, wxs, lines, avail_fn, now=None):
    """Every qualifying prop for week W games that haven't kicked off (live) or all of them (backtest)."""
    out = {}
    wts_f, bias, calib = model["w"], model["b"], model["a"]
    for g in [x for x in ctx.games if x["wk"] == W]:
        if mode == "live":
            ko = kickoff_utc(g)
            if ko and now and ko <= now: continue
        avail = avail_fn(g)
        for t in (g["home"], g["away"]):
            for p in ctx.by_team[t]:
                if not avail.get(p["id"], True): continue
                games = _games_before(ctx, p, W)
                cur = [h for h in games if h["s"] == ctx.season]
                if len(cur) < 2 or len(games) < 4: continue
                cv = lambda k: _mean([_val(h, k) for h in cur]) or 0
                pos = p["p"]
                if (pos == "QB" and (cv("att") < 20 or len(cur) < 3)) or (pos == "RB" and cv("car") < 5 and cv("tgt") < 3) or (pos in ("WR", "TE") and cv("tgt") < 3): continue
                for k in PROPS[pos]:
                    if pos == "QB" and k == "ryd" and cv("ryd") < 15: continue
                    if pos == "RB" and k in ("rec", "recyd") and cv("tgt") < 2.5: continue
                    if pos == "RB" and k == "ryd" and cv("car") < 7: continue
                    base = baseline(ctx, p, k, games)
                    if base is None or base <= 0: continue
                    if base < {"pyd": 150, "ryd": 15, "recyd": 15, "rec": 1.5, "ptd": 0.4}[k]: continue
                    F = factors(ctx, p, k, g, W, mode, avail, wxs.get(g["id"]))
                    lsum = _clamp(sum(wts_f.get(f, 1.0) * F[f][0] for f in F), -0.3, 0.3)   # factors overlap; cap the stack
                    proj = base * math.exp(lsum + bias.get(k, 0.0))
                    proj0 = base * math.exp(sum(F[f][0] for f in F))
                    vb = baseline(ctx, p, VOL[k], games) or 0
                    vproj = vb * math.exp(sum(wts_f.get(f, 1.0) * F[f][0] for f in F if f in ("script", "funnel", "tgt_funnel", "injury", "weather", "own_status")))
                    pg = [h for h in games if _val(h, "att") >= 10] if (pos == "QB" and k in ("pyd", "ptd")) else games
                    vals = [_val(h, k) for h in pg]
                    nl = sum(1 for h in pg if h["s"] != ctx.season) or 1
                    wts = [1.0 if h["s"] == ctx.season else PRIOR_GAMES / nl for h in pg]   # last season = 3 games' worth
                    key = f"{p['id']}|{k}"
                    bl = lines.get(key)
                    if bl: line, lsrc = bl["line"], "book"
                    else:
                        # no sportsbook line: use his matchup-neutral expectation (the 50% point of his own
                        # games rescaled to his baseline), so any edge comes from this matchup, not a hot start
                        mv = (sum(v * w for v, w in zip(vals, wts)) / sum(wts)) if vals else base
                        nb = math.exp(bias.get(k, 0.0))           # what the model learned about his kind of baseline
                        # weighted exactly like the projection, so a stale line can't fake an edge
                        med = (_wmedian(vals, wts) or 0) * (base / mv if mv else 1) * nb
                        line, lsrc = half_line(med, base * nb), "est"
                    nb = math.exp(bias.get(k, 0.0))
                    po = prob_over(vals, wts, base, proj, line, k)
                    po0 = prob_over(vals, wts, base, proj0, line, k)
                    pn = prob_over(vals, wts, base, base * nb, line, k)     # same line, no matchup
                    a = calib.get(k, A0)
                    cal = lambda x: 0.5 + a * (x - 0.5)
                    # break-even: a book's price if we have one, otherwise what this line wins with no matchup plus the house cut
                    def be(side):
                        if bl and bl.get("over" if side == "over" else "under") is not None:
                            pr = bl["over" if side == "over" else "under"]
                            return (-pr / (-pr + 100)) if pr < 0 else (100 / (pr + 100))
                        if bl: return BREAKEVEN
                        return cal(pn if side == "over" else 1 - pn) + (BREAKEVEN - 0.5)
                    eo, eu = cal(po) - be("over"), cal(1 - po) - be("under")
                    side = "over" if eo >= eu else "under"
                    praw = po if side == "over" else 1 - po
                    pc = cal(praw)
                    p0 = 0.5 + A0 * ((po0 if side == "over" else 1 - po0) - 0.5)
                    curv = [_val(h, k) for h in pg if h["s"] == ctx.season]
                    hit_n = sum(1 for v in curv if (v > line if side == "over" else v < line))
                    edge = pc - be(side)
                    out[key] = {"pid": p["id"], "n": p["n"], "t": t, "opp": g["away"] if g["home"] == t else g["home"], "pos": pos,
                                "gid": g["id"], "k": k, "side": side, "line": line, "lsrc": lsrc, "base": round(base, 2),
                                "proj": round(proj, 2), "vproj": round(vproj, 2), "vk": VOL[k], "praw": round(praw, 4), "p": round(pc, 4),
                                "p0": round(p0, 4), "edge": round(edge, 4), "be": round(be(side), 4), "sc": round(edge * min(1, len(games) / 10), 4),
                                "hits": [hit_n, len(curv)], "fx": {f: round(F[f][0], 4) for f in F},
                                "why": {f: F[f][1] for f in F}}
                    if bl: out[key]["odds"] = {"o": bl.get("over"), "u": bl.get("under"), "books": bl.get("n")}
    return out


def select(cands):
    """The board: strongest edges, a few per game and at most two props per player."""
    ranked = sorted((c for c in cands.values() if c["edge"] >= MIN_EDGE and c["p"] >= 0.52), key=lambda c: -c["sc"])
    per_g, per_p, keep = defaultdict(int), defaultdict(int), []
    for c in ranked:
        if per_g[c["gid"]] >= PER_GAME_CAP or per_p[c["pid"]] >= PER_PLAYER_CAP: continue
        keep.append(c); per_g[c["gid"]] += 1; per_p[c["pid"]] += 1
        if len(keep) >= BOARD_SIZE: break
    return keep


# ---------------------------------------------------------------- explanations
def _signed(c, f):
    """Effect of a factor on the side we lean, in percent of the projection."""
    x = c["fx"].get(f, 0)
    return x if c["side"] == "over" else -x


def top_reasons(c, n=3, model=None):
    w = (model or {}).get("w", {})
    fs = sorted(c["fx"], key=lambda f: -abs(c["fx"][f] * w.get(f, 1.0)))
    out = []
    for f in fs:
        if abs(c["fx"][f]) < 0.012: continue
        eff = (math.exp(c["fx"][f] * w.get(f, 1.0)) - 1) * 100
        helps = _signed(c, f) > 0
        out.append({"f": f, "l": FNAME[f], "e": round(eff, 1), "helps": helps, "t": c["why"][f]})
        if len(out) >= n: break
    return out


def headline(c, model):
    r = [x for x in top_reasons(c, 6, model) if x["helps"]]
    side = "Over" if c["side"] == "over" else "Under"
    if not r: return f"{side} {c['line']:g} {PNAME[c['k']]}: his own track record does the work"
    return f"{side} {c['line']:g} {PNAME[c['k']]}: {r[0]['l'].lower()} ({'+' if r[0]['e'] > 0 else ''}{r[0]['e']:.0f}%)"


def _biggest_change(old, new):
    fs = set(old.get("fx", {})) | set(new.get("fx", {}))
    best = None
    for f in fs:
        d = new.get("fx", {}).get(f, 0) - old.get("fx", {}).get(f, 0)
        if abs(d) >= 0.01 and (best is None or abs(d) > abs(best[1])): best = (f, d)
    return best


def change_reason(old, new):
    bits = []
    if old and new and old.get("line") != new.get("line"):
        bits.append(f"line moved {old['line']:g} → {new['line']:g}" + (" (sportsbook)" if new.get("lsrc") == "book" else ""))
    bc = _biggest_change(old or {}, new or {}) if old and new else None
    if bc:
        f, d = bc
        txt = (new["why"].get(f) if new["fx"].get(f) else (old.get("why", {}).get(f, "")))
        verb = "now" if new["fx"].get(f) else "no longer"
        bits.append(f"{FNAME[f].lower()} {verb} {'helps' if (d > 0) == (new['side'] == 'over') else 'hurts'} ({'+' if d > 0 else ''}{(math.exp(d) - 1) * 100:.0f}%): {txt}")
    return "; ".join(bits)


# ---------------------------------------------------------------- weather and odds (both optional, both free)
def fetch_weather(games, now):
    out = {}
    for g in games:
        if g.get("roof") in ("dome", "closed"): continue
        ko = kickoff_utc(g)
        if not ko or ko < now or ko - now > timedelta(days=7): continue
        ll = NEUTRAL.get(g.get("stad")) if g.get("loc") == "Neutral" else STADIUMS.get(g["home"])
        if not ll: continue
        q = urllib.parse.urlencode({"latitude": ll[0], "longitude": ll[1], "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,wind_gusts_10m",
                                    "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "UTC",
                                    "start_date": ko.strftime("%Y-%m-%d"), "end_date": ko.strftime("%Y-%m-%d")})
        try:
            j = _get_json("https://api.open-meteo.com/v1/forecast?" + q)
            hrs = j["hourly"]["time"]; target = ko.strftime("%Y-%m-%dT%H:00")
            i = hrs.index(target) if target in hrs else min(range(len(hrs)), key=lambda x: abs(int(hrs[x][11:13]) - ko.hour))
            # average kickoff hour and the next two (the game, not just the coin toss)
            rng = range(i, min(i + 3, len(hrs)))
            pick = lambda key: _mean([j["hourly"][key][x] for x in rng if j["hourly"][key][x] is not None])
            out[g["id"]] = {"temp": round(pick("temperature_2m") or 0), "wind": round(pick("wind_speed_10m") or 0),
                            "gust": round(pick("wind_gusts_10m") or 0), "pp": round(pick("precipitation_probability") or 0),
                            "at": _iso(now), "src": "Open-Meteo forecast"}
        except Exception as e:
            print(f"  weather skipped for {g['id']}: {e}")
    return out


# four markets x ~14 games = ~56 credits a pull; two pulls a week stays under the free 500 a month
ODDS_MARKETS = {"player_pass_yds": "pyd", "player_rush_yds": "ryd", "player_receptions": "rec", "player_reception_yds": "recyd"}


def fetch_lines(key, ctx, games):
    """Sportsbook player-prop lines from The Odds API (free tier: 500 credits a month; this uses ~4 per game)."""
    out = {}
    name_to_team = {v.get("name"): t for t, v in ctx.teams.items()}
    try:
        events = _get_json("https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events?" + urllib.parse.urlencode({"apiKey": key}))
    except Exception as e:
        print(f"  odds skipped: {e}"); return out
    gid = {(g["home"], g["away"]): g for g in games}
    for ev in events:
        h, a = name_to_team.get(ev.get("home_team")), name_to_team.get(ev.get("away_team"))
        g = gid.get((h, a))
        if not g: continue
        try:
            j = _get_json(f"https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{ev['id']}/odds?" + urllib.parse.urlencode(
                {"apiKey": key, "regions": "us", "markets": ",".join(ODDS_MARKETS), "oddsFormat": "american"}))
        except Exception as e:
            print(f"  odds skipped for {g['id']}: {e}"); continue
        roster = {_norm_name(p["n"]): p for p in ctx.by_team[h] + ctx.by_team[a]}
        acc = defaultdict(lambda: {"pts": [], "o": [], "u": []})
        for bk in j.get("bookmakers", []):
            for mk in bk.get("markets", []):
                k = ODDS_MARKETS.get(mk.get("key"))
                if not k: continue
                for oc in mk.get("outcomes", []):
                    p = roster.get(_norm_name(oc.get("description")))
                    if not p or oc.get("point") is None: continue
                    A = acc[f"{p['id']}|{k}"]
                    if oc.get("name") == "Over": A["pts"].append(oc["point"]); A["o"].append(oc.get("price"))
                    elif oc.get("name") == "Under": A["u"].append(oc.get("price"))
        for kk, A in acc.items():
            if A["pts"]:
                out[kk] = {"line": _median(A["pts"]), "over": _median([x for x in A["o"] if x is not None]) if A["o"] else None,
                           "under": _median([x for x in A["u"] if x is not None]) if A["u"] else None, "n": len(A["pts"])}
    return out


# ---------------------------------------------------------------- grading and learning
def actual(ctx, pid, W, k):
    for h in ctx.hist.get(pid, []):
        if h["s"] == ctx.season and h["w"] == W:
            return _val(h, k), h
    return None, None


def near_miss(k, line, act):
    return abs(act - line) <= ({"pyd": 15, "ryd": 8, "recyd": 8, "rec": 1, "ptd": 0.5}[k])


def diagnose(ctx, c, W, act, h):
    """Why a prop landed where it did, in plain words."""
    out = []
    pid, k = c["pid"], c["k"]
    p = ctx.by_id.get(pid) or {}
    vk = c.get("vk") or VOL[k]
    if h is not None:
        va = _val(h, vk)
        if c.get("vproj"):
            r = va / c["vproj"] if c["vproj"] else 1
            if r <= 0.7: out.append(f"Volume came up short: {va:.0f} {VOL1[vk] if va == 1 else VOLN[vk]} vs {c['vproj']:.1f} projected.")
            elif r >= 1.3: out.append(f"Volume spiked: {va:.0f} {VOLN[vk]} vs {c['vproj']:.1f} projected.")
            else:
                eff = act / va if va else 0
                exp_eff = c["proj"] / c["vproj"] if c["vproj"] else 0
                if k in ("recyd", "ryd", "pyd") and exp_eff:
                    if eff <= 0.7 * exp_eff: out.append(f"Volume was there ({va:.0f} {VOLN[vk]}) but efficiency wasn’t: {eff:.1f} yards each vs {exp_eff:.1f} expected.")
                    elif eff >= 1.3 * exp_eff: out.append(f"Big plays: {eff:.1f} yards per {VOL1[vk]} on normal volume ({va:.0f}).")
        sp = ctx.wk_snap.get(pid, {}).get(W)
        usual = _mean([v for w, v in ctx.wk_snap.get(pid, {}).items() if w != W and v is not None])
        if sp is not None and usual and sp < 0.65 * usual and usual >= 45:
            out.append(f"Played {sp}% of snaps (usually {usual:.0f}%): an early exit or a smaller role.")
    g = ctx.tg.get((c["t"], W))
    if g and _final(g):
        mg = (g["hs"] - g["as"]) if g["home"] == c["t"] else (g["as"] - g["hs"])
        if mg >= 17: out.append(f"{ctx.nick(c['t'])} won by {mg}: a blowout that usually means more running late.")
        elif mg <= -17: out.append(f"{ctx.nick(c['t'])} lost by {-mg}: trailing big means more passing.")
        uq = ctx.usual_qb(c["t"], W); qn = g["hqb"] if g["home"] == c["t"] else g["aqb"]
        if uq and qn and uq != qn and p.get("p") != "QB": out.append(f"{qn} started at quarterback instead of {uq}.")
        if g.get("wind") is not None and g["wind"] >= 15 and g.get("roof") not in ("dome", "closed"): out.append(f"Wind {g['wind']} mph at kickoff.")
    # teammates who sat that week and had played before
    for m_ in ctx.by_team.get(c["t"], []):
        if m_["id"] == pid or m_["p"] not in ("WR", "TE", "RB"): continue
        mh = [h_ for h_ in ctx.hist.get(m_["id"], []) if h_["s"] == ctx.season]
        if len(mh) < 2: continue
        tg = _mean([_val(h_, "tgt") for h_ in mh]); car = _mean([_val(h_, "car") for h_ in mh])
        if (tg or 0) < 5 and (car or 0) < 10: continue
        if not any(h_["w"] == W for h_ in mh) and any(h_["w"] < W for h_ in mh):
            if not any(m_["n"] in s for s in c.get("why", {}).values()):
                out.append(f"{m_['n']} didn’t play.")
    return out[:3]


def grade_week(ctx, W, wk_board, cand, model):
    """Grade the board (and every candidate) for a finished week."""
    rows, picks, removed = [], [], []

    def res(c):
        act, h = actual(ctx, c["pid"], W, c["k"])
        if act is None: return None, None, "void"
        if act == c["line"]: return act, h, "push"
        won = act > c["line"] if c["side"] == "over" else act < c["line"]
        return act, h, ("hit" if won else ("near" if near_miss(c["k"], c["line"], act) else "miss"))

    for key, e in (wk_board.get("board") or {}).items():
        act, h, r = res(e)
        item = {"key": key, "n": e["n"], "t": e["t"], "opp": e["opp"], "pos": e["pos"], "k": e["k"], "side": e["side"], "line": e["line"], "be": e.get("be"),
                "lsrc": e.get("lsrc"), "proj": e["proj"], "p": e["p"], "act": act, "r": r,
                "why": [x["l"] for x in (e.get("reasons") or [])[:3]], "dx": diagnose(ctx, e, W, act, h) if act is not None else ["Didn’t play."]}
        (picks if e.get("status") != "removed" else removed).append(item)
    on_board = {k for k, e in (wk_board.get("board") or {}).items() if e.get("status") != "removed"}
    sneaky, blind = [], []
    for key, c in cand.items():
        act, h, r = res(c)
        if act is None: continue
        rows.append([c["pid"], c["k"], c["side"], c["line"], c["base"], c["proj"], c.get("praw", c["p"]), act, c["fx"], W])
        if key in on_board: continue
        margin = (act - c["line"]) if c["side"] == "over" else (c["line"] - act)
        big = max(0.25 * c["line"], {"pyd": 40, "ryd": 20, "recyd": 20, "rec": 2, "ptd": 1}[c["k"]])
        if r == "hit" and margin >= big and c["p"] >= 0.53:
            sneaky.append({"key": key, "n": c["n"], "t": c["t"], "opp": c["opp"], "k": c["k"], "side": c["side"], "line": c["line"], "act": act,
                           "p": c["p"], "proj": c["proj"], "why": [x["l"] for x in top_reasons(c, 2, model) if x["helps"]],
                           "dx": diagnose(ctx, c, W, act, h)})
        elif r == "miss" and margin <= -big and c["p"] >= 0.58:
            blind.append({"key": key, "n": c["n"], "t": c["t"], "opp": c["opp"], "k": c["k"], "side": c["side"], "line": c["line"], "act": act,
                          "p": c["p"], "proj": c["proj"], "dx": diagnose(ctx, c, W, act, h)})
    sneaky.sort(key=lambda x: -abs(x["act"] - x["line"]) / max(x["line"], 1)); blind.sort(key=lambda x: -abs(x["act"] - x["line"]) / max(x["line"], 1))
    rec = defaultdict(int)
    for x in picks: rec[x["r"]] += 1
    exp_hits = sum(((wk_board.get("board") or {}).get(x["key"]) or {}).get("be", BREAKEVEN) for x in picks if x["r"] in ("hit", "miss", "near"))
    by_k = defaultdict(lambda: [0, 0])
    for x in picks:
        if x["r"] in ("hit", "miss", "near"): by_k[x["k"]][0 if x["r"] == "hit" else 1] += 1
    rem = defaultdict(int)
    for x in removed: rem[x["r"]] += 1
    return {"wk": W, "record": dict(rec), "exp_hits": round(exp_hits, 1), "by_k": {k: v for k, v in by_k.items()}, "picks": picks, "removed": removed, "removed_record": dict(rem),
            "sneaky": sneaky[:10], "blind": blind[:8], "n_cand": len(rows)}, rows


def fit(rows, prev=None):
    """Bayesian ridge on log(actual/baseline): factor weights (prior 1), bias per prop (prior 0), calibration per prop."""
    if not rows: return {"w": {f: 1.0 for f in FACTORS}, "b": {}, "a": {}, "n": 0, "acc": {}}
    last = max(r[9] for r in rows)
    data = []
    for pid, k, side, line, base, proj, praw, act, fx, W in rows:
        c = SMOOTH[k]
        y = _clamp(math.log(max(act + c, 0.5 * c) / (base + c)), -1.5, 1.5)
        data.append((k, y, fx, DECAY ** (last - W), side, line, praw, act))
    # residual variance with prior weights, for the prior strengths
    res = [y - sum(fx.values()) for k, y, fx, *_ in data]
    s2 = max(0.05, sum(r * r for r in res) / len(res))
    w = {f: 1.0 for f in FACTORS}; b = defaultdict(float)
    for _ in range(25):
        for f in FACTORS:
            sxx = sxr = 0.0
            for k, y, fx, dw, *_ in data:
                x = fx.get(f, 0.0)
                if not x: continue
                r = y - b[k] - sum(w[g] * v for g, v in fx.items() if g != f)
                sxx += dw * x * x; sxr += dw * x * r
            lam = s2 / TAU_W ** 2
            w[f] = _clamp((sxr + lam * 1.0) / (sxx + lam), 0.25, 2.0) if sxx > 0 else 1.0
        for k in PNAME:
            sr = sw = 0.0
            for kk, y, fx, dw, *_ in data:
                if kk != k: continue
                sr += dw * (y - sum(w[g] * v for g, v in fx.items())); sw += dw
            lam = s2 / TAU_B ** 2
            b[k] = _clamp(sr / (sw + lam), -0.25, 0.25) if sw else 0.0
    # calibration: did a 60% lean win 60% of the time?
    a = {}
    for k in PNAME:
        sdd = sdo = 0.0
        for kk, y, fx, dw, side, line, praw, act in data:
            if kk != k or act == line: continue
            won = (act > line) if side == "over" else (act < line)
            d = praw - 0.5; sdd += dw * d * d; sdo += dw * d * ((1 if won else 0) - 0.5)
        lam = 0.25 / TAU_A ** 2
        a[k] = round(_clamp((sdo + lam * A0) / (sdd + lam), 0.3, 1.2), 3)
    # how often each factor pointed the right way (for the explanations)
    acc = {}
    for f in FACTORS:
        up = [y > 0 for k, y, fx, *_ in data if fx.get(f, 0) >= 0.02]
        dn = [y < 0 for k, y, fx, *_ in data if fx.get(f, 0) <= -0.02]
        acc[f] = {"up": [sum(up), len(up)], "dn": [sum(dn), len(dn)]}
    return {"w": {f: round(v, 3) for f, v in w.items()}, "b": {k: round(v, 4) for k, v in b.items()}, "a": a, "n": len(data), "acc": acc}


def lessons(before, after, rep):
    out = []
    for f in FACTORS:
        o, n = before["w"].get(f, 1.0), after["w"].get(f, 1.0)
        if abs(n - o) >= 0.03:
            ac = after["acc"].get(f, {})
            up, dn = ac.get("up", [0, 0]), ac.get("dn", [0, 0])
            ev = []
            if up[1]: ev.append(f"when it pushed a projection up, the player beat his baseline {up[0] / up[1] * 100:.0f}% of the time ({up[1]} props)")
            if dn[1]: ev.append(f"when it pushed down, he fell short {dn[0] / dn[1] * 100:.0f}% ({dn[1]})")
            out.append({"f": f, "l": FNAME[f], "from": o, "to": n, "t": f"{FNAME[f]} {'counts more' if n > o else 'counts less'} now ({o:.2f} → {n:.2f}): " + ("; ".join(ev) or "small sample") + "."})
    for k in PNAME:
        o, n = before["b"].get(k, 0.0), after["b"].get(k, 0.0)
        if abs(n - o) >= 0.01:
            out.append({"f": "bias", "l": PNAME[k], "from": o, "to": n, "t": f"{PNAME[k].capitalize()}: projections now run {abs(n) * 100:.0f}% {'higher' if n > 0 else 'lower'} than the raw matchup math (was {o * 100:+.0f}%), because actual results ran {'above' if n > o else 'below'} the projections."})
        oa, na = before["a"].get(k, A0), after["a"].get(k, A0)
        if abs(na - oa) >= 0.03:
            out.append({"f": "calib", "l": PNAME[k], "from": oa, "to": na, "t": f"{PNAME[k].capitalize()}: confidence {'raised' if na > oa else 'trimmed'} ({oa:.2f} → {na:.2f}); {'leans won more often than their stated chance' if na > oa else 'leans won less often than their stated chance'}."})
    out.sort(key=lambda x: -abs(math.log(max(x["to"], 1e-3) / max(x["from"], 1e-3))) if x["f"] not in ("bias",) else -abs(x["to"] - x["from"]) * 4)
    return out[:6]


# ---------------------------------------------------------------- the weekly cycle
def _avail_live(ctx, W):
    def f(g):
        out = {}
        for t in (g["home"], g["away"]):
            for p in ctx.by_team[t]:
                i = p.get("inj") or {}
                out[p["id"]] = not (i.get("wk") == W and i.get("st") in ("Out", "Doubtful"))
                if p.get("g", 0) == 0: out[p["id"]] = False
        return out
    return f


def _avail_back(ctx, W):
    def f(g):
        out = {}
        for t in (g["home"], g["away"]):
            for p in ctx.by_team[t]:
                out[p["id"]] = any(h["s"] == ctx.season and h["w"] == W for h in ctx.hist[p["id"]])
        return out
    return f


def _week_done(ctx, W):
    gs = [g for g in ctx.games if g["wk"] == W]
    if not gs or not all(_final(g) for g in gs): return False
    teams = {t for g in gs for t in (g["home"], g["away"])}
    seen = {p["t"] for p in ctx.players if any(h["s"] == ctx.season and h["w"] == W for h in ctx.hist[p["id"]])}
    return teams <= seen


def _entry(c, model, ts, why_add):
    e = {k: c[k] for k in ("pid", "n", "t", "opp", "pos", "gid", "k", "side", "line", "lsrc", "base", "proj", "vproj", "vk", "p", "p0", "edge", "be", "hits", "fx", "why")}
    if c.get("odds"): e["odds"] = c["odds"]
    e["reasons"] = top_reasons(c, 4, model); e["head"] = headline(c, model)
    e["status"] = "active"; e["added"] = ts; e["added_why"] = why_add
    return e


def build_board(data, states, allowed, now=None, odds_key=None):
    now = now or datetime.now(timezone.utc)
    ts = _iso(now)
    board = json.load(open(BOARD_FILE)) if os.path.exists(BOARD_FILE) else {"weeks": {}, "history": []}
    learn = json.load(open(LEARN_FILE)) if os.path.exists(LEARN_FILE) else {"weeks": {}}
    if board.get("season") not in (None, data["season"]):
        board, learn = {"weeks": {}, "history": []}, {"weeks": {}}
    board["season"] = data["season"]
    method_change = board.get("method") not in (None, METHOD) or (board.get("weeks") and not board.get("method"))
    if method_change:
        # the model's inputs changed: rebuild the backtests and the learned model from scratch; the live week
        # keeps its history and logs the switch as the reason for whatever changes
        for w in [w for w, wb in board["weeks"].items() if wb.get("mode") == "backtest"]:
            board["weeks"].pop(w, None); learn["weeks"].pop(w, None)
        for wb in board["weeks"].values():
            wb.pop("report", None)
        board["history"], board["model"] = [], None
        print(f"  method changed to {METHOD}: backtests and learned model rebuilt")
    board["method"] = METHOD
    ctx = Ctx(data, states, allowed)
    model = (board.get("model") if not method_change else None) or {"w": {f: 1.0 for f in FACTORS}, "b": {}, "a": {}, "n": 0, "acc": {}}
    weeks = sorted({g["wk"] for g in ctx.games})
    open_wk = [w for w in weeks if any(not _final(g) for g in ctx.games if g["wk"] == w)]
    live = open_wk[0] if open_wk else None
    if live is not None:
        left = [g for g in ctx.games if g["wk"] == live and not _final(g) and (kickoff_utc(g) or now) > now]
        if len(left) == 0 and len(open_wk) > 1: live = open_wk[1]
        elif len([g for g in ctx.games if g["wk"] == live and not _final(g)]) <= 2 and len(open_wk) > 1 and str(live) not in board["weeks"]:
            live = open_wk[1]

    # 1) backfill finished weeks that never had a board, then grade anything finished
    for W in weeks:
        if live is not None and W >= live: break
        sW = str(W)
        if sW in board["weeks"] and board["weeks"][sW].get("report"): continue
        if not _week_done(ctx, W): continue
        if sW not in board["weeks"]:
            cand = candidates(ctx, W, "back", model, {g["id"]: {"temp": g.get("temp"), "wind": g.get("wind"), "pp": None}
                                                        for g in ctx.games if g["wk"] == W and g.get("roof") not in ("dome", "closed")},
                              {}, _avail_back(ctx, W))
            if not cand: continue
            sel = select(cand)
            board["weeks"][sW] = {"wk": W, "mode": "backtest", "opened": ts,
                                  "board": {f"{c['pid']}|{c['k']}": dict(_entry(c, model, ts, "Backtest pick"), status="locked") for c in sel},
                                  "log": [{"ts": ts, "kind": "note", "txt": f"Backtest: built after the fact from games before week {W}. Availability uses who actually played."}]}
            learn["weeks"][sW] = {"cand": cand}
        wb = board["weeks"][sW]
        for e in wb["board"].values():
            if e.get("status") == "active": e["status"] = "locked"
        cand = (learn["weeks"].get(sW) or {}).get("cand") or {}
        rep, _ = grade_week(ctx, W, wb, cand, model)
        rows = []
        for w2, L in learn["weeks"].items():
            if int(w2) > W or not board["weeks"].get(w2): continue
            if int(w2) != W and not board["weeks"][w2].get("report"): continue
            for key, c in (L.get("cand") or {}).items():
                act, _h = actual(ctx, c["pid"], int(w2), c["k"])
                if act is not None:
                    rows.append([c["pid"], c["k"], c["side"], c["line"], c["base"], c["proj"], c.get("praw", c["p"]), act, c["fx"], int(w2)])
        before = model
        model = fit(rows, before)
        rep["lessons"] = lessons(before, model, rep)
        rep["model_before"] = {k: before[k] for k in ("w", "b", "a")}
        rep["model_after"] = {k: model[k] for k in ("w", "b", "a")}
        rep["graded"] = ts
        wb["report"] = rep
        for c in cand.values(): c.pop("why", None)            # graded: the reasons live in the report now
        board["history"] = [h for h in board.get("history", []) if h["wk"] != W] + [{"wk": W, "mode": wb.get("mode", "live"), "record": rep["record"], "w": model["w"], "b": model["b"], "a": model["a"]}]
        print(f"  graded week {W}: {rep['record']} — {len(rep['lessons'])} lessons")
    board["model"] = model

    # 2) the live board
    if live is not None:
        sW = str(live)
        wb = board["weeks"].setdefault(sW, {"wk": live, "mode": "live", "opened": ts, "board": {}, "log": []})
        L = learn["weeks"].setdefault(sW, {"cand": {}})
        games = [g for g in ctx.games if g["wk"] == live]
        wx = dict(wb.get("weather") or {})
        wx.update(fetch_weather(games, now))
        wb["weather"] = wx
        lines = wb.get("lines") or {}
        pulls = wb.get("line_pulls") or []
        first_ko = min([kickoff_utc(g) for g in games if kickoff_utc(g)] or [now])
        if odds_key and len(pulls) < 2 and first_ko - now <= timedelta(days=6) and (not pulls or now - datetime.strptime(pulls[-1], "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc) >= timedelta(days=2)):
            got = fetch_lines(odds_key, ctx, [g for g in games if (kickoff_utc(g) or now) > now])
            if got:
                lines.update(got); pulls.append(ts); print(f"  sportsbook lines: {len(got)} props")
        wb["lines"], wb["line_pulls"] = lines, pulls
        old = L["cand"]
        new = candidates(ctx, live, "live", model, wx, lines, _avail_live(ctx, live), now)
        # games that already kicked off keep their last pre-kickoff candidates (that's what gets graded)
        started = {g["id"] for g in games if (kickoff_utc(g) or now) <= now}
        for key, c in old.items():
            if c["gid"] in started: new[key] = c
        sel = select({k: c for k, c in new.items() if c["gid"] not in started})
        sel_keys = {f"{c['pid']}|{c['k']}" for c in sel}
        B = wb["board"]; log = wb["log"]
        opening = not B
        if method_change and B:
            log.append({"ts": ts, "kind": "note", "txt": "Method change: props now run on recent form (this season, with last season counted as 3 games and two seasons back dropped), and so do the defense numbers. Every change below in this run comes from that switch."})
        tag = "Recent-form switch: " if (method_change and B) else ""
        # lock everything whose game has started
        for key, e in B.items():
            if e["gid"] in started and e["status"] == "active":
                e["status"] = "locked"; e["locked"] = ts
        cut = min([c["sc"] for c in sel] or [MIN_EDGE])
        for key, e in list(B.items()):
            if e["status"] != "active" or key in sel_keys: continue
            c = new.get(key)
            p = ctx.by_id.get(e["pid"]) or {}
            i = p.get("inj") or {}
            if i.get("wk") == live and i.get("st") in ("Out", "Doubtful"):
                why = f"Ruled {i['st']} ({i.get('i') or 'injury'}) on the week {live} injury report."
            elif c is None:
                why = "No longer qualifies (role or availability changed)."
            elif c["edge"] < MIN_EDGE:
                why = f"Edge fell from {e['edge'] * 100:.0f}% to {c['edge'] * 100:.0f}% (below the {MIN_EDGE * 100:.0f}% bar)" + (f": {change_reason(old.get(key) or e, c)}" if change_reason(old.get(key) or e, c) else ".")
                if c["side"] != e["side"]: why = f"The lean flipped to the {c['side']}: " + (change_reason(old.get(key) or e, c) or "the projection moved through the line.")
            else:
                why = f"Bumped by stronger plays (score {c['sc'] * 100:.1f} vs the cut at {cut * 100:.1f})." + (f" {change_reason(old.get(key) or e, c)}" if change_reason(old.get(key) or e, c) else "")
            why = tag + why
            e["status"] = "removed"; e["removed"] = ts; e["removed_why"] = why
            log.append({"ts": ts, "kind": "remove", "key": key, "n": e["n"], "k": e["k"], "side": e["side"], "line": e["line"], "txt": why})
        for c in sel:
            key = f"{c['pid']}|{c['k']}"
            e = B.get(key)
            if e and e["status"] == "active":
                chg = []
                if c["line"] != e["line"]: chg.append(f"line {e['line']:g} → {c['line']:g}" + (" (sportsbook)" if c["lsrc"] == "book" and e.get("lsrc") != "book" else ""))
                if c["side"] != e["side"]: chg.append(f"lean flipped to the {c['side']}")
                if abs(c["p"] - e["p"]) >= 0.05: chg.append(f"chance {e['p'] * 100:.0f}% → {c['p'] * 100:.0f}%")
                if chg:
                    why = tag + "; ".join(chg)
                    cr = change_reason(old.get(key) or e, c)
                    if cr: why += f". Why: {cr}"
                    log.append({"ts": ts, "kind": "update", "key": key, "n": c["n"], "k": c["k"], "side": c["side"], "line": c["line"], "txt": why})
                ne = _entry(c, model, e["added"], e["added_why"]); ne["upd"] = ts
                B[key] = ne
                continue
            prev = old.get(key)
            if opening:
                why = "Opening board: " + "; ".join(f"{r['l'].lower()} ({'+' if r['e'] > 0 else ''}{r['e']:.0f}%)" for r in top_reasons(c, 3, model) if r["helps"]) or "Opening board: his track record against this line."
            elif e and e["status"] == "removed":
                why = "Back on: " + (change_reason(prev or e, c) or f"edge back to {c['edge'] * 100:.0f}%.")
            elif prev and (abs(c["edge"] - prev["edge"]) >= 0.01 or change_reason(prev, c)):
                why = f"Edge rose from {prev['edge'] * 100:.0f}% to {c['edge'] * 100:.0f}%" + (f": {change_reason(prev, c)}" if change_reason(prev, c) else ".")
            elif prev:
                why = f"Moved into the top {BOARD_SIZE} as other picks dropped off (edge {c['edge'] * 100:.0f}%, unchanged)."
            else:
                why = "New candidate this run: " + ("; ".join(f"{r['l'].lower()} ({'+' if r['e'] > 0 else ''}{r['e']:.0f}%)" for r in top_reasons(c, 2, model) if r["helps"]) or "his track record against this line.")
            why = tag + why
            B[key] = _entry(c, model, ts, why)
            log.append({"ts": ts, "kind": "add", "key": key, "n": c["n"], "k": c["k"], "side": c["side"], "line": c["line"], "txt": why})
        L["cand"] = new
        wb["updated"] = ts
        # where this week's picks lean on what the model learned
        wb["learned"] = sorted([{"key": k_, "n": e["n"], "k": e["k"], "side": e["side"], "line": e["line"], "p": e["p"], "p0": e["p0"]}
                                for k_, e in B.items() if e["status"] == "active" and abs(e["p"] - e["p0"]) >= 0.02],
                               key=lambda x: -abs(x["p"] - x["p0"]))[:8]
    board["cur"] = live
    board["updated"] = ts
    board["consts"] = {"size": BOARD_SIZE, "min_edge": MIN_EDGE, "breakeven": BREAKEVEN, "fname": FNAME, "pname": PNAME}
    with open(BOARD_FILE, "w") as f: json.dump(board, f, separators=(",", ":"))
    with open(LEARN_FILE, "w") as f: json.dump(learn, f, separators=(",", ":"))
    act = len([e for e in (board["weeks"].get(str(live), {}).get("board") or {}).values() if e["status"] == "active"]) if live else 0
    print(f"Wrote {BOARD_FILE} — week {live}: {act} active picks; {len([w for w in board['weeks'].values() if w.get('report')])} weeks graded")
    return board
