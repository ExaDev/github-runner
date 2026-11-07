#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'This fixture requires root inside a disposable VM.' >&2
    exit 1
fi
if ! systemd-detect-virt --vm --quiet; then
    printf '%s\n' 'Refusing: systemd does not identify a VM.' >&2
    exit 1
fi
if [ ! -f /run/nm-isolated-test/disposable-vm ] || [ -L /run/nm-isolated-test/disposable-vm ]; then
    printf '%s\n' 'Refusing: disposable VM sentinel is missing.' >&2
    exit 1
fi
printf '%s\n' 'Fixture incomplete: no private bus, daemon or Update2 client is launched.' >&2
exit 1
