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

#include "AIConversationView.h"

#include <QCoreApplication>
#include <QCursor>
#include <QDateTime>
#include <QEvent>
#include <QHBoxLayout>
#include <QMouseEvent>
#include <QPainter>
#include <QPalette>
#include <QScrollBar>
#include <QTextBlock>
#include <QTextBrowser>
#include <QTextDocument>
#include <QTextFragment>
#include <QTimer>
#include <QToolTip>
#include <QUrlQuery>
#include <utility>

namespace
{
bool allowedLink(const QUrl &url)
{
  if(!url.isValid())
    return false;
  const QString scheme = url.scheme().toLower();
  return scheme == lit("http") || scheme == lit("https") || scheme == lit("rdoc");
}

// Render inline code and [label](target) links in one forward pass. A nested '[' invalidates the
// current link candidate, which keeps malformed model output from causing repeated rescans.
QString inlineMarkdown(const QString &text)
{
  QString html;
  html.reserve(text.size() + text.size() / 8);
  int plainStart = 0;
  int i = 0;
  while(i < text.size())
  {
    if(text[i] == QLatin1Char('`'))
    {
      const int end = text.indexOf(QLatin1Char('`'), i + 1);
      if(end < 0)
        break;
      html += text.mid(plainStart, i - plainStart).toHtmlEscaped();
      html += lit("<code>") + text.mid(i + 1, end - i - 1).toHtmlEscaped() + lit("</code>");
      i = end + 1;
      plainStart = i;
      continue;
    }

    if(text[i] == QLatin1Char('['))
    {
      int labelEnd = -1;
      for(int j = i + 1; j < text.size(); ++j)
      {
        if(text[j] == QLatin1Char('[') || text[j] == QLatin1Char('\n') ||
           text[j] == QLatin1Char('\r'))
          break;
        if(text[j] == QLatin1Char(']'))
        {
          labelEnd = j;
          break;
        }
      }

      if(labelEnd > i + 1 && labelEnd + 1 < text.size() && text[labelEnd + 1] == QLatin1Char('('))
      {
        int targetEnd = -1;
        for(int j = labelEnd + 2; j < text.size(); ++j)
        {
          if(text[j] == QLatin1Char(')'))
          {
            targetEnd = j;
            break;
          }
          if(text[j].isSpace() || text[j] == QLatin1Char('('))
            break;
        }

        if(targetEnd > labelEnd + 2)
        {
          const QString targetText = text.mid(labelEnd + 2, targetEnd - labelEnd - 2);
          const QUrl target(targetText, QUrl::StrictMode);
          if(allowedLink(target))
          {
            html += text.mid(plainStart, i - plainStart).toHtmlEscaped();
            html += lit("<a href=\"") + target.toString(QUrl::FullyEncoded).toHtmlEscaped() +
                    lit("\">") + text.mid(i + 1, labelEnd - i - 1).toHtmlEscaped() + lit("</a>");
            i = targetEnd + 1;
            plainStart = i;
            continue;
          }
        }
      }
    }
    ++i;
  }
  html += text.mid(plainStart).toHtmlEscaped();
  return html;
}

void closeList(QString &html, int &listKind)
{
  if(listKind == 1)
    html += lit("</ul>");
  else if(listKind == 2)
    html += lit("</ol>");
  listKind = 0;
}

bool orderedListLine(const QString &line, int &contentStart)
{
  int i = 0;
  while(i < line.size() && line[i].isDigit())
    ++i;
  if(i == 0 || i > 9 || i + 1 >= line.size() || line[i] != QLatin1Char('.') || !line[i + 1].isSpace())
    return false;
  contentStart = i + 2;
  return true;
}

bool sourceKeyword(const QString &word)
{
  static const QStringList keywords =
      QStringList() << QLatin1String("auto") << QLatin1String("break")
                    << QLatin1String("case") << QLatin1String("class")
                    << QLatin1String("const") << QLatin1String("continue")
                    << QLatin1String("discard") << QLatin1String("do")
                    << QLatin1String("else") << QLatin1String("enum")
                    << QLatin1String("false") << QLatin1String("for")
                    << QLatin1String("if") << QLatin1String("in")
                    << QLatin1String("interface")
                    << QLatin1String("namespace") << QLatin1String("null")
                    << QLatin1String("nullptr") << QLatin1String("out")
                    << QLatin1String("register") << QLatin1String("return") << QLatin1String("static")
                    << QLatin1String("struct") << QLatin1String("switch")
                    << QLatin1String("true") << QLatin1String("typedef")
                    << QLatin1String("uniform") << QLatin1String("using")
                    << QLatin1String("var") << QLatin1String("while");
  return keywords.contains(word);
}

bool sourceType(const QString &word)
{
  static const QStringList types =
      QStringList() << QLatin1String("bool") << QLatin1String("buffer")
                    << QLatin1String("byte") << QLatin1String("cbuffer")
                    << QLatin1String("char") << QLatin1String("double")
                    << QLatin1String("fixed") << QLatin1String("float")
                    << QLatin1String("half") << QLatin1String("int")
                    << QLatin1String("long") << QLatin1String("matrix")
                    << QLatin1String("object") << QLatin1String("short")
                    << QLatin1String("size_t") << QLatin1String("string")
                    << QLatin1String("uint") << QLatin1String("ulong")
                    << QLatin1String("ushort") << QLatin1String("void");
  if(types.contains(word))
    return true;

  // Cover shader vector/matrix spellings (float4, half3x3, ivec2, mat4, etc.) without
  // accepting arbitrary identifiers that merely start with a primitive type name.
  static const QStringList numericPrefixes =
      QStringList() << QLatin1String("float") << QLatin1String("half")
                    << QLatin1String("double") << QLatin1String("fixed")
                    << QLatin1String("uint") << QLatin1String("int")
                    << QLatin1String("bool") << QLatin1String("vec")
                    << QLatin1String("ivec") << QLatin1String("uvec")
                    << QLatin1String("bvec") << QLatin1String("dvec")
                    << QLatin1String("mat") << QLatin1String("dmat");
  for(const QString &prefix : numericPrefixes)
  {
    if(word.startsWith(prefix) && word.size() > prefix.size() && word[prefix.size()].isDigit())
      return true;
  }

  // Rider treats named types as a distinct semantic category. An initial upper-case character
  // gives useful, language-agnostic coverage for C++, C#, Java and shader resource types.
  return !word.isEmpty() && word[0].isUpper();
}

QString highlightedSourceLine(const QString &line, const QString &language)
{
  const QString loweredLanguage = language.toLower();
  const bool hashComment =
      loweredLanguage == QLatin1String("python") || loweredLanguage == QLatin1String("py") ||
      loweredLanguage == QLatin1String("bash") || loweredLanguage == QLatin1String("shell") ||
      loweredLanguage == QLatin1String("sh") ||
      loweredLanguage == QLatin1String("powershell") || loweredLanguage == QLatin1String("ps1");
  QString html;
  int i = 0;
  while(i < line.size())
  {
    if(i + 1 < line.size() && line[i] == QLatin1Char('/') &&
       line[i + 1] == QLatin1Char('/'))
    {
      html += QLatin1String("<span class=\"source-comment\">") + line.mid(i).toHtmlEscaped() +
              QLatin1String("</span>");
      break;
    }
    if(i + 1 < line.size() && line[i] == QLatin1Char('/') &&
       line[i + 1] == QLatin1Char('*'))
    {
      const int end = line.indexOf(QLatin1String("*/"), i + 2);
      const int length = end < 0 ? line.size() - i : end + 2 - i;
      html += QLatin1String("<span class=\"source-comment\">") +
              line.mid(i, length).toHtmlEscaped() + QLatin1String("</span>");
      i += length;
      continue;
    }
    if(hashComment && line[i] == QLatin1Char('#'))
    {
      html += QLatin1String("<span class=\"source-comment\">") + line.mid(i).toHtmlEscaped() +
              QLatin1String("</span>");
      break;
    }
    if(line[i] == QLatin1Char('"') || line[i] == QLatin1Char('\''))
    {
      const QChar quote = line[i];
      const int start = i++;
      bool escaped = false;
      while(i < line.size())
      {
        const QChar ch = line[i++];
        if(ch == quote && !escaped)
          break;
        if(ch == QLatin1Char('\\') && !escaped)
          escaped = true;
        else
          escaped = false;
      }
      int next = i;
      while(next < line.size() && line[next].isSpace())
        ++next;
      const QString cssClass = next < line.size() && line[next] == QLatin1Char(':')
                                   ? QLatin1String("source-key")
                                   : QLatin1String("source-string");
      html += QLatin1String("<span class=\"") + cssClass + QLatin1String("\">") +
              line.mid(start, i - start).toHtmlEscaped() + QLatin1String("</span>");
      continue;
    }
    if(line[i].isLetter() || line[i] == QLatin1Char('_'))
    {
      const int start = i++;
      while(i < line.size() && (line[i].isLetterOrNumber() || line[i] == QLatin1Char('_')))
        ++i;
      const QString word = line.mid(start, i - start);
      int next = i;
      while(next < line.size() && line[next].isSpace())
        ++next;
      if(sourceKeyword(word))
        html += QLatin1String("<span class=\"source-keyword\">") + word.toHtmlEscaped() +
                QLatin1String("</span>");
      else if(next < line.size() && line[next] == QLatin1Char(':'))
        html += QLatin1String("<span class=\"source-key\">") + word.toHtmlEscaped() +
                QLatin1String("</span>");
      else if(next < line.size() && line[next] == QLatin1Char('('))
        html += QLatin1String("<span class=\"source-function\">") + word.toHtmlEscaped() +
                QLatin1String("</span>");
      else if(sourceType(word))
        html += QLatin1String("<span class=\"source-type\">") + word.toHtmlEscaped() +
                QLatin1String("</span>");
      else
        html += QLatin1String("<span class=\"source-variable\">") + word.toHtmlEscaped() +
                QLatin1String("</span>");
      continue;
    }
    if(line[i].isDigit() ||
       (line[i] == QLatin1Char('-') && i + 1 < line.size() && line[i + 1].isDigit()))
    {
      const int start = i++;
      while(i < line.size() &&
            (line[i].isDigit() || line[i] == QLatin1Char('.') ||
             line[i] == QLatin1Char('x') || line[i] == QLatin1Char('X') ||
             (line[i].toLower() >= QLatin1Char('a') &&
              line[i].toLower() <= QLatin1Char('f'))))
        ++i;
      html += QLatin1String("<span class=\"source-number\">") +
              line.mid(start, i - start).toHtmlEscaped() + QLatin1String("</span>");
      continue;
    }
    html += QString(line[i]).toHtmlEscaped();
    ++i;
  }
  return html;
}

QString renderFencedCode(const QStringList &lines, QString language)
{
  language = language.trimmed().section(QLatin1Char(' '), 0, 0).left(32);
  QString label = language;
  if(label.isEmpty())
    label = QCoreApplication::translate("AIConversationView", "Code");

  QString html = QLatin1String("<table class=\"source-block\" width=\"100%\" border=\"0\" ") +
                 QLatin1String("cellspacing=\"0\" cellpadding=\"0\"><tr><td colspan=\"2\" ") +
                 QLatin1String("class=\"source-header\">") + label.toHtmlEscaped() +
                 QLatin1String("</td></tr>");
  const int lineCount = qMax(1, lines.size());
  for(int lineIndex = 0; lineIndex < lineCount; ++lineIndex)
  {
    const QString line = lineIndex < lines.size() ? lines[lineIndex] : QString();
    html += QLatin1String("<tr><td class=\"source-line-number\" width=\"36\" align=\"right\" ") +
            QLatin1String("valign=\"top\">") + QString::number(lineIndex + 1) +
            QLatin1String("</td><td class=\"source-line\" valign=\"top\">") +
            (line.isEmpty() ? QLatin1String("&nbsp;") : highlightedSourceLine(line, language)) +
            QLatin1String("</td></tr>");
  }
  return html + QLatin1String("</table>");
}

// Raw HTML and images are intentionally unsupported. This parser is small, deterministic, and
// linear for the bounded transcript sizes used by AIAssistant.
QString safeMarkdown(QString text)
{
  text.replace(lit("\r\n"), lit("\n"));
  text.replace(QLatin1Char('\r'), QLatin1Char('\n'));
  const QStringList lines = text.split(QLatin1Char('\n'), QString::KeepEmptyParts);
  QString html;
  html.reserve(text.size() + text.size() / 4);
  bool codeBlock = false;
  QString codeLanguage;
  QStringList codeLines;
  int listKind = 0;

  for(const QString &line : lines)
  {
    const QString trimmed = line.trimmed();
    if(trimmed.startsWith(lit("```")))
    {
      closeList(html, listKind);
      if(codeBlock)
      {
        html += renderFencedCode(codeLines, codeLanguage);
        codeLines.clear();
        codeLanguage.clear();
      }
      else
      {
        codeLanguage = trimmed.mid(3).trimmed();
      }
      codeBlock = !codeBlock;
      continue;
    }
    if(codeBlock)
    {
      codeLines.push_back(line);
      continue;
    }
    if(trimmed.isEmpty())
    {
      closeList(html, listKind);
      continue;
    }

    int heading = 0;
    while(heading < line.size() && heading < 4 && line[heading] == QLatin1Char('#'))
      ++heading;
    if(heading > 0 && heading < line.size() && line[heading].isSpace())
    {
      closeList(html, listKind);
      html += lit("<h") + QString::number(heading + 2) + lit(">") +
              inlineMarkdown(line.mid(heading + 1)) + lit("</h") + QString::number(heading + 2) +
              lit(">");
      continue;
    }

    if(line.startsWith(lit("- ")) || line.startsWith(lit("* ")))
    {
      if(listKind != 1)
      {
        closeList(html, listKind);
        html += lit("<ul>");
        listKind = 1;
      }
      html += lit("<li>") + inlineMarkdown(line.mid(2)) + lit("</li>");
      continue;
    }

    int orderedStart = 0;
    if(orderedListLine(line, orderedStart))
    {
      if(listKind != 2)
      {
        closeList(html, listKind);
        html += lit("<ol>");
        listKind = 2;
      }
      html += lit("<li>") + inlineMarkdown(line.mid(orderedStart)) + lit("</li>");
      continue;
    }

    closeList(html, listKind);
    if(line.startsWith(lit("> ")))
      html += lit("<blockquote>") + inlineMarkdown(line.mid(2)) + lit("</blockquote>");
    else
      html += lit("<p>") + inlineMarkdown(line) + lit("</p>");
  }

  closeList(html, listKind);
  if(codeBlock)
    html += renderFencedCode(codeLines, codeLanguage);
  return html;
}

QString activityState(const AIActivityItem &activity)
{
  if(activity.kind == lit("queued"))
    return QCoreApplication::translate("AIConversationView", "queued");
  if(activity.kind == lit("cancelled"))
    return QCoreApplication::translate("AIConversationView", "cancelled");
  if(activity.kind == lit("interrupted"))
    return QCoreApplication::translate("AIConversationView", "interrupted");
  if(activity.kind == lit("superseded"))
    return QCoreApplication::translate("AIConversationView", "redirected");
  if(activity.kind == lit("steered"))
    return QCoreApplication::translate("AIConversationView", "adjusted");
  if(activity.kind == lit("approval"))
    return QCoreApplication::translate("AIConversationView", "attention");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(activity.kind == lit("needs-user-input"))
    return QCoreApplication::translate("AIConversationView", "waiting for input");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(activity.kind == lit("blocked"))
    return QCoreApplication::translate("AIConversationView", "blocked");
  if(activity.error)
    return QCoreApplication::translate("AIConversationView", "failed");
  if(activity.completed)
    return QCoreApplication::translate("AIConversationView", "completed");
  return QCoreApplication::translate("AIConversationView", "running");
}

QString messageStateLabel(const QString &state)
{
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("queued"))
    return QCoreApplication::translate("AIConversationView", "queued");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("running"))
    return QCoreApplication::translate("AIConversationView", "running");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("planning"))
    return QCoreApplication::translate("AIConversationView", "planning");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("finalizing"))
    return QCoreApplication::translate("AIConversationView", "finalizing");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("cancelled"))
    return QCoreApplication::translate("AIConversationView", "cancelled");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("interrupted"))
    return QCoreApplication::translate("AIConversationView", "interrupted");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("superseded"))
    return QCoreApplication::translate("AIConversationView", "redirected");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("steered"))
    return QCoreApplication::translate("AIConversationView", "adjusted");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("failed"))
    return QCoreApplication::translate("AIConversationView", "failed");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("completed"))
    return QCoreApplication::translate("AIConversationView", "completed");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("needs-user-input"))
    return QCoreApplication::translate("AIConversationView", "waiting for input");
  // i18n-ignore: Conversation state is an internal lifecycle identifier used only for lookup.
  if(state == lit("blocked"))
    return QCoreApplication::translate("AIConversationView", "blocked");
  return state;
}

