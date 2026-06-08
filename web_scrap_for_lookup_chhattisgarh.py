import time
import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
import psycopg2
from psycopg2 import extras
from typing import Tuple
from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from vehicle_number_utils import is_vehicle_number_eligible, normalize_vehicle_number

URL = "https://parivahan.gov.in/"
WAIT_TIME = 30
ELEMENT_WAIT = 15
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
        print("[DB] Connection established")
        return conn
    except psycopg2.Error as e:
        print(f"[DB-ERROR] Error connecting to database: {e}")
        return None

def check_and_restore_db_connection(conn):
    """
    Check if connection is still open. If closed, reconnect.
    
    Args:
        conn: PostgreSQL connection object
        
    Returns:
        conn: Open PostgreSQL connection (new if old one was closed)
    """
    if conn is None:
        print("[DB] Connection is None, creating new connection...")
        return get_db_connection()
    
    try:
        # Try to execute a simple query to check if connection is alive
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
        print("[DB] Connection is healthy")
        return conn
    except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
        print(f"[DB-WARNING] Connection closed ({e}), reconnecting...")
        try:
            conn.close()
        except:
            pass
        
        # Try to reconnect
        new_conn = get_db_connection()
        if new_conn:
            print("[DB] Successfully reconnected to database")
            return new_conn
        else:
            print("[DB-ERROR] Could not reconnect to database")
            return None
    except Exception as e:
        print(f"[DB-ERROR] Error checking connection: {e}")
        return conn

def setup_driver():
    try:
        options = webdriver.ChromeOptions()
        
        # Basic options
        options.add_argument('--disable-images')
        options.add_argument('--blink-settings=imagesEnabled=false')
        options.add_argument('--disable-gpu')
        options.page_load_strategy = 'normal'
        
        # Stability and compatibility options
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-software-rasterizer')
        options.add_argument('--disable-background-timer-throttling')
        options.add_argument('--disable-backgrounding-occluded-windows')
        options.add_argument('--disable-renderer-backgrounding')
        options.add_argument('--disable-features=TranslateUI')
        options.add_argument('--remote-allow-origins=*')
        options.add_argument('--disable-web-security')
        options.add_argument('--allow-running-insecure-content')
        
        # User agent to avoid detection
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        # Preferences
        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
            "profile.default_content_settings.popups": 0
        }
        options.add_experimental_option("prefs", prefs)
        
        # Try to create driver with service
        try:
            # First try with default service
            driver = webdriver.Chrome(options=options)
        except Exception as e1:
            print(f"  [WARN] First attempt failed: {e1}")
            print("  [INFO] Retrying with explicit service configuration...")
            try:
                # Try with explicit service (handles ChromeDriver path issues)
                service = Service()
                driver = webdriver.Chrome(service=service, options=options)
            except Exception as e2:
                print(f"  [WARN] Second attempt failed: {e2}")
                print("  [INFO] Retrying with minimal options...")
                # Last resort: minimal options
                minimal_options = webdriver.ChromeOptions()
                minimal_options.add_argument('--no-sandbox')
                minimal_options.add_argument('--disable-dev-shm-usage')
                driver = webdriver.Chrome(options=minimal_options)
        
        try:
            driver.maximize_window()
        except Exception as e:
            print(f"  [WARNING] Could not maximize window: {e}")
            # Continue anyway - window might already be maximized
        
        # Verify driver is responsive (without JavaScript test to avoid Runtime.evaluate issues)
        try:
            _ = driver.current_url
            print("Browser launched successfully - Driver is responsive")
        except Exception as url_error:
            print(f"  [ERROR] Driver is not responsive: {url_error}")
            try:
                driver.quit()
            except:
                pass
            return None
        
        return driver
        
    except WebDriverException as e:
        print(f"Browser setup failed: {e}")
        import traceback
        print(f"Full error traceback:\n{traceback.format_exc()}")
        return None
    except Exception as e:
        print(f"Unexpected error during browser setup: {e}")
        import traceback
        print(f"Full error traceback:\n{traceback.format_exc()}")
        return None

def wait_for_page_load(driver, wait, timeout=30):
    """Wait for page to completely load."""
    try:
        print("Waiting for page to load completely...")
        wait.until(lambda driver: driver.execute_script("return document.readyState") == "complete")
        print("Page loaded completely")
        return True
    except Exception as e:
        print(f"Page load timeout: {e}")
        return False

