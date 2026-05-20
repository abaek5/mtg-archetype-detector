"""
MTG Arena Watcher — reads Player.log in real time
Resolves grpId -> card name via Scryfall API (per-card fallback)
Serves full game state at localhost:5000
"""
import json, os, re, sys, time, threading, urllib.request
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

LOG_PATH = Path(os.path.expandvars(
    r"%APPDATA%\..\LocalLow\Wizards of the Coast\MTGA\Player.log"
))
SCRYFALL_BULK = Path(os.path.expandvars(r"%TEMP%\mtga_cards.json"))

# ── Shared state ───────────────────────────────────────────────────────────────
state = {
    "opponent_cards": [],
    "my_hand": [],
    "my_battlefield": [],
    "opp_battlefield": [],
    "phase": "",
    "turn": 0,
    "my_life": 20,
    "opp_life": 20,
    "last_update": 0,
    "grp_map": {},        # grpId (int) -> card name (str)
    "instance_map": {},   # instanceId -> {grpId, owner, zone_type, ...}
}
lock = threading.Lock()

SKIP_NAMES = {
    "Plains","Island","Swamp","Mountain","Forest",
    "Snow-Covered Plains","Snow-Covered Island","Snow-Covered Swamp",
    "Snow-Covered Mountain","Snow-Covered Forest","Wastes",
}

PHASE_LABELS = {
    "Phase_Beginning":"Beginning","Phase_Main1":"Main Phase 1",
    "Phase_Combat":"Combat","Phase_Main2":"Main Phase 2","Phase_Ending":"End Step",
}
STEP_LABELS = {
    "Step_Upkeep":"Upkeep","Step_Draw":"Draw","Step_BeginCombat":"Begin Combat",
    "Step_DeclareAttack":"Declare Attackers","Step_DeclareBlock":"Declare Blockers",
    "Step_CombatDamage":"Combat Damage","Step_EndCombat":"End of Combat",
    "Step_End":"End Step","Step_Cleanup":"Cleanup",
}

# ── Scryfall card resolution ───────────────────────────────────────────────────
_looked_up = set()   # grpIds already attempted via API
_lookup_lock = threading.Lock()

def load_bulk():
    """Try to load bulk Scryfall data (arena_id -> name). Optional speedup."""
    if SCRYFALL_BULK.exists():
        try:
            with open(SCRYFALL_BULK, encoding="utf-8") as f:
                cards = json.load(f)
            grp = {}
            for c in cards:
                aid = c.get("arena_id")
                if aid:
                    grp[int(aid)] = c["name"]
            print(f"Loaded {len(grp):,} cards from cached Scryfall data.")
            return grp
        except Exception as e:
            print(f"Could not load cached data: {e}")

    print("No local card cache — will look up cards via Scryfall API as they appear.")
    print("(Run once with internet to cache all cards for faster future use)\n")

    # Try to download in background
    def _download():
        try:
            meta = urllib.request.urlopen("https://api.scryfall.com/bulk-data", timeout=10).read()
            url = next(
                (d["download_uri"] for d in json.loads(meta)["data"] if d["type"] == "default_cards"),
                None
            )
            if url:
                print(f"Downloading card database ({url[:60]}...)...")
                urllib.request.urlretrieve(url, SCRYFALL_BULK)
                print(f"Card database saved — restart watcher for full offline resolution.")
        except Exception as e:
            print(f"Background download failed: {e}")
    threading.Thread(target=_download, daemon=True).start()
    return {}

