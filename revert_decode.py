import re


def decode_revert(msg):
    m = re.search(r"0x08c379a0[0-9a-fA-F]*", str(msg))
    if not m:
        return str(msg)[:130]
    h = m.group(0)[10:]
    try:
        ln = int(h[64:128], 16)
        return "REVERT: " + bytes.fromhex(h[128:128 + ln * 2]).decode(errors="replace")
    except Exception:
        return str(msg)[:130]
