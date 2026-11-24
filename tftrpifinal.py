import colorsys
import contextlib
import datetime
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import pygame
import requests
import speech_recognition as sr
from gpiozero import OutputDevice

# ==============================================================================
# 1. CONFIGURATION (NO API KEY OR PAID CREDITS REQUIRED)
# ==============================================================================
MIC_DEVICE_INDEX = None              # Set e.g. 1 or 2 if needed, or None for default mic
IDLE_SLEEP_SECONDS = 60.0            # Auto-sleep after 1 minute of inactivity
POST_SPEECH_SETTLE_SECONDS = 3.0     # Wait up to 3 seconds after speaking before searching
SEARCH_START_DELAY = 0.45            # Short beat in THINKING state before lookup
SCREENSAVER_SWITCH_SECONDS = 8.5    # Switch sleep screensaver every N seconds
EYE_COLOR_CYCLE_SECONDS = 12.0       # Full smooth color cycle duration (Red -> Green -> Blue -> All)
DUCAT_RISE_URL = "https://ducatrise.com/"
AUTO_SCROLL_SECONDS = 12.0           # Duration for optional browser preview auto-scroll

# Map spoken slots 1..5 ("first".."fifth") to your 5 BCM GPIO pins
GPIO_SLOT_MAP = {
    1: 17,
    2: 27,
    3: 22,
    4: 23,
    5: 24,
}
ALLOWED_PINS = list(GPIO_SLOT_MAP.values())
# ==============================================================================

os.environ.setdefault("DISPLAY", ":0")

gpio_devices = {
    pin: OutputDevice(pin, active_high=True, initial_value=False)
    for pin in ALLOWED_PINS
}

pygame.init()
info = pygame.display.Info()
width, height = info.current_w, info.current_h
if width <= 0 or height <= 0:
    width, height = 1024, 768

screen = pygame.display.set_mode((width, height), pygame.FULLSCREEN)
pygame.mouse.set_visible(False)
clock = pygame.time.Clock()


def make_font(size: int, bold: bool = True, mono: bool = False):
    family = "monospace" if mono else "dejavusans"
    return pygame.font.SysFont(family, max(12, int(size)), bold=bold)


font_sleep_brand = make_font(max(32, int(height * 0.062)), bold=True)
font_mode_title = make_font(max(24, int(height * 0.044)), bold=True)
font_card_title = make_font(max(22, int(height * 0.038)), bold=True)
font_body = make_font(max(18, int(height * 0.030)), bold=True)
font_stt = make_font(max(17, int(height * 0.028)), bold=True)
font_reply = make_font(max(20, int(height * 0.034)), bold=True)
font_hw_item = make_font(max(19, int(height * 0.031)), bold=True)
font_pill = make_font(max(13, int(height * 0.020)), bold=True)
font_matrix = make_font(16, bold=True, mono=True)

event_queue = queue.Queue()
state_lock = threading.Lock()
running = True

# Session & display state
expression = "NORMAL"         # NORMAL, LISTENING, HAPPY, THINKING, ALERT, SLEEP
heard_text = ""
reply_text = ""
sleep_mode = False
hardware_mode = False
courses_mode = False
course_index = 0
capturing_session = False
captured_chunks = []
last_speech_time = 0.0
last_activity_time = time.time()
voice_energy = 0.0

# GPIO visual feedback animation state
last_action_time = 0.0
last_action_high = False
last_action_slot = 0

# Knowledge cards sourced from Ducat RISE (https://ducatrise.com/) & Ducat India
COURSE_TOPICS = [
    {
        "key": "robotics",
        "title": "Robotics, LEGO & Arduino (School)",
        "summary": (
            "Ducat RISE introduces school students to robotics, sensors, motors, "
            "automation, and LEGO / Arduino builds through hands-on lab sessions."
        ),
    },
    {
        "key": "python",
        "title": "Python Champion (School)",
        "summary": (
            "Hands-on Python programming for students covering core logic, data "
            "organization, analysis, and visual data insights."
        ),
    },
    {
        "key": "scratch",
        "title": "Scratch Programming (School)",
        "summary": (
            "Visual block-based coding on Scratch where school learners build "
            "interactive stories, animations, and 2D games while learning logic."
        ),
    },
    {
        "key": "3d",
        "title": "3D Designing & Printing (School)",
        "summary": (
            "Teaches students 3D shapes, coordinate/spatial thinking, 3D modeling, "
            "and turning designs into physical 3D-printed models."
        ),
    },
    {
        "key": "finance",
        "title": "Financial Literacy (School)",
        "summary": (
            "Beginner-friendly money skills for students covering assets, liabilities, "
            "interest, inflation, taxes, and digital/cyber awareness."
        ),
    },
    {
        "key": "ducat",
        "title": "About Ducat India & Ducat RISE",
        "summary": (
            "Ducat RISE is Ducat's school-focused future-skills venture (ducatrise.com). "
            "Ducat India (est. 2000) offers 180+ practical IT, software, cloud, "
            "cybersecurity, data science, and AI courses across Delhi NCR."
        ),
    },
]

ORDINAL_AND_NUM_TO_SLOT = {
    "first": 1, "1st": 1, "one": 1, "won": 1, "fast": 1,
    "second": 2, "2nd": 2, "two": 2, "to": 2, "too": 2, "tu": 2,
    "third": 3, "3rd": 3, "three": 3, "tree": 3, "free": 3, "tred": 3,
    "fourth": 4, "4th": 4, "forth": 4, "four": 4, "for": 4, "fore": 4,
    "fifth": 5, "5th": 5, "fith": 5, "five": 5, "feif": 5,
}

# Sleep animations (DUCAT_RISE shown exclusively during SLEEP)
SCREENSAVER_MODES = ["DUCAT_RISE", "AURORA", "ORBIT_PULSE", "DINO", "MATRIX", "WARP"]
active_saver = "DUCAT_RISE"
last_saver_switch = time.time()

