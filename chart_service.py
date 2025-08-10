# chart_service.py
# FastAPI service: reads rectangles + a point from Google Sheets,
# renders a custom scatter with colored rectangles + wrapped text,
# returns a PNG as base64. Render.com compatible (no Dockerfile needed).

import matplotlib
matplotlib.use("Agg")  # headless for server

import os, io, base64, textwrap, traceback, re
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib import colors as mcolors
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ===== CONFIG =====
SHEET_ID = "1NZ1KSX3gn6XRcWwLY7BRhLDXLVtVIFxdhjAsk-9Fy_A"
RECT_RANGE = "'Backend (Qualitative)'!E90:J101"   # x_min,x_max,y_min,y_max,fill_colour,text_content (header at row 90)
POINT_X_CELL = "'Qualitative Inputs'!N25"         # X = Health
POINT_Y_CELL = "'Qualitative Inputs'!E4"          # Y = Exit
X_LABEL = "Health"
Y_LABEL = "Exit"
AX_MIN, AX_MAX = 0.0, 10.0

# Keep image under Sheets limits (≤ 1M px, ≤ 2MB)
FIG_W, FIG_H = 8, 8          # inches
DPI = int(os.getenv("CHART_DPI", "110"))  # 8*110 = 880px (≈774k px)

# Fixed, readable font across all rectangles
RECT_TEXT_FONT_SIZE = int(os.getenv("RECT_FONT_SIZE", "12"))
RECT_TEXT_PADDING = float(os.getenv("RECT_TEXT_PAD", "0.2"))  # axis units padding inside rect

# Render Secret Files path for service account JSON
DEFAULT_KEY_PATH = "/etc/secrets/service_account.json"

app = FastAPI()

# ---------- helpers ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}

