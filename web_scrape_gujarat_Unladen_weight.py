"""
Gujarat vehicle weight web scraper.

Takes vehicle numbers as input and fetches two weights from the parivahan.gov.in
Checkpost Tax portal for the state "GUJARAT" using the
"VEHICLE TAX COLLECTION (OTHER STATE)" service:

    1. Gross Vehicle Wt.(In Kg.)  -> input id "txt_laden_wt"
    2. Unladen Wt.(In Kg.)        -> input id "txt_unladen_wt"

The result DataFrame gets two dedicated columns for these weights.

This module is Selenium Grid ready: pass remote_url (e.g.
http://localhost:4444/wd/hub) to scrape_gujarat_weights() to run the browser on
a Docker Selenium Grid node. When remote_url is None a local Chrome is used.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

from vehicle_number_utils import is_vehicle_number_eligible, normalize_vehicle_number
from config import (
    MAX_SELENIUM_GRID_NODES,
    SELENIUM_AUTO_MANAGE_NODES,
    SELENIUM_REMOTE_URL,
)
from selenium_grid_manager import (
    assert_grid_ready,
    split_dataframe as split_df_for_grid,
    start_managed_nodes,
    stop_managed_nodes,
    wait_for_grid_ready,
)

URL = "https://parivahan.gov.in/"
WAIT_TIME = 30
ELEMENT_WAIT = 15

# Selenium Grid toggle:
#   True  -> run the browser on the Docker Selenium Grid (SELENIUM_REMOTE_URL from .env)
#   False -> run a single local Chrome browser
# An explicit remote_url passed to scrape_gujarat_weights() always overrides this.
#
# When grid is used, Chrome nodes are auto-started (SELENIUM_AUTO_MANAGE_NODES) and the
# work is split across up to MAX_SELENIUM_GRID_NODES parallel sessions, matching how
# main_experimental_threading.py handles the grid. On grid failure it falls back to local Chrome.
USE_SELENIUM_GRID = True

# Output column names for the two weights
GROSS_WEIGHT_COLUMN = "Gross Vehicle Wt.(In Kg.)"
UNLADEN_WEIGHT_COLUMN = "Unladen Wt.(In Kg.)"

STATE_NAME = "GUJARAT"
SERVICE_NAME = "VEHICLE TAX COLLECTION (OTHER STATE)"

# Per-thread remote URL so parallel Selenium Grid sessions each use their own node.
_thread_remote_url = threading.local()


def setup_driver(remote_url=None):
    """Create a Chrome WebDriver (local or Selenium Grid remote)."""
    if remote_url is None:
        remote_url = getattr(_thread_remote_url, "value", None)
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

        if remote_url:
            from IHMCL_bot_selenium import ensure_grid_ready

            ensure_grid_ready(remote_url)
            driver = webdriver.Remote(command_executor=remote_url, options=options)
            print(f"Browser connected to Selenium Grid: {remote_url}")
        else:
            # Try to create local driver with service
            try:
                driver = webdriver.Chrome(options=options)
            except Exception as e1:
                print(f"  [WARN] First attempt failed: {e1}")
                print("  [INFO] Retrying with explicit service configuration...")
                try:
                    service = Service()
                    driver = webdriver.Chrome(service=service, options=options)
                except Exception as e2:
                    print(f"  [WARN] Second attempt failed: {e2}")
                    print("  [INFO] Retrying with minimal options...")
                    minimal_options = webdriver.ChromeOptions()
                    minimal_options.add_argument('--no-sandbox')
                    minimal_options.add_argument('--disable-dev-shm-usage')
                    driver = webdriver.Chrome(options=minimal_options)

        try:
            driver.maximize_window()
        except Exception as e:
            print(f"  [WARNING] Could not maximize window: {e}")

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
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
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


def select_state_gujarat(driver, wait):
    """Select Gujarat from the state selection interface."""
    for attempt in range(3):
        try:
            print(f"Selecting {STATE_NAME} state (attempt {attempt+1})")
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

            gujarat_selectors = [
                f"//div[contains(text(), '{STATE_NAME}')]",
                f"//span[contains(text(), '{STATE_NAME}')]",
                f"//a[contains(text(), '{STATE_NAME}')]",
                f"//li[contains(text(), '{STATE_NAME}')]",
                f"//option[contains(text(), '{STATE_NAME}')]",
                f"//*[contains(text(), '{STATE_NAME}') and contains(@class, 'option')]",
                f"//*[contains(text(), '{STATE_NAME}')]"
            ]
            gujarat_element = None
            for selector in gujarat_selectors:
                try:
                    gujarat_element = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    print(f"Found {STATE_NAME} option with: {selector}")
                    break
                except:
                    continue

            if not gujarat_element:
                print(f"Could not find {STATE_NAME} option")
                return False

            driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});", gujarat_element)
            time.sleep(1)
            print(f"Clicking {STATE_NAME}...")
            gujarat_element.click()
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

    print(f"Failed to select {STATE_NAME} after all attempts")
    return False


def select_service(driver, wait):
    """Select the VEHICLE TAX COLLECTION (OTHER STATE) service."""
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

            print(f"Waiting for {SERVICE_NAME} option...")
            service_option_xpath = f"//li[contains(@data-label, '{SERVICE_NAME}')]"
            service_option = wait.until(EC.element_to_be_clickable((By.XPATH, service_option_xpath)))
            print(f"Selecting {SERVICE_NAME}...")
            service_option.click()
            time.sleep(2)

            print(f"Selected service: {SERVICE_NAME}")
            print("Waiting for Go button...")
            go_button_xpath = "//button[.//span[contains(text(), 'Go')]]"
            go_button = wait.until(EC.element_to_be_clickable((By.XPATH, go_button_xpath)))
            print("Clicking Go button...")
            go_button.click()

            print("Waiting for vehicle entry page...")
            time.sleep(5)

            vehicle_input_xpath = "//input[@type='text' and @maxlength='10' and not(@id='mobileno')]"
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
    """Navigate from parivahan.gov.in to the Gujarat tax collection vehicle entry page."""
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

        if not select_state_gujarat(driver, wait):
            print(f"Failed to select {STATE_NAME} state")
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


def _read_weight_field(driver, field_id, label_keywords):
    """Read a single weight input value by id, with label-based fallbacks."""
    weight = "0"
    try:
        try:
            weight_element = driver.find_element(By.ID, field_id)
            weight = weight_element.get_attribute('value') or "0"
            print(f"  {field_id}: {weight}")
        except Exception as e:
            print(f"  Could not find {field_id} by ID: {e}")
            # Fallback: locate filled inputs and match against nearby label text
            try:
                all_inputs = driver.find_elements(By.XPATH, "//input[contains(@class, 'ui-state-filled')]")
                for inp in all_inputs:
                    value = inp.get_attribute('value') or ''
                    if not (value and value.isdigit() and int(value) > 0):
                        continue
                    try:
                        parent_text = inp.find_element(
                            By.XPATH, "./ancestor::div[contains(@class,'ui-grid-col')][1]"
                        ).text.lower()
                    except:
                        parent_text = ""
                    if any(kw in parent_text for kw in label_keywords):
                        weight = value
                        print(f"  Found {field_id} via label text: {weight}")
                        break
            except Exception as e2:
                print(f"  Error in fallback for {field_id}: {e2}")
    except Exception as e:
        print(f"  Error reading {field_id}: {e}")
    return weight


def get_vehicle_weights(driver):
    """Extract both Gross Vehicle Wt. and Unladen Wt. Returns (gross, unladen)."""
    print("Getting Gross Vehicle Wt. and Unladen Wt. ...")
    gross_weight = _read_weight_field(driver, "txt_laden_wt", ["gross", "laden", "gvw"])
    unladen_weight = _read_weight_field(driver, "txt_unladen_wt", ["unladen"])
    print(f"Gross Vehicle Wt.: {gross_weight} | Unladen Wt.: {unladen_weight}")
    return gross_weight, unladen_weight


def restart_browser_and_continue(driver):
    """Restart browser and re-navigate to the tax page."""
    print("\n[RESTART] RESTARTING BROWSER...")
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


def _set_weights(df, idx, gross, unladen):
    df.at[idx, GROSS_WEIGHT_COLUMN] = gross
    df.at[idx, UNLADEN_WEIGHT_COLUMN] = unladen


def process_single_vehicle(driver, wait, vehicle_no, idx, df):
    """Process a single vehicle - extract both weights."""
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

            vehicle_input_xpath = "//input[@type='text' and @maxlength='10' and not(@id='mobileno')]"

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
                    _set_weights(df, idx, "Error - Input Not Found", "Error - Input Not Found")
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
                _set_weights(df, idx, "N/A", "N/A")
                print("[OK] Marked as N/A (no data available)")
                return driver, wait, True

            elif data_appeared:
                gross_weight, unladen_weight = get_vehicle_weights(driver)
                _set_weights(df, idx, gross_weight, unladen_weight)
                print(f"[OK] Weights extracted — Gross: {gross_weight}, Unladen: {unladen_weight}")
                return driver, wait, True

            else:
                # No data loaded after attempts - restart browser and retry
                print("[WARNING] No data loaded after attempts - restarting browser...")
                new_driver, new_wait = restart_browser_and_continue(driver)
                if new_driver:
                    print(f"[RETRY] Re-processing vehicle {vehicle_no} after browser restart...")
                    driver = new_driver
                    wait = new_wait
                    return process_single_vehicle(driver, wait, vehicle_no, idx, df)
                else:
                    print(f"[ERROR] Could not restart browser for {vehicle_no}")
                    _set_weights(df, idx, "Error - Browser Restart Failed", "Error - Browser Restart Failed")
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
                _set_weights(df, idx, "Error", "Error")
                return driver, wait, False

    _set_weights(df, idx, "Error - Max Retries", "Error - Max Retries")
    return driver, wait, False


def scrape_gujarat_weights(df_input, remote_url=None, use_selenium_grid=None):
    """
    Scrape Gross Vehicle Wt. and Unladen Wt. for vehicles in df_input (Gujarat web UI).

    use_selenium_grid: True  -> use Selenium Grid at SELENIUM_REMOTE_URL
                       False -> use a local Chrome browser
                       None  -> fall back to the module-level USE_SELENIUM_GRID flag
    remote_url: explicit Selenium Grid URL (e.g. http://localhost:4444/wd/hub);
                when set it overrides use_selenium_grid and forces grid usage.

    Returns df with two added columns: GROSS_WEIGHT_COLUMN and UNLADEN_WEIGHT_COLUMN.
    """
    if use_selenium_grid is None:
        use_selenium_grid = USE_SELENIUM_GRID

    # An explicit remote_url always forces grid usage.
    use_grid = use_selenium_grid or (remote_url is not None)
    grid_url = remote_url or SELENIUM_REMOTE_URL

    print("=" * 80)
    print("STARTING GUJARAT VEHICLE WEIGHT SCRAPING (Gross + Unladen)")
    print("=" * 80)

    if not use_grid:
        print("Web scrape mode: local Chrome (single browser)")
        return _scrape_chunk(df_input, None)

    print(f"Web scrape mode: Selenium Grid ({grid_url}, auto_nodes={SELENIUM_AUTO_MANAGE_NODES})")
    return _run_grid_scrape(df_input, grid_url)


def _scrape_chunk(chunk_df, remote_url):
    """Run the scraping flow for one chunk with a per-thread remote URL (None = local Chrome)."""
    _thread_remote_url.value = remote_url
    try:
        return _scrape_gujarat_weights_impl(chunk_df)
    finally:
        _thread_remote_url.value = None


def _run_grid_scrape(df_input, remote_url):
    """
    Auto-start Chrome nodes (if enabled), split the work across parallel Grid sessions,
    merge results, and fall back to local Chrome on failure. Mirrors the approach in
    main_experimental_threading._run_selenium_grid_scrape.
    """
    chunks = split_df_for_grid(df_input, MAX_SELENIUM_GRID_NODES)
    if not chunks:
        return df_input

    print(f"  [SELENIUM GRID] {len(chunks)} chunk(s) -> {remote_url}", flush=True)

    managed_nodes = []
    merged = None
    grid_error = None

    try:
        if SELENIUM_AUTO_MANAGE_NODES:
            managed_nodes = start_managed_nodes(len(chunks))
            wait_for_grid_ready(remote_url)
        else:
            assert_grid_ready(remote_url)

        result_frames = []
        failures = []
        with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
            future_to_chunk = {
                executor.submit(_scrape_chunk, chunk, remote_url): chunk_id
                for chunk_id, chunk in enumerate(chunks, start=1)
            }
            for future in as_completed(future_to_chunk):
                chunk_id = future_to_chunk[future]
                try:
                    result_frames.append(future.result())
                    print(f"  [SELENIUM GRID] chunk {chunk_id}/{len(chunks)} completed", flush=True)
                except Exception as exc:
                    failures.append((chunk_id, exc))
                    print(f"  [SELENIUM GRID] chunk {chunk_id} failed: {exc}", flush=True)

        if result_frames:
            merged = df_input.copy()
            for col in (GROSS_WEIGHT_COLUMN, UNLADEN_WEIGHT_COLUMN):
                if col not in merged.columns:
                    merged[col] = ""
            for frame in result_frames:
                for col in frame.columns:
                    merged.loc[frame.index, col] = frame[col]

        if not result_frames:
            grid_error = f"All Selenium Grid chunks failed: {failures}"
        elif failures:
            grid_error = f"{len(failures)} Selenium Grid chunk(s) failed: {failures}"

    except Exception as exc:
        grid_error = str(exc)
        print(f"  [SELENIUM GRID] error: {exc}", flush=True)
    finally:
        if managed_nodes:
            stop_managed_nodes(managed_nodes)

    if grid_error:
        print(f"  [SELENIUM GRID] falling back to local Chrome... ({grid_error})", flush=True)
        df_for_local = merged if merged is not None else df_input
        return _scrape_chunk(df_for_local, None)

    return merged


def _scrape_gujarat_weights_impl(df_input):
    df = df_input.copy()

    # Find vehicle number column (flexible naming: contains 'veh' and 'reg')
    veh_col = None
    for col in df.columns:
        if 'veh' in col.lower() and 'reg' in col.lower():
            veh_col = col
            break

    # Fallback: any column containing 'vehicle'
    if veh_col is None:
        for col in df.columns:
            if 'vehicle' in col.lower():
                veh_col = col
                break

    if veh_col is None:
        print("[ERROR] Could not find vehicle number column in dataframe")
        return df

    if GROSS_WEIGHT_COLUMN not in df.columns:
        df[GROSS_WEIGHT_COLUMN] = ""
    if UNLADEN_WEIGHT_COLUMN not in df.columns:
        df[UNLADEN_WEIGHT_COLUMN] = ""

    df[veh_col] = df[veh_col].apply(normalize_vehicle_number)
    invalid_vehicle_count = int((~df[veh_col].apply(is_vehicle_number_eligible)).sum())
    if invalid_vehicle_count:
        print(
            f"[INFO] Skipping {invalid_vehicle_count} vehicle numbers outside eligible length range "
            f"(allowed lengths: 8, 9, 10)"
        )

    print(f"Total vehicles to process: {len(df)}")
    if len(df) == 0:
        print("No vehicles to process.")
        return df

    # Only scrape valid vehicle numbers
    valid_mask = df[veh_col].apply(is_vehicle_number_eligible)
    remaining_df = df[valid_mask]
    remaining_count = len(remaining_df)

    print("\n" + "=" * 80)
    print(f"Web scraping {remaining_count} vehicles...")
    print("=" * 80)

    if remaining_count == 0:
        print("No valid vehicles to scrape.")
        return df

    driver = setup_driver()
    if not driver:
        print("[ERROR] Could not start browser — skipping web scraping")
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
            except:
                pass
            time.sleep(5)
            driver = setup_driver()
            if not driver:
                break
            wait = WebDriverWait(driver, WAIT_TIME)

    if not nav_success:
        print("[ERROR] Could not navigate to tax page — skipping web scraping")
        try:
            driver.quit()
        except:
            pass
        return df

    start_time = time.perf_counter()
    scraped_count = 0

    for idx, row in remaining_df.iterrows():
        vehicle_no = normalize_vehicle_number(row[veh_col])
        if not vehicle_no or not is_vehicle_number_eligible(vehicle_no):
            continue

        driver, wait, success = process_single_vehicle(driver, wait, vehicle_no, idx, df)
        if success:
            scraped_count += 1
        print(f"Progress: {scraped_count}/{remaining_count} scraped")
        time.sleep(1)

    try:
        driver.quit()
    except:
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
    print("=" * 80)

    return df


# Example usage when running standalone
if __name__ == "__main__":
    EXCEL_PATH = "Kota railway project - Checkpost run.xlsx"
    SHEET_NAME = "Sheet2"

    df_vehicles = pd.read_excel(EXCEL_PATH, sheet_name=SHEET_NAME, dtype=str)
    print(f"Loaded {len(df_vehicles)} vehicles from Excel")

    # Toggle USE_SELENIUM_GRID at the top of this file (or pass use_selenium_grid=True)
    # to run on the Selenium Grid at SELENIUM_REMOTE_URL instead of local Chrome.
    df_updated = scrape_gujarat_weights(df_vehicles, use_selenium_grid=USE_SELENIUM_GRID)

    with pd.ExcelFile(EXCEL_PATH) as xls:
        sheets_dict = {sheet: pd.read_excel(xls, sheet_name=sheet) for sheet in xls.sheet_names}

    sheets_dict[SHEET_NAME] = df_updated

    with pd.ExcelWriter(EXCEL_PATH, engine='openpyxl') as writer:
        for sheet, data in sheets_dict.items():
            data.to_excel(writer, sheet_name=sheet, index=False)

    print(f"[OK] Results saved back to '{EXCEL_PATH}'")
