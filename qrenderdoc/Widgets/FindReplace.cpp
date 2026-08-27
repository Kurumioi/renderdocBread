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

#include "FindReplace.h"
#include <QKeyEvent>
#include <QLineEdit>
#include "Widgets/Extended/RDLineEdit.h"
#include "ui_FindReplace.h"

FindReplace::FindReplace(QWidget *parent) : QFrame(parent), ui(new Ui::FindReplace)
{
  ui->setupUi(this);

  ui->searchContext->setCurrentIndex(FindReplace::File);

  // default to just find
  setReplaceMode(false);
  setDirection(FindReplace::Down);

  RDLineEdit *edit = new RDLineEdit(this);
  ui->findText->setLineEdit(edit);
  RDLineEdit *replaceEdit = new RDLineEdit(this);
  ui->replaceText->setLineEdit(replaceEdit);

  ui->findText->setAutoCompletion(false);
  ui->replaceText->setAutoCompletion(false);

  QObject::connect(edit, &RDLineEdit::keyPress, [this](QKeyEvent *event) {
    if(event->key() == Qt::Key_Escape)
    {
      emit closeRequested();
    }
    else if(event->key() == Qt::Key_F3 || event->key() == Qt::Key_Return ||
            event->key() == Qt::Key_Enter)
    {
      if(event->modifiers() & Qt::ShiftModifier)
        m_direction = SearchDirection::Up;
      else
        m_direction = SearchDirection::Down;

      addHistory(ui->findText);
      emit performFind();
    }
  });
  QObject::connect(replaceEdit, &RDLineEdit::keyPress, [this](QKeyEvent *event) {
    if(event->key() == Qt::Key_Escape)
    {
      emit closeRequested();
    }
    else if(event->key() == Qt::Key_F3)
    {
      m_direction = event->modifiers() & Qt::ShiftModifier ? SearchDirection::Up
                                                           : SearchDirection::Down;
      emit performFind();
    }
  });
  QObject::connect(ui->replaceText->lineEdit(), &QLineEdit::returnPressed, this,
                   &FindReplace::on_replace_clicked);

  QObject::connect(edit, &QLineEdit::textChanged, this, &FindReplace::searchChanged);
  QObject::connect(ui->matchCase, &QToolButton::toggled, this, &FindReplace::searchChanged);
  QObject::connect(ui->matchWord, &QToolButton::toggled, this, &FindReplace::searchChanged);
  QObject::connect(ui->regexp, &QToolButton::toggled, this, &FindReplace::searchChanged);
  QObject::connect(static_cast<QComboBox *>(ui->searchContext),
                   static_cast<void (QComboBox::*)(int)>(&QComboBox::currentIndexChanged), this,
                   &FindReplace::searchChanged);

  setAttribute(Qt::WA_StyledBackground, true);
  setObjectName(QStringLiteral("editorFindBar"));
  setResultCount(0, 0);
}

FindReplace::~FindReplace()
{
  delete ui;
}

bool FindReplace::replaceMode()
{
  return ui->replaceMode->isChecked();
}

FindReplace::SearchContext FindReplace::context()
{
  return (FindReplace::SearchContext)ui->searchContext->currentIndex();
}

FindReplace::SearchDirection FindReplace::direction()
{
  return m_direction;
}

bool FindReplace::matchCase()
{
  return ui->matchCase->isChecked();
}

bool FindReplace::matchWord()
{
  return ui->matchWord->isChecked();
}

bool FindReplace::regexp()
{
  return ui->regexp->isChecked();
}

void FindReplace::setFindText(QString text)
{
  ui->findText->setCurrentText(text);
}

QString FindReplace::findText()
{
  return ui->findText->currentText();
}

void FindReplace::setReplaceText(QString text)
{
  ui->replaceText->setCurrentText(text);
}

QString FindReplace::replaceText()
{
  return ui->replaceText->currentText();
}

void FindReplace::setResultCount(int current, int total)
{
  if(total <= 0)
    ui->matchCount->setText(tr("0 results"));
  else
    ui->matchCount->setText(tr("%1/%2").arg(qMax(1, current)).arg(total));
}

void FindReplace::setSearchError(const QString &message)
{
  ui->findText->lineEdit()->setToolTip(message);
  ui->findText->lineEdit()->setStyleSheet(
      message.isEmpty() ? QString()
                        : QStringLiteral("QLineEdit { border: 1px solid #ff5647; }"));
  if(!message.isEmpty())
    ui->matchCount->setText(message);
}

void FindReplace::allowUserModeChange(bool allow)
{
  ui->modeChangeFrame->setVisible(allow);
}

void FindReplace::setReplaceMode(bool replacing)
{
  ui->replaceLabel->setVisible(replacing);
  ui->replaceText->setVisible(replacing);
  ui->replace->setVisible(replacing);
  ui->replaceAll->setVisible(replacing);

  ui->findMode->setChecked(!replacing);
  ui->replaceMode->setChecked(replacing);

  setWindowTitle(replacing ? tr("Find && Replace") : tr("Find"));
  adjustSize();
  emit layoutChanged();
}

void FindReplace::setDirection(SearchDirection dir)
{
  m_direction = dir;
}

void FindReplace::takeFocus()
{
  ui->findText->setFocus();
  ui->findText->lineEdit()->selectAll();
}

void FindReplace::keyPressEvent(QKeyEvent *event)
{
  if(event->key() == Qt::Key_F3)
  {
    if(event->modifiers() & Qt::ShiftModifier)
      m_direction = SearchDirection::Up;
    else
      m_direction = SearchDirection::Down;

    emit performFind();
  }
  else
  {
    emit keyPress(event);
  }
}

void FindReplace::addHistory(QComboBox *combo)
{
  QString text = combo->currentText();

  for(int i = 0; i < combo->count(); i++)
  {
    if(combo->itemText(i) == text)
    {
      // remove the item so we can bump it up to the top of the list
      combo->removeItem(i);
      break;
    }
  }

  combo->insertItem(0, text);
  combo->setCurrentText(text);
}

void FindReplace::on_findPrev_clicked()
{
  setDirection(FindReplace::Up);
  addHistory(ui->findText);
  emit performFind();
}

void FindReplace::on_find_clicked()
{
  setDirection(FindReplace::Down);
  addHistory(ui->findText);
  emit performFind();
}

void FindReplace::on_findAll_clicked()
{
  addHistory(ui->findText);
  emit performFindAll();
}

void FindReplace::on_replace_clicked()
{
  addHistory(ui->findText);
  addHistory(ui->replaceText);
  emit performReplace();
}

void FindReplace::on_replaceAll_clicked()
{
  addHistory(ui->findText);
  addHistory(ui->replaceText);
  emit performReplaceAll();
}

void FindReplace::on_findMode_clicked()
{
  setReplaceMode(false);
}

void FindReplace::on_replaceMode_clicked()
{
  setReplaceMode(true);
}

void FindReplace::on_close_clicked()
{
  emit closeRequested();
}
