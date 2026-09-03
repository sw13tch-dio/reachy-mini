#!/usr/bin/env python3
"""
Reachy Mini Dashboard - local server.

Finds the robot on the network and serves a simple control page.
Run it, and your browser opens automatically. No terminal knowledge needed.

Why this exists
---------------
Reachy Mini Control finds the robot using mDNS (`reachy-mini.local`). Campus,
lab, conference and hotel networks block mDNS multicast, so the app's "finding
robot" screen spins forever even though the robot is online and reachable.
This tool finds the robot four different ways instead, and talks straight to
the daemon's REST API.

Discovery order:
  1. cached IP from the last successful run
  2. mDNS (reachy-mini.local) - works on simple home networks
  3. Bluetooth LE - ask the robot directly what its IP is (needs `bleak`)
  4. subnet sweep - brute force port 8000 across the local /24, then /18

Usage:
    python reachy_dash.py
    python reachy_dash.py --ip 10.1.221.118    # skip discovery
    python reachy_dash.py --port 9999          # change dashboard port
"""

import argparse
import concurrent.futures as cf
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".reachy_ip")
ROBOT_PORT = 8000
BLE_NAME = "ReachyMini"
BLE_NET_CHAR = "12345678-1234-5678-1234-56789abcdef4"  # "CONNECTED [wlan0] <ip>"

STATE = {
    "phase": "idle",          # idle | searching | found | failed
    "message": "Starting up...",
    "ip": None,
    "method": None,
    "log": [],
}
_lock = threading.Lock()


def log(msg):
    line = "%s  %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with _lock:
        STATE["log"].append(line)
        del STATE["log"][:-40]


def set_state(**kw):
    with _lock:
        STATE.update(kw)


# --------------------------------------------------------------- robot API

def robot_call(ip, method, path, body=None, timeout=15):
    url = "http://%s:%d%s" % (ip, ROBOT_PORT, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw.decode("utf-8", "replace")[:400]}


def is_robot(ip, timeout=0.6):
    """True if a Reachy Mini daemon answers at this address."""
    try:
        s = socket.socket()
        s.settimeout(timeout)
        rc = s.connect_ex((ip, ROBOT_PORT))
        s.close()
        if rc != 0:
            return False
        return robot_call(ip, "GET", "/api/daemon/status",
                          timeout=3).get("robot_name") == "reachy_mini"
    except Exception:
        return False


# --------------------------------------------------------------- discovery

def _try_cache():
    try:
        with open(CACHE) as f:
            ip = f.read().strip()
    except Exception:
        return None
    return ip if ip and is_robot(ip) else None


def _try_mdns():
    try:
        ip = socket.gethostbyname("reachy-mini.local")
        return ip if is_robot(ip) else None
    except Exception:
        return None


def _try_ble():
    """Ask the robot over Bluetooth what its Wi-Fi address is."""
    try:
        import asyncio
        from bleak import BleakClient, BleakScanner
    except ImportError:
        log("  (Bluetooth search needs 'bleak' - run: pip install bleak)")
        return None

    async def go():
        dev = None
        for _ in range(2):
            for d in await BleakScanner.discover(timeout=12.0):
                if d.name and BLE_NAME.lower() in d.name.lower():
                    dev = d
                    break
            if dev:
                break
        if not dev:
            return None
        log("  Bluetooth: found %s, asking for its address..." % dev.address)
        async with BleakClient(dev, timeout=30.0,
                               winrt={"use_cached_services": False}) as c:
            txt = (await c.read_gatt_char(BLE_NET_CHAR)).decode("utf-8", "replace")
            log("  Bluetooth says: %s" % txt.strip())
            for tok in txt.replace("[", " ").replace("]", " ").split():
                try:
                    ipaddress.ip_address(tok)
                    return tok
                except ValueError:
                    continue
        return None

    try:
        ip = asyncio.run(go())
    except Exception as e:
        log("  Bluetooth search failed: %s" % type(e).__name__)
        return None
    if ip and is_robot(ip):
        return ip
    if ip:
        log("  Bluetooth gave %s but the daemon isn't answering there." % ip)
    return None


