"""Generate messy, Yardi-like multifamily CSV extracts into ``data/raw/``.

Run from the repo root::

    python -m generator.generate --seed 42 --out data/raw

How it works:
1. Simulate a clean, internally consistent portfolio: owners, properties
   (one changes owner mid-history), units, leases (new, renewal, transfer,
   early termination), a resident ledger (rent, concessions, fees, payments,
   bad debt) and prospect guest cards.
2. Inject a fixed list of real-world data-quality problems on the way out.
3. Measure those problems in the written rows and describe them in
   ``<out>/KNOWN_ISSUES.md``.

Determinism: all randomness comes from one ``random.Random(seed)`` and a Faker
instance seeded with the same value, and no wall-clock dates are used, so a
given seed always produces byte-identical files.
"""

from __future__ import annotations

import argparse
import csv
import random
import re
from calendar import monthrange
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

from faker import Faker

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_SEED = 42
DEFAULT_OUT = Path("data/raw")
HISTORY_START = date(2023, 10, 1)
AS_OF = date(2026, 9, 30)  # extract date: nothing after this exists yet
ACQUISITION_DATE = date(2018, 1, 1)

OWNERS: tuple[tuple[str, str], ...] = (
    ("OWN-001", "Harborview Residential Partners"),
    ("OWN-002", "Cedar Ridge Capital"),
)


@dataclass(frozen=True)
class PropertySpec:
    """Static description of one property and its ownership history."""

    property_id: str
    name: str
    city: str
    state: str
    unit_count: int
    rent_scale: float
    original_owner: str
    sold_to: str | None = None
    sale_date: date | None = None


PROPERTIES: tuple[PropertySpec, ...] = (
    PropertySpec("P100", "Lakeshore Commons", "Austin", "TX", 132, 1.00, "OWN-001"),
    PropertySpec("P200", "Maple Heights", "Charlotte", "NC", 108, 0.92, "OWN-001"),
    PropertySpec(
        "P300", "Riverbend Lofts", "Denver", "CO", 120, 1.15, "OWN-001",
        sold_to="OWN-002", sale_date=date(2025, 4, 1),
    ),
    PropertySpec("P400", "Sunset Terrace", "Phoenix", "AZ", 150, 0.95, "OWN-002"),
    PropertySpec("P500", "Cobblestone Park", "Atlanta", "GA", 96, 1.00, "OWN-002"),
)
PROPERTY_BY_ID = {p.property_id: p for p in PROPERTIES}


@dataclass(frozen=True)
class UnitType:
    """Floor-plan family with its share of the unit mix and base rent."""

    name: str
    share: float
    base_rent: int
    bedrooms: int
    bathrooms: float
    sqft_min: int
    sqft_max: int


UNIT_TYPES: tuple[UnitType, ...] = (
    UnitType("studio", 0.15, 1250, 0, 1.0, 450, 550),
    UnitType("1BR", 0.40, 1500, 1, 1.0, 650, 800),
    UnitType("2BR", 0.35, 1950, 2, 2.0, 950, 1150),
    UnitType("3BR", 0.10, 2450, 3, 2.0, 1250, 1450),
)
UNIT_TYPE_BY_NAME = {t.name: t for t in UNIT_TYPES}
UNITS_PER_FLOOR = 30
NON_REVENUE_SHARE = 0.03
NON_REVENUE_USES = ("model", "employee", "down")
MARKET_BASE_YEAR = 2023
MARKET_GROWTH = 0.035
MARKET_RENT_YEARS = range(2022, 2027)

# Lease behaviour
INITIAL_OCCUPANCY = 0.94
EARLY_TERMINATION_RATE = 0.06
RENEWAL_RATE = 0.52
RENEWAL_DECISION_DAYS = 60
BAD_DEBT_RATE = 0.04
TRANSFER_RATE = 0.03
MOVE_IN_OFFSET_RATE = 0.20
BACKDATED_MOVE_OUT_RATE = 0.06
NEW_LEASE_TERMS = (12,) * 8 + (6, 9, 13, 15)
RENEWAL_TERMS = (12,) * 6 + (6, 9, 15)

# Ledger
CONCESSION_RATE = 0.25
LATE_FEE = 75.00
CHARGE_CODES: tuple[tuple[str, str, str], ...] = (
    ("RENT", "Monthly rent", "rent"),
    ("CONC", "Rent concession", "concession"),
    ("LATE", "Late fee", "fee"),
    ("ETF", "Early termination fee", "fee"),
    ("PET", "Pet rent", "fee"),
    ("PARK", "Parking", "fee"),
    ("PMT", "Resident payment", "payment"),
)
CODE_SORT_ORDER = {"RENT": 0, "CONC": 1, "ETF": 2, "LATE": 3, "PMT": 9}
UNKNOWN_CHARGE_CODES = ("RNTADJ", "MISC99", "LATEFEE2", "PRKX")
UNKNOWN_CHARGE_COUNT = 8

# Guest cards
LEAD_SOURCES = (
    "ILS", "Property Website", "Walk-in", "Resident Referral", "Paid Search", "Social Media",
)
LEAD_SOURCE_WEIGHTS = (0.35, 0.25, 0.08, 0.10, 0.14, 0.08)
UNCONVERTED_STATUSES = ("Prospect", "Toured", "Cancelled", "Denied", "Lost")
UNCONVERTED_STATUS_WEIGHTS = (0.45, 0.25, 0.10, 0.05, 0.15)
UNCONVERTED_LEADS_PER_LEASE = 6
DUPLICATE_GUEST_RATE = 0.04
EMAIL_DOMAINS = ("example.com", "example.net", "example.org")

