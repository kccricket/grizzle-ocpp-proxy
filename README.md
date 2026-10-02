# Grizzl-E OCPP Proxy

[![Add this repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fkccricket%2Fgrizzle-ocpp-proxy)

A small proxy that sits between a **Grizzl-E** EV charger and your OCPP server (typically the
[Home Assistant OCPP integration](https://github.com/lbbrhzn/ocpp)) and repairs the frames the
charger firmware gets wrong. It runs as a Home Assistant app (add-on) or as a plain Docker container.

## The problem

The Grizzl-E speaks OCPP 1.6, but not always correctly. The visible symptoms in Home Assistant:

- `Unable to parse message ... "configurationKey":],"unknownKey":[...]` errors in the log.
- `Waited 10s for response on ... GetConfiguration` timeouts every time the charger connects, because
  the reply was invalid JSON and was never delivered.
- The charger's setup sequence retrying over and over, and the integration occasionally seeming
  stuck.

## What it repairs

| Charger behavior | What the proxy does |
|---|---|
| Replies to `GetConfiguration` for an unknown key with `"configurationKey":]` (missing `[`), which is not valid JSON | Rewrites it to `"configurationKey":[]` so the OCPP library accepts the reply |
| Sends frames that still can't be parsed | Logs and drops them instead of taking the connection down |
| Reconnects without closing the old connection | Replaces the stale connection for that charger ID |
| Goes quiet for long periods (heartbeat interval is 3600 s) | Pings it before giving up, so a healthy idle charger is never dropped |

Everything else passes through untouched, including the charger's `Authorization` header.

## Install as a Home Assistant app

1. Click the badge above, or in Home Assistant go to **Settings → Apps → App store → ⋮ →
   Repositories** and add `https://github.com/kccricket/grizzle-ocpp-proxy`.
2. Install **Grizzl-E OCPP Proxy**, review its configuration, and start it.
3. In the charger's settings, set its OCPP server URL to `ws://<your-home-assistant-ip>:8321/charger-`.
   The charger appends its own station ID to this URL, so don't type it yourself, but the URL does
   need a path. Whatever path you use becomes part of the charge point ID Home Assistant sees
   (`charger-GRS-…` here), so keep the one you already have to avoid creating a new device.
4. Leave the Home Assistant OCPP integration as it is, listening on port 9000.

The app log shows `Applied Grizzl-E configurationKey repair` whenever it fixes a frame. See the
app's **Documentation** tab for the options.

## Run with Docker

```
docker run -d --name grizzle-ocpp-proxy -p 8321:8321 \
  -e CSMS_URL=ws://homeassistant.local:9000 \
  ghcr.io/kccricket/grizzle-ocpp-proxy:latest
```

or use [`compose.yaml`](compose.yaml). Images are published for `amd64` and `aarch64`.

### Configuration

Set environment variables (in Home Assistant, the same settings are the app's options):

| Variable | Default | Description |
|---|---|---|
| `CSMS_URL` | *(required)* | The OCPP server to forward to, e.g. `ws://homeassistant.local:9000`. The path the charger connected with (such as `charger-GRS-…`) is appended. |
| `LISTEN_HOST` / `LISTEN_PORT` | `0.0.0.0` / `8321` | Where the proxy listens for the charger. |
| `WATCHDOG_STALE` | `300` | Seconds without an OCPP message before the proxy pings the charger. |
| `WATCHDOG_INTERVAL` | `30` | How often idleness is checked, in seconds. |
| `PING_TIMEOUT` | `60` | Seconds to wait for a pong. |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error`. `debug` logs every frame. |

## Pointing the charger at the proxy

The Grizzl-E's OCPP backend is set from its own web UI at `http://<charger-ip>/`, under
**Advanced OCPP Settings**. That form is gated by a password prompt, but it's checked
client-side only: the actual save (`POST /ocpp`) takes no password or auth of its own. So you can
also script it with [`scripts/configure-charger.py`](scripts/configure-charger.py):

```
python3 scripts/configure-charger.py --host <charger-ip> show
python3 scripts/configure-charger.py --host <charger-ip> set --proxy ws://<proxy-host>:8321
python3 scripts/configure-charger.py --host <charger-ip> reset   # back to the factory backend
```

`set --proxy` swaps in the proxy's address and keeps the path the charger's URL already has
(`charger-`), because the charger appends its station ID to that path and the result is the charge
point ID Home Assistant knows it by. Use `--path` to choose a different path (that creates a new
device in Home Assistant), `--dry-run` to preview a change, and `--ocpp-url`/`--auth-key`/`--station-id`
to set fields individually. The path may only contain letters, digits, `-` and `_`.

## Troubleshooting

To see the frames Home Assistant itself sends and receives, set the OCPP loggers in `configuration.yaml`:

```yaml
logger:
  logs:
    ocpp: info
    custom_components.ocpp: debug
```

## Development

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Tests run a mock charger against the proxy and a mock server, so no hardware is needed.
To release, bump `version` in `grizzle-ocpp-proxy/config.yaml` and `__version__` in `proxy.py`,
add a `CHANGELOG.md` entry, and push a matching `vX.Y.Z` tag; CI builds and publishes the images.

## Credits

This project is a fork of [ocpp-2w-proxy](https://github.com/ocpp-balanz/ocpp-2w-proxy) by
Jens Vedel Markussen, a two-way OCPP proxy. Thank you for the websocket proxy skeleton.
This version drops the second-server feature and focuses on repairing the Grizzl-E.

Not affiliated with United Chargers. Grizzl-E is their product and trademark.

## License

[MIT](LICENSE). Copyright (c) 2025 Jens Vedel Markussen, (c) 2026 Keith Constable.
