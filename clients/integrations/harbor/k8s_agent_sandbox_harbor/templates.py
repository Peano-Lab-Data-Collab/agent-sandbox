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

"""One SandboxTemplate and one SandboxWarmPool per task image.

A Harbor task names an image and its resources. The template renders that as
the pod the controller runs, the pool is the claim target (``replicas: 0``
means every claim cold-starts from the template, ``warm_replicas`` keeps pods
ready). Objects are named by a hash of the rendered spec, the replica count and the
labels, so a changed input gets a new pair and an existing pair is never edited.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from .labels import (ANNOTATION_ENVIRONMENT_ID, ANNOTATION_IMAGE, LABEL_ENVIRONMENT,
                     LABEL_IMAGE_HASH, LABEL_MANAGED_BY, MANAGED_BY_VALUE, label_value,
                     object_name, short_hash)
from .options import EgressService, EnvironmentOptions

GROUP = "extensions.agents.x-k8s.io"
VERSION = "v1beta1"
TEMPLATES = "sandboxtemplates"
POOLS = "sandboxwarmpools"
CLAIMS = "sandboxclaims"
CONTAINER = "main"

# Private ranges the controller's default policy blocks; kept blocked here so
# a task reaches the internet and cluster DNS, and nothing else inside the VPC.
# 100.64.0.0/10 is the shared-address range some clusters use for pods or
# nodes; the IPv6 list covers unique-local and link-local addresses.
_PRIVATE_V4 = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
               "100.64.0.0/10"]
_PRIVATE_V6 = ["fc00::/7", "fe80::/10"]


@dataclass(frozen=True)
class ResourceSpec:
    """Kubernetes quantities for the sandbox container; ``None`` leaves a field out."""
    cpu_request: str | None = None
    cpu_limit: str | None = None
    memory_request: str | None = None
    memory_limit: str | None = None
    storage_request: str | None = None

    def as_resources(self) -> dict[str, dict[str, str]]:
        requests = {k: v for k, v in (("cpu", self.cpu_request), ("memory", self.memory_request),
                                      ("ephemeral-storage", self.storage_request)) if v}
        limits = {k: v for k, v in (("cpu", self.cpu_limit), ("memory", self.memory_limit)) if v}
        out: dict[str, dict[str, str]] = {}
        if requests:
            out["requests"] = requests
        if limits:
            out["limits"] = limits
        return out


@dataclass(frozen=True)
class ResolvedService:
    """What the cluster says about the operator's ``egress_service``.

    A NetworkPolicy matches the backend pods and their target port, after the
    Service has translated the address, so the rule is built from ``selector``
    and ``target_port``. ``cluster_ip`` only feeds the pod's ``hostAliases``.
    """

    namespace: str
    name: str
    cluster_ip: str
    selector: tuple[tuple[str, str], ...]
    """The Service's pod selector as sorted pairs, so the dataclass stays hashable."""
    target_port: int
    protocol: str

    @property
    def hostnames(self) -> list[str]:
        return [f"{self.name}.{self.namespace}.svc", f"{self.name}.{self.namespace}.svc.cluster.local"]


class EgressServiceError(RuntimeError):
    """The Service named by ``egress_service`` cannot be reached through a NetworkPolicy."""


async def resolve_egress_service(core_api: Any, wanted: EgressService) -> ResolvedService:
    """Read the Service and check that a NetworkPolicy can admit its backends.

    ``core_api`` is a ``kubernetes_asyncio`` ``CoreV1Api``. Every refusal names
    the Service, so the operator sees which ``egress_service`` to fix before
    any claim exists.
    """
    ref = f"Service {wanted.namespace}/{wanted.name}"
    try:
        service = await core_api.read_namespaced_service(wanted.name, wanted.namespace)
    except Exception as exc:  # noqa: BLE001 - only 404 and 403 are translated
        status = getattr(exc, "status", None)
        if status == 404:
            raise EgressServiceError(f"egress_service: {ref} does not exist") from exc
        if status == 403:
            raise EgressServiceError(
                f"egress_service: not allowed to read {ref}; grant 'get' on 'services' in "
                f"namespace {wanted.namespace}") from exc
        raise
    spec = service.spec
    if not spec.selector:
        raise EgressServiceError(
            f"egress_service: {ref} has no selector, so there are no pods a NetworkPolicy "
            f"can match")
    if spec.cluster_ip in (None, "", "None"):
        raise EgressServiceError(
            f"egress_service: {ref} is headless (no cluster IP), so its name cannot be "
            f"pinned in the pod's hosts file")
    port = next((p for p in spec.ports or [] if p.port == wanted.port), None)
    if port is None:
        offered = ", ".join(str(p.port) for p in spec.ports or []) or "none"
        raise EgressServiceError(f"egress_service: {ref} has no port {wanted.port} "
                                 f"(ports: {offered})")
    target = port.target_port if port.target_port is not None else port.port
    if isinstance(target, str) and not target.isdecimal():
        # GKE Dataplane V2 silently drops egress to a named port in another
        # namespace, so the rule must carry the number.
        raise EgressServiceError(
            f"egress_service: {ref} port {wanted.port} has the named targetPort {target!r}; "
            f"set targetPort to the container's port number")
    return ResolvedService(
        namespace=wanted.namespace, name=wanted.name, cluster_ip=spec.cluster_ip,
        selector=tuple(sorted(spec.selector.items())), target_port=int(target),
        protocol=port.protocol or "TCP")


