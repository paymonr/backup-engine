from app.engine import buckets

def test_slugify():
    assert buckets.slugify("My Photos!") == "my-photos"
    assert buckets.slugify("appdata_backups") == "appdata-backups"

def test_suggest():
    assert buckets.suggest("unraid-backup-123", "Photos") == "unraid-backup-123-photos"

def test_is_prefixed():
    assert buckets.is_prefixed("be-123", "be-123-photos") is True
    assert buckets.is_prefixed("be-123", "be-123") is True
    assert buckets.is_prefixed("be-123", "totally-other") is False

def test_valid_bucket_name():
    assert buckets.valid_bucket_name("be-123-photos") is True
    assert buckets.valid_bucket_name("BadCaps") is False
    assert buckets.valid_bucket_name("ab") is False            # too short
    assert buckets.valid_bucket_name("a..b") is False
    assert buckets.valid_bucket_name("192.168.1.1") is False   # IP-like


def test_valid_bucket_name_refuses_a_trailing_newline():
    # Shape checks (review Minor 6): match()+"$" tolerates a trailing "\n" (it
    # matches just before it) -- fullmatch closes that hole.
    assert buckets.valid_bucket_name("good-name\n") is False
