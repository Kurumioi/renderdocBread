"""CBuffer alias store shared with the qrenderdoc panel (keyed by block layout)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.shader_workspace import aliases

SOURCE = """cbuffer cb0 : register(b0) {
  float4 v0;            // 0
  float3 v1;            // 16
  float v2;             // 28  (fits behind the float3)
  float2 v3;            // 32
  float4x4 m0;          // 48
  row_major float3x2 m1;// 112 (3 rows)
  float4 arr[3];        // 160
  uint flags : packoffset(c20.y);   // 324
  SomeStruct s;         // unknown from here on
  float after;          // -1
};
cbuffer Lights : register(b2, space1) { int count; float3 dir; };
"""
LIGHTS = [{"name": "count", "offset": 0}, {"name": "dir", "offset": 4}]


class ParseTests(unittest.TestCase):
    def test_offsets_follow_hlsl_packing_and_packoffset(self) -> None:
        members = aliases.cbuffer_members(SOURCE)
        by_name = {m["name"]: m for m in members}
        self.assertEqual([m["name"] for m in members], ["v0", "v1", "v2", "v3", "m0", "m1", "arr", "flags", "s", "after", "count", "dir"])
        self.assertEqual(by_name["v0"]["offset"], 0)
        self.assertEqual(by_name["v1"]["offset"], 16)
        self.assertEqual(by_name["v2"]["offset"], 28)
        self.assertEqual(by_name["v3"]["offset"], 32)
        self.assertEqual(by_name["m0"]["offset"], 48)
        self.assertEqual(by_name["m1"]["offset"], 112)
        self.assertEqual(by_name["arr"]["offset"], 160)
        self.assertFalse(by_name["arr"]["leaf"])
        self.assertEqual(by_name["flags"]["offset"], 324)
        self.assertEqual(by_name["s"]["offset"], -1)
        self.assertEqual(by_name["after"]["offset"], -1)
        self.assertEqual((by_name["v0"]["block"], by_name["v0"]["bind"], by_name["v0"]["space"]), ("cb0", 0, 0))
        self.assertEqual((by_name["dir"]["block"], by_name["dir"]["bind"], by_name["dir"]["space"], by_name["dir"]["offset"]), ("Lights", 2, 1, 4))

    def test_layout_signature_matches_the_panel_contract(self) -> None:
        # ReplayLabCBufferAliases::LayoutSignature builds exactly this text.
        self.assertEqual(aliases.layout_signature("Lights", LIGHTS), "Lights\ncount@0\ndir@4")
        self.assertEqual(aliases.layout_sha256("Lights", LIGHTS), hashlib.sha256(b"Lights\ncount@0\ndir@4").hexdigest())
        self.assertEqual(aliases.layout_signature("cb0", [{"name": "s", "offset": -1}]), "")
        self.assertNotEqual(aliases.layout_sha256("A", LIGHTS), aliases.layout_sha256("B", LIGHTS))

    def test_publications_from_rename_map_cover_only_complete_cbuffer_layouts(self) -> None:
        pubs = aliases.aliases_from_renames(SOURCE, {"v0": "tint", "cb0": "MaterialCB", "arr": "bones", "dir": "sunDir", "ghost": "x", "count": "count"})
        self.assertEqual([p["block"] for p in pubs], ["cb0", "Lights"])
        cb0, lights = pubs
        self.assertEqual(cb0["layoutSha256"], "")  # struct member -> layout incomplete
        self.assertEqual([(e["path"], e["alias"], e["offset"]) for e in cb0["entries"]], [("v0", "tint", 0), ("arr", "bones", -1)])
        self.assertEqual(cb0["blockAlias"], "MaterialCB")
        self.assertEqual(lights["layoutSha256"], aliases.layout_sha256("Lights", LIGHTS))
        self.assertEqual(lights["entries"], [{"path": "dir", "offset": 4, "alias": "sunDir", "byteSize": 12, "original": "dir"}])
        self.assertIsNone(lights["blockAlias"])

    def test_alias_validation(self) -> None:
        self.assertTrue(aliases.is_valid_alias("基础色 (albedo)"))
        for bad in ("", "  ", "a\nb", "x" * 65, " lead"):
            self.assertFalse(aliases.is_valid_alias(bad), repr(bad))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"REPLAYLAB_ALIAS_ROOT": self.tmp.name})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_ai_publish_never_overwrites_user_entries(self) -> None:
        user = aliases.publish_aliases(
            "Lights",
            LIGHTS,
            [{"path": "count", "offset": 0, "alias": "lightCount"}],
            source="user",
            block_alias="Lighting Constants",
        )
        self.assertEqual(user["written"], ["Lights.count"])
        self.assertTrue(user["blockAliasWritten"])
        ai = aliases.publish_aliases("Lights", LIGHTS, [
            {"path": "count", "alias": "aiCount"},
            {"path": "dir", "offset": 4, "alias": "sunDir"},
            {"path": "ghost", "alias": ""},
        ], block_alias="AI Lighting")
        self.assertEqual(ai["keptUser"], ["Lights.count"])
        self.assertTrue(ai["blockAliasKeptUser"])
        self.assertFalse(ai["blockAliasWritten"])
        self.assertEqual(ai["written"], ["Lights.dir"])
        self.assertEqual(ai["rejected"][0]["path"], "Lights.ghost")
        key = aliases.layout_sha256("Lights", LIGHTS)
        stored = aliases.read_aliases(key)
        self.assertEqual({e["path"]: (e["alias"], e["source"]) for e in stored}, {"count": ("lightCount", "user"), "dir": ("sunDir", "ai")})
        payload = json.loads(Path(ai["path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], aliases.SCHEMA)
        self.assertEqual((payload["layoutSha256"], payload["block"], payload["members"]), (key, "Lights", LIGHTS))
        self.assertEqual(
            aliases.read_block_alias(key),
            {"alias": "Lighting Constants", "source": "user", "updatedAt": payload["blockAlias"]["updatedAt"]},
        )
        self.assertEqual(Path(ai["path"]).name, key + ".json")
        # a later user rename replaces the AI entry
        again = aliases.publish_aliases("Lights", LIGHTS, [{"path": "dir", "alias": "sunDirection"}], source="user")
        self.assertEqual(again["written"], ["Lights.dir"])
        self.assertEqual([e["alias"] for e in aliases.read_aliases(key) if e["path"] == "dir"], ["sunDirection"])
        # the same layout in another shader / capture sees the same aliases
        evidence = aliases.evidence_for_source("cbuffer Lights : register(b7) { int count; float3 dir; };")
        self.assertEqual(evidence["blocks"][0]["user"], [{"path": "count", "alias": "lightCount"}, {"path": "dir", "alias": "sunDirection"}])
        self.assertEqual(evidence["blocks"][0]["ai"], [])
        self.assertEqual(evidence["blocks"][0]["blockAlias"]["alias"], "Lighting Constants")

    def test_incomplete_layout_and_corrupt_store_are_reported_not_hidden(self) -> None:
        with self.assertRaises(ValueError):
            aliases.publish_aliases("cb0", [{"name": "s", "offset": -1}], [{"path": "s", "alias": "x"}])
        key = aliases.layout_sha256("Lights", LIGHTS)
        path = aliases.layout_alias_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            aliases.publish_aliases("Lights", LIGHTS, [{"path": "count", "alias": "x"}])
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")
        evidence = aliases.evidence_for_source(SOURCE)
        self.assertIn("layout incomplete", evidence["blocks"][0]["error"])
        self.assertIn("JSON", evidence["blocks"][1]["error"])

    def test_captured_binding_retargets_generated_names_to_original_panel_key(self) -> None:
        source = "cbuffer _41_43 : register(b0, space2) { float generated : packoffset(c1.y); };"
        members = [{"name": "original", "offset": 20}]
        identity = {"blocks": [{"block": "cbuffer0", "bind": 0, "space": 2,
                                "members": members, "memberMappingAvailable": True,
                                "leaves": [{"path": "original", "offset": 20, "byteSize": 4}]}]}
        user = aliases.publish_aliases("cbuffer0", members,
                                      [{"path": "original", "offset": 20, "alias": "userValue"}],
                                      source="user", block_alias="User Constants")
        publications = aliases.aliases_from_renames(
            source, {"_41_43": "MaterialCB", "generated": "inferredValue"}, identity)
        self.assertEqual(len(publications), 1)
        publication = publications[0]
        self.assertEqual(publication["block"], "cbuffer0")
        self.assertEqual(publication["layoutSha256"], user["layoutSha256"])
        self.assertEqual(publication["entries"], [{"path": "original", "offset": 20, "alias": "inferredValue", "byteSize": 4, "original": "generated"}])
        self.assertTrue(publication["capturedIdentityMatched"])
        result = aliases.publish_aliases(publication["block"], publication["members"],
                                        publication["entries"], block_alias=publication["blockAlias"])
        self.assertEqual(result["keptUser"], ["cbuffer0.original"])
        self.assertTrue(result["blockAliasKeptUser"])
        evidence = aliases.evidence_for_source(source, identity)["blocks"][0]
        self.assertEqual(evidence["user"], [{"path": "generated", "alias": "userValue"}])
        self.assertEqual(evidence["blockAlias"]["alias"], "User Constants")
        self.assertNotEqual(user["layoutSha256"], aliases.layout_sha256("_41_43", [{"name": "generated", "offset": 20}]))

    def test_opaque_or_different_width_fields_only_publish_captured_block_alias(self) -> None:
        source = "cbuffer generatedCB : register(b0) { float scalar : packoffset(c0); };"
        target = {"block": "cbuffer0", "bind": 0, "space": 0,
                  "members": [{"name": "unknown", "offset": 0}],
                  "memberMappingAvailable": False, "leaves": []}
        for complete, leaves in ((False, []), (True, [{"path": "vector", "offset": 0, "byteSize": 16}])):
            target.update(memberMappingAvailable=complete, leaves=leaves)
            result = aliases.aliases_from_renames(source, {"generatedCB": "Material", "scalar": "red"}, {"blocks": [target]})[0]
            self.assertEqual(result["entries"], [])
            self.assertEqual(result["unmapped"], ["scalar"])
            self.assertEqual(result["blockAlias"], "Material")
            self.assertEqual(result["layoutSha256"], aliases.layout_sha256("cbuffer0", target["members"]))

    def test_missing_or_ambiguous_capture_slot_cannot_publish_generated_layout(self) -> None:
        source = "cbuffer generatedCB : register(b0) { float scalar; };"
        target = {"block": "cbuffer0", "bind": 0, "space": 0,
                  "members": [], "leaves": [], "memberMappingAvailable": False}
        for blocks in ([], [target, target]):
            result = aliases.aliases_from_renames(source, {"generatedCB": "Material", "scalar": "red"}, {"blocks": blocks})[0]
            self.assertEqual(result["layoutSha256"], "")
            self.assertFalse(result["capturedIdentityMatched"])
            self.assertEqual(result["entries"], [])


if __name__ == "__main__":
    unittest.main()
