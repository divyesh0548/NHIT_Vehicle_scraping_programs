import time
import pandas as pd
import psycopg2
from selenium.webdriver.support.ui import WebDriverWait

from web_scrap_for_lookup_chhattisgarh import (
    WAIT_TIME,
    _thread_remote_url,
    check_and_restore_db_connection,
    get_db_connection,
    navigate_to_tax_page,
    process_single_vehicle,
    setup_driver,
)
from vehicle_number_utils import is_vehicle_number_eligible, normalize_vehicle_number


def upsert_weight_to_checkpostmaster(cursor, veh_reg_no, weight):
    """Insert or update scraped weight into checkpostmaster only."""
    try:
        try:
            weight_value = float(weight)
        except (TypeError, ValueError):
            return False, f"Invalid weight: {weight}"

        # Update first so an existing vehicle is never inserted again.
        update_query = """
        UPDATE checkpostmaster
        SET weight = %s
        WHERE "Unique Vehicle Number" = %s
        """
        cursor.execute(update_query, (weight_value, veh_reg_no))

        if cursor.rowcount > 0:
            return True, "Updated weight in checkpostmaster"

        insert_query = """
        INSERT INTO checkpostmaster
        ("Unique Vehicle Number", "weight")
        VALUES (%s, %s)
        """
        cursor.execute(insert_query, (veh_reg_no, weight_value))
        return True, "Added successfully to checkpostmaster"

    except psycopg2.Error as e:
        return False, f"Database error: {e}"
    except Exception as e:
        return False, f"Error: {e}"


def scrape_vehicle_weights_rerun(df_input, remote_url=None):
    """
    Web-only vehicle weight scraping for rerun files.

    - Skips DB lookup phases entirely
    - Scrapes weight from website for each vehicle
    - Updates/inserts scraped weight into checkpostmaster only

    remote_url: if set, uses Selenium Grid (e.g. http://localhost:4444/wd/hub).
    """
    print("=" * 80)
    print("STARTING WEB-ONLY VEHICLE WEIGHT RERUN")
    print("=" * 80)
    if remote_url:
        print(f"Selenium Grid remote URL: {remote_url}")

    _thread_remote_url.value = remote_url
    try:
        return _scrape_vehicle_weights_rerun_impl(df_input)
    finally:
        _thread_remote_url.value = None


def _scrape_vehicle_weights_rerun_impl(df_input):
    df = df_input.copy()

    veh_col = None
    for col in df.columns:
        if "veh" in col.lower() and "reg" in col.lower():
            veh_col = col
            break

    if veh_col is None:
        print("[ERROR] Could not find vehicle number column in dataframe")
        return df

    if "Weight" not in df.columns:
        df["Weight"] = ""

    df[veh_col] = df[veh_col].apply(normalize_vehicle_number)
    invalid_vehicle_mask = ~df[veh_col].apply(is_vehicle_number_eligible)
    invalid_vehicle_count = int(invalid_vehicle_mask.sum())
    if invalid_vehicle_count:
        print(
            f"[INFO] Skipping {invalid_vehicle_count} vehicle numbers outside eligible length range "
            f"after cleanup (allowed lengths: 8, 9, 10)"
        )

    print(f"Total vehicles to process: {len(df)}")

    if len(df) == 0:
        print("No vehicles to process.")
        return df

    conn = get_db_connection()
    if not conn:
        print("[ERROR] Could not connect to database")
        return df

    driver = setup_driver()
    if not driver:
        print("[ERROR] Could not start browser — aborting")
        conn.close()
        return df

    wait = WebDriverWait(driver, WAIT_TIME)

    print("Navigating to tax page...")
    max_nav_attempts = 5
    nav_success = False

    for nav_attempt in range(1, max_nav_attempts + 1):
        print(f"[Attempt {nav_attempt}/{max_nav_attempts}] Navigating to tax page...")

        if navigate_to_tax_page(driver, wait):
            nav_success = True
            print("[OK] Navigation successful")
            break

        if nav_attempt < max_nav_attempts:
            print("Restarting browser and retrying...")
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(5)
            driver = setup_driver()
            if not driver:
                break
            wait = WebDriverWait(driver, WAIT_TIME)

    if not nav_success:
        print("[ERROR] Could not navigate to tax page — aborting")
        driver.quit()
        conn.close()
        return df

    start_time = time.perf_counter()
    scraped_count = 0
    db_added_count = 0
    db_updated_count = 0

    invalid_weight_values = {
        "N/A",
        "Error",
        "Error - Input Not Found",
        "Error - Browser Restart Failed",
        "Error - Max Retries",
        "-",
        "",
        "nan",
        "0",
    }

    for idx, row in df.iterrows():
        vehicle_no = normalize_vehicle_number(row[veh_col])
        if not vehicle_no or not is_vehicle_number_eligible(vehicle_no):
            df.at[idx, "Weight"] = "N/A"
            continue

        driver, wait, success = process_single_vehicle(driver, wait, vehicle_no, idx, df)

        weight = str(df.at[idx, "Weight"]).strip()

        if success:
            scraped_count += 1

            if weight not in invalid_weight_values:
                try:
                    conn = check_and_restore_db_connection(conn)
                    if conn is None:
                        continue

                    cursor = conn.cursor()
                    db_success, message = upsert_weight_to_checkpostmaster(cursor, vehicle_no, weight)
                    conn.commit()

                    if db_success:
                        if "Updated" in message:
                            db_updated_count += 1
                        else:
                            db_added_count += 1
                        print(f"[DB-OK] {vehicle_no}: {message}")
                    else:
                        print(f"[DB-INFO] {vehicle_no}: {message}")
                except Exception as e:
                    print(f"[DB-ERROR] {vehicle_no}: {e}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass

            print(
                f"Progress: {scraped_count}/{len(df)} scraped | "
                f"{db_added_count} added, {db_updated_count} updated in checkpostmaster"
            )

        time.sleep(1)

    try:
        driver.quit()
    except Exception:
        pass

    if conn:
        try:
            conn.close()
            print("[DB] Database connection closed")
        except Exception:
            pass

    elapsed = time.perf_counter() - start_time
    hrs = int(elapsed // 3600)
    mins = int((elapsed % 3600) // 60)
    secs = int(elapsed % 60)
    print(f"\nWeb scraping completed in {hrs:02d}:{mins:02d}:{secs:02d}")

    print("\n" + "=" * 80)
    print("PROCESSING COMPLETE")
    print("=" * 80)
    print(f"Total vehicles processed: {len(df)}")
    print(f"Web scraped: {scraped_count}")
    print(f"Added to checkpostmaster: {db_added_count}")
    print(f"Updated in checkpostmaster: {db_updated_count}")
    print("=" * 80)

    return df
