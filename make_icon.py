"""Generate VideoAnalyzer app icon — dark rounded square with play triangle + waveform."""
from PIL import Image, ImageDraw, ImageFont
import math

SIZE = 1024
PAD = 100  # corner radius padding

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Rounded rectangle background
radius = 180
draw.rounded_rectangle(
    [(40, 40), (SIZE - 40, SIZE - 40)],
    radius=radius,
    fill=(18, 18, 22),       # near-black
    outline=(42, 42, 50),    # subtle border
    width=3,
)

# Subtle gradient overlay (top-to-bottom purple tint)
overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
odraw = ImageDraw.Draw(overlay)
for y in range(200, SIZE - 40):
    alpha = int(35 * ((y - 200) / (SIZE - 240)))
    odraw.line([(60, y), (SIZE - 60, y)], fill=(109, 90, 205, alpha))
img = Image.alpha_composite(img, overlay)
draw = ImageDraw.Draw(img)

# Play triangle (left side)
cx, cy = 380, 512
tri_size = 160
points = [
    (cx - tri_size * 0.4, cy - tri_size),
    (cx + tri_size * 0.8, cy),
    (cx - tri_size * 0.4, cy + tri_size),
]
draw.polygon(points, fill=(109, 90, 205))

# Audio waveform bars (right side) — representing transcription
bar_x_start = 540
bar_count = 7
bar_width = 28
bar_gap = 16
bar_heights = [60, 110, 80, 140, 95, 120, 55]

for i, h in enumerate(bar_heights):
    x = bar_x_start + i * (bar_width + bar_gap)
    y_top = cy - h
    y_bot = cy + h
    draw.rounded_rectangle(
        [(x, y_top), (x + bar_width, y_bot)],
        radius=bar_width // 2,
        fill=(139, 122, 224),  # lighter purple
    )

# "VT" text at bottom
try:
    font = ImageFont.truetype("/System/Library/Fonts/SFCompact.ttf", 82)
except:
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 82)
    except:
        font = ImageFont.load_default()

bbox = draw.textbbox((0, 0), "VT", font=font)
tw = bbox[2] - bbox[0]
draw.text(
    ((SIZE - tw) / 2, SIZE - 185),
    "VT",
    fill=(161, 161, 170),
    font=font,
)

img.save("/Users/vaultai/PROJECTS/VideoAnalyzer/icon_1024.png")
print("Icon saved: icon_1024.png")
