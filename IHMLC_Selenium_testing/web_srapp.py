from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import math
import os
import subprocess
import time

import pandas as pd

from IHMCL_bot import scrape_ihmcl_for_dataframe
from IHMCL_bot import build_grid_status_url


INPUT_EXCEL = "test_input.xlsx"
INPUT_COLUMN = "Veh Reg No."
OUTPUT_PREFIX = "ihmcl_aggregated_output"
MAX_NODES = int(os.getenv("MAX_NODES", "3"))
SELENIUM_REMOTE_URL = os.getenv("SELENIUM_REMOTE_URL", "http://localhost:4444/wd/hub")
MOBILE_NUMBER = os.getenv("IHMCL_MOBILE_NUMBER", "9999999999")
PLAZA_NAME = os.getenv("IHMCL_PLAZA_NAME", "Phulwaria Toll Plaza")
HEADLESS = os.getenv("IHMCL_HEADLESS", "false").strip().lower() in {"1", "true", "yes"}
AUTO_MANAGE_NODES = os.getenv("AUTO_MANAGE_NODES", "true").strip().lower() in {"1", "true", "yes"}
SELENIUM_NETWORK = os.getenv("SELENIUM_NETWORK", "selenium-grid")
SELENIUM_HUB_CONTAINER = os.getenv("SELENIUM_HUB_CONTAINER", "selenium-hub")
SELENIUM_NODE_IMAGE = os.getenv("SELENIUM_NODE_IMAGE", "selenium/node-chrome:latest")
NODE_STARTUP_TIMEOUT = int(os.getenv("NODE_STARTUP_TIMEOUT", "90"))


def load_vehicle_dataframe(input_excel=INPUT_EXCEL, vehicle_column=INPUT_COLUMN):
    """Read vehicle numbers from Excel and return a clean one-column DataFrame."""
    input_path = Path(input_excel)
    if not input_path.exists():
        raise FileNotFoundError(f"Input Excel file not found: {input_path}")

    df = pd.read_excel(input_path)
    if vehicle_column not in df.columns:
        raise KeyError(
            f"Column '{vehicle_column}' not found in {input_path}. Available columns: {list(df.columns)}"
        )

    vehicle_df = pd.DataFrame({vehicle_column: df[vehicle_column]})
    vehicle_df = vehicle_df.dropna(subset=[vehicle_column]).copy()
    vehicle_df[vehicle_column] = vehicle_df[vehicle_column].astype(str).str.strip()
    vehicle_df = vehicle_df[vehicle_df[vehicle_column] != ""].drop_duplicates().reset_index(drop=True)
    return vehicle_df


def split_dataframe(df, max_chunks):
    """Split a DataFrame into balanced chunks."""
    if df.empty:
        return []

    chunk_count = max(1, min(max_chunks, len(df)))
    chunk_size = math.ceil(len(df) / chunk_count)
    return [df.iloc[i:i + chunk_size].copy() for i in range(0, len(df), chunk_size)]


def run_docker_command(args, check=True):
    """Run a docker CLI command and return stdout."""
    completed = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"Docker command failed: {' '.join(args)}\n"
            f"stdout: {completed.stdout.strip()}\n"
            f"stderr: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def ensure_network_exists(network_name):
    """Create the selenium network if it does not already exist."""
    existing = run_docker_command(["docker", "network", "ls", "--format", "{{.Name}}"], check=True)
    networks = {line.strip() for line in existing.splitlines() if line.strip()}
    if network_name not in networks:
        print(f"Creating Docker network: {network_name}")
        run_docker_command(["docker", "network", "create", network_name], check=True)


def ensure_hub_container_running():
    """Fail with a clear error if the hub container is not running."""
    running = run_docker_command(["docker", "ps", "--format", "{{.Names}}"], check=True)
    running_names = {line.strip() for line in running.splitlines() if line.strip()}
    if SELENIUM_HUB_CONTAINER not in running_names:
        raise RuntimeError(
            f"Selenium hub container '{SELENIUM_HUB_CONTAINER}' is not running. "
            f"Start the hub first, then rerun the scraper."
        )