# Unit-code spellings per file (problem 1)
UNIT_CODE_FORMATS = {"ste": "STE-{n}", "suite": "Suite {n}", "bare": "{n}"}
LEASE_UNIT_CODE_MIX = {"ste": 0.80, "bare": 0.20}
CHARGE_UNIT_CODE_MIX = {"suite": 0.50, "bare": 0.35, "ste": 0.15}

Row = dict[str, object]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


class IdFactory:
    """Hands out sequential, prefixed IDs such as ``L000001``."""

    def __init__(self) -> None:
        self._counters: Counter[str] = Counter()

    def next(self, prefix: str, width: int = 6) -> str:
        """Return the next ID for ``prefix``."""
        self._counters[prefix] += 1
        return f"{prefix}{self._counters[prefix]:0{width}d}"


def add_months(d: date, months: int) -> date:
    """Add calendar months, clamping the day to the target month's length."""
    years, month_index = divmod(d.month - 1 + months, 12)
    year, month = d.year + years, month_index + 1
    return date(year, month, min(d.day, monthrange(year, month)[1]))


def month_start(d: date) -> date:
    """First day of ``d``'s month."""
    return d.replace(day=1)


def month_end(d: date) -> date:
    """Last day of ``d``'s month."""
    return d.replace(day=monthrange(d.year, d.month)[1])


def iter_months(first: date, last: date) -> Iterator[date]:
    """Yield month-start dates from ``first``'s month through ``last``'s month."""
    current = month_start(first)
    while current <= last:
        yield current
        current = add_months(current, 1)


def round_to(value: float, step: int = 5) -> float:
    """Round to the nearest ``step`` dollars (rents are quoted in round numbers)."""
    return float(step * round(value / step))


def days(n: int) -> timedelta:
    """Shorthand for ``timedelta(days=n)``."""
    return timedelta(days=n)


# --------------------------------------------------------------------------
# Units and market rent
# --------------------------------------------------------------------------


@dataclass
class Unit:
    """One apartment unit; ``unit_use`` other than residential is non-revenue."""

    property_id: str
    number: int
    unit_type: str
    sqft: int
    rent_premium: float
    unit_use: str = "residential"

    @property
    def code(self) -> str:
        """Canonical unit code as used in the unit master file."""
        return UNIT_CODE_FORMATS["ste"].format(n=self.number)

    @property
    def is_rentable(self) -> bool:
        """True for revenue units that can be leased."""
        return self.unit_use == "residential"


def unit_number_for_index(index: int) -> int:
    """Map a 0-based unit index to a floor-style number (0 -> 101, 30 -> 201)."""
    floor, position = divmod(index, UNITS_PER_FLOOR)
    return (floor + 1) * 100 + position + 1


def allocate_unit_types(count: int, rng: random.Random) -> list[str]:
    """Return a shuffled list of unit-type names matching the target mix."""
    names: list[str] = []
    for unit_type in UNIT_TYPES:
        names += [unit_type.name] * round(count * unit_type.share)
    names = (names + ["1BR"] * count)[:count]
    rng.shuffle(names)
    return names


def build_property_units(spec: PropertySpec, rng: random.Random) -> list[Unit]:
    """Create all units for one property, flagging ~3% as non-revenue."""
    units = []
    for index, type_name in enumerate(allocate_unit_types(spec.unit_count, rng)):
        unit_type = UNIT_TYPE_BY_NAME[type_name]
        units.append(Unit(
            property_id=spec.property_id,
            number=unit_number_for_index(index),
            unit_type=type_name,
            sqft=rng.randint(unit_type.sqft_min, unit_type.sqft_max),
            rent_premium=round(rng.uniform(0.97, 1.06), 3),
        ))
    non_revenue_count = max(1, round(spec.unit_count * NON_REVENUE_SHARE))
    for j, index in enumerate(sorted(rng.sample(range(len(units)), non_revenue_count))):
        units[index].unit_use = NON_REVENUE_USES[j % len(NON_REVENUE_USES)]
    return units


def build_units(rng: random.Random) -> list[Unit]:
    """Create units for every property."""
    return [unit for spec in PROPERTIES for unit in build_property_units(spec, rng)]


def market_rent(unit: Unit, on: date) -> float:
    """Asking (market) rent for ``unit`` on date ``on``; steps up each January."""
    spec = PROPERTY_BY_ID[unit.property_id]
    base = UNIT_TYPE_BY_NAME[unit.unit_type].base_rent
    growth = (1 + MARKET_GROWTH) ** (on.year - MARKET_BASE_YEAR)
    return round_to(base * spec.rent_scale * unit.rent_premium * growth)


# --------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------


@dataclass
class Lease:
    """One lease contract. ``bill_end`` and ``bad_debt_months`` are internal."""

    lease_id: str
    resident_id: str
    property_id: str
    unit_number: int
    lease_type: str  # new | renewal | transfer
    lease_start: date
    lease_end: date
    monthly_rent: float
    move_in: date | None = None
    notice_date: date | None = None
    move_out: date | None = None
    move_out_entered_on: date | None = None
    move_out_reason: str = ""
    bill_end: date = AS_OF
    bad_debt_months: int = 0


