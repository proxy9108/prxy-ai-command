"""Privacy scrubbing for the 24 h rule: once evidence expires, outside IP addresses and MAC addresses are masked in the text that stays behind.
Private (RFC1918) addresses describe our own infrastructure and are kept."""
import ipaddress, re

_IP = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
_MAC = re.compile(r"\b([0-9a-fA-F]{2}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2}):[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\b")


def scrub(text: str) -> str:
    def ip(m):
        try:
            a = ipaddress.ip_address(m.group(0))
        except ValueError:
            return m.group(0)
        return m.group(0) if (a.is_private or a.is_loopback) else f"{m.group(1)}.{m.group(2)}.x.x"
    return _MAC.sub(lambda m: f"{m.group(1)}:{m.group(2)}:{m.group(3)}:xx:xx:xx", _IP.sub(ip, text or ""))
