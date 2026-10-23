"""MLflow tracking: DagsHub when MLFLOW_TRACKING_URI is set, else a local store under ./mlruns.

Switching to DagsHub is configuration only (MLFLOW_TRACKING_URI / _USERNAME / _PASSWORD, read by
MLflow itself). The local fallback is a SQLite tracking DB `./mlruns/mlflow.db` with artifacts in
`./mlruns/artifacts/` (git-ignored): MLflow >= 3.x refuses the plain `./mlruns` file store unless
MLFLOW_ALLOW_FILE_STORE is set (DATA_NOTES §15). MLflow holds params, metrics and small tables;
model binaries live in the object store and MLflow records only their key (plan §3.8e).
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LOCAL_ROOT = Path(__file__).resolve().parents[3] / "mlruns"


def tracking_uri() -> str:
    uri = os.environ.get("MLFLOW_TRACKING_URI")
    if uri:
        return uri
    LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + (LOCAL_ROOT / "mlflow.db").as_posix()


@contextmanager
def start_run(
    experiment: str, run_name: str, tags: Mapping[str, str] | None = None
) -> Iterator[Any]:
    """An MLflow run on the configured store; yields the `mlflow` module's active run."""
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    import mlflow

    mlflow.set_tracking_uri(tracking_uri())
    if mlflow.get_experiment_by_name(experiment) is None:
        location = (
            None if os.environ.get("MLFLOW_TRACKING_URI") else (LOCAL_ROOT / "artifacts").as_uri()
        )
        mlflow.create_experiment(experiment, artifact_location=location)
    mlflow.set_experiment(experiment)
    with mlflow.start_run(run_name=run_name, tags=dict(tags or {})) as run:
        yield run


def log(params: Mapping[str, Any], metrics: Mapping[str, float], texts: Mapping[str, str]) -> None:
    """Log to the active run. NaN metrics are skipped (MLflow rejects them on some stores)."""
    import mlflow

    mlflow.log_params({k: str(v)[:500] for k, v in params.items()})
    mlflow.log_metrics({k: float(v) for k, v in metrics.items() if v == v})
    for name, body in texts.items():
        mlflow.log_text(body, name)