def get_sheets_service():
    """Initialize Google Sheets API client using a service-account key."""
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", DEFAULT_KEY_PATH)
    if not os.path.exists(key_path):
        raise FileNotFoundError(
            f"Service account key not found at '{key_path}'. "
            "On Render, add a Secret File named service_account.json."
        )
    creds = service_account.Credentials.from_service_account_file(
        key_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return build("sheets", "v4", credentials=creds)

# --- Color normalization (hex/HEX with spaces, rgb(...)/rgba(...), force opaque) ---
_rgb_re = re.compile(r"rgba?\(\s*([^)]+)\s*\)", re.IGNORECASE)

def _parse_rgb_like(s: str):
    m = _rgb_re.match(s)
    if not m:
        return None
    parts = [p.strip() for p in m.group(1).split(",")]
    if len(parts) < 3:
        return None
    nums = []
    for p in parts[:3]:
        if p.endswith("%"):
            v = max(0, min(100, float(p[:-1])))
            nums.append(v / 100.0)
        else:
            v = max(0, min(255, float(p)))
            nums.append(v / 255.0)
    return (*nums, 1.0)  # force alpha = 1

def normalize_color(val: str, default="#ffffff") -> str:
    if not val:
        return default
    s = str(val).strip().lower()
    s = re.sub(r"\s+", "", s)
    rgba = _parse_rgb_like(s)
    if rgba:
        return mcolors.to_hex(rgba, keep_alpha=False)
    if not s.startswith("#") and len(s) in (3, 6):
        s = "#" + s
    try:
        r, g, b, _ = mcolors.to_rgba(s)
        return mcolors.to_hex((r, g, b), keep_alpha=False)
    except ValueError:
        return default

# --- Fixed-size, left-aligned, wrapped text; vertically centered & clipped to rect ---
def draw_wrapped_block_fixed(ax, text, x_min, y_min, width, height,
                             rect_patch, pad=RECT_TEXT_PADDING, font_size=RECT_TEXT_FONT_SIZE):
    """
    Left-align text, fixed font size, wrap by available pixel width,
    vertically center inside rectangle, clip overflow to rect.
    """
    avail_w = max(width - 2 * pad, 0.1)
    avail_h = max(height - 2 * pad, 0.1)
    x_left = x_min + pad
    y_top  = y_min + height - pad

    # estimate wrap count from pixel width and font size
    ax.figure.canvas.draw()
    r = ax.figure.canvas.get_renderer()
    (px0, _) = ax.transData.transform((x_left, y_top))
    (px1, _) = ax.transData.transform((x_left + avail_w, y_top))
    avail_px_w = max(px1 - px0, 1)

    approx_char_px = max(1.0, 0.6 * font_size)  # heuristic glyph width
    wrap_chars = max(5, int(avail_px_w / approx_char_px))
    wrapped = textwrap.fill(text or "", width=wrap_chars)

    t = ax.text(x_left, y_top, wrapped,
                ha="left", va="top",
                fontsize=font_size, wrap=True, multialignment="left",
                clip_on=True)
    # Clip strictly inside the rectangle
    t.set_clip_path(rect_patch)

    # vertically center
    ax.figure.canvas.draw()
    bbox = t.get_window_extent(renderer=r).transformed(ax.transData.inverted())
    extra_h = avail_h - bbox.height
    if extra_h > 0:
        t.set_position((x_left, y_top - extra_h / 2))

# ---------- main endpoint ----------

@app.get("/chart")
def chart():
    try:
        svc = get_sheets_service()

        # Rectangles
        resp = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=RECT_RANGE
        ).execute()
        values = resp.get("values", [])
        if not values or len(values) < 2:
            raise ValueError(f"No rectangle data found in {RECT_RANGE}")

        # Expect header: x_min,x_max,y_min,y_max,fill_colour,text_content
        rects = []
        for row in values[1:]:
            if len(row) < 6:
                continue
            try:
                x_min = float(row[0]); x_max = float(row[1])
                y_min = float(row[2]); y_max = float(row[3])
            except Exception:
                continue
            if x_max <= x_min or y_max <= y_min:
                continue
            fill_raw = row[4] if len(row) > 4 else "#ffffff"
            fill = normalize_color(fill_raw, default="#fff2cc")  # default pastel
            text = row[5] if len(row) > 5 else ""
            rects.append((x_min, x_max, y_min, y_max, fill, text))

        if not rects:
            raise ValueError("No valid rectangle rows after parsing.")

        # Point (X from N25, Y from E4)
        x_raw = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=POINT_X_CELL
        ).execute().get("values", [["0"]])[0][0]
        y_raw = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=POINT_Y_CELL
        ).execute().get("values", [["0"]])[0][0]
        x_score = float(x_raw); y_score = float(y_raw)

        # Plot
        fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
        ax.set_xlim(AX_MIN, AX_MAX)
        ax.set_ylim(AX_MIN, AX_MAX)
        ax.set_xlabel(X_LABEL, fontsize=12)
        ax.set_ylabel(Y_LABEL, fontsize=12)

        # Rectangles (no borders, antialias off to avoid seams); then text; then point
        for x_min, x_max, y_min, y_max, fill, text in rects:
            W = x_max - x_min; H = y_max - y_min
            patch = Rectangle(
                (x_min, y_min), W, H,
                facecolor=fill,
                edgecolor="none",
                linewidth=0.0,
                antialiased=False,
                zorder=1
            )
            ax.add_patch(patch)
            # fixed-size, left-aligned, wrapped, vertically centered, clipped
            draw_wrapped_block_fixed(ax, text, x_min, y_min, W, H, patch)

        ax.scatter([x_score], [y_score], s=80, zorder=5)

        # Export → base64
        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format="png", dpi=DPI)  # keep within Sheets limits
        plt.close(fig)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("utf-8")
        return JSONResponse({"image_base64": b64})

    except Exception as e:
        detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}"
        raise HTTPException(status_code=500, detail=detail)

# Optional local debug
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
