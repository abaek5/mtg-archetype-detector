import re
import sys
import time
import json
import os
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Arena log path (Windows default) ──────────────────────────────────────────
LOG_PATH = Path(os.path.expandvars(
    r"%APPDATA%\..\LocalLow\Wizards of the Coast\MTGA\Player.log"
))

# ── Shared state ───────────────────────────────────────────────────────────────
state = {
    "cards": [],       # opponent cards seen this game
    "game_active": False,
    "last_update": 0,
}
state_lock = threading.Lock()

# ── Card name lookup from grpId ────────────────────────────────────────────────
# Arena logs card plays by grpId (integer). We resolve names from the log itself
# when Arena prints a "greased" card name in GRE messages, or fall back to
# Scryfall bulk data. This regex covers the most common log patterns.

# Pattern 1: GRE game state messages  →  "cardName":"Lightning Bolt"
RE_CARD_NAME_KV  = re.compile(r'"cardName"\s*:\s*"([^"]+)"')
# Pattern 2: opponent zone change with name inline
RE_ZONE_CHANGE   = re.compile(r'GREMessageType_GameStateMessage.*?"ownerSeatId"\s*:\s*(\d+).*?"cardName"\s*:\s*"([^"]+)"', re.S)
# Pattern 3: GRE annotation — card played (instanceId changes zone to battlefield/stack)
RE_PLAY_CARD     = re.compile(
    r'"type"\s*:\s*"AnnotationType_CardPlayed".*?"affectedIds"\s*:\s*\[.*?\].*?"grpId"\s*:\s*(\d+)',
    re.S
)
# Pattern 4: opponent seat detection
RE_SEAT          = re.compile(r'"systemSeatIds"\s*:\s*\[(\d+)\].*?"playerName"\s*:\s*"([^"]+)"', re.S)

# Simpler per-line patterns for streaming parse
RE_CARD_PLAYED   = re.compile(r'cardName.*?:\s*["\']?([A-Z][A-Za-z\s\',\-]+?)["\']?\s*[,}\]]')
RE_OPP_PLAYED    = re.compile(r'greToClientEvent.*?cardName.*?"([^"]+)"')

# ── Most reliable pattern: GRE log lines with card names ─────────────────────
# Each time a card hits the stack or battlefield the log writes a block like:
#   "cardName": "Goblin Guide",
#   "ownerSeatId": 2,           ← 2 = opponent (you are seat 1)
RE_BLOCK = re.compile(
    r'"cardName"\s*:\s*"([^"]+)"[^}]*?"ownerSeatId"\s*:\s*(\d+)',
    re.S
)
RE_BLOCK_REV = re.compile(
    r'"ownerSeatId"\s*:\s*(\d+)[^}]*?"cardName"\s*:\s*"([^"]+)"',
    re.S
)

SKIP_NAMES = {
    "", "Unknown", "None", "Token", "Emblem",
    # basic lands
    "Plains", "Island", "Swamp", "Mountain", "Forest",
    "Snow-Covered Plains", "Snow-Covered Island", "Snow-Covered Swamp",
    "Snow-Covered Mountain", "Snow-Covered Forest",
}

def is_valid_card(name: str) -> bool:
    name = name.strip()
    if name in SKIP_NAMES:
        return False
    if len(name) < 3 or len(name) > 60:
        return False
    if name[0].islower():
        return False
    return True

def add_opponent_card(name: str):
    name = name.strip()
    if not is_valid_card(name):
        return
    with state_lock:
        if name not in state["cards"]:
            state["cards"].append(name)
            state["last_update"] = time.time()
            print(f"  [+] Opponent played: {name}")

def parse_chunk(chunk: str):
    """Parse a chunk of log text for opponent card plays (seat 2)."""
    # Primary: cardName before ownerSeatId
    for m in RE_BLOCK.finditer(chunk):
        card, seat = m.group(1), m.group(2)
        if seat == "2":
            add_opponent_card(card)
    # Secondary: ownerSeatId before cardName
    for m in RE_BLOCK_REV.finditer(chunk):
        seat, card = m.group(1), m.group(2)
        if seat == "2":
            add_opponent_card(card)

def watch_log():
    """Tail Arena's Player.log and parse new lines as they arrive."""
    print(f"\nWatching: {LOG_PATH}")
    if not LOG_PATH.exists():
        print(f"\n[ERROR] Log file not found at:\n  {LOG_PATH}")
        print("\nTry these alternate locations:")
        for alt in [
            Path.home() / "AppData/LocalLow/Wizards of the Coast/MTGA/Player.log",
            Path("C:/Program Files/Wizards of the Coast/MTGA/Player.log"),
        ]:
            exists = "✓ EXISTS" if alt.exists() else "✗ not found"
            print(f"  {alt}  {exists}")
        print("\nUpdate LOG_PATH at the top of watcher.py and restart.")
        return

    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)  # seek to end — only watch new content
        print("Ready — waiting for opponent cards...\n")
        buf = ""
        while True:
            new = f.read(8192)
            if new:
                buf += new
                # Process in 4KB chunks with overlap to avoid split matches
                while len(buf) > 2048:
                    parse_chunk(buf[:4096])
                    buf = buf[2048:]
            else:
                if buf:
                    parse_chunk(buf)
                    buf = ""
                time.sleep(0.5)

# ── HTTP server ────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default request logging

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/cards":
            with state_lock:
                body = json.dumps({
                    "cards": state["cards"],
                    "last_update": state["last_update"],
                }).encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/reset":
            with state_lock:
                state["cards"] = []
                state["last_update"] = time.time()
            self.send_response(200)
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            print("\n[Game reset — card list cleared]\n")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        self.do_GET()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

def run_server(port=5000):
    server = HTTPServer(("localhost", port), Handler)
    print(f"API running at http://localhost:{port}")
    print(f"  GET /cards  → opponent cards seen")
    print(f"  GET /reset  → clear for new game\n")
    server.serve_forever()

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  MTG Arena Opponent Watcher")
    print("=" * 50)

    # Start log watcher in background thread
    t = threading.Thread(target=watch_log, daemon=True)
    t.start()

    # Run HTTP server on main thread
    try:
        run_server()
    except KeyboardInterrupt:
        print("\nStopped.")
