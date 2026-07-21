"""Verification for the synthetic cohort generator (Task 1.4).

Runs `04_synthetic_cohort.py` twice into separate directories and asserts the
properties that actually matter. Exists because CLAUDE.md §6's rule — "a ✅ in
CLAUDE.md is a claim, not a fact" — bit this exact file twice: it was marked
complete while writing zero rows.

Usage:
    python data/ingestion/verify_cohort.py

Requires: pandas, pyarrow, numpy (pip install -r requirements-dev.txt).
No Spark, no Databricks workspace, no network.
"""

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
GENERATOR = Path(__file__).resolve().parent / "04_synthetic_cohort.py"
OPENFDA = Path(__file__).resolve().parent / "01_openfda_ingest.py"
TABLES = [
    "synthetic_patients_raw",
    "synthetic_schedules_raw",
    "synthetic_dose_events_raw",
]
DEMO_UUID = "12345678-1234-1234-1234-123456789012"

_failures = []


def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(name)


def run_generator(out_dir):
    subprocess.run(
        [sys.executable, str(GENERATOR)],
        env={"NEURORX_COHORT_OUTPUT_DIR": str(out_dir), "PATH": "/usr/bin:/bin"},
        cwd=REPO, check=True, capture_output=True,
    )
    return {t: pd.read_parquet(Path(out_dir) / f"{t}.parquet") for t in TABLES}


def curated_drugs():
    tree = ast.parse(OPENFDA.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "DRUG_LIST" for t in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise RuntimeError("DRUG_LIST not found in 01_openfda_ingest.py")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "run1", Path(tmp) / "run2"
        run1 = run_generator(a)
        run2 = run_generator(b)

        # Determinism. `_ingested_at` is a genuine wall-clock audit column and is
        # excluded; everything else must be byte-identical across processes.
        print("determinism (seed=42, two separate process runs)")
        for t in TABLES:
            x = run1[t].drop(columns=["_ingested_at"])
            y = run2[t].drop(columns=["_ingested_at"])
            check(t, x.equals(y), f"{len(x):,} rows byte-identical")

        print("\ndrug list is the curated one")
        used = set(run1["synthetic_schedules_raw"]["drug_name"])
        cur = curated_drugs()
        check("all scheduled drugs curated", used <= cur,
              f"{len(used)} distinct, {len(used - cur)} uncurated")
        junk = {"capriccio", "canola", "antibody", "caprifig", "canine", "antioxidant"}
        check("no junk entries", not (junk & used))

        print("\nnames")
        nm = run1["synthetic_patients_raw"]["display_name"]
        check("all display_names distinct", nm.nunique() == len(nm), f"{nm.nunique()} unique")
        surnames = {n.split()[-1] for n in nm}
        check("surnames varied (not all 'Smith')", len(surnames) > 1,
              f"{len(surnames)} distinct surnames")

        print("\nrows actually written")
        for t in TABLES:
            check(t, len(run1[t]) > 0, f"{len(run1[t]):,} rows")

        print("\nno wall-clock leakage into generated data")
        created = pd.to_datetime(run1["synthetic_patients_raw"].created_at)
        check("created_at date-truncated", (created.dt.time.astype(str) == "00:00:00").all())

        print("\nMargaret Demo — load-bearing demo story")
        sch = run1["synthetic_schedules_raw"]
        ms = sch[sch.patient_id == DEMO_UUID]
        check("4 fixed drugs", set(ms.drug_name) ==
              {"metformin", "lisinopril", "warfarin", "atorvastatin"},
              str(sorted(set(ms.drug_name))))
        met = ms[ms.drug_name == "metformin"].iloc[0]
        check("metformin 2x/day", int(met.times_per_day) == 2, str(list(met.dose_times)))

        ev = run1["synthetic_dose_events_raw"]
        demo = ev[ev.patient_id == DEMO_UUID]
        overall = (demo.status == "taken").mean()
        # Asserted in setup/phase1_checkpoint.sql and get_adherence_stats.sql's
        # regression cells. If this drifts, those break too.
        check("overall adherence ~44%", 0.42 <= overall <= 0.46, f"{overall:.2%}")

        met_ids = set(ms[ms.drug_name == "metformin"].schedule_id)
        mv = ev[ev.schedule_id.isin(met_ids)].copy()
        mv["hour"] = pd.to_datetime(mv.planned_ts).dt.hour
        evening_miss = (mv[mv.hour >= 17].status != "taken").mean()
        morning_miss = (mv[(mv.hour >= 5) & (mv.hour < 12)].status != "taken").mean()
        check("metformin evening miss ~75.6%", 0.73 <= evening_miss <= 0.78,
              f"{evening_miss:.1%} evening vs {morning_miss:.1%} morning")
        check("evening worse than morning", evening_miss > morning_miss)

        print("\nschema invariants (DATA_CONTRACTS.md §3)")
        check("statuses valid", set(ev.status) <= {"taken", "missed", "skipped"},
              str(sorted(set(ev.status))))
        check("missed doses carry no actioned_ts",
              ev[ev.status == "missed"].actioned_ts.isna().all())
        check("taken doses carry an actioned_ts",
              ev[ev.status == "taken"].actioned_ts.notna().all())
        check("schedule_id FK resolves",
              set(ev.schedule_id) <= set(sch.schedule_id))
        check("patient_id FK resolves",
              set(sch.patient_id) <= set(run1["synthetic_patients_raw"].patient_id))
        check("times_per_day matches dose_times length",
              all(len(r.dose_times) == int(r.times_per_day) for r in sch.itertuples()))

    print("\n" + "=" * 50)
    if _failures:
        print(f"{len(_failures)} FAILED: {_failures}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
