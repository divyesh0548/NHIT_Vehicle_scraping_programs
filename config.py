import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

AWS_S3_BUCKET_NAME = os.getenv("AWS_S3_BUCKET_NAME")
AWS_REGION = os.getenv("AWS_REGION")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

# ---------------------------------------------------------------------------
# Selenium browser mode
#   SELENIUM_MODE=local  -> single local Chrome (no Grid)
#   SELENIUM_MODE=grid   -> Selenium Grid at SELENIUM_REMOTE_URL
# Legacy: SELENIUM_PROCESSING=true/false still works when SELENIUM_MODE is unset.
# ---------------------------------------------------------------------------
_raw_mode = (os.getenv("SELENIUM_MODE") or "").strip().lower()
_legacy_processing = os.getenv("SELENIUM_PROCESSING", "false").strip().lower() in {
    "1",
    "true",
    "yes",
}
if _raw_mode in {"local", "chrome"}:
    SELENIUM_MODE = "local"
elif _raw_mode in {"grid", "remote", "hub"}:
    SELENIUM_MODE = "grid"
elif _raw_mode:
    raise ValueError(
        f"Invalid SELENIUM_MODE={_raw_mode!r}. Use 'local' or 'grid'."
    )
else:
    SELENIUM_MODE = "grid" if _legacy_processing else "local"

# True when Grid / hub should be used (keeps older imports working)
SELENIUM_PROCESSING = SELENIUM_MODE == "grid"

# Hub WebDriver URL (not the /ui/ page). Examples:
#   local Docker hub:  http://localhost:4444/wd/hub
#   LAN hosted hub:    http://192.168.1.200:4444/wd/hub
SELENIUM_REMOTE_URL = (
    os.getenv("SELENIUM_REMOTE_URL") or "http://localhost:4444/wd/hub"
).strip()

MAX_SELENIUM_GRID_NODES = max(1, int(os.getenv("MAX_SELENIUM_GRID_NODES", "3")))
SELENIUM_AUTO_MANAGE_NODES = os.getenv("SELENIUM_AUTO_MANAGE_NODES", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
SELENIUM_NETWORK = os.getenv("SELENIUM_NETWORK", "selenium-grid")
SELENIUM_HUB_CONTAINER = os.getenv("SELENIUM_HUB_CONTAINER", "selenium-hub")
SELENIUM_NODE_IMAGE = os.getenv("SELENIUM_NODE_IMAGE", "selenium/node-chrome:latest")
SELENIUM_NODE_STARTUP_TIMEOUT = int(os.getenv("SELENIUM_NODE_STARTUP_TIMEOUT", "90"))
# Chrome sessions per auto-managed node (Grid UI: Max. Concurrency)
SE_NODE_MAX_SESSIONS = max(1, int(os.getenv("SE_NODE_MAX_SESSIONS", "1")))
SE_NODE_OVERRIDE_MAX_SESSIONS = os.getenv(
    "SE_NODE_OVERRIDE_MAX_SESSIONS", "true"
).strip().lower() in {"1", "true", "yes"}
# Shared memory for Chrome inside each auto-managed node container (e.g. 2g)
SELENIUM_NODE_SHM_SIZE = (os.getenv("SELENIUM_NODE_SHM_SIZE") or "2g").strip() or "2g"

# Total parallel browsers the scraper will open.
# Prefer explicit SELENIUM_MAX_PARALLEL_SESSIONS (needed for uneven remote nodes
# e.g. 10 + 10 + 50 = 70). Falls back to nodes x sessions-per-node.
_parallel_raw = (os.getenv("SELENIUM_MAX_PARALLEL_SESSIONS") or "").strip()
if _parallel_raw:
    SELENIUM_MAX_PARALLEL_SESSIONS = max(1, int(_parallel_raw))
else:
    SELENIUM_MAX_PARALLEL_SESSIONS = MAX_SELENIUM_GRID_NODES * SE_NODE_MAX_SESSIONS

# Kerala Registration Date / Fuel scraper input
# Single .xlsx workbook or a folder containing .xlsx workbooks
REG_DATE_FUEL_EXCEL_PATH = (os.getenv("REG_DATE_FUEL_EXCEL_PATH") or "").strip()

# Kerala checkpost scraper input (.xlsx workbook)
KERALA_CHECKPOST_EXCEL_PATH = (os.getenv("KERALA_CHECKPOST_EXCEL_PATH") or "").strip()
