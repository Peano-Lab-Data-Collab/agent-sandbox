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

"""Compose parsing: what maps onto a pod, what is refused, and how."""

import pytest

from k8s_agent_sandbox_harbor.compose import ComposeSpec, Readiness, Sidecar, parse_compose

from .compose_samples import CYBERGYM_COMPOSE

SRC = "environment/docker-compose.yaml"


def parse(text, environ=None, image_for=lambda service, image: image or f"reg/{service}:1"):
    return parse_compose(text, source=SRC, environ=environ or {}, image_for=image_for)


def sidecar(**fields):
    """The one sidecar of a compose file built from ``fields``."""
    body = "".join(f"    {k}: {v}\n" for k, v in fields.items())
    (only,) = parse(f"services:\n  svc:\n    image: img:1\n{body}").sidecars
    return only


def test_cybergym_compose_becomes_main_settings_and_a_task_server_sidecar():
    environ = {"ALLOWED_HOSTS": "llm.example.com", "ANTHROPIC_BASE_URL": "https://gw.example.com"}
    seen = []

    def image_for(service, compose_image):
        seen.append((service, compose_image))
        return "reg/cybergym_arvo_1065:task-server"

    spec = parse(CYBERGYM_COMPOSE, environ, image_for)

    assert seen == [("task-server", None)]
    env = dict(spec.main_env)
    assert env["ALLOWED_HOSTS"] == "llm.example.com"
    assert env["ANTHROPIC_BASE_URL"] == "https://gw.example.com"
    assert env["OPENAI_BASE_URL"] == ""
    assert spec.main_capabilities == ("NET_ADMIN",)
    (task_server,) = spec.sidecars
    assert task_server == Sidecar(
        name="task-server", image="reg/cybergym_arvo_1065:task-server",
        env=(("AUTH_TOKEN", "f7748496b9e994156c685b4bf7c7afb8"),),
        readiness=Readiness(command=("python3", "-c", "import urllib.request; "
                                     "urllib.request.urlopen('http://localhost:9111/health')"),
                            period_s=3, timeout_s=5),
        cpu_limit="2", memory_limit="4096Mi")


def test_a_compose_file_without_main_adds_only_sidecars():
    spec = parse("services:\n  db:\n    image: postgres:16\n")
    assert spec == ComposeSpec(sidecars=(Sidecar(name="db", image="postgres:16"),))


@pytest.mark.parametrize("snippet,key", [
    ("  svc:\n    image: i\n    ports: ['80:80']\n", "ports"),
    ("  svc:\n    image: i\n    volumes: ['/a:/b']\n", "volumes"),
    ("  svc:\n    image: i\n    network_mode: host\n", "network_mode"),
    ("  svc:\n    image: i\n    privileged: true\n", "privileged"),
    ("  svc:\n    image: i\n    shm_size: 1g\n", "shm_size"),
    ("  svc:\n    image: i\n    entrypoint: ['/x']\n", "entrypoint"),
    ("  svc:\n    image: i\n    deploy:\n      replicas: 2\n", "deploy.replicas"),
    ("  svc:\n    image: i\n    deploy:\n      resources:\n        reservations:\n"
     "          cpus: '1'\n", "deploy.resources.reservations"),
    ("  svc:\n    image: i\n    healthcheck:\n      test: ['NONE']\n      exotic: 1\n",
     "healthcheck.exotic"),
    ("  main:\n    healthcheck:\n      test: ['CMD', 'true']\n", "healthcheck"),
    ("  main:\n    volumes: ['/a:/b']\n", "volumes"),
])
def test_unsupported_service_keys_name_the_file_the_service_and_the_key(snippet, key):
    service = "main" if snippet.startswith("  main") else "svc"
    with pytest.raises(ValueError) as err:
        parse(f"services:\n{snippet}")
    message = str(err.value)
    assert SRC in message and repr(service) in message and repr(key) in message


def test_unsupported_top_level_key_names_the_file_and_the_key():
    with pytest.raises(ValueError, match=r"docker-compose.yaml.*'volumes'"):
        parse("services: {}\nvolumes:\n  data: {}\n")


def test_tolerates_the_version_key():
    assert parse("version: '3.8'\nservices: {}\n") == ComposeSpec()


