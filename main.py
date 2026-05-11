#!/usr/bin/env python3
"""
VPS Auto-Extender
Solves the CAPTCHA pop-up using Gemini Vision and extends the VPS session on a set interval.
"""

import os
import io
import re
import sys
import time
import select as _select
import socket
import random
import logging
import socketserver as _socketserver
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests

from dotenv import load_dotenv
import google.generativeai as genai
import PIL.Image
from patchright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

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
CAPSOLVER_API_KEY      = os.getenv("CAPSOLVER_API_KEY", "")

_STEALTH_JS = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
    "if(!window.chrome){window.chrome={runtime:{},loadTimes:function(){},csi:function(){},app:{}}}"
)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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

def _capsolver_get_turnstile_token(page_url: str, site_key: str) -> str | None:
    """Ask CapSolver to solve a Cloudflare Turnstile challenge and return the token."""
    try:
        r = requests.post("https://api.capsolver.com/createTask", json={
            "clientKey": CAPSOLVER_API_KEY,
            "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": page_url,
                "websiteKey": site_key,
                "metadata": {"action": ""},
            },
        }, timeout=15)
        task_id = r.json().get("taskId")
        if not task_id:
            log.error("CapSolver createTask failed: %s", r.json())
            return None
        log.info("CapSolver task %s created, polling for result...", task_id)
        for _ in range(40):
            time.sleep(3)
            res = requests.post("https://api.capsolver.com/getTaskResult", json={
                "clientKey": CAPSOLVER_API_KEY,
                "taskId": task_id,
            }, timeout=10).json()
            if res.get("status") == "ready":
                token = res["solution"].get("token")
                log.info("CapSolver token received: %s...", token[:30] if token else None)
                return token
            if res.get("status") == "failed":
                log.error("CapSolver task failed: %s", res)
                return None
    except Exception as e:
        log.error("CapSolver error: %s", e)
    return None


