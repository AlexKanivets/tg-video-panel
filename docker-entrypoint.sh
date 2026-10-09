#!/bin/sh
# Папки data и videos монтируются с хоста и могут принадлежать root — отдаём их пользователю tvp
# и запускаем панель уже без прав root.
set -e
if [ "$(id -u)" = "0" ]; then
  for d in "${TVP_DATA_DIR:-/data}" "${TVP_DOWNLOAD_DIR:-/videos}"; do
    mkdir -p "$d"
    [ "$(stat -c %u "$d")" = "$(id -u tvp)" ] || chown -R tvp:tvp "$d"
  done
  exec setpriv --reuid=tvp --regid=tvp --init-groups "$@"
fi
exec "$@"
