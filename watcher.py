#!/usr/bin/env python3
"""
MTG Arena Watcher — pushes game state to Firebase Realtime Database.
Tracks ALL cast cards with seat ownership; site filters by loaded deck.
"""

import json
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
FIREBASE_URL = "https://mtg-detector-40285-default-rtdb.firebaseio.com"
LOG_PATH = Path(os.path.expandvars(
    r"%APPDATA%\..\LocalLow\Wizards of the Coast\MTGA\Player.log"
))

SKIP_NAMES = {"", "Plains", "Island", "Swamp", "Mountain", "Forest"}

# ── Shared state ──────────────────────────────────────────────────────────────
lock = threading.Lock()
state = {
    "all_cast_cards": [],   # [{name, owner}] — all cast non-land cards
    "opp_graveyard":  [],   # cards in opponent graveyard (for mill)
    "my_hand":        [],
    "my_battlefield": [],
    "opp_battlefield":[],
    "phase": "",
    "turn":  0,
    "my_life":  20,
    "opp_life": 20,
    "last_update": 0,
    "match_game": 1,
    "grp_map":      {},     # grpId -> card name
    "instance_map": {},     # instanceId -> info dict
    "my_seat": 0,           # detected from log each match
}

# ── Scryfall card name lookup ─────────────────────────────────────────────────
def lookup_grp(grp_id: int):
    """Async Scryfall lookup; resolves pending_add cards when name arrives."""
    def _fetch():
        try:
            req = urllib.request.Request(
                f"https://api.scryfall.com/cards/arena/{grp_id}",
                headers={"User-Agent": "MTGArchetypeDetector/1.0",
                         "Accept": "application/json"}
            )
            data = urllib.request.urlopen(req, timeout=6).read()
            obj  = json.loads(data)
            name = obj.get("name", "")
            if not name:
                return
            with lock:
                state["grp_map"][grp_id] = name
                print(f"  [RESOLVED] grp={grp_id} -> {name}")
                # Resolve any pending cards waiting for this name
                my_seat  = state["my_seat"]
                opp_seat = 1 if my_seat == 2 else 2
                for iid, info in state["instance_map"].items():
                    if (info.get("grpId") == grp_id
                            and info.get("pending_add")):
                        info["pending_add"] = False
                        info["name"] = name
                        owner  = info.get("owner")
                        ctypes = info.get("cardTypes", [])
                        token  = info.get("token", False)
                        if ("CardType_Land" not in ctypes
                                and not token
                                and name not in SKIP_NAMES):
                            entry = {"name": name, "owner": owner}
                            if entry not in state["all_cast_cards"]:
                                state["all_cast_cards"].append(entry)
                                state["last_update"] = time.time()
                                print(f"  [LATE ] seat={owner}: {name}")
        except Exception as e:
            print(f"  [ERR  ] Scryfall lookup grp={grp_id}: {e}")
    threading.Thread(target=_fetch, daemon=True).start()

