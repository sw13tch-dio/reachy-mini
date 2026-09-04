# Hardware and networking reference

Facts about the robot, kept next to the code that depends on them.

## The robot

Reachy Mini **Wireless** — onboard Raspberry Pi CM4, battery, WiFi, IMU.
The daemon runs *on the robot*, so your laptop talks to it over the network.
There is no USB data port; the USB-C connector is power only. (The **Lite**
variant is the one that tethers to a laptop over USB.)

| Part | DOF | Joint names |
|---|---|---|
| Head | 6 (x, y, z, roll, pitch, yaw) | Stewart platform, `stewart_1`–`stewart_6` |
| Body | 1 (yaw) | `body_rotation` |
| Antennas | 2 | `right_antenna`, `left_antenna` — also usable as buttons |

Assembly is 2–3 hours per kit.

SSH: `ssh pollen@<robot-ip>` — password `root`. Change it.
Self-test on the robot: `reachyminios_check`.

## Units

The daemon speaks **metres and radians**. `gestures.py` speaks **millimetres and
degrees** and converts in exactly one place (`gestures.pose`). Unit confusion is
the most common source of motion bugs here — keep the conversion in one place.

Head `z = 0` is neutral. `z ≈ -0.05 m` is the slumped sleep position.

## Joint limits

Mirrored in `gestures.py`. Re-verify against upstream when the robot's software
is updated. The daemon clamps too, but we clamp first so our code never silently
commands something different from what it asked for.

| Joint | Range |
|---|---|
| Head pitch / roll | −40° … +40° |
| Head yaw | −180° … +180° |
| Body yaw | −160° … +160° |
| Head yaw − body yaw | max 65° apart |
| Head x / y / z | ±25 mm (conservative) |
| Antennas | ±90° (expressive-but-safe) |

Gentle collisions with the body are safe. `gestures.gaze()` recruits the body
automatically for turns beyond 45°, so the yaw-delta limit is never the thing
that silently truncates a turn.

**Antennas park at ~10°, not 0°.** At exactly vertical the gearbox backlash puts
them in unstable equilibrium — the motor hunts around a point with almost no
friction and they visibly buzz. A few degrees of offset lets gravity take up the
play in one direction.

## More than one robot on the same network

Every wireless Reachy Mini advertises itself as **`reachy-mini.local`**. That is
the default on every unit out of the box. Put two or more on the same WiFi and
mDNS resolution becomes a coin flip: the name will resolve, but you cannot
predict *which robot* answers.

Two ways out, in order of preference:

1. **Static DHCP reservations (preferred).** Get each robot's WiFi MAC and have
   whoever runs the network pin each to a fixed IP. Then use IPs, not hostnames.
   No mDNS, no ambiguity — and on enterprise WiFi this is likely the only
   reliable option anyway.
2. **Rename each robot.** SSH in and give each a distinct hostname, then address
   them as `reachy-mini-a.local`. Cleaner to read, still dependent on mDNS.

   ```bash
   ssh pollen@reachy-mini
   sudo hostnamectl set-hostname reachy-mini-a
   sudo reboot
   ```

If the network blocks client-to-client traffic, neither helps — the fallback is
a dedicated travel router for the robots and the laptop. Settle that *before*
there is an audience.

## Known failure modes

| Symptom | Likely cause |
|---|---|
| Cannot resolve `reachy-mini.local` | mDNS blocked — normal on campus WiFi. Use the IP. |
| Wrong robot responds | Two units both answering as `reachy-mini.local` |
| Resolves but connection refused | Still booting, or the daemon crashed |
| Session rejected / robot busy | Another app holds it — close Reachy Mini Control |
| Motion accepted but nothing moves | **Motors disabled.** The daemon returns 200 and keeps its 50 Hz loop with no errors. Enable motors first. |
| Bluetooth discovery fails | Reachy Mini Control is open and holding the radio |
| Antennas buzzing at rest | Parked at exactly 0° — offset them ~10° |

## Diagnostics

```bash
ping <robot-ip>
curl http://<robot-ip>:8000/api/daemon/status
```

Open `http://<robot-ip>:8000/docs` in a browser for the daemon's interactive API
documentation. If that page loads, the robot is healthy and the problem is on
your side.

A healthy robot reports `state: running`, a control loop near **50 Hz**, and
`nb_error: 0`.
