"""Check the Crystal Sports booking pages and send Telegram alerts."""

import json
import logging
import os
import re
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytz
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://crystalsports-booking.kegroup.co.th"
BKKSPIN_URL = "https://bkkspin.com/reservation"
BANGKOK = pytz.timezone("Asia/Bangkok")
DAYS_AHEAD = 14
WEEKENDS_ONLY = True
WATCH_HOURS = range(6, 24)
LOCATIONS = {"LOC001": "Crystal Sports", "LOC002": "Crystal Sports G"}
TIME_PATTERN = re.compile(r"\b(\d{2}):00\b")
PRICE_PATTERN = re.compile(r"฿\s*([\d,.]+)")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)


class BookingError(Exception):
    pass


class LoginRequired(BookingError):
    pass


def booking_url(day, location):
    query = urlencode({"date": day.isoformat(), "loc": location, "time": 0, "sel": ""})
    return f"{BASE_URL}/booking?{query}#step-time"


def scan_dates(today):
    for offset in range(DAYS_AHEAD + 1):
        day = today + timedelta(days=offset)
        if not WEEKENDS_ONLY or day.weekday() >= 5:
            yield day


def read_booking_page(session, url):
    try:
        response = session.get(url, timeout=20, allow_redirects=False)
    except requests.RequestException as exc:
        raise BookingError(f"Booking request failed ({type(exc).__name__})") from exc

    redirect = urlsplit(response.headers.get("Location", ""))
    if response.status_code == 401 or (
        response.is_redirect and redirect.path.lower().rstrip("/") == "/account/login"
    ):
        raise LoginRequired("Booking site requires sign-in. Refresh CRYSTAL_COOKIE.")
    if response.status_code != 200:
        raise BookingError(f"Booking site returned HTTP {response.status_code}")

    page = BeautifulSoup(response.content, "html.parser")
    if page.select_one('form[action^="/account/login"]'):
        raise LoginRequired("Booking site requires sign-in. Refresh CRYSTAL_COOKIE.")
    return page


def booking_link(element):
    href = element["href"]
    if not href.startswith("/booking?"):
        raise BookingError("Unexpected booking link")
    return BASE_URL + href


def check_location(session, day, location):
    page = read_booking_page(session, booking_url(day, location))
    dates = page.select_one("#step-date")
    times = page.select_one("#step-time")
    if dates is None:
        raise BookingError("Date picker missing from booking page")

    # A date that the site has yet to open is shown without a booking link.
    expected_date = f"date={day.isoformat()}&"
    if not any(expected_date in link["href"] for link in dates.select("a[href]")):
        log.info("%s | %s | date not selectable", day, LOCATIONS[location])
        return []
    if times is None or not times.select_one(".slot-time"):
        raise BookingError("Time picker missing or empty on a selectable date")

    results = []
    for slot in times.select('a.slot[href]:not(.is-disabled):not([aria-disabled="true"])'):
        label = slot.select_one(".slot-time")
        match = TIME_PATTERN.fullmatch(label.get_text(strip=True)) if label else None
        if not match or int(match.group(1)) not in WATCH_HOURS:
            continue

        time_url = booking_link(slot)
        detail = read_booking_page(session, time_url)
        courts = detail.select_one("#step-court")
        if courts is None or not courts.select_one(".court-tile"):
            raise BookingError("Court picker missing on the selected time page")

        names = [
            tile["aria-label"]
            for tile in courts.select(
                'a.court-tile[href][aria-label]:not(.is-disabled):not([aria-disabled="true"])'
            )
        ]
        if names:
            heading = courts.select_one(".dash-label")
            price = PRICE_PATTERN.search(heading.get_text(" ", strip=True)) if heading else None
            rate = price.group(1) if price else ""
            results.append((label.get_text(strip=True), names, rate, time_url))
    return results


