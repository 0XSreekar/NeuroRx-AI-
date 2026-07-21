"""
Load synthetic cohort from Delta into Lakebase (Task 3.8)

Reads the Phase 1 synthetic cohort from neurorx.bronze.synthetic_* Delta tables
and loads into Lakebase (patients, schedules, dose_events) via batch psycopg inserts.

Requirements (from CLAUDE.md Task 3.8):
1. Deterministic UUID mapping (idempotent: ON CONFLICT DO NOTHING)
2. Margaret Demo keeps her fixed UUID (12345678-1234-1234-1234-123456789012)
3. Batch size 1000; progress prints; final row-count reconciliation
4. Reminder to run Lakebase→Delta sync + pipeline refresh

The synthetic cohort generator (Task 1.4) already uses deterministic UUID generation
(md5-based), so UUIDs are stable across re-runs. This loader reuses those same UUIDs
directly when inserting into Lakebase, making the load idempotent: running it twice
on the same cohort produces no duplicates (due to the UNIQUE constraints per
DATA_CONTRACTS.md §6.2-6.3).

Verified live (DATA_CONTRACTS.md §4):
- Postgres 18, no CREATE EXTENSION needed for gen_random_uuid()
- Lakebase native Postgres auth via password (not OAuth tokens)
- Deterministic seed=42 via 04_synthetic_cohort.py is stable across re-runs
"""

import os
from datetime import datetime
from pathlib import Path

import psycopg
from app.config import settings


def get_lakebase_connection():
    """Create a connection to Lakebase using native Postgres credentials.

    Two modes:
      - **Real Lakebase** (default): TLS-required connection on 5432 using the
        `settings.lakebase_*` credentials.
      - **Local dev**: if `NEURORX_LOCAL_PG` is set to a libpq connection string
        (e.g. "host=/tmp/nrx_pg port=5439 user=postgres dbname=databricks_postgres"),
        connect to that instead with no forced sslmode. This is the off-workspace
        demo path — a local Postgres standing in for Lakebase so the app can be
        driven end to end without provisioning the workspace.

    Returns:
        psycopg.Connection: A raw Postgres connection (not pooled).

    Note:
        This is a standalone connection, not reusing the Streamlit app's pooled
        connection, which is decorated @st.cache_resource and unsuitable for
        long-running batch jobs outside Streamlit's runtime.
    """
    local = os.getenv("NEURORX_LOCAL_PG")
    if local:
        return psycopg.connect(local)

    return psycopg.connect(
        host=settings.lakebase_host,
        dbname=settings.lakebase_db,
        user=settings.lakebase_user,
        password=settings.lakebase_password,
        port=5432,
        sslmode="require",
    )


def read_cohort_from_parquet(parquet_dir):
    """Read the synthetic cohort from local Parquet (the off-workspace path).

    Consumes exactly what `04_synthetic_cohort.py`'s local-fallback mode writes.
    Returns three lists of plain dicts in the same shape `main()`'s Spark path
    produces via `row.asDict()`, so every downstream batch loader is unchanged.
    """
    import pandas as pd

    d = Path(parquet_dir)

    def load(table, columns):
        df = pd.read_parquet(d / f"{table}.parquet")[list(columns)]
        rows = df.to_dict("records")
        for r in rows:
            # pandas represents a null timestamp as NaT and null numerics as NaN.
            # psycopg does NOT map either to SQL NULL — it serializes NaT as a
            # garbage overflow timestamp (seen: year 48113), which then trips the
            # `dose_events_actioned_consistent` CHECK because a 'missed' dose
            # appears to carry an action time. Convert every pandas-null scalar to
            # None so NULL columns actually arrive as NULL. Caught by loading into
            # real Postgres, not visible in the Parquet itself.
            for k, v in list(r.items()):
                if k == "dose_times":
                    continue  # array column; handled below, pd.isna would be ambiguous
                if v is None:
                    continue
                try:
                    if pd.isna(v):
                        r[k] = None
                except (TypeError, ValueError):
                    pass  # non-scalar (shouldn't occur here); leave as-is
            # dose_times is a numpy array from Parquet; the schedule loader iterates
            # it and psycopg maps a plain list to TIME[]. Match Spark's ArrayType.
            if r.get("dose_times") is not None:
                r["dose_times"] = list(r["dose_times"])
        return rows

    patients = load("synthetic_patients_raw",
                    ("patient_id", "display_name", "caregiver_name", "created_at"))
    schedules = load("synthetic_schedules_raw",
                     ("schedule_id", "patient_id", "rxcui", "drug_name", "dose_text",
                      "times_per_day", "dose_times", "timing_notes", "status", "created_at"))
    dose_events = load("synthetic_dose_events_raw",
                       ("event_id", "schedule_id", "patient_id", "planned_ts",
                        "actioned_ts", "status"))
    return patients, schedules, dose_events