def _wait_for_cf_frame(page, timeout_ms=25_000):
    """Poll page.frames until a challenges.cloudflare.com frame appears."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for frame in page.frames:
            if "challenges.cloudflare.com" in (frame.url or ""):
                return frame
        page.wait_for_timeout(500)
    return None


def _click_cf_challenge(page) -> bool:
    """Click the Cloudflare 'Are you human?' Turnstile widget, if present."""
    page.mouse.move(random.randint(80, 500), random.randint(80, 350))
    page.wait_for_timeout(random.randint(300, 700))

    cf_frame = _wait_for_cf_frame(page)
    if not cf_frame:
        log.info("CF — no iframe after 25s. frames: %s", [f.url for f in page.frames])
    else:
        log.info("CF iframe ready: %s", cf_frame.url)

        # ── CapSolver path (preferred when API key is configured) ────────────
        if CAPSOLVER_API_KEY:
            m = re.search(r'/(0x[0-9A-Za-z]+)/', cf_frame.url or "")
            site_key = m.group(1) if m else None
            if site_key:
                token = _capsolver_get_turnstile_token(page.url, site_key)
                if token:
                    submitted = page.evaluate("""(token) => {
                        const inp = document.querySelector('[name="cf-turnstile-response"]');
                        if (inp) inp.value = token;
                        const form = document.getElementById('challenge-form');
                        if (form) { form.submit(); return true; }
                        return false;
                    }""", token)
                    if submitted:
                        log.info("CF challenge form submitted with CapSolver token.")
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=30_000)
                        except Exception:
                            pass
                        return True
                    log.warning("CF form not found on page after getting token.")

        # ── Fallback: direct click attempts ──────────────────────────────────
        try:
            cf_frame.wait_for_load_state("domcontentloaded", timeout=8_000)
        except Exception:
            pass
        for sel in (".ctp-checkbox-label", "[role='checkbox']", "input[type='checkbox']",
                    "label", "div[tabindex]", "button"):
            try:
                cf_frame.locator(sel).first.click(timeout=4_000, force=True)
                log.info("Clicked CF iframe (%s).", sel)
                return True
            except Exception:
                pass
        try:
            cf_frame.evaluate("document.body.dispatchEvent(new MouseEvent('click', {bubbles:true}))")
            log.info("JS-dispatched click on CF iframe body.")
            return True
        except Exception as e:
            log.warning("JS body click failed: %s", e)
        log.warning("CF iframe present but no element clicked.")

    # Fallback: elements on the main challenge page
    for sel in ("button:has-text('human')", "#challenge-form button", "input[type='checkbox']"):
        try:
            page.locator(sel).first.click(timeout=2_000)
            log.info("Clicked main-page CF element (%s).", sel)
            return True
        except Exception:
            pass

    log.warning("CF click: no matching element found.")
    return False


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

    # Poll until Cloudflare challenge clears (up to 90 s), clicking it each iteration
    cf_deadline = time.time() + 90
    while "just a moment" in page.title().lower():
        if time.time() > cf_deadline:
            log.warning("CF challenge did not clear after 90s.")
            break
        _click_cf_challenge(page)
        page.wait_for_timeout(3_000)
    log.info("CF wait done. URL=%s  title=%r", page.url, page.title())

    if page.url.rstrip("/") != LOGIN_URL.rstrip("/"):
        log.info("Not on login page — already redirected to dashboard, skipping login.")
        return

    log.info("Still on login page, proceeding with login...")
    try:
        page.wait_for_selector(USERNAME_SELECTOR, timeout=60_000)
    except PlaywrightTimeout:
        page.screenshot(path="login_debug.png")
        log.error("Username field not found. URL: %s  title=%r — saved screenshot to login_debug.png",
                  page.url, page.title())
        raise

    page.fill(USERNAME_SELECTOR, SITE_USERNAME)
    page.fill(PASSWORD_SELECTOR, SITE_PASSWORD)
    page.click(LOGIN_SUBMIT_SELECTOR)
    page.wait_for_load_state("domcontentloaded")
    log.info("Login submitted.")


def extend_vps(page, model):
    log.info("Navigating to VPS dashboard: %s", WEBSITE_URL)
    page.goto(WEBSITE_URL, wait_until="networkidle")

    log.info("Waiting 45 seconds for dashboard to fully load...")
    page.wait_for_timeout(45_000)
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

# Hostname → IP cache populated at startup; shared with the proxy handler
# so it never needs to call DNS from a background thread.
_RESOLVED_IPS: dict[str, str] = {}


def _resolve_hostnames(urls: list) -> dict:
    """Resolve unique hostnames from a list of URLs or bare hostnames."""
    seen: dict[str, str] = {}
    for u in urls:
        if not u:
            continue
        parsed = urlparse(u)
        host = parsed.hostname or (u if "." in u and "/" not in u else None)
        if not host or host in seen:
            continue
        try:
            ip = socket.gethostbyname(host)
            seen[host] = ip
            _RESOLVED_IPS[host] = ip
            log.info("Pre-resolved %s → %s", host, ip)
        except Exception as e:
            log.warning("Could not pre-resolve %s: %s", host, e)
    return seen


def _update_etc_hosts(host_ip: dict) -> None:
    """Write missing host→IP entries to /etc/hosts for the system resolver."""
    hosts_path = Path("/etc/hosts")
    try:
        content = hosts_path.read_text()
        new_lines = []
        for host, ip in host_ip.items():
            if f" {host}" not in content and f"\t{host}" not in content:
                new_lines.append(f"{ip}  {host}  # anti-captcha\n")
                log.info("Added to /etc/hosts: %s  %s", ip, host)
        if new_lines:
            with hosts_path.open("a") as f:
                f.writelines(new_lines)
    except Exception as e:
        log.warning("Could not update /etc/hosts: %s", e)


def _prepare_dns(urls: list) -> None:
    """Pre-resolve hostnames and update /etc/hosts for the system resolver."""
    host_ip = _resolve_hostnames(urls)
    _update_etc_hosts(host_ip)


# ---------------------------------------------------------------------------
# Local CONNECT proxy (bypasses patchright Chromium's broken DNS resolver)
# ---------------------------------------------------------------------------

class _ProxyServer(_socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ConnectProxyHandler(_socketserver.BaseRequestHandler):
    """HTTP CONNECT tunnel: Chrome connects here, we resolve DNS via Python."""

    def handle(self):
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                buf += chunk
            first_line = buf.split(b"\r\n")[0].decode("ascii", errors="ignore")
            parts = first_line.split()
            if len(parts) < 2 or parts[0] != "CONNECT":
                return
            host, _, port_str = parts[1].rpartition(":")
            port = int(port_str) if port_str.isdigit() else 443
            # Use pre-resolved IP when available — avoids DNS in background threads.
            dest = _RESOLVED_IPS.get(host, host)
            try:
                remote = socket.create_connection((dest, port), timeout=30)
            except Exception as e:
                log.error("Proxy: connect %s:%d (→%s) failed: %s", host, port, dest, e)
                try:
                    self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                except OSError:
                    pass
                return
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.request.setblocking(False)
            remote.setblocking(False)
            while True:
                r, _, _ = _select.select([self.request, remote], [], [], 120)
                if not r:
                    break
                for s in r:
                    other = remote if s is self.request else self.request
                    try:
                        data = s.recv(65536)
                    except OSError:
                        return
                    if not data:
                        return
                    try:
                        other.sendall(data)
                    except OSError:
                        return
        except Exception as e:
            log.error("Proxy handler exception: %s", e)


def _start_local_proxy(port: int = 8877) -> int:
    """Start a background CONNECT proxy on localhost; returns the bound port."""
    server = _ProxyServer(("127.0.0.1", port), _ConnectProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Local CONNECT proxy started on 127.0.0.1:%d", port)
    return port


def run():
    validate_env()
    model = build_gemini_model()
    Path(USER_DATA_DIR).mkdir(parents=True, exist_ok=True)

    _prepare_dns([WEBSITE_URL, LOGIN_URL, "challenges.cloudflare.com"])

    # Use a local CONNECT proxy so Chromium's broken DNS is bypassed entirely;
    # the proxy resolves hostnames via Python's system resolver (same as curl).
    if PROXY_SERVER:
        proxy = {"server": PROXY_SERVER}
    else:
        proxy = {"server": f"http://127.0.0.1:{_start_local_proxy()}"}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=HEADLESS,
            proxy=proxy,
            user_agent=_USER_AGENT,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = context.new_page()
        page.add_init_script(_STEALTH_JS)
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
