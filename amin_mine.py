#!/usr/bin/env python3
"""Agent Mining AMNT CPU miner (lane 2 = Plan B, 28 bit).

Proof: keccak256(abi.encode(seed, wallet, agentId, lane, day, nonce)) <= cpuTarget
- fixed 192-byte payload: seed(32) ‖ wallet(20) ‖ agentId(32) ‖ lane(32) ‖ day(32) ‖ nonce(32)
- nonce written BIG-ENDIAN in the last 32 bytes.

Usage: python3 amin_mine.py <nonce_start> <nonce_count> [max_seconds]
Prints FOUND nonce + hash on stdout, exits 0.
"""
import json, math, sys, time

sys.path.insert(0, "/tmp")
from keccak_mod import keccak256

AMNT = "0x2d07B9CcDF5FCdbE26167358B94eeb4c9E995e4E"
BAW = "0x888dB97d0dD291c5F0C99F417f143ca279AC29a1"
AGENT = 100163968864600285908130940375525972986039065382661818444081446679526346826826
LANE, DAY = 2, 0
SEED = int("1488c2b9cd6462bf7db63c585894f7bdc3654e8a0e9ba3e297c53a163f84d536", 16)

def cpu_target():
    import subprocess
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": AMNT, "data": "0x8129e010"}, "latest"]})
    out = subprocess.run(["curl", "-sk", "-m", "20", "-X", "POST",
                          "-H", "Content-Type: application/json", "--data", body,
                          "https://bsc-dataseed.bnbchain.org"], capture_output=True, text=True, timeout=30).stdout
    return int(json.loads(out)["result"], 16)

TARGET = cpu_target()
BITS = 256 - math.log2(TARGET)
# fixed 192-byte block: seed ‖ wallet ‖ agentId ‖ lane ‖ day ‖ [32 nonce bytes to be patched]
PAYLOAD = (b"%064x" % SEED) + (b"%040x" % int(BAW, 16)) + (b"%064x" % AGENT) + \
          (b"%08x" % LANE) + (b"%040x" % DAY)

def build_nonce(nonce):
    return PAYLOAD + (b"%064x" % nonce)

def main():
    start = int(sys.argv[1], 10)
    count = int(sys.argv[2], 10)
    secs = int(sys.argv[3], 10) if len(sys.argv) > 3 else 0
    t0 = time.time()
    end = start + count
    hits = 0
    while start < end and (secs <= 0 or time.time() - t0 < secs):
        h = keccak256(build_nonce(start))
        if int.from_bytes(bytes.fromhex(h), "big") < TARGET:
            print(json.dumps({"nonce": start, "hash": h.hex(),
                              "seconds": round(time.time() - t0, 2),
                              "rate": round(start / max(time.time() - t0, 1e-9), 0)}))
            sys.stdout.flush()
            hits += 1
            if hits >= 10: break
        start += 1
    sys.exit(0)

if __name__ == "__main__":
    main()
