# Wardrobe AI

Upload photos of your tops, bottoms and shoes. A local vision model (via Ollama) tags each item,
simple rules score every outfit combination, and the "What to buy" tab tests common basics against
your wardrobe to show which purchase unlocks the most new outfits.

Everything runs on your PC. Photos stay in the `wardrobe/` folder, which is never committed.

## Run

1. Install [Ollama](https://ollama.com) and pull the model: `ollama pull qwen3-vl:2b`
2. Double-click `start.bat` (or run `python server.py`). The page opens at http://127.0.0.1:8765

No pip packages are needed.

## Settings

| Env var          | Default                  | Notes                                   |
|------------------|--------------------------|-----------------------------------------|
| `WARDROBE_MODEL` | `qwen3-vl:2b`            | Use `qwen3-vl:8b` on a 12 GB+ GPU       |
| `OLLAMA_URL`     | `http://localhost:11434` |                                         |
| `WARDROBE_PORT`  | `8765`                   |                                         |
| `WARDROBE_PROFILE` | 30-year-old man upgrading to a refined style | Who the AI stylist advises |
| `WARDROBE_SKIN_TONE` | `medium tan skin with warm-neutral undertones` | Skin-tone context for styling advice |

## Features

- **My clothes:** upload many tops, bottoms and shoes at once; fix any tag the model gets wrong.
- **Match my outfits:** picks your best outfits, spread across your clothes, each with a palette
  name (e.g. "Tonal earth", "Navy & cream") and occasion, plus an AI styling tip.
- **Suggestions:** missing pieces (no smart shoes, items that match nothing, season gaps), the purchases
  that unlock the most new outfits, and an AI stylist summary.

## How scoring works

Each outfit gets 0–100 from colour harmony (35%), how current/chic the palette is (20%),
matching dress level (25%), pattern count (12%) and shared season (8%). Graphic prints, gym
trainers with normal clothes, and mixed dress levels lose points. 75+ counts as a good outfit.

The chic palettes follow 2026 menswear trends: tonal earth tones (chocolate brown, camel, cream),
navy with cream, charcoal with camel, olive with beige, pink with brown, and a single rich
red/burgundy accent on neutrals. Edit `palette()` and `CANDIDATES` in `server.py` to change them.