def parse_bkkspin_grid(html):
    page = BeautifulSoup(html, "html.parser")
    markers = page.select('button[id^="slot-"]')
    if not markers:
        raise BookingError("Bangkok Spin booking grid is missing")
    grid = markers[0].parent
    courts = [label["title"].removesuffix(" (Auction)") for label in grid.select('[title^="Court "]')]
    if not courts:
        raise BookingError("Bangkok Spin court headers are missing")

    slots = []
    for marker in markers:
        hour = marker["id"].removeprefix("slot-")
        cells = []
        for cell in marker.next_siblings:
            if getattr(cell, "name", None) is None:
                continue
            if cell.get("id", "").startswith("slot-"):
                break
            cells.append(cell)
        if len(cells) != len(courts):
            raise BookingError("Bangkok Spin court columns do not match the headers")
        by_price = {}
        for court, cell in zip(courts, cells):
            bookable = any(
                re.fullmatch(r"(?:Book|จอง(?:เลย)?)\s*→?", button.get_text(" ", strip=True), re.I)
                for button in cell.select('button:not([disabled]):not([aria-disabled="true"])')
            )
            if not bookable:
                continue
            price = re.search(r"([\d,.]+)\s*(?:THB|บาท)", cell.get_text(" ", strip=True), re.I)
            by_price.setdefault(price.group(1) if price else "", []).append(court)
        slots.extend((hour, names, rate, BKKSPIN_URL) for rate, names in by_price.items())
    return slots


def scan_bkkspin(today, dates, storage_state):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(storage_state=json.loads(storage_state), timezone_id="Asia/Bangkok", locale="en-GB")
        page = context.new_page()
        page.set_default_timeout(15000)
        log.info("Bangkok Spin | opening reservation page")
        response = page.goto(BKKSPIN_URL, wait_until="domcontentloaded")
        log.info("Bangkok Spin | page response HTTP %s", response.status if response else "unknown")
        if response and response.status >= 400:
            raise BookingError(f"Bangkok Spin reservation returned HTTP {response.status}")
        log.info("Bangkok Spin | waiting for sign-in redirect or booking grid")
        page.wait_for_function(
            "location.pathname === '/login' || !!document.querySelector('#step-grid-heading')"
        )
        if urlsplit(page.url).path == "/login":
            raise LoginRequired("Bangkok Spin requires sign-in. Refresh BKKSPIN_STORAGE_STATE.")
        log.info("Bangkok Spin | booking grid loaded")
        reject = page.get_by_role("button", name=re.compile(r"Reject all|ปฏิเสธทั้งหมด", re.I))
        if reject.is_visible():
            reject.click()
        english = page.get_by_role("button", name="English", exact=True)
        if english.is_visible():
            log.info("Bangkok Spin | switching booking grid to English")
            english.click()

        calendar = page.locator('section[aria-labelledby="step-date-heading"]')
        grid = page.locator('section[aria-labelledby="step-grid-heading"]')
        week = today - timedelta(days=today.weekday())
        for day in dates:
            log.info("Bangkok Spin | selecting %s", day)
            while day >= week + timedelta(days=7):
                next_week = calendar.get_by_role("button", name="Next week", exact=True)
                if not next_week.is_enabled():
                    return
                next_week.dispatch_event("click")
                week += timedelta(days=7)
            day_number = page.locator("span:nth-child(2)").filter(
                has_text=re.compile(rf"^0?{day.day}$")
            )
            button = calendar.locator(".grid-cols-7 > button").filter(has=day_number)
            if button.count() != 1:
                raise BookingError(f"Bangkok Spin could not identify calendar day {day}")
            log.info("Bangkok Spin | found calendar button for %s", day)
            if not button.is_enabled():
                continue
            if not button.evaluate("element => element.classList.contains('text-white')"):
                button.dispatch_event("click")
            date_label = re.compile(rf"^{day:%a} 0?{day.day} {day:%b %Y}$")
            log.info("Bangkok Spin | waiting for the %s grid", day)
            try:
                grid.get_by_text(date_label).wait_for()
            except Exception:
                heading = grid.locator("#step-grid-heading + span").first
                actual = heading.text_content(timeout=2000) if heading.count() else "missing"
                log.error("Bangkok Spin | selected grid date is %s, expected %s", actual, day)
                raise
            grid.locator('button[id^="slot-"]').first.wait_for()
            yield day, parse_bkkspin_grid(grid.inner_html())


