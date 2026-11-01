"""Loads fictional demo data. Safe to run repeatedly."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from mailroom.api_keys import generate_api_key, hash_api_key, key_prefix
from mailroom.db import session_scope
from mailroom.models import ApiKey, Matter, Tenant

TENANT_NAME = "Fictional Firm LLP"
API_KEY_NAME = "demo"
# Two matters share a client on purpose: a name alone can't decide between them.
MATTERS = (
    ("FIC-2026-001", "Aldermere Holdings"),
    ("FIC-2026-002", "Brightwater Cooperative"),
    ("FIC-2026-003", "Brightwater Cooperative"),
)


def seed() -> str | None:
    """Returns the raw API key when one is created. It can't be recovered afterwards."""
    with session_scope() as session:
        session.execute(insert(Tenant).values(name=TENANT_NAME).on_conflict_do_nothing())
        tenant = session.scalars(select(Tenant).where(Tenant.name == TENANT_NAME)).one()
        session.execute(
            insert(Matter)
            .values(
                [
                    {"tenant_id": tenant.id, "matter_number": number, "client_name": client}
                    for number, client in MATTERS
                ]
            )
            .on_conflict_do_nothing()
        )

        raw_key = generate_api_key()
        created = session.execute(
            insert(ApiKey)
            .values(
                tenant_id=tenant.id,
                name=API_KEY_NAME,
                key_prefix=key_prefix(raw_key),
                key_hash=hash_api_key(raw_key),
            )
            .on_conflict_do_nothing()
            .returning(ApiKey.id)
        ).first()
    return raw_key if created else None


if __name__ == "__main__":
    key = seed()
    print(f"Seeded tenant '{TENANT_NAME}' with {len(MATTERS)} matters.")
    if key:
        print(f"Demo API key, shown once: {key}")
    else:
        print("Demo API key already exists.")
