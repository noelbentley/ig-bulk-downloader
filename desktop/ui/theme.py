import re


def stylesheet(theme="Dark", accent="#7c6ff0"):
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        accent = "#7c6ff0"
    dark = theme == "Dark"
    bg, panel, text, muted, border = (("#15171c", "#1e2129", "#eef0f6", "#a2a9ba", "#343947") if dark else
                                      ("#f3f4f8", "#ffffff", "#202431", "#616b7e", "#d9deea"))
    return f"""
        QWidget {{ color: {text}; font-family: Inter, 'Segoe UI'; font-size: 13px; }}
        QMainWindow, QDialog {{ background: {bg}; }}
        QScrollArea, QWidget#settingsContent, QWidget#settingsViewport {{ background: {bg}; border: 0; }}
        QSpinBox:disabled {{ color: {muted}; }}
        QLabel#title {{ font-size: 28px; font-weight: 700; }}
        QLabel#subtitle, QLabel#muted {{ color: {muted}; }}
        QLabel#metric {{ font-size: 18px; font-weight: 600; padding: 14px;
                          background: {panel}; border: 1px solid {border}; border-radius: 10px; }}
        QGroupBox {{ background: {panel}; border: 1px solid {border}; border-radius: 10px;
                     margin-top: 15px; padding: 18px 12px 12px; font-weight: 600; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; }}
        QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTableWidget {{ background: {panel};
            border: 1px solid {border}; border-radius: 6px; padding: 8px; selection-background-color: {accent}; }}
        QPushButton {{ background: {panel}; border: 1px solid {border}; border-radius: 6px; padding: 9px 15px; }}
        QPushButton:hover {{ border-color: {accent}; }}
        QPushButton#primary {{ background: {accent}; color: white; border-color: {accent}; font-weight: 600; }}
        QPushButton:disabled {{ color: {muted}; background: {bg}; }}
        QTabWidget::pane {{ border: 0; }}
        QTabBar::tab {{ padding: 12px 22px; color: {muted}; border-bottom: 2px solid transparent; }}
        QTabBar::tab:selected {{ color: {text}; border-bottom: 2px solid {accent}; }}
        QHeaderView::section {{ background: {panel}; color: {muted}; border: 0;
                                border-bottom: 1px solid {border}; padding: 10px; }}
        QTableWidget {{ gridline-color: {border}; }}
        QProgressBar {{ border: 1px solid {border}; background: {panel}; border-radius: 4px; height: 12px; text-align: center; }}
        QProgressBar::chunk {{ background: {accent}; border-radius: 3px; }}
        QCheckBox {{ padding: 5px; }}
        QStatusBar {{ color: {muted}; }}
    """