def close_mobile_popup(driver, wait):
    """Close the mobile number update popup if it appears."""
    try:
        popup_text_xpath = "//span[contains(@class, 'english') and contains(text(), 'Update Your Mobile Number')]"
        popup_text = WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.XPATH, popup_text_xpath)))
        if popup_text:
            close_button_xpath = "//button[contains(@class, 'btn-close') and contains(@class, 'position-absolute')]"
            close_button = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, close_button_xpath)))
            close_button.click()
            print("Mobile number popup closed")
            time.sleep(2)
            return True
    except TimeoutException:
        return False
    except Exception as e:
        print(f"Error handling mobile popup: {e}")
        return False

def select_state_chhattisgarh(driver, wait):
    """Select Chhattisgarh from the state selection interface."""
    for attempt in range(3):
        try:
            print(f"Selecting Chhattisgarh state (attempt {attempt+1})")
            state_selectors = [
                "//div[contains(text(), 'Select State Name')]",
                "//span[contains(text(), 'Select State Name')]",
                "//a[contains(text(), 'Select State Name')]",
                "//button[contains(text(), 'Select State Name')]",
                "//*[contains(text(), 'Select State Name') and contains(@class, 'select')]",
                "//*[contains(text(), 'Select State Name')]"
            ]
            state_element = None
            for selector in state_selectors:
                try:
                    state_element = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    print(f"Found state selection element with: {selector}")
                    break
                except:
                    continue

            if not state_element:
                print("Could not find state selection element")
                return False

            driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});", state_element)
            time.sleep(1)
            print("Clicking state selection element...")
            state_element.click()
            time.sleep(2)

            chhattisgarh_selectors = [
                "//div[contains(text(), 'CHHATTISGARH')]",
                "//span[contains(text(), 'CHHATTISGARH')]",
                "//a[contains(text(), 'CHHATTISGARH')]",
                "//li[contains(text(), 'CHHATTISGARH')]",
                "//option[contains(text(), 'CHHATTISGARH')]",
                "//*[contains(text(), 'CHHATTISGARH') and contains(@class, 'option')]",
                "//*[contains(text(), 'CHHATTISGARH')]"
            ]
            chhattisgarh_element = None
            for selector in chhattisgarh_selectors:
                try:
                    chhattisgarh_element = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    print(f"Found Chhattisgarh option with: {selector}")
                    break
                except:
                    continue

            if not chhattisgarh_element:
                print("Could not find Chhattisgarh option")
                return False

            driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});", chhattisgarh_element)
            time.sleep(1)
            print("Clicking Chhattisgarh...")
            chhattisgarh_element.click()
            time.sleep(3)

            service_heading_xpath = "//h3[@class='top-space' and contains(text(), 'Service Name')]"
            try:
                WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.XPATH, service_heading_xpath)))
                print("State selection successful - Service Name section found")
                return True
            except:
                print("Service Name section not found yet")
                print("Retrying state selection...")
                time.sleep(2)
        except Exception as e:
            print(f"Error selecting state on attempt {attempt+1}: {e}")
            time.sleep(2)

    print("Failed to select Chhattisgarh after all attempts")
    return False

