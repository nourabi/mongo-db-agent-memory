"""
Manual verification script for db.py, run directly against a live MongoDB
replica set (see README.md for setup). Not a pytest suite — prints PASS/FAIL
per check so failures are easy to spot by eye.

Run with: python test_manual.py
"""

import threading

from db import ConflictError, MemoryStore, TaskAlreadyClaimedError

store = MemoryStore()

# Clean slate so repeated runs are deterministic.
store.memories.delete_many({"key": {"$regex": "^test_"}})
store.task_claims.delete_many({"taskId": {"$regex": "^test_"}})

failures = []


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


# ---------------- write_memory / read_memory ----------------

result = store.write_memory(
    key="test_auth_design_decision",
    value={"approach": "JWT with refresh tokens"},
    written_by="coordinator-1",
    tags=["architecture", "security"],
)
check("write_memory returns version 1 on first write", result["version"] == 1)
check("write_memory sets createdAt on first write", "createdAt" in result)

read_back = store.read_memory("test_auth_design_decision")
check("read_memory returns the written value", read_back["value"]["approach"] == "JWT with refresh tokens")

# Update the same key (no expected_version) -> version should bump
result2 = store.write_memory(
    key="test_auth_design_decision",
    value={"approach": "JWT with refresh tokens", "ttl": "15m"},
    written_by="coordinator-2",
)
check("write_memory bumps version on update", result2["version"] == 2)

# ---------------- optimistic-concurrency conflict ----------------

conflict_raised = False
try:
    store.write_memory(
        key="test_auth_design_decision",
        value={"approach": "should not apply"},
        written_by="coordinator-3",
        expected_version=1,  # stale on purpose -- current version is 2
    )
except ConflictError as e:
    conflict_raised = True
    check("ConflictError reports correct actual_version", e.actual_version == 2)

check("write_memory raises ConflictError on stale expected_version", conflict_raised)

post_conflict = store.read_memory("test_auth_design_decision")
check(
    "rejected write did not apply (value unchanged, version still 2)",
    post_conflict["version"] == 2 and post_conflict["value"].get("approach") == "JWT with refresh tokens",
)

# ---------------- concurrent write_memory (does ConflictError survive with_transaction's retry?) ----------------

store.write_memory(key="test_concurrent_key", value={"n": 0}, written_by="seed")
seed_version = store.read_memory("test_concurrent_key")["version"]

concurrent_results = []


def racer(agent_id):
    try:
        r = store.write_memory(
            key="test_concurrent_key",
            value={"n": agent_id},
            written_by=f"agent-{agent_id}",
            expected_version=seed_version,
        )
        concurrent_results.append(("ok", agent_id, r["version"]))
    except ConflictError as e:
        concurrent_results.append(("conflict", agent_id, e.actual_version))


threads = [threading.Thread(target=racer, args=(i,)) for i in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()

ok_count = sum(1 for r in concurrent_results if r[0] == "ok")
conflict_count = sum(1 for r in concurrent_results if r[0] == "conflict")
check(
    f"concurrent same-expected_version writes: exactly one winner ({ok_count} ok, {conflict_count} conflict)",
    ok_count == 1 and conflict_count == 4,
)

# ---------------- claim_task (sequential, from README) ----------------

claim1 = store.claim_task("test_optimize_db", "worker-1", ttl_seconds=60)
check("first claim succeeds", claim1["claimedBy"] == "worker-1")

already_claimed = False
try:
    store.claim_task("test_optimize_db", "worker-2", ttl_seconds=60)
except TaskAlreadyClaimedError as e:
    already_claimed = True
    check("TaskAlreadyClaimedError reports correct holder", e.claimed_by == "worker-1")

check("second sequential claim is correctly blocked", already_claimed)

store.release_task("test_optimize_db", "worker-1", status="done")
reclaim = store.claim_task("test_optimize_db", "worker-2", ttl_seconds=60)
check("claim succeeds after release", reclaim["claimedBy"] == "worker-2")
store.release_task("test_optimize_db", "worker-2", status="done")

# ---------------- claim_task (real concurrent race) ----------------

claim_results = []
claim_lock = threading.Lock()


def claimer(agent_id):
    try:
        r = store.claim_task("test_race_task", f"worker-{agent_id}", ttl_seconds=60)
        with claim_lock:
            claim_results.append(("claimed", agent_id, r["claimedBy"]))
    except TaskAlreadyClaimedError as e:
        with claim_lock:
            claim_results.append(("blocked", agent_id, e.claimed_by))


threads = [threading.Thread(target=claimer, args=(i,)) for i in range(10)]
for t in threads:
    t.start()
for t in threads:
    t.join()

claimed_count = sum(1 for r in claim_results if r[0] == "claimed")
blocked_count = sum(1 for r in claim_results if r[0] == "blocked")
winner = next(r[2] for r in claim_results if r[0] == "claimed") if claimed_count else None
all_blocked_agree = all(r[2] == winner for r in claim_results if r[0] == "blocked")
check(
    f"10-way concurrent claim_task race: exactly one winner ({claimed_count} claimed, {blocked_count} blocked)",
    claimed_count == 1 and blocked_count == 9,
)
check("all blocked callers see the same claimedBy as the winner", all_blocked_agree)

store.release_task("test_race_task", winner, status="done")

# ---------------- heartbeat / list_active_agents ----------------

store.heartbeat("test-agent-1", role="worker", current_task="test_optimize_db")
active = store.list_active_agents()
check("heartbeat agent shows up in list_active_agents", any(a["_id"] == "test-agent-1" for a in active))

# ---------------- cleanup ----------------

store.memories.delete_many({"key": {"$regex": "^test_"}})
store.task_claims.delete_many({"taskId": {"$regex": "^test_"}})
store.agent_registry.delete_many({"_id": "test-agent-1"})

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)
else:
    print("All checks passed.")