# ── Game state parser ─────────────────────────────────────────────────────────
def parse_game_state(msg: dict):
    gm = msg.get("gameStateMessage", {})
    if not gm:
        return

    with lock:
        my_seat  = state["my_seat"]
        if my_seat == 0:
            return  # wait for seat detection
        opp_seat = 1 if my_seat == 2 else 2

        # Turn info
        ti = gm.get("turnInfo", {})
        cur_turn = ti.get("turnNumber", 0)

        # Detect new game
        if cur_turn == 1 and state["turn"] > 3:
            state["match_game"] += 1
            if state["match_game"] > 3:
                state["match_game"] = 1
            state["all_cast_cards"] = []
            state["opp_graveyard"]  = []
            state["instance_map"]   = {}
            state["my_seat"]        = 0
            state["my_life"]  = 20
            state["opp_life"] = 20
            print(f"  [GAME  ] Game {state['match_game']} started")

        if cur_turn:
            state["turn"] = cur_turn

        phase = ti.get("phase", "")
        step  = ti.get("step", "")
        if phase:
            state["phase"] = phase.replace("Phase_", "").replace("Step_", "")
        if step:
            state["phase"] += f" {step.replace('Step_', '')}"

        # Life totals
        for p in gm.get("players", []):
            seat = p.get("systemSeatNumber")
            life = p.get("lifeTotal")
            if life is None:
                continue
            if seat == my_seat:
                state["my_life"] = life
            elif seat == opp_seat:
                state["opp_life"] = life

        # Update instance map from gameObjects
        for obj in gm.get("gameObjects", []):
            iid   = obj.get("instanceId")
            grpid = obj.get("grpId")
            owner = obj.get("ownerSeatId")
            zone  = obj.get("zoneId")
            tapped = obj.get("isTapped", False)
            power  = obj.get("power") or obj.get("powerValue")
            tough  = obj.get("toughness") or obj.get("toughnessValue")
            raw_ctypes = obj.get("cardTypes", [])
            if raw_ctypes and isinstance(raw_ctypes[0], dict):
                ctypes = [t.get("type", "") for t in raw_ctypes]
            else:
                ctypes = raw_ctypes
            token  = obj.get("isToken", False) or obj.get("type","") == "GameObjectType_Token"
            if not iid:
                continue
            name = state["grp_map"].get(grpid)
            existing = state["instance_map"].get(iid, {})
            known_zones = state.get("zone_map", {})
            inferred_zone = known_zones.get(zone, existing.get("zone_type", ""))
            state["instance_map"][iid] = {
                "grpId":     grpid,
                "name":      name,
                "owner":     owner,
                "zoneId":    zone,
                "zone_type": inferred_zone,
                "tapped":    tapped,
                "power":     power,
                "toughness": tough,
                "cardTypes": ctypes,
                "token":     token,
                "pending_add": existing.get("pending_add", False),
            }

        # Zone tracking
        if "zone_map" not in state:
            state["zone_map"] = {}
        for z in gm.get("zones", []):
            ztype = z.get("type", "")
            owner = z.get("ownerSeatId")
            iids  = z.get("objectInstanceIds", [])
            zid   = z.get("zoneId")
            if zid and ztype:
                zname = ztype.replace("ZoneType_", "")
                state["zone_map"][zid] = zname
            if ztype == "ZoneType_Battlefield":
                for iid in iids:
                    if iid in state["instance_map"]:
                        state["instance_map"][iid]["zone_type"] = "Battlefield"
                    elif iid:
                        state["instance_map"][iid] = {
                            "zone_type": "Battlefield", "owner": owner}
            elif ztype == "ZoneType_Hand":
                if owner == my_seat:
                    for iid in iids:
                        if iid in state["instance_map"]:
                            state["instance_map"][iid]["zone_type"] = "Hand"
                        else:
                            state["instance_map"][iid] = {
                                "zone_type": "Hand", "owner": owner, "name": None}
            elif ztype == "ZoneType_Graveyard" and owner == opp_seat:
                for iid in iids:
                    if iid in state["instance_map"]:
                        state["instance_map"][iid]["zone_type"] = "Graveyard"

        # CastSpell annotations — track ALL cast cards with owner
        for ann in gm.get("annotations", []):
            if "AnnotationType_ZoneTransfer" not in ann.get("type", []):
                continue
            details  = {d["key"]: d for d in ann.get("details", [])}
            category = details.get("category", {}).get("valueString", [""])[0]
            if category != "CastSpell":
                continue
            for iid in ann.get("affectedIds", []):
                info   = state["instance_map"].get(iid, {})
                grpid  = info.get("grpId")
                name   = info.get("name") or state["grp_map"].get(grpid)
                ctypes = info.get("cardTypes", [])
                token  = info.get("token", False)
                owner  = info.get("owner")
                if "CardType_Land" in ctypes or token:
                    continue
                if name and name not in SKIP_NAMES:
                    entry = {"name": name, "owner": owner}
                    if entry not in state["all_cast_cards"]:
                        state["all_cast_cards"].append(entry)
                        state["last_update"] = time.time()
                        print(f"  [CAST ] seat={owner}: {name}")
                elif grpid:
                    print(f"  [QUEUE] grp={grpid} not resolved, looking up...")
                    info["pending_add"] = True
                    state["instance_map"][iid] = info
                    lookup_grp(grpid)

        # Rebuild visible state (hand, battlefields, graveyard)
        my_hand, my_bf, opp_bf = [], [], []
        for iid, info in state["instance_map"].items():
            zt    = info.get("zone_type", "")
            name  = info.get("name")
            owner = info.get("owner")
            is_token = info.get("token", False)
            if not name:
                grpid = info.get("grpId")
                if grpid:
                    name = state["grp_map"].get(grpid)
                    if name:
                        info["name"] = name
            if not name:
                continue
            if zt == "Hand" and owner == my_seat and not is_token:
                my_hand.append(name)
            elif zt == "Graveyard" and owner == opp_seat and not is_token:
                if name not in state["opp_graveyard"] and name not in SKIP_NAMES:
                    state["opp_graveyard"].append(name)
                    print(f"  [GRAVE] Opponent graveyard: {name}")
            elif zt == "Battlefield":
                entry = {
                    "name":      name + (" [Token]" if is_token else ""),
                    "power":     info.get("power"),
                    "toughness": info.get("toughness"),
                    "tapped":    info.get("tapped", False),
                    "types":     info.get("cardTypes", []),
                    "token":     is_token,
                }
                if owner == my_seat:
                    my_bf.append(entry)
                elif owner == opp_seat:
                    opp_bf.append(entry)

        state["my_hand"]         = my_hand
        state["my_battlefield"]  = my_bf
        state["opp_battlefield"] = opp_bf
        state["last_update"]     = time.time()

