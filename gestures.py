"""Joint limits and a small vocabulary of named gestures.

Two rules carried over from the earlier Link Lab prototype, both of which
prevent real classes of bug:

1. **Human units at the boundary.** This module speaks millimetres and
   degrees. Conversion to the daemon's metres and radians happens in exactly
   one place (:func:`pose`). Unit confusion is the most common source of
   motion bugs on this robot.
2. **Clamp before sending.** The daemon clamps too, but clamping on our side
   means our code never silently commands something different from what it
   asked for.

Unlike the prototype this talks to the daemon's REST API directly, so it needs
no `reachy_mini` SDK install - just the standard library.

    from gestures import nod, shake, perk, rest
    nod("10.1.221.118")
"""

from __future__ import annotations

import json
import math
import time
import urllib.request
from dataclasses import dataclass

ROBOT_PORT = 8000

# --------------------------------------------------------------------- limits
# Degrees. Mirrors the limits published in the Reachy Mini SDK docs.
# Re-check these when bumping the robot's software.
HEAD_PITCH_RANGE = (-40.0, 40.0)
HEAD_ROLL_RANGE = (-40.0, 40.0)
HEAD_YAW_RANGE = (-180.0, 180.0)
BODY_YAW_RANGE = (-160.0, 160.0)

#: Max allowed difference between head yaw and body yaw, in degrees.
MAX_YAW_DELTA = 65.0

#: Antenna travel we consider expressive-but-safe, in degrees.
ANTENNA_RANGE = (-90.0, 90.0)

#: Head translation we consider safe, in millimetres.
HEAD_XYZ_RANGE_MM = (-25.0, 25.0)

#: Antennas rest a few degrees off vertical on purpose. At exactly 0 the
#: gearbox backlash puts them in unstable equilibrium and they buzz.
ANTENNA_PARK_DEG = 10.0

