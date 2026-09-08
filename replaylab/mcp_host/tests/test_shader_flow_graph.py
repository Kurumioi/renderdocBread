from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from src.shader_workspace.flow_graph import (
    FLOW_GRAPH_SCHEMA,
    MAX_GRAPH_NODES,
    MODULE_DESCRIPTIONS_SCHEMA,
    ShaderFlowGraphError,
    build_shader_flow_graph,
    load_workspace_flow_graph_status,
    module_description_template,
    validate_module_descriptions,
    validate_shader_flow_graph,
)


class ShaderFlowGraphTests(unittest.TestCase):
    WORKSPACE_ID = "1" * 32

    @staticmethod
    def _build(source: str, **overrides):
        arguments = {
            "workspace_id": ShaderFlowGraphTests.WORKSPACE_ID,
            "source_path": "source/shader.hlsl",
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "source_revision": 7,
            "stage": "fragment",
            "entry_point": "MainPS",
            "generated_at": "2026-08-31T00:00:00Z",
            "origin": "organized-proposal",
            "descriptions": {"ApplyLighting": "Evaluates the lighting contribution."},
        }
        arguments.update(overrides)
        return build_shader_flow_graph(source, **arguments)

    def test_extracts_calls_locations_and_bounded_ai_descriptions(self) -> None:
        source = (
            "#define PretendCall(x) Ignored(x)\n"
            "// float4 CommentedOut() { return 0; }\n"
            "Texture2D<float4> Tex : register(t0);\n"
            "float4 SampleBase(float2 uv) { return Tex.Sample(Samp, uv); }\n"
            "float3 ApplyLighting(float3 value) { return value; }\n"
            "[shader(\"pixel\")]\n"
            "float4 MainPS(float2 uv : TEXCOORD0) : SV_Target0\n"
            "{\n"
            "  return float4(ApplyLighting(SampleBase(uv).rgb), 1.0);\n"
            "}\n"
        )

        graph = self._build(source)

        self.assertEqual(graph["schema"], FLOW_GRAPH_SCHEMA)
        self.assertEqual(
            [node["name"] for node in graph["nodes"]],
            ["MainPS", "ApplyLighting", "SampleBase"],
        )
        by_name = {node["name"]: node for node in graph["nodes"]}
        self.assertEqual(by_name["MainPS"]["kind"], "entry-point")
        self.assertEqual(by_name["MainPS"]["source"]["line"], 9)
        self.assertEqual(by_name["MainPS"]["source"]["column"], 3)
        self.assertEqual(
            by_name["ApplyLighting"]["description"],
            "Evaluates the lighting contribution.",
        )
        edges = {
            (edge["source"], edge["target"])
            for edge in graph["edges"]
        }
        self.assertEqual(
            edges,
            {
                (by_name["MainPS"]["id"], by_name["ApplyLighting"]["id"]),
                (by_name["MainPS"]["id"], by_name["SampleBase"]["id"]),
            },
        )
        self.assertTrue(graph["policy"]["navigationOnly"])
        self.assertFalse(graph["policy"]["compileAuthorization"])
        self.assertFalse(graph["policy"]["applyAuthorization"])

    def test_graph_id_ignores_generation_time_but_binds_source_revision(self) -> None:
        source = "float4 MainPS() : SV_Target { return 1.0; }\n"
        first = self._build(source)
        second = self._build(
            source, generated_at="2026-08-31T01:00:00Z"
        )
        changed_revision = self._build(source, source_revision=8)

        self.assertEqual(first["graphId"], second["graphId"])
        self.assertNotEqual(first["graphId"], changed_revision["graphId"])

    def test_macro_declared_entry_still_has_a_navigation_node(self) -> None:
        source = (
            "#define DECLARE_PIXEL(name) float4 name() : SV_Target { return 1; }\n"
            "DECLARE_PIXEL(MainPS)\n"
        )

        graph = self._build(source)

        self.assertEqual(len(graph["nodes"]), 1)
        self.assertEqual(graph["nodes"][0]["name"], "MainPS")
        self.assertEqual(graph["nodes"][0]["kind"], "entry-point")
        self.assertEqual(graph["nodes"][0]["source"]["line"], 2)

    def test_description_template_and_validation_are_plain_and_bounded(self) -> None:
        template = module_description_template(
            "float Helper() { return 1; }\n"
            "float4 MainPS() : SV_Target { return Helper(); }\n"
        )
        self.assertEqual(template["schema"], MODULE_DESCRIPTIONS_SCHEMA)
        self.assertEqual(
            [item["name"] for item in template["modules"]],
            ["Helper", "MainPS"],
        )
        template["modules"][0]["description"] = "Computes an intermediate value."
        self.assertEqual(
            validate_module_descriptions(template)["Helper"],
            "Computes an intermediate value.",
        )
        invalid = copy.deepcopy(template)
        invalid["modules"][0]["description"] = "two\nlines"
        with self.assertRaisesRegex(ShaderFlowGraphError, "single-line"):
            validate_module_descriptions(invalid)

    def test_node_limit_is_explicit_and_entry_point_is_retained(self) -> None:
        helpers = "".join(
            "float Helper%d() { return %d.0; }\n" % (index, index)
            for index in range(MAX_GRAPH_NODES + 8)
        )
        source = helpers + "float4 MainPS() : SV_Target { return Helper0(); }\n"

        graph = self._build(source)

        self.assertEqual(len(graph["nodes"]), MAX_GRAPH_NODES)
        self.assertEqual(graph["nodes"][0]["name"], "MainPS")
        self.assertTrue(graph["truncated"]["nodes"])
        self.assertEqual(graph["truncated"]["omittedNodes"], 9)

    def test_validation_rejects_path_escape_and_content_tamper(self) -> None:
        source = "float4 MainPS() : SV_Target { return 1.0; }\n"
        graph = self._build(source)
        escaped = copy.deepcopy(graph)
        escaped["nodes"][0]["source"]["path"] = "../outside.hlsl"
        with self.assertRaisesRegex(ShaderFlowGraphError, "within"):
            validate_shader_flow_graph(escaped)

        tampered = copy.deepcopy(graph)
        tampered["nodes"][0]["description"] = "Unbound changed text."
        with self.assertRaisesRegex(ShaderFlowGraphError, "graphId"):
            validate_shader_flow_graph(tampered)

    def test_workspace_projection_marks_source_changes_stale(self) -> None:
        source = "float4 MainPS() : SV_Target { return 1.0; }\n"
        graph = self._build(source)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ai").mkdir()
            (root / "ai" / "flow-graph.json").write_text(
                json.dumps(graph), encoding="utf-8"
            )

            current = load_workspace_flow_graph_status(
                root,
                workspace_id=self.WORKSPACE_ID,
                source_sha256=graph["sourceSha256"],
                source_revision=7,
                source_path="source/shader.hlsl",
            )
            stale = load_workspace_flow_graph_status(
                root,
                workspace_id=self.WORKSPACE_ID,
                source_sha256="2" * 64,
                source_revision=8,
                source_path="source/shader.hlsl",
            )

        self.assertTrue(current["available"])
        self.assertEqual(current["status"], "current")
        self.assertFalse(current["stale"])
        self.assertEqual(stale["status"], "stale")
        self.assertTrue(stale["stale"])


if __name__ == "__main__":
    unittest.main()
