/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2016-2026 Baldur Karlsson
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

#include <algorithm>
#include "core/settings.h"
#include "driver/dxgi/dxgi_common.h"
#include "driver/ihv/amd/official/DXExt/AmdExtD3DCommandListMarkerApi.h"
#include "d3d12_command_list.h"
#include "d3d12_command_queue.h"
#include "replay/replay_timing.h"

RDOC_EXTERN_CONFIG(bool, D3D12_Debug_RT_Auditing);

WRAPPED_POOL_INST(WrappedID3D12CommandQueue);
WRAPPED_POOL_INST(WrappedID3D12GraphicsCommandList);

template <>
ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList1 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList2 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList3 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList4 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList5 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList6 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList7 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList8 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList9 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList10 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList1 *Unwrap1(ID3D12GraphicsCommandList1 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal1();
}

ID3D12GraphicsCommandList2 *Unwrap2(ID3D12GraphicsCommandList2 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal2();
}

ID3D12GraphicsCommandList3 *Unwrap3(ID3D12GraphicsCommandList3 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal3();
}

ID3D12GraphicsCommandList4 *Unwrap4(ID3D12GraphicsCommandList4 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal4();
}

ID3D12GraphicsCommandList5 *Unwrap5(ID3D12GraphicsCommandList5 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal5();
}

ID3D12GraphicsCommandList6 *Unwrap6(ID3D12GraphicsCommandList6 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal6();
}

ID3D12GraphicsCommandList7 *Unwrap7(ID3D12GraphicsCommandList7 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal7();
}

ID3D12GraphicsCommandList8 *Unwrap8(ID3D12GraphicsCommandList8 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal8();
}

ID3D12GraphicsCommandList9 *Unwrap9(ID3D12GraphicsCommandList9 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal9();
}

ID3D12GraphicsCommandList10 *Unwrap10(ID3D12GraphicsCommandList10 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal10();
}

template <>
ID3D12CommandList *Unwrap(ID3D12CommandList *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList1 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList2 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList3 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList4 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList5 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList6 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList7 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList8 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList9 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList10 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12CommandList *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
D3D12ResourceRecord *GetRecord(ID3D12GraphicsCommandList *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceRecord();
}

template <>
D3D12ResourceRecord *GetRecord(ID3D12CommandList *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceRecord();
}

template <>
ID3D12CommandQueue *Unwrap(ID3D12CommandQueue *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12CommandQueue *)obj)->GetReal();
}

template <>
ResourceId GetResID(ID3D12CommandQueue *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12CommandQueue *)obj)->GetResourceID();
}

template <>
D3D12ResourceRecord *GetRecord(ID3D12CommandQueue *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12CommandQueue *)obj)->GetResourceRecord();
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList1 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList2 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList3 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList4 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList5 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList6 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList7 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList8 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList9 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList10 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

ULONG STDMETHODCALLTYPE WrappedID3D12DebugCommandQueue::AddRef()
{
  if(m_pQueue)
    m_pQueue->AddRef();
  return 1;
}

ULONG STDMETHODCALLTYPE WrappedID3D12DebugCommandQueue::Release()
{
  if(m_pQueue)
    m_pQueue->Release();
  return 1;
}

ULONG STDMETHODCALLTYPE WrappedID3D12DebugCommandList::AddRef()
{
  if(m_pList)
    m_pList->AddRef();
  return 1;
}

ULONG STDMETHODCALLTYPE WrappedID3D12DebugCommandList::Release()
{
  if(m_pList)
    m_pList->Release();
  return 1;
}

HRESULT STDMETHODCALLTYPE WrappedID3D12CompatibilityQueue::QueryInterface(REFIID riid,
                                                                          void **ppvObject)
{
  return m_pQueue.QueryInterface(riid, ppvObject);
}

ULONG STDMETHODCALLTYPE WrappedID3D12CompatibilityQueue::AddRef()
{
  return m_pQueue.AddRef();
}

ULONG STDMETHODCALLTYPE WrappedID3D12CompatibilityQueue::Release()
{
  return m_pQueue.Release();
}

HRESULT STDMETHODCALLTYPE WrappedID3D12CompatibilityQueue::AcquireKeyedMutex(
    _In_ ID3D12Object *pHeapOrResourceWithKeyedMutex, UINT64 Key, DWORD dwTimeout,
    _Reserved_ void *pReserved, _In_range_(0, 0) UINT Reserved)
{
  return m_pReal->AcquireKeyedMutex(Unwrap(pHeapOrResourceWithKeyedMutex), Key, dwTimeout,
                                    pReserved, Reserved);
}

HRESULT STDMETHODCALLTYPE WrappedID3D12CompatibilityQueue::ReleaseKeyedMutex(
    _In_ ID3D12Object *pHeapOrResourceWithKeyedMutex, UINT64 Key, _Reserved_ void *pReserved,
    _In_range_(0, 0) UINT Reserved)
{
  return m_pReal->ReleaseKeyedMutex(Unwrap(pHeapOrResourceWithKeyedMutex), Key, pReserved, Reserved);
}

HRESULT STDMETHODCALLTYPE WrappedDownlevelQueue::QueryInterface(REFIID riid, void **ppvObject)
{
  return m_pQueue.QueryInterface(riid, ppvObject);
}

ULONG STDMETHODCALLTYPE WrappedDownlevelQueue::AddRef()
{
  return m_pQueue.AddRef();
}

ULONG STDMETHODCALLTYPE WrappedDownlevelQueue::Release()
{
  return m_pQueue.Release();
}

HRESULT STDMETHODCALLTYPE WrappedDownlevelQueue::Present(ID3D12GraphicsCommandList *pOpenCommandList,
                                                         ID3D12Resource *pSourceTex2D, HWND hWindow,
                                                         D3D12_DOWNLEVEL_PRESENT_FLAGS Flags)
{
  return m_pQueue.Present(pOpenCommandList, pSourceTex2D, hWindow, Flags);
}

WrappedID3D12CommandQueue::WrappedID3D12CommandQueue(ResourceId id, ID3D12CommandQueue *real,
                                                     WrappedID3D12Device *device, CaptureState &state)
    : RefCounter12(real),
      m_pDevice(device),
      m_State(state),
      m_WrappedDownlevel(*this),
      m_WrappedCompat(*this),
      m_SharingContract(*m_pDevice)
{
  RenderDoc::Inst().RegisterMemoryRegion(this, sizeof(WrappedID3D12CommandQueue));

  m_WrappedDebug.m_pQueue = this;
  m_pDownlevel = NULL;
  m_pReal1 = NULL;
  if(m_pReal)
  {
    m_pReal->QueryInterface(__uuidof(ID3D12DebugCommandQueue), (void **)&m_WrappedDebug.m_pReal);
    m_pReal->QueryInterface(__uuidof(ID3D12DebugCommandQueue1), (void **)&m_WrappedDebug.m_pReal1);
    m_pReal->QueryInterface(__uuidof(ID3D12CommandQueueDownlevel), (void **)&m_pDownlevel);
    m_pReal->QueryInterface(__uuidof(ID3D12CommandQueue1), (void **)&m_pReal1);
    m_pReal->QueryInterface(__uuidof(ID3D12CompatibilityQueue), (void **)&m_WrappedCompat.m_pReal);
    m_pReal->QueryInterface(__uuidof(ID3D12SharingContract), (void **)&m_SharingContract.m_pReal);
  }

  if(RenderDoc::Inst().IsReplayApp())
  {
    m_ReplayList = new WrappedID3D12GraphicsCommandList(ResourceId(), NULL, m_pDevice, state);

    m_ReplayList->SetCommandData(&m_Cmd);
  }

  if(id == ResourceId())
    id = ResourceIDGen::GetNewUniqueID();
  m_ResourceID = id;

  m_QueueRecord = NULL;
  m_CreationRecord = NULL;

  m_Cmd.m_pDevice = m_pDevice;

  if(!RenderDoc::Inst().IsReplayApp())
  {
    m_QueueRecord = m_pDevice->GetResourceManager()->AddResourceRecord(m_ResourceID);
    m_QueueRecord->type = Resource_CommandQueue;
    m_QueueRecord->DataInSerialiser = false;
    m_QueueRecord->InternalResource = true;
    m_QueueRecord->Length = 0;

    // a bit of a hack, we make a parallel resource record with the same lifetime as the command
    // queue. It will hold onto our create chunk and not get thrown away as we clear and re-fill
    // submissions into the queue record itself. We'll pull it into the capture by marking the
    // queues as referenced.
    m_CreationRecord =
        m_pDevice->GetResourceManager()->AddResourceRecord(ResourceIDGen::GetNewUniqueID());
    m_CreationRecord->type = Resource_CommandQueue;
    m_CreationRecord->InternalResource = true;
  }

  m_pDevice->GetResourceManager()->AddResource(GetResourceID(), this);

  m_pDevice->SoftRef();
}

WrappedID3D12CommandQueue::~WrappedID3D12CommandQueue()
{
  SAFE_DELETE(m_FrameReader);

  // The prepared allocators are deliberately never reset while their reusable lists are alive.
  // Queue teardown happens after replay work has been drained by device shutdown.
  m_Cmd.ClearPreparedReplayCache();
  m_Cmd.ClearFullReplayCompanions();

  SAFE_RELEASE(m_CallbackFence);

  for(SDObject *o : m_Cmd.m_EventAnnotations)
    delete o;

  delete m_Cmd.m_RootAnnotation;

  if(m_CreationRecord)
    m_CreationRecord->Delete(m_pDevice->GetResourceManager());

  if(m_QueueRecord)
    m_QueueRecord->Delete(m_pDevice->GetResourceManager());
  m_pDevice->GetResourceManager()->ReleaseResource(GetResourceID());
  m_pDevice->RemoveQueue(this);

  SAFE_RELEASE(m_pDownlevel);
  SAFE_RELEASE(m_pReal1);

  SAFE_RELEASE(m_WrappedCompat.m_pReal);
  SAFE_RELEASE(m_WrappedDebug.m_pReal);
  SAFE_RELEASE(m_WrappedDebug.m_pReal1);
  SAFE_RELEASE(m_SharingContract.m_pReal);
  SAFE_RELEASE(m_pReal);
}

HRESULT STDMETHODCALLTYPE WrappedID3D12CommandQueue::QueryInterface(REFIID riid, void **ppvObject)
{
  if(riid == __uuidof(IUnknown))
  {
    *ppvObject = (IUnknown *)(ID3D12CommandQueue *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12CommandQueue))
  {
    *ppvObject = (ID3D12CommandQueue *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12DebugCommandQueue))
  {
    if(m_WrappedDebug.m_pReal)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandQueue *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DebugCommandQueue1))
  {
    if(m_WrappedDebug.m_pReal1)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandQueue1 *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12CompatibilityQueue))
  {
    if(m_WrappedCompat.m_pReal)
    {
      AddRef();
      *ppvObject = (ID3D12CompatibilityQueue *)&m_WrappedCompat;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12SharingContract))
  {
    *ppvObject = (ID3D12SharingContract *)&m_SharingContract;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12Pageable))
  {
    *ppvObject = (ID3D12Pageable *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12DeviceChild))
  {
    *ppvObject = (ID3D12DeviceChild *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12Object))
  {
    *ppvObject = (ID3D12DeviceChild *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12CommandQueueDownlevel))
  {
    if(m_pDownlevel)
    {
      *ppvObject = &m_WrappedDownlevel;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }

  return RefCounter12::QueryInterface("ID3D12CommandQueue", riid, ppvObject);
}

void WrappedID3D12CommandQueue::CheckAndFreeRayDispatches()
{
  UINT64 signalled = 0;
  if(m_CallbackFence)
    signalled = m_CallbackFence->GetCompletedValue();

  for(PatchedRayDispatch::Resources &ray : m_RayDispatchesPending)
  {
    if(signalled >= ray.fenceValue)
    {
      GetResourceManager()->GetRTManager()->AddDispatchTimer(ray.query);
      ray.Release();
    }
  }

  m_RayDispatchesPending.removeIf(
      [](const PatchedRayDispatch::Resources &ray) { return ray.lookupBuffer == NULL; });
}

void WrappedID3D12CommandQueue::ClearAfterCapture()
{
  // delete cmd buffers now - had to keep them alive until after serialiser flush.
  for(size_t i = 0; i < m_CmdListRecords.size(); i++)
    m_CmdListRecords[i]->Delete(GetResourceManager());

  for(size_t i = 0; i < m_CmdListAllocators.size(); i++)
    m_CmdListAllocators[i]->Delete(GetResourceManager());

  m_CmdListRecords.clear();
  m_CmdListAllocators.clear();

  m_QueueRecord->DeleteChunks();
}

WriteSerialiser &WrappedID3D12CommandQueue::GetThreadSerialiser()
{
  return m_pDevice->GetThreadSerialiser();
}

rdcstr WrappedID3D12CommandQueue::GetChunkName(uint32_t idx)
{
  if((SystemChunk)idx < SystemChunk::FirstDriverChunk)
    return ToStr((SystemChunk)idx);

  return ToStr((D3D12Chunk)idx);
}

const APIEvent &WrappedID3D12CommandQueue::GetEvent(uint32_t eventId)
{
  // start at where the requested eventId would be
  size_t idx = eventId;

  // find the next valid event (some may be skipped)
  while(idx < m_Cmd.m_Events.size() - 1 && m_Cmd.m_Events[idx].eventId == 0)
    idx++;

  return m_Cmd.m_Events[RDCMIN(idx, m_Cmd.m_Events.size() - 1)];
}

