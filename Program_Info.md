# Program Info

| Program | Purpose |
|---------|---------|
| `web_scrape_kerala_checkpost.py` | Scrapes Kerala Checkpost Tax portal (parivahan.gov.in) for Gross Vehicle Weight, Unladen Weight, Vehicle Type, and Vehicle Class. Runs for all eligible vehicles (no checkpostmaster pre-lookup). Upserts Gross Vehicle Weight into `checkpostmaster` after scrape and logs whether each weight was **ADDED** or **UPDATED**. Supports Selenium Grid via `USE_SELENIUM_GRID` with local Chrome fallback. Each Grid node writes its own `chunk_XX.xlsx` progress file (saved after every vehicle); files are merged at the end so progress is not lost. Standalone — not wired into `main_experimental_threading.py`. |
