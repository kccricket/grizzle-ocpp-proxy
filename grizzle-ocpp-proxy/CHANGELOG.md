# Changelog

## 0.3.0

First release as `grizzle-ocpp-proxy`, forked from
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