def new_lease(
    unit: Unit, resident_id: str, lease_type: str, start: date, term_months: int,
    rent: float, ids: IdFactory,
) -> Lease:
    """Construct a lease whose end date is the day before the term anniversary."""
    return Lease(
        lease_id=ids.next("L"),
        resident_id=resident_id,
        property_id=unit.property_id,
        unit_number=unit.number,
        lease_type=lease_type,
        lease_start=start,
        lease_end=add_months(start, term_months) - days(1),
        monthly_rent=rent,
    )


def first_lease_start(rng: random.Random) -> date:
    """Start date of the first lease simulated for a unit (most are in place already)."""
    if rng.random() < INITIAL_OCCUPANCY:
        return HISTORY_START - days(rng.randint(1, 330))
    return HISTORY_START + days(rng.randint(0, 45))


def vacancy_days(rng: random.Random) -> int:
    """Days a unit sits vacant between residents (make-ready plus marketing)."""
    return int(rng.gammavariate(2.0, 12.0)) + 5


def move_in_offset_days(rng: random.Random) -> int:
    """Days between lease_start and physical move-in; non-zero for ~20% of leases."""
    if rng.random() >= MOVE_IN_OFFSET_RATE:
        return 0
    if rng.random() < 0.2:
        return -rng.randint(1, 3)  # early key release
    return rng.randint(1, 10)


def draw_outcome(rng: random.Random) -> str:
    """Decide how a lease ends: early_termination, renew or move_out."""
    roll = rng.random()
    if roll < EARLY_TERMINATION_RATE:
        return "early_termination"
    if roll < EARLY_TERMINATION_RATE + RENEWAL_RATE:
        return "renew"
    return "move_out"


def renew(prev: Lease, unit: Unit, rng: random.Random, ids: IdFactory) -> Lease:
    """Create the renewal following ``prev``; increases are capped at market."""
    start = prev.lease_end + days(1)
    proposed = round_to(prev.monthly_rent * (1 + rng.uniform(0.02, 0.055)))
    rent = max(prev.monthly_rent, min(proposed, market_rent(unit, start)))
    return new_lease(unit, prev.resident_id, "renewal", start, rng.choice(RENEWAL_TERMS), rent, ids)


def planned_move_out(lease: Lease, first_move_in: date, outcome: str, rng: random.Random) -> date:
    """Physical move-out date the resident is heading for."""
    if outcome == "early_termination":
        earliest = max(lease.lease_start, first_move_in) + days(60)
        latest = max(earliest, lease.lease_end - days(30))
        return earliest + days(rng.randint(0, (latest - earliest).days))
    return max(lease.lease_end + days(rng.randint(-5, 3)), first_move_in + days(30))


def move_out_entry_lag(move_out: date, rng: random.Random) -> int:
    """Days between a move-out and when staff recorded it (problem 3)."""
    if rng.random() < BACKDATED_MOVE_OUT_RATE and move_out + days(45) <= AS_OF:
        return rng.randint(14, 45)
    return rng.randint(0, 3)


def close_out(
    lease: Lease, first_move_in: date, outcome: str, will_skip: bool, rng: random.Random,
) -> None:
    """Record notice and move-out on the resident's last lease, if they happened by AS_OF."""
    move_out = planned_move_out(lease, first_move_in, outcome, rng)
    notice = move_out - days(rng.randint(30, 60))
    if notice > AS_OF:
        return
    lease.notice_date = notice
    if move_out > AS_OF:
        return  # on notice at extract time
    lease.move_out = move_out
    lease.move_out_entered_on = min(move_out + days(move_out_entry_lag(move_out, rng)), AS_OF)
    if will_skip:
        lease.bad_debt_months = rng.randint(1, 3)
        lease.move_out_reason = "Eviction" if lease.bad_debt_months >= 2 else "Skip"
    elif outcome == "early_termination":
        lease.move_out_reason = "Early Termination"
    else:
        lease.move_out_reason = "Lease Expired"


def simulate_stay(unit: Unit, start: date, rng: random.Random, ids: IdFactory) -> list[Lease]:
    """Simulate one resident's leases in one unit: original lease plus renewals."""
    rent = round_to(market_rent(unit, start) * rng.uniform(0.96, 1.02))
    lease = new_lease(unit, ids.next("R"), "new", start, rng.choice(NEW_LEASE_TERMS), rent, ids)
    lease.move_in = start + days(move_in_offset_days(rng))
    stay, will_skip = [lease], rng.random() < BAD_DEBT_RATE
    while True:
        outcome = draw_outcome(rng)
        if outcome != "renew":
            close_out(lease, stay[0].move_in, outcome, will_skip, rng)
            break
        if lease.lease_end - days(RENEWAL_DECISION_DAYS) > AS_OF:
            break  # renewal not decided yet at extract time
        lease.bill_end = lease.lease_end
        lease = renew(lease, unit, rng, ids)
        stay.append(lease)
    lease.bill_end = lease.move_out or AS_OF
    return stay


def simulate_unit(unit: Unit, rng: random.Random, ids: IdFactory) -> list[Lease]:
    """Simulate the full lease history of one rentable unit."""
    leases: list[Lease] = []
    start = first_lease_start(rng)
    while start <= AS_OF:
        stay = simulate_stay(unit, start, rng, ids)
        leases.extend(stay)
        last_move_out = stay[-1].move_out
        if last_move_out is None:
            break
        start = last_move_out + days(vacancy_days(rng))
    return leases


def build_leases(units: Sequence[Unit], rng: random.Random, ids: IdFactory) -> list[Lease]:
    """Simulate leases for every rentable unit; non-revenue units get none."""
    return [
        lease for unit in units if unit.is_rentable for lease in simulate_unit(unit, rng, ids)
    ]


