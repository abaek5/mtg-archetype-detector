#!/usr/bin/env python3
"""
MTG Arena Watcher — generation-scoped, stale-packet resistant.
Fixes: pending graveyard resolution, age-based GC, chronological cast list,
       preserved event log across resets, configurable player name.
"""

import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

# ── Scryfall rate limiting ────────────────────────────────────────────────────
scryfall_semaphore = threading.Semaphore(2)   # max 2 concurrent requests
failed_grp_ids     = set()                    # 404s — never retry
pending_lookups    = set()                    # in-flight — never duplicate

# ── Scryfall rate limiting ────────────────────────────────────────────────────
scryfall_semaphore = threading.Semaphore(2)
failed_grp_ids     = set()
pending_lookups    = set()

# ── Config ────────────────────────────────────────────────────────────────────
FIREBASE_URL  = "https://mtg-detector-40285-default-rtdb.firebaseio.com"
PLAYER_NAME   = os.environ.get("MTGA_PLAYER_NAME", "RagingDachshund")
LOG_PATH      = Path(os.path.expandvars(
    r"%APPDATA%\..\LocalLow\Wizards of the Coast\MTGA\Player.log"
))
SKIP_NAMES = {"", "Plains", "Island", "Swamp", "Mountain", "Forest"}

# ── Mulligan evaluator ───────────────────────────────────────────────────────
def evaluate_hand(hand, on_draw=False, matchup="unknown"):
    """
    Generic rule-based mulligan evaluator — works for any MTG deck.
    Uses universal principles: land count, early plays, spell density.
    Returns (decision, score, reasons)
    """
    # Universal land detection — covers all common land name patterns
    land_patterns = [
        "Forest","Island","Swamp","Mountain","Plains",
        "Passage","Castle","Boseiju","Nykthos","Pathway",
        "Clearing","Grove","Triome","Tarn","Delta","Marsh",
        "Shore","Depths","Sanctum","Cavern","Haven","Hub",
        "Spire","Garden","Pool","Tomb","Crypt","Vault",
        "Gate","Vale","Ridge","Strand","Foothills","Fetch",
        "Shock","Dual","Check","Fast","Pain","Filter",
        "Horizon","Scrubland","Savannah","Taiga","Badlands",
        "Plateau","Volcanic","Tundra","Bayou","Tropical",
    ]
    lands = [c for c in hand if any(p.lower() in c.lower() for p in land_patterns)]
    spells = [c for c in hand if c not in lands]

    land_count  = len(lands)
    spell_count = len(spells)
    reasons     = []
    score       = 0

    # ── Universal land rules ──
    if land_count == 0:
        return "MULLIGAN", -10, ["No lands — automatic mulligan"]

    if land_count == 1:
        score -= 3
        reasons.append("1 land — very risky opener")

    elif land_count == 2:
        score += 1
        reasons.append("2 lands — functional if spells are cheap")

    elif land_count == 3:
        score += 3
        reasons.append("3 lands — ideal land count")

    elif land_count == 4:
        score += 1
        reasons.append("4 lands — slightly heavy")

    elif land_count >= 5:
        score -= 2
        reasons.append(f"{land_count} lands — flood risk")

    # ── Spell density ──
    if spell_count == 0:
        score -= 3
        reasons.append("All lands — no action")

    elif spell_count >= 4:
        score += 1
        reasons.append("Spell-rich hand")

    # ── 1-drop presence (universal aggro/tempo signal) ──
    # Detect likely 1-drops by checking grpMap or common naming
    # Since we don't have mana costs here, use heuristic: short card names tend to be cheap
    short_names = [c for c in spells if len(c) <= 12]
    if short_names:
        score += 1
        reasons.append("Likely early play available")

    # ── Dead hand check ──
    if land_count <= 2 and spell_count <= 1:
        score -= 3
        reasons.append("Too few spells with low land count")

    # ── On draw adjustment ──
    if on_draw:
        score += 0.5
        reasons.append("On draw: slightly looser keep")

    # ── Aggro matchup ──
    if matchup in ("aggro", "Mono-Red Aggro", "Burn", "Mono-White Lifegain"):
        if land_count >= 4:
            score -= 1
            reasons.append("Heavy hand vs aggro — risky")

    # ── Final decision ──
    decision = "KEEP" if score >= 2 else "MULLIGAN"
    return decision, score, reasons

