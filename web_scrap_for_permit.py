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
        # Removed excludeSwitches and useAutomationExtension as they might interfere with ChromeDriver
        
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

def get_vehicle_details(driver, wait):
    """
    Extract permit details from the website.
    
    Extracts the following permit fields:
    - Permit Type
    - Permit/Authorization No
    - Permit Validity
    
    Returns a dictionary with extracted details. Empty values default to "none".
    """
    details = {}
    
    try:
        # Helper function to safely extract value from element with wait
        def extract_value(selectors, is_span=False, default="none", timeout=5):
            """
            Extract value from element using multiple selector strategies with wait.
            
            Args:
                selectors: List of selectors to try (can be IDs, XPaths, or dicts with 'by' and 'value')
                is_span: True if element is a span (use text), False if input (use value)
                default: Default value if element not found or empty
                timeout: Maximum time to wait for element
            """
            for selector in selectors:
                try:
                    # Handle different selector types
                    if isinstance(selector, dict):
                        # Dict format: {'by': By.ID, 'value': 'element_id'}
                        by_type = selector['by']
                        selector_value = selector['value']
                        element = WebDriverWait(driver, timeout).until(
                            EC.presence_of_element_located((by_type, selector_value))
                        )
                    elif selector.startswith("//") or selector.startswith("(//"):
                        # XPath selector
                        element = WebDriverWait(driver, timeout).until(
                            EC.presence_of_element_located((By.XPATH, selector))
                        )
                    else:
                        # Default: Try as ID first, then as name
                        try:
                            element = WebDriverWait(driver, timeout).until(
                                EC.presence_of_element_located((By.ID, selector))
                            )
                        except:
                            # If ID fails, try as name attribute
                            element = WebDriverWait(driver, timeout).until(
                                EC.presence_of_element_located((By.NAME, selector))
                            )
                    
                    # Extract value
                    if is_span:
                        value = element.text.strip() if element.text else ""
                    else:
                        value = element.get_attribute('value') or ""
                    
                    if value:
                        return value
                except Exception as e:
                    continue  # Try next selector
            
            return default
        
        # Wait briefly for page to fully load permit details
        time.sleep(0.5)

        # Extract Permit Validity
        permit_validity_selectors = [
            "//div[.//span[normalize-space()='Permit Validity']]//input[contains(@class, 'hasDatepicker')]",
            "//label[.//span[normalize-space()='Permit Validity']]/following::input[contains(@class, 'hasDatepicker')][1]",
            "//input[@id='j_idt540_input']",
        ]
        permit_validity = extract_value(permit_validity_selectors, is_span=False, timeout=3)
        details['Permit Validity'] = permit_validity
        print(f"Permit Validity: {permit_validity}")
        
        # Extract Permit Number
        permit_no_selectors = [
            "txt_permit_no",
            {'by': By.NAME, 'value': 'txt_permit_no'},
            "//input[@id='txt_permit_no']",
        ]
        permit_no = extract_value(permit_no_selectors, is_span=False, timeout=3)
        details['Permit/Authorization No'] = permit_no
        print(f"Permit Number: {permit_no}")
        
        # Extract Permit Type (span element - use text)
        permit_type_selectors = [
            "permit_type_label",
            "//span[@id='permit_type_label']",
            "//div[.//span[normalize-space()='Permit Type']]//span[contains(@class, 'ui-selectonemenu-label')]",
        ]
        permit_type = extract_value(permit_type_selectors, is_span=True, timeout=3)
        details['Permit Type'] = permit_type
        print(f"Permit Type: {permit_type}")
        
    except Exception as e:
        print(f"Error in get_vehicle_details: {e}")
        import traceback
        print(traceback.format_exc())
        details['Permit Type'] = "none"
        details['Permit/Authorization No'] = "none"
        details['Permit Validity'] = "none"
    
    return details

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


def process_single_vehicle(driver, wait, vehicle_no, idx, df):
    """Process a single vehicle - extract all available details."""
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
                    # Mark all permit columns as "none"
                    permit_fields = ['Permit Type', 'Permit/Authorization No', 'Permit Validity']
                    for field in permit_fields:
                        if field not in df.columns:
                            df[field] = ""
                        df.at[idx, field] = "none"
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
                        print("Popup detected - clicking OK to dismiss")
                        # Click OK to dismiss popup
                        ok_button_xpath = "//div[contains(@class,'ui-dialog')]//button[.//span[contains(text(),'OK') or contains(text(),'Ok')]]"
                        safe_click(driver, wait, ok_button_xpath, "OK button on popup")
                        time.sleep(1)  # Wait for popup to close
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

            # Always try to extract data - popup has been dismissed if it appeared
            # Extract all vehicle details
            details = get_vehicle_details(driver, wait)
            
            # Update dataframe with extracted details
            for key, value in details.items():
                if key not in df.columns:
                    df[key] = ""
                # Use "none" if value is empty, otherwise use the extracted value
                df.at[idx, key] = value if value else "none"
            
            print(f"[OK] Details extracted: {list(details.keys())}")
            return driver, wait, True

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
                permit_fields = ['Permit Type', 'Permit/Authorization No', 'Permit Validity']
                for field in permit_fields:
                    if field not in df.columns:
                        df[field] = ""
                    df.at[idx, field] = "none"
                return driver, wait, False

    # Mark all permit columns as "none" on max retries
    permit_fields = ['Permit Type', 'Permit/Authorization No', 'Permit Validity']
    for field in permit_fields:
        if field not in df.columns:
            df[field] = ""
        df.at[idx, field] = "none"
    return driver, wait, False

