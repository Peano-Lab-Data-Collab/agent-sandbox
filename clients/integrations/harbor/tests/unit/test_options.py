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

"""Option parsing from --ek strings and environment variables."""

import pytest

from k8s_agent_sandbox_harbor.options import EgressService, EnvironmentOptions


def test_defaults_and_string_coercion():
    kwargs = {"namespace": "harbor", "ready_timeout_s": "120", "warm_replicas": "2",
              "run_as_root": "false", "node_selector": "pool=sandbox,kvm=true",
              "tolerations": '[{"key": "sandbox", "operator": "Exists"}]',
              "cpu_limit_multiplier": "2", "suppress_override_warnings": "1"}
    opts = EnvironmentOptions.from_kwargs(kwargs)
    assert opts.namespace == "harbor" and opts.ready_timeout_s == 120 and opts.warm_replicas == 2
    assert opts.run_as_root is False and opts.cpu_limit_multiplier == 2.0
    assert opts.node_selector == {"pool": "sandbox", "kvm": "true"}
    assert opts.tolerations == [{"key": "sandbox", "operator": "Exists"}]
    assert kwargs == {"suppress_override_warnings": "1"}   # ours consumed, Harbor's left
    assert EnvironmentOptions.from_kwargs({}).network == "dns-public"


def test_unknown_or_invalid_options_name_the_valid_keys():
    with pytest.raises(ValueError, match="unknown agent-sandbox environment option.*namespce"):
        EnvironmentOptions.from_kwargs({"namespce": "x"})
    with pytest.raises(ValueError, match="network"):
        EnvironmentOptions.from_kwargs({"network": "open-bar"})
    with pytest.raises(ValueError, match="key=value"):
        EnvironmentOptions.from_kwargs({"node_selector": "nope"})


def test_env_vars_are_defaults_and_kwargs_win(monkeypatch):
    monkeypatch.setenv("K8S_SANDBOX_HARBOR_NAMESPACE", "from-env")
    monkeypatch.setenv("K8S_SANDBOX_HARBOR_RUNTIME_CLASS", "gvisor")
    opts = EnvironmentOptions.from_kwargs({"namespace": "from-ek"})
    assert opts.namespace == "from-ek" and opts.runtime_class == "gvisor"


def test_egress_service_parses_namespace_service_and_port(monkeypatch):
    opts = EnvironmentOptions.from_kwargs({"egress_service": "proxy-ns/model-proxy:8080"})
    assert opts.egress_service == EgressService("proxy-ns", "model-proxy", 8080)
    assert EnvironmentOptions.from_kwargs({}).egress_service is None
    monkeypatch.setenv("K8S_SANDBOX_HARBOR_EGRESS_SERVICE", "other/svc:1")
    assert EnvironmentOptions.from_kwargs({}).egress_service == EgressService("other", "svc", 1)


@pytest.mark.parametrize("bad", [
    "model-proxy:8080", "ns/model-proxy", "ns/model-proxy:http", "ns/model-proxy:0",
    "ns/model-proxy:65536", "ns/model-proxy:-1", "NS/model-proxy:80", "ns/model_proxy:80",
    "ns/-proxy:80", "/svc:80", "ns/:80", "a/b/c:80", f"ns/{'a' * 64}:80"])
def test_egress_service_rejects_a_malformed_value(bad):
    with pytest.raises(ValueError, match=r"egress_service .*<namespace>/<service>:<port>"):
        EnvironmentOptions.from_kwargs({"egress_service": bad})


def test_egress_service_cannot_extend_the_controllers_default_policy():
    with pytest.raises(ValueError, match="egress_service.*network=managed-default"):
        EnvironmentOptions.from_kwargs({"egress_service": "ns/svc:80", "network": "managed-default"})
    # The default network and an explicit dns-public both accept it.
    assert EnvironmentOptions.from_kwargs({"egress_service": "ns/svc:80", "network": "dns-public"})
