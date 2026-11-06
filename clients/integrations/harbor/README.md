# Harbor environment for Agent Sandbox

`k8s-agent-sandbox-harbor` runs [Harbor](https://github.com/harbor-framework/harbor)
evaluation trials in Agent Sandbox `SandboxClaim`s instead of local Docker
containers. Harbor loads it by import path, so no change to Harbor is needed:

```bash
harbor run -d terminal-bench@2.0 -a terminus-2 -m <model> -k 5 -n 50 \
  -e k8s_agent_sandbox_harbor:AgentSandboxEnvironment --ek namespace=harbor
```

Each trial gets one claim against a warm pool that holds the task's image, and
every `exec` and file transfer goes through the Kubernetes pod exec API, so the
task image needs no agent or server: only `sh`, `bash`, `tar` and `sleep`, plus
`su` and `getent` when Harbor runs a command as another user, and coreutils
`timeout` for deadlines (the command still runs without it, then without a
sandbox-side deadline). Every Debian, Ubuntu and `python` base image has them.
Terminal-Bench 2.0's 89 tasks all ship a prebuilt image and run unchanged. A task
with a `docker-compose.yaml` runs its other services as sidecar containers of the
same pod (see [Compose tasks](#compose-tasks)).

## Quick install

```bash
pip install "git+https://github.com/kubernetes-sigs/agent-sandbox.git@<tag>#subdirectory=clients/integrations/harbor"
```

Requirements: Python >= 3.12 (Harbor's floor), a kubeconfig for a cluster with
Agent Sandbox and its extension controllers (`SandboxTemplate`, `SandboxWarmPool`,
`SandboxClaim`), and RBAC for the user running `harbor`: create, get, list, watch
and delete on those three kinds (the SDK watches the claim until it is Ready); get,
list and watch on `sandboxes` in `agents.x-k8s.io` (the SDK reads the Sandbox for
the pod name); get and list on pods; and get and create on `pods/exec` (the exec
websocket is a GET). The example in `examples/harbor-terminal-bench/deploy/rbac.yaml`
has the Role.

## How a trial runs

1. `start()` resolves the task image (`[environment].docker_image` or an
   `image_map` entry, optionally through a registry mirror), creates a
   `SandboxTemplate` and a `SandboxWarmPool` for that image, its resources and
   its compose sidecars (once per distinct pod spec per process; a 409 means
   another trial got there first), then creates a `SandboxClaim` with a shutdown
   time and waits for it to be Ready and to accept exec. The claim is Ready
   when the pod is, so a sidecar with a healthcheck holds the claim until it is
   healthy.
2. `exec()` runs `bash -c <command>` through the pod exec API with Harbor's cwd,
   env, `user` (`su`) and `timeout_sec` (coreutils `timeout`, exit 124), the
   same contract as Harbor's own Kubernetes provider.
3. `upload_dir()` and `download_dir()` stream tar archives through exec;
   `upload_file()` and `download_file()` use the same path with one member.
4. `stop(delete=True)` deletes the claim; `--no-delete` keeps it and logs the
   `kubectl exec` command to inspect the pod.

The `main` container keeps its image's `ENTRYPOINT` and runs `sleep infinity` as
its arguments, as Harbor's Docker and GKE providers do. Exec always targets the
`main` container.

The pod runs as root (Harbor installs agents and uses `su`), with
`automountServiceAccountToken: false`, the task's CPU, memory and storage as
requests, limits derived from them, and a NetworkPolicy that allows cluster DNS
and the public internet only. The controller's default policy blocks DNS, and
Harbor verifiers install packages with `apt` and `uv`, so the template carries its
own egress rule (`--ek network=managed-default` keeps the controller default).
A task environment without internet (`network_mode = "no-network"`, or the older
`allow_internet = false`) gets a policy that blocks all traffic instead, whatever
`network` says. `egress_service` adds one exception to either policy (see
[Reaching a Service in the cluster](#reaching-a-service-in-the-cluster)).

## Options (`--ek key=value`)

| Key | Default | Meaning |
| --- | --- | --- |
| `namespace` | `default` | Namespace for templates, pools and claims. |
| `kube_context` | current | kubeconfig context; ignored in-cluster. |
| `ready_timeout_s` | `300` | Wait for the claim to be Ready and the container to accept exec. |
| `runtime_class` | none | `runtimeClassName` for the sandbox pod, for example `gvisor`. |
| `node_selector` | none | `k=v,k2=v2`. |
| `tolerations` | none | JSON list of tolerations. |
| `image_pull_secret` | none | Pull secret name in `namespace`. |
| `image_mirror` | none | Registry prefix that replaces the image's registry (Docker Hub shorthand expands to `library/`). |
| `image_map` | none | JSON file `{environment_id or task name: image}` for tasks without `docker_image`. A value is the image of the `main` container, or an object `{"main": image, "<service>": image}` that also names the image of each compose service that has `build:`. |
| `warm_replicas` | `0` | Pods kept ready per task image; `0` cold-starts each claim. |
| `cpu_limit_multiplier` | `1.0` | CPU limit as a multiple of the request; `0` sets none. |
| `memory_limit_multiplier` | `1.0` | Memory limit as a multiple of the request; `0` sets none. |
| `network` | `dns-public` | `dns-public` or `managed-default`. |
| `claim_ttl_s` | `21600` | Shutdown time of each claim, a safety net when `harbor` dies; `0` sets none. |
| `max_output_bytes` | `0` | Truncate exec output beyond this size; `0` keeps everything. |
| `exec_threads` | `64` | Threads for the blocking exec websockets per process. |
| `template_prefix` | `hb` | Name prefix of templates, pools and claims. |
| `extra_labels` | none | `k=v,...` labels on every object created. |
| `run_as_root` | `true` | Run the pod as uid 0. |
| `egress_service` | none | `<namespace>/<service>:<port>` of an in-cluster Service that every trial pod may reach. Needs `network=dns-public`. |

Every option also reads a `K8S_SANDBOX_HARBOR_<KEY>` environment variable; an
`--ek` value wins. A misspelled key is an error that lists the valid ones.

Harbor's own flags apply as usual: `-n` concurrent trials, `-k` attempts,
`--no-delete`, `--override-memory-mb`, and the resource enforcement policies.

## Reaching a Service in the cluster

An agent often has to call a model proxy that runs in the cluster as a
Deployment behind a Service, while the trial otherwise has no network. Name the
Service and its port:

```bash
harbor run ... -e k8s_agent_sandbox_harbor:AgentSandboxEnvironment \
    --ek egress_service=llm-proxy/model-proxy:8080
```

Every trial pod can then open connections to the pods behind that Service, and
to nothing else beyond what the task's network mode already allows. A task with
`no-network` gets exactly this one rule. This is an operator exception to the
task's network mode, so the task's own setting does not turn it off. The port
is the Service's `port`, the one you would put in a URL.

At the start of each trial the environment reads the Service and renders two
things into the template.

- An egress rule for the Service's backend pods, matched by the Service's
  namespace and pod selector, on the Service's `targetPort`. A NetworkPolicy
  sees the backend pod after the Service has translated the address, so the
  rule cannot name the cluster IP. The `targetPort` must be a number: GKE
  Dataplane V2 drops egress to a named port in another namespace, so the
  environment refuses a Service whose `targetPort` is a port name.
- A `hostAliases` entry in the pod that maps `<service>.<namespace>.svc` and
  `<service>.<namespace>.svc.cluster.local` to the Service's cluster IP. The
  pod resolves the name from its hosts file, so cluster DNS stays closed to
  the task and no IP is configured by hand.

The environment refuses to start the trial, before it creates a claim, when the
Service does not exist, has no selector, is headless, or has no port equal to
the one you gave. The template name includes the cluster IP, selector, target
port and protocol, so a recreated Service gets a new template and the old one
is left alone.

Requirements:

- The user running `harbor` needs `get` on `services` in the Service's
  namespace, in addition to the permissions listed under
  [Quick install](#quick-install).
- The cluster must enforce NetworkPolicy (on GKE, Dataplane V2 or network
  policy enforcement turned on). Without enforcement the template's policy is
  ignored and every pod can reach everything.
- `network=managed-default` is rejected together with `egress_service`,
  because the controller's default policy cannot be extended.

## Cleanup

Trials delete their claims. After a killed run:

```bash
python -m k8s_agent_sandbox_harbor cleanup --namespace harbor --claims   # claims only, pools stay
python -m k8s_agent_sandbox_harbor cleanup --namespace harbor --all      # claims, pools, templates
```

Both select only objects labelled `harbor.io/managed-by=k8s-agent-sandbox-harbor`.

## Compose tasks

A task's `docker-compose.yaml` describes services that Docker would run in
separate containers on one network. This environment runs them as containers of
the sandbox pod, which share one network namespace, so a sidecar listens on
`localhost` and the pod's `hostAliases` resolve each service name to
`127.0.0.1`. The environment reads these keys and rejects the rest, naming the
file, the service and the key, before it creates a claim:

| Service | Keys |
| --- | --- |
| `main` | `environment`, `cap_add` (only `NET_ADMIN` and `NET_RAW`), `depends_on`, and `build` and `image`, which it ignores because the image comes from `docker_image` or `image_map`. |
| Every other service | `image`, `build`, `environment`, `expose`, `healthcheck`, `command`, `depends_on`, `platform` (`linux/amd64` or `linux/arm64`, the same on every service that sets it), and `deploy.resources.limits` with `cpus` and `memory`. |

Compose behavior that carries over:

- `${VAR}`, `${VAR:-default}` and `$$` read the environment of the `harbor`
  process, when the environment object is created. A `${` inside a default,
  such as `${A:-${B}}`, is rejected.
- `main` gets `environment` as container variables and `cap_add` as added
  capabilities, so a task can ask for `NET_ADMIN`. Other capabilities are
  rejected because `main` runs as root.
- `platform` becomes the pod's `kubernetes.io/arch` node selector, next to
  `node_selector`. A `node_selector` that sets another architecture makes the
  environment fail when it is created.
- A `healthcheck` becomes an exec readiness probe. `interval` and `timeout`
  round up to whole seconds. `retries`, `start_period` and `start_interval` are
  ignored: the probe runs until it passes or `ready_timeout_s` ends the wait.
- `command` replaces the image's `CMD` and keeps its `ENTRYPOINT`.
- `deploy.resources.limits` become the sidecar's limits, and Kubernetes sets its
  requests to the same values. The node needs room for `main` and every sidecar.

Compose behavior that does not:

- A `depends_on` order is not kept, because the containers start together. A
  `service_healthy` condition holds because the claim waits for every probe.
- Services share a network namespace, so two services cannot listen on the same
  port.
- A sidecar runs as uid 0 like `main` while `run_as_root` is on, even when its
  image sets a `USER`.
- Harbor's `--extra-docker-compose` and per-service exec, artifacts and collect
  hooks are not supported, so `capabilities.docker_compose` stays off.

A service with `build:` runs the image in `image_map`, as Compose runs the image
it builds, even when it also sets `image:`. A service with only `image:` uses
that image. `tools/build_task_images.py` in this directory of the repository
builds the images and writes the map. It is not part of the installed package.

```bash
python <agent-sandbox>/clients/integrations/harbor/tools/build_task_images.py --tasks-dir <tasks> \
  --registry us-central1-docker.pkg.dev/<project>/<repo> \
  --image-map image-map.json --jobs 4 --skip-existing
```

It builds `environment/` as `main` and every service with `build:` for
`linux/amd64`, pushes `<registry>/<task>:<service>`, and writes
`{"<task>": {"main": ..., "<service>": ...}}` with each image as
`<registry>/<task>:<service>@sha256:<digest>`. The digest makes a node pull the
image that was just pushed, not an earlier push of the same tag that the node
cached. The map uses the task's short name, which is the last part of
`[task].name` and Harbor's `environment_name`. It needs the `docker` CLI with
buildx and PyYAML. `--from-rewrite 'REGEX=REPLACEMENT'` edits
the image of every `FROM` line, repeatable and applied in order; the script
builds from an edited copy of each Dockerfile and leaves the tasks untouched.
`--skip-existing` skips tags the registry already has and records the digest the
tag points to, which also skips an image whose Dockerfile changed since it was
pushed. `--dry-run` prints the commands
and the `FROM` lines it would change.

## CyberGym

[CyberGym](https://github.com/harbor-framework/harbor/tree/main/adapters/cybergym)
gives the agent a vulnerable C or C++ project and asks for an input that crashes
it. Each task has a `main` image with the agent's tools and a `task-server`
service that holds the vulnerable and fixed binaries and scores each submission.
The agent's `ENTRYPOINT` applies an iptables allowlist that needs `NET_ADMIN`,
which is why `main` keeps its `ENTRYPOINT` and receives the added capability.

1. Generate the tasks with the Harbor adapter. It needs network access to
   HuggingFace for metadata.

   ```bash
   git clone --depth 1 https://github.com/harbor-framework/harbor
   cd harbor/adapters/cybergym
   python run_adapter.py --output-dir ../../datasets/cybergym --subset \
     --exclude-task-ids oss-fuzz:385167047
   ```

   The tasks land in `datasets/cybergym/level1/`.

2. Build and push the images. This example pulls the ARVO base images from a
   private mirror of `n132/arvo`:

   ```bash
   python <agent-sandbox>/clients/integrations/harbor/tools/build_task_images.py --tasks-dir datasets/cybergym/level1 \
     --registry us-central1-docker.pkg.dev/<project>/<repo>/cybergym \
     --from-rewrite '^n132/arvo:(\d+)-(vul|fix)$=us-central1-docker.pkg.dev/<project>/<repo>/arvo/\1:\2' \
     --image-map image-map.json --skip-existing
   ```

3. Run with the image map. Set `ALLOWED_HOSTS` and the `*_BASE_URL` variables
   that the compose file lists in the environment of `harbor`: they pick the
   hosts the agent's network allowlist keeps open.

   ```bash
   ALLOWED_HOSTS=llm-gateway.example.com harbor run -p datasets/cybergym/level1 \
     -a <agent> -m <model> -e k8s_agent_sandbox_harbor:AgentSandboxEnvironment \
     --ek namespace=harbor --ek image_map=image-map.json --ek ready_timeout_s=3600
   ```

Notes:

- Do not pass `--ek image_mirror`. It replaces the registry host of every image,
  so it would rewrite the Artifact Registry names in the map into paths under
  the mirror.
- Use a node pool with the default `runc` runtime, not gVisor. The allowlist
  script skips itself when `iptables` fails, and the agent would then run with
  open egress.
- Raise `ready_timeout_s` well above the default. The claim is Ready only after
  the node has pulled both multi-gigabyte images and the `task-server` passes
  its healthcheck.
- A pod requests the CPU and memory of `main` plus the `task-server` limits, 2
  CPUs and 4096 MiB each in the adapter's files.
- The adapter writes a new `AUTH_TOKEN` into `docker-compose.yaml`, `test.sh` and
  `solve.sh` each time it runs. The images do not contain the token, so one set
  of pushed images serves every generation of the tasks.
- The template name hashes the sidecar environment, so a different
  `ALLOWED_HOSTS` creates another template and pool.

## Limits

- Harbor's stream mode, GPU and TPU tasks and network allowlists are not
  supported. Compose tasks that use keys outside the table above are not
  supported either. `_validate_definition` rejects all of them before a claim is
  created.
- Images are not built here. A task that ships only a Dockerfile needs an
  `image_map` entry, which `tools/build_task_images.py` can produce.
- Harbor's pod labels are not forwarded to the pod: the controller admits pod
  labels from an allowlisted domain only. Claims, templates and pools carry the
  `harbor.io/*` labels.
- Pod Security Admission `restricted` rejects the root pod; use `baseline`, or a
  gVisor or Kata runtime class for isolation.

## Development

```bash
cd clients/integrations/harbor
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ../../python/agentic-sandbox-client[async] -e '.[test]'
pytest
```

The unit tests run the environment against Harbor's real `BaseEnvironment`
with fake Kubernetes clients and a local stand-in for the pod, so a Harbor
signature change fails here first. `make test-unit` at the repo root runs the
same suite (`integrations-harbor`).
