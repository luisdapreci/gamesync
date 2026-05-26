import os
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Header, HTTPException, Query, UploadFile, File, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import string

from .config import ConfigManager
from .database import SyncDatabase
from .sync_engine import SyncEngine
from .models import GameProfile, AppSettings

logger = logging.getLogger("gamesync.api")

def create_app(config_manager: ConfigManager, db: SyncDatabase, sync_engine: SyncEngine) -> FastAPI:
    app = FastAPI(title="GameSync API")

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Active WebSocket clients
    active_websockets: List[WebSocket] = []

    # Middleware-like PIN verification function
    def check_pin(x_pin: Optional[str] = Header(None, alias="X-PIN"), pin: Optional[str] = Query(None)):
        expected_pin = config_manager.settings.pin
        if x_pin != expected_pin and pin != expected_pin:
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid PIN")

    # Define a handler to broadcast status to WebSockets
    async def broadcast_status(message: dict):
        for ws in list(active_websockets):
            try:
                await ws.send_json(message)
            except Exception:
                active_websockets.remove(ws)

    # Attach this handler to sync engine
    sync_engine.set_event_callback(broadcast_status)

    # --- Endpoints ---

    @app.get("/api/health")
    def health():
        return {"status": "ok", "node_name": config_manager.settings.node_name}

    # PIN validation endpoint
    @app.post("/api/auth/verify")
    def verify_auth(pin: str = Query(...)):
        if pin == config_manager.settings.pin:
            return {"status": "success", "node_name": config_manager.settings.node_name}
        raise HTTPException(status_code=401, detail="Invalid PIN")

    @app.get("/api/status")
    async def get_status(x_pin: Optional[str] = Header(None, alias="X-PIN")):
        check_pin(x_pin)
        conflicts = await db.get_pending_conflicts()
        peers = await db.get_peers()
        
        return {
            "node_name": config_manager.settings.node_name,
            "port": config_manager.settings.port,
            "pin": config_manager.settings.pin,
            "games_count": len(config_manager.settings.games),
            "peers_count": len([p for p in peers if p["status"] == "online"]),
            "conflicts_count": len(conflicts),
            "settings": config_manager.settings
        }

    # --- Config Management ---
    @app.post("/api/settings")
    async def update_settings(settings: AppSettings, x_pin: Optional[str] = Header(None, alias="X-PIN")):
        check_pin(x_pin)
        
        # Preserve PIN if not specifically requested to change, or allow changes
        config_manager.save_config(settings)
        
        # Apply watcher updates
        # Since watches might have changed, main.py should re-schedule watches. 
        # But we can import and trigger watches through a global or directly here.
        # Let's import the watcher class instance dynamically or trigger an update.
        # We can handle it via uvicorn state
        if hasattr(app.state, "watcher"):
            # Update all watches
            for game in settings.games:
                app.state.watcher.update_watch(game)
            # Remove deleted watches
            current_game_ids = {g.id for g in settings.games}
            for w_id in list(app.state.watcher.watches.keys()):
                if w_id not in current_game_ids:
                    app.state.watcher.remove_watch(w_id)
        
        return {"status": "success", "settings": config_manager.settings}

    # --- Game Profiles ---
    class GameProfileCreate(BaseModel):
        name: str
        path: str
        pattern: str = "*"

    @app.post("/api/games")
    async def add_game(profile: GameProfileCreate, x_pin: Optional[str] = Header(None, alias="X-PIN")):
        check_pin(x_pin)
        
        game_id = profile.name.lower().replace(" ", "_")
        # Check uniqueness
        if any(g.id == game_id for g in config_manager.settings.games):
            game_id = f"{game_id}_{int(time.time())}"
            
        new_game = GameProfile(
            id=game_id,
            name=profile.name,
            path=profile.path,
            pattern=profile.pattern
        )
        
        config_manager.settings.games.append(new_game)
        config_manager.save_config()
        
        if hasattr(app.state, "watcher"):
            app.state.watcher.update_watch(new_game)
            
        return {"status": "success", "game": new_game}

    @app.delete("/api/games/{game_id}")
    async def delete_game(game_id: str, x_pin: Optional[str] = Header(None, alias="X-PIN")):
        check_pin(x_pin)
        
        game = next((g for g in config_manager.settings.games if g.id == game_id), None)
        if not game:
            raise HTTPException(status_code=404, detail="Game not found")
            
        config_manager.settings.games.remove(game)
        config_manager.save_config()
        
        if hasattr(app.state, "watcher"):
            app.state.watcher.remove_watch(game_id)
            
        return {"status": "success"}

    # --- Safe Directory Browser ---
    @app.get("/api/browse")
    def browse_directories(path: Optional[str] = None, x_pin: Optional[str] = Header(None, alias="X-PIN")):
        check_pin(x_pin)
        
        if not path:
            shortcuts = []
            user_profile = os.environ.get("USERPROFILE")
            if user_profile:
                p = Path(user_profile)
                shortcuts.append({"name": "User Profile", "path": str(p)})
                if (p / "Documents").exists():
                    shortcuts.append({"name": "Documents", "path": str(p / "Documents")})
                if (p / "Saved Games").exists():
                    shortcuts.append({"name": "Saved Games", "path": str(p / "Saved Games")})
                if (p / "AppData" / "Local").exists():
                    shortcuts.append({"name": "AppData Local", "path": str(p / "AppData" / "Local")})
                if (p / "AppData" / "Roaming").exists():
                    shortcuts.append({"name": "AppData Roaming", "path": str(p / "AppData" / "Roaming")})
            
            # Windows Logical Drives
            if os.name == 'nt':
                try:
                    from ctypes import windll
                    bitmask = windll.kernel32.GetLogicalDrives()
                    for letter in string.ascii_uppercase:
                        if bitmask & 1:
                            shortcuts.append({"name": f"Local Disk ({letter}:)", "path": f"{letter}:\\"})
                        bitmask >>= 1
                except Exception:
                    pass
            return {"current_path": "", "directories": shortcuts}
        
        try:
            p = Path(path).resolve()
            if not p.exists():
                return {"error": "Path does not exist", "directories": []}
            
            dirs = []
            for child in p.iterdir():
                try:
                    if child.is_dir():
                        if not child.name.startswith('$') and not child.name.startswith('.'):
                            dirs.append({
                                "name": child.name,
                                "path": str(child)
                            })
                except Exception:
                    pass # Ignore permission errors for system subdirectories
            
            return {
                "current_path": str(p),
                "parent_path": str(p.parent) if p.parent != p else None,
                "directories": sorted(dirs, key=lambda x: x["name"].lower())
            }
        except Exception as e:
            return {"error": str(e), "directories": []}

    # --- Conflict Management ---
    @app.get("/api/conflicts")
    async def get_conflicts(x_pin: Optional[str] = Header(None, alias="X-PIN")):
        check_pin(x_pin)
        return await db.get_pending_conflicts()

    @app.post("/api/conflicts/{conflict_id}/resolve")
    async def resolve_conflict(conflict_id: int, selection: str = Query(...), x_pin: Optional[str] = Header(None, alias="X-PIN")):
        check_pin(x_pin)
        try:
            await sync_engine.resolve_conflict_selection(conflict_id, selection)
            return {"status": "success"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # --- Peers Panel ---
    @app.get("/api/peers")
    async def get_peers(x_pin: Optional[str] = Header(None, alias="X-PIN")):
        check_pin(x_pin)
        return await db.get_peers()

    @app.post("/api/peers")
    async def add_manual_peer(peer_address: str, x_pin: Optional[str] = Header(None, alias="X-PIN")):
        check_pin(x_pin)
        if peer_address not in config_manager.settings.manual_peers:
            config_manager.settings.manual_peers.append(peer_address)
            config_manager.save_config()
            return {"status": "success"}
        return {"status": "error", "message": "Peer already exists"}

    @app.delete("/api/peers")
    async def remove_manual_peer(peer_address: str, x_pin: Optional[str] = Header(None, alias="X-PIN")):
        check_pin(x_pin)
        if peer_address in config_manager.settings.manual_peers:
            config_manager.settings.manual_peers.remove(peer_address)
            config_manager.save_config()
            return {"status": "success"}
        raise HTTPException(status_code=404, detail="Peer not found")

    # --- Activity Logs ---
    @app.get("/api/logs")
    async def get_logs(x_pin: Optional[str] = Header(None, alias="X-PIN")):
        check_pin(x_pin)
        return await db.get_logs(limit=50)

    # --- Peer-to-Peer File Transfer Endpoints ---
    @app.post("/api/sync/push")
    async def receive_push(
        response: Response,
        x_game_id: str = Header(..., alias="X-Game-Id"),
        x_relative_path: str = Header(..., alias="X-Relative-Path"),
        x_file_size: int = Header(..., alias="X-File-Size"),
        x_mtime: float = Header(..., alias="X-Mtime"),
        x_sha256: str = Header(..., alias="X-Sha256"),
        x_sync_version: int = Header(..., alias="X-Sync-Version"),
        x_sender_id: str = Header(..., alias="X-Sender-Id"),
        x_sender_name: str = Header(..., alias="X-Sender-Name"),
        x_pin: str = Header(..., alias="X-PIN"),
        file: UploadFile = File(...)
    ):
        # Validate peer auth PIN
        if x_pin != config_manager.settings.pin:
            raise HTTPException(status_code=401, detail="Unauthorized peer")

        # Read binary file content
        content = await file.read()
        
        # Handle file sync logic
        res = await sync_engine.handle_incoming_push(
            game_id=x_game_id,
            relative_path=x_relative_path,
            size=x_file_size,
            mtime=x_mtime,
            sha256=x_sha256,
            sync_version=x_sync_version,
            sender_id=x_sender_id,
            sender_name=x_sender_name,
            file_content=content
        )
        
        if res["status"] == "error":
            raise HTTPException(status_code=400, detail=res["message"])
        elif res["status"] == "conflict":
            response.status_code = 409
            
        return res

    # --- WebSocket connection for web client ---
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket, pin: Optional[str] = Query(None)):
        # Validate websocket PIN connection
        if pin != config_manager.settings.pin:
            await websocket.accept()
            await websocket.send_json({"type": "auth_error", "message": "Invalid PIN"})
            await websocket.close(code=1008)
            return

        await websocket.accept()
        active_websockets.append(websocket)
        logger.info(f"WebSocket client connected from {websocket.client}")
        try:
            while True:
                # Keep connection open, handle incoming heartbeat or requests
                await websocket.receive_text()
        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        finally:
            if websocket in active_websockets:
                active_websockets.remove(websocket)

    # Static assets
    static_path = Path(__file__).parent / "static"
    if static_path.exists():
        app.mount("/", StaticFiles(directory=str(static_path), html=True), name="static")

    return app
