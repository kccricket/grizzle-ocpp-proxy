# OCPP fix-up proxy for the Grizzl-E EV charger.
# Sits between the charger and a CSMS (e.g. the Home Assistant OCPP integration) and repairs
# the frames the charger firmware gets wrong.
#
# Based on ocpp-2w-proxy by Jens Vedel Markussen: https://github.com/ocpp-balanz/ocpp-2w-proxy

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import IntEnum
from functools import partial
from pathlib import Path

import websockets
import websockets.asyncio.server

__version__ = "0.3.0"

# Written by the Home Assistant Supervisor from the app's configuration tab.
OPTIONS_FILE = Path("/data/options.json")

SUBPROTOCOL = "ocpp1.6"
LOG_LEVELS = ("debug", "info", "warning", "error")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("proxy")


@dataclass(frozen=True)
class Settings:
    """Proxy configuration. Each field is read from the upper-cased environment variable of the
    same name, then from the Home Assistant options file, then falls back to its default."""

    csms_url: str  # required
    listen_host: str = "0.0.0.0"
    listen_port: int = 8321
    # Seconds without OCPP traffic from the charger before the proxy pings it
    watchdog_stale: int = 300
    # How often the watchdog checks, in seconds
    watchdog_interval: int = 30
    # Seconds to wait for a pong, both for keepalive and for the watchdog's ping
    ping_timeout: int = 60
    log_level: str = "info"

    # Fixed by the app's port mapping, so not offered as a Home Assistant option
    _env_only = ("listen_host", "listen_port")

    @classmethod
    def load(
        cls, env: Mapping[str, str] | None = None, options_file: Path | None = None
    ) -> "Settings":
        env = os.environ if env is None else env
        options_file = OPTIONS_FILE if options_file is None else options_file
        options: dict = {}
        if options_file.is_file():
            try:
                options = json.loads(options_file.read_text())
            except (OSError, json.JSONDecodeError) as e:
                raise ValueError(f"Cannot read {options_file}: {e}") from e

        values = {}
        for field in fields(cls):
            raw = env.get(field.name.upper())
            if raw is None and field.name not in cls._env_only:
                raw = options.get(field.name)
            if raw is None or raw == "":
                continue
            try:
                values[field.name] = field.type(raw) if field.type is not str else str(raw)
            except ValueError as e:
                raise ValueError(f"{field.name.upper()} must be an integer, got {raw!r}") from e

        if "csms_url" not in values:
            raise ValueError("CSMS_URL is required, e.g. CSMS_URL=ws://homeassistant:9000")
        values["csms_url"] = values["csms_url"].rstrip("/")
        settings = cls(**values)
        if settings.log_level.lower() not in LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
        return settings


class OCPPMessageType(IntEnum):
    Call = 2
    CallResult = 3
    CallError = 4


