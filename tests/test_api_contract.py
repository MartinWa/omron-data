import json

from django.http import HttpResponse
from django.test import Client

from records.contract import CONTRACT_VERSION, MAX_LIMIT
from records.core import _handle_unexpected
from records.middleware import _converted
from records.models import COMPONENTS, METRICS, PANEL, Measurement

ENVELOPE = {"contract_version", "generated_at"}


def test_every_success_body_carries_the_envelope(auth):
    paths = (
        "/api/v1/",
        "/api/v1/healthz",
        "/api/v1/readyz",
        "/api/v1/metrics",
        "/api/v1/measurements",
        "/api/v1/summary",
        "/api/v1/imports",
    )

    for path in paths:
        body = auth.get(path).json()
        assert ENVELOPE.issubset(body), path


def test_every_error_body_carries_the_envelope_and_an_error_object(auth):
    responses = [
        Client().get("/api/v1/summary"),
        auth.get("/api/v1/measurements/9999999"),
        auth.post("/api/v1/measurements"),
        auth.get("/api/v1/measurements", {"limit": 0}),
    ]

    assert [response.status_code for response in responses] == [401, 404, 405, 422]
    for response in responses:
        body = response.json()
        assert ENVELOPE.issubset(body)
        assert set(body["error"]) == {"code", "message", "details"}


def test_an_unknown_query_parameter_is_refused_and_named(auth):
    response = auth.get("/api/v1/measurements", {"colour": "red"})

    assert response.status_code == 422
    assert "colour" in response.json()["error"]["message"]
    assert response.json()["error"]["details"] == ["colour"]


def test_a_repeated_query_parameter_is_refused(auth):
    response = auth.get("/api/v1/measurements?metric=pulse&metric=pulse")

    assert response.status_code == 422
    assert "metric" in response.json()["error"]["message"]


def test_a_limit_above_the_maximum_is_refused(auth):
    assert auth.get("/api/v1/measurements", {"limit": MAX_LIMIT + 1}).status_code == 422


def test_a_zero_limit_is_refused(auth):
    assert auth.get("/api/v1/measurements", {"limit": 0}).status_code == 422


def test_a_valid_limit_is_accepted(auth):
    response = auth.get("/api/v1/measurements", {"limit": 2})

    assert response.status_code == 200
    assert len(response.json()["items"]) == 2


def test_a_reversed_time_range_is_refused(auth):
    response = auth.get(
        "/api/v1/measurements",
        {"since": "2024-07-01T00:00:00Z", "until": "2024-06-01T00:00:00Z"},
    )

    assert response.status_code == 422
    assert "since" in response.json()["error"]["message"]


def test_a_range_wider_than_the_maximum_is_refused(auth):
    response = auth.get(
        "/api/v1/measurements",
        {"since": "2019-01-01T00:00:00Z", "until": "2024-06-01T00:00:00Z"},
    )

    assert response.status_code == 422
    assert "1830" in response.json()["error"]["message"]


def test_a_datetime_without_an_offset_is_refused(auth):
    response = auth.get("/api/v1/measurements", {"since": "2024-06-01T00:00:00"})

    assert response.status_code == 422


def test_a_plain_date_is_refused(auth):
    response = auth.get("/api/v1/measurements", {"since": "2024-06-01"})

    assert response.status_code == 422


def test_a_time_window_filters_the_page(auth):
    body = auth.get(
        "/api/v1/measurements",
        {"since": "2024-06-15T00:00:00Z", "until": "2024-07-02T00:00:00Z"},
    ).json()

    assert body["count"] == 2


def test_an_unsupported_sort_key_is_refused(auth):
    response = auth.get("/api/v1/measurements", {"sort": "note"})

    assert response.status_code == 422
    assert "note" in response.json()["error"]["message"]


def test_value_and_at_are_accepted_sort_keys(auth):
    assert auth.get("/api/v1/measurements", {"sort": "value"}).status_code == 200
    assert auth.get("/api/v1/measurements", {"sort": "-at"}).status_code == 200


