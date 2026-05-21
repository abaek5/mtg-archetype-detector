"""
MTG Arena Watcher — reads Player.log in real time
Resolves grpId -> card name via Scryfall API (per-card fallback)
Serves full game state at localhost:5000
"""
import json, os, re, sys, time, threading, urllib.request
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

FIREBASE_URL = "https://mtg-detector-40285-default-rtdb.firebaseio.com"

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
    "my_seat": None,      # detected from log (1 or 2)
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
                opener = urllib.request.build_opener()
            opener.addheaders = [("User-Agent", "MTGArchetypeDetector/1.0")]
            with opener.open(url, timeout=30) as r:
                with open(SCRYFALL_BULK, "wb") as f:
                    f.write(r.read())
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
            req = urllib.request.Request(url, headers={"User-Agent": "MTGArchetypeDetector/1.0", "Accept": "application/json"})
            data = urllib.request.urlopen(req, timeout=6).read()
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
            print(f"  [ERR  ] Scryfall lookup grp={grp_id}: {e}")
    threading.Thread(target=_fetch, daemon=True).start()

# ── Parse game state messages ──────────────────────────────────────────────────
def detect_my_seat(text: str):
    """Extract our seat number from ClientToGREUIMessage lines."""
    import re
    m = re.search(r'"systemSeatId"\s*:\s*(\d+)', text)
    if m:
        seat = int(m.group(1))
        with lock:
            if state["my_seat"] != seat:
                state["my_seat"] = seat
                print(f"  [SEAT ] You are seat {seat}, opponent is seat {3-seat}")

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
                my_seat = state.get("my_seat") or 1
                opp_seat = 3 - my_seat
                if info.get("owner") != opp_seat:
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
        my_seat = state.get("my_seat") or 1
        opp_seat = 3 - my_seat
        if grpid and owner == opp_seat:
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