class OCPPProxy:
    # Static dict of OCPPProxy instances. key is charger_id
    proxy_list: dict[str, "OCPPProxy"] = {}

    # Utility functions
    @staticmethod
    def decode_ocpp_message(message: str) -> tuple[int | None, str | None, str | None]:
        """Decode an OCPP message from a string"""
        try:
            j = json.loads(message)
            action = j[2] if j[0] == OCPPMessageType.Call else None
            return (j[0], j[1], action)
        except (json.JSONDecodeError, IndexError, KeyError, TypeError):
            return (None, None, None)

    @staticmethod
    def repair_message(message: str) -> str:
        """Apply Grizzl-E firmware workarounds to a raw OCPP frame."""
        # Grizzl-E firmware bug: missing '[' before ']' in configurationKey array.
        # Observed: {"configurationKey":],...}
        repaired = re.sub(r'"configurationKey":\](?=,)', '"configurationKey":[]', message)
        if repaired != message:
            logger.warning("Applied Grizzl-E configurationKey repair")
        return repaired

    @staticmethod
    def repair_change_configuration_response(message: str) -> str:
        """Grizzl-E returns NotSupported for ChangeConfiguration; normalize to Rejected."""
        repaired = message.replace('"NotSupported"', '"Rejected"')
        if repaired != message:
            logger.warning("Replaced NotSupported with Rejected in ChangeConfiguration response")
        return repaired

    def __init__(
        self,
        websocket: websockets.asyncio.server.ServerConnection,
        charger_id: str,
        settings: Settings,
    ):
        self.ws = websocket
        self.charger_id = charger_id
        self.settings = settings
        self.csms_connection = None

        if not re.match(r"^[A-Za-z0-9_-]+$", charger_id):
            logger.error(f"Charger ID '{charger_id}' contains invalid characters")
            raise ValueError("Charger ID contains invalid characters")

        # Maps in-flight CSMS call IDs to their action name for response repair
        self.call_ids: dict[str, str] = {}

        # Insert new OCPPProxy instance in the (static) dict of instances.
        self.proxy_list[charger_id] = self

    async def close(self):
        """Close the connections to the charger and the CSMS"""
        try:
            await self.ws.close()
            if self.csms_connection:
                await self.csms_connection.close()
        except Exception:
            pass  # Ignore exceptions

    async def run(self):
        """Connect to the CSMS and shuttle messages until either side goes away."""

        # Forward any available Authorization and User-Agent headers. The Authorization value
        # is the charger's credential, so it is never logged.
        headers = {}
        if "Authorization" in self.ws.request.headers:
            headers["Authorization"] = self.ws.request.headers["Authorization"]
            logger.debug(f"{self.charger_id} Forwarding charger's Authorization header")
        user_agent = self.ws.request.headers.get("User-Agent", None)
        csms_url = f"{self.settings.csms_url}/{self.charger_id}"

        try:
            self.csms_connection = await websockets.connect(
                uri=csms_url,
                user_agent_header=user_agent,
                additional_headers=headers,
                subprotocols=[SUBPROTOCOL],
            )
            logger.info(f"{self.charger_id} Connected to CSMS @ {csms_url}")

            # One task per direction, plus a watch dog task to take down the
            # connections if the charger goes stale.
            self._last_charger_update = time.time()
            self.tasks = [
                asyncio.create_task(self.receive_charger_messages()),
                asyncio.create_task(self.receive_csms_messages()),
                asyncio.create_task(self.watchdog()),
            ]

            # Wait for tasks to complete
            done, pending = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
            logger.debug(f"{self.charger_id} Task(s) completed: {done}, {pending}")

            for task in done:
                e = task.exception()
                if e:
                    logger.warning(f"{self.charger_id} Task {task} raised exception {e}")

            # Cancel any remaining tasks
            for task in pending:
                task.cancel()

        except websockets.exceptions.InvalidURI:
            logger.error(f"{self.charger_id} Invalid CSMS URL: {csms_url}")
        except websockets.exceptions.InvalidHandshake as e:
            logger.error(f"{self.charger_id} Handshake with the CSMS failed: {e}")
        except OSError as e:
            logger.error(f"{self.charger_id} Cannot reach the CSMS at {csms_url}: {e}")
        except Exception as e:
            logger.error(f"{self.charger_id} Unexpected error: {e}")
        finally:
            # Always close stuff. close is well tempered, so can close even if not established
            await self.close()

    async def receive_charger_messages(self):
        try:
            while True:
                message = await self.ws.recv()
                self._last_charger_update = time.time()
                logger.info(f"{self.charger_id} ^ : {message}")

                message = OCPPProxy.repair_message(message)
                (message_type, message_id, _) = OCPPProxy.decode_ocpp_message(message)
                if message_type is None:
                    logger.warning(
                        f"{self.charger_id} ^: Unparseable frame after repair, dropping: "
                        f"{message[:120]}"
                    )
                    continue

                if message_type in (OCPPMessageType.CallResult, OCPPMessageType.CallError):
                    if self.call_ids.pop(message_id, None) == "ChangeConfiguration":
                        message = OCPPProxy.repair_change_configuration_response(message)
                await self.csms_connection.send(message)
        except websockets.exceptions.ConnectionClosedOK:
            logger.info(f"{self.charger_id} Connection closed")
        except Exception as e:
            logger.error(f"{self.charger_id} Error in receive_charger_messages: {e}")

    async def receive_csms_messages(self):
        try:
            while True:
                message = await self.csms_connection.recv()
                logger.info(f"{self.charger_id} v : {message}")

                (message_type, message_id, action) = OCPPProxy.decode_ocpp_message(message)
                if message_type is None:
                    logger.warning(f"{self.charger_id} v: Unparseable frame from CSMS, dropping")
                    continue
                if message_type == OCPPMessageType.Call:
                    self.call_ids[message_id] = action

                await self.ws.send(message)
        except websockets.exceptions.ConnectionClosedOK:
            logger.info(f"{self.charger_id} CSMS connection closed")
        except Exception as e:
            logger.error(f"{self.charger_id} Error in receive_csms_messages: {e}")

    async def watchdog(self):
        """Close the connections if the charger goes silent and stops answering pings."""
        while True:
            await asyncio.sleep(self.settings.watchdog_interval)

            elapsed = time.time() - self._last_charger_update
            if elapsed > self.settings.watchdog_stale:
                # A healthy idle charger can go much longer than this between OCPP messages
                # (the Grizzl-E heartbeat is 3600s), and websockets answers its pings internally
                # without surfacing them. So only give up if the charger fails a ping of our own.
                try:
                    pong_waiter = await self.ws.ping()
                    await asyncio.wait_for(pong_waiter, timeout=self.settings.ping_timeout)
                except Exception as e:
                    logger.error(
                        f"{self.charger_id} Watch dog: no OCPP traffic for {elapsed:.0f}s and "
                        f"ping failed ({e!r}). Closing connections"
                    )
                    return
                logger.debug(
                    f"{self.charger_id} Watch dog: idle for {elapsed:.0f}s but charger "
                    "answered ping"
                )
                self._last_charger_update = time.time()


