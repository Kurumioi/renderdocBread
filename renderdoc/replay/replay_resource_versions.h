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

#pragma once

#include <map>
#include "api/replay/rdcarray.h"
#include "api/replay/resourceid.h"

// Device-local content identity, not an EID or a hash of presentation pixels. A backend can reuse
// an identity only after proving that the bytes are unchanged. Zero is never a known identity.
struct ReplayResourceVersion
{
  uint64_t content = 0;
  uint64_t editGeneration = 0;

  bool Known() const { return content != 0; }
  bool operator==(const ReplayResourceVersion &other) const
  {
    return content == other.content && editGeneration == other.editGeneration;
  }
  bool operator!=(const ReplayResourceVersion &other) const { return !(*this == other); }
};

// Half-open storage range. Buffers use byte offsets. Texture subresources use [0, 1), unless the
// backend can independently copy and prove content for smaller tiles. IDs are original capture IDs.
struct ReplayResourceRange
{
  ResourceId resource;
  uint32_t subresource = 0;
  uint64_t offset = 0;
  uint64_t size = 0;
};

enum class ReplayResourceFirstAccess
{
  Unknown,
  Read,
  FullOverwrite,
  Unused,
};

struct ReplayResourceAccessProof
{
  ReplayResourceFirstAccess firstAccess = ReplayResourceFirstAccess::Unknown;
  // Covers the complete replay interval AND any output inspection performed before leaving it.
  // A missing entry in GetUsage, a writable descriptor, or Discard alone is not such a proof.
  bool complete = false;
  // Includes unresolved aliasing, sparse mappings, indirect addresses and hidden shader accesses.
  bool aliasUncertain = true;
};

enum class ReplayResourceRestoreAction
{
  Keep,
  Restore,
  Defer,
  // The requested checkpoint precedes an edit affecting this storage. Rebuild the prefix instead.
  Rebuild,
};

// Shared policy, usable without the ledger when a backend already tracks exact resident lineage.
ReplayResourceRestoreAction PlanReplayResourceRestore(const ReplayResourceVersion &resident,
                                                      const ReplayResourceVersion &target,
                                                      uint64_t minimumEditGeneration,
                                                      const ReplayResourceAccessProof &proof);

struct ReplayResourceRestoreItem
{
  ReplayResourceRange range;
  ReplayResourceRestoreAction action = ReplayResourceRestoreAction::Restore;
  ReplayResourceVersion resident;
  ReplayResourceVersion target;
};

// This ledger does not own GPU allocations or perform transitions. Register only independently
// restorable units. Mutations must be reported after commands are ordered on the replay queue;
// uncertain/aborted execution must call Invalidate, never MarkResident. A Plan call records debt,
// not a successful copy. There is deliberately no "clear all dirty at end of replay" operation.
class ReplayResourceVersions
{
public:
  // capturedReadOnly needs complete capture-wide CPU/GPU write provenance, not resource flags or
  // absent usage records. knownAliasGroup is a backing allocation identity, or null if unaliased.
  bool Register(ResourceId resource, uint32_t subresource, uint64_t extent,
                bool capturedReadOnly = false, ResourceId knownAliasGroup = ResourceId());
  void Clear();

  // Invalid/unregistered ranges return one Restore item without mutating the ledger.
  rdcarray<ReplayResourceRestoreItem> Plan(const ReplayResourceRange &range,
                                           const ReplayResourceVersion &target,
                                           const ReplayResourceAccessProof &proof = {});
  // A completed overwrite also supersedes deferred initial bytes, only inside this range.
  bool MarkResident(const ReplayResourceRange &range, const ReplayResourceVersion &version);
  bool Invalidate(const ReplayResourceRange &range);
  void InvalidateAll();

  // Inspection also needs materialization: ask Plan with a complete Read proof first. Returned
  // debt persists until a corresponding MarkResident or a new Plan establishes the right version.
  rdcarray<ReplayResourceRange> Pending() const;
  bool IsResident(const ReplayResourceRange &range, const ReplayResourceVersion &version) const;

  // affected includes the edited bytes AND transitively dependent outputs. Unknown shader/alias
  // dependencies require dependencyClosureComplete=false, invalidating every mutable unit. Truly
  // read-only units remain shared unless they are explicitly included or alias an affected unit.
  uint64_t InvalidateForEdit(const rdcarray<ReplayResourceRange> &affected,
                             bool dependencyClosureComplete);
  uint64_t EditGeneration() const { return m_EditGeneration; }

private:
  struct Key
  {
    ResourceId resource;
    uint32_t subresource = 0;
    bool operator<(const Key &other) const
    {
      return resource < other.resource ||
             (resource == other.resource && subresource < other.subresource);
    }
  };
  struct Span
  {
    uint64_t begin = 0, end = 0;
    ReplayResourceVersion resident, wanted;
    uint64_t minimumEditGeneration = 0;
    bool pending = false;
  };
  struct Resource
  {
    uint64_t extent = 0;
    bool capturedReadOnly = false;
    ResourceId aliasGroup;
    rdcarray<Span> spans;
  };
  typedef std::map<Key, Resource> Resources;
  Resources m_Resources;
  uint64_t m_EditGeneration = 0;

  Resources::iterator Find(const ReplayResourceRange &range);
  Resources::const_iterator Find(const ReplayResourceRange &range) const;
  static void Split(Resource &resource, uint64_t position);
  static void Coalesce(Resource &resource);
  void InvalidateAliases(const Key &key, ResourceId group, uint64_t minimumEditGeneration);
};