def egress_service_rule(service: ResolvedService) -> dict[str, Any]:
    """Egress to the Service's backend pods on its target port."""
    return {
        "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name":
                                                      service.namespace}},
                "podSelector": {"matchLabels": dict(service.selector)}}],
        "ports": [{"protocol": service.protocol, "port": service.target_port}],
    }


def cpu_quantity(cpus: int | float) -> str:
    """Whole cores as ``"2"``, fractions as millicores ``"500m"``."""
    if float(cpus).is_integer():
        return str(int(cpus))
    return f"{int(round(cpus * 1000))}m"


def dns_public_egress() -> list[dict[str, Any]]:
    """Egress to cluster DNS and to public addresses only.

    With NodeLocal DNSCache (a GKE add-on, also common elsewhere), a pod's
    queries to the kube-dns Service go to the ``node-local-dns`` pod on its
    node, which has a private pod IP and its own label. Without that peer
    every lookup times out.
    """
    kube_system = {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}
    return [
        {
            "to": [{"namespaceSelector": kube_system,
                    "podSelector": {"matchLabels": {"k8s-app": app}}}
                   for app in ("kube-dns", "node-local-dns")],
            "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
        },
        {
            "to": [
                {"ipBlock": {"cidr": "0.0.0.0/0", "except": list(_PRIVATE_V4)}},
                {"ipBlock": {"cidr": "::/0", "except": list(_PRIVATE_V6)}},
            ],
        },
    ]


def template_network_policy(options: EnvironmentOptions, internet: bool,
                            service: ResolvedService | None = None) -> dict[str, Any] | None:
    """The template's ``networkPolicy``; ``None`` keeps the controller default.

    The controller blocks all traffic in a direction whose rule list is empty,
    so a task without internet gets no egress at all, not even cluster DNS.
    No rule allows ingress, because the pod exec API reaches the pod through
    the kubelet. The operator's ``service``, when set, is the one exception
    to the task's network mode and is added to either rule list. The options
    refuse it together with the controller default, which cannot be extended.
    """
    if not internet:
        egress: list[dict[str, Any]] = []
    elif options.network == "dns-public":
        # The controller's default policy blocks every private range, cluster
        # DNS included, so a task could not resolve a hostname. Harbor tasks
        # install packages and fetch test tooling, thus DNS and public egress.
        egress = dns_public_egress()
    else:
        return None
    if service is not None:
        egress.append(egress_service_rule(service))
    return {"ingress": [], "egress": egress}


def render_pod_spec(image: str, resources: ResourceSpec, options: EnvironmentOptions,
                    workdir: str | None, service: ResolvedService | None = None) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": CONTAINER,
        "image": image,
        # Harbor drives everything through exec; the main process only has to
        # stay alive. Setting args, not command, keeps the image's ENTRYPOINT,
        # as Harbor's Docker and GKE providers do. Some task images do their
        # setup there, for example CyberGym's network allowlist.
        "args": ["sleep", "infinity"],
    }
    if workdir:
        container["workingDir"] = workdir
    res = resources.as_resources()
    if res:
        container["resources"] = res
    if options.run_as_root:
        container["securityContext"] = {"runAsUser": 0, "runAsGroup": 0}
    spec: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "containers": [container],
    }
    if options.run_as_root:
        spec["securityContext"] = {"runAsUser": 0, "runAsGroup": 0}
    if options.runtime_class:
        spec["runtimeClassName"] = options.runtime_class
    if options.node_selector:
        spec["nodeSelector"] = dict(options.node_selector)
    if options.tolerations:
        spec["tolerations"] = [dict(t) for t in options.tolerations]
    if options.image_pull_secret:
        spec["imagePullSecrets"] = [{"name": options.image_pull_secret}]
    if service is not None:
        # The pod may not query DNS, so the Service's names resolve from its hosts file.
        spec["hostAliases"] = [{"ip": service.cluster_ip, "hostnames": service.hostnames}]
    return spec


def common_labels(options: EnvironmentOptions) -> dict[str, str]:
    return {LABEL_MANAGED_BY: MANAGED_BY_VALUE, **options.extra_labels}


