"""
Backend Río Serrano — v5
================================================================
Doble modo de operación automático:

  MODO A — API OFICIAL (preferido, cuando llegue Amadeus)
  Si están definidas las variables de entorno:
      AMADEUS_CLIENT_ID
      AMADEUS_CLIENT_SECRET
  → Usa OAuth client_credentials contra api.travelclick.com/oauth/token
  → Sin Playwright, sin Akamai, sin Chromium
  → Reservas en 1-2 segundos
  → Tarjeta en texto plano (PCI aplica)

  MODO B — SCRAPING (fallback actual)
  Si NO están esas variables:
  → Usa Playwright para capturar JWT del booking engine público
  → 30-60s la primera vez, luego cacheado 50min
  → Solo soporta consultas; reservas NO funcionan (Akamai bloquea)

El switch es automático al arranque, sin tocar código.
Para activar modo A en Render: Settings → Environment → Add Environment Variable
================================================================
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

# ---------------------------------------------------------------------
#  Configuración
# ---------------------------------------------------------------------
HOTEL_ID = 99498
API_BASE = "https://api.travelclick.com"
BOOKING_URL = f"https://bookings.travelclick.com/{HOTEL_ID}"
API_AVAIL = f"{API_BASE}/ibe-shop/v1/hotel/{HOTEL_ID}/avail"
API_HOLD = f"{API_BASE}/ibe-book/v1/hotel/{HOTEL_ID}/hold-reservation/multi-room"
API_RESERVE = f"{API_BASE}/ibe-book/v1/hotel/{HOTEL_ID}/reservation/multi-room"
API_OAUTH_OFICIAL = f"{API_BASE}/oauth/token"
API_OAUTH_REFERER = f"{API_BASE}/oauth/token-referer"
TC_IMAGES_BASE = "https://bookings.travelclick.com"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

TOKEN_TTL_SECONDS = 50 * 60
CACHE_TTL_SECONDS = 10 * 60

HERE = Path(__file__).parent

LANG_MAP = {
    "es": "ES_ES", "en": "EN_US",
    "ES_ES": "ES_ES", "EN_US": "EN_US",
}

# Detectar modo activo al arranque
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")
MODO_OFICIAL = bool(AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("rioserrano")
log.info("=" * 60)
log.info(f"Iniciando Río Serrano backend")
log.info(f"Modo: {'OFICIAL (Amadeus API)' if MODO_OFICIAL else 'SCRAPING (Playwright fallback)'}")
log.info("=" * 60)


# ---------------------------------------------------------------------
#  Token cache
# ---------------------------------------------------------------------
class _TokenCache:
    def __init__(self):
        self.jwt: Optional[str] = None
        self.captured_at: float = 0.0
        self.lock = threading.Lock()

    def expired(self) -> bool:
        return (not self.jwt) or (time.time() - self.captured_at > TOKEN_TTL_SECONDS)


_token = _TokenCache()


# ---------------------------------------------------------------------
#  MODO A — Obtener token vía OAuth oficial
# ---------------------------------------------------------------------
def _obtener_jwt_oficial() -> str:
    """OAuth client_credentials contra api.travelclick.com/oauth/token."""
    log.info("Solicitando token OAuth oficial...")
    r = requests.post(
        API_OAUTH_OFICIAL,
        data={
            "grant_type": "client_credentials",
            "client_id": AMADEUS_CLIENT_ID,
            "client_secret": AMADEUS_CLIENT_SECRET,
        },
        headers={"Accept": "application/json"},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OAuth oficial falló: HTTP {r.status_code} - {r.text[:200]}")
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Respuesta OAuth sin access_token: {data}")
    log.info(f"Token oficial obtenido (ttl: {data.get('expires_in', '?')}s)")
    return token


# ---------------------------------------------------------------------
#  MODO B — Obtener token vía scraping (Playwright)
#  Solo se importa Playwright si realmente se necesita
# ---------------------------------------------------------------------
async def _obtener_jwt_scraping_async() -> str:
    from playwright.async_api import async_playwright

    log.info("Capturando JWT con Playwright (scraping)...")
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
            raise RuntimeError("No se pudo capturar JWT con scraping")
        log.info("JWT capturado vía scraping")
        return jwt_found


# ---------------------------------------------------------------------
#  Función unificada para obtener token (elige modo automáticamente)
# ---------------------------------------------------------------------
def obtener_jwt(force: bool = False) -> str:
    """Devuelve un JWT válido. Elige modo según variables de entorno."""
    with _token.lock:
        if force or _token.expired():
            if MODO_OFICIAL:
                _token.jwt = _obtener_jwt_oficial()
            else:
                _token.jwt = asyncio.run(_obtener_jwt_scraping_async())
            _token.captured_at = time.time()
        return _token.jwt


# ---------------------------------------------------------------------
#  Cache de respuestas de disponibilidad
# ---------------------------------------------------------------------
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


# ---------------------------------------------------------------------
#  Llamada al motor — consulta disponibilidad
# ---------------------------------------------------------------------
def _headers_motor(currency: str = "USD") -> dict:
    """Headers comunes para llamar a api.travelclick.com."""
    jwt = obtener_jwt()
    return {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CL",
        "Origin": "https://bookings.travelclick.com",
        "Referer": "https://bookings.travelclick.com/",
        "User-Agent": UA,
        "x-tc-header": f"currency={currency}",
    }


def consultar_motor(date_in: str, date_out: str,
                    adultos: int, ninos: int,
                    currency: str = "USD", lang: str = "ES_ES") -> dict:
    headers = _headers_motor(currency)
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
        # Token expirado — renovar y reintentar
        headers["Authorization"] = f"Bearer {obtener_jwt(force=True)}"
        r = requests.get(API_AVAIL, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------
#  Normalización de respuesta para el frontend
# ---------------------------------------------------------------------
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
    moneda = data.get("currencyCode", "USD")
    room_stays = data.get("roomStays") or []
    if not room_stays:
        return {
            "check_in": check_in, "check_out": check_out,
            "noches": 0, "moneda": moneda,
            "habitaciones": [], "rate_plans": [],
        }

    rs = room_stays[0]
    duration = rs.get("timeSpan", {}).get("duration", 0)

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

    habitaciones = []
    for rt in rs.get("roomTypes", []):
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

        amenidades = [
            {"nombre": a.get("amenityName"), "premium": a.get("isPremiumAmenity", False)}
            for a in (rt.get("amenities") or [])
        ]
        features = [
            {"tipo": f.get("type"), "nombre": f.get("amenityName"), "cantidad": f.get("quantity")}
            for f in (rt.get("roomFeatures") or [])
        ]

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
            # Datos para usar en el paso de reserva
            "room_type_code": rt.get("roomTypeCode"),
            "pms_room_external_code": rt.get("pmsRoomExternalCode"),
        })

    habitaciones.sort(key=lambda h: (
        not h["disponible"],
        min((t["tarifa_promedio_noche"] for t in h["tarifas"] if t["disponible"]), default=999999),
    ))

    return {
        "check_in": check_in, "check_out": check_out,
        "noches": duration,
        "adultos": adultos, "ninos": ninos,
        "moneda": moneda,
        "habitaciones": habitaciones,
        "rate_plans": list(rate_plans.values()),
    }


# ---------------------------------------------------------------------
#  Endpoint de reserva (solo funciona en modo oficial)
# ---------------------------------------------------------------------
def crear_reserva(payload: dict) -> dict:
    """
    Crea una reserva en OPERA vía API de TravelClick.
    Solo disponible en modo oficial (con credenciales Amadeus).
    
    El payload debe seguir la estructura descubierta en la captura del cURL real:
    {
      "hotelCode": 99498,
      "languageCode": "EN_US",
      "itineraryId": "...",
      "reservationRequestParams": [...],  # con datos del huésped, tarjeta, habitación, etc.
    }
    """
    if not MODO_OFICIAL:
        raise RuntimeError(
            "Reservas solo disponibles en modo oficial. "
            "Configura AMADEUS_CLIENT_ID y AMADEUS_CLIENT_SECRET en Render."
        )

    headers = _headers_motor("USD")
    headers["Content-Type"] = "application/json;charset=UTF-8"

    # Paso 1: hold-reservation (reserva temporal del inventario)
    log.info("Iniciando hold-reservation...")
    r1 = requests.post(API_HOLD, headers=headers, json=payload, timeout=30)
    if r1.status_code != 200:
        raise RuntimeError(f"Hold-reservation falló: HTTP {r1.status_code} - {r1.text[:300]}")
    log.info("Hold-reservation OK")

    # Paso 2: reservation (confirmación final que inserta en OPERA)
    log.info("Iniciando confirmación final...")
    r2 = requests.post(API_RESERVE, headers=headers, json=payload, timeout=30)
    if r2.status_code != 200:
        raise RuntimeError(f"Reservation falló: HTTP {r2.status_code} - {r2.text[:300]}")
    log.info("Reserva confirmada en OPERA")
    return r2.json()


# ---------------------------------------------------------------------
#  FastAPI app
# ---------------------------------------------------------------------
app = FastAPI(title="Río Serrano · Booking Engine")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "hotel_id": HOTEL_ID,
        "mode": "official" if MODO_OFICIAL else "scraping",
        "jwt_captured": _token.jwt is not None,
        "jwt_age_seconds": int(time.time() - _token.captured_at) if _token.jwt else None,
        "can_book": MODO_OFICIAL,
    }


@app.get("/api/disponibilidad")
def disponibilidad(
    check_in: str = Query(..., description="YYYY-MM-DD"),
    check_out: str = Query(..., description="YYYY-MM-DD"),
    adultos: int = Query(2, ge=1, le=6),
    ninos: int = Query(0, ge=0, le=4),
    lang: str = Query("es", description="es | en"),
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

    motor_lang = LANG_MAP.get(lang, "ES_ES")
    cache_key = (check_in, check_out, adultos, ninos, motor_lang)

    cached = _cache_get(cache_key)
    if cached:
        return JSONResponse(cached)

    try:
        data = consultar_motor(check_in, check_out, adultos, ninos, lang=motor_lang)
    except requests.HTTPError as e:
        log.error(f"Motor error: {e}")
        raise HTTPException(502, f"Motor de reservas error: {e}")
    except Exception as e:
        log.error(f"Error inesperado: {e}")
        raise HTTPException(500, f"Error: {e}")

    result = normalizar(data, check_in, check_out, adultos, ninos)
    _cache_put(cache_key, result)
    return JSONResponse(result)


@app.post("/api/reservar")
async def reservar(payload: dict):
    """
    Crear reserva real en OPERA.
    Solo disponible en modo oficial (con credenciales Amadeus configuradas).
    
    En modo scraping devuelve 503 — el frontend debe mostrar mensaje
    de "Solicita tu reserva por email" o similar.
    """
    if not MODO_OFICIAL:
        raise HTTPException(
            503,
            "Reservas online no disponibles temporalmente. "
            "Por favor contacta al hotel directamente."
        )

    try:
        result = crear_reserva(payload)
        return JSONResponse({
            "success": True,
            "reservation": result,
        })
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    except Exception as e:
        log.error(f"Error creando reserva: {e}")
        raise HTTPException(500, f"Error inesperado: {e}")


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
