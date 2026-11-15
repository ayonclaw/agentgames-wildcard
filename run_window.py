#!/usr/bin/env python3
"""Agent Mining (AMNT) — Plan B (lane 2) window runner.

Mining window: 20:00–22:00 WIB daily (13:00–15:00 UTC). The CPU pool holds 20,000
proofs and fills fast, so this runs every minute inside the window and stops as soon
as our per-ID cap (20 proofs) is reached.

All hashing runs on Modal (C miner, verified 5/5 against the contract's proofDigest()).
This script only: checks state → pins the seed if needed → mines → simulates → submits.

Silent (no_agent cron): prints ONLY on a real submit or an actionable failure.
"""
import json
import os
import re
import signal
import subprocess
import sys
import urllib.request

RPC = "https://bsc-dataseed.bnbchain.org"
AMNT = "0x2d07B9CcDF5FCdbE26167358B94eeb4c9E995e4E"
ADAPTER = "0x3c44B8EE0581F70026a7b650eC02373e25EF3200"
WALLET = os.environ.get("AGENT_WALLET", "")   # agent EOA address; set AGENT_WALLET before running
LANE = 2
HERE = os.path.dirname(os.path.abspath(__file__))
SWARM = "/home/ubuntu/.hermes/profiles/ayon/scripts/agentmining/amin_swarm_modal.py"
STATE = "/tmp/amin_state.json"
PY = "/home/ubuntu/.hermes/tools/python-3.14.9+20261021-linux-arm64/bin/python3"
MAX_PASSES = 12          # Modal-spawn cap per day (cost guard)
SHARDS = 24              # Modal containers per pass (cpu=2 each -> 48 vCPU, 4x lighter than the old 24x cpu=8)
WANT_PROOFS = 3          # target proofs per pass; nonces/shard are sized from the LIVE difficulty
SWARM_TIMEOUT = 900      # Modal autoscale can queue containers; give the run room, then fail loudly
PASS_SLEEP = 45          # seconds between spawns inside one cron tick (anti-burst)

SEL = {
    "agentIdOf(address)": "e9c9ec73",
    "inSession()": "b7b2d036",
    "dayIndex()": "9646dec6",
    "sessionSeedBase(uint256)": "7666e2ae",
    "cpuTarget()": "8129e010",
    "cpuProofsToday()": "1b835fa4",
    "cpuDailyCap()": "7f634012",
    "cpuPerIdCap()": "e47c86b1",
    "cpuCountOfId(uint256)": "311e36ed",
    "cpuDayOfId(uint256)": "fa3a2967",
    "pinSession()": "9c26dd34",
    "mineBatch(uint8,uint256[])": "738c12e5",
    "effectiveLane(uint256)": "9388452a",
    "maxBatch()": "67765b87",
}


def rpc(payload):
    req = urllib.request.Request(RPC, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=25).read())


def call_hex(sig, args=(), to=AMNT):
    data = "0x" + SEL[sig] + "".join(hex(a)[2:].rjust(64, "0") for a in args)
    r = rpc({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
             "params": [{"to": to, "data": data}, "latest"]})
    return int(r["result"], 16) if "result" in r else None


def send(calldata, gas_hint=200000):
    """Preview + execute through baw. Returns tx hash or None."""
    pv = subprocess.run(["baw", "contract-call", "preview", "--binanceChainId", "56",
                         "--from", WALLET, "--to", AMNT, "--value", "0",
                         "--inputData", calldata, "--gasLimit", str(gas_hint),
                         "--json"], capture_output=True, text=True, timeout=120).stdout
    m = re.search(r'"requestId":\s*"([^"]+)"', pv)
    if not m:
        return None, pv[:160]
    ex = subprocess.run(["baw", "contract-call", "execute", "--requestId", m.group(1),
                         "--json"], capture_output=True, text=True, timeout=180).stdout
    t = re.search(r'"txHash":\s*"(0x[0-9a-f]+)"', ex)
    return (t.group(1) if t else None), ex[:160]


def release_lock():
    try:
        os.remove("/tmp/amin_run.lock")
    except OSError:
        pass


def acquire_lock():
    """Overlapping runs share the same offset and re-mine the same range. The cron fires every
    4 minutes while a pass can take longer, so a lock is mandatory, not optional."""
    lock = "/tmp/amin_run.lock"
    pid = os.getpid()
    if os.path.exists(lock):
        try:
            old = int(open(lock).read().strip())
            os.kill(old, 0)                       # signal 0: alive check only
            # A bare PID check is not enough: PIDs get recycled, and a `timeout`-killed parent can
            # leave the lock behind. Confirm the PID really is our script before deferring.
            cmd = open(f"/proc/{old}/cmdline", "rb").read().decode(errors="replace")
            if "run_window.py" in cmd and time.time() - os.path.getmtime(lock) < SWARM_TIMEOUT + 120:
                return False                      # another pass is legitimately running
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass                                  # dead / recycled / unreadable -> stale, take it
    open(lock, "w").write(str(pid))
    return True


