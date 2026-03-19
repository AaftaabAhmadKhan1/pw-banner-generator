"""
  DIGITAL GRAPHIC DESIGN GENERATOR - Batch Banner Creator
  Run: python app.py
  Open: http://localhost:5500
"""

import io
import base64
from pathlib import Path

from flask import Flask, render_template, send_from_directory, request, jsonify
from PIL import Image

ROOT = Path(__file__).parent

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
    print("  [WARN] rembg not installed - background removal disabled")


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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/icon.jpg")
def serve_icon():
    return send_from_directory(str(ROOT), "icon.jpg")


@app.route("/outputs/<path:filename>")
def serve_output(filename):
    return send_from_directory(str(ROOT / "outputs"), filename)


@app.route("/api/remove-bg", methods=["POST"])
def remove_bg():
    """Remove background from an uploaded image using rembg."""
    if not HAS_REMBG:
        return jsonify({"error": "rembg is not installed. Run: pip install rembg"}), 500

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
            normalized = neutralize_background_cast(source_image.convert("RGBA"))
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
            cleaned_buffer = io.BytesIO()
            cleaned.save(cleaned_buffer, format="PNG")
            output_bytes = cleaned_buffer.getvalue()

        b64 = base64.b64encode(output_bytes).decode("ascii")
        return jsonify({"status": "ok", "image": f"data:image/png;base64,{b64}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/has-rembg", methods=["GET"])
def has_rembg():
    return jsonify({"available": HAS_REMBG})


if __name__ == "__main__":
    print("\n  ================================================")
    print("    DIGITAL GRAPHIC DESIGN GENERATOR")
    print("    Batch Banner Creator")
    print("  ================================================")
    print("  Open: http://localhost:5500")
    print("  ================================================\n")
    app.run(debug=True, port=5500, host="0.0.0.0")