def find_transfer_target(
    out: Lease, candidates: Sequence[Lease], used: set[str],
) -> Lease | None:
    """Find a new lease in the same property that starts just after ``out`` moves out."""
    assert out.move_out is not None
    for lease in candidates:
        if (
            lease.lease_id not in used
            and lease.property_id == out.property_id
            and lease.unit_number != out.unit_number
            and lease.move_in is not None
            and out.move_out <= lease.move_in <= out.move_out + days(7)
        ):
            return lease
    return None


def apply_transfers(leases: list[Lease], rng: random.Random) -> int:
    """Turn ~3% of move-outs into on-site transfers by re-pointing a matching new lease.

    The departing resident takes over the new lease (and its renewals) in
    another unit of the same property. Returns the number of transfers made.
    """
    move_outs = [l for l in leases if l.move_out and l.move_out_reason == "Lease Expired"]
    new_leases = [l for l in leases if l.lease_type == "new" and l.lease_start >= HISTORY_START]
    used: set[str] = set()
    made = 0
    for out in sorted(rng.sample(move_outs, round(len(move_outs) * TRANSFER_RATE)),
                      key=lambda l: l.lease_id):
        target = find_transfer_target(out, new_leases, used)
        if target is None:
            continue
        old_resident = target.resident_id
        for lease in leases:
            if lease.resident_id == old_resident:
                lease.resident_id = out.resident_id
        target.lease_type = "transfer"
        out.move_out_reason = "Transfer"
        used.add(target.lease_id)
        made += 1
    return made


# --------------------------------------------------------------------------
# Resident ledger: charges and payments
# --------------------------------------------------------------------------


@dataclass
class Txn:
    """One ledger line. Charges are positive; concessions and payments negative."""

    property_id: str
    unit_number: int
    resident_id: str
    lease_id: str
    txn_date: date
    charge_code: str
    amount: float
    description: str


def lease_txn(lease: Lease, on: date, code: str, amount: float, description: str) -> Txn:
    """Build a ledger line attached to ``lease``."""
    return Txn(lease.property_id, lease.unit_number, lease.resident_id, lease.lease_id,
               on, code, round(amount, 2), description)


def rent_charges(lease: Lease) -> list[Txn]:
    """Monthly rent from lease_start to billing end, prorated in partial months."""
    end = min(lease.bill_end, AS_OF)
    charges = []
    for month in iter_months(max(lease.lease_start, HISTORY_START), end):
        period_start, period_end = max(month, lease.lease_start), min(month_end(month), end)
        if period_end < period_start:
            continue
        covered = (period_end - period_start).days + 1
        in_month = monthrange(month.year, month.month)[1]
        if covered == in_month:
            charges.append(lease_txn(lease, period_start, "RENT", lease.monthly_rent, "Rent"))
        else:
            amount = lease.monthly_rent * covered / in_month
            charges.append(lease_txn(lease, period_start, "RENT", amount, "Rent (prorated)"))
    return charges


def lease_charges(lease: Lease, rng: random.Random) -> list[Txn]:
    """All charges for one lease: rent, a possible move-in concession, and any ETF."""
    charges = rent_charges(lease)
    if (lease.lease_type == "new" and HISTORY_START <= lease.lease_start <= AS_OF
            and rng.random() < CONCESSION_RATE):
        amount = -lease.monthly_rent * rng.choice((0.5, 1.0))
        charges.append(lease_txn(lease, lease.lease_start, "CONC", amount, "Move-in special"))
    if lease.move_out_reason == "Early Termination" and lease.move_out:
        charges.append(lease_txn(lease, lease.move_out, "ETF", lease.monthly_rent,
                                 "Early termination fee"))
    return charges


def resident_payments(charges: list[Txn], bad_debt_months: int, rng: random.Random) -> list[Txn]:
    """Simulate one resident's payments (and late fees) against their charges.

    Each month the resident pays the running balance, sometimes late (plus a
    late fee) or only partially. Bad-debt residents stop paying for their
    final ``bad_debt_months`` months, leaving an unpaid balance.
    """
    p_late = rng.choices((0.03, 0.25, 0.5), weights=(0.85, 0.12, 0.03))[0]
    pays_partially = rng.random() < 0.03
    by_month: dict[date, list[Txn]] = defaultdict(list)
    for txn in charges:
        by_month[month_start(txn.txn_date)].append(txn)
    months = sorted(by_month)
    unpaid = set(months[-bad_debt_months:]) if bad_debt_months else set()
    balance, out = 0.0, []
    for month in months:
        month_txns = by_month[month]
        balance += sum(t.amount for t in month_txns)
        if month in unpaid:
            continue
        anchor, first_charge = month_txns[-1], min(t.txn_date for t in month_txns)
        if rng.random() < p_late:
            fee_date = max(month + days(5), first_charge)
            if fee_date <= AS_OF:
                out.append(replace(anchor, txn_date=fee_date, charge_code="LATE",
                                   amount=LATE_FEE, description="Late fee"))
                balance += LATE_FEE
            pay_day = rng.randint(8, 20)
        else:
            pay_day = rng.randint(1, 5)
        pay_date = max(month + days(pay_day - 1), first_charge)
        share = rng.uniform(0.5, 0.9) if pays_partially and rng.random() < 0.4 else 1.0
        amount = round(balance * share, 2)
        if pay_date > AS_OF or amount <= 0:
            continue
        out.append(replace(anchor, txn_date=pay_date, charge_code="PMT", amount=-amount,
                           description="Payment"))
        balance = round(balance - amount, 2)
    return out


