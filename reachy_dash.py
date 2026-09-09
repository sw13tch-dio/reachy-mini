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

import robot_ops
import wifi_setup

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
    """One Bluetooth pass: get the robot's address, or learn it needs setup.

    This used to have its own copy of the bleak calls, which meant two
    unsynchronised BLE code paths in one process. Windows permits exactly
    one BLE client at a time, so they knocked each other over - a
    TimeoutError here and a silent empty result there. Everything now goes
    through robot_ops, which serialises on a single lock.
    """
    units = robot_ops.ble_list(timeout=14.0)
    if not units:
        log("  no robots advertising over Bluetooth")
        return None

    setup_needed = None
    for u in units:
        log("  Bluetooth: found %s (%s dBm), asking for its address..."
            % (u["address"], u["rssi"]))
        st = robot_ops.ble_status(u["address"], tries=2)
        if not st:
            log("    could not read it this time")
            continue
        log("    it says: %s" % st.get("network"))
        if robot_ops.in_hotspot_mode(st):
            setup_needed = (u["address"], st.get("network"))
            continue
        ip = robot_ops.ip_from_ble(st)
        if ip and is_robot(ip):
            return ip
        if ip:
            log("    daemon not answering at %s yet" % ip)

    if setup_needed:
        raise NeedsSetup(*setup_needed)
    return None


def _local_subnets():
    """Which subnets to sweep, based on this machine's own addresses.

    Widening order matters. Campus DHCP pools can be far larger than the
    /24 the interface implies: this laptop has been handed 10.1.199.88,
    10.1.72.245 and 10.1.202.90 on ISCHOOL_IOT on different days, which is
    a /16 pool. Searching only a /24 or /18 silently misses the robot, so
    we try progressively wider masks and accept that the last one is slow.
    """
    tiers = [[], [], []]            # /24 fast, /18 medium, /16 last resort
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            # 192.168.137.x is Windows' own hotspot/ICS adapter. Its /24 is
            # worth a look; widening it to a /16 is 65,000 wasted probes.
            masks = (24,) if ip.startswith("192.168.137.") else (24, 18, 16)
            for slot, mask in enumerate(masks):
                tiers[slot].append(
                    str(ipaddress.ip_network("%s/%d" % (ip, mask), strict=False)))
    except Exception:
        pass
    out, seen = [], set()
    for tier in tiers:
        for n in tier:
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def _try_sweep():
    for net in _local_subnets():
        hosts = [str(h) for h in ipaddress.ip_network(net).hosts()]
        log("  scanning %s (%d addresses)..." % (net, len(hosts)))
        set_state(message="Scanning %s for Reachy..." % net)
        with cf.ThreadPoolExecutor(max_workers=500) as ex:
            for ip, ok in zip(hosts, ex.map(is_robot, hosts)):
                if ok:
                    return ip
    return None


class NeedsSetup(Exception):
    """Raised when a robot is found but has no WiFi - stops the search."""

    def __init__(self, address, network):
        super().__init__(address)
        self.address = address
        self.network = network


def _hotspot_check():
    """If Bluetooth says the robot is in setup mode, sweeping is pointless.

    A robot serving its own AP lives at 10.42.0.1 on a network this laptop
    is not on. No amount of LAN scanning will ever reach it, so stop and
    send the user to the Setup tab instead of grinding through 163,000
    addresses (which is what this used to do).
    """
    units = robot_ops.ble_list(timeout=14.0)
    for u in units:
        st = robot_ops.ble_status(u["address"], tries=2)
        if robot_ops.in_hotspot_mode(st):
            log("  %s is in SETUP MODE (%s) - stopping here" %
                (u["address"], (st or {}).get("network")))
            raise NeedsSetup(u["address"], (st or {}).get("network"))
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
        except NeedsSetup:
            raise
        except Exception as e:
            log("  %s failed: %s" % (label, type(e).__name__))
            ip = None
        if ip:
            return ip, label
    return None, None