bool WrappedID3D12CommandQueue::ProcessChunk(ReadSerialiser &ser, D3D12Chunk chunk)
{
  // Sparse mapping handlers validate the captured source/heap relation after deserialisation;
  // an incomplete alias closure still invalidates the complete dirty proof there.
  m_Cmd.m_AddedAction = false;

  bool ret = false;

  switch(chunk)
  {
    case D3D12Chunk::Device_CreateConstantBufferView:
    case D3D12Chunk::Device_CreateShaderResourceView:
    case D3D12Chunk::Device_CreateUnorderedAccessView:
    case D3D12Chunk::Device_CreateRenderTargetView:
    case D3D12Chunk::Device_CreateDepthStencilView:
    case D3D12Chunk::Device_CreateSampler:
    case D3D12Chunk::Device_CreateSampler2:
      ret = m_pDevice->Serialise_DynamicDescriptorWrite(ser, NULL);
      break;
    case D3D12Chunk::Device_CopyDescriptors:
    case D3D12Chunk::Device_CopyDescriptorsSimple:
      ret = m_pDevice->Serialise_DynamicDescriptorCopies(ser, rdcarray<DynamicDescriptorCopy>());
      break;

    case D3D12Chunk::Queue_ExecuteCommandLists:
      ret = Serialise_ExecuteCommandLists(ser, 0, NULL);
      break;
    case D3D12Chunk::Queue_Signal: ret = Serialise_Signal(ser, NULL, 0); break;
    case D3D12Chunk::Queue_Wait: ret = Serialise_Wait(ser, NULL, 0); break;
    case D3D12Chunk::Queue_UpdateTileMappings:
      ret = Serialise_UpdateTileMappings(ser, NULL, 0, NULL, NULL, NULL, 0, NULL, NULL, NULL,
                                         D3D12_TILE_MAPPING_FLAGS(0));
      break;
    case D3D12Chunk::Queue_CopyTileMappings:
      ret =
          Serialise_CopyTileMappings(ser, NULL, NULL, NULL, NULL, NULL, D3D12_TILE_MAPPING_FLAGS(0));
      break;
    case D3D12Chunk::Queue_BeginEvent: ret = Serialise_BeginEvent(ser, 0, NULL, 0); break;
    case D3D12Chunk::Queue_SetMarker: ret = Serialise_SetMarker(ser, 0, NULL, 0); break;
    case D3D12Chunk::Queue_EndEvent: ret = Serialise_EndEvent(ser); break;

    case D3D12Chunk::List_Close: ret = m_ReplayList->Serialise_Close(ser); break;
    case D3D12Chunk::List_Reset: ret = m_ReplayList->Serialise_Reset(ser, NULL, NULL); break;
    case D3D12Chunk::List_ResourceBarrier:
      ret = m_ReplayList->Serialise_ResourceBarrier(ser, 0, NULL);
      break;
    case D3D12Chunk::List_BeginQuery:
      ret = m_ReplayList->Serialise_BeginQuery(ser, NULL, D3D12_QUERY_TYPE(0), 0);
      break;
    case D3D12Chunk::List_EndQuery:
      ret = m_ReplayList->Serialise_EndQuery(ser, NULL, D3D12_QUERY_TYPE(0), 0);
      break;
    case D3D12Chunk::List_ResolveQueryData:
      ret = m_ReplayList->Serialise_ResolveQueryData(ser, NULL, D3D12_QUERY_TYPE(0), 0, 0, NULL, 0);
      break;
    case D3D12Chunk::List_SetPredication:
      ret = m_ReplayList->Serialise_SetPredication(ser, NULL, 0, D3D12_PREDICATION_OP(0));
      break;
    case D3D12Chunk::List_DrawIndexedInstanced:
      ret = m_ReplayList->Serialise_DrawIndexedInstanced(ser, 0, 0, 0, 0, 0);
      break;
    case D3D12Chunk::List_DrawInstanced:
      ret = m_ReplayList->Serialise_DrawInstanced(ser, 0, 0, 0, 0);
      break;
    case D3D12Chunk::List_Dispatch: ret = m_ReplayList->Serialise_Dispatch(ser, 0, 0, 0); break;
    case D3D12Chunk::List_ExecuteIndirect:
      ret = m_ReplayList->Serialise_ExecuteIndirect(ser, NULL, 0, NULL, 0, NULL, 0);
      break;
    case D3D12Chunk::List_ExecuteBundle:
      ret = m_ReplayList->Serialise_ExecuteBundle(ser, NULL);
      break;
    case D3D12Chunk::List_CopyBufferRegion:
      ret = m_ReplayList->Serialise_CopyBufferRegion(ser, NULL, 0, NULL, 0, 0);
      break;
    case D3D12Chunk::List_CopyTextureRegion:
      ret = m_ReplayList->Serialise_CopyTextureRegion(ser, NULL, 0, 0, 0, NULL, NULL);
      break;
    case D3D12Chunk::List_CopyResource:
      ret = m_ReplayList->Serialise_CopyResource(ser, NULL, NULL);
      break;
    case D3D12Chunk::List_ResolveSubresource:
      ret = m_ReplayList->Serialise_ResolveSubresource(ser, NULL, 0, NULL, 0, DXGI_FORMAT_UNKNOWN);
      break;
    case D3D12Chunk::List_ClearRenderTargetView:
      ret = m_ReplayList->Serialise_ClearRenderTargetView(ser, D3D12_CPU_DESCRIPTOR_HANDLE(),
                                                          (FLOAT *)NULL, 0, NULL);
      break;
    case D3D12Chunk::List_ClearDepthStencilView:
      ret = m_ReplayList->Serialise_ClearDepthStencilView(ser, D3D12_CPU_DESCRIPTOR_HANDLE(),
                                                          D3D12_CLEAR_FLAGS(0), 0.0f, 0, 0, NULL);
      break;
    case D3D12Chunk::List_ClearUnorderedAccessViewUint:
      ret = m_ReplayList->Serialise_ClearUnorderedAccessViewUint(
          ser, D3D12_GPU_DESCRIPTOR_HANDLE(), D3D12_CPU_DESCRIPTOR_HANDLE(), NULL, NULL, 0, NULL);
      break;
    case D3D12Chunk::List_ClearUnorderedAccessViewFloat:
      ret = m_ReplayList->Serialise_ClearUnorderedAccessViewFloat(
          ser, D3D12_GPU_DESCRIPTOR_HANDLE(), D3D12_CPU_DESCRIPTOR_HANDLE(), NULL, NULL, 0, NULL);
      break;
    case D3D12Chunk::List_DiscardResource:
      ret = m_ReplayList->Serialise_DiscardResource(ser, NULL, NULL);
      break;
    case D3D12Chunk::List_IASetPrimitiveTopology:
      ret = m_ReplayList->Serialise_IASetPrimitiveTopology(ser, D3D_PRIMITIVE_TOPOLOGY_UNDEFINED);
      break;
    case D3D12Chunk::List_IASetIndexBuffer:
      ret = m_ReplayList->Serialise_IASetIndexBuffer(ser, NULL);
      break;
    case D3D12Chunk::List_IASetVertexBuffers:
      ret = m_ReplayList->Serialise_IASetVertexBuffers(ser, 0, 0, NULL);
      break;
    case D3D12Chunk::List_SOSetTargets:
      ret = m_ReplayList->Serialise_SOSetTargets(ser, 0, 0, NULL);
      break;
    case D3D12Chunk::List_RSSetViewports:
      ret = m_ReplayList->Serialise_RSSetViewports(ser, 0, NULL);
      break;
    case D3D12Chunk::List_RSSetScissorRects:
      ret = m_ReplayList->Serialise_RSSetScissorRects(ser, 0, NULL);
      break;
    case D3D12Chunk::List_SetPipelineState:
      ret = m_ReplayList->Serialise_SetPipelineState(ser, NULL);
      break;
    case D3D12Chunk::List_SetDescriptorHeaps:
      ret = m_ReplayList->Serialise_SetDescriptorHeaps(ser, 0, NULL);
      break;
    case D3D12Chunk::List_OMSetRenderTargets:
      ret = m_ReplayList->Serialise_OMSetRenderTargets(ser, 0, NULL, FALSE, NULL);
      break;
    case D3D12Chunk::List_OMSetStencilRef:
      ret = m_ReplayList->Serialise_OMSetStencilRef(ser, 0);
      break;
    case D3D12Chunk::List_OMSetBlendFactor:
      ret = m_ReplayList->Serialise_OMSetBlendFactor(ser, NULL);
      break;
    case D3D12Chunk::List_SetGraphicsRootDescriptorTable:
      ret = m_ReplayList->Serialise_SetGraphicsRootDescriptorTable(ser, 0,
                                                                   D3D12_GPU_DESCRIPTOR_HANDLE());
      break;
    case D3D12Chunk::List_SetGraphicsRootSignature:
      ret = m_ReplayList->Serialise_SetGraphicsRootSignature(ser, NULL);
      break;
    case D3D12Chunk::List_SetGraphicsRoot32BitConstant:
      ret = m_ReplayList->Serialise_SetGraphicsRoot32BitConstant(ser, 0, 0, 0);
      break;
    case D3D12Chunk::List_SetGraphicsRoot32BitConstants:
      ret = m_ReplayList->Serialise_SetGraphicsRoot32BitConstants(ser, 0, 0, NULL, 0);
      break;
    case D3D12Chunk::List_SetGraphicsRootConstantBufferView:
      ret = m_ReplayList->Serialise_SetGraphicsRootConstantBufferView(ser, 0,
                                                                      D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_SetGraphicsRootShaderResourceView:
      ret = m_ReplayList->Serialise_SetGraphicsRootShaderResourceView(ser, 0,
                                                                      D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_SetGraphicsRootUnorderedAccessView:
      ret = m_ReplayList->Serialise_SetGraphicsRootUnorderedAccessView(ser, 0,
                                                                       D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_SetComputeRootDescriptorTable:
      ret = m_ReplayList->Serialise_SetComputeRootDescriptorTable(ser, 0,
                                                                  D3D12_GPU_DESCRIPTOR_HANDLE());
      break;
    case D3D12Chunk::List_SetComputeRootSignature:
      ret = m_ReplayList->Serialise_SetComputeRootSignature(ser, NULL);
      break;
    case D3D12Chunk::List_SetComputeRoot32BitConstant:
      ret = m_ReplayList->Serialise_SetComputeRoot32BitConstant(ser, 0, 0, 0);
      break;
    case D3D12Chunk::List_SetComputeRoot32BitConstants:
      ret = m_ReplayList->Serialise_SetComputeRoot32BitConstants(ser, 0, 0, NULL, 0);
      break;
    case D3D12Chunk::List_SetComputeRootConstantBufferView:
      ret = m_ReplayList->Serialise_SetComputeRootConstantBufferView(ser, 0,
                                                                     D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_SetComputeRootShaderResourceView:
      ret = m_ReplayList->Serialise_SetComputeRootShaderResourceView(ser, 0,
                                                                     D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_SetComputeRootUnorderedAccessView:
      ret = m_ReplayList->Serialise_SetComputeRootUnorderedAccessView(ser, 0,
                                                                      D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_CopyTiles:
      ret = m_ReplayList->Serialise_CopyTiles(ser, NULL, NULL, NULL, NULL, 0,
                                              D3D12_TILE_COPY_FLAGS(0));
      break;
    case D3D12Chunk::List_AtomicCopyBufferUINT:
      ret = m_ReplayList->Serialise_AtomicCopyBufferUINT(ser, NULL, 0, NULL, 0, 0, NULL, NULL);
      break;
    case D3D12Chunk::List_AtomicCopyBufferUINT64:
      ret = m_ReplayList->Serialise_AtomicCopyBufferUINT64(ser, NULL, 0, NULL, 0, 0, NULL, NULL);
      break;
    case D3D12Chunk::List_OMSetDepthBounds:
      ret = m_ReplayList->Serialise_OMSetDepthBounds(ser, 0.0f, 0.0f);
      break;
    case D3D12Chunk::List_ResolveSubresourceRegion:
      ret = m_ReplayList->Serialise_ResolveSubresourceRegion(
          ser, NULL, 0, 0, 0, NULL, 0, NULL, DXGI_FORMAT_UNKNOWN, D3D12_RESOLVE_MODE_DECOMPRESS);
      break;
    case D3D12Chunk::List_SetSamplePositions:
      ret = m_ReplayList->Serialise_SetSamplePositions(ser, 0, NULL, NULL);
      break;
    case D3D12Chunk::List_SetViewInstanceMask:
      ret = m_ReplayList->Serialise_SetViewInstanceMask(ser, 0);
      break;
    case D3D12Chunk::List_WriteBufferImmediate:
      ret = m_ReplayList->Serialise_WriteBufferImmediate(ser, 0, NULL, NULL);
      break;
    case D3D12Chunk::List_BeginRenderPass:
      ret = m_ReplayList->Serialise_BeginRenderPass(ser, 0, NULL, NULL, D3D12_RENDER_PASS_FLAG_NONE);
      break;
    case D3D12Chunk::List_EndRenderPass: ret = m_ReplayList->Serialise_EndRenderPass(ser); break;
    case D3D12Chunk::List_RSSetShadingRate:
      ret = m_ReplayList->Serialise_RSSetShadingRate(ser, D3D12_SHADING_RATE_1X1, NULL);
      break;
    case D3D12Chunk::List_RSSetShadingRateImage:
      ret = m_ReplayList->Serialise_RSSetShadingRateImage(ser, NULL);
      break;
    case D3D12Chunk::List_OMSetFrontAndBackStencilRef:
      ret = m_ReplayList->Serialise_OMSetFrontAndBackStencilRef(ser, 0, 0);
      break;
    case D3D12Chunk::List_RSSetDepthBias:
      ret = m_ReplayList->Serialise_RSSetDepthBias(ser, 0.0f, 0.0f, 0.0f);
      break;
    case D3D12Chunk::List_IASetIndexBufferStripCutValue:
      ret = m_ReplayList->Serialise_IASetIndexBufferStripCutValue(
          ser, D3D12_INDEX_BUFFER_STRIP_CUT_VALUE_DISABLED);
      break;
    case D3D12Chunk::List_Barrier: ret = m_ReplayList->Serialise_Barrier(ser, 0, NULL); break;
    case D3D12Chunk::List_DispatchMesh:
      ret = m_ReplayList->Serialise_DispatchMesh(ser, 0, 0, 0);
      break;

    case D3D12Chunk::PushMarker: ret = m_ReplayList->Serialise_BeginEvent(ser, 0, NULL, 0); break;
    case D3D12Chunk::PopMarker: ret = m_ReplayList->Serialise_EndEvent(ser); break;
    case D3D12Chunk::SetMarker: ret = m_ReplayList->Serialise_SetMarker(ser, 0, NULL, 0); break;

    case D3D12Chunk::CoherentMapWrite:
    case D3D12Chunk::Resource_Unmap:
      ret = m_pDevice->Serialise_MapDataWrite(ser, NULL, 0, NULL, D3D12_RANGE(), false);
      break;
    case D3D12Chunk::Resource_WriteToSubresource:
      ret = m_pDevice->Serialise_WriteToSubresource(ser, NULL, 0, NULL, NULL, 0, 0);
      break;
    case D3D12Chunk::List_IndirectSubCommand:
      // this is a fake chunk generated at runtime as part of indirect draws.
      // Just in case it gets exported and imported, completely ignore it.
      return true;

    case D3D12Chunk::Swapchain_Present: ret = m_pDevice->Serialise_Present(ser, NULL, 0, 0); break;

    case D3D12Chunk::List_ClearState: ret = m_ReplayList->Serialise_ClearState(ser, NULL); break;

    case D3D12Chunk::List_BuildRaytracingAccelerationStructure:
      ret = m_ReplayList->Serialise_BuildRaytracingAccelerationStructure(ser, NULL, 0, NULL);
      break;
    case D3D12Chunk::List_CopyRaytracingAccelerationStructure:
      ret = m_ReplayList->Serialise_CopyRaytracingAccelerationStructure(
          ser, D3D12_GPU_VIRTUAL_ADDRESS(), D3D12_GPU_VIRTUAL_ADDRESS(),
          D3D12_RAYTRACING_ACCELERATION_STRUCTURE_COPY_MODE_CLONE);
      break;
    case D3D12Chunk::List_EmitRaytracingAccelerationStructurePostbuildInfo:
      ret = m_ReplayList->Serialise_EmitRaytracingAccelerationStructurePostbuildInfo(ser, NULL, 0,
                                                                                     NULL);
      break;
    case D3D12Chunk::List_DispatchRays:
      ret = m_ReplayList->Serialise_DispatchRays(ser, NULL);
      break;
    case D3D12Chunk::List_SetPipelineState1:
      ret = m_ReplayList->Serialise_SetPipelineState1(ser, NULL);
      break;

    case D3D12Chunk::SetCommandAnnotation:
      ret = m_ReplayList->Serialise_SetCommandAnnotation(ser, rdcstr(), eRENDERDOC_AnnotationMax, 0,
                                                         RENDERDOC_AnnotationValue());
      break;
    case D3D12Chunk::SetQueueAnnotation:
      ret = Serialise_SetQueueAnnotation(ser, rdcstr(), eRENDERDOC_AnnotationMax, 0,
                                         RENDERDOC_AnnotationValue());
      break;

    // in order to get a warning if we miss a case, we explicitly handle the device creation chunks
    // here. If we actually encounter one it's an error (we shouldn't see these inside the captured
    // frame itself)
    case D3D12Chunk::Device_CreateCommandQueue:
    case D3D12Chunk::Device_CreateCommandAllocator:
    case D3D12Chunk::Device_CreateCommandList:
    case D3D12Chunk::Device_CreateGraphicsPipeline:
    case D3D12Chunk::Device_CreateComputePipeline:
    case D3D12Chunk::Device_CreateDescriptorHeap:
    case D3D12Chunk::Device_CreateRootSignature:
    case D3D12Chunk::Device_CreateCommandSignature:
    case D3D12Chunk::Device_CreateHeap:
    case D3D12Chunk::Device_CreateCommittedResource:
    case D3D12Chunk::Device_CreatePlacedResource:
    case D3D12Chunk::Device_CreateReservedResource:
    case D3D12Chunk::Device_CreateQueryHeap:
    case D3D12Chunk::Device_CreateFence:
    case D3D12Chunk::SetName:
    case D3D12Chunk::SetShaderDebugPath:
    case D3D12Chunk::CreateSwapBuffer:
    case D3D12Chunk::Device_CreatePipelineState:
    case D3D12Chunk::Device_CreateHeapFromAddress:
    case D3D12Chunk::Device_CreateHeapFromFileMapping:
    case D3D12Chunk::Device_OpenSharedHandle:
    case D3D12Chunk::Device_CreateCommandList1:
    case D3D12Chunk::Device_CreateCommittedResource1:
    case D3D12Chunk::Device_CreateHeap1:
    case D3D12Chunk::Device_ExternalDXGIResource:
    case D3D12Chunk::CompatDevice_CreateSharedResource:
    case D3D12Chunk::CompatDevice_CreateSharedHeap:
    case D3D12Chunk::SetShaderExtUAV:
    case D3D12Chunk::Device_CreateCommittedResource2:
    case D3D12Chunk::Device_CreatePlacedResource1:
    case D3D12Chunk::Device_CreateCommandQueue1:
    case D3D12Chunk::Device_CreateCommittedResource3:
    case D3D12Chunk::Device_CreatePlacedResource2:
    case D3D12Chunk::Device_CreateReservedResource1:
    case D3D12Chunk::Device_CreateReservedResource2:
    case D3D12Chunk::Device_CreateStateObject:
    case D3D12Chunk::Device_AddToStateObject:
    case D3D12Chunk::CreateAS:
    case D3D12Chunk::StateObject_SetPipelineStackSize:
    case D3D12Chunk::Device_CreateHeapFromAddress1:
    case D3D12Chunk::Device_CreateRootSignatureFromSubobjectInLibrary:
    case D3D12Chunk::List_SetProgram:
    case D3D12Chunk::List_DispatchGraph:
      RDCERR("Unexpected chunk while processing frame: %s", ToStr(chunk).c_str());
      return false;

    // no explicit default so that we have compiler warnings if a chunk isn't explicitly handled.
    case D3D12Chunk::Max: break;
  }

  {
    SystemChunk system = (SystemChunk)chunk;

    if(system == SystemChunk::CaptureEnd)
    {
      SERIALISE_ELEMENT_LOCAL(PresentedImage, ResourceId()).TypedAs("ID3D12Resource *"_lit);

      SERIALISE_CHECK_READ_ERRORS();

      if(PresentedImage != ResourceId())
        m_Cmd.m_LastPresentedImage = PresentedImage;

      if(IsLoading(m_State) && m_Cmd.m_LastChunk != D3D12Chunk::Swapchain_Present)
      {
        m_Cmd.AddEvent();

        ActionDescription action;
        action.customName = "End of Capture";
        action.flags |= ActionFlags::Present;

        action.copyDestination = m_Cmd.m_LastPresentedImage;

        m_Cmd.AddAction(action);
      }

      ret = true;
    }
    else if(!ret)
    {
      RDCERR("Chunk failed to serialise: %s", ToStr(chunk).c_str());
      return false;
    }
  }

  if(IsLoading(m_State))
  {
    if(chunk == D3D12Chunk::List_Reset || chunk == D3D12Chunk::List_Close)
    {
      // don't add these events - they will be handled when inserted in-line into queue submit
    }
    else if(chunk == D3D12Chunk::Queue_EndEvent)
    {
      // also ignore, this just pops the action stack
    }
    else if(chunk == D3D12Chunk::SetCommandAnnotation || chunk == D3D12Chunk::SetQueueAnnotation)
    {
    }
    else
    {
      if(!m_Cmd.m_AddedAction)
        m_Cmd.AddEvent();
    }
  }

  m_Cmd.m_AddedAction = false;

  return ret;
}

static const char *PreparedReplayRejectReasonName(PreparedReplayRejectReason reason)
{
  switch(reason)
  {
    case PreparedReplayRejectReason::None: return "none";
    case PreparedReplayRejectReason::UnsupportedChunk: return "unsupportedChunk";
    case PreparedReplayRejectReason::InterleavedChunk: return "interleavedChunk";
    case PreparedReplayRejectReason::InterleavedReset: return "interleavedReset";
    case PreparedReplayRejectReason::APICommand: return "apiCommand";
    case PreparedReplayRejectReason::IncompleteRange: return "incompleteRange";
  }
  return "unknown";
}

RDResult WrappedID3D12CommandQueue::ReplayLog(CaptureState readType, uint32_t startEventID,
                                              uint32_t endEventID, bool partial,
                                              bool allowPreparedReplay, bool allowCheckpoints)
{
  ScopedReplayTimingSection setupTiming(ReplayTimingSection::ReplaySetup);
  m_State = readType;

  m_Cmd.m_PreparedReplayEnabled = false;
  m_Cmd.m_PreparedSkipEnd = 0;
  m_Cmd.m_PreparedCacheHits = 0;
  m_Cmd.m_PreparedCacheBuilds = 0;
  m_Cmd.m_PreparedSkippedChunks = 0;
  m_Cmd.m_PreparedSkippedBytes = 0;
  m_Cmd.m_PreparedAttempts = 0;
  m_Cmd.m_PreparedRejectedType = 0;
  m_Cmd.m_PreparedRejectedUnsafe = 0;
  m_Cmd.m_PreparedRejectedExisting = 0;
  m_Cmd.m_PreparedRejectedBudget = 0;
  m_Cmd.m_PreparedRejectSummary.clear();
  m_Cmd.m_PreparedTimingEnabled =
      IsActiveReplaying(m_State) && Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1";
  m_Cmd.m_ReplayWriteCutoffEnabled =
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_DIRTY_WRITE_CUTOFF") != "1";

  if(!partial)
  {
    // The device drained the previous replay before entering this non-partial recording, as
    // required for the existing rerecorded wrappers below. Never reset a submitted allocator.
    m_Cmd.ClearFullReplayCompanions();
    for(size_t i = 0; i < m_Cmd.m_RerecordCmdList.size(); i++)
      SAFE_RELEASE(m_Cmd.m_RerecordCmdList[i]);

    m_Cmd.m_RerecordCmdList.clear();
  }

  if(!m_FrameReader)
  {
    RETURN_ERROR_RESULT(ResultCode::InvalidParameter,
                        "Can't replay context capture without frame reader");
  }

  m_FrameReader->SetOffset(0);

  ReadSerialiser ser(m_FrameReader, Ownership::Nothing);
  if(IsReplayTimingActive())
  {
    ser.SetParameterReadCallback(&NotifyReplayTimingParametersDecoded);
    SetReplayTimingChunkNameLookup(&GetChunkName);
  }

  ser.SetStringDatabase(&m_StringDB);
  ser.SetUserData(GetResourceManager());
  ser.SetVersion(m_pDevice->GetCaptureVersion());

  if(IsLoading(m_State) || IsStructuredExporting(m_State))
  {
    ser.ConfigureStructuredExport(&GetChunkName, IsStructuredExporting(m_State),
                                  m_pDevice->GetTimeBase(), m_pDevice->GetTimeFrequency());

    ser.GetStructuredFile().Swap(*m_pDevice->GetStructuredFile());

    m_StructuredFile = &ser.GetStructuredFile();
  }
  else
  {
    m_StructuredFile = m_pDevice->GetStructuredFile();
  }

  m_Cmd.m_StructuredFile = m_StructuredFile;

  SystemChunk header = ser.ReadChunk<SystemChunk>();
  RDCASSERTEQUAL(header, SystemChunk::CaptureBegin);

  if(partial)
    ser.SkipCurrentChunk();
  else
    m_pDevice->Serialise_BeginCaptureFrame(ser);

  ser.EndChunk();

  m_Cmd.m_RootEvents.clear();

  if(IsLoading(m_State))
  {
    m_pDevice->ApplyInitialContents();

    m_pDevice->ExecuteLists();
    m_pDevice->FlushLists();

    if(m_pDevice->HasFatalError())
      return m_pDevice->FatalErrorCheck();
  }

  if(IsActiveReplaying(m_State))
  {
    APIEvent ev = GetEvent(startEventID);
    m_Cmd.m_RootEventID = ev.eventId;

    // if not partial, we need to be sure to replay
    // past the command list records, so can't
    // skip to the file offset of the first event
    if(partial)
    {
      ser.GetReader()->SetOffset(ev.fileOffset);

      D3D12CommandData::ActionUse use(ev.fileOffset, 0);
      auto it = std::lower_bound(m_Cmd.m_ActionUses.begin(), m_Cmd.m_ActionUses.end(), use);

      if(it != m_Cmd.m_ActionUses.end())
      {
        BakedCmdListInfo &cmdInfo = m_Cmd.m_BakedCmdListInfo[it->cmdList];
        cmdInfo.curEventID = it->relativeEID;
      }
    }

    m_Cmd.m_FirstEventID = startEventID;
    m_Cmd.m_LastEventID = endEventID;

    // Only a normal full-frame prefix ending inside one primary command list can use prepared
    // nodes. This preserves the existing partial-list state reconstruction for the selected EID.
    bool targetInsideCommandList = false;
    for(auto it = m_Cmd.m_Partial[D3D12CommandData::Primary].cmdListExecs.begin();
        it != m_Cmd.m_Partial[D3D12CommandData::Primary].cmdListExecs.end(); ++it)
    {
      const uint32_t eventCount = m_Cmd.m_BakedCmdListInfo[it->first].eventCount;
      for(uint32_t baseEvent : it->second)
      {
        if(baseEvent <= endEventID && endEventID < baseEvent + eventCount)
        {
          targetInsideCommandList = true;
          break;
        }
      }
      if(targetInsideCommandList)
        break;
    }

    m_Cmd.m_PreparedReplayEnabled =
        allowPreparedReplay && !partial && startEventID == 1 && targetInsideCommandList &&
        m_Cmd.m_ActionCallback == NULL &&
        // Prepared hits are experimental until native command-list lifetime and query replay have
        // passed capture validation. Never enable this path implicitly for normal EID scrubbing.
        Process::GetEnvVariable("RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY") == "1" &&
        Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY") != "1";
  }
  else
  {
    m_Cmd.m_RootEventID = 1;
    m_Cmd.m_RootActionID = 1;
    m_Cmd.m_FirstEventID = 0;
    m_Cmd.m_LastEventID = ~0U;
  }

  if(IsReplayMode(m_State))
  {
    for(size_t i = 0; i < m_Cmd.m_IndirectBuffers.size(); i++)
      SAFE_RELEASE(m_Cmd.m_IndirectBuffers[i]);
    m_Cmd.m_IndirectBuffers.clear();
  }

  uint64_t startOffset = ser.GetReader()->GetOffset();
  const auto recordFullCompanionAtClose = [&](ResourceId baked, uint64_t closeEnd) -> RDResult {
    const RDResult companion = m_Cmd.RecordFullReplayCompanion(
        baked, closeEnd,
        [&](const BakedCmdListInfo::CheckpointRecordingChunk &chunk) -> RDResult {
          ser.GetReader()->SetOffset(chunk.offset);
          m_Cmd.m_CurChunkOffset = chunk.offset;
          D3D12Chunk type = ser.ReadChunk<D3D12Chunk>();
          if(ser.GetReader()->IsErrored() || type != chunk.type)
            return RDResult(ResultCode::APIDataCorrupted,
                            "Repeated-list companion source chunk changed");
          m_Cmd.m_ChunkMetadata = ser.ChunkMetadata();
          m_Cmd.m_LastCmdListID = ResourceId();
          bool success;
          {
            ScopedReplayTimingChunk timing((uint32_t)type);
            success = ProcessChunk(ser, type);
          }
          ser.EndChunk();
          AddReplayTimingCounter(ReplayTimingCounter::ChunkBytes, chunk.endOffset - chunk.offset);
          if(ser.GetReader()->IsErrored())
            return RDResult(ResultCode::APIDataCorrupted, ser.GetError().message);
          if(!success)
            return m_Cmd.m_FailedReplayResult;
          if(m_pDevice->HasFatalError())
            return m_pDevice->FatalErrorCheck();
          if(m_Cmd.m_LastCmdListID != chunk.baked ||
             ser.GetReader()->GetOffset() != chunk.endOffset)
            return RDResult(ResultCode::APIDataCorrupted,
                            "Repeated-list companion command identity changed");
          return ResultCode::Succeeded;
        });
    // No outer stream event/chunk is replayed twice. The helper only records list commands.
    ser.GetReader()->SetOffset(closeEnd);
    return companion;
  };
  if(allowCheckpoints && IsActiveReplaying(m_State) && !partial && startEventID == 1)
  {
    uint64_t resumeOffset = 0;
    uint32_t nextEvent = 0, nextAction = 0;
    if(m_pDevice->RestoreReplayCheckpoint(endEventID, resumeOffset, nextEvent, nextAction))
    {
      m_pDevice->MarkCheckpointReplayWrites();
      if(!m_pDevice->ApplyCheckpointReplayBufferPatches())
        return RDResult(ResultCode::APIReplayFailed,
                        "Could not apply current shared buffer values after checkpoint restore");
      m_Cmd.m_RootEventID = nextEvent;
      m_Cmd.m_RootActionID = nextAction;
      // Resume is still a root command stream, not the existing single-list partial path.
      // Keep FirstEventID == 1 so queries, Reset..Close and submission event accounting work.
      m_Cmd.m_PreparedReplayEnabled = false;
      rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> continuation;
      rdcstr continuationError;
      const bool targetContinuation =
          Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_TARGET_CONTINUATION") != "1";
      if(!m_Cmd.GetCheckpointContinuation(resumeOffset, nextEvent, continuation, continuationError,
                                         NULL, targetContinuation ? endEventID : ~0U))
        return RDResult(ResultCode::APIReplayFailed, continuationError);
      PerformanceTimer continuationTimer;
      uint64_t continuationBytes = 0, metadataBytes = 0;
      uint32_t decodedChunks = 0, metadataChunks = 0;
      std::set<ResourceId> decodedLists, metadataLists;
      uint32_t indirectChunks = 0, indirectRecorded = 0;
      // Reconstruct only outstanding command recordings. These whitelisted chunks record native
      // commands and CPU pipeline state; they never execute a queue or read back GPU data. The
      // resource checkpoint remains untouched until the normal suffix reaches their submission.
      for(const auto &chunk : continuation)
      {
        // Lists whose first relevant submission is after this request never get a native
        // rerecord mapping. Keep their Reset/Close identities and interleaved ordering, but use
        // loading's final (including indirect-count compression) cursor instead of decoding
        // command payloads. If Reset disagrees with the plan, use the original handler.
        if(m_Cmd.ApplyCheckpointContinuationMetadata(chunk))
        {
          metadataChunks++;
          metadataBytes += chunk.endOffset - chunk.offset;
          metadataLists.insert(chunk.baked);
          continue;
        }
        decodedChunks++;
        decodedLists.insert(chunk.baked);
        ser.GetReader()->SetOffset(chunk.offset);
        m_Cmd.m_CurChunkOffset = chunk.offset;
        D3D12Chunk context;
        {
          ScopedReplayTimingSection timing(ReplayTimingSection::ChunkFraming);
          context = ser.ReadChunk<D3D12Chunk>();
        }
        if(ser.GetReader()->IsErrored() || context != chunk.type)
          return RDResult(ResultCode::APIDataCorrupted,
                          "Checkpoint continuation command does not match its source chunk");
        m_Cmd.m_ChunkMetadata = ser.ChunkMetadata();
        m_Cmd.m_LastCmdListID = ResourceId();
        if(context == D3D12Chunk::List_ExecuteIndirect)
        {
          indirectChunks++;
          if(m_Cmd.InRerecordRange(chunk.baked))
            indirectRecorded++;
        }
        bool success;
        {
          ScopedReplayTimingChunk timing((uint32_t)context);
          success = ProcessChunk(ser, context);
        }
        {
          ScopedReplayTimingSection timing(ReplayTimingSection::ChunkFraming);
          ser.EndChunk();
        }
        if(ser.GetReader()->IsErrored())
          return RDResult(ResultCode::APIDataCorrupted, ser.GetError().message);
        if(!success)
          return m_Cmd.m_FailedReplayResult;
        if(m_pDevice->HasFatalError())
          return m_pDevice->FatalErrorCheck();
        if(m_Cmd.m_LastCmdListID != chunk.baked || ser.GetReader()->GetOffset() != chunk.endOffset)
          return RDResult(ResultCode::APIDataCorrupted,
                          "Checkpoint continuation command-list identity changed");
        m_Cmd.m_BakedCmdListInfo[chunk.baked].curEventID = chunk.eventAfter;
        if(context == D3D12Chunk::List_Close)
        {
          const RDResult companion = recordFullCompanionAtClose(chunk.baked, chunk.endOffset);
          if(companion != ResultCode::Succeeded)
            return companion;
        }
        m_Cmd.m_LastChunk = context;
        continuationBytes += chunk.endOffset - chunk.offset;
        AddReplayTimingCounter(ReplayTimingCounter::ChunkBytes, chunk.endOffset - chunk.offset);
      }
      if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
      {
        const rdcstr line = StringFormat::Fmt(
            "api=D3D12Checkpoint op=continuation target=%u chunks=%zu bytes=%llu "
            "targetAware=%u decodedChunks=%u metadataChunks=%u metadataBytes=%llu "
            "decodedLists=%zu metadataLists=%zu indirectChunks=%u indirectRecorded=%u total_ms=%.3f",
            endEventID, continuation.size(), (unsigned long long)continuationBytes,
            targetContinuation ? 1U : 0U, decodedChunks, metadataChunks,
            (unsigned long long)metadataBytes, decodedLists.size(), metadataLists.size(),
            indirectChunks, indirectRecorded, continuationTimer.GetMilliseconds());
        RDCLOG("[EIDTiming] %s", line.c_str());
        AppendReplayTimingLog(line);
      }
      ser.GetReader()->SetOffset(resumeOffset);
      m_Cmd.m_LastCmdListID = ResourceId();
      m_Cmd.m_LastChunk = D3D12Chunk::Queue_ExecuteCommandLists;
    }
  }
  setupTiming.End();

  for(;;)
  {
    if(IsActiveReplaying(m_State) && m_Cmd.m_RootEventID > endEventID)
    {
      // we can just break out if we've done all the events desired.
      // note that the command list events aren't 'real' and we just blaze through them
      break;
    }

    m_Cmd.m_CurChunkOffset = ser.GetReader()->GetOffset();

    D3D12Chunk context;
    {
      ScopedReplayTimingSection timing(ReplayTimingSection::ChunkFraming);
      context = ser.ReadChunk<D3D12Chunk>();
    }

    if(ser.GetReader()->IsErrored())
      return RDResult(ResultCode::APIDataCorrupted, ser.GetError().message);

    m_Cmd.m_ChunkMetadata = ser.ChunkMetadata();

    m_Cmd.m_LastCmdListID = ResourceId();

    bool success;
    {
      ScopedReplayTimingChunk timing((uint32_t)context);
      success = ProcessChunk(ser, context);
    }

    {
      ScopedReplayTimingSection timing(ReplayTimingSection::ChunkFraming);
      ser.EndChunk();
    }

    ScopedReplayTimingSection bookkeepingTiming(ReplayTimingSection::ReplayBookkeeping);

    const uint64_t chunkEndOffset = ser.GetReader()->GetOffset();
    AddReplayTimingCounter(ReplayTimingCounter::ChunkBytes, chunkEndOffset - m_Cmd.m_CurChunkOffset);

    if(ser.GetReader()->IsErrored())
      return RDResult(ResultCode::APIDataCorrupted, ser.GetError().message);

    // if there wasn't a serialisation error, but the chunk didn't succeed, then it's an API replay
    // failure.
    if(!success)
    {
      rdcstr extra;

      if(m_pDevice->GetInfoQueue())
      {
        extra += "\n";

        for(UINT64 i = 0;
            i < m_pDevice->GetInfoQueue()->GetNumStoredMessagesAllowedByRetrievalFilter(); i++)
        {
          SIZE_T len = 0;
          m_pDevice->GetInfoQueue()->GetMessage(i, NULL, &len);

          char *msgbuf = new char[len];
          D3D12_MESSAGE *message = (D3D12_MESSAGE *)msgbuf;

          m_pDevice->GetInfoQueue()->GetMessage(i, message, &len);

          extra += "\n";
          extra += message->pDescription;

          delete[] msgbuf;
        }
      }
      else
      {
        extra +=
            "\n\nMore debugging information may be available by enabling API validation on "
            "replay via `File` -> `Open Capture with Options`";
      }

      if(m_pDevice->HasFatalError())
      {
        RDResult result = m_pDevice->FatalErrorCheck();
        result.message = rdcstr(result.message) + extra;
        return result;
      }

      m_Cmd.m_FailedReplayResult.message = rdcstr(m_Cmd.m_FailedReplayResult.message) + extra;
      return m_Cmd.m_FailedReplayResult;
    }

    if(m_pDevice->HasFatalError())
      return ResultCode::Succeeded;

    if(IsLoading(m_State))
      m_Cmd.ObservePreparedReplayChunk(context, chunkEndOffset);
    else if(IsActiveReplaying(m_State))
    {
      if(context == D3D12Chunk::List_Close)
      {
        m_Cmd.FinishPreparedCommandList(m_Cmd.m_LastCmdListID, chunkEndOffset);
        const RDResult companion = recordFullCompanionAtClose(m_Cmd.m_LastCmdListID, chunkEndOffset);
        if(companion != ResultCode::Succeeded)
          return companion;
      }

      if(context == D3D12Chunk::List_Reset && m_Cmd.m_PreparedSkipEnd > chunkEndOffset)
      {
        const ResourceId baked = m_Cmd.m_LastCmdListID;
        BakedCmdListInfo &info = m_Cmd.m_BakedCmdListInfo[baked];
        m_Cmd.m_PreparedSkippedBytes += m_Cmd.m_PreparedSkipEnd - chunkEndOffset;
        if(info.endChunk > info.beginChunk)
          m_Cmd.m_PreparedSkippedChunks += info.endChunk - info.beginChunk;
        info.curEventID = 0;
        ser.GetReader()->SetOffset(m_Cmd.m_PreparedSkipEnd);
        m_Cmd.m_PreparedSkipEnd = 0;
        m_Cmd.m_LastChunk = D3D12Chunk::List_Close;
        continue;
      }
    }

    if(IsLoading(m_State) || IsStructuredExporting(m_State))
      RenderDoc::Inst().SetProgress(
          LoadProgress::FrameEventsRead,
          float(m_Cmd.m_CurChunkOffset - startOffset) / float(ser.GetReader()->GetSize()));

    if((SystemChunk)context == SystemChunk::CaptureEnd || ser.GetReader()->AtEnd())
      break;

    // break out if we were only executing one event
    if(IsActiveReplaying(m_State) && startEventID == endEventID)
      break;

    m_Cmd.m_LastChunk = context;

    // increment root event ID either if we didn't just replay a cmd
    // buffer event, OR if we are doing a frame sub-section replay,
    // in which case it's up to the calling code to make sure we only
    // replay inside a command list (if we crossed command list
    // boundaries, the event IDs would no longer match up).
    if(m_Cmd.m_LastCmdListID == ResourceId() || startEventID > 1)
    {
      if(context != D3D12Chunk::SetCommandAnnotation)
        m_Cmd.m_RootEventID++;

      if(startEventID > 1)
        ser.GetReader()->SetOffset(GetEvent(m_Cmd.m_RootEventID).fileOffset);
    }
    else
    {
      // these events are completely omitted, so don't increment the curEventID
      if(context != D3D12Chunk::List_Reset && context != D3D12Chunk::List_Close &&
         context != D3D12Chunk::SetCommandAnnotation)
        m_Cmd.m_BakedCmdListInfo[m_Cmd.m_LastCmdListID].curEventID++;
    }

    bookkeepingTiming.End();
    if(IsLoading(m_State))
      m_Cmd.ObserveCheckpointChunk(context, chunkEndOffset);
    else if(allowCheckpoints && IsActiveReplaying(m_State) && !partial && startEventID == 1 &&
            context == D3D12Chunk::Queue_ExecuteCommandLists &&
            m_Cmd.m_RootEventID <= endEventID)
      m_pDevice->SaveReplayCheckpoint(m_Cmd.m_RootEventID - 1, chunkEndOffset,
                                      m_Cmd.m_RootEventID, m_Cmd.m_RootActionID);
  }

  // swap the structure back now that we've accumulated the frame as well.
  if(IsLoading(m_State) || IsStructuredExporting(m_State))
    ser.GetStructuredFile().Swap(*m_pDevice->GetStructuredFile());

  m_StructuredFile = NULL;

  m_Cmd.m_RerecordCmds.clear();

  if(m_Cmd.m_PreparedTimingEnabled)
  {
    const rdcstr preparedLine = StringFormat::Fmt(
        "api=D3D12Prepared enabled=%u attempts=%u hits=%u builds=%u rejectedType=%u "
        "rejectedUnsafe=%u rejectedExisting=%u rejectedBudget=%u skippedChunks=%u "
        "skippedBytes=%llu cacheLists=%zu cacheSourceBytes=%llu",
        m_Cmd.m_PreparedReplayEnabled ? 1U : 0U, m_Cmd.m_PreparedAttempts,
        m_Cmd.m_PreparedCacheHits, m_Cmd.m_PreparedCacheBuilds, m_Cmd.m_PreparedRejectedType,
        m_Cmd.m_PreparedRejectedUnsafe, m_Cmd.m_PreparedRejectedExisting,
        m_Cmd.m_PreparedRejectedBudget, m_Cmd.m_PreparedSkippedChunks,
        (unsigned long long)m_Cmd.m_PreparedSkippedBytes, m_Cmd.m_PreparedCmdLists.size(),
        (unsigned long long)m_Cmd.m_PreparedCacheBytes);
    RDCLOG("[EIDTiming] %s", preparedLine.c_str());

    // Opening/flushing the timing file (and the main diagnostic logger) for every blocker adds
    // measurable latency to the replay being measured. Keep the regular log compact and append
    // the complete diagnostic block to the timing file in one operation.
    rdcstr preparedLog = preparedLine;

    for(const auto &entry : m_Cmd.m_PreparedRejectSummary)
    {
      const D3D12CommandData::PreparedReplayRejectSummary &summary = entry.second;
      const rdcstr rejectedLine = StringFormat::Fmt(
          "api=D3D12PreparedReject start=%u end=%u reason=%s chunk=%s chunkId=%u attempts=%u "
          "firstBaked=%s firstParent=%s observedList=%s observedParent=%s "
          "firstBeginOffset=%llu firstOffset=%llu scope=firstBlocker",
          startEventID, endEventID, PreparedReplayRejectReasonName(summary.reason),
          ToStr(summary.chunk).c_str(), (uint32_t)summary.chunk, summary.attempts,
          ToStr(summary.firstBaked).c_str(), ToStr(summary.firstParent).c_str(),
          ToStr(summary.observedList).c_str(), ToStr(summary.observedParent).c_str(),
          (unsigned long long)summary.firstBeginOffset, (unsigned long long)summary.firstOffset);
      preparedLog += "\n";
      preparedLog += rejectedLine;
    }
    AppendReplayTimingLog(preparedLog);
  }

  return ResultCode::Succeeded;
}

WrappedID3D12GraphicsCommandList::WrappedID3D12GraphicsCommandList(ResourceId id,
                                                                   ID3D12GraphicsCommandList *real,
                                                                   WrappedID3D12Device *device,
                                                                   CaptureState &state)
    : m_RefCounter(real, false), m_pList(real), m_pDevice(device), m_State(state)
{
  RenderDoc::Inst().RegisterMemoryRegion(this, sizeof(WrappedID3D12GraphicsCommandList));

  m_pList1 = NULL;
  m_pList2 = NULL;
  m_pList3 = NULL;
  m_pList4 = NULL;
  m_pList5 = NULL;

  m_WrappedDebug.m_pList = this;
  if(m_pList)
  {
    m_pList->QueryInterface(__uuidof(ID3D12DebugCommandList), (void **)&m_WrappedDebug.m_pReal);
    m_pList->QueryInterface(__uuidof(ID3D12DebugCommandList1), (void **)&m_WrappedDebug.m_pReal1);
    m_pList->QueryInterface(__uuidof(ID3D12DebugCommandList2), (void **)&m_WrappedDebug.m_pReal2);
    m_pList->QueryInterface(__uuidof(ID3D12DebugCommandList3), (void **)&m_WrappedDebug.m_pReal3);

    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList1), (void **)&m_pList1);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList2), (void **)&m_pList2);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList3), (void **)&m_pList3);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList4), (void **)&m_pList4);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList5), (void **)&m_pList5);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList6), (void **)&m_pList6);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList7), (void **)&m_pList7);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList8), (void **)&m_pList8);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList9), (void **)&m_pList9);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList10), (void **)&m_pList10);
  }

  m_ResourceID = id;
  if(id == ResourceId())
    m_ResourceID = ResourceIDGen::GetNewUniqueID();

  RDCEraseEl(m_Init);

  m_ListRecord = NULL;
  m_CreationRecord = NULL;
  m_Cmd = NULL;

  m_CurGfxRootSig = NULL;
  m_CurCompRootSig = NULL;

  if(!RenderDoc::Inst().IsReplayApp())
  {
    m_ListRecord = m_pDevice->GetResourceManager()->AddResourceRecord(m_ResourceID);
    m_ListRecord->type = Resource_GraphicsCommandList;
    m_ListRecord->DataInSerialiser = false;
    m_ListRecord->InternalResource = true;
    m_ListRecord->Length = 0;

    m_ListRecord->cmdInfo = new CmdListRecordingInfo();

    // this is set up in the implicit Reset() right after creation
    m_ListRecord->bakedCommands = NULL;

    // a bit of a hack, we make a parallel resource record with the same lifetime as the command
    // list and make it a parent, so it will hold onto our create chunk and not try to
    // record it (and throw it away with baked commands that are unused), then it'll be pulled
    // into the capture.
    m_CreationRecord =
        m_pDevice->GetResourceManager()->AddResourceRecord(ResourceIDGen::GetNewUniqueID());
    m_CreationRecord->type = Resource_GraphicsCommandList;
    m_CreationRecord->InternalResource = true;

    m_ListRecord->AddParent(m_CreationRecord);
  }
  else
  {
    m_Cmd = m_pDevice->GetQueue()->GetCommandData();
  }

  if(m_pList)
  {
    bool ret = m_pDevice->GetResourceManager()->AddWrapper(this, m_pList);
    if(!ret)
      RDCERR("Error adding wrapper for ID3D12GraphicsCommandList");
  }

  m_pDevice->GetResourceManager()->AddResource(GetResourceID(), this);

  m_pDevice->SoftRef();
}

