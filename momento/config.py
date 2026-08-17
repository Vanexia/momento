"""Momento's configuration schema and JSON persistence."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import ClassVar

from momento.util.paths import config_path, default_output_folder

logger = logging.getLogger(__name__)


def _dedupe_casefolded(values) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


# Curated default list. ~350 popular PC games circa 2010-2026 across all the
# major genres. Coverage is intentionally generous — false positives are
# harmless (recording a launcher splash you didn't want = manual delete),
# whereas a missed game means lost gameplay. Users still add their own in
# Settings and can flip "record any fullscreen" for a catch-all.
#
# Matched case-insensitively by executable basename.
DEFAULT_KNOWN_GAMES: tuple[str, ...] = (
    # ------ FromSoftware & soulslikes ------
    "eldenring.exe", "DarkSoulsIII.exe", "DarkSoulsII.exe", "DarkSouls.exe",
    "Sekiro.exe", "Bloodborne.exe",
    "Nioh2.exe", "Nioh.exe", "Wo Long.exe", "WoLongFallenDynasty.exe",
    "LiesofP.exe", "code_vein.exe", "remnant2.exe", "remnantfromtheashes.exe",
    "lordsofthefallen.exe", "lotf2.exe", "thesurge.exe", "thesurge2.exe",
    "Mortal Shell.exe", "ashenexe.exe", "salt-and-sacrifice.exe",
    "blasphemous.exe", "blasphemous2.exe", "stray-blade.exe",
    # ------ MMO ------
    "ffxiv_dx11.exe", "ffxiv.exe", "ffxiv_boot.exe", "ffxivlauncher.exe",
    "Wow.exe", "WowClassic.exe",
    "GW2-64.exe", "Gw2.exe", "Gw2-64.exe", "Guild Wars 2.exe",
    "ESO.exe", "eso64.exe", "EsoLauncher.exe",
    "BlackDesert64.exe", "BlackDesertLauncher.exe", "Lost Ark.exe", "lostark.exe",
    "RuneLite.exe", "JagexLauncher.exe", "OldSchool.exe", "osrs.exe",
    "newworld.exe", "TERA.exe", "EverQuest.exe", "RIFT.exe",
    "Aion.exe", "AionLauncher.exe", "Star Citizen.exe", "StarCitizen.exe",
    "Throne and Liberty.exe", "throne-and-liberty.exe",
    "swkotor.exe", "swtor.exe", "DCUOLauncher.exe",
    "Final Fantasy XI.exe", "Project Gorgon.exe", "PathofExile.exe",
    "PathofExileSteam.exe", "PathofExile_x64Steam.exe", "PathofExile2.exe",
    "Lineage II.exe", "Mabinogi.exe", "Vindictus.exe",
    # ------ MOBA / arena ------
    # NOTE: only Riot's GAMEPLAY exes belong here. RiotClientServices.exe is
    # the always-running launcher service and LeagueClient.exe is the lobby —
    # both linger in the background and triggered recordings of a launcher
    # window (see _AUTO_PRUNE_GAMES, which strips them from saved configs).
    "League of Legends.exe",
    "Dota2.exe", "HotS.exe", "Smite.exe", "Smite2.exe",
    "Pokemon Unite.exe", "PredecessorClient-Win64-Shipping.exe",
    "Deadlock.exe", "project8.exe",
    # ------ Shooters / battle royale / extraction / hero shooter ------
    "valorant.exe", "VALORANT-Win64-Shipping.exe", "VALORANT-Win64-Test.exe",
    "cs2.exe", "csgo.exe",
    "FortniteClient-Win64-Shipping.exe", "FortniteLauncher.exe",
    "r5apex.exe", "r5apex_dx12.exe", "EasyAntiCheat_launcher.exe",
    "PUBG.exe", "TslGame.exe", "PUBGLite.exe",
    "DestinyLauncher.exe", "destiny2.exe",
    "Overwatch.exe",
    "RainbowSix.exe", "RainbowSix_Vulkan.exe", "RainbowSixSiege.exe",
    "ModernWarfare.exe", "ModernWarfare2.exe", "ModernWarfare3.exe",
    "CoDMW.exe", "CoDMW2.exe", "cod.exe", "BlackOps6.exe", "BlackOps4.exe",
    "BlackOpsColdWar.exe", "Vanguard.exe", "iw7_ship.exe", "iw8_ship.exe",
    "Warzone.exe",
    "Battlefield 2042.exe", "BF2042.exe", "bf1.exe", "BF1.exe",
    "BattlefieldV.exe", "bfv.exe", "Battlefield4.exe", "bf4.exe",
    "Battlefield2.exe",
    "MarvelRivals_Launcher.exe", "MarvelRivals.exe", "MarvelRivals-Win64-Shipping.exe",
    "thefinals.exe", "Discovery-Win64-Shipping.exe",
    "deltaforce.exe", "DeltaForce-Win64-Shipping.exe",
    "EscapeFromTarkov.exe", "EFT.exe", "EscapeFromTarkov_BE.exe",
    "ArenaBreakoutInfinite.exe",
    "Hunt.exe", "huntshowdown.exe", "Hunt-Win64-Shipping.exe",
    "GrayZoneWarfare.exe", "MWS-Win64-Shipping.exe",
    "DOOMEternalx64vk.exe", "DOOMEternal.exe", "DOOM.exe", "DOOMx64vk.exe",
    "DOOMTheDarkAges.exe",
    "TitanFall2.exe", "Titanfall2.exe",
    "ChivalryMW.exe", "Mordhau-Win64-Shipping.exe",
    "Splitgate.exe", "Splitgate2.exe", "QuakeChampions.exe",
    "halo.exe", "MCC-Win64-Shipping.exe", "haloreach.exe",
    "haloinfinite.exe", "HaloInfinite.exe",
    "PaladinsClient.exe", "PlanetSide2.exe", "Warframe.exe", "Warframe.x64.exe",
    "Helldivers2.exe", "Helldivers2-Win64-Shipping.exe",
    "ReadyOrNot-Win64-Shipping.exe", "GroundBranch-Win64-Shipping.exe",
    "Squad.exe", "PostScriptum.exe", "Hell Let Loose.exe",
    "HLL-Win64-Shipping.exe", "EnlistedClient-Win64-Shipping.exe",
    "WarThunder.exe", "WoT.exe", "WoWs.exe", "WorldOfWarships.exe",
    "WorldOfTanks.exe",
    # ------ Survival / sandbox / crafting ------
    "Minecraft.Windows.exe", "javaw.exe", "MinecraftLauncher.exe",
    "VRChat.exe", "VRChat-Win64-Shipping.exe",
    "rust_client.exe", "RustClient.exe",
    "DayZ_x64.exe", "DayZLauncher.exe",
    "ARK.exe", "ShooterGame.exe", "ArkAscended.exe",
    "Palworld-Win64-Shipping.exe", "Palworld.exe",
    "valheim.exe", "Terraria.exe", "Stardew Valley.exe", "StardewValley.exe",
    "Satisfactory-Win64-Shipping.exe", "FactoryGame-Win64-Shipping.exe",
    "Subnautica.exe", "SubnauticaZero.exe",
    "TheForest.exe", "EndnightGame.exe", "SonsOfTheForest.exe", "SonsOfTheForest-Win64-Shipping.exe",
    "Grounded.exe", "Maine-Win64-Shipping.exe",
    "Project Zomboid64.exe", "ProjectZomboid64.exe",
    "TheLongDark.exe", "Green Hell.exe", "GreenHell.exe",
    "icarus.exe", "Icarus-Win64-Shipping.exe",
    "Enshrouded.exe", "Enshrouded-Win64-Shipping.exe",
    "NightingaleClient-Win64-Shipping.exe",
    "Soulmask-Win64-Shipping.exe",
    "Once Human.exe", "OnceHumanClient.exe",
    "7DaysToDie.exe", "Conan.exe", "ConanSandbox.exe",
    "Astroneer-Win64-Shipping.exe", "PortalKnights.exe",
    "Vintage Story.exe", "Vintagestory.exe",
    "no man's sky.exe", "NMS.exe", "NMSSE.exe",
    # ------ AAA action / open world ------
    "GTA5.exe", "GTAV.exe", "GTAVLauncher.exe", "GTA6.exe",
    "RDR2.exe", "RDR.exe", "MaxPayne3.exe",
    "Cyberpunk2077.exe", "Witcher3.exe", "witcher.exe", "witcher2.exe",
    "BG3.exe", "bg3_dx11.exe", "DivinityOriginalSin2.exe", "DOS2.exe",
    "DivinityOriginalSin.exe", "DivinityEngine2.exe",
    "Starfield.exe", "Fallout4.exe", "Fallout76.exe", "Fallout3.exe",
    "FalloutNV.exe", "Fallout4VR.exe",
    "Skyrim.exe", "SkyrimSE.exe", "SkyrimVR.exe", "TESV.exe",
    "Oblivion.exe", "OblivionRemastered.exe", "Oblivion-Win64-Shipping.exe",
    "HogwartsLegacy.exe", "HogwartsLegacy_DX12.exe",
    "Spider-Man.exe", "Spider-Man2.exe", "Miles.exe",
    "GodOfWar.exe", "GoWR.exe", "GhostOfTsushima.exe",
    "AC-Mirage.exe", "ACShadows.exe", "AssassinsCreedShadows.exe",
    "ACOdyssey.exe", "ACValhalla.exe", "ACOrigins.exe", "ACBrotherhood.exe",
    "ACUnity.exe", "ACSyndicate.exe",
    "Dragons Dogma 2.exe", "DD2.exe", "DragonsDogma.exe",
    "MonsterHunterWilds.exe", "MHWilds.exe", "MHRise.exe", "MHWorld.exe",
    "DragonAge.exe", "DragonAgeInquisition.exe", "DragonAgeVeilguard.exe",
    "MassEffect.exe", "MassEffect2.exe", "MassEffect3.exe", "MELE.exe",
    "MassEffectAndromeda.exe", "MEALauncher.exe",
    "DyingLight.exe", "DyingLightGame.exe", "DyingLightGame_x64_rwdi.exe",
    "DyingLight2.exe",
    "DeadSpace.exe", "Callisto.exe", "Callisto-Win64-Shipping.exe",
    "ResidentEvil2.exe", "ResidentEvil3.exe", "ResidentEvil4.exe",
    "ResidentEvil7.exe", "ResidentEvilVillage.exe", "re2.exe", "re3.exe",
    "re4.exe", "re7.exe", "re8.exe",
    "ReturnalSteam.exe", "Returnal.exe",
    "ControlGame.exe", "Control.exe", "ControlAlanWake.exe", "AlanWake2.exe",
    "Senua.exe", "HellbladeSenua.exe", "Hellblade2.exe",
    "DishonoredDeathOfTheOutsider.exe", "Dishonored2.exe", "Prey.exe",
    "Deathloop.exe",
    "MetalGearSolidV.exe", "mgsvtpp.exe", "MGSDelta.exe",
    "Indiana.exe", "IndianaJonesGreatCircle.exe",
    "BlackMyth_Wukong.exe", "BlackMythWukong.exe", "b1-Win64-Shipping.exe",
    "Stalker2.exe", "Stalker2-Win64-Shipping.exe",
    # ------ Racing / sim / sports ------
    "F1_24.exe", "F1_23.exe", "F124.exe", "F123.exe", "F1_22.exe", "F1_25.exe",
    "iRacingSim64DX12.exe", "iRacing.exe", "iRacingUI.exe",
    "AC2-Win64-Shipping.exe", "AssettoCorsa.exe", "acs.exe",
    "rfactor2.exe", "rFactor2.exe",
    "FlightSimulator.exe", "MicrosoftFlightSimulator.exe",
    "FlightSimulator2024.exe", "FS2024.exe",
    "EuroTruckSim2.exe", "ETS2.exe", "AmericanTruckSim.exe", "ATS.exe",
    "ForzaHorizon5.exe", "ForzaHorizon4.exe", "ForzaMotorsport.exe",
    "ForzaHorizon5Steam.exe", "ForzaHorizon4Steam.exe",
    "BeamNG.drive.exe", "BeamNG.exe", "BeamNG.tech.exe",
    "RocketLeague.exe",
    "TheCrew2.exe", "TheCrewMotorfest.exe",
    "FIFA23.exe", "FIFA24.exe", "FC24.exe", "FC25.exe", "EAFC.exe",
    "NBA2K24.exe", "NBA2K25.exe",
    "Madden24.exe", "Madden25.exe",
    "MLBTheShow.exe",
    "GranTurismo7.exe",
    "WRC.exe", "DirtRally2.0.exe", "DirtRally.exe", "EASportsWRC.exe",
    "Snowrunner.exe", "MudRunner.exe",
    "TonyHawkProSkater.exe", "THPS.exe",
    "Skater XL.exe", "skaterxl.exe", "Session.exe",
    # ------ Fighters ------
    "Tekken8.exe", "Tekken7.exe", "PolarisLauncher-Win64-Shipping.exe",
    "SF6.exe", "StreetFighter6.exe", "StreetFighterV.exe", "SFV.exe",
    "MK1.exe", "MK11.exe", "MortalKombat11.exe", "MortalKombat1.exe",
    "DBFighterZ.exe", "DragonBallSparkingZero.exe",
    "GuiltyGearStrive.exe", "GGStrive-Win64-Shipping.exe",
    "GranBlueFantasyVersusRising.exe", "GBVSR.exe",
    "Skullgirls.exe", "Brawlhalla.exe", "Multiversus.exe",
    "naruto-ultimate-ninja-storm.exe", "NSUNS4.exe",
    "InjusticeGAU2.exe", "Injustice2.exe",
    # ------ Strategy / 4X / city / management ------
    "civ7.exe", "civilizationvii.exe", "CivilizationVII.exe", "CivilizationVII_DX12.exe",
    "civilizationvi.exe", "Civ6.exe", "civilizationv.exe",
    "AoE2DE_s.exe", "AoE2DE.exe", "AoE4_s.exe", "AoE4.exe", "RelicCardinal.exe",
    "Stellaris.exe", "EU5.exe", "EU4.exe", "HOI4.exe", "CK3.exe", "CK2.exe",
    "Victoria3.exe", "Victoria2.exe", "Imperator.exe",
    "TotalWarWarhammer3.exe", "TotalWarWarhammer2.exe", "TotalWarWarhammer.exe",
    "Warhammer3.exe", "TotalWarRome2.exe", "TotalWarThreeKingdoms.exe",
    "Pharaoh.exe", "TroyTW.exe", "AttilaTW.exe", "Empire.exe",
    "TotalWarPharaohDynasties.exe",
    "CitiesSkylines2.exe", "Cities.exe", "CitiesSkylines.exe",
    "CitiesSkylinesII.exe", "Cities2-Win64-Shipping.exe",
    "RimWorldWin64.exe", "RimWorld.exe",
    "Frostpunk.exe", "Frostpunk2.exe",
    "TimberbornWin.exe", "Timberborn.exe",
    "Manor Lords.exe", "ManorLords-Win64-Shipping.exe",
    "DwarfFortress.exe", "Dwarf Fortress.exe",
    "Anno1800.exe", "Anno117.exe",
    "Tropico6.exe",
    "GalacticCivilizations.exe", "Spore.exe",
    "Northgard.exe", "Songs of Conquest.exe",
    "BannerLord.exe", "MountAndBladeBannerlord.exe", "Bannerlord.exe",
    "Mount&BladeWarband.exe",
    # ------ Roguelikes / metroidvanias / action-indie ------
    "Hades2.exe", "Hades.exe",
    "DeadCells.exe",
    "Hollow Knight.exe", "hollow_knight.exe", "Silksong.exe", "HollowKnightSilksong.exe",
    "BalatroGame.exe", "Balatro.exe",
    "Slay the Spire.exe", "SlayTheSpire.exe", "SlayTheSpire2.exe",
    "Risk of Rain 2.exe", "RoR2.exe", "RiskOfRain2.exe", "RiskOfRainReturns.exe",
    "Enter the Gungeon.exe", "EnterTheGungeon.exe",
    "Cult of the Lamb.exe", "CultOfTheLamb.exe",
    "Vampire Survivors.exe", "VampireSurvivors.exe",
    "BrotatoSteam.exe", "Brotato.exe",
    "Noita.exe",
    "Spelunky2.exe",
    "Loop Hero.exe", "LoopHero.exe",
    "Inscryption.exe",
    "DwarfFortressClassic.exe", "Caves of Qud.exe", "CavesOfQud.exe",
    "Backpack Hero.exe", "BackpackHero.exe",
    "Peglin.exe",
    "Tiny Tina's Wonderlands.exe", "Borderlands3.exe", "Borderlands2.exe",
    "BorderlandsTPS.exe", "Borderlands.exe", "borderlands4.exe",
    "BiomutantTOY.exe",
    # ------ Co-op / party / arcade ------
    "Among Us.exe", "AmongUs.exe",
    "Phasmophobia.exe", "Lethal Company.exe", "Lethal.exe", "lethal company.exe",
    "ContentWarning.exe", "ContentWarning-Win64-Shipping.exe",
    "REPO.exe", "R.E.P.O.exe",
    "Deep Rock Galactic.exe", "FSD-Win64-Shipping.exe",
    "FallGuys_client_game.exe", "FallGuys.exe",
    "GangBeasts.exe", "Human Fall Flat.exe",
    "ItTakesTwo.exe", "ItTakesTwoTrial.exe",
    "ASplitFiction.exe", "SplitFiction.exe", "SplitFiction-Win64-Shipping.exe",
    "RaftClient.exe", "Raft.exe",
    "Goose Game.exe", "Untitled Goose Game.exe",
    "PartyAnimals-Win64-Shipping.exe", "Overcooked2.exe", "Overcooked.exe",
    "PlateUp.exe",
    "PowerWashSimulator.exe", "HouseFlipper.exe",
    # ------ Visual / story / walking sims / immersive sims ------
    "Disco Elysium.exe", "DiscoElysium.exe",
    "Outer Wilds.exe", "OuterWilds.exe",
    "The Outer Worlds.exe", "OuterWorlds.exe", "TheOuterWorlds2.exe",
    "Returnal.exe", "ScornGame.exe",
    "Pentiment.exe",
    "TalosPrincipal.exe", "TalosPrincipal2.exe",
    "TwelveMinutes.exe",
    "What Remains of Edith Finch.exe",
    "Firewatch.exe",
    "Death Stranding.exe", "DSDirectorsCut.exe", "ds.exe",
    "Days Gone.exe", "DaysGone.exe",
    "The Last of Us Part I.exe", "tlou-i.exe", "tlou-ii.exe",
    "Persona5R.exe", "Persona5Royal.exe", "Persona3R.exe", "Persona4Golden.exe",
    "MetaphorReFantazio.exe", "Metaphor.exe",
    "Yakuza.exe", "Like a Dragon.exe", "LikeADragon.exe", "LikeADragonInfiniteWealth.exe",
    "Judgment.exe", "LostJudgment.exe",
    "FinalFantasyXVI.exe", "ff16.exe", "FinalFantasyXVI-Win64-Shipping.exe",
    "FinalFantasyVIIRemake.exe", "FinalFantasy7Rebirth.exe", "FF7Rebirth.exe",
    "NieRAutomata.exe", "NieRReplicant.exe",
    "OctopathTraveler.exe", "OctopathTraveler2.exe",
    "Sea Of Thieves.exe", "SoTGame.exe",
    "Atomfall.exe", "Avowed.exe",
    "Veilguard-Win64-Shipping.exe",
    # NOTE: launchers (Steam / Epic / EA / GOG / Ubisoft / Battle.net) are
    # intentionally NOT in this list. They're nearly always running in the
    # background, and tracking them caused a spurious "Couldn't record …"
    # toast at every Momento launch when the launcher's main window was
    # hidden/minimised. Their display names live in game_names.py for the
    # rare user who explicitly adds them by hand.
    # ------ Indie heavy hitters / popular smaller titles ------
    "Animal Well.exe", "AnimalWell.exe",
    "Dredge.exe",
    "TinyGlade.exe",
    "DaveTheDiver.exe",
    "PizzaTower.exe",
    "BomBRushCyberFunk.exe",
    "ChainedTogether.exe",
    "OnlyUp.exe",
    "PEAK.exe", "PEAK-Win64-Shipping.exe",
    "RustyLake.exe", "RustyLakeParadise.exe",
    "Stray.exe", "stray-Win64-Shipping.exe",
    "Spiritfarer.exe",
    "Coral Island.exe", "CoralIsland-Win64-Shipping.exe",
    "PlateUp.exe", "Webfishing.exe",
    "TUNIC.exe", "Cocoon.exe", "MicroMacro.exe",
    "AshenAxe.exe", "ConcordGame.exe",
    "Wuthering Waves.exe", "Wuthering Waves Game.exe", "Client-Win64-Shipping.exe",
    "GenshinImpact.exe", "YuanShen.exe", "GenshinImpact-Win64-Shipping.exe",
    "ZenlessZoneZero.exe", "ZZZ.exe", "ZZZ-Win64-Shipping.exe",
    "HonkaiStarRail.exe", "StarRail.exe", "StarRail-Win64-Shipping.exe",
    # ------ Older classics still played daily ------
    "Quake.exe", "Quake2.exe", "Quake3Arena.exe", "ioquake3.exe",
    "hl2.exe", "halflife.exe", "halflife2.exe", "halflifealyx.exe",
    "Portal.exe", "Portal2.exe",
    "L4D2.exe", "left4dead2.exe", "left4dead.exe",
    "tf_win64.exe", "tf2.exe",
    "GarrysMod.exe", "gmod.exe", "hl2_win64.exe",
    "Diablo IV.exe", "Diablo IV Launcher.exe", "Diablo III.exe", "Diablo II.exe",
    "Diablo II Resurrected.exe", "D2R.exe",
    "PathofExile_x64.exe",
    "TheBindingOfIsaac.exe", "isaac-ng.exe",
    "FTL.exe", "FasterThanLight.exe",
    "DyingLightGame.exe",
    "MountAndBlade.exe",
)
DEFAULT_KNOWN_GAMES = tuple(_dedupe_casefolded(DEFAULT_KNOWN_GAMES))


@dataclass
class Config:
    # Set only after the first-run wizard successfully persists its choices.
    # Existing pre-0.2 configs are migrated as complete in ``from_dict``.
    setup_complete: bool = False
    mic_device: str = ""
    system_audio_device: str = ""
    mic_volume_pct: int = 100
    system_volume_pct: int = 100
    output_folder: Path = field(default_factory=default_output_folder)
    autostart_with_windows: bool = False
    known_games: list[str] = field(default_factory=lambda: list(DEFAULT_KNOWN_GAMES))
    # Subset of ``known_games`` the user has toggled off. Entries here stay
    # visible in the Games settings page (so the user can flip them back on
    # without re-finding the exe) but are filtered out of the watcher's
    # match set, so they won't trigger auto-record. Case-insensitive.
    disabled_games: list[str] = field(default_factory=list)
    # Storage hygiene controls — see _enforce_storage_limit at startup +
    # after every recording. ``max_storage_gb`` = 0 means unlimited.
    max_storage_gb: int = 0
    # Free-space watermark below which Momento warns the user (in GiB).
    # 0 disables the warning.
    low_disk_warning_gb: int = 5
    # A fixed 60 fps is a dependable default for new users. Display-matched
    # capture remains available as an explicit choice in Settings.
    framerate: int = 60
    # When True, recording fps is taken from the primary monitor's refresh
    # rate at session start (clamped to [24, 240]) instead of ``framerate``.
    # Matches what most people actually want — record at the rate the game
    # is being displayed at, no thinking required.
    framerate_auto: bool = False
    # Output resolution preset. ``source`` (default) records at the game's
    # native window size. ``1080p`` / ``1440p`` / ``4k`` downscale during
    # encode; upscaling is never applied — if the source is smaller than
    # the target, the recorder falls back to ``source``.
    target_resolution: str = "source"
    # NVENC quality preset. Each maps to a constant-quality CQ value the
    # encoder uses. ``custom`` switches to CBR with ``custom_bitrate_kbps``.
    # See InProcessEncoder._build_video_options.
    quality_preset: str = "custom"
    # Bitrate (in kbit/s) used only when ``quality_preset == "custom"``.
    custom_bitrate_kbps: int = 16_000
    # Global hotkey for marking a bookmark while a game is being recorded.
    # Accepts e.g. "F8", "Ctrl+B", "Ctrl+Shift+M". Active only while recording.
    bookmark_hotkey: str = "F8"
    # When True, the watcher also records any foreground app that goes
    # fullscreen (window covers an entire monitor), even if its exe isn't in
    # ``known_games``. The known-games match still takes priority.
    record_any_fullscreen: bool = False
    # Branded overlay toasts in the top-left corner. Two independent toggles
    # so users can keep "recording started" (useful confirmation when a game
    # launches) while silencing "recording saved" (less critical, more
    # likely to land in the middle of a celebration). Not Windows
    # notifications — rendered by Momento, ignore DnD/Focus Assist.
    show_recording_started_toast: bool = True
    show_recording_saved_toast: bool = True
    # Couldn't-record warning. Defaults on because failures always need
    # user action (missing mic, output folder gone, ...) and silently
    # swallowing them leaves the user wondering why their session went
    # uncaptured.
    show_failure_toast: bool = True
    # OBS-style audio sync offset, in milliseconds. Negative shifts audio
    # earlier in the recording — compensates for WASAPI loopback's inherent
    # ~30-80 ms buffer latency vs WGC's ~16-33 ms compositor latency. Clamped
    # to [-1000, 1000].
    audio_offset_ms: int = -50
    # Plays a soft 200 ms chime when the bookmark hotkey lands. The toast
    # gives visual feedback; the chime is for when the user's eyes are on
    # the game. Note: the chime plays through the user's default output
    # so it WILL be captured by the system-audio loopback and end up in
    # the recording (matches OBS's behaviour with any notification sound).
    bookmark_sound: bool = True
    # Visual "Bookmark added @ M:SS" toast — separate from the chime so the
    # user can silence one without the other.
    show_bookmark_toast: bool = True
    # Which corner of the primary screen the toast appears in. One of
    # "top-left", "top-right", "bottom-left", "bottom-right".
    notification_position: str = "top-left"
    # When True, the editor's close button hides the window instead of
    # quitting. Quit is still available from the tray menu.
    close_to_tray: bool = True
    # When False, the game watcher is paused at app launch and the tray
    # menu's "Resume monitoring" must be used to start it. Lets the user
    # keep Momento ticking in the tray without auto-recording.
    start_monitoring_on_launch: bool = True
    # YouTube upload bridge (Phase 11). Defaults that pre-fill the upload
    # dialog; the user can override per-upload. ``youtube_channel_name`` /
    # ``youtube_channel_id`` are cached from the last successful auth so the
    # Settings tab can show "Signed in as: X" without a network call.
    # ``youtube_default_privacy`` is one of "public" / "unlisted" / "private";
    # unlisted is the safest default — link-only sharing, no public discovery,
    # no accidental "everyone sees my first upload" surprise.
    youtube_default_privacy: str = "unlisted"
    # YouTube category ID. 20 = Gaming. Full list:
    # https://developers.google.com/youtube/v3/docs/videoCategories/list
    youtube_default_category: int = 20
    # Comma-separated default tags appended to every upload. Empty = no tags.
    youtube_default_tags: str = ""
    # Cached display info from the last successful auth. Empty when not
    # connected. Cleared when the user clicks Disconnect.
    youtube_channel_name: str = ""
    youtube_channel_id: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["output_folder"] = str(self.output_folder)
        d["known_games"] = _dedupe_casefolded(self.known_games)
        d["disabled_games"] = _dedupe_casefolded(self.disabled_games)
        return d

    # Entries we silently strip from any loaded known_games list — these were
    # in earlier default lists but cause spurious "couldn't record" toasts
    # because they're nearly always running in the background.
    # ClassVar so @dataclass doesn't treat it as a field: without this it
    # ends up in asdict(), and json.dumps then crashes on the frozenset.
    _AUTO_PRUNE_GAMES: ClassVar[frozenset[str]] = frozenset({
        "steam.exe", "epicgameslauncher.exe", "ealauncher.exe", "eadesktop.exe",
        "gog galaxy.exe", "ubisoftconnect.exe",
        "battle.net.exe", "battle.net launcher.exe",
        # Riot's launcher service + the League lobby client (2026-07-02: a
        # RiotClientServices.exe launcher window got recorded as a "game").
        "riotclientservices.exe", "riotclientux.exe", "riotclientuxrender.exe",
        "leagueclient.exe", "leagueclientux.exe", "leagueclientuxrender.exe",
    })

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        defaults = cls()
        kwargs: dict = {}

        for name in (
            "mic_device",
            "system_audio_device",
            "bookmark_hotkey",
            "youtube_default_tags",
            "youtube_channel_name",
            "youtube_channel_id",
        ):
            if name in data:
                kwargs[name] = _coerce_str(data[name], getattr(defaults, name))

        for name in (
            "setup_complete",
            "autostart_with_windows",
            "framerate_auto",
            "record_any_fullscreen",
            "show_recording_started_toast",
            "show_recording_saved_toast",
            "show_failure_toast",
            "bookmark_sound",
            "show_bookmark_toast",
            "close_to_tray",
            "start_monitoring_on_launch",
        ):
            if name in data:
                kwargs[name] = _coerce_bool(data[name], getattr(defaults, name))

        # Config files from before onboarding had no completion marker. They
        # belong to returning users and must not unexpectedly reopen setup.
        if "setup_complete" not in data:
            kwargs["setup_complete"] = True

        if "output_folder" in data:
            kwargs["output_folder"] = _coerce_output_folder(
                data["output_folder"], defaults.output_folder
            )

        # Backward-compat: an older config used a single ``show_recording_toast``
        # flag for both toasts. If the new keys aren't set, fall back to it.
        if "show_recording_toast" in data:
            legacy = _coerce_bool(data["show_recording_toast"], True)
            kwargs.setdefault("show_recording_started_toast", legacy)
            kwargs.setdefault("show_recording_saved_toast", legacy)

        if "known_games" in data and isinstance(data["known_games"], list):
            known_games = _coerce_string_list(data["known_games"])
            # Strip launcher exes that earlier versions seeded by default.
            pruned = [
                game for game in known_games if game.lower() in cls._AUTO_PRUNE_GAMES
            ]
            if pruned:
                # Leave a trace — a silently vanishing entry is undiagnosable.
                # (The Games tab also refuses to add these, so this normally
                # only fires once per legacy config.)
                logger.info(
                    "Pruned launcher entries from known_games: %s", ", ".join(pruned)
                )
            kwargs["known_games"] = [
                game for game in known_games if game.lower() not in cls._AUTO_PRUNE_GAMES
            ]
        if "disabled_games" in data and isinstance(data["disabled_games"], list):
            kwargs["disabled_games"] = _coerce_string_list(data["disabled_games"])

        # Validate volume ranges. Coercions are type-tolerant: a single
        # type-invalid value (e.g. a hand-edit or buggy writer leaving
        # {"mic_volume_pct": "100%"}) falls back to that field's default rather
        # than raising and bricking startup (see _coerce_int / load_config).
        for name in ("mic_volume_pct", "system_volume_pct"):
            if name in data:
                kwargs[name] = max(
                    0, min(200, _coerce_int(data[name], getattr(defaults, name)))
                )
        if "framerate" in data:
            kwargs["framerate"] = max(
                24, min(240, _coerce_int(data["framerate"], defaults.framerate))
            )
        if "audio_offset_ms" in data:
            kwargs["audio_offset_ms"] = max(
                -1000,
                min(1000, _coerce_int(data["audio_offset_ms"], defaults.audio_offset_ms)),
            )
        if "max_storage_gb" in data:
            kwargs["max_storage_gb"] = max(
                0, _coerce_int(data["max_storage_gb"], defaults.max_storage_gb)
            )
        if "low_disk_warning_gb" in data:
            kwargs["low_disk_warning_gb"] = max(
                0, _coerce_int(data["low_disk_warning_gb"], defaults.low_disk_warning_gb)
            )
        if "custom_bitrate_kbps" in data:
            kwargs["custom_bitrate_kbps"] = max(
                1000,
                min(
                    200_000,
                    _coerce_int(data["custom_bitrate_kbps"], defaults.custom_bitrate_kbps),
                ),
            )

        choices = {
            "notification_position": {"top-left", "top-right", "bottom-left", "bottom-right"},
            "target_resolution": {"source", "1080p", "1440p", "4k"},
            "quality_preset": {"low", "medium", "high", "custom"},
            "youtube_default_privacy": {"public", "unlisted", "private"},
        }
        for name, allowed in choices.items():
            if name in data:
                kwargs[name] = _coerce_choice(data[name], getattr(defaults, name), allowed)

        if "youtube_default_category" in data:
            kwargs["youtube_default_category"] = max(
                1,
                _coerce_int(
                    data["youtube_default_category"], defaults.youtube_default_category
                ),
            )
        return cls(**kwargs)


def _coerce_int(value: object, default: int) -> int:
    """``int(value)`` but fall back to ``default`` on a type-invalid config value
    instead of raising — so one bad field can't crash ``load_config`` and brick
    startup (mirrors the defensive ``youtube_default_category`` handling)."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _coerce_str(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _coerce_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _coerce_choice(value: object, default: str, allowed: set[str]) -> str:
    if not isinstance(value, str):
        return default
    normalized = value.lower()
    return normalized if normalized in allowed else default


