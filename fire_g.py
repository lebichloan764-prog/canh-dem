"""Aave v3 Base liquidation gun - GENERIC driver (any collateral, any debt asset).

fork : pin fork, fund the real wallet, drop the collateral oracle, run the exact
       mainnet path with real signed transactions.
main : no oracle touch; every swap candidate is dry-run with eth_call first.

usage: python fire_g.py fork <rpc> <user> [frac] [hf_target]
       python fire_g.py main <user> [frac]
"""
import json, os, sys, time, urllib.request
from eth_account import Account
from eth_utils import to_checksum_address, keccak
from revert_decode import decode_revert

MODE = sys.argv[1]
RPC = sys.argv[2] if MODE == "fork" else (os.environ.get("FIRE_RPC") or "https://base-rpc.publicnode.com")
CHAIN = 8453
POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
PROVIDER = "0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D"
UI = "0x68100bD5345eA474D93577127C11F39FF8463e93"
ORACLE = "0x2Cc0Fc26eD4563A5ce5e8bdcfe1A2878676Ae156"
WETH = "0x4200000000000000000000000000000000000006"
RUN_SEL = "0xb13ea98c"
SETSWAP_SEL = "0x7115f099"
KS = (1.0, 0.998, 0.995, 0.99, 0.985, 0.98, 0.97, 0.95, 0.92)
SEIZE_CAP_USD = 1_800_000
GET_USER_RES = "0x" + keccak(text="getUserReservesData(address,address)").hex()[:8]

if MODE == "fork":
    USER = sys.argv[3]
    FRAC = float(sys.argv[4]) if len(sys.argv) > 4 else 0.5
    HF_TARGET = float(sys.argv[5]) if len(sys.argv) > 5 else 0.98
else:
    USER = sys.argv[2]
    FRAC = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    HF_TARGET = 0.98

KEY = os.environ["DSH_SECRET_EVM_PRIVATE_KEY"]
ME = Account.from_key(KEY).address


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(RPC, body, {"Content-Type": "application/json",
                                             "User-Agent": "anvil/1.0"})
    r = json.load(urllib.request.urlopen(req, timeout=180))
    if "error" in r:
        raise RuntimeError(f"{method}: {r['error']}")
    return r.get("result")


def call(to, data, frm=None):
    o = {"to": to, "data": data}
    if frm:
        o["from"] = frm
    d = rpc("eth_call", [o, "latest"])
    if d in (None, "0x"):
        raise RuntimeError("empty return from %s" % to)
    return d


def num(to, data):
    r = rpc("eth_call", [{"to": to, "data": data}, "latest"])
    return int(r, 16) if r and r != "0x" else 0


def acc(u):
    d = call(POOL, "0xbf92857c" + u[2:].rjust(64, "0"))
    w = [int(d[2 + i * 64: 2 + (i + 1) * 64], 16) for i in range(6)]
    return {"coll": w[0] / 1e8, "debt": w[1] / 1e8, "hf": w[5] / 1e18,
            "lt": w[3]}


def reserve(res):
    """(config, aToken, vToken)"""
    d = call(POOL, "0x35ea6a75" + res[2:].rjust(64, "0"))
    w = [int(d[2 + i * 64: 2 + (i + 1) * 64], 16) for i in range(15)]
    return w[0], "0x" + hex(w[8])[2:].rjust(40, "0"), "0x" + hex(w[10])[2:].rjust(40, "0")


def position(u):
    """pick the collateral and the debt reserve with the biggest USD value"""
    d = call(UI, GET_USER_RES + PROVIDER[2:].rjust(64, "0") + u[2:].rjust(64, "0"))
    w = []
    h = d[2:]
    for i in range(0, len(h), 64):
        w.append(int(h[i:i + 64], 16))
    off = w[0] // 32
    cnt = w[off]
    emode = w[1]
    best_c = (0, None, 0)
    best_d = (0, None, 0)
    for i in range(cnt):
        o = off + 1 + i * 4
        res = "0x" + hex(w[o])[2:].rjust(40, "0")
        a, used, v = w[o + 1], w[o + 2], w[o + 3]
        if not a and not v:
            continue
        cfg = reserve(res)[0]
        dec = (cfg >> 48) & 0xFF
        if dec > 18:
            continue
        p = num(ORACLE, "0xb3596f07" + res[2:].rjust(64, "0")) if (used and a) or v else 0
        uval = a * p // 10 ** dec if used and a else 0
        dval = v * p // 10 ** dec if v else 0
        if uval > best_c[0]:
            best_c = (uval, res, a)
        if dval > best_d[0]:
            best_d = (dval, res, v)
    if not best_c[1] or not best_d[1]:
        raise RuntimeError("no collateral/debt pair")
    return best_c[1], best_d[1], emode, best_c[2], best_d[2]


