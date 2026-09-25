#!/bin/sh
# Installed by the github_runner_cluster Ansible role beside Litestream's configuration, and run in the Litestream image for Headscale's database. Usage: litestream.sh restore|replicate|follow
# restore runs before Headscale starts: it restores the database from the replica when this host has none, and puts the server's keys in place; with GITHUB_RUNNER_LITESTREAM_FORCE=1 (promotion) it replaces whatever database the host holds with the replica's latest state, and fails if the replica is empty. replicate is the sidecar that streams every change to the replica. follow is the warm standby's copy, kept up to date from the replica without touching Headscale's own state.
set -eu

config_dir=/etc/github-runner-litestream
keys_dir=/etc/github-runner-headscale-keys
config="$config_dir/litestream.yml"
state=/var/lib/headscale
db="$state/db.sqlite"
standby_db=/var/lib/headscale-standby/db.sqlite

# The replica's credentials, kept out of the configuration file and the container's environment.
if [ -f "$config_dir/litestream.env" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$config_dir/litestream.env"
  set +a
fi

case "${1:-}" in
  restore)
    force="${GITHUB_RUNNER_LITESTREAM_FORCE:-0}"
    mkdir -p "$state"
    # The Noise key (and the embedded DERP key) identify the server to its nodes and are not in the database.
    for key in "$keys_dir"/*; do
      [ -f "$key" ] || continue
      target="$state/$(basename "$key")"
      if [ "$force" = 1 ] || [ ! -s "$target" ]; then
        cp "$key" "$target"
        chmod 0600 "$target"
      fi
    done
    if [ "$force" = 1 ]; then
      # Litestream's own tracking directory goes too, so the promoted server starts from the restored database alone.
      rm -rf "$db" "$db-wal" "$db-shm" "$state/.db.sqlite-litestream"
      exec litestream restore -config "$config" -integrity-check quick "$db"
    fi
    exec litestream restore -config "$config" -if-db-not-exists -if-replica-exists -integrity-check quick "$db"
    ;;
  replicate)
    exec litestream replicate -config "$config"
    ;;
  follow)
    mkdir -p "$(dirname "$standby_db")"
    # Returns at once while the replica is still empty; the pause keeps the container's restart policy from spinning.
    litestream restore -config "$config" -f -if-replica-exists -o "$standby_db" "$db"
    sleep 30
    ;;
  *)
    echo "usage: $0 restore|replicate|follow" >&2
    exit 64
    ;;
esac
