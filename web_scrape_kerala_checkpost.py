"""
Kerala checkpost tax web scraper.

Scrapes vehicle details from parivahan.gov.in Checkpost Tax portal for state
"KERALA" using the "VEHICLE TAX COLLECTION (OTHER STATE)" service.

Extracted fields (see reference.html):
    1. Gross Vehicle Weight(In Kg.)  -> input id "txt_laden_wt"
    2. Unladen Wt(In Kg.)            -> input id "txt_distance"
    3. Vehicle Type                  -> selected dropdown option / label (e.g. GOODS VEHICLE)
    4. Vehicle Class                 -> selected dropdown option / label (e.g. TRAILER)

Does NOT look up vehicles in checkpostmaster before scraping — every eligible
vehicle is scraped. After a successful scrape, Gross Vehicle Weight is upserted
into checkpostmaster (explicit Added / Updated log).

Selenium Grid:
    USE_SELENIUM_GRID = True/False toggles parallel Grid vs local Chrome.
    On Grid failure, falls back to a single local Chrome session.

Concurrent progress:
    Each Grid node writes its own chunk_XX.xlsx (no shared-file conflicts).
    Progress is saved after every vehicle. At the end all chunks are merged
    into merged_output.xlsx (and returned as one DataFrame).
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg2
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

from config import (
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
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
from vehicle_number_utils import is_vehicle_number_eligible, normalize_vehicle_number

URL = "https://parivahan.gov.in/"
WAIT_TIME = 30
ELEMENT_WAIT = 15

# Selenium Grid toggle:
#   True  -> Docker Selenium Grid (SELENIUM_REMOTE_URL), parallel Chrome nodes
#   False -> single local Chrome browser
# Explicit remote_url passed to scrape_kerala_checkpost() always overrides this.
# On grid failure the scraper falls back to local Chrome.
USE_SELENIUM_GRID = True

STATE_NAME = "KERALA"
SERVICE_NAME = "VEHICLE TAX COLLECTION (OTHER STATE)"

GROSS_WEIGHT_COLUMN = "Gross Vehicle Weight(In Kg.)"
UNLADEN_WEIGHT_COLUMN = "Unladen Wt(In Kg.)"
VEHICLE_TYPE_COLUMN = "Vehicle Type"
VEHICLE_CLASS_COLUMN = "Vehicle Class"

DETAIL_COLUMNS = [
    GROSS_WEIGHT_COLUMN,
    UNLADEN_WEIGHT_COLUMN,
    VEHICLE_TYPE_COLUMN,
    VEHICLE_CLASS_COLUMN,
]

# Column used in per-node progress Excel files so merge can restore row positions
ORIG_INDEX_COLUMN = "_orig_index"

INVALID_WEIGHT_VALUES = {
    "",
    "N/A",
    "Error",
    "Error - Input Not Found",
    "Error - Browser Restart Failed",
    "Error - Max Retries",
    "-",
    "0",
    "none",
    "nan",
}

_thread_remote_url = threading.local()


def get_db_connection():
    """Create and return a PostgreSQL database connection."""
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
        )
        print("[DB] Connection established")
        return conn
    except psycopg2.Error as e:
        print(f"[DB-ERROR] Error connecting to database: {e}")
        return None


def check_and_restore_db_connection(conn):
    """Reconnect if the DB connection was closed."""
    if conn is None:
        print("[DB] Connection is None, creating new connection...")
        return get_db_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
        return conn
    except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
        print(f"[DB-WARNING] Connection closed ({e}), reconnecting...")
        try:
            conn.close()
        except Exception:
            pass
        new_conn = get_db_connection()
        if new_conn:
            print("[DB] Successfully reconnected to database")
            return new_conn
        print("[DB-ERROR] Could not reconnect to database")
        return None
    except Exception as e:
        print(f"[DB-ERROR] Error checking connection: {e}")
        return conn


def setup_driver(remote_url=None):
    """Create a Chrome WebDriver (local or Selenium Grid remote)."""
    if remote_url is None:
        remote_url = getattr(_thread_remote_url, "value", None)
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--disable-images")
        options.add_argument("--blink-settings=imagesEnabled=false")
        options.add_argument("--disable-gpu")
        options.page_load_strategy = "normal"
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-features=TranslateUI")
        options.add_argument("--remote-allow-origins=*")
        options.add_argument("--disable-web-security")
        options.add_argument("--allow-running-insecure-content")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2,
            "profile.default_content_settings.popups": 0,
        }
        options.add_experimental_option("prefs", prefs)

        if remote_url:
            from IHMCL_bot_selenium import ensure_grid_ready

            ensure_grid_ready(remote_url)
            driver = webdriver.Remote(command_executor=remote_url, options=options)
            print(f"Browser connected to Selenium Grid: {remote_url}")
        else:
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
                    minimal_options.add_argument("--no-sandbox")
                    minimal_options.add_argument("--disable-dev-shm-usage")
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
            except Exception:
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
        popup_text_xpath = (
            "//span[contains(@class, 'english') and contains(text(), 'Update Your Mobile Number')]"
        )
        popup_text = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located((By.XPATH, popup_text_xpath))
        )
        if popup_text:
            close_button_xpath = (
                "//button[contains(@class, 'btn-close') and contains(@class, 'position-absolute')]"
            )
            close_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, close_button_xpath))
            )
            close_button.click()
            print("Mobile number popup closed")
            time.sleep(2)
            return True
    except TimeoutException:
        return False
    except Exception as e:
        print(f"Error handling mobile popup: {e}")
        return False


def select_state_kerala(driver, wait):
    """Select Kerala from the state selection interface."""
    for attempt in range(3):
        try:
            print(f"Selecting {STATE_NAME} state (attempt {attempt + 1})")
            state_selectors = [
                "//div[contains(text(), 'Select State Name')]",
                "//span[contains(text(), 'Select State Name')]",
                "//a[contains(text(), 'Select State Name')]",
                "//button[contains(text(), 'Select State Name')]",
                "//*[contains(text(), 'Select State Name') and contains(@class, 'select')]",
                "//*[contains(text(), 'Select State Name')]",
            ]
            state_element = None
            for selector in state_selectors:
                try:
                    state_element = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    print(f"Found state selection element with: {selector}")
                    break
                except Exception:
                    continue

            if not state_element:
                print("Could not find state selection element")
                return False

            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                state_element,
            )
            time.sleep(1)
            print("Clicking state selection element...")
            state_element.click()
            time.sleep(2)

            kerala_selectors = [
                f"//div[contains(text(), '{STATE_NAME}')]",
                f"//span[contains(text(), '{STATE_NAME}')]",
                f"//a[contains(text(), '{STATE_NAME}')]",
                f"//li[contains(text(), '{STATE_NAME}')]",
                f"//option[contains(text(), '{STATE_NAME}')]",
                f"//*[contains(text(), '{STATE_NAME}') and contains(@class, 'option')]",
                f"//*[contains(text(), '{STATE_NAME}')]",
            ]
            kerala_element = None
            for selector in kerala_selectors:
                try:
                    kerala_element = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    print(f"Found {STATE_NAME} option with: {selector}")
                    break
                except Exception:
                    continue

            if not kerala_element:
                print(f"Could not find {STATE_NAME} option")
                return False

            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                kerala_element,
            )
            time.sleep(1)
            print(f"Clicking {STATE_NAME}...")
            kerala_element.click()
            time.sleep(3)

            service_heading_xpath = "//h3[@class='top-space' and contains(text(), 'Service Name')]"
            try:
                WebDriverWait(driver, 10).until(
                    EC.visibility_of_element_located((By.XPATH, service_heading_xpath))
                )
                print("State selection successful - Service Name section found")
                return True
            except Exception:
                print("Service Name section not found yet")
                print("Retrying state selection...")
                time.sleep(2)
        except Exception as e:
            print(f"Error selecting state on attempt {attempt + 1}: {e}")
            time.sleep(2)

    print(f"Failed to select {STATE_NAME} after all attempts")
    return False


def select_service(driver, wait):
    """Select VEHICLE TAX COLLECTION (OTHER STATE) service."""
    for attempt in range(3):
        try:
            print(f"\nSelecting service (attempt {attempt + 1})")
            print("Waiting for service dropdown...")
            service_dropdown_xpath = (
                "//span[contains(@class,'ui-selectonemenu-label') and "
                "contains(text(),'---Select Service Name---')]"
            )
            service_dropdown = wait.until(
                EC.element_to_be_clickable((By.XPATH, service_dropdown_xpath))
            )
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});",
                service_dropdown,
            )
            time.sleep(1)
            print("Clicking service dropdown...")
            service_dropdown.click()
            time.sleep(2)

            print(f"Waiting for {SERVICE_NAME} option...")
            service_option_xpath = f"//li[contains(@data-label, '{SERVICE_NAME}')]"
            service_option = wait.until(
                EC.element_to_be_clickable((By.XPATH, service_option_xpath))
            )
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

            vehicle_input_xpath = (
                "//input[@type='text' and @maxlength='10' and not(@id='mobileno')]"
            )
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.XPATH, vehicle_input_xpath))
                )
                print("Successfully loaded vehicle entry page")
                return True
            except Exception:
                print("Vehicle entry page not loaded, retrying...")
                driver.execute_script("location.reload()")
                time.sleep(3)
        except Exception as e:
            print(f"Error selecting service: {e}")
            try:
                driver.execute_script("location.reload()")
            except Exception:
                pass
            time.sleep(3)

    print("Failed to select service after multiple attempts")
    return False


def navigate_to_tax_page(driver, wait):
    """Navigate from parivahan.gov.in to the Kerala tax collection vehicle entry page."""
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
        online_services_xpath = (
            "//a[@id='Online' and contains(@class, 'parent-link-with-submenu')]"
        )
        online_services = wait.until(
            EC.element_to_be_clickable((By.XPATH, online_services_xpath))
        )
        ActionChains(driver).move_to_element(online_services).pause(2).perform()
        time.sleep(3)

        print("Clicking Checkpost Tax...")
        checkpost_tax_xpath = (
            "//a[@href='/en/node/579' and contains(@class, 'second-child-menu')]"
        )
        checkpost_tax = wait.until(EC.element_to_be_clickable((By.XPATH, checkpost_tax_xpath)))
        checkpost_tax.click()

        print("Waiting for Checkpost Tax page to load...")
        checkpost_title_xpath = (
            "//span[@class='field field--name-title field--type-string "
            "field--label-hidden' and contains(text(), 'Checkpost Tax')]"
        )
        wait.until(EC.visibility_of_element_located((By.XPATH, checkpost_title_xpath)))
        print("Checkpost Tax page loaded successfully")

        if not select_state_kerala(driver, wait):
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


def _read_input_field(driver, field_ids, label_keywords):
    """
    Read an input value by id(s), then label-based fallbacks.

    field_ids: str or list of candidate element ids (quiet if missing).
    """
    if isinstance(field_ids, str):
        field_ids = [field_ids]

    for field_id in field_ids:
        try:
            element = driver.find_element(By.ID, field_id)
            value = (element.get_attribute("value") or "").strip()
            if value:
                print(f"  {field_id}: {value}")
                return value
        except Exception:
            # Missing id is common when details have not loaded yet — stay quiet
            continue

    # Label-based JS lookup inside the Kerala panel
    try:
        value = driver.execute_script(
            """
            var keywords = arguments[0];
            var root = document.getElementById('kltaxcollection') || document;
            function norm(s) { return (s || '').replace(/\\s+/g, ' ').trim().toLowerCase(); }
            var labels = root.querySelectorAll('span.ui-outputlabel-label');
            for (var i = 0; i < labels.length; i++) {
              var label = norm(labels[i].textContent);
              var matched = false;
              for (var k = 0; k < keywords.length; k++) {
                if (label.indexOf(String(keywords[k]).toLowerCase()) !== -1) { matched = true; break; }
              }
              if (!matched) continue;
              var col = labels[i].closest('[class*="ui-grid-col"]');
              if (!col) continue;
              var input = col.querySelector('input');
              if (!input) continue;
              var val = (input.value || '').trim();
              if (val) return val;
            }
            return '';
            """,
            list(label_keywords),
        )
        value = (value or "").strip()
        if value:
            print(f"  input via label {label_keywords}: {value}")
            return value
    except Exception:
        pass

    try:
        all_inputs = driver.find_elements(
            By.XPATH, "//input[contains(@class, 'ui-state-filled')]"
        )
        for inp in all_inputs:
            candidate = (inp.get_attribute("value") or "").strip()
            if not candidate:
                continue
            try:
                parent_text = inp.find_element(
                    By.XPATH, "./ancestor::div[contains(@class,'ui-grid-col')][1]"
                ).text.lower()
            except Exception:
                parent_text = ""
            if any(kw in parent_text for kw in label_keywords):
                print(f"  input via filled fallback: {candidate}")
                return candidate
    except Exception:
        pass

    return "0"


def _is_placeholder_select_value(text):
    if not text:
        return True
    lowered = text.strip().lower()
    return (
        lowered.startswith("---select")
        or lowered in {"-1", "none", "nan", "n/a", "select", ""}
    )


def _laden_weight_value(driver):
    """Return current Gross Vehicle Weight text if present and numeric-looking."""
    try:
        laden = driver.find_element(By.ID, "txt_laden_wt")
        return (laden.get_attribute("value") or "").strip()
    except Exception:
        return ""


def _vehicle_details_ready(driver):
    """
    True only when vehicle-specific details have loaded after Get Details.

    Do NOT use generic filled dropdowns (From State / District etc. are pre-filled
    and caused false positives → empty extract → endless browser restarts).
    """
    laden_val = _laden_weight_value(driver)
    try:
        if laden_val and float(laden_val) > 0:
            return True
    except (TypeError, ValueError):
        if laden_val and laden_val not in {"0", "-", ""}:
            return True

    vtype = _js_read_dropdown_by_label(driver, "Vehicle Type", quiet=True)
    if vtype:
        return True
    vclass = _js_read_dropdown_by_label(driver, "Vehicle Class", quiet=True)
    if vclass:
        return True
    return False


def _js_read_dropdown_by_label(driver, label_text, quiet=False):
    """
    Read selected PrimeFaces dropdown value by its field label text.

    Matches reference.html structure inside #kltaxcollection:
      - span.ui-outputlabel-label  = "Vehicle Type" / "Vehicle Class"
      - sibling ui-selectonemenu in the same ui-grid-col-*
      - selected <option> in the hidden <select>, or span.ui-selectonemenu-label
        (e.g. GOODS VEHICLE / TRAILER)
    """
    script = """
    var labelText = arguments[0];
    var root = document.getElementById('kltaxcollection') || document;

    function norm(s) {
      return (s || '').replace(/\\s+/g, ' ').trim();
    }

    function isPlaceholder(s) {
      var t = norm(s).toLowerCase();
      return !t || t.indexOf('---select') === 0 || t === '-1' || t === 'none' || t === 'n/a';
    }

    function selectedFromSelect(sel) {
      if (!sel || sel.selectedIndex < 0) return '';
      // Prefer option with selected attribute, then selectedIndex
      var opts = sel.options || [];
      for (var i = 0; i < opts.length; i++) {
        if (opts[i].selected || opts[i].getAttribute('selected') !== null) {
          var t = norm(opts[i].textContent || opts[i].text || '');
          if (!isPlaceholder(t)) return t;
        }
      }
      var opt = opts[sel.selectedIndex];
      return opt ? norm(opt.textContent || opt.text || '') : '';
    }

    var labels = root.querySelectorAll('span.ui-outputlabel-label');
    for (var i = 0; i < labels.length; i++) {
      if (norm(labels[i].textContent) !== labelText) continue;

      var col = labels[i].closest('[class*="ui-grid-col"]');
      if (!col) {
        // climb a few parents if closest() unavailable / mismatched
        col = labels[i].parentElement;
        for (var up = 0; up < 6 && col; up++) {
          if (col.className && String(col.className).indexOf('ui-grid-col') !== -1) break;
          col = col.parentElement;
        }
      }
      if (!col) continue;

      // 1) Hidden native <select> selected option (authoritative)
      var sel = col.querySelector('select');
      var fromSelect = selectedFromSelect(sel);
      if (fromSelect && !isPlaceholder(fromSelect)) {
        return {value: fromSelect, source: 'select'};
      }

      // 2) Visible PrimeFaces label span
      var span = col.querySelector('span.ui-selectonemenu-label');
      if (span) {
        var fromSpan = norm(span.textContent || span.innerText || '');
        if (fromSpan && !isPlaceholder(fromSpan)) {
          return {value: fromSpan, source: 'label-span'};
        }
      }
    }
    return {value: '', source: 'not-found'};
    """
    try:
        result = driver.execute_script(script, label_text) or {}
        value = str(result.get("value") or "").strip()
        source = result.get("source") or "?"
        if value and not _is_placeholder_select_value(value):
            if not quiet:
                print(f"  {label_text}: {value} (via JS/{source})")
            return value
        if not quiet:
            print(f"  {label_text}: not ready yet (JS/{source})")
    except Exception as e:
        if not quiet:
            print(f"  {label_text}: JS read failed: {e}")
    return ""


def _read_select_label(driver, preferred_ids, label_text):
    """
    Read the selected value from a PrimeFaces selectOneMenu dropdown.

    Primary: JS lookup by field label inside #kltaxcollection (stable across j_idt* changes).
    Fallback: preferred element ids, then Selenium XPath on the grid column.
    """
    # 1) Label-based JS (preferred — ids like j_idt215 change every session)
    js_value = _js_read_dropdown_by_label(driver, label_text)
    if js_value:
        return js_value

    # 2) Preferred ids from known captures (best-effort only)
    for element_id in preferred_ids:
        try:
            element = driver.find_element(By.ID, element_id)
            tag = (element.tag_name or "").lower()
            if tag == "select":
                text = driver.execute_script(
                    """
                    var s = arguments[0];
                    if (!s || s.selectedIndex < 0) return '';
                    var opt = s.options[s.selectedIndex];
                    return opt ? (opt.textContent || opt.text || '').trim() : '';
                    """,
                    element,
                )
            else:
                text = driver.execute_script(
                    "return (arguments[0].textContent || arguments[0].innerText || '').trim();",
                    element,
                )
            text = (text or "").strip()
            if text and not _is_placeholder_select_value(text):
                print(f"  {element_id}: {text}")
                return text
        except Exception:
            continue

        if element_id.endswith("_label"):
            select_id = element_id[: -len("_label")] + "_input"
            try:
                select_el = driver.find_element(By.ID, select_id)
                text = driver.execute_script(
                    """
                    var s = arguments[0];
                    if (!s || s.selectedIndex < 0) return '';
                    var opt = s.options[s.selectedIndex];
                    return opt ? (opt.textContent || opt.text || '').trim() : '';
                    """,
                    select_el,
                )
                text = (text or "").strip()
                if text and not _is_placeholder_select_value(text):
                    print(f"  {select_id}: {text}")
                    return text
            except Exception:
                continue

    # 3) Selenium XPath scoped to the labeled grid column
    col_xpath = (
        "//div[contains(@class,'ui-grid-col')]"
        "[.//span[contains(@class,'ui-outputlabel-label') and "
        f"normalize-space()='{label_text}']]"
    )
    try:
        col = driver.find_element(By.XPATH, col_xpath)
        try:
            select_el = col.find_element(By.XPATH, ".//select")
            text = driver.execute_script(
                """
                var s = arguments[0];
                if (!s || s.selectedIndex < 0) return '';
                var opt = s.options[s.selectedIndex];
                return opt ? (opt.textContent || opt.text || '').trim() : '';
                """,
                select_el,
            )
            text = (text or "").strip()
            if text and not _is_placeholder_select_value(text):
                print(f"  {label_text} via XPath <select>: {text}")
                return text
        except Exception:
            pass

        try:
            label_span = col.find_element(
                By.XPATH, ".//span[contains(@class,'ui-selectonemenu-label')]"
            )
            text = driver.execute_script(
                "return (arguments[0].textContent || arguments[0].innerText || '').trim();",
                label_span,
            )
            text = (text or "").strip()
            if text and not _is_placeholder_select_value(text):
                print(f"  {label_text} via XPath label-span: {text}")
                return text
        except Exception:
            pass
    except Exception as e:
        print(f"  Could not locate grid column for '{label_text}': {e}")

    return "none"


def _wait_for_type_and_class(driver, timeout=12):
    """
    Wait until Vehicle Type and Vehicle Class dropdowns show real selected values.

    Gross weight often fills before these cascaded dropdowns finish loading.
    Skip the long wait when Gross Weight never loaded (nothing to wait for).
    """
    laden_val = _laden_weight_value(driver)
    try:
        laden_ok = bool(laden_val) and float(laden_val) > 0
    except (TypeError, ValueError):
        laden_ok = bool(laden_val) and laden_val not in {"0", "-", ""}

    if not laden_ok:
        # Details panel not populated — don't burn 12s waiting for Type/Class
        return (
            _js_read_dropdown_by_label(driver, "Vehicle Type", quiet=True),
            _js_read_dropdown_by_label(driver, "Vehicle Class", quiet=True),
        )

    deadline = time.time() + timeout
    last_type, last_class = "", ""
    print("  Waiting for Vehicle Type / Vehicle Class dropdowns to populate...")
    while time.time() < deadline:
        last_type = _js_read_dropdown_by_label(driver, "Vehicle Type", quiet=True)
        last_class = _js_read_dropdown_by_label(driver, "Vehicle Class", quiet=True)
        if last_type and last_class:
            print(f"  Vehicle Type: {last_type}")
            print(f"  Vehicle Class: {last_class}")
            return last_type, last_class
        time.sleep(0.5)
    print(
        f"  [WARN] Timed out waiting for Type/Class "
        f"(type='{last_type or 'none'}', class='{last_class or 'none'}')"
    )
    return last_type, last_class


def _details_look_populated(details):
    """True if scraped details look like real vehicle data (not empty/N/A)."""
    gross = str(details.get(GROSS_WEIGHT_COLUMN, "")).strip()
    unladen = str(details.get(UNLADEN_WEIGHT_COLUMN, "")).strip()
    vtype = str(details.get(VEHICLE_TYPE_COLUMN, "")).strip().lower()
    vclass = str(details.get(VEHICLE_CLASS_COLUMN, "")).strip().lower()

    def _numeric_ok(val):
        try:
            return float(val) > 0
        except (TypeError, ValueError):
            return False

    if _numeric_ok(gross) or _numeric_ok(unladen):
        return True
    if vtype not in {"", "none", "n/a", "nan"} and not vtype.startswith("---"):
        return True
    if vclass not in {"", "none", "n/a", "nan"} and not vclass.startswith("---"):
        return True
    return False


def get_vehicle_details(driver):
    """
    Extract Kerala checkpost fields from the loaded details page.

    reference.html example:
      Vehicle Type  -> GOODS VEHICLE  (selectonemenu selected option / label span)
      Vehicle Class -> TRAILER
      Unladen Wt    -> txt_distance (also try txt_unladen_wt / label lookup)
    """
    print("Getting Kerala vehicle details...")
    gross = _read_input_field(
        driver, ["txt_laden_wt"], ["gross vehicle weight", "gross", "laden", "gvw"]
    )
    unladen = _read_input_field(
        driver,
        ["txt_distance", "txt_unladen_wt"],
        ["unladen"],
    )

    # Wait for cascaded Type/Class only when weight fields indicate data loaded
    waited_type, waited_class = _wait_for_type_and_class(driver, timeout=12)

    vehicle_type = waited_type or _read_select_label(
        driver,
        [
            "j_idt215_label",
            "j_idt215_input",
            "j_idt499_label",
            "j_idt499_input",
        ],
        "Vehicle Type",
    )
    vehicle_class = waited_class or _read_select_label(
        driver,
        [
            "j_idt221_label",
            "j_idt221_input",
            "j_idt505_label",
            "j_idt505_input",
        ],
        "Vehicle Class",
    )

    details = {
        GROSS_WEIGHT_COLUMN: gross if gross else "0",
        UNLADEN_WEIGHT_COLUMN: unladen if unladen else "0",
        VEHICLE_TYPE_COLUMN: vehicle_type if vehicle_type else "none",
        VEHICLE_CLASS_COLUMN: vehicle_class if vehicle_class else "none",
    }
    print(
        f"Gross: {details[GROSS_WEIGHT_COLUMN]} | Unladen: {details[UNLADEN_WEIGHT_COLUMN]} | "
        f"Type: {details[VEHICLE_TYPE_COLUMN]} | Class: {details[VEHICLE_CLASS_COLUMN]}"
    )
    return details


def restart_browser_and_continue(driver):
    """Restart browser and re-navigate to the tax page."""
    print("\n[RESTART] RESTARTING BROWSER...")
    try:
        driver.quit()
        print("Closed current browser session")
    except Exception:
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
        try:
            new_driver.quit()
        except Exception:
            pass
        return None, None

    print("[OK] Browser restarted successfully")
    return new_driver, new_wait


def _set_details(df, idx, details):
    for col, value in details.items():
        if col not in df.columns:
            df[col] = ""
        df.at[idx, col] = value


def _na_details():
    return {
        GROSS_WEIGHT_COLUMN: "N/A",
        UNLADEN_WEIGHT_COLUMN: "N/A",
        VEHICLE_TYPE_COLUMN: "N/A",
        VEHICLE_CLASS_COLUMN: "N/A",
    }


def _error_details(message):
    return {
        GROSS_WEIGHT_COLUMN: message,
        UNLADEN_WEIGHT_COLUMN: message,
        VEHICLE_TYPE_COLUMN: message,
        VEHICLE_CLASS_COLUMN: message,
    }


def upsert_gross_weight_to_checkpostmaster(cursor, veh_reg_no, weight):
    """
    Insert or update Gross Vehicle Weight into checkpostmaster only.

    Returns: (success: bool, action: str, message: str)
      action is "ADDED", "UPDATED", or "FAILED"
    """
    try:
        try:
            weight_value = float(weight)
        except (TypeError, ValueError):
            return False, "FAILED", f"Invalid weight: {weight}"

        update_query = """
        UPDATE checkpostmaster
        SET weight = %s
        WHERE "Unique Vehicle Number" = %s
        """
        cursor.execute(update_query, (weight_value, veh_reg_no))

        if cursor.rowcount > 0:
            return True, "UPDATED", "Updated Gross Vehicle Weight in checkpostmaster"

        insert_query = """
        INSERT INTO checkpostmaster
        ("Unique Vehicle Number", "weight")
        VALUES (%s, %s)
        """
        cursor.execute(insert_query, (veh_reg_no, weight_value))
        return True, "ADDED", "Added Gross Vehicle Weight to checkpostmaster"

    except psycopg2.Error as e:
        return False, "FAILED", f"Database error: {e}"
    except Exception as e:
        return False, "FAILED", f"Error: {e}"


def process_single_vehicle(driver, wait, vehicle_no, idx, df):
    """Process a single vehicle — extract gross, unladen, type, and class."""
    max_retries = 5
    retry_count = 0

    while retry_count <= max_retries:
        try:
            print(f"\n{'=' * 50}")
            print(
                f"Processing {idx + 1}/{len(df)} — Vehicle: {vehicle_no} "
                f"(Attempt {retry_count + 1})"
            )
            print(f"{'=' * 50}")

            print("Quick refreshing page...")
            driver.execute_script("location.reload()")
            time.sleep(2)

            vehicle_input_xpath = (
                "//input[@type='text' and @maxlength='10' and not(@id='mobileno')]"
            )

            print("Waiting for Vehicle Number input to be interactable...")
            try:
                input_element = WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable((By.XPATH, vehicle_input_xpath))
                )
                print("[OK] Vehicle Number input found and ready")
            except TimeoutException:
                print("[ERROR] Timeout: could not find Vehicle Number input")
                if retry_count < max_retries:
                    retry_count += 1
                    print(
                        f"[RETRY] Attempting browser restart "
                        f"({retry_count}/{max_retries})..."
                    )
                    new_driver, new_wait = restart_browser_and_continue(driver)
                    if new_driver:
                        driver = new_driver
                        wait = new_wait
                        continue
                    break
                print("[ERROR] Max retries reached")
                _set_details(df, idx, _error_details("Error - Input Not Found"))
                return driver, wait, False

            try:
                popup_xpath = (
                    "//div[contains(@class,'ui-dialog') and contains(@style,'display: block')]"
                )
                popup = WebDriverWait(driver, 2).until(
                    EC.visibility_of_element_located((By.XPATH, popup_xpath))
                )
                if popup:
                    ok_button_xpath = (
                        "//div[contains(@class,'ui-dialog')]"
                        "//button[.//span[contains(text(),'OK') or contains(text(),'Ok')]]"
                    )
                    safe_click(driver, wait, ok_button_xpath, "OK button on popup")
            except TimeoutException:
                pass

            try:
                input_element = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, vehicle_input_xpath))
                )
                # Set value and fire events so PrimeFaces registers the change
                driver.execute_script(
                    """
                    var el = arguments[0], val = arguments[1];
                    el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    if (typeof el.onkeyup === 'function') { try { el.onkeyup(); } catch (e) {} }
                    """,
                    input_element,
                    vehicle_no,
                )
                print("[OK] Vehicle number entered via JavaScript")
            except Exception:
                try:
                    input_element.clear()
                    input_element.send_keys(vehicle_no)
                    print("[OK] Vehicle number entered")
                except Exception as e:
                    print(f"[ERROR] Could not enter vehicle number: {e}")
                    retry_count += 1
                    continue

            get_details_xpath = "//button[.//span[contains(text(), 'Get Details')]]"
            try:
                get_details_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, get_details_xpath))
                )
                driver.execute_script("arguments[0].click();", get_details_btn)
                print("[OK] Get Details clicked")
            except Exception:
                safe_click(driver, wait, get_details_xpath, "Get Details button")

            print("Waiting for vehicle details...")
            time.sleep(3)

            popup_appeared = False
            data_appeared = False

            for _ in range(8):
                try:
                    popup_xpath = (
                        "//div[contains(@class,'ui-dialog') and "
                        "contains(@style,'display: block')]"
                    )
                    try:
                        WebDriverWait(driver, 2).until(
                            EC.visibility_of_element_located((By.XPATH, popup_xpath))
                        )
                        popup_appeared = True
                        print("Popup detected — dismissing (will still try to read details)")
                        ok_button_xpath = (
                            "//div[contains(@class,'ui-dialog')]"
                            "//button[.//span[contains(text(),'OK') or contains(text(),'Ok')]]"
                        )
                        safe_click(driver, wait, ok_button_xpath, "OK button on popup")
                        time.sleep(1)
                    except TimeoutException:
                        pass

                    # Only trust vehicle-specific fields (not pre-filled From State etc.)
                    if _vehicle_details_ready(driver):
                        data_appeared = True
                        laden_val = _laden_weight_value(driver) or "?"
                        print(f"[OK] Vehicle details loaded (txt_laden_wt={laden_val})")
                        break
                except Exception:
                    pass

                time.sleep(1)

            if data_appeared:
                details = get_vehicle_details(driver)
                if _details_look_populated(details):
                    _set_details(df, idx, details)
                    print(f"[OK] Details extracted for {vehicle_no}")
                    return driver, wait, True
                print("[WARNING] Details signal present but extracted values were empty")

            if popup_appeared and not data_appeared:
                _set_details(df, idx, _na_details())
                print("[OK] Marked as N/A (popup only, no usable details)")
                return driver, wait, True

            # Soft retry on same browser — do NOT full-restart here (that caused
            # endless reopen loops when details never loaded / false positives).
            if retry_count < max_retries:
                retry_count += 1
                print(
                    f"[WARNING] No usable details for {vehicle_no} — "
                    f"retrying on same browser ({retry_count}/{max_retries})..."
                )
                continue

            _set_details(df, idx, _na_details())
            print(
                f"[OK] Marked as N/A for {vehicle_no} "
                f"(no details after {max_retries + 1} attempts)"
            )
            return driver, wait, True

        except Exception as e:
            print(f"Error processing {vehicle_no}: {e}")
            if retry_count < max_retries:
                retry_count += 1
                print(
                    f"[RETRY] Attempting browser restart ({retry_count}/{max_retries})..."
                )
                new_driver, new_wait = restart_browser_and_continue(driver)
                if new_driver:
                    driver = new_driver
                    wait = new_wait
                    continue
                break
            print("[ERROR] Max retries reached")
            _set_details(df, idx, _error_details("Error"))
            return driver, wait, False

    _set_details(df, idx, _error_details("Error - Max Retries"))
    return driver, wait, False


def make_progress_dir(base_path=None):
    """
    Create a directory for per-node progress Excel files.

    base_path: optional file/folder path used to name the progress folder.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if base_path:
        base = Path(base_path)
        parent = base.parent if base.suffix else base
        name = f"{base.stem}_progress_{stamp}" if base.suffix else f"progress_{stamp}"
        progress_dir = parent / name
    else:
        progress_dir = Path(f"kerala_progress_{stamp}")
    progress_dir.mkdir(parents=True, exist_ok=True)
    print(f"[PROGRESS] Writing concurrent node outputs to: {progress_dir}")
    return str(progress_dir)


