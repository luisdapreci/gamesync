import os
import shutil
import hashlib
import httpx
import logging
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional
import time
from .config import ConfigManager, CONFIG_DIR
from .database import SyncDatabase
from .models import GameProfile

logger = logging.getLogger("gamesync.sync_engine")

CONFLICTS_DIR = CONFIG_DIR / "conflicts"
BACKUPS_DIR = CONFIG_DIR / "backups"

def compute_sha256(file_path: Path) -> str:
    sha256 = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception as e:
        logger.error(f"Error hashing file {file_path}: {e}")
        return ""

class SyncEngine:
    def __init__(self, config_manager: ConfigManager, db: SyncDatabase, node_id: str):
        self.config_manager = config_manager
        self.db = db
        self.node_id = node_id
        self.event_callback = None # Set by FastAPI / WebSocket manager
        self.loop: Optional[asyncio.AbstractEventLoop] = None  # Set after startup

        CONFLICTS_DIR.mkdir(parents=True, exist_ok=True)
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    def set_event_callback(self, callback):
        self.event_callback = callback

    def trigger_ui_update(self, event_type: str, data: dict):
        if self.event_callback and self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.event_callback({"type": event_type, "data": data}),
                self.loop
            )

    # --- Local File Watching Trigger ---
    def handle_local_change(self, game_id: str, relative_path: str):
        """Called by watchdog watcher when a file change is debounced."""
        # Because watcher runs in its own OS threads, we must schedule the sync logic in the main asyncio loop
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._sync_local_file_change(game_id, relative_path),
                self.loop
            )

    async def _sync_local_file_change(self, game_id: str, relative_path: str):
        game = next((g for g in self.config_manager.settings.games if g.id == game_id), None)
        if not game:
            return

        absolute_path = Path(game.path) / relative_path
        if not absolute_path.exists():
            return # Deletion is not fully supported in this draft or handled differently

        stat = absolute_path.stat()
        file_size = stat.st_size
        mtime = stat.st_mtime
        sha256 = compute_sha256(absolute_path)

        # Check size warning
        size_mb = file_size / (1024 * 1024)
        if size_mb > self.config_manager.settings.warn_file_size_mb:
            logger.warning(f"File {relative_path} in game {game.name} is large ({size_mb:.1f}MB). Warning triggered.")
            self.trigger_ui_update("warning", {
                "message": f"Large save file detected: {game.name}/{relative_path} ({size_mb:.1f}MB)",
                "game_id": game_id,
                "file": relative_path
            })

        # Load current DB state
        db_state = await self.db.get_file_state(game_id, relative_path)
        
        # If DB matches this state, we don't need to do anything
        if db_state and db_state["sha256"] == sha256:
            return

        # Update local db, bump sync_version
        await self.db.update_file_state(game_id, relative_path, file_size, mtime, sha256, status="synced", bump_version=True)
        new_db_state = await self.db.get_file_state(game_id, relative_path)
        
        await self.db.log_event(game_id, relative_path, "local_change", details=f"Size: {file_size}, SHA: {sha256[:8]}")
        self.trigger_ui_update("sync_log", {
            "game_id": game_id,
            "relative_path": relative_path,
            "action": "local_change",
            "details": f"Detected local file change"
        })
    async def push_file_to_peers(self, game: GameProfile, relative_path: str, local_path: Path, sync_version: int):
        peers = await self.db.get_peers()
        online_peers = [p for p in peers if p["status"] == "online"]
        
        if not online_peers:
            logger.info("No online peers to push change to")
            return

        stat = local_path.stat()
        file_size = stat.st_size
        mtime = stat.st_mtime
        sha256 = compute_sha256(local_path)

        for peer in online_peers:
            url = f"http://{peer['host']}:{peer['port']}/api/sync/push"
            try:
                # Prepare headers
                headers = {
                    "X-Game-Id": game.id,
                    "X-Relative-Path": relative_path,
                    "X-File-Size": str(file_size),
                    "X-Mtime": str(mtime),
                    "X-Sha256": sha256,
                    "X-Sync-Version": str(sync_version),
                    "X-Sender-Id": self.node_id,
                    "X-Sender-Name": self.config_manager.settings.node_name,
                    "X-PIN": self.config_manager.settings.pin
                }
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    logger.info(f"Pushing {relative_path} to {peer['name']} ({url})...")
                    with open(local_path, "rb") as f:
                        response = await client.post(url, headers=headers, content=f)
                    
                    if response.status_code == 200:
                        logger.info(f"Successfully pushed to {peer['name']}")
                    else:
                        logger.error(f"Failed to push to {peer['name']}: {response.status_code} - {response.text}")
            except Exception as e:
                logger.error(f"Error communicating with peer {peer['name']}: {e}")

    # --- Remote File Push handler (Incoming REST call) ---
    async def handle_incoming_push(self, game_id: str, relative_path: str, size: int, mtime: float, sha256: str,
                                   sync_version: int, sender_id: str, sender_name: str, file_content: bytes) -> Dict[str, Any]:
        
        game = next((g for g in self.config_manager.settings.games if g.id == game_id), None)
        if not game:
            return {"status": "error", "message": f"Game profile {game_id} not configured on this host"}

        local_dir = Path(game.path)
        local_file_path = local_dir / relative_path
        
        # Ensure parent directories exist
        local_file_path.parent.mkdir(parents=True, exist_ok=True)

        # Get local file db status
        db_state = await self.db.get_file_state(game_id, relative_path)
        
        # Scenario 1: Local file doesn't exist
        if not local_file_path.exists():
            # Validate integrity before writing
            actual_sha = hashlib.sha256(file_content).hexdigest()
            if actual_sha != sha256:
                return {"status": "error", "message": f"SHA256 mismatch: expected {sha256}, got {actual_sha}"}
            if len(file_content) != size:
                return {"status": "error", "message": f"Size mismatch: expected {size}, got {len(file_content)}"}
            await self._apply_remote_file(game_id, relative_path, local_file_path, file_content, size, mtime, sha256, sync_version, sender_name)
            return {"status": "ok", "message": "File created"}

        # Calculate current local file hash
        current_local_sha = compute_sha256(local_file_path)

        # Scenario 2: Local file matches incoming remote file exactly
        if current_local_sha == sha256:
            # Just keep DB updated
            await self.db.update_file_state(game_id, relative_path, size, mtime, sha256, status="synced", bump_version=False)
            return {"status": "ok", "message": "File is already in sync"}

        # Validate integrity of incoming bytes before any write
        actual_sha = hashlib.sha256(file_content).hexdigest()
        if actual_sha != sha256:
            return {"status": "error", "message": f"SHA256 mismatch: expected {sha256}, got {actual_sha}"}
        if len(file_content) != size:
            return {"status": "error", "message": f"Size mismatch: expected {size}, got {len(file_content)}"}

        # Scenario 3: Check if local file was modified since last sync (Three-way check)
        is_local_modified = False
        if db_state:
            # If current local file hash differs from what we had in DB when last synced/saved
            if current_local_sha != db_state["sha256"]:
                is_local_modified = True
        else:
            # File exists physically but not in DB -> treat as local change
            is_local_modified = True

        if not is_local_modified:
            # Safe to overwrite (Fast-Forward)
            await self._create_backup(game_id, relative_path, local_file_path)
            await self._apply_remote_file(game_id, relative_path, local_file_path, file_content, size, mtime, sha256, sync_version, sender_name)
            return {"status": "ok", "message": "Fast-forward sync successful"}

        # Scenario 4: Both modified (Conflict!)
        # Create conflict records, save conflict versions
        conflict_id = await self._stage_conflict(
            game_id, relative_path, local_file_path, current_local_sha,
            file_content, size, mtime, sha256, sender_id, sender_name
        )
        return {"status": "conflict", "conflict_id": conflict_id, "message": "Sync conflict detected"}

    # --- Conflict Management helpers ---
    async def _stage_conflict(self, game_id: str, relative_path: str, local_path: Path, local_sha: str,
                              remote_bytes: bytes, remote_size: int, remote_mtime: float, remote_sha: str,
                              remote_peer_id: str, remote_peer_name: str) -> int:
        
        # Insert conflict record first so we have a conflict_id to embed in filenames
        local_stat = local_path.stat()
        conflict_id = await self.db.add_conflict(
            game_id, relative_path,
            local_size=local_stat.st_size, local_mtime=local_stat.st_mtime, local_sha256=local_sha,
            remote_size=remote_size, remote_mtime=remote_mtime, remote_sha256=remote_sha,
            remote_peer_id=remote_peer_id, remote_peer_name=remote_peer_name
        )

        # Generate safe filenames that include conflict_id so resolution always picks the right files
        safe_rel_name = relative_path.replace("/", "_").replace("\\", "_")
        local_staging_path = CONFLICTS_DIR / f"{conflict_id}_{game_id}_{safe_rel_name}.local"
        remote_staging_path = CONFLICTS_DIR / f"{conflict_id}_{game_id}_{safe_rel_name}.remote"

        # Save local version
        shutil.copy2(local_path, local_staging_path)

        # Save remote version
        with open(remote_staging_path, "wb") as f:
            f.write(remote_bytes)
        
        # Set remote timestamp
        os.utime(remote_staging_path, (remote_mtime, remote_mtime))

        await self.db.log_event(
            game_id, relative_path, "conflict_detected",
            peer_name=remote_peer_name,
            details=f"Conflict ID: {conflict_id}"
        )
        
        self.trigger_ui_update("conflict", {
            "id": conflict_id,
            "game_id": game_id,
            "relative_path": relative_path,
            "local_mtime": local_stat.st_mtime,
            "remote_mtime": remote_mtime,
            "remote_peer_name": remote_peer_name
        })

        return conflict_id

    async def resolve_conflict_selection(self, conflict_id: int, selection: str):
        """Resolves conflict: selection must be 'local' or 'remote'"""
        conflicts = await self.db.get_pending_conflicts()
        conflict = next((c for c in conflicts if c["id"] == conflict_id), None)
        if not conflict:
            raise ValueError(f"Conflict {conflict_id} not found or already resolved")

        game_id = conflict["game_id"]
        relative_path = conflict["relative_path"]
        
        game = next((g for g in self.config_manager.settings.games if g.id == game_id), None)
        if not game:
            raise ValueError(f"Game profile {game_id} is missing")

        # Find staging files by conflict_id prefix (deterministic, no ambiguity)
        safe_rel_name = relative_path.replace("/", "_").replace("\\", "_")
        local_staged = CONFLICTS_DIR / f"{conflict_id}_{game_id}_{safe_rel_name}.local"
        remote_staged = CONFLICTS_DIR / f"{conflict_id}_{game_id}_{safe_rel_name}.remote"

        if not local_staged.exists() or not remote_staged.exists():
            raise FileNotFoundError("Conflict files not found in staging area")

        target_file_path = Path(game.path) / relative_path
        target_file_path.parent.mkdir(parents=True, exist_ok=True)

        if selection == "local":
            # Keep local version: it is already in target_file_path theoretically, but let's restore it
            # from staging just in case.
            shutil.copy2(local_staged, target_file_path)
            size = conflict["local_size"]
            mtime = conflict["local_mtime"]
            sha256 = conflict["local_sha256"]
        elif selection == "remote":
            # Apply remote version
            await self._create_backup(game_id, relative_path, target_file_path)
            shutil.copy2(remote_staged, target_file_path)
            size = conflict["remote_size"]
            mtime = conflict["remote_mtime"]
            sha256 = conflict["remote_sha256"]
        else:
            raise ValueError("Selection must be 'local' or 'remote'")

        # Clear conflict staging files
        try:
            local_staged.unlink()
            remote_staged.unlink()
        except Exception as e:
            logger.error(f"Error removing staged conflict files: {e}")

        # Resolve in DB
        await self.db.resolve_conflict(conflict_id)
        
        # Update file state: bump version on resolution so it pushes to peers
        await self.db.update_file_state(game_id, relative_path, size, mtime, sha256, status="synced", bump_version=True)
        updated_state = await self.db.get_file_state(game_id, relative_path)

        await self.db.log_event(game_id, relative_path, "conflict_resolved", details=f"Resolved keeping: {selection}")
        self.trigger_ui_update("sync_log", {
            "game_id": game_id,
            "relative_path": relative_path,
            "action": "conflict_resolved",
            "details": f"Conflict resolved keeping {selection}"
        })

        # Push the resolved version to peers to force sync
        await self.push_file_to_peers(game, relative_path, target_file_path, updated_state["sync_version"])

    # --- Apply File Helper ---
    async def _apply_remote_file(self, game_id: str, relative_path: str, dest_path: Path, content: bytes,
                                 size: int, mtime: float, sha256: str, sync_version: int, sender_name: str):
        # Write content
        with open(dest_path, "wb") as f:
            f.write(content)
        
        # Set modification time to match source
        os.utime(dest_path, (mtime, mtime))

        # Save to DB (force sync_version to remote version)
        await self.db.conn.execute(
            """INSERT OR REPLACE INTO files (game_id, relative_path, size, mtime, sha256, sync_version, last_synced_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'synced')""",
            (game_id, relative_path, size, mtime, sha256, sync_version, time.time())
        )
        await self.db.conn.commit()

        await self.db.log_event(game_id, relative_path, "download", peer_name=sender_name, details=f"Version: {sync_version}")
        self.trigger_ui_update("sync_log", {
            "game_id": game_id,
            "relative_path": relative_path,
            "action": "download",
            "peer_name": sender_name,
            "details": f"Synced remote file change"
        })

    # --- Backup rotation engine ---
    async def _create_backup(self, game_id: str, relative_path: str, file_path: Path):
        """Creates a rotating backup of file_path before it gets overwritten."""
        if not file_path.exists():
            return

        # Encode path to avoid folder nesting in backup directories
        safe_rel_path = relative_path.replace("/", "_").replace("\\", "_")
        game_backup_dir = BACKUPS_DIR / game_id / safe_rel_path
        game_backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(time.time())
        backup_path = game_backup_dir / f"{timestamp}_{file_path.name}.bak"
        
        try:
            shutil.copy2(file_path, backup_path)
            logger.info(f"Created backup: {backup_path}")
            
            # Rotate backups (keep only backup_count latest versions)
            backup_count = self.config_manager.settings.backup_count
            backups = sorted(game_backup_dir.glob("*.bak"), key=lambda x: x.stat().st_mtime)
            
            if len(backups) > backup_count:
                # Remove oldest backups
                to_remove = backups[:-backup_count]
                for old_bak in to_remove:
                    old_bak.unlink()
                    logger.info(f"Rotated old backup out: {old_bak}")
            
            await self.db.log_event(game_id, relative_path, "backup_created", details=f"Backup file: {backup_path.name}")
        except Exception as e:
            logger.error(f"Failed to create backup for {relative_path}: {e}")

    # --- Manual Sync Engine Methods ---
    async def compare_with_peer(self, game_id: str, peer_id: str) -> Dict[str, Any]:
        game = next((g for g in self.config_manager.settings.games if g.id == game_id), None)
        if not game:
            raise ValueError(f"Game profile {game_id} not configured locally")
            
        peers = await self.db.get_peers()
        peer = next((p for p in peers if p["peer_id"] == peer_id), None)
        if not peer:
            raise ValueError(f"Peer {peer_id} not found")
            
        if peer["status"] != "online":
            raise ValueError(f"Peer {peer['name']} is offline")
            
        # Get remote state
        url = f"http://{peer['host']}:{peer['port']}/api/games/{game_id}/state"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"X-PIN": self.config_manager.settings.pin})
            if resp.status_code != 200:
                raise ValueError(f"Failed to fetch remote state: {resp.text}")
            remote_files = resp.json()
            
        # Get local state
        root_path = Path(game.path).resolve()
        local_files = {}
        if root_path.exists():
            import fnmatch
            from .watcher import IGNORE_EXTENSIONS
            for file_path in root_path.rglob("*"):
                if file_path.is_file():
                    if file_path.suffix.lower() in IGNORE_EXTENSIONS:
                        continue
                    try:
                        relative_path = file_path.relative_to(root_path)
                        rel_str = str(relative_path).replace('\\', '/')
                    except ValueError:
                        continue
                    
                    pattern = game.pattern or "*"
                    if not fnmatch.fnmatch(file_path.name, pattern):
                        continue
                    
                    stat = file_path.stat()
                    local_files[rel_str] = {
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "sha256": compute_sha256(file_path)
                    }
                    
        remote_dict = {f["relative_path"]: f for f in remote_files}
        
        # Compare
        differences = []
        all_paths = set(local_files.keys()).union(set(remote_dict.keys()))
        
        for path in all_paths:
            l = local_files.get(path)
            r = remote_dict.get(path)
            
            if l and r:
                if l["sha256"] != r["sha256"]:
                    differences.append({
                        "relative_path": path,
                        "local": l,
                        "remote": r,
                        "status": "modified"
                    })
            elif l and not r:
                differences.append({
                    "relative_path": path,
                    "local": l,
                    "remote": None,
                    "status": "local_only"
                })
            elif not l and r:
                differences.append({
                    "relative_path": path,
                    "local": None,
                    "remote": r,
                    "status": "remote_only"
                })
                
        return {"differences": differences}

    async def _push_file_to_specific_peer(self, game: GameProfile, relative_path: str, local_path: Path, sync_version: int, peer: dict):
        stat = local_path.stat()
        file_size = stat.st_size
        mtime = stat.st_mtime
        sha256 = compute_sha256(local_path)
        
        url = f"http://{peer['host']}:{peer['port']}/api/sync/push"
        try:
            headers = {
                "X-Game-Id": game.id,
                "X-Relative-Path": relative_path,
                "X-File-Size": str(file_size),
                "X-Mtime": str(mtime),
                "X-Sha256": sha256,
                "X-Sync-Version": str(sync_version),
                "X-Sender-Id": self.node_id,
                "X-Sender-Name": self.config_manager.settings.node_name,
                "X-PIN": self.config_manager.settings.pin
            }
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                with open(local_path, "rb") as f:
                    response = await client.post(url, headers=headers, content=f)
                
                if response.status_code == 200:
                    logger.info(f"Successfully pushed to {peer['name']}")
                else:
                    logger.error(f"Failed to push to {peer['name']}: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Error communicating with peer {peer['name']}: {e}")

    async def execute_manual_sync(self, game_id: str, peer_id: str, selection: str):
        game = next((g for g in self.config_manager.settings.games if g.id == game_id), None)
        if not game:
            raise ValueError(f"Game profile {game_id} not configured locally")
            
        peers = await self.db.get_peers()
        peer = next((p for p in peers if p["peer_id"] == peer_id), None)
        if not peer:
            raise ValueError(f"Peer {peer_id} not found")
            
        compare_result = await self.compare_with_peer(game_id, peer_id)
        differences = compare_result["differences"]
        
        root_path = Path(game.path).resolve()
        
        if selection == "local":
            for diff in differences:
                if diff["local"]:
                    local_path = root_path / diff["relative_path"]
                    db_state = await self.db.get_file_state(game_id, diff["relative_path"])
                    sv = db_state["sync_version"] if db_state else 1
                    await self._push_file_to_specific_peer(game, diff["relative_path"], local_path, sv, peer)
            
            pushed_count = len([d for d in differences if d.get("local")])
            self.trigger_ui_update("sync_log", {
                "game_id": game_id,
                "relative_path": "Multiple Files",
                "action": "local_change",
                "details": f"Manually pushed {pushed_count} files to {peer['name']}"
            })
            
        elif selection == "remote":
            async with httpx.AsyncClient(timeout=30.0) as client:
                for diff in differences:
                    if diff["remote"]:
                        remote_file = diff["remote"]
                        url = f"http://{peer['host']}:{peer['port']}/api/games/{game_id}/file?path={remote_file['relative_path']}"
                        resp = await client.get(url, headers={"X-PIN": self.config_manager.settings.pin})
                        if resp.status_code == 200:
                            content = resp.content
                            local_path = root_path / diff["relative_path"]
                            local_path.parent.mkdir(parents=True, exist_ok=True)
                            await self._create_backup(game_id, diff["relative_path"], local_path)
                            await self._apply_remote_file(
                                game_id, 
                                diff["relative_path"], 
                                local_path, 
                                content, 
                                remote_file["size"], 
                                remote_file["mtime"], 
                                remote_file["sha256"], 
                                remote_file.get("sync_version", 1), 
                                peer["name"]
                            )
                        else:
                            logger.error(f"Failed to download {diff['relative_path']}: {resp.status_code}")
