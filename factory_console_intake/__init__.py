"""Safe, non-secret staging boundary for Factory Console submissions.

This package accepts only manifest metadata and in-memory text uploads.  It
does not fetch URLs, read caller-selected paths, write files, or invoke any
factory lifecycle operation.
"""

from .intake import (
    FactoryConsoleIntake,
    IntakeReviewArtifact,
    IntakeValidationError,
    KnowledgeUpload,
    StagedIntake,
    ValidationIssue,
)

__all__ = [
    "FactoryConsoleIntake",
    "IntakeReviewArtifact",
    "IntakeValidationError",
    "KnowledgeUpload",
    "StagedIntake",
    "ValidationIssue",
]
