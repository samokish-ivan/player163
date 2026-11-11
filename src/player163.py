#!/usr/bin/env python3
"""
player163 — a small, no-fuss music player for Linux Mint / Ubuntu / Debian.

  * Pick a folder, it scans it (and remembers it; later scans only re-read changed files).
  * Browse by Songs, Artists or Albums, type anywhere to search.
  * Fix tags (single song, many songs, or rename a whole artist/album) — written to the files.
  * Gapless playback through GStreamer: FLAC, ALAC, MP3, AAC, OGG, Opus, WAV, AIFF, APE, WavPack, WMA...
  * Shuffle, repeat (all / one), volume, media keys + Cinnamon sound applet (MPRIS).

GTK 3 + GStreamer + mutagen. MIT licence.
Author: Ivan Samokish
"""

import base64
import hashlib
import json
import os
import queue
import random
import re
import shutil
import sqlite3
import sys
import threading
import math
import time
import urllib.error
import urllib.parse
import urllib.request

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("Gst", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gst, Gtk, Pango  # noqa: E402
try:  # tray icon on Linux Mint (Cinnamon, MATE, Xfce)
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp  # noqa: E402
except (ValueError, ImportError):
    XApp = None

try:
    import cairo
except ImportError:  # pragma: no cover
    cairo = None

try:
    import mutagen
    import mutagen.id3 as id3frames
    from mutagen.apev2 import APEv2
    from mutagen.asf import ASFTags
    from mutagen.flac import Picture
    from mutagen.id3 import ID3
    try:
        from mutagen.mp4 import MP4Cover
    except Exception:
        MP4Cover = None
except ImportError:  # pragma: no cover
    mutagen = None

APP_NAME = "player163"
APP_ID = "com.smokyfish.Player163"
VERSION = "1.7.1"

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "player163")
DATA_DIR = os.path.join(GLib.get_user_data_dir(), "player163")
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "player163")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
ARTIST_DIR = os.path.join(DATA_DIR, "artists")   # artist pictures (removed in 1.7.1; cleaned up)
COVER_DIR = os.path.join(DATA_DIR, "covers")
USER_AGENT = "player163/%s (https://github.com/samokish-ivan/player163) Python-urllib/%d.%d" % (
    VERSION, sys.version_info[0], sys.version_info[1])
THUMB_DIR = os.path.join(CACHE_DIR, "thumbs")
ALBUM_THUMB = 40
SMALL_THUMB = 18
GRID_THUMB = 150
MAX_WATCHED_DIRS = 8000

AUDIO_EXTS = {
    ".mp3", ".mp2", ".flac", ".m4a", ".m4b", ".mp4", ".alac", ".aac", ".ogg", ".oga",
    ".opus", ".spx", ".wav", ".wave", ".aif", ".aiff", ".aifc", ".ape", ".wv", ".wma",
    ".asf", ".mpc", ".tta", ".dsf", ".dff", ".mka", ".webm", ".ac3", ".caf", ".w64",
}

SMART_LISTS = (("smart:added", "Recently Added", "Newest in your library"),
               ("smart:most", "Most Played", "Your top songs"),
               ("smart:recent", "Recently Played", "What you listened to lately"),
               ("smart:never", "Never Played", "Songs waiting for a first listen"))
SMART_LIMIT = 300
UNKNOWN_ARTIST = "Unknown Artist"
UNKNOWN_ALBUM = "Unknown Album"
ALL_KEY = "__player163_all__"  # must not contain NUL: GTK stores C strings
ART_SIZE = 52
SIDE_ART = 232
SIDE_WIDTH = 280
SPEEDS = (1.0, 1.25, 1.5, 1.75, 2.0, 0.75)

# ----------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------


