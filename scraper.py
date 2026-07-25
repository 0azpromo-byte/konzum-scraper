"""
Konzum scraper -> Firestore ("cijene" kolekcija)

Konzum mehanizam:
- stranica /cjenici sadrži CSV linkove za sve poslovnice (sa stranicama)
- linkovi su <a href="/cjenici/download?title=...CSV">
- Bjelovar poslovnica je na stranici 2
- CSV je , (zarez) delimited, UTF-8 encoding
- CSV sadrži 12 stupaca

Pokretanje lokalno (brzi test, 100 artikala):
    LOKALNI_TEST=true python scraper.py

Puni run (2000 artikala, za GitHub):
    python scraper.py
"""

import csv
import hashlib
import io
import os
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore


# ---------- KONFIGURACIJA ----------
SERVICE_ACCOUNT = "serviceAccountKey.json"
TRGOVINA = "Konzum"
GRAD = "Bjelovar"
DELIMITER = ","
CSV_ENCODING = "utf-8"

BASE_URL = "https://www.konzum.hr"
INDEX_URL = "https://www.konzum.hr/cjenici"
PAGES_TO_CHECK = 5

LOKALNI_TEST = os.environ.get("LOKALNI_TEST", "false").lower() == "true"

if LOKALNI_TEST:
    KVOTA_HRANA = 50
    KVOTA_OSTALO_UKUPNO = 50
else:
    KVOTA_HRANA = 1000
    KVOTA_OSTALO_UKUPNO = 1000

KATEGORIJE_MAP = {
    "hrana": "HRANA",
    "piće": "PIĆE",
    "pića": "PIĆE",
    "pice": "PIĆE",
    "sredstva za čišćenje": "SREDSTVA ZA ČIŠĆENJE",
    "sredstva za ciscenje": "SREDSTVA ZA ČIŠĆENJE",
    "kozmetika": "KOZMETIKA",
    "toaletne potrepštine": "TOALETNE POTREPŠTINE",
    "proizvodi za kućanstvo": "PROIZVODI ZA KUĆANSTVO",
}

STUPAC_NAZIV = "NAZIV PROIZVODA"
STUPAC_SIFRA = "ŠIFRA PROIZVODA"
STUPAC_MARKA = "MARKA PROIZVODA"
STUPAC_KOLICINA = "NETO KOLIČINA"
STUPAC_JEDINICA = "JEDINICA MJERE"
STUPAC_CIJENA = "MALOPRODAJNA CIJENA"
STUPAC_BARKOD = "BARKOD"
STUPAC_KATEGORIJA = "KATEGORIJA PROIZVODA"
STUPAC_POSEB_OBLIK = "MPC ZA VRIJEME POSEBNOG OBLIKA PRODAJE"


# ---------- INICIJALIZACIJA ----------
if not firebase_admin._apps:
    cred = credentials.Certificate(SERVICE_ACCOUNT)
    firebase_admin.initialize_app(cred)
db = firestore.client()


# ---------- PRONALAŽENJE CSV-a ----------
def pronadji_csv_url() -> str | None:
    """Konzum objavljuje CSV linkove na stranicama /cjenici?page=N.
    Bjelovar je obično na stranici 2."""
    for page in range(1, PAGES_TO_CHECK + 1):
        url = f"{INDEX_URL}?page={page}"
        print(f"🔍 Provjeravam {url}...")
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content.decode("utf-8"), "html.parser")

        for link in soup.find_all("a", href=True):
            href = link["href"]
            if "/cjenici/download" in href and GRAD.upper() in href.upper():
                if href.startswith("/"):
                    return BASE_URL + href
                return href

    return None


def preuzmi_csv(csv_url: str) -> str | None:
    """Preuzima CSV i vraća dekodirani sadržaj."""
    resp = requests.get(csv_url, timeout=60)
    resp.raise_for_status()

    for enc in (CSV_ENCODING, "utf-8-sig", "cp1250"):
        try:
            return resp.content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Ne mogu dekodirati CSV")


# ---------- OBRADA CSV-a ----------
def normaliziraj_kategoriju(raw: str) -> str | None:
    if not raw:
        return None
    raw_lower = re.sub(r"\s+", " ", raw.strip().lower())
    for kljuc, vrijednost in KATEGORIJE_MAP.items():
        if kljuc in raw_lower:
            return vrijednost
    return raw.strip().upper()


def parsiraj_broj(raw: str) -> float | None:
    if not raw or raw.strip() in ("", "0"):
        return None
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError:
        return None