def select_service(driver, wait):
    """Select service on the service selection page."""
    for attempt in range(3):
        try:
            print(f"\nSelecting service (attempt {attempt+1})")
            print("Waiting for service dropdown...")
            service_dropdown_xpath = "//span[contains(@class,'ui-selectonemenu-label') and contains(text(),'---Select Service Name---')]"
            service_dropdown = wait.until(EC.element_to_be_clickable((By.XPATH, service_dropdown_xpath)))
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});", service_dropdown)
            time.sleep(1)
            print("Clicking service dropdown...")
            service_dropdown.click()
            time.sleep(2)

            print("Waiting for ADVANCE PAYMENT OF ODC EXEMPTION FEE option...")
            service_option_xpath = "//li[contains(@data-label, 'ADVANCE PAYMENT OF ODC EXEMPTION FEE')]"
            service_option = wait.until(EC.element_to_be_clickable((By.XPATH, service_option_xpath)))
            print("Selecting ADVANCE PAYMENT OF ODC EXEMPTION FEE...")
            service_option.click()
            time.sleep(2)

            print("Selected service: ADVANCE PAYMENT OF ODC EXEMPTION FEE")
            print("Waiting for Go button...")
            go_button_xpath = "//button[.//span[contains(text(), 'Go')]]"
            go_button = wait.until(EC.element_to_be_clickable((By.XPATH, go_button_xpath)))
            print("Clicking Go button...")
            go_button.click()

            print("Waiting for vehicle entry page...")
            time.sleep(5)

            vehicle_input_xpath = "//input[@type='text' and @maxlength='10']"
            try:
                WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.XPATH, vehicle_input_xpath)))
                print("Successfully loaded vehicle entry page")
                return True
            except:
                print("Vehicle entry page not loaded, retrying...")
                driver.execute_script("location.reload()")
                time.sleep(3)
        except Exception as e:
            print(f"Error selecting service: {e}")
            driver.execute_script("location.reload()")
            time.sleep(3)

    print("Failed to select service after multiple attempts")
    return False

def navigate_to_tax_page(driver, wait):
    """Navigate from parivahan.gov.in to the tax collection page."""
    try:
        print("Opening parivahan.gov.in...")
        driver.get(URL)

        if not wait_for_page_load(driver, wait):
            return False

        popup_closed = close_mobile_popup(driver, wait)
        if popup_closed:
            print("Mobile number popup closed")
        else:
            print("No mobile number popup found")

        print("Hovering over Online Services...")
        online_services_xpath = "//a[@id='Online' and contains(@class, 'parent-link-with-submenu')]"
        online_services = wait.until(EC.element_to_be_clickable((By.XPATH, online_services_xpath)))
        ActionChains(driver).move_to_element(online_services).pause(2).perform()
        time.sleep(3)

        print("Clicking Checkpost Tax...")
        checkpost_tax_xpath = "//a[@href='/en/node/579' and contains(@class, 'second-child-menu')]"
        checkpost_tax = wait.until(EC.element_to_be_clickable((By.XPATH, checkpost_tax_xpath)))
        checkpost_tax.click()

        print("Waiting for Checkpost Tax page to load...")
        checkpost_title_xpath = "//span[@class='field field--name-title field--type-string field--label-hidden' and contains(text(), 'Checkpost Tax')]"
        wait.until(EC.visibility_of_element_located((By.XPATH, checkpost_title_xpath)))
        print("Checkpost Tax page loaded successfully")

        if not select_state_chhattisgarh(driver, wait):
            print("Failed to select Chhattisgarh state")
            return False

        print("Waiting for Service Name section...")
        service_heading_xpath = "//h3[@class='top-space' and contains(text(), 'Service Name')]"
        wait.until(EC.visibility_of_element_located((By.XPATH, service_heading_xpath)))
        print("Service Name section is visible")

        if not select_service(driver, wait):
            print("Failed to select service")
            return False

        print("Successfully navigated to vehicle entry page")
        return True

    except Exception as e:
        print(f"Error navigating to tax page: {e}")
        return False

def safe_click(driver, wait, xpath, description="element", timeout=15):
    """Safely click element with proper waits."""
    try:
        print(f"Waiting for {description} to be clickable...")
        elem = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, xpath)))
        print(f"Clicking {description}...")
        elem.click()
        time.sleep(1)
        print(f"Successfully clicked {description}")
        return True
    except TimeoutException:
        print(f"Timeout: could not find or click {description}")
        return False
    except Exception as e:
        print(f"Error clicking {description}: {e}")
        return False

