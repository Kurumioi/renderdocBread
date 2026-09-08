"""Rename-only equivalence, Host-applied renames, review rounds and verdicts."""

from __future__ import annotations

import json
import os
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from src.shader_workspace import equivalence as eq
from src.shader_workspace.templates import analysis_markdown_issues

BASELINE = """cbuffer cb0 : register(b0) { float4 v0 : packoffset(c0); float4x4 m0; };
Texture2D<float4> t0 : register(t0);
SamplerState s0 : register( s0 );
struct PSIn { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
float3 f0(float3 a, float b) { return a * b + v0.xyz; }
float4 MainPS(PSIn i) : SV_Target0 {
  float4 c = t0.Sample(s0, i.uv);
  return float4(f0(c.rgb, v0.w), c.a);
}
"""
RENAMES = {"cb0": "MaterialCB", "v0": "tintAndAlpha", "t0": "tAlbedo", "s0": "sLinear", "f0": "applyTint", "uv": "texcoord"}


class RenameTests(unittest.TestCase):
    def test_host_applied_renames_keep_registers_and_are_rename_only_equivalent(self) -> None:
        clean = eq.validate_renames(BASELINE, RENAMES)
        proposal = eq.apply_renames(BASELINE, clean)
        self.assertIn("Texture2D<float4> tAlbedo : register(t0);", proposal)
        self.assertIn("SamplerState sLinear : register( s0 );", proposal)
        self.assertIn("float4 tintAndAlpha : packoffset(c0);", proposal)
        self.assertIn("tAlbedo.Sample(sLinear, i.texcoord)", proposal)
        proposal, missing = eq.insert_function_comments(
            proposal, {"applyTint": "Multiplies the sample by the tint.", "MainPS": "Entry point.", "ghost": "x"}
        )
        self.assertEqual(missing, ["ghost"])
        self.assertIn("// [ai] Multiplies the sample by the tint.\nfloat3 applyTint(", proposal)
        self.assertTrue(eq.rename_only_equivalent(BASELINE, proposal, clean)["equal"])
        # idempotent comments
        again, _ = eq.insert_function_comments(proposal, {"MainPS": "Entry point (v2)."})
        self.assertEqual(again.count("// [ai] Entry point"), 1)

    def test_unsafe_renames_are_refused_by_name(self) -> None:
        for renames, fragment in (
            ({"Sample": "x"}, "cannot be renamed"),
            ({"v0": "float4"}, "reserved HLSL word"),
            ({"v0": "t0"}, "reserved HLSL word"),
            ({"v0": "m0"}, "already exists"),
            ({"zzz": "y"}, "does not occur"),
            ({"v0": "SV_Target"}, "reserved HLSL word"),
            ({"v0": "newName", "m0": "newName"}, "both map to"),
        ):
            with self.assertRaises(eq.EquivalenceError) as raised:
                eq.validate_renames(BASELINE, renames)
            self.assertIn(fragment, str(raised.exception))

    def test_code_change_is_located_with_a_direction(self) -> None:
        clean = eq.validate_renames(BASELINE, RENAMES)
        proposal = eq.apply_renames(BASELINE, clean).replace("a * b", "b * a")
        result = eq.rename_only_equivalent(BASELINE, proposal, clean)
        self.assertFalse(result["equal"])
        first = result["differences"][0]
        self.assertEqual(first["baselineLine"], 5)
        self.assertIn("a * b", first["baseline"])
        self.assertIn("b * a", first["proposal"])
        reasons = eq.differences_to_reasons(result["differences"])
        self.assertEqual(reasons[0]["code"], "code-changed")
        self.assertEqual(reasons[0]["location"], "proposal.hlsl:5")
        self.assertIn("rename_shader_workspace_symbols", reasons[0]["direction"])

    def test_comments_and_whitespace_never_count_as_changes(self) -> None:
        proposal = "// header\n" + BASELINE.replace("{ return", "{\n    return /* tint */")
        self.assertTrue(eq.rename_only_equivalent(BASELINE, proposal)["equal"])


