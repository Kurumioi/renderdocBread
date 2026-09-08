from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from src.asset_catalog import AssetCatalogService, asset_tokens
from src.shader_asset_retrieval import (
    ShaderAssetResolutionStore,
    retrieve,
    structural_key,
    texture_name_tokens,
)
from src.symbols.engines import detect_engine, parse_engine_asset
from src.symbols.engines.unreal_package import map_object_path, read_summary


def _fstring(text: str) -> bytes:
    raw = text.encode("ascii") + b"\x00"
    return struct.pack("<i", len(raw)) + raw


def build_package(names: list[str], imports: list[tuple[str, str, int, str]], export_name: str,
                  export_class_import: int) -> bytes:
    """Emit a minimal UE 5.4-style cooked package summary + tables."""

    name_index = {name: index for index, name in enumerate(names)}

    def fname(name: str) -> bytes:
        return struct.pack("<ii", name_index[name], 0)

    name_table = b"".join(_fstring(name) + b"\x00\x00\x00\x00" for name in names)
    import_table = b"".join(
        fname(class_package) + fname(class_name) + struct.pack("<i", outer) + fname(object_name) + struct.pack("<i", 0)
        for class_package, class_name, outer, object_name in imports
    )
    export_table = (
        struct.pack("<iiii", export_class_import, 0, 0, 0) + fname(export_name)
        + struct.pack("<I", 0) + struct.pack("<qq", 1234, 0)
    )
    header = bytearray()
    header += struct.pack("<Ii", 0x9E2A83C1, -8)          # tag, legacy version
    header += struct.pack("<i", 864)                       # legacy UE3
    header += struct.pack("<iii", 522, 1012, 0)            # UE4, UE5, licensee
    header += struct.pack("<i", 0)                         # custom versions
    fixed_after = 4 + len(_fstring("None")) + 4 + 8 + 8 + 8 + 16 + 4  # total, folder, flags, names, soft, gatherable, exp/imp, depends
    summary_size = len(header) + fixed_after
    name_offset = summary_size
    import_offset = name_offset + len(name_table)
    export_offset = import_offset + len(import_table)
    total = export_offset + len(export_table)
    header += struct.pack("<i", total) + _fstring("None") + struct.pack("<I", 0x80000200)
    header += struct.pack("<ii", len(names), name_offset)
    header += struct.pack("<ii", 0, 0)                     # soft object paths
    header += struct.pack("<ii", 0, 0)                     # gatherable text
    header += struct.pack("<ii", 1, export_offset) + struct.pack("<ii", len(imports), import_offset)
    header += struct.pack("<i", total)                     # depends offset
    assert len(header) == summary_size, (len(header), summary_size)
    return bytes(header) + name_table + import_table + export_table


NAMES = ["None", "/Script/CoreUObject", "/Script/Engine", "Package", "Class", "Material", "Texture2D",
         "MaterialInstanceConstant", "/Game/Art/Textures/T_Crow_Body_D", "T_Crow_Body_D",
         "/Game/Art/Textures/T_Crow_Body_N", "T_Crow_Body_N", "M_CrowFeather", "MI_CrowFeather",
         "/Game/Art/Materials/M_CrowFeather", "FeatherSheen", "IridescenceStrength", "StructProperty",
         "Default__Material", "M_Rock", "/Game/Art/Textures/T_Rock_D", "T_Rock_D", "RockRoughness"]


def crow_material() -> bytes:
    imports = [
        ("/Script/CoreUObject", "Package", 0, "/Script/Engine"),      # -1
        ("/Script/CoreUObject", "Class", -1, "Material"),             # -2
        ("/Script/CoreUObject", "Package", 0, "/Game/Art/Textures/T_Crow_Body_D"),  # -3
        ("/Script/Engine", "Texture2D", -3, "T_Crow_Body_D"),         # -4
        ("/Script/CoreUObject", "Package", 0, "/Game/Art/Textures/T_Crow_Body_N"),  # -5
        ("/Script/Engine", "Texture2D", -5, "T_Crow_Body_N"),         # -6
    ]
    return build_package(NAMES, imports, "M_CrowFeather", -2)


