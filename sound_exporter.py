"""
Radical Sound Exporter  (v4)
Extracts and plays audio embedded in Prototype 2 .p3d files.
Supports PC/LE and PS3/BE. Includes Russian-version P3D types.

0xFE000000 chunk types — audio
──────────────────────────────────────────────────────────────────
 1  AudioFile           RADP   IMA-ADPCM, 1ch  (blue)
 2  AudioFile           RADP   IMA-ADPCM, 2+ch (blue, downmix→mono on play)
 3  AudioFile           RIFF   PCM-16 WAV      (green)
 4  AudioFile           MP3    PS3/BE only     (purple)

0xFE000000 chunk types — config (playable via Simulate)
──────────────────────────────────────────────────────────────────
 5  BasicSoundII        —  1 audio ref, 3D positional sound
 6  DualDistanceSound   —  close+distant clips, distance-blend
 7  PhysicsSound3Voice  —  physics collision, 3-voice polyphony
 8  PhysicsSoundLoop    —  speed-driven looping sound
 9  RandomSound         —  random-pick from N choices
10  TankSound           —  engine with RPM/speed curves
11  AmbienceSound2      —  ambient loop (multi-channel ADPCM)
12  BaseAmbienceSound   —  base ambient loop
13  LairAmbienceSound   —  hive/lair ambient loop
14  SubsonicSound       —  LFE/subwoofer explosion sound
15  AmbientVehicleSound —  background vehicle (engine/startup/passby)
16  Sequence            —  adaptive music state machine

0xFE000000 chunk types — metadata (display only)
──────────────────────────────────────────────────────────────────
17  MaterialMap         —  surface material ->footstep sound mapping
18  ReverbSetting       —  RADverb preset name
19  CompLimitSetting    —  compressor/limiter parameters
20  AudioDialogueSubtitle — per-language subtitle text
21  AudioMemoryBudget   —  audio memory budget categories
22  AudioSoundGroups    —  sound group category list
23  DialogueSoundGroups —  dialogue routing group list
24  FrontendSounds      —  UI event ->sound mapping
25  GasMaskSound        —  VO gas-mask processing filter
26  Mixer               —  master audio mixer hierarchy
27  SideChain           —  sidechain/ducking config
──────────────────────────────────────────────────────────────────
"""

import os, struct, wave, array, io, tempfile, threading, random
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ═══════════════════════════════════════════════════════════════
#  IMA-ADPCM decoder
# ═══════════════════════════════════════════════════════════════

_STEP = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
    598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707,
    1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871,
    5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635,
    13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
]
_IDX = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]

_IMA_BLOCK = 20
_IMA_SAMPS = 32


def _decode_nibble(n, pred, si):
    step  = _STEP[si]
    delta = step >> 3
    if n & 1: delta += step >> 2
    if n & 2: delta += step >> 1
    if n & 4: delta += step
    if n & 8: delta = -delta
    pred = max(-32768, min(32767, pred + delta))
    si   = max(0,      min(88,    si + _IDX[n & 7]))
    return pred, si


def decode_adpcm(adpcm: bytes, channels: int) -> array.array:
    bpc     = len(adpcm) // channels
    nblocks = bpc // _IMA_BLOCK
    ch_out  = [array.array('h') for _ in range(channels)]
    for blk in range(nblocks):
        for ch in range(channels):
            pos   = (blk * channels + ch) * _IMA_BLOCK
            block = adpcm[pos : pos + _IMA_BLOCK]
            si    = struct.unpack_from('<H', block, 0)[0]
            pred  = struct.unpack_from('<h', block, 2)[0]
            si    = max(0, min(88, si))
            buf   = ch_out[ch]
            for i in range(4, _IMA_BLOCK):
                byte = block[i]
                pred, si = _decode_nibble(byte & 0xF, pred, si); buf.append(pred)
                pred, si = _decode_nibble(byte >> 4,  pred, si); buf.append(pred)
    if channels == 1:
        return ch_out[0]
    length = min(len(s) for s in ch_out)
    out = array.array('h')
    for i in range(length):
        for ch in range(channels):
            out.append(ch_out[ch][i])
    return out


def _encode_nibble(sample: int, pred: int, si: int):
    """Encode one PCM sample as a 4-bit IMA-ADPCM nibble; return (nibble, new_pred, new_si)."""
    step  = _STEP[si]
    delta = sample - pred
    nibble = 0
    if delta < 0:
        nibble = 8
        delta  = -delta
    if delta >= step:       nibble |= 4; delta -= step
    step >>= 1
    if delta >= step:       nibble |= 2; delta -= step
    step >>= 1
    if delta >= step:       nibble |= 1
    new_pred, new_si = _decode_nibble(nibble, pred, si)
    return nibble, new_pred, new_si


def encode_adpcm(pcm: array.array, channels: int) -> bytes:
    """Encode interleaved s16 PCM to Radical IMA-ADPCM (block-interleaved, 20 bytes/ch/block)."""
    nframes = len(pcm) // channels
    nblocks = nframes // _IMA_SAMPS
    if nblocks == 0:
        return b''
    ch_blocks: list[list[bytes]] = [[] for _ in range(channels)]
    for ch in range(channels):
        si = 0
        for blk in range(nblocks):
            base = blk * _IMA_SAMPS
            samps = [pcm[(base + i) * channels + ch] for i in range(_IMA_SAMPS)]
            pred  = max(-32768, min(32767, samps[0]))
            header = struct.pack('<Hh', si, pred)
            nibbles: list[int] = []
            for s in samps:
                n, pred, si = _encode_nibble(s, pred, si)
                nibbles.append(n)
            data = bytearray(16)
            for i in range(16):
                data[i] = nibbles[i * 2] | (nibbles[i * 2 + 1] << 4)
            ch_blocks[ch].append(header + bytes(data))
    result = bytearray()
    for blk in range(nblocks):
        for ch in range(channels):
            result += ch_blocks[ch][blk]
    return bytes(result)


def _resample_pcm(pcm: array.array, src_rate: int, dst_rate: int, channels: int) -> array.array:
    """Linear-interpolation resample; pure Python so may be slow for very long files."""
    if src_rate == dst_rate:
        return pcm
    nframes_src = len(pcm) // channels
    nframes_dst = max(1, int(nframes_src * dst_rate / src_rate))
    result = array.array('h', [0] * nframes_dst * channels)
    ratio  = src_rate / dst_rate
    for i in range(nframes_dst):
        pos = i * ratio
        i0  = int(pos)
        i1  = min(i0 + 1, nframes_src - 1)
        frac = pos - i0
        for ch in range(channels):
            s0 = pcm[i0 * channels + ch]
            s1 = pcm[i1 * channels + ch]
            result[i * channels + ch] = int(s0 + frac * (s1 - s0))
    return result