def _local_subnets():
    """Which subnets to sweep, based on this machine's own addresses."""
    small, big = [], []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            small.append(str(ipaddress.ip_network(ip + "/24", strict=False)))
            big.append(str(ipaddress.ip_network(ip + "/18", strict=False)))
    except Exception:
        pass
    out, seen = [], set()
    for n in small + big:           # fast /24s first, then wide /18s
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _try_sweep():
    for net in _local_subnets():
        hosts = [str(h) for h in ipaddress.ip_network(net).hosts()]
        log("  scanning %s (%d addresses)..." % (net, len(hosts)))
        set_state(message="Scanning %s for Reachy..." % net)
        with cf.ThreadPoolExecutor(max_workers=256) as ex:
            for ip, ok in zip(hosts, ex.map(is_robot, hosts)):
                if ok:
                    return ip
    return None


def discover(hint=None):
    set_state(phase="searching", message="Looking for Reachy...")
    if hint:
        log("Checking %s ..." % hint)
        if is_robot(hint):
            return hint, "the address you gave"
        log("Nothing there - searching properly.")

    for label, fn in [("last known address", _try_cache),
                      ("network name (mDNS)", _try_mdns),
                      ("Bluetooth", _try_ble),
                      ("network scan", _try_sweep)]:
        set_state(message="Looking for Reachy via %s..." % label)
        log("Trying %s..." % label)
        try:
            ip = fn()
        except Exception as e:
            log("  %s failed: %s" % (label, type(e).__name__))
            ip = None
        if ip:
            return ip, label
    return None, None


def discovery_thread(hint):
    ip, method = discover(hint)
    if ip:
        try:
            with open(CACHE, "w") as f:
                f.write(ip)
        except Exception:
            pass
        log("FOUND Reachy at %s (via %s)" % (ip, method))
        set_state(phase="found", ip=ip, method=method,
                  message="Connected to Reachy at %s" % ip)
    else:
        log("Could not find Reachy on this network.")
        set_state(phase="failed", ip=None, method=None,
                  message="Couldn't find Reachy.")


# --------------------------------------------------------------- motions

def do_wake(ip):
    robot_call(ip, "POST", "/api/motors/set_mode/enabled")
    time.sleep(1.0)
    robot_call(ip, "POST", "/api/move/play/wake_up", timeout=30)
    time.sleep(3.5)
    robot_call(ip, "POST", "/api/move/goto", {
        "duration": 1.2, "interpolation": "minjerk",
        "head_pose": {"x": 0, "y": 0, "z": 0, "roll": 0, "pitch": 0, "yaw": 0},
        "antennas": [0.17, 0.17], "body_yaw": 0.0})
    time.sleep(1.4)


def do_wiggle(ip, cycles=5):
    if robot_call(ip, "GET", "/api/motors/status").get("mode") != "enabled":
        robot_call(ip, "POST", "/api/motors/set_mode/enabled")
        time.sleep(0.8)
    for _ in range(cycles):
        robot_call(ip, "POST", "/api/move/goto",
                   {"duration": 0.22, "interpolation": "linear",
                    "antennas": [0.7, -0.7]})
        time.sleep(0.24)
        robot_call(ip, "POST", "/api/move/goto",
                   {"duration": 0.22, "interpolation": "linear",
                    "antennas": [-0.7, 0.7]})
        time.sleep(0.24)
    robot_call(ip, "POST", "/api/move/goto",
               {"duration": 0.6, "antennas": [0.17, 0.17]})
    time.sleep(0.7)


def do_sleep(ip):
    robot_call(ip, "POST", "/api/move/play/goto_sleep", timeout=30)
    time.sleep(3.0)
    robot_call(ip, "POST", "/api/motors/set_mode/disabled")


# ------------------------------------------------- hand off to the Pollen app

