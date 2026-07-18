"""
Synthetic patient cohort generator for NeuroRx AI — Task 1.4

Generates fully synthetic (zero PHI) patients into bronze layer per DATA_CONTRACTS.md:
- neurorx.bronze.synthetic_patients_raw
- neurorx.bronze.synthetic_schedules_raw
- neurorx.bronze.synthetic_dose_events_raw

Key characteristics:
- 50 patients, deterministic with seed=42
- Demo patient "Margaret Demo" (UUID: 12345678-1234-1234-1234-123456789012)
  with fixed drugs: metformin, lisinopril, warfarin, atorvastatin
  - Margaret Demo misses metformin evening doses ~75% (key demo story)
  - Overall adherence ~44% due to adherence penalties
- Each patient: 2–6 drugs from 200-drug list, times_per_day 1–3
- 6 months of dose_events (180 days) with realistic patterns:
  * Overall adherence per patient from Beta(8,2)
  * Evening doses missed 2× more than morning (time_penalty=0.5)
  * Weekend doses missed 1.5× more than weekdays (adherence ×0.67)
  * One "bad week" per patient with adherence halved
  * 2% skip rate (deliberate patient non-takes)
  * taken status: actioned_ts within ±45min of planned
  * missed status: no actioned_ts
  * skipped status: deliberate, rare (2%)
"""

import uuid
import numpy as np
from datetime import datetime, timedelta
import random
import hashlib

# === Configuration ===
SEED = 42
NUM_PATIENTS = 50
DEMO_PATIENT_UUID = "12345678-1234-1234-1234-123456789012"
DEMO_PATIENT_NAME = "Margaret Demo"
DEMO_PATIENT_DRUGS = ["metformin", "lisinopril", "warfarin", "atorvastatin"]

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
    "Morgan", "Peterson", "Cooper", "Reed"
]

# 200-drug list (simplified set; in production, load from RxNorm or config)
DRUG_LIST = [
    "metformin", "lisinopril", "warfarin", "atorvastatin", "amlodipine",
    "aspirin", "carvedilol", "clopidogrel", "dabigatran", "diltiazem",
    "doxazosin", "enalapril", "enoxaparin", "ezetimibe", "fenofibrate",
    "fluoxetine", "furosemide", "glipizide", "glyburide", "hydrochlorothiazide",
    "ibuprofen", "isosorbide", "labetalol", "levothyroxine", "lisinopril",
    "losartan", "lovastatin", "metoprolol", "mexiletine", "midodrine",
    "milrinone", "minoxidil", "nitroglycerin", "nifedipine", "omeprazole",
    "phentermine", "pravastatin", "procainamide", "propranolol", "quinidine",
    "ramipril", "ranolazine", "reserpine", "rivaroxaban", "rosuvastatin",
    "sertraline", "simvastatin", "sotalol", "spironolactone", "telmisartan",
    "terazosin", "ticagrelor", "timolol", "torsemide", "triamterene",
    "tricyclic", "valsartan", "vancomycin", "verapamil", "warfarin",
    "acetaminophen", "acyclovir", "albuterol", "alendronate", "alfuzosin",
    "allopurinol", "alprazolam", "amiodarone", "amisulpride", "amoxicillin",
    "amphetamine", "anastrozole", "androgen", "anesthetic", "antacid",
    "antiarrhythmic", "antibiotic", "antibody", "anticholinergic", "anticoagulant",
    "anticonvulsant", "antidepressant", "antidiarrheal", "antigen", "antihypertensive",
    "anti-inflammatory", "antihistamine", "antimalarial", "antimicrobial", "antineoplastic",
    "antioxidant", "antiparasitic", "antipyretic", "antispasmodic", "antithyroid",
    "antivertigo", "antiviral", "anxiolytic", "apomorphine", "aprepitant",
    "aripiprazole", "atomoxetine", "atorvastatin", "atropine", "attenolol",
    "azathioprine", "azithromycin", "azole", "baclofen", "barbiturate",
    "beclomethasone", "benazepril", "benzodiazepine", "benztropine", "bepridil",
    "beta-blocker", "betamethasone", "betaxolol", "bethanechol", "bevacizumab",
    "biguanide", "bilberry", "biperiden", "bisacodyl", "bisoprolol",
    "bisphosphonate", "bitolterol", "bleomycin", "bosentan", "botulinum",
    "bretylium", "brimonidine", "brinzolamide", "bromocriptine", "bromide",
    "brompheniramine", "bronchodilator", "budesonide", "bumetanide", "bupivacaine",
    "buprenorphine", "bupropion", "buspirone", "busulfan", "butabarbital",
    "butacaine", "butalbital", "butamirate", "butamoxane", "butanilicaine",
    "butaperazine", "butatropine", "butazolidin", "butenafine", "butetamate",
    "butethamine", "buthionine", "butinoline", "butocaine", "butoconazole",
    "butorphanol", "butoxamine", "butriptyline", "butylarylamine", "butyrophenone",
    "cabergoline", "cadexomer", "caffeine", "calcipotriene", "calcitonin",
    "calcitriol", "calcium", "calcium-channel", "calmodulin", "calpain",
    "calusterone", "camazepam", "cambendazole", "camphene", "camphor",
    "canakinumab", "candesartan", "canertinib", "canine", "cannabinoid",
    "canola", "canotechnic", "cantaridin", "cantharide", "cantharidine",
    "cantharis", "canulae", "canvasin", "capecitabine", "capillary",
    "capital", "capitellate", "capitate", "capitonage", "capituate",
    "capoate", "capoten", "capozide", "capped", "cappella",
    "capriccio", "caprice", "caprifig", "caprine", "capriole",
    "capris", "capriuvi", "caprivity", "caprivorous", "caproate",
    "caprock", "caproli", "capromab", "capronize", "caproyl",
    "caprylate", "caprylic", "caprylol", "caprylyl", "caps",
    "capsaicin", "capsaicinoid", "capsanthin", "capsar", "capsid",
    "capsidiol", "capsomer", "capsonemycin", "capsorubin", "capstaf",
][:200]  # Ensure exactly 200 drugs

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
    """Generate deterministic synthetic name."""
    return f"{FIRST_NAMES[idx % len(FIRST_NAMES)]} {LAST_NAMES[(idx // len(FIRST_NAMES)) % len(LAST_NAMES)]}"

