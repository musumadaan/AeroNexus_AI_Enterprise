"""Registry of the open, free datasets AeroNexus trains on — with provenance, license notes and checksums.

Every dataset records:
  * `source_of_record`: the official publisher page (cite this, not the mirror);
  * `mirrors`: where the fetch script downloads from (pinned files, verified by SHA-256 so a
    changed or tampered mirror is rejected rather than silently trained on);
  * `license` / `terms`: what you may do with it. US-government works (NASA, NTSB, FAA) are public
    domain in the US; others carry their own terms (OpenSky: non-profit research only).

`python scripts/fetch_public_data.py` downloads everything that can be fetched automatically into
data/public/. Datasets behind interactive portals (ASRS exports, FAA SDR yearly CSVs, OpenSky) are
loaded from files you export yourself into the same folder.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA

PUBLIC = DATA / "public"
CMAPSS_MIRROR = "https://raw.githubusercontent.com/edwardzjl/CMAPSSData/master/"
NTSB_MIRROR = "https://raw.githubusercontent.com/sayed-ashfaq/aviation-accidents-eda/main/"


@dataclass(frozen=True)
class PublicDataset:
    key: str
    name: str
    publisher: str
    source_of_record: str
    license: str
    terms: str
    used_for: str
    files: dict[str, str] = field(default_factory=dict)   # filename → sha256 (empty = user-supplied export)
    mirror: str = ""
    auto_fetch: bool = True

    @property
    def folder(self) -> Path:
        return PUBLIC / self.key

    def available(self) -> bool:
        return bool(self.files) and all((self.folder / f).exists() for f in self.files)


CMAPSS_FILES = {
    "readme.txt": "4f5270554b775c67e73aff383c5436fd329d6e4cc3d3a116913276fae511269b",
    "train_FD001.txt": "963b5e22825b34d8b21c69e1aeb4af3e647050eb672ee8834ba4b5d91d2de0f8",
    "test_FD001.txt": "3cda7109ce17bafb5443f2ac926cfcf88154b941b8c4cf95eb55d1ddd6f52851",
    "RUL_FD001.txt": "a19c8ec94931949d0485bdc35118206e9c81c4547b422efb9cf86f4ceddbceca",
    "train_FD002.txt": "dac6c4dbc4e7c1bdeb5747da3d313d05c395bb99801b44a002b26a2ba13d788f",
    "test_FD002.txt": "de7b5bf7e998a985c378488480528b7c02cff1406a46740def362dda8d9b4e02",
    "RUL_FD002.txt": "c851dd96a6ea6998d3c4a8f834d3c8013aa90e93a6ed950dc826ad0655b2906b",
    "train_FD003.txt": "2abbe9968cc5e8eb091980f51b20f62bb4127336d3482cb52071d53bf23329e2",
    "test_FD003.txt": "299babd63c8d987cef079c4a425429f33b3a34797d803bbe2ad48c29dbd0d790",
    "RUL_FD003.txt": "df1e0566306b174a2de41c67a3e7a51877889598b78643fc3e5685259091b7cb",
    "train_FD004.txt": "27ef6160b6a1dcb2613a88de9c239f763b223f02cdc41dc5cdedc5dc189b6218",
    "test_FD004.txt": "1dc675fff0624bac10786927c6715b37d1297657137400d2b1a3138d777a3ba5",
    "RUL_FD004.txt": "196b836b85a95ac7fdbbf29c5fdf1657382eafa445644d114ffaaf50dc2975e1",
}

REGISTRY: dict[str, PublicDataset] = {d.key: d for d in [
    PublicDataset(
        "cmapss", "C-MAPSS turbofan engine degradation (FD001–FD004)", "NASA Prognostics Center of Excellence",
        "https://data.nasa.gov/dataset/c-mapss-aircraft-engine-simulator-data",
        "US Government work (public domain in the US); cite Saxena et al. 2008",
        "Simulated run-to-failure data; not real fleet data.",
        "Remaining-useful-life model, sensor anomaly detection, maintenance risk",
        CMAPSS_FILES, CMAPSS_MIRROR),
    PublicDataset(
        "ntsb", "NTSB aviation accident database extract (1948–2022, 88,890 events)", "National Transportation Safety Board",
        "https://www.ntsb.gov/Pages/AviationQuery.aspx",
        "US Government work (public domain in the US)",
        "Extract of the public NTSB database as redistributed on GitHub; refresh from NTSB CAROL for current data.",
        "Safety analytics: phase-of-flight classifier on probable-cause text, similar-event retrieval, trends",
        {"AviationData.csv": "1a3571bc3491252d33ca2d677eb6c1cff016c3243abcb06dee2e67ac53d54ad7"}, NTSB_MIRROR),
    PublicDataset(
        "faa_sdr", "FAA Service Difficulty Reports (1995–2026, yearly CSV)", "Federal Aviation Administration",
        "https://www.faa.gov/av-info/download_SDR", "US Government work (public domain in the US)",
        "Unverified operator/repair-station submissions. Download yearly CSVs manually into data/public/faa_sdr/.",
        "Component-failure trends; part-number linkage to the knowledge graph", auto_fetch=False),
    PublicDataset(
        "asrs", "NASA Aviation Safety Reporting System (ASRS) narratives", "NASA",
        "https://asrs.arc.nasa.gov/search/database.html", "US Government work; sanitized, de-identified narratives",
        "Self-reported and NOT verified by NASA; not statistically representative. CSV export ≤10,000 records per download.",
        "Safety narrative classification and retrieval (drop exported CSVs into data/public/asrs/)", auto_fetch=False),
    PublicDataset(
        "opensky", "OpenSky Network ADS-B trajectories", "OpenSky Network (Swiss non-profit)",
        "https://opensky-network.org", "OpenSky General Terms of Use & Data License",
        "Non-profit research/education, government, or commercial *internal testing* only; no redistribution; cite OpenSky.",
        "Flight-trajectory analytics (notebook 07 only; never shipped in the repo)", auto_fetch=False),
    PublicDataset(
        "mechvqa", "MechVQA mechanical drawings (3.3K drawings, ~21K QA)", "MechVQA authors (ICML 2026)",
        "https://huggingface.co/datasets/XiaofengAlg/MechVQA", "Apache-2.0", "Download in Colab via `datasets`.",
        "Drawing VLM LoRA (notebook 02)", auto_fetch=False),
    PublicDataset(
        "maintnet", "MaintNet aviation maintenance logbooks (6,169 entries)", "MaintNet (COLING 2020)",
        "https://aclanthology.org/2020.coling-demos.2", "Open-source research library (check repository terms)",
        "Download from the MaintNet repository and place the aviation CSV in data/public/maintnet/.",
        "Logbook NER robustness (notebook 01)", auto_fetch=False),
]}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify(ds: PublicDataset) -> dict[str, bool]:
    return {f: (ds.folder / f).exists() and sha256(ds.folder / f) == h for f, h in ds.files.items()}


def catalog() -> list[dict]:
    return [{"key": d.key, "name": d.name, "publisher": d.publisher, "source": d.source_of_record, "license": d.license,
             "terms": d.terms, "used_for": d.used_for, "auto_fetch": d.auto_fetch, "available": d.available()}
            for d in REGISTRY.values()]
