"""NeuroRx AI — Chat view (Task 3.4).

The Chat tab: conversation with the supervisor agent, citation chips on
grounded answers, prescription-upload → extraction-confirmation flow, and
explicit confirm/cancel cards for anything `manage_schedule` won't write
without a human saying so.

## What this reuses from Databricks' own current chat-app template, and what's different

The streaming-with-spinner-fallback structure (`_send_message()` below) is
adapted from `databricks/app-templates/e2e-chatbot-app` (`app.py` +
`model_serving_utils.py`, fetched live via `raw.githubusercontent.com` this
session, same discipline Task 2.6 already applied to `agent/agent.py`) —
specifically its `query_chat_completions_endpoint_and_render()`'s pattern of
`st.chat_message` + `st.empty()` placeholder + accumulate-and-rerender in a
`try`/`except` that falls back to a non-streaming call on any error.

**What's different, deliberately**: the template is a generic multi-endpoint-
type chatbot (it branches on `chat/completions` vs `agent/v2/chat` vs
`agent/v1/responses`, since it doesn't know in advance what kind of endpoint
it's pointed at). This view doesn't need that branching — `neurorx-agent` is
always exactly one `agent/v1/responses`-shaped `NeuroRxAgent`
(`agent/agent.py`, Task 2.6) — so this file calls straight into
`app/agent_client.py`'s `chat()`/`chat_stream()`, which already do the
Responses-API-specific parsing (Task 3.3, extended this task for streaming
and confirmation-payload extraction), rather than reimplementing endpoint-
type detection this project will never need.

## The UI is the confirmation surface, not the model (Requirement 5)

`agent_client.chat()`/`chat_stream()` (via `parse_agent_output()`) already
dig `pending_confirmation` out of the agent's raw tool-call trace — the
*attempted* `action`/`payload` paired with `manage_schedule`'s own verdict,
not the model's paraphrase of either. This view renders that as an explicit
card with real Confirm/Cancel buttons; nothing here ever sets
`user_confirmed`/`confirmed_interactions` based on the model's own words,
only on an actual button click in this session.
"""

import streamlit as st

from app import agent_client


def render(patient_id: str) -> None:
    """Entry point called by app/app.py inside the Chat tab."""
    _init_session_state()

    if not patient_id:
        st.info("📋 No patient selected. Choose a patient ID in the sidebar to chat.")
        return

    st.caption(f"💬 Chatting as patient `{patient_id[:8]}...`")

    _render_prescription_upload(patient_id)
    _render_pending_confirmation_card_if_any(patient_id)
    _render_history()

    user_text = st.chat_input("Ask about your medications, adherence, or interactions...")
    if user_text and patient_id:
        _send_message(patient_id, user_text)
        st.rerun()


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


def _init_session_state() -> None:
    if "chat_messages" not in st.session_state:
        # Each entry: {"role": "user"/"assistant", "content": str,
        #              "resolved_citations": [dict, ...]}  (assistant only)
        st.session_state.chat_messages = []
    if "pending_extraction" not in st.session_state:
        # The propose() payload awaiting Confirm/Edit/Cancel, or None.
        st.session_state.pending_extraction = None
    if "extraction_editing" not in st.session_state:
        st.session_state.extraction_editing = False
    if "pending_confirmation" not in st.session_state:
        # The most recent needs_confirmation/blocked_pending_confirmation
        # payload surfaced by either the agent's own tool call or the app's
        # own call_manage_schedule after a Confirm click, or None.
        st.session_state.pending_confirmation = None


# ---------------------------------------------------------------------------
# Sending a message — streaming if the endpoint supports it, else spinner
# ---------------------------------------------------------------------------


def _send_message(patient_id: str, user_text: str) -> None:
    st.session_state.chat_messages.append({"role": "user", "content": user_text})

    # Responses-API shape expected by agent_client.chat()/chat_stream():
    # only role+content, no resolved_citations — strip that UI-only field
    # before sending history back to the model.
    history = [
        {"role": m["role"], "content": m["content"]} for m in st.session_state.chat_messages
    ]

    result = _render_streaming_response(patient_id, history)
    if result is None:
        result = _render_spinner_response(patient_id, history)

    resolved_citations = (
        agent_client.resolve_citations(result["citations"]) if result["citations"] else []
    )
    st.session_state.chat_messages.append(
        {
            "role": "assistant",
            "content": result["text"],
            "resolved_citations": resolved_citations,
        }
    )
    if result.get("pending_confirmation"):
        st.session_state.pending_confirmation = result["pending_confirmation"]


