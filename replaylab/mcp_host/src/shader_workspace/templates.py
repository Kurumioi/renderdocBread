"""Bounded model fields and Host-owned rendering for Shader analysis."""

from __future__ import annotations

import json

import re
from typing import Any, Mapping

from ..review_contract import reason

ANALYSIS_SCHEMA = "replaylab-shader-analysis/v1"
HEADINGS = ("Overview", "Inputs and resources", "Computation", "Outputs", "Observed facts", "Inferences")
_KEYS = ("overview", "inputsResources", "computation", "outputs", "observedFacts", "inferences")


def analysis_template() -> dict[str, Any]:
    return {"schema": ANALYSIS_SCHEMA, "overview": "", "inputsResources": [],
            "computation": [], "outputs": [],
            "observedFacts": [{"claim": "", "evidence": ""}],
            "inferences": [{"claim": "", "confidence": "medium"}]}


def organization_contract(source: str) -> dict[str, Any]:
    """Describe measurable organization work separately from correctness checks."""
    from .flow_graph import computation_ranges
    from .quality import organization_quality
    functions = computation_ranges(source)
    quality = organization_quality(source)
    return {"schema": "replaylab-shader-algorithm-organization/v1", "goal": "algorithm-structure",
            "functions": functions, "largeFunctionStatementThreshold": 160,
            "quality": quality,
            "needsAlgorithmRestructuring": (any(item["statementCount"] > 160 for item in functions)
                                            or quality["anonymousFunctionCount"] > 0
                                            or quality["mechanicalFunctionCount"] > 0),
            "rules": [
                "Split large computation bodies into cohesive functions and meaningful data-flow stages; comments alone are not completed algorithm organization.",
                "Use source-supported names, preserve uncertain meanings, and do not invent lighting, samples, branches or resources.",
                "Review every executable function, including small helpers. Replace anonymous decompiler locals/shared fields with expression or data-role names; temp123 is not a meaningful rename. Unknown business intent may retain neutral operational names.",
                "Do not evade organization by fixed-size Stage1/Stage2 chunks. Decompose at actual calculation boundaries, factor repeated pure operations, and document internal sections of long functions.",
                "Recognize dot/cross/floor/smoothstep/matrix identities only with supported types, domain, arithmetic order and precision; retain original operations when uncertain.",
                "A small or already structured shader needs no artificial helper functions. Long functions may retain cohesive sections documented at actual calculation spans.",
                "Structure checks do not prove behavior: compile, ABI and the selected bounded numerical observation remain independent.",
                "Keep shared math as inline source functions in this transaction; external includes require separately managed source-bundle support."]}


def organization_structure_issues(baseline: str, proposal: str, flow_spec: Mapping[str, Any], renames: Mapping[str, str] | None = None):
    from .equivalence import rename_only_equivalent
    from .flow_graph import bind_computation_range, computation_ranges
    from .quality import organization_quality_issues
    original = computation_ranges(baseline)
    large = [item for item in original if item["statementCount"] > 160]
    issues = organization_quality_issues(proposal)
    if not large:
        return issues
    target = ", ".join("%s (%d statements)" % (item["function"], item["statementCount"]) for item in large[:6])
    if rename_only_equivalent(baseline, proposal, renames).get("equal"):
        issues.append(reason("algorithm-organization-incomplete", "Large calculation bodies remain unchanged except for names/comments: " + target,
                             location="proposal", direction="Extract cohesive calculations into functions and simplify only proven expressions. Keep unknown meanings explicit. Submit the full proposal and corresponding calculation stages.",
                             target="Actual algorithm organization of " + target,
                             example="PrepareInputData -> SampleMaterial -> CombineContributions -> WriteOutputs, only where these calculations exist."))
    current = computation_ranges(proposal)
    if (len(current) <= len(original) and max((item["statementCount"] for item in current), default=0)
            >= max(item["statementCount"] for item in large)):
        if not issues:
            issues.append(reason("algorithm-body-not-decomposed", "The largest calculation body was not reduced or decomposed.", location="proposal",
                                 direction="Extract coherent data preparation/calculation/output helpers or a proven substantial simplification; changing syntax alone does not organize the algorithm.",
                                 target=target, example="Extract a complete computation using explicit input/output parameters, then validate that batch."))
    stages = flow_spec.get("stages", [])
    if not stages:
        issues.append(reason("algorithm-flow-required", "A large reconstructed shader needs a calculation-stage flow specification.", location="flow_spec.stages",
                             direction="Fill flowSpecTemplate using actual function-body sourceRange and intermediate reads/writes.", target="Core algorithm data flow, not a declaration list.",
                             example='{"id":"sample_material","function":"SampleMaterial","sourceRange":{"startLine":40,"endLine":80},"reads":["uv"],"writes":["SampleMaterial.return"],"next":["combine"]}'))
    for item in current:
        if item["statementCount"] <= 160:
            continue
        matching = [stage for stage in stages if stage["function"] == item["function"]]
        counts = [bind_computation_range(proposal, stage["function"], stage.get("sourceRange"))["statementCount"] for stage in matching]
        if len(matching) < 2 or any(count > 160 for count in counts):
            issues.append(reason("algorithm-stage-too-broad", "A long function is represented as one oversized calculation node: " + item["function"],
                                 location="flow_spec.stages", direction="Use a few complete algorithm sections with concrete source ranges, or extract these sections as functions. Do not create one node per temporary.",
                                 target="Readable calculation stages within " + item["function"], example="Coordinate preparation, texture evaluation, contribution combination, final packing (only actual sections)."))
    return issues


