"""Admin doctor management endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from app.models import User, UserRole

MakeUser = Callable[..., Awaitable[User]]
Headers = dict[str, str]

BASE = "/admin/doctors"


def doctor_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "email": f"doctor-{uuid.uuid4().hex[:8]}@example.com",
        "password": "a-long-enough-password",
        "full_name": "Dr Asha Rao",
        "specialisation": "Cardiology",
        "slot_duration_minutes": 30,
        "working_hours": [
            {"weekday": 0, "start_time": "09:00:00", "end_time": "17:00:00"},
        ],
    }
    payload.update(overrides)
    return payload


async def create_doctor(client: AsyncClient, headers: Headers, **overrides: Any) -> dict[str, Any]:
    response = await client.post(BASE, headers=headers, json=doctor_payload(**overrides))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------- access control


async def test_admin_can_create_a_doctor(client: AsyncClient, admin_headers: Headers) -> None:
    body = await create_doctor(client, admin_headers)

    assert body["specialisation"] == "Cardiology"
    assert body["slot_duration_minutes"] == 30
    assert body["is_active"] is True
    assert len(body["working_hours"]) == 1


async def test_patient_cannot_manage_doctors(client: AsyncClient, patient_headers: Headers) -> None:
    response = await client.post(BASE, headers=patient_headers, json=doctor_payload())

    assert response.status_code == 403


async def test_doctor_cannot_manage_doctors(
    client: AsyncClient,
    make_user: MakeUser,
    auth_header: Callable[[User], Headers],
) -> None:
    """A doctor administers patients, not the clinic's roster."""
    doctor = await make_user(role=UserRole.DOCTOR)

    response = await client.post(BASE, headers=auth_header(doctor), json=doctor_payload())

    assert response.status_code == 403


async def test_creating_a_doctor_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(BASE, json=doctor_payload())

    assert response.status_code == 401


async def test_a_created_doctor_can_log_in(client: AsyncClient, admin_headers: Headers) -> None:
    """The profile is useless without a working account, so the two are created together."""
    await create_doctor(client, admin_headers, email="asha@example.com")

    login = await client.post(
        "/auth/login", json={"email": "asha@example.com", "password": "a-long-enough-password"}
    )

    assert login.status_code == 200
    me = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}
    )
    assert me.json()["role"] == "doctor"


# ---------------------------------------------------------------- creation validation


async def test_duplicate_email_is_rejected(client: AsyncClient, admin_headers: Headers) -> None:
    await create_doctor(client, admin_headers, email="taken@example.com")

    response = await client.post(
        BASE, headers=admin_headers, json=doctor_payload(email="taken@example.com")
    )

    assert response.status_code == 409


async def test_overlapping_working_hours_are_rejected_with_an_explanation(
    client: AsyncClient, admin_headers: Headers
) -> None:
    response = await client.post(
        BASE,
        headers=admin_headers,
        json=doctor_payload(
            working_hours=[
                {"weekday": 0, "start_time": "09:00:00", "end_time": "13:00:00"},
                {"weekday": 0, "start_time": "12:00:00", "end_time": "17:00:00"},
            ]
        ),
    )

    assert response.status_code == 422
    assert "Monday" in response.json()["detail"]


async def test_working_hours_that_do_not_divide_into_slots_are_rejected(
    client: AsyncClient, admin_headers: Headers
) -> None:
    response = await client.post(
        BASE,
        headers=admin_headers,
        json=doctor_payload(
            slot_duration_minutes=45,
            working_hours=[{"weekday": 0, "start_time": "09:00:00", "end_time": "17:00:00"}],
        ),
    )

    assert response.status_code == 422
    assert "16:30" in response.json()["detail"]


async def test_a_backwards_window_is_rejected(client: AsyncClient, admin_headers: Headers) -> None:
    response = await client.post(
        BASE,
        headers=admin_headers,
        json=doctor_payload(
            working_hours=[{"weekday": 0, "start_time": "17:00:00", "end_time": "09:00:00"}]
        ),
    )

    assert response.status_code == 422


async def test_a_doctor_can_be_created_without_working_hours(
    client: AsyncClient, admin_headers: Headers
) -> None:
    body = await create_doctor(client, admin_headers, working_hours=[])

    assert body["working_hours"] == []


# ---------------------------------------------------------------- listing and retrieval