#: Interpolation methods the daemon accepts.
METHODS = ("linear", "minjerk", "ease_in_out", "cartoon")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class HeadPose:
    """A head pose in human units: millimetres and degrees."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0

    def clamped(self) -> "HeadPose":
        lo, hi = HEAD_XYZ_RANGE_MM
        return HeadPose(
            x=clamp(self.x, lo, hi),
            y=clamp(self.y, lo, hi),
            z=clamp(self.z, lo, hi),
            roll=clamp(self.roll, *HEAD_ROLL_RANGE),
            pitch=clamp(self.pitch, *HEAD_PITCH_RANGE),
            yaw=clamp(self.yaw, *HEAD_YAW_RANGE),
        )


def clamp_yaw_pair(head_yaw: float, body_yaw: float) -> tuple[float, float]:
    """Keep ``head_yaw - body_yaw`` within :data:`MAX_YAW_DELTA`.

    The body is the heavier, slower axis, so hold the body where it was asked
    to go and pull the head back toward it.
    """
    body = clamp(body_yaw, *BODY_YAW_RANGE)
    head = clamp(head_yaw, *HEAD_YAW_RANGE)
    delta = head - body
    if abs(delta) > MAX_YAW_DELTA:
        head = body + (MAX_YAW_DELTA if delta > 0 else -MAX_YAW_DELTA)
    return head, body


def clamp_antennas(right: float, left: float) -> tuple[float, float]:
    return clamp(right, *ANTENNA_RANGE), clamp(left, *ANTENNA_RANGE)


REST = HeadPose()


# --------------------------------------------------------------- the one place
# Everything below converts human units to the daemon's metres/radians here,
# and nowhere else.

def _post(ip: str, path: str, body=None, timeout: float = 15.0):
    url = "http://%s:%d%s" % (ip, ROBOT_PORT, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        try:
            return json.loads(raw)
        except Exception:
            return {}


def pose(ip, head=None, *, antennas_deg=None, body_yaw_deg=None,
         duration=1.0, method="minjerk", wait=True):
    """Send one clamped move, in human units (mm and degrees).

    ``antennas_deg`` is ``(right, left)``.
    """
    if method not in METHODS:
        raise ValueError("method must be one of %s, got %r" % (METHODS, method))
    if duration <= 0:
        raise ValueError("duration must be positive, got %r" % duration)

    payload = {"duration": duration, "interpolation": method}
    body = body_yaw_deg

    if head is not None:
        h = head.clamped()
        if body is not None:
            head_yaw, body = clamp_yaw_pair(h.yaw, body)
            h = HeadPose(h.x, h.y, h.z, h.roll, h.pitch, head_yaw)
        payload["head_pose"] = {
            "x": h.x / 1000.0,           # mm  -> m
            "y": h.y / 1000.0,
            "z": h.z / 1000.0,
            "roll": math.radians(h.roll),   # deg -> rad
            "pitch": math.radians(h.pitch),
            "yaw": math.radians(h.yaw),
        }

    if antennas_deg is not None:
        right, left = clamp_antennas(*antennas_deg)
        payload["antennas"] = [math.radians(right), math.radians(left)]

    if body is not None:
        payload["body_yaw"] = math.radians(clamp(body, *BODY_YAW_RANGE))

    _post(ip, "/api/move/goto", payload)
    if wait:
        time.sleep(duration + 0.05)


def enable_motors(ip):
    """Motors on. Without this the daemon accepts moves and ignores them."""
    _post(ip, "/api/motors/set_mode/enabled")
    time.sleep(0.8)


def rest(ip, duration=1.0):
    """Return to neutral. Call this in every ``finally`` block."""
    pose(ip, REST, antennas_deg=(ANTENNA_PARK_DEG, ANTENNA_PARK_DEG),
         body_yaw_deg=0.0, duration=duration)


# ------------------------------------------------------------------- gestures
# Deliberately primitives, not behaviours.

def nod(ip, times=2, depth=14.0, speed=0.35):
    """Vertical head nod. Reads as agreement."""
    for _ in range(times):
        pose(ip, HeadPose(pitch=depth), duration=speed, method="linear")
        pose(ip, HeadPose(pitch=-depth * 0.4), duration=speed, method="linear")
    pose(ip, REST, duration=speed)


def shake(ip, times=2, extent=22.0, speed=0.35):
    """Horizontal head shake. Reads as disagreement."""
    for _ in range(times):
        pose(ip, HeadPose(yaw=extent), duration=speed, method="linear")
        pose(ip, HeadPose(yaw=-extent), duration=speed, method="linear")
    pose(ip, REST, duration=speed)


def tilt(ip, degrees=18.0, duration=0.6):
    """Head cocked to one side. Reads as doubt or consideration."""
    pose(ip, HeadPose(roll=degrees), duration=duration)


def perk(ip, degrees=55.0, duration=0.3):
    """Antennas up and out. Reads as attention or excitement."""
    pose(ip, antennas_deg=(degrees, degrees), duration=duration)


def droop(ip, degrees=-45.0, duration=0.5):
    """Antennas down. Reads as deflation or deference."""
    pose(ip, antennas_deg=(degrees, degrees), duration=duration)


def wiggle(ip, times=5, extent=40.0, speed=0.22):
    """Antennas flapping side to side. Reads as delight."""
    for _ in range(times):
        pose(ip, antennas_deg=(extent, -extent), duration=speed, method="linear")
        pose(ip, antennas_deg=(-extent, extent), duration=speed, method="linear")
    pose(ip, antennas_deg=(ANTENNA_PARK_DEG, ANTENNA_PARK_DEG), duration=0.5)


def gaze(ip, yaw_deg, pitch_deg=0.0, duration=0.8):
    """Turn to look in a direction.

    Large turns recruit the body so the head never hits the yaw-delta limit
    and silently truncates the turn.
    """
    if abs(yaw_deg) <= 45.0:
        pose(ip, HeadPose(yaw=yaw_deg, pitch=pitch_deg), duration=duration)
        return
    body_share = yaw_deg - (45.0 if yaw_deg > 0 else -45.0)
    pose(ip, HeadPose(yaw=yaw_deg, pitch=pitch_deg),
         body_yaw_deg=body_share, duration=duration)


GESTURES = {
    "nod": nod, "shake": shake, "tilt": tilt,
    "perk": perk, "droop": droop, "wiggle": wiggle,
}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        raise SystemExit("usage: python gestures.py <robot-ip> <%s>"
                         % "|".join(GESTURES))
    ip, name = sys.argv[1], sys.argv[2]
    if name not in GESTURES:
        raise SystemExit("unknown gesture %r (have: %s)"
                         % (name, ", ".join(GESTURES)))
    enable_motors(ip)
    try:
        GESTURES[name](ip)
    finally:
        rest(ip)
