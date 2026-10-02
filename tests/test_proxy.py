"""End-to-end tests: a mock charger talks to the proxy, which talks to a mock CSMS."""

import asyncio
import base64
import json
import os
import time

import pytest
import websockets
from proxy import OCPPProxy, Settings, serve

CHARGER = "charger-T1"


@pytest.fixture(autouse=True)
def clean_proxy_list():
    OCPPProxy.proxy_list.clear()
    yield
    OCPPProxy.proxy_list.clear()


async def eventually(predicate, within=5.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


class MockCSMS:
    def __init__(self):
        self.received: list[str] = []
        self.connections = []

    async def handler(self, ws):
        self.connections.append(ws)
        async for message in ws:
            self.received.append(message)

    async def send(self, message: str):
        await self.connections[-1].send(message)


@pytest.fixture
async def csms():
    mock = MockCSMS()
    async with websockets.serve(mock.handler, "127.0.0.1", 0, subprotocols=["ocpp1.6"]) as server:
        mock.url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        yield mock


@pytest.fixture
async def start_proxy(csms):
    """Start a proxy on an ephemeral port; returns its ws:// base URL."""
    servers = []

    async def start(**overrides):
        settings = Settings(
            csms_url=csms.url, listen_host="127.0.0.1", listen_port=0, ping_timeout=2, **overrides
        )
        context = serve(settings)
        server = await context.__aenter__()
        servers.append(context)
        return f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"

    yield start
    for context in servers:
        await context.__aexit__(None, None, None)


async def connect_charger(base_url: str, charger_id: str = CHARGER):
    return await websockets.connect(f"{base_url}/{charger_id}", subprotocols=["ocpp1.6"])


# Message repairs


def test_repair_message_fixes_missing_bracket():
    broken = '[3,"x",{"configurationKey":],"unknownKey":["MeterValuesSampledData"]}]'
    fixed = OCPPProxy.repair_message(broken)
    assert json.loads(fixed) == [
        3,
        "x",
        {"configurationKey": [], "unknownKey": ["MeterValuesSampledData"]},
    ]


def test_repair_message_leaves_valid_frames_alone():
    ok = '[3,"x",{"configurationKey":[{"key":"A","readonly":true,"value":"1"}]}]'
    assert OCPPProxy.repair_message(ok) == ok


async def test_malformed_configuration_reply_reaches_csms_repaired(csms, start_proxy):
    base = await start_proxy()
    async with await connect_charger(base) as charger:
        await eventually(lambda: csms.connections)
        await csms.send('[2,"a1","GetConfiguration",{"key":["MeterValuesSampledData"]}]')
        await charger.recv()
        await charger.send(
            '[3,"a1",{"configurationKey":],"unknownKey":["MeterValuesSampledData"]}]'
        )
        await eventually(lambda: csms.received)
    assert json.loads(csms.received[0])[2] == {
        "configurationKey": [],
        "unknownKey": ["MeterValuesSampledData"],
    }


async def test_not_supported_replies_pass_through_unchanged(csms, start_proxy):
    base = await start_proxy()
    async with await connect_charger(base) as charger:
        await eventually(lambda: csms.connections)
        await csms.send('[2,"a1","ChangeConfiguration",{"key":"K","value":"1"}]')
        await charger.recv()
        reply = '[3,"a1",{"status":"NotSupported"}]'
        await charger.send(reply)
        await eventually(lambda: csms.received)
    assert csms.received == [reply]


async def test_unparseable_frame_is_dropped_without_closing(csms, start_proxy):
    base = await start_proxy()
    async with await connect_charger(base) as charger:
        await charger.send("this is not json")
        await charger.send('[2,"1","Heartbeat",{}]')
        await eventually(lambda: csms.received)
    assert csms.received == ['[2,"1","Heartbeat",{}]']


async def test_csms_calls_reach_charger(csms, start_proxy):
    base = await start_proxy()
    async with await connect_charger(base) as charger:
        await eventually(lambda: csms.connections)
        await csms.send('[2,"a1","RemoteStopTransaction",{"transactionId":1}]')
        assert json.loads(await charger.recv())[2] == "RemoteStopTransaction"


# Connection handling


async def test_new_connection_replaces_old_one(csms, start_proxy):
    base = await start_proxy()
    old = await connect_charger(base)
    await eventually(lambda: len(csms.connections) == 1)
    async with await connect_charger(base) as new:
        await asyncio.wait_for(old.wait_closed(), 5)
        await new.send('[2,"1","Heartbeat",{}]')
        await eventually(lambda: csms.received)


async def test_headers_are_forwarded_to_csms(csms, start_proxy):
    base = await start_proxy()
    auth = {"Authorization": "Basic dGVzdDp0ZXN0"}
    async with websockets.connect(
        f"{base}/{CHARGER}", subprotocols=["ocpp1.6"], additional_headers=auth
    ):
        await eventually(lambda: csms.connections)
    assert csms.connections[0].request.headers["Authorization"] == auth["Authorization"]


# Watchdog


async def test_idle_but_responsive_charger_survives_watchdog(csms, start_proxy):
    base = await start_proxy(watchdog_stale=0.5, watchdog_interval=0.2)
    async with await connect_charger(base) as charger:
        await asyncio.sleep(2)  # several stale periods with no OCPP traffic
        await charger.send('[2,"1","Heartbeat",{}]')
        await eventually(lambda: csms.received)


async def read_frame(reader):
    header = await reader.readexactly(2)
    length = header[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    return header[0] & 0x0F, await reader.readexactly(length)


async def test_charger_that_stops_answering_pings_is_closed(csms, start_proxy):
    base = await start_proxy(watchdog_stale=0.5, watchdog_interval=0.2)
    host, port = base.removeprefix("ws://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write(
        (
            f"GET /{CHARGER} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: ocpp1.6\r\n\r\n"
        ).encode()
    )
    await reader.readuntil(b"\r\n\r\n")

    # This raw client never answers: the proxy should ping it, wait, then send a close frame.
    opcodes = []
    async with asyncio.timeout(10):
        while 0x8 not in opcodes:
            opcode, _ = await read_frame(reader)
            opcodes.append(opcode)
    assert 0x9 in opcodes
    writer.close()


# Settings


def settings(env=None, options=None, tmp_path=None):
    path = tmp_path / "options.json"
    if options is not None:
        path.write_text(json.dumps(options))
    return Settings.load(env=env or {}, options_file=path)


def test_settings_defaults(tmp_path):
    s = settings({"CSMS_URL": "ws://ha:9000/"}, tmp_path=tmp_path)
    assert s.csms_url == "ws://ha:9000"  # trailing slash stripped
    assert (s.listen_port, s.watchdog_stale, s.ping_timeout, s.log_level) == (8321, 300, 60, "info")


def test_settings_csms_url_is_required(tmp_path):
    with pytest.raises(ValueError, match="CSMS_URL"):
        settings({}, tmp_path=tmp_path)


def test_settings_env_overrides_options_overrides_default(tmp_path):
    options = {"csms_url": "ws://opt:1", "ping_timeout": 11, "log_level": "debug"}
    s = settings({"PING_TIMEOUT": "22"}, options, tmp_path)
    assert (s.csms_url, s.ping_timeout, s.log_level) == ("ws://opt:1", 22, "debug")


def test_settings_listen_address_is_not_an_option(tmp_path):
    s = settings({}, {"csms_url": "ws://opt:1", "listen_port": 9999}, tmp_path)
    assert s.listen_port == 8321


def test_settings_reject_bad_values(tmp_path):
    with pytest.raises(ValueError, match="PING_TIMEOUT"):
        settings({"CSMS_URL": "ws://x", "PING_TIMEOUT": "soon"}, tmp_path=tmp_path)
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        settings({"CSMS_URL": "ws://x", "LOG_LEVEL": "loud"}, tmp_path=tmp_path)