def discovery_thread(hint):
    try:
        ip, method = discover(hint)
    except NeedsSetup as ns:
        set_state(phase="needs_setup", ip=None, method=None,
                  message="Robot found over Bluetooth, but it has no WiFi yet.")
        log("Robot %s needs first-time WiFi setup (%s)"
            % (ns.address, ns.network))
        return
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


# -------------------------------------------------- first-time WiFi setup
# The robot ships in access-point mode. To hand it credentials we have to
# leave your network, join its AP, talk to it, then come back. SETUP holds
# the state of that trip so the browser can follow along.

SETUP = {"stage": "idle", "message": "", "home_ssid": None,
         "ap_joined": False, "networks": [], "robot": None, "new_ip": None}


def setup_set(**kw):
    with _lock:
        SETUP.update(kw)


def setup_scan():
    """Find robots over Bluetooth and report whether they need WiFi setup."""
    setup_set(stage="scanning", message="Looking for robots over Bluetooth...")
    log("Setup: Bluetooth scan for robots...")
    found = robot_ops.ble_list(timeout=16.0)
    out = []
    for d in found:
        st = robot_ops.ble_status(d["address"], tries=2)
        ip = robot_ops.ip_from_ble(st)
        out.append({
            "address": d["address"],
            "rssi": d["rssi"],
            "network": (st or {}).get("network"),
            "hotspot": robot_ops.in_hotspot_mode(st),
            "ip": ip,
            "needs_setup": robot_ops.in_hotspot_mode(st),
        })
        log("  %s -> %s" % (d["address"], (st or {}).get("network")))
    setup_set(stage="scanned", message="Found %d robot(s)." % len(out),
              robot=out[0] if out else None)
    return {"robots": out}


def setup_networks():
    """Join the robot's AP and ask which networks it can see.

    Deliberately does NOT require a Bluetooth scan first. Joining the AP and
    getting an answer from the daemon on it is itself proof that a robot is
    there and needs setup - and it is far more reliable than BLE, whose GATT
    connect can wedge after repeated attempts and stay wedged until the robot
    is power-cycled.
    """
    home = wifi_setup.current_ssid()
    setup_set(stage="joining", home_ssid=home,
              message="Joining the robot's setup network...")
    log("Setup: home network is %s" % home)

    ok, msg = wifi_setup.ensure_profile(wifi_setup.AP_SSID, wifi_setup.AP_PASS)
    log("Setup: profile for %s -> %s (%s)" % (wifi_setup.AP_SSID, ok, msg[:120]))

    if not wifi_setup.join(wifi_setup.AP_SSID):
        setup_set(stage="error",
                  message="Could not join %s. Is the robot powered on and in "
                          "setup mode?" % wifi_setup.AP_SSID)
        if home:
            wifi_setup.join(home)
        return {"ok": False, "error": SETUP["message"]}

    setup_set(ap_joined=True, message="On the robot's network. Asking what it sees...")
    log("Setup: joined %s" % wifi_setup.AP_SSID)

    up, status = wifi_setup.robot_on_ap()
    if not up:
        setup_set(stage="error", message="Joined the AP but the robot's daemon "
                                         "did not answer.")
        if home:
            wifi_setup.join(home)
        return {"ok": False, "error": SETUP["message"]}

    st, nets = wifi_setup.robot_scan()
    log("Setup: robot sees %d networks" % len(nets))
    _, wifi_st = wifi_setup.robot_wifi_status()
    setup_set(stage="picking", networks=nets,
              message="Pick your network and enter the password.")
    return {"ok": True, "networks": nets,
            "daemon": status if isinstance(status, dict) else None,
            "wifi_status": wifi_st,
            "home_ssid": home}