async def test_doctors_can_be_filtered_by_specialisation(
    client: AsyncClient, admin_headers: Headers
) -> None:
    await create_doctor(client, admin_headers, specialisation="Cardiology")
    await create_doctor(client, admin_headers, specialisation="Dermatology")

    response = await client.get(BASE, headers=admin_headers, params={"specialisation": "cardio"})

    assert response.status_code == 200
    assert [doctor["specialisation"] for doctor in response.json()] == ["Cardiology"]


async def test_unknown_doctor_returns_not_found(
    client: AsyncClient, admin_headers: Headers
) -> None:
    response = await client.get(f"{BASE}/{uuid.uuid4()}", headers=admin_headers)

    assert response.status_code == 404


# ---------------------------------------------------------------- updates


async def test_specialisation_can_be_changed(client: AsyncClient, admin_headers: Headers) -> None:
    doctor = await create_doctor(client, admin_headers)

    response = await client.patch(
        f"{BASE}/{doctor['id']}", headers=admin_headers, json={"specialisation": "Neurology"}
    )

    assert response.status_code == 200
    assert response.json()["specialisation"] == "Neurology"


async def test_changing_slot_duration_revalidates_the_existing_schedule(
    client: AsyncClient, admin_headers: Headers
) -> None:
    """A duration that no longer divides the working day would create unbookable gaps."""
    doctor = await create_doctor(client, admin_headers)  # Monday 09:00-17:00, 30-minute slots

    response = await client.patch(
        f"{BASE}/{doctor['id']}", headers=admin_headers, json={"slot_duration_minutes": 45}
    )

    assert response.status_code == 422
    assert "16:30" in response.json()["detail"]


async def test_a_compatible_slot_duration_is_accepted(
    client: AsyncClient, admin_headers: Headers
) -> None:
    doctor = await create_doctor(client, admin_headers)

    response = await client.patch(
        f"{BASE}/{doctor['id']}", headers=admin_headers, json={"slot_duration_minutes": 60}
    )

    assert response.status_code == 200
    assert response.json()["slot_duration_minutes"] == 60


async def test_deactivated_doctors_are_hidden_unless_asked_for(
    client: AsyncClient, admin_headers: Headers
) -> None:
    doctor = await create_doctor(client, admin_headers)

    await client.patch(f"{BASE}/{doctor['id']}", headers=admin_headers, json={"is_active": False})

    default_listing = await client.get(BASE, headers=admin_headers)
    assert default_listing.json() == []

    full_listing = await client.get(BASE, headers=admin_headers, params={"include_inactive": True})
    assert len(full_listing.json()) == 1


async def test_an_empty_update_is_rejected(client: AsyncClient, admin_headers: Headers) -> None:
    doctor = await create_doctor(client, admin_headers)

    response = await client.patch(f"{BASE}/{doctor['id']}", headers=admin_headers, json={})

    assert response.status_code == 422


# ---------------------------------------------------------------- working hours


async def test_replacing_the_schedule_swaps_it_rather_than_appending(
    client: AsyncClient, admin_headers: Headers
) -> None:
    doctor = await create_doctor(client, admin_headers)

    response = await client.put(
        f"{BASE}/{doctor['id']}/working-hours",
        headers=admin_headers,
        json={
            "working_hours": [
                {"weekday": 1, "start_time": "10:00:00", "end_time": "13:00:00"},
                {"weekday": 3, "start_time": "14:00:00", "end_time": "17:00:00"},
            ]
        },
    )

    assert response.status_code == 200
    weekdays = [row["weekday"] for row in response.json()["working_hours"]]
    assert weekdays == [1, 3]  # the original Monday window is gone, not kept alongside


async def test_the_schedule_can_be_cleared(client: AsyncClient, admin_headers: Headers) -> None:
    doctor = await create_doctor(client, admin_headers)

    response = await client.put(
        f"{BASE}/{doctor['id']}/working-hours",
        headers=admin_headers,
        json={"working_hours": []},
    )

    assert response.status_code == 200
    assert response.json()["working_hours"] == []


async def test_replacing_with_an_overlapping_schedule_is_rejected(
    client: AsyncClient, admin_headers: Headers
) -> None:
    doctor = await create_doctor(client, admin_headers)

    response = await client.put(
        f"{BASE}/{doctor['id']}/working-hours",
        headers=admin_headers,
        json={
            "working_hours": [
                {"weekday": 2, "start_time": "09:00:00", "end_time": "12:00:00"},
                {"weekday": 2, "start_time": "11:00:00", "end_time": "14:00:00"},
            ]
        },
    )

    assert response.status_code == 422


