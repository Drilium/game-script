"""
Chatter bot: what people are saying about this week's players, prop-related posts only.

Sources
  - X (Twitter) recent search, when the X_BEARER_TOKEN repo secret is set. X has no free read tier:
    pay-per-use is about $0.005 per post returned and $0.010 per author returned, so every run is held
    to a monthly budget (X_MONTHLY_BUDGET, default $10) and a per-run cap (X_MAX_READS, default 40 posts).
    Posts already seen are skipped with since_id, so the same post is never paid for twice.
  - Bluesky public search (free, no key), so the page has chatter even without an X key.
    Set BUZZ_BLUESKY=0 to turn it off.

Filter (deliberately light for now): a post is kept only if it names one of the watched players AND
mentions a prop (over/under, a stat word like yards/receptions/TD, a line like 64.5 or 5+, alt/ladder/parlay).
Replies and comments that only say the player's name are dropped. Reposts are skipped.

Writes nfl_buzz.json (published; the site reads it lazily). Standard library only.
"""
import json, os, re, urllib.request, urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta

BUZZ_FILE = "nfl_buzz.json"
KEEP_DAYS, PER_PLAYER = 7, 40
X_POST_COST, X_USER_COST = 0.005, 0.010

# What counts as "talking props": a betting-shaped phrase, not just a loose word like "over" or "pass".
PROP_PATTERNS = [
    r"\b(?:over|under|o|u)\s?\d+(?:\.5)?\b",                                   # over 64.5, o6.5, u 1.5
    r"\b\d+(?:\.5)?\s?\+?\s?(?:rec(?:eiving)?\s|rush(?:ing)?\s|pass(?:ing)?\s)?(?:yds|yards|yd|recs?|receptions|catches|targets|tgts|tds?|touchdowns?|carries|completions|comps|attempts|atts)\b",
    r"\b(?:prop|props|anytime|atd|attd|first td|1st td|alt|alts|ladder|parlay|sgp|o/u|over/under)\b",
    r"\b(?:receiving|rushing|passing|rec|rush|pass)\s(?:yards|yds|tds?|touchdowns?)\b",
    r"\b(?:receptions|catches|targets|tds?|touchdowns?)\b",
]
PROP_RE = re.compile("|".join(PROP_PATTERNS), re.I)
STAT_MAP = [  # (regex, prop key)
    (r"rec(?:eiving)?\s*(?:yds|yards|yd)|(?:yds|yards)\s*rec", "recyd"),
    (r"rush(?:ing)?\s*(?:yds|yards|yd)|(?:yds|yards)\s*rush", "ryd"),
    (r"pass(?:ing)?\s*(?:yds|yards|yd)", "pyd"),
    (r"pass(?:ing)?\s*tds?|passing touchdowns?", "ptd"),
    (r"receptions|catches|recs?\b", "rec"),
    (r"anytime|atd|attd|\btds?\b|touchdowns?|first td|1st td", "td"),
    (r"targets|tgts", "tgt"),
    (r"(?:yds|yards|yd)", "yds"),
]
LEAN_RE = re.compile(r"\b(over|under|o|u)\s?(\d+(?:\.\d)?)\b|\b(\d+(?:\.\d)?)\s?\+", re.I)


