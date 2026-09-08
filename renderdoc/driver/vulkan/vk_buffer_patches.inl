// Included in vk_core.cpp: native persistent uniform-buffer edits. All upload objects are replay
// owned; neither captured bytes nor the immutable initial contents are modified.

static bool VulkanReplayPatchUsageSupported(uint64_t usage)
{
  const uint64_t allowed = VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT |
                           VK_BUFFER_USAGE_TRANSFER_DST_BIT;
  return (usage & VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT) != 0 && (usage & ~allowed) == 0;
}

static bool VulkanReplayPatchBindingFits(uint64_t offset, uint64_t size, uint64_t allocationSize)
{
  return offset <= allocationSize && size <= allocationSize - offset;
}

static bool VulkanReplayPatchUniformInputs(const DescSetLayout &layout, const BindingStorage &storage,
                                           rdcarray<ResourceId> &inputs, rdcstr &reason)
{
  // Bound descriptors can be consumed by a replacement shader even if original reflection marked
  // them unused. Scan all elements (also mutable types), with a fail-closed work cap.
  if(storage.totalDescriptorCount() > 1024 * 1024 || layout.bindings.size() > 1024 * 1024)
  {
    reason = "descriptorEvidenceWorkLimit";
    return false;
  }
  std::set<ResourceId> unique;
  for(size_t b = 0; b < layout.bindings.size(); ++b)
  {
    const DescSetLayout::Binding &binding = layout.bindings[b];
    const uint32_t count = binding.GetDescriptorCount(storage.variableDescriptorCount);
    if(count == 0)
      continue;
    if(b >= storage.binds.size() || !storage.binds[b] ||
       binding.elemOffset > storage.totalDescriptorCount() ||
       count > storage.totalDescriptorCount() - binding.elemOffset)
    {
      reason = "incompleteDescriptorElementStorage";
      return false;
    }
    for(uint32_t element = 0; element < count; ++element)
    {
      const DescriptorSetSlot &slot = storage.binds[b][element];
      if((slot.type == DescriptorSlotType::UniformBuffer ||
          slot.type == DescriptorSlotType::UniformBufferDynamic) && slot.resource != ResourceId())
        unique.insert(slot.resource);
    }
  }
  for(ResourceId id : unique)
    inputs.push_back(id);
  return true;
}

void WrappedVulkan::RecordReplayPatchUniformInputs(VulkanActionTreeNode &action,
                                                  const VulkanStatePipeline &pipeline)
{
  for(const VulkanStatePipeline::DescriptorAndOffsets &bound : pipeline.descSets)
  {
    if(!bound.IsBound())
      continue;
    if(bound.descBufferIdx != ~0U || bound.descSet == ResourceId() || bound.push)
    {
      m_ReplayPatchReadEvidenceError = "descriptorBufferOrPushDescriptorEvidenceUnavailable";
      continue;
    }
    auto set = m_DescriptorSetState.find(bound.descSet);
    if(set == m_DescriptorSetState.end())
    {
      m_ReplayPatchReadEvidenceError = "missingBoundDescriptorSet";
      continue;
    }
    auto layout = m_CreationInfo.m_DescSetLayout.find(set->second.layout);
    if(layout == m_CreationInfo.m_DescSetLayout.end())
    {
      m_ReplayPatchReadEvidenceError = "missingBoundDescriptorLayout";
      continue;
    }
    VulkanReplayPatchUniformInputs(layout->second, set->second.data, action.replayPatchUniformInputs,
                                   m_ReplayPatchReadEvidenceError);
  }
}

static void VulkanReplayPatchRecordRead(std::map<ResourceId, uint32_t> &reads, ResourceId buffer,
                                       uint32_t submittedEvent)
{
  if(buffer == ResourceId() || submittedEvent == 0)
    return;
  uint32_t &first = reads[buffer];
  first = first ? RDCMIN(first, submittedEvent) : submittedEvent;
}

void WrappedVulkan::RecordReplayPatchSubmittedReads(const VulkanActionTreeNode &action)
{
  for(ResourceId buffer : action.replayPatchUniformInputs)
    VulkanReplayPatchRecordRead(m_ReplayPatchFirstReads, buffer, action.action.eventId);
  // A buffer-to-buffer or buffer-to-image copy propagates the edit before later shader consumers.
  if(m_CreationInfo.m_Buffer.find(action.action.copySource) != m_CreationInfo.m_Buffer.end())
    VulkanReplayPatchRecordRead(m_ReplayPatchFirstReads, action.action.copySource,
                                action.action.eventId);
}