WrappedID3D12GraphicsCommandList::~WrappedID3D12GraphicsCommandList()
{
  SAFE_RELEASE(m_AMDMarkers);

  for(std::function<void()> &func : m_UnusedCleanupCallbacks)
    func();
  m_UnusedCleanupCallbacks.clear();

  if(m_pList)
    m_pDevice->GetResourceManager()->RemoveWrapper(this, m_pList);

  if(m_CreationRecord)
    m_CreationRecord->Delete(m_pDevice->GetResourceManager());

  if(m_ListRecord && m_ListRecord->bakedCommands)
    m_ListRecord->bakedCommands->Delete(m_pDevice->GetResourceManager());

  if(m_ListRecord)
    m_ListRecord->Delete(m_pDevice->GetResourceManager());

  m_pDevice->GetResourceManager()->ReleaseResource(GetResourceID());

  SAFE_RELEASE(m_WrappedDebug.m_pReal);
  SAFE_RELEASE(m_WrappedDebug.m_pReal1);
  SAFE_RELEASE(m_WrappedDebug.m_pReal2);
  SAFE_RELEASE(m_WrappedDebug.m_pReal3);
  SAFE_RELEASE(m_pList10);
  SAFE_RELEASE(m_pList9);
  SAFE_RELEASE(m_pList8);
  SAFE_RELEASE(m_pList7);
  SAFE_RELEASE(m_pList6);
  SAFE_RELEASE(m_pList5);
  SAFE_RELEASE(m_pList4);
  SAFE_RELEASE(m_pList3);
  SAFE_RELEASE(m_pList2);
  SAFE_RELEASE(m_pList1);
  SAFE_RELEASE(m_pList);
}

bool WrappedID3D12GraphicsCommandList::ValidateRootGPUVA(D3D12_GPU_VIRTUAL_ADDRESS buffer)
{
  if(buffer == 0)
  {
    // silently drop binds of 0 since this sometimes crashes drivers, and is not legal if the
    // address is ever actually accessed (if it is accessed, whatever was bound or never bound here
    // is just as illegal)

    return true;
  }

  return false;
}

WriteSerialiser &WrappedID3D12GraphicsCommandList::GetThreadSerialiser()
{
  return m_pDevice->GetThreadSerialiser();
}

void WrappedID3D12GraphicsCommandList::AddRayDispatches(rdcarray<PatchedRayDispatch::Resources> &dispatches)
{
  dispatches.reserve(dispatches.size() + m_RayDispatches.size());
  for(const PatchedRayDispatch::Resources &r : m_RayDispatches)
  {
    dispatches.push_back(r);
    r.AddRef();
  }
}

rdcstr WrappedID3D12GraphicsCommandList::GetChunkName(uint32_t idx)
{
  if((SystemChunk)idx < SystemChunk::FirstDriverChunk)
    return ToStr((SystemChunk)idx);

  return ToStr((D3D12Chunk)idx);
}

HRESULT STDMETHODCALLTYPE WrappedID3D12GraphicsCommandList::QueryInterface(REFIID riid,
                                                                           void **ppvObject)
{
  if(riid == __uuidof(IUnknown))
  {
    *ppvObject = (IUnknown *)(ID3D12GraphicsCommandList *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList))
  {
    *ppvObject = (ID3D12GraphicsCommandList *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12DebugCommandList))
  {
    if(m_WrappedDebug.m_pReal)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandList *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DebugCommandList1))
  {
    if(m_WrappedDebug.m_pReal1)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandList1 *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DebugCommandList2))
  {
    if(m_WrappedDebug.m_pReal2)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandList2 *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DebugCommandList3))
  {
    if(m_WrappedDebug.m_pReal3)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandList3 *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList1))
  {
    if(m_pList1)
    {
      *ppvObject = (ID3D12GraphicsCommandList1 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList2))
  {
    if(m_pList2)
    {
      *ppvObject = (ID3D12GraphicsCommandList2 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList3))
  {
    if(m_pList3)
    {
      *ppvObject = (ID3D12GraphicsCommandList3 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList4))
  {
    if(m_pList4)
    {
      *ppvObject = (ID3D12GraphicsCommandList4 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList5))
  {
    if(m_pList5)
    {
      *ppvObject = (ID3D12GraphicsCommandList5 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList6))
  {
    if(m_pList6)
    {
      *ppvObject = (ID3D12GraphicsCommandList6 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList7))
  {
    if(m_pList7)
    {
      *ppvObject = (ID3D12GraphicsCommandList7 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList8))
  {
    if(m_pList8)
    {
      *ppvObject = (ID3D12GraphicsCommandList8 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList9))
  {
    if(m_pList9)
    {
      *ppvObject = (ID3D12GraphicsCommandList9 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList10))
  {
    if(m_pList10)
    {
      *ppvObject = (ID3D12GraphicsCommandList10 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12CommandList))
  {
    *ppvObject = (ID3D12CommandList *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12Pageable))
  {
    *ppvObject = (ID3D12Pageable *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12DeviceChild))
  {
    *ppvObject = (ID3D12DeviceChild *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12Object))
  {
    *ppvObject = (ID3D12DeviceChild *)this;
    AddRef();
    return S_OK;
  }

  return m_RefCounter.QueryInterface("ID3D12GraphicsCommandList", riid, ppvObject);
}

void BakedCmdListInfo::ShiftForRemoved(uint32_t shiftActionID, uint32_t shiftEID, size_t idx)
{
  rdcarray<D3D12ActionTreeNode> &actions = action->children;

  actionCount -= shiftActionID;
  eventCount -= shiftEID;

  if(idx < actions.size())
  {
    const uint32_t firstFollowingEvent = actions[idx].action.eventId;
    // ExecuteIndirect initially reserves MaxCommandCount events. Loading later removes unused
    // arguments once the count buffer is known. Keep checkpoint stream counters and boundary
    // metadata in the same final EID space as the action tree, including the indirect chunk's
    // eventAfter value (which points just past its pop marker).
    for(CheckpointRecordingChunk &chunk : checkpointChunks)
      if(chunk.eventAfter >= firstFollowingEvent)
        chunk.eventAfter -= shiftEID;
    for(CheckpointQueryOp &query : checkpointQueries)
      if(query.relativeEvent >= firstFollowingEvent)
        query.relativeEvent -= shiftEID;
    for(CheckpointBarrierOp &barrier : checkpointBarriers)
      if(barrier.relativeEvent >= firstFollowingEvent)
        barrier.relativeEvent -= shiftEID;
    for(auto &write : replayWriteFirstEvents)
      if(write.second >= firstFollowingEvent)
        write.second -= shiftEID;
    for(auto &write : replayRootUAVFirstEvents)
      if(write.second >= firstFollowingEvent)
        write.second -= shiftEID;
    for(auto &write : replayUAVFirstEvents)
      if(write.second >= firstFollowingEvent)
        write.second -= shiftEID;
    if(replayFirstUnknownWriteEvent != ~0U && replayFirstUnknownWriteEvent >= firstFollowingEvent)
      replayFirstUnknownWriteEvent -= shiftEID;

    for(size_t i = idx; i < actions.size(); i++)
    {
      // should have no children as we don't push in for markers since they
      // can cross command list boundaries.
      RDCASSERT(actions[i].children.empty());

      actions[i].action.eventId -= shiftEID;
      actions[i].action.actionId -= shiftActionID;

      for(APIEvent &ev : actions[i].action.events)
        ev.eventId -= shiftEID;

      for(size_t u = 0; u < actions[i].resourceUsage.size(); u++)
        actions[i].resourceUsage[u].second.eventId -= shiftEID;
    }

    uint32_t lastEID = actions[idx].action.eventId;

    // shift any resource usage for actions after the removed section

    // patch any subsequent executes
    for(size_t i = 0; i < executeEvents.size(); i++)
    {
      if(executeEvents[i].baseEvent >= lastEID)
        executeEvents[i].baseEvent -= shiftEID;
    }
  }

  for(size_t i = 0; i < curEvents.size(); i++)
  {
    curEvents[i].eventId -= shiftEID;
  }
}

SubresourceStateVector BakedCmdListInfo::GetState(WrappedID3D12Device *device, ResourceId id)
{
  std::map<ResourceId, SubresourceStateVector> data;

  data[id] = device->GetSubresourceStates(id);

  device->GetResourceManager()->ApplyBarriers(barriers, data);

  SubresourceStateVector ret = data[id];

  if(type == D3D12_COMMAND_LIST_TYPE_COMPUTE)
  {
    for(D3D12ResourceLayout &layout : ret)
    {
      if(layout.IsStates())
      {
        D3D12_RESOURCE_STATES states = layout.ToStates();
        states &= ~(D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE | D3D12_RESOURCE_STATE_STREAM_OUT |
                    D3D12_RESOURCE_STATE_INDEX_BUFFER | D3D12_RESOURCE_STATE_RENDER_TARGET |
                    D3D12_RESOURCE_STATE_DEPTH_WRITE | D3D12_RESOURCE_STATE_DEPTH_READ);
        layout = D3D12ResourceLayout::FromStates(states);
      }
    }
  }

  return ret;
}

D3D12CommandData::D3D12CommandData()
{
  m_CurChunkOffset = 0;

  m_IndirectOffset = 0;

  m_RootEventID = 1;
  m_RootActionID = 1;
  m_FirstEventID = 0;
  m_LastEventID = ~0U;

  m_StructuredFile = NULL;

  m_pDevice = NULL;

  m_ActionCallback = NULL;

  m_AddedAction = false;

  m_RootActionStack.push_back(&m_ParentAction);
}

void D3D12CommandData::GetIndirectBuffer(size_t size, ID3D12Resource **buf, uint64_t *offs)
{
  // check if we need to allocate a new buffer
  if(m_IndirectBuffers.empty() || m_IndirectOffset + size > m_IndirectSize)
  {
    D3D12_RESOURCE_DESC indirectDesc;
    indirectDesc.Alignment = 0;
    indirectDesc.DepthOrArraySize = 1;
    indirectDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    indirectDesc.Flags = D3D12_RESOURCE_FLAG_NONE;
    indirectDesc.Format = DXGI_FORMAT_UNKNOWN;
    indirectDesc.Height = 1;
    indirectDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    indirectDesc.MipLevels = 1;
    indirectDesc.SampleDesc.Count = 1;
    indirectDesc.SampleDesc.Quality = 0;
    indirectDesc.Width = RDCMAX(AlignUp((uint64_t)size, 64ULL), m_IndirectSize);

    D3D12_HEAP_PROPERTIES heapProps;
    heapProps.Type = D3D12_HEAP_TYPE_READBACK;
    heapProps.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
    heapProps.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
    heapProps.CreationNodeMask = 1;
    heapProps.VisibleNodeMask = 1;

    ID3D12Resource *argbuf = NULL;

    HRESULT hr = m_pDevice->CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &indirectDesc,
                                                    D3D12_RESOURCE_STATE_COPY_DEST, NULL,
                                                    __uuidof(ID3D12Resource), (void **)&argbuf);
    m_pDevice->RemoveReplayResource(GetResID(argbuf));

    SetObjName(argbuf, StringFormat::Fmt("Indirect Readback Buf (%llu bytes)", (uint64_t)size));

    if(FAILED(hr))
      RDCERR("Failed to create indirect buffer, HRESULT: %s", ToStr(hr).c_str());

    m_IndirectBuffers.push_back(argbuf);
    m_IndirectOffset = 0;
  }

  *buf = m_IndirectBuffers.back();
  *offs = m_IndirectOffset;

  m_IndirectOffset = AlignUp16(m_IndirectOffset + size);
}

uint32_t D3D12CommandData::HandlePreCallback(ID3D12GraphicsCommandListX *list, ActionFlags type,
                                             uint32_t multiDrawOffset)
{
  if(!m_ActionCallback)
    return 0;

  // look up the EID this action came from
  ActionUse use(m_CurChunkOffset, 0);
  auto it = std::lower_bound(m_ActionUses.begin(), m_ActionUses.end(), use);

  if(it == m_ActionUses.end())
  {
    RDCERR("Couldn't find action use entry for %llu", m_CurChunkOffset);
    return 0;
  }

  uint32_t eventId = it->eventId;

  RDCASSERT(eventId != 0);

  // handle all aliases of this action as long as it's not a multidraw
  const ActionDescription *action = m_pDevice->GetAction(eventId);

  if(action == NULL || !(action->flags & ActionFlags::MultiAction))
  {
    ++it;
    while(it != m_ActionUses.end() && it->fileOffset == m_CurChunkOffset)
    {
      m_ActionCallback->AliasEvent(eventId, it->eventId);
      ++it;
    }
  }

  eventId += multiDrawOffset;

  switch(type)
  {
    case ActionFlags::MeshDispatch:
    case ActionFlags::Drawcall:
    {
      m_ActionCallback->PreDraw(eventId, list);
      break;
    }
    case ActionFlags::Dispatch:
    case ActionFlags::DispatchRay:
    {
      m_ActionCallback->PreDispatch(eventId, list);
      break;
    }
    default:
    {
      m_ActionCallback->PreMisc(eventId, type, list);
      break;
    }
  }

  return eventId;
}

bool D3D12CommandData::InRerecordRange(ResourceId cmdid)
{
  // if we have an outside command list, assume the range is valid and we're replaying all events
  // onto it.
  if(m_OutsideCmdList != NULL)
    return true;

  // if not, check if we're one of the actual partial command buffers and check to see if we're in
  // the range for their partial replay.
  for(int p = 0; p < ePartialNum; p++)
  {
    if(cmdid == m_Partial[p].partialParent)
    {
      return m_BakedCmdListInfo[m_Partial[p].partialParent].curEventID <=
             m_LastEventID - m_Partial[p].baseEvent;
    }
  }

  // otherwise just check if we have a re-record command list for this, as then we're doing a full
  // re-record and replay
  return m_RerecordCmds.find(cmdid) != m_RerecordCmds.end();
}

bool D3D12CommandData::HasRerecordCmdList(ResourceId cmdid)
{
  if(m_OutsideCmdList != NULL)
    return true;

  return m_RerecordCmds.find(cmdid) != m_RerecordCmds.end();
}

bool D3D12CommandData::IsPartialCmdList(ResourceId cmdid)
{
  if(m_OutsideCmdList != NULL)
    return true;

  for(int p = 0; p < ePartialNum; p++)
    if(cmdid == m_Partial[p].partialParent)
      return true;

  return false;
}

ID3D12GraphicsCommandListX *D3D12CommandData::RerecordCmdList(ResourceId cmdid,
                                                              PartialReplayIndex partialType)
{
  if(m_OutsideCmdList != NULL)
    return m_OutsideCmdList;

  auto it = m_RerecordCmds.find(cmdid);

  if(it == m_RerecordCmds.end())
  {
    RDCERR("Didn't generate re-record command for %s", ToStr(cmdid).c_str());
    return NULL;
  }

  return it->second;
}

bool D3D12PreparedQueryTracker::Begin(ResourceId heap, D3D12_QUERY_TYPE type, UINT index)
{
  if(heap == ResourceId() || type == D3D12_QUERY_TYPE_TIMESTAMP)
    return false;

  for(const Query &query : pending)
    if(query.heap == heap && query.index == index)
      return false;

  pending.push_back({heap, type, index});
  return true;
}

bool D3D12PreparedQueryTracker::End(ResourceId heap, D3D12_QUERY_TYPE type, UINT index)
{
  if(heap == ResourceId())
    return false;

  // Timestamps have only an EndQuery and deliberately do not open a query scope.
  if(type == D3D12_QUERY_TYPE_TIMESTAMP)
    return true;

  for(size_t i = 0; i < pending.size(); i++)
  {
    const Query &query = pending[i];
    if(query.heap == heap && query.type == type && query.index == index)
    {
      pending.erase(i);
      return true;
    }
  }
  return false;
}

static bool IsPreparedReplaySafeChunk(D3D12Chunk chunk)
{
  // Whitelist only commands which record entirely into a native command list and whose referenced
  // objects remain live for the capture. Anything with replay-time readback, generated argument
  // buffers, nested command-list lifetime, or queue/CPU side effects fails closed.
  switch(chunk)
  {
    case D3D12Chunk::List_Close:
    case D3D12Chunk::List_Reset:
    case D3D12Chunk::List_ResourceBarrier:
    // Query validity and saved fallback results are fixed after capture loading. Active replay
    // records native queries/resolves/copies without changing that metadata or allocating buffers.
    // Paired non-timestamp scopes must also pass the per-list loading check below.
    case D3D12Chunk::List_BeginQuery:
    case D3D12Chunk::List_EndQuery:
    case D3D12Chunk::List_ResolveQueryData:
    case D3D12Chunk::List_DrawIndexedInstanced:
    case D3D12Chunk::List_DrawInstanced:
    case D3D12Chunk::List_Dispatch:
    case D3D12Chunk::List_CopyBufferRegion:
    case D3D12Chunk::List_CopyTextureRegion:
    case D3D12Chunk::List_CopyResource:
    case D3D12Chunk::List_ResolveSubresource:
    case D3D12Chunk::List_ClearRenderTargetView:
    case D3D12Chunk::List_ClearDepthStencilView:
    case D3D12Chunk::List_IASetPrimitiveTopology:
    case D3D12Chunk::List_IASetIndexBuffer:
    case D3D12Chunk::List_IASetVertexBuffers:
    case D3D12Chunk::List_SOSetTargets:
    case D3D12Chunk::List_RSSetViewports:
    case D3D12Chunk::List_RSSetScissorRects:
    case D3D12Chunk::List_SetPipelineState:
    case D3D12Chunk::List_SetDescriptorHeaps:
    case D3D12Chunk::List_OMSetRenderTargets:
    case D3D12Chunk::List_OMSetStencilRef:
    case D3D12Chunk::List_OMSetBlendFactor:
    case D3D12Chunk::List_SetGraphicsRootDescriptorTable:
    case D3D12Chunk::List_SetGraphicsRootSignature:
    case D3D12Chunk::List_SetGraphicsRoot32BitConstant:
    case D3D12Chunk::List_SetGraphicsRoot32BitConstants:
    case D3D12Chunk::List_SetGraphicsRootConstantBufferView:
    case D3D12Chunk::List_SetGraphicsRootShaderResourceView:
    case D3D12Chunk::List_SetGraphicsRootUnorderedAccessView:
    case D3D12Chunk::List_SetComputeRootDescriptorTable:
    case D3D12Chunk::List_SetComputeRootSignature:
    case D3D12Chunk::List_SetComputeRoot32BitConstant:
    case D3D12Chunk::List_SetComputeRoot32BitConstants:
    case D3D12Chunk::List_SetComputeRootConstantBufferView:
    case D3D12Chunk::List_SetComputeRootShaderResourceView:
    case D3D12Chunk::List_SetComputeRootUnorderedAccessView:
    case D3D12Chunk::List_OMSetDepthBounds:
    case D3D12Chunk::List_ResolveSubresourceRegion:
    case D3D12Chunk::List_SetSamplePositions:
    case D3D12Chunk::List_SetViewInstanceMask:
    case D3D12Chunk::List_BeginRenderPass:
    case D3D12Chunk::List_EndRenderPass:
    case D3D12Chunk::List_RSSetShadingRate:
    case D3D12Chunk::List_RSSetShadingRateImage:
    case D3D12Chunk::List_OMSetFrontAndBackStencilRef:
    case D3D12Chunk::List_RSSetDepthBias:
    case D3D12Chunk::List_IASetIndexBufferStripCutValue:
    case D3D12Chunk::List_DispatchMesh:
    case D3D12Chunk::List_ClearState:
    case D3D12Chunk::PushMarker:
    case D3D12Chunk::SetMarker:
    case D3D12Chunk::PopMarker:
    case D3D12Chunk::SetCommandAnnotation: return true;
    default: break;
  }

  return false;
}

static bool IsCheckpointContinuationSafeChunk(D3D12Chunk chunk)
{
  if(IsPreparedReplaySafeChunk(chunk))
    return true;

  // Continuation re-runs the original handler and consumes temporary CPU descriptors now; it
  // does not retain a generated native list across replays. Keep this separate from PreparedReplay.
  switch(chunk)
  {
    case D3D12Chunk::List_SetPredication:
    case D3D12Chunk::List_WriteBufferImmediate:
    case D3D12Chunk::List_ClearUnorderedAccessViewUint:
    case D3D12Chunk::List_ClearUnorderedAccessViewFloat:
    case D3D12Chunk::List_AtomicCopyBufferUINT:
    case D3D12Chunk::List_AtomicCopyBufferUINT64:
    // CopyTiles records a copy, not tile mappings. Written sparse storage and queue mapping
    // changes are still independently rejected by resource and stream checkpoint validation.
    case D3D12Chunk::List_CopyTiles:
    // Optional discard-pattern setup fills only debug-owned UPLOAD buffers and records work into
    // this list. The captured destination is not changed until the original suffix submission.
    case D3D12Chunk::List_DiscardResource: return true;
    default: break;
  }
  return false;
}

static bool IsCompletedReplayOccurrence(uint32_t base, uint32_t length, uint32_t partialBase,
                                        uint32_t target)
{
  return length > 0 && base < partialBase && uint64_t(base) + length <= target;
}

static bool SameLiveForwardOccurrence(const rdcarray<uint32_t> &submissions, uint32_t base,
                                     uint32_t length, uint32_t current, uint32_t target)
{
  return length > 0 && submissions.contains(base) && base <= current && current < target &&
         uint64_t(target) < uint64_t(base) + length;
}

static bool CompletedLiveForwardIndirect(const BakedCmdListInfo::CheckpointRecordingChunk &chunk,
                                         uint64_t currentOffset, uint32_t currentRelative,
                                         rdcstr &reason)
{
  if(chunk.offset >= currentOffset)
  {
    reason = "indirectAtEndpointOrForwardRange";
    return false;
  }
  // eventAfter is the first following relative EID after loading has removed unused indirect
  // children. It includes the group's closing marker; never infer completion from a child index.
  if(chunk.eventAfter == 0 || chunk.eventAfter > currentRelative)
  {
    reason = "indirectPrefixNotCompleted";
    return false;
  }
  // Loading's signature proof excludes GPU addresses, ray dispatch, and unknown argument types.
  // This prefix has already executed; no old native list or patched argument source is reused.
  if(!chunk.directIndirect)
  {
    reason = "indirectPrefixAddressRayOrUnknown";
    return false;
  }
  return true;
}

static bool LiveForwardCommandsSafe(const BakedCmdListInfo &info, uint64_t currentOffset,
                                   uint64_t targetOffset, uint32_t currentRelative,
                                   uint32_t targetRelative, rdcstr &reason)
{
  if(info.type != D3D12_COMMAND_LIST_TYPE_DIRECT || !info.checkpointBegin ||
     info.checkpointBegin >= currentOffset || currentOffset >= targetOffset ||
     info.checkpointEnd <= targetOffset || info.checkpointChunks.empty() ||
     info.checkpointChunks.front().type != D3D12Chunk::List_Reset)
  {
    reason = "missingDirectRecording";
    return false;
  }
  for(const auto &chunk : info.checkpointChunks)
  {
    if(chunk.offset > targetOffset)
      break;
    // ApplyState cannot reopen these native scopes. Reject them even before the current draw:
    // ordinary partial Close may have repaired an outstanding query, hiding its original scope.
    switch(chunk.type)
    {
      case D3D12Chunk::List_ExecuteIndirect:
        if(!CompletedLiveForwardIndirect(chunk, currentOffset, currentRelative, reason))
          return false;
        break;
      case D3D12Chunk::List_BeginQuery:
      case D3D12Chunk::List_BeginRenderPass:
      case D3D12Chunk::List_EndRenderPass:
      case D3D12Chunk::List_DispatchMesh:
      case D3D12Chunk::List_ClearState:
        reason = "nativeScopeOrUnsupportedState";
        return false;
      case D3D12Chunk::List_EndQuery:
      case D3D12Chunk::List_ResolveQueryData:
        // Completed timestamp values and resolves before current remain physically resident.
        // Do not run the old partial handler over new queries: it deliberately skips EndQuery.
        if(chunk.offset > currentOffset)
        {
          reason = "queryInForwardRange";
          return false;
        }
        break;
      default:
        if(!IsCheckpointContinuationSafeChunk(chunk.type))
        {
          reason = StringFormat::Fmt("unsupportedChunk:%s", ToStr(chunk.type).c_str());
          return false;
        }
        break;
    }
  }
  for(const auto &query : info.checkpointQueries)
    if(query.relativeEvent <= currentRelative && !query.resolve &&
       (!query.completed || query.type != D3D12_QUERY_TYPE_TIMESTAMP))
    {
      reason = "nonTimestampPrefixQuery";
      return false;
    }
  for(const auto &barrier : info.checkpointBarriers)
    if(barrier.relativeEvent <= targetRelative &&
       barrier.type == D3D12_RESOURCE_BARRIER_TYPE_TRANSITION &&
       barrier.flags != D3D12_RESOURCE_BARRIER_FLAG_NONE)
    {
      reason = "splitBarrierPrefix";
      return false;
    }
  return true;
}

static bool LiveForwardEventsMatch(const BakedCmdListInfo &info, const rdcarray<APIEvent> &events,
                                  ResourceId baked, uint32_t current, uint32_t target,
                                  rdcstr &reason)
{
  if(target >= events.size())
  {
    reason = "missingEventIndex";
    return false;
  }
  uint64_t previous = 0;
  for(uint64_t eid = current; eid <= target; eid++)
  {
    const APIEvent &event = events[(size_t)eid];
    if(event.eventId != eid || (eid != current && event.fileOffset <= previous))
    {
      reason = "nonLinearEventRange";
      return false;
    }
    const auto found = std::lower_bound(
        info.checkpointChunks.begin(), info.checkpointChunks.end(), event.fileOffset,
        [](const BakedCmdListInfo::CheckpointRecordingChunk &chunk, uint64_t offset) {
          return chunk.offset < offset;
        });
    if(found == info.checkpointChunks.end() || found->offset != event.fileOffset ||
       found->baked != baked || found->type == D3D12Chunk::List_Reset ||
       found->type == D3D12Chunk::List_Close)
    {
      reason = "eventOutsideRecording";
      return false;
    }
    previous = event.fileOffset;
  }
  return true;
}

bool D3D12CommandData::HasResolvedLiveForwardState() const
{
  const auto &pending = m_RenderState.indirectState;
  return pending.argsBuf == NULL && pending.argsOffs == 0 && pending.comSig == NULL &&
         pending.argsToProcess == 0 && m_IndirectData.commandSig == NULL &&
         m_IndirectData.argsBuffer == NULL && m_IndirectData.argsOffset == 0;
}

bool D3D12CommandData::ValidateLiveForwardRange(ResourceId baked, uint32_t baseEvent,
                                               uint32_t currentEvent, uint32_t targetEvent,
                                               rdcstr &reason) const
{
  reason.clear();
  if(!HasResolvedLiveForwardState())
  {
    reason = "unresolvedIndirectState";
    return false;
  }
  const auto info = m_BakedCmdListInfo.find(baked);
  const auto submissions = m_Partial[Primary].cmdListExecs.find(baked);
  if(info == m_BakedCmdListInfo.end() || submissions == m_Partial[Primary].cmdListExecs.end() ||
     !SameLiveForwardOccurrence(submissions->second, baseEvent, info->second.eventCount,
                                currentEvent, targetEvent))
  {
    reason = "differentSubmissionOrDirection";
    return false;
  }
  if(!m_pDevice)
  {
    reason = "missingDevice";
    return false;
  }
  for(uint32_t event : {currentEvent, targetEvent})
  {
    const ActionDescription *action = m_pDevice->GetAction(event);
    if(!action || action->drawIndex != 0 ||
       !(action->flags & (ActionFlags::Drawcall | ActionFlags::Dispatch)) ||
       (action->flags & (ActionFlags::MultiAction | ActionFlags::DispatchRay | ActionFlags::MeshDispatch)))
    {
      reason = "notOrdinaryDrawOrDispatch";
      return false;
    }
  }
  return LiveForwardEventsMatch(info->second, m_Events, baked, currentEvent, targetEvent, reason) &&
         LiveForwardCommandsSafe(info->second, m_Events[currentEvent].fileOffset,
                                  m_Events[targetEvent].fileOffset, currentEvent - baseEvent,
                                  targetEvent - baseEvent, reason);
}

static bool IsFullReplayCompanionSafe(const BakedCmdListInfo &info)
{
  // Recording the companion at the original Close must observe the same CPU descriptor values
  // as the first recording. Contiguity excludes intervening descriptor writes and other lists.
  if(info.type != D3D12_COMMAND_LIST_TYPE_DIRECT || !info.preparedReplaySafe ||
     info.preparedReplayEnd <= info.preparedReplayBegin || !info.checkpointBarriers.empty() ||
     info.checkpointChunks.size() < 2 ||
     info.checkpointChunks.front().type != D3D12Chunk::List_Reset ||
     info.checkpointChunks.back().type != D3D12Chunk::List_Close)
    return false;

  uint64_t previous = info.preparedReplayBegin;
  for(const auto &chunk : info.checkpointChunks)
  {
    if(chunk.offset != previous || chunk.endOffset <= chunk.offset ||
       !IsPreparedReplaySafeChunk(chunk.type))
      return false;
    previous = chunk.endOffset;
    switch(chunk.type)
    {
      // Keep the first implementation free of nested recording contexts, render-pass repair,
      // generated GPU addresses, and callback/annotation state outside the local state below.
      case D3D12Chunk::List_BeginRenderPass:
      case D3D12Chunk::List_EndRenderPass:
      case D3D12Chunk::SetCommandAnnotation: return false;
      default: break;
    }
  }
  return previous == info.preparedReplayEnd;
}

// Copy only mutable recording fields. BakedCmdListInfo owns an action pointer and also contains
// immutable loading indexes: copying or replacing that whole object would double-free/drop them.
struct ReplayCompanionMutableState
{
  ReplayCompanionMutableState(const BakedCmdListInfo &info)
      : state(info.state), barriers(info.barriers), queries(info.m_OutstandingQueries),
        markerCount(info.markerCount), event(info.curEventID)
  {
  }
  void Restore(BakedCmdListInfo &info) const
  {
    info.state = state;
    info.barriers = barriers;
    info.m_OutstandingQueries = queries;
    info.markerCount = markerCount;
    info.curEventID = event;
  }
  D3D12RenderState state;
  BarrierSet barriers;
  rdcarray<BakedCmdListInfo::OutstandingQuery> queries;
  int markerCount;
  uint32_t event;
};

static bool IsCheckpointDirectIndirectSignature(const D3D12CommandSignature &signature)
{
  if(signature.raytraced || signature.arguments.empty())
    return false;
  for(const D3D12_INDIRECT_ARGUMENT_DESC &argument : signature.arguments)
  {
    switch(argument.Type)
    {
      case D3D12_INDIRECT_ARGUMENT_TYPE_DRAW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_MESH:
      case D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT: break;
      // Address-bearing VB/IB/CBV/SRV/UAV and ray arguments depend on generated GPU patch buffers
      // and a shared upload ring. Unknown future arguments are not implicitly continuation-safe.
      default: return false;
    }
  }
  return true;
}

#if ENABLED(ENABLE_UNIT_TESTS)

#include "catch/catch.hpp"

TEST_CASE("D3D12 live forward stays inside the actual primary submission instance",
          "[d3d12][replay][live-forward]")
{
  const rdcarray<uint32_t> submissions = {44, 68};
  CHECK(SameLiveForwardOccurrence(submissions, 44, 19, 50, 58));
  CHECK(SameLiveForwardOccurrence(submissions, 68, 19, 74, 82));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 44, 19, 58, 82));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 68, 19, 58, 82));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 68, 19, 82, 82));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 68, 19, 82, 74));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 68, 19, 82, 87));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 69, 19, 74, 82));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 68, 0, 74, 82));
  const rdcarray<uint32_t> high = {~0U - 10};
  CHECK(SameLiveForwardOccurrence(high, ~0U - 10, 20, ~0U - 2, ~0U - 1));
  CHECK_FALSE(SameLiveForwardOccurrence(high, ~0U - 10, 20, ~0U - 2, 1));
}

