/******************************************************************************
 * Native Vulkan replay checkpoints. Included by vk_core.cpp so these methods use
 * the same driver implementation/PCH and do not introduce a separate build target.
 ******************************************************************************/

#include <set>

static void LogVulkanCheckpoint(const rdcstr &line)
{
  RDCLOG("[EIDTiming] %s", line.c_str());
  AppendReplayTimingLog(line);
}

static bool VulkanCheckpointWrite(ResourceUsage usage)
{
  return (usage >= ResourceUsage::VS_RWResource && usage <= ResourceUsage::All_RWResource) ||
         usage == ResourceUsage::CopyDst || usage == ResourceUsage::Copy ||
         usage == ResourceUsage::ResolveDst || usage == ResourceUsage::Resolve ||
         usage == ResourceUsage::GenMips || usage == ResourceUsage::Clear ||
         usage == ResourceUsage::Discard || usage == ResourceUsage::CPUWrite ||
         usage == ResourceUsage::ColorTarget || usage == ResourceUsage::DepthStencilTarget ||
         usage == ResourceUsage::StreamOut;
}

enum class VulkanCheckpointSubmissionRoute
{
  Execute,
  SkipPrefix,
  RestoreBoundary,
  RejectMissingBoundary,
};

static VulkanCheckpointSubmissionRoute VulkanCheckpointRouteSubmission(bool failed, bool pending,
                                                                      uint32_t savedEnd,
                                                                      uint32_t submittedEnd)
{
  if(failed)
    return VulkanCheckpointSubmissionRoute::RejectMissingBoundary;
  if(!pending)
    return VulkanCheckpointSubmissionRoute::Execute;
  if(submittedEnd < savedEnd)
    return VulkanCheckpointSubmissionRoute::SkipPrefix;
  if(submittedEnd == savedEnd)
    return VulkanCheckpointSubmissionRoute::RestoreBoundary;
  return VulkanCheckpointSubmissionRoute::RejectMissingBoundary;
}

static bool VulkanCheckpointSubmissionEvidenceSafe(bool captureSafe,
                                                  const std::set<ResourceId> &queues,
                                                  const std::set<uint32_t> &ends)
{
  return captureSafe && queues.size() == 1 && queues.count(ResourceId()) == 0 && !ends.empty();
}

bool WrappedVulkan::CheckReplayCheckpointCapture(rdcstr &reason)
{
  if(!m_StructuredFile || m_Partial.commandTree.empty() || DescriptorBuffers())
  {
    reason = "missingCommandTreeOrDescriptorBuffers";
    return false;
  }
  for(const CommandBufferNode *node : m_Partial.commandTree)
  {
    if(!node->childCmdNodes.empty() || FindCommandQueueFamily(node->cmdId) != m_QueueFamilyIdx)
    {
      reason = "secondaryCommandsOrOtherQueueFamily";
      return false;
    }
  }

  // Descriptor contents, queries, events and acceleration structures are not byte-addressable
  // resource state. Do not silently omit them from a checkpoint. Only inspect the frame section;
  // initial descriptor creation/update is immutable and remains shared with canonical replay.
  bool frame = false;
  for(const SDChunk *chunk : m_StructuredFile->chunks)
  {
    if(chunk->metadata.chunkID == (uint32_t)SystemChunk::CaptureBegin)
      frame = true;
    if(!frame || chunk->metadata.chunkID < (uint32_t)SystemChunk::FirstDriverChunk)
      continue;
    const rdcstr name = ToStr((VulkanChunk)chunk->metadata.chunkID);
    if(name == "vkCmdBindDescriptorSets")
      continue;
    const char *unsupported[] = {"Descriptor", "Query", "Timestamp", "AccelerationStructure",
                                 "TraceRays", "ExecuteCommands", "Indirect", "Conditional",
                                 "TransformFeedback", "Event", "Sparse", "BindMemory",
                                 "BindBufferMemory", "BindImageMemory", "Create", "Destroy"};
    for(const char *part : unsupported)
      if(name.find(part) >= 0)
      {
        reason = "unsupportedFrameCommand:" + name;
        return false;
      }
  }
  if(!frame)
  {
    reason = "missingFrameChunkBoundary";
    return false;
  }
  reason = "primaryCommandBufferNativeResources";
  return true;
}

bool WrappedVulkan::CheckReplayCheckpointResourceReplacements()
{
  // Shader replacements are supported through the normal replay state rebind. Resource
  // replacements can change both allocation identity and copy compatibility, so reject them
  // until their mutable-resource dependency closure has its own checkpoint representation.
  for(const auto &buffer : m_CreationInfo.m_Buffer)
    if(!ResourceIDGen::IsReplayOnlyID(buffer.first) &&
       GetResourceManager()->HasReplacement(buffer.first))
      return false;
  for(const auto &image : m_ImageStates)
    if(!ResourceIDGen::IsReplayOnlyID(image.first) &&
       GetResourceManager()->HasReplacement(image.first))
      return false;
  return true;
}

static uint32_t VulkanCheckpointCandidateSubmission(uint32_t candidate,
                                                     const std::set<uint32_t> &ends)
{
  auto boundary = ends.upper_bound(candidate);
  return boundary == ends.begin() ? 0 : *--boundary;
}

static bool VulkanCheckpointMatchesPatchAnchor(uint32_t eventId, uint32_t partialAnchor,
                                                uint32_t upstream, bool partial, bool initial)
{
  if(partial)
    return partialAnchor != 0 && eventId == partialAnchor;
  if(initial)
    return upstream == 0 && eventId == 0;
  return upstream != 0 && eventId == upstream;
}

