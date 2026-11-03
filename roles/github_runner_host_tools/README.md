# github_runner_host_tools

Homebrew and [mise](https://mise.jdx.dev) on every host of the fleet, Mac or Linux: installed when missing, kept at their latest release and made available to the connecting user's shell, and the package-install security settings of npm and pnpm and the shell environment variables a host asks for, written from variables. It runs on macOS (Apple silicon and Intel) and on Debian-family Linux (Debian and Ubuntu) and skips any other host with a message, so it can be applied to a mixed inventory. `playbooks/site.yml` runs it for every cluster host, before the two host baseline plays and the fleet play.

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

`.zshenv` is read by every zsh, including the non-interactive one an SSH command starts ([zsh, Startup Files](https://zsh.sourceforge.io/Doc/Release/Files.html); [Wayback Machine snapshot](https://web.archive.org/web/20261024165218/https://zsh.sourceforge.io/Doc/Release/Files.html)), so a command run over SSH finds both tools on a zsh host. A non-interactive bash reads `.bashrc` only as far as the file's interactive guard, which Debian's and Ubuntu's default `.bashrc` puts near its top, so on a bash host the two lines, which are appended at the end of the file where the interactive setup is, are for interactive shells; the environment variables below are written at the top instead. Each block is a `blockinfile` block between `ANSIBLE MANAGED BLOCK` marker lines, one per tool, holding one line guarded by `[ -x ... ]` that uses the absolute path of the binary:

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

## Package-install security settings

Hosts that install packages can refuse packages younger than a minimum age and skip install scripts. The role writes these settings from variables, and all four are empty by default: empty, the role does not read or write `~/.npmrc` or pnpm's config file, so a host that sets none of them sees no change. They do not need Homebrew or mise, and they apply on every host the role supports. Node and pnpm themselves are not installed here; put them in `github_runner_host_tools_mise_config`.

### npm

`github_runner_host_tools_npm_settings` is a mapping of npmrc keys to string, number or boolean values, and `github_runner_host_tools_npm_min_release_age_exclude` is a list of package names or patterns. Both are written to the connecting user's `~/.npmrc` in one block between `ANSIBLE MANAGED BLOCK` marker lines, and nothing outside the block is touched:

```yaml
github_runner_host_tools_npm_settings:
  min-release-age: 7
  ignore-scripts: true
github_runner_host_tools_npm_min_release_age_exclude:
  - example-package
  - "@example/*"
```

renders to

```ini
# BEGIN ANSIBLE MANAGED BLOCK github_runner_host_tools npm
min-release-age=7
ignore-scripts=true
min-release-age-exclude[]=example-package
min-release-age-exclude[]=@example/*
# END ANSIBLE MANAGED BLOCK github_runner_host_tools npm
```

`min-release-age` is in days ([npm config](https://docs.npmjs.com/cli/v11/using-npm/config#min-release-age); [Wayback Machine snapshot](https://web.archive.org/web/20261022065815/https://docs.npmjs.com/cli/v11/using-npm/config/)). A second run changes nothing, dropping an entry removes its line, and dropping them all removes the block.

An `.npmrc` can hold credentials, so the role reads it only to tell which keys the user already sets, with `no_log`, and prints key names and nothing else. It never changes the mode of an existing file, and creates a missing one with mode `0600`. A file that is a symlink stays one and its target is edited.

A key the file already sets outside the block is the user's own, and the role keeps it: the role's value for that key is not written, and the run prints a message naming the key (`~/.npmrc already sets min-release-age outside the block the role manages, so the host's own value is kept and the role's is not written.`). Writing it would not be neutral, because npm reads the last of two assignments of a key, so a line in a block at the end of the file would silently replace the user's. The run does not fail, since a host that chose its own value on purpose is not a fault. A key set only in a comment does not count, and an exclusion the file lists already is not listed again. A key the user adds later leaves the block on the next run.

The ini format npm reads treats `;` and `#` as comments and a leading quote as quoting, so a value with any of those, a backslash, a control character or surrounding space is refused with a message rather than written in a form npm might read differently. A key that is the exclusion list's own (`min-release-age-exclude`) has the variable above and is refused in the mapping.

### pnpm

`github_runner_host_tools_pnpm_settings` is a mapping of top-level keys of pnpm's global `config.yaml` to values, and `github_runner_host_tools_pnpm_minimum_release_age_exclude` is a list written under `minimumReleaseAgeExclude`:

```yaml
github_runner_host_tools_pnpm_settings:
  minimumReleaseAge: 10080
  saveExact: true
github_runner_host_tools_pnpm_minimum_release_age_exclude:
  - example-package
```

`minimumReleaseAge` is in minutes ([pnpm, Supply chain security](https://pnpm.io/supply-chain-security); [Wayback Machine snapshot](https://web.archive.org/web/20261022233946/https://pnpm.io/supply-chain-security)). The file is at `~/Library/Preferences/pnpm/config.yaml` on macOS and `~/.config/pnpm/config.yaml` on Linux, the two paths [pnpm config](https://pnpm.io/cli/config) documents for the global configuration file ([Wayback Machine snapshot](https://web.archive.org/web/20260915213602/https://pnpm.io/cli/config)). pnpm reads `$XDG_CONFIG_HOME/pnpm/config.yaml` instead when `XDG_CONFIG_HOME` is set, which a play over a non-interactive SSH session cannot know, so a host that sets it has the file elsewhere and the role still writes the platform's own location.

The role sets each key it is given and edits nothing else: every other key, comment and blank line stays byte for byte, a key that already holds the value is not touched (so a second run changes nothing and a file that is right is not rewritten), and a list is written indented under its key. A comment on the same line as a rewritten key goes with its old value. The result is parsed and compared with what the merge should hold before it is written, and a file whose layout the merge cannot edit safely (not YAML, not a mapping at the top, a flow-style mapping, several documents) fails the run with a message and is left as it is. Setting `minimumReleaseAgeExclude` in both variables fails the run before anything is written. The role never changes the mode of an existing file, a new one gets the user's umask, and a symlink stays one with its target edited. A key dropped from the variable is left in the file, since the role cannot tell which keys in a YAML file it wrote.

## Shell environment

`github_runner_host_tools_shell_env` is a mapping of environment variable names to string, number or boolean values, for example `UV_PYTHON_DOWNLOADS: never`. Each variable gets its own managed block with one `export` line in the shell's startup file (`~/.zshenv` for zsh, `~/.bashrc` for bash, as under Shell integration), written at the top of the file, ahead of anything the user or the distribution has in it, so a command run over SSH sees it:

```sh
export UV_PYTHON_DOWNLOADS=never
```

The top is where a non-interactive shell reads it. `sshd` starts the user's shell non-interactively for an SSH command, and zsh reads `~/.zshenv` in full for every instance. Debian's bash is built to read `~/.bashrc` for a non-interactive shell whose standard input is a network connection, as `sshd` starts ([bash(1) in Debian](https://manpages.debian.org/unstable/bash/bash.1.en.html); [Wayback Machine snapshot](https://web.archive.org/web/20261018084059/https://manpages.debian.org/unstable/bash/bash.1.en.html); upstream bash does so only when built with `SSH_SOURCE_BASHRC`), but the `~/.bashrc` that Debian and Ubuntu give every user starts with `case $- in *i*) ;; *) return;; esac`, which ends the file for that shell, so a block appended below it is never read. The role does not look for the guard, because a guard can be written many ways: a block belongs at the top when everything above it is blank, a comment or another block of the role's, and any other code above it is treated as the guard. A block an earlier version of the role appended lower down is taken out and written again at the top on the next run, with the value it now has, and nothing else in the file moves, so a host converges without anyone touching it. A file that already has the block at the top is not rewritten, and `--check` reports a move as a change and makes none. The startup file is read for this with `no_log`, because it can hold secrets; only the position of the block is worked out from it. A `~/.zshenv` whose variable blocks follow the role's Homebrew and mise blocks is already in order and is left as it is.

The same discipline as the Homebrew and mise lines applies: a variable is added only when none of that shell's startup files assigns it already (`NAME=` or `export NAME=` outside a comment, whatever the value), so a value the user chose is never overridden, and a block the role wrote earlier is kept current when the value changes. The value is quoted for the shell (`shlex.quote`: single quotes, with an embedded single quote written as `'"'"'`), so spaces, quotes, `$`, backticks and `;` reach the variable exactly as given in zsh and bash alike. A name the shell does not accept, and a value of more than one line, fail the run with a message. A boolean is written as `true` or `false`.

The variables are written whether or not `github_runner_host_tools_shell_integration` is on, and without Homebrew: that variable covers only the Homebrew and mise lines, and a mapping the host gives is the host asking for the variables. A user whose login shell is neither zsh nor bash is left alone with a message. A variable dropped from the mapping keeps its line.

## Variables

- `github_runner_host_tools_homebrew` (default true): install Homebrew when it is missing and run `brew update` on every run.
- `github_runner_host_tools_mise` (default true): install mise through Homebrew and keep it at its latest release. Needs `github_runner_host_tools_homebrew`.
- `github_runner_host_tools_shell_integration` (default true): add the Homebrew and mise lines to the connecting user's shell startup file where none has them.
- `github_runner_host_tools_homebrew_install_commit`, `github_runner_host_tools_homebrew_install_sha256`: the commit of Homebrew/install whose `install.sh` is downloaded, and that file's SHA-256 checksum. Both are checked to be full lower-case hexadecimal hashes (40 and 64 characters) before anything runs; `defaults/main.yml` holds the values.
- `github_runner_host_tools_mise_config` (default an empty mapping): see Global mise defaults above.
- `github_runner_host_tools_npm_settings` (default an empty mapping): npmrc keys and values written to `~/.npmrc`; see Package-install security settings above.
- `github_runner_host_tools_npm_min_release_age_exclude` (default an empty list): package names or patterns written as `min-release-age-exclude[]=NAME` lines in the same block.
- `github_runner_host_tools_pnpm_settings` (default an empty mapping): top-level keys and values merged into pnpm's global `config.yaml`.
- `github_runner_host_tools_pnpm_minimum_release_age_exclude` (default an empty list): package names or patterns written under `minimumReleaseAgeExclude` in that file.
- `github_runner_host_tools_shell_env` (default an empty mapping): environment variables written as `export` lines at the top of the shell's startup file; see Shell environment above.
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

```yaml
# host_vars/example.yml: refuse packages younger than a week and skip install scripts, and keep uv from downloading Pythons
github_runner_host_tools_npm_settings:
  min-release-age: 7
  ignore-scripts: true
github_runner_host_tools_pnpm_settings:
  minimumReleaseAge: 10080
github_runner_host_tools_shell_env:
  UV_PYTHON_DOWNLOADS: never
```

## Testing

`tests/unit/test_host_tools.py` covers the filters in `plugins/filter/host_tools.py` (the prefix of each system and architecture, the shell startup lines, and the TOML rendering against golden strings), the defaults against the argument specs and this file, the shape of the pinned commit and checksum, that the only formula the role ever names to Homebrew is mise, the wiring in `playbooks/site.yml`, and the role's tasks (`tests/host_tools/run.yml`) against stand-ins for `brew`, `sudo`, `apt-get`, `dpkg-query`, `getent`, `dscl` and Homebrew's installer on `PATH`, with the installer served from a local directory and every shell startup file in a temporary home. They check that the installer runs non-interactively as the user and only when its checksum matches, that a second run and `--check` change nothing, that root is refused, that a startup file keeps its mode, its content and its symlink, and that `config.toml` is never written. They also check the package-install settings and the shell variables: that nothing is read or written when the variables are empty, that the `.npmrc` block holds only the role's lines, keeps the user's lines and credentials (made-up values) out of the output, is idempotent and shrinks as entries are dropped, that a key the user sets outside the block is kept and reported, that a new `.npmrc` is `0600` and no existing file or symlink changes mode, that the pnpm merge keeps unrelated keys and comments, and that a shell variable's value, quotes and dollar signs included, round-trips through `sh` and `bash` reading the written file. The bash cases run against `tests/host_tools/ubuntu-26.04-skel.bashrc`, a byte-for-byte copy of Ubuntu's default `/etc/skel/.bashrc` (its checksum is asserted), and read the result with a real non-interactive `bash`, which sees a variable only when the block is above the interactive guard; they cover a block an earlier version appended below the guard being moved to the top without a duplicate, a second run and `--check` changing nothing, and a variable the file assigns already being kept. Nothing is downloaded and no real package manager, privilege or startup file is touched.
