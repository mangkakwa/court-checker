"""Check the Crystal Sports booking pages and send Telegram alerts."""

import logging
import os
import re
from datetime import datetime, timedelta
from html import escape
from urllib.parse import urlencode, urlsplit

import pytz
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://crystalsports-booking.kegroup.co.th"
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
