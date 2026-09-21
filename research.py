"""External evidence and strict, auditable research proposals."""
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from research_data import DataAcquisition


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    title: str = Field(min_length=3)
    finding: str = Field(min_length=30)
    limitations: str = Field(min_length=20)
    source_kind: str = Field(description="Paper, author research, institutional documentation, or commercial claim")


class ResearchBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[SourceEvidence] = Field(min_length=1, max_length=5)
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


def validate_brief(brief, evidence, assets, available_fields=None):
    known = {source["url"] for source in evidence["sources"]}
    if any(source.url not in known for source in brief.sources):
        raise ValueError("La propuesta cita una URL que no apareció en la búsqueda web")
    if not brief.compatible:
        raise ValueError("Idea incompatible: " + brief.compatibility_reason)
    available = ({"timestamp", "open", "high", "low", "close"} if available_fields is None else
                 set.intersection(*(set(available_fields.get(asset, set())) for asset in brief.target_assets)))
    missing = set(brief.required_fields) - available
    if missing or brief.timeframe != "1d" or brief.requires_shorting or brief.requires_leverage:
        raise ValueError("La idea requiere datos, frecuencia, cortos o apalancamiento no disponibles")
    if not set(brief.target_assets).issubset(assets):
        raise ValueError("La idea requiere activos no disponibles")
