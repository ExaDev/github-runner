#!/bin/bash
# Real systemd, real NetworkManager and the real transaction and watchdog modules: does the host-owned worker roll back a change whose owner crashed?
set -u
PORT=/dev/virtio-ports/test.nm.fixture
STATE=/var/lib/github-runner-connectivity
UNIT=github-runner-connectivity-watchdog
mkdir -p /run/nm-isolated-test && touch /run/nm-isolated-test/disposable-vm
systemctl daemon-reload
systemctl start NetworkManager
for _ in $(seq 1 60); do nmcli general status >/dev/null 2>&1 && break; sleep 0.5; done
systemctl start $UNIT
for _ in $(seq 1 40); do [ -S $STATE/watchdog.sock ] && break; sleep 0.5; done
python3 - >/run/nm-isolated-test/wd.json 2>/run/nm-isolated-test/wd.err <<'PY'
import json, os, subprocess, sys, time
sys.path.insert(0, "/usr/local/lib/github-runner-connectivity")
from pathlib import Path
from connectivity_transaction import ConnectivityTransaction
from connectivity_watchdog import probe_worker
STATE = Path("/var/lib/github-runner-connectivity")
DROPIN = Path("/etc/NetworkManager/conf.d/zz-test-connectivity.conf")
CONTENT = b"[connectivity]\nenabled=false\n"
UNIT = "github-runner-connectivity-watchdog"
out = {}
clock = lambda: time.clock_gettime(time.CLOCK_BOOTTIME)
boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
def reload():
    subprocess.run(["nmcli", "general", "reload", "conf"], check=True, timeout=30)
class Scheduler:
    def arm(self, tid, gen, deadline):
        pass
    def is_armed(self, tid, gen, deadline):
        return probe_worker(STATE, tid, gen, deadline)
def phase():
    try:
        return json.loads((STATE / "connectivity-transaction.json").read_text())["phase"]
    except Exception as e:
        return "unreadable:" + type(e).__name__
def main_pid():
    return subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", UNIT], capture_output=True, text=True).stdout.strip()
def begin(tid, seconds):
    ConnectivityTransaction(STATE, reload, exclusive_writer=True).begin(tid, DROPIN, CONTENT, deadline=clock() + seconds, scheduler=Scheduler(), now=clock, boot_id=boot_id)
def wait(cond, limit):
    end = time.time() + limit
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.5)
    return False
def runtime():
    r = subprocess.run(["busctl", "get-property", "org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager", "ConnectivityCheckEnabled"], capture_output=True, text=True)
    return r.stdout.strip()
out["worker_active"] = subprocess.run(["systemctl", "is-active", UNIT], capture_output=True, text=True).stdout.strip()
out["worker_pid_0"] = main_pid()
out["runtime_before"] = runtime()
# A: the owner of the change crashes before committing; the worker must restore.
begin("tA", 12)
out["A_after_begin"] = {"dropin": DROPIN.exists(), "phase": phase(), "runtime": runtime()}
t0 = time.time()
out["A_restored"] = wait(lambda: not DROPIN.exists() and phase() == "rolled_back", 60)
out["A_seconds"] = round(time.time() - t0)
out["A_runtime_after"] = runtime()
# B: the owner crashes and the worker is killed too; systemd restarts it and it must still restore.
begin("tB", 15)
pid1 = main_pid()
subprocess.run(["systemctl", "kill", "-s", "KILL", UNIT])
out["B_restarted"] = wait(lambda: main_pid() not in ("", "0", pid1), 20)
out["B_restored"] = wait(lambda: not DROPIN.exists() and phase() == "rolled_back", 60)
# C: the owner commits in time; the worker must leave the change alone.
begin("tC", 12)
ConnectivityTransaction(STATE, reload, exclusive_writer=True).commit("tC", now=clock, boot_id=boot_id)
time.sleep(20)
out["C_after_deadline"] = {"dropin": DROPIN.exists(), "phase": phase()}
if DROPIN.exists():
    DROPIN.unlink()
print(json.dumps(out))
PY
sc=$(tr '\012\042\134' '  _' < /run/nm-isolated-test/wd.json | cut -c1-1800)
er=$(tail -c 900 /run/nm-isolated-test/wd.err 2>/dev/null | tr '\012\042\134' '  _')
jl=$(journalctl -u $UNIT --no-pager 2>/dev/null | tail -4 | tr '\012\042\134' '  _' | cut -c1-500)
printf '{"watchdog":"%s","err":"%s","unit_log":"%s"}\n' "$sc" "$er" "$jl" > $PORT
