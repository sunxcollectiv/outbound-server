"""Seed the areas table with the world's countries (plus a few distinct travel
territories).

Run it any time — it's idempotent. Existing areas are matched case-insensitively
on ``(name, kind)``, and only the missing ones are inserted, so re-running never
duplicates and never touches rows you've already edited:

    uv run python -m app.seeds.countries

Editorial stance (all of this is just data below — edit to taste):

- Names follow how travelers commonly refer to a place, not formal/official
  names: "South Korea" not "Republic of Korea", "Russia" not "Russian
  Federation", "China" not "People's Republic of China", "Ivory Coast" not
  "Cote d'Ivoire".
- Taiwan is its own country.
- Sovereign states plus the entries most travelers simply think of as countries
  (Taiwan, Kosovo, Palestine, Vatican City) get ``kind="country"``.
- Non-sovereign places with their own border control that travelers treat as
  separate destinations (Hong Kong, Macau, Greenland, ...) get
  ``kind="territory"`` rather than "country". Collapse the two lists into one
  kind if you'd rather not draw that line.

``level`` is deliberately left NULL — prioritization/ranking is a later concern.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Area

COUNTRIES: list[str] = [
    "Afghanistan",
    "Albania",
    "Algeria",
    "Andorra",
    "Angola",
    "Antigua and Barbuda",
    "Argentina",
    "Armenia",
    "Australia",
    "Austria",
    "Azerbaijan",
    "Bahamas",
    "Bahrain",
    "Bangladesh",
    "Barbados",
    "Belarus",
    "Belgium",
    "Belize",
    "Benin",
    "Bhutan",
    "Bolivia",
    "Bosnia and Herzegovina",
    "Botswana",
    "Brazil",
    "Brunei",
    "Bulgaria",
    "Burkina Faso",
    "Burundi",
    "Cambodia",
    "Cameroon",
    "Canada",
    "Cape Verde",
    "Central African Republic",
    "Chad",
    "Chile",
    "China",
    "Colombia",
    "Comoros",
    "Republic of the Congo",
    "Democratic Republic of the Congo",
    "Costa Rica",
    "Croatia",
    "Cuba",
    "Cyprus",
    "Czech Republic",
    "Denmark",
    "Djibouti",
    "Dominica",
    "Dominican Republic",
    "East Timor",
    "Ecuador",
    "Egypt",
    "El Salvador",
    "Equatorial Guinea",
    "Eritrea",
    "Estonia",
    "Eswatini",
    "Ethiopia",
    "Fiji",
    "Finland",
    "France",
    "Gabon",
    "Gambia",
    "Georgia",
    "Germany",
    "Ghana",
    "Greece",
    "Grenada",
    "Guatemala",
    "Guinea",
    "Guinea-Bissau",
    "Guyana",
    "Haiti",
    "Honduras",
    "Hungary",
    "Iceland",
    "India",
    "Indonesia",
    "Iran",
    "Iraq",
    "Ireland",
    "Israel",
    "Italy",
    "Ivory Coast",
    "Jamaica",
    "Japan",
    "Jordan",
    "Kazakhstan",
    "Kenya",
    "Kiribati",
    "Kosovo",
    "Kuwait",
    "Kyrgyzstan",
    "Laos",
    "Latvia",
    "Lebanon",
    "Lesotho",
    "Liberia",
    "Libya",
    "Liechtenstein",
    "Lithuania",
    "Luxembourg",
    "Madagascar",
    "Malawi",
    "Malaysia",
    "Maldives",
    "Mali",
    "Malta",
    "Marshall Islands",
    "Mauritania",
    "Mauritius",
    "Mexico",
    "Micronesia",
    "Moldova",
    "Monaco",
    "Mongolia",
    "Montenegro",
    "Morocco",
    "Mozambique",
    "Myanmar",
    "Namibia",
    "Nauru",
    "Nepal",
    "Netherlands",
    "New Zealand",
    "Nicaragua",
    "Niger",
    "Nigeria",
    "North Korea",
    "North Macedonia",
    "Norway",
    "Oman",
    "Pakistan",
    "Palau",
    "Palestine",
    "Panama",
    "Papua New Guinea",
    "Paraguay",
    "Peru",
    "Philippines",
    "Poland",
    "Portugal",
    "Qatar",
    "Romania",
    "Russia",
    "Rwanda",
    "Saint Kitts and Nevis",
    "Saint Lucia",
    "Saint Vincent and the Grenadines",
    "Samoa",
    "San Marino",
    "Sao Tome and Principe",
    "Saudi Arabia",
    "Senegal",
    "Serbia",
    "Seychelles",
    "Sierra Leone",
    "Singapore",
    "Slovakia",
    "Slovenia",
    "Solomon Islands",
    "Somalia",
    "South Africa",
    "South Korea",
    "South Sudan",
    "Spain",
    "Sri Lanka",
    "Sudan",
    "Suriname",
    "Sweden",
    "Switzerland",
    "Syria",
    "Taiwan",
    "Tajikistan",
    "Tanzania",
    "Thailand",
    "Togo",
    "Tonga",
    "Trinidad and Tobago",
    "Tunisia",
    "Turkey",
    "Turkmenistan",
    "Tuvalu",
    "Uganda",
    "Ukraine",
    "United Arab Emirates",
    "United Kingdom",
    "United States",
    "Uruguay",
    "Uzbekistan",
    "Vanuatu",
    "Vatican City",
    "Venezuela",
    "Vietnam",
    "Yemen",
    "Zambia",
    "Zimbabwe",
]

TERRITORIES: list[str] = [
    "Hong Kong",
    "Macau",
    "Greenland",
    "Faroe Islands",
    "Gibraltar",
    "Bermuda",
    "Cayman Islands",
    "Aruba",
    "Curaçao",
    "French Polynesia",
    "New Caledonia",
    "Cook Islands",
]


def seed_areas(session: Session) -> dict[str, int]:
    """Insert any missing country/territory areas. Idempotent; returns counts."""
    existing = {
        (name.casefold(), kind)
        for name, kind in session.execute(select(Area.name, Area.kind)).all()
    }

    created = 0
    for kind, names in (("country", COUNTRIES), ("territory", TERRITORIES)):
        for name in names:
            key = (name.casefold(), kind)
            if key in existing:
                continue
            session.add(Area(name=name, kind=kind))
            existing.add(key)
            created += 1

    session.commit()
    total = len(COUNTRIES) + len(TERRITORIES)
    return {"created": created, "skipped": total - created, "total": total}


def main() -> None:
    with SessionLocal() as session:
        result = seed_areas(session)
    print(
        f"Seeded areas: created {result['created']}, "
        f"skipped {result['skipped']} already present "
        f"({result['total']} countries + territories in the list)."
    )


if __name__ == "__main__":
    main()