def build_ledger(leases: Sequence[Lease], rng: random.Random) -> list[Txn]:
    """Charges for every lease plus each resident's payments, in posting order."""
    charges_by_resident: dict[str, list[Txn]] = defaultdict(list)
    for lease in sorted(leases, key=lambda l: (l.resident_id, l.lease_start)):
        charges_by_resident[lease.resident_id].extend(lease_charges(lease, rng))
    bad_debt = {l.resident_id: l.bad_debt_months for l in leases if l.bad_debt_months}
    ledger: list[Txn] = []
    for resident_id in sorted(charges_by_resident):
        charges = sorted(charges_by_resident[resident_id], key=lambda t: t.txn_date)
        ledger += charges + resident_payments(charges, bad_debt.get(resident_id, 0), rng)
    ledger.sort(key=lambda t: (t.txn_date, t.property_id, t.unit_number, t.resident_id,
                               CODE_SORT_ORDER.get(t.charge_code, 5)))
    return ledger


def inject_unknown_charge_codes(ledger: Sequence[Txn], rng: random.Random) -> None:
    """Relabel a few late fees with codes missing from charge_codes.csv (problem 5)."""
    fees = [t for t in ledger if t.charge_code == "LATE"]
    for txn in rng.sample(fees, min(UNKNOWN_CHARGE_COUNT, len(fees))):
        txn.charge_code = rng.choice(UNKNOWN_CHARGE_CODES)
        txn.description = "Misc adjustment"


# --------------------------------------------------------------------------
# Guest cards
# --------------------------------------------------------------------------


@dataclass
class GuestCard:
    """A prospect lead. Converted cards link to the resident they became."""

    property_id: str
    first_name: str
    last_name: str
    email: str
    phone: str
    source: str
    first_contact_date: date
    desired_move_in: date
    desired_unit_type: str
    status: str
    tour_date: date | None = None
    application_date: date | None = None
    resident_id: str = ""


class PersonFactory:
    """Creates fake prospects with unique phone numbers and example.* emails."""

    def __init__(self, rng: random.Random, fake: Faker) -> None:
        self._rng, self._fake, self._phones = rng, fake, set[str]()

    def phone(self) -> str:
        """Return a phone number not handed out before."""
        while True:
            r = self._rng
            number = f"({r.randint(201, 989)}) {r.randint(200, 999)}-{r.randint(0, 9999):04d}"
            if number not in self._phones:
                self._phones.add(number)
                return number

    def person(self) -> tuple[str, str, str, str]:
        """Return (first_name, last_name, email, phone)."""
        first, last = self._fake.first_name(), self._fake.last_name()
        handle = re.sub(r"[^a-z.]", "", f"{first}.{last}".lower())
        email = f"{handle}{self._rng.randint(1, 99)}@{self._rng.choice(EMAIL_DOMAINS)}"
        return first, last, email, self.phone()


def pick_source(rng: random.Random) -> str:
    """Draw a lead source using the marketing mix weights."""
    return rng.choices(LEAD_SOURCES, weights=LEAD_SOURCE_WEIGHTS)[0]


def converted_card(
    lease: Lease, unit_type: str, contact: date, people: PersonFactory, rng: random.Random,
) -> GuestCard:
    """Guest card for a prospect who signed ``lease``."""
    first, last, email, phone = people.person()
    tour = contact + days(rng.randint(0, 7))
    application = min(tour + days(rng.randint(0, 5)), lease.lease_start - days(1))
    return GuestCard(
        lease.property_id, first, last, email, phone, pick_source(rng), contact,
        lease.lease_start + days(rng.randint(-7, 7)), unit_type, "Leased",
        tour, application, lease.resident_id,
    )


def unconverted_card(people: PersonFactory, rng: random.Random) -> GuestCard:
    """Guest card for a prospect who did not lease."""
    spec = rng.choices(PROPERTIES, weights=[p.unit_count for p in PROPERTIES])[0]
    contact = HISTORY_START + days(rng.randint(0, (AS_OF - HISTORY_START).days))
    status = rng.choices(UNCONVERTED_STATUSES, weights=UNCONVERTED_STATUS_WEIGHTS)[0]
    unit_type = rng.choices([t.name for t in UNIT_TYPES], weights=[t.share for t in UNIT_TYPES])[0]
    tour = min(contact + days(rng.randint(0, 10)), AS_OF) if status in (
        "Toured", "Cancelled", "Denied") else None
    application = min(tour + days(rng.randint(0, 5)), AS_OF) if tour and status in (
        "Cancelled", "Denied") else None
    first, last, email, phone = people.person()
    return GuestCard(spec.property_id, first, last, email, phone, pick_source(rng), contact,
                     contact + days(rng.randint(14, 75)), unit_type, status, tour, application)


def typo(name: str, rng: random.Random) -> str:
    """Return ``name`` with one realistic typing error (drop, swap or double a letter)."""
    for _ in range(10):
        i = rng.randrange(1, len(name)) if len(name) > 1 else 0
        op = rng.choice(("drop", "swap", "double"))
        if op == "drop" and len(name) > 3:
            candidate = name[:i] + name[i + 1:]
        elif op == "swap" and i < len(name) - 1:
            candidate = name[:i] + name[i + 1] + name[i] + name[i + 2:]
        else:
            candidate = name[:i] + name[i] + name[i:]
        if candidate != name:
            return candidate
    return name + "e"


