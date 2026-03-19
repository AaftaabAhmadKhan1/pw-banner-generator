"""
  DIGITAL GRAPHIC DESIGN GENERATOR - Batch Banner Creator
  Run: python app.py
  Open: http://localhost:5500
"""

import io
import base64
import os
from collections import deque
from pathlib import Path

from flask import Flask, render_template, send_from_directory, request, jsonify
from PIL import Image, ImageFilter

ROOT = Path(__file__).parent
BG_REMOVE_API_URL = os.getenv("BG_REMOVE_API_URL", "").strip()

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB

# Try to import rembg at startup and create a persistent session
try:
    from rembg import remove as rembg_remove, new_session

    try:
        _rembg_session = new_session("u2net_human_seg")
        print("  [OK] rembg loaded - using u2net_human_seg model")
    except Exception:
        _rembg_session = new_session("u2net")
        print("  [OK] rembg loaded - using default u2net model")
    HAS_REMBG = True
except ImportError:
    HAS_REMBG = False
    _rembg_session = None
    print("  [WARN] rembg not installed - using lightweight fallback remover")


def reduce_green_spill(image: Image.Image) -> Image.Image:
    """Reduce green-screen fringing on semi-transparent edge pixels."""
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size

    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a == 0:
                continue

            if 12 <= a <= 220 and g > r + 12 and g > b + 12:
                target = max(r, b)
                excess = g - target
                new_g = max(target, g - int(excess * 0.85))
                new_r = min(255, r + int(excess * 0.18))
                new_b = min(255, b + int(excess * 0.12))
                pixels[x, y] = (new_r, new_g, new_b, a)
            elif a < 80 and g > r + 18 and g > b + 18:
                pixels[x, y] = (r, max(r, b), b, max(0, a - 18))

    return rgba


