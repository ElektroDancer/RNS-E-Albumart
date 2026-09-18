#!/usr/bin/env python3
#
# Copyright (C) 2026 ElektroDancer
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Make embedded MP3 album art compatible with the 193 PU RNS-E custom firmware.

Requirements taken from https://rnse.pcbbc.co.uk/feature-albumart.php:

  * 193 PU units read album art from the MP3 ID3v2 tag (or albumart.jpg files)
  * "Optimal image format for the 193 PU RNS-E is 256x256 pixels jpeg format"
  * the picture must be of type "front cover" and be a JPEG

Everything else this script enforces is defensive hardening for the small
embedded JPEG decoder in the head unit, and is what the reference tools
(MP3TAG / MediaMonkey workflows) produce anyway:

  * baseline (non progressive) JPEG, 3 component YCbCr, no EXIF/ICC payload
  * exactly one picture frame per file, type 0x03, MIME "image/jpeg",
    empty description
  * ID3v2.3 tag (most widely understood by old decoders); v2.4 text frames
    are converted, v2.2 tags are upgraded

No third party modules are required.  Image scaling/encoding uses Pillow when
it is installed and falls back to GdkPixbuf (PyGObject), which ships with every
GTK based desktop.

Usage
-----

    python3 rnse_albumart.py MUSIC_DIRECTORY [options]

The folder is traversed recursively by default; every *.mp3 is rewritten in
place.  The audio data, a trailing ID3v1 tag, the file permissions and the
modification time are all left untouched.

    # 1) Dry run: only reports what would happen
    python3 rnse_albumart.py ~/Music -n

    # 2) The real run
    python3 rnse_albumart.py ~/Music

    # 3) First run with a safety copy (<name>.mp3.bak)
    python3 rnse_albumart.py ~/Music --backup

    # Centre crop non-square covers instead of padding them with black
    python3 rnse_albumart.py ~/Music --fit cover

    # White bars instead of black ones
    python3 rnse_albumart.py ~/Music --pad-color ffffff

    # When no cover is embedded: use cover.jpg/folder.jpg/albumart.jpg
    # from the album folder
    python3 rnse_albumart.py ~/Music --folder-fallback

    # Also drop an albumart.jpg next to the MP3 (the firmware reads that too)
    python3 rnse_albumart.py ~/Music --write-albumart-jpg

    # Only the given folder, no subfolders
    python3 rnse_albumart.py ~/Music/Artist/Album --no-recursive

    # Keep the original tag version instead of unifying on ID3v2.3
    python3 rnse_albumart.py ~/Music --id3 keep

    # Re-encode the cover even when it already fits (costs image quality)
    python3 rnse_albumart.py ~/Music --force

    # Print the summary only
    python3 rnse_albumart.py ~/Music -q

All options: python3 rnse_albumart.py --help

A second run over the same folder changes nothing - files that already fit are
skipped as "already RNS-E compatible".  The exit code is 0 when no file
failed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import zlib
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# ID3 low level helpers
# --------------------------------------------------------------------------

PICTURE_IDS = (b"APIC", b"PIC")
COVER_FILENAMES = (
    "albumart.jpg", "cover.jpg", "folder.jpg", "front.jpg", "album.jpg",
    "cover.png", "folder.png", "front.png", "albumart.png",
)

