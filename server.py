"""
Multi-agent shared memory MCP server, backed by MongoDB.

Run with:
    python server.py

Register in Claude Desktop config as a stdio server (see README.md).
"""

from typing import Any, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from db import MemoryStore, ConflictError, TaskAlreadyClaimedError

load_dotenv()

mcp = FastMCP("agent-memory-mongodb")
store = MemoryStore()


@mcp.tool()
def write_memory(
    key: str,
    value: dict,
    written_by: str,
    tags: Optional[list[str]] = None,
    ttl_seconds: Optional[int] = None,
    expected_version: Optional[int] = None,
) -> dict:
    """
    Write or update a shared memory record.

    If expected_version is provided, the write only succeeds if no other
    agent has changed the record since. On conflict, returns the current
    stored value and version so the caller can decide how to merge,
    instead of silently overwriting another agent's write.
    """
    try:
        result = store.write_memory(
            key=key,
            value=value,
            written_by=written_by,
            tags=tags,
            ttl_seconds=ttl_seconds,
            expected_version=expected_version,
        )
        return {"status": "ok", "memory": {k: v for k, v in result.items() if k != "_id"}}
    except ConflictError as e:
        return {
            "status": "conflict",
            "key": e.key,
            "expected_version": e.expected_version,
            "actual_version": e.actual_version,
            "current_value": e.current_value,
        }


@mcp.tool()
def read_memory(key: str) -> dict:
    """Read a single memory record by key. Returns null fields if not found."""
    result = store.read_memory(key)
    return result or {"key": key, "found": False}


@mcp.tool()
def search_memory(tags: Optional[list[str]] = None, text: Optional[str] = None) -> list[dict]:
    """Search memory records by tag list and/or a substring match on key."""
    return store.search_memory(tags=tags, text=text)


@mcp.tool()
def claim_task(task_id: str, agent_id: str, ttl_seconds: int = 300) -> dict:
    """
    Atomically claim a task so no other agent works on it simultaneously.
    The claim auto-expires after ttl_seconds unless released or renewed,
    so a crashed agent doesn't block the task forever.
    """
    try:
        result = store.claim_task(task_id, agent_id, ttl_seconds)
        return {"status": "claimed", "claim": result}
    except TaskAlreadyClaimedError as e:
        return {
            "status": "already_claimed",
            "task_id": e.task_id,
            "claimed_by": e.claimed_by,
            "expires_at": str(e.expires_at),
        }


@mcp.tool()
def release_task(task_id: str, agent_id: str, status: str = "done") -> dict:
    """Release a claimed task early (status: 'done' or 'failed') instead of waiting for TTL expiry."""
    try:
        result = store.release_task(task_id, agent_id, status)
        return {"status": "released", "claim": result}
    except ValueError as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def heartbeat(agent_id: str, role: str = "worker", current_task: Optional[str] = None) -> dict:
    """Record that an agent is alive. Entries auto-expire after 120s of no heartbeat."""
    result = store.heartbeat(agent_id, role, current_task)
    return {k: v for k, v in result.items() if k != "_id"}


@mcp.tool()
def list_active_agents() -> list[dict]:
    """List all agents that have sent a heartbeat within the last 120 seconds."""
    return [{k: v for k, v in a.items() if k != "_id"} for a in store.list_active_agents()]


if __name__ == "__main__":
    mcp.run(transport="stdio")
