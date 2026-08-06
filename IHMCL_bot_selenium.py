from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import time
import requests
import numpy as np
from PIL import Image
import io
from ocr_module import extract_text, special_character_remover
import pandas as pd
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import urllib.request
import json


def setup_driver(headless=False, remote_url=None):
    """Setup and return Chrome WebDriver"""
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)

    if remote_url:
        ensure_grid_ready(remote_url)
        driver = webdriver.Remote(command_executor=remote_url, options=chrome_options)
    else:
        driver = webdriver.Chrome(options=chrome_options)
    return driver


def ensure_grid_ready(remote_url, timeout=5):
    """Fail fast if Selenium Grid is reachable but has no ready nodes."""
    status_url = build_grid_status_url(remote_url)
    try:
        with urllib.request.urlopen(status_url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not reach Selenium Grid status endpoint: {status_url}. {exc}") from exc

    value = payload.get("value", {})
    ready = bool(value.get("ready"))
    nodes = value.get("nodes") or []
    if not ready:
        raise RuntimeError(
            f"Selenium Grid is not ready at {status_url}. "
            f"ready={ready}, registered_nodes={len(nodes)}"
        )


def build_grid_status_url(remote_url):
    """Convert a Grid remote URL like /wd/hub into its /status endpoint."""
    parsed = urlsplit(remote_url)
    path = parsed.path or ""
    if path.endswith("/wd/hub"):
        path = path[:-7]
    if not path.endswith("/status"):
        path = path.rstrip("/") + "/status"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def get_captcha_image(driver, captcha_element):
    """
    Get captcha image from the element and convert to numpy array.
    Returns numpy array of the image.
    """
    try:
        # Method 1: Try to get image from src URL with session cookies
        captcha_src = captcha_element.get_attribute("src")
        
        if captcha_src:
            # Construct full URL if relative
            if captcha_src.startswith("/"):
                # Absolute path from domain root
                from urllib.parse import urljoin
                image_url = urljoin(driver.current_url, captcha_src)
            elif captcha_src.startswith("http"):
                image_url = captcha_src
            else:
                # Relative path without leading slash
                from urllib.parse import urljoin
                image_url = urljoin(driver.current_url, captcha_src)
            
            # Create a session and add cookies
            session = requests.Session()
            selenium_cookies = driver.get_cookies()
            for cookie in selenium_cookies:
                session.cookies.set(cookie['name'], cookie['value'])
            
            # Download the image
            headers = {
                'User-Agent': driver.execute_script("return navigator.userAgent;"),
                'Referer': driver.current_url
            }
            response = session.get(image_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                # Convert to PIL Image then to numpy array
                img = Image.open(io.BytesIO(response.content))
                # Convert to RGB if needed
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img_array = np.array(img)
                return img_array
            else:
                print(f"Failed to download image. Status code: {response.status_code}")
                # Fallback to screenshot method
                return get_captcha_from_screenshot(driver, captcha_element)
        else:
            # No src attribute, use screenshot method
            return get_captcha_from_screenshot(driver, captcha_element)
            
    except Exception as e:
        print(f"Error getting captcha from URL: {e}")
        # Fallback to screenshot method
        return get_captcha_from_screenshot(driver, captcha_element)


def get_captcha_from_screenshot(driver, captcha_element):
    """Take screenshot of captcha element and convert to numpy array"""
    temp_file = None
    try:
        # Use a unique temp file so parallel sessions do not overwrite each other.
        fd, temp_file = tempfile.mkstemp(prefix="ihmcl_captcha_", suffix=".png")
        os.close(fd)
        captcha_element.screenshot(temp_file)
        
        # Read the image and convert to numpy array
        img = Image.open(temp_file)
        # Convert to RGB if needed (screenshots might be RGBA)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img_array = np.array(img)
        
        return img_array
    except Exception as e:
        print(f"Error taking screenshot: {e}")
        raise
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass


def select_plaza_from_dropdown(driver, plaza_name="Phulwaria Toll Plaza"):
    """Select plaza from dropdown"""
    # Click on the dropdown element with placeholder "Search for the Plaza"
    print(f"Clicking on plaza dropdown to select '{plaza_name}'...")
    # Try multiple selectors for the dropdown
    dropdown_selectors = [
        "span.select2-selection__placeholder",
        "span.select2-selection",
        ".select2-selection__rendered"
    ]
    dropdown = None
    for selector in dropdown_selectors:
        try:
            dropdown = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
            )
            break
        except:
            continue
    
    if not dropdown:
        raise Exception("Could not find dropdown element")
    
    dropdown.click()
    time.sleep(2)  # Wait for dropdown options to appear
    
    # Select the plaza option
    option_selectors = [
        f"//li[contains(text(), '{plaza_name}')]",
        f"//li[@class='select2-results__option' and contains(text(), '{plaza_name.split()[0]}')]",
        "(//li[@class='select2-results__option'])[1]"  # First option as fallback
    ]
    plaza_option = None
    for xpath in option_selectors:
        try:
            plaza_option = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            break
        except:
            continue
    
    if not plaza_option:
        raise Exception(f"Could not find '{plaza_name}' option")
    
    plaza_option.click()
    time.sleep(2)  # Wait for selection to be applied
    print(f"Successfully selected '{plaza_name}'!")


def extract_table_data_to_excel(driver, excel_file="ihmcl_vehicle_data.xlsx"):
    """Extract table data and save to Excel file. Headers are written only once. Returns True if successful, False if table doesn't appear."""
    try:
        print("Waiting for table to appear...")
        # Wait for table to appear (table has id="tbody_Tag_VehicleDetails")
        # Use longer timeout and check for table
        try:
            table = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table#tbody_Tag_VehicleDetails"))
            )
        except Exception as e:
            print(f"ERROR: Table did not appear after clicking search button!")
            print("This might indicate website loading issues or network problems.")
            return False
        
        time.sleep(2)  # Wait for table to fully load
        
        # Extract headers from thead
        print("Extracting table headers...")
        thead = table.find_element(By.TAG_NAME, "thead")
        header_cells = thead.find_elements(By.TAG_NAME, "th")
        headers = [cell.text.strip() for cell in header_cells]
        print(f"Headers: {headers}")
        
        # Find the index of 'Tag Status' column
        try:
            tag_status_index = headers.index('Tag Status')
            print(f"Found 'Tag Status' column at index {tag_status_index}")
        except ValueError:
            print("Warning: 'Tag Status' column not found in headers. Extracting all rows.")
            tag_status_index = None
        
        # Extract rows from tbody
        print("Extracting table rows (filtering for Tag Status = 'A')...")
        tbody = table.find_element(By.TAG_NAME, "tbody")
        rows = tbody.find_elements(By.TAG_NAME, "tr")
        
        data_rows = []
        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            row_data = []
            for cell in cells:
                # Check if cell contains a button
                button = cell.find_elements(By.TAG_NAME, "input")
                if button:
                    # If button exists, get its value attribute
                    cell_text = button[0].get_attribute("value") or ""
                else:
                    cell_text = cell.text.strip()
                row_data.append(cell_text)
            
            # Only add row if it has the correct number of columns and has some data
            if len(row_data) == len(headers):
                # Check if row has at least one non-empty cell (excluding first column which might be empty)
                if any(str(cell).strip() for cell in row_data[1:] if cell):
                    # Filter: Only add rows where Tag Status is 'A'
                    if tag_status_index is not None:
                        tag_status_value = str(row_data[tag_status_index]).strip()
                        if tag_status_value == 'A':
                            data_rows.append(row_data)
                            print(f"Added row with Tag Status = '{tag_status_value}'")
                        else:
                            print(f"Skipped row with Tag Status = '{tag_status_value}' (not 'A')")
                    else:
                        # If Tag Status column not found, add all rows
                        data_rows.append(row_data)
        
        print(f"Extracted {len(data_rows)} rows with Tag Status = 'A'")
        
        # Create DataFrame
        df = pd.DataFrame(data_rows, columns=headers)
        
        # Check if Excel file exists
        if os.path.exists(excel_file):
            # Read existing data
            existing_df = pd.read_excel(excel_file)
            # Append new data
            combined_df = pd.concat([existing_df, df], ignore_index=True)
            # Remove duplicates if any
            combined_df = combined_df.drop_duplicates()
            # Write to Excel
            combined_df.to_excel(excel_file, index=False)
            print(f"Appended {len(df)} rows to existing Excel file: {excel_file}")
        else:
            # Create new Excel file with headers
            df.to_excel(excel_file, index=False)
            print(f"Created new Excel file with {len(df)} rows: {excel_file}")
        
        return True
        
    except Exception as e:
        print(f"Error extracting table data: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def find_vehicle_number_column(df, alternative_names=None):
    """Find vehicle number column in DataFrame using alternative names"""
    if alternative_names is None:
        alternative_names = [
            'vehicle number', 'vehicle_number', 'vehiclenumber', 'vehicle no', 
            'vehicle_no', 'vehicleno', 'vrn', 'registration number', 
            'registration_number', 'reg number', 'reg_number', 'regno'
        ]
    
    # Normalize column names (case-insensitive, remove spaces/special chars)
    df_columns_lower = [str(col).lower().strip().replace(' ', '_').replace('-', '_') for col in df.columns]
    alternative_names_lower = [name.lower().strip().replace(' ', '_').replace('-', '_') for name in alternative_names]
    
    for alt_name in alternative_names_lower:
        if alt_name in df_columns_lower:
            idx = df_columns_lower.index(alt_name)
            return df.columns[idx]
    
    # If not found, return None
    return None


def enter_mobile_number_if_empty(driver, mobile_number="9999999999"):
    """Check if mobile number input is empty, if so enter it"""
    try:
        mobile_input = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "txtMobileNumber"))
        )
        
        current_value = mobile_input.get_attribute("value") or ""
        if not current_value.strip():
            print(f"Mobile number field is empty. Entering mobile number: {mobile_number}")
            mobile_input.clear()
            mobile_input.send_keys(mobile_number)
            time.sleep(1)
        else:
            print(f"Mobile number already filled: {current_value}")
        
        return True
    except Exception as e:
        print(f"Error checking/entering mobile number: {str(e)}")
        return False


