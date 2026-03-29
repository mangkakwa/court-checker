import requests
import schedule
import time
import logging
import pytz
from datetime import datetime, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Keep-alive web server (stops Replit from sleeping) ──────────────
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Court Checker is alive!")
    def log_message(self, format, *args):
        pass  # silence server logs

def run_server():
    HTTPServer(("0.0.0.0", 8080), PingHandler).serve_forever()

Thread(target=run_server, daemon=True).start()
# ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN   = "8333046974:AAEAAP4tRiXIum4gOa8rx1CRjsvQNVkEtbg"
TELEGRAM_CHAT_ID = "8358560195"
PHPSESSID        = "st5l3jih6klefclvr3big2rs71"   # refresh if expired
DAYS_AHEAD       = 7
CHECK_START_HOUR = 8
CHECK_END_HOUR   = 22
BANGKOK          = pytz.timezone("Asia/Bangkok")

WATCH_COURTS = [
    "North-1","North-2","North-3","Center-1","Center-2",
    "South-1","South-2","South-3",
    "G North-1","G North-2","G North-3",
    "G Center-1","G Center-2","G Center-3",
    "G South-1","G South-2","G South-3",
]
WATCH_TIMES = [
    "06:00","07:00","08:00","09:00","10:00","11:00",
    "12:00","13:00","14:00","15:00","16:00","17:00",
    "18:00","19:00","20:00","21:00","22:00"
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log        = logging.getLogger()
BASE_URL   = "https://crystalsports-booking.kegroup.co.th"
API_URL    = BASE_URL + "/api_helper.php?action=getAvailableStadiums"
notified   = {}
check_num  = 0
last_reset = None


def now_bkk():
    return datetime.now(BANGKOK)

def today_bkk():
    return now_bkk().strftime("%Y-%m-%d")

def get_date_range():
    base = now_bkk().date()
    return [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DAYS_AHEAD)]

def friendly_date(d):
    dt       = datetime.strptime(d, "%Y-%m-%d")
    today    = today_bkk()
    tomorrow = (now_bkk() + timedelta(days=1)).strftime("%Y-%m-%d")
    tag      = " (Today)" if d == today else " (Tomorrow)" if d == tomorrow else ""
    return dt.strftime("%a %d %b") + tag

def reset_notified_if_new_day():
    global notified, last_reset
    today = today_bkk()
    if last_reset != today:
        old        = len(notified)
        notified   = {}
        last_reset = today
        if old > 0:
            log.info("New day - reset " + str(old) + " notified slots")

def send_telegram(msg):
    try:
        r = requests.post(
            "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        log.info("Telegram sent!" if r.status_code == 200 else "Telegram error: " + str(r.status_code))
    except Exception as e:
        log.error("Telegram exception: " + str(e))

def check_date(target_date):
    available = []
    try:
        resp = requests.post(API_URL,
            headers={"Content-Type":"application/json","Cookie":"PHPSESSID="+PHPSESSID,
                     "Referer":BASE_URL+"/booking.php","User-Agent":"Mozilla/5.0"},
            json={"date":target_date,"stadium_id":1,"court_id":2}, timeout=15)
        if resp.status_code != 200 or "login" in resp.text.lower()[:100]:
            log.warning("Session expired!")
            send_telegram("Session Expired! Update PHPSESSID in main.py on Replit")
            return []
        for slot in resp.json():
            if slot.get("reservestatus") != "0": continue
            court = slot.get("stadiumName","")
            ts    = slot.get("timeName","")
            if court not in WATCH_COURTS or ts not in WATCH_TIMES: continue
            available.append({
                "date":  target_date,
                "court": court,
                "time":  ts,
                "loc":   slot.get("locName",""),
                "price": slot.get("stadiumtimePrice","0"),
                "key":   target_date+"|"+court+"|"+ts
            })
    except Exception as e:
        log.error("API error: " + str(e))
    return available

def run_check():
    global check_num
    reset_notified_if_new_day()
    now = now_bkk()
    if not (CHECK_START_HOUR <= now.hour <= CHECK_END_HOUR):
        log.info("Outside Bangkok hours | " + now.strftime("%H:%M"))
        return
    check_num += 1
    dates = get_date_range()
    log.info("Check #" + str(check_num) + " | Bangkok: " + now.strftime("%H:%M") + " | " + str(len(dates)) + " days")
    all_new = {}
    for d in dates:
        slots = check_date(d)
        new   = [s for s in slots if not notified.get(s["key"])]
        if new:
            all_new[d] = new
            for s in new:
                notified[s["key"]] = today_bkk()
                log.info("FOUND: " + d + " | " + s["court"] + " @ " + s["time"])
        else:
            log.info(d + " | " + (str(len(slots)) + " already notified" if slots else "no slots"))
    if all_new:
        lines = ["Court Available!\n"]
        for d, slots in all_new.items():
            lines.append("<b>" + friendly_date(d) + "</b>")
            cs  = [s for s in slots if s["loc"] == "Crystal Sports"]
            csg = [s for s in slots if s["loc"] == "Crystal Sports G"]
            if cs:
                lines.append("Crystal Sports")
                for s in cs:
                    lines.append("  * " + s["court"] + "  " + s["time"] + "  THB" + str(int(float(s["price"]))))
            if csg:
                lines.append("Crystal Sports G")
                for s in csg:
                    lines.append("  * " + s["court"] + "  " + s["time"] + "  THB" + str(int(float(s["price"]))))
            lines.append("")
        lines.append('<a href="' + BASE_URL + '/booking.php">Book Now!</a>')
        send_telegram("\n".join(lines))
    else:
        log.info("No new slots.")

def main():
    global last_reset
    dates      = get_date_range()
    now        = now_bkk()
    last_reset = today_bkk()
    log.info("Court Checker started | Bangkok: " + now.strftime("%Y-%m-%d %H:%M"))
    log.info("Keep-alive server on port 8080")
    send_telegram(
        "Court Checker started!\n\n"
        "Bangkok: " + now.strftime("%H:%M") + "\n"
        "Watching: " + friendly_date(dates[0]) + " to " + friendly_date(dates[-1]) + "\n"
        "Checks every hour " + str(CHECK_START_HOUR) + ":00-" + str(CHECK_END_HOUR) + ":00 Bangkok\n\n"
        "Will notify when court opens!"
    )
    run_check()
    schedule.every().hour.at(":00").do(run_check)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
