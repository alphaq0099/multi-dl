"""Console output: logging setup, progress, and the end-of-run summary."""
from __future__ import annotations

import logging
import sys

from rich.console import Console
from rich.table import Table

from .models import PageResult

console = Console()
quiet_console = Console(quiet=True)


def setup_logging(log_path, verbose: bool = False) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("anypic")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(fh)

    if verbose:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.DEBUG)
        sh.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(sh)


def print_summary(results: list[PageResult]) -> None:
    table = Table(title="Summary", show_lines=False)
    table.add_column("URL", overflow="fold", max_width=48)
    table.add_column("Tier", justify="center")
    table.add_column("Found", justify="right")
    table.add_column("Saved", justify="right", style="green")
    table.add_column("Skipped", justify="right", style="yellow")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("Best resolution", justify="right")

    for pr in results:
        best = max(
            ((r.width, r.height) for r in pr.results if r.width),
            default=None,
        )
        table.add_row(
            pr.url,
            pr.tier,
            str(pr.found),
            str(len(pr.downloaded)),
            str(len(pr.skipped)),
            str(len(pr.failed)),
            f"{best[0]}x{best[1]}" if best else "-",
        )
    console.print(table)

    total_saved = sum(len(p.downloaded) for p in results)
    total_skipped = sum(len(p.skipped) for p in results)
    console.print(
        f"[green]{total_saved} saved[/] · [yellow]{total_skipped} skipped (already had)[/] "
        f"· {sum(len(p.failed) for p in results)} failed · log: data/anypic.log"
    )