def obradi_csv(sadrzaj: str) -> list[dict]:
    proizvodi = []
    reader = csv.DictReader(io.StringIO(sadrzaj), delimiter=DELIMITER)

    for row in reader:
        barkod = row.get(STUPAC_BARKOD, "").strip()
        naziv = row.get(STUPAC_NAZIV, "").strip()
        marka = row.get(STUPAC_MARKA, "").strip()
        kolicina = row.get(STUPAC_KOLICINA, "").strip()
        jedinica = row.get(STUPAC_JEDINICA, "").strip()
        redovna_cijena_str = row.get(STUPAC_CIJENA, "").strip()
        kategorija_raw = row.get(STUPAC_KATEGORIJA, "").strip()
        akcijska_cijena_val = parsiraj_broj(row.get(STUPAC_POSEB_OBLIK, ""))

        if not barkod or not naziv:
            continue

        redovna_cijena = parsiraj_broj(redovna_cijena_str)

        if redovna_cijena is None and akcijska_cijena_val is None:
            continue

        kategorija = normaliziraj_kategoriju(kategorija_raw)
        if kategorija is None:
            continue

        if redovna_cijena is not None and akcijska_cijena_val is not None and akcijska_cijena_val < redovna_cijena:
            tip = "akcija"
            cijena = akcijska_cijena_val
            stara_cijena = redovna_cijena
        elif redovna_cijena is not None:
            tip = "redovno"
            cijena = redovna_cijena
            stara_cijena = None
        else:
            tip = "akcija"
            cijena = akcijska_cijena_val
            stara_cijena = None

        proizvodi.append({
            "barkod": barkod,
            "naziv": naziv,
            "marka": marka,
            "kolicina": f"{kolicina} {jedinica}".strip(),
            "cijena": cijena,
            "stara_cijena": stara_cijena,
            "trgovina": TRGOVINA,
            "grad": GRAD,
            "kategorija": kategorija,
            "tip": tip,
            "datum": datetime.now().strftime("%Y-%m-%d"),
        })

    return proizvodi


# ---------- ODABIR PROIZVODA ----------
def deterministicki_kljuc(p: dict) -> str:
    return hashlib.md5(p["barkod"].encode()).hexdigest()


def odaberi_proizvode(proizvodi: list[dict]) -> list[dict]:
    kategorije: dict[str, list[dict]] = {}
    for p in proizvodi:
        kategorije.setdefault(p["kategorija"], []).append(p)

    print("🔍 Pronađene kategorije:")
    for kat, lista in sorted(kategorije.items(), key=lambda x: -len(x[1])):
        print(f"   {kat}: {len(lista)}")

    odabrani = []

    hrana = sorted(kategorije.get("HRANA", []), key=deterministicki_kljuc)
    odabrani.extend(hrana[:KVOTA_HRANA])
    print(f"  📌 HRANA: odabrano {min(len(hrana), KVOTA_HRANA)}/{len(hrana)}")

    ostale_kat = {k: v for k, v in kategorije.items() if k != "HRANA"}
    ukupno_ostalo_dostupno = sum(len(v) for v in ostale_kat.values())

    for kat, lista in sorted(ostale_kat.items(), key=lambda x: -len(x[1])):
        udio = len(lista) / ukupno_ostalo_dostupno if ukupno_ostalo_dostupno else 0
        broj = round(udio * KVOTA_OSTALO_UKUPNO)
        broj = min(broj, len(lista))
        lista_sortirana = sorted(lista, key=deterministicki_kljuc)
        odabrani.extend(lista_sortirana[:broj])
        print(f"  📌 {kat}: odabrano {broj}/{len(lista)}")

    return odabrani


# ---------- SPREMANJE ----------
def spremi_u_firestore(proizvodi: list[dict], batch_size: int = 500) -> int:
    ukupno = len(proizvodi)
    print(f"📦 Spremanje {ukupno} proizvoda u Firestore...")
    batch = db.batch()
    brojac = 0
    for p in proizvodi:
        doc_id = f"{p['barkod']}_{p['trgovina']}_{p['grad']}".replace(" ", "_")
        doc_ref = db.collection("cijene").document(doc_id)
        data = {k: v for k, v in p.items() if v is not None}
        batch.set(doc_ref, data, merge=True)
        brojac += 1
        if brojac % batch_size == 0:
            batch.commit()
            print(f"  ✅ Spremljeno {brojac}/{ukupno}")
            batch = db.batch()
    if brojac % batch_size != 0:
        batch.commit()
        print(f"  ✅ Spremljeno {brojac}/{ukupno}")
    return brojac


# ---------- GLAVNI DIO ----------
def main():
    print("\n" + "=" * 50)
    print(f"🛒 {TRGOVINA} ({GRAD}) Automatski Scraper")
    nacin = f"LOKALNI TEST ({KVOTA_HRANA + KVOTA_OSTALO_UKUPNO} proizvoda)" if LOKALNI_TEST else f"PUNI RUN ({KVOTA_HRANA + KVOTA_OSTALO_UKUPNO} proizvoda)"
    print(f"   način rada: {nacin}")
    print("=" * 50 + "\n")

    csv_url = pronadji_csv_url()
    if not csv_url:
        print("❌ Nije pronađen CSV za Bjelovar!")
        return

    print(f"✅ Pronađen CSV: {csv_url}")

    print(f"📥 Preuzimam CSV za {GRAD}...")
    csv_sadrzaj = preuzmi_csv(csv_url)
    if not csv_sadrzaj:
        print("❌ CSV je prazan!")
        return

    print("🔄 Obrađujem CSV...")
    proizvodi = obradi_csv(csv_sadrzaj)
    if not proizvodi:
        print("❌ Nema proizvoda za obradu!")
        return
    print(f"✅ Ukupno proizvoda u CSV-u: {len(proizvodi)}")

    odabrani = odaberi_proizvode(proizvodi)
    print(f"\n📊 Ukupno odabrano za {TRGOVINA}: {len(odabrani)}")

    if odabrani:
        spremi_u_firestore(odabrani)
        print(f"\n✅ Završeno! Upisano {len(odabrani)} dokumenata za {TRGOVINA}.")
    else:
        print("❌ Nema odabranih proizvoda, ništa nije spremljeno.")


if __name__ == "__main__":
    main()
