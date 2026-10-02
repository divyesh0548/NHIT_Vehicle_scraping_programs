"""Scrape all vehicle details shown on the Chhattisgarh ODC tax page.

Reads every labeled field inside the Tax Payment Details panel. Column names
match those labels. Empty inputs and unselected "Select ..." prompts stay blank.
"""

import os
import threading
import time
import traceback

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from vehicle_number_utils import is_vehicle_number_eligible, normalize_vehicle_number

URL = "https://parivahan.gov.in/"
WAIT_TIME = 30
STATUS_COL = "Status"
_thread_remote_url = threading.local()


def setup_driver(remote_url=None):
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
        options.add_experimental_option(
            "prefs",
            {
                "profile.managed_default_content_settings.images": 2,
                "profile.default_content_setting_values.notifications": 2,
                "profile.default_content_settings.popups": 0,
            },
        )

        if remote_url:
            driver = webdriver.Remote(command_executor=remote_url, options=options)
            print(f"Browser connected to Selenium Grid: {remote_url}")
        else:
            try:
                driver = webdriver.Chrome(options=options)
            except Exception as e1:
                print(f"  [WARN] First attempt failed: {e1}")
                try:
                    driver = webdriver.Chrome(service=Service(), options=options)
                except Exception as e2:
                    print(f"  [WARN] Second attempt failed: {e2}")
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
            print("Browser launched successfully")
        except Exception as url_error:
            print(f"  [ERROR] Driver is not responsive: {url_error}")
            try:
                driver.quit()
            except Exception:
                pass
            return None
        return driver
    except (WebDriverException, Exception) as e:
        print(f"Browser setup failed: {e}")
        print(traceback.format_exc())
        return None


def wait_for_page_load(driver, wait):
    try:
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        return True
    except Exception as e:
        print(f"Page load timeout: {e}")
        return False


def close_mobile_popup(driver, wait):
    try:
        WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located(
                (
                    By.XPATH,
                    "//span[contains(@class, 'english') and contains(text(), 'Update Your Mobile Number')]",
                )
            )
        )
        close_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//button[contains(@class, 'btn-close') and contains(@class, 'position-absolute')]",
                )
            )
        )
        close_button.click()
        time.sleep(2)
        return True
    except TimeoutException:
        return False
    except Exception as e:
        print(f"Error handling mobile popup: {e}")
        return False


def select_state_chhattisgarh(driver, wait):
    for attempt in range(3):
        try:
            print(f"Selecting Chhattisgarh state (attempt {attempt + 1})")
            state_element = None
            for selector in [
                "//div[contains(text(), 'Select State Name')]",
                "//span[contains(text(), 'Select State Name')]",
                "//a[contains(text(), 'Select State Name')]",
                "//button[contains(text(), 'Select State Name')]",
                "//*[contains(text(), 'Select State Name')]",
            ]:
                try:
                    state_element = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    break
                except Exception:
                    continue
            if not state_element:
                print("Could not find state selection element")
                return False

            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", state_element
            )
            time.sleep(1)
            state_element.click()
            time.sleep(2)

            chhattisgarh_element = None
            for selector in [
                "//div[contains(text(), 'CHHATTISGARH')]",
                "//span[contains(text(), 'CHHATTISGARH')]",
                "//a[contains(text(), 'CHHATTISGARH')]",
                "//li[contains(text(), 'CHHATTISGARH')]",
                "//option[contains(text(), 'CHHATTISGARH')]",
                "//*[contains(text(), 'CHHATTISGARH')]",
            ]:
                try:
                    chhattisgarh_element = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    break
                except Exception:
                    continue
            if not chhattisgarh_element:
                print("Could not find Chhattisgarh option")
                return False

            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", chhattisgarh_element
            )
            time.sleep(1)
            chhattisgarh_element.click()
            time.sleep(3)
            try:
                WebDriverWait(driver, 10).until(
                    EC.visibility_of_element_located(
                        (By.XPATH, "//h3[@class='top-space' and contains(text(), 'Service Name')]")
                    )
                )
                print("State selection successful")
                return True
            except Exception:
                print("Service Name section not found yet")
                time.sleep(2)
        except Exception as e:
            print(f"Error selecting state on attempt {attempt + 1}: {e}")
            time.sleep(2)
    print("Failed to select Chhattisgarh after all attempts")
    return False


