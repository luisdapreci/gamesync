import os
import time
import logging
import threading
from pathlib import Path
from typing import Dict, Any, Callable, Optional
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from .models import GameProfile

logger = logging.getLogger("gamesync.watcher")

# Temporary files or lockfiles to ignore
IGNORE_EXTENSIONS = {
    '.tmp', '.temp', '.lock', '.swp', '.part', '.crdownload',
    '.bak', '~', '.log', '.git'
}

class DebouncedHandler:
    def __init__(self, delay: float, callback: Callable[[str, str], Any]):
        self.delay = delay
        self.callback = callback
        self.timers: Dict[str, threading.Timer] = {}
        self.lock = threading.Lock()

    def touch(self, game_id: str, relative_path: str, absolute_path: str):
        key = f"{game_id}::{relative_path}"
        with self.lock:
            if key in self.timers:
                self.timers[key].cancel()
            
            # Start a timer to call callback after 'delay' seconds
            timer = threading.Timer(
                self.delay, 
                self._fire, 
                args=[game_id, relative_path, absolute_path]
            )
            self.timers[key] = timer
            timer.start()

    def _fire(self, game_id: str, relative_path: str, absolute_path: str):
        with self.lock:
            key = f"{game_id}::{relative_path}"
            if key in self.timers:
                del self.timers[key]
        
        # Check if the file still exists (not a deletion event or temporary file)
        if os.path.exists(absolute_path):
            self.callback(game_id, relative_path)

class GameDirectoryHandler(FileSystemEventHandler):
    def __init__(self, game_profile: GameProfile, debouncer: DebouncedHandler):
        super().__init__()
        self.game_profile = game_profile
        self.debouncer = debouncer
        self.root_path = Path(game_profile.path).resolve()

    def on_any_event(self, event):
        if event.is_directory:
            return

        # Check action type
        if event.event_type not in ('modified', 'created'):
            return

        src_path = Path(event.src_path).resolve()
        
        # Ignore files with matching extension
        if src_path.suffix.lower() in IGNORE_EXTENSIONS:
            return

        # Double check if inside watched path
        try:
            relative_path = src_path.relative_to(self.root_path)
            rel_str = str(relative_path).replace('\\', '/')
            self.debouncer.touch(self.game_profile.id, rel_str, event.src_path)
        except ValueError:
            # Not in folder
            pass

class SaveWatcher:
    def __init__(self, file_change_callback: Callable[[str, str], Any]):
        self.file_change_callback = file_change_callback
        self.debouncer = DebouncedHandler(delay=2.0, callback=self._on_debounced_change)
        self.observer = Observer()
        self.watches: Dict[str, Any] = {}
        self.is_running = False

    def start(self):
        if not self.is_running:
            self.observer.start()
            self.is_running = True
            logger.info("Save watcher observer thread started")

    def stop(self):
        if self.is_running:
            self.observer.stop()
            self.observer.join()
            self.is_running = False
            logger.info("Save watcher observer thread stopped")

    def update_watch(self, game_profile: GameProfile):
        """Add or update watcher for a specific game profile."""
        self.remove_watch(game_profile.id)
        
        path = Path(game_profile.path).resolve()
        if not path.exists():
            logger.warning(f"Watched path does not exist for game {game_profile.name}: {path}")
            return
            
        handler = GameDirectoryHandler(game_profile, self.debouncer)
        try:
            # Schedule watch (recursive=True to capture nested save folders)
            watch = self.observer.schedule(handler, str(path), recursive=True)
            self.watches[game_profile.id] = watch
            logger.info(f"Started watching: {game_profile.name} at {path}")
        except Exception as e:
            logger.error(f"Failed to watch directory {path} for game {game_profile.name}: {e}")

    def remove_watch(self, game_id: str):
        """Remove a watched directory if exists."""
        if game_id in self.watches:
            watch = self.watches.pop(game_id)
            try:
                self.observer.unschedule(watch)
                logger.info(f"Stopped watching game: {game_id}")
            except Exception as e:
                logger.error(f"Failed to unschedule watch for game {game_id}: {e}")

    def _on_debounced_change(self, game_id: str, relative_path: str):
        # Fire off-thread file change to core sync engine
        self.file_change_callback(game_id, relative_path)
