"""Docker Selenium Grid helpers (hub must be running; nodes can be auto-started)."""

import json
import math
import subprocess
import time
import urllib.request
from datetime import datetime

import pandas as pd

from config import (
    SELENIUM_AUTO_MANAGE_NODES,
    SELENIUM_HUB_CONTAINER,
    SELENIUM_NETWORK,
    SELENIUM_NODE_IMAGE,
    SELENIUM_NODE_STARTUP_TIMEOUT,
    SELENIUM_REMOTE_URL,
)
from IHMCL_bot_selenium import build_grid_status_url


def split_dataframe(df, max_chunks):
    """Split a DataFrame into balanced chunks (preserves original index)."""
    if df is None or df.empty:
        return []
    chunk_count = max(1, min(int(max_chunks), len(df)))
    chunk_size = math.ceil(len(df) / chunk_count)
    return [df.iloc[i : i + chunk_size].copy() for i in range(0, len(df), chunk_size)]


def run_docker_command(args, check=True):
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"Docker command failed: {' '.join(args)}\n"
            f"stdout: {completed.stdout.strip()}\n"
            f"stderr: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def ensure_network_exists(network_name=SELENIUM_NETWORK):
    existing = run_docker_command(["docker", "network", "ls", "--format", "{{.Name}}"], check=True)
    networks = {line.strip() for line in existing.splitlines() if line.strip()}
    if network_name not in networks:
        print(f"Creating Docker network: {network_name}")
        run_docker_command(["docker", "network", "create", network_name], check=True)


def ensure_hub_container_running(hub_name=SELENIUM_HUB_CONTAINER):
    running = run_docker_command(["docker", "ps", "--format", "{{.Names}}"], check=True)
    running_names = {line.strip() for line in running.splitlines() if line.strip()}
    if hub_name not in running_names:
        hint = ""
        if running_names:
            hint = f" Running containers: {', '.join(sorted(running_names))}."
        raise RuntimeError(
            f"Selenium hub container '{hub_name}' is not running.{hint} "
            "Set SELENIUM_HUB_CONTAINER in .env to match `docker ps` name, "
            "then start the hub (e.g. docker compose up -d)."
        )


def ensure_hub_on_network(hub_name=SELENIUM_HUB_CONTAINER, network=SELENIUM_NETWORK):
    """
    Auto-started nodes join SELENIUM_NETWORK and reach the hub by container name.
    If the hub was started outside that network, nodes never register (registered_nodes=0).
    """
    try:
        raw = run_docker_command(
            ["docker", "inspect", hub_name, "--format", "{{json .NetworkSettings.Networks}}"],
            check=True,
        )
        networks = json.loads(raw) if raw else {}
        if network in networks:
            return
        print(
            f"Connecting hub '{hub_name}' to Docker network '{network}' "
            f"(required for auto-started Chrome nodes)",
            flush=True,
        )
        run_docker_command(["docker", "network", "connect", network, hub_name], check=True)
    except Exception as exc:
        raise RuntimeError(
            f"Could not attach hub '{hub_name}' to network '{network}': {exc}. "
            "Start hub and nodes on the same Docker network, or set SELENIUM_AUTO_MANAGE_NODES=false "
            "and register nodes yourself."
        ) from exc


def start_managed_nodes(node_count, hub_name=SELENIUM_HUB_CONTAINER, network=SELENIUM_NETWORK):
    """Create Chrome node containers; returns names started by this run."""
    ensure_network_exists(network)
    ensure_hub_container_running(hub_name)
    ensure_hub_on_network(hub_name, network)

    started_nodes = []
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    for index in range(1, node_count + 1):
        node_name = f"auto-chrome-node-{timestamp}-{index}"
        print(f"Starting Chrome node: {node_name}", flush=True)
        run_docker_command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                node_name,
                "--network",
                network,
                "-e",
                f"SE_EVENT_BUS_HOST={hub_name}",
                "-e",
                "SE_EVENT_BUS_PUBLISH_PORT=4442",
                "-e",
                "SE_EVENT_BUS_SUBSCRIBE_PORT=4443",
                SELENIUM_NODE_IMAGE,
            ],
            check=True,
        )
        started_nodes.append(node_name)
    return started_nodes


def stop_managed_nodes(node_names):
    for node_name in node_names:
        try:
            print(f"Removing Chrome node: {node_name}", flush=True)
            run_docker_command(["docker", "rm", "-f", node_name], check=False)
        except Exception as exc:
            print(f"Failed to remove node {node_name}: {exc}", flush=True)


def assert_grid_ready(remote_url=SELENIUM_REMOTE_URL):
    status_url = build_grid_status_url(remote_url)
    with urllib.request.urlopen(status_url, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))

    value = payload.get("value", {})
    ready = bool(value.get("ready"))
    nodes = value.get("nodes") or []
    if not ready:
        raise RuntimeError(
            f"Selenium Grid is not ready at {status_url}. registered_nodes={len(nodes)}"
        )


def wait_for_grid_ready(remote_url=SELENIUM_REMOTE_URL, timeout_seconds=SELENIUM_NODE_STARTUP_TIMEOUT):
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            assert_grid_ready(remote_url)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(3)
    raise RuntimeError(
        f"Selenium Grid did not become ready within {timeout_seconds}s. {last_error}. "
        f"Check: (1) SELENIUM_HUB_CONTAINER={SELENIUM_HUB_CONTAINER} matches `docker ps`, "
        f"(2) hub is on network {SELENIUM_NETWORK}, "
        f"(3) http://localhost:4444/status shows ready=true, "
        f"(4) `docker logs <node-name>` if nodes exit immediately."
    )
