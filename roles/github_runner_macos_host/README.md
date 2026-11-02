# github_runner_macos_host

The host-level baseline of a Mac that carries a k3s node, for what the cluster role does not manage: the Mac's power settings, so it never sleeps on AC power, the Mac's own Tailscale client, run as the `tailscaled` system daemon, and, only where asked for, passwordless sudo for the connecting user. It does nothing on any other operating system, so it can be applied to a mixed inventory. `playbooks/site.yml` runs it for every cluster host, before the cluster role.

It never touches the node's containers, or the Tailscale client each node's k3s container runs on the cluster's tailnet (see Tailscale in the repository README). The Mac's own client is a separate device, usually on a different tailnet, and is how an operator reaches the host over SSH.

## Privileges

The tasks that change the host use `become` (sudo), and are the only tasks in the collection that do: the cluster role never does, and the rest of `site.yml` still does not. Run the play with passwordless sudo for the connecting user, or with `--ask-become-pass` (or `ansible_become_password`, from any source Ansible reads). Reading the host and Homebrew, which refuses to run as root, run as the connecting user.

`--check` needs no privilege for the power settings and the Tailscale daemon: the tasks that would use it are not run, and the run reports what they would change instead. The one exception is comparing a sudoers file that already exists, which only root can read, so that comparison runs under `become` in `--check` too (a file that does not exist yet is reported without it).

## Passwordless sudo

Off by default, because it is security-sensitive: a fleet states `github_runner_macos_host_sudoers_nopasswd: true` per host, for the Macs where this role is how the connecting user gets sudo. It writes `/etc/sudoers.d/<user>` (root:wheel, mode 0440) holding `<user> ALL=(ALL) NOPASSWD: ALL`, with `ansible.builtin.copy` under `become` and `validate: /usr/sbin/visudo -cf %s`, so a file that does not parse never replaces the old one and sudo cannot be locked out. A file that already has that content, owner and mode is reported as ok. The user is the one Ansible connects as, unless `github_runner_macos_host_sudoers_user` names another; it must be a plain lower-case login name, since it becomes a file name and a sudoers word.

Leave it off on a host that gets its sudo grant from another tool, such as a device-management agent like JumpCloud, which would otherwise fight the role over the same user.

The first application on a host has no grant to use yet, so it needs the sudo password once, as a bootstrap:

```bash
ansible-playbook -i inventory.yml exadev.github_runner.site --limit example --ask-become-pass
```

Every later run, and every other `become` task in the same run, then needs no password.

## Power settings

