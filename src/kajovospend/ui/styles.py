from __future__ import annotations

# Simple corporate-style monochrome QSS.
# Rules derived from provided design manual: black/white, soft corners, minimal, red only for EXIT/critical.

QSS = """
* {
    font-family: 'Montserrat';
    font-size: 14px;
}
QMainWindow {
    background: #F4F4F4;
}
QWidget#HeaderBar {
    background: #FFFFFF;
    border-bottom: 1px solid #D0D0D0;
}
QWidget[panel="true"] {
    background: #FFFFFF;
    border: 1px solid #D0D0D0;
    border-radius: 12px;
}
QLabel#TitleLabel {
    font-size: 22px;
    font-weight: 600;
    color: #000000;
}
QLabel#LogoLabel {
    background: transparent;
}
QLabel[card="true"] {
    background: #FFFFFF;
    border: 1px solid #D0D0D0;
    border-radius: 12px;
    padding: 12px;
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
QPushButton:disabled {
    background: #555555;
    color: #DDDDDD;
}
QPushButton#ExitButton {
    background: #D40E1F;
    color: #FFFFFF;
}
QPushButton#ExitButton:hover {
    background: #B20B19;
}
QTabWidget::pane {
    background: #FFFFFF;
    border: 1px solid #D0D0D0;
    border-radius: 12px;
    margin-top: 6px;
}
QTabBar::tab {
    background: #EAEAEA;
    color: #000000;
    padding: 10px 16px;
    margin-right: 6px;
    border-top-left-radius: 12px;
    border-top-right-radius: 12px;
    border: 1px solid #D0D0D0;
}
QTabBar::tab:selected {
    background: #000000;
    color: #FFFFFF;
    border: 1px solid #000000;
}
QLineEdit, QComboBox, QDateEdit, QSpinBox {
    border: 1px solid #9E9E9E;
    border-radius: 12px;
    padding: 8px;
    background: #FFFFFF;
}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus {
    border: 2px solid #000000;
}
QTableView {
    background: #FFFFFF;
    alternate-background-color: #F5F5F5;
    selection-background-color: #000000;
    selection-color: #FFFFFF;
    border: 1px solid #C8C8C8;
    border-radius: 12px;
    gridline-color: #DADADA;
}
QHeaderView::section {
    background: #000000;
    color: #FFFFFF;
    padding: 8px;
    border: none;
    border-right: 1px solid #333333;
}
QSplitter::handle {
    background: #D0D0D0;
}
QLabel#PreviewBox {
    background: #FFFFFF;
    border: 1px dashed #9E9E9E;
    border-radius: 12px;
    padding: 8px;
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