# ── Shared state ──────────────────────────────────────────────────────────────
lock = threading.Lock()
state = {
    "generation":   0,
    "match_id":     None,
    "match_start":  0,

    # Bug #4 fix: list (not dict) for stable chronological order
    "all_cast_cards": [],
    "cast_seen":      set(),   # (generation, iid) — prevent exact replay only

    "opp_graveyard":   set(),
    "graveyard_cards": {},     # (generation, iid) -> card info

    # Bug #7 fix: large event log
    "event_log":       [],
    "next_event_id":   0,

    "my_hand":        [],
    "my_battlefield": [],
    "opp_battlefield":[],

    "phase":        "",
    "turn":         0,
    "my_life":      20,
    "opp_life":     20,
    "last_update":  0,
    "match_game":   1,

    "grp_map":      {},
    "instance_map": {},
    "zone_map":     {},   # persisted across resets — zone IDs are stable

    "my_seat":      0,
    "reset_time":   0,
    "ignored_generations": set(),
}

# ── Hard reset ────────────────────────────────────────────────────────────────
def hard_reset_state(reason="manual"):
    old_gen = state["generation"]
    state["ignored_generations"].add(old_gen)
    state["generation"] += 1
    gen = state["generation"]

    state["all_cast_cards"] = []
    state["cast_seen"]      = set()

    state["opp_graveyard"]  = set()

    # Bug #2 fix: preserve graveyard_cards from current generation, don't wipe
    state["graveyard_cards"] = {
        k: v for k, v in state["graveyard_cards"].items()
        if v.get("generation") == gen
    }

    state["instance_map"]   = {}
    # zone_map intentionally preserved — zone IDs are stable per Arena session

    state["my_hand"]        = []
    state["my_battlefield"] = []
    state["opp_battlefield"]= []

    state["phase"]          = ""
    state["turn"]           = 0
    state["my_life"]        = 20
    state["opp_life"]       = 20
    state["my_seat"]        = 0

    # Bug #2 fix: preserve event log (trim, don't clear)
    state["event_log"]      = state["event_log"][-500:]

    state["match_start"]    = time.time()
    state["reset_time"]     = time.time()
    state["last_update"]    = time.time()

    print(f"  [RESET] generation={gen} reason={reason}")

# ── Scryfall lookup ───────────────────────────────────────────────────────────
def lookup_grp(grp_id: int):
    with lock:
        if grp_id in state["grp_map"]:  return   # already resolved
        if grp_id in failed_grp_ids:    return   # known 404
        if grp_id in pending_lookups:   return   # already in flight
        pending_lookups.add(grp_id)

    def _fetch():
        try:
            with scryfall_semaphore:
                time.sleep(0.1)  # 100ms between requests per slot
                req = urllib.request.Request(
                    f"https://api.scryfall.com/cards/arena/{grp_id}",
                    headers={"User-Agent": "MTGArchetypeDetector/1.0",
                             "Accept": "application/json"}
                )
                try:
                    data = urllib.request.urlopen(req, timeout=6).read()
                except urllib.error.HTTPError as he:
                    if he.code == 404:
                        with lock:
                            failed_grp_ids.add(grp_id)
                            pending_lookups.discard(grp_id)
                        return
                    elif he.code == 429:
                        time.sleep(5)   # back off on rate limit
                        with lock:
                            pending_lookups.discard(grp_id)
                        return
                    raise
            obj  = json.loads(data)
            name = obj.get("name", "")
            if not name:
                with lock:
                    failed_grp_ids.add(grp_id)
                    pending_lookups.discard(grp_id)
                return
            with lock:
                state["grp_map"][grp_id] = name
                print(f"  [RESOLVED] grp={grp_id} -> {name}")
                gen      = state["generation"]
                my_seat  = state["my_seat"]
                opp_seat = (1 if my_seat == 2 else 2) if my_seat != 0 else 0

                for iid, info in state["instance_map"].items():
                    if info.get("grpId") != grp_id:
                        continue
                    if info.get("generation") != gen:
                        continue
                    info["name"] = name

                    # Resolve pending cast
                    if info.get("pending_add"):
                        info["pending_add"] = False
                        owner  = info.get("owner")
                        ctypes = info.get("cardTypes", [])
                        token  = info.get("token", False)
                        if "CardType_Land" not in ctypes and not token and name not in SKIP_NAMES:
                            cast_key = (gen, iid)
                            if cast_key not in state["cast_seen"]:
                                state["cast_seen"].add(cast_key)
                                state["all_cast_cards"].append({
                                    "name": name, "owner": owner,
                                    "iid": iid, "generation": gen,
                                    "turn": state["turn"],
                                    "event_id": state["next_event_id"],
                                })
                                state["next_event_id"] += 1
                                state["last_update"] = time.time()
                                print(f"  [LATE ] seat={owner}: {name}")

                    # Bug #1 fix: resolve pending graveyard
                    if info.get("pending_graveyard"):
                        info["pending_graveyard"] = False
                        owner = info.get("owner")
                        gy_key = (gen, iid)
                        if gy_key not in state["graveyard_cards"]:
                            state["graveyard_cards"][gy_key] = {
                                "name": name, "owner": owner,
                                "turn": state["turn"],
                                "source": "mill_pending_resolve",
                                "generation": gen,
                            }
                            if opp_seat != 0 and owner == opp_seat:
                                state["opp_graveyard"].add(name)
                                print(f"  [GRAVE] (mill-resolved) seat={owner}: {name}")
                            state["event_log"].append({
                                "id": state["next_event_id"],
                                "generation": gen,
                                "turn": state["turn"],
                                "event": "graveyard",
                                "card": name, "owner": owner,
                                "source": "mill_pending_resolve",
                            })
                            state["next_event_id"] += 1
                            state["last_update"] = time.time()
        except Exception as e:
            print(f"  [ERR  ] Scryfall lookup grp={grp_id}: {e}")
        finally:
            with lock:
                pending_lookups.discard(grp_id)
    threading.Thread(target=_fetch, daemon=True).start()