class RoundsAndVerdictTests(unittest.TestCase):
    def test_rounds_from_env_config_and_default(self) -> None:
        with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "5"}):
            self.assertEqual(eq.max_rounds(), 5)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "ReplayLab" / "config" / "review.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"maxRounds": 2, "shader-organization": {"maxRounds": 4}}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "", "LOCALAPPDATA": tmp}):
                self.assertEqual(eq.max_rounds("shader-organization"), 4)
                self.assertEqual(eq.max_rounds("other"), 2)
        with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "nonsense"}):
            self.assertEqual(eq.max_rounds(), eq.DEFAULT_MAX_ROUNDS)

    def test_rounds_persist_and_soft_accept_after_the_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "2"}):
            rounds = eq.ReviewRounds(Path(tmp))
            first = rounds.record(accepted=False, reasons=[eq.reason("code-changed", "x")])
            self.assertEqual(first, 1)
            self.assertFalse(rounds.exhausted(first))
            second = eq.ReviewRounds(Path(tmp)).record(accepted=False, reasons=[])
            self.assertEqual(second, 2)
            self.assertTrue(eq.ReviewRounds(Path(tmp)).exhausted(second))
            verdict = eq.build_verdict(accepted=False, level="rejected", round_used=2, round_limit=2, soft_accepted=True)
            self.assertTrue(verdict["softAccepted"])
            self.assertIn("baseline", verdict["nextStep"])
            # Exhaustion cannot be reset by a late concurrent success.
            with self.assertRaisesRegex(eq.EquivalenceError, "exhausted"):
                eq.ReviewRounds(Path(tmp)).record(accepted=True, reasons=[])
        with tempfile.TemporaryDirectory() as tmp:
            eq.ReviewRounds(Path(tmp)).record(accepted=True, reasons=[])
            self.assertEqual(eq.ReviewRounds(Path(tmp)).used, 0)
            payload = json.loads((Path(tmp) / "ai" / eq.ROUNDS_FILE).read_text(encoding="utf-8"))
            self.assertTrue(payload["shader-organization"]["lastAccepted"])

    def test_template_errors_never_consume_content_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "2"}):
            rounds = eq.ReviewRounds(Path(tmp))
            for _ in range(5):
                errors, limit = rounds.record_template_error([eq.reason("analysis-schema", "bad fields")])
            self.assertEqual((errors, limit), (5, 6))
            self.assertEqual(eq.ReviewRounds(Path(tmp)).used, 0)
            self.assertFalse(rounds.template_exhausted())
            rounds.record_template_error([])
            self.assertTrue(eq.ReviewRounds(Path(tmp)).template_exhausted())

    def test_exhausted_verdict_carries_the_user_decision(self) -> None:
        verdict = eq.build_verdict(accepted=False, level="content-rejected", round_used=3, round_limit=3,
                                   reasons=[eq.reason("algorithm-anonymous-values", "__replaylab_* remain")],
                                   workspace_path="D:/p/ws")
        self.assertTrue(verdict["budgetExhausted"])
        decision = verdict["userDecision"]
        self.assertTrue(decision["required"])
        self.assertEqual(decision["tool"], "decide_shader_organization_review_budget")
        self.assertEqual([o["id"] for o in decision["options"]], ["extend", "publish-baseline", "stop"])
        self.assertEqual(decision["toolArguments"]["workspace_path"], "D:/p/ws")
        self.assertIn("decide_shader_organization_review_budget", verdict["nextStep"])
        # not exhausted -> no question
        self.assertNotIn("userDecision", eq.build_verdict(accepted=False, level="x", round_used=1, round_limit=3))

    def test_user_grant_extends_rounds_and_soft_publish_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "2"}):
            rounds = eq.ReviewRounds(Path(tmp))
            rounds.record(accepted=False, reasons=[])
            rounds.record(accepted=False, reasons=[])
            self.assertTrue(rounds.exhausted(rounds.used))
            state = eq.ReviewRounds(Path(tmp)).grant(extra_rounds=3, soft_publish_allowed=False, note="用户：再来3轮")
            self.assertEqual((state["maxRounds"], state["roundsLeft"]), (5, 3))
            again = eq.ReviewRounds(Path(tmp))
            self.assertEqual(again.limit, 5)
            self.assertFalse(again.user_soft_publish_allowed)
            self.assertEqual(again.record(accepted=False, reasons=[]), 3)
            eq.ReviewRounds(Path(tmp)).grant(extra_rounds=1, soft_publish_allowed=True, note="原样入库")
            self.assertTrue(eq.ReviewRounds(Path(tmp)).user_soft_publish_allowed)
            eq.ReviewRounds(Path(tmp)).grant(extra_rounds=1, soft_publish_allowed=False, note="3")
            with self.assertRaisesRegex(eq.EquivalenceError, "budget grants"):
                eq.ReviewRounds(Path(tmp)).grant(extra_rounds=1, soft_publish_allowed=False, note="4")
            # an accepted review closes the chapter: extension and consent are dropped
            eq.ReviewRounds(Path(tmp)).record(accepted=True, reasons=[])
            fresh = eq.ReviewRounds(Path(tmp))
            self.assertEqual(fresh.limit, 2)
            self.assertFalse(fresh.user_soft_publish_allowed)

    def test_verdict_shape_for_rejection(self) -> None:
        verdict = eq.build_verdict(
            accepted=False, level="rejected", round_used=1, round_limit=3,
            reasons=[eq.reason("code-changed", "line 5 differs", location="proposal.hlsl:5", direction="revert")],
        )
        self.assertEqual(verdict["schema"], eq.REVIEW_VERDICT_SCHEMA)
        self.assertEqual(verdict["roundsLeft"], 2)
        self.assertEqual(verdict["reasons"][0]["direction"], "revert")
        self.assertIn("2 round(s) left", verdict["nextStep"])

    def test_corrupt_or_negative_existing_state_never_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ai" / eq.ROUNDS_FILE
            path.parent.mkdir()
            for content in ("{", "[]", '{"shader-organization":{"rounds":-1}}',
                            '{"shader-organization":{"rounds":true}}', '{"shader-organization":{"rounds":"2"}}'):
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(eq.EquivalenceError):
                    eq.ReviewRounds(Path(tmp))
                self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_redirected_state_is_not_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ai" / eq.ROUNDS_FILE
            path.parent.mkdir()
            target = Path(tmp) / "actual.json"
            target.write_text("{}", encoding="utf-8")
            os.link(target, path)
            with self.assertRaisesRegex(eq.EquivalenceError, "redirected"):
                eq.ReviewRounds(Path(tmp))
            self.assertEqual(target.read_text(encoding="utf-8"), "{}")

    def test_stale_instances_and_separate_processes_increment_one_counter(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "10"}):
            first, stale = eq.ReviewRounds(Path(tmp)), eq.ReviewRounds(Path(tmp))
            self.assertEqual(first.record(accepted=False, reasons=[]), 1)
            self.assertEqual(stale.record(accepted=False, reasons=[]), 2)
            code = ("import sys; from pathlib import Path; from src.shader_workspace.equivalence import ReviewRounds; "
                    "ReviewRounds(Path(sys.argv[1])).record(accepted=False,reasons=[])")
            processes = [subprocess.Popen([sys.executable, "-c", code, tmp],
                         cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
            self.assertEqual(first.used, 6)
            first.record(accepted=True, reasons=[])
            self.assertEqual(stale.used, 0)


class DiagramAndAnalysisTests(unittest.TestCase):
    def test_mermaid_is_deterministic_and_safe(self) -> None:
        graph = {
            "entryPoint": "MainPS",
            "nodes": [
                {"id": "a", "name": "MainPS", "kind": "entry-point", "description": "entry"},
                {"id": "b", "name": "applyTint", "kind": "function", "description": 'tint "x" <b>'},
            ],
            "edges": [{"source": "a", "target": "b"}],
        }
        text = eq.mermaid_from_flow_graph(graph)
        self.assertTrue(text.startswith("flowchart TD\n"))
        self.assertIn('n0["MainPS — entry"]', text)
        self.assertIn("n0 --> n1", text)
        self.assertNotIn("<b>", text)
        self.assertEqual(text, eq.mermaid_from_flow_graph(graph))
        self.assertIn("classDef entry", eq.mermaid_from_flow_graph({"nodes": [], "edges": []}))

    def test_analysis_placeholders_are_not_valid_content(self) -> None:
        self.assertTrue(analysis_markdown_issues(eq.ANALYSIS_TEMPLATE))
        self.assertFalse(hasattr(eq, "validate_analysis"))


class FlowSpecTests(unittest.TestCase):
    SOURCE = """cbuffer MaterialCB : register(b0) { float4 tint; };
Texture2D<float4> tAlbedo : register(t0);
SamplerState sLinear : register(s0);
float3 applyTint(float3 a) { return a * tint.rgb; }
float4 MainPS(float2 uv : TEXCOORD0) : SV_Target0 { return float4(applyTint(tAlbedo.Sample(sLinear, uv).rgb), 1); }
"""

    def test_template_lists_declared_symbols_and_functions(self) -> None:
        symbols = eq.declared_symbols(self.SOURCE)
        self.assertEqual(symbols, {"resources": ["tAlbedo", "sLinear"], "cbuffers": ["MaterialCB"], "outputs": ["SV_Target0"]})
        template = eq.flow_spec_template(self.SOURCE, ["applyTint", "MainPS"], "MainPS")
        self.assertEqual(template["schema"], eq.FLOW_SPEC_SCHEMA)
        self.assertEqual([s["function"] for s in template["stages"]], ["MainPS"])
        self.assertEqual(template["stages"][0]["next"], [])
        self.assertEqual([item["function"] for item in template["computationRanges"]], ["applyTint", "MainPS"])
        self.assertIn("MaterialCB", template["resources"])

    def test_valid_spec_renders_fixed_layout_and_invalid_spec_is_explained(self) -> None:
        spec = {
            "schema": eq.FLOW_SPEC_SCHEMA,
            "inputs": ["uv"],
            "resources": ["tAlbedo", "sLinear", "MaterialCB"],
            "outputs": ["SV_Target0"],
            "stages": [
                {"id": "sample", "label": "Sample albedo at uv", "function": "MainPS", "reads": ["uv", "tAlbedo", "sLinear"], "next": ["tint"]},
                {"id": "tint", "label": "Multiply by material tint", "function": "applyTint", "reads": ["MaterialCB.tint"], "writes": ["SV_Target0"]},
            ],
        }
        bindings = eq.flow_value_bindings(self.SOURCE)
        clean, reasons = eq.validate_flow_spec(spec, functions=["applyTint", "MainPS"], resources=["tAlbedo", "sLinear", "MaterialCB"], outputs=["SV_Target0"], inputs=bindings["inputs"], fields=bindings["fields"])
        self.assertEqual(reasons, [])
        text = eq.mermaid_from_flow_spec(clean)
        self.assertTrue(text.startswith("flowchart TD\n"))
        for fragment in ("subgraph IN[Inputs]", "subgraph RES[Resources]", "subgraph FLOW[Computation]", "subgraph OUT[Outputs]", "==> ", "MaterialCB.tint"):
            self.assertIn(fragment, text)
        self.assertNotIn("<", text)
        self.assertEqual(text, eq.mermaid_from_flow_spec(clean))
        bad = dict(spec, stages=[
            {"id": "Bad Id", "label": "x"},
            {"id": "ok", "label": 'has "quotes"', "function": "MainPS"},
            {"id": "ghost", "label": "fine", "function": "nope", "next": ["missing"]},
            {"id": "w", "label": "writes wrong", "function": "MainPS", "writes": ["SV_Target7"], "reads": ["tNotDeclared"]},
        ])
        _clean, reasons = eq.validate_flow_spec(bad, functions=["applyTint", "MainPS"], resources=["tAlbedo"], outputs=["SV_Target0"])
        codes = [r["code"] for r in reasons]
        for code in ("flow-spec-id", "flow-spec-text", "flow-spec-function", "flow-spec-output", "flow-spec-read"):
            self.assertIn(code, codes)
        self.assertTrue(all(r["direction"] for r in reasons))


if __name__ == "__main__":
    unittest.main()
