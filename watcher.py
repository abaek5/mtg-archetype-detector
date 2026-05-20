"""
MTG Arena Watcher — reads Player.log in real time
Resolves grpId -> card name via Scryfall bulk data
Serves full game state at localhost:5000
"""
import json, os, re, sys, time, threading, urllib.request, gzip
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

LOG_PATH = Path(os.path.expandvars(
    r"%APPDATA%\..\LocalLow\Wizards of the Coast\MTGA\Player.log"
))
SCRYFALL_BULK = Path(os.path.expandvars(r"%TEMP%\mtga_cards.json"))
SCRYFALL_URL  = "https://data.scryfall.io/oracle-cards/oracle-cards-20250101100208.json"

# ── Shared state ───────────────────────────────────────────────────────────────
state = {
    "opponent_cards": [],       # card names seen played by opponent
    "my_hand": [],              # my current hand (card names)
    "my_battlefield": [],       # my creatures/permanents [{name,power,toughness,tapped}]
    "opp_battlefield": [],      # opponent creatures/permanents
    "phase": "",                # current phase
    "turn": 0,
    "my_life": 20,
    "opp_life": 20,
    "last_update": 0,
    "grp_map": {},              # grpId (int) -> card name (str)
    "instance_map": {},         # instanceId (int) -> {grpId, owner, zone, ...}
}
lock = threading.Lock()

SKIP_NAMES = {
    "Plains","Island","Swamp","Mountain","Forest",
    "Snow-Covered Plains","Snow-Covered Island","Snow-Covered Swamp",
    "Snow-Covered Mountain","Snow-Covered Forest",
    "Wastes","Token",
}

PHASE_LABELS = {
    "Phase_Beginning": "Beginning",
    "Phase_Main1": "Main Phase 1",
    "Phase_Combat": "Combat",
    "Phase_Main2": "Main Phase 2",
    "Phase_Ending": "End Step",
}

STEP_LABELS = {
    "Step_Upkeep": "Upkeep",
    "Step_Draw": "Draw",
    "Step_BeginCombat": "Begin Combat",
    "Step_DeclareAttack": "Declare Attackers",
    "Step_DeclareBlock": "Declare Blockers",
    "Step_CombatDamage": "Combat Damage",
    "Step_EndCombat": "End of Combat",
    "Step_End": "End Step",
    "Step_Cleanup": "Cleanup",
}

# ── Load Scryfall card data ────────────────────────────────────────────────────
def load_grp_map():
    """Build grpId->name map from MTGA card data embedded in Scryfall bulk."""
    # Scryfall oracle cards include arena_id which matches grpId
    print("Loading card database from Scryfall...")
    if not SCRYFALL_BULK.exists():
        print("Downloading Scryfall bulk data (~50MB, once only)...")
        try:
            # Try to find the latest bulk data URL
            meta = urllib.request.urlopen(
                "https://api.scryfall.com/bulk-data", timeout=10
            ).read()
            meta_json = json.loads(meta)
            url = next(
                (d["download_uri"] for d in meta_json["data"]
                 if d["type"] == "oracle_cards"), SCRYFALL_URL
            )
            urllib.request.urlretrieve(url, SCRYFALL_BULK)
            print(f"Saved to {SCRYFALL_BULK}")
        except Exception as e:
            print(f"Download failed: {e}")
            print("Will resolve card names as they appear in the log instead.")
            return {}

    grp = {}
    try:
        with open(SCRYFALL_BULK, encoding="utf-8") as f:
            cards = json.load(f)
        for c in cards:
            aid = c.get("arena_id")
            if aid:
                grp[aid] = c["name"]
        print(f"Loaded {len(grp):,} cards from Scryfall.")
    except Exception as e:
        print(f"Failed to parse card data: {e}")
    return grp

def resolve(grp_id: int) -> str | None:
    with lock:
        return state["grp_map"].get(grp_id)