def chunk_progress_path(progress_dir, chunk_id):
    """Path for one Selenium Grid node's progress Excel file."""
    return str(Path(progress_dir) / f"chunk_{int(chunk_id):02d}.xlsx")


def save_chunk_progress(df, progress_path):
    """
    Persist current chunk DataFrame to its own Excel file (no shared lock).

    Stores original row index in ORIG_INDEX_COLUMN so files can be merged later.
    """
    if not progress_path:
        return
    try:
        out = df.copy()
        out[ORIG_INDEX_COLUMN] = out.index
        # Keep index column first for readability
        cols = [ORIG_INDEX_COLUMN] + [c for c in out.columns if c != ORIG_INDEX_COLUMN]
        out = out[cols]
        Path(progress_path).parent.mkdir(parents=True, exist_ok=True)
        out.to_excel(progress_path, index=False)
    except Exception as e:
        print(f"  [PROGRESS] Failed to save {progress_path}: {e}", flush=True)


def load_chunk_progress_frame(progress_path):
    """Load a chunk progress Excel and restore original index."""
    frame = pd.read_excel(progress_path, dtype=str)
    if ORIG_INDEX_COLUMN in frame.columns:
        idx = pd.to_numeric(frame[ORIG_INDEX_COLUMN], errors="coerce")
        frame = frame.drop(columns=[ORIG_INDEX_COLUMN])
        valid = idx.notna()
        frame = frame.loc[valid].copy()
        frame.index = idx.loc[valid].astype(int)
    return frame


