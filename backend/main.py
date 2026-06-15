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

app = FastAPI(title="ElBeregner API", version="1.0.0")

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
_mp_cache: dict = {"mp_id": None}
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
                CREATE TABLE IF NOT EXISTS forbrug (
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


async def get_metering_point_id(token: str) -> str:
    if _mp_cache["mp_id"]:
        return _mp_cache["mp_id"]

    async with httpx.AsyncClient(timeout=30) as client:
        for forsøg in range(3):
            resp = await client.get(
                f"{ELOVERBLIK_BASE}/meteringpoints/meteringpoints?includeAll=false",
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

    _mp_cache["mp_id"] = points[0]["meteringPointId"]
    return _mp_cache["mp_id"]


def parse_timeseries(result: list) -> dict[str, float]:
    consumption: dict[str, float] = {}
    for item in result:
        doc = item.get("MyEnergyData_MarketDocument", {})
        for ts in doc.get("TimeSeries", []):
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
                    qty_raw = point.get("out_Quantity.quantity", "0") or "0"
                    try:
                        kwh = float(qty_raw)
                    except ValueError:
                        kwh = 0.0
                    key = (start_dt + timedelta(hours=pos)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    consumption[key] = consumption.get(key, 0.0) + kwh
    return consumption


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

@app.get("/api/forbrug")
async def get_forbrug(
    fra: str = Query(...),
    til: str = Query(...),
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    try:
        datetime.strptime(fra, "%Y-%m-%d")
        datetime.strptime(til, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Dato skal være YYYY-MM-DD")

    data = await _fetch_forbrug_raw(fra, til)
    return {
        "fra": fra, "til": til,
        "timeforbrug": data["timeforbrug"],
        "total_kwh": round(sum(data["timeforbrug"].values()), 3),
        "fra_cache": data.get("fra_cache", False),
    }


@app.get("/api/spotpriser")
async def get_spotpriser(
    fra: str = Query(...),
    til: str = Query(...),
    zone: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(default=None),
):
    check_api_key(x_api_key)
    try:
        datetime.strptime(fra, "%Y-%m-%d")
        datetime.strptime(til, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Dato skal være YYYY-MM-DD")

    pris_zone = zone or PRISZONE
    data = await _fetch_spotpriser_raw(fra, til, pris_zone)
    return {"fra": fra, "til": til, "zone": pris_zone, "spotpriser": data["spotpriser"]}


@app.get("/api/maaned")
async def get_maaned(
    aar: int = Query(...),
    maaned: int = Query(...),
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

    forbrug_data, spot_data = await asyncio.gather(
        asyncio.create_task(_fetch_forbrug_raw(fra, til)),
        asyncio.create_task(_fetch_spotpriser_raw(fra, til, PRISZONE)),
    )

    timeforbrug = forbrug_data["timeforbrug"]
    spotpriser = spot_data["spotpriser"]
    fast_ore = ELAFGIFT_ORE + SYSTEMTARIF_ORE + TRANSMISSIONSTARIF_ORE + ELSELSKAB_TILLÆG_ORE

    timer = []
    total_kr_forbrug = 0.0
    total_kwh = 0.0
    manglende_timer = []
    spotpris_sum = 0.0
    spotpris_count = 0

    for hour in sorted(timeforbrug.keys()):
        kwh = timeforbrug[hour]
        spot = spotpriser.get(hour)
        if spot is None:
            manglende_timer.append(hour)
            continue
        nettarif = nettarif_for_hour(hour)
        pris_per_kwh = spot * (1 + MOMS) + (fast_ore + nettarif) / 100.0
        kr = round(kwh * pris_per_kwh, 4)
        total_kr_forbrug += kr
        total_kwh += kwh
        spotpris_sum += spot
        spotpris_count += 1
        timer.append({
            "time": hour,
            "kwh": round(kwh, 4),
            "spotpris_kwh": round(spot, 6),
            "nettarif_ore": nettarif,
            "pris_per_kwh": round(pris_per_kwh, 4),
            "kr": kr,
        })

    gns_spotpris = (spotpris_sum / spotpris_count) if spotpris_count else 0.0
    total_kr = round(total_kr_forbrug + ABONNEMENT_KR, 2)

    return {
        "aar": aar, "maaned": maaned, "fra": fra, "til": til, "zone": PRISZONE,
        "total_kwh": round(total_kwh, 3),
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
        "fra_cache": forbrug_data.get("fra_cache", False),
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

    # Compute exact UTC boundaries for today/tomorrow in DK local time
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

    # Aggregate 15-min records to hourly
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

async def _fetch_forbrug_raw(fra: str, til: str) -> dict:
    til_dt = datetime.strptime(til, "%Y-%m-%d")
    til_excl = (til_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    # Brug cache til afsluttede måneder
    if not is_current_month(fra):
        cached = await db_get_forbrug(fra, til_excl)
        if cached:
            return {"timeforbrug": cached, "fra_cache": True}

    token = await get_access_token()
    mp_id = await get_metering_point_id(token)
    body = {"meteringPoints": {"meteringPoint": [mp_id]}}

    # Hent i 14-dages blokke for at undgå eloverblik's datalimit
    fra_dt = datetime.strptime(fra, "%Y-%m-%d")
    til_dt = datetime.strptime(til, "%Y-%m-%d")
    consumption: dict[str, float] = {}
    chunk_start = fra_dt

    async with httpx.AsyncClient(timeout=60) as client:
        while chunk_start <= til_dt:
            chunk_end = min(chunk_start + timedelta(days=13), til_dt)
            if chunk_start == chunk_end:
                break  # eloverblik tillader ikke samme fra- og til-dato
            resp = await client.post(
                f"{ELOVERBLIK_BASE}/meterdata/gettimeseries/{chunk_start.strftime('%Y-%m-%d')}/{chunk_end.strftime('%Y-%m-%d')}/Hour",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Eloverblik tidsseriedata fejl: {resp.status_code} ({chunk_start.strftime('%Y-%m-%d')} til {chunk_end.strftime('%Y-%m-%d')}): {resp.text[:300]}")
            consumption.update(parse_timeseries(resp.json().get("result", [])))
            chunk_start = chunk_end + timedelta(days=1)

    await db_save_forbrug(consumption)
    return {"timeforbrug": consumption, "fra_cache": False}


async def _fetch_spotpriser_raw(fra: str, til: str, zone: str = PRISZONE) -> dict:
    til_dt = datetime.strptime(til, "%Y-%m-%d")
    til_excl = (til_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    # Brug cache til afsluttede måneder
    if not is_current_month(fra):
        cached = await db_get_spotpriser(fra, til_excl, zone)
        if cached:
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
