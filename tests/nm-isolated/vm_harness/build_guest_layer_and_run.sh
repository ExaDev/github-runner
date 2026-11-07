set -eu
ROOT=~/Developer/ExaDev/Internal/github-runner-worktrees/nm-isolated-vm
cd $ROOT && rm -rf build2 && mkdir build2 && cd build2
python3 - <<'PY'
import base64, hashlib, json, os, sys
root = os.path.expanduser("~/Developer/ExaDev/Internal/github-runner-worktrees/nm-isolated-vm")
sys.path.insert(0, root + "/fixture")
import guest_provision as gp
files, digests = [], {}
for name in gp.FILES:
    data = open(os.path.join(root + "/fixture", name), "rb").read()
    digests[name] = hashlib.sha256(data).hexdigest()
    files.append({"path": f"{gp.DESTINATION}/{name}", "encoding": "b64", "content": base64.b64encode(data).decode(), "permissions": "0755" if name.endswith(".sh") else "0644"})
smoke = open(os.path.expanduser('~/Developer/ExaDev/Internal/github-runner-worktrees/nm-isolated-vm/smoke.sh')).read()
files.append({"path": gp.DESTINATION + "/DIGESTS", "content": "".join(f"{n} {h}\n" for n, h in digests.items()), "permissions": "0644"})
files.append({"path": gp.DESTINATION + "/smoke.sh", "content": smoke, "permissions": "0755"})
files.append({"path": "/etc/systemd/system/nm-smoke.service", "content": "[Unit]\nDescription=Fixture smoke\nAfter=multi-user.target\n[Service]\nType=oneshot\nExecStart=/opt/nm-isolated-test/smoke.sh\n[Install]\nWantedBy=multi-user.target\n", "permissions": "0644"})
ud = {"write_files": files, "runcmd": [["systemctl", "enable", "nm-smoke.service"]], "power_state": {"mode": "poweroff", "timeout": 30, "condition": True}}
open("user-data", "w").write("#cloud-config\n" + json.dumps(ud))
open("meta-data", "w").write("instance-id: nm-golden-23\nlocal-hostname: nm-golden\n")
print("files", len(files))
PY
cloud-localds seed2.img user-data meta-data
qemu-img create -f qcow2 -F qcow2 -b $ROOT/golden-nm-1.54.3.qcow2 build2.qcow2 >/dev/null
sg kvm -c "timeout 600 qemu-system-x86_64 -enable-kvm -cpu host -m 2048 -smp 2 -display none -serial file:console.log -monitor none -nic none -drive file=build2.qcow2,if=virtio,format=qcow2 -drive file=seed2.img,if=virtio,format=raw,readonly=on" </dev/null; echo "qemu-exit=$?"
cd $ROOT && chmod u+w golden2-nm-fixture.qcow2 2>/dev/null; rm -f golden2-nm-fixture.qcow2; qemu-img convert -O qcow2 build2/build2.qcow2 golden2-nm-fixture.qcow2 && chmod a-w golden2-nm-fixture.qcow2
cd fixture && sg kvm -c "python3 - <<'PY'
import os, shutil, socket, tempfile, time, qemu_vm
root = os.path.expanduser('~/Developer/ExaDev/Internal/github-runner-worktrees/nm-isolated-vm')
w = tempfile.mkdtemp(prefix='nmw', dir=root); os.chmod(w, 0o700)
with qemu_vm.disposable_vm(root + '/golden2-nm-fixture.qcow2', w, qemu='/usr/bin/qemu-system-x86_64', qemu_img='/usr/bin/qemu-img') as (name, overlay, process):
    for _ in range(100):
        if os.path.exists(w + '/guest.sock'): break
        time.sleep(0.1)
    s = socket.socket(socket.AF_UNIX); s.settimeout(90); s.connect(w + '/guest.sock')
    buf = b''
    while b'\n' not in buf:
        c = s.recv(4096)
        if not c: break
        buf += c
    print('guest line:', buf.decode(errors='replace').strip())
shutil.rmtree(w, ignore_errors=True)
PY"