# ── Firebase sync ─────────────────────────────────────────────────────────────
def push_to_firebase():
    """Push current state to Firebase Realtime Database."""
    try:
        with lock:
            payload = json.dumps({
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
        req = urllib.request.Request(
            f"{FIREBASE_URL}/state.json",
            data=payload,
            method="PUT",
            headers={"Content-Type": "application/json", "User-Agent": "MTGArchetypeDetector/1.0"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"  [WARN] Firebase sync failed: {e}")

def push_loop():
    """Push to Firebase every 2 seconds."""
    while True:
        push_to_firebase()
        time.sleep(2)

# ── Log watcher ────────────────────────────────────────────────────────────────
def parse_chunk(text: str):
    detect_my_seat(text)
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
_HTML_B64 = "PCFET0NUWVBFIGh0bWw+CjxodG1sIGxhbmc9ImVuIj4KPGhlYWQ+CjxtZXRhIGNoYXJzZXQ9IlVURi04Ij4KPG1ldGEgbmFtZT0idmlld3BvcnQiIGNvbnRlbnQ9IndpZHRoPWRldmljZS13aWR0aCwgaW5pdGlhbC1zY2FsZT0xLjAiPgo8dGl0bGU+TVRHIEFyY2hldHlwZSBEZXRlY3RvcjwvdGl0bGU+CjxsaW5rIHJlbD0ic3R5bGVzaGVldCIgaHJlZj0iaHR0cHM6Ly9jZG4uanNkZWxpdnIubmV0L25wbS9AdGFibGVyL2ljb25zLXdlYmZvbnRAMy4xOS4wL2Rpc3QvdGFibGVyLWljb25zLm1pbi5jc3MiPgo8c3R5bGU+CiosICo6OmJlZm9yZSwgKjo6YWZ0ZXIgeyBib3gtc2l6aW5nOiBib3JkZXItYm94OyB9Cgo6cm9vdCB7CiAgLS1iZzogI2ZmZmZmZjsKICAtLWJnLXNlY29uZGFyeTogI2Y1ZjVmMjsKICAtLWJnLXRlcnRpYXJ5OiAjZWVlZGU4OwogIC0tdGV4dDogIzFhMWExYTsKICAtLXRleHQtc2Vjb25kYXJ5OiAjNmI2YjY4OwogIC0tYm9yZGVyOiByZ2JhKDAsMCwwLDAuMTIpOwogIC0tYm9yZGVyLXNlY29uZGFyeTogcmdiYSgwLDAsMCwwLjIyKTsKICAtLXJhZGl1cy1tZDogOHB4OwogIC0tcmFkaXVzLWxnOiAxMnB4OwogIC0tYWNjZW50OiAjMUQ5RTc1OwogIC0tYWNjZW50LWFpOiAjN0Y3N0REOwogIC0tYWNjZW50LWFkdmljZTogIzM3OEFERDsKfQoKQG1lZGlhIChwcmVmZXJzLWNvbG9yLXNjaGVtZTogZGFyaykgewogIDpyb290IHsKICAgIC0tYmc6ICMxYzFjMWE7CiAgICAtLWJnLXNlY29uZGFyeTogIzI1MjUyMjsKICAgIC0tYmctdGVydGlhcnk6ICMyZTJlMmI7CiAgICAtLXRleHQ6ICNmMGVmZTg7CiAgICAtLXRleHQtc2Vjb25kYXJ5OiAjOWE5YTk1OwogICAgLS1ib3JkZXI6IHJnYmEoMjU1LDI1NSwyNTUsMC4xKTsKICAgIC0tYm9yZGVyLXNlY29uZGFyeTogcmdiYSgyNTUsMjU1LDI1NSwwLjIpOwogIH0KfQoKYm9keSB7CiAgbWFyZ2luOiAwOwogIGJhY2tncm91bmQ6IHZhcigtLWJnLXRlcnRpYXJ5KTsKICBjb2xvcjogdmFyKC0tdGV4dCk7CiAgZm9udC1mYW1pbHk6IC1hcHBsZS1zeXN0ZW0sIEJsaW5rTWFjU3lzdGVtRm9udCwgJ1NlZ29lIFVJJywgc2Fucy1zZXJpZjsKICBmb250LXNpemU6IDE0cHg7CiAgbGluZS1oZWlnaHQ6IDEuNTsKICBtaW4taGVpZ2h0OiAxMDB2aDsKfQoKLnRvcGJhciB7CiAgYmFja2dyb3VuZDogdmFyKC0tYmcpOwogIGJvcmRlci1ib3R0b206IDAuNXB4IHNvbGlkIHZhcigtLWJvcmRlcik7CiAgcGFkZGluZzogMTBweCAyMHB4OwogIGRpc3BsYXk6IGZsZXg7CiAgYWxpZ24taXRlbXM6IGNlbnRlcjsKICBqdXN0aWZ5LWNvbnRlbnQ6IHNwYWNlLWJldHdlZW47CiAgcG9zaXRpb246IHN0aWNreTsKICB0b3A6IDA7CiAgei1pbmRleDogMTA7Cn0KCi50b3BiYXItdGl0bGUgewogIGZvbnQtc2l6ZTogMTVweDsgZm9udC13ZWlnaHQ6IDUwMDsgY29sb3I6IHZhcigtLXRleHQpOwogIGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogOHB4Owp9CgoudG9wYmFyLXRpdGxlIC5kb3QgeyB3aWR0aDogOHB4OyBoZWlnaHQ6IDhweDsgYm9yZGVyLXJhZGl1czogNTAlOyBiYWNrZ3JvdW5kOiB2YXIoLS1hY2NlbnQpOyB9Ci50b3BiYXItc3ViIHsgZm9udC1zaXplOiAxMnB4OyBjb2xvcjogdmFyKC0tdGV4dC1zZWNvbmRhcnkpOyB9CgovKiDilIDilIAgbGF5b3V0IOKUgOKUgCAqLwouYXBwIHsgZGlzcGxheTogZ3JpZDsgZ3JpZC10ZW1wbGF0ZS1jb2x1bW5zOiAzMDBweCAxZnI7IG1pbi1oZWlnaHQ6IGNhbGMoMTAwdmggLSA0NXB4KTsgfQoKLnNpZGViYXIgewogIGJhY2tncm91bmQ6IHZhcigtLWJnKTsKICBib3JkZXItcmlnaHQ6IDAuNXB4IHNvbGlkIHZhcigtLWJvcmRlcik7CiAgcGFkZGluZzogMTZweDsKICBkaXNwbGF5OiBmbGV4OwogIGZsZXgtZGlyZWN0aW9uOiBjb2x1bW47CiAgZ2FwOiAxNHB4OwogIG92ZXJmbG93LXk6IGF1dG87CiAgcG9zaXRpb246IHN0aWNreTsKICB0b3A6IDQ1cHg7CiAgaGVpZ2h0OiBjYWxjKDEwMHZoIC0gNDVweCk7Cn0KCi5mZWVkIHsKICBwYWRkaW5nOiAxNnB4OwogIG92ZXJmbG93LXk6IGF1dG87CiAgZGlzcGxheTogZmxleDsKICBmbGV4LWRpcmVjdGlvbjogY29sdW1uOwogIGdhcDogMTJweDsKfQoKQG1lZGlhIChtYXgtd2lkdGg6IDY1MHB4KSB7CiAgLmFwcCB7IGdyaWQtdGVtcGxhdGUtY29sdW1uczogMWZyOyB9CiAgLnNpZGViYXIgeyBwb3NpdGlvbjogc3RhdGljOyBoZWlnaHQ6IGF1dG87IGJvcmRlci1yaWdodDogbm9uZTsgYm9yZGVyLWJvdHRvbTogMC41cHggc29saWQgdmFyKC0tYm9yZGVyKTsgfQogIC5mZWVkIHsgcGFkZGluZzogMTJweDsgfQp9CgovKiDilIDilIAgc2hhcmVkIGNvbXBvbmVudHMg4pSA4pSAICovCi5zZWN0aW9uLWxhYmVsIHsKICBmb250LXNpemU6IDExcHg7IGZvbnQtd2VpZ2h0OiA1MDA7IGNvbG9yOiB2YXIoLS10ZXh0LXNlY29uZGFyeSk7CiAgdGV4dC10cmFuc2Zvcm06IHVwcGVyY2FzZTsgbGV0dGVyLXNwYWNpbmc6IDAuMDZlbTsgbWFyZ2luLWJvdHRvbTogNXB4Owp9CgoucGFuZWwgeyBiYWNrZ3JvdW5kOiB2YXIoLS1iZy1zZWNvbmRhcnkpOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMtbGcpOyBwYWRkaW5nOiAxMnB4IDE0cHg7IH0KLnBhbmVsLXRpdGxlIHsgZm9udC1zaXplOiAxM3B4OyBmb250LXdlaWdodDogNTAwOyBjb2xvcjogdmFyKC0tdGV4dCk7IG1hcmdpbjogMCAwIDhweDsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA2cHg7IH0KLnBhbmVsLXRpdGxlIGkgeyBmb250LXNpemU6IDE1cHg7IGNvbG9yOiB2YXIoLS10ZXh0LXNlY29uZGFyeSk7IH0KCnRleHRhcmVhIHsKICB3aWR0aDogMTAwJTsgaGVpZ2h0OiAxMjBweDsgcmVzaXplOiB2ZXJ0aWNhbDsKICBmb250LXNpemU6IDExcHg7IGZvbnQtZmFtaWx5OiAnU0YgTW9ubycsICdDb25zb2xhcycsIG1vbm9zcGFjZTsKICBiYWNrZ3JvdW5kOiB2YXIoLS1iZyk7IGNvbG9yOiB2YXIoLS10ZXh0KTsKICBib3JkZXI6IDAuNXB4IHNvbGlkIHZhcigtLWJvcmRlcik7IGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cy1tZCk7CiAgcGFkZGluZzogN3B4IDlweDsgb3V0bGluZTogbm9uZTsKfQp0ZXh0YXJlYTpmb2N1cyB7IGJvcmRlci1jb2xvcjogdmFyKC0tYm9yZGVyLXNlY29uZGFyeSk7IH0KCmlucHV0W3R5cGU9InRleHQiXSwgaW5wdXRbdHlwZT0icGFzc3dvcmQiXSB7CiAgYmFja2dyb3VuZDogdmFyKC0tYmcpOyBjb2xvcjogdmFyKC0tdGV4dCk7CiAgYm9yZGVyOiAwLjVweCBzb2xpZCB2YXIoLS1ib3JkZXIpOyBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMtbWQpOwogIHBhZGRpbmc6IDAgOXB4OyBoZWlnaHQ6IDMycHg7IGZvbnQtc2l6ZTogMTNweDsgb3V0bGluZTogbm9uZTsKICBmb250LWZhbWlseTogaW5oZXJpdDsgd2lkdGg6IDEwMCU7Cn0KaW5wdXRbdHlwZT0idGV4dCJdOmZvY3VzLCBpbnB1dFt0eXBlPSJwYXNzd29yZCJdOmZvY3VzIHsgYm9yZGVyLWNvbG9yOiB2YXIoLS1ib3JkZXItc2Vjb25kYXJ5KTsgfQoKYnV0dG9uIHsKICBiYWNrZ3JvdW5kOiB2YXIoLS1iZyk7IGNvbG9yOiB2YXIoLS10ZXh0KTsKICBib3JkZXI6IDAuNXB4IHNvbGlkIHZhcigtLWJvcmRlci1zZWNvbmRhcnkpOwogIGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cy1tZCk7CiAgcGFkZGluZzogNXB4IDEycHg7IGZvbnQtc2l6ZTogMTJweDsKICBjdXJzb3I6IHBvaW50ZXI7IGZvbnQtZmFtaWx5OiBpbmhlcml0OwogIHRyYW5zaXRpb246IGJhY2tncm91bmQgMC4xMnMsIHRyYW5zZm9ybSAwLjFzOwogIGRpc3BsYXk6IGlubGluZS1mbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBnYXA6IDVweDsKfQpidXR0b246aG92ZXIgeyBiYWNrZ3JvdW5kOiB2YXIoLS1iZy1zZWNvbmRhcnkpOyB9CmJ1dHRvbjphY3RpdmUgeyB0cmFuc2Zvcm06IHNjYWxlKDAuOTgpOyB9CgoucGFyc2UtYnRuIHsgd2lkdGg6IDEwMCU7IGp1c3RpZnktY29udGVudDogY2VudGVyOyBtYXJnaW4tdG9wOiA2cHg7IGZvbnQtc2l6ZTogMTJweDsgfQoKLmZvcm1hdC1yb3cgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LXdyYXA6IHdyYXA7IGdhcDogNXB4OyB9Ci5mbXQtYnRuIHsKICBmb250LXNpemU6IDExcHg7IHBhZGRpbmc6IDNweCAxMHB4OyBib3JkZXItcmFkaXVzOiA5OTlweDsKICBib3JkZXI6IDAuNXB4IHNvbGlkIHZhcigtLWJvcmRlci1zZWNvbmRhcnkpOwogIGJhY2tncm91bmQ6IHZhcigtLWJnKTsgY29sb3I6IHZhcigtLXRleHQtc2Vjb25kYXJ5KTsgY3Vyc29yOiBwb2ludGVyOwp9Ci5mbXQtYnRuLmFjdGl2ZSB7IGJhY2tncm91bmQ6IHZhcigtLXRleHQpOyBjb2xvcjogdmFyKC0tYmcpOyBib3JkZXItY29sb3I6IHZhcigtLXRleHQpOyB9CgovKiDilIDilIAgY2FyZCBpbnB1dCBhcmVhIOKUgOKUgCAqLwouY2FyZC1pbnB1dC1yb3cgeyBkaXNwbGF5OiBmbGV4OyBnYXA6IDZweDsgfQouY2FyZC1pbnB1dC1yb3cgaW5wdXQgeyBmbGV4OiAxOyB9CgoudGFnLWFyZWEgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LXdyYXA6IHdyYXA7IGdhcDogNHB4OyBtaW4taGVpZ2h0OiAyOHB4OyBtYXJnaW4tYm90dG9tOiA2cHg7IH0KLnRhZyB7CiAgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA0cHg7CiAgYmFja2dyb3VuZDogdmFyKC0tYmcpOyBib3JkZXI6IDAuNXB4IHNvbGlkIHZhcigtLWJvcmRlci1zZWNvbmRhcnkpOwogIGJvcmRlci1yYWRpdXM6IDk5OXB4OyBwYWRkaW5nOiAycHggNnB4IDJweCA5cHg7IGZvbnQtc2l6ZTogMTFweDsgY29sb3I6IHZhcigtLXRleHQpOwp9Ci50YWcgYnV0dG9uIHsgYmFja2dyb3VuZDogbm9uZTsgYm9yZGVyOiBub25lOyBwYWRkaW5nOiAwOyBjb2xvcjogdmFyKC0tdGV4dC1zZWNvbmRhcnkpOyBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyB9CgouZW1wdHktaGludCB7IGZvbnQtc2l6ZTogMTFweDsgY29sb3I6IHZhcigtLXRleHQtc2Vjb25kYXJ5KTsgYWxpZ24tc2VsZjogY2VudGVyOyB9Ci5jbGVhci1saW5rIHsgZm9udC1zaXplOiAxMXB4OyBjb2xvcjogdmFyKC0tdGV4dC1zZWNvbmRhcnkpOyBiYWNrZ3JvdW5kOiBub25lOyBib3JkZXI6IG5vbmU7IHBhZGRpbmc6IDA7IGN1cnNvcjogcG9pbnRlcjsgdGV4dC1kZWNvcmF0aW9uOiB1bmRlcmxpbmU7IH0KCi5kZWNrLWxvYWRlZCB7CiAgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA1cHg7IGZvbnQtc2l6ZTogMTFweDsKICBjb2xvcjogIzA4NTA0MTsgYmFja2dyb3VuZDogI0UxRjVFRTsgYm9yZGVyLXJhZGl1czogdmFyKC0tcmFkaXVzLW1kKTsgcGFkZGluZzogNXB4IDhweDsgbWFyZ2luLXRvcDogNXB4Owp9CgovKiDilIDilIAgZmVlZCBlbnRyaWVzIOKUgOKUgCAqLwouZmVlZC1lbXB0eSB7CiAgdGV4dC1hbGlnbjogY2VudGVyOyBwYWRkaW5nOiAzcmVtIDFyZW07CiAgY29sb3I6IHZhcigtLXRleHQtc2Vjb25kYXJ5KTsgZm9udC1zaXplOiAxM3B4Owp9CgouZmVlZC1lbnRyeSB7CiAgYmFja2dyb3VuZDogdmFyKC0tYmcpOwogIGJvcmRlcjogMC41cHggc29saWQgdmFyKC0tYm9yZGVyKTsKICBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMtbGcpOwogIG92ZXJmbG93OiBoaWRkZW47CiAgYW5pbWF0aW9uOiBzbGlkZUluIDAuMnMgZWFzZTsKfQoKQGtleWZyYW1lcyBzbGlkZUluIHsKICBmcm9tIHsgb3BhY2l0eTogMDsgdHJhbnNmb3JtOiB0cmFuc2xhdGVZKC02cHgpOyB9CiAgdG8gICB7IG9wYWNpdHk6IDE7IHRyYW5zZm9ybTogdHJhbnNsYXRlWSgwKTsgfQp9CgouZW50cnktaGVhZGVyIHsKICBkaXNwbGF5OiBmbGV4OyBhbGlnbi1pdGVtczogY2VudGVyOyBqdXN0aWZ5LWNvbnRlbnQ6IHNwYWNlLWJldHdlZW47CiAgcGFkZGluZzogMTBweCAxNHB4OwogIGJvcmRlci1ib3R0b206IDAuNXB4IHNvbGlkIHZhcigtLWJvcmRlcik7CiAgYmFja2dyb3VuZDogdmFyKC0tYmctc2Vjb25kYXJ5KTsKICBjdXJzb3I6IHBvaW50ZXI7CiAgdXNlci1zZWxlY3Q6IG5vbmU7Cn0KCi5lbnRyeS1oZWFkZXItbGVmdCB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogOHB4OyB9CgouZW50cnktc2VxIHsKICBmb250LXNpemU6IDExcHg7IGZvbnQtd2VpZ2h0OiA1MDA7CiAgYmFja2dyb3VuZDogdmFyKC0tYmcpOyBib3JkZXI6IDAuNXB4IHNvbGlkIHZhcigtLWJvcmRlci1zZWNvbmRhcnkpOwogIGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cy1tZCk7IHBhZGRpbmc6IDJweCA3cHg7CiAgY29sb3I6IHZhcigtLXRleHQtc2Vjb25kYXJ5KTsKfQoKLmVudHJ5LW5hbWUgeyBmb250LXNpemU6IDE0cHg7IGZvbnQtd2VpZ2h0OiA1MDA7IGNvbG9yOiB2YXIoLS10ZXh0KTsgfQouZW50cnktc3RyYXRlZ3kgeyBmb250LXNpemU6IDExcHg7IGNvbG9yOiB2YXIoLS10ZXh0LXNlY29uZGFyeSk7IH0KCi5lbnRyeS1yaWdodCB7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogOHB4OyB9CgouY29uZi1waWxsIHsKICBmb250LXNpemU6IDExcHg7IGZvbnQtd2VpZ2h0OiA1MDA7IHBhZGRpbmc6IDJweCA4cHg7CiAgYm9yZGVyLXJhZGl1czogOTk5cHg7Cn0KLmNvbmYtaGlnaCB7IGJhY2tncm91bmQ6ICNFMUY1RUU7IGNvbG9yOiAjMDg1MDQxOyB9Ci5jb25mLW1pZCAgeyBiYWNrZ3JvdW5kOiAjRUVFREZFOyBjb2xvcjogIzNDMzQ4OTsgfQouY29uZi1sb3cgIHsgYmFja2dyb3VuZDogI0YxRUZFODsgY29sb3I6ICM0NDQ0NDE7IH0KCi5lbnRyeS1jaGV2cm9uIHsgZm9udC1zaXplOiAxNHB4OyBjb2xvcjogdmFyKC0tdGV4dC1zZWNvbmRhcnkpOyB0cmFuc2l0aW9uOiB0cmFuc2Zvcm0gMC4yczsgfQouZW50cnktY2hldnJvbi5vcGVuIHsgdHJhbnNmb3JtOiByb3RhdGUoMTgwZGVnKTsgfQoKLmVudHJ5LW5ldy10YWcgewogIGZvbnQtc2l6ZTogMTBweDsgYmFja2dyb3VuZDogI0UxRjVFRTsgY29sb3I6ICMwODUwNDE7CiAgYm9yZGVyLXJhZGl1czogOTk5cHg7IHBhZGRpbmc6IDFweCA3cHg7IGZvbnQtd2VpZ2h0OiA1MDA7Cn0KCi5lbnRyeS1ib2R5IHsgcGFkZGluZzogMTJweCAxNHB4OyBkaXNwbGF5OiBub25lOyB9Ci5lbnRyeS1ib2R5Lm9wZW4geyBkaXNwbGF5OiBibG9jazsgfQoKLmVudHJ5LWNhcmQtYWRkZWQgewogIGZvbnQtc2l6ZTogMTFweDsgY29sb3I6IHZhcigtLXRleHQtc2Vjb25kYXJ5KTsKICBtYXJnaW4tYm90dG9tOiA4cHg7IGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogNXB4Owp9CgouZW50cnktY2FyZC1hZGRlZCAuYWRkZWQtY2FyZCB7CiAgYmFja2dyb3VuZDogdmFyKC0tYmctc2Vjb25kYXJ5KTsgYm9yZGVyOiAwLjVweCBzb2xpZCB2YXIoLS1ib3JkZXIpOwogIGJvcmRlci1yYWRpdXM6IHZhcigtLXJhZGl1cy1tZCk7IHBhZGRpbmc6IDFweCA3cHg7CiAgZm9udC1zaXplOiAxMXB4OyBjb2xvcjogdmFyKC0tdGV4dCk7Cn0KCi5jb25mLWJhci13cmFwIHsgbWFyZ2luLWJvdHRvbTogMTBweDsgfQouY29uZi1iYXIgeyBoZWlnaHQ6IDRweDsgYmFja2dyb3VuZDogdmFyKC0tYmctc2Vjb25kYXJ5KTsgYm9yZGVyLXJhZGl1czogMnB4OyBvdmVyZmxvdzogaGlkZGVuOyBtYXJnaW4tYm90dG9tOiAzcHg7IH0KLmNvbmYtYmFyLWZpbGwgeyBoZWlnaHQ6IDEwMCU7IGJvcmRlci1yYWRpdXM6IDJweDsgdHJhbnNpdGlvbjogd2lkdGggMC41cyBlYXNlOyB9CgouYmFkZ2VzIHsgZGlzcGxheTogZmxleDsgZmxleC13cmFwOiB3cmFwOyBnYXA6IDRweDsgbWFyZ2luLWJvdHRvbTogOHB4OyB9Ci5iYWRnZSB7IGZvbnQtc2l6ZTogMTFweDsgcGFkZGluZzogMnB4IDhweDsgYm9yZGVyLXJhZGl1czogdmFyKC0tcmFkaXVzLW1kKTsgZm9udC13ZWlnaHQ6IDUwMDsgfQouYmFkZ2UtdGVhbCAgIHsgYmFja2dyb3VuZDogI0UxRjVFRTsgY29sb3I6ICMwODUwNDE7IH0KLmJhZGdlLXB1cnBsZSB7IGJhY2tncm91bmQ6ICNFRUVERkU7IGNvbG9yOiAjM0MzNDg5OyB9Ci5iYWRnZS1hbWJlciAgeyBiYWNrZ3JvdW5kOiAjRkFFRURBOyBjb2xvcjogIzYzMzgwNjsgfQouYmFkZ2UtcmVkICAgIHsgYmFja2dyb3VuZDogI0ZDRUJFQjsgY29sb3I6ICM3OTFGMUY7IH0KLmJhZGdlLWJsdWUgICB7IGJhY2tncm91bmQ6ICNFNkYxRkI7IGNvbG9yOiAjMEM0NDdDOyB9Ci5iYWRnZS1ncmF5ICAgeyBiYWNrZ3JvdW5kOiAjRjFFRkU4OyBjb2xvcjogIzQ0NDQ0MTsgfQouYmFkZ2UtZ3JlZW4gIHsgYmFja2dyb3VuZDogI0VBRjNERTsgY29sb3I6ICMyNzUwMEE7IH0KCi5lbnRyeS1kZXNjIHsgZm9udC1zaXplOiAxMnB4OyBjb2xvcjogdmFyKC0tdGV4dC1zZWNvbmRhcnkpOyBsaW5lLWhlaWdodDogMS42OyBtYXJnaW4tYm90dG9tOiA2cHg7IH0KLmVudHJ5LWNvdW50ZXJzIHsgZm9udC1zaXplOiAxMnB4OyBjb2xvcjogdmFyKC0tdGV4dC1zZWNvbmRhcnkpOyB9Ci5lbnRyeS1jb3VudGVycyBzdHJvbmcgeyBjb2xvcjogdmFyKC0tdGV4dCk7IGZvbnQtd2VpZ2h0OiA1MDA7IH0KCi5vdGhlcnMtcm93IHsgbWFyZ2luLXRvcDogOHB4OyBkaXNwbGF5OiBmbGV4OyBmbGV4LXdyYXA6IHdyYXA7IGdhcDogNHB4OyB9Ci5vdGhlci1waWxsIHsKICBmb250LXNpemU6IDExcHg7IHBhZGRpbmc6IDJweCA4cHg7IGJvcmRlci1yYWRpdXM6IDk5OXB4OwogIGJhY2tncm91bmQ6IHZhcigtLWJnLXNlY29uZGFyeSk7IGJvcmRlcjogMC41cHggc29saWQgdmFyKC0tYm9yZGVyKTsKICBjb2xvcjogdmFyKC0tdGV4dC1zZWNvbmRhcnkpOwp9CgouYWR2aWNlLWJsb2NrIHsgbWFyZ2luLXRvcDogMTBweDsgYm9yZGVyLXRvcDogMC41cHggc29saWQgdmFyKC0tYm9yZGVyKTsgcGFkZGluZy10b3A6IDEwcHg7IH0KLmFkdmljZS10aXAgewogIGZvbnQtc2l6ZTogMTJweDsgY29sb3I6IHZhcigtLXRleHQtc2Vjb25kYXJ5KTsgbGluZS1oZWlnaHQ6IDEuNjsKICBwYWRkaW5nOiA2cHggMTBweDsgYmFja2dyb3VuZDogdmFyKC0tYmctc2Vjb25kYXJ5KTsKICBib3JkZXItcmFkaXVzOiB2YXIoLS1yYWRpdXMtbWQpOyBtYXJnaW4tYm90dG9tOiA1cHg7Cn0KLmFkdmljZS10aXAgc3Ryb25nIHsgY29sb3I6IHZhcigtLXRleHQpOyBmb250LXdlaWdodDogNTAwOyB9CgovKiDilIDilIAgY29uZmlkZW5jZSB0aW1lbGluZSBpbiBzaWRlYmFyIOKUgOKUgCAqLwoudGltZWxpbmUgeyBkaXNwbGF5OiBmbGV4OyBmbGV4LWRpcmVjdGlvbjogY29sdW1uOyBnYXA6IDRweDsgfQoudGwtcm93IHsgZGlzcGxheTogZmxleDsgYWxpZ24taXRlbXM6IGNlbnRlcjsgZ2FwOiA3cHg7IGZvbnQtc2l6ZTogMTFweDsgfQoudGwtc2VxIHsgbWluLXdpZHRoOiAyMHB4OyBjb2xvcjogdmFyKC0tdGV4dC1zZWNvbmRhcnkpOyBmb250LXNpemU6IDEwcHg7IH0KLnRsLWJhci1iZyB7IGZsZXg6IDE7IGJhY2tncm91bmQ6IHZhcigtLWJnLXRlcnRpYXJ5KTsgYm9yZGVyLXJhZGl1czogMnB4OyBoZWlnaHQ6IDZweDsgb3ZlcmZsb3c6IGhpZGRlbjsgfQoudGwtYmFyLWZpbGwgeyBoZWlnaHQ6IDEwMCU7IGJvcmRlci1yYWRpdXM6IDJweDsgdHJhbnNpdGlvbjogd2lkdGggMC41czsgfQoudGwtcGN0IHsgbWluLXdpZHRoOiAyOHB4OyBjb2xvcjogdmFyKC0tdGV4dC1zZWNvbmRhcnkpOyB0ZXh0LWFsaWduOiByaWdodDsgfQoudGwtbmFtZSB7IG1pbi13aWR0aDogODBweDsgY29sb3I6IHZhcigtLXRleHQpOyB3aGl0ZS1zcGFjZTogbm93cmFwOyBvdmVyZmxvdzogaGlkZGVuOyB0ZXh0LW92ZXJmbG93OiBlbGxpcHNpczsgfQoKLm1ldGhvZC1iYWRnZSB7CiAgZGlzcGxheTogaW5saW5lLWZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogM3B4OwogIGZvbnQtc2l6ZTogMTBweDsgcGFkZGluZzogMnB4IDZweDsgYm9yZGVyLXJhZGl1czogdmFyKC0tcmFkaXVzLW1kKTsgbWFyZ2luLWJvdHRvbTogOHB4Owp9Ci5tZXRob2QtcGF0dGVybiB7IGJhY2tncm91bmQ6ICNFMUY1RUU7IGNvbG9yOiAjMDg1MDQxOyB9Ci5tZXRob2QtYWkgICAgICB7IGJhY2tncm91bmQ6ICNFRUVERkU7IGNvbG9yOiAjM0MzNDg5OyB9CgouYW5hbHl6aW5nLXNwaW5uZXIgewogIGRpc3BsYXk6IGZsZXg7IGFsaWduLWl0ZW1zOiBjZW50ZXI7IGdhcDogNnB4OwogIGZvbnQtc2l6ZTogMTFweDsgY29sb3I6IHZhcigtLXRleHQtc2Vjb25kYXJ5KTsgcGFkZGluZzogOHB4IDA7Cn0KCi5kb3QtYW5pbTo6YWZ0ZXIgeyBjb250ZW50OiAnJzsgYW5pbWF0aW9uOiBkb3RzIDEuMnMgc3RlcHMoNCxlbmQpIGluZmluaXRlOyB9CkBrZXlmcmFtZXMgZG90cyB7IDAlLDIwJXtjb250ZW50OicuJ30gNDAle2NvbnRlbnQ6Jy4uJ30gNjAle2NvbnRlbnQ6Jy4uLid9IDgwJSwxMDAle2NvbnRlbnQ6Jyd9IH0KCi5hcGktcm93IHsgZGlzcGxheTogZmxleDsgZmxleC1kaXJlY3Rpb246IGNvbHVtbjsgZ2FwOiA0cHg7IH0KLmFwaS1yb3cgbGFiZWwgeyBmb250LXNpemU6IDExcHg7IGNvbG9yOiB2YXIoLS10ZXh0LXNlY29uZGFyeSk7IH0KLmFwaS1oaW50IHsgZm9udC1zaXplOiAxMHB4OyBjb2xvcjogdmFyKC0tdGV4dC1zZWNvbmRhcnkpOyB9Ci5hcGktaGludCBhIHsgY29sb3I6IHZhcigtLWFjY2VudC1hZHZpY2UpOyB9CgouY2xlYXItZmVlZC1idG4gewogIHdpZHRoOiAxMDAlOyBqdXN0aWZ5LWNvbnRlbnQ6IGNlbnRlcjsgZm9udC1zaXplOiAxMXB4OwogIGNvbG9yOiB2YXIoLS10ZXh0LXNlY29uZGFyeSk7IGJhY2tncm91bmQ6IG5vbmU7IGJvcmRlci1jb2xvcjogdmFyKC0tYm9yZGVyKTsKfQoKLm5ldy1nYW1lLWJ0biB7CiAgd2lkdGg6IDEwMCU7IGp1c3RpZnktY29udGVudDogY2VudGVyOyBmb250LXNpemU6IDEycHg7IGZvbnQtd2VpZ2h0OiA1MDA7CiAgYmFja2dyb3VuZDogdmFyKC0tdGV4dCk7IGNvbG9yOiB2YXIoLS1iZyk7IGJvcmRlci1jb2xvcjogdmFyKC0tdGV4dCk7Cn0KLm5ldy1nYW1lLWJ0bjpob3ZlciB7IG9wYWNpdHk6IDAuODg7IGJhY2tncm91bmQ6IHZhcigtLXRleHQpOyB9Cjwvc3R5bGU+CjwvaGVhZD4KPGJvZHk+Cgo8ZGl2IGNsYXNzPSJ0b3BiYXIiPgogIDxkaXYgY2xhc3M9InRvcGJhci10aXRsZSI+PGRpdiBjbGFzcz0iZG90Ij48L2Rpdj4gTVRHIEFyY2hldHlwZSBEZXRlY3RvcjwvZGl2PgogIDxkaXYgY2xhc3M9InRvcGJhci1zdWIiPkxpdmUgbWF0Y2ggZmVlZDwvZGl2Pgo8L2Rpdj4KCjxkaXYgY2xhc3M9ImFwcCI+CgogIDwhLS0g4pSA4pSAIFNJREVCQVIg4pSA4pSAIC0tPgogIDxkaXYgY2xhc3M9InNpZGViYXIiPgoKICAgIDxkaXYgY2xhc3M9ImFwaS1yb3ciPgogICAgICA8bGFiZWw+PGkgY2xhc3M9InRpIHRpLWtleSIgc3R5bGU9ImZvbnQtc2l6ZToxM3B4O3ZlcnRpY2FsLWFsaWduOi0ycHg7bWFyZ2luLXJpZ2h0OjNweCI+PC9pPiBBbnRocm9waWMgQVBJIGtleTwvbGFiZWw+CiAgICAgIDxpbnB1dCB0eXBlPSJwYXNzd29yZCIgaWQ9ImFwaS1rZXkiIHBsYWNlaG9sZGVyPSJzay1hbnQtLi4uIiAvPgogICAgICA8c3BhbiBjbGFzcz0iYXBpLWhpbnQiPkZvciBBSSBmYWxsYmFjayArIGFkdmljZS4gPGEgaHJlZj0iaHR0cHM6Ly9jb25zb2xlLmFudGhyb3BpYy5jb20iIHRhcmdldD0iX2JsYW5rIj5HZXQgb25lIGhlcmU8L2E+LiBTdGF5cyBpbiBicm93c2VyIG9ubHkuPC9zcGFuPgogICAgPC9kaXY+CgogICAgPGRpdj4KICAgICAgPGRpdiBjbGFzcz0ic2VjdGlvbi1sYWJlbCIgc3R5bGU9Im1hcmdpbi1ib3R0b206NXB4OyI+Rm9ybWF0PC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImZvcm1hdC1yb3ciIGlkPSJmb3JtYXQtcm93Ij48L2Rpdj4KICAgIDwvZGl2PgoKICAgIDxkaXYgY2xhc3M9InBhbmVsIj4KICAgICAgPGRpdiBjbGFzcz0icGFuZWwtdGl0bGUiPjxpIGNsYXNzPSJ0aSB0aS1jYXJkcyI+PC9pPiBNeSBkZWNrPC9kaXY+CiAgICAgIDx0ZXh0YXJlYSBpZD0ibXktZGVjay1pbnB1dCIgcGxhY2Vob2xkZXI9IlBhc3RlIEFyZW5hIGV4cG9ydDoKCjQgTGlnaHRuaW5nIEJvbHQKNCBHb2JsaW4gR3VpZGUKLi4uCgooQXJlbmEg4oaSIFNoYXJlIOKGkiBDb3B5IERlY2tsaXN0KSI+PC90ZXh0YXJlYT4KICAgICAgPGJ1dHRvbiBjbGFzcz0icGFyc2UtYnRuIiBvbmNsaWNrPSJwYXJzZURlY2soKSI+PGkgY2xhc3M9InRpIHRpLWNoZWNrIj48L2k+IExvYWQgZGVjazwvYnV0dG9uPgogICAgICA8ZGl2IGlkPSJkZWNrLXN0YXR1cyI+PC9kaXY+CiAgICA8L2Rpdj4KCiAgICA8ZGl2IGNsYXNzPSJwYW5lbCI+CiAgICAgIDxkaXYgY2xhc3M9InBhbmVsLXRpdGxlIj48aSBjbGFzcz0idGkgdGktZXllIj48L2k+IE9wcG9uZW50IGNhcmRzIHNlZW48L2Rpdj4KICAgICAgPGRpdiBjbGFzcz0idGFnLWFyZWEiIGlkPSJ0YWdzIj48c3BhbiBjbGFzcz0iZW1wdHktaGludCI+VHlwZSBhIGNhcmQgYW5kIHByZXNzIEVudGVyPC9zcGFuPjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJjYXJkLWlucHV0LXJvdyI+CiAgICAgICAgPGlucHV0IHR5cGU9InRleHQiIGlkPSJjYXJkLWlucHV0IiBwbGFjZWhvbGRlcj0iQ2FyZCBuYW1lLi4uIiBvbmtleWRvd249ImlmKGV2ZW50LmtleT09PSdFbnRlcicpYWRkQ2FyZCgpIiAvPgogICAgICAgIDxidXR0b24gb25jbGljaz0iYWRkQ2FyZCgpIiBzdHlsZT0icGFkZGluZzowIDEycHg7Ij48aSBjbGFzcz0idGkgdGktcGx1cyI+PC9pPjwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpmbGV4LWVuZDttYXJnaW4tdG9wOjVweDsiPgogICAgICAgIDxidXR0b24gY2xhc3M9ImNsZWFyLWxpbmsiIG9uY2xpY2s9ImNsZWFyQWxsKCkiPkNsZWFyIGFsbCBjYXJkczwvYnV0dG9uPgogICAgICA8L2Rpdj4KICAgIDwvZGl2PgoKICAgIDxkaXYgaWQ9InRpbWVsaW5lLXBhbmVsIiBzdHlsZT0iZGlzcGxheTpub25lOyI+CiAgICAgIDxkaXYgY2xhc3M9InNlY3Rpb24tbGFiZWwiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjZweDsiPkNvbmZpZGVuY2Ugb3ZlciB0aW1lPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9InRpbWVsaW5lIiBpZD0idGltZWxpbmUiPjwvZGl2PgogICAgPC9kaXY+CgogICAgPGRpdiBpZD0iZ2FtZS1zdGF0ZS1wYW5lbCIgc3R5bGU9ImRpc3BsYXk6bm9uZTsiPjwvZGl2PgoKICAgIDxkaXYgaWQ9IndhdGNoZXItc3RhdHVzIiBzdHlsZT0iZm9udC1zaXplOjExcHg7bGluZS1oZWlnaHQ6MS41O3BhZGRpbmc6OHB4IDEwcHg7YmFja2dyb3VuZDp2YXIoLS1iZy1zZWNvbmRhcnkpO2JvcmRlci1yYWRpdXM6dmFyKC0tcmFkaXVzLW1kKTsiPgogICAgICA8c3BhbiBzdHlsZT0iY29sb3I6dmFyKC0tdGV4dC1zZWNvbmRhcnkpIj48aSBjbGFzcz0idGkgdGktd2lmaS1vZmYiIHN0eWxlPSJmb250LXNpemU6MTNweDt2ZXJ0aWNhbC1hbGlnbjotMnB4Ij48L2k+IENoZWNraW5nIGZvciBBcmVuYSB3YXRjaGVyLi4uPC9zcGFuPgogICAgPC9kaXY+CgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2ZsZXgtZGlyZWN0aW9uOmNvbHVtbjtnYXA6NnB4O21hcmdpbi10b3A6YXV0bzsiPgogICAgICA8YnV0dG9uIGNsYXNzPSJuZXctZ2FtZS1idG4iIG9uY2xpY2s9Im5ld0dhbWUoKTtyZXNldFdhdGNoZXIoKSI+PGkgY2xhc3M9InRpIHRpLXJlZnJlc2giPjwvaT4gTmV3IGdhbWU8L2J1dHRvbj4KICAgICAgPGJ1dHRvbiBjbGFzcz0iY2xlYXItZmVlZC1idG4iIG9uY2xpY2s9ImNsZWFyRmVlZCgpIj48aSBjbGFzcz0idGkgdGktdHJhc2giPjwvaT4gQ2xlYXIgZmVlZDwvYnV0dG9uPgogICAgPC9kaXY+CgogIDwvZGl2PgoKICA8IS0tIOKUgOKUgCBGRUVEIOKUgOKUgCAtLT4KICA8ZGl2IGNsYXNzPSJmZWVkIiBpZD0iZmVlZCI+CiAgICA8ZGl2IGNsYXNzPSJmZWVkLWVtcHR5IiBpZD0iZmVlZC1lbXB0eSI+CiAgICAgIDxpIGNsYXNzPSJ0aSB0aS1jYXJkcyIgc3R5bGU9ImZvbnQtc2l6ZTozMnB4O2Rpc3BsYXk6YmxvY2s7bWFyZ2luLWJvdHRvbTo4cHg7Y29sb3I6dmFyKC0tdGV4dC1zZWNvbmRhcnkpIj48L2k+CiAgICAgIEFkZCBvcHBvbmVudCBjYXJkcyB0byBzdGFydCByZWFkaW5nIHRoZWlyIGRlY2sKICAgIDwvZGl2PgogIDwvZGl2PgoKPC9kaXY+Cgo8c2NyaXB0Pgpjb25zdCBGT1JNQVRTID0gWyJBbGwiLCJNb2Rlcm4iLCJMZWdhY3kiLCJQaW9uZWVyIiwiSGlzdG9yaWMiLCJTdGFuZGFyZCIsIkNvbW1hbmRlciJdOwpsZXQgc2VsZWN0ZWRGb3JtYXQgPSAiQWxsIjsKbGV0IG9wcG9uZW50Q2FyZHMgPSBbXTsKbGV0IG15RGVjayA9IHsgY2FyZHM6IFtdIH07CmxldCBmZWVkRW50cmllcyA9IFtdOwpsZXQgYW5hbHl6aW5nID0gZmFsc2U7CmxldCBhbmFseXNpc1F1ZXVlID0gZmFsc2U7Cgpjb25zdCBBTExfQVJDSEVUWVBFUyA9IFsKICB7IG5hbWU6IkJ1cm4iLCBmb3JtYXRzOlsiTW9kZXJuIiwiTGVnYWN5IiwiSGlzdG9yaWMiLCJTdGFuZGFyZCJdLCBjb2xvcnM6WyJSZWQiXSwgc3RyYXRlZ3k6IkFnZ3JvIiwgY2FyZHM6WyJsaWdodG5pbmcgYm9sdCIsImdvYmxpbiBndWlkZSIsImVpZG9sb24gb2YgdGhlIGdyZWF0IHJldmVsIiwibGF2YSBzcGlrZSIsInJpZnQgYm9sdCIsInNrdWxsY3JhY2siLCJzaGFyZCB2b2xsZXkiLCJsaWdodG5pbmcgaGVsaXgiLCJib3JvcyBjaGFybSIsInNlYXJpbmcgYmxhemUiLCJtb25hc3Rlcnkgc3dpZnRzcGVhciIsInN1bmJha2VkIGNhbnlvbiIsInNhY3JlZCBmb3VuZHJ5IiwibGlnaHQgdXAgdGhlIHN0YWdlIiwiYXRhcmthJ3MgY29tbWFuZCIsImJ1bXAgaW4gdGhlIG5pZ2h0IiwidmV4aW5nIGRldmlsIiwicmFtdW5hcCBydWlucyIsImt1bWFubyIsInBsYXkgd2l0aCBmaXJlIiwicGhvZW5peCBjaGljayIsInN0cmFuZ2xlIl0sIGRlc2M6IlJhY2VzIHRvIDIwIGRhbWFnZSB3aXRoIGNoZWFwIGRpcmVjdCBkYW1hZ2UgYW5kIGFnZ3Jlc3NpdmUgMS1kcm9wcyBiZWZvcmUgdGhlIG9wcG9uZW50IGNhbiBzdGFiaWxpemUuIiwgY291bnRlcnM6IkxpZmVnYWluLCBMZXlsaW5lIG9mIFNhbmN0aXR5LCBEcmFnb24ncyBDbGF3LCBXZWF0aGVyIHRoZSBTdG9ybS4iIH0sCiAgeyBuYW1lOiJBbXVsZXQgVGl0YW4iLCBmb3JtYXRzOlsiTW9kZXJuIl0sIGNvbG9yczpbIkdyZWVuIiwiQmx1ZSIsIlJlZCJdLCBzdHJhdGVneToiQ29tYm8gLyBSYW1wIiwgY2FyZHM6WyJhbXVsZXQgb2Ygdmlnb3IiLCJwcmltZXZhbCB0aXRhbiIsInN1bW1vbmVyJ3MgcGFjdCIsInRvbGFyaWEgd2VzdCIsInNpbWljIGdyb3d0aCBjaGFtYmVyIiwiYm9yb3MgZ2Fycmlzb24iLCJmaWVsZCBvZiB0aGUgZGVhZCIsInNsYXllcnMnIHN0cm9uZ2hvbGQiLCJkcnlhZCBvZiB0aGUgaWx5c2lhbiBncm92ZSIsImF6dXNhIGxvc3QgYnV0IHNlZWtpbmciLCJleHBsb3JlIiwiYW5jaWVudCBzdGlycmluZ3MiLCJvbmNlIHVwb24gYSB0aW1lIiwiYXJib3JlYWwgZ3JhemVyIiwid2Fsa2luZyBiYWxsaXN0YSIsImNhdmVybiBvZiBzb3VscyJdLCBkZXNjOiJVc2VzIEFtdWxldCBvZiBWaWdvciB3aXRoIGJvdW5jZSBsYW5kcyB0byBnZW5lcmF0ZSBtYXNzaXZlIG1hbmEsIHRoZW4gZGVwbG95cyBQcmltZXZhbCBUaXRhbiBhcyBhIGdhbWUtZW5kaW5nIGVuZ2luZS4iLCBjb3VudGVyczoiQmxvb2QgTW9vbiwgQWxwaW5lIE1vb24sIERhbXBpbmcgU3BoZXJlLCBTdXJnaWNhbCBFeHRyYWN0aW9uIG9uIFByaW1ldmFsIFRpdGFuLiIgfSwKICB7IG5hbWU6Ik11cmt0aWRlIFJlZ2VudCIsIGZvcm1hdHM6WyJNb2Rlcm4iXSwgY29sb3JzOlsiQmx1ZSIsIlJlZCJdLCBzdHJhdGVneToiVGVtcG8iLCBjYXJkczpbIm11cmt0aWRlIHJlZ2VudCIsImRyYWdvbidzIHJhZ2UgY2hhbm5lbGVyIiwiZXhwcmVzc2l2ZSBpdGVyYXRpb24iLCJtaXNocmEncyBiYXVibGUiLCJsaWdodG5pbmcgYm9sdCIsImNvdW50ZXJzcGVsbCIsImNvbnNpZGVyIiwic3BlbGwgcGllcmNlIiwidW5ob2x5IGhlYXQiLCJzY2FsZGluZyB0YXJuIiwic3RlYW0gdmVudHMiLCJmaWVyeSBpc2xldCIsInNwaXJlYmx1ZmYgY2FuYWwiLCJmb3JjZSBvZiBuZWdhdGlvbiIsImFyY2htYWdlJ3MgY2hhcm0iXSwgZGVzYzoiQSB0ZW1wbyBkZWNrIHVzaW5nIGNhbnRyaXBzIGFuZCBkaXNydXB0aW9uIHRvIGZ1ZWwgYSBtYXNzaXZlIE11cmt0aWRlIFJlZ2VudCwgYmFja2VkIGJ5IGJ1cm4gYW5kIGNvdW50ZXJzcGVsbHMuIiwgY291bnRlcnM6IkNoYWxpY2Ugb2YgdGhlIFZvaWQgb24gMSwgZ3JhdmV5YXJkIGhhdGUsIExleWxpbmUgb2YgU2FuY3RpdHkuIiB9LAogIHsgbmFtZToiSGFtbWVyIFRpbWUiLCBmb3JtYXRzOlsiTW9kZXJuIl0sIGNvbG9yczpbIldoaXRlIiwiQmx1ZSJdLCBzdHJhdGVneToiQWdncm8gLyBDb21ibyIsIGNhcmRzOlsiY29sb3NzdXMgaGFtbWVyIiwic2lnYXJkYSdzIGFpZCIsInB1cmVzdGVlbCBwYWxhZGluIiwib3JuaXRob3B0ZXIiLCJzdGVlbHNoYXBlcidzIGdpZnQiLCJlc3BlciBzZW50aW5lbCIsInN0b25lZm9yZ2UgbXlzdGljIiwidXJ6YSdzIHNhZ2EiLCJpbmttb3RoIG5leHVzIiwic3ByaW5nbGVhZiBkcnVtIiwicGFyYWRpc2UgbWFudGxlIiwic2hhZG93c3BlYXIiLCJsdXhpb3IgZ2lhZGEncyBnaWZ0Il0sIGRlc2M6IkF0dGFjaGVzIENvbG9zc3VzIEhhbW1lciB0byBhbiBldmFzaXZlIGNyZWF0dXJlIGZvciBhIHBvdGVudGlhbCB0dXJuLTIga2lsbCwgYnlwYXNzaW5nIGVxdWlwIGNvc3RzIHZpYSBTaWdhcmRhJ3MgQWlkIG9yIFB1cmVzdGVlbCBQYWxhZGluLiIsIGNvdW50ZXJzOiJTdG9ueSBTaWxlbmNlLCBGb3JjZSBvZiBWaWdvciwgSHVya3lsJ3MgUmVjYWxsLCBWb2lkIE1pcnJvci4iIH0sCiAgeyBuYW1lOiJSaGlub3MgLyBDcmFzaGNhZGUiLCBmb3JtYXRzOlsiTW9kZXJuIl0sIGNvbG9yczpbIkdyZWVuIiwiUmVkIiwiV2hpdGUiLCJCbHVlIl0sIHN0cmF0ZWd5OiJDb21ibyIsIGNhcmRzOlsic2hhcmRsZXNzIGFnZW50IiwidmlvbGVudCBvdXRidXJzdCIsImNyYXNoaW5nIGZvb3RmYWxscyIsInN1YnRsZXR5IiwiYnJhemVuIGJvcnJvd2VyIiwiZm9yY2Ugb2YgbmVnYXRpb24iLCJmb3JjZSBvZiB2aWdvciIsImFyZGVudCBwbGVhIiwiZGVtb25pYyBkcmVhZCIsImJsb29kYnJhaWQgZWxmIiwibGl2aW5nIGVuZCIsImJvbmVjcnVzaGVyIGdpYW50IiwidGhlIG9uZSByaW5nIl0sIGRlc2M6IkNhc2NhZGVzIGludG8gQ3Jhc2hpbmcgRm9vdGZhbGxzIHRvIGRlcGxveSB0d28gZnJlZSA0LzQgUmhpbm9zLCBwcm90ZWN0ZWQgYnkgRm9yY2Ugb2YgTmVnYXRpb24gYW5kIFN1YnRsZXR5LiIsIGNvdW50ZXJzOiJDaGFsaWNlIG9uIDAsIFZvaWQgTWlycm9yLCBjb3VudGVybWFnaWMuIiB9LAogIHsgbmFtZToiVHJvbiIsIGZvcm1hdHM6WyJNb2Rlcm4iXSwgY29sb3JzOlsiR3JlZW4iLCJDb2xvcmxlc3MiXSwgc3RyYXRlZ3k6IlJhbXAiLCBjYXJkczpbInVyemEncyB0b3dlciIsInVyemEncyBwb3dlciBwbGFudCIsInVyemEncyBtaW5lIiwia2FybiBsaWJlcmF0ZWQiLCJ1Z2luIHRoZSBzcGlyaXQgZHJhZ29uIiwid3VybWNvaWwgZW5naW5lIiwib2JsaXZpb24gc3RvbmUiLCJjaHJvbWF0aWMgc3BoZXJlIiwiY2hyb21hdGljIHN0YXIiLCJleHBlZGl0aW9uIG1hcCIsImFuY2llbnQgc3RpcnJpbmdzIiwic3lsdmFuIHNjcnlpbmciLCJlbXJha3VsIHRoZSBhZW9ucyB0b3JuIiwid29ybGQgYnJlYWtlciIsInVsYW1vZyB0aGUgY2Vhc2VsZXNzIGh1bmdlciIsInJlbGljIG9mIHByb2dlbml0dXMiXSwgZGVzYzoiQXNzZW1ibGVzIHRoZSBVcnphIHRyaWZlY3RhIGZvciA3KyBtYW5hIG9uIHR1cm4gMywgZGVwbG95aW5nIEthcm4gb3IgV3VybWNvaWwgRW5naW5lLiIsIGNvdW50ZXJzOiJCbG9vZCBNb29uLCBBbHBpbmUgTW9vbiwgU3ByZWFkaW5nIFNlYXMsIEdob3N0IFF1YXJ0ZXIuIiB9LAogIHsgbmFtZToiTGl2aW5nIEVuZCIsIGZvcm1hdHM6WyJNb2Rlcm4iXSwgY29sb3JzOlsiQmxhY2siLCJHcmVlbiIsIlJlZCJdLCBzdHJhdGVneToiQ29tYm8iLCBjYXJkczpbImxpdmluZyBlbmQiLCJ2aW9sZW50IG91dGJ1cnN0IiwiYXJkZW50IHBsZWEiLCJzdHJlZXQgd3JhaXRoIiwiYXJjaGl0ZWN0cyBvZiB3aWxsIiwiZGVhZHNob3QgbWlub3RhdXIiLCJjdXJhdG9yIG9mIG15c3RlcmllcyIsImZhZXJpZSBtYWNhYnJlIiwiZGVzZXJ0IGNlcm9kb24iLCJncmllZiIsInNvbGl0dWRlIiwiYnJhemVuIGJvcnJvd2VyIiwiZm9yY2Ugb2YgbmVnYXRpb24iXSwgZGVzYzoiQ3ljbGVzIGxhcmdlIGNyZWF0dXJlcyBpbnRvIHRoZSBncmF2ZXlhcmQsIHRoZW4gY2FzY2FkZXMgaW50byBMaXZpbmcgRW5kIHRvIHN3ZWVwIHRoZSBib2FyZCB3aGlsZSByZWFuaW1hdGluZyB5b3Vycy4iLCBjb3VudGVyczoiQ2hhbGljZSBvbiAwLCBMZXlsaW5lIG9mIHRoZSBWb2lkLCBSZXN0IGluIFBlYWNlLCBWb2lkIE1pcnJvci4iIH0sCiAgeyBuYW1lOiJEZWF0aCdzIFNoYWRvdyIsIGZvcm1hdHM6WyJNb2Rlcm4iXSwgY29sb3JzOlsiQmxhY2siLCJSZWQiLCJHcmVlbiJdLCBzdHJhdGVneToiQWdncm8gLyBUZW1wbyIsIGNhcmRzOlsiZGVhdGgncyBzaGFkb3ciLCJyYWdhdmFuIG5pbWJsZSBwaWxmZXJlciIsImRyYWdvbidzIHJhZ2UgY2hhbm5lbGVyIiwic3RyZWV0IHdyYWl0aCIsIm1pc2hyYSdzIGJhdWJsZSIsInRob3VnaHRzZWl6ZSIsImlucXVpc2l0aW9uIG9mIGtvemlsZWsiLCJmYXRhbCBwdXNoIiwidW5ob2x5IGhlYXQiLCJ0ZW11ciBiYXR0bGUgcmFnZSIsInN0dWJib3JuIGRlbmlhbCIsImJsb29kIGNyeXB0IiwiYmxvb2RzdGFpbmVkIG1pcmUiLCJ2ZXJkYW50IGNhdGFjb21icyJdLCBkZXNjOiJEZWxpYmVyYXRlbHkgcmVkdWNlcyBpdHMgb3duIGxpZmUgdG90YWwgdG8gZGVwbG95IGEgbWFzc2l2ZSBEZWF0aCdzIFNoYWRvdyBhbG9uZ3NpZGUgaGFuZCBkaXNydXB0aW9uLiIsIGNvdW50ZXJzOiJMaWZlZ2FpbiwgUGF0aCB0byBFeGlsZSwgYm91bmNlIGVmZmVjdHMuIiB9LAogIHsgbmFtZToiWWF3Z21vdGggQ29tYm8iLCBmb3JtYXRzOlsiTW9kZXJuIl0sIGNvbG9yczpbIkJsYWNrIiwiR3JlZW4iXSwgc3RyYXRlZ3k6IkNvbWJvIiwgY2FyZHM6WyJ5YXdnbW90aCB0aHJhbiBwaHlzaWNpYW4iLCJ5b3VuZyB3b2xmIiwic3RyYW5nbGVyb290IGdlaXN0IiwiZ2VyYWxmJ3MgbWVzc2VuZ2VyIiwibmVjcm9za2l0dGVyIiwiZ3Jpc3QgdGhlIGh1bmdlciB0aWRlIiwiY2hvcmQgb2YgY2FsbGluZyIsImVsZHJpdGNoIGV2b2x1dGlvbiIsImNvbGxlY3RlZCBjb21wYW55IiwiZ29sZ2FyaSBjaGFybSIsIm51cnR1cmluZyBwZWF0bGFuZCIsIm92ZXJncm93biB0b21iIl0sIGRlc2M6Ikxvb3BzIFlhd2dtb3RoIHdpdGggdW5keWluZyBjcmVhdHVyZXMgdG8gZHJhdyBpbmZpbml0ZWx5IGFuZCBkcmFpbiB0aGUgb3Bwb25lbnQgdG8gemVyby4iLCBjb3VudGVyczoiR3JhZmRpZ2dlcidzIENhZ2UsIFlpeGxpZCBKYWlsZXIsIFNvbGl0dWRlLCBSZXN0IGluIFBlYWNlLiIgfSwKICB7IG5hbWU6IlN0b3JtIiwgZm9ybWF0czpbIk1vZGVybiIsIkxlZ2FjeSJdLCBjb2xvcnM6WyJCbHVlIiwiUmVkIl0sIHN0cmF0ZWd5OiJDb21ibyIsIGNhcmRzOlsiZ2lmdHMgdW5naXZlbiIsInBhc3QgaW4gZmxhbWVzIiwiZ3JhcGVzaG90IiwiZW1wdHkgdGhlIHdhcnJlbnMiLCJnb2JsaW4gZWxlY3Ryb21hbmNlciIsImJhcmFsIGNoaWVmIG9mIGNvbXBsaWFuY2UiLCJzZXJ1bSB2aXNpb25zIiwibWFuYW1vcnBob3NlIiwicHlyZXRpYyByaXR1YWwiLCJkZXNwZXJhdGUgcml0dWFsIiwic3RyaWtlIGl0IHJpY2giLCJzdGVhbSB2ZW50cyIsInBvbmRlciIsInByZW9yZGFpbiIsIm15c3RpY2FsIHR1dG9yIiwiYnVybmluZyB3aXNoIl0sIGRlc2M6IkNoYWlucyByaXR1YWxzIGFuZCBjYW50cmlwcyB0byBidWlsZCBzdG9ybSBjb3VudCBmb3IgYSBsZXRoYWwgR3JhcGVzaG90IG9yIEVtcHR5IHRoZSBXYXJyZW5zIGJ1cnN0LiIsIGNvdW50ZXJzOiJDaGFsaWNlIG9uIDEsIFJ1bGUgb2YgTGF3LCBFdGhlcnN3b3JuIENhbm9uaXN0LCBGbHVzdGVyc3Rvcm0sIERhbXBpbmcgU3BoZXJlLiIgfSwKICB7IG5hbWU6Ikh1bWFucyIsIGZvcm1hdHM6WyJNb2Rlcm4iXSwgY29sb3JzOlsiV2hpdGUiLCJCbHVlIiwiUmVkIiwiR3JlZW4iLCJCbGFjayJdLCBzdHJhdGVneToiQWdncm8iLCBjYXJkczpbInRoYWxpYSdzIGxpZXV0ZW5hbnQiLCJjaGFtcGlvbiBvZiB0aGUgcGFyaXNoIiwibWFudGlzIHJpZGVyIiwicmVmbGVjdG9yIG1hZ2UiLCJtZWRkbGluZyBtYWdlIiwia2l0ZXNhaWwgZnJlZWJvb3RlciIsIm1pbGl0aWEgYnVnbGVyIiwidGhhbGlhIGd1YXJkaWFuIG9mIHRocmFiZW4iLCJtYXlvciBvZiBhdmFicnVjayIsImltcGVyaWFsIHJlY3J1aXRlciIsInBoYW50YXNtYWwgaW1hZ2UiLCJhZXRoZXIgdmlhbCIsImFuY2llbnQgemlnZ3VyYXQiLCJjYXZlcm4gb2Ygc291bHMiLCJ1bmNsYWltZWQgdGVycml0b3J5Il0sIGRlc2M6IkZpdmUtY29sb3IgSHVtYW4gdHJpYmFsIHVzaW5nIEFldGhlciBWaWFsIGFuZCBDYXZlcm4gb2YgU291bHMgdG8gZG9kZ2UgaW50ZXJhY3Rpb24gd2hpbGUgdGF4aW5nIG9wcG9uZW50cy4iLCBjb3VudGVyczoiRW5naW5lZXJlZCBFeHBsb3NpdmVzLCBTdXByZW1lIFZlcmRpY3QsIEFuZ2VyIG9mIHRoZSBHb2RzLiIgfSwKICB7IG5hbWU6IlVXIENvbnRyb2wiLCBmb3JtYXRzOlsiTW9kZXJuIiwiUGlvbmVlciIsIkxlZ2FjeSJdLCBjb2xvcnM6WyJCbHVlIiwiV2hpdGUiXSwgc3RyYXRlZ3k6IkNvbnRyb2wiLCBjYXJkczpbImNvdW50ZXJzcGVsbCIsInN1cHJlbWUgdmVyZGljdCIsIndyYXRoIG9mIGdvZCIsInRlZmVyaSBoZXJvIG9mIGRvbWluYXJpYSIsImphY2UgdGhlIG1pbmQgc2N1bHB0b3IiLCJmb3JjZSBvZiBuZWdhdGlvbiIsImFyY2htYWdlJ3MgY2hhcm0iLCJzb2xpdHVkZSIsImxleWxpbmUgYmluZGluZyIsInByaXNtYXRpYyBlbmRpbmciLCJoYWxsb3dlZCBmb3VudGFpbiIsImNlbGVzdGlhbCBjb2xvbm5hZGUiLCJmbG9vZGVkIHN0cmFuZCIsInNuYXBjYXN0ZXIgbWFnZSIsIm9wdCIsImNvbnNpZGVyIiwiY3J5cHRpYyBjb21tYW5kIiwiYXpjYW50YSB0aGUgc3Vua2VuIHJ1aW4iXSwgZGVzYzoiUmVhY3RpdmUgY29udHJvbCB1c2luZyBjb3VudGVyc3BlbGxzIGFuZCBzd2VlcGVycywgd2lubmluZyB0aHJvdWdoIGNhcmQgYWR2YW50YWdlIGFuZCBwbGFuZXN3YWxrZXJzLiIsIGNvdW50ZXJzOiJQbGFuZXN3YWxrZXIgYWdncmVzc2lvbiwgVGhhbGlhLCBoYW5kIGRpc3J1cHRpb24sIEJsb29kIE1vb24uIiB9LAogIHsgbmFtZToiRG9vbXNkYXkiLCBmb3JtYXRzOlsiTGVnYWN5Il0sIGNvbG9yczpbIkJsdWUiLCJCbGFjayJdLCBzdHJhdGVneToiQ29tYm8iLCBjYXJkczpbImRvb21zZGF5IiwidGhhc3NhJ3Mgb3JhY2xlIiwiZGFyayByaXR1YWwiLCJsb3R1cyBwZXRhbCIsImZvcmNlIG9mIHdpbGwiLCJmb3JjZSBvZiBuZWdhdGlvbiIsInBvbmRlciIsImJyYWluc3Rvcm0iLCJwcmVvcmRhaW4iLCJ1bmRlcmdyb3VuZCBzZWEiLCJwb2xsdXRlZCBkZWx0YSIsImRhemUiLCJmbHVzdGVyc3Rvcm0iLCJkdXJlc3MiLCJzdHJlZXQgd3JhaXRoIl0sIGRlc2M6IkNhc3RzIERvb21zZGF5IHRvIGJ1aWxkIGEgNS1jYXJkIGxpYnJhcnkgcGlsZSwgdGhlbiB3aW5zIHRocm91Z2ggVGhhc3NhJ3MgT3JhY2xlIHdpdGggYW4gZW1wdHkgZGVjay4iLCBjb3VudGVyczoiU3VyZ2ljYWwgRXh0cmFjdGlvbiwgTmloaWwgU3BlbGxib21iLCBIdXNoYnJpbmdlci4iIH0sCiAgeyBuYW1lOiJTbmVhayAmIFNob3ciLCBmb3JtYXRzOlsiTGVnYWN5Il0sIGNvbG9yczpbIlJlZCIsIkJsdWUiXSwgc3RyYXRlZ3k6IkNvbWJvIiwgY2FyZHM6WyJzaG93IGFuZCB0ZWxsIiwic25lYWsgYXR0YWNrIiwiZW1yYWt1bCB0aGUgYWVvbnMgdG9ybiIsImdyaXNlbGJyYW5kIiwib21uaXNjaWVuY2UiLCJmb3JjZSBvZiB3aWxsIiwicG9uZGVyIiwiYnJhaW5zdG9ybSIsImxvdHVzIHBldGFsIiwiYW5jaWVudCB0b21iIiwiY2l0eSBvZiB0cmFpdG9ycyIsInZvbGNhbmljIGlzbGFuZCIsInNjYWxkaW5nIHRhcm4iXSwgZGVzYzoiUHV0cyBFbXJha3VsIG9yIEdyaXNlbGJyYW5kIGludG8gcGxheSB2aWEgU2hvdyBhbmQgVGVsbCBvciBTbmVhayBBdHRhY2sgYXMgZWFybHkgYXMgdHVybiAxLiIsIGNvdW50ZXJzOiJLYXJha2FzLCBDb250YWlubWVudCBQcmllc3QsIExleWxpbmUgb2YgdGhlIFZvaWQsIFBoeXJleGlhbiBSZXZva2VyLiIgfSwKICB7IG5hbWU6IkRlbHZlciAoTGVnYWN5KSIsIGZvcm1hdHM6WyJMZWdhY3kiXSwgY29sb3JzOlsiQmx1ZSIsIlJlZCJdLCBzdHJhdGVneToiVGVtcG8iLCBjYXJkczpbImRlbHZlciBvZiBzZWNyZXRzIiwibXVya3RpZGUgcmVnZW50IiwiZHJhZ29uJ3MgcmFnZSBjaGFubmVsZXIiLCJwb25kZXIiLCJicmFpbnN0b3JtIiwicHJlb3JkYWluIiwiZGF6ZSIsImZvcmNlIG9mIHdpbGwiLCJsaWdodG5pbmcgYm9sdCIsInVuaG9seSBoZWF0Iiwid2FzdGVsYW5kIiwidm9sY2FuaWMgaXNsYW5kIiwic2NhbGRpbmcgdGFybiIsImV4cHJlc3NpdmUgaXRlcmF0aW9uIiwicHlyb2JsYXN0Il0sIGRlc2M6IlBhaXJzIGNoZWFwIHRocmVhdHMgd2l0aCBmcmVlIGludGVyYWN0aW9uOyBXYXN0ZWxhbmQgYW5kIERhemUgbG9jayBvcHBvbmVudHMgb3V0IHdoaWxlIERlbHZlciBjbG9zZXMgdGhlIGdhbWUuIiwgY291bnRlcnM6IkJhc2ljIGxhbmRzLCBjcmVhdHVyZS1saWdodCBidWlsZHMsIENoYWxpY2Ugb24gMS4iIH0sCiAgeyBuYW1lOiJMYW5kcyIsIGZvcm1hdHM6WyJMZWdhY3kiXSwgY29sb3JzOlsiR3JlZW4iLCJSZWQiXSwgc3RyYXRlZ3k6IkNvbnRyb2wgLyBDb21ibyIsIGNhcmRzOlsiZGFyayBkZXB0aHMiLCJ0aGVzcGlhbidzIHN0YWdlIiwibWFyaXQgbGFnZSIsImxpZmUgZnJvbSB0aGUgbG9hbSIsImV4cGxvcmF0aW9uIiwiY3JvcCByb3RhdGlvbiIsIm1veCBkaWFtb25kIiwiZ2FtYmxlIiwicmlzaGFkYW4gcG9ydCIsIndhc3RlbGFuZCIsImdyb3ZlIG9mIHRoZSBidXJud2lsbG93cyIsInB1bmlzaGluZyBmaXJlIiwia2FyYWthcyIsImZpZWxkIG9mIHRoZSBkZWFkIiwiZ2hvc3QgcXVhcnRlciJdLCBkZXNjOiJEYXJrIERlcHRocyArIFRoZXNwaWFuJ3MgU3RhZ2UgY3JlYXRlcyBhIDIwLzIwIE1hcml0IExhZ2U7IFdhc3RlbGFuZCBhbmQgUmlzaGFkYW4gUG9ydCBsb2NrIHRoZSBvcHBvbmVudCBvdXQuIiwgY291bnRlcnM6IkJvanVrYSBCb2csIFN1cmdpY2FsIEV4dHJhY3Rpb24gb24gRGFyayBEZXB0aHMsIEthcmFrYXMuIiB9LAogIHsgbmFtZToiQU5UIC8gVEVTIiwgZm9ybWF0czpbIkxlZ2FjeSJdLCBjb2xvcnM6WyJCbHVlIiwiQmxhY2siLCJSZWQiXSwgc3RyYXRlZ3k6IkNvbWJvIiwgY2FyZHM6WyJhZCBuYXVzZWFtIiwidGVuZHJpbHMgb2YgYWdvbnkiLCJkYXJrIHJpdHVhbCIsImNhYmFsIHJpdHVhbCIsImxpb24ncyBleWUgZGlhbW9uZCIsImxvdHVzIHBldGFsIiwiaW5mZXJuYWwgdHV0b3IiLCJicmFpbnN0b3JtIiwicG9uZGVyIiwiZ2l0YXhpYW4gcHJvYmUiLCJkdXJlc3MiLCJ0aG91Z2h0c2VpemUiLCJwb2xsdXRlZCBkZWx0YSIsInVuZGVyZ3JvdW5kIHNlYSIsInZvbGNhbmljIGlzbGFuZCIsImVjaG8gb2YgZW9ucyJdLCBkZXNjOiJDaGFpbnMgcml0dWFscyBhbmQgZHJhdyB2aWEgQWQgTmF1c2VhbSB0byByZWFjaCBsZXRoYWwgVGVuZHJpbHMgc3Rvcm0gY291bnQsIG9mdGVuIG9uIHR1cm4gMSBvciAyLiIsIGNvdW50ZXJzOiJGbHVzdGVyc3Rvcm0sIE1pbmRicmVhayBUcmFwLCBMZXlsaW5lIG9mIFNhbmN0aXR5LCBDaGFsaWNlIG9uIDAuIiB9LAogIHsgbmFtZToiRGVhdGggJiBUYXhlcyIsIGZvcm1hdHM6WyJMZWdhY3kiLCJQaW9uZWVyIl0sIGNvbG9yczpbIldoaXRlIl0sIHN0cmF0ZWd5OiJBZ2dybyAvIENvbnRyb2wiLCBjYXJkczpbInRoYWxpYSBndWFyZGlhbiBvZiB0aHJhYmVuIiwiZmxpY2tlcndpc3AiLCJtb3RoZXIgb2YgcnVuZXMiLCJzdG9uZWZvcmdlIG15c3RpYyIsImJhdHRlcnNrdWxsIiwiYWV0aGVyIHZpYWwiLCJyaXNoYWRhbiBwb3J0Iiwid2FzdGVsYW5kIiwia2FyYWthcyIsInN3b3JkcyB0byBwbG93c2hhcmVzIiwicmVjcnVpdGVyIG9mIHRoZSBndWFyZCIsInNhbmN0dW0gcHJlbGF0ZSIsInBhbGFjZSBqYWlsZXIiLCJsZW9uaW4gYXJiaXRlciIsImdob3N0IHF1YXJ0ZXIiXSwgZGVzYzoiV2hpdGUgd2VlbmllIHByaXNvbiB0YXhpbmcgYW5kIGRpc3J1cHRpbmcgd2l0aCBUaGFsaWEsIFJpc2hhZGFuIFBvcnQsIGFuZCBXYXN0ZWxhbmQuIiwgY291bnRlcnM6IkJhc2ljIGxhbmRzLCBmYXN0IGNvbWJvLCBzd2VlcGVycywgYXJ0aWZhY3QgaGF0ZS4iIH0sCiAgeyBuYW1lOiJSZWFuaW1hdG9yIiwgZm9ybWF0czpbIkxlZ2FjeSJdLCBjb2xvcnM6WyJCbGFjayIsIkJsdWUiXSwgc3RyYXRlZ3k6IkNvbWJvIiwgY2FyZHM6WyJhbmltYXRlIGRlYWQiLCJleGh1bWUiLCJlbnRvbWIiLCJyZWFuaW1hdGUiLCJncmlzZWxicmFuZCIsImFyY2hvbiBvZiBjcnVlbHR5IiwiY2hhbmNlbGxvciBvZiB0aGUgYW5uZXgiLCJpb25hIHNoaWVsZCBvZiBlbWVyaWEiLCJkYXJrIHJpdHVhbCIsImxvdHVzIHBldGFsIiwiZm9yY2Ugb2Ygd2lsbCIsInRob3VnaHRzZWl6ZSIsImR1cmVzcyIsInVubWFzayIsInBvbGx1dGVkIGRlbHRhIiwidW5kZXJncm91bmQgc2VhIl0sIGRlc2M6IkVudG9tYnMgYSBtYXNzaXZlIGNyZWF0dXJlIHRoZW4gcmVhbmltYXRlcyBpdCBpbW1lZGlhdGVseSBmb3IgYW4gZWFybHkgZ2FtZS13aW5uaW5nIHRocmVhdC4iLCBjb3VudGVyczoiTGV5bGluZSBvZiB0aGUgVm9pZCwgUmVzdCBpbiBQZWFjZSwgRmFlcmllIE1hY2FicmUsIEthcmFrYXMuIiB9LAogIHsgbmFtZToiUmFrZG9zIE1pZHJhbmdlIiwgZm9ybWF0czpbIlBpb25lZXIiLCJIaXN0b3JpYyJdLCBjb2xvcnM6WyJCbGFjayIsIlJlZCJdLCBzdHJhdGVneToiTWlkcmFuZ2UiLCBjYXJkczpbInRob3VnaHRzZWl6ZSIsImZhdGFsIHB1c2giLCJibG9vZHRpdGhlIGhhcnZlc3RlciIsImZhYmxlIG9mIHRoZSBtaXJyb3ItYnJlYWtlciIsInNoZW9sZHJlZCB0aGUgYXBvY2FseXBzZSIsImdyYXZleWFyZCB0cmVzcGFzc2VyIiwic29yaW4gaW1wZXJpb3VzIGJsb29kbG9yZCIsImthbGl0YXMgdHJhaXRvciBvZiBnaGV0IiwidmVpbiByaXBwZXIiLCJyZWNrb25lciBiYW5rYnVzdGVyIiwia3JveGEgdGl0YW4gb2YgZGVhdGgncyBodW5nZXIiLCJkcmVhZGJvcmUiLCJibG9vZCBjcnlwdCIsImJsYWNrY2xlYXZlIGNsaWZmcyIsImhhdW50ZWQgcmlkZ2UiXSwgZGVzYzoiVmFsdWUgbWlkcmFuZ2UgY29tYmluaW5nIGhhbmQgZGlzcnVwdGlvbiwgZWZmaWNpZW50IHJlbW92YWwsIGFuZCBwb3dlcmZ1bCB0aHJlYXRzIGluIFNoZW9sZHJlZCBhbmQgRmFibGUuIiwgY291bnRlcnM6IkxleWxpbmUgb2YgU2FuY3RpdHksIGZhc3QgYWdncm8sIGVuY2hhbnRtZW50IHJlbW92YWwgZm9yIEZhYmxlLiIgfSwKICB7IG5hbWU6IkxvdHVzIEZpZWxkIENvbWJvIiwgZm9ybWF0czpbIlBpb25lZXIiLCJIaXN0b3JpYyJdLCBjb2xvcnM6WyJHcmVlbiIsIkJsdWUiXSwgc3RyYXRlZ3k6IkNvbWJvIiwgY2FyZHM6WyJsb3R1cyBmaWVsZCIsImhpZGRlbiBzdHJpbmdzIiwicG9yZSBvdmVyIHRoZSBwYWdlcyIsInRoYXNzYSdzIG9yYWNsZSIsInRob3VnaHQgZGlzdG9ydGlvbiIsInN0cmF0ZWdpYyBwbGFubmluZyIsInNoaW1tZXIgb2YgcG9zc2liaWxpdHkiLCJzeWx2YW4gc2NyeWluZyIsImJhbGEgZ2VkIHJlY292ZXJ5IiwiYXJib3JlYWwgZ3JhemVyIiwiZXhwbG9yZSIsIndpbGRlcm5lc3MgcmVjbGFtYXRpb24iXSwgZGVzYzoiVW50YXBzIExvdHVzIEZpZWxkIHJlcGVhdGVkbHkgdG8gYnVpbGQgaW5maW5pdGUgbWFuYSwgd2lubmluZyB0aHJvdWdoIFRoYXNzYSdzIE9yYWNsZS4iLCBjb3VudGVyczoiRmllbGQgb2YgUnVpbiwgRGFtcGluZyBTcGhlcmUsIEFscGluZSBNb29uLCBUaG91Z2h0c2VpemUuIiB9LAogIHsgbmFtZToiTW9uby1HcmVlbiBEZXZvdGlvbiIsIGZvcm1hdHM6WyJQaW9uZWVyIiwiSGlzdG9yaWMiXSwgY29sb3JzOlsiR3JlZW4iXSwgc3RyYXRlZ3k6IlJhbXAgLyBDb21ibyIsIGNhcmRzOlsiZWx2aXNoIG15c3RpYyIsImxsYW5vd2FyIGVsdmVzIiwib2xkLWdyb3d0aCB0cm9sbCIsIm55a3Rob3Mgc2hyaW5lIHRvIG55eCIsImNhdmFsaWVyIG9mIHRob3JucyIsInN0b3JtIHRoZSBmZXN0aXZhbCIsImtpb3JhIGJlaGVtb3RoIGJlY2tvbmVyIiwidm9yYWNpb3VzIGh5ZHJhIiwia2FybiB0aGUgZ3JlYXQgY3JlYXRvciIsIm5pc3NhIHdobyBzaGFrZXMgdGhlIHdvcmxkIiwidWxhbW9nIHRoZSBjZWFzZWxlc3MgaHVuZ2VyIiwicG9sdWtyYW5vcyB3b3JsZCBlYXRlciJdLCBkZXNjOiJHZW5lcmF0ZXMgZW5vcm1vdXMgbWFuYSB0aHJvdWdoIE55a3Rob3MgYW5kIG1hbmEgZG9ya3MsIGRlcGxveWluZyBVbGFtb2cgb3IgbG9ja2luZyB3aXRoIEthcm4uIiwgY291bnRlcnM6IkFscGluZSBNb29uLCByZW1vdmFsIGZvciBtYW5hIGRvcmtzLCBzd2VlcGVycywgRGFtcGluZyBTcGhlcmUuIiB9LAogIHsgbmFtZToiSXp6ZXQgUGhvZW5peCIsIGZvcm1hdHM6WyJQaW9uZWVyIiwiSGlzdG9yaWMiXSwgY29sb3JzOlsiQmx1ZSIsIlJlZCJdLCBzdHJhdGVneToiQWdncm8gLyBDb21ibyIsIGNhcmRzOlsiYXJjbGlnaHQgcGhvZW5peCIsInRyZWFzdXJlIGNydWlzZSIsInRlbXBvcmFsIHRyZXNwYXNzIiwic3RyYXRlZ2ljIHBsYW5uaW5nIiwiY2hhcnQgYSBjb3Vyc2UiLCJjb25zaWRlciIsInBpZWNlcyBvZiB0aGUgcHV6emxlIiwibGVkZ2VyIHNocmVkZGVyIiwiY3JhY2tsaW5nIGRyYWtlIiwiZmllcnkgdGVtcGVyIiwicml2ZXJnbGlkZSBwYXRod2F5Iiwic3BpcmVibHVmZiBjYW5hbCIsInN0ZWFtIHZlbnRzIiwic2hpdmFuIHJlZWYiXSwgZGVzYzoiRmlsbHMgdGhlIGdyYXZleWFyZCB3aXRoIGNoZWFwIHNwZWxscyB0byByZWN1ciBBcmNsaWdodCBQaG9lbml4LCBiYWNrZWQgYnkgVHJlYXN1cmUgQ3J1aXNlIGZvciBleHBsb3NpdmUgY2FyZCBhZHZhbnRhZ2UuIiwgY291bnRlcnM6IkdyYXZleWFyZCBoYXRlIChSZXN0IGluIFBlYWNlLCBVbmxpY2Vuc2VkIEhlYXJzZSksIERhbXBpbmcgU3BoZXJlLiIgfSwKICB7IG5hbWU6IkFiemFuIEdyZWFzZWZhbmciLCBmb3JtYXRzOlsiUGlvbmVlciIsIkhpc3RvcmljIl0sIGNvbG9yczpbIldoaXRlIiwiQmxhY2siLCJHcmVlbiJdLCBzdHJhdGVneToiQ29tYm8iLCBjYXJkczpbImdyZWFzZWZhbmcgb2tpYmEgYm9zcyIsInBhcmhlbGlvbiBpaSIsInZlc3NlbCBvZiBuYXNjZW5jeSIsInJhZmZpbmUncyBpbmZvcm1hbnQiLCJmYWxzaWZpZWQgZG9jdW1lbnRzIiwiZmFibGUgb2YgdGhlIG1pcnJvci1icmVha2VyIiwidGhvdWdodHNlaXplIiwiZmF0YWwgcHVzaCIsImNvbmNlYWxlZCBjb3VydHlhcmQiLCJvdmVyZ3Jvd24gdG9tYiIsImdvZGxlc3Mgc2hyaW5lIl0sIGRlc2M6IkRpc2NhcmRzIFBhcmhlbGlvbiBJSSB0aGVuIHJldHVybnMgaXQgYXR0YWNraW5nIG9uIHR1cm4gMyB3aXRoIEdyZWFzZWZhbmcgZm9yIGEgbWFzc2l2ZSBsaWZlbGluayBmbHlpbmcgYXNzYXVsdC4iLCBjb3VudGVyczoiR3JhdmV5YXJkIGhhdGUsIEZhdGFsIFB1c2ggb24gR3JlYXNlZmFuZywgYXJ0aWZhY3QgcmVtb3ZhbC4iIH0sCiAgeyBuYW1lOiJBdXJhcyAoQm9nbGVzKSIsIGZvcm1hdHM6WyJIaXN0b3JpYyJdLCBjb2xvcnM6WyJXaGl0ZSIsIkdyZWVuIiwiQmx1ZSJdLCBzdHJhdGVneToiQWdncm8gLyBDb21ibyIsIGNhcmRzOlsic3JhbSBzZW5pb3IgZWRpZmljZXIiLCJrb3Igc3Bpcml0ZGFuY2VyIiwic2V0ZXNzYW4gY2hhbXBpb24iLCJhbGwgdGhhdCBnbGl0dGVycyIsImV0aGVyZWFsIGFybW9yIiwiY3VyaW91cyBvYnNlc3Npb24iLCJjYXJ0b3VjaGUgb2Ygc29saWRhcml0eSIsInNlbnRpbmVsJ3MgZXllcyIsInNwaWRlciB1bWJyYSIsInJhbmNvciIsImh5ZW5hIHVtYnJhIiwiZ2xhZGVjb3ZlciBzY291dCIsInNsaXBwZXJ5IGJvZ2xlIiwiaGFsbG93ZWQgZm91bnRhaW4iLCJicmVlZGluZyBwb29sIiwiaGludGVybGFuZCBoYXJib3IiLCJnbGFjaWFsIGZvcnRyZXNzIl0sIGRlc2M6IlN0YWNrcyBhdXJhcyBvbnRvIGEgaGV4cHJvb2YgY3JlYXR1cmUgdG8gY3JlYXRlIGFuIG92ZXJ3aGVsbWluZyB0aHJlYXQgdGhhdCBieXBhc3NlcyB0YXJnZXRlZCByZW1vdmFsLiIsIGNvdW50ZXJzOiJTd2VlcGVycywgRGV0ZW50aW9uIFNwaGVyZSwgU2hhZG93c3BlYXIuIiB9LAogIHsgbmFtZToiSnVuZCBGb29kIiwgZm9ybWF0czpbIkhpc3RvcmljIl0sIGNvbG9yczpbIkJsYWNrIiwiUmVkIiwiR3JlZW4iXSwgc3RyYXRlZ3k6Ik1pZHJhbmdlIC8gQ29tYm8iLCBjYXJkczpbIndpdGNoJ3Mgb3ZlbiIsImNhdWxkcm9uIGZhbWlsaWFyIiwidHJhaWwgb2YgY3J1bWJzIiwibWF5aGVtIGRldmlsIiwibWlkbmlnaHQgcmVhcGVyIiwid29lIHN0cmlkZXIiLCJrb3J2b2xkIGZhZS1jdXJzZWQga2luZyIsImNsYWltIHRoZSBmaXJzdGJvcm4iLCJyYXZlbm91cyBzcXVpcnJlbCIsImdpbGRlZCBnb29zZSIsIm9rbyB0aGllZiBvZiBjcm93bnMiLCJvbmNlIHVwb24gYSB0aW1lIiwib3Zlcmdyb3duIHRvbWIiLCJibG9vZCBjcnlwdCIsInN0b21waW5nIGdyb3VuZCIsImJsb29taW5nIG1hcnNoIl0sIGRlc2M6Ikxvb3BzIENhdWxkcm9uIEZhbWlsaWFyIGFuZCBXaXRjaCdzIE92ZW4gZm9yIHJlcGVhdGVkIGRyYWluIHZpYSBNYXloZW0gRGV2aWwsIGdlbmVyYXRpbmcgdmFsdWUgdGhyb3VnaCBUcmFpbCBvZiBDcnVtYnMgYW5kIEtvcnZvbGQuIiwgY291bnRlcnM6IkdyYWZkaWdnZXIncyBDYWdlLCBMZXlsaW5lIG9mIHRoZSBWb2lkLCBleGlsZS1iYXNlZCByZW1vdmFsLiIgfSwKICB7IG5hbWU6IkdvYmxpbnMiLCBmb3JtYXRzOlsiSGlzdG9yaWMiXSwgY29sb3JzOlsiUmVkIl0sIHN0cmF0ZWd5OiJBZ2dybyAvIENvbWJvIiwgY2FyZHM6WyJnb2JsaW4gY2hpZWZ0YWluIiwiZ29ibGluIHdhcmNoaWVmIiwibXV4dXMgZ29ibGluIGdyYW5kZWUiLCJrcmVua28gbW9iIGJvc3MiLCJnb2JsaW4gbWF0cm9uIiwic2tpcmsgcHJvc3BlY3RvciIsImdvYmxpbiByaW5nbGVhZGVyIiwiY29uc3BpY3VvdXMgc25vb3AiLCJtdW5pdGlvbnMgZXhwZXJ0IiwicGFzaGFsaWsgbW9ucyIsImdvYmxpbiB0cmFzaG1hc3RlciIsImNhdmVybiBvZiBzb3VscyIsInVuY2xhaW1lZCB0ZXJyaXRvcnkiLCJ0cmFuc21vZ3JpZnkiXSwgZGVzYzoiQWNjZWxlcmF0ZXMgaW50byBNdXh1cyB0byBmbG9vZCB0aGUgYm9hcmQgd2l0aCBHb2JsaW5zIGluc3RhbnRseSwgb3IgdXNlcyBLcmVua28gdG8gZXhwb25lbnRpYWxseSBtdWx0aXBseSB0b2tlbnMuIiwgY291bnRlcnM6IldyYXRoIG9mIEdvZCwgQW5nZXIgb2YgdGhlIEdvZHMsIEdyYWZkaWdnZXIncyBDYWdlLCBub24tY3JlYXR1cmUgcmVtb3ZhbC4iIH0sCiAgeyBuYW1lOiJHcnV1bCBTaGFtYW5zIiwgZm9ybWF0czpbIkhpc3RvcmljIl0sIGNvbG9yczpbIlJlZCIsIkdyZWVuIl0sIHN0cmF0ZWd5OiJBZ2dybyAvIENvbWJvIiwgY2FyZHM6WyJiZWxsb3dzYnJlYXRoIG9ncmUiLCJnb2JsaW4gYW5hcmNob21hbmNlciIsImhhcm1vbmljIHByb2RpZ3kiLCJyYWdlIGZvcmdlciIsImJ1cm5pbmctdHJlZSBlbWlzc2FyeSIsInNlYXNvbmVkIHB5cm9tYW5jZXIiLCJjb2xsZWN0ZWQgY29tcGFueSIsImNob3JkIG9mIGNhbGxpbmciLCJzdG9tcGluZyBncm91bmQiLCJyb290Ym91bmQgY3JhZyIsInJhbXVuYXAgcnVpbnMiLCJmb3Jlc3QiLCJtb3VudGFpbiJdLCBkZXNjOiJUcmliYWwgU2hhbWFuIHN5bmVyZ3kgd2hlcmUgSGFybW9uaWMgUHJvZGlneSBkb3VibGVzIGFsbCBTaGFtYW4gdHJpZ2dlcnMg4oCUIEJlbGxvd3NicmVhdGggT2dyZSBhbmQgUmFnZSBGb3JnZXIgYmVjb21lIGxldGhhbCBleHBvbmVudGlhbGx5IGZhc3QuIiwgY291bnRlcnM6IlN3ZWVwZXJzLCBHcmFmZGlnZ2VyJ3MgQ2FnZSAoZm9yIENvbXBhbnkpLCBub24tY3JlYXR1cmUgcmVtb3ZhbC4iIH0sCiAgeyBuYW1lOiJTdWx0YWkgTWlkcmFuZ2UiLCBmb3JtYXRzOlsiSGlzdG9yaWMiXSwgY29sb3JzOlsiQmx1ZSIsIkJsYWNrIiwiR3JlZW4iXSwgc3RyYXRlZ3k6Ik1pZHJhbmdlIiwgY2FyZHM6WyJ0aG91Z2h0c2VpemUiLCJmYXRhbCBwdXNoIiwidGFybW9nb3lmIiwib2tvIHRoaWVmIG9mIGNyb3ducyIsInVybyB0aXRhbiBvZiBuYXR1cmUncyB3cmF0aCIsImh5ZHJvaWQga3Jhc2lzIiwiZ3Jvd3RoIHNwaXJhbCIsIm5pc3NhIHdobyBzaGFrZXMgdGhlIHdvcmxkIiwib25jZSB1cG9uIGEgdGltZSIsIm1pc3R5IHJhaW5mb3Jlc3QiLCJ2ZXJkYW50IGNhdGFjb21icyIsImJyZWVkaW5nIHBvb2wiLCJ3YXRlcnkgZ3JhdmUiLCJvdmVyZ3Jvd24gdG9tYiIsImFzc2Fzc2luJ3MgdHJvcGh5IiwiYWJydXB0IGRlY2F5Il0sIGRlc2M6IkVsaXRlIHRocmVhdHMgbGlrZSBVcm8gYW5kIE9rbyBwYWlyZWQgd2l0aCBwcmVtaXVtIGRpc3J1cHRpb24sIGxldmVyYWdpbmcgSGlzdG9yaWMncyBleHBhbmRlZCBjYXJkIHBvb2wuIiwgY291bnRlcnM6IkhhbmQgZGlzcnVwdGlvbiwgZXhpbGUtYmFzZWQgcmVtb3ZhbCwgUmVzdCBpbiBQZWFjZSwgZW5jaGFudG1lbnQgaGF0ZSBmb3IgTmlzc2EuIiB9LAogIHsgbmFtZToiSmVza2FpIENvbnRyb2wiLCBmb3JtYXRzOlsiSGlzdG9yaWMiXSwgY29sb3JzOlsiV2hpdGUiLCJCbHVlIiwiUmVkIl0sIHN0cmF0ZWd5OiJDb250cm9sIiwgY2FyZHM6WyJ0ZWZlcmkgdGltZSByYXZlbGVyIiwidGVmZXJpIGhlcm8gb2YgZG9taW5hcmlhIiwibmFyc2V0IHBhcnRlciBvZiB2ZWlscyIsInN1cHJlbWUgdmVyZGljdCIsImFuZ2VyIG9mIHRoZSBnb2RzIiwibGlnaHRuaW5nIGhlbGl4IiwiY291bnRlcnNwZWxsIiwibWVtb3J5IGRlbHVnZSIsInJlc3QgaW4gcGVhY2UiLCJtYWdtYSBvcHVzIiwic2hhcmsgdHlwaG9vbiIsInRvcnJlbnRpYWwgZ2Vhcmh1bGsiLCJzdGVhbSB2ZW50cyIsInNhY3JlZCBmb3VuZHJ5IiwiaGFsbG93ZWQgZm91bnRhaW4iLCJjbGlmZnRvcCByZXRyZWF0Iiwic3VsZnVyIGZhbGxzIl0sIGRlc2M6IlRlZmVyaSBsb2NrcyBkb3duIG9wcG9uZW50cycgaW5zdGFudHMgd2hpbGUgc3dlZXBlcnMgY2xlYXIgYm9hcmRzOyBjYXJkIGFkdmFudGFnZSBlbmdpbmVzIGdyaW5kIHRvIHZpY3RvcnkuIiwgY291bnRlcnM6IlBsYW5lc3dhbGtlciBhZ2dyZXNzaW9uLCBEdXJlc3MsIFRob3VnaHRzZWl6ZSwgZmFzdCBjb21iby4iIH0sCiAgeyBuYW1lOiJGaXZlLUNvbG9yIE5pdi1NaXp6ZXQiLCBmb3JtYXRzOlsiSGlzdG9yaWMiXSwgY29sb3JzOlsiV2hpdGUiLCJCbHVlIiwiQmxhY2siLCJSZWQiLCJHcmVlbiJdLCBzdHJhdGVneToiTWlkcmFuZ2UgLyBDb250cm9sIiwgY2FyZHM6WyJuaXYtbWl6emV0IHJlYm9ybiIsImJyaW5nIHRvIGxpZ2h0IiwiYmluZGluZyB0aGUgb2xkIGdvZHMiLCJ0ZWZlcmkgdGltZSByYXZlbGVyIiwidGhvdWdodHNlaXplIiwidmVpbCBvZiBzdW1tZXIiLCJjb2xsZWN0ZWQgY29tcGFueSIsImdyb3d0aCBzcGlyYWwiLCJoeWRyb2lkIGtyYXNpcyIsImFzc2Fzc2luJ3MgdHJvcGh5IiwiemFnb3RoIHRyaW9tZSIsImluZGF0aGEgdHJpb21lIiwicmF1Z3JpbiB0cmlvbWUiLCJrZXRyaWEgdHJpb21lIiwic2F2YWkgdHJpb21lIiwibWFuYSBjb25mbHVlbmNlIl0sIGRlc2M6Ik5pdi1NaXp6ZXQgUmVib3JuIGRyYXdzIGEgaGFuZCBvZiBnb2xkIGNhcmRzOyBCcmluZyB0byBMaWdodCBmaW5kcyBhbnkgdHdvLWNvbG9yIGFuc3dlciBpbiB0aGUgcGlsZS4iLCBjb3VudGVyczoiR3JhdmV5YXJkIGhhdGUsIGNvdW50ZXJzcGVsbHMsIGV4aWxlIHJlbW92YWwgb24gTml2LU1penpldC4iIH0sCiAgeyBuYW1lOiJEb21haW4gUmFtcCIsIGZvcm1hdHM6WyJTdGFuZGFyZCJdLCBjb2xvcnM6WyJXaGl0ZSIsIkJsdWUiLCJCbGFjayIsIlJlZCIsIkdyZWVuIl0sIHN0cmF0ZWd5OiJSYW1wIC8gQ29udHJvbCIsIGNhcmRzOlsiYXRyYXhhIGdyYW5kIHVuaWZpZXIiLCJzdW5mYWxsIiwid3Jlbm4gYW5kIHJlYWxtYnJlYWtlciIsImxheSBkb3duIGFybXMiLCJ0ZW1wb3JhcnkgbG9ja2Rvd24iLCJpbnZhc2lvbiBvZiB6ZW5kaWthciIsInp1ciBldGVybmFsIHNjaGVtZXIiLCJ0aGUgd2FuZGVyaW5nIGVtcGVyb3IiLCJ0YWludGVkIGluZHVsZ2VuY2UiLCJ0cmlvbWUiXSwgZGVzYzoiQXNzZW1ibGVzIGFsbCBmaXZlIGxhbmQgdHlwZXMgZm9yIERvbWFpbiBzeW5lcmdpZXMsIHRoZW4gd2lucyB3aXRoIEF0cmF4YSBvciBTdW5mYWxsLiIsIGNvdW50ZXJzOiJBZ2dybyBiZWZvcmUgQXRyYXhhLCBOZWdhdGUsIER1cmVzcyBvbiBrZXkgc3BlbGxzLiIgfSwKICB7IG5hbWU6IkVzcGVyIE1pZHJhbmdlIiwgZm9ybWF0czpbIlN0YW5kYXJkIl0sIGNvbG9yczpbIldoaXRlIiwiQmx1ZSIsIkJsYWNrIl0sIHN0cmF0ZWd5OiJNaWRyYW5nZSIsIGNhcmRzOlsicmFmZmluZSBzY2hlbWluZyBzZWVyIiwic2hlb2xkcmVkIHRoZSBhcG9jYWx5cHNlIiwicmVja29uZXIgYmFua2J1c3RlciIsIndlZGRpbmcgYW5ub3VuY2VtZW50Iiwic3VuZmFsbCIsIm1ha2UgZGlzYXBwZWFyIiwiY3V0IGRvd24iLCJ3YW5kZXJpbmcgZW1wZXJvciIsImZhZGluZyBob3BlIiwiYXRyYXhhIGdyYW5kIHVuaWZpZXIiLCJyYWZmaW5lJ3MgdG93ZXIiLCJkYXJrc2xpY2sgc2hvcmVzIiwiZ29kbGVzcyBzaHJpbmUiLCJoYWxsb3dlZCBmb3VudGFpbiJdLCBkZXNjOiJSYWZmaW5lIGZpbHRlcnMgZHJhd3MgYW5kIHB1bXBzIGNyZWF0dXJlczsgU2hlb2xkcmVkIGRyYWlucyBsaWZlOyBwcmVtaXVtIGludGVyYWN0aW9uIGNvbnRyb2xzIHRoZSBib2FyZC4iLCBjb3VudGVyczoiR28gd2lkZSwgZW5jaGFudG1lbnQgcmVtb3ZhbCBmb3IgV2VkZGluZyBBbm5vdW5jZW1lbnQsIGNvdW50ZXJzcGVsbHMuIiB9LAogIHsgbmFtZToiTW9uby1SZWQgQWdncm8iLCBmb3JtYXRzOlsiU3RhbmRhcmQiLCJQaW9uZWVyIiwiSGlzdG9yaWMiXSwgY29sb3JzOlsiUmVkIl0sIHN0cmF0ZWd5OiJBZ2dybyIsIGNhcmRzOlsibW9uYXN0ZXJ5IHN3aWZ0c3BlYXIiLCJrdW1hbm8gZmFjZXMga2Fra2F6YW4iLCJ2b2xkYXJlbiBlcGljdXJlIiwiZmVsZG9uIHJlZm9yZ2VkIiwiYmxvb2R0aGlyc3R5IGFkdmVyc2FyeSIsInBsYXkgd2l0aCBmaXJlIiwic3RyYW5nbGUiLCJwaG9lbml4IGNoaWNrIiwid2FyYm9zcyBnb2JsaW4iLCJzaGl2YW4gZGV2YXN0YXRvciIsInNxdWVlIGR1YmlvdXMgbW9uYXJjaCIsInJlY2tsZXNzIGltcHVsc2UiLCJtaXNocmEncyBmb3VuZHJ5IiwicmFtdW5hcCBydWlucyIsInN1bnNjb3JjaGVkIGRlc2VydCJdLCBkZXNjOiJCbGF6aW5nLWZhc3QgcmVkIGFnZ3JvIGZvY3VzZWQgb24gMjAgZGFtYWdlIGJlZm9yZSB0aGUgb3Bwb25lbnQgc3RhYmlsaXplcy4iLCBjb3VudGVyczoiTGlmZSBnYWluLCBibG9ja2Vycywgc3dlZXBlcnMsIExleWxpbmUgb2YgU2FuY3RpdHkuIiB9LAogIHsgbmFtZToiVGhyYXNpb3MgJiBUeW1uYSAoY0VESCkiLCBmb3JtYXRzOlsiQ29tbWFuZGVyIl0sIGNvbG9yczpbIldoaXRlIiwiQmx1ZSIsIkJsYWNrIiwiR3JlZW4iXSwgc3RyYXRlZ3k6IkNvbWJvIiwgY2FyZHM6WyJ0aHJhc2lvcyB0cml0b24gaGVybyIsInR5bW5hIHRoZSB3ZWF2ZXIiLCJkZW1vbmljIGNvbnN1bHRhdGlvbiIsInRoYXNzYSdzIG9yYWNsZSIsImFkIG5hdXNlYW0iLCJuZWNyb3BvdGVuY2UiLCJmb3JjZSBvZiB3aWxsIiwibWFuYSBkcmFpbiIsIm1veCBkaWFtb25kIiwiY2hyb21lIG1veCIsIm1hbmEgY3J5cHQiLCJzb2wgcmluZyIsImJpcmRzIG9mIHBhcmFkaXNlIiwibm9ibGUgaGllcmFyY2giLCJteXN0aWNhbCB0dXRvciIsInZhbXBpcmljIHR1dG9yIiwicmh5c3RpYyBzdHVkeSIsInNtb3RoZXJpbmcgdGl0aGUiXSwgZGVzYzoiQ29tcGV0aXRpdmUgRURIIGNvbWJvIHBhaXJpbmcgVGhyYXNpb3MncyBtYW5hIHNpbmsgd2l0aCBUeW1uYSdzIGNhcmQgZHJhdywgd2lubmluZyB0aHJvdWdoIENvbnN1bHRhdGlvbitPcmFjbGUgb3IgQWQgTmF1c2VhbS4iLCBjb3VudGVyczoiU3RheCAoUnVsZSBvZiBMYXcsIFNwaGVyZSBvZiBSZXNpc3RhbmNlKSwgQ3Vyc2VkIFRvdGVtLCBmYXN0IGFnZ3JvLiIgfSwKXTsKCi8qIOKUgOKUgCBoZWxwZXJzIOKUgOKUgCAqLwpmdW5jdGlvbiBnZXRBcmNoZXR5cGVzKCkgewogIHJldHVybiBzZWxlY3RlZEZvcm1hdCA9PT0gIkFsbCIgPyBBTExfQVJDSEVUWVBFUyA6IEFMTF9BUkNIRVRZUEVTLmZpbHRlcihhID0+IGEuZm9ybWF0cy5pbmNsdWRlcyhzZWxlY3RlZEZvcm1hdCkpOwp9CgpmdW5jdGlvbiBzY29yZUFyY2hldHlwZShhLCBjYXJkTGlzdCkgewogIGNvbnN0IGxjID0gY2FyZExpc3QubWFwKGMgPT4gYy50b0xvd2VyQ2FzZSgpLnRyaW0oKSk7CiAgbGV0IHNjb3JlID0gMCwgbWF0Y2hlcyA9IFtdOwogIGZvciAoY29uc3QgY2FyZCBvZiBsYykgewogICAgZm9yIChjb25zdCBhYyBvZiBhLmNhcmRzKSB7CiAgICAgIGlmIChhYy5pbmNsdWRlcyhjYXJkKSB8fCBjYXJkLmluY2x1ZGVzKGFjLnNwbGl0KCcgJylbMF0pKSB7IHNjb3JlKys7IGlmICghbWF0Y2hlcy5pbmNsdWRlcyhhYykpIG1hdGNoZXMucHVzaChhYyk7IGJyZWFrOyB9CiAgICB9CiAgfQogIHJldHVybiB7IHNjb3JlLCBtYXRjaGVzLCBjb25maWRlbmNlOiBNYXRoLm1pbigxMDAsIE1hdGgucm91bmQoKHNjb3JlIC8gTWF0aC5tYXgobGMubGVuZ3RoLDEpKSAqIDEwMCkpIH07Cn0KCmZ1bmN0aW9uIGJhZGdlRm9yKGMpIHsKICByZXR1cm4gYz09PSdSZWQnPydiYWRnZS1yZWQnOmM9PT0nQmx1ZSc/J2JhZGdlLWJsdWUnOmM9PT0nR3JlZW4nPydiYWRnZS10ZWFsJzpjPT09J1doaXRlJz8nYmFkZ2UtZ3JheSc6Yz09PSdCbGFjayc/J2JhZGdlLWdyYXknOidiYWRnZS1hbWJlcic7Cn0KCmZ1bmN0aW9uIGNvbmZDbGFzcyhwY3QpIHsgcmV0dXJuIHBjdCA+PSA2NSA/ICdjb25mLWhpZ2gnIDogcGN0ID49IDM1ID8gJ2NvbmYtbWlkJyA6ICdjb25mLWxvdyc7IH0KZnVuY3Rpb24gY29uZkNvbG9yKHBjdCkgeyByZXR1cm4gcGN0ID49IDY1ID8gJyMxRDlFNzUnIDogcGN0ID49IDM1ID8gJyM3Rjc3REQnIDogJyM4ODg3ODAnOyB9CgpmdW5jdGlvbiBnZXRBcGlLZXkoKSB7IHJldHVybiAoZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2FwaS1rZXknKS52YWx1ZSB8fCAnJykudHJpbSgpOyB9Cgphc3luYyBmdW5jdGlvbiBjYWxsQ2xhdWRlKHN5c3RlbSwgdXNlck1zZykgewogIGNvbnN0IGtleSA9IGdldEFwaUtleSgpOwogIGlmICgha2V5KSByZXR1cm4gbnVsbDsKICBjb25zdCByZXNwID0gYXdhaXQgZmV0Y2goImh0dHBzOi8vYXBpLmFudGhyb3BpYy5jb20vdjEvbWVzc2FnZXMiLCB7CiAgICBtZXRob2Q6ICJQT1NUIiwKICAgIGhlYWRlcnM6IHsKICAgICAgIkNvbnRlbnQtVHlwZSI6ICJhcHBsaWNhdGlvbi9qc29uIiwKICAgICAgIngtYXBpLWtleSI6IGtleSwKICAgICAgImFudGhyb3BpYy12ZXJzaW9uIjogIjIwMjMtMDYtMDEiLAogICAgICAiYW50aHJvcGljLWRhbmdlcm91cy1kaXJlY3QtYnJvd3Nlci1hY2Nlc3MiOiAidHJ1ZSIKICAgIH0sCiAgICBib2R5OiBKU09OLnN0cmluZ2lmeSh7IG1vZGVsOiAiY2xhdWRlLXNvbm5ldC00LTIwMjUwNTE0IiwgbWF4X3Rva2VuczogMTAwMCwgc3lzdGVtLCBtZXNzYWdlczogW3tyb2xlOiJ1c2VyIixjb250ZW50OnVzZXJNc2d9XSB9KQogIH0pOwogIGNvbnN0IGRhdGEgPSBhd2FpdCByZXNwLmpzb24oKTsKICBjb25zdCB0ZXh0ID0gKGRhdGEuY29udGVudHx8W10pLmZpbHRlcihiPT5iLnR5cGU9PT0ndGV4dCcpLm1hcChiPT5iLnRleHQpLmpvaW4oJycpOwogIHJldHVybiBKU09OLnBhcnNlKHRleHQucmVwbGFjZSgvYGBganNvbnxgYGAvZywnJykudHJpbSgpKTsKfQoKLyog4pSA4pSAIGZvcm1hdCBzZWxlY3RvciDilIDilIAgKi8KZnVuY3Rpb24gcmVuZGVyRm9ybWF0cygpIHsKICBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnZm9ybWF0LXJvdycpLmlubmVySFRNTCA9IEZPUk1BVFMubWFwKGYgPT4KICAgIGA8YnV0dG9uIGNsYXNzPSJmbXQtYnRuJHtmPT09c2VsZWN0ZWRGb3JtYXQ/JyBhY3RpdmUnOicnfSIgb25jbGljaz0ic2VsZWN0Rm9ybWF0KCcke2Z9JykiPiR7Zn08L2J1dHRvbj5gCiAgKS5qb2luKCcnKTsKfQpmdW5jdGlvbiBzZWxlY3RGb3JtYXQoZikgeyBzZWxlY3RlZEZvcm1hdCA9IGY7IHJlbmRlckZvcm1hdHMoKTsgfQoKLyog4pSA4pSAIGRlY2sgbG9hZGluZyDilIDilIAgKi8KZnVuY3Rpb24gcGFyc2VEZWNrKCkgewogIGNvbnN0IHJhdyA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdteS1kZWNrLWlucHV0JykudmFsdWUudHJpbSgpOwogIGNvbnN0IGNhcmRzID0gW107CiAgZm9yIChjb25zdCBsaW5lIG9mIHJhdy5zcGxpdCgnXG4nKSkgewogICAgY29uc3QgdCA9IGxpbmUudHJpbSgpOwogICAgaWYgKCF0IHx8IHQuc3RhcnRzV2l0aCgnLy8nKSB8fCB0LnRvTG93ZXJDYXNlKCk9PT0nZGVjaycgfHwgdC50b0xvd2VyQ2FzZSgpPT09J3NpZGVib2FyZCcpIGNvbnRpbnVlOwogICAgY29uc3QgbSA9IHQubWF0Y2goL14oXGQrKVxzKyguKz8pKFxzK1woLiopPyQvKTsKICAgIGlmIChtKSB7IGNvbnN0IHF0eT1wYXJzZUludChtWzFdKTsgY29uc3QgbmFtZT1tWzJdLnRyaW0oKTsgZm9yKGxldCBpPTA7aTxxdHk7aSsrKSBjYXJkcy5wdXNoKG5hbWUpOyB9CiAgfQogIG15RGVjayA9IHsgY2FyZHMgfTsKICBjb25zdCBlbCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdkZWNrLXN0YXR1cycpOwogIGVsLmlubmVySFRNTCA9IGNhcmRzLmxlbmd0aAogICAgPyBgPGRpdiBjbGFzcz0iZGVjay1sb2FkZWQiPjxpIGNsYXNzPSJ0aSB0aS1jaGVjayIgc3R5bGU9ImZvbnQtc2l6ZToxM3B4Ij48L2k+ICR7Y2FyZHMubGVuZ3RofSBjYXJkcyAoJHtbLi4ubmV3IFNldChjYXJkcyldLmxlbmd0aH0gdW5pcXVlKTwvZGl2PmAKICAgIDogYDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXRleHQtc2Vjb25kYXJ5KTttYXJnaW4tdG9wOjVweDsiPkNvdWxkbid0IHBhcnNlIOKAlCB1c2UgQXJlbmEgZXhwb3J0IGZvcm1hdC48L2Rpdj5gOwp9CgovKiDilIDilIAgb3Bwb25lbnQgY2FyZHMg4pSA4pSAICovCmZ1bmN0aW9uIGFkZENhcmQoKSB7CiAgY29uc3QgaW5wdXQgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnY2FyZC1pbnB1dCcpOwogIGNvbnN0IHZhbCA9IGlucHV0LnZhbHVlLnRyaW0oKTsKICBpZiAoIXZhbCkgcmV0dXJuOwogIG9wcG9uZW50Q2FyZHMucHVzaCh2YWwpOwogIGlucHV0LnZhbHVlID0gJyc7CiAgcmVuZGVyVGFncygpOwogIHRyaWdnZXJBbmFseXNpcyh2YWwpOwp9CgoKCmZ1bmN0aW9uIHJlbW92ZUNhcmQoaSkgeyBvcHBvbmVudENhcmRzLnNwbGljZShpLCAxKTsgcmVuZGVyVGFncygpOyB9CgpmdW5jdGlvbiByZW5kZXJUYWdzKCkgewogIGNvbnN0IGVsID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ3RhZ3MnKTsKICBlbC5pbm5lckhUTUwgPSBvcHBvbmVudENhcmRzLmxlbmd0aAogICAgPyBvcHBvbmVudENhcmRzLm1hcCgoYyxpKSA9PiBgPGRpdiBjbGFzcz0idGFnIj4ke2N9PGJ1dHRvbiBvbmNsaWNrPSJyZW1vdmVDYXJkKCR7aX0pIj48aSBjbGFzcz0idGkgdGkteCIgc3R5bGU9ImZvbnQtc2l6ZToxMnB4Ij48L2k+PC9idXR0b24+PC9kaXY+YCkuam9pbignJykKICAgIDogJzxzcGFuIGNsYXNzPSJlbXB0eS1oaW50Ij5UeXBlIGEgY2FyZCBhbmQgcHJlc3MgRW50ZXI8L3NwYW4+JzsKfQoKLyog4pSA4pSAIGFuYWx5c2lzIGVuZ2luZSDilIDilIAgKi8KYXN5bmMgZnVuY3Rpb24gdHJpZ2dlckFuYWx5c2lzKG5ld0NhcmQpIHsKICBpZiAoYW5hbHl6aW5nKSB7IGFuYWx5c2lzUXVldWUgPSBuZXdDYXJkOyByZXR1cm47IH0KICBhbmFseXppbmcgPSB0cnVlOwogIGF3YWl0IHJ1bkFuYWx5c2lzKG5ld0NhcmQpOwogIGFuYWx5emluZyA9IGZhbHNlOwogIGlmIChhbmFseXNpc1F1ZXVlKSB7CiAgICBjb25zdCBxdWV1ZWQgPSBhbmFseXNpc1F1ZXVlOwogICAgYW5hbHlzaXNRdWV1ZSA9IGZhbHNlOwogICAgYXdhaXQgdHJpZ2dlckFuYWx5c2lzKHF1ZXVlZCk7CiAgfQp9Cgphc3luYyBmdW5jdGlvbiBydW5BbmFseXNpcyhuZXdDYXJkKSB7CiAgY29uc3QgcG9vbCA9IGdldEFyY2hldHlwZXMoKTsKICBjb25zdCBzY29yZWQgPSBwb29sCiAgICAubWFwKGEgPT4gKHsuLi5hLCAuLi5zY29yZUFyY2hldHlwZShhLCBvcHBvbmVudENhcmRzKX0pKQogICAgLmZpbHRlcihhID0+IGEuc2NvcmUgPiAwKQogICAgLnNvcnQoKGEsYikgPT4gYi5zY29yZSAtIGEuc2NvcmUpOwoKICBsZXQgYXJjaGV0eXBlID0gbnVsbDsKICBsZXQgdXNlZEFJID0gZmFsc2U7CgogIGlmIChzY29yZWQubGVuZ3RoICYmIHNjb3JlZFswXS5jb25maWRlbmNlID49IDIwKSB7CiAgICBhcmNoZXR5cGUgPSBzY29yZWRbMF07CiAgICBhcmNoZXR5cGUub3RoZXJzID0gc2NvcmVkLnNsaWNlKDEsNCk7CiAgfSBlbHNlIHsKICAgIGNvbnN0IGtleSA9IGdldEFwaUtleSgpOwogICAgaWYgKGtleSAmJiBvcHBvbmVudENhcmRzLmxlbmd0aCA+PSAyKSB7CiAgICAgIGNvbnN0IGZtdEN0eCA9IHNlbGVjdGVkRm9ybWF0PT09J0FsbCcgPyAnYW55IE1URyBmb3JtYXQnIDogc2VsZWN0ZWRGb3JtYXQ7CiAgICAgIHRyeSB7CiAgICAgICAgYXJjaGV0eXBlID0gYXdhaXQgY2FsbENsYXVkZSgKICAgICAgICAgIGBZb3UgYXJlIGFuIGV4cGVydCBNVEcgYW5hbHlzdC4gSWRlbnRpZnkgdGhlIGFyY2hldHlwZSBmcm9tIG9wcG9uZW50IGNhcmRzIGluICR7Zm10Q3R4fS4gUmVzcG9uZCBPTkxZIHdpdGggdmFsaWQgSlNPTiAobm8gbWFya2Rvd24pOiB7bmFtZSwgZm9ybWF0LCBzdHJhdGVneSwgY29sb3JzIChhcnJheSksIGNvbmZpZGVuY2UgKDAtMTAwKSwgZGVzYyAoMiBzZW50ZW5jZXMpLCBjb3VudGVycywgbWF0Y2hlcyAoYXJyYXkpfWAsCiAgICAgICAgICBgT3Bwb25lbnQgY2FyZHM6ICR7b3Bwb25lbnRDYXJkcy5qb2luKCcsICcpfWAKICAgICAgICApOwogICAgICAgIHVzZWRBSSA9IHRydWU7CiAgICAgIH0gY2F0Y2goZSkge30KICAgIH0KICB9CgogIGlmICghYXJjaGV0eXBlKSByZXR1cm47CgogIC8vIGZldGNoIGFkdmljZSBpZiBkZWNrIGxvYWRlZCArIGtleSBwcmVzZW50CiAgbGV0IGFkdmljZSA9IG51bGw7CiAgaWYgKG15RGVjay5jYXJkcy5sZW5ndGggJiYgZ2V0QXBpS2V5KCkpIHsKICAgIGNvbnN0IGZtdEN0eCA9IHNlbGVjdGVkRm9ybWF0PT09J0FsbCcgPyAnYW55IE1URyBmb3JtYXQnIDogc2VsZWN0ZWRGb3JtYXQ7CiAgICB0cnkgewogICAgICBhZHZpY2UgPSBhd2FpdCBjYWxsQ2xhdWRlKAogICAgICAgIGBZb3UgYXJlIGFuIGV4cGVydCBNVEcgZ2FtZXBsYXkgYWR2aXNvci4gUHJvdmlkZSB0YWlsb3JlZCBpbi1nYW1lIGFkdmljZS4gUmVzcG9uZCBPTkxZIHdpdGggdmFsaWQgSlNPTiAobm8gbWFya2Rvd24pOiB7dGlwczpbe3RpdGxlLGFkdmljZX1dLCBrZXlfdGhyZWF0czpbc3RyaW5nXSwgd2luX2NvbmRpdGlvbjpzdHJpbmd9LiAzIGNvbmNpc2UgdGlwcyByZWZlcmVuY2luZyBzcGVjaWZpYyBjYXJkcyBpbiB0aGUgcGxheWVyJ3MgZGVjay5gLAogICAgICAgIGBGb3JtYXQ6ICR7Zm10Q3R4fS4gT3Bwb25lbnQ6ICR7YXJjaGV0eXBlLm5hbWV9ICgke2FyY2hldHlwZS5zdHJhdGVneX0pLiBDYXJkcyBzZWVuOiAke29wcG9uZW50Q2FyZHMuam9pbignLCAnKX0uIE15IGRlY2s6ICR7Wy4uLm5ldyBTZXQobXlEZWNrLmNhcmRzKV0uam9pbignLCAnKX1gCiAgICAgICk7CiAgICB9IGNhdGNoKGUpIHt9CiAgfQoKICBhZGRGZWVkRW50cnkoeyBhcmNoZXR5cGUsIHVzZWRBSSwgbmV3Q2FyZCwgYWR2aWNlLCBjYXJkQ291bnQ6IG9wcG9uZW50Q2FyZHMubGVuZ3RoIH0pOwp9CgovKiDilIDilIAgZmVlZCByZW5kZXJpbmcg4pSA4pSAICovCmZ1bmN0aW9uIGFkZEZlZWRFbnRyeSh7IGFyY2hldHlwZSwgdXNlZEFJLCBuZXdDYXJkLCBhZHZpY2UsIGNhcmRDb3VudCB9KSB7CiAgY29uc3Qgc2VxID0gZmVlZEVudHJpZXMubGVuZ3RoICsgMTsKICBjb25zdCBjb25mID0gTWF0aC5yb3VuZChhcmNoZXR5cGUuY29uZmlkZW5jZSB8fCAwKTsKICBjb25zdCBjb2xvcnMgPSBhcmNoZXR5cGUuY29sb3JzIHx8IFtdOwogIGNvbnN0IG90aGVycyA9IGFyY2hldHlwZS5vdGhlcnMgfHwgW107CgogIGNvbnN0IG90aGVyc0hUTUwgPSBvdGhlcnMubGVuZ3RoCiAgICA/IGA8ZGl2IGNsYXNzPSJvdGhlcnMtcm93Ij4ke290aGVycy5tYXAobz0+YDxzcGFuIGNsYXNzPSJvdGhlci1waWxsIj4ke28ubmFtZX0gKCR7by5zY29yZX0pPC9zcGFuPmApLmpvaW4oJycpfTwvZGl2PmAKICAgIDogJyc7CgogIGNvbnN0IGFkdmljZUhUTUwgPSBhZHZpY2UgPyBgCiAgICA8ZGl2IGNsYXNzPSJhZHZpY2UtYmxvY2siPgogICAgICA8ZGl2IGNsYXNzPSJzZWN0aW9uLWxhYmVsIiBzdHlsZT0ibWFyZ2luLWJvdHRvbTo1cHg7Ij5Zb3VyIGdhbWUgcGxhbjwvZGl2PgogICAgICA8ZGl2IGNsYXNzPSJhZHZpY2UtdGlwIiBzdHlsZT0ibWFyZ2luLWJvdHRvbTo2cHg7Ij4ke2FkdmljZS53aW5fY29uZGl0aW9ufHwnJ308L2Rpdj4KICAgICAgJHsoYWR2aWNlLnRpcHN8fFtdKS5tYXAodD0+YDxkaXYgY2xhc3M9ImFkdmljZS10aXAiPjxzdHJvbmc+JHt0LnRpdGxlfTo8L3N0cm9uZz4gJHt0LmFkdmljZX08L2Rpdj5gKS5qb2luKCcnKX0KICAgIDwvZGl2PmAgOiAnJzsKCiAgY29uc3QgZW50cnlJZCA9IGBlbnRyeS0ke3NlcX1gOwogIGNvbnN0IGJvZHlJZCA9IGBib2R5LSR7c2VxfWA7CgogIGNvbnN0IGh0bWwgPSBgCiAgICA8ZGl2IGNsYXNzPSJmZWVkLWVudHJ5IiBpZD0iJHtlbnRyeUlkfSI+CiAgICAgIDxkaXYgY2xhc3M9ImVudHJ5LWhlYWRlciIgb25jbGljaz0idG9nZ2xlRW50cnkoJyR7Ym9keUlkfScsIHRoaXMpIj4KICAgICAgICA8ZGl2IGNsYXNzPSJlbnRyeS1oZWFkZXItbGVmdCI+CiAgICAgICAgICA8c3BhbiBjbGFzcz0iZW50cnktc2VxIj4jJHtzZXF9PC9zcGFuPgogICAgICAgICAgPGRpdj4KICAgICAgICAgICAgPGRpdiBjbGFzcz0iZW50cnktbmFtZSI+JHthcmNoZXR5cGUubmFtZX0gPHNwYW4gY2xhc3M9ImVudHJ5LW5ldy10YWciPiske25ld0NhcmR9PC9zcGFuPjwvZGl2PgogICAgICAgICAgICA8ZGl2IGNsYXNzPSJlbnRyeS1zdHJhdGVneSI+JHthcmNoZXR5cGUuZm9ybWF0cyA/IGFyY2hldHlwZS5mb3JtYXRzLmpvaW4oJyAvICcpIDogYXJjaGV0eXBlLmZvcm1hdHx8Jyd9IMK3ICR7YXJjaGV0eXBlLnN0cmF0ZWd5fTwvZGl2PgogICAgICAgICAgPC9kaXY+CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iZW50cnktcmlnaHQiPgogICAgICAgICAgPHNwYW4gY2xhc3M9ImNvbmYtcGlsbCAke2NvbmZDbGFzcyhjb25mKX0iPiR7Y29uZn0lPC9zcGFuPgogICAgICAgICAgPGkgY2xhc3M9InRpIHRpLWNoZXZyb24tZG93biBlbnRyeS1jaGV2cm9uIG9wZW4iPjwvaT4KICAgICAgICA8L2Rpdj4KICAgICAgPC9kaXY+CiAgICAgIDxkaXYgY2xhc3M9ImVudHJ5LWJvZHkgb3BlbiIgaWQ9IiR7Ym9keUlkfSI+CiAgICAgICAgPGRpdiBjbGFzcz0ibWV0aG9kLWJhZGdlICR7dXNlZEFJPydtZXRob2QtYWknOidtZXRob2QtcGF0dGVybid9Ij4KICAgICAgICAgIDxpIGNsYXNzPSJ0aSB0aS0ke3VzZWRBST8nc3BhcmtsZXMnOidkYXRhYmFzZSd9IiBzdHlsZT0iZm9udC1zaXplOjEwcHgiPjwvaT4KICAgICAgICAgICR7dXNlZEFJID8gJ0NsYXVkZSBBSScgOiAnUGF0dGVybiBtYXRjaCd9IMK3ICR7Y2FyZENvdW50fSBjYXJkJHtjYXJkQ291bnQhPT0xPydzJzonJ30gc2VlbgogICAgICAgIDwvZGl2PgogICAgICAgIDxkaXYgY2xhc3M9ImNvbmYtYmFyLXdyYXAiPgogICAgICAgICAgPGRpdiBjbGFzcz0iY29uZi1iYXIiPjxkaXYgY2xhc3M9ImNvbmYtYmFyLWZpbGwiIHN0eWxlPSJ3aWR0aDoke2NvbmZ9JTtiYWNrZ3JvdW5kOiR7Y29uZkNvbG9yKGNvbmYpfSI+PC9kaXY+PC9kaXY+CiAgICAgICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTBweDtjb2xvcjp2YXIoLS10ZXh0LXNlY29uZGFyeSkiPiR7Y29uZn0lIGNvbmZpZGVuY2U8L2Rpdj4KICAgICAgICA8L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJiYWRnZXMiPgogICAgICAgICAgJHtjb2xvcnMubWFwKGM9PmA8c3BhbiBjbGFzcz0iYmFkZ2UgJHtiYWRnZUZvcihjKX0iPiR7Y308L3NwYW4+YCkuam9pbignJyl9CiAgICAgICAgICAke2FyY2hldHlwZS5tYXRjaGVzID8gYDxzcGFuIGNsYXNzPSJiYWRnZSBiYWRnZS10ZWFsIj4ke2FyY2hldHlwZS5tYXRjaGVzLmxlbmd0aH0gbWF0Y2hlZDwvc3Bhbj5gIDogJyd9CiAgICAgICAgPC9kaXY+CiAgICAgICAgPGRpdiBjbGFzcz0iZW50cnktZGVzYyI+JHthcmNoZXR5cGUuZGVzY3x8YXJjaGV0eXBlLmRlc2NyaXB0aW9ufHwnJ308L2Rpdj4KICAgICAgICA8ZGl2IGNsYXNzPSJlbnRyeS1jb3VudGVycyI+PHN0cm9uZz5Db3VudGVyOjwvc3Ryb25nPiAke2FyY2hldHlwZS5jb3VudGVyc3x8Jyd9PC9kaXY+CiAgICAgICAgJHtvdGhlcnNIVE1MfQogICAgICAgICR7YWR2aWNlSFRNTH0KICAgICAgPC9kaXY+CiAgICA8L2Rpdj5gOwoKICAvLyBjb2xsYXBzZSBhbGwgcHJldmlvdXMgZW50cmllcwogIGRvY3VtZW50LnF1ZXJ5U2VsZWN0b3JBbGwoJy5lbnRyeS1ib2R5Lm9wZW4nKS5mb3JFYWNoKGIgPT4gewogICAgYi5jbGFzc0xpc3QucmVtb3ZlKCdvcGVuJyk7CiAgICBjb25zdCBoZWFkZXIgPSBiLnByZXZpb3VzRWxlbWVudFNpYmxpbmc7CiAgICBpZiAoaGVhZGVyKSB7IGNvbnN0IGNoZXYgPSBoZWFkZXIucXVlcnlTZWxlY3RvcignLmVudHJ5LWNoZXZyb24nKTsgaWYoY2hldikgY2hldi5jbGFzc0xpc3QucmVtb3ZlKCdvcGVuJyk7IH0KICB9KTsKCiAgLy8gcmVtb3ZlIGVtcHR5IHN0YXRlCiAgY29uc3QgZW1wdHkgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnZmVlZC1lbXB0eScpOwogIGlmIChlbXB0eSkgZW1wdHkucmVtb3ZlKCk7CgogIGNvbnN0IGZlZWQgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnZmVlZCcpOwogIGNvbnN0IGRpdiA9IGRvY3VtZW50LmNyZWF0ZUVsZW1lbnQoJ2RpdicpOwogIGRpdi5pbm5lckhUTUwgPSBodG1sOwogIGZlZWQuaW5zZXJ0QmVmb3JlKGRpdi5maXJzdEVsZW1lbnRDaGlsZCwgZmVlZC5maXJzdENoaWxkKTsKCiAgZmVlZEVudHJpZXMucHVzaCh7IHNlcSwgbmFtZTogYXJjaGV0eXBlLm5hbWUsIGNvbmYgfSk7CiAgdXBkYXRlVGltZWxpbmUoKTsKfQoKZnVuY3Rpb24gdG9nZ2xlRW50cnkoYm9keUlkLCBoZWFkZXIpIHsKICBjb25zdCBib2R5ID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoYm9keUlkKTsKICBjb25zdCBjaGV2ID0gaGVhZGVyLnF1ZXJ5U2VsZWN0b3IoJy5lbnRyeS1jaGV2cm9uJyk7CiAgY29uc3QgaXNPcGVuID0gYm9keS5jbGFzc0xpc3QuY29udGFpbnMoJ29wZW4nKTsKICBib2R5LmNsYXNzTGlzdC50b2dnbGUoJ29wZW4nLCAhaXNPcGVuKTsKICBjaGV2LmNsYXNzTGlzdC50b2dnbGUoJ29wZW4nLCAhaXNPcGVuKTsKfQoKZnVuY3Rpb24gdXBkYXRlVGltZWxpbmUoKSB7CiAgY29uc3QgcGFuZWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgndGltZWxpbmUtcGFuZWwnKTsKICBjb25zdCB0bCA9IGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCd0aW1lbGluZScpOwogIGlmICghZmVlZEVudHJpZXMubGVuZ3RoKSB7IHBhbmVsLnN0eWxlLmRpc3BsYXk9J25vbmUnOyByZXR1cm47IH0KICBwYW5lbC5zdHlsZS5kaXNwbGF5ID0gJ2Jsb2NrJzsKICB0bC5pbm5lckhUTUwgPSBbLi4uZmVlZEVudHJpZXNdLnJldmVyc2UoKS5tYXAoZSA9PiBgCiAgICA8ZGl2IGNsYXNzPSJ0bC1yb3ciPgogICAgICA8c3BhbiBjbGFzcz0idGwtc2VxIj4jJHtlLnNlcX08L3NwYW4+CiAgICAgIDxzcGFuIGNsYXNzPSJ0bC1uYW1lIj4ke2UubmFtZX08L3NwYW4+CiAgICAgIDxkaXYgY2xhc3M9InRsLWJhci1iZyI+PGRpdiBjbGFzcz0idGwtYmFyLWZpbGwiIHN0eWxlPSJ3aWR0aDoke2UuY29uZn0lO2JhY2tncm91bmQ6JHtjb25mQ29sb3IoZS5jb25mKX0iPjwvZGl2PjwvZGl2PgogICAgICA8c3BhbiBjbGFzcz0idGwtcGN0Ij4ke2UuY29uZn0lPC9zcGFuPgogICAgPC9kaXY+YCkuam9pbignJyk7Cn0KCi8qIOKUgOKUgCBjb250cm9scyDilIDilIAgKi8KZnVuY3Rpb24gY2xlYXJBbGwoKSB7IG9wcG9uZW50Q2FyZHMgPSBbXTsgcmVuZGVyVGFncygpOyB9CmZ1bmN0aW9uIGNsZWFyRmVlZCgpIHsKICBmZWVkRW50cmllcyA9IFtdOwogIGNvbnN0IGZlZWQgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnZmVlZCcpOwogIGZlZWQuaW5uZXJIVE1MID0gJzxkaXYgY2xhc3M9ImZlZWQtZW1wdHkiIGlkPSJmZWVkLWVtcHR5Ij48aSBjbGFzcz0idGkgdGktY2FyZHMiIHN0eWxlPSJmb250LXNpemU6MzJweDtkaXNwbGF5OmJsb2NrO21hcmdpbi1ib3R0b206OHB4O2NvbG9yOnZhcigtLXRleHQtc2Vjb25kYXJ5KSI+PC9pPkFkZCBvcHBvbmVudCBjYXJkcyB0byBzdGFydCByZWFkaW5nIHRoZWlyIGRlY2s8L2Rpdj4nOwogIHVwZGF0ZVRpbWVsaW5lKCk7Cn0KZnVuY3Rpb24gbmV3R2FtZSgpIHsgY2xlYXJBbGwoKTsgY2xlYXJGZWVkKCk7IH0KCnJlbmRlckZvcm1hdHMoKTsKcmVuZGVyVGFncygpOwoKLyog4pSA4pSAIEFyZW5hIHdhdGNoZXIgcG9sbGluZyDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIDilIAgKi8KY29uc3QgV0FUQ0hFUl9VUkwgPSAiIjsKbGV0IHdhdGNoZXJDb25uZWN0ZWQgPSBmYWxzZTsKbGV0IGxhc3RTZWVuQ2FyZHMgPSBbXTsKbGV0IGxhc3RHYW1lU3RhdGUgPSB7fTsKCmZ1bmN0aW9uIHVwZGF0ZVdhdGNoZXJTdGF0dXMoY29ubmVjdGVkKSB7CiAgY29uc3QgZWwgPSBkb2N1bWVudC5nZXRFbGVtZW50QnlJZCgnd2F0Y2hlci1zdGF0dXMnKTsKICBpZiAoIWVsKSByZXR1cm47CiAgaWYgKGNvbm5lY3RlZCAmJiAhd2F0Y2hlckNvbm5lY3RlZCkgewogICAgZWwuaW5uZXJIVE1MID0gJzxzcGFuIHN0eWxlPSJjb2xvcjojMUQ5RTc1Ij48aSBjbGFzcz0idGkgdGktd2lmaSIgc3R5bGU9ImZvbnQtc2l6ZToxM3B4O3ZlcnRpY2FsLWFsaWduOi0ycHgiPjwvaT4gQXJlbmEgd2F0Y2hlciBjb25uZWN0ZWQ8L3NwYW4+JzsKICB9IGVsc2UgaWYgKCFjb25uZWN0ZWQpIHsKICAgIGVsLmlubmVySFRNTCA9ICc8c3BhbiBzdHlsZT0iY29sb3I6dmFyKC0tdGV4dC1zZWNvbmRhcnkpIj48aSBjbGFzcz0idGkgdGktd2lmaS1vZmYiIHN0eWxlPSJmb250LXNpemU6MTNweDt2ZXJ0aWNhbC1hbGlnbjotMnB4Ij48L2k+IEFyZW5hIHdhdGNoZXIgb2ZmbGluZSDigJQgPGEgaHJlZj0iaHR0cHM6Ly9naXRodWIuY29tL2FiYWVrNS9tdGctYXJjaGV0eXBlLWRldGVjdG9yI3NldHVwIiB0YXJnZXQ9Il9ibGFuayIgc3R5bGU9ImNvbG9yOnZhcigtLWFjY2VudC1hZHZpY2UpIj5zdGFydCB3YXRjaGVyLnB5PC9hPjwvc3Bhbj4nOwogIH0KICB3YXRjaGVyQ29ubmVjdGVkID0gY29ubmVjdGVkOwp9CgpmdW5jdGlvbiByZW5kZXJHYW1lU3RhdGUocykgewogIGNvbnN0IGVsID0gZG9jdW1lbnQuZ2V0RWxlbWVudEJ5SWQoJ2dhbWUtc3RhdGUtcGFuZWwnKTsKICBpZiAoIWVsKSByZXR1cm47CiAgaWYgKCFzLnBoYXNlICYmICFzLnR1cm4pIHsgZWwuc3R5bGUuZGlzcGxheT0nbm9uZSc7IHJldHVybjsgfQogIGVsLnN0eWxlLmRpc3BsYXkgPSAnYmxvY2snOwoKICBjb25zdCBsaWZlID0gKG4sIGNscykgPT4gYDxzcGFuIHN0eWxlPSJmb250LXNpemU6MTNweDtmb250LXdlaWdodDo1MDA7Y29sb3I6JHtjbHN9Ij4ke259PC9zcGFuPmA7CiAgY29uc3QgbXlMaWZlICA9IHMubXlfbGlmZSAgPD0gNSA/ICcjRTI0QjRBJyA6IHMubXlfbGlmZSAgPD0gMTAgPyAnI0JBNzUxNycgOiAnIzFEOUU3NSc7CiAgY29uc3Qgb3BwTGlmZSA9IHMub3BwX2xpZmUgPD0gNSA/ICcjRTI0QjRBJyA6IHMub3BwX2xpZmUgPD0gMTAgPyAnI0JBNzUxNycgOiAnIzFEOUU3NSc7CgogIGNvbnN0IGNyZWF0dXJlID0gYmYgPT4gYmYuZmlsdGVyKGMgPT4gYy50eXBlcy5pbmNsdWRlcygnQ2FyZFR5cGVfQ3JlYXR1cmUnKSk7CiAgY29uc3QgY2FyZFBpbGwgPSAoYywgdGFwcGVkKSA9PiBgPGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7cGFkZGluZzoycHggN3B4O2JhY2tncm91bmQ6dmFyKC0tYmctc2Vjb25kYXJ5KTtib3JkZXI6MC41cHggc29saWQgdmFyKC0tYm9yZGVyKTtib3JkZXItcmFkaXVzOnZhcigtLXJhZGl1cy1tZCk7bWFyZ2luLWJvdHRvbTozcHg7b3BhY2l0eToke3RhcHBlZD8nMC41JzonMSd9Ij4ke2MubmFtZX0ke2MucG93ZXIhPW51bGw/YCA8c3BhbiBzdHlsZT0iY29sb3I6dmFyKC0tdGV4dC1zZWNvbmRhcnkpIj4ke2MucG93ZXJ9LyR7Yy50b3VnaG5lc3N9PC9zcGFuPmA6Jyd9PC9kaXY+YDsKCiAgY29uc3QgbXlDcmVhdHVyZXMgID0gY3JlYXR1cmUocy5teV9iYXR0bGVmaWVsZCAgfHwgW10pOwogIGNvbnN0IG9wcENyZWF0dXJlcyA9IGNyZWF0dXJlKHMub3BwX2JhdHRsZWZpZWxkIHx8IFtdKTsKICBjb25zdCBteUhhbmQgICAgICAgPSBzLm15X2hhbmQgfHwgW107CgogIGVsLmlubmVySFRNTCA9IGAKICAgIDxkaXYgY2xhc3M9InNlY3Rpb24tbGFiZWwiIHN0eWxlPSJtYXJnaW4tYm90dG9tOjZweDsiPkxpdmUgZ2FtZSBzdGF0ZTwvZGl2PgogICAgPGRpdiBzdHlsZT0iZGlzcGxheTpmbGV4O2p1c3RpZnktY29udGVudDpzcGFjZS1iZXR3ZWVuO2FsaWduLWl0ZW1zOmNlbnRlcjttYXJnaW4tYm90dG9tOjhweDsiPgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTFweDtjb2xvcjp2YXIoLS10ZXh0LXNlY29uZGFyeSkiPlR1cm4gJHtzLnR1cm4gfHwgJ+KAlCd9PC9kaXY+CiAgICAgIDxkaXYgc3R5bGU9ImZvbnQtc2l6ZToxMXB4O2NvbG9yOnZhcigtLXRleHQtc2Vjb25kYXJ5KTt0ZXh0LWFsaWduOnJpZ2h0Ij4ke3MucGhhc2UgfHwgJyd9PC9kaXY+CiAgICA8L2Rpdj4KICAgIDxkaXYgc3R5bGU9ImRpc3BsYXk6ZmxleDtqdXN0aWZ5LWNvbnRlbnQ6c3BhY2UtYmV0d2VlbjttYXJnaW4tYm90dG9tOjhweDsiPgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS10ZXh0LXNlY29uZGFyeSkiPllvdSAke2xpZmUocy5teV9saWZlID8/IDIwLCBteUxpZmUpfTwvZGl2PgogICAgICA8ZGl2IHN0eWxlPSJmb250LXNpemU6MTJweDtjb2xvcjp2YXIoLS10ZXh0LXNlY29uZGFyeSkiPk9wcCAke2xpZmUocy5vcHBfbGlmZSA/PyAyMCwgb3BwTGlmZSl9PC9kaXY+CiAgICA8L2Rpdj4KICAgICR7b3BwQ3JlYXR1cmVzLmxlbmd0aCA/IGA8ZGl2IGNsYXNzPSJzZWN0aW9uLWxhYmVsIiBzdHlsZT0ibWFyZ2luLWJvdHRvbTo0cHg7Ij5UaGVpciBib2FyZDwvZGl2PiR7b3BwQ3JlYXR1cmVzLm1hcChjPT5jYXJkUGlsbChjLGMudGFwcGVkKSkuam9pbignJyl9YCA6ICcnfQogICAgJHtteUNyZWF0dXJlcy5sZW5ndGggID8gYDxkaXYgY2xhc3M9InNlY3Rpb24tbGFiZWwiIHN0eWxlPSJtYXJnaW4tdG9wOjZweDttYXJnaW4tYm90dG9tOjRweDsiPllvdXIgYm9hcmQ8L2Rpdj4ke215Q3JlYXR1cmVzLm1hcChjPT5jYXJkUGlsbChjLGMudGFwcGVkKSkuam9pbignJyl9YCA6ICcnfQogICAgJHtteUhhbmQubGVuZ3RoID8gYDxkaXYgY2xhc3M9InNlY3Rpb24tbGFiZWwiIHN0eWxlPSJtYXJnaW4tdG9wOjZweDttYXJnaW4tYm90dG9tOjRweDsiPllvdXIgaGFuZDwvZGl2PiR7bXlIYW5kLm1hcChuPT5gPGRpdiBzdHlsZT0iZm9udC1zaXplOjExcHg7cGFkZGluZzoycHggN3B4O2JhY2tncm91bmQ6I0UxRjVFRTtib3JkZXItcmFkaXVzOnZhcigtLXJhZGl1cy1tZCk7Y29sb3I6IzA4NTA0MTttYXJnaW4tYm90dG9tOjNweDsiPiR7bn08L2Rpdj5gKS5qb2luKCcnKX1gIDogJyd9CiAgYDsKfQoKYXN5bmMgZnVuY3Rpb24gcG9sbFdhdGNoZXIoKSB7CiAgdHJ5IHsKICAgIGNvbnN0IHJlc3AgPSBhd2FpdCBmZXRjaChXQVRDSEVSX1VSTCArICIvc3RhdGUiLCB7IHNpZ25hbDogQWJvcnRTaWduYWwudGltZW91dCgxNTAwKSB9KTsKICAgIGlmICghcmVzcC5vaykgdGhyb3cgbmV3IEVycm9yKCJiYWQiKTsKICAgIGNvbnN0IGRhdGEgPSBhd2FpdCByZXNwLmpzb24oKTsKICAgIHVwZGF0ZVdhdGNoZXJTdGF0dXModHJ1ZSk7CgogICAgLy8gQXV0by1hZGQgbmV3IG9wcG9uZW50IGNhcmRzCiAgICBjb25zdCBuZXdDYXJkcyA9IChkYXRhLm9wcG9uZW50X2NhcmRzIHx8IFtdKS5maWx0ZXIoYyA9PiAhbGFzdFNlZW5DYXJkcy5pbmNsdWRlcyhjKSk7CiAgICBmb3IgKGNvbnN0IGNhcmQgb2YgbmV3Q2FyZHMpIHsKICAgICAgaWYgKCFvcHBvbmVudENhcmRzLmluY2x1ZGVzKGNhcmQpKSB7CiAgICAgICAgb3Bwb25lbnRDYXJkcy5wdXNoKGNhcmQpOwogICAgICAgIHJlbmRlclRhZ3MoKTsKICAgICAgICBhd2FpdCB0cmlnZ2VyQW5hbHlzaXMoY2FyZCk7CiAgICAgICAgYXdhaXQgbmV3IFByb21pc2UociA9PiBzZXRUaW1lb3V0KHIsIDMwMCkpOwogICAgICB9CiAgICB9CiAgICBsYXN0U2VlbkNhcmRzID0gZGF0YS5vcHBvbmVudF9jYXJkcyB8fCBbXTsKCiAgICAvLyBVcGRhdGUgZ2FtZSBzdGF0ZSBwYW5lbAogICAgcmVuZGVyR2FtZVN0YXRlKGRhdGEpOwogICAgbGFzdEdhbWVTdGF0ZSA9IGRhdGE7CiAgfSBjYXRjaChlKSB7CiAgICB1cGRhdGVXYXRjaGVyU3RhdHVzKGZhbHNlKTsKICAgIGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdnYW1lLXN0YXRlLXBhbmVsJykgJiYgKGRvY3VtZW50LmdldEVsZW1lbnRCeUlkKCdnYW1lLXN0YXRlLXBhbmVsJykuc3R5bGUuZGlzcGxheT0nbm9uZScpOwogIH0KfQoKYXN5bmMgZnVuY3Rpb24gcmVzZXRXYXRjaGVyKCkgewogIHRyeSB7IGF3YWl0IGZldGNoKFdBVENIRVJfVVJMICsgIi9yZXNldCIsIHsgc2lnbmFsOiBBYm9ydFNpZ25hbC50aW1lb3V0KDE1MDApIH0pOyB9IGNhdGNoKGUpIHt9CiAgbGFzdFNlZW5DYXJkcyA9IFtdOwogIGxhc3RHYW1lU3RhdGUgPSB7fTsKfQoKc2V0SW50ZXJ2YWwocG9sbFdhdGNoZXIsIDIwMDApOwpwb2xsV2F0Y2hlcigpOwo8L3NjcmlwdD4KPC9ib2R5Pgo8L2h0bWw+Cg=="
_EMBEDDED_HTML = __import__("base64").b64decode(_HTML_B64)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()


    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(_EMBEDDED_HTML))
            self.end_headers()
            self.wfile.write(_EMBEDDED_HTML)
            return
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(_EMBEDDED_HTML))
            self.end_headers()
            self.wfile.write(_EMBEDDED_HTML)
            return
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
        self.send_header("Access-Control-Allow-Headers","Content-Type, ngrok-skip-browser-warning")
        self.send_header("ngrok-skip-browser-warning", "1")

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 52)
    print("  MTG Arena Watcher  —  Full Game State Edition")
    print("=" * 52 + "\n")

    grp = load_bulk()
    with lock:
        state["grp_map"] = grp

    threading.Thread(target=watch_log, daemon=True).start()
    threading.Thread(target=push_loop, daemon=True).start()

    try:
        print("Pushing game state to Firebase...")
        print(f"Open: https://abaek5.github.io/mtg-archetype-detector\n")
        # Keep main thread alive
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nStopped.")

# ── Serve local HTML (added to fix HTTPS/localhost CORS issue) ─────────────────
import pathlib as _pl
_HTML_PATH = _pl.Path(__file__).parent / "index_local.html"
_HTML_CACHE = None

def _get_html():
    global _HTML_CACHE
    if _HTML_CACHE is None and _HTML_PATH.exists():
        _HTML_CACHE = _HTML_PATH.read_bytes()
    return _HTML_CACHE