def get_vehicle_weight(driver):
    """Extract vehicle weight (GVW) from txt_laden_wt element."""
    weight = "0"
    try:
        print("Getting GVW (Laden Weight)...")
        try:
            weight_element = driver.find_element(By.ID, "txt_laden_wt")
            weight = weight_element.get_attribute('value') or "0"
            print(f"GVW (Laden Weight): {weight}")
        except Exception as e:
            print(f"Could not find laden weight by ID: {e}")
            weight_selectors = [
                "//input[contains(@id, 'laden') and contains(@class, 'ui-state-filled')]",
                "//input[contains(@name, 'laden') and contains(@class, 'ui-state-filled')]",
                "//input[contains(@id, 'weight') and contains(@class, 'ui-state-filled')]",
                "//input[contains(@name, 'weight') and contains(@class, 'ui-state-filled')]"
            ]
            for selector in weight_selectors:
                try:
                    weight_element = driver.find_element(By.XPATH, selector)
                    weight = weight_element.get_attribute('value') or "0"
                    if weight != "0":
                        print(f"Found weight via fallback: {weight}")
                        break
                except:
                    continue

            if weight == "0" or not weight:
                print("Weight is 0 or empty, checking for any weight input...")
                try:
                    all_inputs = driver.find_elements(By.XPATH, "//input[contains(@class, 'ui-state-filled')]")
                    for inp in all_inputs:
                        value = inp.get_attribute('value') or ''
                        if value and value.isdigit() and int(value) > 0:
                            try:
                                parent_text = inp.find_element(By.XPATH, "./ancestor::tr[1] | ./ancestor::div[1]").text.lower()
                                if 'weight' in parent_text or 'laden' in parent_text or 'gvw' in parent_text:
                                    weight = value
                                    print(f"Found weight via text analysis: {weight}")
                                    break
                            except:
                                continue
                except Exception as e:
                    print(f"Error in final weight search: {e}")

    except Exception as e:
        print(f"Error in get_vehicle_weight: {e}")

    return weight

def restart_browser_and_continue(driver):
    """Restart browser and continue."""
    print(f"\n[RESTART] RESTARTING BROWSER...")
    try:
        driver.quit()
        print("Closed current browser session")
    except:
        print("Could not properly close browser")

    time.sleep(3)

    new_driver = setup_driver()
    if not new_driver:
        print("Could not restart browser — aborting")
        return None, None

    new_wait = WebDriverWait(new_driver, WAIT_TIME)

    print("Re-navigating to tax page...")
    if not navigate_to_tax_page(new_driver, new_wait):
        print("Could not navigate to tax page after restart — aborting")
        new_driver.quit()
        return None, None

    print("[OK] Browser restarted successfully")
    return new_driver, new_wait

def insert_vehicle_to_checkpost(cursor, veh_reg_no, weight):
    """Insert or update a single vehicle record into appropriate table based on weight.
    
    - If weight < 100: Insert/Update into capacity_vehicle_numbers table
    - If weight >= 100: Insert/Update into checkpostmaster table
    
    If vehicle exists in checkpostmaster, update its weight.
    If vehicle exists in the target table, update its weight.
    Otherwise, insert new record.
    
    Returns: (success: bool, message: str, table_name: str)
    """
    try:
        # Convert weight to float and validate
        try:
            weight_value = float(weight)
        except (TypeError, ValueError):
            return False, f"Invalid weight: {weight}", None

        # First, check if vehicle exists in checkpostmaster (regardless of target table)
        check_checkpost_query = """
        SELECT COUNT(*) as count
        FROM checkpostmaster
        WHERE "Unique Vehicle Number" = %s
        """
        cursor.execute(check_checkpost_query, (veh_reg_no,))
        checkpost_result = cursor.fetchone()
        exists_in_checkpost = checkpost_result[0] > 0

        # Determine which table to use based on weight
        if weight_value < 100:
            table_name = "capacity_vehicle_numbers"
        else:
            table_name = "checkpostmaster"

        # If vehicle exists in checkpostmaster, always update checkpostmaster
        if exists_in_checkpost:
            update_query = """
            UPDATE checkpostmaster
            SET weight = %s
            WHERE "Unique Vehicle Number" = %s
            """
            cursor.execute(update_query, (weight_value, veh_reg_no))
            return True, f"Updated weight in checkpostmaster", "checkpostmaster"

        # Check if vehicle already exists in the target table
        check_query = f"""
        SELECT COUNT(*) as count
        FROM {table_name}
        WHERE "Unique Vehicle Number" = %s
        """
        cursor.execute(check_query, (veh_reg_no,))
        result = cursor.fetchone()

        if result[0] > 0:
            # Update existing record
            update_query = f"""
            UPDATE {table_name}
            SET weight = %s
            WHERE "Unique Vehicle Number" = %s
            """
            cursor.execute(update_query, (weight_value, veh_reg_no))
            return True, f"Updated weight in {table_name}", table_name
        else:
            # Insert new vehicle into the appropriate table
            insert_query = f"""
            INSERT INTO {table_name}
            ("Unique Vehicle Number", "weight")
            VALUES (%s, %s)
            """
            cursor.execute(insert_query, (veh_reg_no, weight_value))
            return True, f"Added successfully to {table_name}", table_name

    except psycopg2.Error as e:
        return False, f"Database error: {e}", None
    except Exception as e:
        return False, f"Error: {e}", None

