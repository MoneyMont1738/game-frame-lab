"""Command line interface: gfl run | analyze | validate | compare | report | hypothesis-check."""

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
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Print every step without running anything (any OS)."),
    tag: str = typer.Option(None, "--tag", help="Override the config's tag, e.g. fix"),
    settings_path: Path = typer.Option(None, "--settings",
                                       help="Local settings file (default: gfl.local.yaml)"),
) -> None:
    """Capture every config in an experiment (Windows PC, or a Mac for gflbench)."""
    from gfl.capture import (DryRunRunner, SubprocessRunner, capture_dir_for, mac_power_warnings,
                             require_capture_platform, run_experiment, with_tag)
    from gfl.settings import load_settings

    try:
        experiment = with_tag(load_experiment(config), tag)
        settings = load_settings(settings_path)
        runs = experiment.expand()
        cap = experiment.capture
        load_s = 0 if experiment.scene.launcher == "gflbench" else settings.load_wait_s
        per_run_s = load_s + cap.duration_s + cap.cooldown_s
        if experiment.variants:
            names = ", ".join(name for name, _ in experiment.variants)
            label = f"variants {names}, captured alternately"
        else:
            label = f"tag {experiment.tag}"
        typer.echo(f"Experiment: {experiment.name} ({config}), {label}")
        typer.echo(f"{len(runs)} configs x {cap.runs} runs x {cap.duration_s:g} s "
                   f"(first {cap.warmup_s:g} s dropped). "
                   f"About {len(runs) * cap.runs * per_run_s / 60:.0f} min including load and cooldown.")
        out_dir = capture_dir_for(experiment, settings)
        if dry_run:
            typer.echo(f"Dry run: nothing is executed. Output would go to {out_dir}\n")
            run_experiment(experiment, DryRunRunner(typer.echo), settings, out_dir, write_files=False)
            return
        require_capture_platform(experiment)
        if experiment.scene.launcher == "gflbench":
            problems = mac_power_warnings()
            for problem in problems:
                typer.secho(f"warning: {problem}", fg=typer.colors.YELLOW, err=True)
            if problems and not typer.confirm("Results taken like this are not comparable with "
                                              "plugged-in runs. Continue anyway?", default=False):
                raise typer.Exit(code=1)
        runner = SubprocessRunner(typer.echo)
        run_experiment(experiment, runner, settings, out_dir)
        typer.echo(f"\nCaptured. Next: gfl analyze {out_dir}")
    except _USER_ERRORS as exc:
        _fail(exc)


@app.command()
def validate(
    run_dir: Path = typer.Argument(..., help="Capture directory containing manifest.csv"),
    frameview_dir: Path = typer.Option(None, "--frameview-dir",
                                       help="Folder with FrameView logs to attach to the runs first"),
    out: Path = typer.Option(None, "--out", help="Output directory (default: results/<experiment>)"),
) -> None:
    """Check how closely PresentMon and FrameView agree on the same runs."""
    from gfl.analyze import default_out_dir
    from gfl.manifest import read_manifest
    from gfl.validate import attach_frameview, validate_directory, validation_markdown

    try:
        if frameview_dir is not None:
            for note in attach_frameview(run_dir, frameview_dir):
                typer.echo(note)
        table, data_kind = validate_directory(run_dir)
        synthetic = data_kind == "synthetic"
        out = out or default_out_dir(read_manifest(run_dir))
        out.mkdir(parents=True, exist_ok=True)
        prefix = "SYNTHETIC_" if synthetic else ""
        table.to_csv(out / f"{prefix}validation.csv", index=False, float_format="%.5f")
        md = validation_markdown(table, synthetic, SYNTHETIC_BANNER)
        (out / f"{prefix}validation.md").write_text(md, encoding="utf-8")
    except _USER_ERRORS as exc:
        _fail(exc)
        return
    typer.echo(md)
    typer.echo(f"Wrote {out / f'{prefix}validation.csv'} and {out / f'{prefix}validation.md'}")
    if not table["agree"].all():
        raise typer.Exit(code=2)


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
def compare(
    run_dir: Path = typer.Argument(..., help="Capture directory with runs of both variants"),
    baseline: str = typer.Option("baseline", "--baseline", help="Tag of the before runs"),
    fix: str = typer.Option("fix", "--fix", help="Tag of the after runs"),
    out: Path = typer.Option(None, "--out", help="Output directory (default: results/<experiment>)"),
) -> None:
    """Before/after: change in frame time (ms) between two tagged variants, with a 95% interval."""
    from gfl.compare import compare_tags, comparison_markdown

    try:
        result = analyze(run_dir, out)
        table = compare_tags(result.per_run, baseline, fix)
        prefix = "SYNTHETIC_" if result.is_synthetic else ""
        table.to_csv(result.out_dir / f"{prefix}comparison.csv", index=False, float_format="%.4f")
        md = comparison_markdown(table, baseline, fix)
        if result.is_synthetic:
            md = f"> **{SYNTHETIC_BANNER}**\n\n" + md
        (result.out_dir / f"{prefix}comparison.md").write_text(md, encoding="utf-8")
    except _USER_ERRORS as exc:
        _fail(exc)
        return
    typer.echo(md)
    typer.echo(f"Wrote {result.out_dir / f'{prefix}comparison.md'}")


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