def test_an_unsupported_level_is_refused(auth):
    response = auth.get("/api/v1/measurements", {"level": "everything"})

    assert response.status_code == 422
    assert "everything" in response.json()["error"]["message"]


def test_a_component_metric_at_panel_level_is_refused(auth):
    refused = auth.get("/api/v1/measurements", {"metric": COMPONENTS[0], "level": "panel"})
    accepted = auth.get("/api/v1/measurements", {"metric": COMPONENTS[0], "level": "component"})

    assert refused.status_code == 422
    assert "level=component" in refused.json()["error"]["message"]
    assert accepted.status_code == 200
    assert {item["metric"] for item in accepted.json()["items"]} == {COMPONENTS[0]}


def test_a_panel_metric_at_component_level_is_refused(auth):
    refused = auth.get("/api/v1/measurements", {"metric": PANEL, "level": "component"})
    accepted = auth.get("/api/v1/measurements", {"metric": PANEL, "level": "all"})

    assert refused.status_code == 422
    assert "level=panel" in refused.json()["error"]["message"]
    assert accepted.status_code == 200
    assert {item["metric"] for item in accepted.json()["items"]} == {PANEL}


def test_healthz_answers_without_a_token_and_carries_the_envelope(db):
    response = Client().get("/api/v1/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert ENVELOPE.issubset(body)


def test_readyz_answers_without_a_token_when_the_database_is_migrated(db):
    response = Client().get("/api/v1/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert ENVELOPE.issubset(body)


def test_readyz_answers_503_with_the_envelope_when_migrations_are_pending(db, monkeypatch, caplog):
    import logging
    from types import SimpleNamespace

    from django.db.migrations.executor import MigrationExecutor

    pending = [(SimpleNamespace(app_label="records", name="0005_future"), False)]
    monkeypatch.setattr(MigrationExecutor, "migration_plan", lambda self, targets: pending)

    with caplog.at_level(logging.ERROR, logger="django"):
        response = Client().get("/api/v1/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"
    assert body["error"]["message"] == "Service temporarily unavailable"
    assert "0005_future" not in response.content.decode()
    assert ENVELOPE.issubset(body)
    assert any("records.0005_future" in record.getMessage() for record in caplog.records)


def test_readyz_answers_503_with_the_envelope_when_the_database_is_unreadable(
    db, monkeypatch, caplog
):
    import logging

    from django.db import OperationalError

    import records.routers.meta as meta

    class BrokenConnection:
        settings_dict = {"NAME": ":memory:", "ENGINE": "django.db.backends.sqlite3"}

        def is_in_memory_db(self):
            return True

        def cursor(self):
            raise OperationalError("disk I/O error")

    monkeypatch.setattr(meta, "connection", BrokenConnection())

    with caplog.at_level(logging.ERROR, logger="django"):
        response = Client().get("/api/v1/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "service_unavailable"
    assert body["error"]["message"] == "Service temporarily unavailable"
    assert "disk I/O error" not in response.content.decode()
    assert ENVELOPE.issubset(body)
    assert any(
        "Readiness check failed" in record.getMessage() and record.exc_info
        for record in caplog.records
    )


def test_readyz_refuses_a_missing_database_file_without_creating_it(db, monkeypatch, tmp_path):
    import records.routers.meta as meta

    missing = tmp_path / "gone.sqlite3"

    class MissingConnection:
        settings_dict = {
            "NAME": str(missing),
            "ENGINE": "django.db.backends.sqlite3",
        }

        def is_in_memory_db(self):
            return False

    monkeypatch.setattr(meta, "connection", MissingConnection())

    response = Client().get("/api/v1/readyz")

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Service temporarily unavailable"
    assert not missing.exists()


def test_readyz_treats_an_empty_database_file_as_pending_without_writing_to_it(
    db, monkeypatch, tmp_path
):
    import records.routers.meta as meta

    empty = tmp_path / "empty.sqlite3"
    empty.touch()

    class EmptyConnection:
        settings_dict = {
            "NAME": str(empty),
            "ENGINE": "django.db.backends.sqlite3",
        }

        def is_in_memory_db(self):
            return False

    monkeypatch.setattr(meta, "connection", EmptyConnection())

    response = Client().get("/api/v1/readyz")

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Service temporarily unavailable"
    assert empty.stat().st_size == 0


def test_metrics_matches_the_registry_with_units_and_kinds(auth):
    body = auth.get("/api/v1/metrics").json()

    assert body["count"] == len(METRICS)
    assert {item["code"]: item["unit"] for item in body["items"]} == {
        code: metric.unit for code, metric in METRICS.items()
    }
    kinds = {item["code"]: item["kind"] for item in body["items"]}
    assert kinds[PANEL] == "panel"
    assert {kinds[code] for code in COMPONENTS} == {"component"}
    assert kinds["body_weight"] == "reading"


def test_a_panel_detail_carries_its_components(auth, panel_ids):
    body = auth.get(f"/api/v1/measurements/{panel_ids[0]}").json()

    assert body["metric"] == PANEL
    assert body["value"] is None
    assert {item["metric"] for item in body["components"]} == set(COMPONENTS)
    assert all(item["parent_id"] == body["id"] for item in body["components"])


def test_an_unknown_measurement_is_not_found(auth):
    response = auth.get("/api/v1/measurements/9999999")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_another_owners_measurement_is_not_found(auth, bob):
    other = Measurement.objects.filter(owner=bob, parent__isnull=True).first()
    response = auth.get(f"/api/v1/measurements/{other.id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_level_panel_returns_only_top_level_rows(auth):
    body = auth.get("/api/v1/measurements", {"level": "panel"}).json()

    assert body["count"] == 4
    assert {item["metric"] for item in body["items"]} == {PANEL}
    assert all(item["parent_id"] is None for item in body["items"])


def test_level_component_returns_only_components(auth):
    body = auth.get("/api/v1/measurements", {"level": "component"}).json()

    assert body["count"] == 12
    assert {item["metric"] for item in body["items"]} == set(COMPONENTS)
    assert all(item["parent_id"] is not None for item in body["items"])


def test_level_all_returns_both(auth):
    body = auth.get("/api/v1/measurements", {"level": "all"}).json()

    assert body["count"] == 16
    assert {item["metric"] for item in body["items"]} == {PANEL, *COMPONENTS}


def test_include_components_adds_them_to_a_panel_item(auth):
    item = auth.get("/api/v1/measurements", {"include": "components", "limit": 1}).json()["items"][
        0
    ]

    assert item["value"] is None
    assert {component["metric"] for component in item["components"]} == set(COMPONENTS)


def test_a_panel_without_include_carries_no_components(auth):
    item = auth.get("/api/v1/measurements", {"limit": 1}).json()["items"][0]

    assert item["components"] is None


def test_paging_is_stable_and_the_next_url_keeps_the_filters(auth):
    first = auth.get("/api/v1/measurements", {"metric": PANEL, "level": "panel", "limit": 2}).json()

    assert first["count"] == 4
    assert first["has_more"] is True
    assert "metric=blood_pressure" in first["next"]
    assert "level=panel" in first["next"]
    assert "offset=2" in first["next"]

    second = auth.get(first["next"]).json()
    ids = [item["id"] for item in first["items"] + second["items"]]
    ats = [item["at"] for item in first["items"] + second["items"]]
    assert len(set(ids)) == 4
    assert ats == sorted(ats, reverse=True)
    assert second["has_more"] is False
    assert "offset=0" in second["previous"]


def test_a_write_method_is_refused_with_a_json_body(auth):
    for method in (auth.post, auth.put, auth.delete):
        response = method("/api/v1/measurements")
        assert response.status_code == 405
        assert response["Content-Type"].startswith("application/json")
        assert "read-only" in response.json()["error"]["message"]


def test_head_is_allowed(auth):
    assert auth.head("/api/v1/measurements").status_code == 200


def test_cache_control_is_no_store_on_success_and_on_rejection(auth):
    assert auth.get("/api/v1/summary")["Cache-Control"] == "no-store"
    assert Client().get("/api/v1/summary")["Cache-Control"] == "no-store"


def test_a_rejected_token_carries_the_bearer_challenge(db):
    response = Client().get("/api/v1/summary")

    assert response.status_code == 401
    assert response["WWW-Authenticate"] == "Bearer"


def test_the_api_root_names_the_contract_and_the_endpoints(auth):
    body = auth.get("/api/v1/").json()

    assert body["contract_version"] == CONTRACT_VERSION
    assert body["name"] == "omron-data"
    assert "/api/v1/measurements" in body["endpoints"]


def test_the_metric_filter_advertises_the_registry(auth):
    schema = auth.get("/api/v1/openapi.json").json()
    parameters = schema["paths"]["/api/v1/measurements"]["get"]["parameters"]
    metric = next(parameter for parameter in parameters if parameter["name"] == "metric")

    assert metric["schema"]["enum"] == sorted(METRICS)
    assert auth.get(f"/api/v1/measurements?metric={sorted(METRICS)[0]}").status_code == 200
    assert auth.get("/api/v1/measurements?metric=not_a_metric").status_code == 422


def test_the_openapi_version_is_the_contract_version(auth):
    schema = auth.get("/api/v1/openapi.json").json()
    envelope = auth.get("/api/v1/").json()

    assert schema["info"]["version"] == CONTRACT_VERSION
    assert schema["info"]["version"] == envelope["contract_version"]


def test_the_renamed_metric_codes_are_a_breaking_contract_change(auth):
    assert auth.get("/api/v1/").json()["contract_version"] == CONTRACT_VERSION
    assert auth.get("/api/v1/measurements?metric=fat_mass").status_code == 200
    assert auth.get("/api/v1/measurements?metric=fat_free_mass").status_code == 422
    assert auth.get("/api/v1/measurements?metric=average_weight").status_code == 422


def test_an_unknown_api_path_is_json_with_the_envelope(auth):
    response = auth.get("/api/v1/nothing-here")

    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/json")
    assert ENVELOPE.issubset(response.json())
    assert response.json()["error"]["code"] == "not_found"


def test_summary_lists_each_metric_once_in_order(auth):
    body = auth.get("/api/v1/summary").json()

    metrics = [item["metric"] for item in body["items"]]
    assert body["count"] == 4
    assert metrics == sorted(metrics)
    assert sum(item["count"] for item in body["items"]) == 16


def test_imports_reports_the_owners_runs(auth):
    body = auth.get("/api/v1/imports").json()

    assert body["count"] == 1
    item = body["items"][0]
    assert item["file_name"] == "alice.csv"
    assert item["first_at"].endswith("Z")
    assert item["first_at"] == body["first_at"]
    assert item["last_at"] == body["last_at"]
    assert body["has_more"] is False


def test_bob_sees_none_of_alices_rows(alice, bob_auth):
    measurements = bob_auth.get("/api/v1/measurements", {"level": "all"}).json()
    alice_ids = set(Measurement.objects.filter(owner=alice).values_list("id", flat=True))

    assert measurements["count"] == 4
    assert not alice_ids & {item["id"] for item in measurements["items"]}

    summary = bob_auth.get("/api/v1/summary").json()
    assert {item["metric"] for item in summary["items"]} == {PANEL, *COMPONENTS}
    assert sum(item["count"] for item in summary["items"]) == 4

    imports = bob_auth.get("/api/v1/imports").json()
    assert [item["file_name"] for item in imports["items"]] == ["bob.csv"]


def test_the_openapi_schema_documents_the_error_responses(auth):
    response = auth.get("/api/v1/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    responses = schema["paths"]["/api/v1/measurements"]["get"]["responses"]
    for status in ("404", "405", "422", "500"):
        assert status in responses
        assert responses[status]["content"]["application/json"]["schema"]["$ref"].endswith(
            "ErrorOut"
        )
    assert "401" in responses
    assert "ErrorOut" in schema["components"]["schemas"]
    assert "401" not in schema["paths"]["/api/v1/healthz"]["get"]["responses"]
    assert "401" not in schema["paths"]["/api/v1/readyz"]["get"]["responses"]


def test_the_api_base_url_without_a_slash_is_refused_with_the_envelope(auth):
    response = auth.get("/api/v1")

    assert response.status_code == 404
    assert response["Content-Type"].startswith("application/json")
    assert response["Cache-Control"] == "no-store"
    assert "/api/v1/" in response.json()["error"]["message"]


def test_a_write_to_the_api_base_url_is_refused_rather_than_redirected(auth):
    response = auth.post("/api/v1")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_every_http_method_other_than_get_and_head_is_refused(auth):
    for method in (auth.post, auth.put, auth.patch, auth.delete, auth.options):
        response = method("/api/v1/measurements")
        assert response.status_code == 405, method
        assert response["Cache-Control"] == "no-store"


def test_head_carries_the_envelope_and_no_store(auth):
    response = auth.head("/api/v1/measurements")

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"


def test_an_offset_above_the_maximum_is_refused(auth):
    assert auth.get("/api/v1/measurements", {"offset": 2**31}).status_code == 422
    assert auth.get("/api/v1/measurements", {"offset": 2**31 - 1}).status_code == 200


def test_an_offset_past_the_end_offers_no_previous_page(auth):
    body = auth.get("/api/v1/measurements", {"offset": 100}).json()

    assert body["items"] == []
    assert body["next"] is None
    assert body["previous"] is None


def test_an_unsupported_metric_is_refused(auth):
    response = auth.get("/api/v1/measurements", {"metric": "blood-pressure"})

    assert response.status_code == 422
    assert "blood-pressure" in response.json()["error"]["message"]


def test_the_maximum_range_is_inclusive(auth):
    at_maximum = {"since": "2024-01-01T00:00:00Z", "until": "2029-01-04T00:00:00Z"}
    assert auth.get("/api/v1/measurements", at_maximum).status_code == 200

    past_maximum = {"since": "2024-01-01T00:00:00Z", "until": "2029-01-04T00:00:01Z"}
    assert auth.get("/api/v1/measurements", past_maximum).status_code == 422


def test_the_id_column_is_a_sort_key(auth):
    body = auth.get("/api/v1/measurements", {"sort": "id"}).json()

    ids = [item["id"] for item in body["items"]]
    assert ids == sorted(ids)


def test_cache_control_is_no_store_on_every_refusal(auth):
    responses = [
        auth.get("/api/v1/measurements", {"limit": 0}),
        auth.get("/api/v1/nothing-here"),
        auth.post("/api/v1/measurements"),
        Client().get("/api/v1/summary"),
    ]

    assert [response.status_code for response in responses] == [422, 404, 405, 401]
    assert {response["Cache-Control"] for response in responses} == {"no-store"}


def test_a_crowded_query_string_is_a_bad_request(auth):
    crowded = {f"p{index}": index for index in range(5001)}
    response = auth.get("/api/v1/measurements", crowded)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_an_unexpected_failure_is_json_without_the_exception_text(db):
    response = _handle_unexpected(None, RuntimeError("synthetic detail"))

    assert response.status_code == 500
    body = json.loads(response.content)
    assert ENVELOPE.issubset(body)
    assert body["error"]["code"] == "internal_error"
    assert "synthetic detail" not in body["error"]["message"]


def test_an_html_error_under_the_api_prefix_is_converted_to_json():
    response = _converted(HttpResponse("<h1>boom</h1>", status=500, content_type="text/html"))

    assert response.status_code == 500
    assert response["Content-Type"].startswith("application/json")
    assert ENVELOPE.issubset(json.loads(response.content))