def emode_cfg(cat):
    """liquidation bonus of an e-mode category, 0 when the category has none"""
    if cat == 0:
        return 0
    d = call(POOL, "0x6c6f6ae1" + hex(cat)[2:].rjust(64, "0"))
    h = d[2:]
    w = [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]
    off = w[0] // 32
    # fields at offset: ltv, liquidationThreshold, liquidationBonus, priceSource
    return w[off + 2]


def wait(h, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = rpc("eth_getTransactionReceipt", [h])
        if r:
            return r
        time.sleep(0.4)
    raise RuntimeError("no receipt " + h)


def send(tx):
    gp = int(rpc("eth_gasPrice", []), 16)
    tx = dict(tx)
    tx.update({"gasPrice": gp, "nonce": rpc("eth_getTransactionCount", [ME, "pending"]),
               "chainId": CHAIN, "value": 0})
    if tx.get("to"):
        tx["to"] = to_checksum_address(tx["to"])
    s = Account.sign_transaction(tx, KEY)
    return rpc("eth_sendRawTransaction", ["0x" + s.raw_transaction.hex().removeprefix("0x")])


def kyber(gun, tin, tout, amt):
    hdr = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    u = ("https://aggregator-api.kyberswap.com/base/api/v1/routes?tokenIn=%s&tokenOut=%s"
         "&amountIn=%s&to=%s&saveGas=0&gasInclude=1") % (tin, tout, amt, gun)
    r = json.load(urllib.request.urlopen(urllib.request.Request(u, headers=hdr), timeout=60))
    d = r["data"]
    body = {"routeSummary": d["routeSummary"], "sender": gun, "recipient": gun,
            "slippageTolerance": 150, "deadline": int(time.time()) + 900, "clientData": "{}"}
    t = json.load(urllib.request.urlopen(urllib.request.Request(
        "https://aggregator-api.kyberswap.com/base/api/v1/route/build",
        json.dumps(body).encode(), hdr), timeout=120))["data"]
    return t["routerAddress"], t["routerAddress"], t["data"], int(d["routeSummary"]["amountOut"])


def setswap(gun, target, spender, data):
    d = data[2:]
    cd = SETSWAP_SEL + target[2:].rjust(64, "0") + spender[2:].rjust(64, "0") + \
        hex(0x60)[2:].rjust(64, "0") + hex(len(d) // 2)[2:].rjust(64, "0") + d
    gas = min(25_000_000, 600_000 + (len(d) // 2) * 700)
    return wait(send({"to": gun, "data": cd, "gas": gas}))


def run_data(coll, debt, amount):
    w = [USER[2:].rjust(64, "0"), coll[2:].rjust(64, "0"), debt[2:].rjust(64, "0"),
         "0" * 64, "0" * 64, hex(2)[2:].rjust(64, "0"), ME[2:].rjust(64, "0"),
         hex(amount)[2:].rjust(64, "0")]
    return RUN_SEL + "".join(w)


def main():
    print("wallet", ME, "eth", int(rpc("eth_getBalance", [ME, "latest"]), 16) / 1e18)
    print("BEFORE", acc(USER))
    coll, debt, emode, _cbal, _dbal = position(USER)
    ccfg, aTok, _ = reserve(coll)
    dcfg, _, vTok = reserve(debt)
    cdec = (ccfg >> 48) & 0xFF
    print("coll", coll, "debt", debt, "emode", emode, "aTok", aTok, "vTok", vTok)
    if cdec > 18 or aTok == "0x" + "0" * 40:
        print("no usable aToken / bad decimals, abort")
        return

    ecfg = emode_cfg(emode)
    # fork-measured: the liquidator keeps the whole bonus, e-mode pays its own
    # category bonus and the protocol takes no cut either way
    bonus = ecfg if ecfg > 10000 else (ccfg >> 32) & 0xFFFF
    pfee = 0
    print("bonus bps", bonus, "protocol fee bps", pfee)
    if bonus <= 10000:
        print("no bonus, abort")
        return

    if MODE == "fork":
        a = acc(USER)
        price = num(ORACLE, "0xb3596f07" + coll[2:].rjust(64, "0"))
        fake = int(price * HF_TARGET / a["hf"])
        slot = "0x" + keccak(hexstr="0x" + coll[2:].rjust(64, "0") + "0" * 64).hex()
        art = json.load(open("gun/out/Gun.sol/FakeAgg.json", encoding="utf-8"))["bytecode"]["object"]
        r = wait(send({"data": art + hex(fake)[2:].rjust(64, "0"), "gas": 400000}))
        rpc("anvil_setStorageAt", [ORACLE, slot, "0x" + r["contractAddress"][2:].rjust(64, "0")])
        a = acc(USER)
        print("AFTER DROP hf", a["hf"])
        if a["hf"] >= 1.0:
            print("not liquidatable, abort")
            return

    g = wait(send({"data": json.load(open("gun/out/Gun.sol/Gun.json",
                                          encoding="utf-8"))["bytecode"]["object"],
                   "gas": 3500000}))
    gun = g["contractAddress"]
    print("gun", gun)

    dbal = num(vTok, "0x70a08231" + USER[2:].rjust(64, "0"))
    cbal = num(aTok, "0x70a08231" + USER[2:].rjust(64, "0"))
    dp = num(ORACLE, "0xb3596f07" + debt[2:].rjust(64, "0"))
    cp = num(ORACLE, "0xb3596f07" + coll[2:].rjust(64, "0"))
    ddec = (dcfg >> 48) & 0xFF
    # cap the shot by USD value: aggregator depth dies past this on thin pairs
    cap = SEIZE_CAP_USD * 10 ** 8 * 10 ** ddec // dp if dp else dbal
    amount = min(int(dbal * FRAC), cap)
    # protocol fee is taken out of the bonus only, not out of the whole seize
    profit_bps = (bonus - 10000) * (10000 - pfee) // 10000
    value = amount * dp // 10 ** ddec                      # covered debt in USD (1e8)
    seize = value * (10000 + profit_bps) // 10000 * 10 ** cdec // cp
    if seize > cbal:
        seize = cbal
    print("debt", dbal, "cover", amount, "seize est", seize,
          "profit est", profit_bps, "bps")
    if seize == 0:
        print("zero seize, abort")
        return
    same = coll == debt
    rd = run_data(coll, debt, amount)

    for k in KS:
        amt = int(seize * k)
        if amt > cbal:
            amt = cbal
        if same:
            tgt, sp, dat, out = coll, coll, "0x", amt
        else:
            try:
                tgt, sp, dat, out = kyber(gun, coll, debt, amt)
            except Exception as e:
                print("k=%.2f quote fail %s" % (k, str(e)[:90]))
                continue
        sr = setswap(gun, tgt, sp, dat)
        if int(sr["status"], 16) != 1:
            print("k=%.2f setSwap failed" % k)
            continue
        print("k=%.2f swap %d expect %d setSwap gas %d" % (k, amt, out, int(sr["gasUsed"], 16)))
        try:
            rpc("eth_call", [{"from": ME, "to": gun, "data": rd, "gas": "0x1c9c380"}, "latest"])
        except Exception as e:
            print("   dry-run revert", decode_revert(e))
            continue
        print("   dry-run OK, firing")
        before = num(debt, "0x70a08231" + ME[2:].rjust(64, "0"))
        gas = int(rpc("eth_estimateGas", [{"from": ME, "to": gun, "data": rd}]), 16)
        lim = min(25_000_000, max(int(gas * 2) + 600_000, gas + 3_000_000))
        r = wait(send({"to": gun, "data": rd, "gas": lim}))
        print("FIRE status", int(r["status"], 16), "gasUsed", int(r["gasUsed"], 16), "tx", r["transactionHash"])
        after = num(debt, "0x70a08231" + ME[2:].rjust(64, "0"))
        dec = 10 ** ddec
        print("PROFIT", debt, (after - before) / dec)
        print("AFTER", acc(USER))
        print("gun coll left", num(coll, "0x70a08231" + gun[2:].rjust(64, "0")))
        return
    print("no candidate passed")


main()


