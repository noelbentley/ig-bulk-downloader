from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class AuthenticationDialog(QDialog):
    chosen = Signal(str, str)

    def __init__(self, request, accounts, parent=None):
        super().__init__(parent)
        self.decided = False
        self.setWindowTitle("Choose how to continue")
        self.setMinimumWidth(600)
        self.setMaximumWidth(760)
        layout = QVBoxLayout(self)
        title = QLabel(f"@{request['username']} · {request['content']}")
        layout.addWidget(title)
        reason = QLabel(request["reason"])
        reason.setWordWrap(True)
        layout.addWidget(reason)
        scope = QLabel("Authentication is used only for this content. Later public profiles return to Public Mode."
                       if request["required"] else
                       "Login is optional. You can skip this content and continue the queue in Public Mode.")
        scope.setWordWrap(True)
        layout.addWidget(scope)
        self.sessions = QComboBox()
        for account in accounts:
            self.sessions.addItem("@" + account, account)
        self.sessions.setEnabled(bool(accounts))
        layout.addWidget(self.sessions)
        buttons = QHBoxLayout()
        self.login = QPushButton("Login with Instagram")
        self.saved = QPushButton("Use Saved Session")
        self.skip = QPushButton("Skip This Content")
        self.saved.setEnabled(bool(accounts))
        self.login.clicked.connect(lambda: self.choose("login"))
        self.saved.clicked.connect(lambda: self.choose("saved", self.sessions.currentData()))
        self.skip.clicked.connect(lambda: self.choose("skip"))
        for button in (self.login, self.saved, self.skip):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.finished.connect(self._finished)
        self.finished.connect(self.deleteLater)

    def choose(self, action, account=""):
        self.decided = True
        self.accept()
        self.chosen.emit(action, account or "")

    def _finished(self, *_):
        if not self.decided:
            self.chosen.emit("skip", "")

    def dismiss(self):
        self.decided = True
        self.close()
