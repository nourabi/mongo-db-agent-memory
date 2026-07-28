"""
MongoDB data-access layer for the multi-agent shared memory MCP server.

Collections:
  - memories:      shared knowledge, versioned, optionally auto-expiring
  - task_claims:   who is working on what, with TTL-based auto-release
  - agent_registry: heartbeat/presence tracking for active agents
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/?replicaSet=rs0")
DB_NAME = os.environ.get("MONGODB_DB_NAME", "agent_memory")


class MemoryStore:
    def __init__(self, uri: str = MONGODB_URI, db_name: str = DB_NAME):
        # tz_aware=True so datetimes read back from MongoDB are
        # timezone-aware UTC, matching datetime.now(timezone.utc) used
        # throughout this module. Without it, pymongo deserializes BSON
        # dates as naive datetimes and every comparison against "now"
        # raises TypeError.
        self.client = MongoClient(uri, tz_aware=True)
        self.db = self.client[db_name]
        self.memories = self.db["memories"]
        self.task_claims = self.db["task_claims"]
        self.agent_registry = self.db["agent_registry"]
        self._ensure_indexes()

    def _ensure_indexes(self):
        # Unique key per memory record
        self.memories.create_index("key", unique=True)
        self.memories.create_index("tags")
        # TTL index: MongoDB auto-deletes documents once expiresAt passes.
        # A null expiresAt means "never expires" (TTL index ignores null values).
        self.memories.create_index("expiresAt", expireAfterSeconds=0)

        self.task_claims.create_index("taskId", unique=True)
        self.task_claims.create_index("expiresAt", expireAfterSeconds=0)

        self.agent_registry.create_index("lastHeartbeat", expireAfterSeconds=120)

    # ---------------- Memory operations ----------------

    def write_memory(
        self,
        key: str,
        value: Any,
        written_by: str,
        tags: Optional[list[str]] = None,
        ttl_seconds: Optional[int] = None,
        expected_version: Optional[int] = None,
    ) -> dict:
        """
        Write or update a memory record inside a transaction.

        If expected_version is given, the write only succeeds if the
        current stored version matches (optimistic concurrency control).
        This is how two agents racing to update the same key get a
        clean conflict signal instead of silently clobbering each other.
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds) if ttl_seconds else None

        with self.client.start_session() as session:
            def _txn(session):
                existing = self.memories.find_one({"key": key}, session=session)

                if existing and expected_version is not None:
                    if existing["version"] != expected_version:
                        raise ConflictError(
                            key=key,
                            expected_version=expected_version,
                            actual_version=existing["version"],
                            current_value=existing["value"],
                        )

                new_version = (existing["version"] + 1) if existing else 1

                doc = {
                    "key": key,
                    "value": value,
                    "writtenBy": written_by,
                    "version": new_version,
                    "tags": tags or [],
                    "updatedAt": now,
                    "expiresAt": expires_at,
                }
                if not existing:
                    doc["createdAt"] = now

                result = self.memories.find_one_and_update(
                    {"key": key},
                    {"$set": doc, "$setOnInsert": {} if existing else {}},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                    session=session,
                )
                return result

            return session.with_transaction(_txn)

    def read_memory(self, key: str) -> Optional[dict]:
        return self.memories.find_one({"key": key}, {"_id": 0})

    def search_memory(self, tags: Optional[list[str]] = None, text: Optional[str] = None) -> list[dict]:
        query: dict = {}
        if tags:
            query["tags"] = {"$in": tags}
        if text:
            query["key"] = {"$regex": text, "$options": "i"}
        return list(self.memories.find(query, {"_id": 0}))

    # ---------------- Task claim operations ----------------

    def claim_task(self, task_id: str, agent_id: str, ttl_seconds: int = 300) -> dict:
        """
        Atomically claim a task. Fails if another agent already holds
        an unexpired claim on the same task_id. Relies on MongoDB's
        single-document atomicity, so no separate lock manager needed.
        """
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)

        existing = self.task_claims.find_one({"taskId": task_id})
        if existing and existing["expiresAt"] > now and existing["status"] == "in_progress":
            if existing["claimedBy"] != agent_id:
                raise TaskAlreadyClaimedError(task_id, existing["claimedBy"], existing["expiresAt"])

        try:
            result = self.task_claims.find_one_and_update(
                {
                    "taskId": task_id,
                    "$or": [
                        {"expiresAt": {"$lte": now}},
                        {"status": {"$ne": "in_progress"}},
                        {"claimedBy": agent_id},
                    ],
                },
                {
                    "$set": {
                        "claimedBy": agent_id,
                        "claimedAt": now,
                        "expiresAt": expires_at,
                        "status": "in_progress",
                    }
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return {k: v for k, v in result.items() if k != "_id"}
        except DuplicateKeyError:
            # Another agent's upsert won the race between our read of
            # `existing` and our own upsert attempt. Re-read so the
            # error reports who actually holds the claim, rather than
            # our own (losing) attempted values.
            winner = self.task_claims.find_one({"taskId": task_id})
            raise TaskAlreadyClaimedError(task_id, winner["claimedBy"], winner["expiresAt"])

    def release_task(self, task_id: str, agent_id: str, status: str = "done") -> dict:
        result = self.task_claims.find_one_and_update(
            {"taskId": task_id, "claimedBy": agent_id},
            {"$set": {"status": status}},
            return_document=ReturnDocument.AFTER,
        )
        if not result:
            raise ValueError(f"No active claim on '{task_id}' held by '{agent_id}'")
        return {k: v for k, v in result.items() if k != "_id"}

    # ---------------- Agent presence ----------------

    def heartbeat(self, agent_id: str, role: str = "worker", current_task: Optional[str] = None) -> dict:
        now = datetime.now(timezone.utc)
        result = self.agent_registry.find_one_and_update(
            {"_id": agent_id},
            {"$set": {"role": role, "lastHeartbeat": now, "currentTask": current_task}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return result

    def list_active_agents(self) -> list[dict]:
        return list(self.agent_registry.find({}))


class ConflictError(PyMongoError):
    def __init__(self, key, expected_version, actual_version, current_value):
        self.key = key
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.current_value = current_value
        super().__init__(
            f"Version conflict on '{key}': expected v{expected_version}, "
            f"found v{actual_version}"
        )


class TaskAlreadyClaimedError(Exception):
    def __init__(self, task_id, claimed_by, expires_at):
        self.task_id = task_id
        self.claimed_by = claimed_by
        self.expires_at = expires_at
        super().__init__(
            f"Task '{task_id}' is already claimed by '{claimed_by}' until {expires_at}"
        )
