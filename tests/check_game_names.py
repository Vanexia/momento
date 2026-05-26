"""Pretty-print humanise_game_name() for a sample of exes (table + fallback)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from momento.core.game_names import humanise_game_name  # noqa: E402

CASES = [
    # Table hits
    "eldenring.exe", "ffxiv_dx11.exe", "Wow.exe", "LiesofP.exe",
    "valorant.exe", "cs2.exe", "BG3.exe", "RDR2.exe", "GTA5.exe",
    "Witcher3.exe", "HogwartsLegacy.exe", "ACShadows.exe",
    "FortniteClient-Win64-Shipping.exe", "r5apex.exe", "TslGame.exe",
    "Cyberpunk2077.exe", "RocketLeague.exe", "Hades2.exe",
    "Helldivers2.exe", "MarvelRivals-Win64-Shipping.exe",
    "BlackMythWukong.exe", "Satisfactory-Win64-Shipping.exe",
    "Diablo IV.exe", "PathofExile2.exe", "GenshinImpact.exe",
    "BlackOps6.exe", "Sea Of Thieves.exe",
    # Fallback (not in table)
    "MyCoolIndieGame.exe", "supersecret-Win64-Shipping.exe",
    "weird_name_v2.exe", "ALLCAPS.exe", "lower.exe",
]

w = max(len(c) for c in CASES) + 2
for c in CASES:
    print(f"  {c:<{w}}  ->  {humanise_game_name(c)}")
