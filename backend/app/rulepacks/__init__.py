"""Scheme rule packs: deadlines, document lists, rate cards. Versioned YAML, loaded once.

A hospital can override a rate card by uploading a CSV (Setup > Rate cards); uploaded
cards live under DATA_DIR/rate_cards/<hospital_id>/<scheme>/ and win over the bundled
SAMPLE files.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from ..config import settings
from ..money import parse_date, to_paise

HERE = Path(__file__).parent


class Deadline(BaseModel):
    value: float
    unit: Literal["hours", "days"]
    label: str
    source: str
    from_: str = Field(alias="from")


class DocRequirement(BaseModel):
    doc: str
    when: str | None = None
    unless: str | None = None

    def applies(self, flags: dict[str, bool]) -> bool:
        if self.when and not flags.get(self.when):
            return False
        if self.unless and flags.get(self.unless):
            return False
        return True


class CondonationSlab(BaseModel):
    from_day: int
    to_day: int | None
    authority: str
    source: str


class Interest(BaseModel):
    rate_per_week_pct: float
    starts: str
    source: str


class CounterCheck(BaseModel):
    key: str
    label: str


class RateCardRef(BaseModel):
    file: str
    effective_from: str
    sample: bool = False


class RulePack(BaseModel):
    key: str
    name: str
    version: str
    portal: str
    kind: str
    preauth_required: bool
    preauth_waived_if_emergency: bool = False
    deadlines: dict[str, Deadline | None]
    condonation: list[CondonationSlab] = []
    interest: Interest | None = None
    counter_checks: list[CounterCheck] = []
    required_documents: dict[str, list[DocRequirement]]
    stall_days: dict[str, int] = {}
    rate_cards: list[RateCardRef] = []
    sources: list[dict] = []

    def deadline(self, name: str) -> Deadline | None:
        return self.deadlines.get(name)

    def docs_for(self, stage: str, flags: dict[str, bool]) -> list[str]:
        out: list[str] = []
        stages = ["preauth", "final"] if stage == "final" else [stage]
        for st in stages:
            for req in self.required_documents.get(st, []):
                if req.applies(flags) and req.doc not in out:
                    out.append(req.doc)
        return out

    def needs_preauth(self, emergency: bool) -> bool:
        return self.preauth_required and not (emergency and self.preauth_waived_if_emergency)

    def condonation_slab(self, days_after_discharge: int) -> CondonationSlab | None:
        for s in self.condonation:
            if days_after_discharge >= s.from_day and (s.to_day is None or days_after_discharge <= s.to_day):
                return s
        return None


@dataclass
class RateEntry:
    code: str
    description: str
    rate_paise: int
    los_days: int | None
    category: str
    sample: bool


@lru_cache
def all_packs() -> dict[str, RulePack]:
    packs = {}
    for f in sorted(HERE.glob("*.yaml")):
        if f.name == "clinical_rules.yaml":
            continue
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        packs[data["key"]] = RulePack.model_validate(data)
    return packs


def get_pack(key: str) -> RulePack:
    packs = all_packs()
    if key not in packs:
        raise KeyError(f"No rule pack '{key}'. Known: {', '.join(packs)}")
    return packs[key]


@lru_cache
def clinical_rules() -> dict:
    return yaml.safe_load((HERE / "clinical_rules.yaml").read_text(encoding="utf-8"))


def parse_rate_card(text: str, sample: bool = False) -> dict[str, RateEntry]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    out: dict[str, RateEntry] = {}
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        code = row.get("code")
        rate = to_paise(row.get("rate_inr") or row.get("rate"))
        if not code or rate is None:
            continue
        los = row.get("los_days")
        out[code.upper()] = RateEntry(code=code.upper(), description=row.get("description", ""), rate_paise=rate,
                                      los_days=int(los) if los and los.isdigit() else None,
                                      category=row.get("category", "package") or "package", sample=sample)
    return out


def _hospital_cards(hospital_id: str, scheme: str) -> list[tuple[date, Path]]:
    d = settings.data_dir / "rate_cards" / hospital_id / scheme
    if not d.exists():
        return []
    out = []
    for f in d.glob("*.csv"):
        eff = parse_date(f.stem.split("__")[0])  # files are saved as <YYYY-MM-DD>__<name>.csv
        if eff:
            out.append((eff, f))
    return out


def rate_card_for(scheme: str, hospital_id: str | None, service_date: date | None) -> tuple[dict[str, RateEntry], dict]:
    """Return (entries, meta). Hospital uploads beat bundled samples; the latest card
    effective on or before the service date wins."""
    service_date = service_date or date.today()
    candidates: list[tuple[date, Path, bool]] = []
    if hospital_id:
        candidates += [(eff, p, False) for eff, p in _hospital_cards(hospital_id, scheme)]
    if not candidates:
        pack = get_pack(scheme)
        for ref in pack.rate_cards:
            eff = parse_date(ref.effective_from)
            if eff:
                candidates.append((eff, HERE / "rate_cards" / ref.file, ref.sample))
    eligible = sorted([c for c in candidates if c[0] <= service_date], key=lambda c: c[0])
    if not eligible:
        return {}, {"loaded": False, "reason": "No rate card loaded for this scheme and date."}
    eff, path, sample = eligible[-1]
    return parse_rate_card(path.read_text(encoding="utf-8"), sample=sample), {
        "loaded": True, "file": path.name, "effective_from": eff.isoformat(), "sample": sample}


def save_hospital_rate_card(hospital_id: str, scheme: str, effective_from: date, filename: str, content: str) -> Path:
    entries = parse_rate_card(content)
    if not entries:
        raise ValueError("No rows found. The CSV needs the columns code, description, rate_inr (los_days, category optional).")
    d = settings.data_dir / "rate_cards" / hospital_id / scheme
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in Path(filename).stem if c.isalnum() or c in "-_")[:60] or "rates"
    p = d / f"{effective_from.isoformat()}__{safe}.csv"
    p.write_text(content, encoding="utf-8")
    return p
