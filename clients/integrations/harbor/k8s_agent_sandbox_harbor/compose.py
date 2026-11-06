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

"""A task's ``docker-compose.yaml`` as sidecar containers of the sandbox pod.

Compose runs each service in its own container on a shared network. A pod
gives the same thing: containers that share one network namespace, so a
sidecar answers on ``localhost`` and ``hostAliases`` lets the main container
reach it by its service name. :func:`parse_compose` reads the keys that map
onto a pod and rejects every other key, so a task that needs more than this
package can provide fails before a claim exists instead of running wrong.

The parse function is pure. The caller passes the process environment for
``${VAR}`` interpolation and a function that picks the image of each
sidecar, because only the caller knows the image map.
"""

from __future__ import annotations

import math
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import yaml

MAIN_SERVICE = "main"

# A key maps to a nested table to restrict its children, or to None to accept
# any value. Every key not listed is rejected.
_Shape = Mapping[str, "_Shape | None"]


def _keys(*names: str) -> dict[str, None]:
    return dict.fromkeys(names)


_TOP_LEVEL: _Shape = _keys("services", "version")
_MAIN: _Shape = _keys("environment", "cap_add", "depends_on", "build", "image")
_SIDECAR: _Shape = {
    **_keys("image", "build", "environment", "expose", "command", "depends_on", "platform"),
    # Compose's retry count and grace period have no readiness-probe equivalent:
    # the probe keeps running until the pod is Ready or the claim times out.
    "healthcheck": _keys("test", "interval", "timeout", "retries", "start_period",
                         "start_interval", "disable"),
    "deploy": {"resources": {"limits": _keys("cpus", "memory")}},
}
_DEPENDS_ON_ENTRY: _Shape = _keys("condition")
_DEPENDS_ON_CONDITIONS = frozenset({"service_started", "service_healthy"})
# One pod runs on one node, so the pod gets one architecture. The value after
# the slash is the kubernetes.io/arch node label.
_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})
# The main container runs as root, so any other capability would hand a task
# more than the network setup it needs.
_MAIN_CAPABILITIES = frozenset({"NET_ADMIN", "NET_RAW"})

# Compose's own default timeout is 30s; its 30s interval would delay a pod's
# readiness by that long, so the probe period defaults to the Kubernetes value.
_DEFAULT_PERIOD_S = 10
_DEFAULT_TIMEOUT_S = 30

_SERVICE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_INTERPOLATION = re.compile(
    r"\$(?:(?P<escaped>\$)|(?P<bare>[A-Za-z_]\w*)"
    r"|\{(?P<name>[A-Za-z_]\w*)(?:(?P<op>:?[-?+])(?P<arg>[^}]*))?\}|(?P<invalid>))")
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(us|ms|s|m|h)")
_SECONDS_PER_UNIT = {"us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
_BYTES = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt]?)b?$", re.IGNORECASE)
_BYTES_PER_UNIT = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}


@dataclass(frozen=True)
class Readiness:
    """An exec readiness probe, the Kubernetes form of a compose healthcheck."""
    command: tuple[str, ...]
    period_s: int
    timeout_s: int


@dataclass(frozen=True)
class Sidecar:
    """One compose service other than ``main``, as a container of the pod."""
    name: str
    image: str
    args: tuple[str, ...] | None = None
    """Replaces the image's CMD and keeps its ENTRYPOINT, as compose ``command`` does."""
    env: tuple[tuple[str, str], ...] = ()
    readiness: Readiness | None = None
    cpu_limit: str | None = None
    memory_limit: str | None = None


@dataclass(frozen=True)
class ComposeSpec:
    """What a task's compose file adds to the sandbox pod."""
    main_env: tuple[tuple[str, str], ...] = ()
    main_capabilities: tuple[str, ...] = ()
    sidecars: tuple[Sidecar, ...] = ()
    platform: str | None = None
    """The ``platform`` every service that declares one agrees on, such as ``linux/amd64``."""


