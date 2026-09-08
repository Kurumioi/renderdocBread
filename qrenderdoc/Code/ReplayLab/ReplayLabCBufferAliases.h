/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 The RenderDoc Authors
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

#include <QCoreApplication>
#include <QList>
#include <QString>

struct ConstantBlock;
class QWidget;

// Identity of one reflected constant-buffer variable inside its block, as the CBuffer panel
// shows it.
struct ReplayLabCBufferAliasKey
{
  QString path;       // reflected variable path: "v0", "light.color", "bones[2]"
  qint64 offset = -1; // absolute byte offset of a leaf inside the block, -1 for non-leaves
};

struct ReplayLabCBufferAlias
{
  QString alias;
  QString source;    // "user" or "ai"
  bool valid() const { return !alias.isEmpty(); }
};

// Per-user display names for constant-buffer blocks and variables. The captured shader and every
// workspace source keep their original identifiers; an alias only changes what the panel shows.
//
// Aliases follow the constant block's *layout* (name + top-level members and offsets), not one
// shader or one capture: every shader in every capture of the same game that binds a block with
// this layout shows the same aliases.
//
// Storage is shared with the ReplayLab Host (replaylab/mcp_host/src/shader_workspace/aliases.py),
// which publishes AI renames with source "ai". Keep the file format and the layout signature in
// sync with that module:
//   %LOCALAPPDATA%\ReplayLab\aliases\cbuffer\<layoutSha256>.json
class ReplayLabCBufferAliases
{
  Q_DECLARE_TR_FUNCTIONS(ReplayLabCBufferAliases)

public:
  static QString StoreRoot();
  // "<block>\n<name>@<offset>\n..." over the top-level members; empty when it cannot be built.
  static QString LayoutSignature(const ConstantBlock &block);
  static QString LayoutHash(const ConstantBlock &block);
  static bool IsValidAlias(const QString &alias, QString *error = NULL);

  // (Re)reads the alias file of this block layout. Cheap enough to call on every panel refresh.
  void Load(const ConstantBlock *block);
  bool Loaded() const { return !m_LayoutSha256.isEmpty(); }
  QString LayoutSha256() const { return m_LayoutSha256; }

  ReplayLabCBufferAlias Resolve(const ReplayLabCBufferAliasKey &key) const;
  bool Set(const ReplayLabCBufferAliasKey &key, const QString &alias, QString *error);
  bool Remove(const ReplayLabCBufferAliasKey &key, QString *error);
  ReplayLabCBufferAlias BlockAlias() const { return m_BlockAlias; }
  bool SetBlockAlias(const QString &alias, QString *error);
  bool RemoveBlockAlias(QString *error);

  // Modal rename dialog. Returns false when cancelled; an empty alias means "remove".
  static bool PromptAlias(QWidget *parent, const QString &original,
                          const ReplayLabCBufferAlias &current, QString &alias);
  static bool PromptBlockAlias(QWidget *parent, const QString &original,
                               const ReplayLabCBufferAlias &current, QString &alias);

private:
  struct Entry
  {
    QString path;
    qint64 offset = -1;
    QString alias;
    QString source;
    QString updatedAt;
  };
  struct Member
  {
    QString name;
    qint64 offset = -1;
  };

  static QString LayoutFile(const QString &layoutSha256);
  static bool ReadFile(const QString &path, QList<Entry> &entries,
                       ReplayLabCBufferAlias &blockAlias, QString &blockAliasUpdatedAt,
                       QString *error);
  bool WriteFile(const QList<Entry> &entries, const ReplayLabCBufferAlias &blockAlias,
                 const QString &blockAliasUpdatedAt, QString *error) const;
  static int FindExact(const QList<Entry> &entries, const ReplayLabCBufferAliasKey &key);
  bool Update(const ReplayLabCBufferAliasKey &key, const QString &alias, QString *error);
  bool UpdateBlockAlias(const QString &alias, QString *error);

  QString m_LayoutSha256;
  QString m_BlockName;
  QList<Member> m_Members;
  QList<Entry> m_Entries;
  ReplayLabCBufferAlias m_BlockAlias;
  QString m_BlockAliasUpdatedAt;
};
