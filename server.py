"""
Backend Río Serrano — versión 2.0
Endpoint nuevo: GET /ibe-shop/v1/hotel/99498/avail
Devuelve detalle por tipo de habitación con tarifas, amenidades, imágenes.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from playwright.async_api import async_playwright

# =========================================================
# Configuración
# =========================================================
HOTEL_ID = 99498
BOOKING_URL = f"https://bookings.travelclick.com/{HOTEL_ID}"
API_AVAIL = f"https://api.travelclick.com/ibe-shop/v1/hotel/{HOTEL_ID}/avail"
TC_IMAGES_BASE = "https://images.travelclick.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

TOKEN_TTL_SECONDS = 50 * 60
CACHE_TTL_SECONDS = 10 * 60

HERE = Path(__file__).parent


# =========================================================
# JWT cache
# =========================================================
class _TokenCache:
    def __init__(self):
        self.jwt: Optional[str] = None
        self.captured_at: float = 0.0
        self.lock = threading.Lock()

    def expired(self) -> bool:
        return (not self.jwt) or (time.time() - self.captured_at > TOKEN_TTL_SECONDS)


_token = _TokenCache()


async def _capturar_jwt_async() -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
            ],
        )
        ctx = await browser.new_context(
            user_agent=UA,
            viewport={'width': 1280, 'height': 900},
            locale='es-CL',
        )
        await ctx.add_init_script(
            'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
        )
        page = await ctx.new_page()

        jwt_found: Optional[str] = None

        async def on_request(req):
            nonlocal jwt_found
            if jwt_found:
                return
            if 'api.travelclick.com' in req.url and req.resource_type in ('fetch', 'xhr'):
                auth = req.headers.get('authorization', '')
                if auth.startswith('Bearer eyJ'):
                    jwt_found = auth[7:]

        page.on('request', on_request)
        await page.goto(BOOKING_URL, wait_until='domcontentloaded', timeout=45000)

        for _ in range(60):
            if jwt_found:
                break
            await asyncio.sleep(0.5)

        await browser.close()
        if not jwt_found:
            raise RuntimeError("No se pudo capturar JWT")
        return jwt_found


def obtener_jwt(force: bool = False) -> str:
    with _token.lock:
        if force or _token.expired():
            _token.jwt = asyncio.run(_capturar_jwt_async())
            _token.captured_at = time.time()
        return _token.jwt


# =========================================================
# Cache de respuestas
# =========================================================
_response_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: tuple) -> Optional[dict]:
    with _cache_lock:
        entry = _response_cache.get(key)
        if entry and (time.time() - entry[0]) < CACHE_TTL_SECONDS:
            return entry[1]
    return None


def _cache_put(key: tuple, value: dict) -> None:
    with _cache_lock:
        _response_cache[key] = (time.time(), value)


# =========================================================
# Llamada al motor
# =========================================================
def consultar_motor(date_in: str, date_out: str,
                    adultos: int, ninos: int,
                    currency: str = "USD", lang: str = "EN_US") -> dict:
    jwt = obtener_jwt()
    headers = {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CL",
        "Origin": "https://bookings.travelclick.com",
        "Referer": "https://bookings.travelclick.com/",
        "User-Agent": UA,
        "x-tc-header": f"currency={currency}",
        "sec-ch-ua": '"Chromium";v="141", "Not?A_Brand";v="8"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    params = {
        "lang": lang,
        "adults": adultos,
        "infants": ninos,
        "currency": currency,
        "rooms": 1,
        "dateIn": date_in,
        "dateOut": date_out,
        "isAltHotelsReq": "true",
        "bookerIdentifier": "",
        "partnerIdentifier": "Web4_Desktop",
    }
    r = requests.get(API_AVAIL, headers=headers, params=params, timeout=30)
    if r.status_code == 401:
        jwt = obtener_jwt(force=True)
        headers["Authorization"] = f"Bearer {jwt}"
        r = requests.get(API_AVAIL, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


# =========================================================
# Normalización: simplifica la respuesta para el frontend
# =========================================================
def _absolute_image(source: Optional[str]) -> Optional[str]:
    if not source:
        return None
    if source.startswith("http"):
        return source
    if source.startswith("/"):
        return f"{TC_IMAGES_BASE}{source}"
    return None


def normalizar(data: dict, check_in: str, check_out: str,
               adultos: int, ninos: int) -> dict:
    """Convierte la respuesta cruda de /avail en algo simple para el frontend."""
    moneda = data.get("currencyCode", "USD")
    room_stays = data.get("roomStays") or []
    if not room_stays:
        return {
            "check_in": check_in,
            "check_out": check_out,
            "noches": 0,
            "moneda": moneda,
            "habitaciones": [],
            "rate_plans": [],
        }

    rs = room_stays[0]
    duration = rs.get("timeSpan", {}).get("duration", 0)

    # Indexar rate plans por código
    rate_plans = {}
    for rp in rs.get("ratePlans", []):
        rate_plans[rp["ratePlanCode"]] = {
            "code": rp["ratePlanCode"],
            "nombre": rp.get("ratePlanName", ""),
            "descripcion": rp.get("ratePlanDescription", ""),
            "lead_rate": rp.get("leadRate"),
            "cancelacion": rp.get("cancellationPolicy", {}).get("policyDescription", ""),
            "no_refundable": rp.get("cancellationPolicy", {}).get("nonRefundable", False),
        }

    # Habitaciones
    habitaciones = []
    for rt in rs.get("roomTypes", []):
        # Tarifas por rate plan
        tarifas = []
        for ar in rt.get("averageRates", []):
            rp_code = ar.get("ratePlanCode")
            rp_info = rate_plans.get(rp_code, {})
            tarifa_noche = ar.get("rate") or 0
            tarifas.append({
                "rate_plan_code": rp_code,
                "rate_plan_nombre": rp_info.get("nombre", ""),
                "tarifa_promedio_noche": tarifa_noche,
                "tarifa_total": round(tarifa_noche * duration, 2),
                "disponible": ar.get("available", False),
            })

        # Amenidades simplificadas
        amenidades = [
            {"nombre": a.get("amenityName"), "premium": a.get("isPremiumAmenity", False)}
            for a in (rt.get("amenities") or [])
        ]

        # Features (capacidad, m², camas)
        features = []
        for f in (rt.get("roomFeatures") or []):
            features.append({
                "tipo": f.get("type"),
                "nombre": f.get("amenityName"),
                "cantidad": f.get("quantity"),
            })

        # Galería de imágenes
        imagenes = []
        if rt.get("mainImage"):
            url = _absolute_image(rt["mainImage"].get("source"))
            if url:
                imagenes.append(url)
        for m in (rt.get("media") or []):
            url = _absolute_image(m.get("source"))
            if url and url not in imagenes:
                imagenes.append(url)

        habitaciones.append({
            "codigo": rt.get("roomExternalCode"),
            "nombre": rt.get("roomTypeName"),
            "descripcion": rt.get("description", "").strip(),
            "cantidad_disponible": rt.get("quantityRemaining", 0),
            "urgencia": rt.get("displayUrgencyMessage", False),
            "disponible": rt.get("available", False),
            "imagen_principal": imagenes[0] if imagenes else None,
            "imagenes": imagenes,
            "features": features,
            "amenidades": amenidades,
            "tarifas": tarifas,
        })

    # Ordenar habitaciones disponibles primero, después por tarifa
    habitaciones.sort(key=lambda h: (
        not h["disponible"],
        min((t["tarifa_promedio_noche"] for t in h["tarifas"] if t["disponible"]), default=999999),
    ))

    return {
        "check_in": check_in,
        "check_out": check_out,
        "noches": duration,
        "adultos": adultos,
        "ninos": ninos,
        "moneda": moneda,
        "habitaciones": habitaciones,
        "rate_plans": list(rate_plans.values()),
    }


# =========================================================
# FastAPI app
# =========================================================
app = FastAPI(title="Río Serrano · Disponibilidad")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "hotel_id": HOTEL_ID,
        "jwt_captured": _token.jwt is not None,
        "jwt_age_seconds": int(time.time() - _token.captured_at) if _token.jwt else None,
    }


@app.get("/api/disponibilidad")
def disponibilidad(
    check_in: str = Query(..., description="YYYY-MM-DD"),
    check_out: str = Query(..., description="YYYY-MM-DD"),
    adultos: int = Query(2, ge=1, le=6),
    ninos: int = Query(0, ge=0, le=4),
):
    try:
        din = datetime.strptime(check_in, "%Y-%m-%d").date()
        dout = datetime.strptime(check_out, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Fechas deben ser YYYY-MM-DD")
    if dout <= din:
        raise HTTPException(400, "check_out debe ser posterior a check_in")
    if din < date.today():
        raise HTTPException(400, "check_in no puede estar en el pasado")
    if (dout - din).days > 30:
        raise HTTPException(400, "Rango máximo 30 noches")

    cache_key = (check_in, check_out, adultos, ninos)
    cached = _cache_get(cache_key)
    if cached:
        return JSONResponse(cached)

    try:
        data = consultar_motor(check_in, check_out, adultos, ninos)
    except requests.HTTPError as e:
        raise HTTPException(502, f"Motor de reservas error: {e}")
    except Exception as e:
        raise HTTPException(500, f"Error: {e}")

    result = normalizar(data, check_in, check_out, adultos, ninos)
    _cache_put(cache_key, result)
    return JSONResponse(result)


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