def duplicate_card(card: GuestCard, rng: random.Random) -> GuestCard:
    """Second card for the same person: name typo, other source, same phone (problem 2)."""
    first, last = card.first_name, card.last_name
    if rng.random() < 0.5:
        first = typo(first, rng)
    else:
        last = typo(last, rng)
    contact = card.first_contact_date + days(rng.randint(-5, 10))
    return replace(
        card, first_name=first, last_name=last,
        email=card.email if rng.random() < 0.5 else "",
        source=rng.choice([s for s in LEAD_SOURCES if s != card.source]),
        first_contact_date=min(max(contact, HISTORY_START), AS_OF),
        status="Prospect", tour_date=None, application_date=None, resident_id="",
    )


def build_guest_cards(
    leases: Sequence[Lease], units: Sequence[Unit], rng: random.Random, fake: Faker,
) -> list[GuestCard]:
    """Converted cards for new leases, unconverted leads, and injected duplicates."""
    people = PersonFactory(rng, fake)
    unit_type_of = {(u.property_id, u.number): u.unit_type for u in units}
    cards = []
    for lease in leases:
        contact = lease.lease_start - days(rng.randint(10, 60))
        if lease.lease_type == "new" and contact >= HISTORY_START and lease.lease_start <= AS_OF:
            unit_type = unit_type_of[(lease.property_id, lease.unit_number)]
            cards.append(converted_card(lease, unit_type, contact, people, rng))
    cards += [unconverted_card(people, rng)
              for _ in range(len(cards) * UNCONVERTED_LEADS_PER_LEASE)]
    originals = rng.sample(cards, round(len(cards) * DUPLICATE_GUEST_RATE))
    cards += [duplicate_card(card, rng) for card in originals]
    cards.sort(key=lambda c: (c.first_contact_date, c.property_id))
    return cards


# --------------------------------------------------------------------------
# Rendering rows
# --------------------------------------------------------------------------


def messy_unit_code(number: int, mix: dict[str, float], rng: random.Random) -> str:
    """Spell a unit code using one of the formats in ``mix`` (problem 1)."""
    style = rng.choices(list(mix), weights=list(mix.values()))[0]
    return UNIT_CODE_FORMATS[style].format(n=number)


def current_owner(spec: PropertySpec) -> str:
    """Owner of the property as of the extract date."""
    if spec.sold_to and spec.sale_date and spec.sale_date <= AS_OF:
        return spec.sold_to
    return spec.original_owner


def owner_rows() -> list[Row]:
    """Rows for owners.csv."""
    return [{"owner_id": oid, "owner_name": name} for oid, name in OWNERS]


def property_rows() -> list[Row]:
    """Rows for properties.csv (owner_id is the current owner only)."""
    return [{"property_id": p.property_id, "property_name": p.name, "city": p.city,
             "state": p.state, "unit_count": p.unit_count, "owner_id": current_owner(p)}
            for p in PROPERTIES]


def property_owner_rows() -> list[Row]:
    """Rows for property_owners.csv: effective-dated ownership history."""
    rows: list[Row] = []
    for p in PROPERTIES:
        sold = p.sold_to is not None and p.sale_date is not None
        rows.append({"property_id": p.property_id, "owner_id": p.original_owner,
                     "effective_from": ACQUISITION_DATE,
                     "effective_to": p.sale_date - days(1) if sold and p.sale_date else None})
        if sold:
            rows.append({"property_id": p.property_id, "owner_id": p.sold_to,
                         "effective_from": p.sale_date, "effective_to": None})
    return rows


def unit_rows(units: Sequence[Unit]) -> list[Row]:
    """Rows for units.csv (the unit master, canonical STE-### codes)."""
    rows: list[Row] = []
    for u in units:
        t = UNIT_TYPE_BY_NAME[u.unit_type]
        rows.append({
            "property_id": u.property_id, "unit_code": u.code, "unit_type": u.unit_type,
            "bedrooms": t.bedrooms, "bathrooms": t.bathrooms, "sqft": u.sqft,
            "market_rent": market_rent(u, AS_OF), "unit_use": u.unit_use,
            "exclude_from_occupancy": not u.is_rentable,
        })
    return rows


def market_rent_rows(units: Sequence[Unit]) -> list[Row]:
    """Rows for unit_market_rents.csv: market rent effective each January 1."""
    return [{"property_id": u.property_id, "unit_code": u.code,
             "effective_date": date(year, 1, 1), "market_rent": market_rent(u, date(year, 1, 1))}
            for u in units for year in MARKET_RENT_YEARS]


def lease_rows(leases: Sequence[Lease], rng: random.Random) -> list[Row]:
    """Rows for leases.csv, with inconsistently spelled unit codes."""
    ordered = sorted(leases, key=lambda l: (l.property_id, l.unit_number, l.lease_start))
    return [{
        "lease_id": l.lease_id, "resident_id": l.resident_id, "property_id": l.property_id,
        "unit_code": messy_unit_code(l.unit_number, LEASE_UNIT_CODE_MIX, rng),
        "lease_type": l.lease_type, "lease_start": l.lease_start, "lease_end": l.lease_end,
        "move_in": l.move_in, "notice_date": l.notice_date, "move_out": l.move_out,
        "move_out_entered_on": l.move_out_entered_on, "move_out_reason": l.move_out_reason,
        "monthly_rent": l.monthly_rent,
    } for l in ordered]


