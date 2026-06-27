<sub>It is a fork of RockerLol's project, as it is based on his ideas in <a href="https://github.com/RockeyLol/Prototype-RADP-Conv-v1.0">Prototype-RADP-Conv-v1.0</a></sub>
# Radical Sound Exporter

Extracts, previews, exports, and imports audio in Prototype 2 `.p3d` files.
Can also create new AudioFile chunks and inject them into an existing `.p3d`.  
Supports PC (little-endian) and maybe support PS3 (big-endian) files.

---

## Requirements

- Python 3.10 or newer
- `tkinter` (included with standard Python on Windows)
- `winsound` (Windows only — built into the standard library)

No external packages required.

---

## Running

```
python sound_exporter.py
```

Or double-click `sound_exporter.py` if `.py` files are associated with Python.

---

## What it reads

The tool parses **0xFE000000 (FileObjectTable)** chunks inside `.p3d` files.  
Each chunk stores one named audio or config object.

---

## Treeview color coding

Each row in the list uses a distinct color to identify the type at a glance:

| Color | Type | Description |
|-------|------|-------------|
| Blue on light blue | **RADP ADPCM** | IMA-ADPCM audio (mono or multi-channel) |
| Green on light green | **PCM WAV** | Uncompressed 16-bit PCM audio |
| Purple on lavender | **MP3** | MPEG-1 audio (PS3 files only) |
| Orange-brown | **BasicSoundII** | 3D positional one-shot sound cue |
| Teal | **DualDistanceSound** | Close/distant blend sound |
| Olive | **PhysicsSound3Voice** | Physics impact, 3-voice polyphony |
| Gold | **PhysicsSoundLoop** | Speed-driven looping sound |
| Pink | **RandomSound** | Random clip picker |
| Brick red | **TankSound** | Vehicle engine (RPM-driven) |
| Deep blue | **AmbienceSound2 / BaseAmbienceSound / LairAmbienceSound** | Ambient background loop |
| Navy | **SubsonicSound** | LFE / subwoofer effect |
| Khaki | **AmbientVehicleSound** | Background traffic vehicle |
| Warm gold | **Sequence** | Adaptive music state machine |
| Tan | **MaterialMap** | Surface → footstep sound mapping |
| Slate | **ReverbSetting / CompLimitSetting** | DSP / mix configuration |
| Light gray | **Mixer / SideChain / AudioMemoryBudget** | Global audio system config |
| Mid gray | **AudioSoundGroups / DialogueSoundGroups / FrontendSounds / GasMaskSound** | Routing / registry data |

---

## Controls

| Action | How |
|--------|-----|
| Open file | Click **Open P3D…** or press **Ctrl+O** |
| Play raw audio | Select a row and click **Play**, or double-click / press Enter |
| Stop playback | Click **Stop** |
| Export one track | Select a row and click **Export…** |
| Export all audio | Click **Export All…** (exports all playable tracks to a folder) |
| Import sound | Click **Import…** to replace or add an AudioFile chunk from a `.wav` or `.mp3` file |
| Create new chunk | Click **New Chunk…** to create a new AudioFile chunk and inject it into the open `.p3d` |

---

## Config panel

Selecting any config entry (non-raw-audio row) opens the **Config Details** panel below the list.

### Audio references section

Lists every AudioFile that this config object references, with a role label:

| Role label | Meaning |
|------------|---------|
| *(blank)* | Generic audio reference (BasicSoundII, ambience types, LFE) |
| `[Close]` | Close-range clip (DualDistanceSound) |
| `[Dist]` | Distant clip (DualDistanceSound) |
| `[Voice]` | One polyphony voice (PhysicsSound3Voice) |
| `[Pick]` | One random choice (RandomSound) |
| `[Move]` | Engine loop (TankSound) |
| `[Start]` | Engine startup (TankSound, AmbientVehicleSound) |
| `[Stop]` | Engine stop (TankSound) |
| `[Treads]` | Tread/tracks sound (TankSound) |
| `[Ambi]` | Ambient idle sound (TankSound) |
| `[Engine]` | Engine loop (AmbientVehicleSound) |
| `[Passby]` | Doppler passby clip (AmbientVehicleSound) |
| `[Music]` | Music track (Sequence) |

Click **→ Go to** next to any reference to jump to that AudioFile in the list (if it exists in the same `.p3d` file).

> Audio references may point to AudioFiles in a different `.p3d` file. In that case "→ Go to" will show "not found in this file" in the status bar.

### Radical UIDs section

For config classes the panel shows a **Radical UIDs** frame containing every
known Radical UID found in the chunk payload. UIDs are computed by the engine's
`MakeUID` hash (`h = 0; for each char c: h = h*65599 ^ c`) and stored
little-endian regardless of platform.

