"""
Synthetic patient cohort generator for NeuroRx AI — Task 1.4

Generates fully synthetic (zero PHI) patients into bronze layer per DATA_CONTRACTS.md:
- neurorx.bronze.synthetic_patients_raw
- neurorx.bronze.synthetic_schedules_raw
- neurorx.bronze.synthetic_dose_events_raw

Writes in one of two modes — Delta tables when a SparkSession is available, local
Parquet under `data/generated/` otherwise. See `write_to_bronze_tables`.

Verified by `data/ingestion/verify_cohort.py` (runs the generator twice and
asserts 24 properties, including determinism and the demo story). Run it after
any change here.

Key characteristics:
- 50 patients, deterministic with seed=42 — verified across two separate process
  runs, not assumed. All generated timestamps derive from GENERATION_ANCHOR
  (date-truncated), so runs on the same day are byte-identical.
- Demo patient "Margaret Demo" (UUID: 12345678-1234-1234-1234-123456789012)
  with fixed drugs: metformin, lisinopril, warfarin, atorvastatin
  - Margaret Demo misses metformin evening doses 75.6% (key demo story)
  - Overall adherence 44.4%. Both figures are PINNED via DEMO_BASE_ADHERENCE /
    DEMO_METFORMIN_EVENING_TAKE_RATE, not drawn from the shared RNG stream —
    they are asserted in six other files and must not drift when the cohort
    changes.
- Each patient: 2–6 drugs from the curated ~223-drug list, sourced from
  01_openfda_ingest.py so every drug has real FDA label coverage
- 6 months of dose_events (180 days) with realistic patterns:
  * Overall adherence per non-demo patient from Beta(8,2)
  * Evening doses missed 2× more than morning (time_penalty=0.5)
  * Weekend doses missed 1.5× more than weekdays (adherence ×0.67)
  * One "bad week" per patient with adherence halved
  * 2% skip rate (deliberate patient non-takes)
  * taken status: actioned_ts within ±45min of planned
  * missed status: no actioned_ts
  * skipped status: deliberate, rare (2%)
"""

import ast
import os
import uuid
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import random
import hashlib

# === Configuration ===
SEED = 42
NUM_PATIENTS = 50
DEMO_PATIENT_UUID = "12345678-1234-1234-1234-123456789012"
DEMO_PATIENT_NAME = "Margaret Demo"
DEMO_PATIENT_DRUGS = ["metformin", "lisinopril", "warfarin", "atorvastatin"]

# === Demo-story constants ===
# Margaret Demo's adherence numbers are asserted in at least six places
# (setup/phase1_checkpoint.sql, agent/tools/get_adherence_stats.sql's regression
# cells, docs/demo_script.md, evals/safety_judge.md, CLAUDE.md). They are load-
# bearing demo facts, so they are PINNED here rather than drawn from the shared
# RNG stream.
#
# Why that matters: `base_adherence` used to come from a shared
# `np.random.beta(8, 2)` sequence whose position depends on how much RNG every
# *earlier* patient consumed. Fixing the drug list (which changed how many draws
# `np.random.choice` makes) silently re-rolled Margaret from ~44% to ~27.8%
# adherence — a demo-breaking drift with no error, caused by an unrelated fix.
# Pinning decouples the demo story from cohort-wide RNG consumption so future
# changes to the cohort cannot re-roll it.
# 0.66 chosen by grid search against the actual generator output (not derived
# analytically — the weekend/bad-week/evening penalties compound). At seed=42 it
# yields 44.4% overall and a 75.6% metformin-evening miss rate, matching both
# documented figures. Re-tune with the same search if the RNG stream ever shifts.
DEMO_BASE_ADHERENCE = 0.66
DEMO_METFORMIN_EVENING_TAKE_RATE = 0.244  # → 75.6% miss rate on the evening dose

# Anchor for all generated timestamps. Date-truncated so two runs on the same day
# are byte-identical: `datetime.now()` carries microseconds, which broke
# determinism for `created_at` in patients and schedules (dose_events already
# truncated, which is why only two of the three tables drifted).
GENERATION_ANCHOR = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

