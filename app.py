"""pi-home-dash: a tiny self-hosted home dashboard.

Grouped app tiles with up/down dots, an Immich photo carousel, clock, weather (Open-Meteo),
host stats and a Docker container table. Python stdlib + PyYAML only.

- The Immich API key stays server-side; the browser only receives /thumb/<id> for assets this
  server handed out itself.
- Docker: this code only issues GET requests (container list + one-shot stats), either over a
  unix socket (DOCKER_SOCK) or a TCP docker-socket-proxy (DOCKER_HOST=tcp://host:port).
"""
import json, os, re, time, random, socket, threading, http.client, urllib.request, urllib.error, urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import yaml

VERSION = "1.0.0"
CONF = os.environ.get("DASH_CONFIG", "/config/config.yaml")
ICONS = os.environ.get("DASH_ICONS", os.path.join(os.path.dirname(CONF), "icons"))
STATIC = os.environ.get("DASH_STATIC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))
LISTEN = os.environ.get("DASH_LISTEN", "0.0.0.0")
PORT = int(os.environ.get("DASH_LISTEN_PORT", "8080"))
IMMICH = os.environ.get("IMMICH_URL", "").strip().rstrip("/")
IMMICH_PUBLIC = os.environ.get("IMMICH_PUBLIC_URL", "").strip().rstrip("/")
KEY = os.environ.get("IMMICH_API_KEY", "").strip()
DOCKER_HOST = os.environ.get("DOCKER_HOST", "").strip()  # tcp://docker-proxy:2375 or unix:///path
DOCKER_SOCK = DOCKER_HOST[7:] if DOCKER_HOST.startswith("unix://") else os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
DOCKER_TCP = urllib.parse.urlsplit(DOCKER_HOST.replace("tcp://", "http://", 1)) if DOCKER_HOST.startswith("tcp://") else None
HOST_PROC = os.environ.get("HOST_PROC", "/proc")
THERMAL = os.environ.get("HOST_THERMAL", "/sys/class/thermal/thermal_zone0/temp")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ICON_RE = re.compile(r"^[a-z0-9._-]+\.(svg|png|webp)$")
lock = threading.Lock()
_cfg = {"m": None, "d": {}}
_feeds = OrderedDict()   # feed key -> {"t", "photos", "error", "forced"}
_allowed = OrderedDict()  # asset ids recently handed to the browser (thumb proxy whitelist)
_thumbs = OrderedDict()  # id/size -> (ctype, bytes); small LRU
_albums = {"t": 0.0, "d": [], "error": None}
_status = {"t": 0.0, "d": {}}
_weather = {"t": 0.0, "d": None, "error": None}
_dock = {"t": 0.0, "d": None, "prev": {}, "busy": False}
_host = {"prev": None}
wlock, dlock = threading.Lock(), threading.Lock()


def cfg():
    try:
        m = os.path.getmtime(CONF)
        if m != _cfg["m"]:
            with open(CONF, encoding="utf-8") as f:
                _cfg["d"] = yaml.safe_load(f) or {}
            _cfg["m"] = m
    except Exception as e:  # keep last good config
        print("config error:", e)
    return _cfg["d"]


def all_tiles(c):
    out = list(c.get("tiles") or [])
    for g in c.get("groups") or []:
        out += g.get("tiles") or []
    return out


# ---------------------------------------------------------------- Immich
def immich(path, method="GET", body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    h = {"x-api-key": KEY, "Accept": "application/json"}
    if data:
        h["Content-Type"] = "application/json"
    return urllib.request.urlopen(urllib.request.Request(IMMICH + path, data=data, method=method, headers=h), timeout=timeout)


def albums():
    """[{id, name, count}] — cached 10 min."""
    with lock:
        if time.time() - _albums["t"] < 600 and (_albums["d"] or _albums["error"]):
            return _albums["d"], _albums["error"]
    try:
        seen, out = set(), []
        for x in json.load(immich("/api/albums")) + json.load(immich("/api/albums?shared=true")):
            if x.get("id") in seen or not UUID.match(x.get("id", "")):
                continue
            seen.add(x["id"])
            out.append({"id": x["id"], "name": x.get("albumName", ""), "count": x.get("assetCount", 0)})
        out.sort(key=lambda a: a["name"].lower())
        with lock:
            _albums.update(t=time.time(), d=out, error=None)
    except Exception as e:
        err = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(e)[:200]
        print("immich albums error:", err)
        with lock:
            _albums.update(t=time.time() - 540, error=err)  # retry in 1 min
    return _albums["d"], _albums["error"]


def feed(sel="", force=False):
    """sel: '' = default from config, 'all' = whole library, <album uuid> = that album."""
    c = cfg().get("immich") or {}
    n = max(1, min(int(c.get("count", 12)), 50))
    ttl = max(1, int(c.get("refresh_minutes", 15))) * 60
    if not IMMICH:
        return {"status": "disabled", "photos": []}
    if not KEY:
        return {"status": "needs_key", "photos": []}
    al, aerr = albums()
    if sel == "":
        name = str(c.get("album") or "").strip().lower()
        a = next((x for x in al if x["name"].lower() == name or x["id"] == name), None) if name else None
        if name and not a:
            return {"status": "error", "error": aerr or "album_not_found", "photos": [], "album": None}
    elif sel == "all":
        a = None
    else:
        a = next((x for x in al if x["id"] == sel), None)
        if not a:
            return {"status": "error", "error": "unknown_album", "photos": [], "album": None}
    key = (a["id"] if a else "all", n)
    ainfo = {"id": a["id"], "name": a["name"]} if a else None
    with lock:
        f = _feeds.get(key)
        # new=1 (carousel finished a cycle / selector changed) always gets a fresh random set;
        # only a tiny 2 s guard so a double request cannot hammer Immich.
        fresh = f and time.time() - f["t"] < (2 if force else ttl) and (f["photos"] or f["error"])
        if fresh:
            return {"status": "error" if f["error"] else "ok", "error": f["error"], "photos": f["photos"], "album": ainfo}
    try:
        q = {"size": n, "type": "IMAGE", "withExif": False}
        if a:
            q["albumIds"] = [a["id"]]
        items = json.load(immich("/api/search/random", "POST", q))
        photos = [{"id": x["id"], "date": (x.get("localDateTime") or x.get("fileCreatedAt") or "")[:10],
                   "link": f"{IMMICH_PUBLIC}/photos/{x['id']}" if IMMICH_PUBLIC else ""}
                  for x in items if UUID.match(x.get("id", ""))]
        random.shuffle(photos)
        f = {"t": time.time(), "photos": photos[:n], "error": None}
    except Exception as e:
        err = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(e)[:200]
        print("immich error:", err)
        f = {"t": time.time() - ttl + 60, "photos": [], "error": err}  # retry in 1 min
    with lock:
        _feeds[key] = f
        _feeds.move_to_end(key)
        while len(_feeds) > 6:
            _feeds.popitem(last=False)
        for p in f["photos"]:
            _allowed[p["id"]] = 1
            _allowed.move_to_end(p["id"])
        while len(_allowed) > 400:
            _allowed.popitem(last=False)
    return {"status": "error" if f["error"] else "ok", "error": f["error"], "photos": f["photos"], "album": ainfo}


def thumb(aid, size):
    with lock:
        allowed = aid in _allowed
        ck = aid + size
        if ck in _thumbs:
            _thumbs.move_to_end(ck)
            return _thumbs[ck]
    if not allowed:
        return None
    r = immich(f"/api/assets/{aid}/thumbnail?size={size}", timeout=20)
    item = (r.headers.get("Content-Type", "image/jpeg"), r.read(3_000_000))
    if size == "thumbnail":
        with lock:
            _thumbs[ck] = item
            while len(_thumbs) > 60:
                _thumbs.popitem(last=False)
    return item


# ---------------------------------------------------------------- tile pings
def ping(url):
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=3).close()
        return True
    except urllib.error.HTTPError:
        return True  # it answered (401/403/404 = up)
    except Exception:
        return False


def status():
    with lock:
        if time.time() - _status["t"] < 60:
            return _status["d"]
    tiles = [t for t in all_tiles(cfg()) if t.get("ping")]
    with ThreadPoolExecutor(6) as ex:
        res = dict(zip([t["name"] for t in tiles], ex.map(lambda t: ping(t["ping"]), tiles)))
    with lock:
        _status.update(t=time.time(), d=res)
    return res


# ---------------------------------------------------------------- weather (Open-Meteo, cached)
def weather_enabled(c):
    return bool(c) and c.get("enabled", True) is not False and c.get("lat") is not None and c.get("lon") is not None


def weather():
    c = cfg().get("weather") or {}
    if not weather_enabled(c):
        return {"place": "", "data": None, "error": "disabled"}
    lat, lon = float(c["lat"]), float(c["lon"])
    ttl = max(5, int(c.get("refresh_minutes", 15))) * 60
    units = "fahrenheit" if str(c.get("units", "")).lower() in ("imperial", "fahrenheit", "f") else "celsius"
    with wlock:
        if _weather["d"] and time.time() - _weather["t"] < ttl or (_weather["error"] and time.time() - _weather["t"] < 120):
            return {"place": c.get("name", ""), "data": _weather["d"], "error": _weather["error"]}
        try:
            qs = urllib.parse.urlencode({
                "latitude": lat, "longitude": lon, "timezone": c.get("timezone") or "auto", "forecast_days": 4,
                "temperature_unit": units, "wind_speed_unit": "mph" if units == "fahrenheit" else "kmh",
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"})
            r = json.load(urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + qs, timeout=10))
            cur, d = r.get("current") or {}, r.get("daily") or {}
            days = [{"date": d["time"][i], "code": d["weather_code"][i], "max": d["temperature_2m_max"][i],
                     "min": d["temperature_2m_min"][i], "rain": (d.get("precipitation_probability_max") or [None] * 9)[i]}
                    for i in range(len(d.get("time") or []))]
            _weather.update(t=time.time(), error=None, d={"units": units,
                "temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
                "humidity": cur.get("relative_humidity_2m"), "wind": cur.get("wind_speed_10m"),
                "code": cur.get("weather_code"), "is_day": cur.get("is_day"), "time": cur.get("time"),
                "sunrise": (d.get("sunrise") or [""])[0][-5:], "sunset": (d.get("sunset") or [""])[0][-5:], "days": days})
        except Exception as e:
            print("weather error:", str(e)[:200])
            _weather.update(t=time.time(), error="weather_unavailable")  # keep last good data
        return {"place": c.get("name", ""), "data": _weather["d"], "error": _weather["error"]}


# ---------------------------------------------------------------- host (Pi) stats from /proc
def host():
    out = {}
    try:
        with open(HOST_PROC + "/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        idle, total = v[3] + v[4], sum(v[:8])
        prev = _host["prev"]
        if prev and total > prev[1]:
            out["cpu"] = round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)
        _host["prev"] = (idle, total, time.time())
        if prev is None:  # first call: tiny second sample
            time.sleep(0.5)
            return host()
    except Exception:
        pass
    try:
        with open(HOST_PROC + "/loadavg") as f:
            out["load"] = [float(x) for x in f.read().split()[:3]]
        out["cores"] = os.cpu_count()
        m = {}
        with open(HOST_PROC + "/meminfo") as f:
            for line in f:
                k, val = line.split(":", 1)
                m[k] = int(val.split()[0])
        out["mem_total"] = m["MemTotal"] * 1024
        out["mem_used"] = (m["MemTotal"] - m["MemAvailable"]) * 1024
        with open(HOST_PROC + "/uptime") as f:
            out["uptime"] = int(float(f.read().split()[0]))
    except Exception:
        pass
    try:
        with open(THERMAL) as f:
            out["temp"] = round(int(f.read()) / 1000, 1)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- Docker (GET-only, unix socket or TCP proxy)
class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.sock_path)


def docker_configured():
    return bool(DOCKER_TCP) or os.path.exists(DOCKER_SOCK)


def dget(path, timeout=6):
    if DOCKER_TCP:
        conn = http.client.HTTPConnection(DOCKER_TCP.hostname, DOCKER_TCP.port or 2375, timeout=timeout)
    else:
        conn = UnixHTTP(DOCKER_SOCK, timeout)
    try:
        conn.request("GET", path, headers={"Host": "docker"})
        r = conn.getresponse()
        body = r.read(4_000_000)
        if r.status != 200:
            raise RuntimeError(f"docker HTTP {r.status}")
        return json.loads(body)
    finally:
        conn.close()


def _stats(cid):
    try:
        return cid, dget(f"/containers/{cid}/stats?stream=false&one-shot=true", 8)
    except Exception:
        return cid, None


def _sample(ids):
    with ThreadPoolExecutor(6) as ex:
        return dict(ex.map(_stats, ids))


def containers():
    """Name/state/CPU%/memory for all containers; cached 20 s, CPU from deltas between our own samples."""
    with dlock:
        if _dock["d"] is not None and time.time() - _dock["t"] < 20:
            return _dock["d"]
        if not docker_configured():
            return {"error": "docker_disabled", "items": []}
        try:
            lst = dget("/containers/json?all=1")
        except Exception as e:
            print("docker error:", str(e)[:200])
            return {"error": "docker_unavailable", "items": []}
        running = [c["Id"] for c in lst if c.get("State") == "running"]
        stats = _sample(running)
        prev = _dock["prev"]
        if not prev:  # first time: take a second sample so CPU% is meaningful
            first = {k: v for k, v in stats.items() if v}
            time.sleep(1.0)
            stats = _sample(running)
            prev = {k: (v["cpu_stats"]["cpu_usage"]["total_usage"], v["cpu_stats"].get("system_cpu_usage", 0))
                    for k, v in first.items() if v.get("cpu_stats")}
        newprev, items = {}, []
        for c in lst:
            name = (c.get("Names") or ["?"])[0].lstrip("/")
            it = {"name": name, "state": c.get("State", ""), "status": c.get("Status", ""),
                  "image": (c.get("Image") or "").split("@")[0][:60], "cpu": None, "mem": None, "mem_limit": None,
                  "project": (c.get("Labels") or {}).get("com.docker.compose.project", "")}
            s = stats.get(c["Id"])
            if s and s.get("cpu_stats"):
                cs = s["cpu_stats"]
                tot, sys_ = cs["cpu_usage"]["total_usage"], cs.get("system_cpu_usage", 0)
                newprev[c["Id"]] = (tot, sys_)
                p = prev.get(c["Id"])
                ncpu = cs.get("online_cpus") or os.cpu_count() or 1
                if p and sys_ > p[1] and tot >= p[0]:
                    it["cpu"] = round((tot - p[0]) / (sys_ - p[1]) * ncpu * 100, 1)
                ms = s.get("memory_stats") or {}
                if ms.get("usage") is not None:
                    st = ms.get("stats") or {}
                    it["mem"] = max(0, ms["usage"] - st.get("inactive_file", st.get("total_inactive_file", 0)))
                    lim = ms.get("limit") or 0
                    it["mem_limit"] = lim if lim and lim < (1 << 50) else None
            items.append(it)
        items.sort(key=lambda i: (i["state"] != "running", -(i["cpu"] or 0), i["name"]))
        res = {"error": None, "items": items, "running": sum(i["state"] == "running" for i in items), "total": len(items)}
        _dock.update(t=time.time(), d=res, prev=newprev)
        return res


def system():
    show_docker = (cfg().get("docker") or {}).get("enabled", True) is not False
    return {"host": host(), "docker": containers() if show_docker else {"error": "docker_disabled", "items": []}}


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    server_version = "pi-home-dash"
    sys_version = ""

    def log_message(self, *a):
        pass

    def send(self, code, ctype, body, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def js(self, obj):
        self.send(200, "application/json", json.dumps(obj, ensure_ascii=False).encode())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        try:
            if p in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self.send(200, "text/html; charset=utf-8", f.read())
            if p == "/healthz":
                return self.send(200, "text/plain", b"ok")
            if p == "/api/config":
                c = cfg()
                icons = set(os.listdir(ICONS)) if os.path.isdir(ICONS) else set()

                def tile(t):
                    ic = t.get("icon", "")
                    return {"name": t.get("name", ""), "url": t.get("url", ""), "ping": bool(t.get("ping")),
                            "invert": bool(t.get("icon_invert")),
                            "icon": next((f"/icons/{ic}.{e}" for e in ("svg", "png", "webp") if f"{ic}.{e}" in icons), "")}
                groups = [{"name": g.get("name", ""), "tiles": [tile(t) for t in g.get("tiles") or []]} for g in c.get("groups") or []]
                if c.get("tiles"):  # flat list without groups is also accepted
                    groups.append({"name": "", "tiles": [tile(t) for t in c["tiles"]]})
                im, ui = c.get("immich") or {}, c.get("ui") or {}
                show_docker = (c.get("docker") or {}).get("enabled", True) is not False
                return self.js({"title": c.get("title", "Home"), "groups": groups, "version": VERSION,
                                "ui": {"locale": str(ui.get("locale") or "en"), "timezone": ui.get("timezone") or None,
                                       "clock_24h": ui.get("clock_24h", True) is not False,
                                       "host_label": str(ui.get("host_label") or "")},
                                "features": {"photos": bool(IMMICH) and im.get("enabled", True) is not False,
                                             "weather": weather_enabled(c.get("weather") or {}),
                                             "docker": show_docker and docker_configured()},
                                "photo_link": bool(IMMICH_PUBLIC),
                                "photo_minutes": max(1, int(im.get("refresh_minutes", 15))),
                                "slide_seconds": max(4, int(im.get("slide_seconds", 8))),
                                "default_album": str(im.get("album") or "").strip()})
            if p == "/api/status":
                return self.js(status())
            if p == "/api/albums":
                if not IMMICH or not KEY:
                    return self.js({"albums": [], "error": "disabled" if not IMMICH else "needs_key"})
                al, err = albums()
                return self.js({"albums": al, "error": err})
            if p == "/api/photos":
                sel = (q.get("album") or [""])[0]
                if sel not in ("", "all") and not UUID.match(sel):
                    return self.send(400, "text/plain", b"bad album")
                return self.js(feed(sel, force=(q.get("new") or [""])[0] == "1"))
            if p == "/api/weather":
                return self.js(weather())
            if p == "/api/system":
                return self.js(system())
            if p.startswith("/icons/"):
                name = p[7:]
                if not ICON_RE.match(name):
                    return self.send(404, "text/plain", b"not found")
                ctype = {"svg": "image/svg+xml", "png": "image/png", "webp": "image/webp"}[name.rsplit(".", 1)[1]]
                with open(os.path.join(ICONS, name), "rb") as f:
                    return self.send(200, ctype, f.read(), "public, max-age=86400")
            if p.startswith("/thumb/"):
                aid = p[7:]
                size = "preview" if (q.get("size") or [""])[0] == "preview" else "thumbnail"
                if not UUID.match(aid) or not KEY or not IMMICH:
                    return self.send(404, "text/plain", b"not found")
                item = thumb(aid, size)
                if not item:
                    return self.send(404, "text/plain", b"not found")
                return self.send(200, item[0], item[1], "private, max-age=3600")
            return self.send(404, "text/plain", b"not found")
        except FileNotFoundError:
            return self.send(404, "text/plain", b"not found")
        except Exception as e:
            print("error", p, type(e).__name__)
            return self.send(502, "text/plain", b"upstream error")


if __name__ == "__main__":
    cfg()
    print(f"pi-home-dash {VERSION} on {LISTEN}:{PORT} | immich", ("key set" if KEY else "key MISSING") if IMMICH else "off",
          "| docker", ("tcp " + DOCKER_TCP.netloc) if DOCKER_TCP else ("socket" if os.path.exists(DOCKER_SOCK) else "off"))
    threading.Thread(target=lambda: (host(), containers(), weather()), daemon=True).start()  # warm caches
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer((LISTEN, PORT), H).serve_forever()