TEST_CASE("D3D12 live forward requires a dense unambiguous API event interval",
          "[d3d12][replay][live-forward]")
{
  BakedCmdListInfo info;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  const ResourceId other = ResourceIDGen::GetNewUniqueID();
  info.checkpointChunks = {{baked, 10, 20, 0, D3D12Chunk::List_Reset},
                           {baked, 20, 30, 1, D3D12Chunk::List_DrawInstanced},
                           {baked, 30, 40, 2, D3D12Chunk::List_SetPipelineState},
                           {baked, 40, 50, 3, D3D12Chunk::List_DrawInstanced},
                           {baked, 50, 60, 0, D3D12Chunk::List_Close}};
  rdcarray<APIEvent> events;
  events.resize(8);
  for(uint32_t eid = 5; eid <= 7; eid++)
  {
    events[eid].eventId = eid;
    events[eid].fileOffset = 20 + 10 * (eid - 5);
  }
  rdcstr reason;
  CHECK(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  events[6].eventId = 0;
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  events[6].eventId = 6;
  events[6].fileOffset = 20; // indirect children may share one original chunk
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  events[6].fileOffset = 31;
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  events[6].fileOffset = 30;
  info.checkpointChunks[2].baked = other;
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  info.checkpointChunks[2].baked = baked;
  info.checkpointChunks[2].type = D3D12Chunk::List_Close;
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  info.checkpointChunks[2].type = D3D12Chunk::List_SetPipelineState;
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 8, reason));
}

TEST_CASE("D3D12 live forward excludes native scopes and preserves resident prefix queries",
          "[d3d12][replay][live-forward]")
{
  BakedCmdListInfo info;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  info.checkpointBegin = 10;
  info.checkpointEnd = 80;
  info.checkpointChunks = {{baked, 10, 20, 0, D3D12Chunk::List_Reset},
                           {baked, 20, 30, 1, D3D12Chunk::List_EndQuery},
                           {baked, 30, 40, 2, D3D12Chunk::List_DrawInstanced},
                           {baked, 40, 50, 3, D3D12Chunk::List_ResourceBarrier},
                           {baked, 50, 60, 4, D3D12Chunk::List_DrawInstanced},
                           {baked, 60, 70, 5, D3D12Chunk::List_ExecuteIndirect},
                           {baked, 70, 80, 0, D3D12Chunk::List_Close}};
  BakedCmdListInfo::CheckpointQueryOp query;
  query.relativeEvent = 1;
  query.type = D3D12_QUERY_TYPE_TIMESTAMP;
  query.completed = true;
  info.checkpointQueries.push_back(query);
  rdcstr reason;
  CHECK(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  // Even a query scope which partial Close repaired cannot be reconstructed by ApplyState.
  info.checkpointChunks[1].type = D3D12Chunk::List_BeginQuery;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointChunks[1].type = D3D12Chunk::List_EndQuery;
  info.checkpointQueries[0].type = D3D12_QUERY_TYPE_OCCLUSION;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointQueries[0].type = D3D12_QUERY_TYPE_TIMESTAMP;
  info.checkpointQueries[0].completed = false;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointQueries[0].completed = true;
  for(D3D12Chunk unsupported : {D3D12Chunk::List_EndQuery, D3D12Chunk::List_ResolveQueryData,
                                D3D12Chunk::List_BeginRenderPass, D3D12Chunk::List_EndRenderPass,
                                D3D12Chunk::List_ExecuteIndirect, D3D12Chunk::List_ExecuteBundle,
                                D3D12Chunk::List_DispatchRays, D3D12Chunk::List_ClearState})
  {
    info.checkpointChunks[3].type = unsupported;
    CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  }
  info.checkpointChunks[3].type = D3D12Chunk::List_ResourceBarrier;
  BakedCmdListInfo::CheckpointBarrierOp barrier;
  barrier.relativeEvent = 3;
  barrier.type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
  info.checkpointBarriers.push_back(barrier);
  CHECK(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointBarriers[0].type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
  info.checkpointBarriers[0].flags = D3D12_RESOURCE_BARRIER_FLAG_BEGIN_ONLY;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointBarriers[0].flags = D3D12_RESOURCE_BARRIER_FLAG_END_ONLY;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointBarriers[0].relativeEvent = 5;
  CHECK(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.type = D3D12_COMMAND_LIST_TYPE_COMPUTE;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
}

TEST_CASE("D3D12 live forward accepts only completed direct indirect prefixes with resolved state",
          "[d3d12][replay][live-forward]")
{
  BakedCmdListInfo info;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  info.checkpointBegin = 10;
  info.checkpointEnd = 80;
  info.checkpointChunks = {{baked, 10, 20, 0, D3D12Chunk::List_Reset},
                           {baked, 20, 30, 2, D3D12Chunk::List_ExecuteIndirect, true},
                           {baked, 30, 40, 3, D3D12Chunk::List_DrawInstanced},
                           {baked, 40, 50, 4, D3D12Chunk::List_SetPipelineState},
                           {baked, 50, 60, 5, D3D12Chunk::List_DrawInstanced},
                           {baked, 60, 70, 6, D3D12Chunk::List_ExecuteIndirect},
                           {baked, 70, 80, 0, D3D12Chunk::List_Close}};
  rdcstr reason;
  CHECK(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  // The compacted group's first following event may be the current ordinary draw, but not later.
  info.checkpointChunks[1].eventAfter = 3;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  CHECK(reason == "indirectPrefixNotCompleted");
  info.checkpointChunks[1].eventAfter = 0;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointChunks[1].eventAfter = 2;
  info.checkpointChunks[1].directIndirect = false; // GPU VA, ray, or unproved signature
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  CHECK(reason == "indirectPrefixAddressRayOrUnknown");
  info.checkpointChunks[1].directIndirect = true;
  // Even the first indirect child can have drawIndex zero: the native chunk must precede current.
  CHECK_FALSE(LiveForwardCommandsSafe(info, 20, 50, 2, 4, reason));
  CHECK(reason == "indirectAtEndpointOrForwardRange");
  info.checkpointChunks[3].type = D3D12Chunk::List_ExecuteIndirect;
  info.checkpointChunks[3].directIndirect = true;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  CHECK(reason == "indirectAtEndpointOrForwardRange");
  info.checkpointChunks[3].type = D3D12Chunk::List_SetPipelineState;
  info.checkpointChunks[4].type = D3D12Chunk::List_ExecuteIndirect;
  info.checkpointChunks[4].directIndirect = true;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointChunks[4].type = D3D12Chunk::List_DrawInstanced;
  CHECK(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));

  D3D12CommandData command;
  CHECK(command.HasResolvedLiveForwardState());
  command.m_RenderState.indirectState.argsToProcess = 1;
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_RenderState.indirectState = {};
  command.m_RenderState.indirectState.argsOffs = 16;
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_RenderState.indirectState = {};
  // These borrowed addresses are never dereferenced or owned; only pending-pointer presence is tested.
  command.m_RenderState.indirectState.argsBuf = reinterpret_cast<ID3D12Resource *>(&command);
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_RenderState.indirectState = {};
  command.m_RenderState.indirectState.comSig = reinterpret_cast<ID3D12CommandSignature *>(&command);
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_RenderState.indirectState = {};
  command.m_IndirectData.argsOffset = 16;
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_IndirectData = {};
  command.m_IndirectData.argsBuffer = reinterpret_cast<ID3D12Resource *>(&command);
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_IndirectData = {};
  command.m_IndirectData.commandSig = reinterpret_cast<ID3D12CommandSignature *>(&command);
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_IndirectData = {};
  CHECK(command.HasResolvedLiveForwardState());
}

TEST_CASE("D3D12 repeated submissions choose full versions only for completed occurrences",
          "[d3d12][replay][repeated-list]")
{
  // In the query fixture, target82's WithoutDraw ends at81. The first occurrence must retain
  // its trailing EndQuery at local17; the second occurrence is truncated at local13.
  CHECK(IsCompletedReplayOccurrence(44, 19, 68, 81));
  CHECK_FALSE(IsCompletedReplayOccurrence(68, 19, 68, 81));
  CHECK_FALSE(IsCompletedReplayOccurrence(44, 19, 44, 57));
  CHECK_FALSE(IsCompletedReplayOccurrence(92, 19, 68, 81));
  CHECK_FALSE(IsCompletedReplayOccurrence(44, 0, 68, 81));
  CHECK_FALSE(IsCompletedReplayOccurrence(~0U - 1, 19, ~0U, ~0U));
}

TEST_CASE("D3D12 full companions reject interleaving and state outside their recording context",
          "[d3d12][replay][repeated-list]")
{
  BakedCmdListInfo info;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  info.preparedReplaySafe = true;
  info.preparedReplayBegin = 10;
  info.preparedReplayEnd = 50;
  info.checkpointChunks = {{baked, 10, 20, 0, D3D12Chunk::List_Reset},
                           {baked, 20, 30, 1, D3D12Chunk::List_ResolveQueryData},
                           {baked, 30, 40, 2, D3D12Chunk::List_CopyBufferRegion},
                           {baked, 40, 50, 0, D3D12Chunk::List_Close}};
  CHECK(IsFullReplayCompanionSafe(info));
  info.checkpointChunks[2].offset++;
  CHECK_FALSE(IsFullReplayCompanionSafe(info));
  info.checkpointChunks[2].offset--;
  for(D3D12Chunk chunk : {D3D12Chunk::List_ExecuteBundle, D3D12Chunk::List_ExecuteIndirect,
                         D3D12Chunk::List_BeginRenderPass, D3D12Chunk::List_EndRenderPass,
                         D3D12Chunk::SetCommandAnnotation, D3D12Chunk::Device_CreateRenderTargetView})
  {
    info.checkpointChunks[2].type = chunk;
    CHECK_FALSE(IsFullReplayCompanionSafe(info));
  }
  info.checkpointChunks[2].type = D3D12Chunk::List_CopyBufferRegion;
  info.checkpointBarriers.push_back({});
  CHECK_FALSE(IsFullReplayCompanionSafe(info));
  info.checkpointBarriers.clear();
  info.type = D3D12_COMMAND_LIST_TYPE_BUNDLE;
  CHECK_FALSE(IsFullReplayCompanionSafe(info));
  info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  info.preparedReplaySafe = false;
  CHECK_FALSE(IsFullReplayCompanionSafe(info));
}

TEST_CASE("D3D12 companion state restoration preserves partial outputs and loading ownership",
          "[d3d12][replay][repeated-list]")
{
  BakedCmdListInfo info;
  info.action = new D3D12ActionTreeNode();
  D3D12ActionTreeNode *const ownedAction = info.action;
  info.markerCount = 2;
  info.curEventID = 13;
  info.state.pipe = ResourceIDGen::GetNewUniqueID();
  const ResourceId partialPipeline = info.state.pipe;
  info.state.views.push_back({1.0f, 2.0f, 3.0f, 4.0f, 0.0f, 1.0f});
  info.state.graphics.sigelems.resize(1);
  info.state.graphics.sigelems[0].SetConstant(0, 7);
  info.m_OutstandingQueries.push_back({NULL, D3D12_QUERY_TYPE_BINARY_OCCLUSION, 1});
  info.barriers.barriers.push_back({});
  info.bufferPatchConsumers.insert(partialPipeline);
  ReplayCompanionMutableState saved(info);
  info.state.pipe = ResourceId();
  info.state.views[0].Width = 999.0f;
  info.state.graphics.sigelems[0].SetConstant(0, 99);
  info.markerCount = 0;
  info.curEventID = 0;
  info.m_OutstandingQueries.clear();
  info.barriers.clear();
  saved.Restore(info);
  CHECK(info.state.pipe == partialPipeline);
  CHECK(info.state.views[0].Width == 3.0f);
  CHECK(info.state.graphics.sigelems[0].constants[0] == 7);
  CHECK(info.markerCount == 2);
  CHECK(info.curEventID == 13);
  REQUIRE(info.m_OutstandingQueries.size() == 1);
  CHECK(info.m_OutstandingQueries[0].Index == 1);
  CHECK(info.barriers.barriers.size() == 1);
  CHECK(info.action == ownedAction);
  CHECK(info.bufferPatchConsumers.count(partialPipeline) == 1);
}

TEST_CASE("D3D12 checkpoint indirect signatures exclude GPU address patching",
          "[d3d12][replay][checkpoint]")
{
  D3D12CommandSignature signature;
  CHECK_FALSE(IsCheckpointDirectIndirectSignature(signature));
  D3D12_INDIRECT_ARGUMENT_DESC argument = {};
  for(D3D12_INDIRECT_ARGUMENT_TYPE type :
      {D3D12_INDIRECT_ARGUMENT_TYPE_DRAW, D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED,
       D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH, D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_MESH,
       D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT})
  {
    argument.Type = type;
    signature.arguments = {argument};
    CHECK(IsCheckpointDirectIndirectSignature(signature));
  }
  for(D3D12_INDIRECT_ARGUMENT_TYPE type :
      {D3D12_INDIRECT_ARGUMENT_TYPE_VERTEX_BUFFER_VIEW, D3D12_INDIRECT_ARGUMENT_TYPE_INDEX_BUFFER_VIEW,
       D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT_BUFFER_VIEW,
       D3D12_INDIRECT_ARGUMENT_TYPE_SHADER_RESOURCE_VIEW,
       D3D12_INDIRECT_ARGUMENT_TYPE_UNORDERED_ACCESS_VIEW, D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_RAYS,
       D3D12_INDIRECT_ARGUMENT_TYPE(0xffff)})
  {
    argument.Type = type;
    signature.arguments.push_back(argument);
    CHECK_FALSE(IsCheckpointDirectIndirectSignature(signature));
    signature.arguments.pop_back();
  }
  signature.raytraced = true;
  CHECK_FALSE(IsCheckpointDirectIndirectSignature(signature));
  CHECK_FALSE(IsPreparedReplaySafeChunk(D3D12Chunk::List_ExecuteIndirect));
}

TEST_CASE("D3D12 checkpoint metadata follows final indirect event counts",
          "[d3d12][replay][checkpoint]")
{
  BakedCmdListInfo info;
  D3D12ActionTreeNode action;
  info.action = &action;
  info.eventCount = 12;
  info.actionCount = 8;
  // Finalisation has removed three unused child actions. The remaining pop marker and later
  // clear still carry their old EIDs until ShiftForRemoved updates them.
  for(uint32_t event : {1U, 2U, 6U, 10U})
  {
    D3D12ActionTreeNode node;
    node.action.eventId = event;
    node.action.actionId = event;
    action.children.push_back(node);
  }
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  info.checkpointChunks.push_back({baked, 1, 2, 0, D3D12Chunk::List_Reset});
  info.checkpointChunks.push_back({baked, 3, 4, 1, D3D12Chunk::List_SetPipelineState});
  info.checkpointChunks.push_back({baked, 5, 6, 7, D3D12Chunk::List_ExecuteIndirect});
  info.checkpointChunks.push_back({baked, 7, 8, 11, D3D12Chunk::List_ClearRenderTargetView});
  info.checkpointChunks.push_back({baked, 9, 10, 0, D3D12Chunk::List_Close});
  info.checkpointQueries.push_back({baked, 0, 1, 1, false});
  info.checkpointQueries.push_back({baked, 0, 1, 7, true});
  BakedCmdListInfo::CheckpointBarrierOp barrier;
  barrier.relativeEvent = 8;
  info.checkpointBarriers.push_back(barrier);
  info.ShiftForRemoved(3, 3, 2);
  CHECK(info.eventCount == 9);
  CHECK(action.children[2].action.eventId == 3);
  CHECK(info.checkpointChunks[0].eventAfter == 0);
  CHECK(info.checkpointChunks[1].eventAfter == 1);
  CHECK(info.checkpointChunks[2].eventAfter == 4);
  CHECK(info.checkpointChunks[3].eventAfter == 8);
  CHECK(info.checkpointChunks[4].eventAfter == 0);
  CHECK(info.checkpointQueries[0].relativeEvent == 1);
  CHECK(info.checkpointQueries[1].relativeEvent == 4);
  CHECK(info.checkpointBarriers[0].relativeEvent == 5);
  info.action = NULL;
}

TEST_CASE("D3D12 prepared replay queries require matching command-list-local scopes",
          "[d3d12][replay][prepared-query]")
{
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  const ResourceId otherHeap = ResourceIDGen::GetNewUniqueID();
  D3D12PreparedQueryTracker firstList, secondList;

  CHECK(firstList.Complete());
  REQUIRE(firstList.Begin(heap, D3D12_QUERY_TYPE_OCCLUSION, 7));
  CHECK_FALSE(firstList.Complete());
  CHECK_FALSE(firstList.Begin(heap, D3D12_QUERY_TYPE_BINARY_OCCLUSION, 7));
  CHECK_FALSE(firstList.End(otherHeap, D3D12_QUERY_TYPE_OCCLUSION, 7));
  CHECK_FALSE(firstList.End(heap, D3D12_QUERY_TYPE_BINARY_OCCLUSION, 7));
  CHECK_FALSE(firstList.End(heap, D3D12_QUERY_TYPE_OCCLUSION, 8));
  CHECK_FALSE(secondList.End(heap, D3D12_QUERY_TYPE_OCCLUSION, 7));
  CHECK_FALSE(firstList.Complete());
  CHECK(firstList.End(heap, D3D12_QUERY_TYPE_OCCLUSION, 7));
  CHECK(firstList.Complete());
  CHECK_FALSE(firstList.End(heap, D3D12_QUERY_TYPE_OCCLUSION, 7));
}

TEST_CASE("D3D12 prepared timestamps have no begin scope and do not consume paired queries",
          "[d3d12][replay][prepared-query]")
{
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  D3D12PreparedQueryTracker tracker;

  CHECK_FALSE(tracker.Begin(heap, D3D12_QUERY_TYPE_TIMESTAMP, 1));
  CHECK(tracker.End(heap, D3D12_QUERY_TYPE_TIMESTAMP, 1));
  CHECK(tracker.Complete());
  REQUIRE(tracker.Begin(heap, D3D12_QUERY_TYPE_PIPELINE_STATISTICS, 2));
  CHECK(tracker.End(heap, D3D12_QUERY_TYPE_TIMESTAMP, 1));
  CHECK_FALSE(tracker.Complete());
  CHECK_FALSE(tracker.End(ResourceId(), D3D12_QUERY_TYPE_TIMESTAMP, 1));
  CHECK(tracker.End(heap, D3D12_QUERY_TYPE_PIPELINE_STATISTICS, 2));
  CHECK(tracker.Complete());
}

TEST_CASE("D3D12 checkpoints require closed root-stream boundaries",
          "[d3d12][replay][checkpoint]")
{
  D3D12CommandData command;
  const ResourceId prefix = ResourceIDGen::GetNewUniqueID();
  const ResourceId suffix = ResourceIDGen::GetNewUniqueID();
  auto &first = command.m_BakedCmdListInfo[prefix];
  first.checkpointBegin = 10;
  first.checkpointEnd = 100;
  first.eventCount = 20;
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[prefix].push_back(2);
  auto &second = command.m_BakedCmdListInfo[suffix];
  second.checkpointBegin = 120;
  second.checkpointEnd = 200;
  second.eventCount = 30;
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[suffix].push_back(25);
  D3D12CommandData::CheckpointBoundary boundary = {23, 24, 8, 110};
  rdcstr reason;
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  const ResourceId queryHeap = ResourceIDGen::GetNewUniqueID();
  second.checkpointQueries.push_back({queryHeap, 4, 1, 1, false});
  second.checkpointQueries.push_back({queryHeap, 4, 1, 2, true});
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  second.checkpointQueries.erase(0);
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == 27);
  second.checkpointQueries.clear();

  BakedCmdListInfo::CheckpointBarrierOp beginSplit;
  beginSplit.before = ResourceIDGen::GetNewUniqueID();
  beginSplit.relativeEvent = 1;
  beginSplit.subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
  beginSplit.flags = D3D12_RESOURCE_BARRIER_FLAG_BEGIN_ONLY;
  beginSplit.stateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
  beginSplit.stateAfter = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
  first.checkpointBarriers.push_back(beginSplit);
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  auto endSplit = beginSplit;
  endSplit.relativeEvent = 2;
  endSplit.flags = D3D12_RESOURCE_BARRIER_FLAG_END_ONLY;
  first.checkpointBarriers.push_back(endSplit);
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  first.checkpointBarriers.back().stateAfter = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  first.checkpointBarriers.clear();
  first.checkpointBarriers.push_back(endSplit);
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  first.checkpointBarriers.clear();
  first.checkpointBarriers.push_back(beginSplit);
  auto individualSplit = beginSplit;
  individualSplit.subresource = 0;
  first.checkpointBarriers.push_back(individualSplit);
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  first.checkpointBarriers.clear();
  first.checkpointBarriers.push_back(beginSplit);
  second.checkpointBarriers.push_back(endSplit);
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  D3D12CommandData::CheckpointBoundary afterBothSubmissions = {56, 57, 10, 210};
  CHECK(command.IsCheckpointBoundarySafe(afterBothSubmissions, reason));
  first.checkpointBarriers.clear();
  second.checkpointBarriers.clear();

  // An outstanding recording needs its indexed Reset and pure recording prefix reconstructed.
  second.checkpointBegin = 90;
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  second.checkpointChunks.push_back({suffix, 90, 95, 0, D3D12Chunk::List_Reset});
  second.checkpointChunks.push_back({suffix, 96, 99, 1, D3D12Chunk::List_DrawInstanced});
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> continuation;
  CHECK(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation, reason));
  REQUIRE(continuation.size() == 2);
  CHECK(continuation[0].type == D3D12Chunk::List_Reset);
  CHECK(continuation[1].eventAfter == 1);
  second.checkpointChunks.back().type = D3D12Chunk::List_ExecuteIndirect;
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  second.checkpointChunks.back().directIndirect = true;
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  second.checkpointChunks.back().directIndirect = false;
  second.checkpointChunks.back().type = D3D12Chunk::List_DiscardResource;
  CHECK_FALSE(IsPreparedReplaySafeChunk(D3D12Chunk::List_DiscardResource));
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation, reason));
  REQUIRE(continuation.size() == 2);
  CHECK(continuation.back().type == D3D12Chunk::List_DiscardResource);
  for(D3D12Chunk chunk : {D3D12Chunk::List_ClearUnorderedAccessViewUint,
                          D3D12Chunk::List_ClearUnorderedAccessViewFloat,
                          D3D12Chunk::List_AtomicCopyBufferUINT,
                          D3D12Chunk::List_AtomicCopyBufferUINT64, D3D12Chunk::List_CopyTiles})
  {
    second.checkpointChunks.back().type = chunk;
    CHECK_FALSE(IsPreparedReplaySafeChunk(chunk));
    CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  }
  // Collect all rejected types in one pass, deduplicating source offsets across candidate checks.
  second.checkpointChunks.back().type = D3D12Chunk::List_ExecuteIndirect;
  second.checkpointChunks.push_back({suffix, 100, 103, 2, D3D12Chunk::List_ExecuteBundle});
  D3D12CommandData::CheckpointUnsupportedChunks unsupported;
  CHECK_FALSE(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation,
                                                 reason, &unsupported));
  CHECK(continuation.empty());
  REQUIRE(unsupported.size() == 2);
  CHECK(unsupported[D3D12Chunk::List_ExecuteIndirect].size() == 1);
  CHECK(unsupported[D3D12Chunk::List_ExecuteBundle].size() == 1);
  CHECK(reason.contains(ToStr(D3D12Chunk::List_ExecuteIndirect)));
  CHECK(reason.contains(ToStr(D3D12Chunk::List_ExecuteBundle)));
  CHECK_FALSE(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation,
                                                 reason, &unsupported));
  CHECK(unsupported[D3D12Chunk::List_ExecuteIndirect].size() == 1);
  CHECK(unsupported[D3D12Chunk::List_ExecuteBundle].size() == 1);
  second.checkpointChunks.pop_back();
  second.checkpointChunks.back().type = D3D12Chunk::List_DrawInstanced;
  // A closed but not-yet-submitted list must be reconstructed too.
  second.checkpointEnd = 108;
  second.checkpointChunks.push_back({suffix, 105, 108, 0, D3D12Chunk::List_Close});
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation, reason));
  REQUIRE(continuation.size() == 3);
  CHECK(continuation.back().type == D3D12Chunk::List_Close);
  second.checkpointEnd = 200;
  second.checkpointChunks.pop_back();
  // Never-submitted open lists still need their wrapper/CPU state for later chunks and Close.
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[suffix].clear();
  // Without any execution, even an address-bearing indirect only advances the event counter;
  // the active replay handler cannot enter GPU patching or allocate indirect readback state.
  second.checkpointChunks.back().type = D3D12Chunk::List_ExecuteIndirect;
  CHECK(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation, reason));
  CHECK(continuation.size() == 2);
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[suffix].push_back(25);
  second.checkpointChunks.clear();
  second.checkpointBegin = 120;
  // A second submission of a previously recorded list also crosses the boundary.
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[prefix].push_back(60);
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[prefix].pop_back();
  first.checkpointEnd = 0;
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  first.checkpointEnd = 100;
  command.m_CheckpointUnsupported = "split barrier";
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
}