# Synthetic names for patients (deterministic with seed)
FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "Michael", "Jennifer", "William", "Linda",
    "David", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah",
    "Charles", "Karen", "Christopher", "Nancy", "Daniel", "Lisa", "Matthew", "Betty",
    "Mark", "Margaret", "Donald", "Sandra", "Steven", "Ashley", "Paul", "Kimberly",
    "Andrew", "Donna", "Joshua", "Carol", "Kenneth", "Michelle", "Kevin", "Amanda",
    "Brian", "Melissa", "George", "Deborah", "Edward", "Stephanie", "Ronald", "Rebecca",
    "Anthony", "Emily", "Frank", "Carolyn"
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Young",
    "Allen", "King", "Wright", "Scott", "Torres", "Peterson", "Phillips", "Campbell",
    "Parker", "Evans", "Edwards", "Collins", "Reyes", "Morris", "Murphy", "Rogers",
    "Morgan", "Cooper", "Reed", "Bailey"
]
# "Peterson" appeared twice in this list (positions 37 and 48). Harmless for
# correctness but it made two surnames collide at different indices; replaced the
# duplicate with "Bailey" so all 51 entries are distinct.
assert len(LAST_NAMES) == len(set(LAST_NAMES)), "LAST_NAMES must be distinct"

# === Drug list ===
# Sourced from `01_openfda_ingest.py`'s curated DRUG_LIST — the single source of
# truth for which drugs this project actually has FDA label coverage for.
#
# This used to be a hardcoded 235-entry list of which ~175 entries were not drugs
# at all ("capriccio", "caprifig", "canola", "canine", "antibody", "antioxidant").
# Those generated schedules referencing drugs with no FDA label, no RxCUI, and no
# DDInter coverage — i.e. most of the cohort was unusable downstream, silently.
#
# Parsed with `ast.literal_eval` rather than imported, deliberately:
# `01_openfda_ingest.py` is a Databricks notebook whose module level makes live
# openFDA HTTP calls and touches `spark`/`dbutils`. Importing it here would fire
# ~200 API requests as a side effect of generating a cohort. Parsing the literal
# gets the single source of truth without executing anything.

_OPENFDA_NOTEBOOK = Path(__file__).resolve().parent / "01_openfda_ingest.py"


