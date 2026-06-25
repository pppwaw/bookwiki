from __future__ import annotations

from _common import book_arg_parser, parse_chapters, parse_list_arg

from bookwiki.scheduler.config import load_config
from bookwiki.scheduler.lg_runner import run_pipeline
from bookwiki.scheduler.resume import NODE_ORDER


def build_parser():
    parser = book_arg_parser("Run the BookWiki pipeline.")
    parser.add_argument("--resume", action="store_true", help="Resume from the latest checkpoint")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print graph and cost estimate without writes"
    )
    parser.add_argument("--pause-after", help="Comma-separated node names to pause after")
    parser.add_argument(
        "--from",
        dest="from_node",
        choices=NODE_ORDER,
        help="Node to rerun from; requires --force",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Clear checkpoint/cache and rerun from --from",
    )
    parser.add_argument("--to", dest="to_node", choices=NODE_ORDER, help="Stop after this node")
    parser.add_argument("--only", help="Accepted for M1 CLI compatibility; ignored")
    parser.add_argument(
        "--chapter",
        action="append",
        help=(
            "When used with --from generate --force, only rerun generate for this chapter id. "
            "Repeat or comma-separate for multiple."
        ),
    )
    parser.add_argument(
        "--concept",
        action="append",
        help=(
            "When used with --from concept_pages --force, only rerun this concept page. "
            "Repeat or comma-separate for multiple."
        ),
    )
    return parser


def parse_pause_after(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def resolve_force_from(args, parser) -> str | None:
    if args.from_node and not args.force:
        parser.error("--from requires --force")
    if args.force and not args.from_node:
        parser.error("--force requires --from")
    return args.from_node if args.force else None


def resolve_target_chapters(args, parser) -> list[str]:
    chapters = parse_chapters(args.chapter)
    if chapters and not (args.force and args.from_node == "generate"):
        parser.error("--chapter requires --from generate --force")
    return chapters


def resolve_target_concepts(args, parser) -> list[str]:
    concepts = parse_list_arg(args.concept)
    if concepts and not (args.force and args.from_node == "concept_pages"):
        parser.error("--concept requires --from concept_pages --force")
    return concepts


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    cfg = load_config(args.book_dir)
    cfg.force_from = resolve_force_from(args, parser)
    cfg.target_chapters = resolve_target_chapters(args, parser)
    cfg.target_concepts = resolve_target_concepts(args, parser)
    state = run_pipeline(
        cfg,
        stop_after=args.to_node,
        pause_after=parse_pause_after(args.pause_after),
        dry_run=args.dry_run,
        resume=args.resume,
    )
    if state.get("dry_run"):
        print(state["report"])
    else:
        print(f"run status: {state.get('book_id', cfg.book_id)}")


if __name__ == "__main__":
    main()
