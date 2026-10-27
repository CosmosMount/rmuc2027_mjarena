"""Render an RGB image from a camera defined in a MuJoCo model."""

from __future__ import annotations

from numbers import Integral

import mujoco
import numpy as np


class FPVCamera:
  """Render frames from an existing model camera.

  ``camera`` is a camera name (including any attachment prefix) or a nonnegative
  MuJoCo camera ID. The camera follows its parent body according to the MJCF.
  The requested dimensions must fit the model's offscreen framebuffer.

  Create, use, and close this object on the same thread. The caller must ensure
  that ``data`` belongs to this model and is not modified concurrently. After
  stepping or directly changing state, call ``mujoco.mj_forward(model, data)``
  if derived poses need to be updated before rendering. No stream or scheduling
  is created; the caller chooses when to render and how to consume the frames.
  """

  def __init__(
    self,
    model: mujoco.MjModel,
    camera: str | int,
    width: int = 640,
    height: int = 480,
    max_geom: int = 10000,
  ) -> None:
    for name, value in (("width", width), ("height", height), ("max_geom", max_geom)):
      if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
        raise ValueError(f"{name} must be a positive integer")

    if isinstance(camera, str):
      camera_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, camera)
      if camera_id < 0:
        raise ValueError(f"Camera {camera!r} does not exist in the model")
    elif isinstance(camera, Integral) and not isinstance(camera, bool):
      camera_id = int(camera)
      if not 0 <= camera_id < model.ncam:
        raise ValueError(f"Camera ID {camera_id} is outside [0, {model.ncam})")
    else:
      raise ValueError("camera must be a camera name or nonnegative ID")

    self._camera_id = camera_id
    self._renderer = mujoco.Renderer(
      model, width=int(width), height=int(height), max_geom=int(max_geom)
    )
    self._closed = False

  def render(self, data: mujoco.MjData) -> np.ndarray:
    """Return a new ``(height, width, 3)`` RGB uint8 image for ``data``."""
    if self._closed:
      raise RuntimeError("Cannot render with a closed FPVCamera")
    self._renderer.update_scene(data, camera=self._camera_id)
    return self._renderer.render()

  def close(self) -> None:
    """Release the rendering context; repeated calls are safe."""
    if not self._closed:
      self._renderer.close()
      self._closed = True

  def __enter__(self) -> FPVCamera:
    return self

  def __exit__(self, exc_type, exc_value, traceback) -> None:
    self.close()
