/* ----------------------------------------------------
   GameSync Client Application Logic
   ---------------------------------------------------- */

// App state
let API_PIN = localStorage.getItem("gamesync_pin") || "";
let ws = null;
let currentBrowserPath = "";

// DOM Elements
const authScreen = document.getElementById("auth-screen");
const appContainer = document.getElementById("app-container");
const pinInput = document.getElementById("pin-input");
const authBtn = document.getElementById("auth-btn");
const authError = document.getElementById("auth-error");
const logoutBtn = document.getElementById("logout-btn");

const currentNodeName = document.getElementById("current-node-name");
const wsStatusIndicator = document.getElementById("ws-status-indicator");

// Statistics
const statGames = document.getElementById("stat-games");
const statPeers = document.getElementById("stat-peers");
const statConflicts = document.getElementById("stat-conflicts");
const conflictSummaryCard = document.getElementById("conflict-summary-card");
const conflictsSection = document.getElementById("conflicts-section");
const conflictsList = document.getElementById("conflicts-list");

// Lists
const gamesList = document.getElementById("games-list");
const peersList = document.getElementById("peers-list");
const logsList = document.getElementById("logs-list");

// Peer additions
const peerIpInput = document.getElementById("peer-ip-input");
const addPeerBtn = document.getElementById("add-peer-btn");

// Settings Form
const settingsForm = document.getElementById("settings-form");
const settingPin = document.getElementById("setting-pin");
const settingBackups = document.getElementById("setting-backups");
const settingWarnSize = document.getElementById("setting-warn-size");
const settingStartup = document.getElementById("setting-startup");

// Add Game Modal
const addGameModal = document.getElementById("add-game-modal");
const addGameBtn = document.getElementById("add-game-btn");
const gameNameInput = document.getElementById("game-name-input");
const gamePathInput = document.getElementById("game-path-input");
const gamePatternInput = document.getElementById("game-pattern-input");
const saveGameBtn = document.getElementById("save-game-btn");
const btnCloseModalList = document.querySelectorAll(".btn-close-modal");

// Directory Browser Modal
const directoryBrowserModal = document.getElementById("directory-browser-modal");
const browseDirBtn = document.getElementById("browse-dir-btn");
const browserBackBtn = document.getElementById("browser-back-btn");
const browserCurrentPath = document.getElementById("browser-current-path");
const browserListContainer = document.getElementById("browser-list-container");
const selectDirBtn = document.getElementById("select-dir-btn");
const btnCloseBrowserList = document.querySelectorAll(".btn-close-browser");

// Toast Container
const toastContainer = document.getElementById("toast-container");

// API Request Helper
async function apiFetch(endpoint, options = {}) {
    const url = `${window.location.origin}${endpoint}`;
    
    // Add auth headers
    const headers = {
        "Content-Type": "application/json",
        "X-PIN": API_PIN,
        ...(options.headers || {})
    };
    
    const fetchOptions = {
        ...options,
        headers
    };
    
    try {
        const response = await fetch(url, fetchOptions);
        if (response.status === 401) {
            // Unauthorized
            showAuthError("Invalid PIN session. Please unlock again.");
            lockConsole();
            throw new Error("Unauthorized");
        }
        if (!response.ok) {
            const errData = await response.json().catch(() => ({}));
            throw new Error(errData.detail || `HTTP error ${response.status}`);
        }
        return await response.json();
    } catch (e) {
        console.error(`API Fetch Error [${endpoint}]:`, e);
        throw e;
    }
}

// Show Toast message
function showToast(message, type = "success") {
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    
    let icon = "fa-circle-check";
    if (type === "warning") icon = "fa-circle-exclamation";
    if (type === "error") icon = "fa-circle-xmark";
    
    toast.innerHTML = `
        <i class="fa-solid ${icon}"></i>
        <div class="toast-content">${message}</div>
        <button class="toast-close"><i class="fa-solid fa-xmark"></i></button>
    `;
    
    toastContainer.appendChild(toast);
    
    // Auto dismiss
    const timeout = setTimeout(() => {
        toast.style.opacity = "0";
        setTimeout(() => toast.remove(), 300);
    }, 4500);
    
    toast.querySelector(".toast-close").addEventListener("click", () => {
        clearTimeout(timeout);
        toast.remove();
    });
}