def parse_compose(text: str, *, source: str, environ: Mapping[str, str],
                  image_for: Callable[[str, str | None], str]) -> ComposeSpec:
    """Parse the compose file ``text`` of a task.

    ``source`` names the file in error messages. ``environ`` supplies
    ``${VAR}`` and ``${VAR:-default}`` values the way Compose reads the
    process environment. ``image_for(service, compose_image)`` returns the
    image of a sidecar, given its compose ``image:``, or None when the service
    has ``build:`` because Compose then runs the image it builds. Raises ``ValueError`` that names ``source``, the service and
    the key for anything this package cannot run.
    """
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{source}: not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{source}: expected a mapping with a 'services' key")
    _check_keys(document, _TOP_LEVEL, source, "top-level key")
    services = document.get("services") or {}
    if not isinstance(services, dict):
        raise ValueError(f"{source}: 'services' must be a mapping")

    main_env: tuple[tuple[str, str], ...] = ()
    main_capabilities: tuple[str, ...] = ()
    sidecars: list[Sidecar] = []
    platforms: dict[str, list[str]] = {}
    for name, raw in services.items():
        where = f"{source}: service {name!r}"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: expected a mapping")
        if name == MAIN_SERVICE:
            _check_keys(raw, _MAIN, where)
            service = _interpolate_tree(raw, environ, where)
            main_env = _env_pairs(service.get("environment"), environ, where)
            main_capabilities = _capabilities(service.get("cap_add"), where)
            _check_depends_on(service.get("depends_on"), where)
        else:
            sidecar, platform = _sidecar(str(name), raw, environ, image_for, where)
            sidecars.append(sidecar)
            if platform:
                platforms.setdefault(platform, []).append(sidecar.name)
    if len(platforms) > 1:
        declared = "; ".join(f"{p} on {', '.join(names)}" for p, names in sorted(platforms.items()))
        raise ValueError(f"{source}: services declare different platforms ({declared}), but one "
                         f"pod runs on one node")
    return ComposeSpec(main_env=main_env, main_capabilities=main_capabilities,
                       sidecars=tuple(sidecars), platform=next(iter(platforms), None))


def _sidecar(name: str, raw: dict[str, Any], environ: Mapping[str, str],
             image_for: Callable[[str, str | None], str],
             where: str) -> tuple[Sidecar, str | None]:
    if not _SERVICE_NAME.fullmatch(name) or len(name) > 63:
        raise ValueError(f"{where}: the name must be a lowercase DNS label because it names a "
                         f"container and a hostname in the pod")
    _check_keys(raw, _SIDECAR, where)
    service = _interpolate_tree(raw, environ, where)
    platform = service.get("platform")
    if platform is not None and platform not in _PLATFORMS:
        raise ValueError(f"{where}: unsupported platform {platform!r}; only "
                         f"{', '.join(sorted(_PLATFORMS))} runs here")
    _check_depends_on(service.get("depends_on"), where)
    limits = _limits(service.get("deploy"), where)
    image = None if "build" in service else service.get("image")
    sidecar = Sidecar(
        name=name,
        image=image_for(name, image),
        args=_command(service.get("command"), where),
        env=_env_pairs(service.get("environment"), environ, where),
        readiness=_readiness(service.get("healthcheck"), where),
        cpu_limit=_cpu_limit(limits.get("cpus"), where),
        memory_limit=_memory_limit(limits.get("memory"), where),
    )
    return sidecar, platform


def _limits(deploy: Any, where: str) -> Mapping[str, Any]:
    node: Any = deploy
    for key in ("resources", "limits"):
        if node is None:
            return {}
        if not isinstance(node, dict):
            raise ValueError(f"{where}: 'deploy' must hold resources.limits as mappings")
        node = node.get(key)
    if node is not None and not isinstance(node, dict):
        raise ValueError(f"{where}: 'deploy' must hold resources.limits as mappings")
    return node or {}


def _check_keys(mapping: Mapping[str, Any], shape: _Shape, where: str,
                kind: str = "key", prefix: str = "") -> None:
    for key, value in mapping.items():
        path = f"{prefix}{key}"
        if key not in shape:
            raise ValueError(f"{where}: unsupported {kind} {path!r}. This environment runs "
                             f"compose services as sidecar containers of one pod and supports "
                             f"only {', '.join(sorted(shape))}")
        child = shape[key]
        if child is not None and isinstance(value, Mapping):
            _check_keys(value, child, where, kind, prefix=f"{path}.")


def _interpolate_tree(value: Any, environ: Mapping[str, str], where: str) -> Any:
    if isinstance(value, str):
        return _interpolate(value, environ, where)
    if isinstance(value, list):
        return [_interpolate_tree(v, environ, where) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate_tree(v, environ, where) for k, v in value.items()}
    return value


