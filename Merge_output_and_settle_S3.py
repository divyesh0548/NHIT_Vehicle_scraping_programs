"""
Merge two Excel files (same columns), replace the existing S3 output for a
nhit_vehicles record, and update output_file_s3_url.

Flow:
  1. Load RECORD_ID from nhit_vehicles -> read output_file_s3_url
  2. Derive S3 key from that URL
  3. Delete the existing object from S3
  4. Merge EXCEL_FILE_1 + EXCEL_FILE_2
  5. Upload merged Excel to the same S3 key
  6. Save the new URL into output_file_s3_url

When DRY_RUN is True: merge is still generated (and optionally saved locally),
but no S3 delete/upload and no DB updates are performed. The URL that would be
replaced and the new URL that would be written are printed instead.

Configure via variables below (no CLI args).
"""

from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

import boto3
import pandas as pd
import psycopg2
from botocore.exceptions import ClientError, NoCredentialsError

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
)

# ---------------------------------------------------------------------------
# Configure these variables before running
# ---------------------------------------------------------------------------
RECORD_ID = 384                    # nhit_vehicles.id to update
EXCEL_FILE_1 = "ihmcl_nathavalasa_1.xlsx"       # First Excel to merge
EXCEL_FILE_2 = "ihmcl_nathavalasa_2.xlsx"       # Second Excel to merge
# Optional local copy of merged file; blank = do not save locally
LOCAL_MERGED_FILE = ""
# True  -> merge only; print URLs that would be replaced / written (no S3/DB changes)
# False -> delete old S3 object, upload merged file, update DB
DRY_RUN = False
# ---------------------------------------------------------------------------

s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION,
)


def get_db_connection():
    try:
        return psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
        )
    except psycopg2.Error as e:
        print(f"[ERROR] Database connection failed: {e}")
        return None


