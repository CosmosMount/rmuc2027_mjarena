"""Inspect a bundled or external arena and optionally attach an MJCF model."""

from __future__ import annotations

import argparse
from pathlib import Path
import time

from .scene import Attachment, Simulation, load_scene


ARENAS = {
  "rmuc2027": Path(__file__).parent / "arena_mjcf" / "model.xml",
}


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--arena", default="rmuc2027", help="Bundled name (rmuc2027) or MJCF path")
  parser.add_argument("--model", type=Path, help="Optional external MJCF to attach")
  parser.add_argument("--prefix", default="object/", help="Prefix for attached model names")
  parser.add_argument("--position", type=float, nargs=3, default=(0, 0, 0), metavar=("X", "Y", "Z"))
  parser.add_argument("--headless", action="store_true", help="Run without a viewer")
  parser.add_argument("--steps", type=int, help="Number of simulation steps (headless default: 1000)")
  parser.add_argument("--host", default="127.0.0.1")
  parser.add_argument("--port", type=int, default=8080)
  parser.add_argument("--frame-rate-hz", type=float, default=30)
  parser.add_argument("--fpv-camera", help="Name of an MJCF camera for the browser FPV view")
  parser.add_argument("--first-person", action="store_true", help="Start in the FPV view (requires --fpv-camera)")
  args = parser.parse_args()
  if args.steps is not None and args.steps <= 0:
    parser.error("--steps must be positive")
  if not 0 <= args.port <= 65535:
    parser.error("--port must be between 0 and 65535")
  if args.first_person and not args.fpv_camera:
    parser.error("--first-person requires --fpv-camera")
  arena = ARENAS.get(args.arena, Path(args.arena))
  attachments = [Attachment(args.model, args.prefix, tuple(args.position))] if args.model else []
  sim = Simulation(load_scene(arena, attachments))
  if args.headless:
    for _ in range(args.steps or 1000):
      sim.step()
    print(f"steps={args.steps or 1000} time={sim.data.time:.6f}s")
    return
  from .input import WebServer
  from .viewer import BrowserViewer

  with BrowserViewer(sim.model, port=0, frame_rate_hz=args.frame_rate_hz,
                     fpv_camera=args.fpv_camera, first_person=args.first_person) as viewer:
    keys = {"KeyP", "KeyR"}
    if args.fpv_camera:
      keys.add("Tab")
    web = WebServer(args.host, args.port, viewer.server.get_port(), keys=frozenset(keys))
    try:
      print(f"Viewer: http://{args.host}:{web.port} (P pause, R reset" +
            (", Tab FPV" if args.fpv_camera else "") + ")", flush=True)
      paused = False
      steps = 0
      deadline = time.monotonic()
      while args.steps is None or steps < args.steps:
        web.set_first_person(viewer.first_person_enabled, viewer.fpv_fovy_rad)
        state = web.poll()
        if "KeyP" in state.pressed:
          paused = not paused
        if "KeyR" in state.pressed:
          sim.reset()
        if "Tab" in state.pressed:
          viewer.toggle_first_person()
        if not paused:
          sim.step()
          steps += 1
        viewer.draw(sim.data)
        deadline += sim.model.opt.timestep if not paused else 0.01
        delay = deadline - time.monotonic()
        if delay > 0:
          time.sleep(delay)
        else:
          deadline = time.monotonic()
    except KeyboardInterrupt:
      pass
    finally:
      web.close()


if __name__ == "__main__":
  main()
