"""Tests that the secrets role reads 1Password through another machine when github_runner_secrets_onepassword_ssh_host is set: the role runs its own files/op-over-ssh.sh, which calls ssh with the item read, and uses the output. A stub ssh records the call and prints a made-up item, so no machine or 1Password is contacted."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

ITEM = {"fields": [{"label": "k3s-token", "value": "made-up-token"}]}

PLAYBOOK = """\
- hosts: localhost
  gather_facts: false
  vars:
    github_runner_secrets_onepassword_vault: V
    github_runner_secrets_onepassword_item: made-up-item
    github_runner_secrets_onepassword_consumers: [cluster]
    github_runner_secrets_onepassword_cache_path: "{cache}"
    github_runner_secrets_onepassword_ssh_host: remote-mac
    github_runner_secrets_onepassword_ssh_account: a.example.com
  tasks:
    - ansible.builtin.include_role:
        name: github_runner_secrets_onepassword
    - ansible.builtin.assert:
        that: github_runner_cluster_k3s_token == 'made-up-token'
"""


@unittest.skipUnless(shutil.which("ansible-playbook") and shutil.which("jq"), "needs ansible-playbook and jq")
class SshHostTests(unittest.TestCase):
    def test_reads_the_item_through_the_forwarder(self) -> None:
        with tempfile.TemporaryDirectory() as work_name:
            work = Path(work_name)
            bin_dir = work / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "ssh"
            stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{work}/ssh-calls'\ncat <<'JSON'\n{json.dumps(ITEM)}\nJSON\n")
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            (work / "play.yml").write_text(PLAYBOOK.format(cache=work / "cache.json"))
            roles = work / "roles"
            roles.symlink_to(REPO / "roles")
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "ANSIBLE_ROLES_PATH": str(roles), "ANSIBLE_LOCALHOST_WARNING": "False"}
            result = subprocess.run(["ansible-playbook", str(work / "play.yml")], cwd=work, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = (work / "ssh-calls").read_text()
        self.assertIn("remote-mac", calls)
        self.assertIn("OP_ACCOUNT=a.example.com", calls)
        self.assertIn("op item get made-up-item", calls)


if __name__ == "__main__":
    unittest.main()
