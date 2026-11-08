"""
Wardrobe AI - local outfit helper.

Run:  python server.py
Then the browser opens at http://127.0.0.1:8765

Needs only the Python standard library and a running Ollama with a vision model.
Change the model with the WARDROBE_MODEL environment variable (or edit MODEL below).
"""

import base64
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

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "wardrobe"
IMG_DIR = DATA_DIR / "images"
ITEMS_FILE = DATA_DIR / "items.json"
INDEX_FILE = ROOT / "index.html"
IMG_DIR.mkdir(parents=True, exist_ok=True)

CATEGORIES = ["top", "bottom", "shoes"]
COLORS = [
    "black", "charcoal", "gray", "white", "cream", "beige", "camel", "khaki",
    "brown", "navy", "denim", "blue", "light blue", "olive", "sage", "green",
    "burgundy", "red", "rust", "orange", "pink", "yellow", "purple",
]
PATTERNS = ["solid", "striped", "checked", "floral", "graphic", "other"]
SEASONS = ["spring", "summer", "fall", "winter"]

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


def normalize_tags(raw, category):
    """Force model output (or user edits) into the allowed vocabulary."""
    raw = raw or {}

    def pick(value, allowed, default):
        value = str(value or "").strip().lower()
        if value in allowed:
            return value
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
    return {
        "category": category if category in CATEGORIES else "top",
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


def tag_image(image_b64, category):
    prompt = (
        f"This photo shows one clothing item, a {category}. Describe ONLY that item.\n"
        "Return JSON with:\n"
        "- type: short name, e.g. 't-shirt', 'oxford shirt', 'knit polo', 'jeans', 'chinos', "
        "'pleated trousers', 'sneakers', 'loafers', 'chelsea boots'\n"
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


def score_outfit(top, bottom, shoes):
    outfit = (top, bottom, shoes)
    t, b, s = (i["primary_color"] for i in outfit)
    reasons = []

    harmony = 0.5 * color_pair(t, b) + 0.25 * color_pair(t, s) + 0.25 * color_pair(b, s)
    loud = [c for c in (t, b, s) if c not in NEUTRALS]
    if len(loud) == 3:
        harmony -= 0.2
        reasons.append("three bold colours compete")
    clash = color_pair(t, b) <= 0.15
    if clash:
        reasons.append(f"{t} and {b} clash")

    look, chic = palette([t, b, s])
    # Upscale details: graphic prints and gym shoes pull a look down hard.
    penalty = 0
    if any(i["pattern"] == "graphic" for i in outfit):
        penalty += 15
        reasons.append("graphic prints read young; swap for a plain knit or shirt")
    if shoes["formality"] <= 1 and max(top["formality"], bottom["formality"]) >= 2:
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

    common = set(top["seasons"]) & set(bottom["seasons"]) & set(shoes["seasons"])
    season = 1.0 if common else 0.4
    if not common:
        reasons.append("pieces suit different seasons")

    total = 100 * (0.35 * max(harmony, 0) + 0.20 * max(chic, 0) + 0.25 * formality
                   + 0.12 * pattern + 0.08 * season) - penalty
    if clash:
        total = min(total, GOOD_SCORE - 15)  # a clashing top/bottom is never "good"
    return {
        "score": max(0, round(total)),
        "palette": look,
        "occasion": occasion(sum(forms) / 3),
        "reasons": reasons,
        "seasons": sorted(common),
    }


def all_outfits(items, season=None):
    by_cat = {c: [i for i in items if i["category"] == c] for c in CATEGORIES}
    results = []
    for top, bottom, shoes in itertools.product(by_cat["top"], by_cat["bottom"], by_cat["shoes"]):
        o = score_outfit(top, bottom, shoes)
        if season and season not in o["seasons"]:
            continue
        results.append({"top": top["id"], "bottom": bottom["id"], "shoes": shoes["id"], **o})
    results.sort(key=lambda o: o["score"], reverse=True)
    return results


def lookbook(items, season=None, limit=12, max_uses=2):
    """Pick the best outfits while spreading them across your clothes,
    so you don't get 10 looks that all use the same shirt."""
    pool = [o for o in all_outfits(items, season)
            if o["score"] >= GOOD_SCORE and o["occasion"] != "Lounge / gym"]
    uses = Counter()
    picks = []
    # Pass 1: best outfits, no piece used more than max_uses times.
    for o in pool:
        ids = (o["top"], o["bottom"], o["shoes"])
        if all(uses[i] < max_uses for i in ids):
            picks.append(o)
            uses.update(ids)
            if len(picks) >= limit:
                return picks
    # Pass 2: small wardrobe - fill up with the next best ones.
    for o in pool:
        if o not in picks:
            picks.append(o)
            if len(picks) >= limit:
                break
    return picks


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
    ("top", "Cream linen shirt", "linen shirt", "cream", 3),
    ("bottom", "Dark indigo straight jeans", "jeans", "denim", 2),
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


def _fake(cat, typ, color, formality):
    return {"id": "candidate", "category": cat, "type": typ, "primary_color": color,
            "secondary_color": "none", "pattern": "solid", "formality": formality,
            "seasons": list(SEASONS)}


def recommendations(items, limit=8):
    by_id = {i["id"]: i for i in items}
    owned = {(i["category"], i["primary_color"], i["type"].lower()) for i in items}
    baseline = sum(1 for o in all_outfits(items) if o["score"] >= GOOD_SCORE)
    recs = []
    for cat, name, typ, color, formality in CANDIDATES:
        if (cat, color, typ) in owned:
            continue
        good = [o for o in all_outfits(items + [_fake(cat, typ, color, formality)])
                if o["score"] >= GOOD_SCORE and "candidate" in (o["top"], o["bottom"], o["shoes"])]
        if not good:
            continue
        partners = Counter(x for o in good for x in (o["top"], o["bottom"], o["shoes"]) if x != "candidate")
        looks = Counter(o["palette"] for o in good)
        recs.append({
            "name": name, "category": cat, "color": color, "new_outfits": len(good),
            "pairs_with": [label(by_id[x]) for x, _ in partners.most_common(3)],
            "looks": [p for p, _ in looks.most_common(2)],
        })
    recs.sort(key=lambda r: r["new_outfits"], reverse=True)
    return {"baseline_good": baseline, "items": recs[:limit]}


def gaps(items):
    """Plain-language list of missing pieces and weak spots."""
    out = []
    by_cat = {c: [i for i in items if i["category"] == c] for c in CATEGORIES}
    names = {"top": "tops", "bottom": "bottoms", "shoes": "shoes"}

    for c in CATEGORIES:
        if not by_cat[c]:
            out.append({"level": "high", "text": f"No {names[c]} yet, so no outfits can be built.",
                        "fix": f"Upload your {names[c]}."})
    if out:
        return out

    outfits = all_outfits(items)
    good = [o for o in outfits if o["score"] >= GOOD_SCORE]

    # Items that don't work with anything.
    in_good = {x for o in good for x in (o["top"], o["bottom"], o["shoes"])}
    for item in items:
        if item["id"] in in_good:
            continue
        best = None
        for cat, name, typ, color, formality in CANDIDATES:
            if cat == item["category"]:
                continue
            trial = all_outfits(items + [_fake(cat, typ, color, formality)])
            n = sum(1 for o in trial if o["score"] >= GOOD_SCORE and item["id"] in
                    (o["top"], o["bottom"], o["shoes"]))
            if n and (best is None or n > best[1]):
                best = (name, n)
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


def stylist_prompt(items):
    lines = [f"- {i['category']}: {label(i)}, {i['pattern']}, formality {i['formality']}/5" for i in items]
    picks = lookbook(items, limit=3)
    by_id = {i["id"]: i for i in items}
    best = [" + ".join(label(by_id[o[k]]) for k in ("top", "bottom", "shoes")) for o in picks]
    recs = [r["name"] for r in recommendations(items, limit=4)["items"]]
    issues = [g["text"] for g in gaps(items)][:5]
    return (
        f"You are a menswear stylist. Your client is {PROFILE}.\n"
        "Current colour trends: tonal earth tones (chocolate brown, camel, cream), navy with cream, "
        "olive with beige, pink with brown, and a single rich red or burgundy accent on neutrals. "
        "Clean, quiet, well-fitted pieces over logos and graphics.\n\n"
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
            self.send_json(load_items())
        elif parts == ["api", "outfits"]:
            self.send_json(all_outfits(load_items(), season)[:40])
        elif parts == ["api", "lookbook"]:
            self.send_json(lookbook(load_items(), season))
        elif parts == ["api", "suggestions"]:
            items = load_items()
            self.send_json({"gaps": gaps(items), **recommendations(items)})
        elif parts == ["api", "options"]:
            self.send_json({"colors": COLORS, "patterns": PATTERNS, "seasons": SEASONS})
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
            if category not in CATEGORIES or not image:
                return self.send_json({"error": "category and image required"}, 400)
            try:
                raw = base64.b64decode(image, validate=True)
            except ValueError:
                return self.send_json({"error": "bad image"}, 400)
            item_id = uuid.uuid4().hex
            (IMG_DIR / f"{item_id}.jpg").write_bytes(raw)

            warning = None
            try:
                tags = tag_image(image, category)
            except Exception as e:  # Ollama down, model missing, bad JSON...
                tags = normalize_tags({}, category)
                warning = f"Auto-tagging failed ({e}). Please set the tags by hand."
            item = {"id": item_id, "image": f"/images/{item_id}.jpg", "added": int(time.time()),
                    "needs_review": warning is not None, **tags}
            with _lock:
                items = load_items()
                items.append(item)
                save_items(items)
            return self.send_json({"item": item, "warning": warning})

        if parts == ["api", "explain"]:
            items = {i["id"]: i for i in load_items()}
            try:
                outfit = [items[data[k]] for k in ("top", "bottom", "shoes")]
            except KeyError:
                return self.send_json({"error": "unknown item"}, 400)
            o = score_outfit(*outfit)
            desc = "; ".join(f"{i['category']}: {label(i)} ({i['pattern']}, formality {i['formality']}/5)"
                             for i in outfit)
            prompt = (
                f"You are a menswear stylist advising {PROFILE}.\n"
                f"Outfit: {desc}. Palette: {o['palette']}. Occasion: {o['occasion']}.\n"
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
                                    "needs_review": False}
                        save_items(items)
                        return self.send_json(items[i])
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
