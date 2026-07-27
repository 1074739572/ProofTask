#!/usr/bin/env python3
"""Run the improved harness agent from the package root."""

# Apply SSL patch early, before any huggingface_hub imports.
# Required on some Windows machines where huggingface.co's CA is missing.
import harness._ssl_patch  # noqa: F401

import argparse
import sys


def _ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not reconfigure:
            continue
        try:
            if stream.encoding and stream.encoding.lower() not in ("utf-8", "utf8"):
                reconfigure(encoding="utf-8")
        except Exception:
            pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="improved_harness agent CLI")
    parser.add_argument(
        "--banner-demo",
        action="store_true",
        help="Preview all welcome banner styles (classic, emoji, typewriter, shadow3d)",
    )
    ui = parser.add_mutually_exclusive_group()
    ui.add_argument(
        "--classic",
        action="store_true",
        help="Use classic Rich line CLI (also: HARNESS_TUI=0)",
    )
    ui.add_argument(
        "--tui",
        action="store_true",
        help="Force Textual TUI (default when textual is installed)",
    )
    ui.add_argument(
        "--node-cli",
        action="store_true",
        help="Run the Node.js terminal client",
    )
    subparsers = parser.add_subparsers(dest="command")

    rag_parser = subparsers.add_parser(
        "rag",
        help="Manage local RAG corpus (index reference docs without starting the agent)",
    )
    rag_sub = rag_parser.add_subparsers(dest="rag_command", required=True)

    rag_status = rag_sub.add_parser("status", help="Show RAG index status")
    rag_status.set_defaults(rag_handler="status")

    rag_index = rag_sub.add_parser("index", help="Build or refresh the RAG index")
    rag_index.add_argument(
        "path",
        nargs="?",
        default="files",
        help="Corpus file or directory (default: files/)",
    )
    rag_index.set_defaults(rag_handler="index")

    rag_add = rag_sub.add_parser(
        "add",
        help="Copy an external document into files/ and re-index",
    )
    rag_add.add_argument("file", help="Path to .md, .txt, .docx, or .pdf")
    rag_add.set_defaults(rag_handler="add")

    rag_docs = rag_sub.add_parser("docs", help="List indexed documents")
    rag_docs.set_defaults(rag_handler="docs")

    rag_select = rag_sub.add_parser("select", help="Select documents for Q&A")
    rag_select.add_argument(
        "spec",
        nargs="?",
        default="",
        help="Numbers (1,3), all, or clear",
    )
    rag_select.set_defaults(rag_handler="select")

    rag_ask = rag_sub.add_parser("ask", help="Ask a question over selected documents")
    rag_ask.add_argument("question", help="Your question")
    rag_ask.set_defaults(rag_handler="ask")

    rag_eval = rag_sub.add_parser("eval", help="Run deterministic offline RAG evaluation")
    rag_eval.add_argument(
        "--corpus",
        default=None,
        help="Corpus path (default: evals/rag/fixtures/tiny_corpus)",
    )
    rag_eval.add_argument(
        "--gold",
        default=None,
        help="Gold YAML path (default: evals/rag/gold_queries.yaml)",
    )
    rag_eval.add_argument(
        "--embedding",
        default="hash",
        choices=("hash", "bge-m3", "openai", "auto"),
        help="Evaluation embedding backend (default: hash, deterministic/offline)",
    )
    rag_eval.add_argument("--output", default=None, help="Optional JSON report path")
    rag_eval.set_defaults(rag_handler="eval")

    return parser.parse_args()


def _run_rag_command(args: argparse.Namespace) -> int:
    from harness.rag.commands import (
        run_rag_add,
        run_rag_ask_command,
        run_rag_index_command,
        run_rag_select_command,
        run_rag_status,
    )
    from harness.rag.sources import format_docs_list

    handler = args.rag_handler
    if handler == "status":
        print(run_rag_status())
        return 0
    if handler == "index":
        print(run_rag_index_command(args.path))
        return 0
    if handler == "add":
        print(run_rag_add(args.file))
        return 0
    if handler == "docs":
        print(format_docs_list())
        return 0
    if handler == "select":
        print(run_rag_select_command(getattr(args, "spec", "")))
        return 0
    if handler == "ask":
        print(run_rag_ask_command(args.question))
        return 0
    if handler == "eval":
        import os
        from pathlib import Path

        from harness.rag.eval import format_eval_report, run_eval

        os.environ["HARNESS_RAG_EMBEDDING"] = args.embedding
        gold = Path(args.gold) if args.gold else None
        report = run_eval(args.corpus, gold, output_path=args.output)
        print(format_eval_report(report))
        return 0 if report["passed"] else 1
    print(f"Unknown rag handler: {handler}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    _ensure_utf8_stdio()
    args = _parse_args()

    if args.banner_demo:
        from harness.ui.banner import run_banner_demo

        run_banner_demo()
        raise SystemExit(0)

    if args.command == "rag":
        raise SystemExit(_run_rag_command(args))

    if args.node_cli:
        import os
        import subprocess
        from pathlib import Path

        node_cli = Path(__file__).resolve().parent / "node_cli"
        if not (node_cli / "package.json").exists():
            print("Node CLI is not installed: node_cli/package.json", file=sys.stderr)
            raise SystemExit(2)
        command = "npm.cmd" if os.name == "nt" else "npm"
        raise SystemExit(subprocess.call([command, "run", "cli"], cwd=node_cli))

    from harness.cli import run_cli
    from harness.ui.tui import prefer_tui, run_tui

    use_classic = bool(args.classic) or not prefer_tui()
    if args.tui:
        use_classic = False

    if use_classic:
        run_cli()
        raise SystemExit(0)

    try:
        run_tui()
    except ImportError as exc:
        print(str(exc), file=sys.stderr)
        print("Falling back to classic CLI…", file=sys.stderr)
        run_cli()