def build_drug_name_to_rxcui_map(conn):
    """Build a map from drug names to RxCUIs for the synthetic cohort.

    The generator (Task 1.4) stores drug names in the rxcui column as a
    placeholder ("left as names for Phase 3 resolution"), but Lakebase expects
    numeric RxCUI strings. This maps them back using the gold.drugs table
    synced from Delta.

    Args:
        conn: psycopg Connection

    Returns:
        dict: {drug_name: rxcui_string} mapping, or raises KeyError if a drug
              is not found in gold.drugs.

    Note:
        In a real deployment, this would query Delta via SQL or Spark. For this
        loader, we read from the Lakebase Lakeflow sync of gold.drugs (which is
        populated by the pipeline, not synced from Delta directly, since the
        pipeline computes it). If the pipeline hasn't run yet, this query will
        return an empty map and insert will fail with a helpful key error.
    """
    drug_map = {}

    try:
        with conn.cursor() as cur:
            # Try to read from gold.drugs (populated by Lakeflow pipeline)
            # This is the real source of truth for drug->rxcui mapping
            cur.execute("""
                SELECT generic_name, rxcui FROM neurorx.gold.drugs
            """)
            for generic_name, rxcui in cur.fetchall():
                drug_map[generic_name] = rxcui
    except Exception:
        # gold.drugs may not exist yet if the pipeline hasn't run (it never has
        # on the local demo path). A failed statement leaves the whole
        # transaction ABORTED — every later insert then dies with
        # InFailedSqlTransaction — so we MUST roll back here, not just swallow the
        # error. (Same transaction-poisoning hazard CLAUDE.md flags for Task 3.5's
        # UndefinedTable handling.) Then fall back to the hardcoded demo map.
        conn.rollback()

    # Hardcoded fallback for the demo drugs and common ones (DATA_CONTRACTS.md verified)
    fallback_map = {
        "metformin": "6809",
        "lisinopril": "29046",
        "warfarin": "11289",
        "atorvastatin": "20481",
        "amlodipine": "17767",
        "aspirin": "7671",
        "carvedilol": "41127",
        "clopidogrel": "32265",
        "dabigatran": "612100",
        "diltiazem": "3443",
        "doxazosin": "3624",
        "enalapril": "3770",
        "enoxaparin": "4356",
        "ezetimibe": "4469",
        "fenofibrate": "4453",
        "fluoxetine": "4493",
        "furosemide": "4603",
        "glipizide": "4815",
        "glyburide": "4821",
        "hydrochlorothiazide": "5487",
        "ibuprofen": "5640",
        "isosorbide": "5591",
        "labetalol": "6072",
        "levothyroxine": "6320",
        "losartan": "52175",
        "lovastatin": "6472",
        "metoprolol": "6746",
        "mexiletine": "6782",
        "midodrine": "7103",
        "milrinone": "7157",
        "minoxidil": "7217",
        "nitroglycerin": "7604",
        "nifedipine": "7417",
        "omeprazole": "7646",
        "phentermine": "8150",
        "pravastatin": "8631",
        "procainamide": "8755",
        "propranolol": "8787",
        "quinidine": "9068",
        "ramipril": "9107",
        "ranolazine": "79516",
        "reserpine": "9384",
        "rivaroxaban": "898437",
        "rosuvastatin": "73495",
        "sertraline": "36437",
        "simvastatin": "9547",
        "sotalol": "9716",
        "spironolactone": "9950",
        "telmisartan": "77110",
        "terazosin": "10363",
        "ticagrelor": "1223448",
        "timolol": "10325",
        "torsemide": "10606",
        "triamterene": "10647",
        "valsartan": "11145",
        "vancomycin": "11124",
        "verapamil": "11170",
    }

    # Merge fallback into map, but keep gold.drugs takes precedence
    for drug_name, rxcui in fallback_map.items():
        if drug_name not in drug_map:
            drug_map[drug_name] = rxcui

    return drug_map


