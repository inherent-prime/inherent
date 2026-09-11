"""Guard the PyPI release lane and its clean-wheel install check."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_publish_workflow_releases_both_python_packages_with_oidc() -> None:
    text = (REPO_ROOT / ".github/workflows/publish.yml").read_text()
    assert "build-cli:" in text
    assert "publish-cli:" in text
    assert "environment: pypi-publish" in text
    assert "id-token: write" in text
    assert "pypa/gh-action-pypi-publish" in text
    assert "test.pypi.org" in text
    assert "PYPI_API_TOKEN" not in text
    assert "inh-contracts" in text
    assert "inherent" in text
    assert "python -m venv" in text
    assert "inherent --version" in text
    assert "importlib.resources" in text


def test_cli_distribution_pins_the_published_contract_package() -> None:
    text = (REPO_ROOT / "services/inh-cli/pyproject.toml").read_text()
    assert '"inh-contracts>=2.2,<3"' in text
