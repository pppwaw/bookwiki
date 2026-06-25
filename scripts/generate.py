from __future__ import annotations

from _common import book_arg_parser, parse_chapters, run_stage


def main() -> None:
    parser = book_arg_parser("Run BookWiki M4 content generation agents.")
    parser.add_argument(
        "--chapter",
        action="append",
        help="Only rerun generate for this chapter id. Repeat or comma-separate for multiple.",
    )
    args = parser.parse_args()
    run_stage(args.book_dir, stop_after="generate", target_chapters=parse_chapters(args.chapter))


if __name__ == "__main__":
    main()