def neutralize_background_cast(image: Image.Image) -> Image.Image:
    """Pre-neutralize likely chroma background so hair edges carry less color spill."""
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size

    sample_points = [
        (0, 0),
        (width - 1, 0),
        (0, height - 1),
        (width - 1, height - 1),
        (width // 2, 0),
        (width // 2, height - 1),
        (0, height // 2),
        (width - 1, height // 2),
    ]
    samples = [pixels[x, y] for x, y in sample_points if width > 0 and height > 0]
    if not samples:
        return rgba

    avg_r = sum(p[0] for p in samples) / len(samples)
    avg_g = sum(p[1] for p in samples) / len(samples)
    avg_b = sum(p[2] for p in samples) / len(samples)
    bg_green_dominant = avg_g > avg_r + 20 and avg_g > avg_b + 20

    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a == 0:
                continue

            if bg_green_dominant:
                green_excess = g - max(r, b)
                if green_excess > 24:
                    proximity = min(
                        abs(r - avg_r) + abs(g - avg_g) + abs(b - avg_b),
                        255 * 3,
                    )
                    # Strongest neutralization on background-like and semi-edge pixels.
                    match_strength = max(0.0, 1.0 - (proximity / 220.0))
                    edge_strength = 1.0 if a < 220 else 0.6
                    neutral_strength = max(match_strength, 0.35) * edge_strength
                    neutral = int((r + b) / 2)
                    new_g = int(g - green_excess * 0.9 * neutral_strength)
                    new_r = int(r + green_excess * 0.22 * neutral_strength)
                    new_b = int(b + green_excess * 0.16 * neutral_strength)
                    pixels[x, y] = (
                        min(255, new_r),
                        max(neutral, min(255, new_g)),
                        min(255, new_b),
                        a,
                    )

    return rgba


def _rgb_distance_sq(a, b):
    dr = a[0] - b[0]
    dg = a[1] - b[1]
    db = a[2] - b[2]
    return dr * dr + dg * dg + db * db


def _estimate_border_color(image: Image.Image):
    rgb = image.convert("RGB")
    width, height = rgb.size
    border = max(4, min(width, height) // 24)
    samples = []
    pixels = rgb.load()

    for x in range(width):
        for y in range(border):
            samples.append(pixels[x, y])
            samples.append(pixels[x, height - 1 - y])
    for y in range(border, height - border):
        for x in range(border):
            samples.append(pixels[x, y])
            samples.append(pixels[width - 1 - x, y])

    if not samples:
        return (255, 255, 255), False

    avg = tuple(int(sum(c[i] for c in samples) / len(samples)) for i in range(3))
    green_dominant = avg[1] > avg[0] + 18 and avg[1] > avg[2] + 18
    return avg, green_dominant


def fallback_remove_background(image: Image.Image) -> Image.Image:
    """Lightweight border-aware background remover for Vercel-sized deploys."""
    rgba = neutralize_background_cast(image.convert("RGBA"))
    original_size = rgba.size

    working = rgba.copy()
    max_dim = max(working.size)
    if max_dim > 900:
        scale = 900 / max_dim
        working = working.resize(
            (max(1, int(working.width * scale)), max(1, int(working.height * scale))),
            Image.Resampling.LANCZOS,
        )

    rgb = working.convert("RGB")
    width, height = rgb.size
    bg_color, green_dominant = _estimate_border_color(rgb)
    pixels = rgb.load()

    seed_threshold = 70 ** 2
    grow_threshold = 92 ** 2
    soft_threshold = 118 ** 2
    visited = bytearray(width * height)
    queue = deque()

    def idx(x, y):
        return y * width + x

    def maybe_seed(x, y):
        color = pixels[x, y]
        dist = _rgb_distance_sq(color, bg_color)
        is_greenish = color[1] > color[0] + 18 and color[1] > color[2] + 18
        if dist <= seed_threshold or (green_dominant and is_greenish):
            pos = idx(x, y)
            if not visited[pos]:
                visited[pos] = 1
                queue.append((x, y))

    for x in range(width):
        maybe_seed(x, 0)
        maybe_seed(x, height - 1)
    for y in range(height):
        maybe_seed(0, y)
        maybe_seed(width - 1, y)

    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            pos = idx(nx, ny)
            if visited[pos]:
                continue
            color = pixels[nx, ny]
            dist = _rgb_distance_sq(color, bg_color)
            is_greenish = color[1] > color[0] + 14 and color[1] > color[2] + 14
            if dist <= grow_threshold or (green_dominant and is_greenish and dist <= soft_threshold):
                visited[pos] = 1
                queue.append((nx, ny))

    alpha = Image.new("L", (width, height), 255)
    alpha_pixels = alpha.load()
    for y in range(height):
        for x in range(width):
            pos = idx(x, y)
            color = pixels[x, y]
            dist = _rgb_distance_sq(color, bg_color)
            if visited[pos]:
                a = 0
            elif dist <= soft_threshold:
                a = int(max(0, min(255, ((dist - grow_threshold) / max(1, soft_threshold - grow_threshold)) * 255)))
            else:
                a = 255
            alpha_pixels[x, y] = a

    alpha = alpha.filter(ImageFilter.GaussianBlur(radius=1.6))
    if alpha.size != original_size:
        alpha = alpha.resize(original_size, Image.Resampling.LANCZOS)

    result = rgba.copy()
    result.putalpha(alpha)
    return reduce_green_spill(result)


@app.route("/")
def index():
    return render_template("index.html", bg_remove_api_url=BG_REMOVE_API_URL)


@app.route("/icon.jpg")
def serve_icon():
    return send_from_directory(str(ROOT), "icon.jpg")


@app.route("/outputs/<path:filename>")
def serve_output(filename):
    return send_from_directory(str(ROOT / "outputs"), filename)


@app.route("/api/remove-bg", methods=["POST"])
def remove_bg():
    """Remove background from an uploaded image."""

    if "file" not in request.files:
        data = request.get_json(silent=True)
        if data and "image" in data:
            img_data = data["image"]
            if "," in img_data:
                img_data = img_data.split(",", 1)[1]
            input_bytes = base64.b64decode(img_data)
        else:
            return jsonify({"error": "No image provided"}), 400
    else:
        input_bytes = request.files["file"].read()

    try:
        with Image.open(io.BytesIO(input_bytes)) as source_image:
            source_rgba = source_image.convert("RGBA")

            if HAS_REMBG:
                normalized = neutralize_background_cast(source_rgba)
                buffer = io.BytesIO()
                normalized.save(buffer, format="PNG")
                prepared_bytes = buffer.getvalue()

                output_bytes = rembg_remove(
                    prepared_bytes,
                    session=_rembg_session,
                    alpha_matting=True,
                    alpha_matting_foreground_threshold=250,
                    alpha_matting_background_threshold=4,
                    alpha_matting_erode_size=12,
                    post_process_mask=True,
                )

                with Image.open(io.BytesIO(output_bytes)) as cutout_image:
                    cleaned = reduce_green_spill(cutout_image)
            else:
                cleaned = fallback_remove_background(source_rgba)

            cleaned_buffer = io.BytesIO()
            cleaned.save(cleaned_buffer, format="PNG")
            output_bytes = cleaned_buffer.getvalue()

        b64 = base64.b64encode(output_bytes).decode("ascii")
        return jsonify({"status": "ok", "image": f"data:image/png;base64,{b64}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/has-rembg", methods=["GET"])
def has_rembg():
    engine = "rembg" if HAS_REMBG else "fallback"
    return jsonify({"available": True, "engine": engine})


if __name__ == "__main__":
    print("\n  ================================================")
    print("    DIGITAL GRAPHIC DESIGN GENERATOR")
    print("    Batch Banner Creator")
    print("  ================================================")
    print("  Open: http://localhost:5500")
    print("  ================================================\n")
    app.run(debug=True, port=5500, host="0.0.0.0")