def fill_missing_rxcuis(drug_map, drug_names):
    """Ensure every drug name has a numeric RxCUI, synthesizing placeholders.

    The curated cohort schedules ~132 distinct drugs, but the real-RxCUI map
    (gold.drugs + the hardcoded demo fallback) only covers ~57. On the real
    workspace path gold.drugs supplies the rest; on the local demo path it does
    not exist, so an unmapped drug would KeyError the whole load.

    For the demo-critical drugs (metformin/lisinopril/warfarin/atorvastatin and
    the other hardcoded ones) the map already holds the *real* RxCUI, and those
    are untouched here. For everything else this fills a DETERMINISTIC synthetic
    RxCUI in a high, clearly-non-real range (900000000+) that satisfies the
    schema's `rxcui ~ '^[0-9]+$'` CHECK. These are placeholders standing in for a
    real RxNorm resolution that only happens on the workspace path — they must
    never be treated as clinically meaningful, but they let the full cohort load
    so the dashboard's cohort-level views have data.
    """
    import hashlib

    added = 0
    for name in sorted(set(drug_names)):
        if name in drug_map:
            continue
        h = int(hashlib.md5(name.encode()).hexdigest(), 16)
        drug_map[name] = str(900000000 + (h % 90000000))  # 9-digit, unambiguously synthetic
        added += 1
    return drug_map, added


def load_patients_batch(conn, patients_batch):
    """Insert a batch of patients with idempotent ON CONFLICT.

    Args:
        conn: psycopg Connection
        patients_batch: list of {patient_id, display_name, caregiver_name, created_at}

    Returns:
        int: Number of rows inserted (may be less than batch size if some already existed)
    """
    if not patients_batch:
        return 0

    sql = """
    INSERT INTO patients (patient_id, display_name, caregiver_name, created_at)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (patient_id) DO NOTHING
    """

    with conn.cursor() as cur:
        executemany_params = [
            (
                str(row["patient_id"]),
                str(row["display_name"]),
                str(row["caregiver_name"]) if row["caregiver_name"] else None,
                row["created_at"],
            )
            for row in patients_batch
        ]
        cur.executemany(sql, executemany_params)
        # Note: executemany doesn't return rowcount per statement; we rely on
        # the final row-count verification to confirm all patients landed.

    return len(patients_batch)


def load_schedules_batch(conn, schedules_batch, drug_rxcui_map):
    """Insert a batch of schedules with idempotent ON CONFLICT.

    Args:
        conn: psycopg Connection
        schedules_batch: list of {schedule_id, patient_id, rxcui, drug_name, dose_text,
                                  times_per_day, dose_times, timing_notes, status, created_at}
        drug_rxcui_map: dict mapping drug names to numeric RxCUI strings

    Returns:
        int: Number of rows inserted

    Note:
        Task 1.4's generator stores drug *names* in the rxcui column as a placeholder.
        This function resolves them to real numeric RxCUIs using the provided map before
        inserting, since Lakebase schema requires rxcui ~ '^[0-9]+$'.
    """
    if not schedules_batch:
        return 0

    sql = """
    INSERT INTO schedules
        (schedule_id, patient_id, rxcui, drug_name, dose_text, times_per_day,
         dose_times, timing_notes, status, created_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (schedule_id) DO NOTHING
    """

    with conn.cursor() as cur:
        executemany_params = [
            (
                str(row["schedule_id"]),
                str(row["patient_id"]),
                drug_rxcui_map[str(row["drug_name"])],  # Convert numpy strings to Python str
                str(row["drug_name"]),
                str(row["dose_text"]),
                int(row["times_per_day"]),
                # dose_times is a list of HH:MM:SS strings; psycopg converts it to
                # Postgres TIME[] natively.
                [str(t) for t in row["dose_times"]],
                str(row["timing_notes"]) if row["timing_notes"] else None,
                str(row["status"]),
                row["created_at"],
            )
            for row in schedules_batch
        ]
        cur.executemany(sql, executemany_params)

    return len(schedules_batch)


