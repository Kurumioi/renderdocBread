/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 RenderDoc contributors
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

#include "d3d12_test.h"

RD_TEST(D3D12_Checkpoint_Triangle, D3D12GraphicsTest)
{
  static constexpr const char *Description =
      "Four completed submissions draw persistent, disjoint triangle regions with a shared "
      "pixel shader. Exercises native replay checkpoints and shader replacement invalidation.";

  int main()
  {
    if(!Init())
      return 3;

    ID3DBlobPtr vs = Compile(D3DDefaultVertex, "main", "vs_4_0");
    // A constant replacement must change every triangle, including those in saved prefixes.
    ID3DBlobPtr ps = Compile(
        "float4 main(float4 pos : SV_Position, float4 col : COLOR0, float2 uv : TEXCOORD0) "
        ": SV_Target0 { return float4(0.1 + 0.7 * uv.x, 0.15 + 0.6 * uv.y, 0.9, 1.0); }",
        "main", "ps_4_0");
    ID3D12ResourcePtr vb = MakeBuffer().Data(DefaultTri);
    ID3D12RootSignaturePtr sig = MakeSig({});
    ID3D12PipelineStatePtr pso = MakePSO().RootSig(sig).InputLayout().VS(vs).PS(ps);
    ResourceBarrier(vb, D3D12_RESOURCE_STATE_COMMON, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);

    while(Running())
    {
      ID3D12ResourcePtr bb;
      D3D12_CPU_DESCRIPTOR_HANDLE rtv = {};
      // Four draws also leave action-planner candidates after two completed submissions:
      // a candidate at a draw precedes that draw's own closing queue boundary.
      for(uint32_t region = 0; region < 4; region++)
      {
        // Each following recording starts after its predecessor completes; there is no
        // open recording, query, bundle, alias, or split barrier across a checkpoint boundary.
        ID3D12GraphicsCommandListPtr cmd = GetCommandBuffer();
        Reset(cmd);
        if(region == 0)
        {
          bb = StartUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
          bb->SetName(L"CheckpointTriangleOutput");
          rtv = MakeRTV(bb).Format(DXGI_FORMAT_R8G8B8A8_UNORM_SRGB).CreateCPU(0);
          ClearRenderTargetView(cmd, rtv, {0.025f, 0.04f, 0.06f, 1.0f});
        }

        cmd->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        IASetVertexBuffer(cmd, vb, sizeof(DefaultA2V), 0);
        cmd->SetPipelineState(pso);
        cmd->SetGraphicsRootSignature(sig);
        const float width = float(screenWidth) / 4.0f;
        RSSetViewport(cmd, {float(region) * width, 0.0f, width, float(screenHeight), 0.0f, 1.0f});
        RSSetScissorRect(cmd, {0, 0, screenWidth, screenHeight});
        OMSetRenderTargets(cmd, {rtv}, {});
        setMarker(cmd, std::string("Checkpoint region ") + std::to_string(region));
        cmd->DrawInstanced(3, 1, 0, 0);

        if(region == 3)
          FinishUsingBackbuffer(cmd, D3D12_RESOURCE_STATE_RENDER_TARGET);
        CHECK_HR(cmd->Close());
        Submit({cmd});
        GPUSync();
      }
      Present();
    }
    return 0;
  }
};

REGISTER_TEST();