def crow_instance() -> bytes:
    imports = [
        ("/Script/CoreUObject", "Package", 0, "/Script/Engine"),
        ("/Script/CoreUObject", "Class", -1, "MaterialInstanceConstant"),
        ("/Script/CoreUObject", "Package", 0, "/Game/Art/Materials/M_CrowFeather"),
        ("/Script/Engine", "Material", -3, "M_CrowFeather"),
    ]
    return build_package(NAMES, imports, "MI_CrowFeather", -2)


def rock_material() -> bytes:
    imports = [
        ("/Script/CoreUObject", "Package", 0, "/Script/Engine"),
        ("/Script/CoreUObject", "Class", -1, "Material"),
        ("/Script/CoreUObject", "Package", 0, "/Game/Art/Textures/T_Rock_D"),
        ("/Script/Engine", "Texture2D", -3, "T_Rock_D"),
    ]
    return build_package(NAMES, imports, "M_Rock", -2)


def uexp_blob(shader_types: list[str], floats: list[float] = ()) -> bytes:
    body = b"\x00".join(name.encode() for name in shader_types)
    payload = b"".join(struct.pack("<f", value) for value in floats)
    # one odd byte before the floats: parameter payloads are not aligned
    return (b"FMaterialShaderMapContent\x00FGPUSkinVertexFactoryShaderParameters\x00" + body
            + b"\x00LMP_NO_LIGHTMAP\x00" + b"\x07" + payload + b"\x00\x00")