def _interpolate(text: str, environ: Mapping[str, str], where: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if match["escaped"]:
            return "$"
        if match["invalid"] is not None:
            raise ValueError(f"{where}: invalid interpolation in {text!r}; write $$ for a "
                             f"literal dollar sign")
        name = match["bare"] or match["name"]
        value = environ.get(name)
        op = match["op"]
        if match["arg"] and "$" in match["arg"]:
            raise ValueError(f"{where}: nested interpolation in {text!r} is not supported; "
                             f"write the default as plain text")
        if op is None:
            return value or ""
        if op in ("-", ":-"):
            return match["arg"] if value is None or (op == ":-" and value == "") else value
        raise ValueError(f"{where}: unsupported interpolation operator {op!r} in {text!r}; "
                         f"only ${{VAR}}, ${{VAR-default}} and ${{VAR:-default}} work")

    return _INTERPOLATION.sub(replace, text)


def _env_pairs(value: Any, environ: Mapping[str, str], where: str) -> tuple[tuple[str, str], ...]:
    """``environment`` as ordered pairs. An entry without a value takes the
    process environment's value and is dropped when that is unset, as in Compose."""
    if value is None:
        return ()
    if isinstance(value, dict):
        entries = [(str(k), None if v is None else _env_text(v)) for k, v in value.items()]
    elif isinstance(value, list):
        entries = []
        for item in value:
            key, sep, val = str(item).partition("=")
            entries.append((key, val if sep else None))
    else:
        raise ValueError(f"{where}: 'environment' must be a list or a mapping")
    out: dict[str, str] = {}
    for key, val in entries:
        if val is None:
            val = environ.get(key)
        if val is not None:
            out[key] = val
    return tuple(out.items())


def _env_text(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _capabilities(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{where}: 'cap_add' must be a list")
    # Docker accepts a CAP_ prefix that Kubernetes capability names leave out.
    caps = tuple(str(cap).upper().removeprefix("CAP_") for cap in value)
    for cap in caps:
        if cap not in _MAIN_CAPABILITIES:
            raise ValueError(f"{where}: unsupported cap_add {cap!r}; only "
                             f"{', '.join(sorted(_MAIN_CAPABILITIES))} can be added")
    return caps


def _check_depends_on(value: Any, where: str) -> None:
    """Containers of a pod start together, so no dependency order survives.
    The pod becoming Ready on every readiness probe is what ``service_healthy``
    asks for, so only a condition that needs something else is refused."""
    if not isinstance(value, dict):
        return
    for target, entry in value.items():
        _check_keys(entry or {}, _DEPENDS_ON_ENTRY, f"{where}: depends_on {target!r}")
        condition = (entry or {}).get("condition", "service_started")
        if condition not in _DEPENDS_ON_CONDITIONS:
            raise ValueError(f"{where}: unsupported depends_on condition {condition!r} for "
                             f"{target!r}; use one of {', '.join(sorted(_DEPENDS_ON_CONDITIONS))}")


def _command(value: Any, where: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return tuple(shlex.split(value))
    if isinstance(value, list):
        return tuple(str(part) for part in value)
    raise ValueError(f"{where}: 'command' must be a string or a list")


def _readiness(value: Any, where: str) -> Readiness | None:
    if value is None:
        return None
    where = f"{where}: healthcheck"
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected a mapping")
    if value.get("disable") is True:
        return None
    command = _probe_command(value.get("test"), where)
    if command is None:
        return None
    return Readiness(command=command,
                     period_s=_seconds(value.get("interval"), _DEFAULT_PERIOD_S, where),
                     timeout_s=_seconds(value.get("timeout"), _DEFAULT_TIMEOUT_S, where))


def _probe_command(test: Any, where: str) -> tuple[str, ...] | None:
    if test is None:
        return None
    if isinstance(test, str):
        return ("/bin/sh", "-c", test)
    if isinstance(test, list) and test:
        kind, *rest = (str(part) for part in test)
        if kind == "NONE":
            return None
        if kind == "CMD" and rest:
            return tuple(rest)
        if kind == "CMD-SHELL" and len(rest) == 1:
            return ("/bin/sh", "-c", rest[0])
    raise ValueError(f"{where}: unsupported test {test!r}; use [\"CMD\", ...], "
                     f"[\"CMD-SHELL\", \"...\"], a string or [\"NONE\"]")


def _seconds(value: Any, default: int, where: str) -> int:
    """A Compose duration such as ``3s``, ``1m30s`` or ``500ms`` in whole
    seconds, rounded up to at least one because probe fields are integers."""
    if value is None:
        return default
    text = str(value)
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"{where}: invalid duration {value!r}; write it like 3s, 1m30s or 500ms")
    total = sum(float(n) * _SECONDS_PER_UNIT[u] for n, u in parts)
    return max(1, math.ceil(total))


def _cpu_limit(value: Any, where: str) -> str | None:
    if value is None:
        return None
    try:
        cpus = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: invalid cpus {value!r}") from None
    return f"{cpus:g}"


def _memory_limit(value: Any, where: str) -> str | None:
    """A Compose byte value such as ``4096M`` as a Kubernetes quantity. Compose
    reads ``M`` as 1024 * 1024 bytes, where a Kubernetes ``M`` is 10^6."""
    if value is None:
        return None
    match = _BYTES.match(str(value).strip())
    if not match:
        raise ValueError(f"{where}: invalid memory {value!r}; write it like 512M or 4g")
    size = int(float(match[1]) * _BYTES_PER_UNIT[match[2].lower()])
    return f"{size >> 20}Mi" if size % (1 << 20) == 0 else str(size)