bool isLiveState(const QString &state)
{
  // i18n-ignore: Conversation states are internal lifecycle identifiers used only for control flow.
  return state == lit("queued") || state == lit("running") || state == lit("planning") ||
         // i18n-ignore: Conversation state is an internal lifecycle identifier used only for control flow.
         state == lit("finalizing");
}

QString elapsedText(qint64 elapsedMs)
{
  const qint64 totalSeconds = qMax<qint64>(0, elapsedMs / 1000);
  const qint64 hours = totalSeconds / 3600;
  const int minutes = int((totalSeconds % 3600) / 60);
  const int seconds = int(totalSeconds % 60);

  if(hours > 0)
    return QCoreApplication::translate("AIConversationView", "%1 h %2 min %3 sec")
        .arg(hours)
        .arg(minutes)
        .arg(seconds);
  if(minutes > 0)
    return QCoreApplication::translate("AIConversationView", "%1 min %2 sec")
        .arg(minutes)
        .arg(seconds);
  return QCoreApplication::translate("AIConversationView", "%1 sec").arg(seconds);
}

QString activityKindLabel(const QString &kind)
{
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("tool"))
    return QCoreApplication::translate("AIConversationView", "Tool");
  // i18n-ignore: Activity kinds are backend protocol identifiers used only for label selection.
  if(kind == lit("planner") || kind == lit("plan"))
    return QCoreApplication::translate("AIConversationView", "Plan");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("reasoning"))
    return QCoreApplication::translate("AIConversationView", "Reasoning summary");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("commandExecution"))
    return QCoreApplication::translate("AIConversationView", "Command");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("mcpToolCall"))
    return QCoreApplication::translate("AIConversationView", "MCP tool");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("webSearch"))
    return QCoreApplication::translate("AIConversationView", "Web search");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("imageView"))
    return QCoreApplication::translate("AIConversationView", "Image");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("contextCompaction"))
    return QCoreApplication::translate("AIConversationView", "Context");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("providerStatus"))
    return QCoreApplication::translate("AIConversationView", "Provider");
  // i18n-ignore: Activity kind and prefix are backend protocol identifiers used for label selection.
  if(kind == lit("reviewer") || kind.startsWith(lit("reviewer-")))
    return QCoreApplication::translate("AIConversationView", "Review");
  // i18n-ignore: Activity kind prefix is a backend protocol identifier used for label selection.
  if(kind.startsWith(lit("specialist-")))
    return QCoreApplication::translate("AIConversationView", "Agent");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("approval"))
    return QCoreApplication::translate("AIConversationView", "Approval");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("needs-user-input"))
    return QCoreApplication::translate("AIConversationView", "Input needed");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("blocked"))
    return QCoreApplication::translate("AIConversationView", "Blocked");
  // i18n-ignore: Activity kind is a backend protocol identifier used only for label selection.
  if(kind == lit("completed"))
    return QCoreApplication::translate("AIConversationView", "Completed");
  // i18n-ignore: Activity kinds are backend protocol identifiers used only for label selection.
  if(kind == lit("steered") || kind == lit("superseded"))
    return QCoreApplication::translate("AIConversationView", "Direction");
  return kind.isEmpty() ? QCoreApplication::translate("AIConversationView", "Activity") : kind;
}

