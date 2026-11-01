from sqlalchemy import func, select

from mailroom.api_keys import hash_api_key
from mailroom.db import session_scope
from mailroom.models import ApiKey, Matter, Tenant
from mailroom.seed import MATTERS, seed


def counts() -> tuple[int, int, int]:
    with session_scope() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (Tenant, Matter, ApiKey)
        )


def test_seed_stores_only_the_hash_of_the_key():
    raw_key = seed()

    assert raw_key is not None
    with session_scope() as session:
        stored = session.scalars(select(ApiKey)).one()
    assert stored.key_hash == hash_api_key(raw_key)
    assert raw_key not in (stored.key_hash, stored.key_prefix, stored.name)
    assert len(stored.key_prefix) < len(raw_key) / 2


def test_seed_is_idempotent_and_never_replaces_a_key():
    seed()
    first = counts()

    assert seed() is None
    assert counts() == first == (1, len(MATTERS), 1)
