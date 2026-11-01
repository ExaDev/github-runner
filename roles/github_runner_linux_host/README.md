# github_runner_linux_host

The host-level baseline of a Linux host that carries a k3s node, for a machine that is dedicated to the cluster. It does nothing on any other operating system, so it can be applied to a mixed inventory, and both of its steps are off by default, because each changes how the host behaves for whoever else uses it. `playbooks/site.yml` runs it before the fleet play, on every host of the cluster group; a fleet turns the steps on per host in `host_vars`. It touches neither the node nor the host's Tailscale client, which `github_runner_cluster` owns (see its [Native nodes](../github_runner_cluster/README.md#native-nodes)).

Its tasks that change the host use `become` on their own, so the connecting user needs passwordless sudo, or the play needs `--ask-become-pass`. Everything the role only reads runs as the connecting user, and under `--check` nothing is changed and each change a real run would make is reported. Every variable is checked against `meta/argument_specs.yml` when the role runs.

## Keeping the host awake

`github_runner_linux_host_keep_awake: true` masks `sleep.target`, `suspend.target`, `hibernate.target` and `hybrid-sleep.target`, and writes a systemd-logind drop-in (`/etc/systemd/logind.conf.d/90-github-runner-keep-awake.conf`) that sets `HandleLidSwitch`, `HandleLidSwitchExternalPower` and `HandleLidSwitchDocked` to `ignore`, then has logind read it with `SIGHUP`, which reloads its configuration without ending the login sessions it tracks. A host that suspends drops off the network, so its node leaves the cluster and its runner pods stop; a laptop suspends whenever its lid closes (logind's default for `HandleLidSwitch` is `suspend`), and its desktop or power button can ask logind to suspend it as well. Masking is what stops a request that does not come from the lid: the target cannot be started, so `systemctl suspend` fails. Both parts are idempotent, and the role checks what is masked before it masks anything.

## Wi-Fi power save

`github_runner_linux_host_wifi_powersave_off: true` tells NetworkManager not to power-save Wi-Fi. A radio in power-save mode sleeps between beacons and delays or drops packets, which on a node of the cluster shows as the node flapping `NotReady`. It matters only for a host on Wi-Fi, and does nothing to a host without a Wi-Fi device; on a host without NetworkManager it fails, since the setting is explicitly asked for.

It writes `/etc/NetworkManager/conf.d/zz-github-runner-wifi-powersave.conf` with `wifi.powersave = 2` (disable), which sets the default for every connection that does not set its own, and has NetworkManager read it (`nmcli general reload conf`). NetworkManager applies a connection's power save setting each time it activates the connection, so that is what keeps it off across reconnects and reboots. The drop-in's name sorts after Ubuntu's `default-wifi-powersave-on.conf` (`wifi.powersave = 3`), because NetworkManager reads its configuration files in alphabetical order and the last one to set a value wins. To switch it off on a radio that is running now, the role runs `iw dev <device> set power_save off` for each Wi-Fi device that reports it on, which changes the radio without taking the link down; `nmcli device reapply` does not change a running radio. A host without `iw` is told so in the run, and gets the setting at the next activation of its connection.

A Wi-Fi-only node still drops when the radio does. These settings only stop the radio adding drops of its own; see [A Wi-Fi-only host](../github_runner_cluster/README.md#a-wi-fi-only-host).

## Variables

- `github_runner_linux_host_keep_awake` (default `false`): mask the sleep targets and have logind ignore the lid switch.
- `github_runner_linux_host_wifi_powersave_off` (default `false`): turn Wi-Fi power save off in NetworkManager and on the radio.
- `github_runner_linux_host_sleep_targets` and `github_runner_linux_host_lid_switch_settings`: the targets masked and the logind settings set to `ignore`. Constants, not settings; variables only so the tests can narrow them.
- `github_runner_linux_host_logind_dropin` and `github_runner_linux_host_networkmanager_dropin`: where the two drop-ins go. Not settings; variables only so the tests can point them at a stand-in.

## Tests

`tests/unit/test_linux_host.py` runs the role (`tests/linux_host/run.yml`) against stand-ins for `systemctl`, `nmcli` and `iw` on `PATH`, with the drop-ins and the host's facts redirected: both steps off by default and on request, idempotence, `--check` changing nothing and reporting each change, only unmasked targets masked, the logind reload only when the drop-in changed, a host without Wi-Fi or without NetworkManager, a non-Linux host left alone, and the defaults, argument specs and this README agreeing.
