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

#include "ReplayLabCBufferAliases.h"

#include <QCryptographicHash>
#include <QDateTime>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QFormLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QSaveFile>
#include <QVBoxLayout>
#include "Code/QRDUtils.h"

namespace
{
// i18n-ignore: fixed on-disk schema/protocol identifiers shared with the Python Host.
const QLatin1String AliasSchema("replaylab-cbuffer-aliases/v2");
const QLatin1String SourceUser("user");
const QLatin1String SourceAI("ai");
const int MaxAliasChars = 64;
const int MaxEntries = 4096;
const qint64 MaxFileBytes = 4 * 1024 * 1024;

QString nowUtc()
{
  return QDateTime::currentDateTimeUtc().toString(Qt::ISODate);
}
}    // namespace

QString ReplayLabCBufferAliases::StoreRoot()
{
  const QByteArray envRoot = qgetenv("REPLAYLAB_ALIAS_ROOT");
  if(!envRoot.isEmpty())
    return QDir::fromNativeSeparators(QString::fromLocal8Bit(envRoot));
  const QByteArray localAppData = qgetenv("LOCALAPPDATA");
  // i18n-ignore: fixed fallback data path when LOCALAPPDATA is unset, never shown to the user.
  const QString fallbackRoot = QDir::homePath() + lit("/AppData/Local");
  const QString localRoot =
      localAppData.isEmpty() ? fallbackRoot : QString::fromLocal8Bit(localAppData);
  // i18n-ignore: fixed ReplayLab per-user data directory, never shown to the user.
  return QDir::cleanPath(QDir(localRoot).absoluteFilePath(lit("ReplayLab/aliases/cbuffer")));
}

QString ReplayLabCBufferAliases::LayoutSignature(const ConstantBlock &block)
{
  // Must stay byte-identical with aliases.layout_signature() in the Python Host.
  QString signature = QString(block.name);
  for(const ShaderConstant &constant : block.variables)
  {
    if(constant.name.empty())
      return QString();
    signature += QLatin1Char('\n');
    signature += QString(constant.name);
    signature += QLatin1Char('@');
    signature += QString::number(constant.byteOffset);
  }
  return signature;
}

QString ReplayLabCBufferAliases::LayoutHash(const ConstantBlock &block)
{
  const QString signature = LayoutSignature(block);
  if(signature.isEmpty())
    return QString();
  return QString::fromLatin1(
      QCryptographicHash::hash(signature.toUtf8(), QCryptographicHash::Sha256).toHex());
}

bool ReplayLabCBufferAliases::IsValidAlias(const QString &alias, QString *error)
{
  if(alias.trimmed().isEmpty())
  {
    if(error)
      *error = tr("The alias cannot be empty.");
    return false;
  }
  if(alias != alias.trimmed())
  {
    if(error)
      *error = tr("The alias cannot start or end with whitespace.");
    return false;
  }
  if(alias.size() > MaxAliasChars)
  {
    if(error)
      *error = tr("The alias cannot be longer than %1 characters.").arg(MaxAliasChars);
    return false;
  }
  for(const QChar c : alias)
  {
    if(!c.isPrint() || c == QLatin1Char('\n') || c == QLatin1Char('\r') || c == QLatin1Char('\t'))
    {
      if(error)
        *error = tr("The alias cannot contain control characters or line breaks.");
      return false;
    }
  }
  return true;
}

QString ReplayLabCBufferAliases::LayoutFile(const QString &layoutSha256)
{
  // i18n-ignore: file extension, not user-facing text.
  return QDir(StoreRoot()).absoluteFilePath(layoutSha256.toLower() + lit(".json"));
}

