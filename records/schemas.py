from ninja import Schema


class Envelope(Schema):
    contract_version: str
    generated_at: str


class ErrorBody(Schema):
    code: str
    message: str
    details: list | None = None


class ErrorOut(Envelope):
    error: ErrorBody


class HealthOut(Envelope):
    status: str


class RootOut(Envelope):
    name: str
    endpoints: list[str]


class MetricOut(Schema):
    code: str
    unit: str
    kind: str


class MetricsOut(Envelope):
    count: int
    items: list[MetricOut]


class MeasurementOut(Schema):
    id: int
    metric: str
    value: float | None
    unit: str
    at: str
    note: str
    parent_id: int | None
    components: list[MeasurementOut] | None = None


class MeasurementPageOut(Envelope):
    items: list[MeasurementOut]
    count: int
    next: str | None
    previous: str | None
    has_more: bool


class MeasurementDetailOut(MeasurementOut, Envelope):
    components: list[MeasurementOut]


class SummaryItem(Schema):
    metric: str
    count: int
    first: str
    last: str


class SummaryOut(Envelope):
    count: int
    items: list[SummaryItem]


class ImportOut(Schema):
    id: int
    file_name: str
    content_sha256: str
    readings: int
    created: int
    skipped: int
    conflicts: int
    overwritten: int
    panels: int
    first_at: str | None
    last_at: str | None
    imported_at: str


class ImportsOut(Envelope):
    count: int
    first_at: str | None
    last_at: str | None
    items: list[ImportOut]
    next: str | None
    previous: str | None
    has_more: bool
