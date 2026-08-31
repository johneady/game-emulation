#!/usr/bin/env python3
"""
Admin service for the retro games launcher.

Extends the original box-art service with two more jobs:

  * uploading games   - DOS folders arrive as a zip and are repackaged into a
                        js-dos bundle; console ROMs are dropped into /opt/roms
                        and consoles.json is rebuilt. A PS1 game may arrive as
                        a .7z holding a .cue and its .bin tracks - it is kept
                        as-is, because EmulatorJS boots such archives directly.
  * hiding games      - a hidden-id overlay, so hiding survives scan-roms
                        regenerating consoles.json and never deletes a file.

Large PS1 discs are uploaded in chunks: the browser slices the file, each chunk
is appended to a staging file, and a final commit call installs it. That keeps
memory flat and lets a 450 MB disc report real progress.

LAN-only, no auth, by deployment choice.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

ART_DIR = "/opt/art"
WEB = "/var/www/dosgames"
BUNDLES = os.path.join(WEB, "games")
MANIFEST = os.path.join(WEB, "games.json")
CONSOLES = os.path.join(WEB, "consoles.json")
HIDDEN = os.path.join(WEB, "hidden.json")
META = os.path.join(WEB, "meta.json")
DOS_SRC = "/opt/dosgames"
ROMS = "/opt/roms/Roms"
STAGING = "/opt/uploads"

MAX_ART_BYTES = 12 * 1024 * 1024
MAX_CHUNK_BYTES = 16 * 1024 * 1024      # one slice of a chunked upload
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
STAGING_TTL = 24 * 3600                 # abandoned part-files are swept after this
THUMB = (640, 480)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SESSION_RE = re.compile(r"^[a-z0-9]{8,64}$")
LISTEN = ("127.0.0.1", 8089)

# Console targets a ROM may be uploaded to. Extensions are the ones scan-roms
# already indexes, so an upload is visible after the rescan it triggers. The
# ps1 list adds .7z: not an image itself, but an archive of a cue and bins.
SYSTEMS = {
    "nes": {"folder": "NES Roms", "exts": (".nes",)},
    "ps1": {"folder": "PS1 Roms", "exts": (".chd", ".cue", ".pbp", ".iso", ".bin", ".7z")},
}

SEVENZ = "/usr/bin/7z"

# Writes to the JSON manifests are serialised; uploads are concurrent otherwise.
_manifest_lock = threading.Lock()


def safe_id(raw):
    gid = (raw or "").strip().lower()
    return gid if ID_RE.match(gid) else None


def slugify(raw, fallback="game"):
    s = re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-")[:48]
    return s or fallback


def read_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path, data):
    """Write via a temp file in the same dir so a reader never sees half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def safe_member(name):
    """Reject absolute paths and traversal before extracting a zip entry."""
    if name.startswith("/") or name.startswith("\\"):
        return False
    parts = re.split(r"[/\\]", name)
    return ".." not in parts


def find_executable(root):
    """
    Pick the program that starts a DOS game.

    A .bat is usually the author's own launcher and knows how to start the
    game (loading an interpreter, setting paths), so it wins. Otherwise prefer
    an executable whose stem matches the folder, then .exe over .com, ignoring
    installers, configurators and bundled interpreters.
    """
    SKIP = {"install", "setup", "config", "setsound", "uninstal", "readme", "instal"}
    # Bundled runtimes: these start a shell/prompt, not the game itself.
    RUNTIME = {"basica", "gwbasic", "basic", "qbasic", "runtime", "dos4gw", "dos32a"}
    cands = []
    for dirpath, _dirnames, filenames in os.walk(root):
        depth = os.path.relpath(dirpath, root).count(os.sep) if dirpath != root else 0
        if depth > 1:
            continue
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in (".exe", ".com", ".bat"):
                cands.append(os.path.relpath(os.path.join(dirpath, fn), root))
    if not cands:
        return None

    base = os.path.basename(root).lower()
    rank = {".exe": 0, ".com": 1, ".bat": 2}

    def score(rel):
        stem = os.path.splitext(os.path.basename(rel))[0].lower()
        ext = os.path.splitext(rel)[1].lower()
        return (
            1 if stem in RUNTIME else 0,      # never launch a bare interpreter
            1 if stem in SKIP else 0,         # nor an installer
            0 if ext == ".bat" else 1,        # the author's launcher knows best
            0 if stem == base else 1,
            rel.count(os.sep),
            rank.get(ext, 3),
            len(rel),
        )

    return sorted(cands, key=score)[0]


