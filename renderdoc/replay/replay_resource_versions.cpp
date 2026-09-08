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

#include "replay_resource_versions.h"
#include "common/common.h"

template <>
rdcstr DoStringise(const ReplayResourceRestoreAction &el)
{
  BEGIN_ENUM_STRINGISE(ReplayResourceRestoreAction);
  {
    STRINGISE_ENUM_CLASS(Keep)
    STRINGISE_ENUM_CLASS(Restore)
    STRINGISE_ENUM_CLASS(Defer)
    STRINGISE_ENUM_CLASS(Rebuild)
  }
  END_ENUM_STRINGISE();
}

template <>
rdcstr DoStringise(const ReplayResourceFirstAccess &el)
{
  BEGIN_ENUM_STRINGISE(ReplayResourceFirstAccess);
  {
    STRINGISE_ENUM_CLASS(Unknown)
    STRINGISE_ENUM_CLASS(Read)
    STRINGISE_ENUM_CLASS(FullOverwrite)
    STRINGISE_ENUM_CLASS(Unused)
  }
  END_ENUM_STRINGISE();
}

ReplayResourceRestoreAction PlanReplayResourceRestore(const ReplayResourceVersion &resident,
                                                      const ReplayResourceVersion &target,
                                                      uint64_t minimumEditGeneration,
                                                      const ReplayResourceAccessProof &proof)
{
  if(target.editGeneration < minimumEditGeneration)
    return ReplayResourceRestoreAction::Rebuild;
  if(resident.Known() && target.Known() && resident == target)
    return ReplayResourceRestoreAction::Keep;
  if(target.Known() && proof.complete && !proof.aliasUncertain &&
     (proof.firstAccess == ReplayResourceFirstAccess::FullOverwrite ||
      proof.firstAccess == ReplayResourceFirstAccess::Unused))
    return ReplayResourceRestoreAction::Defer;
  return ReplayResourceRestoreAction::Restore;
}

bool ReplayResourceVersions::Register(ResourceId id, uint32_t subresource, uint64_t extent,
                                      bool capturedReadOnly, ResourceId knownAliasGroup)
{
  if(id == ResourceId() || extent == 0)
    return false;
  Key key = {id, subresource};
  if(m_Resources.find(key) != m_Resources.end())
    return false;
  Resource &resource = m_Resources[key];
  resource.extent = extent;
  resource.capturedReadOnly = capturedReadOnly;
  resource.aliasGroup = knownAliasGroup;
  Span span;
  span.end = extent;
  // Late registration cannot certify that an old checkpoint predates no relevant edits.
  span.minimumEditGeneration = m_EditGeneration;
  resource.spans.push_back(span);
  return true;
}

void ReplayResourceVersions::Clear()
{
  m_Resources.clear();
  m_EditGeneration = 0;
}

ReplayResourceVersions::Resources::iterator ReplayResourceVersions::Find(const ReplayResourceRange &range)
{
  const auto found = m_Resources.find({range.resource, range.subresource});
  if(found == m_Resources.end() || range.size == 0 || range.offset >= found->second.extent ||
     range.size > found->second.extent - range.offset)
    return m_Resources.end();
  return found;
}

ReplayResourceVersions::Resources::const_iterator ReplayResourceVersions::Find(
    const ReplayResourceRange &range) const
{
  const auto found = m_Resources.find({range.resource, range.subresource});
  if(found == m_Resources.end() || range.size == 0 || range.offset >= found->second.extent ||
     range.size > found->second.extent - range.offset)
    return m_Resources.end();
  return found;
}

void ReplayResourceVersions::Split(Resource &resource, uint64_t position)
{
  for(size_t i = 0; i < resource.spans.size(); i++)
  {
    Span &span = resource.spans[i];
    if(span.begin < position && position < span.end)
    {
      Span right = span;
      right.begin = position;
      span.end = position;
      resource.spans.insert(i + 1, right);
      return;
    }
  }
}