// Authentication Logic
async function tryUnlock(pinCode) {
    if (!pinCode) {
        showAuthError("Please enter a PIN");
        return;
    }
    
    try {
        const response = await fetch(`${window.location.origin}/api/auth/verify`, {
            method: "POST",
            headers: { "X-PIN": pinCode }
        });
        
        if (response.ok) {
            const data = await response.json();
            API_PIN = pinCode;
            localStorage.setItem("gamesync_pin", pinCode);
            authScreen.classList.add("hidden");
            appContainer.classList.remove("hidden");
            
            showToast(`Unlocked console: ${data.node_name}`, "success");
            initDashboard();
        } else {
            showAuthError("Invalid PIN code.");
        }
    } catch (e) {
        showAuthError("Failed to connect to GameSync service.");
    }
}

function showAuthError(msg) {
    authError.textContent = msg;
    pinInput.classList.add("shake");
    setTimeout(() => pinInput.classList.remove("shake"), 500);
}

function lockConsole() {
    API_PIN = "";
    localStorage.removeItem("gamesync_pin");
    if (ws) {
        ws.close();
    }
    appContainer.classList.add("hidden");
    authScreen.classList.remove("hidden");
    pinInput.value = "";
    authError.textContent = "";
}

// WS Connection
function connectWebSocket() {
    if (ws) ws.close();
    
    const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${wsProto}//${window.location.host}/ws?pin=${API_PIN}`;
    
    ws = new WebSocket(wsUrl);
    
    ws.onopen = () => {
        wsStatusIndicator.className = "status-indicator online";
        wsStatusIndicator.innerHTML = '<span class="pulse-ring"></span>Connected';
    };
    
    ws.onclose = () => {
        wsStatusIndicator.className = "status-indicator offline";
        wsStatusIndicator.innerHTML = '<span class="pulse-ring"></span>Disconnected';
        // Auto reconnect
        setTimeout(connectWebSocket, 4000);
    };
    
    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleWsMessage(msg);
        } catch (e) {
            console.error("WS parse error:", e);
        }
    };
}

function handleWsMessage(msg) {
    console.log("WebSocket event received:", msg);
    
    if (msg.type === "auth_error") {
        showToast("Session expired, please unlock again", "error");
        lockConsole();
        return;
    }
    
    if (msg.type === "sync_log") {
        addLogToTable(msg.data, true);
        showToast(`Sync update: ${msg.data.relative_path}`, "success");
        refreshStats();
    }
    
    if (msg.type === "conflict") {
        showToast(`Conflict detected in file: ${msg.data.relative_path}`, "warning");
        refreshConflicts();
        refreshStats();
    }
    
    if (msg.type === "warning") {
        showToast(msg.data.message, "warning");
    }
}

// Initializers & Refreshers
async function initDashboard() {
    connectWebSocket();
    await refreshStats();
    await refreshGames();
    await refreshPeers();
    await refreshConflicts();
    await loadInitialLogs();
}

async function refreshStats() {
    try {
        const status = await apiFetch("/api/status");
        currentNodeName.textContent = status.node_name;
        statGames.textContent = status.games_count;
        statPeers.textContent = status.peers_count;
        statConflicts.textContent = status.conflicts_count;
        
        // Populate settings fields on initial load
        if (!settingBackups.value) {
            settingBackups.value = status.settings.backup_count;
            settingWarnSize.value = status.settings.warn_file_size_mb;
            settingStartup.checked = status.settings.run_on_startup;
        }
    } catch (e) {}
}

async function refreshGames() {
    try {
        const status = await apiFetch("/api/status");
        const games = status.settings.games;
        
        if (games.length === 0) {
            gamesList.innerHTML = `
                <div class="empty-state">
                    <i class="fa-solid fa-circle-info"></i>
                    <p>No games configured yet. Click "Add Game" to get started.</p>
                </div>`;
            return;
        }
        
        gamesList.innerHTML = "";
        games.forEach(game => {
            const item = document.createElement("div");
            item.className = "game-item";
            item.innerHTML = `
                <div class="game-info">
                    <div class="game-title">${escapeHtml(game.name)}</div>
                    <div class="game-path">${escapeHtml(game.path)}</div>
                    <div class="game-meta">Sync Pattern: <code>${escapeHtml(game.pattern)}</code></div>
                </div>
                <div class="game-actions">
                    <button class="btn btn-sm btn-danger delete-game-btn" data-id="${game.id}">
                        <i class="fa-solid fa-trash-can"></i> Remove
                    </button>
                </div>
            `;
            gamesList.appendChild(item);
        });
        
        // Attach delete listeners
        document.querySelectorAll(".delete-game-btn").forEach(btn => {
            btn.addEventListener("click", async (e) => {
                const gameId = btn.getAttribute("data-id");
                if (confirm("Are you sure you want to stop syncing this game? Existing save files won't be deleted.")) {
                    try {
                        await apiFetch(`/api/games/${gameId}`, { method: "DELETE" });
                        showToast("Game profile removed");
                        refreshGames();
                        refreshStats();
                    } catch (err) {
                        showToast(err.message, "error");
                    }
                }
            });
        });
        
    } catch (e) {}
}

