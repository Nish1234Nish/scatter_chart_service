import matplotlib
matplotlib.use("Agg")import io
import base64
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from google.oauth2 import service_account
from googleapiclient.discovery import build
import textwrap
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ---- CONFIG ----
SHEET_ID = "1NZ1KSX3gn6XRcWwLY7BRhLDXLVtVIFxdhjAsk-9Fy_A"  # <-- Replace with your actual Google Sheet ID
RECT_RANGE = "Backend (Qualitative)!E90:J101"
POINT_X_CELL = "Qualitative Inputs!N25"
POINT_Y_CELL = "Qualitative Inputs!E4"

# Load credentials (Service Account in this example)
import os
KEY_PATH = "/etc/secrets/service_account.json"  # Path where Render stores the secret
creds = service_account.Credentials.from_service_account_file(
    KEY_PATH,
    scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
)
service = build("sheets", "v4", credentials=creds)

app = FastAPI()

def fit_text_to_rect(ax, text, x_center, y_center, width, height, max_fontsize=12, min_fontsize=6):
    """
    Automatically wrap and scale text to fit inside a rectangle.
    """
    for fontsize in range(max_fontsize, min_fontsize - 1, -1):
        wrapped = textwrap.fill(text, width=int(width * 4))  # Adjust wrap based on rect width
        text_obj = ax.text(
            x_center, y_center, wrapped,
            ha='center', va='center',
            fontsize=fontsize, wrap=True
        )
        # Renderer check to see if it fits
        ax.figure.canvas.draw()
        bbox = text_obj.get_window_extent(renderer=ax.figure.canvas.get_renderer())
        inv = ax.transData.inverted()
        bbox_data = bbox.transformed(inv)
        if bbox_data.width <= width and bbox_data.height <= height:
            return  # Found a font size that fits
        text_obj.remove()

@app.get("/chart")
def create_chart():
    # ---- Fetch rectangles ----
    rect_data = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=RECT_RANGE
    ).execute().get("values", [])

    if not rect_data or len(rect_data) < 2:
        return JSONResponse(content={"error": "No rectangle data found"}, status_code=400)

    rects = []
    for row in rect_data[1:]:  # Skip header row
        if len(row) < 6:
            continue
        rects.append({
            "x_min": float(row[0]),
            "x_max": float(row[1]),
            "y_min": float(row[2]),
            "y_max": float(row[3]),
            "fill_color": row[4],
            "text": row[5]
        })

    # ---- Fetch point ----
    x_score = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=POINT_X_CELL
    ).execute().get("values", [["0"]])[0][0]

    y_score = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=POINT_Y_CELL
    ).execute().get("values", [["0"]])[0][0]

    x_score = float(x_score)
    y_score = float(y_score)

    # ---- Build chart ----
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.set_xlabel("Health", fontsize=12)
    ax.set_ylabel("Exit", fontsize=12)

    for r in rects:
        width = r["x_max"] - r["x_min"]
        height = r["y_max"] - r["y_min"]

        rect_patch = Rectangle(
            (r["x_min"], r["y_min"]),
            width,
            height,
            facecolor=r["fill_color"],
            edgecolor="black",
            linewidth=0.5
        )
        ax.add_patch(rect_patch)

        # Auto-fit wrapped text
        fit_text_to_rect(ax, r["text"], r["x_min"] + width/2, r["y_min"] + height/2, width, height)

    # ---- Plot point ----
    ax.scatter([x_score], [y_score], color="red", s=80, zorder=5)

    plt.tight_layout()

    # ---- Convert to base64 ----
    img_buffer = io.BytesIO()
    plt.savefig(img_buffer, format="png", dpi=150)
    plt.close(fig)
    img_buffer.seek(0)
    img_base64 = base64.b64encode(img_buffer.read()).decode("utf-8")

    return JSONResponse(content={"image_base64": img_base64})

