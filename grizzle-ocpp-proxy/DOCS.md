# Grizzl-E OCPP Proxy

The Grizzl-E charger answers some OCPP requests with invalid JSON. Home Assistant's OCPP integration
never receives those replies, so each one costs a 10 second timeout, and charger setup retries over
and over. This app sits between the charger and Home Assistant and repairs the frames in flight.

## Setup

1. Install and start this app. Review the configuration (below); the defaults suit the Home
   Assistant OCPP integration on its default port.
2. In the charger's settings, change its OCPP server (central system) URL to the proxy:

   ```
   ws://<your-home-assistant-ip>:8321/charger-
   ```

   The charger appends its own station ID to this URL, so don't add it yourself, but the URL needs
   a path. Keep the path and any credentials the charger already has: the path plus the station ID
   is the charge point ID Home Assistant knows it by, and changing it creates a new device.
3. Leave the Home Assistant OCPP integration as it is. It continues to listen on port 9000, and the
   charger now reaches it through the proxy.

## Configuration

| Option | Default | Description |
|---|---|---|
| CSMS URL | `ws://homeassistant:9000` | Where traffic is forwarded. The path the charger connected with (such as `charger-GRS-…`) is appended. If `homeassistant` doesn't resolve for you, use `ws://<your-home-assistant-ip>:9000`. |
| Log level | `info` | `debug` logs every OCPP frame. |
| Idle time before a ping | 300 s | The charger is pinged after this long without an OCPP message. Closed only if the ping goes unanswered. |
| Idle check interval | 30 s | How often idleness is checked. |
| Ping timeout | 60 s | How long to wait for a pong. |

The listening port is 8321. To change it, use the Network section of the app's configuration tab.

## Troubleshooting

- The app log shows `Applied Grizzl-E configurationKey repair` each time it fixes a frame.
- To see the raw frames Home Assistant sends and receives, add this to `configuration.yaml`:

  ```yaml
  logger:
    logs:
      ocpp: info
      custom_components.ocpp: debug
  ```

- If the charger can't connect, check that its OCPP URL has a path (such as `/charger-`) and points at port 8321. The path may contain only letters, digits, `-` and `_`.
