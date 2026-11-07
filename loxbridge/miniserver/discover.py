"""Find Gen 2 Miniservers on the LAN: probe https://<ip>/jdev/cfg/apiKey (needs no login) on whole /24 networks.

UDP discovery (Loxone Config's search) and mDNS don't cross the Docker bridge network TrueNAS apps run in, but
plain HTTPS requests do. The networks to scan come from the browser: the NAS address it used (Host header) and
the browser's own address, because the Miniserver is often in the client VLAN, not the NAS one.
"""

import asyncio
import ipaddress
import re
import socket

import httpx

_FIELD = re.compile(r"'(snr|version|address|httpsStatus)'\s*:\s*'?([^',}]*)")
MAX_HOSTS = 1024


def parse_api_key(text: str) -> dict | None:
    """{'snr': ..., 'version': ...} from the jdev/cfg/apiKey answer, None if it isn't a Miniserver."""
    found = dict(_FIELD.findall(text))
    return found if "snr" in found else None


def _own_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; picks the interface of the default route
            return s.getsockname()[0]
    except OSError:
        return None


def networks(*addresses: str | None) -> list[str]:
    """/24 networks of the given private IPv4 addresses, skipping the container's own (Docker) network."""
    own = _own_ip()
    skip = ipaddress.ip_network(f"{own}/24", strict=False) if own else None
    out: list[str] = []
    for a in addresses:
        try:
            ip = ipaddress.ip_address((a or "").split("%")[0])
        except ValueError:
            continue
        if ip.version != 4 or not ip.is_private or ip.is_loopback:
            continue
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        if net != skip and str(net) not in out:
            out.append(str(net))
    return out


def targets(spec: str) -> list[str]:
    """'192.168.3.0/24', '192.168.3.140', 'host:port' or a hostname → hosts to probe."""
    spec = spec.strip()
    try:
        net = ipaddress.ip_network(spec, strict=False)
    except ValueError:
        return [spec] if spec else []
    if net.num_addresses > MAX_HOSTS:
        raise ValueError(f"{spec} is too large, use a /22 or smaller")
    return [str(h) for h in net.hosts()] or [str(net.network_address)]


async def scan(specs: list[str], timeout: float = 1.5, concurrency: int = 128) -> list[dict]:
    hosts = list(dict.fromkeys(h for s in specs for h in targets(s)))
    sem = asyncio.Semaphore(concurrency)

    async def probe(c: httpx.AsyncClient, host: str) -> dict | None:
        async with sem:
            try:
                r = await c.get(f"https://{host}/jdev/cfg/apiKey")
            except (httpx.HTTPError, OSError):
                return None
        info = parse_api_key(r.text) if r.status_code == 200 else None
        if not info:
            return None
        return {"host": host, "url": f"https://{host}", "serial": info["snr"].replace(":", ""),
                "firmware": info.get("version", "")}

    async with httpx.AsyncClient(verify=False, timeout=httpx.Timeout(timeout + 1, connect=timeout),
                                 follow_redirects=False) as c:
        found = await asyncio.gather(*(probe(c, h) for h in hosts))
    return [f for f in found if f]