TEST_CASE("D3D12 checkpoints retain the interval before the first dependent native query resolve",
          "[d3d12][replay][checkpoint-query]")
{
  D3D12CommandData command;
  const ResourceId prefix = ResourceIDGen::GetNewUniqueID();
  const ResourceId suffix = ResourceIDGen::GetNewUniqueID();
  const ResourceId heapA = ResourceIDGen::GetNewUniqueID();
  const ResourceId heapB = ResourceIDGen::GetNewUniqueID();
  auto &first = command.m_BakedCmdListInfo[prefix];
  first.checkpointBegin = 10;
  first.checkpointEnd = 100;
  first.eventCount = 20;
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[prefix].push_back(2);
  auto &second = command.m_BakedCmdListInfo[suffix];
  second.checkpointBegin = 120;
  second.checkpointEnd = 200;
  second.eventCount = 30;
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[suffix].push_back(25);
  D3D12CommandData::CheckpointBoundary boundary = {23, 24, 8, 110};
  rdcstr reason;
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == ~0U);

  // Array/map insertion order must not select a later resolve. A prefix EndQuery is not saved
  // with resource bytes; only a new suffix EndQuery can make its native result self-contained.
  first.checkpointQueries.push_back({heapA, 4, 1, 1, false, D3D12_QUERY_TYPE_TIMESTAMP});
  second.checkpointQueries.push_back({heapA, 4, 1, 9, true, D3D12_QUERY_TYPE_TIMESTAMP});
  second.checkpointQueries.push_back({heapB, 7, 1, 3, true, D3D12_QUERY_TYPE_PIPELINE_STATISTICS});
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == 28);
  CHECK(boundary.firstNativeResolveType == D3D12_QUERY_TYPE_PIPELINE_STATISTICS);
  CHECK(IsCheckpointQueryTargetSafe(27, boundary.firstNativeResolveEID));
  CHECK_FALSE(IsCheckpointQueryTargetSafe(28, boundary.firstNativeResolveEID));
  CHECK_FALSE(IsCheckpointQueryTargetSafe(29, boundary.firstNativeResolveEID));
  // WithoutDraw may internally end at 27, but a requested EID 28 includes its subsequent
  // OnlyDraw resolve and must use the canonical prefix instead of this checkpoint.
  const uint32_t requestedEvent = 28;
  CHECK(IsCheckpointQueryTargetSafe(requestedEvent - 1, boundary.firstNativeResolveEID));
  CHECK_FALSE(IsCheckpointQueryTargetSafe(requestedEvent, boundary.firstNativeResolveEID));

  second.checkpointQueries.push_back({heapB, 7, 1, 2, false, D3D12_QUERY_TYPE_PIPELINE_STATISTICS});
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == 34);
  CHECK(boundary.firstNativeResolveType == D3D12_QUERY_TYPE_TIMESTAMP);
  second.checkpointQueries.push_back({heapA, 4, 1, 10, false, D3D12_QUERY_TYPE_TIMESTAMP});
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == 34);    // an EndQuery after resolve is too late
  second.checkpointQueries.back().relativeEvent = 8;
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == ~0U);
  CHECK(boundary.firstNativeResolveType == D3D12_QUERY_TYPE_OCCLUSION);
  CHECK(IsCheckpointQueryTargetSafe(~0U, boundary.firstNativeResolveEID));

  second.checkpointQueries[0].count = 2;
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == 34);    // neighbouring query 5 still depends on prefix
  second.checkpointQueries.push_back({heapA, 5, 1, 8, false, D3D12_QUERY_TYPE_TIMESTAMP});
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == ~0U);
  // Once both submissions are in the prefix, no future resolve can bound this later checkpoint.
  D3D12CommandData::CheckpointBoundary afterBoth = {56, 57, 10, 210};
  REQUIRE(command.IsCheckpointBoundarySafe(afterBoth, reason));
  CHECK(afterBoth.firstNativeResolveEID == ~0U);
}

TEST_CASE("D3D12 checkpoint continuation decodes only recordings submitted by the target",
          "[d3d12][replay][checkpoint-continuation]")
{
  D3D12CommandData command;
  const ResourceId first = ResourceIDGen::GetNewUniqueID();
  const ResourceId later = ResourceIDGen::GetNewUniqueID();
  const ResourceId repeated = ResourceIDGen::GetNewUniqueID();
  const ResourceId never = ResourceIDGen::GetNewUniqueID();
  const auto addList = [&](ResourceId id, uint64_t begin, bool closed) {
    BakedCmdListInfo &info = command.m_BakedCmdListInfo[id];
    info.checkpointBegin = begin;
    info.checkpointEnd = closed ? begin + 63 : 200;
    info.eventCount = 20;
    info.checkpointChunks.push_back({id, begin, begin + 1, 0, D3D12Chunk::List_Reset});
    info.checkpointChunks.push_back(
        {id, begin + 10, begin + 11, 1, D3D12Chunk::List_SetPipelineState});
    info.checkpointChunks.push_back(
        {id, begin + 20, begin + 21, 7, D3D12Chunk::List_ExecuteIndirect, true});
    if(closed)
      info.checkpointChunks.push_back(
          {id, begin + 60, begin + 63, 0, D3D12Chunk::List_Close});
  };
  // Interleaved source ranges contain two already closed but unsubmitted lists and two open
  // recordings. A repeated list is needed if any of its submissions falls in the target range.
  addList(first, 10, true);
  addList(later, 12, false);
  addList(repeated, 14, true);
  addList(never, 16, false);
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[first] = {100};
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[later] = {300};
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[repeated] = {80, 400};
  const uint64_t offset = 100;
  const uint32_t nextEvent = 50;
  rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> plan;
  rdcstr reason;
  const auto checkPlan = [&](uint32_t target, bool firstFull, bool laterFull, bool repeatedFull,
                             bool neverFull) {
    REQUIRE(command.GetCheckpointContinuation(offset, nextEvent, plan, reason, NULL, target));
    REQUIRE(plan.size() == 14);
    uint64_t previous = 0;
    for(const auto &chunk : plan)
    {
      CHECK(chunk.offset > previous);
      previous = chunk.offset;
      const bool identity =
          chunk.type == D3D12Chunk::List_Reset || chunk.type == D3D12Chunk::List_Close;
      const bool full = chunk.baked == first      ? firstFull
                        : chunk.baked == later    ? laterFull
                        : chunk.baked == repeated ? repeatedFull
                                                  : neverFull;
      CHECK(chunk.metadataOnly == (!identity && !full));
      if(chunk.type == D3D12Chunk::List_ExecuteIndirect)
        CHECK(chunk.eventAfter == 7);
    }
  };
  checkPlan(79, false, false, false, false);   // before the first suffix submission
  checkPlan(80, false, false, true, false);    // exact start, still a partial native recording
  checkPlan(105, true, false, true, false);    // inside the first list
  checkPlan(121, true, false, true, false);    // after the first list
  checkPlan(305, true, true, true, false);     // inside the later open list
  checkPlan(405, true, true, true, false);     // second submission of the same baked list
  checkPlan(~0U, true, true, true, true);      // conservative A/B and boundary safety validation
  // Planning must not modify the loading index or the native query/barrier continuation guards.
  for(const auto &entry : command.m_BakedCmdListInfo)
    for(const auto &chunk : entry.second.checkpointChunks)
      CHECK_FALSE(chunk.metadataOnly);
  command.m_Partial[D3D12CommandData::Secondary].cmdListExecs[never] = {60};
  checkPlan(79, false, false, false, true);    // match Reset's secondary-list safety decision
  command.m_Partial[D3D12CommandData::Secondary].cmdListExecs[never].clear();
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[repeated].push_back(40);
  CHECK_FALSE(command.GetCheckpointContinuation(offset, nextEvent, plan, reason, NULL, 79));
  CHECK(reason.contains("across the checkpoint"));
}

TEST_CASE("D3D12 checkpoint metadata continuation retains identity and final indirect cursors",
          "[d3d12][replay][checkpoint-continuation]")
{
  D3D12CommandData command;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  BakedCmdListInfo::CheckpointRecordingChunk chunk = {
      baked, 10, 20, 7, D3D12Chunk::List_ExecuteIndirect, true, true};
  command.m_BakedCmdListInfo[baked].curEventID = 1;
  command.m_RootEventID = 100;
  REQUIRE(command.ApplyCheckpointContinuationMetadata(chunk));
  CHECK(command.m_BakedCmdListInfo[baked].curEventID == 7);
  CHECK(command.m_LastCmdListID == baked);
  CHECK(command.m_CurChunkOffset == 10);
  CHECK(command.m_LastChunk == D3D12Chunk::List_ExecuteIndirect);
  CHECK(command.m_RootEventID == 100);
  for(D3D12Chunk identity : {D3D12Chunk::List_Reset, D3D12Chunk::List_Close})
  {
    chunk.type = identity;
    CHECK_FALSE(command.ApplyCheckpointContinuationMetadata(chunk));
  }
  chunk.type = D3D12Chunk::List_ExecuteIndirect;
  chunk.eventAfter = 8;
  command.m_RerecordCmds[baked] = NULL;    // existence, not pointer value, drives the real handler
  CHECK_FALSE(command.ApplyCheckpointContinuationMetadata(chunk));
  CHECK(command.m_BakedCmdListInfo[baked].curEventID == 7);
  command.m_RerecordCmds.clear();
  command.m_Partial[D3D12CommandData::Primary].partialParent = baked;
  CHECK_FALSE(command.ApplyCheckpointContinuationMetadata(chunk));
  command.m_Partial[D3D12CommandData::Primary].partialParent = ResourceId();
  chunk.metadataOnly = false;
  CHECK_FALSE(command.ApplyCheckpointContinuationMetadata(chunk));
}

TEST_CASE("D3D12 shared buffer consumers include native roots and conservatively unknown bindings",
          "[d3d12][replay][checkpoint-buffer-patch]")
{
  D3D12CommandData command;
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const ResourceId other = ResourceIDGen::GetNewUniqueID();
  const ResourceId directList = ResourceIDGen::GetNewUniqueID();
  const ResourceId tableList = ResourceIDGen::GetNewUniqueID();
  auto &direct = command.m_BakedCmdListInfo[directList];
  direct.curEventID = 1;
  command.m_LastCmdListID = directList;
  command.m_CurChunkOffset = 10;
  direct.state.graphics.sigelems.push_back(
      D3D12RenderState::SignatureElement(eRootCBV, buffer, 256));
  command.ObserveCheckpointChunk(D3D12Chunk::List_SetGraphicsRootConstantBufferView, 20);
  CHECK(direct.bufferPatchConsumers.count(buffer) == 1);
  CHECK_FALSE(direct.bufferPatchUnknownConsumers);
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == ~0U);    // recording is not execution
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[directList] = {100, 50};
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 50);
  CHECK(command.GetBufferPatchFirstConsumer(other) == ~0U);
  direct.state.compute.sigelems.push_back(
      D3D12RenderState::SignatureElement(eRootSRV, other, 0));
  command.ObserveCheckpointChunk(D3D12Chunk::List_SetComputeRootShaderResourceView, 30);
  CHECK(command.GetBufferPatchFirstConsumer(other) == 50);

  auto &table = command.m_BakedCmdListInfo[tableList];
  table.curEventID = 1;
  command.m_LastCmdListID = tableList;
  command.ObserveCheckpointChunk(D3D12Chunk::List_SetGraphicsRootDescriptorTable, 40);
  CHECK(table.bufferPatchUnknownConsumers);
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 50);    // unsubmitted table does not bind
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[tableList] = {25};
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 25);
  CHECK(command.GetBufferPatchFirstConsumer(other) == 25);
  command.m_Partial[D3D12CommandData::Secondary].cmdListExecs[directList] = {10};
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 10);
  command.m_Partial[D3D12CommandData::Secondary].cmdListExecs[directList].clear();
  table.bufferPatchUnknownConsumers = false;
  command.ObserveCheckpointChunk(D3D12Chunk::List_ExecuteIndirect, 50);
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 25);
  // An unresolved signature may expose directly indexed descriptor storage. It is not evidence
  // that only the explicit root buffers can be consumed.
  table.bufferPatchUnknownConsumers = false;
  table.state.graphics.rootsig = ResourceIDGen::GetNewUniqueID();
  command.ObserveCheckpointChunk(D3D12Chunk::List_SetGraphicsRootSignature, 60);
  CHECK(table.bufferPatchUnknownConsumers);
  command.m_BakedCmdListInfo.erase(tableList);
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 0);    // missing execution metadata fails closed
}

#endif    // ENABLED(ENABLE_UNIT_TESTS)

template <typename Key>
static void RecordReplayWriteStart(std::set<Key> &writes, std::map<Key, uint32_t> &firstEvents,
                                   const Key &key, uint32_t relativeEvent)
{
  writes.insert(key);
  auto inserted = firstEvents.insert({key, relativeEvent});
  inserted.first->second = RDCMIN(inserted.first->second, relativeEvent);
}

static void RecordUnknownReplayWrite(BakedCmdListInfo &info, uint32_t relativeEvent,
                                     D3D12Chunk chunk)
{
  info.replayUnknownWrites = true;
  if(relativeEvent < info.replayFirstUnknownWriteEvent)
  {
    info.replayFirstUnknownWriteEvent = relativeEvent;
    info.replayUnknownWriteChunk = chunk;
  }
}

static bool AddReplayUAVRange(BakedCmdListInfo &info, ResourceId heap, uint32_t heapCount,
                             uint64_t base, uint64_t offset, uint32_t requestedCount,
                             uint32_t relativeEvent = 0)
{
  if(base > heapCount || offset > heapCount - base)
    return false;
  const uint64_t first = base + offset;
  const uint64_t remaining = heapCount - first;
  const uint64_t count = requestedCount == UINT_MAX ? remaining : requestedCount;
  if(count > remaining)
    return false;
  if(count)
    RecordReplayWriteStart(info.replayUAVRanges, info.replayUAVFirstEvents,
                           BakedCmdListInfo::ReplayUAVRange{heap, uint32_t(first), uint32_t(count)},
                           relativeEvent);
  return true;
}

static bool HasKnownReplayIndirectWrites(const D3D12CommandSignature &signature)
{
  if(signature.raytraced || signature.arguments.empty())
    return false;
  for(const D3D12_INDIRECT_ARGUMENT_DESC &argument : signature.arguments)
  {
    switch(argument.Type)
    {
      case D3D12_INDIRECT_ARGUMENT_TYPE_DRAW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_MESH:
      case D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT:
      case D3D12_INDIRECT_ARGUMENT_TYPE_INCREMENTING_CONSTANT:
      case D3D12_INDIRECT_ARGUMENT_TYPE_VERTEX_BUFFER_VIEW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_INDEX_BUFFER_VIEW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT_BUFFER_VIEW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_SHADER_RESOURCE_VIEW: break;
      // Generated read addresses can change branches but cannot introduce new write bindings.
      // Generated root UAV addresses, rays and future argument kinds require a complete reset.
      default: return false;
    }
  }
  return true;
}

void D3D12CommandData::ObserveReplayWriteBindings(BakedCmdListInfo &info,
                                                const D3D12RenderState &state,
                                                uint32_t relativeEvent)
{
  for(ResourceId target : state.GetRTVIDs())
    if(target != ResourceId())
      RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, target, relativeEvent);
  if(state.GetDSVID() != ResourceId())
    RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, state.GetDSVID(),
                           relativeEvent);
  for(const auto &output : state.streamouts)
  {
    if(output.buf != ResourceId())
      RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, output.buf, relativeEvent);
    if(output.countbuf != ResourceId())
      RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, output.countbuf,
                             relativeEvent);
  }

  for(const auto *root : {&state.graphics, &state.compute})
  {
    if(root->rootsig == ResourceId())
      continue;
    WrappedID3D12RootSignature *signature =
        m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12RootSignature>(root->rootsig);
    if(!signature)
    {
      info.replayUnknownWrites = true;
      continue;
    }
    if(signature->sig.Flags & D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED)
    {
      for(ResourceId heapID : state.heaps)
      {
        WrappedID3D12DescriptorHeap *heap =
            m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(heapID);
        if(!heap)
          info.replayUnknownWrites = true;
        else if(heap->GetDesc().Type == D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV)
          RecordReplayWriteStart(info.replayUAVRanges, info.replayUAVFirstEvents,
                                 BakedCmdListInfo::ReplayUAVRange{heapID, 0, heap->GetNumDescriptors()},
                                 relativeEvent);
      }
    }
    for(size_t i = 0; i < signature->sig.Parameters.size() && i < root->sigelems.size(); i++)
    {
      const auto &parameter = signature->sig.Parameters[i];
      const auto &element = root->sigelems[i];
      if(parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_UAV && element.type == eRootUAV)
      {
        if(element.id != ResourceId())
          RecordReplayWriteStart(info.replayRootUAVWrites, info.replayRootUAVFirstEvents, element.id,
                                 relativeEvent);
      }
      else if(parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE &&
              element.type == eRootTable)
      {
        WrappedID3D12DescriptorHeap *heap =
            m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(element.id);
        if(!heap)
        {
          info.replayUnknownWrites = true;
          continue;
        }
        uint64_t previousEnd = 0;
        for(const D3D12_DESCRIPTOR_RANGE1 &range : parameter.ranges)
        {
          uint64_t offset = range.OffsetInDescriptorsFromTableStart;
          if(offset == D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND)
            offset = previousEnd;
          const uint64_t remaining = element.offset <= heap->GetNumDescriptors() &&
                                            offset <= heap->GetNumDescriptors() - element.offset
                                         ? heap->GetNumDescriptors() - element.offset - offset : 0;
          const uint64_t count = range.NumDescriptors == UINT_MAX ? remaining : range.NumDescriptors;
          previousEnd = offset + count;
          if(range.RangeType != D3D12_DESCRIPTOR_RANGE_TYPE_UAV)
            continue;
          if(!AddReplayUAVRange(info, element.id, heap->GetNumDescriptors(), element.offset,
                                offset, range.NumDescriptors, relativeEvent))
            info.replayUnknownWrites = true;
        }
      }
    }
  }
}

