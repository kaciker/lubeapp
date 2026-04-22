import asyncio
import base64
import json
import os
import sqlite3
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="FuelLog API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "/data/lubeapp.db"
OR_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
LL_KEY  = os.environ.get("LUBELOGGER_API_KEY", "")

def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url

LL_URL = _normalize_url(os.environ.get("LUBELOGGER_URL", ""))

ALLOWED_MODELS = {
    "google/gemini-2.0-flash-001",
    "anthropic/claude-3-5-haiku",
    "anthropic/claude-sonnet-4-5",
    "openai/gpt-4o-mini",
}

PROMPT_TEMPLATE = """Analiza estas dos imágenes:
1. Un ticket/recibo de repostaje de combustible
2. El odómetro o cuadro de mandos de un vehículo
{gps_context}
Devuelve ÚNICAMENTE un objeto JSON válido, sin texto adicional, sin markdown, sin backticks. Estructura exacta:

{{
  "fecha": "YYYY-MM-DD",
  "hora": "HH:MM",
  "gasolinera": "nombre completo o null",
  "direccion_gasolinera": "dirección o null",
  "tipo_combustible": "gasolina_95|gasolina_98|diesel|glp|electrico|otro",
  "litros": 0.00,
  "precio_por_litro": 0.000,
  "importe_total": 0.00,
  "numero_ticket": "número o null",
  "odometro_km": 0,
  "vehiculo": "{vehicle}",
  "metodo_pago": "efectivo|tarjeta|app|null",
  "notas": "datos adicionales o null"
}}

Reglas: números como type number (no string). Si no se lee claramente, usa null. Solo el JSON."""


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS records (
            id                   TEXT PRIMARY KEY,
            timestamp            TEXT NOT NULL,
            fecha                TEXT,
            hora                 TEXT,
            gasolinera           TEXT,
            direccion_gasolinera TEXT,
            tipo_combustible     TEXT,
            litros               REAL,
            precio_por_litro     REAL,
            importe_total        REAL,
            numero_ticket        TEXT,
            odometro_km          INTEGER,
            vehiculo             TEXT,
            metodo_pago          TEXT,
            notas                TEXT,
            model                TEXT,
            tokens               INTEGER,
            raw_json             TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()
    # Migration: add GPS columns if they don't exist yet
    for col, coltype in [("lat", "REAL"), ("lon", "REAL"), ("gps_address", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE records ADD COLUMN {col} {coltype}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# GPS / Nominatim
# ---------------------------------------------------------------------------

async def reverse_geocode(lat: float, lon: float) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "zoom": 17, "addressdetails": 1},
                headers={"User-Agent": "FuelLog/1.0 (lubeapp)"},
            )
        if not r.is_success:
            return None
        data = r.json()
        return data.get("display_name")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Webhook helper
# ---------------------------------------------------------------------------

async def fire_webhook(data: dict) -> None:
    conn = get_db()
    row = conn.execute("SELECT value FROM config WHERE key='webhook_url'").fetchone()
    conn.close()
    if not row or not row["value"]:
        return
    url = row["value"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=data, headers={"Content-Type": "application/json"})
    except Exception:
        pass  # webhook failures are intentionally silent


# ---------------------------------------------------------------------------
# LubeLogger integration
# ---------------------------------------------------------------------------

async def push_to_lubelogger(parsed: dict, ticket_b64: str) -> dict:
    ll_url = LL_URL
    ll_key = LL_KEY

    conn = get_db()
    vid_row = conn.execute("SELECT value FROM config WHERE key='lubelogger_vehicle_id'").fetchone()
    conn.close()
    ll_vid = vid_row["value"] if vid_row and vid_row["value"] else ""

    if not ll_url or not ll_key or not ll_vid:
        return {"ok": False, "error": "no_config"}

    headers = {"x-api-key": ll_key}
    record_id = parsed.get("_id", "0")

    # Step 1 – upload ticket image
    doc_location: str | None = None
    doc_name = f"ticket_{record_id}.jpg"
    try:
        img_bytes = base64.b64decode(ticket_b64)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{ll_url}/api/documents/upload",
                headers=headers,
                files={"documents": (doc_name, img_bytes, "image/jpeg")},
            )
        if r.is_success:
            docs = r.json()
            if docs and isinstance(docs, list):
                doc_location = docs[0].get("location")
    except Exception as e:
        doc_location = None  # continue without image

    # Step 2 – build gas record payload
    # Notes: everything we collect that has no dedicated LubeLogger field
    # Excluded from notes (already in LL fields): fecha, odometro_km, litros, importe_total, gps coords
    notes_parts = []
    if parsed.get("gasolinera"):
        notes_parts.append(parsed["gasolinera"])
    if parsed.get("tipo_combustible"):
        notes_parts.append(parsed["tipo_combustible"])
    if parsed.get("precio_por_litro") is not None:
        notes_parts.append(f"{float(parsed['precio_por_litro']):.3f} €/L")
    if parsed.get("direccion_gasolinera"):
        notes_parts.append(parsed["direccion_gasolinera"])
    if parsed.get("numero_ticket"):
        notes_parts.append(f"Ticket: {parsed['numero_ticket']}")
    if parsed.get("metodo_pago"):
        notes_parts.append(f"Pago: {parsed['metodo_pago']}")
    if parsed.get("notas"):
        notes_parts.append(parsed["notas"])

    odometer = parsed.get("odometro_km")
    fuel = parsed.get("litros")
    cost = parsed.get("importe_total")

    payload: dict = {
        "date": parsed.get("fecha") or datetime.now().strftime("%Y-%m-%d"),
        "odometer": round(float(odometer)) if odometer is not None else 0,
        "fuelConsumed": float(fuel) if fuel is not None else 0.0,
        "cost": float(cost) if cost is not None else 0.0,
        "isFillToFull": True,
        "missedFuelUp": False,
        "notes": " | ".join(notes_parts),
        "extraFields": [],
    }

    # GPS → global extraField "gps" (fieldType: Location) defined in LubeLogger
    lat = parsed.get("_lat")
    lon = parsed.get("_lon")
    if lat is not None and lon is not None:
        payload["extraFields"].append({"name": "gps", "value": f"{lat:.6f},{lon:.6f}"})

    if doc_location:
        payload["files"] = [{"name": doc_name, "location": doc_location}]

    # Step 3 – create gas record
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{ll_url}/api/vehicle/gasrecords/add?vehicleId={ll_vid}",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
        if r.is_success:
            return {"ok": True, "doc": doc_location}
        return {"ok": False, "error": f"{r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    ticket_b64: str
    odo_b64: Optional[str] = None
    model: str = "google/gemini-2.0-flash-001"
    vehicle: str = "No especificado"
    lat: Optional[float] = None
    lon: Optional[float] = None
    manual_odometer_km: Optional[int] = None


class ConfigBody(BaseModel):
    value: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

HTML_FILE = os.path.join(os.path.dirname(__file__), "index.html")

@app.get("/")
def serve_frontend():
    return FileResponse(HTML_FILE, media_type="text/html",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "key_configured": bool(OR_KEY),
        "lubelogger_configured": bool(LL_URL and LL_KEY),
    }


@app.get("/api/lubelogger/vehicles")
async def get_lubelogger_vehicles():
    if not LL_URL or not LL_KEY:
        raise HTTPException(503, "LUBELOGGER_URL o LUBELOGGER_API_KEY no configurados en .env")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{LL_URL}/api/vehicles",
                headers={"x-api-key": LL_KEY},
            )
        if r.is_success:
            return r.json()
        raise HTTPException(r.status_code, f"LubeLogger: {r.text[:200]}")
    except httpx.TimeoutException:
        raise HTTPException(504, "Timeout al conectar con LubeLogger")


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    if not OR_KEY:
        raise HTTPException(400, "OPENROUTER_API_KEY no configurada en el servidor. Edita el fichero .env y reinicia.")

    if req.model not in ALLOWED_MODELS:
        raise HTTPException(400, f"Modelo no permitido: {req.model}")

    # Size guard
    if len(req.ticket_b64) > 4_000_000:
        raise HTTPException(413, "Imagen del ticket demasiado grande (máx ~3 MB en base64)")
    if req.odo_b64 and len(req.odo_b64) > 4_000_000:
        raise HTTPException(413, "Imagen del odómetro demasiado grande (máx ~3 MB en base64)")
    if not req.odo_b64 and req.manual_odometer_km is None:
        raise HTTPException(400, "Se necesita foto del odómetro o km manual.")

    # Reverse geocode before building prompt so the AI gets location context
    gps_address: str | None = None
    if req.lat is not None and req.lon is not None:
        gps_address = await reverse_geocode(req.lat, req.lon)

    gps_context = ""
    if gps_address:
        gps_context = f"\nContexto GPS: el vehículo está en «{gps_address}». Usa esta información para completar gasolinera y dirección si no se leen claramente en el ticket.\n"

    # Build prompt — single image if no odo photo
    if req.odo_b64:
        intro = "Analiza estas dos imágenes:\n1. Un ticket/recibo de repostaje de combustible\n2. El odómetro o cuadro de mandos de un vehículo"
        odo_note = ""
    else:
        intro = "Analiza esta imagen (ticket de repostaje de combustible)."
        odo_note = f"\nOdómetro: {req.manual_odometer_km} km (introducido manualmente; usa este valor exacto en odometro_km).\n"

    prompt = PROMPT_TEMPLATE.format(
        vehicle=req.vehicle.replace('"', "'")[:80],
        gps_context=gps_context,
    ).replace("Analiza estas dos imágenes:\n1. Un ticket/recibo de repostaje de combustible\n2. El odómetro o cuadro de mandos de un vehículo", intro, 1)
    if odo_note:
        prompt = prompt.replace("{gps_context}", odo_note + gps_context, 1) if "{gps_context}" in prompt else odo_note + prompt

    # Build image content list
    img_content = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{req.ticket_b64}"}}]
    if req.odo_b64:
        img_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{req.odo_b64}"}})
    img_content.append({"type": "text", "text": prompt})

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OR_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://lubeapp",
                    "X-Title": "FuelLog",
                },
                json={
                    "model": req.model,
                    "messages": [{"role": "user", "content": img_content}],
                    "temperature": 0.1,
                    "max_tokens": 600,
                    "response_format": {"type": "json_object"},
                },
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "Timeout al contactar OpenRouter. Inténtalo de nuevo.")

    if not response.is_success:
        try:
            err = response.json()
            msg = err.get("error", {}).get("message", "")
        except Exception:
            msg = ""
        if response.status_code == 401:
            raise HTTPException(401, "API Key inválida. Revisa OPENROUTER_API_KEY en .env")
        if response.status_code == 402:
            raise HTTPException(402, "Sin crédito en OpenRouter. Recarga en openrouter.ai/credits")
        if response.status_code == 429:
            raise HTTPException(429, "Demasiadas peticiones. Espera un momento.")
        raise HTTPException(response.status_code, msg or f"Error {response.status_code}")

    data = response.json()
    raw_text = data["choices"][0]["message"]["content"]
    clean = raw_text.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        raise HTTPException(422, "La IA no devolvió JSON válido. Prueba con fotos más nítidas.")

    # Metadata
    record_id = str(int(datetime.now().timestamp() * 1000))
    parsed["_id"] = record_id
    parsed["_timestamp"] = datetime.now().isoformat()
    parsed["_model"] = req.model
    if "usage" in data:
        parsed["_tokens"] = data["usage"].get("total_tokens")
    if req.lat is not None:
        parsed["_lat"] = req.lat
        parsed["_lon"] = req.lon
    if gps_address:
        parsed["_gps_address"] = gps_address
    # If AI returned null for odometer but user provided manual value, use it
    if req.manual_odometer_km is not None and not parsed.get("odometro_km"):
        parsed["odometro_km"] = req.manual_odometer_km

    # Push to LubeLogger (synchronous so status is included in response)
    ll_result = await push_to_lubelogger(parsed, req.ticket_b64)
    parsed["_lubelogger"] = ll_result

    # Persist
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO records
           (id, timestamp, fecha, hora, gasolinera, direccion_gasolinera, tipo_combustible,
            litros, precio_por_litro, importe_total, numero_ticket, odometro_km, vehiculo,
            metodo_pago, notas, model, tokens, lat, lon, gps_address, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record_id, parsed["_timestamp"],
            parsed.get("fecha"), parsed.get("hora"),
            parsed.get("gasolinera"), parsed.get("direccion_gasolinera"),
            parsed.get("tipo_combustible"), parsed.get("litros"),
            parsed.get("precio_por_litro"), parsed.get("importe_total"),
            parsed.get("numero_ticket"), parsed.get("odometro_km"),
            parsed.get("vehiculo"), parsed.get("metodo_pago"),
            parsed.get("notas"), req.model,
            parsed.get("_tokens"), req.lat, req.lon, gps_address,
            json.dumps(parsed),
        ),
    )
    conn.commit()
    conn.close()

    # Webhook (fire and forget)
    asyncio.create_task(fire_webhook(parsed))

    return parsed


@app.get("/api/records")
def get_records():
    conn = get_db()
    rows = conn.execute(
        "SELECT raw_json FROM records ORDER BY timestamp DESC"
    ).fetchall()
    conn.close()
    return [json.loads(r["raw_json"]) for r in rows]


@app.delete("/api/records/{record_id}")
def delete_record(record_id: str):
    conn = get_db()
    conn.execute("DELETE FROM records WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/records")
def delete_all_records():
    conn = get_db()
    conn.execute("DELETE FROM records")
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/export")
def export_records():
    conn = get_db()
    rows = conn.execute(
        "SELECT raw_json FROM records ORDER BY timestamp DESC"
    ).fetchall()
    conn.close()
    records = [json.loads(r["raw_json"]) for r in rows]
    content = json.dumps(records, ensure_ascii=False, indent=2)
    filename = f"fuellog_{datetime.now().strftime('%Y-%m-%d')}.json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/config/{key}")
def get_config(key: str):
    conn = get_db()
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    conn.close()
    return {"value": row["value"] if row else ""}


@app.put("/api/config/{key}")
def set_config(key: str, body: ConfigBody):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        (key, body.value),
    )
    conn.commit()
    conn.close()
    return {"ok": True}
