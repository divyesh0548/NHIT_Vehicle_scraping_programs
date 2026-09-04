# Program Info

| Program | Purpose |
|---------|---------|
| `web_scrape_kerala_checkpost.py` | Scrapes Kerala Checkpost Tax portal (parivahan.gov.in) for Gross Vehicle Weight, Unladen Weight, Vehicle Type, and Vehicle Class. Runs for all eligible vehicles (no checkpostmaster pre-lookup). Upserts Gross Vehicle Weight into `checkpostmaster` after scrape and logs whether each weight was **ADDED** or **UPDATED**. Supports Selenium Grid via `USE_SELENIUM_GRID` with local Chrome fallback. Each Grid node writes its own `chunk_XX.xlsx` progress file (saved after every vehicle); files are merged at the end so progress is not lost. Standalone — not wired into `main_experimental_threading.py`. |
| `Output Cleaning/Output_lookup.py` | Compares one column from an input file against one column from an output (lookup) file, then removes from the input every row whose value already appears in the output. Paths and both column names are set as program variables (no CLI args). |
| `Output Cleaning/remove_empty_cell_rows.py` | Deletes rows where a chosen column cell is empty (NaN, blank, whitespace, `"nan"`). Paths and column name are set as program variables. |
| `Merge_output_and_settle_S3.py` | Merges two Excel files (same columns), deletes the existing `output_file_s3_url` object from S3 for a given `nhit_vehicles.id`, uploads the merged file to the same S3 key, and saves the new URL back into `output_file_s3_url`. Paths and record id are set as program variables. |
| `Delete_DB_row_and_file.py` | Deletes a `nhit_vehicles` row by id and removes its `input_file_s3_url` / `output_file_s3_url` objects from S3. Supports `DRY_RUN` to preview deletes. Record id is set as a program variable. |
