from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(frozen=True)
class GlossaryEntry:
    key: str
    title: str
    definition: str
    formula: str | None = None
    interpretation: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageHelpEntry:
    page: str
    title: str
    summary: str
    sections: tuple[Dict[str, str], ...] = ()
    next_steps: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


_GLOSSARY: tuple[GlossaryEntry, ...] = (
    GlossaryEntry(
        key="revenue",
        title="Revenue",
        definition="Total sales value for orders in the active scope and time window.",
        formula="Revenue = sum(line sales amount)",
        interpretation="Higher revenue is positive when margin quality and customer concentration remain healthy.",
        aliases=("sales", "net sales"),
    ),
    GlossaryEntry(
        key="profit",
        title="Profit",
        definition="Commercial profit calculated from revenue minus cost.",
        formula="Profit = Revenue - Cost",
        interpretation="Profit quality should be interpreted alongside cost coverage and margin risk.",
        aliases=("gross profit", "gp"),
    ),
    GlossaryEntry(
        key="margin_pct",
        title="Margin %",
        definition="Profit as a percentage of revenue.",
        formula="Margin % = Profit / Revenue",
        interpretation="Margin % helps compare commercial quality across products, customers, and periods.",
        aliases=("margin", "gross margin"),
    ),
    GlossaryEntry(
        key="aov",
        title="AOV",
        definition="Average order value in the active window.",
        formula="AOV = Revenue / Orders",
        interpretation="Rising AOV may come from price, mix, or larger orders.",
        aliases=("average order value",),
    ),
    GlossaryEntry(
        key="asp",
        title="ASP",
        definition="Average selling price per unit in the active window.",
        formula="ASP = Revenue / Units",
        interpretation="ASP should be reviewed with volume and mix to avoid false price signals.",
        aliases=("average selling price",),
    ),
    GlossaryEntry(
        key="hhi",
        title="HHI",
        definition="Herfindahl-Hirschman Index used to quantify concentration risk.",
        formula="HHI = sum(share^2) across entities",
        interpretation="Higher HHI means more dependency on a small number of customers or products.",
        aliases=("concentration index",),
    ),
    GlossaryEntry(
        key="margin_dispersion",
        title="Margin Dispersion",
        definition="Spread of margin outcomes across entities, often shown as standard deviation or percentile range.",
        interpretation="High dispersion indicates unstable pricing or cost performance.",
        aliases=("dispersion", "margin spread"),
    ),
    GlossaryEntry(
        key="price_volume_mix",
        title="Price/Volume/Mix",
        definition="Driver decomposition splitting commercial movement into price, volume, and mix effects.",
        interpretation="Use this to explain why revenue or profit moved, not just how much it moved.",
        aliases=("pvm", "drivers"),
    ),
    GlossaryEntry(
        key="forecast_confidence",
        title="Forecast Confidence",
        definition="Quality indicator for forecast reliability based on history depth and fit quality.",
        interpretation="Low confidence means outlook direction can be useful but should not be treated as precise commitment.",
        aliases=("forecast quality",),
    ),
    GlossaryEntry(
        key="cost_coverage",
        title="Cost Coverage",
        definition="Percentage of records with usable cost values.",
        formula="Cost coverage = records with cost / total records",
        interpretation="Low coverage weakens profit, margin, and risk conclusions.",
        aliases=("coverage",),
    ),
    GlossaryEntry(
        key="pack_coverage",
        title="Pack Coverage",
        definition="Coverage level for pack/weight fields used by weighted and per-pound metrics.",
        interpretation="Low pack coverage can distort weight-based KPIs.",
        aliases=("weight coverage",),
    ),
    GlossaryEntry(
        key="returns_workflow",
        title="Returns Workflow",
        definition="Operational process for returns intake, approvals, warehouse handling, and refund execution.",
        interpretation="Pending approvals and repeat reasons are primary control points.",
        aliases=("returns process", "rma workflow"),
    ),
    GlossaryEntry(
        key="profit_per_lb",
        title="Profit per Pound",
        definition="Profit normalized by shipped weight.",
        formula="Profit per lb = Profit / Weight (lb)",
        interpretation="Use with pack/weight coverage context; low pack coverage can distort this metric.",
        aliases=("contribution per pound", "margin per lb"),
    ),
    GlossaryEntry(
        key="mix_growth",
        title="Mix Growth",
        definition="Change attributable to product/customer composition shifts rather than pure volume or price.",
        interpretation="Mix growth can raise revenue while still lowering margin if lower-quality mix expands.",
        aliases=("mix effect",),
    ),
    GlossaryEntry(
        key="revenue_growth",
        title="Revenue Growth",
        definition="Change in total revenue between two periods.",
        formula="Revenue growth % = (Current - Prior) / Prior",
        interpretation="Interpret with low-base rules and margin quality, not in isolation.",
        aliases=("topline growth",),
    ),
    GlossaryEntry(
        key="cost_coverage_effect",
        title="Cost Coverage Effect",
        definition="Impact of missing cost records on profit and margin reliability.",
        interpretation="When cost coverage is low, treat profit and margin signals as directional instead of exact.",
        aliases=("coverage caveat", "cost data quality"),
    ),
)