def render_analysis_fields(value: Any) -> tuple[str, list[dict[str, str]]]:
    issues: list[dict[str, str]] = []

    def issue(code: str, location: str, message: str, example: str = "") -> None:
        issues.append(reason(code, message, location=location, direction="Fill the matching field in analysisTemplate; the Host supplies Markdown formatting.",
                             target=location, example=example))

    if not isinstance(value, Mapping) or set(value) != {"schema", *_KEYS} or value.get("schema") != ANALYSIS_SCHEMA:
        expected = ["schema", *_KEYS]
        if isinstance(value, Mapping):
            missing = [key for key in expected if key not in value]
            extra = [str(key) for key in value if key not in expected]
            detail = " Missing: %s. Unexpected: %s.%s" % (
                ", ".join(missing) or "none", ", ".join(extra) or "none",
                "" if value.get("schema") == ANALYSIS_SCHEMA else " schema must be %r." % ANALYSIS_SCHEMA)
        else:
            detail = " The content must be one JSON object, not %s." % type(value).__name__
        issue("analysis-schema", "analysis",
              "Analysis must use exactly the analysisTemplate fields and schema: %s.%s" % (", ".join(expected), detail),
              json.dumps(analysis_template(), ensure_ascii=False))
        return "", issues

    def text(item: Any, location: str, maximum: int = 2048) -> str:
        if (not isinstance(item, str) or not item.strip() or len(item) > maximum
                or any(ord(c) < 32 or 0x7f <= ord(c) <= 0x9f for c in item)
                or any(mark in item for mark in ("```", "<", ">"))):
            issue("analysis-text", location, "Use non-empty single-line plain text within %d characters." % maximum, "The pixel shader writes one color target.")
            return ""
        return re.sub(r"([\\`*_\[\]#])", r"\\\1", item.strip())

    sections: list[str] = [text(value["overview"], "analysis.overview")]
    for key in _KEYS[1:]:
        items = value[key]
        if not isinstance(items, list) or len(items) > 64:
            issue("analysis-list", "analysis." + key, "Use an array with at most 64 entries.")
            sections.append("")
            continue
        if key in ("computation", "outputs", "observedFacts") and not items:
            issue("analysis-empty", "analysis." + key, "Record at least one supported item.")
        lines = []
        for index, item in enumerate(items):
            location = "analysis.%s[%d]" % (key, index)
            if key == "observedFacts":
                if not isinstance(item, Mapping) or set(item) != {"claim", "evidence"}:
                    issue("analysis-fact-schema", location, "An observed fact requires claim and evidence.", '{"claim":"One color target is declared","evidence":"source:MainPS SV_Target0"}')
                    continue
                lines.append("- %s [evidence: %s]" % (text(item["claim"], location + ".claim"), text(item["evidence"], location + ".evidence", 512)))
            elif key == "inferences":
                if not isinstance(item, Mapping) or set(item) != {"claim", "confidence"} or item.get("confidence") not in ("high", "medium", "low"):
                    issue("analysis-inference-schema", location, "An inference requires a claim and high/medium/low confidence.", '{"claim":"This may be a lighting pass","confidence":"low"}')
                    continue
                lines.append("- %s [confidence: %s]" % (text(item["claim"], location + ".claim"), item["confidence"]))
            else:
                lines.append("- " + text(item, location))
        sections.append("\n".join(lines) or "- None recorded.")
    if issues:
        return "", issues
    return "# Shader analysis\n\n" + "\n\n".join("## %s\n%s" % pair for pair in zip(HEADINGS, sections)) + "\n", []


def analysis_markdown_issues(text: str) -> list[dict[str, str]]:
    """Review persisted Markdown too; old files cannot bypass template checks."""
    matches = list(re.finditer(r"^## ([^\r\n]+)\s*$", text, re.MULTILINE))
    if [m.group(1).strip() for m in matches] != list(HEADINGS) or "```" in text:
        return [reason("analysis-template", "Analysis sections must exactly match the six Host headings in order.",
                       location="analysis.md", direction="Submit analysisTemplate through write_shader_workspace_organization_file(kind='analysis').",
                       target="analysisTemplate", example='{"overview":"One color output", ...}')]
    issues = []
    for index, match in enumerate(matches):
        content = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip()
        if not content or content.startswith("(") or "<" in content or ">" in content:
            issues.append(reason("analysis-empty", "Analysis section is empty, still a placeholder, or contains markup.",
                                 location="analysis.md:" + match.group(1), direction="Fill this section with supported plain-text observations or explicit inferences.",
                                 target=match.group(1), example="- Returns the declared SV_Target0 value."))
        if match.group(1) == "Observed facts" and content and any(
                not re.search(r"\[evidence: [^\r\n]+\]$", line.strip())
                for line in content.splitlines() if line.strip()):
            issues.append(reason("analysis-evidence", "Observed facts need an explicit source/tool reference.", location="analysis.observedFacts",
                                 direction="Provide each claim with its evidence field.", target="analysis.observedFacts", example='{"claim":"One color output","evidence":"source:MainPS"}'))
        if match.group(1) == "Inferences" and content != "- None recorded." and content and any(
                not re.search(r"\[confidence: (?:high|medium|low)\]$", line.strip())
                for line in content.splitlines() if line.strip()):
            issues.append(reason("analysis-confidence", "Inferences need explicit confidence.", location="analysis.inferences",
                                 direction="Provide each claim with high, medium or low confidence.", target="analysis.inferences", example='{"claim":"May be lighting","confidence":"low"}'))
    return issues