def merge_chunk_progress_files(df_input, progress_dir):
    """
    Merge all chunk_*.xlsx files from progress_dir onto a copy of df_input.

    Safe to call even if some nodes failed — uses whatever files exist on disk.
    """
    if not progress_dir:
        return None

    progress_path = Path(progress_dir)
    if not progress_path.exists():
        return None

    chunk_files = sorted(progress_path.glob("chunk_*.xlsx"))
    if not chunk_files:
        print(f"[PROGRESS] No chunk files found in {progress_dir}")
        return None

    merged = df_input.copy()
    for col in DETAIL_COLUMNS:
        if col not in merged.columns:
            merged[col] = ""

    loaded = 0
    for path in chunk_files:
        try:
            frame = load_chunk_progress_frame(path)
            common = frame.index.intersection(merged.index)
            if len(common) == 0:
                print(f"[PROGRESS] Warning: no index overlap for {path.name}; skipped")
                continue
            for col in frame.columns:
                if col not in merged.columns:
                    merged[col] = ""
                merged.loc[common, col] = frame.loc[common, col]
            loaded += 1
            print(f"[PROGRESS] Merged {path.name} ({len(common)} rows)")
        except Exception as e:
            print(f"[PROGRESS] Could not merge {path}: {e}")

    if loaded == 0:
        return None

    merged_out = progress_path / "merged_output.xlsx"
    try:
        merged.to_excel(merged_out, index=False)
        print(f"[PROGRESS] Wrote merged output: {merged_out}")
    except Exception as e:
        print(f"[PROGRESS] Could not write merged_output.xlsx: {e}")

    return merged


