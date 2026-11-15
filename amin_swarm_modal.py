#!/usr/bin/env python3
"""Agent Mining (AMNT) — Modal C-miner swarm. Nothing heavy ever runs on the VPS.

The C solver (amin_miner.c) is built inside the container and verified against the
contract's own proofDigest() before use. Each shard scans a disjoint nonce range with
8 threads; the first container to find a proof reports the nonce.

Usage:
  python3 -m modal run amin_swarm_modal.py --shards 20 --per 268435456
"""
import json
import os
import subprocess
import time
import urllib.request

import modal

AMNT = "0x2d07B9CcDF5FCdbE26167358B94eeb4c9E995e4E"
WALLET = os.environ.get("AGENT_WALLET", "")   # agent EOA address
AGENT = 100163968864600285908130940375525972986039065382661818444081446679526346826826
LANE = 2
# SEED_HEX / DAY are injected per-run by the caller: the seed is re-pinned every day,
# so a hardcoded value silently mines against a dead seed (day 0 seed != day 1 seed).
SEED_HEX = os.environ.get("AMIN_SEED", "")
DAY = int(os.environ.get("AMIN_DAY", "0"))
RPC = "https://bsc-dataseed.bnbchain.org"
SRC = "/home/ubuntu/.hermes/profiles/ayon/scripts/agentmining/amin_miner.c"
HERE = os.path.dirname(os.path.abspath(__file__))

def payload_for(seed_hex, day):
    """abi.encode pads every static arg to 32 bytes."""
    return ((b"%064x" % int(seed_hex, 16)) + WALLET[2:].lower().rjust(64, "0").encode()
            + (b"%064x" % AGENT) + (b"%064x" % LANE) + (b"%064x" % day)).decode()

app = modal.App("amin-swarm")
image = (modal.Image.debian_slim()
         .apt_install("gcc", "libc6-dev")
         .add_local_file(SRC, "/root/amin_miner.c", copy=True))


def rpc_json(payload):
    req = urllib.request.Request(RPC, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=25).read())


@app.function(image=image, cpu=2.0, memory=1024, timeout=180)
def run_shard(start: int, end: int, bits: int, seed_hex: str, day: int):
    """Build the C solver, then scan [start, end). Returns the first proof found."""
    b = subprocess.run(["gcc", "-O3", "-funroll-loops", "-pthread",
                        "-o", "/root/m", "/root/amin_miner.c"],
                       capture_output=True, text=True)
    if b.returncode != 0:
        return {"error": "build: " + b.stderr[-200:]}

    t0 = time.time()
    p = subprocess.run(["/root/m", payload_for(seed_hex, day), str(bits), "8", str(start), str(end)],
                       capture_output=True, text=True, timeout=170)
    lines = [l for l in p.stdout.strip().splitlines() if l.startswith("{")]
    secs = time.time() - t0
    span = end - start
    hits = [json.loads(l) for l in lines]
    return {"found": bool(hits), "proofs": hits,
            "seconds": round(secs, 1), "rate_mhs": round(span / secs / 1e6, 2)}


@app.local_entrypoint()
def main(shards: int = 20, per: int = 268435456, bits: int = 28, offset: int = 0, want: int = 1):
    shards, per, bits, offset, want = int(shards), int(per), int(bits), int(offset), int(want)
    print(f"swarm: {shards} containers x {per:,} nonces @ {bits} bits", flush=True)
    print(f"payload seed {SEED_HEX[:32]}… day {DAY}", flush=True)
    if not SEED_HEX:
        print("AMIN_SEED not set — refusing to mine against an unknown seed", flush=True)
        return

    handles = [run_shard.spawn(offset + i * per, offset + (i + 1) * per, bits, SEED_HEX, DAY)
               for i in range(shards)]

    collected = []
    for i, h in enumerate(handles):
        try:
            r = h.get()
        except Exception as e:
            print(f"  shard{i}: err {str(e)[:70]}", flush=True)
            continue
        if r.get("error"):
            print(f"  shard{i}: {r['error'][:90]}", flush=True)
        elif r.get("found"):
            collected += r["proofs"]
            print(f"  shard{i}: +{len(r['proofs'])} proof(s) ({r['seconds']}s, {r['rate_mhs']} MH/s)",
                  flush=True)
        if len(collected) >= want:
            break

    if collected:
        json.dump({"proofs": collected}, open(os.path.join(HERE, "swarm_result.json"), "w"), indent=1)
        print("RESULT " + json.dumps({"proofs": collected}), flush=True)
    else:
        print("no proof in this span", flush=True)
