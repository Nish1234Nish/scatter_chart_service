# chart_service.py
# FastAPI service that reads rectangle defs + a point from Google Sheets,
# renders a custom scatter with colored rectangles + wrapped labels,
# and returns a PNG as base64. Ready for Render.com (no Dockerfile needed).

import matplotlib
matplotlib.use("Agg")  # headless backend for servers

import os, io, base64, textwrap, traceback
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ==== CONFIG (you can change these if your ranges/sheet names move) ====
SHEET_ID = "1NZ1KSX3gn6XRcWwLY7BRhLDXLVtVIFxdhjAsk-9Fy_A"  # your sheet
RECT_RANGE = "'Backend (Qualitative)'!E90:J101"            # header at row 90
POINT_X_CELL = "'Qualitative Inputs'!N25"                  # X = Health
POINT_Y_CELL = "'Qualitative Inputs'!E4"                   # Y = Exit
X_LABEL = "Health"
Y_LABEL = "Exit"
AX_MIN, AX_MAX = 0.0, 10.0

# Render Secret Files mount here by default:
DEFAULT_KEY_PATH = "/etc/secrets/service_account.json"

app = FastAPI()


@app.get("/healthz")
def healthz():
    return {"ok": True}


def get_sheets_service():
    """
    Initialize Google Sheets API client using a service-account key.
    On Render, store the JSON as a Secret File named 'service_account.json'.
    """
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
    """
    Wraps and scales text so it fits inside a rectangle (in data units).
    Tries larger font sizes first, then shrinks until it fits.
    """
    safe_width = max(width, 0.1)
    safe_height = max(height, 0.1)

    for fontsize in range(max_fontsize, min_fontsize - 1, -1):
        # Heuristic wrap length based on rect width in axis units
        wrap_chars = max(10, int(safe_width * 4))
        wrapped = textwrap.fill(text or "", width=wrap_chars)

        txt = ax.text(
            x_center, y_center, wrapped,
            ha="center", va="center",
            fontsize=fontsize, wrap=True,
        )
        # Ask the renderer for the size in data coordinates
        ax.figure.canvas.draw()
        renderer = ax.figure.canvas.get_renderer()
        bbox_disp = txt.get_window_extent(renderer=renderer)
        bbox_data = bbox_disp.transformed(ax.transData.inverted())
        if bbox_data.width <= safe_width and bbox_data.height <= safe_height:
            return  # fits—keep this text object
        txt.remove()  # too big—try smaller

    # If nothing fit, place smallest anyway (clipped gracefully)
    ax.text(
        x_center, y_center, textwrap.fill(text or "", width=wrap_chars),
        ha="center", va="center", fontsize=min_fontsize, wrap=True
    )


@app.get("/chart")
def chart():
    try:
        service = get_sheets_service()

        # --- Fetch rectangles (expects header row at E90:J90) ---
        resp = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=RECT_RANGE
        ).execute()
        values = resp.get("values", [])
        if not values or len(values) < 2:
            raise ValueError(f"No rectangle data found in {RECT_RANGE}")

        # Header row: x_min,x_max,y_min,y_max,fill_colour,text_content
        rects = []
        for row in values[1:]:
            if len(row) < 6:
                continue
            try:
                x_min = float(row[0]); x_max = float(row[1])
                y_min = float(row[2]); y_max = float(row[3])
            except ValueError:
                # Skip rows with non-numeric coords
                continue
            fill = row[4].strip() if len(row) > 4 else "#FFFFFF"
            text = row[5] if len(row) > 5 else ""
            if x_max <= x_min or y_max <= y_min:
                continue  # skip degenerate rectangles
            rects.append((x_min, x_max, y_min, y_max, fill, text))

        if not rects:
            raise ValueError("No valid rectangle rows after parsing.")

        # --- Fetch point (X from N25, Y from E4) ---
        x_raw = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=POINT_X_CELL
        ).execute().get("values", [["0"]])[0][0]
        y_raw = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=POINT_Y_CELL
        ).execute().get("values", [["0"]])[0][0]
        x_score = float(x_raw)
        y_score = float(y_raw)

        # --- Build chart ---
        fig, ax = plt.subplots(figsize=(8, 8))
        ax.set_xlim(AX_MIN, AX_MAX)
        ax.set_ylim(AX_MIN, AX_MAX)
        ax.set_xlabel(X_LABEL, fontsize=12)
        ax.set_ylabel(Y_LABEL, fontsize=12)

        # draw rectangles + wrapped labels
        for x_min, x_max, y_min, y_max, fill, text in rects:
            W = x_max - x_min
            H = y_max - y_min
            ax.add_patch(Rectangle(
                (x_min, y_min), W, H,
                facecolor=fill, edgecolor="black", linewidth=0.5, zorder=1
            ))
            fit_text_to_rect(ax, text, x_min + W/2, y_min + H/2, W, H)

        # scatter point on top
        ax.scatter([x_score], [y_score], s=80, zorder=5)

        # export as PNG → base64
        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format="png", dpi=150)
        plt.close(fig)
        buf.seek(0)