bool ReplayLabCBufferAliases::ReadFile(const QString &path, QList<Entry> &entries,
                                       ReplayLabCBufferAlias &blockAlias,
                                       QString &blockAliasUpdatedAt, QString *error)
{
  entries.clear();
  blockAlias = ReplayLabCBufferAlias();
  blockAliasUpdatedAt.clear();
  QFile file(path);
  if(!file.exists())
    return true;
  if(file.size() <= 0 || file.size() > MaxFileBytes)
  {
    if(error)
      *error = tr("The alias file %1 has an invalid size.").arg(QDir::toNativeSeparators(path));
    return false;
  }
  if(!file.open(QIODevice::ReadOnly))
  {
    if(error)
      *error = tr("The alias file %1 could not be read: %2")
                   .arg(QDir::toNativeSeparators(path), file.errorString());
    return false;
  }
  QJsonParseError parseError;
  const QJsonDocument doc = QJsonDocument::fromJson(file.readAll(), &parseError);
  if(parseError.error != QJsonParseError::NoError || !doc.isObject())
  {
    if(error)
      *error = tr("The alias file %1 is not valid JSON: %2")
                   .arg(QDir::toNativeSeparators(path), parseError.errorString());
    return false;
  }
  const QJsonObject root = doc.object();
  if(root.value(QStringLiteral("schema")).toString() != AliasSchema)
  {
    if(error)
      *error = tr("The alias file %1 has an unknown schema.").arg(QDir::toNativeSeparators(path));
    return false;
  }
  const QJsonObject blockAliasObject = root.value(QStringLiteral("blockAlias")).toObject();
  const QString blockAliasText = blockAliasObject.value(QStringLiteral("alias")).toString();
  if(IsValidAlias(blockAliasText))
  {
    blockAlias.alias = blockAliasText;
    blockAlias.source = blockAliasObject.value(QStringLiteral("source")).toString() == SourceUser
                            ? SourceUser
                            : SourceAI;
    blockAliasUpdatedAt = blockAliasObject.value(QStringLiteral("updatedAt")).toString();
  }
  for(const QJsonValue &value : root.value(QStringLiteral("aliases")).toArray())
  {
    const QJsonObject item = value.toObject();
    Entry entry;
    entry.path = item.value(QStringLiteral("path")).toString();
    entry.offset = (qint64)item.value(QStringLiteral("offset")).toDouble(-1.0);
    entry.alias = item.value(QStringLiteral("alias")).toString();
    entry.source =
        item.value(QStringLiteral("source")).toString() == SourceUser ? SourceUser : SourceAI;
    entry.updatedAt = item.value(QStringLiteral("updatedAt")).toString();
    if(entry.path.isEmpty() || !IsValidAlias(entry.alias))
      continue;
    entries.push_back(entry);
    if(entries.count() >= MaxEntries)
      break;
  }
  return true;
}

bool ReplayLabCBufferAliases::WriteFile(const QList<Entry> &entries,
                                        const ReplayLabCBufferAlias &blockAlias,
                                        const QString &blockAliasUpdatedAt, QString *error) const
{
  const QString path = LayoutFile(m_LayoutSha256);
  QDir().mkpath(QFileInfo(path).absolutePath());
  QJsonArray array;
  for(const Entry &entry : entries)
  {
    QJsonObject item;
    item.insert(QStringLiteral("path"), entry.path);
    item.insert(QStringLiteral("offset"), (double)entry.offset);
    item.insert(QStringLiteral("alias"), entry.alias);
    item.insert(QStringLiteral("source"), entry.source);
    item.insert(QStringLiteral("updatedAt"), entry.updatedAt);
    array.push_back(item);
  }
  QJsonArray members;
  for(const Member &member : m_Members)
  {
    QJsonObject item;
    item.insert(QStringLiteral("name"), member.name);
    item.insert(QStringLiteral("offset"), (double)member.offset);
    members.push_back(item);
  }
  QJsonObject root;
  root.insert(QStringLiteral("schema"), AliasSchema);
  root.insert(QStringLiteral("layoutSha256"), m_LayoutSha256);
  root.insert(QStringLiteral("block"), m_BlockName);
  root.insert(QStringLiteral("members"), members);
  root.insert(QStringLiteral("updatedAt"), nowUtc());
  root.insert(QStringLiteral("aliases"), array);
  if(blockAlias.valid())
  {
    QJsonObject item;
    item.insert(QStringLiteral("alias"), blockAlias.alias);
    item.insert(QStringLiteral("source"), blockAlias.source);
    item.insert(QStringLiteral("updatedAt"), blockAliasUpdatedAt);
    root.insert(QStringLiteral("blockAlias"), item);
  }

  QSaveFile file(path);
  if(!file.open(QIODevice::WriteOnly | QIODevice::Truncate))
  {
    if(error)
      *error = tr("The alias file %1 could not be written: %2")
                   .arg(QDir::toNativeSeparators(path), file.errorString());
    return false;
  }
  file.write(QJsonDocument(root).toJson(QJsonDocument::Indented));
  if(!file.commit())
  {
    if(error)
      *error = tr("The alias file %1 could not be written: %2")
                   .arg(QDir::toNativeSeparators(path), file.errorString());
    return false;
  }
  return true;
}

