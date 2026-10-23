"""
ContactCars Used Cars Web Scraper (Hybrid Architecture - Fixed & Optimized)
==========================================================================
Fixes:
- Added `eager` page load strategy to bypass infinite page loads / timeouts.
- Safe `driver.get()` wrapper with `window.stop()` execution on timeout.
- Explicit element waiting and optimized Chrome flags.
"""

import os
import subprocess
import sys
import re
import time
import json
import random
import logging
from typing import Dict, List, Set, Optional

import requests
import pandas as pd
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# ================= Configuration =================
BASE_URL = "https://www.contactcars.com"
SEARCH_URL_TEMPLATE = "https://www.contactcars.com/en/cars?type=car&status=3&page={page}&sortBy=&sortOrder=false"

START_PAGE = 1
MAX_PAGES = 1  # Number of pages to scrape (e.g. 5, 10, or 100)
HEADLESS = True         # Run Chrome in headless mode
DETAIL_DELAY = 0.5      # Polite delay between detail page requests (seconds)

OUTPUT_CSV = "contactcars_used_cars.csv"
OUTPUT_EXCEL = "contactcars_used_cars.xlsx"
CHECKPOINT_FILE = "scraped_ad_ids.txt"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scraper.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("ContactCarsScraper")


def init_driver(headless: bool = True) -> webdriver.Chrome:
    """Initializes and returns a configured Selenium Chrome WebDriver with optimized speed configurations."""
    options = Options()
    
    # CRITICAL FIX 1: Set page load strategy to eager (DOM ready, don't wait for images/ads)
    options.page_load_strategy = 'eager'
    
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-extensions")
    options.add_argument("--dns-prefetch-disable")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    
    # Block non-essential media to prevent page hang
    prefs = {
        "profile.default_content_setting_values.notifications": 2,
        "profile.managed_default_content_settings.images": 2,  # Block images in Selenium (Requests gets details)
    }
    options.add_experimental_option("prefs", prefs)

    driver = webdriver.Chrome(options=options)
    
    # CRITICAL FIX 2: Set reasonable timeouts
    driver.set_page_load_timeout(25)
    driver.set_script_timeout(25)

    # Inject stealth scripts
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.navigator.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            """
        }
    )
    return driver


def init_requests_session() -> requests.Session:
    """Creates a configured requests Session with desktop browser headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        "Referer": "https://www.contactcars.com/en/cars",
    })
    return session


def load_scraped_ids() -> Set[str]:
    """Loads previously scraped ad IDs to avoid duplicate work upon resume."""
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_scraped_id(ad_id: str):
    """Appends a completed ad ID to the checkpoint file."""
    with open(CHECKPOINT_FILE, "a", encoding="utf-8") as f:
        f.write(f"{ad_id}\n")


def safe_navigate(driver: webdriver.Chrome, url: str) -> None:
    """Navigates to URL safely, stopping page load if hanging occurs after DOM is loaded."""
    try:
        driver.get(url)
    except TimeoutException:
        logger.warning(f"Navigation to {url} timed out; stopping window execution and continuing.")
        driver.execute_script("window.stop();")


def get_ad_links_from_listing_page(driver: webdriver.Chrome, page_num: int) -> List[str]:
    """
    Loads search results page via Selenium and extracts all car ad URLs.
    """
    url = SEARCH_URL_TEMPLATE.format(page=page_num)
    logger.info(f"--- Loading Search Page {page_num}: {url} ---")
    
    # CRITICAL FIX 3: Safe navigation wrapper
    safe_navigate(driver, url)

    # Wait until car links or search results are rendered
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//a[contains(@href, '/en/used-cars/')]"))
        )
    except Exception:
        logger.warning(f"Timeout waiting for car links on page {page_num}. Extracting available content.")

    # Gentle scroll to ensure all dynamic elements load
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")
    time.sleep(1.0)
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(0.5)

    soup = BeautifulSoup(driver.page_source, "html.parser")
    ad_links = []
    seen = set()

    link_regex = re.compile(r"^/en/used-cars/[^/]+/[0-9a-zA-Z_-]+$")

    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0].strip()
        if "/city/" in href or "/attribute-" in href or "/year-" in href:
            continue
        if link_regex.match(href):
            if href not in seen:
                seen.add(href)
                ad_links.append(BASE_URL + href)

    logger.info(f"Found {len(ad_links)} unique car ad links on page {page_num}.")
    return ad_links


