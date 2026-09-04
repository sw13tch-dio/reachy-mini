# Reachy Mini Dashboard

A dead-simple way to connect to a **Reachy Mini Wireless** and check that it works
— built for people who have never opened a terminal.

![status](https://img.shields.io/badge/robot-Reachy%20Mini%20Wireless-blue)

---

## Quick start (Windows)

1. Download this folder.
2. Double-click **`Start Reachy Dashboard.bat`**.
3. Your browser opens. Wait for the dot to turn green.
4. Press **“Say hello”**. If the robot moves, everything works.

That's it. There is no step 5.

> First launch installs one small add-on (`bleak`) automatically and may take
> an extra minute. After that it starts in seconds.

**Requires:** Python 3.8+ with *“Add Python to PATH”* ticked during install.
Get it at <https://www.python.org/downloads/>.

---

## The problem this solves

The official **Reachy Mini Control** app finds the robot using mDNS — the
`reachy-mini.local` name. Campus, lab, conference and hotel networks block mDNS
multicast, so the app's *“finding robot”* screen spins forever **even when the
robot is online, healthy, and perfectly reachable**.

That is a documented limitation, not a broken robot:

> *"A wireless unit advertises itself as `reachy-mini.local` via mDNS. This works
> on most home and office networks, but may fail on some enterprise, conference,
> or hotel networks."*
> — [Reachy Mini troubleshooting docs](https://huggingface.co/docs/reachy_mini/en/troubleshooting)

This dashboard doesn't rely on mDNS. It finds the robot **four ways**, in order:

| # | Method | Notes |
|---|--------|-------|
| 1 | Cached address | Instant on repeat runs |
| 2 | mDNS (`reachy-mini.local`) | Works on simple home networks |
| 3 | **Bluetooth LE** | Asks the robot directly what its IP is — works when the network hides it |
| 4 | Subnet sweep | Brute-forces port 8000 across your local `/24`, then `/18` |

Method 3 is the one that saves you. The robot broadcasts its own Wi-Fi address
over Bluetooth, so even on a network that blocks all discovery, we can just
**ask it where it is**.

---

## What it shows

- **Address** — where the robot actually is
- **Daemon** — is the robot's software running
- **Motors** — on or off *(see the gotcha below)*
- **Speed** — motor control loop rate; should be ~50 Hz
- **Errors** — should be 0
- **Version** — robot software version

## What it does

| Button | Effect |
|---|---|
| **Say hello** | Motors on → wake up → head lifts → antennas wiggle. The test. |
| Wake up | Motors on, head to neutral |
| Wiggle antennas | Just the antennas |
| Go to sleep | Parks the head down, motors off |
| Let go (limp) | Motors off — move the head by hand |

---

## Making the official app work too

You don't have to choose. **Reachy Mini Control works fine on campus Wi-Fi — you
just have to tell it where the robot is**, because its auto-detect relies on the
mDNS that the network blocks.

The dashboard does the handoff for you. Once it's connected, a card appears with
the robot's address and an **"Open the app & copy the address"** button. Press it
and the address is on your clipboard with the app already opening. Then:

1. Ignore the three tiles — *Reachy WiFi* will show a red dot and say
   **"No robot detected"**. That is the mDNS failure, not a broken robot.
2. Click the **"Connect by IP address…"** box at the bottom and paste
   (<kbd>Ctrl</kbd>+<kbd>V</kbd>).
3. The greyed-out **Start** button lights up. Press it.

⚠️ **The app does not remember the address.** We checked — nothing is written to
its config or local storage. You have to paste it in *every single time*. So the
habit to teach is: **dashboard first, app second.** The address will always be
sitting there waiting for you.

Use whichever fits the moment: this dashboard to *find* the robot and prove it
moves, the official app for the things it does better — camera preview, speaker
and microphone, installing and launching apps, Hugging Face login, and system
updates.

---

## The three gotchas that cost us a semester

**1. Motors default to `disabled`, and that failure is silent.**
With torque off, the daemon still accepts every move command, returns HTTP 200,
and keeps its control loop running at 50 Hz with no errors. Nothing anywhere
says the robot isn't moving. Always press **Wake up** first.

**2. `reachy-mini.local` will not resolve on most institutional Wi-Fi.**
This is the mDNS problem above. It is not a fault — the robot is fine.

**3. Reachy Mini Control holds the Bluetooth radio.**
If that app is open, this dashboard's Bluetooth discovery will fail, and so will
the web Bluetooth console. Close it first. (It also polls
`netsh wlan show networks` every ~5 seconds, which flashes a console window on
screen the whole time it's running.)

---

## If there is no Wi-Fi at all

The robot can create **its own network** — SSID `reachy-mini-ap`. This works
anywhere, with no router, no campus, no infrastructure. It is stored on the robot
as a NetworkManager profile with `autoconnect: no`, so it only comes up when you
ask for it, over Bluetooth:

1. Install **nRF Connect** ([Android](https://play.google.com/store/apps/details?id=no.nordicsemi.android.mcp) /
   [iOS](https://apps.apple.com/app/nrf-connect-for-mobile/id1054362403)) — or use the
   [web Bluetooth tool](https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_reset/).
2. Connect to **ReachyMini**.
3. In the writable characteristic, send your PIN first: `PIN_xxxxx`
   — the last 5 digits of the robot's serial number, printed on the base.
4. Then send `CMD_HOTSPOT`.
5. Join `reachy-mini-ap` from your laptop, then run this dashboard.

⚠️ **Only do this when you have no other option.** `CMD_HOTSPOT` switches `wlan0`
out of client mode, which drops the robot off whatever network it's on — including
the one you're currently talking to it over.

💡 A cheap **USB Wi-Fi dongle** lets your laptop sit on `reachy-mini-ap` *and*
normal Wi-Fi at the same time, so you keep internet while working with the robot.

---

## Command line

```bash
python reachy_dash.py                    # find the robot, open the dashboard
python reachy_dash.py --ip 10.1.221.118  # skip discovery
python reachy_dash.py --port 8800        # different dashboard port
python reachy_dash.py --no-browser       # don't auto-open
```

## Gestures

`gestures.py` is a small vocabulary of named, clamped motions — usable on its
own or as a building block:

```bash
python gestures.py 10.1.221.118 nod      # also: shake tilt perk droop wiggle
```

```python
from gestures import enable_motors, nod, perk, rest
enable_motors(ip)
try:
    perk(ip); nod(ip, times=3)
finally:
    rest(ip)          # always return to rest
```

It speaks **millimetres and degrees**, clamps every value against the published
joint limits before sending, and converts to the daemon's metres/radians in one
place. See [`HARDWARE.md`](HARDWARE.md) for the limits and why they matter.

## Direct SSH to the robot

```bash
ssh pollen@<robot-ip>     # password: root
reachyminios_check        # hardware self-test
```

Note: `reachyminios_check` reports false errors for the camera, audio and motors
while the daemon is running — the daemon holds those devices. Stop the daemon
first if you want a clean read.

---

## Under the hood

Everything goes through the daemon's REST API on port 8000. Full interactive API
docs are served by the robot itself at `http://<robot-ip>:8000/docs`.

Units are **meters and radians** — not degrees. Head `z=0` is neutral;
`z=-0.05` is the slumped sleep position. Antennas are parked at `0.17` rad
(~10°) rather than `0`, because at exactly vertical the gearbox backlash puts
them in unstable equilibrium and they buzz.

Safety limits (the robot clamps to these automatically):

- Body yaw: ±180°
- Head pitch / roll: ±40°
- Head yaw: ±180°
- `body_yaw` and `head_yaw` must stay within 65° of each other

---

## License

MIT — do what you like with it.
