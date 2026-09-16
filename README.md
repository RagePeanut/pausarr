# Pausarr

Pause your torrents while you're actually watching something — from **any** source.

Pausarr is a tiny always-on service that sits between your media sources and
qBittorrent. Each source raises a flag when it wants torrenting paused; Pausarr
**pauses all torrents while any flag is active and resumes them once every flag
is clear.** It replaces a single-purpose Tautulli→qBittorrent script with a
general hub that also handles YouTube/Twitch on a TV, apps on your phone, or
anything else that can send an HTTP request.

---

## Why

A typical setup pauses torrents when Plex starts playing (via Tautulli) so your
stream doesn't buffer. But playback isn't the only thing that wants your
bandwidth: opening YouTube or Twitch on the TV, or a streaming app on your
phone, deserves the same treatment. Those sources can't all speak Tautulli's
webhook format, and some can't reliably send a "stopped" event at all.

Pausarr solves this with **two kinds of sources** and one simple rule.

---

## How it works

### The rule

> **Pause all torrents while _any_ flag is active. Resume once _all_ flags are clear.**

Flags are keyed by an arbitrary **`tag`** you choose per source (`plex`,
`youtube-tv`, `twitch-tv`, `youtube-phone`, …). Tags are created dynamically on
the first request — there's nothing to pre-register.

### Two source types

| Type | Endpoint | Who | How it clears |
|------|----------|-----|---------------|
| **Push** (event-driven) | `POST /pause` | Sources that emit start/stop events, e.g. **Tautulli/Plex** | Explicitly, when the source sends `resume` |
| **Heartbeat** (time-driven) | `POST /heartbeat` | Sources that can only be *polled*, e.g. **YouTube/Twitch on a TV** via Tasker or Home Assistant | Automatically, when no ping arrives within `HEARTBEAT_TIMEOUT` |

A **push** flag stays exactly where the source last set it — it never expires.
Send `pause` and it stays paused until you send `resume`.

A **heartbeat** flag is kept alive by periodic pings. As long as a ping arrives
within the `HEARTBEAT_TIMEOUT` window (default **180 s**), the flag is active.
Stop pinging — because you closed the app — and the flag expires on its own,
resuming torrents. So a client just needs to ping every minute *while* the app
is open and do nothing when it isn't; there's no need to detect "app closed".

### Under the hood

- A background **watchdog** runs every `POLL_INTERVAL` seconds to expire stale
  heartbeats and reconcile qBittorrent. Reconciliation also runs immediately
  after every request, so pauses take effect instantly.
- qBittorrent is only called **when the desired state changes**, so it isn't
  spammed on every poll.
- All flag changes are serialised behind a lock, so overlapping requests from
  multiple sources can't race.
- State is **persisted to a JSON file** (`STATE_FILE`) and reloaded on startup,
  so a restart doesn't forget an active pause. Stale heartbeats are expired
  immediately on boot.

```
  Tautulli ──POST /pause {tag:"plex", request:"pause"|"resume"}──┐
                                                                 │
  Tasker/HA ─POST /heartbeat {tag:"youtube-tv"} every 60s ───────┤
                                                                 ▼
                                                          ┌─────────────┐
                                                          │   Pausarr   │  any flag active?
                                                          │ flag store  │ ───────────────► pause_all
                                                          │  + watchdog │  all clear?
                                                          └─────────────┘ ───────────────► resume_all
                                                                 │
                                                                 ▼
                                                           qBittorrent
```

---

## Quick start

### Option A — pull the prebuilt image from GHCR (recommended)

A multi-arch image (`linux/amd64` + `linux/arm64`) is published to the GitHub
Container Registry on every push to `main` and every version tag:

```bash
docker run -d --name pausarr \
  --restart unless-stopped \
  -p 8080:8080 \
  -v "$(pwd)/data:/data" \
  -e QBITTORRENT_URL="http://<qbittorrent-host>:8080" \
  ghcr.io/ragepeanut/pausarr:latest
```

Available tags:

| Tag | Points to |
|-----|-----------|
| `latest` | Newest build from `main` |
| `1.2.3`, `1.2` | A specific released version (from a `v1.2.3` Git tag) |
| `main` | Latest `main` build |
| `sha-<short-sha>` | An exact commit |

Pin to a version tag (e.g. `ghcr.io/ragepeanut/pausarr:1.2`) for reproducible
deploys, or track `latest` to always get the newest build.

### Option B — build it yourself