def _convert_channels_pcm(pcm: array.array, src_ch: int, dst_ch: int) -> array.array:
    if src_ch == dst_ch:
        return pcm
    nframes = len(pcm) // src_ch
    result  = array.array('h', [0] * nframes * dst_ch)
    for i in range(nframes):
        if dst_ch == 1:
            total = sum(pcm[i * src_ch + c] for c in range(src_ch))
            result[i] = max(-32768, min(32767, total // src_ch))
        else:
            for dc in range(dst_ch):
                sc = dc if dc < src_ch else 0
                result[i * dst_ch + dc] = pcm[i * src_ch + sc]
    return result


_WAV_FMT_NAMES = {
    3: 'IEEE float32 (pcm_f32le)',
    6: 'A-law', 7: 'mu-law',
    0x11: 'IMA ADPCM', 0x55: 'MP3-in-WAV', 0x161: 'WMA',
}


def _read_wav_to_pcm(path: str):
    """Return (array.array('h'), channels, sample_rate) from a WAV file.
    Raises ValueError for float or other non-PCM-integer formats."""
    # Scan the RIFF fmt chunk to catch unsupported formats before wave.open
    with open(path, 'rb') as f:
        hdr = f.read(512)
    if len(hdr) >= 12 and hdr[:4] == b'RIFF' and hdr[8:12] == b'WAVE':
        pos = 12
        while pos + 8 <= len(hdr):
            cid = hdr[pos:pos+4]
            csz = struct.unpack_from('<I', hdr, pos+4)[0]
            if cid == b'fmt ' and pos + 10 <= len(hdr):
                fmt_code = struct.unpack_from('<H', hdr, pos+8)[0]
                if fmt_code == 3:
                    raise ValueError(
                        "Float32 WAV is not supported (IEEE float format).\n"
                        "Re-export as integer PCM:\n"
                        "  ffmpeg:   ffmpeg -i in.wav -c:a pcm_s16le out.wav\n"
                        "  Audacity: Export > WAV > Signed 16-bit PCM")
                elif fmt_code not in (1, 0xFFFE):   # 1=PCM, 0xFFFE=extensible (may be PCM)
                    name = _WAV_FMT_NAMES.get(fmt_code, f'code 0x{fmt_code:04X}')
                    raise ValueError(
                        f"Unsupported WAV encoding: {name}.\n"
                        "Re-export as PCM (pcm_s16le / pcm_s24le / pcm_s32le).")
                break
            pos += 8 + csz + (csz & 1)

    with wave.open(path, 'rb') as wf:
        nch = wf.getnchannels()
        sr  = wf.getframerate()
        sw  = wf.getsampwidth()
        pcm_bytes = wf.readframes(wf.getnframes())
    if sw == 2:
        pcm = array.array('h')
        pcm.frombytes(pcm_bytes)
    elif sw == 1:
        pcm = array.array('h', [(b - 128) << 8 for b in pcm_bytes])
    elif sw == 3:
        pcm = array.array('h')
        for i in range(0, len(pcm_bytes) - 2, 3):
            v = int.from_bytes(pcm_bytes[i:i + 3], 'little', signed=True)
            pcm.append(max(-32768, min(32767, v >> 8)))
    elif sw == 4:
        src = array.array('i')
        src.frombytes(pcm_bytes)
        pcm = array.array('h', (max(-32768, min(32767, s >> 16)) for s in src))
    else:
        raise ValueError(f"Unsupported WAV sample width: {sw} bytes")
    return pcm, nch, sr


def _downmix_mono(pcm: array.array, channels: int) -> array.array:
    nframes = len(pcm) // channels
    out = array.array('h')
    for i in range(nframes):
        total = sum(pcm[i * channels + ch] for ch in range(channels))
        out.append(max(-32768, min(32767, total // channels)))
    return out


def pcm_to_wav_bytes(pcm: array.array, channels: int, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════
#  Pure3D (.p3d) chunk parser
# ═══════════════════════════════════════════════════════════════

_MAGIC_LE = 0xFF443350
_MAGIC_BE = 0x503344FF


class _Chunk:
    __slots__ = ('chunk_id', 'data', 'children', 'big_endian', 'file_offset', 'ds', 'ts')
    def __init__(self, chunk_id, data, children, big_endian, file_offset=0, ds=0, ts=0):
        self.chunk_id    = chunk_id
        self.data        = data
        self.children    = children
        self.big_endian  = big_endian
        self.file_offset = file_offset
        self.ds          = ds
        self.ts          = ts


def _ru32(raw, off, be):
    return struct.unpack_from('>I' if be else '<I', raw, off)[0]


def parse_p3d(path: str):
    with open(path, 'rb') as fh:
        raw = fh.read()
    root, be = parse_p3d_bytes(raw)
    return root, be, raw


def parse_p3d_bytes(raw: bytes):
    if len(raw) < 12:
        raise ValueError("File too small to be a P3D.")
    magic = struct.unpack_from('<I', raw, 0)[0]
    if   magic == _MAGIC_LE: be = False
    elif magic == _MAGIC_BE: be = True
    else:
        raise ValueError(f"Not a P3D file (magic=0x{magic:08X})")
    root = _parse_iterative(raw, be)
    if root is None:
        raise ValueError("P3D structure is malformed.")
    return root, be


def _parse_iterative(raw, be):
    if len(raw) < 12: return None
    cid = _ru32(raw, 0, be); ds = _ru32(raw, 4, be); ts = _ru32(raw, 8, be)
    if ds < 12 or ts < ds or ts > len(raw): return None
    return _Chunk(cid, raw[12:ds], _parse_children_iterative(raw, ds, ts, be), be,
                  file_offset=0, ds=ds, ts=ts)


def _parse_children_iterative(raw, start, end, be):
    children, cur = [], start
    while cur + 12 <= end:
        cid = _ru32(raw, cur, be); ds = _ru32(raw, cur+4, be); ts = _ru32(raw, cur+8, be)
        if ds < 12 or ts < ds or cur + ts > end: break
        own = raw[cur+12:cur+ds]
        sub = _parse_children_iterative(raw, cur+ds, cur+ts, be)
        children.append(_Chunk(cid, own, sub, be, file_offset=cur, ds=ds, ts=ts))
        cur += ts
    return children


def _walk(chunk):
    yield chunk
    for c in chunk.children:
        yield from _walk(c)


# ═══════════════════════════════════════════════════════════════
#  String-table helpers
# ═══════════════════════════════════════════════════════════════

def _read_string_table(data, be):
    E = '>I' if be else '<I'
    off = 0; groups = []
    try:
        off += 4  # version
        for _ in range(200):
            if off + 4 > len(data): break
            n = struct.unpack_from(E, data, off)[0]; off += 4
            if n == 0 or n > 256 or off + n > len(data): break
            gname = data[off:off+n].decode('ascii', errors='replace'); off += n + 1
            if off + 4 > len(data): break
            ni = struct.unpack_from(E, data, off)[0]; off += 4
            if ni > 10000: break
            items = []
            for _ in range(ni):
                if off + 4 > len(data): break
                ln = struct.unpack_from(E, data, off)[0]; off += 4
                if ln == 0 or ln > 256: break
                iname = data[off:off+ln].decode('ascii', errors='replace'); off += ln + 1
                items.append(iname)
            groups.append((gname, items))
    except Exception:
        pass
    return groups


def _extract_names(data, be):
    for gname, items in _read_string_table(data, be):
        if gname == 'AudioFile':
            seen, unique = set(), []
            for nm in items:
                if nm and nm not in seen:
                    seen.add(nm); unique.append(nm)
            return unique
    return []


def _extract_class_and_name(data, be):
    groups = _read_string_table(data, be)
    if not groups: return None, None
    cname, items = groups[0]
    oname = next((s for s in items if s), '') if items else ''
    return cname, oname


def _skip_strtable(data, be):
    """Return byte offset just past the string table."""
    E = '>I' if be else '<I'
    off = 4
    for _ in range(200):
        if off + 4 > len(data): break
        n = struct.unpack_from(E, data, off)[0]; off += 4
        if n == 0 or n > 256 or off + n > len(data): break
        off += n + 1
        if off + 4 > len(data): break
        ni = struct.unpack_from(E, data, off)[0]; off += 4
        if ni > 10000: break
        for _ in range(ni):
            if off + 4 > len(data): break
            ln = struct.unpack_from(E, data, off)[0]; off += 4
            if ln == 0 or ln > 256: break
            off += ln + 1
    return off


def _find_payload_strs(payload):
    """Scan payload for len-prefixed ASCII strings. Returns [(byte_offset, string)]."""
    result, o = [], 0
    while o + 5 < len(payload):
        n = struct.unpack_from('<I', payload, o)[0]
        if 6 <= n <= 128 and o + 4 + n < len(payload):
            s = payload[o+4:o+4+n]
            if all(0x20 <= b < 0x7F for b in s):
                result.append((o, s.decode('ascii')))
                o += 4 + n + 1
                continue
        o += 1
    return result


# Marker GUID that follows the close-clips in every DualDistanceSound payload.
# Confirmed present in all 4 DualDistanceSound entries in the reference file.
_DUAL_CLOSE_MARKER = bytes.fromhex('1140d70847a109ad')


_METADATA_ONLY_CLASSES = frozenset({
    'MaterialMap', 'ReverbSetting', 'CompLimitSetting', 'AudioDialogueSubtitle',
    'AudioMemoryBudget', 'AudioSoundGroups', 'DialogueSoundGroups', 'FrontendSounds',
    'GasMaskSound', 'Mixer', 'SideChain',
})


def _parse_config_refs(data, class_name, be):
    """Return list of (role, name) for audio references in a config chunk."""
    if class_name in _METADATA_ONLY_CLASSES:
        return []
    payload = data[_skip_strtable(data, be):]
    strs = _find_payload_strs(payload)

    # Remove routing strings: 'surround', bus path (\master\...), 'sound_groups' + its category
    filtered = []
    skip_next = False
    for off, s in strs:
        if skip_next:
            skip_next = False
            continue
        if s == 'sound_groups':
            skip_next = True
            continue
        if s == 'surround' or s.startswith('\\'):
            continue
        filtered.append((off, s))

    if not filtered:
        return []

    if class_name == 'BasicSoundII':
        return [('audio', n) for _, n in filtered]

    elif class_name == 'DualDistanceSound':
        # Strings before the close-type marker byte position = close clips
        marker_pos = payload.find(_DUAL_CLOSE_MARKER)
        refs = []
        for off, n in filtered:
            if marker_pos != -1 and off < marker_pos:
                refs.append(('close', n))
            else:
                refs.append(('distant', n))
        return refs

    elif class_name == 'PhysicsSound3Voice':
        return [('voice', n) for _, n in filtered]

    elif class_name == 'PhysicsSoundLoop':
        return [('audio', n) for _, n in filtered]

    elif class_name == 'RandomSound':
        return [('choice', n) for _, n in filtered]

    elif class_name == 'TankSound':
        role_order = ['move', 'start', 'stop', 'treads', 'ambient']
        return [(role_order[i] if i < len(role_order) else 'audio', n)
                for i, (_, n) in enumerate(filtered)]

    elif class_name in ('AmbienceSound2', 'BaseAmbienceSound', 'LairAmbienceSound'):
        return [('audio', n) for _, n in filtered]

    elif class_name == 'SubsonicSound':
        return [('audio', n) for _, n in filtered]

    elif class_name == 'AmbientVehicleSound':
        roles = ['engine', 'startup', 'passby']
        return [(roles[i] if i < len(roles) else 'audio', n)
                for i, (_, n) in enumerate(filtered)]

    elif class_name == 'Sequence':
        # Deduplicate; keep non-routing strings as music refs
        seen: set = set()
        refs = []
        for _, n in filtered:
            if n not in seen:
                seen.add(n)
                refs.append(('music', n))
        return refs

    return [('audio', n) for _, n in filtered]


def _parse_config_params(data, class_name, be):
    """
    Return dict {label: [current_val, min_val, max_val, step]}.
    Where possible, values are read from the binary payload.
    """
    payload = data[_skip_strtable(data, be):]

    def f32(off):
        if off + 4 <= len(payload):
            return struct.unpack_from('<f', payload, off)[0]
        return 0.0

    if class_name == 'BasicSoundII':
        # Float block layout (verified across 371 tracks):
        #   [0]=Volume  [1-2]=constants  [3]=NearDist  [4]=FarDist
        #   [5]=PitchScale (0.5–2.0, often 1.0)  [6]=PitchLow  [7]=PitchHigh
        # PitchScale is a constant multiplier applied on top of the PitchLow/PitchHigh
        # random range — it governs how fast the game plays the audio relative to the
        # RADP sample rate. The exporter must include it in the pitch calculation or
        # in-game speed will not match the preview.
        strs = _find_payload_strs(payload)
        block_floats = []
        for soff, s in strs:
            if s.startswith('\\'):
                block_start = soff + 4 + len(s) + 1
                for i in range(10):
                    o = block_start + i * 4
                    if o + 4 > len(payload): break
                    v = struct.unpack_from('<f', payload, o)[0]
                    if v != v or abs(v) > 1e7: break
                    block_floats.append(v)
                break

        def _bf(idx, lo, hi, default):
            if idx < len(block_floats):
                v = block_floats[idx]
                if lo <= v <= hi:
                    return v
            return default

        vol     = _bf(0, 0.0, 1.0,    1.0)
        near    = _bf(3, 0.0, 100000, 5.0)
        far     = _bf(4, 0.0, 100000, 50.0)
        p_scale = _bf(5, 0.01, 4.0,   1.0)
        p_low   = _bf(6, 0.1, 4.0,    1.0)
        p_high  = _bf(7, 0.1, 4.0,    1.0)
        return {
            'Volume':       [vol,     0.0,  1.0,              0.05],
            'Pitch Scale':  [p_scale, 0.01, 2.0,              0.05],
            'Pitch Low':    [p_low,   0.1,  2.0,              0.05],
            'Pitch High':   [p_high,  0.1,  2.0,              0.05],
            'Near Dist':    [near,    0.0,  max(near*5, 10.0), 0.5],
            'Far Dist':     [far,     0.0,  max(far*5,  50.0), 1.0],
        }

    elif class_name == 'DualDistanceSound':
        # payload[4:8] = crossover distance as float (3159.0 in reference file)
        xover = f32(4)
        if not (100.0 <= xover <= 50000.0):
            xover = 3159.0
        return {
            'Distance':  [0.0, 0.0, xover * 2.5, 10.0],
            'Crossover': [xover, 100.0, 50000.0, 10.0],
        }

    elif class_name == 'PhysicsSound3Voice':
        # payload[4] = velocity-to-volume scale (0.002436 in ref)
        v2v = f32(4)
        if not (0.0001 <= v2v <= 0.1):
            v2v = 0.002436
        return {
            'Velocity':  [300.0, 0.0, 2000.0, 10.0],
            'Vol Scale': [v2v, 0.0001, 0.05, 0.0001],
        }

    elif class_name == 'PhysicsSoundLoop':
        # payload[8]=min_speed, [12]=vol_scale, [16]=max_vol, [20]=speed_at_max, [24]=min_pitch, [52]=max_pitch
        vol_sc  = f32(12); vol_sc  = vol_sc  if 0.001 <= vol_sc  <= 5.0   else 0.25
        max_vol = f32(16); max_vol = max_vol if 0.01  <= max_vol <= 1.0   else 1.0
        sp_max  = f32(20); sp_max  = sp_max  if 0.1   <= sp_max  <= 1000.0 else 10.0
        min_p   = f32(24); min_p   = min_p   if 0.01  <= min_p   <= 2.0   else 0.32
        max_p   = f32(52); max_p   = max_p   if 0.01  <= max_p   <= 4.0   else 1.0
        return {
            'Speed':      [0.0,   0.0, sp_max * 1.5, 0.1],
            'Vol Scale':  [vol_sc, 0.0, 5.0,  0.01],
            'Max Vol':    [max_vol, 0.0, 1.0, 0.05],
            'Min Pitch':  [min_p,  0.1, 2.0,  0.05],
            'Max Pitch':  [max_p,  0.1, 4.0,  0.05],
        }

    elif class_name == 'RandomSound':
        return {}

    elif class_name == 'TankSound':
        max_rpm   = f32(16); max_rpm   = max_rpm   if 10.0 <= max_rpm   <= 10000.0 else 500.0
        base_pit  = f32(24); base_pit  = base_pit  if 0.1  <= base_pit  <= 2.0     else 0.95
        idle_vol  = f32(40); idle_vol  = idle_vol  if 0.0  <  idle_vol  <= 1.0     else 0.8
        return {
            'RPM':        [0.0,      0.0, max_rpm, 5.0],
            'Base Pitch': [base_pit, 0.5, 2.0,     0.01],
            'Idle Vol':   [idle_vol, 0.0, 1.0,     0.05],
        }

    return {}


# Config classes whose slider changes can be written back to the binary
_PARAM_WRITABLE_CLASSES = frozenset({'BasicSoundII'})

# Slider label -> index in the float block immediately after the bus-path string
_BASIC_SOUND_FLOAT_IDX = {
    'Volume':      0,
    'Near Dist':   3,
    'Far Dist':    4,
    'Pitch Scale': 5,
    'Pitch Low':   6,
    'Pitch High':  7,
}


def _write_basic_sound_params(chunk_own: bytes, be: bool, params: dict) -> bytes:
    """
    Write updated BasicSoundII slider values into the chunk own data.
    Returns a new bytes object of the same length (only float values change).
    """
    payload_off = _skip_strtable(chunk_own, be)
    payload     = chunk_own[payload_off:]
    strs        = _find_payload_strs(payload)
    block_start = None
    for soff, s in strs:
        if s.startswith('\\'):
            block_start = soff + 4 + len(s) + 1
            break
    if block_start is None:
        raise ValueError("BasicSoundII float block not found in chunk data.")
    fmt_f = '>f' if be else '<f'
    data  = bytearray(chunk_own)
    for label, blk_idx in _BASIC_SOUND_FLOAT_IDX.items():
        if label in params:
            abs_off = payload_off + block_start + blk_idx * 4
            if abs_off + 4 <= len(data):
                struct.pack_into(fmt_f, data, abs_off, float(params[label]))
    return bytes(data)


# ═══════════════════════════════════════════════════════════════
#  AudioTrack data model
# ═══════════════════════════════════════════════════════════════

_MP3_BITRATES = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
_MP3_SRMAP    = [44100, 48000, 32000, 0]


class AudioTrack:
    def __init__(self):
        self.codec        = ''
        self.channels     = 0
        self.sample_rate  = 0
        self.duration     = 0.0
        self.raw_data     = b''
        self.names        = []
        self.big_endian   = False
        self.playable     = True
        self.config_class = ''
        # v3/v4: config-specific
        self.config_refs    = []   # list of (role, name)
        self.config_params  = {}   # {label: [val, min, max, step]}
        self.config_display = []   # list of str for metadata-only types
        # v5: write-back support
        self.chunk_own  = b''  # full own data of the 0xFE000000 chunk
        self.file_offset = 0   # byte offset of chunk header in the raw file
        self.chunk_ds    = 0   # data_size from chunk header (12 + len(own))
        self.chunk_ts    = 0   # total_size from chunk header (= chunk_ds for these leafs)


# ═══════════════════════════════════════════════════════════════
#  Audio detection inside 0xFE000000 chunks
# ═══════════════════════════════════════════════════════════════

_FE_ID = 0xFE000000

_CONFIG_CLASSES = {
    # Original config types
    'BasicSoundII':          'BasicSound',
    'DualDistanceSound':     'DualDist',
    'PhysicsSound3Voice':    'Physics3V',
    'PhysicsSoundLoop':      'PhysLoop',
    'RandomSound':           'RandomSnd',
    'TankSound':             'TankSnd',
    # New audio-playable types (Russian/full version)
    'AmbienceSound2':        'AmbSound2',
    'BaseAmbienceSound':     'BaseAmb',
    'LairAmbienceSound':     'LairAmb',
    'SubsonicSound':         'SubSonic',
    'AmbientVehicleSound':   'AmbVehicle',
    'Sequence':              'Sequence',
    # New metadata-only types
    'MaterialMap':           'MatMap',
    'ReverbSetting':         'Reverb',
    'CompLimitSetting':      'CompLimit',
    'AudioDialogueSubtitle': 'Subtitle',
    'AudioMemoryBudget':     'MemBudget',
    'AudioSoundGroups':      'SndGroups',
    'DialogueSoundGroups':   'DlgGroups',
    'FrontendSounds':        'Frontend',
    'GasMaskSound':          'GasMask',
    'Mixer':                 'Mixer',
    'SideChain':             'SideChain',
}


def _valid_mp3_frame(data, off):
    if off + 4 > len(data): return None
    hdr     = struct.unpack_from('>I', data, off)[0]
    sync    = (hdr >> 21) & 0x7FF
    version = (hdr >> 19) & 0x3
    layer   = (hdr >> 17) & 0x3
    br_idx  = (hdr >> 12) & 0xF
    sr_idx  = (hdr >> 10) & 0x3
    ch_mode = (hdr >>  6) & 0x3
    if sync != 0x7FF or version not in (2, 3) or layer != 1: return None
    if br_idx in (0, 15): return None
    sr = _MP3_SRMAP[sr_idx]
    if sr == 0: return None
    return (1 if ch_mode == 3 else 2), sr


def detect_audio(chunk_data, big_endian):
    d, be = chunk_data, big_endian

    radp_pos = d.find(b'RADP')
    if radp_pos != -1 and radp_pos + 20 <= len(d):
        channels    = struct.unpack_from('<I', d, radp_pos +  4)[0]
        sample_rate = struct.unpack_from('<I', d, radp_pos +  8)[0]
        adpcm_size  = struct.unpack_from('<I', d, radp_pos + 16)[0]
        data_start  = radp_pos + 20
        if (1 <= channels <= 8 and 8000 <= sample_rate <= 192000 and
                0 < adpcm_size <= len(d) - data_start):
            nblocks = adpcm_size // (_IMA_BLOCK * channels)
            t = AudioTrack()
            t.codec       = 'adpcm'
            t.channels    = channels
            t.sample_rate = sample_rate
            t.raw_data    = d[data_start : data_start + adpcm_size]
            t.duration    = nblocks * _IMA_SAMPS / sample_rate
            t.names       = _extract_names(d, be)
            t.big_endian  = be
            t.chunk_own   = d
            return t

    riff_pos = d.find(b'RIFF')
    if riff_pos != -1 and riff_pos + 12 <= len(d):
        if d[riff_pos+8:riff_pos+12] == b'WAVE':
            channels = sample_rate = 0; fmt_ok = False
            pcm_start = pcm_count = 0
            pos = riff_pos + 12
            while pos + 8 <= len(d):
                cid = d[pos:pos+4]; csz = struct.unpack_from('<I', d, pos+4)[0]
                if pos + 8 + csz > len(d): break
                if cid == b'fmt ' and csz >= 16:
                    if struct.unpack_from('<H', d, pos+8)[0] == 0x0001:
                        channels    = struct.unpack_from('<H', d, pos+10)[0]
                        sample_rate = struct.unpack_from('<I', d, pos+12)[0]
                        fmt_ok = True
                elif cid == b'data' and csz > 0:
                    pcm_start = pos + 8
                    pcm_count = min(csz, len(d) - pcm_start)
                pos += 8 + csz + (csz & 1)
            if fmt_ok and channels and sample_rate and pcm_count:
                t = AudioTrack()
                t.codec       = 'pcm_wav'
                t.channels    = channels
                t.sample_rate = sample_rate
                t.raw_data    = d[pcm_start : pcm_start + pcm_count]
                t.duration    = pcm_count / (channels * 2 * sample_rate)
                t.names       = _extract_names(d, be)
                t.big_endian  = be
                t.chunk_own   = d
                return t

    if d.find(b'mp3') != -1:
        for i in range(len(d) - 3):
            if d[i] == 0xFF and (d[i+1] & 0xE0) == 0xE0:
                result = _valid_mp3_frame(d, i)
                if result is not None:
                    channels, sample_rate = result
                    mp3_data = d[i:]
                    br_idx   = (struct.unpack_from('>I', d, i)[0] >> 12) & 0xF
                    bitrate  = _MP3_BITRATES[br_idx]
                    duration = (len(mp3_data) * 8 / (bitrate * 1000)) if bitrate else 0.0
                    t = AudioTrack()
                    t.codec       = 'mp3'
                    t.channels    = channels
                    t.sample_rate = sample_rate
                    t.duration    = duration
                    t.raw_data    = mp3_data
                    t.names       = _extract_names(d, be)
                    t.big_endian  = be
                    t.chunk_own   = d
                    return t

    # Empty AudioFile chunk — valid string table but no audio payload yet
    class_name, obj_name = _extract_class_and_name(d, be)
    if class_name == 'AudioFile':
        t = AudioTrack()
        t.codec       = 'empty'
        t.channels    = 0
        t.sample_rate = 0
        t.duration    = 0.0
        t.raw_data    = b''
        t.names       = [obj_name] if obj_name else ['new_track']
        t.big_endian  = be
        t.playable    = False
        t.chunk_own   = d
        return t

    return None


def _get_config_display(data, class_name, be):
    """Return list of human-readable display lines for metadata config types."""
    payload = data[_skip_strtable(data, be):]

    def scan_strings(min_len=3):
        result, o = [], 0
        while o + 5 < len(payload):
            n = struct.unpack_from('<I', payload, o)[0]
            if min_len <= n <= 128 and o + 4 + n < len(payload):
                s = payload[o+4:o+4+n]
                if all(0x20 <= b < 0x7F for b in s):
                    result.append(s.decode('ascii'))
                    o += 4 + n + 1
                    continue
            o += 1
        return result

    def floats_at(offsets):
        vals = []
        for off in offsets:
            if off + 4 <= len(payload):
                v = struct.unpack_from('<f', payload, off)[0]
                vals.append(v if (v == v and abs(v) < 1e9) else 0.0)
            else:
                vals.append(0.0)
        return vals

    if class_name in ('MaterialMap', 'FrontendSounds'):
        # Scan full data — _skip_strtable can overshoot for these types.
        # Skip the known string-table names (class + object).
        skip_strs: set = set()
        for gname, items in _read_string_table(data, be):
            skip_strs.add(gname)
            skip_strs.update(items)
        all_strs = []
        o2 = 0
        while o2 + 5 < len(data):
            n2 = struct.unpack_from('<I', data, o2)[0]
            if 3 <= n2 <= 64 and o2 + 4 + n2 < len(data):
                s2 = data[o2+4:o2+4+n2]
                if all(0x20 <= b < 0x7F for b in s2):
                    decoded = s2.decode('ascii')
                    if decoded not in skip_strs:
                        all_strs.append(decoded)
                    o2 += 4 + n2 + 1
                    continue
            o2 += 1
        cap = 24 if class_name == 'FrontendSounds' else 40
        lines = []
        for i in range(0, len(all_strs) - 1, 2):
            a, b2 = all_strs[i], all_strs[i+1]
            lines.append(f"  {a:<28} -> {b2}")
            if len(lines) >= cap:
                lines.append(f"  ... ({len(all_strs)//2} total pairs)")
                break
        return lines if lines else ['(no mappings found)']

    elif class_name == 'AudioDialogueSubtitle':
        strs = scan_strings(3)
        lines = []; i = 0
        while i < len(strs) - 1:
            lang, text = strs[i], strs[i+1]
            if len(lang) <= 12 and len(text) >= 2 and ' ' in text or len(text) > 4:
                lines.append(f"  {lang:<10} : {text}")
                i += 2
            else:
                i += 1
        return lines if lines else [f"  {s}" for s in strs]

    elif class_name == 'AudioSoundGroups':
        strs = scan_strings(4)
        routing = {'sound_groups', 'surround'}
        groups = [s for s in strs if s not in routing and not s.startswith('\\')]
        return [f"  {s}" for s in groups] if groups else ['(no groups found)']

    elif class_name == 'DialogueSoundGroups':
        strs = scan_strings(4)
        routing = {'dialogue_groups', 'surround'}
        seen: set = set(); unique = []
        for s in strs:
            if s not in routing and s not in seen:
                seen.add(s); unique.append(s)
        return [f"  {s}" for s in unique] if unique else ['(no groups)']

    elif class_name == 'ReverbSetting':
        strs = scan_strings(4)
        return [f"  Preset: {s}" for s in strs[:3]] if strs else ['  (no preset name)']

    elif class_name == 'CompLimitSetting':
        strs = scan_strings(4)
        lines = [f"  Mode: {strs[0]}"] if strs else []
        for i in range(min(12, len(payload)//4)):
            v = struct.unpack_from('<f', payload, i*4)[0]
            if v == v and 1e-5 <= abs(v) <= 1e5:
                lines.append(f"  [{i:2d}] = {v:.5f}")
        return lines[:10] if lines else ['(no params)']

    elif class_name == 'AudioMemoryBudget':
        strs = scan_strings(4)
        routing = {'MemoryBudget'}
        cats = [s for s in strs if s not in routing]
        lines = []
        float_vals = []
        for i in range(min(30, len(payload)//4)):
            v = struct.unpack_from('<f', payload, i*4)[0]
            if v == v and 1.0 <= v <= 1e6:
                float_vals.append(v)
        for j, cat in enumerate(cats):
            val = float_vals[j] if j < len(float_vals) else None
            if val is not None:
                lines.append(f"  {cat:<12} : {val:.0f} KB")
            else:
                lines.append(f"  {cat}")
        return lines if lines else ['(no budget data)']

    elif class_name == 'SideChain':
        strs = scan_strings(4)
        return [f"  Chain: {s}" for s in strs[:3]] if strs else ['  SideChain config']

    elif class_name == 'GasMaskSound':
        groups = _read_string_table(data, be)
        obj_name = ''
        for gname, items in groups:
            if items: obj_name = items[0]; break
        return [
            f"  Applies muffled/filtered effect to VO dialogue",
            f"  Object: '{obj_name}'",
            f"  Bus: \\master\\vo\\peds",
        ]

    elif class_name == 'Mixer':
        strs = scan_strings(4)
        lines = [f"  Master audio mixer definition  ({len(data):,} bytes)"]
        bus_names = [s for s in strs if len(s) >= 4 and not s.startswith('\\')
                     and s not in ('surround', 'Mixer', 'Strip', 'Filter', 'Duck',
                                   'Default', 'master', 'RuntimeUserClass')]
        if bus_names:
            lines.append(f"  Mix buses ({min(len(bus_names), 20)} of {len(bus_names)} shown):")
            lines += [f"    {s}" for s in bus_names[:20]]
        return lines

    return []


def _detect_config(chunk_data, big_endian):
    class_name, obj_name = _extract_class_and_name(chunk_data, big_endian)
    if not class_name:
        return None
    t = AudioTrack()
    t.codec        = 'config'
    t.channels     = 0
    t.sample_rate  = 0
    t.duration     = 0.0
    t.raw_data     = b''
    t.names        = [obj_name] if obj_name else [class_name]
    t.big_endian   = big_endian
    t.playable     = False
    t.config_class = class_name
    t.chunk_own    = chunk_data
    t.config_refs    = _parse_config_refs(chunk_data, class_name, big_endian)
    t.config_params  = _parse_config_params(chunk_data, class_name, big_endian)
    t.config_display = _get_config_display(chunk_data, class_name, big_endian)
    return t


def find_all_tracks(root):
    tracks = []
    for chunk in _walk(root):
        if chunk.chunk_id != _FE_ID or not chunk.data:
            continue
        t = detect_audio(chunk.data, chunk.big_endian)
        if t is not None:
            t.file_offset = chunk.file_offset
            t.chunk_ds    = chunk.ds
            t.chunk_ts    = chunk.ts
            tracks.append(t)
            continue
        cfg = _detect_config(chunk.data, chunk.big_endian)
        if cfg is not None:
            cfg.file_offset = chunk.file_offset
            cfg.chunk_ds    = chunk.ds
            cfg.chunk_ts    = chunk.ts
            tracks.append(cfg)
    return tracks


# ═══════════════════════════════════════════════════════════════
#  Playback helpers
# ═══════════════════════════════════════════════════════════════

_play_lock   = threading.Lock()
_play_thread = None
_tmp_files   = []


def _register_tmp(path):
    _tmp_files.append(path)
    return path


def cleanup_tmp():
    for p in list(_tmp_files):
        try: os.remove(p)
        except: pass
    _tmp_files.clear()


def stop_playback():
    global _play_thread
    try:
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:
        pass
    _play_thread = None


def _build_wav(track: AudioTrack, volume: float = 1.0, pitch: float = 1.0) -> bytes:
    """Decode / retrieve PCM, apply volume + pitch (via sample-rate trick), return WAV bytes."""
    if track.codec == 'adpcm':
        pcm = decode_adpcm(track.raw_data, track.channels)
        if track.channels > 2:
            pcm = _downmix_mono(pcm, track.channels)
            ch  = 1
        else:
            ch = track.channels
    elif track.codec == 'pcm_wav':
        pcm = array.array('h')
        pcm.frombytes(track.raw_data)
        ch = track.channels
    else:
        return b''

    if abs(volume - 1.0) > 0.01:
        pcm = array.array('h',
            (max(-32768, min(32767, int(s * volume))) for s in pcm))

    sr = max(8000, min(192000, int(track.sample_rate * pitch)))
    return pcm_to_wav_bytes(pcm, ch, sr)


def _play_wav_bytes(wav: bytes, status_cb=None, label=''):
    import winsound
    tf = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    tf.write(wav); tf.close()
    _register_tmp(tf.name)
    if status_cb and label:
        status_cb(f"Playing {label} …")
    winsound.PlaySound(tf.name, winsound.SND_FILENAME)


def play_track(track: AudioTrack, status_cb=None, done_cb=None):
    """Start raw-audio playback in a background thread."""
    global _play_thread
    stop_playback()

    def _run():
        try:
            if track.codec == 'adpcm':
                if status_cb: status_cb("Decoding IMA-ADPCM …")
                wav = _build_wav(track)
                if wav:
                    ch_lbl = f"{track.channels}ch→mono" if track.channels > 2 else f"{track.channels}ch"
                    _play_wav_bytes(wav, status_cb,
                                    f"RADP {ch_lbl} {track.sample_rate} Hz {track.duration:.1f}s")
            elif track.codec == 'pcm_wav':
                wav = _build_wav(track)
                if wav:
                    _play_wav_bytes(wav, status_cb,
                                    f"PCM {track.channels}ch {track.sample_rate} Hz {track.duration:.1f}s")
            elif track.codec == 'mp3':
                tf = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
                tf.write(track.raw_data); tf.close()
                _register_tmp(tf.name)
                if status_cb: status_cb(f"Opening MP3 in system player ({track.duration:.1f}s) …")
                os.startfile(tf.name)
            else:
                if status_cb: status_cb("No audio data in this entry.")
        except Exception as exc:
            if status_cb: status_cb(f"Playback error: {exc}")
        finally:
            if done_cb: done_cb()

    _play_thread = threading.Thread(target=_run, daemon=True)
    _play_thread.start()


def play_simulated(track: AudioTrack, all_tracks: list,
                   param_vals: dict, status_cb=None, done_cb=None):
    """
    Simulate config-type playback: find referenced audio, apply parameters, play.
    param_vals: {label: float} — current slider values
    """
    global _play_thread
    stop_playback()

    def _find(name):
        for t in all_tracks:
            if t.names and t.names[0] == name and t.codec != 'config':
                return t
        return None

    def _find_config(name):
        for t in all_tracks:
            if t.names and t.names[0] == name and t.codec == 'config':
                return t
        return None

    def _run():
        try:
            cls = track.config_class

            if cls == 'BasicSoundII':
                audio_refs = [n for r, n in track.config_refs if r == 'audio']
                if not audio_refs:
                    if status_cb: status_cb("BasicSoundII: no audio ref found.")
                    return
                ref = _find(audio_refs[0])
                if ref is None:
                    if status_cb: status_cb(f"BasicSoundII: '{audio_refs[0]}' not in this file.")
                    return
                vol     = float(param_vals.get('Volume',      1.0))
                p_scale = float(param_vals.get('Pitch Scale', 1.0))
                p_low   = float(param_vals.get('Pitch Low',   1.0))
                p_high  = float(param_vals.get('Pitch High',  1.0))
                base    = random.uniform(p_low, p_high) if p_high > p_low else p_low
                pitch   = p_scale * base
                wav = _build_wav(ref, volume=vol, pitch=pitch)
                if wav:
                    _play_wav_bytes(wav, status_cb,
                                    f"BasicSoundII '{track.names[0]}'  vol={vol:.2f}  "
                                    f"pitch={pitch:.3f} (scale={p_scale:.3f}×{base:.3f})")

            elif cls == 'DualDistanceSound':
                dist   = float(param_vals.get('Distance',  0.0))
                xover  = float(param_vals.get('Crossover', 3159.0))
                close_refs   = [n for r, n in track.config_refs if r == 'close']
                distant_refs = [n for r, n in track.config_refs if r == 'distant']
                pool = close_refs if dist < xover else distant_refs
                if not pool:
                    pool = close_refs + distant_refs
                if not pool:
                    if status_cb: status_cb("DualDistanceSound: no refs found.")
                    return
                name = random.choice(pool)
                ref  = _find(name)
                if ref is None:
                    if status_cb: status_cb(f"DualDist: '{name}' not in this file.")
                    return
                zone = "close" if dist < xover else "distant"
                wav = _build_wav(ref)
                if wav:
                    _play_wav_bytes(wav, status_cb,
                                    f"DualDist '{track.names[0]}'  dist={dist:.0f}  [{zone}] ->'{name}'")

            elif cls == 'PhysicsSound3Voice':
                vel   = float(param_vals.get('Velocity',  500.0))
                v2v   = float(param_vals.get('Vol Scale', 0.002436))
                vol   = max(0.0, min(1.0, vel * v2v))
                voices = [n for r, n in track.config_refs if r == 'voice']
                if not voices:
                    if status_cb: status_cb("PhysicsSound3Voice: no voice refs found.")
                    return
                name = random.choice(voices)
                ref  = _find(name)
                if ref is None:
                    if status_cb: status_cb(f"Physics3V: '{name}' not in this file.")
                    return
                wav = _build_wav(ref, volume=vol)
                if wav:
                    _play_wav_bytes(wav, status_cb,
                                    f"Physics3V '{track.names[0]}'  vel={vel:.0f}  vol={vol:.2f}  ->'{name}'")

            elif cls == 'PhysicsSoundLoop':
                speed    = float(param_vals.get('Speed',     5.0))
                vol_sc   = float(param_vals.get('Vol Scale', 0.25))
                max_vol  = float(param_vals.get('Max Vol',   1.0))
                min_p    = float(param_vals.get('Min Pitch', 0.32))
                max_p    = float(param_vals.get('Max Pitch', 1.0))
                # speed_at_max from params definition
                sp_max_cfg = track.config_params.get('Speed', [0, 0, 20, 0])
                sp_max = sp_max_cfg[2] / 1.5  # stored as sp_max*1.5
                sp_max = max(sp_max, 0.1)
                t_norm  = min(1.0, speed / sp_max)
                vol     = min(max_vol, speed * vol_sc)
                pitch   = min_p + t_norm * (max_p - min_p)
                audio_refs = [n for r, n in track.config_refs if r == 'audio']
                if not audio_refs:
                    if status_cb: status_cb("PhysicsSoundLoop: no audio ref found.")
                    return
                ref = _find(audio_refs[0])
                if ref is None:
                    if status_cb: status_cb(f"PhysLoop: '{audio_refs[0]}' not in this file.")
                    return
                wav = _build_wav(ref, volume=vol, pitch=pitch)
                if wav:
                    _play_wav_bytes(wav, status_cb,
                                    f"PhysLoop '{track.names[0]}'  spd={speed:.1f}  vol={vol:.2f}  pitch={pitch:.2f}")

            elif cls == 'RandomSound':
                choices = [n for r, n in track.config_refs if r == 'choice']
                if not choices:
                    if status_cb: status_cb("RandomSound: no choices found.")
                    return
                name = random.choice(choices)
                # The choice might be another config (e.g. DualDistanceSound); cascade if possible
                ref = _find(name)
                if ref is None:
                    cfg_ref = _find_config(name)
                    if cfg_ref is not None:
                        if status_cb: status_cb(f"RandomSound: picked '{name}' ->simulating …")
                        play_simulated(cfg_ref, all_tracks, cfg_ref.config_params,
                                       status_cb=status_cb, done_cb=done_cb)
                        return
                    if status_cb: status_cb(f"RandomSnd: '{name}' not in this file.")
                    return
                wav = _build_wav(ref)
                if wav:
                    _play_wav_bytes(wav, status_cb,
                                    f"RandomSnd '{track.names[0]}' ->'{name}'")

            elif cls == 'TankSound':
                rpm      = float(param_vals.get('RPM',        100.0))
                base_p   = float(param_vals.get('Base Pitch', 0.95))
                idle_v   = float(param_vals.get('Idle Vol',   0.8))
                max_rpm_cfg = track.config_params.get('RPM', [0, 0, 500, 0])
                max_rpm  = max_rpm_cfg[2]
                t_norm   = min(1.0, rpm / max(max_rpm, 1.0))
                vol      = idle_v + t_norm * (1.0 - idle_v)
                pitch    = base_p + t_norm * 0.6  # pitch rises ~0.6× from idle to redline
                move_refs = [n for r, n in track.config_refs if r == 'move']
                if not move_refs:
                    move_refs = [n for _, n in track.config_refs]
                if not move_refs:
                    if status_cb: status_cb("TankSound: no move ref found.")
                    return
                ref = _find(move_refs[0])
                if ref is None:
                    if status_cb: status_cb(f"TankSnd: '{move_refs[0]}' not in this file.")
                    return
                wav = _build_wav(ref, volume=vol, pitch=pitch)
                if wav:
                    _play_wav_bytes(wav, status_cb,
                                    f"TankSnd '{track.names[0]}'  RPM={rpm:.0f}  vol={vol:.2f}  pitch={pitch:.2f}")

            elif cls in ('AmbienceSound2', 'BaseAmbienceSound', 'LairAmbienceSound'):
                audio_refs = [n for r, n in track.config_refs if r == 'audio']
                if not audio_refs:
                    if status_cb: status_cb(f"{cls}: no audio ref found.")
                    return
                ref = _find(audio_refs[0])
                if ref is None:
                    if status_cb: status_cb(f"{cls}: '{audio_refs[0]}' not in this file.")
                    return
                wav = _build_wav(ref)
                if wav:
                    _play_wav_bytes(wav, status_cb, f"{cls} '{track.names[0]}' ->'{audio_refs[0]}'")

            elif cls == 'SubsonicSound':
                audio_refs = [n for r, n in track.config_refs if r == 'audio']
                if not audio_refs:
                    if status_cb: status_cb("SubsonicSound: no audio ref found.")
                    return
                ref = _find(audio_refs[0])
                if ref is None:
                    if status_cb: status_cb(f"SubsonicSound: '{audio_refs[0]}' not in this file.")
                    return
                wav = _build_wav(ref)
                if wav:
                    _play_wav_bytes(wav, status_cb, f"SubSonic '{track.names[0]}' [LFE] ->'{audio_refs[0]}'")

            elif cls == 'AmbientVehicleSound':
                # Prefer engine loop; fall back to any ref
                engine_refs = [n for r, n in track.config_refs if r == 'engine']
                all_refs    = [n for _, n in track.config_refs]
                pool = engine_refs if engine_refs else all_refs
                if not pool:
                    if status_cb: status_cb("AmbientVehicleSound: no audio refs found.")
                    return
                for name in pool:
                    ref = _find(name)
                    if ref is not None:
                        wav = _build_wav(ref)
                        if wav:
                            _play_wav_bytes(wav, status_cb, f"AmbVehicle '{track.names[0]}' ->'{name}'")
                        return
                if status_cb: status_cb(f"AmbVehicle: audio refs not in this file.")

            elif cls == 'Sequence':
                music_refs = [n for r, n in track.config_refs if r == 'music']
                if not music_refs:
                    if status_cb: status_cb("Sequence: no music refs found.")
                    return
                for name in music_refs:
                    ref = _find(name)
                    if ref is not None:
                        wav = _build_wav(ref)
                        if wav:
                            _play_wav_bytes(wav, status_cb, f"Sequence '{track.names[0]}' ->'{name}'")
                        return
                if status_cb: status_cb(f"Sequence: music tracks not in this file (check the music P3D).")

            else:
                if status_cb: status_cb(f"No simulation defined for {cls}.")

        except Exception as exc:
            if status_cb: status_cb(f"Simulation error: {exc}")
        finally:
            if done_cb: done_cb()

    _play_thread = threading.Thread(target=_run, daemon=True)
    _play_thread.start()


# ═══════════════════════════════════════════════════════════════
#  Export helpers
# ═══════════════════════════════════════════════════════════════

def export_track(track: AudioTrack, out_path: str, status_cb=None):
    if track.codec == 'adpcm':
        if status_cb: status_cb("Decoding IMA-ADPCM …")
        pcm  = decode_adpcm(track.raw_data, track.channels)
        data = pcm_to_wav_bytes(pcm, track.channels, track.sample_rate)
        with open(out_path, 'wb') as fh: fh.write(data)
    elif track.codec == 'pcm_wav':
        with wave.open(out_path, 'wb') as wf:
            wf.setnchannels(track.channels)
            wf.setsampwidth(2)
            wf.setframerate(track.sample_rate)
            wf.writeframes(track.raw_data)
    elif track.codec == 'mp3':
        with open(out_path, 'wb') as fh: fh.write(track.raw_data)
    else:
        raise ValueError("No audio data to export.")
    if status_cb: status_cb(f"Saved ->{os.path.basename(out_path)}")


def _safe_name(s):
    for ch in r'\/:*?"<>|': s = s.replace(ch, '_')
    return s.strip() or "track"


def unique_path(base, ext):
    path = base + ext; n = 1
    while os.path.exists(path):
        path = f"{base}_{n}{ext}"; n += 1
    return path


# ═══════════════════════════════════════════════════════════════
#  P3D write-back helpers
# ═══════════════════════════════════════════════════════════════

def replace_chunk_in_p3d(raw: bytes, file_offset: int, old_ts: int,
                          new_own_data: bytes, be: bool) -> bytes:
    """Replace a 0xFE000000 chunk's own data in raw P3D bytes; updates root totalSize."""
    fmt = '>I' if be else '<I'
    new_ds  = 12 + len(new_own_data)
    new_ts  = new_ds
    delta   = new_ts - old_ts
    new_chunk = struct.pack(('>III' if be else '<III'),
                             0xFE000000, new_ds, new_ts) + new_own_data
    new_raw = bytearray(raw[:file_offset]) + bytearray(new_chunk) + bytearray(raw[file_offset + old_ts:])
    root_ts = struct.unpack_from(fmt, new_raw, 8)[0]
    struct.pack_into(fmt, new_raw, 8, root_ts + delta)
    return bytes(new_raw)


def add_chunk_to_p3d(raw: bytes, new_own_data: bytes, be: bool) -> bytes:
    """Append a new 0xFE000000 chunk at the end of the P3D file; updates root totalSize."""
    fmt = '>I' if be else '<I'
    new_ds  = 12 + len(new_own_data)
    new_ts  = new_ds
    new_chunk = struct.pack(('>III' if be else '<III'),
                             0xFE000000, new_ds, new_ts) + new_own_data
    new_raw = bytearray(raw) + bytearray(new_chunk)
    root_ts = struct.unpack_from(fmt, new_raw, 8)[0]
    struct.pack_into(fmt, new_raw, 8, root_ts + new_ts)
    return bytes(new_raw)


def _make_empty_fe_own(be: bool, name: str = 'new_track') -> bytes:
    """Create minimal valid AudioFile chunk own data with no audio payload."""
    E     = '>I' if be else '<I'
    cname = b'AudioFile'
    nb    = name.encode('ascii', 'replace')[:64]
    data  = struct.pack(E, 10)
    data += struct.pack(E, len(cname)) + cname + b'\x00'
    data += struct.pack(E, 1)
    data += struct.pack(E, len(nb)) + nb + b'\x00'
    data += struct.pack(E, 0)
    return data


def _rebuild_radp_own(chunk_own: bytes, adpcm_bytes: bytes,
                       channels: int, sample_rate: int) -> bytes:
    """Rebuild ADPCM chunk own data: keep string table prefix, replace RADP block."""
    radp_pos = chunk_own.find(b'RADP')
    if radp_pos == -1:
        raise ValueError("RADP tag not found in chunk data")
    loop_start = struct.unpack_from('<I', chunk_own, radp_pos + 12)[0]
    prefix  = chunk_own[:radp_pos]
    new_radp = (b'RADP' +
                struct.pack('<IIII', channels, sample_rate, loop_start, len(adpcm_bytes)) +
                adpcm_bytes)
    return prefix + new_radp


def _rebuild_pcmwav_own(chunk_own: bytes, pcm: array.array,
                         channels: int, sample_rate: int) -> bytes:
    """Rebuild PCM WAV chunk own data: keep string table prefix, replace RIFF block."""
    riff_pos = chunk_own.find(b'RIFF')
    if riff_pos == -1:
        raise ValueError("RIFF not found in chunk data")
    return chunk_own[:riff_pos] + pcm_to_wav_bytes(pcm, channels, sample_rate)


def _rebuild_mp3_own(chunk_own: bytes, mp3_bytes: bytes) -> bytes:
    """Rebuild MP3 chunk own data: keep string table prefix, replace MP3 payload."""
    for i in range(len(chunk_own) - 3):
        if chunk_own[i] == 0xFF and (chunk_own[i + 1] & 0xE0) == 0xE0:
            return chunk_own[:i] + mp3_bytes
    return chunk_own + mp3_bytes  # fallback: append


def _import_type_hint(track: 'AudioTrack') -> str:
    if track.codec == 'adpcm':
        return f"RADP IMA-ADPCM  {track.channels}ch  {track.sample_rate} Hz"
    if track.codec == 'pcm_wav':
        return f"PCM WAV  {track.channels}ch  {track.sample_rate} Hz"
    if track.codec == 'mp3':
        return "MPEG-1 MP3 (PS3) — import .mp3 directly; other formats need pydub"
    if track.codec == 'empty':
        return "New chunk — will create RADP IMA-ADPCM 1ch 48000 Hz"
    return ""


def import_audio(track: 'AudioTrack', source_path: str):
    """
    Load source_path, convert to the format expected by track, return (new_own_data, message).
    On error returns (b'', error_message).
    """
    ext = os.path.splitext(source_path)[1].lower()

    # ── 1. Load source audio ──────────────────────────────────
    pcm: array.array | None = None
    src_ch = src_sr = 0

    if ext == '.wav':
        try:
            pcm, src_ch, src_sr = _read_wav_to_pcm(source_path)
        except Exception as e:
            return b'', f"Cannot read WAV: {e}"

    elif ext in ('.mp3', '.ogg', '.flac'):
        try:
            from pydub import AudioSegment           # type: ignore
            seg   = AudioSegment.from_file(source_path)
            src_ch = seg.channels
            src_sr = seg.frame_rate
            raw_data = seg.raw_data
            if seg.sample_width != 2:
                seg = seg.set_sample_width(2)
                raw_data = seg.raw_data
            pcm = array.array('h')
            pcm.frombytes(raw_data)
        except ImportError:
            # pydub not installed — only allow direct .mp3 -> mp3 copy
            if ext == '.mp3' and track.codec == 'mp3':
                with open(source_path, 'rb') as f:
                    mp3_data = f.read()
                try:
                    own = _rebuild_mp3_own(track.chunk_own, mp3_data)
                    return own, f"Imported MP3 as-is ({len(mp3_data) // 1024} KB)"
                except Exception as e:
                    return b'', f"Failed to embed MP3: {e}"
            return b'', (f"Cannot import {ext.upper()} without pydub.\n"
                         f"Install it:  pip install pydub\n"
                         f"(and ffmpeg for MP3/OGG decoding)")
        except Exception as e:
            return b'', f"Cannot read {ext.upper()}: {e}"
    else:
        return b'', f"Unsupported format: {ext}  (use .wav, .mp3, or .ogg)"

    # ── 2. Convert and rebuild ─────────────────────────────────
    if track.codec == 'adpcm':
        dst_ch = track.channels
        dst_sr = track.sample_rate
        pcm2 = _resample_pcm(pcm, src_sr, dst_sr, src_ch)
        pcm3 = _convert_channels_pcm(pcm2, src_ch, dst_ch)
        n_aligned = (len(pcm3) // dst_ch // _IMA_SAMPS) * _IMA_SAMPS * dst_ch
        pcm3 = array.array('h', pcm3[:n_aligned])
        if not pcm3:
            return b'', "Audio too short (need >= 32 samples per channel)."
        adpcm = encode_adpcm(pcm3, dst_ch)
        try:
            own = _rebuild_radp_own(track.chunk_own, adpcm, dst_ch, dst_sr)
        except Exception as e:
            return b'', f"Rebuild error: {e}"
        dur = (len(pcm3) // dst_ch) / dst_sr
        return own, (f"RADP IMA-ADPCM {dst_ch}ch {dst_sr} Hz {dur:.2f}s "
                     f"({len(adpcm) // 1024} KB ADPCM)")

    elif track.codec == 'pcm_wav':
        dst_ch = track.channels
        dst_sr = track.sample_rate
        pcm2 = _resample_pcm(pcm, src_sr, dst_sr, src_ch)
        pcm3 = _convert_channels_pcm(pcm2, src_ch, dst_ch)
        try:
            own = _rebuild_pcmwav_own(track.chunk_own, pcm3, dst_ch, dst_sr)
        except Exception as e:
            return b'', f"Rebuild error: {e}"
        dur = len(pcm3) // dst_ch / dst_sr
        return own, f"PCM WAV {dst_ch}ch {dst_sr} Hz {dur:.2f}s"

    elif track.codec == 'mp3':
        if ext == '.mp3':
            with open(source_path, 'rb') as f:
                mp3_data = f.read()
            try:
                own = _rebuild_mp3_own(track.chunk_own, mp3_data)
            except Exception as e:
                return b'', f"Rebuild error: {e}"
            return own, f"MP3 as-is ({len(mp3_data) // 1024} KB)"
        # pcm available via pydub but no MP3 encoder
        return b'', ("MP3 encoding requires pydub+ffmpeg.\n"
                     "Convert to WAV first, or import an .mp3 directly.")

    elif track.codec == 'empty':
        # New chunk: encode as RADP IMA-ADPCM 1ch 48000 Hz
        dst_ch = 1
        dst_sr = 48000
        pcm2 = _resample_pcm(pcm, src_sr, dst_sr, src_ch)
        pcm3 = _convert_channels_pcm(pcm2, src_ch, dst_ch)
        n_aligned = (len(pcm3) // dst_ch // _IMA_SAMPS) * _IMA_SAMPS * dst_ch
        pcm3 = array.array('h', pcm3[:n_aligned])
        if not pcm3:
            return b'', "Audio too short (need >= 32 samples per channel)."
        adpcm = encode_adpcm(pcm3, dst_ch)
        # Build from scratch using the existing string table prefix
        new_radp = (b'RADP' +
                    struct.pack('<IIII', dst_ch, dst_sr, 10, len(adpcm)) +
                    adpcm)
        own = track.chunk_own + new_radp
        dur = (len(pcm3) // dst_ch) / dst_sr
        return own, (f"RADP IMA-ADPCM {dst_ch}ch {dst_sr} Hz {dur:.2f}s "
                     f"({len(adpcm) // 1024} KB ADPCM)")

    return b'', f"Import not supported for codec '{track.codec}'."


def _export_ext(track):
    if track.codec in ('adpcm', 'pcm_wav'): return '.wav'
    if track.codec == 'mp3':                return '.mp3'
    return ''


# ═══════════════════════════════════════════════════════════════
#  Display helpers
# ═══════════════════════════════════════════════════════════════

def _codec_label(track):
    if track.codec == 'adpcm':   return f"RADP {track.channels}ch"
    if track.codec == 'pcm_wav': return f"PCM WAV {track.channels}ch"
    if track.codec == 'mp3':     return "MP3"
    if track.codec == 'empty':   return "Empty AudioFile"
    if track.codec == 'config':
        return _CONFIG_CLASSES.get(track.config_class, f'Cfg:{track.config_class[:8]}')
    return track.codec or "?"


def _status_detail(track):
    if track.codec == 'empty':
        return ("Empty AudioFile chunk — click 'Import Audio' to add audio.  "
                "Will encode as RADP IMA-ADPCM 1ch 48000 Hz.")
    if track.codec == 'config':
        n_refs = len(track.config_refs)
        cls = track.config_class
        if n_refs > 0:
            return (f"Config: {cls}  |  {n_refs} audio ref(s)  |  "
                    "use 'Simulate Play' to preview with settings")
        else:
            n_disp = len(track.config_display)
            return (f"Config: {cls}  |  metadata-only"
                    + (f"  ({n_disp} items)" if n_disp else "")
                    + "  — no audio refs in this chunk")
    codec_full = {'adpcm': 'IMA-ADPCM (RADP)',
                  'pcm_wav': 'PCM WAV (RIFF)',
                  'mp3': 'MPEG-1 Layer 3'}.get(track.codec, track.codec)
    note     = '  [downmix->mono for playback]' if (track.codec == 'adpcm' and track.channels > 2) else ''
    hint     = _import_type_hint(track)
    hint_str = f'  |  Import expects: {hint}' if hint else ''
    return (f"{codec_full}  |  {track.channels}ch  |  "
            f"{track.sample_rate} Hz  |  {track.duration:.2f}s  |  "
            f"{len(track.raw_data)//1024} KB raw{note}{hint_str}")


# Per-class treeview tag names
def _tag_for(track):
    if track.codec == 'adpcm':   return 'adpcm'
    if track.codec == 'pcm_wav': return 'pcm_wav'
    if track.codec == 'mp3':     return 'mp3'
    if track.codec == 'empty':   return 'empty'
    if track.codec == 'config':
        return {
            'BasicSoundII':          'cfg_basic',
            'DualDistanceSound':     'cfg_dual',
            'PhysicsSound3Voice':    'cfg_phys3',
            'PhysicsSoundLoop':      'cfg_physlp',
            'RandomSound':           'cfg_random',
            'TankSound':             'cfg_tank',
            'AmbienceSound2':        'cfg_amb2',
            'BaseAmbienceSound':     'cfg_amb2',
            'LairAmbienceSound':     'cfg_amb2',
            'SubsonicSound':         'cfg_sub',
            'AmbientVehicleSound':   'cfg_ambveh',
            'Sequence':              'cfg_seq',
            'MaterialMap':           'cfg_matmap',
            'ReverbSetting':         'cfg_reverb',
            'CompLimitSetting':      'cfg_comp',
            'AudioDialogueSubtitle': 'cfg_sub',
            'AudioMemoryBudget':     'cfg_meta',
            'AudioSoundGroups':      'cfg_meta',
            'DialogueSoundGroups':   'cfg_meta',
            'FrontendSounds':        'cfg_meta',
            'GasMaskSound':          'cfg_meta',
            'Mixer':                 'cfg_mixer',
            'SideChain':             'cfg_meta',
        }.get(track.config_class, 'cfg_unk')
    return ''


# ═══════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════

_FG_WHITE  = '#FFFFFF'
_COL_OPEN  = '#2E6DB4'
_COL_PLAY  = '#1B6B2E'
_COL_SIM   = '#5A3A8A'   # simulate play (purple)
_COL_STOP  = '#A62020'
_COL_EXPO  = '#4A4A5A'
_COL_IMP   = '#165A5A'   # import audio (teal)
_COL_ADD   = '#2A4A2A'   # add new chunk (dark green)
_COL_SAVE  = '#4A3020'   # save P3D (dark brown)
_COL_BG    = '#F4F4F6'


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Radical Sound Exporter  v4")
        self.geometry("960x700")
        self.minsize(720, 500)
        self.configure(bg=_COL_BG)
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.bind_all('<Control-o>', lambda _e: self._open_file())
        self.bind_all('<Control-s>', lambda _e: self._save_p3d())

        self._tracks: list[AudioTrack] = []
        self._cur:    AudioTrack | None = None
        self._name_to_idx: dict[str, int] = {}
        # Live slider vars for config panel: {label: DoubleVar}
        self._slider_vars: dict[str, tk.DoubleVar] = {}
        self._sim_btn = None   # "Simulate Play" button reference
        # Write-back state
        self._raw:      bytes = b''
        self._path:     str   = ''
        self._be:       bool  = False
        self._modified: bool  = False

        self._build_ui()

    # ─── build ────────────────────────────────────────────────

    def _build_ui(self):
        # ── Toolbar ──
        bar = tk.Frame(self, bg='#2B2D3A', pady=5)
        bar.pack(fill='x')
        self._btn_open = tk.Button(
            bar, text=" Open P3D… ", command=self._open_file,
            bg=_COL_OPEN, fg=_FG_WHITE, relief='flat',
            font=('Segoe UI', 10, 'bold'), cursor='hand2',
            activebackground='#1D5498', activeforeground=_FG_WHITE,
            padx=8, pady=3)
        self._btn_open.pack(side='left', padx=(8, 12))
        self._lbl_file = tk.Label(
            bar, text="No file loaded", bg='#2B2D3A', fg='#BBBBCC',
            font=('Segoe UI', 9), anchor='w')
        self._lbl_file.pack(side='left', fill='x', expand=True)
        tk.Label(bar, text="Ctrl+O", bg='#2B2D3A', fg='#666688',
                 font=('Segoe UI', 8)).pack(side='right', padx=8)

        # ── Treeview ──
        lf = tk.LabelFrame(self, text=" FE000000 Chunks ", bg=_COL_BG,
                           font=('Segoe UI', 9))
        lf.pack(fill='both', expand=True, padx=8, pady=(8, 4))

        cols = ('name', 'codec', 'ch', 'rate', 'dur')
        self._tv = ttk.Treeview(lf, columns=cols, show='headings',
                                selectmode='browse')
        for col, lbl, w, anc in [
            ('name',  'Track / Entry Name',  310, 'w'),
            ('codec', 'Codec / Type',         120, 'center'),
            ('ch',    'Ch',                    35, 'center'),
            ('rate',  'Sample Rate',            90, 'center'),
            ('dur',   'Duration',               80, 'center'),
        ]:
            self._tv.heading(col, text=lbl, anchor=anc)
            self._tv.column(col, width=w, anchor=anc, stretch=(col == 'name'))

        # Color tags — audio
        self._tv.tag_configure('adpcm',    foreground='#174FA0', background='#EDF2FB')
        self._tv.tag_configure('pcm_wav',  foreground='#175A24', background='#EDF7EF')
        self._tv.tag_configure('mp3',      foreground='#5A1A80', background='#F3EDF8')
        # Color tags — original config types
        self._tv.tag_configure('cfg_basic',  foreground='#7A3800', background='#FFF3E6')
        self._tv.tag_configure('cfg_dual',   foreground='#006060', background='#E3F6F6')
        self._tv.tag_configure('cfg_phys3',  foreground='#3A5800', background='#F0F5E6')
        self._tv.tag_configure('cfg_physlp', foreground='#7A6000', background='#F7F3E3')
        self._tv.tag_configure('cfg_random', foreground='#780050', background='#F7E6F3')
        self._tv.tag_configure('cfg_tank',   foreground='#7A2000', background='#F7EDE6')
        # Color tags — new audio config types
        self._tv.tag_configure('cfg_amb2',   foreground='#004080', background='#E6F0FF')
        self._tv.tag_configure('cfg_sub',    foreground='#0D2D50', background='#DCE8F7')
        self._tv.tag_configure('cfg_ambveh', foreground='#404000', background='#F5F5DC')
        self._tv.tag_configure('cfg_seq',    foreground='#4A3000', background='#FFF8DC')
        # Color tags — metadata-only types
        self._tv.tag_configure('cfg_matmap', foreground='#3A2010', background='#F5EDE0')
        self._tv.tag_configure('cfg_reverb', foreground='#202040', background='#EBEBF5')
        self._tv.tag_configure('cfg_comp',   foreground='#201040', background='#F0EBFF')
        self._tv.tag_configure('cfg_meta',   foreground='#404040', background='#F0F0F0')
        self._tv.tag_configure('cfg_mixer',  foreground='#2A2A2A', background='#E8E8E8')
        self._tv.tag_configure('cfg_unk',    foreground='#555555', background='#EEEEEE')
        self._tv.tag_configure('empty',      foreground='#666666', background='#EBEBEB')

        vsb = ttk.Scrollbar(lf, orient='vertical', command=self._tv.yview)
        self._tv.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        self._tv.pack(fill='both', expand=True, padx=2, pady=2)
        self._tv.bind('<<TreeviewSelect>>', self._on_select)
        self._tv.bind('<Double-1>',         self._on_dblclick)
        self._tv.bind('<Return>',           self._on_dblclick)

        # ── Config detail panel (hidden until a config entry is selected) ──
        self._cfg_panel = tk.LabelFrame(self, text=" Config Details ",
                                        bg=_COL_BG, font=('Segoe UI', 9))
        # Starts hidden; _show_config_panel / _hide_config_panel toggle it

        # ── Control buttons ──
        ctrl = tk.Frame(self, bg=_COL_BG, pady=6)
        ctrl.pack(fill='x', padx=8)

        def btn(parent, text, cmd, color, w=10):
            return tk.Button(parent, text=text, command=cmd,
                             width=w, bg=color, fg=_FG_WHITE,
                             relief='flat', font=('Segoe UI', 10),
                             cursor='hand2', state='disabled',
                             activebackground=color, activeforeground=_FG_WHITE,
                             disabledforeground='#AAAAAA', padx=6, pady=4)

        self._btn_play   = btn(ctrl, "▶  Play",        self._play,         _COL_PLAY)
        self._btn_stop   = btn(ctrl, "■  Stop",        self._stop,         _COL_STOP)
        self._btn_exp    = btn(ctrl, "Export…",        self._export,       _COL_EXPO)
        self._btn_exall  = btn(ctrl, "Export All…",    self._export_all,   _COL_EXPO, w=12)
        self._btn_import = btn(ctrl, "Import Audio…",  self._import_audio, _COL_IMP,  w=14)
        self._btn_add    = btn(ctrl, "Add New Chunk",  self._add_new_chunk,_COL_ADD,  w=14)
        self._btn_save   = btn(ctrl, "Save P3D",       self._save_p3d,     _COL_SAVE, w=10)

        for b in (self._btn_play, self._btn_stop):
            b.pack(side='left', padx=(0, 4))
        tk.Frame(ctrl, width=12, bg=_COL_BG).pack(side='left')
        for b in (self._btn_exp, self._btn_exall):
            b.pack(side='left', padx=(0, 4))
        tk.Frame(ctrl, width=12, bg=_COL_BG).pack(side='left')
        self._btn_import.pack(side='left', padx=(0, 4))
        self._btn_add.pack(side='left', padx=(0, 4))
        tk.Frame(ctrl, width=12, bg=_COL_BG).pack(side='left')
        self._btn_save.pack(side='left', padx=(0, 4))

        # ── Import type hint label ──
        self._import_hint_lbl = tk.Label(
            self, text="", bg=_COL_BG, fg='#446666',
            font=('Segoe UI', 8), anchor='w')
        self._import_hint_lbl.pack(fill='x', padx=12)

        # ── Status bar ──
        self._status_var = tk.StringVar(value="Ready — open a .p3d file  (Ctrl+O)")
        tk.Label(self, textvariable=self._status_var,
                 anchor='w', relief='sunken', bd=1,
                 bg='#E8E8F0', fg='#333344', font=('Segoe UI', 9),
                 ).pack(fill='x', side='bottom', ipady=3)

    # ─── config panel build / show / hide ─────────────────────

    def _hide_config_panel(self):
        self._cfg_panel.pack_forget()
        for w in self._cfg_panel.winfo_children():
            w.destroy()
        self._slider_vars.clear()
        self._sim_btn = None

    def _show_config_panel(self, track: AudioTrack):
        self._hide_config_panel()

        cls   = track.config_class
        label = _CONFIG_CLASSES.get(cls, cls)
        color_map = {
            'cfg_basic':  '#7A3800', 'cfg_dual':   '#006060',
            'cfg_phys3':  '#3A5800', 'cfg_physlp': '#7A6000',
            'cfg_random': '#780050', 'cfg_tank':   '#7A2000',
            'cfg_amb2':   '#004080', 'cfg_sub':    '#0D2D50',
            'cfg_ambveh': '#404000', 'cfg_seq':    '#4A3000',
            'cfg_matmap': '#3A2010', 'cfg_reverb': '#202040',
            'cfg_comp':   '#201040', 'cfg_meta':   '#404040',
            'cfg_mixer':  '#2A2A2A',
        }
        fg = color_map.get(_tag_for(track), '#333333')
        self._cfg_panel.config(
            text=f"  Config Details — {label}: {track.names[0]}  ",
            fg=fg)
        self._cfg_panel.pack(fill='x', padx=8, pady=(0, 4))

        inner = tk.Frame(self._cfg_panel, bg=_COL_BG)
        inner.pack(fill='both', expand=True, padx=4, pady=4)

        # ── Left: references ──
        ref_lf = tk.LabelFrame(inner, text=" Audio References ",
                               bg=_COL_BG, font=('Segoe UI', 8))
        ref_lf.pack(side='left', fill='both', expand=True, padx=(0, 4))

        role_prefix = {
            'audio':   '',          'close':   '[Close]  ',
            'distant': '[Dist]   ', 'voice':   '[Voice]  ',
            'choice':  '[Pick]   ', 'move':    '[Move]   ',
            'start':   '[Start]  ', 'stop':    '[Stop]   ',
            'treads':  '[Treads] ', 'ambient': '[Ambi]   ',
            'engine':  '[Engine] ', 'startup': '[Start]  ',
            'passby':  '[Passby] ', 'music':   '[Music]  ',
        }

        if track.config_refs:
            canvas = tk.Canvas(ref_lf, bg=_COL_BG, highlightthickness=0, height=120)
            vsb2   = ttk.Scrollbar(ref_lf, orient='vertical', command=canvas.yview)
            canvas.configure(yscrollcommand=vsb2.set)
            vsb2.pack(side='right', fill='y')
            canvas.pack(side='left', fill='both', expand=True)
            frm = tk.Frame(canvas, bg=_COL_BG)
            canvas.create_window((0, 0), window=frm, anchor='nw')

            def _on_frm_configure(event, c=canvas):
                c.configure(scrollregion=c.bbox('all'))
            frm.bind('<Configure>', _on_frm_configure)

            for row_i, (role, name) in enumerate(track.config_refs):
                prefix = role_prefix.get(role, f'[{role}] ')
                tk.Label(frm, text=f"{prefix}{name}",
                         bg=_COL_BG, fg='#333344',
                         font=('Consolas', 8), anchor='w',
                         width=38).grid(row=row_i, column=0, sticky='w', pady=1)
                tk.Button(
                    frm, text="→ Go to",
                    font=('Segoe UI', 8), relief='flat', cursor='hand2',
                    bg='#DDEEFF', fg='#1A3A6A', padx=3, pady=1,
                    command=lambda n=name: self._goto_by_name(n),
                ).grid(row=row_i, column=1, padx=(4, 0), pady=1)
        else:
            tk.Label(ref_lf, text="(no audio refs extracted)",
                     bg=_COL_BG, fg='#888888',
                     font=('Segoe UI', 8)).pack(padx=4, pady=4)

        # ── Right: sliders ──
        if track.config_params:
            slider_lf = tk.LabelFrame(inner, text=" Parameters ",
                                      bg=_COL_BG, font=('Segoe UI', 8))
            slider_lf.pack(side='left', fill='y', padx=(0, 4))

            self._slider_vars.clear()
            for row_i, (lbl, spec) in enumerate(track.config_params.items()):
                cur_val, mn, mx, step = spec
                var = tk.DoubleVar(value=cur_val)
                self._slider_vars[lbl] = var

                tk.Label(slider_lf, text=lbl, bg=_COL_BG, fg='#333344',
                         font=('Segoe UI', 8), width=10, anchor='e',
                         ).grid(row=row_i, column=0, padx=(4, 2), pady=2, sticky='e')

                resolution = step if step > 0 else 0.01
                sl = ttk.Scale(slider_lf, from_=mn, to=mx, variable=var,
                               orient='horizontal', length=160)
                sl.grid(row=row_i, column=1, padx=2, pady=2)

                val_lbl = tk.Label(slider_lf, textvariable=var,
                                   bg=_COL_BG, fg='#333344',
                                   font=('Consolas', 8), width=7, anchor='w')
                # Show formatted value
                def _fmt_trace(*_, v=var, wl=val_lbl):
                    try: wl.config(text=f"{v.get():.3g}")
                    except: pass
                var.trace_add('write', _fmt_trace)
                _fmt_trace()
                val_lbl.grid(row=row_i, column=2, padx=(2, 4), pady=2)

            # For writable classes, add traces so slider moves save back to binary
            if track.config_class in _PARAM_WRITABLE_CLASSES and self._raw:
                def _param_trace(*_, t=track):
                    p = {lb: vr.get() for lb, vr in self._slider_vars.items()}
                    self._on_param_change(t, p)
                for _pv in self._slider_vars.values():
                    _pv.trace_add('write', _param_trace)

        # ── Content section (metadata-only types) ──
        if track.config_display:
            section_label = {
                'MaterialMap':           'Material ->Sound Mapping',
                'FrontendSounds':        'UI Event ->Sound Mapping',
                'AudioDialogueSubtitle': 'Subtitle Text by Language',
                'AudioSoundGroups':      'Sound Group Categories',
                'DialogueSoundGroups':   'Dialogue Group Categories',
                'AudioMemoryBudget':     'Memory Budget',
                'ReverbSetting':         'Reverb Preset',
                'CompLimitSetting':      'Compressor Parameters',
                'GasMaskSound':          'VO Filter Info',
                'Mixer':                 'Mixer Info',
                'SideChain':             'SideChain Info',
            }.get(track.config_class, 'Content')
            cont_lf = tk.LabelFrame(self._cfg_panel,
                                    text=f" {section_label} ",
                                    bg=_COL_BG, font=('Segoe UI', 8))
            cont_lf.pack(fill='x', padx=4, pady=(2, 2))
            cont_canvas = tk.Canvas(cont_lf, bg=_COL_BG, highlightthickness=0,
                                    height=min(140, 16 + 16 * len(track.config_display)))
            vsb_c = ttk.Scrollbar(cont_lf, orient='vertical', command=cont_canvas.yview)
            cont_canvas.configure(yscrollcommand=vsb_c.set)
            vsb_c.pack(side='right', fill='y')
            cont_canvas.pack(side='left', fill='x', expand=True)
            cont_frm = tk.Frame(cont_canvas, bg=_COL_BG)
            cont_canvas.create_window((0, 0), window=cont_frm, anchor='nw')
            cont_frm.bind('<Configure>',
                          lambda e, c=cont_canvas: c.configure(scrollregion=c.bbox('all')))
            for line in track.config_display:
                tk.Label(cont_frm, text=line, bg=_COL_BG, fg='#333344',
                         font=('Consolas', 8), anchor='w', justify='left'
                         ).pack(fill='x', padx=2)

        # ── Simulate Play button (only for types with audio refs) ──
        if track.config_refs:
            sim_row = tk.Frame(self._cfg_panel, bg=_COL_BG)
            sim_row.pack(fill='x', padx=4, pady=(2, 4))
            self._sim_btn = tk.Button(
                sim_row, text="▶  Simulate Play",
                command=self._simulate_play,
                bg=_COL_SIM, fg=_FG_WHITE, relief='flat',
                font=('Segoe UI', 10, 'bold'), cursor='hand2',
                activebackground='#3A2060', activeforeground=_FG_WHITE,
                padx=10, pady=4)
            self._sim_btn.pack(side='left', padx=(0, 8))
            tk.Label(sim_row,
                     text="Plays the referenced audio with the parameters above applied.",
                     bg=_COL_BG, fg='#666677', font=('Segoe UI', 8)).pack(side='left')

    # ─── helpers ──────────────────────────────────────────────

    def _set_status(self, msg):
        self._status_var.set(msg)
        self.update_idletasks()

    def _set_btns(self, track):
        import_ok = track is not None and track.codec in ('adpcm', 'pcm_wav', 'mp3', 'empty')
        self._btn_import.config(state='normal' if import_ok else 'disabled')
        if track is None:
            for b in (self._btn_play, self._btn_stop, self._btn_exp): b.config(state='disabled')
        elif track.playable:
            for b in (self._btn_play, self._btn_stop, self._btn_exp): b.config(state='normal')
        else:
            self._btn_play.config(state='disabled')
            self._btn_stop.config(state='disabled')
            self._btn_exp.config(state='disabled')

    def _goto_by_name(self, name):
        """Select the treeview entry whose first name matches 'name'."""
        idx = self._name_to_idx.get(name)
        if idx is not None:
            iid = str(idx)
            self._tv.selection_set(iid)
            self._tv.see(iid)
            self._on_select(None)
        else:
            self._set_status(f"'{name}' not found in this file (may be in another .p3d).")

    # ─── open ─────────────────────────────────────────────────

    def _open_file(self):
        path = filedialog.askopenfilename(
            title="Open P3D file",
            filetypes=[("Pure3D files", "*.p3d"), ("All files", "*.*")])
        if not path: return

        self._set_status(f"Parsing {os.path.basename(path)} …")
        self._tv.delete(*self._tv.get_children())
        self._tracks.clear()
        self._cur = None
        self._name_to_idx.clear()
        self._set_btns(None)
        self._btn_exall.config(state='disabled')
        self._hide_config_panel()

        try:
            root, be, raw = parse_p3d(path)
        except Exception as exc:
            messagebox.showerror("Parse Error", str(exc))
            self._set_status("Failed to parse file.")
            return

        self._raw      = raw
        self._path     = path
        self._be       = be
        self._modified = False
        self._update_title_modified()
        self._btn_add.config(state='normal')

        platform = "PS3" if be else "PC"
        tracks   = find_all_tracks(root)
        self._tracks = tracks

        audio_count  = sum(1 for t in tracks if t.playable)
        config_count = sum(1 for t in tracks if not t.playable and t.codec != 'empty')
        empty_count  = sum(1 for t in tracks if t.codec == 'empty')
        fname = os.path.basename(path)

        if tracks:
            parts = []
            if audio_count:  parts.append(f"{audio_count} audio")
            if config_count: parts.append(f"{config_count} config")
            if empty_count:  parts.append(f"{empty_count} empty")
            self._lbl_file.config(
                text=f"  {fname}  [{platform}]  —  {', '.join(parts)} entries")
            self._populate(tracks)
            if audio_count: self._btn_exall.config(state='normal')
            self._set_status(
                f"Loaded {len(tracks)} FE entries "
                f"({audio_count} audio, {config_count} config"
                + (f", {empty_count} empty" if empty_count else "")
                + ").  Double-click or Enter to play raw audio.")
        else:
            self._lbl_file.config(
                text=f"  {fname}  [{platform}]  — no FE entries found")
            self._set_status("No 0xFE000000 entries in this file.")

    def _populate(self, tracks):
        self._tv.delete(*self._tv.get_children())
        self._name_to_idx.clear()
        for i, t in enumerate(tracks):
            name = ', '.join(t.names) if t.names else f"Entry {i+1}"
            if t.names:
                self._name_to_idx.setdefault(t.names[0], i)
            codec = _codec_label(t)
            if t.playable:
                m, s   = divmod(t.duration, 60)
                dur_str  = f"{int(m)}:{s:05.2f}"
                ch_str   = str(t.channels)
                rate_str = f"{t.sample_rate} Hz"
            else:
                dur_str = ch_str = rate_str = '—'
            tag = _tag_for(t)
            self._tv.insert('', 'end', iid=str(i),
                            values=(name, codec, ch_str, rate_str, dur_str),
                            tags=(tag,) if tag else ())
        if tracks:
            self._tv.selection_set('0')
            self._tv.focus('0')
            self._on_select(None)

    # ─── selection ────────────────────────────────────────────

    def _on_select(self, _event):
        sel = self._tv.selection()
        if not sel: return
        t = self._tracks[int(sel[0])]
        self._cur = t
        self._set_btns(t)
        self._set_status(_status_detail(t))

        hint = _import_type_hint(t)
        self._import_hint_lbl.config(
            text=f"  Import expects: {hint}" if hint else "")

        if t.codec == 'config':
            self._show_config_panel(t)
        else:
            self._hide_config_panel()

    def _on_dblclick(self, _event):
        if self._cur and self._cur.playable:
            self._play()

    # ─── playback ─────────────────────────────────────────────

    def _play(self):
        if not self._cur or not self._cur.playable: return
        self._btn_play.config(state='disabled')

        def done():
            self.after(0, lambda: self._btn_play.config(state='normal'))
            self.after(0, lambda: self._set_status("Playback finished."))

        play_track(self._cur,
                   status_cb=lambda m: self.after(0, lambda msg=m: self._set_status(msg)),
                   done_cb=done)

    def _simulate_play(self):
        if not self._cur or self._cur.codec != 'config': return
        if self._sim_btn: self._sim_btn.config(state='disabled')

        param_vals = {lbl: var.get() for lbl, var in self._slider_vars.items()}

        def done():
            self.after(0, lambda: (self._sim_btn.config(state='normal')
                                   if self._sim_btn else None))
            self.after(0, lambda: self._set_status("Simulation finished."))

        play_simulated(
            self._cur, self._tracks, param_vals,
            status_cb=lambda m: self.after(0, lambda msg=m: self._set_status(m)),
            done_cb=done)

    def _stop(self):
        stop_playback()
        self._btn_play.config(state='normal')
        if self._sim_btn: self._sim_btn.config(state='normal')
        self._set_status("Playback stopped.")

    # ─── export ───────────────────────────────────────────────

    def _export(self):
        t = self._cur
        if not t or not t.playable: return
        ext     = _export_ext(t)
        default = _safe_name(t.names[0] if t.names else "track") + ext
        ftypes  = ([("WAV audio", "*.wav")] if ext == '.wav'
                   else [("MP3 audio", "*.mp3")])
        out = filedialog.asksaveasfilename(
            title="Export track", defaultextension=ext,
            initialfile=default, filetypes=ftypes)
        if not out: return
        self._set_status("Exporting …")

        def _run():
            try:
                export_track(t, out,
                             status_cb=lambda m: self.after(
                                 0, lambda msg=m: self._set_status(msg)))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("Export Error", str(exc)))

        threading.Thread(target=_run, daemon=True).start()

    def _export_all(self):
        audio_tracks = [t for t in self._tracks if t.playable]
        if not audio_tracks: return
        folder = filedialog.askdirectory(title="Select output folder")
        if not folder: return
        total = len(audio_tracks)
        self._set_status(f"Exporting {total} audio tracks …")

        def _run():
            ok = fail = 0
            for i, t in enumerate(audio_tracks):
                ext  = _export_ext(t)
                name = _safe_name(t.names[0] if t.names else f"track_{i+1}")
                out  = unique_path(os.path.join(folder, name), ext)
                try:
                    export_track(t, out); ok += 1
                except Exception:
                    fail += 1
                self.after(0, lambda m=f"Exporting … {i+1}/{total}":
                           self._set_status(m))
            result = f"Done — {ok} exported"
            if fail: result += f", {fail} failed"
            result += f"  -> {folder}"
            self.after(0, lambda: self._set_status(result))

        threading.Thread(target=_run, daemon=True).start()

    # ─── import / add / save ──────────────────────────────────

    def _on_param_change(self, track: AudioTrack, params: dict):
        """Called when a writable config-panel slider changes value."""
        if not self._raw or not track.file_offset:
            return
        try:
            new_own = _write_basic_sound_params(track.chunk_own, track.big_endian, params)
        except Exception:
            return
        # Patch float bytes in-place; chunk size is unchanged so no root resize needed
        own_start = track.file_offset + 12
        if own_start + len(new_own) > len(self._raw):
            return
        raw_ba = bytearray(self._raw)
        raw_ba[own_start:own_start + len(new_own)] = new_own
        self._raw       = bytes(raw_ba)
        track.chunk_own = new_own
        if not self._modified:
            self._modified = True
            self._update_title_modified()

    def _update_title_modified(self):
        title = "Radical Sound Exporter  v4"
        if self._modified:
            title += "  [MODIFIED — Ctrl+S to save]"
        self.title(title)
        self._btn_save.config(state='normal' if self._modified else 'disabled')

    def _reload_from_raw(self):
        """Re-parse self._raw and refresh the treeview."""
        try:
            root, _ = parse_p3d_bytes(self._raw)
        except Exception as e:
            self._set_status(f"Reload error: {e}")
            return
        tracks = find_all_tracks(root)
        self._tracks = tracks
        audio_count  = sum(1 for t in tracks if t.playable)
        config_count = sum(1 for t in tracks if not t.playable and t.codec != 'empty')
        empty_count  = sum(1 for t in tracks if t.codec == 'empty')
        if any(t.playable for t in tracks):
            self._btn_exall.config(state='normal')
        fname    = os.path.basename(self._path) if self._path else "file"
        platform = "PS3" if self._be else "PC"
        parts    = []
        if audio_count:  parts.append(f"{audio_count} audio")
        if config_count: parts.append(f"{config_count} config")
        if empty_count:  parts.append(f"{empty_count} empty")
        if parts:
            self._lbl_file.config(
                text=f"  {fname}  [{platform}]  —  {', '.join(parts)} entries")
        self._populate(tracks)

    def _import_audio(self):
        t = self._cur
        if not t or t.codec not in ('adpcm', 'pcm_wav', 'mp3', 'empty'):
            return
        hint = _import_type_hint(t)
        src = filedialog.askopenfilename(
            title=f"Import Audio  ({hint})",
            filetypes=[("Audio files", "*.wav *.mp3 *.ogg"),
                       ("WAV files", "*.wav"),
                       ("MP3 files", "*.mp3"),
                       ("OGG files", "*.ogg"),
                       ("All files", "*.*")])
        if not src:
            return

        self._set_status(f"Converting {os.path.basename(src)} ...")
        self.update_idletasks()

        try:
            new_own, msg = import_audio(t, src)
        except Exception as exc:
            messagebox.showerror("Import Error", str(exc))
            return

        if not new_own:
            messagebox.showerror("Import Failed", msg)
            return

        try:
            new_raw = replace_chunk_in_p3d(
                self._raw, t.file_offset, t.chunk_ts, new_own, self._be)
        except Exception as exc:
            messagebox.showerror("Write Error", str(exc))
            return

        sel_name = t.names[0] if t.names else None
        self._raw      = new_raw
        self._modified = True
        self._update_title_modified()
        self._reload_from_raw()

        # Restore selection by name so the modified track stays highlighted
        if sel_name and sel_name in self._name_to_idx:
            idx = self._name_to_idx[sel_name]
            self._tv.selection_set(str(idx))
            self._tv.see(str(idx))
            self._on_select(None)

        self._set_status(f"Imported: {msg}  — click 'Save P3D' to write to disk.")

    def _add_new_chunk(self):
        if not self._raw:
            return
        existing = {t.names[0] for t in self._tracks if t.names}
        n = 1
        while f"new_track_{n:03d}" in existing:
            n += 1
        name = f"new_track_{n:03d}"

        own = _make_empty_fe_own(self._be, name)
        try:
            new_raw = add_chunk_to_p3d(self._raw, own, self._be)
        except Exception as exc:
            messagebox.showerror("Error", str(exc))
            return

        self._raw      = new_raw
        self._modified = True
        self._update_title_modified()
        self._reload_from_raw()

        # Select the new track (last in list)
        if self._tracks:
            last = str(len(self._tracks) - 1)
            self._tv.selection_set(last)
            self._tv.see(last)
            self._on_select(None)

        self._set_status(
            f"Added empty chunk '{name}'.  "
            "Select it and click 'Import Audio' to add audio data.")

    def _save_p3d(self):
        if not self._raw or not self._path:
            return
        if not self._modified:
            self._set_status("No changes to save.")
            return
        try:
            with open(self._path, 'wb') as fh:
                fh.write(self._raw)
            self._modified = False
            self._update_title_modified()
            self._set_status(
                f"Saved {len(self._raw) // 1024} KB -> {os.path.basename(self._path)}")
        except Exception as exc:
            messagebox.showerror("Save Error", str(exc))

    # ─── close ────────────────────────────────────────────────

    def _on_close(self):
        if self._modified:
            if not messagebox.askyesno(
                    "Unsaved Changes",
                    "The P3D file has been modified. Exit without saving?"):
                return
        stop_playback()
        cleanup_tmp()
        self.destroy()


# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    app = App()
    app.mainloop()
