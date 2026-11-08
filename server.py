"""
Wardrobe AI - local outfit helper.

Run:  python server.py
Then the browser opens at http://127.0.0.1:8765

Needs only the Python standard library and a running Ollama with a vision model.
Change the model with the WARDROBE_MODEL environment variable (or edit MODEL below).
"""

import base64
import hashlib
import itertools
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- settings
HOST = "127.0.0.1"
PORT = int(os.environ.get("WARDROBE_PORT", "8765"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("WARDROBE_MODEL", "qwen3-vl:2b")  # RTX 3050 4 GB friendly
GOOD_SCORE = 75  # outfits at or above this count as "good"
# Who the advice is for. Edit to taste.
PROFILE = os.environ.get(
    "WARDROBE_PROFILE",
    "a 30-year-old man upgrading from casual basics to a refined, chic, modern style",
)
SKIN_TONE = os.environ.get(
    "WARDROBE_SKIN_TONE", "medium tan skin with warm-neutral undertones"
)
SKIN_TONE_TOP_COLORS = {
    "camel", "brown", "cream", "beige", "olive", "sage", "rust", "burgundy",
    "navy", "green", "blue",
}

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "wardrobe"
IMG_DIR = DATA_DIR / "images"
ITEMS_FILE = DATA_DIR / "items.json"
INDEX_FILE = ROOT / "index.html"
IMG_DIR.mkdir(parents=True, exist_ok=True)

CATEGORIES = ["top", "bottom", "shoes"]
OPTIONAL_CATEGORIES = ["layer"]
ITEM_CATEGORIES = CATEGORIES + OPTIONAL_CATEGORIES
COLORS = [
    "black", "charcoal", "gray", "white", "cream", "beige", "camel", "khaki",
    "brown", "navy", "denim", "blue", "light blue", "olive", "sage", "green",
    "burgundy", "red", "rust", "orange", "pink", "yellow", "purple",
]
PATTERNS = ["solid", "striped", "checked", "floral", "graphic", "other"]
SEASONS = ["spring", "summer", "fall", "winter"]
LAYER_ROLES = ["auto", "top", "mid", "outer"]

# How each colour name tends to look in a photo. Several references per name cover
# light/dark shades, so a shadowed camel knit still reads as camel. The first entry
# is the swatch shown in the page.
COLOR_REFS = {
    "black": [(20, 20, 22), (38, 38, 40)],
    "charcoal": [(54, 57, 61), (72, 74, 78)],
    "gray": [(128, 128, 128), (165, 165, 165), (105, 106, 108)],
    "white": [(246, 246, 246), (230, 232, 235)],
    "cream": [(240, 232, 214), (232, 222, 196)],
    "beige": [(217, 200, 169), (198, 184, 156)],
    "camel": [(193, 154, 107), (176, 132, 84)],
    "khaki": [(185, 167, 122), (160, 150, 110)],
    "brown": [(90, 58, 36), (122, 82, 52), (62, 42, 30)],
    "navy": [(31, 42, 68), (40, 52, 84), (24, 30, 48)],
    "denim": [(65, 89, 122), (92, 117, 152), (50, 68, 98)],
    "blue": [(44, 95, 191), (32, 78, 160)],
    "light blue": [(156, 195, 230), (182, 206, 230)],
    "olive": [(107, 107, 46), (86, 90, 52)],
    "sage": [(163, 177, 138), (140, 156, 122)],
    "green": [(31, 90, 61), (42, 122, 72)],
    "burgundy": [(109, 27, 43), (88, 30, 40)],
    "red": [(198, 40, 40), (170, 30, 36)],
    "rust": [(183, 71, 42), (158, 82, 52)],
    "orange": [(239, 125, 34), (226, 140, 70)],
    "pink": [(232, 166, 180), (218, 140, 160)],
    "yellow": [(242, 207, 58), (228, 200, 92)],
    "purple": [(106, 61, 154), (80, 50, 110)],
}

_lock = threading.Lock()


# ---------------------------------------------------------------- storage
def load_items():
    if not ITEMS_FILE.exists():
        return []
    try:
        return json.loads(ITEMS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_items(items):
    tmp = ITEMS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(ITEMS_FILE)


def find_duplicate_image(image_hash, items):
    for item in items:
        if item.get("image_hash") == image_hash:
            return item
        item_id = item.get("id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", item_id):
            continue
        path = IMG_DIR / f"{item_id}.jpg"
        try:
            existing_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        if existing_hash == image_hash:
            return item
    return None


def normalize_tags(raw, category):
    """Force model output (or user edits) into the allowed vocabulary."""
    raw = raw or {}

    def pick(value, allowed, default):
        value = str(value or "").strip().lower()
        if value in allowed:
            return value
        aliases = {
            "dark blue": "navy", "deep blue": "navy", "midnight blue": "navy",
            "dark gray": "charcoal", "dark grey": "charcoal",
            "light gray": "gray", "light grey": "gray",
            "off-white": "cream", "off white": "cream", "ivory": "cream",
        }
        if value in aliases and aliases[value] in allowed:
            return aliases[value]
        for a in sorted(allowed, key=len, reverse=True):  # "dark navy" -> "navy"
            if a in value:
                return a
        return default

    try:
        formality = int(raw.get("formality", 2))
    except (TypeError, ValueError):
        formality = 2
    seasons = [s for s in (raw.get("seasons") or []) if s in SEASONS] or list(SEASONS)
    secondary = str(raw.get("secondary_color") or "none").lower()
    layer_role = str(raw.get("layer_role") or "auto").lower()
    return {
        "category": category if category in ITEM_CATEGORIES else "top",
        "layer_role": layer_role if layer_role in LAYER_ROLES else "auto",
        "type": str(raw.get("type") or category)[:40],
        "primary_color": pick(raw.get("primary_color"), COLORS, "gray"),
        "secondary_color": secondary if secondary in COLORS else "none",
        "pattern": pick(raw.get("pattern"), PATTERNS, "solid"),
        "formality": max(1, min(5, formality)),
        "seasons": seasons,
        "material": str(raw.get("material") or "")[:40],
        "description": str(raw.get("description") or "")[:200],
    }


def label(item):
    return f"{item['primary_color']} {item['type']}"


# ---------------------------------------------------------------- garment kinds
# Order matters: "overshirt" and "t-shirt" both contain "shirt", "sport coat" contains "coat".
TOP_KINDS = [
    ("hoodie", ("hoodie", "hoody")),
    ("overshirt", ("overshirt", "shacket", "shirt jacket")),
    ("blazer", ("blazer", "sport coat", "suit jacket")),
    ("coat", ("overcoat", "peacoat", "trench", "topcoat", "coat")),
    ("jacket", ("jacket", "bomber", "parka", "puffer", "windbreaker", "anorak", "harrington")),
    ("gilet", ("gilet", "waistcoat", "vest")),
    ("sweatshirt", ("sweatshirt", "crewneck sweat")),
    ("cardigan", ("cardigan",)),
    ("polo", ("polo",)),
    ("sweater", ("sweater", "jumper", "knit", "turtleneck", "crewneck", "merino", "quarter-zip", "pullover")),
    ("t-shirt", ("t-shirt", "tshirt", "tee")),
    ("tank top", ("tank",)),
    ("shirt", ("shirt", "oxford", "button", "flannel")),
]
BOTTOM_KINDS = [
    ("jeans", ("jean", "denim")),
    ("shorts", ("short",)),
    ("joggers", ("jogger", "sweatpant", "track")),
    ("chinos", ("chino",)),
    ("trousers", ("trouser", "pant", "slack")),
]
SHOE_KINDS = [
    ("loafers", ("loafer",)),
    ("boots", ("boot",)),
    ("sneakers", ("sneaker", "trainer", "runner", "running")),
    ("dress shoes", ("derby", "oxford", "brogue", "dress shoe", "monk")),
    ("sandals", ("sandal", "slide")),
]
KIND_RULES = {"top": TOP_KINDS, "layer": TOP_KINDS, "bottom": BOTTOM_KINDS, "shoes": SHOE_KINDS}
OUTER_KINDS = {"blazer", "coat", "jacket", "gilet"}
HOODIE_KINDS = {"hoodie", "sweatshirt"}
SHIRT_KINDS = {"shirt", "t-shirt", "polo"}
BULKY_KINDS = {"hoodie", "sweatshirt", "sweater", "cardigan"}
MID_KINDS = BULKY_KINDS | {"overshirt"}


def kind(item):
    """Plain garment label (jacket, hoodie, shirt, jeans...) from the free-text type."""
    text = str(item.get("type") or "").lower()
    for name, words in KIND_RULES.get(item.get("category"), ()):
        if any(w in text for w in words):
            return name
    return text.strip() or item.get("category", "item")


def layer_role(item):
    """How a top-half piece is worn, worked out from its type so you only upload "tops":
    'top'   = base layer only (shirt, tee, polo)
    'mid'   = worn alone or layered over a shirt (hoodie, overshirt, sweater, cardigan)
    'outer' = always over something else, never on its own (jacket, coat, blazer)"""
    role = item.get("layer_role", "auto")
    if role in ("top", "mid", "outer"):
        return role
    k = kind(item)
    if k in OUTER_KINDS:
        return "outer"
    if k in MID_KINDS:
        return "mid"
    return "mid" if item.get("category") == "layer" else "top"


def slot(item):
    """Where an item goes in an outfit: top, mid, outer, bottom or shoes."""
    if item["category"] in ("top", "layer"):
        return layer_role(item)
    return item["category"]


def public_item(item):
    return {**item, "kind": kind(item), "slot": slot(item)}


# ---------------------------------------------------------------- photo colours
def _lab(rgb):
    def lin(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(float(v)) for v in rgb)
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


LIGHT_NEUTRALS = {"white", "cream", "beige"}
REF_LABS = {name: [_lab(c) for c in refs] for name, refs in COLOR_REFS.items()}


def _dist(a, b):
    # Lightness counts a bit less than hue, so shadows and highlights don't flip the name.
    return ((0.75 * (a[0] - b[0])) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def color_distance(lab, name):
    return min(_dist(lab, ref) for ref in REF_LABS[name])


def nearest_color(rgb):
    lab = _lab(rgb)
    return min(COLORS, key=lambda n: color_distance(lab, n))


def clean_measured(raw):
    """Validate the colour clusters the browser measured from the photo."""
    out = []
    for c in (raw or [])[:4] if isinstance(raw, list) else []:
        try:
            rgb = [max(0, min(255, int(v))) for v in c["rgb"]][:3]
            share = max(0.0, min(1.0, float(c["share"])))
        except (KeyError, TypeError, ValueError):
            continue
        if len(rgb) == 3 and share > 0:
            out.append({"rgb": rgb, "share": share})
    return sorted(out, key=lambda c: c["share"], reverse=True)


def resolve_colors(tags, measured):
    """Check the model's colour names against the colours measured from the photo's pixels.
    The model keeps its pick when it is close to what was measured (it handles nuance like
    khaki vs beige, or white in warm light); otherwise the measured colour wins."""
    clusters = clean_measured(measured)
    if not clusters:
        return tags, []
    detected = [{"color": nearest_color(c["rgb"]), "share": round(c["share"], 2),
                 "hex": "#%02x%02x%02x" % tuple(c["rgb"])} for c in clusters]
    main_lab = _lab(clusters[0]["rgb"])
    measured_name = detected[0]["color"]
    model = tags["primary_color"]
    d_model = color_distance(main_lab, model)
    d_measured = color_distance(main_lab, measured_name)
    primary = model if d_model <= max(12, d_measured + 8) else measured_name
    if {model, measured_name} <= LIGHT_NEUTRALS:
        primary = model  # white vs cream vs beige depends on the light; trust the model

    secondary = "none"
    for c, d in zip(clusters[1:], detected[1:]):
        if c["share"] >= 0.15 and d["color"] != primary and _dist(_lab(c["rgb"]), main_lab) > 25:
            secondary = d["color"]
            break
    model_secondary = tags.get("secondary_color", "none")
    if secondary == "none" and model_secondary not in ("none", primary):
        if any(c["share"] >= 0.08 and color_distance(_lab(c["rgb"]), model_secondary) <= 14
               for c in clusters[1:]):
            secondary = model_secondary
    return {**tags, "primary_color": primary, "secondary_color": secondary}, detected


# ---------------------------------------------------------------- ollama
def ollama_chat(prompt, image_b64=None, schema=None, timeout=180):
    message = {"role": "user", "content": prompt}
    if image_b64:
        message["images"] = [image_b64]
    body = {
        "model": MODEL,
        "messages": [message],
        "stream": False,
        "options": {"temperature": 0 if schema else 0.4},
    }
    if schema:
        body["format"] = schema
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["message"]["content"]


TAG_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "primary_color": {"type": "string", "enum": COLORS},
        "secondary_color": {"type": "string", "enum": COLORS + ["none"]},
        "pattern": {"type": "string", "enum": PATTERNS},
        "formality": {"type": "integer", "minimum": 1, "maximum": 5},
        "seasons": {"type": "array", "items": {"type": "string", "enum": SEASONS}},
        "material": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["type", "primary_color", "secondary_color", "pattern",
                 "formality", "seasons", "material", "description"],
}


def tag_image(image_b64, category, measured=None):
    detected = [f"{nearest_color(c['rgb'])} ({round(c['share'] * 100)}%)"
                for c in clean_measured(measured)]
    hint = (f"A pixel analysis of the garment (background removed) measured these colours: "
            f"{', '.join(detected)}. Use them unless the photo clearly shows otherwise "
            "(for example white fabric under warm light).\n") if detected else ""
    prompt = (
        f"The upload category is {category}, but identify the specific garment visible in the photo.\n"
        "Report the main visible color of the garment fabric, not the background, skin, labels or shadows. "
        "Use navy for very dark blue, blue for clearer/brighter blue, black only for true black, "
        "charcoal for very dark gray, and gray for lighter gray. Do not guess details that cannot be seen.\n"
        + hint +
        "Return JSON with:\n"
        "- type: the specific garment, not the broad upload category, e.g. 't-shirt', 'oxford shirt', "
        "'knit polo', 'sweater', 'zip hoodie', 'overshirt', 'denim jacket', 'bomber jacket', "
        "'blazer', 'overcoat', 'jeans', 'chinos', 'pleated trousers', "
        "'sneakers', 'loafers', 'chelsea boots'\n"
        "- primary_color and secondary_color (or 'none') from the allowed list. "
        "camel = warm tan/caramel, charcoal = very dark gray, cream = off-white\n"
        "- pattern\n"
        "- formality from 1 (gym/lounge) 2 (casual) 3 (smart casual) 4 (business) 5 (formal)\n"
        "- seasons it suits\n"
        "- material, e.g. cotton, linen, denim, wool, suede, leather\n"
        "- description: one short sentence"
    )
    content = ollama_chat(prompt, image_b64=image_b64, schema=TAG_SCHEMA)
    return normalize_tags(json.loads(content), category)


def ollama_status():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as resp:
            names = [m.get("name", "") for m in json.loads(resp.read()).get("models", [])]
        has_model = any(n == MODEL or n.split(":")[0] == MODEL for n in names)
        return {"ollama": True, "model": MODEL, "model_installed": has_model}
    except Exception:
        return {"ollama": False, "model": MODEL, "model_installed": False}


# ---------------------------------------------------------------- colour rules
# Neutrals go with almost anything.
NEUTRALS = {"black", "charcoal", "gray", "white", "cream", "beige", "camel",
            "khaki", "brown", "navy", "denim"}
# Families used for tonal (same-family) dressing - the dominant chic look right now.
FAMILIES = {
    "earth": {"brown", "camel", "beige", "cream", "khaki", "olive", "rust", "sage"},
    "blue": {"navy", "denim", "blue", "light blue"},
    "gray": {"black", "charcoal", "gray"},
    "light": {"white", "cream", "beige"},
}
GOOD_PAIRS = {frozenset(p) for p in [
    ("blue", "light blue"), ("olive", "burgundy"), ("light blue", "pink"),
    ("olive", "orange"), ("green", "pink"), ("burgundy", "pink"), ("olive", "rust"),
    ("sage", "rust"), ("sage", "pink"), ("light blue", "burgundy"), ("olive", "light blue"),
    ("green", "rust"), ("sage", "olive"),
]}
CLASH_PAIRS = {frozenset(p) for p in [
    ("red", "pink"), ("red", "orange"), ("red", "green"), ("purple", "orange"),
    ("orange", "pink"), ("yellow", "purple"), ("red", "purple"), ("red", "burgundy"),
    ("green", "purple"), ("rust", "pink"), ("red", "rust"), ("yellow", "pink"),
]}
SOFT_CLASH = {frozenset(("black", "brown"))}


def color_pair(a, b):
    if a == b:
        return 0.85 if a in NEUTRALS else 0.5
    pair = frozenset((a, b))
    if pair in SOFT_CLASH:
        return 0.65
    if a in NEUTRALS or b in NEUTRALS:
        return 1.0
    if pair in GOOD_PAIRS:
        return 0.85
    if pair in CLASH_PAIRS:
        return 0.15
    return 0.45


def palette(colors):
    """Name the look and score how chic/current it is (0-1).
    Based on 2026 trends: tonal earth tones, chocolate/camel, navy & cream,
    olive & beige, pink & brown, one rich red/burgundy accent on neutrals."""
    c = set(colors)
    loud = [x for x in colors if x not in NEUTRALS]
    has = lambda *xs: any(x in c for x in xs)  # noqa: E731

    if c <= FAMILIES["earth"] and len(c) >= 2:
        return "Tonal earth (brown · camel · cream)", 1.0
    if "navy" in c and has("cream", "beige", "camel") and not loud:
        return "Navy & cream", 0.95
    if "navy" in c and "white" in c and not loud:
        return "Navy & white", 0.9
    if "charcoal" in c and has("camel", "cream", "brown") and not loud:
        return "Charcoal & camel", 0.95
    if has("olive", "sage") and has("beige", "cream", "khaki", "camel", "white"):
        return "Olive & beige utility", 0.9
    if "pink" in c and has("brown", "cream", "camel"):
        return "Pink & brown", 0.9
    if len(loud) == 1 and loud[0] in {"burgundy", "red", "rust", "green"}:
        return f"Rich {loud[0]} accent", 0.9
    if "white" in c and has("beige", "cream", "camel") and not loud:
        return "White & beige", 0.88
    for name, fam in FAMILIES.items():
        if c <= fam and len(c) >= 2:
            return f"Tonal {name}", 0.85
    if len(c) == 1:
        return "Monochrome", 0.8 if loud == [] else 0.4
    if not loud:
        if c <= {"black", "white", "gray", "charcoal"}:
            return "Classic black & white", 0.6
        return "Clean neutrals", 0.72
    if len(loud) == 1:
        return f"{loud[0].title()} accent", 0.65
    return "Mixed colours", 0.35


def occasion(avg_formality):
    if avg_formality < 1.8:
        return "Lounge / gym"
    if avg_formality < 2.6:
        return "Weekend casual"
    if avg_formality < 3.4:
        return "Smart casual / dinner"
    if avg_formality < 4.3:
        return "Office"
    return "Formal event"


OUTFIT_KEYS = ("top", "mid", "outer", "bottom", "shoes")
UPPER_KEYS = ("top", "mid", "outer")


def outfit_ids(o):
    return tuple(o[k] for k in OUTFIT_KEYS if o.get(k))


def layering_label(top, mid=None, outer=None):
    """Inner to outer, e.g. 'Shirt + hoodie + jacket'."""
    parts = [kind(i) for i in (top, mid, outer) if i]
    return " + ".join(parts).capitalize()


def score_outfit(top, bottom, shoes, mid=None, outer=None):
    layers = [x for x in (mid, outer) if x]
    outfit = (top, *layers, bottom, shoes)
    t, b, s = (i["primary_color"] for i in (top, bottom, shoes))
    colors = (t, b, s) + tuple(x["primary_color"] for x in layers)
    reasons = []

    if layers:
        harmony = 0.35 * color_pair(t, b) + 0.15 * color_pair(t, s) + 0.15 * color_pair(b, s)
        w = 1 / len(layers)
        under = t
        for x in layers:
            c = x["primary_color"]
            harmony += w * (0.20 * color_pair(under, c) + 0.075 * color_pair(b, c)
                            + 0.075 * color_pair(s, c))
            under = c
    else:
        harmony = 0.5 * color_pair(t, b) + 0.25 * color_pair(t, s) + 0.25 * color_pair(b, s)
    loud = [c for c in colors if c not in NEUTRALS]
    if len(loud) >= 3:
        harmony -= 0.2
        reasons.append("three bold colours compete")
    clash = color_pair(t, b) <= 0.15
    if clash:
        reasons.append(f"{t} and {b} clash")
    under = top
    for x in layers:
        if color_pair(under["primary_color"], x["primary_color"]) <= 0.15:
            clash = True
            reasons.append(f"{under['primary_color']} and {x['primary_color']} clash")
        under = x

    look, chic = palette(colors)
    tone_bonus = 3 if t in SKIN_TONE_TOP_COLORS else 0
    if tone_bonus:
        reasons.append("top colour complements your skin tone")
    # Upscale details: graphic prints and gym shoes pull a look down hard.
    penalty = 0
    if any(i["pattern"] == "graphic" for i in outfit):
        penalty += 15
        reasons.append("graphic prints read young; swap for a plain knit or shirt")
    if shoes["formality"] <= 1 and max(
        item["formality"] for item in outfit if item is not shoes
    ) >= 2:
        penalty += 12
        reasons.append("gym trainers; try clean leather sneakers or loafers")

    forms = [i["formality"] for i in outfit]
    spread = max(forms) - min(forms)
    formality = {0: 1.0, 1: 0.9, 2: 0.3}.get(spread, 0.0)
    if spread >= 2:
        penalty += 8
        reasons.append("dress levels don't match")

    patterned = sum(1 for i in outfit if i["pattern"] != "solid")
    pattern = 1.0 if patterned <= 1 else 0.35
    if patterned >= 2:
        reasons.append("too many patterns")

    common = set.intersection(*(set(item["seasons"]) for item in outfit))
    season = 1.0 if common else 0.4
    if not common:
        reasons.append("pieces suit different seasons")

    total = 100 * (0.35 * max(harmony, 0) + 0.20 * max(chic, 0) + 0.25 * formality
                   + 0.12 * pattern + 0.08 * season) - penalty + tone_bonus
    if clash:
        total = min(total, GOOD_SCORE - 15)  # a clashing top/bottom is never "good"
    return {
        "score": min(100, max(0, round(total))),
        "palette": look,
        "layering": layering_label(top, mid, outer),
        "occasion": occasion(sum(forms) / len(forms)),
        "reasons": reasons,
        "seasons": sorted(common),
    }


LAYERING_MODES = {
    "any": "Any",
    "none": "No layers (one top)",
    "layered": "Any layered look",
    "jacket": "With a jacket / coat",
    "jacket_shirt": "Shirt or tee + jacket",
    "jacket_hoodie": "Hoodie + jacket",
    "hoodie_shirt": "Shirt or tee + hoodie / overshirt",
    "three": "Three layers (shirt + hoodie + jacket)",
}


def layering_ok(mode, top, mid, outer):
    if not mode or mode == "any":
        return True
    if mode == "none":
        return not mid and not outer
    if mode == "layered":
        return bool(mid or outer)
    if mode == "jacket":
        return bool(outer)
    if mode == "jacket_shirt":
        return bool(outer) and not mid and kind(top) in SHIRT_KINDS
    if mode == "jacket_hoodie":
        return bool(outer) and HOODIE_KINDS & {kind(top), kind(mid) if mid else ""}
    if mode == "hoodie_shirt":
        return bool(mid) and not outer and kind(top) in SHIRT_KINDS
    if mode == "three":
        return bool(mid and outer)
    return True


def outfit_combos(items, layers=True, pinned=()):
    """Every wearable (top, mid, outer, bottom, shoes) combination.
    A jacket/coat is never worn on its own: it always goes over a top."""
    by_id = {i["id"]: i for i in items}
    upper = [i for i in items if i["category"] in ("top", "layer")]
    bases = [i for i in upper if layer_role(i) in ("top", "mid")]
    mids = [i for i in upper if layer_role(i) == "mid"] if layers else []
    outer_opts = [None] + ([i for i in upper if layer_role(i) == "outer"] if layers else [])
    bottoms = [i for i in items if i["category"] == "bottom"]
    shoes = [i for i in items if i["category"] == "shoes"]

    fixed, need_upper = {}, set()
    for pid in pinned:
        if pid not in by_id:
            raise ValueError("Unknown item picked.")
        p = by_id[pid]
        s = slot(p)
        if s in ("bottom", "shoes", "top", "outer"):
            if s in fixed:
                raise ValueError(f"You picked two {s} pieces; pick just one.")
            fixed[s] = p
        if s in UPPER_KEYS:
            need_upper.add(pid)
    if "bottom" in fixed:
        bottoms = [fixed["bottom"]]
    if "shoes" in fixed:
        shoes = [fixed["shoes"]]
    if "top" in fixed:
        bases = [fixed["top"]]
    if "outer" in fixed:
        outer_opts = [fixed["outer"]]
    mid_pins = [by_id[p] for p in need_upper if slot(by_id[p]) == "mid"]
    if mid_pins and not layers:  # a picked hoodie can still be worn over a shirt
        mids = mid_pins
    mid_opts = [None] + mids
    kinds = {i["id"]: kind(i) for i in upper}

    for top in bases:
        for mid in mid_opts:
            if mid is not None and (mid is top or (
                    kinds[mid["id"]] in BULKY_KINDS
                    and kinds[top["id"]] in BULKY_KINDS | {"overshirt"})):
                continue  # no hoodie over a sweater or an overshirt
            for outer in outer_opts:
                if need_upper and not need_upper <= {x["id"] for x in (top, mid, outer) if x}:
                    continue
                for bottom in bottoms:
                    for shoe in shoes:
                        yield top, mid, outer, bottom, shoe


def all_outfits(items, season=None, layers=True, pinned=(), layering=None):
    results = []
    for top, mid, outer, bottom, shoes in outfit_combos(items, layers, pinned):
        if not layering_ok(layering, top, mid, outer):
            continue
        o = score_outfit(top, bottom, shoes, mid, outer)
        if season and season not in o["seasons"]:
            continue
        outfit = {"top": top["id"], "bottom": bottom["id"], "shoes": shoes["id"]}
        if mid:
            outfit["mid"] = mid["id"]
        if outer:
            outfit["outer"] = outer["id"]
        results.append({**outfit, **o})
    results.sort(key=lambda o: o["score"], reverse=True)
    return results


OK_SCORE = 60  # shown after the good ones so there are plenty of options


def lookbook(items, season=None, limit=24, pinned=(), layering=None):
    """Pick the best outfits while spreading them across your clothes,
    so you don't get 10 looks that all use the same shirt."""
    outfits = all_outfits(items, season, pinned=pinned, layering=layering)
    if not pinned:
        outfits = [o for o in outfits if o["occasion"] != "Lounge / gym"]
    good = [o for o in outfits if o["score"] >= GOOD_SCORE]
    okay = [o for o in outfits if OK_SCORE <= o["score"] < GOOD_SCORE]
    pinned = set(pinned)
    max_uses = max(2, limit // 8)
    uses = Counter()
    picks, picked = [], set()
    # Pass 1: best outfits, no piece used more than max_uses times (picked items don't count).
    for o in good:
        ids = [i for i in outfit_ids(o) if i not in pinned]
        if all(uses[i] < max_uses for i in ids):
            picks.append(o)
            picked.add(id(o))
            uses.update(ids)
            if len(picks) >= limit:
                return picks, len(good)
    # Pass 2: the remaining good ones, then decent ones, so there are many options.
    for o in good + okay:
        if id(o) not in picked:
            picks.append(o)
            if len(picks) >= limit:
                break
    if not picks and pinned:
        picks = outfits[:6]  # nothing decent: show the closest so you can see why
    return picks, len(good)


# ---------------------------------------------------------------- what to buy
# Upscale staples a 30-something wardrobe is built on (category, name, type, colour, formality).
CANDIDATES = [
    ("top", "White oxford shirt", "oxford shirt", "white", 3),
    ("top", "Light blue oxford shirt", "oxford shirt", "light blue", 3),
    ("top", "Cream knit polo", "knit polo", "cream", 3),
    ("top", "Navy merino crewneck", "sweater", "navy", 3),
    ("top", "Camel quarter-zip knit", "sweater", "camel", 3),
    ("top", "Charcoal fine-knit turtleneck", "turtleneck", "charcoal", 3),
    ("top", "Chocolate brown overshirt", "overshirt", "brown", 2),
    ("top", "Olive overshirt", "overshirt", "olive", 2),
    ("top", "Burgundy merino knit", "sweater", "burgundy", 3),
    ("top", "Heavyweight white t-shirt", "t-shirt", "white", 2),
    ("top", "Heavyweight black t-shirt", "t-shirt", "black", 2),
    ("top", "Cream linen shirt", "linen shirt", "cream", 3),
    ("top", "Gray zip hoodie", "zip hoodie", "gray", 2),
    ("top", "Navy hoodie", "hoodie", "navy", 2),
    ("top", "Cream hoodie", "hoodie", "cream", 2),
    ("top", "Navy harrington jacket", "harrington jacket", "navy", 3),
    ("top", "Olive field jacket", "field jacket", "olive", 2),
    ("top", "Black leather jacket", "leather jacket", "black", 3),
    ("top", "Dark denim jacket", "denim jacket", "denim", 2),
    ("top", "Camel overcoat", "overcoat", "camel", 4),
    ("top", "Navy blazer", "blazer", "navy", 4),
    ("bottom", "Dark indigo straight jeans", "jeans", "denim", 2),
    ("bottom", "Black straight jeans", "jeans", "black", 2),
    ("bottom", "Charcoal wool trousers", "trousers", "charcoal", 4),
    ("bottom", "Cream pleated trousers", "pleated trousers", "cream", 3),
    ("bottom", "Beige pleated chinos", "chinos", "beige", 3),
    ("bottom", "Navy chinos", "chinos", "navy", 3),
    ("bottom", "Chocolate brown trousers", "trousers", "brown", 3),
    ("bottom", "Olive chinos", "chinos", "olive", 3),
    ("shoes", "White minimal leather sneakers", "sneakers", "white", 2),
    ("shoes", "Brown suede loafers", "loafers", "brown", 4),
    ("shoes", "Dark brown suede chelsea boots", "chelsea boots", "brown", 3),
    ("shoes", "Black leather loafers", "loafers", "black", 4),
    ("shoes", "Tan suede desert boots", "desert boots", "camel", 3),
]


def _fake(cat, typ, color, formality, fake_id="candidate"):
    return {"id": fake_id, "category": cat, "type": typ, "primary_color": color,
            "secondary_color": "none", "pattern": "solid", "formality": formality,
            "seasons": list(SEASONS), "layer_role": "auto"}


def _candidate_outfits(items, cand, season=None, pinned=(), layering=None):
    """Good outfits that include the candidate purchase (plus any picked items)."""
    cat, _, typ, color, formality = cand
    fake = _fake(cat, typ, color, formality)
    layers = slot(fake) in ("mid", "outer") or bool(pinned) or bool(layering)
    try:
        outfits = all_outfits(items + [fake], season, layers=layers,
                              pinned=list(pinned) + ["candidate"], layering=layering)
    except ValueError:  # same slot as a picked item
        return []
    return [o for o in outfits if o["score"] >= GOOD_SCORE]


def _owned(items):
    return {(kind(i), i["primary_color"]) for i in items}


def recommendations(items, limit=8):
    by_id = {i["id"]: i for i in items}
    owned = _owned(items)
    baseline = sum(1 for o in all_outfits(items) if o["score"] >= GOOD_SCORE)
    recs = []
    for cand in CANDIDATES:
        cat, name, typ, color, formality = cand
        if (kind(_fake(cat, typ, color, formality)), color) in owned:
            continue
        good = _candidate_outfits(items, cand)
        if not good:
            continue
        partners = Counter(x for o in good for x in outfit_ids(o) if x != "candidate")
        looks = Counter(o["palette"] for o in good)
        recs.append({
            "name": name, "category": cat, "color": color, "new_outfits": len(good),
            "pairs_with": [label(by_id[x]) for x, _ in partners.most_common(3)],
            "looks": [p for p, _ in looks.most_common(2)],
        })
    recs.sort(key=lambda r: r["new_outfits"], reverse=True)
    return {"baseline_good": baseline, "items": recs[:limit]}


def fixes(items, pinned=(), layering=None, season=None, limit=5):
    """When nothing matches the picked item(s) or layering, the purchases that make it work.
    Tries one new piece first, then pairs (e.g. a hoodie AND a jacket for 'hoodie + jacket')."""
    by_id = {i["id"]: i for i in items}
    owned = _owned(items)
    cands = [c for c in CANDIDATES
             if (kind(_fake(c[0], c[2], c[3], c[4])), c[3]) not in owned]

    def attempt(buy):
        fakes = [_fake(c[0], c[2], c[3], c[4], f"candidate{n}") for n, c in enumerate(buy)]
        names = {f["id"]: c[1] for f, c in zip(fakes, buy)}
        try:
            outfits = all_outfits(items + fakes, season, pinned=list(pinned) + list(names),
                                  layering=layering)
        except ValueError:  # same slot as a picked item
            return None
        good = [o for o in outfits if o["score"] >= GOOD_SCORE]
        if not good:
            return None
        best = good[0]
        pieces = [names.get(best[k]) or label(by_id[best[k]]) for k in OUTFIT_KEYS if best.get(k)]
        return {"name": " + ".join(c[1] for c in buy), "category": buy[0][0], "color": buy[0][3],
                "colors": [c[3] for c in buy], "new_outfits": len(good), "best_score": best["score"],
                "example": " + ".join(pieces), "palette": best["palette"]}

    out = [r for r in (attempt([c]) for c in cands) if r]
    if not out:
        for a, b in itertools.combinations(cands, 2):
            fa, fb = _fake(a[0], a[2], a[3], a[4]), _fake(b[0], b[2], b[3], b[4])
            if a[0] == b[0] and slot(fa) == slot(fb):
                continue  # two of the same kind of piece never helps
            r = attempt([a, b])
            if r:
                out.append(r)
    out.sort(key=lambda r: (r["best_score"], r["new_outfits"]), reverse=True)
    return out[:limit]


def gaps(items):
    """Plain-language list of missing pieces and weak spots."""
    out = []
    upper = [i for i in items if i["category"] in ("top", "layer")]
    by_cat = {
        "top": [i for i in upper if layer_role(i) in ("top", "mid")],
        "bottom": [i for i in items if i["category"] == "bottom"],
        "shoes": [i for i in items if i["category"] == "shoes"],
    }
    names = {"top": "tops", "bottom": "bottoms", "shoes": "shoes"}

    if not by_cat["top"] and upper:
        out.append({"level": "high", "text": "You only have jackets/coats: a jacket needs a shirt, tee or hoodie under it.",
                    "fix": "Upload a shirt, t-shirt or hoodie to wear under your jackets."})
    for c in CATEGORIES:
        if not by_cat[c] and not (c == "top" and upper):
            out.append({"level": "high", "text": f"No {names[c]} yet, so no outfits can be built.",
                        "fix": f"Upload your {names[c]}."})
    if out:
        return out

    outfits = all_outfits(items)
    good = [o for o in outfits if o["score"] >= GOOD_SCORE]

    # Items that don't work with anything.
    in_good = {x for o in good for x in outfit_ids(o)}
    for item in items:
        if item["id"] in in_good:
            continue
        best = None
        for cand in CANDIDATES:
            n = len(_candidate_outfits(items, cand, pinned=[item["id"]]))
            if n and (best is None or n > best[1]):
                best = (cand[1], n)
        fix = f"A {best[0].lower()} would make it work." if best else "Consider retiring it."
        out.append({"level": "medium", "item": item["id"],
                    "text": f"Your {label(item)} doesn't match anything you own.", "fix": fix})

    def lacks(cat, test, text, fix):
        if not any(test(i) for i in by_cat[cat]):
            out.append({"level": "high", "text": text, "fix": fix})

    lacks("bottom", lambda i: i["primary_color"] in NEUTRALS,
          "No neutral trousers or jeans to anchor outfits.",
          "Get dark indigo jeans or beige pleated chinos.")
    lacks("top", lambda i: i["primary_color"] in NEUTRALS,
          "No neutral tops (white, cream, navy, gray).",
          "Get a white oxford shirt or a cream knit polo.")
    lacks("shoes", lambda i: i["formality"] >= 3,
          "No smart shoes, so you can't dress up for dinner or the office.",
          "Get brown suede loafers or dark brown chelsea boots.")
    lacks("shoes", lambda i: i["formality"] == 2,
          "No smart-casual sneakers for relaxed days.",
          "Get white minimal leather sneakers.")
    lacks("top", lambda i: i["formality"] >= 3,
          "No smart tops (shirts or fine knits).",
          "Get a white oxford shirt or a navy merino crewneck.")
    if not any(layer_role(i) == "outer" for i in upper):
        out.append({"level": "low", "text": "No jacket or coat to layer over your tops.",
                    "fix": "A navy harrington or olive field jacket works over shirts and hoodies."})
    if not any(i["primary_color"] in FAMILIES["earth"] for i in items):
        out.append({"level": "low", "text": "No earth tones (brown, camel, cream, olive): the most current palette.",
                    "fix": "Add one camel or chocolate-brown piece; it upgrades everything else."})
    if any(i["pattern"] == "graphic" for i in items):
        out.append({"level": "low", "text": "Graphic-print pieces hold back a refined look.",
                    "fix": "Keep them for the gym or weekends; build around plain knits and shirts."})

    for s in SEASONS:
        if not any(s in o["seasons"] for o in good):
            out.append({"level": "medium", "text": f"No good outfits for {s}.",
                        "fix": "Check the season tags, or add a piece suited to that weather."})
    if 0 < len(good) < 5:
        out.append({"level": "medium", "text": f"Only {len(good)} good outfits so far.",
                    "fix": "The top suggestions on this page add the most new outfits."})
    elif not good:
        out.append({"level": "high", "text": "None of your combinations score as a good outfit yet.",
                    "fix": "Start with one neutral bottom and smart shoes from the suggestions."})
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda g: order[g["level"]])


def describe(items_by_id, o):
    return " + ".join(label(items_by_id[o[k]]) for k in OUTFIT_KEYS if o.get(k))


def stylist_prompt(items):
    lines = [f"- {i['category']} ({kind(i)}, worn as {slot(i)}): {label(i)}, {i['pattern']}, "
             f"formality {i['formality']}/5" for i in items]
    picks, _ = lookbook(items, limit=3)
    by_id = {i["id"]: i for i in items}
    best = [describe(by_id, o) for o in picks]
    recs = [r["name"] for r in recommendations(items, limit=4)["items"]]
    issues = [g["text"] for g in gaps(items)][:5]
    return (
        f"You are a menswear stylist. Your client is {PROFILE}.\n"
        f"His skin tone is {SKIN_TONE}; consider which colours near his face are most flattering.\n"
        "Current colour trends: tonal earth tones (chocolate brown, camel, cream), navy with cream, "
        "olive with beige, pink with brown, and a single rich red or burgundy accent on neutrals. "
        "Clean, quiet, well-fitted pieces over logos and graphics. "
        "Jackets and coats are always worn over a shirt, tee or hoodie.\n\n"
        "Wardrobe:\n" + "\n".join(lines) + "\n\n"
        f"Best outfits found: {'; '.join(best) or 'none yet'}\n"
        f"Weak spots: {'; '.join(issues) or 'none'}\n"
        f"Purchases that add the most outfits: {', '.join(recs) or 'none'}\n\n"
        "Give practical advice in under 150 words: 1) the single best thing to buy next and why, "
        "2) one styling tip using clothes he already owns, 3) one thing to stop wearing or replace. "
        "Use short bullet points."
    )


# ---------------------------------------------------------------- http
ID_RE = re.compile(r"^[0-9a-f]{32}$")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep console quiet

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 15 * 1024 * 1024:
            raise ValueError("request too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def path_parts(self):
        url = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        return [p for p in url.path.split("/") if p], query

    def do_GET(self):
        parts, query = self.path_parts()
        season = query.get("season") or None
        if not parts:
            body = INDEX_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parts == ["api", "status"]:
            self.send_json(ollama_status())
        elif parts == ["api", "items"]:
            self.send_json([public_item(i) for i in load_items()])
        elif parts in (["api", "outfits"], ["api", "lookbook"]):
            items = load_items()
            pinned = [p for p in (query.get("pin") or "").split(",") if ID_RE.match(p)]
            layering = query.get("layering") if query.get("layering") in LAYERING_MODES else None
            try:
                limit = max(1, min(500, int(query.get("limit") or 24)))
            except ValueError:
                limit = 24
            try:
                if parts[1] == "outfits":  # every combination, best first
                    every = all_outfits(items, season, pinned=pinned, layering=layering)
                    picks, good = every[:limit], sum(o["score"] >= GOOD_SCORE for o in every)
                    total = len(every)
                else:
                    picks, good = lookbook(items, season, limit, pinned, layering)
                    total = None
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            resp = {"outfits": picks, "good_count": good, "total": total}
            if not good:  # nothing works: say what to buy to make it work
                resp["fixes"] = fixes(items, pinned, layering, season)
            self.send_json(resp)
        elif parts == ["api", "suggestions"]:
            items = load_items()
            self.send_json({"gaps": gaps(items), **recommendations(items)})
        elif parts == ["api", "options"]:
            self.send_json({"colors": COLORS, "patterns": PATTERNS, "seasons": SEASONS,
                            "swatches": {n: "#%02x%02x%02x" % refs[0] for n, refs in COLOR_REFS.items()},
                            "layering": LAYERING_MODES, "layer_roles": LAYER_ROLES})
        elif len(parts) == 2 and parts[0] == "images" and re.fullmatch(r"[0-9a-f]{32}\.jpg", parts[1]):
            file = IMG_DIR / parts[1]
            if not file.exists():
                return self.send_json({"error": "not found"}, 404)
            body = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parts, _ = self.path_parts()
        try:
            data = self.read_json()
        except (ValueError, json.JSONDecodeError) as e:
            return self.send_json({"error": str(e)}, 400)

        if parts == ["api", "items"]:
            category = data.get("category")
            image = (data.get("image") or "").split(",", 1)[-1]  # strip data: prefix
            if category not in ITEM_CATEGORIES or not image:
                return self.send_json({"error": "category and image required"}, 400)
            try:
                raw = base64.b64decode(image, validate=True)
            except ValueError:
                return self.send_json({"error": "bad image"}, 400)
            image_hash = hashlib.sha256(raw).hexdigest()
            with _lock:
                duplicate = find_duplicate_image(image_hash, load_items())
                if duplicate:
                    return self.send_json({"duplicate": True, "existing_item_id": duplicate["id"]})
            item_id = uuid.uuid4().hex
            (IMG_DIR / f"{item_id}.jpg").write_bytes(raw)

            warning = None
            measured = data.get("colors")
            try:
                tags = tag_image(image, category, measured)
                if tags["type"].strip().lower() == category or not tags["description"].strip():
                    warning = "Auto-tags are incomplete. Please review this item's tags."
            except Exception as e:  # Ollama down, model missing, bad JSON...
                tags = normalize_tags({}, category)
                warning = f"Auto-tagging failed ({e}). Please set the tags by hand."
            tags, detected = resolve_colors(tags, measured)
            item = {"id": item_id, "image": f"/images/{item_id}.jpg", "added": int(time.time()),
                    "image_hash": image_hash, "needs_review": warning is not None,
                    "detected_colors": detected, "color_source": "photo" if detected else "model",
                    **tags}
            with _lock:
                items = load_items()
                duplicate = find_duplicate_image(image_hash, items)
                if duplicate:
                    (IMG_DIR / f"{item_id}.jpg").unlink(missing_ok=True)
                    return self.send_json({"duplicate": True, "existing_item_id": duplicate["id"]})
                items.append(item)
                save_items(items)
            return self.send_json({"item": public_item(item), "warning": warning})

        # Re-check an item's colours from its photo (measured in the browser).
        if len(parts) == 4 and parts[:2] == ["api", "items"] and ID_RE.match(parts[2]) \
                and parts[3] == "colors":
            with _lock:
                items = load_items()
                for i, item in enumerate(items):
                    if item["id"] == parts[2]:
                        tags, detected = resolve_colors(item, data.get("colors"))
                        if not detected:
                            return self.send_json({"error": "no colours measured"}, 400)
                        items[i] = {**tags, "detected_colors": detected, "color_source": "photo"}
                        save_items(items)
                        return self.send_json({**public_item(items[i]),
                                               "changed": tags["primary_color"] != item["primary_color"]
                                               or tags["secondary_color"] != item["secondary_color"]})
            return self.send_json({"error": "not found"}, 404)

        if parts == ["api", "explain"]:
            items = {i["id"]: i for i in load_items()}
            try:
                pieces = {k: items[data[k]] for k in OUTFIT_KEYS if data.get(k)}
                if data.get("layer"):  # older page versions
                    layer = items[data["layer"]]
                    pieces[layer_role(layer) if layer_role(layer) == "outer" else "mid"] = layer
                top, bottom, shoes = pieces["top"], pieces["bottom"], pieces["shoes"]
            except KeyError:
                return self.send_json({"error": "unknown item"}, 400)
            mid, outer = pieces.get("mid"), pieces.get("outer")
            o = score_outfit(top, bottom, shoes, mid, outer)
            names = {"top": "base top", "mid": "mid layer", "outer": "outer layer"}
            desc = "; ".join(f"{names.get(k, k)}: {label(pieces[k])} ({pieces[k]['pattern']}, "
                             f"formality {pieces[k]['formality']}/5)"
                             for k in OUTFIT_KEYS if k in pieces)
            prompt = (
                f"You are a menswear stylist advising {PROFILE}.\n"
                f"His skin tone is {SKIN_TONE}; consider whether the top colour complements it.\n"
                f"Outfit: {desc}. Layering: {o['layering']}. Palette: {o['palette']}. "
                f"Occasion: {o['occasion']}.\n"
                "In at most 3 short sentences: why it works (or the one change that would make it "
                "look more refined), and one finishing touch (belt, watch, sock colour, fit or tuck)."
            )
            try:
                return self.send_json({"text": ollama_chat(prompt, timeout=120).strip()})
            except Exception as e:
                return self.send_json({"error": f"Ollama not reachable: {e}"}, 502)

        if parts == ["api", "stylist"]:
            items = load_items()
            if not items:
                return self.send_json({"error": "Upload some clothes first."}, 400)
            try:
                return self.send_json({"text": ollama_chat(stylist_prompt(items), timeout=180).strip()})
            except Exception as e:
                return self.send_json({"error": f"Ollama not reachable: {e}"}, 502)

        self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
        parts, _ = self.path_parts()
        if len(parts) == 3 and parts[:2] == ["api", "items"] and ID_RE.match(parts[2]):
            try:
                data = self.read_json()
            except (ValueError, json.JSONDecodeError) as e:
                return self.send_json({"error": str(e)}, 400)
            with _lock:
                items = load_items()
                for i, item in enumerate(items):
                    if item["id"] == parts[2]:
                        merged = {**item, **data}
                        items[i] = {**item, **normalize_tags(merged, item["category"]),
                                    "needs_review": False, "color_source": "manual"}
                        save_items(items)
                        return self.send_json(public_item(items[i]))
            return self.send_json({"error": "not found"}, 404)
        self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        parts, _ = self.path_parts()
        if len(parts) == 3 and parts[:2] == ["api", "items"] and ID_RE.match(parts[2]):
            with _lock:
                items = [i for i in load_items() if i["id"] != parts[2]]
                save_items(items)
            (IMG_DIR / f"{parts[2]}.jpg").unlink(missing_ok=True)
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    status = ollama_status()
    print(f"Wardrobe AI running at {url}  (model: {MODEL})")
    if not status["ollama"]:
        print("  ! Ollama is not running. Start the Ollama app, or tagging will be manual.")
    elif not status["model_installed"]:
        print(f"  ! Model missing. Run:  ollama pull {MODEL}")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
