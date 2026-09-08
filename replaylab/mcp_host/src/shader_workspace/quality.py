"""Bounded, source-derived readability checks for initial AI organization.

These checks assess the delivered explanation/code, never semantic equivalence.
They are deliberately not part of user-save compilation or project reopening.
"""

from __future__ import annotations

import re
from typing import Any

from ..review_contract import reason
from .flow_graph import _function_records, _masked_source

_ANONYMOUS = re.compile(r"^(?:_\d+|(?:temp|tmp|var)\d+|frontier_phi_\w+)$", re.IGNORECASE)
_MECHANICAL_STAGE = re.compile(r"Stage\d+$")
_COMMENT = re.compile(r"(?m)^[ \t]+//\s*(\S.*)$")


def organization_quality(source: str) -> dict[str, Any]:
    """Inspect every executable function, including small extracted helpers."""
    masked = _masked_source(source)
    physical = set()
    for match in re.finditer(r"\b(?:cbuffer|tbuffer)\s+\w+[^{}]*\{([^{}]*)\}", masked):
        physical.update(re.findall(r"\b(_\d+)\b", match[1]))
    functions = []
    for record in _function_records(source):
        start, end = record["bodyStartOffset"], record["bodyEndOffset"]
        body = source[start:end]
        tokens = record["bodyTokens"]
        parameters = {item["name"] for item in record["parameters"]}
        names = sorted({token for token in [*tokens, *parameters] if _ANONYMOUS.fullmatch(token)})
        # A physical constant-buffer member may legitimately retain an original
        # captured name. Local/struct temporaries are not physical ABI fields.
        locals_ = set(re.findall(r"\b(?:float|half|double|uint|int|bool)(?:[1-4](?:x[1-4])?)?\s+(_\d+)\b", masked[start:end]))
        locals_.update(parameters)
        locals_.update(re.findall(r"\.\s*(_\d+)\b", masked[start:end]))
        names = [name for name in names if name not in physical or name in locals_]
        statements = record["statements"]
        comments = [match[1] for match in _COMMENT.finditer(body)
                    if len(match[1]) >= 12 and not re.search(r"(?:Stage\s*\d|连续计算段|unknown\s*$)", match[1])]
        long_expressions = [item for item in statements if len(item["tokens"]) > 180]
        functions.append({
            "function": record["name"], "line": record["line"],
            "statementCount": len(statements),
            "anonymousIdentifierCount": len(names), "anonymousIdentifiers": names[:16],
            "mechanicalStageName": bool(_MECHANICAL_STAGE.search(record["name"])),
            "sectionCommentCount": len(comments),
            "oversizedExpressionCount": len(long_expressions),
            "oversizedExpressionLines": [item["startLine"] for item in long_expressions[:8]],
        })
    return {"schema": "replaylab-shader-organization-quality/v1", "functions": functions,
            "anonymousFunctionCount": sum(bool(item["anonymousIdentifierCount"]) for item in functions),
            "mechanicalFunctionCount": sum(item["mechanicalStageName"] for item in functions),
            "policy": {"allExecutableFunctions": True, "semanticProof": False,
                       "appliesToIntentionalUserEdits": False,
                       "longFunctionCommentStatementThreshold": 80,
                       "oversizedExpressionTokenThreshold": 180}}


def organization_quality_issues(source: str) -> list[dict[str, str]]:
    issues = []
    for item in organization_quality(source)["functions"]:
        name = item["function"]
        location = "proposal:%d (%s)" % (item["line"], name)
        if item["anonymousIdentifierCount"]:
            issues.append(reason(
                "algorithm-anonymous-values",
                "%s still uses %d anonymous computation identifiers: %s" %
                (name, item["anonymousIdentifierCount"], ", ".join(item["anonymousIdentifiers"])),
                location=location,
                direction="Name each local/shared calculation value from its expression, input, component or downstream use. Use a neutral data role when domain intent is unknown; do not substitute temp123 or invent a material meaning. Preserve physical ABI names and layouts.",
                target="No anonymous decompiler locals or computation fields in " + name,
                example="float radialDistance = sqrt(x * x + y * y); // Geometric distance; effect identity remains unknown."))
        if item["mechanicalStageName"] and item["statementCount"] > 24:
            issues.append(reason(
                "algorithm-mechanical-partition", name + " is a numbered chunk, not a named computation.",
                location=location,
                direction="Identify complete data-dependency boundaries, name the actual operation, and extract shared pure helpers where the same calculation occurs. Do not split by a fixed line count or merely rename the chunk.",
                target="Cohesive calculation functions and inspectable internal sections.",
                example="DecodeLayerMask -> EvaluateDirectionalResponse -> CombineLayerColor, only when those operations are present."))
        if item["statementCount"] > 80 and item["sectionCommentCount"] < 2:
            issues.append(reason(
                "algorithm-long-function-sections", name + " has a long body without calculation-section explanations.",
                location=location,
                direction="Extract cohesive helpers or add concise section comments at real changes of data preparation, sampling, calculation and combination. Do not insert a comment every fixed number of lines.",
                target="Understandable internal calculation blocks in " + name,
                example="// Reconstruct the sampled tangent normal. ... // Transform it with the captured tangent basis."))
        if item["oversizedExpressionCount"]:
            issues.append(reason(
                "algorithm-dense-expressions", name + " contains very large expressions at lines " +
                ", ".join(map(str, item["oversizedExpressionLines"])),
                severity="warning", location=location,
                direction="Name coherent subexpressions or share proven helpers. Merge short single-use pure temporaries only when order, precise types, branches, sampling and derivatives stay unchanged. A precision-sensitive expression may remain with an explanation.",
                target="Readable expressions without unsafe reassociation.",
                example="Keep a named luminance/normal-length intermediate instead of expanding its entire dependency tree."))
    # Bound feedback for very large shaders; the contract still returns metrics
    # for all functions, so a model can repair the omitted instances as a batch.
    return sorted(issues, key=lambda item: item["severity"] != "blocker")[:64]
