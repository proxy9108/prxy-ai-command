"""Read-only clients for the lab's data sources. Agents never open raw HTTP themselves; add new sources here.
Every call has a timeout and raises SourceError on trouble so an agent can report 'I cannot see X' instead of guessing."""
import asyncio, os, math, time
import aiohttp
from . import trace


class SourceError(Exception):
    pass


async def _get(url, params=None, timeout=20, headers=None):
    kind = "loki" if "/loki/" in url else "prometheus" if "/api/v1/" in url else "http"
    ref = url.split("?")[0].split("/", 3)[-1] + (" " + str((params or {}).get("query", ""))[:200] if params else "")
    with trace.timed(kind, ref):
        return await _get_inner(url, params, timeout, headers)


async def _get_inner(url, params=None, timeout=20, headers=None):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status != 200:
                    raise SourceError(f"{url.split('?')[0]} answered HTTP {r.status}")
                return await r.json(content_type=None)
    except SourceError:
        raise
    except Exception as e:
        raise SourceError(f"{url.split('?')[0]}: {type(e).__name__}: {e}")


class Prom:
    def __init__(self, url=None):
        self.url = (url or os.getenv("PROM_URL", "http://10.20.40.116:9090")).rstrip("/")

    async def query(self, expr):
        """Instant query -> [{'metric': {...}, 'value': float}] (NaN rows dropped)."""
        d = await _get(self.url + "/api/v1/query", {"query": expr})
        if d.get("status") != "success":
            raise SourceError(f"Prometheus rejected the query: {expr[:80]}")
        out = []
        for r in d["data"]["result"]:
            v = float(r["value"][1])
            if not math.isnan(v):
                out.append({"metric": r["metric"], "value": v})
        return out

    async def range(self, expr, hours=24, step=300):
        end = time.time()
        d = await _get(self.url + "/api/v1/query_range", {"query": expr, "start": end - hours * 3600, "end": end, "step": step}, timeout=30)
        return [{"metric": r["metric"], "values": [(float(t), float(v)) for t, v in r["values"] if v != "NaN"]} for r in d["data"]["result"]]


class Loki:
    def __init__(self, url=None):
        self.url = (url or os.getenv("LOKI_URL", "http://10.20.40.133:3100")).rstrip("/")

    async def query(self, logql, minutes=60, limit=500, start=None, end=None, timeout=60):
        end = end or time.time()
        start = start or (end - minutes * 60)
        d = await _get(self.url + "/loki/api/v1/query_range", {"query": logql, "limit": limit, "direction": "backward",
                       "start": int(start * 1e9), "end": int(end * 1e9)}, timeout=timeout)
        lines = []
        for st in d["data"]["result"]:
            for ts, line in st["values"]:
                lines.append({"ts": int(ts) / 1e9, "labels": st["stream"], "line": line})
        return sorted(lines, key=lambda x: -x["ts"])

    async def label_values(self, label):
        return (await _get(self.url + f"/loki/api/v1/label/{label}/values")).get("data", [])


class PVE:
    """Read-only Proxmox API client (token with the PVEAuditor role; the server certificate is pinned by fingerprint)."""
    def __init__(self):
        self.url = os.getenv("PVE_URL", "https://10.20.40.224:8006").rstrip("/")
        self.node = os.getenv("PVE_NODE", "pve-node")
        tid, sec = os.getenv("PVE_TOKEN_ID", ""), os.getenv("PVE_TOKEN_SECRET", "")
        self.headers = {"Authorization": f"PVEAPIToken={tid}={sec}"} if tid and sec else {}
        fp = os.getenv("PVE_FINGERPRINT", "").replace(":", "")
        self.ssl = aiohttp.Fingerprint(bytes.fromhex(fp)) if len(fp) == 64 else False

    async def get(self, path, params=None):
        with trace.timed("proxmox", path):
            return await self._get(path, params)

    async def _get(self, path, params=None):
        if not self.headers:
            raise SourceError("Proxmox API token is not configured (PVE_TOKEN_ID / PVE_TOKEN_SECRET)")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.url}/api2/json/{path.lstrip('/')}", params=params, headers=self.headers, ssl=self.ssl,
                                 timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status != 200:
                        raise SourceError(f"Proxmox API {path} answered HTTP {r.status}")
                    return (await r.json()).get("data")
        except SourceError:
            raise
        except Exception as e:
            raise SourceError(f"Proxmox API {path}: {type(e).__name__}: {e}")



class PVEAct:
    """The ONLY write path into Proxmox. A separate token (PVE_ACT_TOKEN_ID / PVE_ACT_TOKEN_SECRET) that the owner creates with the minimum privileges for the
    actions in actionspec.py. Without it the executor cannot do anything: the read-only token above can only read."""
    def __init__(self):
        self.url = os.getenv("PVE_URL", "https://10.20.40.224:8006").rstrip("/")
        self.node = os.getenv("PVE_NODE", "pve-node")
        tid, sec = os.getenv("PVE_ACT_TOKEN_ID", ""), os.getenv("PVE_ACT_TOKEN_SECRET", "")
        self.headers = {"Authorization": f"PVEAPIToken={tid}={sec}"} if tid and sec else {}
        fp = os.getenv("PVE_FINGERPRINT", "").replace(":", "")
        self.ssl = aiohttp.Fingerprint(bytes.fromhex(fp)) if len(fp) == 64 else False

    def configured(self):
        return bool(self.headers)

    async def call(self, method, path, data=None):
        if not self.headers:
            raise SourceError("the action token is not configured (PVE_ACT_TOKEN_ID / PVE_ACT_TOKEN_SECRET)")
        with trace.timed("proxmox-write", f"{method} {path}"):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.request(method, f"{self.url}/api2/json/{path.lstrip('/')}", json=data, headers=self.headers, ssl=self.ssl, timeout=aiohttp.ClientTimeout(total=40)) as r:
                        if r.status == 403:
                            raise SourceError(f"Proxmox refused {method} {path}: the action token lacks the privilege for this (see Settings > Actions for what each action needs)")
                        if r.status != 200:
                            detail = (await r.text())[:160]
                            raise SourceError(f"Proxmox answered HTTP {r.status} to {method} {path}: {detail}")
                        return (await r.json()).get("data")
            except SourceError:
                raise
            except Exception as e:
                raise SourceError(f"Proxmox {method} {path}: {type(e).__name__}: {e}")

    async def wait_task(self, upid, timeout=90):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = await self.call("GET", f"nodes/{self.node}/tasks/{upid}/status")
            if st.get("status") == "stopped":
                if st.get("exitstatus") != "OK":
                    raise SourceError(f"the Proxmox task ended with: {st.get('exitstatus')}")
                return
            await asyncio.sleep(1.5)
        raise SourceError("the Proxmox task did not finish in time")


async def http_text(url, timeout=15):
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 PrxyCommand"}) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status != 200:
                    raise SourceError(f"{url.split('?')[0]} answered HTTP {r.status}")
                return await r.text(errors="replace")
    except SourceError:
        raise
    except Exception as e:
        raise SourceError(f"{url.split('?')[0]}: {type(e).__name__}: {e}")


async def weather(lat, lon):
    """Open-Meteo (no key). Rounded coordinates are enough for a daily forecast."""
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code,wind_speed_10m"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone=auto&forecast_days=1")
    import json as _json
    return _json.loads(await http_text(url))