static uint32_t VulkanReplayPatchValueCutoff(const rdcarray<ReplayBufferPatch> &previous,
                                           const rdcarray<ReplayBufferPatch> &next,
                                           const std::map<ResourceId, uint32_t> &firstReads,
                                           rdcstr &reason)
{
  if(next.empty() || previous.size() != next.size())
  {
    reason = "patchLayoutChanged";
    return 0;
  }
  uint32_t first = ~0U;
  for(size_t i = 0; i < next.size(); ++i)
  {
    if(previous[i].buffer != next[i].buffer || previous[i].byteOffset != next[i].byteOffset ||
       previous[i].data.size() != next[i].data.size())
    {
      reason = "patchLayoutChanged";
      return 0;
    }
    auto use = firstReads.find(next[i].buffer);
    if(use == firstReads.end() || use->second == 0)
    {
      reason = "missingPotentialUniformOrCopyRead";
      return 0;
    }
    first = RDCMIN(first, use->second);
  }
  reason = "sameRangesPotentialUniformAndCopyReads";
  return first;
}

bool WrappedVulkan::CanBuildReplayPatchAnchor(uint32_t target, rdcstr &reason)
{
  if(!m_ReplayPatchAnchorEvent || target <= m_ReplayPatchAnchorEvent)
  {
    reason = "targetBeforeFirstConsumer";
    return false;
  }
  bool sameCommandBuffer = false;
  for(const CommandBufferNode *node : m_Partial.commandTree)
  {
    auto baked = m_BakedCmdBufferInfo.find(node->cmdId);
    if(baked != m_BakedCmdBufferInfo.end() && node->childCmdNodes.empty() &&
       baked->second.level == VK_COMMAND_BUFFER_LEVEL_PRIMARY &&
       IsEventInCommandBuffer(node, m_ReplayPatchAnchorEvent, baked->second.eventCount) &&
       IsEventInCommandBuffer(node, target, baked->second.eventCount))
      sameCommandBuffer = true;
  }
  if(!sameCommandBuffer)
  {
    reason = "prefixAnchorRequiresSamePrimarySubmission";
    return false;
  }
  // Match the existing restore interval's native boundaries, including the final OnlyDraw. Never
  // create an expensive anchor which cannot resume this target across a pass/command boundary.
  for(uint32_t event = m_ReplayPatchAnchorEvent; event <= target; ++event)
  {
    const APIEvent &ev = GetEvent(event);
    if(ev.eventId != event || ev.chunkIndex >= m_StructuredFile->chunks.size())
    {
      reason = "prefixAnchorMissingNativeEvent";
      return false;
    }
    const rdcstr name = ToStr((VulkanChunk)m_StructuredFile->chunks[ev.chunkIndex]->metadata.chunkID);
    if(name.find("vkCmd") != 0 || name.find("Indirect") >= 0 ||
       name.find("ExecuteCommands") >= 0 || name.find("Debug") >= 0 || name.find("Marker") >= 0 ||
       (event > m_ReplayPatchAnchorEvent && (name.find("RenderPass") >= 0 ||
         name.find("Rendering") >= 0 || name.find("NextSubpass") >= 0)))
    {
      reason = "prefixAnchorCrossesUnsupportedNativeBoundary:" + name;
      return false;
    }
  }
  reason = "samePrimarySubmissionAndPass";
  return true;
}

static ReplayBufferPatch VulkanReplayPatchInMemory(const ReplayBufferPatch &patch, ResourceId memory,
                                                   uint64_t bindingOffset)
{
  ReplayBufferPatch mapped = patch;
  mapped.buffer = memory;
  mapped.byteOffset += bindingOffset;
  return mapped;
}