```bash
git clone https://github.com/RagePeanut/pausarr.git
cd pausarr
docker build -t pausarr .

docker run -d --name pausarr \
  --restart unless-stopped \
  -p 8080:8080 \
  -v "$(pwd)/data:/data" \
  -e QBITTORRENT_URL="http://<qbittorrent-host>:8080" \
  pausarr
```

Add `QBITTORRENT_USER` / `QBITTORRENT_PASS` only if your Web UI requires a
login (see [Configuration](#configuration)).

**Running it alongside qBittorrent in Docker?** Add `pausarr` as a service in
your existing Compose stack so it shares the network and can reach qBittorrent
by container name. A minimal service definition:

```yaml
  pausarr:
    image: ghcr.io/ragepeanut/pausarr:latest   # or `build: /path/to/pausarr`
    container_name: pausarr
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      - QBITTORRENT_URL=http://qbittorrent:8080
      # - QBITTORRENT_USER=admin      # only if the Web UI requires auth
      # - QBITTORRENT_PASS=adminadmin
      - HEARTBEAT_TIMEOUT=180
      - POLL_INTERVAL=15
      - STATE_FILE=/data/state.json
    volumes:
      - ./pausarr-data:/data
```

Check it's alive and see current state:

```bash
curl http://localhost:8080/status
```

---

## Configuration

All configuration is via environment variables (see `.env.example`).

| Variable | Default | Description |
|----------|---------|-------------|
| `QBITTORRENT_URL` | `http://localhost:8080` | qBittorrent Web UI base URL |
| `QBITTORRENT_USER` | _(empty)_ | Web UI username — **optional**, see note below |
| `QBITTORRENT_PASS` | _(empty)_ | Web UI password — **optional**, see note below |
| `HEARTBEAT_TIMEOUT` | `180` | Seconds without a ping before a heartbeat flag expires (**global**) |
| `POLL_INTERVAL` | `15` | Seconds between watchdog runs (expiry + reconcile) |
| `STATE_FILE` | `/data/state.json` | Where flag state is persisted |
| `LOG_LEVEL` | `INFO` | Python log level |

> **Heartbeat clients should ping well within `HEARTBEAT_TIMEOUT`.** With the
> default 180 s window, ping every 60 s. That tolerates two missed pings before
> a false resume.

> **qBittorrent credentials are optional.** If your qBittorrent has
> *"Bypass authentication for clients on localhost"* or a whitelisted IP subnet
> that includes Pausarr, leave `QBITTORRENT_USER`/`QBITTORRENT_PASS` unset —
> Pausarr will talk to the Web API without logging in. If the server ever
> replies `403`, Pausarr will attempt to log in with whatever credentials you
> *did* provide and retry. Set both variables only if your Web UI requires a
> login.

---

## API

### `POST /pause` — push sources

```json
{ "tag": "plex", "request": "pause" }
```

`request` is `pause` or `resume`. Returns the current status snapshot.

### `POST /heartbeat` — heartbeat sources

```json
{ "tag": "youtube-tv" }
```

Call this on an interval (e.g. every 60 s) while the source is active. Returns
the current status snapshot.

### `GET /status` — debugging

```json
{
  "should_pause": true,
  "heartbeat_timeout": 180.0,
  "flags": {
    "plex":       { "kind": "push",      "active": false, "updated_at": 1700000000.0 },
    "youtube-tv": { "kind": "heartbeat", "active": true,  "last_seen": 1700000100.0,
                    "seconds_since_last_seen": 12.4, "expires_in": 167.6 }
  }
}
```

### `GET /healthz`

Liveness probe → `{"status": "ok"}`.

---

## Source setup recipes

### 1. Tautulli / Plex (push)

In Tautulli: **Settings → Notification Agents → Add → Webhook**.

- **Webhook URL:** `http://<pausarr-host>:8080/pause`
- **Method:** `POST`
- Enable triggers **Playback Start**, **Playback Stop**, **Playback Resume**,
  and **Playback Pause** (optional — see note).

For the **Playback Start** / **Playback Resume** triggers, set the JSON data to:

```json
{ "tag": "plex", "request": "pause" }
```

For the **Playback Stop** trigger, set:

```json
{ "tag": "plex", "request": "resume" }
```

> **Tip:** Whether you also treat *Playback Pause* as `resume` is your call. If
> you pause the movie to grab a snack, do you want torrents to resume during
> that gap? Wire the *Pause* trigger to `resume` if yes; leave it out if no.

### 2. YouTube / Twitch on Android TV via Home Assistant (heartbeat) — recommended for TVs

Home Assistant can read the TV's foreground app over ADB and ping Pausarr,
without ever touching a UI on the TV.

1. Add the [**Android Debug Bridge** integration](https://www.home-assistant.io/integrations/androidtv/)
   and point it at your TV (enable network debugging on the TV, authorise HA
   once). This gives a `media_player.<tv>` entity with a `current_app` attribute
   like `com.google.android.youtube.tv` or `tv.twitch.android.app`.
2. Add a REST command and an automation to `configuration.yaml`:

```yaml
rest_command:
  pausarr_heartbeat:
    url: "http://<pausarr-host>:8080/heartbeat"
    method: POST
    content_type: "application/json"
    payload: '{"tag": "{{ tag }}"}'

automation:
  - alias: "Pausarr - heartbeat while YouTube/Twitch on TV"
    trigger:
      - platform: time_pattern
        seconds: "/60"          # fire every 60s
    condition:
      - condition: template
        value_template: >
          {{ state_attr('media_player.living_room_tv', 'current_app')
             in ['com.google.android.youtube.tv', 'tv.twitch.android.app'] }}
    action:
      - service: rest_command.pausarr_heartbeat
        data:
          tag: "tv"
```

When you leave the app, the condition stops being true, pings stop, and Pausarr
resumes torrents within `HEARTBEAT_TIMEOUT`.

### 3. Android phone / TV via Tasker (heartbeat)

Great on a phone (Tasker's native app-foreground context is reliable there).

1. **Profile → Application** → select YouTube, Twitch, etc.
2. Linked **Task** → add a **Repeat** or use a recurring alarm to fire every
   60 s while the profile is active, with an **HTTP Request** action:
   - **Method:** `POST`
   - **URL:** `http://<pausarr-host>:8080/heartbeat`
   - **Headers:** `Content-Type: application/json`
   - **Body:** `{"tag": "youtube-phone"}`

Use distinct tags per device (`youtube-phone`, `youtube-tv`, …) so you can tell
them apart in `/status` — though the pause behaviour is identical regardless.

### 4. Anything else (curl)

```bash
# Push
curl -X POST http://<pausarr-host>:8080/pause \
  -H 'Content-Type: application/json' \
  -d '{"tag":"plex","request":"pause"}'

# Heartbeat (run on a 60s loop while active)
curl -X POST http://<pausarr-host>:8080/heartbeat \
  -H 'Content-Type: application/json' \
  -d '{"tag":"youtube-tv"}'
```

---

## Design notes & trade-offs

- **No auth.** Pausarr is intended for a trusted LAN. Anything that can reach it
  can pause your torrents. Don't expose it to the internet.
- **Pause = all torrents.** Pausarr stops/starts *all* torrents (`hashes=all`).
  It does not track which torrents it paused, so a resume will also start
  torrents you paused manually. This keeps it simple and predictable.
- **Push flags never auto-expire.** If a source sends `pause` and its `resume`
  is lost, that flag stays set. This is intentional (a missed resume shouldn't
  silently un-pause mid-movie); clear it manually via `/status` inspection and a
  `resume` call if needed.
- **qBittorrent v4 and v5.** v5.0 renamed `pause`/`resume` to `stop`/`start`.
  Pausarr tries the modern endpoint and falls back to the legacy one.

---

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn pausarr.app:app --reload --port 8080
```

## Publishing (maintainers)

Images are published to GHCR automatically by
[`.github/workflows/publish.yml`](.github/workflows/publish.yml):

- **On push to `main`** → `ghcr.io/ragepeanut/pausarr:latest` (and `:main`).
- **On a version tag** (`git tag v1.2.3 && git push --tags`) → `:1.2.3`,
  `:1.2`, and `:latest`.
- **Manually** from the repo's **Actions → Publish Docker image to GHCR → Run
  workflow**.

The workflow authenticates with the built-in `GITHUB_TOKEN`, so **no secrets
need to be configured** — it just needs `packages: write` permission, which the
workflow already requests.

**One-time setup — make the package public.** By default a newly published
GHCR package is private. To let anyone `docker pull` it without logging in:

1. Go to the package page: `https://github.com/users/RagePeanut/packages/container/package/pausarr`
   (or the repo's **Packages** sidebar entry after the first successful run).
2. **Package settings → Danger Zone → Change visibility → Public**.

Optionally, under the package's settings, link it to this repository and grant
the repo **Write** access so future workflow runs can keep pushing.

## License

MIT
