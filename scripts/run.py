from __future__ import annotations

from _common import book_arg_parser

from bookwiki.scheduler.config import load_config
from bookwiki.scheduler.graph import NODE_ORDER, build_graph, resume_or_start


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


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    cfg = load_config(args.book_dir)
    cfg.force_from = resolve_force_from(args, parser)
    graph = build_graph(
        cfg,
        stop_after=args.to_node,
        pause_after=parse_pause_after(args.pause_after),
        dry_run=args.dry_run,
    )
    state = resume_or_start(graph, cfg.book_id, resume=args.resume)
    if not args.dry_run:
        print(f"run status: {state.get('book_id', cfg.book_id)}")


if __name__ == "__main__":
    main()