QString activityStatusClass(const AIActivityItem &activity)
{
  if(activity.error)
    // i18n-ignore: CSS class name is an internal selector, not user-visible text.
    return lit("activity-error");
  if(activity.completed)
    // i18n-ignore: CSS class name is an internal selector, not user-visible text.
    return lit("activity-completed");
  // i18n-ignore: CSS class name is an internal selector, not user-visible text.
  return lit("activity-running");
}

bool isCodeActivity(const QString &kind, const QString &detail)
{
  // i18n-ignore: Activity kinds are backend protocol identifiers used only for styling.
  if(kind == QLatin1String("commandExecution") || kind == QLatin1String("mcpToolCall"))
    return true;
  // Generic tools often report only a one-word lifecycle status; leave that in the timeline as
  // prose. Promote actual arguments, results, or multi-line payloads into a code surface.
  return kind == QLatin1String("tool") &&
         (detail.contains(QLatin1Char('\n')) || detail.contains(QLatin1Char('{')) ||
          detail.contains(QLatin1Char('[')));
}

QString highlightedDetailLine(const QString &line)
{
  QString html;
  int i = 0;
  while(i < line.size())
  {
    if(i + 1 < line.size() && line[i] == QLatin1Char('/') &&
       line[i + 1] == QLatin1Char('/'))
    {
      html += QLatin1String("<span class=\"detail-comment\">") +
              line.mid(i).toHtmlEscaped() + QLatin1String("</span>");
      break;
    }
    if(i + 1 < line.size() && line[i] == QLatin1Char('/') &&
       line[i + 1] == QLatin1Char('*'))
    {
      const int end = line.indexOf(QLatin1String("*/"), i + 2);
      const int length = end < 0 ? line.size() - i : end + 2 - i;
      html += QLatin1String("<span class=\"detail-comment\">") +
              line.mid(i, length).toHtmlEscaped() + QLatin1String("</span>");
      i += length;
      continue;
    }
    if(line[i] == QLatin1Char('#') &&
       (i == 0 || line[i - 1].isSpace()))
    {
      html += QLatin1String("<span class=\"detail-comment\">") +
              line.mid(i).toHtmlEscaped() + QLatin1String("</span>");
      break;
    }
    if(line[i] == QLatin1Char('"') || line[i] == QLatin1Char('\''))
    {
      const QChar quote = line[i];
      const int start = i++;
      bool escaped = false;
      while(i < line.size())
      {
        const QChar ch = line[i++];
        if(ch == quote && !escaped)
          break;
        if(ch == QLatin1Char('\\') && !escaped)
          escaped = true;
        else
          escaped = false;
      }
      int next = i;
      while(next < line.size() && line[next].isSpace())
        ++next;
      const QString cssClass = next < line.size() && line[next] == QLatin1Char(':')
                                   ? QLatin1String("detail-key")
                                   : QLatin1String("detail-string");
      html += QLatin1String("<span class=\"") + cssClass + QLatin1String("\">") +
              line.mid(start, i - start).toHtmlEscaped() + QLatin1String("</span>");
      continue;
    }

    if(line[i].isLetter() || line[i] == QLatin1Char('_'))
    {
      const int start = i++;
      while(i < line.size() &&
            (line[i].isLetterOrNumber() || line[i] == QLatin1Char('_') ||
             line[i] == QLatin1Char('-')))
        ++i;
      int next = i;
      while(next < line.size() && line[next].isSpace())
        ++next;
      const QString word = line.mid(start, i - start);
      QString cssClass;
      if(next < line.size() && line[next] == QLatin1Char(':'))
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-key");
      else if(word.compare(QLatin1String("true"), Qt::CaseInsensitive) == 0 ||
              word.compare(QLatin1String("false"), Qt::CaseInsensitive) == 0 ||
              word.compare(QLatin1String("null"), Qt::CaseInsensitive) == 0)
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-literal");
      else if(word.compare(QLatin1String("running"), Qt::CaseInsensitive) == 0 ||
              word.compare(QLatin1String("completed"), Qt::CaseInsensitive) == 0 ||
              word.compare(QLatin1String("failed"), Qt::CaseInsensitive) == 0 ||
              word.compare(QLatin1String("error"), Qt::CaseInsensitive) == 0)
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-status");
      else if(next < line.size() && line[next] == QLatin1Char('('))
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-function");
      else if(sourceType(word))
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-type");
      else
        // i18n-ignore: CSS class name is an internal selector.
        cssClass = QLatin1String("detail-variable");

      html += QLatin1String("<span class=\"") + cssClass + QLatin1String("\">") +
              word.toHtmlEscaped() + QLatin1String("</span>");
      continue;
    }

    if(line[i].isDigit() ||
       (line[i] == QLatin1Char('-') && i + 1 < line.size() && line[i + 1].isDigit()))
    {
      const int start = i++;
      while(i < line.size() &&
            (line[i].isDigit() || line[i] == QLatin1Char('.') ||
             line[i] == QLatin1Char('e') || line[i] == QLatin1Char('E') ||
             line[i] == QLatin1Char('+') || line[i] == QLatin1Char('-')))
        ++i;
      html += QLatin1String("<span class=\"detail-number\">") +
              line.mid(start, i - start).toHtmlEscaped() + QLatin1String("</span>");
      continue;
    }

    html += QString(line[i]).toHtmlEscaped();
    ++i;
  }
  return html;
}

