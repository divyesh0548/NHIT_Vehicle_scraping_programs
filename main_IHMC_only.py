import os
import time
import pandas as pd
import psycopg2
import boto3
import requests
from io import BytesIO
from datetime import datetime
from botocore.exceptions import NoCredentialsError, ClientError
from IHMCL_bot import scrape_ihmcl_for_dataframe
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
from vehicle_number_utils import is_vehicle_number_eligible, normalize_vehicle_number

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
    'Veh No.'
]


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
        print("\n[STEP 2] Reading input file and extracting vehicle numbers...")
        try:
            if is_csv:
                # CSV -> DataFrame
                file_buffer.seek(0)
                df = pd.read_csv(file_buffer)
                print(f"  [OK] CSV file loaded: {len(df)} rows")
            else:
                # Default: treat as Excel
                df = pd.read_excel(file_buffer, engine='openpyxl')
                print(f"  [OK] Excel file loaded: {len(df)} rows")
        except Exception as e:
            print(f"  [ERROR] Failed to read input file: {e}")
            return False
        
        # Find vehicle number column from the predefined list
        veh_col = None
        df_columns_lower = {col.lower(): col for col in df.columns}  # Create case-insensitive mapping
        
        # First, try exact match (case-insensitive)
        for col_name in VEHICLE_COLUMN_NAMES:
            if col_name.lower() in df_columns_lower:
                veh_col = df_columns_lower[col_name.lower()]
                print(f"  [OK] Found vehicle column (exact match): '{veh_col}'")
                break
        
        # If no exact match, try partial match (contains vehicle column name)
        if veh_col is None:
            for col_name in VEHICLE_COLUMN_NAMES:
                for df_col in df.columns:
                    if col_name.lower() in df_col.lower() or df_col.lower() in col_name.lower():
                        veh_col = df_col
                        print(f"  [OK] Found vehicle column (partial match): '{veh_col}'")
                        break
                if veh_col:
                    break
        
        # Fallback: search for columns with 'vehicle' and 'no' in name
        if veh_col is None:
            for col in df.columns:
                if 'vehicle' in col.lower() and 'no' in col.lower():
                    veh_col = col
                    print(f"  [OK] Found vehicle column (fallback search): '{veh_col}'")
                    break
        
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
        
        # Step 3: IHMCL only (non-IHMCL rows are never selected from DB in this script)
        print("\n[STEP 3] IHMCL FASTag portal automation...")
        filename_lc = (input_filename or "").lower()
        if "ihmcl" not in filename_lc and "ihmcl" not in (input_url or "").lower():
            print("  [SKIP] Not an IHMCL job — leaving row unchanged (should not happen with IHMCL-only DB filter)")
            return False
        result_df = scrape_ihmcl_for_dataframe(vehicle_df)
        
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
        
        s3_key = f"NHIT_IHMCL/output/{output_filename}"
        
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
            # Only IHMCL input URLs: never lock or set in_process on rows main.py should handle
            query = """
                UPDATE nhit_vehicles
                SET in_process = 1
                WHERE id = (
                    SELECT id
                    FROM nhit_vehicles
                    WHERE (output_file_s3_url IS NULL OR output_file_s3_url = '' OR output_file_s3_url::text = '')
                    AND input_file_s3_url IS NOT NULL
                    AND input_file_s3_url != ''
                    AND LOWER(input_file_s3_url) LIKE '%ihmcl%'
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
    """Main loop — pending IHMCL input files only (same table as main.py; URL must contain 'ihmcl')."""
    print("="*80)
    print("NHIT IHMCL-ONLY PROCESSING (parallel-safe with main.py)")
    print("="*80)
    print("Program will only lock rows whose input_file_s3_url contains 'ihmcl'.")
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