| Label | Meaning |
|-------|---------|
| `classUID` | 8-byte class-version hash unique to each config type. Constant across all instances of that class. |
| `uid_schema[surround]` | Schema UID of the "surround" routing property (originating string unknown). |
| `uid["surround"]` | `MakeUID("surround")` = `0xBD37972B64167C6E` — the routing bus target. |
| `uid_schema[snd_grps]` | Schema UID of the "sound_groups" routing property (originating string unknown). |
| `uid["sound_groups"]` | `MakeUID("sound_groups")` = `0xE245EC93C2FB08B0` — the priority/category target. |
| `uid["Points"]` | `MakeUID("Points")` = `0xAD09A14708D74011` — property tag for data-point arrays (velocity curves, clip lists). |

Only UIDs that are actually present in the chunk are shown. Raw AudioFile
chunks (ADPCM/WAV/MP3) never have this section — UIDs only appear in config
class payloads.

To know more about Radical's UIDs, Check UID Dehasher.

### Parameters section

Shows sliders for the numeric parameters read from the binary payload:

| Type | Sliders |
|------|---------|
| BasicSoundII | Volume, Pitch Low, Pitch High, Near Dist, Far Dist |
| DualDistanceSound | Distance (simulated listener distance), Crossover |
| PhysicsSound3Voice | Velocity, Vol Scale |
| PhysicsSoundLoop | Speed, Vol Scale, Max Vol, Min Pitch, Max Pitch |
| RandomSound | *(none)* |
| TankSound | RPM, Base Pitch, Idle Vol |
| All other config types | *(none)* |

### Content section (metadata types)

For types that contain lookup tables or configuration text rather than audio references, the panel shows a scrollable content view:

| Type | Content shown |
|------|--------------|
| MaterialMap | Surface material name → footstep AudioFile name |
| FrontendSounds | UI event name → AudioFile name (first 24 pairs) |
| AudioDialogueSubtitle | Per-language subtitle text |
| AudioSoundGroups | All sound group category names |
| DialogueSoundGroups | All dialogue routing group names |
| AudioMemoryBudget | Category names with budget in KB |
| ReverbSetting | Reverb preset name |
| CompLimitSetting | Mode (Stereo/Surround) and compressor float parameters |
| GasMaskSound | VO filter description |
| Mixer | Mix bus names (first 20 of ~200+) |
| SideChain | Chain name |

### Simulate Play button

Appears for config types that have audio references. Plays the referenced audio with the current slider values applied:

| Type | Simulation behavior |
|------|---------------------|
| BasicSoundII | Plays the audio ref with volume scaled and pitch randomized between PitchLow and PitchHigh |
| DualDistanceSound | Picks close or distant pool based on Distance vs Crossover; picks randomly within the pool |
| PhysicsSound3Voice | Scales volume by `Velocity × VolScale`, picks one of the 3 voices at random |
| PhysicsSoundLoop | Derives volume and pitch from Speed using the binary-read parameters |
| RandomSound | Picks one choice at random; cascades into a sub-config if needed |
| TankSound | Derives pitch = base_pitch + (RPM/maxRPM)×0.6 and volume = lerp(idleVol, 1.0, RPM/maxRPM) |
| AmbienceSound2 / BaseAmbienceSound / LairAmbienceSound | Plays the referenced multi-channel ADPCM (downmixed to mono) at volume 1.0 |
| SubsonicSound | Plays the LFE clip at volume 1.0 |
| AmbientVehicleSound | Plays the engine loop clip |
| Sequence | Plays the first music track that can be found in the current file |

> Multi-channel ADPCM (3+ch) is downmixed to mono for playback because Windows `winsound` only supports 1- or 2-channel WAV. The exported file retains the original channel count.

---

## Import

- **Import…** replaces the selected AudioFile chunk's audio data with a new sound file (`.wav` or `.mp3`). The chunk's object name is preserved; only the raw audio payload is swapped. The modified `.p3d` is saved in place.
- Imported `.wav` files are re-encoded as Radical IMA-ADPCM (RADP). Imported `.mp3` files are stored as-is.

---

## New Chunk

- **New Chunk…** creates a brand-new AudioFile chunk from a sound file and injects it into the currently open `.p3d`. You supply the object name that other config chunks (e.g. BasicSoundII) will reference by name. The new chunk appears in the treeview immediately after injection.

---

## Export

- **Export…** saves the selected track: ADPCM → `.wav` (full multi-channel), PCM → `.wav`, MP3 → `.mp3`
- **Export All…** saves all playable tracks in the file to a chosen folder, naming them by their object name
- Config entries (non-audio rows) cannot be exported — they contain no raw audio data

---

## Known limitations

- Playback requires Windows (`winsound`). The parser and exporter work on any OS.
- Music Sequence tracks typically reside in a different `.p3d` file than the Sequence config itself. "Simulate Play" for Sequence will report "not in this file" unless you open a file that contains both.
- The binary layout of the Sequence state-machine transition table is not fully decoded. Audio track references are extracted but the full state logic is not simulated.
- Mixer, SideChain, and ReverbSetting are read-only display — their parameters are not applied to playback.
- Programm loop & Sound Playing logic are in the same thread which is a well known issue.