def enter_vehicle_number_and_search(driver, vehicle_number, excel_file="ihmcl_vehicle_data.xlsx"):
    """Enter vehicle number and click search button, then extract table data. Returns True if successful, False if table doesn't appear."""
    try:
        print(f"Processing vehicle number: {vehicle_number}")
        
        # Wait for vehicle number input field
        vehicle_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "txt_Tag_VehicleDetails"))
        )
        
        # Enter vehicle number
        print(f"Entering vehicle number: {vehicle_number}")
        vehicle_input.clear()
        vehicle_input.send_keys(vehicle_number)
        time.sleep(1)
        
        # Click search button
        print("Clicking search button...")
        search_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "btnSearch"))
        )
        search_button.click()
        time.sleep(3)  # Wait for search to process
        
        print("Successfully entered vehicle number and clicked search!")
        
        # Extract table data to Excel - this will return False if table doesn't appear
        success = extract_table_data_to_excel(driver, excel_file)
        
        if not success:
            print("WARNING: Table did not appear after search. This may require restarting the flow.")
            raise Exception("Table did not appear after clicking search button")
        
        return True
        
    except Exception as e:
        print(f"Error entering vehicle number and searching: {str(e)}")
        import traceback
        traceback.print_exc()
        # Re-raise exception to trigger restart mechanism
        raise


