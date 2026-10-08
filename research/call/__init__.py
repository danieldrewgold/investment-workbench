"""
Call layer: turns the research into an investment view.

Sits on top of the existing pipeline (DAG fetches, brief, audit, EPS bridge)
and adds what a view needs and the old layers never had:

  price.py            live price with its date; fails loudly if missing or stale
  guidance_ledger.py  every guided item and its reported outcome, persisted per
                      ticker; historical bias; bias-adjusted current guidance
  mgmt_ledger.py      management statements classified (a)-(e) plus behavior
                      signals (dodged questions, dropped metrics, tone)
  valuation_pack.py   what the current price implies (own P/E history, peers)
  bridges.py          margin bridges computed in code, so they always foot
  decide.py           the call: stance, scenarios, expected value, catalysts,
                      kill criteria; validated, repaired once, else the run fails
  render.py           full digest (with provenance tags) and one-page pitch
"""
