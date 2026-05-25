#!/usr/bin/env python3
"""
MTG Arena Watcher — Event-Sourced Edition
Architecture:
  Layer 1: Log ingestion (reads raw MTGA messages)
  Layer 2: Normalization (converts messages → Event objects)
  Layer 3: Reducer (pure function, only place state changes occur)
  Side effects (Scryfall, Firebase) happen AFTER reduction
"""

import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

# ── Config ────────────────────────────────────────────────────────────────────
FIREBASE_URL = "https://mtg-detector-40285-default-rtdb.firebaseio.com"
PLAYER_NAME  = os.environ.get("MTGA_PLAYER_NAME", "RagingDachshund")
LOG_PATH     = Path(os.path.expandvars(
    r"%APPDATA%\..\LocalLow\Wizards of the Coast\MTGA\Player.log"
))
SKIP_NAMES = {"", "Plains", "Island", "Swamp", "Mountain", "Forest"}

# ── Event dataclass ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Event:
    type:       str
    key:        str          # idempotency key — same event never applied twice
    generation: int
    payload:    Dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

# ── Scryfall rate limiting ────────────────────────────────────────────────────
scryfall_semaphore = threading.Semaphore(2)
failed_grp_ids     = set()
pending_lookups    = set()

# ── Shared state ──────────────────────────────────────────────────────────────
lock = threading.Lock()

state = {
    "generation":    0,
    "my_seat":       0,       # set only from seat_assigned event
    "match_game":    1,
    "turn":          0,
    "phase":         "",
    "my_life":       20,
    "opp_life":      20,
    "last_update":   0,

    # Card data
    "all_cast_cards":  [],    # [{name, owner, iid, generation, turn}]
    "opp_graveyard":   set(), # set of names
    "graveyard_cards": {},    # (generation, iid) -> {name, owner, turn, source}
    "my_hand":         [],
    "my_battlefield":  [],
    "opp_battlefield": [],

    # Instance tracking
    "instance_map":  {},      # iid -> copy-on-write dict
    "zone_map":      {},      # zoneId -> zone type string (persisted, stable IDs)
    "owner_hand_zones": {},   # ownerSeatId -> hand zoneId

    # Deduplication
    "cast_seen":     set(),   # (type, iid, turn, name) tuples
    "event_log":     [],
    "next_event_id": 0,

    # Scryfall
    "grp_map":       {},      # grpId -> card name

    # Reset tracking
    "reset_time":    0,
    "ignored_generations": set(),
}

applied_events: set = set()  # global idempotency set

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_me(owner_seat) -> bool:
    return owner_seat is not None and owner_seat == state["my_seat"]

def is_opp(owner_seat) -> bool:
    return (owner_seat is not None
            and state["my_seat"] != 0
            and owner_seat != state["my_seat"])

# ── Hard reset ────────────────────────────────────────────────────────────────
def hard_reset_state(reason: str = "manual"):
    old_gen = state["generation"]
    state["ignored_generations"].add(old_gen)
    state["generation"] += 1

    state["my_seat"]       = 0
    state["turn"]          = 0
    state["phase"]         = ""
    state["my_life"]       = 20
    state["opp_life"]      = 20

    state["all_cast_cards"]  = []
    state["opp_graveyard"]   = set()
    state["graveyard_cards"] = {}
    state["my_hand"]         = []
    state["my_battlefield"]  = []
    state["opp_battlefield"] = []

    state["instance_map"]    = {}
    state["owner_hand_zones"]= {}

    # Clear dedup state so new match is fresh
    state["cast_seen"]       = set()
    state["event_log"]       = state["event_log"][-500:]  # preserve history
    applied_events.clear()

    # Clear Scryfall tracking (keep grp_map cache)
    pending_lookups.clear()
    # Note: failed_grp_ids and grp_map are kept (stable data)

    state["reset_time"]    = time.time()
    state["last_update"]   = time.time()
    print(f"  [RESET] generation={state['generation']} reason={reason}")