# ── Game state parser ─────────────────────────────────────────────────────────
def parse_game_state(msg: dict):
    gm = msg.get("gameStateMessage", {})
    if not gm:
        return

    with lock:
        packet_generation = state["generation"]
        my_seat  = state["my_seat"]
        opp_seat = (1 if my_seat == 2 else 2) if my_seat != 0 else 0

        # Build zone_map FIRST — needed for ZoneTransfer annotation processing
        for z in gm.get("zones", []):
            zid   = z.get("zoneId")
            ztype = z.get("type", "")
            if zid and ztype:
                state["zone_map"][zid] = ztype.replace("ZoneType_", "")

        # Turn info
        ti = gm.get("turnInfo", {})
        cur_turn = ti.get("turnNumber", 0)

        # Detect new game within match
        if cur_turn == 1 and state["turn"] >= 2:
            state["match_game"] += 1
            if state["match_game"] > 3:
                state["match_game"] = 1
            hard_reset_state("new_game")
            # Shorter reset window for in-match game transitions
            state["reset_time"] = time.time() - 2  # only 1 second block
            return

        if cur_turn:
            state["turn"] = cur_turn

        phase = ti.get("phase", "")
        step  = ti.get("step", "")
        if phase:
            state["phase"] = phase.replace("Phase_", "").replace("Step_", "")
        if step:
            state["phase"] += f" {step.replace('Step_', '')}"

        # Life totals
        if my_seat != 0:
            for p in gm.get("players", []):
                seat = p.get("systemSeatNumber")
                life = p.get("lifeTotal")
                if life is None:
                    continue
                if seat == my_seat:
                    state["my_life"] = life
                elif seat == opp_seat:
                    state["opp_life"] = life

        now = time.time()

        # Update instance map from gameObjects — generation-scoped
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
            token  = obj.get("isToken", False) or obj.get("type", "") == "GameObjectType_Token"
            if not iid:
                continue
            name = state["grp_map"].get(grpid)
            existing = state["instance_map"].get(iid, {})
            inferred_zone = state["zone_map"].get(zone, existing.get("zone_type", ""))
            state["instance_map"][iid] = {
                "generation":       packet_generation,
                "last_seen":        now,
                "grpId":            grpid,
                "name":             name,
                "owner":            owner,
                "zoneId":           zone,
                "zone_type":        inferred_zone,
                "tapped":           tapped,
                "power":            power,
                "toughness":        tough,
                "cardTypes":        ctypes,
                "token":            token,
                "pending_add":      existing.get("pending_add", False),
                "pending_graveyard":existing.get("pending_graveyard", False),
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
                        state["instance_map"][iid]["last_seen"] = now
                    else:
                        state["instance_map"][iid] = {
                            "generation": packet_generation, "last_seen": now,
                            "zone_type": "Battlefield", "owner": owner}

            elif ztype == "ZoneType_Hand":
                # Track all hands — my_seat used later in rebuild
                for iid in iids:
                    if iid in state["instance_map"]:
                        state["instance_map"][iid]["zone_type"] = "Hand"
                        state["instance_map"][iid]["last_seen"] = now
                    else:
                        state["instance_map"][iid] = {
                            "generation": packet_generation, "last_seen": now,
                            "zone_type": "Hand", "owner": owner, "name": None}

            elif ztype == "ZoneType_Graveyard" and my_seat != 0 and owner == opp_seat:
                for iid in iids:
                    if iid in state["instance_map"]:
                        state["instance_map"][iid]["zone_type"] = "Graveyard"
                        state["instance_map"][iid]["last_seen"] = now
                    else:
                        state["instance_map"][iid] = {
                            "generation": packet_generation, "last_seen": now,
                            "zone_type": "Graveyard", "owner": opp_seat, "name": None}

        # ZoneTransfer annotations — detect ALL cards entering graveyard (including mill)
        for ann in gm.get("annotations", []):
            if "AnnotationType_ZoneTransfer" not in ann.get("type", []):
                continue
            details = {d["key"]: d for d in ann.get("details", [])}
            src_id  = (details.get("zone_src",  {}).get("valueInt32", [None]) or [None])[0]
            dst_id  = (details.get("zone_dest", {}).get("valueInt32", [None]) or [None])[0]
            src_type = state["zone_map"].get(src_id, "") if src_id is not None else ""
            dst_type = state["zone_map"].get(dst_id, "") if dst_id is not None else ""
            if dst_type != "Graveyard":
                continue
            is_mill = src_type == "Library"
            for iid in ann.get("affectedIds", []):
                info  = state["instance_map"].get(iid, {})
                owner = info.get("owner")
                grpid = info.get("grpId")
                name  = info.get("name") or state["grp_map"].get(grpid)

                # Bug #1 fix: queue pending graveyard resolution if name unknown
                if not name:
                    if grpid:
                        lookup_grp(grpid)
                        state["instance_map"][iid] = {
                            **info,
                            "pending_graveyard": True,
                            "generation": packet_generation,
                            "last_seen": now,
                        }
                    continue

                if name in SKIP_NAMES:
                    continue

                source = "mill" if is_mill else "other"
                gy_key = (packet_generation, iid)
                if gy_key not in state["graveyard_cards"]:
                    state["graveyard_cards"][gy_key] = {
                        "name": name, "owner": owner,
                        "turn": state["turn"], "source": source,
                        "generation": packet_generation,
                    }
                    if my_seat != 0 and opp_seat != 0 and owner == opp_seat:
                        state["opp_graveyard"].add(name)
                        print(f"  [GRAVE] ({source}) seat={owner}: {name}")
                    state["event_log"].append({
                        "id": state["next_event_id"],
                        "generation": packet_generation,
                        "turn": state["turn"],
                        "event": "graveyard",
                        "card": name, "owner": owner, "source": source,
                    })
                    state["next_event_id"] += 1

        # CastSpell annotations
        if my_seat != 0:
            for ann in gm.get("annotations", []):
                if "AnnotationType_ZoneTransfer" not in ann.get("type", []):
                    continue
                details  = {d["key"]: d for d in ann.get("details", [])}
                category = details.get("category", {}).get("valueString", [""])[0]
                if category != "CastSpell":
                    continue
                for iid in ann.get("affectedIds", []):
                    info   = state["instance_map"].get(iid, {})
                    if info.get("generation") != packet_generation:
                        continue
                    grpid  = info.get("grpId")
                    name   = info.get("name") or state["grp_map"].get(grpid)
                    ctypes = info.get("cardTypes", [])
                    token  = info.get("token", False)
                    owner  = info.get("owner")
                    if "CardType_Land" in ctypes or token:
                        continue
                    if name and name not in SKIP_NAMES:
                        cast_key = (packet_generation, iid)
                        if cast_key not in state["cast_seen"]:
                            state["cast_seen"].add(cast_key)
                            state["all_cast_cards"].append({
                                "name": name, "owner": owner,
                                "iid": iid, "generation": packet_generation,
                                "turn": state["turn"],
                                "event_id": state["next_event_id"],
                            })
                            state["event_log"].append({
                                "id": state["next_event_id"],
                                "generation": packet_generation,
                                "turn": state["turn"],
                                "event": "cast",
                                "card": name, "owner": owner,
                            })
                            state["next_event_id"] += 1
                            state["last_update"] = time.time()
                            print(f"  [CAST ] seat={owner}: {name}")
                    elif grpid:
                        print(f"  [QUEUE] grp={grpid} not resolved, looking up...")
                        info["pending_add"] = True
                        lookup_grp(grpid)

        # Rebuild hand and battlefields — reject stale instances
        my_hand, my_bf, opp_bf = [], [], []
        hand_instances = [(iid, info) for iid, info in state["instance_map"].items()
                         if info.get("zone_type") == "Hand" and info.get("generation") == packet_generation]
        for iid, info in state["instance_map"].items():
            if info.get("generation") != packet_generation:
                continue
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
            if my_seat == 0:
                continue
            # For unresolved cards, trigger lookup
            if not name:
                grpid = info.get("grpId")
                if grpid:
                    resolved = state["grp_map"].get(grpid)
                    if resolved:
                        info["name"] = resolved
                        name = resolved
                    else:
                        lookup_grp(grpid)
            if not name:
                continue
            if zt == "Hand" and owner == my_seat and not is_token:
                my_hand.append(name)
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

        # Bug #3 fix: age-based GC only — don't remove by active_iids (packets are partial)
        now2 = time.time()
        remove = [
            iid for iid, info in state["instance_map"].items()
            if info.get("generation") != packet_generation
            or now2 - info.get("last_seen", now2) > 120
        ]
        for iid in remove:
            state["instance_map"].pop(iid, None)

# ── Seat detection ────────────────────────────────────────────────────────────
def detect_seat(line: str):
    if "systemSeatId" not in line or "playerName" not in line:
        return
    for m in re.finditer(r'"playerName"\s*:\s*"([^"]+)"\s*,\s*"systemSeatId"\s*:\s*(\d+)', line):
        name = m.group(1)
        seat = int(m.group(2))
        if name == PLAYER_NAME and seat in (1, 2):
            with lock:
                if state["my_seat"] != seat:
                    state["my_seat"] = seat
                    print(f"  [SEAT ] You are seat {seat}, opponent is seat {3-seat}")
            return

# ── Firebase sync ─────────────────────────────────────────────────────────────
def _get_mulligan_eval(hand):
    if not hand or len(hand) < 5:
        return None
    decision, score, reasons = evaluate_hand(hand)
    return {"decision": decision, "score": score, "reasons": reasons}

def push_to_firebase():
    try:
        with lock:
            # Bug #6 fix: include both opp_graveyard and graveyard_cards
            gy_names    = sorted(list(state["opp_graveyard"]))
            hand_copy   = list(state["my_hand"])
            cast_copy   = list(state["all_cast_cards"])
            gy_cards    = list(state["graveyard_cards"].values())
            evlog_copy  = state["event_log"][-2000:]
            snap = {
                "all_cast_cards":  cast_copy,
                "opp_graveyard":   gy_names,
                "graveyard_cards": gy_cards,
                "event_log":       evlog_copy,
                "my_hand":         hand_copy,
                "mulligan_eval":   _get_mulligan_eval(hand_copy),
                "my_battlefield":  state["my_battlefield"],
                "opp_battlefield": state["opp_battlefield"],
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
        import traceback
        print(f"  [WARN] Firebase sync failed: {e}")
        traceback.print_exc()

reset_hold_until = 0

def push_loop():
    global reset_hold_until
    while True:
        try:
            req = urllib.request.Request(
                f"{FIREBASE_URL}/reset_requested.json",
                headers={"User-Agent": "MTGArchetypeDetector/1.0"}
            )
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read())
            if data is True:
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

# ── Log watcher ───────────────────────────────────────────────────────────────
def parse_chunk(text: str):
    for line in text.split("\n"):
        if not line.strip():
            continue
        if "playerName" in line and "systemSeatId" in line:
            detect_seat(line)
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
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
    print("No local card cache — looking up cards via Scryfall as they appear.")
    try:
        opener = urllib.request.build_opener()
        opener.addheaders = [("User-Agent", "MTGArchetypeDetector/1.0")]
        meta = json.loads(opener.open("https://api.scryfall.com/bulk-data", timeout=10).read())
        bulk_data = meta.get("data", []) if isinstance(meta, dict) else meta
        url  = next((b["download_uri"] for b in bulk_data
                     if b.get("type") == "default_cards"), None)
        if url:
            print("Downloading card database...")
            with opener.open(url, timeout=60) as r:
                raw = json.loads(r.read())
            arena = [{"arena_id": c.get("arena_id"), "name": c["name"]}
                     for c in raw if c.get("arena_id")]
            SCRYFALL_BULK.write_text(json.dumps(arena), encoding="utf-8")
            with lock:
                for c in arena:
                    state["grp_map"][c["arena_id"]] = c["name"]
            print(f"Cached {len(arena)} Arena cards.")
    except Exception as e:
        print(f"Background download failed: {e}")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 52)
    print("  MTG Arena Watcher  —  Generation-Scoped Edition")
    print("=" * 52)
    load_bulk()
    print(f"Player: {PLAYER_NAME}")
    print(f"Firebase: {FIREBASE_URL}")
    print(f"Open: https://mtg-archetype-detector.pages.dev\n")
    threading.Thread(target=watch_log, daemon=True).start()
    threading.Thread(target=push_loop, daemon=True).start()
    while True:
        time.sleep(60)
