# chart_service.py
# FastAPI service: renders a custom scatter with colored rectangles + wrapped text
# Returns PNG as base64. Render.com compatible (no Dockerfile needed).

import matplotlib
matplotlib.use("Agg")  # headless for server

import os, io, base64, textwrap, traceback, re, json
from typing import Any, Dict, List
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib import colors as mcolors
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# For legacy GET /chart (reads a single sheet)
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ====== CHART CONFIG ======
X_LABEL = "Health"
Y_LABEL = "Exit"
AX_MIN, AX_MAX = 0.0, 10.0
FIG_W, FIG_H = 8, 8                    # inches
DPI = int(os.getenv("CHART_DPI", "110"))  # 8*110=880px (<1M px)
RECT_TEXT_FONT_SIZE = int(os.getenv("RECT_FONT_SIZE", "12"))
RECT_TEXT_PADDING = float(os.getenv("RECT_TEXT_PAD", "0.2"))

# Optional API token (set in Render → Environment)
API_TOKEN = os.getenv("API_TOKEN", "").strip()

# ---- Legacy GET /chart config (kept for backwards-compat) ----
SHEET_ID = "1NZ1KSX3gn6XRcWwLY7BRhLDXLVtVIFxdhjAsk-9Fy_A"
RECT_RANGE = "'Backend (Qualitative)'!E90:J101"   # x_min,x_max,y_min,y_max,fill_colour,text_content
POINT_X_CELL = "'Qualitative Inputs'!N25"         # X = Health
POINT_Y_CELL = "'Qualitative Inputs'!E4"          # Y = Exit
DEFAULT_KEY_PATH = "/etc/secrets/service_account.json"

app = FastAPI()

# ---------------- Security ----------------
def require_token(request: Request):
    if API_TOKEN and request.headers.get("x-api-key", "") != API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ---------------- Utils ----------------
_rgb_re = re.compile(r"rgba?\(\s*([^)]+)\s*\)", re.IGNORECASE)
_nbsp = "\u00a0"

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
    return (*nums, 1.0)  # force opaque

def normalize_color(val: str, default="#ffffff") -> str:
    if not val:
        return default
    s = re.sub(r"\s+", "", str(val).strip().lower())
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

def to_float(val: Any) -> float:
    """Robust float: handles NBSP, commas, stray text."""
    s = str(val).strip().replace(_nbsp, " ")
    s = s.replace(",", "")
    s = re.sub(r"[^0-9eE\.\-+]", "", s)
    if s in ("", "-", "+", ".", "e", "E"):
        raise ValueError(f"invalid number: {val!r}")
    return float(s)

def to_score(val: Any) -> float:
    """
    Convert common notations to a 0–10 score:
      '7' -> 7
      '7/10' -> 7
      '70%' -> 7
    Then clamp to [0, 10].
    """
    s = str(val).strip()
    # a/b fraction
    m = re.match(r'^\s*([+-]?\d+(?:\.\d+)?)\s*/\s*([+-]?\d+(?:\.\d+)?)\s*$', s)
    if m:
        num = float(m.group(1)); den = float(m.group(2)) or 1.0
        v = (num / den) * 10.0
        return max(0.0, min(10.0, v))
    # percentage
    m = re.match(r'^\s*([+-]?\d+(?:\.\d+)?)\s*%\s*$', s)
    if m:
        v = float(m.group(1)) / 10.0
        return max(0.0, min(10.0, v))
    # plain number
    v = to_float(s)
    return max(0.0, min(10.0, v))