# ── Parse game state messages ──────────────────────────────────────────────────
def parse_game_state(msg: dict):
    gm = msg.get("gameStateMessage", {})
    if not gm:
        return

    with lock:
        # Life totals
        for p in gm.get("players", []):
            seat = p.get("systemSeatNumber")
            life = p.get("lifeTotal")
            if seat == 1 and life is not None:
                state["my_life"] = life
            elif seat == 2 and life is not None:
                state["opp_life"] = life

        # Phase / step
        ti = gm.get("turnInfo", {})
        if ti:
            phase = ti.get("phase", "")
            step  = ti.get("step", "")
            turn  = ti.get("turnNumber", state["turn"])
            label = PHASE_LABELS.get(phase, phase)
            if step:
                label += f" — {STEP_LABELS.get(step, step)}"
            state["phase"] = label
            state["turn"]  = turn

        # Update instance map with new game objects
        for obj in gm.get("gameObjects", []):
            iid   = obj.get("instanceId")
            grpid = obj.get("grpId")
            owner = obj.get("ownerSeatId")
            zone  = obj.get("zoneId")
            tapped = obj.get("isTapped", False)
            power  = obj.get("power",  {}).get("value")
            tough  = obj.get("toughness", {}).get("value")
            ctype  = obj.get("type", "")
            ctypes = obj.get("cardTypes", [])
            token  = (ctype == "GameObjectType_Token")

            if iid is None or grpid is None:
                continue

            name = state["grp_map"].get(grpid)

            state["instance_map"][iid] = {
                "grpId": grpid,
                "name": name,
                "owner": owner,
                "zone": zone,
                "tapped": tapped,
                "power": power,
                "toughness": tough,
                "cardTypes": ctypes,
                "token": token,
            }

        # Update zones — rebuild battlefield / hand from instance map
        zones_data = {z["zoneId"]: z for z in gm.get("zones", [])}

        # Zone IDs we care about (we track by instanceId membership):
        # We rebuild full battlefield/hand on each GameStateMessage that
        # includes zone info, since diffs accumulate.
        rebuild_bf  = False
        rebuild_hand = False

        for z in gm.get("zones", []):
            ztype = z.get("type", "")
            owner = z.get("ownerSeatId")
            iids  = z.get("objectInstanceIds", [])

            if ztype == "ZoneType_Battlefield":
                rebuild_bf = True
                # Track which instances are on battlefield
                for iid in iids:
                    if iid in state["instance_map"]:
                        state["instance_map"][iid]["zone_type"] = "Battlefield"

            elif ztype == "ZoneType_Hand" and owner == 1:
                rebuild_hand = True
                for iid in iids:
                    if iid in state["instance_map"]:
                        state["instance_map"][iid]["zone_type"] = "Hand"

            elif ztype == "ZoneType_Stack":
                # Cards cast by opponent going on stack — capture as opponent card
                for iid in iids:
                    info = state["instance_map"].get(iid, {})
                    if info.get("owner") == 2 and info.get("name"):
                        name = info["name"]
                        if (name not in SKIP_NAMES
                                and name not in state["opponent_cards"]
                                and "Token" not in info.get("cardTypes", [])):
                            state["opponent_cards"].append(name)
                            state["last_update"] = time.time()
                            print(f"  [STACK] Opponent: {name}")

        # Check annotations for ZoneTransfer CastSpell by opponent
        for ann in gm.get("annotations", []):
            ann_types = ann.get("type", [])
            if "AnnotationType_ZoneTransfer" not in ann_types:
                continue
            details = {d["key"]: d for d in ann.get("details", [])}
            category = details.get("category", {}).get("valueString", [""])[0]
            if category not in ("CastSpell", "Resolve"):
                continue
            for iid in ann.get("affectedIds", []):
                info = state["instance_map"].get(iid, {})
                if info.get("owner") != 2:
                    continue
                grpid = info.get("grpId")
                # Try name from instance map first, then re-resolve from grp_map
                name = info.get("name") or state["grp_map"].get(grpid)
                ctypes = info.get("cardTypes", [])
                token  = info.get("token", False)

                print(f"  [DBG ] ZoneTransfer cat={category} iid={iid} owner={info.get('owner')} grp={grpid} name={name} types={ctypes}")

                if not name:
                    print(f"  [WARN] grpId {grpid} not in card database — add manually if needed")
                    continue
                if name in SKIP_NAMES:
                    continue
                if "CardType_Land" in ctypes:
                    continue
                if token:
                    continue
                if name not in state["opponent_cards"]:
                    state["opponent_cards"].append(name)
                    state["last_update"] = time.time()
                    print(f"  [CAST ] Opponent: {name}  (grp={grpid})")

        state["last_update"] = time.time()