def start_managed_nodes(node_count):
    """Create Chrome node containers and return the names started by this run."""
    ensure_network_exists(SELENIUM_NETWORK)
    ensure_hub_container_running()

    started_nodes = []
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    for index in range(1, node_count + 1):
        node_name = f"auto-chrome-node-{timestamp}-{index}"
        print(f"Starting Chrome node: {node_name}")
        run_docker_command(
            [
                "docker", "run", "-d",
                "--name", node_name,
                "--network", SELENIUM_NETWORK,
                "-e", f"SE_EVENT_BUS_HOST={SELENIUM_HUB_CONTAINER}",
                "-e", "SE_EVENT_BUS_PUBLISH_PORT=4442",
                "-e", "SE_EVENT_BUS_SUBSCRIBE_PORT=4443",
                SELENIUM_NODE_IMAGE,
            ],
            check=True,
        )
        started_nodes.append(node_name)
    return started_nodes


def stop_managed_nodes(node_names):
    """Remove only the node containers started by this run."""
    for node_name in node_names:
        try:
            print(f"Removing Chrome node: {node_name}")
            run_docker_command(["docker", "rm", "-f", node_name], check=False)
        except Exception as exc:
            print(f"Failed to remove node {node_name}: {exc}")


def scrape_chunk(chunk_df, chunk_id, remote_url):
    """Scrape one chunk using one Selenium Grid session."""
    print(f"[Chunk {chunk_id}] Starting with {len(chunk_df)} vehicle numbers")
    result_df = scrape_ihmcl_for_dataframe(
        chunk_df,
        vehicle_column_names=[INPUT_COLUMN, "Veh Reg No"],
        mobile_number=MOBILE_NUMBER,
        plaza_name=PLAZA_NAME,
        remote_url=remote_url,
        headless=HEADLESS,
    )

    if result_df is None:
        result_df = pd.DataFrame()
    else:
        result_df = result_df.copy()

    result_df["source_chunk"] = chunk_id
    print(f"[Chunk {chunk_id}] Completed with {len(result_df)} scraped row(s)")
    return result_df


def aggregate_results(result_frames):
    """Merge all chunk results into one DataFrame."""
    non_empty_frames = [frame for frame in result_frames if frame is not None and not frame.empty]
    if not non_empty_frames:
        return pd.DataFrame()

    combined_df = pd.concat(non_empty_frames, ignore_index=True)
    combined_df = combined_df.drop_duplicates().reset_index(drop=True)
    return combined_df


def save_output(df, output_prefix=OUTPUT_PREFIX):
    """Write the aggregated result to an Excel file and return its path."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(f"{output_prefix}_{timestamp}.xlsx")
    df.to_excel(output_path, index=False)
    return output_path


def assert_grid_ready(remote_url=SELENIUM_REMOTE_URL):
    """Stop before dispatch if Selenium Grid has no ready nodes."""
    import json
    import urllib.request

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


def wait_for_grid_ready(remote_url=SELENIUM_REMOTE_URL, timeout_seconds=NODE_STARTUP_TIMEOUT):
    """Wait for nodes to register after starting them."""
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            assert_grid_ready(remote_url)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(3)
    raise RuntimeError(f"Selenium Grid did not become ready within {timeout_seconds}s. {last_error}")


def main():
    vehicle_df = load_vehicle_dataframe()
    print(f"Loaded {len(vehicle_df)} unique vehicle numbers from {INPUT_EXCEL}")

    chunks = split_dataframe(vehicle_df, MAX_NODES)
    if not chunks:
        raise ValueError("No vehicle numbers found to process.")

    managed_nodes = []
    try:
        if AUTO_MANAGE_NODES:
            managed_nodes = start_managed_nodes(len(chunks))
            wait_for_grid_ready(SELENIUM_REMOTE_URL)
        else:
            assert_grid_ready(SELENIUM_REMOTE_URL)

        print(f"Dispatching {len(chunks)} chunk(s) to Selenium Grid at {SELENIUM_REMOTE_URL}")

        results = []
        failures = []

        with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
            future_to_chunk = {
                executor.submit(scrape_chunk, chunk, chunk_id, SELENIUM_REMOTE_URL): chunk_id
                for chunk_id, chunk in enumerate(chunks, start=1)
            }

            for future in as_completed(future_to_chunk):
                chunk_id = future_to_chunk[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    failures.append((chunk_id, str(exc)))
                    print(f"[Chunk {chunk_id}] Failed: {exc}")

        aggregated_df = aggregate_results(results)
        output_path = save_output(aggregated_df)

        print(f"Aggregated {len(aggregated_df)} row(s) into {output_path}")
        if failures:
            print("Some chunks failed:")
            for chunk_id, error in failures:
                print(f"  - Chunk {chunk_id}: {error}")
    finally:
        if managed_nodes:
            stop_managed_nodes(managed_nodes)


if __name__ == "__main__":
    main()