bool WrappedVulkan::ReplayCheckpointIsPatchAnchor(uint32_t eventId,
                                                  ReplayCheckpointKind kind) const
{
  // Parameter editing owns a separate pre-consumer anchor, including the real initial state
  // when its first consumer is in the first submission. Ordinary candidate changes retain it.
  if(!m_ReplayBufferPatches.empty() && m_ReplayPatchFirstConsumer > 0)
  {
    const uint32_t upstream = VulkanCheckpointCandidateSubmission(m_ReplayPatchFirstConsumer - 1,
                                                                  m_CheckpointSubmissionEnds);
    if(VulkanCheckpointMatchesPatchAnchor(eventId, m_ReplayPatchAnchorEvent, upstream,
                                            kind == ReplayCheckpointKind::PartialCommand,
                                            kind == ReplayCheckpointKind::FrameInitial))
      return true;
  }
  return false;
}

bool WrappedVulkan::ReplayCheckpointCandidateSelected(uint32_t eventId,
                                                       ReplayCheckpointKind kind) const
{
  if(ReplayCheckpointIsPatchAnchor(eventId, kind))
    return true;
  for(const ReplayCheckpointCandidate &candidate : m_CheckpointCandidates)
  {
    const uint32_t submission =
        VulkanCheckpointCandidateSubmission(candidate.eventId, m_CheckpointSubmissionEnds);
    if(kind == ReplayCheckpointKind::CompletedSubmission && submission && eventId == submission)
      return true;
    if(kind == ReplayCheckpointKind::FrameInitial && submission == 0)
      return true;
    if(kind == ReplayCheckpointKind::PartialCommand && eventId == candidate.eventId)
      return true;
  }
  return false;
}

void WrappedVulkan::ConfigureReplayCheckpoints(
    const ReplayCheckpointConfig &config, const rdcarray<ReplayCheckpointCandidate> &candidates)
{
  const bool retain = m_CheckpointStats.enabled &&
                      ReplayCheckpointStorageCompatible(m_CheckpointConfig, config);
  if(!retain)
    InvalidateReplayCheckpoints();
  m_CheckpointConfig = config;
  m_CheckpointCandidates = candidates;
  if(retain)
  {
    bool drained = false;
    for(size_t i = m_Checkpoints.size(); i > 0; --i)
    {
      ReplayCheckpoint *checkpoint = m_Checkpoints[i - 1];
      if(ReplayCheckpointCandidateSelected(checkpoint->eventId, checkpoint->kind))
        continue;
      if(!drained)
      {
        SubmitCmds();
        FlushQ();
        drained = true;
      }
      DestroyReplayCheckpoint(checkpoint);
      m_Checkpoints.erase(i - 1);
    }
  }
  m_CheckpointStats.enabled = config.enabled;
  m_CheckpointStats.supported = true;
  m_CheckpointCaptureSafe = CheckReplayCheckpointCapture(m_CheckpointStats.reason);
  m_CheckpointSubmissionCaptureSafe = VulkanCheckpointSubmissionEvidenceSafe(
      m_CheckpointCaptureSafe, m_CheckpointSubmittedQueues, m_CheckpointSubmissionEnds);
  if(!config.enabled)
    m_CheckpointStats.reason = "disabled";
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    LogVulkanCheckpoint(StringFormat::Fmt(
        "api=VulkanCheckpoint op=configure enabled=%u budget=%llu maxCheckpoints=%u "
        "captureSafe=%u submissionSafe=%u queues=%zu boundaries=%zu reason=%s", config.enabled ? 1U : 0U,
        (unsigned long long)config.byteBudget, config.maxCheckpoints,
        m_CheckpointCaptureSafe ? 1U : 0U, m_CheckpointSubmissionCaptureSafe ? 1U : 0U,
        m_CheckpointSubmittedQueues.size(), m_CheckpointSubmissionEnds.size(),
        m_CheckpointStats.reason.c_str()));
}

void WrappedVulkan::DestroyReplayCheckpoint(ReplayCheckpoint *checkpoint)
{
  const VkDevDispatchTable *vt = ObjDisp(GetDev());
  for(ReplayCheckpointResource &resource : checkpoint->resources)
  {
    if(resource.buffer != VK_NULL_HANDLE)
      vt->DestroyBuffer(Unwrap(GetDev()), resource.buffer, NULL);
    if(resource.image != VK_NULL_HANDLE)
      vt->DestroyImage(Unwrap(GetDev()), resource.image, NULL);
    if(resource.memory != VK_NULL_HANDLE)
      vt->FreeMemory(Unwrap(GetDev()), resource.memory, NULL);
  }
  delete checkpoint;
}

void WrappedVulkan::InvalidateReplayCheckpoints(uint32_t firstAffectedEvent)
{
  if(firstAffectedEvent == 0)
    m_ReplayPatchAnchorAttempted = false;
  bool drained = false;
  for(size_t i = m_Checkpoints.size(); i > 0; --i)
  {
    ReplayCheckpoint *checkpoint = m_Checkpoints[i - 1];
    if(firstAffectedEvent && checkpoint->eventId < firstAffectedEvent)
      continue;
    if(!drained)
    {
      SubmitCmds();
      FlushQ();
      drained = true;
    }
    DestroyReplayCheckpoint(checkpoint);
    m_Checkpoints.erase(i - 1);
  }
  ++m_CheckpointStats.invalidations;
}

ReplayCheckpointStats WrappedVulkan::GetReplayCheckpointStats()
{
  ReplayCheckpointStats ret = m_CheckpointStats;
  ret.supported = true;
  ret.enabled = m_CheckpointConfig.enabled;
  ret.checkpointCount = (uint32_t)m_Checkpoints.size();
  ret.hasInitialCheckpoint = false;
  ret.allocatedBytes = ret.sharedBytes = 0;
  ret.events.clear();
  std::set<ResourceId> shared;
  for(const ReplayCheckpoint *checkpoint : m_Checkpoints)
  {
    ret.hasInitialCheckpoint |= checkpoint->kind == ReplayCheckpointKind::FrameInitial;
    ret.events.push_back(checkpoint->eventId);
    for(const ReplayCheckpointResource &resource : checkpoint->resources)
      if(resource.shared)
      {
        if(shared.insert(resource.source).second)
          ret.sharedBytes += resource.copyBytes;
      }
      else
        ret.allocatedBytes += resource.bytes;
  }
  std::sort(ret.events.begin(), ret.events.end());
  return ret;
}

