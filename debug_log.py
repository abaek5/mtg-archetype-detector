"""
Run this DURING a match to see exactly what Arena is writing to the log.
It prints every new line so you can see what patterns to match.

Usage: python debug_log.py
"""
import os
import time
from pathlib import Path

LOG_PATH = Path(os.path.expandvars(
    r"%APPDATA%\..\LocalLow\Wizards of the Coast\MTGA\Player.log"
))

print(f"Reading: {LOG_PATH}")
print(f"Exists: {LOG_PATH.exists()}")
print(f"Size: {LOG_PATH.stat().st_size if LOG_PATH.exists() else 'N/A'} bytes")
print("-" * 60)
print("Tailing log — play some cards and watch what appears...")
print("Press Ctrl+C to stop.\n")

KEYWORDS = [
    "cardName", "grpId", "GRE", "battlefield", "hand",
    "ZoneTransfer", "ownerSeatId", "Cast", "Play",
    "seatId", "instanceId", "zoneSrc", "zoneDst",
    "Battlefield", "Hand", "Stack", "Graveyard",
]

with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
    f.seek(0, 2)  # go to end
    print("Waiting for new log lines...\n")
    buf = ""
    while True:
        chunk = f.read(4096)
        if chunk:
            buf += chunk
            lines = buf.split("\n")
            buf = lines[-1]
            for line in lines[:-1]:
                # print lines containing any keyword
                if any(k.lower() in line.lower() for k in KEYWORDS):
                    print(line.rstrip())
        else:
            time.sleep(0.3)
