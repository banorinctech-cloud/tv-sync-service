# TV Welcome-Screen Sync Service

Small server that sits between Hospitable and the TVs in your properties.

- Polls the Hospitable Public API for the guests currently in-house
- Serves `GET /api/now?property=<uuid>` — the endpoint each TV app polls
  every few minutes to show the current guest's name
- Hosts a persistent advert library (`POST /api/adverts` to upload
  images/video, `DELETE` to remove) that the TVs rotate through
- `/tv?property=<uuid>` is a bare-bones live welcome screen for testing

Stdlib only — no dependencies, no build step.

## Run it

```bash
export HOSPITABLE_TOKEN="<personal access token, reservation:read scope>"
export POLL_INTERVAL=600   # seconds between Hospitable polls
export PORT=8090
python3 server.py
```

Without `HOSPITABLE_TOKEN` it runs in demo mode, serving `data/guests.json`
as-is (generate one with `hospitable current-guests > data/guests.json`).

## Deploy it (so the TVs can reach it 24/7)

Any always-on host works. Easiest options:

- **Railway / Render / Fly.io** — connect this folder as a repo, set the
  `HOSPITABLE_TOKEN` env var, expose the port. ~$5/mo.
- **A mini PC / Raspberry Pi on your home network** — `python3 server.py`
  under systemd; TVs reach it at `http://<lan-ip>:8090`.

## TV wiring

- **Samsung (Tizen):** package the welcome-screen app as `.wgt` (Tizen
  Studio), install via Developer Mode. The app polls
  `http://<server>/api/now?property=<hospitable-property-uuid>` and
  `/api/adverts`, refreshes the name on change.
- **Vizio:** same web app, launched to the TV through the built-in
  Chromecast dev launcher.

## Power behavior

- **Auto-open on power-on:** Samsung → Settings → General → Smart Features
  → *Autorun Last App*. The TV reopens whatever was on screen when it went
  to standby, so it lands back on the welcome screen by itself.
- **Turn on at check-in:** an app can't wake a fully-off TV (nothing runs
  when it's off). Options:
  1. Leave the TV on the welcome screen 24/7 (what most signage does).
  2. Samsung's On/Off Timer for fixed daily schedules.
  3. Wake-on-LAN: a tiny device on the property's network sends a magic
     packet to the TV's MAC at check-in time; with Autorun Last App it wakes
     straight into the welcome screen. Needs the TV on the same LAN and WoL
     enabled.
- **Vizio caveat:** a cast web-app session does not survive a reboot — it
  must be relaunched after the TV restarts.

## Property UUIDs

Run `hospitable properties` (workspace skill) to list them, or
`GET /api/properties` on a running server.