# ── Firebase sync ─────────────────────────────────────────────────────────────
def push_to_firebase():
    try:
        with lock:
            payload = json.dumps({
                "all_cast_cards": state["all_cast_cards"],
                "opp_graveyard":  state["opp_graveyard"],
                "my_hand":        state["my_hand"],
                "my_battlefield": state["my_battlefield"],
                "opp_battlefield":state["opp_battlefield"],
                "phase":          state["phase"],
                "turn":           state["turn"],
                "my_life":        state["my_life"],
                "opp_life":       state["opp_life"],
                "last_update":    state["last_update"],
                "match_game":     state["match_game"],
                "my_seat":        state["my_seat"],
            }).encode()
        req = urllib.request.Request(
            f"{FIREBASE_URL}/state.json",
            data=payload, method="PUT",
            headers={"Content-Type": "application/json",
                     "User-Agent":   "MTGArchetypeDetector/1.0"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"  [WARN] Firebase sync failed: {e}")

reset_hold_until = 0

def push_loop():
    global reset_hold_until
    while True:
        # Check for reset signal from New Game button
        try:
            req = urllib.request.Request(
                f"{FIREBASE_URL}/reset_requested.json",
                headers={"User-Agent": "MTGArchetypeDetector/1.0"}
            )
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read())
            if data is True:
                # Clear flag first
                try:
                    clear_req = urllib.request.Request(
                        f"{FIREBASE_URL}/reset_requested.json",
                        data=b"false", method="PUT",
                        headers={"Content-Type": "application/json",
                                 "User-Agent": "MTGArchetypeDetector/1.0"}
                    )
                    urllib.request.urlopen(clear_req, timeout=3)
                except Exception:
                    pass
                # Clear state
                with lock:
                    state["all_cast_cards"] = []
                    state["opp_graveyard"]  = []
                    state["instance_map"]   = {}
                    state["my_seat"]        = 0
                    state["my_life"]  = 20
                    state["opp_life"] = 20
                    state["last_update"] = time.time()
                    print("  [RESET] New game — cleared by browser")
                push_to_firebase()
                reset_hold_until = time.time() + 12
                time.sleep(2)
                continue
        except Exception:
            pass

        if time.time() >= reset_hold_until:
            push_to_firebase()
        time.sleep(2)