class UnrealPackageTests(unittest.TestCase):
    def test_summary_names_imports_and_mounts(self) -> None:
        data = crow_material()
        summary = read_summary(data)
        self.assertEqual((summary["fileVersionUE4"], summary["fileVersionUE5"], summary["importCount"]), (522, 1012, 6))
        context = detect_engine(["Game/Game.uproject", "Game/Content/Art/Materials/M_CrowFeather.uasset",
                                 "Engine/Content/EngineMaterials/DefaultDiffuse.uasset"])
        self.assertEqual(context.engine, "unreal")
        self.assertEqual(context.mounts["/Game"], "Game/Content")
        metadata, references = parse_engine_asset(context, "Game/Content/Art/Materials/M_CrowFeather.uasset", data)
        self.assertEqual(metadata["className"], "Material")
        self.assertEqual(metadata["textures"], ["/Game/Art/Textures/T_Crow_Body_D.T_Crow_Body_D",
                                                "/Game/Art/Textures/T_Crow_Body_N.T_Crow_Body_N"])
        self.assertIn("FeatherSheen", metadata["names"])
        self.assertNotIn("StructProperty", metadata["names"])
        self.assertEqual([item["targetPath"] for item in references],
                         ["Game/Content/Art/Textures/T_Crow_Body_D.uasset", "Game/Content/Art/Textures/T_Crow_Body_N.uasset"])
        self.assertEqual(map_object_path("/Engine/X/Y", context.mounts), "Engine/Content/X/Y.uasset")
        self.assertEqual(map_object_path("/Plugin/X", context.mounts), "")
        tokens = asset_tokens("Game/Content/Art/Materials/M_CrowFeather.uasset", metadata)
        self.assertTrue({"crow", "feather", "crowfeather", "body", "sheen"} <= set(tokens), tokens)

    def test_export_strings(self) -> None:
        metadata, references = parse_engine_asset(None, "Game/Content/Art/Materials/M_CrowFeather.uexp",
                                                  uexp_blob(["FDepthOnlyPS", "TBasePassPSFNoLightMapPolicy"]))
        self.assertTrue(metadata["hasInlineShaderMap"])
        self.assertEqual(sorted(metadata["shaderTypes"]), ["FDepthOnlyPS", "TBasePassPSFNoLightMapPolicy"])
        self.assertEqual(metadata["vertexFactories"], ["FGPUSkinVertexFactoryShaderParameters"])
        self.assertEqual(references[0]["targetPath"], "Game/Content/Art/Materials/M_CrowFeather.uasset")


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name) / "unpacked"
        materials = root / "Game" / "Content" / "Art" / "Materials"
        textures = root / "Game" / "Content" / "Art" / "Textures"
        materials.mkdir(parents=True)
        textures.mkdir(parents=True)
        (root / "Game" / "Game.uproject").write_text("{}", "utf-8")
        (materials / "M_CrowFeather.uasset").write_bytes(crow_material())
        (materials / "M_CrowFeather.uexp").write_bytes(uexp_blob(
            ["FDepthOnlyPS", "TBasePassPSFNoLightMapPolicy"], [12.354754, 0.7318211, 3.1415927, 0.5, 1.0]))
        (materials / "MI_CrowFeather.uasset").write_bytes(crow_instance())
        (materials / "M_Rock.uasset").write_bytes(rock_material())
        (materials / "M_Rock.uexp").write_bytes(uexp_blob(["FDepthOnlyPS"], [0.4471231, 9.812345]))
        for name in ("T_Crow_Body_D", "T_Crow_Body_N", "T_Rock_D"):
            (textures / (name + ".uasset")).write_bytes(b"\x00" * 16)
        self.root = root
        self.catalog = AssetCatalogService(Path(self.temporary.name) / "catalog.sqlite3")
        self.scan = self.catalog.scan(root, build_id="crow-build", scan_mode="full")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scan_records_engine_relationships_and_tokens(self) -> None:
        self.assertEqual(self.scan["engine"]["engine"], "unreal")
        self.assertEqual(self.scan["countsByKind"]["package"], 8)
        self.assertGreaterEqual(self.scan["verifiedRelationshipCount"], 4)  # 3 textures + MI parent + uexp siblings
        status = self.catalog.status()
        self.assertEqual((status["engine"], status["tokenIndex"]), ("unreal", "fts5"))
        found = self.catalog.query(text="MI_CrowFeather", limit=5)
        instance = found["assets"][0]
        parents = [item for item in instance["relationships"] if item["relation"] == "parent-material"]
        self.assertEqual(parents[0]["targetPath"], "Game/Content/Art/Materials/M_CrowFeather.uasset")
        self.assertTrue(parents[0]["verified"])

    def test_texture_names_drive_retrieval_and_memory_short_circuits(self) -> None:
        tokens, evidence = texture_name_tokens(["Texture2D 812 T_Crow_Body_D", "T_Crow_Body_N", "SceneDepth", "white"])
        self.assertIn("crow_body", tokens)
        self.assertFalse([item for item in evidence if item["texture"] == "white"][0]["used"])
        features = {"stage": "fragment", "boundTextureNames": ["Texture2D 812 T_Crow_Body_D", "T_Crow_Body_N"],
                    "passHint": "basepass", "meshHint": "skeletal", "textureSlotCount": 2}
        result = retrieve(self.catalog, features, top_k=3)
        self.assertEqual(result["candidates"][0]["relativePath"], "Game/Content/Art/Materials/M_CrowFeather.uasset")
        self.assertEqual(result["decisionHint"], "clear leader")
        leader = result["candidates"][0]
        self.assertIn("crow", leader["matchedTokens"])
        self.assertTrue(all(check["agrees"] for check in leader["consistency"]), leader["consistency"])
        self.assertIn("TBasePassPSFNoLightMapPolicy", leader["document"]["shaderTypes"])
        self.assertNotIn("M_Rock", [c["relativePath"] for c in result["candidates"][:1]])
        empty = retrieve(self.catalog, {"stage": "fragment", "boundTextureNames": ["white"]}, top_k=3)
        self.assertEqual(empty["candidates"], [])
        store = ShaderAssetResolutionStore(Path(self.temporary.name) / "resolutions.sqlite3")
        shape = structural_key("fragment", [48], 2, 1, 0, "MainPS")
        store.record(game_key="crow@1", shader_raw_sha256="a" * 64, structural=shape, stage="fragment",
                     entry_point="MainPS", asset_relative_path=leader["relativePath"],
                     asset_content_sha256=leader["contentSha256"], asset_class="Material",
                     catalog_sha256=result["catalogSha256"], negative=False, confidence=0.9,
                     decided_by="ai", rationale="crow textures", evidence={})
        store.record(game_key="crow@1", shader_raw_sha256="a" * 64, structural=shape, stage="fragment",
                     entry_point="MainPS", asset_relative_path="Game/Content/Art/Materials/M_Rock.uasset",
                     asset_content_sha256="", asset_class="Material", catalog_sha256="", negative=True,
                     confidence=0.9, decided_by="ai", rationale="rock is wrong", evidence={})
        memory = store.lookup("crow@1", "a" * 64, shape, "fragment", "MainPS")
        self.assertTrue(memory["resolved"])
        self.assertEqual(memory["resolution"]["assetRelativePath"], leader["relativePath"])
        self.assertEqual(len(memory["rejected"]), 1)
        similar = store.lookup("crow@1", "b" * 64, shape, "fragment", "MainPS")
        self.assertFalse(similar["resolved"])
        self.assertEqual(similar["sameShapeSuggestions"][0]["assetRelativePath"], leader["relativePath"])
        self.assertEqual(store.forget("crow@1", "a" * 64, "fragment", "MainPS"), 2)