def scrape_kerala_checkpost(
    df_input,
    remote_url=None,
    use_selenium_grid=None,
    progress_dir=None,
):
    """
    Scrape Kerala checkpost fields for all vehicles in df_input.

    use_selenium_grid: True  -> Selenium Grid at SELENIUM_REMOTE_URL
                       False -> local Chrome
                       None  -> module-level USE_SELENIUM_GRID
    remote_url: explicit Grid URL; when set, forces grid usage.
    progress_dir: folder for per-node chunk_XX.xlsx progress files (created if needed).
                  When None and grid is used, a timestamped folder is created automatically.
    """
    if use_selenium_grid is None:
        use_selenium_grid = USE_SELENIUM_GRID

    use_grid = use_selenium_grid or (remote_url is not None)
    grid_url = remote_url or SELENIUM_REMOTE_URL

    print("=" * 80)
    print("STARTING KERALA CHECKPOST VEHICLE SCRAPING")
    print("=" * 80)

    if not use_grid:
        print("Web scrape mode: local Chrome (single browser)")
        if progress_dir is None:
            progress_dir = make_progress_dir()
        local_path = chunk_progress_path(progress_dir, 1)
        return _scrape_chunk(df_input, None, chunk_id=1, progress_path=local_path)

    if progress_dir is None:
        progress_dir = make_progress_dir()
    else:
        Path(progress_dir).mkdir(parents=True, exist_ok=True)
        print(f"[PROGRESS] Writing concurrent node outputs to: {progress_dir}")

    print(
        f"Web scrape mode: Selenium Grid "
        f"({grid_url}, auto_nodes={SELENIUM_AUTO_MANAGE_NODES})"
    )
    return _run_grid_scrape(df_input, grid_url, progress_dir=progress_dir)


