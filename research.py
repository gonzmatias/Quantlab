"""External evidence and strict, auditable research proposals."""
from urllib.parse import urlsplit
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from research_data import DataAcquisition


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    title: str = Field(min_length=3)
    finding: str = Field(min_length=30)
    limitations: str = Field(min_length=20)
    source_kind: str = Field(description="Paper, author research, institutional documentation, or commercial claim")
    published_at: str = "unknown"
    supporting_passage: str = Field(default="", description="Short exact passage supporting the claim; empty when unavailable")
    evidence_scope: str = Field(default="", description="Actual instruments, period, horizon and assumptions of the source")


class ResearchBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[SourceEvidence] = Field(default_factory=list, max_length=5)
    origin: Literal["replication", "adaptation", "conjecture"] = "adaptation"
    assumptions: list[str] = Field(default_factory=list)
    rule_mapping: list[str] = Field(default_factory=list, description="For each entry/exit rule explain the link to the mechanism; separate interpretation from source claims")
    execution_requirements: list[str] = Field(default_factory=list, description="Required order types, latency, tick/depth data, financing and contract")
    order_type: Literal["market", "limit", "stop"] = "market"
    requires_ticks: bool = False
    requires_depth: bool = False
    requires_bid_ask: bool = False
    stop_evaluation: Literal["signal_close", "intrabar"] = "signal_close"
    mechanism: str = Field(min_length=40)
    prediction: str = Field(min_length=30)
    falsification: str = Field(min_length=30)
    adaptation: str = Field(min_length=40)
    parameter_reasoning: str = Field(min_length=30)
    target_assets: list[str] = Field(min_length=1)
    required_fields: list[str] = Field(min_length=1)
    timeframe: str
    requires_shorting: bool
    requires_leverage: bool
    compatible: bool
    compatibility_reason: str = Field(min_length=30)
    research_approach: str = Field(default="", description="Método elegido para investigar esta ventaja")
    change_from_previous: str = Field(default="", description="Qué se conserva, refuta o cambia y por qué")
    contrary_evidence: str = Field(default="", description="Evidencia que contradice el mecanismo")
    data_requests: list[str] = Field(default_factory=list, description="Datos faltantes: campo, fuente, frecuencia, disponibilidad histórica y revisiones")
    data_acquisition: list[DataAcquisition] = Field(default_factory=list, description="CSV públicos con fecha real de disponibilidad: URL presente en fuentes, columna de valor y de publicación. Nunca usar fecha de observación macro como publicación.")


def safe_source_url(url):
    try:
        parsed = urlsplit(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def extract_web_evidence(response):
    """Accept citations only from tool metadata, never URLs invented in prose."""
    if response.get("status") != "completed":
        raise RuntimeError("La búsqueda web no terminó correctamente")
    sources, calls, text = {}, [], []
    for item in response.get("output", []):
        if item.get("type") == "web_search_call":
            if item.get("status") == "completed":
                calls.append(item.get("action", {}))
                for source in item.get("action", {}).get("sources", []):
                    url = source.get("url", "")
                    if safe_source_url(url):
                        sources[url] = {"url": url, "title": source.get("title", url)}
        if item.get("type") == "message":
            for block in item.get("content", []):
                if block.get("type") != "output_text":
                    continue
                text.append(block.get("text", ""))
                for citation in block.get("annotations", []):
                    url = citation.get("url", "")
                    if citation.get("type") == "url_citation" and safe_source_url(url):
                        sources[url] = {"url": url, "title": citation.get("title", url)}
    if not calls or not sources or not any(text):
        raise RuntimeError("La búsqueda no aportó fuentes trazables; no se sustituirá por una estrategia genérica")
    return {"response_id": response.get("id"), "sources": list(sources.values()),
            "search_actions": calls, "summary": "\n".join(text)}


def validate_brief(brief, evidence, assets, available_fields=None, capabilities=None):
    known = {source["url"] for source in evidence["sources"]}
    if any(source.url not in known for source in brief.sources):
        raise ValueError("La propuesta cita una URL que no apareció en la búsqueda web")
    if not brief.sources and brief.origin != "conjecture":
        raise ValueError("Replicación/adaptación requiere fuentes; una idea original debe declararse conjetura")
    if not set(brief.target_assets).issubset(assets):
        raise ValueError("NEEDS_CAPABILITY: la idea requiere activos no disponibles")
    if not brief.compatible:
        raise ValueError("NEEDS_CAPABILITY: " + brief.compatibility_reason)
    available = ({"timestamp", "open", "high", "low", "close"} if available_fields is None else
                 set.intersection(*(set(available_fields.get(asset, set())) for asset in brief.target_assets)))
    missing = set(brief.required_fields) - available
    capabilities = capabilities or {a: {"signal_timeframes": ["1d"], "directions": ["long"]} for a in assets}
    unsupported = any(brief.timeframe not in capabilities[a]["signal_timeframes"] or
                      (brief.requires_shorting and "short" not in capabilities[a]["directions"])
                      or (brief.requires_bid_ask and capabilities[a].get("quote_model") != "bid_ask")
                      for a in brief.target_assets)
    if missing or unsupported or brief.requires_leverage or brief.requires_ticks or brief.requires_depth or brief.order_type != "market" or brief.stop_evaluation != "signal_close":
        raise ValueError("NEEDS_CAPABILITY: la idea requiere datos, frecuencia, cortos o apalancamiento no disponibles")


def evidence_review(brief):
    """Expose missing support; never promote model-written prose to verified facts."""
    warnings = []
    if not brief.rule_mapping:
        warnings.append("Correspondencia mecanismo-reglas no documentada")
    if not brief.assumptions:
        warnings.append("Supuestos no explicitados")
    if not brief.contrary_evidence:
        warnings.append("Evidencia contraria no documentada")
    sources = [{"url": s.url, "has_passage": bool(s.supporting_passage), "has_scope": bool(s.evidence_scope),
                "verification": "HUMAN_REVIEW_REQUIRED"} for s in brief.sources]
    return {"origin": brief.origin, "warnings": warnings, "sources": sources,
            "status": "DOCUMENTATION_INCOMPLETE" if warnings else "DOCUMENTED_NOT_VERIFIED",
            "limitation": "Traceable URLs and model explanations do not establish truth or causal fidelity"}
