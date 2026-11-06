# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Exec-plugin tokens of the shared async client renew before they expire."""

import datetime
import json
import sys
from pathlib import Path

import pytest
from kubernetes_asyncio.config import kube_config

from k8s_agent_sandbox_harbor import _clients

# Prints a fresh token per run, numbered by how often the plugin has run,
# with an expiry that is the given number of seconds away.
_PLUGIN = """
import datetime, json, pathlib, sys
runs = pathlib.Path(sys.argv[1])
n = int(runs.read_text() or 0) + 1 if runs.exists() else 1
runs.write_text(str(n))
expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=int(sys.argv[2]))
print(json.dumps({"apiVersion": "client.authentication.k8s.io/v1beta1", "kind": "ExecCredential",
                  "status": {"token": f"t{n}", "expirationTimestamp": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")}}))
"""


@pytest.fixture
def kubeconfig_with_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def write(token_lifetime_s: int) -> Path:
        plugin = tmp_path / "plugin.py"
        plugin.write_text(_PLUGIN)
        runs = tmp_path / "runs"
        config = {
            "apiVersion": "v1", "kind": "Config", "current-context": "c",
            "clusters": [{"name": "k", "cluster": {"server": "https://127.0.0.1:1"}}],
            "users": [{"name": "u", "user": {"exec": {
                "apiVersion": "client.authentication.k8s.io/v1beta1", "command": sys.executable,
                "args": [str(plugin), str(runs), str(token_lifetime_s)]}}}],
            "contexts": [{"name": "c", "context": {"cluster": "k", "user": "u"}}],
        }
        path = tmp_path / "kubeconfig"
        path.write_text(json.dumps(config))
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.setattr(kube_config, "KUBE_CONFIG_DEFAULT_LOCATION", str(path))
        return runs

    return write


async def test_a_token_near_its_expiry_is_renewed_before_the_next_request(kubeconfig_with_plugin):
    runs = kubeconfig_with_plugin(token_lifetime_s=60)  # inside the loader's 5-minute margin
    client = await _clients._async_api_client(None)
    try:
        assert await client.configuration.get_api_key_with_prefix("BearerToken") == "Bearer t2"
        assert runs.read_text() == "2"
    finally:
        await client.close()


async def test_a_valid_token_is_reused_without_rerunning_the_plugin(kubeconfig_with_plugin):
    runs = kubeconfig_with_plugin(token_lifetime_s=int(datetime.timedelta(hours=1).total_seconds()))
    client = await _clients._async_api_client(None)
    try:
        for _ in range(3):
            assert await client.configuration.get_api_key_with_prefix("BearerToken") == "Bearer t1"
        assert runs.read_text() == "1"
    finally:
        await client.close()
