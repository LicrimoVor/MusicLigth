from PyQt5 import QtGui, QtWidgets


DARK = {"window": "#151a1f", "plot": "#0d1115", "base": "#0f1419", "text": "#edf2f7",
        "muted": "#9aa6b2", "grid": "#26313a", "border": "#303941", "button": "#26313a",
        "accent": "#62e5ab", "detector": "#40c0ff", "beat": "#ffcf66", "selected": "#34404a"}
LIGHT = {"window": "#f1f4f8", "plot": "#ffffff", "base": "#ffffff", "text": "#172331",
         "muted": "#536579", "grid": "#dce4ec", "border": "#c5cfd9", "button": "#e2e9f0",
         "accent": "#087f5b", "detector": "#087bb7", "beat": "#a56a00", "selected": "#d3e3f2"}


def current_theme():
    app = QtWidgets.QApplication.instance()
    return "light" if app and app.property("theme") == "light" else "dark"


def colors(theme=None):
    return LIGHT if (theme or current_theme()) == "light" else DARK


def apply_theme(theme):
    from ui.widgets import app_stylesheet
    app = QtWidgets.QApplication.instance()
    theme = "light" if theme == "light" else "dark"
    values = colors(theme)
    if app.property("musicLightBaseStyle") != "fusion":
        app.setStyle("Fusion")
        app.setProperty("musicLightBaseStyle", "fusion")
    if app.property("musicLightAppliedTheme") == theme:
        return
    app.setProperty("theme", theme)
    palette = QtGui.QPalette()
    for role, key in ((QtGui.QPalette.Window, "window"), (QtGui.QPalette.Base, "base"),
                      (QtGui.QPalette.AlternateBase, "window"), (QtGui.QPalette.Button, "button"),
                      (QtGui.QPalette.WindowText, "text"), (QtGui.QPalette.Text, "text"),
                      (QtGui.QPalette.ButtonText, "text"), (QtGui.QPalette.ToolTipBase, "base"),
                      (QtGui.QPalette.Light, "border"), (QtGui.QPalette.Midlight, "button"),
                      (QtGui.QPalette.Mid, "grid"), (QtGui.QPalette.Dark, "border"), (QtGui.QPalette.Shadow, "muted"),
                      (QtGui.QPalette.ToolTipText, "text"), (QtGui.QPalette.Highlight, "selected"),
                      (QtGui.QPalette.HighlightedText, "text"), (QtGui.QPalette.Link, "detector")):
        palette.setColor(role, QtGui.QColor(values[key]))
    for role in (QtGui.QPalette.Text, QtGui.QPalette.WindowText, QtGui.QPalette.ButtonText):
        palette.setColor(QtGui.QPalette.Disabled, role, QtGui.QColor(values["muted"]))
    # Palette and stylesheet each invalidate widget styles. Paint the completed
    # theme once instead of repainting native windows during both passes.
    windows = [(widget, widget.updatesEnabled()) for widget in app.topLevelWidgets()]
    try:
        for widget, enabled in windows:
            if enabled:
                widget.setUpdatesEnabled(False)
        app.setPalette(palette)
        app.setStyleSheet(app_stylesheet(theme))
        app.setProperty("musicLightAppliedTheme", theme)
    finally:
        for widget, enabled in windows:
            if enabled:
                widget.setUpdatesEnabled(True)
