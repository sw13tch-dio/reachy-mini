"""First-time WiFi setup for a Reachy Mini, driven from the dashboard.

A brand-new robot boots into access-point mode: it serves its own network
called `reachy-mini-ap` and knows no WiFi. To get it onto your network,
something has to join that AP and hand it credentials. This module does that.

Two things make it fiddly, both learned the hard way:

* **The AP never shows up in a Windows WiFi scan.** Windows generally won't
  return other-band scan results while it is associated to a 5 GHz network,
  and the robot's AP is 2.4 GHz. A *directed* connect against a saved profile
  works fine, so we never rely on the scan.
* **The profile must be added at all-user scope.** `netsh wlan add profile
  ... user=current` fails with "a profile with this name already exists in
  group policy or different user scope" if one already exists. Omitting
  `user=current` works.

And the endpoint itself: `POST /wifi/connect` takes **ssid and password as
query parameters**, not a JSON body. Posting JSON gets you a 422.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

AP_SSID = "reachy-mini-ap"
AP_PASS = "reachy-mini"          # documented default, same on every unit
AP_GATEWAY = "10.42.0.1"
PORT = 8000

PROFILE_XML = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{ssid}</name>
  <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security>
    <authEncryption>
      <authentication>WPA2PSK</authentication>
      <encryption>AES</encryption>
      <useOneX>false</useOneX>
    </authEncryption>
    <sharedKey>
      <keyType>passPhrase</keyType>
      <protected>false</protected>
      <keyMaterial>{key}</keyMaterial>
    </sharedKey>
  </security></MSM>
</WLANProfile>
"""


def _sh(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, str(e)


def current_ssid():
    _, out = _sh(["netsh", "wlan", "show", "interfaces"])
    if not re.search(r"^\s*State\s*:\s*connected", out, re.M):
        return None
    m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
    return m.group(1).strip() if m else None


def ensure_profile(ssid, key):
    """Add a WLAN profile at all-user scope (see module docstring)."""
    path = os.path.join(tempfile.gettempdir(), "reachy_ap_profile.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(PROFILE_XML.format(ssid=ssid, key=key))
    rc, out = _sh(["netsh", "wlan", "add", "profile", "filename=%s" % path])
    try:
        os.remove(path)
    except OSError:
        pass
    return rc == 0, out.strip()


def join(ssid, timeout=45):
    """Directed connect; does not require the SSID to appear in a scan."""
    _sh(["netsh", "wlan", "connect", "name=%s" % ssid, "ssid=%s" % ssid])
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(2)
        if current_ssid() == ssid:
            time.sleep(2)          # let DHCP settle
            return True
    return False


def _api(method, path, host=AP_GATEWAY, timeout=20):
    url = "http://%s:%d%s" % (host, PORT, path)
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw.decode("utf-8", "replace")[:600]
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:600]
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)


def robot_on_ap(retries=12):
    """Wait for the robot's daemon on its own AP."""
    for _ in range(retries):
        st, body = _api("GET", "/api/daemon/status", timeout=5)
        if st == 200:
            return True, body
        time.sleep(3)
    return False, None


def robot_wifi_status():
    return _api("GET", "/wifi/status", timeout=12)


def robot_scan():
    """Ask the robot which networks *it* can see. Returns a list of SSIDs."""
    st, body = _api("POST", "/wifi/scan_and_list", timeout=50)
    names = []
    if st == 200:
        if isinstance(body, list):
            names = [n for n in body if isinstance(n, str) and n.strip()]
        elif isinstance(body, dict):
            for e in (body.get("networks") or body.get("results") or []):
                if isinstance(e, dict) and e.get("ssid"):
                    names.append(e["ssid"])
                elif isinstance(e, str) and e.strip():
                    names.append(e)
    # drop the robot's own AP and dedupe, keep order
    out, seen = [], set()
    for n in names:
        if n != AP_SSID and n not in seen:
            seen.add(n)
            out.append(n)
    return st, out


def send_credentials(ssid, password):
    """POST /wifi/connect - ssid and password are QUERY params, not a body."""
    q = urllib.parse.urlencode({"ssid": ssid, "password": password})
    return _api("POST", "/wifi/connect?" + q, timeout=60)


def robot_error():
    return _api("GET", "/wifi/error", timeout=10)
