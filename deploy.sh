#!/usr/bin/env bash

# Deploy / update the retro games launcher to a Proxmox LXC container.
#
# First run:  creates the LXC, installs the app, starts the services.
# Subsequent: pushes new code into the existing LXC and restarts services.
#
# Code deployed:   web/ (index.html, img, ejs, jsdos), admin/*,
#                  nginx/dosgames.conf, systemd/dosgames-admin.service
# Never touched:   live manifests (games.json, consoles.json, hidden.json,
#                  meta.json), uploaded bundles (web games/), box art,
#                  ROMs (/opt/roms), uploads and DOS sources under /opt.

set -euo pipefail

# Colors
BL='\033[1;34m'
GN='\033[1;92m'
RD='\033[0;31m'
OR='\033[0;33m'
CL='\033[0m'
BFR="\\r\\033[K"

APP_NAME="dosgames"
TEMP_TAR="/tmp/${APP_NAME}.tar.gz"
REMOTE_TEMP_DIR="/tmp/${APP_NAME}-src"
DEPLOY_TAR="/tmp/${APP_NAME}-deploy.tar.gz"
LXC_STAGING="/tmp/${APP_NAME}-staging"
SERVICE_NAME="dosgames-admin.service"

# Proxmox connection
PROXMOX_HOST=""
SSH_USER=""
SSH_PORT=""
SSH_KEY=""

# LXC settings (only used on first deploy)
CT_ID=""
CT_HOSTNAME="$APP_NAME"
CT_CORES="2"
CT_MEMORY="1024"
CT_DISK="32"
CT_STORAGE="local-lvm"
CT_TEMPLATE=""
CT_IP="192.168.1.63/24"
CT_GW="192.168.1.1"
LXC_IP=""

MSG_BLU=" - [${BL}INFO${CL}]"
MSG_GN=" [${GN}SUCC${CL}]"
MSG_RD=" [${RD}FAIL${CL}]"
MSG_WARN=" [${OR}WARN${CL}]"

pve_ssh() {
    ssh \
        -p "${SSH_PORT}" \
        -i "${SSH_KEY}" \
        -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=accept-new \
        -o BatchMode=yes \
        "${SSH_USER}@${PROXMOX_HOST}" "LC_ALL=C $*"
}

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

get_connection_details() {
    echo -e "${BL}Proxmox Host Connection${CL}"
    echo ""

    read -p "Proxmox Host/IP [192.168.1.40]: " PROXMOX_HOST
    PROXMOX_HOST=${PROXMOX_HOST:-192.168.1.40}

    read -p "SSH Username [root]: " SSH_USER
    SSH_USER=${SSH_USER:-root}

    read -p "SSH Port [22]: " SSH_PORT
    SSH_PORT=${SSH_PORT:-22}

    DEFAULT_KEY=$(ls ~/.ssh/id_ed25519 ~/.ssh/id_rsa 2>/dev/null | head -1 || true)
    DEFAULT_KEY=${DEFAULT_KEY:-~/.ssh/id_rsa}
    read -p "SSH Key [${DEFAULT_KEY}]: " SSH_KEY
    SSH_KEY=${SSH_KEY:-${DEFAULT_KEY}}
    SSH_KEY="${SSH_KEY/#\~/$HOME}"
    if [ ! -f "$SSH_KEY" ]; then
        echo -e "${MSG_RD} Key not found: ${SSH_KEY}${CL}"
        exit 1
    fi

    echo ""
    echo -e "${BL}Connection Details:${CL}"
    echo -e "  Host: ${PROXMOX_HOST}"
    echo -e "  User: ${SSH_USER}"
    echo -e "  Port: ${SSH_PORT}"
    echo -e "  Key:  ${SSH_KEY}"
    echo ""
    read -p "Continue? [y/N]: " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "${RD}Cancelled${CL}"
        exit 0
    fi
}