# ── Scryfall lookup ───────────────────────────────────────────────────────────
def lookup_grp(grp_id: int):
    """Enriches grp_map only. Never creates gameplay events."""
    with lock:
        if grp_id in state["grp_map"]:   return
        if grp_id in failed_grp_ids:     return
        if grp_id in pending_lookups:    return
        pending_lookups.add(grp_id)

    def _fetch():
        """Enriches grp_map and instance_map names ONLY. Never creates gameplay events."""
        try:
            with scryfall_semaphore:
                time.sleep(0.1)
                req = urllib.request.Request(
                    f"https://api.scryfall.com/cards/arena/{grp_id}",
                    headers={"User-Agent": "MTGArchetypeDetector/1.0",
                             "Accept":     "application/json"}
                )
                try:
                    data = urllib.request.urlopen(req, timeout=6).read()
                except urllib.error.HTTPError as he:
                    if he.code == 404:
                        with lock: failed_grp_ids.add(grp_id)
                        return
                    elif he.code == 429:
                        time.sleep(5)
                        return
                    raise
            obj  = json.loads(data)
            name = obj.get("name", "")
            if not name:
                with lock: failed_grp_ids.add(grp_id)
                return
            with lock:
                state["grp_map"][grp_id] = name
                print(f"  [RESOLVED] grp={grp_id} -> {name}")
                gen = state["generation"]
                # Enrich instance_map names only — no event creation
                for iid, info in state["instance_map"].items():
                    if info.get("grpId") == grp_id and info.get("generation") == gen:
                        new_info = dict(info)
                        new_info["name"] = name
                        state["instance_map"][iid] = new_info
                        # If this card is in my hand, update hand immediately
                        if (new_info.get("zone_type") == "Hand"
                                and is_me(new_info.get("owner"))
                                and not new_info.get("token", False)
                                and name not in SKIP_NAMES
                                and name not in state["my_hand"]):
                            state["my_hand"].append(name)
                            state["last_update"] = time.time()
                            print(f"  [HAND ] resolved: {name}")
                state["last_update"] = time.time()
        except Exception as e:
            print(f"  [ERR  ] Scryfall grp={grp_id}: {e}")
        finally:
            with lock: pending_lookups.discard(grp_id)

    threading.Thread(target=_fetch, daemon=True).start()

# ── LAYER 2: Normalize MTGA message → Events ──────────────────────────────────
def normalize(msg: dict, generation: int) -> list:
    """Pure normalization: MTGA message → list of Event objects. No state reads."""
    events = []
    gm = msg.get("gameStateMessage", {})
    if not gm:
        return events

    ti = gm.get("turnInfo", {})
    turn = ti.get("turnNumber", 0)

    # Turn/phase event
    if turn:
        events.append(Event(
            type="turn_update", key=f"turn:{generation}:{turn}",
            generation=generation, payload={"turn": turn,
            "phase": ti.get("phase", ""), "step": ti.get("step", "")}
        ))

    # Life total events
    for p in gm.get("players", []):
        seat = p.get("systemSeatNumber")
        life = p.get("lifeTotal")
        if seat and life is not None:
            events.append(Event(
                type="life_update",
                key=f"life:{generation}:{seat}:{gm.get('gameStateId',0)}",
                generation=generation,
                payload={"seat": seat, "life": life}
            ))

    # Zone map updates
    for z in gm.get("zones", []):
        zid   = z.get("zoneId")
        ztype = z.get("type", "")
        owner = z.get("ownerSeatId")
        iids  = z.get("objectInstanceIds", [])
        if zid and ztype:
            events.append(Event(
                type="zone_map_update",
                key=f"zone_map:{zid}:{ztype}",
                generation=generation,
                payload={"zid": zid, "ztype": ztype, "owner": owner, "iids": iids}
            ))

    # GameObject updates
    for obj in gm.get("gameObjects", []):
        iid = obj.get("instanceId")
        if not iid:
            continue
        events.append(Event(
            type="object_update",
            key=f"obj:{generation}:{iid}:{gm.get('gameStateId',0)}",
            generation=generation,
            payload=obj
        ))

    # ZoneTransfer annotations — single source of truth for all zone changes
    for ann in gm.get("annotations", []):
        if "AnnotationType_ZoneTransfer" not in ann.get("type", []):
            continue
        details  = {d["key"]: d for d in ann.get("details", [])}
        category = (details.get("category", {}).get("valueString") or [""])[0]
        src_id   = (details.get("zone_src",  {}).get("valueInt32") or [None])[0]
        dst_id   = (details.get("zone_dest", {}).get("valueInt32") or [None])[0]

        for iid in ann.get("affectedIds", []):
            if category == "CastSpell":
                events.append(Event(
                    type="cast",
                    key=f"cast:{generation}:{iid}:{turn}",
                    generation=generation,
                    payload={"iid": iid, "turn": turn,
                             "src_id": src_id, "dst_id": dst_id}
                ))
            else:
                events.append(Event(
                    type="zone_transfer",
                    key=f"zt:{generation}:{iid}:{src_id}:{dst_id}:{gm.get('gameStateId',0)}",
                    generation=generation,
                    payload={"iid": iid, "turn": turn, "category": category,
                             "src_id": src_id, "dst_id": dst_id}
                ))

    return events

