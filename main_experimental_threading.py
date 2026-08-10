import os
import time
import pandas as pd
import psycopg2
import boto3
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from datetime import datetime
from botocore.exceptions import NoCredentialsError, ClientError
from web_scrap_for_lookup_chhattisgarh import scrape_vehicle_weights_chhattisgarh, check_vehicle_in_database
from web_scrap_for_permit import scrape_vehicle_details_for_permit
from web_scrap_re_run import scrape_vehicle_weights_rerun
from IHMCL_bot_selenium import scrape_ihmcl_for_dataframe
from vehicle_number_utils import is_vehicle_number_eligible, normalize_vehicle_number
from config import (
    AWS_ACCESS_KEY_ID,
    AWS_REGION,
    AWS_S3_BUCKET_NAME,
    AWS_SECRET_ACCESS_KEY,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    MAX_SELENIUM_GRID_NODES,
    SELENIUM_AUTO_MANAGE_NODES,
    SELENIUM_PROCESSING,
    SELENIUM_REMOTE_URL,
)
from selenium_grid_manager import (
    assert_grid_ready,
    split_dataframe as split_df_for_grid,
    start_managed_nodes,
    stop_managed_nodes,
    wait_for_grid_ready,
)

# Initialize S3 client
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION
)

# List of possible vehicle number column names (case-insensitive matching)
VEHICLE_COLUMN_NAMES = [
    'vehicle_no',
    'vehicle number',
    'veh reg no',
    'veh reg no.',
    'vehicle reg no',
    'vehicle reg no.',
    'veh_no',
    'veh number',
    'vehicle registration number',
    'registration number',
    'reg no',
    'reg no.',
    'unique vehicle number',
    'unique veh no',
    'veh_reg_no',
    'vehicle_reg_no',
    'Veh Reg No.',
    'Veh Reg No',
    'VEH REG NO',
    'Vehicle Number',
    'Vehicle No',
    'Veh No.',
    'Licence Plate No.'
]

# How many leading rows to scan when the header is not on row 1
HEADER_SCAN_ROWS = 10

# Parallel DB prefetch (checkpostmaster; default flow also uses capacity_vehicle_numbers).
# Each worker uses its own DB connection; web scraping stays single-threaded in scrape modules.
DB_PREFETCH_WORKERS = 8

# Web scraping: True = parallel sessions on Docker Selenium Grid; False = one local Chrome.
# Default comes from .env (SELENIUM_PROCESSING); set True here to override without editing .env.
selenium_processing = SELENIUM_PROCESSING


