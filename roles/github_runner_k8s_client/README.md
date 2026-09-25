# github_runner_k8s_client

Installs the `kubernetes` Python package into a dedicated virtualenv on the target host and sets `ansible_python_interpreter` to that virtualenv's Python, so `kubernetes.core` modules work even where the system Python is externally managed (PEP 668, as with Homebrew's Python on macOS). Including it a second time in the same play is a no-op.

The `github_runner_cluster` and `github_runner_arc` roles include it themselves, only on hosts that talk to the Kubernetes API, so it rarely needs listing in a playbook.

## Variables

- `github_runner_k8s_client_venv_path`: where the virtualenv lives. Defaults to `~/.ansible-github-runner-venv` on the target host.

## Example

```yaml
- ansible.builtin.include_role:
    name: exadev.github_runner.github_runner_k8s_client
```