bool WrappedVulkan::CheckReplayCheckpointInterval(const ReplayCheckpoint &checkpoint,
                                                 uint32_t target)
{
  if(checkpoint.kind != ReplayCheckpointKind::PartialCommand)
    return m_CheckpointSubmissionCaptureSafe && target >= checkpoint.eventId &&
           (checkpoint.kind == ReplayCheckpointKind::FrameInitial ||
            m_CheckpointSubmissionEnds.count(checkpoint.eventId) != 0);
  if(checkpoint.partialStack.size() != 1 || target < checkpoint.eventId)
    return false;
  const CommandBufferNode &node = checkpoint.partialStack[0];
  auto baked = m_BakedCmdBufferInfo.find(node.cmdId);
  if(baked == m_BakedCmdBufferInfo.end() || !node.childCmdNodes.empty() ||
     baked->second.level != VK_COMMAND_BUFFER_LEVEL_PRIMARY ||
     !GetResourceManager()->HasResource(node.cmdId) || checkpoint.pushCommandBuffer == ResourceId() ||
     !IsEventInCommandBuffer(&node, target, baked->second.eventCount))
    return false;

  // The existing outside-command-buffer path restores a LOAD renderpass, but its subpass and
  // end-pass bookkeeping is for a single item. Stay within the saved pass (or outside any pass).
  // Exact API-event identity also excludes synthetic indirect subcommands and skipped markers.
  for(uint32_t event = checkpoint.eventId + 1; event <= target; ++event)
  {
    const APIEvent &ev = GetEvent(event);
    if(ev.eventId != event || ev.chunkIndex >= m_StructuredFile->chunks.size())
      return false;
    const rdcstr name = ToStr((VulkanChunk)m_StructuredFile->chunks[ev.chunkIndex]->metadata.chunkID);
    if(name.find("vkCmd") != 0 || name.find("RenderPass") >= 0 ||
       name.find("Rendering") >= 0 || name.find("NextSubpass") >= 0 ||
       name.find("Indirect") >= 0 || name.find("ExecuteCommands") >= 0 ||
       name.find("Debug") >= 0 || name.find("Marker") >= 0)
      return false;
  }
  return true;
}

uint32_t WrappedVulkan::RestoreReplayCheckpoint(uint32_t endEventID, ReplayLogType replayType)
{
  if(!m_CheckpointConfig.enabled || !m_CheckpointCaptureSafe ||
     m_Replay->m_ReplayCheckpointBypass || replayType == eReplay_OnlyDraw || m_ActionCallback)
    return 0;
  if(!CheckReplayCheckpointResourceReplacements())
  {
    ++m_CheckpointStats.rejected;
    m_CheckpointStats.reason = "bufferOrImageReplacement";
    return 0;
  }
  uint32_t target = replayType == eReplay_WithoutDraw ? RDCMAX(1U, endEventID) - 1 : endEventID;
  ReplayCheckpoint *best = NULL;
  for(ReplayCheckpoint *checkpoint : m_Checkpoints)
    if((!best || checkpoint->eventId > best->eventId) &&
       CheckReplayCheckpointInterval(*checkpoint, target))
      best = checkpoint;
  if(!best)
  {
    ++m_CheckpointStats.misses;
    return 0;
  }
  PerformanceTimer timer;
  if(best->kind == ReplayCheckpointKind::CompletedSubmission)
  {
    // Rebuild native commands in file order without executing the already-saved submissions.
    // Restore at their exact completed boundary so captured host writes before it cannot clobber
    // the saved resources. Only then may suffix submissions execute.
    m_CheckpointFileOrderReplay = true;
    m_CheckpointFileOrderRestore = best;
    return 1;
  }
  if(!CopyReplayCheckpointResources(*best, true))
  {
    ++m_CheckpointStats.rejected;
    m_CheckpointStats.reason = "restoreResourceIdentityOrCopyFailed";
    return 0;
  }
  if(best->kind == ReplayCheckpointKind::FrameInitial)
  {
    m_CheckpointFileOrderReplay = true;
    best->serial = ++m_CheckpointSerial;
    ++m_CheckpointStats.hits;
    m_CheckpointStats.lastResumeEvent = 0;
    m_CheckpointStats.reason = "restoredFrameInitialNativeResources";
    if(IsReplayTimingActive())
      LogVulkanCheckpoint(StringFormat::Fmt(
          "api=VulkanCheckpoint op=restore kind=FrameInitial event=0 target=%u nextEvent=1 total_ms=%.3f",
          target, timer.GetMilliseconds()));
    return 1;
  }
  // Copy/drain executes deferred cleanup from previous replays. The saved CPU cursor alone does
  // not keep a baked command wrapper alive, so re-check before changing its replacement mapping.
  // On failure the caller runs ApplyInitialContents and the full canonical command traversal.
  if(!GetResourceManager()->HasResource(best->partialStack[0].cmdId))
  {
    ++m_CheckpointStats.rejected;
    m_CheckpointStats.reason = "bakedCommandIdentityRetired";
    return 0;
  }

  m_RenderState = best->renderState;
  m_Partial.partialStack = best->partialStack;
  m_LastCmdBufferID = best->lastCmdBuffer;
  m_PushCommandBuffer = best->pushCommandBuffer;
  // Full replays of another submission may have changed the original -> baked ID mapping.
  // The resumed serialiser must resolve commands to this execution's CPU-state record.
  if(GetResourceManager()->HasReplacement(best->pushCommandBuffer))
    GetResourceManager()->RemoveReplacement(best->pushCommandBuffer);
  GetResourceManager()->ReplaceResource(best->pushCommandBuffer, best->partialStack[0].cmdId);
  BakedCmdBufferInfo &baked = m_BakedCmdBufferInfo[best->partialStack[0].cmdId];
  baked.state = best->renderState;
  baked.imageStates = best->cmdImageStates;
  baked.curEventID = best->curEventID;
  baked.activeSubpass = best->activeSubpass;
  baked.renderPassOpen = best->renderPassOpen;
  baked.endBarriers = best->endBarriers;
  best->serial = ++m_CheckpointSerial;
  ++m_CheckpointStats.hits;
  m_CheckpointStats.lastResumeEvent = best->eventId;
  m_CheckpointStats.reason = "restoredPrimaryCommandBufferPrefix";
  if(IsReplayTimingActive())
    LogVulkanCheckpoint(StringFormat::Fmt(
        "api=VulkanCheckpoint op=restore event=%u target=%u nextEvent=%u total_ms=%.3f",
        best->eventId, target, best->eventId + 1, timer.GetMilliseconds()));
  return best->eventId + 1;
}

