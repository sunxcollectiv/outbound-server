"""Generate app/seeds/_cities_data.py — the curated city seed list.

Run with geonamescache available (a build-time tool, not a runtime dependency):

    uv run --with geonamescache python scripts/generate_cities.py

Composition (travel-curated hybrid):
  1. Every capital of a country/territory we seed.
  2. A hand-picked set of famous tourist cities/towns (below), regardless of
     size — the iconic places a population ranking misses (Venice, Salzburg,
     Siem Reap, Queenstown, ...).
  3. The remaining slots filled with the largest cities by population.

Those three are capped at TARGET entries, deduped on (city, country).

  4. A second, travel-curated batch (CURATED_TRAVEL below), appended AFTER the
     cap. The population fill turned out to be a poor proxy for where people
     travel (hundreds of large industrial cities, no Miami / Edinburgh / Porto),
     so further growth is picked by travel demand instead. Appending after the
     cap means the first TARGET entries never change when this batch grows.

Output is a plain list of (city, country) pairs using OUR country names, so the
seed just looks up the parent area by name.
"""
from __future__ import annotations

import datetime as dt
import unicodedata
from collections import defaultdict
from pathlib import Path

import geonamescache

from app.seeds.countries import COUNTRIES, TERRITORIES

TARGET = 1000

# geonames country name -> our area name, where they differ.
# Cape Verde, East Timor, Palestine, Curaçao and Vatican City are also named
# differently in geonames but are deliberately NOT mapped here: doing so would
# add their capitals in step 1 and push five cities out of the population fill,
# changing the already-seeded first TARGET entries. Their cities come from
# CURATED_TRAVEL instead.
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

