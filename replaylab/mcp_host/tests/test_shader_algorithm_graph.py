from __future__ import annotations

import copy
import hashlib

import pytest

from src.shader_workspace.equivalence import (
    declared_symbols, flow_value_bindings, flow_spec_template, validate_flow_spec,
    mermaid_from_flow_graph,
)
from src.shader_workspace.flow_graph import (
    _function_records, bind_computation_range, build_shader_flow_graph, computation_ranges,
)
from src.shader_workspace.templates import organization_contract, organization_structure_issues


SOURCE = """Texture2D<float4> albedo : register(t0);
SamplerState linearSampler : register(s0);
float4 SampleMaterial(float2 uv)
{
    float4 unused;
    return albedo.Sample(linearSampler, uv);
}
float4 MainPS(float2 uv : TEXCOORD0) : SV_Target0
{
    float4 sampled;
    // Sample then combine the existing contribution.
    sampled = SampleMaterial(uv);
    float4 result = sampled * 0.5;
    return result;
}
"""


def _validate(source, stages):
    functions = [record["name"] for record in _function_records(source)]
    spec = flow_spec_template(source, functions, "MainPS")
    spec["stages"] = stages
    symbols = declared_symbols(source)
    values = flow_value_bindings(source)
    return validate_flow_spec(spec, functions=functions,
                              resources=symbols["resources"] + symbols["cbuffers"],
                              outputs=symbols["outputs"], inputs=values["inputs"], fields=values["fields"], source=source)


def _stages():
    return [
        {"id": "sampling", "label": "Sample material", "function": "SampleMaterial", "sourceRange": {"startLine": 5, "endLine": 6},
         "reads": ["albedo", "uv"], "writes": ["SampleMaterial.return"], "next": ["prepare"]},
        {"id": "prepare", "label": "Receive material sample", "function": "MainPS", "sourceRange": {"startLine": 12, "endLine": 12},
         "reads": ["SampleMaterial.return", "uv"], "writes": ["sampled"], "next": ["combine"]},
        {"id": "combine", "label": "Scale color contribution", "function": "MainPS", "sourceRange": {"startLine": 13, "endLine": 13},
         "reads": ["sampled"], "writes": ["result"], "next": ["output"]},
        {"id": "output", "label": "Output color", "function": "MainPS", "sourceRange": {"startLine": 14, "endLine": 14},
         "reads": ["result"], "writes": ["SV_Target0"], "next": []},
    ]


def _graph(source, spec):
    return build_shader_flow_graph(source, workspace_id="1" * 32, source_path="source/shader.hlsl",
                                   source_sha256=hashlib.sha256(source.encode()).hexdigest(), source_revision=2,
                                   stage="fragment", entry_point="MainPS", generated_at="2026-09-05T00:00:00Z",
                                   origin="organized-proposal", flow_spec=spec)


def test_algorithm_stages_survive_into_navigation_graph_and_mermaid():
    spec, issues = _validate(SOURCE, _stages())
    assert issues == []
    graph = _graph(SOURCE, spec)
    assert graph["graphKind"] == "algorithm-dataflow" and graph["layoutDirection"] == "TD"
    assert [node["label"] for node in graph["nodes"]] == [stage["label"] for stage in _stages()]
    assert [node["source"]["line"] for node in graph["nodes"]] == [6, 12, 13, 14]
    assert graph["nodes"][0]["sourceRange"] == {"startLine": 6, "endLine": 6}
    assert [edge["values"] for edge in graph["edges"]] == [["SampleMaterial.return"], ["sampled"], ["result"]]
    diagram = mermaid_from_flow_graph(graph)
    assert diagram.startswith("flowchart TD")
    assert "Sample material" in diagram and "sampled" in diagram
    assert "subgraph RES" not in diagram
    assert graph["policy"]["applyAuthorization"] is False


@pytest.mark.parametrize("span", [{"startLine": 1, "endLine": 2}, {"startLine": 3, "endLine": 3}, {"startLine": 5, "endLine": 5}])
def test_parameters_resources_and_uninitialized_declarations_cannot_be_nodes(span):
    stages = _stages()
    stages[0]["sourceRange"] = span
    _, issues = _validate(SOURCE, stages)
    assert "flow-spec-source-range" in {issue["code"] for issue in issues}


def test_invented_data_flow_and_backwards_edges_are_rejected():
    stages = _stages()
    stages[0]["reads"] = ["result"]  # Declared, but not read by SampleMaterial.
    stages[1]["writes"] = ["result"]  # Exists elsewhere, not assigned here.
    stages[-1]["next"] = ["sampling"]
    _, issues = _validate(SOURCE, stages)
    codes = {issue["code"] for issue in issues}
    assert "flow-spec-value-binding" in codes and "flow-spec-order" in codes


def test_same_named_locals_in_different_functions_do_not_create_fake_data_edges():
    source = "float A() { float value = 1; return value; }\nfloat MainPS() : SV_Target { float value = 2; return value; }\n"
    spec, issues = _validate(source, [
        {"id": "first", "label": "First computation", "function": "A", "reads": [], "writes": ["value"], "next": ["second"]},
        {"id": "second", "label": "Second computation", "function": "MainPS", "reads": ["value"], "writes": ["SV_Target"], "next": []},
    ])
    assert issues == []
    assert _graph(source, spec)["edges"] == [{"source": "stage-first", "target": "stage-second", "kind": "control"}]


