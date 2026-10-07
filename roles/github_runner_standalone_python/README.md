# github_runner_standalone_python

Installs a pinned, self-contained CPython from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) on a Linux host that has no Python, so Ansible can run its modules there. Unraid is the case it exists for: the system ships none, and anything installed into its root filesystem is lost at the next reboot, so the interpreter goes on persistent storage instead.

The build bundles its own libraries and needs only the glibc already on the host, so it does not depend on the host's distribution or release. The release, CPython version and archive SHA-256 are pinned together; the download is checked against the checksum before it is unpacked, and the install directory is replaced by a single rename so an interrupted run never leaves half of one.

Every task is `ansible.builtin.raw`, a plain shell command over SSH, because no module can run on the host until it has a Python. The play that includes the role therefore sets `gather_facts: false`.

## Variables

- `github_runner_standalone_python_install_dir`: required. An absolute path on storage that survives a reboot (on Unraid, under `/mnt/user/appdata`). The role fails if it exists, is not empty and holds no `bin/python3`, so it never replaces anything but its own install.
- `github_runner_standalone_python_release`, `github_runner_standalone_python_version`, `github_runner_standalone_python_sha256`: the pinned build. Change all three together; the checksum comes from the release's `SHA256SUMS`.
- `github_runner_standalone_python_archive`, `github_runner_standalone_python_url`: derived from the three above. Only the x86-64 Linux build is pinned, and the role fails on any other platform.

Set `ansible_python_interpreter` to `<install dir>/bin/python3` in the host's `host_vars`. Ansible reads that before a play starts, so the role cannot set it in time; it fails with that instruction when the variable is missing.

## Example

```yaml
- hosts: unraid
  gather_facts: false
  tasks:
    - ansible.builtin.include_role:
        name: exadev.github_runner.github_runner_standalone_python
      vars:
        github_runner_standalone_python_install_dir: /mnt/user/appdata/github-runner/python
```

`playbooks/site.yml` runs the role first for every cluster host that sets `github_runner_standalone_python_install: true`.