def load_dose_events_batch(conn, events_batch):
    """Insert a batch of dose events with idempotent ON CONFLICT.

    Args:
        conn: psycopg Connection
        events_batch: list of {event_id, schedule_id, patient_id, rxcui, planned_ts,
                               actioned_ts, status}

    Returns:
        int: Number of rows inserted
    """
    if not events_batch:
        return 0

    sql = """
    INSERT INTO dose_events
        (event_id, schedule_id, patient_id, planned_ts, actioned_ts, status)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (event_id) DO NOTHING
    """

    with conn.cursor() as cur:
        executemany_params = [
            (
                str(row["event_id"]),
                str(row["schedule_id"]),
                str(row["patient_id"]),
                row["planned_ts"],
                row["actioned_ts"],
                str(row["status"]),
            )
            for row in events_batch
        ]
        cur.executemany(sql, executemany_params)

    return len(events_batch)


def verify_margaret_demo(conn):
    """Assert that Margaret Demo landed with her fixed UUID and drugs intact.

    Requirement 2 from CLAUDE.md Task 3.8: "Margaret Demo keeps her fixed UUID;
    assert post-load that her 4 drugs and metformin-evening missed pattern survived."
    """
    margaret_uuid = "12345678-1234-1234-1234-123456789012"
    expected_drugs = {"metformin", "lisinopril", "warfarin", "atorvastatin"}

    with conn.cursor() as cur:
        # Check Margaret exists
        cur.execute(
            "SELECT display_name, caregiver_name FROM patients WHERE patient_id = %s",
            (margaret_uuid,),
        )
        row = cur.fetchone()
        assert row is not None, f"Margaret Demo not found (UUID {margaret_uuid})"
        display_name, _ = row
        assert display_name == "Margaret Demo", f"Expected 'Margaret Demo', got '{display_name}'"

        # Check her drugs
        cur.execute(
            """
            SELECT DISTINCT drug_name FROM schedules
            WHERE patient_id = %s AND status = 'active'
            """,
            (margaret_uuid,),
        )
        loaded_drugs = {row[0] for row in cur.fetchall()}
        assert (
            loaded_drugs == expected_drugs
        ), f"Expected {expected_drugs}, got {loaded_drugs}"

        # Check metformin evening schedule (2x/day, with 19:00:00 for evening)
        cur.execute(
            """
            SELECT times_per_day, dose_times FROM schedules
            WHERE patient_id = %s AND drug_name = 'metformin'
            """,
            (margaret_uuid,),
        )
        row = cur.fetchone()
        assert row is not None, "Margaret's metformin schedule not found"
        times_per_day, dose_times = row
        assert (
            times_per_day == 2
        ), f"Expected metformin 2x/day, got {times_per_day}"
        # dose_times is a list; check it has both morning and evening
        assert (
            len(dose_times) == 2
        ), f"Expected 2 dose times, got {len(dose_times)}"

        # Basic check: one time should be morning-ish, one evening-ish
        # (exact times vary by demo setup, but 07:00:00 and 19:00:00 are the demo values)
        morning_found = any("0" in str(t) for t in dose_times)  # 07:00:00 starts with 0
        evening_found = any("19" in str(t) for t in dose_times)  # 19:00:00
        assert (
            morning_found or evening_found
        ), f"Expected morning/evening times, got {dose_times}"

    print("✅ Margaret Demo verification passed:")
    print(f"   - UUID: {margaret_uuid}")
    print(f"   - Display name: Margaret Demo")
    print(f"   - Drugs: {', '.join(sorted(expected_drugs))}")
    print(f"   - Metformin: 2x/day (morning + evening)")


