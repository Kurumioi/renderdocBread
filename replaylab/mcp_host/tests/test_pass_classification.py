from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from src.pass_classification import (
    KNOWLEDGE_SCHEMA,
    PassClassificationError,
    PassKnowledgeStore,
    apply_ai_semantic_proposals,
    classify_passes,
)

CAPTURE_A = "a" * 64
CAPTURE_B = "b" * 64
PS_A = "1" * 64
VS_A = "2" * 64


def _pipeline(
    *,
    render_targets=None,
    depth_target=None,
    blend=None,
    depth_test=None,
    depth_write=None,
    reads=None,
    fullscreen=None,
    shader_name="",
):
    value = {
        "renderTargets": [] if render_targets is None else render_targets,
        "depthTarget": depth_target,
        "blendEnabled": blend,
        "depthTestEnabled": depth_test,
        "depthWriteEnabled": depth_write,
        "readResources": [] if reads is None else reads,
        "shaders": {
            "vertex": {"shaderId": 10, "rawSha256": VS_A},
            "fragment": {
                "shaderId": 11,
                "rawSha256": PS_A,
                "displayName": shader_name,
            },
        },
    }
    if fullscreen is not None:
        value["fullscreen"] = fullscreen
    return value


class PassClassificationTests(unittest.TestCase):
    def test_depth_only_is_confirmed_from_pipeline_shape(self) -> None:
        result = classify_passes(
            [{"eventId": 10, "name": "DrawIndexed", "flags": 0x0002}],
            pipeline_states={10: _pipeline(depth_target=900)},
            capture_id=CAPTURE_A,
        )
        item = result["items"][0]
        self.assertEqual(item["category"], "Depth")
        self.assertEqual(item["subcategory"], "DepthOnly")
        self.assertEqual(item["confidence"], "confirmed")
        self.assertEqual(item["authority"], "FixedProgram")
        self.assertFalse(item["semanticVerified"])
        self.assertEqual(item["evidence"][0]["code"], "depth-only-target")
        self.assertRegex(item["passFingerprint"], r"^[0-9a-f]{64}$")

    def test_shadow_marker_and_depth_shape_select_shadow(self) -> None:
        result = classify_passes(
            [
                {
                    "eventId": 20,
                    "name": "DrawIndexed",
                    "flags": 0x0002,
                    "markerPath": ["Frame", "CSM ShadowDepth"],
                }
            ],
            pipeline_states={20: _pipeline(depth_target=901)},
        )
        item = result["items"][0]
        self.assertEqual(item["category"], "Shadow")
        self.assertEqual(item["subcategory"], "CascadedShadow")
        self.assertEqual(item["confidence"], "probable")
        self.assertEqual(
            {entry["source"] for entry in item["evidence"]},
            {"markerPath", "pipelineState"},
        )

    def test_graphics_semantic_categories_use_independent_fixed_evidence(self) -> None:
        actions = [
            {"eventId": 1, "name": "BasePass Opaque", "flags": 2},
            {"eventId": 2, "name": "Draw", "flags": 2, "markerPath": ["GBuffer"]},
            {"eventId": 3, "name": "Water Translucency", "flags": 2},
            {"eventId": 4, "name": "ToneMap", "flags": 2},
            {"eventId": 5, "name": "HUD Canvas", "flags": 2},
            {"eventId": 6, "name": "Deferred Lighting", "flags": 2},
        ]
        states = {
            1: _pipeline(
                render_targets=[101], depth_target=201, blend=False, depth_write=True
            ),
            2: _pipeline(
                render_targets=[101, 102, 103],
                depth_target=201,
                blend=False,
                depth_write=True,
            ),
            3: _pipeline(
                render_targets=[101], depth_target=201, blend=True, depth_write=False
            ),
            4: _pipeline(
                render_targets=[101], blend=False, depth_write=False, fullscreen=True
            ),
            5: _pipeline(
                render_targets=[101], blend=True, depth_test=False, depth_write=False
            ),
            6: _pipeline(
                render_targets=[101],
                reads=[1, 2, 3, 4],
                fullscreen=True,
                depth_write=False,
            ),
        }
        result = classify_passes(actions, pipeline_states=states)
        categories = {item["eventId"]: item["category"] for item in result["items"]}
        self.assertEqual(
            categories,
            {
                1: "BasePass",
                2: "GBuffer",
                3: "Transparent",
                4: "Post",
                5: "UI",
                6: "Lighting",
            },
        )
        self.assertTrue(all(item["evidence"] for item in result["items"]))
        self.assertEqual(
            [node["label"] for node in result["tree"]],
            ["BasePass", "GBuffer", "Lighting", "Transparent", "Post", "UI"],
        )

    def test_dispatch_stays_compute_and_reports_semantic_role(self) -> None:
        result = classify_passes(
            [
                {
                    "eventId": 30,
                    "name": "TAA Upscale",
                    "flags": 0x0004,
                    "markerPath": ["PostProcess"],
                }
            ],
            pipeline_states={30: _pipeline(render_targets=[100])},
        )
        item = result["items"][0]
        self.assertEqual(item["category"], "Compute")
        self.assertEqual(item["confidence"], "confirmed")
        self.assertEqual(item["semanticRole"], "Post")

    def test_utility_actions_are_exact_action_categories(self) -> None:
        result = classify_passes(
            [
                {"eventId": 40, "name": "ClearRenderTarget", "flags": 0x0001},
                {"eventId": 41, "name": "Resolve", "flags": 0x0800},
                {"eventId": 42, "name": "Copy", "flags": 0x0400},
            ]
        )
        self.assertEqual(
            [(item["category"], item["subcategory"]) for item in result["items"]],
            [("Utility", "Clear"), ("Utility", "Resolve"), ("Utility", "Copy")],
        )
        self.assertTrue(
            all(item["confidence"] == "confirmed" for item in result["items"])
        )

    def test_execute_indirect_parent_is_container_and_children_are_not_duplicated(
        self,
    ) -> None:
        nested = [
            {
                "eventId": 50,
                "name": "ExecuteIndirect(2)",
                "flags": 0x0200,
                "children": [
                    {
                        "eventId": 51,
                        "name": "DrawIndexedIndirect[0]",
                        "flags": 0x080002,
                    },
                    {
                        "eventId": 52,
                        "name": "DrawIndexedIndirect[1]",
                        "flags": 0x080002,
                    },
                ],
            },
            # A duplicate flattened copy from an adapter must not become a
            # second leaf.
            {
                "eventId": 51,
                "name": "DrawIndexedIndirect[0]",
                "flags": 0x080002,
                "markerPath": ["ExecuteIndirect(2)"],
            },
        ]
        states = {
            51: _pipeline(
                render_targets=[101], depth_target=201, blend=False, depth_write=True
            ),
            52: _pipeline(
                render_targets=[101], depth_target=201, blend=False, depth_write=True
            ),
        }
        result = classify_passes(nested, pipeline_states=states)
        self.assertEqual(result["summary"]["containerCount"], 1)
        self.assertEqual(result["summary"]["actionCount"], 2)
        self.assertEqual(result["summary"]["duplicateCount"], 1)
        self.assertEqual([item["eventId"] for item in result["items"]], [51, 52])
        self.assertTrue(all(item["indirect"] for item in result["items"]))
        groups = result["tree"][0]["children"][0]["children"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["nodeType"], "container")
        self.assertEqual(groups[0]["count"], 2)
        self.assertTrue(groups[0]["foldByDefault"])

    def test_markers_become_context_not_empty_action_leaves(self) -> None:
        result = classify_passes(
            [
                {"eventId": 60, "name": "BasePass", "flags": 0x0040},
                {
                    "eventId": 61,
                    "name": "Draw",
                    "flags": 2,
                    "markerPath": ["BasePass"],
                },
            ],
            pipeline_states={61: _pipeline(render_targets=[1], depth_target=2)},
        )
        self.assertEqual([item["eventId"] for item in result["items"]], [61])
        self.assertEqual(
            result["omittedEvents"][0]["reason"], "marker-used-as-tree-context"
        )

    def test_shader_catalog_names_and_hashes_are_joined_by_event(self) -> None:
        catalog = {
            "shaders": [
                {
                    "shaderKey": "11:fragment:main:x",
                    "shaderResourceId": 11,
                    "stage": "fragment",
                    "rawSha256": PS_A,
                    "displayName": "DeferredLightingPS",
                }
            ],
            "uses": [
                {
                    "eventId": 70,
                    "shaderKey": "11:fragment:main:x",
                    "resourceId": 11,
                    "stage": "fragment",
                }
            ],
        }
        state = _pipeline(render_targets=[100], reads=[1, 2, 3], fullscreen=True)
        state["shaders"]["fragment"].pop("rawSha256")
        result = classify_passes(
            [{"eventId": 70, "name": "Draw", "flags": 2}],
            pipeline_states={70: state},
            shader_catalog=catalog,
        )
        item = result["items"][0]
        self.assertEqual(item["category"], "Lighting")
        fragment = next(
            shader for shader in item["shaders"] if shader["stage"] == "fragment"
        )
        self.assertEqual(fragment["rawSha256"], PS_A)
        self.assertEqual(fragment["name"], "DeferredLightingPS")

    def test_ai_proposal_is_isolated_and_never_changes_fixed_tree(self) -> None:
        fixed = classify_passes(
            [{"eventId": 80, "name": "BasePass", "flags": 2}],
            pipeline_states={80: _pipeline(render_targets=[1], depth_target=2)},
        )
        fixed_tree = deepcopy(fixed["tree"])
        overlaid = apply_ai_semantic_proposals(
            fixed,
            [
                {
                    "eventId": 80,
                    "category": "UI",
                    "subcategory": "Inventory",
                    "displayLabel": "Inventory UI candidate",
                    "rationale": "The asset name resembles a menu material.",
                }
            ],
        )
        self.assertEqual(overlaid["tree"], fixed_tree)
        self.assertEqual(overlaid["items"][0]["category"], "BasePass")
        proposal = overlaid["items"][0]["semanticProposal"]
        self.assertEqual(proposal["authority"], "AIProvisional")
        self.assertFalse(proposal["verified"])
        self.assertFalse(proposal["applied"])
        self.assertFalse(proposal["physicalEvidence"])
        self.assertFalse(overlaid["policy"]["aiCanChangeFixedTree"])

    def test_ai_proposal_rejects_unknown_or_duplicate_event(self) -> None:
        fixed = classify_passes([{"eventId": 90, "name": "Dispatch", "flags": 4}])
        with self.assertRaises(PassClassificationError):
            apply_ai_semantic_proposals(
                fixed, [{"eventId": 999, "category": "Compute"}]
            )
        with self.assertRaises(PassClassificationError):
            apply_ai_semantic_proposals(
                fixed,
                [
                    {"eventId": 90, "category": "Compute"},
                    {"eventId": 90, "category": "Post"},
                ],
            )


class PassKnowledgeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        (self.project / "knowledge").mkdir()
        self.manifest = {
            "schema": "renderdoc-ultra-replay-project/v1",
            "schemaVersion": 1,
            "projectId": "1" * 32,
            "scope": {
                "gameId": "example-game",
                "buildId": "build-1",
                "platform": "windows-x64",
                "apiFamily": "d3d12",
            },
            "captures": [{"captureId": CAPTURE_A}, {"captureId": CAPTURE_B}],
            "knowledge": {"workspaces": [], "aliases": [], "materials": []},
        }
        (self.project / "project.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )
        self.store = PassKnowledgeStore(self.project)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _classification(self, capture_id: str, name: str) -> dict:
        return classify_passes(
            [{"eventId": 100, "name": name, "flags": 2}],
            pipeline_states={
                100: _pipeline(
                    render_targets=[{"resourceId": 1, "format": "R8G8B8A8"}],
                    depth_target={"resourceId": 2, "format": "D32"},
                    blend=False,
                    depth_test=True,
                    depth_write=True,
                )
            },
            capture_id=capture_id,
        )

    def test_save_load_and_exact_fingerprint_reuse(self) -> None:
        first = self._classification(CAPTURE_A, "BasePass Opaque")
        saved = self.store.save(first)
        self.assertEqual(saved["schema"], KNOWLEDGE_SCHEMA)
        self.assertEqual(saved["recordCount"], 1)
        self.assertEqual(saved["reusableRecordCount"], 1)
        knowledge = self.store.load()
        self.assertEqual(knowledge["records"][0]["category"], "BasePass")
        self.assertEqual(knowledge["records"][0]["authority"], "FixedProgram")
        self.assertFalse(knowledge["records"][0]["semanticVerified"])

        future = classify_passes(
            [{"eventId": 200, "name": "DrawIndexed", "flags": 2}],
            pipeline_states={
                200: _pipeline(
                    render_targets=[{"resourceId": 999, "format": "R8G8B8A8"}],
                    depth_target={"resourceId": 888, "format": "D32"},
                    blend=False,
                    depth_test=True,
                    depth_write=True,
                )
            },
            project_knowledge=knowledge,
            capture_id=CAPTURE_B,
        )
        item = future["items"][0]
        self.assertEqual(item["category"], "BasePass")
        self.assertTrue(
            any(entry["source"] == "projectKnowledge" for entry in item["evidence"])
        )

        # Saving the same capture replaces its observation instead of counting
        # it twice.
        self.store.save(first)
        loaded_again = self.store.load()
        self.assertEqual(len(loaded_again["captureObservations"]), 1)
        self.assertEqual(loaded_again["records"][0]["captureCount"], 1)

    def test_conflicting_fixed_observations_disable_cross_frame_reuse(self) -> None:
        first = self._classification(CAPTURE_A, "BasePass")
        second = self._classification(CAPTURE_B, "UI HUD")
        # Force the second capture to describe the same exact portable pass
        # fingerprint; only semantic evidence differs.
        self.assertEqual(
            first["items"][0]["passFingerprint"],
            second["items"][0]["passFingerprint"],
        )
        self.store.save(first)
        saved = self.store.save(second)
        self.assertEqual(saved["reusableRecordCount"], 0)
        record = self.store.load()["records"][0]
        self.assertTrue(record["conflict"])
        self.assertFalse(record["reusable"])
        self.assertEqual(record["category"], "Unknown")

    def test_ai_overlays_are_persisted_separately_and_never_become_records(
        self,
    ) -> None:
        fixed = self._classification(CAPTURE_A, "BasePass")
        overlaid = apply_ai_semantic_proposals(
            fixed,
            [{"eventId": 100, "category": "UI", "rationale": "candidate only"}],
        )
        saved = self.store.save(overlaid)
        self.assertEqual(saved["provisionalOverlayCount"], 1)
        knowledge = self.store.load()
        self.assertEqual(knowledge["records"][0]["category"], "BasePass")
        proposal = knowledge["provisionalOverlays"][0]
        self.assertEqual(proposal["authority"], "AIProvisional")
        self.assertFalse(proposal["verified"])
        self.assertFalse(proposal["applied"])
        self.assertFalse(knowledge["policy"]["aiProposalsReusableAuthority"])

    def test_store_rejects_unimported_capture_and_project_scope_change(self) -> None:
        unknown = self._classification("c" * 64, "BasePass")
        with self.assertRaises(PassClassificationError):
            self.store.save(unknown)

        self.store.save(self._classification(CAPTURE_A, "BasePass"))
        changed = dict(self.manifest)
        changed["scope"] = dict(changed["scope"])
        changed["scope"]["buildId"] = "other-build"
        (self.project / "project.json").write_text(
            json.dumps(changed), encoding="utf-8"
        )
        with self.assertRaisesRegex(PassClassificationError, "exact project scope"):
            self.store.load()

    def test_load_rejects_ai_record_as_reusable_authority(self) -> None:
        self.store.save(self._classification(CAPTURE_A, "BasePass"))
        value = json.loads(self.store.path.read_text(encoding="utf-8"))
        value["records"][0]["authority"] = "AIProvisional"
        self.store.path.write_text(json.dumps(value), encoding="utf-8")
        loaded = self.store.load()
        result = classify_passes(
            [{"eventId": 200, "name": "Draw", "flags": 2}],
            pipeline_states={200: _pipeline(render_targets=[1], depth_target=2)},
            project_knowledge=loaded,
        )
        self.assertFalse(
            any(
                evidence["source"] == "projectKnowledge"
                for evidence in result["items"][0]["evidence"]
            )
        )


if __name__ == "__main__":
    unittest.main()
