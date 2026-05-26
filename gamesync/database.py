import os
import aiosqlite
import time
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

DB_PATH = Path.home() / ".gamesync" / "state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    game_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT NOT NULL,
    sync_version INTEGER NOT NULL DEFAULT 1,
    last_synced_at REAL,
    status TEXT NOT NULL DEFAULT 'synced',
    PRIMARY KEY (game_id, relative_path)
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    local_size INTEGER NOT NULL,
    local_mtime REAL NOT NULL,
    local_sha256 TEXT NOT NULL,
    remote_size INTEGER NOT NULL,
    remote_mtime REAL NOT NULL,
    remote_sha256 TEXT NOT NULL,
    remote_peer_id TEXT NOT NULL,
    remote_peer_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    detected_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    game_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    action TEXT NOT NULL,
    peer_name TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS peers (
    peer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    is_manual INTEGER NOT NULL DEFAULT 0,
    last_seen REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'online'
);
"""

class SyncDatabase:
    def __init__(self):
        self.db_path = DB_PATH
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.db_path)
        # Enable WAL mode for concurrency
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        # Build schema
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    # --- File State Methods ---
    async def get_file_state(self, game_id: str, relative_path: str) -> Optional[Dict[str, Any]]:
        async with self.conn.execute(
            "SELECT size, mtime, sha256, sync_version, last_synced_at, status FROM files WHERE game_id = ? AND relative_path = ?",
            (game_id, relative_path)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "game_id": game_id,
                    "relative_path": relative_path,
                    "size": row[0],
                    "mtime": row[1],
                    "sha256": row[2],
                    "sync_version": row[3],
                    "last_synced_at": row[4],
                    "status": row[5]
                }
        return None

    async def update_file_state(self, game_id: str, relative_path: str, size: int, mtime: float, sha256: str, status: str = "synced", bump_version: bool = True):
        # We use an upsert. If we bump version, we add 1 to the existing version (or start at 1).
        if bump_version:
            await self.conn.execute(
                """INSERT INTO files (game_id, relative_path, size, mtime, sha256, sync_version, last_synced_at, status)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(game_id, relative_path) DO UPDATE SET
                       size = excluded.size,
                       mtime = excluded.mtime,
                       sha256 = excluded.sha256,
                       sync_version = files.sync_version + 1,
                       last_synced_at = excluded.last_synced_at,
                       status = excluded.status""",
                (game_id, relative_path, size, mtime, sha256, time.time(), status)
            )
        else:
            await self.conn.execute(
                """INSERT INTO files (game_id, relative_path, size, mtime, sha256, sync_version, last_synced_at, status)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(game_id, relative_path) DO UPDATE SET
                       size = excluded.size,
                       mtime = excluded.mtime,
                       sha256 = excluded.sha256,
                       last_synced_at = excluded.last_synced_at,
                       status = excluded.status""",
                (game_id, relative_path, size, mtime, sha256, time.time(), status)
            )
        await self.conn.commit()

    async def delete_file_state(self, game_id: str, relative_path: str):
        await self.conn.execute(
            "DELETE FROM files WHERE game_id = ? AND relative_path = ?",
            (game_id, relative_path)
        )
        await self.conn.commit()

    # --- Conflict Methods ---
    async def add_conflict(self, game_id: str, relative_path: str, local_size: int, local_mtime: float, local_sha256: str,
                           remote_size: int, remote_mtime: float, remote_sha256: str, remote_peer_id: str, remote_peer_name: str) -> int:
        cursor = await self.conn.execute(
            """INSERT INTO conflicts (game_id, relative_path, local_size, local_mtime, local_sha256,
                                     remote_size, remote_mtime, remote_sha256, remote_peer_id, remote_peer_name, status, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (game_id, relative_path, local_size, local_mtime, local_sha256, remote_size, remote_mtime, remote_sha256, remote_peer_id, remote_peer_name, time.time())
        )
        await self.conn.commit()
        # Mark file state as conflict
        await self.conn.execute(
            "UPDATE files SET status = 'conflict' WHERE game_id = ? AND relative_path = ?",
            (game_id, relative_path)
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_pending_conflicts(self) -> List[Dict[str, Any]]:
        async with self.conn.execute(
            "SELECT id, game_id, relative_path, local_size, local_mtime, local_sha256, remote_size, remote_mtime, remote_sha256, remote_peer_id, remote_peer_name, detected_at FROM conflicts WHERE status = 'pending'"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "id": r[0],
                    "game_id": r[1],
                    "relative_path": r[2],
                    "local_size": r[3],
                    "local_mtime": r[4],
                    "local_sha256": r[5],
                    "remote_size": r[6],
                    "remote_mtime": r[7],
                    "remote_sha256": r[8],
                    "remote_peer_id": r[9],
                    "remote_peer_name": r[10],
                    "detected_at": r[11]
                }
                for r in rows
            ]

    async def resolve_conflict(self, conflict_id: int):
        await self.conn.execute(
            "UPDATE conflicts SET status = 'resolved' WHERE id = ?",
            (conflict_id,)
        )
        await self.conn.commit()

    # --- Peer Methods ---
    async def update_peer(self, peer_id: str, name: str, host: str, port: int, is_manual: bool = False, status: str = "online"):
        await self.conn.execute(
            """INSERT INTO peers (peer_id, name, host, port, is_manual, last_seen, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(peer_id) DO UPDATE SET
                   name = excluded.name,
                   host = excluded.host,
                   port = excluded.port,
                   last_seen = excluded.last_seen,
                   status = excluded.status""",
            (peer_id, name, host, port, 1 if is_manual else 0, time.time(), status)
        )
        await self.conn.commit()

    async def get_peers(self) -> List[Dict[str, Any]]:
        async with self.conn.execute(
            "SELECT peer_id, name, host, port, is_manual, last_seen, status FROM peers"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "peer_id": r[0],
                    "name": r[1],
                    "host": r[2],
                    "port": r[3],
                    "is_manual": bool(r[4]),
                    "last_seen": r[5],
                    "status": r[6]
                }
                for r in rows
            ]

    # --- Sync Log Methods ---
    async def log_event(self, game_id: str, relative_path: str, action: str, peer_name: Optional[str] = None, details: Optional[str] = None):
        await self.conn.execute(
            "INSERT INTO sync_log (timestamp, game_id, relative_path, action, peer_name, details) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), game_id, relative_path, action, peer_name, details)
        )
        await self.conn.commit()

    async def get_logs(self, limit: int = 50) -> List[Dict[str, Any]]:
        async with self.conn.execute(
            "SELECT timestamp, game_id, relative_path, action, peer_name, details FROM sync_log ORDER BY id DESC LIMIT ?",
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "timestamp": r[0],
                    "game_id": r[1],
                    "relative_path": r[2],
                    "action": r[3],
                    "peer_name": r[4],
                    "details": r[5]
                }
                for r in rows
            ]