QString renderActivityDetail(QString detail, bool codeStyle)
{
  detail.replace(lit("\r\n"), lit("\n"));
  detail.replace(QLatin1Char('\r'), QLatin1Char('\n'));
  QStringList lines = detail.split(QLatin1Char('\n'), QString::KeepEmptyParts);
  while(!lines.isEmpty() && lines.first().trimmed().isEmpty())
    lines.removeFirst();
  while(!lines.isEmpty() && lines.last().trimmed().isEmpty())
    lines.removeLast();
  if(lines.isEmpty())
    return QString();

  const int maxLines = codeStyle ? 10 : 7;
  const int maxLineCharacters = codeStyle ? 280 : 360;
  const int visibleLineCount = qMin(lines.size(), maxLines);
  bool truncated = lines.size() > maxLines;

  if(codeStyle)
  {
    // Use a small table instead of CSS overflow/max-height: those properties are not reliably
    // honoured by Qt 5.9's QTextDocument. Capping rows gives tool output a predictable footprint.
    // i18n-ignore: Structural transcript HTML contains no user-visible prose.
    QString html = QLatin1String("<table class=\"activity-code\" width=\"100%\" border=\"0\" ") +
                   // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                   QLatin1String("cellspacing=\"0\" cellpadding=\"0\">");
    for(int lineIndex = 0; lineIndex < visibleLineCount; ++lineIndex)
    {
      QString line = lines[lineIndex];
      if(line.size() > maxLineCharacters)
      {
        line = line.left(maxLineCharacters) + QChar(0x2026);
        truncated = true;
      }
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += QLatin1String("<tr><td class=\"code-line-number\" width=\"30\" align=\"right\" ") +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              QLatin1String("valign=\"top\">") + QString::number(lineIndex + 1) +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              QLatin1String("</td><td class=\"code-line\" valign=\"top\">") +
              (line.isEmpty() ? QLatin1String("&nbsp;") : highlightedDetailLine(line)) +
              QLatin1String("</td></tr>");
    }
    if(truncated)
    {
      const QString omitted = QCoreApplication::translate(
          "AIConversationView", "Additional output omitted from the timeline");
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += QLatin1String("<tr><td class=\"code-line-number\"></td><td class=\"code-omitted\">") +
              omitted.toHtmlEscaped() + QLatin1String("&hellip;</td></tr>");
    }
    // i18n-ignore: Structural transcript HTML contains no user-visible prose.
    return html + QLatin1String("</table>");
  }

  QStringList visibleLines;
  visibleLines.reserve(visibleLineCount);
  for(int lineIndex = 0; lineIndex < visibleLineCount; ++lineIndex)
  {
    QString line = lines[lineIndex];
    if(line.size() > maxLineCharacters)
    {
      line = line.left(maxLineCharacters) + QChar(0x2026);
      truncated = true;
    }
    visibleLines.push_back(line.toHtmlEscaped());
  }
  QString html = visibleLines.join(QLatin1String("<br/>"));
  if(truncated)
    html += QLatin1String("<br/>&hellip;");
  return QLatin1String("<div class=\"activity-detail\">") + html + QLatin1String("</div>");
}

