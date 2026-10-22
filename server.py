#!/usr/bin/env python3
"""
Postgame - local server.

Serves the Postgame app at http://localhost:8765 and fetches your ranked games
from Riot's API using your own development key. Uses only Python's standard
library, so there is nothing to install beyond Python 3.8+.

Run:  python server.py      (Windows)
      python3 server.py     (macOS / Linux)
"""
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CACHE = os.path.join(DATA, "matches")
KEY_FILE = os.path.join(HERE, "riot_key.txt")
STATE_FILE = os.path.join(DATA, "state.json")
PORT = int(os.environ.get("POSTGAME_PORT", "8765"))
PROCESS_VERSION = "v2"
RANKED_SOLO = 420
GAMES = 20

os.makedirs(CACHE, exist_ok=True)

# Platform -> regional routing for account-v1 and match-v5
REGION = {
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe", "me1": "europe",
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas",
    "kr": "asia", "jp1": "asia",
    "oc1": "sea", "sg2": "sea", "tw2": "sea", "vn2": "sea",
}

PROGRESS = {"stage": "", "done": 0, "total": 0}
_ddragon = {"version": None, "at": 0}


def ddragon_version():
    """Latest game-asset version from Riot's Data Dragon (no key needed). Cached for 6 hours."""
    if _ddragon["version"] and time.time() - _ddragon["at"] < 6 * 3600:
        return _ddragon["version"]
    try:
        req = urllib.request.Request("https://ddragon.leagueoflegends.com/api/versions.json", headers={"User-Agent": "Postgame-local/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            _ddragon.update(version=json.load(r)[0], at=time.time())
    except (urllib.error.URLError, ValueError, IndexError):
        pass
    return _ddragon["version"]


# ---------------------------------------------------------------- Riot API
class RiotError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(message or code)
        self.status = status
        self.code = code


def get_key():
    k = os.environ.get("RIOT_API_KEY", "").strip()
    if not k and os.path.exists(KEY_FILE):
        with open(KEY_FILE, encoding="utf-8") as f:
            k = f.read().strip()
    return k


_rate_lock = threading.Lock()
_calls = []


def _wait_for_rate_limit():
    # Development keys allow 20 requests per second and 100 per 2 minutes.
    with _rate_lock:
        while True:
            now = time.time()
            _calls[:] = [t for t in _calls if now - t < 120]
            last_second = sum(1 for t in _calls if now - t < 1)
            if last_second < 18 and len(_calls) < 95:
                _calls.append(now)
                return
            if len(_calls) >= 95:
                PROGRESS["stage"] = "Waiting for Riot's rate limit (development keys allow 100 requests per 2 minutes)"
            time.sleep(0.5)


def riot(host, path, params=None):
    key = get_key()
    if not key:
        raise RiotError(401, "NO_KEY")
    url = f"https://{host}.api.riotgames.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(6):
        _wait_for_rate_limit()
        req = urllib.request.Request(url, headers={"X-Riot-Token": key, "User-Agent": "Postgame-local/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "10"))
                PROGRESS["stage"] = f"Riot asked us to slow down, waiting {wait} seconds"
                time.sleep(wait + 0.5)
                continue
            if e.code in (401, 403):
                raise RiotError(401, "BAD_KEY")
            if e.code == 404:
                raise RiotError(404, "NOT_FOUND")
            if e.code >= 500 and attempt < 5:
                time.sleep(2)
                continue
            raise RiotError(502, "RIOT_ERROR", f"Riot's API returned error {e.code}")
        except urllib.error.URLError:
            if attempt < 5:
                time.sleep(2)
                continue
            raise RiotError(502, "NETWORK")
    raise RiotError(429, "RATE_LIMIT")


# ---------------------------------------------------------------- state
_state_lock = threading.Lock()


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {"players": {}}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- analysis
MAP_MAX = 14870.0
OBJECTIVES = {"DRAGON": "Dragon", "BARON_NASHOR": "Baron"}


def norm(pos, red):
    """Map position to a 0-100 box with the player's own base bottom-left.
    For the red team, reflect across the river so top lane stays top."""
    x, y = pos["x"], pos["y"]
    if red:
        x, y = MAP_MAX - y, MAP_MAX - x
    return round(6 + 88 * x / MAP_MAX, 1), round(94 - 88 * y / MAP_MAX, 1)


def raw(pos):
    """True map position on a 0-100 box, matching Riot's minimap image (blue base bottom-left)."""
    return round(pos["x"] / MAP_MAX * 100, 1), round(100 - pos["y"] / MAP_MAX * 100, 1)


def area(x, y):
    if x < 22 and y > 78:
        return "your base"
    if x > 78 and y < 22:
        return "the enemy base"
    if x < 14 or y < 14:
        return "the top side lane"
    if x > 86 or y > 86:
        return "the bot side lane"
    if abs(x + y - 100) < 9:
        return "the mid lane"
    if abs(x - y) < 8:
        return "the river"
    return "your jungle" if x < y else "the enemy jungle"


def process(match, timeline, puuid):
    info = match["info"]
    parts = info["participants"]
    me = next(p for p in parts if p["puuid"] == puuid)
    pid = me["participantId"]
    team = me["teamId"]
    red = team == 200
    role = me.get("teamPosition") or me.get("individualPosition") or ""
    opp = next((p for p in parts if p["teamId"] != team and role and p.get("teamPosition") == role), None)
    allies = [p["participantId"] for p in parts if p["teamId"] == team]
    enemies = [p["participantId"] for p in parts if p["teamId"] != team]
    enemy_jg = next((p["participantId"] for p in parts if p["teamId"] != team and p.get("teamPosition") == "JUNGLE"), None)

    dur = info.get("gameDuration", 0)
    if dur > 20000:  # very old matches reported milliseconds
        dur = dur / 1000
    minutes = max(dur / 60, 1)

    frames = timeline["info"]["frames"]

    def pframe(f, i):
        return f.get("participantFrames", {}).get(str(i), {})

    def team_gold(f):
        return sum(pframe(f, i).get("totalGold", 0) for i in allies) - sum(pframe(f, i).get("totalGold", 0) for i in enemies)

    gold = [team_gold(f) for f in frames]
    i15 = min(15, len(frames) - 1)
    lane15 = 0
    if opp:
        lane15 = pframe(frames[i15], pid).get("totalGold", 0) - pframe(frames[i15], opp["participantId"]).get("totalGold", 0)

    def pos_at(i, ts):
        f = frames[min(len(frames) - 1, max(0, round(ts / 60000)))]
        return pframe(f, i).get("position")

    events = [e for f in frames for e in f.get("events", [])]
    wards = [e["timestamp"] for e in events
             if e.get("type") == "WARD_PLACED" and e.get("creatorId") == pid and e.get("wardType") not in (None, "UNDEFINED")]

    ev = []
    for e in events:
        ts = e.get("timestamp", 0)
        t = ts / 60000
        typ = e.get("type")
        if typ == "CHAMPION_KILL":
            involved = {e.get("killerId")} | set(e.get("assistingParticipantIds") or [])
            if e.get("victimId") == pid:
                pos = e.get("position") or pos_at(pid, ts) or {"x": MAP_MAX / 2, "y": MAP_MAX / 2}
                x, y = norm(pos, red)
                near = 0
                for a in allies:
                    if a == pid:
                        continue
                    ap = pos_at(a, ts)
                    if ap and math.dist((ap["x"], ap["y"]), (pos["x"], pos["y"])) < 2500:
                        near += 1
                rx, ry = raw(pos)
                ev.append({
                    "type": "death", "t": round(t, 2), "x": x, "y": y, "rx": rx, "ry": ry, "early": t < 15,
                    "enemies": max(1, len([k for k in involved if k in enemies])),
                    "jg": enemy_jg in involved, "allies": near,
                    "wards": len([w for w in wards if ts - 180000 <= w <= ts]),
                    "where": area(x, y),
                })
            elif e.get("killerId") == pid:
                ev.append({"type": "kill", "t": round(t, 2)})
        elif typ == "ELITE_MONSTER_KILL" and e.get("monsterType") in OBJECTIVES:
            name = "Elder Dragon" if e.get("monsterSubType") == "ELDER_DRAGON" else OBJECTIVES[e["monsterType"]]
            pos = e.get("position")
            mp = pos_at(pid, ts)
            near = bool(pos and mp and math.dist((mp["x"], mp["y"]), (pos["x"], pos["y"])) < 4000)
            wx, wy = norm(mp, red) if mp else (50, 50)
            ev.append({"type": "obj", "t": round(t, 2), "name": name,
                       "ally": e.get("killerTeamId") == team, "near": near, "where": area(wx, wy)})
    ev.sort(key=lambda e: e["t"])

    team_kills = sum(p.get("kills", 0) for p in parts if p["teamId"] == team) or 1
    kp = (me.get("challenges") or {}).get("killParticipation")
    kp = round(kp * 100) if kp is not None else round((me["kills"] + me["assists"]) / team_kills * 100)
    cs = me.get("totalMinionsKilled", 0) + me.get("neutralMinionsKilled", 0)
    end = info.get("gameEndTimestamp") or (info.get("gameStartTimestamp", 0) + dur * 1000)

    return {
        "id": match["metadata"]["matchId"],
        "end": end,
        "win": bool(me.get("win")),
        "dur": round(minutes),
        "champ": me.get("championName", "?"),
        "opp": opp.get("championName") if opp else None,
        "role": role,
        "red": red,
        "items": [me.get(f"item{n}", 0) for n in range(7)],
        "kills": me.get("kills", 0), "deaths": me.get("deaths", 0), "assists": me.get("assists", 0),
        "cs": cs, "csMin": round(cs / minutes, 1),
        "vision": round(me.get("visionScore", 0) / minutes, 2),
        "kp": kp,
        "g15": lane15, "team15": gold[i15] if gold else 0,
        "dPre15": len([x for x in ev if x["type"] == "death" and x["early"]]),
        "gold": gold,
        "ev": ev,
        "remake": bool(me.get("gameEndedInEarlySurrender")) or dur < 300,
    }


def get_match(mid, region, puuid):
    path = os.path.join(CACHE, f"{mid}_{puuid[:16]}_{PROCESS_VERSION}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    match = riot(region, f"/lol/match/v5/matches/{mid}")
    timeline = riot(region, f"/lol/match/v5/matches/{mid}/timeline")
    data = process(match, timeline, puuid)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def get_rank(platform, puuid):
    try:
        entries = riot(platform, f"/lol/league/v4/entries/by-puuid/{puuid}")
    except RiotError as e:
        if e.code != "NOT_FOUND":
            raise
        summ = riot(platform, f"/lol/summoner/v4/summoners/by-puuid/{puuid}")
        entries = riot(platform, f"/lol/league/v4/entries/by-summoner/{summ['id']}")
    solo = next((x for x in entries if x.get("queueType") == "RANKED_SOLO_5x5"), None)
    if not solo:
        return None
    return {"tier": solo["tier"], "div": solo.get("rank", ""), "lp": solo.get("leaguePoints", 0),
            "wins": solo.get("wins", 0), "losses": solo.get("losses", 0)}


def load_player(name, tag, platform):
    platform = platform.lower()
    region = REGION.get(platform)
    if not region:
        raise RiotError(400, "BAD_REGION")
    PROGRESS.update(stage="Finding your account", done=0, total=0)
    acct = riot(region, f"/riot/account/v1/accounts/by-riot-id/{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}")
    puuid = acct["puuid"]
    PROGRESS["stage"] = "Reading your rank"
    rank = get_rank(platform, puuid)
    PROGRESS["stage"] = "Listing your recent ranked games"
    ids = riot(region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids", {"queue": RANKED_SOLO, "start": 0, "count": GAMES})
    PROGRESS.update(stage="Analysing games", done=0, total=len(ids))
    matches = []
    for mid in ids:
        try:
            m = get_match(mid, region, puuid)
            if not m["remake"]:
                matches.append(m)
        except RiotError as e:
            if e.code in ("BAD_KEY", "NO_KEY"):
                raise
        PROGRESS["done"] += 1
    matches.sort(key=lambda m: m["end"], reverse=True)
    roles = Counter(m["role"] for m in matches if m["role"])

    with _state_lock:
        state = load_state()
        p = state["players"].setdefault(puuid, {"lpHistory": [], "focus": None})
        if rank:
            snap = {"ts": int(time.time() * 1000), "tier": rank["tier"], "div": rank["div"], "lp": rank["lp"]}
            last = p["lpHistory"][-1] if p["lpHistory"] else None
            if not last or (last["tier"], last["div"], last["lp"]) != (snap["tier"], snap["div"], snap["lp"]):
                p["lpHistory"].append(snap)
                p["lpHistory"] = p["lpHistory"][-200:]
        save_state(state)
    PROGRESS.update(stage="Done")
    return {
        "player": {"name": acct.get("gameName", name), "tag": acct.get("tagLine", tag), "platform": platform,
                   "puuid": puuid, "rank": rank, "role": roles.most_common(1)[0][0] if roles else "MIDDLE"},
        "matches": matches,
        "focus": p["focus"],
        "lpHistory": p["lpHistory"],
    }


# ---------------------------------------------------------------- HTTP
ERROR_TEXT = {
    "NO_KEY": "Add your Riot development key to get started.",
    "BAD_KEY": "Riot rejected the key. Development keys expire every 24 hours, so get a fresh one at developer.riotgames.com and paste it here.",
    "NOT_FOUND": "No account found for that Riot ID in this region. Check the name, the #tag and the region.",
    "BAD_REGION": "Choose a region from the list.",
    "NETWORK": "Could not reach Riot's servers. Check your internet connection and try again.",
    "RATE_LIMIT": "Riot's rate limit was hit too many times. Wait two minutes and try again.",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, e):
        self._send(e.status, {"error": e.code, "message": ERROR_TEXT.get(e.code, str(e))})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        if url.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/api/status":
            return self._send(200, {"hasKey": bool(get_key())})
        if url.path == "/api/assets":
            return self._send(200, {"version": ddragon_version()})
        if url.path == "/api/progress":
            return self._send(200, PROGRESS)
        if url.path == "/api/load":
            try:
                return self._send(200, load_player(q.get("name", ""), q.get("tag", ""), q.get("platform", "")))
            except RiotError as e:
                PROGRESS["stage"] = ""
                return self._error(e)
            except Exception as e:  # keep the app responsive and show what broke
                PROGRESS["stage"] = ""
                return self._send(500, {"error": "INTERNAL", "message": f"Something went wrong while reading your games: {e}"})
        self._send(404, {"error": "NOT_FOUND", "message": "Unknown page"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        try:
            body = self._body()
        except ValueError:
            return self._send(400, {"error": "BAD_JSON", "message": "Invalid request"})
        if url.path == "/api/key":
            key = str(body.get("key", "")).strip()
            if not key.startswith("RGAPI-"):
                return self._send(400, {"error": "BAD_KEY_FORMAT", "message": "Riot keys start with RGAPI-. Copy the whole key from the developer portal."})
            with open(KEY_FILE, "w", encoding="utf-8") as f:
                f.write(key)
            return self._send(200, {"ok": True})
        if url.path == "/api/focus":
            with _state_lock:
                state = load_state()
                p = state["players"].setdefault(body.get("puuid", ""), {"lpHistory": [], "focus": None})
                p["focus"] = {"metric": body.get("metric"), "after": body.get("after"), "set": int(time.time() * 1000)}
                save_state(state)
            return self._send(200, {"ok": True, "focus": p["focus"]})
        self._send(404, {"error": "NOT_FOUND", "message": "Unknown action"})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Postgame is running at {url}")
    print("Leave this window open while you use it. Press Ctrl+C to stop.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