def rank_candidates(root):
    """
    Score every launchable file and report whether the best one is trustworthy.

    Returns (candidates, confident). Each candidate is a dict the UI can show.
    `confident` is False when the top two are close enough that guessing would
    be a coin-flip - the caller should ask the user instead of picking blind.
    """
    SKIP = {"install", "setup", "config", "setsound", "uninstal", "readme", "instal"}
    RUNTIME = {"basica", "gwbasic", "basic", "qbasic", "runtime", "dos4gw", "dos32a"}

    cands = []
    for dirpath, _dirnames, filenames in os.walk(root):
        depth = os.path.relpath(dirpath, root).count(os.sep) if dirpath != root else 0
        if depth > 1:
            continue
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in (".exe", ".com", ".bat"):
                cands.append(os.path.relpath(os.path.join(dirpath, fn), root))
    if not cands:
        return [], False

    base = os.path.basename(root).lower()
    rank = {".exe": 0, ".com": 1, ".bat": 2}

    def parts(rel):
        stem = os.path.splitext(os.path.basename(rel))[0].lower()
        ext = os.path.splitext(rel)[1].lower()
        return (
            1 if stem in RUNTIME else 0,
            1 if stem in SKIP else 0,
            0 if ext == ".bat" else 1,
            0 if stem == base else 1,
            rel.count(os.sep),
            rank.get(ext, 3),
            len(rel),
        )

    ordered = sorted(cands, key=parts)

    def why(rel):
        stem = os.path.splitext(os.path.basename(rel))[0].lower()
        ext = os.path.splitext(rel)[1].lower()
        if stem in RUNTIME:
            return "interpreter, not the game"
        if stem in SKIP:
            return "installer or setup tool"
        if ext == ".bat":
            return "batch launcher"
        if stem == base:
            return "matches the game name"
        return "program"

    out = []
    for rel in ordered:
        full = os.path.join(root, rel)
        out.append({
            "file": rel,
            "note": why(rel),
            "size": os.path.getsize(full) if os.path.exists(full) else 0,
        })

    # Confident when there is only one real option, or the winner is clearly
    # better than the runner-up on the meaningful criteria (skip/runtime
    # status, being a .bat launcher, or matching the folder name).
    if len(ordered) == 1:
        return out, True
    best, second = parts(ordered[0]), parts(ordered[1])
    return out, best[:4] != second[:4]


def build_jsdos(src_dir, gid, exe=None):
    """Zip a DOS game folder into <gid>.jsdos with a dosbox.conf that runs exe."""
    if exe:
        if not safe_member(exe) or not os.path.isfile(os.path.join(src_dir, exe)):
            raise ValueError("executable not found in upload: %s" % exe)
    else:
        exe = find_executable(src_dir)
        if not exe:
            raise ValueError("no .exe, .com or .bat found in the upload")

    # dosbox runs under DOS, which uses backslashes. Split on the POSIX path
    # first, since os.path cannot see DOS separators.
    sub, name = os.path.split(exe)
    # Change into the program's own directory before running it: games that
    # load data by relative name only find it from where they were installed.
    run = ("cd \\%s\n%s\n" % (sub.replace("/", "\\"), name)) if sub else ("%s\n" % name)
    conf = (
        "[sdl]\nautolock=true\n\n"
        "[cpu]\ncore=auto\ncputype=auto\ncycles=auto\n\n"
        "[autoexec]\nmount c .\nc:\n%s" % run
    )

    os.makedirs(BUNDLES, exist_ok=True)
    tmp_out = os.path.join(BUNDLES, ".%s.tmp" % gid)
    dst = os.path.join(BUNDLES, "%s.jsdos" % gid)
    try:
        with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(".jsdos/dosbox.conf", conf)
            for dirpath, _dirnames, filenames in os.walk(src_dir):
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, src_dir)
                    if rel.startswith(".jsdos"):
                        continue
                    zf.write(full, rel)
        os.replace(tmp_out, dst)
        os.chmod(dst, 0o644)
    except Exception:
        if os.path.exists(tmp_out):
            os.unlink(tmp_out)
        raise
    return exe