def main():
    import atexit
    if not acquire_lock():
        return 0                                  # silent: a sibling pass owns the window
    atexit.register(release_lock)
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: sys.exit(0))   # -> atexit runs -> lock released
        except (ValueError, OSError):
            pass
    dry = "--dry" in sys.argv
    st = {}
    if os.path.exists(STATE):
        try:
            st = json.load(open(STATE))
        except Exception:
            st = {}

    sess = call_hex("inSession()")
    day = call_hex("dayIndex()")
    cap = call_hex("cpuDailyCap()")
    used = call_hex("cpuProofsToday()")
    if None in (sess, day, cap, used):
        return 0                                  # RPC wobble -> silent
    if st.get("day") != day:
        st = {"day": day, "submitted": 0, "passes": 0, "txs": []}
    if not sess or used >= cap:
        json.dump(st, open(STATE, "w"))
        return 0                                  # window shut / pool full -> silent

    per_id = call_hex("cpuPerIdCap()") or 20
    agent_id = call_hex("agentIdOf(address)", (int(WALLET, 16),), to=ADAPTER)
    if not agent_id:
        return 0
    # cpuCountOfId is a LIFETIME counter; the contract resets it lazily on the first mine of a
    # new day and stamps cpuDayOfId with that day. Guarding on the raw lifetime count silently
    # killed every day-2 run (lifetime 20 >= cap 20) even though the daily allowance was fresh.
    # Proof the cap is per-day: other agentIds show lifetime counts above 20 (e.g. 35).
    lifetime = call_hex("cpuCountOfId(uint256)", (agent_id,)) or 0
    day_of_id = call_hex("cpuDayOfId(uint256)", (agent_id,)) or 0
    mine_today = lifetime if day_of_id == day else 0
    if mine_today >= per_id:
        json.dump(st, open(STATE, "w"))
        return 0                                  # today's cap done -> silent

    if call_hex("effectiveLane(uint256)", (agent_id,)) != LANE:
        return 0                                  # not on Plan B

    # --- pin the seed ourselves if nobody has yet ---
    seed = call_hex("sessionSeedBase(uint256)", (day,))
    if not seed:
        if dry:
            print(f"AMNT: seed unpinned for day {day} (pinSession needed)")
            return 0
        tx, msg = send("0x" + SEL["pinSession()"], 150000)
        if not tx:
            print(f"AMNT: pinSession failed — {msg}")
            return 0
        seed = call_hex("sessionSeedBase(uint256)", (day,))
        if not seed:
            print("AMNT: seed still unpinned after pinSession")
            return 0

    want = min(per_id - mine_today, 16)
    if want <= 0:
        return 0                                  # per-ID cap reached -> silent
    if dry:
        print(f"AMNT day {day} OPEN | pool {used}/{cap} | ours {mine_today}/{per_id} | want {want}")
        return 0
    if st.get("passes", 0) >= MAX_PASSES:
        return 0                                  # cost guard -> silent

    st["passes"] = st.get("passes", 0) + 1
    json.dump(st, open(STATE, "w"))

    # --- mine on Modal ---
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    # bits derived from the LIVE target, not a constant: the target moves on retarget
    target = call_hex("cpuTarget()") or 0
    bits = 256 - target.bit_length() + 1 if target else 28
    env["AMIN_SEED"] = format(seed, "064x")
    env["AMIN_DAY"] = str(day)
    # Nonces per shard must be sized from the LIVE difficulty: at 29 bits a proof takes ~5.4e8
    # attempts, so a fixed 64M/shard x 10 shards expects only 1.25 proofs and often finds zero.
    # Ask for WANT_PROOFS with headroom, capped so a retarget cannot explode the run.
    per = int(2 ** bits * WANT_PROOFS * 1.5 / SHARDS)
    per = max(67108864, min(per, 536870912))
    # CRITICAL: the shard range must advance every pass. Without --offset each pass re-mines the
    # same nonce window and returns proofs already submitted, which revert on-chain (nonce spent).
    base = st.get("offset", 0)
    used = set(st.get("used", []))
    # advance past the range we are about to mine so the next pass searches fresh nonces
    st["offset"] = base + SHARDS * per
    json.dump(st, open(STATE, "w"))
    try:
        r = subprocess.run([PY, "-m", "modal", "run", SWARM, "--shards", str(SHARDS),
                            "--per", str(per), "--bits", str(bits), "--offset", str(base),
                            "--want", str(want)],
                           cwd=os.path.dirname(SWARM), capture_output=True, text=True,
                           timeout=SWARM_TIMEOUT, env=env)
        out = r.stdout
    except subprocess.TimeoutExpired as e:
        # never fail silently: a timed-out swarm is the difference between "no luck" and "broken"
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        tail = [l for l in out.splitlines() if l.strip()][-3:]
        print(f"AMNT: swarm timeout after {SWARM_TIMEOUT}s (day {day}, {SHARDS} shards x {per:,})")
        for l in tail:
            print("  " + l[:160])
        return 0
    proofs = []
    for line in out.splitlines():
        if line.startswith("RESULT "):
            proofs = json.loads(line[7:]).get("proofs", [])
            break
    proofs = [p for p in proofs if int(p["nonce"]) not in used]
    if not proofs:
        return 0                                  # only stale proofs -> silent

    # --- simulate + submit ---
    batch = proofs[:10]
    arg = "".join(hex(int(p["nonce"]))[2:].rjust(64, "0") for p in batch)
    data = ("0x" + SEL["mineBatch(uint8,uint256[])"] + hex(LANE)[2:].rjust(64, "0")
            + hex(64)[2:].rjust(64, "0") + hex(len(batch))[2:].rjust(64, "0") + arg)
    try:
        est = rpc({"jsonrpc": "2.0", "id": 1, "method": "eth_estimateGas",
                   "params": [{"from": WALLET, "to": AMNT, "data": data, "value": "0x0"}, "latest"]})
        gas = int(est["result"], 16)
    except Exception:
        return 0                                  # stale proof / pool filled -> silent

    tx, msg = send(data, int(gas * 1.5))
    if not tx:
        print(f"AMNT: submit failed — {msg}")
        return 0

    st["submitted"] = st.get("submitted", 0) + len(batch)
    st["txs"] = (st.get("txs") or []) + [tx]
    json.dump(st, open(STATE, "w"))
    print(f"AMNT MINED — {len(batch)} proof(s) = {2500 * len(batch):,} AMNT (day {day})")
    print(f"https://bscscan.com/tx/{tx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