# ── LAYER 3: Reducer ──────────────────────────────────────────────────────────
def reduce_event(event: Event):
    """Pure reducer: applies a single Event to state. Called under lock."""
    if event.key in applied_events:
        return  # idempotent
    applied_events.add(event.key)

    if event.generation != state["generation"]:
        return  # stale

    p = event.payload

    if event.type == "turn_update":
        cur_turn = p["turn"]
        # New game detection
        if cur_turn == 1 and state["turn"] >= 2:
            state["match_game"] = min(state["match_game"] + 1, 3)
            hard_reset_state("new_game")
            return
        state["turn"]  = cur_turn
        phase = p.get("phase", "").replace("Phase_", "").replace("Step_", "")
        step  = p.get("step",  "").replace("Step_", "")
        state["phase"] = (phase + " " + step).strip()

    elif event.type == "life_update":
        seat = p["seat"]
        life = p["life"]
        if is_me(seat):
            state["my_life"]  = life
        elif is_opp(seat):
            state["opp_life"] = life

    elif event.type == "zone_map_update":
        zid   = p["zid"]
        ztype = p["ztype"]
        owner = p.get("owner")
        iids  = p.get("iids", [])
        state["zone_map"][zid] = ztype.replace("ZoneType_", "")
        if ztype == "ZoneType_Hand" and owner:
            state["owner_hand_zones"][owner] = zid
        # Update zone_type for all listed iids
        for iid in iids:
            zone_name = ztype.replace("ZoneType_", "")
            existing = state["instance_map"].get(iid, {})
            new_info = dict(existing)
            new_info.update({
                "zone_type":  zone_name,
                "owner":      existing.get("owner") or owner,
                "generation": event.generation,
                "last_seen":  time.time(),
            })
            if not new_info.get("grpId") and not new_info.get("name"):
                new_info["name"] = None
            state["instance_map"][iid] = new_info

    elif event.type == "object_update":
        iid   = p.get("instanceId")
        grpid = p.get("grpId")
        owner = p.get("ownerSeatId")
        zone  = p.get("zoneId")
        raw_ct = p.get("cardTypes", [])
        ctypes = ([t.get("type", "") for t in raw_ct]
                  if raw_ct and isinstance(raw_ct[0], dict) else raw_ct)
        token  = p.get("isToken", False) or p.get("type","") == "GameObjectType_Token"
        name   = state["grp_map"].get(grpid)
        # Infer zone type
        zone_type = (state["zone_map"].get(zone)
                     or (state["instance_map"].get(iid) or {}).get("zone_type", ""))
        if not zone_type and zone and owner:
            if state["owner_hand_zones"].get(owner) == zone:
                zone_type = "Hand"
        # Copy-on-write
        existing = state["instance_map"].get(iid, {})
        new_info = dict(existing)
        new_info.update({
            "generation": event.generation,
            "last_seen":  time.time(),
            "grpId":      grpid,
            "owner":      owner,
            "zoneId":     zone,
            "zone_type":  zone_type or existing.get("zone_type", ""),
            "cardTypes":  ctypes,
            "token":      token,
            "tapped":     p.get("isTapped", False),
            "power":      p.get("power") or p.get("powerValue"),
            "toughness":  p.get("toughness") or p.get("toughnessValue"),
        })
        if name:
            new_info["name"] = name
        elif not new_info.get("name"):
            new_info["name"] = None
            if grpid:
                lookup_grp(grpid)
        state["instance_map"][iid] = new_info

    elif event.type == "cast":
        iid  = p["iid"]
        turn = p["turn"]
        info = state["instance_map"].get(iid, {})
        if info.get("generation") != event.generation:
            return
        grpid  = info.get("grpId")
        name   = info.get("name") or state["grp_map"].get(grpid)
        owner  = info.get("owner")
        ctypes = info.get("cardTypes", [])
        token  = info.get("token", False)
        if "CardType_Land" in ctypes or token:
            return
        if not name:
            if grpid: lookup_grp(grpid)
            return
        if name in SKIP_NAMES:
            return
        cast_key = ("cast", iid, turn, name)
        if cast_key in state["cast_seen"]:
            return
        state["cast_seen"].add(cast_key)
        state["all_cast_cards"].append({
            "name": name, "owner": owner, "iid": iid,
            "generation": event.generation, "turn": turn,
        })
        state["event_log"].append({
            "id": state["next_event_id"], "generation": event.generation,
            "turn": turn, "event": "cast", "card": name, "owner": owner,
        })
        state["next_event_id"] += 1
        state["last_update"] = time.time()
        print(f"  [CAST ] seat={owner}: {name}")

    elif event.type == "zone_transfer":
        iid    = p["iid"]
        turn   = p["turn"]
        src_id = p.get("src_id")
        dst_id = p.get("dst_id")
        src_type = state["zone_map"].get(src_id, "")
        dst_type = state["zone_map"].get(dst_id, "")
        if dst_type != "Graveyard":
            return
        info  = state["instance_map"].get(iid, {})
        if info.get("generation") != event.generation:
            return
        grpid = info.get("grpId")
        name  = info.get("name") or state["grp_map"].get(grpid)
        owner = info.get("owner")
        if not name or name in SKIP_NAMES:
            return
        source = "mill" if src_type == "Library" else "other"
        gy_key = (event.generation, iid)
        if gy_key in state["graveyard_cards"]:
            return  # ZoneTransfer is single source of truth — no duplicates
        state["graveyard_cards"][gy_key] = {
            "name": name, "owner": owner, "turn": turn,
            "source": source, "generation": event.generation,
        }
        if is_opp(owner):
            state["opp_graveyard"].add(name)
            print(f"  [GRAVE] ({source}) seat={owner}: {name}")
        state["event_log"].append({
            "id": state["next_event_id"], "generation": event.generation,
            "turn": turn, "event": "graveyard",
            "card": name, "owner": owner, "source": source,
        })
        state["next_event_id"] += 1
        state["last_update"] = time.time()

    # Rebuild derived state after every event
    _rebuild_live_state(event.generation)