QString renderActivityGroup(const AIChatMessage &message, qint64 finishedMs, qint64 nowMs)
{
  if(message.activity.isEmpty())
    return QString();

  qint64 effectiveFinishedMs = finishedMs;
  if(isLiveState(message.state))
  {
    effectiveFinishedMs = nowMs;
  }
  else if(effectiveFinishedMs <= 0)
  {
    for(const AIActivityItem &activity : message.activity)
      effectiveFinishedMs = qMax(effectiveFinishedMs, activity.finishedTimestampMs);
  }

  QString duration = lit("--");
  if(message.timestampMs > 0 && effectiveFinishedMs >= message.timestampMs)
    duration = elapsedText(effectiveFinishedMs - message.timestampMs);

  const QString token = QString::fromLatin1(QUrl::toPercentEncoding(message.id));
  const bool nextExpanded = !message.activityExpanded;
  const QString href = lit("replaylab-activity://toggle/") + token + lit("?expanded=") +
                       (nextExpanded ? lit("1") : lit("0"));
  const QString elapsed =
      QCoreApplication::translate("AIConversationView", "Elapsed %1").arg(duration);

  // i18n-ignore: Structural transcript HTML contains no user-visible prose.
  QString html = lit("<div class=\"activity-group\"><table class=\"activity-header\" ") +
                 // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                 lit("width=\"100%\" border=\"0\" cellspacing=\"0\" cellpadding=\"0\">") +
                 // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                 lit("<tr><td width=\"18\" valign=\"middle\"><a class=\"activity-disclosure\" href=\"") +
                 href.toHtmlEscaped() + lit("\">") +
                 // i18n-ignore: HTML entities are visual disclosure indicators, not prose.
                 (message.activityExpanded ? lit("&#9660;") : lit("&#9654;")) +
                 // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                 lit("</a></td><td><a class=\"activity-toggle\" href=\"") +
                 href.toHtmlEscaped() + lit("\">") + elapsed.toHtmlEscaped() +
                 // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                 lit("</a></td></tr></table><hr/>");

  if(message.activityExpanded)
  {
    // i18n-ignore: Structural transcript HTML contains no user-visible prose.
    html += lit("<div class=\"activity-list\">");
    for(const AIActivityItem &activity : message.activity)
    {
      QString title = activity.title.trimmed();
      if(title.isEmpty())
        title = activityKindLabel(activity.kind);

      qint64 itemFinishedMs = activity.finishedTimestampMs;
      if(itemFinishedMs <= 0 && !activity.completed && !activity.error)
        itemFinishedMs = nowMs;
      QString itemDuration;
      if(activity.timestampMs > 0 && itemFinishedMs >= activity.timestampMs)
        itemDuration = elapsedText(itemFinishedMs - activity.timestampMs);

      const QString statusClass = activityStatusClass(activity);
      const QString marker =
          // i18n-ignore: HTML entities are visual status markers, not user-visible prose.
          activity.error ? lit("&#10005;") : (activity.completed ? lit("&#10003;") : lit("&#9679;"));
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += lit("<div class=\"activity-row\"><table width=\"100%\" border=\"0\" ") +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("cellspacing=\"0\" cellpadding=\"0\"><tr><td width=\"22\" valign=\"top\">") +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("<span class=\"activity-marker ") + statusClass + lit("\">") + marker +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("</span></td><td><div class=\"activity-title\">") + title.toHtmlEscaped() +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("</div><div class=\"activity-meta\"><span class=\"activity-kind\">") +
              activityKindLabel(activity.kind).toHtmlEscaped() + QLatin1String("</span>") +
              // i18n-ignore: Structural transcript HTML and separator entity contain no prose.
              lit("<span class=\"activity-state\"> &middot; ") +
              activityState(activity).toHtmlEscaped();
      if(!itemDuration.isEmpty())
        // i18n-ignore: HTML entity is a visual separator, not user-visible prose.
        html += lit(" &middot; ") + itemDuration.toHtmlEscaped();
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += lit("</span></div>");
      if(!activity.detail.trimmed().isEmpty())
      {
        // Activity detail is already an explicit public event summary. This view never requests or
        // renders private model reasoning; reasoning summaries stay plain text, while bounded tool
        // arguments/results use a dedicated code surface.
        html += renderActivityDetail(activity.detail,
                                     isCodeActivity(activity.kind, activity.detail));
      }
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += lit("</td></tr></table></div>");
    }
    // i18n-ignore: Structural transcript HTML contains no user-visible prose.
    html += lit("</div>");
  }
  // i18n-ignore: Structural transcript HTML contains no user-visible prose.
  html += lit("</div>");
  return html;
}
}    // namespace

// QTextDocument's Qt 5.9 rich-text subset does not clip table-cell backgrounds to CSS corner
// radii. Paint the user-message surface explicitly so the bubble remains rounded on every
// supported Qt backend while retaining native text selection and link handling.
class AIRoundedTextBrowser : public QTextBrowser
{
public:
  explicit AIRoundedTextBrowser(QWidget *parent = NULL) : QTextBrowser(parent) {}

  void setBubbleAnchors(const QVector<QPair<QString, QString>> &anchors)
  {
    m_BubbleCursors.clear();
    if(anchors.isEmpty() || !document())
    {
      viewport()->update();
      return;
    }

    QHash<QString, QTextCursor> anchorCursors;
    for(QTextBlock block = document()->begin(); block.isValid(); block = block.next())
    {
      for(QTextBlock::iterator it = block.begin(); !it.atEnd(); ++it)
      {
        const QTextFragment fragment = it.fragment();
        if(!fragment.isValid())
          continue;
        const QStringList names = fragment.charFormat().anchorNames();
        if(names.isEmpty())
          continue;
        QTextCursor cursor(document());
        cursor.setPosition(fragment.position());
        for(const QString &name : names)
          anchorCursors[name] = cursor;
      }
    }
    for(const QPair<QString, QString> &names : anchors)
    {
      if(anchorCursors.contains(names.first) && anchorCursors.contains(names.second))
        m_BubbleCursors.push_back(
            qMakePair(anchorCursors.value(names.first), anchorCursors.value(names.second)));
    }
    viewport()->update();
  }

protected:
  void paintEvent(QPaintEvent *event) override
  {
    QTextBrowser::paintEvent(event);
    if(m_BubbleCursors.isEmpty() || !document())
      return;

    QPainter painter(viewport());
    painter.setRenderHint(QPainter::Antialiasing, true);
    QColor fill = palette().color(QPalette::AlternateBase);
    fill.setAlpha(46);
    QColor outline = palette().color(QPalette::Mid);
    outline.setAlpha(190);
    painter.setBrush(fill);
    painter.setPen(QPen(outline, 1.0));
    painter.setClipRect(event->rect());
    for(const QPair<QTextCursor, QTextCursor> &cursors : m_BubbleCursors)
    {
      const QRect start = cursorRect(cursors.first);
      const QRect end = cursorRect(cursors.second);
      if(end.bottom() < start.top())
        continue;
      QRectF bubble(start.left() - 8.0, start.top() - 5.0, viewport()->width() - start.left() - 9.0,
                    end.bottom() - start.top() + 10.0);
      if(bubble.width() <= 4.0 || bubble.height() <= 4.0 || !bubble.intersects(QRectF(event->rect())))
        continue;
      const qreal radius = qMin<qreal>(18.0, bubble.height() / 2.0);
      painter.drawRoundedRect(bubble, radius, radius);
    }
  }

private:
  QVector<QPair<QTextCursor, QTextCursor>> m_BubbleCursors;
};

