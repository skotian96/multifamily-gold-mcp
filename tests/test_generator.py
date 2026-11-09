"""Tests for generator.generate: shape, determinism, and every injected problem.

Each problem is detected directly from the written CSVs, independently of the
generator's own KNOWN_ISSUES.md counts.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from generator.generate import AS_OF, DEFAULT_SEED, generate

Table = list[dict[str, str]]


@pytest.fixture(scope="module")
def raw_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate one dataset with the default seed for the whole module."""
    out = tmp_path_factory.mktemp("raw")
    generate(DEFAULT_SEED, out)
    return out


def read(raw_dir: Path, name: str) -> Table:
    """Read ``<name>.csv`` from the generated folder."""
    with (raw_dir / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def unit_key(row: dict[str, str]) -> tuple[str, str]:
    """(property_id, unit number) from any spelling of unit_code."""
    match = re.search(r"(\d+)\s*$", row["unit_code"])
    assert match, f"unparseable unit_code {row['unit_code']!r}"
    return row["property_id"], match.group(1)


def as_date(value: str) -> date | None:
    """Parse an ISO date cell; blank means None."""
    return date.fromisoformat(value) if value else None


# ---------------------------------------------------------------- shape


def test_row_counts(raw_dir: Path) -> None:
    """Portfolio has the requested shape and enough history to be useful."""
    units = read(raw_dir, "units")
    assert len(read(raw_dir, "owners")) == 2
    assert len(read(raw_dir, "properties")) == 5
    assert 570 <= len(units) <= 630
    assert 1_500 <= len(read(raw_dir, "leases")) <= 4_000
    assert len(read(raw_dir, "charges")) >= 30_000
    assert len(read(raw_dir, "guest_cards")) >= 3_000
    non_revenue = [u for u in units if u["unit_use"] != "residential"]
    assert 0.02 <= len(non_revenue) / len(units) <= 0.04
    assert {u["unit_use"] for u in non_revenue} == {"model", "employee", "down"}


def test_same_seed_is_byte_identical(raw_dir: Path, tmp_path: Path) -> None:
    """Re-running with the same seed reproduces every file exactly."""
    generate(DEFAULT_SEED, tmp_path)
    for path in raw_dir.iterdir():
        assert path.read_bytes() == (tmp_path / path.name).read_bytes(), path.name


# ---------------------------------------------------------------- domain sanity


def test_one_property_changes_owner(raw_dir: Path) -> None:
    """Exactly one property has two owners, with contiguous effective dates."""
    history: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read(raw_dir, "property_owners"):
        history[row["property_id"]].append(row)
    changed = {pid: rows for pid, rows in history.items() if len(rows) > 1}
    assert len(changed) == 1
    (first, second), = changed.values()
    assert first["owner_id"] != second["owner_id"]
    assert (as_date(second["effective_from"]) - as_date(first["effective_to"])).days == 1
    current = {p["property_id"]: p["owner_id"] for p in read(raw_dir, "properties")}
    assert current[second["property_id"]] == second["owner_id"]


def test_non_revenue_units_have_no_leases(raw_dir: Path) -> None:
    """Model/employee/down units are never leased."""
    non_revenue = {unit_key(u) for u in read(raw_dir, "units") if u["unit_use"] != "residential"}
    assert not non_revenue & {unit_key(l) for l in read(raw_dir, "leases")}


def test_every_unit_code_resolves_to_unit_master(raw_dir: Path) -> None:
    """Messy unit codes still identify a real unit once normalized."""
    master = {unit_key(u) for u in read(raw_dir, "units")}
    for name in ("leases", "charges"):
        assert {unit_key(r) for r in read(raw_dir, name)} <= master, name


def test_lease_dates_are_consistent(raw_dir: Path) -> None:
    """Notice precedes move-out; renewals carry no move-in; nothing after AS_OF is recorded."""
    for lease in read(raw_dir, "leases"):
        move_out, notice = as_date(lease["move_out"]), as_date(lease["notice_date"])
        if lease["lease_type"] == "renewal":
            assert not lease["move_in"]
        if move_out:
            assert notice and notice <= move_out
            assert move_out <= AS_OF
            assert as_date(lease["move_out_entered_on"]) >= move_out


def test_units_are_never_double_occupied(raw_dir: Path) -> None:
    """Physical stays in a unit (first move_in to last move_out) never overlap."""
    stays: dict[tuple[tuple[str, str], str], list[date | None]] = {}
    for lease in read(raw_dir, "leases"):
        key = (unit_key(lease), lease["resident_id"])
        move_in, move_out = as_date(lease["move_in"]), as_date(lease["move_out"])
        stay = stays.setdefault(key, [None, None])
        stay[0] = move_in or stay[0]
        stay[1] = move_out or stay[1]
    by_unit: dict[tuple[str, str], list[list[date | None]]] = defaultdict(list)
    for (unit, _), stay in stays.items():
        by_unit[unit].append(stay)
    for unit, unit_stays in by_unit.items():
        unit_stays.sort(key=lambda s: s[0])
        for prev, nxt in zip(unit_stays, unit_stays[1:]):
            assert prev[1] is not None and prev[1] < nxt[0], unit


# ---------------------------------------------------------------- injected problems


def unit_code_style(code: str) -> str:
    """Classify a unit-code spelling."""
    if code.startswith("STE-"):
        return "ste"
    if code.startswith("Suite "):
        return "suite"
    return "bare" if code.isdigit() else "other"


def test_problem_1_unit_codes_spelled_differently(raw_dir: Path) -> None:
    """Units use one spelling; leases and charges mix several."""
    styles = {name: {unit_code_style(r["unit_code"]) for r in read(raw_dir, name)}
              for name in ("units", "leases", "charges")}
    assert styles["units"] == {"ste"}
    assert len(styles["leases"]) >= 2
    assert {"ste", "suite", "bare"} <= styles["charges"]


def test_problem_2_duplicate_guest_cards(raw_dir: Path) -> None:
    """Some people have several cards with a name typo and a different source."""
    by_phone: dict[str, Table] = defaultdict(list)
    for card in read(raw_dir, "guest_cards"):
        by_phone[card["phone"]].append(card)
    dup_groups = [g for g in by_phone.values() if len(g) > 1]
    assert len(dup_groups) >= 20
    for group in dup_groups:
        names = {(c["first_name"], c["last_name"]) for c in group}
        assert len(names) > 1, "duplicate should have a name typo"
        assert len({c["source"] for c in group}) > 1, "duplicate should have another source"


def test_problem_3_backdated_move_outs(raw_dir: Path) -> None:
    """Some move-outs were entered two or more weeks after they happened."""
    lags = [(as_date(l["move_out_entered_on"]) - as_date(l["move_out"])).days
            for l in read(raw_dir, "leases") if l["move_out"]]
    backdated = [lag for lag in lags if lag >= 14]
    assert len(backdated) >= 10
    assert len(backdated) / len(lags) < 0.15, "back-dating should be the exception"


def test_problem_4_move_in_differs_from_lease_start(raw_dir: Path) -> None:
    """About 20% of move-in leases have move_in != lease_start."""
    with_move_in = [l for l in read(raw_dir, "leases") if l["move_in"]]
    differs = [l for l in with_move_in if l["move_in"] != l["lease_start"]]
    assert 0.15 <= len(differs) / len(with_move_in) <= 0.25


def test_problem_5_unknown_charge_codes(raw_dir: Path) -> None:
    """A few ledger lines use codes missing from charge_codes.csv."""
    known = {c["charge_code"] for c in read(raw_dir, "charge_codes")}
    unknown = [c for c in read(raw_dir, "charges") if c["charge_code"] not in known]
    assert 1 <= len(unknown) <= 20


def test_known_issues_report_lists_every_problem(raw_dir: Path) -> None:
    """KNOWN_ISSUES.md is written and has a section per injected problem."""
    report = (raw_dir / "KNOWN_ISSUES.md").read_text(encoding="utf-8")
    for heading in ("## 1. Unit codes", "## 2. Duplicate guest cards", "## 3. Back-dated",
                    "## 4. move_in differs", "## 5. Charges with an unknown charge code"):
        assert heading in report
