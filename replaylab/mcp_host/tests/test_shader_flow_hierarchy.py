from __future__ import annotations

import ast
import copy
import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_shader_flow_hierarchy.py"
SPEC = importlib.util.spec_from_file_location("shader_hierarchy_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_index_is_python36_and_current_source_only():
    ast.parse(MODULE_PATH.read_text(encoding="utf-8"), feature_version=(3, 6))
    source = "float Square(float x) { return x*x; }\nfloat Main(float a) { float b=Square(a); return b; }"
    index = MODULE.SourceFlowHierarchy(source, {"documentId": "doc", "path": "source/shader.hlsl"})
    graph = index.function_graph(index.function_identity("Main"))
    call, result = graph["nodes"]
    assert call["functionId"] == index.function_identity("Square")
    assert call["source"]["symbol"] == "Main"
    assert call["source"]["snippet"] == "float b=Square(a);"
    assert call["source"]["documentId"] == "doc"
    assert call["source"]["statementId"]
    assert graph["edges"] == [{"source": call["id"], "target": result["id"], "kind": "sequence"}]
    assert index.function_graph(call["functionId"])["nodes"][0]["label"] == "return x*x;"


def test_root_preserves_accepted_stages_and_only_expands_real_unique_functions():
    index = MODULE.SourceFlowHierarchy("float Actual(float x) { return x*x; }")
    accepted = {"graphId": "accepted", "nodes": [
        {"id": "real", "name": "Actual", "label": "Lighting", "source": {"line": 1}},
        {"id": "fake", "name": "Missing", "label": "Actual", "functionId": "forged"}],
        "edges": []}
    before = copy.deepcopy(accepted)
    root = index.project_root(accepted)
    assert accepted == before
    assert root["graphId"] == "accepted"
    assert root["nodes"][0]["label"] == "Lighting"
    assert root["nodes"][0]["functionId"] == index.function_identity("Actual")
    assert root["nodes"][0]["source"]["symbol"] == "Actual"
    assert "functionId" not in root["nodes"][1]


def test_legacy_definition_graph_projects_entry_calculation_without_changing_artifact():
    index = MODULE.SourceFlowHierarchy("float H(float x){return x*x;} float Main(float x){return H(x);}")
    artifact = {"entryPoint": "Main", "graphId": "accepted-legacy", "nodes": [
        {"id": "definition-main", "name": "Main"}, {"id": "definition-h", "name": "H"}], "edges": []}
    before = copy.deepcopy(artifact)
    root = index.project_root(artifact)
    assert artifact == before
    assert len(root["nodes"]) == 1
    assert root["nodes"][0]["label"] == "return H(x);"
    assert root["nodes"][0]["functionId"] == index.function_identity("H")
    assert root["graphKind"] == "algorithm-dataflow"


def test_function_id_survives_body_edits_but_missing_signature_does_not_relocate():
    original = MODULE.SourceFlowHierarchy("float Work(float x) { return x; }")
    function_id = original.function_identity("Work")
    changed = MODULE.SourceFlowHierarchy("\n// moved\nfloat Work(float x) { float y=floor(x); return y*y; }")
    assert changed.function_identity("Work") == function_id
    assert changed.function_graph(function_id)["nodes"][0]["source"]["line"] == 3
    with pytest.raises(ValueError, match="missing or ambiguous"):
        MODULE.SourceFlowHierarchy("float Other(float x) { return x; }").function_graph(function_id)


def test_comments_strings_prototypes_and_member_calls_do_not_create_children():
    source = '''
#define FAKE(x) Fake(x)
float Forward(float x);
float Real(float x) { return x; }
float Main(float x) {
  // Real(x); Fake(x);
  float y = object.Real(x);
  return y;
}
'''
    index = MODULE.SourceFlowHierarchy(source)
    assert index.function_identity("Forward") is None
    assert index.function_identity("Fake") is None
    graph = index.function_graph(index.function_identity("Main"))
    assert not any(node.get("functionId") for node in graph["nodes"])


def test_overloads_are_not_guessed_from_a_call_name():
    source = "float F(float x){return x;} float2 F(float2 x){return x;} float Main(float x){return F(x);}"
    index = MODULE.SourceFlowHierarchy(source)
    assert index.function_identity("F") is None
    assert "functionId" not in index.function_graph(index.function_identity("Main"))["nodes"][0]


def test_duplicate_preprocessor_signatures_fail_closed():
    source = "#if A\nfloat F(float x){return x;}\n#else\nfloat F(float x){return -x;}\n#endif\n"
    index = MODULE.SourceFlowHierarchy(source)
    assert index.function_identity("F") is None
    with pytest.raises(ValueError):
        index.function_graph(next(iter(index.functions)))


def test_conditions_keep_mutually_exclusive_calculations_separate_and_join():
    index = MODULE.SourceFlowHierarchy("float F(float x) { float y=0; if(x>0){y=x*x;}else{y=-x;} return y; }")
    graph = index.function_graph(index.function_identity("F"))
    by_label = {node["label"]: node["id"] for node in graph["nodes"]}
    condition, true, false, output = [by_label[label] for label in ("if(x>0)", "y=x*x;", "y=-x;", "return y;")]
    edges = {(edge["source"], edge["target"], edge["kind"]) for edge in graph["edges"]}
    assert (condition, true, "true") in edges
    assert (condition, false, "false") in edges
    assert (true, output, "sequence") in edges
    assert (false, output, "sequence") in edges
    assert not any(source == true and target == false for source, target, kind in edges)


def test_unbraced_nested_if_and_return_preserve_real_branches():
    index = MODULE.SourceFlowHierarchy("float F(float x){if(x>0) if(x>1) return 1; else return 2; return 0;}")
    graph = index.function_graph(index.function_identity("F"))
    returns = {node["id"] for node in graph["nodes"] if node["kind"] == "return"}
    assert len(returns) == 3
    assert not any(edge["source"] in returns for edge in graph["edges"])


def test_straight_calculations_are_batched_but_math_and_return_are_visible():
    source = "float F(float x){" + "".join("x=x+%s;" % i for i in range(16)) + "return smoothstep(0,1,x);}"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("F"))
    assert len(graph["nodes"]) == 3
    assert "smoothstep" in graph["nodes"][-1]["label"]
    assert all(node["sourceRange"]["startLine"] == 1 for node in graph["nodes"])


