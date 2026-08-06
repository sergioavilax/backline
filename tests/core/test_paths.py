"""Repo-root path anchoring (D-022): artifact/trace paths never depend on the CWD.

Eval run ddb797dc's artifacts nested inside an older run's directory because the
harness resolved ``data/evals`` against the process CWD. Relative configured paths
now anchor at the repo root; absolute ones pass through untouched.
"""

from pathlib import Path

import pytest

from backline.config import Settings, anchor_path, repo_root


def test_repo_root_is_the_project_root() -> None:
    root = repo_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "backline" / "config.py").is_file()


def test_anchor_path_resolves_relative_against_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The regression shape: launched from inside an old artifact directory, a
    # relative path must still land under the repo root, not under the CWD.
    monkeypatch.chdir(tmp_path)
    anchored = anchor_path(Path("data/evals"))
    assert anchored == repo_root() / "data" / "evals"
    assert not anchored.is_relative_to(tmp_path)


def test_anchor_path_leaves_absolute_paths_alone(tmp_path: Path) -> None:
    assert anchor_path(tmp_path / "elsewhere") == tmp_path / "elsewhere"
    assert anchor_path(str(tmp_path / "elsewhere")) == tmp_path / "elsewhere"


def test_settings_data_path_anchors_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(data_dir="data")
    assert settings.data_path == repo_root() / "data"
    # Compose-style absolute configuration is authoritative as-is.
    absolute = Settings(data_dir=str(tmp_path / "data"))
    assert absolute.data_path == tmp_path / "data"


def test_eval_artifact_dir_is_cwd_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uuid

    from evals.runner import RunnerConfig, artifact_dir
    from evals.types import Suite

    suite = Suite(name="empty", world_seed=1, suite_hash="deadbeef", questions=[])
    config = RunnerConfig(suite=suite, model="mock-sonnet")
    run_id = uuid.uuid4()

    monkeypatch.chdir(tmp_path)
    resolved = artifact_dir(config, run_id)
    assert resolved == repo_root() / "data" / "evals" / str(run_id)

    pinned = RunnerConfig(suite=suite, model="mock-sonnet", out_dir=tmp_path / "out")
    assert artifact_dir(pinned, run_id) == tmp_path / "out" / str(run_id)
