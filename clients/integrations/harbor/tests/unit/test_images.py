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

"""Image resolution: docker_image, image map, mirror rewriting, clear errors."""

import json

import pytest

from k8s_agent_sandbox_harbor.images import ImageResolver, ImageUnavailableError, rewrite_registry
from k8s_agent_sandbox_harbor.options import EnvironmentOptions

M = "us-central1-docker.pkg.dev/proj/hub-mirror"


@pytest.mark.parametrize("image,expected", [
    ("python:3.13-slim", f"{M}/library/python:3.13-slim"),
    ("alexgshaw/hello-world:v1", f"{M}/alexgshaw/hello-world:v1"),
    ("docker.io/alexgshaw/hello-world:v1", f"{M}/alexgshaw/hello-world:v1"),
    ("ghcr.io/org/img@sha256:abc", f"{M}/org/img@sha256:abc"),
    ("localhost:5000/img:1", f"{M}/img:1"),
])
def test_rewrite_registry(image, expected):
    assert rewrite_registry(image, M) == expected
    assert rewrite_registry(image, None) == image


def test_resolver_prefers_docker_image_then_map_then_errors(tmp_path):
    env_map = tmp_path / "images.json"
    env_map.write_text(json.dumps({"abc123": "reg/built:abc123", "by-name": "reg/built:name"}))
    r = ImageResolver(EnvironmentOptions(image_map=str(env_map), image_mirror=M))
    assert r.resolve(docker_image="alexgshaw/t:1", environment_id="abc123", environment_name="t",
                     has_dockerfile=True) == f"{M}/alexgshaw/t:1"
    assert r.resolve(docker_image=None, environment_id="abc123", environment_name="t",
                     has_dockerfile=True) == f"{M}/reg/built:abc123"
    assert r.resolve(docker_image=None, environment_id="zzz", environment_name="by-name",
                     has_dockerfile=True) == f"{M}/reg/built:name"
    with pytest.raises(ImageUnavailableError, match="image_map"):
        r.resolve(docker_image=None, environment_id="zzz", environment_name="other",
                  has_dockerfile=True)
    with pytest.raises(ImageUnavailableError, match="docker_image"):
        ImageResolver(EnvironmentOptions()).resolve(docker_image=None, environment_id="z",
                                                    environment_name="o", has_dockerfile=False)


def test_object_form_map_entries_give_main_and_service_images(tmp_path):
    env_map = tmp_path / "images.json"
    env_map.write_text(json.dumps({
        "cybergym_arvo_1065": {"main": "reg/t:main", "task-server": "reg/t:task-server"},
        "plain": "reg/plain:1"}))
    r = ImageResolver(EnvironmentOptions(image_map=str(env_map)))
    assert r.resolve(docker_image=None, environment_id="x", environment_name="cybergym_arvo_1065",
                     has_dockerfile=True) == "reg/t:main"
    assert r.resolve_service("task-server", compose_image=None, environment_id="x",
                             environment_name="cybergym_arvo_1065") == "reg/t:task-server"
    # The string form still names the main image.
    assert r.resolve(docker_image=None, environment_id="x", environment_name="plain",
                     has_dockerfile=True) == "reg/plain:1"


def test_a_build_only_service_needs_a_map_entry(tmp_path):
    env_map = tmp_path / "images.json"
    env_map.write_text(json.dumps({"t": "reg/t:main", "u": {"main": "reg/u:main"}}))
    r = ImageResolver(EnvironmentOptions(image_map=str(env_map)))
    for name in ("t", "u", "unknown"):
        with pytest.raises(ImageUnavailableError, match=rf"task '{name}'.*service 'task-server'"):
            r.resolve_service("task-server", compose_image=None, environment_id="x",
                              environment_name=name)
    with pytest.raises(ImageUnavailableError, match="task 't'"):
        ImageResolver(EnvironmentOptions()).resolve_service(
            "task-server", compose_image=None, environment_id="x", environment_name="t")


def test_a_service_with_an_image_needs_no_map_and_goes_through_the_mirror():
    r = ImageResolver(EnvironmentOptions(image_mirror=M))
    assert r.resolve_service("db", compose_image="postgres:16", environment_id="x",
                             environment_name="t") == f"{M}/library/postgres:16"


def test_an_image_map_entry_must_be_a_string_or_an_object(tmp_path):
    env_map = tmp_path / "images.json"
    env_map.write_text(json.dumps({"t": ["reg/t:main"]}))
    with pytest.raises(ValueError, match="'t'"):
        ImageResolver(EnvironmentOptions(image_map=str(env_map))).resolve(
            docker_image=None, environment_id="x", environment_name="t", has_dockerfile=True)


def test_a_digest_pinned_map_value_reaches_the_pod_unchanged_without_a_mirror(tmp_path):
    pinned = "reg/t:task-server@sha256:" + "a" * 64
    env_map = tmp_path / "images.json"
    env_map.write_text(json.dumps({"t": {"main": "reg/t:main@sha256:" + "b" * 64,
                                         "task-server": pinned}}))
    r = ImageResolver(EnvironmentOptions(image_map=str(env_map)))
    assert r.resolve_service("task-server", compose_image=None, environment_id="x",
                             environment_name="t") == pinned
    assert rewrite_registry(pinned, None) == pinned