def render_template(*, name: str, namespace: str, image: str, resources: ResourceSpec,
                    options: EnvironmentOptions, workdir: str | None, internet: bool,
                    environment_name: str, environment_id: str,
                    service: ResolvedService | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "podTemplate": {
            "metadata": {"labels": common_labels(options)},
            "spec": render_pod_spec(image, resources, options, workdir, service),
        },
    }
    network_policy = template_network_policy(options, internet, service)
    if network_policy is not None:
        spec["networkPolicy"] = network_policy
    labels = {
        **common_labels(options),
        LABEL_ENVIRONMENT: label_value(environment_name),
        LABEL_IMAGE_HASH: short_hash(image),
    }
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "SandboxTemplate",
        "metadata": {
            "name": name, "namespace": namespace, "labels": labels,
            "annotations": {ANNOTATION_IMAGE: image, ANNOTATION_ENVIRONMENT_ID: environment_id},
        },
        "spec": spec,
    }


def render_pool(*, name: str, namespace: str, template_name: str, replicas: int,
                labels: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "SandboxWarmPool",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(labels)},
        "spec": {"replicas": replicas, "sandboxTemplateRef": {"name": template_name}},
    }


def template_name_for(options: EnvironmentOptions, image: str, resources: ResourceSpec,
                      workdir: str | None, *, internet: bool,
                      service: ResolvedService | None = None) -> str:
    """Name from everything that shapes the pair, so equal inputs share one
    template and pool, and a changed input (a new image, other resources, a
    different replica count, labels, network policy or egress Service) gets a
    new pair instead of a silent reuse."""
    pod = render_pod_spec(image, resources, options, workdir, service)
    network = template_network_policy(options, internet, service)
    key = json.dumps({"pod": pod, "network": network,
                      "replicas": options.warm_replicas, "labels": common_labels(options)},
                     sort_keys=True)
    return object_name(options.template_prefix, key)


def _is_conflict(exc: BaseException) -> bool:
    return getattr(exc, "status", None) == 409


class TemplateStore:
    """Creates the template and pool of an image once per process.

    ``custom_api`` is a ``kubernetes_asyncio`` ``CustomObjectsApi``. A 409 on
    create is success: another trial of the same task got there first, in this
    process or another.
    """

    def __init__(self, custom_api: Any, namespace: str, options: EnvironmentOptions,
                 logger: logging.Logger | None = None):
        self._api = custom_api
        self._namespace = namespace
        self._options = options
        self._logger = logger or logging.getLogger(__name__)
        self._ready: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = self._locks[name] = asyncio.Lock()
        return lock

    async def _create(self, plural: str, body: dict[str, Any]) -> bool:
        try:
            await self._api.create_namespaced_custom_object(
                group=GROUP, version=VERSION, namespace=self._namespace, plural=plural, body=body)
            return True
        except Exception as exc:  # noqa: BLE001 - only a 409 is swallowed
            if _is_conflict(exc):
                return False
            raise

    async def ensure(self, *, image: str, resources: ResourceSpec, workdir: str | None,
                     internet: bool, environment_name: str, environment_id: str,
                     service: ResolvedService | None = None) -> str:
        """Return the pool name for ``image``, creating template and pool if needed."""
        name = template_name_for(self._options, image, resources, workdir, internet=internet,
                                 service=service)
        if name in self._ready:
            return name
        async with self._lock(name):
            if name in self._ready:
                return name
            template = render_template(
                name=name, namespace=self._namespace, image=image, resources=resources,
                options=self._options, workdir=workdir, internet=internet,
                environment_name=environment_name,
                environment_id=environment_id, service=service)
            created = await self._create(TEMPLATES, template)
            pool = render_pool(name=name, namespace=self._namespace, template_name=name,
                               replicas=self._options.warm_replicas,
                               labels=template["metadata"]["labels"])
            await self._create(POOLS, pool)
            self._ready.add(name)
            self._logger.info("%s SandboxTemplate/SandboxWarmPool %s for %s (%s)",
                              "Created" if created else "Reusing", name, environment_name, image)
            return name

    async def delete_managed(self, *, plurals: tuple[str, ...] = (CLAIMS, POOLS, TEMPLATES),
                             extra_selector: str = "") -> dict[str, int]:
        """Delete every object this package labelled, claims first. Returns counts."""
        selector = f"{LABEL_MANAGED_BY}={MANAGED_BY_VALUE}"
        if extra_selector:
            selector += f",{extra_selector}"
        counts: dict[str, int] = {}
        for plural in plurals:
            listing = await self._api.list_namespaced_custom_object(
                group=GROUP, version=VERSION, namespace=self._namespace, plural=plural,
                label_selector=selector)
            items = listing.get("items") or []
            for item in items:
                name = item["metadata"]["name"]
                try:
                    await self._api.delete_namespaced_custom_object(
                        group=GROUP, version=VERSION, namespace=self._namespace,
                        plural=plural, name=name)
                except Exception as exc:  # noqa: BLE001 - a vanished object is fine
                    if getattr(exc, "status", None) != 404:
                        raise
            counts[plural] = len(items)
        return counts