def enter_mobile_and_vehicle_details(driver, mobile_number="9999999999", vehicle_number="KA01C4746", excel_file="ihmcl_vehicle_data.xlsx"):
    """Enter mobile number and vehicle number, then click search button and extract table data"""
    try:
        print("Waiting for mobile number and vehicle number input fields...")
        
        # Check and enter mobile number if empty
        enter_mobile_number_if_empty(driver, mobile_number)
        
        # Wait for vehicle number input field
        vehicle_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "txt_Tag_VehicleDetails"))
        )
        
        # Enter vehicle number
        print(f"Entering vehicle number: {vehicle_number}")
        vehicle_input.clear()
        vehicle_input.send_keys(vehicle_number)
        time.sleep(1)
        
        # Click search button
        print("Clicking search button...")
        search_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "btnSearch"))
        )
        search_button.click()
        time.sleep(3)  # Wait for search to process
        
        print("Successfully entered mobile and vehicle details and clicked search!")
        
        # Extract table data to Excel
        extract_table_data_to_excel(driver, excel_file)
        
        return True
        
    except Exception as e:
        print(f"Error entering mobile and vehicle details: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def solve_captcha(driver):
    """Solve captcha and submit form. Returns True if successful, False otherwise."""
    try:
        # Store current URL to check if page redirects after submission
        current_url_before = driver.current_url
        
        # Wait for captcha image to appear
        print("Waiting for captcha image...")
        captcha_img = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.row img[alt='Captcha']"))
        )
        
        # Download and process captcha image
        print("Downloading captcha image...")
        captcha_array = get_captcha_image(driver, captcha_img)
        
        # Extract text using OCR
        print("Extracting captcha text using OCR...")
        extracted_text_list = extract_text(captcha_array)
        extracted_text = " ".join(extracted_text_list)
        
        # Clean the text
        cleaned_text = special_character_remover(extracted_text)
        print(f"Extracted captcha text: {cleaned_text}")
        
        if not cleaned_text:
            print("Warning: No text extracted from captcha!")
            return False
        
        # Enter captcha text in the input field
        print("Entering captcha text...")
        captcha_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "UserCaptchaCode"))
        )
        captcha_input.clear()
        captcha_input.send_keys(cleaned_text)
        time.sleep(1)
        
        # Click submit button
        print("Clicking submit button...")
        submit_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input.btnSubmit"))
        )
        # Check if button is disabled
        if submit_button.get_attribute("disabled"):
            print("Submit button is disabled. Waiting for it to be enabled...")
            WebDriverWait(driver, 10).until(
                lambda d: not submit_button.get_attribute("disabled")
            )
        
        submit_button.click()
        time.sleep(3)  # Wait for page to process
        
        # Check if page redirected (indicates success)
        current_url_after = driver.current_url
        if current_url_after != current_url_before:
            print("Page redirected - captcha was correct!")
            return True
        
        # Check if captcha was wrong
        try:
            error_element = driver.find_element(By.ID, "WrongCaptchaError")
            error_style = error_element.get_attribute("style") or ""
            
            # Check if error message is visible (display: inline)
            if "display: inline" in error_style:
                print("Captcha was incorrect!")
                return False
            else:
                # Error element exists but is not visible, captcha might be correct
                print("Captcha appears to be correct (no error message visible)!")
                return True
        except Exception as e:
            # If error element is not found, check if we're still on the form page
            # If form elements are gone, might have succeeded
            try:
                driver.find_element(By.ID, "UserCaptchaCode")
                # Still on form page, but no error - might be success
                print("No error element found and still on form page - assuming success")
                return True
            except:
                # Form elements gone, likely redirected or page changed
                print("Form elements not found - assuming successful submission")
                return True
            
    except Exception as e:
        print(f"Error solving captcha: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def process_vehicles_flow(driver, df, vehicle_col, pending_df, mobile_number, output_excel_file, input_excel_file):
    """Process vehicles in the flow. Returns True if all processed, False if error occurred requiring restart."""
    try:
        first_vehicle = True
        vehicle_count = 0
        
        for idx, row in pending_df.iterrows():
            vehicle_number = str(row[vehicle_col]).strip()
            
            if not vehicle_number or vehicle_number.lower() in ['nan', 'none', '']:
                print(f"Skipping row {idx}: Empty vehicle number")
                continue
            
            vehicle_count += 1
            print(f"\n{'='*60}")
            print(f"Processing vehicle {vehicle_count} of {len(pending_df)}: {vehicle_number}")
            print(f"{'='*60}")
            
            try:
                if first_vehicle:
                    # For first vehicle, enter mobile and vehicle number
                    print("First vehicle - entering mobile and vehicle number...")
                    enter_mobile_number_if_empty(driver, mobile_number)
                    success = enter_vehicle_number_and_search(driver, vehicle_number, output_excel_file)
                    first_vehicle = False
                else:
                    # For subsequent vehicles, just update vehicle number
                    print("Updating vehicle number (no page reload)...")
                    enter_mobile_number_if_empty(driver, mobile_number)
                    success = enter_vehicle_number_and_search(driver, vehicle_number, output_excel_file)
                
                if success:
                    # Mark as processed
                    df.at[idx, 'processed'] = 'Done'
                    print(f"✓ Successfully processed vehicle: {vehicle_number}")
                    
                    # Save progress to input file after each vehicle
                    df.to_excel(input_excel_file, index=False)
                    print(f"Saved progress to {input_excel_file}")
                else:
                    print(f"✗ Failed to process vehicle: {vehicle_number}")
                    
            except Exception as e:
                error_msg = str(e)
                print(f"ERROR processing vehicle {vehicle_number}: {error_msg}")
                
                # Check if it's a table loading error
                if "Table did not appear" in error_msg or "table" in error_msg.lower():
                    print("\n" + "="*60)
                    print("CRITICAL ERROR: Table did not appear after search!")
                    print("This indicates website loading issues.")
                    print("Will close browser and restart flow for remaining vehicles.")
                    print("="*60)
                    # Return False to trigger restart
                    return False
                
                # For other errors, continue with next vehicle
                import traceback
                traceback.print_exc()
        
        return True
        
    except Exception as e:
        print(f"Error in process_vehicles_flow: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def navigate_and_solve_captcha(driver, plaza_name, max_attempts=5):
    """Navigate to website and solve captcha. Returns True if successful."""
    try:
        # Navigate to the initial URL
        print("Navigating to IHMCL FASTag portal...")
        driver.get("https://ihmcl.co.in/fastag-user/")
        time.sleep(2)  # Wait for page to load
        
        # Click on "Buy Monthly FASTag Pass" button
        print("Clicking on 'Buy Monthly FASTag Pass' button...")
        buy_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.LINK_TEXT, "Buy Monthly FASTag Pass"))
        )
        buy_button.click()
        time.sleep(3)  # Wait for new page to load
        
        # Retry loop for captcha solving
        captcha_solved = False
        for attempt in range(1, max_attempts + 1):
            print(f"\n=== Captcha Attempt {attempt} of {max_attempts} ===")
            
            try:
                # Select plaza from dropdown
                select_plaza_from_dropdown(driver, plaza_name)
                
                # Solve captcha and submit
                success = solve_captcha(driver)
                
                if success:
                    print("Successfully solved captcha and submitted form!")
                    captcha_solved = True
                    # Wait for next page to load
                    time.sleep(2)
                    return True
                else:
                    if attempt < max_attempts:
                        print(f"Captcha failed. Reloading page for retry {attempt + 1}...")
                        # Reload the page
                        driver.refresh()
                        time.sleep(3)  # Wait for page to reload
                    else:
                        print("Max attempts reached. Failed to solve captcha.")
                        return False
                        
            except Exception as e:
                print(f"Error in attempt {attempt}: {str(e)}")
                if attempt < max_attempts:
                    print("Reloading page and retrying...")
                    driver.refresh()
                    time.sleep(3)
                else:
                    raise
        
        return captcha_solved
        
    except Exception as e:
        print(f"Error navigating and solving captcha: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def main(
    input_excel_file="input_vehicles.xlsx",
    output_excel_file="ihmcl_vehicle_data.xlsx",
    vehicle_column_names=None,
    mobile_number="9999999999",
    plaza_name="Phulwaria Toll Plaza",
    remote_url=None,
    headless=False,
):
    """
    Main function to automate IHMCL FASTag portal for multiple vehicle numbers
    
    Args:
        input_excel_file: Path to input Excel file with vehicle numbers
        output_excel_file: Path to output Excel file for extracted data
        vehicle_column_names: List of alternative column names for vehicle number
        mobile_number: Mobile number to use
        plaza_name: Plaza name to select
    """
    driver = None
    max_attempts = 5
    max_restarts = 3  # Maximum number of times to restart the flow
    restart_count = 0
    
    try:
        # Read input Excel file
        print(f"Reading input Excel file: {input_excel_file}")
        if not os.path.exists(input_excel_file):
            print(f"Error: Input file '{input_excel_file}' not found!")
            return
        
        df = pd.read_excel(input_excel_file)
        print(f"Loaded {len(df)} rows from input file")
        
        # Find vehicle number column
        vehicle_col = find_vehicle_number_column(df, vehicle_column_names)
        if vehicle_col is None:
            print("Error: Could not find vehicle number column in input file!")
            print(f"Available columns: {list(df.columns)}")
            return
        
        print(f"Found vehicle number column: '{vehicle_col}'")
        
        # Add 'processed' column if it doesn't exist and persist it immediately
        # (otherwise a captcha failure before any save leaves the Excel without this column)
        if 'processed' not in df.columns:
            df['processed'] = ''
            print("Added 'processed' column to input file")
            df.to_excel(input_excel_file, index=False)

        def _pending_mask(frame):
            """Rows not yet marked Done (handles missing/NaN processed values)."""
            if 'processed' not in frame.columns:
                return pd.Series(True, index=frame.index)
            status = frame['processed'].fillna('').astype(str).str.strip().str.lower()
            return status != 'done'

        # Main processing loop with restart capability
        while restart_count <= max_restarts:
            # Filter out already processed vehicles
            pending_df = df[_pending_mask(df)].copy()
            print(f"\n{'='*60}")
            print(f"Found {len(pending_df)} vehicles to process (out of {len(df)} total)")
            print(f"{'='*60}")
            
            if len(pending_df) == 0:
                print("All vehicles have been processed. Exiting.")
                break
            
            # Close previous browser session if exists
            if driver:
                try:
                    print("Closing previous browser session...")
                    driver.quit()
                    time.sleep(2)
                except:
                    pass
            
            # Setup Chrome driver
            print("Setting up Chrome driver...")
            driver = setup_driver(headless=headless, remote_url=remote_url)
            driver.maximize_window()
            
            # Navigate and solve captcha
            captcha_success = navigate_and_solve_captcha(driver, plaza_name, max_attempts)
            
            if not captcha_success:
                print("Failed to solve captcha. Exiting.")
                break
            
            # Process vehicles
            flow_success = process_vehicles_flow(driver, df, vehicle_col, pending_df, mobile_number, 
                                                  output_excel_file, input_excel_file)
            
            if flow_success:
                print("\nAll vehicles processed successfully in this session!")
                break
            else:
                restart_count += 1
                print(f"\n{'='*60}")
                print(f"Restarting flow (attempt {restart_count} of {max_restarts})...")
                print(f"{'='*60}")
                
                # Save current progress before restarting
                df.to_excel(input_excel_file, index=False)
                print("Progress saved before restart.")
                
                if restart_count > max_restarts:
                    print("Max restart attempts reached. Stopping.")
                    break
        
        # Final check for remaining vehicles
        print(f"\n{'='*60}")
        print("Performing final check for remaining vehicles...")
        print(f"{'='*60}")
        
        df_final = pd.read_excel(input_excel_file)
        if 'processed' not in df_final.columns:
            df_final['processed'] = ''
            df_final.to_excel(input_excel_file, index=False)

        remaining_df = df_final[_pending_mask(df_final)].copy()
        
        if len(remaining_df) > 0:
            print(f"Found {len(remaining_df)} remaining vehicles to process.")
            print("Attempting one final processing session...")
            
            # Close browser if still open
            if driver:
                try:
                    driver.quit()
                    time.sleep(2)
                except:
                    pass
            
            # One more attempt
            driver = setup_driver(headless=headless, remote_url=remote_url)
            driver.maximize_window()
            
            captcha_success = navigate_and_solve_captcha(driver, plaza_name, max_attempts)
            if captcha_success:
                process_vehicles_flow(driver, df_final, vehicle_col, remaining_df, mobile_number, 
                                      output_excel_file, input_excel_file)
                df_final.to_excel(input_excel_file, index=False)
        
        # Final save of input file
        final_df = pd.read_excel(input_excel_file)
        if 'processed' not in final_df.columns:
            final_df['processed'] = ''
        final_df.to_excel(input_excel_file, index=False)
        
        print(f"\n{'='*60}")
        print("Processing complete!")
        print(f"Final progress saved to {input_excel_file}")
        print(f"Extracted data saved to {output_excel_file}")
        print(f"{'='*60}")
        
        # Keep browser open for a few seconds to verify
        print("\nWaiting 10 seconds before closing browser...")
        time.sleep(10)
        
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        import traceback
        traceback.print_exc()
        
    finally:
        if driver:
            print("Closing browser...")
            try:
                driver.quit()
            except:
                pass


