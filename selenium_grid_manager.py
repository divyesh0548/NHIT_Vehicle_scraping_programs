"""Docker Selenium Grid helpers (hub must be running; nodes can be auto-started)."""

import json
import math
import subprocess
import time
import urllib.request
from datetime import datetime

import pandas as pd

from config import (
    MAX_SELENIUM_GRID_NODES,
    SE_NODE_MAX_SESSIONS,
    SE_NODE_OVERRIDE_MAX_SESSIONS,
    SELENIUM_AUTO_MANAGE_NODES,
    SELENIUM_HUB_CONTAINER,
    SELENIUM_MAX_PARALLEL_SESSIONS,
    SELENIUM_NETWORK,
    SELENIUM_NODE_IMAGE,
    SELENIUM_NODE_SHM_SIZE,
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


def max_parallel_sessions():
    """Total Chrome sessions the configured Grid layout can run."""
    return max(1, int(SELENIUM_MAX_PARALLEL_SESSIONS))


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


def start_managed_nodes(node_count=None, hub_name=SELENIUM_HUB_CONTAINER, network=SELENIUM_NETWORK):
    """
    Create Chrome node containers; returns names started by this run.

    Each node is started with SE_NODE_MAX_SESSIONS so one node can host
    multiple concurrent Chrome browsers (Grid UI: Max. Concurrency).
    """
    if node_count is None:
        node_count = MAX_SELENIUM_GRID_NODES
    node_count = max(1, int(node_count))

    ensure_network_exists(network)
    ensure_hub_container_running(hub_name)
    ensure_hub_on_network(hub_name, network)

    override_value = "true" if SE_NODE_OVERRIDE_MAX_SESSIONS else "false"
    print(
        f"Starting {node_count} Chrome node(s) with "
        f"SE_NODE_MAX_SESSIONS={SE_NODE_MAX_SESSIONS} "
        f"(override={override_value}, shm={SELENIUM_NODE_SHM_SIZE}) "
        f"-> up to {node_count * SE_NODE_MAX_SESSIONS} parallel sessions",
        flush=True,
    )

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
                "--shm-size",
                SELENIUM_NODE_SHM_SIZE,
                "-e",
                f"SE_EVENT_BUS_HOST={hub_name}",
                "-e",
                "SE_EVENT_BUS_PUBLISH_PORT=4442",
                "-e",
                "SE_EVENT_BUS_SUBSCRIBE_PORT=4443",
                "-e",
                f"SE_NODE_MAX_SESSIONS={SE_NODE_MAX_SESSIONS}",
                "-e",
                f"SE_NODE_OVERRIDE_MAX_SESSIONS={override_value}",
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


def _grid_status(remote_url=SELENIUM_REMOTE_URL):
    status_url = build_grid_status_url(remote_url)
    with urllib.request.urlopen(status_url, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    value = payload.get("value", {})
    nodes = value.get("nodes") or []
    up_nodes = [
        node
        for node in nodes
        if str(node.get("availability", "")).upper() in {"UP", ""}
    ]
    total_sessions = 0
    for node in up_nodes:
        try:
            total_sessions += int(node.get("maxSessions") or 0)
        except (TypeError, ValueError):
            continue
    return {
        "status_url": status_url,
        "ready": bool(value.get("ready")),
        "nodes": nodes,
        "up_nodes": up_nodes,
        "registered_nodes": len(nodes),
        "up_node_count": len(up_nodes),
        "total_max_sessions": total_sessions,
    }


def assert_grid_ready(
    remote_url=SELENIUM_REMOTE_URL,
    min_nodes=1,
    min_sessions=1,
):
    """
    Require enough UP nodes/session slots.

    Do not require status.ready == True: that flag becomes false when slots are
    temporarily full, which is normal once scraping starts.
    """
    min_nodes = max(1, int(min_nodes or 1))
    min_sessions = max(1, int(min_sessions or 1))
    info = _grid_status(remote_url)

    if info["up_node_count"] < min_nodes:
        raise RuntimeError(
            f"Selenium Grid at {info['status_url']} has "
            f"{info['up_node_count']}/{min_nodes} UP node(s) "
            f"(registered={info['registered_nodes']}, "
            f"ready={info['ready']}, max_sessions={info['total_max_sessions']})"
        )
    if info["total_max_sessions"] < min_sessions:
        raise RuntimeError(
            f"Selenium Grid at {info['status_url']} has only "
            f"{info['total_max_sessions']}/{min_sessions} session slot(s) "
            f"across {info['up_node_count']} UP node(s)"
        )


def wait_for_grid_ready(
    remote_url=SELENIUM_REMOTE_URL,
    timeout_seconds=SELENIUM_NODE_STARTUP_TIMEOUT,
    min_nodes=None,
    min_sessions=None,
):
    """
    Wait until the expected node/session capacity is registered.

    Defaults to MAX_SELENIUM_GRID_NODES and SELENIUM_MAX_PARALLEL_SESSIONS so we
    do not start 25 workers against a single early node.
    """
    if min_nodes is None:
        min_nodes = MAX_SELENIUM_GRID_NODES
    if min_sessions is None:
        min_sessions = SELENIUM_MAX_PARALLEL_SESSIONS

    deadline = time.time() + timeout_seconds
    last_error = None
    print(
        f"Waiting for Grid capacity: >= {min_nodes} node(s), "
        f">= {min_sessions} session slot(s) (timeout {timeout_seconds}s)",
        flush=True,
    )
    while time.time() < deadline:
        try:
            assert_grid_ready(
                remote_url,
                min_nodes=min_nodes,
                min_sessions=min_sessions,
            )
            info = _grid_status(remote_url)
            print(
                f"Grid capacity ready: {info['up_node_count']} UP node(s), "
                f"{info['total_max_sessions']} session slot(s)",
                flush=True,
            )
            return
        except Exception as exc:
            last_error = exc
            time.sleep(3)
    raise RuntimeError(
        f"Selenium Grid did not reach required capacity within {timeout_seconds}s. "
        f"{last_error}. "
        f"Check: (1) SELENIUM_HUB_CONTAINER={SELENIUM_HUB_CONTAINER} matches `docker ps`, "
        f"(2) hub is on network {SELENIUM_NETWORK}, "
        f"(3) http://localhost:4444/status shows {min_nodes} UP nodes, "
        f"(4) `docker logs <node-name>` if nodes exit immediately."
    )