def _rebuild_live_state(generation: int):
    """Rebuild my_hand, my_battlefield, opp_battlefield from instance_map."""
    my_seat = state["my_seat"]
    if my_seat == 0:
        # Try to infer seat from hand zone instances
        for iid, info in state["instance_map"].items():
            if (info.get("generation") == generation
                    and info.get("zone_type") == "Hand"
                    and info.get("owner") in (1, 2)):
                state["my_seat"] = info["owner"]
                my_seat = info["owner"]
                print(f"  [RECOVER] inferred seat {my_seat} from hand zone")
                break

    my_hand, my_bf, opp_bf = [], [], []
    for iid, info in state["instance_map"].items():
        if info.get("generation") != generation:
            continue
        zt    = info.get("zone_type", "")
        name  = info.get("name")
        owner = info.get("owner")
        token = info.get("token", False)
        if not name:
            continue
        if zt == "Hand" and is_me(owner) and not token:
            my_hand.append(name)
        elif zt == "Battlefield":
            entry = {
                "name":      name + (" [Token]" if token else ""),
                "power":     info.get("power"),
                "toughness": info.get("toughness"),
                "tapped":    info.get("tapped", False),
                "types":     info.get("cardTypes", []),
                "token":     token,
            }
            if is_me(owner):
                my_bf.append(entry)
            elif is_opp(owner):
                opp_bf.append(entry)

    # Dedup hand
    state["my_hand"]        = list(dict.fromkeys(my_hand))
    state["my_battlefield"] = my_bf
    state["opp_battlefield"]= opp_bf
    if state["my_hand"]:
        print(f"  [HAND ] {state['my_hand']}")