def parse_specs_summary_html(soup: BeautifulSoup) -> Dict[str, str]:
    """Parses Specs Summary box from HTML."""
    specs: Dict[str, str] = {
        "Car Status": "",
        "Body Shape": "",
        "Transmission": "",
        "Mileage": "",
        "Fuel Type": "",
        "Engine Capacity": "",
        "Color": "",
        "Cylinder Count": "",
    }

    specs_heading = soup.find(lambda t: t.name in ["h2", "h3", "h4", "div", "span"] and "specs summary" in t.get_text(strip=True).lower())
    if not specs_heading:
        return specs

    container = specs_heading.find_parent("section") or specs_heading.find_parent("div")
    if not container:
        return specs

    all_text = [t.strip() for t in container.get_text("\n").split("\n") if t.strip()]

    labels = list(specs.keys())
    for i, line in enumerate(all_text):
        for lbl in labels:
            if line.lower() == lbl.lower() and i + 1 < len(all_text):
                val = all_text[i + 1]
                if i + 2 < len(all_text) and all_text[i + 2].upper() in ["KM", "CC"]:
                    val = f"{val} {all_text[i + 2]}"
                specs[lbl] = val

    if not specs.get("Car Status"):
        specs["Car Status"] = "Used"

    return specs


def extract_description(soup: BeautifulSoup) -> str:
    """Extracts seller's full Description text."""
    desc_heading = soup.find(lambda t: t.name in ["h2", "h3", "h4", "div", "span"] and t.get_text(strip=True).lower() == "description")
    if not desc_heading:
        return ""

    container = desc_heading.find_parent("section") or desc_heading.find_parent("div")
    if not container:
        return ""

    lines = [line.strip() for line in container.get_text("\n").split("\n") if line.strip()]
    cleaned_lines = []
    for line in lines:
        lower = line.lower()
        if lower in ["description", "read more", "show number", "turn on notifications", "add to fav.", "report an issue"]:
            continue
        if line not in cleaned_lines:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def scrape_ad_details(session: requests.Session, ad_url: str) -> Optional[Dict[str, str]]:
    """Fetches car detail page via requests.Session."""
    ad_id = ad_url.rstrip("/").split("/")[-1]

    try:
        resp = session.get(ad_url, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"HTTP {resp.status_code} on {ad_url}")
            return None
    except Exception as e:
        logger.error(f"Network error on {ad_url}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    ld_vehicle = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and data.get("@type") == "Vehicle":
                ld_vehicle = data
                break
        except Exception:
            continue

    brand_name = ""
    model_name = ""
    year = ""
    trim = ""

    if ld_vehicle:
        brand_obj = ld_vehicle.get("brand")
        if isinstance(brand_obj, dict):
            brand_name = brand_obj.get("name", "")
        elif isinstance(brand_obj, str):
            brand_name = brand_obj

        model_name = str(ld_vehicle.get("model", ""))
        year = str(ld_vehicle.get("vehicleModelDate") or ld_vehicle.get("productionDate") or "")
        trim = str(ld_vehicle.get("vehicleConfiguration", ""))

    if not brand_name:
        h1 = soup.find("h1")
        if h1:
            brand_name = h1.get_text(" ", strip=True)

    price_val = ""
    price_curr = "EGP"
    if ld_vehicle and "offers" in ld_vehicle:
        offers = ld_vehicle["offers"]
        if isinstance(offers, dict):
            price_val = str(offers.get("price", ""))
            price_curr = offers.get("priceCurrency", "EGP")

    if not price_val:
        price_elem = soup.find(string=re.compile(r"EGP", re.IGNORECASE))
        if price_elem and price_elem.parent:
            text = price_elem.parent.get_text(strip=True)
            match = re.search(r"([0-9,]+)\s*EGP", text)
            if match:
                price_val = match.group(1).replace(",", "")

    date_published = ld_vehicle.get("datePublished", "") if ld_vehicle else ""
    relative_date = ""
    rel_elem = soup.find(string=re.compile(r"(?:hours?|days?|weeks?|months?)\s+ago", re.IGNORECASE))
    if rel_elem:
        relative_date = rel_elem.strip()

    specs = parse_specs_summary_html(soup)

    if ld_vehicle:
        if not specs["Body Shape"]:
            specs["Body Shape"] = str(ld_vehicle.get("bodyType", ""))
        if not specs["Transmission"]:
            specs["Transmission"] = str(ld_vehicle.get("vehicleTransmission", ""))
        if not specs["Color"]:
            specs["Color"] = str(ld_vehicle.get("color", ""))
        if not specs["Mileage"] and "mileageFromOdometer" in ld_vehicle:
            m = ld_vehicle["mileageFromOdometer"].get("value")
            if m:
                specs["Mileage"] = f"{m:,} KM"
        if not specs["Fuel Type"] and "vehicleEngine" in ld_vehicle:
            specs["Fuel Type"] = str(ld_vehicle["vehicleEngine"].get("fuelType", ""))
        if not specs["Engine Capacity"] and "vehicleEngine" in ld_vehicle:
            disp = ld_vehicle["vehicleEngine"].get("engineDisplacement")
            if disp:
                specs["Engine Capacity"] = f"{disp} CC"

    description = extract_description(soup)
    if not description and ld_vehicle:
        description = str(ld_vehicle.get("description", ""))

    return {
        "Ad ID": ad_id,
        "Ad URL": ad_url,
        "Brand": brand_name,
        "Model": model_name,
        "Year": year,
        "Trim": trim,
        "Price (EGP)": price_val,
        "Currency": price_curr,
        "Listed Date (ISO)": date_published,
        "Listed Date (Relative)": relative_date,
        "Car Status": specs.get("Car Status", ""),
        "Body Shape": specs.get("Body Shape", ""),
        "Transmission": specs.get("Transmission", ""),
        "Mileage": specs.get("Mileage", ""),
        "Fuel Type": specs.get("Fuel Type", ""),
        "Engine Capacity": specs.get("Engine Capacity", ""),
        "Color": specs.get("Color", ""),
        "Cylinder Count": specs.get("Cylinder Count", ""),
        "Description": description,
    }


def save_record_to_csv(record: Dict[str, str], filepath: str):
    """Appends a single record to CSV file."""
    df = pd.DataFrame([record])
    file_exists = os.path.exists(filepath)
    df.to_csv(filepath, mode="a", index=False, header=not file_exists, encoding="utf-8-sig")


def main():
    """Scrapes exactly ONE page (START_PAGE) per process launch."""
    logger.info("=" * 60)
    logger.info(f"Starting ContactCars Used Cars Scraper — Page {START_PAGE} | {MAX_PAGES} page(s) remaining")
    logger.info("=" * 60)

    driver = init_driver(headless=HEADLESS)
    session = init_requests_session()
    scraped_ids = load_scraped_ids()
    logger.info(f"Loaded {len(scraped_ids)} previously scraped ads from checkpoint.")

    total_scraped_this_run = 0

    try:
        # ── Only scrape START_PAGE — one page per process to avoid navigation timeouts ──
        ad_links = get_ad_links_from_listing_page(driver, START_PAGE)
        if not ad_links:
            logger.info(f"No car listings found on page {START_PAGE}. Stopping chain.")
            return

        logger.info(f"Scraping details for {len(ad_links)} ads on Page {START_PAGE}...")

        for idx, ad_url in enumerate(ad_links, 1):
            ad_id = ad_url.rstrip("/").split("/")[-1]
            if ad_id in scraped_ids:
                logger.info(f"[{idx}/{len(ad_links)}] Skipping already scraped ad: {ad_id}")
                continue

            try:
                record = scrape_ad_details(session, ad_url)
                if record:
                    save_record_to_csv(record, OUTPUT_CSV)
                    save_scraped_id(ad_id)
                    scraped_ids.add(ad_id)
                    total_scraped_this_run += 1

                    brand = record.get("Brand") or "Car"
                    model = record.get("Model") or ""
                    year  = record.get("Year")  or ""
                    price = record.get("Price (EGP)") or "N/A"
                    logger.info(
                        f"[P{START_PAGE}][{idx}/{len(ad_links)}] Saved: {brand} {model} {year} | {price} EGP"
                    )

                time.sleep(DETAIL_DELAY)

            except Exception as e:
                logger.error(f"Error scraping {ad_url}: {e}")
                time.sleep(1.0)

        if os.path.exists(OUTPUT_CSV):
            try:
                df_all = pd.read_csv(OUTPUT_CSV, encoding="utf-8-sig")
                df_all.to_excel(OUTPUT_EXCEL, index=False)
                logger.info("=" * 60)
                logger.info(f"Exported {len(df_all)} total rows to {OUTPUT_EXCEL}")
            except Exception as e:
                logger.warning(f"Could not write Excel file: {e}")

        logger.info(f"Scraping finished! Ads scraped this run: {total_scraped_this_run}")
        logger.info("=" * 60)

    finally:
        driver.quit()
        logger.info("WebDriver closed cleanly.")


def _relaunch_for_next_page():
    """
    Rewrites START_PAGE (+=1) and MAX_PAGES (-=1) in this script file,
    then spawns a fresh Python process to scrape the next page.
    Only relaunches when MAX_PAGES > 1 (i.e. there are more pages to do).
    """
    if MAX_PAGES <= 1:
        logger.info("All requested pages have been scraped. Chain complete — not relaunching.")
        return

    script_path = os.path.abspath(__file__)
    with open(script_path, "r", encoding="utf-8") as f:
        source = f.read()

    next_page     = START_PAGE + 1
    pages_left    = MAX_PAGES  - 1

    source = re.sub(
        r"^(START_PAGE\s*=\s*)\d+",
        f"\g<1>{next_page}",
        source, count=1, flags=re.MULTILINE,
    )
    source = re.sub(
        r"^(MAX_PAGES\s*=\s*)\d+",
        f"\g<1>{pages_left}",
        source, count=1, flags=re.MULTILINE,
    )

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(source)

    logger.info(
        f"START_PAGE updated {START_PAGE} → {next_page} | "
        f"MAX_PAGES updated {MAX_PAGES} → {pages_left}. Relaunching..."
    )
    subprocess.Popen([sys.executable, script_path])


if __name__ == "__main__":
    main()
    _relaunch_for_next_page()
    logger.info("This process will now exit.")