def scrape_ihmcl_for_dataframe(
    df_input: pd.DataFrame,
    vehicle_column_names=None,
    mobile_number: str = "9999999999",
    plaza_name: str = "Phulwaria Toll Plaza",
    remote_url: str = None,
    headless: bool = False,
):
    """
    Wrapper to run the IHMCL scraping flow using an in-memory DataFrame.

    - Takes a DataFrame with vehicle numbers as input
    - Runs the existing automation logic using temporary Excel files internally
    - Returns a DataFrame with the scraped IHMCL data (same structure as the usual output Excel)
    """
    # Use unique temporary filenames so parallel sessions do not collide.
    tmp_dir = tempfile.mkdtemp(prefix="ihmcl_")
    tmp_input_file = str(Path(tmp_dir) / "input.xlsx")
    tmp_output_file = str(Path(tmp_dir) / "output.xlsx")

    # Make a copy so we don't mutate the caller's DataFrame
    df = df_input.copy()
    # Ensure we write something even if empty
    df.to_excel(tmp_input_file, index=False)

    # Default alternative vehicle column names (same as CLI usage)
    if vehicle_column_names is None:
        vehicle_column_names = [
            "Veh Reg No.",
            "vehicle_number",
            "vehiclenumber",
            "vehicle no",
            "vehicle_no",
            "vehicleno",
            "vrn",
            "registration number",
            "registration_number",
            "reg number",
            "reg_number",
            "regno",
            "vehicle",
            "veh_no",
            "veh_number",
            "Veh Reg No",
        ]

    try:
        # Reuse existing main flow, but with our temporary files
        main(
            input_excel_file=tmp_input_file,
            output_excel_file=tmp_output_file,
            vehicle_column_names=vehicle_column_names,
            mobile_number=mobile_number,
            plaza_name=plaza_name,
            remote_url=remote_url,
            headless=headless,
        )

        # Read the resulting output Excel into a DataFrame
        if os.path.exists(tmp_output_file):
            result_df = pd.read_excel(tmp_output_file)
        else:
            print(f"[IHMCL] Output file {tmp_output_file} not found, returning empty DataFrame")
            result_df = pd.DataFrame()

        return result_df
    finally:
        # Best-effort cleanup of temporary files
        for path in (tmp_input_file, tmp_output_file):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                # If cleanup fails, it's non-fatal
                pass
        try:
            os.rmdir(tmp_dir)
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    
    # Default values
    input_file = "test.xlsx"
    output_file = "ihmcl_vehicle_data.xlsx"
    mobile = "9999999999"
    plaza = "Phulwaria Toll Plaza"
    
    # Allow command line arguments
    if len(sys.argv) > 1:
        input_file = sys.argv[1]
    if len(sys.argv) > 2:
        output_file = sys.argv[2]
    if len(sys.argv) > 3:
        mobile = sys.argv[3]
    if len(sys.argv) > 4:
        plaza = sys.argv[4]
    
    # Alternative vehicle column names (can be customized)
    vehicle_column_alternatives = [
        'Veh Reg No.', 'vehicle_number', 'vehiclenumber', 'vehicle no', 
        'vehicle_no', 'vehicleno', 'vrn', 'registration number', 
        'registration_number', 'reg number', 'reg_number', 'regno',
        'vehicle', 'veh_no', 'veh_number'
    ]
    
    main(
        input_excel_file=input_file,
        output_excel_file=output_file,
        vehicle_column_names=vehicle_column_alternatives,
        mobile_number=mobile,
        plaza_name=plaza
    )

