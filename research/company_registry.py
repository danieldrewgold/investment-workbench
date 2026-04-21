"""
Company Data Registry

Compact, ticker-keyed data for running the research pipeline.
Each entry has: company info, earnings text for extraction,
model prior-year data, constants, and driver setup.

This is NOT a database. It's a bootstrap file that lets the CLI
run `python3 cli.py research CMG` without any external data source.
In production, this data would come from EDGAR fetch + extraction.
"""


COMPANY_REGISTRY = {
    "CMG": {
        "name": "Chipotle Mexican Grill, Inc.",
        "cik": "0001058090",
        "sic_code": "5812",

        # Compact earnings text for extraction
        "earnings_text": (
            "Chipotle Mexican Grill FY2024 Results. "
            "Total revenue increased 14.3% to $11.3 billion from company-operated restaurants. "
            "All restaurants are company-owned; no franchise. "
            "Comparable restaurant sales increased 6.5%. "
            "Restaurant-level operating margin was 28.4%. "
            "Food, beverage and packaging: 28.8% of revenue. "
            "Labor costs: 24.7% of revenue. Occupancy: 5.0% of revenue. "
            "Other operating costs: 13.1% of revenue. "
            "General and administrative: 6.2% of revenue. "
            "Diluted EPS was $1.15. Diluted shares: 1,370 million. "
            "Opened 304 new restaurants in FY2024. Total restaurants: 3,726. "
            "Digital sales represented 34% of food revenue. "
            "FY2025 guidance: low-to-mid single digit comparable restaurant sales growth. "
            "315 to 345 new restaurant openings. "
            "Stock repurchases of $1.5 billion in FY2024."
        ),

        # Model data (for schema-driven ModelSpec)
        "prior_year": {
            "revenue_m": 11311.5,
            "store_count": 3726,
            "food_pct": 29.8, "labor_pct": 24.7,
            "occupancy_pct": 5.0, "other_operating_pct": 13.9,
            "cash_ga_m": 566.0, "stock_comp_m": 131.7,
            "da_m": 335.0, "preopen_m": 41.9,
            "prior_new_restaurants": 304,
        },
        "constants": {
            "tax_rate": 0.237, "shares_m": 1340, "net_interest_m": 94,
        },

        # Driver assumptions (pre-challenge)
        "drivers": {
            "sss_growth": {
                "assumption_key": "sss_growth_pct",
                "formula": "traffic + ticket",
                "components": {
                    "traffic": {"value": 2.0, "confidence": 0.45,
                                "basis": "Throughput improvements sustaining transaction growth"},
                    "ticket": {"value": 2.5, "confidence": 0.65,
                               "basis": "Menu pricing 2.0% + mix shift 0.5%"},
                },
            },
            "food_cost": {
                "assumption_key": "food_cost_delta_bps",
                "formula": "commodity_pressure + pricing_offset",
                "unit": "bps",
                "components": {
                    "commodity": {"value": 30, "unit": "bps", "confidence": 0.40,
                                  "basis": "Beef/chicken inflation + tariff risk"},
                    "pricing_offset": {"value": -50, "unit": "bps", "confidence": 0.60,
                                       "basis": "Menu pricing absorbs commodity pressure"},
                },
            },
            "labor_cost": {
                "assumption_key": "labor_cost_delta_bps",
                "formula": "wage_pressure + throughput_offset",
                "unit": "bps",
                "components": {
                    "wage_pressure": {"value": 50, "unit": "bps", "confidence": 0.55,
                                      "basis": "Minimum wage increases"},
                    "throughput_offset": {"value": -80, "unit": "bps", "confidence": 0.45,
                                          "basis": "Throughput improvements"},
                },
            },
            "new_stores": {
                "assumption_key": "new_restaurants",
                "formula": "guidance_midpoint",
                "unit": "count",
                "components": {
                    "guidance_midpoint": {"value": 330, "unit": "count", "confidence": 0.80,
                                          "basis": "Guidance 315-345, using midpoint"},
                },
            },
        },
        "extra_assumptions": {
            "new_store_productivity": 0.75,
            "other_cost_delta_bps": 0,
            "cash_ga_growth_pct": 5.0,
            "stock_comp_growth_pct": -5.0,
        },

        # Adversarial bear revisions (component-level)
        "bear_revisions": [
            {"driver": "sss_growth", "component": "traffic", "new_value": 1.0,
             "reason": "Consumer spending weakening, SSS decelerating"},
            {"driver": "food_cost", "component": "commodity", "new_value": 50,
             "reason": "Tariff risk + beef inflation worse than expected"},
        ],
        "confidence_revisions": [
            {"assumption_key": "labor_cost_delta_bps", "new_confidence": 0.35,
             "reason": "Throughput offset uncertain"},
        ],

        # Actual FY2025 for comparison
        "actuals": {"eps": 1.14, "revenue_m": 11926, "sss_pct": -1.7},
        "consensus": {"eps": 1.25, "revenue_m": 12200},
    },

    "WING": {
        "name": "Wingstop Inc.",
        "cik": "0001636222",
        "sic_code": "5812",

        "earnings_text": (
            "Wingstop FY2024 Results. "
            "System-wide sales increased 36.8% to $4.8 billion. "
            "Total revenue increased 36.0% to $625.8 million. "
            "Royalty revenue, franchise fees and other: $233 million. "
            "Advertising fees: $224 million. "
            "Company-owned restaurant sales: $119 million. "
            "Number of domestic franchise restaurants: 2,154. "
            "Number of international franchise restaurants: 359. "
            "50 company-owned restaurants. "
            "2,563 system-wide restaurants. "
            "Domestic same-store sales increased 19.9%. "
            "Net income $108.7 million, $3.70 per diluted share. "
            "SG&A increased to $116.8 million. "
            "349 net new restaurant openings in FY2024. "
            "FY2025 guidance: low-to-mid single digit domestic same-store sales growth. "
            "Global unit growth rate of 14% to 15%. "
            "SG&A of approximately $140 million. "
            "Stock-based compensation of approximately $26 million. "
            "Interest expense net of approximately $46 million. "
            "D&A of between $29-30 million."
        ),

        "prior_year": {
            "revenue_m": 625.8,
            "system_wide_sales_m": 4765,
            "store_count": 2563,
            "company_owned_stores": 50,
            "company_owned_auv_m": 2.38,
            "cos_pct": 14.6,
            "ad_exp_pct": 35.8,
            "sga_pct": 18.7,
            "stock_comp_m": 26.0,
            "da_m": 19.5,
        },
        "constants": {
            "tax_rate": 0.22, "shares_m": 28.5, "net_interest_m": -46,
        },

        "drivers": {
            "sss_growth": {
                "assumption_key": "sss_growth_pct",
                "formula": "traffic + ticket",
                "components": {
                    "traffic": {"value": 2.0, "confidence": 0.40,
                                "basis": "Digital ordering + brand momentum"},
                    "ticket": {"value": 3.0, "confidence": 0.55,
                               "basis": "Menu pricing + chicken sandwich mix"},
                },
            },
            "new_stores": {
                "assumption_key": "new_restaurants",
                "formula": "guidance_midpoint",
                "unit": "count",
                "components": {
                    "guidance_midpoint": {"value": 370, "unit": "count", "confidence": 0.75,
                                          "basis": "Guidance 14-15% growth from 2,563"},
                },
            },
        },
        "extra_assumptions": {
            "royalty_rate_pct": 5.9,
            "ad_fund_rate_pct": 5.3,
            "company_owned_stores": 50,
            "company_sss_pct": 3.0,
            "cos_delta_bps": 0,
            "sga_delta_bps": 0,
            "stock_comp_growth_pct": 0.0,
            "da_growth_pct": 50.0,
        },

        "bear_revisions": [
            {"driver": "sss_growth", "component": "traffic", "new_value": 0.0,
             "reason": "Consumer spending weakening materially"},
        ],
        "confidence_revisions": [],

        "actuals": {"eps": 4.08, "revenue_m": 696.9, "sss_pct": -3.3},  # FY2025 non-GAAP
        "consensus": {"eps": 4.44, "revenue_m": 742},
    },

    "VRSK": {
        "name": "Verisk Analytics, Inc.",
        "cik": "0001442145",
        "sic_code": "7372",

        "earnings_text": (
            "Verisk FY2024 Results. "
            "Leading global data analytics and technology provider to the insurance industry. "
            "Subscription-based revenue model with high retention rates. "
            "Consolidated revenues were $2,882 million, up 7.5% and up 7.1% on an organic constant currency basis. "
            "Two segments: Underwriting revenue $2,025 million (up 7.0%), Claims revenue $857 million (up 8.7%). "
            "Adjusted EBITDA was $1,576 million, up 9.9%. Adjusted EBITDA margin was 54.7%. "
            "Income from continuing operations was $951 million. "
            "Diluted adjusted EPS were $6.64, up 16.3%. Diluted GAAP EPS were $6.66. "
            "Net cash from operating activities was $1,144 million. Free cash flow was $920 million. "
            "Fixed asset D&A approximately $260 million. Intangible amortization approximately $65 million. "
            "Interest expense approximately $155 million. "
            "Effective tax rate approximately 23%. "
            "Diluted share count approximately 142 million. "
            "Stock-based compensation approximately $120 million. "
            "FY2025 guidance: Revenue $3,030 to $3,080 million. "
            "Adjusted EBITDA $1,670 to $1,720 million. Adjusted EBITDA margin 55.0% to 55.8%. "
            "Diluted adjusted EPS $6.80 to $7.10. "
            "D&A $250 to $270 million. Interest expense $145 to $165 million. "
            "Effective tax rate 23% to 25%. "
            "Paid $0.39 quarterly dividend. Board approved 15% dividend increase. "
            "Repurchased $300 million of shares via accelerated share repurchase."
        ),

        "prior_year": {
            "revenue_m": 2882,
            "store_count": 1,           # not store-based
            "cogs_pct": 33.0,           # cost of revenues ~33% of revenue
            "sm_pct": 5.0,              # sales & marketing ~5%
            "rd_pct": 5.0,              # R&D embedded in cost of revenues
            "ga_pct": 2.3,              # G&A ~2.3%
            # NOTE: SBC is already embedded in cost bucket percentages above
            # (EBITDA adds back SBC, so cost% from GAAP include it)
            # Set SBC to 0 below-line to avoid double-counting
            "stock_comp_m": 0,
            "da_m": 325.0,              # D&A ($260M fixed + $65M intangible)
        },
        "constants": {
            "tax_rate": 0.24,           # midpoint of 23-25% guidance
            "shares_m": 140,            # declining from buybacks
            "net_interest_m": -155,     # net interest EXPENSE
        },

        "drivers": {
            "revenue_growth": {
                "assumption_key": "net_retention_pct",
                "formula": "base_retention + expansion",
                "components": {
                    "base_retention": {"value": 100, "confidence": 0.80,
                                        "basis": "Subscription model with very high retention. "
                                                 "Insurance data is mission-critical, high switching costs."},
                    "expansion": {"value": 5.5, "confidence": 0.60,
                                   "basis": "Cross-sell, pricing escalators, new products. "
                                            "OCC growth was 7.1% in FY2024, implies ~5-6% expansion on base."},
                },
            },
            "new_arr": {
                "assumption_key": "new_arr_growth_pct",
                "formula": "new_customer_growth",
                "components": {
                    "new_customer_growth": {"value": 2.0, "confidence": 0.55,
                                            "basis": "Modest new customer acquisition. "
                                                     "Most growth comes from existing client expansion."},
                },
            },
        },
        "extra_assumptions": {
            "cogs_delta_bps": -30,      # slight COGS improvement (operating leverage)
            "sm_delta_bps": -20,        # S&M leverage
            "rd_delta_bps": 0,
            "ga_delta_bps": -10,
            "stock_comp_growth_pct": 5.0,
            "da_growth_pct": 3.0,       # modest D&A growth
        },

        "bear_revisions": [
            {"driver": "revenue_growth", "component": "expansion", "new_value": 4.0,
             "reason": "Expansion slows: insurance market softening, clients tighten budgets"},
        ],
        "confidence_revisions": [],

        "actuals": {"eps": 7.16, "revenue_m": 3073},  # FY2025 non-GAAP adjusted EPS
        "consensus": {"eps": 6.95, "revenue_m": 3055},
    },

    # ── DPZ: Domino's Pizza — franchise-heavy with supply chain ──
    "DPZ": {
        "name": "Domino's Pizza, Inc.",
        "cik": "0001286681",
        "sic_code": "5812",

        "earnings_text": (
            "Domino's Pizza FY2024 Results. Largest pizza company in the world. "
            "Primarily franchise-driven model with over 21,000 stores across 90+ markets. "
            "Revenue streams: U.S. franchise royalties and fees, franchise advertising contributions, "
            "supply chain revenues (dough manufacturing), international franchise royalties, "
            "and company-owned store revenues. "
            "Total revenues $4,706 million, up 5.1%. "
            "U.S. same store sales growth of 3.5% for fiscal 2024. "
            "International same store sales growth of 1.1%. "
            "Global net store growth of 411. Total stores approximately 21,000. "
            "U.S. franchise royalties and fees increased due to higher same store sales and net store growth. "
            "Supply chain revenues increased due to higher order volumes and food basket pricing increases. "
            "Income from operations $879.0 million. "
            "Net income $584.2 million. Diluted EPS $16.69. "
            "Diluted shares approximately 35 million. "
            "Supply chain gross margin 11.0%. Company-owned store gross margin 17.3%. "
            "SG&A approximately $445 million. D&A approximately $85 million. "
            "Interest expense approximately $195 million. Tax rate approximately 21%. "
            "FY2025 outlook: 3%+ U.S. same store sales growth, 1,100+ global net stores. "
            "Board approved 15% increase in quarterly dividend to $1.74 per share."
        ),

        "prior_year": {
            "revenue_m": 4706,
            "system_wide_sales_m": 10800,  # US retail sales ~$10.8B
            "store_count": 21000,
            "company_owned_stores": 290,
            "company_owned_auv_m": 1.55,
            "supply_chain_m": 2650,        # supply chain revenue — biggest line
            "intl_royalty_m": 300,          # international royalties
            "cos_pct": 57.5,               # supply chain COGS + company-owned COGS / total rev
            "ad_exp_pct": 14.0,            # franchise advertising (pass-through)
            "sga_pct": 9.5,               # G&A ~$445M / $4,706M
            "stock_comp_m": 0,
            "da_m": 85.0,
        },
        "constants": {
            "tax_rate": 0.21,
            "shares_m": 34.0,             # declining from aggressive buybacks
            "net_interest_m": -140,        # net interest after hedging
        },

        "drivers": {
            "sss_growth": {
                "assumption_key": "sss_growth_pct",
                "formula": "traffic + ticket",
                "components": {
                    "traffic": {"value": 2.0, "confidence": 0.50,
                                "basis": "Order count growth from Uber Eats partnership, loyalty program"},
                    "ticket": {"value": 1.5, "confidence": 0.60,
                               "basis": "Modest pricing + mix shift"},
                },
            },
            "new_stores": {
                "assumption_key": "new_restaurants",
                "formula": "guidance_midpoint",
                "unit": "count",
                "components": {
                    "guidance_midpoint": {"value": 1100, "unit": "count", "confidence": 0.70,
                                          "basis": "Guidance 1,100+ global net new stores"},
                },
            },
        },
        "extra_assumptions": {
            "royalty_rate_pct": 5.5,
            "ad_fund_rate_pct": 6.0,
            "company_owned_stores": 290,
            "company_sss_pct": 3.0,
            "supply_chain_pricing_pct": 1.5,  # food basket pricing growth
            "intl_sss_pct": 1.0,              # international same store sales
            "cos_delta_bps": 0,
            "sga_delta_bps": -20,
            "stock_comp_growth_pct": 0,
            "da_growth_pct": 5.0,
        },

        "bear_revisions": [
            {"driver": "sss_growth", "component": "traffic", "new_value": 1.0,
             "reason": "Consumer spending pressure on QSR, Uber Eats cannibalization fades"},
        ],
        "confidence_revisions": [],

        "actuals": {"eps": 17.57, "revenue_m": 4940},
        "consensus": {"eps": 17.80, "revenue_m": 4950},
    },

    # ── NOW: ServiceNow — pure SaaS ──
    "NOW": {
        "name": "ServiceNow, Inc.",
        "cik": "0001373715",
        "sic_code": "7372",

        "earnings_text": (
            "ServiceNow FY2024 Results. AI platform for business transformation. "
            "Subscription-based revenue model. "
            "Subscription revenues $10,646 million, up 23% year-over-year. "
            "Total revenues $10,984 million, up 22%. "
            "Subscription revenue is 97% of total revenue. "
            "Net retention rate exceeds 100%. "
            "2,109 customers with more than $1 million in ACV. "
            "Current remaining performance obligations $10.27 billion, up 19% YoY. "
            "Non-GAAP operating margin approximately 30%. "
            "Non-GAAP gross margin approximately 83%. "
            "GAAP net income approximately $1,487 million. "
            "Non-GAAP diluted EPS approximately $14.40. "
            "Diluted shares approximately 210 million. "
            "R&D approximately 17% of revenue. Sales and marketing approximately 25% of revenue. "
            "G&A approximately 6% of revenue. "
            "Stock-based compensation approximately $2.5 billion. "
            "D&A approximately $600 million. "
            "FY2025 guidance: subscription revenues $13.0-13.1 billion (20-21% growth). "
            "Non-GAAP operating margin approximately 30.5%."
        ),

        "prior_year": {
            "revenue_m": 10984,
            "store_count": 1,
            "cogs_pct": 17.0,             # Non-GAAP cost of revenue (~83% non-GAAP gross margin)
            "sm_pct": 25.0,               # Non-GAAP S&M (~25% of revenue)
            "rd_pct": 17.0,               # Non-GAAP R&D (~17% of revenue)
            "ga_pct": 6.0,               # Non-GAAP G&A (~6% of revenue)
            "stock_comp_m": 0,            # SBC excluded from non-GAAP
            "da_m": 600.0,               # D&A ~$600M
        },
        "constants": {
            "tax_rate": 0.19,             # Non-GAAP effective tax rate
            "shares_m": 210,              # Non-GAAP diluted shares (~210M)
            "net_interest_m": 150,        # Net interest INCOME (large cash balance)
        },

        "drivers": {
            "revenue_growth": {
                "assumption_key": "net_retention_pct",
                "formula": "base_retention + expansion",
                "components": {
                    "base_retention": {"value": 100, "confidence": 0.85,
                                        "basis": "Mission-critical platform, very high retention"},
                    "expansion": {"value": 18.0, "confidence": 0.60,
                                   "basis": "AI-driven expansion, GenAI net new ACV ramping"},
                },
            },
            "new_arr": {
                "assumption_key": "new_arr_growth_pct",
                "formula": "new_customer_growth",
                "components": {
                    "new_customer_growth": {"value": 3.0, "confidence": 0.55,
                                            "basis": "Strong pipeline but large base"},
                },
            },
        },
        "extra_assumptions": {
            "cogs_delta_bps": 0,
            "sm_delta_bps": 200,       # heavy sales hiring, AI go-to-market
            "rd_delta_bps": 250,       # massive AI R&D investment
            "ga_delta_bps": 100,       # growth infrastructure
            "stock_comp_growth_pct": 0,
            "da_growth_pct": 0,
        },

        "bear_revisions": [
            {"driver": "revenue_growth", "component": "expansion", "new_value": 15.0,
             "reason": "AI spending pauses, enterprise budgets tighten"},
        ],
        "confidence_revisions": [],

        "actuals": {"eps": 6.48, "revenue_m": 13278},  # FY2025 non-GAAP diluted EPS
        "consensus": {"eps": 6.30, "revenue_m": 13200},
    },

    # ── TXRH: Texas Roadhouse — company-operated restaurant ──
    "TXRH": {
        "name": "Texas Roadhouse, Inc.",
        "cik": "0001289460",
        "sic_code": "5812",

        "earnings_text": (
            "Texas Roadhouse FY2024 Results. "
            "Company-operated casual dining restaurant chain. "
            "Total revenue $5,373 million, up 16.0%. "
            "772 total restaurants including approximately 660 company-owned and 112 franchise locations. "
            "Comparable restaurant sales increased 8.5% at company restaurants. "
            "Average weekly sales at company restaurants $155,807. "
            "Restaurant margin 17.1%, up from 15.4% in prior year. "
            "Food cost approximately 32% of restaurant revenue. "
            "Labor cost approximately 32% of restaurant revenue. "
            "Other operating costs approximately 18% of restaurant revenue. "
            "Income from operations $517 million. "
            "Net income $434 million. Diluted EPS $6.47. "
            "Diluted shares approximately 67 million. "
            "G&A approximately $270 million. D&A approximately $255 million. "
            "Opened 31 company restaurants and 14 franchise locations in FY2024. "
            "FY2025 guidance: positive comparable sales growth, 5% store week growth, "
            "commodity cost inflation 3-4%, wage inflation 4-5%. "
            "Board approved 11% increase in quarterly dividend to $0.68 per share."
        ),

        "prior_year": {
            "revenue_m": 5373,
            "store_count": 772,
            "food_pct": 32.0, "labor_pct": 32.0,
            "occupancy_pct": 4.5, "other_operating_pct": 14.5,
            "cash_ga_m": 270.0, "stock_comp_m": 0,
            "da_m": 112.0, "preopen_m": 20.0,  # D&A is corporate only; restaurant-level is in "other"
            "prior_new_restaurants": 31,
        },
        "constants": {
            "tax_rate": 0.22,
            "shares_m": 67,
            "net_interest_m": 5,           # minimal net interest
        },

        "drivers": {
            "sss_growth": {
                "assumption_key": "sss_growth_pct",
                "formula": "traffic + ticket",
                "components": {
                    "traffic": {"value": 2.0, "confidence": 0.50,
                                "basis": "Strong brand loyalty, wait time management improving"},
                    "ticket": {"value": 4.0, "confidence": 0.55,
                               "basis": "Menu pricing 3-4% + modest mix shift"},
                },
            },
            "food_cost": {
                "assumption_key": "food_cost_delta_bps",
                "formula": "commodity_pressure + pricing_offset",
                "unit": "bps",
                "components": {
                    "commodity": {"value": 50, "unit": "bps", "confidence": 0.45,
                                  "basis": "Guidance: commodity inflation 3-4%"},
                    "pricing_offset": {"value": -80, "unit": "bps", "confidence": 0.55,
                                       "basis": "Menu pricing absorbs most commodity pressure"},
                },
            },
            "labor_cost": {
                "assumption_key": "labor_cost_delta_bps",
                "formula": "wage_pressure + productivity_offset",
                "unit": "bps",
                "components": {
                    "wage_pressure": {"value": 60, "unit": "bps", "confidence": 0.50,
                                      "basis": "Guidance: wage inflation 4-5%"},
                    "productivity_offset": {"value": -90, "unit": "bps", "confidence": 0.45,
                                             "basis": "Labor productivity gains from throughput"},
                },
            },
            "new_stores": {
                "assumption_key": "new_restaurants",
                "formula": "guidance_estimate",
                "unit": "count",
                "components": {
                    "guidance_estimate": {"value": 35, "unit": "count", "confidence": 0.75,
                                           "basis": "5% store week growth implies ~35 new company restaurants"},
                },
            },
        },
        "extra_assumptions": {
            "new_store_productivity": 0.80,
            "other_cost_delta_bps": 0,
            "cash_ga_growth_pct": 8.0,
            "stock_comp_growth_pct": 0,
        },

        "bear_revisions": [
            {"driver": "sss_growth", "component": "traffic", "new_value": 0.5,
             "reason": "Consumer pullback on casual dining, competition from fast-casual"},
            {"driver": "labor_cost", "component": "wage_pressure", "new_value": 80,
             "reason": "Wage inflation runs hotter than guidance"},
        ],
        "confidence_revisions": [],

        "actuals": {"eps": 7.60, "revenue_m": 5878},  # FY2025 non-GAAP
        "consensus": {"eps": 7.50, "revenue_m": 5850},
    },

    # ── AAOI: Applied Optoelectronics — hardware manufacturing ──
    "AAOI": {
        "name": "Applied Optoelectronics, Inc.",
        "cik": "0001158114",
        "sic_code": "3674",

        "earnings_text": (
            "Applied Optoelectronics FY2024 Results. "
            "Fiber-optic access network products for datacenter, cable broadband, telecom and FTTH markets. "
            "Hardware manufacturing company. Product sales revenue, not subscription-based. "
            "GAAP revenue was $249.4 million, up 15% from $217.6 million in 2023. "
            "GAAP gross margin was 24.8%. Cost of goods sold $187.5 million, 75% of revenue. "
            "Non-GAAP net loss was $32.7 million, or $0.79 per basic share. "
            "Q4 2024 revenue $100.3 million, up 65.8% year-over-year. "
            "FY2025 Q1 guidance: revenue $94-104 million, non-GAAP gross margin 29-30.5%."
        ),

        "prior_year": {
            "revenue_m": 249.4,
            "store_count": 1,
            "cogs_pct": 75.0,
            "opex_pct": 18.0,             # R&D + SG&A
            "stock_comp_m": 10.0,
            "da_m": 25.0,
        },
        "constants": {
            "tax_rate": 0.0,
            "shares_m": 43,
            "net_interest_m": -15,
        },

        "drivers": {
            "revenue_growth": {
                "assumption_key": "revenue_growth_pct",
                "formula": "datacenter + catv",
                "components": {
                    "datacenter": {"value": 50.0, "confidence": 0.40,
                                    "basis": "Strong datacenter demand, 1.6T transceiver pipeline"},
                    "catv": {"value": 15.0, "confidence": 0.35,
                              "basis": "1.8 GHz amplifier ramp, CATV market cyclical"},
                },
            },
        },
        "extra_assumptions": {
            "cogs_delta_bps": -300,
            "opex_delta_bps": -200,
            "stock_comp_growth_pct": 10.0,
            "da_growth_pct": 5.0,
        },

        "bear_revisions": [
            {"driver": "revenue_growth", "component": "datacenter", "new_value": 30.0,
             "reason": "AI capex cycle peaks, datacenter order deferrals"},
        ],
        "confidence_revisions": [],

        "actuals": None,
        "consensus": {"eps": 0.50, "revenue_m": 450},
    },

    # ── RKLB: Rocket Lab — pre-profit aerospace ──
    "RKLB": {
        "name": "Rocket Lab USA, Inc.",
        "cik": "0001819994",
        "sic_code": "3761",

        "earnings_text": (
            "Rocket Lab FY2024 Results. "
            "Global leader in launch services and space systems. "
            "Product and services revenue, not subscription-based. "
            "Two segments: Launch Services and Space Systems. "
            "Total revenue $436.2 million, up 78% year-over-year. "
            "Q4 2024 revenue $132.4 million, up 121% year-over-year. "
            "GAAP gross margin approximately 27%. "
            "Cost of revenues approximately 73% of total revenue. "
            "Company is pre-profit with significant R&D investment in Neutron rocket. "
            "Revenue mix: approximately 70% Space Systems, 30% Launch Services. "
            "Backlog over $1 billion including $515 million SDA contract. "
            "GAAP net loss approximately $120 million. "
            "Shares outstanding approximately 500 million."
        ),

        "prior_year": {
            "revenue_m": 436.2,
            "store_count": 1,
            "cogs_pct": 73.0,
            "opex_pct": 25.0,             # R&D + SG&A (heavy Neutron investment)
            "stock_comp_m": 55.0,
            "da_m": 40.0,
        },
        "constants": {
            "tax_rate": 0.0,
            "shares_m": 500,
            "net_interest_m": -20,
        },

        "drivers": {
            "revenue_growth": {
                "assumption_key": "revenue_growth_pct",
                "formula": "space_systems + launch",
                "components": {
                    "space_systems": {"value": 30.0, "confidence": 0.45,
                                       "basis": "Backlog $1B+, SDA contract, spacecraft demand"},
                    "launch": {"value": 10.0, "confidence": 0.50,
                                "basis": "Electron cadence increasing, Neutron not yet revenue"},
                },
            },
        },
        "extra_assumptions": {
            "cogs_delta_bps": -200,       # gross margin improving with scale
            "opex_delta_bps": 0,          # R&D stays high (Neutron)
            "stock_comp_growth_pct": 0,
            "da_growth_pct": 15.0,
        },

        "bear_revisions": [
            {"driver": "revenue_growth", "component": "space_systems", "new_value": 15.0,
             "reason": "Neutron delays, contract timing slips, defense budget uncertainty"},
        ],
        "confidence_revisions": [],

        "actuals": None,
        "consensus": {"eps": -0.15, "revenue_m": 620},
    },
}
