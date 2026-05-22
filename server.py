"""
Forza Horizon 6 telemetry server.

- Listens for UDP telemetry packets on UDP_PORT (default 20440).
- Players are identified by source IP address.
- Broadcasts JSON messages over WebSocket to all connected browser clients.
- Serves static frontend files from ./static/.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UDP_PORT: int = int(os.environ.get("UDP_PORT", "20440"))
PLAYER_TIMEOUT_S: float = 30.0
TRACE_MAXLEN: int = 500

PLAYER_COLORS = [
    "#3b82f6",  # blue
    "#ef4444",  # red
    "#22c55e",  # green
    "#f59e0b",  # amber
    "#a855f7",  # purple
    "#ec4899",  # pink
    "#14b8a6",  # teal
    "#f97316",  # orange
]

# ---------------------------------------------------------------------------
# Packet parsing
# ---------------------------------------------------------------------------

# fmt: off
_STRUCT = struct.Struct(
    "<"
    "i"   # 0   is_race_on         i32
    "I"   # 4   timestamp_ms       u32
    "fff" # 8   engine_max/idle/current rpm  f32 x3
    "fff" # 20  accel x/y/z        f32 x3
    "fff" # 32  vel x/y/z          f32 x3
    "xxx" # 44  pad (angular vel, 12 bytes)
    "x"   # (total 12 bytes skipped: offset 44-55)
    # We need offsets to line up — use one big format with explicit pads.
)
# fmt: on

# Rather than fighting with struct alignment, parse field by field with
# targeted unpacks using pre-built single-field structs for performance.

_i32 = struct.Struct("<i")
_u32 = struct.Struct("<I")
_f32 = struct.Struct("<f")
_u16 = struct.Struct("<H")
_u8 = struct.Struct("<B")
_i8 = struct.Struct("<b")


def _f(data: bytes, offset: int) -> float:
    return _f32.unpack_from(data, offset)[0]


def _i(data: bytes, offset: int) -> int:
    return _i32.unpack_from(data, offset)[0]


def _u(data: bytes, offset: int) -> int:
    return _u32.unpack_from(data, offset)[0]


def _b(data: bytes, offset: int) -> int:
    return _u8.unpack_from(data, offset)[0]


def _sb(data: bytes, offset: int) -> int:
    return _i8.unpack_from(data, offset)[0]


def _h(data: bytes, offset: int) -> int:
    return _u16.unpack_from(data, offset)[0]


def _f_to_c(fahrenheit: float) -> float:
    return (fahrenheit - 32.0) * 5.0 / 9.0


@dataclass
class Packet:
    is_race_on: int
    timestamp_ms: int
    engine_max_rpm: float
    engine_idle_rpm: float
    current_engine_rpm: float
    accel_x: float
    accel_y: float
    accel_z: float
    vel_x: float
    vel_y: float
    vel_z: float
    yaw: float
    pitch: float
    roll: float
    suspension_fl: float
    suspension_fr: float
    suspension_rl: float
    suspension_rr: float
    tire_slip_ratio_fl: float
    tire_slip_ratio_fr: float
    tire_slip_ratio_rl: float
    tire_slip_ratio_rr: float
    tire_slip_angle_fl: float
    tire_slip_angle_fr: float
    tire_slip_angle_rl: float
    tire_slip_angle_rr: float
    car_ordinal: int
    car_class: int
    car_pi: int
    drivetrain_type: int
    num_cylinders: int
    position_x: float
    position_y: float
    position_z: float
    speed_ms: float
    power: float
    torque: float
    tire_temp_fl: float
    tire_temp_fr: float
    tire_temp_rl: float
    tire_temp_rr: float
    boost: float
    fuel: float
    distance_traveled: float
    best_lap: float
    last_lap: float
    current_lap: float
    current_race_time: float
    lap_number: int
    race_position: int
    throttle: int
    brake: int
    clutch: int
    handbrake: int
    gear: int
    steer: int
    # optional (only if packet >= 339 bytes)
    tire_wear_fl: float | None = None
    tire_wear_fr: float | None = None
    tire_wear_rl: float | None = None
    tire_wear_rr: float | None = None

    def to_camel_dict(self) -> dict[str, Any]:
        """Return packet fields as camelCase dict ready for JSON serialisation."""
        d: dict[str, Any] = {
            "isRaceOn": self.is_race_on,
            "timestampMs": self.timestamp_ms,
            "engineMaxRpm": self.engine_max_rpm,
            "engineIdleRpm": self.engine_idle_rpm,
            "currentEngineRpm": self.current_engine_rpm,
            "accelX": self.accel_x,
            "accelY": self.accel_y,
            "accelZ": self.accel_z,
            "velX": self.vel_x,
            "velY": self.vel_y,
            "velZ": self.vel_z,
            "yaw": self.yaw,
            "pitch": self.pitch,
            "roll": self.roll,
            "suspensionFl": self.suspension_fl,
            "suspensionFr": self.suspension_fr,
            "suspensionRl": self.suspension_rl,
            "suspensionRr": self.suspension_rr,
            "tireSlipRatioFl": self.tire_slip_ratio_fl,
            "tireSlipRatioFr": self.tire_slip_ratio_fr,
            "tireSlipRatioRl": self.tire_slip_ratio_rl,
            "tireSlipRatioRr": self.tire_slip_ratio_rr,
            "tireSlipAngleFl": self.tire_slip_angle_fl,
            "tireSlipAngleFr": self.tire_slip_angle_fr,
            "tireSlipAngleRl": self.tire_slip_angle_rl,
            "tireSlipAngleRr": self.tire_slip_angle_rr,
            "carOrdinal": self.car_ordinal,
            "carClass": self.car_class,
            "carPi": self.car_pi,
            "drivetrainType": self.drivetrain_type,
            "numCylinders": self.num_cylinders,
            "positionX": self.position_x,
            "positionY": self.position_y,
            "positionZ": self.position_z,
            "speedMs": self.speed_ms,
            "power": self.power,
            "torque": self.torque,
            "tireTempFl": self.tire_temp_fl,
            "tireTempFr": self.tire_temp_fr,
            "tireTempRl": self.tire_temp_rl,
            "tireTempRr": self.tire_temp_rr,
            "boost": self.boost,
            "fuel": self.fuel,
            "distanceTraveled": self.distance_traveled,
            "bestLap": self.best_lap,
            "lastLap": self.last_lap,
            "currentLap": self.current_lap,
            "currentRaceTime": self.current_race_time,
            "lapNumber": self.lap_number,
            "racePosition": self.race_position,
            "throttle": self.throttle,
            "brake": self.brake,
            "clutch": self.clutch,
            "handbrake": self.handbrake,
            "gear": self.gear,
            "steer": self.steer,
            "tireWearFl": self.tire_wear_fl,
            "tireWearFr": self.tire_wear_fr,
            "tireWearRl": self.tire_wear_rl,
            "tireWearRr": self.tire_wear_rr,
        }
        return d


def parse_packet(data: bytes) -> Packet | None:
    """Parse a raw UDP datagram into a Packet, or return None if too short."""
    if len(data) < 323:
        log.debug("Dropping short packet (%d bytes)", len(data))
        return None

    try:
        pkt = Packet(
            is_race_on=_i(data, 0),
            timestamp_ms=_u(data, 4),
            engine_max_rpm=_f(data, 8),
            engine_idle_rpm=_f(data, 12),
            current_engine_rpm=_f(data, 16),
            accel_x=_f(data, 20),
            accel_y=_f(data, 24),
            accel_z=_f(data, 28),
            vel_x=_f(data, 32),
            vel_y=_f(data, 36),
            vel_z=_f(data, 40),
            # 44-55: skip angular velocity (12 bytes)
            yaw=_f(data, 56),
            pitch=_f(data, 60),
            roll=_f(data, 64),
            suspension_fl=_f(data, 68),
            suspension_fr=_f(data, 72),
            suspension_rl=_f(data, 76),
            suspension_rr=_f(data, 80),
            tire_slip_ratio_fl=_f(data, 84),
            tire_slip_ratio_fr=_f(data, 88),
            tire_slip_ratio_rl=_f(data, 92),
            tire_slip_ratio_rr=_f(data, 96),
            # 100-163: skipped blocks
            tire_slip_angle_fl=_f(data, 164),
            tire_slip_angle_fr=_f(data, 168),
            tire_slip_angle_rl=_f(data, 172),
            tire_slip_angle_rr=_f(data, 176),
            # 180-211: skipped blocks
            car_ordinal=_i(data, 212),
            car_class=_i(data, 216),
            car_pi=_i(data, 220),
            drivetrain_type=_i(data, 224),
            num_cylinders=_i(data, 228),
            # 232-243: 12 unknown bytes skipped
            position_x=_f(data, 244),
            position_y=_f(data, 248),
            position_z=_f(data, 252),
            speed_ms=_f(data, 256),
            power=_f(data, 260),
            torque=_f(data, 264),
            tire_temp_fl=_f_to_c(_f(data, 268)),
            tire_temp_fr=_f_to_c(_f(data, 272)),
            tire_temp_rl=_f_to_c(_f(data, 276)),
            tire_temp_rr=_f_to_c(_f(data, 280)),
            boost=_f(data, 284),
            fuel=_f(data, 288),
            distance_traveled=_f(data, 292),
            best_lap=_f(data, 296),
            last_lap=_f(data, 300),
            current_lap=_f(data, 304),
            current_race_time=_f(data, 308),
            lap_number=_h(data, 312),
            race_position=_b(data, 314),
            throttle=_b(data, 315),
            brake=_b(data, 316),
            clutch=_b(data, 317),
            handbrake=_b(data, 318),
            gear=_b(data, 319),
            steer=_sb(data, 320),
            # 321-322: driving_line, ai_brake_diff — skipped
        )

        if len(data) >= 339:
            pkt.tire_wear_fl = _f(data, 323)
            pkt.tire_wear_fr = _f(data, 327)
            pkt.tire_wear_rl = _f(data, 331)
            pkt.tire_wear_rr = _f(data, 335)

        return pkt

    except struct.error as exc:
        log.warning("Failed to parse packet (%d bytes): %s", len(data), exc)
        return None


# ---------------------------------------------------------------------------
# Player state
# ---------------------------------------------------------------------------


@dataclass
class PlayerState:
    player_id: str  # source IP string
    label: str
    color: str
    active: bool = True
    last_seen: float = field(default_factory=time.monotonic)
    packet: Packet | None = None
    trace: deque[list[float]] = field(
        default_factory=lambda: deque(maxlen=TRACE_MAXLEN)
    )

    def update(self, pkt: Packet) -> None:
        self.last_seen = time.monotonic()
        self.active = True
        self.packet = pkt
        self.trace.append([pkt.position_x, pkt.position_z])

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "id": self.player_id,
            "label": self.label,
            "color": self.color,
            "active": self.active,
            "packet": self.packet.to_camel_dict() if self.packet else None,
        }

    def to_update_dict(self) -> dict[str, Any]:
        return {
            "type": "update",
            "player_id": self.player_id,
            "packet": self.packet.to_camel_dict() if self.packet else None,
            "trace": list(self.trace),
        }


# ---------------------------------------------------------------------------
# Hostname resolution
# ---------------------------------------------------------------------------


async def _resolve_hostname(ip: str) -> str:
    """Return the reverse-DNS hostname for *ip*, falling back to the IP itself."""
    loop = asyncio.get_event_loop()
    try:
        hostname, _aliases, _addrs = await loop.run_in_executor(
            None, socket.gethostbyaddr, ip
        )
        # Prefer a real name; if the PTR record just echoes the address, keep IP.
        return hostname if hostname != ip else ip
    except (socket.herror, socket.gaierror, OSError):
        return ip


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------


class AppState:
    def __init__(self) -> None:
        self.players: dict[str, PlayerState] = {}
        self._color_index: int = 0
        self.ws_clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    def _next_color(self) -> str:
        color = PLAYER_COLORS[self._color_index % len(PLAYER_COLORS)]
        self._color_index += 1
        return color

    async def get_or_create_player(self, ip: str) -> tuple[PlayerState, bool]:
        """Return (player, is_new). Must be called from async context."""
        async with self._lock:
            if ip in self.players:
                return self.players[ip], False
            label = await _resolve_hostname(ip)
            player = PlayerState(
                player_id=ip,
                label=label,
                color=self._next_color(),
            )
            self.players[ip] = player
            log.info("New player: %s (%s)", player.label, ip)
            return player, True

    async def remove_player(self, ip: str) -> PlayerState | None:
        async with self._lock:
            return self.players.pop(ip, None)

    def state_message(self) -> str:
        payload = {
            "type": "state",
            "players": {ip: p.to_state_dict() for ip, p in self.players.items()},
        }
        return json.dumps(payload)

    async def broadcast(self, message: str) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.ws_clients):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)

    async def broadcast_json(self, obj: Any) -> None:
        await self.broadcast(json.dumps(obj))


# ---------------------------------------------------------------------------
# UDP protocol
# ---------------------------------------------------------------------------


class TelemetryProtocol(asyncio.DatagramProtocol):
    def __init__(self, app_state: AppState, loop: asyncio.AbstractEventLoop) -> None:
        self._state = app_state
        self._loop = loop

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        log.info("UDP telemetry listener ready on port %d", UDP_PORT)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        ip = addr[0]
        pkt = parse_packet(data)
        if pkt is None:
            return
        # Schedule coroutine on the running event loop from this sync callback.
        asyncio.run_coroutine_threadsafe(self._handle_packet(ip, pkt), self._loop)

    def error_received(self, exc: Exception) -> None:
        log.warning("UDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        log.info("UDP connection lost: %s", exc)

    async def _handle_packet(self, ip: str, pkt: Packet) -> None:
        player, is_new = await self._state.get_or_create_player(ip)
        player.update(pkt)

        if is_new:
            # Broadcast full state so all clients learn about the new player.
            await self._state.broadcast(self._state.state_message())
        else:
            await self._state.broadcast_json(player.to_update_dict())


# ---------------------------------------------------------------------------
# Player timeout checker
# ---------------------------------------------------------------------------


async def player_timeout_loop(app_state: AppState) -> None:
    """Periodically evict players that have gone silent for PLAYER_TIMEOUT_S."""
    while True:
        await asyncio.sleep(5.0)
        now = time.monotonic()
        timed_out: list[str] = [
            ip
            for ip, p in list(app_state.players.items())
            if (now - p.last_seen) > PLAYER_TIMEOUT_S
        ]
        for ip in timed_out:
            player = await app_state.remove_player(ip)
            if player:
                log.info("Player timed out: %s (%s)", player.label, ip)
                await app_state.broadcast_json({"type": "player_left", "player_id": ip})


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

_app_state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: TelemetryProtocol(_app_state, loop),
        local_addr=("0.0.0.0", UDP_PORT),
    )
    log.info("UDP server listening on 0.0.0.0:%d", UDP_PORT)
    task = asyncio.create_task(player_timeout_loop(_app_state), name="player-timeout")
    try:
        yield
    finally:
        task.cancel()
        transport.close()


app = FastAPI(title="FH6 Telemetry Server", lifespan=lifespan)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _app_state.ws_clients.add(ws)
    log.info("WebSocket client connected: %s", ws.client)

    # Send current full state immediately on connect.
    try:
        await ws.send_text(_app_state.state_message())
    except Exception as exc:
        log.warning("Failed to send initial state to new WS client: %s", exc)
        _app_state.ws_clients.discard(ws)
        return

    try:
        # Keep the connection alive; we only send from the broadcast path.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        log.info("WebSocket client disconnected: %s", ws.client)
    except Exception as exc:
        log.warning("WebSocket error (%s): %s", ws.client, exc)
    finally:
        _app_state.ws_clients.discard(ws)


# Serve static files (frontend) at /.
# This must come after the /ws route so FastAPI matches WS first.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
