#!/bin/sh
set -eu

if [ "$(id -u)" != 0 ]; then
    echo "Run this installer as root." >&2
    exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ required"'
command -v git >/dev/null
docker compose version >/dev/null
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
app_dir=${APP_DIR:-/opt/FreelanceNotification}
test -d "$app_dir/.git"
test -f "$app_dir/.env"
test -d "$app_dir/data"
docker inspect fh-bots-notifier >/dev/null

install -d -m 700 /var/lib/freelancenotify-deploy
install -m 755 "$script_dir/deploy.py" /usr/local/sbin/freelancenotify-deploy
install -m 644 "$script_dir/freelancenotify-deploy.service" /etc/systemd/system/
install -m 644 "$script_dir/freelancenotify-deploy.timer" /etc/systemd/system/
if [ ! -f /etc/default/freelancenotify-deploy ]; then
    printf 'APP_DIR=%s\nDEPLOY_BRANCH=main\n' "$app_dir" > /etc/default/freelancenotify-deploy
    chmod 600 /etc/default/freelancenotify-deploy
fi
chmod 600 "$app_dir/.env"
systemctl daemon-reload
systemctl enable --now freelancenotify-deploy.timer
echo "Autodeploy installed. Logs: journalctl -u freelancenotify-deploy.service -f"
