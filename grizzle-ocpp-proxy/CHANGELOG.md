# Changelog

## 0.1.1

- Removed the `NotSupported` to `Rejected` rewrite for `ChangeConfiguration` replies. `NotSupported`
  is a valid OCPP 1.6 status and the current integration handles it, so the charger's replies now
  pass through untouched. Only the malformed `configurationKey` frames are repaired.
- Corrected the docs on pointing the charger at the proxy: the charger appends its own station ID to
  the OCPP URL, so enter `ws://<host>:8321/charger-` and not the full ID.
- Frames that aren't shaped like an OCPP message are dropped, as before, using a simpler check.

## 0.1.0

First release. Forked from
[ocpp-2w-proxy](https://github.com/ocpp-balanz/ocpp-2w-proxy) and reduced to a fix-up proxy for the Grizzl-E.

- Repairs the malformed `"configurationKey":]` reply the Grizzl-E sends for unknown keys, which Home
  Assistant otherwise never receives (each one costs a 10 second timeout).
- Rewrites `NotSupported` to `Rejected` in `ChangeConfiguration` replies.
- Watchdog no longer drops a healthy idle charger: it pings the charger first and closes the
  connection only if the ping goes unanswered.
- Configured by environment variables, or by the Home Assistant app options. No ini file.
- Removed the secondary server, TLS listener and OCPP 2.0.1 support.
- Never logs the charger's credentials; clean disconnects are no longer logged as errors; `docker stop`
  shuts down promptly.