def charge_code_rows() -> list[Row]:
    """Rows for charge_codes.csv, the chart of valid ledger codes."""
    return [{"charge_code": c, "description": d, "category": k} for c, d, k in CHARGE_CODES]


def charge_rows(ledger: Sequence[Txn], rng: random.Random) -> list[Row]:
    """Rows for charges.csv, with inconsistently spelled unit codes."""
    return [{
        "transaction_id": f"T{i:07d}", "property_id": t.property_id,
        "unit_code": messy_unit_code(t.unit_number, CHARGE_UNIT_CODE_MIX, rng),
        "resident_id": t.resident_id, "lease_id": t.lease_id, "transaction_date": t.txn_date,
        "post_month": month_start(t.txn_date), "charge_code": t.charge_code,
        "description": t.description, "amount": t.amount,
    } for i, t in enumerate(ledger, start=1)]


def guest_card_rows(cards: Sequence[GuestCard]) -> list[Row]:
    """Rows for guest_cards.csv."""
    return [{
        "guest_card_id": f"GC{i:06d}", "property_id": c.property_id,
        "first_name": c.first_name, "last_name": c.last_name, "email": c.email,
        "phone": c.phone, "source": c.source, "first_contact_date": c.first_contact_date,
        "desired_move_in": c.desired_move_in, "desired_unit_type": c.desired_unit_type,
        "tour_date": c.tour_date, "application_date": c.application_date,
        "status": c.status, "resident_id": c.resident_id,
    } for i, c in enumerate(cards, start=1)]


# --------------------------------------------------------------------------
# Writing files
# --------------------------------------------------------------------------


def format_value(value: object) -> str:
    """Serialize one cell: ISO dates, 2-dp floats, Y/N booleans, blank for None."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Y" if value else "N"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def write_csv(path: Path, rows: Sequence[Row]) -> None:
    """Write rows to ``path`` with a header taken from the first row."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(rows[0].keys())
        writer.writerows([format_value(v) for v in row.values()] for row in rows)


# --------------------------------------------------------------------------
# Known-issues report (measured from the rendered rows)
# --------------------------------------------------------------------------


def unit_code_style(code: object) -> str:
    """Classify a unit-code spelling."""
    text = str(code)
    if text.startswith("STE-"):
        return "STE-###"
    if text.startswith("Suite "):
        return "Suite ###"
    return "### (bare number)" if text.isdigit() else "other"


def style_summary(rows: Sequence[Row]) -> str:
    """Human-readable count of unit-code spellings in a table."""
    counts = Counter(unit_code_style(r["unit_code"]) for r in rows)
    return ", ".join(f"`{style}` {n:,}" for style, n in sorted(counts.items()))


def duplicate_guest_groups(cards: Sequence[Row]) -> list[list[Row]]:
    """Guest cards sharing a phone number (one person, several cards)."""
    by_phone: dict[object, list[Row]] = defaultdict(list)
    for card in cards:
        by_phone[card["phone"]].append(card)
    return [group for group in by_phone.values() if len(group) > 1]


def backdated_move_outs(leases: Sequence[Row]) -> list[Row]:
    """Leases whose move-out was recorded 14+ days after it happened."""
    return [l for l in leases if l["move_out"] and isinstance(l["move_out"], date)
            and isinstance(l["move_out_entered_on"], date)
            and (l["move_out_entered_on"] - l["move_out"]).days >= 14]


def move_in_leases(leases: Sequence[Row]) -> list[Row]:
    """Leases that carry a physical move-in (new and transfer leases)."""
    return [l for l in leases if l["move_in"] is not None]


def move_in_mismatches(leases: Sequence[Row]) -> list[Row]:
    """Move-in leases whose move_in differs from lease_start."""
    return [l for l in move_in_leases(leases) if l["move_in"] != l["lease_start"]]


def unknown_code_charges(charges: Sequence[Row]) -> list[Row]:
    """Ledger lines whose charge_code is missing from charge_codes.csv."""
    known = {code for code, _, _ in CHARGE_CODES}
    return [c for c in charges if c["charge_code"] not in known]