async def test_a_rejected_replacement_leaves_the_previous_schedule_intact(
    client: AsyncClient, admin_headers: Headers
) -> None:
    """A failed edit must not half-apply and leave the doctor with no hours."""
    doctor = await create_doctor(client, admin_headers)

    await client.put(
        f"{BASE}/{doctor['id']}/working-hours",
        headers=admin_headers,
        json={
            "working_hours": [
                {"weekday": 2, "start_time": "09:00:00", "end_time": "12:00:00"},
                {"weekday": 2, "start_time": "11:00:00", "end_time": "14:00:00"},
            ]
        },
    )

    after = await client.get(f"{BASE}/{doctor['id']}", headers=admin_headers)
    assert [row["weekday"] for row in after.json()["working_hours"]] == [0]


# ---------------------------------------------------------------- leave


async def test_leave_can_be_recorded(client: AsyncClient, admin_headers: Headers) -> None:
    doctor = await create_doctor(client, admin_headers)
    leave_date = (date.today() + timedelta(days=7)).isoformat()

    response = await client.post(
        f"{BASE}/{doctor['id']}/leave",
        headers=admin_headers,
        json={"leave_date": leave_date, "reason": "Conference"},
    )

    assert response.status_code == 201
    assert response.json()["leave_date"] == leave_date


async def test_leave_in_the_past_is_rejected(client: AsyncClient, admin_headers: Headers) -> None:
    doctor = await create_doctor(client, admin_headers)

    response = await client.post(
        f"{BASE}/{doctor['id']}/leave",
        headers=admin_headers,
        json={"leave_date": (date.today() - timedelta(days=1)).isoformat()},
    )

    assert response.status_code == 422
    assert "in the past" in response.json()["detail"]


async def test_today_can_be_taken_as_leave(client: AsyncClient, admin_headers: Headers) -> None:
    doctor = await create_doctor(client, admin_headers)

    response = await client.post(
        f"{BASE}/{doctor['id']}/leave",
        headers=admin_headers,
        json={"leave_date": date.today().isoformat()},
    )

    assert response.status_code == 201


async def test_the_same_leave_date_cannot_be_recorded_twice(
    client: AsyncClient, admin_headers: Headers
) -> None:
    doctor = await create_doctor(client, admin_headers)
    leave_date = (date.today() + timedelta(days=3)).isoformat()
    payload = {"leave_date": leave_date}

    first = await client.post(f"{BASE}/{doctor['id']}/leave", headers=admin_headers, json=payload)
    second = await client.post(f"{BASE}/{doctor['id']}/leave", headers=admin_headers, json=payload)

    assert first.status_code == 201
    assert second.status_code == 409


async def test_leave_can_be_removed(client: AsyncClient, admin_headers: Headers) -> None:
    doctor = await create_doctor(client, admin_headers)
    created = await client.post(
        f"{BASE}/{doctor['id']}/leave",
        headers=admin_headers,
        json={"leave_date": (date.today() + timedelta(days=5)).isoformat()},
    )
    leave_id = created.json()["id"]

    deleted = await client.delete(f"{BASE}/{doctor['id']}/leave/{leave_id}", headers=admin_headers)

    assert deleted.status_code == 204
    after = await client.get(f"{BASE}/{doctor['id']}", headers=admin_headers)
    assert after.json()["leave_days"] == []


async def test_leave_cannot_be_removed_through_another_doctor(
    client: AsyncClient, admin_headers: Headers
) -> None:
    """The leave id is scoped to its doctor in the query, not just in the URL."""
    owner = await create_doctor(client, admin_headers)
    bystander = await create_doctor(client, admin_headers)

    created = await client.post(
        f"{BASE}/{owner['id']}/leave",
        headers=admin_headers,
        json={"leave_date": (date.today() + timedelta(days=5)).isoformat()},
    )
    leave_id = created.json()["id"]

    response = await client.delete(
        f"{BASE}/{bystander['id']}/leave/{leave_id}", headers=admin_headers
    )

    assert response.status_code == 404


@pytest.mark.parametrize("missing_id_path", ["", "/leave"])
async def test_operations_on_an_unknown_doctor_return_not_found(
    client: AsyncClient, admin_headers: Headers, missing_id_path: str
) -> None:
    unknown = uuid.uuid4()

    if missing_id_path == "/leave":
        response = await client.post(
            f"{BASE}/{unknown}/leave",
            headers=admin_headers,
            json={"leave_date": (date.today() + timedelta(days=2)).isoformat()},
        )
    else:
        response = await client.patch(
            f"{BASE}/{unknown}", headers=admin_headers, json={"specialisation": "Neurology"}
        )

    assert response.status_code == 404