def _render_streaming_response(patient_id: str, history: list[dict]) -> dict | None:
    """Try streaming; return None (not an empty result) on any failure so
    the caller falls back to the spinner path — mirrors the verified
    template's try/except-around-the-whole-stream structure, not just
    around the connection setup.
    """
    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("_Thinking..._")
        accumulated_text = ""
        last_output_items: list[dict] = []
        try:
            for event in agent_client.chat_stream(history, patient_id):
                # ResponsesAgentStreamEvent shapes, per agent/agent.py's own
                # _process_agent_stream_events(): "response.output_text.delta"
                # for token deltas, "response.output_item.done" for a
                # completed item (message, function_call, or
                # function_call_output) we need for citation/confirmation
                # extraction once the stream ends.
                event_type = event.get("type")
                if event_type == "response.output_text.delta":
                    accumulated_text += event.get("delta", "")
                    placeholder.markdown(accumulated_text)
                elif event_type == "response.output_item.done":
                    last_output_items.append(event.get("item", {}))
        except NotImplementedError:
            # Confirmed real signal (see agent_client.chat_stream()'s
            # docstring): the deployment client doesn't support streaming at
            # all — fall back without alarming the user.
            placeholder.empty()
            return None
        except Exception:
            # Any other failure mid-stream — same fallback, but leave a
            # visible trace rather than silently retrying, matching the
            # template's own "Ran into an error. Retrying..." pattern.
            placeholder.markdown("_Ran into an error — retrying without streaming..._")
            return None

        parsed = agent_client.parse_agent_output(last_output_items)
        placeholder.markdown(parsed["text"] or accumulated_text)
        return parsed


def _render_spinner_response(patient_id: str, history: list[dict]) -> dict:
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            result = agent_client.chat(history, patient_id)
        st.markdown(result["text"])
        return result


# ---------------------------------------------------------------------------
# Rendering history + citation chips (Requirement 3)
# ---------------------------------------------------------------------------


def _render_history() -> None:
    for message in st.session_state.chat_messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["role"] == "assistant":
                _render_citation_chips(message.get("resolved_citations", []))


def _render_citation_chips(resolved_citations: list[dict]) -> None:
    """One chip per citation actually present in the response — never for
    an uncited clinical-looking sentence (Requirement 3's own words). This
    is naturally satisfied by construction: a chip only exists here because
    its chunk_id was found by CHUNK_ID_PATTERN in the response text
    (`agent_client.parse_agent_output()`); nothing invents one for text
    that merely sounds clinical.

    `st.expander` is this view's "chip that expands on click" — Streamlit
    has no dedicated inline-chip widget; an expander collapsed by default,
    labeled "drug — section", is the closest native primitive that matches
    "clicking expands the verbatim chunk_text + set_id" exactly as asked.
    """
    if not resolved_citations:
        return

    st.markdown("**📚 Sources:**")
    cols = st.columns(min(3, len(resolved_citations)))
    for idx, citation in enumerate(resolved_citations):
        section_label = citation.get('section', 'info').replace('_', ' ').title()
        with cols[idx % len(cols)]:
            with st.expander(f"📄 {citation.get('drug_name', '?')} — {section_label}"):
                st.markdown(f"> {citation.get('chunk_text', '(text unavailable)')}")
                st.caption(f"Set ID: `{citation.get('set_id', '?')}`")
                st.caption(f"Chunk: `{citation.get('chunk_id', '?')}`")


# ---------------------------------------------------------------------------
# Pending confirmation card (Requirement 5) — shared by the chat agent's own
# tool calls AND the app's direct call_manage_schedule after a Confirm click
# ---------------------------------------------------------------------------


def _render_pending_confirmation_card_if_any(patient_id: str) -> None:
    pending = st.session_state.pending_confirmation
    if not pending:
        return

    with st.container(border=True):
        if pending["status"] == "blocked_pending_confirmation":
            st.error("🚨 **Drug Interaction Alert** — Action blocked pending your confirmation")
            st.markdown("**Interactions found with your current medications:**")
            for interaction in pending.get("interactions", []):
                severity = interaction.get("severity", "unknown").upper()
                severity_icon = "🔴" if severity == "MAJOR" else "🟡" if severity == "MODERATE" else "🔵"
                st.markdown(
                    f"{severity_icon} **{severity}** — {interaction.get('description', 'No description available.')}"
                )
                sources = interaction.get("sources", [])
                if sources:
                    st.caption(f"Source: {', '.join(sources)}")
        else:  # needs_confirmation
            st.warning("📋 **Schedule Change Pending**  \nPlease review and confirm this change:")
            st.json(pending.get("proposed_change", {}), expanded=True)

        col_confirm, col_cancel = st.columns(2)
        with col_confirm:
            if st.button("✓ Confirm", key="confirm_pending", type="primary", use_container_width=True):
                payload = dict(pending.get("payload") or {})
                payload["user_confirmed"] = True
                if pending["status"] == "blocked_pending_confirmation":
                    payload["confirmed_interactions"] = True
                with st.spinner("Updating schedule..."):
                    response = agent_client.call_manage_schedule(
                        pending.get("patient_id") or patient_id, pending["action"], payload
                    )
                st.session_state.pending_confirmation = (
                    response if response.get("status") in
                    ("needs_confirmation", "blocked_pending_confirmation")
                    else None
                )
                st.rerun()
        with col_cancel:
            if st.button("✕ Cancel", key="cancel_pending", use_container_width=True):
                st.session_state.pending_confirmation = None
                st.rerun()


# ---------------------------------------------------------------------------
# Prescription upload + extraction confirmation card (Requirement 4)
# ---------------------------------------------------------------------------


