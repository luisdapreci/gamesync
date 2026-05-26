import asyncio
import fnmatch
import logging
import uvicorn
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI

from .config import ConfigManager
from .database import SyncDatabase
from .discovery import DiscoveryManager
from .watcher import SaveWatcher
from .sync_engine import SyncEngine, compute_sha256
from .api import create_app

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("gamesync.main")

config_manager = ConfigManager()
db = SyncDatabase()
node_id = config_manager.settings.node_id
sync_engine = SyncEngine(config_manager, db, node_id)

async def run_initial_scan(config_manager: ConfigManager, db: SyncDatabase, sync_engine: SyncEngine):
    logger.info("Running initial startup scan of save directories...")
    for game in config_manager.settings.games:
        root_path = Path(game.path).resolve()
        if not root_path.exists():
            logger.warning(f"Save directory for {game.name} does not exist at {root_path}")
            continue
        
        # Read ignores
        from .watcher import IGNORE_EXTENSIONS
        
        # Find all files recursively in the save path
        for file_path in root_path.rglob("*"):
            if file_path.is_file():
                if file_path.suffix.lower() in IGNORE_EXTENSIONS:
                    continue
                
                # Calculate relative path
                try:
                    relative_path = file_path.relative_to(root_path)
                    rel_str = str(relative_path).replace('\\', '/')
                except ValueError:
                    continue
                
                # Apply the game-specific glob pattern filter
                pattern = game.pattern or "*"
                if not fnmatch.fnmatch(file_path.name, pattern):
                    continue
                
                stat = file_path.stat()
                file_size = stat.st_size
                mtime = stat.st_mtime
                sha256 = compute_sha256(file_path)
                
                # Check DB state
                db_state = await db.get_file_state(game.id, rel_str)
                
                if not db_state:
                    logger.info(f"Scan: Found new save file {game.name}/{rel_str}")
                    await db.update_file_state(game.id, rel_str, file_size, mtime, sha256, status="synced", bump_version=True)
                    new_db_state = await db.get_file_state(game.id, rel_str)
                    await db.log_event(game.id, rel_str, "local_change", details="Found during initial scan")
                elif db_state["sha256"] != sha256:
                    logger.info(f"Scan: Found offline modified save file {game.name}/{rel_str}")
                    await db.update_file_state(game.id, rel_str, file_size, mtime, sha256, status="synced", bump_version=True)
                    new_db_state = await db.get_file_state(game.id, rel_str)
                    await db.log_event(game.id, rel_str, "local_change", details="Modified offline")

# Define file change callback for watchdog
def on_file_changed(game_id: str, relative_path: str):
    logger.info(f"File watch trigger: {game_id} -> {relative_path}")
    sync_engine.handle_local_change(game_id, relative_path)

# Initialize watcher and discovery
watcher = SaveWatcher(on_file_changed)
discovery_manager = DiscoveryManager(config_manager, db, node_id)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup tasks
    logger.info("Initializing GameSync DB...")
    await db.connect()
    
    # Wire the running event loop into sync_engine so worker threads can schedule coroutines
    running_loop = asyncio.get_running_loop()
    sync_engine.loop = running_loop
    
    # Store watcher in app state so REST endpoints can update watches dynamically
    app.state.watcher = watcher
    
    logger.info("Starting peer discovery...")
    loop = asyncio.get_running_loop()
    discovery_manager.start(loop)
    
    # Scan before we start watching to avoid feedback loops
    await run_initial_scan(config_manager, db, sync_engine)
    
    logger.info("Starting file watcher...")
    watcher.start()
    for game in config_manager.settings.games:
        watcher.update_watch(game)
        
    yield
    
    # Shutdown tasks
    logger.info("Stopping file watcher...")
    watcher.stop()
    
    logger.info("Stopping peer discovery...")
    discovery_manager.stop()
    
    logger.info("Closing database...")
    await db.close()

# Create FastAPI app
app = create_app(config_manager, db, sync_engine)
# Set lifespan
app.router.lifespan_context = lifespan

def main():
    port = config_manager.settings.port
    logger.info(f"Starting GameSync Server on port {port}...")

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        # Release the port immediately when the process exits so a quick
        # restart never hits "address already in use".
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)

    # Patch the underlying socket to set SO_REUSEADDR before binding,
    # which lets a new process claim the port even if the old one's
    # TIME_WAIT sockets haven't fully expired yet.
    import socket as _socket
    _orig_bind = _socket.socket.bind
    def _reuseaddr_bind(self, *args, **kwargs):
        try:
            self.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        return _orig_bind(self, *args, **kwargs)
    _socket.socket.bind = _reuseaddr_bind

    try:
        server.run()
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down.")
    finally:
        _socket.socket.bind = _orig_bind  # restore

if __name__ == "__main__":
    main()