def test_loop_break_does_not_link_back_to_predicate():
    index = MODULE.SourceFlowHierarchy("float F(float x){while(x>0){if(x>9) break; x=x-1;} return x;}")
    graph = index.function_graph(index.function_identity("F"))
    lookup = {node["label"]: node["id"] for node in graph["nodes"]}
    edges = {(edge["source"], edge["target"], edge["kind"]) for edge in graph["edges"]}
    assert (lookup["x=x-1;"], lookup["while(x>0)"], "loop") in edges
    assert (lookup["break;"], lookup["return x;"], "sequence") in edges
    assert not any(a == lookup["break;"] and b == lookup["while(x>0)"] for a, b, k in edges)


def test_target_graph_is_bounded_and_declares_incomplete_projection():
    source = "void H() {} void Main(){" + "H();" * 150 + "}"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("Main"))
    assert len(graph["nodes"]) == MODULE.MAX_LOCAL_NODES
    assert graph["truncated"]["nodes"] is True
    assert len({node["id"] for node in graph["nodes"]}) == len(graph["nodes"])


def test_invalid_current_source_cannot_return_an_old_function_graph():
    with pytest.raises(ValueError, match="unmatched"):
        MODULE.SourceFlowHierarchy("float F(float x){ return x;")


def test_local_declarations_are_not_algorithm_nodes_and_hlsl_attributes_keep_branches():
    source = "struct Data {float value;}; float F(float x){ Data d; float t; [branch] if(x>0){t=x;}else{t=-x;} return t;}"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("F"))
    labels = [node["label"] for node in graph["nodes"]]
    assert labels == ["if(x>0)", "t=x;", "t=-x;", "return t;"]
    assert {edge["kind"] for edge in graph["edges"]} >= {"true", "false"}


def test_inout_array_signature_and_attributed_loop_have_stable_function_identity():
    source = "struct Data{float value;}; void Update(inout Data d, float weights[4]) { [unroll] for(int i=0;i<4;i++){ if(weights[i]<0) continue; d.value += weights[i]; } }"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("Update"))
    labels = {node["label"]: node["id"] for node in graph["nodes"]}
    loop = labels["for(int i=0;i<4;i++)"]
    assert any(edge["source"] == labels["continue;"] and edge["target"] == loop
               and edge["kind"] == "loop" for edge in graph["edges"])
    assert "inout Data d" in graph["nodes"][0]["source"]["functionSignature"]


def test_switch_cases_are_one_honest_selection_not_false_linear_edges():
    source = "float H(float x){return x*x;} float F(float x){switch((int)x){case 1:return H(x); default:return 0;}}"
    index = MODULE.SourceFlowHierarchy(source)
    graph = index.function_graph(index.function_identity("F"))
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["kind"] == "selection"
    assert "functionId" not in graph["nodes"][0]
    assert graph["edges"] == []