def _coerce_string_list(values: list) -> list[str]:
    return _dedupe_casefolded(
        value.strip() for value in values if isinstance(value, str) and value.strip()
    )


def _coerce_output_folder(value: object, default: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        path = Path(value.strip()).expanduser()
        # Relative folders depend on the process working directory, which is
        # different between source and packaged launches. Settings always saves
        # an absolute path; treat hand-edited relative values as malformed.
        return path if path.is_absolute() else default
    except (OSError, RuntimeError):
        return default


def load_config(path: Path | None = None) -> Config:
    """Load config from JSON; on missing/malformed file, return defaults.

    Malformed configs get a side-cared backup at ``<config>.broken-<ts>.json``
    so the user can recover hand-edited values; the loud log line tells them
    where to look. We don't show a modal here because this runs before the
    QApplication exists.
    """
    p = path or config_path()
    if not p.exists():
        return Config()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        logger.exception("Could not read config %s; using defaults", p)
        return Config()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        backup = _stash_broken_config(p, text)
        logger.error(
            "Config %s is not valid JSON — using defaults. "
            "Original saved as %s for inspection.",
            p, backup,
        )
        return Config()
    if not isinstance(data, dict):
        logger.warning("Config %s is not a JSON object; using defaults", p)
        return Config()
    try:
        return Config.from_dict(data)
    except Exception:
        # Valid JSON object but a field holds a type-invalid value that survived
        # from_dict's per-field coercions (e.g. a future field-type change, or a
        # nested-structure mismatch). Degrade to defaults instead of crashing
        # before the tray ever appears — same intent as the JSONDecodeError path.
        backup = _stash_broken_config(p, text)
        logger.exception(
            "Config %s has invalid values — using defaults. "
            "Original saved as %s for inspection.",
            p, backup,
        )
        return Config()


def _stash_broken_config(path: Path, raw_text: str) -> Path:
    """Move/copy the broken config aside so user fixes can be recovered."""
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.name}.broken-{stamp}.txt")
    try:
        backup.write_text(raw_text, encoding="utf-8")
    except OSError:
        logger.exception("Could not write broken-config backup to %s", backup)
    return backup


def save_config(cfg: Config, path: Path | None = None) -> None:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: a crash/power-loss mid-save can't truncate the live config
    # into a corrupt state. Write a sibling temp then os.replace (atomic on NTFS).
    tmp = p.with_name(f"{p.name}.tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(p)