num_cols = max(20, width // 22)
matrix_drops = [random.randint(-40, height // 16) for _ in range(num_cols)]
matrix_speeds = [random.uniform(4.0, 11.0) for _ in range(num_cols)]

warp_stars = [
    [random.uniform(-1.0, 1.0), random.uniform(-1.0, 1.0), random.uniform(0.15, 1.0)]
    for _ in range(110)
]

aurora_motes = [
    [random.uniform(0.05, 0.95), random.uniform(0.12, 0.88), random.uniform(2.0, 5.0), random.uniform(0.0, 6.28)]
    for _ in range(28)
]


@contextlib.contextmanager
def suppress_alsa_stderr():
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    old_stderr_fd = os.dup(2)
    os.dup2(devnull_fd, 2)
    os.close(devnull_fd)
    try:
        yield
    finally:
        os.dup2(old_stderr_fd, 2)
        os.close(old_stderr_fd)


def lerp(a: float, b: float, factor: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, factor))


def lerp_rgb(c1, c2, factor: float):
    f = max(0.0, min(1.0, factor))
    smooth_f = f * f * (3.0 - 2.0 * f)
    return (
        int(round(c1[0] + (c2[0] - c1[0]) * smooth_f)),
        int(round(c1[1] + (c2[1] - c1[1]) * smooth_f)),
        int(round(c1[2] + (c2[2] - c1[2]) * smooth_f)),
    )


def get_smooth_eye_color(t: float):
    """Smoothly transitions eye color one by one: Red -> Green -> Blue -> Gold -> Cyan -> Magenta -> Red."""
    palette = [
        (235, 38, 52),    # Red
        (36, 225, 112),   # Green
        (45, 125, 255),   # Blue
        (250, 195, 45),   # Gold / Yellow
        (32, 225, 235),   # Cyan
        (215, 65, 235),   # Magenta / Violet
    ]
    cycle_pos = ((t / max(1.0, EYE_COLOR_CYCLE_SECONDS)) * len(palette)) % len(palette)
    idx_a = int(cycle_pos) % len(palette)
    idx_b = (idx_a + 1) % len(palette)
    frac = cycle_pos - int(cycle_pos)
    return lerp_rgb(palette[idx_a], palette[idx_b], frac)


def shade_rgb(rgb, factor: float):
    return (
        max(0, min(255, int(round(rgb[0] * factor)))),
        max(0, min(255, int(round(rgb[1] * factor)))),
        max(0, min(255, int(round(rgb[2] * factor)))),
    )


def get_fitting_font(text: str, max_w: int, start_size: int, min_size: int = 13, bold: bool = True):
    sz = start_size
    while sz > min_size:
        f = make_font(sz, bold=bold)
        if f.size(text)[0] <= max_w:
            return f
        sz -= 2
    return make_font(min_size, bold=bold)


def wrap_text(text: str, font: pygame.font.Font, max_width: int):
    if not text:
        return []
    words = text.split()
    if not words:
        return []
    lines = []
    cur = words[0]
    for w in words[1:]:
        test = f"{cur} {w}"
        if font.size(test)[0] <= max_width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def draw_centered_wrapped(
    surface,
    text: str,
    font: pygame.font.Font,
    color,
    center_x: int,
    top_y: int,
    max_width: int,
    max_lines: int = 4,
    line_gap: int = 6,
) -> int:
    """Draws wrapped text centered at center_x and returns the bottom y coordinate."""
    lines = wrap_text(text, font, max_width)
    step = font.get_linesize() + line_gap
    cur_y = top_y
    for line in lines[:max_lines]:
        surf = font.render(line, True, color)
        rect = surf.get_rect(center=(center_x, cur_y + surf.get_height() // 2))
        surface.blit(surf, rect)
        cur_y += step
    return cur_y


def touch_activity():
    global last_activity_time
    with state_lock:
        last_activity_time = time.time()


def record_gpio_animation(slot_num: int, is_high: bool):
    global last_action_time, last_action_high, last_action_slot
    with state_lock:
        last_action_time = time.time()
        last_action_high = is_high
        last_action_slot = slot_num


def set_all_gpios(value: bool):
    for dev in gpio_devices.values():
        if value:
            dev.on()
        else:
            dev.off()
    record_gpio_animation(0, value)


def set_pin_state(pin: int, high: bool, slot_num: int = 0):
    if pin not in gpio_devices:
        return False, f"GPIO {pin} not mapped."
    if high:
        gpio_devices[pin].on()
        record_gpio_animation(slot_num, True)
        return True, f"GPIO {pin} ON (HIGH)."
    else:
        gpio_devices[pin].off()
        record_gpio_animation(slot_num, False)
        return True, f"GPIO {pin} OFF (LOW)."


def get_gpio_states_snapshot():
    snap = []
    for slot, pin in sorted(GPIO_SLOT_MAP.items()):
        dev = gpio_devices.get(pin)
        is_on = bool(dev.is_active) if dev is not None else False
        snap.append((slot, pin, is_on))
    return snap


def normalize_slot_and_action_tokens(text: str) -> str:
    tokens = text.lower().split()
    out = []
    for tok in tokens:
        clean_tok = re.sub(r"[^a-z0-9]", "", tok)
        if not clean_tok:
            continue
        if clean_tok in ORDINAL_AND_NUM_TO_SLOT:
            out.append(str(ORDINAL_AND_NUM_TO_SLOT[clean_tok]))
        elif clean_tok in ("of", "off", "offf"):
            out.append("off")
        elif clean_tok in ("on", "onnn", "awn"):
            out.append("on")
        elif clean_tok in ("all", "al", "aul"):
            out.append("all")
        else:
            out.append(clean_tok)
    return " ".join(out)


def is_exit_phrase(low: str) -> bool:
    return bool(re.search(r"\b(exit|leave|back)\b", low))


def is_enter_hardware_phrase(low: str) -> bool:
    if is_exit_phrase(low):
        return False
    compact = re.sub(r"[^a-z0-9\s]", " ", low)
    compact = re.sub(r"\s+", " ", compact).strip()
    if not compact:
        return False
    if re.search(r"\b(hardware|hard\s*wear|hardwire)\b", compact):
        return True
    if re.search(r"\b(enter|open|start|launch)\s+(hard|hw)\b", compact):
        return True
    return False


def is_enter_courses_phrase(low: str) -> bool:
    if is_exit_phrase(low):
        return False
    compact = re.sub(r"[^a-z0-9\s]", " ", low)
    compact = re.sub(r"\s+", " ", compact).strip()
    if not compact:
        return False
    if re.search(r"\b(courses?|cources?|syllabus|programs?)\b", compact) and any(
        w in compact for w in ["enter", "open", "tell", "about", "show", "list", "what", "ducat", "rise", "school"]
    ):
        return True
    if compact in (
        "courses",
        "cources",
        "enter courses",
        "enter cources",
        "tell me about courses",
        "tell me about cources",
    ):
        return True
    return False


def launch_website_autoscroll():
    """Opens ducatrise.com in browser and sends gentle scroll keys for AUTO_SCROLL_SECONDS."""
    def _worker():
        proc = None
        try:
            env = os.environ.copy()
            env.setdefault("DISPLAY", ":0")
            proc = subprocess.Popen(
                ["chromium-browser", "--kiosk", DUCAT_RISE_URL],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            time.sleep(2.5)
            end_ts = time.time() + AUTO_SCROLL_SECONDS
            direction_down = True
            while time.time() < end_ts and running:
                key = "Page_Down" if direction_down else "Page_Up"
                subprocess.run(
                    ["xdotool", "key", key],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
                time.sleep(1.5)
                if time.time() > end_ts - 4.0:
                    direction_down = False
        except Exception:
            try:
                subprocess.Popen(["xdg-open", DUCAT_RISE_URL])
            except Exception:
                pass
        finally:
            if proc and proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.terminate()

    threading.Thread(target=_worker, daemon=True).start()


def answer_from_ducat_knowledge(q_low: str):
    """Answers questions about Ducat RISE courses and Ducat India using gathered facts."""
    if any(k in q_low for k in ["lego", "robot", "arduino", "sensor", "motor"]):
        return COURSE_TOPICS[0]["summary"], 0
    if "python" in q_low:
        return COURSE_TOPICS[1]["summary"], 1
    if "scratch" in q_low or "block" in q_low or "game" in q_low:
        return COURSE_TOPICS[2]["summary"], 2
    if any(k in q_low for k in ["3d", "design", "print", "modeling"]):
        return COURSE_TOPICS[3]["summary"], 3
    if any(k in q_low for k in ["financ", "money", "asset", "interest", "tax", "budget"]):
        return COURSE_TOPICS[4]["summary"], 4
    if any(k in q_low for k in ["ducat india", "company", "institute", "noida", "placement", "full stack", "about ducat"]):
        return COURSE_TOPICS[5]["summary"], 5

    if any(k in q_low for k in ["course", "cource", "ducat rise", "rise", "school", "program"]):
        return (
            "Ducat RISE school courses: Robotics/LEGO/Arduino, Python Champion, "
            "Scratch Programming, 3D Designing & Printing, and Financial Literacy."
        ), 0

    return None, None


def try_gpio_command(raw_low: str):
    low = normalize_slot_and_action_tokens(raw_low)
    collapsed = re.sub(r"\s+", " ", low).strip()

    if re.search(r"\b(?:all(?:\s+all)?|every|turn\s+(?:all|every))\s+(?:off|low|disable)\b", collapsed) or re.search(
        r"\b(?:off|low|disable)\s+(?:all(?:\s+all)?|every)\b", collapsed
    ):
        set_all_gpios(False)
        return True, True, "All 5 GPIOs OFF."

    if re.search(r"\b(?:all(?:\s+all)?|every|turn\s+(?:all|every))\s+(?:on|high|enable)\b", collapsed) or re.search(
        r"\b(?:on|high|enable)\s+(?:all(?:\s+all)?|every)\b", collapsed
    ):
        set_all_gpios(True)
        return True, True, "All 5 GPIOs ON."

    slot_match = re.search(r"\b([1-5])\s*(on|off|high|low|enable|disable)\b", collapsed)
    if not slot_match:
        slot_match = re.search(r"\b(on|off|high|low|enable|disable)\s*([1-5])\b", collapsed)
        if slot_match:
            action_word, slot_str = slot_match.group(1), slot_match.group(2)
        else:
            slot_str = None
            action_word = None
    else:
        slot_str, action_word = slot_match.group(1), slot_match.group(2)

    if slot_str and action_word:
        slot_num = int(slot_str)
        target_pin = GPIO_SLOT_MAP.get(slot_num)
        want_high = action_word in ("on", "high", "enable")
        ok, msg = set_pin_state(target_pin, want_high, slot_num=slot_num)
        return True, ok, f"[{slot_num}] {msg}"

    pin_match = re.search(r"\b(?:gpio|pin)\s*(\d+)\s*(on|off|high|low|enable|disable)?\b", collapsed)
    if pin_match:
        pin_num = int(pin_match.group(1))
        act = pin_match.group(2) or ""
        rev_slot = next((k for k, v in GPIO_SLOT_MAP.items() if v == pin_num), 0)
        if act in ("on", "high", "enable") or any(w in collapsed for w in ["high", "on", "enable"]):
            ok, msg = set_pin_state(pin_num, True, slot_num=rev_slot)
            return True, ok, msg
        if act in ("off", "low", "disable") or any(w in collapsed for w in ["low", "off", "disable"]):
            ok, msg = set_pin_state(pin_num, False, slot_num=rev_slot)
            return True, ok, msg

    return False, False, ""


def try_local_math(query: str):
    cleaned = query.lower().strip()
    cleaned = cleaned.replace("what is", "").replace("what's", "").replace("calculate", "")
    cleaned = cleaned.replace("plus", "+").replace("minus", "-")
    cleaned = cleaned.replace("multiplied by", "*").replace("times", "*").replace("x", "*")
    cleaned = cleaned.replace("divided by", "/").replace("over", "/")
    cleaned = cleaned.strip(" ?.")

    if re.fullmatch(r"[0-9+\-*/().\s]+", cleaned) and re.search(r"\d", cleaned):
        try:
            val = eval(cleaned, {"__builtins__": None}, {})
            if isinstance(val, (int, float)):
                if isinstance(val, float) and val.is_integer():
                    val = int(val)
                else:
                    val = round(val, 4)
                return f"{cleaned} = {val}"
        except Exception:
            return None
    return None


def trim_sentences(text: str, max_sentences: int = 3) -> str:
    clean = " ".join(text.split()).strip()
    if not clean:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", clean)
    return " ".join(parts[:max_sentences]).strip()


def clean_topic(q: str) -> str:
    q = re.sub(
        r"^(who is|who was|what is|what are|what was|tell me about|explain|define|where is)\s+",
        "",
        q.strip(),
        flags=re.IGNORECASE,
    ).strip(" ?.")
    return q or q.strip()


def query_wikipedia(query: str) -> str:
    headers = {"User-Agent": "HammerDucatHUD/11.0 (RaspberryPi)"}
    topic = clean_topic(query)

    search_url = (
        "https://en.wikipedia.org/w/api.php?"
        f"action=opensearch&search={urllib.parse.quote(topic)}&limit=1&namespace=0&format=json"
    )
    r = requests.get(search_url, headers=headers, timeout=8)
    r.raise_for_status()
    data = r.json()
    titles = data[1] if len(data) > 1 else []
    if not titles:
        return ""

    summary_url = (
        f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(titles[0])}"
    )
    sr_resp = requests.get(summary_url, headers=headers, timeout=8)
    sr_resp.raise_for_status()
    sdata = sr_resp.json()
    extract = (sdata.get("extract") or "").strip()
    if extract and sdata.get("type") != "disambiguation":
        return trim_sentences(extract, max_sentences=3)
    return ""


def query_duckduckgo(query: str) -> str:
    url = (
        "https://api.duckduckgo.com/?"
        f"q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
    )
    resp = requests.get(url, timeout=8)
    resp.raise_for_status()
    data = resp.json()

    for field in ["Answer", "AbstractText", "Definition"]:
        if data.get(field):
            return trim_sentences(str(data[field]), max_sentences=3)

    for item in data.get("RelatedTopics", []):
        if isinstance(item, dict) and item.get("Text"):
            return trim_sentences(str(item["Text"]), max_sentences=2)
    return ""


def query_open_meteo_weather(city: str) -> str:
    geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1"
    gr = requests.get(geo_url, timeout=8)
    gr.raise_for_status()
    results = gr.json().get("results") or []
    if not results:
        return ""

    place = results[0]
    lat, lon = place["latitude"], place["longitude"]
    name = place.get("name", city)
    w_url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&current=temperature_2m,wind_speed_10m"
    )
    wr = requests.get(w_url, timeout=8)
    wr.raise_for_status()
    cur = wr.json().get("current") or {}
    temp = cur.get("temperature_2m")
    wind = cur.get("wind_speed_10m")
    if temp is not None:
        return f"{name}: {temp} C, wind {wind} km/h."
    return ""


def answer_question(question: str):
    q_low = question.lower().strip()
    now = datetime.datetime.now()

    ducat_ans, _ = answer_from_ducat_knowledge(q_low)
    if ducat_ans:
        return ducat_ans, "HAPPY"

    if re.search(r"\b(date|day|today)\b", q_low) and any(
        w in q_low for w in ["what", "which", "today", "date", "day"]
    ):
        return f"{now.strftime('%A, %B %d, %Y')}", "HAPPY"

    if re.search(r"\btime\b", q_low):
        return f"{now.strftime('%I:%M:%S %p')}", "HAPPY"

    math_ans = try_local_math(question)
    if math_ans:
        return math_ans, "HAPPY"

    weather_match = re.search(r"\bweather(?:\s+(?:in|at|for))?\s+([a-zA-Z\s]+)", q_low)
    if weather_match:
        city = weather_match.group(1).strip(" ?.")
        try:
            w_ans = query_open_meteo_weather(city)
            if w_ans:
                return w_ans, "HAPPY"
        except Exception:
            pass

    try:
        wiki_ans = query_wikipedia(question)
        if wiki_ans:
            return wiki_ans, "HAPPY"
    except Exception:
        pass

    try:
        ddg_ans = query_duckduckgo(question)
        if ddg_ans:
            return ddg_ans, "HAPPY"
    except Exception:
        pass

    return f"No result for '{question}'.", "NORMAL"


def execute_search_worker(full_query: str):
    global sleep_mode, hardware_mode, courses_mode, course_index
    q = full_query.strip()
    low = q.lower()

    if SEARCH_START_DELAY > 0:
        time.sleep(SEARCH_START_DELAY)

    if not q:
        touch_activity()
        event_queue.put(("state", ("NORMAL", "", "", False)))
        return

    if is_enter_courses_phrase(low):
        with state_lock:
            sleep_mode = False
            hardware_mode = False
            courses_mode = True
            course_index = 0
        touch_activity()
        top = COURSE_TOPICS[0]
        event_queue.put(("state", ("HAPPY", q, f"{top['title']}: {top['summary']}", False)))
        return

    if is_enter_hardware_phrase(low):
        with state_lock:
            sleep_mode = False
            hardware_mode = True
            courses_mode = False
        touch_activity()
        event_queue.put(("state", ("HAPPY", q, "Hardware mode: say 'first on', 'forth off', or 'all off'.", False)))
        return

    if is_exit_phrase(low):
        with state_lock:
            hardware_mode = False
            courses_mode = False
        touch_activity()
        event_queue.put(("state", ("NORMAL", q, "", False)))
        return

    if "deep sleep" in low or "go to sleep" in low or low == "sleep":
        with state_lock:
            sleep_mode = True
            hardware_mode = False
            courses_mode = False
        touch_activity()
        event_queue.put(("state", ("SLEEP", q, "", True)))
        return

    ans, mood = answer_question(q)
    touch_activity()
    event_queue.put(("state", (mood, q, ans, False)))


def handle_phrase(phrase: str):
    global sleep_mode, hardware_mode, courses_mode, course_index
    global capturing_session, captured_chunks, last_speech_time

    clean = phrase.strip()
    low = clean.lower()
    if not low:
        return

    now_ts = time.time()

    with state_lock:
        in_courses = courses_mode
        cur_idx = course_index

    if is_enter_courses_phrase(low):
        touch_activity()
        with state_lock:
            sleep_mode = False
            hardware_mode = False
            courses_mode = True
            course_index = 0
            capturing_session = False
            captured_chunks = []
        top = COURSE_TOPICS[0]
        event_queue.put(("state", ("HAPPY", clean, f"{top['title']}: {top['summary']}", False)))
        return

    if is_enter_hardware_phrase(low):
        touch_activity()
        with state_lock:
            sleep_mode = False
            hardware_mode = True
            courses_mode = False
            capturing_session = False
            captured_chunks = []
        event_queue.put(("state", ("HAPPY", clean, "Say 'first on' / 'forth off' / 'all on' / 'all off'.", False)))
        return

    if is_exit_phrase(low):
        touch_activity()
        with state_lock:
            hardware_mode = False
            courses_mode = False
            capturing_session = False
            captured_chunks = []
        event_queue.put(("state", ("NORMAL", clean, "", False)))
        return

    if in_courses:
        if any(w in low for w in ["website", "site", "scroll", "ducatrise"]):
            touch_activity()
            launch_website_autoscroll()
            event_queue.put(("state", ("HAPPY", clean, "Opening ducatrise.com & auto-scrolling...", False)))
            return

        if any(w in low for w in ["next", "forward", "more"]):
            nxt = (cur_idx + 1) % len(COURSE_TOPICS)
            touch_activity()
            with state_lock:
                course_index = nxt
            top = COURSE_TOPICS[nxt]
            event_queue.put(("state", ("HAPPY", clean, f"{top['title']}: {top['summary']}", False)))
            return

        if any(w in low for w in ["previous", "prev", "back"]):
            prv = (cur_idx - 1) % len(COURSE_TOPICS)
            touch_activity()
            with state_lock:
                course_index = prv
            top = COURSE_TOPICS[prv]
            event_queue.put(("state", ("HAPPY", clean, f"{top['title']}: {top['summary']}", False)))
            return

        topic_ans, matched_idx = answer_from_ducat_knowledge(low)
        if topic_ans and matched_idx is not None:
            touch_activity()
            with state_lock:
                course_index = matched_idx
            top = COURSE_TOPICS[matched_idx]
            event_queue.put(("state", ("HAPPY", clean, f"{top['title']}: {topic_ans}", False)))
            return

    matched_gpio, ok_gpio, gpio_msg = try_gpio_command(low)
    if matched_gpio:
        touch_activity()
        with state_lock:
            is_slp = sleep_mode
        next_exp = "SLEEP" if is_slp else ("HAPPY" if ok_gpio else "ALERT")
        event_queue.put(("state", (next_exp, clean, gpio_msg, is_slp)))
        return

    has_hammer = bool(re.search(r"\bhammer\b", low))

    with state_lock:
        is_capturing = capturing_session

    if not is_capturing:
        if has_hammer:
            touch_activity()
            after_hammer = re.sub(r"^.*?\bhammer\b", "", low, count=1).strip(" ,.!?")
            with state_lock:
                sleep_mode = False
                capturing_session = True
                captured_chunks = [after_hammer] if after_hammer else []
                last_speech_time = now_ts

            live_stt = " ".join(captured_chunks) if captured_chunks else "Listening..."
            event_queue.put(("state", ("LISTENING", live_stt, "", False)))
        return

    touch_activity()
    with state_lock:
        captured_chunks.append(clean)
        combined = " ".join(captured_chunks)
        last_speech_time = now_ts
    event_queue.put(("state", ("LISTENING", combined, "", False)))


def capture_and_idle_loop():
    global sleep_mode, capturing_session, captured_chunks
    while running:
        time.sleep(0.15)
        now_ts = time.time()

        with state_lock:
            is_cap = capturing_session
            chunks_copy = list(captured_chunks)
            speech_age = now_ts - last_speech_time
            already_sleeping = sleep_mode
            idle_elapsed = now_ts - last_activity_time

        if is_cap and chunks_copy and speech_age >= POST_SPEECH_SETTLE_SECONDS:
            full_q = " ".join(chunks_copy).strip()
            with state_lock:
                capturing_session = False
                captured_chunks = []
            event_queue.put(("state", ("THINKING", full_q, "", False)))
            threading.Thread(target=execute_search_worker, args=(full_q,), daemon=True).start()
            continue

        if not is_cap and not already_sleeping and idle_elapsed >= IDLE_SLEEP_SECONDS:
            with state_lock:
                sleep_mode = True
                capturing_session = False
                captured_chunks = []
            event_queue.put(("state", ("SLEEP", "", "", True)))


def voice_loop():
    global voice_energy
    rec = sr.Recognizer()
    rec.dynamic_energy_threshold = True
    rec.pause_threshold = 2.0
    rec.non_speaking_duration = 1.2

    try:
        with suppress_alsa_stderr():
            mic = (
                sr.Microphone(device_index=MIC_DEVICE_INDEX)
                if MIC_DEVICE_INDEX is not None
                else sr.Microphone()
            )

        with mic as source:
            with suppress_alsa_stderr():
                rec.adjust_for_ambient_noise(source, duration=1.0)

            while running:
                try:
                    with suppress_alsa_stderr():
                        audio = rec.listen(source, timeout=5, phrase_time_limit=15)
                    raw_bytes = audio.get_raw_data()
                    if raw_bytes:
                        sample_step = max(2, len(raw_bytes) // 400)
                        peak = 0
                        for i in range(0, len(raw_bytes) - 1, sample_step):
                            val = int.from_bytes(raw_bytes[i:i + 2], "little", signed=True)
                            if abs(val) > peak:
                                peak = abs(val)
                        with state_lock:
                            voice_energy = min(1.0, peak / 14000.0)

                    phrase = rec.recognize_google(audio).strip()
                    if phrase:
                        handle_phrase(phrase)
                except sr.WaitTimeoutError:
                    with state_lock:
                        voice_energy *= 0.6
                    continue
                except sr.UnknownValueError:
                    with state_lock:
                        voice_energy *= 0.6
                    continue
                except sr.RequestError as e:
                    event_queue.put(("state", ("ALERT", "", f"Speech error: {e}", False)))
    except Exception as e:
        event_queue.put(("state", ("ALERT", "", f"Mic error: {e}", False)))


def draw_sleep_ducat_rise_animation(surface, t):
    """Animated DUCAT RISE centerpiece shown exclusively in SLEEP mode."""
    surface.fill((4, 8, 18))
    cx, cy = width // 2, int(height * 0.44)

    for idx in range(4):
        phase = (t * 0.72 + idx * 0.25) % 1.0
        radius = int(55 + phase * min(width, height) * 0.34)
        alpha = max(12, int(185 * (1.0 - phase)))
        ring_surf = pygame.Surface((width, height), pygame.SRCALPHA)
        ring_col = (0, 235, 255) if idx % 2 == 0 else (255, 165, 45)
        pygame.draw.circle(ring_surf, (*ring_col, alpha), (cx, cy), radius, 3)
        surface.blit(ring_surf, (0, 0))

    for orb_i in range(14):
        ang = t * 1.55 + (2 * math.pi * orb_i / 14)
        ox = int(cx + math.cos(ang) * min(width * 0.28, 220))
        oy = int(cy + math.sin(ang) * min(height * 0.16, 110))
        orb_col = (60, 250, 160) if orb_i % 2 == 0 else (255, 215, 75)
        pygame.draw.circle(surface, orb_col, (ox, oy), 6)

    hue_t = 0.5 + 0.5 * math.sin(t * 2.5)
    txt_col = (
        int(40 + 215 * hue_t),
        int(238 - 50 * hue_t),
        int(255 - 120 * hue_t),
    )
    bob_y = int(math.sin(t * 3.0) * 8)
    brand_font = get_fitting_font("DUCAT RISE", width - 140, font_sleep_brand.get_height(), 28, True)
    brand_surf = brand_font.render("DUCAT RISE", True, txt_col)
    box_w = min(width - 80, brand_surf.get_width() + 68)
    box_h = brand_surf.get_height() + 28
    box_rect = pygame.Rect(cx - box_w // 2, cy - box_h // 2 + bob_y, box_w, box_h)

    pygame.draw.rect(surface, (10, 20, 40), box_rect, border_radius=16)
    pygame.draw.rect(surface, (0, 235, 255), box_rect, 3, border_radius=16)

    sweep_x = box_rect.x + 12 + int(((t * 240.0) % max(20, box_w - 24)))
    pygame.draw.rect(surface, (255, 200, 65), (sweep_x, box_rect.y + 5, 22, box_h - 10), border_radius=6)

    surface.blit(brand_surf, brand_surf.get_rect(center=box_rect.center))

    draw_centered_wrapped(
        surface,
        "SLEEP MODE  •  Say 'Hammer' to wake or 'enter courses'",
        font_stt,
        (170, 220, 245),
        cx,
        int(height * 0.78),
        width - 80,
        max_lines=2,
    )


def draw_courses_mode_screen(surface, t, idx, stt_str, rep_str):
    """Clean, well-proportioned Ducat RISE & Ducat India explorer with zero clipping."""
    surface.fill((4, 8, 18))
    pad_x = max(24, int(width * 0.04))
    inner_w = width - pad_x * 2

    pygame.draw.rect(surface, (0, 220, 255), (14, 14, width - 28, height - 28), 2, border_radius=14)

    title_font = get_fitting_font("DUCAT RISE & DUCAT INDIA COURSES", inner_w, font_mode_title.get_height(), 20, True)
    title_surf = title_font.render("DUCAT RISE & DUCAT INDIA COURSES", True, (65, 245, 255))
    surface.blit(title_surf, title_surf.get_rect(center=(width // 2, 46)))

    y_after_sub = draw_centered_wrapped(
        surface,
        "Say 'next', 'previous', 'python', '3d', 'finance', 'ducat india', 'website', or 'exit'",
        font_stt,
        (255, 210, 95),
        width // 2,
        74,
        inner_w,
        max_lines=2,
        line_gap=4,
    )

    pill_y = y_after_sub + 10
    pill_h = max(30, int(height * 0.046))
    pill_w = inner_w // len(COURSE_TOPICS)
    for i, item in enumerate(COURSE_TOPICS):
        px = pad_x + i * pill_w
        active = (i == idx)
        p_rect = pygame.Rect(px + 3, pill_y, max(20, pill_w - 6), pill_h)
        pygame.draw.rect(surface, (18, 65, 92) if active else (10, 18, 34), p_rect, border_radius=8)
        pygame.draw.rect(surface, (60, 250, 160) if active else (45, 70, 100), p_rect, 2, border_radius=8)
        lbl_font = get_fitting_font(item["key"].upper(), p_rect.width - 8, font_pill.get_height(), 11, True)
        lbl_surf = lbl_font.render(item["key"].upper(), True, (255, 235, 120) if active else (165, 190, 215))
        surface.blit(lbl_surf, lbl_surf.get_rect(center=p_rect.center))

    card_top = pill_y + pill_h + 14
    bottom_reserve = max(92, int(height * 0.14))
    card_h = max(170, height - card_top - bottom_reserve)
    card_rect = pygame.Rect(pad_x, card_top, inner_w, card_h)

    pulse_g = int(190 + 50 * math.sin(t * 4.0))
    pygame.draw.rect(surface, (9, 22, 42), card_rect, border_radius=12)
    pygame.draw.rect(surface, (0, pulse_g, 255), card_rect, 2, border_radius=12)

    cur_topic = COURSE_TOPICS[idx % len(COURSE_TOPICS)]
    card_text_w = card_rect.width - 44
    t_font = get_fitting_font(cur_topic["title"], card_text_w, font_card_title.get_height(), 18, True)
    t_surf = t_font.render(cur_topic["title"], True, (65, 250, 165))
    surface.blit(t_surf, (card_rect.x + 22, card_rect.y + 16))

    body_y = card_rect.y + 22 + t_surf.get_height() + 8
    body_lines = wrap_text(cur_topic["summary"], font_body, card_text_w)
    body_step = font_body.get_linesize() + 5
    max_body_lines = max(2, (card_rect.bottom - body_y - 38) // max(1, body_step))

    for line_i, line in enumerate(body_lines[:max_body_lines]):
        b_surf = font_body.render(line, True, (242, 248, 255))
        surface.blit(b_surf, (card_rect.x + 22, body_y + line_i * body_step))

    url_surf = font_stt.render(f"Source: {DUCAT_RISE_URL}", True, (255, 200, 80))
    surface.blit(url_surf, (card_rect.x + 22, card_rect.bottom - url_surf.get_height() - 12))

    foot_y = card_rect.bottom + 8
    if stt_str:
        foot_y = draw_centered_wrapped(surface, stt_str, font_stt, (0, 235, 255), width // 2, foot_y, inner_w, 1, 2)
    if rep_str:
        draw_centered_wrapped(surface, rep_str, font_stt, (255, 230, 120), width // 2, foot_y + 2, inner_w, 2, 3)


def draw_gpio_action_burst(surface, t, act_age, act_high, act_slot):
    if act_age > 1.8:
        return
    progress = act_age / 1.8
    alpha = max(0, int(210 * (1.0 - progress)))
    cx = width // 2
    cy = int(height * 0.45)

    ring_col = (40, 255, 140) if act_high else (255, 95, 130)
    burst_surf = pygame.Surface((width, height), pygame.SRCALPHA)

    for ring_i in range(3):
        r = int(45 + progress * (260 + ring_i * 65))
        pygame.draw.circle(burst_surf, (*ring_col, max(10, alpha // (ring_i + 1))), (cx, cy), r, 3)

    for p_idx in range(18):
        ang = (2 * math.pi * p_idx / 18) + (t * (1.8 if act_high else -1.8))
        dist = 35 + progress * 240
        px = int(cx + math.cos(ang) * dist)
        py = int(cy + math.sin(ang) * dist)
        p_col = (255, 200, 60) if p_idx % 2 == 0 else (0, 230, 255)
        pygame.draw.circle(burst_surf, (*p_col, alpha), (px, py), 4)

    surface.blit(burst_surf, (0, 0))


def draw_hardware_mode_screen(surface, t, stt_str, rep_str, act_age, act_high, act_slot):
    surface.fill((4, 8, 18))
    pad_x = max(24, int(width * 0.05))
    inner_w = width - pad_x * 2

    glow_c = (0, 225, 255) if int(t * 2) % 2 == 0 else (255, 145, 45)
    pygame.draw.rect(surface, glow_c, (14, 14, width - 28, height - 28), 2, border_radius=14)

    title_surf = font_mode_title.render("HARDWARE CONTROL MODE", True, (90, 245, 255))
    surface.blit(title_surf, title_surf.get_rect(center=(width // 2, 46)))

    y_sub = draw_centered_wrapped(
        surface,
        "Say 'first on', 'forth off', 'all on', 'all off'  |  Say 'exit' or 'leave'",
        font_stt,
        (255, 205, 95),
        width // 2,
        74,
        inner_w,
        max_lines=2,
        line_gap=3,
    )

    slot_names = {1: "FIRST", 2: "SECOND", 3: "THIRD", 4: "FOURTH", 5: "FIFTH"}
    accent_palette = [
        (45, 245, 150),
        (0, 220, 255),
        (255, 185, 45),
        (255, 95, 165),
        (155, 125, 255),
    ]
    states = get_gpio_states_snapshot()

    footer_space = max(72, int(height * 0.11))
    avail_h = max(220, height - (y_sub + 14) - footer_space)
    gap = max(8, int(avail_h * 0.025))
    card_h = max(42, (avail_h - gap * (len(states) - 1)) // len(states))
    start_y = y_sub + 12

    for idx, (slot, pin, is_on) in enumerate(states):
        cy = start_y + idx * (card_h + gap)
        pal = accent_palette[idx % len(accent_palette)]
        is_recently_toggled = (act_age < 1.6) and (act_slot == 0 or act_slot == slot)

        if is_on:
            border_col = (255, 235, 110) if is_recently_toggled else pal
            bg_col = (14, 42, 48)
            badge_col = (60, 255, 150)
            state_label = "HIGH (ON)"
        else:
            border_col = (255, 95, 130) if is_recently_toggled else (42, 62, 92)
            bg_col = (12, 18, 34)
            badge_col = (255, 125, 145) if is_recently_toggled else (130, 150, 175)
            state_label = "LOW (OFF)"

        rect = pygame.Rect(pad_x, cy, inner_w, card_h)
        pygame.draw.rect(surface, bg_col, rect, border_radius=9)
        pygame.draw.rect(surface, border_col, rect, 3 if is_recently_toggled else 2, border_radius=9)

        ord_label = slot_names.get(slot, f"SLOT {slot}")
        left_txt = font_hw_item.render(f"{ord_label} -> GPIO {pin}", True, (245, 250, 255))
        right_txt = font_hw_item.render(state_label, True, badge_col)

        left_x = rect.x + 18
        right_x = rect.right - right_txt.get_width() - 18
        surface.blit(left_txt, (left_x, cy + (card_h - left_txt.get_height()) // 2))
        surface.blit(right_txt, (right_x, cy + (card_h - right_txt.get_height()) // 2))

        meter_start = left_x + left_txt.get_width() + 16
        meter_end = right_x - 16
        if meter_end - meter_start >= 40:
            mw = min(120, meter_end - meter_start)
            mx = (meter_start + meter_end - mw) // 2
            my = cy + card_h // 2 - 6
            pygame.draw.rect(surface, (22, 32, 52), (mx, my, mw, 12), border_radius=6)
            if is_on:
                anim_fill = int(mw * (0.75 + 0.25 * math.sin(t * 9.0 + idx)))
                pygame.draw.rect(surface, pal, (mx, my, anim_fill, 12), border_radius=6)

    draw_gpio_action_burst(surface, t, act_age, act_high, act_slot)

    bottom_y = start_y + len(states) * (card_h + gap) + 8
    if stt_str:
        bottom_y = draw_centered_wrapped(surface, stt_str, font_stt, (0, 235, 255), width // 2, bottom_y, inner_w, 1, 2)
    if rep_str:
        draw_centered_wrapped(surface, rep_str, font_stt, (255, 225, 110), width // 2, bottom_y + 2, inner_w, 1, 2)


def draw_scifi_hud_backdrop(surface, t, expr, iris_col):
    """Draws background rings and curved cyan accents matching WhatsApp Image 2026-11-24 at 3.21.03 PM.jpeg."""
    surface.fill((7, 17, 36))
    cx = width // 2
    cy = int(height * 0.30)

    ring_col = (18, 135, 195) if expr != "THINKING" else (215, 165, 55)
    for idx, mult in enumerate([0.72, 1.02, 1.32]):
        r = int(min(width, height) * 0.18 * mult)
        pygame.draw.circle(surface, ring_col, (cx, cy), r, 1)

    # Outer cyan curved brackets & inner arcs from reference image
    arc_r_outer = int(min(width, height) * 0.24)
    arc_r_inner = int(min(width, height) * 0.14)
    for side in (-1, 1):
        ox = cx + side * int(width * 0.19)
        rect_o = pygame.Rect(ox - arc_r_outer, cy - arc_r_outer, arc_r_outer * 2, arc_r_outer * 2)
        a1 = math.radians(135 if side < 0 else -45)
        a2 = math.radians(225 if side < 0 else 45)
        pygame.draw.arc(surface, (28, 195, 245), rect_o, a1, a2, 3)

        rect_i = pygame.Rect(cx - arc_r_inner, cy - arc_r_inner, arc_r_inner * 2, arc_r_inner * 2)
        b1 = math.radians(150 if side < 0 else -30)
        b2 = math.radians(210 if side < 0 else 30)
        pygame.draw.arc(surface, (28, 195, 245), rect_i, b1, b2, 2)


def build_curvy_eyelid_points(cx: int, cy: int, ew: int, cur_h: int, steps: int = 28):
    """Smooth curved top and bottom almond/curvy eye contour."""
    top_pts = []
    bot_pts = []
    half_w = ew / 2.0
    half_h = cur_h / 2.0

    for i in range(steps + 1):
        u = (i / steps) * 2.0 - 1.0
        x = cx + u * half_w
        arch = math.cos(u * (math.pi * 0.5)) ** 0.75
        y_top = cy - half_h * arch + (1.0 - arch) * (half_h * 0.10)
        y_bot = cy + half_h * arch - (1.0 - arch) * (half_h * 0.10)
        top_pts.append((int(round(x)), int(round(y_top))))
        bot_pts.append((int(round(x)), int(round(y_bot))))

    return top_pts, bot_pts


def draw_curvy_colored_eye(surface, cx, cy, ew, eh, t, expr, is_right_eye, smooth_open, iris_col):
    """Draws curvy eye with full filled iris (no lower red oval patch) and smooth color transition."""
    open_factor = max(0.06, min(1.0, smooth_open))
    cur_h = max(10, int(eh * open_factor))

    gaze_x = 0.0
    gaze_y = 0.0
    if expr == "THINKING":
        gaze_x = math.sin(t * 2.2) * (ew * 0.06)
        gaze_y = -cur_h * 0.03 + math.cos(t * 1.6) * (cur_h * 0.02)
    elif expr == "LISTENING":
        gaze_x = math.sin(t * 3.8) * (ew * 0.03)
    elif expr == "HAPPY":
        gaze_y = -cur_h * 0.03

    top_pts, bot_pts = build_curvy_eyelid_points(cx, cy, ew, cur_h, steps=28)
    poly_pts = top_pts + list(reversed(bot_pts))

    clip_rect = pygame.Rect(cx - ew // 2 - 6, cy - cur_h // 2 - 6, ew + 12, cur_h + 12)
    prev_clip = surface.get_clip()
    surface.set_clip(clip_rect)

    # 1. Soft pale cyan-white sclera matching reference
    pygame.draw.polygon(surface, (186, 238, 242), poly_pts)

    # 2. Full, uniformly filled iris (no extra lower red oval patch)
    if open_factor > 0.15:
        iris_r = max(16, int(min(ew * 0.25, eh * 0.42) * min(1.0, open_factor * 1.08)))
        ix = int(cx + gaze_x)
        iy = int(cy + gaze_y)

        # Smooth full fill of the active color + subtle outer border & soft radial depth
        pygame.draw.circle(surface, shade_rgb(iris_col, 0.55), (ix, iy), iris_r)
        pygame.draw.circle(surface, iris_col, (ix, iy), max(10, iris_r - 3))
        pygame.draw.circle(surface, shade_rgb(iris_col, 1.15), (ix, iy), max(7, int(iris_r * 0.68)))

        # Crisp dark pupil in center
        pupil_r = max(6, int(iris_r * (0.42 if expr == "LISTENING" else 0.36)))
        pygame.draw.circle(surface, (28, 22, 24), (ix, iy), pupil_r)

        # Clean upper-left and lower-right catchlights
        pygame.draw.circle(
            surface,
            (255, 255, 255),
            (ix - int(iris_r * 0.34), iy - int(iris_r * 0.26)),
            max(4, int(iris_r * 0.19)),
        )
        pygame.draw.circle(
            surface,
            (255, 255, 255),
            (ix + int(iris_r * 0.28), iy + int(iris_r * 0.28)),
            max(2, int(iris_r * 0.09)),
        )

    surface.set_clip(prev_clip)

    # 3. Curvy outer eye border + white curved arc beside each eye
    pygame.draw.lines(surface, (145, 215, 224), False, top_pts, 2)
    pygame.draw.lines(surface, (145, 215, 224), False, bot_pts, 2)

    arc_side = 1 if not is_right_eye else -1
    arc_x = cx + arc_side * int(ew * 0.56)
    arc_rect = pygame.Rect(arc_x - int(ew * 0.28), cy - int(eh * 0.46), int(ew * 0.56), int(eh * 0.92))
    a_start = math.radians(-55 if arc_side > 0 else 125)
    a_end = math.radians(55 if arc_side > 0 else 235)
    pygame.draw.arc(surface, (238, 242, 248), arc_rect, a_start, a_end, 4)


def draw_listening_waveform(surface, t, energy):
    bar_count = 46
    total_w = int(width * 0.56)
    start_x = (width - total_w) // 2
    center_y = int(height * 0.52)
    max_bar_h = int(height * 0.095)
    step = total_w / max(1, bar_count - 1)
    bar_w = max(3, int(step * 0.52))

    amp_boost = 0.35 + 0.65 * max(energy, 0.25 + 0.2 * math.sin(t * 4.0))

    for i in range(bar_count):
        norm_pos = (i / max(1, bar_count - 1)) * 2.0 - 1.0
        envelope = max(0.12, math.cos(norm_pos * (math.pi * 0.46)))

        wave1 = 0.55 + 0.45 * math.sin(t * 11.0 + i * 0.42)
        wave2 = 0.5 + 0.5 * math.cos(t * 7.2 - i * 0.27)
        h_val = max(6, int(max_bar_h * envelope * (0.35 + 0.65 * wave1 * wave2) * amp_boost))

        mix = (i / max(1, bar_count - 1))
        r = int(20 + 210 * (mix ** 2) * 0.25)
        g = int(165 + 80 * envelope)
        b = int(220 - 80 * mix)

        x = int(start_x + i * step) - bar_w // 2
        y = center_y - h_val // 2
        pygame.draw.rect(surface, (r, g, b), (x, y, bar_w, h_val), border_radius=bar_w // 2)


def draw_sleep_screensaver(surface, t, mode_name):
    if mode_name == "DUCAT_RISE":
        draw_sleep_ducat_rise_animation(surface, t)
        return

    surface.fill((4, 8, 18))

    if mode_name == "AURORA":
        aurora_surf = pygame.Surface((width, height), pygame.SRCALPHA)
        for band in range(3):
            pts_top = []
            pts_bot = []
            base_y = height * (0.28 + band * 0.16)
            col = [(30, 240, 175, 55), (45, 175, 255, 50), (165, 115, 255, 45)][band]
            for x in range(0, width + 25, 24):
                wave = math.sin(x * 0.008 + t * (1.2 + band * 0.35) + band) * 34
                y_t = base_y + wave
                pts_top.append((x, int(y_t)))
                pts_bot.append((x, int(y_t + 68)))
            pygame.draw.polygon(aurora_surf, col, pts_top + list(reversed(pts_bot)))
        surface.blit(aurora_surf, (0, 0))

        for mote in aurora_motes:
            mx = int(mote[0] * width + math.sin(t * 0.7 + mote[3]) * 18)
            my = int(mote[1] * height + math.cos(t * 0.6 + mote[3]) * 14)
            twinkle = 0.5 + 0.5 * math.sin(t * mote[2] + mote[3])
            pygame.draw.circle(surface, (190, 250, 245), (mx, my), max(2, int(2 + twinkle * 3)))

        draw_centered_wrapped(
            surface,
            "SLEEP MODE  •  AURORA DRIFT",
            font_stt,
            (175, 235, 225),
            width // 2,
            int(height * 0.80),
            width - 80,
            max_lines=1,
        )

    elif mode_name == "ORBIT_PULSE":
        cx, cy = width // 2, int(height * 0.45)
        breath = 0.85 + 0.15 * math.sin(t * 2.2)
        for ring_i in range(4):
            r = int((52 + ring_i * 44) * breath)
            c = [(55, 235, 255), (90, 250, 175), (255, 195, 75), (155, 130, 255)][ring_i]
            pygame.draw.circle(surface, c, (cx, cy), r, 2)

        for sat_i in range(6):
            ang = t * (0.9 + sat_i * 0.12) + sat_i * (math.pi / 3.0)
            rad = 95 + (sat_i % 3) * 44
            sx = int(cx + math.cos(ang) * rad)
            sy = int(cy + math.sin(ang) * rad * 0.65)
            pygame.draw.circle(surface, (255, 235, 130), (sx, sy), 6)

        draw_centered_wrapped(
            surface,
            "SLEEP MODE  •  ORBIT PULSE",
            font_stt,
            (180, 220, 250),
            cx,
            int(height * 0.80),
            width - 80,
            max_lines=1,
        )

    elif mode_name == "MATRIX":
        for idx in range(num_cols):
            x = idx * 22 + 4
            y = int(matrix_drops[idx] * 16)
            for trail in range(7):
                ty = y - trail * 18
                if 0 <= ty <= height:
                    ch = chr(33 + ((idx * 17 + trail * 13 + int(t * 14)) % 89))
                    col = (190, 255, 220) if trail == 0 else (0, max(35, 220 - trail * 28), 95)
                    surf = font_matrix.render(ch, True, col)
                    surface.blit(surf, (x, ty))
            matrix_drops[idx] += matrix_speeds[idx] * 0.35
            if matrix_drops[idx] * 16 > height + 120:
                matrix_drops[idx] = random.randint(-15, -2)

    elif mode_name == "WARP":
        cx, cy = width // 2, height // 2
        for star in warp_stars:
            star[2] -= 0.018
            if star[2] <= 0.05:
                star[0] = random.uniform(-1.0, 1.0)
                star[1] = random.uniform(-1.0, 1.0)
                star[2] = 1.0
            sx = int(cx + (star[0] / star[2]) * (width * 0.45))
            sy = int(cy + (star[1] / star[2]) * (height * 0.45))
            prev_sx = int(cx + (star[0] / min(1.0, star[2] + 0.04)) * (width * 0.45))
            prev_sy = int(cy + (star[1] / min(1.0, star[2] + 0.04)) * (height * 0.45))
            if 0 <= sx < width and 0 <= sy < height:
                bright = max(50, min(255, int((1.0 - star[2]) * 255)))
                pygame.draw.line(surface, (0, bright, min(255, bright + 35)), (prev_sx, prev_sy), (sx, sy), 2)
        pulse_r = int(45 + 25 * math.sin(t * 4.0))
        pygame.draw.circle(surface, (0, 210, 255), (cx, cy), pulse_r, 2)

    else:  # "DINO" screensaver
        ground_y = int(height * 0.68)
        pygame.draw.line(surface, (0, 210, 240), (30, ground_y), (width - 30, ground_y), 3)

        tick_shift = int(t * 240) % 40
        for gx in range(40 - tick_shift, width - 30, 40):
            pygame.draw.line(surface, (20, 75, 105), (gx, ground_y + 8), (gx + 14, ground_y + 8), 2)

        jump_phase = (t * 1.45) % 2.4
        if jump_phase < 0.85:
            arc = math.sin((jump_phase / 0.85) * math.pi)
            jump_h = int(arc * 115)
        else:
            jump_h = 0

        dino_x = int(width * 0.22)
        dino_y = ground_y - 44 - jump_h
        dino_col = (45, 245, 130)

        pygame.draw.rect(surface, dino_col, (dino_x, dino_y + 12, 34, 24), border_radius=4)
        pygame.draw.rect(surface, dino_col, (dino_x + 22, dino_y, 22, 18), border_radius=3)
        pygame.draw.rect(surface, (5, 10, 18), (dino_x + 34, dino_y + 4, 5, 5))
        pygame.draw.rect(surface, (255, 150, 30), (dino_x - 10, dino_y + 16, 12, 8))
        if int(t * 12) % 2 == 0 or jump_h > 0:
            pygame.draw.rect(surface, dino_col, (dino_x + 6, dino_y + 36, 7, 10))
            pygame.draw.rect(surface, (0, 160, 90), (dino_x + 22, dino_y + 36, 7, 6))
        else:
            pygame.draw.rect(surface, (0, 160, 90), (dino_x + 6, dino_y + 36, 7, 6))
            pygame.draw.rect(surface, dino_col, (dino_x + 22, dino_y + 36, 7, 10))

        for obs_idx in range(2):
            span = width - 120
            obs_x = int(width - 60 - (((t * 250 + obs_idx * (span * 0.55)) % span)))
            obs_h = 28 if obs_idx == 0 else 38
            obs_y = ground_y - obs_h
            pygame.draw.rect(surface, (255, 85, 65), (obs_x, obs_y, 16, obs_h), border_radius=3)


threading.Thread(target=voice_loop, daemon=True).start()
threading.Thread(target=capture_and_idle_loop, daemon=True).start()

start_ts = time.time()
smooth_openness = 1.0

while running:
    for event in pygame.event.get():
        if event.type in (pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN, pygame.QUIT):
            running = False

    while not event_queue.empty():
        kind, payload = event_queue.get()
        if kind == "state":
            expression, heard_text, reply_text, sleep_mode = payload

    now_wall = time.time()
    t_now = now_wall - start_ts

    with state_lock:
        is_sleeping = sleep_mode
        is_hw = hardware_mode
        is_courses = courses_mode
        cur_c_idx = course_index
        is_cap = capturing_session
        cur_energy = voice_energy
        act_age = now_wall - last_action_time
        act_high = last_action_high
        act_slot = last_action_slot

    if is_sleeping:
        if now_wall - last_saver_switch >= SCREENSAVER_SWITCH_SECONDS:
            choices = [m for m in SCREENSAVER_MODES if m != active_saver]
            active_saver = random.choice(choices)
            last_saver_switch = now_wall
        draw_sleep_screensaver(screen, t_now, active_saver)
    elif is_courses:
        draw_courses_mode_screen(screen, t_now, cur_c_idx, heard_text, reply_text)
    elif is_hw:
        draw_hardware_mode_screen(screen, t_now, heard_text, reply_text, act_age, act_high, act_slot)
    else:
        if expression == "THINKING":
            target_open = 0.84 + 0.10 * math.sin(t_now * 5.0)
        elif expression == "LISTENING" or is_cap:
            target_open = 0.96 + 0.04 * math.sin(t_now * 9.0)
        elif expression == "HAPPY":
            target_open = 0.92
        else:
            blink = t_now % 4.5
            if 4.3 <= blink <= 4.5:
                target_open = max(0.08, abs(1.0 - ((blink - 4.3) / 0.1)))
            else:
                target_open = 1.0

        smooth_openness = lerp(smooth_openness, target_open, 0.25)
        iris_rgb = get_smooth_eye_color(t_now)

        draw_scifi_hud_backdrop(screen, t_now, expression, iris_rgb)

        eye_cy = int(height * 0.30)
        eye_w = int(width * 0.27)
        eye_h = int(height * 0.19)
        eye_dx = int(width * 0.20)

        draw_curvy_colored_eye(screen, width // 2 - eye_dx, eye_cy, eye_w, eye_h, t_now, expression, False, smooth_openness, iris_rgb)
        draw_curvy_colored_eye(screen, width // 2 + eye_dx, eye_cy, eye_w, eye_h, t_now, expression, True, smooth_openness, iris_rgb)

        draw_gpio_action_burst(screen, t_now, act_age, act_high, act_slot)

        text_area_w = width - max(48, int(width * 0.08))
        cur_y = int(height * 0.45)

        if expression == "LISTENING" or is_cap:
            draw_listening_waveform(screen, t_now, cur_energy)
            cur_y = int(height * 0.59)

        if heard_text:
            cur_y = draw_centered_wrapped(
                screen,
                heard_text,
                font_stt,
                (0, 235, 255),
                width // 2,
                cur_y,
                text_area_w,
                max_lines=2,
                line_gap=4,
            ) + 6

        if reply_text:
            max_rep_lines = max(2, (height - cur_y - 24) // max(1, font_reply.get_linesize() + 6))
            draw_centered_wrapped(
                screen,
                reply_text,
                font_reply,
                (255, 225, 110),
                width // 2,
                cur_y,
                text_area_w,
                max_lines=min(5, max_rep_lines),
                line_gap=6,
            )

    pygame.display.flip()
    clock.tick(30)

set_all_gpios(False)
for dev in gpio_devices.values():
    dev.close()
pygame.quit()
sys.exit()
