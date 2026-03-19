import base64
import io
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS
from PIL import Image
from rembg import new_session, remove as rembg_remove


ROOT = Path(__file__).parent
app = Flask(__name__)
CORS(app)

try:
    REMBG_SESSION = new_session("u2net_human_seg")
except Exception:
    REMBG_SESSION = new_session("u2net")


def reduce_green_spill(image: Image.Image) -> Image.Image:
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


@app.get("/")
def root():
    return jsonify({"status": "ok", "service": "pw-banner-bg-removal"})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "engine": "rembg"})


@app.post("/api/remove-bg")
def remove_bg():
    if "file" in request.files:
        input_bytes = request.files["file"].read()
    else:
        data = request.get_json(silent=True) or {}
        img_data = data.get("image")
        if not img_data:
            return jsonify({"error": "No image provided"}), 400
        if "," in img_data:
            img_data = img_data.split(",", 1)[1]
        input_bytes = base64.b64decode(img_data)

    try:
        with Image.open(io.BytesIO(input_bytes)) as source_image:
            normalized = neutralize_background_cast(source_image.convert("RGBA"))
            prepared = io.BytesIO()
            normalized.save(prepared, format="PNG")

        output_bytes = rembg_remove(
            prepared.getvalue(),
            session=REMBG_SESSION,
            alpha_matting=True,
            alpha_matting_foreground_threshold=250,
            alpha_matting_background_threshold=4,
            alpha_matting_erode_size=12,
            post_process_mask=True,
        )

        with Image.open(io.BytesIO(output_bytes)) as cutout_image:
            cleaned = reduce_green_spill(cutout_image)
            final_buffer = io.BytesIO()
            cleaned.save(final_buffer, format="PNG")

        image_b64 = base64.b64encode(final_buffer.getvalue()).decode("ascii")
        return jsonify({"status": "ok", "image": f"data:image/png;base64,{image_b64}"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860)
