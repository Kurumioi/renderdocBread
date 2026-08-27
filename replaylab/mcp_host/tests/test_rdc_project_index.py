from __future__ import annotations

import copy
import hashlib
import json
import unittest

from src.rdc_project_index import (
    MAX_EVENT_IDS_PER_SHADER,
    MAX_PROJECT_BINDINGS,
    RDC_PROJECT_INDEX_SCHEMA,
    RDC_PROJECT_INDEX_VERSION,
    RdcProjectIndexError,
    binding_knowledge_is_current,
    encode,
    encode_index,
    new,
    new_index,
    parse,
    parse_index,
    remove,
    remove_project_binding,
    select,
    select_project_binding,
    upsert,
    upsert_project_binding,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _id(label: str) -> str:
    return _sha(label)[:32]


def _shader(
    label: str = "shader",
    *,
    resource_id: int = 7,
    event_ids: list[int] | None = None,
    workspace_path: str | None = None,
) -> dict:
    return {
        "identity": {
            "resourceId": resource_id,
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": _sha(label + ":captured-raw"),
            "codeSha256": _sha(label + ":captured-code"),
            "interfaceHash": _sha(label + ":interface"),
            "layoutHash": _sha(label + ":layout"),
            "interfaceComplete": True,
            "eventIds": [9, 3] if event_ids is None else event_ids,
        },
        "knowledgeId": _id(label + ":knowledge"),
        "managedWorkspace": {
            "workspaceId": _id(label + ":managed-workspace"),
            "path": workspace_path
            or "shader-workspaces/%s" % _id(label + ":managed-workspace"),
        },
        "candidateRevision": 3,
        "candidateRawSha256": _sha(label + ":candidate-raw"),
        "sourceSha256": _sha(label + ":source"),
    }


def _material(label: str = "material", *, shader_resource_id: int = 7) -> dict:
    return {
        "knowledgeId": _id(label + ":knowledge"),
        "shaderResourceId": shader_resource_id,
        "stage": "fragment",
        "blockIndex": 0,
        "path": "PerMaterial",
        "layoutSha256": _sha(label + ":layout"),
        "eventId": 9,
        "parameters": [
            {"parameterId": _id(label + ":roughness")},
            {
                "parameterId": _id(label + ":base-color"),
                "path": "BaseColor",
                "baseType": "Float",
                "components": 4,
                "arrayIndex": 0,
                "byteOffset": 0,
                "byteSize": 16,
            },
        ],
    }


def _binding(
    label: str = "binding",
    *,
    project_id: str | None = None,
    workspace_id: str | None = None,
    knowledge_hash: str | None = None,
    with_records: bool = True,
) -> dict:
    return {
        "projectId": project_id or _id(label + ":project"),
        "workspaceId": workspace_id or _id(label + ":workspace"),
        "scopeSha256": _sha(label + ":scope"),
        "captureId": _sha(label + ":capture"),
        "catalogSha256": _sha(label + ":catalog"),
        "knowledgeContentSha256": knowledge_hash or _sha(label + ":knowledge-content"),
        "matcherPolicySha256": _sha(label + ":matcher-policy"),
        "shaders": [_shader(label)] if with_records else [],
        "materials": [_material(label)] if with_records else [],
    }


class RdcProjectIndexTests(unittest.TestCase):
    def test_new_index_round_trips_as_canonical_json(self) -> None:
        index = new_index()
        payload = encode_index(index)

        self.assertEqual(parse_index(payload), index)
        self.assertEqual(payload, encode_index(parse_index(payload)))
        decoded = json.loads(payload.decode("utf-8"))
        self.assertEqual(decoded["schema"], RDC_PROJECT_INDEX_SCHEMA)
        self.assertEqual(decoded["schemaVersion"], RDC_PROJECT_INDEX_VERSION)
        self.assertEqual(decoded["bindings"], [])

        unsigned = dict(decoded)
        seal = unsigned.pop("sealSha256")
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(seal, hashlib.sha256(canonical).hexdigest())

    def test_short_public_apis_match_descriptive_apis(self) -> None:
        binding = _binding()
        index = upsert(new(), binding)
        self.assertEqual(encode(index), encode_index(index))
        self.assertEqual(parse(encode(index)), parse_index(encode_index(index)))
        self.assertEqual(
            select(index, binding["projectId"], binding["workspaceId"]),
            select_project_binding(
                index, binding["projectId"], binding["workspaceId"]
            ),
        )
        self.assertEqual(
            remove(index, binding["projectId"], binding["workspaceId"]),
            remove_project_binding(
                index, binding["projectId"], binding["workspaceId"]
            ),
        )

    def test_upsert_normalises_and_selects_complete_records(self) -> None:
        binding = _binding()
        original = copy.deepcopy(binding)
        index = upsert_project_binding(new_index(), binding)

        # Public mutation APIs do not mutate caller-owned objects.
        self.assertEqual(binding, original)
        selected = select_project_binding(
            index,
            binding["projectId"],
            binding["workspaceId"],
            knowledge_content_sha256=binding["knowledgeContentSha256"],
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["shaders"][0]["identity"]["eventIds"], [3, 9])
        self.assertEqual(
            [item["parameterId"] for item in selected["materials"][0]["parameters"]],
            sorted(item["parameterId"] for item in binding["materials"][0]["parameters"]),
        )
        self.assertEqual(
            selected["shaders"][0]["managedWorkspace"]["path"],
            binding["shaders"][0]["managedWorkspace"]["path"],
        )

        selected["captureId"] = _sha("mutated-copy")
        self.assertNotEqual(index["bindings"][0]["captureId"], selected["captureId"])

    def test_empty_binding_is_valid_and_can_be_populated_later(self) -> None:
        empty = _binding(with_records=False)
        index = upsert_project_binding(new_index(), empty)
        self.assertEqual(index["bindings"][0]["shaders"], [])
        self.assertEqual(index["bindings"][0]["materials"], [])

        populated = _binding(
            project_id=empty["projectId"], workspace_id=empty["workspaceId"]
        )
        index = upsert_project_binding(index, populated)
        self.assertEqual(len(index["bindings"]), 1)
        self.assertEqual(len(index["bindings"][0]["shaders"]), 1)

    def test_upsert_replaces_exact_key_and_sorts_distinct_bindings(self) -> None:
        first = _binding("first", project_id="f" * 32, workspace_id="f" * 32)
        replacement = copy.deepcopy(first)
        replacement["knowledgeContentSha256"] = _sha("replacement-knowledge")
        second = _binding("second", project_id="0" * 32, workspace_id="1" * 32)

        index = upsert_project_binding(new_index(), first)
        index = upsert_project_binding(index, replacement)
        index = upsert_project_binding(index, second)

        self.assertEqual(len(index["bindings"]), 2)
        self.assertEqual(index["bindings"][0]["projectId"], "0" * 32)
        selected = select_project_binding(index, "f" * 32, "f" * 32)
        assert selected is not None
        self.assertEqual(
            selected["knowledgeContentSha256"], replacement["knowledgeContentSha256"]
        )

    def test_knowledge_hash_invalidates_selection(self) -> None:
        binding = _binding()
        index = upsert_project_binding(new_index(), binding)

        self.assertTrue(
            binding_knowledge_is_current(binding, binding["knowledgeContentSha256"])
        )
        self.assertFalse(binding_knowledge_is_current(binding, _sha("stale")))
        self.assertIsNone(
            select_project_binding(
                index,
                binding["projectId"],
                binding["workspaceId"],
                knowledge_content_sha256=_sha("stale"),
            )
        )

    def test_remove_one_workspace_or_all_project_bindings(self) -> None:
        project_id = _id("shared-project")
        one = _binding("one", project_id=project_id, workspace_id=_id("workspace-one"))
        two = _binding("two", project_id=project_id, workspace_id=_id("workspace-two"))
        other = _binding("other")
        index = new_index()
        for binding in (one, two, other):
            index = upsert_project_binding(index, binding)

        index = remove_project_binding(index, project_id, one["workspaceId"])
        self.assertIsNone(select_project_binding(index, project_id, one["workspaceId"]))
        self.assertIsNotNone(select_project_binding(index, project_id, two["workspaceId"]))

        index = remove_project_binding(index, project_id)
        self.assertIsNone(select_project_binding(index, project_id, two["workspaceId"]))
        self.assertIsNotNone(
            select_project_binding(index, other["projectId"], other["workspaceId"])
        )
        self.assertEqual(remove_project_binding(index, project_id), index)

    def test_duplicate_json_keys_and_non_finite_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(RdcProjectIndexError, "duplicate JSON key"):
            parse_index(b'{"schema":"one","schema":"two"}')
        with self.assertRaisesRegex(RdcProjectIndexError, "non-finite"):
            parse_index(b'{"schema":NaN}')

    def test_tampering_and_non_v1_or_extra_fields_are_rejected(self) -> None:
        binding = _binding()
        index = upsert_project_binding(new_index(), binding)
        tampered = copy.deepcopy(index)
        tampered["bindings"][0]["catalogSha256"] = _sha("tampered")
        with self.assertRaisesRegex(RdcProjectIndexError, "sealSha256"):
            encode_index(tampered)

        wrong_version = copy.deepcopy(index)
        wrong_version["schemaVersion"] = 2
        with self.assertRaisesRegex(RdcProjectIndexError, "schema/version"):
            encode_index(wrong_version)

        boolean_version = copy.deepcopy(index)
        boolean_version["schemaVersion"] = True
        with self.assertRaisesRegex(RdcProjectIndexError, "schema/version"):
            encode_index(boolean_version)

        extra = copy.deepcopy(binding)
        extra["unexpected"] = True
        with self.assertRaisesRegex(RdcProjectIndexError, "fields"):
            upsert_project_binding(new_index(), extra)

    def test_managed_workspace_path_must_be_safe_project_relative(self) -> None:
        unsafe_paths = (
            "/absolute/path",
            "C:/absolute/path",
            "../escape",
            "nested/../escape",
            "nested/./alias",
            "nested\\windows",
            "nested//empty",
            "nested/trailing/",
        )
        for unsafe in unsafe_paths:
            with self.subTest(path=unsafe):
                binding = _binding()
                binding["shaders"][0]["managedWorkspace"]["path"] = unsafe
                with self.assertRaisesRegex(RdcProjectIndexError, "project-relative|root"):
                    upsert_project_binding(new_index(), binding)

    def test_material_path_must_not_be_absolute(self) -> None:
        for unsafe in ("/PerMaterial", r"C:\\PerMaterial"):
            with self.subTest(path=unsafe):
                binding = _binding()
                binding["materials"][0]["path"] = unsafe
                with self.assertRaisesRegex(RdcProjectIndexError, "absolute"):
                    upsert_project_binding(new_index(), binding)

    def test_parameter_summary_supports_id_only_but_validates_typed_entries(self) -> None:
        # The fixture contains both a typed summary and an intentionally ID-only hint.
        index = upsert_project_binding(new_index(), _binding())
        parameters = index["bindings"][0]["materials"][0]["parameters"]
        self.assertEqual({len(item) for item in parameters}, {1, 7})

        invalid = _binding()
        typed = next(
            item for item in invalid["materials"][0]["parameters"] if len(item) > 1
        )
        typed["byteSize"] = 12
        with self.assertRaisesRegex(RdcProjectIndexError, "byteSize"):
            upsert_project_binding(new_index(), invalid)

    def test_candidate_revision_is_a_strict_positive_integer(self) -> None:
        for invalid_revision in (0, -1, True, "3", _sha("not-a-revision")):
            with self.subTest(revision=invalid_revision):
                binding = _binding()
                binding["shaders"][0]["candidateRevision"] = invalid_revision
                with self.assertRaisesRegex(RdcProjectIndexError, "candidateRevision"):
                    upsert_project_binding(new_index(), binding)

    def test_material_stage_is_canonical_and_bound_to_the_same_shader_stage(self) -> None:
        unknown = _binding()
        unknown["materials"][0]["stage"] = "not-a-renderdoc-stage"
        with self.assertRaisesRegex(RdcProjectIndexError, "RenderDoc stage"):
            upsert_project_binding(new_index(), unknown)

        wrong_stage = _binding()
        wrong_stage["materials"][0]["stage"] = "vertex"
        with self.assertRaisesRegex(RdcProjectIndexError, "Shader/stage absent"):
            upsert_project_binding(new_index(), wrong_stage)

    def test_duplicate_associations_and_unknown_material_shader_are_rejected(self) -> None:
        duplicate_shader = _binding()
        alternate = copy.deepcopy(duplicate_shader["shaders"][0])
        alternate["knowledgeId"] = _id("different-knowledge")
        duplicate_shader["shaders"].append(alternate)
        with self.assertRaisesRegex(RdcProjectIndexError, "duplicate associations"):
            upsert_project_binding(new_index(), duplicate_shader)

        duplicate_material = _binding()
        alternate_material = copy.deepcopy(duplicate_material["materials"][0])
        alternate_material["knowledgeId"] = _id("different-material-knowledge")
        duplicate_material["materials"].append(alternate_material)
        with self.assertRaisesRegex(RdcProjectIndexError, "duplicate associations"):
            upsert_project_binding(new_index(), duplicate_material)

        absent_shader = _binding()
        absent_shader["materials"][0]["shaderResourceId"] = 999
        with self.assertRaisesRegex(RdcProjectIndexError, "Shader/stage absent"):
            upsert_project_binding(new_index(), absent_shader)

    def test_record_and_binding_limits_are_enforced(self) -> None:
        too_many_events = _binding()
        too_many_events["shaders"][0]["identity"]["eventIds"] = list(
            range(1, MAX_EVENT_IDS_PER_SHADER + 2)
        )
        with self.assertRaisesRegex(RdcProjectIndexError, "eventIds"):
            upsert_project_binding(new_index(), too_many_events)

        index = new_index()
        for number in range(MAX_PROJECT_BINDINGS):
            binding = _binding(
                "binding-%d" % number,
                project_id="%032x" % number,
                workspace_id="%032x" % number,
                with_records=False,
            )
            index = upsert_project_binding(index, binding)
        with self.assertRaisesRegex(RdcProjectIndexError, "binding limit"):
            upsert_project_binding(
                index,
                _binding(
                    "overflow",
                    project_id="%032x" % MAX_PROJECT_BINDINGS,
                    workspace_id="%032x" % MAX_PROJECT_BINDINGS,
                    with_records=False,
                ),
            )


if __name__ == "__main__":
    unittest.main()
