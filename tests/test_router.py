"""The gate is structural: while signed out, the app page is not registered,
so there is nothing for that URL to route to.
"""

from streamlit.testing.v1 import AppTest

APP = "app/app.py"


def test_signed_out_lands_on_the_home_page():
    at = AppTest.from_file(APP).run()
    assert not at.exception
    assert any(b.label == "Create account" for b in at.button)


def test_signed_out_does_not_render_the_app_tabs():
    at = AppTest.from_file(APP).run()
    assert not at.tabs


def test_signed_out_never_shows_the_safety_ticker():
    """The ticker belongs to the app shell; seeing it signed out would mean
    the gated page rendered."""
    at = AppTest.from_file(APP).run()
    body = " ".join(m.value for m in at.markdown)
    assert "Organizational assistant" not in body