def fmt_time(sec):
    sec = int(max(0, sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def fmt_total(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m = rem // 60
    if h >= 24:
        d, h = divmod(h, 24)
        return "%d d %d h" % (d, h)
    if h:
        return "%d h %d min" % (h, m)
    return "%d min" % max(1, m) if sec else "0 min"


def sort_key(s):
    s = (s or "").casefold().strip()
    if s.startswith("the "):
        s = s[4:]
    return (s == "", s)  # empty / unknown values sort last


def to_int(s):
    m = re.match(r"\s*(\d+)", s or "")
    return int(m.group(1)) if m else 0


def to_year(s):
    m = re.search(r"\d{4}", s or "")
    return m.group(0) if m else ""


def esc(s):
    return GLib.markup_escape_text(s or "")


def on_screen(x, y):
    """True if (x, y) lies on a connected monitor (a saved window spot may be on an unplugged screen)."""
    try:
        x, y = int(x), int(y)
        disp = Gdk.Display.get_default()
        for i in range(disp.get_n_monitors()):
            g = disp.get_monitor(i).get_geometry()
            if g.x <= x < g.x + g.width - 60 and g.y <= y < g.y + g.height - 60:
                return True
    except Exception:
        pass
    return False


CFG_TYPES = {"long_minutes": (int, float), "long_speed": (int, float), "text_scale": (int, float),
             "volume": (int, float), "width": int, "height": int, "pane": int, "last_pos": (int, float),
             "folders": list, "player_pos": str, "theme": str, "mode": str}


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return {}
    if not isinstance(cfg, dict):
        return {}
    for k, typ in CFG_TYPES.items():
        if k in cfg and (not isinstance(cfg[k], typ) or isinstance(cfg[k], bool)):
            del cfg[k]
    if "folders" in cfg:
        cfg["folders"] = [f for f in cfg["folders"] if isinstance(f, str) and os.path.isabs(f)]
    if cfg.get("theme") not in (None, "dark", "light"):
        del cfg["theme"]
    if "long_speed" in cfg and cfg["long_speed"] not in SPEEDS:
        cfg["long_speed"] = 1.0
    if "text_scale" in cfg and not 0.8 <= cfg["text_scale"] <= 1.5:
        cfg["text_scale"] = 1.0
    if "long_minutes" in cfg and not 1 <= cfg["long_minutes"] <= 600:
        cfg["long_minutes"] = 20
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        print("player163: could not save settings:", e, file=sys.stderr)


# ----------------------------------------------------------------------------------------
# Tags (mutagen)
# ----------------------------------------------------------------------------------------

TAG_FIELDS = ("title", "artist", "album", "albumartist", "tracknumber", "discnumber", "date", "genre")
ID3_MAP = {"title": "TIT2", "artist": "TPE1", "album": "TALB", "albumartist": "TPE2",
           "tracknumber": "TRCK", "discnumber": "TPOS", "date": "TDRC", "genre": "TCON"}
ASF_MAP = {"title": "Title", "artist": "Author", "album": "WM/AlbumTitle",
           "albumartist": "WM/AlbumArtist", "tracknumber": "WM/TrackNumber",
           "discnumber": "WM/PartOfSet", "date": "WM/Year", "genre": "WM/Genre"}
APE_MAP = {"title": "Title", "artist": "Artist", "album": "Album", "albumartist": "Album Artist",
           "tracknumber": "Track", "discnumber": "Disc", "date": "Year", "genre": "Genre"}


def _tag_kind(tags):
    if isinstance(tags, ID3):
        return "id3"      # WAV, AIFF, DSF (raw ID3)
    if isinstance(tags, ASFTags):
        return "asf"      # WMA
    if isinstance(tags, APEv2):
        return "ape"      # APE, WavPack, Musepack, TAK
    return "easy"         # MP3 (EasyID3), MP4/M4A/ALAC, FLAC, Ogg, Opus


def read_tags(path):
    out = dict.fromkeys(TAG_FIELDS, "")
    length = 0.0
    if mutagen is None:
        return out, length
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        return out, length
    if f is None:
        return out, length
    try:
        length = float(getattr(f.info, "length", 0) or 0)
    except Exception:
        pass
    tags = f.tags
    if tags is None:
        return out, length
    kind = _tag_kind(tags)
    for field in TAG_FIELDS:
        val = ""
        try:
            if kind == "id3":
                frames = tags.getall(ID3_MAP[field])
                if frames:
                    fr = frames[0]
                    if field == "genre" and getattr(fr, "genres", None):
                        val = fr.genres[0]
                    elif fr.text:
                        val = str(fr.text[0])
            elif kind == "asf":
                v = tags.get(ASF_MAP[field])
                if v:
                    val = str(v[0].value)
            elif kind == "ape":
                v = tags.get(APE_MAP[field])
                if v is not None:
                    val = str(v).split("\0")[0]
            else:
                v = tags.get(field)
                if v:
                    val = str(v[0])
        except Exception:
            val = ""
        out[field] = (val or "").strip()
    return out, length


def write_tags(path, changes):
    """changes: {tag_field: value}; an empty value removes the tag."""
    if mutagen is None:
        raise RuntimeError("python3-mutagen is not installed")
    f = mutagen.File(path, easy=True)
    if f is None:
        raise ValueError("this file type can't hold tags")
    if f.tags is None:
        f.add_tags()
    tags = f.tags
    kind = _tag_kind(tags)
    for field, value in changes.items():
        if kind == "id3":
            fid = ID3_MAP[field]
            if value:
                tags.setall(fid, [getattr(id3frames, fid)(encoding=3, text=[value])])
            else:
                tags.delall(fid)
        elif kind == "asf":
            key = ASF_MAP[field]
            if value:
                tags[key] = [value]
            elif key in tags:
                del tags[key]
        elif kind == "ape":
            key = APE_MAP[field]
            if value:
                tags[key] = value
            elif key in tags:
                del tags[key]
        else:
            if value:
                tags[field] = [value]
            elif field in tags:
                del tags[field]
    f.save()


def read_embedded_cover(path):
    try:
        f = mutagen.File(path)
        return _embedded_cover(f) if f is not None else None
    except Exception:
        return None


def embed_cover(path, data, mime="image/jpeg"):
    """Write (or with data=None remove) the front cover inside the file. False if the format can't hold one."""
    if mutagen is None:
        raise RuntimeError("python3-mutagen is not installed")
    f = mutagen.File(path)
    if f is None:
        raise ValueError("this file type can't hold tags")
    if hasattr(f, "add_picture") and hasattr(f, "clear_pictures"):   # FLAC
        keep = [pic for pic in f.pictures if pic.type != 3]
        f.clear_pictures()
        for pic in keep:
            f.add_picture(pic)
        if data:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, mime, "Cover", data
            f.add_picture(pic)
        f.save()
        return True
    if f.tags is None:
        f.add_tags()
    tags = f.tags
    if isinstance(tags, ID3):   # MP3, WAV, AIFF
        for fr in list(tags.getall("APIC")):
            if fr.type == 3 or len(tags.getall("APIC")) == 1:
                tags.delall(fr.HashKey)
        if data:
            tags.add(id3frames.APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        f.save()
        return True
    if isinstance(tags, (APEv2, ASFTags)):
        return False
    if MP4Cover is not None and type(tags).__name__ == "MP4Tags":   # M4A / ALAC
        if data:
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        elif "covr" in tags:
            del tags["covr"]
        f.save()
        return True
    if hasattr(tags, "keys"):   # Ogg Vorbis / Opus (Vorbis comments)
        for k in ("metadata_block_picture", "coverart", "coverartmime"):
            if k in tags:
                del tags[k]
        if data:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, mime, "Cover", data
            tags["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
        f.save()
        return True
    return False


def _image_mime(data):
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def track_from_file(path, mtime=None):
    if mtime is None:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = 0.0
    t, length = read_tags(path)
    return {
        "path": path, "mtime": mtime, "title": t["title"], "artist": t["artist"],
        "album": t["album"], "albumartist": t["albumartist"],
        "track": to_int(t["tracknumber"]), "disc": to_int(t["discnumber"]),
        "year": to_year(t["date"]), "genre": t["genre"], "duration": float(length or 0),
    }


def prepare(t):
    """Add display strings, grouping keys and sort keys (not stored in the DB)."""
    base = os.path.basename(t["path"])
    t["d_title"] = t["title"] or os.path.splitext(base)[0]
    t["d_artist"] = t["artist"] or UNKNOWN_ARTIST
    t["d_album"] = t["album"] or UNKNOWN_ALBUM
    aa = t["albumartist"] or t["artist"]
    t["d_albumartist"] = aa or UNKNOWN_ARTIST
    t["album_key"] = t["album"] + "\x1f" + aa
    t["sk_title"] = sort_key(t["d_title"])
    t["sk_artist"] = sort_key(t["artist"])
    t["sk_album"] = sort_key(t["album"])
    t["sk_albumartist"] = sort_key(aa)
    t["hay"] = " ".join((t["d_title"], t["artist"], t["album"], t["albumartist"],
                         t["genre"], t["year"], base)).casefold()
    return t


# ----------------------------------------------------------------------------------------
# Cover art
# ----------------------------------------------------------------------------------------

COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "albumartlarge", "albumartsmall")


def _embedded_cover(f):
    pics = getattr(f, "pictures", None)  # FLAC
    if pics:
        return sorted(pics, key=lambda p: p.type != 3)[0].data
    tags = f.tags
    if tags is None:
        return None
    if hasattr(tags, "getall"):  # ID3 (MP3, WAV, AIFF)
        apics = tags.getall("APIC")
        if apics:
            return sorted(apics, key=lambda p: p.type != 3)[0].data
        return None
    if isinstance(tags, APEv2):
        v = tags.get("Cover Art (Front)")
        if v is not None:
            raw = v.value
            return raw[raw.find(b"\0") + 1:]
        return None
    if isinstance(tags, ASFTags):
        return None
    covr = tags.get("covr")  # MP4 / M4A / ALAC
    if covr:
        return bytes(covr[0])
    mbp = tags.get("metadata_block_picture")  # Ogg Vorbis / Opus
    if mbp:
        return Picture(base64.b64decode(mbp[0])).data
    return None


def cover_files(album_key):
    base = os.path.join(COVER_DIR, hashlib.md5(album_key.encode("utf-8", "surrogateescape"),
                                               usedforsecurity=False).hexdigest())
    return base + ".img", base + ".json"


def cover_meta(album_key):
    try:
        with open(cover_files(album_key)[1], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def stored_cover(album_key):
    try:
        fn = cover_files(album_key)[0]
        if os.path.getsize(fn) <= MAX_COVER_BYTES:
            with open(fn, "rb") as f:
                return f.read()
    except OSError:
        pass
    return None


def stored_cover_stamp(album_key):
    try:
        return os.stat(cover_files(album_key)[0]).st_mtime
    except OSError:
        return 0


def normalize_cover(data, limit=1200, reencode=False):
    """Any JPEG/PNG/GIF → (bytes, mime), at most `limit` px, safe to embed. None if unusable.
    reencode=True always decodes and re-saves the pixels (used for anything downloaded)."""
    dims = image_dimensions(data) if data else None
    if not dims:
        return None
    if reencode:
        pass
    elif data[:2] == b"\xff\xd8" and max(dims) <= limit:
        return data, "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n" and max(dims) <= limit:
        return data, "image/png"
    pb = pixbuf_from_bytes(data, min(limit, max(dims)))
    if pb is None:
        return None
    if pb.get_has_alpha():
        ok, buf = pb.save_to_bufferv("png", [], [])
        return (bytes(buf), "image/png") if ok else None
    ok, buf = pb.save_to_bufferv("jpeg", ["quality"], ["90"])
    return (bytes(buf), "image/jpeg") if ok else None


def save_stored_cover(album_key, data, meta):
    os.makedirs(COVER_DIR, exist_ok=True)
    img, js = cover_files(album_key)
    if data is None:
        try:
            os.remove(img)
        except OSError:
            pass
    else:
        tmp = img + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, img)
    if meta is None:
        try:
            os.remove(js)
        except OSError:
            pass
    else:
        with open(js, "w", encoding="utf-8") as f:
            json.dump(dict(meta, fetched=meta.get("fetched", time.time())), f)


def find_cover(path, album_key=None):
    """Your own cover (set in player163) → embedded art → cover.jpg & co. → cover found online."""
    if album_key:
        meta = cover_meta(album_key)
        if meta and meta.get("source") == "custom":
            data = stored_cover(album_key)
            if data:
                return data
    data = _local_cover(path)
    if data is None and album_key:
        data = stored_cover(album_key)
    return data


def _local_cover(path):
    if mutagen is not None:
        try:
            f = mutagen.File(path)
            if f is not None:
                data = _embedded_cover(f)
                if data:
                    return data
        except Exception:
            pass
    folder = os.path.dirname(path)
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    images = [n for n in names if os.path.splitext(n.lower())[1] in (".jpg", ".jpeg", ".png")]
    best, best_rank = None, 999
    for n in images:
        stem = os.path.splitext(n.lower())[0]
        if stem in COVER_NAMES and COVER_NAMES.index(stem) < best_rank:
            best, best_rank = n, COVER_NAMES.index(stem)
    if best is None and len(images) == 1:
        best = images[0]
    if best is None:
        return None
    try:
        fn = os.path.join(folder, best)
        if os.path.getsize(fn) > MAX_COVER_BYTES:
            return None
        with open(fn, "rb") as fh:
            return fh.read()
    except OSError:
        return None


MAX_COVER_BYTES = 30 * 1024 * 1024   # skip absurdly large cover files
MAX_COVER_PIXELS = 40_000_000        # ~6300 x 6300; refuses "decompression bomb" images


def image_dimensions(data):
    """Width/height from a PNG, JPEG or GIF header without decoding it; None if unknown/invalid."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data[:2] == b"\xff\xd8":  # JPEG: walk the markers to the frame header
            i, n = 2, len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xFF:
                    i += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seglen = int.from_bytes(data[i + 2:i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return w, h
                if seglen < 2:
                    return None
                i += 2 + seglen
    except Exception:
        pass
    return None


def pixbuf_from_bytes(data, size):
    """Decode cover art safely: only PNG/JPEG/GIF whose header reports a sane size."""
    if not data or len(data) > MAX_COVER_BYTES:
        return None
    dims = image_dimensions(data)
    if not dims or dims[0] <= 0 or dims[1] <= 0 or dims[0] * dims[1] > MAX_COVER_PIXELS:
        return None
    loader = GdkPixbuf.PixbufLoader()

    def on_size(ld, w, h):
        scale = size / float(max(w, h, 1))
        ld.set_size(max(1, int(w * scale)), max(1, int(h * scale)))

    loader.connect("size-prepared", on_size)
    try:
        loader.write(data)
        loader.close()
        return loader.get_pixbuf()
    except Exception:
        try:
            loader.close()
        except Exception:
            pass
        return None


# ----------------------------------------------------------------------------------------
# Missing album covers: MusicBrainz (album → release group) → Cover Art Archive
# ----------------------------------------------------------------------------------------

RETRY_CODES = (429, 500, 502, 503, 504)   # "busy / slow down": wait and try again
RETRY_DELAYS = (3, 8, 20)
LOOKUP_RETRY_SCALE = 1.0   # tests shrink the "try again in a minute" wait
MB_GAP = [1.1]   # seconds between MusicBrainz requests; grows after a "busy" answer


def _retry_wait(err, attempt):
    try:
        ra = int(err.headers.get("Retry-After", "")) if getattr(err, "headers", None) else 0
    except (TypeError, ValueError):
        ra = 0
    return min(60, max(ra, RETRY_DELAYS[attempt]))


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise urllib.error.HTTPError(newurl, code, "refusing redirect to a non-https address", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Only https; no proxies-from-files, no ftp/file/data handlers: just HTTPS with safe redirects.
_OPENER = urllib.request.OpenerDirector()
for _h in (urllib.request.ProxyHandler(), urllib.request.HTTPSHandler(), _HttpsOnlyRedirects(),
           urllib.request.HTTPErrorProcessor(), urllib.request.HTTPDefaultErrorHandler()):
    _OPENER.add_handler(_h)


def _fetch(url, headers, timeout, read):
    """GET with retries for busy servers (HTTP 429/5xx) and timeouts. Other errors are raised at once."""
    if not url.startswith("https://"):
        raise ValueError("refusing non-https URL")
    attempt = 0
    while True:
        req = urllib.request.Request(url, headers=dict(headers, **{"User-Agent": USER_AGENT}))
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                return read(r)
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_CODES or attempt >= len(RETRY_DELAYS):
                raise
            if "musicbrainz.org" in url:
                MB_GAP[0] = 2.0   # be gentler for the rest of the session
            wait = _retry_wait(e, attempt)
        except (TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            timed_out = isinstance(reason, TimeoutError) or "timed out" in str(reason)
            if not timed_out or attempt >= len(RETRY_DELAYS):
                raise
            wait = RETRY_DELAYS[attempt]
        print("player163: %s is busy, retrying in %ds" % (urllib.parse.urlparse(url).hostname, wait), file=sys.stderr)
        time.sleep(wait)
        attempt += 1


def _http_json(url):
    return _fetch(url, {"Accept": "application/json"}, 15,
                  lambda r: json.loads(r.read(2 * 1024 * 1024).decode("utf-8")))


def _http_bytes(url):
    data = _fetch(url, {}, 20, lambda r: r.read(MAX_COVER_BYTES + 1))
    return data if len(data) <= MAX_COVER_BYTES else None


class CoverFinder:
    """Background cover lookups, one at a time, never more than one request per second to MusicBrainz:
    MusicBrainz (album → release group) → Cover Art Archive (front cover)."""

    def __init__(self, on_album):
        self.on_album = on_album
        self.q = queue.Queue()
        self.pending = set()  # touched only from the main thread
        self._last_mb = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def request_album(self, key, album, artist):
        tag = "album:" + key
        if tag not in self.pending:
            self.pending.add(tag)
            self.q.put(("album", key, album, artist))

    def find_album(self, album, artist):
        def clean(x):
            return x.replace('"', " ").replace("\\", " ")
        query = 'releasegroup:"%s"' % clean(album)
        if artist:
            query += ' AND artist:"%s"' % clean(artist)
        res = self._mb("https://musicbrainz.org/ws/2/release-group/?" +
                       urllib.parse.urlencode({"query": query, "fmt": "json", "limit": 5}))
        want = sort_key(album)
        for rg in res.get("release-groups", []):
            rid = rg.get("id", "")
            if int(rg.get("score", 0)) < 90 or sort_key(rg.get("title", "")) != want:
                continue
            if not re.match(r"^[0-9a-f-]{36}$", rid):
                continue
            try:
                data = _http_bytes("https://coverartarchive.org/release-group/%s/front-500" % rid)
            except urllib.error.HTTPError as e:
                if e.code == 404:   # this edition has no cover: try the next match
                    continue
                raise
            if data and image_dimensions(data):
                return {"data": data, "source": "caa", "mbid": rid,
                        "page": "https://musicbrainz.org/release-group/%s" % rid}
        return None

    def _mb(self, url):
        wait = MB_GAP[0] - (time.monotonic() - self._last_mb)
        if wait > 0:
            time.sleep(wait)
        self._last_mb = time.monotonic()
        return _http_json(url)

    def _run(self):
        while True:
            _kind, key, album, artist = self.q.get()
            err = None
            try:
                res = self.find_album(album, artist)
                ok = True
            except Exception as e:  # offline, timeout, service hiccup: try again later
                print("player163: cover lookup failed for %r: %s" % (album, e), file=sys.stderr)
                res, ok, err = None, False, (str(e) or e.__class__.__name__)
            GLib.idle_add(self.on_album, key, res, ok, err)


# ----------------------------------------------------------------------------------------
# Volume leveling (ReplayGain) and equalizer
# ----------------------------------------------------------------------------------------

EQ_LABELS = ("31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k")
EQ_PRESETS = {
    "Flat": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "Bass Boost": (6, 5, 4, 2, 0, 0, 0, 0, 0, 0),
    "Treble Boost": (0, 0, 0, 0, 0, 1, 2, 4, 5, 6),
    "Vocal": (-2, -2, -1, 1, 3, 4, 3, 1, 0, -1),
    "Rock": (4, 3, 2, 0, -1, -1, 1, 2, 3, 4),
    "Pop": (-1, 1, 3, 4, 3, 1, 0, -1, -1, -1),
    "Jazz": (3, 2, 1, 2, -1, -1, 0, 1, 2, 3),
    "Classical": (3, 2, 1, 0, 0, 0, -1, -1, 1, 2),
    "Electronic": (4, 3, 1, 0, -2, 1, 0, 1, 3, 4),
    "Late Night": (-2, -1, 0, 1, 2, 2, 1, 0, -1, -2),
}
_RG_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _rg_float(v):
    m = _RG_NUM.search(str(v))
    return float(m.group(0)) if m else None


def read_replaygain(path):
    """ReplayGain/R128 values already stored in the file: (track_gain, track_peak, album_gain)."""
    if mutagen is None:
        return None
    try:
        f = mutagen.File(path)
        tags = f.tags if f is not None else None
        if tags is None:
            return None
        vals = {}
        if isinstance(tags, ID3):
            for fr in tags.getall("TXXX"):
                vals[fr.desc.lower()] = str(fr.text[0]) if fr.text else ""
        else:
            for k, v in tags.items():
                k2 = str(k).lower()
                if k2.startswith("----:com.apple.itunes:"):
                    k2 = k2.split(":")[-1]
                if isinstance(v, list):
                    v = v[0] if v else ""
                if hasattr(v, "value"):
                    v = v.value
                if isinstance(v, (bytes, bytearray)):
                    v = bytes(v).decode("utf-8", "ignore")
                vals[k2] = str(v)
    except Exception:
        return None
    tg = _rg_float(vals["replaygain_track_gain"]) if "replaygain_track_gain" in vals else None
    ag = _rg_float(vals["replaygain_album_gain"]) if "replaygain_album_gain" in vals else None
    pk = _rg_float(vals["replaygain_track_peak"]) if "replaygain_track_peak" in vals else None
    try:  # Opus R128 gains are Q7.8 relative to -23 LUFS; ReplayGain's reference is ~5 dB louder
        if tg is None and "r128_track_gain" in vals:
            tg = int(vals["r128_track_gain"]) / 256.0 + 5.0
        if ag is None and "r128_album_gain" in vals:
            ag = int(vals["r128_album_gain"]) / 256.0 + 5.0
    except ValueError:
        pass
    return tg, pk, ag


def analyze_loudness(path):
    """(track_gain, peak, album_gain, source). Uses tags if present, else measures the file."""
    tags = read_replaygain(path)
    if tags and tags[0] is not None:
        return tags[0], tags[1], tags[2], "tag"
    pipe = Gst.parse_launch("filesrc name=src ! decodebin ! audioconvert ! audioresample ! "
                            "rganalysis name=rga ! fakesink sync=false")
    pipe.get_by_name("src").set_property("location", path)
    rga = pipe.get_by_name("rga")
    found = {}

    def probe(pad, info):  # rganalysis sends its result downstream as a tag event at the end
        ev = info.get_event()
        if ev is not None and ev.type == Gst.EventType.TAG:
            tl = ev.parse_tag()
            ok, g = tl.get_double("replaygain-track-gain")
            if ok:
                found["gain"] = g
            ok, pk = tl.get_double("replaygain-track-peak")
            if ok:
                found["peak"] = pk
        return Gst.PadProbeReturn.OK

    rga.get_static_pad("src").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, probe)
    bus = pipe.get_bus()
    pipe.set_state(Gst.State.PLAYING)
    deadline = time.monotonic() + 600
    try:
        while time.monotonic() < deadline:
            msg = bus.timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if msg is not None:
                break
    finally:
        pipe.set_state(Gst.State.NULL)
    if "gain" in found:
        return found["gain"], found.get("peak"), None, "scan"
    return None


class LoudnessAnalyzer:
    """Measures loudness of songs without ReplayGain tags, one at a time, at low priority."""

    def __init__(self, on_result):
        self.on_result = on_result
        self.q = queue.Queue()
        self.queued = set()  # touched only from the main thread
        self.paused = False
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, items):
        for path, mtime in items:
            if path not in self.queued:
                self.queued.add(path)
                self.q.put((path, mtime))

    def _run(self):
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 15)
        except Exception:
            pass
        while True:
            path, mtime = self.q.get()
            while self.paused:
                time.sleep(1)
            try:
                res = analyze_loudness(path)
            except Exception:
                res = None
            GLib.idle_add(self.on_result, path, mtime, res)


# ----------------------------------------------------------------------------------------
# Library (SQLite cache of the scanned folder)
# ----------------------------------------------------------------------------------------


class Library:
    COLS = ("path", "mtime", "title", "artist", "album", "albumartist", "track", "disc",
            "year", "genre", "duration")

    def __init__(self, dbpath):
        os.makedirs(os.path.dirname(dbpath), exist_ok=True)
        self.db = sqlite3.connect(dbpath)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS tracks (path TEXT PRIMARY KEY, mtime REAL, title TEXT, "
            "artist TEXT, album TEXT, albumartist TEXT, track INTEGER, disc INTEGER, year TEXT, "
            "genre TEXT, duration REAL)")
        self.db.commit()
        self.tracks = {}
        for row in self.db.execute("SELECT %s FROM tracks" % ",".join(self.COLS)):
            t = dict(zip(self.COLS, row))
            for k in ("title", "artist", "album", "albumartist", "year", "genre"):
                t[k] = t[k] or ""
            t["track"] = t["track"] or 0
            t["disc"] = t["disc"] or 0
            t["duration"] = t["duration"] or 0.0
            self.tracks[t["path"]] = prepare(t)
        self.db.execute("CREATE TABLE IF NOT EXISTS favorites (path TEXT PRIMARY KEY, added REAL)")
        self.db.commit()
        self.favs = dict(self.db.execute("SELECT path, added FROM favorites"))
        self.db.execute("CREATE TABLE IF NOT EXISTS loudness (path TEXT PRIMARY KEY, mtime REAL, "
                        "track_gain REAL, peak REAL, album_gain REAL, source TEXT)")
        self.db.commit()
        self.loud = {r[0]: r[1:] for r in self.db.execute(
            "SELECT path, mtime, track_gain, peak, album_gain, source FROM loudness")}
        self._init_extra()

    def _init_extra(self):
        db = self.db
        db.execute("CREATE TABLE IF NOT EXISTS plays (path TEXT PRIMARY KEY, count INTEGER, last REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS added (path TEXT PRIMARY KEY, ts REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS playlists (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, created REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS playlist_items (playlist_id INTEGER, pos INTEGER, path TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS resume (path TEXT PRIMARY KEY, pos REAL, updated REAL)")
        db.commit()
        self.plays = {r[0]: (r[1], r[2]) for r in db.execute("SELECT path, count, last FROM plays")}
        self.added = dict(db.execute("SELECT path, ts FROM added"))
        self.resume = {r[0]: r[1] for r in db.execute("SELECT path, pos FROM resume")}
        self.playlists = {}
        for pid, name, created in db.execute("SELECT id, name, created FROM playlists ORDER BY created"):
            self.playlists[pid] = {"name": name, "items": []}
        for pid, path in db.execute("SELECT playlist_id, path FROM playlist_items ORDER BY playlist_id, pos"):
            if pid in self.playlists:
                self.playlists[pid]["items"].append(path)
        missing = [p for p in self.tracks if p not in self.added]
        if missing:
            self._note_added(missing)

    def _note_added(self, paths):
        rows = []
        for p in paths:
            try:
                ts = os.stat(p).st_ctime  # when the file arrived on disk
            except OSError:
                ts = time.time()
            self.added[p] = ts
            rows.append((p, ts))
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO added VALUES (?, ?)", rows)

    def record_play(self, path):
        c = self.plays.get(path, (0, 0))[0] + 1
        now = time.time()
        self.plays[path] = (c, now)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO plays VALUES (?, ?, ?)", (path, c, now))

    def set_resume(self, path, pos):
        self.resume[path] = pos
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO resume VALUES (?, ?, ?)", (path, pos, time.time()))

    def clear_resume(self, path):
        if self.resume.pop(path, None) is not None:
            with self.db:
                self.db.execute("DELETE FROM resume WHERE path = ?", (path,))

    def create_playlist(self, name, paths=()):
        with self.db:
            cur = self.db.execute("INSERT INTO playlists (name, created) VALUES (?, ?)", (name, time.time()))
        pid = cur.lastrowid
        self.playlists[pid] = {"name": name, "items": []}
        if paths:
            self.set_playlist_items(pid, list(dict.fromkeys(paths)))
        return pid

    def rename_playlist(self, pid, name):
        self.playlists[pid]["name"] = name
        with self.db:
            self.db.execute("UPDATE playlists SET name = ? WHERE id = ?", (name, pid))

    def delete_playlist(self, pid):
        self.playlists.pop(pid, None)
        with self.db:
            self.db.execute("DELETE FROM playlists WHERE id = ?", (pid,))
            self.db.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (pid,))

    def set_playlist_items(self, pid, paths):
        self.playlists[pid]["items"] = list(paths)
        with self.db:
            self.db.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (pid,))
            self.db.executemany("INSERT INTO playlist_items VALUES (?, ?, ?)",
                                [(pid, i, p) for i, p in enumerate(paths)])

    def add_to_playlist(self, pid, paths):
        items = self.playlists[pid]["items"]
        new = [p for p in dict.fromkeys(paths) if p not in items]
        self.set_playlist_items(pid, items + new)
        return len(new)

    def set_loudness(self, path, mtime, tg, peak, ag, source):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO loudness VALUES (?, ?, ?, ?, ?, ?)",
                            (path, mtime, tg, peak, ag, source))
        self.loud[path] = (mtime, tg, peak, ag, source)

    def set_favorite(self, paths, on):
        now = time.time()
        with self.db:
            for i, p in enumerate(paths):
                if on and p not in self.favs:
                    self.favs[p] = now + i * 1e-3  # keep selection order
                    self.db.execute("INSERT OR REPLACE INTO favorites VALUES (?, ?)", (p, self.favs[p]))
                elif not on and p in self.favs:
                    del self.favs[p]
                    self.db.execute("DELETE FROM favorites WHERE path = ?", (p,))

    def move_favorite(self, old, new):
        added = self.favs.pop(old, None)
        if added is None:
            return
        self.favs[new] = added
        with self.db:
            self.db.execute("DELETE FROM favorites WHERE path = ?", (old,))
            self.db.execute("INSERT OR REPLACE INTO favorites VALUES (?, ?)", (new, added))

    def upsert(self, tracks):
        if not tracks:
            return
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO tracks (%s) VALUES (%s)" % (",".join(self.COLS), ",".join("?" * len(self.COLS))),
                [tuple(t[c] for c in self.COLS) for t in tracks])
        new = [t["path"] for t in tracks if t["path"] not in self.tracks]
        for t in tracks:
            self.tracks[t["path"]] = prepare(t)
        if new and hasattr(self, "added"):
            self._note_added([p for p in new if p not in self.added])

    def remove(self, paths):
        paths = list(paths)
        if not paths:
            return
        with self.db:
            self.db.executemany("DELETE FROM tracks WHERE path = ?", [(p,) for p in paths])
        for p in paths:
            self.tracks.pop(p, None)

    def clear(self):
        with self.db:
            self.db.execute("DELETE FROM tracks")
        self.tracks = {}


def scan_worker(roots, known, cancelled, on_batch, on_progress, on_done):
    """Runs in a thread. Callbacks are invoked through GLib.idle_add by the caller's wrappers."""
    files = []
    seen_dirs = set()
    dirs = []
    walks = (w for r in roots if os.path.isdir(r) for w in os.walk(r, followlinks=True))
    for dirpath, dirnames, filenames in walks:
        if cancelled():
            return
        try:
            st = os.stat(dirpath)
        except OSError:
            dirnames[:] = []
            continue
        if (st.st_dev, st.st_ino) in seen_dirs:  # symlink loop guard
            dirnames[:] = []
            continue
        seen_dirs.add((st.st_dev, st.st_ino))
        dirs.append(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in filenames:
            if not fn.startswith(".") and os.path.splitext(fn)[1].lower() in AUDIO_EXTS:
                files.append(os.path.join(dirpath, fn))
        if len(files) % 500 == 0:
            on_progress(0, len(files))
    seen = set(files)
    total = len(files)
    batch = []
    last = 0.0
    for i, p in enumerate(files):
        if cancelled():
            return
        try:
            mt = os.stat(p).st_mtime
        except OSError:
            seen.discard(p)
            continue
        if known.get(p) != mt:
            batch.append(track_from_file(p, mt))
            if len(batch) >= 300:
                on_batch(batch)
                batch = []
        now = time.monotonic()
        if now - last > 0.15:
            on_progress(i + 1, total)
            last = now
    on_done(batch, seen, dirs)


# ----------------------------------------------------------------------------------------
# Play queue
# ----------------------------------------------------------------------------------------


class PlayQueue:
    def __init__(self):
        self.lock = threading.Lock()
        self.paths = []
        self.order = []
        self.pos = -1
        self.shuffle = False
        self.repeat = "off"  # off | all | one
        self.upnext = []     # "Play Next" / "Add to Queue" songs, played before the normal order
        self.skipped = []    # files that vanished (moved/renamed) and were skipped

    def add_upnext(self, paths, front):
        with self.lock:
            if front:
                self.upnext[0:0] = list(paths)
            else:
                self.upnext.extend(paths)

    def clear_upnext(self):
        with self.lock:
            self.upnext = []

    def _build(self, start):
        n = len(self.paths)
        if self.shuffle:
            rest = [i for i in range(n) if i != start]
            random.shuffle(rest)
            self.order = ([start] if 0 <= start < n else []) + rest
            self.pos = 0 if self.order else -1
        else:
            self.order = list(range(n))
            self.pos = start if 0 <= start < n else (0 if n else -1)

    def set(self, paths, start):
        with self.lock:
            self.paths = list(paths)
            self._build(start)
            return self.pos, self._path(self.pos)

    def set_shuffle(self, on):
        with self.lock:
            cur = self.order[self.pos] if 0 <= self.pos < len(self.order) else 0
            self.shuffle = on
            if self.paths:
                self._build(cur)

    def _path(self, pos):
        if 0 <= pos < len(self.order):
            return self.paths[self.order[pos]]
        return None

    def current(self):
        with self.lock:
            return self._path(self.pos)

    def peek(self, step, auto):
        """(pos, path) of the next/previous song, or None at the end of the queue.
        pos is None for an up-next song. Files that no longer exist are skipped."""
        with self.lock:
            if step == 1:
                while self.upnext:
                    if os.path.exists(self.upnext[0]):
                        return None, self.upnext[0]
                    self.skipped.append(self.upnext.pop(0))
            n = len(self.order)
            if not n:
                return None
            if auto and self.repeat == "one" and 0 <= self.pos < n and os.path.exists(self._path(self.pos)):
                return self.pos, self._path(self.pos)
            p = self.pos
            for _ in range(n):
                np = p + step
                if np >= n:
                    if self.repeat == "off":
                        return None
                    np = 0
                if np < 0:
                    np = n - 1 if self.repeat != "off" else 0
                path = self._path(np)
                if os.path.exists(path):
                    return np, path
                if path not in self.skipped:
                    self.skipped.append(path)
                if np == p:
                    return None
                p = np
            return None

    def take_skipped(self):
        with self.lock:
            out, self.skipped = self.skipped, []
            return out

    def sync(self, pos, path):
        with self.lock:
            if pos is None:  # an up-next song started
                if self.upnext and self.upnext[0] == path:
                    self.upnext.pop(0)
                return
            if self._path(pos) == path:
                self.pos = pos
                return
            for i, idx in enumerate(self.order):
                if self.paths[idx] == path:
                    self.pos = i
                    return


# ----------------------------------------------------------------------------------------
# GStreamer engine
# ----------------------------------------------------------------------------------------


class Engine:
    FLAG_AUDIO = 1 << 1
    FLAG_SOFT_VOLUME = 1 << 4

    def __init__(self, win):
        self.win = win
        self.pb = Gst.ElementFactory.make("playbin", "player")
        if self.pb is None:
            raise RuntimeError("GStreamer 'playbin' is missing (install gstreamer1.0-plugins-base)")
        # No SOFT_VOLUME flag: playbin passes volume/mute to PulseAudio/PipeWire, so our slider *is*
        # player163's slider in Mint's Sound settings → Applications (and changes there come back).
        self.pb.set_property("flags", self.FLAG_AUDIO)
        self.pb.connect("notify::volume", lambda *a: GLib.idle_add(self.win.on_engine_volume))
        self.pb.connect("notify::mute", lambda *a: GLib.idle_add(self.win.on_engine_mute))
        sink_name = os.environ.get("PLAYER163_AUDIOSINK")  # for testing / special setups
        if sink_name:
            sink = Gst.ElementFactory.make(sink_name, None)
            if sink is not None:
                if sink.find_property("sync") is not None:
                    sink.set_property("sync", True)
                self.pb.set_property("audio-sink", sink)
        bus = self.pb.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        self.pb.connect("about-to-finish", self._on_about_to_finish)
        self.state = "stopped"  # stopped | playing | paused
        self._pending_seek = None
        self.rg_mode = "off"      # off | track | album
        self.eq_bands = [0.0] * 10
        self.rg = None
        self.lev = None
        self.eq = None
        self.applied = (None, 0.0)
        self._filter_mode = None
        self.rate = 1.0
        self._have_scaletempo = Gst.ElementFactory.find("scaletempo") is not None

    def _build_filter(self):
        """audio-filter: [rgvolume ! rglimiter] ! equalizer-10bands. Only changeable in NULL state."""
        parts = ["audioconvert", "scaletempo", "audioconvert"] if self._have_scaletempo else ["audioconvert"]
        if self.rg_mode != "off":
            # rgvolume handles files that carry ReplayGain tags; "lev" applies our own measurement
            # to files without tags (set per song in _rg_probe); rglimiter catches any peaks.
            parts += ["rgvolume name=rg fallback-gain=0", "volume name=lev", "rglimiter", "audioconvert"]
        parts += ["equalizer-10bands name=eq", "audioconvert"]
        try:
            fbin = Gst.parse_bin_from_description(" ! ".join(parts), True)
        except Exception as e:
            print("player163: audio filter unavailable:", e, file=sys.stderr)
            self.rg = self.lev = self.eq = None
            self._filter_mode = self.rg_mode
            return
        self.rg = fbin.get_by_name("rg")
        self.lev = fbin.get_by_name("lev")
        self.eq = fbin.get_by_name("eq")
        if self.rg is not None:
            self.rg.set_property("album-mode", self.rg_mode == "album")
            self.rg.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._rg_probe)
        self._apply_eq()
        self.pb.set_property("audio-filter", fbin)
        self._filter_mode = self.rg_mode

    def _rg_probe(self, pad, info):
        """Runs in the streaming thread right as each song starts (also gapless): songs without
        ReplayGain tags get their measured gain as rgvolume's fallback."""
        ev = info.get_event()
        if ev is not None and ev.type == Gst.EventType.STREAM_START and self.lev is not None:
            pend = self.win.pending
            path = pend[1] if pend else self.win.current_path
            try:
                db = float(self.win.level_gain(path))
            except Exception:
                db = 0.0
            self.lev.set_property("volume", 10 ** (db / 20.0))
            self.applied = (path, db)
        return Gst.PadProbeReturn.OK

    def set_eq(self, bands):
        self.eq_bands = [float(b) for b in bands]
        self._apply_eq()

    def _apply_eq(self):
        if self.eq is not None:
            for i, g in enumerate(self.eq_bands):
                self.eq.set_property("band%d" % i, g)

    def set_rate(self, rate):
        """Playback speed; scaletempo keeps voices at their normal pitch."""
        self.rate = rate
        if self.state == "stopped":
            return
        self.pb.seek(rate, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                     Gst.SeekType.SET, int(max(0.0, self.position()) * Gst.SECOND), Gst.SeekType.NONE, -1)

    def load(self, path, start=0.0):
        self.rate = 1.0
        self.pb.set_state(Gst.State.NULL)
        if self._filter_mode != self.rg_mode:
            self._build_filter()
        self.pb.set_property("uri", Gst.filename_to_uri(path))
        self._pending_seek = start if start and start > 0.5 else None
        self.pb.set_state(Gst.State.PLAYING)
        self.state = "playing"

    def play(self):
        self.pb.set_state(Gst.State.PLAYING)
        self.state = "playing"

    def pause(self):
        self.pb.set_state(Gst.State.PAUSED)
        self.state = "paused"

    def stop(self):
        self.pb.set_state(Gst.State.NULL)
        self.state = "stopped"

    def seek(self, sec):
        if self.state == "stopped":
            return
        self.pb.seek(self.rate, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                     Gst.SeekType.SET, int(max(0, sec) * Gst.SECOND), Gst.SeekType.NONE, -1)

    def position(self):
        ok, pos = self.pb.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok and pos >= 0 else 0.0

    def duration(self):
        ok, dur = self.pb.query_duration(Gst.Format.TIME)
        return dur / Gst.SECOND if ok and dur > 0 else 0.0

    def set_volume(self, v):
        self.pb.set_property("volume", max(0.0, min(1.0, v)) ** 3)  # cubic = perceptual

    def set_mute(self, m):
        self.pb.set_property("mute", bool(m))

    # Called from a GStreamer streaming thread: queue the next file for gapless playback.
    def _on_about_to_finish(self, pb):
        nxt = self.win.queue.peek(1, auto=True)
        if nxt:
            self.win.pending = nxt
            pb.set_property("uri", Gst.filename_to_uri(nxt[1]))

    def _on_message(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.STREAM_START:
            self.win.on_stream_start()
        elif t == Gst.MessageType.EOS:
            self.win.on_eos()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print("player163: GStreamer error:", err.message, dbg or "", file=sys.stderr)
            self.win.on_engine_error(err.message)
        elif t == Gst.MessageType.ASYNC_DONE:
            if self._pending_seek is not None:
                s, self._pending_seek = self._pending_seek, None
                self.seek(s)
        elif t == Gst.MessageType.DURATION_CHANGED:
            self.win.on_duration_changed()


# ----------------------------------------------------------------------------------------
# MPRIS (media keys, Cinnamon/GNOME sound applet)
# ----------------------------------------------------------------------------------------

MPRIS_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

MPRIS_ROOT = "org.mpris.MediaPlayer2"
MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"
MPRIS_PATH = "/org/mpris/MediaPlayer2"


class Mpris:
    def __init__(self, win):
        self.win = win
        self.conn = None
        self.art_url = ""
        try:
            self.node = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
            Gio.bus_own_name(Gio.BusType.SESSION, "org.mpris.MediaPlayer2.player163",
                             Gio.BusNameOwnerFlags.NONE, self._on_bus, None, None)
        except Exception as e:
            print("player163: MPRIS unavailable:", e, file=sys.stderr)

    def _on_bus(self, conn, name):
        self.conn = conn
        for iface in self.node.interfaces:
            try:
                conn.register_object(MPRIS_PATH, iface, self._on_call, self._on_get, self._on_set)
            except Exception as e:
                print("player163: MPRIS register failed:", e, file=sys.stderr)

    @staticmethod
    def track_id(path):
        if not path:
            return "/org/mpris/MediaPlayer2/TrackList/NoTrack"
        return "/com/smokyfish/Player163/track/t" + hashlib.md5(path.encode("utf-8", "surrogateescape"), usedforsecurity=False).hexdigest()

    def metadata(self):
        w = self.win
        t = w.get_track(w.current_path) if w.current_path else None
        if not t:
            return {"mpris:trackid": GLib.Variant("o", self.track_id(None))}
        md = {
            "mpris:trackid": GLib.Variant("o", self.track_id(t["path"])),
            "xesam:title": GLib.Variant("s", t["d_title"]),
            "xesam:artist": GLib.Variant("as", [t["d_artist"]]),
            "xesam:album": GLib.Variant("s", t["d_album"]),
            "xesam:albumArtist": GLib.Variant("as", [t["d_albumartist"]]),
            "xesam:url": GLib.Variant("s", Gst.filename_to_uri(t["path"])),
            "mpris:length": GLib.Variant("x", int((w.current_duration or t["duration"]) * 1e6)),
        }
        if t["track"]:
            md["xesam:trackNumber"] = GLib.Variant("i", t["track"])
        if self.art_url:
            md["mpris:artUrl"] = GLib.Variant("s", self.art_url)
        return md

    def _props(self, iface):
        w = self.win
        if iface == MPRIS_ROOT:
            return {
                "CanQuit": GLib.Variant("b", True),
                "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False),
                "Identity": GLib.Variant("s", APP_NAME),
                "DesktopEntry": GLib.Variant("s", "player163"),
                "SupportedUriSchemes": GLib.Variant("as", ["file"]),
                "SupportedMimeTypes": GLib.Variant("as", ["audio/mpeg", "audio/flac", "audio/mp4",
                                                          "audio/ogg", "audio/x-wav", "audio/aac"]),
            }
        status = {"playing": "Playing", "paused": "Paused"}.get(w.engine.state, "Stopped")
        loop = {"off": "None", "all": "Playlist", "one": "Track"}[w.queue.repeat]
        return {
            "PlaybackStatus": GLib.Variant("s", status),
            "LoopStatus": GLib.Variant("s", loop),
            "Rate": GLib.Variant("d", 1.0),
            "Shuffle": GLib.Variant("b", w.queue.shuffle),
            "Metadata": GLib.Variant("a{sv}", self.metadata()),
            "Volume": GLib.Variant("d", w.vol.get_value()),
            "Position": GLib.Variant("x", int(w.engine.position() * 1e6)),
            "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "CanGoNext": GLib.Variant("b", True),
            "CanGoPrevious": GLib.Variant("b", True),
            "CanPlay": GLib.Variant("b", True),
            "CanPause": GLib.Variant("b", True),
            "CanSeek": GLib.Variant("b", True),
            "CanControl": GLib.Variant("b", True),
        }

    def _on_call(self, conn, sender, path, iface, method, params, invocation):
        w = self.win
        try:
            invocation.return_value(None)
            if iface == MPRIS_ROOT:
                if method == "Raise":
                    w.raise_ui()
                elif method == "Quit":
                    GLib.idle_add(w.app.quit)
                return
            if method == "Next":
                w.next_track()
            elif method == "Previous":
                w.prev_track()
            elif method == "Pause":
                w.pause()
            elif method == "PlayPause":
                w.play_pause()
            elif method == "Stop":
                w.stop()
            elif method == "Play":
                w.play()
            elif method == "Seek":
                w.seek_to(w.engine.position() + params.unpack()[0] / 1e6)
            elif method == "SetPosition":
                tid, pos = params.unpack()
                if tid == self.track_id(w.current_path):
                    w.seek_to(pos / 1e6)
            elif method == "OpenUri":
                uri = params.unpack()[0]
                p = Gio.File.new_for_uri(uri).get_path() if uri.startswith("file://") else None
                if p:
                    w.open_paths([p])
        except Exception as e:
            print("player163: MPRIS call failed:", e, file=sys.stderr)

    def _on_get(self, conn, sender, path, iface, prop):
        return self._props(iface).get(prop)

    def _on_set(self, conn, sender, path, iface, prop, value):
        w = self.win
        v = value.unpack()
        if prop == "LoopStatus":
            w.set_repeat({"None": "off", "Playlist": "all", "Track": "one"}.get(v, "off"))
        elif prop == "Shuffle":
            w.shuffle_btn.set_active(bool(v))
        elif prop == "Volume":
            w.vol.set_value(max(0.0, min(1.0, v)))
        return True

    def changed(self, *names):
        if not self.conn:
            return
        try:
            props = self._props(MPRIS_PLAYER)
            body = GLib.Variant("(sa{sv}as)", (MPRIS_PLAYER, {n: props[n] for n in names}, []))
            self.conn.emit_signal(None, MPRIS_PATH, "org.freedesktop.DBus.Properties",
                                  "PropertiesChanged", body)
        except Exception as e:
            print("player163: MPRIS signal failed:", e, file=sys.stderr)

    def seeked(self, sec):
        if self.conn:
            try:
                self.conn.emit_signal(None, MPRIS_PATH, MPRIS_PLAYER, "Seeked",
                                      GLib.Variant("(x)", (int(sec * 1e6),)))
            except Exception:
                pass


# ----------------------------------------------------------------------------------------
# Dialogs
# ----------------------------------------------------------------------------------------


def _completion(values):
    store = Gtk.ListStore(str)
    for v in values:
        store.append([v])
    c = Gtk.EntryCompletion(model=store)
    c.set_text_column(0)
    c.set_minimum_key_length(1)
    c.set_popup_set_width(True)
    return c


class TagDialog(Gtk.Dialog):
    SPEC = (("title", "Title", "title"), ("artist", "Artist", "artist"),
            ("album", "Album", "album"), ("albumartist", "Album artist", "albumartist"),
            ("track", "Track #", "tracknumber"), ("disc", "Disc #", "discnumber"),
            ("year", "Year", "date"), ("genre", "Genre", "genre"))

    def __init__(self, parent, tracks, suggestions):
        multi = len(tracks) > 1
        Gtk.Dialog.__init__(self, title=("Edit %d Songs" % len(tracks)) if multi else "Edit Song",
                            transient_for=parent, modal=True, use_header_bar=True)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = self.add_button("Save", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        area = self.get_content_area()
        area.set_border_width(16)
        area.set_spacing(12)

        if multi:
            hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=50)
            hint.set_markup("Changes apply to all <b>%d</b> songs. Fields marked <i>Mixed</i> "
                            "keep each song's own value unless you type something." % len(tracks))
            area.pack_start(hint, False, False, 0)
        else:
            fl = Gtk.Label(label=tracks[0]["path"], xalign=0, selectable=True,
                           ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=60)
            fl.get_style_context().add_class("dim-label")
            area.pack_start(fl, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        area.pack_start(grid, True, True, 0)
        self.entries = {}
        row = 0
        for key, label, tagkey in self.SPEC:
            if multi and key in ("title", "track"):
                continue
            vals = {self._val(t, key) for t in tracks}
            e = Gtk.Entry(hexpand=True, activates_default=True, width_chars=38)
            if len(vals) == 1:
                init = vals.pop()
                e.set_text(init)
                if key == "title" and not init:
                    e.set_placeholder_text(tracks[0]["d_title"])
            else:
                init = ""
                e.set_placeholder_text("Mixed — leave empty to keep")
            if key in suggestions:
                e.set_completion(_completion(suggestions[key]))
            lab = Gtk.Label(label=label, xalign=1)
            lab.get_style_context().add_class("dim-label")
            grid.attach(lab, 0, row, 1, 1)
            grid.attach(e, 1, row, 1, 1)
            self.entries[key] = (e, init, tagkey)
            row += 1

        self.err = Gtk.Label(xalign=0)
        self.err.get_style_context().add_class("error")
        area.pack_start(self.err, False, False, 0)
        area.show_all()
        self.err.hide()

    @staticmethod
    def _val(t, key):
        if key in ("track", "disc"):
            return str(t[key]) if t[key] else ""
        return t[key] or ""

    def get_changes(self):
        out = {}
        for key, (e, init, tagkey) in self.entries.items():
            text = e.get_text().strip()
            if text != init:
                out[tagkey] = text
        return out

    def validate(self):
        for key in ("track", "disc"):
            if key in self.entries:
                text = self.entries[key][0].get_text().strip()
                if text and not re.match(r"^\d+(/\d+)?$", text):
                    self.err.set_text("%s must be a number, like 3 or 3/12" % ("Track #" if key == "track" else "Disc #"))
                    self.err.show()
                    return False
        return True


class EqualizerDialog(Gtk.Dialog):
    def __init__(self, main):
        Gtk.Dialog.__init__(self, title="Equalizer", transient_for=main, modal=False, use_header_bar=True)
        self.main = main
        self.set_resizable(False)
        self._setting = False
        hb = self.get_header_bar()
        self.switch = Gtk.Switch(active=bool(main.cfg.get("eq_on")), valign=Gtk.Align.CENTER)
        self.switch.set_tooltip_text("Equalizer on / off")
        self.switch.connect("notify::active", self._on_switch)
        hb.pack_start(self.switch)
        self.preset = Gtk.ComboBoxText()
        for name in list(EQ_PRESETS) + ["Custom"]:
            self.preset.append(name, name)
        self.preset.connect("changed", self._on_preset)
        hb.pack_end(self.preset)

        area = self.get_content_area()
        area.set_border_width(18)
        bands = Gtk.Box(spacing=10, homogeneous=True)
        area.pack_start(bands, True, True, 0)
        self.scales, self.vlabels = [], []
        for i, lab in enumerate(EQ_LABELS):
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            v = Gtk.Label(label="0")
            v.get_style_context().add_class("dim-label")
            sc = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL,
                           adjustment=Gtk.Adjustment(value=0, lower=-12, upper=12, step_increment=0.5, page_increment=2))
            sc.set_inverted(True)
            sc.set_draw_value(False)
            sc.set_size_request(-1, 190)
            sc.add_mark(0, Gtk.PositionType.LEFT, None)
            sc.connect("value-changed", self._on_band, i)
            f = Gtk.Label()
            f.set_markup("<small>%s</small>" % lab)
            col.pack_start(v, False, False, 0)
            col.pack_start(sc, True, True, 0)
            col.pack_start(f, False, False, 0)
            bands.pack_start(col, True, True, 0)
            self.scales.append(sc)
            self.vlabels.append(v)
        hint = Gtk.Label(xalign=0.5)
        hint.set_markup("<small>Bands in Hz · ±12 dB. Changes apply instantly.</small>")
        hint.get_style_context().add_class("dim-label")
        area.pack_start(hint, False, False, 10)
        self._set_values(main._eq_values())
        self._setting = True
        self.preset.set_active_id(main.cfg.get("eq_preset", "Flat") if main.cfg.get("eq_preset") in EQ_PRESETS else "Custom")
        self._setting = False
        self._sync_sensitive()
        self.connect("delete-event", lambda *a: (self.hide(), True)[1])

    def _set_values(self, vals):
        self._setting = True
        for sc, v in zip(self.scales, vals):
            sc.set_value(v)
        self._setting = False
        self._update_labels()

    def _update_labels(self):
        for sc, lab in zip(self.scales, self.vlabels):
            v = sc.get_value()
            lab.set_text(("+%g" % v) if v > 0 else ("%g" % v))

    def _values(self):
        return [round(sc.get_value(), 1) for sc in self.scales]

    def _sync_sensitive(self):
        on = self.switch.get_active()
        for sc in self.scales:
            sc.set_sensitive(on)
        self.preset.set_sensitive(on)

    def _on_switch(self, *a):
        self.main.set_eq_on(self.switch.get_active())
        self._sync_sensitive()

    def _on_band(self, sc, i):
        self._update_labels()
        if self._setting:
            return
        self._setting = True
        self.preset.set_active_id("Custom")
        self._setting = False
        self.main.set_eq_bands(self._values(), "Custom")

    def _on_preset(self, combo):
        if self._setting:
            return
        name = combo.get_active_id()
        if name in EQ_PRESETS:
            self._set_values(EQ_PRESETS[name])
            self.main.set_eq_bands(self._values(), name)


def ask_text(parent, title, message, initial, suggestions=(), ok_label="Rename"):
    d = Gtk.Dialog(title=title, transient_for=parent, modal=True, use_header_bar=True)
    d.add_button("Cancel", Gtk.ResponseType.CANCEL)
    ok = d.add_button(ok_label, Gtk.ResponseType.OK)
    ok.get_style_context().add_class("suggested-action")
    d.set_default_response(Gtk.ResponseType.OK)
    area = d.get_content_area()
    area.set_border_width(16)
    area.set_spacing(10)
    lab = Gtk.Label(xalign=0, wrap=True, max_width_chars=50)
    lab.set_markup(message)
    e = Gtk.Entry(text=initial, activates_default=True, width_chars=40)
    if suggestions:
        e.set_completion(_completion(suggestions))
    area.pack_start(lab, False, False, 0)
    area.pack_start(e, False, False, 0)
    area.show_all()
    r = d.run()
    text = e.get_text().strip()
    d.destroy()
    return text if r == Gtk.ResponseType.OK else None


# ----------------------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------------------

BRAND_YELLOW = "#FFC400"

PALETTES = {
    "dark": {"bg": "#0f0f0f", "surface": "#171717", "bar": "#0a0a0a", "border": "#262626",
             "hover": "#232323", "fg": "#f2f2f2", "dim": "#8c8c8c", "trough": "#2f2f2f",
             "scroll": "#3d3d3d", "accent": BRAND_YELLOW, "accent_hover": "#ffd23f",
             "accent_fg": "#111111", "accent_text": BRAND_YELLOW},
    "light": {"bg": "#ffffff", "surface": "#f5f5f2", "bar": "#fafaf8", "border": "#e2e2dd",
              "hover": "#ececE8", "fg": "#141414", "dim": "#6b6b6b", "trough": "#dadad5",
              "scroll": "#c2c2bd", "accent": BRAND_YELLOW, "accent_hover": "#ffd23f",
              "accent_fg": "#111111", "accent_text": "#946d00"},
}

CSS_RULES = """
window, window.background, dialog, .background { background-color: @p_bg; color: @p_fg; }
headerbar, .titlebar, headerbar:backdrop, .titlebar:backdrop {
  background-color: @p_bar; background-image: none; border-color: @p_border; box-shadow: none; color: @p_fg; }
label { text-shadow: none; }

button { background-color: @p_surface; background-image: none; border: 1px solid @p_border;
  box-shadow: none; text-shadow: none; -gtk-icon-shadow: none; color: @p_fg; }
button:hover { background-color: @p_hover; }
button:active, button:checked { background-color: @p_accent; border-color: @p_accent; color: @p_accent_fg; }
button:disabled { color: @p_dim; background-color: transparent; }
button.flat, button.flat:backdrop { background-color: transparent; border-color: transparent; }
button.flat:hover { background-color: @p_hover; }
button.suggested-action { background-color: @p_accent; border-color: @p_accent; color: @p_accent_fg; font-weight: bold; }
button.suggested-action:hover { background-color: @p_accent_hover; }

.mode-switch button { padding: 4px 16px; font-weight: bold; }

.brand { background-color: #0b0b0b; border-radius: 999px; padding: 3px 14px 3px 8px; margin-right: 6px; }
.brand label { color: #ffffff; font-weight: 800; font-size: 1.08em; }

entry { background-color: @p_surface; background-image: none; color: @p_fg; border: 1px solid @p_border;
  box-shadow: none; caret-color: @p_accent_text; }
entry:focus { border-color: @p_accent; box-shadow: inset 0 0 0 1px @p_accent; }
entry image { color: @p_dim; }
entry selection, label selection { background-color: @p_accent; color: @p_accent_fg; }

treeview.view { background-color: @p_bg; color: @p_fg; }
treeview.view:selected, treeview.view:selected:focus, treeview.view:selected:backdrop {
  background-color: @p_accent; color: @p_accent_fg; }
treeview.view header button { background-color: @p_bg; border: none; border-bottom: 1px solid @p_border;
  color: @p_dim; font-weight: bold; padding: 4px 6px; }
treeview.view header button:hover { background-color: @p_hover; color: @p_fg; }
treeview.view header button:active { background-color: @p_hover; color: @p_fg; }
.sidebar-list, .sidebar-list.view { background-color: @p_surface; }

scrollbar, scrollbar trough { background-color: transparent; background-image: none; border: none; }
scrollbar slider { background-color: @p_scroll; border: none; border-radius: 999px; min-width: 6px; min-height: 6px; }
scrollbar slider:hover { background-color: @p_dim; }

scale trough { background-color: @p_trough; background-image: none; border: none; border-radius: 999px; min-height: 4px; min-width: 4px; }
scale highlight { background-color: @p_accent; background-image: none; border: none; border-radius: 999px; }
scale slider { background-color: @p_accent; background-image: none; border: 2px solid @p_bar; border-radius: 999px;
  box-shadow: none; min-width: 12px; min-height: 12px; margin: -7px; }
scale slider:hover { background-color: @p_accent_hover; }
scale:disabled highlight, scale:disabled slider { background-color: @p_trough; }

paned > separator, separator { background-color: @p_border; background-image: none; }

popover, popover.background { background-color: @p_surface; color: @p_fg; border-color: @p_border; }
popover modelbutton { color: @p_fg; }
popover modelbutton:hover { background-color: @p_accent; color: @p_accent_fg; }
menu, .menu, .context-menu { background-color: @p_surface; color: @p_fg; border: 1px solid @p_border; }
menu menuitem { color: @p_fg; }
menu menuitem:hover { background-color: @p_accent; color: @p_accent_fg; }
tooltip, tooltip.background { background-color: #111111; color: #f2f2f2; border: 1px solid #333333; }
tooltip label { color: #f2f2f2; }
infobar > revealer > box { background-color: @p_surface; color: @p_fg; border-bottom: 2px solid @p_accent; padding: 6px 10px; }
infobar label, infobar:backdrop label { color: @p_fg; }
infobar button:not(.suggested-action), infobar:backdrop button:not(.suggested-action) { background-color: @p_hover; color: @p_fg; border-color: @p_border; }
spinner { color: @p_accent_text; }

.player-bar { background-color: @p_bar; padding: 6px 12px; }
.player-bar button.flat { color: @p_fg; }
.player-bar button.flat:checked { background-color: transparent; color: @p_accent_text; }
.player-bar button.flat:checked:hover { background-color: @p_hover; }
.player-bar button.play-button { background-color: @p_accent; border-color: @p_accent; color: @p_accent_fg;
  border-radius: 999px; min-width: 40px; min-height: 40px; padding: 0; margin: 0 4px; }
.player-bar button.play-button:hover { background-color: @p_accent_hover; }
.np-title { font-weight: bold; }
window.mini-window-transparent, window.mini-window-transparent.background { background-color: transparent; }
.player-bar.mini { padding: 8px 8px 8px 8px; border: 1px solid @p_border; border-radius: 16px; }
.player-bar.mini button.flat { padding: 2px 5px; min-height: 24px; min-width: 24px; }
.player-bar.mini button.play-button { min-width: 34px; min-height: 34px; padding: 0; margin: 0 2px; }
.player-bar.mini scale { padding-top: 2px; padding-bottom: 2px; }
.mini-time { font-size: 0.8em; }
.player-bar.player-side { padding: 22px 18px; }
.player-side .np-title { font-size: 1.15em; }
.speed-button { font-weight: bold; min-width: 40px; }
.upnext-panel { background-color: @p_surface; border-left: 1px solid @p_border; }
list.settings-list { background-color: @p_surface; border: 1px solid @p_border; border-radius: 10px; }
list.settings-list row { background-color: transparent; }
list.settings-list row label { color: @p_fg; }
.status-line { background-color: @p_bar; font-size: 0.9em; padding: 3px 10px; }
.empty-title { font-size: 1.5em; font-weight: bold; }
.big-wordmark { font-size: 2.2em; font-weight: 800; }

/* Rounded everywhere, whatever the system theme does */
button { border-radius: 8px; }
button.image-button, button.titlebutton, headerbar button.titlebutton,
.player-bar button.flat, .player-bar button.play-button, .view-toggle button { border-radius: 999px; }
headerbar button.image-button, headerbar button.titlebutton { min-width: 30px; min-height: 30px; padding: 3px; }
.mode-switch button { border-radius: 999px; padding: 5px 16px; }
entry.search, headerbar entry { border-radius: 999px; padding-left: 10px; padding-right: 10px; }
treeview.view header button { border-radius: 0; }
popover, popover.background { border-radius: 12px; }
menu, .menu, .context-menu { border-radius: 10px; }
/* Switches drawn entirely by player163: no theme images (Mint-Y's on/off glyphs), no theme accent color */
switch { background-image: none; background-color: @p_border; border: 1px solid @p_border; border-radius: 999px;
         box-shadow: none; outline: none; color: transparent; font-size: 0; min-width: 44px; min-height: 24px;
         padding: 0; }
switch:checked { background-image: none; background-color: @p_accent; border-color: @p_accent; }
switch:disabled { opacity: 0.5; }
switch image { -gtk-icon-source: none; color: transparent; opacity: 0; }
switch slider { background-image: none; background-color: #ffffff; border: 1px solid rgba(0,0,0,0.15);
                border-radius: 999px; box-shadow: 0 1px 2px rgba(0,0,0,0.35); min-width: 22px; min-height: 22px;
                margin: 0; outline: none; }
switch:checked slider { border-color: rgba(0,0,0,0.25); }
iconview { background-color: @p_bg; color: @p_fg; }
iconview:selected, iconview:selected:focus { background-color: @p_accent; color: @p_accent_fg; border-radius: 12px; }
"""


def build_css(scheme):
    pal = PALETTES.get(scheme, PALETTES["dark"])
    defs = "".join("@define-color p_%s %s;\n" % (k, v) for k, v in pal.items())
    return (defs + CSS_RULES).encode("utf-8")


def wordmark_markup(size_pct=None):
    open_ = "<span size='%s'>" % size_pct if size_pct else "<span>"
    return open_ + "player<span foreground='%s'>163</span></span>" % BRAND_YELLOW


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def rounded_pixbuf(pb, radius):
    """Album art with rounded corners (falls back to the square image)."""
    if cairo is None or pb is None:
        return pb
    try:
        w, h = pb.get_width(), pb.get_height()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        _rounded_rect(cr, 0, 0, w, h, min(radius, w / 2.0, h / 2.0))
        cr.clip()
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()
        return Gdk.pixbuf_get_from_surface(surf, 0, 0, w, h)
    except Exception:
        return pb


def draw_logo(cr, size, background=True, color=BRAND_YELLOW, bg_color="#0b0b0b"):
    """The player163 mark: play triangle between two arcs (128-unit design grid)."""
    cr.save()
    if background:
        cr.scale(size / 128.0, size / 128.0)
        _rounded_rect(cr, 8, 8, 112, 112, 28)
        cr.set_source_rgb(*_hex_rgb(bg_color))
        cr.fill()
    else:  # glyph only, filling the whole square
        cr.scale(size / 92.0, size / 92.0)
        cr.translate(-18, -17.5)
    cr.set_source_rgb(*_hex_rgb(color))
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_width(7.5)
    cr.new_path()
    cr.arc(64, 63.5, 38, math.radians(-88), math.radians(-5))
    cr.stroke()
    cr.new_path()
    cr.arc(64, 63.5, 38, math.radians(97), math.radians(175))
    cr.stroke()
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.set_line_width(6)
    cr.move_to(49, 45.5)
    cr.line_to(82, 64.5)
    cr.line_to(49, 83.5)
    cr.close_path()
    cr.fill_preserve()
    cr.stroke()
    cr.restore()


def _thumb_path(cr):
    """Thumbs-up on a 16-unit grid."""
    _rounded_rect(cr, 1.3, 7.0, 3.0, 7.6, 0.8)
    cr.move_to(6.0, 7.4)
    cr.line_to(8.5, 2.5)
    cr.curve_to(8.9, 1.7, 10.3, 1.7, 10.6, 2.7)
    cr.curve_to(10.9, 3.6, 10.6, 4.8, 10.1, 6.0)
    cr.line_to(13.3, 6.0)
    cr.curve_to(14.4, 6.0, 15.1, 6.9, 14.9, 7.9)
    cr.line_to(13.8, 13.4)
    cr.curve_to(13.6, 14.2, 13.0, 14.6, 12.2, 14.6)
    cr.line_to(6.0, 14.6)
    cr.close_path()


def queue_surface(size, color, scale=1):
    """'Up Next' icon: three lines with a small play triangle."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size * scale, size * scale)
    surf.set_device_scale(scale, scale)
    cr = cairo.Context(surf)
    cr.scale(size / 16.0, size / 16.0)
    cr.set_source_rgb(*_hex_rgb(color))
    cr.set_line_width(1.6)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for y, x0 in ((3.5, 7.0), (8.0, 1.5), (12.5, 1.5)):
        cr.move_to(x0, y)
        cr.line_to(14.5, y)
    cr.stroke()
    cr.move_to(1.5, 1.2)
    cr.line_to(5.2, 3.5)
    cr.line_to(1.5, 5.8)
    cr.close_path()
    cr.fill()
    return surf


def thumb_surface(size, filled, color, alpha=1.0, scale=1):
    if cairo is None:
        return None
    try:
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size * scale, size * scale)
        surf.set_device_scale(scale, scale)
        cr = cairo.Context(surf)
        cr.scale(size / 16.0, size / 16.0)
        r, g, b = _hex_rgb(color)
        cr.set_source_rgba(r, g, b, alpha)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        if filled:
            _thumb_path(cr)
            cr.fill()
        else:
            cr.set_line_width(1.3)
            cr.translate(0.15, 0.1)
            cr.scale(0.97, 0.97)
            _thumb_path(cr)
            cr.stroke()
        return surf
    except Exception:
        return None


def placeholder_surface(size, bg, fg, scale=1):
    """Rounded square with a faint player163 mark, for albums without artwork."""
    if cairo is None:
        return None
    try:
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size * scale, size * scale)
        surf.set_device_scale(scale, scale)
        cr = cairo.Context(surf)
        _rounded_rect(cr, 0, 0, size, size, 5)
        cr.set_source_rgb(*_hex_rgb(bg))
        cr.fill()
        cr.translate(size * 0.27, size * 0.27)
        draw_logo(cr, size * 0.46, background=False, color=fg)
        return surf
    except Exception:
        return None


class ThumbLoader:
    """Album-art thumbnails, made in a background thread and cached on disk."""

    def __init__(self, size, scale, on_ready):
        self.size, self.scale, self.on_ready = size, scale, on_ready
        self.q = queue.Queue()
        self.pending = set()  # touched only from the main thread
        threading.Thread(target=self._run, daemon=True).start()

    def request(self, key, path):
        if key not in self.pending:
            self.pending.add(key)
            self.q.put((key, path))

    def _cache_base(self, key, path):
        try:
            tm = os.stat(path).st_mtime
            dm = os.stat(os.path.dirname(path)).st_mtime  # changes when a cover.jpg is added
        except OSError:
            tm = dm = 0
        raw = "%s|%s|%s|%s|%s|%d" % (key, path, tm, dm, stored_cover_stamp(key), self.size * self.scale)
        return os.path.join(THUMB_DIR, hashlib.md5(raw.encode("utf-8", "surrogateescape"),
                                                   usedforsecurity=False).hexdigest())

    def _run(self):
        while True:
            key, path = self.q.get()
            pb = None
            try:
                base = self._cache_base(key, path)
                if os.path.exists(base + ".png"):
                    pb = GdkPixbuf.Pixbuf.new_from_file(base + ".png")
                elif not os.path.exists(base + ".none"):
                    data = find_cover(path, key)
                    pb = pixbuf_from_bytes(data, self.size * self.scale) if data else None
                    os.makedirs(THUMB_DIR, exist_ok=True)
                    if pb is not None:
                        pb = rounded_pixbuf(pb, max(3, self.size // 10) * self.scale)
                        pb.savev(base + ".png", "png", [], [])
                    else:
                        open(base + ".none", "w").close()
            except Exception:
                pb = None
            GLib.idle_add(self.on_ready, key, path, pb)


def logo_pixbuf(size, background=True, color=BRAND_YELLOW, bg_color="#0b0b0b"):
    if cairo is None:
        return None
    try:
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        draw_logo(cairo.Context(surf), size, background, color, bg_color)
        return Gdk.pixbuf_get_from_surface(surf, 0, 0, size, size)
    except Exception:
        return None

class MiniPlayer(Gtk.ApplicationWindow):
    """Super compact player: art, now playing, controls, shuffle/repeat, volume, back to library."""
    ART = 96

    def __init__(self, main):
        Gtk.ApplicationWindow.__init__(self, application=main.app, title=APP_NAME)
        self.main = main
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_icon_name("player163")
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():  # rounded corners
            self.set_visual(visual)
            self.set_app_paintable(True)
            self.get_style_context().add_class("mini-window-transparent")
        self.set_keep_above(bool(main.cfg.get("mini_on_top", False)))

        root = Gtk.EventBox()
        root.connect("button-press-event", self._on_press)
        self.add(root)
        box = Gtk.Box(spacing=10)
        box.get_style_context().add_class("player-bar")
        box.get_style_context().add_class("mini")
        root.add(box)

        self.art = Gtk.Image()
        self.art.set_size_request(self.ART, self.ART)
        self.art.set_valign(Gtk.Align.CENTER)
        box.pack_start(self.art, False, False, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        box.pack_start(right, True, True, 0)

        top = Gtk.Box(spacing=2)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(label="Nothing playing", xalign=0, ellipsize=Pango.EllipsizeMode.END,
                               width_chars=22, max_width_chars=22)
        self.title.get_style_context().add_class("np-title")
        self.sub = Gtk.Label(label="", xalign=0, ellipsize=Pango.EllipsizeMode.END,
                             width_chars=22, max_width_chars=22)
        self.sub.get_style_context().add_class("dim-label")
        info.pack_start(self.title, False, False, 0)
        info.pack_start(self.sub, False, False, 0)
        top.pack_start(info, True, True, 0)
        expand = main._icon_button("view-fullscreen-symbolic", "Back to library  (Ctrl+M)",
                                   lambda *_: main.set_compact(False))
        close = main._icon_button("window-close-symbolic", "Quit  (Ctrl+Q)", lambda *_: main.app.quit_app())
        self.fav_btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE, focus_on_click=False, valign=Gtk.Align.START)
        self.fav_btn.set_image(Gtk.Image())
        self.fav_btn.connect("clicked", lambda *_: main.toggle_fav_current())
        top.pack_start(self.fav_btn, False, False, 0)
        for b in (expand, close):
            b.set_valign(Gtk.Align.START)
            top.pack_start(b, False, False, 0)
        right.pack_start(top, False, False, 0)

        ctl = Gtk.Box(spacing=1)
        self.prev_btn = main._icon_button("media-skip-backward-symbolic", "Previous", lambda *_: main.prev_track())
        self.play_btn = main._icon_button("media-playback-start-symbolic", "Play / Pause  (Space)",
                                          lambda *_: main.play_pause())
        self.play_btn.get_style_context().add_class("play-button")
        self.next_btn = main._icon_button("media-skip-forward-symbolic", "Next", lambda *_: main.next_track())
        for b in (self.prev_btn, self.play_btn, self.next_btn):
            ctl.pack_start(b, False, False, 0)
        ctl.pack_start(Gtk.Box(), True, True, 0)
        self.shuffle_btn = main._icon_button("media-playlist-shuffle-symbolic", "Shuffle", self._on_shuffle, toggle=True)
        self.repeat_btn = main._icon_button("media-playlist-repeat-symbolic", "Repeat", self._on_repeat, toggle=True)
        self.mute_btn = main._icon_button("audio-volume-high-symbolic", "Mute", lambda *_: main.toggle_mute())
        self.vol = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=main.vol.get_adjustment())
        self.vol.set_draw_value(False)
        self.vol.set_size_request(72, -1)
        self.vol.set_valign(Gtk.Align.CENTER)
        self.vol.set_tooltip_text("Volume")
        for b in (self.shuffle_btn, self.repeat_btn, self.mute_btn):
            ctl.pack_start(b, False, False, 0)
        ctl.pack_start(self.vol, False, False, 0)
        right.pack_start(ctl, False, False, 0)

        seekrow = Gtk.Box(spacing=6)
        self.pos_label = Gtk.Label(label="0:00", width_chars=5, xalign=1)
        self.dur_label = Gtk.Label(label="0:00", width_chars=5, xalign=0)
        for lab in (self.pos_label, self.dur_label):
            lab.get_style_context().add_class("dim-label")
            lab.get_style_context().add_class("mini-time")
        self.seek = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=main.seek.get_adjustment())
        self.seek.set_draw_value(False)
        self.seek.set_hexpand(True)
        self.seek.connect("button-press-event", main._on_seek_press)
        self.seek.connect("button-release-event", main._on_seek_release)
        self.seek.connect("change-value", main._on_seek_change)
        seekrow.pack_start(self.pos_label, False, False, 0)
        seekrow.pack_start(self.seek, True, True, 0)
        seekrow.pack_start(self.dur_label, False, False, 0)
        right.pack_start(seekrow, False, False, 0)

        root.show_all()
        self.set_art(None)
        self.connect("delete-event", lambda *a: main.close_window(self))
        self.connect("key-press-event", self._on_key)

    # -- sync from the main window
    def set_track(self, t, data):
        if not t:
            self.title.set_text("Nothing playing")
            self.sub.set_text("")
            self.set_title(APP_NAME)
            self.set_art(None)
            return
        self.title.set_text(t["d_title"])
        self.title.set_tooltip_text(t["d_title"])
        sub = t["d_artist"] + ("  —  " + t["album"] if t["album"] else "")
        self.sub.set_text(sub)
        self.sub.set_tooltip_text(sub)
        self.dur_label.set_text(fmt_time(t["duration"]))
        self.set_title("%s – %s" % (t["d_title"], t["d_artist"]))
        self.set_art(data)

    def set_art(self, data):
        pb = pixbuf_from_bytes(data, self.ART) if data else None
        pb = rounded_pixbuf(pb, 10) if pb else logo_pixbuf(self.ART)
        if pb:
            self.art.set_from_pixbuf(pb)

    def sync_play(self, state):
        icon = "media-playback-pause-symbolic" if state == "playing" else "media-playback-start-symbolic"
        self.play_btn.get_image().set_from_icon_name(icon, Gtk.IconSize.BUTTON)
        self.seek.set_sensitive(state != "stopped")

    def sync_shuffle(self, on):
        self.shuffle_btn.handler_block_by_func(self._on_shuffle)
        self.shuffle_btn.set_active(on)
        self.shuffle_btn.handler_unblock_by_func(self._on_shuffle)

    def sync_repeat(self, mode, icon, tip):
        self.repeat_btn.handler_block_by_func(self._on_repeat)
        self.repeat_btn.set_active(mode != "off")
        self.repeat_btn.handler_unblock_by_func(self._on_repeat)
        self.repeat_btn.get_image().set_from_icon_name(icon, Gtk.IconSize.BUTTON)
        self.repeat_btn.set_tooltip_text(tip)

    def sync_volume_icon(self, icon, tip):
        self.mute_btn.get_image().set_from_icon_name(icon, Gtk.IconSize.BUTTON)
        self.mute_btn.set_tooltip_text(tip)

    # -- user actions
    def _on_shuffle(self, btn):
        self.main.shuffle_btn.set_active(btn.get_active())

    def _on_repeat(self, *_):
        self.main._on_repeat_clicked()

    def show_mini(self):
        x, y = self.main.cfg.get("mini_x"), self.main.cfg.get("mini_y")
        if x is not None and y is not None and on_screen(x, y):
            self.move(int(x), int(y))
        else:
            mx, my = self.main.get_position()
            self.move(mx + 40, my + 40)
        self.present()

    def remember_pos(self):
        if self.get_visible():
            x, y = self.get_position()
            self.main.cfg["mini_x"], self.main.cfg["mini_y"] = x, y

    def _on_press(self, w, ev):
        if ev.button == 1 and ev.type == Gdk.EventType._2BUTTON_PRESS:
            self.main.set_compact(False)
            return True
        if ev.button == 1 and ev.type == Gdk.EventType.BUTTON_PRESS:
            self.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
            return True
        if ev.button == 3:
            menu = Gtk.Menu()
            top = Gtk.CheckMenuItem(label="Keep on Top")
            top.set_active(bool(self.main.cfg.get("mini_on_top", False)))
            top.connect("toggled", self._on_top_toggled)
            menu.append(top)
            for label, cb in (("Back to Library", lambda: self.main.set_compact(False)),
                              ("Quit", self.main.app.quit_app)):
                it = Gtk.MenuItem(label=label)
                it.connect("activate", lambda _i, c=cb: c())
                menu.append(it)
            menu.show_all()
            menu.attach_to_widget(self, None)
            self._menu = menu
            menu.popup_at_pointer(ev)
            return True
        return False

    def _on_top_toggled(self, item):
        on = item.get_active()
        self.main.cfg["mini_on_top"] = on
        self.set_keep_above(on)

    def _on_key(self, w, ev):
        m = self.main
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        k = ev.keyval
        if k == Gdk.KEY_space or k in (Gdk.KEY_AudioPlay, Gdk.KEY_AudioPause):
            m.play_pause()
        elif k == Gdk.KEY_AudioNext or (ctrl and k == Gdk.KEY_Right):
            m.next_track()
        elif k == Gdk.KEY_AudioPrev or (ctrl and k == Gdk.KEY_Left):
            m.prev_track()
        elif ctrl and k == Gdk.KEY_Up:
            m.vol.set_value(min(1.0, m.vol.get_value() + 0.05))
        elif ctrl and k == Gdk.KEY_Down:
            m.vol.set_value(max(0.0, m.vol.get_value() - 0.05))
        elif ctrl and k in (Gdk.KEY_m, Gdk.KEY_M):
            m.set_compact(False)
        elif ctrl and k in (Gdk.KEY_d, Gdk.KEY_D):
            m.toggle_fav_current()
        elif ctrl and k in (Gdk.KEY_s, Gdk.KEY_S):
            m.shuffle_btn.set_active(not m.shuffle_btn.get_active())
        elif ctrl and k in (Gdk.KEY_r, Gdk.KEY_R):
            m._on_repeat_clicked()
        elif ctrl and k in (Gdk.KEY_q, Gdk.KEY_Q):
            m.app.quit_app()
        else:
            return False
        return True


SONG_COLS = (  # header, store column, width, expand, xalign, sort id
    ("#", 1, 44, False, 1.0, "track"),
    ("Title", 2, 170, True, 0.0, "title"),
    ("Artist", 3, 170, False, 0.0, "artist"),
    ("Album", 4, 190, False, 0.0, "album"),
    ("Year", 5, 56, False, 0.0, "year"),
    ("Time", 6, 60, False, 1.0, "time"),
)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        Gtk.ApplicationWindow.__init__(self, application=app, title=APP_NAME)
        self.app = app
        self.cfg = load_config()
        self.set_default_size(self.cfg.get("width", 1080), self.cfg.get("height", 680))
        if self.cfg.get("maximized"):
            self.maximize()
        if "x" in self.cfg and on_screen(self.cfg["x"], self.cfg["y"]):
            self.move(int(self.cfg["x"]), int(self.cfg["y"]))
        self.set_icon_name("player163")

        self.library = Library(os.path.join(DATA_DIR, "library.db"))
        self.queue = PlayQueue()
        self.queue.shuffle = bool(self.cfg.get("shuffle", False))
        self.queue.repeat = self.cfg.get("repeat", "off") if self.cfg.get("repeat") in ("off", "all", "one") else "off"
        self.engine = Engine(self)
        self.engine.rg_mode = self.cfg.get("replaygain", "album") if self.cfg.get("replaygain") in ("off", "track", "album") else "album"
        self.engine.set_eq(self._eq_values() if self.cfg.get("eq_on") else [0] * 10)
        self.analyzer = LoudnessAnalyzer(self._loudness_ready)

        self.current_path = None
        self.current_duration = 0.0
        self.pending = None          # (queue pos, path) of the file the engine is about to start
        self.extra = {}              # tracks opened from outside the library folder
        self.mode = self.cfg.get("mode", "songs") if self.cfg.get("mode") in ("songs", "artists", "albums", "favorites", "playlists") else "songs"
        self.group_sel = self.cfg.get("sel_" + self.mode) if self.mode in ("artists", "albums", "playlists") else None
        self._list_order = None      # playlist / smart-list order: path -> rank
        self.album_thumbs = {}       # album key -> (track path, cairo surface or None)
        self.sort_col = None
        self.sort_desc = False
        self.visible_paths = []
        self.row_index = {}
        self._scan_gen = 0
        self._scan_refresh_id = 0
        self._seeking = False
        self._errors = 0
        self._building_groups = False
        self._menu = None
        self._art_file = None
        self._resume = None
        self._writing = False
        self._ready = False
        self._syncing_volume = False
        self._scan_new = set()
        self._scanning = False
        self._scan_quiet = False
        self._rescan_after = False
        self._monitors = {}
        self._autoscan_id = 0
        self._ignore_fs_until = 0.0
        self._scan_roots = []
        self._listened = 0.0
        self._counted = False
        self._resume_tick = 0.0
        self._manual_switch = False
        self.settings_dialog = None
        self._base_font = None
        self._notify_path = None
        self.small_thumbs = {}       # album key -> surface (False = no art)
        self.big_thumbs = {}
        self._album_rep = {}         # album key -> path of its first track
        self.album_layout = self.cfg.get("album_layout", "list") if self.cfg.get("album_layout") in ("list", "grid") else "list"
        self._album_gain = {}        # album key -> combined measured gain (dB)
        self._eq_dialog = None
        self._an_status_t = 0.0

        self._apply_css()
        self._main_pos = (int(self.cfg["x"]), int(self.cfg["y"])) if "x" in self.cfg and on_screen(self.cfg["x"], self.cfg["y"]) else None
        self._build_ui()
        self.mini = MiniPlayer(self)
        self.update_fav_buttons()
        self._install_actions()
        self.mpris = Mpris(self)

        self._muted = bool(self.cfg.get("muted", False))
        self.engine.set_mute(self._muted)
        self.vol.set_value(float(self.cfg.get("volume", 0.8)))
        self.engine.set_volume(self.vol.get_value())
        self._update_volume_icon()
        self.shuffle_btn.set_active(self.queue.shuffle)
        self._update_repeat_button()

        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", self._on_delete)
        for w in (self, self.grid_view, self.group_view, self.art_ev):
            w.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
            w.drag_dest_add_uri_targets()
            if w is not self:
                w.connect("drag-data-received", self._on_drop)
        self.connect("drag-data-received", self._on_drop)
        GLib.timeout_add(250, self._tick)

        self.get_titlebar().show_all()
        self.get_child().show_all()
        self.mode_buttons[self.mode].set_active(True)
        self._apply_mode_visibility()
        self.err_bar.hide()
        self.cover_bar.hide()
        self.undo_bar.hide()
        self.scan_spinner.hide()
        self._ready = True
        self.tray = TrayIcon(self)
        if self.cfg.pop("artist_photos", None) is not None or os.path.isdir(ARTIST_DIR):
            shutil.rmtree(ARTIST_DIR, ignore_errors=True)   # artist pictures were removed in 1.7.1
        if self.cfg.get("tray", True):
            self.tray.set_visible(True)
        if float(self.cfg.get("text_scale", 1.0)) != 1.0:
            self.apply_text_scale(float(self.cfg.get("text_scale", 1.0)))
        GLib.idle_add(lambda: self._maybe_ask_covers() and False)
        if self.cfg.get("upnext_open"):
            self.upnext_btn.set_active(True)

        root = self.folders()
        if not root and not self.library.tracks:
            if self._auto_music_folder():
                root = self.folders()
        if root:
            self.stack.set_visible_child_name("library")
            self.refresh_all()
            self.start_scan()
            self.analyzer.paused = self.engine.rg_mode == "off"
            last = self.cfg.get("last_path")
            if last and last in self.library.tracks:
                self._resume = (last, float(self.cfg.get("last_pos", 0)))
                self._show_now_playing(last)
                GLib.idle_add(self._scroll_to_path, last)
        else:
            self.stack.set_visible_child_name("empty")
        self._set_play_icon()

    # ---------------------------------------------------------------- UI construction
    def _apply_css(self):
        self._css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self._css,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.apply_theme(self.cfg.get("theme", "dark"))

    def apply_theme(self, scheme):
        scheme = scheme if scheme in PALETTES else "dark"
        self.scheme = scheme
        self.cfg["theme"] = scheme
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", scheme == "dark")
        self._css.load_from_data(build_css(scheme))
        self._render_thumbs()
        if hasattr(self, "fav_btn"):
            self.update_fav_buttons()
        if hasattr(self, "upnext_btn") and self._thumbs.get("queue") is not None:
            self.upnext_btn.get_image().set_from_surface(self._thumbs["queue"])
        if hasattr(self, "song_view"):
            self.song_view.queue_draw()

    def _icon_button(self, icon, tip, cb, toggle=False, size=Gtk.IconSize.BUTTON):
        b = Gtk.ToggleButton() if toggle else Gtk.Button()
        b.set_image(Gtk.Image.new_from_icon_name(icon, size))
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_tooltip_text(tip)
        b.set_focus_on_click(False)
        b.set_valign(Gtk.Align.CENTER)
        b.connect("toggled" if toggle else "clicked", cb)
        return b

    def _build_ui(self):
        # Header bar --------------------------------------------------------
        hb = Gtk.HeaderBar(show_close_button=True)
        self.set_titlebar(hb)
        switch = Gtk.Box(spacing=8)
        switch.get_style_context().add_class("mode-switch")
        self.mode_buttons = {}
        group = None
        for key, label in (("songs", "Songs"), ("artists", "Artists"), ("albums", "Albums"),
                           ("favorites", "Favorites"), ("playlists", "Playlists")):
            b = Gtk.RadioButton.new_with_label_from_widget(group, label)
            b.set_mode(False)
            b.set_focus_on_click(False)
            group = group or b
            b.connect("toggled", self._on_mode_toggled, key)
            switch.pack_start(b, False, False, 0)
            self.mode_buttons[key] = b
        hb.set_custom_title(switch)

        folder_btn = Gtk.Button.new_from_icon_name("folder-music-symbolic", Gtk.IconSize.BUTTON)
        if not Gtk.IconTheme.get_default().has_icon("folder-music-symbolic"):
            folder_btn.set_image(Gtk.Image.new_from_icon_name("folder-open-symbolic", Gtk.IconSize.BUTTON))
        folder_btn.set_tooltip_text("Choose music folder  (Ctrl+O)")
        folder_btn.connect("clicked", lambda *_: self.choose_folder())
        brand = Gtk.Box(spacing=7, valign=Gtk.Align.CENTER)
        brand.get_style_context().add_class("brand")
        glyph = logo_pixbuf(18, background=False)
        if glyph:
            brand.pack_start(Gtk.Image.new_from_pixbuf(glyph), False, False, 0)
        wm = Gtk.Label()
        wm.set_markup(wordmark_markup())
        brand.pack_start(wm, False, False, 0)
        hb.pack_start(brand)
        hb.pack_start(folder_btn)
        rescan = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        rescan.set_tooltip_text("Rescan folder for new or changed files  (F5)")
        rescan.connect("clicked", lambda *_: self.start_scan())
        hb.pack_start(rescan)
        self.layout_box = Gtk.Box(spacing=4)
        self.layout_box.get_style_context().add_class("view-toggle")
        self.layout_btns = {}
        lgroup = None
        for key, icon, tip in (("grid", "view-grid-symbolic", "Album grid"), ("list", "view-list-symbolic", "Album list")):
            b = Gtk.RadioButton.new_from_widget(lgroup)
            lgroup = lgroup or b
            b.set_mode(False)
            b.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
            b.get_style_context().add_class("image-button")
            b.set_tooltip_text(tip)
            b.set_focus_on_click(False)
            b.set_active(key == self.album_layout)
            b.connect("toggled", self._on_layout_toggled, key)
            self.layout_box.pack_start(b, False, False, 0)
            self.layout_btns[key] = b
        hb.pack_start(self.layout_box)

        menu = Gio.Menu()
        sec1 = Gio.Menu()
        sec1.append("Add Music Folder…", "win.open-folder")
        sec1.append("Rescan Folder", "win.rescan")
        sec1.append("Jump to Playing Song", "win.jump")
        sec1.append("Mini Player", "win.mini")
        sec1.append("Clear Up Next", "win.clear-queue")
        sec1.append("Export Favorites as Playlist…", "win.export-favs")
        sec1.append("Undo Last Tag Change", "win.undo")
        menu.append_section(None, sec1)
        sec_sound = Gio.Menu()
        lev = Gio.Menu()
        lev.append("Off", "win.replaygain::off")
        lev.append("Per Song", "win.replaygain::track")
        lev.append("Per Album", "win.replaygain::album")
        sec_sound.append_submenu("Volume Leveling", lev)
        sec_sound.append("Equalizer…", "win.equalizer")
        menu.append_section(None, sec_sound)
        sec2 = Gio.Menu()
        sec_theme = Gio.Menu()
        sec_theme.append("Dark Theme", "win.theme::dark")
        sec_theme.append("Light Theme", "win.theme::light")
        sec_theme.append("Album Covers from Cover Art Archive", "win.album-covers")
        menu.append_section(None, sec_theme)
        sec2.append("Preferences", "win.preferences")
        sec2.append("Keyboard Shortcuts", "win.shortcuts")
        sec2.append("About player163", "win.about")
        menu.append_section(None, sec2)
        mb = Gtk.MenuButton(menu_model=menu)
        mb.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        hb.pack_end(mb)

        self.search = Gtk.SearchEntry(placeholder_text="Search songs, artists, albums…", width_chars=26)
        self.search.connect("search-changed", self._on_search_changed)
        self.search.connect("stop-search", lambda e: e.set_text(""))
        hb.pack_end(self.search)

        # Body ------------------------------------------------------------------
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)

        self.err_bar = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING, show_close_button=True)
        self.err_label = Gtk.Label(xalign=0, wrap=True)
        self.err_bar.get_content_area().add(self.err_label)
        self.err_bar.connect("response", lambda b, r: b.hide())
        outer.pack_start(self.err_bar, False, False, 0)

        self.cover_bar = Gtk.InfoBar(message_type=Gtk.MessageType.QUESTION, show_close_button=False)
        cl = Gtk.Label(xalign=0, wrap=True)
        cl.set_markup("<b>Find missing album covers online?</b>  For albums without artwork, player163 can look "
                      "them up on MusicBrainz and download the cover from the Cover Art Archive. Your files aren't "
                      "changed. You can also drop an image on an album to set its cover.")
        self.cover_bar.get_content_area().add(cl)
        self.cover_bar.add_button("No Thanks", 0)
        cyes = self.cover_bar.add_button("Find Covers", 1)
        cyes.get_style_context().add_class("suggested-action")
        self.cover_bar.connect("response", lambda b, r: self.set_album_covers(r == 1))
        outer.pack_start(self.cover_bar, False, False, 0)

        self.undo_bar = Gtk.InfoBar(message_type=Gtk.MessageType.INFO, show_close_button=True)
        self.undo_label = Gtk.Label(xalign=0, wrap=True)
        self.undo_bar.get_content_area().add(self.undo_label)
        self.undo_bar.add_button("Undo", 1)
        self.undo_bar.connect("response", lambda b, r: self.undo_last() if r == 1 else b.hide())
        outer.pack_start(self.undo_bar, False, False, 0)
        self._undo = None
        self._undo_hide_id = 0
        self._forced_cover = None

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.body = Gtk.Box()
        outer.pack_start(self.body, True, True, 0)
        self.body.pack_start(self.stack, True, True, 0)

        # Empty state
        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        big = logo_pixbuf(120)
        img = Gtk.Image.new_from_pixbuf(big) if big else Gtk.Image.new_from_icon_name("player163", Gtk.IconSize.DIALOG)
        img.set_pixel_size(120)
        empty.pack_start(img, False, False, 0)
        bigwm = Gtk.Label()
        bigwm.set_markup(wordmark_markup())
        bigwm.get_style_context().add_class("big-wordmark")
        empty.pack_start(bigwm, False, False, 0)
        t1 = Gtk.Label(label="Where does your music live?")
        t1.get_style_context().add_class("empty-title")
        empty.pack_start(t1, False, False, 0)
        t2 = Gtk.Label(label="Pick a folder. player163 scans it, sorts it by artist and album,\n"
                             "and remembers it for next time.", justify=Gtk.Justification.CENTER)
        t2.get_style_context().add_class("dim-label")
        empty.pack_start(t2, False, False, 0)
        pick = Gtk.Button(label="Choose Music Folder…", halign=Gtk.Align.CENTER)
        pick.get_style_context().add_class("suggested-action")
        pick.connect("clicked", lambda *_: self.choose_folder())
        empty.pack_start(pick, False, False, 6)
        self.stack.add_named(empty, "empty")

        # Library page: sidebar + songs
        libpage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.stack.add_named(libpage, "library")
        self.lib_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        lib_row = Gtk.Box()
        libpage.pack_start(lib_row, True, True, 0)
        lib_row.pack_start(self.lib_stack, True, True, 0)
        lib_row.pack_end(self._build_upnext(), False, False, 0)
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.lib_stack.add_named(self.paned, "list")

        self.grid_store = Gtk.ListStore(str, str)  # album key, markup
        self.grid_view = Gtk.IconView(model=self.grid_store)
        self.grid_view.set_item_width(GRID_THUMB)
        self.grid_view.set_margin(16)
        self.grid_view.set_column_spacing(10)
        self.grid_view.set_row_spacing(14)
        self.grid_view.set_item_padding(8)
        self.grid_view.set_activate_on_single_click(True)
        gpix = Gtk.CellRendererPixbuf()
        gpix.set_fixed_size(GRID_THUMB, GRID_THUMB)
        self.grid_view.pack_start(gpix, False)
        self.grid_view.set_cell_data_func(gpix, self._cell_grid_thumb)
        gtxt = Gtk.CellRendererText(xalign=0.5, alignment=Pango.Alignment.CENTER,
                                    ellipsize=Pango.EllipsizeMode.END, wrap_width=GRID_THUMB)
        gtxt.set_fixed_size(GRID_THUMB, -1)
        self.grid_view.pack_start(gtxt, False)
        self.grid_view.add_attribute(gtxt, "markup", 1)
        self.grid_view.connect("item-activated", self._on_grid_activated)
        self.grid_view.connect("button-press-event", self._on_grid_button)
        gscroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        gscroll.add(self.grid_view)
        self.lib_stack.add_named(gscroll, "grid")

        self.group_store = Gtk.ListStore(str, str)  # key, markup
        self.group_view = Gtk.TreeView(model=self.group_store, headers_visible=False, enable_search=True)
        self.group_view.set_search_column(1)
        self.group_view.get_style_context().add_class("sidebar-list")
        self.group_view.set_search_equal_func(self._group_search_eq)
        gcell = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, ypad=4, xpad=6)
        gcol = Gtk.TreeViewColumn("")
        self.thumb_cell = Gtk.CellRendererPixbuf(xpad=4, ypad=3)
        self.thumb_cell.set_fixed_size(ALBUM_THUMB + 8, ALBUM_THUMB + 6)
        gcol.pack_start(self.thumb_cell, False)
        gcol.set_cell_data_func(self.thumb_cell, self._cell_group_thumb)
        gcol.pack_start(gcell, True)
        gcol.add_attribute(gcell, "markup", 1)
        self.group_view.append_column(gcol)
        sf = max(1, self.get_scale_factor())
        self._thumb_loader = ThumbLoader(ALBUM_THUMB, sf, self._thumb_ready)
        self._small_loader = ThumbLoader(SMALL_THUMB, sf, self._small_thumb_ready)
        self._big_loader = ThumbLoader(GRID_THUMB, sf, self._big_thumb_ready)
        self.group_view.get_selection().connect("changed", self._on_group_changed)
        self.group_view.connect("button-press-event", self._on_group_button)
        self.photo_finder = CoverFinder(self._cover_ready)
        self._cover_tried = set()
        self._lookup_retries = {}
        self._lookup_err_shown = set()
        self.group_view.connect("row-activated", lambda *a: self.play_visible(None))
        sscroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sscroll.add(self.group_view)
        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.sidebar.get_style_context().add_class("sidebar-box")
        self.sidebar.pack_start(sscroll, True, True, 0)
        self.newpl_btn = Gtk.Button(label="+  New Playlist", margin=8)
        self.newpl_btn.connect("clicked", lambda *_: self.new_playlist())
        self.sidebar.pack_start(self.newpl_btn, False, False, 0)
        self.sidebar.set_size_request(180, -1)
        self.paned.pack1(self.sidebar, False, False)
        self.paned.set_position(int(self.cfg.get("pane", 250)))

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.paned.pack2(right, True, False)
        # path, #, title, artist, album, year, time
        self.song_store = Gtk.ListStore(str, str, str, str, str, str, str)
        self.song_view = Gtk.TreeView(model=self.song_store, enable_search=False, fixed_height_mode=True)
        self.song_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        icell = Gtk.CellRendererPixbuf(xpad=4)
        icol = Gtk.TreeViewColumn("", icell)
        icol.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        icol.set_fixed_width(30)
        icol.set_cell_data_func(icell, self._cell_icon)
        qcell = Gtk.CellRendererText(xalign=0.5, scale=0.85, weight=700)
        icol.pack_start(qcell, True)
        icol.set_cell_data_func(qcell, self._cell_qnum)
        self.song_view.append_column(icol)
        self.sort_columns = {}
        for header, idx, width, expand, xalign, sid in SONG_COLS:
            if sid == "time":
                fcell = Gtk.CellRendererPixbuf(xpad=2)
                self.fav_col = Gtk.TreeViewColumn("", fcell)
                self.fav_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
                self.fav_col.set_fixed_width(30)
                self.fav_col.set_cell_data_func(fcell, self._cell_fav)
                self.song_view.append_column(self.fav_col)
            cell = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, xalign=xalign)
            if sid == "album":  # small cover beside the album name
                col = Gtk.TreeViewColumn(header)
                acell = Gtk.CellRendererPixbuf(xpad=0)
                acell.set_fixed_size(SMALL_THUMB + 6, SMALL_THUMB)
                col.pack_start(acell, False)
                col.set_cell_data_func(acell, self._cell_small_thumb)
                col.pack_start(cell, True)
                col.add_attribute(cell, "text", idx)
            else:
                col = Gtk.TreeViewColumn(header, cell, text=idx)
            col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            col.set_fixed_width(int(self.cfg.get("col_" + sid, width)))
            col.set_resizable(True)
            col.set_expand(expand)
            col.set_clickable(True)
            col.set_alignment(xalign)
            col.set_cell_data_func(cell, self._cell_weight)
            col.connect("clicked", self._on_header_clicked, sid)
            self.song_view.append_column(col)
            self.sort_columns[sid] = col
        self.song_view.connect("row-activated", self._on_song_activated)
        self.song_view.connect("button-press-event", self._on_song_button)
        self.song_scroll = Gtk.ScrolledWindow()
        self.song_scroll.add(self.song_view)
        right.pack_start(self.song_scroll, True, True, 0)

        status = Gtk.Box(spacing=6)
        status.get_style_context().add_class("status-line")
        self.scan_spinner = Gtk.Spinner()
        self.scan_spinner.set_size_request(12, 12)
        status.pack_start(self.scan_spinner, False, False, 0)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.get_style_context().add_class("dim-label")
        status.pack_start(self.status, True, True, 0)
        libpage.pack_start(status, False, False, 0)

        self.player_sep = Gtk.Separator()
        self.body.pack_start(self.player_sep, False, False, 0)
        self.body.pack_start(self._build_player_bar(), False, False, 0)
        self.apply_player_position(self.cfg.get("player_pos", "bottom"))

    def _build_player_bar(self):
        self.player_box = Gtk.Box()
        self.player_box.set_hexpand(False)   # the seek bar's hexpand must not widen the side panel
        self.player_box.get_style_context().add_class("player-bar")
        self._art_px = ART_SIZE
        self._cover_data = None

        self.art = Gtk.Image()
        self._set_default_art()
        self.art_ev = Gtk.EventBox()
        self.art_ev.add(self.art)
        self.art_ev.set_tooltip_text("Jump to playing song  (Ctrl+J)\nDrop an image here to set the album cover")
        self.art_ev.connect("button-press-event", lambda *a: self.jump_to_current())

        self.info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=2)
        self.np_title = Gtk.Label(label="Nothing playing", xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                  width_chars=26, max_width_chars=26)
        self.np_title.get_style_context().add_class("np-title")
        self.np_sub = Gtk.Label(label="", xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                width_chars=26, max_width_chars=26)
        self.np_sub.get_style_context().add_class("dim-label")
        self.info_box.pack_start(self.np_title, False, False, 0)
        self.info_box.pack_start(self.np_sub, False, False, 0)
        self.fav_btn = Gtk.Button(relief=Gtk.ReliefStyle.NONE, focus_on_click=False, valign=Gtk.Align.CENTER)
        self.fav_btn.set_image(Gtk.Image())
        self.fav_btn.connect("clicked", lambda *_: self.toggle_fav_current())

        self.prev_btn = self._icon_button("media-skip-backward-symbolic", "Previous  (Ctrl+←)", lambda *_: self.prev_track())
        self.play_btn = self._icon_button("media-playback-start-symbolic", "Play / Pause  (Space)",
                                          lambda *_: self.play_pause(), size=Gtk.IconSize.LARGE_TOOLBAR)
        self.play_btn.get_style_context().add_class("play-button")
        self.next_btn = self._icon_button("media-skip-forward-symbolic", "Next  (Ctrl+→)", lambda *_: self.next_track())

        self.pos_label = Gtk.Label(label="0:00", width_chars=5, xalign=1)
        self.pos_label.get_style_context().add_class("dim-label")
        self.seek = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                              adjustment=Gtk.Adjustment(value=0, lower=0, upper=1, step_increment=5, page_increment=15))
        self.seek.set_draw_value(False)
        self.seek.set_hexpand(True)
        self.seek.set_valign(Gtk.Align.CENTER)
        self.seek.connect("button-press-event", self._on_seek_press)
        self.seek.connect("button-release-event", self._on_seek_release)
        self.seek.connect("change-value", self._on_seek_change)
        self.dur_label = Gtk.Label(label="0:00", width_chars=5, xalign=0)
        self.dur_label.get_style_context().add_class("dim-label")

        self.shuffle_btn = self._icon_button("media-playlist-shuffle-symbolic", "Shuffle  (Ctrl+S)",
                                             self._on_shuffle_toggled, toggle=True)
        self.repeat_btn = self._icon_button("media-playlist-repeat-symbolic", "Repeat: off  (Ctrl+R)",
                                            self._on_repeat_clicked, toggle=True)
        self.speed_btn = Gtk.Button(label="1×", relief=Gtk.ReliefStyle.NONE, focus_on_click=False, valign=Gtk.Align.CENTER)
        self.speed_btn.get_style_context().add_class("speed-button")
        self.speed_btn.set_tooltip_text("Playback speed for long files (audiobooks, podcasts)")
        self.speed_btn.connect("clicked", lambda *_: self.cycle_speed())
        self.speed_btn.set_no_show_all(True)
        self.upnext_btn = Gtk.ToggleButton(relief=Gtk.ReliefStyle.NONE, focus_on_click=False, valign=Gtk.Align.CENTER)
        img = Gtk.Image()
        if self._thumbs.get("queue") is not None:
            img.set_from_surface(self._thumbs["queue"])
        else:
            img.set_from_icon_name("view-list-symbolic", Gtk.IconSize.BUTTON)
        self.upnext_btn.set_image(img)
        self.upnext_btn.set_tooltip_text("Up Next  (Ctrl+U)")
        self.upnext_btn.connect("toggled", lambda b: self.show_upnext(b.get_active()))

        self.mute_btn = self._icon_button("audio-volume-high-symbolic", "Mute", lambda *_: self.toggle_mute())
        self.vol = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                             adjustment=Gtk.Adjustment(value=0.8, lower=0, upper=1, step_increment=0.05, page_increment=0.1))
        self.vol.set_draw_value(False)
        self.vol.set_valign(Gtk.Align.CENTER)
        self.vol.set_tooltip_text("Volume  (Ctrl+↑ / Ctrl+↓)")
        self.vol.connect("value-changed", self._on_volume_changed)
        self.mini_btn = self._icon_button("view-restore-symbolic", "Mini player  (Ctrl+M)", lambda *_: self.set_compact(True))
        return self.player_box

    def _player_leaves(self):
        return [self.art_ev, self.info_box, self.fav_btn, self.prev_btn, self.play_btn, self.next_btn,
                self.pos_label, self.seek, self.dur_label, self.shuffle_btn, self.repeat_btn, self.speed_btn,
                self.upnext_btn, self.mute_btn, self.vol, self.mini_btn]

    def _arrange_player(self, side):
        pb = self.player_box
        for w in self._player_leaves():
            parent = w.get_parent()
            if parent is not None:
                parent.remove(w)
        for child in pb.get_children():
            pb.remove(child)
            child.destroy()
        ctx = pb.get_style_context()
        if side:
            ctx.add_class("player-side")
            pb.set_orientation(Gtk.Orientation.VERTICAL)
            pb.set_spacing(12)
            pb.set_size_request(SIDE_WIDTH, -1)
            self.art_ev.set_halign(Gtk.Align.CENTER)
            pb.pack_start(self.art_ev, False, False, 0)
            for lab in (self.np_title, self.np_sub):
                lab.set_xalign(0.5)
                lab.set_width_chars(-1)
                lab.set_max_width_chars(28)
            title_row = Gtk.Box(spacing=4, halign=Gtk.Align.CENTER)
            title_row.pack_start(self.info_box, False, False, 0)
            title_row.pack_start(self.fav_btn, False, False, 0)
            pb.pack_start(title_row, False, False, 0)
            seek_row = Gtk.Box(spacing=6)
            seek_row.pack_start(self.pos_label, False, False, 0)
            seek_row.pack_start(self.seek, True, True, 0)
            seek_row.pack_start(self.dur_label, False, False, 0)
            pb.pack_start(seek_row, False, False, 0)
            ctl = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
            for w in (self.prev_btn, self.play_btn, self.next_btn):
                ctl.pack_start(w, False, False, 0)
            pb.pack_start(ctl, False, False, 0)
            extras = Gtk.Box(spacing=4, halign=Gtk.Align.CENTER)
            for w in (self.shuffle_btn, self.repeat_btn, self.speed_btn, self.upnext_btn, self.mini_btn):
                extras.pack_start(w, False, False, 0)
            pb.pack_start(extras, False, False, 0)
            volrow = Gtk.Box(spacing=6)
            volrow.pack_start(self.mute_btn, False, False, 0)
            volrow.pack_start(self.vol, True, True, 0)
            self.vol.set_size_request(-1, -1)
            pb.pack_start(volrow, False, False, 0)
            self._art_px = SIDE_ART
        else:
            ctx.remove_class("player-side")
            pb.set_orientation(Gtk.Orientation.HORIZONTAL)
            pb.set_spacing(8)
            pb.set_size_request(-1, -1)
            self.art_ev.set_halign(Gtk.Align.FILL)
            for lab in (self.np_title, self.np_sub):
                lab.set_xalign(0)
                lab.set_width_chars(26)
                lab.set_max_width_chars(26)
            pb.pack_start(self.art_ev, False, False, 0)
            pb.pack_start(self.info_box, False, False, 4)
            for w in (self.fav_btn, self.prev_btn, self.play_btn, self.next_btn):
                pb.pack_start(w, False, False, 0)
            pb.pack_start(self.pos_label, False, False, 4)
            pb.pack_start(self.seek, True, True, 0)
            pb.pack_start(self.dur_label, False, False, 0)
            for w in (self.shuffle_btn, self.repeat_btn, self.speed_btn, self.upnext_btn, self.mute_btn):
                pb.pack_start(w, False, False, 0)
            self.vol.set_size_request(110, -1)
            pb.pack_start(self.vol, False, False, 0)
            pb.pack_start(self.mini_btn, False, False, 2)
            self._art_px = ART_SIZE
        self.art.set_size_request(self._art_px, self._art_px)
        pb.show_all()
        self._render_art()
        self._update_speed_btn()

    def apply_player_position(self, pos):
        pos = pos if pos in ("bottom", "left", "right") else "bottom"
        self.cfg["player_pos"] = pos
        side = pos != "bottom"
        self.body.set_orientation(Gtk.Orientation.HORIZONTAL if side else Gtk.Orientation.VERTICAL)
        self.player_sep.set_orientation(Gtk.Orientation.VERTICAL if side else Gtk.Orientation.HORIZONTAL)
        order = [self.player_box, self.player_sep, self.stack] if pos == "left" else [self.stack, self.player_sep, self.player_box]
        for i, w in enumerate(order):
            self.body.reorder_child(w, i)
            self.body.child_set_property(w, "expand", w is self.stack)
            self.body.child_set_property(w, "fill", True)
        self._arrange_player(side)

    def _render_art(self):
        data = self._cover_data
        pb = pixbuf_from_bytes(data, self._art_px) if data else None
        if pb:
            self.art.set_from_pixbuf(rounded_pixbuf(pb, 6 if self._art_px <= ART_SIZE else 16))
        else:
            self._set_default_art()

    # ---------------------------------------------------------------- up next panel
    def _build_upnext(self):
        rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_LEFT, transition_duration=180)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.get_style_context().add_class("upnext-panel")
        box.set_size_request(300, -1)
        rev.add(box)
        head = Gtk.Box(spacing=6, margin=10)
        t = Gtk.Label(xalign=0)
        t.set_markup("<b>Up Next</b>")
        head.pack_start(t, True, True, 0)
        clear = Gtk.Button(label="Clear", relief=Gtk.ReliefStyle.NONE)
        clear.set_tooltip_text("Clear the queued songs")
        clear.connect("clicked", lambda *_: self.clear_queue())
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.connect("clicked", lambda *_: self.upnext_btn.set_active(False))
        head.pack_start(clear, False, False, 0)
        head.pack_start(close, False, False, 0)
        box.pack_start(head, False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 0)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroll.add(inner)
        box.pack_start(scroll, True, True, 0)

        def section(text):
            lab = Gtk.Label(xalign=0, margin_start=12, margin_top=8)
            lab.set_markup("<small><b>%s</b></small>" % text)
            lab.get_style_context().add_class("dim-label")
            return lab
        inner.pack_start(section("QUEUED  ·  drag to reorder"), False, False, 0)
        self.q_store = Gtk.ListStore(str, str)
        self.q_view = Gtk.TreeView(model=self.q_store, headers_visible=False, reorderable=True)
        self.q_view.get_style_context().add_class("sidebar-list")
        qtxt = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, ypad=3, xpad=8)
        qcol = Gtk.TreeViewColumn("", qtxt, markup=1)
        qcol.set_expand(True)
        self.q_view.append_column(qcol)
        xcell = Gtk.CellRendererPixbuf(icon_name="window-close-symbolic", xpad=6)
        self.q_remove_col = Gtk.TreeViewColumn("", xcell)
        self.q_view.append_column(self.q_remove_col)
        self.q_view.connect("button-press-event", self._on_q_button)
        self.q_view.connect("row-activated", self._on_q_activated)
        self.q_store.connect("row-deleted", self._on_q_reordered)
        inner.pack_start(self.q_view, False, False, 0)
        self.q_empty = Gtk.Label(label="Nothing queued.\nRight-click songs → Play Next or Add to Queue.",
                                 justify=Gtk.Justification.CENTER, margin=12, wrap=True)
        self.q_empty.get_style_context().add_class("dim-label")
        inner.pack_start(self.q_empty, False, False, 0)
        inner.pack_start(section("THEN"), False, False, 0)
        self.later_store = Gtk.ListStore(int, str, str)
        self.later_view = Gtk.TreeView(model=self.later_store, headers_visible=False)
        self.later_view.get_style_context().add_class("sidebar-list")
        ltxt = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, ypad=3, xpad=8)
        self.later_view.append_column(Gtk.TreeViewColumn("", ltxt, markup=2))
        self.later_view.connect("row-activated", self._on_later_activated)
        inner.pack_start(self.later_view, False, False, 0)
        self.upnext_rev = rev
        self._q_rebuilding = False
        self._q_sync_id = 0
        return rev

    def _song_markup(self, path):
        t = self.get_track(path)
        if not t:
            return esc(os.path.basename(path))
        return "<b>%s</b>\n<small><span alpha='60%%'>%s</span></small>" % (esc(t["d_title"]), esc(t["d_artist"]))

    def show_upnext(self, on):
        self.upnext_rev.set_reveal_child(bool(on))
        self.cfg["upnext_open"] = bool(on)
        self.refresh_upnext()

    def refresh_upnext(self):
        if not hasattr(self, "upnext_rev") or not self.upnext_rev.get_reveal_child():
            return
        self._q_rebuilding = True
        self.q_store.clear()
        for p in list(self.queue.upnext):
            self.q_store.append([p, self._song_markup(p)])
        self._q_rebuilding = False
        has = bool(self.queue.upnext)
        self.q_view.set_visible(has)
        self.q_empty.set_visible(not has)
        self.later_store.clear()
        q = self.queue
        with q.lock:
            order, paths, pos, rep_all = list(q.order), list(q.paths), q.pos, q.repeat == "all"
        n = len(order)
        i, shown = pos, 0
        while n and shown < 25:
            i += 1
            if i >= n:
                if not rep_all:
                    break
                i = 0
            if i == pos:
                break
            self.later_store.append([i, paths[order[i]], self._song_markup(paths[order[i]])])
            shown += 1

    def _on_q_button(self, view, ev):
        if ev.type == Gdk.EventType.BUTTON_PRESS and ev.button == 1:
            hit = view.get_path_at_pos(int(ev.x), int(ev.y))
            if hit and hit[1] is self.q_remove_col:
                idx = hit[0].get_indices()[0]
                with self.queue.lock:
                    if 0 <= idx < len(self.queue.upnext):
                        self.queue.upnext.pop(idx)
                self._after_queue_change()
                return True
        return False

    def _on_q_activated(self, view, path, col):
        idx = path.get_indices()[0]
        with self.queue.lock:
            p = self.queue.upnext.pop(idx) if 0 <= idx < len(self.queue.upnext) else None
        if p:
            self._play(None, p)
        self._after_queue_change()

    def _on_later_activated(self, view, path, col):
        pos, p = self.later_store[path][0], self.later_store[path][1]
        self._play(pos, p)

    def _on_q_reordered(self, model, path):
        if self._q_rebuilding or self._q_sync_id:
            return

        def sync():
            self._q_sync_id = 0
            with self.queue.lock:
                self.queue.upnext = [r[0] for r in self.q_store]
            self._after_queue_change(rebuild=False)
            return False
        self._q_sync_id = GLib.idle_add(sync)

    def _after_queue_change(self, rebuild=True):
        if rebuild:
            self.refresh_upnext()
        self.song_view.queue_draw()
        self._update_status()

    # ---------------------------------------------------------------- long files (audiobooks, podcasts)
    def is_long(self, path):
        if not path or not self.cfg.get("resume_long", True):
            return False
        t = self.get_track(path)
        dur = (t["duration"] if t else 0) or (self.current_duration if path == self.current_path else 0)
        return dur >= float(self.cfg.get("long_minutes", 20)) * 60

    def _speed_for(self, path):
        return float(self.cfg.get("long_speed", 1.0)) if self.is_long(path) else 1.0

    def _update_speed_btn(self):
        if not hasattr(self, "speed_btn"):
            return
        show = bool(self.current_path) and self.is_long(self.current_path)
        self.speed_btn.set_visible(show)
        self.speed_btn.set_label("%g×" % float(self.cfg.get("long_speed", 1.0)))

    def cycle_speed(self):
        cur = float(self.cfg.get("long_speed", 1.0))
        nxt = SPEEDS[(SPEEDS.index(cur) + 1) % len(SPEEDS)] if cur in SPEEDS else 1.0
        self.set_long_speed(nxt)

    def set_long_speed(self, speed):
        self.cfg["long_speed"] = float(speed)
        self._update_speed_btn()
        if self.current_path and self.is_long(self.current_path) and self.engine.state != "stopped":
            self.engine.set_rate(float(speed))
        self.flash_status("Speed for long files: %g×" % float(speed))

    def _save_long_position(self, path=None):
        path = path or self.current_path
        if not path or not self.is_long(path) or self.engine.state == "stopped" or path not in self.library.tracks:
            return
        pos = self.engine.position()
        dur = self.engine.duration() or self.current_duration
        if dur and pos > dur - 30:
            self.library.clear_resume(path)  # finished
        elif pos > 5:
            self.library.set_resume(path, round(pos, 1))

    def _install_actions(self):
        def add(name, cb, accels=None):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            self.add_action(a)
            if accels:
                self.app.set_accels_for_action("win." + name, accels)

        add("open-folder", self.choose_folder, ["<Primary>o"])
        add("rescan", self.start_scan, ["F5"])
        add("search", lambda: self.search.grab_focus(), ["<Primary>f"])
        add("edit-tags", self.edit_selected, ["<Primary>e", "F2"])
        add("toggle-shuffle", lambda: self.shuffle_btn.set_active(not self.shuffle_btn.get_active()), ["<Primary>s"])
        add("cycle-repeat", self._on_repeat_clicked, ["<Primary>r"])
        add("jump", self.jump_to_current, ["<Primary>j"])
        add("mini", lambda: self.set_compact(True), ["<Primary>m"])
        add("mode-songs", lambda: self.mode_buttons["songs"].set_active(True), ["<Primary>1"])
        add("mode-artists", lambda: self.mode_buttons["artists"].set_active(True), ["<Primary>2"])
        add("mode-albums", lambda: self.mode_buttons["albums"].set_active(True), ["<Primary>3"])
        add("mode-favorites", lambda: self.mode_buttons["favorites"].set_active(True), ["<Primary>4"])
        add("mode-playlists", lambda: self.mode_buttons["playlists"].set_active(True), ["<Primary>5"])
        add("fav-current", self.toggle_fav_current, ["<Primary>d"])
        add("export-favs", self.export_favorites)
        add("clear-queue", self.clear_queue)
        add("upnext", lambda: self.upnext_btn.set_active(not self.upnext_btn.get_active()), ["<Primary>u"])
        ac = Gio.SimpleAction.new_stateful("album-covers", None, GLib.Variant("b", self._covers_online()))
        ac.connect("activate", lambda a, v: self.set_album_covers(not a.get_state().get_boolean()))
        self.add_action(ac)
        add("undo", self.undo_last, ["<Primary>z"])
        self.lookup_action("undo").set_enabled(False)
        add("equalizer", self.show_equalizer, ["<Primary><Shift>e"])
        rg = Gio.SimpleAction.new_stateful("replaygain", GLib.VariantType.new("s"), GLib.Variant("s", self.engine.rg_mode))
        rg.connect("activate", lambda a, v: (a.set_state(v), self.set_rg_mode(v.get_string())))
        self.add_action(rg)
        add("shortcuts", self.show_shortcuts, ["F1"])
        add("preferences", self.show_settings, ["<Primary>comma"])
        add("about", self.show_about)
        theme = Gio.SimpleAction.new_stateful("theme", GLib.VariantType.new("s"), GLib.Variant("s", self.scheme))
        theme.connect("activate", lambda a, v: (a.set_state(v), self.apply_theme(v.get_string())))
        self.add_action(theme)

    # ---------------------------------------------------------------- cell rendering
    def _cell_icon(self, col, cell, model, it, data):
        if model.get_value(it, 0) == self.current_path and self.current_path:
            icon = "media-playback-pause-symbolic" if self.engine.state == "paused" else "media-playback-start-symbolic"
            cell.set_property("icon-name", icon if self.engine.state != "stopped" else "media-playback-stop-symbolic")
            cell.set_visible(True)
        else:
            cell.set_property("icon-name", None)
            cell.set_visible(False)  # leave the space for a queue number

    def _cell_qnum(self, col, cell, model, it, data):
        p = model.get_value(it, 0)
        text = ""
        if self.queue.upnext and not (p == self.current_path and self.engine.state != "stopped"):
            try:
                text = str(self.queue.upnext.index(p) + 1)
            except ValueError:
                text = ""
        cell.set_property("text", text)
        if text:
            sel = self.song_view.get_selection().iter_is_selected(it)
            pal = PALETTES[self.scheme]
            cell.set_property("foreground", pal["accent_fg"] if sel else pal["accent_text"])
        cell.set_visible(bool(text))

    def _cell_fav(self, col, cell, model, it, data):
        on = model.get_value(it, 0) in self.library.favs
        sel = self.song_view.get_selection().iter_is_selected(it)
        cell.set_property("surface", self._thumbs["list_" + ("on" if on else "off") + ("_sel" if sel else "")])

    def _cell_group_thumb(self, col, cell, model, it, data):
        if self.mode != "albums":
            cell.set_visible(False)
            return
        cell.set_visible(True)
        key = model.get_value(it, 0)
        if key == ALL_KEY:
            cell.set_property("surface", self._blank_thumb)
            return
        e = self.album_thumbs.get(key)
        cell.set_property("surface", e[1] if e and e[1] is not None else self._album_placeholder)

    def _lookup_failed(self, tag, retry, err, what):
        """A lookup still failed after its quick retries: try again in a minute (twice), then say so once."""
        busy = any(code in (err or "") for code in ("429", "500", "502", "503", "504"))
        n = self._lookup_retries.get(tag, 0)
        if busy and n < 2:
            self._lookup_retries[tag] = n + 1
            GLib.timeout_add_seconds(int(60 * (n + 1) * LOOKUP_RETRY_SCALE), lambda: retry() and False)
            return
        if what in self._lookup_err_shown:
            return
        self._lookup_err_shown.add(what)
        why = "the service is busy right now" if busy else "you may be offline"
        self.show_error("Some %s couldn't be downloaded (%s: %s). player163 will try again next time you open it."
                        % (what, why, err))

    # ---------------------------------------------------------------- album covers (local + online)
    def _covers_online(self):
        return self.cfg.get("album_covers") is True

    def _want_cover(self, key, force=False):
        """No cover in the files or folder: look one up on the Cover Art Archive (if the user said yes)."""
        if not force and (not self._covers_online() or key in self._cover_tried):
            return
        album, _sep, artist = key.partition("\x1f")
        if not album.strip():
            return
        meta = cover_meta(key)
        if meta and not force:
            if meta.get("source") in ("custom", "caa"):
                return
            if meta.get("source") == "none" and time.time() - meta.get("fetched", 0) < 30 * 86400:
                return
        self._cover_tried.add(key)
        self.photo_finder.request_album(key, album, artist)

    def _cover_ready(self, key, res, ok, err=None):
        self.photo_finder.pending.discard("album:" + key)
        if not ok:
            album, _sep, artist = key.partition("\x1f")
            self._lookup_failed("album:" + key, lambda: self.photo_finder.request_album(key, album, artist),
                                err, "album covers")
            return False
        meta = cover_meta(key)
        if meta and meta.get("source") == "custom":
            return False
        if res:
            data = normalize_cover(res.pop("data"), 600, reencode=True)
            if data is None:
                save_stored_cover(key, None, {"source": "none"})
            else:
                save_stored_cover(key, data[0], res)
                self._cover_changed(key)
                if self._forced_cover == key:
                    self.flash_status("Found a cover on the Cover Art Archive.")
        else:
            save_stored_cover(key, None, {"source": "none"})
            if self._forced_cover == key:
                self.flash_status("No cover found online for this album.", 5)
        return False

    def _cover_changed(self, key):
        self.album_thumbs.pop(key, None)
        self.small_thumbs.pop(key, None)
        self.big_thumbs.pop(key, None)
        self._thumb_loader.pending.discard(key)
        self._small_loader.pending.discard(key)
        self._big_loader.pending.discard(key)
        if self.mode == "albums" and key in self._album_rep:
            self._thumb_loader.request(key, self._album_rep[key])
        self.group_view.queue_draw()
        self.song_view.queue_draw()
        self.grid_view.queue_draw()
        t = self.get_track(self.current_path) if self.current_path else None
        if t and t["album_key"] == key:
            self._show_now_playing(self.current_path)

    def find_cover_online(self, key):
        meta = cover_meta(key)
        if meta and meta.get("source") == "none":
            save_stored_cover(key, None, None)
        self._forced_cover = key
        self._cover_tried.discard(key)
        self.flash_status("Looking for a cover online…")
        self._want_cover(key, force=True)

    def set_album_covers(self, on):
        self.cfg["album_covers"] = bool(on)
        self.cover_bar.hide()
        self._cover_tried.clear()
        if on:
            self._clear_thumbs()   # empty results get asked for again
            self.refresh_groups() if self.mode == "albums" else None
            self.grid_view.queue_draw()
            self.song_view.queue_draw()
        act = self.lookup_action("album-covers")
        if act is not None:
            act.set_state(GLib.Variant("b", bool(on)))

    def _maybe_ask_covers(self):
        if self.mode == "albums" and self.cfg.get("album_covers") is None and self.library.tracks:
            self.cover_bar.show()

    def _album_tracks(self, key):
        return [t for t in self.library.tracks.values() if t["album_key"] == key]

    def choose_album_cover(self, key):
        fn = self._choose_image("Choose a cover for “%s”" % (key.partition("\x1f")[0] or UNKNOWN_ALBUM))
        if fn:
            self.set_album_cover(key, fn)

    def _choose_image(self, title):
        dlg = Gtk.FileChooserNative(title=title, transient_for=self,
                                    action=Gtk.FileChooserAction.OPEN, accept_label="Use Picture")
        flt = Gtk.FileFilter()
        flt.set_name("Images")
        for pat in ("*.jpg", "*.jpeg", "*.png", "*.JPG", "*.JPEG", "*.PNG", "*.gif", "*.GIF"):
            flt.add_pattern(pat)
        dlg.add_filter(flt)
        pics = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        if pics and os.path.isdir(pics):
            dlg.set_current_folder(pics)
        fn = dlg.get_filename() if dlg.run() == Gtk.ResponseType.ACCEPT else None
        dlg.destroy()
        return fn

    def set_album_cover(self, key, fn):
        """Use an image file as the album's cover: shown at once, and written into the songs' files."""
        tracks = self._album_tracks(key)
        try:
            if os.path.getsize(fn) > MAX_COVER_BYTES:
                raise ValueError("file is too large")
            with open(fn, "rb") as f:
                norm = normalize_cover(f.read())
        except Exception as e:
            print("player163: album cover:", e, file=sys.stderr)
            norm = None
        if norm is None:
            self.show_error("Couldn't use that picture. Try a JPEG or PNG image.")
            return
        data, mime = norm
        before = (stored_cover(key), cover_meta(key))
        save_stored_cover(key, data, {"source": "custom"})
        self._cover_changed(key)
        if tracks:
            self._write_jobs([(t["path"], {}, ("set", data, mime)) for t in tracks],
                             label="Set the cover of “%s”" % (tracks[0]["d_album"]),
                             covers={key: before})

    def remove_album_cover(self, key):
        """Forget the cover you set in player163 (the files keep any cover written into them)."""
        save_stored_cover(key, None, None)
        self._cover_tried.discard(key)
        self._cover_changed(key)

    def _clear_thumbs(self):
        self.album_thumbs.clear()
        self.small_thumbs.clear()
        self.big_thumbs.clear()

    def _surface(self, pb):
        if pb is None:
            return None
        try:
            return Gdk.cairo_surface_create_from_pixbuf(pb, max(1, self.get_scale_factor()), None)
        except Exception:
            return None

    def _cell_small_thumb(self, col, cell, model, it, data):
        t = self.get_track(model.get_value(it, 0))
        surf = self._blank_small
        if t and t["album"]:
            key = t["album_key"]
            e = self.small_thumbs.get(key)
            if e is None:
                self._small_loader.request(key, self._album_rep.get(key, t["path"]))
            elif e is not False:
                surf = e
        cell.set_property("surface", surf)

    def _small_thumb_ready(self, key, path, pb):
        self._small_loader.pending.discard(key)
        if pb is None:
            self._want_cover(key)
        self.small_thumbs[key] = self._surface(pb) or False
        self.song_view.queue_draw()
        return False

    def _cell_grid_thumb(self, layout, cell, model, it, data=None):
        key = model.get_value(it, 0)
        e = self.big_thumbs.get(key)
        if e is None and key in self._album_rep:
            self._big_loader.request(key, self._album_rep[key])
        cell.set_property("surface", e if e else self._grid_placeholder)

    def _big_thumb_ready(self, key, path, pb):
        self._big_loader.pending.discard(key)
        if pb is None:
            self._want_cover(key)
        self.big_thumbs[key] = self._surface(pb) or False
        self.grid_view.queue_draw()
        return False

    def _thumb_ready(self, key, path, pb):
        self._thumb_loader.pending.discard(key)
        if pb is None:
            self._want_cover(key)
        surf = None
        if pb is not None:
            try:
                surf = Gdk.cairo_surface_create_from_pixbuf(pb, max(1, self.get_scale_factor()), None)
            except Exception:
                surf = None
        self.album_thumbs[key] = (path, surf)
        if self.mode == "albums":
            self.group_view.queue_draw()
        return False

    def _render_thumbs(self):
        pal = PALETTES[self.scheme]
        sf = max(1, self.get_scale_factor())
        if cairo is not None:
            self._blank_thumb = cairo.ImageSurface(cairo.FORMAT_ARGB32, ALBUM_THUMB * sf, ALBUM_THUMB * sf)
            self._blank_thumb.set_device_scale(sf, sf)
        else:
            self._blank_thumb = None
        ph_bg = "#1e1e1e" if self.scheme == "dark" else "#e8e8e3"
        ph_fg = "#4a4a4a" if self.scheme == "dark" else "#b9b9b3"
        self._album_placeholder = placeholder_surface(ALBUM_THUMB, ph_bg, ph_fg, sf)
        self._grid_placeholder = placeholder_surface(GRID_THUMB, ph_bg, ph_fg, sf)
        if cairo is not None:
            self._blank_small = cairo.ImageSurface(cairo.FORMAT_ARGB32, SMALL_THUMB * sf, SMALL_THUMB * sf)
            self._blank_small.set_device_scale(sf, sf)
        else:
            self._blank_small = None
        if hasattr(self, "grid_view"):
            self.grid_view.queue_draw()
        if hasattr(self, "group_view"):
            self.group_view.queue_draw()
        self._thumbs = {
            "list_on": thumb_surface(15, True, pal["accent_text"], 1.0, sf),
            "list_on_sel": thumb_surface(15, True, pal["accent_fg"], 1.0, sf),
            "list_off": thumb_surface(15, False, pal["fg"], 0.22, sf),
            "list_off_sel": thumb_surface(15, False, pal["accent_fg"], 0.45, sf),
            "btn_on": thumb_surface(18, True, pal["accent_text"], 1.0, sf),
            "btn_off": thumb_surface(18, False, pal["fg"], 0.85, sf),
            "queue": queue_surface(16, pal["fg"], sf) if cairo is not None else None,
        }

    def _cell_weight(self, col, cell, model, it, data):
        cur = bool(self.current_path) and model.get_value(it, 0) == self.current_path
        cell.set_property("weight", 700 if cur else 400)
        if cur and not self.song_view.get_selection().iter_is_selected(it):
            cell.set_property("foreground", PALETTES[self.scheme]["accent_text"])
        else:
            cell.set_property("foreground-set", False)

    @staticmethod
    def _group_search_eq(model, column, key, it):
        # Return False when the row matches (GTK convention)
        return key.casefold() not in (model.get_value(it, 1) or "").casefold()

    # ---------------------------------------------------------------- library views
    def get_track(self, path):
        if path is None:
            return None
        return self.library.tracks.get(path) or self.extra.get(path)

    def _filter_tracks(self, use_group=True):
        words = self.search.get_text().casefold().split()
        sel = self.group_sel if use_group else None
        mode = self.mode
        favs = self.library.favs if mode == "favorites" else None
        order = self._list_order if (mode == "playlists" and use_group) else None
        never = mode == "playlists" and use_group and self.group_sel == "smart:never"
        for t in self.library.tracks.values():
            if favs is not None and t["path"] not in favs:
                continue
            if order is not None and t["path"] not in order:
                continue
            if never and t["path"] in self.library.plays:
                continue
            if words and not all(w in t["hay"] for w in words):
                continue
            if sel is not None:
                if mode == "artists" and t["d_artist"] != sel:
                    continue
                if mode == "albums" and t["album_key"] != sel:
                    continue
            yield t

    def refresh_all(self, keep_scroll=False):
        if self.mode in ("artists", "albums"):
            self.refresh_groups()
        elif self.mode == "playlists":
            self.refresh_playlists()
        self.refresh_songs(keep_scroll)

    # ---------------------------------------------------------------- playlists
    def _compute_order(self):
        """Order of the selected playlist / smart list (path -> rank), or None for Never Played."""
        lib, key = self.library, self.group_sel
        if self.mode != "playlists" or not key:
            return None
        tracks = lib.tracks
        if key == "smart:added":
            items = sorted(tracks, key=lambda p: -lib.added.get(p, 0))[:SMART_LIMIT]
        elif key == "smart:most":
            items = sorted((p for p in lib.plays if p in tracks), key=lambda p: (-lib.plays[p][0], -lib.plays[p][1]))[:SMART_LIMIT]
        elif key == "smart:recent":
            items = sorted((p for p in lib.plays if p in tracks), key=lambda p: -lib.plays[p][1])[:SMART_LIMIT]
        elif key == "smart:never":
            return None
        elif key.startswith("pl:"):
            pl = lib.playlists.get(int(key[3:]))
            items = pl["items"] if pl else []
        else:
            items = []
        return {p: i for i, p in enumerate(items)}

    def _playlist_id(self):
        if self.mode == "playlists" and self.group_sel and self.group_sel.startswith("pl:"):
            pid = int(self.group_sel[3:])
            return pid if pid in self.library.playlists else None
        return None

    def refresh_playlists(self):
        lib = self.library
        if self.group_sel is None or (self.group_sel.startswith("pl:") and self._playlist_id() is None):
            self.group_sel = "smart:added"
        counts = {"smart:added": min(len(lib.tracks), SMART_LIMIT),
                  "smart:most": min(sum(1 for p in lib.plays if p in lib.tracks), SMART_LIMIT),
                  "smart:recent": min(sum(1 for p in lib.plays if p in lib.tracks), SMART_LIMIT),
                  "smart:never": sum(1 for p in lib.tracks if p not in lib.plays)}
        self._building_groups = True
        try:
            self.group_view.set_model(None)
            self.group_store.clear()
            keys = []
            for key, name, sub in SMART_LISTS:
                self.group_store.append([key, "%s\n<small><span alpha='60%%'>%s · %d</span></small>"
                                         % (esc(name), esc(sub), counts[key])])
                keys.append(key)
            for pid, pl in sorted(lib.playlists.items(), key=lambda kv: kv[1]["name"].casefold()):
                n = sum(1 for p in pl["items"] if p in lib.tracks)
                self.group_store.append(["pl:%d" % pid, "<b>%s</b>\n<small><span alpha='60%%'>Playlist · %d song%s</span></small>"
                                         % (esc(pl["name"]), n, "" if n == 1 else "s")])
                keys.append("pl:%d" % pid)
            self.group_view.set_model(self.group_store)
            if self.group_sel in keys:
                i = keys.index(self.group_sel)
                self.group_view.get_selection().select_path(Gtk.TreePath(i))
                self.group_view.scroll_to_cell(Gtk.TreePath(i), None, False, 0, 0)
        finally:
            self._building_groups = False

    def _playlist_name(self, key):
        for k, name, _s in SMART_LISTS:
            if k == key:
                return name
        if key and key.startswith("pl:"):
            pl = self.library.playlists.get(int(key[3:]))
            return pl["name"] if pl else ""
        return ""

    def new_playlist(self, paths=(), name=None):
        if name is None:
            name = ask_text(self, "New Playlist", "Name your playlist:", "", ok_label="Create")
        if not name:
            return None
        pid = self.library.create_playlist(name, [p for p in paths if p in self.library.tracks])
        if paths:
            self.flash_status("Created \u201c%s\u201d with %d song%s" % (name, len(paths), "" if len(paths) == 1 else "s"))
        if self.mode == "playlists":
            self.group_sel = "pl:%d" % pid
            self.refresh_all()
        return pid

    def add_to_playlist(self, pid, paths):
        n = self.library.add_to_playlist(pid, [p for p in paths if p in self.library.tracks])
        name = self.library.playlists[pid]["name"]
        self.flash_status("Added %d song%s to \u201c%s\u201d" % (n, "" if n == 1 else "s", name) if n
                          else "Already in \u201c%s\u201d" % name)
        if self.mode == "playlists":
            self.refresh_all(keep_scroll=True)

    def remove_from_playlist(self, paths):
        pid = self._playlist_id()
        if pid is None:
            return
        drop = set(paths)
        items = [p for p in self.library.playlists[pid]["items"] if p not in drop]
        self.library.set_playlist_items(pid, items)
        self.refresh_all(keep_scroll=True)

    def move_in_playlist(self, paths, where):
        """where: 'up' | 'down' | 'top' | 'bottom'"""
        pid = self._playlist_id()
        if pid is None:
            return
        items = list(self.library.playlists[pid]["items"])
        sel = [p for p in items if p in set(paths)]
        if not sel:
            return
        if where in ("top", "bottom"):
            rest = [p for p in items if p not in set(sel)]
            items = sel + rest if where == "top" else rest + sel
        else:
            step = -1 if where == "up" else 1
            idxs = [items.index(p) for p in sel]
            for i in (idxs if step < 0 else reversed(idxs)):
                j = i + step
                if 0 <= j < len(items) and items[j] not in sel:
                    items[i], items[j] = items[j], items[i]
        self.library.set_playlist_items(pid, items)
        self.refresh_all(keep_scroll=True)
        sel_rows = self.song_view.get_selection()
        sel_rows.unselect_all()
        for p in sel:
            if p in self.row_index:
                sel_rows.select_path(Gtk.TreePath(self.row_index[p]))

    def rename_playlist(self, pid):
        name = ask_text(self, "Rename Playlist", "New name:", self.library.playlists[pid]["name"])
        if name:
            self.library.rename_playlist(pid, name)
            self.refresh_all(keep_scroll=True)

    def delete_playlist(self, pid):
        name = self.library.playlists[pid]["name"]
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.NONE, text="Delete \u201c%s\u201d?" % name,
                              secondary_text="The songs stay in your library; only the playlist goes.")
        d.add_button("Cancel", Gtk.ResponseType.CANCEL)
        b = d.add_button("Delete", Gtk.ResponseType.OK)
        b.get_style_context().add_class("destructive-action")
        ok = d.run() == Gtk.ResponseType.OK
        d.destroy()
        if ok:
            self.library.delete_playlist(pid)
            self.group_sel = None
            self.refresh_all()

    def import_playlist(self):
        dlg = Gtk.FileChooserNative(title="Import a playlist", transient_for=self,
                                    action=Gtk.FileChooserAction.OPEN, accept_label="Import")
        flt = Gtk.FileFilter()
        flt.set_name("Playlists (.m3u, .m3u8)")
        for pat in ("*.m3u", "*.m3u8", "*.M3U", "*.M3U8"):
            flt.add_pattern(pat)
        dlg.add_filter(flt)
        fn = dlg.get_filename() if dlg.run() == Gtk.ResponseType.ACCEPT else None
        dlg.destroy()
        if not fn:
            return
        base = os.path.dirname(fn)
        paths = []
        try:
            with open(fn, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("file://"):
                        line = Gio.File.new_for_uri(line).get_path() or ""
                    path = os.path.normpath(line if os.path.isabs(line) else os.path.join(base, line))
                    if path in self.library.tracks:
                        paths.append(path)
        except OSError as e:
            self.show_error("Couldn't read that playlist: %s" % e)
            return
        name = os.path.splitext(os.path.basename(fn))[0]
        pid = self.library.create_playlist(name, paths)
        self.flash_status("Imported \u201c%s\u201d: %d song%s found in your library" % (name, len(paths), "" if len(paths) == 1 else "s"))
        self.mode_buttons["playlists"].set_active(True)
        self.group_sel = "pl:%d" % pid
        self.refresh_all()

    def export_m3u(self, paths, name):
        paths = [p for p in paths if p in self.library.tracks and "\n" not in p and "\r" not in p]
        if not paths:
            self.show_error("Nothing to export yet.")
            return
        dlg = Gtk.FileChooserNative(title="Export Playlist", transient_for=self,
                                    action=Gtk.FileChooserAction.SAVE, accept_label="Export")
        dlg.set_do_overwrite_confirmation(True)
        dlg.set_current_name("%s.m3u" % re.sub(r'[/\\:*?"<>|]', "_", name))
        folder = (self.folders() or [None])[0]
        if folder and os.path.isdir(folder):
            dlg.set_current_folder(folder)
        fn = dlg.get_filename() if dlg.run() == Gtk.ResponseType.ACCEPT else None
        dlg.destroy()
        if not fn:
            return

        def one_line(x):  # a line break in a tag must not add playlist entries
            return " ".join(str(x).splitlines())
        try:
            with open(fn, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for p in paths:
                    t = self.library.tracks[p]
                    f.write("#EXTINF:%d,%s - %s\n%s\n" % (int(t["duration"]), one_line(t["d_artist"]), one_line(t["d_title"]), p))
            self.flash_status("Exported {:,} songs to {}".format(len(paths), os.path.basename(fn)))
        except OSError as e:
            self.show_error("Couldn't export: %s" % e)

    def refresh_groups(self):
        if self.mode == "playlists":
            return self.refresh_playlists()
        groups = {}
        for t in self._filter_tracks(use_group=False):
            key = t["d_artist"] if self.mode == "artists" else t["album_key"]
            g = groups.get(key)
            if g is None:
                groups[key] = g = {"n": 0, "t": t, "albums": set(), "year": "", "rep": t}
            if (t["disc"], t["track"], t["path"]) < (g["rep"]["disc"], g["rep"]["track"], g["rep"]["path"]):
                g["rep"] = t  # first track of the album: its art (or folder cover) is the album's
            g["n"] += 1
            g["albums"].add(t["album"])
            if t["year"] and (not g["year"] or t["year"] < g["year"]):
                g["year"] = t["year"]

        if self.mode == "artists":
            keys = sorted(groups, key=lambda k: sort_key("" if k == UNKNOWN_ARTIST else k))
        else:
            keys = sorted(groups, key=lambda k: (groups[k]["t"]["sk_album"], groups[k]["t"]["sk_albumartist"]))

        self._building_groups = True
        try:
            self.group_view.set_model(None)
            self.group_store.clear()
            total = sum(g["n"] for g in groups.values())
            label = "All Artists" if self.mode == "artists" else "All Albums"
            self.group_store.append([ALL_KEY, "<b>%s</b>  <span alpha='60%%'>%d</span>" % (label, total)])
            for k in keys:
                g = groups[k]
                if self.mode == "artists":
                    na = len(g["albums"])
                    markup = "%s\n<small><span alpha='60%%'>%d song%s · %d album%s</span></small>" % (
                        esc(k), g["n"], "" if g["n"] == 1 else "s", na, "" if na == 1 else "s")
                else:
                    t = g["t"]
                    bits = [esc(t["d_albumartist"])]
                    if g["year"]:
                        bits.append(g["year"])
                    bits.append("%d song%s" % (g["n"], "" if g["n"] == 1 else "s"))
                    markup = "%s\n<small><span alpha='60%%'>%s</span></small>" % (esc(t["d_album"]), " · ".join(bits))
                    rep_path = g["rep"]["path"]
                    self._album_rep[k] = rep_path
                    have = self.album_thumbs.get(k)
                    if have is None or have[0] != rep_path:
                        self._thumb_loader.request(k, rep_path)
                self.group_store.append([k, markup])
            self.group_view.set_model(self.group_store)
            if self.mode == "albums":
                self.grid_view.set_model(None)
                self.grid_store.clear()
                for k in keys:
                    t = groups[k]["t"]
                    sub = t["d_albumartist"] + (" · " + groups[k]["year"] if groups[k]["year"] else "")
                    self.grid_store.append([k, "<b>%s</b>\n<small><span alpha='60%%'>%s</span></small>"
                                            % (esc(t["d_album"]), esc(sub))])
                self.grid_view.set_model(self.grid_store)
            sel = self.group_view.get_selection()
            target = 0
            if self.group_sel is not None:
                if self.group_sel in groups:
                    target = keys.index(self.group_sel) + 1
                else:
                    self.group_sel = None
            sel.select_path(Gtk.TreePath(target))
            if target:
                self.group_view.scroll_to_cell(Gtk.TreePath(target), None, False, 0, 0)
        finally:
            self._building_groups = False

    def _sort_keyfunc(self):
        c = self.sort_col
        if c == "title":
            return lambda t: (t["sk_title"], t["sk_artist"])
        if c == "artist":
            return lambda t: (t["sk_artist"], t["sk_album"], t["disc"], t["track"], t["sk_title"])
        if c == "album":
            return lambda t: (t["sk_album"], t["sk_albumartist"], t["disc"], t["track"], t["sk_title"])
        if c == "year":
            return lambda t: (t["year"] or "9999", t["sk_album"], t["disc"], t["track"])
        if c == "time":
            return lambda t: (t["duration"], t["sk_title"])
        if c == "track":
            return lambda t: (t["disc"], t["track"], t["sk_title"])
        # default orderings per view
        if self.mode == "playlists":
            order = self._list_order
            if order is not None:
                return lambda t: order.get(t["path"], 1 << 30)
            return lambda t: (t["sk_artist"], t["sk_album"], t["disc"], t["track"], t["sk_title"])
        if self.mode == "favorites":  # newest favorite first
            favs = self.library.favs
            return lambda t: -favs.get(t["path"], 0)
        if self.mode == "albums" and self.group_sel is not None:
            return lambda t: (t["disc"], t["track"], t["sk_title"])
        if self.mode == "albums":
            return lambda t: (t["sk_album"], t["sk_albumartist"], t["disc"], t["track"], t["sk_title"])
        if self.mode == "artists" and self.group_sel is not None:
            return lambda t: (t["year"] or "9999", t["sk_album"], t["disc"], t["track"], t["sk_title"])
        return lambda t: (t["sk_artist"], t["sk_album"], t["disc"], t["track"], t["sk_title"])

    def refresh_songs(self, keep_scroll=False):
        self._list_order = self._compute_order()
        tracks = list(self._filter_tracks(True))
        tracks.sort(key=self._sort_keyfunc(), reverse=bool(self.sort_col and self.sort_desc))
        selected = self.selected_paths()
        vval = self.song_scroll.get_vadjustment().get_value()

        self.song_view.set_model(None)
        self.song_store.clear()
        append = self.song_store.append
        for t in tracks:
            append([t["path"], str(t["track"]) if t["track"] else "", t["d_title"], t["d_artist"],
                    t["d_album"], t["year"], fmt_time(t["duration"]) if t["duration"] else ""])
        self.song_view.set_model(self.song_store)
        self.visible_paths = [t["path"] for t in tracks]
        self.row_index = {p: i for i, p in enumerate(self.visible_paths)}

        sel = self.song_view.get_selection()
        for p in selected:
            i = self.row_index.get(p)
            if i is not None:
                sel.select_path(Gtk.TreePath(i))
        if keep_scroll:
            GLib.idle_add(lambda: self.song_scroll.get_vadjustment().set_value(vval) and False)
        self._update_status(tracks)

    def _update_status(self, tracks=None, text=None):
        if text is not None:
            self.status.set_text(text)
            return
        if tracks is None:
            tracks = [self.library.tracks[p] for p in self.visible_paths if p in self.library.tracks]
        fl = self.folders()
        folder = "\n".join(fl)
        n = len(tracks)
        total = sum(t["duration"] for t in tracks)
        bits = [(os.path.basename(fl[0].rstrip("/")) or fl[0]) if len(fl) == 1 else "%d folders" % len(fl)]
        bits.append("{:,} song{}".format(n, "" if n == 1 else "s"))
        if total:
            bits.append(fmt_total(total))
        if self.search.get_text().strip():
            bits.append("filtered")
        if self.queue.upnext:
            bits.append("%d up next" % len(self.queue.upnext))
        if self.engine.rg_mode != "off" and self.analyzer.queued:
            bits.append("leveling: measuring {:,} song{}".format(len(self.analyzer.queued),
                                                               "" if len(self.analyzer.queued) == 1 else "s"))
        if self.mode == "playlists" and self.group_sel:
            bits[0] = self._playlist_name(self.group_sel)
            if not n and self.group_sel.startswith("pl:") and not self.search.get_text().strip():
                bits = ["Empty playlist. Right-click songs anywhere → Add to Playlist"]
        if self.mode == "favorites":
            bits[0] = "Favorites"
            if not n and not self.search.get_text().strip():
                bits = ["No favorites yet — click 👍 next to the song title (or press Ctrl+D) while it plays"]
        self.status.set_text("  ·  ".join(bits))
        self.status.set_tooltip_text(folder)

    def selected_paths(self):
        model, rows = self.song_view.get_selection().get_selected_rows()
        if model is None:
            return []
        return [model[r][0] for r in rows]

    def _apply_mode_visibility(self):
        self.sidebar.set_visible(self.mode in ("artists", "albums", "playlists"))
        self.newpl_btn.set_visible(self.mode == "playlists")
        grid = self.mode == "albums" and self.album_layout == "grid"
        self.lib_stack.set_visible_child_name("grid" if grid else "list")
        self.layout_box.set_visible(self.mode == "albums")

    def _on_layout_toggled(self, btn, layout):
        if not btn.get_active() or not self._ready:
            return
        self.album_layout = layout
        self.cfg["album_layout"] = layout
        self._apply_mode_visibility()
        self.refresh_all()

    def _album_paths(self, key):
        ts = [t for t in self.library.tracks.values() if t["album_key"] == key]
        ts.sort(key=lambda t: (t["disc"], t["track"], t["sk_title"]))
        return [t["path"] for t in ts]

    def _on_grid_activated(self, view, tree_path):
        key = self.grid_store[tree_path][0]
        self.group_sel = key
        self.cfg["sel_albums"] = key
        self.layout_btns["list"].set_active(True)  # opens the album's songs

    def _on_grid_button(self, view, ev):
        if ev.type != Gdk.EventType.BUTTON_PRESS or ev.button != 3:
            return False
        tp = view.get_path_at_pos(int(ev.x), int(ev.y))
        if tp is None:
            return True
        view.select_path(tp)
        key = self.grid_store[tp][0]
        paths = self._album_paths(key)
        menu = Gtk.Menu()
        self._menu_item(menu, "Play Album", lambda: self.play_paths(paths))
        self._menu_item(menu, "Play Next", lambda: self.queue_paths(paths, True))
        self._menu_item(menu, "Add to Queue", lambda: self.queue_paths(paths, False))
        menu.append(Gtk.SeparatorMenuItem())
        self._menu_item(menu, "Rename Album…", lambda: self.rename_album(key))
        self._menu_item(menu, "Edit Tags of All %d Songs…" % len(paths),
                        lambda: self.edit_tracks([self.library.tracks[p] for p in paths]))
        self._cover_menu_items(menu, key)
        self._popup(menu, ev, view)
        return True

    def _on_mode_toggled(self, btn, key):
        if not btn.get_active() or not self._ready:
            return
        self.mode = key
        self.group_sel = self.cfg.get("sel_" + key) if key in ("artists", "albums", "playlists") else None
        self.sort_col = None
        self._update_sort_indicators()
        self._apply_mode_visibility()
        self.cfg["mode"] = key
        self.refresh_all()
        self._maybe_ask_covers()

    def _on_group_changed(self, sel):
        if self._building_groups:
            return
        model, it = sel.get_selected()
        key = model.get_value(it, 0) if it else ALL_KEY
        self.group_sel = None if key == ALL_KEY else key
        if self.mode in ("artists", "albums", "playlists"):
            self.cfg["sel_" + self.mode] = self.group_sel
        self.sort_col = None
        self._update_sort_indicators()
        self.refresh_songs()
        self.song_scroll.get_vadjustment().set_value(0)

    def _on_search_changed(self, entry):
        if self.mode in ("artists", "albums"):
            self.refresh_groups()
        self.refresh_songs()

    def _on_header_clicked(self, col, sid):
        if self.sort_col != sid:
            self.sort_col, self.sort_desc = sid, False
        elif not self.sort_desc:
            self.sort_desc = True
        else:
            self.sort_col = None
        self._update_sort_indicators()
        self.refresh_songs()

    def _update_sort_indicators(self):
        for sid, col in self.sort_columns.items():
            on = sid == self.sort_col
            col.set_sort_indicator(on)
            if on:
                col.set_sort_order(Gtk.SortType.DESCENDING if self.sort_desc else Gtk.SortType.ASCENDING)

    # ---------------------------------------------------------------- folder & scanning
    # ---------------------------------------------------------------- music folders
    def folders(self):
        f = self.cfg.get("folders")
        if not isinstance(f, list):  # older versions kept a single "folder"
            f = [self.cfg["folder"]] if self.cfg.get("folder") else []
            self.cfg["folders"] = f
            self.cfg.pop("folder", None)
        return [x for x in f if isinstance(x, str) and x]

    @staticmethod
    def _under(path, root):
        root = root.rstrip("/") + "/"
        return path.startswith(root)

    def choose_folder(self):
        dlg = Gtk.FileChooserNative(title="Add a music folder", transient_for=self,
                                    action=Gtk.FileChooserAction.SELECT_FOLDER, accept_label="Add Folder")
        music = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC)
        if music and os.path.isdir(music):
            dlg.set_current_folder(music)
        path = dlg.get_filename() if dlg.run() == Gtk.ResponseType.ACCEPT else None
        dlg.destroy()
        if path:
            self.add_folder(path)

    def add_folder(self, path, quiet=False):
        path = os.path.abspath(path).rstrip("/") or "/"
        folders = self.folders()
        for f in folders:
            if path == f or self._under(path, f):
                if not quiet:
                    self.flash_status("%s is already in your library" % os.path.basename(path))
                return False
        # a parent of existing folders replaces them
        folders = [f for f in folders if not self._under(f, path)] + [path]
        self.cfg["folders"] = folders
        save_config(self.cfg)
        self.stack.set_visible_child_name("library")
        self.refresh_all()
        self.start_scan()
        if hasattr(self, "settings_dialog") and self.settings_dialog:
            self.settings_dialog.refresh_folders()
        return True

    def set_folder(self, path):
        self.add_folder(path)

    def remove_folder(self, path):
        folders = [f for f in self.folders() if f != path]
        self.cfg["folders"] = folders
        save_config(self.cfg)
        gone = [p for p in self.library.tracks if self._under(p, path)
                and not any(self._under(p, f) for f in folders)]
        self.library.remove(gone)
        self._clear_thumbs()
        if not folders:
            self.stack.set_visible_child_name("empty")
        self.refresh_all()
        self.start_scan(quiet=True)
        if hasattr(self, "settings_dialog") and self.settings_dialog:
            self.settings_dialog.refresh_folders()

    def _auto_music_folder(self):
        """First launch: use ~/Music if it holds any music."""
        music = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC) or os.path.expanduser("~/Music")
        if not os.path.isdir(music):
            return False
        n = 0
        for dp, dn, fns in os.walk(music):
            n += 1
            if any(os.path.splitext(f)[1].lower() in AUDIO_EXTS for f in fns):
                self.add_folder(music, quiet=True)
                GLib.timeout_add_seconds(3, lambda: self.flash_status(
                    "Using your Music folder. Add more folders in Settings (Ctrl+,).", 8) and False)
                return True
            if n > 3000:
                break
        return False

    def _on_drop(self, widget, context, x, y, data, info, time_):
        paths = [Gio.File.new_for_uri(u).get_path() for u in (data.get_uris() or [])]
        paths = [x for x in paths if x]
        if not paths:
            return
        image = len(paths) == 1 and os.path.splitext(paths[0].lower())[1] in (".jpg", ".jpeg", ".png", ".gif")
        if image and widget is not self:
            key = self._drop_key(widget, x, y)
            if key is not None:
                self.set_album_cover(key[1], paths[0])
                return
        if image:
            self.flash_status("Drop a picture on an album to use it as its cover.", 6)
            return
        self.open_paths(paths)

    def _drop_key(self, widget, x, y):
        """Which album an image was dropped on."""
        if widget is self.art_ev:
            t = self.get_track(self.current_path) if self.current_path else None
            return ("album", t["album_key"]) if t and t["album"] else None
        if widget is self.grid_view:
            tp = widget.get_path_at_pos(*widget.convert_widget_to_bin_window_coords(x, y))
            return ("album", self.grid_store[tp][0]) if tp is not None else None
        if widget is self.group_view and self.mode == "albums":
            hit = widget.get_path_at_pos(*widget.convert_widget_to_bin_window_coords(x, y))
            if not hit:
                return None
            key = self.group_store[hit[0]][0]
            return None if key == ALL_KEY else ("album", key)
        return None

    def start_scan(self, quiet=False):
        roots = self.folders()
        if not roots:
            if not quiet:
                self.choose_folder()
            return
        missing = [r for r in roots if not os.path.isdir(r)]
        if missing and not quiet:
            self.show_error("Can't reach %s:\n%s\n(Is the drive connected? Its songs stay in your library.)" % (
                "this music folder" if len(missing) == 1 else "these music folders", "\n".join(missing)))
        if len(missing) == len(roots):
            return
        self._scan_roots = [r for r in roots if r not in missing]
        self._scan_gen += 1
        gen = self._scan_gen
        self._scanning = True
        self._scan_quiet = quiet
        known = {p: t["mtime"] for p, t in self.library.tracks.items()}
        if not quiet:
            self.scan_spinner.show()
            self.scan_spinner.start()
            self._update_status(text="Scanning your music…")

        def cancelled():
            return gen != self._scan_gen

        def on_batch(batch):
            GLib.idle_add(self._scan_batch, gen, batch)

        def on_progress(i, n):
            GLib.idle_add(self._scan_progress, gen, i, n)

        def on_done(batch, seen, dirs):
            GLib.idle_add(self._scan_done, gen, batch, seen, dirs)

        th = threading.Thread(target=scan_worker, args=(self._scan_roots, known, cancelled, on_batch, on_progress, on_done),
                              daemon=True)
        th.start()

    def _merge_scanned(self, batch):
        # Never overwrite a newer in-app edit with an older read from the scanner.
        fresh = [t for t in batch
                 if t["path"] not in self.library.tracks or self.library.tracks[t["path"]]["mtime"] <= t["mtime"]]
        self.library.upsert(fresh)

    def _scan_batch(self, gen, batch):
        if gen != self._scan_gen:
            return False
        self._scan_new |= {t["path"] for t in batch}
        self._merge_scanned(batch)
        if not self._scan_refresh_id:  # refresh at most every 1.5 s during a big first scan
            def later():
                self._scan_refresh_id = 0
                if gen == self._scan_gen:
                    self.refresh_all(keep_scroll=True)
                    if not self._scan_quiet:
                        self._update_status(text=self._scan_text)
                return False
            self._scan_refresh_id = GLib.timeout_add(1500, later)
        return False

    _scan_text = "Scanning your music…"

    def _scan_progress(self, gen, i, n):
        if gen != self._scan_gen:
            return False
        self._scan_text = ("Finding music… {:,} files".format(n) if i == 0
                           else "Reading tags… {:,} of {:,}".format(i, n))
        if not self._scan_quiet:
            self._update_status(text=self._scan_text)
        return False

    def _scan_done(self, gen, batch, seen, dirs=None):
        if gen != self._scan_gen:
            return False
        self._scanning = False
        self._merge_scanned(batch)
        if seen or not self.library.tracks:
            avail = self._scan_roots
            # songs on an unplugged drive stay; songs outside every folder go
            gone = [p for p in self.library.tracks if p not in seen and
                    (any(self._under(p, r) for r in avail) or not any(self._under(p, f) for f in self.folders()))]
            lost = {}
            for p in gone:
                if p in self.library.favs:
                    t = self.library.tracks[p]
                    lost[(t["title"].casefold(), t["artist"].casefold(), t["album"].casefold())] = p
            self.library.remove(gone)
            if lost:
                new_paths = {t["path"] for t in batch} | self._scan_new
                for p in new_paths:
                    t = self.library.tracks.get(p)
                    if t and t["title"] and p not in self.library.favs:
                        old = lost.pop((t["title"].casefold(), t["artist"].casefold(), t["album"].casefold()), None)
                        if old:
                            self.library.move_favorite(old, p)
        self._scan_new = set()
        self._clear_thumbs()  # covers may have been added/changed; the disk cache makes this cheap
        self.scan_spinner.stop()
        self.scan_spinner.hide()
        if self._scan_refresh_id:
            GLib.source_remove(self._scan_refresh_id)
            self._scan_refresh_id = 0
        self.refresh_all(keep_scroll=True)
        if not self.library.tracks:
            self._update_status(text="No music files found in this folder")
        self._watch_dirs(dirs)
        self._schedule_analysis()
        if self._rescan_after:
            self._rescan_after = False
            GLib.idle_add(lambda: self.start_scan(quiet=True) and False)
        return False

    # ---------------------------------------------------------------- volume leveling
    def level_gain(self, path):
        """Gain (dB) for a song we measured ourselves; 0 for songs with ReplayGain tags (rgvolume
        handles those) or not measured yet. Called from the streaming thread."""
        e = self.library.loud.get(path) if path else None
        if not e or e[1] is None or e[4] != "scan":
            return 0.0
        g = e[1]
        if self.engine.rg_mode == "album":
            t = self.get_track(path)
            ag = self._album_gain.get(t["album_key"]) if t else None
            if ag is not None:
                g = ag
        if e[2]:  # don't boost past the song's own peak (no clipping)
            g = min(g, -20.0 * math.log10(max(e[2], 1e-6)))
        return max(-20.0, min(20.0, g))

    def _recompute_album_gain(self, key):
        tracks = [t for t in self.library.tracks.values() if t["album_key"] == key]
        num = den = 0.0
        for t in tracks:
            e = self.library.loud.get(t["path"])
            if not e or e[1] is None or e[0] != t["mtime"]:
                self._album_gain.pop(key, None)  # wait until the whole album is measured
                return
            w = max(t["duration"], 1.0)
            num += w * 10 ** (-e[1] / 10.0)  # loudness relative to the reference, power-averaged
            den += w
        if den:
            self._album_gain[key] = -10.0 * math.log10(num / den)

    def _schedule_analysis(self):
        if self.engine.rg_mode == "off":
            return
        loud = self.library.loud
        cur_album = (self.get_track(self.current_path) or {}).get("album_key")
        todo = [t for t in self.library.tracks.values()
                if t["path"] not in loud or loud[t["path"]][0] != t["mtime"]]
        todo.sort(key=lambda t: (t["album_key"] != cur_album, t["sk_artist"], t["sk_album"], t["disc"], t["track"]))
        self.analyzer.submit([(t["path"], t["mtime"]) for t in todo])
        if not self._album_gain:
            for key in {t["album_key"] for t in self.library.tracks.values()}:
                self._recompute_album_gain(key)

    def _loudness_ready(self, path, mtime, res):
        self.analyzer.queued.discard(path)
        if res is None:
            res = (None, None, None, "fail")
        self.library.set_loudness(path, mtime, *res)
        t = self.library.tracks.get(path)
        if t:
            self._recompute_album_gain(t["album_key"])
        now = time.monotonic()
        if not getattr(self, "_flash_id", 0) and not self._scanning and (now - self._an_status_t > 1.0 or not self.analyzer.queued):
            self._an_status_t = now
            self._update_status()
        return False

    def set_rg_mode(self, mode):
        if mode not in ("off", "track", "album") or mode == self.engine.rg_mode:
            return
        self.cfg["replaygain"] = mode
        self.engine.rg_mode = mode
        self.analyzer.paused = mode == "off"
        if self.engine.state != "stopped" and self.current_path:  # apply now: reload at the same spot
            was = self.engine.state
            pos = self.engine.position()
            self.pending = (self.queue.pos, self.current_path)
            self.engine.load(self.current_path, pos)
            if was == "paused":
                self.engine.pause()
            self._set_play_icon()
        self._schedule_analysis()
        label = {"off": "Volume leveling off", "track": "Volume leveling: per song",
                 "album": "Volume leveling: per album"}[mode]
        self.flash_status(label)

    # ---------------------------------------------------------------- equalizer
    def _eq_values(self):
        vals = self.cfg.get("eq_bands")
        if isinstance(vals, list) and len(vals) == 10:
            try:
                return [max(-12.0, min(12.0, float(v))) for v in vals]
            except (TypeError, ValueError):
                pass
        return list(EQ_PRESETS.get(self.cfg.get("eq_preset", "Flat"), EQ_PRESETS["Flat"]))

    def set_eq_on(self, on):
        self.cfg["eq_on"] = bool(on)
        self.engine.set_eq(self._eq_values() if on else [0] * 10)

    def set_eq_bands(self, bands, preset):
        self.cfg["eq_bands"] = [round(float(b), 1) for b in bands]
        self.cfg["eq_preset"] = preset
        if self.cfg.get("eq_on"):
            self.engine.set_eq(bands)

    def show_equalizer(self):
        if self._eq_dialog is None:
            self._eq_dialog = EqualizerDialog(self)
        self._eq_dialog.show_all()
        self._eq_dialog.present()

    # ---------------------------------------------------------------- auto-detect new music
    def _watch_dirs(self, dirs):
        if dirs is None:
            return
        if not self.cfg.get("watch", True):
            dirs = []
        want = set(dirs[:MAX_WATCHED_DIRS])
        for d in list(self._monitors):
            if d not in want:
                self._monitors.pop(d).cancel()
        for d in want:
            if d in self._monitors:
                continue
            try:
                m = Gio.File.new_for_path(d).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
                m.connect("changed", self._on_dir_changed)
                self._monitors[d] = m
            except Exception as e:  # e.g. the system's inotify watch limit
                print("player163: can't watch more folders:", e, file=sys.stderr)
                break

    @staticmethod
    def _fs_relevant(name):
        if not name or name.startswith("."):
            return False
        ext = os.path.splitext(name.lower())[1]
        return ext in AUDIO_EXTS or ext in (".jpg", ".jpeg", ".png") or ext == ""

    def _on_dir_changed(self, mon, f, other, event):
        E = Gio.FileMonitorEvent
        if event not in (E.CREATED, E.DELETED, E.CHANGES_DONE_HINT, E.MOVED_IN, E.MOVED_OUT, E.RENAMED):
            return
        if time.monotonic() < self._ignore_fs_until:  # our own tag edits
            return
        names = [f.get_basename() if f else None, other.get_basename() if other else None]
        if not any(self._fs_relevant(n) for n in names):
            return
        if self._autoscan_id:
            GLib.source_remove(self._autoscan_id)  # wait until the copy settles
        self._autoscan_id = GLib.timeout_add(2500, self._autoscan)

    def _autoscan(self):
        self._autoscan_id = 0
        if self._scanning:
            self._rescan_after = True
        else:
            self.start_scan(quiet=True)
        return False

    # ---------------------------------------------------------------- playback
    def play_visible(self, path=None, start=0.0, from_start=False):
        paths = self.visible_paths
        if not paths:
            return
        if path is None:
            sel = self.selected_paths()
            path = sel[0] if sel else None
        idx = self.row_index.get(path, 0) if path else 0
        if path is None and self.queue.shuffle:
            idx = random.randrange(len(paths))
        pos, p = self.queue.set(paths, idx)
        self._play(pos, p, start, from_start)
        self.refresh_upnext()

    def play_paths(self, paths, idx=0):
        if paths:
            pos, p = self.queue.set(paths, idx)
            self._play(pos, p)
            self.refresh_upnext()

    def queue_paths(self, paths, front):
        """Play Next (front=True) or Add to Queue (front=False)."""
        paths = [p for p in paths if self.get_track(p)]
        if not paths:
            return
        if self.engine.state == "stopped":
            if paths[0] in self.row_index:
                self.play_visible(paths[0])
            else:
                self.play_paths([paths[0]])
            paths = paths[1:]
            if not paths:
                return
        self.queue.add_upnext(paths, front)
        self.refresh_upnext()
        n = len(self.queue.upnext)
        what = "Playing next" if front else "Added to queue"
        self.flash_status("%s: %s  ·  %d song%s up next" % (
            what, self.get_track(paths[0])["d_title"] + (" + %d more" % (len(paths) - 1) if len(paths) > 1 else ""),
            n, "" if n == 1 else "s"))
        self.song_view.queue_draw()

    def clear_queue(self):
        self.queue.clear_upnext()
        self.refresh_upnext()
        self.flash_status("Up next cleared")
        self.song_view.queue_draw()

    def flash_status(self, text, secs=4):
        self._update_status(text=text)
        if getattr(self, "_flash_id", 0):
            GLib.source_remove(self._flash_id)

        def restore():
            self._flash_id = 0
            self._update_status()
            return False
        self._flash_id = GLib.timeout_add_seconds(secs, restore)

    def _play(self, pos, path, start=0.0, from_start=False):
        if not path:
            return
        if self.current_path and self.engine.state != "stopped":
            self._save_long_position()   # leaving an audiobook mid-way: remember the spot
            self._manual_switch = True
        if not start and not from_start and self.is_long(path) and path in self.library.resume:
            start = self.library.resume[path]
            GLib.timeout_add(600, lambda: self.flash_status(
                "Resuming at %s  ·  right-click the song → Play from Start" % fmt_time(start), 6) and False)
        if not os.path.exists(path):
            self.show_error("File not found, skipping:\n%s" % path)
            self._errors += 1
            self.queue.sync(pos, path)
            if self._errors < 10:
                GLib.idle_add(self._skip_after_error)
            return
        self._resume = None
        self.pending = (pos, path)
        self.engine.load(path, start)
        self._set_play_icon()

    def _report_skipped(self):
        gone = self.queue.take_skipped()
        if gone:
            names = ", ".join(os.path.basename(g) for g in gone[:3]) + (" and %d more" % (len(gone) - 3) if len(gone) > 3 else "")
            self.show_error("Skipped %d song%s that %s moved, renamed or deleted: %s" % (
                len(gone), "" if len(gone) == 1 else "s", "was" if len(gone) == 1 else "were", names))

    def on_stream_start(self):
        pend, self.pending = self.pending, None
        self._report_skipped()
        if pend:
            self.queue.sync(*pend)
            self._errors = 0
            self._set_current(pend[1])
        self._set_play_icon()
        self.mpris.changed("PlaybackStatus", "Metadata")

    def _set_current(self, path):
        prev = self.current_path
        if prev and prev != path and not self._manual_switch and self.is_long(prev):
            self.library.clear_resume(prev)  # it played to the end
        self._manual_switch = False
        self._listened = 0.0
        self._counted = False
        self._resume_tick = 0.0
        self.current_path = path
        t = self.get_track(path)
        self.current_duration = t["duration"] if t else 0.0
        self._show_now_playing(path)
        self.song_view.queue_draw()
        self._update_speed_btn()
        t = self.get_track(path)
        if t and self._ready and self._notify_path != path:
            self._notify_path = path
            self._notify_song(t, self._cover_data)
        self.refresh_upnext()
        speed = self._speed_for(path)
        if speed != 1.0:
            GLib.timeout_add(250, lambda: (path == self.current_path and self.engine.state != "stopped"
                                           and self.engine.set_rate(speed)) and False)

    def _show_now_playing(self, path):
        t = self.get_track(path)
        if not t:
            self.np_title.set_text("Nothing playing")
            self.np_sub.set_text("")
            self._cover_data = None
            self._set_default_art()
            self.set_title(APP_NAME)
            self.mini.set_track(None, None)
            return
        self.np_title.set_text(t["d_title"])
        self.np_title.set_tooltip_text(t["d_title"])
        sub = t["d_artist"] + ("  —  " + t["album"] if t["album"] else "")
        self.np_sub.set_text(sub)
        self.np_sub.set_tooltip_text(sub)
        self.set_title("%s – %s" % (t["d_title"], t["d_artist"]))
        self.dur_label.set_text(fmt_time(t["duration"]))
        data = find_cover(path, t["album_key"])
        self._cover_data = data
        self._render_art()
        self.mini.set_track(t, data)
        if hasattr(self, "tray"):
            self.tray.update()
        self.update_fav_buttons()
        self._write_mpris_art(path, data)

    def _write_mpris_art(self, path, data):
        old = self._art_file
        self._art_file = None
        self.mpris.art_url = ""
        if data:
            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                fn = os.path.join(CACHE_DIR, "cover-%s" % hashlib.md5(path.encode("utf-8", "surrogateescape"), usedforsecurity=False).hexdigest())
                if fn != old:
                    with open(fn, "wb") as fh:
                        fh.write(data)
                self._art_file = fn
                self.mpris.art_url = GLib.filename_to_uri(fn, None)
            except Exception:
                pass
        if old and old != self._art_file:
            try:
                os.remove(old)
            except OSError:
                pass

    def _set_default_art(self):
        size = getattr(self, "_art_px", ART_SIZE)
        pb = logo_pixbuf(size)
        if pb:
            self.art.set_from_pixbuf(pb)
        else:
            self.art.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
            self.art.set_pixel_size(size - 8)

    def _set_play_icon(self):
        icon = "media-playback-pause-symbolic" if self.engine.state == "playing" else "media-playback-start-symbolic"
        self.play_btn.get_image().set_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR)
        self.seek.set_sensitive(self.engine.state != "stopped")
        self.mini.sync_play(self.engine.state)
        self.song_view.queue_draw()
        if hasattr(self, "tray"):
            self.tray.update()

    def on_eos(self):
        if self.current_path and self.is_long(self.current_path):
            self.library.clear_resume(self.current_path)
        # Normally only at the end of the queue. If the gapless handoff couldn't queue the next
        # song for any reason, carry on instead of stopping.
        nxt = self.queue.peek(1, auto=True)
        if nxt:
            self._play(*nxt)
            return
        self._report_skipped()
        self.engine.stop()
        self.seek.set_value(0)
        self.pos_label.set_text("0:00")
        self.mini.pos_label.set_text("0:00")
        self._set_play_icon()
        self.mpris.changed("PlaybackStatus")

    def on_engine_error(self, message):
        failed = self.pending[1] if self.pending else self.current_path
        if self.pending:
            self.queue.sync(*self.pending)
            self.pending = None
        self.engine.stop()
        self._set_play_icon()
        self.show_error("Couldn't play %s\n%s" % (os.path.basename(failed or "file"), message))
        self._errors += 1
        if self._errors < 10:
            GLib.timeout_add(300, self._skip_after_error)

    def _skip_after_error(self):
        nxt = self.queue.peek(1, auto=False)
        if nxt:
            self._play(*nxt)
        return False

    def on_duration_changed(self):
        d = self.engine.duration()
        if d > 0:
            self.current_duration = d
            t = self.library.tracks.get(self.current_path)
            if t is not None and not t["duration"]:
                t = dict(t)
                t["duration"] = d
                self.library.upsert([t])
            self.mpris.changed("Metadata")

    def play(self):
        if self.engine.state == "paused":
            self.engine.play()
        elif self.engine.state == "stopped":
            if self._resume:
                path, pos = self._resume
                if path in self.row_index:
                    self.play_visible(path, start=pos)
                else:
                    p, q = self.queue.set([path], 0)
                    self._play(p, q, pos)
            elif self.current_path and self.queue.current():
                self._play(self.queue.pos, self.queue.current())
            else:
                self.play_visible(None)
        self._set_play_icon()
        self.mpris.changed("PlaybackStatus")

    def pause(self):
        if self.engine.state == "playing":
            self._save_long_position()
            self.engine.pause()
            self._set_play_icon()
            self.mpris.changed("PlaybackStatus")

    def play_pause(self):
        if self.engine.state == "playing":
            self.pause()
        else:
            self.play()

    def stop(self):
        self.engine.stop()
        self.seek.set_value(0)
        self.pos_label.set_text("0:00")
        self.mini.pos_label.set_text("0:00")
        self._set_play_icon()
        self.mpris.changed("PlaybackStatus")

    def next_track(self):
        nxt = self.queue.peek(1, auto=False)
        if nxt:
            self._play(*nxt)
        elif self.queue.order:
            self._report_skipped()
            self.stop()

    def prev_track(self):
        if self.engine.state != "stopped" and self.engine.position() > 3:
            self.seek_to(0)
            return
        prv = self.queue.peek(-1, auto=False)
        if prv:
            self._play(*prv)

    def seek_to(self, sec):
        if self.engine.state == "stopped":
            return
        dur = self.engine.duration() or self.current_duration
        sec = max(0.0, min(sec, dur - 0.5 if dur else sec))
        self.engine.seek(sec)
        self.mpris.seeked(sec)

    def _on_seek_press(self, w, ev):
        self._seeking = True
        return False

    def _on_seek_release(self, w, ev):
        self._seeking = False
        self.seek_to(self.seek.get_value())
        return False

    def _on_seek_change(self, scale, scroll, value):
        self.pos_label.set_text(fmt_time(value))
        self.mini.pos_label.set_text(fmt_time(value))
        if not self._seeking:
            self.seek_to(value)
        return False

    def _tick(self):
        if self.engine.state == "playing" and self.current_path:
            self._listened += 0.25
            dur = self.current_duration or 0
            if not self._counted and self._listened >= min(30.0, max(5.0, dur * 0.5)) and self.current_path in self.library.tracks:
                self._counted = True
                self.library.record_play(self.current_path)
            self._resume_tick += 0.25
            if self._resume_tick >= 10:
                self._resume_tick = 0
                self._save_long_position()
        if self.engine.state != "stopped" and not self._seeking:
            pos = self.engine.position()
            dur = self.engine.duration() or self.current_duration
            adj = self.seek.get_adjustment()
            if dur > 0 and abs(adj.get_upper() - dur) > 0.5:
                adj.set_upper(dur)
                self.dur_label.set_text(fmt_time(dur))
                self.mini.dur_label.set_text(fmt_time(dur))
            self.seek.set_value(pos)
            self.pos_label.set_text(fmt_time(pos))
            self.mini.pos_label.set_text(fmt_time(pos))
        return True

    # shuffle / repeat / volume
    def _on_shuffle_toggled(self, btn):
        on = btn.get_active()
        self.queue.set_shuffle(on)
        self.cfg["shuffle"] = on
        self.refresh_upnext()
        btn.set_tooltip_text("Shuffle: %s  (Ctrl+S)" % ("on" if on else "off"))
        self.mini.sync_shuffle(on)
        self.mpris.changed("Shuffle")

    def _on_repeat_clicked(self, *_):
        nxt = {"off": "all", "all": "one", "one": "off"}[self.queue.repeat]
        self.set_repeat(nxt)

    def set_repeat(self, mode):
        self.queue.repeat = mode
        self.refresh_upnext()
        self.cfg["repeat"] = mode
        self._update_repeat_button()
        self.mpris.changed("LoopStatus")

    def _update_repeat_button(self):
        mode = self.queue.repeat
        b = self.repeat_btn
        b.handler_block_by_func(self._on_repeat_clicked)
        b.set_active(mode != "off")
        b.handler_unblock_by_func(self._on_repeat_clicked)
        icon = "media-playlist-repeat-song-symbolic" if mode == "one" else "media-playlist-repeat-symbolic"
        b.get_image().set_from_icon_name(icon, Gtk.IconSize.BUTTON)
        tip = {"off": "Repeat: off", "all": "Repeat: all", "one": "Repeat: this song"}[mode] + "  (Ctrl+R)"
        b.set_tooltip_text(tip)
        self.mini.sync_repeat(mode, icon, tip)

    def _on_volume_changed(self, scale):
        v = scale.get_value()
        self.cfg["volume"] = round(v, 3)
        if not self._syncing_volume:
            self.engine.set_volume(v)
            if self._muted and v > 0:
                self.toggle_mute()
        self._update_volume_icon()
        self.mpris.changed("Volume")

    def on_engine_volume(self):
        """Volume changed in the system mixer (or echoed back from our own change)."""
        try:
            lin = float(self.engine.pb.get_property("volume"))
        except Exception:
            return False
        v = max(0.0, min(1.0, lin)) ** (1.0 / 3.0)
        if abs(v - self.vol.get_value()) > 0.01:
            self._syncing_volume = True
            self.vol.set_value(v)
            self._syncing_volume = False
        return False

    def on_engine_mute(self):
        try:
            m = bool(self.engine.pb.get_property("mute"))
        except Exception:
            return False
        if m != self._muted:
            self._muted = m
            self.cfg["muted"] = m
            self._update_volume_icon()
        return False

    def toggle_mute(self):
        self._muted = not self._muted
        self.engine.set_mute(self._muted)
        self.cfg["muted"] = self._muted
        self._update_volume_icon()

    def _update_volume_icon(self):
        v = self.vol.get_value()
        if self._muted or v <= 0.001:
            icon = "audio-volume-muted-symbolic"
        elif v < 0.34:
            icon = "audio-volume-low-symbolic"
        elif v < 0.67:
            icon = "audio-volume-medium-symbolic"
        else:
            icon = "audio-volume-high-symbolic"
        self.mute_btn.get_image().set_from_icon_name(icon, Gtk.IconSize.BUTTON)
        self.mute_btn.set_tooltip_text("Unmute" if self._muted else "Mute")
        self.mini.sync_volume_icon(icon, "Unmute" if self._muted else "Mute")

    # ---------------------------------------------------------------- favorites
    def update_fav_buttons(self):
        path = self.current_path or (self._resume[0] if self._resume else None)
        on = bool(path) and path in self.library.favs
        surf = self._thumbs["btn_on" if on else "btn_off"]
        tip = ("Remove from Favorites" if on else "Add to Favorites") + "  (Ctrl+D)"
        for b in (self.fav_btn, self.mini.fav_btn):
            if surf is not None:
                b.get_image().set_from_surface(surf)
            else:
                b.set_label("♥" if on else "♡")
            b.set_tooltip_text(tip)
            b.set_sensitive(bool(path))

    def set_favorite(self, paths, on):
        paths = [p for p in paths if p in self.library.tracks]
        if not paths:
            return
        self.library.set_favorite(paths, on)
        self.update_fav_buttons()
        if self.mode == "favorites":
            self.refresh_songs(keep_scroll=True)
        else:
            self.song_view.queue_draw()

    def toggle_fav_current(self):
        path = self.current_path or (self._resume[0] if self._resume else None)
        if path:
            self.set_favorite([path], path not in self.library.favs)

    def export_favorites(self):
        favs = [p for _a, p in sorted((a, p) for p, a in self.library.favs.items())]
        self.export_m3u(favs, "player163 Favorites")

    def set_compact(self, on):
        self.cfg["compact"] = bool(on)
        if on:
            if self.get_visible():
                self._main_pos = self.get_position()
            self.mini.show_mini()
            self.hide()
        else:
            self.mini.remember_pos()
            self.mini.hide()
            if self._main_pos:
                self.move(*self._main_pos)
            self.present()

    # ---------------------------------------------------------------- settings, background, tray
    def set_cfg(self, key, value):
        self.cfg[key] = value
        if key in ("resume_long", "long_minutes"):
            self._update_speed_btn()
        save_config(self.cfg)

    def show_settings(self, page=None):
        if self.settings_dialog is None:
            self.settings_dialog = SettingsDialog(self)
            self.settings_dialog.connect("destroy", lambda *_: setattr(self, "settings_dialog", None))
            self.settings_dialog.show_all()
        if page:
            self.settings_dialog.stack.set_visible_child_name(page)
        self.settings_dialog.present()

    def set_watch(self, on):
        self.cfg["watch"] = bool(on)
        if on:
            self.start_scan(quiet=True)   # collects the folders to watch
        else:
            self._watch_dirs([])

    def set_tray(self, on):
        self.cfg["tray"] = bool(on)
        self.tray.set_visible(on)

    def apply_text_scale(self, scale):
        self.cfg["text_scale"] = scale
        settings = Gtk.Settings.get_default()
        if self._base_font is None:
            self._base_font = settings.props.gtk_font_name or "Sans 10"
        fd = Pango.FontDescription(self._base_font)
        size = fd.get_size() / Pango.SCALE or 10
        fd.set_size(int(round(size * scale * Pango.SCALE)))
        settings.props.gtk_font_name = fd.to_string()

    def clear_downloaded_pictures(self):
        n = 0
        for d in (COVER_DIR,):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for fn in names:
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                        src = json.load(f).get("source")
                except Exception:
                    src = None
                if src == "custom":
                    continue
                for ext in (".json", ".png", ".img"):
                    try:
                        os.remove(os.path.join(d, fn[:-5] + ext))
                        n += ext != ".json"
                    except OSError:
                        pass
        self._cover_tried.clear()
        self._clear_thumbs()
        self.refresh_all(keep_scroll=True)
        if self.current_path:
            self._show_now_playing(self.current_path)
        self.flash_status("Removed %d downloaded cover%s." % (n, "" if n == 1 else "s"))

    def toggle_visible(self):
        win = self.mini if self.cfg.get("compact") else self
        if win.get_visible() and win.is_active():
            win.hide()
        else:
            self.raise_ui()

    def close_window(self, win):
        """Closing while music plays keeps it playing in the background (if that's on)."""
        if self.cfg.get("background", True) and self.engine.state == "playing":
            if win is self.mini:
                self.mini.remember_pos()
            else:
                self.save_state()
            win.hide()
            if not self.cfg.get("bg_hint_shown"):
                self.cfg["bg_hint_shown"] = True
                save_config(self.cfg)
                self._notify("player163 keeps playing",
                             "Click the tray icon or open player163 again to bring it back. Quit with Ctrl+Q.",
                             None, "bg-hint")
            return True
        self.app.quit_app()
        return True

    def _notify(self, title, body, icon_file, nid="now-playing"):
        try:
            n = Gio.Notification.new(esc(title))   # some notification servers read markup
            n.set_body(esc(body))
            if icon_file:
                n.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(icon_file)))
            else:
                n.set_icon(Gio.ThemedIcon.new("player163"))
            n.set_default_action("app.show")
            self.app.send_notification(nid, n)
        except Exception as e:
            print("player163: notification:", e, file=sys.stderr)

    def _notify_song(self, t, data):
        if not self.cfg.get("notify", True) or self.is_active() or self.mini.is_active():
            return
        icon = None
        if data:
            try:
                pb = pixbuf_from_bytes(data, 128)
                if pb is not None:
                    os.makedirs(THUMB_DIR, exist_ok=True)
                    icon = os.path.join(THUMB_DIR, "notify-%s.png" % hashlib.md5(
                        t["album_key"].encode("utf-8", "surrogateescape"), usedforsecurity=False).hexdigest())
                    if not os.path.exists(icon):
                        pb.savev(icon, "png", [], [])
            except Exception:
                icon = None
        self._notify(t["d_title"], "%s — %s" % (t["d_artist"], t["d_album"]), icon)

    def raise_ui(self):
        if self.cfg.get("compact"):
            if self.mini.get_visible():
                self.mini.present()
            else:
                self.mini.show_mini()
        else:
            self.present()

    def jump_to_current(self):
        if not self.current_path:
            return
        if self.current_path not in self.row_index:
            self.search.set_text("")
            self.group_sel = None
            self.refresh_all()
        i = self.row_index.get(self.current_path)
        if i is None:
            return
        path = Gtk.TreePath(i)
        self.song_view.set_cursor(path, None, False)
        self.song_view.scroll_to_cell(path, None, True, 0.4, 0)

    def open_paths(self, paths):
        files = []
        for p in paths:
            if os.path.isdir(p):
                for dp, dn, fns in os.walk(p):
                    dn.sort()
                    files += [os.path.join(dp, f) for f in sorted(fns) if os.path.splitext(f)[1].lower() in AUDIO_EXTS]
            elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in AUDIO_EXTS:
                files.append(p)
        if not files:
            if paths:
                self.flash_status("That isn't a music file player163 can play.", 5)
            return
        for f in files:
            if f not in self.library.tracks and f not in self.extra:
                self.extra[f] = prepare(track_from_file(f))
        if self.stack.get_visible_child_name() == "empty" and not self.folders():
            self.np_sub.set_text("")
        pos, path = self.queue.set(files, 0)
        self._play(pos, path)

    # ---------------------------------------------------------------- list interaction
    def _on_song_activated(self, view, path, col):
        self.play_visible(self.song_store[path][0])

    def _on_song_button(self, view, ev):
        if ev.type == Gdk.EventType.BUTTON_PRESS and ev.button == 1:
            hit = view.get_path_at_pos(int(ev.x), int(ev.y))
            if hit and hit[1] is self.fav_col:
                p = self.song_store[hit[0]][0]
                self.set_favorite([p], p not in self.library.favs)
                return True
            return False
        if ev.type != Gdk.EventType.BUTTON_PRESS or ev.button != 3:
            return False
        hit = view.get_path_at_pos(int(ev.x), int(ev.y))
        if not hit:
            return True
        sel = view.get_selection()
        if not sel.path_is_selected(hit[0]):
            sel.unselect_all()
            sel.select_path(hit[0])
        paths = self.selected_paths()
        n = len(paths)
        menu = Gtk.Menu()
        self._menu_item(menu, "Play", lambda: self.play_visible(paths[0]))
        if n == 1 and self.is_long(paths[0]) and paths[0] in self.library.resume:
            self._menu_item(menu, "Play from Start  (saved at %s)" % fmt_time(self.library.resume[paths[0]]),
                            lambda: self.play_visible(paths[0], from_start=True))
        self._menu_item(menu, "Play Next" + ("" if n == 1 else " (%d)" % n), lambda: self.queue_paths(paths, True))
        self._menu_item(menu, "Add to Queue" + ("" if n == 1 else " (%d)" % n), lambda: self.queue_paths(paths, False))
        if self.queue.upnext:
            self._menu_item(menu, "Clear Up Next (%d)" % len(self.queue.upnext), self.clear_queue)
        menu.append(Gtk.SeparatorMenuItem())
        plmenu = Gtk.Menu()
        for pid, pl in sorted(self.library.playlists.items(), key=lambda kv: kv[1]["name"].casefold()):
            self._menu_item(plmenu, pl["name"], lambda pid=pid: self.add_to_playlist(pid, paths))
        if self.library.playlists:
            plmenu.append(Gtk.SeparatorMenuItem())
        self._menu_item(plmenu, "New Playlist…", lambda: self.new_playlist(paths))
        pli = Gtk.MenuItem(label="Add to Playlist")
        pli.set_submenu(plmenu)
        menu.append(pli)
        if self._playlist_id() is not None:
            editable = not self.sort_col and not self.search.get_text().strip()
            self._menu_item(menu, "Remove from Playlist" + ("" if n == 1 else " (%d)" % n), lambda: self.remove_from_playlist(paths))
            if editable:
                mv = Gtk.Menu()
                for label, where in (("Move Up  (Alt+↑)", "up"), ("Move Down  (Alt+↓)", "down"),
                                     ("Move to Top", "top"), ("Move to Bottom", "bottom")):
                    self._menu_item(mv, label, lambda w=where: self.move_in_playlist(paths, w))
                mvi = Gtk.MenuItem(label="Move")
                mvi.set_submenu(mv)
                menu.append(mvi)
        all_fav = all(p in self.library.favs for p in paths)
        self._menu_item(menu, ("Remove from Favorites" if all_fav else "Add to Favorites") + ("" if n == 1 else " (%d)" % n),
                        lambda: self.set_favorite(paths, not all_fav))
        self._menu_item(menu, "Edit Tags…" if n == 1 else "Edit Tags of %d Songs…" % n, self.edit_selected)
        self._menu_item(menu, "Open Containing Folder", lambda: self._open_folder(paths[0]))
        self._popup(menu, ev, view)
        return True

    def _on_group_button(self, view, ev):
        if ev.type != Gdk.EventType.BUTTON_PRESS or ev.button != 3:
            return False
        hit = view.get_path_at_pos(int(ev.x), int(ev.y))
        if not hit:
            return True
        view.get_selection().select_path(hit[0])
        key = self.group_store[hit[0]][0]
        menu = Gtk.Menu()
        self._menu_item(menu, "Play", lambda: self.play_visible(self.visible_paths[0] if self.visible_paths else None))
        if self.mode == "playlists":
            group = list(self.visible_paths)
            self._menu_item(menu, "Play Next", lambda: self.queue_paths(group, True))
            self._menu_item(menu, "Add to Queue", lambda: self.queue_paths(group, False))
            menu.append(Gtk.SeparatorMenuItem())
            name = self._playlist_name(key)
            if key.startswith("pl:"):
                pid = int(key[3:])
                self._menu_item(menu, "Rename Playlist…", lambda: self.rename_playlist(pid))
                self._menu_item(menu, "Export as .m3u…", lambda: self.export_m3u(group, name))
                self._menu_item(menu, "Delete Playlist…", lambda: self.delete_playlist(pid))
            else:
                self._menu_item(menu, "Save as Playlist…", lambda: self.new_playlist(
                    group, ask_text(self, "Save as Playlist", "Name your playlist:", name, ok_label="Save")))
                self._menu_item(menu, "Export as .m3u…", lambda: self.export_m3u(group, name))
            menu.append(Gtk.SeparatorMenuItem())
            self._menu_item(menu, "New Playlist…", lambda: self.new_playlist())
            self._menu_item(menu, "Import Playlist…", self.import_playlist)
            self._popup(menu, ev, view)
            return True
        if key != ALL_KEY:
            group = list(self.visible_paths)
            self._menu_item(menu, "Play Next", lambda: self.queue_paths(group, True))
            self._menu_item(menu, "Add to Queue", lambda: self.queue_paths(group, False))
            menu.append(Gtk.SeparatorMenuItem())
            if self.mode == "artists":
                self._menu_item(menu, "Rename Artist…", lambda: self.rename_artist(key))
                menu.append(Gtk.SeparatorMenuItem())
            else:
                self._menu_item(menu, "Rename Album…", lambda: self.rename_album(key))
            self._menu_item(menu, "Edit Tags of All %d Songs…" % len(self.visible_paths),
                            lambda: self.edit_tracks([self.library.tracks[p] for p in self.visible_paths]))
            if self.mode == "albums":
                self._cover_menu_items(menu, key)
        self._popup(menu, ev, view)
        return True

    def _cover_menu_items(self, menu, key):
        menu.append(Gtk.SeparatorMenuItem())
        self._menu_item(menu, "Set Album Cover…", lambda: self.choose_album_cover(key))
        meta = cover_meta(key) or {}
        if meta.get("source") == "custom":
            self._menu_item(menu, "Forget My Cover", lambda: self.remove_album_cover(key))
        elif meta.get("source") == "caa":
            page = meta.get("page", "")
            if page.startswith("https://musicbrainz.org/"):
                self._menu_item(menu, "About This Cover", lambda: Gio.AppInfo.launch_default_for_uri(page, None))
            self._menu_item(menu, "Remove Downloaded Cover", lambda: self._hide_online_cover(key))
        elif key.partition("\x1f")[0].strip():
            self._menu_item(menu, "Find Cover Online", lambda: self.find_cover_online(key))

    def _hide_online_cover(self, key):
        save_stored_cover(key, None, {"source": "none", "fetched": time.time() + 3650 * 86400})
        self._cover_changed(key)

    def _menu_item(self, menu, label, cb):
        it = Gtk.MenuItem(label=label)
        it.connect("activate", lambda *_: cb())
        menu.append(it)

    def _popup(self, menu, ev, widget):
        menu.show_all()
        menu.attach_to_widget(widget, None)
        self._menu = menu
        menu.popup_at_pointer(ev)

    def _open_folder(self, path):
        try:
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(os.path.dirname(path), None), None)
        except Exception as e:
            self.show_error("Couldn't open folder: %s" % e)

    # ---------------------------------------------------------------- tag editing
    def _suggestions(self):
        arts, albs, gens = set(), set(), set()
        for t in self.library.tracks.values():
            if t["artist"]:
                arts.add(t["artist"])
            if t["albumartist"]:
                arts.add(t["albumartist"])
            if t["album"]:
                albs.add(t["album"])
            if t["genre"]:
                gens.add(t["genre"])
        a = sorted(arts, key=sort_key)
        return {"artist": a, "albumartist": a, "album": sorted(albs, key=sort_key), "genre": sorted(gens, key=str.casefold)}

    def edit_selected(self):
        tracks = [self.get_track(p) for p in self.selected_paths()]
        tracks = [t for t in tracks if t]
        if tracks:
            self.edit_tracks(tracks)

    def edit_tracks(self, tracks):
        if not tracks:
            return
        if self._writing:
            self.show_error("Still saving the previous change — try again in a moment.")
            return
        dlg = TagDialog(self, tracks, self._suggestions())
        changes = None
        while True:
            r = dlg.run()
            if r != Gtk.ResponseType.OK:
                break
            if dlg.validate():
                changes = dlg.get_changes()
                break
        dlg.destroy()
        if changes:
            self._write_jobs([(t["path"], changes) for t in tracks])

    def rename_artist(self, key):
        old = "" if key == UNKNOWN_ARTIST else key
        n = sum(1 for t in self.library.tracks.values() if t["d_artist"] == key)
        new = ask_text(self, "Rename Artist",
                       "Change the artist on <b>%d</b> song%s. This updates the files' tags.\n"
                       "Tip: pick an existing name to merge them." % (n, "" if n == 1 else "s"),
                       old, self._suggestions()["artist"])
        if new is None or new == old or not new:
            return
        jobs = []
        for t in self.library.tracks.values():
            ch = {}
            if t["d_artist"] == key:
                ch["artist"] = new
            if old and t["albumartist"] == old:
                ch["albumartist"] = new
            if ch:
                jobs.append((t["path"], ch))
        self.group_sel = new
        self._write_jobs(jobs, label="Renamed “%s” to “%s”" % (key, new))

    def rename_album(self, key):
        tracks = [t for t in self.library.tracks.values() if t["album_key"] == key]
        if not tracks:
            return
        old = tracks[0]["album"]
        new = ask_text(self, "Rename Album",
                       "Change the album name on <b>%d</b> song%s by %s. This updates the files' tags."
                       % (len(tracks), "" if len(tracks) == 1 else "s", esc(tracks[0]["d_albumartist"])),
                       old, self._suggestions()["album"])
        if new is None or new == old or not new:
            return
        self.group_sel = new + "\x1f" + (tracks[0]["albumartist"] or tracks[0]["artist"])
        move = (stored_cover(key), cover_meta(key))
        new_key = new + "\x1f" + (tracks[0]["albumartist"] or tracks[0]["artist"])
        covers = {}
        if move[0] or move[1]:   # a cover you set or found online follows the renamed album
            covers = {key: move, new_key: (stored_cover(new_key), cover_meta(new_key))}
            save_stored_cover(new_key, move[0], move[1])
            save_stored_cover(key, None, None)
        self._write_jobs([(t["path"], {"album": new}) for t in tracks],
                         label="Renamed album to “%s”" % new, covers=covers)

    def _write_jobs(self, jobs, label=None, undo=False, covers=None):
        """jobs: (path, {tag: value}) or (path, {tag: value}, cover_op) with cover_op ("set", data, mime)
        or ("remove",). Remembers the old values so the change can be undone."""
        if not jobs:
            return
        self._writing = True
        self._ignore_fs_until = time.monotonic() + 3600
        self._update_status(text="Saving tags to {:,} file{}…".format(len(jobs), "" if len(jobs) == 1 else "s"))

        def work():
            done, errors, back = [], [], []
            for job in jobs:
                path, ch = job[0], job[1]
                cop = job[2] if len(job) > 2 else None
                try:
                    old_tags = read_tags(path)[0] if ch else {}
                    old_cover = read_embedded_cover(path) if cop else None
                    if ch:
                        write_tags(path, ch)
                    wrote_cover = False
                    if cop:
                        if cop[0] == "set":
                            wrote_cover = embed_cover(path, cop[1], cop[2])
                        else:
                            wrote_cover = embed_cover(path, None)
                    rev_cover = None
                    if wrote_cover:
                        rev_cover = ("set", old_cover, _image_mime(old_cover)) if old_cover else ("remove",)
                    back.append((path, {k: old_tags.get(k, "") for k in ch}, rev_cover))
                except Exception as e:
                    errors.append((path, str(e) or e.__class__.__name__))
                done.append(track_from_file(path))
            GLib.idle_add(self._write_done, done, errors, back, label, undo, covers)

        threading.Thread(target=work, daemon=True).start()

    def _write_done(self, tracks, errors, back=(), label=None, undo=False, covers=None):
        self._writing = False
        self._ignore_fs_until = time.monotonic() + 4
        self._clear_thumbs()
        lib = [t for t in tracks if t["path"] in self.library.tracks]
        self.library.upsert(lib)
        for t in tracks:
            if t["path"] in self.extra:
                self.extra[t["path"]] = prepare(t)
        self.refresh_all(keep_scroll=True)
        if self.current_path and any(t["path"] == self.current_path for t in tracks):
            self._show_now_playing(self.current_path)
            self.mpris.changed("Metadata")
        if errors:
            lines = "\n".join("• %s — %s" % (os.path.basename(p), e) for p, e in errors[:8])
            more = "\n…and %d more" % (len(errors) - 8) if len(errors) > 8 else ""
            self.show_error("Couldn't save tags for %d file%s (read-only or unsupported):\n%s%s"
                            % (len(errors), "" if len(errors) == 1 else "s", lines, more))
        back = [b for b in back if b[1] or b[2]]
        if undo:
            self._undo = None
            self.undo_bar.hide()
            self.flash_status("Undone.")
        elif back or covers:
            n = len(tracks)
            self._undo = {"jobs": back, "covers": covers or {}}
            text = label or "Saved changes to {:,} song{}".format(n, "" if n == 1 else "s")
            self.undo_label.set_text(text + ".")
            self.undo_bar.show()
            if self._undo_hide_id:
                GLib.source_remove(self._undo_hide_id)
            self._undo_hide_id = GLib.timeout_add_seconds(20, self._hide_undo_bar)
        self._sync_undo_action()
        return False

    def _hide_undo_bar(self):
        self._undo_hide_id = 0
        self.undo_bar.hide()
        return False

    def _sync_undo_action(self):
        act = self.lookup_action("undo")
        if act is not None:
            act.set_enabled(bool(self._undo) and not self._writing)

    def undo_last(self):
        u = self._undo
        if not u or self._writing:
            return
        for key, (data, meta) in u["covers"].items():
            save_stored_cover(key, data, meta)
            self._cover_changed(key)
        if self._undo_hide_id:
            GLib.source_remove(self._undo_hide_id)
            self._undo_hide_id = 0
        self.undo_bar.hide()
        jobs = [(p, ch, c) if c else (p, ch) for p, ch, c in u["jobs"]]
        if jobs:
            self._write_jobs(jobs, undo=True)
        else:
            self._undo = None
            self._sync_undo_action()
            self.flash_status("Undone.")

    # ---------------------------------------------------------------- misc
    def show_error(self, text):
        self.err_label.set_text(text)
        self.err_bar.show()

    def _on_key(self, w, ev):
        focus = self.get_focus()
        editing = isinstance(focus, Gtk.Editable)
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(ev.state & Gdk.ModifierType.MOD1_MASK)
        k = ev.keyval
        if editing:
            if k == Gdk.KEY_Escape and focus is self.search:
                self.search.set_text("")
                self.song_view.grab_focus()
                return True
            return False
        if k == Gdk.KEY_space and not ctrl and not alt:
            self.play_pause()
            return True
        if self._playlist_id() is not None and focus is self.song_view:
            if k == Gdk.KEY_Delete:
                self.remove_from_playlist(self.selected_paths())
                return True
            if alt and k in (Gdk.KEY_Up, Gdk.KEY_Down):
                self.move_in_playlist(self.selected_paths(), "up" if k == Gdk.KEY_Up else "down")
                return True
        if ctrl and not alt:
            if k == Gdk.KEY_Right:
                self.next_track()
                return True
            if k == Gdk.KEY_Left:
                self.prev_track()
                return True
            if k == Gdk.KEY_Up:
                self.vol.set_value(min(1.0, self.vol.get_value() + 0.05))
                return True
            if k == Gdk.KEY_Down:
                self.vol.set_value(max(0.0, self.vol.get_value() - 0.05))
                return True
        if k in (Gdk.KEY_AudioPlay, Gdk.KEY_AudioPause):
            self.play_pause()
            return True
        if k == Gdk.KEY_AudioNext:
            self.next_track()
            return True
        if k == Gdk.KEY_AudioPrev:
            self.prev_track()
            return True
        # type anywhere to search (unless the sidebar's own type-ahead is in use)
        if not ctrl and not alt and focus is not self.group_view:
            ch = Gdk.keyval_to_unicode(k)
            if ch and chr(ch).isprintable() and not chr(ch).isspace():
                if self.search.handle_event(ev):
                    self.search.grab_focus_without_selecting()
                    return True
        return False

    def show_shortcuts(self):
        rows = (("Space", "Play / pause"), ("Ctrl + → / ←", "Next / previous song"),
                ("Ctrl + ↑ / ↓", "Volume up / down"), ("Ctrl + S", "Shuffle on / off"),
                ("Ctrl + R", "Repeat: off → all → one"), ("Ctrl + 1 / 2 / 3", "Songs / Artists / Albums"),
                ("Just type", "Search"), ("Esc", "Clear search"), ("Ctrl + E  or  F2", "Edit tags of selected songs"),
                ("Ctrl + D", "👍 Favorite the playing song"), ("Ctrl + 4", "Favorites"),
                ("Ctrl + Shift + E", "Equalizer"), ("Right-click a song", "Play Next / Add to Queue"),
                ("Ctrl + J", "Jump to playing song"), ("Ctrl + M", "Mini player ⇄ library"),
                ("Ctrl + 5", "Playlists and smart lists"), ("Ctrl + U", "Show / hide Up Next"),
                ("Ctrl + Z", "Undo the last tag or cover change"), ("Ctrl + ,", "Preferences"),
                ("Ctrl + O", "Add a music folder"), ("Drop an image on an album", "Set its cover"),
                ("F5", "Rescan folder"), ("Right-click", "Edit tags, rename artist / album"),
                ("Ctrl + Q", "Quit"))
        d = Gtk.Dialog(title="Keyboard Shortcuts", transient_for=self, modal=True, use_header_bar=True)
        grid = Gtk.Grid(column_spacing=24, row_spacing=6, margin=18)
        for i, (k, desc) in enumerate(rows):
            kl = Gtk.Label(xalign=1)
            kl.set_markup("<b>%s</b>" % esc(k))
            grid.attach(kl, 0, i, 1, 1)
            grid.attach(Gtk.Label(label=desc, xalign=0), 1, i, 1, 1)
        d.get_content_area().add(grid)
        d.show_all()
        d.run()
        d.destroy()

    def show_about(self):
        a = Gtk.AboutDialog(transient_for=self, modal=True, program_name=APP_NAME, version=VERSION,
                            logo=logo_pixbuf(96),
                            comments="A small, no-fuss music player.\nGTK + GStreamer + mutagen.",
                            website="https://ivansamokish.com", website_label="ivansamokish.com",
                            copyright="© 2026 Ivan Samokish", authors=["Ivan Samokish"],
                            license_type=Gtk.License.MIT_X11)
        a.run()
        a.destroy()

    def _scroll_to_path(self, path):
        i = self.row_index.get(path)
        if i is not None:
            self.song_view.scroll_to_cell(Gtk.TreePath(i), None, True, 0.4, 0)
        return False

    def save_state(self):
        if not self.is_maximized():
            w, h = self.get_size()
            self.cfg["width"], self.cfg["height"] = w, h
            pos = self.get_position() if self.get_visible() else self._main_pos
            if pos:
                self.cfg["x"], self.cfg["y"] = int(pos[0]), int(pos[1])
        self.cfg["maximized"] = self.is_maximized()
        self.cfg["pane"] = self.paned.get_position() if self.sidebar.get_visible() else self.cfg.get("pane", 250)
        for sid, col in self.sort_columns.items():
            if sid != "title":
                self.cfg["col_" + sid] = col.get_width()
        if self.current_path and self.current_path in self.library.tracks:
            self.cfg["last_path"] = self.current_path
            self.cfg["last_pos"] = round(self.engine.position(), 1) if self.engine.state != "stopped" else 0
        elif self._resume:
            self.cfg["last_path"], self.cfg["last_pos"] = self._resume
        self._save_long_position()
        self.mini.remember_pos()
        save_config(self.cfg)

    def _on_delete(self, *a):
        return self.close_window(self)


# ----------------------------------------------------------------------------------------
# Tray icon
# ----------------------------------------------------------------------------------------

class TrayIcon:
    """Status icon: click shows/hides player163, right-click for controls. XApp on Mint, else Gtk.StatusIcon."""

    def __init__(self, main):
        self.main = main
        self.icon = None
        self.menu = Gtk.Menu()
        self.play_item = Gtk.MenuItem(label="Play")
        self.play_item.connect("activate", lambda *_: main.play_pause())
        self.menu.append(self.play_item)
        for label, cb in (("Next", main.next_track), ("Previous", main.prev_track)):
            it = Gtk.MenuItem(label=label)
            it.connect("activate", lambda _i, c=cb: c())
            self.menu.append(it)
        self.menu.append(Gtk.SeparatorMenuItem())
        for label, cb in (("Show player163", main.raise_ui), ("Quit", main.app.quit_app)):
            it = Gtk.MenuItem(label=label)
            it.connect("activate", lambda _i, c=cb: c())
            self.menu.append(it)
        self.menu.show_all()

    def set_visible(self, on):
        if on and self.icon is None:
            try:
                if XApp is not None:
                    self.icon = XApp.StatusIcon()
                    self.icon.set_icon_name("player163")
                    self.icon.set_name("player163")
                    self.icon.set_secondary_menu(self.menu)
                    self.icon.connect("activate", lambda *a: self.main.toggle_visible())
                else:
                    with warnings_off():
                        self.icon = Gtk.StatusIcon.new_from_icon_name("player163")
                    self.icon.connect("activate", lambda *a: self.main.toggle_visible())
                    self.icon.connect("popup-menu", lambda i, b, t: self.menu.popup(None, None, None, None, b, t))
            except Exception as e:
                print("player163: no tray icon:", e, file=sys.stderr)
                self.icon = None
        if self.icon is not None:
            self.icon.set_visible(bool(on))
        self.update()

    def available(self):
        return self.icon is not None

    def update(self):
        m = self.main
        playing = m.engine.state == "playing"
        self.play_item.set_label("Pause" if playing else "Play")
        if self.icon is None:
            return
        t = m.get_track(m.current_path) if m.current_path else None
        tip = "%s — %s" % (t["d_title"], t["d_artist"]) if t else APP_NAME
        try:
            self.icon.set_tooltip_text(tip)
        except Exception:
            pass


class warnings_off:
    def __enter__(self):
        import warnings
        self._cm = warnings.catch_warnings()
        self._cm.__enter__()
        warnings.simplefilter("ignore")

    def __exit__(self, *a):
        return self._cm.__exit__(*a)


# ----------------------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------------------

TEXT_SIZES = (("Small", 0.9), ("Normal", 1.0), ("Large", 1.15), ("Larger", 1.3))


class SettingsDialog(Gtk.Window):
    """Everything in one place; every change applies at once."""

    def __init__(self, main):
        Gtk.Window.__init__(self, title="Preferences", transient_for=main, modal=False)
        self.main = main
        self.set_default_size(560, 520)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.connect("key-press-event", lambda w, e: e.keyval == Gdk.KEY_Escape and (self.close() or True))
        hb = Gtk.HeaderBar(show_close_button=True)
        self.set_titlebar(hb)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        sw = Gtk.StackSwitcher(stack=self.stack)
        hb.set_custom_title(sw)
        self.add(self.stack)
        self.stack.add_titled(self._library_page(), "library", "Library")
        self.stack.add_titled(self._playback_page(), "playback", "Playback")
        self.stack.add_titled(self._appearance_page(), "appearance", "Appearance")
        self.stack.add_titled(self._online_page(), "online", "Online")

    # -- helpers
    def _page(self):
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=20)
        scroll.add(box)
        return scroll, box

    def _heading(self, box, text):
        lab = Gtk.Label(xalign=0, margin_top=10)
        lab.set_markup("<b>%s</b>" % esc(text))
        box.pack_start(lab, False, False, 0)

    def _row(self, box, title, sub, widget):
        row = Gtk.Box(spacing=12, margin_top=4, margin_bottom=4)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        t = Gtk.Label(label=title, xalign=0, wrap=True)
        texts.pack_start(t, False, False, 0)
        if sub:
            s2 = Gtk.Label(label=sub, xalign=0, wrap=True, max_width_chars=48)
            s2.get_style_context().add_class("dim-label")
            s2.set_markup("<small>%s</small>" % esc(sub))
            texts.pack_start(s2, False, False, 0)
        row.pack_start(texts, True, True, 0)
        widget.set_valign(Gtk.Align.CENTER)
        row.pack_end(widget, False, False, 0)
        box.pack_start(row, False, False, 0)
        return widget

    def _switch(self, box, title, sub, active, cb):
        sw = Gtk.Switch(active=bool(active))
        sw.connect("notify::active", lambda w, _p: cb(w.get_active()))
        return self._row(box, title, sub, sw)

    def _combo(self, box, title, sub, items, active, cb):
        c = Gtk.ComboBoxText()
        for k, label in items:
            c.append(k, label)
        c.set_active_id(active)
        c.connect("changed", lambda w: w.get_active_id() is not None and cb(w.get_active_id()))
        return self._row(box, title, sub, c)

    # -- pages
    def _library_page(self):
        m = self.main
        page, box = self._page()
        self._heading(box, "Music folders")
        self.folder_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.folder_list.get_style_context().add_class("settings-list")
        box.pack_start(self.folder_list, False, False, 0)
        btns = Gtk.Box(spacing=8, margin_top=6)
        add = Gtk.Button(label="Add Folder…")
        add.connect("clicked", lambda *_: m.choose_folder())
        rescan = Gtk.Button(label="Rescan Now")
        rescan.connect("clicked", lambda *_: m.start_scan())
        btns.pack_start(add, False, False, 0)
        btns.pack_start(rescan, False, False, 0)
        box.pack_start(btns, False, False, 0)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_markup("<small>Tip: you can also drop folders or songs onto the player window.</small>")
        hint.get_style_context().add_class("dim-label")
        box.pack_start(hint, False, False, 0)
        self._heading(box, "Updates")
        self._switch(box, "Watch folders for new music", "New, changed or removed songs show up on their own.",
                     m.cfg.get("watch", True), m.set_watch)
        self.refresh_folders()
        return page

    def refresh_folders(self):
        for c in self.folder_list.get_children():
            self.folder_list.remove(c)
        folders = self.main.folders()
        if not folders:
            lab = Gtk.Label(label="No folders yet.", margin=12)
            lab.get_style_context().add_class("dim-label")
            self.folder_list.add(lab)
        for f in folders:
            row = Gtk.Box(spacing=8, margin=8)
            icon = Gtk.Image.new_from_icon_name("folder-music-symbolic" if os.path.isdir(f) else "dialog-warning-symbolic",
                                                Gtk.IconSize.BUTTON)
            row.pack_start(icon, False, False, 0)
            n = sum(1 for p in self.main.library.tracks if self.main._under(p, f))
            lab = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
            lab.set_markup("%s\n<small><span alpha='60%%'>%s</span></small>" % (
                esc(f), "{:,} songs".format(n) if os.path.isdir(f) else "not connected"))
            row.pack_start(lab, True, True, 0)
            rm = Gtk.Button.new_from_icon_name("list-remove-symbolic", Gtk.IconSize.BUTTON)
            rm.set_relief(Gtk.ReliefStyle.NONE)
            rm.set_tooltip_text("Remove from library (your files stay where they are)")
            rm.connect("clicked", lambda _b, path=f: self.main.remove_folder(path))
            row.pack_end(rm, False, False, 0)
            self.folder_list.add(row)
        self.folder_list.show_all()

    def _playback_page(self):
        m = self.main
        page, box = self._page()
        self._heading(box, "Sound")
        self._combo(box, "Volume leveling", "Plays every song at a similar loudness (ReplayGain).",
                    (("off", "Off"), ("track", "Per song"), ("album", "Per album")), m.engine.rg_mode,
                    lambda v: m.lookup_action("replaygain").activate(GLib.Variant("s", v)))
        eq = Gtk.Button(label="Open…")
        eq.connect("clicked", lambda *_: m.show_equalizer())
        self._row(box, "Equalizer", "Ten bands with presets.", eq)
        self._heading(box, "Audiobooks and podcasts")
        self._switch(box, "Remember where I stopped in long files", "They pick up where you left off.",
                     m.cfg.get("resume_long", True), lambda v: m.set_cfg("resume_long", v))
        cur = int(float(m.cfg.get("long_minutes", 20)))
        opts = sorted({10, 15, 20, 30, 45, 60, cur})
        self._combo(box, "Count a file as long after", None, [(str(v), "%d minutes" % v) for v in opts], str(cur),
                    lambda v: m.set_cfg("long_minutes", int(v)))
        self._combo(box, "Speed for long files", "Voices keep their normal pitch.",
                    [("%g" % v, "%g×" % v) for v in sorted(SPEEDS)], "%g" % float(m.cfg.get("long_speed", 1.0)),
                    lambda v: m.set_long_speed(float(v)))
        self._heading(box, "Window")
        self._switch(box, "Keep playing when the window is closed", "Reopen from the tray icon or the menu.",
                     m.cfg.get("background", True), lambda v: m.set_cfg("background", v))
        self._switch(box, "Show tray icon", None, m.cfg.get("tray", True), m.set_tray)
        self._switch(box, "Notify when the song changes", "Only while the player window isn't in front.",
                     m.cfg.get("notify", True), lambda v: m.set_cfg("notify", v))
        return page

    def _appearance_page(self):
        m = self.main
        page, box = self._page()
        self._heading(box, "Look")
        self._combo(box, "Theme", None, (("dark", "Dark"), ("light", "Light")), m.scheme,
                    lambda v: m.lookup_action("theme").activate(GLib.Variant("s", v)))
        self._combo(box, "Player controls", "Bottom bar, or a tall panel with big artwork on the side.",
                    (("bottom", "Bottom"), ("left", "Left side"), ("right", "Right side")),
                    m.cfg.get("player_pos", "bottom"), m.apply_player_position)
        self._combo(box, "Text size", None, [("%g" % v, k) for k, v in TEXT_SIZES],
                    "%g" % float(m.cfg.get("text_scale", 1.0)), lambda v: m.apply_text_scale(float(v)))
        self._combo(box, "Albums view", None, (("list", "List"), ("grid", "Cover grid")), m.album_layout,
                    lambda v: m.layout_btns[v].set_active(True))
        return page

    def _online_page(self):
        m = self.main
        page, box = self._page()
        self._heading(box, "Album covers from the internet")
        self._switch(box, "Missing album covers", "From the Cover Art Archive, found through MusicBrainz. "
                     "Your files aren't changed.", m._covers_online(), m.set_album_covers)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda *_: m.clear_downloaded_pictures())
        self._row(box, "Downloaded covers", "Removes the covers player163 downloaded. "
                  "Covers you set yourself stay.", clear)
        note = Gtk.Label(xalign=0, wrap=True, margin_top=12)
        note.set_markup("<small>Only album and artist names are sent, one request per second. Nothing is "
                        "looked up while this is off.</small>")
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)
        return page