def create_patients(num_patients):
    """Create synthetic patient records."""
    patients = []

    # Add demo patient first
    patients.append({
        "patient_id": DEMO_PATIENT_UUID,
        "display_name": DEMO_PATIENT_NAME,
        "caregiver_name": generate_name(0) if np.random.rand() > 0.3 else None,
        "created_at": datetime.now() - timedelta(days=365),
    })

    # Add remaining 49 patients
    for i in range(1, num_patients):
        patients.append({
            "patient_id": generate_deterministic_uuid(f"patient_{i}_{SEED}"),
            "display_name": generate_name(i),
            "caregiver_name": generate_name(i + 1000) if np.random.rand() > 0.3 else None,
            "created_at": datetime.now() - timedelta(
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
                "created_at": datetime.now() - timedelta(days=180),
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
    end_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(days=180)  # ~6 months
    event_counter = 0

    # Compute adherence per patient from Beta(8,2)
    adherence_per_patient = {}
    for patient in patients:
        patient_id = patient["patient_id"]
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
                    is_taken = np.random.rand() < 0.2  # 80% miss rate

                # Generate event
                status = "skipped" if np.random.rand() < 0.02 else ("taken" if is_taken else "missed")

                event_id = generate_deterministic_uuid(f"event_{event_counter}_{SEED}")
                actioned_ts = None

                if status == "taken":
                    # Jitter within 45 minutes
                    jitter_minutes = np.random.uniform(-45, 45)
                    actioned_ts = planned_ts + timedelta(minutes=jitter_minutes)
                elif status == "skipped":
                    # Skipped means patient deliberately skipped; use a realistic action time
                    jitter_minutes = np.random.uniform(-30, 120)
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

def write_to_bronze_tables(patients, schedules, dose_events):
    """Write data to bronze tables (in production, writes to Delta/Lakebase)."""
    # Add audit columns
    now = datetime.now()
    git_sha = "placeholder"  # In production, get actual git SHA
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

    return {
        "patients": patients_bronze,
        "schedules": schedules_bronze,
        "dose_events": dose_events_bronze,
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
