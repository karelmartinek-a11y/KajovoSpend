from __future__ import annotations

QSS = """
/* KajovoSpend – dark, čisté, s akcentem (lepší kontrast, méně „šedé plochy“) */
QMainWindow { background: #0F172A; }
QWidget { color: #E5E7EB; font-size: 12px; }

QWidget#HeaderBar {
  background: #111827;
  border-bottom: 1px solid #253041;
}
QLabel#TitleLabel { color: #E5E7EB; font-size: 20px; font-weight: 700; }
QLabel#LogoLabel {
  background: #FFFFFF;
  border-radius: 10px;
  padding: 4px;
}

QWidget#Panel {
  background: #111827;
  border: 1px solid #253041;
  border-radius: 10px;
}
QLabel#CardTitle { color: #E5E7EB; font-weight: 700; }
QLabel#CardValue { color: #93C5FD; font-weight: 800; font-size: 18px; }

QPushButton {
  background: #2563EB;
  color: #F9FAFB;
  border: 1px solid #1E40AF;
  border-radius: 8px;
  padding: 6px 10px;
}
QPushButton:hover { background: #1D4ED8; }
QPushButton:pressed { background: #1E40AF; }
QPushButton:disabled {
  background: #334155;
  color: #CBD5E1;
  border: 1px solid #334155;
}
QPushButton#ExitButton {
  background: #DC2626;
  border: 1px solid #991B1B;
}
QPushButton#ExitButton:hover { background: #B91C1C; }

QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QComboBox {
  background: #0B1220;
  color: #E5E7EB;
  border: 1px solid #334155;
  border-radius: 8px;
  padding: 6px;
  selection-background-color: #2563EB;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QDateEdit:focus, QComboBox:focus {
  border: 1px solid #60A5FA;
}

QTabWidget::pane {
  border: 1px solid #253041;
  top: -1px;
  background: #0B1220;
  border-radius: 10px;
}
QTabBar::tab {
  background: #1F2937;
  color: #E5E7EB;
  border: 1px solid #253041;
  border-bottom: none;
  padding: 8px 12px;
  border-top-left-radius: 8px;
  border-top-right-radius: 8px;
  margin-right: 2px;
}
QTabBar::tab:selected {
  background: #0B1220;
  color: #93C5FD;
  border-color: #253041;
}
QTabBar::tab:hover { color: #BFDBFE; }

QTableView {
  background: #0B1220;
  alternate-background-color: #0F1B2D;
  gridline-color: #253041;
  border: 1px solid #253041;
  border-radius: 8px;
  selection-background-color: #2563EB;
  selection-color: #F9FAFB;
}
QHeaderView::section {
  background: #1F2937;
  color: #E5E7EB;
  padding: 6px;
  border: 1px solid #253041;
}

QScrollBar:vertical, QScrollBar:horizontal {
  background: #0B1220;
  border: 1px solid #253041;
  border-radius: 8px;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
  background: #334155;
  border-radius: 8px;
}
QScrollBar::handle:hover { background: #475569; }

QCheckBox { color: #E5E7EB; }
QCheckBox::indicator {
  width: 16px; height: 16px;
  border-radius: 4px;
  border: 1px solid #334155;
  background: #0B1220;
}
QCheckBox::indicator:checked {
  background: #2563EB;
  border: 1px solid #1E40AF;
}

QProgressBar {
  border: 1px solid #253041;
  border-radius: 8px;
  background: #0B1220;
  text-align: center;
}
QProgressBar::chunk {
  border-radius: 8px;
  background: #2563EB;
}
"""
