# 2 way OCPP proxy (can also be used as a 1-way simple proxy)

import asyncio
import logging
import re
import time
from typing import Tuple
import json

import websockets
import websockets.asyncio
import websockets.asyncio.server

from enum import IntEnum
import ssl
import argparse
import configparser

__version__ = "0.1.0"

config = configparser.ConfigParser()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("proxy")

class OCPP2WProxy: # Forward declaration
    pass

class OCPPMessageType(IntEnum):
    Call = 2
    CallResult = 3
    CallError = 4

# main class
class OCPP2WProxy:
    # Static dict of OCPP2WProxy instances. key is charger_id
    proxy_list: dict[str, OCPP2WProxy] = {}

    # Utility functions
    @staticmethod
    def decode_ocpp_message(message: str) -> Tuple[OCPPMessageType, str]:
        """Decode an OCPP message from a string"""
        try:
            j = json.loads(message)
            action = j[2] if j[0] == OCPPMessageType.Call else None
            return [j[0], j[1], action]
        except (json.JSONDecodeError, IndexError, KeyError):
            return [None, None, None]

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
        # Store the websocket for later     
        logger.debug(websocket.request)
        self.ws = websocket
        self.charger_id = charger_id

        if not re.match(r'^[A-Za-z0-9_-]+$', charger_id):
            logger.error(f"Charger ID '{charger_id}' contains invalid characters")
            raise Exception("Charger ID contains invalid characters")

        # Maps in-flight CSMS call IDs to their action name for response routing/repair
        self.primary_call_ids: dict[str, str] = {}
        self.secondary_call_ids: dict[str, str] = {}

        # Insert new OCPP2WProxy instance in the (static) dict of instances.
        self.proxy_list[charger_id] = self

    async def close(self):
        """Close all connections to the charger and primary, secondary server"""
        try:
            await self.ws.close()
            await self.primary_connection.close()
            if self.secondary_connection:
                await self.secondary_connection.close()
        except Exception as e:
            pass # Ignore exceptions

    async def run(self):
        """Main loop for this proxy. This is where all the magic happens."""

        # Create connections to the two CSMSes.
        # Forward any available Authorization and User-Agent headers
        headers = {}
        if "Authorization" in self.ws.request.headers:
            headers["Authorization"] = self.ws.request.headers["Authorization"]
            logger.debug(f'Authorization header set to {headers["Authorization"]}')
        user_agent = self.ws.request.headers.get("User-Agent", None) 
        subprotocols = self.ws.request.headers.get("Sec-WebSocket-Protocol", "ocpp1.6")
        primary_url = config.get("ext-server", "server") + "/" + self.charger_id
        if config.has_option("ext-server", "secondary_server"):
            secondary_url = config.get("ext-server", "secondary_server") + "/" + self.charger_id
        else:
            secondary_url = None    

        try:
            self.primary_connection = await websockets.connect(
                uri=primary_url,
                user_agent_header=user_agent,
                additional_headers=headers,
                subprotocols=[subprotocols],
            )
            logger.info(f"Connected to primary server @ {primary_url}")

            # Connect to secondary server if it is enabled.
            if secondary_url:
                self.secondary_connection = await websockets.connect(
                    uri=secondary_url,
                    user_agent_header=user_agent,
                    additional_headers=headers,
                    subprotocols=[subprotocols],
                )
                logger.info(f"{self.charger_id} Connected to secondary server @ {secondary_url}")
            else:
                self.secondary_connection = None
                logger.info(f"{self.charger_id} Secondary server not enabled")
            
            # Create tasks to handle the charger. Each task each to handle receiving messages from
            # the charger, and the (one or two) CSMSes, and finally a watch dog task to take down
            # connections if connection goes stale.
            self._last_charger_update = time.time()
            self.tasks = []
            self.tasks.append(asyncio.create_task(self.receive_charger_messages()))
            self.tasks.append(asyncio.create_task(self.receive_primary_messages()))
            if self.secondary_connection is not None:
                self.tasks.append(asyncio.create_task(self.receive_secondary_messages()))
            self.tasks.append(asyncio.create_task(self.watchdog()))

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
            logger.error(f"{self.charger_id} Handshake with the external server failed")
        except Exception as e:
            logger.error(f"{self.charger_id} Unexpected error: {e}")
        finally:
            # Always close stuff. close is well tempered, so can close even if not stablished
            await self.close()

    async def receive_charger_messages(self):
        try:
            while True:
                message = await self.ws.recv()
                self._last_charger_update = time.time()
                logger.info(f"{self.charger_id} ^ : {message}")

                message = OCPP2WProxy.repair_message(message)
                [message_type, message_id, _] = OCPP2WProxy.decode_ocpp_message(message)
                if message_type is None:
                    logger.warning(f"{self.charger_id} ^: Unparseable frame after repair attempt, dropping: {message[:120]}")
                    continue

                if message_type == OCPPMessageType.Call:
                    await self.primary_connection.send(message)
                    if self.secondary_connection:
                        await self.secondary_connection.send(message)
                elif message_type == OCPPMessageType.CallResult or message_type == OCPPMessageType.CallError:
                    if message_id in self.primary_call_ids:
                        action = self.primary_call_ids.pop(message_id)
                        if action == "ChangeConfiguration":
                            message = OCPP2WProxy.repair_change_configuration_response(message)
                        logger.info(f"{self.charger_id} ^ : Result/Error forwarded to primary")
                        await self.primary_connection.send(message)
                    elif message_id in self.secondary_call_ids:
                        action = self.secondary_call_ids.pop(message_id)
                        if action == "ChangeConfiguration":
                            message = OCPP2WProxy.repair_change_configuration_response(message)
                        logger.info(f"{self.charger_id} ^ : Result/Error forwarded to secondary")
                        await self.secondary_connection.send(message)
                    else:
                        logger.error(f"{self.charger_id} ^: Received CallResult/CallError against unknown message id {message_id}")
                else:
                    logger.error(f"{self.charger_id} ^: Unknown message type {message_type}")
        except Exception as e:
            logger.error(f"{self.charger_id} Error in receive_charger_messages: {e}")

    async def receive_primary_messages(self):
        try:
            while True:
                message = await self.primary_connection.recv()
                logger.info(f"{self.charger_id} v (prim) : {message}")

                [message_type, message_id, action] = OCPP2WProxy.decode_ocpp_message(message)
                if message_type is None:
                    logger.warning(f"{self.charger_id}: Unparseable frame from primary, dropping")
                    continue
                if message_type == OCPPMessageType.Call:
                    self.primary_call_ids[message_id] = action

                await self.ws.send(message)
        except Exception as e:
            logger.error(f"{self.charger_id} Error in receive_primary_messages: {e}")

    async def receive_secondary_messages(self):
        try:
            while True:
                message = await self.secondary_connection.recv()
                logger.info(f"{self.charger_id} v (sec) : {message}")

                [message_type, message_id, action] = OCPP2WProxy.decode_ocpp_message(message)
                if message_type is None:
                    logger.warning(f"{self.charger_id}: Unparseable frame from secondary, dropping")
                    continue
                if message_type == OCPPMessageType.Call:
                    self.secondary_call_ids[message_id] = action
                    await self.ws.send(message)
                # Note! We do not forward CallResults or CallErrors from the secondary server
                # These are silently ignored.
        except Exception as e:
            logger.error(f"{self.charger_id} Error in receive_secondary_messages: {e}")

    async def watchdog(self):
        """Watch time vs. timestamp updated by receiving messages from charger."""
        while True:
            # And ... sleep
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
        # Delete any existing charger setup
        if charger_id in OCPP2WProxy.proxy_list:
            await OCPP2WProxy.proxy_list[charger_id].close()
            del OCPP2WProxy.proxy_list[charger_id]

        # Setup
        proxy = OCPP2WProxy(websocket=websocket, charger_id=charger_id)

        # Connect and run proxy operations
        await proxy.run()

    except Exception as e:
        logger.error(f'{charger_id} Error creating OCPP2WProxy: {e}')
    finally:
        logger.info(f"{charger_id} closed/done")


