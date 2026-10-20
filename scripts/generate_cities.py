"""Generate app/seeds/_cities_data.py — the curated ~1000-city seed list.

Run with geonamescache available (a build-time tool, not a runtime dependency):

    uv run --with geonamescache python scripts/generate_cities.py

Composition (travel-curated hybrid):
  1. Every capital of a country/territory we seed.
  2. A hand-picked set of famous tourist cities/towns (below), regardless of
     size — the iconic places a population ranking misses (Venice, Salzburg,
     Siem Reap, Queenstown, ...).
  3. The remaining slots filled with the largest cities by population.

Capped at TARGET entries, deduped on (city, country). Output is a plain list of
(city, country) pairs using OUR country names, so the seed just looks up the
parent area by name.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import geonamescache

from app.seeds.countries import COUNTRIES, TERRITORIES

TARGET = 1000

# geonames country name -> our area name, where they differ.
COUNTRY_NAME_OVERRIDES = {
    "Czechia": "Czech Republic",
    "Macao": "Macau",
    "The Netherlands": "Netherlands",
}

# Famous tourist cities/towns regardless of population, keyed by our country name.
# Big metros (Rome, Tokyo, Bangkok, ...) are intentionally omitted here — they
# arrive via the population fill. This list is for the smaller gems.
CURATED: dict[str, list[str]] = {
    "Italy": [
        "Venice", "Florence", "Siena", "Pisa", "Verona", "Bologna", "Positano",
        "Sorrento", "Amalfi", "Como", "Lucca", "Ravenna", "Matera", "Assisi",
        "Portofino", "Taormina", "Bergamo",
    ],
    "France": [
        "Nice", "Cannes", "Antibes", "Avignon", "Aix-en-Provence", "Colmar",
        "Strasbourg", "Annecy", "Chamonix", "Biarritz", "Carcassonne",
        "Saint-Tropez", "Bayeux", "Dijon", "Reims",
    ],
    "Spain": [
        "Granada", "Córdoba", "Toledo", "Santiago de Compostela", "San Sebastián",
        "Segovia", "Ronda", "Salamanca", "Girona", "San Sebastian",
    ],
    "Portugal": ["Sintra", "Lagos", "Évora", "Óbidos", "Faro", "Coimbra"],
    "Switzerland": [
        "Lucerne", "Interlaken", "Zermatt", "Lugano", "Lausanne", "Montreux",
        "St. Moritz", "Grindelwald",
    ],
    "Austria": ["Salzburg", "Innsbruck", "Hallstatt", "Graz"],
    "Germany": [
        "Heidelberg", "Rothenburg ob der Tauber", "Füssen", "Baden-Baden",
        "Garmisch-Partenkirchen",
    ],
    "Netherlands": ["Delft", "Haarlem", "Leiden", "Maastricht", "Giethoorn"],
    "Belgium": ["Bruges", "Ghent", "Antwerp"],
    "Croatia": ["Dubrovnik", "Split", "Hvar", "Rovinj", "Zadar"],
    "Greece": [
        "Santorini", "Mykonos", "Rhodes", "Heraklion", "Chania", "Nafplio",
        "Corfu", "Delphi",
    ],
    "Czech Republic": ["Český Krumlov", "Karlovy Vary"],
    "Slovenia": ["Bled", "Piran"],
    "Montenegro": ["Kotor", "Budva"],
    "Norway": ["Bergen", "Tromsø", "Ålesund", "Flåm", "Stavanger"],
    "Iceland": ["Akureyri", "Vík"],
    "Finland": ["Rovaniemi"],
    "United Kingdom": [
        "Bath", "Oxford", "Cambridge", "York", "Inverness", "Windsor",
        "Stratford-upon-Avon",
    ],
    "Ireland": ["Galway", "Cork", "Killarney", "Kilkenny", "Dingle"],
    "Turkey": ["Antalya", "Bodrum", "Cappadocia", "Göreme", "Fethiye", "Pamukkale"],
    "Morocco": ["Marrakesh", "Fez", "Chefchaouen", "Essaouira", "Tangier"],
    "Egypt": ["Luxor", "Aswan", "Hurghada", "Sharm El Sheikh"],
    "Jordan": ["Petra", "Wadi Musa", "Aqaba"],
    "Tanzania": ["Zanzibar City", "Arusha"],
    "Zambia": ["Livingstone"],
    "South Africa": ["Stellenbosch", "Knysna"],
    "Japan": [
        "Kyoto", "Nara", "Hakone", "Nikko", "Takayama", "Kanazawa", "Nagano",
        "Nikkō", "Kamakura",
    ],
    "Cambodia": ["Siem Reap"],
    "Laos": ["Luang Prabang", "Vang Vieng"],
    "Vietnam": ["Hoi An", "Hue", "Da Nang", "Nha Trang", "Sa Pa", "Ha Long"],
    "Thailand": ["Chiang Mai", "Krabi", "Phuket", "Ayutthaya", "Pai"],
    "Indonesia": ["Ubud", "Denpasar", "Yogyakarta"],
    "Nepal": ["Pokhara"],
    "India": ["Agra", "Jaipur", "Udaipur", "Jodhpur", "Varanasi", "Rishikesh"],
    "Sri Lanka": ["Kandy", "Galle", "Ella"],
    "China": ["Guilin", "Lijiang", "Zhangjiajie"],
    "Peru": ["Cusco", "Arequipa"],
    "Colombia": ["Cartagena"],
    "Mexico": [
        "San Miguel de Allende", "Oaxaca", "Mérida", "Tulum", "Playa del Carmen",
        "Guanajuato", "Puerto Vallarta",
    ],
    "Guatemala": ["Antigua Guatemala"],
    "Argentina": ["Bariloche", "Mendoza", "El Calafate", "Ushuaia"],
    "Chile": ["Valparaíso", "Puerto Varas"],
    "Brazil": ["Florianópolis", "Foz do Iguaçu", "Búzios"],
    "Canada": ["Banff", "Jasper", "Whistler", "Quebec City", "Victoria"],
    "United States": [
        "Aspen", "Savannah", "Charleston", "Santa Fe", "Key West", "Monterey",
        "Carmel-by-the-Sea", "Napa", "Sedona", "Asheville", "Jackson",
        "Park City", "Newport",
    ],
    "New Zealand": ["Queenstown", "Rotorua", "Wanaka", "Nelson"],
    "Australia": ["Cairns", "Byron Bay", "Port Douglas", "Alice Springs"],
    "Fiji": ["Nadi"],
}


def _norm(name: str) -> str:
    return name.casefold()


def main() -> None:
    gc = geonamescache.GeonamesCache()
    gc_countries = gc.get_countries()
    cities = sorted(
        gc.get_cities().values(),
        key=lambda c: c.get("population", 0),
        reverse=True,
    )

    ours = {n for n in COUNTRIES + TERRITORIES}
    ours_cf = {_norm(n) for n in ours}

    def our_country(code: str) -> str | None:
        meta = gc_countries.get(code)
        if not meta:
            return None
        name = COUNTRY_NAME_OVERRIDES.get(meta["name"], meta["name"])
        return name if _norm(name) in ours_cf else None

    picked: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(city: str, country: str) -> bool:
        key = (_norm(city), _norm(country))
        if key in seen:
            return False
        seen.add(key)
        picked.append((city, country))
        return True

    # 1. Capitals of every country/territory we recognize.
    for code, meta in gc_countries.items():
        country = our_country(code)
        capital = (meta.get("capital") or "").strip()
        if country and capital:
            add(capital, country)

    # 2. Curated tourist cities/towns.
    unknown_curated = []
    for country, towns in CURATED.items():
        if country not in ours:
            unknown_curated.append(country)
            continue
        for town in towns:
            add(town, country)

    # 3. Fill the rest with the biggest cities by population.
    for c in cities:
        if len(picked) >= TARGET:
            break
        country = our_country(c["countrycode"])
        if country:
            add(c["name"], country)

    picked = picked[:TARGET]
    picked.sort(key=lambda t: (t[1], t[0]))

    out = Path("app/seeds/_cities_data.py")
    lines = [
        '"""Generated by scripts/generate_cities.py — do not edit by hand.',
        "",
        f"Curated ~{TARGET} cities/towns as (city, country) pairs, country names",
        "matching app.seeds.countries. Regenerate with:",
        "    uv run --with geonamescache python scripts/generate_cities.py",
        f'Generated {dt.date.today().isoformat()}.',
        '"""',
        "",
        "CITIES: list[tuple[str, str]] = [",
    ]
    lines += [f"    ({city!r}, {country!r})," for city, country in picked]
    lines += ["]", ""]
    out.write_text("\n".join(lines), encoding="utf-8")

    if unknown_curated:
        print("WARNING: curated countries not in areas list:", unknown_curated)
    print(f"Wrote {len(picked)} cities to {out}")


if __name__ == "__main__":
    main()