_PAGE_HELP: tuple[PageHelpEntry, ...] = (
    PageHelpEntry(
        page="overview",
        title="Overview Workspace",
        summary="Executive-level view of revenue, profit quality, concentration risk, and trust signals.",
        sections=(
            {"id": "scorecard", "title": "Scorecard", "description": "Core KPIs with prior-period deltas."},
            {"id": "drivers", "title": "Drivers", "description": "Price/volume/mix decomposition and mover diagnostics."},
            {"id": "risk", "title": "Risk", "description": "Concentration and profitability watchouts."},
            {"id": "health", "title": "Data Health", "description": "Coverage and freshness checks for confidence."},
        ),
        next_steps=(
            "Drill into customers or products driving the biggest movement.",
            "Validate trust/coverage before escalating margin conclusions.",
            "Use executive summary mode for leadership communication.",
        ),
        aliases=("dashboard", "overview page"),
    ),
    PageHelpEntry(
        page="customers",
        title="Customers Workspace",
        summary="Portfolio health view for customer growth, retention, churn risk, and account concentration.",
        sections=(
            {"id": "churn_risk", "title": "Churn Risk", "description": "At-risk and declining-account patterns."},
            {"id": "drivers", "title": "Drivers", "description": "Top gainers/decliners and revenue mix change."},
            {"id": "rfm", "title": "RFM", "description": "Recency-frequency-monetary behavioral segmentation."},
            {"id": "clv", "title": "CLV", "description": "Lifetime value and high-value risk groups."},
        ),
        next_steps=(
            "Review top at-risk customers and related product mix.",
            "Compare repeat behavior and prior-period baseline.",
            "Open customer drilldown for account-level action planning.",
        ),
        aliases=("customer page",),
    ),
    PageHelpEntry(
        page="products",
        title="Products Workspace",
        summary="SKU performance view for pricing, velocity, profitability, and dependency risks.",
        sections=(
            {"id": "trajectory", "title": "Trajectory", "description": "Revenue/profit trend and momentum."},
            {"id": "pricing_guardrails", "title": "Guardrails", "description": "Outlier pricing and margin risk checks."},
            {"id": "recommendations", "title": "Recommendations", "description": "Actionable pricing or mix adjustments."},
        ),
        next_steps=(
            "Focus on high-revenue SKUs with negative or deteriorating margin.",
            "Check customer dependency for exposed products.",
            "Use product drilldown for customer-region-supplier context.",
        ),
        aliases=("product page",),
    ),
    PageHelpEntry(
        page="regions",
        title="Regions Workspace",
        summary="Regional performance view combining revenue, profitability, retention, and operational signals.",
        sections=(
            {"id": "momentum", "title": "Momentum", "description": "Regional gainers/decliners across periods."},
            {"id": "concentration", "title": "Concentration", "description": "Dependency on top customers/products/suppliers."},
            {"id": "risk", "title": "Risk", "description": "High-risk regions and quality warnings."},
        ),
        next_steps=(
            "Investigate high-risk regions with declining trend and concentration exposure.",
            "Compare regions by margin and retention quality.",
        ),
        aliases=("region page",),
    ),
    PageHelpEntry(
        page="suppliers",
        title="Suppliers Workspace",
        summary="Supplier performance and dependency lens with margin and continuity watchouts.",
        sections=(
            {"id": "movers", "title": "Movers", "description": "Suppliers with biggest positive/negative movement."},
            {"id": "risk_opportunities", "title": "Risk & Opportunities", "description": "At-risk suppliers and improvement candidates."},
            {"id": "segments", "title": "Segments", "description": "Portfolio segmentation for prioritization."},
        ),
        next_steps=(
            "Review suppliers with large revenue stake and weak margin trend.",
            "Check dependency concentration before supplier escalation.",
        ),
        aliases=("supplier page",),
    ),
    PageHelpEntry(
        page="salesreps",
        title="Sales Reps Workspace",
        summary="Rep portfolio performance with concentration and account-risk diagnostics.",
        sections=(
            {"id": "top_reps", "title": "Top Reps", "description": "Performance ranking and contribution."},
            {"id": "concentration", "title": "Concentration", "description": "Dependency on top customer accounts."},
            {"id": "risk_flags", "title": "Risk Flags", "description": "Automated watchouts on margin and trend."},
        ),
        next_steps=(
            "Review reps with high top-customer concentration and declining trend.",
            "Compare portfolios for risk-adjusted performance.",
        ),
        aliases=("sales rep page", "portfolio page"),
    ),
    PageHelpEntry(
        page="returns",
        title="Returns Workspace",
        summary="End-to-end returns workflow for intake, approvals, warehouse handling, and closure.",
        sections=(
            {"id": "workflow", "title": "Workflow", "description": "Requested -> review/approval -> receipt -> inspection -> closure."},
            {"id": "approvals", "title": "Approvals", "description": "Pending queues and manager/warehouse checkpoints."},
            {"id": "reasons", "title": "Reasons", "description": "Top return reasons and recurring patterns."},
        ),
        next_steps=(
            "Prioritize pending approvals with the largest credit impact.",
            "Track recurring reason codes by customer and SKU.",
            "Escalate supplier-credit opportunities when pattern repeats.",
        ),
        aliases=("rma", "returns page", "returns workflow"),
    ),
)