def select_service(driver, wait):
    for attempt in range(3):
        try:
            print(f"\nSelecting service (attempt {attempt + 1})")
            service_dropdown = wait.until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//span[contains(@class,'ui-selectonemenu-label') and contains(text(),'---Select Service Name---')]",
                    )
                )
            )
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", service_dropdown
            )
            time.sleep(1)
            service_dropdown.click()
            time.sleep(2)

            service_option = wait.until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        "//li[contains(@data-label, 'ADVANCE PAYMENT OF ODC EXEMPTION FEE')]",
                    )
                )
            )
            service_option.click()
            time.sleep(2)

            go_button = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//button[.//span[contains(text(), 'Go')]]")
                )
            )
            go_button.click()
            time.sleep(5)
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//input[@type='text' and @maxlength='10']")
                    )
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
    try:
        print("Opening parivahan.gov.in...")
        driver.get(URL)
        if not wait_for_page_load(driver, wait):
            return False

        if close_mobile_popup(driver, wait):
            print("Mobile number popup closed")

        print("Hovering over Online Services...")
        online_services = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//a[@id='Online' and contains(@class, 'parent-link-with-submenu')]")
            )
        )
        ActionChains(driver).move_to_element(online_services).pause(2).perform()
        time.sleep(3)

        print("Clicking Checkpost Tax...")
        checkpost_tax = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//a[@href='/en/node/579' and contains(@class, 'second-child-menu')]")
            )
        )
        checkpost_tax.click()
        wait.until(
            EC.visibility_of_element_located(
                (
                    By.XPATH,
                    "//span[contains(@class, 'field--name-title') and contains(text(), 'Checkpost Tax')]",
                )
            )
        )

        if not select_state_chhattisgarh(driver, wait):
            return False
        wait.until(
            EC.visibility_of_element_located(
                (By.XPATH, "//h3[@class='top-space' and contains(text(), 'Service Name')]")
            )
        )
        if not select_service(driver, wait):
            return False
        print("Successfully navigated to vehicle entry page")
        return True
    except Exception as e:
        print(f"Error navigating to tax page: {e}")
        return False


def safe_click(driver, wait, xpath, description="element", timeout=15):
    try:
        elem = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, xpath)))
        elem.click()
        time.sleep(1)
        return True
    except TimeoutException:
        print(f"Timeout: could not find or click {description}")
        return False
    except Exception as e:
        print(f"Error clicking {description}: {e}")
        return False


def restart_browser_and_continue(driver):
    print("\n[RESTART] Restarting browser...")
    try:
        driver.quit()
    except Exception:
        pass
    time.sleep(3)
    new_driver = setup_driver()
    if not new_driver:
        return None, None
    new_wait = WebDriverWait(new_driver, WAIT_TIME)
    if not navigate_to_tax_page(new_driver, new_wait):
        try:
            new_driver.quit()
        except Exception:
            pass
        return None, None
    print("[OK] Browser restarted")
    return new_driver, new_wait

# Labels from reference.html, in page order. Extra labels found on the live
# page are appended when they appear.
DETAIL_COLUMNS = [
    "Vehicle No.",
    "Chassis No.",
    "Owner Type",
    "Owner/Firm Name",
    "Mobile No.",
    "Vehicle Type",
    "Vehicle Class",
    "GVW (In Kg.)",
    "Vehicle Total Weight with Consignment(In Kg.)",
    "Body Type",
    "Overdimension Type",
    "Nature of Goods",
    "Permit Type",
    "Permit/Authorization No.",
    "Road Tax Validity",
    "From State",
    "From District",
    "To State",
    "To District",
    "Insurance Validity",
    "Fitness Validity",
    "PUCC Validity",
    "Permit Validity",
    "LR No.",
    "LR Date",
    "Goods Description",
    "Fill in case of Other",
    "Fee Amount",
    "Sl. No.",
    "Particulars",
    "Height",
    "Width",
    "Length",
    "Fee",
]

