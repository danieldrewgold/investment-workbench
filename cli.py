#!/usr/bin/env python3
"""
Investment Research CLI

Usage:
  python cli.py research <TICKER>                  # full research (default: excel + word)
  python cli.py research <TICKER> --verbose         # step-by-step trace
  python cli.py research <TICKER> --detail <type>   # expand a workpaper
  python cli.py research <TICKER> --format word     # word doc only
  python cli.py research <TICKER> --format excel    # excel only
  python cli.py research <TICKER> --format both     # both (default)
  python cli.py research <TICKER> --deep            # deep per-section recursion (gated in v1)
  python cli.py scan <TICKER>,<TICKER>,...          # batch scan
  python cli.py scan --all                          # scan all test tickers
"""

import sys


def _parse_format(argv) -> str:
    """Return one of 'word', 'excel', 'both'. Default 'both'."""
    if "--format" in argv:
        idx = argv.index("--format")
        if idx + 1 < len(argv):
            val = argv[idx + 1].lower().strip()
            if val in ("word", "excel", "both"):
                return val
    # Back-compat: legacy --excel/--xlsx/--word flags still work
    if "--excel" in argv or "--xlsx" in argv:
        return "excel"
    if "--word" in argv or "--docx" in argv:
        return "word"
    return "both"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1].lower()

    if command == "research":
        from research.pipeline import run_research, print_concise, print_detail
        ticker = sys.argv[2].upper() if len(sys.argv) > 2 else ""
        if not ticker:
            print("  Usage: python cli.py research <TICKER>")
            print("  Any ticker: AAPL, CMG, NVDA, MSFT, ...")
            sys.exit(0)

        verbose = "--verbose" in sys.argv or "-v" in sys.argv
        detail_type = None
        if "--detail" in sys.argv:
            idx = sys.argv.index("--detail")
            if idx + 1 < len(sys.argv):
                detail_type = sys.argv[idx + 1]

        fmt = _parse_format(sys.argv)
        deep = "--deep" in sys.argv

        try:
            result = run_research(ticker, verbose=verbose)
            if not verbose:
                print_concise(result)
            if detail_type:
                print_detail(result, detail_type)

            brief = result.get("_brief")

            # Excel export
            if fmt in ("excel", "both"):
                try:
                    from research.export import export_excel
                    path = export_excel(result)
                    print(f"  Excel: {path}")
                except Exception as e:
                    print(f"  Excel export skipped: {e}")

            # Word export
            if fmt in ("word", "both"):
                try:
                    from research.word_report import render_word_report
                    path = render_word_report(result, brief=brief, deep=deep)
                    print(f"  Word:  {path}")
                except Exception as e:
                    print(f"  Word export skipped: {e}")

        except ValueError as e:
            print(f"  Error: {e}")
            sys.exit(1)
        sys.exit(0)

    if command == "scan":
        from research.pipeline import run_research
        tickers_arg = sys.argv[2] if len(sys.argv) > 2 else ""

        if tickers_arg == "--all":
            tickers = ["CMG", "AAPL", "MSFT", "NVDA", "TXRH", "DPZ", "NOW", "VRSK", "WING"]
        elif tickers_arg:
            tickers = [t.strip().upper() for t in tickers_arg.split(",")]
        else:
            print("  Usage: python cli.py scan CMG,AAPL,NVDA")
            sys.exit(0)

        print(f"\n  Scanning {len(tickers)} tickers...")
        print(f"  {'Ticker':<8} {'EPS':>8} {'vs Cons':>10} {'Edge':<20} {'Score':>7} {'Decision':<20} {'WPs':>4}")
        print(f"  {'-'*75}")

        for ticker in tickers:
            try:
                r = run_research(ticker)
                ea = r.get("edge_assessment", {}) or {}
                cons = r.get("consensus_eps")
                vs_cons = f"${r['post_eps'] - cons:+.2f}" if cons else "N/A"
                edge_v = ea.get("verdict", "?")[:19]
                score = ea.get("actionability_score", 0)
                dec = r.get("decision", {}).get("verdict", "?")[:19]
                wps = r.get("total_workpapers", 0)
                print(f"  {ticker:<8} ${r['post_eps']:>7.2f} {vs_cons:>10} {edge_v:<20} {score:>6.3f} {dec:<20} {wps:>4}")
            except Exception as e:
                print(f"  {ticker:<8} ERROR: {str(e)[:50]}")

        print()
        sys.exit(0)

    print(f"Unknown command: {command}")
    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    main()
