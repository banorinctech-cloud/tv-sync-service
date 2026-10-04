#!/usr/bin/env python3
"""TV welcome-screen sync service.

Polls Hospitable for the guests currently in-house and serves that data to
the TV welcome-screen apps, plus a persistent advert library.

Environment:
  HOSPITABLE_TOKEN   Personal Access Token (live mode). If unset, the service
                     serves data/guests.json as-is (demo/test mode).
  POLL_INTERVAL      Seconds between Hospitable polls (default 600).
  PORT               HTTP port (default 8090).
  DATA_DIR           Storage dir (default ./data).

Endpoints:
  GET  /api/health
  GET  /api/properties                 -> [{id, name}]
  GET  /api/now?property=<uuid>        -> {property_name, current_guest|null, updated_at}
  GET  /api/adverts                    -> [{id, filename, duration_secs, properties[]}]
  POST /api/adverts                    multipart: file, duration_secs, properties[] (repeatable)
  DELETE /api/adverts/<id>
  GET  /tv?property=<uuid>             -> live welcome-screen preview (for testing)
  GET  /adverts/<file>                 -> advert file bytes
"""
import datetime
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOSPITABLE_BASE = "https://public.api.hospitable.com/v2"
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
ADVERT_DIR = os.path.join(DATA_DIR, "adverts")
GUESTS_FILE = os.path.join(DATA_DIR, "guests.json")
ADVERTS_FILE = os.path.join(DATA_DIR, "adverts.json")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "600"))
TOKEN = os.environ.get("HOSPITABLE_TOKEN", "").strip()

os.makedirs(ADVERT_DIR, exist_ok=True)

_state = {"guests": {}, "updated_at": None, "live": bool(TOKEN)}


def _hosp_get(path, params=None):
    url = HOSPITABLE_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _parse_dt(s):
    try:
        dt = datetime.datetime.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def poll_hospitable():
    """Refresh the current-guest cache from Hospitable."""
    try:
        today = datetime.date.today()
        today_s = today.isoformat()
        window_start = (today - datetime.timedelta(days=60)).isoformat()
        now = datetime.datetime.now(datetime.timezone.utc)
        props = _hosp_get("/properties").get("data", [])
        out = {}
        # One request per property: the batched response carries no
        # property identifier, so per-property queries are the only way
        # to attribute a stay.
        for p in props:
            query = [("start_date", window_start), ("end_date", today_s),
                     ("include", "guest"), ("per_page", "100"),
                     ("properties[]", p["id"])]
            res = _hosp_get("/reservations", query).get("data", [])
            current = None
            for r in res:
                st = (r.get("reservation_status") or {}).get("current", {}).get("category")
                arr = (r.get("arrival_date") or "")[:10]
                dep = (r.get("departure_date") or "")[:10]
                # Prefer exact check-in/out datetimes so the guest stays on
                # screen until actual checkout time (not midnight UTC).
                ci = _parse_dt(r.get("check_in"))
                co = _parse_dt(r.get("check_out"))
                if ci is not None and co is not None:
                    is_current = (ci <= now < co)
                else:
                    is_current = (arr <= today_s < dep)
                if st == "accepted" and is_current:
                    g = r.get("guest") or {}
                    current = {
                        "guest_name": (g.get("first_name", "") + " " + (g.get("last_name") or "")).strip(),
                        "arrival_date": arr,
                        "departure_date": dep,
                        "check_in": r.get("check_in"),
                        "check_out": r.get("check_out"),
                        "platform": r.get("platform"),
                        "guest_count": (r.get("guests") or {}).get("total"),
                    }
            out[p["id"]] = {"property_name": p["name"],
                            "current_guest": current}
        _state["guests"] = out
        _state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(GUESTS_FILE, "w") as f:
            json.dump(out, f, indent=2)
        print(f"[sync] refreshed {len(out)} properties at {_state['updated_at']}", flush=True)
    except Exception as exc:  # keep serving stale cache on failure
        print(f"[sync] poll failed: {exc}", flush=True)


def poll_loop():
    while True:
        poll_hospitable()
        time.sleep(POLL_INTERVAL)