void ReplayResourceVersions::Coalesce(Resource &resource)
{
  size_t kept = 0;
  for(size_t i = 0; i < resource.spans.size(); i++)
  {
    const Span &span = resource.spans[i];
    if(kept > 0)
    {
      Span &previous = resource.spans[kept - 1];
      if(previous.end == span.begin && previous.resident == span.resident &&
         previous.wanted == span.wanted && previous.pending == span.pending &&
         previous.minimumEditGeneration == span.minimumEditGeneration)
      {
        previous.end = span.end;
        continue;
      }
    }
    if(kept != i)
      resource.spans[kept] = span;
    kept++;
  }
  resource.spans.resize(kept);
  // Alternating edits to a large buffer must not produce unbounded metadata. Losing precision is
  // safe: this unit becomes unknown and must be restored/rebuilt before its next read.
  if(resource.spans.size() > 4096)
  {
    Span unknown;
    unknown.end = resource.extent;
    unknown.pending = true;
    for(const Span &span : resource.spans)
      unknown.minimumEditGeneration =
          RDCMAX(unknown.minimumEditGeneration, span.minimumEditGeneration);
    resource.spans.resize(1);
    resource.spans[0] = unknown;
  }
}

rdcarray<ReplayResourceRestoreItem> ReplayResourceVersions::Plan(const ReplayResourceRange &range,
                                                                 const ReplayResourceVersion &target,
                                                                 const ReplayResourceAccessProof &proof)
{
  rdcarray<ReplayResourceRestoreItem> result;
  auto found = Find(range);
  if(found == m_Resources.end())
  {
    result.push_back({range, ReplayResourceRestoreAction::Restore, {}, target});
    return result;
  }
  Resource &resource = found->second;
  const uint64_t end = range.offset + range.size;
  Split(resource, range.offset);
  Split(resource, end);
  for(Span &span : resource.spans)
  {
    if(span.begin < range.offset || span.end > end)
      continue;
    const ReplayResourceRestoreAction action =
        PlanReplayResourceRestore(span.resident, target, span.minimumEditGeneration, proof);
    span.wanted = target;
    span.pending = action != ReplayResourceRestoreAction::Keep;
    result.push_back({{range.resource, range.subresource, span.begin, span.end - span.begin},
                      action,
                      span.resident,
                      target});
  }
  Coalesce(resource);
  return result;
}

void ReplayResourceVersions::InvalidateAliases(const Key &key, ResourceId group,
                                               uint64_t minimumEditGeneration)
{
  if(group == ResourceId())
    return;
  for(auto &entry : m_Resources)
  {
    if(entry.second.aliasGroup != group ||
       (entry.first.resource == key.resource && entry.first.subresource == key.subresource))
      continue;
    for(Span &span : entry.second.spans)
    {
      span.resident = {};
      span.pending = true;
      span.minimumEditGeneration = RDCMAX(span.minimumEditGeneration, minimumEditGeneration);
    }
    Coalesce(entry.second);
  }
}

bool ReplayResourceVersions::MarkResident(const ReplayResourceRange &range,
                                          const ReplayResourceVersion &version)
{
  auto found = Find(range);
  if(found == m_Resources.end() || !version.Known())
    return false;
  Resource &resource = found->second;
  const uint64_t end = range.offset + range.size;
  // An old checkpoint must not become valid merely because the physical copy completed.
  for(const Span &span : resource.spans)
    if(span.begin < end && span.end > range.offset &&
       version.editGeneration < span.minimumEditGeneration)
      return false;
  Split(resource, range.offset);
  Split(resource, end);
  for(Span &span : resource.spans)
  {
    if(span.begin < range.offset || span.end > end)
      continue;
    span.resident = version;
    // A completed full overwrite supersedes a deferred initial version. Partial writes must
    // report only their covered range; untouched debt survives in the adjacent spans.
    span.wanted = version;
    span.pending = false;
  }
  InvalidateAliases(found->first, resource.aliasGroup, 0);
  Coalesce(resource);
  return true;
}

bool ReplayResourceVersions::Invalidate(const ReplayResourceRange &range)
{
  auto found = Find(range);
  if(found == m_Resources.end())
    return false;
  Resource &resource = found->second;
  const uint64_t end = range.offset + range.size;
  Split(resource, range.offset);
  Split(resource, end);
  for(Span &span : resource.spans)
    if(span.begin >= range.offset && span.end <= end)
    {
      span.resident = {};
      span.pending = true;
    }
  InvalidateAliases(found->first, resource.aliasGroup, 0);
  Coalesce(resource);
  return true;
}

void ReplayResourceVersions::InvalidateAll()
{
  for(auto &entry : m_Resources)
  {
    for(Span &span : entry.second.spans)
    {
      span.resident = {};
      span.pending = true;
    }
    Coalesce(entry.second);
  }
}