def lookup_grp(grp_id: int):
    """Look up a single grpId via Scryfall /cards/arena/:id in a background thread."""
    with _lookup_lock:
        if grp_id in _looked_up:
            return
        _looked_up.add(grp_id)

    def _fetch():
        try:
            url = f"https://api.scryfall.com/cards/arena/{grp_id}"
            data = urllib.request.urlopen(url, timeout=6).read()
            card = json.loads(data)
            name = card.get("name")
            if not name:
                return
            print(f"  [RESOLVED] grp={grp_id} -> {name}")
            with lock:
                state["grp_map"][grp_id] = name
                # Retry any instance that was waiting on this grpId
                for iid, info in state["instance_map"].items():
                    if info.get("grpId") == grp_id and not info.get("name"):
                        info["name"] = name
                    if (info.get("grpId") == grp_id
                            and info.get("owner") == 2
                            and info.get("pending_add")):
                        info["pending_add"] = False
                        ctypes = info.get("cardTypes", [])
                        token  = info.get("token", False)
                        if ("CardType_Land" not in ctypes
                                and not token
                                and name not in SKIP_NAMES
                                and name not in state["opponent_cards"]):
                            state["opponent_cards"].append(name)
                            state["last_update"] = time.time()
                            print(f"  [CAST ] Opponent: {name}")
        except Exception as e:
            pass  # card might not exist in Arena
    threading.Thread(target=_fetch, daemon=True).start()

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

        # Build/update instance map from game objects
        for obj in gm.get("gameObjects", []):
            iid    = obj.get("instanceId")
            grpid  = obj.get("grpId")
            owner  = obj.get("ownerSeatId")
            zone   = obj.get("zoneId")
            tapped = obj.get("isTapped", False)
            power  = obj.get("power",  {}).get("value")
            tough  = obj.get("toughness", {}).get("value")
            ctypes = obj.get("cardTypes", [])
            otype  = obj.get("type", "")
            token  = (otype == "GameObjectType_Token")

            if iid is None or grpid is None:
                continue

            name = state["grp_map"].get(grpid)
            existing = state["instance_map"].get(iid, {})
            state["instance_map"][iid] = {
                "grpId":     grpid,
                "name":      name,
                "owner":     owner,
                "zoneId":    zone,
                "zone_type": existing.get("zone_type", ""),
                "tapped":    tapped,
                "power":     power,
                "toughness": tough,
                "cardTypes": ctypes,
                "token":     token,
                "pending_add": existing.get("pending_add", False),
            }

        # Zone tracking
        for z in gm.get("zones", []):
            ztype = z.get("type", "")
            owner = z.get("ownerSeatId")
            iids  = z.get("objectInstanceIds", [])

            if ztype == "ZoneType_Battlefield":
                for iid in iids:
                    if iid in state["instance_map"]:
                        state["instance_map"][iid]["zone_type"] = "Battlefield"

            elif ztype == "ZoneType_Hand" and owner == 1:
                for iid in iids:
                    if iid in state["instance_map"]:
                        state["instance_map"][iid]["zone_type"] = "Hand"

        # Annotation: ZoneTransfer CastSpell = opponent played a card
        for ann in gm.get("annotations", []):
            if "AnnotationType_ZoneTransfer" not in ann.get("type", []):
                continue
            details  = {d["key"]: d for d in ann.get("details", [])}
            category = details.get("category", {}).get("valueString", [""])[0]
            if category != "CastSpell":
                continue

            for iid in ann.get("affectedIds", []):
                info  = state["instance_map"].get(iid, {})
                if info.get("owner") != 2:
                    continue
                grpid  = info.get("grpId")
                name   = info.get("name") or state["grp_map"].get(grpid)
                ctypes = info.get("cardTypes", [])
                token  = info.get("token", False)

                if "CardType_Land" in ctypes or token:
                    continue

                if name and name not in SKIP_NAMES:
                    if name not in state["opponent_cards"]:
                        state["opponent_cards"].append(name)
                        state["last_update"] = time.time()
                        print(f"  [CAST ] Opponent: {name}")
                elif grpid:
                    # Name not resolved yet — mark pending and look up async
                    print(f"  [QUEUE] grp={grpid} not resolved yet, looking up...")
                    info["pending_add"] = True
                    state["instance_map"][iid] = info

        state["last_update"] = time.time()

    # Trigger async lookups for any unresolved grpIds seen this message
    for obj in gm.get("gameObjects", []):
        grpid = obj.get("grpId")
        owner = obj.get("ownerSeatId")
        if grpid and owner == 2:
            with lock:
                if grpid not in state["grp_map"]:
                    lookup_grp(grpid)

def rebuild_visible_state():
    with lock:
        my_hand, my_bf, opp_bf = [], [], []
        for iid, info in state["instance_map"].items():
            zt    = info.get("zone_type", "")
            name  = info.get("name")
            owner = info.get("owner")
            if not name or info.get("token"):
                continue
            if zt == "Hand" and owner == 1:
                my_hand.append(name)
            elif zt == "Battlefield":
                entry = {
                    "name":      name,
                    "power":     info.get("power"),
                    "toughness": info.get("toughness"),
                    "tapped":    info.get("tapped", False),
                    "types":     info.get("cardTypes", []),
                }
                if owner == 1:
                    my_bf.append(entry)
                elif owner == 2:
                    opp_bf.append(entry)
        state["my_hand"]         = my_hand
        state["my_battlefield"]  = my_bf
        state["opp_battlefield"] = opp_bf

# ── Log watcher ────────────────────────────────────────────────────────────────
def parse_chunk(text: str):
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            blob = json.loads(line)
        except Exception:
            continue
        for msg in blob.get("greToClientEvent", {}).get("greToClientMessages", []):
            if msg.get("type") == "GREMessageType_GameStateMessage":
                parse_game_state(msg)
    rebuild_visible_state()

def watch_log():
    print(f"Watching: {LOG_PATH}")
    if not LOG_PATH.exists():
        print(f"\n[ERROR] Log not found. Enable Detailed Logs in Arena Settings.\n")
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

# ── HTTP API ───────────────────────────────────────────────────────────────────
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
                body = json.dumps({"cards": state["opponent_cards"], "last_update": state["last_update"]}).encode()
            self._respond(body)
        elif self.path == "/reset":
            with lock:
                state.update({"opponent_cards":[],"my_hand":[],"my_battlefield":[],
                    "opp_battlefield":[],"instance_map":{},"phase":"","turn":0,
                    "my_life":20,"opp_life":20,"last_update":time.time()})
            print("\n[Reset — new game]\n")
            self._respond(b'{"ok":true}')
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self): self.do_GET()

    def _respond(self, body):
        self.send_response(200); self._cors()
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers(); self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 52)
    print("  MTG Arena Watcher  —  Full Game State Edition")
    print("=" * 52 + "\n")

    grp = load_bulk()
    with lock:
        state["grp_map"] = grp

    threading.Thread(target=watch_log, daemon=True).start()

    try:
        print(f"API:  http://localhost:5000/state")
        print(f"      http://localhost:5000/reset\n")
        HTTPServer(("localhost", 5000), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