def scrape_vehicle_details_for_permit(df_input):
    print("=" * 80)
    print("STARTING VEHICLE DETAILS SCRAPING")
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

    print(f"Total vehicles to process: {len(df)}")

    if len(df) == 0:
        print("No vehicles to process.")
        return df

    # Initialize permit detail columns
    permit_fields = ['Permit Type', 'Permit/Authorization No', 'Permit Validity']
    for field in permit_fields:
        if field not in df.columns:
            df[field] = ""
    print("Initialized permit columns: " + ", ".join(permit_fields))

    df[veh_col] = df[veh_col].apply(normalize_vehicle_number)
    invalid_vehicle_mask = ~df[veh_col].apply(is_vehicle_number_eligible)
    invalid_vehicle_count = int(invalid_vehicle_mask.sum())
    if invalid_vehicle_count:
        print(
            f"[INFO] Skipping {invalid_vehicle_count} vehicle numbers outside eligible length range "
            f"after cleanup (allowed lengths: 8, 9, 10)"
        )
        for idx in df[invalid_vehicle_mask].index:
            for field in permit_fields:
                df.at[idx, field] = "none"
    
    # Start browser
    driver = setup_driver()
    if not driver:
        print("[ERROR] Could not start browser — aborting")
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
        print("[ERROR] Could not navigate to tax page — aborting")
        driver.quit()
        return df

    # Process all vehicles
    start_time = time.perf_counter()
    scraped_count = 0

    for idx, row in df.iterrows():
        vehicle_no = normalize_vehicle_number(row[veh_col])
        if not vehicle_no or not is_vehicle_number_eligible(vehicle_no):
            # Mark skipped rows with "none" for consistency
            permit_fields = ['Permit Type', 'Permit/Authorization No', 'Permit Validity']
            for field in permit_fields:
                if field not in df.columns:
                    df[field] = ""
                df.at[idx, field] = "none"
            continue

        # Process single vehicle
        driver, wait, success = process_single_vehicle(driver, wait, vehicle_no, idx, df)

        if success:
            scraped_count += 1
            print(f"Progress: {scraped_count}/{len(df)} scraped")
        
        time.sleep(1)

    driver.quit()
    
    # Calculate execution time
    elapsed = time.perf_counter() - start_time
    hrs = int(elapsed // 3600)
    mins = int((elapsed % 3600) // 60)
    secs = int(elapsed % 60)
    print(f"\nWeb scraping completed in {hrs:02d}:{mins:02d}:{secs:02d}")

    # Summary
    print("\n" + "="*80)
    print("PROCESSING COMPLETE")
    print("="*80)
    print(f"Total vehicles processed: {len(df)}")
    print(f"Successfully scraped: {scraped_count}")
    print("="*80)

    return df


if __name__ == "__main__":
    # Load from Excel - read first sheet only
    EXCEL_PATH = "test_vehicles_permit.xlsx"

    # Read the first sheet (sheet_name=0 means first sheet)
    df_not_found = pd.read_excel(EXCEL_PATH, sheet_name=0, dtype=str)

    print(f"Loaded {len(df_not_found)} vehicles from Excel (first sheet)")

    # Scrape vehicle details
    df_updated = scrape_vehicle_details_for_permit(df_not_found)

    # Save back to Excel - update first sheet only
    with pd.ExcelFile(EXCEL_PATH) as xls:
        first_sheet_name = xls.sheet_names[0]
        sheets_dict = {sheet: pd.read_excel(xls, sheet_name=sheet) for sheet in xls.sheet_names}

    # Update the first sheet with scraped data
    sheets_dict[first_sheet_name] = df_updated

    with pd.ExcelWriter(EXCEL_PATH, engine='openpyxl') as writer:
        for sheet, data in sheets_dict.items():
            data.to_excel(writer, sheet_name=sheet, index=False)

    print(f"[OK] Results saved back to '{EXCEL_PATH}' (first sheet: '{first_sheet_name}')")