# Second batch: places people actually travel to that steps 1-3 missed, keyed by
# our country name. Chosen by travel demand, not size: major destinations under
# the population cutoff, resort/beach/ski towns, gateways to parks, and real
# destinations in countries that only had their capital. Use the name travelers
# use ("Seville", not "Sevilla"). Entries already present (same name ignoring
# accents, or a GeoNames alternate name of a city we already have, e.g.
# "Cologne" for "Köln") are skipped and reported, so over-listing is harmless.
# Islands and regions (Bali, Tuscany) are not cities: see app/seeds/_regions_data.py.
CURATED_TRAVEL: dict[str, list[str]] = {
    # --- North America ---
    "United States": [
        "San Francisco", "Miami", "Las Vegas", "Boston", "Seattle", "New Orleans",
        "Nashville", "Honolulu", "Orlando", "Denver", "Portland", "Atlanta",
        "Salt Lake City", "Anchorage", "Palm Springs", "Moab", "Santa Barbara",
        "Scottsdale", "Fort Lauderdale", "Boulder", "Telluride", "Vail",
    ],
    "Canada": [
        "Vancouver", "Halifax", "Niagara Falls", "Tofino", "Lake Louise",
        "Mont-Tremblant",
    ],
    "Mexico": [
        "Cabo San Lucas", "San José del Cabo", "Puerto Escondido", "Sayulita",
        "La Paz", "Mazatlán", "Acapulco", "San Cristóbal de las Casas", "Bacalar",
    ],
    # --- Caribbean and Central America ---
    "Jamaica": ["Montego Bay", "Negril", "Ocho Rios"],
    "Dominican Republic": ["Punta Cana", "Puerto Plata"],
    "Cuba": ["Trinidad", "Varadero", "Viñales", "Santiago de Cuba"],
    "Saint Lucia": ["Soufrière"],
    "Curaçao": ["Willemstad"],
    "Belize": ["San Pedro", "Placencia", "San Ignacio"],
    "Guatemala": ["Flores", "Panajachel"],
    "Honduras": ["Copán Ruinas"],
    "El Salvador": ["El Tunco"],
    "Nicaragua": ["Granada", "León", "San Juan del Sur"],
    "Costa Rica": [
        "La Fortuna", "Tamarindo", "Monteverde", "Manuel Antonio",
        "Puerto Viejo de Talamanca", "Jacó", "Santa Teresa", "Nosara",
    ],
    "Panama": ["Bocas del Toro", "Boquete"],
    # --- South America ---
    "Colombia": ["Santa Marta", "Salento", "Villa de Leyva", "Guatapé", "Barichara"],
    "Ecuador": ["Cuenca", "Baños", "Otavalo", "Montañita", "Puerto Ayora"],
    "Peru": [
        "Aguas Calientes", "Ollantaytambo", "Puno", "Huaraz", "Iquitos", "Paracas",
        "Huacachina", "Máncora",
    ],
    "Bolivia": ["Uyuni", "Copacabana"],
    "Chile": [
        "San Pedro de Atacama", "Puerto Natales", "Punta Arenas", "Pucón",
        "Viña del Mar", "Hanga Roa",
    ],
    "Argentina": [
        "Salta", "Puerto Iguazú", "El Chaltén", "San Martín de los Andes",
        "Puerto Madryn", "Cafayate",
    ],
    "Uruguay": ["Punta del Este", "Colonia del Sacramento", "José Ignacio"],
    "Brazil": [
        "Paraty", "Ouro Preto", "Jericoacoara", "Porto de Galinhas", "Bonito",
        "Arraial do Cabo", "Petrópolis", "Gramado", "Olinda", "Trancoso",
    ],
    # --- Europe ---
    "United Kingdom": [
        "Edinburgh", "Glasgow", "Manchester", "Liverpool", "Bristol", "Brighton",
        "Belfast", "Cardiff", "Newcastle upon Tyne", "St Andrews",
    ],
    "Ireland": ["Limerick", "Kinsale"],
    "France": [
        "Lyon", "Bordeaux", "Toulouse", "Montpellier", "Saint-Malo", "Arles",
        "Versailles", "La Rochelle", "Honfleur",
    ],
    "Spain": [
        "Seville", "Valencia", "Málaga", "Bilbao", "Palma", "Marbella", "Cádiz",
        "Alicante", "Las Palmas de Gran Canaria",
    ],
    "Portugal": ["Porto", "Cascais", "Albufeira", "Funchal", "Ponta Delgada"],
    "Italy": [
        "Genoa", "Bari", "Palermo", "Catania", "Syracuse", "Cagliari", "San Gimignano",
        "Lecce", "Cortina d'Ampezzo", "Bellagio",
    ],
    "Germany": [
        "Frankfurt", "Düsseldorf", "Stuttgart", "Dresden", "Leipzig", "Nuremberg",
        "Freiburg",
    ],
    "Austria": ["Linz", "Kitzbühel", "Zell am See"],
    "Switzerland": ["Zurich", "Geneva", "Basel", "Lauterbrunnen"],
    "Netherlands": ["The Hague", "Utrecht"],
    "Denmark": ["Aarhus"],
    "Sweden": ["Gothenburg", "Malmö"],
    "Norway": ["Trondheim", "Geiranger", "Longyearbyen"],
    "Finland": ["Turku"],
    "Iceland": ["Húsavík"],
    "Estonia": ["Tartu"],
    "Latvia": ["Jūrmala"],
    "Lithuania": ["Kaunas"],
    "Poland": ["Kraków", "Gdańsk", "Wrocław", "Zakopane"],
    "Czech Republic": ["Brno", "Kutná Hora"],
    "Slovakia": ["Košice"],
    "Hungary": ["Eger", "Pécs"],
    "Croatia": ["Pula", "Korčula", "Makarska", "Šibenik"],
    "Bosnia and Herzegovina": ["Mostar"],
    "Serbia": ["Novi Sad"],
    "Montenegro": ["Herceg Novi"],
    "North Macedonia": ["Ohrid"],
    "Albania": ["Berat", "Sarandë"],
    "Kosovo": ["Prizren"],
    "Romania": ["Brașov", "Sibiu", "Cluj-Napoca"],
    "Bulgaria": ["Plovdiv", "Varna"],
    "Greece": ["Thessaloniki", "Oia", "Kalambaka", "Rethymno"],
    "Cyprus": ["Limassol", "Paphos", "Larnaca"],
    "Malta": ["Sliema", "St. Julian's"],
    "Turkey": ["Kaş", "Marmaris", "Kuşadası", "Alanya", "Çeşme"],
    "Georgia": ["Batumi", "Stepantsminda"],
    "Armenia": ["Dilijan"],
    "Azerbaijan": ["Sheki"],
    "Russia": ["Sochi"],
    "Ukraine": ["Lviv"],
    # --- Middle East and Africa ---
    "Israel": ["Tel Aviv", "Haifa", "Eilat", "Nazareth"],
    "Palestine": ["Bethlehem", "Ramallah"],
    "Jordan": ["Jerash", "Madaba"],
    "Lebanon": ["Byblos"],
    "United Arab Emirates": ["Ras Al Khaimah"],
    "Oman": ["Salalah", "Nizwa"],
    "Saudi Arabia": ["AlUla"],
    "Iran": ["Yazd"],
    "Egypt": ["Dahab", "Marsa Alam", "Siwa"],
    "Morocco": ["Agadir", "Meknes", "Ouarzazate", "Merzouga"],
    "Tunisia": ["Sousse", "Hammamet", "Sidi Bou Said"],
    "Cape Verde": ["Praia", "Mindelo", "Santa Maria"],
    "Senegal": ["Saint-Louis"],
    "Ghana": ["Cape Coast"],
    "Kenya": ["Diani Beach", "Malindi", "Lamu", "Naivasha"],
    "Tanzania": ["Moshi", "Nungwi", "Karatu"],
    "Uganda": ["Entebbe", "Jinja"],
    "Rwanda": ["Musanze"],
    "Ethiopia": ["Lalibela", "Gondar"],
    "South Africa": [
        "Franschhoek", "Hermanus", "Plettenberg Bay", "Oudtshoorn", "Hoedspruit",
    ],
    "Namibia": ["Swakopmund", "Walvis Bay"],
    "Botswana": ["Maun", "Kasane"],
    "Zimbabwe": ["Victoria Falls"],
    "Mozambique": ["Vilankulo", "Tofo"],
    "Mauritius": ["Grand Baie", "Flic en Flac"],
    # --- Asia ---
    "Japan": [
        "Naha", "Hakodate", "Otaru", "Niseko", "Furano", "Matsumoto",
        "Fujikawaguchiko", "Nagasaki", "Kagoshima", "Beppu", "Himeji", "Koyasan",
        "Shirakawa-go", "Ishigaki",
    ],
    "South Korea": ["Jeju City", "Gyeongju", "Jeonju", "Sokcho", "Gangneung"],
    "China": [
        "Lhasa", "Dali", "Yangshuo", "Shangri-La", "Pingyao", "Huangshan", "Dunhuang",
        "Kashgar",
    ],
    "Taiwan": ["Hualien", "Jiufen", "Taitung"],
    "Thailand": [
        "Pattaya", "Hua Hin", "Chiang Rai", "Sukhothai", "Kanchanaburi", "Ao Nang",
        "Khao Lak", "Patong",
    ],
    "Vietnam": ["Da Lat", "Ninh Binh", "Mui Ne", "Quy Nhon", "Ha Giang", "Phong Nha"],
    "Cambodia": ["Sihanoukville", "Kampot", "Battambang"],
    "Laos": ["Pakse"],
    "Myanmar": ["Bagan"],
    "Malaysia": [
        "George Town", "Malacca", "Ipoh", "Kota Kinabalu", "Kuching", "Semporna",
    ],
    "Indonesia": [
        "Seminyak", "Canggu", "Kuta", "Uluwatu", "Sanur", "Nusa Dua", "Labuan Bajo",
        "Manado",
    ],
    "Philippines": [
        "El Nido", "Coron", "Puerto Princesa", "Baguio", "Vigan", "Dumaguete",
    ],
    "East Timor": ["Dili"],
    "India": [
        "Jaisalmer", "Kochi", "Darjeeling", "Shimla", "Manali", "Leh", "Hampi",
        "Pondicherry", "Munnar", "Alleppey", "Pushkar",
    ],
    "Sri Lanka": ["Sigiriya", "Nuwara Eliya", "Mirissa", "Trincomalee", "Anuradhapura"],
    "Nepal": ["Bhaktapur", "Lumbini", "Namche Bazaar"],
    "Bhutan": ["Paro", "Punakha"],
    "Bangladesh": ["Cox's Bazar"],
    "Pakistan": ["Skardu"],
    "Maldives": ["Maafushi"],
    "Uzbekistan": ["Samarkand", "Bukhara", "Khiva"],
    "Kyrgyzstan": ["Karakol"],
    # --- Oceania ---
    "Australia": [
        "Gold Coast", "Hobart", "Darwin", "Launceston", "Broome", "Noosa Heads",
        "Airlie Beach", "Margaret River", "Fremantle", "Yulara",
    ],
    "New Zealand": [
        "Christchurch", "Dunedin", "Taupō", "Te Anau", "Kaikōura", "Franz Josef",
        "Paihia",
    ],
    "Palau": ["Koror"],
}


