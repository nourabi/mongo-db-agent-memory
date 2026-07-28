# agent-memory-mongodb

A Model Context Protocol (MCP) server providing **shared memory and task
coordination for multiple AI agents**, backed by MongoDB.

Unlike file-based or SQLite-based multi-agent memory servers, this uses
MongoDB's native features for correctness at scale:

- **Multi-document transactions** for conflict-free memory writes
- **Atomic `findOneAndUpdate`** for race-free task claiming (no separate lock manager)
- **TTL indexes** so stale task claims and offline agent records clean themselves up automatically
- Designed to extend to **change streams** (real-time push) and **vector search** (semantic memory recall) later

## Requirements

- Python 3.10+
- Docker (for local MongoDB) — or a MongoDB Atlas free-tier cluster
- MongoDB **must run as a replica set** — transactions and change streams don't work on a standalone instance. The included `docker-compose.yml` sets this up for you locally. Atlas clusters are already replica sets, so no extra config needed there.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt --break-system-packages

# 2. Start local MongoDB (single-node replica set)
docker compose up -d

# Wait ~10s for the replica set to initiate, then verify:
docker exec -it agent-memory-mongo mongosh --eval "rs.status()"

# 3. Copy env file and adjust if needed
cp .env.example .env
```

If using MongoDB Atlas instead of Docker, just set `MONGODB_URI` in `.env`
to your Atlas connection string — no replica set setup needed since Atlas
already runs as one.

## Running the server directly (for manual testing)

```bash
python server.py
```

This starts the MCP server on stdio. It won't print anything on success —
that's normal, it's waiting for an MCP client to connect over stdin/stdout.

## Testing with a Python script (fastest way to verify logic works)

Before wiring this into Claude Desktop, it's easiest to test the `db.py`
layer directly:

```python
from db import MemoryStore

store = MemoryStore()

# Write a memory
result = store.write_memory(
    key="auth_design_decision",
    value={"approach": "JWT with refresh tokens"},
    written_by="coordinator-1",
    tags=["architecture", "security"],
)
print(result)

# Read it back
print(store.read_memory("auth_design_decision"))

# Simulate a task claim race: two agents, same task
claim1 = store.claim_task("optimize-db", "worker-1", ttl_seconds=60)
print("Worker 1 claimed:", claim1)

try:
    claim2 = store.claim_task("optimize-db", "worker-2", ttl_seconds=60)
    print("Worker 2 claimed:", claim2)
except Exception as e:
    print("Worker 2 correctly blocked:", e)
```

Run with `python test_manual.py` (save the snippet above) to confirm
writes, reads, and claim conflicts behave as expected before adding the
MCP layer into the mix.

## Connecting to Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "python",
      "args": ["/absolute/path/to/agent-memory-mcp/server.py"],
      "env": {
        "MONGODB_URI": "mongodb://localhost:27017/?replicaSet=rs0"
      }
    }
  }
}
```

Restart Claude Desktop after saving. You should then be able to ask
Claude to call `write_memory`, `read_memory`, `claim_task`, etc.

## Tools exposed

| Tool | Purpose |
|---|---|
| `write_memory` | Write/update a memory record; supports optimistic-concurrency conflict detection via `expected_version` |
| `read_memory` | Read a single memory record by key |
| `search_memory` | Search records by tags and/or key substring |
| `claim_task` | Atomically claim a task; fails cleanly if another agent already holds it |
| `release_task` | Release a claim early instead of waiting for TTL expiry |
| `heartbeat` | Record that an agent is alive |
| `list_active_agents` | List agents that have sent a heartbeat in the last 120s |

## Known limitations / next steps

- **No real-time push yet.** `search_memory`/`read_memory` are poll-based.
  Change-stream-based push doesn't map cleanly onto MCP's stdio
  request/response model — a streamable-HTTP transport would be needed
  for agents to truly subscribe to live updates instead of polling.
- **No semantic search yet.** `search_memory` does tag and substring
  matching only. Adding embeddings + MongoDB Atlas Vector Search would
  let agents query memory by meaning rather than exact tags/keywords.
- **Single MongoDB instance assumed.** For genuinely distributed agents
  across machines, deploy this server with the HTTP transport rather
  than stdio, pointed at a shared MongoDB Atlas cluster.
