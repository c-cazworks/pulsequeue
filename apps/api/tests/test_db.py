import asyncio
import json
from unittest.mock import MagicMock

import botocore.session
import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from app import db

ARN = "arn:aws:secretsmanager:us-east-2:123456789012:secret:rds!db-mock"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    session = MagicMock()
    monkeypatch.setattr(botocore.session, "get_session", lambda: session)
    monkeypatch.setenv("DB_SECRET_ARN", ARN)
    client = session.create_client.return_value
    client.create = session.create_client
    return client


def secret(password: str) -> dict[str, str]:
    return {"SecretString": json.dumps({"username": "u", "password": password})}


def test_uses_pgpassword_without_secret_arn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_SECRET_ARN", raising=False)
    assert db.password_source() is None


def test_region_comes_from_the_arn(client: MagicMock) -> None:
    db.password_source()
    assert client.create.call_args.kwargs["region_name"] == "us-east-2"


def test_every_connection_sees_the_rotated_password(client: MagicMock) -> None:
    client.get_secret_value.side_effect = [secret("old"), secret("new")]
    password = db.password_source()
    assert password is not None
    assert [asyncio.run(password()), asyncio.run(password())] == ["old", "new"]
    client.get_secret_value.assert_called_with(SecretId=ARN)


@pytest.mark.parametrize(
    "error",
    [
        NoCredentialsError(),
        ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetSecretValue"),
    ],
)
def test_aws_errors_look_like_a_database_outage(client: MagicMock, error: Exception) -> None:
    client.get_secret_value.side_effect = error
    password = db.password_source()
    assert password is not None
    with pytest.raises(ConnectionError):
        asyncio.run(password())