def _get_json(url, headers=None, timeout=20):
    h = {"User-Agent": "game-script-nfl/1.0 (portfolio project)"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'.-]", " ", (s or "").lower())).strip()


def _last(n):
    parts = [w for w in n.split() if w.lower().rstrip(".") not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return parts[-1] if parts else n


def watch_list(data, board):
    """Board players first, then each team's main skill players in this week's games."""
    cur = board.get("cur") if board else None
    out, seen = [], set()
    wb = (board or {}).get("weeks", {}).get(str(cur)) or {}
    for e in sorted((wb.get("board") or {}).values(), key=lambda e: -e.get("edge", 0)):
        if e.get("status") != "removed" and e["pid"] not in seen:
            seen.add(e["pid"]); out.append(e["pid"])
    teams = {t for g in data["games"] if g.get("wk") == cur for t in (g["home"], g["away"])} if cur else set()
    by_team = defaultdict(list)
    for p in data["players"]:
        if p["t"] in teams and p["p"] in ("QB", "RB", "WR", "TE") and p.get("ss") and p.get("g", 0) >= 2:
            by_team[p["t"]].append(p)
    for t, ps in by_team.items():
        key = lambda p: (p["ss"].get("att", 0) * 0.5 + p["ss"].get("car", 0) + p["ss"].get("tgt", 0) * 1.2)
        for p in sorted(ps, key=key, reverse=True)[:5]:
            if p["id"] not in seen:
                seen.add(p["id"]); out.append(p["id"])
    return out


def match_players(text, cands):
    """Which watched players a post names: full name, or a last name that only one of them has."""
    t = _norm(text)
    hits = [p for p in cands if _norm(p["n"]) in t]
    if hits: return hits
    lasts = defaultdict(list)
    for p in cands: lasts[_norm(_last(p["n"]))].append(p)
    out = []
    for ln, ps in lasts.items():
        if len(ps) == 1 and len(ln) >= 4 and re.search(r"(?<!\w)" + re.escape(ln) + r"(?!\w)", t):
            out.append(ps[0])
    return out


def is_prop_talk(text, player):
    """True if the post talks props, not just the name. Strips the name, handles, links and emoji first."""
    t = re.sub(r"https?://\S+|[@#]\w+", " ", text)
    for part in [player["n"], _last(player["n"])]:
        t = re.sub(re.escape(part), " ", t, flags=re.I)
    rest = re.sub(r"[^\w.+/ ]", " ", t)
    if len(rest.split()) < 2: return False              # basically just the name
    return bool(PROP_RE.search(rest))


def read_prop(text):
    tl = text.lower()
    side = line = None
    m = LEAN_RE.search(text)
    # the stat named right after the number wins ("60+ yards and a TD" is a yards prop); otherwise the first stat named
    near = tl[m.end():m.end() + 22] if m else ""
    def first(s):
        hits = [(mm.start(), i, key) for i, (rx, key) in enumerate(STAT_MAP) for mm in [re.search(rx, s)] if mm]
        return min(hits)[2] if hits else None
    k = first(near) or next((key for rx, key in STAT_MAP if re.search(rx, tl)), None)
    if m:
        if m.group(1):
            side = "over" if m.group(1).lower() in ("over", "o") else "under"; line = float(m.group(2))
        elif m.group(3):
            side = "over"; line = float(m.group(3)) - 0.5
    if side is None:
        if re.search(r"\bover\b|\bsmash\b|\bhammer\b|\block\b", tl): side = "over"
        elif re.search(r"\bunder\b|\bfade\b", tl): side = "under"
    return k, side, line


def x_search(token, query, max_results, since_id=None):
    params = {"query": query, "max_results": max(10, min(100, max_results)),
              "tweet.fields": "created_at,public_metrics,referenced_tweets,in_reply_to_user_id,lang",
              "expansions": "author_id", "user.fields": "username,name,verified"}
    if since_id: params["since_id"] = since_id
    return _get_json("https://api.x.com/2/tweets/search/recent?" + urllib.parse.urlencode(params),
                     {"Authorization": f"Bearer {token}"})


BSKY = {"token": None, "host": None, "tried": False}


def _bsky_login():
    """Free Bluesky app password (BSKY_HANDLE + BSKY_APP_PASSWORD secrets); Bluesky blocks anonymous search from some hosts."""
    BSKY["tried"] = True
    h, pw = os.environ.get("BSKY_HANDLE"), os.environ.get("BSKY_APP_PASSWORD")
    if not (h and pw): return False
    req = urllib.request.Request("https://bsky.social/xrpc/com.atproto.server.createSession",
                                 data=json.dumps({"identifier": h, "password": pw}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "game-script-nfl/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode())
    BSKY["token"], BSKY["host"] = j["accessJwt"], "https://bsky.social"
    return True


def bsky_search(q, since):
    params = urllib.parse.urlencode({"q": q, "limit": 40, "sort": "latest", "lang": "en", "since": since})
    if BSKY["token"]:
        return _get_json(f"{BSKY['host']}/xrpc/app.bsky.feed.searchPosts?" + params, {"Authorization": f"Bearer {BSKY['token']}"})
    last = None
    for host in ("https://api.bsky.app", "https://public.api.bsky.app"):
        try:
            return _get_json(f"{host}/xrpc/app.bsky.feed.searchPosts?" + params)
        except Exception as e:
            last = e
    if not BSKY["tried"] and _bsky_login():
        return bsky_search(q, since)
    raise last


def build_buzz(data, board, now=None):
    now = now or datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%MZ")
    buzz = json.load(open(BUZZ_FILE)) if os.path.exists(BUZZ_FILE) else {}
    buzz.setdefault("players", {}); buzz.setdefault("state", {}); buzz.setdefault("spend", {})
    by_id = {p["id"]: p for p in data["players"]}
    watch = [by_id[i] for i in watch_list(data, board) if i in by_id]
    month = now.strftime("%Y-%m")
    stats = {"x": {"queries": 0, "read": 0, "kept": 0, "cost": 0.0, "status": "off"}, "bsky": {"queries": 0, "read": 0, "kept": 0, "status": "off"}}
    seen_ids = {it["id"] for lst in buzz["players"].values() for it in lst}

    def keep(pid, item):
        if item["id"] in seen_ids: return False
        seen_ids.add(item["id"]); buzz["players"].setdefault(pid, []).append(item); return True

    def consider(text, item, src):
        names = match_players(text, watch)
        kept = 0
        for p in names:
            if not is_prop_talk(text, p): continue          # just the name, or no prop in it: skip
            k, side, line = read_prop(text)
            if k == "yds": k = {"QB": "pyd", "RB": "ryd"}.get(p.get("p"), "recyd")
            it = dict(item, k=k, side=side, line=line)
            it["id"] = f"{item['id']}|{p['id']}" if len(names) > 1 else item["id"]
            if keep(p["id"], it): kept += 1
        stats[src]["kept"] += kept

    # ---- X (paid; budget-guarded)
    token = os.environ.get("X_BEARER_TOKEN") or None
    budget = float(os.environ.get("X_MONTHLY_BUDGET") or 10)
    per_run = int(os.environ.get("X_MAX_READS") or 40)
    spent = buzz["spend"].get(month, 0.0)
    if token and watch:
        stats["x"]["status"] = "on"
        left_usd = budget - spent
        reads_left = min(per_run, int(left_usd / (X_POST_COST + X_USER_COST)))   # worst case: every post has a new author
        if reads_left < 10:
            stats["x"]["status"] = f"paused: ${spent:.2f} of ${budget:.2f} spent in {month}"
        else:
            st = buzz["state"].setdefault("x", {"cursor": 0, "since": {}})
            groups = [watch[i:i + 6] for i in range(0, len(watch), 6)]
            gi = st["cursor"] % max(1, len(groups))
            while reads_left >= 10 and groups:
                grp = groups[gi]
                names = " OR ".join(f'"{p["n"]}"' for p in grp)
                q = f"({names}) (over OR under OR yards OR yds OR receptions OR catches OR TD OR touchdown OR anytime OR prop OR props OR line OR alt) -is:retweet lang:en"
                gkey = ",".join(sorted(p["id"] for p in grp))
                try:
                    j = x_search(token, q, min(reads_left, 25), st["since"].get(gkey))
                except Exception as e:
                    stats["x"]["status"] = f"error: {str(e)[:120]}"; print(f"  X search skipped: {e}"); break
                posts = j.get("data") or []; users = {u["id"]: u for u in (j.get("includes") or {}).get("users", [])}
                n_users = len({p.get("author_id") for p in posts})
                cost = len(posts) * X_POST_COST + n_users * X_USER_COST
                spent += cost; stats["x"]["cost"] += cost; stats["x"]["queries"] += 1; stats["x"]["read"] += len(posts)
                reads_left -= max(10, len(posts))
                if (j.get("meta") or {}).get("newest_id"): st["since"][gkey] = j["meta"]["newest_id"]
                for p_ in posts:
                    u = users.get(p_.get("author_id"), {})
                    pm = p_.get("public_metrics") or {}
                    item = {"id": "x:" + p_["id"], "src": "x", "t": p_.get("text", "")[:400], "ts": p_.get("created_at", "")[:16] + "Z",
                            "by": u.get("username"), "nm": u.get("name"), "likes": pm.get("like_count", 0), "rt": pm.get("retweet_count", 0),
                            "reply": any(r.get("type") == "replied_to" for r in (p_.get("referenced_tweets") or [])),
                            "url": f"https://x.com/{u.get('username') or 'i/web'}/status/{p_['id']}"}
                    consider(item["t"], item, "x")
                gi = (gi + 1) % len(groups)
                if gi == st["cursor"] % len(groups): break       # went all the way round
            st["cursor"] = gi
            buzz["spend"][month] = round(spent, 4)
    elif not token:
        stats["x"]["status"] = "off: add an X_BEARER_TOKEN secret to turn it on"

    # ---- Bluesky (free)
    if os.environ.get("BUZZ_BLUESKY", "1") != "0" and watch:
        stats["bsky"]["status"] = "on"
        since = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        st = buzz["state"].setdefault("bsky", {"cursor": 0})
        n = min(len(watch), 45)
        start = st["cursor"] % len(watch)
        order = (watch[start:] + watch[:start])[:n]
        fails = 0
        for p in order:
            try:
                j = bsky_search(f'"{p["n"]}"', since)
            except Exception as e:
                fails += 1
                if fails >= 3:
                    need = "" if os.environ.get("BSKY_APP_PASSWORD") else " (Bluesky blocks anonymous search here: add free BSKY_HANDLE and BSKY_APP_PASSWORD secrets)"
                    stats["bsky"]["status"] = f"error: {str(e)[:80]}{need}"; print(f"  Bluesky search skipped: {e}"); break
                continue
            stats["bsky"]["queries"] += 1
            for post in j.get("posts") or []:
                rec = post.get("record") or {}; au = post.get("author") or {}
                text = rec.get("text") or ""
                stats["bsky"]["read"] += 1
                rkey = (post.get("uri") or "").rsplit("/", 1)[-1]
                item = {"id": "bsky:" + (post.get("uri") or rkey), "src": "bsky", "t": text[:400], "ts": (rec.get("createdAt") or post.get("indexedAt") or "")[:16] + "Z",
                        "by": au.get("handle"), "nm": au.get("displayName"), "likes": post.get("likeCount", 0), "rt": post.get("repostCount", 0),
                        "reply": bool(rec.get("reply")), "url": f"https://bsky.app/profile/{au.get('handle')}/post/{rkey}"}
                consider(text, item, "bsky")
        st["cursor"] = (start + n) % len(watch)

    # ---- trim and summarize
    cut = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M")
    summ = {}
    for pid, lst in list(buzz["players"].items()):
        pl = by_id.get(pid)
        # re-check stored posts against the current filter, so tightening it cleans old entries too
        lst = sorted([x for x in lst if x.get("ts", "") >= cut and (not pl or is_prop_talk(x.get("t", ""), pl))],
                     key=lambda x: x.get("ts", ""), reverse=True)[:PER_PLAYER]
        if not lst:
            buzz["players"].pop(pid); continue
        buzz["players"][pid] = lst
        by_k = defaultdict(lambda: {"over": 0, "under": 0, "n": 0})
        for x in lst:
            k = x.get("k") or "other"
            by_k[k]["n"] += 1
            if x.get("side"): by_k[k][x["side"]] += 1
        summ[pid] = {"n": len(lst), "k": dict(by_k), "last": lst[0]["ts"]}
    buzz["summary"] = summ
    buzz["updated"] = ts
    buzz["run"] = stats
    buzz["watch"] = len(watch)
    with open(BUZZ_FILE, "w") as f:
        json.dump(buzz, f, separators=(",", ":"))
    print(f"Wrote {BUZZ_FILE} — watching {len(watch)} players; X {stats['x']['status']} ({stats['x']['kept']} kept, ${stats['x']['cost']:.2f}); "
          f"Bluesky {stats['bsky']['status']} ({stats['bsky']['kept']} kept of {stats['bsky']['read']} read)")
    return buzz
