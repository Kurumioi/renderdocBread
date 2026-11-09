/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2017-2026 Baldur Karlsson
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

#include "UpdateDialog.h"
#include <QApplication>
#include <QDesktopServices>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QRegularExpression>
#include <QTextBrowser>
#include <QTimer>
#include <QVersionNumber>
#include "Code/BackgroundProcess.h"
#include "Code/QRDUtils.h"
#include "ui_UpdateDialog.h"

namespace
{
bool IsReleaseVersion(const QString &version)
{
  const QVersionNumber parsed = QVersionNumber::fromString(version);
  return parsed.segmentCount() == 3 && parsed.toString() == version;
}

class ReleaseNotesBrowser : public QTextBrowser
{
public:
  using QTextBrowser::QTextBrowser;

protected:
  QVariant loadResource(int, const QUrl &) override
  {
    // Release notes must not load remote images or read local files.
    return QVariant();
  }
};

QString SystemProgram(const QString &path)
{
  return QDir(QString::fromLocal8Bit(qgetenv("SystemRoot")))
      .filePath(lit("System32/") + path);    // i18n-ignore: Windows system executable directory.
}
}

QUrl BreadRelease::ReleasesPage()
{
  return QUrl(
      lit("https://github.com/Straw1997/renderdocBread/releases"));    // i18n-ignore: Canonical
                                                                       // GitHub release page URL.
}

QString BreadRelease::InstalledVersion()
{
  const QDir app(QCoreApplication::applicationDirPath());
  QFile manifest(
      app.filePath(lit("RenderDocBread.manifest.json")));    // i18n-ignore: Release manifest key,
                                                             // marker or schema identifier.
  if(!app.exists(lit("RenderDocBread.portable")) ||          // i18n-ignore: GitHub API value.
     !manifest.open(
         QIODevice::ReadOnly))    // i18n-ignore: Release manifest key, marker or schema identifier.
    return QString();

  const QJsonObject package = QJsonDocument::fromJson(manifest.readAll()).object();
  const QString version =
      package
          .value(lit("breadVersion"))    // i18n-ignore: GitHub API value.
          .toString();    // i18n-ignore: Release manifest key, marker or schema identifier.
  if(package.value(lit("schema")).toString() ==
         lit("renderdocbread-release/v1") &&    // i18n-ignore: Release manifest key, marker or schema identifier.
     package.value(lit("product")).toString() ==
         lit("RenderDocBread") &&    // i18n-ignore: Release manifest key, marker or schema identifier.
     package.value(lit("platform")).toString() ==
         lit("windows-x64") &&    // i18n-ignore: Release manifest key, marker or schema identifier.
     package.value(lit("profile")).toString() == lit("standard") &&
     IsReleaseVersion(version))    // i18n-ignore: Release manifest key, marker or schema identifier.
    return version;

  return QString();
}

BreadRelease BreadRelease::FromGitHub(const QByteArray &json)
{
  BreadRelease release;
  const QJsonObject info = QJsonDocument::fromJson(json).object();
  const QString tag = info.value(lit("tag_name"))    // i18n-ignore: GitHub API value.
                          .toString();               // i18n-ignore: GitHub API value.
  if(info.value(lit("draft")).toBool(true) ||
     info.value(lit("prerelease")).toBool(true) ||    // i18n-ignore: GitHub release API field, path
                                                      // or validation pattern.
     !tag.startsWith(QLatin1Char('v')) || !IsReleaseVersion(tag.mid(1)))
    return release;

  release.version = tag.mid(1);
  const QString page = ReleasesPage().toString();
  release.page = QUrl(page + lit("/tag/") +                  // i18n-ignore: GitHub API value.
                      tag);                                  // i18n-ignore: GitHub API value.
  release.notesHtml = info.value(lit("body_html"))           // i18n-ignore: GitHub API value.
                          .toString();                       // i18n-ignore: GitHub API value.
  release.notesText = info.value(lit("body")).toString();    // i18n-ignore: GitHub release API
                                                             // field, path or validation pattern.

  const QRegularExpression namePattern(
      lit("^RenderDocBread-[0-9][0-9.-]*-win-x64\\.zip$"));    // i18n-ignore: GitHub release API
                                                               // field, path or validation pattern.
  const QRegularExpression digestPattern(
      lit("^sha256:([0-9a-f]{64})$"));    // i18n-ignore: GitHub API value.
  for(const QJsonValue &value :
      info.value(lit("assets")).toArray())    // i18n-ignore: GitHub release API field, path or
                                              // validation pattern.
  {
    const QJsonObject asset = value.toObject();
    const QString name = asset
                             .value(lit("name"))    // i18n-ignore: update protocol.
                             .toString();           // i18n-ignore: GitHub API value.
    const QString url = page + lit("/download/") + tag +
                        QLatin1Char('/') +    // i18n-ignore: update protocol.
                        name;                 // i18n-ignore: GitHub API value.
    const auto digest = digestPattern.match(
        asset.value(lit("digest")).toString());    // i18n-ignore: GitHub release API field, path or
                                                   // validation pattern.
    const double size = asset.value(lit("size")).toDouble();    // i18n-ignore: GitHub release API
                                                                // field, path or validation pattern.
    if(namePattern.match(name).hasMatch() &&
       asset.value(lit("state")).toString() ==    // i18n-ignore: update protocol.
           lit("uploaded") &&                     // i18n-ignore: GitHub API value.
       asset.value(lit("browser_download_url")).toString() == url &&
       digest.hasMatch() &&    // i18n-ignore: GitHub API value.
       size > 0 && size < 1.0e12)
    {
      // An ambiguous installer should be selected on the release page.
      if(release.size != 0)
      {
        release.download = QUrl();
        break;
      }
      release.download = QUrl(url);
      release.sha256 = digest.captured(1);
      release.size = qint64(size);
    }
  }
  return release;
}