APP_PATHS = [
    r"C:\Program Files\Reachy Mini Control\reachy-mini-control.exe",
    r"C:\Program Files (x86)\Reachy Mini Control\reachy-mini-control.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Reachy Mini Control"
                       r"\reachy-mini-control.exe"),
    "/Applications/Reachy Mini Control.app",
]


def find_control_app():
    for p in APP_PATHS:
        if os.path.exists(p):
            return p
    return None


def open_control_app():
    """Launch Reachy Mini Control so the user can paste the address in."""
    p = find_control_app()
    if not p:
        raise RuntimeError("Reachy Mini Control isn't installed in the usual place.")
    if sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen([p], close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return p


# --------------------------------------------------------------- web server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                      # keep the console readable

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    return self._send(200, f.read().decode("utf-8"),
                                      "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(500, "dashboard.html is missing from %s" % HERE,
                                  "text/plain")

        if self.path == "/dash/state":
            with _lock:
                s = dict(STATE)
            if s["phase"] == "found":
                try:
                    d = robot_call(s["ip"], "GET", "/api/daemon/status", timeout=4)
                    b = d.get("backend_status", {})
                    cl = b.get("control_loop_stats", {})
                    s["robot"] = {
                        "reachable": True,
                        "state": d.get("state"),
                        "version": d.get("version"),
                        "motors": b.get("motor_control_mode"),
                        "hz": round(cl.get("mean_control_loop_frequency", 0), 1),
                        "errors": cl.get("nb_error"),
                    }
                    try:
                        f = robot_call(s["ip"], "GET", "/api/state/full", timeout=4)
                        s["robot"]["head_z"] = round(f["head_pose"]["z"], 4)
                        s["robot"]["antennas"] = [round(a, 2)
                                                  for a in f["antennas_position"]]
                    except Exception:
                        pass
                except Exception:
                    s["robot"] = {"reachable": False}
            s["control_app"] = bool(find_control_app())
            return self._send(200, s)

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        with _lock:
            ip = STATE.get("ip")

        if self.path == "/dash/search":
            threading.Thread(target=discovery_thread, args=(None,),
                             daemon=True).start()
            return self._send(200, {"ok": True})

        if self.path == "/dash/open-app":
            try:
                p = open_control_app()
                log("Opened Reachy Mini Control (%s)" % p)
                return self._send(200, {"ok": True, "path": p})
            except Exception as e:
                log("Could not open Reachy Mini Control: %s" % e)
                return self._send(500, {"error": str(e)})

        if not ip:
            return self._send(409, {"error": "Reachy isn't connected yet."})

        actions = {
            "/dash/wake":   lambda: do_wake(ip),
            "/dash/wiggle": lambda: do_wiggle(ip),
            "/dash/hello":  lambda: (do_wake(ip), do_wiggle(ip)),
            "/dash/sleep":  lambda: do_sleep(ip),
            "/dash/limp":   lambda: robot_call(ip, "POST",
                                               "/api/motors/set_mode/disabled"),
        }
        fn = actions.get(self.path)
        if not fn:
            return self._send(404, {"error": "unknown action"})
        try:
            log("Running '%s'..." % self.path.split("/")[-1])
            fn()
            log("  done.")
            return self._send(200, {"ok": True})
        except Exception as e:
            log("  FAILED: %s" % e)
            return self._send(500, {"error": str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", help="skip discovery, use this robot address")
    ap.add_argument("--port", type=int, default=9999, help="dashboard port")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    print("=" * 60)
    print("   Reachy Mini Dashboard")
    print("=" * 60)
    print()
    print("   Your browser should open automatically at:")
    print("       http://localhost:%d" % a.port)
    print()
    print("   Keep this window open while you use the dashboard.")
    print("   Close it when you are done.")
    print()
    print("-" * 60)

    threading.Thread(target=discovery_thread, args=(a.ip,), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(
            "http://localhost:%d" % a.port)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped. You can close this window.")


if __name__ == "__main__":
    main()
