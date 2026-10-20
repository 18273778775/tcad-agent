# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0

class CadError(Exception):
    """An expected error that can be returned through the agent protocol."""

    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self):
        result = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result