def process_single_vehicle(driver, wait, vehicle_no, idx, df):
    """Process a single vehicle - extract weight only."""
    max_retries = 5
    retry_count = 0

    while retry_count <= max_retries:
        try:
            print(f"\n{'='*50}")
            print(f"Processing {idx+1}/{len(df)} — Vehicle: {vehicle_no} (Attempt {retry_count + 1})")
            print(f"{'='*50}")

            print("Quick refreshing page...")
            driver.execute_script("location.reload()")
            time.sleep(2)

            vehicle_input_xpath = "//input[@type='text' and @maxlength='10']"

            print("Waiting for Vehicle Number input to be interactable...")
            try:
                input_element = WebDriverWait(driver, 15).until(EC.element_to_be_clickable((By.XPATH, vehicle_input_xpath)))
                print("[OK] Vehicle Number input found and ready")
            except TimeoutException:
                print("[ERROR] Timeout: could not find Vehicle Number input")
                if retry_count < max_retries:
                    retry_count += 1
                    print(f"[RETRY] Attempting browser restart ({retry_count}/{max_retries})...")
                    new_driver, new_wait = restart_browser_and_continue(driver)
                    if new_driver:
                        driver = new_driver
                        wait = new_wait
                        continue
                    else:
                        break
                else:
                    print("[ERROR] Max retries reached")
                    df.at[idx, "Weight"] = "Error - Input Not Found"
                    return driver, wait, False

            try:
                popup_xpath = "//div[contains(@class,'ui-dialog') and contains(@style,'display: block')]"
                popup = WebDriverWait(driver, 2).until(EC.visibility_of_element_located((By.XPATH, popup_xpath)))
                if popup:
                    ok_button_xpath = "//div[contains(@class,'ui-dialog')]//button[.//span[contains(text(),'OK') or contains(text(),'Ok')]]"
                    safe_click(driver, wait, ok_button_xpath, "OK button on popup")
            except TimeoutException:
                pass

            try:
                input_element = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, vehicle_input_xpath)))
                driver.execute_script("arguments[0].value = arguments[1];", input_element, vehicle_no)
                print("[OK] Vehicle number entered via JavaScript")
            except:
                input_element.clear()
                input_element.send_keys(vehicle_no)
                print("[OK] Vehicle number entered")

            get_details_xpath = "//button[.//span[contains(text(), 'Get Details')]]"
            try:
                get_details_btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, get_details_xpath)))
                driver.execute_script("arguments[0].click();", get_details_btn)
                print("[OK] Get Details clicked")
            except:
                safe_click(driver, wait, get_details_xpath, "Get Details button")

            print("Waiting for vehicle details...")
            time.sleep(3)

            popup_appeared = False
            data_appeared = False

            for attempt in range(3):
                try:
                    popup_xpath = "//div[contains(@class,'ui-dialog') and contains(@style,'display: block')]"
                    try:
                        popup = WebDriverWait(driver, 2).until(EC.visibility_of_element_located((By.XPATH, popup_xpath)))
                        popup_appeared = True
                        print("Popup detected - no data available")
                        break
                    except TimeoutException:
                        pass

                    try:
                        filled_fields = driver.find_elements(By.XPATH, "//input[@class='ui-state-filled'] | //span[contains(@class,'ui-selectonemenu-label') and not(contains(text(),'---Select'))]")
                        if filled_fields:
                            data_appeared = True
                            print("[OK] Vehicle details loaded")
                            break
                    except:
                        pass

                except Exception:
                    pass

                if not popup_appeared and not data_appeared:
                    time.sleep(1)

            if popup_appeared:
                ok_button_xpath = "//div[contains(@class,'ui-dialog')]//button[.//span[contains(text(),'OK') or contains(text(),'Ok')]]"
                safe_click(driver, wait, ok_button_xpath, "OK button on popup")
                df.at[idx, "Weight"] = "N/A"
                print("[OK] Marked as N/A (no data available)")
                return driver, wait, True

            elif data_appeared:
                weight = get_vehicle_weight(driver)
                df.at[idx, "Weight"] = weight
                print(f"[OK] Weight extracted: {weight}")
                return driver, wait, True

            else:
                # No data loaded after 2 attempts - restart browser and retry
                print("[WARNING] No data loaded after attempts - restarting browser...")
                new_driver, new_wait = restart_browser_and_continue(driver)
                if new_driver:
                    # Browser restarted successfully - retry this vehicle
                    print(f"[RETRY] Re-processing vehicle {vehicle_no} after browser restart...")
                    driver = new_driver
                    wait = new_wait
                    return process_single_vehicle(driver, wait, vehicle_no, idx, df)
                else:
                    # Browser restart failed - mark as error
                    print(f"[ERROR] Could not restart browser for {vehicle_no}")
                    df.at[idx, "Weight"] = "Error - Browser Restart Failed"
                    return driver, wait, False

        except Exception as e:
            print(f"Error processing {vehicle_no}: {e}")
            if retry_count < max_retries:
                retry_count += 1
                print(f"[RETRY] Attempting browser restart ({retry_count}/{max_retries})...")
                new_driver, new_wait = restart_browser_and_continue(driver)
                if new_driver:
                    driver = new_driver
                    wait = new_wait
                    continue
                else:
                    break
            else:
                print("[ERROR] Max retries reached")
                df.at[idx, "Weight"] = "Error"
                return driver, wait, False

    df.at[idx, "Weight"] = "Error - Max Retries"
    return driver, wait, False