bool WrappedVulkan::ReplayCheckpointSubmissionSkipped(uint32_t endEventID)
{
  ReplayCheckpoint *checkpoint = m_CheckpointFileOrderRestore;
  const VulkanCheckpointSubmissionRoute route = VulkanCheckpointRouteSubmission(
      m_CheckpointFileOrderFailed, checkpoint != NULL, checkpoint ? checkpoint->eventId : 0,
      endEventID);
  if(route == VulkanCheckpointSubmissionRoute::Execute)
    return false;
  if(route == VulkanCheckpointSubmissionRoute::RejectMissingBoundary)
  {
    // The saved boundary was not encountered exactly. Do not execute on an unrestored state.
    m_CheckpointFileOrderFailed = true;
    m_CheckpointStats.reason = "missingExactCompletedSubmissionBoundary";
    return true;
  }
  ++m_CheckpointSkippedSubmissions;
  if(route == VulkanCheckpointSubmissionRoute::RestoreBoundary)
  {
    PerformanceTimer timer;
    m_CheckpointFileOrderRestore = NULL;
    if(!CopyReplayCheckpointResources(*checkpoint, true))
    {
      m_CheckpointFileOrderFailed = true;
      ++m_CheckpointStats.rejected;
      m_CheckpointStats.reason = "completedSubmissionRestoreFailed";
      return true;
    }
    checkpoint->serial = ++m_CheckpointSerial;
    ++m_CheckpointStats.hits;
    m_CheckpointStats.lastResumeEvent = checkpoint->eventId;
    m_CheckpointStats.reason = "restoredCompletedSubmissionNativeResources";
    if(IsReplayTimingActive())
      LogVulkanCheckpoint(StringFormat::Fmt(
          "api=VulkanCheckpoint op=restore kind=CompletedSubmission event=%u target=%u "
          "skippedSubmissions=%u total_ms=%.3f", checkpoint->eventId, m_LastEventID,
          m_CheckpointSkippedSubmissions, timer.GetMilliseconds()));
  }
  return true;
}

bool WrappedVulkan::ReplayCheckpointIsProtected(const ReplayCheckpoint &checkpoint,
                                               const ReplayCheckpoint &building) const
{
  if(&checkpoint == m_CheckpointFileOrderRestore)
    return true;
  if(m_ReplayBufferPatches.empty())
    return false;
  if(checkpoint.kind == ReplayCheckpointKind::PartialCommand)
    return checkpoint.eventId == m_ReplayPatchAnchorEvent;
  // Protect the newest complete boundary before any potential consumer. A better complete
  // boundary may replace the initial anchor when the same total byte budget cannot hold both.
  if(checkpoint.eventId >= m_ReplayPatchFirstConsumer)
    return false;
  if(building.kind != ReplayCheckpointKind::PartialCommand &&
     building.eventId > checkpoint.eventId && building.eventId < m_ReplayPatchFirstConsumer)
    return false;
  for(const ReplayCheckpoint *other : m_Checkpoints)
    if(other->kind != ReplayCheckpointKind::PartialCommand &&
       other->eventId > checkpoint.eventId && other->eventId < m_ReplayPatchFirstConsumer)
      return false;
  return true;
}

