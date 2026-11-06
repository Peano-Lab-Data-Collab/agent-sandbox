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

"""Template and pool rendering, and the store's create-once contract."""

import pytest

from k8s_agent_sandbox_harbor.labels import LABEL_MANAGED_BY, MANAGED_BY_VALUE
from k8s_agent_sandbox_harbor.options import EgressService, EnvironmentOptions
from k8s_agent_sandbox_harbor.templates import (CLAIMS, POOLS, TEMPLATES, EgressServiceError,
                                                ResolvedService, ResourceSpec, TemplateStore,
                                                cpu_quantity, render_pool, render_template,
                                                resolve_egress_service, template_name_for)

from .fake_k8s import FakeCoreV1, FakeCustomObjects, make_service

RES = ResourceSpec(cpu_request="1", cpu_limit="1", memory_request="2048Mi", memory_limit="2048Mi",
                   storage_request="10240Mi")


def test_cpu_quantity():
    assert cpu_quantity(1) == "1" and cpu_quantity(2.0) == "2" and cpu_quantity(0.5) == "500m"


def test_render_template_pod_spec_and_network():
    opts = EnvironmentOptions(runtime_class="gvisor", node_selector={"pool": "sb"},
                              image_pull_secret="ps", extra_labels={"team": "rl"},
                              tolerations=[{"key": "sandbox", "operator": "Exists"}])
    t = render_template(name="hb-abc", namespace="harbor", image="alexgshaw/x:1", resources=RES,
                        options=opts, workdir="/app", internet=True, environment_name="x", environment_id="e1")
    pod = t["spec"]["podTemplate"]["spec"]
    c = pod["containers"][0]
    assert t["kind"] == "SandboxTemplate" and t["metadata"]["labels"][LABEL_MANAGED_BY] == MANAGED_BY_VALUE
    assert t["metadata"]["labels"]["team"] == "rl" and t["metadata"]["annotations"]["harbor.io/image"] == "alexgshaw/x:1"
    assert c["name"] == "main" and c["image"] == "alexgshaw/x:1" and c["command"] == ["sleep", "infinity"]
    assert c["workingDir"] == "/app" and c["securityContext"] == {"runAsUser": 0, "runAsGroup": 0}
    assert c["resources"] == {"requests": {"cpu": "1", "memory": "2048Mi", "ephemeral-storage": "10240Mi"},
                              "limits": {"cpu": "1", "memory": "2048Mi"}}
    assert pod["automountServiceAccountToken"] is False and pod["runtimeClassName"] == "gvisor"
    assert pod["nodeSelector"] == {"pool": "sb"} and pod["imagePullSecrets"] == [{"name": "ps"}]
    assert pod["tolerations"] == [{"key": "sandbox", "operator": "Exists"}]
    egress = t["spec"]["networkPolicy"]["egress"]
    assert t["spec"]["networkPolicy"]["ingress"] == []
    # DNS reaches kube-dns directly, or the per-node cache when NodeLocal DNSCache is on.
    assert [(p["namespaceSelector"]["matchLabels"], p["podSelector"]["matchLabels"])
            for p in egress[0]["to"]] == [
        ({"kubernetes.io/metadata.name": "kube-system"}, {"k8s-app": "kube-dns"}),
        ({"kubernetes.io/metadata.name": "kube-system"}, {"k8s-app": "node-local-dns"})]
    assert egress[0]["ports"] == [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]
    assert "10.0.0.0/8" in egress[1]["to"][0]["ipBlock"]["except"]
    assert "100.64.0.0/10" in egress[1]["to"][0]["ipBlock"]["except"]
    assert egress[1]["to"][1]["ipBlock"] == {"cidr": "::/0", "except": ["fc00::/7", "fe80::/10"]}


def test_managed_default_network_and_no_root_leave_fields_out():
    opts = EnvironmentOptions(network="managed-default", run_as_root=False)
    t = render_template(name="n", namespace="ns", image="i", resources=ResourceSpec(), options=opts,
                        workdir=None, internet=True, environment_name="x", environment_id="e")
    pod = t["spec"]["podTemplate"]["spec"]
    assert "networkPolicy" not in t["spec"] and "securityContext" not in pod
    assert "resources" not in pod["containers"][0] and "workingDir" not in pod["containers"][0]