void BreadRelease::Request(QProcess *process, const QUrl &url, const QStringList &options)
{
  ConfigureBackgroundProcess(process);
  QStringList args = {lit("-q"),        // i18n-ignore: update protocol.
                      lit("--fail"),    // i18n-ignore: curl argument.
                      lit("--silent"),
                      lit("--show-error"),    // i18n-ignore: curl argument.
                      lit("--location"),
                      lit("--proto"),    // i18n-ignore: curl argument.
                      lit("=https"),
                      lit("--proto-redir"),    // i18n-ignore: curl argument.
                      lit("=https"),
                      lit("--connect-timeout"),    // i18n-ignore: curl argument.
                      lit("20")};                  // i18n-ignore: curl argument.
  args.append(options);
  args.append(url.toString());
  process->start(SystemProgram(lit("curl.exe")),    // i18n-ignore: update protocol.
                 args);                             // i18n-ignore: curl argument.
}

UpdateDialog::UpdateDialog(const QString &installedVersion, const BreadRelease &release,
                           QWidget *parent)
    : QDialog(parent), ui(new Ui::UpdateDialog), m_Release(release)
{
  ui->setupUi(this);
  setWindowFlags(windowFlags() & ~Qt::WindowContextHelpButtonHint);
  setWindowTitle(tr("Update Available - v%1").arg(release.version));
  ui->updateVer->setText(windowTitle());
  ui->currentVersion->setText(QFormatStr("v%1").arg(installedVersion));
  ui->newVersion->setText(QFormatStr("v%1").arg(release.version));

  ReleaseNotesBrowser *notes = new ReleaseNotesBrowser(this);
  ui->verticalLayout_3->insertWidget(0, notes, 1);
  notes->setMinimumHeight(250);
  notes->setAccessibleName(tr("Release notes"));
  notes->setOpenLinks(false);
  notes->document()->setBaseUrl(release.page);
  QObject::connect(notes, &QTextBrowser::anchorClicked, this, [release](const QUrl &url) {
    const QUrl resolved = release.page.resolved(url);
    if(resolved.scheme() ==
       lit("https"))    // i18n-ignore: Update staging archive filename or URL scheme.
      QDesktopServices::openUrl(resolved);
  });
  if(!release.notesHtml.trimmed().isEmpty())
    notes->setHtml(release.notesHtml);
  else if(!release.notesText.trimmed().isEmpty())
    notes->setPlainText(release.notesText);
  else
    notes->setPlainText(tr("This release has no notes."));

  ui->progressBar->hide();
  ui->progressText->hide();
  ui->update->setEnabled(!release.download.isEmpty());
  if(release.download.isEmpty())
  {
    ui->downloadSize->setText(tr("See release page"));
    ui->progressText->setText(tr("This release does not have a verifiable Windows x64 package."));
    ui->progressText->show();
  }
  else
  {
    ui->downloadSize->setText(tr("%1 MB").arg(double(release.size) / 1000000.0, 0, 'f', 2));
  }

  QObject::connect(ui->close, &QPushButton::clicked, this, &UpdateDialog::reject);
  m_Download = new QProcess(this);
  m_ProgressTimer = new QTimer(this);
  m_ProgressTimer->setInterval(250);
  QObject::connect(m_ProgressTimer, &QTimer::timeout, this, [this]() {
    const qint64 size =
        QFileInfo(m_Staging->filePath(lit("update.zip")))    // i18n-ignore: update protocol.
            .size();    // i18n-ignore: Update staging archive filename or URL scheme.
    if(size > 0)
      UpdateTransferProgress(size, m_Release.size, &m_DownloadTimer, ui->progressBar,
                             ui->progressText, tr("Downloading update..."));
  });
  QObject::connect(m_Download, &QProcess::errorOccurred, this, [this](QProcess::ProcessError err) {
    if(err == QProcess::FailedToStart)
      DownloadFailed(m_Download->errorString());
  });
  QObject::connect(
      m_Download, OverloadedSlot<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
      [this](int code, QProcess::ExitStatus status) {
        m_ProgressTimer->stop();
        if(m_Cancelled)
        {
          QDialog::reject();
          return;
        }

        if(status != QProcess::NormalExit || code != 0)
        {
          DownloadFailed(QString::fromLocal8Bit(m_Download->readAllStandardError()).trimmed());
          return;
        }
        if(QFileInfo(m_Staging->filePath(lit("update.zip"))).size() !=    // i18n-ignore: update protocol.
           m_Release.size)    // i18n-ignore: Update staging archive filename or URL scheme.
        {
          DownloadFailed(tr("The downloaded package has an unexpected size."));
          return;
        }
        accept();
      });
  ui->close->setFocus();
  resize(680, 590);
}

