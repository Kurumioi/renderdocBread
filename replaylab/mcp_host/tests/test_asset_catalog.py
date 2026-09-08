from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest

from src.asset_catalog import AssetCatalogError, AssetCatalogService


def _png(width: int, height: int, *, srgb: bool = True) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0) + b"\x00\x00\x00\x00"
    chunks = ihdr
    if srgb:
        chunks += struct.pack(">I", 1) + b"sRGB" + b"\x00" + b"\x00\x00\x00\x00"
    chunks += struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00"
    return signature + chunks


class AssetCatalogTests(unittest.TestCase):
    def test_indexes_metadata_relationships_and_verified_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "project" / "shared" / "assets"
            (root / "models").mkdir(parents=True)
            (root / "textures").mkdir()
            (root / "materials").mkdir()
            (root / "shaders").mkdir()
            managed.mkdir(parents=True)
            (root / "textures" / "hero.png").write_bytes(_png(64, 32))
            (root / "materials" / "hero.mtl").write_text(
                "newmtl Hero\nmap_Kd ../textures/hero.png\n", encoding="utf-8"
            )
            (root / "models" / "hero.obj").write_text(
                "mtllib ../materials/hero.mtl\n"
                "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
                "vt 0 0\nvt 1 0\nvt 0 1\n"
                "vn 0 0 1\nf 1/1/1 2/2/1 3/3/1\n",
                encoding="utf-8",
            )
            (root / "shaders" / "common.hlsli").write_text(
                "float4 tint;\n", encoding="utf-8"
            )
            (root / "shaders" / "hero.hlsl").write_text(
                '#include "common.hlsli"\nfloat4 main():SV_Target{return tint;}\n',
                encoding="utf-8",
            )
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            result = service.scan(root, build_id="build-1")
            self.assertEqual(result["assetCount"], 5)
            self.assertGreaterEqual(result["verifiedRelationshipCount"], 3)
            query = service.query(text="hero", limit=20)
            by_path = {item["relativePath"]: item for item in query["assets"]}
            texture = by_path["textures/hero.png"]
            self.assertEqual(texture["metadata"]["width"], 64)
            self.assertEqual(texture["metadata"]["height"], 32)
            self.assertEqual(texture["metadata"]["colorSpaceEvidence"], "srgb-chunk")
            obj = by_path["models/hero.obj"]
            self.assertEqual(obj["metadata"]["faceCount"], 1)
            self.assertTrue(
                any(
                    relation["relation"] == "uses-material-library"
                    and relation["verified"] is True
                    for relation in obj["relationships"]
                )
            )
            delivery = service.resolve(
                root,
                asset_id=texture["assetId"],
                expected_catalog_sha256=result["catalogSha256"],
            )
            self.assertTrue(delivery["verified"])
            self.assertEqual(Path(delivery["absolutePath"]), root / "textures" / "hero.png")

    def test_incremental_scan_reuses_unchanged_content_and_full_rehashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            root.mkdir()
            managed.mkdir()
            (root / "a.bin").write_bytes(b"same")
            (root / "b.bin").write_bytes(b"same")
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            first = service.scan(root, build_id="b")
            second = service.scan(root, build_id="b", scan_mode="incremental")
            self.assertEqual(first["catalogSha256"], second["catalogSha256"])
            self.assertEqual(second["cache"]["reusedFiles"], 2)
            self.assertEqual(second["cache"]["hashedFiles"], 0)
            duplicate = service.query(
                sha256=hashlib.sha256((root / "a.bin").read_bytes()).hexdigest(),
                limit=20,
            )
            self.assertEqual(duplicate["count"], 2)
            full = service.scan(root, build_id="b", scan_mode="full")
            self.assertEqual(full["cache"]["hashedFiles"], 2)
            rows = service.query(kinds=["binary"], limit=20)["assets"]
            self.assertNotEqual(rows[0]["assetId"], rows[1]["assetId"])
            self.assertTrue(
                any(
                    relation["relation"] == "same-content" and relation["verified"]
                    for row in rows
                    for relation in row["relationships"]
                )
            )

    def test_interrupted_scan_leaves_a_resumable_checkpoint(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            root.mkdir()
            managed.mkdir()
            for index in range(12):
                (root / ("f%02d.bin" % index)).write_bytes(b"content-%d" % index)
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            # Every file trips the checkpoint clock; the sixth checkpoint aborts the scan.
            calls = {"n": 0}
            original = service.scan

            def _explode_after(count):
                def _progress(update):
                    if update.get("phase") == "checkpoint":
                        calls["n"] += 1
                        if calls["n"] >= count:
                            raise KeyboardInterrupt()  # simulate the task process dying
                return _progress

            with self.assertRaises(KeyboardInterrupt):
                original(root, build_id="b", checkpoint_seconds=0.0001, progress=_explode_after(6))
            status = service.status()
            self.assertFalse(status["complete"])
            self.assertGreaterEqual(status["assetCount"], 5)
            self.assertLess(status["assetCount"], 12)
            self.assertIn("walked", status["progress"])
            # Resume: the checkpointed files are reused, only the rest is hashed.
            resumed = service.scan(root, build_id="b", checkpoint_seconds=0)
            self.assertTrue(resumed["complete"])
            self.assertEqual(resumed["assetCount"], 12)
            self.assertGreaterEqual(resumed["cache"]["reusedFiles"], 5)
            self.assertEqual(resumed["cache"]["reusedFiles"] + resumed["cache"]["hashedFiles"], 12)
            self.assertTrue(service.status()["complete"])
            # A checkpoint never shrinks coverage below the previous complete catalog.
            (root / "f00.bin").write_bytes(b"changed")
            calls["n"] = 0
            with self.assertRaises(KeyboardInterrupt):
                service.scan(root, build_id="b", checkpoint_seconds=0.0001, progress=_explode_after(2))
            partial = service.status()
            self.assertFalse(partial["complete"])
            self.assertEqual(partial["assetCount"], 12)

    def test_failed_bounded_scan_preserves_previous_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            root.mkdir()
            managed.mkdir()
            (root / "first.obj").write_text("v 0 0 0\n", encoding="utf-8")
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            accepted = service.scan(root)
            (root / "second.obj").write_text("v 1 0 0\n", encoding="utf-8")
            with self.assertRaises(AssetCatalogError):
                service.scan(root, max_files=1)
            status = service.status()
            self.assertEqual(status["catalogSha256"], accepted["catalogSha256"])
            self.assertEqual(status["assetCount"], 1)

    def test_delivery_rejects_stale_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            root.mkdir()
            managed.mkdir()
            path = root / "hero.obj"
            path.write_text("v 0 0 0\n", encoding="utf-8")
            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            indexed = service.scan(root)
            item = service.query(text="hero")["assets"][0]
            path.write_text("v 9 9 9\n", encoding="utf-8")
            with self.assertRaises(AssetCatalogError):
                service.resolve(
                    root,
                    asset_id=item["assetId"],
                    expected_catalog_sha256=indexed["catalogSha256"],
                )

    def test_indexes_uex_bundle_metadata_and_transitive_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "assets"
            managed = Path(temporary) / "managed"
            bundle = root / "bundle"
            (bundle / "meshes").mkdir(parents=True)
            (bundle / "materials").mkdir()
            (bundle / "textures").mkdir()
            managed.mkdir()
            (bundle / "meshes" / "lod0.glb").write_bytes(b"glTF")
            (bundle / "materials" / "body.json").write_text("{}\n", encoding="utf-8")
            (bundle / "textures" / "base.png").write_bytes(_png(8, 8))
            (bundle / "textures" / "normal.png").write_bytes(_png(8, 8, srgb=False))
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "uex.blender-bundle.v1",
                        "sourceAsset": "/Game/Hero/SK_Hero",
                        "assetType": "USkeletalMesh",
                        "meshName": "Hero",
                        "skeleton": "/Game/Hero/SKEL_Hero",
                        "boneCount": 32,
                        "sourceLodCount": 1,
                        "morphTargetCount": 2,
                        "meshes": [{"lodIndex": 0, "relativePath": "meshes/lod0.glb"}],
                        "materials": [
                            {
                                "materialJson": "materials/body.json",
                                "rawMaterialJson": None,
                                "baseColor": {"relativePath": "textures/base.png"},
                                "directTextureParameters": [
                                    {"relativePath": "textures/normal.png"}
                                ],
                                "textureParameters": [],
                            }
                        ],
                        "textures": [
                            {"relativePath": "textures/base.png"},
                            {"relativePath": "textures/normal.png"},
                        ],
                        "warnings": [],
                    }
                ),
                encoding="utf-8",
            )

            service = AssetCatalogService(managed / "asset-catalog-v1.sqlite3")
            service.scan(root)
            item = service.query(text="manifest.json", kinds=["metadata"])["assets"][0]

            self.assertEqual(item["metadata"]["parser"], "uex.blender-bundle.v1")
            self.assertEqual(item["metadata"]["assetType"], "USkeletalMesh")
            self.assertEqual(item["metadata"]["sourceAsset"], "/Game/Hero/SK_Hero")
            relations = {(edge["relation"], edge["targetPath"]): edge for edge in item["relationships"]}
            self.assertTrue(relations[("contains-lod-geometry", "bundle/meshes/lod0.glb")]["verified"])
            self.assertTrue(relations[("uses-material-record", "bundle/materials/body.json")]["verified"])
            self.assertTrue(relations[("uses-texture-parameter", "bundle/textures/normal.png")]["verified"])

    @unittest.skipUnless(hasattr(os, "symlink"), "symbolic links unavailable")
    def test_child_symlink_is_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "assets"
            managed = base / "managed"
            root.mkdir()
            managed.mkdir()
            outside = base / "outside.bin"
            outside.write_bytes(b"secret")
            link = root / "redirect.bin"
            try:
                os.symlink(outside, link)
            except OSError:
                self.skipTest("test process cannot create symbolic links")
            result = AssetCatalogService(managed / "asset-catalog-v1.sqlite3").scan(root)
            self.assertEqual(result["assetCount"], 0)
            self.assertEqual(result["diagnostics"]["skippedSymlinks"], 1)


if __name__ == "__main__":
    unittest.main()
