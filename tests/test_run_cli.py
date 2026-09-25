import pytest

import run


@pytest.mark.parametrize("flag", ["--agent", "--investigate", "--skip-fetch"])
def test_unsupported_graph_flags_fail_before_side_effects(flag, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("side effect before flag validation")

    monkeypatch.setattr(run.db, "connect", unexpected)
    monkeypatch.setattr(run.providers, "build_client", unexpected)
    with pytest.raises(SystemExit) as exc:
        run.main(["--graph", flag])
    assert exc.value.code == 2
    assert "--graph cannot be combined" in capsys.readouterr().err


def test_unsupported_ollama_investigation_fails_before_side_effects(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("side effect before flag validation")

    monkeypatch.setattr(run.db, "connect", unexpected)
    monkeypatch.setattr(run.providers, "build_client", unexpected)
    with pytest.raises(SystemExit) as exc:
        run.main(["--provider", "ollama", "--investigate"])
    assert exc.value.code == 2
    assert "--investigate requires --provider anthropic" in capsys.readouterr().err
