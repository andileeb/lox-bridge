import asyncio

from loxbridge.miniserver.discover import networks, parse_api_key, scan, targets

REAL = ("{\"LL\": { \"control\": \"dev/cfg/apiKey\", \"value\": \"{'snr': '50:4F:94:D0:D3:84', 'version':'17.3.10.27', "
        "'key':'3544', 'isInTrust':false, 'local':true,'address':'192.168.3.140','certTLD':'com', "
        "'httpsStatus':1}\", \"Code\": \"200\"}}")  # firmware 17.3, measured


def test_parse_api_key():
    assert parse_api_key(REAL)["snr"] == "50:4F:94:D0:D3:84"
    assert parse_api_key(REAL)["version"] == "17.3.10.27"
    assert parse_api_key("<html>router</html>") is None


def test_networks_and_targets(monkeypatch):
    monkeypatch.setattr("loxbridge.miniserver.discover._own_ip", lambda: "172.16.0.2")  # the container's network
    assert networks("192.168.2.10", "192.168.3.188", "192.168.2.99", "8.8.8.8", "nas.local", "::1",
                    "172.16.0.1") == \
        ["192.168.2.0/24", "192.168.3.0/24"]
    assert len(targets("192.168.3.0/24")) == 254
    assert targets("10.0.0.5") == ["10.0.0.5"] and targets("mock-ms:8443") == ["mock-ms:8443"]


def test_scan_finds_mock(mock_ms):
    url, _ = mock_ms
    found = asyncio.run(scan([url.removeprefix("https://"), "127.0.0.1:1"]))
    assert [(f["url"], f["serial"]) for f in found] == [(url, "504F94A0FFEE")]
