#!/usr/bin/env bash
# nutshutdown installer for Debian / Ubuntu / Proxmox LXCs.
#
# Read this script before running it. It does:
#   1. apt-install dependencies (nut, nut-snmp, python3-flask, sshpass, openssh-client)
#   2. copy code to ${INSTALL_DIR} (default /opt/nutshutdown)
#   3. write config.json from config.example.json (only if not already present)
#   4. install the systemd web-UI unit
#   5. install the nut-driver@.service TimeoutStopSec=10 drop-in
#   6. add the per-minute cron line (only if not already there)
#
# It does NOT:
#   - Touch /etc/nut/*.conf — that's UPS-specific. See README "NUT setup".
#   - Generate or copy SSH keys — that's a security decision for you.
#   - Start the web service — start it after you've edited config.json.
#
# Re-running is safe: every step is idempotent.

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/nutshutdown}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; CYAN=$'\033[1;36m'; RESET=$'\033[0m'

step() { printf "%s==>%s %s\n" "$CYAN" "$RESET" "$*"; }
warn() { printf "%s!! %s%s\n" "$YELLOW" "$*" "$RESET"; }
done_() { printf "%sok%s  %s\n" "$GREEN" "$RESET" "$*"; }

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root (sudo $0)" >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]]; then
  warn "can't read /etc/os-release — proceeding, but this script is tested only on Debian/Ubuntu."
else
  . /etc/os-release
  case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*) ;;
    *) warn "Detected '${ID:-unknown}' — not Debian-family. apt commands will fail." ;;
  esac
fi

step "installing apt packages"
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  nut nut-snmp python3 python3-flask sshpass openssh-client cron >/dev/null
done_ "apt packages installed"

step "copying code to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
install -m 0755 "${SRC_DIR}/pmlib.py"         "${INSTALL_DIR}/pmlib.py"
install -m 0755 "${SRC_DIR}/power_monitor.py" "${INSTALL_DIR}/power_monitor.py"
install -m 0755 "${SRC_DIR}/manage.py"        "${INSTALL_DIR}/manage.py"
install -m 0755 "${SRC_DIR}/webapp.py"        "${INSTALL_DIR}/webapp.py"
done_ "code at ${INSTALL_DIR}"

step "preparing config.json"
if [[ -e "${INSTALL_DIR}/config.json" ]]; then
  done_ "config.json already exists — not touching it"
else
  install -m 0640 "${SRC_DIR}/config.example.json" "${INSTALL_DIR}/config.json"
  done_ "wrote ${INSTALL_DIR}/config.json (from config.example.json)"
fi

step "installing systemd units"
install -m 0644 "${SRC_DIR}/examples/nutshutdown-web.service" \
                /etc/systemd/system/nutshutdown-web.service
mkdir -p /etc/systemd/system/nut-driver@.service.d/
install -m 0644 "${SRC_DIR}/examples/nut-driver-timeout.conf" \
                /etc/systemd/system/nut-driver@.service.d/override.conf
systemctl daemon-reload
done_ "systemd units installed and daemon reloaded"

step "installing per-minute cron line"
CRON_LINE="* * * * * /usr/bin/python3 ${INSTALL_DIR}/power_monitor.py"
if crontab -l 2>/dev/null | grep -Fq "${INSTALL_DIR}/power_monitor.py"; then
  done_ "cron line already present"
else
  ( crontab -l 2>/dev/null || true; echo "${CRON_LINE}" ) | crontab -
  done_ "added: ${CRON_LINE}"
fi

cat <<EOF

${GREEN}Install complete.${RESET}

Next steps — these can't be automated because they depend on your hardware:

  ${CYAN}1.${RESET} Configure NUT (~5 minutes, one-time).
      cp ${SRC_DIR}/examples/nut.conf.sample  /etc/nut/nut.conf
      cp ${SRC_DIR}/examples/ups.conf.sample  /etc/nut/ups.conf
      cp ${SRC_DIR}/examples/upsd.conf.sample /etc/nut/upsd.conf
      nano /etc/nut/ups.conf       # set port=<NMC_IP>, community=<COMMUNITY>
      nano /etc/nut/upsd.conf      # set LISTEN <this-host-LAN-IP>
      systemctl enable --now nut.target
      upsc ups-main                # smoke test — should dump UPS variables

  ${CYAN}2.${RESET} Edit ${INSTALL_DIR}/config.json.
      Set web.password (the default is intentionally invalid).
      Make ups_name match the section name in /etc/nut/ups.conf.
      Leave hosts empty — you'll add them via the UI in step 5.

  ${CYAN}3.${RESET} Generate an SSH key for the orchestrator.
      ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""
      # then in config.json set ssh.key_path = "/root/.ssh/id_ed25519"

  ${CYAN}4.${RESET} Start the web UI.
      systemctl enable --now nutshutdown-web.service
      # then open http://<this-host>:8080

  ${CYAN}5.${RESET} Add each Proxmox host via the web UI's add-host row
      (tick "push SSH key on add" — supply the host's root password once).

Re-run this script any time to upgrade the code in ${INSTALL_DIR}. Your
config.json is never overwritten.
EOF
