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

"""The compose file the CyberGym adapter writes for each task."""

# Copied from adapters/cybergym/template/environment/docker-compose.yaml in
# harbor-framework/harbor, with the {auth_token} placeholder filled in.
CYBERGYM_COMPOSE = """\
# CyberGym task-server sidecar.
# Merged on top of Harbor's base docker-compose config.
# The task-server holds vul/fix binaries and ground truth PoC
# in an isolated container — the agent only interacts via HTTP.
services:
  main:
    cap_add:
      - NET_ADMIN
    environment:
      - ALLOWED_HOSTS=${ALLOWED_HOSTS:-}
      - ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL:-}
      - OPENAI_BASE_URL=${OPENAI_BASE_URL:-}
      - LLM_BASE_URL=${LLM_BASE_URL:-}
      - GOOGLE_BASE_URL=${GOOGLE_BASE_URL:-}
      - OPENROUTER_BASE_URL=${OPENROUTER_BASE_URL:-}
      - NVIDIA_BASE_URL=${NVIDIA_BASE_URL:-}
      - AZURE_BASE_URL=${AZURE_BASE_URL:-}
      - AZURE_OPENAI_ENDPOINT=${AZURE_OPENAI_ENDPOINT:-}
      - DOUBAO_BASE_URL=${DOUBAO_BASE_URL:-}
      - LITELLM_BASE_URL=${LITELLM_BASE_URL:-}
      - DYNAMO_BASE_URL=${DYNAMO_BASE_URL:-}
      - HF_ENDPOINT_URL=${HF_ENDPOINT_URL:-}
    depends_on:
      task-server:
        condition: service_healthy

  task-server:
    build:
      context: ./task-server
    platform: linux/amd64
    environment:
      - AUTH_TOKEN=f7748496b9e994156c685b4bf7c7afb8
    expose:
      - "9111"
    deploy:
      resources:
        limits:
          cpus: "2"
          memory: 4096M
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:9111/health')"]
      interval: 3s
      timeout: 5s
      retries: 20
      start_period: 5s
"""
