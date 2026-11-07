#!/bin/bash
# Real-NetworkManager scenario in the disposable guest: a non-root Update2 is held by the PolicyKit stub, the client disconnects, authorisation is then released; the report says whether NetworkManager applied it anyway.
set -u
D=/opt/nm-isolated-test
PORT=/dev/virtio-ports/test.nm.fixture
RUN=/run/nm-isolated-test
ADDR=unix:path=$RUN/bus
mkdir -p $RUN && touch $RUN/disposable-vm
id nm-isolated >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --no-create-home nm-isolated
systemctl stop NetworkManager 2>/dev/null
dbus-daemon --config-file=$D/bus.conf --nofork >$RUN/bus.log 2>&1 &
BUSPID=$!
for _ in $(seq 1 50); do [ -S $RUN/bus ] && break; sleep 0.1; done
chmod 0666 $RUN/bus 2>/dev/null
export DBUS_SYSTEM_BUS_ADDRESS=$ADDR
python3 $D/polkit_stub.py --private-bus-address $ADDR >$RUN/stub.log 2>&1 &
STUBPID=$!
modprobe dummy 2>/dev/null; ip link add dummy0 type dummy 2>$RUN/dummy.err && ip link set dummy0 up
NetworkManager --no-daemon --log-level=INFO --log-domains=ALL >$RUN/nm.log 2>&1 &
NMPID=$!
export NMPID
python3 - >$RUN/scenario.json 2>$RUN/scenario.err <<'PY'
import dbus, json, subprocess, time, os
ADDR = "unix:path=/run/nm-isolated-test/bus"
out = {}
def conn():
    return dbus.bus.BusConnection(ADDR)
b = None
for _ in range(100):
    try:
        b = conn()
        s = b.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings")
        dbus.Interface(s, "org.freedesktop.NetworkManager.Settings").ListConnections()
        break
    except dbus.DBusException:
        time.sleep(0.3)