# ── Log watcher ───────────────────────────────────────────────────────────────
def detect_seat(line: str):
    """Find RagingDachshund in matchGameRoomStateChangedEvent and get their seat."""
    if "RagingDachshund" not in line:
        return
    import re
    # Pattern: "playerName":"RagingDachshund", "systemSeatId": N
    for m in re.finditer(r'"playerName"\s*:\s*"RagingDachshund"\s*,\s*"systemSeatId"\s*:\s*(\d+)', line):
        seat = int(m.group(1))
        if seat in (1, 2):
            with lock:
                if state["my_seat"] != seat:
                    state["my_seat"] = seat
                    print(f"  [SEAT ] You are seat {seat}, opponent is seat {3-seat}")
            return

def parse_chunk(text: str):
    for line in text.split("\n"):
        if not line.strip():
            continue
        # Detect seat from matchGameRoomStateChangedEvent
        if "RagingDachshund" in line and "systemSeatId" in line:
            detect_seat(line)
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        # greToClientEvent contains game state messages
        evt = obj.get("greToClientEvent", {})
        for msg in evt.get("greToClientMessages", []):
            parse_game_state(msg)

def watch_log():
    print(f"Watching: {LOG_PATH}")
    if not LOG_PATH.exists():
        print("\n[ERROR] Log not found. Enable Detailed Logs in Arena Settings.\n")
        return
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)
        print("Ready — watching for game events...\n")
        buf = ""
        while True:
            chunk = f.read(131072)
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

# ── Bulk card cache download ───────────────────────────────────────────────────
SCRYFALL_BULK = Path(__file__).parent / "scryfall_arena.json"

def load_bulk():
    if SCRYFALL_BULK.exists():
        try:
            data = json.loads(SCRYFALL_BULK.read_text(encoding="utf-8"))
            with lock:
                for card in data:
                    aid = card.get("arena_id")
                    name = card.get("name")
                    if aid and name:
                        state["grp_map"][aid] = name
            print(f"Loaded {len(state['grp_map'])} cards from cache.")
            return
        except Exception:
            pass
    print("No local card cache — will look up cards via Scryfall API as they appear.")
    print("(Run once with internet to cache all cards for faster future use)")
    # Try to download bulk data
    try:
        opener = urllib.request.build_opener()
        opener.addheaders = [("User-Agent", "MTGArchetypeDetector/1.0")]
        meta = json.loads(opener.open(
            "https://api.scryfall.com/bulk-data", timeout=10).read())
        url = next((b["download_uri"] for b in meta.get("data", [])
                    if b["type"] == "default_cards"), None)
        if url:
            print("Downloading card database...")
            with opener.open(url, timeout=60) as r:
                raw = json.loads(r.read())
            arena = [{"arena_id": c.get("arena_id"), "name": c["name"]}
                     for c in raw if c.get("arena_id")]
            SCRYFALL_BULK.write_text(
                json.dumps(arena), encoding="utf-8")
            with lock:
                for c in arena:
                    state["grp_map"][c["arena_id"]] = c["name"]
            print(f"Cached {len(arena)} Arena cards.")
    except Exception as e:
        print(f"Background download failed: {e}")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 52)
    print("  MTG Arena Watcher  —  Firebase Edition")
    print("=" * 52)
    load_bulk()
    print(f"Pushing game state to Firebase...")
    print(f"Open: https://mtg-archetype-detector.pages.dev\n")
    threading.Thread(target=watch_log, daemon=True).start()
    threading.Thread(target=push_loop, daemon=True).start()
    while True:
        time.sleep(60)