def draw_wrapped_block_fixed(ax, text, x_min, y_min, width, height,
                             rect_patch, pad=RECT_TEXT_PADDING, font_size=RECT_TEXT_FONT_SIZE):
    """
    Left-align text, fixed font size, wrap by available pixel width,
    vertically center inside the rectangle, and clip to the rect.
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

    approx_char_px = max(1.0, 0.6 * font_size)  # heuristic
    wrap_chars = max(5, int(avail_px_w / approx_char_px))
    wrapped = textwrap.fill(text or "", width=wrap_chars)

    t = ax.text(x_left, y_top, wrapped,
                ha="left", va="top",
                fontsize=font_size, wrap=True, multialignment="left",
                clip_on=True)
    t.set_clip_path(rect_patch)

    # vertically center
    ax.figure.canvas.draw()
    bbox = t.get_window_extent(renderer=r).transformed(ax.transData.inverted())
    extra_h = avail_h - bbox.height
    if extra_h > 0:
        t.set_position((x_left, y_top - extra_h / 2))

def render_chart(rects: List[Dict[str, Any]], x_score: float, y_score: float) -> str:
    fig, ax = plt.subplots(figsize=(FIG_W, FIG_H))
    ax.set_xlim(AX_MIN, AX_MAX)
    ax.set_ylim(AX_MIN, AX_MAX)
    ax.set_xlabel(X_LABEL, fontsize=12)
    ax.set_ylabel(Y_LABEL, fontsize=12)

    # Rectangles → text → point
    for r in rects:
        x_min, x_max = to_float(r["x_min"]), to_float(r["x_max"])
        y_min, y_max = to_float(r["y_min"]), to_float(r["y_max"])
        if x_max <= x_min or y_max <= y_min:
            continue
        fill_raw = r.get("fill_colour") or r.get("fill_color") or "#ffffff"
        fill = normalize_color(fill_raw, default="#fff2cc")
        text = r.get("text_content", "") or r.get("text", "")

        W, H = x_max - x_min, y_max - y_min
        patch = Rectangle(
            (x_min, y_min), W, H,
            facecolor=fill, edgecolor="none",
            linewidth=0.0, antialiased=False, zorder=1
        )
        ax.add_patch(patch)
        draw_wrapped_block_fixed(ax, text, x_min, y_min, W, H, patch)

    ax.scatter([to_score(x_score)], [to_score(y_score)], s=80, zorder=5)

    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png", dpi=DPI)  # stays under Sheets limits
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")

# ---------------- Endpoints ----------------

@app.get("/healthz")
def healthz():
    return {"ok": True}

# Copy-friendly: Apps Script posts rectangles + point here
@app.post("/chart_json")
async def chart_json(request: Request):
    require_token(request)
    try:
        payload = await request.json()
        rects = payload.get("rectangles", [])
        pt = payload.get("point", {})
        x_score = to_score(pt.get("x", 0))
        y_score = to_score(pt.get("y", 0))
        b64 = render_chart(rects, x_score, y_score)
        # optional debug: show used scores
        if request.query_params.get("debug") == "1":
            return JSONResponse({"image_base64": b64, "used": {"x": x_score, "y": y_score}})
        return JSONResponse({"image_base64": b64})
    except Exception as e:
        detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}"
        raise HTTPException(status_code=500, detail=detail)

# Legacy: reads a specific sheet directly (kept for your current workbook)
def get_sheets_service():
    key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", DEFAULT_KEY_PATH)
    if not os.path.exists(key_path):
        raise FileNotFoundError(f"Service account key not found at '{key_path}'.")
    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=creds)

@app.get("/chart")
def chart(request: Request):
    require_token(request)
    try:
        svc = get_sheets_service()

        vals = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=RECT_RANGE
        ).execute().get("values", [])
        if not vals or len(vals) < 2:
            raise ValueError(f"No rectangle data in {RECT_RANGE}")

        rects = []
        for row in vals[1:]:
            if len(row) < 4:   # only coords required; color/text optional
                continue
            rects.append({
                "x_min": row[0], "x_max": row[1],
                "y_min": row[2], "y_max": row[3],
                "fill_colour": row[4] if len(row) > 4 else "#ffffff",
                "text_content": row[5] if len(row) > 5 else ""
            })

        x_raw = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=POINT_X_CELL
        ).execute().get("values", [["0"]])[0][0]
        y_raw = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=POINT_Y_CELL
        ).execute().get("values", [["0"]])[0][0]

        b64 = render_chart(rects, to_score(x_raw), to_score(y_raw))
        # optional debug: show used scores
        if request.query_params.get("debug") == "1":
            return JSONResponse({"image_base64": b64, "used": {"x": to_score(x_raw), "y": to_score(y_raw)}})
        return JSONResponse({"image_base64": b64})
    except Exception as e:
        detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=2)}"
        raise HTTPException(status_code=500, detail=detail)

# Optional local debug
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)

