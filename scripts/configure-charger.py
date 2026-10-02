#!/usr/bin/env python3
"""Read and change a Grizzl-E charger's OCPP backend settings from the command line.

The charger's web UI (http://<charger-ip>/) gates its "Advanced OCPP Settings" form behind a
client-side password prompt: the page POSTs to /check-password only to decide whether to reveal
the form. The actual save goes to POST /ocpp, which takes no password or auth of its own. This
script talks to GET /info and POST /ocpp directly, the same requests the page itself makes.

Examples:
    # Show the charger's current settings
    configure-charger.py --host 192.168.84.217 show

    # Point the charger at this proxy, keeping the URL path it already uses (e.g. "charger-")
    configure-charger.py --host 192.168.84.217 set --proxy ws://192.168.42.3:8321

    # Set the whole URL yourself
    configure-charger.py --host 192.168.84.217 set --ocpp-url ws://192.168.42.3:8321/charger-

The charger appends its own station ID to the configured OCPP URL, so don't put the ID in the URL.
The URL needs a path part for it to do so, and that path plus the ID becomes the charge point ID
the CSMS sees (here "charger-GRS-170000598f4"). Changing the path therefore makes Home Assistant
treat the charger as a new device.

    # Put the charger back on its factory OCPP backend
    configure-charger.py --host 192.168.84.217 reset
"""

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 8
# Used when the charger's current URL has no path, e.g. the factory "...?station=" form
DEFAULT_PATH = "charger-"
# What the proxy accepts as a charge point ID (the URL path plus the station ID)
PATH_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def get_info(host: str) -> dict:
    with urllib.request.urlopen(f"http://{host}/info", timeout=TIMEOUT) as r:
        return json.load(r)


def encode_body(ocpp_url: str, auth_key: str, station_id: str) -> bytes:
    """Build the request body exactly as the charger's own page does.

    The page's sendReq() runs JSON.stringify() on a payload that is already a urlencoded string, so
    the firmware receives the form wrapped in literal double quotes. Its parser relies on that: sent
    bare, the first character of the URL and the last character of the station ID are dropped.
    """
    form = urllib.parse.urlencode(
        {"ocppUrl": ocpp_url, "authKey": auth_key, "stationId": station_id}
    )
    return json.dumps(form).encode()


def post_ocpp(host: str, ocpp_url: str, auth_key: str, station_id: str) -> dict:
    req = urllib.request.Request(
        f"http://{host}/ocpp",
        data=encode_body(ocpp_url, auth_key, station_id),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"error: charger rejected the change (HTTP {e.code}): {detail}") from e


def apply_and_verify(host: str, new: dict, timeout: float = 30, interval: float = 1):
    """POST the settings, then wait for the charger to report them and fail if it never does.

    The charger applies a write asynchronously: /info keeps returning the old values for several
    seconds afterwards, so a single read-back right after the POST isn't meaningful.
    """
    post_ocpp(host, new["ocppUrl"], new["authKey"], new["stationId"])
    print("\nSent. Waiting for the charger to apply it", end="", flush=True)
    deadline = time.monotonic() + timeout
    while True:
        info = get_info(host)
        wrong = {k: (v, info[k]) for k, v in new.items() if info[k] != v}
        if not wrong:
            break
        if time.monotonic() >= deadline:
            lines = [
                f"  {k}: sent {sent!r}, charger has {got!r}" for k, (sent, got) in wrong.items()
            ]
            raise SystemExit(
                f"\nerror: the charger did not report the new settings within {timeout:.0f}s:\n"
                + "\n".join(lines)
            )
        print(".", end="", flush=True)
        time.sleep(interval)
    print(" done. Current settings now:")
    print_settings(info)


def proxy_url(proxy: str, current_url: str, path: str | None) -> str:
    """Point `current_url` at `proxy`, keeping its path (or using `path`) so the charge point ID
    the CSMS sees doesn't change."""
    target = urllib.parse.urlsplit(proxy)
    if target.scheme not in ("ws", "wss") or not target.netloc:
        raise SystemExit(f"error: --proxy must look like ws://host:port, got {proxy!r}")
    if path is None:
        path = urllib.parse.urlsplit(current_url).path.strip("/") or DEFAULT_PATH
    path = path.strip("/")
    if not PATH_PATTERN.match(path):
        raise SystemExit(
            f"error: URL path {path!r} must contain only letters, digits, '-' and '_' "
            "for the proxy to accept it as a charger ID"
        )
    return f"{target.scheme}://{target.netloc}/{path}"