# ── LAYER 1: Log ingestion ────────────────────────────────────────────────────
def detect_seat(line: str):
    patterns = [
        r'"playerName"\s*:\s*"([^"]+)"\s*,\s*"systemSeatId"\s*:\s*(\d+)',
        r'"systemSeatId"\s*:\s*(\d+)\s*,\s*"playerName"\s*:\s*"([^"]+)"',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, line):
            try:
                if pattern.startswith('"playerName"'):
                    name, seat = m.group(1), int(m.group(2))
                else:
                    seat, name = int(m.group(1)), m.group(2)
                if name == PLAYER_NAME and seat in (1, 2):
                    with lock:
                        if state["my_seat"] != seat:
                            state["my_seat"] = seat
                            print(f"  [SEAT ] You are seat {seat}, opponent is seat {3-seat}")
                    return
            except Exception:
                continue

def process_message(msg: dict):
    """Normalize message to events, then reduce each event."""
    with lock:
        generation = state["generation"]
        events = normalize(msg, generation)
        for event in events:
            reduce_event(event)

def parse_chunk(text: str):
    for line in text.split("\n"):
        if not line.strip():
            continue
        if "playerName" in line and "systemSeatId" in line:
            with lock:
                detect_seat(line)
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        evt = obj.get("greToClientEvent", {})
        for msg in evt.get("greToClientMessages", []):
            process_message(msg)

def watch_log():
    print(f"Watching: {LOG_PATH}")
    if not LOG_PATH.exists():
        print("\n[ERROR] Log not found. Enable Detailed Logs in Arena Settings.\n")
        return
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)
        print("Ready — watching for game events.\n")
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

# ── Mulligan evaluator ────────────────────────────────────────────────────────
def evaluate_hand(hand, on_draw=False, matchup="unknown"):
    land_patterns = [
        "Forest","Island","Swamp","Mountain","Plains","Passage","Castle",
        "Boseiju","Nykthos","Pathway","Clearing","Grove","Triome","Tarn",
        "Delta","Marsh","Shore","Depths","Sanctum","Cavern","Haven","Hub",
        "Spire","Garden","Pool","Tomb","Crypt","Vault","Gate","Vale","Ridge",
        "Strand","Foothills","Fetch","Shock","Dual","Check","Fast","Pain",
    ]
    lands  = [c for c in hand if any(p.lower() in c.lower() for p in land_patterns)]
    spells = [c for c in hand if c not in lands]
    land_count  = len(lands)
    spell_count = len(spells)
    reasons, score = [], 0

    if land_count == 0:  return "MULLIGAN", -10, ["No lands"]
    if land_count == 1:  score -= 3; reasons.append("1 land — very risky")
    elif land_count == 2: score += 1; reasons.append("2 lands — functional if cheap")
    elif land_count == 3: score += 3; reasons.append("3 lands — ideal")
    elif land_count == 4: score += 1; reasons.append("4 lands — slightly heavy")
    elif land_count >= 5: score -= 2; reasons.append(f"{land_count} lands — flood risk")

    if spell_count == 0: score -= 3; reasons.append("All lands — no action")
    elif spell_count >= 4: score += 1; reasons.append("Spell-rich hand")

    short_names = [c for c in spells if len(c) <= 12]
    if short_names: score += 1; reasons.append("Likely early play available")
    if land_count <= 2 and spell_count <= 1: score -= 3; reasons.append("Too few spells")
    if on_draw: score += 0.5; reasons.append("On draw: looser keep")
    if matchup in ("aggro", "Mono-Red Aggro", "Burn") and land_count >= 4:
        score -= 1; reasons.append("Heavy hand vs aggro")

    return ("KEEP" if score >= 2 else "MULLIGAN"), score, reasons

