"""Rebuild hot.json / fast.json from live chain state.

hot.json  = every borrower with HF < 1.15 (watched slowly)
fast.json = subset whose collateral class differs from its debt class
            (ETH/BTC collateral vs USD debt), i.e. actually sensitive to a price move.
            cbETH-coll / WETH-debt loops are NOT price sensitive: both legs fall together.
"""
import json, urllib.request
from concurrent.futures import ThreadPoolExecutor
from eth_utils import keccak

R = "https://base-rpc.publicnode.com"
UI = "0x68100bD5345eA474D93577127C11F39FF8463e93"
POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
PROVIDER = "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D"
SEL = "0x" + keccak(text="getUserReservesData(address,address)").hex()[:8]
HOT = 1.15
MIN_DEBT = 1500

CLASS = {
    "0x4200000000000000000000000000000000000006": "ETH",
    "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452": "ETH",
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": "ETH",
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": "BTC",
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USD",
    "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42": "USD",
}


def rpc(m, p, t=30):
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode()
    for _ in range(3):
        try:
            d = json.loads(urllib.request.urlopen(urllib.request.Request(
                R, b, {"Content-Type": "application/json", "User-Agent": "anvil/1.0"}), timeout=t).read())
            return d.get("result")
        except Exception:
            pass
    return None


def hf_of(u):
    r = rpc("eth_call", [{"to": POOL, "data": "0xbf92857c" + u[2:].rjust(64, "0")}, "latest"])
    if not r or len(r) < 2 + 6 * 64:
        return None
    w = r[2:]
    return int(w[5 * 64:6 * 64], 16) / 1e18, int(w[1 * 64:2 * 64], 16) / 1e8


def classes(u):
    d = rpc("eth_call", [{"to": UI, "data": SEL + PROVIDER[2:].rjust(64, "0") + u[2:].rjust(64, "0")}, "latest"])
    if not d or len(d) < 200:
        return set(), set()
    h = d[2:]
    w = [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]
    off = w[0] // 32
    n = w[off]
    col, debt = set(), set()
    for i in range(n):
        b = off + 1 + i * 4
        a = "0x" + hex(w[b])[2:].rjust(40, "0")
        c = CLASS.get(a, "?")
        if w[b + 2]:
            col.add(c)
        if w[b + 3]:
            debt.add(c)
    return col, debt


def main():
    users = sorted({(x["u"] if isinstance(x, dict) else x).lower() for x in json.load(open("borrowers_all.json"))})
    rows = []
    with ThreadPoolExecutor(16) as ex:
        for u, x in zip(users, ex.map(hf_of, users)):
            if x and 0 < x[0] < HOT and x[1] >= MIN_DEBT:
                rows.append({"u": u, "hf": x[0], "debt": x[1]})
    rows.sort(key=lambda r: r["hf"])
    fast = []
    with ThreadPoolExecutor(16) as ex:
        cls = list(ex.map(lambda r: classes(r["u"]), rows))
    for r, (col, debt) in zip(rows, cls):
        r["coll"] = sorted(col)
        r["debtk"] = sorted(debt)
        if col and debt and col != debt:
            fast.append({"u": r["u"], "hf": r["hf"], "debt": r["debt"]})
    json.dump(rows, open("hot.json", "w"), indent=1)
    json.dump(fast, open("fast.json", "w"), indent=1)
    print("hot", len(rows), "fast", len(fast))
    for r in fast[:10]:
        print(round(r["hf"], 5), r["u"], round(r["debt"]))
    print("dead-neutral hot", len(rows) - len(fast))


main()