def load_curated_drug_list(path=_OPENFDA_NOTEBOOK):
    """Extract the curated DRUG_LIST literal from the openFDA ingest notebook.

    Raises rather than falling back to a hardcoded list: a silent fallback is how
    the non-curated drug list survived undetected in the first place.
    """
    tree = ast.parse(Path(path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "DRUG_LIST" for t in node.targets
        ):
            drugs = ast.literal_eval(node.value)
            # Preserve first-seen order (dict is insertion-ordered) so the list
            # stays deterministic across runs; a set() here would not be.
            return list(dict.fromkeys(drugs))
    raise RuntimeError(f"DRUG_LIST not found in {path}")


DRUG_LIST = load_curated_drug_list()

# The demo story depends on these four having real labels and RxCUIs.
for _required in DEMO_PATIENT_DRUGS + ["ibuprofen"]:
    assert _required in DRUG_LIST, f"demo drug '{_required}' missing from curated DRUG_LIST"

# Realistic dose times by time of day (HH:MM:SS format)
MORNING_TIMES = ["06:00:00", "07:00:00", "08:00:00", "09:00:00"]
AFTERNOON_TIMES = ["12:00:00", "13:00:00", "14:00:00", "15:00:00"]
EVENING_TIMES = ["18:00:00", "19:00:00", "20:00:00", "21:00:00"]
NIGHT_TIMES = ["22:00:00", "23:00:00"]

def set_seeds():
    """Set all seeds for reproducibility."""
    np.random.seed(SEED)
    random.seed(SEED)

def generate_deterministic_uuid(seed_str):
    """Generate a deterministic UUID from a seed string."""
    hash_obj = hashlib.md5(seed_str.encode())
    return str(uuid.UUID(bytes=hash_obj.digest()))

def generate_name(idx):
    """Generate deterministic synthetic name.

    Surname is indexed by `idx % len(LAST_NAMES)`, NOT the previous
    `idx // len(FIRST_NAMES)`. That integer division evaluated to 0 for every
    index below 52, and the cohort is only 50 patients — so all 49 non-demo
    patients were named "<First> Smith" (LAST_NAMES[0]). Confirmed by running it.

    len(FIRST_NAMES)=52 and len(LAST_NAMES)=51 are coprime, so the (first, last)
    pair does not repeat until 52*51=2652 indices — far beyond any cohort size
    this generator is used at, and beyond the +1000 offset used for caregivers.
    """
    return f"{FIRST_NAMES[idx % len(FIRST_NAMES)]} {LAST_NAMES[idx % len(LAST_NAMES)]}"

def create_patients(num_patients):
    """Create synthetic patient records."""
    patients = []

    # Add demo patient first
    patients.append({
        "patient_id": DEMO_PATIENT_UUID,
        "display_name": DEMO_PATIENT_NAME,
        "caregiver_name": generate_name(0) if np.random.rand() > 0.3 else None,
        "created_at": GENERATION_ANCHOR - timedelta(days=365),
    })

    # Add remaining 49 patients
    for i in range(1, num_patients):
        patients.append({
            "patient_id": generate_deterministic_uuid(f"patient_{i}_{SEED}"),
            "display_name": generate_name(i),
            "caregiver_name": generate_name(i + 1000) if np.random.rand() > 0.3 else None,
            "created_at": GENERATION_ANCHOR - timedelta(
                days=int(np.random.randint(30, 365*2))
            ),
        })

    return patients

def assign_drugs_to_patients(patients, drug_list):
    """Assign 2-6 drugs to each patient."""
    patient_drugs = {}

    for patient in patients:
        patient_id = patient["patient_id"]

        if patient_id == DEMO_PATIENT_UUID:
            # Demo patient gets exactly these drugs
            drugs = DEMO_PATIENT_DRUGS.copy()
        else:
            # Others get 2-6 random drugs
            num_drugs = int(np.random.randint(2, 7))
            drugs = list(np.random.choice(drug_list, size=num_drugs, replace=False))

        patient_drugs[patient_id] = drugs

    return patient_drugs

def get_dose_times(times_per_day, is_demo=False, drug_name=None):
    """Generate realistic dose times for a schedule."""
    if times_per_day == 1:
        return [np.random.choice(MORNING_TIMES + AFTERNOON_TIMES)]
    elif times_per_day == 2:
        # Morning and evening
        return [
            np.random.choice(MORNING_TIMES),
            np.random.choice(EVENING_TIMES),
        ]
    elif times_per_day == 3:
        # Morning, afternoon, evening
        return [
            np.random.choice(MORNING_TIMES),
            np.random.choice(AFTERNOON_TIMES),
            np.random.choice(EVENING_TIMES),
        ]
    else:
        # Fallback for unexpected values
        return [np.random.choice(MORNING_TIMES)]

def create_schedules(patient_drugs, patients):
    """Create drug schedules for each patient."""
    schedules = []
    schedule_counter = 0

    for patient in patients:
        patient_id = patient["patient_id"]
        drugs = patient_drugs[patient_id]
        is_demo = patient_id == DEMO_PATIENT_UUID

        for drug_name in drugs:
            # Demo patient gets specific, realistic schedules for better demo stories
            if is_demo:
                if drug_name == "metformin":
                    times_per_day = 2
                    dose_times = ["07:00:00", "19:00:00"]  # Morning and evening for demo
                elif drug_name == "lisinopril":
                    times_per_day = 1
                    dose_times = ["08:00:00"]  # Morning
                elif drug_name == "warfarin":
                    times_per_day = 1
                    dose_times = ["18:00:00"]  # Evening
                elif drug_name == "atorvastatin":
                    times_per_day = 1
                    dose_times = ["21:00:00"]  # Bedtime
                else:
                    times_per_day = int(np.random.randint(1, 4))
                    dose_times = get_dose_times(times_per_day, is_demo=False, drug_name=None)
            else:
                times_per_day = int(np.random.randint(1, 4))
                dose_times = get_dose_times(times_per_day, is_demo=False, drug_name=None)

            timing_notes_options = [None, "with food", "at bedtime", "with water"]
            timing_weights = [0.6, 0.2, 0.1, 0.1]
            timing_notes = np.random.choice(timing_notes_options, p=timing_weights)

            schedules.append({
                "schedule_id": generate_deterministic_uuid(f"schedule_{schedule_counter}_{SEED}"),
                "patient_id": patient_id,
                "rxcui": drug_name,  # Using name, not RXCUI (per requirements: "left as names for Phase 3 resolution")
                "drug_name": drug_name,
                "dose_text": f"{int(np.random.choice([250, 500, 750, 1000]))} mg",
                "times_per_day": times_per_day,
                "dose_times": dose_times,  # Will be array in Delta
                "timing_notes": timing_notes,
                "status": "active",
                "created_at": GENERATION_ANCHOR - timedelta(days=180),
            })
            schedule_counter += 1

    return schedules

def classify_dose_time(ts):
    """Classify a timestamp into day_part."""
    hour = ts.hour
    if 5 <= hour < 12:
        return "morning"
    elif 12 <= hour < 17:
        return "afternoon"
    elif 17 <= hour < 21:
        return "evening"
    else:
        return "night"

def generate_dose_events(schedules, patients):
    """Generate 6 months of dose events with realistic adherence patterns."""
    events = []
    end_date = GENERATION_ANCHOR
    start_date = end_date - timedelta(days=180)  # ~6 months
    event_counter = 0

    # Compute adherence per patient from Beta(8,2)
    adherence_per_patient = {}
    for patient in patients:
        patient_id = patient["patient_id"]
        if patient_id == DEMO_PATIENT_UUID:
            # Pinned, not drawn — see DEMO_BASE_ADHERENCE. The beta draw is still
            # consumed below so the demo patient does not shift the RNG stream
            # for everyone else relative to the un-pinned version.
            np.random.beta(8, 2)
            adherence_per_patient[patient_id] = DEMO_BASE_ADHERENCE
        else:
            adherence_per_patient[patient_id] = np.random.beta(8, 2)

    for schedule in schedules:
        patient_id = schedule["patient_id"]
        schedule_id = schedule["schedule_id"]
        rxcui = schedule["rxcui"]
        dose_times = schedule["dose_times"]
        times_per_day = schedule["times_per_day"]

        base_adherence = adherence_per_patient[patient_id]

        # Generate events for each day
        current_date = start_date
        bad_week_start = start_date + timedelta(days=int(np.random.randint(30, 150)))
        bad_week_end = bad_week_start + timedelta(days=7)

        while current_date < end_date:
            is_weekend = current_date.weekday() >= 5
            is_bad_week = bad_week_start <= current_date < bad_week_end

            # Apply adherence penalties
            adherence = base_adherence
            if is_weekend:
                adherence *= 0.67  # 1.5x miss rate
            if is_bad_week:
                adherence *= 0.5  # Halved adherence

            for dose_time_str in dose_times:
                # Parse dose time
                dose_hour, dose_minute, dose_second = map(int, dose_time_str.split(":"))
                planned_ts = current_date.replace(hour=dose_hour, minute=dose_minute, second=dose_second)

                # Determine if missed based on adherence and time-of-day penalty
                dose_part = classify_dose_time(planned_ts)

                # Evening doses missed 2x more often
                time_penalty = 0.5 if dose_part == "evening" else 1.0
                is_taken = np.random.rand() < (adherence * time_penalty)

                # Special case: Margaret Demo misses metformin evening doses most
                if (patient_id == DEMO_PATIENT_UUID and
                    rxcui == "metformin" and
                    dose_part == "evening"):
                    is_taken = np.random.rand() < DEMO_METFORMIN_EVENING_TAKE_RATE

                # Generate event
                status = "skipped" if np.random.rand() < 0.02 else ("taken" if is_taken else "missed")

                event_id = generate_deterministic_uuid(f"event_{event_counter}_{SEED}")
                actioned_ts = None

                # actioned_ts MUST be >= planned_ts. DATA_CONTRACTS.md §6.3 (frozen)
                # and lakebase/schema.sql both enforce
                # `dose_events_actioned_after_planned` (actioned_ts >= planned_ts).
                # The original ±45min / -30..120min jitter allowed actions BEFORE
                # the planned time, which loads fine into Delta (warn-only there)
                # but is REJECTED by the Lakebase CHECK — the load hit a
                # CheckViolation on Margaret's very first taken dose. Since the
                # contract is frozen, the generator yields: jitter is now
                # non-negative (on-time or late), which leaves the status-based
                # adherence figures untouched. Found only by loading into real
                # Postgres, not by reading either file.
                if status == "taken":
                    jitter_minutes = np.random.uniform(0, 45)  # on time or up to 45min late
                    actioned_ts = planned_ts + timedelta(minutes=jitter_minutes)
                elif status == "skipped":
                    jitter_minutes = np.random.uniform(0, 120)  # deliberate skip, logged later
                    actioned_ts = planned_ts + timedelta(minutes=jitter_minutes)

                events.append({
                    "event_id": event_id,
                    "schedule_id": schedule_id,
                    "patient_id": patient_id,
                    "rxcui": rxcui,
                    "planned_ts": planned_ts,
                    "actioned_ts": actioned_ts,
                    "status": status,
                })
                event_counter += 1

            current_date += timedelta(days=1)

    return events

def compute_adherence_summary(dose_events, schedules, patients):
    """Compute and print per-patient adherence %."""
    print("\n" + "="*80)
    print("SYNTHETIC COHORT ADHERENCE SUMMARY (6-month period)")
    print("="*80)

    for patient in patients:
        patient_id = patient["patient_id"]
        patient_name = patient["display_name"]

        patient_events = [e for e in dose_events if e["patient_id"] == patient_id]

        if len(patient_events) == 0:
            adherence_pct = 0.0
        else:
            taken = sum(1 for e in patient_events if e["status"] == "taken")
            planned = len(patient_events)
            adherence_pct = (taken / planned * 100) if planned > 0 else 0.0

        # Get patient's drugs
        patient_schedule_drugs = set()
        for schedule in schedules:
            if schedule["patient_id"] == patient_id:
                patient_schedule_drugs.add(schedule["drug_name"])
        drugs = ", ".join(sorted(patient_schedule_drugs))

        marker = " ⭐ DEMO" if patient_id == DEMO_PATIENT_UUID else ""
        print(f"{patient_name:25s} | Adherence: {adherence_pct:6.1f}% | Drugs: {drugs}{marker}")

    print("="*80 + "\n")

def _get_spark():
    """Return the ambient SparkSession, or None if running off-workspace.

    In a Databricks notebook `spark` is injected into the module globals by the
    runtime. Locally there is none, and that is a supported mode (see
    write_to_bronze_tables' fallback) rather than an error.
    """
    if "spark" in globals():
        return globals()["spark"]
    try:
        from pyspark.sql import SparkSession
    except ImportError:
        return None
    return SparkSession.getActiveSession()


def _git_sha():
    """Best-effort short git SHA for the audit column; 'unknown' off a checkout."""
    import subprocess
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).resolve().parent,
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# Where the local fallback writes. Overridable so a test can point it elsewhere.
LOCAL_OUTPUT_DIR = Path(
    os.getenv("NEURORX_COHORT_OUTPUT_DIR", Path(__file__).resolve().parents[2] / "data" / "generated")
)