async function refreshPeers() {
    try {
        const peers = await apiFetch("/api/peers");
        
        if (peers.length === 0) {
            peersList.innerHTML = `
                <div class="empty-state">
                    <i class="fa-solid fa-circle-wifi"></i>
                    <p>Scanning network... Add IPs manually to bypass autodiscovery.</p>
                </div>`;
            return;
        }
        
        peersList.innerHTML = "";
        peers.forEach(peer => {
            const item = document.createElement("div");
            item.className = "peer-item";
            
            const isOnline = peer.status === "online";
            const peerTypeBadge = peer.is_manual ? "manual" : "auto";
            
            item.innerHTML = `
                <div class="peer-name-container">
                    <div class="peer-name">
                        <span class="status-indicator ${isOnline ? 'online' : 'offline'}">
                            <span class="pulse-ring"></span>
                        </span>
                        ${escapeHtml(peer.name)}
                    </div>
                    <div class="peer-host">${escapeHtml(peer.host)}:${peer.port}</div>
                </div>
                <div class="peer-actions">
                    <span class="peer-badge ${peerTypeBadge}">${peerTypeBadge.toUpperCase()}</span>
                    ${peer.is_manual ? `
                        <button class="btn-icon remove-peer-btn" data-addr="${peer.host}:${peer.port}" title="Remove Manual Peer">
                            <i class="fa-solid fa-xmark"></i>
                        </button>` : ''
                    }
                </div>
            `;
            peersList.appendChild(item);
        });
        
        // Attach remove manual peer listeners
        document.querySelectorAll(".remove-peer-btn").forEach(btn => {
            btn.addEventListener("click", async () => {
                const addr = btn.getAttribute("data-addr");
                try {
                    await apiFetch(`/api/peers?peer_address=${encodeURIComponent(addr)}`, { method: "DELETE" });
                    showToast("Manual peer removed");
                    refreshPeers();
                    refreshStats();
                } catch (err) {
                    showToast(err.message, "error");
                }
            });
        });
    } catch (e) {}
}