@pytest.mark.parametrize("snippet,message", [
    ("    platform: linux/s390x\n", "platform"),
    ("    depends_on:\n      other:\n        condition: service_completed_successfully\n",
     "service_completed_successfully"),
    ("    command: 3\n", "command"),
    ("    deploy:\n      resources:\n        limits:\n          memory: lots\n", "memory"),
    ("    healthcheck:\n      test: ['CMD']\n", "CMD"),
    ("    healthcheck:\n      test: ['CMD', 'true']\n      interval: soon\n", "duration"),
])
def test_unsupported_values_are_refused_with_the_service_named(snippet, message):
    with pytest.raises(ValueError, match=rf"'svc'.*{message}"):
        parse(f"services:\n  svc:\n    image: i\n{snippet}")


def test_a_service_name_that_cannot_name_a_container_is_refused():
    with pytest.raises(ValueError, match="'Task_Server'.*DNS label"):
        parse("services:\n  Task_Server:\n    image: i\n")


def test_the_platform_of_the_services_is_recorded_for_the_pod():
    assert parse("services:\n  a:\n    image: i\n    platform: linux/amd64\n"
                 "  b:\n    image: i\n    platform: linux/amd64\n  c:\n    image: i\n"
                 ).platform == "linux/amd64"
    assert parse("services:\n  a:\n    image: i\n").platform is None


def test_services_that_declare_different_platforms_are_refused_because_a_pod_has_one_node():
    with pytest.raises(ValueError, match=r"different platforms.*linux/amd64 on a.*linux/arm64 on b"):
        parse("services:\n  a:\n    image: i\n    platform: linux/amd64\n"
              "  b:\n    image: i\n    platform: linux/arm64\n")


@pytest.mark.parametrize("text,environ,expected", [
    ("${A}", {"A": "x"}, "x"),
    ("$A", {"A": "x"}, "x"),
    ("${A}", {}, ""),
    ("${A:-fallback}", {}, "fallback"),
    ("${A:-fallback}", {"A": ""}, "fallback"),
    ("${A-fallback}", {"A": ""}, ""),
    ("${A-fallback}", {}, "fallback"),
    ("${A:-fallback}", {"A": "set"}, "set"),
    ("price: $$5", {}, "price: $5"),
    ("pre-${A}-post", {"A": "x"}, "pre-x-post"),
])
def test_interpolation_follows_compose(text, environ, expected):
    spec = parse(f"services:\n  main:\n    environment:\n      - \"V={text}\"\n", environ)
    assert spec.main_env == (("V", expected),)


@pytest.mark.parametrize("text", ["${A:?missing}", "${A:+yes}", "cost $5", "${1}"])
def test_interpolation_forms_this_package_cannot_honor_are_refused(text):
    with pytest.raises(ValueError, match="'main'.*interpolation"):
        parse(f"services:\n  main:\n    environment:\n      - \"V={text}\"\n")


@pytest.mark.parametrize("text", ["${A:-${B:-x}}", "${A-${B}}", "pre-${A:-${B}}-post",
                                  "${A:-$B}", "${A:-$$}"])
def test_nested_interpolation_is_refused_instead_of_cut_at_the_inner_brace(text):
    with pytest.raises(ValueError, match="'main'.*nested interpolation.*not supported"):
        parse(f"services:\n  main:\n    environment:\n      - \"V={text}\"\n",
              {"B": "b"})


def test_environment_accepts_a_mapping_and_values_from_the_process():
    text = ("services:\n  main:\n    environment:\n      PLAIN: a\n      FLAG: true\n"
            "      COUNT: 3\n      FROM_HOST:\n      MISSING:\n")
    assert parse(text, {"FROM_HOST": "h"}).main_env == (
        ("PLAIN", "a"), ("FLAG", "true"), ("COUNT", "3"), ("FROM_HOST", "h"))
    listed = parse("services:\n  main:\n    environment:\n      - FROM_HOST\n      - NOPE\n",
                   {"FROM_HOST": "h"})
    assert listed.main_env == (("FROM_HOST", "h"),)


def test_capabilities_drop_docker_s_cap_prefix():
    spec = parse("services:\n  main:\n    cap_add: [CAP_NET_ADMIN, net_raw]\n")
    assert spec.main_capabilities == ("NET_ADMIN", "NET_RAW")


