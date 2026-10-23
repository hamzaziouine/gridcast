"""storage/objstore.py against moto (same code path for R2, B2 and MinIO endpoints)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from moto import mock_aws

from gridcast.storage.objstore import (
    ObjectStore,
    ObjstoreNotConfiguredError,
    ObjstoreSettings,
    make_client,
    private_store,
)

SETTINGS = ObjstoreSettings(
    _env_file=None,  # type: ignore[call-arg]
    endpoint_url=None,
    region="us-east-1",
    access_key_id="test",
    secret_access_key="test",  # noqa: S106 - moto dummy
    ro_access_key_id="test-ro",
    ro_secret_access_key="test-ro",  # noqa: S106 - moto dummy
    bucket_private="gridcast-artifacts",
)


@pytest.fixture
def store() -> Iterator[ObjectStore]:
    with mock_aws():
        client = make_client(SETTINGS)
        client.create_bucket(Bucket=SETTINGS.bucket_private)
        yield private_store(settings=SETTINGS)


def test_bytes_round_trip_and_head(store: ObjectStore) -> None:
    assert not store.exists("models/x.bin")
    store.put_bytes("models/x.bin", b"\x00\x01payload")
    assert store.exists("models/x.bin")
    assert store.get_bytes("models/x.bin") == b"\x00\x01payload"


def test_json_round_trip(store: ObjectStore) -> None:
    store.put_json("models/champion.json", {"model_name": "lgbm", "n": 1})
    assert store.get_json("models/champion.json") == {"model_name": "lgbm", "n": 1}


def test_list_keys_paginates(store: ObjectStore) -> None:
    for i in range(5):
        store.put_bytes(f"raw/a/{i:04d}", b"")
    store.put_bytes("other/z", b"")
    keys = list(store.list_keys("raw/a/", page_size=2))
    assert keys == [f"raw/a/{i:04d}" for i in range(5)]


def test_read_only_client_uses_ro_keys(store: ObjectStore) -> None:
    store.put_bytes("k", b"v")
    ro = private_store(read_only=True, settings=SETTINGS)
    assert ro.get_bytes("k") == b"v"


def test_missing_credentials_raise() -> None:
    s = ObjstoreSettings(_env_file=None, access_key_id=None, secret_access_key=None)  # type: ignore[call-arg]
    with pytest.raises(ObjstoreNotConfiguredError):
        make_client(s)