def test_template_name_tracks_the_rendered_spec():
    opts = EnvironmentOptions()
    a = template_name_for(opts, "img:1", RES, "/app", internet=True)
    assert a == template_name_for(opts, "img:1", RES, "/app", internet=True) and a.startswith("hb-")
    assert a != template_name_for(opts, "img:2", RES, "/app", internet=True)
    assert a != template_name_for(opts, "img:1", ResourceSpec(cpu_request="2"), "/app", internet=True)
    assert a != template_name_for(EnvironmentOptions(runtime_class="gvisor"), "img:1", RES, "/app", internet=True)
    # A different replica count or label set is a different pair, not a silent reuse.
    assert a != template_name_for(EnvironmentOptions(warm_replicas=2), "img:1", RES, "/app", internet=True)
    assert a != template_name_for(EnvironmentOptions(extra_labels={"team": "x"}), "img:1", RES, "/app", internet=True)
    assert a != template_name_for(opts, "img:1", RES, "/app", internet=False)


def test_a_changed_egress_rule_gets_a_new_template_name(monkeypatch):
    # A template that already exists is reused as is, so a release that changes
    # the rule must not land on the old name and keep the old policy.
    import k8s_agent_sandbox_harbor.templates as templates
    before = template_name_for(EnvironmentOptions(), "img:1", RES, "/app", internet=True)
    monkeypatch.setattr(templates, "dns_public_egress", lambda: [{"to": [{"ipBlock": {"cidr": "1.2.3.4/32"}}]}])
    assert template_name_for(EnvironmentOptions(), "img:1", RES, "/app", internet=True) != before


@pytest.mark.parametrize("network", ["dns-public", "managed-default"])
def test_template_without_internet_blocks_all_traffic(network):
    t = render_template(name="n", namespace="ns", image="i", resources=RES,
                        options=EnvironmentOptions(network=network), workdir=None, internet=False,
                        environment_name="x", environment_id="e")
    assert t["spec"]["networkPolicy"] == {"ingress": [], "egress": []}


def test_render_pool():
    p = render_pool(name="hb-abc", namespace="ns", template_name="hb-abc", replicas=3, labels={"a": "b"})
    assert p["kind"] == "SandboxWarmPool" and p["spec"] == {"replicas": 3, "sandboxTemplateRef": {"name": "hb-abc"}}


async def test_store_creates_once_tolerates_409_and_cleans_up():
    api = FakeCustomObjects()
    opts = EnvironmentOptions(namespace="ns", warm_replicas=1)
    store = TemplateStore(api, "ns", opts)
    name = await store.ensure(image="img:1", resources=RES, workdir=None, internet=True, environment_name="t",
                              environment_id="e")
    assert await store.ensure(image="img:1", resources=RES, workdir=None, internet=True, environment_name="t",
                              environment_id="e") == name
    assert [c for c in api.calls if c[0] == "create"] == [("create", TEMPLATES, name), ("create", POOLS, name)]
    assert api.objects[(POOLS, name)]["spec"]["replicas"] == 1

    # Another process created the pair first: 409 on both is success.
    other = TemplateStore(api, "ns", opts)
    assert await other.ensure(image="img:1", resources=RES, workdir=None, internet=True, environment_name="t",
                              environment_id="e") == name

    api.objects[(CLAIMS, "c1")] = {"metadata": {"name": "c1", "labels": {LABEL_MANAGED_BY: MANAGED_BY_VALUE}}}
    api.objects[(CLAIMS, "foreign")] = {"metadata": {"name": "foreign", "labels": {}}}
    counts = await store.delete_managed()
    assert counts == {CLAIMS: 1, POOLS: 1, TEMPLATES: 1}
    assert list(api.objects) == [(CLAIMS, "foreign")]


async def test_store_raises_on_other_api_errors():
    class Broken(FakeCustomObjects):
        async def create_namespaced_custom_object(self, **kw):
            from .fake_k8s import ApiError
            raise ApiError(403, "Forbidden")

    with pytest.raises(Exception, match="403"):
        await TemplateStore(Broken(), "ns", EnvironmentOptions()).ensure(
            image="img", resources=RES, workdir=None, internet=True, environment_name="t", environment_id="e")


WANTED = EgressService("proxy-ns", "model-proxy", 8080)
PROXY = ResolvedService(namespace="proxy-ns", name="model-proxy", cluster_ip="10.96.0.20",
                        selector=(("app", "model-proxy"),), target_port=3000, protocol="TCP")