def rebuild_visible_state():
    """Rebuild my_hand, my_battlefield, opp_battlefield from instance_map."""
    with lock:
        my_hand, my_bf, opp_bf = [], [], []
        for iid, info in state["instance_map"].items():
            zt = info.get("zone_type")
            name = info.get("name", "Unknown")
            owner = info.get("owner")
            if not name or info.get("token"):
                continue
            if zt == "Hand" and owner == 1:
                my_hand.append(name)
            elif zt == "Battlefield":
                entry = {
                    "name": name,
                    "power": info.get("power"),
                    "toughness": info.get("toughness"),
                    "tapped": info.get("tapped", False),
                    "types": info.get("cardTypes", []),
                }
                if owner == 1:
                    my_bf.append(entry)
                elif owner == 2:
                    opp_bf.append(entry)
        state["my_hand"] = my_hand
        state["my_battlefield"] = my_bf
        state["opp_battlefield"] = opp_bf

# ── Log parsing ────────────────────────────────────────────────────────────────
def parse_chunk(text: str):
    """Find and parse all GreToClientEvent JSON blobs in a chunk."""
    # Each event is a single-line JSON object after the log header
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            blob = json.loads(line)
        except Exception:
            continue
        event = blob.get("greToClientEvent", {})
        for msg in event.get("greToClientMessages", []):
            if msg.get("type") == "GREMessageType_GameStateMessage":
                parse_game_state(msg)
    rebuild_visible_state()

def watch_log():
    print(f"\nWatching: {LOG_PATH}")
    if not LOG_PATH.exists():
        print(f"\n[ERROR] Log not found: {LOG_PATH}")
        print("Enable Detailed Logs in Arena Settings and restart Arena.")
        return

    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)
        print("Ready — watching for game events...\n")
        buf = ""
        while True:
            chunk = f.read(65536)
            if chunk:
                buf += chunk
                lines = buf.split("\n")
                buf = lines[-1]
                parse_chunk("\n".join(lines[:-1]))
            else:
                if buf.strip():
                    parse_chunk(buf)
                    buf = ""
                time.sleep(0.4)

# ── HTTP server ────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path == "/state":
            with lock:
                body = json.dumps({
                    "opponent_cards":  state["opponent_cards"],
                    "my_hand":         state["my_hand"],
                    "my_battlefield":  state["my_battlefield"],
                    "opp_battlefield": state["opp_battlefield"],
                    "phase":           state["phase"],
                    "turn":            state["turn"],
                    "my_life":         state["my_life"],
                    "opp_life":        state["opp_life"],
                    "last_update":     state["last_update"],
                }).encode()
            self._respond(body)

        elif self.path == "/cards":
            with lock:
                body = json.dumps({
                    "cards": state["opponent_cards"],
                    "last_update": state["last_update"],
                }).encode()
            self._respond(body)

        elif self.path == "/reset":
            with lock:
                state["opponent_cards"]  = []
                state["my_hand"]         = []
                state["my_battlefield"]  = []
                state["opp_battlefield"] = []
                state["instance_map"]    = {}
                state["phase"]           = ""
                state["turn"]            = 0
                state["my_life"]         = 20
                state["opp_life"]        = 20
                state["last_update"]     = time.time()
            print("\n[Reset — new game]\n")
            self._respond(b'{"ok":true}')
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self): self.do_GET()

    def _respond(self, body):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

def run_server(port=5000):
    s = HTTPServer(("localhost", port), Handler)
    print(f"API:  http://localhost:{port}/state  — full game state")
    print(f"      http://localhost:{port}/cards  — opponent cards only")
    print(f"      http://localhost:{port}/reset  — new game\n")
    s.serve_forever()

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 52)
    print("  MTG Arena Watcher  —  Full Game State Edition")
    print("=" * 52)

    grp = load_grp_map()
    with lock:
        state["grp_map"] = grp

    t = threading.Thread(target=watch_log, daemon=True)
    t.start()

    try:
        run_server()
    except KeyboardInterrupt:
        print("\nStopped.")