check_ssh_connection() {
    echo -en "${MSG_BLU} Testing SSH connection to ${PROXMOX_HOST}...${CL}"
    if pve_ssh "echo connected" &>/dev/null; then
        echo -e "${BFR}${MSG_GN} Connected${CL}"
    else
        echo -e "${BFR}${MSG_RD} Failed to connect${CL}"
        echo -e "${MSG_WARN} Check:"
        echo -e "  - Host is reachable"
        echo -e "  - Key is authorized on the remote host (ssh-copy-id)"
        echo -e "  - User has SSH access"
        exit 1
    fi
}

# Returns 0 (true) if the app is already installed in the given CT.
app_is_installed() {
    pve_ssh "pct exec ${CT_ID} -- test -f /usr/local/bin/admin-service.py" &>/dev/null
}

get_lxc_settings() {
    echo ""
    echo -e "${BL}LXC Container Settings${CL}"
    echo ""

    NEXT_CT_ID=$(pve_ssh "pvesh get /cluster/nextid" 2>/dev/null || echo "100")

    read -p "CT ID [${NEXT_CT_ID}]: " CT_ID
    CT_ID=${CT_ID:-${NEXT_CT_ID}}

    read -p "Hostname [${CT_HOSTNAME}]: " input
    CT_HOSTNAME=${input:-$CT_HOSTNAME}

    read -p "CPU Cores [${CT_CORES}]: " input
    CT_CORES=${input:-$CT_CORES}

    read -p "Memory (MB) [${CT_MEMORY}]: " input
    CT_MEMORY=${input:-$CT_MEMORY}

    read -p "Disk Size (GB) [${CT_DISK}]: " input
    CT_DISK=${input:-$CT_DISK}

    read -p "Storage [${CT_STORAGE}]: " input
    CT_STORAGE=${input:-$CT_STORAGE}

    read -p "IP Address [${CT_IP}]: " input
    CT_IP=${input:-$CT_IP}

    read -p "Gateway [${CT_GW}]: " input
    CT_GW=${input:-$CT_GW}

    TEMPLATE_STORAGE=$(pve_ssh "pvesm status --content vztmpl 2>/dev/null | awk 'NR>1 && \$3==\"active\" {print \$1; exit}'" || echo "local")
    TEMPLATE_STORAGE=${TEMPLATE_STORAGE:-local}

    echo ""
    echo -e "${BL}Available Debian Templates (storage: ${TEMPLATE_STORAGE}):${CL}"
    AVAILABLE_TEMPLATES=$(pve_ssh "pveam available --section system" 2>/dev/null | awk '{print $2}' | grep '^debian-' || true)
    echo "$AVAILABLE_TEMPLATES"
    echo ""

    DEFAULT_TEMPLATE=$(echo "$AVAILABLE_TEMPLATES" | grep '^debian-13' | sort -V | tail -1)
    if [ -z "$DEFAULT_TEMPLATE" ]; then
        DEFAULT_TEMPLATE=$(echo "$AVAILABLE_TEMPLATES" | grep '^debian-12' | sort -V | tail -1)
    fi
    if [ -z "$DEFAULT_TEMPLATE" ]; then
        DEFAULT_TEMPLATE=$(echo "$AVAILABLE_TEMPLATES" | sort -V | tail -1)
    fi
    DEFAULT_TEMPLATE=${DEFAULT_TEMPLATE:-debian-13-standard_13.0-1_amd64.tar.zst}

    read -p "Template [${TEMPLATE_STORAGE}:vztmpl/${DEFAULT_TEMPLATE}]: " CT_TEMPLATE
    CT_TEMPLATE=${CT_TEMPLATE:-${TEMPLATE_STORAGE}:vztmpl/${DEFAULT_TEMPLATE}}

    echo ""
    echo -e "${BL}LXC Configuration:${CL}"
    echo -e "  CT ID:    ${CT_ID}"
    echo -e "  Hostname: ${CT_HOSTNAME}"
    echo -e "  Cores:    ${CT_CORES}"
    echo -e "  Memory:   ${CT_MEMORY}MB"
    echo -e "  Disk:     ${CT_DISK}GB"
    echo -e "  Storage:  ${CT_STORAGE}"
    echo -e "  IP:       ${CT_IP} gw ${CT_GW}"
    echo -e "  Template: ${CT_TEMPLATE}"
    echo ""
    read -p "Create this LXC? [y/N]: " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "${RD}Cancelled${CL}"
        exit 0
    fi
}

