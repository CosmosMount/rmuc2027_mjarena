# Arena Sim

将 MuJoCo 场地、外部模型和浏览器显示组合起来的 Python 库。场景和输入不绑定机器人、策略、训练框架或传感器：使用者自己提供 MJCF、控制器和控制映射。软件包包含 RMUC2027 场地，不包含本项目的机器人模型或 policy。

## 安装和运行

Python 3.10+，MuJoCo 3.15+。在本目录执行：

```bash
python -m pip install -e '.[viewer]'
arena-sim --arena rmuc2027
# 包含外部 MJCF 的示例；位置是附加 frame 的世界位置，单位 m
arena-sim --arena rmuc2027 --model /path/to/model.xml --position 0 0 1
# 机器人模型中需有名为 fpv 的相机；默认附加前缀为 object/
arena-sim --model /path/to/model.xml --position 0 0 1 --fpv-camera object/fpv --first-person
# 只执行物理步，不启动浏览器或限制实时速度
arena-sim --arena rmuc2027 --headless --steps 1000
```

打开终端显示的地址（默认 `http://127.0.0.1:8080`）；远程使用时转发该端口。`--host 0.0.0.0` 会让网络上的其他客户端也能访问：本服务没有认证，只应在受信任网络中使用。P 暂停/继续、R 重置。指定 `--fpv-camera` 后可用 Tab 或 First person 复选框切换视角，`--first-person` 从 FPV 开始。FPV 模式下点击画布捕获鼠标，Esc 释放；非 FPV 模式可点击 Capture mouse 后点击画布捕获。命令行示例不提供机器人动作控制。

`--arena` 可为 `rmuc2027`，也可以是外部 MJCF 文件路径。场地和附加模型的相对资源相对于各自的 MJCF 文件解析。场地提供物理选项；库不会替外部模型改写碰撞过滤、执行器、传感器、时间步或求解器。接入方应检查场地和模型的接触掩码、关节、姿态、时间步及控制周期是否兼容。复杂场地可能低于实时速度，降低显示帧率不会解决物理计算瓶颈。

## Python 接入

```python
from pathlib import Path
from arena_sim import Attachment, Simulation, load_scene
from arena_sim.input import WebServer
from arena_sim.viewer import BrowserViewer

arena = Path("/path/to/arena/model.xml")
model = load_scene(arena, [Attachment(Path("/path/to/robot.xml"), "robot/", (0, 0, 1))])
sim = Simulation(model)

# 在同一个线程读取输入、写入 sim.data.ctrl、step 并 draw。
with BrowserViewer(model, port=0, fpv_camera="robot/fpv") as viewer:
  web = WebServer("127.0.0.1", 8080, viewer.server.get_port(), keys=frozenset({"KeyW", "KeyS"}))
  try:
    while True:
      web.set_first_person(viewer.first_person_enabled, viewer.fpv_fovy_rad)
      state = web.poll()  # keys、pressed、released：DOM KeyboardEvent.code 集合
      # 根据 state 和 sim.data 的传感器/关节状态，由接入方计算 sim.data.ctrl。
      # state.mouse_delta 为自上次 poll 累计的原始像素位移 (dx, dy)。
      # state.aim_delta 为 FPV 下按视场和画面高度换算的 (yaw, pitch) 弧度增量。
      sim.step()
      viewer.draw(sim.data)
  except KeyboardInterrupt:
    pass
  finally:
    web.close()
```

示例中的机器人模型须定义 `fpv` 相机。`viewer.set_first_person(True/False)` 和 `viewer.toggle_first_person()` 可通过代码切换视角，`viewer.set_fpv_camera(name_or_id)` 可选择其他相机；每次循环同步查看器状态到输入服务器。`aim_delta` 两轴均按 `-鼠标位移 × 垂直视场弧度 / 画面高度` 计算，如何应用到云台由调用方决定。

示例省略实时限速；`sim.reset()` 重置 MuJoCo 数据，调用方也要重置自己的控制器状态。`BrowserViewer.draw()` 应在仿真数据所属线程调用或由调用方同步；显示层复制独立快照。`WebServer.poll()` 也应由单一线程调用。输入服务器对按住的键使用独占所有权，断线或超时会释放；`pressed` 和 `released` 保留两次 poll 之间的转换。按键集合只应包含应用需要拦截的 DOM code，其他快捷键仍由浏览器/Viser 处理。

## FPV 相机

在机器人 MJCF 的相应 body 内定义相机，例如：

```xml
<camera name="fpv" pos="0.2 0 0.1" xyaxes="0 -1 0 0 0 1" fovy="60"/>
```

这个例子的相机朝机器人局部 +X 方向，位置、朝向和视场角应按模型调整。通过 `Attachment` 加载机器人时，相机名称也会带上指定的前缀。下面从该相机读取图像：

```python
import mujoco
from arena_sim import Attachment, FPVCamera, Simulation, load_scene

model = load_scene("/path/to/arena.xml", [
  Attachment("/path/to/robot.xml", prefix="robot/", position=(0, 0, 1)),
])
sim = Simulation(model)
with FPVCamera(model, "robot/fpv", width=640, height=480) as camera:
  sim.step()
  mujoco.mj_forward(model, sim.data)  # 更新当前位置对应的相机姿态
  rgb = camera.render(sim.data)       # uint8 数组，形状为 (480, 640, 3)
  # 将 rgb 交给自己的控制器、图像处理或视频传输代码。
```

也可以用非负相机 ID 代替名称；相机必须存在于编译后的模型中。分辨率不能超过模型的离屏缓冲区尺寸（由 MJCF 的 `visual/global` 设置）；大型场景可按需增大 `max_geom` 参数。渲染器需要可用的 OpenGL 环境，无显示器运行时需在启动 Python 前根据环境配置 MuJoCo 的离屏渲染后端，例如 `MUJOCO_GL=egl`。相机的创建、渲染和关闭应在同一线程执行；读取 `sim.data` 时须与仿真写入同步。调用方自行决定采样频率及图像传输方式，此图像接口与浏览器视角独立。

场地的几何结构、验证和限制见 [场地说明](rmuc2027_battlefield_mjcf/README.md)。场地验证记录属于模型的采样结果，并不代表任意机器人、控制器或硬件通过验收。
