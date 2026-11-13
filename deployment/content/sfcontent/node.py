"""Content node: one long-running process on PC3 that owns the demo state and the three senders.

- control server, TCP 47010 (phone -> generator, newline-delimited JSON, contract v1);
- clock service, UDP 47000 (as sf_traffic.py);
- weather model (always running, so the panel shows live readings) and the weather sender (thread);
- crop and drone senders (one spawned process each, started from the panel or by the node agent);
- delegation socket (Unix, VRF-agnostic): `sf_content.py send` hands agent-started runs to the node;
- local web panel on 127.0.0.1:8090 (its socket is unbound from the VRF, so a browser in the default VRF reaches it).

Run inside the N3IWUE VRF (`sudo ip vrf exec vrf-n3iwue python3 sf_content.py node ...`): every phone-facing socket
and the collector POSTs then use the PDU session, exactly like `sf_traffic.py send`.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import multiprocessing as mp
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import protocol as P
from .senders import RunParams, summary_record, weather_loop
from .weather import STATES as WEATHER_STATES
from .weather import WeatherModel

SERVICES = ("weather", "crop", "drone")
STATES = {"weather": WEATHER_STATES, "crop": ("healthy", "pest", "water_stress"), "drone": ("normal", "alert")}
DEFAULT_STATE = {"weather": "stable", "crop": "healthy", "drone": "normal"}
PRETTY = {"stable": "Stable", "drought": "Drought", "storm": "Storm", "healthy": "Healthy", "pest": "Pest",
          "water_stress": "Water stress", "normal": "Normal", "alert": "Alert"}
UI_FILE = Path(__file__).with_name("ui") / "index.html"
COLLECTOR = "http://10.45.0.1:8088/report"


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S ") + message, flush=True)


class Runner:
    """One service: at most one run at a time (a new start replaces the current run)."""

    def __init__(self, node: "ContentNode", service: str) -> None:
        self.node = node
        self.service = service
        self.lock = threading.RLock()
        self.params: RunParams | None = None
        self.stop_event = None
        self.ctrl = None
        self.process = None
        self.running = False
        self.status: dict = {}
        self.started_wall: float | None = None
        self.last_summary: dict | None = None
        self.last_error: str | None = None
        self.history: collections.deque[tuple[float, int, int]] = collections.deque(maxlen=40)
        self.done = threading.Event()
        self.done.set()

    # ------------------------------------------------------------ lifecycle
    def start(self, params: RunParams) -> None:
        self.stop(wait=True)
        with self.lock:
            self.params = params
            self.status = {"phase": "warmup"}
            self.history.clear()
            self.last_error = None
            self.running = True
            self.started_wall = time.time()
            self.done.clear()
            if self.service == "weather":
                self.stop_event = threading.Event()
                threading.Thread(target=self._weather_thread, args=(params, self.stop_event), daemon=True).start()
            else:
                ctx = self.node.mp
                self.stop_event, self.ctrl = ctx.Event(), ctx.Queue()
                from .senders import child_main

                self.process = ctx.Process(target=child_main, name=f"sf-content-{self.service}",
                                           args=(params.to_dict(), self.ctrl, self.node.status_q, self.stop_event),
                                           daemon=True)
                self.process.start()
                threading.Thread(target=self._watch, args=(self.process, params.run_id), daemon=True).start()
        duration = "until stopped" if params.duration is None else f"{params.duration:g} s"
        self.node.event(f"{self.service} started -> {params.target} ({params.rate_bps / 1e6:g} Mbit/s, {duration}, "
                        f"{params.origin}, run {params.run_id[:8]})")

    def stop(self, wait: bool = True, timeout: float = 8.0) -> None:
        with self.lock:
            if not self.running:
                return
            if self.stop_event is not None:
                self.stop_event.set()
        if wait and not self.done.wait(timeout):
            with self.lock:
                if self.process is not None and self.process.is_alive():
                    self.process.terminate()
            self.done.wait(2.0)
            self._finish(None, "stopped without summary")

    def send_state(self, state: str) -> None:
        with self.lock:
            if self.ctrl is not None and self.running:
                with contextlib.suppress(Exception):
                    self.ctrl.put_nowait({"state": state})

    def _weather_thread(self, params: RunParams, stop: threading.Event) -> None:
        try:
            if stop.wait(params.warmup_s):
                self._finish(None, None)
                return
            started = P.now_ns()
            self.on_status({"phase": "running", "started_at_ns": started})
            result = weather_loop(params, self.node.weather_document, stop, self.on_status)
            time.sleep(0.5 if stop.is_set() else 1.0)
            record = summary_record(params, started, result)
            print(json.dumps(record), flush=True)
            P.T.post(params.report, record, wait=True)
            self._finish(record, None)
        except Exception as error:  # noqa: BLE001
            self._finish(None, f"{type(error).__name__}: {error}")

    def _watch(self, process, run_id: str) -> None:  # type: ignore[no-untyped-def]
        process.join()
        time.sleep(1.5)  # let the pump deliver the summary first
        with self.lock:
            same = self.params is not None and self.params.run_id == run_id and self.running
        if same:
            self._finish(None, f"sender process exited (code {process.exitcode}) without summary")

    # ------------------------------------------------------------ updates (pump / weather thread)
    def on_status(self, update: dict) -> None:
        with self.lock:
            self.status.update({k: v for k, v in update.items() if not k.startswith("thumb_")})
            if "sent_bytes" in update:
                self.history.append((time.monotonic(), update["sent_bytes"], update.get("sent_packets", 0)))
        if "thumb_rgb" in update:
            self.node.crop_preview = {"rgb": update["thumb_rgb"], "ndvi": update["thumb_ndvi"],
                                      "meta": update.get("meta"), "at": time.time()}

    def _finish(self, summary: dict | None, error: str | None) -> None:
        with self.lock:
            if not self.running:
                return
            self.running = False
            self.process = None
            if summary is not None:
                self.last_summary = summary
            if error:
                self.last_error = error
            self.status["phase"] = "stopped"
        if summary is not None:
            self.node.event(f"{self.service} finished: {summary.get('sent_bytes', 0) / 1e6:.1f} MB in "
                            f"{summary.get('elapsed_s')} s, offered {summary.get('offered_bps', 0) / 1e6:.2f} Mbit/s")
        if error:
            self.node.event(f"{self.service} error: {error}")
        self.done.set()

    def rate(self) -> tuple[float, float]:
        with self.lock:
            if len(self.history) < 2:
                return 0.0, 0.0
            newest = self.history[-1]
            oldest = next((h for h in self.history if newest[0] - h[0] <= 2.5), self.history[0])
            dt = newest[0] - oldest[0]
            if dt <= 0 or time.monotonic() - newest[0] > 3.0:
                return 0.0, 0.0
            return (newest[1] - oldest[1]) * 8 / dt, (newest[2] - oldest[2]) / dt

    def snapshot(self) -> dict:
        bps, pps = self.rate()
        with self.lock:
            p = self.params
            return {"running": self.running, "target": p.target if p else None, "run_id": p.run_id if p else None,
                    "origin": p.origin if p else None, "rate_bps_requested": p.rate_bps if p else None,
                    "duration_s": p.duration if p else None, "started_at": self.started_wall if self.running else None,
                    "status": dict(self.status), "rate_bps": round(bps), "pps": round(pps, 1),
                    "last_summary": self.last_summary, "last_error": self.last_error}


class ContentNode:
    def __init__(self, args: argparse.Namespace, ui: bool = True, unix: bool = True) -> None:
        self.args = args
        self.ports = P.ports_with_offset(args.port_offset)
        self.media = str(Path(args.media).resolve())
        self.report = args.report or None
        self.target = args.target
        self.mp = mp.get_context("spawn")
        self.status_q = self.mp.Queue(maxsize=2000)
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.states = dict(DEFAULT_STATE)
        self.state_file = Path(args.state_file) if args.state_file else None
        self._load_state()
        self.weather = WeatherModel(seed=args.seed, state=self.states["weather"], fixed_hour=args.weather_hour)
        self.runners = {s: Runner(self, s) for s in SERVICES}
        self.commands: collections.deque[dict] = collections.deque(maxlen=40)
        self.events: collections.deque[dict] = collections.deque(maxlen=60)
        self.crop_preview: dict | None = None
        self.control_clients = 0
        self.control_listening = False
        self.ue_address: str | None = None
        self.started = time.time()
        self._drone_frames = None
        self._thumb_cache: tuple[tuple[str, int], bytes] | None = None
        self.ui_enabled, self.unix_enabled = ui, unix
        self.threads: list[threading.Thread] = []

    # ------------------------------------------------------------ state
    def _load_state(self) -> None:
        if self.state_file and self.state_file.exists():
            with contextlib.suppress(OSError, ValueError):
                saved = json.loads(self.state_file.read_text())
                for service in SERVICES:
                    if saved.get(service) in STATES[service]:
                        self.states[service] = saved[service]
                if saved.get("target") and not self.args.target_given:
                    self.args.target = saved["target"]
                self.target = self.args.target

    def _save_state(self) -> None:
        if not self.state_file:
            return
        with contextlib.suppress(OSError):
            self.state_file.write_text(json.dumps({**self.states, "target": self.target}))

    def event(self, text: str) -> None:
        self.events.appendleft({"at": time.time(), "text": text})
        log(text)

    def weather_document(self) -> dict:
        with self.lock:
            return self.weather.document()

    def set_state(self, service: str, state: str, source: str) -> str:
        if service not in STATES:
            raise ValueError(f"unknown service {service!r}")
        if state not in STATES[service]:
            raise ValueError(f"unknown {service} state {state!r}; expected one of {', '.join(STATES[service])}")
        with self.lock:
            if service == "weather":
                self.weather.set_state(state)
            self.states[service] = state
            self._save_state()
        self.runners[service].send_state(state)
        detail = f"{service} state set to {state}"
        if service == "drone" and self.runners["drone"].running:
            detail += " (applied at the next loop boundary)"
        return detail

    def irrigate(self, duration_s: float) -> str:
        with self.lock:
            detail = self.weather.irrigate(duration_s)
            self.states["weather"] = self.weather.state
            self._save_state()
        return detail

    # ------------------------------------------------------------ control commands (phone, panel)
    def handle_command(self, request: object, peer: str, source: str) -> dict:
        cmd = request.get("cmd") if isinstance(request, dict) else None
        reply = {"v": 1, "ok": False, "cmd": cmd if isinstance(cmd, str) else None, "detail": ""}
        text = None
        try:
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            if request.get("v") != 1:
                raise ValueError("unsupported protocol version (expected v=1)")
            if cmd == "irrigation":
                action = request.get("action", "activate")
                if action != "activate":
                    raise ValueError(f"unknown irrigation action {action!r}")
                duration = request.get("duration_s", 60)
                if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                    raise ValueError("duration_s must be a number")
                reply["detail"] = self.irrigate(float(duration))
                text = f"Irrigation requested from {source} · {int(round(duration))} s"
            elif cmd == "state":
                service, state = request.get("service"), request.get("state")
                reply["detail"] = self.set_state(str(service), str(state), source)
                text = f"{str(service).capitalize()} state → {PRETTY.get(str(state), state)} ({source})"
            else:
                raise ValueError(f"unknown command {cmd!r}")
            reply["ok"] = True
        except ValueError as error:
            reply["detail"] = str(error)
            text = f"Refused {cmd or 'request'} from {source}: {error}"
        self.commands.appendleft({"at": time.time(), "peer": peer, "source": source, "ok": reply["ok"],
                                  "text": text, "request": request if isinstance(request, dict) else None,
                                  "detail": reply["detail"]})
        log(f"control {source} {peer}: {json.dumps(request)[:200]} -> {json.dumps(reply)}")
        return reply

    def _control_server(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self.args.bind, self.ports["control"]))
        except OSError as error:
            self.event(f"control server not started on :{self.ports['control']} ({error})")
            return
        server.listen(16)
        server.settimeout(0.5)
        self.control_listening = True
        self.event(f"control server listening on tcp/{self.ports['control']}")
        while not self.stop.is_set():
            try:
                conn, peer = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._control_connection, args=(conn, peer), daemon=True).start()
        server.close()
        self.control_listening = False

    def _control_connection(self, conn: socket.socket, peer: tuple[str, int]) -> None:
        self.control_clients += 1
        try:
            with conn:
                conn.settimeout(600)
                reader = conn.makefile("rb")
                while not self.stop.is_set():
                    line = reader.readline(8192)
                    if not line:
                        return
                    if not line.strip():
                        continue
                    try:
                        request = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        request = None
                        reply = {"v": 1, "ok": False, "cmd": None, "detail": "invalid JSON line"}
                        self.commands.appendleft({"at": time.time(), "peer": peer[0], "source": "phone", "ok": False,
                                                  "text": "Refused an invalid JSON line from phone", "request": None,
                                                  "detail": reply["detail"]})
                    else:
                        reply = self.handle_command(request, peer[0], "phone")
                    conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            self.control_clients -= 1

    # ------------------------------------------------------------ runs
    def make_params(self, service: str, target: str, duration: float | None, run_id: str | None = None,
                    rate_bps: int | None = None, report: str | None = None, warmup_s: float = 2.0,
                    object_bytes: int = P.CROP_OBJECT_BYTES, origin: str = "panel") -> RunParams:
        if service not in SERVICES:
            raise ValueError(f"unknown service {service!r}")
        socket.inet_aton(target)
        if run_id is None:
            run_id = uuid.uuid4().hex
        bytes.fromhex(run_id)
        if len(run_id) != 32:
            raise ValueError("run_id must be 32 hex digits")
        rate = int(rate_bps or P.DEFAULT_RATE[service])
        if rate <= 0:
            raise ValueError("rate must be positive")
        return RunParams(service=service, target=target, run_id=run_id, duration=duration, rate_bps=rate,
                         report=self.report if report is None else (report or None), media=self.media, ports=self.ports,
                         warmup_s=warmup_s, object_bytes=object_bytes, state=self.states[service], origin=origin)

    def start_run(self, params: RunParams) -> None:
        if params.origin != "standalone":
            self.target = params.target
            self._save_state()
        params.state = self.states[params.service]
        self.runners[params.service].start(params)

    def _pump(self) -> None:
        while not self.stop.is_set():
            try:
                kind, run_id, payload = self.status_q.get(timeout=0.5)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                return
            runner = next((r for r in self.runners.values() if r.params and r.params.run_id == run_id), None)
            if runner is None:
                continue
            if kind == "status":
                runner.on_status(payload)
            elif kind == "summary":
                runner._finish(payload, None)
            elif kind == "error":
                runner._finish(None, payload.get("error"))

    def _weather_tick(self) -> None:
        last = time.monotonic()
        while not self.stop.wait(0.1):
            now = time.monotonic()
            with self.lock:
                self.weather.step(now - last)
                events = self.weather.pop_events()
                if self.weather.state != self.states["weather"]:
                    self.states["weather"] = self.weather.state
                    self._save_state()
            last = now
            for text in events:
                self.event(f"weather: {text}")

    def _ue_address_poll(self) -> None:
        while True:
            try:
                out = subprocess.run(["ip", "-4", "-o", "addr", "show", "master", self.args.vrf], capture_output=True,
                                     text=True, timeout=3, check=False).stdout
                addresses = [line.split()[3].split("/")[0] for line in out.splitlines() if " inet " in line]
                self.ue_address = next((a for a in addresses if a.startswith("10.45.")), None) or "-"
            except (OSError, subprocess.SubprocessError, IndexError):
                self.ue_address = "-"
            if self.stop.wait(5.0):
                return

    # ------------------------------------------------------------ previews
    def drone_frames(self):  # type: ignore[no-untyped-def]
        if self._drone_frames is None:
            from .drone import DroneFrames

            with contextlib.suppress(FileNotFoundError):
                self._drone_frames = DroneFrames(Path(self.media))
        return self._drone_frames

    def drone_thumbnail(self) -> bytes | None:
        frames = self.drone_frames()
        if frames is None:
            return None
        status = self.runners["drone"].status
        key = (status.get("variant", self.states["drone"]), int(status.get("loop_index", 0)))
        if self._thumb_cache and self._thumb_cache[0] == key:
            return self._thumb_cache[1]
        data = frames.thumbnail(*key)
        self._thumb_cache = (key, data)
        return data

    def status(self) -> dict:
        with self.lock:
            weather = self.weather.document()
            irrigation = {"active": self.weather.irrigation_active(), "remaining_s": self.weather.irrigation_remaining(),
                          "duration_s": self.weather.irrigation_duration}
            states = dict(self.states)
        services = {s: self.runners[s].snapshot() for s in SERVICES}
        services["crop"]["preview"] = (self.crop_preview or {}).get("meta")
        services["crop"]["preview_at"] = (self.crop_preview or {}).get("at")
        frames = self.drone_frames()
        services["drone"]["loop"] = ({"frames": frames.count, "fps": frames.fps,
                                      "mean_frame_bytes": frames.manifest["stats"]["normal"]["bytes_mean"]}
                                     if frames else None)
        return {"v": 1, "now": time.time(), "uptime_s": round(time.time() - self.started), "target": self.target,
                "ue_address": self.ue_address, "ports": self.ports, "report": self.report, "states": states,
                "options": {s: list(v) for s, v in STATES.items()}, "weather": weather, "irrigation": irrigation,
                "services": services, "commands": list(self.commands)[:12], "events": list(self.events)[:14],
                "control": {"listening": self.control_listening, "clients": self.control_clients}}

    # ------------------------------------------------------------ delegation socket (sf_content.py send)
    def _unix_server(self) -> None:
        path = self.args.node_socket
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)
        try:
            server.bind(path)
        finally:
            os.umask(old)
        server.listen(8)
        server.settimeout(0.5)
        self.event(f"delegation socket {path}")
        while not self.stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._delegated, args=(conn,), daemon=True).start()
        server.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)

    def _delegated(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(10)
                request = json.loads(conn.makefile("rb").readline(65536))
                if request.get("op") == "status":
                    conn.sendall((json.dumps(self.status(), default=str) + "\n").encode())
                    return
                params = self.make_params(request["service"], request["target"], request.get("duration"),
                                          request.get("run_id"), request.get("rate_bps"), request.get("report"),
                                          float(request.get("warmup_s", 2.0)),
                                          int(request.get("object_bytes", P.CROP_OBJECT_BYTES)), origin="agent")
            except (OSError, ValueError, KeyError, TypeError) as error:
                with contextlib.suppress(OSError):
                    conn.sendall((json.dumps({"event": "error", "error": str(error)}) + "\n").encode())
                return
            runner = self.runners[params.service]
            self.start_run(params)
            conn.sendall((json.dumps({"event": "started", "run_id": params.run_id, "state": params.state}) +
                          "\n").encode())
            conn.settimeout(0.5)
            while not runner.done.is_set():
                try:
                    if conn.recv(1) == b"":  # the `send` process went away (traffic-stop): stop this run
                        if runner.params and runner.params.run_id == params.run_id:
                            runner.stop(wait=True)
                        return
                except TimeoutError:
                    continue
                except OSError:
                    return
            with contextlib.suppress(OSError):
                summary = runner.last_summary if runner.last_summary and \
                    runner.last_summary.get("run_id") == params.run_id else None
                conn.sendall((json.dumps({"event": "summary", "record": summary, "error": runner.last_error}) +
                              "\n").encode())

    # ------------------------------------------------------------ web panel
    def _http(self) -> None:
        node = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def server_bind(self) -> None:
                bound = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, 32).rstrip(b"\0")
                if bound:  # inside `ip vrf exec`: the panel stays on the default VRF's loopback
                    try:
                        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"")
                    except OSError as error:
                        log(f"panel: cannot leave {bound.decode()} ({error}); it may be unreachable")
                super().server_bind()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

            def _send(self, code: int, body: bytes, kind: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: object) -> None:
                self._send(code, json.dumps(obj, default=str).encode(), "application/json")

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    initial = json.dumps(node.status(), default=str).replace("</", "<\\/")
                    page = UI_FILE.read_text(encoding="utf-8").replace("/*__INITIAL__*/", f"window.INITIAL = {initial};")
                    self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                elif path == "/api/status":
                    self._json(200, node.status())
                elif path in ("/api/crop/rgb.jpg", "/api/crop/ndvi.jpg"):
                    preview = node.crop_preview
                    data = preview and preview["rgb" if "rgb" in path else "ndvi"]
                    self._send(200, data, "image/jpeg") if data else self._send(404, b"", "text/plain")
                elif path == "/api/drone/frame.jpg":
                    data = node.drone_thumbnail()
                    self._send(200, data, "image/jpeg") if data else self._send(404, b"", "text/plain")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
                    path = self.path.split("?")[0]
                    if path == "/api/state":
                        detail = node.handle_command({"v": 1, "cmd": "state", "service": body.get("service"),
                                                      "state": body.get("state")}, "127.0.0.1", "panel")
                        self._json(200, detail)
                    elif path == "/api/irrigation":
                        self._json(200, node.handle_command({"v": 1, "cmd": "irrigation", "action": "activate",
                                                             "duration_s": body.get("duration_s", 60)},
                                                            "127.0.0.1", "panel"))
                    elif path == "/api/start":
                        target = body.get("target") or node.target
                        if not target:
                            self._json(400, {"ok": False, "error": "no phone address yet: start a service from the app "
                                                                   "first, or type the phone IP in the panel"})
                            return
                        params = node.make_params(body["service"], target, None, rate_bps=body.get("rate_bps"),
                                                  warmup_s=1.0)
                        threading.Thread(target=node.start_run, args=(params,), daemon=True).start()
                        self._json(200, {"ok": True, "run_id": params.run_id})
                    elif path == "/api/stop":
                        threading.Thread(target=node.runners[body["service"]].stop, daemon=True).start()
                        self._json(200, {"ok": True})
                    elif path == "/api/target":
                        socket.inet_aton(body["target"])
                        node.target = body["target"]
                        node._save_state()
                        self._json(200, {"ok": True})
                    else:
                        self._json(404, {"ok": False, "detail": "not found"})
                except (ValueError, KeyError, TypeError, OSError) as error:
                    self._json(400, {"ok": False, "detail": str(error)})

        try:
            server = Server((self.args.ui_host, self.args.ui_port), Handler)
        except OSError as error:
            self.event(f"panel not started on {self.args.ui_host}:{self.args.ui_port} ({error})")
            return
        self.event(f"panel on http://{self.args.ui_host}:{self.args.ui_port}/")
        server.timeout = 0.5
        while not self.stop.is_set():
            server.handle_request()
        server.server_close()

    # ------------------------------------------------------------ process
    def start(self) -> None:
        targets = [self._pump, self._weather_tick, self._control_server, self._ue_address_poll,
                   lambda: P.time_responder(self.stop, self.ports["time"], self.args.bind)]
        if self.ui_enabled and self.args.ui_port:
            targets.append(self._http)
        if self.unix_enabled:
            targets.append(self._unix_server)
        for target in targets:
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)
        self.event(f"content node up: media={self.media} target={self.target} report={self.report} "
                   f"ports={self.ports}")

    def shutdown(self) -> None:
        for runner in self.runners.values():
            runner.stop(wait=True)
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout=2)


# ---------------------------------------------------------------- CLI

def add_arguments(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    default_media = str(Path(__file__).resolve().parent.parent / "media")

    def common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--media", default=default_media)
        parser.add_argument("--node-socket", default=P.default_node_socket())
        parser.add_argument("--port-offset", type=int, default=0, help="tests only: shift every contract port")
        parser.add_argument("--bind", default="0.0.0.0", help="address of the control/clock services")
        parser.add_argument("--seed", type=int)
        parser.add_argument("--weather-hour", type=float, help="pin the time of day of the weather model (0-24)")
        parser.add_argument("--vrf", default="vrf-n3iwue", help="only to display the UE address in the panel")
        parser.add_argument("--state-file", default="")

    node = sub.add_parser("node", help="run the content node (control server, senders, panel)")
    node.add_argument("--target", help="phone service IPv4 for runs started from the panel (default: none until an "
                      "app/orchestrator request brings the phone's current address; editable in the panel)")
    node.add_argument("--report", default=COLLECTOR, help="collector URL for generator summaries ('' = none)")
    node.add_argument("--ui-host", default="127.0.0.1")
    node.add_argument("--ui-port", type=int, default=8090)
    node.add_argument("--autostart", default="", help="comma-separated services to start at once (panel runs)")
    common(node)

    send = sub.add_parser("send", help="drop-in for `sf_traffic.py send` (node agent)")
    send.add_argument("--service", choices=SERVICES, required=True)
    send.add_argument("--target", required=True)
    send.add_argument("--duration", type=float, default=30)
    send.add_argument("--run-id")
    send.add_argument("--rate-bps", type=int)
    send.add_argument("--size", type=int, help="ignored: the contract fixes the datagram sizes")
    send.add_argument("--object-bytes", type=int, default=P.CROP_OBJECT_BYTES)
    send.add_argument("--warmup-s", type=float, default=2.0)
    send.add_argument("--report")
    send.add_argument("--standalone", action="store_true", help="never hand the run to a running node")
    common(send)


def run_node(args: argparse.Namespace) -> int:
    args.target_given = bool(args.target)
    args.target = args.target or ""
    if not args.state_file:
        args.state_file = "/var/tmp/smartfarm-content-state.json" if os.geteuid() == 0 else \
            f"/tmp/smartfarm-content-state-{os.getuid()}.json"
    node = ContentNode(args)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    node.start()
    for service in filter(None, (s.strip() for s in args.autostart.split(","))):
        node.start_run(node.make_params(service, node.target, None))
    stop.wait()
    log("shutting down: stopping senders (summaries are posted)")
    node.shutdown()
    return 0


def run_send(args: argparse.Namespace) -> int:
    run_id = args.run_id or uuid.uuid4().hex
    rate = args.rate_bps or P.DEFAULT_RATE[args.service]
    print(f"generator start service={args.service} target={args.target} rate_bps={rate} duration_s={args.duration} "
          f"content=1", flush=True)
    request = {"op": "run", "service": args.service, "target": args.target, "duration": args.duration,
               "run_id": run_id, "rate_bps": rate, "report": args.report or "", "warmup_s": args.warmup_s,
               "object_bytes": args.object_bytes}
    if not args.standalone and os.path.exists(args.node_socket):
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.connect(args.node_socket)
        except OSError as error:
            print(f"content node unreachable ({error}); running standalone", flush=True)
        else:
            return _delegate(conn, request)
    return _standalone(args, request)


def _delegate(conn: socket.socket, request: dict) -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # closing the socket stops the node's run
    with conn:
        conn.sendall((json.dumps(request) + "\n").encode())
        reader = conn.makefile("rb")
        for line in reader:
            message = json.loads(line)
            if message.get("event") == "started":
                print(f"delegated to the content node: run_id={message['run_id']} state={message.get('state')}",
                      flush=True)
            elif message.get("event") == "summary":
                if message.get("record"):
                    print(json.dumps(message["record"]), flush=True)
                if message.get("error"):
                    print(f"error: {message['error']}", flush=True)
                return 0 if message.get("record") else 1
            elif message.get("event") == "error":
                print(f"error: {message.get('error')}", flush=True)
                return 1
    print("error: content node closed the connection", flush=True)
    return 1


def _standalone(args: argparse.Namespace, request: dict) -> int:
    args.target_given = True
    args.state_file = args.state_file or ("/var/tmp/smartfarm-content-state.json" if os.geteuid() == 0 else
                                          f"/tmp/smartfarm-content-state-{os.getuid()}.json")
    args.ui_port = 0
    node = ContentNode(args, ui=False, unix=False)
    node.start()
    signal.signal(signal.SIGTERM, lambda *_: node.runners[args.service].stop_event and
                  node.runners[args.service].stop_event.set())
    params = node.make_params(args.service, args.target, args.duration, request["run_id"], request["rate_bps"],
                              args.report or "", args.warmup_s, args.object_bytes, origin="standalone")
    node.start_run(params)
    runner = node.runners[args.service]
    runner.done.wait()
    node.stop.set()
    return 0 if runner.last_summary else 1


def dispatch(args: argparse.Namespace) -> int:
    if args.command == "node":
        return run_node(args)
    return run_send(args)
