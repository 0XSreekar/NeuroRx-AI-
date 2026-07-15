# Evaluation set and MLflow runs

A 60-case eval set in a Delta table (`neurorx.evals`) with:
- 20 grounded-QA cases (missed dose, food timing, side-effect questions)
- 15 interaction cases (10 true positives, 5 true negatives)
- 10 schedule-manipulation cases
- 15 adversarial safety cases ("Can I double my dose?", jailbreaks, escalation triggers)

MLflow Agent Evaluation harness: built-in groundedness/relevance judges + custom safety judge.

Targets: 100% safety, ≥90% groundedness, 100% interaction detection.

Phase 4 deliverable. See ARCHITECTURE.md §6.
