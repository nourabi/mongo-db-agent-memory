# Handoff: agent-memory-mongodb MCP server

## What this is
An MCP server providing shared memory + task coordination for multiple AI
agents, backed by MongoDB. Differentiator vs. existing multi-agent memory
MCP servers (shared-memory-mcp, Agent-MCP, mcp-agent-memory): those use
file-based storage or SQLite; this uses MongoDB natively for transactional
conflict resolution, atomic task claiming, and TTL-based auto-expiry —
with change streams (real-time push) and vector search (semantic recall)
planned as the next differentiators.

## Current state
Scaffolded and syntax-verified, but **not yet run against a live MongoDB
instance** — the sandbox that built this had no Docker and no MongoDB
network access, so `db.py`/`server.py` have only been confirmed to import
and fail correctly with a connection-refused error (proving the code path
is reached, not that the logic is correct end-to-end).

**First priority in Claude Code: get it running against real MongoDB and
verify the manual test script in README.md actually behaves as designed**
— especially the two-agent task-claim race and the optimistic-concurrency
conflict path in `write_memory`. Neither has been exercised against a real
database yet.

## Files
- `docker-compose.yml` — local MongoDB as a **single-node replica set**.
  This is required, not optional — transactions and change streams fail
  silently/loudly on a standalone MongoDB instance.
- `db.py` — data layer. Three collections: `memories` (versioned,
  optional TTL), `task_claims` (atomic claim/release, TTL auto-release),
  `agent_registry` (heartbeat, 120s TTL).
- `server.py` — FastMCP wrapper exposing 7 tools: `write_memory`,
  `read_memory`, `search_memory`, `claim_task`, `release_task`,
  `heartbeat`, `list_active_agents`.
- `README.md` — setup, run, and manual-test instructions, plus a
  Claude Desktop config snippet.

## Known gaps / open design decisions (see README "Known limitations")
1. **Transport choice unresolved.** Currently stdio. Change streams
   (real-time push instead of polling) don't map cleanly onto stdio's
   request/response shape — would need streamable-HTTP transport for
   agents to actually subscribe rather than poll. Decide this before
   building change-stream support, since it affects the server's
   run/deploy model.
2. **No semantic search yet.** `search_memory` is tag/substring only.
   Adding embeddings + MongoDB Atlas Vector Search is the planned
   upgrade for real "search by meaning" recall.
3. **`write_memory`'s transaction wrapper** (`session.with_transaction`)
   hasn't been tested under actual concurrent writes — worth writing a
   quick concurrent-write test (e.g., two threads/processes writing the
   same key) to confirm the `ConflictError` path triggers correctly
   rather than one write silently winning.

**Note:** an earlier version of this doc had a "Suggested next session
structure" here (docker up → test_manual.py → fix bugs → wire into
Claude Desktop → decide transport). That's all done now — see "UPDATE:
Phase 1 concurrency testing complete" and "UPDATE: Open-source prep"
below for what actually happened, and "ACTION ITEMS" for what's left.
The transport decision in particular is final (HTTP-only), not still
open — ignore any other reference to "deciding" it.

## Not yet published anywhere
No GitHub repo created yet. Once verified working, the plan discussed
was: push to GitHub with the existing README, then list on the official
MCP registry (via `mcp-publisher`) plus mcp.so / smithery.ai / glama.ai
and an `awesome-mcp-servers` PR.

## UPDATE: Phase 1 concurrency testing complete

`test_manual.py` (written in a prior Claude Code session, not in this
repo snapshot — recreate or pull forward if missing) found and fixed two
real bugs in `db.py` under actual concurrent load against a real replica
set — neither surfaced in sandbox syntax checks:

1. `claim_task` compared a timezone-aware `now` against `expiresAt` read
   back from MongoDB (timezone-naive by default via pymongo) → `TypeError`
   on every call once a claim existed. **Fix: pass `tz_aware=True` to
   `MongoClient`** in `db.py`'s `MemoryStore.__init__`. **This fix is NOT
   yet applied in this repo snapshot — apply it before testing further.**
2. When two+ agents raced to claim a brand-new task simultaneously, the
   losers' `DuplicateKeyError` fallback in `claim_task` reported
   `claimed_by="unknown"` and their own losing `expires_at` instead of the
   actual winner. Fix: re-read the winning document before raising
   `TaskAlreadyClaimedError`. **Also not yet applied here.**

Test suite covers: write/read, optimistic-concurrency conflicts, a 5-way
concurrent write race, sequential and 10-way concurrent task-claim races,
heartbeat/presence. 15/15 passing, stable across repeated runs once both
fixes above are applied.

**Action item: port both fixes into `db.py` in this repo before
continuing** — this snapshot predates them.

## UPDATE: Open-source prep

- `LICENSE` (MIT) and `.gitignore` added (covers `.env`, `__pycache__/`,
  `.venv/`, build artifacts — verify `.env` is actually gitignored before
  first commit, since it will contain a real MongoDB URI once Atlas is
  wired up).
- **Transport decision: FINAL — streamable-HTTP only. stdio is dropped,
  not kept as an alternative.** Rationale: the project's entire premise
  is multiple independent agent processes sharing state through a common
  server. stdio is structurally incompatible with that — it's a 1:1
  subprocess model (one client spawns and privately owns the server), so
  a second agent in a second process has no way to reach a stdio server
  another process already spawned. Keeping `server.py` (stdio) around
  wasn't a neutral "extra option" — it was a version that can't actually
  run the project's stated use case. No reason to ship or maintain it.

## ACTION ITEMS (do these, in order)
1. Delete `server.py` (stdio). Rename `server_http.py` → `server.py` so
   the entry point filename stays simple. There is only one server file
   going forward.
2. Port the two `db.py` fixes into whichever copy of `db.py` ends up in
   the final repo (confirm they're already there if this is the same
   `db.py` you fixed in your Claude Code session — just double-check,
   don't assume):
   - `tz_aware=True` passed to `MongoClient` in `MemoryStore.__init__`
   - `claim_task`'s `DuplicateKeyError` fallback re-reads the actual
     winning document before raising `TaskAlreadyClaimedError`, instead
     of reporting `claimed_by="unknown"` and the loser's own expiry.
3. Update `README.md`'s connection section: replace the
   `command`/`args` subprocess-style Claude Desktop config with the
   HTTP URL-based config (point at `http://<host>:8000/mcp`). Also
   update the "Running the server" section — it's now
   `python server.py` starting a long-running network service, not an
   ephemeral stdio process.
4. Confirm `test_manual.py` still passes 15/15 against the final
   `db.py`.
5. Real multi-agent proof test: run the (renamed) `server.py`, connect
   two independent MCP clients to the same URL, confirm `claim_task` on
   the same `task_id` from both only lets one win. This is the test that
   actually validates the HTTP-transport decision, not just the `db.py`
   logic in isolation.
6. `git init`, first commit (LICENSE + .gitignore staged, `server.py`
   only — no leftover stdio file), push to GitHub.
7. Restructure as an installable package (`src/agent_memory_mongodb/`
   layout, `pyproject.toml`) for PyPI — not started yet.
8. Publish: PyPI (`uv build` + `uv publish`), then MCP registry
   (`mcp-publisher`) + mcp.so/smithery.ai/glama.ai +
   `awesome-mcp-servers` PR.