def load_adverts():
    if os.path.exists(ADVERTS_FILE):
        with open(ADVERTS_FILE) as f:
            return json.load(f)
    return []


def save_adverts(items):
    with open(ADVERTS_FILE, "w") as f:
        json.dump(items, f, indent=2)


TV_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Welcome</title>
<style>
  *{margin:0;box-sizing:border-box}
  body{background:#0b1020;color:#fff;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;
       height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
  .kicker{letter-spacing:.35em;text-transform:uppercase;color:#8fa3c7;font-size:clamp(12px,1.6vw,20px)}
  h1{font-size:clamp(40px,7vw,110px);margin:18px 0 8px;font-weight:700}
  .sub{color:#c7d3e8;font-size:clamp(16px,2.2vw,30px)}
  .meta{margin-top:26px;color:#8fa3c7;font-size:clamp(13px,1.6vw,20px)}
  #ad{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:#000}
  #ad img,#ad video{max-width:100%;max-height:100%}
</style></head><body>
<div class="kicker">Welcome to</div>
<h1 id="prop">—</h1>
<div class="sub" id="guest"></div>
<div class="meta" id="dates"></div>
<div id="ad"></div>
<script>
const PID = new URLSearchParams(location.search).get('property') || '';
async function tick(){
  try{
    const r = await fetch('/api/now?property='+encodeURIComponent(PID));
    const j = await r.json();
    document.getElementById('prop').textContent = j.property_name || '—';
    const g = j.current_guest;
    document.getElementById('guest').textContent = g ? ('Welcome, ' + g.guest_name + ' 🎉') : 'Ready for our next guest';
    document.getElementById('dates').textContent = g ? (g.arrival_date + ' → ' + g.departure_date) : '';
  }catch(e){}
}
tick(); setInterval(tick, 60000);
</script></body></html>
"""


HOME_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TV Sync Service</title>
<style>
  body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;max-width:640px;
       margin:40px auto;padding:0 20px;color:#1c2733;line-height:1.6}
  .pill{display:inline-block;padding:4px 12px;border-radius:20px;font-size:13px;
        background:#e8f5e9;color:#2e7d32}
  .pill.off{background:#fce4ec;color:#c62828}
  code{background:#f4f6f8;padding:2px 6px;border-radius:4px;font-size:13px}
  li{margin:6px 0}
</style></head><body>
<h1>TV Sync Service</h1>
<p>Status: <span class="pill" id="st">checking&hellip;</span></p>
<p>Polls Hospitable for in-house guests and serves the data to the TV
welcome-screen apps.</p>
<h3>Endpoints</h3>
<ul>
  <li><code>/api/health</code> &mdash; service status</li>
  <li><code>/api/properties</code> &mdash; property list</li>
  <li><code>/api/now?property=&lt;uuid&gt;</code> &mdash; current guest for a property</li>
  <li><code>/tv?property=&lt;uuid&gt;</code> &mdash; welcome-screen preview</li>
  <li><code>/api/adverts</code> &mdash; advert library (GET list, POST upload)</li>
</ul>
<script>
fetch('/api/health').then(r=>r.json()).then(j=>{
  const el=document.getElementById('st');
  el.textContent=j.live?'live \u00b7 updated '+j.updated_at:'demo mode';
  if(!j.live)el.classList.add('off');
}).catch(()=>{const el=document.getElementById('st');
  el.textContent='unreachable';el.classList.add('off');});
</script></body></html>
"""

class Handler(BaseHTTPRequestHandler):
    server_version = "TVSync/1.0"

    def _send(self, code, obj, ctype="application/json"):
        body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _parse(self):
        return urllib.parse.urlparse(self.path), urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query)

    def do_GET(self):
        parsed, qs = self._parse()
        if parsed.path == "/":
            return self._send(200, HOME_PAGE, "text/html")
        if parsed.path == "/api/health":
            return self._send(200, {"ok": True, "live": _state["live"],
                                    "updated_at": _state["updated_at"]})
        if parsed.path == "/api/properties":
            return self._send(200, [{"id": pid, "name": v["property_name"]}
                                    for pid, v in _state["guests"].items()])
        if parsed.path == "/api/now":
            pid = qs.get("property", [""])[0]
            info = _state["guests"].get(pid)
            if not info:
                return self._send(404, {"error": "unknown property"})
            return self._send(200, {**info, "updated_at": _state["updated_at"]})
        if parsed.path == "/api/adverts":
            return self._send(200, load_adverts())
        if parsed.path.startswith("/adverts/"):
            name = os.path.basename(parsed.path[len("/adverts/"):])
            path = os.path.join(ADVERT_DIR, name)
            if not os.path.isfile(path):
                return self._send(404, {"error": "not found"})
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return self.wfile.write(body)
        if parsed.path == "/tv":
            return self._send(200, TV_PAGE, "text/html")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed, _ = self._parse()
        if parsed.path != "/api/adverts":
            return self._send(404, {"error": "not found"})
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._send(400, {"error": "multipart upload required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send(400, {"error": "missing content length"})
        if length <= 0 or length > 100 * 1024 * 1024:
            return self._send(400, {"error": "bad content length"})
        body = self.rfile.read(length)
        # Manual multipart parse (no deprecated cgi module).
        boundary = None
        for part in ctype.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip().strip('"')
        if not boundary:
            return self._send(400, {"error": "missing multipart boundary"})
        delim = ("--" + boundary).encode()
        fields = {}
        files = {}
        for chunk in body.split(delim):
            if b'Content-Disposition' not in chunk:
                continue
            head, _, data = chunk.partition(b"\r\n\r\n")
            if not data:
                continue
            data = data.rsplit(b"\r\n", 1)[0]
            disp = ""
            for line in head.decode("latin1").split("\r\n"):
                if "Content-Disposition" in line:
                    disp = line
                    break
            name = None
            filename = None
            for seg in disp.split(";"):
                seg = seg.strip()
                if seg.startswith("name="):
                    name = seg[5:].strip().strip('"')
                elif seg.startswith("filename="):
                    filename = seg[9:].strip().strip('"')
            if not name:
                continue
            if filename:
                files[name] = (os.path.basename(filename), data)
            else:
                fields.setdefault(name, []).append(data.decode("utf-8", "replace"))
        if "file" not in files or not files["file"][0]:
            return self._send(400, {"error": "file field required"})
        item_id = uuidlib.uuid4().hex[:12]
        orig_name, file_data = files["file"]
        safe = "".join(c for c in orig_name
                       if c.isalnum() or c in "._-") or "upload.bin"
        fname = f"{item_id}_{safe}"
        with open(os.path.join(ADVERT_DIR, fname), "wb") as f:
            f.write(file_data)
        try:
            duration = int((fields.get("duration_secs") or ["10"])[0])
        except (TypeError, ValueError):
            duration = 10
        props = fields.get("properties[]") or fields.get("properties") or []
        items = load_adverts()
        items.append({"id": item_id, "filename": fname, "duration_secs": duration,
                      "properties": props,
                      "url": f"/adverts/{fname}"})
        save_adverts(items)
        return self._send(200, items[-1])

    def do_DELETE(self):
        parsed, _ = self._parse()
        if not parsed.path.startswith("/api/adverts/"):
            return self._send(404, {"error": "not found"})
        item_id = parsed.path.rsplit("/", 1)[-1]
        items = [a for a in load_adverts() if a["id"] != item_id]
        removed = [a for a in load_adverts() if a["id"] == item_id]
        for a in removed:
            try:
                os.remove(os.path.join(ADVERT_DIR, a["filename"]))
            except OSError:
                pass
        save_adverts(items)
        return self._send(200, {"ok": True})

    def log_message(self, *args):
        pass


def main():
    if TOKEN:
        threading.Thread(target=poll_loop, daemon=True).start()
    elif os.path.exists(GUESTS_FILE):
        with open(GUESTS_FILE) as f:
            _state["guests"] = json.load(f)
        print("[sync] demo mode: serving", GUESTS_FILE, flush=True)
    port = int(os.environ.get("PORT", "8090"))
    print(f"[sync] listening on :{port} (live={bool(TOKEN)})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