rdcarray<ReplayResourceRange> ReplayResourceVersions::Pending() const
{
  rdcarray<ReplayResourceRange> ranges;
  for(const auto &entry : m_Resources)
    for(const Span &span : entry.second.spans)
      if(span.pending)
        ranges.push_back(
            {entry.first.resource, entry.first.subresource, span.begin, span.end - span.begin});
  return ranges;
}

bool ReplayResourceVersions::IsResident(const ReplayResourceRange &range,
                                        const ReplayResourceVersion &version) const
{
  auto found = Find(range);
  if(found == m_Resources.end() || !version.Known())
    return false;
  const uint64_t end = range.offset + range.size;
  for(const Span &span : found->second.spans)
    if(span.begin < end && span.end > range.offset &&
       (span.resident != version || version.editGeneration < span.minimumEditGeneration))
      return false;
  return true;
}

uint64_t ReplayResourceVersions::InvalidateForEdit(const rdcarray<ReplayResourceRange> &affected,
                                                   bool dependencyClosureComplete)
{
  m_EditGeneration++;
  // Bad dependency ranges must not accidentally turn a supposedly complete closure into no work.
  for(const ReplayResourceRange &range : affected)
    if(Find(range) == m_Resources.end())
      dependencyClosureComplete = false;
  if(!dependencyClosureComplete)
    for(auto &entry : m_Resources)
      if(!entry.second.capturedReadOnly)
      {
        for(Span &span : entry.second.spans)
        {
          span.resident = {};
          span.pending = true;
          span.minimumEditGeneration = m_EditGeneration;
        }
        InvalidateAliases(entry.first, entry.second.aliasGroup, m_EditGeneration);
        Coalesce(entry.second);
      }
  for(const ReplayResourceRange &range : affected)
  {
    auto found = Find(range);
    if(found == m_Resources.end())
      continue;
    Resource &resource = found->second;
    const uint64_t end = range.offset + range.size;
    Split(resource, range.offset);
    Split(resource, end);
    for(Span &span : resource.spans)
      if(span.begin >= range.offset && span.end <= end)
      {
        span.resident = {};
        span.pending = true;
        span.minimumEditGeneration = m_EditGeneration;
      }
    InvalidateAliases(found->first, resource.aliasGroup, m_EditGeneration);
    Coalesce(resource);
  }
  return m_EditGeneration;
}

#if ENABLED(ENABLE_UNIT_TESTS)

#include "catch/catch.hpp"

namespace
{
ReplayResourceRange VersionTestRange(ResourceId id, uint64_t offset = 0, uint64_t size = 64,
                                     uint32_t subresource = 0)
{
  return {id, subresource, offset, size};
}

ReplayResourceAccessProof VersionTestProof(ReplayResourceFirstAccess access)
{
  ReplayResourceAccessProof proof;
  proof.firstAccess = access;
  proof.complete = true;
  proof.aliasUncertain = false;
  return proof;
}
}

TEST_CASE("Replay version policy requires complete evidence for deferred restoration",
          "[replay][checkpoint][resourceversions]")
{
  CHECK(ToStr(ReplayResourceRestoreAction::Restore) == "Restore");
  CHECK(ToStr(ReplayResourceFirstAccess::FullOverwrite) == "FullOverwrite");
  const ReplayResourceVersion unknown, initial = {1, 0}, changed = {2, 0}, edited = {1, 1};
  ReplayResourceAccessProof proof;
  CHECK(PlanReplayResourceRestore(initial, initial, 0, proof) == ReplayResourceRestoreAction::Keep);
  CHECK(PlanReplayResourceRestore(unknown, unknown, 0, proof) == ReplayResourceRestoreAction::Restore);
  CHECK(PlanReplayResourceRestore(changed, initial, 0, proof) == ReplayResourceRestoreAction::Restore);
  CHECK(PlanReplayResourceRestore(initial, edited, 0, proof) == ReplayResourceRestoreAction::Restore);
  const ReplayResourceFirstAccess accesses[] = {
      ReplayResourceFirstAccess::Unknown, ReplayResourceFirstAccess::Read,
      ReplayResourceFirstAccess::FullOverwrite, ReplayResourceFirstAccess::Unused};
  for(ReplayResourceFirstAccess access : accesses)
  {
    proof.firstAccess = access;
    proof.complete = false;
    proof.aliasUncertain = false;
    CHECK(PlanReplayResourceRestore(changed, initial, 0, proof) ==
          ReplayResourceRestoreAction::Restore);
    proof.complete = true;
    proof.aliasUncertain = true;
    CHECK(PlanReplayResourceRestore(changed, initial, 0, proof) ==
          ReplayResourceRestoreAction::Restore);
    proof.aliasUncertain = false;
    const bool defer = access == ReplayResourceFirstAccess::FullOverwrite ||
                       access == ReplayResourceFirstAccess::Unused;
    CHECK(PlanReplayResourceRestore(changed, initial, 0, proof) ==
          (defer ? ReplayResourceRestoreAction::Defer : ReplayResourceRestoreAction::Restore));
    // An edit invalidates old source content even when no content copy would otherwise be needed.
    CHECK(PlanReplayResourceRestore(initial, initial, 1, proof) ==
          ReplayResourceRestoreAction::Rebuild);
    CHECK(PlanReplayResourceRestore(unknown, unknown, 0, proof) ==
          ReplayResourceRestoreAction::Restore);
  }
}

