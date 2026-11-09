"""Fast Aave v3 Base liquidation watcher.

Improvements over autofire.py:
  * hot addresses (HF < 1.07) are polled every ~1.5s, the full borrower list only every ~20s
  * shots are launched non-blocking, so the loop keeps watching while a shot runs
  * per-user cooldown + running-set dedupe

usage: python autofire2.py
"""
import json, os, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

R = "https://base-rpc.publicnode.com"
POOL = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
LOG = "autofire2.log"
MIN_DEBT_USD = 2000
HOT_HF = 1.07
FAST_SLEEP = 1.5
FULL_EVERY = 14          # full sweep every ~20s
RELOAD_EVERY = 80        # reload lists every ~2 min
COOLDOWN = 150
WORKERS = 16
MAX_PARALLEL = 3
MAX_ROUNDS = 4
STATE = "fired.json"

FILES = ["hot.json", "borrowers_all.json", "aave_borrowers.json"]


def rpc(m, p, t=20):
    b = json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode()
    for _ in range(3):
        try:
            d = json.loads(urllib.request.urlopen(urllib.request.Request(
                R, b, {"Content-Type": "application/json", "User-Agent": "anvil/1.0"}), timeout=t).read())
            return d.get("result")
        except Exception:
            time.sleep(0.3)
    return None


def note(s):
    line = "[%s] %s" % (time.strftime("%m-%d %H:%M:%S"), s)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf8") as f:
        f.write(line + "\n")


def hf_of(u):
    r = rpc("eth_call", [{"to": POOL, "data": "0xbf92857c" + u[2:].rjust(64, "0")}, "latest"])
    if not r or len(r) < 2 + 6 * 64:
        return None
    w = r[2:]
    return int(w[5 * 64:6 * 64], 16) / 1e18, int(w[1 * 64:2 * 64], 16) / 1e8


def load_users():
    users, hot = set(), set()
    for f in FILES:
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for x in d:
            u = (x["u"] if isinstance(x, dict) else x).lower()
            users.add(u)
    # fast set = price-sensitive candidates only (collateral class != debt class)
    try:
        for x in json.load(open("fast.json")):
            users.add(x["u"].lower())
            if x.get("hf", 9) < HOT_HF:
                hot.add(x["u"].lower())
    except Exception:
        pass
    return sorted(users), sorted(hot)


def main():
    from eth_account import Account
    users, hot = load_users()
    hot = set(hot)
    bal = Account.from_key(os.environ["DSH_SECRET_EVM_PRIVATE_KEY"]).address.lower()
    note("autofire2 start users=%d hot=%d wallet=%s" % (len(users), len(hot), bal))

    running = {}          # user -> (Popen, logpath, started_ts)
    cool = {}             # user -> ts
    rounds = {}           # user -> rounds fired this episode
    fastview = set(hot)

    def check(u):
        return u, hf_of(u)

    with ThreadPoolExecutor(WORKERS) as ex:
        cycle = 0
        while True:
            cycle += 1
            if cycle % RELOAD_EVERY == 0:
                if cycle % (RELOAD_EVERY * 4) == 0:
                    subprocess.Popen([sys.executable, "refresh.py"],
                                     stdout=open("refresh.log", "a"), stderr=subprocess.STDOUT)
                time.sleep(2)
                users, hot = load_users()
                hot = set(hot)
                fastview = set(hot) | {u for u, t in cool.items() if time.time() - t < 900}
                note("reload users=%d hot=%d" % (len(users), len(hot)))

            # collect finished shots
            for u, (p, lp, ts) in list(running.items()):
                if p.poll() is not None:
                    running.pop(u)
                    try:
                        out = open(lp, encoding="utf8", errors="replace").read()[-700:]
                    except Exception:
                        out = ""
                    note("result %s rc=%d\n%s" % (u, p.returncode, out))
                    with open(STATE, "a", encoding="utf8") as f:
                        f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "user": u,
                                            "rc": p.returncode, "out": out[-600:]}) + "\n")
                    cool[u] = time.time()

            targets = sorted(fastview) if cycle % FULL_EVERY else sorted(users)
            found = []
            for u, x in ex.map(check, targets):
                if not x:
                    continue
                hf, debt = x
                if hf >= HOT_HF:
                    fastview.discard(u)
                    continue
                fastview.add(u)
                if 0 < hf < 1.0 and debt >= MIN_DEBT_USD:
                    found.append((hf, debt, u))

            if found:
                found.sort()
                note("HOT %s" % json.dumps([(round(h, 5), int(d), u) for h, d, u in found[:5]]))
                for hf, debt, u in found:
                    if len(running) >= MAX_PARALLEL or u in running:
                        continue
                    if time.time() - cool.get(u, 0) < COOLDOWN:
                        continue
                    if rounds.get(u, 0) >= MAX_ROUNDS:
                        continue
                    ethb = rpc("eth_getBalance", [bal, "latest"])
                    eth = int(ethb, 16) / 1e18 if ethb else 0
                    if eth < 0.0004:
                        note("low gas %.6f ETH" % eth)
                        break
                    lp = "fire_%s_%s.log" % (u[:10], time.strftime("%H%M%S"))
                    rounds[u] = rounds.get(u, 0) + 1
                    note("fire %s hf=%.5f debt$=%d round=%d" % (u, hf, debt, rounds[u]))
                    with open(lp, "w", encoding="utf8") as f:
                        p = subprocess.Popen([sys.executable, "fire_g.py", "main", u],
                                             stdout=f, stderr=subprocess.STDOUT)
                    running[u] = (p, lp, time.time())
            elif cycle % 20 == 0:
                note("clear fast=%d users=%d run=%d" % (len(fastview), len(users), len(running)))

            time.sleep(FAST_SLEEP)


main()