def _cell_as_header_text(value):
    """Normalize a cell value for header-keyword comparison."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    # Collapse whitespace / newlines often seen in Excel header cells
    return " ".join(text.split())


def _cell_matches_vehicle_header(cell_text):
    """
    True if a cell looks like a vehicle-registration header keyword.

    Checks VEHICLE_COLUMN_NAMES (exact then partial), then veh+reg / vehicle+no.
    """
    if not cell_text:
        return False
    lowered = cell_text.lower().strip().rstrip(".")

    for name in VEHICLE_COLUMN_NAMES:
        expected = name.lower().strip().rstrip(".")
        if lowered == expected:
            return True

    for name in VEHICLE_COLUMN_NAMES:
        expected = name.lower().strip().rstrip(".")
        if expected in lowered or lowered in expected:
            return True

    if "veh" in lowered and "reg" in lowered:
        return True
    if "vehicle" in lowered and ("no" in lowered or "number" in lowered):
        return True
    if "licence plate" in lowered or "license plate" in lowered:
        return True
    return False


def _find_header_row_index(raw_df, max_rows=HEADER_SCAN_ROWS):
    """
    Scan the first max_rows of a header-less DataFrame for vehicle header keywords.

    Returns 0-based row index of the header, or None if not found.
    """
    if raw_df is None or raw_df.empty:
        return None

    scan_limit = min(int(max_rows), len(raw_df))
    for row_idx in range(scan_limit):
        row = raw_df.iloc[row_idx]
        for value in row.tolist():
            if _cell_matches_vehicle_header(_cell_as_header_text(value)):
                return row_idx
    return None


def _dataframe_from_header_row(raw_df, header_row_idx):
    """Promote header_row_idx to column names; keep only rows below it."""
    header_values = [
        _cell_as_header_text(v) or f"Unnamed_{i}"
        for i, v in enumerate(raw_df.iloc[header_row_idx].tolist())
    ]

    # Ensure unique column names
    seen = {}
    columns = []
    for name in header_values:
        key = name
        if key in seen:
            seen[key] += 1
            key = f"{name}_{seen[name]}"
        else:
            seen[key] = 0
        columns.append(key)

    data_df = raw_df.iloc[header_row_idx + 1 :].copy()
    data_df.columns = columns
    data_df = data_df.reset_index(drop=True)

    # Drop fully empty rows
    data_df = data_df.dropna(how="all")
    return data_df


def _find_vehicle_column_in_df(df):
    """Find vehicle number column from VEHICLE_COLUMN_NAMES / fallbacks."""
    veh_col = None
    df_columns_lower = {str(col).lower(): col for col in df.columns}

    for col_name in VEHICLE_COLUMN_NAMES:
        if col_name.lower() in df_columns_lower:
            veh_col = df_columns_lower[col_name.lower()]
            print(f"  [OK] Found vehicle column (exact match): '{veh_col}'")
            return veh_col

    for col_name in VEHICLE_COLUMN_NAMES:
        for df_col in df.columns:
            df_col_l = str(df_col).lower()
            name_l = col_name.lower()
            if name_l in df_col_l or df_col_l in name_l:
                print(f"  [OK] Found vehicle column (partial match): '{df_col}'")
                return df_col

    for col in df.columns:
        col_l = str(col).lower()
        if "veh" in col_l and "reg" in col_l:
            print(f"  [OK] Found vehicle column (veh+reg): '{col}'")
            return col

    for col in df.columns:
        col_l = str(col).lower()
        if "vehicle" in col_l and ("no" in col_l or "number" in col_l):
            print(f"  [OK] Found vehicle column (fallback search): '{col}'")
            return col

    return None


def load_input_dataframe(file_buffer, is_csv=False, header_scan_rows=HEADER_SCAN_ROWS):
    """
    Load CSV/Excel and detect header within the first header_scan_rows rows.

    Many uploads put titles/metadata above the real header. This reads without
    assuming row 0 is the header, finds a vehicle-keyword row, then uses rows below.
    """
    file_buffer.seek(0)
    if is_csv:
        raw_df = pd.read_csv(file_buffer, header=None, dtype=str)
    else:
        raw_df = pd.read_excel(file_buffer, engine="openpyxl", header=None, dtype=str)

    print(f"  [INFO] Raw file loaded: {len(raw_df)} rows (scanning first {header_scan_rows} for header)")

    header_row_idx = _find_header_row_index(raw_df, max_rows=header_scan_rows)
    if header_row_idx is None:
        print(
            f"  [ERROR] No vehicle header keyword found in the first {header_scan_rows} rows"
        )
        preview = raw_df.head(header_scan_rows)
        print(f"  [INFO] First rows preview:\n{preview}")
        return None

    if header_row_idx == 0:
        print("  [OK] Header found on row 1")
    else:
        print(
            f"  [OK] Header found on row {header_row_idx + 1} "
            f"(skipped {header_row_idx} leading row(s))"
        )

    df = _dataframe_from_header_row(raw_df, header_row_idx)
    print(f"  [OK] Data rows below header: {len(df)}")
    print(f"  [INFO] Columns: {list(df.columns)}")
    return df


def _find_veh_reg_column(df):
    """Match scrape_vehicle_weights column rule: column name contains both 'veh' and 'reg'."""
    for col in df.columns:
        if "veh" in str(col).lower() and "reg" in str(col).lower():
            return col
    return None


def _split_into_n_chunks(items, n_chunks):
    """Split items into up to n_chunks contiguous slices (as equal in size as possible)."""
    if not items:
        return []
    n_chunks = max(1, min(int(n_chunks), len(items)))
    n = len(items)
    base, extra = divmod(n, n_chunks)
    chunks = []
    start = 0
    for i in range(n_chunks):
        sz = base + (1 if i < extra else 0)
        if sz == 0:
            break
        chunks.append(items[start : start + sz])
        start += sz
    return chunks


def _worker_prefetch_checkpostmaster(rows):
    """rows: list of (df_index, vehicle_no). Returns dict idx -> weight str."""
    out = {}
    conn = get_db_connection()
    if not conn:
        print("  [DB PREFETCH] Worker: could not connect (checkpostmaster)", flush=True)
        return out
    try:
        cur = conn.cursor()
        for idx, vehicle_no in rows:
            w = check_vehicle_in_database(cur, vehicle_no, "checkpostmaster")
            if w is not None:
                out[idx] = str(w)
    finally:
        conn.close()
    return out


def _worker_prefetch_capacity(rows):
    """rows: list of (df_index, vehicle_no). Returns dict idx -> '-' when present in capacity table."""
    out = {}
    conn = get_db_connection()
    if not conn:
        print("  [DB PREFETCH] Worker: could not connect (capacity_vehicle_numbers)", flush=True)
        return out
    try:
        cur = conn.cursor()
        for idx, vehicle_no in rows:
            exists = check_vehicle_in_database(cur, vehicle_no, "capacity_vehicle_numbers")
            if exists is not None:
                out[idx] = "-"
    finally:
        conn.close()
    return out


def _run_checkpost_chunk(chunk_info):
    """(chunk_num, total_chunks, rows) -> (chunk_num, total_chunks, len(rows), len(out), out)"""
    chunk_num, total_chunks, rows = chunk_info
    out = _worker_prefetch_checkpostmaster(rows)
    return chunk_num, total_chunks, len(rows), len(out), out


def _run_capacity_chunk(chunk_info):
    """Same shape as checkpost chunk runner."""
    chunk_num, total_chunks, rows = chunk_info
    out = _worker_prefetch_capacity(rows)
    return chunk_num, total_chunks, len(rows), len(out), out


def parallel_prefetch_vehicle_db_weights(vehicle_df, num_workers=None):
    """
    Resolve checkpostmaster then capacity_vehicle_numbers using threaded workers (same semantics
    as scrape_vehicle_weights steps 1–2). Rows still empty after this need web scraping.
    """
    if num_workers is None:
        num_workers = DB_PREFETCH_WORKERS

    df = vehicle_df.copy()
    veh_col = _find_veh_reg_column(df)
    if veh_col is None:
        print("  [DB PREFETCH] Skipped: no veh+reg column found")
        return df

    if "Weight" not in df.columns:
        df["Weight"] = ""

    tasks = []
    for idx, row in df.iterrows():
        vn = normalize_vehicle_number(row[veh_col])
        if not vn or not is_vehicle_number_eligible(vn):
            continue
        tasks.append((idx, vn))

    n = len(tasks)
    if n == 0:
        return df

    workers = max(1, min(num_workers, n))
    chunks = _split_into_n_chunks(tasks, workers)
    sizes = [len(c) for c in chunks]
    print(
        f"  [DB PREFETCH] checkpostmaster: {n} vehicles, {len(chunks)} chunk(s), "
        f"{workers} worker(s), chunk size min={min(sizes)} max={max(sizes)} — starting...",
        flush=True,
    )
    t0 = time.perf_counter()
    total_chunks = len(chunks)
    chunk_jobs = [(i + 1, total_chunks, ch) for i, ch in enumerate(chunks)]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_run_checkpost_chunk, job) for job in chunk_jobs]
        for fut in as_completed(futures):
            chunk_num, tc, queried, hits, out = fut.result()
            for idx, w in out.items():
                df.at[idx, "Weight"] = w
            elapsed = time.perf_counter() - t0
            print(
                f"  [DB PREFETCH] checkpostmaster chunk {chunk_num}/{tc} done: "
                f"queried={queried}, hits={hits}, elapsed={elapsed:.1f}s",
                flush=True,
            )
    print(f"  [DB PREFETCH] checkpostmaster phase complete in {time.perf_counter() - t0:.1f}s", flush=True)

    remaining_tasks = []
    for idx, vn in tasks:
        cw = df.at[idx, "Weight"]
        if pd.isna(cw):
            remaining_tasks.append((idx, vn))
            continue
        s = str(cw).strip()
        if not s or s.lower() == "nan":
            remaining_tasks.append((idx, vn))

    if not remaining_tasks:
        print("  [DB PREFETCH] No rows left for capacity_vehicle_numbers")
        return df

    workers = max(1, min(num_workers, len(remaining_tasks)))
    chunks = _split_into_n_chunks(remaining_tasks, workers)
    sizes = [len(c) for c in chunks]
    print(
        f"  [DB PREFETCH] capacity_vehicle_numbers: {len(remaining_tasks)} vehicles, {len(chunks)} chunk(s), "
        f"{workers} worker(s), chunk size min={min(sizes)} max={max(sizes)} — starting...",
        flush=True,
    )
    t1 = time.perf_counter()
    total_chunks = len(chunks)
    chunk_jobs = [(i + 1, total_chunks, ch) for i, ch in enumerate(chunks)]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_run_capacity_chunk, job) for job in chunk_jobs]
        for fut in as_completed(futures):
            chunk_num, tc, queried, hits, out = fut.result()
            for idx, w in out.items():
                df.at[idx, "Weight"] = w
            elapsed = time.perf_counter() - t1
            print(
                f"  [DB PREFETCH] capacity chunk {chunk_num}/{tc} done: "
                f"queried={queried}, hits={hits}, elapsed={elapsed:.1f}s",
                flush=True,
            )
    print(
        f"  [DB PREFETCH] capacity_vehicle_numbers phase complete in {time.perf_counter() - t1:.1f}s",
        flush=True,
    )

    return df


def _run_selenium_grid_scrape(vehicle_df, scrape_fn, scrape_label="WEB SCRAPE", merge_mode="index"):
    """
    Try parallel Selenium Grid scrape via scrape_fn(chunk_df, remote_url).
    remote_url=None means local Chrome inside the scraper.
    On grid failure, falls back to local Chrome on full/partial results.

    merge_mode:
      - "index": merge chunk columns back onto vehicle_df by row index (weight/rerun flows)
      - "concat": concatenate chunk result frames (IHMCL scraped output rows)
    """
    if not selenium_processing:
        print(f"  [{scrape_label}] Running local Chrome (single browser)...", flush=True)
        return scrape_fn(vehicle_df, None)

    chunks = split_df_for_grid(vehicle_df, MAX_SELENIUM_GRID_NODES)
    if not chunks:
        return vehicle_df

    print(
        f"  [SELENIUM GRID] {scrape_label}: {len(chunks)} chunk(s) -> {SELENIUM_REMOTE_URL} "
        f"(auto_nodes={SELENIUM_AUTO_MANAGE_NODES})",
        flush=True,
    )

    managed_nodes = []
    merged = None
    grid_error = None

    try:
        if SELENIUM_AUTO_MANAGE_NODES:
            managed_nodes = start_managed_nodes(len(chunks))
            wait_for_grid_ready(SELENIUM_REMOTE_URL)
        else:
            assert_grid_ready(SELENIUM_REMOTE_URL)

        result_frames = []
        failures = []
        with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
            future_to_chunk = {
                executor.submit(scrape_fn, chunk, SELENIUM_REMOTE_URL): chunk_id
                for chunk_id, chunk in enumerate(chunks, start=1)
            }
            for future in as_completed(future_to_chunk):
                chunk_id = future_to_chunk[future]
                try:
                    result_frames.append(future.result())
                    print(
                        f"  [SELENIUM GRID] {scrape_label} chunk {chunk_id}/{len(chunks)} completed",
                        flush=True,
                    )
                except Exception as exc:
                    failures.append((chunk_id, exc))
                    print(
                        f"  [SELENIUM GRID] {scrape_label} chunk {chunk_id} failed: {exc}",
                        flush=True,
                    )

        if result_frames:
            if merge_mode == "concat":
                non_empty = [
                    frame for frame in result_frames if frame is not None and not frame.empty
                ]
                if non_empty:
                    merged = pd.concat(non_empty, ignore_index=True)
                    merged = merged.drop_duplicates().reset_index(drop=True)
                else:
                    merged = pd.DataFrame()
            else:
                merged = vehicle_df.copy()
                for frame in result_frames:
                    for col in frame.columns:
                        merged.loc[frame.index, col] = frame[col]

        if not result_frames:
            grid_error = f"All Selenium Grid chunks failed: {failures}"
        elif failures:
            grid_error = f"{len(failures)} Selenium Grid chunk(s) failed: {failures}"

    except Exception as exc:
        grid_error = str(exc)
        print(f"  [SELENIUM GRID] {scrape_label} error: {exc}", flush=True)
    finally:
        if managed_nodes:
            stop_managed_nodes(managed_nodes)

    if grid_error:
        print(
            f"  [SELENIUM GRID] {scrape_label}: falling back to local Chrome...",
            flush=True,
        )
        if merge_mode == "concat":
            df_for_local = vehicle_df
        else:
            df_for_local = merged if merged is not None else vehicle_df
        return scrape_fn(df_for_local, None)

    return merged


def run_ihmcl_web_scrape(vehicle_df):
    """IHMCL FASTag portal scrape (grid with local fallback)."""
    mobile_number = os.getenv("IHMCL_MOBILE_NUMBER", "9999999999")
    plaza_name = os.getenv("IHMCL_PLAZA_NAME", "Phulwaria Toll Plaza")
    headless = os.getenv("IHMCL_HEADLESS", "false").strip().lower() in {"1", "true", "yes"}

    def scrape_chunk(chunk, remote_url):
        return scrape_ihmcl_for_dataframe(
            chunk,
            mobile_number=mobile_number,
            plaza_name=plaza_name,
            remote_url=remote_url,
            headless=headless,
        )

    return _run_selenium_grid_scrape(
        vehicle_df, scrape_chunk, scrape_label="IHMCL", merge_mode="concat"
    )


def run_vehicle_weight_web_scrape(vehicle_df, skip_db_phases=True):
    """Chhattisgarh web scrape after DB prefetch (grid with local fallback)."""

    def scrape_chunk(chunk, remote_url):
        return scrape_vehicle_weights_chhattisgarh(
            chunk, skip_db_phases=skip_db_phases, remote_url=remote_url
        )

    return _run_selenium_grid_scrape(vehicle_df, scrape_chunk, scrape_label="WEIGHT")


def run_rerun_web_scrape(vehicle_df):
    """Rerun web-only scrape (grid with local fallback)."""

    def scrape_chunk(chunk, remote_url):
        return scrape_vehicle_weights_rerun(chunk, remote_url=remote_url)

    return _run_selenium_grid_scrape(vehicle_df, scrape_chunk, scrape_label="RERUN")


def get_db_connection():
    """Create and return a PostgreSQL database connection"""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD
        )
        return conn
    except psycopg2.Error as e:
        print(f"[ERROR] Database connection failed: {e}")
        return None


def download_file_from_url(url, timeout=30):
    """Download file from URL and return as BytesIO"""
    try:
        print(f"  Downloading file from: {url}")
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        return BytesIO(response.content)
    except requests.RequestException as e:
        print(f"  [ERROR] Download failed: {e}")
        return None


def upload_file_to_s3(file_buffer, s3_key):
    """Upload file buffer to S3 and return URL"""
    try:
        file_buffer.seek(0)  # Reset buffer position
        
        s3_client.upload_fileobj(
            file_buffer,
            AWS_S3_BUCKET_NAME,
            s3_key,
            ExtraArgs={
                'ContentType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'ContentDisposition': 'inline'
            }
        )
        
        # Generate S3 URL
        s3_url = f"https://{AWS_S3_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
        print(f"  [OK] Uploaded to S3: {s3_url}")
        return s3_url
        
    except NoCredentialsError:
        print("  [ERROR] AWS credentials not available")
        return None
    except ClientError as e:
        print(f"  [ERROR] S3 upload failed: {e}")
        return None
    except Exception as e:
        print(f"  [ERROR] Unexpected error uploading to S3: {e}")
        return None


def extract_filename_from_url(url):
    """Extract filename from S3 URL"""
    try:
        # URL format: https://snt-nhit-data.s3.us-east-1.amazonaws.com/NHIT_Vehicles/input/vrn_file_20251217_101521.xlsx
        filename = url.split('/')[-1]
        return filename
    except Exception as e:
        print(f"  [ERROR] Could not extract filename: {e}")
        return None


def reset_in_process_flag(record_id):
    """Reset in_process flag to 0 for a given record ID"""
    conn = get_db_connection()
    if not conn:
        print(f"  [WARNING] Could not reset in_process flag for Record ID {record_id} (connection failed)")
        return
    
    try:
        with conn.cursor() as cursor:
            update_query = """
                UPDATE nhit_vehicles
                SET in_process = 0
                WHERE id = %s
            """
            cursor.execute(update_query, (record_id,))
            conn.commit()
            print(f"  [OK] Reset in_process flag to 0 for Record ID {record_id}")
    except psycopg2.Error as e:
        print(f"  [ERROR] Failed to reset in_process flag: {e}")
        conn.rollback()
    finally:
        conn.close()


def process_excel_file(input_url, record_id):
    """Process a single Excel/CSV file: download, extract vehicles, scrape weights, upload result"""
    success = False
    try:
        print(f"\n{'='*80}")
        print(f"Processing Record ID: {record_id}")
        print(f"Input URL: {input_url}")
        print(f"{'='*80}")
        
        # Step 1: Download input file
        print("\n[STEP 1] Downloading input file...")
        file_buffer = download_file_from_url(input_url)
        if not file_buffer:
            return False

        # Detect file type from URL (simple extension-based check)
        input_url_lc = (input_url or "").lower()
        is_csv = input_url_lc.endswith(".csv")

        # Step 2: Read input and extract vehicle numbers
        # Header may not be on row 1 — scan first HEADER_SCAN_ROWS for keywords
        print("\n[STEP 2] Reading input file and extracting vehicle numbers...")
        try:
            df = load_input_dataframe(file_buffer, is_csv=is_csv)
            if df is None or df.empty:
                return False
        except Exception as e:
            print(f"  [ERROR] Failed to read input file: {e}")
            return False

        veh_col = _find_vehicle_column_in_df(df)
        if veh_col is None:
            print("  [ERROR] Could not find vehicle number column")
            print(f"  [INFO] Available columns: {list(df.columns)}")
            print(f"  [INFO] Expected column names: {VEHICLE_COLUMN_NAMES}")
            return False
        
        # Create dataframe with just vehicle numbers
        vehicle_df = pd.DataFrame({veh_col: df[veh_col]})
        vehicle_df = vehicle_df.dropna(subset=[veh_col])  # Remove rows with empty vehicle numbers
        vehicle_df[veh_col] = vehicle_df[veh_col].apply(normalize_vehicle_number)
        vehicle_df = vehicle_df[vehicle_df[veh_col] != '']  # Remove empty strings
        invalid_vehicle_count = (~vehicle_df[veh_col].apply(is_vehicle_number_eligible)).sum()
        if invalid_vehicle_count:
            print(
                f"  [INFO] Skipping {invalid_vehicle_count} vehicle numbers outside eligible length range "
                f"after cleanup (allowed lengths: 8, 9, 10)"
            )

        # Rename column to standard format that web scraping function expects
        # The function looks for columns with 'veh' and 'reg' in name
        # If column doesn't have both, rename to 'Veh Reg No.' for compatibility
        current_col = list(vehicle_df.columns)[0]
        if 'veh' not in current_col.lower() or 'reg' not in current_col.lower():
            vehicle_df = vehicle_df.rename(columns={current_col: 'Veh Reg No.'})
            print(f"  [INFO] Renamed column '{current_col}' to 'Veh Reg No.' for compatibility")
        
        print(f"  [OK] Extracted {len(vehicle_df)} vehicle numbers")
        
        if len(vehicle_df) == 0:
            print("  [WARNING] No vehicle numbers found in file")
            return False

        # Extract filename from input URL and use same name for output
        input_filename = extract_filename_from_url(input_url)
        
        # Step 3: Pass to appropriate web scraping function based on file type
        print("\n[STEP 3] Starting web scraping / automation based on file type...")
        filename_lc = (input_filename or "").lower()
        input_url_lc = (input_url or "").lower()
        is_ihmcl = "ihmcl" in filename_lc or "ihmcl" in input_url_lc
        if "permit" in filename_lc:
            # Permit scraping (Selenium Grid with local Chrome fallback, managed inside scraper)
            result_df = scrape_vehicle_details_for_permit(vehicle_df)
        elif is_ihmcl:
            # IHMCL FASTag portal automation (grid with local fallback)
            result_df = run_ihmcl_web_scrape(vehicle_df)
        elif "rerun" in filename_lc:
            # Web-only weight scraping for rerun files; updates checkpostmaster after scrape
            result_df = run_rerun_web_scrape(vehicle_df)
        else:
            # Default vehicle weight scraping: parallel DB phases, then same web scrape as before
            vehicle_df = parallel_prefetch_vehicle_db_weights(vehicle_df, num_workers=DB_PREFETCH_WORKERS)
            result_df = run_vehicle_weight_web_scrape(vehicle_df, skip_db_phases=True)
        
        if result_df is None or len(result_df) == 0:
            print("  [ERROR] Web scraping returned empty result")
            return False
        
        print(f"  [OK] Web scraping completed: {len(result_df)} vehicles processed")
        
        # Step 4: Convert result to Excel and upload to S3
        print("\n[STEP 4] Uploading result to S3...")
        
        if not input_filename:
            # Fallback: generate filename with timestamp
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            input_filename = f"vehicle_number_file_{timestamp}.xlsx"

        # Decide output filename:
        # - For Excel input: keep same name
        # - For CSV input: change extension to .xlsx (convert CSV -> XLSX logically)
        if input_filename.lower().endswith(".csv"):
            output_filename = input_filename[:-4] + ".xlsx"
        else:
            output_filename = input_filename  # Keep same filename as input
        
        # Check if filename or input URL contains specific keywords (case-insensitive) to decide S3 target folder
        filename_lc = (input_filename or "").lower()
        input_url_lc = (input_url or "").lower()
        if "permit" in filename_lc:
            s3_key = f"NHIT_Permit_Data/output/{output_filename}"
        elif "ihmcl" in filename_lc or "ihmcl" in input_url_lc:
            s3_key = f"NHIT_IHMCL/output/{output_filename}"
        elif "rerun" in filename_lc:
            s3_key = f"NHIT_RE_RUN/output/{output_filename}"
        else:
            s3_key = f"NHIT_Vehicles/output/{output_filename}"
        
        # Convert dataframe to Excel in memory
        excel_buffer = BytesIO()
        with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
            result_df.to_excel(writer, index=False, sheet_name='Sheet1')
        excel_buffer.seek(0)
        
        # Upload to S3
        output_url = upload_file_to_s3(excel_buffer, s3_key)
        if not output_url:
            return False
        
        # Step 5: Update database with output URL
        print("\n[STEP 5] Updating database...")
        conn = get_db_connection()
        if not conn:
            print("  [ERROR] Could not connect to database")
            print("  [INFO] Will reset in_process flag in finally block")
            return False
        
        try:
            with conn.cursor() as cursor:
                update_query = """
                    UPDATE nhit_vehicles
                    SET output_file_s3_url = %s, in_process = 0
                    WHERE id = %s
                """
                cursor.execute(update_query, (output_url, record_id))
                conn.commit()
                print(f"  [OK] Database updated with output URL for Record ID {record_id}")
                print(f"  [OK] Reset in_process flag to 0 for Record ID {record_id}")
                success = True
                return True
        except psycopg2.Error as e:
            print(f"  [ERROR] Database update failed: {e}")
            print("  [INFO] Will reset in_process flag in finally block")
            conn.rollback()
            return False
        finally:
            conn.close()
        
    except KeyboardInterrupt:
        print(f"\n  [WARNING] Processing interrupted for Record ID {record_id}")
        print("  [INFO] Will reset in_process flag in finally block")
        # Don't re-raise here - let finally block reset the flag, then return False
        return False
    except Exception as e:
        print(f"  [ERROR] Unexpected error processing file: {e}")
        import traceback
        print(traceback.format_exc())
        print("  [INFO] Will reset in_process flag in finally block")
        return False
    finally:
        # Always reset in_process flag if processing failed (not successful)
        # This ensures the flag is reset even if the function returns early or raises an exception
        if not success:
            print(f"  [INFO] Resetting in_process flag to 0 for Record ID {record_id} due to processing failure")
            reset_in_process_flag(record_id)


def check_and_process_pending_files():
    """Check database for pending files and process them"""
    conn = get_db_connection()
    if not conn:
        return 0
    
    record_id = None  # Track record_id to reset flag on error
    try:
        with conn.cursor() as cursor:
            # Atomically select and lock a row by setting in_process = 1
            # This prevents other instances from picking the same row
            query = """
                UPDATE nhit_vehicles
                SET in_process = 1
                WHERE id = (
                    SELECT id
                    FROM nhit_vehicles
                    WHERE (output_file_s3_url IS NULL OR output_file_s3_url = '' OR output_file_s3_url::text = '')
                    AND input_file_s3_url IS NOT NULL
                    AND input_file_s3_url != ''
                    AND (in_process IS NULL OR in_process = 0)
                    ORDER BY id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, input_file_s3_url
            """
            cursor.execute(query)
            result = cursor.fetchone()
            
            if result:
                record_id, input_url = result
                conn.commit()  # Commit the in_process = 1 update
                print(f"\n[INFO] Found pending file: Record ID {record_id} (locked for processing)")
                try:
                    success = process_excel_file(input_url, record_id)
                    return 1 if success else 0
                except Exception as e:
                    # If process_excel_file raises an exception, reset the flag
                    print(f"[ERROR] Exception during file processing: {e}")
                    import traceback
                    print(traceback.format_exc())
                    reset_in_process_flag(record_id)
                    return 0
            else:
                return 0
                
    except psycopg2.Error as e:
        print(f"[ERROR] Database query failed: {e}")
        conn.rollback()
        # If we had locked a record but failed, reset it
        if record_id:
            reset_in_process_flag(record_id)
        return 0
    except Exception as e:
        print(f"[ERROR] Unexpected error in check_and_process_pending_files: {e}")
        import traceback
        print(traceback.format_exc())
        # If we had locked a record but failed, reset it
        if record_id:
            reset_in_process_flag(record_id)
        return 0
    finally:
        conn.close()


def main():
    """Main loop - continuously check for pending files"""
    print("="*80)
    print("NHIT VEHICLES PROCESSING AUTOMATION")
    print("="*80)
    print("Program will continuously check for pending files (including IHMCL).")
    print(
        f"Web scrape mode: {'Selenium Grid (' + SELENIUM_REMOTE_URL + ')' if selenium_processing else 'local Chrome'}"
    )
    print("Press Ctrl+C to stop.")
    print("="*80)
    
    iteration = 0
    
    while True:
        iteration += 1
        if iteration > 10000:
            iteration = 1
        
        print(f"\n{'='*80}")
        print(f"ITERATION {iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*80}")
        
        try:
            processed = check_and_process_pending_files()
            
            if processed == 0:
                print("[INFO] No pending files to process")
            else:
                print(f"[OK] Processed {processed} file(s)")
        
        except KeyboardInterrupt:
            print("\n\n" + "="*80)
            print("SHUTDOWN REQUESTED - Stopping automation...")
            print("="*80)
            break
        except Exception as e:
            print(f"[ERROR] Unexpected error: {e}")
            import traceback
            print(traceback.format_exc())
        
        # Wait before next check (3 minutes)
        print(f"\n{'='*80}")
        print("Waiting 3 minutes before next check...")
        print(f"{'='*80}\n")
        time.sleep(180)


if __name__ == "__main__":
    main()