A Mac that sleeps drops off the network, so its SSH and Tailscale stop answering and its node leaves the cluster. The role sets the AC Power profile (`pmset -c`; a laptop's battery profile keeps its own) to the values the fleet's always-on Macs run with: never sleep, never sleep the display, never spin the disks down, and wake for network access, standby, Power Nap and TCP keep-alive on.

It reads `pmset -g custom` and runs `pmset -c` only with the settings whose AC Power value differs, so a Mac already in line is untouched and a second run changes nothing. A setting that `pmset -g custom` does not list for the Mac is hardware the Mac lacks, and is skipped.
A MacBook with its lid closed still sleeps with `sleep 0`, because that setting does not cover clamshell sleep: it enters Maintenance Sleep and DarkWake cycles, the VMs and k3s nodes on it see time jump by minutes, and the node goes NotReady. So the role also runs `pmset -a disablesleep 1` (`SleepDisabled 1` in `pmset -g`), which is system-wide and not part of the AC Power profile. It reads `pmset -g` and runs the command only when it does not report `SleepDisabled 1`, so a second run changes nothing and `--check` reports the change without making it. `github_runner_macos_host_disable_sleep: false` leaves the setting as the host has it; the role never turns `disablesleep` back off, so undoing it is `sudo pmset -a disablesleep 0` by hand. It is part of the power settings, so `github_runner_macos_host_power: false` skips it too.

## Tailscale

The role installs the Homebrew formula `tailscale` and runs `tailscaled install-system-daemon`, which copies `tailscaled` to `/usr/local/bin`, writes the launchd daemon `/Library/LaunchDaemons/com.tailscale.tailscaled.plist` and starts it, so the daemon runs from boot, before anyone logs in. A host that already has that file is not installed on again, whether this role or a person installed it, and neither is a host that has Tailscale's standalone app (`/Applications/Tailscale.app`), which is its own Tailscale client in a system network extension and would conflict with a second daemon; and a daemon without the Homebrew formula behind it (one built by hand) is never replaced; what the role does for a host that has the formula is in Keeping Tailscale current below.

The role never joins or re-authenticates a tailnet on its own, because joining needs an auth key, a secret only the tailnet's owner can create. With `github_runner_macos_host_tailscale_auth_key` set, the role reads the daemon's login state and, for a daemon that needs a login, runs `tailscale up` with the key on standard input (so it is in no process listing and, with `no_log`, in no output), plus `github_runner_macos_host_tailscale_up_args`. A daemon that is already logged in is not touched, and a join that failed is retried by the next run. It applies only to a host whose Homebrew formula the role installed (the formula's `tailscale` is in the Homebrew prefix), so a hand-built daemon is never joined or logged in again.

Without the key, join the host by hand once: `sudo tailscale up`, or run the role again with a key.

### Keeping Tailscale current

A Tailscale that was installed once and never updated falls behind: the formula is only installed when absent, and `tailscaled install-system-daemon` copies the formula's `tailscaled` to `/usr/local/bin` (or, on a prefix of `/usr/local` where that path is a link to the formula, starts it once), so upgrading the formula alone leaves the old release running. With `github_runner_macos_host_tailscale_update` (default true) the role, after the install above and before any join, does three things for what the host already has, and installs nothing: it refreshes Homebrew's index (`brew update`) and brings the `tailscale` formula, and no other formula, to its latest release; it runs `tailscaled install-system-daemon` again, once, when the installed daemon's release differs from the formula's or the running daemon's from the formula's, which removes the installed daemon and its launchd definition and installs and starts the new ones ([`cmd/tailscaled/install_darwin.go`](https://github.com/tailscale/tailscale/blob/main/cmd/tailscaled/install_darwin.go), whose uninstall step runs first), and the daemon keeps its login because that lives in its state directory; and when Tailscale's standalone app is installed as the Homebrew cask `tailscale-app`, it keeps that current too (`greedy`, because the app updates itself and Homebrew then lists it as outdated only when asked to be), never installing it.

Replacing the daemon restarts it, so connections over the host's tailnet, an SSH session included, drop for a moment; upgrading the app runs its installer, which uses `sudo`, and restarts its network extension. A host with neither the formula behind its daemon nor the cask is skipped with a message, as is one without Homebrew, and a daemon built by hand is left as it is. Under `--check` Homebrew's index is not refreshed, so the answer is the one the index the host already has gives, nothing that changes the host runs, and the upgrade and the refresh are reported as changes. The role never joins or logs in to a tailnet, whatever it updates.

## Variables

- `github_runner_macos_host_sudoers_nopasswd` (default false), `github_runner_macos_host_sudoers_user` (default empty, the connecting user): see Passwordless sudo above.
- `github_runner_macos_host_sudoers_dir`: the directory macOS reads sudoers drop-ins from. Not a setting; a variable only so the tests can point it at a stand-in.
- `github_runner_macos_host_power` (default true), `github_runner_macos_host_tailscale` (default true): turn each part off. `bootstrap.sh` turns both off, since it runs without sudo.
- `github_runner_macos_host_power_ac_sleep`, `github_runner_macos_host_power_ac_displaysleep`, `github_runner_macos_host_power_ac_disksleep` (default 0 each): minutes of inactivity before the system sleeps, the display sleeps and the disks spin down on AC power; 0 never.
- `github_runner_macos_host_power_ac_womp`, `github_runner_macos_host_power_ac_standby`, `github_runner_macos_host_power_ac_powernap`, `github_runner_macos_host_power_ac_tcpkeepalive` (default 1 each): wake for network access, standby, Power Nap and keeping TCP connections alive during sleep; 1 on, 0 off.
- `github_runner_macos_host_disable_sleep` (default true): keep the Mac awake with its lid closed, with `pmset -a disablesleep 1`. False leaves the setting alone and never turns it off. See Power settings above.
- `github_runner_macos_host_homebrew_prefix` (default empty): the Homebrew prefix, whose `bin` holds the formula's `tailscaled` and `tailscale`. Empty takes `/opt/homebrew` on Apple silicon and `/usr/local` on Intel.
- `github_runner_macos_host_tailscale_auth_key` (default empty), `github_runner_macos_host_tailscale_up_args` (default empty list): see Tailscale above.
- `github_runner_macos_host_tailscale_plist`: the launchd definition `tailscaled install-system-daemon` writes. Not a setting; a variable only so the tests can point it at a stand-in.
- `github_runner_macos_host_tailscale_update` (default true): keep the formula, its daemon and the standalone app current, as under Keeping Tailscale current above. Part of the Tailscale part, so `github_runner_macos_host_tailscale` false skips it too.
- `github_runner_macos_host_tailscale_app_bundle`: where Tailscale's standalone app lives. A host that has it gets no daemon installed beside it. Not a setting; a variable only so the tests can point it at a stand-in.
- `github_runner_macos_host_tailscale_daemon_binary`, `github_runner_macos_host_tailscale_app_cask`: where `tailscaled install-system-daemon` puts the daemon the launchd definition starts, and the Homebrew cask of the standalone app. Not settings; variables only so the tests can point them at stand-ins.
- `github_runner_macos_host_path`: the `PATH` the Homebrew tasks run with, both Homebrew prefixes and the system directories.

`meta/argument_specs.yml` checks every variable when the role runs, and `defaults/main.yml` holds each default with its reasoning.

## Example

```yaml
- hosts: github_runner_cluster
  roles:
    - exadev.github_runner.github_runner_macos_host
```

```yaml
# host_vars/example.yml: a Mac that may sleep its display, joined with a key from the environment
github_runner_macos_host_power_ac_displaysleep: 10
github_runner_macos_host_tailscale_auth_key: "{{ lookup('ansible.builtin.env', 'TAILSCALE_HOST_AUTH_KEY') }}"
github_runner_macos_host_tailscale_up_args: [--hostname=example]
```

## Testing

`tests/unit/test_macos_host.py` covers the filter in `plugins/filter/macos_host.py` that compares `pmset -g custom` with the wanted values, the defaults against the argument specs and this file, the sudoers file's rendered content, ownership and `visudo` validation, the wiring in `playbooks/site.yml` (the role runs before the fleet play, and `become` appears nowhere else), and the role's tasks (`tests/macos_host/run.yml`) against stand-ins for `pmset`, `brew`, `tailscaled`, `tailscale` and `sudo` on `PATH`. The stand-ins that change the host refuse to run without the `sudo` stand-in, as the real ones do for a user who is not root, so the tests check that exactly the tasks that need it use it. They check that a Mac that sleeps gets only the settings that differ, `disablesleep` is set once and then left alone (and not read or set at all when its variable is false), a second run and `--check` change nothing, a host that already has the daemon is left alone, an older formula and daemon get one upgrade of the formula alone and one refresh of the daemon (a current one gets neither, and `--check` makes neither), the standalone app is upgraded only where it is installed, and the auth key reaches `tailscale up` on standard input and nowhere else.
