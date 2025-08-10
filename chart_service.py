# chart_service.py
# FastAPI service: reads rectangles + a point from Google Sheets,
# renders a custom scatter with colored rectangles + wrapped text,
# returns a PNG as base64. Render.com compatible.

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
RECT_RANGE = "'Backend (Qualitative)'!E90:J101"   # x_min,x_max,y_min,y_max,fill_colour,text_content
POINT_X_CELL = "'Qualitative Inputs'!N25"         # X = Health
POINT_Y_CELL = "'Qualitative Inputs'!E4"          # Y = Exit
X_LABEL = "Health"
Y_LABEL = "Exit"
AX_MIN, AX_MAX = 0.0, 10.0
FIG_W, FIG_H = 8, 8
DPI = int(os.getenv("CHART_DPI", "110"))          # 8*110=880px (<1M px)

DEFAULT_KEY_PATH = "/etc/secrets/service_account.json"

app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"ok": True}

def get_sheets_service():
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

# ---- Color normalization (hex, HEX, rgb(...), rgba(...), trims spaces) ----
_rgb_re = re.compile(r"rgba?\(\s*([^)]+)\s*\)", re.IGNORECASE)

def _parse_rgb_like(s: str):
    m = _rgb_re.match(s)
    if not m:
        return None
    parts = [p.strip() for p in m.group(1).split(",")]
    if len(parts) < 3:
        return None
    nums = []
    for i, p in enumerate(parts[:3]):
        if p.endswith("%"):
            v = max(0, min(100, float(p[:-1])))
            nums.append(v / 100.0)
        else:
            v = max(0, min(255, float(p)))
            nums.append(v / 255.0)
    return (*nums, 1.0)  # force alpha=1

def normalize_color(val: str, default="#ffffff") -> str:
    if not val:
        return default
    s = str(val).strip().lower()
    s = re.sub(r"\s+", "", s)
    # rgb()/rgba() support
    rgba = _parse_rgb_like(s)
    if rgba:
        return mcolors.to_hex(rgba, keep_alpha=False)
    # allow bare hex (3 or 6)
    if not s.startswith("#") and len(s) in (3, 6):
        s = "#" + s
    try:
        r, g, b, _ = mcolors.to_rgba(s)  # ignore incoming alpha
        return mcolors.to_hex((r, g, b), keep_alpha=False)
    except ValueError:
        return default

# ---- Text wrapping: left-aligned, but vertically centered in the rect ----
def draw_wrapped_block(ax, text, x_min, y_min, width, height,
                       pad=0.2, max_fontsize=12, min_fontsize=6):
    """
    Wrap to the actual pixel width, left-align, and vertically center inside
    the rectangle. We estimate wrap length from available pixel width and font size.
    """
    avail_w = max(width - 2*pad, 0.1)
    avail_h = max(height - 2*pad, 0.1)

    # left and top anchors in data units
    x_left = x_min + pad
    y_top  = y_min + height - pad

    fig = ax.figure
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()

    # compute available width in pixels
    (px0, py0) = ax.transData.transform((x_left, y_top))
    (px1, _)   = ax.transData.transform((x_left + avail_w, y_top))
    avail_px_w = max(px1 - px0, 1)

    best = None
    for fs in range(max_fontsize, min_fontsize - 1, -1):
        approx_char_px = 0.6 * fs  # heuristic average glyph width in px
        wrap_chars = max(5, int(avail_px_w / max(1, approx_char_px)))
        wrapped = textwrap.fill(text or "", width=wrap_chars)

        t = ax.text(x_left, y_top, wrapped,
                    ha="left", va="top",
                    fontsize=fs, wrap=True, multialignment="left")
        fig.canvas.draw()
        bbox = t.get_window_extent(renderer=renderer).transformed(ax.transData.inverted())
        if bbox.width <= avail_w and bbox.height <= avail_h:
            best = (t, bbox)
            break
        t.remove()

    if best is None:
        # fallback at smallest size
        fs = min_fontsize
        wrap_chars = max(5, int(avail_px_w / max(1, 0.6*fs)))
        wrapped = textwrap.fill(text or "", width=wrap_chars)
        t = ax.text(x_left, y_top, wrapped,
                    ha="left", va="top",
                    fontsize=fs, wrap=True, multialignment="left")
        fig.canvas.draw()
        bbox = t.get_window_extent(renderer=renderer).transformed(ax.transData.inverted())
        best = (t, bbox)

    t, bbox = best
    # vertical centering: push down by half the leftover vertical space
    extra_h = avail_h - bbox.height
    if extra_h > 0:
        t.set_position((x_left, y_top - extra_h/2))

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

        rects = []
        for row in values[1:]:
            if len(row) < 6:
                continue
            try:
                x_min = float(row[0]); x_max = float(row[1])
                y_min = float(row[2]); y_max = float(row[3])
            except Exception:
                continue
            fill = normalize_color(row[4] if len(row) > 4 else "#ffffff", default="#fff2cc")
            text = row[5] if len(row) > 5 else ""
            if x_max <= x_min or y_max <= y_min:
                continue
            rects.append((x_min, x_max, y_min, y_max, fill, text))

        if not rects:
            raise ValueError("No valid rectangle rows after parsing.")

        # Point
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

        # Rectangles (no borders, no anti-alias to avoid seams), then text, then point
        for x_min, x_max, y_min, y_max, fill, text in rects:
            W = x_max - x_min; H = y_max - y_min
            ax.add_patch(Rectangle(
                (x_min, y_min), W, H,
                facecolor=fill, edgecolor="none", linewidth=0.0,
                antialiased=False, zorder=1
            ))
            draw_wrapped_block(ax, text, x_min, y_min, W, H, pad=0.2)

        ax.scatter([x_score], [y_score], s=80, zorder=5)

        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format="png", dpi=DPI)  # keep under Sheets' limits
        plt.close(fig)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("utf-8")
        return JSONResponse({"image_base64": b64})

    except Exception as e:
        detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}"
        raise HTTPException(status_code=500, detail=detail)

# Optional local run
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
