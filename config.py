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

# Selenium Grid (Docker hub + optional auto-managed Chrome nodes)
SELENIUM_PROCESSING = os.getenv("SELENIUM_PROCESSING", "false").strip().lower() in {
    "1",
    "true",
    "yes",
}
SELENIUM_REMOTE_URL = os.getenv("SELENIUM_REMOTE_URL", "http://localhost:4444/wd/hub")
MAX_SELENIUM_GRID_NODES = int(os.getenv("MAX_SELENIUM_GRID_NODES", "3"))
SELENIUM_AUTO_MANAGE_NODES = os.getenv("SELENIUM_AUTO_MANAGE_NODES", "true").strip().lower() in {
    "1",
    "true",
    "yes",
}
SELENIUM_NETWORK = os.getenv("SELENIUM_NETWORK", "selenium-grid")
SELENIUM_HUB_CONTAINER = os.getenv("SELENIUM_HUB_CONTAINER", "selenium-hub")
SELENIUM_NODE_IMAGE = os.getenv("SELENIUM_NODE_IMAGE", "selenium/node-chrome:latest")
SELENIUM_NODE_STARTUP_TIMEOUT = int(os.getenv("SELENIUM_NODE_STARTUP_TIMEOUT", "90"))
