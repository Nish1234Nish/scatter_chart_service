# chart_service.py
# FastAPI service that reads rectangles + a point from Google Sheets,
# renders a custom scatter with colored rectangles + wrapped labels,
# and returns a PNG as base64. Render.com compatible (no Dockerfile needed).

import matplotlib
matplotlib.use("Agg")  # headless backend for servers

import os, io, base64, textwrap, traceback
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ==== CONFIG ====
SHEET_ID = "1NZ1KSX3gn6XRcWwLY7BRhLDXLVtVIFxdhjAsk-9Fy_A"
RECT_RANGE = "'Backend (Qualitative)'!E90:J101"   # header row in 90
POINT_X_CELL = "'Qualitative Inputs'!N25"         # X = Health
POINT_Y_CELL = "'Qualitative Inputs'!E4"          # Y = Exit
X_LABEL = "Health"
Y_LABEL = "Exit"
AX_MIN, AX_MAX = 0.0, 10.0

# Render Secret Files live here:
DEFAULT_KEY_PATH = "/etc/secrets/service_account.json"

app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"ok": True}

def get_sheets_service():
    """Init Google Sheets API client using a service-account key."""
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

def fit_text_to_rect(ax, text, x_center, y_center, width, height,
                     max_fontsize=12, min_fontsize=6):
    """Wrap & scale text so it fits inside a rectangle (axis units)."""
    safe_w = max(width, 0.1)
    safe_h = max(height, 0.1)
    for fontsize in range(max_fontsize, min_fontsize - 1, -1):
        wrap_chars = max(10, int(safe_w * 4))
        wrapped = textwrap.fill(text or "", width=wrap_chars)
        t = ax.text(x_center, y_center, wrapped,
                    ha="center", va="center",
                    fontsize=fontsize, wrap=True)
        ax.figure.canvas.draw()
        rend = ax.figure.canvas.get_renderer()
        bbox = t.get_window_extent(renderer=rend).transformed(ax.transData.inverted())
        if bbox.width <= safe_w and bbox.height <= safe_h:
            return
        t.remove()
    # fallback (smallest size)
    ax.text(x_center, y_center, textwrap.fill(text or "", width=wrap_chars),
            ha="center", va="center", fontsize=min_fontsize, wrap=True)

@app.get("/chart")
def chart():
    try:
        svc = get_sheets_service()

        # --- Rectangles ---
        rect_vals = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=RECT_RANGE
        ).execute().get("values", [])
        if not rect_vals or len(rect_vals) < 2:
            raise ValueError(f"No rectangle data found in {RECT_RANGE}")

        rects = []
        for row in rect_vals[1:]:
            if len(row) < 6:
                continue
            try:
                x_min = float(row[0]); x_max = float(row[1])
                y_min = float(row[2]); y_max = float(row[3])
            except Exception:
                continue
            fill = (row[4] or "").strip() if len(row) > 4 else "#FFFFFF"
            text = row[5] if len(row) > 5 else ""
            if x_max <= x_min or y_max <= y_min:
                continue
            rects.append((x_min, x_max, y_min, y_max, fill, text))

        if not rects:
            raise ValueError("No valid rectangle rows after parsing.")

        # --- Point (X from N25, Y from E4) ---
        x_raw = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=POINT_X_CELL
        ).execute().get("values", [["0"]])[0][0]
        y_raw = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=POINT_Y_CELL
        ).execute().get("values", [["0"]])[0][0]
        x_score = float(x_raw); y_score = float(y_raw)

        # --- Plot ---
        fig, ax = plt.subplots(figsize=(8, 8))
        ax.set_xlim(AX_MIN, AX_MAX); ax.set_ylim(AX_MIN, AX_MAX)
        ax.set_xlabel(X_LABEL, fontsize=12); ax.set_ylabel(Y_LABEL, fontsize=12)

        for x_min, x_max, y_min, y_max, fill, text in rects:
            W = x_max - x_min; H = y_max - y_min
            ax.add_patch(Rectangle((x_min, y_min), W, H,
                                   facecolor=fill, edgecolor="black",
                                   linewidth=0.5, zorder=1))
            fit_text_to_rect(ax, text, x_min + W/2, y_min + H/2, W, H)

        ax.scatter([x_score], [y_score], s=80, zorder=5)

        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format="png", dpi=150)
        plt.close(fig)
        buf.seek(0)
        b64 = base64.b64encode(buf.read()).decode("utf-8")
        return JSONResponse({"image_base64": b64})

    except Exception as e:
        detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}"
        raise HTTPException(status_code=500, detail=detail)

# Optional local run (ignored by Render)
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