if __name__ == "__main__":
    unittest.main()


class ValueMatchingTests(RetrievalTests):
    def test_shared_complex_values_identify_the_material_without_texture_names(self) -> None:
        # No usable texture names at all: only the cbuffer lanes speak.
        lanes = [{"bind": 2, "space": 0, "lane": 4, "value": 12.354754},
                 {"bind": 2, "space": 0, "lane": 5, "value": 0.7318211},
                 {"bind": 2, "space": 0, "lane": 6, "value": 3.1415927},
                 {"bind": 2, "space": 0, "lane": 9, "value": 0.5}]
        result = retrieve(self.catalog, {"stage": "fragment", "boundTextureNames": ["white"],
                                         "constantBufferLanes": lanes}, top_k=3)
        self.assertTrue(result["candidates"], result)
        leader = result["candidates"][0]
        self.assertEqual(leader["relativePath"], "Game/Content/Art/Materials/M_CrowFeather.uasset")
        self.assertEqual(leader["matchedTokens"], [])
        values = leader["facets"]["values"]
        self.assertEqual(values["rareMatchedCount"], 3)
        self.assertTrue(values["adjacentRuns"], values)
        self.assertGreaterEqual(values["adjacentRuns"][0]["consecutiveLanes"], 3)
        self.assertGreaterEqual(leader["confidence"], 0.9)
        self.assertIn("adjacent run", leader["why"])
        self.assertIn("b2:s0 lane 4", values["laneRefs"]["12.35475"])
        # A single popular/round value is not evidence.
        weak = retrieve(self.catalog, {"stage": "fragment", "boundTextureNames": ["white"],
                                       "constantBufferLanes": [{"bind": 0, "space": 0, "lane": 0, "value": 0.5}]}, top_k=3)
        self.assertEqual(weak["candidates"], [])
        # Values lift a name match that textures alone left uncertain.
        mixed = retrieve(self.catalog, {"stage": "fragment", "boundTextureNames": ["T_Crow_Body_D"],
                                        "constantBufferLanes": lanes[:1]}, top_k=3)
        self.assertEqual(mixed["candidates"][0]["relativePath"], "Game/Content/Art/Materials/M_CrowFeather.uasset")
        self.assertEqual(mixed["candidates"][0]["facets"]["values"]["matched"], ["12.35475"])


