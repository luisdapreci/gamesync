import os
import json
import socket
import random
from pathlib import Path
from typing import Dict, Any
from .models import AppSettings, GameProfile

CONFIG_DIR = Path.home() / ".gamesync"
CONFIG_FILE = CONFIG_DIR / "config.json"
STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
STARTUP_BAT = STARTUP_DIR / "GameSync.bat"

class ConfigManager:
    def __init__(self):
        self.config_dir = CONFIG_DIR
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.settings = self.load_config()

    def load_config(self) -> AppSettings:
        import uuid
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    settings = AppSettings.model_validate(data)
                    if not settings.node_id:
                        settings.node_id = str(uuid.uuid4())
                        self.save_config(settings)
                    return settings
            except Exception as e:
                print(f"Error loading config, using defaults: {e}")
        
        # Generate default settings
        node_name = socket.gethostname()
        if not node_name:
            node_name = f"Node-{random.randint(1000, 9999)}"
        
        default_settings = AppSettings(
            node_id=str(uuid.uuid4()),
            node_name=node_name,
            port=8384,
            pin=f"{random.randint(1000, 9999)}",  # Auto-generate a random 4-digit PIN for first setup
            games=[],
            manual_peers=[],
            backup_count=3,
            warn_file_size_mb=50,
            run_on_startup=False
        )
        self.save_config(default_settings)
        return default_settings

    def save_config(self, settings: AppSettings = None):
        if settings:
            self.settings = settings
        
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.settings.model_dump(), f, indent=4)
        
        # Handle Windows startup registry/batch file
        self._update_startup_status()

    def _update_startup_status(self):
        # We check if we are on Windows and if startup is requested
        if os.name == 'nt' and STARTUP_DIR.exists():
            try:
                if self.settings.run_on_startup:
                    # Create startup batch script
                    run_py_path = Path(__file__).parent.parent / "run.py"
                    content = f'@echo off\nstart "" /B python "{run_py_path.resolve()}"\n'
                    with open(STARTUP_BAT, "w", encoding="utf-8") as f:
                        f.write(content)
                else:
                    # Remove batch script if exists
                    if STARTUP_BAT.exists():
                        STARTUP_BAT.unlink()
            except Exception as e:
                print(f"Failed to update startup script: {e}")
