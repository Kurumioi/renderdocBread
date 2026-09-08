"""CPU-only evidence tests. No RenderDoc import, capture, GPU, or demo execution."""
import copy
import unittest

from discover_d3d12_checkpoint_fixture import binding_issues, select_fixture_events


def action(event, kind, name=""):
    return {"eventId": event, "name": name, "commandBoundary": kind in ("begin", "end"),
            "beginPass": kind == "begin", "endPass": kind == "end", "draw": kind == "draw",
            "clear": kind == "clear", "exactAPI": {"chunkName": "ID3D12GraphicsCommandList::" +
                                                    ("DrawInstanced" if kind == "draw" else "ClearRenderTargetView")}}


def group(start):
    return [action(start, "begin", "=> ExecuteCommandLists(1)[0]: Reset(ResourceId::100)"),
            action(start + 2, "clear"), action(start + 13, "draw"),
            action(start + 15, "end", "=> ExecuteCommandLists(1)[0]: Close(ResourceId::100)")]


class FixtureDiscoveryTests(unittest.TestCase):
    def test_selects_actual_draw_eids_across_completed_submissions(self):
        report = select_fixture_events(group(18) + group(57) + group(91))
        self.assertEqual(report["selectedEvents"], [31, 70, 104])
        self.assertTrue(report["readyForCheckpointMutation"])

    def test_single_submit_is_explicitly_not_mutation_reuse_evidence(self):
        with self.assertRaises(ValueError):
            select_fixture_events(group(100))
        report = select_fixture_events(group(100), True)
        self.assertEqual(report["selectedEvents"], [102, 113])
        self.assertFalse(report["readyForCheckpointMutation"])

    def test_unknown_api_or_incomplete_group_never_guesses_eid(self):
        rows = group(1) + group(50)
        rows[2]["exactAPI"] = None
        with self.assertRaises(ValueError):
            select_fixture_events(rows)
        with self.assertRaises(ValueError):
            select_fixture_events((group(1) + group(50))[:-1])
        rows = group(1) + group(50)
        rows[0]["name"] = "=> ExecuteCommandLists(2)[0]: Reset(ResourceId::100)"
        with self.assertRaises(ValueError):
            select_fixture_events(rows)

    def test_requires_shared_real_shader_rt_and_float_output(self):
        first = {"eventId": 3, "pixelShader": "ResourceId::37", "colorTargets": [{"slot": 0, "resource": "ResourceId::11"}],
                 "shaderOutputSignature": [{"systemValue": "ColorOutput", "regIndex": 0, "compCount": 4, "varType": "Float"}]}
        second = copy.deepcopy(first)
        second["eventId"] = 77
        self.assertEqual(binding_issues([first, second], {3, 77}), [])
        second["pixelShader"] = "ResourceId::38"
        self.assertIn("selected submissions must share the actual captured pixel shader", binding_issues([first, second], {3, 77}))
        second = copy.deepcopy(first)
        second["eventId"] = 77
        second["shaderOutputSignature"][0]["varType"] = "UInt"
        self.assertTrue(binding_issues([first, second], {3, 77}))


if __name__ == "__main__":
    unittest.main()
