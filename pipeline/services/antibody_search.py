"""
Antibody search service using Claude API with web search.

Called ON DEMAND only (not automatically) — costs ~$0.15 per lookup.
Uses ANTHROPIC_API_KEY from Django settings (environment variable).
"""

import json
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-20250514"
TIMEOUT_SECONDS = 90

YCHAROS_PARTNERS = [
    "Abcam",
    "ABCD Antibodies",
    "ABclonal",
    "Abbexa",
    "Aviva Systems Biology",
    "Bio-Techne (R&D Systems / Novus Biologicals)",
    "Cell Signaling Technology (CST)",
    "Developmental Studies Hybridoma Bank (DSHB)",
    "GeneTex",
    "Miltenyi Biotec",
    "MilliporeSigma (Sigma-Aldrich / EMD Millipore)",
    "Proteintech",
    "Synaptic Systems",
    "Thermo Fisher Scientific (Invitrogen)",
]

PARTNER_LIST_STR = ", ".join(YCHAROS_PARTNERS)


def is_configured() -> bool:
    """Check if the Anthropic API key is set."""
    return bool(getattr(settings, 'ANTHROPIC_API_KEY', ''))


def search_recombinant_antibodies(gene_name: str) -> dict:
    """
    Search for recombinant antibodies targeting a gene from YCharOS partners.

    Returns dict with:
        found, gene_name, total_recombinant, antibodies, search_summary, error
    """
    result = {
        "found": False,
        "gene_name": gene_name,
        "total_recombinant": 0,
        "antibodies": [],
        "search_summary": "",
        "error": None,
    }

    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '')
    if not api_key:
        result["error"] = "Anthropic API key not configured"
        return result

    if not gene_name or not gene_name.strip():
        result["error"] = "No gene name provided"
        return result

    gene_name = gene_name.strip().upper()

    prompt = f"""I need you to find RECOMBINANT antibodies targeting the human protein {gene_name} (also known as the mineralocorticoid receptor if that's what {gene_name} encodes — use the common protein name too).

Search these specific vendor websites one by one. For each vendor, search their site for antibodies against {gene_name} and check if any are labelled as recombinant:

1. Search abcam.com for {gene_name} recombinant antibody — look for "RabMAb" or "recombinant" labels
2. Search cellsignal.com for {gene_name} antibody — CST recombinant clones often start with "E" (e.g. E9W1M)
3. Search ptglab.com (Proteintech) for {gene_name} — look for "CoraLite" conjugated recombinant antibodies
4. Search bio-techne.com or rndsystems.com for {gene_name} recombinant antibody
5. Search genetex.com for {gene_name} recombinant antibody
6. Search thermofisher.com for {gene_name} recombinant antibody — look for "Invitrogen" recombinant line
7. Search abclonal.com for {gene_name} — check if any are marked recombinant (note: polyclonal raised against recombinant protein does NOT count)
8. Search sigmaaldrich.com (MilliporeSigma) for {gene_name} recombinant antibody
9. Search miltenyibiotec.com for {gene_name} — look for "REAfinity" recombinant antibodies
10. Search sysy.com (Synaptic Systems) for {gene_name} recombinant antibody

CRITICAL DISTINCTION: A "recombinant antibody" means the ANTIBODY ITSELF is recombinantly produced (e.g. RabMAb, REAfinity, recombinant monoclonal). An antibody "raised against recombinant protein" but produced in animals as a polyclonal is NOT a recombinant antibody.

For each genuine recombinant antibody found, record:
- vendor, catalogue_number, url (direct product page link), clone_id, host, applications, notes

Respond ONLY with JSON (no markdown, no backticks):
{{
    "total_recombinant": <number>,
    "antibodies": [
        {{
            "vendor": "<vendor>",
            "catalogue_number": "<exact cat#>",
            "url": "<product page URL>",
            "clone_id": "<clone ID>",
            "host": "<host species>",
            "applications": "<e.g. WB, IP, IF, FC>",
            "notes": "<e.g. KO validated, BSA-free>"
        }}
    ],
    "search_summary": "<1-2 sentence summary>"
}}

If none found, return total_recombinant: 0 with empty antibodies array."""
    try:
        response = requests.post(
            API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 4096,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "tools": [{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 12,
                }],
            },
            timeout=TIMEOUT_SECONDS,
        )

        if response.status_code != 200:
            logger.error("Anthropic API error for '%s': %s %s",
                         gene_name, response.status_code, response.text[:500])
            result["error"] = f"API error ({response.status_code})"
            return result

        data = response.json()
        
        # Debug: log the raw response content types
        content_types = [block.get("type") for block in data.get("content", [])]
        logger.warning("Antibody search response for '%s': stop_reason=%s, content_types=%s",
                       gene_name, data.get("stop_reason"), content_types)
        
        # Extract text content
        text_parts = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block["text"])

        full_text = "\n".join(text_parts).strip()

        # Clean markdown fences
        if full_text.startswith("```"):
            full_text = full_text.split("\n", 1)[-1]
        if full_text.endswith("```"):
            full_text = full_text.rsplit("```", 1)[0]
        full_text = full_text.strip()

        # Parse JSON
        try:
            parsed = json.loads(full_text)
        except json.JSONDecodeError:
            start = full_text.find("{")
            end = full_text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(full_text[start:end])
                except json.JSONDecodeError:
                    result["error"] = "Could not parse antibody search results"
                    result["search_summary"] = full_text[:500]
                    return result
            else:
                result["error"] = "No JSON found in API response"
                result["search_summary"] = full_text[:500]
                return result

        antibodies = parsed.get("antibodies", [])
        result["total_recombinant"] = parsed.get("total_recombinant", len(antibodies))
        result["antibodies"] = antibodies
        result["search_summary"] = parsed.get("search_summary", "")
        result["found"] = len(antibodies) > 0

        # Log usage
        usage = data.get("usage", {})
        search_count = usage.get("server_tool_use", {}).get("web_search_requests", 0)
        logger.info(
            "Antibody search for '%s': %d results, %d web searches, "
            "%d input/%d output tokens",
            gene_name, len(antibodies), search_count,
            usage.get("input_tokens", 0), usage.get("output_tokens", 0),
        )

        return result

    except requests.Timeout:
        result["error"] = "Search timed out — try again"
        return result
    except requests.RequestException as e:
        result["error"] = f"API request failed: {str(e)}"
        return result
    except Exception as e:
        logger.error("Antibody search error for '%s': %s", gene_name, e)
        result["error"] = f"Unexpected error: {str(e)}"
        return result