def unpack_dos_upload(archive, work):
    """
    Extract an uploaded zip and return the folder that actually holds the game.

    A zip made from a folder usually has a single top-level directory; when it
    does, that directory is the game root, otherwise the zip root is.
    """
    extract = os.path.join(work, "x")
    os.makedirs(extract, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.namelist() if safe_member(m)]
            if not members:
                raise ValueError("zip is empty or contains only unsafe paths")
            zf.extractall(extract, members=members)
    except zipfile.BadZipFile:
        raise ValueError("not a valid zip file")

    entries = [e for e in os.listdir(extract) if not e.startswith("__MACOSX")]
    if len(entries) == 1 and os.path.isdir(os.path.join(extract, entries[0])):
        inner = os.path.join(extract, entries[0])
        # Some games (F29 Retaliator, for one) hardcode their own directory as
        # an absolute DOS path like C:\RETAL\FILE. Flattening the wrapper folder
        # away breaks those, so keep it when the program asks for it by name.
        if wants_own_dir(inner, entries[0]):
            return extract
        return inner
    return extract


def wants_own_dir(game_dir, folder):
    """
    True when the game's executable references \\<folder>\\ as an absolute path.

    DOS games that were installed to a fixed directory look for their data at
    C:\\<folder>\\..., which only resolves if that folder still exists on the
    mounted drive.
    """
    needle = ("\\" + folder.upper()).encode("ascii", "ignore")
    if len(needle) < 3:
        return False
    for dirpath, _dirnames, filenames in os.walk(game_dir):
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in (".exe", ".com"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > 8 * 1024 * 1024:
                    continue
                with open(full, "rb") as fh:
                    blob = fh.read().upper()
            except OSError:
                continue
            if needle in blob:
                return True
    return False


class NeedsChoice(Exception):
    """Raised when the archive does not say which program starts the game."""

    def __init__(self, candidates, work, src_rel):
        super().__init__("choose a startup program")
        self.candidates = candidates
        self.work = work
        self.src_rel = src_rel


def install_dos(upload_path, meta):
    """Turn an uploaded zip into a playable bundle and a games.json entry."""
    gid = safe_id(meta.get("id")) or slugify(meta.get("title") or "")
    if not gid:
        raise ValueError("could not derive a game id; give the game a title")

    chosen = (meta.get("exe") or "").strip() or None
    work = tempfile.mkdtemp(prefix="dosup-", dir=STAGING)
    keep_work = False
    try:
        src = unpack_dos_upload(upload_path, work)

        # With no explicit choice, only auto-pick when the archive makes the
        # answer obvious. A zip holding several equally plausible programs
        # (episodes, a menu beside the game) is a coin-flip, so ask instead of
        # silently installing the wrong one.
        if not chosen:
            cands, confident = rank_candidates(src)
            if not cands:
                raise ValueError("no .exe, .com or .bat found in the upload")
            if not confident:
                keep_work = True
                raise NeedsChoice(cands, work, os.path.relpath(src, work))

        exe = build_jsdos(src, gid, chosen)
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)

    title = (meta.get("title") or "").strip() or gid
    sub = (meta.get("sub") or "").strip()
    with _manifest_lock:
        games = read_json(MANIFEST, [])
        games = [g for g in games if g.get("id") != gid]
        games.append({"id": gid, "title": title, "sub": sub})
        games.sort(key=lambda g: g.get("title", "").lower())
        write_json(MANIFEST, games)

    return {"ok": True, "kind": "dos", "id": gid, "title": title, "exe": exe}


