# ocpp-2w-proxy

A fix-up proxy for the Grizzl-E EV charger. It sits between the charger and a CSMS
(for example the Home Assistant OCPP integration) and repairs the OCPP frames the
Grizzl-E firmware gets wrong.

(The name is a holdover from the upstream two-way proxy this was forked from.
It now talks to a single CSMS.)

## What it fixes

- **Malformed `GetConfiguration` replies.** The charger sends `"configurationKey":]` with the
  opening `[` missing when it doesn't know a key. This is invalid JSON, so HA never receives
  the reply and waits out its 10 s timeout. The proxy repairs it to `"configurationKey":[]`.
- **`NotSupported` in `ChangeConfiguration` replies.** Replaced with `Rejected`, the status
  the OCPP 1.6 spec allows for a refused change.
- **Unparseable frames** are logged and dropped instead of taking the connection down.
- **Stale connections.** The charger reconnects without closing its old connection, so a new
  connection replaces any existing one for the same charger ID. A watchdog pings the charger
  if it has been silent for `watchdog_stale` seconds, and closes the connection only if the
  ping goes unanswered. The charger's heartbeat interval is 3600 s, so silence alone doesn't
  mean it's dead.

Authorization and User-Agent headers from the charger are forwarded to the CSMS.

## Usage

Set `server` in `ocpp-2w-proxy.ini` to your CSMS, then start the proxy with:

`python ocpp-2w-proxy.py`

Point the charger at `ws://<proxy-host>:8321/<charger-id>`. The charger ID from the path is
appended to the CSMS URL.

## Docker

`Dockerfile` and `compose.yaml` files are included.