create_lxc() {
    echo ""
    echo -e "${BL}Creating LXC Container...${CL}"

    TEMPLATE_NAME=$(basename "$CT_TEMPLATE")
    TEMPLATE_STORAGE=${CT_TEMPLATE%%:*}
    echo -en "${MSG_BLU} Checking template...${CL}"

    if pve_ssh "pveam list ${TEMPLATE_STORAGE} 2>/dev/null" | grep -q "$TEMPLATE_NAME"; then
        echo -e "${BFR}${MSG_GN} Found${CL}"
    else
        echo -e "${BFR}${MSG_WARN} Not found, downloading...${CL}"
        if ! pve_ssh "pveam update >/dev/null 2>&1; pveam download ${TEMPLATE_STORAGE} ${TEMPLATE_NAME}"; then
            echo -e "${MSG_RD} Download failed: '${TEMPLATE_NAME}' is not in the pveam catalog${CL}"
            exit 1
        fi
        echo -e "${MSG_GN} Downloaded${CL}"
    fi

    echo -en "${MSG_BLU} Creating CT ${CT_ID}...${CL}"
    pve_ssh "pct create ${CT_ID} ${CT_TEMPLATE} \
        --hostname ${CT_HOSTNAME} \
        --cores ${CT_CORES} \
        --memory ${CT_MEMORY} \
        --swap ${CT_MEMORY} \
        --rootfs ${CT_STORAGE}:${CT_DISK} \
        --net0 name=eth0,bridge=vmbr0,ip=${CT_IP},gw=${CT_GW} \
        --onboot 1 \
        --unprivileged 1 \
        --features nesting=1" || {
        echo -e "${BFR}${MSG_RD} Failed to create CT${CL}"
        exit 1
    }
    echo -e "${BFR}${MSG_GN} Created${CL}"

    echo -en "${MSG_BLU} Starting CT ${CT_ID}...${CL}"
    pve_ssh "pct start ${CT_ID}" 2>/dev/null || true
    echo -e "${BFR}${MSG_GN} Started${CL}"

    echo -en "${MSG_BLU} Waiting for CT to be ready...${CL}"
    for i in $(seq 1 30); do
        status=$(pve_ssh "pct exec ${CT_ID} -- systemctl is-system-running 2>/dev/null" || echo "")
        if echo "$status" | grep -q "running\|degraded"; then
            echo -e "${BFR}${MSG_GN} Ready${CL}"
            break
        fi
        sleep 1
    done

    LXC_IP="${CT_IP%%/*}"
    echo -e "${MSG_GN} IP: ${LXC_IP}${CL}"
}

create_tarball() {
    echo -en "${MSG_BLU} Creating deployment tarball...${CL}"
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    tar -czf "$TEMP_TAR" \
        --exclude='.git' \
        --exclude='.claude' \
        --exclude='.kilo' \
        --exclude='*.tar.gz' \
        --exclude='__pycache__' \
        -C "$SCRIPT_DIR" \
        install.sh \
        web \
        admin \
        nginx \
        systemd
    SIZE=$(du -h "$TEMP_TAR" | cut -f1)
    echo -e "${BFR}${MSG_GN} Created (${SIZE})${CL}"
}

