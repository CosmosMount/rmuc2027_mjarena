"""Browser visualization for an existing MuJoCo model and data."""

from __future__ import annotations

import math
from numbers import Integral
from threading import RLock
import time

import mujoco
from mjviser import ViserMujocoScene
import numpy as np
import viser
from viser import _messages
import viser.transforms as vtf


class BrowserViewer:
  """Visualize one model using a private data snapshot.

  Call ``draw`` from the thread that owns the supplied data, or otherwise
  synchronize access to it. The model must remain unchanged while the viewer
  is open. GUI callbacks only read the viewer's private snapshot.
  """

  def __init__(
    self,
    model: mujoco.MjModel,
    host: str = "127.0.0.1",
    port: int = 8080,
    frame_rate_hz: float = 30.0,
    fpv_camera: str | int | None = None,
    first_person: bool = False,
  ) -> None:
    if not math.isfinite(frame_rate_hz) or frame_rate_hz <= 0:
      raise ValueError("Frame rate must be finite and positive")
    if first_person and fpv_camera is None:
      raise ValueError("first_person requires an FPV camera")
    camera_id = self._resolve_camera(model, fpv_camera) if fpv_camera is not None else None
    self._frame_period_s = 1.0 / frame_rate_hz
    self._next_frame_s = 0.0
    self._render_lock = RLock()
    self._closed = False
    self._model = model
    self._fpv_camera = camera_id
    self._fpv_active = False
    self._tracking = False
    self._fpv_groups = {}
    self._data = mujoco.MjData(model)
    mujoco.mj_forward(model, self._data)
    self.server = viser.ViserServer(host=host, port=port, label="MuJoCo", verbose=False)
    try:
      self.scene = ViserMujocoScene(self.server, model, num_envs=1)
      self.scene.set_refresh_handler(self._refresh)
      self.first_person = self.server.gui.add_checkbox(
        "First person", initial_value=first_person, disabled=camera_id is None,
        hint="View from the selected model camera.",
      )
      self.first_person.on_update(lambda _: self._apply_first_person())
      self.scene.create_visualization_gui()
      self._apply_first_person()
      self._refresh()
    except Exception:
      self.server.stop()
      raise

  @staticmethod
  def _resolve_camera(model: mujoco.MjModel, camera: str | int) -> int:
    if isinstance(camera, str):
      camera_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, camera)
    elif isinstance(camera, Integral) and not isinstance(camera, bool):
      camera_id = int(camera)
    else:
      raise ValueError("camera must be a camera name or nonnegative ID")
    if not 0 <= camera_id < model.ncam:
      raise ValueError(f"Camera {camera!r} does not exist in the model")
    return camera_id

  def set_fpv_camera(self, camera: str | int) -> None:
    """Select an existing model camera by name or ID, including any prefix."""
    camera_id = self._resolve_camera(self._model, camera)
    with self._render_lock:
      if self._closed:
        raise RuntimeError("Viewer is closed")
      with self.server.atomic():
        # Undo the previous camera body's mesh-frame workaround before selecting
        # another body. The next scene update restores world-space batches.
        for handle in self._fpv_groups.values():
          handle.position = (0.0, 0.0, 0.0)
          handle.wxyz = (1.0, 0.0, 0.0, 0.0)
        self._fpv_groups.clear()
        self._fpv_camera = camera_id
        self.first_person.disabled = False
        self._update_scene()

  def _apply_first_person(self) -> None:
    with self._render_lock:
      if self._closed:
        return
      enabled = bool(self.first_person.value)
      if enabled and self._fpv_camera is None:
        raise ValueError("First person requires an FPV camera")
      if enabled != self._fpv_active:
        if enabled:
          self._tracking = self.scene.camera_tracking_enabled
          self.scene.camera_tracking_enabled = False
        else:
          self.scene.camera_tracking_enabled = self._tracking
        self._fpv_active = enabled
      with self.server.atomic():
        self._update_scene()

  def set_first_person(self, enabled: bool) -> None:
    """Enable or disable the selected camera's first-person browser view."""
    with self._render_lock:
      if self._closed:
        raise RuntimeError("Viewer is closed")
      if enabled and self._fpv_camera is None:
        raise ValueError("First person requires an FPV camera")
      self.first_person.value = bool(enabled)
      self._apply_first_person()

  def toggle_first_person(self) -> None:
    """Switch between the model camera and the interactive browser view."""
    with self._render_lock:
      self.set_first_person(not self._fpv_active)

  @property
  def first_person_enabled(self) -> bool:
    with self._render_lock:
      return self._fpv_active

  @property
  def fpv_fovy_rad(self) -> float | None:
    with self._render_lock:
      if self._fpv_camera is None:
        return None
      return float(np.radians(self._model.cam_fovy[self._fpv_camera]))

  def _update_scene(self) -> None:
    if self._fpv_active:
      self.scene.camera_tracking_enabled = False
    self.scene.update_from_mjdata(self._data)
    if self._fpv_camera is None:
      return
    camera_id = self._fpv_camera
    body_id = int(self._model.cam_bodyid[camera_id])
    # Keep the camera body's batched geometry in its local frame. Viser applies
    # batch updates and camera poses in different render phases otherwise.
    position = self._data.xpos[body_id] + self.scene._scene_offset
    rotation = vtf.SO3.from_matrix(self._data.xmat[body_id].reshape(3, 3))
    inverse = rotation.inverse()
    for group in self.scene._mesh_groups:
      if body_id not in group.body_ids or not group.handle.visible:
        continue
      handle = group.handle
      handle.batched_positions = inverse @ (handle.batched_positions - position)
      handle.batched_wxyzs = (inverse @ vtf.SO3(handle.batched_wxyzs)).wxyz
      handle.position = position
      handle.wxyz = rotation.wxyz
      self._fpv_groups[id(handle)] = handle
    if self._fpv_active:
      camera_pos = self._data.cam_xpos[camera_id].copy()
      axes = self._data.cam_xmat[camera_id].reshape(3, 3)
      for message in (
        _messages.SetCameraFovMessage(float(np.radians(self._model.cam_fovy[camera_id]))),
        _messages.SetCameraUpDirectionMessage(tuple(axes[:, 1])),
        _messages.SetCameraPositionMessage(tuple(camera_pos)),
        _messages.SetCameraLookAtMessage(tuple(camera_pos - axes[:, 2])),
      ):
        self.server._websock_server.queue_message(message)

  def _refresh(self) -> None:
    with self._render_lock:
      if not self._closed:
        with self.server.atomic():
          self._update_scene()

  def draw(self, data: mujoco.MjData) -> None:
    """Publish a snapshot if a client is connected and a frame is due."""
    if not self.server.get_clients():
      return
    now = time.monotonic()
    with self._render_lock:
      if self._closed or now < self._next_frame_s:
        return
      if now - self._next_frame_s >= self._frame_period_s:
        self._next_frame_s = now + self._frame_period_s
      else:
        self._next_frame_s += self._frame_period_s
      with self.server.atomic():
        mujoco.mj_copyData(self._data, self._model, data)
        self._update_scene()

  def close(self) -> None:
    """Stop the server; repeated calls are safe."""
    with self._render_lock:
      if self._closed:
        return
      self._closed = True
    self.server.stop()

  def __enter__(self) -> BrowserViewer:
    return self

  def __exit__(self, exc_type, exc_value, traceback) -> None:
    self.close()