PROXY_RULE = {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "proxy-ns"}},
                      "podSelector": {"matchLabels": {"app": "model-proxy"}}}],
              "ports": [{"protocol": "TCP", "port": 3000}]}


def _render(*, internet, service, network="dns-public"):
    return render_template(name="n", namespace="ns", image="i", resources=RES,
                           options=EnvironmentOptions(network=network), workdir=None,
                           internet=internet, environment_name="x", environment_id="e",
                           service=service)


def test_a_task_with_internet_keeps_dns_and_public_egress_and_gains_the_service_rule():
    t = _render(internet=True, service=PROXY)
    egress = t["spec"]["networkPolicy"]["egress"]
    assert egress[:-1] == _render(internet=True, service=None)["spec"]["networkPolicy"]["egress"]
    assert egress[-1] == PROXY_RULE
    assert t["spec"]["networkPolicy"]["ingress"] == []


def test_a_task_without_internet_reaches_the_service_and_nothing_else():
    t = _render(internet=False, service=PROXY)
    assert t["spec"]["networkPolicy"] == {"ingress": [], "egress": [PROXY_RULE]}


def test_the_pod_resolves_the_service_name_without_dns():
    pod = _render(internet=False, service=PROXY)["spec"]["podTemplate"]["spec"]
    assert pod["hostAliases"] == [{"ip": "10.96.0.20", "hostnames": [
        "model-proxy.proxy-ns.svc", "model-proxy.proxy-ns.svc.cluster.local"]}]
    assert "hostAliases" not in _render(internet=False, service=None)["spec"]["podTemplate"]["spec"]


def test_the_protocol_is_passed_through():
    udp = ResolvedService("proxy-ns", "model-proxy", "10.96.0.20", (("app", "p"),), 5353, "UDP")
    rule = _render(internet=False, service=udp)["spec"]["networkPolicy"]["egress"][0]
    assert rule["ports"] == [{"protocol": "UDP", "port": 5353}]


def test_template_name_changes_with_every_resolved_field():
    from dataclasses import replace
    opts = EnvironmentOptions()

    def name(service):
        return template_name_for(opts, "img:1", RES, "/app", internet=False, service=service)

    names = {name(None), name(PROXY),
             name(replace(PROXY, cluster_ip="10.96.0.21")),
             name(replace(PROXY, selector=(("app", "other"),))),
             name(replace(PROXY, target_port=3001)),
             name(replace(PROXY, protocol="UDP")),
             name(replace(PROXY, namespace="elsewhere")),
             name(replace(PROXY, name="other-proxy"))}
    assert len(names) == 8
    assert name(replace(PROXY)) == name(PROXY)


async def test_resolve_reads_the_service_the_rule_needs():
    api = FakeCoreV1()
    api.add_service(make_service(ports=((80, 8000, "TCP"), (8080, "9090", "UDP"))))
    assert await resolve_egress_service(api, WANTED) == ResolvedService(
        namespace="proxy-ns", name="model-proxy", cluster_ip="10.96.0.20",
        selector=(("app", "model-proxy"),), target_port=9090, protocol="UDP")


@pytest.mark.parametrize("service, message", [
    (make_service(selector={}), "no selector"),
    (make_service(cluster_ip="None"), "headless"),
    (make_service(cluster_ip=""), "headless"),
    (make_service(ports=((9090, 9090, "TCP"),)), r"no port 8080 \(ports: 9090\)"),
    (make_service(ports=((8080, "http", "TCP"),)), "named targetPort 'http'.*port number"),
])
async def test_resolve_refuses_a_service_a_network_policy_cannot_admit(service, message):
    api = FakeCoreV1()
    api.add_service(service)
    with pytest.raises(EgressServiceError, match=f"proxy-ns/model-proxy.*{message}"):
        await resolve_egress_service(api, WANTED)


async def test_resolve_names_the_missing_permission_on_403_and_the_service_on_404():
    api = FakeCoreV1()
    with pytest.raises(EgressServiceError, match="proxy-ns/model-proxy does not exist"):
        await resolve_egress_service(api, WANTED)
    api.forbidden.add(("proxy-ns", "model-proxy"))
    with pytest.raises(EgressServiceError,
                       match="proxy-ns/model-proxy.*'get' on 'services' in namespace proxy-ns"):
        await resolve_egress_service(api, WANTED)
