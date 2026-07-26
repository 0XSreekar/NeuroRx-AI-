"""The home page is public and must never leak patient data."""

from streamlit.testing.v1 import AppTest


def _script():
    from app.views import home

    home.render(on_signup=lambda: None, on_login=lambda: None)


def test_home_renders_the_product_name():
    at = AppTest.from_function(_script).run()
    assert not at.exception
    assert any("NeuroRx" in m.value for m in at.markdown)


def test_home_offers_both_entry_points():
    at = AppTest.from_function(_script).run()
    labels = {b.label for b in at.button}
    assert "Create account" in labels
    assert "Sign in" in labels


def test_home_states_the_safety_position():
    at = AppTest.from_function(_script).run()
    body = " ".join(m.value for m in at.markdown).lower()
    assert "not medical advice" in body


def test_home_mentions_no_patient_names():
    """A public page must not name anyone from the cohort."""
    at = AppTest.from_function(_script).run()
    body = " ".join(m.value for m in at.markdown).lower()
    assert "margaret" not in body
