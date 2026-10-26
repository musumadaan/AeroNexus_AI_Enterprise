"""Render docs/RESEARCH.md from data/research_claims.json (single source of truth, verified by tests)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aeronexus.research import verified_claims  # noqa: E402

out = ["# Research basis\n",
       "Every problem statement in the app is tied to a source. `aeronexus/research.py` checks each claim's numbers and wording",
       "against a short excerpt of the source, and `tests/test_research_claims.py` fails CI if any claim drifts.",
       "Industry (non-peer-reviewed) figures are labelled as such and should be read as directional.\n"]
for c in verified_claims():
    out.append(f"### {c['id']} · {c['module']}\n")
    out.append(f"**Claim:** {c['claim']}  ")
    out.append(f"**Excerpt:** \"{c['evidence']}\"  ")
    out.append(f"**Source:** [{c['source_title']}]({c['url']}) — {c['source_type']}, {c['year']}  ")
    out.append(f"**Used for:** {c['how_used']}  ")
    out.append(f"**Check:** {'passed' if c['verified'] else 'FAILED: ' + '; '.join(c['problems'])}\n")
Path(__file__).resolve().parents[2].joinpath("docs", "RESEARCH.md").write_text("\n".join(out))
print("wrote docs/RESEARCH.md")
