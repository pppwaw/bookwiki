from __future__ import annotations

from _common import book_arg_parser, run_stage


def main() -> None:
    parser = book_arg_parser("Run BookWiki split stage.")
    args = parser.parse_args()
    run_stage(args.book_dir, stop_after="split")


if __name__ == "__main__":
    main()
