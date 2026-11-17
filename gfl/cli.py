"""Command line interface: gfl run | analyze | compare | report | hypothesis-check."""

from __future__ import annotations

from pathlib import Path

import typer

from gfl.analyze import SYNTHETIC_BANNER, analyze, summary_display_table
from gfl.config import ConfigError, load_experiment
from gfl.manifest import ManifestError
from gfl.parse import ParseError
from gfl.phases import NotYetImplemented
from gfl.platform_support import UnsupportedPlatformError
from gfl.tables import markdown_table

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Game Frame Lab: benchmark capture and frame-time analysis.",
)

# Expected, user-facing errors are printed without a traceback.
_USER_ERRORS = (ConfigError, ManifestError, ParseError, UnsupportedPlatformError,
                NotYetImplemented, ValueError, RuntimeError)


def _fail(exc: Exception) -> None:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@app.command()
def run(
    config: Path = typer.Argument(..., help="Experiment YAML, e.g. configs/lyra_scalability.yaml"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate the config and print the run plan."),
) -> None:
    """Capture every config in an experiment (Windows benchmark PC)."""
    try:
        experiment = load_experiment(config)
        runs = experiment.expand()
        cap = experiment.capture
        total_min = len(runs) * cap.runs * (cap.duration_s + cap.cooldown_s) / 60
        typer.echo(f"Experiment: {experiment.name} ({config})")
        typer.echo(f"{len(runs)} configs x {cap.runs} runs x {cap.duration_s:g} s "
                   f"(first {cap.warmup_s:g} s dropped). About {total_min:.0f} min of capture.")
        for r in runs:
            typer.echo(f"  {r.config_id}")
        if dry_run:
            return
        from gfl.capture import capture_run

        capture_run(experiment)
    except _USER_ERRORS as exc:
        _fail(exc)


@app.command("analyze")
def analyze_cmd(
    run_dir: Path = typer.Argument(..., help="Capture directory containing manifest.csv"),
    out: Path = typer.Option(None, "--out", help="Output directory (default: results/<experiment>)"),
    trace_resolution: str = typer.Option(None, "--trace-resolution",
                                         help="Resolution for the frame-time chart, e.g. 3840x2160"),
) -> None:
    """Compute metrics per run and per config, write a summary table and charts."""
    try:
        result = analyze(run_dir, out, trace_resolution)
    except _USER_ERRORS as exc:
        _fail(exc)
        return
    if result.is_synthetic:
        typer.secho(SYNTHETIC_BANNER, fg=typer.colors.YELLOW, bold=True)
    typer.echo(markdown_table(summary_display_table(result.summary)))
    typer.echo("\nWrote:")
    for path in result.outputs:
        typer.echo(f"  {path}")


@app.command()
def compare() -> None:
    """Compare DLSS modes, inference load, or baseline vs. fix (Phases 3 to 5)."""
    try:
        from gfl.compare import compare as do_compare

        do_compare()
    except _USER_ERRORS as exc:
        _fail(exc)


@app.command()
def report() -> None:
    """Generate the Markdown study report (Phase 6)."""
    try:
        from gfl.report import build_report

        build_report()
    except _USER_ERRORS as exc:
        _fail(exc)


@app.command("hypothesis-check")
def hypothesis_check(
    run_dirs: list[Path] = typer.Argument(..., help="Capture directories with real data"),
) -> None:
    """Test the study hypotheses against real measurements (refuses synthetic data)."""
    try:
        from gfl.hypothesis_check import check_hypotheses

        check_hypotheses(run_dirs)
    except _USER_ERRORS as exc:
        _fail(exc)


if __name__ == "__main__":
    app()
