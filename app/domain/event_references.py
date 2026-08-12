"""Pinned country reference tables for deterministic systemic-event checks.

The language models identify countries, but only these immutable tables decide
whether a major-interstate-war assessment satisfies the product rules.

Snapshots:

* ``UN_MEMBERS_193_2026-03-29`` — the United Nations treaty-status page and
  Member States directory report 193 members. ISO 3166-1 alpha-2/alpha-3 codes
  are pinned here for those members:
  https://www.un.org/about-us/member-states
* ``G20_SOVEREIGN_MEMBERS_2025`` — the official G20 South Africa membership
  list names 19 sovereign countries plus the EU and AU. The two regional blocs
  are intentionally excluded:
  https://g20.org/wp-content/uploads/2024/12/G20-Brand-Identity-Guidelines_v3_02Dec24.pdf
* ``SIPRI_NUCLEAR_ARMED_STATES_2026`` — SIPRI Yearbook 2026 identifies nine
  nuclear-armed states:
  https://www.sipri.org/media/press-release/2026/increasing-focus-nuclear-weapons-amid-heightened-escalation-risks-new-sipri-yearbook-out-now

Updating any membership requires a deliberate code change and a new version
tag. No network lookup or mutable third-party country database participates in
event qualification.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class CountryReferenceTable:
    """A named immutable snapshot of ISO 3166-1 alpha-2 country codes."""

    version: str
    alpha2_codes: frozenset[str]


# Complete ISO 3166-1 alpha-3 -> alpha-2 mapping for the 193 UN members in the
# pinned UN snapshot. Keeping the mapping beside the membership set lets model
# output use either representation without delegating membership to a package.
_UN_ALPHA3_TO_ALPHA2: Final[Mapping[str, str]] = MappingProxyType(
    {
        "AFG": "AF",
        "ALB": "AL",
        "DZA": "DZ",
        "AND": "AD",
        "AGO": "AO",
        "ATG": "AG",
        "ARG": "AR",
        "ARM": "AM",
        "AUS": "AU",
        "AUT": "AT",
        "AZE": "AZ",
        "BHS": "BS",
        "BHR": "BH",
        "BGD": "BD",
        "BRB": "BB",
        "BLR": "BY",
        "BEL": "BE",
        "BLZ": "BZ",
        "BEN": "BJ",
        "BTN": "BT",
        "BOL": "BO",
        "BIH": "BA",
        "BWA": "BW",
        "BRA": "BR",
        "BRN": "BN",
        "BGR": "BG",
        "BFA": "BF",
        "BDI": "BI",
        "CPV": "CV",
        "KHM": "KH",
        "CMR": "CM",
        "CAN": "CA",
        "CAF": "CF",
        "TCD": "TD",
        "CHL": "CL",
        "CHN": "CN",
        "COL": "CO",
        "COM": "KM",
        "COG": "CG",
        "CRI": "CR",
        "CIV": "CI",
        "HRV": "HR",
        "CUB": "CU",
        "CYP": "CY",
        "CZE": "CZ",
        "PRK": "KP",
        "COD": "CD",
        "DNK": "DK",
        "DJI": "DJ",
        "DMA": "DM",
        "DOM": "DO",
        "ECU": "EC",
        "EGY": "EG",
        "SLV": "SV",
        "GNQ": "GQ",
        "ERI": "ER",
        "EST": "EE",
        "SWZ": "SZ",
        "ETH": "ET",
        "FJI": "FJ",
        "FIN": "FI",
        "FRA": "FR",
        "GAB": "GA",
        "GMB": "GM",
        "GEO": "GE",
        "DEU": "DE",
        "GHA": "GH",
        "GRC": "GR",
        "GRD": "GD",
        "GTM": "GT",
        "GIN": "GN",
        "GNB": "GW",
        "GUY": "GY",
        "HTI": "HT",
        "HND": "HN",
        "HUN": "HU",
        "ISL": "IS",
        "IND": "IN",
        "IDN": "ID",
        "IRN": "IR",
        "IRQ": "IQ",
        "IRL": "IE",
        "ISR": "IL",
        "ITA": "IT",
        "JAM": "JM",
        "JPN": "JP",
        "JOR": "JO",
        "KAZ": "KZ",
        "KEN": "KE",
        "KIR": "KI",
        "KWT": "KW",
        "KGZ": "KG",
        "LAO": "LA",
        "LVA": "LV",
        "LBN": "LB",
        "LSO": "LS",
        "LBR": "LR",
        "LBY": "LY",
        "LIE": "LI",
        "LTU": "LT",
        "LUX": "LU",
        "MDG": "MG",
        "MWI": "MW",
        "MYS": "MY",
        "MDV": "MV",
        "MLI": "ML",
        "MLT": "MT",
        "MHL": "MH",
        "MRT": "MR",
        "MUS": "MU",
        "MEX": "MX",
        "FSM": "FM",
        "MDA": "MD",
        "MCO": "MC",
        "MNG": "MN",
        "MNE": "ME",
        "MAR": "MA",
        "MOZ": "MZ",
        "MMR": "MM",
        "NAM": "NA",
        "NRU": "NR",
        "NPL": "NP",
        "NLD": "NL",
        "NZL": "NZ",
        "NIC": "NI",
        "NER": "NE",
        "NGA": "NG",
        "MKD": "MK",
        "NOR": "NO",
        "OMN": "OM",
        "PAK": "PK",
        "PLW": "PW",
        "PAN": "PA",
        "PNG": "PG",
        "PRY": "PY",
        "PER": "PE",
        "PHL": "PH",
        "POL": "PL",
        "PRT": "PT",
        "QAT": "QA",
        "KOR": "KR",
        "ROU": "RO",
        "RUS": "RU",
        "RWA": "RW",
        "KNA": "KN",
        "LCA": "LC",
        "VCT": "VC",
        "WSM": "WS",
        "SMR": "SM",
        "STP": "ST",
        "SAU": "SA",
        "SEN": "SN",
        "SRB": "RS",
        "SYC": "SC",
        "SLE": "SL",
        "SGP": "SG",
        "SVK": "SK",
        "SVN": "SI",
        "SLB": "SB",
        "SOM": "SO",
        "ZAF": "ZA",
        "SSD": "SS",
        "ESP": "ES",
        "LKA": "LK",
        "SDN": "SD",
        "SUR": "SR",
        "SWE": "SE",
        "CHE": "CH",
        "SYR": "SY",
        "TJK": "TJ",
        "THA": "TH",
        "TLS": "TL",
        "TGO": "TG",
        "TON": "TO",
        "TTO": "TT",
        "TUN": "TN",
        "TUR": "TR",
        "TKM": "TM",
        "TUV": "TV",
        "UGA": "UG",
        "UKR": "UA",
        "ARE": "AE",
        "GBR": "GB",
        "TZA": "TZ",
        "USA": "US",
        "URY": "UY",
        "UZB": "UZ",
        "VUT": "VU",
        "VEN": "VE",
        "VNM": "VN",
        "YEM": "YE",
        "ZMB": "ZM",
        "ZWE": "ZW",
    }
)

UN_MEMBER_STATES: Final = CountryReferenceTable(
    version="UN_MEMBERS_193_2026-03-29",
    alpha2_codes=frozenset(_UN_ALPHA3_TO_ALPHA2.values()),
)

G20_SOVEREIGN_MEMBERS: Final = CountryReferenceTable(
    version="G20_SOVEREIGN_MEMBERS_2025",
    alpha2_codes=frozenset(
        {
            "AR",
            "AU",
            "BR",
            "CA",
            "CN",
            "FR",
            "DE",
            "IN",
            "ID",
            "IT",
            "JP",
            "MX",
            "RU",
            "SA",
            "ZA",
            "KR",
            "TR",
            "GB",
            "US",
        }
    ),
)

NUCLEAR_ARMED_STATES: Final = CountryReferenceTable(
    version="SIPRI_NUCLEAR_ARMED_STATES_2026",
    alpha2_codes=frozenset({"US", "RU", "GB", "FR", "CN", "IN", "PK", "KP", "IL"}),
)


def normalize_iso_country_code(value: str | None) -> str | None:
    """Normalize a UN-member alpha-2/alpha-3 code to alpha-2.

    Syntactically valid two-letter values are returned as-is so callers can
    distinguish a well-formed non-member code from missing/invalid model output.
    Three-letter values must exist in the pinned UN mapping.
    """

    if value is None:
        return None
    normalized = value.strip().upper()
    if len(normalized) == 2 and normalized.isascii() and normalized.isalpha():
        return normalized
    if len(normalized) == 3 and normalized.isascii() and normalized.isalpha():
        return _UN_ALPHA3_TO_ALPHA2.get(normalized)
    return None


if len(_UN_ALPHA3_TO_ALPHA2) != 193 or len(UN_MEMBER_STATES.alpha2_codes) != 193:
    raise RuntimeError("pinned UN member-state reference table must contain exactly 193 states")
