import socket
import asyncio
import time
import httpx
import logging
from typing import List, Dict, Any, Optional
from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser, ServiceListener
from .config import ConfigManager
from .database import SyncDatabase

logger = logging.getLogger("gamesync.discovery")

def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

class GameSyncListener(ServiceListener):
    def __init__(self, manager: 'DiscoveryManager'):
        self.manager = manager

    def add_service(self, zc: Zeroconf, type_: str, name: str):
        info = zc.get_service_info(type_, name)
        if info:
            addresses = info.parsed_addresses()
            if addresses:
                ip = addresses[0]
                port = info.port
                node_id = info.properties.get(b'node_id', b'').decode('utf-8')
                node_name = info.properties.get(b'node_name', b'').decode('utf-8')
                if node_id and node_id != self.manager.node_id:
                    self.manager.add_discovered_peer(node_id, node_name, ip, port)

    def remove_service(self, zc: Zeroconf, type_: str, name: str):
        # We can handle peer removal, but to prevent transient network drops
        # from marking a peer offline immediately, we rely on health checks.
        pass

    def update_service(self, zc: Zeroconf, type_: str, name: str):
        pass

class DiscoveryManager:
    def __init__(self, config_manager: ConfigManager, db: SyncDatabase, node_id: str):
        self.config_manager = config_manager
        self.db = db
        self.node_id = node_id
        self.zeroconf: Optional[Zeroconf] = None
        self.browser: Optional[ServiceBrowser] = None
        self.service_info: Optional[ServiceInfo] = None
        
        # In-memory track of discovered peers
        self.discovered_peers: Dict[str, Dict[str, Any]] = {}
        self.loop = None

    def start(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.zeroconf = Zeroconf()
        
        # Register self
        local_ip = get_local_ip()
        port = self.config_manager.settings.port
        node_name = self.config_manager.settings.node_name
        
        desc = {
            'node_id': self.node_id,
            'node_name': node_name
        }
        
        self.service_info = ServiceInfo(
            "_gamesync._tcp.local.",
            f"{node_name}.{self.node_id}._gamesync._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=port,
            properties=desc,
            server=f"{node_name}.local."
        )
        
        try:
            self.zeroconf.register_service(self.service_info)
            logger.info(f"Registered service: {node_name} on {local_ip}:{port}")
        except Exception as e:
            logger.error(f"Failed to register zeroconf service: {e}")

        # Start browser to find others
        self.browser = ServiceBrowser(self.zeroconf, "_gamesync._tcp.local.", GameSyncListener(self))
        
        # Start background task for peer health checks
        self.loop.create_task(self._peer_health_check_loop())

    def stop(self):
        if self.zeroconf:
            if self.service_info:
                try:
                    self.zeroconf.unregister_service(self.service_info)
                except Exception:
                    pass
            self.zeroconf.close()
            self.zeroconf = None
        self.browser = None

    def add_discovered_peer(self, node_id: str, name: str, ip: str, port: int):
        self.discovered_peers[node_id] = {
            "name": name,
            "host": ip,
            "port": port,
            "last_seen": time.time()   # epoch time, consistent with DB
        }
        if self.loop:
            asyncio.run_coroutine_threadsafe(
                self.db.update_peer(node_id, name, ip, port, is_manual=False, status="online"),
                self.loop
            )

    async def _peer_health_check_loop(self):
        while True:
            await asyncio.sleep(15)  # Health check every 15 seconds
            
            # 1. Health check discovered peers
            for peer_id, info in list(self.discovered_peers.items()):
                online = await self._ping_peer(info["host"], info["port"])
                if online:
                    await self.db.update_peer(peer_id, info["name"], info["host"], info["port"], is_manual=False, status="online")
                else:
                    await self.db.update_peer(peer_id, info["name"], info["host"], info["port"], is_manual=False, status="offline")
            
            # 2. Health check manual peers
            for manual_peer in self.config_manager.settings.manual_peers:
                # manual_peer format is "IP:port"
                try:
                    parts = manual_peer.split(":")
                    host = parts[0]
                    port = int(parts[1]) if len(parts) > 1 else 8384
                    
                    online = await self._ping_peer(host, port)
                    peer_id = f"manual_{host}_{port}"
                    peer_name = f"Manual ({host})"
                    
                    await self.db.update_peer(
                        peer_id, 
                        peer_name, 
                        host, 
                        port, 
                        is_manual=True, 
                        status="online" if online else "offline"
                    )
                except Exception as e:
                    logger.error(f"Error checking manual peer {manual_peer}: {e}")

    async def _ping_peer(self, host: str, port: int) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"http://{host}:{port}/api/health")
                if response.status_code == 200:
                    data = response.json()
                    return data.get("status") == "ok"
        except Exception:
            pass
        return False