def print_settings(info: dict):
    print(f"  Station ID : {info['stationId']} (serial: {info['serialNumber']})")
    print(f"  OCPP URL   : {info['ocppUrl']}")
    print(f"  Auth key   : {info['authKey']}")
    print(f"  Default URL: {info['defaultOcppUrl']}")
    print(f"  Online     : {info['isOnline']}")


def cmd_show(args):
    print_settings(get_info(args.host))


def cmd_set(args):
    info = get_info(args.host)
    ocpp_url = args.ocpp_url
    if args.proxy:
        ocpp_url = proxy_url(args.proxy, info["ocppUrl"], args.path)
    new = {
        "ocppUrl": ocpp_url if ocpp_url is not None else info["ocppUrl"],
        "authKey": args.auth_key if args.auth_key is not None else info["authKey"],
        "stationId": args.station_id if args.station_id is not None else info["stationId"],
    }
    if new == {k: info[k] for k in new}:
        print("Nothing to change.")
        return

    print("Current:")
    print_settings(info)
    print("\nNew:")
    print(f"  Station ID : {new['stationId']}")
    print(f"  OCPP URL   : {new['ocppUrl']}")
    print(f"  Auth key   : {new['authKey']}")
    print(f"\nThe charger will connect to {new['ocppUrl']}{new['stationId']}")
    if urllib.parse.urlsplit(new["ocppUrl"]).path.strip("/") == "":
        print("warning: the URL has no path part; the charger may not append its station ID to it")

    if args.dry_run:
        print("\n(dry run, nothing sent)")
        return
    if not args.yes and input("\nApply? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return

    apply_and_verify(args.host, new)


def cmd_reset(args):
    info = get_info(args.host)
    new = {
        "ocppUrl": info["defaultOcppUrl"],
        "authKey": info["defaultAuthKey"],
        "stationId": info["serialNumber"],
    }
    print("Resetting to factory OCPP backend:")
    print(f"  OCPP URL : {new['ocppUrl']}")
    print(f"  Auth key : {new['authKey']}")
    if args.dry_run:
        print("\n(dry run, nothing sent)")
        return
    if not args.yes and input("\nApply? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return
    apply_and_verify(args.host, new)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--host", required=True, help="Charger's IP address or hostname")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show", help="Print the charger's current OCPP settings").set_defaults(
        func=cmd_show
    )

    p_set = sub.add_parser("set", help="Change one or more OCPP settings")
    p_set.add_argument(
        "--ocpp-url",
        help="Full OCPP WebSocket URL without the station ID, e.g. ws://host:port/charger-",
    )
    p_set.add_argument(
        "--proxy",
        help="Proxy address, e.g. ws://192.168.42.3:8321. Replaces the host in the charger's "
        "current OCPP URL and keeps its path (see --path).",
    )
    p_set.add_argument(
        "--path",
        help="With --proxy: URL path to use instead of the current one (default: keep it). "
        "Changing it changes the charge point ID the CSMS sees.",
    )
    p_set.add_argument("--auth-key", help="OCPP auth key / basic auth password")
    p_set.add_argument("--station-id", help="Station ID the charger appends to the OCPP URL")
    p_set.add_argument("--yes", action="store_true", help="Don't prompt for confirmation")
    p_set.add_argument(
        "--dry-run", action="store_true", help="Show what would change, but don't send it"
    )
    p_set.set_defaults(func=cmd_set)

    p_reset = sub.add_parser(
        "reset", help="Restore the factory OCPP backend (United Chargers' cloud)"
    )
    p_reset.add_argument("--yes", action="store_true", help="Don't prompt for confirmation")
    p_reset.add_argument(
        "--dry-run", action="store_true", help="Show what would change, but don't send it"
    )
    p_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args()
    if args.command == "set" and not any(
        [args.ocpp_url, args.proxy, args.auth_key, args.station_id]
    ):
        parser.error("set requires at least one of --ocpp-url, --proxy, --auth-key, --station-id")
    if args.command == "set" and args.ocpp_url and args.proxy:
        parser.error("--ocpp-url and --proxy are mutually exclusive")
    if args.command == "set" and args.path and not args.proxy:
        parser.error("--path only applies with --proxy")

    try:
        args.func(args)
    except urllib.error.URLError as e:
        raise SystemExit(f"error: could not reach charger at {args.host}: {e}") from e


if __name__ == "__main__":
    main()
