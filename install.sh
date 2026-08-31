#!/usr/bin/env bash

# First-install script for the retro games launcher.
#
# Runs INSIDE a fresh Debian 13 LXC as root, from the staged project tree:
#   bash install.sh
#
# Lays down the web root, admin service, nginx site and systemd unit.
# ROMs (/opt/roms) and other live data are never overwritten if present.

set -euo pipefail

BL='\033[1;34m'
GN='\033[1;92m'
RD='\033[0;31m'
OR='\033[0;33m'
CL='\033[0m'
BFR="\\r\\033[K"

WEB="/var/www/dosgames"
ART_DIR="/opt/art"
ROMS="/opt/roms"
UPLOADS="/opt/uploads"
DOS_SRC="/opt/dosgames"

MSG_BLU=" - [${BL}INFO${CL}]"
MSG_GN=" [${GN}SUCC${CL}]"
MSG_RD=" [${RD}FAIL${CL}]"
MSG_WARN=" [${OR}WARN${CL}]"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

header_info() {
    clear
    cat <<"EOF"
   ____                 _                        __  __
  / ___| ___   _ __ ___| |__  _ __ ___   ___    |  \/  | __ _ _ __   __ _  __ _  ___ _ __
 | |  _ / _ \ | '__/ _ \ '_ \| '_ ` _ \ / _ \   | |\/| |/ _` | '_ \ / _` |/ _` |/ _ \ '__|
 | |_| | (_) || | |  __/ |_) | | | | | |  __/   | |  | | (_| | | | | (_| | (_| |  __/ |
  \____|\___/ |_|  \___|_.__/|_| |_| |_|\___|___|_|  |_|\__,_|_| |_|\__,_|\__, |\___|_|
                                           |___/                          |___/
EOF
}

install_deps() {
    echo -en "${MSG_BLU} Installing packages (nginx, python3-pil, p7zip)...${CL}"
    apt-get update -qq >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        nginx python3 python3-pil p7zip-full zip unzip >/dev/null
    echo -e "${BFR}${MSG_GN} Installed${CL}"
}

create_dirs() {
    echo -en "${MSG_BLU} Creating directories...${CL}"
    mkdir -p "$WEB" "$ART_DIR" "$ROMS/Roms" "$ROMS/Bios" "$UPLOADS" "$DOS_SRC"
    echo -e "${BFR}${MSG_GN} Created${CL}"
}

install_web() {
    echo -en "${MSG_BLU} Installing web root...${CL}"
    cp "$SCRIPT_DIR/web/index.html" "$WEB/"
    cp -r "$SCRIPT_DIR/web/img" "$SCRIPT_DIR/web/ejs" "$SCRIPT_DIR/web/jsdos" "$WEB/"
    # Initial manifests only when absent - live data wins over the snapshot.
    for f in games.json consoles.json hidden.json meta.json; do
        if [ ! -f "$WEB/$f" ] && [ -f "$SCRIPT_DIR/data/manifests/$f" ]; then
            cp "$SCRIPT_DIR/data/manifests/$f" "$WEB/$f"
        fi
    done
    if [ -d "$SCRIPT_DIR/data/games" ]; then
        mkdir -p "$WEB/games"
        for b in "$SCRIPT_DIR/data/games"/*.jsdos; do
            [ -e "$b" ] || continue
            [ -f "$WEB/games/$(basename "$b")" ] || cp "$b" "$WEB/games/"
        done
    fi
    echo -e "${BFR}${MSG_GN} Installed${CL}"
}

install_admin() {
    echo -en "${MSG_BLU} Installing admin scripts...${CL}"
    install -m 755 "$SCRIPT_DIR/admin/admin-service.py" /usr/local/bin/admin-service.py
    install -m 755 "$SCRIPT_DIR/admin/scan-roms" /usr/local/bin/scan-roms
    install -m 755 "$SCRIPT_DIR/admin/add-game" /usr/local/bin/add-game
    install -m 755 "$SCRIPT_DIR/admin/art-service.py" /usr/local/bin/art-service.py
    echo -e "${BFR}${MSG_GN} Installed${CL}"
}

restore_data_snapshot() {
    echo -en "${MSG_BLU} Restoring data snapshot (art, DOS sources)...${CL}"
    if [ -d "$SCRIPT_DIR/data/art" ]; then
        for a in "$SCRIPT_DIR/data/art"/*.jpg; do
            [ -e "$a" ] || continue
            [ -f "$ART_DIR/$(basename "$a")" ] || cp "$a" "$ART_DIR/"
        done
    fi
    if [ -d "$SCRIPT_DIR/data/dosgames" ]; then
        cp -rn "$SCRIPT_DIR/data/dosgames/." "$DOS_SRC/"
    fi
    echo -e "${BFR}${MSG_GN} Restored${CL}"
}

install_nginx() {
    echo -en "${MSG_BLU} Configuring nginx...${CL}"
    cp "$SCRIPT_DIR/nginx/dosgames.conf" /etc/nginx/sites-available/dosgames
    ln -sf /etc/nginx/sites-available/dosgames /etc/nginx/sites-enabled/dosgames
    rm -f /etc/nginx/sites-enabled/default
    nginx -t >/dev/null 2>&1 || {
        echo -e "${BFR}${MSG_RD} nginx config test failed${CL}"
        nginx -t || true
        exit 1
    }
    systemctl enable --now nginx >/dev/null 2>&1
    systemctl reload nginx
    echo -e "${BFR}${MSG_GN} Configured${CL}"
}

install_service() {
    echo -en "${MSG_BLU} Installing admin service...${CL}"
    cp "$SCRIPT_DIR/systemd/dosgames-admin.service" /etc/systemd/system/dosgames-admin.service
    systemctl daemon-reload
    systemctl enable dosgames-admin >/dev/null 2>&1
    systemctl restart dosgames-admin
    sleep 2
    if systemctl is-active --quiet dosgames-admin; then
        echo -e "${BFR}${MSG_GN} Service running${CL}"
    else
        echo -e "${BFR}${MSG_RD} Service failed - check: journalctl -u dosgames-admin -e${CL}"
        exit 1
    fi
}

rescan_roms() {
    echo -en "${MSG_BLU} Scanning ROM library...${CL}"
    /usr/local/bin/scan-roms || echo -e "${BFR}${MSG_WARN} scan-roms reported an error${CL}"
    echo -e "${BFR}${MSG_GN} Done${CL}"
}

show_success() {
    IP=$(ip -4 addr show eth0 | awk -F'[ /]+' '/inet/{print $3; exit}')
    echo ""
    echo -e "${GN}Installation complete!${CL}"
    echo ""
    echo -e "  ${MSG_GN}Site:${CL}          http://${IP:-<ct-ip>}/"
    echo -e "  ${MSG_GN}Services:${CL}      nginx, dosgames-admin"
    echo -e "  ${MSG_GN}Web root:${CL}      ${WEB}"
    echo -e "  ${MSG_GN}ROMs:${CL}          ${ROMS} (NES Roms/, PS1 Roms/, Bios/)"
    echo ""
    echo -e "  ${BL}ROMs are not in the project tarball - copy your library back,${CL}"
    echo -e "  ${BL}or upload games through the site's admin tools, then run:${CL}"
    echo -e "    /usr/local/bin/scan-roms"
    echo ""
}

header_info
echo ""
[ "$(id -u)" -eq 0 ] || { echo -e "${MSG_RD} Must run as root inside the LXC${CL}"; exit 1; }
install_deps
create_dirs
install_web
install_admin
restore_data_snapshot
install_nginx
install_service
rescan_roms
show_success
