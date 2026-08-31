# Retro Games Launcher

Browser arcade: DOS games via js-dos, NES/PS1 via EmulatorJS, behind nginx in
a Proxmox LXC. Recovered from the `dosgames` container (CT 112) — this repo is
the source of truth for all code.

## Layout

```
web/        Static site served by nginx
  index.html      Launcher UI (single file: markup, CSS, JS)
  img/            Background image
  ejs/            EmulatorJS runtime (NES, PS1 cores)
  jsdos/          js-dos runtime (DOS)
admin/      Services running inside the container
  admin-service.py  HTTP API on 127.0.0.1:8089 (uploads, box art, hide, meta)
  scan-roms         Rebuilds consoles.json from /opt/roms
  add-game          CLI to package a DOS folder into a .jsdos bundle
  art-service.py    Original box-art service (superseded by admin-service)
nginx/      nginx site config (web root, /api/, /art/, /roms/, /bios/)
systemd/    dosgames-admin.service unit
data/       Snapshot of live state at recovery time (backup only, not deployed)
  manifests/  games.json, consoles.json, hidden.json, meta.json
  games/      *.jsdos bundles
  art/        box art
  dosgames/   original DOS game sources
install.sh  First install — runs INSIDE a fresh Debian 13 LXC
deploy.sh   Push updates to (or create) the LXC on Proxmox
dev.sh      Local preview with emulator headers on http://127.0.0.1:8090
```

## Code vs live data

The admin UI writes state inside the container. Deploys never touch it:

| Path (in CT)              | Written by       | Deployed? |
|---------------------------|------------------|-----------|
| `/var/www/dosgames/*.json`| admin service    | no        |
| `/var/www/dosgames/games/`| uploads/add-game | no        |
| `/opt/art`, `/opt/uploads`| admin service    | no        |
| `/opt/roms` (5.9G library)| uploads          | no        |
| `/opt/dosgames`           | originals        | no        |

`data/` holds a one-time snapshot for disaster recovery; a fresh install seeds
from it. ROMs are NOT in this repo — back them up separately, e.g.
`ssh root@192.168.1.40 "pct exec 112 -- tar -czf - -C / opt/roms" > roms.tgz`.

## Deploy

```
./deploy.sh
```

Defaults: Proxmox `root@192.168.1.40`, CT 112 (existing dosgames). Update
mode pushes web/admin/nginx/systemd and restarts services. A blank CT ID
creates a new Debian 13 container (hostname `dosgames`, 2 cores, 1 GB, 32 GB).

Site after deploy: http://192.168.1.63/ (or the CT's IP).

## Local preview

```
./dev.sh            # http://127.0.0.1:8090
```

## Adding games

- DOS zip or console ROM: use the upload/tools UI on the site (admin API).
- DOS folder already in the CT: `pct exec 112 -- add-game <folder> --title "..."`.

## Recovery (fresh container)

1. `./deploy.sh`, choose a blank CT ID, create the LXC.
2. Restore ROMs: untar the backup into `/opt/roms` inside the CT.
3. `/usr/local/bin/scan-roms` inside the CT to rebuild `consoles.json`.