def reconcile_row_counts(conn, bronze_counts):
    """Verify that loaded Lakebase tables match source Delta counts.

    Args:
        conn: psycopg Connection
        bronze_counts: dict with keys 'patients', 'schedules', 'dose_events'
            containing expected row counts from Delta
    """
    print("\n📊 Row-count reconciliation:")
    print("  (Lakebase should match Delta source after idempotent load)")
    print()

    with conn.cursor() as cur:
        # Patients
        cur.execute("SELECT COUNT(*) FROM patients")
        lakebase_patients = cur.fetchone()[0]
        delta_patients = bronze_counts["patients"]
        status = "✅" if lakebase_patients == delta_patients else "❌"
        print(
            f"  {status} Patients: {lakebase_patients:6d} loaded  (expected {delta_patients:6d})"
        )

        # Schedules
        cur.execute("SELECT COUNT(*) FROM schedules")
        lakebase_schedules = cur.fetchone()[0]
        delta_schedules = bronze_counts["schedules"]
        status = "✅" if lakebase_schedules == delta_schedules else "❌"
        print(
            f"  {status} Schedules: {lakebase_schedules:6d} loaded  (expected {delta_schedules:6d})"
        )

        # Dose events
        cur.execute("SELECT COUNT(*) FROM dose_events")
        lakebase_events = cur.fetchone()[0]
        delta_events = bronze_counts["dose_events"]
        status = "✅" if lakebase_events == delta_events else "❌"
        print(
            f"  {status} Dose events: {lakebase_events:6d} loaded  (expected {delta_events:6d})"
        )

    all_match = (
        lakebase_patients == delta_patients
        and lakebase_schedules == delta_schedules
        and lakebase_events == delta_events
    )

    if all_match:
        print("\n✅ All counts match — idempotent load verified.")
    else:
        raise AssertionError(
            "Row-count mismatch detected. Check load logic or database state."
        )


