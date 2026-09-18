# RNS-E Albumart

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Make embedded MP3 album art readable by the **193 PU Audi RNS-E** custom firmware.

The 193 PU firmware can display cover art, but only for a narrow image format.
Art that looks fine in every desktop player — 600x600 progressive JPEG, PNG,
EXIF-laden, or stuffed into an ID3v2.4 tag — usually shows up as nothing at all
on the head unit. This script rewrites the art in every MP3 of a folder tree so
the unit actually renders it, and leaves everything else in the file alone.

Single file, no third-party modules required, Python 3.8+.

## What it produces

Per the [RNS-E album art documentation](https://rnse.pcbbc.co.uk/feature-albumart.php):

* **256x256 pixels**, JPEG — the format the docs call optimal for 193 PU
* picture type **`0x03` (front cover)**, MIME `image/jpeg`, empty description
* exactly **one** picture frame per file

Plus defensive hardening for the small embedded JPEG decoder in the head unit —
the same output the reference MP3TAG / MediaMonkey workflows produce:

* **baseline** (non-progressive) JPEG, 3-component YCbCr
* no EXIF and no ICC profile payload
* **ID3v2.3** tag (the version old decoders understand best); v2.4 text frames
  are converted, v2.2 tags are upgraded
* JPEG kept **under 64 KiB** by stepping the quality down if needed

## What it leaves untouched

* the audio data — it is copied byte for byte
* a trailing ID3v1 tag
* file permissions and the modification time (unless you pass `--update-mtime`)
* all other ID3 frames (title, artist, album, …)

Writes are atomic: the new file is built in a temp file next to the original and
then `os.replace`d into position, so an interrupted run cannot leave a half-written MP3.

## Requirements

* Python 3.8 or newer
* one image backend for scaling and JPEG encoding — either is fine:

```bash
# Pillow (preferred, tried first)
pip install Pillow            # or: sudo apt install python3-pil

# or GdkPixbuf, which ships with every GTK desktop
sudo apt install python3-gi gir1.2-gdkpixbuf-2.0
```

Pick one explicitly with `--backend pillow` / `--backend gdk`; the default
`auto` tries Pillow, then GdkPixbuf.

## Install

```bash
git clone https://github.com/ElektroDancer/RNS-E-Albumart.git
cd RNS-E-Albumart
chmod +x rnse_albumart.py
```

## Usage

```bash
python3 rnse_albumart.py MUSIC_DIRECTORY [options]
```

The folder is traversed recursively by default and every `*.mp3` is rewritten in
place. **Do a dry run first**, and keep a backup on the first real pass:

```bash
# 1) Dry run: only reports what would happen
python3 rnse_albumart.py ~/Music -n

# 2) The real run, keeping <name>.mp3.bak per changed file
python3 rnse_albumart.py ~/Music --backup

# 3) Later runs
python3 rnse_albumart.py ~/Music
```

A second run over the same folder changes nothing — files that already fit are
skipped as `already RNS-E compatible`. The exit code is `0` when no file failed,
`1` otherwise, and `2` on a bad invocation.

### Common recipes

```bash
# Centre crop non-square covers instead of padding them with black
python3 rnse_albumart.py ~/Music --fit cover

# White bars instead of black ones
python3 rnse_albumart.py ~/Music --pad-color ffffff

# No art embedded? Fall back to cover.jpg/folder.jpg/albumart.jpg in the album folder
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
```

### Example output

```
RNS-E 193 PU album art: 256x256 baseline JPEG, front cover, ID3v2.3 [pillow] (dry run)
Folder: /home/you/Music
  -> Artist/Album/01 Track.mp3: 600x600 (84 KiB, ID3) => 256x256 JPEG (18 KiB), ID3v2.3
  ok Artist/Album/02 Track.mp3: already RNS-E compatible
  -- Artist/Album/03 Track.mp3: no album art

3 MP3 files: 1 converted, 1 already compatible, 1 without art, 0 failed
```

## Options

| Option | Default | Description |
| --- | --- | --- |
| `folder` | — | folder containing the MP3 files |
| `--no-recursive` | recursive | only process this folder, not its subfolders |
| `--size PX` | `256` | edge length of the generated cover |
| `--quality Q` | `85` | initial JPEG quality |
| `--min-quality Q` | `45` | lowest quality used to reach `--max-bytes` |
| `--max-bytes N` | `65536` | size ceiling for the embedded JPEG |
| `--fit {contain,cover,stretch}` | `contain` | how to square up non-square art: pad, centre crop, or distort |
| `--pad-color RRGGBB` | `000000` | padding colour for `--fit contain` |
| `--id3 {v23,keep}` | `v23` | always write ID3v2.3, or keep the source tag version |
| `--padding N` | `0` | zero padding appended to the rewritten tag |
| `--folder-fallback` | off | use `cover.jpg`/`folder.jpg`/`albumart.jpg` when no art is embedded |
| `--write-albumart-jpg` | off | also write the converted cover as `albumart.jpg` next to the MP3 |
| `--force` | off | re-encode even when the art is already compliant |
| `--backup` | off | keep the original as `<name>.mp3.bak` |
| `--update-mtime` | mtime kept | let the modification time change |
| `--backend {auto,pillow,gdk}` | `auto` | image library for scaling and JPEG encoding |
| `-n`, `--dry-run` | off | report what would change without touching any file |
| `-q`, `--quiet` | off | only print the summary |

Full list: `python3 rnse_albumart.py --help`

## Notes and caveats

* **It edits files in place.** Use `-n` first and `--backup` on the first real run.
* Re-encoding is lossy. The script skips files that are already compliant, so
  repeated runs do not degrade quality — but `--force` will.
* `--size` is configurable, yet 256 is what the 193 PU firmware documentation
  recommends; other values are unlikely to help.
* The **193 PU** firmware is the target. Earlier RNS-E firmware versions do not
  display album art at all.
* Non-MP3 files and folders without MP3s are ignored. A file whose tag cannot be
  parsed is reported and counted as failed, never rewritten.

## License

GNU General Public License v3.0 or later — see [LICENSE](LICENSE).

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.
