// Capture configured DOM key codes before Viser's document-level bindings.
export function attachInput(root, frame, status, capture, config, openSocket = (url) => new WebSocket(url)) {
  const keys = new Set(config.keys);
  const held = new Set();
  const bound = new WeakSet();
  let socket;
  let heartbeat;
  let reconnect;
  let stopped = false;
  let captureMouse = false;
  let firstPerson = false;
  let fovyRad = 0;
  const endpoint = new URL("input", root.location.href);
  endpoint.protocol = endpoint.protocol === "https:" ? "wss:" : "ws:";

  function send(mouse_delta = [0, 0], aim_delta = [0, 0]) {
    if (socket?.readyState === 1) {
      socket.send(JSON.stringify({ keys: [...held], mouse_delta, aim_delta }));
    }
  }

  function release() {
    held.clear();
    send();
  }

  function connect() {
    const current = openSocket(endpoint.toString());
    socket = current;
    current.onopen = () => {
      if (socket !== current || stopped) {
        current.close();
        return;
      }
      held.clear();
      status.textContent = "Input connected";
      send();
    };
    current.onmessage = (event) => {
      if (socket !== current) return;
      const mode = JSON.parse(event.data);
      const wasFirstPerson = firstPerson;
      firstPerson = Boolean(mode.first_person);
      fovyRad = firstPerson ? Number(mode.fovy_rad) : 0;
      if (wasFirstPerson && !firstPerson) frame.contentDocument?.exitPointerLock?.();
    };
    current.onclose = () => {
      if (socket !== current) return;
      held.clear();
      firstPerson = false;
      fovyRad = 0;
      frame.contentDocument?.exitPointerLock?.();
      status.textContent = "Input disconnected; reconnecting";
      if (!stopped) reconnect = root.setTimeout(connect, 1000);
    };
    current.onerror = () => current.close();
  }

  function editable(target) {
    return target?.isContentEditable || /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(target?.tagName || "");
  }

  function keydown(event) {
    if (event.code === "Escape") {
      release();
      return;
    }
    if (editable(event.target) || event.ctrlKey || event.metaKey || event.altKey || !keys.has(event.code)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.repeat || held.has(event.code) || socket?.readyState !== 1) return;
    held.add(event.code);
    send();
  }

  function keyup(event) {
    if (!held.delete(event.code)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    send();
  }

  function bind(window) {
    if (bound.has(window.document)) return;
    bound.add(window.document);
    window.addEventListener("keydown", keydown, true);
    window.addEventListener("keyup", keyup, true);
    window.addEventListener("blur", release);
    window.addEventListener("pointerdown", (event) => {
      if ((!captureMouse && !firstPerson) || event.target?.tagName !== "CANVAS") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.button === 0) event.target.requestPointerLock?.();
    }, true);
    window.addEventListener("pointermove", (event) => {
      if (firstPerson && event.target?.tagName === "CANVAS") event.stopImmediatePropagation();
    }, true);
    window.addEventListener("wheel", (event) => {
      if (!firstPerson || event.target?.tagName !== "CANVAS") return;
      event.preventDefault();
      event.stopImmediatePropagation();
    }, { capture: true, passive: false });
    window.addEventListener("mousemove", (event) => {
      if (!window.document.pointerLockElement || socket?.readyState !== 1) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const mouse = [event.movementX, event.movementY];
      const scale = fovyRad / Math.max(window.innerHeight, 1);
      const aim = firstPerson ? mouse.map((v) => Math.max(-Math.PI, Math.min(Math.PI, -v * scale))) : [0, 0];
      send(mouse.map((v) => Math.max(-10000, Math.min(10000, v))), aim);
    }, true);
    window.document.addEventListener("pointerlockchange", release);
    window.document.addEventListener("focusin", (event) => {
      if (editable(event.target)) release();
    });
    window.document.addEventListener("visibilitychange", () => {
      if (window.document.hidden) release();
    });
  }

  capture.addEventListener("click", () => {
    captureMouse = !captureMouse;
    capture.textContent = captureMouse ? "Use viewer mouse" : "Capture mouse";
    if (!captureMouse) frame.contentDocument?.exitPointerLock?.();
  });
  bind(root);
  frame.addEventListener("load", () => {
    release();
    bind(frame.contentWindow);
  });
  if (frame.contentDocument?.readyState === "complete") bind(frame.contentWindow);
  function start() {
    connect();
    heartbeat = root.setInterval(send, config.heartbeat_ms);
  }
  start();
  root.addEventListener("pagehide", () => {
    stopped = true;
    release();
    root.clearInterval(heartbeat);
    root.clearTimeout(reconnect);
    socket?.close();
  });
  root.addEventListener("pageshow", (event) => {
    if (event.persisted && stopped) {
      stopped = false;
      start();
    }
  });
}

if (typeof window !== "undefined") {
  const frame = document.getElementById("viewer");
  const status = document.getElementById("connection");
  const capture = document.getElementById("capture");
  fetch("input-config").then((response) => {
    if (!response.ok) throw new Error("Input configuration unavailable");
    return response.json();
  }).then((config) => attachInput(window, frame, status, capture, config)).catch(() => {
    status.textContent = "Input unavailable; refresh the page";
  });
}