_EXTRACT_DETAILS_JS = r"""
const root = document.getElementById('taxcollodc') || document;
const result = {};
const cols = root.querySelectorAll('.ui-grid-col-2, .ui-grid-col-3, .ui-grid-col-4, .ui-grid-col-6');
cols.forEach((col) => {
    const labelEl = col.querySelector(':scope > label span.ui-outputlabel-label');
    if (!labelEl) return;
    const label = (labelEl.textContent || '').replace(/\s+/g, ' ').trim();
    if (!label) return;

    let value = '';
    const dateInput = col.querySelector(':scope > span.ui-calendar input');
    const textInput = col.querySelector(':scope > input');
    const menuLabel = col.querySelector(':scope > .ui-selectonemenu .ui-selectonemenu-label');
    const menuBtn = col.querySelector(':scope > .ui-menubutton .ui-button-text');
    if (dateInput) value = dateInput.value || '';
    else if (textInput) value = textInput.value || '';
    else if (menuLabel) value = menuLabel.textContent || '';
    else if (menuBtn) value = menuBtn.textContent || '';
    result[label] = value.replace(/\s+/g, ' ').trim();
});

const table = root.querySelector('#qqq table');
if (table) {
    const headers = Array.from(table.querySelectorAll('thead th')).map((th) =>
        (th.innerText || '').replace(/\s+/g, ' ').trim()
    );
    const rows = [];
    table.querySelectorAll('tbody tr').forEach((tr) => {
        if (tr.classList.contains('ui-datatable-empty-message')) return;
        const cells = Array.from(tr.querySelectorAll('td')).map((td) =>
            (td.innerText || '').replace(/\s+/g, ' ').trim()
        );
        if (cells.some((cell) => cell)) rows.push(cells);
    });
    headers.forEach((header, index) => {
        if (!header) return;
        result[header] = rows.map((row) => row[index] || '').filter(Boolean).join(' | ');
    });
}
return result;
"""


def blank_if_unset(value):
    """Return the page value, or '' for empty fields and unselected prompts."""
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").strip()
    if not text or text.lower() in {"nan", "none"}:
        return ""
    core = text.lower().strip("-").strip()
    if core.startswith("select "):
        return ""
    if core in {"no records found.", "no records found"}:
        return ""
    return text


def find_vehicle_column(df):
    for col in df.columns:
        name = str(col).lower()
        if "veh" in name and ("reg" in name or "no" in name or "num" in name):
            return col
    for col in df.columns:
        if "vehicle" in str(col).lower():
            return col
    raise ValueError(
        "Could not find a vehicle number column. Expected a name containing "
        "'vehicle' or both 'veh' and 'reg'/'no'."
    )


def read_input_table(path, sheet_name=0):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return pd.read_csv(path, dtype=str)
    if ext in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        return pd.read_excel(path, sheet_name=sheet_name, header=0, engine="openpyxl", dtype=str)
    if ext == ".xls":
        return pd.read_excel(path, sheet_name=sheet_name, header=0, dtype=str)
    raise ValueError(f"Unsupported input type '{ext}': {path}")


def ensure_output_columns(df):
    for col in DETAIL_COLUMNS + [STATUS_COL]:
        if col not in df.columns:
            df[col] = ""
        else:
            df[col] = df[col].fillna("").astype(str).replace({"nan": ""})
    return df


def extract_vehicle_details(driver):
    raw = driver.execute_script(_EXTRACT_DETAILS_JS) or {}
    details = {}
    for label, value in raw.items():
        label = str(label).replace("\xa0", " ").strip()
        if not label or label == STATUS_COL:
            continue
        details[label] = blank_if_unset(value)
    return details


def apply_details(df, idx, details, status):
    for col in DETAIL_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    for label, value in details.items():
        if label not in df.columns:
            df[label] = ""
        df.at[idx, label] = value
    for col in DETAIL_COLUMNS:
        if col not in details:
            df.at[idx, col] = ""
    df.at[idx, STATUS_COL] = status


