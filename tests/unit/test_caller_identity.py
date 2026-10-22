from handlers._common import caller_identity


def ev(arn):
    return {"requestContext": {"identity": {"userArn": arn}}}


def test_user_arn_is_unchanged():
    assert caller_identity(ev("arn:aws-us-gov:iam::1:user/app")) == "arn:aws-us-gov:iam::1:user/app"


def test_assumed_role_session_name_is_dropped():
    a = caller_identity(ev("arn:aws-us-gov:sts::1:assumed-role/OcrRole/session-a"))
    b = caller_identity(ev("arn:aws-us-gov:sts::1:assumed-role/OcrRole/session-b"))
    assert a == b == "arn:aws-us-gov:sts::1:assumed-role/OcrRole"
    assert a != caller_identity(ev("arn:aws-us-gov:sts::1:assumed-role/OtherRole/session-a"))


def test_missing_identity_is_unknown():
    assert caller_identity({}) == "unknown"
    assert caller_identity({"requestContext": {}}) == "unknown"