# ----------------------------------------------------------------------------------------
# Application
# ----------------------------------------------------------------------------------------


class App(Gtk.Application):
    def __init__(self):
        Gtk.Application.__init__(self, application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.win = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        Gst.init(None)
        Gtk.Window.set_default_icon_name("player163")
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit_app())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        show_action = Gio.SimpleAction.new("show", None)
        show_action.connect("activate", lambda *_: self.win and self.win.raise_ui())
        self.add_action(show_action)
        try:  # save settings when the session ends / Ctrl+C in a terminal
            import signal
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda *_: (self.quit_app(), False)[1])
        except Exception:
            pass

    def quit_app(self):
        if self.win:
            self.win.save_state()
            self.win.engine.stop()
        self.quit()

    def do_activate(self):
        if mutagen is None:
            d = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                  text="player163 needs python3-mutagen",
                                  secondary_text="Install it with:  sudo apt install python3-mutagen")
            d.run()
            d.destroy()
            return
        if self.win is None:
            self.win = MainWindow(self)
        self.win.raise_ui()

    def do_open(self, files, n_files, hint):
        self.do_activate()
        if self.win:
            self.win.open_paths([f.get_path() for f in files if f.get_path()])


def main():
    GLib.set_prgname("player163")
    GLib.set_application_name(APP_NAME)
    Gst.init(None)
    app = App()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
