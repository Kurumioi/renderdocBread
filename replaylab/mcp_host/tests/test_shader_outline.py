from __future__ import annotations

import unittest

from src.shader_workspace.outline import function_slice, line_slice, outline

SOURCE = """cbuffer _41_43 : register(b0, space0)
{
    float __replaylab_s0_b0_c0_x_f32 : packoffset(c0.x);
    float __replaylab_s0_b0_c0_y_f32 : packoffset(c0.y);
    uint __replaylab_s0_b0_c2_z_i32 : packoffset(c2.z);
};

cbuffer View : register(b1, space0)
{
    float4 View_ViewSize : packoffset(c0);
    float ExposureScale : packoffset(c1.x);
};

Texture2D<float4> BaseColor : register(t0, space0);
SamplerState BaseColorSampler : register(s0, space0);
Texture2D<float4> Normal : register(t1, space0);

struct SPIRV_Cross_Input
{
    float4 gl_FragCoord : SV_Position;
    float2 in_var_TEXCOORD0 : TEXCOORD0;
};

struct SPIRV_Cross_Output
{
    float4 out_var_SV_Target0 : SV_Target0;
};

// helper doing the tone map
float3 ToneMap(float3 c)
{
    float _1001 = c.x * ExposureScale;
    return float3(_1001, c.y, c.z);
}

void MainPS_inner(float2 uv)
{
    float4 _2001 = BaseColor.Sample(BaseColorSampler, uv);
    float4 _2002 = Normal.Sample(BaseColorSampler, uv * View_ViewSize.xy);
    float3 _2003 = ToneMap(_2001.xyz + __replaylab_s0_b0_c0_x_f32);
}

SPIRV_Cross_Output MainPS(SPIRV_Cross_Input stage_input)
{
    MainPS_inner(stage_input.in_var_TEXCOORD0);
    SPIRV_Cross_Output stage_output;
    stage_output.out_var_SV_Target0 = float4(__replaylab_s0_b0_c0_y_f32, 0, 0, 1);
    return stage_output;
}
"""


class OutlineTests(unittest.TestCase):
    def test_outline_reports_declarations_functions_and_use_sites(self) -> None:
        result = outline(SOURCE, entry_point="MainPS")
        self.assertEqual(result["totals"]["functions"], 3)
        cbuffers = {item["name"]: item for item in result["declarations"]["cbuffers"]}
        self.assertEqual(cbuffers["_41_43"]["memberCount"], 3)
        self.assertEqual(cbuffers["_41_43"]["anonymousMembers"], 3)
        self.assertEqual(cbuffers["View"]["anonymousMembers"], 0)
        self.assertEqual([r["name"] for r in result["declarations"]["resources"]], ["BaseColor", "BaseColorSampler", "Normal"])
        entry = result["entry"]
        self.assertEqual(entry["name"], "MainPS")
        self.assertEqual(entry["calls"], ["MainPS_inner"])
        inner = next(f for f in result["functions"] if f["name"] == "MainPS_inner")
        self.assertEqual(inner["callers"], ["MainPS"])
        self.assertEqual(inner["textureSamples"], {"BaseColor": 1, "Normal": 1})
        self.assertGreaterEqual(inner["anonymousLocals"], 3)
        use = result["symbolUse"]
        self.assertEqual(use["ExposureScale"]["owner"], "View")
        self.assertEqual(use["ExposureScale"]["uses"], 1)
        self.assertEqual(use["__replaylab_s0_b0_c0_x_f32"]["uses"], 1)
        self.assertIn("__replaylab_s0_b0_c2_z_i32", result["anonymity"]["unusedDeclared"])
        self.assertEqual(result["anonymity"]["anonymousCBufferMembers"], 3)
        self.assertIn("Do not read the whole file", result["howToRead"])

    def test_slices_are_bounded_and_numbered(self) -> None:
        piece = function_slice(SOURCE, "ToneMap")
        self.assertTrue(piece["text"].splitlines()[0].strip().endswith("float3 ToneMap(float3 c)"))
        self.assertEqual(piece["endLine"] - piece["startLine"], 4)
        ranged = line_slice(SOURCE, 1, 3)
        self.assertEqual(ranged["text"].count("\n"), 2)
        self.assertTrue(ranged["text"].startswith("     1| cbuffer"))
        with self.assertRaises(KeyError):
            function_slice(SOURCE, "Nope")


if __name__ == "__main__":
    unittest.main()
