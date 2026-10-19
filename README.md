# Retro Games Launcher

Browser arcade: DOS games via js-dos, NES/PS1 via EmulatorJS, behind nginx in
a Proxmox LXC.

> **Deployment target: a Proxmox VE LXC.** `deploy.sh` drives a Proxmox host
> over SSH (`pct`/`pct push`) and expects a Debian 13 container. The defaults
> point at the author's homelab (`root@192.168.1.40`, CT 112, site at
> 192.168.1.63) — change them when prompted for your own host. No Docker,
> cloud or bare-metal tooling here. See *Deploying without Proxmox* below if
> you just have a Debian box.

Notes for visitors:
- **No games are included.** This repo is code only; add your own legally
  owned DOS folders/ROMs through the upload UI. `data/manifests/` is just
  seed JSON.
- **The admin API has no authentication** (LAN-only by design, and nginx
  proxies `/api/` to it). Do not port-forward or expose this site to the
  internet.

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
data/       Snapshot of live state (not deployed; only manifests/ is in the repo)
  manifests/  games.json, consoles.json, hidden.json, meta.json (seed data)
  games/      *.jsdos bundles        } gitignored - exist only on the
  art/        box art                } author's machine / your own
  dosgames/   original DOS sources   } container, never pushed
install.sh  First install — runs as root inside a Debian 13 LXC or VM/host
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

## Deploy (Proxmox LXC)

```
./deploy.sh
```

Interactive; connects to your Proxmox host (defaults: `root@192.168.1.40`,
port 22, key autodetected from `~/.ssh/id_ed25519` or `id_rsa`). Then:

- **Existing container** (default CT 112): update mode — pushes
  web/admin/nginx/systemd into the CT and restarts services.
- **Blank CT ID**: creates a new Debian 13 LXC (`dosgames`, 2 cores, 1 GB,
  32 GB) and runs `install.sh` inside it.

Site after deploy: `http://<ct-ip>/`.

## Deploying without Proxmox

The container side is plain Debian: nginx + Python 3 (stdlib + Pillow) +
p7zip. On any Debian 13 machine as root:

```
git clone https://github.com/<you>/game-emulation.git && cd game-emulation
bash install.sh
```

Then visit `http://<host>/`. The `deploy.sh`/`pct` plumbing is only the
convenient update path for the Proxmox setup this was built on.

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
