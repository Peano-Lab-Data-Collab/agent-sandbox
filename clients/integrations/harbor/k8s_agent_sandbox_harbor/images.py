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

"""Which image runs a Harbor task, and where it is pulled from.

Harbor environments acquire their own image. This package does not build:
a task must name a prebuilt ``docker_image`` (as every Terminal-Bench 2.0 task
does), or an image map must list one for it. An optional mirror prefix moves
pulls off Docker Hub.

An image map entry is either one image for the main container or an object
that adds an image for each compose service that has ``build:``::

    {"cybergym_arvo_1065": {"main": "reg/cybergym_arvo_1065:main",
                            "task-server": "reg/cybergym_arvo_1065:task-server"}}
"""

from __future__ import annotations

import json
from pathlib import Path

from .options import EnvironmentOptions

_HUB_HOSTS = ("docker.io", "index.docker.io", "registry-1.docker.io")
_MAIN = "main"


class ImageUnavailableError(RuntimeError):
    """The task has no image this environment can run."""


def rewrite_registry(image: str, mirror: str | None) -> str:
    """``image`` with its registry replaced by ``mirror``; unchanged without one.

    Docker Hub shorthand is expanded first, so ``python:3.13`` maps to
    ``<mirror>/library/python:3.13`` and ``alexgshaw/task:v1`` to
    ``<mirror>/alexgshaw/task:v1``, which is how registry remotes mirror the hub.
    An image from another registry keeps its path and loses its host.
    """
    if not mirror:
        return image
    first, sep, rest = image.partition("/")
    has_registry = bool(sep) and ("." in first or ":" in first or first == "localhost")
    path = rest if has_registry else image
    from_hub = not has_registry or first in _HUB_HOSTS
    if from_hub and "/" not in path:
        path = f"library/{path}"
    return f"{mirror.rstrip('/')}/{path}"


class ImageResolver:
    """Resolve the image of a task from its config, an image map, and a mirror."""

    def __init__(self, options: EnvironmentOptions):
        self._options = options
        self._map: dict[str, dict[str, str]] | None = None

    def _image_map(self) -> dict[str, dict[str, str]]:
        """The image map with every entry as ``{service: image}``; a plain
        string entry is the image of ``main``."""
        if self._map is None:
            path = self._options.image_map
            if path is None:
                self._map = {}
            else:
                data = json.loads(Path(path).read_text())
                if not isinstance(data, dict):
                    raise ValueError(f"image_map {path} must hold a JSON object")
                self._map = {str(k): _parse_entry(path, k, v) for k, v in data.items()}
        return self._map

    def _entry(self, environment_id: str, environment_name: str) -> dict[str, str]:
        mapping = self._image_map()
        return mapping.get(environment_id) or mapping.get(environment_name) or {}

    def resolve(self, *, docker_image: str | None, environment_id: str,
                environment_name: str, has_dockerfile: bool) -> str:
        """The image to run, mirrored when a mirror is configured."""
        image = docker_image or self._entry(environment_id, environment_name).get(_MAIN)
        if not image:
            how = ("build its environment/Dockerfile, push it, and list it in "
                   "--ek image_map=<file>" if has_dockerfile
                   else "set [environment].docker_image in task.toml")
            raise ImageUnavailableError(
                f"task {environment_name!r} ({environment_id}) has no prebuilt image: "
                f"{how}. This environment does not build images.")
        return rewrite_registry(image, self._options.image_mirror)

    def resolve_service(self, service: str, *, compose_image: str | None, environment_id: str,
                        environment_name: str) -> str:
        """The image of a compose service other than ``main``, mirrored when a
        mirror is configured. ``compose_image`` is the compose ``image:`` of a service
        without ``build:``. A service with ``build:`` passes None and gets its
        image from the map, because Compose runs the image it builds."""
        image = compose_image or self._entry(environment_id, environment_name).get(service)
        if not image:
            raise ImageUnavailableError(
                f"task {environment_name!r} ({environment_id}) service {service!r} has no "
                f"prebuilt image: build its compose context, push it, and list it as "
                f"{service!r} in the task's object entry of --ek image_map=<file>. "
                f"This environment does not build images.")
        return rewrite_registry(image, self._options.image_mirror)


def _parse_entry(path: str, key: str, value: object) -> dict[str, str]:
    if isinstance(value, str):
        return {_MAIN: value}
    if isinstance(value, dict):
        return {str(service): str(image) for service, image in value.items()}
    raise ValueError(f"image_map {path}: the entry for {key!r} must be an image string or an "
                     f"object of service images")
