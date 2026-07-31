"""Live background and clickable wordmark.

The wordmark test is the load-bearing one: linking it on the signed-in page
would sign the user out, because following a real link is a full browser
navigation and st.session_state is per-session.
"""

from streamlit.testing.v1 import AppTest

from app import theme


# --- live background -------------------------------------------------------


def test_background_markup_has_blobs_and_star_layers():
    html = theme.live_background()
    for cls in ("b1", "b2", "b3", "b4", "s1", "s2", "s3"):
        assert f'"{cls}"' in html


def test_background_is_hidden_from_assistive_tech():
    """Decorative only — it must not be announced."""
    assert 'aria-hidden="true"' in theme.live_background()


def test_background_animations_are_defined_and_reduced_motion_is_honoured():
    css = theme._COMPONENTS
    for keyframes in ("nrx-drift-a", "nrx-drift-b", "nrx-drift-c", "nrx-drift-d",
                      "nrx-stars", "nrx-twinkle"):
        assert f"@keyframes {keyframes}" in css
    assert "prefers-reduced-motion" in css


def test_background_uses_the_mockups_own_blob_palette():
    """Same four colours as the mockup's canvas `blobs` array."""
    css = theme._COMPONENTS
    for rgb in ("70,190,210", "120,130,240", "200,170,110", "60,150,200"):
        assert rgb in css


def test_background_sits_behind_page_content():
    """The fixed layer is z-index 0; content must be lifted above it, or the
    background would paint over the page."""
    css = theme._COMPONENTS
    assert ".block-container { position: relative; z-index: 1; }" in css


# --- hero preview ----------------------------------------------------------


def test_hero_preview_is_labelled_an_example():
    """It shows dose rows on a public page; it must never read as real data."""
    assert "EXAMPLE" in theme.hero_preview()


def test_hero_preview_shows_a_citation_chip():
    """The headline promises answers traced to a label — the preview is what
    makes that concrete."""
    html = theme.hero_preview()
    assert "FDA LABEL" in html
    assert "chip" in html


def test_home_renders_the_hero_preview():
    at = AppTest.from_function(_home_script).run()
    assert not at.exception
    body = " ".join(m.value for m in at.markdown)
    assert "nrx-preview" in body


def test_home_still_touches_no_database():
    """The home page is public. Guard against a future edit importing db.

    Checked by parsing the imports, not by substring: home.py's own docstring
    says it must not touch `app/db.py`, so a naive `"db." in source` check
    fails on the very comment that documents the rule.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/views/home.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)

    assert not any("db" in name.split(".") for name in imported), imported


# --- clickable wordmark ----------------------------------------------------


def test_brand_is_plain_by_default():
    assert "<a" not in theme.brand()


def test_brand_links_when_given_an_href():
    html = theme.brand(href="/")
    assert 'href="/"' in html
    assert "NeuroRx" in html


# AppTest.from_function re-parses the function's SOURCE, so these must be real
# defs — a lambda raises SyntaxError inside Streamlit's magic-parsing step.


def _home_script():
    from app.views import home

    home.render(on_signup=lambda: None, on_login=lambda: None)


def _signup_script():
    from app.views import signup

    signup.render(on_success=lambda: None, on_login=lambda: None)


def _login_script():
    from app.views import login

    login.render(on_success=lambda: None, on_signup=lambda: None)


def test_home_signup_and_login_all_link_the_wordmark_home():
    for name, script in (
        ("home", _home_script),
        ("signup", _signup_script),
        ("login", _login_script),
    ):
        at = AppTest.from_function(script).run()
        assert not at.exception, name
        body = " ".join(m.value for m in at.markdown)
        assert 'href="/"' in body, f"{name} wordmark is not a link home"


def test_home_signup_and_login_all_paint_the_live_background():
    for name, script in (
        ("home", _home_script),
        ("signup", _signup_script),
        ("login", _login_script),
    ):
        at = AppTest.from_function(script).run()
        body = " ".join(m.value for m in at.markdown)
        assert 'class="nrx-bg"' in body, f"{name} has no live background"


def test_signed_in_page_does_not_link_the_wordmark():
    """A full navigation would drop st.session_state and sign the user out, so
    the app page's wordmark must stay inert."""
    source = (
        __import__("pathlib").Path("app/app.py").read_text()
    )
    assert "theme.brand(href" not in source
