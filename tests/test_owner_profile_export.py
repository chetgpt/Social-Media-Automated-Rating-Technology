import json

from participant_profile_export import _export_path, _field_summary
from tiktok_scraper.owner_profile_export import (
    build_owner_export,
    extract_owner_fields_from_payload,
    normalize_owner_fields,
    owner_response_url_matches,
    read_encrypted_export,
    select_owner_fields,
    write_encrypted_export,
)


def test_owner_field_selection_excludes_unknown_and_secret_values():
    selected = select_owner_fields(
        {
            "username": "owner",
            "gender": "owner-provided",
            "city_name": "Jakarta",
            "email": "",
            "session_token": "must-not-export",
        }
    )

    assert selected == {
        "username": "owner",
        "gender": "owner-provided",
        "city_name": "Jakarta",
    }


def test_normalization_supports_platform_aliases_and_camel_case():
    normalized = normalize_owner_fields(
        {
            "uniqueId": "owner",
            "fullName": "Owner Name",
            "dateOfBirth": "2000-01-02",
            "cityName": "Jakarta",
        }
    )

    assert normalized == {
        "username": "owner",
        "display_name": "Owner Name",
        "birthday": "2000-01-02",
        "city": "Jakarta",
    }


def test_payload_extractor_prefers_rich_owner_node():
    payload = {
        "suggestions": [
            {"id": "other-1", "name": "Other One"},
            {"id": "other-2", "name": "Other Two"},
        ],
        "data": {
            "viewer": {
                "user": {
                    "id": "owner-id",
                    "screen_name": "owner",
                    "name": "Owner Name",
                    "gender": "owner-provided",
                    "birthday": "2000-01-02",
                    "location": "Jakarta",
                }
            }
        },
    }

    selected = extract_owner_fields_from_payload(payload)

    assert selected["screen_name"] == "owner"
    assert selected["birthday"] == "2000-01-02"
    assert selected["location"] == "Jakarta"


def test_owner_response_url_matching_stays_on_first_party_surfaces():
    assert owner_response_url_matches(
        "instagram",
        "https://www.instagram.com/api/v1/accounts/current_user/?edit=true",
    )
    assert owner_response_url_matches(
        "x",
        "https://x.com/i/api/1.1/account/settings.json",
    )
    assert not owner_response_url_matches(
        "instagram",
        "https://attacker.example/api/v1/accounts/current_user/",
    )
    assert not owner_response_url_matches(
        "instagram",
        "https://www.instagram.com/api/v1/feed/timeline/",
    )


def test_export_groups_explicit_demographic_and_geo_fields():
    payload = build_owner_export(
        platform="instagram",
        profile={
            "username": "owner",
            "birthday": "2000-01-02",
            "city_name": "Jakarta",
            "country_code": "62",
        },
        sources=[
            {
                "endpoint_path": "/api/v1/accounts/edit/web_form_data/",
                "http_status": 200,
            }
        ],
        exported_at="2026-07-18T00:00:00+00:00",
    )

    assert payload["inference_used"] is False
    assert payload["demographics"] == {"birthday": "2000-01-02"}
    assert payload["geography"] == {"city": "Jakarta"}
    assert payload["contacts"]["phone_country_code"] == "62"


def test_encrypted_export_round_trip_does_not_store_plain_values(tmp_path):
    payload = build_owner_export(
        platform="instagram",
        profile={"username": "owner", "gender": "owner-provided"},
        sources=[],
        exported_at="2026-07-18T00:00:00+00:00",
    )
    path = tmp_path / "owner-profile.json.dpapi"

    write_encrypted_export(path, payload)

    encrypted_text = path.read_text(encoding="utf-8")
    assert "owner-provided" not in encrypted_text
    assert read_encrypted_export(path) == payload


def test_cli_summary_does_not_include_private_values(tmp_path):
    payload = build_owner_export(
        platform="instagram",
        profile={
            "username": "private-owner",
            "birthday": "2000-01-02",
            "city_name": "Private City",
        },
        sources=[],
        exported_at="2026-07-18T00:00:00+00:00",
    )

    summary = _field_summary(payload)
    serialized = json.dumps(summary)

    assert "private-owner" not in serialized
    assert "2000-01-02" not in serialized
    assert "Private City" not in serialized
    assert summary["demographic_field_names"] == ["birthday"]
    assert summary["geographic_field_names"] == ["city"]
    assert _export_path(
        tmp_path,
        platform="instagram",
        multiple=True,
        plaintext=False,
    ).name == "instagram.owner-profile.json.dpapi"