class MemberRuleTests(unittest.TestCase):
    def test_member_rules_are_remembered_per_family_and_prefer_higher_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = ShaderAssetResolutionStore(Path(temporary) / "r.sqlite3")
            members = [{"space": 0, "bind": 2, "byteOffset": 0, "byteSize": 16, "name": "BaseColorTint"},
                       {"space": 0, "bind": 2, "byteOffset": 32, "byteSize": 4, "name": "Cutoff"},
                       {"space": 0, "bind": 2, "byteOffset": 36, "byteSize": 4, "name": "not valid!"}]
            written = store.record_member_rules(game_key="g@1", family_keys=["Mat/M_A.uasset", "Mat/M_Parent.uasset"],
                                                members=members, source="ai-rename", confidence=0.7)
            self.assertEqual(written, 4)
            rules = store.member_rules("g@1", ["Mat/M_Parent.uasset"])
            self.assertEqual([f["name"] for f in rules["0:2"]], ["BaseColorTint", "Cutoff"])
            self.assertEqual(rules["0:2"][0]["columns"], 4)
            # lower confidence never overwrites, higher does
            store.record_member_rules(game_key="g@1", family_keys=["Mat/M_Parent.uasset"],
                                      members=[{"space": 0, "bind": 2, "byteOffset": 32, "byteSize": 4, "name": "Worse"}],
                                      source="ai-rename", confidence=0.3)
            store.record_member_rules(game_key="g@1", family_keys=["Mat/M_Parent.uasset"],
                                      members=[{"space": 0, "bind": 2, "byteOffset": 0, "byteSize": 16, "name": "BaseColor"}],
                                      source="user", confidence=0.95)
            rules = store.member_rules("g@1", ["Mat/M_Parent.uasset"])
            self.assertEqual([f["name"] for f in rules["0:2"]], ["BaseColor", "Cutoff"])
            self.assertEqual(store.member_rules("other", ["Mat/M_Parent.uasset"]), {})


class GenericEngineTests(unittest.TestCase):
    """An unrecognized engine: no import tables, only the generic probe."""

    def test_generic_probe_indexes_strings_numbers_and_name_mentions(self) -> None:
        from tests.test_texture_identity import gradient_rgba, png_bytes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "custom"
            (root / "materials").mkdir(parents=True)
            (root / "textures").mkdir(parents=True)
            blob = (b"MATL\x00\x01" + b"T_Crow_Body_D\x00" + b"FeatherSheen\x00" + b"IridescenceStrength\x00"
                    + struct.pack("<fff", 12.354754, 0.7318211, 3.1415927) + b"textures/T_Crow_Body_N.tex\x00")
            (root / "materials" / "crow_feather.matbin").write_bytes(blob)
            (root / "materials" / "rock.matbin").write_bytes(b"MATL\x00\x01Granite\x00" + struct.pack("<f", 9.812345))
            (root / "textures" / "T_Crow_Body_D.png").write_bytes(png_bytes(gradient_rgba(16, 16), 16, 16))
            (root / "textures" / "T_Crow_Body_N.tex").write_bytes(b"\x00" * 64)
            catalog = AssetCatalogService(Path(temporary) / "catalog.sqlite3")
            result = catalog.scan(root, build_id="custom", depth="headers")
            self.assertEqual(result["engine"]["engine"], "unknown")
            found = catalog.query(text="crow_feather", limit=5)
            material = found["assets"][0]
            self.assertEqual(material["metadata"]["parser"], "generic-content-probe/v1")
            self.assertIn("FeatherSheen", material["metadata"]["strings"])
            self.assertIn("12.35475", material["metadata"]["numericValues"])
            targets = sorted(item["targetPath"] for item in material["relationships"] if item["relation"] == "mentions-asset")
            self.assertEqual(targets, ["textures/T_Crow_Body_D.png", "textures/T_Crow_Body_N.tex"])
            retrieval = retrieve(catalog, {"stage": "fragment", "boundTextureNames": ["T_Crow_Body_D"],
                                           "constantBufferLanes": [
                                               {"bind": 0, "space": 0, "lane": 0, "value": 12.354754},
                                               {"bind": 0, "space": 0, "lane": 1, "value": 0.7318211}]}, top_k=3)
            self.assertEqual(retrieval["candidates"][0]["relativePath"], "materials/crow_feather.matbin")
            self.assertIn("crow", retrieval["candidates"][0]["matchedTokens"])
            self.assertEqual(retrieval["candidates"][0]["facets"]["values"]["rareMatchedCount"], 2)
            self.assertTrue(retrieval["candidates"][0]["facets"]["values"]["adjacentRuns"])
            self.assertGreaterEqual(catalog.coverage()["thumbnailRows"], 1)