void ReplayLabCBufferAliases::Load(const ConstantBlock *block)
{
  m_LayoutSha256.clear();
  m_BlockName.clear();
  m_Members.clear();
  m_Entries.clear();
  m_BlockAlias = ReplayLabCBufferAlias();
  m_BlockAliasUpdatedAt.clear();
  if(block == NULL)
    return;
  m_LayoutSha256 = LayoutHash(*block);
  if(m_LayoutSha256.isEmpty())
    return;
  m_BlockName = QString(block->name);
  for(const ShaderConstant &constant : block->variables)
  {
    Member member;
    member.name = QString(constant.name);
    member.offset = constant.byteOffset;
    m_Members.push_back(member);
  }
  ReadFile(LayoutFile(m_LayoutSha256), m_Entries, m_BlockAlias, m_BlockAliasUpdatedAt, NULL);
}

int ReplayLabCBufferAliases::FindExact(const QList<Entry> &entries,
                                       const ReplayLabCBufferAliasKey &key)
{
  int found = -1;
  for(int i = 0; i < entries.count(); i++)
  {
    if(entries[i].path != key.path)
      continue;
    // a user entry beats an AI entry for the same variable
    if(found < 0 || (entries[found].source != SourceUser && entries[i].source == SourceUser))
      found = i;
  }
  return found;
}

ReplayLabCBufferAlias ReplayLabCBufferAliases::Resolve(const ReplayLabCBufferAliasKey &key) const
{
  ReplayLabCBufferAlias result;
  if(key.path.isEmpty() || !Loaded())
    return result;

  int index = FindExact(m_Entries, key);
  if(index < 0 && key.offset >= 0)
  {
    // The Host writes decompiler names, which can differ from RenderDoc's reflected names for
    // nested members; a leaf at the same byte offset of the same block is the same variable.
    for(int i = 0; i < m_Entries.count(); i++)
    {
      const Entry &entry = m_Entries[i];
      if(entry.offset == key.offset &&
         (index < 0 || (m_Entries[index].source != SourceUser && entry.source == SourceUser)))
        index = i;
    }
  }
  if(index >= 0)
  {
    result.alias = m_Entries[index].alias;
    result.source = m_Entries[index].source;
  }
  return result;
}