TEST_CASE("Deferred replay debt survives another stage and materializes for inspection",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  const ResourceId other = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0}, drawOutput = {2, 0};
  const auto tex = VersionTestRange(texture, 0, 1);
  const auto unrelated = VersionTestRange(other);
  REQUIRE(versions.Register(texture, 0, 1));
  REQUIRE(versions.Register(other, 0, 64));
  REQUIRE(versions.MarkResident(tex, drawOutput));
  auto plan = versions.Plan(tex, initial, VersionTestProof(ReplayResourceFirstAccess::Unused));
  REQUIRE(plan.size() == 1);
  CHECK(plan[0].action == ReplayResourceRestoreAction::Defer);
  REQUIRE(versions.Pending().size() == 1);
  // Replaying/restoring an unrelated unit cannot make the deferred texture valid.
  REQUIRE(versions.MarkResident(unrelated, initial));
  CHECK(versions.Pending().size() == 1);
  CHECK_FALSE(versions.IsResident(tex, initial));
  CHECK(versions.IsResident(tex, drawOutput));
  plan = versions.Plan(tex, initial, VersionTestProof(ReplayResourceFirstAccess::Read));
  REQUIRE(plan.size() == 1);
  CHECK(plan[0].action == ReplayResourceRestoreAction::Restore);
  CHECK(versions.Pending().size() == 1);
  REQUIRE(versions.MarkResident(tex, initial));
  CHECK(versions.Pending().empty());
  CHECK(versions.IsResident(tex, initial));
  CHECK(versions.Plan(tex, initial)[0].action == ReplayResourceRestoreAction::Keep);
}

TEST_CASE("Replay range versions preserve untouched CBuffer bytes and deferred intervals",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0}, changed = {2, 0};
  const auto whole = VersionTestRange(buffer);
  REQUIRE(versions.Register(buffer, 0, 64));
  REQUIRE(versions.MarkResident(whole, changed));
  REQUIRE(versions.Plan(whole, initial, VersionTestProof(ReplayResourceFirstAccess::FullOverwrite))
              .size() == 1);
  // A partial write is not a whole-buffer overwrite: both sides remain owed.
  REQUIRE(versions.MarkResident(VersionTestRange(buffer, 16, 16), initial));
  const auto pending = versions.Pending();
  REQUIRE(pending.size() == 2);
  CHECK(pending[0].offset == 0);
  CHECK(pending[0].size == 16);
  CHECK(pending[1].offset == 32);
  CHECK(pending[1].size == 32);
  const auto plan = versions.Plan(whole, initial, VersionTestProof(ReplayResourceFirstAccess::Read));
  REQUIRE(plan.size() == 3);
  CHECK(plan[0].action == ReplayResourceRestoreAction::Restore);
  CHECK(plan[1].action == ReplayResourceRestoreAction::Keep);
  CHECK(plan[2].action == ReplayResourceRestoreAction::Restore);
  REQUIRE(versions.MarkResident(VersionTestRange(buffer, 0, 16), initial));
  REQUIRE(versions.MarkResident(VersionTestRange(buffer, 32, 32), initial));
  CHECK(versions.Pending().empty());
  CHECK(versions.IsResident(whole, initial));
  REQUIRE(versions.Plan(whole, initial).size() == 1);
}