static void GatherReplayActionWrites(const D3D12ActionTreeNode &node, BakedCmdListInfo &info)
{
  for(const auto &use : node.resourceUsage)
  {
    const ResourceUsage usage = use.second.usage;
    if((usage >= ResourceUsage::VS_RWResource && usage <= ResourceUsage::All_RWResource) ||
       usage == ResourceUsage::CopyDst || usage == ResourceUsage::Copy ||
       usage == ResourceUsage::ResolveDst || usage == ResourceUsage::ResolveSrc ||
       usage == ResourceUsage::Resolve ||
       usage == ResourceUsage::Clear || usage == ResourceUsage::Discard ||
       usage == ResourceUsage::ColorTarget || usage == ResourceUsage::DepthStencilTarget ||
       usage == ResourceUsage::StreamOut)
      RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, use.first,
                             use.second.eventId);
  }
  for(const D3D12ActionTreeNode &child : node.children)
    GatherReplayActionWrites(child, info);
}

void D3D12CommandData::BuildReplayWriteTracking()
{
  for(auto &entry : m_BakedCmdListInfo)
  {
    BakedCmdListInfo &info = entry.second;
    if(info.action && !info.checkpointChunks.empty() && info.checkpointEnd)
    {
      // Explicit handlers append Copy/Clear/Resolve usages after AddAction. Consume their final
      // tree once loading (including ExecuteIndirect event finalisation) has completed.
      GatherReplayActionWrites(*info.action, info);
      for(const auto &barrier : info.checkpointBarriers)
      {
        if(barrier.type != D3D12_RESOURCE_BARRIER_TYPE_ALIASING)
          continue;
        if(barrier.before == ResourceId() && barrier.after == ResourceId())
        {
          RecordUnknownReplayWrite(info, barrier.relativeEvent, D3D12Chunk::List_ResourceBarrier);
        }
        else
        {
          if(barrier.before != ResourceId())
            RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, barrier.before,
                                   barrier.relativeEvent);
          if(barrier.after != ResourceId())
            RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, barrier.after,
                                   barrier.relativeEvent);
        }
      }
      info.replayWritesKnown = true;
      // The final tree is still command-list-local: insertion into the frame tree rebases a copy.
      // Missing legacy/index metadata cannot turn a previously unknown list into a known prefix.
      info.replayWriteEventsKnown =
          info.replayWriteFirstEvents.size() == info.replayWrites.size() &&
          info.replayRootUAVFirstEvents.size() == info.replayRootUAVWrites.size() &&
          info.replayUAVFirstEvents.size() == info.replayUAVRanges.size() &&
          (!info.replayUnknownWrites || info.replayFirstUnknownWriteEvent != ~0U);
    }
  }
}

struct ReplayWriteCutoff
{
  uint32_t last = ~0U;
  bool indexed = false;
  bool unknown = false;
  const char *fallback = "none";
};

static ReplayWriteCutoff SelectReplayWriteCutoff(const BakedCmdListInfo &info, bool enabled,
                                                bool knownOccurrence, uint32_t baseEvent,
                                                uint32_t lastEvent)
{
  ReplayWriteCutoff cutoff;
  cutoff.unknown = info.replayUnknownWrites;
  if(!enabled)
    cutoff.fallback = "disabled";
  else if(!info.replayWriteEventsKnown)
    cutoff.fallback = "missingWriteIndex";
  else if(!knownOccurrence || !info.eventCount || baseEvent == ~0U || baseEvent > lastEvent)
    cutoff.fallback = "unknownOccurrence";
  else
  {
    // Use the base of this actual submission, never the base of another partial occurrence of
    // the same baked list. Subtraction is checked above and cannot overflow like base + length.
    cutoff.last = RDCMIN(lastEvent - baseEvent, info.eventCount - 1);
    cutoff.indexed = true;
    cutoff.unknown = info.replayUnknownWrites && info.replayFirstUnknownWriteEvent <= cutoff.last;
  }
  return cutoff;
}

template <typename Key>
static bool IsReplayWriteInPrefix(const std::map<Key, uint32_t> &firstEvents, const Key &key,
                                  const ReplayWriteCutoff &cutoff)
{
  if(!cutoff.indexed)
    return true;
  const auto first = firstEvents.find(key);
  // BuildReplayWriteTracking checks index completeness. If a later path ever adds an unindexed
  // union member, retaining it is the safe direction and keeps the old dirty upper bound.
  return first == firstEvents.end() || first->second <= cutoff.last;
}

void D3D12CommandData::MarkReplaySubmittedWrites(ResourceId baked, uint32_t baseEvent,
                                                uint32_t lastEvent)
{
  if(!m_pDevice->IsReplayDirtyTrackingValid())
    return;
  auto found = m_BakedCmdListInfo.find(baked);
  bool knownOccurrence = false;
  for(int p = 0; p < ePartialNum; p++)
  {
    const auto executions = m_Partial[p].cmdListExecs.find(baked);
    if(executions != m_Partial[p].cmdListExecs.end())
      for(uint32_t base : executions->second)
        knownOccurrence |= base == baseEvent;
  }
  ReplayWriteCutoff cutoff;
  if(found != m_BakedCmdListInfo.end())
    cutoff = SelectReplayWriteCutoff(found->second, m_ReplayWriteCutoffEnabled, knownOccurrence,
                                    baseEvent, lastEvent);
  if(found == m_BakedCmdListInfo.end() || !found->second.replayWritesKnown || cutoff.unknown)
  {
    const rdcstr reason = StringFormat::Fmt("unknownSubmittedList:%s:%s", ToStr(baked).c_str(),
        found == m_BakedCmdListInfo.end() ? "missing" :
            ToStr(found->second.replayUnknownWriteChunk).c_str());
    m_pDevice->InvalidateReplayDirtyTracking(reason.c_str());
    if(m_PreparedTimingEnabled)
    {
      const rdcstr line = StringFormat::Fmt(
          "api=D3D12DirtyWriteCutoff baked=%s base=%u requestedEnd=%u localLast=%u enabled=%u "
          "indexed=%u firstUnknown=%u proofValid=0 fallback=%s reason=%s",
          ToStr(baked).c_str(), baseEvent, lastEvent, cutoff.last,
          m_ReplayWriteCutoffEnabled ? 1U : 0U, cutoff.indexed ? 1U : 0U,
          found == m_BakedCmdListInfo.end() ? ~0U : found->second.replayFirstUnknownWriteEvent,
          cutoff.fallback, reason.c_str());
      RDCLOG("%s", line.c_str());
      AppendReplayTimingLog(line);
    }
    return;
  }
  const BakedCmdListInfo &info = found->second;
  uint32_t skippedResources = 0, skippedRootUAVs = 0, skippedRanges = 0;
  for(ResourceId id : info.replayWrites)
    if(IsReplayWriteInPrefix(info.replayWriteFirstEvents, id, cutoff))
      m_pDevice->MarkReplayResourceDirty(id);
    else
      skippedResources++;
  for(ResourceId id : info.replayRootUAVWrites)
    if(IsReplayWriteInPrefix(info.replayRootUAVFirstEvents, id, cutoff))
      m_pDevice->MarkReplayResourceDirty(id, true);
    else
      skippedRootUAVs++;
  std::set<ResourceId> descriptors;
  for(const auto &range : info.replayUAVRanges)
  {
    if(!IsReplayWriteInPrefix(info.replayUAVFirstEvents, range, cutoff))
    {
      skippedRanges++;
      continue;
    }
    WrappedID3D12DescriptorHeap *heap =
        m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(range.heap);
    if(!heap || m_pDevice->GetResourceManager()->HasReplacement(range.heap) ||
       !heap->GatherReplayUAVResources(range.first, range.count, descriptors))
    {
      m_pDevice->InvalidateReplayDirtyTracking("unknownSubmittedDescriptors");
      return;
    }
  }
  for(ResourceId id : descriptors)
    m_pDevice->MarkReplayResourceDirty(id);
  // Log the target occurrence/OnlyDraw and the first submission that loses a valid dirty proof.
  // Other complete occurrences remain unlogged; never write one line for every submitted list.
  if(m_PreparedTimingEnabled &&
     (!m_pDevice->IsReplayDirtyTrackingValid() ||
      (baseEvent != ~0U && lastEvent >= baseEvent &&
       uint64_t(lastEvent) - baseEvent < info.eventCount)))
  {
    const rdcstr line = StringFormat::Fmt(
        "api=D3D12DirtyWriteCutoff baked=%s base=%u requestedEnd=%u localLast=%u enabled=%u "
        "indexed=%u skippedResources=%u skippedRootUAVs=%u skippedRanges=%u "
        "firstUnknown=%u futureUnknown=%u proofValid=%u fallback=%s",
        ToStr(baked).c_str(), baseEvent, lastEvent, cutoff.last,
        m_ReplayWriteCutoffEnabled ? 1U : 0U, cutoff.indexed ? 1U : 0U, skippedResources,
        skippedRootUAVs, skippedRanges, info.replayFirstUnknownWriteEvent,
        info.replayUnknownWrites && !cutoff.unknown ? 1U : 0U,
        m_pDevice->IsReplayDirtyTrackingValid() ? 1U : 0U, cutoff.fallback);
    RDCLOG("%s", line.c_str());
    AppendReplayTimingLog(line);
  }
}

static uint32_t ReplayChunkFirstWriteEvent(const BakedCmdListInfo &info, D3D12Chunk chunk)
{
  if(chunk == D3D12Chunk::List_ExecuteIndirect)
  {
    // ObserveCheckpointChunk runs after the handler and event increment. ExecuteIndirect has
    // already advanced past every reserved child; use its saved pre-expansion group start.
    // An unresolved group gets the whole-recording lower bound, never a guessed later event.
    if(!info.executeEvents.empty() && info.executeEvents.back().baseEvent < info.curEventID)
      return info.executeEvents.back().baseEvent;
    return 0;
  }
  return info.curEventID ? info.curEventID - 1 : 0;
}

void D3D12CommandData::ObserveCheckpointChunk(D3D12Chunk chunk, uint64_t chunkEndOffset)
{
  if(m_LastCmdListID != ResourceId())
  {
    BakedCmdListInfo &info = m_BakedCmdListInfo[m_LastCmdListID];
    if(chunk == D3D12Chunk::List_Reset)
    {
      info.replayWrites.clear();
      info.replayRootUAVWrites.clear();
      info.replayUAVRanges.clear();
      info.replayWriteFirstEvents.clear();
      info.replayRootUAVFirstEvents.clear();
      info.replayUAVFirstEvents.clear();
      info.replayFirstUnknownWriteEvent = ~0U;
      info.replayWritesKnown = info.replayWriteEventsKnown = info.replayUnknownWrites = false;
    }
    const uint32_t firstWriteEvent = ReplayChunkFirstWriteEvent(info, chunk);
    const bool previouslyKnown = !info.replayUnknownWrites;
    if(!IsPreparedReplaySafeChunk(chunk) && chunk != D3D12Chunk::List_ExecuteIndirect &&
       chunk != D3D12Chunk::List_SetPredication && chunk != D3D12Chunk::List_DiscardResource &&
       chunk != D3D12Chunk::List_ClearUnorderedAccessViewUint &&
       chunk != D3D12Chunk::List_ClearUnorderedAccessViewFloat)
      info.replayUnknownWrites = true;
    switch(chunk)
    {
      case D3D12Chunk::List_DrawInstanced:
      case D3D12Chunk::List_DrawIndexedInstanced:
      case D3D12Chunk::List_Dispatch:
      case D3D12Chunk::List_DispatchMesh:
      case D3D12Chunk::List_ExecuteIndirect:
        ObserveReplayWriteBindings(info, info.state, firstWriteEvent);
        break;
      case D3D12Chunk::List_BeginRenderPass:
      case D3D12Chunk::List_EndRenderPass:
      case D3D12Chunk::List_WriteBufferImmediate:
      case D3D12Chunk::List_AtomicCopyBufferUINT:
      case D3D12Chunk::List_AtomicCopyBufferUINT64:
      case D3D12Chunk::List_CopyTiles:
      case D3D12Chunk::List_ExecuteBundle:
      case D3D12Chunk::List_Barrier:
      case D3D12Chunk::List_BuildRaytracingAccelerationStructure:
      case D3D12Chunk::List_CopyRaytracingAccelerationStructure:
      case D3D12Chunk::List_EmitRaytracingAccelerationStructurePostbuildInfo:
      case D3D12Chunk::List_DispatchRays:
      case D3D12Chunk::List_SetProgram:
      case D3D12Chunk::List_DispatchGraph: info.replayUnknownWrites = true; break;
      default: break;
    }
    switch(chunk)
    {
      case D3D12Chunk::List_SetGraphicsRootConstantBufferView:
      case D3D12Chunk::List_SetComputeRootConstantBufferView:
      case D3D12Chunk::List_SetGraphicsRootShaderResourceView:
      case D3D12Chunk::List_SetComputeRootShaderResourceView:
      case D3D12Chunk::List_SetGraphicsRootUnorderedAccessView:
      case D3D12Chunk::List_SetComputeRootUnorderedAccessView:
        for(const auto *root : {&info.state.graphics, &info.state.compute})
          for(const auto &element : root->sigelems)
            if((element.type == eRootCBV || element.type == eRootSRV || element.type == eRootUAV) &&
               element.id != ResourceId())
              info.bufferPatchConsumers.insert(element.id);
        break;
      case D3D12Chunk::List_SetGraphicsRootDescriptorTable:
      case D3D12Chunk::List_SetComputeRootDescriptorTable:
      case D3D12Chunk::List_ExecuteIndirect:
        info.bufferPatchUnknownConsumers = true;
        break;
      case D3D12Chunk::List_SetGraphicsRootSignature:
      case D3D12Chunk::List_SetComputeRootSignature:
        for(const auto *root : {&info.state.graphics, &info.state.compute})
        {
          if(root->rootsig == ResourceId())
            continue;
          WrappedID3D12RootSignature *signature =
              m_pDevice ? m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12RootSignature>(
                              root->rootsig)
                        : NULL;
          if(!signature || (signature->sig.Flags &
                            D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED))
            info.bufferPatchUnknownConsumers = true;
        }
        break;
      default: break;
    }
    info.checkpointChunks.push_back(
        {m_LastCmdListID, m_CurChunkOffset, chunkEndOffset, info.curEventID, chunk});
    if(chunk == D3D12Chunk::List_ExecuteIndirect && !info.executeEvents.empty() &&
       info.executeEvents.back().sig)
      info.checkpointChunks.back().directIndirect =
          IsCheckpointDirectIndirectSignature(info.executeEvents.back().sig->sig);
    if(chunk == D3D12Chunk::List_ExecuteIndirect)
      if(info.executeEvents.empty() || !info.executeEvents.back().sig ||
         !HasKnownReplayIndirectWrites(info.executeEvents.back().sig->sig))
        info.replayUnknownWrites = true;
    if(info.replayUnknownWrites)
    {
      if(previouslyKnown)
        RecordUnknownReplayWrite(info, firstWriteEvent, chunk);
      else if(info.replayFirstUnknownWriteEvent == ~0U)
        RecordUnknownReplayWrite(info, 0, chunk);    // unknown origin keeps the original full fallback
    }
  }
  if(chunk == D3D12Chunk::List_Reset)
    m_BakedCmdListInfo[m_LastCmdListID].checkpointBegin = m_CurChunkOffset;
  else if(chunk == D3D12Chunk::List_Close)
    m_BakedCmdListInfo[m_LastCmdListID].checkpointEnd = chunkEndOffset;
  else if(chunk == D3D12Chunk::Queue_ExecuteCommandLists)
    m_CheckpointBoundaries.push_back(
        {m_RootEventID - 1, m_RootEventID, m_RootActionID, chunkEndOffset});

  // These commands carry state outside normal resource bytes and closed primary lists. A future
  // backend extension must explicitly save their state before permitting a seek across them.
  switch(chunk)
  {
    case D3D12Chunk::Queue_UpdateTileMappings:
    case D3D12Chunk::Queue_CopyTileMappings:
    case D3D12Chunk::List_ExecuteBundle:
    case D3D12Chunk::List_Barrier:
    case D3D12Chunk::List_BuildRaytracingAccelerationStructure:
    case D3D12Chunk::List_CopyRaytracingAccelerationStructure:
    case D3D12Chunk::List_DispatchRays:
    case D3D12Chunk::List_DispatchGraph:
      if(m_CheckpointUnsupported.empty())
        m_CheckpointUnsupported = "Checkpoint state is not implemented for " + ToStr(chunk);
      break;
    default: break;
  }
}

uint32_t D3D12CommandData::GetBufferPatchFirstConsumer(ResourceId buffer) const
{
  uint32_t first = ~0U;
  for(int p = 0; p < ePartialNum; p++)
    for(const auto &submission : m_Partial[p].cmdListExecs)
    {
      const auto found = m_BakedCmdListInfo.find(submission.first);
      if(found == m_BakedCmdListInfo.end())
        return 0;
      const auto &info = found->second;
      if(info.bufferPatchUnknownConsumers || info.bufferPatchConsumers.count(buffer))
        for(uint32_t event : submission.second)
          first = RDCMIN(first, event);
    }
  return first;
}

bool D3D12CommandData::GetCheckpointContinuation(
    uint64_t offset, uint32_t nextEvent,
    rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> &chunks, rdcstr &reason,
    CheckpointUnsupportedChunks *unsupported, uint32_t targetEvent) const
{
  chunks.clear();
  reason.clear();
  std::map<D3D12Chunk, uint64_t> rejectedChunks;
  for(const auto &entry : m_BakedCmdListInfo)
  {
    const BakedCmdListInfo &info = entry.second;
    if(!info.checkpointBegin || info.checkpointBegin >= offset)
      continue;
    const auto executions = m_Partial[Primary].cmdListExecs.find(entry.first);
    bool prefixSubmit = false, suffixSubmit = false;
    if(executions != m_Partial[Primary].cmdListExecs.end())
      for(uint32_t base : executions->second)
      {
        prefixSubmit |= base < nextEvent;
        suffixSubmit |= base >= nextEvent;
      }
    const bool open = !info.checkpointEnd || info.checkpointEnd > offset;
    if(!open && !suffixSubmit)
      continue;
    // Match Serialise_Reset's rerecord decision, including a list containing the target and any
    // repeated submission in the interval. Check secondary executions too: even though bundles
    // are currently rejected by checkpoint validation, never prune a list Reset could record.
    bool targetSubmit = targetEvent == ~0U;
    for(int p = 0; p < ePartialNum && !targetSubmit; p++)
    {
      const auto submits = m_Partial[p].cmdListExecs.find(entry.first);
      if(submits != m_Partial[p].cmdListExecs.end())
        for(uint32_t base : submits->second)
          targetSubmit |= base <= targetEvent;
    }
    if(prefixSubmit)
    {
      if(reason.empty())
        reason = "A submitted command list is recorded or submitted again across the checkpoint";
    }
    if(info.checkpointChunks.empty() ||
       info.checkpointChunks.front().type != D3D12Chunk::List_Reset)
    {
      if(reason.empty())
        reason = "Missing Reset for an outstanding command-list recording";
    }
    for(const auto &chunk : info.checkpointChunks)
    {
      if(chunk.offset >= offset)
        break;
      if(chunk.endOffset > offset && reason.empty())
        reason = "A command chunk crosses the checkpoint stream position";
      const bool directIndirect = chunk.type == D3D12Chunk::List_ExecuteIndirect &&
                                  (chunk.directIndirect || (!prefixSubmit && !suffixSubmit));
      if(!IsCheckpointContinuationSafeChunk(chunk.type) && !directIndirect)
      {
        rejectedChunks[chunk.type]++;
        if(unsupported)
          (*unsupported)[chunk.type].insert(chunk.offset);
      }
      chunks.push_back(chunk);
      chunks.back().metadataOnly = !targetSubmit && chunk.type != D3D12Chunk::List_Reset &&
                                   chunk.type != D3D12Chunk::List_Close;
    }
  }
  if(!rejectedChunks.empty())
  {
    if(!reason.empty())
      reason += "; ";
    reason += "Outstanding command-list prefix needs unsupported replay work: ";
    bool first = true;
    for(const auto &rejected : rejectedChunks)
    {
      reason += StringFormat::Fmt("%s%s(%llu)", first ? "" : ",", ToStr(rejected.first).c_str(),
                                  (unsigned long long)rejected.second);
      first = false;
    }
  }
  if(!reason.empty())
  {
    chunks.clear();
    return false;
  }
  if(!chunks.empty() && m_pDevice && m_pDevice->GetCaptureVersion() < 0x5)
  {
    reason = "Old capture lacks consumed CPU descriptor values for command-list continuation";
    return false;
  }
  // Preserve interleaved Reset/recording order, including parent->baked wrapper replacements.
  std::sort(chunks.begin(), chunks.end(),
              [](const BakedCmdListInfo::CheckpointRecordingChunk &a,
                 const BakedCmdListInfo::CheckpointRecordingChunk &b) { return a.offset < b.offset; });
  return true;
}

bool D3D12CommandData::ApplyCheckpointContinuationMetadata(
    const BakedCmdListInfo::CheckpointRecordingChunk &chunk)
{
  if(!chunk.metadataOnly || chunk.type == D3D12Chunk::List_Reset ||
     chunk.type == D3D12Chunk::List_Close || HasRerecordCmdList(chunk.baked) ||
     IsPartialCmdList(chunk.baked))
    return false;

  // Reset established the captured baked wrapper and parent replacement. No native list can
  // consume the skipped state in this request; suffix chunks/Close still see its exact cursor.
  // In particular do not derive this from MaxCommandCount: loading may have removed unused
  // ExecuteIndirect child events, and ShiftForRemoved already fixed eventAfter in this index.
  m_CurChunkOffset = chunk.offset;
  m_LastCmdListID = chunk.baked;
  m_BakedCmdListInfo[chunk.baked].curEventID = chunk.eventAfter;
  m_LastChunk = chunk.type;
  return true;
}

struct CheckpointQueryUse
{
  uint32_t eventId;
  ResourceId heap;
  uint32_t index;
  bool resolve;
  D3D12_QUERY_TYPE type;
  ResourceId baked;
  uint32_t relativeEvent;
  bool completed;
  bool restorable;
  bool nativeValid;
};

static D3D12CheckpointQueryPlan PlanCheckpointQueries(uint32_t nextEvent,
                                                     rdcarray<CheckpointQueryUse> queries)
{
  D3D12CheckpointQueryPlan plan;
  std::stable_sort(queries.begin(), queries.end(),
                   [](const CheckpointQueryUse &a, const CheckpointQueryUse &b) {
                     return a.eventId < b.eventId;
                   });
  std::map<D3D12CheckpointQuerySlot, D3D12CheckpointQueryVersion> prefix;
  std::set<D3D12CheckpointQuerySlot> suffixWrites;
  std::set<D3D12CheckpointQuerySlot> incompleteSuffixWrites;
  // A baked list can be submitted repeatedly. Its one recorded resolve cannot switch between
  // a saved prefix value and a newly produced native value on different submissions.
  std::map<D3D12CheckpointQueryResolve, bool> modes;
  for(const CheckpointQueryUse &query : queries)
  {
    const D3D12CheckpointQuerySlot slot = {query.heap, query.index};
    if(query.eventId < nextEvent)
    {
      if(!query.resolve)
      {
        prefix.erase(slot);
        if(query.completed && query.restorable)
          prefix[slot] = {query.type, query.eventId};
      }
      continue;
    }
    if(!query.resolve)
    {
      if(query.completed)
      {
        suffixWrites.insert(slot);
        incompleteSuffixWrites.erase(slot);
      }
      else
        incompleteSuffixWrites.insert(slot);
      continue;
    }
    if(!query.nativeValid)
      continue;    // immutable pre-capture query results already have an independent source
    const bool needsPrefix = suffixWrites.find(slot) == suffixWrites.end();
    const auto value = prefix.find(slot);
    const bool saved = needsPrefix && value != prefix.end() && query.restorable &&
                       value->second.type == query.type;
    const D3D12CheckpointQueryResolve key = {{query.baked, query.relativeEvent}, slot};
    const auto mode = modes.find(key);
    if(incompleteSuffixWrites.find(slot) != incompleteSuffixWrites.end() ||
       (needsPrefix && !saved) || (mode != modes.end() && mode->second != saved))
    {
      plan.firstUnsupportedResolve = query.eventId;
      plan.firstUnsupportedType = query.type;
      plan.unsupportedSlot = slot;
      plan.unsupportedReason = incompleteSuffixWrites.find(slot) != incompleteSuffixWrites.end()
                                   ? "Query has an unmatched suffix End"
                               : !needsPrefix || saved
                                   ? "Repeated command-list submission changes query resolve source"
                                   : "Query has no compatible completed prefix slot";
      break;
    }
    modes[key] = saved;
    if(saved)
    {
      plan.slots[slot] = value->second;
      plan.resolves.insert(key);
    }
  }
  return plan;
}

#if ENABLED(ENABLE_UNIT_TESTS)
TEST_CASE("D3D12 dirty plans use final explicit writes and preserve alias storage closure",
          "[d3d12][replay][dirty-restore]")
{
  D3D12CommandData command;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  const ResourceId copied = ResourceIDGen::GetNewUniqueID();
  const ResourceId input = ResourceIDGen::GetNewUniqueID();
  const ResourceId alias = ResourceIDGen::GetNewUniqueID();
  auto &info = command.m_BakedCmdListInfo[baked];
  info.action = new D3D12ActionTreeNode;
  info.action->resourceUsage.push_back({copied, EventUsage(1, ResourceUsage::CopyDst)});
  info.action->resourceUsage.push_back({input, EventUsage(1, ResourceUsage::CopySrc)});
  info.action->children.push_back(D3D12ActionTreeNode());
  info.action->children.back().resourceUsage.push_back(
      {texture, EventUsage(2, ResourceUsage::ColorTarget)});
  info.checkpointChunks.push_back({baked, 0, 20, 0, D3D12Chunk::List_Reset});
  info.checkpointEnd = 100;
  BakedCmdListInfo::CheckpointBarrierOp barrier;
  barrier.type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
  barrier.after = alias;
  info.checkpointBarriers.push_back(barrier);
  command.BuildReplayWriteTracking();
  CHECK(info.replayWritesKnown);
  CHECK_FALSE(info.replayUnknownWrites);
  CHECK(info.replayWrites.count(copied) == 1);
  CHECK(info.replayWrites.count(texture) == 1);
  CHECK(info.replayWrites.count(input) == 0);
  CHECK(info.replayWrites.count(alias) == 1);
  // Building metadata neither invents a submission nor grants completeness to an open recording.
  CHECK(command.m_Partial[D3D12CommandData::Primary].cmdListExecs.empty());
  const ResourceId open = ResourceIDGen::GetNewUniqueID();
  command.m_BakedCmdListInfo[open].action = new D3D12ActionTreeNode;
  command.BuildReplayWriteTracking();
  CHECK_FALSE(command.m_BakedCmdListInfo[open].replayWritesKnown);
  info.checkpointBarriers.back().after = ResourceId();
  command.BuildReplayWriteTracking();
  CHECK(info.replayUnknownWrites);
}

