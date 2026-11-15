# ProofHunter — a verifiable on-chain agent for Agent Games

**Track:** Wildcard (Agent Games by zkVerify, Season 01)

An autonomous agent that mines **ZK-verifiable proofs** on BNB Chain and submits them
on-chain. Not a mock: this agent has produced confirmed proofs against a live contract.

## What it does

`agentmining.fun` pays for *proofs of work*: an agent must find nonces whose
`keccak256` digest meets a target, then submit them in a batch transaction. The
contract verifies each proof before crediting the reward. An agent that merely
*claims* it mined gets nothing — the chain decides.

This agent:

1. Reads the live session seed and difficulty from the contract (`sessionSeedBase(dayIndex())`,
   `cpuTarget()`) — never hardcodes them.
2. Spawns a nonce-search swarm (Modal containers, `cpu=2 / 1GB`) sized from the
   **live** difficulty so expected output per pass is predictable.
3. Validates every candidate against `proofDigest()` before spending gas.
4. Simulates the batch (`eth_estimateGas`), then submits `mineBatch(lane, nonces[])`.
5. Persists state (day, nonce offset, used-nonce set, tx hashes) so restarts and
   overlapping cron ticks cannot duplicate work.

## Why this is an agent and not a script

- **Autonomy:** runs unattended on a schedule inside its own daily window, decides
  when to stop (per-ID cap, daily pass cap, pool exhaustion).
- **Self-correction:** derives difficulty and seed from chain state at runtime, so
  a retarget or a re-pinned seed does not require a code change.
- **Safety rails:** refuses to broadcast when simulation fails, never re-submits a
  spent nonce, and holds a PID lock so two instances cannot collide.

## The three silent failure modes (documented because they cost real rounds)

| Failure | Symptom | Root cause |
|---|---|---|
| Stale seed | Every proof reverts `InvalidProof`, looks like "bad luck" | `sessionSeedBase` is re-pinned **daily**; a cached day-0 seed never matches day-1 |
| Re-mined nonces | Contract rejects with a spent-nonce error, looks like "found nothing" | The shard range never advanced — no `offset`, so each pass re-scanned the same window |
| Swallowed timeout | A broken run is indistinguishable from an unlucky one | `capture_output=True` hid `TimeoutExpired`; the caller returned "no proofs" |

Fixes are in `run_window.py` with comments at each site.

## Verified result

Day 1 against the live contract: **20/20 proofs accepted**, 3 transactions, all
status 1 (`cpuCountOfId` = 20/20). Nonces were deduped across passes and every
digest matched `proofDigest()` before submission.

## Architecture

```
run_window.py          orchestrator: chain reads -> swarm spawn -> validate -> submit
  |- amin_swarm_modal.py   Modal fan-out (N shards x M nonces), prints RESULT json
  |- amin_miner.c          keccak nonce search (compiled in-container)
  |- amin_mine.py          single-shard local fallback
```

## Running it

```bash
pip install modal web3
modal token new

# dry run: show window state, caps, and sizing without spending gas
python3 run_window.py --dry

# live pass (within the event window)
python3 run_window.py
```

Environment: `AMIN_SEED` / `AMIN_DAY` are injected by the orchestrator from live
chain reads; do not set them by hand.

## Notes on zkVerify

The proof this agent produces is a **hash-preimage proof verified on-chain by the
target contract** — the contract recomputes the digest and rejects mismatches, so
the result is objectively verifiable rather than self-reported. zkVerify itself is
optional for this Wildcard entry (per the event rules); the design principle is the
same one the arena rewards: *prove it, don't claim it*.

## License

MIT
