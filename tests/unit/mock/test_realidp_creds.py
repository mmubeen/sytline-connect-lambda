"""T047: reading the developer's temporary credentials (real-IDP mode, FR-018)."""

import os
import time

import pytest

from edge import realidp

SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
TOKEN = "IQoJb3JpZ2luX2VjEXAMPLETOKENVALUE"
KEY = "ASIAEXAMPLEKEYID"


def write(path, expires, mode=0o600, key=KEY):
    body = (f"AWS_ACCESS_KEY_ID={key}\nAWS_SECRET_ACCESS_KEY={SECRET}\nAWS_SESSION_TOKEN={TOKEN}\n"
            f"AWS_CREDENTIAL_EXPIRATION={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(expires))}\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(body)
    os.chmod(path, mode)


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "creds")


def test_missing_file(path):
    c = realidp.Credentials(path)
    assert c.status() == "credentials missing"
    with pytest.raises(realidp.CredentialsUnavailable):
        c.load()


def test_valid_credentials(path):
    write(path, time.time() + 3600)
    c = realidp.Credentials(path)
    assert c.status() == "up"
    assert c.load() == (KEY, SECRET, TOKEN)


def test_expired_credentials_say_when(path):
    write(path, time.time() - 60)
    c = realidp.Credentials(path)
    assert c.status().startswith("credentials expired at ")
    with pytest.raises(realidp.CredentialsUnavailable) as e:
        c.load()
    assert "expired" in str(e.value)


def test_a_refreshed_file_takes_effect_without_a_restart(path):
    write(path, time.time() - 60)
    c = realidp.Credentials(path)
    assert c.status() != "up"
    write(path, time.time() + 3600, key="ASIANEWKEY")
    assert c.status() == "up" and c.load()[0] == "ASIANEWKEY"


def test_a_file_readable_by_others_is_refused(path):
    write(path, time.time() + 3600, mode=0o644)
    assert "readable by others" in realidp.Credentials(path).status()


def test_incomplete_file(path):
    open(path, "w").write("AWS_ACCESS_KEY_ID=x\n")
    os.chmod(path, 0o600)
    assert "incomplete" in realidp.Credentials(path).status()


def test_nothing_secret_is_ever_in_a_repr_or_message(path):
    write(path, time.time() - 60)
    c = realidp.Credentials(path)
    texts = [repr(c), str(c), c.status()]
    try:
        c.load()
    except realidp.CredentialsUnavailable as e:
        texts.append(str(e))
    write(path, time.time() + 3600)
    texts += [repr(c), str(c), c.status()]
    for t in texts:
        assert SECRET not in t and TOKEN not in t and KEY not in t