def _scrape_chunk(chunk_df, remote_url, chunk_id=None, progress_path=None):
    """Run scraping for one chunk with a per-thread remote URL (None = local)."""
    _thread_remote_url.value = remote_url
    try:
        if progress_path:
            print(
                f"[PROGRESS] Node/chunk {chunk_id} output file: {progress_path}",
                flush=True,
            )
        return _scrape_kerala_checkpost_impl(chunk_df, progress_path=progress_path)
    finally:
        _thread_remote_url.value = None


def _run_grid_scrape(df_input, remote_url, progress_dir=None):
    """
    Split work across Grid nodes; each node writes its own chunk_XX.xlsx.
    Merge all chunk files at the end (and on failure) so progress is not lost.
    """
    chunks = split_df_for_grid(df_input, MAX_SELENIUM_GRID_NODES)
    if not chunks:
        return df_input

    if progress_dir is None:
        progress_dir = make_progress_dir()
    else:
        Path(progress_dir).mkdir(parents=True, exist_ok=True)

    print(
        f"  [SELENIUM GRID] {len(chunks)} chunk(s) -> {remote_url} "
        f"| progress_dir={progress_dir}",
        flush=True,
    )

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
            future_to_chunk = {}
            for chunk_id, chunk in enumerate(chunks, start=1):
                path = chunk_progress_path(progress_dir, chunk_id)
                # Seed file immediately so a crash mid-run still leaves something
                save_chunk_progress(chunk, path)
                future = executor.submit(
                    _scrape_chunk, chunk, remote_url, chunk_id, path
                )
                future_to_chunk[future] = chunk_id

            for future in as_completed(future_to_chunk):
                chunk_id = future_to_chunk[future]
                try:
                    result_frames.append(future.result())
                    print(
                        f"  [SELENIUM GRID] chunk {chunk_id}/{len(chunks)} completed",
                        flush=True,
                    )
                except Exception as exc:
                    failures.append((chunk_id, exc))
                    print(
                        f"  [SELENIUM GRID] chunk {chunk_id} failed: {exc}",
                        flush=True,
                    )

        # Prefer on-disk merge (survives partial/crashed workers) then in-memory
        merged = merge_chunk_progress_files(df_input, progress_dir)
        if merged is None and result_frames:
            merged = df_input.copy()
            for col in DETAIL_COLUMNS:
                if col not in merged.columns:
                    merged[col] = ""
            for frame in result_frames:
                for col in frame.columns:
                    merged.loc[frame.index, col] = frame[col]

        if not result_frames and merged is None:
            grid_error = f"All Selenium Grid chunks failed: {failures}"
        elif failures:
            # Partial success is OK when progress files were merged
            print(
                f"  [SELENIUM GRID] {len(failures)} chunk(s) had errors; "
                f"keeping merged progress from disk",
                flush=True,
            )
            if merged is None:
                grid_error = f"{len(failures)} Selenium Grid chunk(s) failed: {failures}"

    except Exception as exc:
        grid_error = str(exc)
        print(f"  [SELENIUM GRID] error: {exc}", flush=True)
        # Still try to recover whatever nodes wrote
        recovered = merge_chunk_progress_files(df_input, progress_dir)
        if recovered is not None:
            merged = recovered
    finally:
        if managed_nodes:
            stop_managed_nodes(managed_nodes)

    if grid_error and merged is None:
        print(
            f"  [SELENIUM GRID] falling back to local Chrome... ({grid_error})",
            flush=True,
        )
        local_path = chunk_progress_path(progress_dir, 99)
        return _scrape_chunk(df_input, None, chunk_id=99, progress_path=local_path)

    if grid_error and merged is not None:
        print(
            f"  [SELENIUM GRID] partial progress kept from {progress_dir} "
            f"(skipped full local fallback because chunk files exist)",
            flush=True,
        )

    return merged if merged is not None else df_input