TEST_CASE("D3D12 dirty UAV ranges have no reflected-usage cutoff or overflowing bounds",
          "[d3d12][replay][dirty-restore]")
{
  BakedCmdListInfo info;
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  CHECK(AddReplayUAVRange(info, heap, 8000000, 64, 128, 5000000));
  REQUIRE(info.replayUAVRanges.size() == 1);
  CHECK(info.replayUAVRanges.begin()->first == 192);
  CHECK(info.replayUAVRanges.begin()->count == 5000000);
  CHECK(AddReplayUAVRange(info, heap, 8000000, 7900000, 100, UINT_MAX));
  CHECK_FALSE(AddReplayUAVRange(info, heap, 8000000, ~0ULL, 1, 1));
  CHECK_FALSE(AddReplayUAVRange(info, heap, 8000000, 7900000, 100001, 1));
  CHECK_FALSE(AddReplayUAVRange(info, heap, 8000000, 7900000, 0, 100001));
  CHECK(AddReplayUAVRange(info, heap, 8000000, 8000000, 0, UINT_MAX));
  CHECK(info.replayUAVRanges.size() == 2);

  D3D12CommandSignature signature;
  CHECK_FALSE(HasKnownReplayIndirectWrites(signature));
  for(D3D12_INDIRECT_ARGUMENT_TYPE type : {
          D3D12_INDIRECT_ARGUMENT_TYPE_DRAW, D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH,
          D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT_BUFFER_VIEW,
          D3D12_INDIRECT_ARGUMENT_TYPE_SHADER_RESOURCE_VIEW,
          D3D12_INDIRECT_ARGUMENT_TYPE_VERTEX_BUFFER_VIEW,
          D3D12_INDIRECT_ARGUMENT_TYPE_INDEX_BUFFER_VIEW})
  {
    D3D12_INDIRECT_ARGUMENT_DESC argument = {};
    argument.Type = type;
    signature.arguments.push_back(argument);
    CHECK(HasKnownReplayIndirectWrites(signature));
  }
  signature.arguments.back().Type = D3D12_INDIRECT_ARGUMENT_TYPE_UNORDERED_ACCESS_VIEW;
  CHECK_FALSE(HasKnownReplayIndirectWrites(signature));
  signature.arguments.back().Type = D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_RAYS;
  CHECK_FALSE(HasKnownReplayIndirectWrites(signature));
  signature.arguments.back().Type = D3D12_INDIRECT_ARGUMENT_TYPE_DRAW;
  signature.raytraced = true;
  CHECK_FALSE(HasKnownReplayIndirectWrites(signature));
}

TEST_CASE("D3D12 dirty write cutoffs exclude future resources without forgetting earlier writes",
          "[d3d12][replay][dirty-restore]")
{
  BakedCmdListInfo info;
  info.eventCount = 30;
  info.replayWritesKnown = info.replayWriteEventsKnown = true;
  const ResourceId output = ResourceIDGen::GetNewUniqueID();
  const ResourceId futureSparse = ResourceIDGen::GetNewUniqueID();
  const ResourceId rootUAV = ResourceIDGen::GetNewUniqueID();
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, output, 9);
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, output, 21);
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, futureSparse, 20);
  RecordReplayWriteStart(info.replayRootUAVWrites, info.replayRootUAVFirstEvents, rootUAV, 11);
  REQUIRE(AddReplayUAVRange(info, heap, 8000000, 64, 128, 5000000, 12));
  const auto range = *info.replayUAVRanges.begin();
  const auto before = SelectReplayWriteCutoff(info, true, true, 100, 108);
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayWriteFirstEvents, output, before));
  const auto at = SelectReplayWriteCutoff(info, true, true, 100, 109);
  CHECK(at.indexed);
  CHECK(at.last == 9);
  CHECK(IsReplayWriteInPrefix(info.replayWriteFirstEvents, output, at));
  // This is the production filter before MarkReplayResourceDirty can invalidate on a sparse RID.
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayWriteFirstEvents, futureSparse, at));
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayRootUAVFirstEvents, rootUAV, at));
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayUAVFirstEvents, range, at));
  const auto uav = SelectReplayWriteCutoff(info, true, true, 100, 112);
  CHECK(IsReplayWriteInPrefix(info.replayRootUAVFirstEvents, rootUAV, uav));
  CHECK(IsReplayWriteInPrefix(info.replayUAVFirstEvents, range, uav));
  CHECK(info.replayUAVFirstEvents.begin()->second == 12);
  CHECK(info.replayWriteFirstEvents[output] == 9);
  CHECK(info.replayWrites.size() == 2);
  const auto full = SelectReplayWriteCutoff(info, true, true, 100, 500);
  CHECK(full.last == 29);
  CHECK(IsReplayWriteInPrefix(info.replayWriteFirstEvents, futureSparse, full));
}

TEST_CASE("D3D12 dirty cutoffs use each repeated submission base and fail closed on unknown origins",
          "[d3d12][replay][dirty-restore]")
{
  BakedCmdListInfo info;
  info.eventCount = 19;
  info.replayWritesKnown = info.replayWriteEventsKnown = true;
  const ResourceId trailingWrite = ResourceIDGen::GetNewUniqueID();
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, trailingWrite, 17);
  // The repeated-list fixture's target82 has WithoutDraw81: occurrence44 is full, occurrence68
  // is partial. Using the latter's base for both would incorrectly forget the earlier write.
  const auto earlier = SelectReplayWriteCutoff(info, true, true, 44, 81);
  const auto partial = SelectReplayWriteCutoff(info, true, true, 68, 81);
  const auto onlyDraw = SelectReplayWriteCutoff(info, true, true, 68, 82);
  CHECK(earlier.last == 18);
  CHECK(partial.last == 13);
  CHECK(onlyDraw.last == 14);
  CHECK(IsReplayWriteInPrefix(info.replayWriteFirstEvents, trailingWrite, earlier));
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayWriteFirstEvents, trailingWrite, partial));
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayWriteFirstEvents, trailingWrite, onlyDraw));
  RecordUnknownReplayWrite(info, 17, D3D12Chunk::List_ExecuteBundle);
  CHECK_FALSE(SelectReplayWriteCutoff(info, true, true, 68, 84).unknown);
  CHECK(SelectReplayWriteCutoff(info, true, true, 68, 85).unknown);
  CHECK(SelectReplayWriteCutoff(info, true, true, 68, 86).unknown);
  CHECK(SelectReplayWriteCutoff(info, true, true, 44, 81).unknown);
  const auto disabled = SelectReplayWriteCutoff(info, false, true, 68, 81);
  CHECK_FALSE(disabled.indexed);
  CHECK(disabled.unknown);
  CHECK(IsReplayWriteInPrefix(info.replayWriteFirstEvents, trailingWrite, disabled));
  CHECK_FALSE(SelectReplayWriteCutoff(info, true, false, 68, 81).indexed);
  CHECK_FALSE(SelectReplayWriteCutoff(info, true, true, 90, 81).indexed);
  CHECK_FALSE(SelectReplayWriteCutoff(info, true, true, ~0U, ~0U).indexed);
  info.replayWriteEventsKnown = false;
  CHECK(SelectReplayWriteCutoff(info, true, true, 68, 81).unknown);
  CHECK_FALSE(SelectReplayWriteCutoff(info, true, true, 68, 81).indexed);
  info.replayWriteEventsKnown = true;
  RecordUnknownReplayWrite(info, 2, D3D12Chunk::List_DispatchRays);
  CHECK(info.replayFirstUnknownWriteEvent == 2);
  CHECK(info.replayUnknownWriteChunk == D3D12Chunk::List_DispatchRays);
  CHECK(SelectReplayWriteCutoff(info, true, true, 68, 81).unknown);
  info.replayUnknownWrites = false;
  const auto largeBase = SelectReplayWriteCutoff(info, true, true, ~0U - 10, ~0U - 1);
  CHECK(largeBase.indexed);
  CHECK(largeBase.last == 9);
}

TEST_CASE("D3D12 dirty indirect write starts precede expanded child events and follow compaction",
          "[d3d12][replay][dirty-restore]")
{
  BakedCmdListInfo info;
  info.eventCount = 24;
  info.actionCount = 20;
  info.curEventID = 21;
  CHECK(ReplayChunkFirstWriteEvent(info, D3D12Chunk::List_DrawInstanced) == 20);
  CHECK(ReplayChunkFirstWriteEvent(info, D3D12Chunk::List_ExecuteIndirect) == 0);
  BakedCmdListInfo::ExecuteData execute = {};
  execute.baseEvent = 2;
  info.executeEvents.push_back(execute);
  CHECK(ReplayChunkFirstWriteEvent(info, D3D12Chunk::List_ExecuteIndirect) == 2);
  const ResourceId indirectWrite = ResourceIDGen::GetNewUniqueID();
  const ResourceId laterWrite = ResourceIDGen::GetNewUniqueID();
  const ResourceId rootUAV = ResourceIDGen::GetNewUniqueID();
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, indirectWrite, 2);
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, laterWrite, 22);
  RecordReplayWriteStart(info.replayRootUAVWrites, info.replayRootUAVFirstEvents, rootUAV, 22);
  REQUIRE(AddReplayUAVRange(info, heap, 64, 0, 0, 64, 22));
  RecordUnknownReplayWrite(info, 23, D3D12Chunk::List_ExecuteBundle);
  info.action = new D3D12ActionTreeNode;
  D3D12ActionTreeNode after;
  after.action.eventId = 21;
  after.action.actionId = 19;
  info.action->children.push_back(after);
  info.ShiftForRemoved(3, 3, 0);
  CHECK(info.replayWriteFirstEvents[indirectWrite] == 2);
  CHECK(info.replayWriteFirstEvents[laterWrite] == 19);
  CHECK(info.replayRootUAVFirstEvents[rootUAV] == 19);
  CHECK(info.replayUAVFirstEvents.begin()->second == 19);
  CHECK(info.replayFirstUnknownWriteEvent == 20);
  CHECK(info.executeEvents[0].baseEvent == 2);
  info.replayWritesKnown = info.replayWriteEventsKnown = true;
  const auto insideIndirect = SelectReplayWriteCutoff(info, true, true, 100, 103);
  CHECK(IsReplayWriteInPrefix(info.replayWriteFirstEvents, indirectWrite, insideIndirect));
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayWriteFirstEvents, laterWrite, insideIndirect));
  // Missing group metadata is assigned event0, which can only over-mark and cannot prove clean.
  info.executeEvents.clear();
  CHECK(ReplayChunkFirstWriteEvent(info, D3D12Chunk::List_ExecuteIndirect) == 0);
}

TEST_CASE("D3D12 checkpoints preserve completed query values by submission and recording identity",
          "[d3d12][replay][checkpoint-query]")
{
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  const ResourceId first = ResourceIDGen::GetNewUniqueID();
  const ResourceId second = ResourceIDGen::GetNewUniqueID();
  rdcarray<CheckpointQueryUse> queries = {
      {5, heap, 4, false, D3D12_QUERY_TYPE_TIMESTAMP, first, 2, true, true, true},
      {25, heap, 4, true, D3D12_QUERY_TYPE_TIMESTAMP, second, 1, false, true, true}};
  auto plan = PlanCheckpointQueries(20, queries);
  REQUIRE(plan.slots.size() == 1);
  CHECK(plan.slots.begin()->second.endEvent == 5);
  CHECK(plan.resolves.size() == 1);
  CHECK(plan.firstUnsupportedResolve == ~0U);

  // No completed prefix End may be inferred from the whole-frame native-valid bit.
  queries[0].completed = false;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == 25);
  queries[0].completed = true;
  queries[0].restorable = false;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == 25);
  queries[0].restorable = true;
  queries[0].type = D3D12_QUERY_TYPE_OCCLUSION;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == 25);
  queries[0].type = D3D12_QUERY_TYPE_TIMESTAMP;

  // Re-recording an End before the boundary's file position is irrelevant: submission EID wins.
  queries.push_back({24, heap, 4, false, D3D12_QUERY_TYPE_TIMESTAMP, second, 0, true, true, true});
  plan = PlanCheckpointQueries(20, queries);
  CHECK(plan.slots.empty());
  CHECK(plan.firstUnsupportedResolve == ~0U);
  queries.back().eventId = 26;
  queries.push_back({45, heap, 4, true, D3D12_QUERY_TYPE_TIMESTAMP, second, 1, false, true, true});
  plan = PlanCheckpointQueries(20, queries);
  CHECK(plan.firstUnsupportedResolve == 45);
  CHECK(IsCheckpointQueryTargetSafe(44, plan.firstUnsupportedResolve));
  CHECK_FALSE(IsCheckpointQueryTargetSafe(45, plan.firstUnsupportedResolve));
  CHECK(plan.resolves.size() == 1);

  // A distinct later recording can resolve the new native value without affecting the old one.
  queries.back().baked = first;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == ~0U);
  queries[2].completed = false;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == 45);
  queries[2].completed = true;
  queries[1].index = 5;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == 25);
  queries[1].nativeValid = false;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == ~0U);
}
#endif

bool D3D12CommandData::IsCheckpointBoundarySafe(CheckpointBoundary &boundary,
                                               rdcstr &reason,
                                               CheckpointUnsupportedChunks *unsupported) const
{
  boundary.firstNativeResolveEID = ~0U;
  boundary.firstNativeResolveType = D3D12_QUERY_TYPE_OCCLUSION;
  boundary.queries = {};
  if(!m_CheckpointUnsupported.empty())
  {
    reason = m_CheckpointUnsupported;
    return false;
  }

  rdcarray<CheckpointQueryUse> queries;
  rdcarray<BakedCmdListInfo::CheckpointBarrierOp> barriers;
  rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> continuation;
  if(!GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation, reason, unsupported))
    return false;
  for(const auto &entry : m_Partial[Primary].cmdListExecs)
  {
    auto found = m_BakedCmdListInfo.find(entry.first);
    if(found == m_BakedCmdListInfo.end())
    {
      reason = "Missing baked command-list metadata";
      return false;
    }
    const BakedCmdListInfo &info = found->second;
    for(uint32_t base : entry.second)
    {
      // Unsubmitted recordings are reconstructed from their own original prefix chunks after
      // resource restore. Submitted lists cannot cross the point (checked above).
      if(base < boundary.nextEvent &&
         (info.checkpointEnd == 0 || info.checkpointEnd > boundary.offset ||
          base + info.eventCount >= boundary.nextEvent))
      {
        reason = "Checkpoint is inside an incomplete command-list submission";
        return false;
      }
      if(base < boundary.nextEvent)
        for(const auto &barrier : info.checkpointBarriers)
        {
          barriers.push_back(barrier);
          barriers.back().relativeEvent += base;
        }
      for(const auto &query : info.checkpointQueries)
      {
        if(query.count > 1024 * 1024 || query.index > ~0U - query.count)
        {
          reason = "Query range exceeds checkpoint metadata limits";
          return false;
        }
        WrappedID3D12QueryHeap *heap =
            m_pDevice ? m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12QueryHeap>(query.heap)
                      : NULL;
        for(uint32_t index = 0; index < query.count; index++)
        {
          const uint32_t slot = query.index + index;
          const bool valid = !heap || heap->IsReplayQueryValid(slot);
          const D3D12_QUERY_TYPE type =
              heap && query.resolve ? heap->GetReplayQueryType(slot) : query.type;
          queries.push_back(
              {base + query.relativeEvent, query.heap, slot, query.resolve, type, entry.first,
               query.relativeEvent, query.completed,
               heap && heap->CanCheckpointQuery(slot, type), valid});
        }
      }
    }
  }
  std::stable_sort(barriers.begin(), barriers.end(),
                   [](const BakedCmdListInfo::CheckpointBarrierOp &a,
                      const BakedCmdListInfo::CheckpointBarrierOp &b) {
                     return a.relativeEvent < b.relativeEvent;
                   });
  using SplitKey = std::pair<ResourceId, uint32_t>;
  std::map<SplitKey, std::pair<D3D12_RESOURCE_STATES, D3D12_RESOURCE_STATES>> pendingSplits;
  for(const auto &barrier : barriers)
  {
    if(barrier.type == D3D12_RESOURCE_BARRIER_TYPE_ALIASING)
    {
      if(!m_pDevice || !m_pDevice->IsReplayCheckpointAliasingSafe(barrier.before, barrier.after))
      {
        reason = "Prefix aliasing involves overlapping or sparse storage";
        return false;
      }
      continue;
    }
    const SplitKey key = std::make_pair(barrier.before, barrier.subresource);
    const auto states = std::make_pair(barrier.stateBefore, barrier.stateAfter);
    if(barrier.flags == D3D12_RESOURCE_BARRIER_FLAG_BEGIN_ONLY)
    {
      // ALL_SUBRESOURCES overlaps each individual subresource too. Do not accept an ambiguous
      // split state merely because its key differs from another pending transition.
      for(const auto &pending : pendingSplits)
        if(pending.first.first == key.first &&
           (pending.first.second == key.second || key.second == D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES ||
            pending.first.second == D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES))
        {
          reason = "Overlapping split resource transitions require additional checkpoint state";
          return false;
        }
      pendingSplits[key] = states;
    }
    else if(barrier.flags == D3D12_RESOURCE_BARRIER_FLAG_END_ONLY)
    {
      auto pending = pendingSplits.find(key);
      if(pending == pendingSplits.end() || pending->second != states)
      {
        reason = "A split resource transition has no matching prefix begin";
        return false;
      }
      pendingSplits.erase(pending);
    }
    else
    {
      reason = "Unsupported split resource barrier flags";
      return false;
    }
  }
  if(!pendingSplits.empty())
  {
    reason = "A split resource transition is still pending at this submission boundary";
    return false;
  }
  std::stable_sort(queries.begin(), queries.end(),
                   [](const CheckpointQueryUse &a, const CheckpointQueryUse &b) {
                     return a.eventId < b.eventId;
                   });
  std::set<std::pair<ResourceId, uint32_t>> suffixWrites;
  for(const CheckpointQueryUse &query : queries)
  {
    if(query.eventId < boundary.nextEvent)
      continue;
    auto key = std::make_pair(query.heap, query.index);
    if(!query.resolve)
      suffixWrites.insert(key);
    else if(suffixWrites.find(key) == suffixWrites.end())
    {
      WrappedID3D12QueryHeap *heap =
          m_pDevice ? m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12QueryHeap>(query.heap)
                    : NULL;
      // Queries never issued in this frame already resolve from the immutable captured result
      // buffer. A live query must be re-issued after the checkpoint before its next resolve.
      if(!heap || heap->IsReplayQueryValid(query.index))
      {
        // Native query heap contents cannot be restored from ordinary resource bytes. Earlier
        // targets remain valid: this resolve is not recorded before its EID enters replay range.
        boundary.firstNativeResolveEID = query.eventId;
        boundary.firstNativeResolveType = heap ? heap->GetReplayQueryType(query.index) : query.type;
        break;
      }
    }
  }
  if(m_pDevice && Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_CHECKPOINT_QUERIES") != "1")
  {
    boundary.queries = PlanCheckpointQueries(boundary.nextEvent, queries);
    boundary.firstNativeResolveEID = boundary.queries.firstUnsupportedResolve;
    boundary.firstNativeResolveType = boundary.queries.firstUnsupportedType;
  }
  return true;
}

void D3D12CommandData::ObservePreparedReplayChunk(D3D12Chunk chunk, uint64_t chunkEndOffset)
{
  const auto reject = [this, chunk](BakedCmdListInfo &info, PreparedReplayRejectReason reason) {
    info.preparedReplaySafe = false;
    if(info.preparedReplayRejectReason == PreparedReplayRejectReason::None)
    {
      info.preparedReplayRejectReason = reason;
      info.preparedReplayRejectChunk = chunk;
      info.preparedReplayRejectOffset = m_CurChunkOffset;
      info.preparedReplayRejectList = m_LastCmdListID;
    }
  };

  if(chunk == D3D12Chunk::List_Reset)
  {
    if(m_PreparedScanList != ResourceId())
      reject(m_BakedCmdListInfo[m_PreparedScanList], PreparedReplayRejectReason::InterleavedReset);

    m_PreparedScanList = m_LastCmdListID;
    if(m_PreparedScanList != ResourceId())
    {
      BakedCmdListInfo &info = m_BakedCmdListInfo[m_PreparedScanList];
      info.preparedReplayBegin = m_CurChunkOffset;
      info.preparedReplayEnd = 0;
      info.preparedReplayRejectReason = PreparedReplayRejectReason::None;
      info.preparedReplayRejectChunk = chunk;
      info.preparedReplayRejectOffset = m_CurChunkOffset;
      info.preparedReplayRejectList = ResourceId();
      info.preparedReplayQueries.Reset();
    }
    return;
  }

  if(m_PreparedScanList == ResourceId())
    return;

  BakedCmdListInfo &info = m_BakedCmdListInfo[m_PreparedScanList];

  if(chunk == D3D12Chunk::List_Close && m_LastCmdListID == m_PreparedScanList &&
     !info.preparedReplayQueries.Complete())
    reject(info, PreparedReplayRejectReason::APICommand);

  // Some handlers reject particular arguments, e.g. ResourceBarrier permits only transitions.
  // Observe after ProcessChunk so this records that rule separately from the chunk whitelist.
  if(!info.preparedReplaySafe &&
     info.preparedReplayRejectReason == PreparedReplayRejectReason::None)
    reject(info, PreparedReplayRejectReason::APICommand);

  // A non-command chunk, interleaved recording from another list, or a command with replay-owned
  // transient data makes Reset..Close unsafe to seek across as one unit.
  if(m_LastCmdListID != m_PreparedScanList)
    reject(info, PreparedReplayRejectReason::InterleavedChunk);
  else if(!IsPreparedReplaySafeChunk(chunk))
    reject(info, PreparedReplayRejectReason::UnsupportedChunk);

  if(chunk == D3D12Chunk::List_Close && m_LastCmdListID == m_PreparedScanList)
  {
    if(info.preparedReplaySafe)
      info.preparedReplayEnd = chunkEndOffset;
    m_PreparedScanList = ResourceId();
  }
}

void D3D12CommandData::PrepareFullReplayCompanion(ResourceId baked, ResourceId parent,
                                                ID3D12PipelineState *initialPipeline)
{
  if(m_OutsideCmdList != NULL || m_FirstEventID != 1 ||
     m_Partial[Primary].partialParent != baked)
    return;

  const BakedCmdListInfo &info = m_BakedCmdListInfo[baked];
  const uint32_t partialBase = m_Partial[Primary].baseEvent;
  bool needsFull = false;
  for(uint32_t base : m_Partial[Primary].cmdListExecs[baked])
    needsFull |= IsCompletedReplayOccurrence(base, info.eventCount, partialBase, m_LastEventID);
  if(!needsFull)
    return;

  if(m_ActionCallback != NULL || D3D12_Debug_RT_Auditing() || !IsFullReplayCompanionSafe(info))
  {
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    {
      const rdcstr line = StringFormat::Fmt(
          "api=D3D12RepeatedList op=unsupported baked=%s target=%u partialBase=%u "
          "reason=nonContiguousOrUnsupportedRecordingContext",
          ToStr(baked).c_str(), m_LastEventID, partialBase);
      RDCLOG("[EIDTiming] %s", line.c_str());
      AppendReplayTimingLog(line);
    }
    return;
  }

  FullReplayCompanion &companion = m_FullReplayCompanions[baked];
  companion.parent = parent;
  companion.initialPipeline = GetResID(initialPipeline);
  companion.resetState = info.state;
  companion.partialBase = partialBase;
}

RDResult D3D12CommandData::RecordFullReplayCompanion(
    ResourceId baked, uint64_t endOffset,
    const std::function<RDResult(const BakedCmdListInfo::CheckpointRecordingChunk &)> &record)
{
  auto found = m_FullReplayCompanions.find(baked);
  if(found == m_FullReplayCompanions.end() || found->second.ready)
    return ResultCode::Succeeded;

  FullReplayCompanion &companion = found->second;
  BakedCmdListInfo &info = m_BakedCmdListInfo[baked];
  if(endOffset != info.preparedReplayEnd || !IsFullReplayCompanionSafe(info) ||
     m_ActionCallback != NULL || m_OutsideCmdList != NULL || !HasRerecordCmdList(baked))
    return RDResult(ResultCode::APIReplayFailed, "Repeated-list companion recording guard changed");

  PerformanceTimer timer;
  HRESULT hr = m_pDevice->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                                                __uuidof(ID3D12CommandAllocator),
                                                (void **)&companion.allocator);
  if(FAILED(hr) || companion.allocator == NULL)
    return RDResult(ResultCode::APIReplayFailed, "Could not allocate repeated-list companion");
  ID3D12PipelineState *initialPipeline = NULL;
  if(companion.initialPipeline != ResourceId())
  {
    initialPipeline = m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12PipelineState>(
        companion.initialPipeline);
    if(initialPipeline == NULL)
      return RDResult(ResultCode::APIReplayFailed, "Repeated-list initial pipeline is no longer live");
  }
  ID3D12GraphicsCommandList *baseList = NULL;
  hr = m_pDevice->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, companion.allocator,
                                    initialPipeline, __uuidof(ID3D12GraphicsCommandList),
                                    (void **)&baseList);
  companion.list = (ID3D12GraphicsCommandListX *)baseList;
  if(FAILED(hr) || companion.list == NULL)
    return RDResult(ResultCode::APIReplayFailed, "Could not create repeated-list companion");
  m_pDevice->GetResourceManager()->SetInternalResource(companion.allocator);
  m_pDevice->GetResourceManager()->SetInternalResource(companion.list);

  // Keep the existing captured-ID wrapper and replacement mapping intact. Only handler routing
  // points at the fresh internal list while the same serialized body is decoded a second time.
  BakedCmdListInfo &parent = m_BakedCmdListInfo[companion.parent];
  ReplayCompanionMutableState partialState(info);
  const uint32_t parentEvent = parent.curEventID;
  const D3D12RenderState renderState = m_RenderState;
  const auto previousRouting = m_RerecordCmds;
  const ResourceId lastList = m_LastCmdListID;
  const uint64_t lastOffset = m_CurChunkOffset;
  const SDChunkMetaData metadata = m_ChunkMetadata;
  const D3D12Chunk lastChunk = m_LastChunk;
  const uint32_t rootEvent = m_RootEventID, rootAction = m_RootActionID;
  const bool addedAction = m_AddedAction;
  ResourceId partialParents[ePartialNum];
  uint32_t partialBases[ePartialNum];
  bool renderPasses[ePartialNum];
  for(int p = 0; p < ePartialNum; p++)
  {
    partialParents[p] = m_Partial[p].partialParent;
    partialBases[p] = m_Partial[p].baseEvent;
    renderPasses[p] = m_Partial[p].renderPassActive;
    if(m_Partial[p].partialParent == baked)
      m_Partial[p].Reset();
  }
  m_RerecordCmds[baked] = companion.list;
  m_RerecordCmds[companion.parent] = companion.list;
  info.state = companion.resetState;
  info.barriers.clear();
  info.m_OutstandingQueries.clear();
  info.curEventID = 0;
  info.markerCount = 0;

  RDResult result = ResultCode::Succeeded;
  uint64_t sourceBytes = 0;
  // Reset is represented by the fresh list's creation and captured reset-state seed. Running
  // Serialise_Reset again would attempt to create a second wrapper with the same captured ID.
  for(size_t i = 1; i < info.checkpointChunks.size(); i++)
  {
    const auto &chunk = info.checkpointChunks[i];
    result = record(chunk);
    if(result != ResultCode::Succeeded)
      break;
    info.curEventID = chunk.eventAfter;
    m_LastChunk = chunk.type;
    sourceBytes += chunk.endOffset - chunk.offset;
  }

  // Full companion barriers remain under its fresh native wrapper ID. Restore the target's
  // state/barriers so OnlyDraw and pipeline publication continue to describe the selected EID.
  partialState.Restore(info);
  parent.curEventID = parentEvent;
  m_RenderState = renderState;
  m_RerecordCmds = previousRouting;
  m_LastCmdListID = lastList;
  m_CurChunkOffset = lastOffset;
  m_ChunkMetadata = metadata;
  m_LastChunk = lastChunk;
  m_RootEventID = rootEvent;
  m_RootActionID = rootAction;
  m_AddedAction = addedAction;
  for(int p = 0; p < ePartialNum; p++)
  {
    m_Partial[p].partialParent = partialParents[p];
    m_Partial[p].baseEvent = partialBases[p];
    m_Partial[p].renderPassActive = renderPasses[p];
  }
  companion.ready = result == ResultCode::Succeeded;
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    const rdcstr line = StringFormat::Fmt(
        "api=D3D12RepeatedList op=recordFull baked=%s target=%u partialBase=%u ready=%u "
        "chunks=%zu sourceBytes=%llu total_ms=%.3f",
        ToStr(baked).c_str(), m_LastEventID, companion.partialBase, companion.ready ? 1U : 0U,
        info.checkpointChunks.size() - 1, (unsigned long long)sourceBytes, timer.GetMilliseconds());
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return result;
}

