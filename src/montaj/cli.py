"""Command-line entry point for Montaj."""

import typer

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Prepare reviewed short clips from permitted videos."""