@pytest.mark.parametrize("cap", ["ALL", "SYS_ADMIN", "cap_sys_ptrace"])
def test_capabilities_beyond_the_network_ones_are_refused_for_the_root_main_container(cap):
    shown = cap.upper().removeprefix("CAP_")
    with pytest.raises(ValueError, match=rf"'main'.*cap_add '{shown}'.*NET_ADMIN, NET_RAW"):
        parse(f"services:\n  main:\n    cap_add: [NET_ADMIN, {cap}]\n")


@pytest.mark.parametrize("test,command", [
    ("['CMD', 'curl', '-f', 'http://localhost/']", ("curl", "-f", "http://localhost/")),
    ("['CMD-SHELL', 'curl -f http://localhost/ || exit 1']",
     ("/bin/sh", "-c", "curl -f http://localhost/ || exit 1")),
    ("'test -f /ready'", ("/bin/sh", "-c", "test -f /ready")),
])
def test_healthcheck_test_forms_become_an_exec_probe(test, command):
    assert sidecar(healthcheck=f"{{test: {test}}}").readiness.command == command


@pytest.mark.parametrize("healthcheck", ["{test: ['NONE']}", "{disable: true}", "{interval: 5s}"])
def test_a_healthcheck_that_checks_nothing_adds_no_probe(healthcheck):
    assert sidecar(healthcheck=healthcheck).readiness is None


@pytest.mark.parametrize("duration,seconds", [
    ("3s", 3), ("1m30s", 90), ("500ms", 1), ("1h", 3600), ("0s", 1), ("1.5s", 2),
])
def test_durations_become_whole_seconds_of_at_least_one(duration, seconds):
    probe = sidecar(healthcheck=f"{{test: ['CMD', 'true'], interval: {duration}, "
                                f"timeout: {duration}}}").readiness
    assert (probe.period_s, probe.timeout_s) == (seconds, seconds)


def test_a_probe_without_timings_gets_defaults():
    probe = sidecar(healthcheck="{test: ['CMD', 'true']}").readiness
    assert (probe.period_s, probe.timeout_s) == (10, 30)


def test_retries_and_start_period_are_accepted_and_ignored():
    hc = "{test: ['CMD', 'true'], retries: 5, start_period: 20s, start_interval: 1s}"
    assert sidecar(healthcheck=hc).readiness == Readiness(("true",), 10, 30)


@pytest.mark.parametrize("command,args", [
    ("['python3', '-u', '/srv.py']", ("python3", "-u", "/srv.py")),
    ("python3 -u '/my srv.py'", ("python3", "-u", "/my srv.py")),
])
def test_command_becomes_args_and_leaves_the_entrypoint_alone(command, args):
    assert sidecar(command=command).args == args
    assert sidecar().args is None


@pytest.mark.parametrize("memory,quantity", [
    ("4096M", "4096Mi"), ("4g", "4096Mi"), ("512m", "512Mi"), ("1GB", "1024Mi"),
    ("1048576", "1Mi"), ("1000", "1000"),
])
def test_memory_limits_keep_compose_units(memory, quantity):
    limits = f"{{resources: {{limits: {{memory: '{memory}'}}}}}}"
    assert sidecar(deploy=limits).memory_limit == quantity


@pytest.mark.parametrize("cpus,quantity", [("2", "2"), ("0.5", "0.5"), ("1.50", "1.5")])
def test_cpu_limits(cpus, quantity):
    assert sidecar(deploy=f"{{resources: {{limits: {{cpus: '{cpus}'}}}}}}").cpu_limit == quantity


def test_a_sidecar_with_an_image_hands_it_to_the_image_function():
    seen = []
    parse("services:\n  db:\n    image: postgres:16\n", image_for=lambda s, i: seen.append((s, i)) or "x")
    assert seen == [("db", "postgres:16")]


def test_a_service_with_build_takes_its_image_from_the_image_function_not_from_image():
    seen = []
    spec = parse("services:\n  db:\n    image: postgres:16\n    build: ./db\n",
                 image_for=lambda s, i: seen.append((s, i)) or "reg/built:db")
    assert seen == [("db", None)]
    assert spec.sidecars[0].image == "reg/built:db"


def test_a_missing_image_error_from_the_image_function_passes_through():
    class Missing(RuntimeError):
        pass

    def image_for(service, compose_image):
        raise Missing(service)

    with pytest.raises(Missing):
        parse(CYBERGYM_COMPOSE, image_for=image_for)


def test_invalid_yaml_names_the_file():
    with pytest.raises(ValueError, match="docker-compose.yaml: not valid YAML"):
        parse("services: [unclosed")