UpdateDialog::~UpdateDialog()
{
  delete ui;
}

void UpdateDialog::reject()
{
  if(m_Download->state() != QProcess::NotRunning)
  {
    m_Cancelled = true;
    m_Download->kill();
    return;
  }

  QDialog::reject();
}

void UpdateDialog::on_releaseNotes_clicked()
{
  QDesktopServices::openUrl(m_Release.page);
}

void UpdateDialog::DownloadFailed(const QString &error)
{
  m_ProgressTimer->stop();
  m_Staging.reset();
  ui->progressBar->hide();
  ui->progressText->setText(tr("Download failed.\n%1").arg(error));
  ui->update->setEnabled(true);
  ui->update->setText(tr("Retry Update"));
  ui->close->setText(tr("Close"));
}

void UpdateDialog::on_update_clicked()
{
  if(RDDialog::question(
         this, tr("Update RenderDocBread"),
         tr("Download and install this update? RenderDocBread will close and "
            "restart after the download. Save any work in progress before continuing."),
         QMessageBox::Yes | QMessageBox::No, QMessageBox::No) != QMessageBox::Yes)
    return;

  const QDir app(QCoreApplication::applicationDirPath());
  m_Staging.reset(new QTemporaryDir(app.filePath(
      lit(".bread-update-XXXXXX"))));    // i18n-ignore: Temporary directory name template.
  ui->progressText->show();
  if(!m_Staging->isValid())
  {
    DownloadFailed(tr("Could not create a staging folder in the installation directory."));
    return;
  }

  ui->progressBar->show();
  ui->progressBar->setRange(0, 10000);
  ui->progressBar->setValue(0);
  ui->progressText->setText(tr("Downloading update..."));
  ui->close->setText(tr("Cancel"));
  ui->update->setEnabled(false);
  m_DownloadTimer.start();
  m_ProgressTimer->start();
  BreadRelease::Request(
      m_Download, m_Release.download,
      {lit("--speed-time"), lit("45"), lit("--speed-limit"),    // i18n-ignore: update protocol.
       lit("1024"),    // i18n-ignore: Windows curl download option.
       lit("--max-filesize"), QString::number(m_Release.size),
       lit("--output"),    // i18n-ignore: Windows curl download option.
       QDir::toNativeSeparators(m_Staging->filePath(
           lit("update.zip")))});    // i18n-ignore: Update staging archive filename or URL scheme.
}

bool UpdateDialog::StartInstaller(const QTemporaryDir &staging, const QString &installedVersion,
                                  const BreadRelease &release)
{
  QFile script(lit(":/ApplyBreadUpdate.ps1"));    // i18n-ignore: updater argument.
  const QString path =
      staging.filePath(lit("ApplyBreadUpdate.ps1"));    // i18n-ignore: updater argument.
  if(!script.copy(path))
    return false;

  const QString shell = SystemProgram(
      lit("WindowsPowerShell/v1.0/powershell.exe"));    // i18n-ignore: Installer resource path or
                                                        // PowerShell argument.
  const QStringList arguments = {lit("-NoProfile"),     // i18n-ignore: updater argument.
                                 lit("-NonInteractive"),     // i18n-ignore: updater argument.
                                 lit("-WindowStyle"),        // i18n-ignore: updater argument.
                                 lit("Hidden"),              // i18n-ignore: updater argument.
                                 lit("-ExecutionPolicy"),    // i18n-ignore: updater argument.
                                 lit("Bypass"),              // i18n-ignore: updater argument.
                                 lit("-File"),               // i18n-ignore: updater argument.
                                 QDir::toNativeSeparators(path),
                                 lit("-ParentProcessId"),    // i18n-ignore: updater argument.
                                 QString::number(QCoreApplication::applicationPid()),
                                 lit("-InstalledVersion"),    // i18n-ignore: updater argument.
                                 installedVersion,
                                 lit("-TargetVersion"),    // i18n-ignore: updater argument.
                                 release.version,
                                 lit("-Sha256"),    // i18n-ignore: updater argument.
                                 release.sha256};
  return QProcess::startDetached(shell, arguments);
}
