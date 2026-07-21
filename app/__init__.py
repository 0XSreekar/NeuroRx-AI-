"""NeuroRx AI application package.

Exists so `app.config`, `app.db`, `app.agent_client`, and `app.views.*` — the
import form every module in this project already uses — actually resolve. The
directory had no `__init__.py`, so `import app.db` only worked by accident of
namespace-package semantics in some launch paths and failed outright in others
(notably `streamlit run app/app.py`, which puts `app/` rather than the repo root
on sys.path).

Deliberately empty of logic: importing this package must stay free of side
effects, since `app/config.py` already fails loudly at import when its nine env
vars are missing and there is no reason to add a second import-time failure mode
above it.
"""
