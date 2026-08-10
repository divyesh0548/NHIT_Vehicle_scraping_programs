"""
Delete a nhit_vehicles row and its linked S3 input/output files.

Flow:
  1. Load RECORD_ID from nhit_vehicles
  2. Read input_file_s3_url and output_file_s3_url
  3. Delete those objects from S3 (skip if URL empty / object missing)
  4. DELETE the database row

When DRY_RUN is True: only prints what would be deleted (no S3/DB changes).

Configure via variables below (no CLI args).
"""

from urllib.parse import unquote, urlparse

import boto3
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
RECORD_ID = 404        # nhit_vehicles.id to delete
# True  -> print what would be deleted; no S3/DB changes
# False -> delete S3 objects and the DB row
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


def fetch_row_urls(record_id):
    """
    Return (input_file_s3_url, output_file_s3_url) for nhit_vehicles.id.

    Missing row -> None. Empty URLs are returned as None.
    """
    conn = get_db_connection()
    if not conn:
        return None

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT input_file_s3_url, output_file_s3_url
                FROM nhit_vehicles
                WHERE id = %s
                """,
                (record_id,),
            )
            row = cursor.fetchone()
            if not row:
                print(f"[ERROR] No nhit_vehicles row found for id={record_id}")
                return None

            def _clean(url):
                if url is None:
                    return None
                text = str(url).strip()
                return text or None

            return _clean(row[0]), _clean(row[1])
    except psycopg2.Error as e:
        print(f"[ERROR] Failed to fetch row: {e}")
        return None
    finally:
        conn.close()


def delete_db_row(record_id):
    """DELETE nhit_vehicles row by id."""
    conn = get_db_connection()
    if not conn:
        return False

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM nhit_vehicles
                WHERE id = %s
                """,
                (record_id,),
            )
            if cursor.rowcount == 0:
                print(f"[ERROR] No row deleted for id={record_id}")
                conn.rollback()
                return False
            conn.commit()
            print(f"[OK] Deleted nhit_vehicles row id={record_id}")
            return True
    except psycopg2.Error as e:
        print(f"[ERROR] Failed to delete DB row: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def s3_key_from_url(s3_url, bucket_name=AWS_S3_BUCKET_NAME):
    """
    Extract object key from a typical S3 HTTPS URL.

    Supports virtual-hosted and path-style URLs.
    """
    parsed = urlparse(str(s3_url).strip())
    host = (parsed.netloc or "").lower()
    path = unquote((parsed.path or "").lstrip("/"))

    if not path:
        raise ValueError(f"Could not parse S3 key from URL: {s3_url}")

    if host.startswith("s3.") or host.startswith("s3-"):
        parts = path.split("/", 1)
        if len(parts) == 2:
            return parts[1]
        raise ValueError(f"Unexpected path-style S3 URL: {s3_url}")

    if bucket_name and host.startswith(bucket_name.lower() + "."):
        return path

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


def delete_url_from_s3(url, label):
    """Parse URL to key and delete; skip quietly if url is empty."""
    if not url:
        print(f"[INFO] No {label} URL — skipping S3 delete")
        return True

    try:
        key = s3_key_from_url(url)
    except ValueError as e:
        print(f"[ERROR] {label}: {e}")
        return False

    print(f"  {label} URL : {url}")
    print(f"  {label} key : {key}")
    return delete_s3_object(key)


def delete_db_row_and_files(record_id=RECORD_ID, dry_run=DRY_RUN):
    """
    Delete nhit_vehicles row and its input/output S3 files.

    dry_run=True: print planned deletes only.
    """
    print("=" * 70)
    print("DELETE NHIT_VEHICLES ROW AND S3 FILES")
    print("=" * 70)
    print(f"Record ID: {record_id}")
    print(f"Dry run  : {dry_run}")

    if not record_id:
        print("[ERROR] RECORD_ID must be set to a valid nhit_vehicles id")
        return False

    print("\n[STEP 1] Fetching row from nhit_vehicles...")
    urls = fetch_row_urls(record_id)
    if urls is None:
        return False

    input_url, output_url = urls
    print(f"  input_file_s3_url : {input_url or '(empty)'}")
    print(f"  output_file_s3_url: {output_url or '(empty)'}")

    if dry_run:
        print("\n" + "=" * 70)
        print("DRY RUN — no S3 or DB deletes")
        print("=" * 70)
        for label, url in (
            ("input", input_url),
            ("output", output_url),
        ):
            if not url:
                print(f"  Would skip {label} S3 delete (empty URL)")
                continue
            try:
                key = s3_key_from_url(url)
                print(f"  Would delete {label} S3: s3://{AWS_S3_BUCKET_NAME}/{key}")
                print(f"    URL: {url}")
            except ValueError as e:
                print(f"  Would fail parsing {label} URL: {e}")
        print(f"  Would DELETE nhit_vehicles WHERE id = {record_id}")
        print("=" * 70)
        return True

    print("\n[STEP 2] Deleting S3 objects...")
    ok_input = delete_url_from_s3(input_url, "input")
    ok_output = delete_url_from_s3(output_url, "output")
    if not ok_input or not ok_output:
        print("[ERROR] S3 delete failed — DB row was NOT deleted")
        return False

    print("\n[STEP 3] Deleting database row...")
    if not delete_db_row(record_id):
        return False

    print("\n" + "=" * 70)
    print("DONE")
    print(f"  Deleted nhit_vehicles id={record_id}")
    print(f"  input S3  : {'deleted' if input_url else 'n/a'}")
    print(f"  output S3 : {'deleted' if output_url else 'n/a'}")
    print("=" * 70)
    return True


if __name__ == "__main__":
    ok = delete_db_row_and_files(record_id=RECORD_ID, dry_run=DRY_RUN)
    raise SystemExit(0 if ok else 1)