# Connection handler (charger connects)
async def on_connect(settings: Settings, websocket: websockets.asyncio.server.ServerConnection):
    # Determine charger_id (final part of path)
    charger_id = websocket.request.path.strip("/")
    logger.info(f"{charger_id} connection request")

    try:
        # The charger reconnects without closing a stale connection first; drop the old one.
        if charger_id in OCPPProxy.proxy_list:
            await OCPPProxy.proxy_list[charger_id].close()
            del OCPPProxy.proxy_list[charger_id]

        proxy = OCPPProxy(websocket=websocket, charger_id=charger_id, settings=settings)
        await proxy.run()

    except Exception as e:
        logger.error(f"{charger_id} Error creating OCPPProxy: {e}")
    finally:
        logger.info(f"{charger_id} closed/done")


def serve(settings: Settings):
    """Create the charger-facing server. Use as `async with serve(settings) as server`."""
    return websockets.serve(
        partial(on_connect, settings),
        settings.listen_host,
        settings.listen_port,
        subprotocols=[SUBPROTOCOL],
        ping_timeout=settings.ping_timeout,
    )


def configure_logging(level: str):
    logger.setLevel(level.upper())
    logging.getLogger("websockets").setLevel(logging.INFO if level == "debug" else logging.WARNING)


async def run_server(settings: Settings):
    # Python as PID 1 ignores SIGTERM unless a handler is installed, which makes
    # `docker stop` wait out its kill timeout.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with serve(settings):
        logger.info(
            f"Proxy {__version__} listening on {settings.listen_host}:{settings.listen_port}, "
            f"forwarding to {settings.csms_url}"
        )
        await stop.wait()
    logger.info("Shut down")


def main():
    parser = argparse.ArgumentParser(
        description="grizzle-ocpp-proxy: a fix-up proxy for the Grizzl-E EV charger. "
        "Configured by environment variables (CSMS_URL, ...) or, in Home Assistant, app options."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args()

    try:
        settings = Settings.load()
    except ValueError as e:
        parser.exit(2, f"error: {e}\n")
    configure_logging(settings.log_level)
    asyncio.run(run_server(settings))


if __name__ == "__main__":
    main()