def setup_connect(ssid, password):
    """Hand the credentials over. ssid/password go as query params."""
    if not SETUP.get("ap_joined") or wifi_setup.current_ssid() != wifi_setup.AP_SSID:
        return {"ok": False, "error": "Not on the robot's network any more. "
                                      "Run 'Find robots' and try again."}
    setup_set(stage="sending", message="Sending credentials to the robot...")
    log("Setup: POST /wifi/connect ssid=%s password=(%d chars)"
        % (ssid, len(password)))
    st, res = wifi_setup.send_credentials(ssid, password)
    log("Setup: robot replied %s %s" % (st, str(res)[:200]))
    ok = 200 <= st < 300
    if not ok:
        setup_set(stage="picking",
                  message="Robot rejected the credentials (HTTP %s)." % st)
        return {"ok": False, "status": st, "error": str(res)[:400]}
    setup_set(stage="switching",
              message="Accepted. The robot is switching networks...")
    return {"ok": True, "status": st, "result": res}


def setup_finish():
    """Put this laptop back on its own network, then locate the robot."""
    home = SETUP.get("home_ssid") or "ISCHOOL_IOT"
    setup_set(stage="returning", message="Putting this laptop back on %s..." % home)
    log("Setup: rejoining %s" % home)
    wifi_setup.join(home, timeout=60)
    setup_set(ap_joined=False)
    log("Setup: laptop now on %s" % wifi_setup.current_ssid())

    setup_set(message="Looking for the robot on your network...")
    time.sleep(8)
    new_ip = None

    # mDNS first - instant when it works (home networks), useless on campus.
    log("  trying reachy-mini.local ...")
    new_ip = _try_mdns()

    # Bluetooth next: exact answer, but its GATT can be wedged. One try only.
    if not new_ip:
        log("  asking over Bluetooth ...")
        try:
            for d in robot_ops.ble_list(timeout=12.0):
                st = robot_ops.ble_status(d["address"], tries=1)
                ip = robot_ops.ip_from_ble(st)
                if ip and not robot_ops.in_hotspot_mode(st):
                    new_ip = ip
                    break
        except Exception as e:
            log("  bluetooth unavailable: %s" % type(e).__name__)

    # Sweep last. Slower, but it does not care whether BLE is healthy.
    if not new_ip:
        log("  sweeping the network instead (Bluetooth did not answer) ...")
        setup_set(message="Scanning your network for the robot...")
        for attempt in range(2):
            new_ip = _try_sweep()
            if new_ip:
                break
            log("  not there yet; the robot may still be joining")
            time.sleep(15)

    if not new_ip:
        setup_set(stage="error",
                  message="Laptop is back online, but the robot has not joined "
                          "the network yet. Give it a minute and press "
                          "'Find robots' again.")
        return {"ok": False, "laptop_ssid": wifi_setup.current_ssid()}

    reachable = is_robot(new_ip)
    setup_set(stage="done", new_ip=new_ip,
              message="Robot is on the network at %s." % new_ip)
    log("Setup: robot is at %s (daemon reachable: %s)" % (new_ip, reachable))
    if reachable:
        try:
            with open(CACHE, "w") as f:
                f.write(new_ip)
        except Exception:
            pass
        set_state(phase="found", ip=new_ip, method="first-time setup",
                  message="Connected to Reachy at %s" % new_ip)
    return {"ok": True, "ip": new_ip, "daemon_reachable": reachable,
            "laptop_ssid": wifi_setup.current_ssid()}


