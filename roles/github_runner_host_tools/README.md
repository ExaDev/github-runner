# github_runner_host_tools

Homebrew and [mise](https://mise.jdx.dev) on every host of the fleet, Mac or Linux: installed when missing, kept at their latest release and made available to the connecting user's shell. It runs on macOS (Apple silicon and Intel) and on Debian-family Linux (Debian and Ubuntu) and skips any other host with a message, so it can be applied to a mixed inventory. `playbooks/site.yml` runs it for every cluster host, before the two host baseline plays and the fleet play.

## Privileges

Homebrew refuses to run as root, so everything the role does with it runs as the connecting user, and the role fails at the start, before it changes anything, if that user is root. The pieces that need privilege use it themselves: the Linux prerequisites are installed with `apt-get` under `become` (sudo), and Homebrew's installer uses sudo for the directories it creates. Run the play with passwordless sudo for the connecting user, or with `--ask-become-pass`. A host that already has Homebrew needs no privilege at all.

Under `--check` nothing is downloaded, installed or run: the role reports the packages, the installer and the `brew update` a real run would make, and the Homebrew tasks never fail for want of a `brew` that the run would have installed.

## Homebrew

When the `brew` binary is missing from the prefix, the role installs Homebrew. The prefix is `/opt/homebrew` on Apple silicon, `/usr/local` on an Intel Mac and `/home/linuxbrew/.linuxbrew` on Linux, the only places Homebrew supports, and a host that has Homebrew already, with or without the role, is not installed over.

On Debian-family Linux the installer needs `build-essential`, `procps`, `curl`, `file` and `git` ([Homebrew on Linux, Requirements](https://docs.brew.sh/Homebrew-on-Linux#requirements)); the role installs those of them that are missing, with `apt-get install --no-install-recommends`, and runs `apt-get update` first only when there is something to install.

The installer is Homebrew's own `install.sh`, downloaded with `ansible.builtin.get_url` from a pinned commit of [Homebrew/install](https://github.com/Homebrew/install) (`github_runner_host_tools_homebrew_install_commit`) and refused unless it matches a SHA-256 checksum (`github_runner_host_tools_homebrew_install_sha256`), into a private temporary directory that is removed afterwards. It is then run by file, never piped from the network into a shell, with `NONINTERACTIVE=1` so it never waits for a key and `creates` set to the `brew` binary. To move to a newer installer, take a commit of Homebrew/install, download its `install.sh` and take the file's checksum with `shasum -a 256`; raise the two together.

Every run then calls `community.general.homebrew` with `update_homebrew: true`, which runs `brew update`, how Homebrew keeps itself at its latest release. It reports a change whenever the update fetched something, so a host that Homebrew had new commits for shows one changed task on the next run, which is the update and not drift.

## mise

mise is installed through Homebrew, and the same `community.general.homebrew` call that updates Homebrew names `mise` with `state: latest`, so a host without it gets it installed and a host with an older release gets it upgraded. Only `mise` is ever named: the role never upgrades another formula, because an upgrade of a formula that something else installed (a container runtime, a daemon) restarts a service that may be running. `github_runner_host_tools_mise` needs `github_runner_host_tools_homebrew`, since Homebrew is how mise is installed, and the role refuses the other way round.

## Shell integration

With `github_runner_host_tools_shell_integration` the role makes both tools available to the connecting user's shell, and adds only what is missing. It reads the user's login shell from the account database (`getent passwd` on Linux, `dscl` on macOS), never assuming it, and supports zsh and bash; a user with any other login shell is left alone with a message.

For each tool the role looks for an equivalent line (`brew shellenv` for Homebrew, `mise activate` for mise, in a line that is not a comment) in every startup file of that shell, and adds a managed block to one file only when none has it:

| Shell | Looked for in | Written to |
| --- | --- | --- |
| zsh | `.zshenv`, `.zshrc`, `.zprofile` | `.zshenv` |
| bash | `.bashrc`, `.bash_profile`, `.profile` | `.bashrc` |

`.zshenv` is read by every zsh, including the non-interactive one an SSH command starts, and `.bashrc` by the non-interactive bash `sshd` starts, so a command run over SSH finds both tools. Each block is a `blockinfile` block between `ANSIBLE MANAGED BLOCK` marker lines, one per tool, holding one line guarded by `[ -x ... ]` that uses the absolute path of the binary:

```sh
[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
[ -x /opt/homebrew/bin/mise ] && eval "$(/opt/homebrew/bin/mise activate zsh)"
```

A host whose startup files already do this, set up by hand or by a dotfiles manager, is not touched. A block the role wrote earlier counts as the equivalent line and is kept current, so a different prefix updates it in place. The role never passes a mode, so an existing startup file keeps the mode it has, and a startup file that is a symlink stays one and its target is edited.

## Global mise defaults

`github_runner_host_tools_mise_config` is a mapping that the role renders to `~/.config/mise/conf.d/00-fleet.toml`. It is empty by default, and empty it writes nothing and leaves an existing `00-fleet.toml` alone. It is never written to `~/.config/mise/config.toml`: that file is the host's own and takes precedence over `conf.d`, so the role neither writes nor deletes it, and a host's own choice of a tool version wins over the fleet's default. Two tables are understood:

- `tools`: a tool name to a version string or a list of them. A name that is not a bare TOML key, such as `npm:typescript`, is quoted in the file.
- `settings`: a setting name to a string, a number, a boolean or a list of those.

```yaml
github_runner_host_tools_mise_config:
  tools:
    node: "22"
    python: ["3.13", "3.12"]
  settings:
    experimental: true
```

renders to

```toml
[tools]
node = "22"
python = ["3.13", "3.12"]

[settings]
experimental = true
```

Any other table, or a value of a kind its table does not take, fails the run with a message rather than being written in a form mise might read differently.

## Variables

- `github_runner_host_tools_homebrew` (default true): install Homebrew when it is missing and run `brew update` on every run.
- `github_runner_host_tools_mise` (default true): install mise through Homebrew and keep it at its latest release. Needs `github_runner_host_tools_homebrew`.
- `github_runner_host_tools_shell_integration` (default true): add the Homebrew and mise lines to the connecting user's shell startup file where none has them.
- `github_runner_host_tools_homebrew_install_commit`, `github_runner_host_tools_homebrew_install_sha256`: the commit of Homebrew/install whose `install.sh` is downloaded, and that file's SHA-256 checksum. Both are checked to be full lower-case hexadecimal hashes (40 and 64 characters) before anything runs; `defaults/main.yml` holds the values.
- `github_runner_host_tools_mise_config` (default an empty mapping): see Global mise defaults above.
- `github_runner_host_tools_homebrew_prefix` (default empty): the Homebrew prefix. Empty takes the prefix described under Homebrew. Not a setting; a variable only so the tests can point it at a stand-in.
- `github_runner_host_tools_homebrew_install_base_url`: where the installer is downloaded from, as `<this>/<commit>/install.sh`. Not a setting; a variable only so the tests can point it at a stand-in.

`meta/argument_specs.yml` checks every variable when the role runs, and `defaults/main.yml` holds each default with its reasoning. `bootstrap.sh` turns `github_runner_host_tools_homebrew` and `github_runner_host_tools_mise` off, since it runs without sudo.

## Example

```yaml
- hosts: github_runner_cluster
  roles:
    - exadev.github_runner.github_runner_host_tools
```

```yaml
# host_vars/example.yml: a host that sets up its shell itself and wants Node and Python from mise
github_runner_host_tools_shell_integration: false
github_runner_host_tools_mise_config:
  tools:
    node: "22"
    python: "3.13"
```

## Testing

`tests/unit/test_host_tools.py` covers the filters in `plugins/filter/host_tools.py` (the prefix of each system and architecture, the shell startup lines, and the TOML rendering against golden strings), the defaults against the argument specs and this file, the shape of the pinned commit and checksum, that the only formula the role ever names to Homebrew is mise, the wiring in `playbooks/site.yml`, and the role's tasks (`tests/host_tools/run.yml`) against stand-ins for `brew`, `sudo`, `apt-get`, `dpkg-query`, `getent`, `dscl` and Homebrew's installer on `PATH`, with the installer served from a local directory and every shell startup file in a temporary home. They check that the installer runs non-interactively as the user and only when its checksum matches, that a second run and `--check` change nothing, that root is refused, that a startup file keeps its mode, its content and its symlink, and that `config.toml` is never written. Nothing is downloaded and no real package manager, privilege or startup file is touched.
