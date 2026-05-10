#!/usr/bin/env python3
"""
VPS Auto-Extender
Solves the CAPTCHA pop-up using Gemini Vision and extends the VPS session on a set interval.
"""

import os
import io
import sys
import time
import logging
from pathlib import Path

from dotenv import load_dotenv
import google.generativeai as genai
import PIL.Image
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WEBSITE_URL            = os.getenv("WEBSITE_URL")
LOGIN_URL              = os.getenv("LOGIN_URL") or WEBSITE_URL
SITE_USERNAME          = os.getenv("SITE_USERNAME")
SITE_PASSWORD          = os.getenv("SITE_PASSWORD")

USERNAME_SELECTOR      = os.getenv("USERNAME_SELECTOR",     'input[name="username"]')
PASSWORD_SELECTOR      = os.getenv("PASSWORD_SELECTOR",     'input[name="password"]')
LOGIN_SUBMIT_SELECTOR  = os.getenv("LOGIN_SUBMIT_SELECTOR", 'button[type="submit"]')

EXTEND_BUTTON_SELECTOR = os.getenv("EXTEND_BUTTON_SELECTOR")
CAPTCHA_IMAGE_SELECTOR = os.getenv("CAPTCHA_IMAGE_SELECTOR")
CAPTCHA_INPUT_SELECTOR = os.getenv("CAPTCHA_INPUT_SELECTOR")
CAPTCHA_SUBMIT_SELECTOR= os.getenv("CAPTCHA_SUBMIT_SELECTOR")

GEMINI_API_KEY         = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL           = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_PROMPT          = os.getenv(
    "GEMINI_PROMPT",
    "This is a CAPTCHA image. Read the characters exactly as shown. "
    "Return only those characters with no spaces, punctuation, or explanation."
)

INTERVAL_MINUTES       = int(os.getenv("INTERVAL_MINUTES", "15"))
USER_DATA_DIR          = os.getenv("USER_DATA_DIR", "./browser_data")
HEADLESS               = os.getenv("HEADLESS", "true").lower() != "false"
PROXY_SERVER           = os.getenv("PROXY_SERVER", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("anticaptcha.log"),
    ],
    force=True,
)
# Flush stdout immediately so logs appear in real time (important for systemd/pipes)
sys.stdout.reconfigure(line_buffering=True)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

REQUIRED = {
    "WEBSITE_URL":    WEBSITE_URL,
    "SITE_USERNAME":  SITE_USERNAME,
    "SITE_PASSWORD":  SITE_PASSWORD,
    "GEMINI_API_KEY": GEMINI_API_KEY,
}

def validate_env():
    missing = [k for k, v in REQUIRED.items() if not v]
    if missing:
        log.error("Missing required .env variables: %s", ", ".join(missing))
        sys.exit(1)

# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def build_gemini_model():
    genai.configure(api_key=GEMINI_API_KEY)
    return genai.GenerativeModel(GEMINI_MODEL)


def solve_captcha(model, image_bytes: bytes) -> str:
    image = PIL.Image.open(io.BytesIO(image_bytes))
    response = model.generate_content([GEMINI_PROMPT, image])
    raw = response.text.strip()
    # Keep only digits in case Gemini adds surrounding text
    answer = "".join(c for c in raw if c.isdigit())
    log.info("Gemini raw=%r  cleaned=%s", raw, answer)
    return answer

# ---------------------------------------------------------------------------
# Browser actions
# ---------------------------------------------------------------------------

def is_logged_in(page) -> bool:
    # If the extend button is already visible we're on the dashboard
    try:
        page.wait_for_selector(EXTEND_BUTTON_SELECTOR, timeout=4_000)
        return True
    except PlaywrightTimeout:
        return False


def login(page):
    log.info("Navigating to login page: %s", LOGIN_URL)
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    log.info("Page landed on: %s", page.url)

    if is_logged_in(page):
        log.info("Already logged in — skipping login form.")
        return

    log.info("Waiting for username field (%s)...", USERNAME_SELECTOR)
    try:
        page.wait_for_selector(USERNAME_SELECTOR, timeout=60_000)
    except PlaywrightTimeout:
        page.screenshot(path="login_debug.png")
        log.error("Username field not found after 60s. URL: %s — saved screenshot to login_debug.png", page.url)
        raise

    page.fill(USERNAME_SELECTOR, SITE_USERNAME)
    page.fill(PASSWORD_SELECTOR, SITE_PASSWORD)
    page.click(LOGIN_SUBMIT_SELECTOR)
    page.wait_for_load_state("domcontentloaded")
    log.info("Login submitted.")


def extend_vps(page, model):
    log.info("Navigating to VPS dashboard: %s", WEBSITE_URL)
    page.goto(WEBSITE_URL, wait_until="networkidle")

    log.info("Waiting 2 minutes for dashboard to fully load...")
    page.wait_for_timeout(120_000)
    log.info("Done waiting. Looking for +60 min button...")

    log.info("Clicking +60 min button...")
    page.wait_for_selector(EXTEND_BUTTON_SELECTOR, timeout=20_000)
    page.click(EXTEND_BUTTON_SELECTOR)

    log.info("Waiting for CAPTCHA pop-up...")
    page.wait_for_selector(CAPTCHA_IMAGE_SELECTOR, timeout=20_000)
    # Brief pause so the CAPTCHA image fully renders before we screenshot it
    page.wait_for_timeout(800)

    captcha_el = page.query_selector(CAPTCHA_IMAGE_SELECTOR)
    image_bytes = captcha_el.screenshot()

    answer = solve_captcha(model, image_bytes)
    if not answer:
        raise ValueError("Gemini returned an empty answer — skipping this cycle.")

    page.fill(CAPTCHA_INPUT_SELECTOR, answer)
    page.click(CAPTCHA_SUBMIT_SELECTOR)

    page.wait_for_timeout(3_000)
    log.info("Extension submitted successfully.")

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run():
    validate_env()
    model = build_gemini_model()
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        proxy = {"server": PROXY_SERVER} if PROXY_SERVER else None
        context = p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=HEADLESS,
            proxy=proxy,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = context.new_page()
        page.set_default_timeout(60_000)

        login(page)
        log.info("Logged in. Extending immediately, then every %d minutes.", INTERVAL_MINUTES)

        cycle = 0
        while True:
            cycle += 1
            log.info("--- Extension cycle #%d ---", cycle)
            try:
                extend_vps(page, model)
            except PlaywrightTimeout as e:
                log.error("Timeout: %s — retrying after re-login.", e)
                try:
                    login(page)
                except Exception as re_login_err:
                    log.error("Re-login failed: %s", re_login_err)
            except Exception as e:
                log.error("Unexpected error: %s", e, exc_info=True)
                try:
                    login(page)
                except Exception as re_login_err:
                    log.error("Re-login after error failed: %s", re_login_err)

            log.info("Next extension in %d minutes.", INTERVAL_MINUTES)
            time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    run()