void WrappedVulkan::SaveReplayCheckpoint(uint32_t endEventID, ReplayLogType replayType,
                                        ReplayCheckpointKind kind)
{
  if(!m_CheckpointConfig.enabled || !m_CheckpointCaptureSafe ||
     m_Replay->m_ReplayCheckpointBypass || replayType == eReplay_OnlyDraw || m_ActionCallback ||
     m_CheckpointConfig.maxCheckpoints == 0 || m_CheckpointConfig.byteBudget == 0)
    return;
  if(!CheckReplayCheckpointResourceReplacements())
  {
    m_CheckpointStats.reason = "bufferOrImageReplacement";
    return;
  }
  const uint32_t event = replayType == eReplay_WithoutDraw ? RDCMAX(1U, endEventID) - 1 : endEventID;
  // Restrict new saves only. Configuration must retain its normal candidate mapping and must
  // never discard an existing downstream point merely because a foreground request is active.
  if(m_Replay->m_ReplayCheckpointPrefixOnly && !m_ReplayBufferPatches.empty() &&
     !ReplayCheckpointIsPatchAnchor(event, kind))
    return;
  if(!ReplayCheckpointCandidateSelected(event, kind))
    return;
  const CommandBufferNode *node = NULL;
  BakedCmdBufferInfo *baked = NULL;
  if(kind == ReplayCheckpointKind::PartialCommand)
  {
    if(event == 0 || m_Partial.partialStack.size() != 1 ||
       m_Partial.partialStack[0].renderPassSuspended ||
       !m_Partial.partialStack[0].childCmdNodes.empty())
      return;
    node = &m_Partial.partialStack[0];
    baked = &m_BakedCmdBufferInfo[node->cmdId];
    if(baked->level != VK_COMMAND_BUFFER_LEVEL_PRIMARY ||
       !IsEventInCommandBuffer(node, event + 1, baked->eventCount))
      return;
  }
  else if(!m_CheckpointSubmissionCaptureSafe ||
          (kind == ReplayCheckpointKind::FrameInitial ? event != 0 :
           m_CheckpointSubmissionEnds.count(event) == 0))
    return;
  for(const ReplayCheckpoint *old : m_Checkpoints)
    if((old->kind == kind && old->eventId == event) ||
       (node && old->kind == ReplayCheckpointKind::PartialCommand &&
                               event != m_ReplayPatchAnchorEvent &&
                               old->partialStack[0].cmdId == node->cmdId &&
                               old->partialStack[0].beginEvent == node->beginEvent &&
                               (event > old->eventId ? event - old->eventId : old->eventId - event) <
                                   m_CheckpointConfig.minEventGap))
      return;

  ReplayCheckpoint *checkpoint = new ReplayCheckpoint;
  PerformanceTimer timer;
  checkpoint->eventId = event;
  checkpoint->kind = kind;
  checkpoint->serial = ++m_CheckpointSerial;
  if(baked)
  {
    checkpoint->renderState = m_RenderState;
    checkpoint->partialStack = m_Partial.partialStack;
    checkpoint->cmdImageStates = baked->imageStates;
    checkpoint->lastCmdBuffer = m_LastCmdBufferID;
    checkpoint->pushCommandBuffer = m_PushCommandBuffer;
    checkpoint->curEventID = baked->curEventID;
    checkpoint->activeSubpass = baked->activeSubpass;
    checkpoint->renderPassOpen = baked->renderPassOpen;
    checkpoint->endBarriers = baked->endBarriers;
  }

  auto written = [this](ResourceId id) {
    auto uses = m_ResourceUses.find(id);
    if(uses != m_ResourceUses.end())
      for(const EventUsage &use : uses->second)
        if(VulkanCheckpointWrite(use.usage))
          return true;
    return false;
  };
  rdcstr failure;
  const VkDevDispatchTable *vt = ObjDisp(GetDev());
  uint64_t allocated = 0;
  std::set<ResourceId> coveredBuffers;
  auto allocate = [&](ReplayCheckpointResource &resource, const VkMemoryRequirements &requirements) {
    // Eviction happens before allocation, keeping the *actual* allocation total within the budget.
    if(requirements.size > m_CheckpointConfig.byteBudget - allocated)
      return false;
    while(!m_Checkpoints.empty() &&
          (GetReplayCheckpointStats().allocatedBytes + allocated + requirements.size >
               m_CheckpointConfig.byteBudget ||
           m_Checkpoints.size() >= m_CheckpointConfig.maxCheckpoints))
    {
      size_t oldest = m_Checkpoints.size();
      for(size_t i = 0; i < m_Checkpoints.size(); ++i)
        if(!ReplayCheckpointIsProtected(*m_Checkpoints[i], *checkpoint) &&
           (oldest == m_Checkpoints.size() ||
            m_Checkpoints[i]->serial < m_Checkpoints[oldest]->serial))
          oldest = i;
      if(oldest == m_Checkpoints.size())
        return false;
      SubmitCmds();
      FlushQ();
      DestroyReplayCheckpoint(m_Checkpoints[oldest]);
      m_Checkpoints.erase(oldest);
    }
    VkMemoryAllocateInfo info = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL, requirements.size,
                                GetGPULocalMemoryIndex(requirements.memoryTypeBits)};
    VkResult result = vt->AllocateMemory(Unwrap(GetDev()), &info, NULL, &resource.memory);
    if(result != VK_SUCCESS)
      return false;
    resource.bytes = requirements.size;
    allocated += requirements.size;
    return true;
  };

  // Snapshot linear allocations rather than individual buffer views. This includes mapped CPU
  // writes and overlapping buffer aliases, and avoids pretending GetUsage(buffer) covers memory
  // writes (Vulkan records those usages against the VkDeviceMemory resource).
  for(const auto &mem : m_CreationInfo.m_Memory)
  {
    if(ResourceIDGen::IsReplayOnlyID(mem.first))
      continue;
    const ResourceDescription &desc = GetResourceDesc(mem.first);
    bool buffers = false, images = false, mutableMemory = written(mem.first);
    // Edits are a mutation of the live allocation even if original usage was purely uniform-read.
    // The first Set/Clear invalidates old points. Value-only edits preserve earlier prefixes and
    // overlay their new ranges after restore, so those points must own the patched allocation.
    for(const ReplayBufferPatch &patch : m_ReplayBufferMemoryPatches)
      mutableMemory |= patch.buffer == mem.first;
    for(ResourceId child : desc.derivedResources)
    {
      if(ResourceIDGen::IsReplayOnlyID(child))
        continue;
      images |= m_CreationInfo.m_Image.find(child) != m_CreationInfo.m_Image.end();
      auto buffer = m_CreationInfo.m_Buffer.find(child);
      if(buffer != m_CreationInfo.m_Buffer.end())
      {
        buffers = true;
        coveredBuffers.insert(child);
        mutableMemory |= written(child);
        mutableMemory |= (buffer->second.usage & (VK_BUFFER_USAGE_STORAGE_BUFFER_BIT |
                            VK_BUFFER_USAGE_STORAGE_TEXEL_BUFFER_BIT |
                            VK_BUFFER_USAGE_TRANSFORM_FEEDBACK_BUFFER_BIT_EXT |
                            VK_BUFFER_USAGE_TRANSFORM_FEEDBACK_COUNTER_BUFFER_BIT_EXT)) != 0;
        if(buffer->second.external || buffer->second.sparse ||
           (buffer->second.usage & VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT))
          failure = "externalSparseOrDeviceAddressBuffer";
      }
    }
    if(buffers && images)
      failure = "linearAndImageAllocation";
    if(!failure.empty())
      break;
    if(!buffers)
      continue;
    if(mem.second.wholeMemBuf == VK_NULL_HANDLE || mem.second.wholeMemBufSize == 0)
    {
      failure = "missingWholeAllocationBuffer";
      break;
    }
    ReplayCheckpointResource resource;
    resource.source = mem.first;
    resource.sourceBuffer = Unwrap(mem.second.wholeMemBuf);
    resource.copyBytes = mem.second.wholeMemBufSize;
    resource.shared = !mutableMemory;
    checkpoint->resources.push_back(resource);
    ReplayCheckpointResource &stored = checkpoint->resources.back();
    if(stored.shared)
      continue;
    VkBufferCreateInfo create = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, NULL, 0, stored.copyBytes,
                                VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                                VK_SHARING_MODE_EXCLUSIVE, 0, NULL};
    if(vt->CreateBuffer(Unwrap(GetDev()), &create, NULL, &stored.buffer) != VK_SUCCESS)
    {
      failure = "createAllocationVersionFailed";
      break;
    }
    VkMemoryRequirements requirements = {};
    vt->GetBufferMemoryRequirements(Unwrap(GetDev()), stored.buffer, &requirements);
    if(!allocate(stored, requirements) ||
       vt->BindBufferMemory(Unwrap(GetDev()), stored.buffer, stored.memory, 0) != VK_SUCCESS)
    {
      failure = "allocationVersionBudgetOrBindFailed";
      break;
    }
  }

  for(const auto &buffer : m_CreationInfo.m_Buffer)
    if(!ResourceIDGen::IsReplayOnlyID(buffer.first) &&
       coveredBuffers.find(buffer.first) == coveredBuffers.end())
      failure = "untrackedBufferAllocation";

  // Image aliases cannot be restored by independent logical image copies. Reject overlapping
  // bound ranges, including a capture that happens not to use both aliases at the selected EID.
  // m_Image also contains the swapchain-ID creation template, which has no native image or
  // ImageState. Enumerate live image state, just like VulkanReplay::GetTextures(), instead of
  // mistaking that template for an unrestorable capture resource.
  for(const auto &imageEntry : m_ImageStates)
  {
    if(!failure.empty())
      break;
    if(ResourceIDGen::IsReplayOnlyID(imageEntry.first))
      continue;
    const auto imageIt = m_CreationInfo.m_Image.find(imageEntry.first);
    if(imageIt == m_CreationInfo.m_Image.end())
    {
      failure = "missingLiveImageCreationInfo";
      break;
    }
    const auto &image = *imageIt;
    const VulkanCreationInfo::Image &info = image.second;
    LockedConstImageStateRef state = FindConstImageState(image.first);
    if(!state || !state->isMemoryBound || info.linear || info.external || info.sparse ||
       IsYUVFormat(info.format) || info.mipLevels == 0 || info.arrayLayers == 0 ||
       !state->oldQueueFamilyTransfers.empty() || !state->newQueueFamilyTransfers.empty())
    {
      failure = "unsupportedImageStorageOrOwnership";
      if(IsReplayTimingActive())
        LogVulkanCheckpoint(StringFormat::Fmt(
            "api=VulkanCheckpoint op=rejectImage event=%u resource=%s state=%u bound=%u "
            "linear=%u external=%u sparse=%u yuv=%u mips=%u layers=%u oldTransfers=%zu newTransfers=%zu",
            event, ToStr(image.first).c_str(), state ? 1U : 0U,
            state && state->isMemoryBound ? 1U : 0U, info.linear ? 1U : 0U,
            info.external ? 1U : 0U, info.sparse ? 1U : 0U, IsYUVFormat(info.format) ? 1U : 0U,
            info.mipLevels, info.arrayLayers, state ? state->oldQueueFamilyTransfers.size() : 0,
            state ? state->newQueueFamilyTransfers.size() : 0));
      break;
    }
    // Replayed swapchain images are allocated by Serialise_vkCreateSwapchainKHR in known
    // non-overlapping ranges. They intentionally have no capture-time memory ResourceId.
    // Other images must have an explicit binding so alias checks cannot silently miss them.
    if(state->boundMemory == ResourceId() && !(info.creationFlags & TextureCategory::SwapBuffer))
    {
      failure = "untrackedImageAllocation";
      break;
    }
    for(const ReplayCheckpointResource &other : checkpoint->resources)
      if(other.sourceImage != VK_NULL_HANDLE && state->boundMemory == other.imageState.boundMemory &&
         state->boundMemoryOffset < other.imageState.boundMemoryOffset + other.imageState.boundMemorySize &&
         other.imageState.boundMemoryOffset < state->boundMemoryOffset + state->boundMemorySize)
        failure = "overlappingImageMemory";
    for(auto sub = state->subresourceStates.begin(); sub != state->subresourceStates.end(); ++sub)
      if(sub->state().newLayout == VK_IMAGE_LAYOUT_PREINITIALIZED ||
         (sub->state().newQueueFamilyIndex != VK_QUEUE_FAMILY_IGNORED &&
          sub->state().newQueueFamilyIndex != m_QueueFamilyIdx))
        failure = "unsupportedImageLayoutOrQueueOwnership";
    if(!failure.empty())
      break;
    ReplayCheckpointResource resource;
    resource.source = image.first;
    resource.sourceImage = state->wrappedHandle;
    resource.imageState = *state;
    resource.copyBytes = info.mrq.size;
    resource.shared = !written(image.first) && !written(state->boundMemory) &&
                      !(info.creationFlags & (TextureCategory::ColorTarget |
                         TextureCategory::DepthTarget | TextureCategory::ShaderReadWrite));
    checkpoint->resources.push_back(resource);
    ReplayCheckpointResource &stored = checkpoint->resources.back();
    if(stored.shared)
      continue;
    VkImageCreateInfo create = {VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, NULL, DefaultImageCreateFlags(),
                               info.type, info.format, info.extent, info.mipLevels, info.arrayLayers,
                               info.samples, VK_IMAGE_TILING_OPTIMAL,
                               VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
                               VK_SHARING_MODE_EXCLUSIVE, 0, NULL, VK_IMAGE_LAYOUT_UNDEFINED};
    if(vt->CreateImage(Unwrap(GetDev()), &create, NULL, &stored.image) != VK_SUCCESS)
    {
      failure = "createImageVersionFailed";
      break;
    }
    VkMemoryRequirements requirements = {};
    vt->GetImageMemoryRequirements(Unwrap(GetDev()), stored.image, &requirements);
    if(!allocate(stored, requirements) ||
       vt->BindImageMemory(Unwrap(GetDev()), stored.image, stored.memory, 0) != VK_SUCCESS)
    {
      failure = "imageVersionBudgetOrBindFailed";
      break;
    }
  }
  if(failure.empty() && checkpoint->resources.empty())
    failure = "noCheckpointResources";
  if(failure.empty() && !CopyReplayCheckpointResources(*checkpoint, false))
    failure = "saveResourceIdentityOrCopyFailed";
  if(!failure.empty())
  {
    DestroyReplayCheckpoint(checkpoint);
    ++m_CheckpointStats.rejected;
    m_CheckpointStats.reason = failure;
    if(IsReplayTimingActive())
      LogVulkanCheckpoint(StringFormat::Fmt(
          "api=VulkanCheckpoint op=reject event=%u reason=%s total_ms=%.3f",
          event, failure.c_str(), timer.GetMilliseconds()));
    return;
  }
  // A capture with only shared data still needs a bounded CPU-state cache.
  while(m_Checkpoints.size() >= m_CheckpointConfig.maxCheckpoints)
  {
    size_t oldest = m_Checkpoints.size();
    for(size_t i = 0; i < m_Checkpoints.size(); ++i)
      if(!ReplayCheckpointIsProtected(*m_Checkpoints[i], *checkpoint) &&
         (oldest == m_Checkpoints.size() ||
          m_Checkpoints[i]->serial < m_Checkpoints[oldest]->serial))
        oldest = i;
    if(oldest == m_Checkpoints.size())
    {
      DestroyReplayCheckpoint(checkpoint);
      ++m_CheckpointStats.rejected;
      m_CheckpointStats.reason = "budgetReservedForSharedBufferPrefixAnchor";
      return;
    }
    SubmitCmds();
    FlushQ();
    DestroyReplayCheckpoint(m_Checkpoints[oldest]);
    m_Checkpoints.erase(oldest);
  }
  m_Checkpoints.push_back(checkpoint);
  ++m_CheckpointStats.builds;
  m_CheckpointStats.lastSaveEvent = event;
  m_CheckpointStats.reason = kind == ReplayCheckpointKind::FrameInitial ? "savedFrameInitialNativeResources" :
      kind == ReplayCheckpointKind::CompletedSubmission ? "savedCompletedSubmissionNativeResources" :
                                                         "savedPrimaryCommandBufferPrefix";
  if(IsReplayTimingActive())
    LogVulkanCheckpoint(StringFormat::Fmt(
        "api=VulkanCheckpoint op=save kind=%s event=%u bytes=%llu resources=%zu total_ms=%.3f",
        kind == ReplayCheckpointKind::FrameInitial ? "FrameInitial" :
        kind == ReplayCheckpointKind::CompletedSubmission ? "CompletedSubmission" : "PartialCommand",
        event, (unsigned long long)allocated, checkpoint->resources.size(), timer.GetMilliseconds()));
}