def check_vehicle_in_database(cursor, vehicle_no, table_name):
    """Check if vehicle exists in database table and return weight if found."""
    try:
        query = f"""
        SELECT weight
        FROM {table_name}
        WHERE "Unique Vehicle Number" = %s
        """
        cursor.execute(query, (vehicle_no,))
        result = cursor.fetchone()
        if result:
            return result[0]  # Return weight
        return None
    except psycopg2.Error as e:
        print(f"[DB-ERROR] Error checking {table_name} for {vehicle_no}: {e}")
        return None

def scrape_vehicle_weights_chhattisgarh(df_input, skip_db_phases=False):
    """
    Vehicle weight lookup and scraping (Chhattisgarh web UI only).
    
    DB phases match scrape_vehicle_weights (default): checkpostmaster, then capacity_vehicle_numbers.
    Web scraping uses the Chhattisgarh browser flow (process_single_vehicle / navigate_to_tax_page).
    
    skip_db_phases: if True, skip DB steps (caller already ran parallel_prefetch_vehicle_db_weights).
    """
    print("=" * 80)
    print("STARTING VEHICLE WEIGHT LOOKUP AND SCRAPING (CHHATTISGARH)")
    print("=" * 80)

    # Make a copy to avoid modifying the original
    df = df_input.copy()

    # Find vehicle number column (flexible naming)
    veh_col = None
    for col in df.columns:
        if 'veh' in col.lower() and 'reg' in col.lower():
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

    # Connect to database
    conn = get_db_connection()
    if not conn:
        print("[ERROR] Could not connect to database")
        return df

    def _cell_is_checkpostmaster_weight(w):
        if pd.isna(w):
            return False
        s = str(w).strip()
        return bool(s) and s.lower() != "nan" and s != "-"

    checkpostmaster_total = 0
    capacity_total = 0

    if not skip_db_phases:
        cursor = conn.cursor()

        print("\n" + "="*80)
        print("STEP 1: Checking checkpostmaster table...")
        print("="*80)

        for idx, row in df.iterrows():
            vehicle_no = normalize_vehicle_number(row[veh_col])
            if not vehicle_no or not is_vehicle_number_eligible(vehicle_no):
                continue
            current_weight = df.at[idx, "Weight"]
            if current_weight and str(current_weight).strip() and str(current_weight) != 'nan':
                continue

            weight = check_vehicle_in_database(cursor, vehicle_no, "checkpostmaster")
            if weight is not None:
                df.at[idx, "Weight"] = str(weight)
                print(f"  [FOUND] {vehicle_no}: Weight = {weight} (from checkpostmaster)")

        checkpostmaster_total = sum(
            1 for idx in df.index if _cell_is_checkpostmaster_weight(df.at[idx, "Weight"])
        )
        print(f"Found {checkpostmaster_total} vehicles in checkpostmaster table")

        print("\n" + "="*80)
        print("STEP 2: Checking capacity_vehicle_numbers table for remaining vehicles...")
        print("="*80)

        for idx, row in df.iterrows():
            current_weight = df.at[idx, "Weight"]
            if current_weight and str(current_weight).strip() and str(current_weight) != 'nan':
                continue

            vehicle_no = normalize_vehicle_number(row[veh_col])
            if not vehicle_no or not is_vehicle_number_eligible(vehicle_no):
                continue

            exists = check_vehicle_in_database(cursor, vehicle_no, "capacity_vehicle_numbers")
            if exists is not None:
                df.at[idx, "Weight"] = "-"
                print(f"  [FOUND] {vehicle_no}: Marked as '-' (found in capacity_vehicle_numbers)")

        capacity_total = sum(
            1 for idx in df.index if str(df.at[idx, "Weight"]).strip() == "-"
        )
        print(f"Found {capacity_total} vehicles in capacity_vehicle_numbers table")
    else:
        print("\n" + "="*80)
        print("STEP 1–2: Skipped (DB phases already done by caller)")
        print("="*80)
        checkpostmaster_total = sum(
            1 for idx in df.index if _cell_is_checkpostmaster_weight(df.at[idx, "Weight"])
        )
        print(f"Found {checkpostmaster_total} vehicles in checkpostmaster table (pre-filled)")
        capacity_total = sum(
            1 for idx in df.index if str(df.at[idx, "Weight"]).strip() == "-"
        )
        print(f"Found {capacity_total} vehicles in capacity_vehicle_numbers table (pre-filled)")

    # STEP 3: Web scrape remaining vehicles (those without weight; excludes capacity "-")
    def is_empty_weight(weight):
        if pd.isna(weight):
            return True
        weight_str = str(weight).strip()
        return weight_str == "" or weight_str == "nan"
    
    # Filter vehicles that need scraping (empty weight)
    remaining_mask = df["Weight"].apply(is_empty_weight)
    remaining_df = df[remaining_mask].copy()
    remaining_count = len(remaining_df)
    
    # Also filter out rows with invalid vehicle numbers
    if remaining_count > 0:
        valid_vehicle_mask = remaining_df[veh_col].apply(
            lambda x: is_vehicle_number_eligible(x)
        )
        remaining_df = remaining_df[valid_vehicle_mask].copy()
        remaining_count = len(remaining_df)
    
    # Debug: Print info if no vehicles to scrape
    if remaining_count == 0:
        print(f"\n[DEBUG] No vehicles to scrape. Checking Weight column values...")
        weight_values = df["Weight"].value_counts()
        print(f"  Weight column value counts:\n{weight_values}")
        print(f"  Total rows in df: {len(df)}")
        print(f"  Weight column sample (first 5): {df['Weight'].head().tolist()}")
        print(f"  Vehicles with empty weight (should be scraped): {df['Weight'].apply(is_empty_weight).sum()}")
    
    # Initialize counters for summary (outside if block)
    scraped_count = 0
    db_added_count = 0
    db_updated_count = 0
    checkpostmaster_added = 0
    checkpostmaster_updated = 0
    capacity_added = 0
    
    print("\n" + "="*80)
    print(f"STEP 3: Web scraping {remaining_count} remaining vehicles...")
    print("="*80)
    
    if remaining_count > 0:
        # Start browser
        driver = setup_driver()
        if not driver:
            print("[ERROR] Could not start browser — skipping web scraping")
            conn.close()
            return df

        wait = WebDriverWait(driver, WAIT_TIME)

        # Navigate to tax page with retry logic
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
                except:
                    pass
                time.sleep(5)
                driver = setup_driver()
                if not driver:
                    break
                wait = WebDriverWait(driver, WAIT_TIME)

        if not nav_success:
            print("[ERROR] Could not navigate to tax page — skipping web scraping")
            driver.quit()
            conn.close()
            return df

        # Process remaining vehicles
        start_time = time.perf_counter()

        for remaining_idx, row in remaining_df.iterrows():
            vehicle_no = normalize_vehicle_number(row[veh_col])
            if not vehicle_no or not is_vehicle_number_eligible(vehicle_no):
                continue

            # Process single vehicle (use remaining_df index)
            driver, wait, success = process_single_vehicle(driver, wait, vehicle_no, remaining_idx, remaining_df)

            if success:
                scraped_count += 1
                weight = remaining_df.at[remaining_idx, "Weight"]
                
                # Update main dataframe using original index
                df.at[remaining_idx, "Weight"] = weight
                
                # Store in database if weight is valid
                if weight and weight not in ["N/A", "Error", "Error - Input Not Found",
                                             "Error - Browser Restart Failed", "Error - Max Retries", "-"]:
                    try:
                        # Check and restore connection if needed
                        conn = check_and_restore_db_connection(conn)
                        if conn is None:
                            continue
                        
                        cursor = conn.cursor()
                        success_insert, message, table_name = insert_vehicle_to_checkpost(cursor, vehicle_no, weight)
                        conn.commit()
                        
                        if success_insert:
                            # Check if it was an update or insert based on message
                            if "Updated" in message:
                                db_updated_count += 1
                                if table_name == "checkpostmaster":
                                    checkpostmaster_updated += 1
                            else:
                                db_added_count += 1
                                if table_name == "checkpostmaster":
                                    checkpostmaster_added += 1
                                elif table_name == "capacity_vehicle_numbers":
                                    capacity_added += 1
                            print(f"[DB-OK] {vehicle_no}: {message}")
                        else:
                            # Even if insert failed (e.g., already exists), log it
                            print(f"[DB-INFO] {vehicle_no}: {message}")
                    except Exception as e:
                        print(f"[DB-ERROR] {vehicle_no}: {e}")
                        try:
                            conn.rollback()
                        except:
                            pass

                print(f"Progress: {scraped_count}/{remaining_count} scraped | {db_added_count} added, {db_updated_count} updated in DB")
            
            time.sleep(1)

        driver.quit()
        
        # Calculate execution time
        elapsed = time.perf_counter() - start_time
        hrs = int(elapsed // 3600)
        mins = int((elapsed % 3600) // 60)
        secs = int(elapsed % 60)
        print(f"\nWeb scraping completed in {hrs:02d}:{mins:02d}:{secs:02d}")

    # Close database connection
    if conn:
        try:
            conn.close()
            print("[DB] Database connection closed")
        except:
            pass

    # Summary
    print("\n" + "="*80)
    print("PROCESSING COMPLETE")
    print("="*80)
    print(f"Total vehicles processed: {len(df)}")
    print(f"Found in checkpostmaster: {checkpostmaster_total}")
    print(f"Found in capacity_vehicle_numbers: {capacity_total}")
    print(f"Web scraped: {scraped_count}")
    print(f"Added to checkpostmaster: {checkpostmaster_added}")
    print(f"Updated in checkpostmaster: {checkpostmaster_updated}")
    print(f"Added to capacity_vehicle_numbers: {capacity_added}")
    print("="*80)

    return df

# Example usage when running standalone
if __name__ == "__main__":
    # Load from Excel
    EXCEL_PATH = "test.xlsx"
    # SHEET_NAME = "Not Found"
    SHEET_NAME = "Sheet1"

    df_not_found = pd.read_excel(EXCEL_PATH, sheet_name=SHEET_NAME, dtype=str)

    print(f"Loaded {len(df_not_found)} vehicles from Excel")

    # Scrape weights and update database immediately
    df_updated = scrape_vehicle_weights_chhattisgarh(df_not_found)

    # Save back to Excel
    with pd.ExcelFile(EXCEL_PATH) as xls:
        sheets_dict = {sheet: pd.read_excel(xls, sheet_name=sheet) for sheet in xls.sheet_names}

    sheets_dict[SHEET_NAME] = df_updated

    with pd.ExcelWriter(EXCEL_PATH, engine='openpyxl') as writer:
        for sheet, data in sheets_dict.items():
            data.to_excel(writer, sheet_name=sheet, index=False)

    print(f"[OK] Results saved back to '{EXCEL_PATH}'")

