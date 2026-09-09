"""Bluetooth work, run as its own process. Prints one JSON object.

Why a subprocess instead of a function call
-------------------------------------------
bleak's Windows backend is WinRT, and WinRT calls need the calling thread to
be in a suitable COM apartment. Python's main thread gets one; threads created
by `threading.Thread` - which is where a web server's request handlers and any
background worker live - do not reliably. The symptom is nasty: scanning still
finds devices, but every GATT connect times out after ~40 seconds with no
useful error. It looks exactly like radio contention, so it is easy to chase
the wrong bug for a while.

Running the work in a fresh process puts it back on a real main thread, which
also guarantees only one BLE client at a time as long as the caller does not
run two of these at once.

    python ble_helper.py list
    python ble_helper.py status AA:BB:CC:DD:EE:FF
"""

import asyncio
import json
import sys

NAME_MATCH = "reachy"
NETWORK = "12345678-1234-5678-1234-56789abcdef4"
ONLINE = "12345678-1234-5678-1234-56789abcdef5"
COMMANDS = "12345678-1234-5678-1234-56789abcdef6"
HWID = "12345678-1234-5678-1234-56789abcdef7"


async def do_list(timeout):
    from bleak import BleakScanner
    devs = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for addr, (d, adv) in devs.items():
        name = d.name or adv.local_name or ""
        if NAME_MATCH in name.lower():
            out.append({"address": addr, "name": name, "rssi": adv.rssi})
    out.sort(key=lambda r: -(r["rssi"] if r["rssi"] is not None else -999))
    return {"ok": True, "robots": out}


async def do_status(address, timeout, tries):
    from bleak import BleakClient, BleakScanner
    last = None
    for attempt in range(tries):
        try:
            dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
            if dev is None:
                last = "not advertising"
                await asyncio.sleep(1.5)
                continue
            async with BleakClient(dev, timeout=40.0,
                                   winrt={"use_cached_services": False}) as c:
                res = {}
                for key, uuid in (("network", NETWORK), ("online", ONLINE),
                                  ("commands", COMMANDS), ("hardware_id", HWID)):
                    try:
                        raw = await c.read_gatt_char(uuid)
                        res[key] = raw.decode("utf-8", "replace").strip()
                    except Exception:
                        res[key] = None
                if res.get("network"):
                    return {"ok": True, "status": res}
                last = "connected but read nothing"
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        await asyncio.sleep(1.5)
    return {"ok": False, "error": last or "unknown"}


def main():
    argv = sys.argv[1:]
    if not argv:
        print(json.dumps({"ok": False, "error": "no command"}))
        return 2
    cmd = argv[0]
    try:
        if cmd == "list":
            timeout = float(argv[1]) if len(argv) > 1 else 14.0
            res = asyncio.run(do_list(timeout))
        elif cmd == "status":
            if len(argv) < 2:
                res = {"ok": False, "error": "status needs an address"}
            else:
                timeout = float(argv[2]) if len(argv) > 2 else 16.0
                tries = int(argv[3]) if len(argv) > 3 else 3
                res = asyncio.run(do_status(argv[1], timeout, tries))
        else:
            res = {"ok": False, "error": "unknown command %r" % cmd}
    except Exception as e:
        res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
