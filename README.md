# Wardrobe AI

Upload photos of your tops (jackets and hoodies included), bottoms and shoes. A local vision model (via Ollama) tags each item,
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

- **Clear labels:** every item gets a fixed garment label (t-shirt, shirt, hoodie, jacket, denim jacket,
  jeans, chinos, trousers, sneakers, loafers…) plus a one-line description from the model.
  Items saved without a label (e.g. just "top") are flagged; *Label them with AI* re-runs the model on them.
- **My clothes:** upload tops, bottoms and shoes; exact duplicate photos are skipped. Put jackets,
  hoodies and coats in *Tops*: each piece's layering role is worked out from its type
  (shirt/tee/polo = base top, hoodie/overshirt/sweater = worn alone or over a shirt,
  jacket/coat/blazer = always worn over a top, never on its own). There is nothing to set: outfits are
  layered automatically.
- **Accurate colours:** the page measures each garment's colours from the photo's pixels
  (background removed, shadows merged) and uses them to check the vision model's colour names.
  The measured colours are shown on each card; *Re-check colours from photos* fixes older items
  (items you saved by hand are left alone).
- **Wear this:** pick one or more pieces you want to wear (button on each card, or the picker on the
  Outfits tab) and get outfits built around them.
- **Match my outfits:** many outfits (24 at a time, *Show more* for more), spread across your clothes,
  each labelled with its layering (e.g. "Shirt + jacket", "Hoodie + jacket", "Shirt + hoodie + jacket"),
  a palette name and occasion, plus an AI styling tip. Each photo is captioned with its garment type.
  Filter by season.
- **When nothing works:** if no good outfit fits the pieces or layering you chose, it lists what to buy
  (one piece, or a pair such as a hoodie and a jacket) to make it work, with an example outfit.
- **Suggestions:** missing pieces (no smart shoes, items that match nothing, season gaps), the purchases
  that unlock the most new outfits, and an AI stylist summary.

## How scoring works

Styling rules (in `style_rules()` in `server.py`, easy to extend):

1. One pattern per outfit: two patterned pieces is a clash.
2. A collar under a patterned layer (e.g. a shirt under a striped sweater) must be plain white/cream
   or one of the layer's own colours, otherwise it's a clash. A layer counts as patterned when it's
   tagged that way or its photo shows clearly different colours, even if the model called it plain.
3. A bold collar colour that doesn't go with a bold layer colour loses points.
4. Shoes should contrast with the trousers (charcoal sneakers with charcoal jeans lose points).
5. A patterned top half wants neutral bottoms.
6. Contrast: two different dark colours side by side (navy + dark brown, navy + black,
   brown + black) blur into one block. With no light piece (white sneakers, a cream layer) to
   break them up the outfit loses a lot of points. Lightness is measured from the photo, so
   navy with *mid* brown or tan is fine.
7. Shoes must tie back to the outfit: white/cream go with anything; other colours must suit the
   trousers (e.g. brown chinos → white, cream, tan, brown) or echo a colour worn up top.
   Gray sneakers under brown chinos lose points.
8. Dress register: a hoodie on its own goes with jeans, joggers or cargos. With chinos or smart
   trousers it lands in an awkward middle (fine under a coat or blazer); loafers with a hoodie
   lose points.

A clash caps the score below 75, so the look is never suggested as a good outfit.

**Colour pairs** come from a men's colour-matching guide (`MATCHES` and `SHOE_MATCHES` in
`server.py`). Being "neutral" is no longer enough: a pair scores full marks only if it is a
recommended combination (navy + beige, olive + cream, brown + light blue…). Neutrals that aren't
listed together score lower, and navy + black or brown + black are marked as hard to pull off.

Each outfit gets 0–100 from colour harmony (35%), how current/chic the palette is (20%),
matching dress level (25%), pattern count (12%) and shared season (8%). With layers, colour
harmony also checks each layer against the piece under it. Graphic prints, gym
trainers with normal clothes, and mixed dress levels lose points. 75+ counts as a good outfit.

The chic palettes follow fall/winter 2026–27 menswear: warm earth tones (chocolate brown, camel,
cream) as the base, earth tones with racing/forest green, navy with cream, charcoal with camel,
olive with beige, brown with light blue, and a single red, burgundy or pink accent on neutrals.
All-dark outfits with no light piece are named "Dark & flat (low contrast)" and score low.
Edit `palette()` and `CANDIDATES` in `server.py` to change them.