static bool VulkanReplayPatchUnsafeMemoryMetadata(const SDObject &object, rdcstr *reason = NULL)
{
  const rdcstr typeName(object.type.name.c_str());
  // An SD array retains its element type name, but owns element children rather than struct
  // members. Inspect those elements; treating the array itself as a barrier rejects valid input.
  if(!object.IsArray() && object.type.basetype != SDBasic::Null)
  {
    if(typeName.contains("DeviceGroup") || typeName.contains("ImportMemory"))
    {
      if(reason)
        *reason = typeName + ":unsupportedMemoryMetadata";
      return true;
    }
    if(typeName.contains("BufferMemoryBarrier"))
    {
      const SDObject *src = object.FindChild("srcQueueFamilyIndex");
      const SDObject *dst = object.FindChild("dstQueueFamilyIndex");
      if(!src || !dst || src->AsUInt64() != dst->AsUInt64())
      {
        if(reason)
          *reason = StringFormat::Fmt("%s:%s src=%llu dst=%llu", typeName.c_str(),
                                     !src || !dst ? "missingQueueFamily" : "ownershipTransfer",
                                     (unsigned long long)(src ? src->AsUInt64() : ~0ULL),
                                     (unsigned long long)(dst ? dst->AsUInt64() : ~0ULL));
        return true;
      }
    }
  }
  for(size_t i = 0; i < object.NumChildren(); ++i)
    if(VulkanReplayPatchUnsafeMemoryMetadata(*object.GetChild(i), reason))
      return true;
  return false;
}

void WrappedVulkan::DestroyReplayBufferPatches()
{
  for(ReplayPatchUpload *patch : m_ReplayBufferPatches)
  {
    patch->upload.Destroy();
    delete patch;
  }
  m_ReplayBufferPatches.clear();
  m_ReplayBufferMemoryPatches.clear();
}