async function refreshConflicts() {
    try {
        const conflicts = await apiFetch("/api/conflicts");
        
        if (conflicts.length === 0) {
            conflictsSection.classList.add("hidden");
            conflictSummaryCard.classList.remove("text-amber");
            return;
        }
        
        conflictsSection.classList.remove("hidden");
        conflictSummaryCard.classList.add("text-amber");
        
        conflictsList.innerHTML = "";
        conflicts.forEach(c => {
            const localDate = new Date(c.local_mtime * 1000).toLocaleString();
            const remoteDate = new Date(c.remote_mtime * 1000).toLocaleString();
            
            const isLocalNewer = c.local_mtime > c.remote_mtime;
            
            const item = document.createElement("div");
            item.className = "conflict-item";
            item.innerHTML = `
                <div class="conflict-title">
                    <span><i class="fa-solid fa-code-compare"></i> Save File Conflict: <strong>${escapeHtml(c.relative_path)}</strong></span>
                    <small class="text-amber">Detected ${new Date(c.detected_at * 1000).toLocaleTimeString()}</small>
                </div>
                <div class="conflict-compare">
                    <div class="conflict-side ${isLocalNewer ? 'recommended' : ''}">
                        <h4>This PC (Local) ${isLocalNewer ? '<span class="peer-badge">NEWER</span>' : ''}</h4>
                        <ul>
                            <li>Size: <span>${formatBytes(c.local_size)}</span></li>
                            <li>Modified: <span>${localDate}</span></li>
                            <li>Hash: <span>${c.local_sha256.substring(0, 8)}</span></li>
                        </ul>
                    </div>
                    <div class="conflict-side ${!isLocalNewer ? 'recommended' : ''}">
                        <h4>${escapeHtml(c.remote_peer_name)} (Remote) ${!isLocalNewer ? '<span class="peer-badge">NEWER</span>' : ''}</h4>
                        <ul>
                            <li>Size: <span>${formatBytes(c.remote_size)}</span></li>
                            <li>Modified: <span>${remoteDate}</span></li>
                            <li>Hash: <span>${c.remote_sha256.substring(0, 8)}</span></li>
                        </ul>
                    </div>
                </div>
                <div class="conflict-actions">
                    <button class="btn btn-secondary resolve-btn" data-id="${c.id}" data-select="local">Keep Local Version</button>
                    <button class="btn btn-primary resolve-btn" data-id="${c.id}" data-select="remote">Keep Remote Version</button>
                </div>
            `;
            conflictsList.appendChild(item);
        });
        
        // Attach conflict resolvers
        document.querySelectorAll(".resolve-btn").forEach(btn => {
            btn.addEventListener("click", async () => {
                const id = btn.getAttribute("data-id");
                const select = btn.getAttribute("data-select");
                try {
                    await apiFetch(`/api/conflicts/${id}/resolve?selection=${select}`, { method: "POST" });
                    showToast("Conflict resolved and synced");
                    refreshConflicts();
                    refreshStats();
                    loadInitialLogs();
                } catch (err) {
                    showToast(err.message, "error");
                }
            });
        });
    } catch (e) {}
}

async function loadInitialLogs() {
    try {
        const logs = await apiFetch("/api/logs");
        logsList.innerHTML = "";
        logs.forEach(log => addLogToTable(log, false));
    } catch (e) {}
}

function addLogToTable(log, prepend = true) {
    const row = document.createElement("tr");
    const formattedTime = new Date(log.timestamp * 1000).toLocaleTimeString();
    
    row.innerHTML = `
        <td class="log-time">${formattedTime}</td>
        <td><strong>${escapeHtml(log.game_id)}</strong></td>
        <td>${escapeHtml(log.relative_path)}</td>
        <td><span class="badge-action ${log.action}">${escapeHtml(log.action.toUpperCase())}</span></td>
        <td>${log.peer_name ? escapeHtml(log.peer_name) : '—'}</td>
        <td class="text-muted">${escapeHtml(log.details || '')}</td>
    `;
    
    if (prepend && logsList.firstChild) {
        logsList.insertBefore(row, logsList.firstChild);
        // Truncate logs list if too long
        if (logsList.children.length > 50) {
            logsList.lastChild.remove();
        }
    } else {
        logsList.appendChild(row);
    }
}

// Directory Browser Traversal
async function loadBrowserPath(path = "") {
    currentBrowserPath = path;
    browserCurrentPath.value = path;
    
    // Disable back button if at drive selection level
    browserBackBtn.disabled = !path;
    
    browserListContainer.innerHTML = '<div class="empty-state"><i class="fa-solid fa-spinner fa-spin"></i><p>Scanning files...</p></div>';
    
    try {
        const url = `/api/browse${path ? `?path=${encodeURIComponent(path)}` : ''}`;
        const data = await apiFetch(url);
        
        if (data.error) {
            browserListContainer.innerHTML = `<div class="empty-state"><i class="fa-solid fa-triangle-exclamation"></i><p>${escapeHtml(data.error)}</p></div>`;
            return;
        }
        
        if (data.directories.length === 0) {
            browserListContainer.innerHTML = '<div class="empty-state"><i class="fa-solid fa-folder-open"></i><p>This folder is empty.</p></div>';
            return;
        }
        
        browserListContainer.innerHTML = "";
        
        // shortcuts display slightly differently
        const isRoots = !path;
        
        data.directories.forEach(dir => {
            const div = document.createElement("div");
            div.className = `browser-item ${isRoots ? 'shortcut' : ''}`;
            div.innerHTML = `
                <i class="fa-solid ${isRoots ? 'fa-hard-drive' : 'fa-folder'}"></i>
                <span>${escapeHtml(dir.name)}</span>
            `;
            
            // Double click to traverse inside folder
            div.addEventListener("dblclick", () => {
                loadBrowserPath(dir.path);
            });
            
            // Single click to select
            div.addEventListener("click", () => {
                document.querySelectorAll(".browser-item").forEach(item => item.style.background = "transparent");
                div.style.background = "rgba(0, 191, 255, 0.15)";
                currentBrowserPath = dir.path;
                browserCurrentPath.value = dir.path;
            });
            
            browserListContainer.appendChild(div);
        });
        
    } catch (e) {
        browserListContainer.innerHTML = '<div class="empty-state"><i class="fa-solid fa-triangle-exclamation"></i><p>Failed to scan directory.</p></div>';
    }
}

