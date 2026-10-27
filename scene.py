"""Compose an external MuJoCo arena with optional external MJCF models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence

import mujoco


@dataclass(frozen=True)
class Attachment:
  """A model attached to the arena, with a frame in world coordinates.

  Quaternion order is w, x, y, z. Prefixes distinguish names when attaching
  multiple models. The MJCF files and their relative resources must exist.
  """

  path: str | Path
  prefix: str = ""
  position: tuple[float, float, float] = (0.0, 0.0, 0.0)
  quaternion: tuple[float, float, float, float] = (1.0, 0.0, 0.0, 0.0)


def load_scene(arena: str | Path, attachments: Sequence[Attachment] = ()) -> mujoco.MjModel:
  """Compile an arena with zero or more MJCF attachments.

  Paths resolve relative to the current working directory, while MJCF includes
  and assets resolve relative to their respective source files. The arena owns
  global simulation options; attached model options cannot be independent in a
  single MuJoCo model. No collision masks, actuators, or solver options are
  rewritten. MuJoCo reports incompatible models or duplicate names at compile.
  """
  spec = mujoco.MjSpec.from_file(str(Path(arena).resolve()))
  for attachment in attachments:
    child = mujoco.MjSpec.from_file(str(Path(attachment.path).resolve()))
    frame = spec.worldbody.add_frame()
    frame.pos = attachment.position
    frame.quat = attachment.quaternion
    spec.attach(child, prefix=attachment.prefix, frame=frame)
  return spec.compile()


class Simulation:
  """Own a MuJoCo data instance; callers own control and simulation scheduling."""

  def __init__(self, model: mujoco.MjModel) -> None:
    self.model = model
    self.data = mujoco.MjData(model)
    mujoco.mj_forward(self.model, self.data)

  def reset(self, keyframe: str | int | None = None) -> None:
    """Reset all state, optionally to a named or indexed model keyframe."""
    if keyframe is None:
      mujoco.mj_resetData(self.model, self.data)
    else:
      key_id = self.model.key(keyframe).id if isinstance(keyframe, str) else keyframe
      if not 0 <= key_id < self.model.nkey:
        raise ValueError(f"Invalid keyframe index: {key_id}")
      mujoco.mj_resetDataKeyframe(self.model, self.data, key_id)
    mujoco.mj_forward(self.model, self.data)

  def step(self, steps: int = 1) -> None:
    """Advance physics using the current data.ctrl; no controller is implied."""
    if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
      raise ValueError("steps must be a positive integer")
    mujoco.mj_step(self.model, self.data, nstep=steps)