rdcstr WrappedVulkan::SetReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches)
{
  rdcstr error = ValidateReplayBufferPatches(patches);
  if(!error.empty())
    return error;
  if(patches.empty() && m_ReplayBufferPatches.empty())
    return rdcstr();

  // No ownership inference from GetUsage: descriptor indexing or a replacement shader can change
  // accesses. The first supported class is ordinary uniform/transfer storage on the replay queue.
  if(!patches.empty())
  {
    if(!m_ReplayPatchCaptureChecked)
    {
      m_ReplayPatchCaptureChecked = true;
      m_ReplayPatchCaptureError = [this]() -> rdcstr {
        if(!m_StructuredFile || m_Partial.commandTree.empty())
          return "Vulkan buffer patches require structured commands and replay queue evidence";
        if(APIProps.SparseResources)
          return "Vulkan buffer patches cannot prove allocation aliases in sparse captures";
        for(const auto &queue : m_commandQueueFamilies)
          if(!ResourceIDGen::IsReplayOnlyID(queue.first) && queue.second != m_QueueFamilyIdx)
            return "Vulkan buffer patches currently require a single replay queue family";
        for(const SDChunk *chunk : m_StructuredFile->chunks)
        {
          if(chunk->metadata.chunkID < (uint32_t)SystemChunk::FirstDriverChunk)
            continue;
          const rdcstr name = ToStr(VulkanChunk(chunk->metadata.chunkID));
          rdcstr metadataReason;
          if((name == "vkAllocateMemory" || name == "vkCreateDevice" ||
              name == "vkBindBufferMemory2" || name.contains("PipelineBarrier") ||
              name.contains("WaitEvents")) &&
             VulkanReplayPatchUnsafeMemoryMetadata(*chunk, &metadataReason))
            return "Vulkan buffer patches do not support buffer ownership transfers, imported memory "
                   "or device-group bindings: " + name + ":" + metadataReason;
          if(name.contains("ImportMemory") || name.contains("Video") ||
             name.contains("WriteBufferMarker") || name == "vkCmdSetDeviceMask")
            return "Vulkan buffer patches do not support imported/video memory, device masks or "
                   "in-pass buffer marker writes: " + name;
          if(name == "vkCmdBeginRendering")
          {
            const SDObject *info = chunk->FindChild("pRenderingInfo");
            const SDObject *flags = info ? info->FindChild("flags") : NULL;
            if(!flags || (flags->AsUInt64() &
                          (VK_RENDERING_SUSPENDING_BIT | VK_RENDERING_RESUMING_BIT)) != 0)
              return "Vulkan buffer patches cannot insert transfers between suspended rendering instances";
          }
        }
        return rdcstr();
      }();
    }
    if(!m_ReplayPatchCaptureError.empty())
      return m_ReplayPatchCaptureError;
  }

  rdcarray<ReplayBufferPatch> memoryPatches;
  uint64_t total = 0;
  for(const ReplayBufferPatch &patch : patches)
  {
    VulkanResourceManager *rm = GetResourceManager();
    auto buffer = m_CreationInfo.m_Buffer.find(patch.buffer);
    if(buffer == m_CreationInfo.m_Buffer.end() || !rm->HasResource(patch.buffer) ||
       ResourceIDGen::IsReplayOnlyID(patch.buffer) || rm->HasReplacement(patch.buffer))
      return "Vulkan buffer patches require an unreplaced captured buffer";
    const VulkanCreationInfo::Buffer &info = buffer->second;
    if(!VulkanReplayPatchUsageSupported(info.usage) || info.external || info.sparse ||
       info.replayPatchProtected)
      return "Vulkan persistent patches currently require unprotected uniform/transfer buffers "
             "without storage, device-address, sparse or external use";
    if(patch.byteOffset > info.size || patch.data.size() > info.size - patch.byteOffset)
      return "Vulkan patch is outside the captured buffer";
    auto memory = m_CreationInfo.m_Memory.find(info.replayMemory);
    if(info.replayMemory == ResourceId() || memory == m_CreationInfo.m_Memory.end() ||
       memory->second.wholeMemBuf == VK_NULL_HANDLE ||
       !VulkanReplayPatchBindingFits(info.replayMemoryOffset, info.size,
                                      memory->second.wholeMemBufSize) ||
       !VulkanReplayPatchBindingFits(info.replayMemoryOffset, info.replayMemorySize,
                                      memory->second.allocSize))
      return "Vulkan buffer patch has no restorable linear memory binding";
    const VkInitialContents initial = rm->GetInitialContents(info.replayMemory);
    if(initial.type != eResDeviceMemory ||
       (initial.buf != VK_NULL_HANDLE && initial.mem.size < info.replayMemoryOffset + info.size))
      return "Vulkan buffer patch requires captured initial memory contents or canonical clear state";
    for(const auto &other : m_CreationInfo.m_Buffer)
    {
      const VulkanCreationInfo::Buffer &alias = other.second;
      if(other.first != patch.buffer && !ResourceIDGen::IsReplayOnlyID(other.first) &&
         alias.replayMemory == info.replayMemory &&
         alias.replayMemoryOffset < info.replayMemoryOffset + info.replayMemorySize &&
         info.replayMemoryOffset < alias.replayMemoryOffset + alias.replayMemorySize)
        return "Vulkan persistent buffer patches do not support overlapping buffer memory bindings";
    }
    // The clear path restores linear memory, so reject image allocations even when an image's
    // current usage happens not to reference the overlapping memory interval.
    for(const auto &image : m_ImageStates)
    {
      LockedConstImageStateRef state = FindConstImageState(image.first);
      if(state && state->boundMemory == info.replayMemory)
        return "Vulkan buffer patches do not support memory allocations also bound to images";
    }
    memoryPatches.push_back(VulkanReplayPatchInMemory(patch, info.replayMemory, info.replayMemoryOffset));
    total += patch.data.size();
    if(total > 128ULL * 1024 * 1024)
      return "Vulkan persistent buffer patch upload exceeds 128 MiB";
  }

  rdcarray<ReplayPatchUpload *> uploads;
  for(const ReplayBufferPatch &patch : patches)
  {
    ReplayPatchUpload *upload = new ReplayPatchUpload;
    upload->patch = patch;
    upload->upload.Create(this, GetDev(), patch.data.size(), 1, 0);
    void *data = upload->upload.UnwrappedMemory() != VK_NULL_HANDLE ? upload->upload.Map() : NULL;
    if(data)
    {
      memcpy(data, patch.data.data(), patch.data.size());
      upload->upload.Unmap();
    }
    uploads.push_back(upload);
    if(!data || HasFatalError())
    {
      for(ReplayPatchUpload *pending : uploads)
      {
        pending->upload.Destroy();
        delete pending;
      }
      return "Vulkan persistent buffer patch upload allocation failed";
    }
  }

  // Existing native command buffers can refer to the previous generation's upload buffers. Drain
  // them before releasing that generation. The controller follows a successful commit with replay.
  SubmitCmds();
  FlushQ();
  if(HasFatalError())
  {
    for(ReplayPatchUpload *pending : uploads)
    {
      pending->upload.Destroy();
      delete pending;
    }
    return "Vulkan replay failed while draining the previous buffer patch generation";
  }
  rdcarray<ReplayBufferPatch> previous;
  for(const ReplayPatchUpload *patch : m_ReplayBufferPatches)
    previous.push_back(patch->patch);
  rdcstr cutoffReason, anchorReason;
  uint32_t cutoff = VulkanReplayPatchValueCutoff(previous, patches, m_ReplayPatchFirstReads,
                                                cutoffReason);
  uint32_t firstRead = VulkanReplayPatchValueCutoff(patches, patches, m_ReplayPatchFirstReads,
                                                   anchorReason);
  // The checkpoint capture envelope excludes frame descriptor mutations/secondary submissions.
  // Within it, recorded potential bindings remain valid for every submission and replacement
  // shader. Outside it the persistent edit still works, but no prefix preservation is claimed.
  if(!m_CheckpointCaptureSafe || !m_ReplayPatchReadEvidenceError.empty())
  {
    cutoffReason = m_ReplayPatchReadEvidenceError.empty() ? "checkpointCaptureEvidenceUnsupported"
                                                        : m_ReplayPatchReadEvidenceError;
    cutoff = firstRead = 0;
  }
  m_ReplayPatchFirstAffectedEvent = cutoff;
  m_ReplayPatchFirstConsumer = firstRead;
  const uint32_t anchor = firstRead > 1 ? firstRead - 1 : 0;
  if(!cutoff || anchor != m_ReplayPatchAnchorEvent)
    m_ReplayPatchAnchorAttempted = false;
  m_ReplayPatchAnchorEvent = anchor;
  InvalidateReplayCheckpoints(cutoff);
  for(const ReplayBufferPatch &patch : m_ReplayBufferMemoryPatches)
    m_ReplayPatchRestoreMemory.insert(patch.buffer);
  for(const ReplayBufferPatch &patch : memoryPatches)
    m_ReplayPatchRestoreMemory.insert(patch.buffer);
  DestroyReplayBufferPatches();
  m_ReplayBufferPatches.swap(uploads);
  m_ReplayBufferMemoryPatches.swap(memoryPatches);
  RDCLOG("Vulkan shared buffer patch commit ranges=%zu firstAffected=%u anchor=%u reason=%s",
         patches.size(), cutoff, anchor, cutoffReason.c_str());
  return rdcstr();
}

