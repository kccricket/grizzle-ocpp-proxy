#!/usr/bin/env python3
"""Read and change a Grizzl-E charger's OCPP backend settings from the command line.

The charger's web UI (http://<charger-ip>/) gates its "Advanced OCPP Settings" form behind a
client-side password prompt: the page POSTs to /check-password only to decide whether to reveal
the form. The actual save goes to POST /ocpp, which takes no password or auth of its own. This
script talks to GET /info and POST /ocpp directly, the same requests the page itself makes.

Examples:
    # Show the charger's current settings
    configure-charger.py --host 192.168.84.217 show

    # Point the charger at this proxy (ws://<proxy-host>:8321/<station-id>)
    configure-charger.py --host 192.168.84.217 set --proxy ws://192.168.42.3:8321

    # Set fields individually
    configure-charger.py --host 192.168.84.217 set --ocpp-url ws://192.168.42.3:8321/GRS-170000598f4

    # Put the charger back on its factory OCPP backend
    configure-charger.py --host 192.168.84.217 reset
"""

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 8


def get_info(host: str) -> dict:
    with urllib.request.urlopen(f"http://{host}/info", timeout=TIMEOUT) as r:
        return json.load(r)


def post_ocpp(host: str, ocpp_url: str, auth_key: str, station_id: str) -> dict:
    body = urllib.parse.urlencode(
        {"ocppUrl": ocpp_url, "authKey": auth_key, "stationId": station_id}
    ).encode()
    req = urllib.request.Request(
        f"http://{host}/ocpp",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"error: charger rejected the change (HTTP {e.code}): {detail}") from e


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
        ocpp_url = f"{args.proxy.rstrip('/')}/{info['stationId']}"
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

    if args.dry_run:
        print("\n(dry run, nothing sent)")
        return
    if not args.yes and input("\nApply? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return

    post_ocpp(args.host, new["ocppUrl"], new["authKey"], new["stationId"])
    print("\nApplied. Current settings now:")
    print_settings(get_info(args.host))


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
    post_ocpp(args.host, **new)
    print("\nApplied. Current settings now:")
    print_settings(get_info(args.host))


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
    p_set.add_argument("--ocpp-url", help="Full OCPP WebSocket URL, e.g. ws://host:port/station-id")
    p_set.add_argument(
        "--proxy",
        help="Base proxy URL, e.g. ws://192.168.42.3:8321. "
        "The charger's current station ID is appended automatically.",
    )
    p_set.add_argument("--auth-key", help="OCPP auth key / basic auth password")
    p_set.add_argument("--station-id", help="Station ID used in the OCPP URL path")
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

    try:
        args.func(args)
    except urllib.error.URLError as e:
        raise SystemExit(f"error: could not reach charger at {args.host}: {e}") from e


if __name__ == "__main__":
    main()