TEST_CASE("Checkpoint edits invalidate exact physical ranges and reject stale saved versions",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const ResourceId asset = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0};
  const auto whole = VersionTestRange(buffer);
  const auto bytes = VersionTestRange(buffer, 20, 4);
  const auto assetRange = VersionTestRange(asset);
  REQUIRE(versions.Register(buffer, 0, 64, true));
  REQUIRE(versions.Register(asset, 0, 64, true));
  REQUIRE(versions.MarkResident(whole, initial));
  REQUIRE(versions.MarkResident(assetRange, initial));
  const uint64_t generation = versions.InvalidateForEdit({bytes}, true);
  CHECK(generation == 1);
  CHECK(versions.EditGeneration() == generation);
  CHECK(versions.IsResident(VersionTestRange(buffer, 0, 20), initial));
  CHECK(versions.IsResident(VersionTestRange(buffer, 24, 40), initial));
  CHECK(versions.IsResident(assetRange, initial));
  CHECK_FALSE(versions.IsResident(bytes, initial));
  CHECK(versions.Plan(bytes, initial)[0].action == ReplayResourceRestoreAction::Rebuild);
  CHECK_FALSE(versions.MarkResident(bytes, initial));
  const ReplayResourceVersion patched = {2, generation};
  CHECK(versions.Plan(bytes, patched)[0].action == ReplayResourceRestoreAction::Restore);
  REQUIRE(versions.MarkResident(bytes, patched));
  CHECK(versions.IsResident(bytes, patched));
  CHECK(versions.Pending().empty());
  // Undo is another edit generation, not permission to install a stale physical version.
  const uint64_t undo = versions.InvalidateForEdit({bytes}, true);
  CHECK(undo == 2);
  CHECK_FALSE(versions.MarkResident(bytes, initial));
  REQUIRE(versions.MarkResident(bytes, {initial.content, undo}));
  CHECK(versions.IsResident(bytes, {initial.content, undo}));
}

TEST_CASE("Unknown shader dependencies invalidate mutable outputs while preserving proven assets",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  const ResourceId output = ResourceIDGen::GetNewUniqueID();
  const ResourceId edited = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0};
  REQUIRE(versions.Register(texture, 0, 64, true));
  REQUIRE(versions.Register(output, 0, 64));
  REQUIRE(versions.Register(edited, 0, 64, true));
  REQUIRE(versions.MarkResident(VersionTestRange(texture), initial));
  REQUIRE(versions.MarkResident(VersionTestRange(output), initial));
  REQUIRE(versions.MarkResident(VersionTestRange(edited), initial));
  CHECK(versions.InvalidateForEdit({VersionTestRange(edited, 8, 4)}, false) == 1);
  CHECK(versions.IsResident(VersionTestRange(texture), initial));
  CHECK_FALSE(versions.IsResident(VersionTestRange(output), initial));
  CHECK(versions.IsResident(VersionTestRange(edited, 0, 8), initial));
  CHECK_FALSE(versions.IsResident(VersionTestRange(edited, 8, 4), initial));
  CHECK(versions.Plan(VersionTestRange(output), initial)[0].action ==
        ReplayResourceRestoreAction::Rebuild);
  // Invalid explicit closure data must fail closed, even if the caller labelled it complete.
  REQUIRE(versions.MarkResident(VersionTestRange(output), {2, 1}));
  CHECK(versions.InvalidateForEdit({VersionTestRange(output, UINT64_MAX, 2)}, true) == 2);
  CHECK_FALSE(versions.IsResident(VersionTestRange(output), {2, 1}));
  CHECK(versions.IsResident(VersionTestRange(texture), initial));
}

TEST_CASE("Replay alias writes invalidate peer views and texture planes remain independent",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  const ResourceId depth = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0};
  REQUIRE(versions.Register(a, 0, 64, false, heap));
  REQUIRE(versions.Register(b, 0, 64, true, heap));
  REQUIRE(versions.Register(depth, 0, 1));
  REQUIRE(versions.Register(depth, 1, 1));
  REQUIRE(versions.MarkResident(VersionTestRange(a), initial));
  REQUIRE(versions.MarkResident(VersionTestRange(b), initial));
  CHECK_FALSE(versions.IsResident(VersionTestRange(a), initial));
  CHECK(versions.IsResident(VersionTestRange(b), initial));
  CHECK(versions.InvalidateForEdit({VersionTestRange(a, 4, 8)}, true) == 1);
  CHECK_FALSE(versions.IsResident(VersionTestRange(b), initial));
  CHECK(versions.Plan(VersionTestRange(b), initial)[0].action == ReplayResourceRestoreAction::Rebuild);
  const auto plane0 = VersionTestRange(depth, 0, 1, 0);
  const auto plane1 = VersionTestRange(depth, 0, 1, 1);
  REQUIRE(versions.MarkResident(plane0, initial));
  REQUIRE(versions.MarkResident(plane1, initial));
  REQUIRE(versions.Invalidate(plane0));
  CHECK_FALSE(versions.IsResident(plane0, initial));
  CHECK(versions.IsResident(plane1, initial));
}

