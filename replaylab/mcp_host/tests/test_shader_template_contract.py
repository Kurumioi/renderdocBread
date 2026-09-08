from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from src import server
from src.shader_workspace import equivalence as eq
from src.shader_workspace.organization import ShaderOrganizationError
from src.shader_workspace.templates import analysis_template, render_analysis_fields, analysis_markdown_issues
import test_shader_organization as fixtures


class ShaderTemplateContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ShaderOrganizationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.env = mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "3"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, opened, kind, content):
        with mock.patch.object(server, "_shader_workspace_service", return_value=self.fixture.service):
            return json.loads(server.write_shader_workspace_organization_file(
                str(self.fixture.root), opened["organizationId"], kind,
                content if isinstance(content, str) else json.dumps(content)))

    def valid_analysis(self):
        value = analysis_template()
        value.update(overview="The source returns one color target.", computation=["Return the entry point result."], outputs=["SV_Target"],
                     observedFacts=[{"claim": "A color output is declared.", "evidence": "source:MainPS SV_Target"}], inferences=[])
        return value

    def test_begin_exposes_real_templates_but_placeholders_are_not_accepted(self):
        opened = self.fixture.manager.begin(str(self.fixture.root), {})
        self.assertEqual(opened["analysisTemplate"], analysis_template())
        self.assertIn("modules", opened["moduleDescriptionsTemplate"])
        self.assertTrue(opened["templateContract"]["modelFieldsOnly"])
        attempts = self.fixture.service.revision
        result = self.fixture.manager.review(str(self.fixture.root), opened["organizationId"])
        self.assertFalse(result["verdict"]["accepted"])
        self.assertFalse(result["verdict"]["softAccepted"])
        self.assertTrue(any(r["code"] == "analysis-empty" for r in result["verdict"]["reasons"]))
        self.assertEqual(self.fixture.service.revision, attempts)

    def test_analysis_fields_render_fixed_markdown_and_reject_missing_evidence(self):
        value = self.valid_analysis()
        text, issues = render_analysis_fields(value)
        self.assertEqual(issues, [])
        self.assertEqual(analysis_markdown_issues(text), [])
        self.assertIn("[evidence: source:MainPS SV", text)
        value["observedFacts"][0]["evidence"] = ""
        _, issues = render_analysis_fields(value)
        self.assertTrue(any(r["location"].endswith(".evidence") for r in issues))
        self.assertTrue(all(r["severity"] == "blocker" for r in issues))

    def test_markdown_migration_failure_counts_as_template_error_not_content_round(self):
        opened = self.fixture._begin()
        old = Path(opened["analysisPath"]).read_bytes()
        with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "2"}):
            failed = self.write(opened, "analysis", "# Existing historical Markdown\n")
            self.assertIn("JSON fields", failed["error"])
            # Format slips are the AI's own output errors: separate counter, no content round spent.
            self.assertEqual(failed["verdict"]["round"], 0)
            self.assertEqual(failed["verdict"]["templateErrors"]["count"], 1)
            self.assertEqual(failed["verdict"]["templateErrors"]["limit"], 6)
            self.assertFalse(failed["verdict"]["budgetExhausted"])
            self.assertIn("format attempt(s) left", failed["verdict"]["nextStep"])
            self.assertEqual(failed["verdict"]["reasons"][0]["target"], "analysisTemplate")
            self.assertEqual(Path(opened["analysisPath"]).read_bytes(), old)
            for _ in range(5):
                failed_again = self.write(opened, "analysis", "{")
            self.assertEqual(failed_again["verdict"]["round"], 0)  # content rounds untouched
            self.assertTrue(failed_again["verdict"]["budgetExhausted"])
            self.assertTrue(failed_again["verdict"]["templateErrors"]["exhausted"])
            self.assertTrue(failed_again["verdict"]["userDecision"]["required"])
            self.assertEqual(failed_again["verdict"]["userDecision"]["tool"], "decide_shader_organization_review_budget")
            # further writes are refused without counting anything more
            stopped = self.write(opened, "analysis", "{")
            self.assertEqual(stopped["verdict"]["templateErrors"]["count"], 6)
            self.assertEqual(eq.ReviewRounds(self.fixture.root).used, 0)

    def test_draft_mode_accumulates_reports_all_issues_and_counts_nothing(self):
        opened = self.fixture._begin()
        with mock.patch.object(server, "_shader_workspace_service", return_value=self.fixture.service):
            def draft(kind, payload):
                return json.loads(server.write_shader_workspace_organization_file(
                    str(self.fixture.root), opened["organizationId"], kind, json.dumps(payload), mode="draft"))
            first = draft("analysis", {"overview": "The source returns one color target.", "summary": "extra"})
            self.assertFalse(first["complete"])
            self.assertFalse(first["counted"])
            self.assertTrue(any("Unexpected: summary" in i["message"] for i in first["issues"]))
            self.assertTrue(first["issueCount"] >= 1)
            valid = self.valid_analysis()
            second = draft("analysis", {k: valid[k] for k in ("inputsResources", "computation", "outputs", "observedFacts", "inferences")})
            # the unexpected key from the first draft is still there: still reported
            self.assertTrue(any("Unexpected: summary" in i["message"] for i in second["issues"]))
            third = json.loads(server.write_shader_workspace_organization_file(
                str(self.fixture.root), opened["organizationId"], "analysis", json.dumps({"summary": None}), mode="draft"))
            self.assertFalse(third["complete"])  # None is still an unexpected key
            self.assertEqual(eq.ReviewRounds(self.fixture.root).template_errors, 0)
            self.assertEqual(eq.ReviewRounds(self.fixture.root).used, 0)
            # a complete draft finalizes with empty content
            path = Path(second["draftPath"])
            document = json.loads(path.read_text(encoding="utf-8"))
            document.pop("summary")
            path.write_text(json.dumps(document), encoding="utf-8")
            final = json.loads(server.write_shader_workspace_organization_file(
                str(self.fixture.root), opened["organizationId"], "analysis", ""))
            self.assertNotIn("error", final)
            self.assertEqual(final["kind"], "analysis")

    def test_analysis_schema_error_names_missing_and_unexpected_keys(self):
        opened = self.fixture._begin()
        wrong = {"schema": "replaylab-shader-analysis/v1", "overview": "x", "summary": "extra"}
        failed = self.write(opened, "analysis", wrong)
        self.assertIn("Missing: inputsResources, computation, outputs, observedFacts, inferences", failed["error"])
        self.assertIn("Unexpected: summary", failed["error"])

    def test_actual_module_template_writes_and_advertised_old_map_is_rejected(self):
        opened = self.fixture._begin()
        failed = self.write(opened, "module_descriptions", {"MainPS": "Returns color"})
        self.assertEqual(failed["verdict"]["reasons"][0]["severity"], "blocker")
        template = copy.deepcopy(opened["moduleDescriptionsTemplate"])
        template["modules"][0]["description"] = "Returns the computed color."
        written = self.write(opened, "module_descriptions", template)
        self.assertNotIn("error", written)
        self.assertEqual(json.loads(Path(written["path"]).read_text()), template)

    def test_new_session_does_not_reset_failed_workspace_template_errors(self):
        first = self.fixture._begin()
        self.assertEqual(self.write(first, "analysis", "{")["verdict"]["templateErrors"]["count"], 1)
        second = self.fixture._begin()
        self.assertNotEqual(first["organizationId"], second["organizationId"])
        self.assertEqual(self.write(second, "analysis", "{")["verdict"]["templateErrors"]["count"], 2)

    def test_concurrent_bad_template_writes_do_not_lose_template_errors(self):
        opened = self.fixture._begin()
        with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "10"}), \
                mock.patch.object(server, "_shader_workspace_service", return_value=self.fixture.service):
            def attempt(_):
                return json.loads(server.write_shader_workspace_organization_file(
                    str(self.fixture.root), opened["organizationId"], "analysis", "{"))
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(attempt, range(4)))
            self.assertEqual(sorted(result["verdict"]["templateErrors"]["count"] for result in results), [1, 2, 3, 4])
            self.assertEqual(eq.ReviewRounds(self.fixture.root).template_errors, 4)
            self.assertEqual(eq.ReviewRounds(self.fixture.root).used, 0)

    def test_invalid_counter_returns_stop_verdict_without_overwriting_it(self):
        opened = self.fixture._begin()
        state = self.fixture.root / "ai" / eq.ROUNDS_FILE
        state.write_text("{", encoding="utf-8")
        result = self.write(opened, "analysis", self.valid_analysis())
        self.assertFalse(result["verdict"]["accepted"])
        self.assertTrue(result["verdict"]["budgetExhausted"])
        self.assertEqual(state.read_text(encoding="utf-8"), "{")
    def test_custom_mermaid_cannot_override_published_style_and_history_is_retained(self):
        opened = self.fixture._begin()
        custom = "flowchart LR\n A[Custom]-->B[Style]\n classDef custom fill:#ff0000;\n"
        refused = self.write(opened, "flowchart", custom)
        self.assertIn("Custom Mermaid", refused["error"])
        # A historical safe input may remain readable, but publication ignores
        # its layout/palette and never overwrites the old input document.
        Path(opened["flowchartPath"]).write_text(custom, encoding="utf-8")
        accepted = self.fixture.manager.review(str(self.fixture.root), opened["organizationId"])
        self.assertTrue(accepted["verdict"]["accepted"])
        self.assertEqual(Path(opened["flowchartPath"]).read_text(encoding="utf-8"), custom)
        published = Path(accepted["flowchartPath"]).read_text(encoding="utf-8")
        self.assertTrue(published.startswith("flowchart TD"))
        self.assertNotIn("#ff0000", published)
        self.assertEqual(published, Path(accepted["flowchartGeneratedPath"]).read_text(encoding="utf-8"))

    def test_invalid_flow_blocks_before_compile_even_when_code_is_unchanged(self):
        opened = self.fixture._begin()
        spec = {"schema": eq.FLOW_SPEC_SCHEMA, "inputs": [], "resources": ["fakeTexture"], "outputs": ["FAKE"],
                "stages": [{"id": "main", "label": "Invented binding", "function": "MainPS", "reads": ["fakeTexture"], "writes": ["FAKE"], "next": []}]}
        Path(opened["flowSpecPath"]).write_text(json.dumps(spec), encoding="utf-8")
        attempts = self.fixture.service.revision
        with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "1"}):
            result = self.fixture.manager.review(str(self.fixture.root), opened["organizationId"])
        self.assertFalse(result["verdict"]["accepted"])
        self.assertFalse(result["verdict"]["softAccepted"])
        self.assertFalse(result["gate"]["safeToStartLiveEdit"])
        self.assertTrue(result["verdict"]["budgetExhausted"])
        self.assertEqual(self.fixture.service.revision, attempts)
        self.assertTrue(all({"code", "location", "severity", "direction", "target", "example"} <= set(r) for r in result["verdict"]["reasons"]))

    def test_soft_baseline_uses_host_summary_not_rejected_proposal_claims(self):
        opened = self.fixture._begin()
        self.fixture._changed_proposal(opened)
        value = self.valid_analysis()
        value["overview"] = "An unverified algorithm claim in the rejected proposal."
        text, _ = render_analysis_fields(value)
        Path(opened["analysisPath"]).write_text(text, encoding="utf-8")
        with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "1"}):
            result = self.fixture.manager.review(str(self.fixture.root), opened["organizationId"])
        self.assertTrue(result["verdict"]["softAccepted"])
        self.assertEqual(self.fixture.service.source_path.read_bytes(), self.fixture.original)
        published = Path(result["analysisPath"]).read_text(encoding="utf-8")
        self.assertNotIn("unverified algorithm claim", published)
        self.assertIn("baseline is retained", published)
        self.assertIn("unverified algorithm claim", Path(opened["analysisPath"]).read_text(encoding="utf-8"))
        self.fixture.manager.validate_publication_gate(str(self.fixture.root))

    def test_physical_failure_does_not_soft_accept_when_budget_is_exhausted(self):
        opened = self.fixture._begin()
        self.fixture._changed_proposal(opened)
        original_validate = self.fixture.manager._validate_candidate
        def fail_proposal(workspace, revision, candidate):
            if b"semantic-change" in self.fixture.service.source_path.read_bytes():
                raise ShaderOrganizationError("candidate ABI is incompatible")
            return original_validate(workspace, revision, candidate)
        self.fixture.manager._validate_candidate = fail_proposal
        with mock.patch.dict(os.environ, {"REPLAYLAB_REVIEW_MAX_ROUNDS": "1"}):
            with self.assertRaises(ShaderOrganizationError) as raised:
                self.fixture.manager.review(str(self.fixture.root), opened["organizationId"])
        verdict = raised.exception.review_verdict
        self.assertFalse(verdict["accepted"])
        self.assertFalse(verdict["softAccepted"])
        self.assertTrue(verdict["budgetExhausted"])
        self.assertEqual(self.fixture.service.source_path.read_bytes(), self.fixture.original)


