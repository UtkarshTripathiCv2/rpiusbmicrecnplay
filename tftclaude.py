#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HAMMER - Ducat RISE voice assistant for Raspberry Pi  (v12, expressive edition)

Say "Hammer ..." to wake / ask a question.
  * "enter courses"   -> Ducat RISE & Ducat India course explorer
  * "enter hardware"  -> voice control of 5 GPIO pins ("first on", "forth off", "all off")
  * "go to sleep"     -> animated screensavers
Run `python3 hammer_assistant.py --selftest` on any PC (with SDL_VIDEODRIVER=dummy
if headless) to smoke-test every face / screen without a mic or GPIO.
"""
import ast
import contextlib
import datetime
import math
import operator
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

try:
    import speech_recognition as sr
except ImportError:  # keeps the UI usable for testing without the package
    sr = None

try:
    from gpiozero import OutputDevice
except Exception:  # not on a Pi -> use a fake pin so the UI still runs
    OutputDevice = None

SELFTEST = "--selftest" in sys.argv

# ==============================================================================
# 1. CONFIGURATION (NO API KEY OR PAID CREDITS REQUIRED)
# ==============================================================================
MIC_DEVICE_INDEX = None              # Set e.g. 1 or 2 if needed, or None for default mic
SPEECH_LANGUAGE = "en-IN"            # Google speech language (en-IN suits Indian accents)
IDLE_SLEEP_SECONDS = 60.0            # Auto-sleep after 1 minute of inactivity
DROWSY_BEFORE_SLEEP = 15.0           # Eyes get sleepy this many seconds before auto-sleep
POST_SPEECH_SETTLE_SECONDS = 3.0     # Wait up to 3 seconds after speaking before searching
SEARCH_START_DELAY = 0.45            # Short beat in THINKING state before lookup
SCREENSAVER_SWITCH_SECONDS = 8.5     # Switch sleep screensaver every N seconds
EYE_COLOR_CYCLE_SECONDS = 12.0       # Full smooth colour cycle duration
EXPRESSION_HOLD_SECONDS = 9.0        # Emotional faces relax back to NORMAL after this
TYPEWRITER_CPS = 60.0                # Reply typing speed (characters / second)
DEFAULT_CITY = "Delhi"               # Used when you ask "what's the weather" with no city
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


class FakeOutput:
    """Stand-in for gpiozero.OutputDevice when not running on a Pi."""

    def __init__(self, pin):
        self.pin = pin
        self._on = False

    def on(self):
        self._on = True

    def off(self):
        self._on = False

    @property
    def is_active(self):
        return self._on

    def close(self):
        pass


def make_output(pin: int):
    if OutputDevice is not None and not SELFTEST:
        try:
            return OutputDevice(pin, active_high=True, initial_value=False)
        except Exception:
            pass
    return FakeOutput(pin)


gpio_devices = {pin: make_output(pin) for pin in ALLOWED_PINS}

pygame.init()
pygame.display.set_caption("HAMMER")
info = pygame.display.Info()
width, height = info.current_w, info.current_h
if SELFTEST:
    width, height = 1024, 600
if width <= 0 or height <= 0:
    width, height = 1024, 768
S = min(width, height)

screen = pygame.display.set_mode((width, height), 0 if SELFTEST else pygame.FULLSCREEN)
pygame.mouse.set_visible(False)
clock = pygame.time.Clock()

_font_cache = {}


def make_font(size: int, bold: bool = True, mono: bool = False):
    size = max(12, int(size))
    key = (size, bold, mono)
    f = _font_cache.get(key)
    if f is None:
        family = "monospace" if mono else "dejavusans"
        f = pygame.font.SysFont(family, size, bold=bold)
        _font_cache[key] = f
    return f


font_sleep_brand = make_font(max(32, int(height * 0.062)))
font_mode_title = make_font(max(24, int(height * 0.044)))
font_card_title = make_font(max(22, int(height * 0.038)))
font_body = make_font(max(18, int(height * 0.030)))
font_stt = make_font(max(17, int(height * 0.028)))
font_reply = make_font(max(20, int(height * 0.034)))
font_hw_item = make_font(max(19, int(height * 0.031)))
font_pill = make_font(max(13, int(height * 0.020)))
font_matrix = make_font(16, bold=True, mono=True)

event_queue = queue.Queue()
state_lock = threading.Lock()
running = True

# Session & display state
expression = "NORMAL"
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

WAKE_RE = re.compile(r"\b(hammer|hammers|hamer|hamar|hummer)\b")

JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs!",
    "There are 10 kinds of people: those who understand binary and those who don't.",
    "Why was the robot so calm? It had nerves of steel.",
    "A SQL query walks into a bar, sees two tables and asks: can I join you?",
    "Why did the developer go broke? He used up all his cache!",
    "I told my Raspberry Pi a joke about UDP. I'm not sure it got it.",
]

# ------------------------------------------------------------------------------
# Face model: every expression is a set of parameters that are smoothly blended
# ------------------------------------------------------------------------------
BASE_FACE = dict(
    open=1.0,     # eyelid opening
    lift=0.0,     # eyebrow height
    tilt=0.0,     # +angry (inner corner down)  -sad (outer corner down)
    asym=0.0,     # one raised eyebrow (thinking)
    cheek=0.0,    # lower lid pushed up -> smiling eyes
    pupil=0.38,   # pupil size relative to iris
    iris=1.0,     # iris size
    blush=0.0,
    tear=0.0,
    heart=0.0,    # heart-shaped iris
    wink=0.0,     # right eye winks
    shake=0.0,    # trembling
    bounce=0.0,   # excited hopping
)


def _face(**kw):
    d = dict(BASE_FACE)
    d.update(kw)
    return d


FACES = {
    "NORMAL": _face(),
    "LISTENING": _face(open=1.06, lift=0.55, pupil=0.50, iris=1.04),
    "THINKING": _face(open=0.84, lift=0.15, asym=0.55, tilt=-0.15, pupil=0.34),
    "HAPPY": _face(open=0.95, lift=0.35, cheek=0.70, pupil=0.44, blush=0.55),
    "EXCITED": _face(open=1.10, lift=0.85, cheek=0.35, pupil=0.52, iris=1.06, blush=0.35, bounce=1.0),
    "ALERT": _face(open=0.92, lift=-0.10, tilt=0.85, pupil=0.20, shake=1.0),
    "SURPRISED": _face(open=1.22, lift=1.0, pupil=0.20, iris=0.86),
    "SAD": _face(open=0.80, lift=0.15, tilt=-0.90, pupil=0.52, tear=1.0),
    "LOVE": _face(open=1.0, lift=0.45, cheek=0.25, blush=1.0, heart=1.0),
    "WINK": _face(open=0.95, lift=0.40, cheek=0.55, pupil=0.42, blush=0.35, wink=1.0),
    "SLEEPY": _face(open=0.34, lift=-0.30, tilt=-0.20, pupil=0.44),
}
FACE_TINT = {"ALERT": (255, 60, 70), "SAD": (70, 140, 255), "LOVE": (255, 95, 160)}
FACE_ACCENT = {
    "ALERT": (255, 70, 80), "THINKING": (240, 185, 55), "LOVE": (255, 105, 170),
    "SAD": (80, 150, 255), "HAPPY": (40, 225, 150), "EXCITED": (255, 190, 50),
    "WINK": (190, 120, 255), "SURPRISED": (255, 245, 150), "SLEEPY": (100, 130, 190),
}
DEFAULT_ACCENT = (28, 195, 245)
TRANSIENT = {"HAPPY", "EXCITED", "ALERT", "SURPRISED", "SAD", "LOVE", "WINK"}
HOLD_OVERRIDE = {"WINK": 2.4, "SURPRISED": 3.2, "ALERT": 6.0}
STATUS_LABEL = {
    "NORMAL": "ONLINE", "LISTENING": "LISTENING", "THINKING": "PROCESSING", "HAPPY": "HAPPY",
    "EXCITED": "EXCITED", "ALERT": "ALERT", "SURPRISED": "SURPRISED", "SAD": "CONFUSED",
    "LOVE": "LOVE", "WINK": "PLAYFUL", "SLEEPY": "DROWSY",
}
SPARK_COLS = [(255, 235, 120), (120, 245, 255), (255, 170, 230), (170, 255, 190)]

# Sleep animations (DUCAT_RISE shown first during SLEEP)
SCREENSAVER_MODES = ["DUCAT_RISE", "DREAM", "AURORA", "ORBIT_PULSE", "DINO", "MATRIX", "WARP"]
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
dream_stars = [[random.random(), random.random() * 0.85, random.uniform(0, 6.28)] for _ in range(70)]
dust = [[random.random(), random.random(), random.uniform(0.01, 0.04), random.uniform(0, 6.28)] for _ in range(46)]
particles = []


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


# ==============================================================================
# 2. SMALL HELPERS
# ==============================================================================
def lerp(a: float, b: float, factor: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, factor))


def lerp_rgb(c1, c2, factor: float):
    f = max(0.0, min(1.0, factor))
    smooth_f = f * f * (3.0 - 2.0 * f)
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * smooth_f)) for i in range(3))


def mix_rgb(c1, c2, f: float):
    f = max(0.0, min(1.0, f))
    return tuple(c1[i] + (c2[i] - c1[i]) * f for i in range(3))


def to_rgb(c):
    return tuple(max(0, min(255, int(v))) for v in c)


def shade_rgb(rgb, factor: float):
    return tuple(max(0, min(255, int(round(v * factor)))) for v in rgb[:3])


def get_smooth_eye_color(t: float):
    """Smoothly cycles: Red -> Green -> Blue -> Gold -> Cyan -> Magenta -> Red."""
    palette = [
        (235, 38, 52), (36, 225, 112), (45, 125, 255),
        (250, 195, 45), (32, 225, 235), (215, 65, 235),
    ]
    cycle_pos = ((t / max(1.0, EYE_COLOR_CYCLE_SECONDS)) * len(palette)) % len(palette)
    idx_a = int(cycle_pos) % len(palette)
    idx_b = (idx_a + 1) % len(palette)
    return lerp_rgb(palette[idx_a], palette[idx_b], cycle_pos - int(cycle_pos))


def get_fitting_font(text: str, max_w: int, start_size: int, min_size: int = 13, bold: bool = True):
    sz = start_size
    while sz > min_size:
        f = make_font(sz, bold=bold)
        if f.size(text)[0] <= max_w:
            return f
        sz -= 2
    return make_font(min_size, bold=bold)


def wrap_text(text: str, font: pygame.font.Font, max_width: int):
    words = text.split() if text else []
    if not words:
        return []
    lines, cur = [], words[0]
    for w in words[1:]:
        test = f"{cur} {w}"
        if font.size(test)[0] <= max_width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def draw_centered_wrapped(surface, text, font, color, center_x, top_y, max_width,
                          max_lines=4, line_gap=6, reveal=None) -> int:
    """Draws wrapped, centred text (optionally typed out) and returns the bottom y."""
    lines = wrap_text(text, font, max_width)
    shown = lines[:max_lines]
    if len(lines) > max_lines and shown:
        shown[-1] = shown[-1].rstrip(" .,;:") + "…"
    step = font.get_linesize() + line_gap
    cur_y = top_y
    remaining = None if reveal is None else int(reveal)
    for line in shown:
        part = line if remaining is None else line[:max(0, remaining)]
        if remaining is not None:
            remaining -= len(line) + 1
        if part:
            left = center_x - font.size(line)[0] // 2
            surface.blit(font.render(part, True, color), (left, cur_y))
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
        dev.on() if value else dev.off()
    record_gpio_animation(0, value)


def set_pin_state(pin: int, high: bool, slot_num: int = 0):
    if pin not in gpio_devices:
        return False, f"GPIO {pin} not mapped."
    if high:
        gpio_devices[pin].on()
        record_gpio_animation(slot_num, True)
        return True, f"GPIO {pin} ON (HIGH)."
    gpio_devices[pin].off()
    record_gpio_animation(slot_num, False)
    return True, f"GPIO {pin} OFF (LOW)."


def get_gpio_states_snapshot():
    snap = []
    for slot, pin in sorted(GPIO_SLOT_MAP.items()):
        dev = gpio_devices.get(pin)
        snap.append((slot, pin, bool(dev.is_active) if dev is not None else False))
    return snap


# ==============================================================================
# 3. VOICE COMMAND LOGIC
# ==============================================================================
def normalize_slot_and_action_tokens(text: str) -> str:
    out = []
    for tok in text.lower().split():
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


def is_exit_phrase(low: str, allow_back: bool = False) -> bool:
    if re.search(r"\b(exit|leave|quit)\b", low):
        return True
    return allow_back and bool(re.search(r"\b(back|home)\b", low))


def _compact(low: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", low)).strip()


def is_enter_hardware_phrase(low: str) -> bool:
    compact = _compact(low)
    if not compact or is_exit_phrase(low):
        return False
    if re.search(r"\b(hardware|hard\s*wear|hardwire)\b", compact):
        return True
    return bool(re.search(r"\b(enter|open|start|launch)\s+(hard|hw)\b", compact))


def is_enter_courses_phrase(low: str) -> bool:
    compact = _compact(low)
    if not compact or is_exit_phrase(low):
        return False
    if re.search(r"\b(courses?|cources?|syllabus|programs?)\b", compact) and any(
        w in compact for w in ["enter", "open", "tell", "about", "show", "list", "what", "ducat", "rise", "school"]
    ):
        return True
    return compact in ("courses", "cources", "enter courses", "enter cources",
                       "tell me about courses", "tell me about cources")


def launch_website_autoscroll():
    """Opens ducatrise.com in a browser and sends gentle scroll keys."""
    def _worker():
        proc = None
        try:
            env = os.environ.copy()
            env.setdefault("DISPLAY", ":0")
            proc = subprocess.Popen(["chromium-browser", "--kiosk", DUCAT_RISE_URL],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            time.sleep(2.5)
            end_ts = time.time() + AUTO_SCROLL_SECONDS
            direction_down = True
            while time.time() < end_ts and running:
                key = "Page_Down" if direction_down else "Page_Up"
                subprocess.run(["xdotool", "key", key], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=env)
                time.sleep(1.5)
                if time.time() > end_ts - 4.0:
                    direction_down = False
        except Exception:
            with contextlib.suppress(Exception):
                subprocess.Popen(["xdg-open", DUCAT_RISE_URL])
        finally:
            if proc and proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.terminate()

    threading.Thread(target=_worker, daemon=True).start()


def has_word(q: str, words) -> bool:
    return re.search(r"\b(?:%s)" % "|".join(re.escape(w) for w in words), q) is not None


def answer_from_ducat_knowledge(q_low: str, require_context: bool = True):
    """Answers questions about Ducat RISE courses / Ducat India.

    Outside courses mode a topic word (python, robot...) only counts when the
    sentence also sounds like a course question, so "what is python" still
    reaches Wikipedia.
    """
    ctx = has_word(q_low, ["course", "cource", "ducat", "rise", "school", "learn", "class", "syllabus", "student"])
    if require_context and not ctx:
        return None, None
    if has_word(q_low, ["lego", "robot", "arduino", "sensor", "motor"]):
        return COURSE_TOPICS[0]["summary"], 0
    if has_word(q_low, ["python"]):
        return COURSE_TOPICS[1]["summary"], 1
    if has_word(q_low, ["scratch", "block code", "block based", "block-based"]):
        return COURSE_TOPICS[2]["summary"], 2
    if has_word(q_low, ["3d", "three d", "3 d", "modeling", "modelling"]):
        return COURSE_TOPICS[3]["summary"], 3
    if has_word(q_low, ["financ", "money", "asset", "interest rate", "inflation", "tax", "budget"]):
        return COURSE_TOPICS[4]["summary"], 4
    if has_word(q_low, ["ducat india", "institute", "placement", "full stack", "about ducat", "noida"]):
        return COURSE_TOPICS[5]["summary"], 5
    if has_word(q_low, ["course", "cource", "ducat", "rise", "school program"]):
        return ("Ducat RISE school courses: Robotics/LEGO/Arduino, Python Champion, "
                "Scratch Programming, 3D Designing & Printing, and Financial Literacy."), 0
    return None, None


def try_gpio_command(raw_low: str, in_hardware: bool = False):
    # Outside hardware mode only short phrases are treated as pin commands, so
    # a long question like "who is the first on the moon" is never misread.
    if not in_hardware and len(raw_low.split()) > 6:
        return False, False, ""
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

    slot_str = action_word = None
    m = re.search(r"\b([1-5])\s*(on|off|high|low|enable|disable)\b", collapsed)
    if m:
        slot_str, action_word = m.group(1), m.group(2)
    else:
        m = re.search(r"\b(on|off|high|low|enable|disable)\s*([1-5])\b", collapsed)
        if m:
            action_word, slot_str = m.group(1), m.group(2)

    if slot_str and action_word:
        slot_num = int(slot_str)
        ok, msg = set_pin_state(GPIO_SLOT_MAP.get(slot_num), action_word in ("on", "high", "enable"), slot_num)
        return True, ok, f"[{slot_num}] {msg}"

    pin_match = re.search(r"\b(?:gpio|pin)\s*(\d+)\s*(on|off|high|low|enable|disable)?\b", collapsed)
    if pin_match:
        pin_num = int(pin_match.group(1))
        act = pin_match.group(2) or ""
        rev_slot = next((k for k, v in GPIO_SLOT_MAP.items() if v == pin_num), 0)
        if act in ("on", "high", "enable") or any(w in collapsed for w in ["high", "on", "enable"]):
            ok, msg = set_pin_state(pin_num, True, rev_slot)
            return True, ok, msg
        if act in ("off", "low", "disable") or any(w in collapsed for w in ["low", "off", "disable"]):
            ok, msg = set_pin_state(pin_num, False, rev_slot)
            return True, ok, msg
    return False, False, ""


# ---- safe calculator (no eval) -------------------------------------------------
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}
NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 12 or abs(left) > 1e6):
            raise ValueError("too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("unsupported")


def try_local_math(query: str):
    c = query.lower().strip()
    c = re.sub(r"^(what is|what's|whats|calculate|how much is|compute)\s+", "", c)
    for word, val in NUMBER_WORDS.items():
        c = re.sub(rf"\b{word}\b", str(val), c)
    for a, b in (("to the power of", "**"), ("multiplied by", "*"), ("divided by", "/"),
                 ("plus", "+"), ("minus", "-"), ("times", "*"), ("over", "/"), ("mod", "%")):
        c = c.replace(a, f" {b} ")
    c = re.sub(r"(?<=\d)\s*x\s*(?=\d)", " * ", c)
    c = re.sub(r"\s+", " ", c).strip(" ?.=")
    if not (re.fullmatch(r"[0-9+\-*/%().\s]+", c) and re.search(r"\d", c) and re.search(r"[+\-*/%]", c)):
        return None
    try:
        val = _safe_eval(ast.parse(c, mode="eval"))
    except Exception:
        return None
    if isinstance(val, float):
        val = int(val) if val.is_integer() else round(val, 4)
    return f"{c} = {val}"


# ---- online lookups --------------------------------------------------------------
def trim_sentences(text: str, max_sentences: int = 3, max_chars: int = 330) -> str:
    clean = " ".join(text.split()).strip()
    if not clean:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", clean)
    out = ""
    for part in parts[:max_sentences]:
        if out and len(out) + len(part) > max_chars:
            break
        out = f"{out} {part}".strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0] + "…"
    return out


def clean_topic(q: str) -> str:
    out = re.sub(r"^(who is|who was|what is|what are|what was|tell me about|explain|define|where is)\s+",
                 "", q.strip(), flags=re.IGNORECASE).strip(" ?.")
    return out or q.strip()


def query_wikipedia(query: str) -> str:
    headers = {"User-Agent": "HammerDucatHUD/12.0 (RaspberryPi)"}
    topic = clean_topic(query)
    search_url = ("https://en.wikipedia.org/w/api.php?"
                  f"action=opensearch&search={urllib.parse.quote(topic)}&limit=1&namespace=0&format=json")
    r = requests.get(search_url, headers=headers, timeout=8)
    r.raise_for_status()
    data = r.json()
    titles = data[1] if len(data) > 1 else []
    if not titles:
        return ""
    summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(titles[0])}"
    resp = requests.get(summary_url, headers=headers, timeout=8)
    resp.raise_for_status()
    sdata = resp.json()
    extract = (sdata.get("extract") or "").strip()
    if extract and sdata.get("type") != "disambiguation":
        return trim_sentences(extract)
    return ""


def query_duckduckgo(query: str) -> str:
    url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
    resp = requests.get(url, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    for field in ["Answer", "AbstractText", "Definition"]:
        if data.get(field):
            return trim_sentences(str(data[field]))
    for item in data.get("RelatedTopics", []):
        if isinstance(item, dict) and item.get("Text"):
            return trim_sentences(str(item["Text"]), max_sentences=2)
    return ""


def query_open_meteo_weather(city: str) -> str:
    gr = requests.get(f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1",
                      timeout=8)
    gr.raise_for_status()
    results = gr.json().get("results") or []
    if not results:
        return ""
    place = results[0]
    w_url = ("https://api.open-meteo.com/v1/forecast?"
             f"latitude={place['latitude']}&longitude={place['longitude']}"
             "&current=temperature_2m,wind_speed_10m,relative_humidity_2m")
    wr = requests.get(w_url, timeout=8)
    wr.raise_for_status()
    cur = wr.json().get("current") or {}
    if cur.get("temperature_2m") is None:
        return ""
    return (f"{place.get('name', city)}: {cur['temperature_2m']}°C, wind {cur.get('wind_speed_10m')} km/h, "
            f"humidity {cur.get('relative_humidity_2m')}%.")


def small_talk(q: str):
    if re.match(r"^(hello|hi|hey|namaste|good (morning|afternoon|evening))\b", q):
        return "Hello! I'm Hammer. Ask me anything!", "EXCITED"
    if re.search(r"\bhow are you\b|\bhow do you feel\b", q):
        return "Running at full voltage and feeling great!", "HAPPY"
    if re.search(r"\bwho are you\b|\byour name\b|\bintroduce yourself\b", q):
        return ("I'm Hammer, a voice assistant on a Raspberry Pi. I answer questions, "
                "control GPIO pins and explain Ducat RISE courses."), "HAPPY"
    if re.search(r"\bwhat can you do\b|\bhelp me\b|\bwhat do you do\b", q):
        return ("Ask me anything, say 'enter courses' for Ducat RISE, "
                "or 'enter hardware' to switch pins on and off."), "EXCITED"
    if re.search(r"\blove you\b|\byou are (?:awesome|amazing|cute|the best)\b", q):
        return "Aww, my circuits are blushing!", "LOVE"
    if re.search(r"\bthank(?:s| you)\b", q):
        return "You're welcome!", "LOVE"
    if re.search(r"\bjoke\b|\bmake me laugh\b", q):
        return random.choice(JOKES), "WINK"
    if re.search(r"\bi(?:'m| am) (?:sad|bored|lonely|tired)\b", q):
        return "Don't worry, I'm right here. Want to hear a joke?", "SAD"
    if re.search(r"\b(?:shut up|be quiet|stop|cancel|never ?mind)\b", q) and len(q.split()) <= 4:
        return "Okay!", "NORMAL"
    return None


def answer_question(question: str):
    q_low = question.lower().strip()
    now = datetime.datetime.now()

    st = small_talk(q_low)
    if st:
        return st

    ducat_ans, _ = answer_from_ducat_knowledge(q_low)
    if ducat_ans:
        return ducat_ans, "HAPPY"

    if re.search(r"\b(weather|forecast)\b|\btemperature (?:in|at)\b", q_low):
        city = DEFAULT_CITY
        m = re.search(r"\b(?:in|at|for)\s+([a-z][a-z ]*)", q_low)
        if m:
            cand = re.sub(r"\b(today|tonight|tomorrow|now|right now|please|currently|outside)\b", "", m.group(1))
            cand = cand.strip(" ?.")
            if cand:
                city = cand
        try:
            w_ans = query_open_meteo_weather(city)
            return (w_ans, "HAPPY") if w_ans else (f"I couldn't find weather for {city}.", "SAD")
        except Exception:
            return "I couldn't reach the weather service. Is the internet on?", "ALERT"

    if re.search(r"\b(what(?:'s| is)? (?:the )?(?:date|day)|today'?s date|what day|which day|current date)\b", q_low):
        return now.strftime("%A, %B %d, %Y"), "HAPPY"
    if re.search(r"\bwhat(?:'s| is)? (?:the )?(?:current )?time\b|\btime (?:is it|now)\b|\bcurrent time\b|"
                 r"\btell (?:me )?the time\b", q_low):
        return now.strftime("%I:%M:%S %p"), "HAPPY"

    math_ans = try_local_math(question)
    if math_ans:
        return math_ans, "EXCITED"

    net_errors = 0
    try:
        wiki_ans = query_wikipedia(question)
        if wiki_ans:
            return wiki_ans, "HAPPY"
    except Exception:
        net_errors += 1
    try:
        ddg_ans = query_duckduckgo(question)
        if ddg_ans:
            return ddg_ans, "HAPPY"
    except Exception:
        net_errors += 1

    if net_errors >= 2:
        return "I can't reach the internet right now.", "ALERT"
    return f"No result for '{question}'.", "SAD"


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
            sleep_mode, hardware_mode, courses_mode, course_index = False, False, True, 0
        touch_activity()
        top = COURSE_TOPICS[0]
        event_queue.put(("state", ("HAPPY", q, f"{top['title']}: {top['summary']}", False)))
        return

    if is_enter_hardware_phrase(low):
        with state_lock:
            sleep_mode, hardware_mode, courses_mode = False, True, False
        touch_activity()
        event_queue.put(("state", ("EXCITED", q, "Hardware mode: say 'first on', 'forth off', or 'all off'.", False)))
        return

    if "deep sleep" in low or "go to sleep" in low or low == "sleep":
        with state_lock:
            sleep_mode, hardware_mode, courses_mode = True, False, False
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
        in_courses, in_hw = courses_mode, hardware_mode
        cur_idx = course_index
        is_capturing = capturing_session

    if is_enter_courses_phrase(low):
        touch_activity()
        with state_lock:
            sleep_mode, hardware_mode, courses_mode, course_index = False, False, True, 0
            capturing_session, captured_chunks = False, []
        top = COURSE_TOPICS[0]
        event_queue.put(("state", ("HAPPY", clean, f"{top['title']}: {top['summary']}", False)))
        return

    if is_enter_hardware_phrase(low):
        touch_activity()
        with state_lock:
            sleep_mode, hardware_mode, courses_mode = False, True, False
            capturing_session, captured_chunks = False, []
        event_queue.put(("state", ("EXCITED", clean, "Say 'first on' / 'forth off' / 'all on' / 'all off'.", False)))
        return

    # "exit" is only a command inside a mode, so questions like "what is back pain" are safe.
    if (in_hw or in_courses) and is_exit_phrase(low, allow_back=in_hw):
        touch_activity()
        with state_lock:
            hardware_mode, courses_mode = False, False
            capturing_session, captured_chunks = False, []
        event_queue.put(("state", ("NORMAL", clean, "", False)))
        return

    if in_courses:
        if any(w in low for w in ["website", "site", "scroll", "ducatrise"]):
            touch_activity()
            launch_website_autoscroll()
            event_queue.put(("state", ("EXCITED", clean, "Opening ducatrise.com & auto-scrolling...", False)))
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
        topic_ans, matched_idx = answer_from_ducat_knowledge(low, require_context=False)
        if topic_ans and matched_idx is not None:
            touch_activity()
            with state_lock:
                course_index = matched_idx
            top = COURSE_TOPICS[matched_idx]
            event_queue.put(("state", ("HAPPY", clean, f"{top['title']}: {topic_ans}", False)))
            return

    if not is_capturing:
        matched_gpio, ok_gpio, gpio_msg = try_gpio_command(low, in_hardware=in_hw)
        if matched_gpio:
            touch_activity()
            with state_lock:
                is_slp = sleep_mode
            next_exp = "SLEEP" if is_slp else ("HAPPY" if ok_gpio else "ALERT")
            event_queue.put(("state", (next_exp, clean, gpio_msg, is_slp)))
            return

        wake = WAKE_RE.search(low)
        if wake:
            touch_activity()
            after = low[wake.end():].strip(" ,.!?")
            with state_lock:
                sleep_mode = False
                capturing_session = True
                captured_chunks = [after] if after else []
                last_speech_time = now_ts
            event_queue.put(("state", ("LISTENING", after or "Listening...", "", False)))
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
                capturing_session, captured_chunks = False, []
            event_queue.put(("state", ("THINKING", full_q, "", False)))
            threading.Thread(target=execute_search_worker, args=(full_q,), daemon=True).start()
            continue

        if not is_cap and not already_sleeping and idle_elapsed >= IDLE_SLEEP_SECONDS:
            with state_lock:
                sleep_mode = True
                capturing_session, captured_chunks = False, []
            event_queue.put(("state", ("SLEEP", "", "", True)))


def voice_loop():
    global voice_energy
    if sr is None:
        event_queue.put(("state", ("ALERT", "", "Install SpeechRecognition: pip install SpeechRecognition", False)))
        return
    rec = sr.Recognizer()
    rec.dynamic_energy_threshold = True
    rec.pause_threshold = 1.2
    rec.non_speaking_duration = 0.7

    try:
        with suppress_alsa_stderr():
            mic = sr.Microphone(device_index=MIC_DEVICE_INDEX) if MIC_DEVICE_INDEX is not None else sr.Microphone()
        with mic as source:
            with suppress_alsa_stderr():
                rec.adjust_for_ambient_noise(source, duration=1.0)
            while running:
                try:
                    with suppress_alsa_stderr():
                        audio = rec.listen(source, timeout=5, phrase_time_limit=15)
                    raw_bytes = audio.get_raw_data()
                    if raw_bytes:
                        step = max(2, len(raw_bytes) // 400)
                        peak = 0
                        for i in range(0, len(raw_bytes) - 1, step):
                            peak = max(peak, abs(int.from_bytes(raw_bytes[i:i + 2], "little", signed=True)))
                        with state_lock:
                            voice_energy = min(1.0, peak / 14000.0)
                    phrase = rec.recognize_google(audio, language=SPEECH_LANGUAGE).strip()
                    if phrase:
                        handle_phrase(phrase)
                except (sr.WaitTimeoutError, sr.UnknownValueError):
                    with state_lock:
                        voice_energy *= 0.6
                except sr.RequestError as e:
                    event_queue.put(("state", ("ALERT", "", f"Speech error: {e}", False)))
                    time.sleep(2.0)
    except Exception as e:
        event_queue.put(("state", ("ALERT", "", f"Mic error: {e}", False)))


# ==============================================================================
# 4. DRAWING
# ==============================================================================
overlay = pygame.Surface((width, height), pygame.SRCALPHA)


def get_overlay():
    overlay.fill((0, 0, 0, 0))
    return overlay


def build_background():
    surf = pygame.Surface((width, height))
    top, bot = (9, 21, 44), (3, 8, 20)
    for y in range(height):
        pygame.draw.line(surf, to_rgb(mix_rgb(top, bot, y / max(1, height - 1))), (0, y), (width, y))
    step = max(28, int(S * 0.06))
    for gx in range(step // 2, width, step):
        for gy in range(step // 2, height, step):
            surf.set_at((gx, gy), (22, 48, 76))
    return surf


bg_surface = build_background()
fade_surface = pygame.Surface((width, height))
fade_surface.fill((0, 0, 0))


def draw_segmented_ring(surface, cx, cy, r, color, segs, gap, rot, w):
    rect = pygame.Rect(cx - r, cy - r, r * 2, r * 2)
    span = 2 * math.pi / segs
    for i in range(segs):
        a0 = rot + i * span
        pygame.draw.arc(surface, color, rect, a0, a0 + span * (1.0 - gap), w)


def draw_heart(surface, cx, cy, size, col):
    if size < 4:
        return
    r = size / 4.0
    pygame.draw.circle(surface, col, (int(cx - r), int(cy - r * 0.55)), max(2, int(r * 1.02)))
    pygame.draw.circle(surface, col, (int(cx + r), int(cy - r * 0.55)), max(2, int(r * 1.02)))
    pygame.draw.polygon(surface, col, [(int(cx - 2 * r + r * 0.08), int(cy - r * 0.2)),
                                       (int(cx + 2 * r - r * 0.08), int(cy - r * 0.2)),
                                       (int(cx), int(cy + 2.0 * r))])


def draw_star(surface, x, y, s, col):
    k = s * 0.28
    pygame.draw.polygon(surface, col, [(x, y - s), (x + k, y - k), (x + s, y), (x + k, y + k),
                                       (x, y + s), (x - k, y + k), (x - s, y), (x - k, y - k)])


def draw_frame_decor(surface, t, col):
    pulse = 0.65 + 0.35 * math.sin(t * 3.0)
    pygame.draw.rect(surface, shade_rgb(col, 0.45), (14, 14, width - 28, height - 28), 2, border_radius=14)
    L = int(S * 0.07)
    c = shade_rgb(col, pulse + 0.3)
    for x, y, sx, sy in ((14, 14, 1, 1), (width - 14, 14, -1, 1), (14, height - 14, 1, -1),
                         (width - 14, height - 14, -1, -1)):
        pygame.draw.line(surface, c, (x, y), (x + sx * L, y), 4)
        pygame.draw.line(surface, c, (x, y), (x, y + sy * L), 4)
    sy = int((t * 110) % (height + 40)) - 20
    pygame.draw.line(surface, (14, 40, 66), (16, sy), (width - 16, sy), 2)


# ---- particles -----------------------------------------------------------------
def spawn_particles(expr, dt):
    if len(particles) > 70:
        return
    rate = {"HAPPY": 5.0, "EXCITED": 13.0, "WINK": 8.0}.get(expr, 0.0)
    if rate and random.random() < rate * dt:
        particles.append([random.uniform(0.08, 0.92) * width, random.uniform(0.08, 0.40) * height,
                          random.uniform(-12, 12), random.uniform(-18, 6), 0.0, random.uniform(0.7, 1.3),
                          "spark", random.uniform(6, 13) * (S / 600.0), random.choice(SPARK_COLS)])
    if expr == "LOVE" and random.random() < 6.0 * dt:
        particles.append([random.uniform(0.2, 0.8) * width, random.uniform(0.34, 0.42) * height,
                          random.uniform(-14, 14), random.uniform(-70, -45), 0.0, random.uniform(1.4, 2.2),
                          "heart", random.uniform(14, 26) * (S / 600.0), (255, 95, 160)])
    if expr == "SLEEPY" and random.random() < 0.6 * dt:
        particles.append([width * 0.66, height * 0.22, 18.0, -22.0, 0.0, 2.6, "z", 1.0, (150, 190, 230)])


def update_particles(dt):
    for p in particles:
        p[0] += p[2] * dt
        p[1] += p[3] * dt
        p[4] += dt
    particles[:] = [p for p in particles if p[4] < p[5]]


def draw_particles(surface):
    for x, y, _vx, _vy, age, life, kind, size, col in particles:
        f = math.sin(math.pi * min(1.0, age / life))
        c = shade_rgb(col, 0.25 + 0.75 * f)
        if kind == "spark":
            draw_star(surface, int(x), int(y), int(size * (0.5 + 0.5 * f)), c)
        elif kind == "heart":
            draw_heart(surface, int(x), int(y), int(size * (0.7 + 0.3 * f)), c)
        else:
            z = make_font(16 + int(18 * age / life)).render("Z", True, c)
            surface.blit(z, (int(x), int(y)))


# ---- HUD backdrop & status -------------------------------------------------------
def draw_hud_backdrop(surface, t, accent, energy):
    surface.blit(bg_surface, (0, 0))
    cx, cy = width // 2, int(height * 0.30)
    base = int(S * 0.18)
    dim = shade_rgb(accent, 0.55)

    for idx, (mult, segs, speed) in enumerate(((0.62, 12, 0.5), (0.88, 16, -0.35), (1.14, 22, 0.22))):
        r = int(base * mult * (1.0 + 0.03 * energy))
        draw_segmented_ring(surface, cx, cy, r, accent if idx == 1 else dim, segs, 0.38, t * speed,
                            3 if idx == 1 else 2)

    arc_r_outer = int(S * 0.24)
    arc_r_inner = int(S * 0.14)
    for side in (-1, 1):
        ox = cx + side * int(width * 0.19)
        rect_o = pygame.Rect(ox - arc_r_outer, cy - arc_r_outer, arc_r_outer * 2, arc_r_outer * 2)
        a1 = math.radians(135 if side < 0 else -45)
        a2 = math.radians(225 if side < 0 else 45)
        pygame.draw.arc(surface, accent, rect_o, a1, a2, 3)
        rect_i = pygame.Rect(cx - arc_r_inner, cy - arc_r_inner, arc_r_inner * 2, arc_r_inner * 2)
        pygame.draw.arc(surface, accent, rect_i, math.radians(150 if side < 0 else -30),
                        math.radians(210 if side < 0 else 30), 2)

    if energy > 0.12:
        ph = (t * 0.9) % 1.0
        pygame.draw.circle(surface, shade_rgb(accent, (1.0 - ph) * min(1.0, energy * 1.3)),
                           (cx, cy), int(base + ph * base * 1.8), 2)

    sy = int((t * 90) % (height + 40)) - 20
    pygame.draw.line(surface, (14, 44, 70), (0, sy), (width, sy), 2)

    for d in dust:
        d[1] -= d[2] * 0.016
        if d[1] < -0.02:
            d[1], d[0] = 1.02, random.random()
        tw = 0.4 + 0.6 * (0.5 + 0.5 * math.sin(t * 1.7 + d[3]))
        pygame.draw.circle(surface, shade_rgb(accent, 0.8 * tw), (int(d[0] * width), int(d[1] * height)),
                           1 + int(tw > 0.8))


def draw_status_bar(surface, t, label, energy, accent):
    r = 5 + int(4 * min(1.0, energy)) + int(1.5 * math.sin(t * 5.0))
    pygame.draw.circle(surface, accent, (30, 26), max(3, r))
    surface.blit(font_pill.render(label, True, shade_rgb(accent, 1.15)), (46, 26 - font_pill.get_height() // 2))
    clk = font_pill.render(datetime.datetime.now().strftime("%I:%M %p"), True, (150, 200, 225))
    surface.blit(clk, (width - clk.get_width() - 28, 26 - clk.get_height() // 2))


# ---- the face ---------------------------------------------------------------------
_eye_cache = {}


def get_eye_surfaces(w, h):
    s = _eye_cache.get((w, h))
    if s is None:
        s = (pygame.Surface((w, h), pygame.SRCALPHA), pygame.Surface((w, h), pygame.SRCALPHA))
        _eye_cache[(w, h)] = s
    return s


def draw_brow(surface, cx, cy, ew, eh, side, p, col, t):
    lift_eff = p["lift"] + p["asym"] * (1.0 if side < 0 else -0.35)
    base_y = cy - eh * 0.5 - int(S * 0.03) - lift_eff * eh * 0.20
    pts = []
    for i in range(13):
        u = (i / 12.0) * 2.0 - 1.0
        inner = -side * u
        x = cx + u * ew * 0.40
        y = base_y - (1.0 - u * u) * eh * 0.06 + p["tilt"] * inner * eh * 0.20
        pts.append((int(x), int(y)))
    pygame.draw.lines(surface, shade_rgb(col, 0.45), False, pts, 11)
    pygame.draw.lines(surface, shade_rgb(col, 1.1), False, pts, 5)
    for pt in (pts[0], pts[-1]):
        pygame.draw.circle(surface, shade_rgb(col, 1.1), pt, 3)


def draw_blush(surface, cx, cy, ew, eh, amount):
    bw, bh = int(ew * 0.46), int(eh * 0.20)
    s = pygame.Surface((bw, bh), pygame.SRCALPHA)
    pygame.draw.ellipse(s, (255, 95, 150, int(110 * min(1.0, amount))), s.get_rect())
    surface.blit(s, (cx - bw // 2, cy + int(eh * 0.52)))


def draw_face_eye(surface, cx, cy, ew, eh, t, side, p, gaze, iris_col, blink, energy):
    wink = p["wink"] if side > 0 else 0.0
    open_f = p["open"] * blink * (1.0 - 0.93 * wink)
    cheek = max(p["cheek"], 0.85 * wink)
    cur_h = max(6, int(eh * open_f))

    w, h = ew + 40, int(eh * 1.5) + 40
    sc, mk = get_eye_surfaces(w, h)
    lcx, lcy = w // 2, h // 2
    half_w, half_h = ew / 2.0, cur_h / 2.0
    steps = 26
    top, bot = [], []
    tilt = p["tilt"]
    for i in range(steps + 1):
        u = (i / steps) * 2.0 - 1.0
        x = lcx + u * half_w
        arch = max(0.0, math.cos(u * math.pi * 0.5)) ** 0.75
        inner = -side * u
        yt = lcy - half_h * arch
        yb = lcy + half_h * arch
        sh = tilt * (0.5 + 0.5 * inner) if tilt >= 0 else -tilt * (0.5 - 0.5 * inner)
        yt += sh * half_h * 0.95 * arch
        yb -= cheek * half_h * 0.80 * arch
        if yb < yt + 2:
            mid = (yt + yb) / 2.0
            yt, yb = mid - 1, mid + 1
        lift = wink * arch * eh * 0.14
        top.append((int(x), int(yt - lift)))
        bot.append((int(x), int(yb - lift)))

    sc.fill((0, 0, 0, 0))
    poly = top + bot[::-1]
    pygame.draw.polygon(sc, (188, 238, 243, 255), poly)

    if cur_h >= 16:
        base_r = min(ew * 0.25, eh * 0.42) * p["iris"] * min(1.0, (cur_h / float(eh)) * 1.08)
        iris_r = max(8, int(base_r))
        ix = lcx + int(gaze[0] * ew * 0.17)
        iy = lcy + int(gaze[1] * cur_h * 0.18)
        ir = int(iris_r * (1.0 - p["heart"]))
        if ir >= 5:
            dark = shade_rgb(iris_col, 0.5)
            pygame.draw.circle(sc, dark, (ix, iy), ir)
            pygame.draw.circle(sc, iris_col, (ix, iy), max(3, ir - 3))
            pygame.draw.circle(sc, shade_rgb(iris_col, 1.2), (ix, iy), max(2, int(ir * 0.66)))
            fib = shade_rgb(iris_col, 0.75)
            for k in range(12):
                a = t * 0.25 + k * math.pi / 6.0
                ca, sa = math.cos(a), math.sin(a)
                pygame.draw.line(sc, fib, (ix + ca * ir * 0.45, iy + sa * ir * 0.45),
                                 (ix + ca * ir * 0.92, iy + sa * ir * 0.92), 1)
            pr = max(3, int(ir * (p["pupil"] + 0.10 * energy)))
            pygame.draw.circle(sc, (24, 18, 26), (ix, iy), pr)
            pygame.draw.circle(sc, (255, 255, 255), (ix - int(ir * 0.34), iy - int(ir * 0.26)),
                               max(3, int(ir * 0.19)))
            pygame.draw.circle(sc, (255, 255, 255), (ix + int(ir * 0.28), iy + int(ir * 0.28)),
                               max(2, int(ir * 0.09)))
        if p["heart"] > 0.05:
            hs = int(iris_r * 2.3 * p["heart"] * (1.0 + 0.10 * math.sin(t * 8.0)))
            draw_heart(sc, ix, iy, hs, iris_col)
            pygame.draw.circle(sc, (255, 225, 240), (ix - int(hs * 0.22), iy - int(hs * 0.12)),
                               max(2, int(hs * 0.07)))

    pygame.draw.lines(sc, (112, 172, 192), False, top, max(3, int(cur_h * 0.10)))

    mk.fill((0, 0, 0, 0))
    pygame.draw.polygon(mk, (255, 255, 255, 255), poly)
    sc.blit(mk, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

    gc = to_rgb(iris_col)
    for pts in (top, bot):
        pygame.draw.lines(sc, (*gc, 45), False, pts, 11)
        pygame.draw.lines(sc, (*gc, 100), False, pts, 6)
    line_col = to_rgb(mix_rgb((160, 225, 235), iris_col, 0.35))
    pygame.draw.lines(sc, line_col, False, top, 3)
    pygame.draw.lines(sc, line_col, False, bot, 3)

    surface.blit(sc, (cx - w // 2, cy - h // 2))

    # white curved accent beside each eye
    arc_side = 1 if side < 0 else -1
    arc_x = cx + arc_side * int(ew * 0.56)
    arc_rect = pygame.Rect(arc_x - int(ew * 0.28), cy - int(eh * 0.46), int(ew * 0.56), int(eh * 0.92))
    a_start = math.radians(-55 if arc_side > 0 else 125)
    a_end = math.radians(55 if arc_side > 0 else 235)
    bright = int(200 + 40 * math.sin(t * 2.0 + side))
    pygame.draw.arc(surface, (bright, bright, 248), arc_rect, a_start, a_end, 4)

    if p["tear"] > 0.3:
        ph = (t * 0.45 + (0.0 if side < 0 else 0.4)) % 1.0
        tx = cx + side * int(ew * 0.26)
        ty = cy + int(cur_h * 0.45) + int(ph * eh * 0.9)
        sz = 4 + int(3 * (1.0 - ph))
        tc = shade_rgb((150, 205, 255), (1.0 - 0.7 * ph) * min(1.0, p["tear"]))
        pygame.draw.circle(surface, tc, (tx, ty), sz)
        pygame.draw.polygon(surface, tc, [(tx, ty - int(sz * 2.4)), (tx - int(sz * 0.85), ty - int(sz * 0.3)),
                                          (tx + int(sz * 0.85), ty - int(sz * 0.3))])


def draw_face(surface, t, p, gaze, iris_col, blink, energy):
    base_cy = int(height * 0.30)
    eye_w, eye_h = int(width * 0.27), int(height * 0.19)
    eye_dx = int(width * 0.20)
    dy = int(math.sin(t * 1.4) * 2.5) - int(abs(math.sin(t * 7.0)) * 9 * p["bounce"])
    dx = int(math.sin(t * 55.0) * 3 * p["shake"])
    cy = base_cy + dy
    for side in (-1, 1):
        cx = width // 2 + side * eye_dx + dx
        draw_brow(surface, cx, cy, eye_w, eye_h, side, p, iris_col, t)
        if p["blush"] > 0.04:
            draw_blush(surface, cx, cy, eye_w, eye_h, p["blush"])
        draw_face_eye(surface, cx, cy, eye_w, eye_h, t, side, p, gaze, iris_col, blink, energy)


def draw_listening_waveform(surface, t, energy, accent):
    bar_count = 46
    total_w = int(width * 0.56)
    start_x = (width - total_w) // 2
    center_y = int(height * 0.55)
    max_bar_h = int(height * 0.085)
    step = total_w / max(1, bar_count - 1)
    bar_w = max(3, int(step * 0.52))
    amp_boost = 0.35 + 0.65 * max(energy, 0.25 + 0.2 * math.sin(t * 4.0))
    for i in range(bar_count):
        norm_pos = (i / max(1, bar_count - 1)) * 2.0 - 1.0
        envelope = max(0.12, math.cos(norm_pos * (math.pi * 0.46)))
        wave1 = 0.55 + 0.45 * math.sin(t * 11.0 + i * 0.42)
        wave2 = 0.5 + 0.5 * math.cos(t * 7.2 - i * 0.27)
        h_val = max(6, int(max_bar_h * envelope * (0.35 + 0.65 * wave1 * wave2) * amp_boost))
        col = to_rgb(mix_rgb(accent, (255, 225, 110), (i / max(1, bar_count - 1)) * 0.6))
        x = int(start_x + i * step) - bar_w // 2
        pygame.draw.rect(surface, col, (x, center_y - h_val // 2, bar_w, h_val), border_radius=bar_w // 2)


def draw_gpio_action_burst(surface, t, act_age, act_high, act_slot):
    if act_age > 1.8:
        return
    progress = act_age / 1.8
    alpha = max(0, int(210 * (1.0 - progress)))
    cx, cy = width // 2, int(height * 0.45)
    ring_col = (40, 255, 140) if act_high else (255, 95, 130)
    burst = get_overlay()
    for ring_i in range(3):
        r = int(45 + progress * (260 + ring_i * 65))
        pygame.draw.circle(burst, (*ring_col, max(10, alpha // (ring_i + 1))), (cx, cy), r, 3)
    for p_idx in range(18):
        ang = (2 * math.pi * p_idx / 18) + (t * (1.8 if act_high else -1.8))
        dist = 35 + progress * 240
        pc = (255, 200, 60) if p_idx % 2 == 0 else (0, 230, 255)
        pygame.draw.circle(burst, (*pc, alpha), (int(cx + math.cos(ang) * dist), int(cy + math.sin(ang) * dist)), 4)
    surface.blit(burst, (0, 0))


# ---- course icons -----------------------------------------------------------------
def draw_topic_icon(surface, key, cx, cy, r, t, col):
    if key == "robotics":
        for i in range(8):
            a = t * 0.9 + i * math.pi / 4
            pygame.draw.line(surface, col, (cx + math.cos(a) * r * 0.78, cy + math.sin(a) * r * 0.78),
                             (cx + math.cos(a) * r * 1.12, cy + math.sin(a) * r * 1.12), max(4, r // 6))
        pygame.draw.circle(surface, col, (cx, cy), int(r * 0.82), 4)
        pygame.draw.circle(surface, col, (cx, cy), int(r * 0.32), 3)
    elif key == "python":
        for off, c in ((-1, (70, 140, 220)), (1, (255, 210, 70))):
            pts = [(cx + int(x), cy + int(off * r * 0.28 + math.sin(x / r * 3.0 + t * 3.0 + off) * r * 0.3))
                   for x in range(-r, r + 1, max(3, r // 14))]
            pygame.draw.lines(surface, c, False, pts, max(6, r // 4))
    elif key == "scratch":
        cols = [(255, 160, 40), (80, 160, 255), (120, 230, 130)]
        for i in range(3):
            bw, bh = int(r * 1.7), int(r * 0.5)
            by = cy + int((i - 1) * r * 0.62 + math.sin(t * 2.2 + i) * 4)
            rect = pygame.Rect(cx - bw // 2, by - bh // 2, bw, bh)
            pygame.draw.rect(surface, cols[i], rect, border_radius=6)
            pygame.draw.rect(surface, cols[i], (rect.x + 10, rect.y - 5, r // 3, 7), border_radius=3)
    elif key == "3d":
        verts = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
        ay, ax = t * 0.9, t * 0.6
        proj = []
        for x, y, z in verts:
            x1, z1 = x * math.cos(ay) - z * math.sin(ay), x * math.sin(ay) + z * math.cos(ay)
            y1, z2 = y * math.cos(ax) - z1 * math.sin(ax), y * math.sin(ax) + z1 * math.cos(ax)
            k = 1.0 / (3.2 - z2 * 0.6)
            proj.append((cx + int(x1 * r * 1.7 * k * 1.25), cy + int(y1 * r * 1.7 * k * 1.25)))
        for i in range(8):
            for j in range(i + 1, 8):
                if bin(i ^ j).count("1") == 1:
                    pygame.draw.line(surface, col, proj[i], proj[j], 3)
    elif key == "finance":
        for i in range(4):
            by = cy + int(r * 0.55 - i * r * 0.34 + math.sin(t * 2.5 + i * 0.7) * 3)
            rect = pygame.Rect(cx - int(r * 0.8), by - int(r * 0.2), int(r * 1.6), int(r * 0.4))
            pygame.draw.ellipse(surface, (255, 200, 60), rect)
            pygame.draw.ellipse(surface, (200, 140, 20), rect, 3)
    else:
        for i in range(3):
            ph = ((t * 0.7) + i / 3.0) % 1.0
            y = cy + int(r * 0.7) - int(ph * r * 1.6)
            c = shade_rgb(col, math.sin(math.pi * ph) + 0.1)
            pygame.draw.lines(surface, c, False, [(cx - int(r * 0.7), y + int(r * 0.35)), (cx, y),
                                                  (cx + int(r * 0.7), y + int(r * 0.35))], max(5, r // 5))


# ---- mode screens -----------------------------------------------------------------
def draw_courses_mode_screen(surface, t, idx, stt_str, rep_str, card_age):
    surface.blit(bg_surface, (0, 0))
    accent = (0, 220, 255)
    draw_frame_decor(surface, t, accent)
    pad_x = max(24, int(width * 0.04))
    inner_w = width - pad_x * 2

    title = "DUCAT RISE & DUCAT INDIA COURSES"
    title_font = get_fitting_font(title, inner_w, font_mode_title.get_height(), 20, True)
    title_surf = title_font.render(title, True, (65, 245, 255))
    surface.blit(title_surf, title_surf.get_rect(center=(width // 2, 46)))

    y_after_sub = draw_centered_wrapped(
        surface, "Say 'next', 'previous', 'python', '3d', 'finance', 'ducat india', 'website', or 'exit'",
        font_stt, (255, 210, 95), width // 2, 74, inner_w, max_lines=2, line_gap=4)

    pill_y = y_after_sub + 10
    pill_h = max(30, int(height * 0.046))
    pill_w = inner_w // len(COURSE_TOPICS)
    for i, item in enumerate(COURSE_TOPICS):
        active = (i == idx)
        p_rect = pygame.Rect(pad_x + i * pill_w + 3, pill_y, max(20, pill_w - 6), pill_h)
        glow = 0.75 + 0.25 * math.sin(t * 5.0) if active else 1.0
        pygame.draw.rect(surface, (18, 65, 92) if active else (10, 18, 34), p_rect, border_radius=8)
        pygame.draw.rect(surface, shade_rgb((60, 250, 160), glow) if active else (45, 70, 100), p_rect, 2,
                         border_radius=8)
        lbl_font = get_fitting_font(item["key"].upper(), p_rect.width - 8, font_pill.get_height(), 11, True)
        lbl = lbl_font.render(item["key"].upper(), True, (255, 235, 120) if active else (165, 190, 215))
        surface.blit(lbl, lbl.get_rect(center=p_rect.center))

    card_top = pill_y + pill_h + 14
    bottom_reserve = max(92, int(height * 0.14))
    card_h = max(170, height - card_top - bottom_reserve)
    slide = int(max(0.0, 1.0 - card_age / 0.35) ** 2 * 50)
    card_rect = pygame.Rect(pad_x + slide, card_top, inner_w, card_h)
    pulse_g = int(190 + 50 * math.sin(t * 4.0))
    pygame.draw.rect(surface, (9, 22, 42), card_rect, border_radius=12)
    pygame.draw.rect(surface, (0, pulse_g, 255), card_rect, 2, border_radius=12)

    cur_topic = COURSE_TOPICS[idx % len(COURSE_TOPICS)]
    icon_r = int(min(card_rect.height * 0.28, width * 0.085))
    draw_topic_icon(surface, cur_topic["key"], card_rect.right - icon_r - 34, card_rect.centery, icon_r, t,
                    (60, 250, 190))

    text_w = card_rect.width - 44 - icon_r * 2 - 40
    t_font = get_fitting_font(cur_topic["title"], text_w, font_card_title.get_height(), 18, True)
    t_surf = t_font.render(cur_topic["title"], True, (65, 250, 165))
    surface.blit(t_surf, (card_rect.x + 22, card_rect.y + 16))

    body_y = card_rect.y + 22 + t_surf.get_height() + 8
    body_lines = wrap_text(cur_topic["summary"], font_body, text_w)
    body_step = font_body.get_linesize() + 5
    max_body_lines = max(2, (card_rect.bottom - body_y - 38) // max(1, body_step))
    remaining = int(card_age * 110)
    for li, line in enumerate(body_lines[:max_body_lines]):
        part = line[:max(0, remaining)]
        remaining -= len(line) + 1
        if part:
            surface.blit(font_body.render(part, True, (242, 248, 255)), (card_rect.x + 22, body_y + li * body_step))

    url_surf = font_stt.render(f"Source: {DUCAT_RISE_URL}", True, (255, 200, 80))
    surface.blit(url_surf, (card_rect.x + 22, card_rect.bottom - url_surf.get_height() - 12))
    cnt = font_pill.render(f"{idx + 1} / {len(COURSE_TOPICS)}", True, (150, 200, 225))
    surface.blit(cnt, (card_rect.right - cnt.get_width() - 18, card_rect.bottom - cnt.get_height() - 12))

    foot_y = card_rect.bottom + 8
    if stt_str:
        foot_y = draw_centered_wrapped(surface, stt_str, font_stt, (0, 235, 255), width // 2, foot_y, inner_w, 1, 2)
    if rep_str:
        draw_centered_wrapped(surface, rep_str, font_stt, (255, 230, 120), width // 2, foot_y + 2, inner_w, 2, 3)


def draw_hardware_mode_screen(surface, t, stt_str, rep_str, act_age, act_high, act_slot):
    surface.blit(bg_surface, (0, 0))
    accent = (0, 225, 255) if int(t * 2) % 2 == 0 else (255, 145, 45)
    draw_frame_decor(surface, t, accent)
    pad_x = max(24, int(width * 0.05))
    inner_w = width - pad_x * 2

    title_surf = font_mode_title.render("HARDWARE CONTROL MODE", True, (90, 245, 255))
    surface.blit(title_surf, title_surf.get_rect(center=(width // 2, 46)))
    y_sub = draw_centered_wrapped(
        surface, "Say 'first on', 'forth off', 'all on', 'all off'  |  Say 'exit' or 'leave'",
        font_stt, (255, 205, 95), width // 2, 74, inner_w, max_lines=2, line_gap=3)

    slot_names = {1: "FIRST", 2: "SECOND", 3: "THIRD", 4: "FOURTH", 5: "FIFTH"}
    palette = [(45, 245, 150), (0, 220, 255), (255, 185, 45), (255, 95, 165), (155, 125, 255)]
    states = get_gpio_states_snapshot()

    footer_space = max(72, int(height * 0.11))
    avail_h = max(220, height - (y_sub + 14) - footer_space)
    gap = max(8, int(avail_h * 0.025))
    card_h = max(42, (avail_h - gap * (len(states) - 1)) // len(states))
    start_y = y_sub + 12

    for idx, (slot, pin, is_on) in enumerate(states):
        cy = start_y + idx * (card_h + gap)
        pal = palette[idx % len(palette)]
        recent = (act_age < 1.6) and (act_slot == 0 or act_slot == slot)
        if is_on:
            border_col = (255, 235, 110) if recent else pal
            bg_col, badge_col, label = (14, 42, 48), (60, 255, 150), "HIGH (ON)"
        else:
            border_col = (255, 95, 130) if recent else (42, 62, 92)
            bg_col = (12, 18, 34)
            badge_col = (255, 125, 145) if recent else (130, 150, 175)
            label = "LOW (OFF)"

        rect = pygame.Rect(pad_x, cy, inner_w, card_h)
        pygame.draw.rect(surface, bg_col, rect, border_radius=9)
        pygame.draw.rect(surface, border_col, rect, 3 if recent else 2, border_radius=9)

        led_r = max(8, int(card_h * 0.22))
        led_x, led_y = rect.x + 24 + led_r, rect.centery
        if is_on:
            glow = int(led_r * (1.5 + 0.35 * math.sin(t * 6.0 + idx)))
            pygame.draw.circle(surface, shade_rgb(pal, 0.35), (led_x, led_y), glow)
            pygame.draw.circle(surface, pal, (led_x, led_y), led_r)
            pygame.draw.circle(surface, (255, 255, 255), (led_x - led_r // 3, led_y - led_r // 3), max(2, led_r // 4))
        else:
            pygame.draw.circle(surface, (30, 40, 60), (led_x, led_y), led_r)
            pygame.draw.circle(surface, (60, 80, 110), (led_x, led_y), led_r, 2)

        left_txt = font_hw_item.render(f"{slot_names.get(slot, f'SLOT {slot}')} -> GPIO {pin}", True, (245, 250, 255))
        right_txt = font_hw_item.render(label, True, badge_col)
        left_x = led_x + led_r + 18
        right_x = rect.right - right_txt.get_width() - 18
        surface.blit(left_txt, (left_x, cy + (card_h - left_txt.get_height()) // 2))
        surface.blit(right_txt, (right_x, cy + (card_h - right_txt.get_height()) // 2))

        m_start, m_end = left_x + left_txt.get_width() + 16, right_x - 16
        if m_end - m_start >= 40:
            mw = min(120, m_end - m_start)
            mx = (m_start + m_end - mw) // 2
            my = cy + card_h // 2 - 6
            pygame.draw.rect(surface, (22, 32, 52), (mx, my, mw, 12), border_radius=6)
            if is_on:
                fill = int(mw * (0.75 + 0.25 * math.sin(t * 9.0 + idx)))
                pygame.draw.rect(surface, pal, (mx, my, fill, 12), border_radius=6)

    draw_gpio_action_burst(surface, t, act_age, act_high, act_slot)

    bottom_y = start_y + len(states) * (card_h + gap) + 8
    if stt_str:
        bottom_y = draw_centered_wrapped(surface, stt_str, font_stt, (0, 235, 255), width // 2, bottom_y, inner_w, 1, 2)
    if rep_str:
        draw_centered_wrapped(surface, rep_str, font_stt, (255, 225, 110), width // 2, bottom_y + 2, inner_w, 1, 2)


# ---- sleep screensavers -------------------------------------------------------------
def draw_sleep_ducat_rise_animation(surface, t):
    surface.fill((4, 8, 18))
    cx, cy = width // 2, int(height * 0.44)

    ring_surf = get_overlay()
    for idx in range(4):
        phase = (t * 0.72 + idx * 0.25) % 1.0
        radius = int(55 + phase * S * 0.34)
        alpha = max(12, int(185 * (1.0 - phase)))
        ring_col = (0, 235, 255) if idx % 2 == 0 else (255, 165, 45)
        pygame.draw.circle(ring_surf, (*ring_col, alpha), (cx, cy), radius, 3)
    surface.blit(ring_surf, (0, 0))

    for orb_i in range(14):
        ang = t * 1.55 + (2 * math.pi * orb_i / 14)
        ox = int(cx + math.cos(ang) * min(width * 0.28, 220))
        oy = int(cy + math.sin(ang) * min(height * 0.16, 110))
        pygame.draw.circle(surface, (60, 250, 160) if orb_i % 2 == 0 else (255, 215, 75), (ox, oy), 6)

    hue_t = 0.5 + 0.5 * math.sin(t * 2.5)
    txt_col = (int(40 + 215 * hue_t), int(238 - 50 * hue_t), int(255 - 120 * hue_t))
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

    draw_centered_wrapped(surface, "SLEEP MODE  •  Say 'Hammer' to wake or 'enter courses'", font_stt,
                          (170, 220, 245), cx, int(height * 0.78), width - 80, max_lines=2)


def draw_sleep_screensaver(surface, t, mode_name):
    if mode_name == "DUCAT_RISE":
        draw_sleep_ducat_rise_animation(surface, t)
        return

    surface.fill((4, 8, 18))

    if mode_name == "DREAM":
        for sx, sy, ph in dream_stars:
            tw = 0.5 + 0.5 * math.sin(t * 1.5 + ph)
            c = int(60 + 170 * tw)
            pygame.draw.circle(surface, (c, c, min(255, c + 25)), (int(sx * width), int(sy * height)),
                               2 if tw > 0.85 else 1)
        mx, my, mr = int(width * 0.82), int(height * 0.20), int(S * 0.07)
        pygame.draw.circle(surface, (250, 238, 185), (mx, my), mr)
        pygame.draw.circle(surface, (4, 8, 18), (mx + int(mr * 0.45), my - int(mr * 0.2)), int(mr * 0.88))

        breath = 0.5 + 0.5 * math.sin(t * 1.1)
        cy = int(height * 0.44) + int(breath * 6)
        ew, eh = int(width * 0.22), int(height * 0.12)
        for side in (-1, 1):
            cx = width // 2 + side * int(width * 0.20)
            rect = pygame.Rect(cx - ew // 2, cy - eh // 2, ew, eh)
            pygame.draw.arc(surface, (30, 70, 100), rect, math.radians(205), math.radians(335), 14)
            pygame.draw.arc(surface, (190, 240, 245), rect, math.radians(205), math.radians(335), 6)
        for i in range(3):
            ph = ((t * 0.35) + i / 3.0) % 1.0
            zx = int(width * 0.60 + ph * width * 0.12)
            zy = int(height * 0.34 - ph * height * 0.22)
            col = shade_rgb((170, 210, 255), math.sin(math.pi * ph) + 0.05)
            surface.blit(make_font(18 + int(ph * 28)).render("Z", True, col), (zx, zy))
        draw_centered_wrapped(surface, "SLEEP MODE  •  SWEET DREAMS", font_stt, (175, 205, 235),
                              width // 2, int(height * 0.80), width - 80, max_lines=1)

    elif mode_name == "AURORA":
        aurora_surf = get_overlay()
        for band in range(3):
            pts_top, pts_bot = [], []
            base_y = height * (0.28 + band * 0.16)
            col = [(30, 240, 175, 55), (45, 175, 255, 50), (165, 115, 255, 45)][band]
            for x in range(0, width + 25, 24):
                wave = math.sin(x * 0.008 + t * (1.2 + band * 0.35) + band) * 34
                pts_top.append((x, int(base_y + wave)))
                pts_bot.append((x, int(base_y + wave + 68)))
            pygame.draw.polygon(aurora_surf, col, pts_top + list(reversed(pts_bot)))
        surface.blit(aurora_surf, (0, 0))
        for mote in aurora_motes:
            mx = int(mote[0] * width + math.sin(t * 0.7 + mote[3]) * 18)
            my = int(mote[1] * height + math.cos(t * 0.6 + mote[3]) * 14)
            twinkle = 0.5 + 0.5 * math.sin(t * mote[2] + mote[3])
            pygame.draw.circle(surface, (190, 250, 245), (mx, my), max(2, int(2 + twinkle * 3)))
        draw_centered_wrapped(surface, "SLEEP MODE  •  AURORA DRIFT", font_stt, (175, 235, 225),
                              width // 2, int(height * 0.80), width - 80, max_lines=1)

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
            pygame.draw.circle(surface, (255, 235, 130),
                               (int(cx + math.cos(ang) * rad), int(cy + math.sin(ang) * rad * 0.65)), 6)
        draw_centered_wrapped(surface, "SLEEP MODE  •  ORBIT PULSE", font_stt, (180, 220, 250),
                              cx, int(height * 0.80), width - 80, max_lines=1)

    elif mode_name == "MATRIX":
        for idx in range(num_cols):
            x = idx * 22 + 4
            y = int(matrix_drops[idx] * 16)
            for trail in range(7):
                ty = y - trail * 18
                if 0 <= ty <= height:
                    ch = chr(33 + ((idx * 17 + trail * 13 + int(t * 14)) % 89))
                    col = (190, 255, 220) if trail == 0 else (0, max(35, 220 - trail * 28), 95)
                    surface.blit(font_matrix.render(ch, True, col), (x, ty))
            matrix_drops[idx] += matrix_speeds[idx] * 0.35
            if matrix_drops[idx] * 16 > height + 120:
                matrix_drops[idx] = random.randint(-15, -2)

    elif mode_name == "WARP":
        cx, cy = width // 2, height // 2
        for star in warp_stars:
            star[2] -= 0.018
            if star[2] <= 0.05:
                star[0], star[1], star[2] = random.uniform(-1.0, 1.0), random.uniform(-1.0, 1.0), 1.0
            sx = int(cx + (star[0] / star[2]) * (width * 0.45))
            sy = int(cy + (star[1] / star[2]) * (height * 0.45))
            psx = int(cx + (star[0] / min(1.0, star[2] + 0.04)) * (width * 0.45))
            psy = int(cy + (star[1] / min(1.0, star[2] + 0.04)) * (height * 0.45))
            if 0 <= sx < width and 0 <= sy < height:
                bright = max(50, min(255, int((1.0 - star[2]) * 255)))
                pygame.draw.line(surface, (0, bright, min(255, bright + 35)), (psx, psy), (sx, sy), 2)
        pygame.draw.circle(surface, (0, 210, 255), (cx, cy), int(45 + 25 * math.sin(t * 4.0)), 2)

    else:  # DINO
        ground_y = int(height * 0.68)
        pygame.draw.line(surface, (0, 210, 240), (30, ground_y), (width - 30, ground_y), 3)
        tick_shift = int(t * 240) % 40
        for gx in range(40 - tick_shift, width - 30, 40):
            pygame.draw.line(surface, (20, 75, 105), (gx, ground_y + 8), (gx + 14, ground_y + 8), 2)
        jump_phase = (t * 1.45) % 2.4
        jump_h = int(math.sin((jump_phase / 0.85) * math.pi) * 115) if jump_phase < 0.85 else 0
        dino_x = int(width * 0.22)
        dino_y = ground_y - 44 - jump_h
        dc = (45, 245, 130)
        pygame.draw.rect(surface, dc, (dino_x, dino_y + 12, 34, 24), border_radius=4)
        pygame.draw.rect(surface, dc, (dino_x + 22, dino_y, 22, 18), border_radius=3)
        pygame.draw.rect(surface, (5, 10, 18), (dino_x + 34, dino_y + 4, 5, 5))
        pygame.draw.rect(surface, (255, 150, 30), (dino_x - 10, dino_y + 16, 12, 8))
        if int(t * 12) % 2 == 0 or jump_h > 0:
            pygame.draw.rect(surface, dc, (dino_x + 6, dino_y + 36, 7, 10))
            pygame.draw.rect(surface, (0, 160, 90), (dino_x + 22, dino_y + 36, 7, 6))
        else:
            pygame.draw.rect(surface, (0, 160, 90), (dino_x + 6, dino_y + 36, 7, 6))
            pygame.draw.rect(surface, dc, (dino_x + 22, dino_y + 36, 7, 10))
        for obs_idx in range(2):
            span = width - 120
            obs_x = int(width - 60 - (((t * 250 + obs_idx * (span * 0.55)) % span)))
            obs_h = 28 if obs_idx == 0 else 38
            pygame.draw.rect(surface, (255, 85, 65), (obs_x, ground_y - obs_h, 16, obs_h), border_radius=3)


# ==============================================================================
# 5. MAIN LOOP
# ==============================================================================
def run_selftest_steps():
    """Scenarios used by --selftest (screenshots go to /tmp/hammer_shots)."""
    def push(expr, heard="", rep=""):
        event_queue.put(("state", (expr, heard, rep, False)))

    def modes(hw=False, courses=False, sleep=False, idx=0):
        global hardware_mode, courses_mode, sleep_mode, course_index
        with state_lock:
            hardware_mode, courses_mode, sleep_mode, course_index = hw, courses, sleep, idx

    lorem = ("Python is a high-level, general-purpose programming language known for readable "
             "syntax. It supports multiple paradigms and has a huge standard library.")
    return [
        ("normal", lambda: (modes(), push("NORMAL"))),
        ("listening", lambda: push("LISTENING", "what is python programming")),
        ("thinking", lambda: push("THINKING", "what is python programming")),
        ("happy", lambda: push("HAPPY", "what is python", lorem)),
        ("excited", lambda: push("EXCITED", "hello", "Hello! I'm Hammer. Ask me anything!")),
        ("alert", lambda: push("ALERT", "", "Mic error: demo")),
        ("surprised", lambda: push("SURPRISED")),
        ("sad", lambda: push("SAD", "xyz", "No result for 'xyz'.")),
        ("love", lambda: push("LOVE", "thank you", "You're welcome!")),
        ("wink", lambda: push("WINK", "tell me a joke", JOKES[0])),
        ("sleepy", lambda: push("SLEEPY")),
        ("courses", lambda: modes(courses=True, idx=3)),
        ("hardware", lambda: (modes(hw=True), gpio_devices[17].on(), gpio_devices[23].on(),
                              record_gpio_animation(1, True))),
        ("sleep_ducat", lambda: (gpio_devices[17].off(), gpio_devices[23].off(), modes(sleep=True))),
        ("sleep_dream", lambda: globals().__setitem__("active_saver", "DREAM")),
    ]


if not SELFTEST:
    threading.Thread(target=voice_loop, daemon=True).start()
    threading.Thread(target=capture_and_idle_loop, daemon=True).start()

start_ts = time.time()
last_frame = start_ts
face = dict(BASE_FACE)
iris_cur = list(get_smooth_eye_color(0.0))
accent_cur = list(DEFAULT_ACCENT)
gaze = [0.0, 0.0]
gaze_target = [0.0, 0.0]
next_saccade = 0.0
next_blink = start_ts + 2.5
blink_start = None
smooth_energy = 0.0
expr_set_time = start_ts
last_reply_text = ""
reply_t0 = 0.0
scene_key = None
scene_t0 = start_ts
prev_sleeping = False
wake_until = 0.0
last_course_idx = -1
course_t0 = start_ts

selftest_steps = run_selftest_steps() if SELFTEST else []
SF = 40
frame_no = 0
draw_cost = 0.0
if SELFTEST:
    os.makedirs("/tmp/hammer_shots", exist_ok=True)

while running:
    for event in pygame.event.get():
        if event.type in (pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN, pygame.QUIT):
            running = False

    if SELFTEST and frame_no % SF == 0 and frame_no // SF < len(selftest_steps):
        selftest_steps[frame_no // SF][1]()

    now = time.time()
    dt = min(0.1, now - last_frame)
    last_frame = now
    t_now = now - start_ts

    while not event_queue.empty():
        kind, payload = event_queue.get()
        if kind == "state":
            new_expr, heard_text, reply_text, new_sleep = payload
            expression = new_expr
            expr_set_time = now
            with state_lock:
                sleep_mode = new_sleep

    with state_lock:
        is_sleeping = sleep_mode
        is_hw = hardware_mode
        is_courses = courses_mode
        cur_c_idx = course_index
        is_cap = capturing_session
        cur_energy = voice_energy
        voice_energy *= max(0.0, 1.0 - dt * 0.8)
        act_age = now - last_action_time
        act_high = last_action_high
        act_slot = last_action_slot
        idle_elapsed = now - last_activity_time
    smooth_energy = lerp(smooth_energy, cur_energy, dt * 10.0)

    if is_sleeping and now - last_saver_switch >= SCREENSAVER_SWITCH_SECONDS and not SELFTEST:
        active_saver = random.choice([m for m in SCREENSAVER_MODES if m != active_saver])
        last_saver_switch = now

    if is_sleeping:
        new_scene = ("sleep", active_saver)
    elif is_courses:
        new_scene = ("courses",)
    elif is_hw:
        new_scene = ("hw",)
    else:
        new_scene = ("face",)
    if new_scene != scene_key:
        if prev_sleeping and not is_sleeping:
            wake_until = now + 1.1          # startled "wake-up" look
            face["open"] = 0.05
        scene_key, scene_t0 = new_scene, now
    prev_sleeping = is_sleeping

    t_draw = time.perf_counter()
    if is_sleeping:
        draw_sleep_screensaver(screen, t_now, active_saver)

    elif is_courses:
        if cur_c_idx != last_course_idx:
            last_course_idx, course_t0 = cur_c_idx, now
        draw_courses_mode_screen(screen, t_now, cur_c_idx, heard_text, reply_text, now - course_t0)

    elif is_hw:
        draw_hardware_mode_screen(screen, t_now, heard_text, reply_text, act_age, act_high, act_slot)

    else:
        # ---- choose the face -------------------------------------------------
        face_expr = expression if expression in FACES else "NORMAL"
        if is_cap:
            face_expr = "LISTENING"
        expr_age = now - expr_set_time
        if now < wake_until:
            face_expr = "SURPRISED"
        elif face_expr in TRANSIENT and expr_age > HOLD_OVERRIDE.get(face_expr, EXPRESSION_HOLD_SECONDS):
            face_expr = "HAPPY" if (face_expr == "WINK" and expr_age < EXPRESSION_HOLD_SECONDS) else "NORMAL"
        if (face_expr == "NORMAL" and not is_cap and not SELFTEST
                and idle_elapsed > IDLE_SLEEP_SECONDS - DROWSY_BEFORE_SLEEP):
            face_expr = "SLEEPY"

        # ---- blend parameters -----------------------------------------------
        k = 1.0 - math.exp(-dt * 9.0)
        for key, val in FACES[face_expr].items():
            face[key] += (val - face[key]) * k
        iris_target = FACE_TINT.get(face_expr) or get_smooth_eye_color(t_now)
        ck = min(1.0, dt * 4.0)
        for i in range(3):
            iris_cur[i] += (iris_target[i] - iris_cur[i]) * ck
            accent_cur[i] += (FACE_ACCENT.get(face_expr, DEFAULT_ACCENT)[i] - accent_cur[i]) * ck
        iris_rgb = to_rgb(iris_cur)
        accent_rgb = to_rgb(accent_cur)

        # ---- blinking (random, with the occasional double blink) ----------------
        blink = 1.0
        if blink_start is None and now >= next_blink and face_expr not in ("SLEEPY", "SURPRISED"):
            blink_start = now
        if blink_start is not None:
            pr = (now - blink_start) / 0.17
            if pr >= 1.0:
                blink_start = None
                next_blink = now + (0.07 if random.random() < 0.2 else random.uniform(2.2, 5.5))
            else:
                blink = 1.0 - math.sin(math.pi * pr)

        # ---- gaze ----------------------------------------------------------------
        fast = False
        if face_expr == "THINKING":
            gaze_target = [math.sin(t_now * 1.7) * 0.75, -0.55 + math.cos(t_now * 1.3) * 0.2]
        elif face_expr == "LISTENING":
            gaze_target = [math.sin(t_now * 2.3) * 0.10, -0.12]
        elif face_expr == "SLEEPY":
            gaze_target = [0.0, 0.45]
        elif face_expr in ("LOVE", "SURPRISED"):
            gaze_target = [0.0, -0.05]
        elif now >= next_saccade:
            if face_expr == "ALERT":
                gaze_target = [random.uniform(-0.6, 0.6), random.uniform(-0.3, 0.3)]
                next_saccade, fast = now + 0.18, True
            else:
                gaze_target = ([0.0, 0.0] if random.random() < 0.35
                               else [random.uniform(-0.8, 0.8), random.uniform(-0.5, 0.4)])
                next_saccade, fast = now + random.uniform(1.2, 3.8), True
        gk = min(1.0, dt * (16.0 if fast else 7.0))
        gaze[0] += (gaze_target[0] - gaze[0]) * gk
        gaze[1] += (gaze_target[1] - gaze[1]) * gk

        # ---- draw ------------------------------------------------------------------
        draw_hud_backdrop(screen, t_now, accent_rgb, smooth_energy)
        draw_face(screen, t_now, face, gaze, iris_rgb, blink, smooth_energy)
        spawn_particles(face_expr, dt)
        update_particles(dt)
        draw_particles(screen)
        draw_gpio_action_burst(screen, t_now, act_age, act_high, act_slot)
        draw_status_bar(screen, t_now, STATUS_LABEL.get(face_expr, "ONLINE"), smooth_energy, accent_rgb)

        text_area_w = width - max(48, int(width * 0.08))
        cur_y = int(height * 0.54)
        if face_expr == "LISTENING" or is_cap:
            draw_listening_waveform(screen, t_now, smooth_energy, accent_rgb)
            cur_y = int(height * 0.64)

        if heard_text:
            cur_y = draw_centered_wrapped(screen, heard_text, font_stt, (0, 235, 255), width // 2, cur_y,
                                          text_area_w, max_lines=2, line_gap=4) + 6

        if reply_text != last_reply_text:
            last_reply_text, reply_t0 = reply_text, now
        if reply_text:
            max_rep_lines = max(2, (height - cur_y - 40) // max(1, font_reply.get_linesize() + 6))
            draw_centered_wrapped(screen, reply_text, font_reply, (255, 225, 110), width // 2, cur_y,
                                  text_area_w, max_lines=min(5, max_rep_lines), line_gap=6,
                                  reveal=(now - reply_t0) * TYPEWRITER_CPS)

        if not heard_text and not reply_text and not is_cap:
            hint = font_pill.render("Say 'Hammer' and ask me anything", True,
                                    shade_rgb(accent_rgb, 0.55 + 0.25 * math.sin(t_now * 2.0)))
            screen.blit(hint, hint.get_rect(center=(width // 2, height - 28)))

    # smooth scene cross-fade from black
    scene_age = now - scene_t0
    if scene_age < 0.45:
        fade_surface.set_alpha(int(255 * (1.0 - scene_age / 0.45)))
        screen.blit(fade_surface, (0, 0))

    draw_cost += time.perf_counter() - t_draw
    pygame.display.flip()

    if SELFTEST:
        if frame_no % SF == SF - 2 and frame_no // SF < len(selftest_steps):
            name = selftest_steps[frame_no // SF][0]
            pygame.image.save(screen, f"/tmp/hammer_shots/{frame_no // SF:02d}_{name}.png")
        frame_no += 1
        if frame_no >= SF * len(selftest_steps):
            print(f"selftest OK: {frame_no} frames, avg draw {1000 * draw_cost / frame_no:.1f} ms/frame")
            running = False

    clock.tick(30)

set_all_gpios(False)
for dev in gpio_devices.values():
    dev.close()
pygame.quit()
sys.exit()