TEST_CASE("Replay version ledger rejects invalid ranges without inventing resident bytes",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId id = ResourceIDGen::GetNewUniqueID();
  CHECK_FALSE(versions.Register(ResourceId(), 0, 64));
  CHECK_FALSE(versions.Register(id, 0, 0));
  REQUIRE(versions.Register(id, 0, 64));
  CHECK_FALSE(versions.Register(id, 0, 64));
  const ReplayResourceVersion initial = {1, 0};
  REQUIRE(versions.MarkResident(VersionTestRange(id), initial));
  const ReplayResourceRange invalid[] = {
      VersionTestRange(id, 0, 0),          VersionTestRange(id, 64, 1),
      VersionTestRange(id, UINT64_MAX, 2), VersionTestRange(id, 1, UINT64_MAX),
      VersionTestRange(id, 0, 64, 1),      VersionTestRange(ResourceId())};
  for(const auto &range : invalid)
  {
    CHECK_FALSE(versions.MarkResident(range, initial));
    CHECK_FALSE(versions.Invalidate(range));
    CHECK_FALSE(versions.IsResident(range, initial));
    const auto plan =
        versions.Plan(range, initial, VersionTestProof(ReplayResourceFirstAccess::Unused));
    REQUIRE(plan.size() == 1);
    CHECK(plan[0].action == ReplayResourceRestoreAction::Restore);
    CHECK(versions.IsResident(VersionTestRange(id), initial));
    CHECK(versions.Pending().empty());
  }
  CHECK_FALSE(versions.MarkResident(VersionTestRange(id), {}));
  versions.InvalidateAll();
  CHECK_FALSE(versions.IsResident(VersionTestRange(id), initial));
  CHECK(versions.Pending().size() == 1);
  versions.Clear();
  CHECK(versions.Pending().empty());
  CHECK(versions.EditGeneration() == 0);
  CHECK_FALSE(versions.IsResident(VersionTestRange(id), initial));
}

TEST_CASE("Late resource registration and excessive range fragmentation fail closed",
          "[replay][checkpoint][resourceversions]")
{
  ReplayResourceVersions versions;
  const ResourceId edited = ResourceIDGen::GetNewUniqueID();
  const ResourceId late = ResourceIDGen::GetNewUniqueID();
  const ReplayResourceVersion initial = {1, 0};
  REQUIRE(versions.Register(edited, 0, 64));
  REQUIRE(versions.MarkResident(VersionTestRange(edited), initial));
  CHECK(versions.InvalidateForEdit({VersionTestRange(edited)}, true) == 1);
  REQUIRE(versions.Register(late, 0, 64, true));
  CHECK_FALSE(versions.MarkResident(VersionTestRange(late), initial));
  CHECK(versions.Plan(VersionTestRange(late), initial)[0].action ==
        ReplayResourceRestoreAction::Rebuild);

  versions.Clear();
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  REQUIRE(versions.Register(buffer, 0, 16384));
  REQUIRE(versions.MarkResident(VersionTestRange(buffer, 0, 16384), initial));
  bool completed = true;
  for(uint64_t i = 0; i < 2050; i++)
    completed &= versions.MarkResident(VersionTestRange(buffer, i * 2, 1), {2, 0});
  REQUIRE(completed);
  // The metadata cap deliberately loses knowledge instead of assuming all fragmented bytes valid.
  CHECK_FALSE(versions.IsResident(VersionTestRange(buffer, 8192, 1), initial));
  CHECK_FALSE(versions.Pending().empty());
  const auto plan = versions.Plan(VersionTestRange(buffer, 8192, 1), initial);
  REQUIRE(plan.size() == 1);
  CHECK(plan[0].action == ReplayResourceRestoreAction::Restore);
}

#endif
