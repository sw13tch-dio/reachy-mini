"""Bluetooth queries, hostname renaming, app control and telemetry.

Everything here talks to one robot at a time, addressed by IP - except the
Bluetooth helpers, which work when the robot has no usable network at all.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

PORT = 8000

# Windows allows exactly one BLE client operation at a time. Two scans at once
# do not queue - they both fail, one with a TimeoutError and one by returning
# nothing. Every BLE call in this module goes through this lock.
BLE_LOCK = threading.Lock()

# The robot's custom GATT service. Discovered by enumerating a live unit.
BLE_NAME_MATCH = "reachy"
BLE_NETWORK = "12345678-1234-5678-1234-56789abcdef4"   # "CONNECTED [wlan0] <ip>"
BLE_ONLINE = "12345678-1234-5678-1234-56789abcdef5"    # "Online"
BLE_COMMANDS = "12345678-1234-5678-1234-56789abcdef6"  # supported commands
BLE_HWID = "12345678-1234-5678-1234-56789abcdef7"

SSH_USER = "pollen"
SSH_PASS = "root"


# ------------------------------------------------------------------ REST

def api(ip, method, path, body=None, timeout=15):
    url = "http://%s:%d%s" % (ip, PORT, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw.decode("utf-8", "replace")[:800]
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:800]
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)


# ------------------------------------------------------------- Bluetooth

def _ble_helper(args, timeout):
    """Run ble_helper.py as a subprocess and parse its one line of JSON.

    See ble_helper.py for why this is a subprocess and not a function call.
    """
    import os
    import subprocess
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(here, "ble_helper.py")] + [str(a) for a in args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = (p.stdout or "").strip().splitlines()
        if not line:
            return {"ok": False, "error": (p.stderr or "no output")[-300:]}
        return json.loads(line[-1])
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "bluetooth helper timed out"}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def ble_list(timeout=16.0):
    """Which Reachy units are advertising right now."""
    with BLE_LOCK:
        res = _ble_helper(["list", timeout], timeout + 45)
    return res.get("robots", []) if res.get("ok") else []


async def _ble_read(address, timeout=16.0):
    from bleak import BleakClient, BleakScanner
    dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
    if dev is None:
        return None
    async with BleakClient(dev, timeout=40.0,
                           winrt={"use_cached_services": False}) as c:
        def dec(b):
            return b.decode("utf-8", "replace").strip()
        out = {}
        for key, uuid in (("network", BLE_NETWORK), ("online", BLE_ONLINE),
                          ("commands", BLE_COMMANDS), ("hardware_id", BLE_HWID)):
            try:
                out[key] = dec(await c.read_gatt_char(uuid))
            except Exception:
                out[key] = None
        return out


def ble_status(address, tries=3):
    """Read a robot's network state over Bluetooth. Works with no WiFi.

    Advertising is intermittent, so a single miss means nothing - the helper
    retries internally.
    """
    with BLE_LOCK:
        res = _ble_helper(["status", address, 16.0, tries], 40 * tries + 40)
    if res.get("ok"):
        return res.get("status")
    LAST_BLE_ERROR["message"] = res.get("error")
    return None


LAST_BLE_ERROR = {"message": None}


def ip_from_ble(status):
    """Pull an IPv4 out of 'CONNECTED [wlan0] 10.1.2.3'."""
    import ipaddress
    if not status or not status.get("network"):
        return None
    txt = status["network"].replace("[", " ").replace("]", " ")
    for tok in txt.split():
        try:
            ipaddress.ip_address(tok)
            if tok != "0.0.0.0":
                return tok
        except ValueError:
            continue
    return None


def in_hotspot_mode(status):
    return bool(status and (status.get("network") or "").upper().startswith("HOTSPOT"))


# ------------------------------------------------------- rename over SSH
# There is no REST endpoint for the hostname, so this goes in over SSH.

RENAME_SCRIPT = (
    "sudo hostnamectl set-hostname {new} && "
    "sudo sed -i 's/127.0.1.1.*/127.0.1.1\\t{new}/' /etc/hosts && "
    "hostname"
)


def rename_robot(ip, new_name):
    """Set the robot's hostname, so two units stop answering to one name.

    Returns (ok, message). The robot needs a reboot for mDNS to re-advertise.
    """
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}", new_name or ""):
        return False, ("Name must be lowercase letters, digits and hyphens, "
                       "2-31 characters, not starting with a hyphen.")
    try:
        import paramiko
    except ImportError:
        return False, "paramiko is not installed (pip install paramiko)"

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(ip, username=SSH_USER, password=SSH_PASS, timeout=20)
        cmd = "echo %s | sudo -S bash -c \"%s\"" % (
            SSH_PASS,
            RENAME_SCRIPT.format(new=new_name).replace("sudo ", ""))
        _, out, err = c.exec_command(cmd, timeout=45)
        o = out.read().decode("utf-8", "replace").strip()
        e = err.read().decode("utf-8", "replace").strip()
        c.close()
        if new_name in o:
            return True, "Hostname is now %s. Reboot the robot to finish." % new_name
        return False, (e or o or "rename produced no output")[-400:]
    except Exception as ex:
        try:
            c.close()
        except Exception:
            pass
        return False, "%s: %s" % (type(ex).__name__, ex)


def reboot_robot(ip):
    try:
        import paramiko
    except ImportError:
        return False, "paramiko is not installed"
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(ip, username=SSH_USER, password=SSH_PASS, timeout=20)
        c.exec_command("echo %s | sudo -S reboot" % SSH_PASS, timeout=10)
        c.close()
        return True, "Reboot sent. Give it about two minutes."
    except Exception as ex:
        return False, "%s: %s" % (type(ex).__name__, ex)


def get_hostname(ip):
    try:
        import paramiko
    except ImportError:
        return None
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(ip, username=SSH_USER, password=SSH_PASS, timeout=12)
        _, out, _ = c.exec_command("hostname", timeout=12)
        name = out.read().decode("utf-8", "replace").strip()
        c.close()
        return name or None
    except Exception:
        return None


# ------------------------------------------------------------------ apps

def apps_list(ip):
    st, body = api(ip, "GET", "/api/apps/list-available", timeout=25)
    if st != 200:
        return []
    items = body if isinstance(body, list) else (
        body.get("apps") or body.get("available") or [])
    out = []
    for it in items:
        if isinstance(it, str):
            out.append({"name": it, "author": None})
        elif isinstance(it, dict):
            out.append({
                "name": it.get("name") or it.get("app_name") or it.get("id"),
                "author": it.get("author") or it.get("owner"),
            })
    return [a for a in out if a["name"]]


def app_status(ip):
    st, body = api(ip, "GET", "/api/apps/current-app-status", timeout=12)
    return body if st == 200 else None


def app_start(ip, name):
    return api(ip, "POST", "/api/apps/start-app/%s" % urllib.parse.quote(name),
               timeout=60)


def app_stop(ip):
    return api(ip, "POST", "/api/apps/stop-current-app", timeout=40)


# ---------------------------------------------------------- diagnostics

def diagnostics(ip):
    """Everything we can cheaply read about one robot."""
    d = {}
    st, status = api(ip, "GET", "/api/daemon/status", timeout=8)
    if st != 200 or not isinstance(status, dict):
        return {"reachable": False}
    b = status.get("backend_status") or {}
    cl = b.get("control_loop_stats") or {}
    d["reachable"] = True
    d["daemon_state"] = status.get("state")
    d["version"] = status.get("version")
    d["hardware_id"] = status.get("hardware_id")
    d["wireless"] = status.get("wireless_version")
    d["wlan_ip_self_report"] = status.get("wlan_ip")
    d["simulation"] = status.get("simulation_enabled")
    d["motors"] = b.get("motor_control_mode")
    d["backend_ready"] = b.get("ready")
    d["loop_hz"] = round(cl.get("mean_control_loop_frequency", 0), 1) or None
    d["loop_max_interval_ms"] = (round(cl.get("max_control_loop_interval", 0) * 1000, 1)
                                 if cl.get("max_control_loop_interval") else None)
    d["loop_errors"] = cl.get("nb_error")
    d["motor_controller"] = cl.get("motor_controller")
    d["backend_error"] = b.get("error") or status.get("error")
    d["face"] = status.get("face_target")

    st, full = api(ip, "GET", "/api/state/full", timeout=8)
    if st == 200 and isinstance(full, dict):
        hp = full.get("head_pose") or {}
        d["head_pose_raw"] = hp
        d["head_pose_human"] = {
            "x_mm": round(hp.get("x", 0) * 1000, 1),
            "y_mm": round(hp.get("y", 0) * 1000, 1),
            "z_mm": round(hp.get("z", 0) * 1000, 1),
            "roll_deg": round(_deg(hp.get("roll")), 1),
            "pitch_deg": round(_deg(hp.get("pitch")), 1),
            "yaw_deg": round(_deg(hp.get("yaw")), 1),
        } if hp else None
        d["body_yaw_deg"] = (round(_deg(full.get("body_yaw")), 1)
                             if full.get("body_yaw") is not None else None)
        ant = full.get("antennas_position")
        d["antennas_deg"] = [round(_deg(a), 1) for a in ant] if ant else None
        d["control_mode"] = full.get("control_mode")
        d["doa"] = full.get("doa")
        d["timestamp"] = full.get("timestamp")

    st, mot = api(ip, "GET", "/api/motors/status", timeout=8)
    if st == 200 and isinstance(mot, dict):
        d["motor_mode"] = mot.get("mode")

    st, upd = api(ip, "GET", "/update/available", timeout=12)
    if st == 200:
        d["update_available"] = upd

    st, ws = api(ip, "GET", "/wifi/status", timeout=10)
    if st == 200 and isinstance(ws, dict):
        d["wifi_mode"] = ws.get("mode")
        d["wifi_network"] = ws.get("connected_network")
        d["wifi_known"] = ws.get("known_networks")

    # capability probe: older firmware has no media/camera routes at all
    st, _ = api(ip, "GET", "/api/media/status", timeout=6)
    d["has_media_api"] = (st == 200)
    return d


def _deg(rad):
    import math
    return math.degrees(rad) if rad is not None else 0.0
