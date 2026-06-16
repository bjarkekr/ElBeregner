import os
import json
import asyncio
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request

app = FastAPI(title="ElBeregner API", version="2.0.0")

# Config from environment
ELOVERBLIK_TOKEN = os.getenv("ELOVERBLIK_TOKEN", "").strip()
ELAFGIFT_ORE = float(os.getenv("ELAFGIFT_ORE", "76.1"))
NETTARIF_T1_ORE = float(os.getenv("NETTARIF_T1_ORE", "30.0"))
NETTARIF_T2_ORE = float(os.getenv("NETTARIF_T2_ORE", "92.0"))
NETTARIF_T3_ORE = float(os.getenv("NETTARIF_T3_ORE", "318.0"))
NETTARIF_T4_ORE = float(os.getenv("NETTARIF_T4_ORE", "92.0"))
SYSTEMTARIF_ORE = float(os.getenv("SYSTEMTARIF_ORE", "6.0"))
TRANSMISSIONSTARIF_ORE = float(os.getenv("TRANSMISSIONSTARIF_ORE", "4.9"))
ELSELSKAB_TILLÆG_ORE = float(os.getenv("ELSELSKAB_TILLÆG_ORE", "0.0"))
ABONNEMENT_KR = float(os.getenv("ABONNEMENT_KR", "38.0"))
MOMS = float(os.getenv("MOMS", "0.25"))
PRISZONE = os.getenv("PRISZONE", "DK1")
API_KEY = os.getenv("API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

DK_TZ = ZoneInfo("Europe/Copenhagen")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

_token_cache: dict = {"token": None, "expires_at": None}
_mp_cache: dict = {"mp_ids": None}
_db_pool: Optional[asyncpg.Pool] = None

ELOVERBLIK_BASE = "https://api.eloverblik.dk/CustomerApi/api"
ENERGIDATA_URL = "https://api.energidataservice.dk/dataset/DayAheadPrices"


@app.on_event("startup")
async def startup():
    global _db_pool
    if DATABASE_URL:
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        _db_pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
        async with _db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS forbrug (
                    hour_utc TEXT PRIMARY KEY,
                    kwh REAL NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS produktion (
                    hour_utc TEXT PRIMARY KEY,
                    kwh REAL NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS spotpriser (
                    hour_utc TEXT NOT NULL,
                    zone TEXT NOT NULL,
                    pris_dkk_kwh REAL NOT NULL,
                    PRIMARY KEY (hour_utc, zone)
                )
            """)
            # One-time migration: clear forbrug table that contained production data
            already = await conn.fetchval(
                "SELECT 1 FROM migrations WHERE name = 'v2_split_forbrug_produktion'"
            )
            if not already:
                await conn.execute("TRUNCATE forbrug")
                await conn.execute(
                    "INSERT INTO migrations (name) VALUES ('v2_split_forbrug_produktion')"
                )


@app.on_event("shutdown")
async def shutdown():
    if _db_pool:
        await _db_pool.close()


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Serverfejl: {type(exc).__name__}: {str(exc)}"},
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ─── Helpers ────────────────────────────────────────────────────────────────

def nettarif_for_hour(utc_iso: str) -> float:
    dt_utc = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    dt_dk = dt_utc.astimezone(DK_TZ)
    h = dt_dk.hour
    if 0 <= h < 6:
        return NETTARIF_T1_ORE
    elif 6 <= h < 17:
        return NETTARIF_T2_ORE
    elif 17 <= h < 21:
        return NETTARIF_T3_ORE
    else:
        return NETTARIF_T4_ORE


def check_api_key(x_api_key: Optional[str]):
    if not API_KEY:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Ugyldig eller manglende API-nøgle.")


def is_current_month(fra: str) -> bool:
    now = datetime.utcnow()
    fra_dt = datetime.strptime(fra, "%Y-%m-%d")
    return fra_dt.year == now.year and fra_dt.month == now.month


async def get_access_token() -> str:
    now = datetime.utcnow()
    if _token_cache["token"] and _token_cache["expires_at"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    if not ELOVERBLIK_TOKEN:
        raise HTTPException(status_code=500, detail="ELOVERBLIK_TOKEN er ikke konfigureret på serveren.")

    async with httpx.AsyncClient(timeout=30) as client:
        for forsøg in range(3):
            resp = await client.get(
                f"{ELOVERBLIK_BASE}/token",
                headers={"Authorization": f"Bearer {ELOVERBLIK_TOKEN}"},
            )
            if resp.status_code == 200:
                break
            if forsøg < 2:
                await asyncio.sleep(3)
        else:
            raise HTTPException(status_code=502, detail=f"Eloverblik token fejl: {resp.status_code} (3 forsøg)")

    token = resp.json().get("result")
    if not token:
        raise HTTPException(status_code=502, detail="Eloverblik returnerede intet token.")

    _token_cache["token"] = token
    _token_cache["expires_at"] = now + timedelta(hours=23)
    return token


async def get_metering_point_ids(token: str) -> list[str]:
    if _mp_cache["mp_ids"]:
        return _mp_cache["mp_ids"]

    async with httpx.AsyncClient(timeout=30) as client:
        for forsøg in range(3):
            resp = await client.get(
                f"{ELOVERBLIK_BASE}/meteringpoints/meteringpoints?includeAll=true",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                break
            if forsøg < 2:
                await asyncio.sleep(3)
        else:
            raise HTTPException(status_code=502, detail=f"Eloverblik målepunkt fejl: {resp.status_code} (3 forsøg)")

    points = resp.json().get("result", [])
    if not points:
        raise HTTPException(status_code=404, detail="Ingen målepunkter fundet på kontoen.")

    mp_ids = [p["meteringPointId"] for p in points]
    _mp_cache["mp_ids"] = mp_ids
    return mp_ids


def parse_timeseries_split(result: list) -> tuple[dict[str, float], dict[str, float]]:
    """
    Separerer eloverblik-svar i (forbrug, produktion).

    eloverblik businessType skelner mellem retninger:
      'A04' = forbrug (import fra nettet, el brugt i huset fra grid)
      Andre  = produktion (eksport til nettet, solceller mv.)

    Begge TimeSeries bruger out_Quantity.quantity som feltnavn.
    """
    forbrug: dict[str, float] = {}
    produktion: dict[str, float] = {}

    for item in result:
        doc = item.get("MyEnergyData_MarketDocument", {})
        for ts in doc.get("TimeSeries", []):
            business_type = ts.get("businessType", "")
            target = forbrug if business_type == "A04" else produktion

            for period in ts.get("Period", []):
                interval_start = period.get("timeInterval", {}).get("start", "")
                if not interval_start:
                    continue
                try:
                    start_dt = datetime.fromisoformat(interval_start.replace("Z", "+00:00"))
                except ValueError:
                    continue
                for point in period.get("Point", []):
                    pos = int(point.get("position", 1)) - 1
                    qty_raw = (
                        point.get("out_Quantity.quantity")
                        or point.get("in_Quantity.quantity")
                        or "0"
                    )
                    try:
                        kwh = float(qty_raw)
                    except ValueError:
                        kwh = 0.0
                    key = (start_dt + timedelta(hours=pos)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    target[key] = target.get(key, 0.0) + kwh

    return forbrug, produktion


# ─── DB helpers ─────────────────────────────────────────────────────────────

async def db_get_forbrug(fra: str, til_excl: str) -> dict[str, float] | None:
    if not _db_pool:
        return None
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT hour_utc, kwh FROM forbrug WHERE hour_utc >= $1 AND hour_utc < $2 ORDER BY hour_utc",
            fra + "T00:00:00Z", til_excl + "T00:00:00Z"
        )
    return {r["hour_utc"]: r["kwh"] for r in rows} if rows else None


async def db_save_forbrug(data: dict[str, float]):
    if not _db_pool or not data:
        return
    async with _db_pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO forbrug (hour_utc, kwh) VALUES ($1, $2) ON CONFLICT (hour_utc) DO UPDATE SET kwh = $2",
            list(data.items())
        )


async def db_get_produktion(fra: str, til_excl: str) -> dict[str, float] | None:
    if not _db_pool:
        return None
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT hour_utc, kwh FROM produktion WHERE hour_utc >= $1 AND hour_utc < $2 ORDER BY hour_utc",
            fra + "T00:00:00Z", til_excl + "T00:00:00Z"
        )
    return {r["hour_utc"]: r["kwh"] for r in rows} if rows else None


async def db_save_produktion(data: dict[str, float]):
    if not _db_pool or not data:
        return
    async with _db_pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO produktion (hour_utc, kwh) VALUES ($1, $2) ON CONFLICT (hour_utc) DO UPDATE SET kwh = $2",
            list(data.items())
        )


async def db_get_spotpriser(fra: str, til_excl: str, zone: str) -> dict[str, float] | None:
    if not _db_pool:
        return None
    async with _db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT hour_utc, pris_dkk_kwh FROM spotpriser WHERE hour_utc >= $1 AND hour_utc < $2 AND zone = $3 ORDER BY hour_utc",
            fra + "T00:00:00Z", til_excl + "T00:00:00Z", zone
        )
    return {r["hour_utc"]: r["pris_dkk_kwh"] for r in rows} if rows else None


async def db_save_spotpriser(data: dict[str, float], zone: str):
    if not _db_pool or not data:
        return
    async with _db_pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO spotpriser (hour_utc, zone, pris_dkk_kwh) VALUES ($1, $2, $3) ON CONFLICT (hour_utc, zone) DO UPDATE SET pris_dkk_kwh = $3",
            [(k, zone, v) for k, v in data.items()]
        )


# ─── API endpoints ──────────────────────────────────────────────────────────

@app.get("/api/maaned")
async def get_maaned(
    aar: int = Query(...),
    maaned: int = Query(...),
    kun_cache: bool = Query(False),
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    if not (1 <= maaned <= 12):
        raise HTTPException(status_code=400, detail="Måned skal være 1-12")

    fra_dt = datetime(aar, maaned, 1)
    til_dt = (datetime(aar, maaned + 1, 1) if maaned < 12 else datetime(aar + 1, 1, 1)) - timedelta(days=1)
    yesterday = (datetime.utcnow() - timedelta(days=1)).date()
    if til_dt.date() > yesterday:
        til_dt = datetime.combine(yesterday, datetime.min.time())

    fra = fra_dt.strftime("%Y-%m-%d")
    til = til_dt.strftime("%Y-%m-%d")
    til_excl = (til_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    if kun_cache:
        timeforbrug = await db_get_forbrug(fra, til_excl) or {}
        timeprod = await db_get_produktion(fra, til_excl) or {}
        spotpriser = await db_get_spotpriser(fra, til_excl, PRISZONE) or {}
        if not timeforbrug:
            raise HTTPException(status_code=404, detail="Ingen data i database for denne måned.")
        fra_cache = True
    else:
        elov_data, spot_data = await asyncio.gather(
            asyncio.create_task(_fetch_eloverblik_raw(fra, til)),
            asyncio.create_task(_fetch_spotpriser_raw(fra, til, PRISZONE)),
        )
        timeforbrug = elov_data["forbrug"]
        timeprod = elov_data["produktion"]
        spotpriser = spot_data["spotpriser"]
        fra_cache = elov_data.get("fra_cache", False)

    fast_ore = ELAFGIFT_ORE + SYSTEMTARIF_ORE + TRANSMISSIONSTARIF_ORE + ELSELSKAB_TILLÆG_ORE

    timer = []
    total_kr_forbrug = 0.0
    total_forbrug_kwh = 0.0
    manglende_timer = []
    spotpris_sum = 0.0
    spotpris_count = 0

    for hour in sorted(timeforbrug.keys()):
        kwh = timeforbrug[hour]
        prod_kwh = timeprod.get(hour, 0.0)
        spot = spotpriser.get(hour)
        if spot is None:
            manglende_timer.append(hour)
            continue
        nettarif = nettarif_for_hour(hour)
        pris_per_kwh = spot * (1 + MOMS) + (fast_ore + nettarif) / 100.0
        kr = round(kwh * pris_per_kwh, 4)
        total_kr_forbrug += kr
        total_forbrug_kwh += kwh
        spotpris_sum += spot
        spotpris_count += 1
        timer.append({
            "time": hour,
            "kwh": round(kwh, 4),
            "produktion_kwh": round(prod_kwh, 4),
            "spotpris_kwh": round(spot, 6),
            "nettarif_ore": nettarif,
            "pris_per_kwh": round(pris_per_kwh, 4),
            "kr": kr,
        })

    total_prod_kwh = round(sum(timeprod.values()), 3)
    netto_kwh = round(total_forbrug_kwh - total_prod_kwh, 3)
    gns_spotpris = (spotpris_sum / spotpris_count) if spotpris_count else 0.0
    total_kr = round(total_kr_forbrug + ABONNEMENT_KR, 2)

    return {
        "aar": aar, "maaned": maaned, "fra": fra, "til": til, "zone": PRISZONE,
        "forbrug_kwh": round(total_forbrug_kwh, 3),
        "produktion_kwh": total_prod_kwh,
        "netto_kwh": netto_kwh,
        "total_kwh": round(total_forbrug_kwh, 3),  # backward compat
        "total_kr_forbrug": round(total_kr_forbrug, 2),
        "abonnement_kr": ABONNEMENT_KR,
        "total_kr": total_kr,
        "gns_spotpris_kwh": round(gns_spotpris, 6),
        "afgifter": {
            "elafgift_ore": ELAFGIFT_ORE,
            "nettarif_t1_ore": NETTARIF_T1_ORE,
            "nettarif_t2_ore": NETTARIF_T2_ORE,
            "nettarif_t3_ore": NETTARIF_T3_ORE,
            "nettarif_t4_ore": NETTARIF_T4_ORE,
            "systemtarif_ore": SYSTEMTARIF_ORE,
            "transmissionstarif_ore": TRANSMISSIONSTARIF_ORE,
            "elselskab_tillæg_ore": ELSELSKAB_TILLÆG_ORE,
            "abonnement_kr": ABONNEMENT_KR,
            "moms_pct": MOMS * 100,
        },
        "fra_cache": fra_cache,
        "timer": timer,
        "manglende_timer_antal": len(manglende_timer),
    }


@app.get("/api/priser/dag")
async def get_priser_dag(
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)

    now_dk = datetime.now(DK_TZ)
    today_dk = now_dk.date()
    tomorrow_dk = today_dk + timedelta(days=1)
    day_after_tomorrow_dk = today_dk + timedelta(days=2)

    start_utc = datetime(today_dk.year, today_dk.month, today_dk.day,
                         tzinfo=DK_TZ).astimezone(ZoneInfo("UTC"))
    end_utc = datetime(day_after_tomorrow_dk.year, day_after_tomorrow_dk.month,
                       day_after_tomorrow_dk.day, tzinfo=DK_TZ).astimezone(ZoneInfo("UTC"))

    params = {
        "start": start_utc.strftime("%Y-%m-%dT%H:%M"),
        "end": end_utc.strftime("%Y-%m-%dT%H:%M"),
        "filter": json.dumps({"PriceArea": PRISZONE}),
        "sort": "TimeUTC asc",
        "limit": 200,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(ENERGIDATA_URL, params=params)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Energi Data Service fejl: {resp.status_code}")

    summer: dict[str, list] = {}
    for r in resp.json().get("records", []):
        t = r.get("TimeUTC", "")
        if t:
            key = t[:13].replace(" ", "T") + ":00:00Z"
            summer.setdefault(key, []).append(r.get("DayAheadPriceDKK") or 0.0)

    fast_ore = ELAFGIFT_ORE + SYSTEMTARIF_ORE + TRANSMISSIONSTARIF_ORE + ELSELSKAB_TILLÆG_ORE
    now_hour_utc = now_dk.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:00:00Z")

    today_hours: list = []
    tomorrow_hours: list = []

    for hour_utc, prices in sorted(summer.items()):
        dt_utc = datetime.fromisoformat(hour_utc.replace("Z", "+00:00"))
        dt_dk = dt_utc.astimezone(DK_TZ)
        dato_dk = dt_dk.date()

        spot = round(sum(prices) / len(prices) / 1000, 6)
        nettarif = nettarif_for_hour(hour_utc)
        total = round(spot * (1 + MOMS) + (fast_ore + nettarif) / 100.0, 4)

        entry = {
            "time_utc": hour_utc,
            "time_dk": dt_dk.strftime("%H:%M"),
            "spot_dkk_kwh": spot,
            "total_dkk_kwh": total,
            "nettarif_ore": nettarif,
            "er_nu": hour_utc == now_hour_utc,
        }

        if dato_dk == today_dk:
            today_hours.append(entry)
        elif dato_dk == tomorrow_dk:
            tomorrow_hours.append(entry)

    return {
        "i_dag": str(today_dk),
        "i_morgen": str(tomorrow_dk),
        "nu_dk": now_dk.strftime("%H:%M"),
        "i_dag_timer": today_hours,
        "i_morgen_timer": tomorrow_hours,
        "i_morgen_tilgængelig": len(tomorrow_hours) > 0,
        "afgifter": {
            "elafgift_ore": ELAFGIFT_ORE,
            "nettarif_t1_ore": NETTARIF_T1_ORE,
            "nettarif_t2_ore": NETTARIF_T2_ORE,
            "nettarif_t3_ore": NETTARIF_T3_ORE,
            "nettarif_t4_ore": NETTARIF_T4_ORE,
            "systemtarif_ore": SYSTEMTARIF_ORE,
            "transmissionstarif_ore": TRANSMISSIONSTARIF_ORE,
            "elselskab_tillæg_ore": ELSELSKAB_TILLÆG_ORE,
            "moms_pct": MOMS * 100,
        },
    }


@app.get("/api/debug/forbrug")
async def debug_forbrug(
    dato: str = Query(...),
    x_api_key: Optional[str] = Header(default=None),
):
    """Rå eloverblik-svar for én dag — viser businessType pr. TimeSeries."""
    check_api_key(x_api_key)
    dato_dt = datetime.strptime(dato, "%Y-%m-%d")
    next_dato = dato_dt + timedelta(days=1)
    next_dato_str = next_dato.strftime("%Y-%m-%d")

    token = await get_access_token()
    mp_ids = await get_metering_point_ids(token)
    body = {"meteringPoints": {"meteringPoint": mp_ids}}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{ELOVERBLIK_BASE}/meterdata/gettimeseries/{dato}/{next_dato_str}/Hour",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
        )

    try:
        raw = resp.json()
    except Exception as json_err:
        return {
            "kald": f"from={dato} to={next_dato_str}",
            "http_status": resp.status_code,
            "json_fejl": str(json_err),
            "råt_svar": resp.text[:1000],
        }

    forbrug_parsed, prod_parsed = parse_timeseries_split(raw.get("result", []))

    result_items = raw.get("result", [])
    struktur = []
    alle_business_types: list[str] = []
    for item in result_items:
        doc = item.get("MyEnergyData_MarketDocument") or {}
        for ts in (doc.get("TimeSeries") or []):
            business_type = ts.get("businessType", "—")
            alle_business_types.append(business_type)
            for period in (ts.get("Period") or []):
                pts = period.get("Point") or []
                struktur.append({
                    "business_type": business_type,
                    "type_tolket": "forbrug" if business_type == "A04" else "produktion",
                    "interval_start": (period.get("timeInterval") or {}).get("start"),
                    "interval_end": (period.get("timeInterval") or {}).get("end"),
                    "antal_punkter": len(pts),
                    "første_punkt": pts[0] if pts else None,
                })

    return {
        "kald": f"from={dato} to={next_dato_str}",
        "http_status": resp.status_code,
        "distinct_business_types": list(dict.fromkeys(alle_business_types)),
        "forbrug_timer": len(forbrug_parsed),
        "forbrug_kwh_total": round(sum(forbrug_parsed.values()), 3),
        "produktion_timer": len(prod_parsed),
        "produktion_kwh_total": round(sum(prod_parsed.values()), 3),
        "struktur": struktur,
        "rå_result": result_items,
    }


@app.get("/api/status")
async def status():
    db_ok = False
    if _db_pool:
        try:
            async with _db_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            db_ok = True
        except Exception:
            pass
    return {
        "status": "ok",
        "zone": PRISZONE,
        "database": "tilsluttet" if db_ok else "ikke konfigureret",
        "afgifter": {
            "elafgift_ore": ELAFGIFT_ORE,
            "nettarif_t1_00_06": NETTARIF_T1_ORE,
            "nettarif_t2_06_17": NETTARIF_T2_ORE,
            "nettarif_t3_17_21": NETTARIF_T3_ORE,
            "nettarif_t4_21_24": NETTARIF_T4_ORE,
            "systemtarif_ore": SYSTEMTARIF_ORE,
            "transmissionstarif_ore": TRANSMISSIONSTARIF_ORE,
            "elselskab_tillæg_ore": ELSELSKAB_TILLÆG_ORE,
            "abonnement_kr": ABONNEMENT_KR,
        },
    }


# ─── Internal fetch helpers ──────────────────────────────────────────────────

async def _fetch_eloverblik_raw(fra: str, til: str) -> dict:
    """Henter forbrug + produktion fra eloverblik. Cacher afsluttede måneder."""
    til_dt = datetime.strptime(til, "%Y-%m-%d")
    til_excl = (til_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    # Brug cache til afsluttede måneder — kræv mindst 24 timer for at undgå partial-cache
    if not is_current_month(fra):
        cached_f = await db_get_forbrug(fra, til_excl)
        if cached_f and len(cached_f) >= 24:
            cached_p = await db_get_produktion(fra, til_excl) or {}
            return {"forbrug": cached_f, "produktion": cached_p, "fra_cache": True}

    token = await get_access_token()
    mp_ids = await get_metering_point_ids(token)
    body = {"meteringPoints": {"meteringPoint": mp_ids}}

    fra_dt = datetime.strptime(fra, "%Y-%m-%d")
    forbrug: dict[str, float] = {}
    produktion: dict[str, float] = {}
    chunk_start = fra_dt

    async with httpx.AsyncClient(timeout=60) as client:
        while chunk_start <= til_dt:
            chunk_end = min(chunk_start + timedelta(days=14), til_dt + timedelta(days=1))
            fra_s = chunk_start.strftime("%Y-%m-%d")
            til_s = chunk_end.strftime("%Y-%m-%d")
            resp = await client.post(
                f"{ELOVERBLIK_BASE}/meterdata/gettimeseries/{fra_s}/{til_s}/Hour",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code == 503:
                await asyncio.sleep(3)
                resp = await client.post(
                    f"{ELOVERBLIK_BASE}/meterdata/gettimeseries/{fra_s}/{til_s}/Hour",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=body,
                )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Eloverblik tidsseriedata fejl: {resp.status_code} ({fra_s}–{til_s}): {resp.text[:300]}"
                )
            f_chunk, p_chunk = parse_timeseries_split(resp.json().get("result", []))
            for hour_key, kwh in f_chunk.items():
                if fra <= hour_key[:10] < til_excl:
                    forbrug[hour_key] = forbrug.get(hour_key, 0.0) + kwh
            for hour_key, kwh in p_chunk.items():
                if fra <= hour_key[:10] < til_excl:
                    produktion[hour_key] = produktion.get(hour_key, 0.0) + kwh
            chunk_start = chunk_end

    await asyncio.gather(
        db_save_forbrug(forbrug),
        db_save_produktion(produktion),
    )
    return {"forbrug": forbrug, "produktion": produktion, "fra_cache": False}


async def _fetch_spotpriser_raw(fra: str, til: str, zone: str = PRISZONE) -> dict:
    til_dt = datetime.strptime(til, "%Y-%m-%d")
    til_excl = (til_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    # Brug cache til afsluttede måneder — kræv mindst 24 timer for at undgå partial-cache
    if not is_current_month(fra):
        cached = await db_get_spotpriser(fra, til_excl, zone)
        if cached and len(cached) >= 24:
            return {"spotpriser": cached}

    params = {
        "start": f"{fra}T00:00",
        "end": f"{til_excl}T00:00",
        "filter": json.dumps({"PriceArea": zone}),
        "sort": "TimeUTC asc",
        "limit": 4000,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(ENERGIDATA_URL, params=params)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Energi Data Service fejl: {resp.status_code}")

    summer: dict[str, list] = {}
    for r in resp.json().get("records", []):
        t = r.get("TimeUTC", "")
        if t:
            key = t[:13].replace(" ", "T") + ":00:00Z"
            summer.setdefault(key, []).append(r.get("DayAheadPriceDKK") or 0.0)
    priser = {k: round(sum(v) / len(v) / 1000, 6) for k, v in summer.items()}

    await db_save_spotpriser(priser, zone)
    return {"spotpriser": priser}
