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

"""The image build script: which images it builds, how it tags and rewrites them."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "tools" / "build_task_images.py"
spec = importlib.util.spec_from_file_location("build_task_images", SCRIPT)
script = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = script
spec.loader.exec_module(script)

REGISTRY = "us-central1-docker.pkg.dev/proj/repo"
PUSHED = "sha256:" + "1" * 64
EXISTING = "sha256:" + "2" * 64
ARVO_MIRROR = r"^n132/arvo:(\d+)-(vul|fix)$=us-central1-docker.pkg.dev/proj/arvo/\1:\2"

MAIN_DOCKERFILE = """\
# FROM commented-out
FROM --platform=linux/amd64 n132/arvo:1065-vul
RUN true
"""
SERVER_DOCKERFILE = """\
FROM --platform=linux/amd64 n132/arvo:1065-fix AS fix-runner
FROM --platform=linux/amd64 n132/arvo:1065-vul
COPY --from=fix-runner /out/ /out/
"""
COMPOSE = """\
services:
  main:
    cap_add: [NET_ADMIN]
  task-server:
    build:
      context: ./task-server
  cache:
    image: redis:7
"""


def write_task(root: Path, name: str, *, compose: str | None = COMPOSE, docker_image: str = ""):
    task = root / name
    (task / "environment" / "task-server").mkdir(parents=True)
    extra = f'\n[environment]\ndocker_image = "{docker_image}"\n' if docker_image else ""
    (task / "task.toml").write_text(f'[task]\nname = "sunblaze-ucb/{name}"\n{extra}')
    (task / "environment" / "Dockerfile").write_text(MAIN_DOCKERFILE)
    (task / "environment" / "task-server" / "Dockerfile").write_text(SERVER_DOCKERFILE)
    if compose is not None:
        (task / "environment" / "docker-compose.yaml").write_text(compose)
    return task


def test_plan_builds_main_and_each_service_with_a_build(tmp_path):
    write_task(tmp_path, "Cybergym_Arvo_1065")
    builds = script.plan_builds(script.discover_tasks(tmp_path), REGISTRY + "/")
    env_dir = tmp_path / "Cybergym_Arvo_1065" / "environment"
    assert [(b.service, b.tag, b.context, b.dockerfile) for b in builds] == [
        ("main", f"{REGISTRY}/cybergym_arvo_1065:main", env_dir, env_dir / "Dockerfile"),
        ("task-server", f"{REGISTRY}/cybergym_arvo_1065:task-server",
         (env_dir / "task-server").resolve(), (env_dir / "task-server").resolve() / "Dockerfile")]
    # The map keeps the task's own name, which Harbor passes as environment_name.
    digests = {builds[0]: PUSHED, builds[1]: EXISTING}
    assert script.image_map(builds, digests) == {"Cybergym_Arvo_1065": {
        "main": f"{REGISTRY}/cybergym_arvo_1065:main@{PUSHED}",
        "task-server": f"{REGISTRY}/cybergym_arvo_1065:task-server@{EXISTING}"}}


def test_a_task_with_a_prebuilt_image_builds_only_its_services(tmp_path):
    write_task(tmp_path, "t", docker_image="reg/t:1")
    assert [b.service for b in script.plan_builds(script.discover_tasks(tmp_path), REGISTRY)] == \
        ["task-server"]


def test_a_build_key_the_script_cannot_honor_is_refused(tmp_path):
    write_task(tmp_path, "t", compose="services:\n  s:\n    build:\n      context: .\n"
                                      "      args: {A: b}\n")
    with pytest.raises(ValueError, match="'t'.*'s'.*'args'"):
        script.plan_builds(script.discover_tasks(tmp_path), REGISTRY)


def test_from_rewrites_change_only_the_image_and_keep_flags_and_aliases():
    rewrites = [script.parse_rewrite(ARVO_MIRROR)]
    out = script.rewrite_from_lines(SERVER_DOCKERFILE, rewrites)
    assert out.splitlines()[:2] == [
        "FROM --platform=linux/amd64 us-central1-docker.pkg.dev/proj/arvo/1065:fix AS fix-runner",
        "FROM --platform=linux/amd64 us-central1-docker.pkg.dev/proj/arvo/1065:vul"]
    assert out.splitlines()[2] == "COPY --from=fix-runner /out/ /out/"
    assert "# FROM commented-out" in script.rewrite_from_lines(MAIN_DOCKERFILE, rewrites)


def test_from_rewrites_apply_in_order_and_split_at_the_last_equals_sign():
    rewrites = [script.parse_rewrite("(?=a)a=b"), script.parse_rewrite("^b$=c")]
    assert script.rewrite_from_lines("FROM a\n", rewrites) == "FROM c\n"
    with pytest.raises(Exception, match="REGEX=REPLACEMENT"):
        script.parse_rewrite("no-separator")


def test_dry_run_prints_the_commands_and_leaves_the_tasks_untouched(tmp_path, capsys):
    tasks = tmp_path / "tasks"
    write_task(tasks, "t")
    before = {p: p.read_text() for p in tasks.rglob("*") if p.is_file()}
    status = script.main(["--tasks-dir", str(tasks), "--registry", REGISTRY, "--dry-run",
                          "--from-rewrite", ARVO_MIRROR, "--image-map", str(tmp_path / "map.json")])
    out = capsys.readouterr().out
    assert status == 0
    assert (f"docker buildx build --platform linux/amd64 --push --metadata-file '<metadata file>' "
            f"-t {REGISTRY}/t:task-server -f '<rewritten Dockerfile>'") in out
    assert f"# t:task-server is recorded as {REGISTRY}/t:task-server@sha256:<digest of the push>" in out
    assert ("# t:main FROM --platform=linux/amd64 n132/arvo:1065-vul  ->  "
            "FROM --platform=linux/amd64 us-central1-docker.pkg.dev/proj/arvo/1065:vul") in out
    assert not (tmp_path / "map.json").exists()
    assert {p: p.read_text() for p in tasks.rglob("*") if p.is_file()} == before


def fake_docker(calls, existing=("main",)):
    """A ``docker`` that reports ``existing`` tags as pushed and writes the build
    metadata file as buildx does."""
    def fake_run(command, **_):
        calls.append(command)
        if command[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            assert command[4:6] == ["--format", "{{json .Manifest}}"]
            if command[-1].rsplit(":", 1)[1] not in existing:
                return type("R", (), {"returncode": 1, "stdout": "", "stderr": "not found"})()
            manifest = {"mediaType": "application/vnd.oci.image.index.v1+json",
                        "digest": EXISTING, "size": 856}
            return type("R", (), {"returncode": 0, "stdout": json.dumps(manifest), "stderr": ""})()
        dockerfile = Path(command[command.index("-f") + 1])
        assert "us-central1-docker.pkg.dev/proj/arvo/1065" in dockerfile.read_text()
        Path(command[command.index("--metadata-file") + 1]).write_text(
            json.dumps({"containerimage.digest": PUSHED, "image.name": command[command.index("-t") + 1]}))
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    return fake_run


def test_a_build_pushes_the_rewritten_dockerfile_and_writes_a_digest_pinned_map(
        tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    write_task(tasks, "t")
    calls = []
    monkeypatch.setattr(script.subprocess, "run", fake_docker(calls))
    map_path = tmp_path / "map.json"
    status = script.main(["--tasks-dir", str(tasks), "--registry", REGISTRY, "--skip-existing",
                          "--from-rewrite", ARVO_MIRROR, "--image-map", str(map_path), "--jobs", "1"])
    assert status == 0
    built = [c[c.index("-t") + 1] for c in calls if c[:3] == ["docker", "buildx", "build"]]
    assert built == [f"{REGISTRY}/t:task-server"]          # main already existed
    # A skipped image keeps the digest the registry has, a fresh push the digest it returned.
    assert json.loads(map_path.read_text()) == {"t": {
        "main": f"{REGISTRY}/t:main@{EXISTING}",
        "task-server": f"{REGISTRY}/t:task-server@{PUSHED}"}}
    assert (tasks / "t" / "environment" / "Dockerfile").read_text() == MAIN_DOCKERFILE


def test_without_skip_existing_every_image_is_built_and_pinned_to_its_push(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    write_task(tasks, "t")
    calls = []
    monkeypatch.setattr(script.subprocess, "run", fake_docker(calls))
    map_path = tmp_path / "map.json"
    assert script.main(["--tasks-dir", str(tasks), "--registry", REGISTRY,
                        "--from-rewrite", ARVO_MIRROR, "--image-map", str(map_path)]) == 0
    assert not [c for c in calls if c[:4] == ["docker", "buildx", "imagetools", "inspect"]]
    assert set(json.loads(map_path.read_text())["t"].values()) == {
        f"{REGISTRY}/t:main@{PUSHED}", f"{REGISTRY}/t:task-server@{PUSHED}"}


@pytest.mark.parametrize("metadata", ["{}", '{"containerimage.digest": "latest"}', "not json"])
def test_a_push_without_a_usable_digest_fails_and_writes_no_map(tmp_path, monkeypatch, capsys,
                                                                metadata):
    tasks = tmp_path / "tasks"
    write_task(tasks, "t", compose=None)

    def fake_run(command, **_):
        Path(command[command.index("--metadata-file") + 1]).write_text(metadata)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(script.subprocess, "run", fake_run)
    map_path = tmp_path / "map.json"
    assert script.main(["--tasks-dir", str(tasks), "--registry", REGISTRY,
                        "--from-rewrite", ARVO_MIRROR, "--image-map", str(map_path)]) == 1
    assert "t:main] FAILED to read the digest" in capsys.readouterr().err
    assert not map_path.exists()


def test_a_failed_build_exits_nonzero_and_writes_no_map(tmp_path, monkeypatch):
    tasks = tmp_path / "tasks"
    write_task(tasks, "t", compose=None)
    monkeypatch.setattr(script.subprocess, "run", lambda c, **_: type(
        "R", (), {"returncode": 1, "stdout": "", "stderr": "boom"})())
    map_path = tmp_path / "map.json"
    assert script.main(["--tasks-dir", str(tasks), "--registry", REGISTRY,
                        "--image-map", str(map_path)]) == 1
    assert not map_path.exists()
