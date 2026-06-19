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

  python cli.py dag <TICKER>                       # run fetch+analysis DAG only
  python cli.py dag <TICKER> --force               # bypass cache, re-fetch all
  python cli.py dag <TICKER> --verbose             # trace per-step timing

  python cli.py scan <TICKER>,<TICKER>,...          # batch scan
  python cli.py scan --all                          # scan all test tickers

  python cli.py refresh-13f                         # ingest 4 quarters of 13F filings
  python cli.py refresh-13f --quarters 8            # ingest 8 quarters instead
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

        force_thin_brief = "--force" in sys.argv
        try:
            result = run_research(ticker, verbose=verbose,
                                   force_thin_brief=force_thin_brief)
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

    if command == "dag":
        # Fetch + analysis layer only — parallel, cached, observable.
        from research.pipeline import run_research_dag
        ticker = sys.argv[2].upper() if len(sys.argv) > 2 else ""
        if not ticker:
            print("  Usage: python cli.py dag <TICKER> [--force] [--verbose]")
            sys.exit(0)
        verbose = "--verbose" in sys.argv or "-v" in sys.argv
        force = "--force" in sys.argv
        try:
            # --force bypasses cache READ but still WRITES fresh results
            # so the next run can hit cache
            results, trace = run_research_dag(
                ticker, verbose=verbose,
                read_cache=not force, write_cache=True,
            )
        except ValueError as e:
            print(f"  Error: {e}")
            sys.exit(1)
        # Summary table
        print()
        print(f"  === {ticker} DAG complete — {trace.total_duration_seconds:.1f}s wall-clock ===")
        print(f"  {'Step':<22} {'Status':<8} {'Duration':>10} {'Output preview':<60}")
        print(f"  {'-'*108}")
        for s in trace.steps:
            preview = (s.output_preview or "").replace("\n", " ")[:58]
            dur = f"{s.duration_seconds:.1f}s" if s.status != "cached" else "cached"
            print(f"  {s.name:<22} {s.status:<8} {dur:>10} {preview:<60}")
        print()
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

    if command in ("refresh-13f", "refresh_13f"):
        # Periodic 13F ingestion — runs offline (manual or cron). Pulls
        # quarterly 13F-HR filings for every fund in data/fund_universe.json
        # and persists holdings to data/workbench.db. Per-ticker pipeline
        # runs read from this DB synchronously via the crowding_assessment
        # DAG step. Run after each 13F filing deadline (Feb/May/Aug/Nov 14).
        import asyncio
        from pathlib import Path
        from core.provenance.database import init_db, RunContext
        from ingestion.loaders.edgar_13f_loader import Edgar13FLoader

        quarters = 4
        if "--quarters" in sys.argv:
            try:
                quarters = int(sys.argv[sys.argv.index("--quarters") + 1])
            except (IndexError, ValueError):
                print("  Usage: python cli.py refresh-13f [--quarters N]")
                sys.exit(1)

        db_path = Path("data/workbench.db")
        print(f"  Initializing persistent DB at {db_path}...")
        conn = init_db(db_path)

        async def _run():
            with RunContext(conn, "refresh_13f", {"quarters": quarters}) as ctx:
                loader = Edgar13FLoader(conn, ctx.run_id)
                # Auto-seeds fund_universe if empty (reads data/fund_universe.json)
                seeded = loader.seed_fund_universe()
                print(f"  Fund universe: {seeded} funds active")
                conn.commit()
                print(f"  Pulling {quarters} quarter(s) of 13F-HR filings (this can take 5-15 min)...")
                try:
                    summary = await loader.ingest_all_funds(quarters_back=quarters)
                finally:
                    await loader.close()
                print(f"\n  === 13F refresh complete ===")
                print(f"  Funds processed: {summary['funds_processed']}")
                print(f"  Filings ingested: {summary['filings_ingested']}")
                if summary["errors"]:
                    print(f"  Errors ({len(summary['errors'])}):")
                    for err in summary["errors"][:10]:
                        print(f"    {err}")
                return summary

        try:
            asyncio.run(_run())
        finally:
            conn.close()
        sys.exit(0)

    print(f"Unknown command: {command}")
    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    main()
