# chart_service.py
# FastAPI service: reads rectangles + a point from Google Sheets,
# renders a custom scatter with colored rectangles + wrapped text,
# returns a PNG as base64. Ready for Render.com (no Dockerfile needed).

import matplotlib
matplotlib.use("Agg")  # headless backend for servers

import os, io, base64, textwrap, traceback, re
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib import colors as mcolors
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ========= CONFIG =========
SHEET_ID = "1NZ1KSX3gn6XRcWwLY7BRhLDXLVtVIFxdhjAsk-9Fy_A"
RECT_RANGE = "'Backend (Qualitative)'!E90:J101"   # header row at 90: x_min,x_max,y_min,y_max,fill_colour,text_content
POINT_X_CELL = "'Qualitative Inputs'!N25"         # X = Health
POINT_Y_CELL = "'Qualitative Inputs'!E4"          # Y = Exit

X_LABEL = "Health"
Y_LABEL = "Exit"
AX_MIN, AX_MAX = 0.0, 10.0
FIG_W, FIG_H = 8, 8             # inches
DPI = int(os.getenv("CHART_DPI", "110"))  # 8in * 110dpi = 880px (<=1M pixels, <=2MB)

DEFAULT_KEY_PATH = "/etc/secrets/service_account.json"

app = FastAPI()


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


def normalize_color(val: str, default="#ffffff") -> str:
    """Normalize Sheet color strings to canonical #rrggbb; tolerate spaces/3- or 6-hex, case, missing '#'. """
    if not val:
        return default
    s = str(val).strip().lower()
    s = re.sub(r"\s+", "", s)      # remove spaces/newlines
    if not s.startswith("#") and len(s) in (3, 6):
        s = "#" + s
    try:
        return mcolors.to_hex(mcolors.to_rgba(s))  # -> '#rrggbb'
    except ValueError:
        return default


def draw_wrapped_left(ax, text, x_min, y_max, width, height,
                      pad=0.2, max_fontsize=12, min_fontsize=6):
    """
    Left-align wrapped text inside the rectangle with padding.
    Shrinks font size until it fits within (width - 2*pad, height - 2*pad).
    """
    avail_w = max(width - 2 * pad, 0.1)
    avail_h = max(height - 2 * pad, 0.1)
    x = x_min + pad      # left padding
    y = y_max - pad      # top padding (using y_max as top)

    # try larger font sizes first, then shrink
    for fs in range(max_fontsize, min_fontsize - 1, -1):
        wrap_chars = max(10, int(avail_w * 4))  # heuristic: 4 chars per axis unit
        wrapped = textwrap.fill(text or "", width=wrap_chars)
        t = ax.text(x, y, wrapped,
                    ha="left", va="top",
                    fontsize=fs, wrap=True, multialignment="left")
        ax.figure.canvas.draw()
        rend = ax.figure.canvas.get_renderer()
        bbox = t.get_window_extent(renderer=rend).transformed(ax.transData.inverted())
        if bbox.width <= avail_w and bbox.height <= avail_h:
            return  # it fits; keep the text
        t.remove()

    # fallback: smallest font, clipped if needed
    wrap_chars = max(10, int(avail_w * 4))
    ax.text(x, y, textwrap.fill(text or "", width=wrap_chars),
            ha="left", va="top", fontsize=min_fontsize, wrap=True, multialignment="left")


@app.get("/chart")
def chart():
    try:
        svc = get_sheets_service()

        # ---- Rectangles ----
        resp = svc.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=RECT_RANGE).execute()
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
            fill_raw = row[4] if len(row) > 4 else "#ffffff"
            fill = normalize_color(fill_raw, default="#fff2cc")  # default pastel
            text = row[5] if len(row) > 5 else ""
            if x_max <= x_min or y_max <= y_min:
                continue
            rects.append((x_min, x_max, y_min, y_max, fill, text))

        if not rects:
            raise ValueError("No valid rectangle rows after parsing.")

        # ---- Point (X from N25, Y from E4) ----
        x_raw = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=POINT_X_CELL
        ).execute().get("values", [["0"]])[0][0]
        y_raw = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=POINT_Y_CELL
        ).execute().get("values", [["0"]])[0][0]
        x_score = float(x_raw); y_score = float(y_raw)

        # ---- Plot ----
        fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
        ax.set_xlim(AX_MIN, AX_MAX)
        ax.set_ylim(AX_MIN, AX_MAX)
        ax.set_xlabel(X_LABEL, fontsize=12)
        ax.set_ylabel(Y_LABEL, fontsize=12)

        # Draw rectangles first (no borders), then text, then point
        for x_min, x_max, y_min, y_max, fill, text in rects:
            W = x_max - x_min; H = y_max - y_min
            ax.add_patch(Rectangle(
                (x_min, y_min), W, H,
                facecolor=fill, edgecolor="none", linewidth=0.0, zorder=1
            ))
            # text: left-aligned, wrapped, padded; start from top-left (y_max)
            draw_wrapped_left(ax, text, x_min, y_max, W, H, pad=0.2)

        # Scatter point on top
        ax.scatter([x_score], [y_score], s=80, zorder=5)

        # Export as PNG → base64
        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format="png", dpi=DPI)
        plt.close(fig)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("utf-8")
        return JSONResponse({"image_base64": b64})

    except Exception as e:
        detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}"
        raise HTTPException(status_code=500, detail=detail)


# Optional: local debug
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)