def save_bkkspin_login(destination):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(timezone_id="Asia/Bangkok")
        page = context.new_page()
        page.goto(BKKSPIN_URL)
        input("Sign in and open the Bangkok Spin booking grid, then press Enter here: ")
        page.locator('button[id^="slot-"]').first.wait_for(timeout=15000)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(context.storage_state(indexed_db=True), output, ensure_ascii=False)


def send_telegram(token, chat_id, message):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
            timeout=15,
        )
        if response.status_code == 200 and response.json().get("ok"):
            return True
        log.error("Telegram rejected the alert (HTTP %s)", response.status_code)
    except (requests.RequestException, ValueError) as exc:
        log.error("Telegram request failed (%s)", type(exc).__name__)
    return False


def availability_messages(results):
    title = "🎾 <b>สรุปสนามเทนนิสว่าง</b>"
    weekdays = ("จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา.")
    months = ("ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
              "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.")
    lines = [title]
    length = len(title)
    last_day = last_location = None
    for day, location, slots in results:
        date_heading = f"<b>{weekdays[day.weekday()]} {day.day} {months[day.month - 1]}</b>"
        location_heading = f"<b>{escape(location)}</b>"
        for hour, courts, price, url in slots:
            rate = f" · ฿{price}/ชม." if price else ""
            detail = f" · {', '.join(courts)}{rate}"
            row = f'<a href="{escape(url)}">{hour}</a>{escape(detail)}'
            headings = []
            if day != last_day:
                headings = ["", date_heading, location_heading]
            elif location != last_location:
                headings = [location_heading]
            added = sum(len(line) + 1 for line in headings) + len(hour + detail) + 1
            if length + added > 4096:
                yield "\n".join(lines)
                lines = [title]
                length = len(title)
                headings = ["", date_heading, location_heading]
                added = sum(len(line) + 1 for line in headings) + len(hour + detail) + 1
            lines.extend([*headings, row])
            length += added
            last_day, last_location = day, location
    if len(lines) > 1:
        yield "\n".join(lines)


def run_check():
    cookie = os.getenv("CRYSTAL_COOKIE", "").strip()
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    spin_state = os.getenv("BKKSPIN_STORAGE_STATE", "").strip()
    if not all((token, chat_id)) or not (cookie or spin_state):
        log.error("Set Telegram credentials and at least one booking site's session.")
        return 1

    now = datetime.now(BANGKOK)
    log.info("Checking Bangkok booking dates from %s", now.strftime("%Y-%m-%d %H:%M"))
    failed = False
    results = []
    login_errors = []
    dates = list(scan_dates(now.date()))
    with requests.Session() as session:
        session.headers.update({
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html",
        })
        for day in dates if cookie else ():
            for location, name in LOCATIONS.items():
                try:
                    slots = check_location(session, day, location)
                except LoginRequired as exc:
                    log.error("%s", exc)
                    login_errors.append(str(exc))
                    break
                except BookingError as exc:
                    log.error("%s | %s | %s", day, name, exc)
                    failed = True
                    continue
                log.info("%s | %s | %s available times", day, name, len(slots))
                if slots:
                    results.append((day, name, slots))
            if login_errors:
                break

    if spin_state:
        try:
            for day, slots in scan_bkkspin(now.date(), dates, spin_state):
                log.info("%s | Bangkok Spin | %s available times", day, len({slot[0] for slot in slots}))
                if slots:
                    results.append((day, "Bangkok Spin", slots))
        except LoginRequired as exc:
            log.error("%s", exc)
            login_errors.append(str(exc))
        except BookingError as exc:
            log.error("%s", exc)
            failed = True
        except Exception as exc:
            # Keep Crystal Sports results deliverable if the Bangkok Spin browser fails.
            log.error("Bangkok Spin check failed (%s)", type(exc).__name__)
            failed = True

    results.sort(key=lambda result: result[0])
    for message in availability_messages(results):
        if not send_telegram(token, chat_id, message):
            failed = True
    if login_errors:
        send_telegram(token, chat_id, "\n".join(login_errors))
        return 1
    return int(failed)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--save-bkkspin-login", type=Path, metavar="PATH")
    arguments = parser.parse_args()
    if arguments.save_bkkspin_login:
        save_bkkspin_login(arguments.save_bkkspin_login)
    else:
        raise SystemExit(run_check())