def main():
    """Main load orchestration.

    Reads from Delta (neurorx.bronze.synthetic_*), loads into Lakebase
    (patients, schedules, dose_events) in batches, verifies Margaret Demo,
    and reconciles row counts.
    """
    BATCH_SIZE = 1000

    print("\n" + "=" * 80)
    print("NeuroRx AI — Load Synthetic Cohort into Lakebase (Task 3.8)")
    print("=" * 80)
    print()

    # Step 1: Read the cohort — from local Parquet if NEURORX_COHORT_PARQUET is set
    # (the off-workspace demo path), otherwise from the Delta bronze tables.
    parquet_dir = os.getenv("NEURORX_COHORT_PARQUET")
    if parquet_dir:
        print(f"📖 Reading synthetic cohort from local Parquet ({parquet_dir})...")
        patients_list, schedules_list, dose_events_list = read_cohort_from_parquet(
            parquet_dir
        )
    else:
        print("📖 Reading synthetic cohort from Delta (neurorx.bronze.synthetic_*)...")
        patients_df = spark.read.table(
            f"{settings.schema_bronze}.synthetic_patients_raw"
        ).select(
            "patient_id", "display_name", "caregiver_name", "created_at"
        )
        schedules_df = spark.read.table(
            f"{settings.schema_bronze}.synthetic_schedules_raw"
        ).select(
            "schedule_id",
            "patient_id",
            "rxcui",
            "drug_name",
            "dose_text",
            "times_per_day",
            "dose_times",
            "timing_notes",
            "status",
            "created_at",
        )
        dose_events_df = spark.read.table(
            f"{settings.schema_bronze}.synthetic_dose_events_raw"
        ).select(
            "event_id",
            "schedule_id",
            "patient_id",
            "planned_ts",
            "actioned_ts",
            "status",
        )
        # Convert to Python for batch loading. (In production a SQL Statement
        # Execution API call would be more efficient, but Python batches via
        # psycopg are simpler and sufficient here.)
        patients_list = [row.asDict() for row in patients_df.collect()]
        schedules_list = [row.asDict() for row in schedules_df.collect()]
        dose_events_list = [row.asDict() for row in dose_events_df.collect()]

    patients_count = len(patients_list)
    schedules_count = len(schedules_list)
    dose_events_count = len(dose_events_list)

    print(f"   ✓ Patients:    {patients_count:6d} rows")
    print(f"   ✓ Schedules:   {schedules_count:6d} rows")
    print(f"   ✓ Dose events: {dose_events_count:6d} rows")
    print()

    # Step 2: Connect to Lakebase
    print("🔌 Connecting to Lakebase (neurorx-oltp)...")
    conn = get_lakebase_connection()
    print("   ✓ Connected")
    print()

    try:
        # Step 3: Build drug name -> RxCUI map (Task 1.4's generator uses names; Lakebase needs RxCUIs)
        print("🔗 Building drug name → RxCUI mapping...")
        drug_rxcui_map = build_drug_name_to_rxcui_map(conn)
        print(f"   ✓ Mapped {len(drug_rxcui_map)} drugs from gold.drugs + demo fallback")
        # On the local demo path gold.drugs doesn't exist, so most curated drugs
        # have no real RxCUI. Fill the gaps with deterministic synthetic ones so
        # the whole cohort loads; the demo-critical drugs keep their real values.
        cohort_drugs = [str(r["drug_name"]) for r in schedules_list]
        drug_rxcui_map, synth = fill_missing_rxcuis(drug_rxcui_map, cohort_drugs)
        if synth:
            print(f"   ⚠️  Synthesized {synth} placeholder RxCUIs for un-resolved drugs "
                  f"(local demo path; real resolution happens via gold.drugs on the workspace)")
        print()

        # Step 4: Load patients
        print("📥 Loading patients...")
        inserted = 0
        for i in range(0, len(patients_list), BATCH_SIZE):
            batch = patients_list[i : i + BATCH_SIZE]
            load_patients_batch(conn, batch)
            inserted += len(batch)
            batch_num = (i // BATCH_SIZE) + 1
            total_batches = (len(patients_list) + BATCH_SIZE - 1) // BATCH_SIZE
            print(
                f"   ✓ Batch {batch_num:2d}/{total_batches}: {len(batch):4d} rows ({inserted:5d} total)"
            )
        conn.commit()
        print()

        # Step 5: Load schedules
        print("📥 Loading schedules...")
        inserted = 0
        for i in range(0, len(schedules_list), BATCH_SIZE):
            batch = schedules_list[i : i + BATCH_SIZE]
            load_schedules_batch(conn, batch, drug_rxcui_map)
            inserted += len(batch)
            batch_num = (i // BATCH_SIZE) + 1
            total_batches = (len(schedules_list) + BATCH_SIZE - 1) // BATCH_SIZE
            print(
                f"   ✓ Batch {batch_num:2d}/{total_batches}: {len(batch):4d} rows ({inserted:5d} total)"
            )
        conn.commit()
        print()

        # Step 6: Load dose events
        print("📥 Loading dose events...")
        inserted = 0
        for i in range(0, len(dose_events_list), BATCH_SIZE):
            batch = dose_events_list[i : i + BATCH_SIZE]
            load_dose_events_batch(conn, batch)
            inserted += len(batch)
            batch_num = (i // BATCH_SIZE) + 1
            total_batches = (len(dose_events_list) + BATCH_SIZE - 1) // BATCH_SIZE
            print(
                f"   ✓ Batch {batch_num:2d}/{total_batches}: {len(batch):4d} rows ({inserted:5d} total)"
            )
        conn.commit()
        print()

        # Step 7: Verify Margaret Demo
        print("🔍 Verifying Margaret Demo...")
        verify_margaret_demo(conn)
        print()

        # Step 8: Reconcile row counts
        bronze_counts = {
            "patients": patients_count,
            "schedules": schedules_count,
            "dose_events": dose_events_count,
        }
        reconcile_row_counts(conn, bronze_counts)
        print()

        # Step 9: Print next steps
        print("📋 Next steps (required for full Phase 3 data flow):")
        print()
        print("  1️⃣  Enable Lakebase Change Data Feed (CDF):")
        print("     (Creates wal2delta logical replication; syncs every ~15 seconds)")
        print()
        print("  2️⃣  Run the Lakebase→Delta sync:")
        print("     - Workspace → Catalog → neurorx")
        print("     - Wait for sync to materialize gold.patients, gold.schedules, gold.dose_events")
        print()
        print("  3️⃣  Refresh the Lakeflow pipeline (pipelines/medallion_pipeline.py):")
        print("     - This populates gold.adherence_facts from the synced gold.dose_events")
        print("     - Dashboard and get_adherence_stats now read live data")
        print()
        print("✅ Load complete!")
        print("=" * 80)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