def _render_prescription_upload(patient_id: str) -> None:
    with st.expander("📷 Add a Prescription (Photo or Text)", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            uploaded_file = st.file_uploader("Prescription Photo", type=["png", "jpg", "jpeg"], help="JPG, PNG, up to 200MB")
        with col2:
            st.markdown("")
            st.caption("**or** paste below:")

        pasted_text = st.text_area("Prescription Text", placeholder="Paste the prescription text here...", height=100)

        if st.button("📄 Extract Prescription", key="extract_button", type="primary", use_container_width=True):
            if uploaded_file is not None:
                image_or_text = uploaded_file.read()
            elif pasted_text.strip():
                image_or_text = pasted_text.strip()
            else:
                st.warning("⚠️ Please upload a photo or paste prescription text first.")
                image_or_text = None

            if image_or_text is not None:
                with st.spinner("📖 Reading prescription..."):
                    try:
                        st.session_state.pending_extraction = agent_client.extract_prescription(
                            image_or_text
                        )
                        st.session_state.extraction_editing = False
                        st.success("✅ Prescription read successfully!")
                    except Exception as exc:
                        st.error(f"❌ Couldn't read this prescription: {exc}")

    if st.session_state.pending_extraction is not None:
        _render_extraction_confirmation_card(patient_id)


def _render_extraction_confirmation_card(patient_id: str) -> None:
    extraction = st.session_state.pending_extraction
    drugs = extraction["drugs"]

    with st.container(border=True):
        st.markdown("### 📋 Confirm Prescription Drugs")
        st.caption(f"{len(drugs)} drug{'s' if len(drugs) != 1 else ''} found — please review:")

        table_rows = [
            {
                "Drug": ("⚠️ " if d.get("needs_review") else "✓ ") + d.get("drug_name", ""),
                "Strength": d.get("strength", ""),
                "Frequency": d.get("frequency_text", ""),
                "Times/Day": d.get("times_per_day", ""),
                "Dose Times": ", ".join(d.get("dose_times") or []),
                "Matched RxCUI": d.get("matched_name") or "(uncertain match)",
            }
            for d in drugs
        ]

        flagged_drugs = [(i, d) for i, d in enumerate(drugs) if d.get("needs_review") and d.get("review_reasons")]
        if flagged_drugs:
            st.warning("**⚠️ Needs Review:**")
            for i, d in flagged_drugs:
                st.caption(f"• **{d.get('drug_name')}**: {'; '.join(d['review_reasons'])}")

        if st.session_state.extraction_editing:
            edited = st.data_editor(table_rows, key="extraction_editor", num_rows="fixed", height=200)
        else:
            st.dataframe(table_rows, hide_index=True, use_container_width=True)
            edited = table_rows

        col_confirm, col_edit, col_cancel = st.columns(3)
        with col_confirm:
            if st.button("✓ Confirm & Add", key="confirm_extraction", type="primary", use_container_width=True):
                _submit_extraction(patient_id, drugs, edited)
        with col_edit:
            if st.button("✏️ Edit", key="edit_extraction", use_container_width=True):
                st.session_state.extraction_editing = not st.session_state.extraction_editing
                st.rerun()
        with col_cancel:
            if st.button("✕ Cancel", key="cancel_extraction", use_container_width=True):
                st.session_state.pending_extraction = None
                st.session_state.extraction_editing = False
                st.rerun()


def _submit_extraction(patient_id: str, original_drugs: list[dict], edited_rows: list[dict]) -> None:
    """Only this function calls manage_schedule for an extraction
    (Requirement 4's own "only Confirm calls...") — mapping the (possibly
    edited) table rows back to the drugs shape manage_schedule's
    create_from_extraction action expects: {rxcui, drug_name, dose_text,
    times_per_day, dose_times, timing_notes?}.
    """
    drugs_payload = []
    for original, row in zip(original_drugs, edited_rows):
        drugs_payload.append(
            {
                "rxcui": original.get("rxcui"),
                "drug_name": row["Drug"].removeprefix("⚠️ ").removeprefix("✓ ").strip(),
                "dose_text": row["Strength"],
                "times_per_day": row["Times/Day"],
                "dose_times": [t.strip() for t in row["Dose Times"].split(",") if t.strip()],
                "timing_notes": original.get("timing_notes") or None,
            }
        )

    payload = {"drugs": drugs_payload, "user_confirmed": True}
    response = agent_client.call_manage_schedule(patient_id, "create_from_extraction", payload)

    if response.get("status") in ("needs_confirmation", "blocked_pending_confirmation"):
        # create_from_extraction ran its mandatory interaction check
        # (agent/tools/manage_schedule.py, Task 2.3) and found a hit — same
        # explicit card as an agent-surfaced one, not a silent override.
        response["patient_id"] = patient_id
        response["action"] = "create_from_extraction"
        response["payload"] = payload
        st.session_state.pending_confirmation = response
    else:
        st.success("Schedule updated.")

    st.session_state.pending_extraction = None
    st.session_state.extraction_editing = False
    st.rerun()