class AITurnRailWidget : public QWidget
{
public:
  struct Turn
  {
    QString messageId;
    QString preview;
  };

  explicit AITurnRailWidget(QWidget *parent = NULL) : QWidget(parent)
  {
    setFixedWidth(36);
    setMouseTracking(true);
    setCursor(Qt::PointingHandCursor);
  }

  void setTurns(const QVector<Turn> &turns)
  {
    m_Turns = turns;
    m_Hovered = -1;
    update();
  }

  void setActivateHandler(std::function<void(const QString &)> handler)
  {
    m_ActivateHandler = std::move(handler);
  }

protected:
  void paintEvent(QPaintEvent *) override
  {
    QPainter painter(this);
    painter.setRenderHint(QPainter::Antialiasing, true);
    const QColor normal = palette().color(QPalette::Mid);
    const QColor active = palette().color(QPalette::Text);
    for(int i = 0; i < m_Turns.size(); ++i)
    {
      const int y = turnY(i);
      const int length = i == m_Hovered ? 24 : 10 + (i % 3) * 4;
      QPen pen(i == m_Hovered ? active : normal, i == m_Hovered ? 2.0 : 1.2, Qt::SolidLine,
               Qt::RoundCap);
      painter.setPen(pen);
      painter.drawLine(6, y, 6 + length, y);
    }
  }

  void mouseMoveEvent(QMouseEvent *event) override
  {
    const int hovered = turnAt(event->pos().y());
    if(hovered == m_Hovered)
      return;
    m_Hovered = hovered;
    update();
    if(m_Hovered >= 0)
      QToolTip::showText(event->globalPos(), m_Turns[m_Hovered].preview, this);
    else
      QToolTip::hideText();
  }

  void leaveEvent(QEvent *) override
  {
    m_Hovered = -1;
    QToolTip::hideText();
    update();
  }

  void mousePressEvent(QMouseEvent *event) override
  {
    if(event->button() != Qt::LeftButton)
      return;
    const int selected = turnAt(event->pos().y());
    if(selected >= 0 && m_ActivateHandler)
      m_ActivateHandler(m_Turns[selected].messageId);
  }

private:
  qreal railStep() const
  {
    if(m_Turns.size() <= 1)
      return 0.0;
    return qMin(12.0, qMax(1, height() - 20) / qreal(m_Turns.size() - 1));
  }

  int turnY(int index) const { return qRound(10.0 + index * railStep()); }

  int turnAt(int y) const
  {
    if(m_Turns.isEmpty())
      return -1;
    if(m_Turns.size() == 1)
      return qAbs(y - 10) <= 7 ? 0 : -1;
    const qreal step = railStep();
    const int candidate = qBound(0, qRound((y - 10) / step), m_Turns.size() - 1);
    return qAbs(y - turnY(candidate)) <= qMax(3, int(step / 2.0 + 1.5)) ? candidate : -1;
  }

  QVector<Turn> m_Turns;
  std::function<void(const QString &)> m_ActivateHandler;
  int m_Hovered = -1;
};

AIConversationView::AIConversationView(QWidget *parent) : QWidget(parent)
{
  QHBoxLayout *layout = new QHBoxLayout(this);
  layout->setContentsMargins(0, 0, 0, 0);
  layout->setSpacing(0);

  m_TurnRail = new AITurnRailWidget(this);
  m_TurnRail->setToolTip(
      QCoreApplication::translate("AIConversationView", "Conversation turns"));
  m_TurnRail->setActivateHandler([this](const QString &messageId) { scrollToMessage(messageId); });
  layout->addWidget(m_TurnRail);

  m_Browser = new AIRoundedTextBrowser(this);
  m_Browser->setFrameShape(QFrame::NoFrame);
  m_Browser->setOpenLinks(false);
  m_Browser->setOpenExternalLinks(false);
  m_Browser->setReadOnly(true);
  m_Browser->setTextInteractionFlags(Qt::TextBrowserInteraction);
  connect(m_Browser, &QTextBrowser::anchorClicked, this,
          [this](const QUrl &url) { activateLink(url); });
  layout->addWidget(m_Browser, 1);

  m_ElapsedTimer = new QTimer(this);
  m_ElapsedTimer->setInterval(1000);
  m_ElapsedTimer->setTimerType(Qt::CoarseTimer);
  connect(m_ElapsedTimer, &QTimer::timeout, this, [this]() { render(); });
}

void AIConversationView::setMessages(const QVector<AIChatMessage> &messages)
{
  m_Messages = messages;
  render();
}

void AIConversationView::clear()
{
  ++m_RenderGeneration;
  m_Messages.clear();
  m_MessageAnchors.clear();
  if(m_TurnRail)
    m_TurnRail->setTurns(QVector<AITurnRailWidget::Turn>());
  if(m_Browser)
    m_Browser->clear();
  if(m_ElapsedTimer)
    m_ElapsedTimer->stop();
}

void AIConversationView::scrollToMessage(const QString &messageId)
{
  if(!m_Browser)
    return;
  const QString anchor = m_MessageAnchors.value(messageId);
  if(!anchor.isEmpty())
    m_Browser->scrollToAnchor(anchor);
}

void AIConversationView::setLinkHandler(std::function<void(const QUrl &)> handler)
{
  m_LinkHandler = std::move(handler);
}

void AIConversationView::setActivityToggleHandler(
    std::function<void(const QString &messageId, bool expanded)> handler)
{
  m_ActivityToggleHandler = std::move(handler);
}

void AIConversationView::changeEvent(QEvent *event)
{
  QWidget::changeEvent(event);
  if(event && (event->type() == QEvent::PaletteChange || event->type() == QEvent::StyleChange) &&
     !m_Messages.isEmpty())
    render();
}

void AIConversationView::activateLink(const QUrl &url)
{
  if(url.scheme().compare(lit("replaylab-message"), Qt::CaseInsensitive) == 0)
  {
    scrollToMessage(QUrl::fromPercentEncoding(url.path().mid(1).toUtf8()));
    return;
  }
  if(url.scheme().compare(lit("replaylab-activity"), Qt::CaseInsensitive) == 0 &&
     url.host().compare(lit("toggle"), Qt::CaseInsensitive) == 0)
  {
    const QString messageId = QUrl::fromPercentEncoding(url.path().mid(1).toUtf8());
    const bool expanded = QUrlQuery(url).queryItemValue(lit("expanded")) == lit("1");
    for(AIChatMessage &message : m_Messages)
    {
      if(message.id == messageId)
      {
        message.activityExpanded = expanded;
        render();
        if(m_ActivityToggleHandler)
          m_ActivityToggleHandler(messageId, expanded);
        return;
      }
    }
    return;
  }

  if(m_LinkHandler)
    m_LinkHandler(url);
}

