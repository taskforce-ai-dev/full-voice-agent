import importlib
import os


def _reload(backend):
    os.environ["KB_BACKEND"] = backend
    import knowledge_base
    return importlib.reload(knowledge_base)


def test_default_is_chroma(monkeypatch):
    monkeypatch.delenv("KB_BACKEND", raising=False)
    import knowledge_base
    kb = importlib.reload(knowledge_base)
    assert kb.ACTIVE_BACKEND == "chroma"


def test_all_four_functions_present_both_backends():
    for backend in ("chroma", "sqlite"):
        kb = _reload(backend)
        for name in ("retrieve_context", "initialize_kb", "prewarm", "reload_kb_from_content"):
            assert callable(getattr(kb, name)), f"{name} missing in {backend}"


def test_sqlite_selected_when_flagged():
    kb = _reload("sqlite")
    assert kb.ACTIVE_BACKEND == "sqlite"
