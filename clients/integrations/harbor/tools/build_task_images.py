#!/usr/bin/env python3
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

"""Build and push the images of Harbor tasks, and write the image map.

The Harbor environment for Agent Sandbox does not build images. A task that
only ships a Dockerfile, or a compose service that only has ``build:``, needs
a prebuilt image listed in the file passed as ``--ek image_map=<file>``. This
script builds those images with ``docker buildx``, pushes them as
``<registry>/<task>:<service>`` and writes the map with the digest of each
push::

    {"<task>": {"main": "<registry>/<task>:main@sha256:...",
                "<service>": "<registry>/<task>:<service>@sha256:..."}}

The digest makes a node pull the image that was pushed. A bare tag would let a
node that cached an earlier push of the same tag keep running the old image,
because the pod sets no ``imagePullPolicy``.

``main`` is the task's ``environment/`` directory and every other key is a
compose service with ``build:``. ``<task>`` is the short name in
``[task].name`` of ``task.toml``, which Harbor passes to the environment as
``environment_name``.

``--from-rewrite 'REGEX=REPLACEMENT'`` edits the image of every ``FROM`` line
before the build, for example to pull base images from a private mirror. The
script writes the edited Dockerfile to a temporary directory and leaves the
tasks directory unchanged. A rerun with ``--skip-existing`` skips the tags
that the registry already has and records the digest the tag points to.

The script needs Python 3.11, PyYAML (a dependency of Harbor and of this
package) and the ``docker`` CLI with buildx.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAIN = "main"
_FROM = re.compile(r"^(?P<head>\s*FROM(?:\s+--\S+)*\s+)(?P<image>\S+)(?P<tail>.*)$",
                   re.IGNORECASE)
_BUILD_KEYS = frozenset({"context", "dockerfile"})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class TaskSource:
    """What the planner needs to know about one task directory."""
    name: str
    environment_dir: Path
    compose: dict[str, Any] | None
    has_docker_image: bool


@dataclass(frozen=True)
class ImageBuild:
    task: str
    service: str
    context: Path
    dockerfile: Path
    tag: str


def plan_builds(tasks: list[TaskSource], registry: str) -> list[ImageBuild]:
    """The images to build: ``main`` for a task with a Dockerfile, and one for
    each compose service with ``build:``."""
    builds: list[ImageBuild] = []
    for task in tasks:
        def tag(service: str) -> str:
            return f"{registry.rstrip('/')}/{task.name.lower()}:{service}"

        dockerfile = task.environment_dir / "Dockerfile"
        if dockerfile.is_file() and not task.has_docker_image:
            builds.append(ImageBuild(task.name, MAIN, task.environment_dir, dockerfile, tag(MAIN)))
        services = (task.compose or {}).get("services") or {}
        for service, body in services.items():
            if service == MAIN or "build" not in (body or {}):
                continue
            context, dockerfile = _build_paths(task, service, body["build"])
            builds.append(ImageBuild(task.name, service, context, dockerfile, tag(service)))
    return builds


def _build_paths(task: TaskSource, service: str, build: Any) -> tuple[Path, Path]:
    spec = {"context": build} if isinstance(build, str) else dict(build)
    unknown = sorted(set(spec) - _BUILD_KEYS)
    if unknown:
        raise ValueError(f"task {task.name!r} service {service!r}: unsupported build key "
                         f"{unknown[0]!r}; this script reads only context and dockerfile")
    context = (task.environment_dir / spec.get("context", ".")).resolve()
    return context, context / spec.get("dockerfile", "Dockerfile")


def image_map(builds: list[ImageBuild], digests: Mapping[ImageBuild, str]) -> dict[str, dict[str, str]]:
    """The map to write, with each image as ``<tag>@<digest>``."""
    mapping: dict[str, dict[str, str]] = {}
    for build in builds:
        mapping.setdefault(build.task, {})[build.service] = f"{build.tag}@{digests[build]}"
    return mapping


def digest_from_build_metadata(text: str) -> str:
    """The pushed digest in the file ``docker buildx build --metadata-file`` writes."""
    return _checked_digest(json.loads(text).get("containerimage.digest"))


def digest_from_inspect(text: str) -> str:
    """The digest in the output of ``imagetools inspect --format '{{json .Manifest}}'``."""
    return _checked_digest(json.loads(text).get("digest"))


def _checked_digest(digest: object) -> str:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError(f"expected a sha256 digest, got {digest!r}")
    return digest


def parse_rewrite(text: str) -> tuple[re.Pattern[str], str]:
    """``REGEX=REPLACEMENT``, split at the last ``=`` so a regex may contain one."""
    pattern, sep, replacement = text.rpartition("=")
    if not sep or not pattern:
        raise argparse.ArgumentTypeError(f"expected REGEX=REPLACEMENT, got {text!r}")
    try:
        return re.compile(pattern), replacement
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"invalid regex {pattern!r}: {exc}") from exc


def rewrite_from_lines(dockerfile: str, rewrites: list[tuple[re.Pattern[str], str]]) -> str:
    """``dockerfile`` with each rewrite applied, in order, to the image of every
    ``FROM`` line. Flags such as ``--platform`` and a trailing ``AS name`` stay."""
    def rewrite_line(line: str) -> str:
        match = _FROM.match(line)
        if not match:
            return line
        image = match["image"]
        for pattern, replacement in rewrites:
            image = pattern.sub(replacement, image)
        return f"{match['head']}{image}{match['tail']}"

    return "\n".join(rewrite_line(line) for line in dockerfile.split("\n"))


def build_command(build: ImageBuild, dockerfile: Path | str, platform: str,
                  metadata_file: Path | str) -> list[str]:
    return ["docker", "buildx", "build", "--platform", platform, "--push",
            "--metadata-file", str(metadata_file), "-t", build.tag,
            "-f", str(dockerfile), str(build.context)]


def discover_tasks(tasks_dir: Path) -> list[TaskSource]:
    import yaml

    tasks: list[TaskSource] = []
    for toml_path in sorted(tasks_dir.glob("**/task.toml")):
        task_dir = toml_path.parent
        environment_dir = task_dir / "environment"
        if not environment_dir.is_dir():
            continue
        config = tomllib.loads(toml_path.read_text())
        name = (config.get("task") or {}).get("name") or task_dir.name
        compose_file = environment_dir / "docker-compose.yaml"
        tasks.append(TaskSource(
            name=name.rsplit("/", 1)[-1], environment_dir=environment_dir,
            compose=yaml.safe_load(compose_file.read_text()) if compose_file.is_file() else None,
            has_docker_image=bool((config.get("environment") or {}).get("docker_image"))))
    return tasks


def _existing_digest(tag: str) -> str | None:
    """The digest ``tag`` points to in the registry, or None when the tag is absent."""
    result = subprocess.run(["docker", "buildx", "imagetools", "inspect",
                             "--format", "{{json .Manifest}}", tag],
                            capture_output=True, text=True, check=False)
    return digest_from_inspect(result.stdout) if result.returncode == 0 else None


def _run_build(build: ImageBuild, args: argparse.Namespace, scratch: Path,
               rewrites: list[tuple[re.Pattern[str], str]]) -> str | None:
    """The digest of the image to record for ``build``, or None when it failed."""
    label = f"{build.task}:{build.service}"
    dockerfile: Path = build.dockerfile
    original = dockerfile.read_text()
    rewritten = rewrite_from_lines(original, rewrites)
    if rewritten != original:
        dockerfile = scratch / f"{build.task}.{build.service}.Dockerfile"
        dockerfile.write_text(rewritten)
    try:
        existing = _existing_digest(build.tag) if args.skip_existing else None
        if existing:
            print(f"[{label}] skipped, {build.tag} exists at {existing}", flush=True)
            return existing
        print(f"[{label}] building {build.tag}", flush=True)
        metadata = scratch / f"{build.task}.{build.service}.metadata.json"
        result = subprocess.run(build_command(build, dockerfile, args.platform, metadata),
                                capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(f"[{label}] FAILED (exit {result.returncode})\n{result.stdout}{result.stderr}",
                  file=sys.stderr, flush=True)
            return None
        digest = digest_from_build_metadata(metadata.read_text())
    except (OSError, ValueError) as exc:
        print(f"[{label}] FAILED to read the digest: {exc}", file=sys.stderr, flush=True)
        return None
    print(f"[{label}] pushed {build.tag}@{digest}", flush=True)
    return digest


def _print_plan(builds: list[ImageBuild], args: argparse.Namespace,
                rewrites: list[tuple[re.Pattern[str], str]]) -> None:
    for build in builds:
        original = build.dockerfile.read_text()
        changed = [(old, new) for old, new in zip(original.split("\n"),
                                                   rewrite_from_lines(original, rewrites).split("\n"))
                   if old != new]
        for old, new in changed:
            print(f"# {build.task}:{build.service} {old.strip()}  ->  {new.strip()}")
        dockerfile: Path | str = "<rewritten Dockerfile>" if changed else build.dockerfile
        print(shlex.join(build_command(build, dockerfile, args.platform, "<metadata file>")))
        print(f"# {build.task}:{build.service} is recorded as {build.tag}@sha256:<digest of the push>")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tasks-dir", type=Path, required=True,
                        help="directory of Harbor task directories (each holds task.toml)")
    parser.add_argument("--registry", required=True,
                        help="registry prefix, for example us-central1-docker.pkg.dev/proj/repo")
    parser.add_argument("--from-rewrite", action="append", default=[], type=parse_rewrite,
                        metavar="REGEX=REPLACEMENT",
                        help="rewrite the image of every FROM line; repeatable, applied in order")
    parser.add_argument("--image-map", type=Path, default=Path("image-map.json"),
                        help="where to write the image map (default: ./image-map.json)")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--jobs", type=int, default=4, help="parallel builds (default: 4)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="skip a build whose tag already exists in the registry and use its digest")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the build commands and write nothing")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    tasks = discover_tasks(args.tasks_dir)
    if not tasks:
        parser.error(f"no task directories with task.toml and environment/ in {args.tasks_dir}")
    builds = plan_builds(tasks, args.registry)
    if args.dry_run:
        _print_plan(builds, args, args.from_rewrite)
        return 0

    with tempfile.TemporaryDirectory(prefix="build-task-images-") as scratch, \
            ThreadPoolExecutor(max_workers=args.jobs) as pool:
        digests = list(pool.map(lambda b: _run_build(b, args, Path(scratch), args.from_rewrite),
                                builds))
    failed = [f"{b.task}:{b.service}" for b, digest in zip(builds, digests) if digest is None]
    if failed:
        print(f"{len(failed)} of {len(builds)} builds failed, image map not written: "
              f"{', '.join(failed)}", file=sys.stderr)
        return 1
    mapping = image_map(builds, {b: d for b, d in zip(builds, digests) if d})
    args.image_map.write_text(json.dumps(mapping, indent=2) + "\n")
    print(f"wrote {args.image_map} ({len(builds)} images, {len(mapping)} tasks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