void AIConversationView::render(bool preserveScroll)
{
  if(!m_Browser || !m_TurnRail)
    return;
  QScrollBar *scroll = m_Browser->verticalScrollBar();
  const int oldValue = scroll ? scroll->value() : 0;
  const bool followTail = !preserveScroll || !scroll || scroll->maximum() - oldValue <= 8;

  m_MessageAnchors.clear();
  QVector<AITurnRailWidget::Turn> turns;
  for(int i = 0; i < m_Messages.size(); ++i)
  {
    const AIChatMessage &message = m_Messages[i];
    const QString anchor = lit("replaylab-turn-") + QString::number(i);
    m_MessageAnchors[message.id] = anchor;
    if(message.role == lit("user"))
    {
      AITurnRailWidget::Turn turn;
      turn.messageId = message.id;
      turn.preview = message.text.simplified().left(180);
      if(turn.preview.isEmpty())
        turn.preview = QCoreApplication::translate("AIConversationView", "Empty user turn");
      turns.push_back(turn);
    }
  }
  m_TurnRail->setTurns(turns);

  const QPalette colors = palette();
  const QString base = colors.color(QPalette::Base).name();
  const QString alternate = colors.color(QPalette::AlternateBase).name();
  const QString text = colors.color(QPalette::Text).name();
  const QString muted = colors.color(QPalette::Disabled, QPalette::Text).name();
  const QString link = colors.color(QPalette::Link).name();
  const QString border = colors.color(QPalette::Mid).name();
  const QString highlight = colors.color(QPalette::Highlight).name();
  const QString highlightedText = colors.color(QPalette::HighlightedText).name();
  const bool darkTheme = colors.color(QPalette::Base).lightness() <
                         colors.color(QPalette::Text).lightness();
  // Darcula's semantic colours are deliberately kept independent from selection/link colours.
  // The light variants preserve the same token separation without reducing contrast on a bright
  // palette.
  // These are internal CSS colour constants modelled after Rider's Darcula token palette.
  const QString syntaxKeyword = darkTheme ? QLatin1String("#cc7832") : QLatin1String("#0033b3");
  const QString syntaxType = darkTheme ? QLatin1String("#b8d7a3") : QLatin1String("#871094");
  const QString syntaxFunction = darkTheme ? QLatin1String("#ffc66d") : QLatin1String("#00627a");
  const QString syntaxVariable = darkTheme ? QLatin1String("#a9b7c6") : QLatin1String("#202020");
  const QString syntaxString = darkTheme ? QLatin1String("#6a8759") : QLatin1String("#067d17");
  const QString syntaxNumber = darkTheme ? QLatin1String("#6897bb") : QLatin1String("#1750eb");
  const QString syntaxComment = darkTheme ? QLatin1String("#808080") : QLatin1String("#6a737d");
  const QString syntaxParameter = darkTheme ? QLatin1String("#9876aa") : QLatin1String("#9e5b00");

  QHash<QString, qint64> assistantFinishedByTask;
  for(int i = 0; i < m_Messages.size(); ++i)
  {
    const AIChatMessage &message = m_Messages[i];
    // i18n-ignore: Message role is a backend protocol identifier used only for routing.
    if(message.role == lit("assistant") && !message.taskId.isEmpty())
      assistantFinishedByTask[message.taskId] = message.timestampMs;
  }

  bool hasLiveElapsed = false;
  for(const AIChatMessage &message : m_Messages)
    hasLiveElapsed |= isLiveState(message.state) && message.timestampMs > 0;
  if(m_ElapsedTimer)
  {
    if(hasLiveElapsed && !m_ElapsedTimer->isActive())
      m_ElapsedTimer->start();
    else if(!hasLiveElapsed && m_ElapsedTimer->isActive())
      m_ElapsedTimer->stop();
  }
  const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();

  // i18n-ignore: Structural transcript HTML contains no user-visible prose.
  QString html = lit("<!DOCTYPE html><html><head><style>");
  html +=
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("body{background:") + base + lit(";color:") + text +
      QLatin1String(";margin:7px;font-family:'Segoe UI Variable Text','Segoe UI','Microsoft YaHei UI',") +
      QLatin1String("'Noto Sans CJK SC',sans-serif;font-size:10pt;line-height:145%;}a{color:") + link +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";}p{margin:3px 0 5px 0;}ul,ol{margin-top:4px;margin-bottom:6px;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("padding-left:22px;}li{margin:2px 0;}h3,h4,h5,h6{margin:7px 0 3px 0;}") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("pre{white-space:pre-wrap;background:") + alternate + lit(";border:1px solid ") + border +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";margin:6px 0;padding:8px;font-family:'Cascadia Code','Cascadia Mono',Consolas,") +
      QLatin1String("'SFMono-Regular',monospace;}code{font-family:'Cascadia Code','Cascadia Mono',Consolas,") +
      QLatin1String("'SFMono-Regular',monospace;background:") + alternate +
      QLatin1String(";}.source-block{font-family:'Cascadia Code','Cascadia Mono',Consolas,") +
      QLatin1String("'SFMono-Regular',monospace;font-size:9.5pt;line-height:125%;background:") + alternate +
      QLatin1String(";border:1px solid ") + border + QLatin1String(";margin:7px 0 9px 0;") +
      QLatin1String("}.source-header{font-family:'Segoe UI Variable Text','Segoe UI','Microsoft YaHei UI',") +
      QLatin1String("sans-serif;color:") + muted +
      QLatin1String(";font-size:9pt;font-weight:600;padding:5px 8px;") +
      QLatin1String("border-bottom:1px solid ") + border +
      QLatin1String(";}.source-line-number{color:") + muted +
      QLatin1String(";padding:0 8px 0 4px;border-right:1px solid ") + border +
      QLatin1String(";}.source-line{color:") + text +
      QLatin1String(";padding:0 8px;white-space:pre-wrap;") +
      QLatin1String("}.source-keyword{color:") + syntaxKeyword +
      QLatin1String(";font-weight:600;}.source-type{color:") + syntaxType +
      QLatin1String(";}.source-function{color:") + syntaxFunction +
      QLatin1String(";}.source-variable{color:") + syntaxVariable +
      QLatin1String(";}.source-key{color:") + syntaxParameter +
      QLatin1String(";font-weight:600;}.source-string{color:") + syntaxString +
      QLatin1String(";}.source-comment{color:") + syntaxComment +
      QLatin1String(";font-style:italic;}.source-number{color:") + syntaxNumber +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";}.bubble-marker{font-size:1px;}.user-bubble{padding:7px 10px;}.assistant{") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("margin:7px 4px 6px 4px;") + lit("padding:0;background:") + base + lit(";}.system{color:") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      muted + lit(";margin:3px 4px;padding-left:7px;border-left:2px solid ") + border +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";}.meta{color:") + muted +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";font-size:small;margin-bottom:3px;}.activity-group{margin:5px 4px 10px 4px;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("}.activity-header{color:") + muted +
      QLatin1String(";font-size:small;}.activity-disclosure{color:") + text +
      QLatin1String(";font-family:'Segoe UI Symbol','Noto Sans Symbols','Arial Unicode MS',sans-serif;") +
      QLatin1String("font-size:16px;line-height:100%;text-decoration:none;}.activity-toggle{color:") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      muted + lit(";text-decoration:none;}hr{border:0;border-top:1px solid ") + border +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";height:1px;margin:4px 0 7px 0;}.activity-list{margin-left:9px;padding-left:11px;") +
      QLatin1String("border-left:1px solid ") + border +
      QLatin1String(";}.activity-row{margin:4px 0 9px 0;color:") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      text + lit(";}.activity-marker{font-size:13px;}.activity-title{color:") + text +
      QLatin1String(";font-weight:600;margin:0 0 2px 0;}.activity-meta{margin:0 0 3px 0;") +
      QLatin1String("line-height:130%;}.activity-kind{color:") + muted +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";font-size:small;}.activity-state{color:") + muted +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";font-size:small;}.activity-detail{color:") + muted +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";margin:3px 0 0 0;line-height:145%;}.activity-code{font-family:'Cascadia Code',") +
      QLatin1String("'Cascadia Mono',Consolas,'SFMono-Regular',monospace;font-size:9pt;") +
      QLatin1String("line-height:125%;background:") + alternate +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";border:1px solid ") + border + lit(";margin:4px 0 2px 0;padding:2px;") +
      QLatin1String("}.code-line-number{color:") + muted +
      QLatin1String(";padding:0 7px 0 3px;border-right:1px solid ") + border +
      QLatin1String(";}.code-line{color:") + text +
      QLatin1String(";padding:0 5px;white-space:pre-wrap;") +
      QLatin1String("}.code-omitted{color:") + muted +
      QLatin1String(";font-style:italic;padding:3px 5px;") +
      QLatin1String("}.detail-key{color:") + syntaxParameter +
      QLatin1String(";font-weight:600;}.detail-string{color:") + syntaxString +
      QLatin1String(";}.detail-number{color:") + syntaxNumber +
      QLatin1String(";}.detail-literal{color:") + syntaxKeyword +
      QLatin1String(";}.detail-type{color:") + syntaxType +
      QLatin1String(";}.detail-function{color:") + syntaxFunction +
      QLatin1String(";}.detail-variable{color:") + syntaxVariable +
      QLatin1String(";}.detail-comment{color:") + syntaxComment +
      QLatin1String(";font-style:italic") +
      QLatin1String(";}.detail-status{color:") + highlight +
      QLatin1String(";font-weight:600;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("}.activity-running{color:") + highlight +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";}.activity-completed{color:") + highlight + lit(";}.activity-error{color:") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      highlightedText + lit(";background:") + highlight + lit(";}.live{margin:4px 4px 6px 4px;") +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit("padding-left:8px;border-left:2px solid ") + highlight +
      // i18n-ignore: Transcript CSS declarations contain no user-visible text.
      lit(";}blockquote{border-left:3px solid ") + border +
      // i18n-ignore: Transcript CSS declarations and closing HTML contain no user-visible text.
      lit(";padding-left:8px;margin:2px 0 2px 4px;}</style></head><body>");

  QVector<QPair<QString, QString>> bubbleAnchors;
  for(int messageIndex = 0; messageIndex < m_Messages.size(); ++messageIndex)
  {
    const AIChatMessage &message = m_Messages[messageIndex];
    const QString anchor = m_MessageAnchors.value(message.id);
    html += lit("<a name=\"") + anchor.toHtmlEscaped() + lit("\"></a>");
    if(message.role == lit("user"))
    {
      // i18n-ignore: Generated anchor prefix is an internal transcript navigation identifier.
      const QString bubbleStart = lit("replaylab-bubble-start-") + QString::number(messageIndex);
      // i18n-ignore: Generated anchor prefix is an internal transcript navigation identifier.
      const QString bubbleEnd = lit("replaylab-bubble-end-") + QString::number(messageIndex);
      bubbleAnchors.push_back(qMakePair(bubbleStart, bubbleEnd));
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += lit("<table width=\"100%\" border=\"0\" cellspacing=\"0\" cellpadding=\"0\">") +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("<tr><td width=\"29%\"></td><td width=\"71%\" class=\"user-bubble\">") +
              // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
              lit("<a class=\"bubble-marker\" name=\"") + bubbleStart + lit("\">&#8203;</a>");
      // i18n-ignore: Conversation state is an internal lifecycle identifier used only for control flow.
      if(!message.state.isEmpty() && message.state != lit("completed"))
        // i18n-ignore: Structural transcript HTML contains no user-visible prose.
        html += lit("<div class=\"meta\">") + messageStateLabel(message.state).toHtmlEscaped() +
                // i18n-ignore: Structural transcript HTML contains no user-visible prose.
                lit("</div>");
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += safeMarkdown(message.text) + lit("<a class=\"bubble-marker\" name=\"") + bubbleEnd +
              // i18n-ignore: Structural transcript HTML and zero-width marker contain no prose.
              lit("\">&#8203;</a></td></tr></table>");

      if(!message.activity.isEmpty())
      {
        const qint64 finishedMs = message.finishedTimestampMs > 0
                                      ? message.finishedTimestampMs
                                      : assistantFinishedByTask.value(message.taskId, 0);
        html += renderActivityGroup(message, finishedMs, nowMs);
      }
    }
    else if(message.role == lit("assistant"))
    {
      html += lit("<div class=\"assistant\">") + safeMarkdown(message.text) + lit("</div>");
    }
    else
    {
      html += lit("<div class=\"system\">") + safeMarkdown(message.text) + lit("</div>");
    }

    // i18n-ignore: Message roles are backend protocol identifiers used only for routing.
    if(!message.activity.isEmpty() && message.role != lit("user") && message.role != lit("assistant"))
    {
      const qint64 finishedMs = message.finishedTimestampMs > 0
                                    ? message.finishedTimestampMs
                                    : assistantFinishedByTask.value(message.taskId, 0);
      html += renderActivityGroup(message, finishedMs, nowMs);
    }

    if(!message.provisionalText.isEmpty())
    {
      QString provisionalLabel =
          QCoreApplication::translate("AIConversationView", "ReplayLab (live)");
      if(message.state == lit("superseded") || message.state == lit("interrupted") ||
         message.state == lit("cancelled"))
        provisionalLabel =
            QCoreApplication::translate("AIConversationView", "Draft before interruption");
      else if(message.state == lit("failed"))
        provisionalLabel = QCoreApplication::translate("AIConversationView", "Failure details");
      // i18n-ignore: Structural transcript HTML contains no user-visible prose.
      html += lit("<div class=\"live\"><div class=\"meta\">") + provisionalLabel.toHtmlEscaped() +
              // i18n-ignore: Structural transcript HTML contains no user-visible prose.
              lit("</div>") + safeMarkdown(message.provisionalText) + lit("</div>");
    }
  }
  html += lit("</body></html>");

  const int generation = ++m_RenderGeneration;
  m_Browser->setHtml(html);
  static_cast<AIRoundedTextBrowser *>(m_Browser)->setBubbleAnchors(bubbleAnchors);
  QTimer::singleShot(0, this, [this, generation, followTail, oldValue]() {
    if(generation != m_RenderGeneration)
      return;
    QScrollBar *bar = m_Browser ? m_Browser->verticalScrollBar() : NULL;
    if(!bar)
      return;
    bar->setValue(followTail ? bar->maximum() : qMin(oldValue, bar->maximum()));
  });
}