def fetch_output_s3_url(record_id):
    """Return output_file_s3_url for nhit_vehicles.id, or None."""
    conn = get_db_connection()
    if not conn:
        return None

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT output_file_s3_url
                FROM nhit_vehicles
                WHERE id = %s
                """,
                (record_id,),
            )
            row = cursor.fetchone()
            if not row:
                print(f"[ERROR] No nhit_vehicles row found for id={record_id}")
                return None
            url = row[0]
            if url is None or str(url).strip() == "":
                print(f"[ERROR] output_file_s3_url is empty for id={record_id}")
                return None
            return str(url).strip()
    except psycopg2.Error as e:
        print(f"[ERROR] Failed to fetch output_file_s3_url: {e}")
        return None
    finally:
        conn.close()


def update_output_s3_url(record_id, new_url):
    """Set output_file_s3_url for the given nhit_vehicles id."""
    conn = get_db_connection()
    if not conn:
        return False

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE nhit_vehicles
                SET output_file_s3_url = %s
                WHERE id = %s
                """,
                (new_url, record_id),
            )
            if cursor.rowcount == 0:
                print(f"[ERROR] No row updated for id={record_id}")
                conn.rollback()
                return False
            conn.commit()
            print(f"[OK] Updated output_file_s3_url for id={record_id}")
            return True
    except psycopg2.Error as e:
        print(f"[ERROR] Failed to update output_file_s3_url: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def clear_output_s3_url(record_id):
    """Clear output_file_s3_url after deleting the S3 object (before re-upload)."""
    conn = get_db_connection()
    if not conn:
        return False

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE nhit_vehicles
                SET output_file_s3_url = NULL
                WHERE id = %s
                """,
                (record_id,),
            )
            conn.commit()
            print(f"[OK] Cleared output_file_s3_url for id={record_id}")
            return True
    except psycopg2.Error as e:
        print(f"[ERROR] Failed to clear output_file_s3_url: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def s3_key_from_url(s3_url, bucket_name=AWS_S3_BUCKET_NAME):
    """
    Extract object key from a typical S3 HTTPS URL.

    Supports:
      https://bucket.s3.region.amazonaws.com/path/to/file.xlsx
      https://bucket.s3.amazonaws.com/path/to/file.xlsx
      https://s3.region.amazonaws.com/bucket/path/to/file.xlsx
    """
    parsed = urlparse(str(s3_url).strip())
    host = (parsed.netloc or "").lower()
    path = unquote((parsed.path or "").lstrip("/"))

    if not path:
        raise ValueError(f"Could not parse S3 key from URL: {s3_url}")

    # Path-style: s3.region.amazonaws.com/bucket/key
    if host.startswith("s3.") or host.startswith("s3-"):
        parts = path.split("/", 1)
        if len(parts) == 2 and parts[0] == bucket_name:
            return parts[1]
        if len(parts) == 2:
            return parts[1]
        raise ValueError(f"Unexpected path-style S3 URL: {s3_url}")

    # Virtual-hosted: bucket.s3.... / key
    if bucket_name and host.startswith(bucket_name.lower() + "."):
        return path

    # Fallback: treat full path as key
    return path


def delete_s3_object(s3_key, bucket_name=AWS_S3_BUCKET_NAME):
    """Delete an object from S3. Missing object is treated as success."""
    try:
        s3_client.delete_object(Bucket=bucket_name, Key=s3_key)
        print(f"[OK] Deleted S3 object: s3://{bucket_name}/{s3_key}")
        return True
    except NoCredentialsError:
        print("[ERROR] AWS credentials not available")
        return False
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            print(f"[WARN] S3 object already missing: {s3_key}")
            return True
        print(f"[ERROR] S3 delete failed: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] Unexpected S3 delete error: {e}")
        return False


def upload_excel_buffer_to_s3(file_buffer, s3_key, bucket_name=AWS_S3_BUCKET_NAME):
    """Upload Excel buffer to S3 and return the public HTTPS URL."""
    try:
        file_buffer.seek(0)
        s3_client.upload_fileobj(
            file_buffer,
            bucket_name,
            s3_key,
            ExtraArgs={
                "ContentType": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                "ContentDisposition": "inline",
            },
        )
        s3_url = f"https://{bucket_name}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
        print(f"[OK] Uploaded to S3: {s3_url}")
        return s3_url
    except NoCredentialsError:
        print("[ERROR] AWS credentials not available")
        return None
    except ClientError as e:
        print(f"[ERROR] S3 upload failed: {e}")
        return None
    except Exception as e:
        print(f"[ERROR] Unexpected S3 upload error: {e}")
        return None


def merge_excel_files(file1, file2):
    """
    Merge two Excel files that share the same columns.

    Concatenates rows; columns are the union ordered by file1 then any extras
    from file2. Both files must have identical column sets.
    """
    path1 = Path(file1)
    path2 = Path(file2)
    if not path1.exists():
        raise FileNotFoundError(f"Excel file not found: {path1}")
    if not path2.exists():
        raise FileNotFoundError(f"Excel file not found: {path2}")

    df1 = pd.read_excel(path1, dtype=str)
    df2 = pd.read_excel(path2, dtype=str)
    print(f"[OK] Loaded {path1.name}: {len(df1)} rows, columns={list(df1.columns)}")
    print(f"[OK] Loaded {path2.name}: {len(df2)} rows, columns={list(df2.columns)}")

    cols1 = list(df1.columns)
    cols2 = list(df2.columns)
    if cols1 != cols2:
        raise ValueError(
            "Excel files must have the same column names in the same order.\n"
            f"  File 1: {cols1}\n"
            f"  File 2: {cols2}"
        )

    merged = pd.concat([df1, df2], ignore_index=True)
    print(f"[OK] Merged dataframe: {len(merged)} rows ({len(df1)} + {len(df2)})")
    return merged


def dataframe_to_excel_buffer(df):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sheet1")
    buffer.seek(0)
    return buffer


def build_s3_url(s3_key, bucket_name=AWS_S3_BUCKET_NAME, region=AWS_REGION):
    """Build the HTTPS URL for an S3 object key (same format as upload)."""
    return f"https://{bucket_name}.s3.{region}.amazonaws.com/{s3_key}"


def merge_and_settle_s3(
    record_id=RECORD_ID,
    excel_file_1=EXCEL_FILE_1,
    excel_file_2=EXCEL_FILE_2,
    local_merged_file=LOCAL_MERGED_FILE,
    dry_run=DRY_RUN,
):
    """
    Merge two Excels and replace the S3/DB output for nhit_vehicles.id.

    dry_run=True: generate merge (and optional local file), print old/new URLs,
    do not touch S3 or the database.
    """
    print("=" * 70)
    print("MERGE OUTPUT AND SETTLE S3")
    print("=" * 70)
    print(f"Record ID   : {record_id}")
    print(f"Excel file 1: {excel_file_1}")
    print(f"Excel file 2: {excel_file_2}")
    print(f"Dry run     : {dry_run}")

    if not record_id:
        print("[ERROR] RECORD_ID must be set to a valid nhit_vehicles id")
        return False

    # 1) Fetch existing output URL / S3 key
    print("\n[STEP 1] Fetching output_file_s3_url from nhit_vehicles...")
    old_url = fetch_output_s3_url(record_id)
    if not old_url:
        return False
    print(f"  Current URL: {old_url}")

    try:
        s3_key = s3_key_from_url(old_url)
    except ValueError as e:
        print(f"[ERROR] {e}")
        return False
    print(f"  S3 key    : {s3_key}")

    new_url = build_s3_url(s3_key)

    # 2) Merge Excels
    print("\n[STEP 2] Merging Excel files...")
    try:
        merged_df = merge_excel_files(excel_file_1, excel_file_2)
    except Exception as e:
        print(f"[ERROR] Merge failed: {e}")
        return False

    local_path = (local_merged_file or "").strip()
    if local_path:
        merged_df.to_excel(local_path, index=False)
        print(f"[OK] Local merged copy saved: {local_path}")
    elif dry_run:
        # Always materialize a dry-run merge file so the result can be inspected
        dry_path = Path(f"merged_dry_run_id_{record_id}.xlsx")
        merged_df.to_excel(dry_path, index=False)
        print(f"[OK] Dry-run merged file saved: {dry_path.resolve()}")

    if dry_run:
        print("\n" + "=" * 70)
        print("DRY RUN — no S3 delete/upload and no DB updates")
        print("=" * 70)
        print(f"  Rows merged              : {len(merged_df)}")
        print(f"  S3 key                   : {s3_key}")
        print(f"  URL that would be replaced: {old_url}")
        print(f"  New URL that would be set : {new_url}")
        print("=" * 70)
        return True

    # 3) Delete old S3 object and clear DB column
    print("\n[STEP 3] Removing existing S3 object and clearing DB column...")
    if not delete_s3_object(s3_key):
        return False
    if not clear_output_s3_url(record_id):
        return False

    # 4) Upload merged file to the same S3 key
    print("\n[STEP 4] Uploading merged Excel to the same S3 key...")
    buffer = dataframe_to_excel_buffer(merged_df)
    uploaded_url = upload_excel_buffer_to_s3(buffer, s3_key)
    if not uploaded_url:
        return False

    # 5) Save new URL
    print("\n[STEP 5] Saving new output_file_s3_url...")
    if not update_output_s3_url(record_id, uploaded_url):
        return False

    print("\n" + "=" * 70)
    print("DONE")
    print(f"  Rows merged : {len(merged_df)}")
    print(f"  S3 key      : {s3_key}")
    print(f"  Old URL     : {old_url}")
    print(f"  New URL     : {uploaded_url}")
    print("=" * 70)
    return True


if __name__ == "__main__":
    ok = merge_and_settle_s3(
        record_id=RECORD_ID,
        excel_file_1=EXCEL_FILE_1,
        excel_file_2=EXCEL_FILE_2,
        local_merged_file=LOCAL_MERGED_FILE,
        dry_run=DRY_RUN,
    )
    raise SystemExit(0 if ok else 1)