ID3D12GraphicsCommandListX *D3D12CommandData::RerecordCmdListForSubmission(ResourceId baked,
                                                                         uint32_t baseEvent)
{
  auto found = m_FullReplayCompanions.find(baked);
  if(found != m_FullReplayCompanions.end() && found->second.ready)
  {
    const bool completed = IsCompletedReplayOccurrence(baseEvent,
        m_BakedCmdListInfo[baked].eventCount, found->second.partialBase, m_LastEventID);
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    {
      const rdcstr line = StringFormat::Fmt(
          "api=D3D12RepeatedList op=%s baked=%s base=%u target=%u",
          completed ? "submitFull" : "submitPartial", ToStr(baked).c_str(), baseEvent, m_LastEventID);
      RDCLOG("[EIDTiming] %s", line.c_str());
      AppendReplayTimingLog(line);
    }
    if(completed)
      return found->second.list;
  }
  return RerecordCmdList(baked);
}

void D3D12CommandData::ClearFullReplayCompanions()
{
  // Lists and their independent allocators stay alive until the next drained prefix replay or
  // queue teardown, including across the subsequent OnlyDraw of this same selected event.
  for(auto &entry : m_FullReplayCompanions)
  {
    if(entry.second.list)
      m_BakedCmdListInfo.erase(GetResID(entry.second.list));
    SAFE_RELEASE(entry.second.list);
    SAFE_RELEASE(entry.second.allocator);
  }
  m_FullReplayCompanions.clear();
}

ID3D12GraphicsCommandListX *D3D12CommandData::BeginPreparedCommandList(
    ResourceId baked, ResourceId parent, D3D12_COMMAND_LIST_TYPE type,
    ID3D12PipelineState *initialState, bool &reused)
{
  reused = false;

  if(!m_PreparedReplayEnabled)
    return NULL;

  m_PreparedAttempts++;

  if(type != D3D12_COMMAND_LIST_TYPE_DIRECT)
  {
    m_PreparedRejectedType++;
    return NULL;
  }

  BakedCmdListInfo &info = m_BakedCmdListInfo[baked];
  if(!info.preparedReplaySafe || info.preparedReplayEnd <= info.preparedReplayBegin)
  {
    m_PreparedRejectedUnsafe++;
    if(m_PreparedTimingEnabled)
    {
      const PreparedReplayRejectReason reason =
          info.preparedReplayRejectReason == PreparedReplayRejectReason::None
              ? PreparedReplayRejectReason::IncompleteRange
              : info.preparedReplayRejectReason;
      const uint64_t key = ((uint64_t)reason << 32) | (uint32_t)info.preparedReplayRejectChunk;
      PreparedReplayRejectSummary &summary = m_PreparedRejectSummary[key];
      if(summary.attempts == 0)
      {
        summary.reason = reason;
        summary.chunk = info.preparedReplayRejectChunk;
        summary.firstBaked = baked;
        summary.firstParent = info.parentList;
        summary.observedList = info.preparedReplayRejectList;
        auto observedIt = m_BakedCmdListInfo.find(summary.observedList);
        if(observedIt != m_BakedCmdListInfo.end())
          summary.observedParent = observedIt->second.parentList;
        summary.firstBeginOffset = info.preparedReplayBegin;
        summary.firstOffset = info.preparedReplayRejectOffset;
      }
      summary.attempts++;
    }
    return NULL;
  }

  auto preparedIt = m_PreparedCmdLists.find(baked);
  if(preparedIt != m_PreparedCmdLists.end())
  {
    PreparedCommandList &prepared = preparedIt->second;
    if(prepared.ready && prepared.beginOffset == info.preparedReplayBegin &&
       prepared.endOffset == info.preparedReplayEnd && prepared.list != NULL)
    {
      m_RerecordCmds[baked] = prepared.list;
      m_RerecordCmds[parent] = prepared.list;
      m_PreparedSkipEnd = prepared.endOffset;
      m_PreparedCacheHits++;
      reused = true;
      return prepared.list;
    }

    m_PreparedRejectedExisting++;
    return NULL;
  }

  const uint64_t sourceBytes = info.preparedReplayEnd - info.preparedReplayBegin;
  static const size_t maxPreparedLists = 128;
  static const uint64_t maxPreparedSourceBytes = 256ULL * 1024ULL * 1024ULL;
  if(m_PreparedCmdLists.size() >= maxPreparedLists ||
     sourceBytes > maxPreparedSourceBytes - RDCMIN(maxPreparedSourceBytes, m_PreparedCacheBytes))
  {
    m_PreparedRejectedBudget++;
    return NULL;
  }

  ID3D12CommandAllocator *allocator = NULL;
  HRESULT hr = m_pDevice->CreateCommandAllocator(type, __uuidof(ID3D12CommandAllocator),
                                                  (void **)&allocator);
  if(FAILED(hr) || allocator == NULL)
  {
    SAFE_RELEASE(allocator);
    return NULL;
  }

  ID3D12GraphicsCommandList *baseList = NULL;
  hr = m_pDevice->CreateCommandList(0, type, allocator, initialState,
                                    __uuidof(ID3D12GraphicsCommandList), (void **)&baseList);
  if(FAILED(hr) || baseList == NULL)
  {
    SAFE_RELEASE(baseList);
    SAFE_RELEASE(allocator);
    return NULL;
  }

  m_pDevice->GetResourceManager()->SetInternalResource(allocator);
  m_pDevice->GetResourceManager()->SetInternalResource(baseList);

  PreparedCommandList &prepared = m_PreparedCmdLists[baked];
  prepared.allocator = allocator;
  prepared.list = (ID3D12GraphicsCommandListX *)baseList;
  prepared.parentList = parent;
  prepared.beginOffset = info.preparedReplayBegin;
  prepared.endOffset = info.preparedReplayEnd;
  prepared.sourceBytes = sourceBytes;
  prepared.ready = false;

  m_PreparedCacheBytes += sourceBytes;
  m_PreparedCacheBuilds++;
  m_PreparedBuildingList = baked;
  m_RerecordCmds[baked] = prepared.list;
  m_RerecordCmds[parent] = prepared.list;
  return prepared.list;
}

void D3D12CommandData::FinishPreparedCommandList(ResourceId baked, uint64_t chunkEndOffset)
{
  if(m_PreparedBuildingList != baked)
    return;

  auto it = m_PreparedCmdLists.find(baked);
  if(it != m_PreparedCmdLists.end())
  {
    PreparedCommandList &prepared = it->second;
    prepared.ready = (prepared.list != NULL && prepared.endOffset == chunkEndOffset);
  }

  m_PreparedBuildingList = ResourceId();
}

void D3D12CommandData::ClearPreparedReplayCache()
{
  m_RerecordCmds.clear();
  m_PreparedBuildingList = ResourceId();
  m_PreparedSkipEnd = 0;

  for(auto it = m_PreparedCmdLists.begin(); it != m_PreparedCmdLists.end(); ++it)
  {
    ResourceId listId = it->second.list ? GetResID(it->second.list) : ResourceId();
    if(listId != ResourceId())
      m_BakedCmdListInfo.erase(listId);
    SAFE_RELEASE(it->second.list);
    SAFE_RELEASE(it->second.allocator);
  }

  m_PreparedCmdLists.clear();
  m_PreparedCacheBytes = 0;
}

void D3D12CommandData::AddEvent()
{
  APIEvent apievent;

  apievent.fileOffset = m_CurChunkOffset;
  apievent.eventId = m_LastCmdListID != ResourceId() ? m_BakedCmdListInfo[m_LastCmdListID].curEventID
                                                     : m_RootEventID;

  apievent.chunkIndex = uint32_t(m_StructuredFile->chunks.size() - 1);

  // if we're using replay-time debug messages, fetch them now since we can do better to correlate
  // to events on replay
  if(m_pDevice->GetReplayOptions().apiValidation)
    m_EventMessages = m_pDevice->GetDebugMessages();

  for(size_t i = 0; i < m_EventMessages.size(); i++)
    m_EventMessages[i].eventId = apievent.eventId;

  if(m_LastCmdListID != ResourceId())
  {
    m_BakedCmdListInfo[m_LastCmdListID].curEvents.push_back(apievent);

    rdcarray<DebugMessage> &msgs = m_BakedCmdListInfo[m_LastCmdListID].debugMessages;

    msgs.append(m_EventMessages);
  }
  else
  {
    if(m_RootAnnotation)
    {
      apievent.annotations = m_RootAnnotation->Duplicate();
      m_EventAnnotations.push_back(apievent.annotations);
    }

    m_RootEvents.push_back(apievent);
    m_Events.resize_for_index(apievent.eventId);
    m_Events[apievent.eventId] = apievent;

    for(auto it = m_EventMessages.begin(); it != m_EventMessages.end(); ++it)
      m_pDevice->AddDebugMessage(*it);
  }

  m_EventMessages.clear();
}

void D3D12CommandData::AddResourceUsage(D3D12ActionTreeNode &actionNode, ResourceId id,
                                        uint32_t EID, ResourceUsage usage)
{
  if(id == ResourceId())
    return;

  actionNode.resourceUsage.push_back(make_rdcpair(id, EventUsage(EID, usage)));
}

void D3D12CommandData::AddCPUUsage(ResourceId id, ResourceUsage usage)
{
  m_ResourceUses[id].push_back(EventUsage(m_RootEventID, usage));
}

void D3D12CommandData::AddUsageForBindInRootSig(const D3D12RenderState &state,
                                                D3D12ActionTreeNode &actionNode,
                                                const D3D12RenderState::RootSignature *rootsig,
                                                D3D12_DESCRIPTOR_RANGE_TYPE type, uint32_t space,
                                                uint32_t bind, uint32_t rangeSize)
{
  static bool hugeRangeWarned = false;

  ActionDescription &a = actionNode.action;
  uint32_t eid = a.eventId;

  // use a 'clamped' range size to avoid annoying overflow issues
  rangeSize = RDCMIN(rangeSize, 0x10000000U);

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  WrappedID3D12RootSignature *sig = rm->GetResAs<WrappedID3D12RootSignature>(rootsig->rootsig);

  for(size_t rootEl = 0; rootEl < sig->sig.Parameters.size(); rootEl++)
  {
    if(rootEl >= rootsig->sigelems.size())
      break;

    const D3D12RootSignatureParameter &p = sig->sig.Parameters[rootEl];
    const D3D12RenderState::SignatureElement &el = rootsig->sigelems[rootEl];

    if(p.ParameterType == D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS)
      continue;

    ResourceUsage cb = ResourceUsage::CS_Constants;
    ResourceUsage ro = ResourceUsage::CS_Resource;
    ResourceUsage rw = ResourceUsage::CS_RWResource;

    if(rootsig == &state.graphics)
    {
      if(p.ShaderVisibility == D3D12_SHADER_VISIBILITY_ALL)
      {
        cb = ResourceUsage::All_Constants;
        ro = ResourceUsage::All_Resource;
        rw = ResourceUsage::All_RWResource;
      }
      else
      {
        cb = CBUsage(p.ShaderVisibility - D3D12_SHADER_VISIBILITY_VERTEX);
        ro = ResUsage(p.ShaderVisibility - D3D12_SHADER_VISIBILITY_VERTEX);
        rw = RWResUsage(p.ShaderVisibility - D3D12_SHADER_VISIBILITY_VERTEX);
      }
    }

    if(p.ParameterType == D3D12_ROOT_PARAMETER_TYPE_CBV && el.type == eRootCBV &&
       type == D3D12_DESCRIPTOR_RANGE_TYPE_CBV && p.Descriptor.RegisterSpace == space &&
       p.Descriptor.ShaderRegister >= bind && p.Descriptor.ShaderRegister < bind + rangeSize)
    {
      AddResourceUsage(actionNode, el.id, eid, cb);

      // common case - root element matches 1:1 with a non-array shader bind, if so we can exit. If
      // not we might have to continue since other parts of it might be mapped to a table, or
      // another root element
      if(rangeSize == 1)
        return;
    }
    else if(p.ParameterType == D3D12_ROOT_PARAMETER_TYPE_SRV && el.type == eRootSRV &&
            type == D3D12_DESCRIPTOR_RANGE_TYPE_SRV && p.Descriptor.RegisterSpace == space &&
            p.Descriptor.ShaderRegister >= bind && p.Descriptor.ShaderRegister < bind + rangeSize)
    {
      AddResourceUsage(actionNode, el.id, eid, ro);

      // common case - root element matches 1:1 with a non-array shader bind, if so we can exit. If
      // not we might have to continue since other parts of it might be mapped to a table, or
      // another root element
      if(rangeSize == 1)
        return;
    }
    else if(p.ParameterType == D3D12_ROOT_PARAMETER_TYPE_UAV && el.type == eRootUAV &&
            type == D3D12_DESCRIPTOR_RANGE_TYPE_UAV && p.Descriptor.RegisterSpace == space &&
            p.Descriptor.ShaderRegister >= bind && p.Descriptor.ShaderRegister < bind + rangeSize)
    {
      AddResourceUsage(actionNode, el.id, eid, rw);

      // common case - root element matches 1:1 with a non-array shader bind, if so we can exit. If
      // not we might have to continue since other parts of it might be mapped to a table, or
      // another root element
      if(rangeSize == 1)
        return;
    }
    else if(p.ParameterType == D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE && el.type == eRootTable)
    {
      WrappedID3D12DescriptorHeap *heap =
          m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(el.id);

      if(heap == NULL)
        continue;

      UINT prevTableOffset = 0;

      for(size_t r = 0; r < p.ranges.size(); r++)
      {
        const D3D12_DESCRIPTOR_RANGE1 &range = p.ranges[r];

        UINT offset = range.OffsetInDescriptorsFromTableStart;

        if(range.OffsetInDescriptorsFromTableStart == D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND)
          offset = prevTableOffset;

        D3D12Descriptor *desc = (D3D12Descriptor *)heap->GetCPUDescriptorHandleForHeapStart().ptr;
        desc += el.offset;
        desc += offset;

        UINT num = range.NumDescriptors;

        if(num == UINT_MAX)
        {
          // find out how many descriptors are left after
          num = heap->GetNumDescriptors() - offset - UINT(el.offset);
        }

        prevTableOffset = offset + num;

        // skip ranges that aren't the type or register space we want
        if(range.RangeType != type || range.RegisterSpace != space)
          continue;

        // skip ranges that don't overlap with the registers we are looking for at all
        if(range.BaseShaderRegister + num <= bind || range.BaseShaderRegister >= bind + rangeSize)
          continue;

        if(num > 1000)
        {
          if(!hugeRangeWarned)
            RDCWARN("Skipping large, most likely 'bindless', descriptor range");
          hugeRangeWarned = true;

          continue;
        }

        bool allInRange = (bind >= range.BaseShaderRegister && rangeSize <= range.NumDescriptors);

        // move to the first descriptor in the range which is in the binding we want, if the binding
        // is later on in the range.
        //
        // It's also possible that the range is later on in the binding (e.g. if the binding is at
        // base register 5 and is 1000000 in length, the range could start at register 10. In that
        // case we just consume as much of the range as still fits in the bind
        if(bind > range.BaseShaderRegister)
          desc += (bind - range.BaseShaderRegister);
        if(range.BaseShaderRegister > bind)
          rangeSize -= (range.BaseShaderRegister - bind);

        if(range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_CBV)
        {
          EventUsage usage(eid, cb);

          for(UINT i = 0; i < num && i < rangeSize; i++)
          {
            ResourceId id = WrappedID3D12Resource::GetResIDFromAddr(desc->GetCBV().BufferLocation);

            AddResourceUsage(actionNode, id, eid, cb);

            desc++;
          }
        }
        else if(range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_SRV ||
                range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_UAV)
        {
          ResourceUsage usage = range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_SRV ? ro : rw;

          for(UINT i = 0; i < num && i < rangeSize; i++)
          {
            AddResourceUsage(actionNode, desc->GetResResourceId(), eid, usage);
            if(range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_UAV &&
               desc->GetCounterResourceId() != ResourceId())
              AddResourceUsage(actionNode, desc->GetCounterResourceId(), eid, usage);

            desc++;
          }
        }

        // if this descriptor range fully covered the binding (which may be quite common) we can
        // return now, other ranges/root elements won't overlap so don't bother looking at them
        if(allInRange)
          return;
      }
    }
  }
}

void D3D12CommandData::AddUsage(const D3D12RenderState &state, D3D12ActionTreeNode &actionNode)
{
  ActionDescription &a = actionNode.action;

  uint32_t eid = a.eventId;

  ActionFlags DrawMask = ActionFlags::Drawcall | ActionFlags::MeshDispatch | ActionFlags::Dispatch;
  if(!(a.flags & DrawMask))
    return;

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  const D3D12RenderState::RootSignature *rootsig = NULL;

  WrappedID3D12PipelineState *pipe = NULL;

  if(state.pipe != ResourceId())
    pipe = rm->GetResAs<WrappedID3D12PipelineState>(state.pipe);

  WrappedID3D12Shader *shaders[NumShaderStages] = {};
  const ShaderReflection *refls[NumShaderStages] = {};

  if((a.flags & ActionFlags::Dispatch) && pipe && pipe->IsCompute())
  {
    shaders[uint32_t(ShaderStage::Compute)] =
        (WrappedID3D12Shader *)pipe->compute->CS.pShaderBytecode;
  }
  else if(!(a.flags & ActionFlags::Dispatch) && pipe && pipe->IsGraphics())
  {
    D3D12_SHADER_BYTECODE *srcArr[] = {
        &pipe->graphics->VS,
        &pipe->graphics->HS,
        &pipe->graphics->DS,
        &pipe->graphics->GS,
        &pipe->graphics->PS,
        // compute
        NULL,
        &pipe->graphics->AS,
        &pipe->graphics->MS,
    };
    for(size_t stage = 0; stage < ARRAY_COUNT(srcArr); stage++)
    {
      if(srcArr[stage])
        shaders[stage] = (WrappedID3D12Shader *)srcArr[stage]->pShaderBytecode;
    }
  }

  // Keep shader use discovery on the existing draw/dispatch action-building path. The final
  // command-list merge retains only the first event per physical shader, so GetUsage(shaderId)
  // stays constant-sized and no EID traversal is necessary.
  for(size_t stage = 0; stage < ARRAY_COUNT(shaders); stage++)
  {
    if(shaders[stage])
      actionNode.resourceUsage.push_back(make_rdcpair(
          shaders[stage]->GetResourceID(), EventUsage(eid, ResourceUsage::Shader)));
  }

  if((a.flags & ActionFlags::Dispatch) && state.compute.rootsig != ResourceId())
  {
    rootsig = &state.compute;

    if(shaders[uint32_t(ShaderStage::Compute)])
      refls[uint32_t(ShaderStage::Compute)] =
          &shaders[uint32_t(ShaderStage::Compute)]->GetDetails();
  }
  else if(state.graphics.rootsig != ResourceId())
  {
    rootsig = &state.graphics;

    for(size_t stage = 0; stage < ARRAY_COUNT(shaders); stage++)
    {
      if(shaders[stage])
        refls[stage] = &shaders[stage]->GetDetails();
    }

    if(a.flags & ActionFlags::Indexed && state.ibuffer.buf != ResourceId())
      actionNode.resourceUsage.push_back(
          make_rdcpair(state.ibuffer.buf, EventUsage(eid, ResourceUsage::IndexBuffer)));

    if(a.flags & ActionFlags::Drawcall)
    {
      for(size_t i = 0; i < state.vbuffers.size(); i++)
      {
        if(state.vbuffers[i].buf != ResourceId())
          actionNode.resourceUsage.push_back(
              make_rdcpair(state.vbuffers[i].buf, EventUsage(eid, ResourceUsage::VertexBuffer)));
      }

      for(size_t i = 0; i < state.streamouts.size(); i++)
      {
        if(state.streamouts[i].buf != ResourceId())
          actionNode.resourceUsage.push_back(
              make_rdcpair(state.streamouts[i].buf, EventUsage(eid, ResourceUsage::StreamOut)));
        if(state.streamouts[i].countbuf != ResourceId())
          actionNode.resourceUsage.push_back(make_rdcpair(
              state.streamouts[i].countbuf, EventUsage(eid, ResourceUsage::StreamOut)));
      }
    }

    rdcarray<ResourceId> rts = state.GetRTVIDs();

    for(size_t i = 0; i < rts.size(); i++)
    {
      if(rts[i] != ResourceId())
        actionNode.resourceUsage.push_back(
            make_rdcpair(rts[i], EventUsage(eid, ResourceUsage::ColorTarget)));
    }

    ResourceId id = state.GetDSVID();
    if(id != ResourceId())
      actionNode.resourceUsage.push_back(
          make_rdcpair(id, EventUsage(eid, ResourceUsage::DepthStencilTarget)));
  }

  if(rootsig)
  {
    // iterate over each stage, looking at its used binds, then for each bind find it in the root
    // signature. We have to do this kind of N:N lookup because of D3D12's bad design, but this
    // should be a better way around to do it than iterating over the root signature and finding a
    // bind for each element
    for(size_t sh = 0; sh < ARRAY_COUNT(refls); sh++)
    {
      if(!refls[sh])
        continue;

      for(const ConstantBlock &b : refls[sh]->constantBlocks)
      {
        AddUsageForBindInRootSig(state, actionNode, rootsig, D3D12_DESCRIPTOR_RANGE_TYPE_CBV,
                                 b.fixedBindSetOrSpace, b.fixedBindNumber, b.bindArraySize);
      }

      for(const ShaderResource &r : refls[sh]->readOnlyResources)
      {
        AddUsageForBindInRootSig(state, actionNode, rootsig, D3D12_DESCRIPTOR_RANGE_TYPE_SRV,
                                 r.fixedBindSetOrSpace, r.fixedBindNumber, r.bindArraySize);
      }

      for(const ShaderResource &r : refls[sh]->readWriteResources)
      {
        AddUsageForBindInRootSig(state, actionNode, rootsig, D3D12_DESCRIPTOR_RANGE_TYPE_UAV,
                                 r.fixedBindSetOrSpace, r.fixedBindNumber, r.bindArraySize);
      }
    }
  }
}

void D3D12CommandData::AddAction(const ActionDescription &a)
{
  m_AddedAction = true;

  ActionDescription action = a;
  action.eventId = m_LastCmdListID != ResourceId() ? m_BakedCmdListInfo[m_LastCmdListID].curEventID
                                                   : m_RootEventID;
  action.actionId = m_LastCmdListID != ResourceId() ? m_BakedCmdListInfo[m_LastCmdListID].actionCount
                                                    : m_RootActionID;

  for(int i = 0; i < 8; i++)
    action.outputs[i] = ResourceId();

  action.depthOut = ResourceId();

  if(m_LastCmdListID != ResourceId())
  {
    rdcarray<ResourceId> rts = m_BakedCmdListInfo[m_LastCmdListID].state.GetRTVIDs();

    for(size_t i = 0; i < ARRAY_COUNT(action.outputs); i++)
    {
      if(i < rts.size())
        action.outputs[i] = rts[i];
      else
        action.outputs[i] = ResourceId();
    }

    action.depthOut = m_BakedCmdListInfo[m_LastCmdListID].state.GetDSVID();
  }

  // markers don't increment action ID
  ActionFlags MarkerMask = ActionFlags::SetMarker | ActionFlags::PushMarker |
                           ActionFlags::PopMarker | ActionFlags::PassBoundary;
  if(!(action.flags & MarkerMask))
  {
    if(m_LastCmdListID != ResourceId())
      m_BakedCmdListInfo[m_LastCmdListID].actionCount++;
    else
      m_RootActionID++;
  }

  action.events.swap(m_LastCmdListID != ResourceId() ? m_BakedCmdListInfo[m_LastCmdListID].curEvents
                                                     : m_RootEvents);

  // should have at least the root action here, push this action
  // onto the back's children list.
  if(!GetActionStack().empty())
  {
    D3D12ActionTreeNode node(action);

    node.resourceUsage.swap(m_BakedCmdListInfo[m_LastCmdListID].resourceUsage);

    if(m_LastCmdListID != ResourceId())
      AddUsage(m_BakedCmdListInfo[m_LastCmdListID].state, node);

    for(const ActionDescription &child : action.children)
      node.children.push_back(D3D12ActionTreeNode(child));
    GetActionStack().back()->children.push_back(node);
  }
  else
    RDCERR("Somehow lost action stack!");
}

void D3D12CommandData::InsertActionsAndRefreshIDs(ResourceId cmd, const BakedCmdListInfo &cmdListInfo)
{
  const rdcarray<D3D12ActionTreeNode> &cmdBufNodes = cmdListInfo.action->children;

  SDObject *localAnnotations = NULL;
  if(m_RootAnnotation)
    localAnnotations = m_RootAnnotation->Duplicate();

  size_t curAnnot = 0;

  // assign new action IDs
  for(size_t i = 0; i < cmdBufNodes.size(); i++)
  {
    D3D12ActionTreeNode n = cmdBufNodes[i];
    n.action.eventId += m_RootEventID;
    n.action.actionId += m_RootActionID;

    for(APIEvent &ev : n.action.events)
    {
      if(localAnnotations)
      {
        for(; curAnnot < cmdListInfo.annotations.size(); curAnnot++)
        {
          const PendingAnnotation &annot = cmdListInfo.annotations[curAnnot];
          if(annot.eventId == ev.eventId)
          {
            if(annot.valueType == eRENDERDOC_Empty)
              localAnnotations->EraseChildByKeyPath(annot.key);
            else
              WriteAnnotation(localAnnotations->CreateChildByKeyPath(annot.key), annot.valueType,
                              annot.valueVectorWidth, annot.value);
          }
          else if(annot.eventId > ev.eventId)
          {
            break;
          }
        }

        ev.annotations = localAnnotations->Duplicate();
        m_EventAnnotations.push_back(ev.annotations);
      }

      ev.eventId += m_RootEventID;
      m_Events.resize(ev.eventId + 1);
      m_Events[ev.eventId] = ev;
    }

    ActionUse use(m_Events.back().fileOffset, n.action.eventId, cmd, cmdBufNodes[i].action.eventId);

    // insert in sorted location
    auto drawit = std::lower_bound(m_ActionUses.begin(), m_ActionUses.end(), use);
    m_ActionUses.insert(drawit - m_ActionUses.begin(), use);

    RDCASSERT(n.children.empty());

    for(auto it = n.resourceUsage.begin(); it != n.resourceUsage.end(); ++it)
    {
      EventUsage u = it->second;
      u.eventId += m_RootEventID;
      rdcarray<EventUsage> &uses = m_ResourceUses[it->first];
      if(u.usage != ResourceUsage::Shader || uses.empty())
        uses.push_back(u);
    }

    GetActionStack().back()->children.push_back(n);

    // if this is a push marker too, step down the action stack
    if(cmdBufNodes[i].action.flags & ActionFlags::PushMarker)
      GetActionStack().push_back(&GetActionStack().back()->children.back());

    // similarly for a pop, but don't pop off the root
    if((cmdBufNodes[i].action.flags & ActionFlags::PopMarker) && GetActionStack().size() > 1)
      GetActionStack().pop_back();
  }
}