bool WrappedVulkan::CopyReplayCheckpointResources(ReplayCheckpoint &checkpoint, bool restore)
{
  PerformanceTimer timer;
  rdcflatmap<ResourceId, ImageState> previousImageStates;
  // Validate every identity before mutating any resource. A replacement invalidates a native
  // version; it must never be copied into a different allocation with coincident dimensions.
  for(const ReplayCheckpointResource &resource : checkpoint.resources)
  {
    if(resource.sourceImage != VK_NULL_HANDLE)
    {
      LockedConstImageStateRef state = FindConstImageState(resource.source);
      if(!state || state->wrappedHandle != resource.sourceImage ||
         GetResourceManager()->GetHandle<VkImage>(resource.source) != resource.sourceImage ||
         state->boundMemory != resource.imageState.boundMemory)
        return false;
      if(!state->oldQueueFamilyTransfers.empty() || !state->newQueueFamilyTransfers.empty())
        return false;
      for(auto sub = state->subresourceStates.begin(); sub != state->subresourceStates.end(); ++sub)
        if(sub->state().newQueueFamilyIndex != VK_QUEUE_FAMILY_IGNORED &&
           sub->state().newQueueFamilyIndex != m_QueueFamilyIdx)
          return false;
      if(restore)
        previousImageStates[resource.source] = *state;
    }
    else
    {
      auto memory = m_CreationInfo.m_Memory.find(resource.source);
      if(memory == m_CreationInfo.m_Memory.end() ||
         Unwrap(memory->second.wholeMemBuf) != resource.sourceBuffer)
        return false;
    }
  }
  SubmitCmds();
  FlushQ();
  const VkDevDispatchTable *vt = ObjDisp(GetDev());
  {
    ScopedReplayTimingSection timing(ReplayTimingSection::ExplicitWait);
    if(vt->DeviceWaitIdle(Unwrap(GetDev())) != VK_SUCCESS)
      return false;
  }
  const double drainMS = timer.GetMilliseconds();
  VkCommandBuffer cmd = GetNextCmd();
  if(cmd == VK_NULL_HANDLE)
    return false;
  VkCommandBufferBeginInfo begin = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, NULL,
                                  VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
  if(vt->BeginCommandBuffer(Unwrap(cmd), &begin) != VK_SUCCESS)
  {
    RemovePendingCommandBuffer(cmd);
    AddFreeCommandBuffer(cmd);
    return false;
  }
  VkMemoryBarrier memoryBarrier = {VK_STRUCTURE_TYPE_MEMORY_BARRIER, NULL,
                                  VK_ACCESS_MEMORY_WRITE_BIT | VK_ACCESS_HOST_WRITE_BIT,
                                  VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT};
  vt->CmdPipelineBarrier(Unwrap(cmd), VK_PIPELINE_STAGE_ALL_COMMANDS_BIT | VK_PIPELINE_STAGE_HOST_BIT,
                         VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1, &memoryBarrier, 0, NULL, 0, NULL);
  for(ReplayCheckpointResource &resource : checkpoint.resources)
  {
    if(resource.sourceImage == VK_NULL_HANDLE)
    {
      if(!resource.shared)
      {
        VkBufferCopy copy = {0, 0, resource.copyBytes};
        vt->CmdCopyBuffer(Unwrap(cmd), restore ? resource.buffer : resource.sourceBuffer,
                          restore ? resource.sourceBuffer : resource.buffer, 1, &copy);
      }
      continue;
    }
    LockedImageStateRef state = FindImageState(resource.source);
    if(resource.shared)
    {
      if(restore)
      {
        state->InlineTransition(cmd, m_QueueFamilyIdx, resource.imageState,
                                VK_ACCESS_MEMORY_WRITE_BIT, VK_ACCESS_MEMORY_READ_BIT,
                                GetImageTransitionInfo());
        *state = resource.imageState;
      }
      continue;
    }
    const ImageInfo &info = resource.imageState.GetImageInfo();
    rdcarray<VkImageCopy> regions;
    // Undefined subresources have no content to preserve. Do not read their uninitialised bytes;
    // restoring their undefined metadata is enough, and the next original transition discards them.
    for(auto aspect = ImageAspectFlagIter::begin(info.aspects); aspect != ImageAspectFlagIter::end(); ++aspect)
      for(uint32_t mip = 0; mip < info.levelCount; ++mip)
        for(uint32_t layer = 0; layer < info.layerCount; ++layer)
        {
          VkImageLayout layout = resource.imageState.GetImageLayout(*aspect, mip, layer);
          if(layout == VK_IMAGE_LAYOUT_UNDEFINED || layout == UNKNOWN_PREV_IMG_LAYOUT)
            continue;
          VkImageCopy region = {{(VkImageAspectFlags)*aspect, mip, layer, 1}, {0, 0, 0},
                                {(VkImageAspectFlags)*aspect, mip, layer, 1}, {0, 0, 0},
                                {RDCMAX(1U, info.extent.width >> mip),
                                 RDCMAX(1U, info.extent.height >> mip),
                                 RDCMAX(1U, info.extent.depth >> mip)}};
          regions.push_back(region);
        }
    if(!restore)
    {
      VkImageMemoryBarrier barrier = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER, NULL, 0,
                                     VK_ACCESS_TRANSFER_WRITE_BIT, VK_IMAGE_LAYOUT_UNDEFINED,
                                     VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                                     VK_QUEUE_FAMILY_IGNORED, VK_QUEUE_FAMILY_IGNORED, resource.image,
                                     {info.aspects, 0, info.levelCount, 0, info.layerCount}};
      vt->CmdPipelineBarrier(Unwrap(cmd), VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                             VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, NULL, 0, NULL, 1, &barrier);
      ImageBarrierSequence setup, cleanup;
      state->TempTransition(m_QueueFamilyIdx, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                            VK_ACCESS_TRANSFER_READ_BIT, setup, cleanup, GetImageTransitionInfo());
      InlineSetupImageBarriers(cmd, setup);
      // Queue-family transfers were rejected before recording, so every barrier is inline.
      if(!regions.empty())
        vt->CmdCopyImage(Unwrap(cmd), Unwrap(resource.sourceImage), VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                         resource.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                         (uint32_t)regions.size(), regions.data());
      InlineCleanupImageBarriers(cmd, cleanup);
      barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
      barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
      barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
      barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
      vt->CmdPipelineBarrier(Unwrap(cmd), VK_PIPELINE_STAGE_TRANSFER_BIT,
                             VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, NULL, 0, NULL, 1, &barrier);
    }
    else
    {
      state->InlineTransition(cmd, m_QueueFamilyIdx, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                              VK_ACCESS_MEMORY_WRITE_BIT, VK_ACCESS_TRANSFER_WRITE_BIT,
                              GetImageTransitionInfo());
      if(!regions.empty())
        vt->CmdCopyImage(Unwrap(cmd), resource.image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                         Unwrap(resource.sourceImage), VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                         (uint32_t)regions.size(), regions.data());
      state->InlineTransition(cmd, m_QueueFamilyIdx, resource.imageState,
                              VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_MEMORY_READ_BIT |
                                  VK_ACCESS_MEMORY_WRITE_BIT, GetImageTransitionInfo());
      *state = resource.imageState;
    }
  }
  // A retained pre-consumer prefix may own the previous parameter bytes. Reapply the full active
  // table after copying it back, including exact-prefix restores which execute no suffix commands.
  if(restore)
    ApplyReplayBufferPatches(cmd);
  memoryBarrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
  memoryBarrier.dstAccessMask = VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT |
                                VK_ACCESS_HOST_READ_BIT;
  vt->CmdPipelineBarrier(Unwrap(cmd), VK_PIPELINE_STAGE_TRANSFER_BIT,
                         VK_PIPELINE_STAGE_ALL_COMMANDS_BIT | VK_PIPELINE_STAGE_HOST_BIT,
                         0, 1, &memoryBarrier, 0, NULL, 0, NULL);
  if(vt->EndCommandBuffer(Unwrap(cmd)) != VK_SUCCESS)
  {
    for(const auto &previous : previousImageStates)
    {
      LockedImageStateRef state = FindImageState(previous.first);
      *state = previous.second;
    }
    vt->ResetCommandBuffer(Unwrap(cmd), 0);
    RemovePendingCommandBuffer(cmd);
    AddFreeCommandBuffer(cmd);
    return false;
  }
  const double recordMS = timer.GetMilliseconds() - drainMS;
  SubmitCmds();
  FlushQ();
  if(IsReplayTimingActive())
    LogVulkanCheckpoint(StringFormat::Fmt(
        "api=VulkanCheckpoint op=%s event=%u drain_ms=%.3f record_ms=%.3f "
        "submitwait_ms=%.3f total_ms=%.3f", restore ? "restoreCopy" : "saveCopy",
        checkpoint.eventId, drainMS, recordMS, timer.GetMilliseconds() - drainMS - recordMS,
        timer.GetMilliseconds()));
  return !HasFatalError();
}
