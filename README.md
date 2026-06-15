# ElBeregner

PWA til at tracke dit danske elforbrug og beregne din månedlige elregning baseret på timeforbrug × spotpris.

## Arkitektur

```
/
├── backend/     FastAPI (deployes på Railway)
└── frontend/    PWA – HTML/CSS/JS, ingen framework (deployes på Vercel)
```

**Datakilder:**
- Elforbrug: [eloverblik.dk Customer API](https://api.eloverblik.dk/CustomerApi/swagger/index.html)
- Spotpriser: [Energi Data Service](https://api.energidataservice.dk/dataset/Elspotprices)

---

## Trin-for-trin deployguide

### 1. Opret GitHub repo

```bash
git init
git add .
git commit -m "Initial commit"
gh repo create elberegner --public --source=. --push
```

Eller opret manuelt på github.com og push.

---

### 2. Hent dit refresh token fra eloverblik.dk

1. Gå til [eloverblik.dk](https://eloverblik.dk) og log ind med MitID
2. Klik på dit navn øverst → **"Datadeling"** → **"Tredjepartsadgang"**
3. Klik **"Opret token"**, giv det et navn (f.eks. "ElBeregner"), vælg **læseadgang**
4. Kopiér det lange token – det bruges som `ELOVERBLIK_TOKEN`

> Tokenet er et refresh token med lang levetid. Backend'en veksler det automatisk til et kortlivet data access token.

---

### 3. Deploy backend på Railway

1. Gå til [railway.app](https://railway.app) og log ind
2. Klik **"New Project"** → **"Deploy from GitHub repo"**
3. Vælg dit repo og mappen `backend/` som **Root Directory** (under Settings → Source)
4. Under **Variables** tilføj følgende environment variables:

| Variabel | Værdi | Beskrivelse |
|---|---|---|
| `ELOVERBLIK_TOKEN` | `dit_token_her` | Refresh token fra trin 2 |
| `ELAFGIFT_ORE` | `76.1` | Elafgift i øre/kWh (2024-sats) |
| `NETTARIF_ORE` | `21.0` | Dit netselskabs tarif |
| `ELSELSKAB_TILLÆG_ORE` | `10.0` | Dit elselskabs tillæg |
| `MOMS` | `0.25` | Moms (25%) |
| `PRISZONE` | `DK1` | `DK1` = Jylland/Fyn, `DK2` = Sjælland |
| `ALLOWED_ORIGIN` | `https://din-app.vercel.app` | Din Vercel-URL (sættes efter deploy af frontend) |

5. Deploy sker automatisk. Notér din Railway-URL (f.eks. `https://elberegner-production.up.railway.app`)

**Test backend:**
```
https://din-api.railway.app/api/status
```

---

### 4. Deploy frontend på Vercel

1. Gå til [vercel.com](https://vercel.com) og log ind
2. Klik **"Add New → Project"** → importér dit GitHub repo
3. Sæt **Root Directory** til `frontend/`
4. Framework Preset: **Other**
5. Klik **Deploy**
6. Notér din Vercel-URL (f.eks. `https://elberegner.vercel.app`)

**Opdatér Railway med Vercel-URL:**
Gå tilbage til Railway → Variables → opdatér `ALLOWED_ORIGIN` til din Vercel-URL.

---

### 5. Installer PWA på Android

1. Åbn din Vercel-URL i **Chrome** på Android
2. Vent et øjeblik – Chrome viser en banner "Tilføj til startskærm" eller
3. Tryk på **⋮ menu → "Tilføj til startskærm"**
4. Bekræft med **"Tilføj"**
5. Åbn appen fra startskærmen – den kører nu i standalone-mode uden browserbaren

---

## API-endpoints

### `GET /api/forbrug?fra=YYYY-MM-DD&til=YYYY-MM-DD`
Returnerer timeforbrug i kWh fra eloverblik.

```json
{
  "fra": "2024-01-01",
  "til": "2024-01-31",
  "maalerid": "571313174100086790",
  "timeforbrug": {
    "2024-01-01T00:00:00Z": 0.453,
    "2024-01-01T01:00:00Z": 0.312
  },
  "total_kwh": 387.4
}
```

### `GET /api/spotpriser?fra=YYYY-MM-DD&til=YYYY-MM-DD&zone=DK1`
Returnerer spotpriser i DKK/kWh pr. time.

### `GET /api/maaned?aar=YYYY&maaned=MM`
Kombinerer forbrug × spotpris + afgifter. Returnerer samlet månedspris.

```json
{
  "aar": 2024,
  "maaned": 1,
  "total_kwh": 387.4,
  "total_kr": 842.15,
  "gns_spotpris_kwh": 0.00152,
  "afgifter": {
    "elafgift_ore": 76.1,
    "nettarif_ore": 21.0,
    "elselskab_tillæg_ore": 10.0,
    "moms_pct": 25.0
  },
  "timer": [...]
}
```

---

## Prisberegning

For hver time:
```
Pris (DKK/kWh) = (spotpris + elafgift + nettarif + elselskabstillæg) × (1 + moms)
Timebeløb (DKK) = kWh × pris_per_kWh
```

Alle øre-satser konverteres til DKK ved division med 100.

---

## Lokal udvikling

**Backend:**
```bash
cd backend
pip install -r requirements.txt
cp .env.example .env  # Udfyld ELOVERBLIK_TOKEN
uvicorn main:app --reload
# API kører på http://localhost:8000
```

**Frontend:**
```bash
cd frontend
python3 -m http.server 3000
# Åbn http://localhost:3000
# Sæt backend URL til http://localhost:8000 i Indstillinger
```
