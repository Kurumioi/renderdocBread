from __future__ import annotations

import unittest

from src.shader_workspace.restructure import RestructureError, apply_operations, extract_function, structure_report

SOURCE = "\r\n".join([
    "cbuffer View : register(b0)",
    "{",
    "    float4 ViewSize : packoffset(c0);",
    "    float Exposure : packoffset(c1.x);",
    "};",
    "Texture2D<float4> BaseColor : register(t0);",
    "SamplerState Linear : register(s0);",
    "static float2 in_uv;",
    "static float4 out_color;",
    "",
    "void frag_main()",
    "{",
    "    float2 _10 = in_uv * ViewSize.xy;",          # 13
    "    float4 _11 = BaseColor.Sample(Linear, _10);",  # 14
    "    float _12 = _11.x * Exposure;",                # 15
    "    float _13 = _11.y * Exposure;",                # 16
    "    float3 _14 = float3(_12, _13, _11.z);",        # 17
    "    float _15 = dot(_14, float3(0.2126f, 0.7152f, 0.0722f));",  # 18
    "    if (_15 > 1.0f)",                                # 19
    "    {",                                              # 20
    "        _14 = _14 / _15;",                           # 21
    "    }",                                              # 22
    "    out_color = float4(_14, 1.0f);",                 # 23
    "}",
    "",
    "float4 MainPS(float2 uv : TEXCOORD0) : SV_Target0",
    "{",
    "    in_uv = uv;",
    "    frag_main();",
    "    return out_color;",
    "}",
    "",
])


class ExtractTests(unittest.TestCase):
    def test_extract_returns_single_output_and_passes_inputs(self) -> None:
        result = extract_function(SOURCE, 15, 17, "ApplyExposure", comment="Scale the sampled colour by exposure.")
        info = result["info"]
        self.assertEqual(info["inputs"], ["_11"])
        self.assertEqual(info["returns"], "_14")
        self.assertEqual(info["returnType"], "float3")
        text = result["source"]
        self.assertIn("float3 ApplyExposure(float4 _11)", text)
        self.assertIn("    return _14;", text)
        self.assertIn("    float3 _14 = ApplyExposure(_11);", text)
        self.assertIn("// Scale the sampled colour by exposure.", text)
        # the helper is placed above frag_main and the file still has both callers
        self.assertLess(text.index("float3 ApplyExposure"), text.index("void frag_main"))
        self.assertNotIn("float _12 = _11.x * Exposure;\r\n    float _13", text.split("void frag_main")[1])

    def test_extract_with_inout_and_block(self) -> None:
        result = extract_function(SOURCE, 18, 22, "NormalizeLuminance")
        info = result["info"]
        self.assertEqual(info["inouts"], ["_14"])
        self.assertEqual(info["inputs"], [])
        self.assertEqual(info["returns"], None)
        self.assertIn("void NormalizeLuminance(inout float3 _14)", result["source"])
        self.assertIn("    NormalizeLuminance(_14);", result["source"])

    def test_multiple_outputs_become_out_parameters(self) -> None:
        result = extract_function(SOURCE, 15, 16, "SplitChannels")
        info = result["info"]
        self.assertEqual(info["outParams"], ["_12", "_13"])
        self.assertIn("void SplitChannels(float4 _11, out float _12, out float _13)", result["source"])
        self.assertIn("    float _12;\r\n    float _13;\r\n    SplitChannels(_11, _12, _13);", result["source"])

    def test_unsafe_ranges_are_refused_with_reasons(self) -> None:
        with self.assertRaisesRegex(RestructureError, "complete blocks"):
            extract_function(SOURCE, 19, 20, "Bad")
        with self.assertRaisesRegex(RestructureError, "complete blocks"):
            extract_function(SOURCE, 20, 21, "Bad")
        with self.assertRaisesRegex(RestructureError, "just before an 'else'|middle of a statement|complete blocks"):
            extract_function(SOURCE, 19, 19, "Bad")
        with self.assertRaisesRegex(RestructureError, "inside one function body"):
            extract_function(SOURCE, 12, 14, "Bad")
        with self.assertRaisesRegex(RestructureError, "contains 'return'"):
            extract_function(SOURCE, 29, 30, "Bad")
        with self.assertRaisesRegex(RestructureError, "already exists"):
            extract_function(SOURCE, 15, 17, "frag_main")

    def test_operations_replay_in_order_and_report_structure(self) -> None:
        first = extract_function(SOURCE, 15, 17, "ApplyExposure")
        lines = first["source"].split("\r\n")
        start = next(i for i, line in enumerate(lines, 1) if "float _15 = dot" in line)
        end = start + 4  # if-block closes four lines later
        text, receipts = apply_operations(SOURCE, [
            {"operation": "extract", "startLine": 15, "endLine": 17, "name": "ApplyExposure"},
            {"operation": "extract", "startLine": start, "endLine": end, "name": "NormalizeLuminance"},
        ])
        self.assertEqual([r["name"] for r in receipts], ["ApplyExposure", "NormalizeLuminance"])
        report = structure_report(text, entry_point="MainPS")
        names = [f["name"] for f in report["functions"]]
        self.assertEqual(names, ["ApplyExposure", "NormalizeLuminance", "frag_main", "MainPS"])
        flagged = {f["name"]: f["flags"] for f in report["functions"]}
        self.assertTrue(any("anonymous-locals" in flag for flag in flagged["ApplyExposure"]))


if __name__ == "__main__":
    unittest.main()