bool ReplayLabCBufferAliases::Update(const ReplayLabCBufferAliasKey &key, const QString &alias,
                                     QString *error)
{
  if(key.path.isEmpty())
    return false;
  if(!Loaded())
  {
    if(error)
      *error = tr("This constant cannot be aliased because its block layout is unknown.");
    return false;
  }
  QList<Entry> entries;
  ReplayLabCBufferAlias blockAlias;
  QString blockAliasUpdatedAt;
  if(!ReadFile(LayoutFile(m_LayoutSha256), entries, blockAlias, blockAliasUpdatedAt, error))
    return false;

  // replace every entry for this variable, whatever wrote it
  bool changed = false;
  for(int i = entries.count() - 1; i >= 0; i--)
  {
    if(entries[i].path == key.path)
    {
      entries.removeAt(i);
      changed = true;
    }
  }
  if(alias.isEmpty() && !changed)
    return true;
  if(!alias.isEmpty())
  {
    if(entries.count() >= MaxEntries)
    {
      if(error)
        *error = tr("The alias file already holds %1 entries.").arg(MaxEntries);
      return false;
    }
    Entry entry;
    entry.path = key.path;
    entry.offset = key.offset;
    entry.alias = alias;
    entry.source = SourceUser;
    entry.updatedAt = nowUtc();
    entries.push_back(entry);
  }
  if(!WriteFile(entries, blockAlias, blockAliasUpdatedAt, error))
    return false;
  m_Entries = entries;
  m_BlockAlias = blockAlias;
  m_BlockAliasUpdatedAt = blockAliasUpdatedAt;
  return true;
}

bool ReplayLabCBufferAliases::Set(const ReplayLabCBufferAliasKey &key, const QString &alias,
                                  QString *error)
{
  if(!IsValidAlias(alias, error))
    return false;
  return Update(key, alias, error);
}

bool ReplayLabCBufferAliases::Remove(const ReplayLabCBufferAliasKey &key, QString *error)
{
  return Update(key, QString(), error);
}

bool ReplayLabCBufferAliases::UpdateBlockAlias(const QString &alias, QString *error)
{
  if(!Loaded())
  {
    if(error)
      *error = tr("This constant block cannot be aliased because its layout is unknown.");
    return false;
  }

  QList<Entry> entries;
  ReplayLabCBufferAlias blockAlias;
  QString blockAliasUpdatedAt;
  if(!ReadFile(LayoutFile(m_LayoutSha256), entries, blockAlias, blockAliasUpdatedAt, error))
    return false;
  if(alias.isEmpty() && !blockAlias.valid())
    return true;
  if(alias.isEmpty())
  {
    blockAlias = ReplayLabCBufferAlias();
    blockAliasUpdatedAt.clear();
  }
  else
  {
    blockAlias.alias = alias;
    blockAlias.source = SourceUser;
    blockAliasUpdatedAt = nowUtc();
  }
  if(!WriteFile(entries, blockAlias, blockAliasUpdatedAt, error))
    return false;
  m_Entries = entries;
  m_BlockAlias = blockAlias;
  m_BlockAliasUpdatedAt = blockAliasUpdatedAt;
  return true;
}

bool ReplayLabCBufferAliases::SetBlockAlias(const QString &alias, QString *error)
{
  if(!IsValidAlias(alias, error))
    return false;
  return UpdateBlockAlias(alias, error);
}

bool ReplayLabCBufferAliases::RemoveBlockAlias(QString *error)
{
  return UpdateBlockAlias(QString(), error);
}

