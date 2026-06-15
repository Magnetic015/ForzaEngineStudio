"""AI image-preprocess sidecar — edit the selected local image via a third-party
image model on an OpenAI-compatible gateway, save the result next to the source.

The gateway exposes image models through TWO different shapes, so this sidecar
routes per model:

  * **images-edit models** (gpt-image-2 / plus/gpt-image-2): OpenAI-compatible
    multipart `POST {base}/images/edits`; the edited image comes back as
    `data[0].b64_json`.

  * **chat-image models** (gemini-*-image, grok-imagine-*): the model is a
    chat-completions model that emits an image. We `POST {base}/chat/completions`
    with the source image inlined as a data URL; the result comes back in
    `choices[0].message.images[0].image_url.url` (a data URL — gemini returns
    JPEG), with markdown/data-url-in-content as fallbacks.

Either way the returned bytes are normalized to a real PNG (via Pillow) and
written next to the source. Exactly one JSON line is printed on stdout:
  {"type":"done","path":"<edited image path>","revised_prompt":"...","model":"..."}
  {"type":"error","message":"..."}

Invocation:
  python image_process.py --image PATH --api-key KEY --prompt TEXT [--model plus/gpt-image-2] [--base-url ...] [--out PATH]
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
from pathlib import Path

import requests
from PIL import Image

# Windows pipes default to the locale code page (e.g. GBK), which corrupts
# non-ASCII paths (Chinese filenames) when Rust decodes stdout as UTF-8.
# Force UTF-8 so the emitted JSON survives the round-trip intact.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def uses_images_endpoint(model: str) -> bool:
    """True for OpenAI-style image-edit models served on /images/edits.

    Everything else (gemini-*-image, grok-imagine-*, …) is a chat-completions
    image model and goes through /chat/completions instead.
    """
    m = (model or "").lower()
    return "gpt-image" in m or "dall-e" in m


def load_source_png_bytes(src: Path) -> bytes:
    """Open the user's image and re-encode to clean PNG bytes.

    Normalizes arbitrary input (jpg/webp/bmp/gif/…) to PNG so the upload's
    declared image/png type is always truthful and the data URL we inline for
    chat models is a real PNG. Keeps an alpha channel if present.
    """
    img = Image.open(src)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.mode or img.mode == "P" else "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def save_png(raw: bytes, out_path: Path) -> None:
    """Decode arbitrary image bytes (PNG/JPEG/…) and write a normalized PNG."""
    img = Image.open(io.BytesIO(raw))
    img.save(out_path, format="PNG")


# ── route 1: OpenAI-style images/edits (gpt-image-2) ─────────────────────────

def run_images_edit(base: str, key: str, model: str, prompt: str,
                    src: Path, png_bytes: bytes) -> tuple[bytes, str]:
    """Return (image_bytes, revised_prompt). Raises RuntimeError on failure."""
    url = base.rstrip("/") + "/images/edits"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}"},
        files={"image": (src.stem + ".png", io.BytesIO(png_bytes), "image/png")},
        data={"model": model, "prompt": prompt},
        timeout=300,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        item = resp.json()["data"][0]
        raw = base64.b64decode(item["b64_json"])
        return raw, item.get("revised_prompt", "")
    except Exception as exc:
        raise RuntimeError(f"unexpected response: {type(exc).__name__}: {exc}") from exc


# ── route 2: chat-completions image models (gemini / grok) ───────────────────

_DATAURL_RE = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+")
_MARKDOWN_IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")


def _extract_image_url(message: dict) -> str | None:
    """Pull an image URL (data: or http(s):) out of a chat message.

    Order: the `images` array (gemini/grok nano-banana shape), then a data URL
    or markdown image embedded in `content` (string or multi-part list).
    """
    for im in (message.get("images") or []):
        if isinstance(im, dict):
            u = (im.get("image_url") or {}).get("url") or im.get("url")
            if u:
                return u
    content = message.get("content")
    if isinstance(content, str) and content:
        m = _DATAURL_RE.search(content)
        if m:
            return m.group(0).replace("\n", "").replace(" ", "")
        m = _MARKDOWN_IMG_RE.search(content)
        if m:
            return m.group(1)
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("image_url", "output_image", "image"):
                u = (part.get("image_url") or {}).get("url") or part.get("url")
                if u:
                    return u
    return None


def _url_to_bytes(url: str, key: str) -> bytes:
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    r = requests.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=120)
    r.raise_for_status()
    return r.content


def run_chat_image(base: str, key: str, model: str, prompt: str,
                   png_bytes: bytes) -> tuple[bytes, str]:
    """Return (image_bytes, revised_prompt=""). Raises RuntimeError on failure."""
    url = base.rstrip("/") + "/chat/completions"
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=300,
    )
    if resp.status_code != 200:
        # Surface the gateway's message verbatim — e.g. grok's 503
        # "无可用渠道（distributor）" means the model has no upstream channel
        # right now, which is a gateway-side availability issue, not a bug here.
        detail = resp.text[:300]
        try:
            detail = resp.json().get("error", {}).get("message", detail) or detail
        except Exception:
            pass
        raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
    try:
        message = resp.json()["choices"][0]["message"]
    except Exception as exc:
        raise RuntimeError(f"unexpected response: {type(exc).__name__}: {exc}") from exc
    img_url = _extract_image_url(message)
    if not img_url:
        text = message.get("content")
        hint = text[:200] if isinstance(text, str) else "no image in response"
        raise RuntimeError(f"model returned no image ({hint})")
    return _url_to_bytes(img_url, key), ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--model", default="plus/gpt-image-2")
    ap.add_argument("--base-url", default="https://your-gateway.example/v1")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    src = Path(args.image)
    if not src.exists():
        emit({"type": "error", "message": f"image not found: {src}"})
        return 1

    try:
        png_bytes = load_source_png_bytes(src)
    except Exception as exc:
        emit({"type": "error", "message": f"cannot read image: {type(exc).__name__}: {exc}"})
        return 1

    try:
        if uses_images_endpoint(args.model):
            raw, revised = run_images_edit(args.base_url, args.api_key, args.model,
                                           args.prompt, src, png_bytes)
        else:
            raw, revised = run_chat_image(args.base_url, args.api_key, args.model,
                                          args.prompt, png_bytes)
    except requests.exceptions.RequestException as exc:
        emit({"type": "error", "message": f"request failed: {type(exc).__name__}: {exc}"})
        return 1
    except RuntimeError as exc:
        emit({"type": "error", "message": str(exc)})
        return 1
    except Exception as exc:
        emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        return 1

    out_path = Path(args.out) if args.out else src.with_name(src.stem + "_ai.png")
    try:
        save_png(raw, out_path)
    except Exception as exc:
        emit({"type": "error", "message": f"save failed: {type(exc).__name__}: {exc}"})
        return 1

    emit({"type": "done", "path": str(out_path), "revised_prompt": revised, "model": args.model})
    return 0


if __name__ == "__main__":
    sys.exit(main())
