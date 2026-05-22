# AGENTS.md — fh6-web

Guidance for AI agents working in this repository.

## What this is

`fh6-web` is a self-contained web telemetry dashboard for Forza Horizon 6.
It is two things:

1. **`server.py`** — a Python (FastAPI + asyncio) server that receives FH6 UDP
   telemetry packets and fans them out to browsers over WebSocket.
2. **`static/index.html`** — a single-file vanilla JS/HTML frontend with a
   Leaflet map and per-player telemetry cards.

There is no build step. The frontend is served as-is by the Python server.

## Repository layout

```
server.py            # All backend logic (UDP, WebSocket, player state)
requirements.txt     # fastapi, uvicorn[standard], websockets
Dockerfile           # python:3.12-slim + uv; exposes 8080/tcp, 20440/udp
docker-compose.yml   # Single service mapping those ports
static/
  index.html         # Complete frontend (CSS + JS inline)
  maptiles/          # FH6 Japan XYZ tiles, z9–z14 (~60 MB, committed)
```

## Running locally

The only supported way to run is Docker (or Podman, which is a drop-in):

```bash
# Build and start
docker compose up -d --build

# View logs
docker logs fh6-web -f
```

**Always rebuild the image after making changes** (`docker compose up -d --build`).
Do not `docker cp` files into a running container — changes will be lost on the
next rebuild and can mask real issues.

Do **not** install Python packages on the host directly. Use the container.

## Key constraints

### Packet parser (`server.py`)
- The FH6 "Car Dash" UDP packet is 323–339 bytes, little-endian.
- All field offsets are hardcoded and verified against the Rust reference
  implementation in the sibling `fh6-tel` project (`src-tauri/src/parser.rs`).
- Tire temperatures arrive in Fahrenheit and are converted to Celsius in
  `parse_packet()`. Do not change this.
- Packets shorter than 323 bytes are silently dropped.
- The optional tire-wear fields (bytes 323–338) are only parsed when present.

### Player identity
- Players are identified solely by their UDP source IP string.
- `PlayerState` holds the last packet and a `deque(maxlen=500)` position trace.
- Players are evicted after `PLAYER_TIMEOUT_S = 30` seconds of silence by
  `player_timeout_loop`, which runs as a background asyncio task.
- Player colours cycle through `PLAYER_COLORS` (8 entries); they are never
  reused within a session.

### WebSocket protocol
Three message types flow from server → browser. Do not rename or restructure
these without updating the corresponding handler in `index.html`:

| `type` | Key field | When sent |
|---|---|---|
| `state` | `players` (dict keyed by IP) | On every new WS connection |
| `update` | `player_id`, `packet`, `trace` | On every parsed UDP packet |
| `player_left` | `player_id` | When a player times out |

The `update` message uses `player_id` (not `id`). The `state` message players
dict entries use `id`. The frontend `handleMessage()` normalises these.

### Frontend (`static/index.html`)
- No framework, no build step, no npm. Keep it that way.
- Leaflet is loaded from CDN **without** integrity/crossorigin attributes
  (the SRI hashes in unpkg URLs are unstable).
- Map CRS: `L.Util.extend({}, L.CRS.Simple, { transformation: new L.Transformation(1, 0, 1, 0) })`.
  This keeps Y-down to match the XYZ tile pyramid. Do not change the CRS.
- Calibration constants `CAL_A_WORLD`, `CAL_A_PIX`, `CAL_B_WORLD`, `CAL_B_PIX`
  map FH6 world coordinates to full-resolution tile pixels. Derived from the
  `FH6_JAPAN` preset in the sibling project's `src/lib/mapDefaults.ts`.
- Player state lives in the `players: Map<string, object>` in JS. Each entry
  holds the Leaflet polyline, marker, DOM card element, and last packet.

### Map tiles
- `static/maptiles/{z}/{y}/{x}.jpg`, zoom levels 9–14, ~60 MB total.
- The tile path order is `z/y/x` (not the more common `z/x/y`). The Leaflet
  tile URL template `/maptiles/{z}/{y}/{x}.jpg` is correct.
- Tiles are committed to the repo. Do not add them to `.gitignore`.

## Common tasks

**Add a new telemetry field to the card:**
1. Confirm the field exists in `Packet.to_camel_dict()` in `server.py`.
2. Add a DOM element with a predictable id in `createCard()` in `index.html`.
3. Update it in `updateCard()`.

**Change the player timeout:**
Adjust `PLAYER_TIMEOUT_S` in `server.py`. The timeout loop wakes every 5 s,
so the effective granularity is 5 s.

**Add a new WebSocket message type:**
1. Broadcast it in `server.py` via `app_state.broadcast_json({...})`.
2. Add a `case` in `handleMessage()` in `index.html`.

**Change the UDP port:**
Set `UDP_PORT` environment variable. The default is `20440`.

## What to avoid

- Do not add a JS build pipeline (webpack, vite, etc.) to the frontend.
  The single-file constraint is intentional.
- Do not add a database or session persistence. This is a live-only dashboard.
- Do not pin Leaflet to a specific SRI hash in the CDN `<script>` tag.
- Do not use `@app.on_event("startup")` — the project uses the modern
  `lifespan=` context manager pattern.
- Do not change the tile path order from `z/y/x` to `z/x/y`.
