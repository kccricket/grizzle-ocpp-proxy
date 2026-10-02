import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "configure_charger", Path(__file__).parent.parent / "scripts" / "configure-charger.py"
)
configure_charger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure_charger)
proxy_url = configure_charger.proxy_url


def test_proxy_url_keeps_the_current_path():
    # The charger appends its station ID, so the path (and the charge point ID) must not change
    assert proxy_url("ws://proxy:8321", "ws://ha:9000/charger-", None) == "ws://proxy:8321/charger-"


def test_proxy_url_ignores_trailing_slash_and_query_on_current_url():
    assert (
        proxy_url("ws://proxy:8321/", "ws://ha:9000/charger-/?x=1", None)
        == "ws://proxy:8321/charger-"
    )


def test_proxy_url_falls_back_when_current_url_has_no_path():
    factory = "wss://ocpp.unitedchargers.com/ocpp1.6?station="
    # "ocpp1.6" would be invalid for the proxy, so a URL like this has to fall back to the default
    assert proxy_url("ws://proxy:8321", "ws://ha:9000", None) == "ws://proxy:8321/charger-"
    with pytest.raises(SystemExit):
        proxy_url("ws://proxy:8321", factory, None)


def test_proxy_url_explicit_path_overrides():
    assert (
        proxy_url("ws://proxy:8321", "ws://ha:9000/charger-", "wallbox_")
        == "ws://proxy:8321/wallbox_"
    )


@pytest.mark.parametrize("path", ["ocpp1.6", "a/b", "has space"])
def test_proxy_url_rejects_paths_the_proxy_would_refuse(path):
    with pytest.raises(SystemExit):
        proxy_url("ws://proxy:8321", "ws://ha:9000/charger-", path)


@pytest.mark.parametrize("proxy", ["proxy:8321", "http://proxy:8321", "ws://"])
def test_proxy_url_rejects_bad_proxy_addresses(proxy):
    with pytest.raises(SystemExit):
        proxy_url(proxy, "ws://ha:9000/charger-", None)