def sevenzip_list(archive):
    """Paths inside a .7z, or None if the archive cannot be read."""
    try:
        proc = subprocess.run(
            [SEVENZ, "l", "-ba", "-slt", archive],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    names = []
    for line in proc.stdout.splitlines():
        if line.startswith("Path = "):
            names.append(line[7:].strip())
    return names


def install_rom_7z(upload_path, sysid, filename):
    """
    Keep an uploaded .7z as the ROM itself.

    EmulatorJS decompresses an archive game file in the browser and then boots
    the .cue inside it; a bare .cue URL would never fetch its .bin tracks
    (the core boots with no disc and drops to its menu). Keeping the archive
    also means generic track names ("Track 1.bin") cannot collide between
    games, and the compressed form halves what lands on disk.
    """
    entries = sevenzip_list(upload_path)
    if entries is None:
        raise ValueError("could not read the .7z (not a valid archive?)")

    exts = set()
    for e in entries:
        ext = os.path.splitext(e)[1].lower()
        if ext:
            exts.add(ext)
    if ".cue" not in exts:
        if not exts & {".chd", ".pbp", ".iso"}:
            raise ValueError("the .7z holds no .cue (with .bin), .chd, .pbp or .iso")
    elif ".bin" not in exts:
        raise ValueError("the .7z has a .cue but no .bin tracks")

    dest_dir = os.path.join(ROMS, SYSTEMS[sysid]["folder"])
    os.makedirs(dest_dir, exist_ok=True)
    dst = os.path.join(dest_dir, filename)
    if os.path.exists(dst):
        raise ValueError("a ROM named %s already exists" % filename)

    shutil.move(upload_path, dst)
    os.chmod(dst, 0o644)

    rescan()
    return {"ok": True, "kind": "rom", "system": sysid, "file": filename}


def install_rom(upload_path, meta):
    """Move an uploaded ROM into its system folder and rebuild consoles.json."""
    sysid = (meta.get("system") or "").strip().lower()
    if sysid not in SYSTEMS:
        raise ValueError("unknown system: %s" % (sysid or "(none)"))

    filename = os.path.basename((meta.get("filename") or "").replace("\\", "/")).strip()
    if not filename or filename.startswith("."):
        raise ValueError("bad filename")
    # Keep the original name: scan-roms derives title and region from it.
    filename = re.sub(r'[\x00-\x1f/:*?"<>|]', "", filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SYSTEMS[sysid]["exts"]:
        raise ValueError(
            "%s does not take %s files (expected %s)"
            % (sysid.upper(), ext or "extensionless", ", ".join(SYSTEMS[sysid]["exts"]))
        )

    dest_dir = os.path.join(ROMS, SYSTEMS[sysid]["folder"])
    os.makedirs(dest_dir, exist_ok=True)

    if ext == ".7z":
        return install_rom_7z(upload_path, sysid, filename)

    dst = os.path.join(dest_dir, filename)
    if os.path.exists(dst):
        raise ValueError("a ROM named %s already exists" % filename)

    shutil.move(upload_path, dst)
    os.chmod(dst, 0o644)

    rescan()
    return {"ok": True, "kind": "rom", "system": sysid, "file": filename}


def rescan():
    """Rebuild consoles.json via the existing scan-roms script."""
    try:
        subprocess.run(
            ["/usr/local/bin/scan-roms"],
            check=True, capture_output=True, timeout=300,
        )
    except Exception as exc:
        sys.stderr.write("scan-roms failed: %s\n" % exc)


def staging_path(session):
    return os.path.join(STAGING, "%s.part" % session)


def sweep_staging():
    """Drop part-files from uploads that were abandoned mid-flight."""
    now = time.time()
    try:
        entries = os.listdir(STAGING)
    except FileNotFoundError:
        return
    for fn in entries:
        full = os.path.join(STAGING, fn)
        try:
            if now - os.path.getmtime(full) < STAGING_TTL:
                continue
            shutil.rmtree(full) if os.path.isdir(full) else os.unlink(full)
        except OSError:
            pass


def hidden_ids():
    ids = read_json(HIDDEN, [])
    return ids if isinstance(ids, list) else []


def meta_overrides():
    """Per-game title/sub overrides, keyed by art id."""
    data = read_json(META, {})
    return data if isinstance(data, dict) else {}


def set_meta(gid, title, sub):
    """
    Record an edited title/subtitle for one game.

    Console entries live in consoles.json, which scan-roms rewrites from the
    ROM filenames on every upload, so edits are kept here instead and applied
    by the page.

    A cleared subtitle is stored as an empty string rather than omitted: the
    page distinguishes "no override" from "deliberately blank", and dropping
    the key would let the scanned region text come back.
    """
    with _manifest_lock:
        data = meta_overrides()
        entry = {"sub": sub}
        if title:
            entry["title"] = title

        # A DOS game owns its manifest row, so write straight through and keep
        # the overlay free of entries that would just shadow identical values.
        games = read_json(MANIFEST, [])
        row = next((g for g in games if g.get("id") == gid), None)
        if row is not None:
            if title:
                row["title"] = title
            row["sub"] = sub
            games.sort(key=lambda g: g.get("title", "").lower())
            write_json(MANIFEST, games)
            data.pop(gid, None)
        else:
            data[gid] = entry

        write_json(META, data)
    return meta_overrides().get(gid, {})


def console_art_id(sysid, path):
    """Mirror of the page's consoleArtId(): system id plus a slug of the filename."""
    base = os.path.splitext(os.path.basename(path))[0]
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:48]
    return "%s-%s" % (sysid, slug or "game")


def find_rom(gid):
    """Locate the ROM whose derived art id is gid, or None."""
    for sysid, spec in SYSTEMS.items():
        if not gid.startswith(sysid + "-"):
            continue
        folder = os.path.join(ROMS, spec["folder"])
        if not os.path.isdir(folder):
            continue
        for fn in os.listdir(folder):
            if os.path.splitext(fn)[1].lower() not in spec["exts"]:
                continue
            if console_art_id(sysid, fn) == gid:
                return os.path.join(folder, fn)
    return None


def delete_game(gid):
    """
    Remove a game and everything that refers to it.

    Deletes the bundle or ROM, its box art, and the hidden/metadata entries, so
    nothing is left that could resurrect the tile on the next rescan. Returns
    the list of paths removed.
    """
    removed = []

    def drop(path):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.unlink(path)
            else:
                return
            removed.append(path)
        except OSError as exc:
            sys.stderr.write("could not delete %s: %s\n" % (path, exc))

    with _manifest_lock:
        games = read_json(MANIFEST, [])
        is_dos = any(g.get("id") == gid for g in games)

        if is_dos:
            drop(os.path.join(BUNDLES, "%s.jsdos" % gid))
            # Only an exact slug match, so an upload never takes out an
            # unrelated original and DOOMS survives deleting DOOM.
            if os.path.isdir(DOS_SRC):
                for name in os.listdir(DOS_SRC):
                    full = os.path.join(DOS_SRC, name)
                    if os.path.isdir(full) and slugify(name) == gid:
                        drop(full)
            write_json(MANIFEST, [g for g in games if g.get("id") != gid])
        else:
            rom = find_rom(gid)
            if rom:
                drop(rom)

        drop(os.path.join(ART_DIR, "%s.jpg" % gid))

        ids = [i for i in hidden_ids() if i != gid]
        write_json(HIDDEN, sorted(ids))

        meta = meta_overrides()
        if meta.pop(gid, None) is not None:
            write_json(META, meta)

    if not is_dos:
        rescan()
    return removed


def set_hidden(gid, hide):
    with _manifest_lock:
        ids = [i for i in hidden_ids() if i != gid]
        if hide:
            ids.append(gid)
        ids.sort()
        write_json(HIDDEN, ids)
    return ids


class Handler(BaseHTTPRequestHandler):
    server_version = "dosadmin/2.0"

    # --- plumbing ---

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self, limit):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None, "bad length"
        if length <= 0:
            return None, "empty body"
        if length > limit:
            return None, "payload too large"
        return self.rfile.read(length), None

    def _json_body(self, limit=1024 * 1024):
        raw, err = self._body(limit)
        if err:
            return None, err
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, "invalid JSON"
        if not isinstance(data, dict):
            return None, "expected a JSON object"
        return data, None

    # --- routes ---

    def do_GET(self):
        if self.path.rstrip("/") == "/api/hidden":
            return self._json(200, {"ok": True, "hidden": hidden_ids()})
        if self.path.rstrip("/") == "/api/meta":
            return self._json(200, {"ok": True, "meta": meta_overrides()})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]

        m = re.match(r"^/api/art/([^/]+)$", path)
        if m:
            return self.art_upload(m.group(1))

        if path == "/api/upload/begin":
            return self.upload_begin()
        if path == "/api/upload/chunk":
            return self.upload_chunk()
        if path == "/api/upload/commit":
            return self.upload_commit()
        if path == "/api/upload/abort":
            return self.upload_abort()
        if path == "/api/hidden":
            return self.hidden_set()
        if path == "/api/meta":
            return self.meta_set()
        if path == "/api/delete":
            return self.game_delete()
        if path == "/api/rescan":
            rescan()
            return self._json(200, {"ok": True})

        return self._json(404, {"error": "not found"})

    def do_DELETE(self):
        m = re.match(r"^/api/art/([^/]+)$", self.path.split("?")[0])
        if not m:
            return self._json(404, {"error": "not found"})
        gid = safe_id(m.group(1))
        if not gid:
            return self._json(400, {"error": "bad game id"})
        dst = os.path.join(ART_DIR, "%s.jpg" % gid)
        if os.path.exists(dst):
            os.unlink(dst)
        return self._json(200, {"ok": True, "id": gid})

    # --- box art (unchanged behaviour from the original service) ---

    def art_upload(self, raw_id):
        gid = safe_id(raw_id)
        if not gid:
            return self._json(400, {"error": "bad game id"})

        raw, err = self._body(MAX_ART_BYTES)
        if err:
            code = 413 if err == "payload too large" else 400
            return self._json(code, {"error": "image too large (max 12 MB)" if code == 413 else err})

        # Pillow both validates and normalises: anything that is not a real
        # image raises here, and the re-encode strips any embedded payload.
        try:
            img = Image.open(io.BytesIO(raw))
            img.verify()
            img = Image.open(io.BytesIO(raw))
            img = img.convert("RGB")
            img.thumbnail(THUMB, Image.LANCZOS)
        except Exception:
            return self._json(415, {"error": "not a valid image"})

        os.makedirs(ART_DIR, exist_ok=True)
        tmp = os.path.join(ART_DIR, ".%s.tmp" % gid)
        dst = os.path.join(ART_DIR, "%s.jpg" % gid)
        try:
            img.save(tmp, "JPEG", quality=82, optimize=True)
            os.replace(tmp, dst)
            os.chmod(dst, 0o644)
        except Exception as exc:
            if os.path.exists(tmp):
                os.unlink(tmp)
            return self._json(500, {"error": "could not save: %s" % exc})

        return self._json(200, {"ok": True, "id": gid, "url": "/art/%s.jpg" % gid})

    # --- hiding ---

    def hidden_set(self):
        data, err = self._json_body()
        if err:
            return self._json(400, {"error": err})
        gid = safe_id(data.get("id"))
        if not gid:
            return self._json(400, {"error": "bad game id"})
        ids = set_hidden(gid, bool(data.get("hidden")))
        return self._json(200, {"ok": True, "id": gid, "hidden": ids})

    def meta_set(self):
        data, err = self._json_body()
        if err:
            return self._json(400, {"error": err})
        gid = safe_id(data.get("id"))
        if not gid:
            return self._json(400, {"error": "bad game id"})

        title = (data.get("title") or "").strip()[:120]
        sub = (data.get("sub") or "").strip()[:120]
        if not title:
            return self._json(400, {"error": "title cannot be empty"})

        set_meta(gid, title, sub)
        return self._json(200, {"ok": True, "id": gid, "title": title, "sub": sub})

    def game_delete(self):
        data, err = self._json_body()
        if err:
            return self._json(400, {"error": err})
        gid = safe_id(data.get("id"))
        if not gid:
            return self._json(400, {"error": "bad game id"})

        # Deleting is irreversible, so require the game to have been hidden
        # first: that makes it a deliberate two-step action, not a stray click.
        if gid not in hidden_ids():
            return self._json(409, {"error": "hide the game before deleting it"})

        removed = delete_game(gid)
        if not removed:
            return self._json(404, {"error": "nothing found to delete for %s" % gid})
        return self._json(200, {"ok": True, "id": gid, "removed": removed})

    # --- chunked upload ---

    def upload_begin(self):
        data, err = self._json_body()
        if err:
            return self._json(400, {"error": err})

        kind = (data.get("kind") or "").strip().lower()
        if kind not in ("dos", "rom"):
            return self._json(400, {"error": "kind must be 'dos' or 'rom'"})

        try:
            size = int(data.get("size") or 0)
        except (TypeError, ValueError):
            return self._json(400, {"error": "bad size"})
        if size <= 0:
            return self._json(400, {"error": "bad size"})
        if size > MAX_UPLOAD_BYTES:
            return self._json(413, {"error": "file too large (max 4 GB)"})

        # Fail early rather than filling the disk and dying at 90%.
        free = shutil.disk_usage(STAGING).free
        needed = size * 2 if kind == "dos" else size + (256 * 1024 * 1024)
        if free < needed:
            return self._json(
                507,
                {"error": "not enough disk space (need ~%d MB, have %d MB)"
                          % (needed // 1048576, free // 1048576)},
            )

        sweep_staging()
        os.makedirs(STAGING, exist_ok=True)
        session = os.urandom(16).hex()
        open(staging_path(session), "wb").close()
        return self._json(200, {"ok": True, "session": session, "chunkSize": 8 * 1024 * 1024})

    def upload_chunk(self):
        session = (self.headers.get("X-Upload-Session") or "").strip().lower()
        if not SESSION_RE.match(session):
            return self._json(400, {"error": "bad session"})
        part = staging_path(session)
        if not os.path.exists(part):
            return self._json(404, {"error": "upload session not found - start again"})

        try:
            offset = int(self.headers.get("X-Upload-Offset", "-1"))
        except ValueError:
            return self._json(400, {"error": "bad offset"})

        raw, err = self._body(MAX_CHUNK_BYTES)
        if err:
            code = 413 if err == "payload too large" else 400
            return self._json(code, {"error": err})

        # The offset check makes a retried or out-of-order chunk safe: a client
        # that resends the previous chunk is told where the file actually ends.
        have = os.path.getsize(part)
        if offset != have:
            return self._json(409, {"error": "offset mismatch", "expected": have})
        if have + len(raw) > MAX_UPLOAD_BYTES:
            return self._json(413, {"error": "file too large"})

        with open(part, "ab") as fh:
            fh.write(raw)
        return self._json(200, {"ok": True, "received": os.path.getsize(part)})

    def upload_commit(self):
        data, err = self._json_body()
        if err:
            return self._json(400, {"error": err})

        session = (data.get("session") or "").strip().lower()
        if not SESSION_RE.match(session):
            return self._json(400, {"error": "bad session"})
        part = staging_path(session)
        if not os.path.exists(part):
            return self._json(404, {"error": "upload session not found"})

        kind = (data.get("kind") or "").strip().lower()
        keep_part = False
        try:
            if kind == "dos":
                result = install_dos(part, data)
            elif kind == "rom":
                result = install_rom(part, data)   # moves the part file into place
            else:
                return self._json(400, {"error": "kind must be 'dos' or 'rom'"})
        except NeedsChoice as ask:
            # Hold the upload so the browser can answer without re-sending it.
            keep_part = True
            shutil.rmtree(ask.work, ignore_errors=True)
            return self._json(409, {
                "needsChoice": True,
                "session": session,
                "candidates": ask.candidates,
            })
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            sys.stderr.write("commit failed: %s\n" % exc)
            return self._json(500, {"error": "could not install: %s" % exc})
        finally:
            if not keep_part and os.path.exists(part):
                os.unlink(part)

        return self._json(200, result)

    def upload_abort(self):
        data, err = self._json_body()
        if err:
            return self._json(400, {"error": err})
        session = (data.get("session") or "").strip().lower()
        if SESSION_RE.match(session):
            part = staging_path(session)
            if os.path.exists(part):
                os.unlink(part)
        return self._json(200, {"ok": True})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    for d in (ART_DIR, STAGING, BUNDLES):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(HIDDEN):
        write_json(HIDDEN, [])
    if not os.path.exists(META):
        write_json(META, {})
    sweep_staging()
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
