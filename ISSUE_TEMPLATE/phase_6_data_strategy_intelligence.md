## Overview
Update PRODUCT_PLAN.md to add Phase 6: Data Strategy & Intelligence — the strategic layer that solves the "Plaid has direct API access" problem.

## What to Add

### Phase 6 — Data Strategy & Intelligence (Weeks 11-14)

**Goal:** Make Plaidify the universal data access layer that intelligently routes to the best data source — API when available, browser when necessary — while self-healing broken blueprints.

| Week | Dates | Focus | Ship |
|------|-------|-------|------|
| **11** | May 25-29 | API Connector blueprint type | `connector_type: "api"` working |
| **12** | Jun 1-5 | Open Banking integration (FDX, PSD2, UK) | Bank API templates |
| **13** | Jun 8-12 | Blueprint Auto-Healer (LLM-powered) | `plaidify blueprint doctor/heal` |
| **14** | Jun 15-19 | Provider aggregation layer + v1.1.0 | Intelligent routing + ship |

### Phase 6 Deliverables
- [ ] API Connector blueprint type with OAuth2, API key, Bearer token auth
- [ ] JSONPath-based API response extraction
- [ ] Automatic fallback: API → browser
- [ ] FDX 6.0 connector template (US/Canada banks)
- [ ] PSD2/Berlin Group connector template (EU banks)
- [ ] Open Banking UK v3.1 connector template
- [ ] LLM-powered blueprint auto-healer
- [ ] Blueprint health monitoring dashboard
- [ ] Provider aggregation layer with intelligent routing
- [ ] Third-party aggregator support (Teller, MX, Akoya)
- [ ] v1.1.0 released

### Updated Execution Calendar (add to §10)
| Week | Dates | Focus | Ship |
|------|-------|-------|------|
| **11** | May 25-29 | API Connector blueprint type | `connector_type: api` |
| **12** | Jun 1-5 | Open Banking (FDX, PSD2, UK OB) | Bank API templates |
| **13** | Jun 8-12 | Blueprint Auto-Healer | `plaidify blueprint doctor` |
| **14** | Jun 15-19 | Provider aggregation + Polish | **🚀 v1.1.0** |

### Updated Key Milestones (add to §10)
| Date | Milestone |
|------|-----------|
| **May 29** | API connectors work alongside browser connectors |
| **Jun 5** | Connect to banks via Open Banking APIs |
| **Jun 12** | Broken blueprints auto-detected and self-healed |
| **Jun 19** | **🚀 v1.1.0 — Universal data access layer** |

### Updated Risk Matrix (add to §11)
| Risk | Prob. | Impact | Mitigation |
|------|-------|--------|------------|
| Open Banking certification requirements | Medium | High | Start with aggregator proxies (Akoya/Teller), get certified later |
| LLM costs for auto-healing | Low | Medium | Cache repairs, only trigger on failure, use small models |
| Third-party aggregator dependencies | Medium | Medium | Multiple providers, graceful fallback to browser |

### Updated Success Metrics (add to §12)
| Metric | Target |
|--------|--------|
| API connector blueprints | 10+ |
| Open Banking standards supported | 3 (FDX, PSD2, UK OB) |
| Blueprint auto-heal success rate | > 70% |
| Provider routing fallback rate | < 5% |

## Also update
- [ ] Table of Contents: add §10 for Phase 6
- [ ] Target Architecture diagram: add "API Connector" and "Provider Router" to the engine layer
- [ ] "What's Built vs What's Next" appendix: add Phase 6 items
- [ ] Version and Last Updated date

## Phase
Meta — documentation update

