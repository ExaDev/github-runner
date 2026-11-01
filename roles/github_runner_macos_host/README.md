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

## Tailscale

The role installs the Homebrew formula `tailscale` and runs `tailscaled install-system-daemon`, which copies `tailscaled` to `/usr/local/bin`, writes the launchd daemon `/Library/LaunchDaemons/com.tailscale.tailscaled.plist` and starts it, so the daemon runs from boot, before anyone logs in. A host that already has that file is left entirely alone, whether this role or a person installed it: nothing is installed, replaced or restarted.

The role never joins or re-authenticates a tailnet on its own, because joining needs an auth key, a secret only the tailnet's owner can create. With `github_runner_macos_host_tailscale_auth_key` set, the role reads the daemon's login state and, for a daemon that needs a login, runs `tailscale up` with the key on standard input (so it is in no process listing and, with `no_log`, in no output), plus `github_runner_macos_host_tailscale_up_args`. A daemon that is already logged in is not touched, and a join that failed is retried by the next run. It applies only to a host whose Homebrew formula the role installed (the formula's `tailscale` is in the Homebrew prefix), so a hand-built daemon is never joined or logged in again.

Without the key, join the host by hand once: `sudo tailscale up`, or run the role again with a key.

## Variables

- `github_runner_macos_host_sudoers_nopasswd` (default false), `github_runner_macos_host_sudoers_user` (default empty, the connecting user): see Passwordless sudo above.
- `github_runner_macos_host_sudoers_dir`: the directory macOS reads sudoers drop-ins from. Not a setting; a variable only so the tests can point it at a stand-in.
- `github_runner_macos_host_power` (default true), `github_runner_macos_host_tailscale` (default true): turn each part off. `bootstrap.sh` turns both off, since it runs without sudo.
- `github_runner_macos_host_power_ac_sleep`, `github_runner_macos_host_power_ac_displaysleep`, `github_runner_macos_host_power_ac_disksleep` (default 0 each): minutes of inactivity before the system sleeps, the display sleeps and the disks spin down on AC power; 0 never.
- `github_runner_macos_host_power_ac_womp`, `github_runner_macos_host_power_ac_standby`, `github_runner_macos_host_power_ac_powernap`, `github_runner_macos_host_power_ac_tcpkeepalive` (default 1 each): wake for network access, standby, Power Nap and keeping TCP connections alive during sleep; 1 on, 0 off.
- `github_runner_macos_host_homebrew_prefix` (default empty): the Homebrew prefix, whose `bin` holds the formula's `tailscaled` and `tailscale`. Empty takes `/opt/homebrew` on Apple silicon and `/usr/local` on Intel.
- `github_runner_macos_host_tailscale_auth_key` (default empty), `github_runner_macos_host_tailscale_up_args` (default empty list): see Tailscale above.
- `github_runner_macos_host_tailscale_plist`: the launchd definition `tailscaled install-system-daemon` writes. Not a setting; a variable only so the tests can point it at a stand-in.
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

`tests/unit/test_macos_host.py` covers the filter in `plugins/filter/macos_host.py` that compares `pmset -g custom` with the wanted values, the defaults against the argument specs and this file, the sudoers file's rendered content, ownership and `visudo` validation, the wiring in `playbooks/site.yml` (the role runs before the fleet play, and `become` appears nowhere else), and the role's tasks (`tests/macos_host/run.yml`) against stand-ins for `pmset`, `brew`, `tailscaled`, `tailscale` and `sudo` on `PATH`. The stand-ins that change the host refuse to run without the `sudo` stand-in, as the real ones do for a user who is not root, so the tests check that exactly the tasks that need it use it. They check that a Mac that sleeps gets only the settings that differ, a second run and `--check` change nothing, a host that already has the daemon is left alone, and the auth key reaches `tailscale up` on standard input and nowhere else.