# --------------------------------------------------------------- web server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                      # keep the console readable

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return None
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return None

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
            with _lock:
                s["setup"] = dict(SETUP)
            s["laptop_ssid"] = wifi_setup.current_ssid()
            return self._send(200, s)

        if self.path == "/dash/diag":
            with _lock:
                ip = STATE.get("ip")
            if not ip:
                return self._send(200, {"reachable": False,
                                        "error": "no robot connected"})
            d = robot_ops.diagnostics(ip)
            d["ip"] = ip
            d["hostname"] = robot_ops.get_hostname(ip)
            return self._send(200, d)

        if self.path == "/dash/apps":
            with _lock:
                ip = STATE.get("ip")
            if not ip:
                return self._send(200, {"apps": [], "error": "no robot connected"})
            return self._send(200, {"apps": robot_ops.apps_list(ip),
                                    "current": robot_ops.app_status(ip)})

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

        # ---------------- first-time WiFi setup ----------------
        if self.path == "/dash/setup/scan":
            return self._send(200, setup_scan())

        if self.path == "/dash/setup/networks":
            return self._send(200, setup_networks())

        if self.path == "/dash/setup/connect":
            body = self._read_json()
            ssid = (body or {}).get("ssid")
            pw = (body or {}).get("password")
            if not ssid or pw is None:
                return self._send(400, {"error": "ssid and password required"})
            return self._send(200, setup_connect(ssid, pw))

        if self.path == "/dash/setup/finish":
            return self._send(200, setup_finish())

        # ---------------- rename / reboot ----------------
        if self.path == "/dash/rename":
            body = self._read_json() or {}
            ip = body.get("ip") or STATE.get("ip")
            new = body.get("name")
            if not ip:
                return self._send(409, {"error": "no robot connected"})
            ok, msg = robot_ops.rename_robot(ip, new)
            log("rename %s -> %s : %s" % (ip, new, msg))
            return self._send(200 if ok else 400, {"ok": ok, "message": msg})

        if self.path == "/dash/reboot":
            body = self._read_json() or {}
            ip = body.get("ip") or STATE.get("ip")
            if not ip:
                return self._send(409, {"error": "no robot connected"})
            ok, msg = robot_ops.reboot_robot(ip)
            log("reboot %s : %s" % (ip, msg))
            return self._send(200 if ok else 400, {"ok": ok, "message": msg})

        # ---------------- apps ----------------
        if self.path == "/dash/apps/start":
            body = self._read_json() or {}
            ip = body.get("ip") or STATE.get("ip")
            name = body.get("name")
            if not ip or not name:
                return self._send(400, {"error": "ip and name required"})
            st, res = robot_ops.app_start(ip, name)
            log("start app %s -> %s" % (name, st))
            return self._send(200 if 200 <= st < 300 else 400,
                              {"ok": 200 <= st < 300, "status": st, "result": res})

        if self.path == "/dash/apps/stop":
            ip = (self._read_json() or {}).get("ip") or STATE.get("ip")
            if not ip:
                return self._send(409, {"error": "no robot connected"})
            st, res = robot_ops.app_stop(ip)
            log("stop app -> %s" % st)
            return self._send(200, {"ok": 200 <= st < 300, "status": st})

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


def _already_running(port):
    """True if a dashboard is already answering on this port."""
    try:
        s = socket.socket()
        s.settimeout(1.0)
        rc = s.connect_ex(("127.0.0.1", port))
        s.close()
        if rc != 0:
            return False
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/dash/state" % port, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


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

    # Refuse to start a second copy. Two instances fight over the one
    # Bluetooth radio and BOTH fail - one with a TimeoutError, one silently
    # returning nothing.
    #
    # Catching the bind error is not enough on Windows: http.server sets
    # SO_REUSEADDR, which here permits two live sockets on the same port
    # instead of failing the second one. So probe for a real listener.
    if _already_running(a.port):
        print()
        print("   A dashboard is already running on port %d." % a.port)
        print("   Open http://localhost:%d - don't start a second copy." % a.port)
        print()
        print("   (Two copies fight over the Bluetooth radio and both fail.)")
        print()
        try:
            input("   Press Enter to close this window. ")
        except EOFError:
            pass
        return

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    threading.Thread(target=discovery_thread, args=(a.ip,), daemon=True).start()
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(
            "http://localhost:%d" % a.port)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped. You can close this window.")


if __name__ == "__main__":
    main()