def save_results(df, output_path):
    ext = os.path.splitext(output_path)[1].lower()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    if ext == ".csv":
        df.to_csv(output_path, index=False)
    else:
        df.to_excel(output_path, index=False, engine="openpyxl")


def _needs_scrape(status):
    text = "" if status is None else str(status).strip()
    if not text or text.lower() == "nan":
        return True
    return text.lower().startswith("error")


def _details_or_popup(driver):
    """Return 'popup', 'data', or '' after Get Details has been clicked."""
    popup_xpath = "//div[contains(@class,'ui-dialog') and contains(@style,'display: block')]"
    try:
        popup = driver.find_element(By.XPATH, popup_xpath)
        if popup.is_displayed():
            return "popup"
    except Exception:
        pass

    chassis_xpath = (
        "//span[contains(@class,'ui-outputlabel-label') and "
        "normalize-space(.)='Chassis No.']/following::input[1]"
    )
    try:
        chassis = driver.find_element(By.XPATH, chassis_xpath)
        if (chassis.get_attribute("value") or "").strip():
            return "data"
    except Exception:
        pass
    return ""


def process_single_vehicle(driver, wait, vehicle_no, idx, df, restart_depth=0):
    """Look up one vehicle and store every filled field from the tax panel."""
    max_retries = 5
    retry_count = 0

    while retry_count <= max_retries:
        try:
            print(f"\n{'=' * 50}")
            print(f"Processing row {idx} — Vehicle: {vehicle_no} (Attempt {retry_count + 1})")
            print(f"{'=' * 50}")

            driver.execute_script("location.reload()")
            time.sleep(2)

            vehicle_input_xpath = "//input[@type='text' and @maxlength='10']"
            try:
                input_element = WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable((By.XPATH, vehicle_input_xpath))
                )
            except TimeoutException:
                print("[ERROR] Timeout: could not find Vehicle Number input")
                if retry_count >= max_retries:
                    apply_details(df, idx, {}, "Error - Input Not Found")
                    return driver, wait, False
                retry_count += 1
                new_driver, new_wait = restart_browser_and_continue(driver)
                if not new_driver:
                    break
                driver, wait = new_driver, new_wait
                continue

            try:
                popup_xpath = "//div[contains(@class,'ui-dialog') and contains(@style,'display: block')]"
                WebDriverWait(driver, 2).until(
                    EC.visibility_of_element_located((By.XPATH, popup_xpath))
                )
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
                driver.execute_script(
                    "arguments[0].value = arguments[1];", input_element, vehicle_no
                )
            except Exception:
                input_element.clear()
                input_element.send_keys(vehicle_no)

            get_details_xpath = "//button[.//span[contains(text(), 'Get Details')]]"
            try:
                get_details_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, get_details_xpath))
                )
                driver.execute_script("arguments[0].click();", get_details_btn)
            except Exception:
                safe_click(driver, wait, get_details_xpath, "Get Details button")

            outcome = ""
            deadline = time.time() + 15
            while time.time() < deadline:
                outcome = _details_or_popup(driver)
                if outcome:
                    break
                time.sleep(0.5)

            if outcome == "popup":
                print("Popup detected - no data available")
                ok_button_xpath = (
                    "//div[contains(@class,'ui-dialog')]"
                    "//button[.//span[contains(text(),'OK') or contains(text(),'Ok')]]"
                )
                safe_click(driver, wait, ok_button_xpath, "OK button on popup")
                apply_details(df, idx, {}, "N/A")
                return driver, wait, True

            if outcome == "data":
                details = extract_vehicle_details(driver)
                filled = [name for name, value in details.items() if value]
                print(f"[OK] Filled fields: {', '.join(filled) if filled else '(none)'}")
                apply_details(df, idx, details, "OK")
                return driver, wait, True

            if restart_depth >= max_retries:
                print("[ERROR] Details did not load after browser restarts")
                apply_details(df, idx, {}, "Error - Max Retries")
                return driver, wait, False

            print("[WARNING] No data loaded — restarting browser...")
            new_driver, new_wait = restart_browser_and_continue(driver)
            if new_driver:
                return process_single_vehicle(
                    new_driver, new_wait, vehicle_no, idx, df, restart_depth + 1
                )
            apply_details(df, idx, {}, "Error - Browser Restart Failed")
            return driver, wait, False

        except Exception as e:
            print(f"Error processing {vehicle_no}: {e}")
            if retry_count >= max_retries:
                apply_details(df, idx, {}, "Error")
                return driver, wait, False
            retry_count += 1
            new_driver, new_wait = restart_browser_and_continue(driver)
            if not new_driver:
                break
            driver, wait = new_driver, new_wait

    apply_details(df, idx, {}, "Error - Max Retries")
    return driver, wait, False


