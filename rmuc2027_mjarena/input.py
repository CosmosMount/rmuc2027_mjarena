"""Browser keyboard and pointer input shared with an unmodified Viser client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import lru_cache
import gzip
from itertools import count
import json
import math
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Lock, Thread
import time
from urllib.parse import urlsplit

import viser
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

DEFAULT_KEYS = frozenset(
  [f"Key{chr(code)}" for code in range(ord("A"), ord("Z") + 1)]
  + [f"Digit{digit}" for digit in range(10)]
  + ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Space", "Enter",
     "ShiftLeft", "ShiftRight"]
)


@dataclass(frozen=True)
class InputState:
  """State since the previous poll.

  ``pressed`` and ``released`` preserve transitions occurring between polls.
  Keyboard codes are DOM ``KeyboardEvent.code`` values, not characters.
  ``mouse_delta`` is raw browser pixels. In first-person mode ``aim_delta``
  gives the corresponding yaw/pitch increments in radians, scaled by the
  vertical field of view and viewport height, with mouse right/down negative.
  """

  keys: frozenset[str] = frozenset()
  pressed: frozenset[str] = frozenset()
  released: frozenset[str] = frozenset()
  mouse_delta: tuple[float, float] = (0.0, 0.0)
  aim_delta: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True)
class _Event:
  client: int
  keys: frozenset[str] | None
  received_s: float
  mouse_delta: tuple[float, float] = (0.0, 0.0)
  aim_delta: tuple[float, float] = (0.0, 0.0)
  mode_generation: int = 0


@dataclass
class _HeldInput:
  """A held keyboard has one owner; rejected or expired owners must release."""

  timeout_s: float
  owner: int | None = None
  keys: frozenset[str] = frozenset()
  deadline_s: float = 0.0
  blocked: set[int] = field(default_factory=set)

  def release(self, block: bool = False) -> None:
    if self.owner is not None and block:
      self.blocked.add(self.owner)
    self.owner = None
    self.keys = frozenset()

  def expire(self, now_s: float) -> None:
    if self.owner is not None and now_s >= self.deadline_s:
      self.release(block=True)

  def receive(self, event: _Event, now_s: float) -> bool:
    """Return whether mouse movement from this message may be consumed."""
    if event.keys is None or not event.keys:
      self.blocked.discard(event.client)
      if self.owner == event.client:
        self.release()
      return event.keys is not None and self.owner is None and event.received_s + self.timeout_s > now_s
    if event.received_s + self.timeout_s <= now_s:
      self.blocked.add(event.client)
      return False
    if event.client in self.blocked:
      return False
    if self.owner is not None and self.owner != event.client:
      self.blocked.add(event.client)
      return False
    self.owner = event.client
    self.keys = event.keys
    self.deadline_s = event.received_s + self.timeout_s
    return True


class WebServer:
  """Serve Viser and generic browser input at one address.

  ``viser_port`` identifies the local Viser server. Poll on one simulation
  thread to consume events; other clients cannot override an active keyboard.
  Restrict ``keys`` to codes owned by the application to leave remaining
  browser/Viser shortcuts intact. Click 'Capture mouse' for pointer deltas;
  Escape releases it. Close this server before stopping Viser.
  """

  def __init__(
    self, host: str, port: int, viser_port: int,
    keys: frozenset[str] = DEFAULT_KEYS, timeout_s: float = 0.5,
  ) -> None:
    if not math.isfinite(timeout_s) or timeout_s < 0.1:
      raise ValueError("Input timeout must be finite and at least 0.1 s")
    if any(not isinstance(key, str) or not key or len(key) > 64 for key in keys) or len(keys) > 128:
      raise ValueError("Keys must contain at most 128 nonempty DOM codes of at most 64 characters")
    self._keys = frozenset(keys)
    self._held = _HeldInput(timeout_s)
    self._events: SimpleQueue[_Event] = SimpleQueue()
    self._mode_lock = Lock()
    self._mode = (False, None)
    self._mode_generation = 0
    self._input_clients: set = set()  # accessed only on the event-loop thread
    self._viser_url = f"ws://127.0.0.1:{viser_port}"
    self._clients = count()
    self._config = json.dumps({"keys": sorted(keys), "heartbeat_ms": timeout_s * 250}).encode()
    self._server = None
    self._loop = asyncio.new_event_loop()
    self._thread = Thread(target=self._loop.run_forever, name="arena-input", daemon=True)
    self._thread.start()
    startup = asyncio.run_coroutine_threadsafe(self._start(host, port), self._loop)
    try:
      startup.result(timeout=10)
    except Exception:
      startup.cancel()
      self.close()
      raise

  async def _start(self, host: str, port: int) -> None:
    self._server = await serve(
      self._connection, host, port, process_request=self._http,
      select_subprotocol=lambda _, offered: next((p for p in offered if p.startswith("viser-v")), None),
      compression=None, max_size=50 * 1024 * 1024, close_timeout=1,
    )

  @property
  def port(self) -> int:
    """Actual listening port, useful when initialized with port 0."""
    return self._server.sockets[0].getsockname()[1]

  @lru_cache(maxsize=8)
  def _content(self, path: str, compressed: bool) -> tuple[bytes, str]:
    frontend = Path(__file__).with_name("web")
    if path == "/":
      body, content_type = (frontend / "index.html").read_bytes(), "text/html"
    elif path == "/input.js":
      body, content_type = (frontend / "input.js").read_bytes(), "text/javascript"
    elif path == "/input-config":
      body, content_type = self._config, "application/json"
    else:
      body = (Path(viser.__file__).parent / "client/build/index.html").read_bytes()
      content_type = "text/html"
    return (gzip.compress(body) if compressed else body), content_type

  def _http(self, connection, request) -> Response | None:
    if request.headers.get("Upgrade", "").lower() == "websocket":
      return None
    path = urlsplit(request.path).path.rstrip("/") or "/"
    if path not in ("/", "/viewer", "/input.js", "/input-config"):
      return connection.respond(404, "Not found\n")
    compressed = "gzip" in request.headers.get("Accept-Encoding", "")
    body, content_type = self._content(path, compressed)
    headers = Headers({"Content-Type": content_type, "Content-Length": str(len(body)), "Cache-Control": "no-store"})
    if compressed:
      headers["Content-Encoding"] = "gzip"
    return Response(200, "OK", headers, body)

  async def _connection(self, socket) -> None:
    path = urlsplit(socket.request.path).path.rstrip("/")
    try:
      if path == "/input":
        await self._input(socket)
      elif path == "/viewer":
        await self._viewer(socket)
      else:
        await socket.close(1008, "Unknown WebSocket endpoint")
    except ConnectionClosed:
      pass

  async def _input(self, socket) -> None:
    client = next(self._clients)
    self._input_clients.add(socket)
    try:
      with self._mode_lock:
        enabled, fovy = self._mode
      await socket.send(json.dumps({"first_person": enabled, "fovy_rad": fovy}))
      async for payload in socket:
        try:
          if not isinstance(payload, str) or len(payload) > 8192:
            raise ValueError("Expected a small JSON input message")
          message = json.loads(payload)
          keys = message["keys"]
          delta = message.get("mouse_delta", [0.0, 0.0])
          aim = message.get("aim_delta", [0.0, 0.0])
          if not isinstance(keys, list) or len(keys) > len(self._keys) or any(
            not isinstance(key, str) or key not in self._keys for key in keys
          ):
            raise ValueError("Invalid keys")
          if not isinstance(delta, list) or len(delta) != 2 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 10000 for v in delta
          ):
            raise ValueError("Invalid mouse displacement")
          if not isinstance(aim, list) or len(aim) != 2 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > math.pi for v in aim
          ):
            raise ValueError("Invalid aim displacement")
        except (KeyError, TypeError, ValueError):
          await socket.close(1008, "Invalid input state")
          return
        with self._mode_lock:
          first_person, _ = self._mode
          generation = self._mode_generation
        self._events.put(_Event(client, frozenset(keys), time.monotonic(), tuple(delta), tuple(aim) if first_person else (0.0, 0.0), generation))
    finally:
      self._input_clients.discard(socket)
      self._events.put(_Event(client, None, time.monotonic()))

  def set_first_person(self, enabled: bool, fovy_rad: float | None = None) -> None:
    """Sync first-person mouse capture and vertical FOV with browser clients.

    Call with the viewer's mode and FOV when either changes. In first-person
    mode a click on the canvas captures the pointer; Escape releases it. The
    input API reports raw pixel and FOV-scaled angular deltas independently.
    """
    if not isinstance(enabled, bool):
      raise ValueError("enabled must be a bool")
    if enabled and (isinstance(fovy_rad, bool) or not isinstance(fovy_rad, (int, float))
                    or not math.isfinite(fovy_rad) or not 0 < fovy_rad < math.pi):
      raise ValueError("First-person vertical FOV must be between 0 and pi radians")
    mode = (enabled, float(fovy_rad) if enabled else None)
    with self._mode_lock:
      if self._mode == mode:
        return
      self._mode = mode
      self._mode_generation += 1
    def broadcast() -> None:
      async def send_mode() -> None:
        payload = json.dumps({"first_person": mode[0], "fovy_rad": mode[1]})
        await asyncio.gather(*(socket.send(payload) for socket in tuple(self._input_clients)), return_exceptions=True)
      asyncio.create_task(send_mode())
    self._loop.call_soon_threadsafe(broadcast)

  async def _viewer(self, socket) -> None:
    protocols = [socket.subprotocol] if socket.subprotocol else None
    async with connect(self._viser_url, subprotocols=protocols, proxy=None, compression=None, max_size=None, close_timeout=1) as upstream:
      async def relay(source, target):
        async for payload in source:
          await target.send(payload)

      tasks = [asyncio.create_task(relay(socket, upstream)), asyncio.create_task(relay(upstream, socket))]
      try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
          task.result()
      finally:
        for task in tasks:
          task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

  def poll(self) -> InputState:
    """Consume pending input and expire stale keyboard owners."""
    now = time.monotonic()
    with self._mode_lock:
      first_person, _ = self._mode
      generation = self._mode_generation
    pressed: set[str] = set()
    released: set[str] = set()
    dx = dy = yaw = pitch = 0.0
    while True:
      try:
        event = self._events.get_nowait()
      except Empty:
        break
      before = self._held.keys
      self._held.expire(event.received_s)
      released.update(before - self._held.keys)
      before = self._held.keys
      accepted = self._held.receive(event, now)
      after = self._held.keys
      pressed.update(after - before)
      released.update(before - after)
      if accepted:
        dx += event.mouse_delta[0]
        dy += event.mouse_delta[1]
        if first_person and event.mode_generation == generation:
          yaw += event.aim_delta[0]
          pitch += event.aim_delta[1]
    before = self._held.keys
    self._held.expire(now)
    released.update(before - self._held.keys)
    return InputState(self._held.keys, frozenset(pressed), frozenset(released), (dx, dy), (yaw, pitch))

  def close(self) -> None:
    if self._loop.is_closed():
      return

    async def shutdown():
      if self._server is not None:
        self._server.close()
        await self._server.wait_closed()

    try:
      asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(timeout=10)
    finally:
      self._loop.call_soon_threadsafe(self._loop.stop)
      self._thread.join(timeout=10)
      self._loop.close()
      self._content.cache_clear()