upload_and_stage() {
    echo -en "${MSG_BLU} Uploading to ${PROXMOX_HOST}...${CL}"
    scp \
        -P "$SSH_PORT" \
        -i "${SSH_KEY}" \
        -o StrictHostKeyChecking=accept-new \
        -o BatchMode=yes \
        "$TEMP_TAR" "${SSH_USER}@${PROXMOX_HOST}:${TEMP_TAR}"
    echo -e "${BFR}${MSG_GN} Uploaded${CL}"

    echo -en "${MSG_BLU} Staging on Proxmox host...${CL}"
    pve_ssh "set -euo pipefail
rm -rf ${REMOTE_TEMP_DIR}
mkdir -p ${REMOTE_TEMP_DIR}
tar -xzf ${TEMP_TAR} -C ${REMOTE_TEMP_DIR}
rm -f ${TEMP_TAR}
cd ${REMOTE_TEMP_DIR} && tar -czf ${DEPLOY_TAR} ."
    echo -e "${BFR}${MSG_GN} Staged${CL}"

    echo -en "${MSG_BLU} Pushing to LXC ${CT_ID}...${CL}"
    pve_ssh "pct push ${CT_ID} ${DEPLOY_TAR} ${DEPLOY_TAR}"
    pve_ssh "pct exec ${CT_ID} -- mkdir -p ${LXC_STAGING}"
    pve_ssh "pct exec ${CT_ID} -- tar -xzf ${DEPLOY_TAR} -C ${LXC_STAGING}"
    pve_ssh "pct exec ${CT_ID} -- rm -f ${DEPLOY_TAR}"
    pve_ssh "rm -rf ${REMOTE_TEMP_DIR} ${DEPLOY_TAR}"
    echo -e "${BFR}${MSG_GN} Done${CL}"
}

run_first_install() {
    echo ""
    echo -e "${BL}Running installer in LXC ${CT_ID}...${CL}"
    ssh \
        -p "$SSH_PORT" \
        -i "${SSH_KEY}" \
        -o StrictHostKeyChecking=accept-new \
        -t \
        "${SSH_USER}@${PROXMOX_HOST}" \
        "LC_ALL=C pct exec ${CT_ID} -- bash ${LXC_STAGING}/install.sh"
}

run_update() {
    echo ""
    echo -e "${BL}Updating application in LXC ${CT_ID}...${CL}"

    echo -en "${MSG_BLU} Copying web code...${CL}"
    pve_ssh "set -euo pipefail
pct exec ${CT_ID} -- bash -c '
    set -euo pipefail
    WEB=/var/www/dosgames
    cp ${LXC_STAGING}/web/index.html \$WEB/index.html
    rm -rf \$WEB/img \$WEB/ejs \$WEB/jsdos
    cp -r ${LXC_STAGING}/web/img ${LXC_STAGING}/web/ejs ${LXC_STAGING}/web/jsdos \$WEB/
'"
    echo -e "${BFR}${MSG_GN} Web code updated${CL}"

    echo -en "${MSG_BLU} Copying admin scripts...${CL}"
    pve_ssh "pct exec ${CT_ID} -- bash -c '
        install -m 755 ${LXC_STAGING}/admin/admin-service.py /usr/local/bin/admin-service.py
        install -m 755 ${LXC_STAGING}/admin/scan-roms /usr/local/bin/scan-roms
        install -m 755 ${LXC_STAGING}/admin/add-game /usr/local/bin/add-game
        install -m 755 ${LXC_STAGING}/admin/art-service.py /usr/local/bin/art-service.py
    '"
    echo -e "${BFR}${MSG_GN} Scripts updated${CL}"

    echo -en "${MSG_BLU} Updating nginx config...${CL}"
    pve_ssh "set -euo pipefail
pct exec ${CT_ID} -- bash -c '
    set -euo pipefail
    if ! cmp -s ${LXC_STAGING}/nginx/dosgames.conf /etc/nginx/sites-available/dosgames; then
        cp /etc/nginx/sites-available/dosgames /etc/nginx/sites-available/dosgames.bak-\$(date +%Y%m%d-%H%M%S) 2>/dev/null || true
        cp ${LXC_STAGING}/nginx/dosgames.conf /etc/nginx/sites-available/dosgames
        ln -sf /etc/nginx/sites-available/dosgames /etc/nginx/sites-enabled/dosgames
        nginx -t
        systemctl reload nginx
    fi
'"
    echo -e "${BFR}${MSG_GN} nginx up to date${CL}"

    echo -en "${MSG_BLU} Updating admin service...${CL}"
    pve_ssh "pct exec ${CT_ID} -- bash -c '
        cp ${LXC_STAGING}/systemd/dosgames-admin.service /etc/systemd/system/dosgames-admin.service
        systemctl daemon-reload
        systemctl restart dosgames-admin
    '"
    sleep 2
    if pve_ssh "pct exec ${CT_ID} -- systemctl is-active --quiet ${SERVICE_NAME}"; then
        echo -e "${BFR}${MSG_GN} Service running${CL}"
    else
        echo -e "${BFR}${MSG_RD} Service failed to start — check: pct exec ${CT_ID} -- journalctl -u ${SERVICE_NAME} -e${CL}"
        exit 1
    fi

    echo -en "${MSG_BLU} Cleaning staging...${CL}"
    pve_ssh "pct exec ${CT_ID} -- rm -rf ${LXC_STAGING}"
    echo -e "${BFR}${MSG_GN} Done${CL}"
}

