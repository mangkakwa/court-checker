import requests
import logging
import pytz
import os
from datetime import datetime, timedelta

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PHPSESSID        = os.environ.get("PHPSESSID", "")

DAYS_AHEAD = 7
BANGKOK    = pytz.timezone("Asia/Bangkok")

WATCH_COURTS = ["North-1","North-2","North-3","Center-1","Center-2","South-1","South-2","South-3","G North-1","G North-2","G North-3","G Center-1","G Center-2","G Center-3","G South-1","G South-2","G South-3"]
WATCH_TIMES  = ["06:00","07:00","08:00","09:00","10:00","11:00","12:00","13:00","14:00","15:00","16:00","17:00","18:00","19:00","20:00","21:00","22:00"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log      = logging.getLogger()
BASE_URL = "https://crystalsports-booking.kegroup.co.th"
API_URL  = BASE_URL + "/api_helper.php?action=getAvailableStadiums"

def now_bkk(): return datetime.now(BANGKOK)
def today_bkk(): return now_bkk().strftime("%Y-%m-%d")
def get_date_range():
    base = now_bkk().date()
    return [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(DAYS_AHEAD)]
def friendly_date(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    today = today_bkk()
    tomorrow = (now_bkk() + timedelta(days=1)).strftime("%Y-%m-%d")
    tag = " (Today)" if d == today else " (Tomorrow)" if d == tomorrow else ""
    return dt.strftime("%a %d %b") + tag
def send_telegram(msg):
    try:
        r = requests.post("https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
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
            send_telegram("Session Expired! Update PHPSESSID in GitHub Secrets")
            return []
        for slot in resp.json():
            if slot.get("reservestatus") != "0": continue
            court = slot.get("stadiumName","")
            ts    = slot.get("timeName","")
            if court not in WATCH_COURTS or ts not in WATCH_TIMES: continue
            available.append({"date":target_date,"court":court,"time":ts,
                              "loc":slot.get("locName",""),"price":slot.get("stadiumtimePrice","0"),
                              "key":target_date+"|"+court+"|"+ts})
    except Exception as e:
        log.error("API error: " + str(e))
    return available
def run_check():
    now   = now_bkk()
    dates = get_date_range()
    log.info("Check | Bangkok: " + now.strftime("%H:%M") + " | " + str(DAYS_AHEAD) + " days")
    all_new = {}
    for d in dates:
        slots = check_date(d)
        if slots:
            all_new[d] = slots
            for s in slots: log.info("FOUND: " + d + " | " + s["court"] + " @ " + s["time"])
        else:
            log.info(d + " | no slots")
    if all_new:
        lines = ["Court Available!\n"]
        for d, slots in all_new.items():
            lines.append("<b>" + friendly_date(d) + "</b>")
            cs  = [s for s in slots if s["loc"] == "Crystal Sports"]
            csg = [s for s in slots if s["loc"] == "Crystal Sports G"]
            if cs:
                lines.append("Crystal Sports")
                for s in cs: lines.append("  * " + s["court"] + "  " + s["time"] + "  THB" + str(int(float(s["price"]))))
            if csg:
                lines.append("Crystal Sports G")
                for s in csg: lines.append("  * " + s["court"] + "  " + s["time"] + "  THB" + str(int(float(s["price"]))))
            lines.append("")
        lines.append('<a href="' + BASE_URL + '/booking.php">Book Now!</a>')
        send_telegram("\n".join(lines))
    else:
        log.info("No new slots found.")
if __name__ == "__main__":
    now   = now_bkk()
    dates = get_date_range()
    log.info("Court Checker | Bangkok: " + now.strftime("%Y-%m-%d %H:%M"))
    send_telegram("Court Checker running!\n\nBangkok: " + now.strftime("%H:%M") + "\nChecking: " + friendly_date(dates[0]) + " to " + friendly_date(dates[-1]))
    run_check()