def _get_mulligan_eval(hand):
    if not hand or len(hand) < 5:
        return None
    decision, score, reasons = evaluate_hand(hand)
    return {"decision": decision, "score": score, "reasons": reasons}

# ── Firebase sync ─────────────────────────────────────────────────────────────
def push_to_firebase():
    try:
        with lock:
            gy_names = sorted(list(state["opp_graveyard"]))
            snap = {
                "all_cast_cards":  list(state["all_cast_cards"]),
                "opp_graveyard":   gy_names,
                "graveyard_cards": list(state["graveyard_cards"].values()),
                "event_log":       state["event_log"][-2000:],
                "my_hand":         list(state["my_hand"]),
                "mulligan_eval":   _get_mulligan_eval(state["my_hand"]),
                "my_battlefield":  list(state["my_battlefield"]),
                "opp_battlefield": list(state["opp_battlefield"]),
                "phase":           state["phase"],
                "turn":            state["turn"],
                "my_life":         state["my_life"],
                "opp_life":        state["opp_life"],
                "last_update":     state["last_update"],
                "match_game":      state["match_game"],
                "my_seat":         state["my_seat"],
                "generation":      state["generation"],
            }
        payload = json.dumps(snap).encode()
        req = urllib.request.Request(
            f"{FIREBASE_URL}/state.json",
            data=payload, method="PUT",
            headers={"Content-Type": "application/json",
                     "User-Agent":   "MTGArchetypeDetector/1.0"}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"  [WARN] Firebase sync failed: {e}")

reset_hold_until = 0.0

def push_loop():
    global reset_hold_until
    while True:
        try:
            req = urllib.request.Request(
                f"{FIREBASE_URL}/reset_requested.json",
                headers={"User-Agent": "MTGArchetypeDetector/1.0"}
            )
            resp = urllib.request.urlopen(req, timeout=3)
            if json.loads(resp.read()) is True:
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        f"{FIREBASE_URL}/reset_requested.json",
                        data=b"false", method="PUT",
                        headers={"Content-Type": "application/json",
                                 "User-Agent": "MTGArchetypeDetector/1.0"}
                    ), timeout=3)
                except Exception:
                    pass
                with lock:
                    hard_reset_state("browser_reset")
                push_to_firebase()
                reset_hold_until = time.time() + 12
                time.sleep(2)
                continue
        except Exception:
            pass
        if time.time() >= reset_hold_until:
            push_to_firebase()
        time.sleep(2)

# ── Card cache ────────────────────────────────────────────────────────────────
SCRYFALL_BULK = Path(__file__).parent / "scryfall_arena.json"

def load_bulk():
    if SCRYFALL_BULK.exists():
        try:
            data = json.loads(SCRYFALL_BULK.read_text(encoding="utf-8"))
            with lock:
                for card in data:
                    aid  = card.get("arena_id")
                    name = card.get("name")
                    if aid and name:
                        state["grp_map"][aid] = name
            print(f"Loaded {len(state['grp_map'])} cards from cache.")
            return
        except Exception:
            pass
    print("No local card cache — run build_cache.py to pre-populate.")
    print("Cards will be looked up via Scryfall as they appear.\n")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 52)
    print("  MTG Arena Watcher  —  Event-Sourced Edition")
    print("=" * 52)
    load_bulk()
    print(f"Player: {PLAYER_NAME}")
    print(f"Firebase: {FIREBASE_URL}")
    print(f"Open: https://mtg-archetype-detector.pages.dev\n")
    threading.Thread(target=watch_log, daemon=True).start()
    threading.Thread(target=push_loop, daemon=True).start()
    while True:
        time.sleep(60)