# Main. Decode arguments, setup handler
async def main():
    parser = argparse.ArgumentParser(
        description='ocpp-2w-proxy: A two way OCPP proxy')
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {__version__}')
    parser.add_argument(
        "--config",
        type=str,
        default="ocpp-2w-proxy.ini",
        help="Configuration file (INI format). Default ocpp-2w-proxy.ini",
    )
    args = parser.parse_args()

    # Read config. config object is then available (via config import) to all.
    logger.warning(f"Reading config from {args.config}")
    config.read(args.config)

    # Adjust log levels
    for logger_name in config["logging"]:
        logger.warning(f'Setting log level for {logger_name} to {config.get("logging", logger_name)}')
        logging.getLogger(logger_name).setLevel(level=config.get("logging", logger_name))

    # Get host config
    host = config.get("host", "addr")
    port = config.get("host", "port")
    cert_chain = config.get("host", "cert_chain", fallback=None)
    cert_key = config.get("host", "cert_key", fallback=None)
    logger.debug(f"host: {host}, port: {port}, cert_chain: {cert_chain}, cert_key: {cert_key}")

    # Start server, either ws:// or wss://
    if cert_chain and cert_key:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=cert_chain, keyfile=cert_key)
        server = await websockets.serve(
            on_connect,
            host,
            port,
            subprotocols=["ocpp1.6", "ocpp2.0.1"],
            ssl=ssl_context,
            ping_timeout=config.getint("host", "ping_timeout"),
        )
    else:
        server = await websockets.serve(
            on_connect,
            host,
            port,
            subprotocols=["ocpp1.6", "ocpp2.0.1"],
            ping_timeout=config.getint("host", "ping_timeout"),
        )

    logger.info("Proxy ready. Waiting for new connections...")
    await server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        exit(0)
