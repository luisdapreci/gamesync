from pydantic import BaseModel, Field
from typing import List, Optional

class GameProfile(BaseModel):
    id: str
    name: str
    path: str
    pattern: str = "*"  # Glob pattern to sync, e.g. *.sav, *.json or *

class PeerInfo(BaseModel):
    id: str
    name: str
    host: str
    port: int
    is_manual: bool = False
    last_seen: float

class AppSettings(BaseModel):
    node_id: str = ""  # Unique UUID or generated string for node identification
    node_name: str
    port: int = 8384
    pin: str  # Set by ConfigManager (randomly generated on first run)
    games: List[GameProfile] = Field(default_factory=list)
    manual_peers: List[str] = Field(default_factory=list)  # IP:port strings
    backup_count: int = 3
    warn_file_size_mb: int = 50  # File size warning threshold in MB
    run_on_startup: bool = False