def test_long_comment_only_body_is_incomplete_but_simple_shader_needs_no_artificial_lighting():
    body = "\n".join("    _1 = _1 + 0.0;" for _ in range(180))
    baseline = "float4 MainPS() : SV_Target {\nfloat4 _1 = 1;\n" + body + "\nreturn _1;\n}\n"
    contract = organization_contract(baseline)
    assert contract["needsAlgorithmRestructuring"] is True
    issues = organization_structure_issues(baseline, "// Pretty comments\n" + baseline, {})
    assert "algorithm-organization-incomplete" in {item["code"] for item in issues}
    assert all(item["severity"] == "blocker" and item["direction"] and item["target"] for item in issues)
    simple = "float4 MainPS() : SV_Target { return 1; }"
    assert organization_structure_issues(simple, simple, {}) == []


def test_cohesive_function_extraction_satisfies_structure_without_claiming_semantics():
    operations = ["    value = value + 0.0;" for _ in range(180)]
    for index in (0, 45, 90, 135):
        operations[index] = "    // Accumulate this bounded input contribution.\n" + operations[index]
    baseline = "float4 MainPS() : SV_Target {\nfloat4 value = 1;\n" + "\n".join(operations) + "\nreturn value;\n}"
    proposal = ("float4 First(float4 value) {\n" + "\n".join(operations[:90]) + "\nreturn value;\n}\n"
                + "float4 Second(float4 value) {\n" + "\n".join(operations[90:]) + "\nreturn value;\n}\n"
                + "float4 MainPS() : SV_Target { return Second(First(1)); }\n")
    stages = [{"id": name.lower(), "label": name + " computation", "function": name, "reads": [], "writes": [], "next": []}
              for name in ("First", "Second", "MainPS")]
    spec, issues = _validate(proposal, stages)
    assert issues == []
    assert organization_structure_issues(baseline, proposal, spec) == []
    assert max(item["statementCount"] for item in computation_ranges(proposal)) < 160


def test_inout_struct_fields_follow_the_actual_unique_call_chain():
    source = """struct FragmentData { float inputValue; float color; };
void Prepare(inout FragmentData data) { data.inputValue = 0.5; }
void ComputePart(inout FragmentData nested) { nested.color = nested.inputValue * 2; }
void Compute(inout FragmentData data) { ComputePart(data); }
float MainPS() : SV_Target { FragmentData data; Prepare(data); Compute(data); return data.color; }
"""
    stages = [
        {"id": "prepare", "label": "Prepare input", "function": "Prepare", "reads": [], "writes": ["data.inputValue"], "next": ["compute"]},
        {"id": "compute", "label": "Compute color", "function": "Compute", "reads": ["data.inputValue"], "writes": ["data.color"], "next": ["output"]},
        {"id": "output", "label": "Return color", "function": "MainPS", "reads": ["data.color"], "writes": ["SV_Target"], "next": []},
    ]
    spec, issues = _validate(source, stages)
    assert issues == []
    edges = _graph(source, spec)["edges"]
    assert edges[0]["kind"] == "data" and edges[0]["values"] == ["data.inputValue"]
    assert edges[1]["kind"] == "data" and edges[1]["values"] == ["data.color"]
    # Same parameter spelling does not bind two independent actual arguments.
    unrelated = source.replace("Prepare(data); Compute(data);", "Prepare(data); FragmentData other; Compute(other);")
    spec, issues = _validate(unrelated, stages)
    assert issues == []
    assert all(edge["kind"] == "control" for edge in _graph(unrelated, spec)["edges"])


def test_ambiguous_multiple_calls_do_not_invent_inout_data_identity():
    source = """struct FragmentData { float value; };
void Prepare(inout FragmentData data) { data.value = 1; }
void Consume(inout FragmentData data) { data.value = data.value * 2; }
float MainPS() : SV_Target { FragmentData data; Prepare(data); Prepare(data); Consume(data); return data.value; }
"""
    stages = [
        {"id": "prepare", "label": "Prepare", "function": "Prepare", "reads": [], "writes": ["data.value"], "next": ["consume"]},
        {"id": "consume", "label": "Consume", "function": "Consume", "reads": ["data.value"], "writes": ["data.value"], "next": []},
    ]
    spec, issues = _validate(source, stages)
    assert issues == []
    assert _graph(source, spec)["edges"][0]["kind"] == "control"


def test_template_omits_large_local_inventory_without_truncating_validation():
    source = "float4 MainPS() : SV_Target {\n" + "\n".join("float _%d = %d;" % (i, i) for i in range(300)) + "\nreturn _299;\n}"
    template = flow_spec_template(source, ["MainPS"], "MainPS")
    assert len(template["allowedInputs"]) <= 32 and "_299" not in template["allowedInputs"]
    assert len(template["allowedFields"]) <= 32 and len(template["stages"]) == 1
    spec, issues = _validate(source, [{"id": "output", "label": "Return final value", "function": "MainPS",
                                      "sourceRange": {"startLine": 302, "endLine": 302},
                                      "reads": ["_299"], "writes": ["SV_Target"], "next": []}])
    assert issues == []


def test_shadowed_actual_structs_do_not_connect_and_void_has_no_return_value():
    source = """struct FragmentData { float value; };
void Prepare(inout FragmentData data) { data.value = 1; return; }
void Consume(inout FragmentData data) { data.value = data.value * 2; }
float MainPS() : SV_Target { { FragmentData data; Prepare(data); } { FragmentData data; Consume(data); } return 1; }
"""
    stages = [
        {"id": "prepare", "label": "Prepare", "function": "Prepare", "reads": [], "writes": ["data.value"], "next": ["consume"]},
        {"id": "consume", "label": "Consume", "function": "Consume", "reads": ["data.value"], "writes": ["data.value"], "next": []},
    ]
    spec, issues = _validate(source, stages)
    assert issues == []
    assert _graph(source, spec)["edges"][0]["kind"] == "control"
    stages[0]["writes"] = ["Prepare.return"]
    _, issues = _validate(source, stages)
    assert "flow-spec-value-binding" in {issue["code"] for issue in issues}