cleanup_local() {
    rm -f "$TEMP_TAR"
}

show_post_deploy() {
    echo ""
    echo -e "${GN}Done!${CL}"
    echo ""
    echo -e "${BL}LXC:${CL} CT ${CT_ID} — ${LXC_IP}"
    echo ""
    echo -e "${BL}Site:${CL}"
    echo -e "  Direct:  http://${LXC_IP}/"
    echo ""
    echo -e "${BL}Logs:${CL} pct exec ${CT_ID} -- journalctl -u ${SERVICE_NAME} -f"
    echo ""
    echo -e "${BL}Next deploy:${CL} just run ./deploy.sh again"
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

header_info
echo ""

get_connection_details
echo ""

check_ssh_connection
echo ""

# Determine whether this is a first install or an update.
echo -en "${MSG_BLU} Checking for existing installation...${CL}"

read -p "CT ID to deploy into (leave blank to create new) [112]: " CT_ID_INPUT
CT_ID_INPUT=${CT_ID_INPUT:-"112"}

if [ -n "$CT_ID_INPUT" ]; then
    CT_ID="$CT_ID_INPUT"
    if ! pve_ssh "pct config ${CT_ID}" &>/dev/null; then
        echo -e "${MSG_RD} CT ${CT_ID} not found on Proxmox${CL}"
        exit 1
    fi
    LXC_IP=$(pve_ssh "pct config ${CT_ID} 2>/dev/null | awk -F'[=,/]' '/^net/{print \$3}'" || echo "")
    IS_UPDATE=true
else
    IS_UPDATE=false
fi

if [ "$IS_UPDATE" = true ] && app_is_installed; then
    echo -e "${BFR}${MSG_GN} Updating CT ${CT_ID}${CL}"
    echo ""
    create_tarball
    echo ""
    upload_and_stage
    echo ""
    run_update
elif [ "$IS_UPDATE" = true ]; then
    echo -e "${BFR}${MSG_WARN} CT ${CT_ID} has no previous install - running installer${CL}"
    echo ""
    create_tarball
    echo ""
    upload_and_stage
    echo ""
    run_first_install
else
    echo -e "${BFR}${MSG_BLU} Creating new LXC${CL}"
    get_lxc_settings
    echo ""
    create_lxc
    echo ""
    create_tarball
    echo ""
    upload_and_stage
    echo ""
    run_first_install
fi

cleanup_local
show_post_deploy
