# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A fix-up proxy for the Grizzl-E EV charger (OCPP 1.6): one Python file, `grizzle-ocpp-proxy/proxy.py`, that sits between the charger and a single CSMS (normally the Home Assistant OCPP integration) and repairs frames the charger firmware gets wrong. It is a fork of `ocpp-balanz/ocpp-2w-proxy`; the upstream two-way (secondary server) feature was removed on purpose, so don't reintroduce it.

It ships two ways from the same image: a Home Assistant app (`grizzle-ocpp-proxy/config.yaml` is the app manifest) and a plain Docker image on GHCR.

## Commands

```
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest                                   # whole suite (about 5 s)
.venv/bin/pytest tests/test_proxy.py -k watchdog   # one test or group
.venv/bin/ruff check . && .venv/bin/ruff format --check .
CSMS_URL=ws://localhost:9000 .venv/bin/python grizzle-ocpp-proxy/proxy.py
podman build -t grizzle-test grizzle-ocpp-proxy    # docker works too
```

Tests import `proxy` via `pythonpath` in `pyproject.toml` and start real mock servers on ephemeral ports (`serve(settings)` with `listen_port=0`). When changing proxy behavior, check a new test fails against the old behavior.

## Architecture

`Settings.load()` reads each setting from the upper-cased environment variable, then the HA Supervisor's `/data/options.json` (so one image serves HA and standalone Docker), then a default. `listen_host`/`listen_port` are env-only because the app's port mapping fixes them.

`on_connect` takes the charger ID from the URL path and creates one `OCPPProxy` per charger; a new connection for the same ID closes the old one first, because the charger reconnects without closing its stale socket. Each proxy opens a client connection to `csms_url/<charger_id>`, forwarding the charger's `Authorization` and `User-Agent` headers, and runs three tasks: charger to CSMS, CSMS to charger, and a watchdog. The first to finish tears down everything.

Repairs happen on the charger-to-CSMS path (`repair_message`, `receive_charger_messages`):
- `"configurationKey":]` is repaired to `[]`. The frame is invalid JSON, so HA's ocpp library never delivers the reply and the call times out after 10 s.
- `NotSupported` becomes `Rejected`, but only in replies to `ChangeConfiguration`. `receive_csms_messages` records each Call's action in `call_ids` so the reply can be matched by message id.
- Frames that still don't parse after repair are logged and dropped, never fatal.

The watchdog doesn't treat OCPP silence as death: the heartbeat interval is 3600 s, and `websockets` answers WebSocket pings internally without surfacing them. After `watchdog_stale` seconds idle it sends its own ping and closes only if no pong arrives within `ping_timeout`.

Never log the charger's `Authorization` header (it carries the charger's credentials) or the full handshake request.

## Releasing

CI (`.github/workflows/ci.yaml`) runs ruff, pytest and the HA app linter. `release.yaml` runs on a `v*` tag, which must equal `version` in `grizzle-ocpp-proxy/config.yaml`. It builds `ghcr.io/kccricket/{aarch64,amd64}-grizzle-ocpp-proxy` (the names the Supervisor derives from the manifest's `image:`) plus a multi-arch `ghcr.io/kccricket/grizzle-ocpp-proxy`. Keep `config.yaml`, `__version__` in `proxy.py`, and `CHANGELOG.md` in step.

## Charger notes

When reading a pcap of the charger, reassemble the TCP stream before unmasking WebSocket frames: it sends the frame header and payload in separate TCP segments. Its clock runs about 11 s fast.