def scrape_chhattisgarh_all_data(
    input_path,
    output_path,
    sheet_name=0,
    remote_url=None,
):
    if os.path.isfile(output_path):
        print(f"Resuming from existing output: {output_path}")
        df = read_input_table(output_path)
    else:
        if not os.path.isfile(input_path):
            raise FileNotFoundError(f"Not found: {input_path}")
        df = read_input_table(input_path, sheet_name=sheet_name)
        print(f"Loaded {len(df)} rows from {input_path}")

    veh_col = find_vehicle_column(df)
    df = ensure_output_columns(df)
    df[veh_col] = df[veh_col].apply(normalize_vehicle_number)

    pending = []
    skipped_invalid = 0
    for idx, row in df.iterrows():
        if not _needs_scrape(row.get(STATUS_COL, "")):
            continue
        vehicle_no = normalize_vehicle_number(row[veh_col])
        if not vehicle_no or not is_vehicle_number_eligible(vehicle_no):
            skipped_invalid += 1
            continue
        pending.append((idx, vehicle_no))

    print(f"Vehicle column: {veh_col}")
    print(f"Rows to scrape: {len(pending)}")
    if skipped_invalid:
        print(f"Skipped {skipped_invalid} numbers outside lengths 8, 9, or 10")
    if not pending:
        save_results(df, output_path)
        print(f"Nothing to scrape. Wrote {output_path}")
        return df

    _thread_remote_url.value = remote_url
    driver = setup_driver(remote_url)
    if not driver:
        raise RuntimeError("Could not start browser")

    wait = WebDriverWait(driver, WAIT_TIME)
    try:
        nav_success = False
        for nav_attempt in range(1, 6):
            print(f"[Attempt {nav_attempt}/5] Navigating to tax page...")
            if navigate_to_tax_page(driver, wait):
                nav_success = True
                break
            try:
                driver.quit()
            except Exception:
                pass
            time.sleep(5)
            driver = setup_driver(remote_url)
            if not driver:
                break
            wait = WebDriverWait(driver, WAIT_TIME)

        if not nav_success:
            raise RuntimeError("Could not navigate to the Chhattisgarh tax page")

        done = 0
        start_time = time.perf_counter()
        for idx, vehicle_no in pending:
            driver, wait, success = process_single_vehicle(
                driver, wait, vehicle_no, idx, df
            )
            done += 1
            save_results(df, output_path)
            print(f"Progress: {done}/{len(pending)} saved to {output_path}")
            if not success and driver is None:
                break
            time.sleep(1)

        elapsed = time.perf_counter() - start_time
        print(f"Finished in {int(elapsed // 60):02d}:{int(elapsed % 60):02d}")
    finally:
        _thread_remote_url.value = None
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        save_results(df, output_path)

    ok_count = int((df[STATUS_COL] == "OK").sum())
    na_count = int((df[STATUS_COL] == "N/A").sum())
    print(f"OK: {ok_count} | N/A: {na_count} | output: {output_path}")
    return df


if __name__ == "__main__":
    INPUT_PATH = r"test.xlsx"
    OUTPUT_PATH = r"chhattisgarh_vehicle_details.xlsx"
    SHEET_NAME = 0
    REMOTE_URL = None  # e.g. "http://localhost:4444/wd/hub"

    scrape_chhattisgarh_all_data(
        INPUT_PATH,
        OUTPUT_PATH,
        sheet_name=SHEET_NAME,
        remote_url=REMOTE_URL,
    )