def find_vehicle_reg_column(df):
    """
    Return the vehicle registration column if a matching header keyword is found.

    Prefers columns containing both 'veh' and 'reg' (e.g. 'Veh Reg No.'),
    then falls back to any column containing 'vehicle'.
    """
    for col in df.columns:
        if "veh" in str(col).lower() and "reg" in str(col).lower():
            return col
    for col in df.columns:
        if "vehicle" in str(col).lower():
            return col
    return None


def _scrape_kerala_checkpost_impl(df_input, progress_path=None):
    df = df_input.copy()

    veh_col = find_vehicle_reg_column(df)
    if veh_col is None:
        print("[ERROR] Could not find vehicle number column in dataframe")
        save_chunk_progress(df, progress_path)
        return df

    for col in DETAIL_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df[veh_col] = df[veh_col].apply(normalize_vehicle_number)
    invalid_vehicle_count = int((~df[veh_col].apply(is_vehicle_number_eligible)).sum())
    if invalid_vehicle_count:
        print(
            f"[INFO] Skipping {invalid_vehicle_count} vehicle numbers outside eligible "
            f"length range (allowed lengths: 8, 9, 10)"
        )

    print(f"Total vehicles to process: {len(df)}")
    if len(df) == 0:
        print("No vehicles to process.")
        save_chunk_progress(df, progress_path)
        return df

    # No checkpostmaster lookup — scrape every eligible vehicle
    valid_mask = df[veh_col].apply(is_vehicle_number_eligible)
    remaining_df = df[valid_mask]
    remaining_count = len(remaining_df)

    print("\n" + "=" * 80)
    print(f"Web scraping {remaining_count} vehicles (no DB pre-lookup)...")
    print("=" * 80)

    # Seed progress file before browser work so a crash still leaves the chunk input
    save_chunk_progress(df, progress_path)

    if remaining_count == 0:
        print("No valid vehicles to scrape.")
        return df

    conn = get_db_connection()
    if not conn:
        print("[WARNING] Could not connect to database — will scrape without DB upserts")

    driver = setup_driver()
    if not driver:
        print("[ERROR] Could not start browser — aborting")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        save_chunk_progress(df, progress_path)
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
        try:
            driver.quit()
        except Exception:
            pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        save_chunk_progress(df, progress_path)
        return df

    start_time = time.perf_counter()
    scraped_count = 0
    db_added_count = 0
    db_updated_count = 0

    for idx, row in remaining_df.iterrows():
        vehicle_no = normalize_vehicle_number(row[veh_col])
        if not vehicle_no or not is_vehicle_number_eligible(vehicle_no):
            continue

        driver, wait, success = process_single_vehicle(driver, wait, vehicle_no, idx, df)

        # Persist after every vehicle so progress survives crashes / node failures
        save_chunk_progress(df, progress_path)

        if not success:
            print(f"Progress: {scraped_count}/{remaining_count} scraped")
            time.sleep(1)
            continue

        scraped_count += 1
        gross = str(df.at[idx, GROSS_WEIGHT_COLUMN]).strip()

        if conn is not None and gross.lower() not in {
            v.lower() for v in INVALID_WEIGHT_VALUES
        }:
            try:
                conn = check_and_restore_db_connection(conn)
                if conn is None:
                    print(f"[DB-ERROR] {vehicle_no}: no database connection for upsert")
                else:
                    cursor = conn.cursor()
                    ok, action, message = upsert_gross_weight_to_checkpostmaster(
                        cursor, vehicle_no, gross
                    )
                    conn.commit()
                    if ok and action == "ADDED":
                        db_added_count += 1
                        print(f"[DB-ADDED] {vehicle_no}: {message} (weight={gross})")
                    elif ok and action == "UPDATED":
                        db_updated_count += 1
                        print(f"[DB-UPDATED] {vehicle_no}: {message} (weight={gross})")
                    else:
                        print(f"[DB-FAILED] {vehicle_no}: {message}")
            except Exception as e:
                print(f"[DB-ERROR] {vehicle_no}: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
        elif gross.lower() in {v.lower() for v in INVALID_WEIGHT_VALUES}:
            print(f"[DB-SKIP] {vehicle_no}: gross weight '{gross}' not written to DB")

        print(
            f"Progress: {scraped_count}/{remaining_count} scraped | "
            f"DB added={db_added_count}, updated={db_updated_count}"
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

    # Final save for this node
    save_chunk_progress(df, progress_path)

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
    print(f"checkpostmaster ADDED: {db_added_count}")
    print(f"checkpostmaster UPDATED: {db_updated_count}")
    if progress_path:
        print(f"Chunk progress file: {progress_path}")
    print("=" * 80)

    return df


if __name__ == "__main__":
    EXCEL_PATH = "kerala checkpost run.xlsx"

    progress_dir = make_progress_dir(EXCEL_PATH)

    with pd.ExcelFile(EXCEL_PATH) as xls:
        sheets_dict = {
            sheet: pd.read_excel(xls, sheet_name=sheet, dtype=str)
            for sheet in xls.sheet_names
        }

    scraped_any = False
    for sheet_name, df_sheet in sheets_dict.items():
        veh_col = find_vehicle_reg_column(df_sheet)
        if veh_col is None:
            print(
                f"[SKIP] Sheet '{sheet_name}': no vehicle registration header keyword found "
                f"(columns: {list(df_sheet.columns)})"
            )
            continue

        print(
            f"[OK] Sheet '{sheet_name}': found vehicle column '{veh_col}' "
            f"({len(df_sheet)} rows) — starting web scrape"
        )
        # Each Grid node writes chunk_XX.xlsx under progress_dir; results are merged after.
        sheets_dict[sheet_name] = scrape_kerala_checkpost(
            df_sheet,
            use_selenium_grid=USE_SELENIUM_GRID,
            progress_dir=progress_dir,
        )
        scraped_any = True

    if not scraped_any:
        print(
            "[ERROR] No sheet contained a vehicle registration header keyword "
            "(looked for columns with 'veh'+'reg' or 'vehicle'). Nothing scraped."
        )
    else:
        with pd.ExcelWriter(EXCEL_PATH, engine="openpyxl") as writer:
            for sheet, data in sheets_dict.items():
                data.to_excel(writer, sheet_name=sheet, index=False)
        print(f"[OK] Results saved back to '{EXCEL_PATH}'")
        print(f"[OK] Per-node progress files kept in: {progress_dir}")
