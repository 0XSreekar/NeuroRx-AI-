# Placeholder: post-generation output guardrail
#
# Phase 4 deliverable. Lightweight check (regex + one cheap LLM-judge call) that blocks
# any response containing un-cited dosage instructions. Every block is logged to a Delta
# table — shown in the demo as proof of the safety net catching a bad output.
#
# See ARCHITECTURE.md §5 and §7.