_INDEX: Dict[str, GlossaryEntry] = {}
_SEARCH_INDEX: Dict[str, set[str]] = defaultdict(set)
for _entry in _GLOSSARY:
    _INDEX[_entry.key] = _entry
    for _alias in _entry.aliases:
        _INDEX[_alias.lower()] = _entry
    tokens = " ".join(
        (
            _entry.key,
            _entry.title,
            _entry.definition,
            _entry.formula or "",
            _entry.interpretation or "",
            " ".join(_entry.aliases),
        )
    ).lower().split()
    for token in tokens:
        _SEARCH_INDEX[token].add(_entry.key)

_PAGE_INDEX: Dict[str, PageHelpEntry] = {}
for _help in _PAGE_HELP:
    _PAGE_INDEX[_help.page] = _help
    for _alias in _help.aliases:
        _PAGE_INDEX[str(_alias).strip().lower()] = _help


def _entry_payload(entry: GlossaryEntry) -> Dict[str, Any]:
    return {
        "key": entry.key,
        "title": entry.title,
        "definition": entry.definition,
        "formula": entry.formula,
        "interpretation": entry.interpretation,
    }


def _help_payload(entry: PageHelpEntry) -> Dict[str, Any]:
    return {
        "page": entry.page,
        "title": entry.title,
        "summary": entry.summary,
        "sections": [dict(item) for item in entry.sections],
        "next_steps": list(entry.next_steps),
    }


def explain_metric(token: str | None) -> Dict[str, Any]:
    needle = str(token or "").strip().lower()
    if not needle:
        return {"status": "empty", "matches": []}
    direct = _INDEX.get(needle)
    if direct:
        return {"status": "ok", "matches": [_entry_payload(direct)]}
    return search_glossary(needle)


def search_glossary(query: str | None, *, limit: int = 6) -> Dict[str, Any]:
    needle = str(query or "").strip().lower()
    if not needle:
        return {"status": "empty", "matches": []}
    split_tokens = [token for token in needle.replace("/", " ").replace("-", " ").split() if token]
    ranked_keys: List[str] = []
    for token in split_tokens:
        ranked_keys.extend(sorted(_SEARCH_INDEX.get(token, set())))
    matches: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for key in ranked_keys:
        if key in seen:
            continue
        seen.add(key)
        entry = _INDEX.get(key)
        if entry:
            matches.append(_entry_payload(entry))
        if len(matches) >= max(1, int(limit)):
            break
    if matches:
        return {"status": "ok", "matches": matches}

    for entry in _GLOSSARY:
        blob = " ".join(
            [
                entry.key,
                entry.title,
                entry.definition,
                entry.formula or "",
                entry.interpretation or "",
                " ".join(entry.aliases),
            ]
        ).lower()
        if needle in blob:
            matches.append(_entry_payload(entry))
        if len(matches) >= max(1, int(limit)):
            break
    return {"status": "ok" if matches else "empty", "matches": matches}


def get_page_help(page: str | None, *, section: str | None = None) -> Dict[str, Any]:
    token = str(page or "").strip().lower()
    if not token:
        return {"status": "empty", "matches": []}
    entry = _PAGE_INDEX.get(token)
    if entry is None and "." in token:
        entry = _PAGE_INDEX.get(token.split(".", 1)[0])
    if entry is None:
        return {"status": "empty", "matches": []}
    payload = _help_payload(entry)
    section_token = str(section or "").strip().lower()
    if section_token:
        filtered = [
            item
            for item in payload.get("sections", [])
            if section_token in str(item.get("id") or "").strip().lower()
            or section_token in str(item.get("title") or "").strip().lower()
        ]
        payload["sections"] = filtered or payload.get("sections", [])
    return {"status": "ok", "matches": [payload]}


def knowledge_snapshot() -> Dict[str, Any]:
    return {
        "status": "ok",
        "glossary_entries": len(_GLOSSARY),
        "page_help_entries": len(_PAGE_HELP),
        "search_terms": len(_SEARCH_INDEX),
    }
