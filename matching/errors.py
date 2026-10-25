"""Failures of the price-list pipeline that a caller can handle."""


class PipelineError(RuntimeError):
    """The pipeline stopped before a result. The message is safe to log."""


class CatalogEmbedRequired(PipelineError):
    """The price-list vectors are not cached. The full embed was not started."""