class FlowSpecSourceContractTests(unittest.TestCase):
    def test_self_declared_resources_outputs_and_schema_do_not_authorize_names(self):
        spec = {"schema": "wrong-schema", "inputs": [], "resources": ["fakeTexture"], "outputs": ["FAKE"],
                "stages": [{"id": "main", "label": "Invented read", "function": "MainPS", "reads": ["fakeTexture"], "writes": ["FAKE"], "next": []}]}
        _, reasons = eq.validate_flow_spec(spec, functions=["MainPS"], resources=["realTexture"], outputs=["SV_Target0"])
        self.assertTrue({"flow-spec-schema", "flow-spec-resources", "flow-spec-outputs", "flow-spec-read", "flow-spec-output"} <= {r["code"] for r in reasons})

    def test_resource_fields_require_source_and_labels_remain_inferred(self):
        source = "cbuffer MaterialCB : register(b0) { float4 tint; };\nfloat4 MainPS(float2 uv : TEXCOORD0) : SV_Target0 { return tint; }"
        values = eq.flow_value_bindings(source)
        self.assertIn("MaterialCB.tint", values["fields"])
        spec = {"schema": eq.FLOW_SPEC_SCHEMA, "inputs": ["uv"], "resources": ["MaterialCB"], "outputs": ["SV_Target0"],
                "stages": [{"id": "main", "label": "May tint material", "function": "MainPS", "reads": ["MaterialCB.fake"], "writes": ["SV_Target0"], "next": []}]}
        args = dict(functions=["MainPS"], resources=["MaterialCB"], outputs=["SV_Target0"], inputs=values["inputs"], fields=values["fields"])
        _, reasons = eq.validate_flow_spec(spec, **args)
        self.assertIn("flow-spec-read", [r["code"] for r in reasons])
        spec["stages"][0]["reads"] = ["MaterialCB.tint"]
        clean, reasons = eq.validate_flow_spec(spec, **args)
        self.assertEqual(reasons, [])
        self.assertIs(clean["stages"][0]["inferred"], True)

    def test_comment_declarations_do_not_become_resource_evidence(self):
        source = "// Texture2D fake : register(t9);\nfloat4 MainPS() : SV_Target0 { return 0; }"
        self.assertEqual(eq.declared_symbols(source)["resources"], [])


if __name__ == "__main__":
    unittest.main()