def known_issues_markdown(tables: dict[str, list[Row]], seed: int) -> str:
    """Render KNOWN_ISSUES.md with counts measured from the generated rows."""
    leases, charges, cards = tables["leases"], tables["charges"], tables["guest_cards"]
    dup_groups = duplicate_guest_groups(cards)
    dup_example = dup_groups[0]
    backdated = backdated_move_outs(leases)
    mismatches, with_move_in = move_in_mismatches(leases), move_in_leases(leases)
    unknown = unknown_code_charges(charges)
    late = backdated[0]
    lines = [
        "# Known data-quality issues in `data/raw/`",
        "",
        f"Generated by `generator/generate.py` with seed `{seed}`. History "
        f"{HISTORY_START} to {AS_OF} (the extract date). Counts below are measured "
        "from the written files, not from the generator's settings.",
        "",
        "| File | Rows |",
        "|---|---:|",
        *[f"| `{name}.csv` | {len(rows):,} |" for name, rows in tables.items()],
        "",
        "## 1. Unit codes are spelled differently across files",
        "- **Where:** `unit_code` in `units.csv`, `unit_market_rents.csv`, `leases.csv`, "
        "`charges.csv`.",
        f"- **units.csv:** {style_summary(tables['units'])}",
        f"- **leases.csv:** {style_summary(leases)}",
        f"- **charges.csv:** {style_summary(charges)}",
        "- **Example:** unit `STE-300` can appear as `Suite 300` or `300`.",
        "- **Handle in silver:** normalize to the unit master's `STE-###` form by "
        "extracting the number and joining on (`property_id`, unit number).",
        "",
        "## 2. Duplicate guest cards for the same person",
        "- **Where:** `guest_cards.csv`.",
        f"- **Count:** {len(dup_groups):,} people have more than one card "
        f"({sum(len(g) for g in dup_groups) - len(dup_groups):,} extra cards).",
        "- **Pattern:** same phone; name has a one-letter typo; different `source`; "
        "email sometimes blank; first contact a few days apart.",
        "- **Example:** " + " vs ".join(
            f"`{c['guest_card_id']}` {c['first_name']} {c['last_name']} via {c['source']}"
            for c in dup_example) + f" (phone {dup_example[0]['phone']}).",
        "- **Handle in silver:** match on normalized phone/email (plus fuzzy name), keep the "
        "earliest card as the lead; credit the first source for attribution.",
        "",
        "## 3. Back-dated move-outs",
        "- **Where:** `leases.csv`: `move_out` vs `move_out_entered_on`.",
        f"- **Count:** {len(backdated):,} move-outs were entered 14-45 days after they "
        "happened (normal entry lag is 0-3 days).",
        f"- **Example:** lease `{late['lease_id']}` moved out {late['move_out']}, "
        f"entered {late['move_out_entered_on']}.",
        "- **Why it matters:** an occupancy snapshot taken between those dates overstated "
        "occupancy. Always compute history from `move_out`, never from entry date or "
        "snapshot tables.",
        "",
        "## 4. move_in differs from lease_start",
        "- **Where:** `leases.csv`, new and transfer leases (the rows that carry `move_in`).",
        f"- **Count:** {len(mismatches):,} of {len(with_move_in):,} "
        f"({len(mismatches) / len(with_move_in):.1%}); mostly 1-10 days late, some 1-3 "
        "days early (early key).",
        "- **Why it matters:** physical occupancy follows `move_in`/`move_out`; rent "
        "billing follows `lease_start`. Never substitute one for the other.",
        "",
        "## 5. Charges with an unknown charge code",
        "- **Where:** `charges.csv` `charge_code`, checked against `charge_codes.csv`.",
        f"- **Count:** {len(unknown):,} lines, codes: "
        + ", ".join(f"`{c}`" for c in sorted({str(r['charge_code']) for r in unknown})) + ".",
        "- **Handle in silver:** map to `UNKNOWN`, keep the amount (the ledger must still "
        "balance), and fail a dbt test so someone fixes the mapping.",
        "",
        "## Modeling notes (intentional, not defects)",
        "- **Ownership change:** `P300` moved from `OWN-001` to `OWN-002` on 2025-04-01. "
        "`properties.csv` shows only the current owner; `property_owners.csv` has the "
        "effective-dated history.",
        "- **Non-revenue units:** `unit_use` in (model, employee, down) with "
        "`exclude_from_occupancy = Y`; they never have leases.",
        "- **Renewals** carry no `move_in`/`move_out`; those dates sit on the lease where "
        "the physical event happened. `move_out` is on the resident's last lease in the unit.",
        "- **Transfers:** the old lease has `move_out_reason = Transfer`; the resident keeps "
        "the same `resident_id` on a `lease_type = transfer` lease in another unit.",
        "- **On notice:** a lease can have `notice_date` with no `move_out` yet.",
        "- **Future renewals:** a renewal may start after the extract date.",
        "- **Signs:** charges positive; concessions (`CONC`) and payments (`PMT`) negative. "
        "Payments are not applied to specific charges.",
        "- **Bad debt:** residents with `move_out_reason` Skip/Eviction stopped paying for "
        "their final 1-3 months; their balance is still open.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def build_tables(seed: int) -> dict[str, list[Row]]:
    """Run the full simulation and return every output table as rows."""
    rng = random.Random(seed)
    fake = Faker("en_US")
    fake.seed_instance(seed)
    ids = IdFactory()
    units = build_units(rng)
    leases = build_leases(units, rng, ids)
    apply_transfers(leases, rng)
    ledger = build_ledger(leases, rng)
    inject_unknown_charge_codes(ledger, rng)
    cards = build_guest_cards(leases, units, rng, fake)
    return {
        "owners": owner_rows(),
        "properties": property_rows(),
        "property_owners": property_owner_rows(),
        "units": unit_rows(units),
        "unit_market_rents": market_rent_rows(units),
        "leases": lease_rows(leases, rng),
        "charge_codes": charge_code_rows(),
        "charges": charge_rows(ledger, rng),
        "guest_cards": guest_card_rows(cards),
    }


def generate(seed: int = DEFAULT_SEED, out_dir: Path = DEFAULT_OUT) -> dict[str, int]:
    """Write all CSVs and KNOWN_ISSUES.md to ``out_dir``; return row counts per file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tables = build_tables(seed)
    for name, rows in tables.items():
        write_csv(out_dir / f"{name}.csv", rows)
    (out_dir / "KNOWN_ISSUES.md").write_text(
        known_issues_markdown(tables, seed), encoding="utf-8", newline="\n")
    return {name: len(rows) for name, rows in tables.items()}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="random seed")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output folder")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point: generate files and print row counts."""
    args = parse_args(argv)
    counts = generate(args.seed, args.out)
    for name, count in counts.items():
        print(f"{name + '.csv':<24}{count:>8,}")
    print(f"Wrote {len(counts)} CSVs and KNOWN_ISSUES.md to {args.out}")


if __name__ == "__main__":
    main()