# ID3v2.2 (3 char) -> ID3v2.3 (4 char) frame identifiers.
V22_TO_V23 = {
    b"BUF": b"RBUF", b"CNT": b"PCNT", b"COM": b"COMM", b"CRA": b"AENC",
    b"ETC": b"ETCO", b"EQU": b"EQUA", b"GEO": b"GEOB", b"IPL": b"IPLS",
    b"LNK": b"LINK", b"MCI": b"MCDI", b"MLL": b"MLLT", b"PIC": b"APIC",
    b"POP": b"POPM", b"REV": b"RVRB", b"RVA": b"RVAD", b"SLT": b"SYLT",
    b"STC": b"SYTC", b"TAL": b"TALB", b"TBP": b"TBPM", b"TCM": b"TCOM",
    b"TCO": b"TCON", b"TCP": b"TCMP", b"TCR": b"TCOP", b"TDA": b"TDAT",
    b"TDY": b"TDLY", b"TEN": b"TENC", b"TFT": b"TFLT", b"TIM": b"TIME",
    b"TKE": b"TKEY", b"TLA": b"TLAN", b"TLE": b"TLEN", b"TMT": b"TMED",
    b"TOA": b"TOPE", b"TOF": b"TOFN", b"TOL": b"TOLY", b"TOR": b"TORY",
    b"TOT": b"TOAL", b"TP1": b"TPE1", b"TP2": b"TPE2", b"TP3": b"TPE3",
    b"TP4": b"TPE4", b"TPA": b"TPOS", b"TPB": b"TPUB", b"TRC": b"TSRC",
    b"TRD": b"TRDA", b"TRK": b"TRCK", b"TS2": b"TSO2", b"TSA": b"TSOA",
    b"TSC": b"TSOC", b"TSI": b"TSIZ", b"TSP": b"TSOP", b"TSS": b"TSSE",
    b"TST": b"TSOT", b"TT1": b"TIT1", b"TT2": b"TIT2", b"TT3": b"TIT3",
    b"TXT": b"TEXT", b"TXX": b"TXXX", b"TYE": b"TYER", b"UFI": b"UFID",
    b"ULT": b"USLT", b"WAF": b"WOAF", b"WAR": b"WOAR", b"WAS": b"WOAS",
    b"WCM": b"WCOM", b"WCP": b"WCOP", b"WPB": b"WPUB", b"WXX": b"WXXX",
}

# ID3v2.4 only frames that have no v2.3 counterpart and are dropped on convert.
V24_ONLY_DROP = {b"TDEN", b"TDOR", b"TDTG", b"TIPL", b"TMCL", b"TPRO", b"TSST",
                 b"EQU2", b"RVA2", b"SEEK", b"ASPI", b"SIGN"}


class TagError(Exception):
    """Raised when an ID3 tag is present but cannot be parsed."""


def syncsafe_decode(data: bytes) -> int:
    value = 0
    for byte in data:
        value = (value << 7) | (byte & 0x7F)
    return value


def syncsafe_encode(value: int, length: int = 4) -> bytes:
    out = bytearray(length)
    for i in range(length - 1, -1, -1):
        out[i] = value & 0x7F
        value >>= 7
    if value:
        raise ValueError("value too large for a syncsafe integer")
    return bytes(out)