def _norm(name: str) -> str:
    return name.casefold()


def _fold(name: str) -> str:
    """Accent- and case-insensitive key ("Málaga" == "malaga"). Used for the
    second batch only: steps 1-3 keep ``_norm`` so their output stays stable."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


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

    # 4. Second batch: travel-curated additions, appended after the cap so the
    #    TARGET entries above are untouched however long this list grows.
    #    geo_names[country][folded name or alternate name] -> folded primary
    #    names, to spot an entry that is another spelling of a city we have.
    geo_names: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for c in cities:
        country = our_country(c["countrycode"])
        if not country:
            continue
        primary = _fold(c["name"])
        for label in (c["name"], *c.get("alternatenames", ())):
            if label:
                geo_names[country][_fold(label)].add(primary)

    have = {(_fold(city), _fold(country)) for city, country in picked}
    unknown_travel: list[str] = []
    already: list[str] = []
    aliases: list[str] = []
    not_in_geonames: list[str] = []
    added_travel = 0
    for country, towns in CURATED_TRAVEL.items():
        if country not in ours:
            unknown_travel.append(country)
            continue
        for town in towns:
            key = (_fold(town), _fold(country))
            if key in have:
                already.append(f"{town} ({country})")
                continue
            primaries = geo_names[country].get(_fold(town), set())
            existing = sorted(p for p in primaries if (p, _fold(country)) in have)
            if existing:
                aliases.append(f"{town} ({country}) = {existing[0]}")
                continue
            if not primaries:
                not_in_geonames.append(f"{town} ({country})")
            have.add(key)
            picked.append((town, country))
            added_travel += 1

    picked.sort(key=lambda t: (t[1], t[0]))

    out = Path("app/seeds/_cities_data.py")
    lines = [
        '"""Generated by scripts/generate_cities.py — do not edit by hand.',
        "",
        f"{len(picked)} curated cities/towns as (city, country) pairs, country names",
        f"matching app.seeds.countries: {len(picked) - added_travel} capitals, tourist towns and",
        f"largest cities, plus {added_travel} travel-curated additions. Regenerate with:",
        "    uv run --with geonamescache python scripts/generate_cities.py",
        f'Generated {dt.date.today().isoformat()}.',
        '"""',
        "",
        "CITIES: list[tuple[str, str]] = [",
    ]
    lines += [f"    ({city!r}, {country!r})," for city, country in picked]
    lines += ["]", ""]
    out.write_text("\n".join(lines), encoding="utf-8")

    if unknown_curated or unknown_travel:
        print(
            "WARNING: curated countries not in areas list:",
            unknown_curated + unknown_travel,
        )
    if already:
        print(f"Travel batch, already present ({len(already)}):", ", ".join(already))
    if aliases:
        print(f"Travel batch, alternate name of an existing city ({len(aliases)}):", ", ".join(aliases))
    if not_in_geonames:
        print(
            f"Travel batch, not in GeoNames; small place, check spelling ({len(not_in_geonames)}):",
            ", ".join(not_in_geonames),
        )
    print(f"Wrote {len(picked)} cities to {out} ({added_travel} from the travel batch)")


if __name__ == "__main__":
    main()
