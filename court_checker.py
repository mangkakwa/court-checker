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


def run_check():
    cookie = os.getenv("CRYSTAL_COOKIE", "").strip()
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not all((cookie, token, chat_id)):
        log.error("Set CRYSTAL_COOKIE, TELEGRAM_TOKEN and TELEGRAM_CHAT_ID.")
        return 1

    now = datetime.now(BANGKOK)
    log.info("Checking Bangkok booking dates from %s", now.strftime("%Y-%m-%d %H:%M"))
    failed = False
    with requests.Session() as session:
        session.headers.update({
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html",
        })
        for day in scan_dates(now.date()):
            for location, name in LOCATIONS.items():
                try:
                    slots = check_location(session, day, location)
                except LoginRequired as exc:
                    log.error("%s", exc)

                    send_telegram(token, chat_id, str(exc))
                    return 1
                except BookingError as exc:
                    log.error("%s | %s | %s", day, name, exc)
                    failed = True
                    continue

                log.info("%s | %s | %s available times", day, name, len(slots))
                if slots:
                    lines = [f"<b>{name} · {day:%a %d %b %Y}</b>"]
                    for hour, courts, price, url in slots:
                        rate = f" · ฿{price}/hr" if price else ""
                        lines.append(
                            f'<a href="{escape(url)}">{hour}</a> · '
                            f'{escape(", ".join(courts))}{rate}'
                        )
                    if not send_telegram(token, chat_id, "\n".join(lines)):
                        failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(run_check())
