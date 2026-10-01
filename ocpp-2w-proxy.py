# OCPP fix-up proxy for the Grizzl-E EV charger.
# Sits between the charger and a CSMS (e.g. the Home Assistant OCPP integration) and repairs
# the frames the charger firmware gets wrong.

import asyncio
import logging
import re
import time
from typing import Optional, Tuple
import json

import websockets
import websockets.asyncio.server

from enum import IntEnum
import argparse
import configparser

__version__ = "0.2.0"

config = configparser.ConfigParser()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("proxy")

SUBPROTOCOL = "ocpp1.6"

class OCPPMessageType(IntEnum):
    Call = 2
    CallResult = 3
    CallError = 4

class OCPPProxy:
    # Static dict of OCPPProxy instances. key is charger_id
    proxy_list: dict[str, "OCPPProxy"] = {}

    # Utility functions
    @staticmethod
    def decode_ocpp_message(message: str) -> Tuple[Optional[int], Optional[str], Optional[str]]:
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

    def __init__(self, websocket: websockets.asyncio.server.ServerConnection, charger_id: str):
        logger.debug(websocket.request)
        self.ws = websocket
        self.charger_id = charger_id
        self.csms_connection = None

        if not re.match(r'^[A-Za-z0-9_-]+$', charger_id):
            logger.error(f"Charger ID '{charger_id}' contains invalid characters")
            raise Exception("Charger ID contains invalid characters")

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
            pass # Ignore exceptions

    async def run(self):
        """Connect to the CSMS and shuttle messages until either side goes away."""

        # Forward any available Authorization and User-Agent headers
        headers = {}
        if "Authorization" in self.ws.request.headers:
            headers["Authorization"] = self.ws.request.headers["Authorization"]
            logger.debug(f'Authorization header set to {headers["Authorization"]}')
        user_agent = self.ws.request.headers.get("User-Agent", None)
        csms_url = config.get("ext-server", "server") + "/" + self.charger_id

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
                    logger.warning(f"{self.charger_id} (Not serious - likely connection loss) Task {task} raised exception {e} related to charger ")

            # Cancel any remaining tasks
            for task in pending:
                task.cancel()

        except websockets.exceptions.InvalidURI:
            logger.error(f"{self.charger_id} Invalid URI")
        except websockets.exceptions.ConnectionClosedError as e:
            logger.error(f"{self.charger_id} Connection closed unexpectedly: {e}")
        except websockets.exceptions.InvalidHandshake:
            logger.error(f"{self.charger_id} Handshake with the CSMS failed")
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
                    logger.warning(f"{self.charger_id} ^: Unparseable frame after repair attempt, dropping: {message[:120]}")
                    continue

                if message_type in (OCPPMessageType.CallResult, OCPPMessageType.CallError):
                    if self.call_ids.pop(message_id, None) == "ChangeConfiguration":
                        message = OCPPProxy.repair_change_configuration_response(message)
                await self.csms_connection.send(message)
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
        except Exception as e:
            logger.error(f"{self.charger_id} Error in receive_csms_messages: {e}")

    async def watchdog(self):
        """Close the connections if the charger goes silent and stops answering pings."""
        while True:
            await asyncio.sleep(config.getint("host", "watchdog_interval", fallback=30))

            elapsed = time.time() - self._last_charger_update
            if elapsed > config.getint("host", "watchdog_stale", fallback=300):
                # A healthy idle charger can go much longer than this between OCPP messages
                # (the Grizzl-E heartbeat is 3600s), and websockets answers its pings internally
                # without surfacing them. So only give up if the charger fails a ping of our own.
                try:
                    pong_waiter = await self.ws.ping()
                    await asyncio.wait_for(pong_waiter, timeout=config.getint("host", "ping_timeout", fallback=60))
                except Exception as e:
                    logger.error(f"{self.charger_id} Watch dog: no OCPP traffic for {elapsed:.0f} seconds and ping failed ({e!r}). Closing connections")
                    return
                logger.debug(f"{self.charger_id} Watch dog: idle for {elapsed:.0f} seconds but charger answered ping")
                self._last_charger_update = time.time()

# Connection handler (charger connects)
async def on_connect(websocket: websockets.asyncio.server.ServerConnection):
    logger.debug(f'Connection request: {websocket.request}')
    # Determine charger_id (final part of path)
    path = websocket.request.path
    charger_id = path.strip("/")
    logger.info(f'{charger_id} connection request')

    try:
        # The charger reconnects without closing a stale connection first; drop the old one.
        if charger_id in OCPPProxy.proxy_list:
            await OCPPProxy.proxy_list[charger_id].close()
            del OCPPProxy.proxy_list[charger_id]

        proxy = OCPPProxy(websocket=websocket, charger_id=charger_id)
        await proxy.run()

    except Exception as e:
        logger.error(f'{charger_id} Error creating OCPPProxy: {e}')
    finally:
        logger.info(f"{charger_id} closed/done")


# Main. Decode arguments, setup handler
async def main():
    parser = argparse.ArgumentParser(
        description='ocpp-2w-proxy: a fix-up proxy for the Grizzl-E EV charger')
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {__version__}')
    parser.add_argument(
        "--config",
        type=str,
        default="ocpp-2w-proxy.ini",
        help="Configuration file (INI format). Default ocpp-2w-proxy.ini",
    )
    args = parser.parse_args()

    logger.warning(f"Reading config from {args.config}")
    config.read(args.config)

    # Adjust log levels
    for logger_name in config["logging"]:
        logger.warning(f'Setting log level for {logger_name} to {config.get("logging", logger_name)}')
        logging.getLogger(logger_name).setLevel(level=config.get("logging", logger_name))

    host = config.get("host", "addr")
    port = config.get("host", "port")
    logger.debug(f"host: {host}, port: {port}")

    server = await websockets.serve(
        on_connect,
        host,
        port,
        subprotocols=[SUBPROTOCOL],
        ping_timeout=config.getint("host", "ping_timeout"),
    )

    logger.info("Proxy ready. Waiting for new connections...")
    await server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        exit(0)
