from __future__ import annotations

# Brand palette (from KajovoSpend logo):
# - Blue:   #2F8FE5
# - Yellow: #F5B800
# - Green:  #7FB51F
# - Neutrals: #111827 / #6B7280 / #E5E7EB / #F6F7F9

QSS = """
/* KajovoSpend – světlý moderní brand (čisté plochy, výrazné CTA, vysoký kontrast) */

QMainWindow { background: #F6F7F9; }
QWidget {
  color: #111827;
  font-size: 12px;
}

/* Header */
QWidget#HeaderBar {
  background: #FFFFFF;
  border-bottom: 1px solid #E5E7EB;
}
QLabel#TitleLabel { color: #111827; font-size: 20px; font-weight: 800; letter-spacing: 0.2px; }
QLabel#LogoLabel {
  background: transparent;
  border-radius: 10px;
  padding: 0px;
}

/* Panels / cards */
QWidget#Panel, QWidget[panel="true"] {
  background: #FFFFFF;
  border: 1px solid #E5E7EB;
  border-radius: 12px;
}
QWidget#DashTile {
  background: #FFFFFF;
  border: 1px solid #E5E7EB;
  border-radius: 14px;
}
QLabel#CardTitle, QLabel#DashTitle { color: #111827; font-weight: 700; }
QLabel#CardValue, QLabel#DashValue { color: #2F8FE5; font-weight: 900; font-size: 20px; }
QLabel#DashHeadline { color: #111827; font-size: 14px; font-weight: 800; }
QFrame#DashSeparator {
  background: #111827;
  border: none;
  min-height: 3px;
  max-height: 3px;
  margin: 4px 0px;
}
QLabel { background: transparent; }

/* Buttons */
QPushButton {
  background: #2F8FE5;
  color: #FFFFFF;
  border: 1px solid #1C7FD9;
  border-radius: 10px;
  padding: 7px 12px;
  font-weight: 700;
}
QPushButton:hover { background: #1C7FD9; }
QPushButton:pressed { background: #166AB6; }
QPushButton:disabled {
  background: #E5E7EB;
  color: #6B7280;
  border: 1px solid #E5E7EB;
}
QPushButton#ExitButton {
  background: #EF4444;
  border: 1px solid #DC2626;
}
QPushButton#ExitButton:hover { background: #DC2626; }

/* Inputs */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QComboBox {
  background: #FFFFFF;
  color: #111827;
  border: 1px solid #D1D5DB;
  border-radius: 10px;
  padding: 7px 8px;
  selection-background-color: #2F8FE5;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QDateEdit:focus, QComboBox:focus {
  border: 1px solid #2F8FE5;
}

/* Tabs */
QTabWidget::pane {
  border: 1px solid #E5E7EB;
  top: -1px;
  background: #FFFFFF;
  border-radius: 12px;
}
QTabBar::tab {
  background: #F3F4F6;
  color: #111827;
  border: 1px solid #E5E7EB;
  border-bottom: none;
  padding: 9px 14px;
  border-top-left-radius: 10px;
  border-top-right-radius: 10px;
  margin-right: 4px;
  font-weight: 700;
}
QTabBar::tab:selected {
  background: #FFFFFF;
  color: #2F8FE5;
  border-color: #E5E7EB;
}
QTabBar::tab:hover { color: #1C7FD9; }

/* Tables */
QTableView {
  background: #FFFFFF;
  alternate-background-color: #F9FAFB;
  gridline-color: #E5E7EB;
  border: 1px solid #E5E7EB;
  border-radius: 10px;
  selection-background-color: #2F8FE5;
  selection-color: #FFFFFF;
}
QHeaderView::section {
  background: #F3F4F6;
  color: #111827;
  padding: 7px;
  border: 1px solid #E5E7EB;
  font-weight: 800;
}

/* Scrollbars */
QScrollBar:vertical, QScrollBar:horizontal {
  background: #F3F4F6;
  border: 1px solid #E5E7EB;
  border-radius: 8px;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
  background: #D1D5DB;
  border-radius: 8px;
}
QScrollBar::handle:hover { background: #9CA3AF; }

/* Checkboxes */
QCheckBox { color: #111827; }
QCheckBox::indicator {
  width: 16px; height: 16px;
  border-radius: 4px;
  border: 1px solid #D1D5DB;
  background: #FFFFFF;
}
QCheckBox::indicator:checked {
  background: #7FB51F;
  border: 1px solid #6FA61A;
}

/* Progress */
QProgressBar {
  border: 1px solid #E5E7EB;
  border-radius: 10px;
  background: #FFFFFF;
  text-align: center;
}
QProgressBar::chunk {
  border-radius: 10px;
  background: #2F8FE5;
}

/* Dialogs */
QDialog, QMessageBox {
  background: #FFFFFF;
  color: #111827;
  border: 1px solid #E5E7EB;
  border-radius: 14px;
}
QMessageBox QLabel, QDialog QLabel { color: #111827; }
QMessageBox QPushButton, QDialog QPushButton { min-width: 92px; }

/* Subtle links/accents */
QLabel[link="true"] { color: #2F8FE5; }
"""