out["nm_ready"] = b is not None
settings_if = dbus.Interface(b.get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings"), "org.freedesktop.NetworkManager.Settings")
ethernet = "802-3-ethernet"
def make(uuid):
    prof = {"connection": {"id": "fixture-" + uuid[:4], "type": ethernet, "uuid": uuid, "autoconnect": False},
            ethernet: {}, "ipv4": {"method": "auto", "route-metric": dbus.Int64(100)}, "ipv6": {"method": "ignore"}}
    path, _ = settings_if.AddConnection2(prof, dbus.UInt32(2), {})
    return str(path)
def conn_if(path):
    return dbus.Interface(conn().get_object("org.freedesktop.NetworkManager", path), "org.freedesktop.NetworkManager.Settings.Connection")
def metric(path):
    return int(conn_if(path).GetSettings()["ipv4"].get("route-metric", -1))
def version(path):
    o = conn().get_object("org.freedesktop.NetworkManager", path)
    try:
        return int(dbus.Interface(o, "org.freedesktop.DBus.Properties").Get("org.freedesktop.NetworkManager.Settings.Connection", "VersionId"))
    except dbus.DBusException as e:
        return "n/a:" + e.get_dbus_name()
ctl_bus = conn()
for _ in range(100):
    if "org.freedesktop.PolicyKit1" in [str(n) for n in ctl_bus.list_names()]:
        break
    time.sleep(0.2)
out["polkit_stub_on_bus"] = "org.freedesktop.PolicyKit1" in [str(n) for n in ctl_bus.list_names()]
ctl = dbus.Interface(ctl_bus.get_object("org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority"), "test.nm.IsolatedControl")
def case(label, uuid, unfenced, root_metric):
    path = make(uuid)
    c = {"v0": version(path), "m0": metric(path)}
    args = ["runuser", "-u", "nm-isolated", "--", "python3", "/opt/nm-isolated-test/update2_client.py", "--private-bus-address", ADDR, "--profile-path", path, "--metric", "150", "--disconnect-after-held", "--timeout", "30"]
    if unfenced:
        args.append("--unfenced")
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    c["client_rc"] = r.returncode
    if r.returncode:
        c["client_err"] = r.stderr.strip()[-160:]
    time.sleep(1)
    pending = [str(x) for x in ctl.Pending()]
    c["pending"] = len(pending)
    if root_metric is not None:
        s2 = conn_if(path).GetSettings()
        new = {k: {kk: vv for kk, vv in v.items()} for k, v in s2.items()}
        new["ipv4"]["route-metric"] = dbus.Int64(root_metric)
        conn_if(path).Update2(new, dbus.UInt32(2), {})
        c["root_set"] = root_metric
        c["v_after_root"] = version(path)
    for cid in pending:
        ctl.Release(cid, True)
    time.sleep(2)
    c["metric_final"] = metric(path)
    c["v_final"] = version(path)
    out[label] = c
case("A_fenced_no_interference", "aaaaaaaa-0000-0000-0000-000000000001", False, None)
case("B_fenced_after_root_change", "bbbbbbbb-0000-0000-0000-000000000002", False, 200)
case("C_unfenced_after_root_change", "cccccccc-0000-0000-0000-000000000003", True, 200)

def nm_ready(timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        try:
            dbus.Interface(conn().get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings"), "org.freedesktop.NetworkManager.Settings").ListConnections()
            return True
        except dbus.DBusException:
            time.sleep(0.3)
    return False
def uuid_of(pp):
    return str(conn_if(pp).GetSettings()["connection"]["uuid"])[:8]
def restart_nm():
    pid = int(os.environ["NMPID"])
    os.kill(pid, 15)
    for _ in range(100):
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except OSError:
            break
    p = subprocess.Popen(["NetworkManager", "--no-daemon", "--log-level=INFO", "--log-domains=ALL"], stdout=open("/run/nm-isolated-test/nm2.log", "w"), stderr=subprocess.STDOUT)
    os.environ["NMPID"] = str(p.pid)
    return nm_ready()
def set_metric(path, value, version_id=None):
    s2 = conn_if(path).GetSettings()
    new = {k: {kk: vv for kk, vv in v.items()} for k, v in s2.items()}
    new["ipv4"]["route-metric"] = dbus.Int64(value)
    args = {"version-id": dbus.UInt64(version_id)} if version_id is not None else {}
    conn_if(path).Update2(new, dbus.UInt32(1), args)
# Case D: persistent profile; a client reads its version, NetworkManager restarts, root changes the profile, then the client writes with the old version.
E = {}
prof = {"connection": {"id": "fixture-persist", "type": ethernet, "uuid": "dddddddd-0000-0000-0000-000000000004", "autoconnect": False},
        ethernet: {}, "ipv4": {"method": "auto", "route-metric": dbus.Int64(100)}, "ipv6": {"method": "ignore"}}
path, _ = settings_if.AddConnection2(prof, dbus.UInt32(1), {})
path = str(path)
set_metric(path, 110)
E["v_client_read"] = version(path)
E["m_client_read"] = metric(path)
E["owner_before"] = str(conn().get_name_owner("org.freedesktop.NetworkManager"))
E["restarted"] = restart_nm()
E["owner_after"] = str(conn().get_name_owner("org.freedesktop.NetworkManager"))
paths = [str(x) for x in dbus.Interface(conn().get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings"), "org.freedesktop.NetworkManager.Settings").ListConnections()]
E["profile_survived"] = path in paths
E["old_path_now_uuid"] = uuid_of(path) if path in paths else None
path = [pp for pp in paths if str(conn_if(pp).GetSettings()["connection"]["uuid"]) == "dddddddd-0000-0000-0000-000000000004"][0]
E["profiles_after_restart"] = {pp.rsplit("/", 1)[-1]: [uuid_of(pp), metric(pp)] for pp in paths}
E["v_after_restart"] = version(path)
E["m_after_restart"] = metric(path)
set_metric(path, 200)
E["v_after_root_change"] = version(path)
E["m_after_root_change"] = metric(path)
try:
    set_metric(path, 150, version_id=E["v_client_read"] if isinstance(E["v_client_read"], int) else None)
    E["stale_write"] = "accepted"
except dbus.DBusException as e:
    E["stale_write"] = "rejected:" + e.get_dbus_name().split(".")[-1]
E["m_final"] = metric(path)
out["D_restart_version_reuse"] = E

# Case E: checkpoint expiry against a held write.
settings_if = dbus.Interface(conn().get_object("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager/Settings"), "org.freedesktop.NetworkManager.Settings")
ctl_bus = conn()
ctl = dbus.Interface(ctl_bus.get_object("org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority"), "test.nm.IsolatedControl")
NMS = "org.freedesktop.NetworkManager"
mgr = dbus.Interface(conn().get_object(NMS, "/org/freedesktop/NetworkManager"), NMS)
E2 = {}
try:
    dev = str(mgr.GetDeviceByIpIface("dummy0"))
    E2["device"] = dev.rsplit("/", 1)[-1]
except dbus.DBusException as e:
    dev = None
    E2["device_error"] = e.get_dbus_name().split(".")[-1]
def checkpoints():
    o = conn().get_object(NMS, "/org/freedesktop/NetworkManager")
    return len(dbus.Interface(o, "org.freedesktop.DBus.Properties").Get(NMS, "Checkpoints"))
def make_generic(uuid):
    prof = {"connection": {"id": "gen-" + uuid[:4], "type": "generic", "uuid": uuid, "interface-name": "dummy0", "autoconnect": False},
            "generic": {}, "ipv4": {"method": "disabled", "route-metric": dbus.Int64(100)}, "ipv6": {"method": "ignore"}}
    pth, _ = settings_if.AddConnection2(prof, dbus.UInt32(1), {})
    return str(pth)
if dev:
    # E1: does a checkpoint roll back a persistent profile's contents?
    p1 = make_generic("eeeeeeee-0000-0000-0000-000000000005")
    cp = str(mgr.CheckpointCreate([dbus.ObjectPath(dev)], dbus.UInt32(4), dbus.UInt32(0)))
    E2["E1_checkpoint"] = cp.rsplit("/", 1)[-1]
    E2["E1_metric_at_checkpoint"] = metric(p1)
    set_metric(p1, 120)
    E2["E1_metric_after_change"] = metric(p1)
    time.sleep(8)
    E2["E1_checkpoints_left"] = checkpoints()
    E2["E1_metric_after_expiry"] = metric(p1)
    # E2: a held fenced write that lands after the checkpoint expired.
    p2 = make_generic("ffffffff-0000-0000-0000-000000000006")
    cp = mgr.CheckpointCreate([dbus.ObjectPath(dev)], dbus.UInt32(5), dbus.UInt32(0))
    E2["E2_metric_at_checkpoint"] = metric(p2)
    r = subprocess.run(["runuser", "-u", "nm-isolated", "--", "python3", "/opt/nm-isolated-test/update2_client.py", "--private-bus-address", ADDR, "--profile-path", p2, "--metric", "150", "--disconnect-after-held", "--timeout", "30"], capture_output=True, text=True, timeout=60)
    E2["E2_client_rc"] = r.returncode
    time.sleep(1)
    pend = [str(x) for x in ctl.Pending()]
    E2["E2_pending"] = len(pend)
    time.sleep(7)
    E2["E2_checkpoints_left_before_release"] = checkpoints()
    E2["E2_metric_before_release"] = metric(p2)
    E2["E2_version_before_release"] = version(p2)
    for cid in pend:
        ctl.Release(cid, True)
    time.sleep(2)
    E2["E2_metric_final"] = metric(p2)
    E2["E2_version_final"] = version(p2)
out["E_checkpoint_expiry"] = E2

# Case F: device checkpoint plus Reapply on an ACTIVE connection, the mechanism the offline transaction module uses.
F = {}
def routes():
    r = subprocess.run(["ip", "-4", "route", "show", "dev", "dummy0"], capture_output=True, text=True)
    return " | ".join(x.strip() for x in r.stdout.splitlines())[:160]
if dev:
    prof = {"connection": {"id": "gen-act", "type": "dummy", "uuid": "99999999-0000-0000-0000-000000000009", "interface-name": "dummy0", "autoconnect": False},
            "dummy": {}, "ipv6": {"method": "ignore"},
            "ipv4": {"method": "manual", "route-metric": dbus.Int64(100),
                     "address-data": dbus.Array([dbus.Dictionary({"address": "10.99.0.2", "prefix": dbus.UInt32(24)}, signature="sv")], signature="a{sv}")}}
    pa, _ = settings_if.AddConnection2(prof, dbus.UInt32(1), {})
    mgr.ActivateConnection(pa, dbus.ObjectPath(dev), dbus.ObjectPath("/"))
    devi = dbus.Interface(conn().get_object(NMS, dev), NMS + ".Device")
    devp = dbus.Interface(conn().get_object(NMS, dev), "org.freedesktop.DBus.Properties")
    state = 0
    for _ in range(100):
        state = int(devp.Get(NMS + ".Device", "State"))
        if state == 100:
            break
        time.sleep(0.2)
    F["device_state"] = state
    def applied_metric():
        a, v = devi.GetAppliedConnection(dbus.UInt32(0))
        return int(a["ipv4"].get("route-metric", -1)), int(v)
    if state == 100:
        F["applied_before"], v0 = applied_metric()
        F["routes_before"] = routes()
        cp = mgr.CheckpointCreate([dbus.ObjectPath(dev)], dbus.UInt32(5), dbus.UInt32(0))
        a, v = devi.GetAppliedConnection(dbus.UInt32(0))
        new = {k: {kk: vv for kk, vv in val.items()} for k, val in a.items()}
        new["ipv4"]["route-metric"] = dbus.Int64(150)
        devi.Reapply(new, dbus.UInt64(v), dbus.UInt32(0))
        F["applied_after_reapply"], _ = applied_metric()
        F["routes_after_reapply"] = routes()
        time.sleep(9)
        F["checkpoints_left"] = checkpoints()
        F["applied_after_expiry"], _ = applied_metric()
        F["routes_after_expiry"] = routes()
        F["profile_metric_after_expiry"] = metric(str(pa))
out["F_checkpoint_reapply_active"] = F

# Case G: a checkpoint spanning a NetworkManager restart (a crash in the middle of a change).
G = {}
if dev and F.get("device_state") == 100:
    mgr = dbus.Interface(conn().get_object(NMS, "/org/freedesktop/NetworkManager"), NMS)
    cp = mgr.CheckpointCreate([dbus.ObjectPath(dev)], dbus.UInt32(20), dbus.UInt32(0))
    devi = dbus.Interface(conn().get_object(NMS, dev), NMS + ".Device")
    a, v = devi.GetAppliedConnection(dbus.UInt32(0))
    new = {k: {kk: vv for kk, vv in val.items()} for k, val in a.items()}
    new["ipv4"]["route-metric"] = dbus.Int64(180)
    devi.Reapply(new, dbus.UInt64(v), dbus.UInt32(0))
    G["routes_after_reapply"] = routes()
    G["restarted"] = restart_nm()
    G["routes_just_after_restart"] = routes()
    time.sleep(25)
    mgr2 = dbus.Interface(conn().get_object(NMS, "/org/freedesktop/NetworkManager"), NMS)
    G["checkpoints_after_restart"] = checkpoints()
    G["routes_after_original_timeout"] = routes()
    dev2 = str(mgr2.GetDeviceByIpIface("dummy0"))
    d2p = dbus.Interface(conn().get_object(NMS, dev2), "org.freedesktop.DBus.Properties")
    G["device_state_after"] = int(d2p.Get(NMS + ".Device", "State"))
    pf = [pp for pp in dbus.Interface(conn().get_object(NMS, "/org/freedesktop/NetworkManager/Settings"), NMS + ".Settings").ListConnections() if str(conn_if(pp).GetSettings()["connection"]["uuid"]).startswith("99999999")]
    G["profile_metric_after"] = metric(str(pf[0])) if pf else None
out["G_checkpoint_across_restart"] = G

# Case H: a writer outside NetworkManager (edits the keyfile and reloads) while a fenced write is held.
H = {}
if dev:
    settings_if = dbus.Interface(conn().get_object(NMS, "/org/freedesktop/NetworkManager/Settings"), NMS + ".Settings")
    ctl_bus = conn()
    ctl = dbus.Interface(ctl_bus.get_object("org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority"), "test.nm.IsolatedControl")
    ph = make_generic("a8a8a8a8-0000-0000-0000-000000000008")
    H["v0"], H["m0"] = version(ph), metric(ph)
    r = subprocess.run(["runuser", "-u", "nm-isolated", "--", "python3", "/opt/nm-isolated-test/update2_client.py", "--private-bus-address", ADDR, "--profile-path", ph, "--metric", "150", "--disconnect-after-held", "--timeout", "30"], capture_output=True, text=True, timeout=60)
    H["client_rc"] = r.returncode
    time.sleep(1)
    pend = [str(x) for x in ctl.Pending()]
    H["pending"] = len(pend)
    import glob, re
    g = subprocess.run(["grep", "-rl", "a8a8a8a8", "/etc", "/run/NetworkManager", "/var/lib/NetworkManager"], capture_output=True, text=True)
    files = [f for f in g.stdout.split() if os.path.isfile(f)]
    H["keyfile_paths"] = files[:3]
    for f in files[:2]:
        try:
            H["dump_" + os.path.basename(f)[:24]] = open(f).read().replace("\n", " ; ")[:420]
        except OSError as e:
            H["dump_err"] = str(e)
    H["keyfile_found"] = len(files)
    yml = [f for f in files if f.endswith(".yaml")]
    if yml:
        txt = open(yml[0]).read()
        H["yaml_has_metric"] = [l.strip() for l in txt.splitlines() if "route-metric" in l][:2]
        new = re.sub(r'(ipv4\.route-metric:\s*)["\']?\d+["\']?', r'\g<1>"300"', txt)
        H["yaml_changed"] = new != txt
        open(yml[0], "w").write(new)
        os.chmod(yml[0], 0o600)
        g = subprocess.run(["netplan", "generate"], capture_output=True, text=True)
        H["netplan_generate_rc"] = g.returncode
        if g.returncode:
            H["netplan_generate_err"] = g.stderr.strip()[-160:]
        dbus.Interface(conn().get_object(NMS, "/org/freedesktop/NetworkManager/Settings"), NMS + ".Settings").ReloadConnections()
        time.sleep(2)
        ph2 = [pp for pp in settings_if.ListConnections() if str(conn_if(pp).GetSettings()["connection"]["uuid"]).startswith("a8a8a8a8")]
        ph = str(ph2[0]) if ph2 else ph
        H["after_external_edit_metric"], H["after_external_edit_version"] = metric(ph), version(ph)
    for cid in pend:
        ctl.Release(cid, True)
    time.sleep(2)
    H["final_metric"], H["final_version"] = metric(ph), version(ph)
out["H_external_writer"] = H
print(json.dumps(out))
PY
sc=$(tr '\012\042\134' '  _' < $RUN/scenario.json | cut -c1-5200)
er=$(tail -c 1100 $RUN/scenario.err 2>/dev/null | tr '\012\042\134' '  _')
sl=$(tail -c 200 $RUN/stub.log 2>/dev/null | tr '\012\042\134' '  _')
printf '{"scenario":"%s","err":"%s","stub":"%s","dummy":"%s"}\n' "$sc" "$er" "$sl" "$(cat $RUN/dummy.err 2>/dev/null | tr '\012\042\134' '  _' | cut -c1-120)" > $PORT
kill $NMPID $STUBPID $BUSPID 2>/dev/null