def write_to_bronze_tables(patients, schedules, dose_events, spark=None, output_dir=None):
    """Write the cohort to the three bronze tables.

    Two modes, because this generator has to be useful in both places it runs:

    - **On Databricks** (a SparkSession is available): writes Delta tables
      `neurorx.bronze.synthetic_{patients,schedules,dose_events}_raw`, overwriting
      so a re-run with the same seed is idempotent rather than duplicating.
    - **Locally** (no Spark): writes the same three datasets as Parquet under
      `data/generated/`. `lakebase/07_load_cohort.py` can then be pointed at
      those files for the local demo path without a workspace.

    This function previously did NEITHER — it attached audit columns to in-memory
    dicts and returned them, so `neurorx.bronze.*` was never written at all while
    the notebook still printed a success summary. Every downstream table in the
    project traces back to these three, so the whole medallion pipeline was
    reading from empty sources. Do not "simplify" this back to a pure return.
    """
    # Add audit columns
    now = datetime.now()
    git_sha = _git_sha()
    source_file = f"generator:04_synthetic_cohort@{git_sha}"

    # synthetic_patients_raw
    patients_bronze = []
    for patient in patients:
        p = patient.copy()
        p["_ingested_at"] = now
        p["_source_file"] = source_file
        patients_bronze.append(p)

    # synthetic_schedules_raw
    schedules_bronze = []
    for schedule in schedules:
        s = schedule.copy()
        s["_ingested_at"] = now
        s["_source_file"] = source_file
        schedules_bronze.append(s)

    # synthetic_dose_events_raw
    dose_events_bronze = []
    for event in dose_events:
        e = event.copy()
        e["_ingested_at"] = now
        e["_source_file"] = source_file
        dose_events_bronze.append(e)

    print("\n📊 Generated Data Summary:")
    print(f"  Patients: {len(patients_bronze)}")
    print(f"  Schedules: {len(schedules_bronze)}")
    print(f"  Dose Events: {len(dose_events_bronze)}")

    datasets = {
        "synthetic_patients_raw": patients_bronze,
        "synthetic_schedules_raw": schedules_bronze,
        "synthetic_dose_events_raw": dose_events_bronze,
    }

    spark = spark or _get_spark()

    if spark is not None:
        catalog_schema = os.getenv("SCHEMA_BRONZE", "neurorx.bronze")
        print(f"\n💾 Writing Delta tables to {catalog_schema}.* ...")
        for table_name, rows in datasets.items():
            fqn = f"{catalog_schema}.{table_name}"
            # createDataFrame infers schema from the dicts; dose_times stays an
            # array<string> and the nullable actioned_ts stays nullable, matching
            # DATA_CONTRACTS.md §3. Overwrite (not append) so re-running with
            # seed=42 is idempotent instead of doubling the row count.
            spark.createDataFrame(rows).write.mode("overwrite").option(
                "overwriteSchema", "true"
            ).saveAsTable(fqn)
            print(f"  ✅ {fqn}: {len(rows):,} rows")
        written_to = catalog_schema
    else:
        out = Path(output_dir or LOCAL_OUTPUT_DIR)
        out.mkdir(parents=True, exist_ok=True)
        print(f"\n💾 No SparkSession — writing local Parquet to {out} ...")
        try:
            import pandas as pd
        except ImportError:
            raise RuntimeError(
                "Local fallback needs pandas + pyarrow: pip install -r requirements-dev.txt"
            )
        for table_name, rows in datasets.items():
            path = out / f"{table_name}.parquet"
            pd.DataFrame(rows).to_parquet(path, index=False)
            print(f"  ✅ {path.name}: {len(rows):,} rows")
        written_to = str(out)

    return {
        "patients": patients_bronze,
        "schedules": schedules_bronze,
        "dose_events": dose_events_bronze,
        "written_to": written_to,
    }

def main():
    """Main execution."""
    print("🏥 NeuroRx AI Synthetic Cohort Generator")
    print(f"Seed: {SEED} (deterministic)")
    print(f"Demo Patient UUID: {DEMO_PATIENT_UUID}")
    print()

    set_seeds()

    # Create entities
    print("Creating patients...")
    patients = create_patients(NUM_PATIENTS)

    print("Assigning drugs...")
    patient_drugs = assign_drugs_to_patients(patients, DRUG_LIST)

    print("Creating schedules...")
    schedules = create_schedules(patient_drugs, patients)

    print("Generating dose events (6 months)...")
    dose_events = generate_dose_events(schedules, patients)

    # Print adherence summary
    compute_adherence_summary(dose_events, schedules, patients)

    # Write to bronze layer
    bronze_tables = write_to_bronze_tables(patients, schedules, dose_events)

    # Return for inspection
    return bronze_tables

if __name__ == "__main__":
    bronze_tables = main()
    print("✅ Synthetic cohort generation complete")