def deunsynchronise(data: bytes) -> bytes:
    """Remove the $00 bytes inserted after every $FF by the unsync scheme."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        byte = data[i]
        out.append(byte)
        i += 1
        if byte == 0xFF and i < n and data[i] == 0x00:
            i += 1
    return bytes(out)


def _valid_frame_id(fid: bytes) -> bool:
    return all(0x30 <= c <= 0x39 or 0x41 <= c <= 0x5A for c in fid)


@dataclass
class Id3Tag:
    major: int
    frames: list = field(default_factory=list)   # list of (id: bytes, payload: bytes)
    audio_offset: int = 0                        # first byte after the tag


def _parse_frames(body: bytes, major: int, syncsafe_sizes: bool):
    """Parse a tag body into decoded (id, payload) pairs.

    Returns (frames, bytes_consumed).  Parsing stops at padding or at the first
    structure that does not look like a frame, which is what every real world
    parser does.
    """
    id_len = 3 if major == 2 else 4
    hdr_len = 6 if major == 2 else 10
    frames = []
    pos = 0
    n = len(body)
    while pos + hdr_len <= n:
        fid = body[pos:pos + id_len]
        if fid[0:1] == b"\x00":          # padding starts here
            break
        if not _valid_frame_id(fid):
            break
        if major == 2:
            size = int.from_bytes(body[pos + 3:pos + 6], "big")
            fflags = b"\x00\x00"
        else:
            raw_size = body[pos + 4:pos + 8]
            size = syncsafe_decode(raw_size) if syncsafe_sizes else int.from_bytes(raw_size, "big")
            fflags = body[pos + 8:pos + 10]
        start = pos + hdr_len
        end = start + size
        if size < 0 or end > n:
            break
        payload = body[start:end]
        pos = end

        if major == 3:
            if fflags[1] & 0x40:                       # encrypted -> unusable
                continue
            if fflags[1] & 0x20:                       # grouping identity byte
                payload = payload[1:]
            if fflags[1] & 0x80:                       # zlib compressed
                try:
                    payload = zlib.decompress(payload[4:])
                except zlib.error:
                    continue
        elif major == 4:
            if fflags[1] & 0x40:                       # grouping identity byte
                payload = payload[1:]
            if fflags[1] & 0x04:                       # encrypted -> unusable
                continue
            if fflags[1] & 0x01:                       # data length indicator
                payload = payload[4:]
            if fflags[1] & 0x02:                       # frame level unsync
                payload = deunsynchronise(payload)
            if fflags[1] & 0x08:                       # zlib compressed
                try:
                    payload = zlib.decompress(payload)
                except zlib.error:
                    continue
        frames.append((fid, payload))
    return frames, pos


def read_tag(fh) -> Id3Tag | None:
    """Read the ID3v2 tag from an open binary file handle positioned at 0."""
    head = fh.read(10)
    if len(head) < 10 or head[:3] != b"ID3" or head[3] in (0xFF, 0x00) or head[4] == 0xFF:
        fh.seek(0)
        return Id3Tag(major=0, frames=[], audio_offset=0)
    major, flags = head[3], head[5]
    if major not in (2, 3, 4) or any(b & 0x80 for b in head[6:10]):
        raise TagError(f"unsupported ID3v2.{major} tag")
    size = syncsafe_decode(head[6:10])
    body = fh.read(size)
    if len(body) != size:
        raise TagError("truncated ID3 tag")
    audio_offset = 10 + size
    if major == 4 and flags & 0x10:      # footer present
        fh.read(10)
        audio_offset += 10

    if flags & 0x80 and major in (2, 3):  # whole tag unsynchronised
        body = deunsynchronise(body)

    pos = 0
    if flags & 0x40:                      # extended header
        if major == 3:
            pos = 4 + int.from_bytes(body[0:4], "big")
        elif major == 4:
            pos = syncsafe_decode(body[0:4])
        if not 0 <= pos <= len(body):
            raise TagError("bogus extended header")
    body = body[pos:]

    frames, consumed = _parse_frames(body, major, syncsafe_sizes=(major == 4))
    if major == 4:
        # Several taggers wrote v2.4 frames with plain big endian sizes.
        alt_frames, alt_consumed = _parse_frames(body, major, syncsafe_sizes=False)
        if alt_consumed > consumed and len(alt_frames) >= len(frames):
            frames = alt_frames
    fh.seek(audio_offset)
    return Id3Tag(major=major, frames=frames, audio_offset=audio_offset)


def build_tag(major: int, frames, padding: int = 0) -> bytes:
    out = bytearray()
    for fid, payload in frames:
        if len(fid) != 4:
            raise ValueError(f"bad frame id {fid!r}")
        size = syncsafe_encode(len(payload)) if major == 4 else len(payload).to_bytes(4, "big")
        out += fid + size + b"\x00\x00" + payload
    body = bytes(out) + b"\x00" * padding
    return b"ID3" + bytes([major, 0, 0]) + syncsafe_encode(len(body)) + body


# --------------------------------------------------------------------------
# Text frame handling (only needed when converting v2.4 / v2.2 to v2.3)
# --------------------------------------------------------------------------

def _decode_strings(enc: int, data: bytes, limit: int | None = None):
    """Split a text payload on its terminators and decode it."""
    if enc == 0:
        codec, term = "latin-1", b"\x00"
    elif enc == 1:
        codec, term = "utf-16", b"\x00\x00"
    elif enc == 2:
        codec, term = "utf-16-be", b"\x00\x00"
    else:
        codec, term = "utf-8", b"\x00"
    step = len(term)
    parts, start, i = [], 0, 0
    while i + step <= len(data):
        if data[i:i + step] == term and (step == 1 or i % 2 == 0):
            parts.append(data[start:i])
            start = i = i + step
            if limit is not None and len(parts) >= limit:
                break
            continue
        i += step if step == 2 else 1
    parts.append(data[start:])
    out = []
    for raw in parts:
        try:
            out.append(raw.decode(codec).rstrip("\x00"))
        except UnicodeDecodeError:
            out.append(raw.decode(codec, "replace").rstrip("\x00"))
    return out


def _encode_strings(*texts: str):
    """Encode for ID3v2.3: latin-1 when possible, UTF-16 with BOM otherwise."""
    try:
        blobs = [t.encode("latin-1") for t in texts]
        return 0, blobs
    except UnicodeEncodeError:
        return 1, [t.encode("utf-16") for t in texts]


def convert_frame_to_v23(fid: bytes, payload: bytes):
    """Return a list of v2.3 safe (id, payload) frames; empty means "drop"."""
    if fid in V24_ONLY_DROP:
        return []
    if not payload:
        return [(fid, payload)]

    if fid == b"TDRC":            # v2.4 recording time -> v2.3 TYER (+ TDAT)
        value = (_decode_strings(payload[0], payload[1:], limit=1) or [""])[0]
        year, out = value[:4], []
        if not year.isdigit():
            return []
        enc, (blob,) = _encode_strings(year)
        out.append((b"TYER", bytes([enc]) + blob))
        if len(value) >= 10 and value[4] == "-" and value[5:7].isdigit() and value[8:10].isdigit():
            enc, (blob,) = _encode_strings(value[8:10] + value[5:7])   # DDMM
            out.append((b"TDAT", bytes([enc]) + blob))
        return out

    enc = payload[0]
    if enc in (0, 1):                        # already valid in v2.3
        return [(fid, payload)]
    if enc not in (2, 3):
        return [(fid, payload)]

    if fid == b"TXXX" or fid == b"WXXX":
        parts = _decode_strings(enc, payload[1:], limit=1)
        desc = parts[0] if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        if fid == b"WXXX":                   # url stays latin-1, unencoded
            new_enc, (d,) = _encode_strings(desc)
            term = b"\x00\x00" if new_enc == 1 else b"\x00"
            return [(fid, bytes([new_enc]) + d + term + rest.encode("latin-1", "replace"))]
        new_enc, (d, v) = _encode_strings(desc, rest)
        term = b"\x00\x00" if new_enc == 1 else b"\x00"
        return [(fid, bytes([new_enc]) + d + term + v)]
    if fid in (b"COMM", b"USLT"):
        lang = payload[1:4]
        parts = _decode_strings(enc, payload[4:], limit=1)
        desc = parts[0] if parts else ""
        text = parts[1] if len(parts) > 1 else ""
        new_enc, (d, t) = _encode_strings(desc, text)
        term = b"\x00\x00" if new_enc == 1 else b"\x00"
        return [(fid, bytes([new_enc]) + lang + d + term + t)]
    if fid.startswith(b"T"):
        values = [v for v in _decode_strings(enc, payload[1:]) if v]
        new_enc, blobs = _encode_strings("/".join(values))
        return [(fid, bytes([new_enc]) + blobs[0])]
    return [(fid, payload)]                   # leave anything else untouched


# --------------------------------------------------------------------------
# APIC / PIC frames
# --------------------------------------------------------------------------

@dataclass
class Picture:
    mime: str
    pic_type: int
    description: str
    data: bytes


def parse_picture(fid: bytes, payload: bytes) -> Picture | None:
    if len(payload) < 4:
        return None
    enc = payload[0]
    if fid == b"PIC":                         # v2.2: 3 character image format
        fmt = payload[1:4].decode("latin-1", "replace").upper()
        mime = {"JPG": "image/jpeg", "PNG": "image/png",
                "BMP": "image/bmp", "GIF": "image/gif"}.get(fmt, "image/" + fmt.lower())
        pic_type = payload[4]
        rest = payload[5:]
    else:
        end = payload.find(b"\x00", 1)
        if end < 0:
            return None
        mime = payload[1:end].decode("latin-1", "replace").strip().lower()
        if mime in ("jpeg", "jpg"):
            mime = "image/jpeg"
        elif mime == "png":
            mime = "image/png"
        pic_type = payload[end + 1] if end + 1 < len(payload) else 0
        rest = payload[end + 2:]
    term = b"\x00\x00" if enc in (1, 2) else b"\x00"
    idx = rest.find(term)
    if enc in (1, 2):                          # keep the terminator 2 byte aligned
        idx = -1
        for i in range(0, len(rest) - 1, 2):
            if rest[i:i + 2] == term:
                idx = i
                break
    if idx < 0:
        return None
    desc = _decode_strings(enc, rest[:idx], limit=1)
    return Picture(mime=mime, pic_type=pic_type,
                   description=desc[0] if desc else "",
                   data=rest[idx + len(term):])


def build_apic(jpeg: bytes) -> bytes:
    """Single front cover, latin-1, empty description - the safest shape."""
    return b"\x00" + b"image/jpeg\x00" + b"\x03" + b"\x00" + jpeg


# --------------------------------------------------------------------------
# JPEG inspection
# --------------------------------------------------------------------------

SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_info(data: bytes) -> dict | None:
    """Return {width, height, components, sof, exif, icc} or None if not a JPEG."""
    if not data.startswith(b"\xFF\xD8"):
        return None
    info = {"width": None, "height": None, "components": None,
            "sof": None, "exif": False, "icc": False}
    i, n = 2, len(data)
    while i + 3 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (0xD9, 0xDA):             # end of image / start of scan
            break
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if seglen < 2:
            break
        if marker in SOF_MARKERS:
            info["sof"] = marker
            info["height"] = int.from_bytes(data[i + 5:i + 7], "big")
            info["width"] = int.from_bytes(data[i + 7:i + 9], "big")
            info["components"] = data[i + 9]
        elif marker == 0xE1 and data[i + 4:i + 8] == b"Exif":
            info["exif"] = True
        elif marker == 0xE2:
            info["icc"] = True
        i += 2 + seglen
    return info if info["sof"] is not None else None


def is_rnse_compliant(data: bytes, size: int, max_bytes: int) -> bool:
    info = jpeg_info(data)
    return bool(
        info
        and info["sof"] == 0xC0                    # baseline sequential
        and info["components"] == 3                # YCbCr, not greyscale/CMYK
        and info["width"] == size == info["height"]
        and not info["exif"] and not info["icc"]
        and len(data) <= max_bytes
    )


# --------------------------------------------------------------------------
# Image backends
# --------------------------------------------------------------------------

class ImageBackend:
    name = "none"

    def encode(self, data: bytes, size: int, fit: str, pad: tuple, quality: int) -> bytes:
        raise NotImplementedError


def _target_rect(w: int, h: int, size: int, fit: str):
    """Return (scaled_w, scaled_h, offset_x, offset_y) for the chosen fit mode."""
    if fit == "stretch" or w <= 0 or h <= 0:
        return size, size, 0, 0
    factor = min(size / w, size / h) if fit == "contain" else max(size / w, size / h)
    sw = max(1, round(w * factor))
    sh = max(1, round(h * factor))
    return sw, sh, (size - sw) // 2, (size - sh) // 2


class PillowBackend(ImageBackend):
    name = "pillow"

    def __init__(self):
        from PIL import Image  # noqa: F401
        self._Image = Image

    def encode(self, data, size, fit, pad, quality):
        import io
        Image = self._Image
        src = Image.open(io.BytesIO(data))
        src.load()
        if src.mode == "P":
            src = src.convert("RGBA" if "transparency" in src.info else "RGB")
        elif src.mode in ("LA", "PA"):
            src = src.convert("RGBA")
        elif src.mode not in ("RGB", "RGBA", "L"):
            src = src.convert("RGB")          # CMYK, YCbCr, I;16, ...
        sw, sh, ox, oy = _target_rect(src.width, src.height, size, fit)
        scaled = src.resize((sw, sh), Image.LANCZOS)
        canvas = Image.new("RGB", (size, size), pad)
        if scaled.mode == "RGBA":             # flatten alpha onto the pad colour
            canvas.paste(scaled, (ox, oy), scaled)
        else:
            canvas.paste(scaled.convert("RGB"), (ox, oy))   # negative offsets clip
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=quality, optimize=True,
                    progressive=False, subsampling=2)
        return buf.getvalue()


class GdkPixbufBackend(ImageBackend):
    name = "gdkpixbuf"

    def __init__(self):
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        self._gp = GdkPixbuf

    def encode(self, data, size, fit, pad, quality):
        gp = self._gp
        loader = gp.PixbufLoader()
        try:
            loader.write(data)
            loader.close()
        except Exception as exc:                      # GLib.Error
            raise ValueError(f"cannot decode image: {exc}") from exc
        src = loader.get_pixbuf()
        if src is None:
            raise ValueError("cannot decode image")
        sw, sh, ox, oy = _target_rect(src.get_width(), src.get_height(), size, fit)
        scaled = src.scale_simple(sw, sh, gp.InterpType.HYPER) or src
        canvas = gp.Pixbuf.new(gp.Colorspace.RGB, False, 8, size, size)
        canvas.fill((pad[0] << 24) | (pad[1] << 16) | (pad[2] << 8) | 0xFF)
        # clip the source rectangle to the canvas (matters for "cover")
        dst_x, dst_y = max(0, ox), max(0, oy)
        width = min(sw, size - dst_x) if ox >= 0 else min(size, sw + ox)
        height = min(sh, size - dst_y) if oy >= 0 else min(size, sh + oy)
        scaled.composite(canvas, dst_x, dst_y, width, height, ox, oy,
                         1.0, 1.0, gp.InterpType.NEAREST, 255)
        ok, buf = canvas.save_to_bufferv("jpeg", ["quality"], [str(quality)])
        if not ok:
            raise ValueError("JPEG encoding failed")
        return bytes(buf)


def select_backend(preferred: str) -> ImageBackend:
    candidates = {"pillow": PillowBackend, "gdk": GdkPixbufBackend}
    order = [preferred] if preferred != "auto" else ["pillow", "gdk"]
    errors = []
    for key in order:
        try:
            return candidates[key]()
        except Exception as exc:
            errors.append(f"  {key}: {exc}")
    raise SystemExit(
        "No usable image backend found.\n" + "\n".join(errors) +
        "\nInstall one of:\n"
        "  pip install Pillow            (or: sudo apt install python3-pil)\n"
        "  sudo apt install python3-gi gir1.2-gdkpixbuf-2.0\n"
    )


def encode_cover(backend: ImageBackend, data: bytes, cfg) -> bytes:
    """Encode to JPEG, stepping quality down until it fits into --max-bytes."""
    quality = cfg.quality
    out = backend.encode(data, cfg.size, cfg.fit, cfg.pad_color, quality)
    while len(out) > cfg.max_bytes and quality > cfg.min_quality:
        quality = max(cfg.min_quality, quality - 8)
        out = backend.encode(data, cfg.size, cfg.fit, cfg.pad_color, quality)
    info = jpeg_info(out)
    if not info or info["sof"] != 0xC0 or info["width"] != cfg.size or info["height"] != cfg.size:
        raise ValueError(f"backend produced an unusable JPEG ({info})")
    if len(out) > cfg.max_bytes:
        raise ValueError(f"cannot get below {cfg.max_bytes} bytes (got {len(out)})")
    return out


# --------------------------------------------------------------------------
# Per file processing
# --------------------------------------------------------------------------

@dataclass
class Stats:
    converted: int = 0
    already_ok: int = 0
    no_art: int = 0
    failed: int = 0


def pick_picture(tag: Id3Tag):
    """Prefer an existing front cover, else the largest embedded picture."""
    pics = []
    for fid, payload in tag.frames:
        if fid in PICTURE_IDS:
            pic = parse_picture(fid, payload)
            if pic and pic.data:
                pics.append(pic)
    if not pics:
        return None
    front = [p for p in pics if p.pic_type == 3]
    pool = front or pics
    return max(pool, key=lambda p: len(p.data))


def find_folder_cover(path: str) -> bytes | None:
    folder = os.path.dirname(os.path.abspath(path))
    try:
        entries = {e.lower(): e for e in os.listdir(folder)}
    except OSError:
        return None
    for name in COVER_FILENAMES:
        real = entries.get(name)
        if real:
            try:
                with open(os.path.join(folder, real), "rb") as fh:
                    return fh.read()
            except OSError:
                continue
    return None


def frames_for_output(tag: Id3Tag, target_major: int, jpeg: bytes, log):
    """Drop every picture frame, normalise the rest, append one clean APIC."""
    out = []
    for fid, payload in tag.frames:
        if fid in PICTURE_IDS:
            continue
        if tag.major == 2:
            new_id = V22_TO_V23.get(fid)
            if not new_id:
                log(f"      dropping unknown v2.2 frame {fid.decode('latin-1')}")
                continue
            out.extend(convert_frame_to_v23(new_id, payload))
        elif tag.major == 4 and target_major == 3:
            out.extend(convert_frame_to_v23(fid, payload))
        else:
            out.append((fid, payload))
    out.append((b"APIC", build_apic(jpeg)))
    return out


def process_file(path: str, cfg, backend: ImageBackend, stats: Stats) -> None:
    def log(msg):
        if not cfg.quiet:
            print(msg)

    rel = os.path.relpath(path, cfg.folder)
    try:
        with open(path, "rb") as fh:
            tag = read_tag(fh)
    except (TagError, OSError) as exc:
        log(f"  !! {rel}: {exc}")
        stats.failed += 1
        return

    picture = pick_picture(tag)
    source = "ID3"
    if picture is None and cfg.folder_fallback:
        raw = find_folder_cover(path)
        if raw:
            picture = Picture("", 3, "", raw)
            source = "folder"
    if picture is None:
        log(f"  -- {rel}: no album art")
        stats.no_art += 1
        return

    target_major = 3 if cfg.id3 == "v23" else (tag.major or 3)
    if target_major == 2:
        target_major = 3
    picture_frames = sum(1 for fid, _ in tag.frames if fid in PICTURE_IDS)

    compliant = (
        source == "ID3"
        and picture_frames == 1
        and picture.pic_type == 3
        and picture.description == ""
        and picture.mime == "image/jpeg"
        and tag.major == target_major
        and is_rnse_compliant(picture.data, cfg.size, cfg.max_bytes)
    )
    if compliant and not cfg.force:
        log(f"  ok {rel}: already RNS-E compatible")
        stats.already_ok += 1
        return

    try:
        jpeg = encode_cover(backend, picture.data, cfg)
    except Exception as exc:
        log(f"  !! {rel}: {exc}")
        stats.failed += 1
        return

    old = jpeg_info(picture.data)
    old_desc = (f"{old['width']}x{old['height']}" if old else picture.mime or "?")
    log(f"  -> {rel}: {old_desc} ({len(picture.data)//1024} KiB, {source}) "
        f"=> {cfg.size}x{cfg.size} JPEG ({len(jpeg)//1024} KiB), ID3v2.{target_major}")

    if cfg.dry_run:
        stats.converted += 1
        return

    frames = frames_for_output(tag, target_major, jpeg, log)
    try:
        new_tag = build_tag(target_major, frames, padding=cfg.padding)
    except ValueError as exc:
        log(f"  !! {rel}: {exc}")
        stats.failed += 1
        return

    try:
        st = os.stat(path)
        folder = os.path.dirname(os.path.abspath(path))
        fd, tmp = tempfile.mkstemp(prefix=".rnse-", suffix=".mp3", dir=folder)
        try:
            with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
                out.write(new_tag)
                src.seek(tag.audio_offset)
                shutil.copyfileobj(src, out, 1024 * 1024)
            if cfg.backup:
                shutil.copy2(path, path + ".bak")
            shutil.copymode(path, tmp)      # mkstemp creates 0600 files
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        if cfg.preserve_mtime:
            os.utime(path, (st.st_atime, st.st_mtime))
    except OSError as exc:
        log(f"  !! {rel}: write failed: {exc}")
        stats.failed += 1
        return

    if cfg.write_albumart_jpg:
        target = os.path.join(os.path.dirname(os.path.abspath(path)), "albumart.jpg")
        try:
            with open(target, "wb") as fh:
                fh.write(jpeg)
        except OSError as exc:
            log(f"  !! {target}: {exc}")

    stats.converted += 1


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_color(text: str):
    value = text.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        raise argparse.ArgumentTypeError("colour must be RRGGBB or RGB")
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise argparse.ArgumentTypeError("colour must be hexadecimal") from None


def build_parser():
    p = argparse.ArgumentParser(
        description="Convert embedded MP3 album art to the format the 193 PU "
                    "RNS-E custom firmware expects: 256x256 baseline JPEG, "
                    "front cover, ID3v2.3.")
    p.add_argument("folder", help="folder containing the MP3 files")
    p.add_argument("--no-recursive", dest="recursive", action="store_false",
                   help="only process this folder, not its subfolders")
    p.add_argument("--size", type=int, default=256, metavar="PX",
                   help="edge length of the generated cover (default: 256)")
    p.add_argument("--quality", type=int, default=85, metavar="Q",
                   help="initial JPEG quality (default: 85)")
    p.add_argument("--min-quality", type=int, default=45, metavar="Q",
                   help="lowest quality used to reach --max-bytes (default: 45)")
    p.add_argument("--max-bytes", type=int, default=65536, metavar="N",
                   help="size ceiling for the embedded JPEG (default: 65536)")
    p.add_argument("--fit", choices=("contain", "cover", "stretch"), default="contain",
                   help="how to square up non-square art: contain = pad (default), "
                        "cover = centre crop, stretch = distort")
    p.add_argument("--pad-color", type=parse_color, default=(0, 0, 0), metavar="RRGGBB",
                   help="padding colour for --fit contain (default: 000000)")
    p.add_argument("--id3", choices=("v23", "keep"), default="v23",
                   help="v23 = always write ID3v2.3 (default), keep = keep the "
                        "source tag version")
    p.add_argument("--padding", type=int, default=0, metavar="N",
                   help="zero padding appended to the rewritten tag (default: 0)")
    p.add_argument("--folder-fallback", action="store_true",
                   help="use cover.jpg/folder.jpg/albumart.jpg when no art is embedded")
    p.add_argument("--write-albumart-jpg", action="store_true",
                   help="also write the converted cover as albumart.jpg next to the MP3")
    p.add_argument("--force", action="store_true",
                   help="re-encode even when the art is already compliant")
    p.add_argument("--backup", action="store_true",
                   help="keep the original as <name>.mp3.bak")
    p.add_argument("--update-mtime", dest="preserve_mtime", action="store_false",
                   help="let the modification time change (it is kept by default)")
    p.add_argument("--backend", choices=("auto", "pillow", "gdk"), default="auto",
                   help="image library for scaling and JPEG encoding (default: auto)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="report what would change without touching any file")
    p.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    return p


def iter_mp3s(folder: str, recursive: bool):
    if not recursive:
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.lower().endswith(".mp3") and os.path.isfile(path):
                yield path
        return
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith(".mp3"):
                yield os.path.join(root, name)


def main(argv=None) -> int:
    cfg = build_parser().parse_args(argv)
    cfg.folder = os.path.abspath(cfg.folder)
    if not os.path.isdir(cfg.folder):
        print(f"not a folder: {cfg.folder}", file=sys.stderr)
        return 2
    if not 1 <= cfg.min_quality <= cfg.quality <= 100:
        print("quality values must satisfy 1 <= min-quality <= quality <= 100", file=sys.stderr)
        return 2

    backend = select_backend(cfg.backend)
    if not cfg.quiet:
        mode = " (dry run)" if cfg.dry_run else ""
        print(f"RNS-E 193 PU album art: {cfg.size}x{cfg.size} baseline JPEG, "
              f"front cover, ID3{'v2.3' if cfg.id3 == 'v23' else ' version kept'}"
              f" [{backend.name}]{mode}")
        print(f"Folder: {cfg.folder}")

    stats = Stats()
    for path in iter_mp3s(cfg.folder, cfg.recursive):
        process_file(path, cfg, backend, stats)

    total = stats.converted + stats.already_ok + stats.no_art + stats.failed
    print(f"\n{total} MP3 files: {stats.converted} converted, "
          f"{stats.already_ok} already compatible, {stats.no_art} without art, "
          f"{stats.failed} failed")
    return 1 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())
