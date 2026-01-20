from __future__ import annotations

# Simple corporate-style monochrome QSS.
# Rules derived from provided design manual: black/white, soft corners, minimal, red only for EXIT/critical.

QSS = """
* {
    font-family: 'Montserrat';
    font-size: 14px;
}
QMainWindow {
    background: #FFFFFF;
}
QLabel#TitleLabel {
    font-size: 22px;
    font-weight: 600;
    color: #000000;
}
QToolButton, QPushButton {
    background: #000000;
    color: #FFFFFF;
    padding: 8px 14px;
    border-radius: 12px;
    border: none;
}
QToolButton:hover, QPushButton:hover {
    background: #222222;
}
QPushButton#ExitButton {
    background: #D40E1F;
    color: #FFFFFF;
}
QPushButton#ExitButton:hover {
    background: #B20B19;
}
QTabWidget::pane {
    border: 1px solid #E5E5E5;
    border-radius: 12px;
    margin-top: 6px;
}
QTabBar::tab {
    background: #F2F2F2;
    color: #000000;
    padding: 10px 16px;
    margin-right: 6px;
    border-top-left-radius: 12px;
    border-top-right-radius: 12px;
}
QTabBar::tab:selected {
    background: #000000;
    color: #FFFFFF;
}
QLineEdit, QComboBox, QDateEdit, QSpinBox {
    border: 1px solid #D0D0D0;
    border-radius: 12px;
    padding: 8px;
    background: #FFFFFF;
}
QTableView {
    border: 1px solid #E5E5E5;
    border-radius: 12px;
    gridline-color: #EDEDED;
}
QHeaderView::section {
    background: #000000;
    color: #FFFFFF;
    padding: 8px;
    border: none;
}
QProgressBar {
    border: 1px solid #D0D0D0;
    border-radius: 12px;
    text-align: center;
}
QProgressBar::chunk {
    background: #000000;
    border-radius: 12px;
}
"""