void WrappedVulkan::ApplyReplayBufferMemoryPatches(VkDeviceMemory memory, uint64_t offset, void *data,
                                                   uint64_t size)
{
  if(IsActiveReplaying(m_State))
    ::ApplyReplayBufferPatches(m_ReplayBufferMemoryPatches, GetResID(memory), offset, data, size);
}

void WrappedVulkan::ApplyReplayBufferPatches(VkCommandBuffer cmd)
{
  if(m_ReplayBufferPatches.empty() || cmd == VK_NULL_HANDLE)
    return;
  // Only called outside render passes, after initial restoration or a captured transfer write,
  // and in the OnlyDraw prelude before resuming a pass. Arbitrary byte ranges use CopyBuffer;
  // UpdateBuffer's four-byte alignment and 64 KiB restriction do not limit the public edit API.
  VkMemoryBarrier barrier = {VK_STRUCTURE_TYPE_MEMORY_BARRIER, NULL,
                            VK_ACCESS_ALL_WRITE_BITS | VK_ACCESS_ALL_READ_BITS,
                            VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT};
  DoPipelineBarrier(cmd, 1, &barrier);
  for(ReplayPatchUpload *patch : m_ReplayBufferPatches)
  {
    VkBuffer target = GetResourceManager()->GetHandle<VkBuffer>(patch->patch.buffer);
    VkBufferCopy copy = {0, patch->patch.byteOffset, patch->patch.data.size()};
    ObjDisp(cmd)->CmdCopyBuffer(Unwrap(cmd), patch->upload.UnwrappedBuffer(), Unwrap(target), 1,
                                &copy);
  }
  barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
  barrier.dstAccessMask = VK_ACCESS_ALL_READ_BITS | VK_ACCESS_ALL_WRITE_BITS;
  DoPipelineBarrier(cmd, 1, &barrier);
}