// Setup Event Listeners
authBtn.addEventListener("click", () => tryUnlock(pinInput.value));
pinInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") tryUnlock(pinInput.value);
});

logoutBtn.addEventListener("click", lockConsole);

addPeerBtn.addEventListener("click", async () => {
    const addr = peerIpInput.value.trim();
    if (!addr) return;
    try {
        await apiFetch(`/api/peers?peer_address=${encodeURIComponent(addr)}`, { method: "POST" });
        showToast("Manual peer added successfully");
        peerIpInput.value = "";
        refreshPeers();
        refreshStats();
    } catch (e) {
        showToast(e.message, "error");
    }
});

// Settings Form Submission
settingsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
        const currentStatus = await apiFetch("/api/status");
        const payload = {
            ...currentStatus.settings,
            pin: settingPin.value.trim(),
            backup_count: parseInt(settingBackups.value),
            warn_file_size_mb: parseInt(settingWarnSize.value),
            run_on_startup: settingStartup.checked
        };
        
        await apiFetch("/api/settings", {
            method: "POST",
            body: JSON.stringify(payload)
        });
        
        // Update PIN session code
        API_PIN = payload.pin;
        localStorage.setItem("gamesync_pin", payload.pin);
        
        showToast("Settings saved successfully!");
        refreshStats();
    } catch (err) {
        showToast(err.message, "error");
    }
});

// Modals Handlers
addGameBtn.addEventListener("click", () => {
    addGameModal.classList.remove("hidden");
    gameNameInput.value = "";
    gamePathInput.value = "";
});

btnCloseModalList.forEach(btn => {
    btn.addEventListener("click", () => addGameModal.classList.add("hidden"));
});

saveGameBtn.addEventListener("click", async () => {
    const name = gameNameInput.value.trim();
    const path = gamePathInput.value.trim();
    const pattern = gamePatternInput.value.trim() || "*";
    
    if (!name || !path) {
        showToast("Game Name and Save Path are required", "error");
        return;
    }
    
    try {
        await apiFetch("/api/games", {
            method: "POST",
            body: JSON.stringify({ name, path, pattern })
        });
        showToast("Game profile created!");
        addGameModal.classList.add("hidden");
        refreshGames();
        refreshStats();
    } catch (err) {
        showToast(err.message, "error");
    }
});

// Directory Browser triggers
browseDirBtn.addEventListener("click", () => {
    directoryBrowserModal.classList.remove("hidden");
    loadBrowserPath(""); // Load defaults
});

btnCloseBrowserList.forEach(btn => {
    btn.addEventListener("click", () => directoryBrowserModal.classList.add("hidden"));
});

browserBackBtn.addEventListener("click", () => {
    if (!currentBrowserPath) return;
    // Go up one level
    const parts = currentBrowserPath.split(/[\\/]/);
    if (parts.length <= 2 && parts[0].endsWith(":")) {
        // We are at root, e.g. "C:\\" or similar
        loadBrowserPath("");
    } else {
        parts.pop();
        // Check if empty (like last trailing separator)
        if (!parts[parts.length - 1]) parts.pop();
        const parent = parts.join(currentBrowserPath.includes("/") ? "/" : "\\");
        loadBrowserPath(parent || "/");
    }
});

selectDirBtn.addEventListener("click", () => {
    if (currentBrowserPath) {
        gamePathInput.value = currentBrowserPath;
        directoryBrowserModal.classList.add("hidden");
    } else {
        showToast("Please choose or select a folder first", "warning");
    }
});

// Utilities
function escapeHtml(str) {
    if (typeof str !== 'string') return '';
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function formatBytes(bytes, decimals = 2) {
    if (!+bytes) return '0 Bytes';
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}

// Auto Load Console Check
if (API_PIN) {
    tryUnlock(API_PIN);
}