bool ReplayLabCBufferAliases::PromptAlias(QWidget *parent, const QString &original,
                                          const ReplayLabCBufferAlias &current, QString &alias)
{
  QDialog dialog(parent);
  dialog.setWindowTitle(tr("Rename Constant (Alias)"));

  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QLabel *intro = new QLabel(
      tr("The alias is only a display name. It applies to every shader and every capture whose "
         "constant block has this name and layout; the shader, its reflection and every "
         "workspace source keep the original name."),
      &dialog);
  intro->setWordWrap(true);
  layout->addWidget(intro);

  QFormLayout *form = new QFormLayout();
  QLineEdit *originalEdit = new QLineEdit(original, &dialog);
  originalEdit->setReadOnly(true);
  form->addRow(tr("Original name:"), originalEdit);

  QLineEdit *aliasEdit = new QLineEdit(current.alias, &dialog);
  aliasEdit->setMaxLength(MaxAliasChars);
  aliasEdit->setPlaceholderText(tr("Leave empty to remove the alias"));
  form->addRow(tr("Alias:"), aliasEdit);

  QLabel *sourceLabel = new QLabel(&dialog);
  if(current.valid())
    sourceLabel->setText(current.source == SourceUser ? tr("Current alias was set by you.")
                                                      : tr("Current alias was proposed by AI."));
  else
    sourceLabel->setText(tr("No alias is set for this constant."));
  form->addRow(QString(), sourceLabel);
  layout->addLayout(form);

  QLabel *errorLabel = new QLabel(&dialog);
  errorLabel->setWordWrap(true);
  errorLabel->setVisible(false);
  layout->addWidget(errorLabel);

  QDialogButtonBox *buttons =
      new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
  layout->addWidget(buttons);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, [&dialog, aliasEdit, errorLabel]() {
    QString problem;
    const QString text = aliasEdit->text();
    if(text.trimmed().isEmpty() || IsValidAlias(text, &problem))
    {
      dialog.accept();
      return;
    }
    errorLabel->setText(problem);
    errorLabel->setVisible(true);
  });

  aliasEdit->setFocus();
  aliasEdit->selectAll();
  dialog.setMinimumWidth(420);

  if(RDDialog::show(&dialog) != QDialog::Accepted)
    return false;

  alias = aliasEdit->text().trimmed();
  return true;
}

bool ReplayLabCBufferAliases::PromptBlockAlias(QWidget *parent, const QString &original,
                                               const ReplayLabCBufferAlias &current,
                                               QString &alias)
{
  QDialog dialog(parent);
  dialog.setWindowTitle(tr("Rename Constant Block (Alias)"));

  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QLabel *intro = new QLabel(
      tr("The alias is only a display name for this constant block. It applies to every shader "
         "and every capture with the same block layout; shader code, reflection and workspace "
         "source keep the original name."),
      &dialog);
  intro->setWordWrap(true);
  layout->addWidget(intro);

  QFormLayout *form = new QFormLayout();
  QLineEdit *originalEdit = new QLineEdit(original, &dialog);
  originalEdit->setReadOnly(true);
  form->addRow(tr("Original name:"), originalEdit);

  QLineEdit *aliasEdit = new QLineEdit(current.alias, &dialog);
  aliasEdit->setMaxLength(MaxAliasChars);
  aliasEdit->setPlaceholderText(tr("Leave empty to remove the alias"));
  form->addRow(tr("Alias:"), aliasEdit);

  QLabel *sourceLabel = new QLabel(&dialog);
  if(current.valid())
    sourceLabel->setText(current.source == SourceUser ? tr("Current alias was set by you.")
                                                      : tr("Current alias was proposed by AI."));
  else
    sourceLabel->setText(tr("No alias is set for this constant block."));
  form->addRow(QString(), sourceLabel);
  layout->addLayout(form);

  QLabel *errorLabel = new QLabel(&dialog);
  errorLabel->setWordWrap(true);
  errorLabel->setVisible(false);
  layout->addWidget(errorLabel);

  QDialogButtonBox *buttons =
      new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, &dialog);
  layout->addWidget(buttons);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog,
                   [&dialog, aliasEdit, errorLabel]() {
                     QString problem;
                     const QString text = aliasEdit->text();
                     if(text.trimmed().isEmpty() || IsValidAlias(text, &problem))
                     {
                       dialog.accept();
                       return;
                     }
                     errorLabel->setText(problem);
                     errorLabel->setVisible(true);
                   });

  aliasEdit->setFocus();
  aliasEdit->selectAll();
  dialog.setMinimumWidth(420);

  if(RDDialog::show(&dialog) != QDialog::Accepted)
    return false;

  alias = aliasEdit->text().trimmed();
  return true;
}
